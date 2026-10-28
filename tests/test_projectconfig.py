"""The project file: parsing, profiles, located errors, and the daemon applying it."""

import asyncio
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli, exits
from dromon.daemon import Overrides, ProjectMismatch, apply_project, resolve
from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog
from dromon.limits import DEFAULT_LIMITS, Limits
from dromon.lineconfig import ToolPolicy, encode_config
from dromon.model import LineAddress, OperatorAddress
from dromon.projectconfig import ConfigFileError, ProjectConfig, load, parse
from dromon.scheduler import LineEngine, LineSpec, Scheduler

EXAMPLE = """\
version = 1

[daemon]
operator = "ana"
tick_seconds = 0.5

[limits]
max_autonomous_turns = 8
max_cost_usd = 1.5

[wake]
every_ticks = 2

[profiles.reader]
model = "claude-haiku-5-5"
strategy = "subscription"
builtin_tools = ["Read", "Grep"]

[profiles.reader.mcp_servers.docs]
type = "http"
url = "https://docs.example/mcp"

[lines.alpha]
profile = "reader"

[lines.beta]
profile = "reader"
model = "claude-opus-5-5"
builtin_tools = ["Read"]
tool_policy = "allow_all"

[lines.beta.limits]
max_autonomous_turns = 3

[lines.beta.wake]
every_ticks = 5
"""


def test_a_full_file_parses_with_profiles_and_per_line_overrides() -> None:
    config = parse(EXAMPLE)
    assert (config.operator, config.tick_seconds) == ("ana", 0.5)
    assert config.limits == Limits(8, 1.5, 0.0)
    assert config.wake_every_ticks == 2
    alpha, beta = config.lines["alpha"], config.lines["beta"]
    assert (alpha.model, alpha.strategy) == ("claude-haiku-5-5", Strategy.SUBSCRIPTION)
    assert alpha.config.builtin_tools == ("Grep", "Read")
    assert alpha.config.server_names() == {"docs"}
    assert (alpha.limits, alpha.wake_every_ticks) == (None, None)
    assert beta.model == "claude-opus-5-5"
    # A line's own key replaces its profile's whole.
    assert beta.config.builtin_tools == ("Read",)
    assert beta.config.tool_policy is ToolPolicy.ALLOW_ALL
    assert beta.config.server_names() == {"docs"}
    assert beta.limits == Limits(3, 1.5, 0.0)
    assert beta.wake_every_ticks == 5


def test_a_profile_gives_its_players_their_recipients() -> None:
    config = parse(
        "version = 1\n"
        "[profiles.player]\n"
        'model = "claude-haiku-5-5"\n'
        'recipients = ["&table"]\n'
        "[lines.north]\n"
        'profile = "player"\n'
        "[lines.dealer]\n"
        'profile = "player"\n'
        'recipients = ["&table", "@ana"]\n'
        "[lines.free]\n"
        'model = "claude-haiku-5-5"\n'
    )
    lines = config.lines
    assert lines["north"].config.recipients == ("&table",)
    assert lines["dealer"].config.recipients == ("&table", "@ana")
    assert lines["free"].config.recipients is None
    with pytest.raises(ConfigFileError, match=r"lines\.free: recipients"):
        parse(
            'version = 1\n[lines.free]\nmodel = "claude-haiku-5-5"\n'
            'recipients = ["!dromon"]\n'
        )


def test_a_minimal_file_takes_the_defaults() -> None:
    config = parse("version = 1\n")
    assert config == ProjectConfig()
    assert config.limits == DEFAULT_LIMITS


def test_a_project_grants_only_named_mcp_secrets_to_each_line() -> None:
    config = parse(
        """\
version = 1

[lines.alpha]
model = "claude-haiku-5-5"
secrets = ["ALPHA_TOKEN"]

[lines.alpha.mcp_servers.docs]
type = "http"
url = "https://docs.example/mcp"
headers = { Authorization = "Bearer ${ALPHA_TOKEN}" }

[lines.beta]
model = "claude-haiku-5-5"
secrets = ["BETA_TOKEN"]

[lines.beta.mcp_servers.docs]
type = "http"
url = "https://docs.example/mcp"
headers = { Authorization = "Bearer ${BETA_TOKEN}" }
"""
    )
    assert config.lines["alpha"].secret_names == ("ALPHA_TOKEN",)
    assert config.lines["beta"].secret_names == ("BETA_TOKEN",)


def test_a_line_cannot_reference_a_secret_not_granted_by_the_project() -> None:
    with pytest.raises(
        ConfigFileError, match="does not grant MCP variable.*BETA_TOKEN"
    ):
        parse(
            """\
version = 1

[lines.alpha]
model = "claude-haiku-5-5"
secrets = ["ALPHA_TOKEN"]

[lines.alpha.mcp_servers.docs]
type = "http"
url = "https://docs.example/mcp"
headers = { Authorization = "Bearer ${BETA_TOKEN}" }
"""
        )


