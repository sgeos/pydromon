"""Round 18, last pass, item 6. ``[daemon] default_profile`` names a profile that a
line created through the client takes when it runs on the profile's engine, so this
project's client-created Claude Code lines get the ``.git`` write denials and ask
rules its profile gives the lines it declares.

**Confirmed before the change.** ``Daemon._create_line`` built the line's configuration
from the client's document alone, ``config_from_document``, and no profile reached
it, so a line created through the client had none of a profile's sandbox or
settings."""

import json
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli, exits
from dromon.antigravity import ISOLATION_REFUSAL
from dromon.configview import Overrides
from dromon.configwarnings import config_warnings
from dromon.daemon import Daemon, check_engines
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.lineconfig import DEFAULT_CONFIG, ConfigError
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import (
    HELD_SANDBOX_KEYS,
    ConfigFileError,
    ProjectConfig,
    apply_default_profile,
    config_path,
    load,
    parse,
)
from dromon.protocol import Response
from dromon.sandbox import TABLES
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
CODEX = "gpt-6-sol"
ANTIGRAVITY = "antigravity:gemini-3.8-flash-high"

PROFILE = """\
[daemon]
default_profile = "claude"

[profiles.claude]
model = "claude-opus-5-5"
builtin_tools = ["Bash", "Read"]
effort = "high"

[profiles.claude.sandbox.filesystem]
denyWrite = ["/work/.git/objects"]

[profiles.claude.settings.permissions]
ask = ["Bash(git commit:*)"]
"""


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


class Client:
    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon

    def ask(self, op: str, args: dict[str, object]) -> Response:
        line = {"id": "r", "op": op, "v": 1, "args": args}
        return self.daemon.respond((json.dumps(line) + "\n").encode())

    def ok(self, op: str, args: dict[str, object]) -> None:
        response = self.ask(op, args)
        assert response.ok, response.error_message


def loaded(paths: ProjectPaths, text: str) -> ProjectConfig:
    config_path(paths.state_dir).write_text("version = 1\n" + text, encoding="utf-8")
    return load(config_path(paths.state_dir))


@contextmanager
def attached(paths: ProjectPaths, project: ProjectConfig) -> Generator[Client]:
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        daemon = Daemon(
            paths,
            log,
            "2026-10-25T00:00:00Z",
            scheduler,
            project=project,
            command_line=Overrides(),
            project_file_present=True,
        )
        daemon.apply_project_values()
        yield Client(daemon)


def create(
    line_id: str, model: str, config: dict[str, object] | None = None
) -> dict[str, object]:
    args: dict[str, object] = {
        "line_id": line_id,
        "model": model,
        "strategy": "subscription",
    }
    if config is not None:
        args["config"] = config
    return args


# The project file.


def test_no_default_profile_is_named_by_default() -> None:
    assert parse("version = 1\n").default_profile is None


def test_the_named_profile_is_read_with_its_engine() -> None:
    profile = parse("version = 1\n" + PROFILE).default_profile
    assert profile is not None
    assert profile.name == "claude" and profile.engine == "claude-code"
    assert profile.tools == {"builtin_tools": ["Bash", "Read"], "effort": "high"}
    assert profile.sandbox is not None and profile.settings is not None
    assert not profile.git_ssh


@pytest.mark.parametrize(
    ("text", "said"),
    [
        pytest.param(
            '[daemon]\ndefault_profile = "none"\n', "names no profile", id="unknown"
        ),
        pytest.param("[daemon]\ndefault_profile = 3\n", "names no profile", id="type"),
        pytest.param(
            '[daemon]\ndefault_profile = "p"\n[profiles.p]\nbuiltin_tools = []\n',
            "names no model",
            id="no-model",
        ),
        pytest.param(
            '[daemon]\ndefault_profile = "p"\n[profiles.p]\n'
            'model = "claude-haiku-5-5"\n'
            'secrets = ["TOKEN"]\n',
            "grants secrets",
            id="secrets",
        ),
        pytest.param(
            '[daemon]\ndefault_profile = "p"\n[profiles.p]\n'
            'model = "claude-haiku-5-5"\n'
            'tool_policy = "sometimes"\n',
            "daemon.default_profile",
            id="a-bad-tool-key",
        ),
    ],
)
def test_a_profile_no_client_line_could_take_is_refused(text: str, said: str) -> None:
    with pytest.raises(ConfigFileError, match=said) as raised:
        parse("version = 1\n" + text)
    assert raised.value.key.startswith("daemon.default_profile")


