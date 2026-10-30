"""Local plugins: a line loads plugin directories that grant nothing and run nothing,
checked against allowlists at every turn, write protected, and set only with operator
authority. Every plugin here is a fake under ``tmp_path``."""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Generator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    SdkMcpTool,
    SystemMessage,
)

from dromon import codex, grok
from dromon.agents import AgentPolicy
from dromon.configview import Overrides
from dromon.daemon import Daemon, check_sandboxes
from dromon.driver import Driver, Strategy
from dromon.engine import (
    PLUGIN_KEYS,
    AgentSession,
    ClaudeCodeLine,
    EngineRefused,
    check_plugin,
)
from dromon.eventlog import EventLog
from dromon.lineconfig import (
    DEFAULT_CONFIG,
    MAX_PLUGINS,
    ConfigError,
    LineConfig,
    config_from_document,
    decode_config,
    encode_config,
)
from dromon.model import AgentAccess
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ProjectConfig, parse
from dromon.protocol import ErrorCode, Operation, Request, Response
from dromon.sandbox import parse_sandbox
from dromon.scheduler import LineEngine, LineSpec, Scheduler, engine_factory

HAIKU = "claude-haiku-5-5"
SANDBOX: dict[str, object] = {"enabled": True}
"""Every line that loads plugins needs an enabled sandbox."""

MANIFEST: dict[str, object] = {
    "name": "fake",
    "version": "1.0.0",
    "description": "A fake plugin with one skill.",
    "author": {"name": "Nobody", "url": "https://example.invalid"},
    "homepage": "https://example.invalid",
    "repository": "https://example.invalid/fake",
    "license": "MIT",
    "keywords": ["test"],
}

SKILL = (
    "---\nname: fake-skill\nlicense: MIT\ndescription: >\n  A skill for tests,\n"
    "  folded across lines.\n---\n\n# Fake\n"
)


def make_plugin(base: Path, name: str = "fake") -> Path:
    """A fake plugin shaped like an installed user plugin."""
    root = base / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps(MANIFEST))
    (root / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "fake-market",
                "description": "Lists the fake plugin.",
                "owner": {"name": "Nobody"},
                "plugins": [{"name": "fake", "description": "x", "source": "./"}],
            }
        )
    )
    skill = root / "skills" / "fake-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL)
    (skill / "LICENSE").write_text("MIT")
    (root / "README.md").write_text("Fake.")
    (root / "LICENSE").write_text("MIT")
    (root / ".in_use").mkdir()
    (root / ".in_use" / "12345").write_text("{}")
    return root


def driver() -> Driver:
    return Driver.for_model(HAIKU, Strategy.SUBSCRIPTION)


# The configuration.


