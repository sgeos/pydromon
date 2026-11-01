"""What a line sees of every other line in ``line_status``: its condition, the short
cause of a hold, since when it has stood so, and how much unread mail waits for it
and how old the oldest is, and never that mail. **Task of 2026-10-31.** A line held
back work planned for a peer, because its status gave no reliable view of the peer.

**Task of 2026-11-01, answering review r13.** The condition and the unread mail are
read from indexes the state keeps, the view is built only when the lists are asked
for, each entry is compact, and a view too large for the response bound keeps as many
lines as fit with a count of the rest. Every hold site is pinned to its cause."""

import asyncio
import json
from collections.abc import Callable, Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import gitssh
from dromon import scheduler as scheduler_module
from dromon.codec import DecodeError, decode, encode
from dromon.driver import Strategy
from dromon.engine import ConfinementBreach, EnginePlanLimit, TurnReport
from dromon.eventlog import EventLog
from dromon.gitssh import GitSshAccess
from dromon.limits import Limits
from dromon.model import (
    Event,
    HoldCause,
    LeaseAcquired,
    LineAddress,
    LineHeld,
    LineMail,
    LineState,
    LineStateChanged,
    Message,
    MessageDelivered,
    MessageKind,
    MessageRead,
    MessageSent,
    OperatorAddress,
    QuestionStatus,
    State,
)
from dromon.scheduler import (
    MAX_LINE_STATUS_DEFAULT_BYTES,
    LineSpec,
    Scheduler,
    TurnRefused,
)

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
BETA = LineAddress("beta")
START = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
HOLD_REASON = "was held with a private reason for this test"
ALWAYS = {"line_id", "state", "condition"}
"""The fields every entry gives. Every other field is given only when it says
something."""


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
    """An engine whose next turn runs the action its world holds for the line, which
    may send mail or raise, and otherwise succeeds."""

    def __init__(self, world: "World", spec: LineSpec) -> None:
        self.world = world
        self.spec = spec
        self.session_id = spec.session_id

    async def run_turn(self, prompt: str) -> TurnReport:
        del prompt
        action = self.world.actions.pop(self.spec.line_id, None)
        if action is not None:
            action()
        return TurnReport("success", 1, "ok", (), None, None, "session-1", None, 0.0)


class World:
    def __init__(self, path: Path, root: Path) -> None:
        self.path = path
        self.root = root
        self.clock = Clock()
        self.actions: dict[str, Callable[[], None]] = {}
        self.log = EventLog(path, self.clock.text)
        self.scheduler = self.start()
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        self.scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
        del tools
        return Recorder(self, spec)

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

    def call(self, args: dict[str, object]) -> str:
        tool = next(
            t for t in self.scheduler.own_tools("alpha") if t.name == "line_status"
        )

        async def invoke() -> dict[str, Any]:
            return await tool.handler(args)

        result = asyncio.run(invoke())
        assert result.get("is_error") is not True
        return cast(str, result["content"][0]["text"])

    def status_text(self) -> str:
        return self.call({"detail": True, "lists": True})

    def others(self) -> list[dict[str, object]]:
        return cast(
            list[dict[str, object]], json.loads(self.status_text())["other_lines"]
        )

    def beta(self) -> dict[str, object]:
        (beta,) = self.others()
        return beta

    def turn(self, line_id: str, action: Callable[[], None]) -> None:
        """Run one turn of ``line_id`` that runs ``action``, woken by an operator's
        note, and wait until the daemon is idle."""
        self.actions[line_id] = action

        async def settle() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            await asyncio.sleep(0)
            self.scheduler.send(ANA, LineAddress(line_id), "go on")
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(settle())


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    root = tmp_path.resolve() / "project"
    (root / ".dromon").mkdir(parents=True)
    target = World(tmp_path / "events.sqlite", root)
    yield target
    target.log.close()


