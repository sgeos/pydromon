"""A Codex line that keeps its app-server between turns, driven by an interactive
fake app-server that answers requests as the installed Codex was read to answer
them. Nothing here runs Codex."""

import asyncio
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.codex import (
    CODEX_SELF_STARTED_NOTE,
    LIST_TERMINALS,
    TERMINATE_TERMINAL,
    CodexLine,
    Connection,
)
from dromon.driver import Driver, Strategy
from dromon.engine import DIED_NOTE, EngineRefused, closed_note
from dromon.lineconfig import LineConfig

LUNA = "gpt-6-luna"
KEPT = LineConfig(keep_session=True, builtin_tools=("Bash",))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Server:
    """One fake app-server process. Requests are answered at once. ``turn/start``
    plays a turn whose messages ``script`` gives."""

    def __init__(self, world: "World") -> None:
        self.world = world
        self.out: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self.turns = 0

    def methods(self) -> list[str]:
        return [str(m["method"]) for m in self.sent if "method" in m]

    async def send(self, message: Mapping[str, object]) -> None:
        sent = dict(message)
        self.sent.append(sent)
        method = sent.get("method")
        if not isinstance(method, str) or "id" not in sent:
            return
        if method in self.world.failing:
            self.out.put_nowait({"id": sent["id"], "error": {"message": "no"}})
            return
        self.out.put_nowait({"id": sent["id"], "result": self.result(method, sent)})
        if method == "turn/start":
            self.turns += 1
            turn_id = f"t{self.turns}"
            self.out.put_nowait(
                {"method": "turn/started", "params": {"turn": {"id": turn_id}}}
            )
            for item in self.world.script.pop(0):
                self.out.put_nowait(item)
        if method == "turn/interrupt":
            self.out.put_nowait(
                {
                    "method": "turn/completed",
                    "params": {"turn": {"id": "x", "status": "interrupted"}},
                }
            )

    def result(self, method: str, sent: dict[str, Any]) -> dict[str, Any]:
        world = self.world
        if method == "account/read":
            return {"account": {"type": world.account}}
        if method in ("thread/start", "thread/resume"):
            return {
                "thread": {"id": "th"},
                "model": LUNA,
                "modelProvider": "openai",
                "activePermissionProfile": {"id": world.profile},
            }
        if method in ("mcpServerStatus/list", "app/list"):
            return {"data": []}
        if method == "turn/start":
            return {"turn": {"id": f"t{self.turns + 1}"}}
        if method == LIST_TERMINALS:
            return {"data": list(world.terminals), "nextCursor": None}
        if method == TERMINATE_TERMINAL:
            params = cast(dict[str, Any], sent["params"])
            world.terminals = [
                t for t in world.terminals if t["processId"] != params["processId"]
            ]
            return {}
        return {}

    async def receive(self) -> dict[str, Any] | None:
        return await self.out.get()


def ran(item_id: str, process_id: str | None = None) -> dict[str, Any]:
    """A command the line's own turn ran, as the engine reports its item."""
    item: dict[str, Any] = {"type": "commandExecution", "id": item_id}
    if process_id is not None:
        item["processId"] = process_id
    return {"method": "item/started", "params": {"item": item}}


def said(text: str) -> dict[str, Any]:
    return {
        "method": "item/completed",
        "params": {"item": {"type": "agentMessage", "text": text}},
    }


def done() -> dict[str, Any]:
    return {
        "method": "turn/completed",
        "params": {"turn": {"id": "t", "status": "completed"}},
    }


