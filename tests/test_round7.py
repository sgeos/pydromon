"""Round 7 of the session-line experience: a compaction withdraws the wake
``compact_session`` scheduled for it, a turn outside Dromon's own says whether the
engine reported zero or nothing, the context note names the compaction as its source,
and a Codex compaction whose size was not reported is estimated from the first call
after it."""

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Callable, Generator, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.codec import DecodeError, decode, encode
from dromon.codex import (
    COMPACT_START,
    COMPACTION_ITEM,
    THREAD_COMPACTED,
    TOKEN_USAGE_UPDATED,
    CodexLine,
    Connection,
)
from dromon.driver import Driver, Strategy
from dromon.engine import INTERRUPTED_KIND, SELF_STARTED_KIND, TurnReport
from dromon.eventlog import EventLog, read_entries
from dromon.limits import Limits
from dromon.lineconfig import config_from_document
from dromon.model import (
    EventRejected,
    InvalidField,
    LineAddress,
    MessageSent,
    OperatorAddress,
    State,
    WakeScheduled,
    WakeWithdrawn,
)
from dromon.scheduler import (
    AFTER_ESTIMATE_NOTE,
    COMPACT_WAKE_NOTE,
    COMPACTION_CONTEXT_WARNING,
    CONTEXT_FROM_CALL,
    CONTEXT_FROM_COMPACTION,
    CONTEXT_FROM_ESTIMATE,
    CONTEXT_NOTES,
    LineSpec,
    Scheduler,
    engine_factory,
)

HAIKU = "claude-haiku-5-5"
LUNA = "gpt-6-luna"
ANA = OperatorAddress("ana")
START = datetime(2026, 10, 26, 9, 0, 0, tzinfo=UTC)
LIMITS: Final = Limits(max_autonomous_turns=20, max_cost_usd=1.0)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class Compacting:
    """An engine that compacts as asked, to ``world.after`` tokens. While
    ``world.gate`` is set, the compaction waits for it, so a test can act while the
    turn is compacting. With ``world.silent`` it is asked and reports nothing."""

    def __init__(self, spec: LineSpec, world: "World") -> None:
        self.spec = spec
        self.world = world
        self.session_id = spec.session_id
        self.prompts: list[str] = []
        self.reasons: list[str] = []
        self.pending = False

    def request_compaction(
        self, reason: str, context_line: Callable[[], str | None]
    ) -> None:
        del context_line
        self.reasons.append(reason)
        self.pending = True

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        if self.pending:
            self.pending = False
            if self.world.gate is not None:
                self.world.compacting.set()
                await self.world.gate.wait()
            if not self.world.silent:
                self.spec.compacted(
                    {
                        "trigger": "manual",
                        "before_tokens": self.world.context,
                        "after_tokens": self.world.after,
                    }
                )
        return TurnReport(
            "success",
            1,
            "ok",
            (),
            None,
            None,
            "session-1",
            None,
            0.125,
            context_tokens=self.world.context,
        )


class World:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.log = EventLog(path)
        self.clock = Clock()
        self.context: int | None = None
        self.after: int | None = 5_000
        self.silent = False
        self.gate: asyncio.Event | None = None
        self.compacting = asyncio.Event()
        self.engines: dict[str, Compacting] = {}
        self.scheduler = self.start()
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        self.compactable()

    def start(self) -> Scheduler:
        """A scheduler on this world's log, as a daemon's restart builds one."""
        return Scheduler(
            self.log, self.build, tick_seconds=0, wall_clock=self.clock, limits=LIMITS
        )

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Compacting:
        del tools
        engine = Compacting(spec, self)
        self.engines[spec.line_id] = engine
        return engine

    def compactable(self, **keys: object) -> None:
        self.scheduler.configure_line(
            "alpha", config_from_document({"allow_compact": True, **keys})
        )

    @property
    def engine(self) -> Compacting:
        return self.engines["alpha"]

    def run(self, *bodies: str) -> None:
        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            for body in bodies:
                self.scheduler.send(ANA, LineAddress("alpha"), body)
            await asyncio.sleep(0)
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(go())

    def status(self) -> dict[str, Any]:
        return self.scheduler.line_status("alpha")

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        tool = next(t for t in self.scheduler.own_tools("alpha") if t.name == name)

        async def go() -> dict[str, Any]:
            return await tool.handler(args)

        return asyncio.run(go())

    def compact_wake(self) -> str:
        """Ask for a compaction and return the identifier of the wake it scheduled."""
        self.call("compact_session", {})
        return self.log.state.scheduled["alpha"].message_id

    def past_due(self) -> None:
        self.clock.now = START + timedelta(seconds=61)


