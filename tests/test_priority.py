"""Wake hibernate, priority mail and self mail, driven by scripted engines.

The scripted engine asks ``LineSpec.redirect`` at each of its safe points, as a line's
``SteeredDriver`` does once a tool result is delivered or a message without a tool use
ends, and ends its turn with the
reason it is given, as every adapter does when its driver answers ``interrupt``. The
steered driver itself runs here on the real packaged driver.
"""

import asyncio
from collections.abc import Callable, Coroutine, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import priority
from dromon.activity import ActivityKind
from dromon.driver import (
    MESSAGE_END,
    MODELS,
    TOOL_RESULT,
    ActionTag,
    Driver,
    Event,
    Strategy,
)
from dromon.engine import BackgroundEnd, TurnReport
from dromon.eventlog import EventLog, read_entries
from dromon.limits import DEFAULT_LIMITS, Limits
from dromon.lineconfig import LineConfig
from dromon.model import (
    AgentAccess,
    AgentAddress,
    AgentRegistered,
    EventRejected,
    LineAddress,
    LineHibernated,
    LineAwakened,
    MessagePrioritized,
    OperatorAddress,
)
from dromon.priority import PriorityLimiter, PriorityRefused, SteeredDriver
from dromon.scheduler import LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
BETA = LineAddress("beta")
START = datetime(2026, 10, 27, 9, 0, 0, tzinfo=UTC)

type Turn = list[dict[str, Any]]


class Steerable:
    """A line engine that makes scripted tool calls, may wait at a gate, and then
    reaches ``safe_points`` safe points, at each of which it asks to be redirected."""

    def __init__(
        self,
        spec: LineSpec,
        tools: Sequence[SdkMcpTool[Any]],
        turns: list[Turn],
        gates: list[asyncio.Event],
        safe_points: int,
    ) -> None:
        self.spec = spec
        self.session_id: str | None = None
        self.tools = {tool.name: tool for tool in tools}
        self.turns = turns
        self.gates = gates
        self.safe_points = safe_points
        self.prompts: list[str] = []
        self.results: list[dict[str, Any]] = []
        self.reasons: list[str | None] = []

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        calls = self.turns.pop(0) if self.turns else []
        for call in calls:
            args = {key: value for key, value in call.items() if key != "tool"}
            tool = self.tools[str(call.get("tool", "send_message"))]
            self.results.append(await tool.handler(args))
        if self.gates:
            await self.gates.pop(0).wait()
        reason: str | None = None
        for _ in range(self.safe_points):
            reason = self.spec.redirect()
            if reason is not None:
                break
        self.reasons.append(reason)
        subtype = "success" if reason is None else "error_during_execution"
        return TurnReport(subtype, 1, "ok", (), reason, None, "session-1", None, None)


