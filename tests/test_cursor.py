"""The Cursor Agent adapter, driven by a scripted ``cursor-agent --print`` stream.

No test here runs Cursor Agent, since a turn spends the operator's plan. The events
follow the shapes Cursor Agent 2026.10.01's installed code writes in stream-json mode.
"""

import asyncio
import json
from collections import deque
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli, cursor, exits
from dromon.activity import ActivityKind, ActivityLog, Observer
from dromon.agents import LineTokens
from dromon.cursor import (
    ISOLATION_REFUSAL,
    LINE_REFUSAL,
    CursorLine,
    check_workspace,
    cli_config,
    cursor_arguments,
    engine_environment,
    line_refusal,
    plugin_files,
    tool_of,
    unsupported,
)
from dromon.driver import (
    ENGINE_OF,
    REPORTED_NAMES,
    ActionTag,
    Credential,
    Driver,
    Event,
    Strategy,
    harness_id,
)
from dromon.engine import EngineRefused, TurnReport
from dromon.grok import bridge_command
from dromon.jsonlines import Connection
from dromon.lineconfig import LineConfig
from dromon.sandbox import parse_sandbox
from dromon.scheduler import LineEngine, LineSpec, engine_factory

CURSOR = "cursor-agent:gpt-5.6-luna-medium"
DISPLAY = "GPT-5.6 Luna 1M"
OWN = "mcp__dromon__send_message"


def driver(strategy: Strategy = Strategy.SUBSCRIPTION) -> Driver:
    return Driver.for_model(CURSOR, strategy)


async def _record(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "ok"}]}


SEND: SdkMcpTool[Any] = SdkMcpTool(
    name="send_message", description="Send.", input_schema={}, handler=_record
)


def init(
    source: str = "login", model: str = DISPLAY, session: str = "chat-1"
) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "init",
        "apiKeySource": source,
        "cwd": "/w",
        "session_id": session,
        "model": model,
        "permissionMode": "default",
    }


def said(text: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        "session_id": "chat-1",
    }


def mcp_call(tool: str = "send_message", provider: str = "dromon") -> dict[str, Any]:
    return {
        "type": "tool_call",
        "subtype": "started",
        "call_id": "c1",
        "tool_call": {
            "mcpToolCall": {
                "args": {"name": tool, "providerIdentifier": provider, "toolName": tool}
            }
        },
        "session_id": "chat-1",
    }


def builtin_call(kind: str) -> dict[str, Any]:
    return {
        "type": "tool_call",
        "subtype": "started",
        "call_id": "c2",
        "tool_call": {kind: {"args": {"path": "/etc/passwd"}}},
        "session_id": "chat-1",
    }


def result(subtype: str = "success", *, error: bool = False) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": error,
        "duration_ms": 10,
        "result": "done",
        "session_id": "chat-1",
    }


def plain(*events: dict[str, Any]) -> list[dict[str, Any]]:
    return [init(), *events, said("done"), result()]