def test_a_profile_its_engine_refuses_refuses_the_start() -> None:
    """An Antigravity profile can never be applied, since the daemon refuses every
    Antigravity line, so naming one is refused at start rather than at a creation."""
    config = parse(
        'version = 1\n[daemon]\ndefault_profile = "g"\n'
        f"[profiles.g]\nmodel = {ANTIGRAVITY!r}\n"
    )
    with pytest.raises(ValueError, match="default_profile names profile 'g'"):
        check_engines(config)
    check_engines(parse("version = 1\n" + PROFILE))


def test_a_profile_s_git_ssh_is_warned_of_and_not_applied() -> None:
    text = (
        'version = 1\n[git_ssh]\nagent = "inherit"\n'
        'known_hosts = "/k/known_hosts"\npublic_key = "/k/id.pub"\n'
        + PROFILE.replace('effort = "high"\n', 'effort = "high"\ngit_ssh = true\n')
    )
    config = parse(text)
    assert config.default_profile is not None and config.default_profile.git_ssh
    found = [w for w in config_warnings(config, Path("/work")) if "git_ssh" in w]
    assert len(found) == 1 and "not given the [git_ssh] agent" in found[0]
    assert not [
        w
        for w in config_warnings(parse("version = 1\n" + PROFILE), Path("/work"))
        if "default_profile" in w
    ]


# Applying it.


def test_a_line_on_another_engine_is_left_as_the_client_sent_it() -> None:
    profile = parse("version = 1\n" + PROFILE).default_profile
    for model in (CODEX, ANTIGRAVITY):
        config, applied = apply_default_profile(profile, model, {})
        assert (config, applied) == (DEFAULT_CONFIG, False)
    assert apply_default_profile(None, HAIKU, {}) == (DEFAULT_CONFIG, False)


def test_a_line_on_its_engine_takes_it_under_the_client_s_keys() -> None:
    profile = parse("version = 1\n" + PROFILE).default_profile
    own = {
        "builtin_tools": ["Read"],
        "sandbox": {"filesystem": {"denyWrite": ["/work/own"]}},
        "settings": {"permissions": {"ask": ["Bash(git push:*)"]}},
    }
    config, applied = apply_default_profile(profile, HAIKU, own)
    assert applied
    assert config.builtin_tools == ("Read",)
    assert config.effort == "high"
    assert config.sandbox is not None and config.settings is not None
    deny = config.sandbox.document()["filesystem"]
    assert isinstance(deny, dict)
    assert deny["denyWrite"] == ["/work/.git/objects", "/work/own"]
    ask = json.dumps(config.settings.document()["permissions"])
    assert "Bash(git commit:*)" in ask and "Bash(git push:*)" in ask


def test_a_malformed_client_document_is_refused_with_or_without_it() -> None:
    profile = parse("version = 1\n" + PROFILE).default_profile
    for model in (HAIKU, CODEX):
        with pytest.raises(ConfigError):
            apply_default_profile(profile, model, {"tool_policy": "sometimes"})


# Through the daemon.


def test_a_line_created_through_the_client_takes_the_profile(
    paths: ProjectPaths,
) -> None:
    with attached(paths, loaded(paths, PROFILE)) as client:
        client.ok("create_line", create("beta", HAIKU))
        given = client.daemon.scheduler.effective_config("beta")
        assert given.effort == "high"
        assert given.sandbox is not None
        assert "/work/.git/objects" in json.dumps(given.sandbox.document())
        assert given.settings is not None
        assert "Bash(git commit:*)" in json.dumps(given.settings.document())


def test_a_line_on_another_engine_is_created_without_it(paths: ProjectPaths) -> None:
    """A Codex line takes no sandbox document, and an Antigravity line is refused
    for the isolation reason it always was, and not for the profile."""
    with attached(paths, loaded(paths, PROFILE)) as client:
        client.ok("create_line", create("cx", CODEX))
        given = client.daemon.scheduler.effective_config("cx")
        assert given.sandbox is None and given.settings is None
        refused = client.ask("create_line", create("ag", ANTIGRAVITY))
        assert not refused.ok
        assert refused.error_message == ISOLATION_REFUSAL


