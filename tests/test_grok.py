"""The Grok Build adapter, driven by a scripted Agent Client Protocol server.

No test here reaches Grok, whose usage was exhausted when the adapter was written. The
script follows Grok Build 1.0.70's documentation and its ``initialize`` answer.
"""

import asyncio
import json
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import grok as grok_module
from dromon.activity import ActivityKind, ActivityLog, Observer, ignore
from dromon.agents import AgentError, LineTokens
from dromon.daemon import Daemon
from dromon.driver import Credential, Driver, Strategy
from dromon.priority import SteeredDriver
from dromon.engine import EngineRefused, TurnReport
from dromon.eventlog import EventLog
from dromon.evidence import IN, OUT, Recording, Replay, read_recording, replay_factory
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


def tool_call(kind: str, name: str = "", ident: str = "t") -> dict[str, Any]:
    return note(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": ident,
            "kind": kind,
            "toolName": name,
        }
    )


def call_update(ident: object, **fields: object) -> dict[str, Any]:
    """A ``tool_call_update`` for ``ident`` carrying only ``fields``."""
    return note({"sessionUpdate": "tool_call_update", "toolCallId": ident, **fields})


def permission(
    kind: str, name: str = "", request_id: str = "p1", ident: str = "t"
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "s1",
            "toolCall": {"toolCallId": ident, "kind": kind, "toolName": name},
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
        self.ignores_cancel = False
        self.blocked_method: str | None = None
        self.activity_delay = 0.0
        """Whether this Grok neither answers a cancel nor closes, so waits block."""
        self.stops = 0

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
            if self.ignores_cancel:
                return
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
        if method == self.blocked_method:
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
        if not self.queue and (self.ignores_cancel or self.blocked_method is not None):
            await asyncio.Event().wait()
        message = self.queue.popleft() if self.queue else None
        if message is not None and message.get("method") == "session/update":
            await asyncio.sleep(self.activity_delay)
        return message


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
        try:
            yield server
        finally:
            server.stops += 1

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


class Tap:
    """The scripted agent, recorded as Grok's own process records."""

    def __init__(self, server: FakeGrok, recording: Recording) -> None:
        self.server = server
        self.recording = recording

    async def send(self, message: Mapping[str, object]) -> None:
        await self.server.send(message)
        self.recording.frame(OUT, message)

    async def receive(self) -> dict[str, Any] | None:
        found = await self.server.receive()
        if found is not None:
            self.recording.frame(IN, found)
        return found


def test_a_recorded_turn_replays_through_the_real_adapter(
    project: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """A Grok probe records this exchange, and the recording then stands in for
    Grok, so the adapter gives the same report without the engine."""
    workspace, home, state = project
    path = tmp_path / "probe.jsonl"
    recording = Recording(path, grok_module.ENGINE_NAME, GROK)
    server = FakeGrok(
        [chunk("hel"), tool_call("other", "dromon__send_message"), chunk("lo")]
    )

    @asynccontextmanager
    async def tapped(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        del arguments, cwd, env
        yield Tap(server, recording)

    def target(factory: Any) -> GrokLine:  # noqa: ANN401
        return GrokLine(
            grok(),
            workspace,
            config=LineConfig(),
            mcp_tools=[SEND],
            protected=(state,),
            environment={},
            home=home,
            connection_factory=factory,
        )

    live = run(target(tapped))
    recording.close()
    assert (live.text, live.tool_uses) == ("hello", ("dromon__send_message",))
    replay = Replay(read_recording(path), wait_seconds=1.0)
    replayed = run(target(replay_factory(replay)))
    assert replay.finished()
    assert replay.sent == replay.recorded_out
    assert (replayed.subtype, replayed.text, replayed.tool_uses) == (
        live.subtype,
        live.text,
        live.tool_uses,
    )
    assert (replayed.credential, replayed.session_id) == (
        live.credential,
        live.session_id,
    )


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
    reader = LineConfig(builtin_tools=("Read",))
    server = FakeGrok(
        [
            permission("read", request_id="p1", ident="a"),
            permission("other", "dromon__send_message", request_id="p2", ident="b"),
            {"jsonrpc": "2.0", "id": "x", "method": "fs/read_text_file", "params": {}},
            chunk("done"),
        ]
    )
    report = run(line(server, project, config=reader))
    answers = {a["id"]: a for a in server.answers()}
    picked = {
        key: answers[key]["result"]["outcome"]["optionId"] for key in ("p1", "p2")
    }
    assert picked == {"p1": "yes", "p2": "yes"}
    assert answers["x"]["error"]["code"] == -32601
    assert report.tool_uses == ("Read", "dromon__send_message")
    # A refused request cancels the turn, and ACP requires every request after a
    # cancel to be answered as cancelled, which grants nothing.
    refused = FakeGrok(
        [
            permission("execute", request_id="p1", ident="a"),
            permission("read", request_id="p2", ident="b"),
            permission("other", "gmail__send", request_id="p3", ident="c"),
        ]
    )
    with pytest.raises(EngineRefused, match="not given: execute"):
        run(line(refused, project, config=reader))
    answers = {a["id"]: a["result"]["outcome"] for a in refused.answers()}
    assert answers["p1"]["optionId"] == "no"
    assert answers["p2"] == answers["p3"] == {"outcome": "cancelled"}


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
    server = FakeGrok(
        [
            tool_call("read", ident="a"),
            tool_call("other", "dromon__send_message", ident="b"),
        ]
    )
    report = run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert report.tool_uses == ("Read", "dromon__send_message")
    blocked = FakeGrok([tool_call("execute")])
    with pytest.raises(EngineRefused, match="not given: execute"):
        run(line(blocked, project, config=LineConfig(builtin_tools=("Read",))))
    assert blocked.params("session/cancel") == {"sessionId": "s1"}
    foreign = FakeGrok([tool_call("other", "gmail__send")])
    with pytest.raises(EngineRefused, match="not given: gmail__send"):
        run(line(foreign, project))


def test_a_title_never_names_one_of_dromons_tools(
    project: tuple[Path, Path, Path],
) -> None:
    """Antigravity review, F4, applied to Grok through the shared client. A call that
    names Dromon's tool only in its title is judged by its kind, and refused."""
    titled = {
        "toolCallId": "t",
        "kind": "other",
        "title": "dromon__send_message",
    }
    asked = permission("other")
    asked["params"]["toolCall"] = titled
    server = FakeGrok([asked, chunk("done")])
    with pytest.raises(EngineRefused, match="not given: dromon__send_message"):
        run(line(server, project))
    answers = {a["id"]: a for a in server.answers()}
    assert answers["p1"]["result"]["outcome"]["optionId"] == "no"
    used = FakeGrok([note({"sessionUpdate": "tool_call", **titled})])
    with pytest.raises(EngineRefused, match="not given: dromon__send_message"):
        run(line(used, project))


def test_no_kind_is_exempt_from_the_tool_check(
    project: tuple[Path, Path, Path],
) -> None:
    """Antigravity review, F1, applied to Grok on 2026-10-26. A ``think`` call was
    once let through uncounted, and is now judged as any other kind, so refused."""
    server = FakeGrok([tool_call("think")])
    with pytest.raises(EngineRefused, match="not given: think"):
        run(line(server, project))
    asked = FakeGrok([permission("think")])
    with pytest.raises(EngineRefused, match="not given: think"):
        run(line(asked, project))
    assert asked.answers()[0]["result"]["outcome"]["optionId"] == "no"


@pytest.mark.parametrize(
    ("first", "later", "field"),
    [
        (tool_call("read"), call_update("t", kind="execute"), "kind"),
        (
            tool_call("other", "dromon__send_message"),
            call_update("t", toolName="gmail__send"),
            "toolName",
        ),
        (tool_call("read"), call_update("t", title="anything"), "title"),
        (tool_call("read"), tool_call("execute"), "kind"),
        # Re-review, N3. A field the first report lacked is a change too, so a
        # Grok that adds the kind only in a later update is refused.
        (
            call_update("t", toolName="dromon__send_message"),
            call_update("t", kind="other"),
            "kind",
        ),
    ],
)
def test_a_tool_call_whose_identity_changes_refuses_the_turn(
    project: tuple[Path, Path, Path],
    first: dict[str, Any],
    later: dict[str, Any],
    field: str,
) -> None:
    """Antigravity review, F2 and F3, applied to Grok. A call is judged when first
    seen, so a later update that renames it, retitles it or changes its kind refuses
    the turn, even from a permitted call to another permitted one."""
    server = FakeGrok([first, later, chunk("done")])
    config = LineConfig(builtin_tools=("Read", "Bash"))
    with pytest.raises(EngineRefused, match=f"changed the {field} of its tool call"):
        run(line(server, project, config=config))
    assert server.params("session/cancel") == {"sessionId": "s1"}


def test_a_permission_request_that_changes_a_call_is_rejected(
    project: tuple[Path, Path, Path],
) -> None:
    """A request naming one of Dromon's tools for a call first seen as a read is not
    the call that was checked, so it is rejected and the turn refused."""
    server = FakeGrok(
        [
            tool_call("read"),
            permission("other", "dromon__send_message"),
        ]
    )
    with pytest.raises(EngineRefused, match="changed the toolName of its tool call"):
        run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert server.answers()[0]["result"]["outcome"]["optionId"] == "no"


def test_a_tool_call_without_an_identifier_refuses_the_turn(
    project: tuple[Path, Path, Path],
) -> None:
    for update in (call_update(None, kind="read"), call_update("", kind="read")):
        server = FakeGrok([update])
        with pytest.raises(EngineRefused, match="without an identifier"):
            run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    asked = permission("read")
    del asked["params"]["toolCall"]["toolCallId"]
    server = FakeGrok([asked])
    with pytest.raises(EngineRefused, match="without an identifier"):
        run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert server.answers()[0]["result"]["outcome"]["optionId"] == "no"


def test_a_call_is_judged_and_counted_once_when_first_seen(
    project: tuple[Path, Path, Path],
) -> None:
    """Updates that repeat or leave out the identity fields are the call already
    judged, whether it was first seen as a request or as a notification."""
    server = FakeGrok(
        [
            permission("read", ident="a"),
            tool_call("read", ident="a"),
            call_update("a", status="completed"),
            tool_call("other", "dromon__send_message", ident="b"),
            call_update("b", toolName="dromon__send_message", status="completed"),
            chunk("done"),
        ]
    )
    report = run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert report.tool_uses == ("Read", "dromon__send_message")
    assert "session/cancel" not in server.methods()


def test_an_update_seen_first_is_judged_by_its_own_fields(
    project: tuple[Path, Path, Path],
) -> None:
    """An update for a call never announced is judged as a new call, and one with no
    kind is of the kind ``other``, which only Dromon's tools may be."""
    server = FakeGrok([call_update("a", kind="execute")])
    with pytest.raises(EngineRefused, match="not given: execute"):
        run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    bare = FakeGrok([call_update("a", status="in_progress")])
    with pytest.raises(EngineRefused, match="not given: an unnamed tool"):
        run(line(bare, project))


def test_the_driver_budget_cancels_a_turn_once(
    project: tuple[Path, Path, Path],
) -> None:
    budget = grok().settings.tool_budget
    server = FakeGrok([tool_call("read", ident=f"t{i}") for i in range(budget + 3)])
    report = run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert report.interrupted_for == "tool_budget"
    assert report.subtype == "cancelled"
    assert server.methods().count("session/cancel") == 1


def test_a_cancel_grok_ignores_ends_the_turn_and_stops_grok(
    project: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review, N2. A cancel bounds the wait for Grok's answer, after which the
    connection is left, which stops Grok. An interrupt stays an interrupt, and a
    refusal keeps its own reason."""
    monkeypatch.setattr(grok_module, "ABORT_SECONDS", 0.05)
    budget = grok().settings.tool_budget
    server = FakeGrok([tool_call("read", ident=f"t{i}") for i in range(budget + 1)])
    server.answer_prompt = False
    server.ignores_cancel = True
    report = run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert report.interrupted_for == "tool_budget"
    assert report.subtype == "cancelled"
    assert server.methods().count("session/cancel") == 1
    assert server.stops == 1
    refused = FakeGrok([tool_call("execute")])
    refused.answer_prompt = False
    refused.ignores_cancel = True
    with pytest.raises(EngineRefused, match="not given: execute"):
        run(line(refused, project, config=LineConfig(builtin_tools=("Read",))))
    assert refused.stops == 1


def test_a_closed_connection_refuses_the_turn(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok([chunk("partial")])
    server.answer_prompt = False
    with pytest.raises(EngineRefused, match="closed its connection"):
        run(line(server, project))


def test_grok_start_and_turn_deadlines_are_bounded(
    project: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grok_module, "START_SECONDS", 0.01)
    stalled_start = FakeGrok()
    stalled_start.blocked_method = "session/new"
    with pytest.raises(EngineRefused, match="start timed out during open session"):
        run(line(stalled_start, project))
    assert stalled_start.stops == 1

    stalled_initialize = FakeGrok()
    stalled_initialize.blocked_method = "initialize"
    with pytest.raises(EngineRefused, match="start timed out during initialize"):
        run(line(stalled_initialize, project))
    assert stalled_initialize.stops == 1

    monkeypatch.setattr(grok_module, "TURN_SECONDS", 0.1)
    stalled_turn = FakeGrok()
    stalled_turn.answer_prompt = False
    stalled_turn.ignores_cancel = True
    with pytest.raises(
        EngineRefused, match="Grok turn had no activity for 0.1 seconds"
    ):
        run(line(stalled_turn, project))
    assert stalled_turn.stops == 1


def test_grok_sliding_deadline_resets_on_activity_and_obeys_absolute_cap(
    project: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(grok_module, "TURN_SECONDS", 0.1)
    monkeypatch.setattr(grok_module, "ABSOLUTE_TURN_SECONDS", 0.5)
    active = FakeGrok([chunk(str(index)) for index in range(6)])
    active.activity_delay = 0.02
    report = run(line(active, project))
    assert report.text == "012345"

    monkeypatch.setattr(grok_module, "ABSOLUTE_TURN_SECONDS", 0.15)
    capped = FakeGrok([chunk(str(index)) for index in range(10)])
    capped.activity_delay = 0.02
    with pytest.raises(EngineRefused, match="absolute cap of 0.15 seconds"):
        run(line(capped, project))


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
            "wake_after",
            "acquire_lease",
            "release_lease",
            "line_status",
            "read_instructions",
            "write_scratch",
            "list_processes",
            "stop_process",
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


def test_a_grok_line_carries_its_process_mark(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok([chunk("hello")])
    target = line(server, project)
    target.mark = "alpha.token"
    run(target)
    assert server.environment["DROMON_LINE_MARK"] == "alpha.token"


# The safe point of priority mail, decided by the operator on 2026-10-27. A turn ends
# once a tool result is delivered and not at the text before the tool use.

type Steered = Callable[[str, Strategy], tuple[SteeredDriver, list[int]]]
REASON = "priority mail m-1 from @ana"


def test_priority_mail_ends_the_turn_at_the_tool_result_and_not_the_text(
    project: tuple[Path, Path, Path], steered: Steered
) -> None:
    driver, asked = steered(GROK, Strategy.SUBSCRIPTION)
    server = FakeGrok(
        [
            chunk("I will read it"),
            tool_call("read", ident="t1"),
            call_update("t1", status="in_progress"),
            call_update("t1", status="completed"),
            call_update("t1", status="completed"),
            chunk("done"),
        ]
    )
    config = LineConfig(builtin_tools=("Read",))
    report = run(line(server, project, config=config, driver=driver))
    # A second update that the call completed is not a second result.
    assert asked == [1]
    assert report.interrupted_for == REASON
    assert server.methods().count("session/cancel") == 1