class FakeCursor:
    """A scripted ``cursor-agent --print --output-format stream-json``, which writes
    its events and ends, or ends at once when stopped."""

    def __init__(
        self, events: Sequence[dict[str, Any]] | None = None, *, hang: bool = False
    ) -> None:
        self.queue: deque[dict[str, Any]] = deque(plain() if events is None else events)
        self.hang = hang
        """Whether the stream stays open after its events until it is stopped."""
        self.stopped = asyncio.Event()
        self.arguments: list[str] = []
        self.environment: dict[str, str] = {}
        self.files: dict[str, Any] = {}
        self.data_entries: list[str] = []
        self.stops = 0
        self.started = False

    async def send(self, message: Mapping[str, object]) -> None:
        raise AssertionError("print mode takes no input")

    async def receive(self) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        if self.queue:
            return self.queue.popleft()
        if self.hang:
            await self.stopped.wait()
        return None

    async def stop(self) -> None:
        self.stops += 1
        self.queue.clear()
        self.stopped.set()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def line(
    server: FakeCursor,
    home: Path,
    *,
    config: LineConfig | None = None,
    strategy: Strategy = Strategy.SUBSCRIPTION,
    session_id: str | None = None,
    environment: Mapping[str, str] | None = None,
    observe: Observer | None = None,
    partial: bool = True,
    tools: bool = True,
    turn_seconds: float | None = None,
) -> CursorLine:
    @asynccontextmanager
    async def connect(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        del cwd
        server.started = True
        server.arguments = arguments
        server.environment = dict(env)
        directory = Path(env["CURSOR_CONFIG_DIR"])
        server.files = {
            str(path.relative_to(directory)): json.loads(path.read_text())
            for path in directory.rglob("*.json")
        }
        server.data_entries = sorted(
            p.name for p in Path(env["CURSOR_DATA_DIR"]).iterdir()
        )
        yield server

    workdir = home.parent / "work"
    workdir.mkdir(exist_ok=True)
    extra: dict[str, Any] = {} if observe is None else {"observe": observe}
    if turn_seconds is not None:
        extra["turn_seconds"] = turn_seconds
    return CursorLine(
        driver(strategy),
        workdir,
        config=config or LineConfig(),
        mcp_tools=[SEND],
        session_id=session_id,
        bridge=["dromon-bridge", "--line", "alpha"] if tools else [],
        line_token="tok" if tools else None,  # noqa: S106, a test token
        environment={"PATH": "/usr/bin"} if environment is None else environment,
        home=home,
        connection_factory=connect,
        partial_isolation=partial,
        **extra,
    )


def run(target: CursorLine, prompt: str = "hello") -> TurnReport:
    return asyncio.run(target.run_turn(prompt))


# Names, configuration and environment.


def test_cursor_models_are_engine_qualified_and_reported_by_display_name() -> None:
    assert ENGINE_OF[CURSOR] == cursor.ENGINE_NAME
    assert harness_id(CURSOR) == "gpt-5.6-luna-medium"
    assert set(REPORTED_NAMES) <= set(ENGINE_OF)
    for strategy in Strategy:
        loaded = driver(strategy)
        assert loaded.step(Event("init", DISPLAY)).tag is ActionTag.ACCEPT
        assert loaded.step(Event("init", "gpt-5.6-luna-medium")).tag is (
            ActionTag.REFUSE
        )


def test_every_cursor_turn_is_refused_without_partial_isolation(
    home: Path,
) -> None:
    server = FakeCursor()
    with pytest.raises(EngineRefused, match="--accept-partial-isolation"):
        run(line(server, home, partial=False))
    assert not server.started
    assert "screen recording" in ISOLATION_REFUSAL


def test_a_cursor_line_takes_no_builtin_tools_or_claude_code_features() -> None:
    assert unsupported(LineConfig()) is None
    assert "no built-in tools" in (
        unsupported(LineConfig(builtin_tools=("Read",))) or ""
    )
    assert "MCP servers" in (unsupported(LineConfig(inherit_account_mcp=True)) or "")
    assert "compact" in (unsupported(LineConfig(allow_compact=True)) or "")
    assert "keep" in (unsupported(LineConfig(keep_session=True)) or "")
    sandboxed = LineConfig(sandbox=parse_sandbox({"enabled": True}, operator=True))
    assert "sandbox" in (unsupported(sandboxed) or "")


def test_a_line_effort_cannot_override_the_models(home: Path) -> None:
    with pytest.raises(EngineRefused, match="effort in the model"):
        run(line(FakeCursor(), home, config=LineConfig(effort="high")))
    run(line(FakeCursor(), home, config=LineConfig(effort="medium")))


def test_a_subscription_environment_has_no_cursor_key() -> None:
    environment = {
        "PATH": "/usr/bin",
        "CURSOR_API_KEY": "k",
        "CURSOR_AUTH_TOKEN": "t",
        "OPENAI_API_KEY": "k",
        "CURSOR_CONFIG_DIR": "/elsewhere",
        "CURSOR_DATA_DIR": "/elsewhere",
    }
    assert engine_environment(environment, Strategy.SUBSCRIPTION) == {
        "PATH": "/usr/bin"
    }
    api = engine_environment(environment, Strategy.API)
    assert api["CURSOR_API_KEY"] == "k"
    assert "CURSOR_DATA_DIR" not in api and "CURSOR_CONFIG_DIR" not in api
    for name in ("CURSOR_API_ENDPOINT", "CURSOR_API_URL"):
        with pytest.raises(EngineRefused, match=name):
            engine_environment({name: "https://elsewhere"}, Strategy.SUBSCRIPTION)


def test_the_subscription_environment_has_no_cloud_credential() -> None:
    """Cursor review, F4. Cursor reads the AWS keys, and ``AWS_SECRET_ACCESS_KEY``
    ends in no credential suffix the earlier filter knew."""
    names = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_SHARED_CREDENTIALS_FILE",
        "AZURE_CLIENT_SECRET",
        "AZURE_CLIENT_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "CLOUDSDK_AUTH_ACCESS_TOKEN",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
        "OCI_CLI_KEY_FILE",
        "DATABASE_PASSWORD",
    )
    environment = {"PATH": "/usr/bin", **{name: "x" for name in names}}
    assert engine_environment(environment, Strategy.SUBSCRIPTION) == {
        "PATH": "/usr/bin"
    }
    assert set(names) <= set(engine_environment(environment, Strategy.API))