@pytest.fixture
def worlds(tmp_path: Path) -> Generator[Callable[[], World]]:
    """Builds worlds whose event logs are closed after the test, since an unclosed
    SQLite connection warns on Python 3.15 and the gate makes warnings errors."""
    built: list[World] = []

    def build() -> World:
        target = World(tmp_path / f"events-{len(built)}.sqlite")
        built.append(target)
        return target

    yield build
    for target in built:
        target.log.close()


@pytest.fixture
def world(worlds: Callable[[], World]) -> World:
    return worlds()


def withdrawn(world: World) -> list[WakeWithdrawn]:
    return [
        entry.event
        for entry in read_entries(world.path)
        if isinstance(entry.event, WakeWithdrawn)
    ]


# A. The compaction withdraws the wake compact_session scheduled for it.


def test_a_compaction_by_an_earlier_turn_withdraws_its_wake(world: World) -> None:
    message_id = world.compact_wake()
    world.run("work")
    assert world.engine.reasons == ["at your request"]
    assert "alpha" not in world.log.state.scheduled
    assert withdrawn(world) == [WakeWithdrawn("alpha", message_id)]
    assert world.status()["context"]["last_compaction"]["withdrawn_wake"] == message_id
    assert world.status()["scheduled_wake"] is None
    world.past_due()
    world.run()
    # No turn for nothing: the only turn is the one the operator's mail started.
    assert len(world.engine.prompts) == 1
    assert message_id not in world.log.state.messages


def test_the_wakes_own_turn_compacts_and_withdraws_nothing(world: World) -> None:
    message_id = world.compact_wake()
    world.past_due()
    world.run()
    assert world.engine.reasons == ["at your request"]
    assert message_id in world.log.state.messages
    assert withdrawn(world) == []
    assert world.status()["context"]["last_compaction"]["withdrawn_wake"] is None
    assert world.status()["autonomous_turns"]["used"] == 0


def test_context_warning_wakes_compactable_line_without_mail(world: World) -> None:
    world.scheduler._context_reported(  # pyright: ignore[reportPrivateUsage]
        "alpha", 150_001, CONTEXT_FROM_CALL
    )
    assert world.log.state.scheduled["alpha"].note.startswith("Dromon requests")
    world.run()
    assert world.engine.reasons == [COMPACTION_CONTEXT_WARNING]
    assert world.status()["context"]["estimated_tokens"] == 5_000
    assert world.status()["autonomous_turns"]["used"] == 0


def test_context_warning_wakes_are_coalesced_and_withdrawn_below_warning(
    world: World,
) -> None:
    world.scheduler._context_reported(  # pyright: ignore[reportPrivateUsage]
        "alpha", 150_001, CONTEXT_FROM_CALL
    )
    first = world.log.state.scheduled["alpha"].message_id
    world.scheduler._context_reported(  # pyright: ignore[reportPrivateUsage]
        "alpha", 151_000, CONTEXT_FROM_CALL
    )
    assert world.log.state.scheduled["alpha"].message_id == first
    assert (
        sum(
            isinstance(entry.event, WakeScheduled) for entry in read_entries(world.path)
        )
        == 1
    )

    world.scheduler._context_reported(  # pyright: ignore[reportPrivateUsage]
        "alpha", 149_999, CONTEXT_FROM_CALL
    )
    assert "alpha" not in world.log.state.scheduled
    assert withdrawn(world) == [WakeWithdrawn("alpha", first)]
    assert "alpha" not in world.scheduler._context_compaction_wakes  # pyright: ignore[reportPrivateUsage]


def test_context_warning_does_not_queue_a_note_for_a_held_line(world: World) -> None:
    world.scheduler.hold("alpha", "a test hold")
    assert world.log.state.lines["alpha"].held_by is not None
    world.scheduler._context_reported(  # pyright: ignore[reportPrivateUsage]
        "alpha", 150_001, CONTEXT_FROM_CALL
    )
    assert "alpha" not in world.log.state.scheduled
    assert "alpha" not in world.scheduler._context_compaction_wakes  # pyright: ignore[reportPrivateUsage]


def test_context_warning_does_not_wake_hibernated_line(world: World) -> None:
    world.scheduler.hibernate("alpha")
    world.scheduler._context_reported(  # pyright: ignore[reportPrivateUsage]
        "alpha", 150_001, CONTEXT_FROM_CALL
    )
    assert world.scheduler._sent == []  # pyright: ignore[reportPrivateUsage]
    assert world.scheduler._context_compaction_wakes == set()  # pyright: ignore[reportPrivateUsage]
    assert "alpha" not in world.log.state.scheduled

    world.scheduler.awaken("alpha")
    assert world.scheduler._work_waiting() is True  # pyright: ignore[reportPrivateUsage]
    assert "alpha" in world.log.state.scheduled