def due(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def raising(error: Exception) -> Callable[[], None]:
    def act() -> None:
        raise error

    return act


# The condition, its cause and since when.


def test_an_idle_line_with_nothing_waiting_takes_only_a_few_fields(
    world: World,
) -> None:
    created = world.clock.now
    world.clock.advance(90)
    assert world.beta() == {
        "line_id": "beta",
        "state": "idle",
        "condition": "idle",
        "since": due(created),
    }


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
    assert beta["condition"] == "held"
    assert beta["hold_cause"] == cause.value
    assert "held" not in beta
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
    assert beta["condition"] == "waiting"
    assert "hold_cause" not in beta
    assert world.log.state.lines["beta"].hold_cause is None
    assert beta["since"] == due(released)


def test_a_hold_recorded_before_causes_shows_its_cause_unrecorded(
    world: World,
) -> None:
    question = world.scheduler.ask("beta", ANA, "Resume beta?", ("resume",))
    world.log.append(LineHeld("beta", question))
    assert world.beta()["hold_cause"] == "unrecorded"


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
    assert (beta["hibernated"], beta["hold_cause"]) == (True, "stopped")


def test_a_line_whose_turn_failed_after_it_mailed_an_operator_shows_failed(
    world: World,
) -> None:
    """A failed turn whose line mailed an operator during it is not held, so the
    line stands as failed, which another line sees, and a later note clears it."""

    def act() -> None:
        world.scheduler.send(BETA, ANA, "this turn is about to fail")
        raise RuntimeError("the engine broke")

    world.turn("beta", act)
    assert world.log.state.lines["beta"].held_by is None
    beta = world.beta()
    assert beta["condition"] == "failed"
    assert "hold_cause" not in beta
    world.turn("beta", lambda: None)
    assert world.beta()["condition"] == "idle"


def test_mail_alone_does_not_move_the_since(world: World) -> None:
    created = world.clock.now
    world.clock.advance(30)
    world.scheduler.send(ANA, BETA, "a note")
    assert world.beta()["since"] == due(created)


def test_a_line_sees_no_entry_for_itself(world: World) -> None:
    assert [line["line_id"] for line in world.others()] == ["beta"]


def test_an_entry_gives_only_the_fields_the_protocol_names(world: World) -> None:
    world.scheduler.send(ANA, BETA, "a note")
    world.scheduler.hold("beta", HOLD_REASON, cause=HoldCause.COST)
    world.scheduler.hibernate("beta")
    beta = world.beta()
    assert set(beta) >= ALWAYS
    assert set(beta) <= ALWAYS | {
        "hold_cause",
        "hibernated",
        "since",
        "unread",
        "oldest_unread_age_seconds",
        "workdir_commit",
        "leases",
    }


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


def test_a_line_with_its_mail_all_read_shows_no_unread_fields(world: World) -> None:
    message_id = world.scheduler.send(ANA, BETA, "a note")
    world.log.append(MessageDelivered(message_id))
    world.log.append(MessageRead(message_id))
    beta = world.beta()
    assert "unread" not in beta and "oldest_unread_age_seconds" not in beta


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
    assert "unread" not in beta and "oldest_unread_age_seconds" not in beta


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


# The indexes the state keeps.


def scanned(state: State, line_id: str) -> LineMail:
    """What ``line_id``'s mail says of it by a scan of every message, the rule the
    indexes keep, so a test compares the two."""
    address = LineAddress(line_id)
    unread = [
        message_id
        for message_id, message in state.messages.items()
        if message.recipient == address
        and not message.read
        and message.sender != address
    ]
    asking = any(
        message.sender == address
        and isinstance(message.recipient, OperatorAddress)
        and message.status is QuestionStatus.OPEN
        for message in state.messages.values()
    )
    return LineMail(asking, len(unread), unread[0] if unread else None)


def every_line(state: State) -> set[str]:
    named = set(state.lines)
    for message in state.messages.values():
        for address in (message.sender, message.recipient):
            if isinstance(address, LineAddress):
                named.add(address.line_id)
    return named


def test_the_indexes_agree_with_a_scan_after_every_event(
    scenario: tuple[Event, ...],
) -> None:
    """The scenario uses every event kind, so each write of a message is reached."""
    state = State()
    for event in scenario:
        state.apply(event)
        for line_id in every_line(state):
            assert state.line_mail(line_id) == scanned(state, line_id), (
                event,
                line_id,
            )
    assert any(state.line_mail(line_id).unread for line_id in state.lines)


def test_the_indexes_hold_no_line_with_nothing_in_it(
    scenario: tuple[Event, ...],
) -> None:
    state = State()
    for event in scenario:
        state.apply(event)
        assert all(state.unread_mail.values())
        assert all(state.open_asked.values())


def test_an_index_keeps_a_message_in_its_place_as_the_message_changes(
    world: World,
) -> None:
    first = world.scheduler.send(ANA, BETA, "the first")
    world.scheduler.send(ANA, BETA, "the second")
    world.log.append(MessageDelivered(first))
    assert world.log.state.line_mail("beta").oldest_unread == first


def test_a_copy_of_the_state_shares_no_index(world: World) -> None:
    world.scheduler.send(ANA, BETA, "a note")
    world.scheduler.ask("beta", ANA, "a question")
    state = world.log.state
    trial = state.copy()
    trial.unread_mail["beta"].clear()
    trial.open_asked["beta"].clear()
    assert state.line_mail("beta") == scanned(state, "beta")
    assert world.log.line_mail("beta") == scanned(world.log.state, "beta")


def test_a_question_answered_leaves_the_line_waiting_on_nothing(world: World) -> None:
    question = world.scheduler.ask("beta", ANA, "a question")
    assert world.beta()["condition"] == "waiting"
    world.log.append(
        MessageSent("a-1", ANA, BETA, MessageKind.NOTE, "yes", (), question)
    )
    assert world.log.line_mail("beta").asking is False
    assert world.beta()["condition"] == "idle"


class Unscannable(dict[str, Message]):
    """A message map that fails any scan, so a test shows that a view reads none."""

    def __iter__(self) -> NoReturn:
        raise AssertionError("the messages were scanned")

    def values(self) -> NoReturn:
        raise AssertionError("the messages were scanned")

    def items(self) -> NoReturn:
        raise AssertionError("the messages were scanned")


def test_the_condition_and_the_view_scan_no_message(world: World) -> None:
    world.scheduler.send(ANA, BETA, "a note")
    world.scheduler.ask("beta", ANA, "a question")
    state = world.log.state
    real = world.log._state  # pyright: ignore[reportPrivateUsage]
    real.messages = Unscannable(real.messages)
    state.messages = Unscannable(state.messages)
    assert world.scheduler.condition("beta") == "waiting"
    (beta,) = world.scheduler._other_lines(state, "alpha")  # pyright: ignore[reportPrivateUsage]
    assert (beta["condition"], beta["unread"]) == ("waiting", 1)


@pytest.mark.parametrize("args", [{}, {"detail": True}])
def test_a_call_without_the_lists_builds_no_view(
    world: World, monkeypatch: pytest.MonkeyPatch, args: dict[str, object]
) -> None:
    def refuse(state: State, line_id: str) -> list[dict[str, object]]:
        raise AssertionError("the view was built")

    monkeypatch.setattr(world.scheduler, "_other_lines", refuse)
    status = json.loads(world.call(args))
    assert status["other_line_count"] == 1
    assert "other_lines" not in status


def test_message_times_are_kept_only_for_unread_line_mail(world: World) -> None:
    """The times map is bounded by the unread mail, not by every message held."""
    to_beta = world.scheduler.send(ANA, BETA, "a note")
    own = world.scheduler.send(BETA, BETA, "a note to myself")
    question = world.scheduler.ask("beta", ANA, "a question")
    times = world.log.times.message_recorded
    assert to_beta in times
    assert own not in times and question not in times
    world.log.append(MessageDelivered(to_beta))
    world.log.append(MessageRead(to_beta))
    assert to_beta not in times
    world.restart()
    assert world.log.times.message_recorded == {}


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
    assert after == before
    assert after["since"] == due(held_at)
    assert after["hold_cause"] == "plan_limit"
    assert after["oldest_unread_age_seconds"] == 60


# Leases.


def test_an_expired_lease_leaves_the_view_as_the_lease_becomes_free(
    world: World,
) -> None:
    """Re-review of r13. A lease past its expiry needs no event to end, so the view
    must judge it by the clock, as ``_live_lease`` does, and the two must agree. At
    its expiry exactly a lease is free."""
    now = world.clock.now
    world.log.append(
        LeaseAcquired("gate", "beta", due(now), due(now + timedelta(seconds=60)))
    )
    world.log.append(
        LeaseAcquired("device", "beta", due(now), due(now + timedelta(seconds=600)))
    )
    live = world.scheduler._live_lease  # pyright: ignore[reportPrivateUsage]
    assert world.beta()["leases"] == ["device", "gate"]
    world.clock.advance(60)
    assert world.beta()["leases"] == ["device"]
    assert live("gate") is None and live("device") is not None
    world.clock.advance(540)
    assert "leases" not in world.beta()
    assert live("device") is None


# The response bound.


def many(world: World, count: int) -> list[str]:
    names = [f"peer-{index:03d}" for index in range(count)]
    for name in names:
        world.scheduler.create_line(name, HAIKU, Strategy.SUBSCRIPTION)
    return ["beta", *names]


@pytest.mark.parametrize("args", [{"lists": True}, {"detail": True, "lists": True}])
def test_twenty_other_lines_are_all_shown(
    world: World, args: dict[str, object]
) -> None:
    """Review r13, T1. The view vanished at about seventeen other lines."""
    expected = many(world, 19)
    status = json.loads(world.call(args))
    lists = status.get("lists", status)
    assert [entry["line_id"] for entry in lists["other_lines"]] == expected
    assert lists["other_lines_omitted"] == 0
    assert "status_truncated" not in status


@pytest.mark.parametrize("args", [{"lists": True}, {"detail": True, "lists": True}])
def test_a_view_past_the_bound_keeps_the_first_lines_and_counts_the_rest(
    world: World, args: dict[str, object]
) -> None:
    expected = many(world, 99)
    text = world.call(args)
    assert len(text.encode("utf-8")) <= MAX_LINE_STATUS_DEFAULT_BYTES
    status = json.loads(text)
    lists = status.get("lists", status)
    shown = [entry["line_id"] for entry in lists["other_lines"]]
    assert shown, "the view must never be empty while other lines exist"
    assert shown == expected[: len(shown)]
    assert len(shown) + lists["other_lines_omitted"] == len(expected)
    assert lists["other_lines_omitted"] > 0
    assert status["other_line_count"] == len(expected)


def make_held(world: World, line_id: str) -> None:
    world.scheduler.hold(line_id, HOLD_REASON, cause=HoldCause.COST)


def make_failed(world: World, line_id: str) -> None:
    def act() -> None:
        world.scheduler.send(LineAddress(line_id), ANA, "this turn is about to fail")
        raise RuntimeError("the engine broke")

    world.turn(line_id, act)
    # The failed turn leaves its note unread. Reading it here leaves the failure as
    # the only thing the line's entry says.
    for message_id in list(world.log.state.unread_mail.get(line_id, {})):
        if not world.log.state.messages[message_id].delivered:
            world.log.append(MessageDelivered(message_id))
        world.log.append(MessageRead(message_id))


def make_unread(world: World, line_id: str) -> None:
    world.scheduler.send(ANA, LineAddress(line_id), "a note left unread")


@pytest.mark.parametrize(
    ("make", "says"),
    [
        (make_held, ("hold_cause", "cost")),
        (make_failed, ("condition", "failed")),
        (make_unread, ("unread", 1)),
    ],
)
@pytest.mark.parametrize("args", [{"lists": True}, {"detail": True, "lists": True}])
def test_a_line_that_says_something_survives_the_bound_though_it_sorts_last(
    world: World,
    make: Callable[[World, str], None],
    says: tuple[str, object],
    args: dict[str, object],
) -> None:
    """Re-review of r13. Past the bound, a held line, a failed one, or one with
    unread mail is kept before the idle lines, and the lines kept are still given in
    the order of their identifiers."""
    idle = many(world, 99)
    world.scheduler.create_line("zeta", HAIKU, Strategy.SUBSCRIPTION)
    make(world, "zeta")
    text = world.call(args)
    assert len(text.encode("utf-8")) <= MAX_LINE_STATUS_DEFAULT_BYTES
    status = json.loads(text)
    lists = status.get("lists", status)
    entries = cast(list[dict[str, object]], lists["other_lines"])
    shown = [cast(str, entry["line_id"]) for entry in entries]
    assert shown[-1] == "zeta"
    key, value = says
    zeta = entries[-1]
    assert zeta[key] == value
    reasons = {name for name in ("hold_cause", "unread") if name in zeta}
    if zeta["condition"] == "failed":
        reasons.add("condition")
    assert reasons == {key}, "each case must be kept for its own reason alone"
    assert shown[:-1] == idle[: len(shown) - 1]
    assert shown == sorted(shown)
    assert lists["other_lines_omitted"] > 0
    assert len(shown) + lists["other_lines_omitted"] == len(idle) + 1


def test_a_hibernated_or_waiting_line_is_not_kept_before_the_rest(
    world: World,
) -> None:
    """Only a hold, a failed turn and unread mail move a line forward, so a line
    that is merely hibernated or waiting on an operator keeps its place."""
    idle = many(world, 99)
    world.scheduler.create_line("zeta", HAIKU, Strategy.SUBSCRIPTION)
    world.scheduler.hibernate("zeta")
    world.scheduler.ask("zeta", ANA, "a question waiting on the operator")
    status = json.loads(world.call({"lists": True}))
    lists = status.get("lists", status)
    shown = [entry["line_id"] for entry in lists["other_lines"]]
    assert lists["other_lines_omitted"] > 0
    assert shown == idle[: len(shown)]
    assert "zeta" not in shown


def test_the_most_lines_that_fit_are_kept(world: World) -> None:
    """One more entry would pass the bound, so the bisection kept the most."""
    many(world, 99)
    full = world.scheduler.line_status("alpha")
    status = json.loads(world.call({"detail": True, "lists": True}))
    kept = len(status["other_lines"])
    entries = cast(list[dict[str, object]], full["other_lines"])
    one_more = scheduler_module._with_other_lines(  # pyright: ignore[reportPrivateUsage]
        full, entries, kept + 1
    )
    assert scheduler_module._status_size(one_more) > MAX_LINE_STATUS_DEFAULT_BYTES  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("detail", [False, True])
def test_other_lists_past_the_bound_still_leave_other_lines(
    world: World, monkeypatch: pytest.MonkeyPatch, detail: bool
) -> None:
    """When the other lists alone pass the bound, the status is truncated as before,
    and still carries the other lines."""
    many(world, 3)
    original = world.scheduler._detailed_line_status  # pyright: ignore[reportPrivateUsage]

    def crowded(line_id: str, *, others: bool = True) -> dict[str, object]:
        status = original(line_id, others=others)
        status["recent_turns"] = [{"subtype": "x" * 1_000} for _ in range(20)]
        return status

    monkeypatch.setattr(world.scheduler, "_detailed_line_status", crowded)
    text = world.call({"detail": detail, "lists": True})
    assert len(text.encode("utf-8")) <= MAX_LINE_STATUS_DEFAULT_BYTES
    status = json.loads(text)
    assert status["status_truncated"] is True
    assert status["lists_truncated"] is True
    assert [entry["line_id"] for entry in status["other_lines"]] == [
        "beta",
        "peer-000",
        "peer-001",
        "peer-002",
    ]
    assert status["other_lines_omitted"] == 0


def test_an_entry_too_large_alone_is_given_with_a_lease_count(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = world.scheduler._other_lines  # pyright: ignore[reportPrivateUsage]

    def leasing(state: State, line_id: str) -> list[dict[str, object]]:
        entries = original(state, line_id)
        for entry in entries:
            entry["leases"] = [f"lease-{index:03d}-" + "x" * 50 for index in range(256)]
        return entries

    monkeypatch.setattr(world.scheduler, "_other_lines", leasing)
    text = world.call({"detail": True, "lists": True})
    assert len(text.encode("utf-8")) <= MAX_LINE_STATUS_DEFAULT_BYTES
    status = json.loads(text)
    (beta,) = status["other_lines"]
    assert beta["line_id"] == "beta"
    assert beta["lease_count"] == 256 and "leases" not in beta


# Each hold site and its cause.


def test_the_autonomous_turn_limit_records_its_cause(world: World) -> None:
    for _ in range(5):
        world.scheduler._apply_limits(  # pyright: ignore[reportPrivateUsage]
            "beta", scheduler_module.TurnCause.AUTONOMOUS, True, None
        )
    assert world.log.state.lines["beta"].hold_cause is HoldCause.AUTONOMOUS_TURNS
    assert world.beta()["hold_cause"] == "autonomous_turns"


def test_the_cost_limit_of_a_turn_records_its_cause(world: World) -> None:
    report = TurnReport("success", 1, "ok", (), None, None, "session-1", None, 4.0)
    world.scheduler._apply_limits(  # pyright: ignore[reportPrivateUsage]
        "beta", scheduler_module.TurnCause.OPERATOR, True, report
    )
    assert world.log.state.lines["beta"].hold_cause is HoldCause.COST


def test_the_cost_limit_between_turns_records_its_cause(world: World) -> None:
    world.scheduler.spent_between("beta", "compaction", 4.0, None)
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


def test_a_changed_git_ssh_file_records_its_cause(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def changed(access: GitSshAccess) -> str:
        del access
        return "known_hosts"

    monkeypatch.setattr(gitssh, "changed", changed)
    world.scheduler.set_git_ssh({"beta": cast(GitSshAccess, object())})
    world.turn("beta", lambda: None)
    assert world.log.state.lines["beta"].hold_cause is HoldCause.GIT_SSH_CHANGED
    assert issubclass(scheduler_module.GitSshChanged, TurnRefused)


@pytest.mark.parametrize(
    ("may_move_role", "cause"),
    [(True, HoldCause.PLAN_LIMIT), (False, HoldCause.INFERRED_LIMIT)],
)
def test_a_plan_limit_in_a_turn_records_its_cause(
    world: World, may_move_role: bool, cause: HoldCause
) -> None:
    error = EnginePlanLimit("plan limit reached", may_move_role=may_move_role)
    world.turn("beta", raising(error))
    assert world.log.state.lines["beta"].hold_cause is cause


@pytest.mark.parametrize(
    ("may_move_role", "cause"),
    [(True, HoldCause.PLAN_LIMIT), (False, HoldCause.INFERRED_LIMIT)],
)
def test_a_plan_limit_in_a_self_started_turn_records_its_cause(
    world: World, may_move_role: bool, cause: HoldCause
) -> None:
    error = EnginePlanLimit("plan limit reached", may_move_role=may_move_role)

    async def go() -> None:
        completion: asyncio.Future[tuple[TurnReport | None, Exception | None]] = (
            asyncio.get_running_loop().create_future()
        )
        completion.set_result((None, error))
        world.log.append(LineStateChanged("beta", LineState.RUNNING))
        await world.scheduler._self_started_turn("beta", completion, 0)  # pyright: ignore[reportPrivateUsage]

    asyncio.run(go())
    assert world.log.state.lines["beta"].hold_cause is cause


@pytest.mark.parametrize(
    ("error", "held"),
    [
        (ConfinementBreach("a Read escaped its confinement"), True),
        (RuntimeError("the engine broke"), False),
    ],
)
def test_a_self_started_turn_is_held_for_a_breach_even_after_operator_mail(
    world: World, error: Exception, held: bool
) -> None:
    """Round 15, review of e9adf91. A count of earlier operator mail that differs
    from the current one stands for mail sent during the turn. It exempts an ordinary
    failure from the hold and does not exempt a confinement breach."""

    async def go() -> None:
        completion: asyncio.Future[tuple[TurnReport | None, Exception | None]] = (
            asyncio.get_running_loop().create_future()
        )
        completion.set_result((None, error))
        world.log.append(LineStateChanged("beta", LineState.RUNNING))
        await world.scheduler._self_started_turn("beta", completion, -1)  # pyright: ignore[reportPrivateUsage]

    asyncio.run(go())
    line = world.log.state.lines["beta"]
    assert (line.held_by is not None) is held
    if held:
        assert line.hold_cause is HoldCause.TURN_FAILED
        body = world.log.state.messages[line.held_by or ""].body
        assert "confinement breach" in body


def test_a_failed_turn_without_operator_mail_records_its_cause(world: World) -> None:
    world.turn("beta", raising(RuntimeError("the engine broke")))
    assert world.log.state.lines["beta"].hold_cause is HoldCause.TURN_FAILED
    assert world.beta()["condition"] == "held"


# The wire.


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