@pytest.mark.parametrize(
    "entry",
    [
        "cli.json",
        "mcp.json",
        "hooks.json",
        "permissions.json",
        "sandbox.json",
        "settings.json",
        "environment.json",
        "worktrees.json",
        "rules",
        "agents",
        None,
    ],
)
def test_any_cursor_directory_in_the_workspace_chain_is_refused(
    tmp_path: Path, entry: str | None
) -> None:
    """Cursor review, F2. Cursor reads many project files, so any ``.cursor`` in the
    working directory or above it refuses the turn, even an empty one."""
    home = tmp_path / "home"
    workspace = tmp_path / "repo" / "sub"
    workspace.mkdir(parents=True)
    check_workspace(workspace, home)
    (tmp_path / "repo" / ".cursor").mkdir()
    if entry is not None:
        (tmp_path / "repo" / ".cursor" / entry).write_text("{}")
    with pytest.raises(EngineRefused, match=r"repo/\.cursor"):
        check_workspace(workspace, home)


@pytest.mark.parametrize("name", cursor.HOME_ENTRIES)
def test_the_users_own_configuration_refuses_the_line(
    tmp_path: Path, name: str
) -> None:
    home = tmp_path / "home"
    workspace = home / "projects" / "repo"
    workspace.mkdir(parents=True)
    (home / ".cursor").mkdir()
    for own in ("cli-config.json", "chats", "projects", "skills-cursor"):
        (home / ".cursor" / own).mkdir()
    check_workspace(workspace, home)
    (home / ".cursor" / name).mkdir()
    with pytest.raises(EngineRefused, match=name):
        check_workspace(workspace, home)


# The configuration Dromon writes.


def test_the_configuration_denies_cursors_own_tools_and_allows_dromons() -> None:
    document = cli_config(["send_message"])
    assert document["permissions"] == {
        "allow": ["Mcp(dromon:send_message)"],
        "deny": ["Read(*)", "Write(*)", "Shell(*)", "WebFetch(*)"],
    }
    assert document["approvalMode"] == "allowlist"
    files = plugin_files(["python", "-m", "dromon.cli"])
    server = files["mcp.json"]["mcpServers"]["dromon"]  # type: ignore[index]
    assert server == {
        "command": "python",
        "args": ["-m", "dromon.cli"],
        "env": {"DROMON_LINE_TOKEN": "${env:DROMON_LINE_TOKEN}"},
    }
    assert files[".cursor-plugin/plugin.json"] == {"name": "dromon"}


