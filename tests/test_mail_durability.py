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
    MessageKind,
    MessageSent,
    OperatorAddress,
    QuestionStatus,
    RequestStamp,
    WakeScheduled,
)
from dromon import model as model_module
from dromon.codec import decode, encode
from dromon.requestkeys import (
    MAX_KEY_LENGTH,
    REPEATED,
    REQUEST_KEY,
    RequestKeyRefused,
    digest_of,
    stamp_of,
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


def test_the_oldest_key_is_forgotten_past_the_bound_before_and_after_replay(
    worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is the state's, so replay keeps the same keys the daemon kept."""
    monkeypatch.setattr(model_module, "MAX_REQUEST_KEYS_PER_PARTY", 2)
    world = worlds(Engine())
    tool = world.scheduler.send_tool("alpha")
    for key in ("a", "b", "c"):
        call(tool, to="@ana", body="x", request_key=key)
    kept = world.log.state.requests[("alpha", "send_message")]
    assert list(kept) == ["b", "c"]
    again = call(tool, to="@ana", body="x", request_key="a")
    assert REPEATED not in text_of(again)
    before = dict(world.log.state.requests)
    world.log.close()
    restarted = worlds(Engine(), create=False)
    assert restarted.log.state.requests == before
    assert list(restarted.log.state.requests[("alpha", "send_message")]) == [
        "c",
        "a",
    ]


def test_arguments_are_compared_whatever_their_order() -> None:
    assert digest_of({"a": 1, "b": 2, REQUEST_KEY: "k"}) == digest_of(
        {REQUEST_KEY: "other", "b": 2, "a": 1}
    )
    assert digest_of({"a": 1}) != digest_of({"a": 2})


@pytest.mark.parametrize(
    "value", [object(), float("nan"), float("inf")], ids=["object", "nan", "inf"]
)
def test_arguments_json_cannot_hold_are_refused_before_the_call_acts(
    value: object,
) -> None:
    """Review of 2026-10-27, K3. Two objects with the same text once shared a
    digest, since values were written as their text."""
    with pytest.raises(RequestKeyRefused, match="must have JSON arguments"):
        stamp_of({"x": value, REQUEST_KEY: "k"})
    assert stamp_of({"x": value}) is None


# Durable request keys, task of 2026-10-29, round 5. Each retry crosses a reload of
# the event log from disk, as a daemon restart does.


def reloaded(worlds: Callable[..., World], world: World) -> World:
    world.log.close()
    return worlds(Engine(), create=False)


def acts_of(world: World, sender: LineAddress) -> list[str]:
    return [
        message_id
        for message_id, message in world.log.state.messages.items()
        if message.sender == sender
    ]


def test_a_keyed_send_repeated_after_a_restart_sends_once(
    worlds: Callable[..., World],
) -> None:
    world = worlds(Engine())
    first = call(
        world.scheduler.send_tool("alpha"), to="@ana", body="hi", request_key="k"
    )
    restarted = reloaded(worlds, world)
    again = call(
        restarted.scheduler.send_tool("alpha"), to="@ana", body="hi", request_key="k"
    )
    assert text_of(again) == text_of(first) + REPEATED
    assert len(acts_of(restarted, ALPHA)) == 1
    assert restarted.events == []


def test_a_keyed_priority_send_repeats_its_priority_after_a_restart(
    worlds: Callable[..., World],
) -> None:
    world = worlds(Engine())
    arguments: dict[str, Any] = {
        "to": "beta",
        "body": "stop",
        "priority": True,
        "request_key": "p",
    }
    first = call(world.scheduler.send_tool("alpha"), **arguments)
    assert text_of(first).endswith(", with priority")
    restarted = reloaded(worlds, world)
    again = call(restarted.scheduler.send_tool("alpha"), **arguments)
    assert text_of(again) == text_of(first) + REPEATED


def test_a_keyed_question_repeated_after_a_restart_is_asked_once(
    worlds: Callable[..., World],
) -> None:
    world = worlds(Engine())
    arguments: dict[str, Any] = {
        "question": "May I?",
        "choices": ["yes", "no"],
        "to": "@ana",
        "request_key": "q",
    }
    first = call(world.scheduler.ask_tool("alpha"), **arguments)
    restarted = reloaded(worlds, world)
    again = call(restarted.scheduler.ask_tool("alpha"), **arguments)
    assert text_of(again) == text_of(first) + REPEATED
    (question,) = acts_of(restarted, ALPHA)
    assert restarted.log.state.messages[question].kind is MessageKind.QUESTION


def test_a_keyed_wake_repeated_after_a_restart_neither_moves_nor_doubles(
    worlds: Callable[..., World],
) -> None:
    world = worlds(Engine())
    first = call(
        world.scheduler.wake_tool("alpha"), seconds=600, note="check", request_key="w"
    )
    pending = world.log.state.scheduled["alpha"]
    restarted = reloaded(worlds, world)
    again = call(
        restarted.scheduler.wake_tool("alpha"),
        seconds=600,
        note="check",
        request_key="w",
    )
    assert text_of(again) == text_of(first) + REPEATED
    assert restarted.log.state.scheduled["alpha"] == pending
    assert restarted.events == []


def test_a_repeated_wake_leaves_out_the_wake_it_replaced(
    worlds: Callable[..., World],
) -> None:
    """The documented difference. ``wake_scheduled`` does not record the wake it
    replaced, so the rebuilt text has no sentence on it."""
    world = worlds(Engine())
    tool = world.scheduler.wake_tool("alpha")
    call(tool, seconds=300, note="earlier")
    first = call(tool, seconds=600, note="check", request_key="w")
    assert " It replaces the wake that was due at " in text_of(first)
    again = call(tool, seconds=600, note="check", request_key="w")
    shown = text_of(first).split(" It replaces the wake")[0]
    assert text_of(again) == shown + REPEATED


@pytest.mark.parametrize(
    ("tool_name", "first", "other"),
    [
        (
            "send_tool",
            {"to": "@ana", "body": "hi"},
            {"to": "@ana", "body": "something else"},
        ),
        (
            "ask_tool",
            {"question": "May I?", "to": "@ana"},
            {"question": "May I not?", "to": "@ana"},
        ),
        ("wake_tool", {"seconds": 600, "note": "x"}, {"seconds": 900, "note": "x"}),
    ],
)
def test_a_key_reused_for_other_arguments_after_a_restart_is_refused(
    worlds: Callable[..., World],
    tool_name: str,
    first: dict[str, Any],
    other: dict[str, Any],
) -> None:
    world = worlds(Engine())
    call(getattr(world.scheduler, tool_name)("alpha"), **first, request_key="k")
    restarted = reloaded(worlds, world)
    refused = call(
        getattr(restarted.scheduler, tool_name)("alpha"), **other, request_key="k"
    )
    assert refused.get("is_error") is True
    assert "was used for a different" in text_of(refused)
    assert restarted.events == []


def test_a_log_written_without_keys_loads_and_keeps_no_keys(
    worlds: Callable[..., World],
) -> None:
    """An unkeyed act is written as before the field existed, so an older daemon
    reads it, and an event of an older daemon decodes with no key."""
    world = worlds(Engine())
    call(world.scheduler.send_tool("alpha"), to="@ana", body="hi")
    call(world.scheduler.wake_tool("alpha"), seconds=600, note="x")
    for event in world.events:
        if isinstance(event, MessageSent | WakeScheduled):
            assert '"request"' not in encode(event)
            assert decode(encode(event)) == event
    restarted = reloaded(worlds, world)
    assert restarted.log.state.requests == {}
    assert len(acts_of(restarted, ALPHA)) == 1


def test_a_keyed_act_carries_its_stamp_in_its_own_event(
    worlds: Callable[..., World],
) -> None:
    """The key is recorded atomically with the act, in the one event appended."""
    world = worlds(Engine())
    arguments: dict[str, Any] = {"to": "@ana", "body": "hi", "request_key": "k"}
    call(world.scheduler.send_tool("alpha"), **arguments)
    (sent,) = sent_by(world, ALPHA)
    assert sent.request == RequestStamp("k", digest_of(arguments))
    assert decode(encode(sent)) == sent


def test_the_model_refuses_a_kept_key_and_a_harness_key(
    worlds: Callable[..., World],
) -> None:
    world = worlds(Engine())
    stamp = RequestStamp("k", "a" * 64)
    world.log.append(
        MessageSent("m-a", ALPHA, ANA, MessageKind.NOTE, "hi", request=stamp)
    )
    with pytest.raises(model_module.EventRejected, match="acted before"):
        world.log.append(
            MessageSent("m-b", ALPHA, ANA, MessageKind.NOTE, "hi", request=stamp)
        )
    # The same key on another tool of the same party is its own.
    world.log.append(
        MessageSent("q-c", ALPHA, ANA, MessageKind.QUESTION, "May I?", request=stamp)
    )
    with pytest.raises(model_module.EventRejected, match="gives no request key"):
        world.log.append(
            MessageSent(
                "m-d", model_module.HARNESS, ALPHA, MessageKind.NOTE, "x", request=stamp
            )
        )


@pytest.mark.parametrize(
    ("key", "digest"),
    [("", "a" * 64), ("x" * (MAX_KEY_LENGTH + 1), "a" * 64), ("k", "nope")],
)
def test_a_malformed_stamp_is_invalid(key: str, digest: str) -> None:
    with pytest.raises(model_module.InvalidField):
        RequestStamp(key, digest)
