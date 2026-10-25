"""The Grok Build adapter, driven by a scripted Agent Client Protocol server.

No test here reaches Grok, whose usage was exhausted when the adapter was written. The
script follows Grok Build 1.0.70's documentation and its ``initialize`` answer.
"""

import asyncio
import json
from collections import deque
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.activity import ActivityKind, ActivityLog, Observer, ignore
from dromon.agents import AgentError, LineTokens
from dromon.daemon import Daemon
from dromon.driver import Credential, Driver, Strategy
from dromon.engine import EngineRefused, TurnReport
from dromon.eventlog import EventLog
from dromon.grok import (
    READER_PROFILE,
    WRITER_PROFILE,
    Connection,
    GrokLine,
    bridge_command,
    check_profile,
    engine_environment,
    grok_arguments,
    unsupported,
)
from dromon.lineconfig import LineConfig
from dromon.mcp import Bridge
from dromon.protocol import Response
from dromon.model import LineAddress, LineCreated, OperatorRegistered
from dromon.paths import project_paths
from dromon.sandbox import parse_sandbox
from dromon.scheduler import LineEngine, LineSpec, Scheduler, engine_factory

GROK = "grok-4.7"


def grok(strategy: Strategy = Strategy.SUBSCRIPTION) -> Driver:
    return Driver.for_model(GROK, strategy)


async def _record(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "ok"}]}


SEND: SdkMcpTool[Any] = SdkMcpTool(
    name="send_message", description="Send.", input_schema={}, handler=_record
)


def write_profiles(home: Path, workspace: Path, *, writer_git: bool = True) -> None:
    (home / ".grok").mkdir(parents=True, exist_ok=True)
    git = json.dumps(str((workspace / ".git").resolve()))
    read_only = f"read_only = [{git}]\n" if writer_git else ""
    (home / ".grok" / "sandbox.toml").write_text(
        f'[profiles.{READER_PROFILE}]\nextends = "read-only"\ndeny = [".dromon"]\n'
        f'[profiles.{WRITER_PROFILE}]\nextends = "workspace"\n{read_only}'
        'deny = [".dromon"]\n'
    )


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = (tmp_path / "work").resolve()
    (workspace / ".dromon").mkdir(parents=True)
    (workspace / ".git").mkdir()
    home = tmp_path / "home"
    write_profiles(home, workspace)
    return workspace, home, workspace / ".dromon"


def note(update: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "s1", "update": update},
    }


def chunk(text: str) -> dict[str, Any]:
    return note(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        }
    )


def tool_call(kind: str, name: str = "") -> dict[str, Any]:
    return note(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "t",
            "kind": kind,
            "toolName": name,
        }
    )


def permission(kind: str, name: str = "", request_id: str = "p1") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "s1",
            "toolCall": {"toolCallId": "t", "kind": kind, "toolName": name},
            "options": [
                {"optionId": "yes", "kind": "allow_once", "name": "Allow"},
                {"optionId": "always", "kind": "allow_always", "name": "Always"},
                {"optionId": "no", "kind": "reject_once", "name": "Reject"},
            ],
        },
    }


class FakeGrok:
    """A scripted ``grok agent stdio``."""

    def __init__(
        self,
        turn: Sequence[dict[str, Any]] = (chunk("done"),),
        *,
        auth: object = "cached_token",
        model: object = GROK,
        modes: object = None,
        stop: str = "end_turn",
    ) -> None:
        self.turn = list(turn)
        self.auth = auth
        self.model = model
        self.modes: object = (
            {"currentModeId": "default", "availableModes": [{"id": "default"}]}
            if modes is None
            else modes
        )
        self.stop = stop
        self.sent: list[dict[str, Any]] = []
        self.queue: deque[dict[str, Any]] = deque()
        self.arguments: list[str] = []
        self.environment: dict[str, str] = {}
        self.prompt_id: object = None
        self.answer_prompt = True

    def methods(self) -> list[str]:
        return [m["method"] for m in self.sent if "method" in m]

    def params(self, method: str) -> dict[str, Any]:
        return next(m["params"] for m in self.sent if m.get("method") == method)

    def answers(self) -> list[dict[str, Any]]:
        return [m for m in self.sent if "method" not in m]

    async def send(self, message: Mapping[str, object]) -> None:
        sent = dict(message)
        self.sent.append(sent)
        method = sent.get("method")
        if method == "session/cancel":
            # An ACP agent ends a cancelled prompt with stopReason cancelled.
            self.queue = deque(m for m in self.queue if m.get("id") != self.prompt_id)
            self.queue.append(
                {
                    "jsonrpc": "2.0",
                    "id": self.prompt_id,
                    "result": {"stopReason": "cancelled"},
                }
            )
            return
        if not isinstance(method, str) or "id" not in sent:
            return
        reply = {"jsonrpc": "2.0", "id": sent["id"]}
        if method == "initialize":
            self.queue.append(
                {
                    **reply,
                    "result": {
                        "protocolVersion": 1,
                        "_meta": {"defaultAuthMethodId": self.auth},
                    },
                }
            )
        elif method in ("session/new", "session/load"):
            result: dict[str, object] = {"modes": self.modes}
            if method == "session/new":
                result["sessionId"] = "s1"
            if self.model is not None:
                result["models"] = {"currentModelId": self.model}
            self.queue.append({**reply, "result": result})
        elif method == "session/set_mode":
            self.queue.append({**reply, "result": {}})
        elif method == "session/prompt":
            self.prompt_id = sent["id"]
            self.queue.extend(self.turn)
            if self.answer_prompt:
                self.queue.append({**reply, "result": {"stopReason": self.stop}})

    async def receive(self) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        return self.queue.popleft() if self.queue else None