def test_compaction_that_leaves_context_high_does_not_queue_another_wake(
    world: World,
) -> None:
    world.scheduler.session_compacted(
        "alpha",
        {"trigger": "manual", "before_tokens": 200_000, "after_tokens": 175_000},
    )
    assert world.status()["context"]["auto_compact_held"] is True
    assert world.status()["context"]["compaction_pending"] is False
    assert "alpha" not in world.log.state.scheduled
    assert "alpha" not in world.scheduler._context_compaction_wakes  # pyright: ignore[reportPrivateUsage]


def test_a_wake_the_line_scheduled_itself_is_never_withdrawn(world: World) -> None:
    world.call("wake_after", {"seconds": 3600, "note": "check the build"})
    own = world.log.state.scheduled["alpha"]
    world.call("compact_session", {})
    world.run("work")
    assert world.engine.reasons == ["at your request"]
    assert world.log.state.scheduled["alpha"] == own
    assert withdrawn(world) == []


def test_a_wake_that_replaced_the_compaction_wake_is_not_withdrawn(
    world: World,
) -> None:
    world.compact_wake()
    world.call("wake_after", {"seconds": 3600, "note": "check the build"})
    own = world.log.state.scheduled["alpha"]
    world.run("work")
    assert world.log.state.scheduled["alpha"] == own
    assert withdrawn(world) == []


def test_a_compaction_the_engine_made_by_itself_withdraws_nothing(
    world: World,
) -> None:
    message_id = world.compact_wake()
    world.scheduler.session_compacted(
        "alpha", {"trigger": "auto", "before_tokens": 9, "after_tokens": 1}
    )
    assert world.log.state.scheduled["alpha"].message_id == message_id
    assert withdrawn(world) == []


def test_a_turn_that_did_not_compact_leaves_the_wake_to_fire(world: World) -> None:
    world.silent = True
    message_id = world.compact_wake()
    world.run("work")
    assert world.log.state.scheduled["alpha"].message_id == message_id
    world.past_due()
    world.run()
    assert len(world.engine.prompts) == 2
    assert world.log.state.messages[message_id].body == COMPACT_WAKE_NOTE


def test_after_a_restart_the_wake_fires_and_says_the_request_was_lost(
    world: World,
) -> None:
    message_id = world.compact_wake()
    world.scheduler = world.start()
    # Even a compaction after the restart cannot withdraw it, since the record of
    # which wake was the compaction's was in memory.
    world.scheduler.session_compacted(
        "alpha", {"trigger": "manual", "before_tokens": 9, "after_tokens": 1}
    )
    world.past_due()
    world.run()
    assert withdrawn(world) == []
    assert world.engine.reasons == []
    body = world.log.state.messages[message_id].body
    assert body == COMPACT_WAKE_NOTE and "restarted" in body


async def compacting_turn(world: World, release: bool) -> None:
    """Start the compacted turn, move the clock past the wake while the turn is
    compacting, tick, then let the compaction finish, or end without it when
    ``release`` is false, and run until idle."""
    world.gate = asyncio.Event()
    stop = asyncio.Event()
    loop = asyncio.create_task(world.scheduler.run(stop))
    world.scheduler.send(ANA, LineAddress("alpha"), "work")
    await world.compacting.wait()
    world.past_due()
    for _ in range(20):
        world.scheduler.step()
        await asyncio.sleep(0)
    held = "alpha" in world.log.state.scheduled
    if not release:
        world.silent = True
    world.gate.set()
    await world.scheduler.idle()
    stop.set()
    await loop
    assert held


def test_a_wake_due_during_the_compacting_turn_is_held_back_then_withdrawn(
    world: World,
) -> None:
    message_id = world.compact_wake()
    asyncio.run(compacting_turn(world, release=True))
    assert withdrawn(world) == [WakeWithdrawn("alpha", message_id)]
    assert message_id not in world.log.state.messages
    assert len(world.engine.prompts) == 1


def test_a_held_back_wake_fires_when_the_turn_ends_without_compacting(
    world: World,
) -> None:
    message_id = world.compact_wake()
    asyncio.run(compacting_turn(world, release=False))
    assert withdrawn(world) == []
    assert message_id in world.log.state.messages
    assert len(world.engine.prompts) == 2


