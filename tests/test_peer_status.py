"""What a line sees of every other line in ``line_status``: its condition, the short
cause of a hold, since when it has stood so, and how much unread mail waits for it
and how old the oldest is, and never that mail. **Task of 2026-10-31.** A line held
back work planned for a peer, because its status gave no reliable view of the peer."""

import asyncio
import json
from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import scheduler as scheduler_module
from dromon.codec import DecodeError, decode, encode
from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog
from dromon.limits import Limits
from dromon.model import (
    HoldCause,
    LineAddress,
    LineHeld,
    LineState,
    LineStateChanged,
    MessageDelivered,
    MessageKind,
    MessageRead,
    MessageSent,
    OperatorAddress,
)
from dromon.scheduler import LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
BETA = LineAddress("beta")
START = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
HOLD_REASON = "was held with a private reason for this test"


class Clock:
    """One clock for the scheduler and the event log, so a recorded time and the
    age read from it come from the same source."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def text(self) -> str:
        return self.now.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class Recorder:
    def __init__(self, spec: LineSpec) -> None:
        self.spec = spec
        self.session_id = spec.session_id

    async def run_turn(self, prompt: str) -> TurnReport:
        del prompt
        return TurnReport("success", 1, "ok", (), None, None, "session-1", None, 0.0)


class World:
    def __init__(self, path: Path, root: Path) -> None:
        self.path = path
        self.root = root
        self.clock = Clock()
        self.log = EventLog(path, self.clock.text)
        self.scheduler = self.start()
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        self.scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
        del tools
        return Recorder(spec)

    def start(self) -> Scheduler:
        return Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            wall_clock=self.clock,
            limits=Limits(max_autonomous_turns=5, max_cost_usd=3.0),
            root=self.root,
            protected=(self.root / ".dromon",),
        )

    def restart(self) -> None:
        """Close the log and open it again with a new scheduler, as a restarted
        daemon does, so nothing kept in memory survives."""
        self.log.close()
        self.log = EventLog(self.path, self.clock.text)
        self.scheduler = self.start()

    def status_text(self) -> str:
        tool = next(
            t for t in self.scheduler.own_tools("alpha") if t.name == "line_status"
        )

        async def invoke() -> dict[str, Any]:
            return await tool.handler({"detail": True, "lists": True})

        result = asyncio.run(invoke())
        assert result.get("is_error") is not True
        return cast(str, result["content"][0]["text"])

    def beta(self) -> dict[str, object]:
        status = json.loads(self.status_text())
        (beta,) = cast(list[dict[str, object]], status["other_lines"])
        return beta


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    root = tmp_path.resolve() / "project"
    (root / ".dromon").mkdir(parents=True)
    target = World(tmp_path / "events.sqlite", root)
    yield target
    target.log.close()


def due(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


# The condition, its cause and since when.


def test_an_idle_line_stands_since_it_was_created(world: World) -> None:
    created = world.clock.now
    world.clock.advance(90)
    beta = world.beta()
    assert (beta["state"], beta["condition"]) == ("idle", "idle")
    assert (beta["held"], beta["hold_cause"], beta["hibernated"]) == (
        False,
        None,
        False,
    )
    assert beta["since"] == due(created)


def test_a_running_line_stands_since_its_turn_started(world: World) -> None:
    world.clock.advance(30)
    started = world.clock.now
    world.log.append(LineStateChanged("beta", LineState.RUNNING))
    world.clock.advance(45)
    beta = world.beta()
    assert (beta["state"], beta["condition"]) == ("running", "running")
    assert beta["since"] == due(started)


@pytest.mark.parametrize("cause", list(HoldCause))
def test_a_held_line_shows_its_short_cause_and_never_the_reason(
    world: World, cause: HoldCause
) -> None:
    world.clock.advance(60)
    held_at = world.clock.now
    world.scheduler.hold("beta", HOLD_REASON, cause=cause)
    world.clock.advance(60)
    text = world.status_text()
    beta = world.beta()
    assert (beta["condition"], beta["held"]) == ("held", True)
    assert beta["hold_cause"] == cause.value
    assert beta["since"] == due(held_at)
    assert HOLD_REASON not in text
    question = world.log.state.lines["beta"].held_by
    assert question is not None and question not in text


def test_a_release_clears_the_cause_and_restarts_the_since(world: World) -> None:
    world.scheduler.hold("beta", HOLD_REASON, cause=HoldCause.COST)
    world.clock.advance(120)
    released = world.clock.now
    world.scheduler.release("beta")
    world.clock.advance(5)
    beta = world.beta()
    # Released without an answer, the hold question is still open, so the line
    # is waiting on an operator rather than idle.
    assert (beta["condition"], beta["held"], beta["hold_cause"]) == (
        "waiting",
        False,
        None,
    )
    assert world.log.state.lines["beta"].hold_cause is None
    assert beta["since"] == due(released)


def test_a_hold_recorded_before_causes_shows_its_cause_unrecorded(
    world: World,
) -> None:
    question = world.scheduler.ask("beta", ANA, "Resume beta?", ("resume",))
    world.log.append(LineHeld("beta", question))
    beta = world.beta()
    assert (beta["held"], beta["hold_cause"]) == (True, "unrecorded")


def test_a_hibernated_line_stands_since_it_was_hibernated(world: World) -> None:
    world.clock.advance(10)
    hibernated = world.clock.now
    assert world.scheduler.hibernate("beta")
    world.clock.advance(10)
    beta = world.beta()
    assert (beta["condition"], beta["hibernated"]) == ("hibernated", True)
    assert beta["since"] == due(hibernated)


def test_a_hibernated_line_that_is_also_held_shows_both(world: World) -> None:
    world.scheduler.hold("beta", HOLD_REASON, cause=HoldCause.STOPPED)
    world.scheduler.hibernate("beta")
    beta = world.beta()
    assert beta["condition"] == "hibernated"
    assert (beta["held"], beta["hold_cause"]) == (True, "stopped")


def test_mail_alone_does_not_move_the_since(world: World) -> None:
    created = world.clock.now
    world.clock.advance(30)
    world.scheduler.send(ANA, BETA, "a note")
    assert world.beta()["since"] == due(created)


def test_a_line_sees_no_entry_for_itself(world: World) -> None:
    status = json.loads(world.status_text())
    lines = cast(list[dict[str, object]], status["other_lines"])
    assert [line["line_id"] for line in lines] == ["beta"]


# The oldest unread mail.


def test_the_oldest_unread_mail_is_aged_from_when_it_was_sent(world: World) -> None:
    first = world.scheduler.send(ANA, BETA, "the first private body")
    world.clock.advance(100)
    world.scheduler.send(ALPHA, BETA, "the second private body")
    world.clock.advance(20)
    beta = world.beta()
    assert (beta["unread"], beta["oldest_unread_age_seconds"]) == (2, 120)
    world.log.append(MessageDelivered(first))
    world.log.append(MessageRead(first))
    beta = world.beta()
    assert (beta["unread"], beta["oldest_unread_age_seconds"]) == (1, 20)


def test_mail_waiting_for_delivery_counts_as_unread(world: World) -> None:
    """A held line's mail waits undelivered, and a peer planning around the line
    needs to see it."""
    world.scheduler.hold("beta", HOLD_REASON, cause=HoldCause.AUTONOMOUS_TURNS)
    message_id = world.scheduler.send(ANA, BETA, "waits while beta is held")
    world.clock.advance(7)
    assert not world.log.state.messages[message_id].delivered
    beta = world.beta()
    assert (beta["unread"], beta["oldest_unread_age_seconds"]) == (1, 7)


def test_a_lines_own_notes_are_not_its_unread_mail(world: World) -> None:
    world.scheduler.send(BETA, BETA, "a note to my next turn")
    world.clock.advance(5)
    beta = world.beta()
    assert (beta["unread"], beta["oldest_unread_age_seconds"]) == (0, None)


def test_another_lines_mail_never_reaches_the_view(world: World) -> None:
    world.scheduler.send(ANA, BETA, "a private body for beta alone")
    question = world.scheduler.ask("beta", ANA, "a private question of beta's")
    text = world.status_text()
    for leaked in ("a private body for beta alone", "a private question", question):
        assert leaked not in text
    for message_id in world.log.state.messages:
        if world.log.state.messages[message_id].recipient == BETA:
            assert message_id not in text


def test_a_clock_that_stepped_back_never_gives_a_negative_age(world: World) -> None:
    world.scheduler.send(ANA, BETA, "a note")
    world.clock.advance(-600)
    assert world.beta()["oldest_unread_age_seconds"] == 0


@pytest.mark.parametrize("recorded", [None, "not a time", "2026-10-31T09:00:00"])
def test_a_time_that_is_absent_or_does_not_parse_gives_no_age(
    recorded: str | None,
) -> None:
    age = scheduler_module._age_seconds(recorded, START)  # pyright: ignore[reportPrivateUsage]
    assert age is None
    since = scheduler_module._recorded_time_of(recorded)  # pyright: ignore[reportPrivateUsage]
    assert since is None


# Across a restart.


def test_the_view_is_rebuilt_from_the_log_after_a_restart(world: World) -> None:
    world.scheduler.send(ANA, BETA, "a note")
    world.clock.advance(40)
    held_at = world.clock.now
    world.scheduler.hold("beta", HOLD_REASON, cause=HoldCause.PLAN_LIMIT)
    world.clock.advance(20)
    before = world.beta()
    world.restart()
    after = world.beta()
    for field in (
        "condition",
        "held",
        "hold_cause",
        "since",
        "unread",
        "oldest_unread_age_seconds",
    ):
        assert after[field] == before[field], field
    assert after["since"] == due(held_at)
    assert after["hold_cause"] == "plan_limit"
    assert after["oldest_unread_age_seconds"] == 60


# The hold paths and the wire.


def test_the_autonomous_turn_limit_records_its_cause(world: World) -> None:
    for _ in range(5):
        world.scheduler._apply_limits(  # pyright: ignore[reportPrivateUsage]
            "beta", scheduler_module.TurnCause.AUTONOMOUS, True, None
        )
    assert world.log.state.lines["beta"].hold_cause is HoldCause.AUTONOMOUS_TURNS
    assert world.beta()["hold_cause"] == "autonomous_turns"


def test_the_cost_limit_records_its_cause(world: World) -> None:
    report = TurnReport("success", 1, "ok", (), None, None, "session-1", None, 4.0)
    world.scheduler._apply_limits(  # pyright: ignore[reportPrivateUsage]
        "beta", scheduler_module.TurnCause.OPERATOR, True, report
    )
    assert world.log.state.lines["beta"].hold_cause is HoldCause.COST


def test_an_operators_stop_records_its_cause(world: World) -> None:
    """An operator's stop of a running turn holds the line as stopped."""

    async def go() -> None:
        async def turn() -> None:
            await asyncio.sleep(3600)

        task = asyncio.create_task(turn())
        world.scheduler._running["beta"] = task  # pyright: ignore[reportPrivateUsage]
        assert world.scheduler.interrupt("beta") is True
        with pytest.raises(asyncio.CancelledError):
            await task
        world.scheduler._running.pop("beta")  # pyright: ignore[reportPrivateUsage]

    asyncio.run(go())
    assert world.log.state.lines["beta"].hold_cause is HoldCause.STOPPED