def test_the_arguments_name_the_model_workspace_plugin_and_prompt() -> None:
    arguments = cursor_arguments(
        "gpt-5.6-luna-medium",
        workspace=Path("/w"),
        plugin=Path("/c/plugin"),
        session_id="chat-1",
        prompt="-starts with a dash",
    )
    assert arguments[:3] == ["--print", "--output-format", "stream-json"]
    assert arguments[arguments.index("--model") + 1] == "gpt-5.6-luna-medium"
    assert arguments[arguments.index("--workspace") + 1] == "/w"
    assert arguments[arguments.index("--plugin-dir") + 1] == "/c/plugin"
    assert arguments[arguments.index("--resume") + 1] == "chat-1"
    assert arguments[arguments.index("--sandbox") + 1] == "enabled"
    assert arguments[-2:] == ["--", "-starts with a dash"]
    for forbidden in (
        "--force",
        "--yolo",
        "--approve-mcps",
        "--api-key",
        "--endpoint",
        "-H",
    ):
        assert forbidden not in arguments
    bare = cursor_arguments(
        "m", workspace=Path("/w"), plugin=None, session_id=None, prompt="p"
    )
    assert "--plugin-dir" not in bare and "--approve-mcps" not in bare
    assert "--resume" not in bare


def test_both_shapes_of_a_tool_call_are_read() -> None:
    assert tool_of({"readToolCall": {"args": {"path": "/x"}}})[0] == "readToolCall"
    case = {"tool": {"case": "mcpToolCall", "value": {"args": {"toolName": "t"}}}}
    kind, value = tool_of(case)
    assert kind == "mcpToolCall" and value == {"args": {"toolName": "t"}}
    assert tool_of({})[0] == ""


# Turns.


def test_a_plain_turn_reports_text_credential_and_chat(home: Path) -> None:
    server = FakeCursor()
    target = line(server, home)
    report = run(target, "the wake prompt")
    assert report.subtype == "success"
    assert report.text == "done"
    assert report.credential is Credential.SUBSCRIPTION
    assert report.session_id == "chat-1" == target.session_id
    assert report.usage is None and report.cost_estimate_usd is None
    assert server.arguments[-1] == "the wake prompt"
    assert server.files["cli-config.json"]["permissions"]["allow"] == [
        "Mcp(dromon:send_message)"
    ]
    assert "plugin/mcp.json" in server.files
    assert server.environment["DROMON_LINE_TOKEN"] == "tok"  # noqa: S105, a test token


def test_the_configuration_directory_is_removed_after_the_turn(home: Path) -> None:
    server = FakeCursor()
    run(line(server, home))
    assert not Path(server.environment["CURSOR_CONFIG_DIR"]).exists()


def test_the_data_directory_is_an_empty_one_of_the_turns_own(home: Path) -> None:
    """Cursor review, F7. An operator's ``CURSOR_DATA_DIR`` is replaced."""
    server = FakeCursor()
    run(line(server, home, environment={"CURSOR_DATA_DIR": str(home)}))
    data = Path(server.environment["CURSOR_DATA_DIR"])
    assert data.parent == Path(server.environment["CURSOR_CONFIG_DIR"])
    assert server.data_entries == []
    assert not data.exists()


def test_a_turn_past_its_deadline_is_stopped_and_refused(home: Path) -> None:
    """Cursor review, F6."""
    server = FakeCursor([init(), said("still going")], hang=True)
    with pytest.raises(EngineRefused, match="deadline of 0.05 seconds"):
        run(line(server, home, turn_seconds=0.05))
    assert server.stops == 1


def test_instructions_open_a_new_chat(home: Path) -> None:
    workdir = home.parent / "work"
    workdir.mkdir(exist_ok=True)
    (workdir / "notes.md").write_text("Be brief.")
    config = LineConfig(instructions=("notes.md",))
    server = FakeCursor()
    run(line(server, home, config=config))
    assert server.arguments[-1] == "Contents of notes.md:\n\nBe brief.\n\nhello"
    assert "--resume" not in server.arguments


