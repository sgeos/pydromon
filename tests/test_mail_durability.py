"""Row 11 of the T3 Code review. Mail that arrives during a turn is never lost,
reordered or settled falsely, and a retried mutating line tool acts once.

Delivery here means the mail was announced in a prompt. Reading, acknowledging and
answering settle it, and only the line does those. Every engine is scripted."""

import asyncio
from collections.abc import Callable, Coroutine, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog
from dromon.model import (
    Event,
    LineAddress,
    MessageDelivered,
    MessageSent,
    OperatorAddress,
    QuestionStatus,
    WakeScheduled,
)
from dromon.requestkeys import (
    MAX_KEY_LENGTH,
    REPEATED,
    REQUEST_KEY,
    RequestKeyRefused,
    RequestKeys,
)
from dromon.scheduler import (
    ASK_TOOL_SCHEMA,
    SEND_TOOL_SCHEMA,
    WAKE_TOOL_SCHEMA,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")


class Engine:
    """A line engine whose turn waits for its gate, then runs ``during``."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.prompts: list[str] = []
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.during: Callable[[], object] | None = None
        self.fail = False

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.during is not None:
            self.during()
        if self.fail:
            raise RuntimeError("the engine broke")
        return TurnReport("success", 1, "ok", (), None, None, "session-1", None, None)


class World:
    def __init__(self, path: Path, engine: Engine, *, create: bool = True) -> None:
        self.log = EventLog(path)
        self.engine = engine
        self.events: list[Event] = []
        original = self.log.append

        def append(event: Event) -> Any:  # noqa: ANN401
            self.events.append(event)
            return original(event)

        self.log.append = append  # type: ignore[method-assign]
        self.scheduler = Scheduler(
            self.log, self.build, tick_seconds=0, clock=lambda: 0.0
        )
        if create:
            self.scheduler.ensure_operator("ana")
            self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
            self.scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Engine:
        del spec, tools
        return self.engine

    def delivered(self, message_id: str) -> int:
        return sum(
            1
            for event in self.events
            if isinstance(event, MessageDelivered) and event.message_id == message_id
        )

    def message(self, message_id: str) -> Any:  # noqa: ANN401
        return self.log.state.messages[message_id]


@pytest.fixture
def worlds(tmp_path: Path) -> Iterator[Callable[..., World]]:
    made: list[World] = []

    def make(engine: Engine, **options: Any) -> World:  # noqa: ANN401
        world = World(tmp_path / "events.sqlite", engine, **options)
        made.append(world)
        return world

    yield make
    for world in made:
        world.log.close()


async def running(
    world: World, act: Callable[[], Coroutine[Any, Any, None]], *, settle: bool = True
) -> None:
    stop = asyncio.Event()
    loop = asyncio.create_task(world.scheduler.run(stop))
    await asyncio.sleep(0)
    await act()
    if settle:
        await world.scheduler.idle()
        for _ in range(20):
            await asyncio.sleep(0)
    stop.set()
    await loop


def run(
    world: World, act: Callable[[], Coroutine[Any, Any, None]], *, settle: bool = True
) -> None:
    asyncio.run(running(world, act, settle=settle))


def order_in(prompt: str, *message_ids: str) -> list[int]:
    return [prompt.index(message_id) for message_id in message_ids]


# Mail during a turn.


def test_mail_that_arrives_during_a_turn_comes_next_once_and_in_order(
    worlds: Callable[..., World],
) -> None:
    engine = Engine()
    world = worlds(engine)
    later: list[str] = []

    async def act() -> None:
        engine.gate = asyncio.Event()
        first = world.scheduler.send(ANA, ALPHA, "first")
        await engine.started.wait()
        for number in range(3):
            later.append(world.scheduler.send(ANA, ALPHA, f"later {number}"))
        later.append(world.scheduler.send(LineAddress("beta"), ALPHA, "from beta"))
        for message_id in later:
            assert world.delivered(message_id) == 0
            assert not world.message(message_id).read
        assert first in engine.prompts[0]
        engine.gate.set()

    run(world, act)
    assert len(engine.prompts) == 2
    second = engine.prompts[1]
    positions = order_in(second, *later)
    assert positions == sorted(positions)
    assert f"{len(later)} new messages" in second
    for message_id in later:
        assert world.delivered(message_id) == 1
        message = world.message(message_id)
        assert not message.read and not message.acknowledged


def test_mail_read_during_its_turn_is_not_announced_again(
    worlds: Callable[..., World],
) -> None:
    engine = Engine()
    world = worlds(engine)
    seen: list[str] = []

    def arrive_and_read() -> None:
        message_id = world.scheduler.send(ANA, ALPHA, "read me now")
        world.scheduler.read(ALPHA, message_id)
        seen.append(message_id)

    async def act() -> None:
        engine.during = arrive_and_read
        world.scheduler.send(ANA, ALPHA, "first")

    run(world, act)
    assert len(engine.prompts) == 1
    assert world.delivered(seen[0]) == 1 and world.message(seen[0]).read


def test_a_failed_turn_settles_none_of_its_mail_and_loses_none_after(
    worlds: Callable[..., World],
) -> None:
    engine = Engine()
    world = worlds(engine)
    arrived: list[str] = []

    async def act() -> None:
        engine.fail = True
        engine.during = lambda: arrived.append(
            world.scheduler.send(ANA, ALPHA, "during the failing turn")
        )
        arrived.insert(0, world.scheduler.send(ANA, ALPHA, "first"))

    run(world, act)
    first, during = arrived
    held = world.log.state.lines["alpha"].held_by
    assert held is not None
    assert world.message(held).status is QuestionStatus.OPEN
    for message_id in arrived:
        message = world.message(message_id)
        assert not message.read and not message.acknowledged
    assert world.delivered(first) == 1 and world.delivered(during) == 0
    engine.fail = False
    engine.during = None

    async def resume() -> None:
        world.scheduler.answer(ANA, held, "resume")

    run(world, resume)
    after = "".join(engine.prompts[1:])
    answer = next(
        message_id
        for message_id, message in world.log.state.messages.items()
        if message.reply_to == held
    )
    assert order_in(after, during, answer) == sorted(order_in(after, during, answer))
    assert world.delivered(during) == 1 and world.delivered(answer) == 1
    assert not world.message(first).read
    assert "3 unread" in engine.prompts[-1]


def test_mail_sent_during_a_turn_survives_a_restart(
    worlds: Callable[..., World],
) -> None:
    engine = Engine()
    world = worlds(engine)
    arrived: list[str] = []

    async def act() -> None:
        engine.gate = asyncio.Event()
        arrived.append(world.scheduler.send(ANA, ALPHA, "first"))
        await engine.started.wait()
        arrived.append(world.scheduler.send(ANA, ALPHA, "during"))

    run(world, act, settle=False)
    world.log.close()
    first, during = arrived
    again = Engine()
    restarted = worlds(again, create=False)
    restarted.scheduler.recover()
    assert restarted.log.state.messages[during].delivered is False

    async def nothing() -> None:
        return

    run(restarted, nothing)
    assert len(again.prompts) == 1
    assert during in again.prompts[0] and first not in again.prompts[0]
    assert "2 unread" in again.prompts[0]
    assert not restarted.message(first).read


# Request keys.


def call(tool: SdkMcpTool[Any], **arguments: Any) -> dict[str, Any]:  # noqa: ANN401
    async def handle() -> dict[str, Any]:
        return await tool.handler(arguments)

    return asyncio.run(handle())


def text_of(result: dict[str, Any]) -> str:
    return str(result["content"][0]["text"])


def sent_by(world: World, sender: LineAddress) -> list[MessageSent]:
    return [
        event
        for event in world.events
        if isinstance(event, MessageSent) and event.sender == sender
    ]


def test_a_retried_send_with_its_key_sends_once(worlds: Callable[..., World]) -> None:
    world = worlds(Engine())
    tool = world.scheduler.send_tool("alpha")
    first = call(tool, to="@ana", body="hello", request_key="k1")
    again = call(tool, to="@ana", body="hello", request_key="k1")
    assert len(sent_by(world, ALPHA)) == 1
    assert text_of(again) == text_of(first) + REPEATED
    assert not first.get("is_error") and not again.get("is_error")


def test_a_send_without_a_key_acts_each_time(worlds: Callable[..., World]) -> None:
    world = worlds(Engine())
    tool = world.scheduler.send_tool("alpha")
    call(tool, to="@ana", body="hello")
    call(tool, to="@ana", body="hello")
    assert len(sent_by(world, ALPHA)) == 2


def test_a_key_reused_for_other_arguments_is_refused(
    worlds: Callable[..., World],
) -> None:
    world = worlds(Engine())
    tool = world.scheduler.send_tool("alpha")
    call(tool, to="@ana", body="hello", request_key="k1")
    refused = call(tool, to="@ana", body="something else", request_key="k1")
    assert refused.get("is_error") is True
    assert "different send_message call" in text_of(refused)
    assert len(sent_by(world, ALPHA)) == 1


def test_a_refused_call_is_not_remembered(worlds: Callable[..., World]) -> None:
    world = worlds(Engine())
    tool = world.scheduler.send_tool("alpha")
    refused = call(tool, to="not an address!", body="hello", request_key="k1")
    assert refused.get("is_error") is True
    sent = call(tool, to="@ana", body="hello", request_key="k1")
    assert not sent.get("is_error")
    assert len(sent_by(world, ALPHA)) == 1


def test_keys_belong_to_their_party_and_tool(worlds: Callable[..., World]) -> None:
    world = worlds(Engine())
    call(world.scheduler.send_tool("alpha"), to="@ana", body="x", request_key="k")
    call(world.scheduler.send_tool("beta"), to="@ana", body="x", request_key="k")
    call(world.scheduler.ask_tool("alpha"), question="x", to="@ana", request_key="k")
    assert len(sent_by(world, ALPHA)) == 2
    assert len(sent_by(world, LineAddress("beta"))) == 1


def test_a_retried_question_with_its_key_is_asked_once(
    worlds: Callable[..., World],
) -> None:
    world = worlds(Engine())
    world.scheduler.send(ANA, ALPHA, "a message, so ana is the last operator")
    tool = world.scheduler.ask_tool("alpha")
    first = call(tool, question="May I?", choices=["yes", "no"], request_key="q")
    again = call(tool, question="May I?", choices=["yes", "no"], request_key="q")
    assert text_of(again) == text_of(first) + REPEATED
    assert len(sent_by(world, ALPHA)) == 1


def test_a_retried_wake_with_its_key_neither_moves_nor_doubles(
    worlds: Callable[..., World],
) -> None:
    world = worlds(Engine())
    tool = world.scheduler.wake_tool("alpha")
    first = call(tool, seconds=600, note="check", request_key="w")
    pending = world.log.state.scheduled["alpha"]
    again = call(tool, seconds=600, note="check", request_key="w")
    assert world.log.state.scheduled["alpha"] == pending
    assert sum(isinstance(e, WakeScheduled) for e in world.events) == 1
    assert text_of(again) == text_of(first) + REPEATED
    moved = call(tool, seconds=600, note="check", request_key="w2")
    assert not moved.get("is_error")
    assert sum(isinstance(e, WakeScheduled) for e in world.events) == 2


@pytest.mark.parametrize(
    "key", ["", "x" * (MAX_KEY_LENGTH + 1), "line\nbreak", 7, ["k"]]
)
def test_a_malformed_key_is_refused_before_anything_acts(
    worlds: Callable[..., World], key: object
) -> None:
    world = worlds(Engine())
    for tool, arguments in (
        (world.scheduler.send_tool("alpha"), {"to": "@ana", "body": "x"}),
        (world.scheduler.ask_tool("alpha"), {"question": "x", "to": "@ana"}),
        (world.scheduler.wake_tool("alpha"), {"seconds": 600, "note": "x"}),
    ):
        refused = call(tool, **arguments, request_key=key)
        assert refused.get("is_error") is True
        assert "request_key must be text" in text_of(refused)
    assert sent_by(world, ALPHA) == []
    assert "alpha" not in world.log.state.scheduled


def test_each_mutating_tool_offers_a_key() -> None:
    for schema in (SEND_TOOL_SCHEMA, ASK_TOOL_SCHEMA, WAKE_TOOL_SCHEMA):
        properties: Any = schema["properties"]
        assert REQUEST_KEY in properties
        assert REQUEST_KEY not in schema["required"]  # type: ignore[operator]


def test_the_oldest_key_is_forgotten_past_the_bound() -> None:
    keys = RequestKeys(limit=2)
    result = {"content": [{"type": "text", "text": "done"}]}
    for key in ("a", "b", "c"):
        keys.remember("alpha", "t", {"x": 1, REQUEST_KEY: key}, result)
    assert keys.earlier("alpha", "t", {"x": 1, REQUEST_KEY: "a"}) is None
    assert keys.earlier("alpha", "t", {"x": 1, REQUEST_KEY: "c"}) is not None
    assert keys.earlier("alpha", "t", {"x": 1}) is None
    with pytest.raises(ValueError, match="at least 1"):
        RequestKeys(limit=0)
    with pytest.raises(RequestKeyRefused):
        keys.earlier("alpha", "t", {"x": 2, REQUEST_KEY: "c"})


def test_arguments_are_compared_whatever_their_order() -> None:
    keys = RequestKeys()
    result = {"content": [{"type": "text", "text": "done"}]}
    keys.remember("alpha", "t", {"a": 1, "b": 2, REQUEST_KEY: "k"}, result)
    found = keys.earlier("alpha", "t", {REQUEST_KEY: "k", "b": 2, "a": 1})
    assert found is not None and found["content"][0]["text"] == "done" + REPEATED
    assert result["content"][0]["text"] == "done"


@pytest.mark.parametrize(
    "value", [object(), float("nan"), float("inf")], ids=["object", "nan", "inf"]
)
def test_arguments_json_cannot_hold_are_refused_before_the_call_acts(
    value: object,
) -> None:
    """Review of 2026-10-27, K3. Two objects with the same text once shared a
    digest, since values were written as their text."""
    keys = RequestKeys()
    with pytest.raises(RequestKeyRefused, match="must have JSON arguments"):
        keys.earlier("alpha", "t", {"x": value, REQUEST_KEY: "k"})
    assert keys.earlier("alpha", "t", {"x": value}) is None