def line(
    server: FakeGrok,
    project: tuple[Path, Path, Path],
    *,
    config: LineConfig | None = None,
    driver: Driver | None = None,
    session_id: str | None = None,
    environment: Mapping[str, str] | None = None,
    observe: Observer = ignore,
) -> GrokLine:
    workspace, home, state = project

    @asynccontextmanager
    async def connect(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        del cwd
        server.arguments = arguments
        server.environment = dict(env)
        yield server

    return GrokLine(
        driver or grok(),
        workspace,
        config=config or LineConfig(),
        mcp_tools=[SEND],
        session_id=session_id,
        protected=(state,),
        bridge=["dromon-bridge", "--line", "alpha"],
        line_token="tok",  # noqa: S106, a test token
        environment={} if environment is None else environment,
        home=home,
        connection_factory=connect,
        observe=observe,
    )


def run(target: GrokLine, prompt: str = "hello") -> TurnReport:
    return asyncio.run(target.run_turn(prompt))


# Configuration and environment.


def test_a_grok_line_takes_its_supported_tools_and_nothing_else() -> None:
    assert unsupported(LineConfig(builtin_tools=("Bash", "Read", "Edit"))) is None
    assert "not Agent" in (unsupported(LineConfig(builtin_tools=("Agent",))) or "")
    assert "MCP servers" in (unsupported(LineConfig(inherit_account_mcp=True)) or "")
    assert "Claude Code's" in (unsupported(LineConfig(claude_code_prompt=True)) or "")
    sandboxed = LineConfig(sandbox=parse_sandbox({"enabled": True}, operator=True))
    assert "Grok sandbox profile" in (unsupported(sandboxed) or "")


def test_the_environment_drops_credentials_and_refuses_redirection() -> None:
    environment = {
        "PATH": "/bin",
        "XAI_API_KEY": "k",
        "GROK_AUTH": "a",
        "GROK_CONFIG": "{}",
        "GROK_SANDBOX": "off",
    }
    assert engine_environment(environment, Strategy.SUBSCRIPTION) == {"PATH": "/bin"}
    assert engine_environment(environment, Strategy.FLEXIBLE) == {"PATH": "/bin"}
    with pytest.raises(EngineRefused, match="api strategy"):
        engine_environment(environment, Strategy.API)
    for name in ("GROK_HOME", "XAI_API_BASE_URL"):
        with pytest.raises(EngineRefused, match=name):
            engine_environment({name: "x"}, Strategy.SUBSCRIPTION)


def test_the_arguments_name_the_model_and_map_effort() -> None:
    assert grok_arguments(GROK, "high") == [
        "agent",
        "--no-leader",
        "--model",
        GROK,
        "--reasoning-effort",
        "high",
        "stdio",
    ]
    assert grok_arguments(GROK, "max")[5] == "xhigh"


# The sandbox profile.


def test_a_reader_and_a_writer_get_their_own_profiles(
    project: tuple[Path, Path, Path],
) -> None:
    workspace, home, state = project
    reader = check_profile(
        LineConfig(builtin_tools=("Bash",)), workspace, (state,), home
    )
    writer = check_profile(
        LineConfig(builtin_tools=("Edit",)), workspace, (state,), home
    )
    assert (reader, writer) == (READER_PROFILE, WRITER_PROFILE)


def test_a_missing_profile_is_refused_with_the_table_to_add(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    (workspace / ".dromon").mkdir(parents=True)
    with pytest.raises(EngineRefused) as raised:
        check_profile(
            LineConfig(), workspace, (workspace / ".dromon",), tmp_path / "home"
        )
    message = str(raised.value)
    assert f"no Grok sandbox profile {READER_PROFILE}" in message
    assert f"[profiles.{READER_PROFILE}]" in message
    assert 'extends = "read-only"' in message and '".dromon"' in message


@pytest.mark.parametrize(
    ("table", "reason"),
    [
        ('extends = "workspace"\ndeny = [".dromon"]', "extends 'workspace'"),
        ('extends = "devbox"\ndeny = [".dromon"]', "extends 'devbox'"),
        ('extends = "read-only"\ndeny = ["**/.dromon"]', "does not deny"),
        ('extends = "read-only"\ndeny = ["other"]', "does not deny"),
        ('extends = "read-only"', "does not deny"),
    ],
)
def test_an_unsafe_reader_profile_is_refused(
    tmp_path: Path, table: str, reason: str
) -> None:
    workspace = tmp_path / "work"
    (workspace / ".dromon").mkdir(parents=True)
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    (home / ".grok" / "sandbox.toml").write_text(
        f"[profiles.{READER_PROFILE}]\n{table}\n"
    )
    with pytest.raises(EngineRefused, match=reason):
        check_profile(LineConfig(), workspace, (workspace / ".dromon",), home)


def test_a_writer_must_keep_git_read_only(tmp_path: Path) -> None:
    workspace = (tmp_path / "work").resolve()
    (workspace / ".dromon").mkdir(parents=True)
    (workspace / ".git").mkdir()
    home = tmp_path / "home"
    write_profiles(home, workspace, writer_git=False)
    with pytest.raises(EngineRefused, match="leaves .*\\.git writable"):
        check_profile(
            LineConfig(builtin_tools=("Edit",)),
            workspace,
            (workspace / ".dromon",),
            home,
        )


def test_deny_entries_resolve_as_grok_documents(tmp_path: Path) -> None:
    workspace = (tmp_path / "work").resolve()
    (workspace / ".dromon").mkdir(parents=True)
    home = (tmp_path / "home").resolve()
    runtime = home / "run"
    runtime.mkdir(parents=True)
    (home / ".grok").mkdir()
    (home / ".grok" / "sandbox.toml").write_text(
        f'[profiles.{READER_PROFILE}]\nextends = "read-only"\n'
        f'deny = [".dromon", "~/run"]\n'
    )
    protected = (workspace / ".dromon", runtime)
    assert check_profile(LineConfig(), workspace, protected, home) == READER_PROFILE


def test_the_users_profile_wins_over_the_projects(tmp_path: Path) -> None:
    workspace = (tmp_path / "work").resolve()
    (workspace / ".dromon").mkdir(parents=True)
    (workspace / ".grok").mkdir()
    (workspace / ".grok" / "sandbox.toml").write_text(
        f'[profiles.{READER_PROFILE}]\nextends = "read-only"\ndeny = [".dromon"]\n'
    )
    home = tmp_path / "home"
    protected = (workspace / ".dromon",)
    assert check_profile(LineConfig(), workspace, protected, home) == READER_PROFILE
    (home / ".grok").mkdir(parents=True)
    (home / ".grok" / "sandbox.toml").write_text(
        f'[profiles.{READER_PROFILE}]\nextends = "read-only"\n'
    )
    with pytest.raises(EngineRefused, match="does not deny"):
        check_profile(LineConfig(), workspace, protected, home)


# A turn.


def test_a_plain_turn_reports_text_credential_and_session(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok([chunk("hello "), chunk("there")])
    target = line(server, project)
    report = run(target)
    assert (report.subtype, report.text, report.session_id) == (
        "success",
        "hello there",
        "s1",
    )
    assert report.credential is Credential.SUBSCRIPTION
    assert target.session_id == "s1"
    assert server.environment["GROK_SANDBOX"] == READER_PROFILE
    init = server.params("initialize")
    assert init["clientCapabilities"] == {
        "fs": {"readTextFile": False, "writeTextFile": False},
        "terminal": False,
    }


def test_the_session_carries_dromons_tools_instructions_and_ask_mode(
    project: tuple[Path, Path, Path],
) -> None:
    workspace, _, _ = project
    (workspace / "RULES.md").write_text("Be careful.\n")
    server = FakeGrok()
    run(line(server, project, config=LineConfig(instructions=("RULES.md",))))
    session = server.params("session/new")
    assert session["cwd"] == str(workspace)
    assert session["mcpServers"] == [
        {
            "name": "dromon",
            "command": "dromon-bridge",
            "args": ["--line", "alpha"],
            "env": [{"name": "DROMON_LINE_TOKEN", "value": "tok"}],
        }
    ]
    assert session["_meta"]["yoloMode"] is False
    assert session["_meta"]["autoMode"] is False
    assert session["_meta"]["rules"] == "Contents of RULES.md:\n\nBe careful."


def test_the_next_turn_loads_the_session(project: tuple[Path, Path, Path]) -> None:
    server = FakeGrok()
    run(line(server, project, session_id="s1"))
    assert server.params("session/load")["sessionId"] == "s1"
    assert "session/new" not in server.methods()


@pytest.mark.parametrize(
    ("server", "reason"),
    [
        (FakeGrok(auth="xai_api_key"), "authentication method 'xai_api_key'"),
        (FakeGrok(auth=None), "authentication method None"),
        (FakeGrok(model="grok-4.5"), "refuses the engine model 'grok-4.5'"),
        (FakeGrok(model=None), "refuses the engine model None"),
        (FakeGrok(modes={}), "permission mode None"),
        (
            FakeGrok(
                modes={"currentModeId": "bypassPermissions", "availableModes": []}
            ),
            "permission mode 'bypassPermissions'",
        ),
    ],
)
def test_an_engine_that_cannot_be_confirmed_is_refused_before_the_prompt(
    project: tuple[Path, Path, Path], server: FakeGrok, reason: str
) -> None:
    with pytest.raises(EngineRefused, match=reason):
        run(line(server, project))
    assert "session/prompt" not in server.methods()


def test_a_session_without_reported_modes_is_refused(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok()
    server.modes = "none"
    with pytest.raises(EngineRefused, match="does not report its permission mode"):
        run(line(server, project))


def test_an_always_approve_session_is_switched_to_the_ask_mode(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(
        modes={
            "currentModeId": "bypassPermissions",
            "availableModes": [{"id": "default"}, {"id": "bypassPermissions"}],
        }
    )
    run(line(server, project))
    assert server.params("session/set_mode") == {"sessionId": "s1", "modeId": "default"}


def test_permission_requests_follow_the_lines_tools(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(
        [
            permission("execute", request_id="p1"),
            permission("read", request_id="p2"),
            permission("other", "dromon__send_message", request_id="p3"),
            permission("other", "gmail__send", request_id="p4"),
            {"jsonrpc": "2.0", "id": "x", "method": "fs/read_text_file", "params": {}},
            chunk("done"),
        ]
    )
    run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    answers = {a["id"]: a for a in server.answers()}
    picked = {
        key: answers[key]["result"]["outcome"]["optionId"]
        for key in ("p1", "p2", "p3", "p4")
    }
    assert picked == {"p1": "no", "p2": "yes", "p3": "yes", "p4": "no"}
    assert answers["x"]["error"]["code"] == -32601


def test_the_turn_is_reported_to_the_observer_as_it_streams(
    project: tuple[Path, Path, Path],
) -> None:
    thought = note(
        {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "hmm"},
        }
    )
    server = FakeGrok(
        [
            thought,
            thought,
            chunk("hello "),
            chunk("there"),
            tool_call("other", "dromon__send_message"),
        ]
    )
    log = ActivityLog()
    run(line(server, project, observe=log.observer("alpha")))
    # Streamed chunks extend one entry, and reasoning is shown only as a mark.
    assert [(entry.kind, entry.text) for entry in log.after("alpha", 0)] == [
        (ActivityKind.THINKING, ""),
        (ActivityKind.TEXT, "hello there"),
        (ActivityKind.TOOL, "dromon__send_message"),
    ]


def test_tool_calls_are_counted_and_others_cancel_the_turn(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok([tool_call("read"), tool_call("other", "dromon__send_message")])
    report = run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert report.tool_uses == ("Read", "dromon__send_message")
    blocked = FakeGrok([tool_call("execute")])
    with pytest.raises(EngineRefused, match="not given: execute"):
        run(line(blocked, project, config=LineConfig(builtin_tools=("Read",))))
    assert blocked.params("session/cancel") == {"sessionId": "s1"}
    foreign = FakeGrok([tool_call("other", "gmail__send")])
    with pytest.raises(EngineRefused, match="not given: gmail__send"):
        run(line(foreign, project))


def test_the_driver_budget_cancels_a_turn_once(
    project: tuple[Path, Path, Path],
) -> None:
    budget = grok().settings.tool_budget
    server = FakeGrok([tool_call("read")] * (budget + 3))
    report = run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert report.interrupted_for == "tool_budget"
    assert report.subtype == "cancelled"
    assert server.methods().count("session/cancel") == 1


def test_a_closed_connection_refuses_the_turn(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok([chunk("partial")])
    server.answer_prompt = False
    with pytest.raises(EngineRefused, match="closed its connection"):
        run(line(server, project))


def test_the_grok_adapter_refuses_a_driver_for_another_engine(
    project: tuple[Path, Path, Path],
) -> None:
    claude = Driver.for_model("claude-haiku-5-5", Strategy.SUBSCRIPTION)
    with pytest.raises(EngineRefused, match="written for the claude-code engine"):
        line(FakeGrok(), project, driver=claude)


# The factory, the line socket and the bridge.


def test_the_factory_builds_a_grok_line_with_its_bridge_and_token(
    tmp_path: Path,
) -> None:
    tokens = LineTokens()
    socket_path = tmp_path / "run" / "lines.sock"
    build = engine_factory(str(tmp_path), line_tokens=tokens, line_socket=socket_path)
    engine: LineEngine = build(LineSpec("alpha", GROK, Strategy.SUBSCRIPTION), [SEND])
    assert isinstance(engine, GrokLine)
    assert engine.line_token == tokens.issue("alpha")
    assert list(engine.bridge) == bridge_command(tmp_path, socket_path.parent, "alpha")
    assert engine.own_tools == frozenset({"dromon__send_message"})


def test_line_tokens_are_per_line_and_checked(tmp_path: Path) -> None:
    tokens = LineTokens()
    alpha = tokens.issue("alpha")
    assert tokens.issue("alpha") == alpha != tokens.issue("beta")
    assert tokens.check("alpha", alpha) == "alpha"
    for line_id, token in (("alpha", "wrong"), ("beta", alpha), ("gamma", alpha)):
        with pytest.raises(AgentError, match="needs its token"):
            tokens.check(line_id, token)
    with pytest.raises(AgentError):
        tokens.issue("Bad Name")


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError("no engine may start in this test")


def test_the_line_socket_serves_only_the_lines_own_tools(
    tmp_path: Path, short_runtime: Path
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700)
    with EventLog(paths.event_log) as log:
        log.append(LineCreated("alpha", GROK, "subscription"))
        log.append(OperatorRegistered("ana"))
        daemon = Daemon(paths, log, "t", Scheduler(log, no_engine, hold_wakes=True))
        token = daemon.line_tokens.issue("alpha")

        def ask(op: str, args: dict[str, object]) -> Response:
            line = (
                json.dumps({"id": "r", "op": op, "v": 1, "args": args}) + "\n"
            ).encode()
            return asyncio.run(daemon.respond_line(line))

        listed = ask("agent_tools", {"line": "alpha", "token": token})
        assert listed.result is not None
        names = {t["name"] for t in cast(list[dict[str, str]], listed.result["tools"])}
        assert names == {
            "send_message",
            "ask_operator",
            "list_mail",
            "read_mail",
            "acknowledge_mail",
            "list_sent",
        }
        sent = ask(
            "agent_call",
            {
                "line": "alpha",
                "token": token,
                "tool": "send_message",
                "arguments": {"to": "@ana", "body": "hi"},
            },
        )
        assert sent.ok and sent.result is not None and not sent.result["is_error"]
        message = list(log.state.messages.values())[-1]
        assert message.sender == LineAddress("alpha")
        assert not ask("agent_tools", {"line": "alpha", "token": "bad"}).ok
        assert not ask("agent_tools", {"agent": "alpha", "token": token}).ok
        refused = ask("status", {}).error_message
        assert refused is not None and refused.startswith("the line socket serves only")
        orphan = daemon.line_tokens.issue("ghost")
        assert ask("agent_tools", {"line": "ghost", "token": orphan}).error_message == (
            "no line ghost"
        )


def test_the_bridge_names_a_line_in_line_mode(short_runtime: Path) -> None:
    bridge = Bridge(short_runtime / "lines.sock", "alpha", "tok", party="line")
    answer = bridge.handle({"id": 1, "method": "initialize", "params": {}})
    assert answer is not None
    instructions = cast(dict[str, Any], answer["result"])["instructions"]
    assert "session line alpha" in instructions
    listed = bridge.handle({"id": 2, "method": "tools/list"})
    assert listed is not None and "not running" in str(listed["error"])