def test_f2_a_failed_withdrawal_still_records_the_compaction(
    world: World,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    message_id = world.compact_wake()
    append = world.log.append

    def refusing(event: object) -> object:
        if isinstance(event, WakeWithdrawn):
            raise EventRejected("the log is closing")
        return append(cast(Any, event))

    monkeypatch.setattr(world.log, "append", refusing)
    world.run("work")
    last = context(world)["last_compaction"]
    assert last["cause"] == "compact_session"
    assert last["after_tokens"] == 5_000
    assert last["withdrawn_wake"] is None
    assert context(world)["estimated_tokens"] == 5_000
    assert "could not be withdrawn: the log is closing" in capsys.readouterr().err
    # The wake was not withdrawn, so it fires, and its note says what to do.
    assert world.log.state.scheduled["alpha"].message_id == message_id
    world.past_due()
    world.run()
    assert world.log.state.messages[message_id].body == COMPACT_WAKE_NOTE


def test_f3_a_wake_due_between_dispatch_and_the_turns_start_is_held(
    world: World,
) -> None:
    message_id = world.compact_wake()

    async def go() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "work")
        world.scheduler.step()  # The mail reaches the post office.
        world.scheduler.step()  # The turn is dispatched, and its task not started.
        assert "alpha" in world.scheduler._running  # pyright: ignore[reportPrivateUsage]
        assert "alpha" not in world.engines  # The turn has not built it yet.
        world.past_due()
        world.scheduler.step()  # Due now, but the compaction was decided.
        assert world.log.state.scheduled["alpha"].message_id == message_id
        await world.scheduler.idle()

    asyncio.run(go())
    assert withdrawn(world) == [WakeWithdrawn("alpha", message_id)]
    assert message_id not in world.log.state.messages
    assert len(world.engine.prompts) == 1


def test_f4_a_held_wake_fires_on_the_next_tick_after_its_turn_ends(
    world: World,
) -> None:
    message_id = world.compact_wake()
    ended: list[int] = []
    fired: list[int] = []
    run_turn = Compacting.run_turn
    fire = world.scheduler.fire_due_wakes

    async def ending(engine: Compacting, prompt: str) -> TurnReport:
        report = await run_turn(engine, prompt)
        ended.append(world.scheduler.tick)
        return report

    def firing() -> list[str]:
        result = fire()
        if result:
            fired.append(world.scheduler.tick)
        return result

    world.scheduler.fire_due_wakes = firing  # type: ignore[method-assign]
    Compacting.run_turn = ending  # type: ignore[method-assign]
    try:
        asyncio.run(compacting_turn(world, release=False))
    finally:
        Compacting.run_turn = run_turn  # type: ignore[method-assign]
    assert message_id in world.log.state.messages
    # The turn that held it ended, then the very next tick fired it.
    assert fired == [ended[0] + 1]


def test_the_tool_says_the_wake_is_withdrawn_if_an_earlier_turn_compacts(
    world: World,
) -> None:
    result = world.call("compact_session", {})
    assert "withdrawn" in str(result["content"][0]["text"])


# A, the wake_withdrawn event.


def scheduled_state() -> State:
    state = State()
    from dromon.model import LineCreated

    state.apply(LineCreated("alpha", HAIKU, "subscription"))
    state.apply(LineCreated("beta", HAIKU, "subscription"))
    state.apply(WakeScheduled("alpha", "s1", "2026-10-26T09:30:00Z", "compact"))
    return state


def test_wake_withdrawn_removes_the_pending_wake_and_keeps_its_id() -> None:
    state = scheduled_state()
    state.apply(WakeWithdrawn("alpha", "s1"))
    assert "alpha" not in state.scheduled
    assert "s1" not in state.messages
    assert state.withdrawn_wakes == {"s1"}
    assert state.copy().withdrawn_wakes == {"s1"}
    assert state.copy().withdrawn_wakes is not state.withdrawn_wakes


def test_f1_a_wake_scheduled_with_a_withdrawn_id_is_refused_for_that_reason() -> None:
    state = scheduled_state()
    state.apply(WakeWithdrawn("alpha", "s1"))
    reused = WakeScheduled("alpha", "s1", "2026-10-26T10:00:00Z", "again")
    with pytest.raises(EventRejected, match="s1 was the identifier of a withdrawn"):
        state.apply(reused)
    # Without the withdrawal, the same event is accepted, so the refusal is the
    # withdrawal's and no other rule's.
    fresh = scheduled_state()
    fresh.apply(WakeScheduled("alpha", "s1", "2026-10-26T10:00:00Z", "again"))
    assert fresh.scheduled["alpha"].note == "again"