class World:
    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        self.script = script
        self.servers: list[Server] = []
        self.account = "chatgpt"
        self.profile = "dromon-line"
        self.terminals: list[dict[str, object]] = []
        self.failing: set[str] = set()
        self.instructions = ""

    def line(
        self,
        tmp_path: Path,
        config: LineConfig = KEPT,
        clock: Clock | None = None,
        poll: float = 3600.0,
        tools: list[SdkMcpTool[Any]] | None = None,
    ) -> CodexLine:
        @asynccontextmanager
        async def connect(
            arguments: list[str], cwd: Path, env: Mapping[str, str]
        ) -> AsyncGenerator[Connection]:
            del arguments, cwd, env
            server = Server(self)
            self.servers.append(server)
            try:
                yield server
            finally:
                server.closed = True
                server.out.put_nowait(None)

        return CodexLine(
            Driver.for_model(LUNA, Strategy.SUBSCRIPTION),
            tmp_path,
            config=config,
            mcp_tools=tools or [],
            environment={},
            connection_factory=connect,
            server_lister=lambda env: [],
            clock=clock or Clock(),
            poll_seconds=poll,
        )


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


def test_a_kept_codex_line_reuses_its_app_server_and_checks_again(
    tmp_path: Path,
) -> None:
    world = World([[said("one"), done()], [said("two"), done()]])
    engine = world.line(tmp_path)

    async def go() -> None:
        first = await engine.run_turn("first")
        second = await engine.run_turn("second")
        assert (first.text, second.text) == ("one", "two")
        assert engine.kept_status()["kept"] is True
        await engine.close()

    asyncio.run(go())
    assert len(world.servers) == 1
    methods = world.servers[0].methods()
    # initialize runs once, and every check runs at both turns.
    assert methods.count("initialize") == 1
    for check in ("account/read", "mcpServerStatus/list", "app/list"):
        assert methods.count(check) == 2
    assert methods.count("thread/start") == 1 and methods.count("thread/resume") == 1
    assert world.servers[0].closed