def test_a_project_without_one_creates_lines_as_before(paths: ProjectPaths) -> None:
    with attached(paths, loaded(paths, "")) as client:
        client.ok("create_line", create("beta", HAIKU))
        assert client.daemon.scheduler.effective_config("beta") == DEFAULT_CONFIG


def test_a_configure_keeps_it_for_a_line_the_file_does_not_declare(
    paths: ProjectPaths,
) -> None:
    """So a client cannot drop the profile's denials by configuring the line again,
    and so a line created before the key was named takes it when configured."""
    with attached(paths, loaded(paths, "")) as client:
        client.ok("create_line", create("beta", HAIKU))
        assert client.daemon.scheduler.effective_config("beta").sandbox is None
    with attached(paths, loaded(paths, PROFILE)) as client:
        client.ok("configure_line", {"line_id": "beta", "config": {"effort": "low"}})
        given = client.daemon.scheduler.effective_config("beta")
        assert given.effort == "low"
        assert given.sandbox is not None
        assert "/work/.git/objects" in json.dumps(given.sandbox.document())


def test_a_configure_of_a_declared_line_does_not_take_it(paths: ProjectPaths) -> None:
    text = PROFILE + "[lines.alpha]\nmodel = 'claude-haiku-5-5'\n"
    with EventLog(paths.event_log) as log:
        Scheduler(log, no_engine, hold_wakes=True).create_line(
            "alpha", HAIKU, Strategy.SUBSCRIPTION
        )
    with attached(paths, loaded(paths, text)) as client:
        client.ok("configure_line", {"line_id": "alpha", "config": {}})
        assert client.daemon.scheduler.effective_config("alpha").sandbox is None