def test_f1_a_message_or_task_note_with_a_withdrawn_id_is_refused() -> None:
    from dromon.model import MessageKind, OperatorRegistered, TaskNoted

    state = scheduled_state()
    state.apply(OperatorRegistered("ana"))
    state.apply(WakeWithdrawn("alpha", "s1"))
    sent = MessageSent("s1", ANA, LineAddress("alpha"), MessageKind.NOTE, "late")
    with pytest.raises(EventRejected, match="withdrawn wake"):
        state.apply(sent)
    with pytest.raises(EventRejected, match="withdrawn wake"):
        state.apply(TaskNoted("alpha", "s1", "t", "completed", "x"))


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (WakeWithdrawn("nobody", "s1"), "nobody"),
        (WakeWithdrawn("beta", "s1"), "no pending scheduled wake"),
        (WakeWithdrawn("alpha", "s2"), "no pending scheduled wake"),
    ],
)
def test_wake_withdrawn_is_refused_without_that_pending_wake(
    event: WakeWithdrawn, message: str
) -> None:
    state = scheduled_state()
    with pytest.raises(EventRejected, match=message):
        state.apply(event)
    assert state.scheduled["alpha"].message_id == "s1"


def test_wake_withdrawn_twice_is_refused() -> None:
    state = scheduled_state()
    state.apply(WakeWithdrawn("alpha", "s1"))
    with pytest.raises(EventRejected):
        state.apply(WakeWithdrawn("alpha", "s1"))


def test_wake_withdrawn_round_trips_and_checks_its_fields() -> None:
    event = WakeWithdrawn("alpha", "s1")
    assert decode(encode(event)) == event
    with pytest.raises(InvalidField):
        WakeWithdrawn("", "s1")
    with pytest.raises(InvalidField):
        WakeWithdrawn("alpha", "")
    with pytest.raises(DecodeError, match="wake_withdrawn"):
        decode(encode(event).replace('"s1"', '""'))


# B. Zero or nothing, for a turn outside Dromon's own.


@pytest.mark.parametrize("kind", [SELF_STARTED_KIND, INTERRUPTED_KIND])
def test_a_reported_zero_is_told_apart_from_nothing(world: World, kind: str) -> None:
    zeros = {"input_tokens": 0, "output_tokens": 0}
    world.scheduler.spent_between("alpha", kind, 0.0, zeros)
    world.scheduler.spent_between("alpha", kind, None, None)
    zero, nothing = world.status()["recent_turns"]
    assert zero["cost_usd"] == "0.0000"
    assert "reported a cost of zero, not nothing" in zero["cost_note"]
    assert "Inferred, not observed" in zero["cost_note"]
    assert zero["usage_note"] == "the engine reported zero tokens, not nothing"
    assert nothing["cost_usd"] is None
    assert nothing["cost_note"] == (
        "the engine reported no cost for this turn, so none was counted"
    )
    assert nothing["usage_note"] == "the engine reported no token usage for this turn"


def test_a_reported_amount_needs_no_note(world: World) -> None:
    world.scheduler.spent_between(
        "alpha", SELF_STARTED_KIND, 0.01, {"input_tokens": 3, "output_tokens": 0}
    )
    (entry,) = world.status()["recent_turns"]
    assert entry["cost_note"] is None and entry["usage_note"] is None


def test_tokens_without_a_cost_say_only_the_cost_is_missing(world: World) -> None:
    world.scheduler.spent_between(
        "alpha", SELF_STARTED_KIND, None, {"output_tokens": 7}
    )
    (entry,) = world.status()["recent_turns"]
    assert "no cost" in entry["cost_note"] and entry["usage_note"] is None


# C. The context note names its source.


def context(world: World) -> dict[str, Any]:
    return cast(dict[str, Any], world.status()["context"])


def test_a_reported_compaction_is_named_as_the_source_until_the_turn_ends(
    world: World,
) -> None:
    world.compactable(context_warn_tokens=500_000)
    world.context = 288_128
    world.run("grow")
    assert context(world)["note"] == CONTEXT_NOTES[CONTEXT_FROM_CALL]
    world.scheduler.session_compacted(
        "alpha", {"trigger": "manual", "before_tokens": 288_128, "after_tokens": 5_477}
    )
    during = context(world)
    assert during["estimated_tokens"] == 5_477
    assert during["note"] == CONTEXT_NOTES[CONTEXT_FROM_COMPACTION]
    assert "compaction" in during["note"] and "call's input" not in during["note"]
    world.context = 30_463
    world.run("work")
    after = context(world)
    assert after["estimated_tokens"] == 30_463
    assert after["note"] == CONTEXT_NOTES[CONTEXT_FROM_CALL]


def test_an_unsized_compaction_says_the_estimate_is_unknown(world: World) -> None:
    world.scheduler.session_compacted(
        "alpha", {"trigger": "manual", "before_tokens": 9_000, "after_tokens": None}
    )
    status = context(world)
    assert status["estimated_tokens"] is None
    assert "unknown since the last compaction" in status["note"]