def test_a_reused_codex_connection_refuses_a_changed_profile(tmp_path: Path) -> None:
    world = World([[done()], [done()]])
    engine = world.line(tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        world.profile = "something-else"
        with pytest.raises(EngineRefused, match="permission profile"):
            await engine.run_turn("second")
        assert engine.kept_status()["kept"] is False

    asyncio.run(go())
    assert world.servers[0].closed


def test_a_reused_codex_connection_refuses_a_changed_account(tmp_path: Path) -> None:
    world = World([[done()], [done()]])
    engine = world.line(tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        world.account = "apiKey"
        with pytest.raises(EngineRefused, match="refuses the api credential"):
            await engine.run_turn("second")

    asyncio.run(go())
    assert world.servers[0].closed


def test_a_failed_check_request_closes_the_connection(tmp_path: Path) -> None:
    world = World([[done()], [done()]])
    engine = world.line(tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        world.failing = {"thread/resume"}
        with pytest.raises(EngineRefused, match="refused a request"):
            await engine.run_turn("second")

    asyncio.run(go())
    assert world.servers[0].closed


def test_changed_instructions_open_a_new_app_server(tmp_path: Path) -> None:
    rules = tmp_path / "AGENTS.md"
    rules.write_text("old")
    config = LineConfig(keep_session=True, instructions=("AGENTS.md",))
    world = World([[done()], [done()]])
    engine = world.line(tmp_path, config=config)

    async def go() -> None:
        await engine.run_turn("first")
        rules.write_text("new")
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert len(world.servers) == 2 and world.servers[0].closed
    prompt = next(m for m in world.servers[1].sent if m.get("method") == "turn/start")[
        "params"
    ]["input"][0]["text"]
    assert prompt.startswith(
        closed_note("its configuration, instructions, tools or environment changed", 0)
    )


def test_a_dead_app_server_is_replaced_with_a_note(tmp_path: Path) -> None:
    world = World([[done()], [done()]])
    engine = world.line(tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        world.servers[0].out.put_nowait(None)
        await settle()
        assert engine.kept_status()["kept"] is False
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    prompt = next(m for m in world.servers[1].sent if m.get("method") == "turn/start")[
        "params"
    ]["input"][0]["text"]
    assert prompt.startswith(DIED_NOTE)


def test_requests_between_turns_are_refused_and_a_self_started_turn_interrupted(
    tmp_path: Path,
) -> None:
    world = World([[done()], [done()]])
    engine = world.line(tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        server = world.servers[0]
        server.out.put_nowait(
            {"id": 900, "method": "item/tool/call", "params": {"tool": "x"}}
        )
        server.out.put_nowait(
            {"id": 901, "method": "item/commandExecution/requestApproval"}
        )
        server.out.put_nowait(
            {"method": "turn/started", "params": {"turn": {"id": "own"}}}
        )
        await settle()
        answers = {m["id"]: m for m in server.sent if m.get("id") in (900, 901)}
        assert "error" in answers[900]
        assert answers[901]["result"] == {"decision": "decline"}
        interrupts = [m for m in server.sent if m.get("method") == "turn/interrupt"]
        assert interrupts[-1]["params"]["turnId"] == "own"
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    prompt = [m for m in world.servers[0].sent if m.get("method") == "turn/start"][-1][
        "params"
    ]["input"][0]["text"]
    assert prompt.startswith(CODEX_SELF_STARTED_NOTE)


def test_background_terminals_are_listed_and_stopped_on_the_own_thread(
    tmp_path: Path,
) -> None:
    # p1 is known by its process identifier, p2 by its item, and p3 is not the line's.
    world = World([[ran("i1", "p1"), ran("i2"), done()]])
    world.terminals = [
        {"itemId": "i1", "processId": "p1", "osPid": 41, "command": "make gate"},
        {"itemId": "i2", "processId": "p2", "osPid": 42},
        {"itemId": "i3", "processId": "p3", "osPid": 43, "command": "other"},
    ]
    engine = world.line(tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        assert engine.kept_status()["background_tasks"] == 2
        listed = await engine.list_background()
        assert listed == [
            {"process_id": "p1", "os_pid": 41, "command": "make gate"},
            {"process_id": "p2", "os_pid": 42, "command": ""},
        ]
        with pytest.raises(EngineRefused, match="not one of your background"):
            await engine.stop_background("p9")
        with pytest.raises(EngineRefused, match="not one of your background"):
            await engine.stop_background("p3")
        with pytest.raises(EngineRefused, match="1 to 128"):
            await engine.stop_background("")
        assert await engine.stop_background("p1") == "terminated background terminal p1"
        assert engine.kept_status()["background_tasks"] == 1
        await engine.close()
        with pytest.raises(EngineRefused, match="keeps no engine session"):
            await engine.list_background()

    asyncio.run(go())
    terminate = next(
        m for m in world.servers[0].sent if m.get("method") == TERMINATE_TERMINAL
    )
    assert terminate["params"] == {"threadId": "th", "processId": "p1"}
    listing = next(
        m for m in world.servers[0].sent if m.get("method") == LIST_TERMINALS
    )
    assert listing["params"] == {"threadId": "th"}


def test_a_tool_may_list_terminals_during_a_turn_without_deadlock(
    tmp_path: Path,
) -> None:
    world = World([])
    world.terminals = [{"processId": "p1", "osPid": 41}]
    found: list[object] = []
    engine_box: list[CodexLine] = []

    async def lister(args: dict[str, Any]) -> dict[str, Any]:
        del args
        found.append(await engine_box[0].list_background())
        return {"content": [{"type": "text", "text": "ok"}]}

    tool: SdkMcpTool[Any] = SdkMcpTool(
        name="lister", description="List.", input_schema={}, handler=lister
    )
    world.script = [
        [
            ran("i1", "p1"),
            {
                "id": 700,
                "method": "item/tool/call",
                "params": {"tool": "lister", "arguments": {}},
            },
            done(),
        ],
        [done()],
    ]
    engine = world.line(tmp_path, tools=[tool])
    engine_box.append(engine)

    async def go() -> None:
        await engine.run_turn("first")
        await asyncio.wait_for(engine.run_turn("second"), 5)
        await engine.close()

    asyncio.run(go())
    assert found and found[-1] == [{"process_id": "p1", "os_pid": 41, "command": ""}]


def test_a_kept_codex_line_closes_when_idle_and_at_the_ceiling(tmp_path: Path) -> None:
    clock = Clock()
    config = LineConfig(
        keep_session=True,
        keep_session_idle_seconds=60,
        keep_session_max_seconds=600,
        builtin_tools=("Bash",),
    )
    world = World([[ran("i1", "p1"), done()], [done()]])
    world.terminals = [{"processId": "p1", "osPid": 41}]
    engine = world.line(tmp_path, config=config, clock=clock)

    async def go() -> None:
        await engine.run_turn("first")
        assert engine.seconds_to_close() == 600
        clock.now += 599
        assert not await engine.expire()
        clock.now += 1
        assert await engine.expire()
        world.terminals = []
        await engine.run_turn("second")
        assert engine.seconds_to_close() == 60
        clock.now += 60
        assert await engine.expire()

    asyncio.run(go())
    assert all(server.closed for server in world.servers)


def test_retiring_a_kept_codex_line_closes_it(tmp_path: Path) -> None:
    world = World([[done()]])
    engine = world.line(tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        await engine.retire()

    asyncio.run(go())
    assert world.servers[0].closed


def test_an_interrupted_kept_codex_turn_interrupts_the_engine(tmp_path: Path) -> None:
    world = World([[], [done()]])
    engine = world.line(tmp_path)

    async def go() -> None:
        turn = asyncio.create_task(engine.run_turn("first"))
        await settle()
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await settle()
        server = world.servers[0]
        interrupts = [m for m in server.sent if m.get("method") == "turn/interrupt"]
        assert interrupts[-1]["params"]["turnId"] == "t1"
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert len(world.servers) == 1


# Review of round 3. F5, F6 and F7.


def test_a_self_started_turn_without_an_identifier_closes_the_connection(
    tmp_path: Path,
) -> None:
    world = World([[done()], [done()]])
    engine = world.line(tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        world.servers[0].out.put_nowait({"method": "turn/started", "params": {}})
        await settle()
        assert engine.kept_status()["kept"] is False
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    first = world.servers[0]
    assert first.closed
    assert not [m for m in first.sent if m.get("method") == "turn/interrupt"]
    prompt = next(m for m in world.servers[1].sent if m.get("method") == "turn/start")[
        "params"
    ]["input"][0]["text"]
    assert CODEX_SELF_STARTED_NOTE in prompt
    assert "without naming it" in prompt


def test_terminals_of_other_commands_are_neither_listed_nor_stopped(
    tmp_path: Path,
) -> None:
    world = World([[done()]])
    # The server returns a terminal no turn of this line reported.
    world.terminals = [{"itemId": "x", "processId": "foreign", "osPid": 9}]
    engine = world.line(tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        assert await engine.list_background() == []
        with pytest.raises(EngineRefused, match="not one of your background"):
            await engine.stop_background("foreign")
        await engine.close()

    asyncio.run(go())
    assert not [
        m for m in world.servers[0].sent if m.get("method") == TERMINATE_TERMINAL
    ]


def test_writes_on_a_kept_connection_are_never_interleaved() -> None:
    from dromon.codex import _Routed  # pyright: ignore[reportPrivateUsage]

    events: list[str] = []

    class Slow:
        async def send(self, message: Mapping[str, object]) -> None:
            events.append(f"start {message['n']}")
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            events.append(f"end {message['n']}")

        async def receive(self) -> dict[str, Any] | None:
            return None

    async def between(message: dict[str, Any]) -> None:
        del message

    async def go() -> None:
        routed = _Routed(Slow(), between)
        await asyncio.gather(*(routed.send({"n": n}) for n in range(3)))

    asyncio.run(go())
    assert events == ["start 0", "end 0", "start 1", "end 1", "start 2", "end 2"]
