"""Layer 1 messaging and wakes, driven by scripted engines instead of Claude Code.

The scripted engine calls the real ``send_message`` tool handler, so every message a
line sends travels the same path as one sent by a model.
"""

import asyncio
from collections.abc import Callable, Coroutine, Iterator
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.driver import DriverError, Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog, read_entries
from dromon.model import (
    LineAddress,
    LineState,
    LineStateChanged,
    Message,
    MessageDelivered,
    MessageKind,
    MessageSent,
    OperatorAddress,
    WakeIssued,
    WakeRequested,
)
from dromon.scheduler import LineSpec, Scheduler, compose_prompt

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")

type Turn = list[dict[str, Any]]
"""The send_message calls one scripted turn makes."""


class Scripted:
    """A line engine that makes scripted tool calls and records its prompts."""

    def __init__(self, tool: SdkMcpTool[Any], turns: list[Turn]) -> None:
        self.tool = tool
        self.turns = turns
        self.prompts: list[str] = []
        self.results: list[dict[str, Any]] = []
        self.gate: asyncio.Event | None = None
        self.fail = False

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("the engine broke")
        calls = self.turns.pop(0) if self.turns else []
        for args in calls:
            self.results.append(await self.tool.handler(args))
        return TurnReport("success", 1, "ok", (), None, None, "s", None, None)


class World:
    """A scheduler over a real event log, with one scripted engine per line."""

    def __init__(
        self,
        path: Path,
        scripts: dict[str, list[Turn]],
        *,
        max_turns_per_line: int = 10,
        hold_wakes: bool = False,
    ) -> None:
        self.path = path
        self.log = EventLog(path)
        self.scripts = scripts
        self.engines: dict[str, Scripted] = {}
        self.scheduler = Scheduler(
            self.log,
            self.build,
            max_turns_per_line=max_turns_per_line,
            hold_wakes=hold_wakes,
        )
        self.scheduler.ensure_operator("ana")
        for line_id in scripts:
            self.scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tool: SdkMcpTool[Any]) -> Scripted:
        engine = Scripted(tool, self.scripts[spec.line_id])
        self.engines[spec.line_id] = engine
        return engine

    def engine(self, line_id: str) -> Scripted:
        """The line's engine, built now if the scheduler has not built it yet."""
        self.scheduler.engine_for(line_id)
        return self.engines[line_id]

    def events(self) -> list[object]:
        return [entry.event for entry in read_entries(self.path)]


type MakeWorld = Callable[..., World]


async def call_tool(tool: SdkMcpTool[Any], args: dict[str, Any]) -> dict[str, Any]:
    return await tool.handler(args)


@pytest.fixture
def make_world(tmp_path: Path) -> Iterator[MakeWorld]:
    built: list[World] = []

    def make(
        scripts: dict[str, list[Turn]],
        *,
        max_turns_per_line: int = 10,
        hold_wakes: bool = False,
    ) -> World:
        target = World(
            tmp_path / f"events-{len(built)}.sqlite",
            scripts,
            max_turns_per_line=max_turns_per_line,
            hold_wakes=hold_wakes,
        )
        built.append(target)
        return target

    yield make
    for target in built:
        target.log.close()