# D. Codex's size after, estimated from the first call after the compaction.


def unsized(world: World, **keys: object) -> None:
    world.compactable(**keys)
    world.scheduler.session_compacted(
        "alpha", {"trigger": "manual", "before_tokens": 200_000, "after_tokens": None}
    )


def test_an_estimate_after_an_unsized_compaction_is_recorded_and_labelled(
    world: World,
) -> None:
    unsized(world)
    world.scheduler.compaction_estimated("alpha", 24_629)
    status = context(world)
    last = status["last_compaction"]
    assert last["after_tokens"] is None
    assert last["after_estimate_tokens"] == 24_629
    assert last["after_estimate_note"] == AFTER_ESTIMATE_NOTE
    assert "upper bound" in AFTER_ESTIMATE_NOTE and "prompt" in AFTER_ESTIMATE_NOTE
    assert status["estimated_tokens"] == 24_629
    assert status["note"] == CONTEXT_NOTES[CONTEXT_FROM_ESTIMATE]


def test_an_estimate_below_the_threshold_releases_the_hold(world: World) -> None:
    unsized(world, auto_compact_tokens=180_000)
    assert context(world)["auto_compact_held"] is True
    world.scheduler.compaction_estimated("alpha", 24_629)
    assert context(world)["auto_compact_held"] is False


@pytest.mark.parametrize("tokens", [180_000, 195_000])
def test_an_estimate_at_or_past_the_threshold_keeps_the_hold(
    world: World, tokens: int
) -> None:
    unsized(world, auto_compact_tokens=180_000)
    world.scheduler.compaction_estimated("alpha", tokens)
    assert context(world)["auto_compact_held"] is True
    # Held, so the next turn does not compact again.
    world.context = tokens
    world.run("work")
    assert world.engine.reasons == []


@pytest.mark.parametrize("tokens", [0, -5, True])
def test_a_bad_estimate_is_ignored(world: World, tokens: int) -> None:
    unsized(world)
    world.scheduler.compaction_estimated("alpha", tokens)
    assert context(world)["last_compaction"]["after_estimate_tokens"] is None
    assert context(world)["estimated_tokens"] is None


def test_an_estimate_without_an_unsized_compaction_is_ignored(world: World) -> None:
    world.scheduler.compaction_estimated("alpha", 1_000)
    assert context(world)["estimated_tokens"] is None
    world.scheduler.session_compacted(
        "alpha", {"trigger": "manual", "before_tokens": 9_000, "after_tokens": 500}
    )
    world.scheduler.compaction_estimated("alpha", 1_000)
    assert context(world)["last_compaction"]["after_estimate_tokens"] is None
    assert context(world)["estimated_tokens"] == 500


def test_a_second_estimate_for_one_compaction_is_ignored(world: World) -> None:
    unsized(world)
    world.scheduler.compaction_estimated("alpha", 24_629)
    world.scheduler.compaction_estimated("alpha", 99_999)
    assert context(world)["last_compaction"]["after_estimate_tokens"] == 24_629


def test_an_estimate_for_an_unknown_line_is_ignored(world: World) -> None:
    world.scheduler.compaction_estimated("nobody", 1_000)
    assert context(world)["estimated_tokens"] is None


# D, at the Codex engine.


def notice(method: str, **params: object) -> dict[str, Any]:
    return {"method": method, "params": params}


def usage(total: int, last: int) -> dict[str, Any]:
    def breakdown(tokens: int) -> dict[str, int]:
        return {
            "totalTokens": tokens,
            "inputTokens": tokens - 10,
            "cachedInputTokens": tokens // 2,
            "cacheWriteInputTokens": 0,
            "outputTokens": 10,
            "reasoningOutputTokens": 4,
        }

    return notice(
        TOKEN_USAGE_UPDATED,
        threadId="th",
        turnId="t1",
        tokenUsage={
            "total": breakdown(total),
            "last": breakdown(last),
            "modelContextWindow": 272_000,
        },
    )


def done(turn_id: str = "t1") -> dict[str, Any]:
    return notice(
        "turn/completed", threadId="th", turn={"id": turn_id, "status": "completed"}
    )


def compaction_item(method: str) -> dict[str, Any]:
    return notice(
        method, threadId="th", turnId="c1", item={"type": COMPACTION_ITEM, "id": "k"}
    )


OBSERVED_COMPACTION: Final = [
    notice("turn/started", turn={"id": "c1"}),
    compaction_item("item/started"),
    compaction_item("item/completed"),
    done("c1"),
]
"""A compaction as Codex 0.164.0 reported it live, which gives no size after."""