class World:
    """A scheduler over a real event log, with one steerable engine per line."""

    def __init__(
        self,
        path: Path,
        lines: Sequence[str],
        *,
        limits: Limits = DEFAULT_LIMITS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = path
        self.log = EventLog(path)
        self.now = START
        self.scripts: dict[str, list[Turn]] = {line_id: [] for line_id in lines}
        self.gates: dict[str, list[asyncio.Event]] = {line_id: [] for line_id in lines}
        self.safe_points: dict[str, int] = dict.fromkeys(lines, 1)
        self.engines: dict[str, Steerable] = {}
        self.scheduler = Scheduler(
            self.log,
            self.build,
            limits=limits,
            tick_seconds=0,
            clock=clock or (lambda: 0.0),
            wall_clock=lambda: self.now,
        )
        self.scheduler.ensure_operator("ana")
        for line_id in lines:
            self.scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Steerable:
        line_id = spec.line_id
        engine = Steerable(
            spec,
            tools,
            self.scripts[line_id],
            self.gates[line_id],
            self.safe_points[line_id],
        )
        self.engines[line_id] = engine
        return engine

    def prompts(self, line_id: str) -> list[str]:
        engine = self.engines.get(line_id)
        return [] if engine is None else engine.prompts


type MakeWorld = Callable[..., World]


@pytest.fixture
def make_world(tmp_path: Path) -> Iterator[MakeWorld]:
    built: list[World] = []

    def make(
        lines: Sequence[str],
        *,
        limits: Limits = DEFAULT_LIMITS,
        clock: Callable[[], float] | None = None,
    ) -> World:
        path = tmp_path / f"events-{len(built)}.sqlite"
        target = World(path, lines, limits=limits, clock=clock)
        built.append(target)
        return target

    yield make
    for target in built:
        target.log.close()


async def until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until ``condition`` holds, within a bounded number of
    passes, so a test that would hang fails instead."""
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never held")


def run(target: World, act: Callable[[], Coroutine[Any, Any, None]]) -> None:
    async def settle() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(target.scheduler.run(stop))
        await asyncio.sleep(0)
        await act()
        await target.scheduler.idle()
        stop.set()
        await loop

    asyncio.run(settle())


async def call(tool: SdkMcpTool[Any], args: dict[str, Any]) -> dict[str, Any]:
    return await tool.handler(args)


def turn_endings(target: World, line_id: str) -> list[str]:
    return [
        entry.text
        for entry in target.scheduler.activity.after(line_id, 0)
        if entry.kind is ActivityKind.TURN_ENDED
    ]


# The steered driver, on the real packaged driver.


class Steer:
    """A steer that records each time it is asked, and answers from a script."""

    def __init__(self, *answers: str | None) -> None:
        self.answers = list(answers)
        self.asked = 0

    def __call__(self) -> str | None:
        self.asked += 1
        return self.answers.pop(0) if self.answers else "priority mail m-1 from @ana"


def steps(steer: Steer, events: Sequence[Event]) -> list[ActionTag]:
    """The steered driver's action tags for ``events``, after checking that its state
    advances exactly as the plain driver's does."""
    steered = priority.steered(HAIKU, Strategy.SUBSCRIPTION, steer)
    plain = Driver.for_model(HAIKU, Strategy.SUBSCRIPTION)
    tags = [steered.step(event).tag for event in events]
    for event in events:
        plain.step(event)
    assert steered.state == plain.state
    return tags


def test_text_and_thinking_are_never_safe_points() -> None:
    # Review finding 1. A text block can share a message with a tool use after it.
    steer = Steer()
    tags = steps(
        steer,
        [Event("turn_start"), Event("thinking"), Event("text", "", 5), Event("text")],
    )
    assert ActionTag.INTERRUPT not in tags
    assert steer.asked == 0


def test_text_before_a_tool_use_in_one_message_is_not_a_safe_point() -> None:
    steer = Steer()
    tags = steps(
        steer,
        [
            Event("turn_start"),
            Event("text", "", 5),
            Event("tool_use", "Read"),
            Event(TOOL_RESULT),
        ],
    )
    assert tags == [ActionTag.CONTINUE] * 3 + [ActionTag.INTERRUPT]
    assert steer.asked == 1


def test_a_tool_result_is_safe_only_once_every_tool_use_has_its_result() -> None:
    steer = Steer()
    tags = steps(
        steer,
        [
            Event("turn_start"),
            Event("tool_use", "Read"),
            Event("tool_use", "Grep"),
            Event(TOOL_RESULT),
            Event(TOOL_RESULT),
        ],
    )
    assert tags[3] is ActionTag.CONTINUE
    assert tags[4] is ActionTag.INTERRUPT
    assert steer.asked == 1


def test_the_end_of_a_message_is_safe_only_without_a_tool_use() -> None:
    steer = Steer(None)
    tags = steps(
        steer,
        [
            Event("turn_start"),
            Event(MESSAGE_END, "", 1),
            Event("tool_use", "Read"),
            Event(MESSAGE_END, "", 0),
            Event(TOOL_RESULT),
            Event(MESSAGE_END, "", 0),
        ],
    )
    # A message with a tool use, and a message's end while a tool runs, are not
    # asked. The result is asked and declines, and the toolless end then interrupts.
    assert tags[:4] == [ActionTag.CONTINUE] * 4
    assert tags[4] is ActionTag.CONTINUE
    assert tags[5] is ActionTag.INTERRUPT
    assert steer.asked == 2


def test_an_unannounced_result_cannot_make_a_later_tool_look_finished() -> None:
    steer = Steer(None)
    steered = priority.steered(HAIKU, Strategy.SUBSCRIPTION, steer)
    steered.step(Event("turn_start"))
    steered.step(Event(TOOL_RESULT))
    assert steered.pending == 0
    steered.step(Event("tool_use", "Bash"))
    assert steered.pending == 1
    assert steered.step(Event(MESSAGE_END, "", 0)).tag is ActionTag.CONTINUE


def test_a_new_turn_forgets_a_tool_use_whose_result_never_came() -> None:
    steer = Steer()
    steered = priority.steered(HAIKU, Strategy.SUBSCRIPTION, steer)
    steered.step(Event("turn_start"))
    steered.step(Event("tool_use", "Bash"))
    assert steered.step(Event(MESSAGE_END, "", 0)).tag is ActionTag.CONTINUE
    steered.step(Event("turn_start"))
    assert steered.step(Event(MESSAGE_END, "", 0)).tag is ActionTag.INTERRUPT


def test_the_driver_s_own_interrupt_is_kept_and_the_steer_is_not_asked() -> None:
    steer = Steer()
    steered = priority.steered(HAIKU, Strategy.SUBSCRIPTION, steer)
    steered.step(Event("turn_start"))
    budget = steered.settings.tool_budget
    for _ in range(budget):
        steered.step(Event("tool_use", "Read"))
        steered.step(Event(TOOL_RESULT))
    asked = steer.asked
    over = steered.step(Event("tool_use", "Read"))
    assert over.tag is ActionTag.INTERRUPT and over.text == "tool_budget"
    assert steer.asked == asked


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("strategy", list(Strategy))
def test_every_packaged_driver_continues_at_both_new_events(
    model: str, strategy: Strategy
) -> None:
    plain = Driver.for_model(model, strategy)
    plain.step(Event("turn_start"))
    before = plain.state
    for event in (Event(TOOL_RESULT), Event(MESSAGE_END, "", 0)):
        assert plain.step(event).tag is ActionTag.CONTINUE
    assert plain.state == before


def test_the_steered_driver_without_a_steer_is_the_plain_driver() -> None:
    steered = SteeredDriver.for_model(HAIKU, Strategy.SUBSCRIPTION)
    plain = Driver.for_model(HAIKU, Strategy.SUBSCRIPTION)
    events = (
        Event("text", "", 1),
        Event("thinking"),
        Event("tool_use", "Bash"),
        Event(TOOL_RESULT),
        Event(MESSAGE_END, "", 0),
    )
    for event in events:
        assert steered.step(event) == plain.step(event)


# The rate limit.


def test_lines_priority_mail_is_limited_by_sender_and_by_recipient() -> None:
    now = [0.0]
    limiter = PriorityLimiter(lambda: now[0])
    for _ in range(priority.MAX_PER_RECIPIENT):
        limiter.check("alpha", "beta")
        limiter.record("alpha", "beta")
    with pytest.raises(PriorityRefused, match="line beta received 2 priority"):
        limiter.check("alpha", "beta")
    # The sender has room for one more, to another line.
    limiter.check("alpha", "gamma")
    limiter.record("alpha", "gamma")
    with pytest.raises(PriorityRefused, match="line alpha sent 3 priority") as caught:
        limiter.check("alpha", "delta")
    assert f"again in {int(priority.WINDOW_SECONDS)} seconds" in str(caught.value)
    # Another sender to another recipient is unaffected.
    limiter.check("gamma", "delta")
    now[0] = priority.WINDOW_SECONDS
    limiter.check("alpha", "beta")


# Self mail.


def test_a_line_may_send_itself_a_note_that_wakes_its_next_turn(
    make_world: MakeWorld,
) -> None:
    target = make_world(["alpha"])
    target.scripts["alpha"].append([{"to": "alpha", "body": "check the log"}])

    async def act() -> None:
        target.scheduler.send(ANA, ALPHA, "start")

    run(target, act)
    engine = target.engines["alpha"]
    assert str(engine.results[0]["content"][0]["text"]).startswith("sent as m-")
    assert len(engine.prompts) == 2
    own = [
        (message_id, message)
        for message_id, message in target.log.state.messages.items()
        if message.sender == ALPHA and message.recipient == ALPHA
    ]
    assert len(own) == 1 and own[0][1].delivered
    assert f"{own[0][0]} from alpha" in engine.prompts[1]
    # A note to itself is not operator mail, so its turn counts as autonomous.
    turns = target.scheduler.line_status("alpha")["autonomous_turns"]
    assert isinstance(turns, dict) and turns["used"] == 1


def test_a_self_note_with_the_pending_wake_s_text_is_no_firing(
    make_world: MakeWorld,
) -> None:
    """Review finding 3 of 2026-10-27. A line cannot fire its own scheduled wake
    early by sending itself the wake's note, nor send under the reserved identifier,
    since the send tool takes none."""
    target = make_world(["alpha"])
    reserved, _ = target.scheduler.schedule_wake("alpha", 3600, "look again")
    target.scripts["alpha"].append(
        [
            {"to": "alpha", "body": "look again"},
            {"to": "alpha", "body": "look again", "message_id": reserved},
        ]
    )

    async def act() -> None:
        target.scheduler.send(ANA, ALPHA, "start")

    run(target, act)
    state = target.log.state
    assert state.scheduled["alpha"].message_id == reserved
    assert reserved not in state.messages and reserved not in state.fired_dues
    own = [
        message_id
        for message_id, message in state.messages.items()
        if message.sender == ALPHA and message.recipient == ALPHA
    ]
    assert len(own) == 1 and own[0] != reserved
    assert not set(own) & set(state.fired_dues)
    engine = target.engines["alpha"]
    assert engine.results[1] == {
        "content": [{"type": "text", "text": "unexpected argument message_id"}],
        "is_error": True,
    }
    assert "your scheduled wake" not in engine.prompts[1]


# Hibernate.


def test_a_hibernated_line_keeps_its_mail_and_runs_no_turn_until_awakened(
    make_world: MakeWorld,
) -> None:
    target = make_world(["alpha"])
    assert target.scheduler.hibernate("alpha") is True
    assert target.scheduler.hibernate("alpha") is False

    async def act() -> None:
        target.scheduler.send(ANA, ALPHA, "work")

    run(target, act)
    assert target.prompts("alpha") == []
    status = target.scheduler.line_status("alpha")
    assert status["hibernated"] is True
    assert status["condition"] == "hibernated"
    assert status["pending"] == 1
    summary = target.scheduler.line_summaries()[0]
    assert summary["hibernated"] is True and summary["condition"] == "hibernated"

    async def awaken() -> None:
        assert target.scheduler.awaken("alpha") is True
        assert target.scheduler.awaken("alpha") is False

    run(target, awaken)
    assert len(target.prompts("alpha")) == 1
    assert target.scheduler.line_status("alpha")["hibernated"] is False
    events = [entry.event for entry in read_entries(target.path)]
    assert [
        type(e) for e in events if isinstance(e, LineHibernated | LineAwakened)
    ] == [
        LineHibernated,
        LineAwakened,
    ]


def test_hibernate_refuses_an_unknown_line(make_world: MakeWorld) -> None:
    target = make_world(["alpha"])
    with pytest.raises(EventRejected, match="no line nobody"):
        target.scheduler.hibernate("nobody")
    with pytest.raises(EventRejected, match="no line nobody"):
        target.scheduler.awaken("nobody")


def test_a_hibernated_line_keeps_its_scheduled_wake_pending_until_awakened(
    make_world: MakeWorld,
) -> None:
    target = make_world(["alpha"])
    message_id, _ = target.scheduler.schedule_wake("alpha", 60, "look again")
    target.scheduler.hibernate("alpha")
    target.now = START + timedelta(seconds=120)

    async def act() -> None:
        pass

    run(target, act)
    assert target.scheduler.seconds_until_next_wake() is None
    assert target.log.state.scheduled["alpha"].message_id == message_id
    assert message_id not in target.log.state.messages
    assert target.prompts("alpha") == []

    async def awaken() -> None:
        target.scheduler.awaken("alpha")

    run(target, awaken)
    assert "alpha" not in target.log.state.scheduled
    assert len(target.prompts("alpha")) == 1
    assert "your scheduled wake, due" in target.prompts("alpha")[0]


def held_while_hibernated(target: World, wake: Callable[[], object]) -> list[str]:
    """Hibernate alpha, run ``wake``, check that no turn ran, then awaken and return the
    prompts of the turns that followed. Review finding 2 of 2026-10-27."""
    target.scheduler.hibernate("alpha")

    async def act() -> None:
        wake()

    run(target, act)
    assert target.prompts("alpha") == []
    assert target.scheduler.condition("alpha") == "hibernated"

    async def awaken() -> None:
        target.scheduler.awaken("alpha")

    run(target, awaken)
    return target.prompts("alpha")


def test_a_hibernated_line_runs_no_turn_for_a_question_answer(
    make_world: MakeWorld,
) -> None:
    target = make_world(["alpha"])
    question = target.scheduler.ask("alpha", ANA, "Which file?", ("a.md", "b.md"))
    answers: list[str] = []
    prompts = held_while_hibernated(
        target, lambda: answers.append(target.scheduler.answer(ANA, question, "b.md"))
    )
    assert len(prompts) == 1 and answers[0] in prompts[0]


def test_a_hibernated_line_runs_no_turn_for_a_ceiling_answer(
    make_world: MakeWorld,
) -> None:
    target = make_world(["alpha"])
    question = target.scheduler.ceiling_reached("alpha", 2)
    assert question is not None
    asked = target.log.state.messages[question]
    assert isinstance(asked.recipient, OperatorAddress)
    operator = asked.recipient
    answers: list[str] = []
    prompts = held_while_hibernated(
        target,
        lambda: answers.append(
            target.scheduler.answer(operator, question, asked.choices[0])
        ),
    )
    assert len(prompts) == 1 and answers[0] in prompts[0]


def test_a_hibernated_line_keeps_its_compaction_wake_pending_until_awakened(
    make_world: MakeWorld,
) -> None:
    target = make_world(["alpha"])
    target.scheduler.configure_line("alpha", LineConfig(allow_compact=True))
    target.scheduler._context["alpha"] = (  # pyright: ignore[reportPrivateUsage]
        target.scheduler.effective_config("alpha").context_warn_tokens
    )
    tool = target.scheduler.compact_session_tool("alpha")
    asyncio.run(call(tool, {"seconds": 60}))
    reserved = target.log.state.scheduled["alpha"].message_id
    target.scheduler._context.pop("alpha", None)  # pyright: ignore[reportPrivateUsage]

    def due() -> None:
        target.now = START + timedelta(seconds=120)

    prompts = held_while_hibernated(target, due)
    assert reserved in target.log.state.messages
    assert len(prompts) == 1 and "your scheduled wake, due" in prompts[0]


def test_a_hibernated_line_runs_no_turn_for_a_task_note(make_world: MakeWorld) -> None:
    target = make_world(["alpha"])
    target.scheduler.hibernate("alpha")

    async def act() -> None:
        noted = target.scheduler.background_ended(
            "alpha", BackgroundEnd("b1", "make test", "completed")
        )
        assert noted is not None

    run(target, act)
    assert target.prompts("alpha") == []
    assert target.scheduler.line_status("alpha")["pending"] == 1


def test_a_turn_running_when_hibernated_finishes_with_its_tools_and_no_turn_follows(
    make_world: MakeWorld,
) -> None:
    target = make_world(["alpha", "beta"])
    gate = asyncio.Event()
    target.gates["alpha"].append(gate)
    target.scripts["alpha"].append([])

    async def act() -> None:
        target.scheduler.send(ANA, ALPHA, "work")
        await until(lambda: len(target.prompts("alpha")) == 1)
        target.scheduler.hibernate("alpha")
        assert target.scheduler.condition("alpha") == "running"
        # The hibernated line's own tools still work in the turn it is running.
        tool = target.scheduler.send_tool("alpha")
        result = await tool.handler({"to": "beta", "body": "from a hibernated line"})
        assert str(result["content"][0]["text"]).startswith("sent as m-")
        target.scheduler.send(ANA, ALPHA, "more work")
        gate.set()

    run(target, act)
    assert len(target.prompts("alpha")) == 1
    assert turn_endings(target, "alpha") == ["success"]
    assert target.scheduler.line_status("alpha")["pending"] == 1
    # The mail it sent woke its recipient as any mail does.
    assert len(target.prompts("beta")) == 1


# Priority mail.


def test_operator_priority_mail_ends_a_running_turn_and_leads_the_next(
    make_world: MakeWorld,
) -> None:
    target = make_world(["alpha"])
    gate = asyncio.Event()
    target.gates["alpha"].append(gate)
    sent: list[str] = []

    async def act() -> None:
        target.scheduler.send(ANA, ALPHA, "long work")
        await until(lambda: len(target.prompts("alpha")) == 1)
        sent.append(target.scheduler.send(ANA, ALPHA, "ordinary"))
        sent.append(target.scheduler.send(ANA, ALPHA, "stop that", priority=True))
        gate.set()

    run(target, act)
    ordinary, urgent = sent
    engine = target.engines["alpha"]
    reason = f"priority mail {urgent} from @ana"
    assert engine.reasons == [reason, None]
    assert turn_endings(target, "alpha") == [
        f"error_during_execution, ended at a safe point for {reason}",
        "success",
    ]
    second = engine.prompts[1]
    assert f"{urgent} from @ana" in second
    assert "priority, which ended your last turn at a safe point" in second
    # The priority note leads the delivery, ahead of the note sent before it.
    assert second.index(urgent) < second.index(ordinary)
    message = target.log.state.messages[urgent]
    assert message.priority and message.delivered
    events = [entry.event for entry in read_entries(target.path)]
    assert MessagePrioritized(urgent) in events
    # Live check item 5. The recent turn says why it was interrupted, since its
    # engine subtype reads as an error.
    status = target.scheduler.line_status("alpha")
    turns = cast(list[dict[str, object]], status["recent_turns"])
    assert [(turn["outcome"], turn["interrupted_for"]) for turn in turns] == [
        ("interrupted", reason),
        ("completed", None),
    ]


def test_priority_mail_leads_the_delivery(make_world: MakeWorld) -> None:
    target = make_world(["alpha"])
    sent: list[str] = []

    async def act() -> None:
        sent.append(target.scheduler.send(ANA, ALPHA, "first"))
        sent.append(target.scheduler.send(ANA, ALPHA, "second", priority=True))

    run(target, act)
    (prompt,) = target.prompts("alpha")
    first, urgent = sent
    assert prompt.index(urgent) < prompt.index(first)
    assert f"{urgent} from @ana, note, unread, priority" in prompt


def test_priority_mail_a_turn_never_reaches_a_safe_point_for_leads_the_next(
    make_world: MakeWorld,
) -> None:
    target = make_world(["alpha"])
    target.safe_points["alpha"] = 0
    gate = asyncio.Event()
    target.gates["alpha"].append(gate)
    sent: list[str] = []

    async def act() -> None:
        target.scheduler.send(ANA, ALPHA, "one long tool call")
        await until(lambda: len(target.prompts("alpha")) == 1)
        sent.append(target.scheduler.send(ANA, ALPHA, "redirect", priority=True))
        gate.set()

    run(target, act)
    assert turn_endings(target, "alpha") == ["success", "success"]
    second = target.prompts("alpha")[1]
    assert f"{sent[0]} from @ana, note, unread, priority" in second
    assert "ended your last turn" not in second


def test_priority_mail_starts_a_turn_without_waiting_for_the_cooldown(
    make_world: MakeWorld,
) -> None:
    limits = Limits(
        max_autonomous_turns=DEFAULT_LIMITS.max_autonomous_turns,
        max_cost_usd=DEFAULT_LIMITS.max_cost_usd,
        cooldown_seconds=3600,
    )
    target = make_world(["alpha"], limits=limits)

    async def first() -> None:
        target.scheduler.send(ANA, ALPHA, "one")

    run(target, first)

    async def ordinary() -> None:
        target.scheduler.send(ANA, ALPHA, "two")
        for _ in range(20):
            await asyncio.sleep(0)

    asyncio.run(_briefly(target, ordinary))
    assert len(target.prompts("alpha")) == 1

    async def urgent() -> None:
        target.scheduler.send(ANA, ALPHA, "three", priority=True)

    run(target, urgent)
    assert len(target.prompts("alpha")) == 2


async def _briefly(target: World, act: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run the scheduler only while ``act`` runs, for a line in its cooldown, which
    keeps the loop ticking and so never lets ``idle`` return."""
    stop = asyncio.Event()
    loop = asyncio.create_task(target.scheduler.run(stop))
    await asyncio.sleep(0)
    await act()
    stop.set()
    await loop


def test_priority_mail_does_not_end_a_hibernated_lines_turn(
    make_world: MakeWorld,
) -> None:
    target = make_world(["alpha"])
    gate = asyncio.Event()
    target.gates["alpha"].append(gate)

    async def act() -> None:
        target.scheduler.send(ANA, ALPHA, "work")
        await until(lambda: len(target.prompts("alpha")) == 1)
        target.scheduler.hibernate("alpha")
        target.scheduler.send(ANA, ALPHA, "redirect", priority=True)
        gate.set()

    run(target, act)
    assert target.engines["alpha"].reasons == [None]
    assert len(target.prompts("alpha")) == 1
    assert target.scheduler.line_status("alpha")["pending"] == 1


def test_lines_priority_mail_is_rate_limited_and_a_refusal_records_nothing(
    make_world: MakeWorld,
) -> None:
    target = make_world(["alpha", "beta"])
    tool = target.scheduler.send_tool("alpha")
    target.scheduler.hibernate("beta")

    async def calls() -> list[dict[str, Any]]:
        args = {"to": "beta", "body": "now", "priority": True}
        return [await tool.handler(dict(args)) for _ in range(3)]

    results = asyncio.run(calls())
    assert [result.get("is_error", False) for result in results] == [
        False,
        False,
        True,
    ]
    assert str(results[0]["content"][0]["text"]).endswith(", with priority")
    assert "Send it without priority" in str(results[2]["content"][0]["text"])
    marked = [m for m in target.log.state.messages.values() if m.priority]
    assert len(marked) == priority.MAX_PER_RECIPIENT
    # The line can still send the note without priority.
    plain = asyncio.run(call(tool, {"to": "beta", "body": "now"}))
    assert plain.get("is_error", False) is False


def test_operator_priority_mail_is_unlimited(make_world: MakeWorld) -> None:
    target = make_world(["alpha"])
    target.scheduler.hibernate("alpha")
    count = priority.MAX_PER_SENDER + priority.MAX_PER_RECIPIENT + 1
    for index in range(count):
        target.scheduler.send(ANA, ALPHA, f"now {index}", priority=True)
    assert sum(1 for m in target.log.state.messages.values() if m.priority) == count


def test_an_external_agent_may_not_send_priority_mail(make_world: MakeWorld) -> None:
    target = make_world(["alpha"])
    target.log.append(AgentRegistered("probe", AgentAccess.MESSAGE, ""))
    before = len(target.log.state.messages)
    result = asyncio.run(
        call(
            target.scheduler.send_tool(AgentAddress("probe")),
            {"to": "alpha", "body": "now", "priority": True},
        )
    )
    assert result["is_error"] is True
    assert "external agent may not send priority mail" in str(
        result["content"][0]["text"]
    )
    assert len(target.log.state.messages) == before


@pytest.mark.parametrize(
    ("recipient", "reason"),
    [
        (OperatorAddress("ana"), "goes to a line"),
        (LineAddress("nobody"), "no line nobody"),
    ],
)
def test_priority_mail_must_go_to_an_existing_line(
    make_world: MakeWorld, recipient: OperatorAddress | LineAddress, reason: str
) -> None:
    target = make_world(["alpha"])
    before = len(target.log.state.messages)
    with pytest.raises(EventRejected, match=reason):
        target.scheduler.send(ALPHA, recipient, "now", priority=True)
    assert len(target.log.state.messages) == before
