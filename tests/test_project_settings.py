"""``project_settings``: a Claude Code line that also loads the project setting source,
while Dromon's tool restriction, init check and sandbox still hold."""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import cast

import pytest
from claude_agent_sdk import ClaudeAgentOptions, Message, ResultMessage, SystemMessage

from dromon import codex, grok
from dromon.driver import Driver, Strategy
from dromon.engine import (
    MAX_PROJECT_SETTINGS_BYTES,
    PROJECT_SETTINGS_KEYS,
    AgentSession,
    ClaudeCodeLine,
    EngineRefused,
    check_project_settings,
)
from dromon.lineconfig import (
    DEFAULT_CONFIG,
    ConfigError,
    LineConfig,
    config_from_document,
    decode_config,
    encode_config,
)
from dromon.projectconfig import parse
from dromon.sandbox import parse_sandbox
from dromon.scheduler import LineSpec, engine_factory

HAIKU = "claude-haiku-5-5"
SANDBOX: dict[str, object] = {"enabled": True}
"""Every line that loads project settings needs an enabled sandbox."""


def driver() -> Driver:
    return Driver.for_model(HAIKU, Strategy.SUBSCRIPTION)


# The configuration.


def test_the_key_carries_through_the_log_and_is_omitted_when_off() -> None:
    config = config_from_document({"project_settings": True})
    assert config.project_settings is True
    assert decode_config(encode_config(config)) == config
    assert "project_settings" not in json.loads(encode_config(DEFAULT_CONFIG))
    with pytest.raises(ConfigError, match="project_settings must be a boolean"):
        config_from_document({"project_settings": "yes"})
    file = parse(
        "version = 1\n[lines.a]\nmodel = 'claude-haiku-5-5'\nproject_settings = true\n"
    )
    assert file.lines["a"].config.project_settings is True


def test_codex_and_grok_lines_refuse_it() -> None:
    config = LineConfig(project_settings=True)
    assert codex.unsupported(config) == (
        "Codex lines do not load Claude Code's project settings"
    )
    assert grok.unsupported(config) == (
        "Grok lines do not load Claude Code's project settings"
    )
    assert codex.unsupported(LineConfig()) is None


# The engine.


class Scripted:
    def __init__(self, messages: Sequence[Message]) -> None:
        self.messages = list(messages)
        self.interrupts = 0
        self.options: list[ClaudeAgentOptions] = []

    async def query(self, prompt: str) -> None:
        del prompt

    async def receive_response(self) -> AsyncIterator[Message]:
        for message in self.messages:
            yield message

    async def interrupt(self) -> None:
        self.interrupts += 1

    def factory(
        self, options: ClaudeAgentOptions
    ) -> AbstractAsyncContextManager[AgentSession]:
        self.options.append(options)

        @asynccontextmanager
        async def opened() -> AsyncGenerator[AgentSession]:
            yield self

        return opened()


def init(tools: list[str]) -> SystemMessage:
    return SystemMessage(
        subtype="init",
        data={
            "model": HAIKU,
            "apiKeySource": "none",
            "claude_code_version": "2.1.310",
            "tools": tools,
            "permissionMode": "default",
            "capabilities": [],
        },
    )


def result() -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="session-1",
        total_cost_usd=0.0,
    )


def test_the_project_source_loads_and_the_tools_stay_as_configured(
    tmp_path: Path,
) -> None:
    config = LineConfig(builtin_tools=("Read",), project_settings=True)
    target = ClaudeCodeLine(driver(), tmp_path, config=config, sandbox=SANDBOX)
    options = target.options()
    assert options.setting_sources == ["project"]
    assert options.tools == ["Read"]
    assert options.strict_mcp_config is True
    assert ClaudeCodeLine(driver(), tmp_path).options().setting_sources == []


def test_the_init_check_still_refuses_a_tool_the_line_was_not_given(
    tmp_path: Path,
) -> None:
    session = Scripted([init(["Read", "Skill", "Bash"]), result()])
    config = LineConfig(builtin_tools=("Read",), project_settings=True)
    target = ClaudeCodeLine(
        driver(),
        tmp_path,
        config=config,
        sandbox=SANDBOX,
        environment={},
        session_factory=session.factory,
    )
    with pytest.raises(
        EngineRefused, match="tools the line was not given: Bash, Skill"
    ):
        asyncio.run(target.run_turn("hello"))
    assert session.interrupts == 1