BAD: list[tuple[str, str, str, int | None]] = [
    ("syntax", "version = 1\nlines = = 2\n", "Invalid value", 2),
    ("no version", "[daemon]\noperator = 'ana'\n", "version: must be 1", None),
    ("a future version", "version = 2\n", "version: must be 1", 1),
    (
        "an unknown top key",
        "version = 1\nmodel = 'x'\n",
        "model: is not a known key",
        2,
    ),
    (
        "an unknown line key",
        "version = 1\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\nmodle = 'x'\n",
        "lines.alpha.modle: is not a known key",
        2,
    ),
    (
        "an unknown model",
        "version = 1\n\n[lines.alpha]\nmodel = 'claude-sonnet-5-5'\n",
        "lines.alpha.model: must be one of",
        3,
    ),
    (
        "an unknown strategy",
        "version = 1\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\nstrategy = 'free'\n",
        "lines.alpha.strategy: must be one of",
        2,
    ),
    (
        "a missing profile",
        "version = 1\n[lines.alpha]\nprofile = 'ghost'\n",
        "lines.alpha.profile: names no profile",
        2,
    ),
    (
        "a bad limit",
        "version = 1\n[limits]\nmax_autonomous_turns = 0\n",
        "limits: max_autonomous_turns must be in",
        2,
    ),
    (
        "a limit of the wrong type",
        "version = 1\n[limits]\nmax_cost_usd = 'cheap'\n",
        "limits.max_cost_usd: must be a number",
        2,
    ),
    (
        "a bad wake policy",
        "version = 1\n[wake]\nevery_ticks = 0\n",
        "wake.every_ticks: must be an integer",
        2,
    ),
    (
        "a bad operator",
        "version = 1\n[daemon]\noperator = 'Ana!'\n",
        "daemon.operator: must be an operator identifier",
        2,
    ),
    (
        "a bad tick",
        "version = 1\n[daemon]\ntick_seconds = 99\n",
        "daemon.tick_seconds: must be a number",
        2,
    ),
    (
        "a bad tool configuration",
        "version = 1\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\n"
        "builtin_tools = ['read']\n",
        "lines.alpha: built-in tool name 'read' is malformed",
        2,
    ),
    (
        "a bad line identifier",
        "version = 1\n[lines.Alpha]\nmodel = 'claude-haiku-5-5'\n",
        "lines.Alpha: is not a valid line identifier",
        2,
    ),
]


@pytest.mark.parametrize(
    ("text", "message", "line"),
    [case[1:] for case in BAD],
    ids=[case[0] for case in BAD],
)
def test_an_invalid_file_is_refused_with_a_located_message(
    text: str, message: str, line: int | None
) -> None:
    with pytest.raises(ConfigFileError) as raised:
        parse(text, Path("project/.dromon/dromon.toml"))
    error = raised.value
    assert message in str(error)
    assert str(error).startswith("project/.dromon/dromon.toml")
    assert error.line == line


def test_load_reports_unreadable_and_non_utf8_files(tmp_path: Path) -> None:
    with pytest.raises(ConfigFileError, match="cannot be read"):
        load(tmp_path / "absent.toml")
    binary = tmp_path / "binary.toml"
    binary.write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigFileError, match="not UTF-8"):
        load(binary)


# Precedence and application by the daemon.


def test_flags_override_the_file_which_overrides_the_defaults() -> None:
    config = parse(EXAMPLE)
    assert resolve(config, Overrides()) == (Limits(8, 1.5, 0.0), "ana", 0.5)
    flagged = Overrides(operator="ben", tick_seconds=2.0, max_cost_usd=9.0)
    assert resolve(config, flagged) == (Limits(8, 9.0, 0.0), "ben", 2.0)
    assert resolve(ProjectConfig(), Overrides()) == (DEFAULT_LIMITS, "operator", 1.0)


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start, but {spec.line_id} did")


def test_the_daemon_creates_and_reconfigures_declared_lines(tmp_path: Path) -> None:
    config = parse(EXAMPLE)
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("beta", "claude-opus-5-5", Strategy.SUBSCRIPTION)
        scheduler.create_line("gamma", "claude-haiku-5-5", Strategy.API)
        apply_project(scheduler, log, config)
        state = log.state
        assert state.lines["alpha"].config == encode_config(
            config.lines["alpha"].config
        )
        assert state.lines["beta"].config == encode_config(config.lines["beta"].config)
        assert state.lines["gamma"].config is None
        assert scheduler.limits_for("beta") == Limits(3, 1.5, 0.0)
        assert scheduler.limits_for("alpha") == scheduler.limits
        before = len(log.state.messages), log.last_seq
        apply_project(scheduler, log, config)
        assert (len(log.state.messages), log.last_seq) == before


def test_a_file_that_changes_a_lines_model_is_refused(tmp_path: Path) -> None:
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("alpha", "claude-opus-5-5", Strategy.SUBSCRIPTION)
        with pytest.raises(ProjectMismatch, match="create a new line instead"):
            apply_project(scheduler, log, parse(EXAMPLE))