class Server:
    """A scripted app-server, which plays ``turns`` after each ``turn/start`` and
    ``compaction`` after ``thread/compact/start``."""

    def __init__(
        self, turns: list[list[dict[str, Any]]], compaction: Sequence[dict[str, Any]]
    ) -> None:
        self.turns = turns
        self.compaction = list(compaction)
        self.sent: list[dict[str, Any]] = []
        self.queue: deque[dict[str, Any]] = deque()

    def methods(self) -> list[str]:
        return [m["method"] for m in self.sent if "method" in m]

    async def send(self, message: Mapping[str, object]) -> None:
        sent = dict(message)
        self.sent.append(sent)
        method = sent.get("method")
        if not isinstance(method, str) or "id" not in sent:
            return
        self.queue.append({"id": sent["id"], "result": self.result(method)})
        if method == "turn/start":
            self.queue.append(notice("turn/started", turn={"id": "t1"}))
            self.queue.extend(self.turns.pop(0))
        if method == COMPACT_START:
            self.queue.extend(self.compaction)

    def result(self, method: str) -> dict[str, Any]:
        if method == "account/read":
            return {"account": {"type": "chatgpt", "planType": "pro"}}
        if method in ("thread/start", "thread/resume"):
            return {
                "thread": {"id": "th"},
                "model": LUNA,
                "modelProvider": "openai",
                "activePermissionProfile": {"id": "dromon-line"},
            }
        if method in ("mcpServerStatus/list", "app/list"):
            return {"data": []}
        if method == "turn/start":
            return {"turn": {"id": "t1"}}
        return {}

    async def receive(self) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        return self.queue.popleft() if self.queue else None


@asynccontextmanager
async def served(server: Server) -> AsyncGenerator[Connection]:
    yield server


def codex_line(
    server: Server,
    estimated: Callable[[int], object],
    compacted: Callable[[Mapping[str, int | str | None]], object] = lambda c: None,
) -> CodexLine:
    return CodexLine(
        Driver.for_model(LUNA, Strategy.SUBSCRIPTION),
        Path("/work"),
        config=config_from_document({"allow_compact": True}),
        environment={},
        connection_factory=lambda arguments, cwd, env: served(server),
        server_lister=lambda env: [],
        compacted=compacted,
        compaction_estimated=estimated,
    )


def second_turn(
    server: Server, estimated: Callable[[int], object]
) -> tuple[TurnReport, list[Mapping[str, int | str | None]]]:
    compactions: list[Mapping[str, int | str | None]] = []
    target = codex_line(server, estimated, compactions.append)

    async def go() -> TurnReport:
        await target.run_turn("first")
        target.request_compaction("at your request", lambda: None)
        return await target.run_turn("second")

    return asyncio.run(go()), compactions


def test_the_first_call_after_an_unsized_codex_compaction_estimates_it() -> None:
    server = Server(
        [
            [usage(9_000, 8_000), done()],
            [usage(12_000, 2_500), usage(16_000, 4_000), done()],
        ],
        OBSERVED_COMPACTION,
    )
    estimates: list[int] = []
    report, compactions = second_turn(server, estimates.append)
    # The first call's input, which holds the compacted thread and the prompt.
    assert estimates == [2_490]
    assert compactions[0]["after_tokens"] is None
    assert report.compaction is not None
    assert report.compaction["after_estimate_tokens"] == 2_490
    assert report.compaction["after_tokens"] is None
    # The turn's end still gives its last call's context.
    assert report.context_tokens == 4_000


def test_a_reported_codex_size_gives_no_estimate() -> None:
    sized = [
        notice("turn/started", turn={"id": "c1"}),
        notice(THREAD_COMPACTED, threadId="th", turnId="c1"),
        usage(9_500, 500),
        done("c1"),
    ]
    server = Server(
        [[usage(9_000, 8_000), done()], [usage(12_000, 2_500), done()]], sized
    )
    estimates: list[int] = []
    report, _ = second_turn(server, estimates.append)
    assert estimates == []
    assert report.compaction is not None
    assert report.compaction["after_tokens"] == 500
    assert report.compaction["after_estimate_tokens"] is None


def test_a_codex_turn_without_a_compaction_gives_no_estimate() -> None:
    server = Server([[usage(9_000, 8_000), done()]], OBSERVED_COMPACTION)
    estimates: list[int] = []
    asyncio.run(codex_line(server, estimates.append).run_turn("first"))
    assert estimates == []


def test_an_unsized_codex_compaction_with_no_call_after_gives_no_estimate() -> None:
    server = Server([[usage(9_000, 8_000), done()], [done()]], OBSERVED_COMPACTION)
    estimates: list[int] = []
    report, _ = second_turn(server, estimates.append)
    assert estimates == []
    assert report.compaction is not None
    assert report.compaction["after_estimate_tokens"] is None