def test_a_held_event_without_a_cause_is_written_as_before_causes() -> None:
    payload = encode(LineHeld("alpha", "q1"))
    assert "cause" not in json.loads(payload)["data"]
    assert decode(payload) == LineHeld("alpha", "q1")


def test_a_held_event_with_a_cause_round_trips() -> None:
    event = LineHeld("alpha", "q1", HoldCause.DISCLOSURE_CHECKS)
    payload = encode(event)
    assert json.loads(payload)["data"]["cause"] == "disclosure_checks"
    assert decode(payload) == event


def test_a_held_event_with_an_unknown_cause_is_refused() -> None:
    payload = json.loads(encode(LineHeld("alpha", "q1", HoldCause.COST)))
    payload["data"]["cause"] = "boredom"
    with pytest.raises(DecodeError, match="cause has no value"):
        decode(json.dumps(payload))


def test_every_cause_is_a_short_word() -> None:
    for cause in HoldCause:
        assert cause.value.replace("_", "").isalpha() and len(cause.value) <= 24


def test_a_messages_time_is_its_first_events(world: World) -> None:
    """The time kept for a message is its ``message_sent``'s, so a later event of
    the same message never makes it younger."""
    sent_at = world.clock.text()
    message_id = world.scheduler.send(ANA, BETA, "a note")
    world.clock.advance(50)
    world.log.append(MessageDelivered(message_id))
    assert world.log.times.message_recorded[message_id] == sent_at


def test_the_times_follow_the_events_the_log_holds(world: World) -> None:
    """Only the events that change a line's condition move its time, and the times
    read after a restart are those recorded, not the time of the restart."""
    world.clock.advance(10)
    changed = world.clock.text()
    world.log.append(MessageSent("m-x", ANA, BETA, MessageKind.NOTE, "a note"))
    world.log.append(LineStateChanged("beta", LineState.RUNNING))
    world.clock.advance(10)
    world.log.append(MessageDelivered("m-x"))
    assert world.log.times.line_changed["beta"] == changed
    world.clock.advance(1000)
    world.restart()
    assert world.log.times.line_changed["beta"] == changed
    assert world.log.times.message_recorded["m-x"] == changed