def test_dromon_config_names_the_profile(
    paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    loaded(paths, PROFILE)
    argv = [
        "--project",
        str(paths.root),
        "--runtime-dir",
        str(paths.runtime_dir),
        "config",
    ]
    assert cli.main(argv) == exits.OK
    assert "default profile: claude, for lines on claude-code" in (
        capsys.readouterr().out
    )


# Round 18, addendum 2, item 1, and addendum 3.

SAMPLES: dict[tuple[str, ...], tuple[object, ...]] = {
    ("excludedCommands",): (["git"],),
    ("network", "tlsTerminate"): (True, False),
    ("network", "httpProxyPort"): (8080,),
    ("network", "socksProxyPort"): (1080,),
    ("network", "allowUnixSockets"): (["/var/run/other.sock"],),
    ("network", "allowMachLookup"): (["com.example.service"],),
}
"""A valid value for each key a client may not set at all. Every other held key is
refused at the boolean that is not its secure one."""


def nested(path: tuple[str, ...], value: object) -> dict[str, object]:
    """A sandbox document that sets only ``path`` to ``value``."""
    document: dict[str, object] = {path[-1]: value}
    for step in reversed(path[:-1]):
        document = {step: document}
    return document


def refused_values(path: tuple[str, ...], secure: bool | None) -> tuple[object, ...]:
    return SAMPLES[path] if secure is None else (not secure,)


HELD = [
    pytest.param(
        nested(path, value),
        "sandbox." + ".".join(path),
        id=".".join(path) + "=" + json.dumps(value),
    )
    for path, secure in HELD_SANDBOX_KEYS
    for value in refused_values(path, secure)
]

ALLOWED: dict[tuple[str, ...], object] = {
    ("autoAllowBashIfSandboxed",): False,
    ("ignoreViolations",): {"git *": ["/work/out"]},
    ("filesystem", "allowWrite"): ["/work/out"],
    ("filesystem", "denyWrite"): ["/work/own"],
    ("filesystem", "denyRead"): ["/work/private"],
    ("filesystem", "allowRead"): ["/work/docs"],
    ("network", "allowedDomains"): ["example.com", "*.example.org"],
    ("network", "deniedDomains"): ["example.net"],
    ("credentials", "files"): [{"path": "/work/token", "mode": "deny"}],
    ("credentials", "envVars"): [{"name": "SOME_TOKEN", "mode": "deny"}],
}
"""Every key a client may still set under a default profile, with a value for it."""


def test_every_sandbox_key_is_either_held_or_allowed() -> None:
    """So a key the parser learns later has to be placed on one side, rather than
    reaching a client unexamined. Derived from the parser's own tables."""
    known = {
        (key,) if table == "" else (table, key)
        for table, keys in TABLES.items()
        for key, spec in keys.items()
        if spec.check is not None
    }
    held = {path for path, _ in HELD_SANDBOX_KEYS}
    assert not held & set(ALLOWED)
    assert held | set(ALLOWED) == known
    assert {path for path, secure in HELD_SANDBOX_KEYS if secure is None} == set(
        SAMPLES
    )


def test_the_keys_addendum_3_names_are_held() -> None:
    held = {".".join(path) for path, _ in HELD_SANDBOX_KEYS}
    named = {
        "allowAppleEvents",
        "allowNestedUserNamespaces",
        "network.allowAllUnixSockets",
        "enableWeakerNestedSandbox",
        "network.strictAllowlist",
        "allowUnsandboxedCommands",
        "excludedCommands",
    }
    assert named <= held


def test_a_client_may_still_add_grants_and_denials() -> None:
    """The negative path. Entries inside the sandbox's bounds are accepted, and are
    united with the profile's."""
    profile = parse("version = 1\n" + PROFILE).default_profile
    sandbox: dict[str, object] = {}
    for path, value in ALLOWED.items():
        table = sandbox if len(path) == 1 else sandbox.setdefault(path[0], {})
        assert isinstance(table, dict)
        cast(dict[str, object], table)[path[-1]] = value
    config, applied = apply_default_profile(profile, HAIKU, {"sandbox": sandbox})
    assert applied and config.sandbox is not None
    given = config.sandbox.document()
    filesystem = given["filesystem"]
    network = given["network"]
    assert isinstance(filesystem, dict) and isinstance(network, dict)
    assert cast(dict[str, object], filesystem)["allowWrite"] == ["/work/out"]
    assert cast(dict[str, object], filesystem)["denyWrite"] == [
        "/work/.git/objects",
        "/work/own",
    ]
    assert cast(dict[str, object], network)["allowedDomains"] == [
        "*.example.org",
        "example.com",
    ]


@pytest.mark.parametrize(
    ("path", "value"), [pytest.param(p, v, id=".".join(p)) for p, v in ALLOWED.items()]
)
def test_each_allowed_key_is_accepted_alone(
    path: tuple[str, ...], value: object
) -> None:
    profile = parse("version = 1\n" + PROFILE).default_profile
    _, applied = apply_default_profile(profile, HAIKU, {"sandbox": nested(path, value)})
    assert applied


def test_a_list_the_profile_sets_itself_may_be_repeated_and_not_widened() -> None:
    """A key a client may not set at all may still be given the profile's own value,
    as for the switches, and any other value of it is refused."""
    text = PROFILE.replace(
        "[profiles.claude.sandbox.filesystem]",
        '[profiles.claude.sandbox.network]\nallowUnixSockets = ["/run/a.sock"]\n'
        "[profiles.claude.sandbox.filesystem]",
    )
    profile = parse("version = 1\n" + text).default_profile
    same = {"sandbox": nested(("network", "allowUnixSockets"), ["/run/a.sock"])}
    _, applied = apply_default_profile(profile, HAIKU, same)
    assert applied
    wider = {
        "sandbox": nested(("network", "allowUnixSockets"), ["/run/a.sock", "/run/b"])
    }
    with pytest.raises(ConfigError, match="sandbox.network.allowUnixSockets"):
        apply_default_profile(profile, HAIKU, wider)


@pytest.mark.parametrize(("sandbox", "key"), HELD)
def test_a_client_sandbox_that_weakens_the_profile_s_is_refused(
    sandbox: dict[str, object], key: str
) -> None:
    """Confirmed before the change, each of these was accepted, and the line's sandbox
    kept the profile's denials in a document that was disabled or optional."""
    profile = parse("version = 1\n" + PROFILE).default_profile
    with pytest.raises(ConfigError, match=f"{key} .* profile 'claude'"):
        apply_default_profile(profile, HAIKU, {"sandbox": sandbox})


@pytest.mark.parametrize(("sandbox", "key"), HELD)
def test_the_daemon_refuses_it_at_create_and_configure(
    paths: ProjectPaths, sandbox: dict[str, object], key: str
) -> None:
    with attached(paths, loaded(paths, PROFILE)) as client:
        refused = client.ask("create_line", create("beta", HAIKU, {"sandbox": sandbox}))
        assert not refused.ok and key in (refused.error_message or "")
        assert "beta" not in client.daemon.log.state.lines
        client.ok("create_line", create("beta", HAIKU))
        refused = client.ask(
            "configure_line", {"line_id": "beta", "config": {"sandbox": sandbox}}
        )
        assert not refused.ok and key in (refused.error_message or "")
        given = client.daemon.scheduler.effective_config("beta")
        assert given.sandbox is not None
        assert "/work/.git/objects" in json.dumps(given.sandbox.document())


@pytest.mark.parametrize(("sandbox", "key"), HELD)
def test_the_same_document_is_accepted_without_a_profile_or_on_another_engine(
    sandbox: dict[str, object], key: str
) -> None:
    """The negative paths. The refusal guards a profile's sandbox, so it applies only
    where a profile is applied."""
    document = {"sandbox": sandbox}
    apply_default_profile(None, HAIKU, document)
    profile = parse("version = 1\n" + PROFILE).default_profile
    _, applied = apply_default_profile(profile, CODEX, document)
    assert not applied


def test_the_secure_value_is_accepted() -> None:
    profile = parse("version = 1\n" + PROFILE).default_profile
    secure: dict[str, object] = {}
    for path, value in HELD_SANDBOX_KEYS:
        if value is None:
            continue
        table = secure if len(path) == 1 else secure.setdefault(path[0], {})
        assert isinstance(table, dict)
        cast(dict[str, object], table)[path[-1]] = value
    config, applied = apply_default_profile(profile, HAIKU, {"sandbox": secure})
    assert applied and config.sandbox is not None
    given = config.sandbox.document()
    assert given["enabled"] is True and given["failIfUnavailable"] is True
    assert given["allowUnsandboxedCommands"] is False


def test_a_weaker_value_the_profile_sets_itself_is_accepted() -> None:
    """The profile's own choice is the operator's, so a client that repeats it is not
    weakening anything."""
    text = PROFILE.replace(
        "[profiles.claude.sandbox.filesystem]",
        "[profiles.claude.sandbox]\nfailIfUnavailable = false\n"
        "[profiles.claude.sandbox.filesystem]",
    )
    profile = parse("version = 1\n" + text).default_profile
    config, _ = apply_default_profile(
        profile, HAIKU, {"sandbox": {"failIfUnavailable": False}}
    )
    assert config.sandbox is not None
    assert config.sandbox.document()["failIfUnavailable"] is False
    with pytest.raises(ConfigError, match="sandbox.enabled"):
        apply_default_profile(profile, HAIKU, {"sandbox": {"enabled": False}})


def test_the_client_s_layer_is_the_more_specific() -> None:
    """Pins the merge order. A scalar both layers set takes the client's value, here a
    stronger one over the profile's weaker one and a key that is not held, and the
    lists keep the profile's entries first. This fails if the layers are swapped."""
    text = PROFILE.replace(
        "[profiles.claude.sandbox.filesystem]",
        "[profiles.claude.sandbox]\nfailIfUnavailable = false\n"
        "autoAllowBashIfSandboxed = true\n"
        "[profiles.claude.sandbox.filesystem]",
    )
    profile = parse("version = 1\n" + text).default_profile
    own = {
        "sandbox": {
            "failIfUnavailable": True,
            "autoAllowBashIfSandboxed": False,
            "filesystem": {"denyWrite": ["/work/own"]},
        }
    }
    config, _ = apply_default_profile(profile, HAIKU, own)
    assert config.sandbox is not None
    given = config.sandbox.document()
    assert given["failIfUnavailable"] is True
    assert given["autoAllowBashIfSandboxed"] is False
    assert json.dumps(given["filesystem"]) == json.dumps(
        {"denyWrite": ["/work/.git/objects", "/work/own"]}
    )