def test_the_key_carries_through_the_log_and_is_omitted_when_empty() -> None:
    config = config_from_document({"plugins": ["/opt/fake", "~/plugins/fake"]})
    assert config.plugins == ("/opt/fake", "~/plugins/fake")
    assert decode_config(encode_config(config)) == config
    assert "plugins" not in json.loads(encode_config(DEFAULT_CONFIG))
    file = parse(
        "version = 1\n[lines.a]\nmodel = 'claude-haiku-5-5'\nplugins = ['/opt/fake']\n"
    )
    assert file.lines["a"].config.plugins == ("/opt/fake",)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (["relative/fake"], "must be an absolute path or start with ~/"),
        (["~other/fake"], "must be an absolute path or start with ~/"),
        (["/a", "/a"], "names a directory twice"),
        ([f"/p{i}" for i in range(MAX_PLUGINS + 1)], "more than 16 directories"),
        ("/opt/fake", "plugins is not an array"),
        ([""], "plugins must be text"),
        (["/a\x00b"], "must be an absolute path"),
    ],
)
def test_a_malformed_plugins_key_is_refused(value: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        config_from_document({"plugins": value})


def test_codex_and_grok_lines_refuse_plugins() -> None:
    config = LineConfig(plugins=("/opt/fake",))
    assert codex.unsupported(config) == "Codex lines do not load Claude Code plugins"
    assert grok.unsupported(config) == "Grok lines do not load Claude Code plugins"


# The allowlists.


def test_a_plugin_shaped_like_an_installed_one_is_accepted(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    check_plugin(plugin)
    (plugin / ".claude-plugin" / "marketplace.json").unlink()
    check_plugin(plugin)
    assert set(MANIFEST) == PLUGIN_KEYS


@pytest.mark.parametrize(
    "key",
    [
        "hooks",
        "mcpServers",
        "commands",
        "agents",
        "outputStyles",
        "lspServers",
        "skills",
        "a-key-claude-code-adds-later",
    ],
)
def test_a_manifest_key_that_names_a_component_refuses_the_turn(
    tmp_path: Path, key: str
) -> None:
    plugin = make_plugin(tmp_path)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({**MANIFEST, key: {}})
    )
    with pytest.raises(EngineRefused, match=f"the plugin {plugin} sets {key}"):
        check_plugin(plugin)


@pytest.mark.parametrize(
    "entry", ["hooks", "commands", "agents", "scripts", ".mcp.json"]
)
def test_a_plugin_entry_outside_the_allowlist_refuses_the_turn(
    tmp_path: Path, entry: str
) -> None:
    plugin = make_plugin(tmp_path)
    (plugin / entry).mkdir()
    with pytest.raises(EngineRefused, match=f"holds {entry}, which a Dromon line"):
        check_plugin(plugin)


EMPTY: dict[str, object] = {}
HOOKED_ENTRY: dict[str, object] = {"name": "fake", "source": "./", "hooks": EMPTY}
MARKETPLACE_CASES: list[tuple[object, str]] = [
    ({"name": "m", "plugins": [], "hooks": EMPTY}, "marketplace sets hooks"),
    ({"name": "m", "plugins": [HOOKED_ENTRY]}, "marketplace sets hooks"),
    ({"name": "m", "plugins": EMPTY}, "something not a list"),
    ({"name": "m", "plugins": ["fake"]}, "not an object"),
    ([], "not a JSON object"),
]


@pytest.mark.parametrize(("listing", "message"), MARKETPLACE_CASES)
def test_a_marketplace_listing_that_declares_anything_refuses_the_turn(
    tmp_path: Path, listing: object, message: str
) -> None:
    plugin = make_plugin(tmp_path)
    (plugin / ".claude-plugin" / "marketplace.json").write_text(json.dumps(listing))
    with pytest.raises(EngineRefused, match=message):
        check_plugin(plugin)


def test_a_malformed_plugin_refuses_the_turn(tmp_path: Path) -> None:
    with pytest.raises(EngineRefused, match="is not a directory"):
        check_plugin(tmp_path / "missing")
    plugin = make_plugin(tmp_path)
    (plugin / ".claude-plugin" / "extra.json").write_text("{}")
    with pytest.raises(EngineRefused, match=r"holds .claude-plugin/extra.json"):
        check_plugin(plugin)
    (plugin / ".claude-plugin" / "extra.json").unlink()
    (plugin / ".claude-plugin" / "plugin.json").write_text("{not json")
    with pytest.raises(EngineRefused, match="is not JSON"):
        check_plugin(plugin)
    (plugin / ".claude-plugin" / "plugin.json").unlink()
    with pytest.raises(EngineRefused, match="has no plugin.json"):
        check_plugin(plugin)
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(EngineRefused, match="has no .claude-plugin directory"):
        check_plugin(bare)


@pytest.mark.parametrize("key", ["allowed-tools", "hooks", "model"])
def test_a_plugin_skill_passes_the_project_skill_allowlist(
    tmp_path: Path, key: str
) -> None:
    plugin = make_plugin(tmp_path)
    (plugin / "skills" / "fake-skill" / "SKILL.md").write_text(
        f"---\nname: fake-skill\n{key}: x\n---\n"
    )
    with pytest.raises(EngineRefused, match=f"sets {key} in its front matter"):
        check_plugin(plugin)


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


def test_plugins_reach_the_engine_resolved_with_home_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve() / "home"
    plugin = make_plugin(home / "plugins")
    monkeypatch.setenv("HOME", str(home))
    config = LineConfig(builtin_tools=("Skill",), plugins=("~/plugins/fake",))
    options = ClaudeCodeLine(
        driver(), tmp_path, config=config, sandbox=SANDBOX
    ).options()
    assert options.plugins == [{"type": "local", "path": str(plugin)}]
    assert options.setting_sources == []
    assert options.tools == ["Skill"]
    assert ClaudeCodeLine(driver(), tmp_path).options().plugins == []


def run_line(
    plugin: Path, tools: tuple[str, ...], offered: list[str], tmp_path: Path
) -> tuple[ClaudeCodeLine, Scripted]:
    session = Scripted([init(offered), result()])
    target = ClaudeCodeLine(
        driver(),
        tmp_path,
        config=LineConfig(builtin_tools=tools, plugins=(str(plugin),)),
        sandbox=SANDBOX,
        environment={},
        session_factory=session.factory,
    )
    return target, session


def test_the_init_check_accepts_the_skill_tool_and_refuses_any_other(
    tmp_path: Path,
) -> None:
    plugin = make_plugin(tmp_path)
    target, _ = run_line(plugin, ("Skill",), ["Skill"], tmp_path)
    assert asyncio.run(target.run_turn("one")).subtype == "success"
    target, session = run_line(plugin, ("Skill",), ["Skill", "Bash"], tmp_path)
    with pytest.raises(EngineRefused, match="tools the line was not given: Bash"):
        asyncio.run(target.run_turn("one"))
    assert session.interrupts == 1
    target, _ = run_line(plugin, ("Read",), ["Read", "Skill"], tmp_path)
    with pytest.raises(EngineRefused, match="tools the line was not given: Skill"):
        asyncio.run(target.run_turn("one"))


def test_a_plugin_is_checked_at_the_start_of_every_turn(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    target, session = run_line(plugin, ("Skill",), ["Skill"], tmp_path)
    assert asyncio.run(target.run_turn("one")).subtype == "success"
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({**MANIFEST, "hooks": {}})
    )
    with pytest.raises(EngineRefused, match="sets hooks"):
        asyncio.run(target.run_turn("two"))
    assert len(session.options) == 1


def test_any_line_needs_an_enabled_sandbox_to_load_plugins(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    for tools in ((), ("Read",), ("Skill",)):
        bare = LineConfig(builtin_tools=tools, plugins=(str(plugin),))
        with pytest.raises(EngineRefused, match="needs an enabled sandbox"):
            ClaudeCodeLine(driver(), tmp_path, config=bare).options()
        with pytest.raises(EngineRefused, match="needs an enabled sandbox"):
            ClaudeCodeLine(
                driver(), tmp_path, config=bare, sandbox={"enabled": False}
            ).options()
    config = LineConfig(builtin_tools=("Bash",), plugins=(str(plugin),))
    with pytest.raises(EngineRefused, match="needs an enabled sandbox"):
        ClaudeCodeLine(driver(), tmp_path, config=config).options()
    sandboxed = ClaudeCodeLine(
        driver(),
        tmp_path,
        config=config,
        sandbox={"enabled": True},
        environment={"SHELL": "/bin/bash"},
    )
    assert sandboxed.options().plugins == [{"type": "local", "path": str(plugin)}]


def test_the_factory_denies_writes_to_each_plugin_whatever_the_tools(
    tmp_path: Path,
) -> None:
    root = tmp_path.resolve()
    plugin = make_plugin(root / "plugins")
    build = engine_factory(
        str(root), project_sandbox=parse_sandbox({"enabled": True}, operator=False)
    )
    for tools in ((), ("Skill",), ("Bash", "Edit", "Write")):
        config = LineConfig(builtin_tools=tools, plugins=(str(plugin),))
        engine = build(LineSpec("alpha", HAIKU, Strategy.SUBSCRIPTION, config), [])
        assert isinstance(engine, ClaudeCodeLine)
        sandbox = cast(dict[str, dict[str, list[str]]], engine.sandbox)
        assert str(plugin) in sandbox["filesystem"]["denyWrite"]
        assert str(plugin) not in sandbox["filesystem"]["denyRead"]
        deny = cast(dict[str, dict[str, list[str]]], engine.settings)["permissions"]
        assert f"Edit(/{plugin}/**)" in deny["deny"]


# Operator authority.


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


@contextmanager
def attached(paths: ProjectPaths) -> Generator[Daemon]:
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        yield Daemon(
            paths,
            log,
            "2026-10-26T00:00:00Z",
            scheduler,
            agents=AgentPolicy(True, AgentAccess.NONE, AgentAccess.OPERATOR),
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=False,
        )


def ask(target: Daemon, op: str, args: dict[str, object]) -> Response:
    line = {"id": "r", "op": op, "v": 1, "args": args}
    return target.respond((json.dumps(line) + "\n").encode())


def test_configure_line_checks_plugins_and_the_settings_view_shows_them(
    paths: ProjectPaths,
) -> None:
    plugin = make_plugin(paths.root / "plugins")
    with attached(paths) as target:
        before = target.log.last_seq
        missing = ask(
            target,
            "configure_line",
            {"line_id": "alpha", "config": {"plugins": [str(paths.root / "none")]}},
        )
        assert (missing.ok, missing.error_code) == (False, ErrorCode.REJECTED)
        assert "is not a directory" in (missing.error_message or "")
        (plugin / "hooks").mkdir()
        risky = ask(
            target,
            "configure_line",
            {"line_id": "alpha", "config": {"plugins": [str(plugin)]}},
        )
        assert not risky.ok and "holds hooks" in (risky.error_message or "")
        assert target.log.last_seq == before
        (plugin / "hooks").rmdir()
        configured = ask(
            target,
            "configure_line",
            {"line_id": "alpha", "config": {"plugins": [str(plugin)]}},
        )
        assert configured.ok, configured.error_message
        settings = ask(target, "settings", {"line_id": "alpha"})
        assert settings.result is not None
        entries = cast(list[dict[str, object]], settings.result["line"])
        entry = next(e for e in entries if e["key"] == "plugins")
        assert (entry["kind"], entry["value"]) == ("info", str(plugin))
        refused = ask(
            target,
            "set_setting",
            {"scope": "line", "line_id": "alpha", "key": "plugins", "value": []},
        )
        assert not refused.ok


def test_the_daemon_refuses_a_project_file_with_a_plugin_that_would_not_load(
    paths: ProjectPaths,
) -> None:
    plugin = make_plugin(paths.root / "plugins")
    text = (
        f"version = 1\n[lines.a]\nmodel = 'claude-haiku-5-5'\nplugins = ['{plugin}']\n"
    )
    check_sandboxes(parse(text), paths)
    (plugin / "commands").mkdir()
    with pytest.raises(ConfigError, match="holds commands"):
        check_sandboxes(parse(text), paths)


def agent_configure(
    target: Daemon, agent: str, token: str, config: dict[str, object]
) -> tuple[str, bool]:
    args: dict[str, object] = {
        "agent": agent,
        "tool": "configure_line",
        "token": token,
        "arguments": {"line_id": "alpha", "config": config},
    }
    line = json.dumps({"id": "r", "op": "agent_call", "v": 1, "args": args}) + "\n"
    response = asyncio.run(target.respond_agent(line.encode()))
    assert response.ok and response.result is not None, response.error_message
    content = cast(list[dict[str, str]], response.result["content"])
    return content[0]["text"], bool(response.result["is_error"])


def register(target: Daemon, agent: str, access: AgentAccess) -> str:
    response = target.execute(
        Request("a", Operation.AGENT_ADD, {"agent": agent, "access": access.value})
    )
    assert response.ok and response.result is not None
    return str(response.result["token"])


def test_only_operator_authority_sets_plugins(paths: ProjectPaths) -> None:
    plugin = make_plugin(paths.root / "plugins")
    with attached(paths) as target:
        manager = register(target, "manager", AgentAccess.MANAGE)
        text, error = agent_configure(
            target, "manager", manager, {"plugins": [str(plugin)]}
        )
        assert error and "only with operator access" in text
        text, error = agent_configure(
            target, "manager", manager, {"builtin_tools": ["Read"]}
        )
        assert not error, text
        operator = register(target, "chief", AgentAccess.OPERATOR)
        text, error = agent_configure(
            target, "chief", operator, {"plugins": [str(plugin)]}
        )
        assert not error, text
        line = target.log.state.lines["alpha"]
        assert decode_config(cast(str, line.config)).plugins == (str(plugin),)


SOURCE_CASES: list[tuple[object, str]] = [
    ("/opt/elsewhere", "is not relative"),
    ("~/elsewhere", "is not relative"),
    ("https://example.invalid/plugin.git", "is not relative"),
    ("../outside", "leaves the plugin"),
    ("skills/../../outside", "leaves the plugin"),
    ({"source": "github", "repo": "owner/repo"}, "is not a path"),
    ("", "is not a path"),
    (None, "is not a path"),
]


@pytest.mark.parametrize(("source", "message"), SOURCE_CASES)
def test_a_listing_source_must_stay_in_the_plugin(
    tmp_path: Path, source: object, message: str
) -> None:
    plugin = make_plugin(tmp_path)
    entry: dict[str, object] = {"name": "fake"}
    if source is not None:
        entry["source"] = source
    (plugin / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "m", "plugins": [entry]})
    )
    with pytest.raises(EngineRefused, match=message):
        check_plugin(plugin)


def test_relative_sources_inside_the_plugin_are_accepted(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    for source in ("./", ".", "./skills", "skills/fake-skill"):
        (plugin / ".claude-plugin" / "marketplace.json").write_text(
            json.dumps({"name": "m", "plugins": [{"name": "fake", "source": source}]})
        )
        check_plugin(plugin)


def test_a_plugins_skills_may_hold_only_skill_directories(tmp_path: Path) -> None:
    plugin = make_plugin(tmp_path)
    (plugin / "skills" / "loose.md").write_text("---\nname: loose\n---\n")
    with pytest.raises(EngineRefused, match="holds loose.md, which is not a skill"):
        check_plugin(plugin)