def write_settings(directory: Path, document: object) -> Path:
    (directory / ".claude").mkdir(exist_ok=True)
    file = directory / ".claude" / "settings.json"
    file.write_text(json.dumps(document), encoding="utf-8")
    return file


def test_only_allowlisted_keys_are_accepted(tmp_path: Path) -> None:
    check_project_settings(tmp_path)
    (tmp_path / ".claude").mkdir()
    check_project_settings(tmp_path)
    write_settings(
        tmp_path,
        {
            "$schema": "https://json.schemastore.org/claude-code-settings.json",
            "attribution": {"commit": "", "pr": ""},
            "includeCoAuthoredBy": False,
        },
    )
    check_project_settings(tmp_path)
    expected = {"$schema", "attribution", "includeCoAuthoredBy"}
    assert expected == PROJECT_SETTINGS_KEYS


@pytest.mark.parametrize(
    "key",
    [
        "hooks",
        "mcpServers",
        "enableAllProjectMcpServers",
        "enabledMcpjsonServers",
        "statusLine",
        "permissions",
        "sandbox",
        "env",
        "enabledPlugins",
        "extraKnownMarketplaces",
        "apiKeyHelper",
        "model",
        "outputStyle",
        "a-key-claude-code-adds-later",
    ],
)
def test_every_other_key_refuses_the_turn_naming_it(tmp_path: Path, key: str) -> None:
    write_settings(tmp_path, {"includeCoAuthoredBy": False, key: {}})
    with pytest.raises(
        EngineRefused, match=f"set {key}, which a Dromon line"
    ) as raised:
        check_project_settings(tmp_path)
    assert "settings.json" in str(raised.value)
    assert "Dromon's settings document" in str(raised.value)


@pytest.mark.parametrize(
    "entry", ["commands", "agents", "hooks", "output-styles", "rules", "notes.txt"]
)
def test_a_claude_directory_entry_outside_the_allowlist_refuses_the_turn(
    tmp_path: Path, entry: str
) -> None:
    (tmp_path / ".claude" / entry).mkdir(parents=True)
    with pytest.raises(EngineRefused, match=f"holds {entry}, which a Dromon line"):
        check_project_settings(tmp_path)


def write_skill(directory: Path, name: str, text: str) -> Path:
    skill = directory / ".claude" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(text, encoding="utf-8")
    return skill / "SKILL.md"


def test_skills_with_allowlisted_front_matter_are_accepted(tmp_path: Path) -> None:
    write_skill(
        tmp_path,
        "folded",
        "\ufeff---\nname: folded\n# a comment\n\ndescription: >\n  Checks types,\n"
        "  allowed-tools: Bash is text here.\nlicense: MIT\nversion: 1\n---\n"
        "# Body may say allowed-tools: Bash\n",
    )
    write_skill(tmp_path, "literal", "---\nname: literal\ndescription: |-\n  x\n---\n")
    write_skill(tmp_path, "plain", "No front matter at all.\n")
    check_project_settings(tmp_path)


@pytest.mark.parametrize("key", ["allowed-tools", "hooks", "model", "context"])
def test_a_skill_that_grants_or_runs_anything_refuses_the_turn(
    tmp_path: Path, key: str
) -> None:
    write_skill(tmp_path, "risky", f"---\nname: risky\n{key}: x\n---\nbody\n")
    with pytest.raises(EngineRefused, match=f"sets {key} in its front matter"):
        check_project_settings(tmp_path)


def test_a_malformed_claude_directory_or_skill_refuses_the_turn(tmp_path: Path) -> None:
    (tmp_path / ".claude").write_text("not a directory")
    with pytest.raises(EngineRefused, match="is not a directory"):
        check_project_settings(tmp_path)
    (tmp_path / ".claude").unlink()
    write_skill(tmp_path, "open", "---\nname: open\n")
    with pytest.raises(EngineRefused, match="front matter that does not end"):
        check_project_settings(tmp_path)


def test_an_unreadable_project_file_refuses_the_turn(tmp_path: Path) -> None:
    file = write_settings(tmp_path, {})
    file.write_text("{not json", encoding="utf-8")
    with pytest.raises(EngineRefused, match="are not JSON"):
        check_project_settings(tmp_path)
    file.write_text("[]", encoding="utf-8")
    with pytest.raises(EngineRefused, match="not a JSON object"):
        check_project_settings(tmp_path)
    file.write_bytes(b" " * (MAX_PROJECT_SETTINGS_BYTES + 1))
    with pytest.raises(EngineRefused, match="too large"):
        check_project_settings(tmp_path)
    file.unlink()
    file.mkdir()
    with pytest.raises(EngineRefused, match="cannot read"):
        check_project_settings(tmp_path)