def test_a_resumed_chat_is_refused_before_cursor_starts(home: Path) -> None:
    """Re-review, N1. The turn's data directory holds no earlier chat, so neither a
    line created on a chat nor the line's second turn may resume one."""
    for session in ("chat-1", "--force"):
        server = FakeCursor()
        with pytest.raises(EngineRefused, match="holds no earlier chat"):
            run(line(server, home, session_id=session))
        assert not server.started
    server = FakeCursor()
    target = line(server, home)
    run(target)
    assert target.session_id == "chat-1"
    server.started = False
    with pytest.raises(EngineRefused, match="holds no earlier chat"):
        run(target)
    assert not server.started


def test_an_api_key_is_refused_by_a_subscription_driver(home: Path) -> None:
    server = FakeCursor([init(source="env"), said("x"), result()])
    with pytest.raises(EngineRefused, match="refuses the api credential"):
        run(line(server, home))
    assert server.stops == 1


def test_an_api_driver_runs_on_a_key(home: Path) -> None:
    server = FakeCursor([init(source="env"), said("x"), result()])
    report = run(line(server, home, strategy=Strategy.API))
    assert report.credential is Credential.API


def test_an_unknown_key_source_or_model_stops_the_turn(home: Path) -> None:
    with pytest.raises(EngineRefused, match="key source"):
        run(line(FakeCursor([init(source="team"), result()]), home))
    server = FakeCursor([init(model="GPT-5.6 Luna 1M High"), said("x"), result()])
    with pytest.raises(EngineRefused, match="refuses the engine model"):
        run(line(server, home))
    assert server.stops == 1


def test_events_before_init_are_refused(home: Path) -> None:
    server = FakeCursor([said("early"), init(), result()])
    with pytest.raises(EngineRefused, match="before its init event"):
        run(line(server, home))


def test_dromons_own_tool_is_counted(home: Path) -> None:
    completed = {**mcp_call(), "subtype": "completed"}
    report = run(line(FakeCursor(plain(mcp_call(), completed)), home))
    assert report.tool_uses == (OWN,)


def test_a_tool_reported_only_when_it_completes_is_refused(home: Path) -> None:
    server = FakeCursor(plain({**builtin_call("semSearchToolCall"), "subtype": "x"}))
    with pytest.raises(EngineRefused, match="semSearchToolCall"):
        run(line(server, home))
    assert server.stops == 1


@pytest.mark.parametrize("kind", ["interaction_query", "connection", None])
def test_a_record_of_an_unknown_type_is_refused(home: Path, kind: str | None) -> None:
    server = FakeCursor(plain({"type": kind}))
    with pytest.raises(EngineRefused, match="does not know"):
        run(line(server, home))
    assert server.stops == 1


@pytest.mark.parametrize(
    "event",
    [
        builtin_call("readToolCall"),
        builtin_call("shellToolCall"),
        builtin_call("taskToolCall"),
        builtin_call("recordScreenToolCall"),
        mcp_call(provider="other"),
        mcp_call(tool="not_given"),
    ],
)
def test_any_other_tool_stops_and_refuses_the_turn(
    home: Path, event: dict[str, Any]
) -> None:
    server = FakeCursor(plain(event))
    with pytest.raises(EngineRefused, match="not given"):
        run(line(server, home))
    assert server.stops == 1


def test_a_line_outside_a_daemon_has_no_tools_at_all(home: Path) -> None:
    server = FakeCursor(plain(mcp_call()))
    target = line(server, home, tools=False)
    assert target.own_tools == frozenset()
    with pytest.raises(EngineRefused, match="not given"):
        run(target)
    assert server.files["cli-config.json"]["permissions"]["allow"] == []
    assert "--plugin-dir" not in server.arguments
    assert "DROMON_LINE_TOKEN" not in server.environment