def test_dromond_refuses_to_start_on_an_invalid_file(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from dromon import daemon

    state = tmp_path / ".dromon"
    state.mkdir(mode=0o700)
    (state / "dromon.toml").write_text("version = 1\n[lines.alpha]\nmodel = 1\n")
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime)]
    assert daemon.main(argv) == exits.FAILURE
    err = capsys.readouterr().err
    assert "dromon.toml:2: lines.alpha.model: must be one of" in err
    assert not short_runtime.exists() or not (short_runtime / "dromond.sock").exists()


# The wake policy.


class Counting:
    def __init__(self) -> None:
        self.session_id: str | None = None
        self.ticks: list[int] = []

    async def run_turn(self, prompt: str) -> TurnReport:
        return TurnReport("success", 1, "", (), None, None, "s", None, None)


def test_a_line_is_woken_only_on_its_wake_ticks(tmp_path: Path) -> None:
    with EventLog(tmp_path / "events.sqlite") as log:
        started: list[int] = []
        holder: list[Scheduler] = []

        class Engine(Counting):
            async def run_turn(self, prompt: str) -> TurnReport:
                started.append(holder[0].tick)
                return await super().run_turn(prompt)

        scheduler = Scheduler(
            log, lambda spec, tools: Engine(), tick_seconds=0, wake_every_ticks=3
        )
        holder.append(scheduler)
        scheduler.ensure_operator("ana")
        scheduler.create_line("alpha", "claude-haiku-5-5", Strategy.SUBSCRIPTION)

        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(scheduler.run(stop))
            scheduler.send(OperatorAddress("ana"), LineAddress("alpha"), "hi")
            await asyncio.sleep(0)
            await scheduler.idle()
            stop.set()
            await loop

        asyncio.run(go())
        assert started and all(tick % 3 == 0 for tick in started)


def test_a_bad_wake_policy_is_refused(tmp_path: Path) -> None:
    with EventLog(tmp_path / "events.sqlite") as log:
        with pytest.raises(ValueError, match="wake_every_ticks"):
            Scheduler(log, no_engine, wake_every_ticks=0)
        scheduler = Scheduler(log, no_engine)
        with pytest.raises(ValueError, match="wake_every_ticks"):
            scheduler.set_line_policy("alpha", None, 0)


# The dromon config command.


def test_config_reports_a_valid_file(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / ".dromon"
    state.mkdir(mode=0o700)
    (state / "dromon.toml").write_text(EXAMPLE)
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "config"]
    assert cli.main(argv) == exits.OK
    out = capsys.readouterr().out
    assert "is valid" in out
    assert "line beta: claude-opus-5-5 subscription, allow_all, Read, docs" in out


def test_config_reports_each_lines_effective_sandbox_write_grant(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / ".dromon"
    state.mkdir(mode=0o700)
    (state / "dromon.toml").write_text(
        """\
version = 1

[profiles.reader]
model = "claude-haiku-5-5"
strategy = "subscription"

[profiles.reader.sandbox]
enabled = true
enableWeakerNetworkIsolation = true

[profiles.reader.sandbox.filesystem]
allowWrite = ["/shared/repo/.git", "~/.cargo/registry"]

[lines.alpha]
profile = "reader"
workdir = "worktrees/alpha"

[lines.alpha.sandbox.filesystem]
allowWrite = [".", "../../cache/alpha"]

[lines.beta]
profile = "reader"
workdir = "worktrees/beta"

[lines.beta.sandbox.filesystem]
allowWrite = [".", "../../cache/beta"]
"""
    )
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "config"]

    assert cli.main(argv) == exits.OK

    out = capsys.readouterr().out
    alpha, beta = out.split("line alpha:", 1)[1].split("line beta:", 1)
    assert f"{tmp_path}/worktrees/alpha" in alpha
    assert f"{tmp_path}/cache/alpha" in alpha
    assert f"{tmp_path}/worktrees/beta" not in alpha
    assert f"{tmp_path}/cache/beta" not in alpha
    assert f"{tmp_path}/worktrees/beta" in beta
    assert f"{tmp_path}/cache/beta" in beta
    assert f"{tmp_path}/worktrees/alpha" not in beta
    assert f"{tmp_path}/cache/alpha" not in beta
    assert "/shared/repo/.git" in alpha and "/shared/repo/.git" in beta
    assert str(Path.home() / ".cargo/registry") in alpha
    assert str(Path.home() / ".cargo/registry") in beta


def test_config_reports_an_invalid_file_with_its_location(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / ".dromon"
    state.mkdir(mode=0o700)
    (state / "dromon.toml").write_text("version = 1\n\n[wake]\nevery_ticks = -1\n")
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "config"]
    assert cli.main(argv) == exits.FAILURE
    assert "dromon.toml:3: wake.every_ticks" in capsys.readouterr().err


def test_config_without_a_file_reports_the_defaults(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "config"]
    assert cli.main(argv) == exits.OK
    assert "Built-in defaults apply" in capsys.readouterr().out