def test_the_project_files_are_checked_at_the_start_of_every_turn(
    tmp_path: Path,
) -> None:
    session = Scripted([init([]), result()])
    target = ClaudeCodeLine(
        driver(),
        tmp_path,
        config=LineConfig(project_settings=True),
        sandbox=SANDBOX,
        environment={},
        session_factory=session.factory,
    )
    assert asyncio.run(target.run_turn("one")).subtype == "success"
    write_settings(tmp_path, {"hooks": {}})
    with pytest.raises(EngineRefused, match="set hooks"):
        asyncio.run(target.run_turn("two"))
    assert len(session.options) == 1
    plain = ClaudeCodeLine(driver(), tmp_path)
    assert plain.options().setting_sources == []


def test_any_line_needs_an_enabled_sandbox_to_load_project_settings(
    tmp_path: Path,
) -> None:
    for tools in ((), ("Read",)):
        bare = LineConfig(builtin_tools=tools, project_settings=True)
        with pytest.raises(EngineRefused, match="whatever its tools"):
            ClaudeCodeLine(driver(), tmp_path, config=bare).options()
    config = LineConfig(builtin_tools=("Bash",), project_settings=True)
    with pytest.raises(EngineRefused, match="needs an enabled sandbox"):
        ClaudeCodeLine(driver(), tmp_path, config=config).options()
    with pytest.raises(EngineRefused, match="needs an enabled sandbox"):
        ClaudeCodeLine(
            driver(), tmp_path, config=config, sandbox={"enabled": False}
        ).options()
    sandboxed = ClaudeCodeLine(
        driver(), tmp_path, config=config, sandbox={"enabled": True}
    )
    assert sandboxed.options().setting_sources == ["project"]


# The factory protects the file the line loads.


def test_the_factory_denies_writes_to_the_workdirs_claude_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    build = engine_factory(
        str(root), project_sandbox=parse_sandbox({"enabled": True}, operator=False)
    )
    spec = LineSpec(
        "alpha",
        HAIKU,
        Strategy.SUBSCRIPTION,
        LineConfig(builtin_tools=("Bash",), project_settings=True),
    )
    engine = build(spec, [])
    assert isinstance(engine, ClaudeCodeLine)
    sandbox = cast(dict[str, dict[str, list[str]]], engine.sandbox)
    assert str(root / ".claude") in sandbox["filesystem"]["denyWrite"]
    assert str(root / ".claude") not in sandbox["filesystem"]["denyRead"]
    settings = cast(dict[str, dict[str, list[str]]], engine.settings)
    assert f"Edit(/{root / '.claude'}/**)" in settings["permissions"]["deny"]
    assert not any(
        rule.startswith(f"Read(/{root / '.claude'}")
        for rule in settings["permissions"]["deny"]
    )
    plain = build(LineSpec("beta", HAIKU, Strategy.SUBSCRIPTION, LineConfig()), [])
    assert isinstance(plain, ClaudeCodeLine)
    plain_sandbox = cast(dict[str, dict[str, list[str]]], plain.sandbox)
    assert str(root / ".claude") not in plain_sandbox["filesystem"]["denyWrite"]


def test_the_settings_view_shows_it_as_information(
    tmp_path: Path, short_runtime: Path
) -> None:
    from typing import Any

    from claude_agent_sdk import SdkMcpTool

    from dromon.configview import Overrides
    from dromon.daemon import Daemon
    from dromon.eventlog import EventLog
    from dromon.paths import project_paths
    from dromon.projectconfig import ProjectConfig
    from dromon.scheduler import LineEngine, Scheduler

    def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        raise AssertionError(f"no engine may start, but {spec.line_id} did")

    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700)
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        target = Daemon(
            paths,
            log,
            "2026-10-26T00:00:00Z",
            scheduler,
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=False,
        )

        def entry() -> dict[str, object]:
            line = {"id": "r", "op": "settings", "v": 1, "args": {"line_id": "alpha"}}
            response = target.respond((json.dumps(line) + "\n").encode())
            assert response.ok and response.result is not None
            entries = cast(list[dict[str, object]], response.result["line"])
            return next(e for e in entries if e["key"] == "project_settings")

        assert (entry()["kind"], entry()["value"]) == ("info", "off")
        scheduler.configure_line("alpha", LineConfig(project_settings=True))
        assert entry()["value"] == "on"
        refused = target.respond(
            (
                json.dumps(
                    {
                        "id": "r",
                        "op": "set_setting",
                        "v": 1,
                        "args": {
                            "scope": "line",
                            "line_id": "alpha",
                            "key": "project_settings",
                            "value": False,
                        },
                    }
                )
                + "\n"
            ).encode()
        )
        assert not refused.ok