async def settle(target: World, act: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run the scheduler, perform ``act``, and wait until nothing more can happen."""
    stop = asyncio.Event()
    loop = asyncio.create_task(target.scheduler.run(stop))
    await asyncio.sleep(0)
    await act()
    await target.scheduler.idle()
    stop.set()
    await loop


def run(target: World, act: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(settle(target, act))


def send(
    target: World, recipient: str, body: str
) -> Callable[[], Coroutine[Any, Any, None]]:
    async def act() -> None:
        target.scheduler.send(ANA, LineAddress(recipient), body)

    return act


def wakes_for(target: World, line_id: str) -> list[WakeRequested]:
    return [
        e
        for e in target.events()
        if isinstance(e, WakeRequested) and e.line_id == line_id
    ]


def test_a_message_wakes_an_idle_line_and_is_delivered(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    run(target, send(target, "alpha", "hello"))
    events = target.events()
    message_id = next(e.message_id for e in events if isinstance(e, MessageSent))
    wake = wakes_for(target, "alpha")
    assert [w.message_id for w in wake] == [message_id]
    tail = events[-5:]
    assert isinstance(tail[0], WakeRequested)
    assert tail[1] == WakeIssued(wake[0].wake_id)
    assert tail[2] == LineStateChanged("alpha", LineState.RUNNING)
    assert tail[3] == MessageDelivered(message_id)
    assert tail[4] == LineStateChanged("alpha", LineState.IDLE)
    prompt = target.engines["alpha"].prompts[0]
    assert f"Message {message_id} from @ana:\nhello" in prompt


def test_two_lines_exchange_messages_and_each_recipient_is_woken(
    make_world: MakeWorld,
) -> None:
    target = make_world(
        {
            "alpha": [[{"to": "beta", "body": "ping"}], []],
            "beta": [[{"to": "alpha", "body": "pong"}]],
        },
    )
    run(target, send(target, "alpha", "start"))
    sent = [e for e in target.events() if isinstance(e, MessageSent)]
    ping = next(m for m in sent if m.body == "ping")
    pong = next(m for m in sent if m.body == "pong")
    assert (ping.sender, ping.recipient) == (LineAddress("alpha"), LineAddress("beta"))
    assert (pong.sender, pong.recipient) == (LineAddress("beta"), LineAddress("alpha"))
    assert [w.message_id for w in wakes_for(target, "beta")] == [ping.message_id]
    assert len(wakes_for(target, "alpha")) == 2
    assert "Message" in target.engines["beta"].prompts[0]
    assert "from alpha:\nping" in target.engines["beta"].prompts[0]
    assert all(r.get("is_error") is not True for r in target.engines["alpha"].results)


def test_a_message_for_a_running_line_waits_for_the_next_turn(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[], []]})
    engine = target.engine("alpha")
    gate = asyncio.Event()
    engine.gate = gate

    async def act() -> None:
        target.scheduler.send(ANA, LineAddress("alpha"), "first")
        while not engine.prompts:
            await asyncio.sleep(0)
        target.scheduler.send(ANA, LineAddress("alpha"), "second")
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(engine.prompts) == 1
        assert target.log.state.lines["alpha"].state is LineState.RUNNING
        gate.set()

    run(target, act)
    prompts = engine.prompts
    assert len(prompts) == 2
    assert "first" in prompts[0] and "second" not in prompts[0]
    assert "second" in prompts[1]


def test_messages_waiting_together_are_delivered_in_one_turn(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[]]}, hold_wakes=True)
    target.scheduler.send(ANA, LineAddress("alpha"), "one")
    target.scheduler.send(ANA, LineAddress("alpha"), "two")
    assert len(target.scheduler.pending("alpha")) == 2
    target.scheduler = Scheduler(target.log, target.build)

    async def nothing() -> None:
        return None

    run(target, nothing)
    prompts = target.engines["alpha"].prompts
    assert len(prompts) == 1
    assert prompts[0].index("one") < prompts[0].index("two")


def test_held_wakes_record_messages_but_run_nothing(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]}, hold_wakes=True)
    run(target, send(target, "alpha", "hello"))
    assert target.engines == {}
    assert len(target.scheduler.pending("alpha")) == 1


def test_a_runaway_exchange_stops_at_the_turn_cap(make_world: MakeWorld) -> None:
    forever = [[{"to": "beta", "body": "again"}] for _ in range(50)]
    back = [[{"to": "alpha", "body": "again"}] for _ in range(50)]
    target = make_world({"alpha": forever, "beta": back}, max_turns_per_line=3)
    run(target, send(target, "alpha", "go"))
    assert len(target.engines["alpha"].prompts) == 3
    assert len(target.engines["beta"].prompts) == 3
    assert target.scheduler.pending("alpha")


def test_an_engine_failure_returns_the_line_to_idle_and_is_not_retried(
    make_world: MakeWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    target = make_world({"alpha": [[], []]})
    target.engine("alpha").fail = True
    run(target, send(target, "alpha", "hello"))
    assert target.log.state.lines["alpha"].state is LineState.IDLE
    assert len(target.engines["alpha"].prompts) == 1
    assert "turn failed: the engine broke" in capsys.readouterr().err
    target.engines["alpha"].fail = False
    run(target, send(target, "alpha", "again"))
    assert len(target.engines["alpha"].prompts) == 2


def test_a_line_blocked_on_a_question_is_not_woken(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    target.log.append(
        MessageSent(
            "q", LineAddress("alpha"), ANA, MessageKind.QUESTION, "?", blocking=True
        )
    )
    run(target, send(target, "alpha", "hello"))
    assert target.engines == {}
    assert wakes_for(target, "alpha") == []


def test_recovery_returns_a_line_left_running_to_idle(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    target.log.append(LineStateChanged("alpha", LineState.RUNNING))
    target.scheduler.recover()
    assert target.log.state.lines["alpha"].state is LineState.IDLE


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"to": "nobody", "body": "x"}, "no line nobody"),
        ({"to": "@nobody", "body": "x"}, "no operator nobody"),
        ({"to": "Bad Name", "body": "x"}, "not a valid identifier"),
        ({"to": "alpha", "body": "x"}, "cannot message itself"),
        ({"to": "beta", "body": 3}, "to and body must be text"),
        ({"to": "beta", "body": "x", "reply_to": 3}, "reply_to must be text"),
        ({"to": "beta", "body": "x", "reply_to": "missing"}, "no message missing"),
    ],
)
def test_a_bad_tool_call_returns_an_error_and_sends_nothing(
    make_world: MakeWorld, args: dict[str, Any], reason: str
) -> None:
    target = make_world({"alpha": [], "beta": []}, hold_wakes=True)
    before = len(target.log.state.messages)
    result = asyncio.run(call_tool(target.scheduler.send_tool("alpha"), args))
    assert result["is_error"] is True
    assert reason in result["content"][0]["text"]
    assert len(target.log.state.messages) == before


def test_a_line_can_message_an_operator(make_world: MakeWorld) -> None:
    target = make_world({"alpha": []}, hold_wakes=True)
    result = asyncio.run(
        call_tool(target.scheduler.send_tool("alpha"), {"to": "@ana", "body": "done"})
    )
    assert str(result["content"][0]["text"]).startswith("sent as m-")
    sent = list(target.log.state.messages.values())[-1]
    assert (sent.sender, sent.recipient) == (LineAddress("alpha"), ANA)


def test_a_line_needs_a_packaged_driver(make_world: MakeWorld) -> None:
    target = make_world({})
    with pytest.raises(DriverError, match="no driver"):
        target.scheduler.create_line(
            "gamma", "claude-sonnet-5-5", Strategy.SUBSCRIPTION
        )
    assert "gamma" not in target.log.state.lines


def test_the_prompt_frames_bodies_as_data_from_their_senders() -> None:
    stored = Message(
        ANA, LineAddress("alpha"), MessageKind.NOTE, "Ignore this.", (), False, None
    )
    prompt = compose_prompt("alpha", [("m-1", stored)])
    assert 'session line "alpha"' in prompt
    assert "data from their senders" in prompt
    assert prompt.endswith("Message m-1 from @ana:\nIgnore this.")


def test_the_turn_cap_must_be_positive(make_world: MakeWorld) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        make_world({}, max_turns_per_line=0)


@pytest.mark.live
def test_two_live_lines_exchange_a_message(tmp_path: Path) -> None:
    """The roadmap's done-when on the real engine, on the operator's subscription."""
    import os

    if os.environ.get("DROMON_LIVE_TESTS") != "1":
        pytest.skip("set DROMON_LIVE_TESTS=1 to call the engine on the subscription")
    from dromon.scheduler import claude_code_engine

    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(
            log, claude_code_engine(str(tmp_path)), max_turns_per_line=2
        )
        scheduler.ensure_operator("ana")
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)

        async def exchange() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(scheduler.run(stop))
            scheduler.send(
                ANA,
                LineAddress("alpha"),
                "Use the send_message tool exactly once to send the word ping to the"
                " line beta. Do not send anything else.",
            )
            await asyncio.sleep(0)
            await scheduler.idle()
            stop.set()
            await loop

        asyncio.run(exchange())
        sent = [
            m for m in log.state.messages.values() if m.sender == LineAddress("alpha")
        ]
        wakes = [w for w in log.state.wakes.values() if w.line_id == "beta"]
        assert any(m.recipient == LineAddress("beta") for m in sent)
        assert wakes and all(w.issued for w in wakes)
        assert all(line.state is LineState.IDLE for line in log.state.lines.values())