def test_the_tool_budget_stops_the_turn_once(home: Path) -> None:
    budget = driver(Strategy.API).settings.tool_budget
    events = [init(source="env"), *[mcp_call()] * (budget + 3), result()]
    server = FakeCursor(events)
    report = run(line(server, home, strategy=Strategy.API))
    assert report.interrupted_for == "tool_budget"
    assert report.subtype == "interrupted"
    assert server.stops == 1


def test_a_failed_run_and_a_missing_result_are_reported(home: Path) -> None:
    failed = run(line(FakeCursor([init(), result("error", error=True)]), home))
    assert failed.subtype == "error"
    with pytest.raises(EngineRefused, match="without a result"):
        run(line(FakeCursor([init(), said("x")]), home))
    with pytest.raises(EngineRefused, match="before it reported its credential"):
        run(line(FakeCursor([]), home))


def test_the_turn_streams_to_the_observer(home: Path) -> None:
    log = ActivityLog()
    thought = {"type": "thinking", "subtype": "delta", "text": "hmm"}
    events = [init(), thought, thought, said("hel"), said("lo"), mcp_call(), result()]
    run(line(FakeCursor(events), home, observe=log.observer("alpha")))
    assert [(entry.kind, entry.text) for entry in log.after("alpha", 0)] == [
        (ActivityKind.THINKING, ""),
        (ActivityKind.TEXT, "hello"),
        (ActivityKind.TOOL, OWN),
    ]


def test_the_adapter_refuses_a_driver_for_another_engine(home: Path) -> None:
    claude = Driver.for_model("claude-haiku-5-5", Strategy.SUBSCRIPTION)
    with pytest.raises(EngineRefused, match="written for the claude-code engine"):
        CursorLine(claude, home)


# The factory.


def test_the_daemons_factory_builds_a_refusing_cursor_line(
    tmp_path: Path,
) -> None:
    tokens = LineTokens()
    socket_path = tmp_path / "run" / "lines.sock"
    build = engine_factory(str(tmp_path), line_tokens=tokens, line_socket=socket_path)
    engine: LineEngine = build(LineSpec("alpha", CURSOR, Strategy.SUBSCRIPTION), [SEND])
    assert isinstance(engine, CursorLine)
    assert engine.partial_isolation is False
    assert engine.line_token == tokens.issue("alpha")
    assert list(engine.bridge) == bridge_command(tmp_path, socket_path.parent, "alpha")
    assert engine.own_tools == frozenset({OWN})
    sandboxed = engine_factory(
        str(tmp_path), project_sandbox=parse_sandbox({"enabled": True}, operator=True)
    )
    with pytest.raises(EngineRefused, match="Cursor lines take no sandbox"):
        sandboxed(LineSpec("alpha", CURSOR, Strategy.SUBSCRIPTION), [])


def test_a_daemon_refuses_every_cursor_line() -> None:
    assert line_refusal(LineConfig()) == LINE_REFUSAL
    assert "no built-in tools" in (
        line_refusal(LineConfig(builtin_tools=("Read",))) or ""
    )


def test_only_the_probes_flag_lets_cursor_run(tmp_path: Path) -> None:
    build = engine_factory(str(tmp_path), accept_partial_isolation=True)
    engine = build(LineSpec("probe", CURSOR, Strategy.SUBSCRIPTION), [])
    assert isinstance(engine, CursorLine) and engine.partial_isolation is True
    assert engine.own_tools == frozenset()


def test_the_probe_refuses_cursor_without_the_flag_and_the_flag_elsewhere(
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "probe"]
    assert cli.main([*base, "--model", CURSOR]) == exits.FAILURE
    assert "--accept-partial-isolation" in capsys.readouterr().err
    flagged = [*base, "--model", "claude-haiku-5-5", "--accept-partial-isolation"]
    assert cli.main(flagged) == exits.FAILURE
    assert "applies only to Cursor Agent" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main([*base, "--help"])
    assert "stopped only after they start" in " ".join(capsys.readouterr().out.split())