def test_the_claude_directory_is_write_protected_whatever_the_tools(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    build = engine_factory(
        str(root), project_sandbox=parse_sandbox({"enabled": True}, operator=False)
    )
    for tools in ((), ("Read",), ("Edit", "Write")):
        spec = LineSpec(
            "alpha",
            HAIKU,
            Strategy.SUBSCRIPTION,
            LineConfig(builtin_tools=tools, project_settings=True),
        )
        engine = build(spec, [])
        assert isinstance(engine, ClaudeCodeLine)
        sandbox = cast(dict[str, dict[str, list[str]]], engine.sandbox)
        assert str(root / ".claude") in sandbox["filesystem"]["denyWrite"]
        deny = cast(dict[str, dict[str, list[str]]], engine.settings)["permissions"]
        # Claude Code 2.1.310 matches only Edit rules for file writes, and reports a
        # Write rule as one that never matches, so no Write rule is handed over.
        assert f"Edit(/{root / '.claude'}/**)" in deny["deny"]
        assert not any(rule.startswith("Write(") for rule in deny["deny"])


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("﻿---\nname: x\nallowed-tools: Bash\n---\n", "sets allowed-tools"),
        ("---\nname: x\n  allowed-tools: Bash\n---\n", "front matter line 3"),
        ('---\nname: x\n"allowed-tools": Bash\n---\n', "front matter line 3"),
        ("---\nname: x\n'hooks': {}\n---\n", "front matter line 3"),
        ("---\n{allowed-tools: Bash}\n---\n", "front matter line 2"),
        ("---\n\tallowed-tools: Bash\n---\n", "front matter line 2"),
        ("---\ndescription: plain\n  hooks: x\n---\n", "front matter line 3"),
        ("---\ndescription: >\n  text\nhooks: x\n---\n", "sets hooks"),
        ("---\nname x\n---\n", "front matter line 2"),
    ],
)
def test_front_matter_admits_only_allowed_keys_at_column_zero(
    tmp_path: Path, text: str, message: str
) -> None:
    write_skill(tmp_path, "risky", text)
    with pytest.raises(EngineRefused, match=message):
        check_project_settings(tmp_path)


@pytest.mark.parametrize("make", ["file", "empty directory"])
def test_skills_may_hold_only_skill_directories(tmp_path: Path, make: str) -> None:
    skills = tmp_path / ".claude" / "skills"
    skills.mkdir(parents=True)
    if make == "file":
        (skills / "loose.md").write_text("---\nname: loose\n---\n")
        name = "loose.md"
    else:
        (skills / "empty").mkdir()
        name = "empty"
    with pytest.raises(EngineRefused, match=f"holds {name}, which is not a skill"):
        check_project_settings(tmp_path)


REAL_SHAPE = (
    "---\n"
    "name: fake-skill\n"
    "license: MIT\n"
    "description: >\n"
    "  Build things with a fake service: small units you can use like\n"
    "  programming primitives. Use when a feature needs judgment, such as\n"
    "  routing: ranking, extraction, verification; these are starting points.\n"
    "  allowed-tools: Bash is only folded text in this position.\n"
    "---\n"
    "\n"
    "# Fake skill\n"
)


def test_the_real_front_matter_shape_passes(tmp_path: Path) -> None:
    write_skill(tmp_path, "fake-skill", REAL_SHAPE)
    check_project_settings(tmp_path)


@pytest.mark.parametrize("indicator", [">", ">-", ">+", "|", "|-", "|+"])
def test_every_chomping_form_of_a_block_scalar_passes(
    tmp_path: Path, indicator: str
) -> None:
    write_skill(
        tmp_path, "block", f"---\nname: block\ndescription: {indicator}\n  text\n---\n"
    )
    check_project_settings(tmp_path)


@pytest.mark.parametrize("indicator", [">2", "|1-", ">-2", "<", ">>"])
def test_any_other_indicator_leaves_the_indented_line_refused(
    tmp_path: Path, indicator: str
) -> None:
    write_skill(
        tmp_path, "block", f"---\nname: block\ndescription: {indicator}\n  text\n---\n"
    )
    with pytest.raises(EngineRefused, match="front matter line 4"):
        check_project_settings(tmp_path)