def test_a_failing_estimate_callback_does_not_end_the_turn(
    capsys: pytest.CaptureFixture[str],
) -> None:
    server = Server(
        [[usage(9_000, 8_000), done()], [usage(12_000, 2_500), done()]],
        OBSERVED_COMPACTION,
    )

    def fail(tokens: int) -> None:
        raise RuntimeError(f"no room for {tokens}")

    report, _ = second_turn(server, fail)
    assert report.subtype == "success"
    assert "could not be recorded" in capsys.readouterr().err


def test_the_codex_estimate_reaches_the_status_within_its_turn(
    world: World,
) -> None:
    """Through the scheduler, with the hold released by the estimate itself."""
    seen: list[dict[str, Any]] = []
    server = Server(
        [
            [usage(200_000, 190_000), done()],
            [usage(220_000, 24_629), done()],
        ],
        OBSERVED_COMPACTION,
    )

    def build(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> CodexLine:
        del tools

        def estimated(tokens: int) -> None:
            spec.compaction_estimated(tokens)
            seen.append(
                cast(dict[str, Any], world.scheduler.line_status("luna")["context"])
            )

        return CodexLine(
            Driver.for_model(LUNA, Strategy.SUBSCRIPTION),
            Path("/work"),
            config=spec.config,
            environment={},
            connection_factory=lambda arguments, cwd, env: served(server),
            server_lister=lambda env: [],
            compacted=spec.compacted,
            compaction_estimated=estimated,
        )

    world.scheduler._engine_factory = build  # pyright: ignore[reportPrivateUsage]
    world.scheduler.create_line("luna", LUNA, Strategy.SUBSCRIPTION)
    world.scheduler.configure_line(
        "luna",
        config_from_document(
            {
                "allow_compact": True,
                "context_warn_tokens": 250_000,
                "auto_compact_tokens": 180_000,
            }
        ),
    )
    for body in ("one", "two"):
        run_luna(world, body)
    assert server.methods().count(COMPACT_START) == 1
    (during,) = seen
    assert during["last_compaction"]["after_estimate_tokens"] == 24_619
    assert during["last_compaction"]["cause"] == "auto_compact_tokens"
    assert during["estimated_tokens"] == 24_619
    assert during["auto_compact_held"] is False
    assert during["note"] == CONTEXT_NOTES[CONTEXT_FROM_ESTIMATE]
    after = cast(dict[str, Any], world.scheduler.line_status("luna")["context"])
    assert after["estimated_tokens"] == 24_629
    assert after["note"] == CONTEXT_NOTES[CONTEXT_FROM_CALL]
    (entry,) = [
        turn
        for turn in cast(
            list[dict[str, Any]], world.scheduler.line_status("luna")["recent_turns"]
        )
        if turn["compaction"] is not None
    ]
    assert entry["compaction"]["after_estimate_tokens"] == 24_619


def run_luna(world: World, body: str) -> None:
    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        world.scheduler.send(ANA, LineAddress("luna"), body)
        await asyncio.sleep(0)
        await world.scheduler.idle()
        stop.set()
        await loop

    asyncio.run(go())


def test_the_production_factory_wires_the_estimate_to_codex(tmp_path: Path) -> None:
    estimates: list[int] = []
    spec = LineSpec(
        "luna",
        LUNA,
        Strategy.SUBSCRIPTION,
        config_from_document({"allow_compact": True}),
        compaction_estimated=estimates.append,
    )
    engine = engine_factory(str(tmp_path))(spec, [])
    assert isinstance(engine, CodexLine)
    engine._estimated(7)  # pyright: ignore[reportPrivateUsage]
    assert estimates == [7]


def test_pi_review_f3_an_engines_own_estimate_is_carried(world: World) -> None:
    """An engine that estimates the size its compaction left, as Pi does, has that
    estimate and its note in last_compaction and as the context estimate."""
    world.scheduler.session_compacted(
        "alpha",
        {
            "trigger": "auto",
            "before_tokens": 150000,
            "after_tokens": None,
            "after_estimate_tokens": 32000,
            "after_estimate_note": "the engine's own estimate",
        },
    )
    context = cast(dict[str, Any], world.status()["context"])
    last = cast(dict[str, Any], context["last_compaction"])
    assert last["after_tokens"] is None
    assert last["after_estimate_tokens"] == 32000
    assert last["after_estimate_note"] == "the engine's own estimate"
    assert context["estimated_tokens"] == 32000
    assert "engine's own estimate" in context["note"]
