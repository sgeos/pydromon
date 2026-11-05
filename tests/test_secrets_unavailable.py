"""Review of 151cbcd. Every caller of ``EventLog.append`` that can meet
``credentials.SecretsUnavailable`` fails safe and says so.

The log refuses an event with text when the source of literal secret values raises.
The callers fall in three kinds, and each is held to its kind here.

- A tool a line or the operator calls returns the refusal as an error that names the
  failure, and stores nothing. The tests call the tools of ``send_message``,
  ``wake_after``, ``set_brief``, ``ask_operator`` and ``acknowledge_mail``.
- An event the daemon makes for itself is dropped with a line on the standard error that
  names what was lost and never its text, and the flow it was part of goes on with
  whatever it had begun undone. These are the walk gap, the restart note, the engine
  stop note, a budget stated from an engine's report, a run's note to its holder and a
  task end's note. A task end is noted when it is reported again.
- Reads that already caught ``EventRejected`` keep doing so, as ``_record_task``,
  ``_record_unverified``, ``_record_left`` and ``_record_commit_refusal`` do.

Review of 7744806. About seventy appends in the scheduler can meet the refusal, and one
guard, ``Scheduler._refusing_on_secrets``, stands at the tick. Each due wake and each
line's update is an action that ends alone when the log refuses it. The other actions
of the tick go on, the loop does not pause as for a fault, and the refused action is
tried again. The tests below drive an ordinary tick and the run loop through it.

Not covered by the guard are the tools and operator requests, which meet the refusal
in their own task and return it as an error, and an event appended in a turn's own
task. The forwarder's outage note is the forwarder's own. All of them keep their state
unchanged by a refusal, so each tries again.
"""

import asyncio
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from dromon import credentials, spawn
from dromon import scheduler as scheduler_module
from dromon.driver import Strategy
from dromon.engine import BackgroundEnd, TurnReport
from dromon.eventlog import EventLog, read_entries
from dromon.model import HARNESS, LineAddress, MessageSent, WakeRequested
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
FAILURE = "the literal secret values could not be read"


def no_engine(spec: LineSpec, tools: Any) -> LineEngine:  # noqa: ANN401
    raise AssertionError(f"no engine may start, but {spec.line_id} did")


def broken() -> list[str]:
    raise RuntimeError("no source")


@pytest.fixture
def world(tmp_path: Path) -> Generator[Scheduler]:
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)
        scheduler.ensure_operator(scheduler._default_operator.operator_id)  # pyright: ignore[reportPrivateUsage]
        yield scheduler


def failing(scheduler: Scheduler) -> int:
    """Make the source fail, and return the log's length before anything is tried."""
    log = scheduler._log  # pyright: ignore[reportPrivateUsage]
    log.secrets = broken
    return log.last_seq


def length(scheduler: Scheduler) -> int:
    return scheduler._log.last_seq  # pyright: ignore[reportPrivateUsage]


def call(scheduler: Scheduler, name: str, args: dict[str, object]) -> tuple[str, bool]:
    tool = next(t for t in scheduler.own_tools("alpha") if t.name == name)

    async def invoke() -> dict[str, Any]:
        return await tool.handler(args)

    result = asyncio.run(invoke())
    return cast(str, result["content"][0]["text"]), result.get("is_error") is True


# Tools.


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("send_message", {"to": "beta", "body": "hello"}),
        ("wake_after", {"seconds": 60, "note": "later"}),
        ("set_brief", {"task": "a task"}),
        ("ask_operator", {"question": "what?", "to": "@operator"}),
    ],
)
def test_a_tool_returns_the_refusal_as_an_error_and_stores_nothing(
    world: Scheduler, name: str, args: dict[str, object]
) -> None:
    before = failing(world)
    text, error = call(world, name, args)
    assert error and FAILURE in text and "RuntimeError" in text
    assert "no source" not in text, "the message of the failure is not echoed"
    assert length(world) == before


def test_acknowledging_mail_with_a_reason_is_refused_and_leaves_it_unacknowledged(
    world: Scheduler,
) -> None:
    message_id = world.send(LineAddress("beta"), LineAddress("alpha"), "hi")
    before = failing(world)
    text, error = call(
        world, "acknowledge_mail", {"message_id": message_id, "reason": "done"}
    )
    assert error and FAILURE in text
    assert not world._state().messages[message_id].acknowledged  # pyright: ignore[reportPrivateUsage]
    assert length(world) <= before + 1, "at most the delivery, which has no text"


# Events the daemon makes for itself.


def test_the_walk_gap_is_dropped_with_a_line_on_the_error_stream(
    world: Scheduler, capsys: pytest.CaptureFixture[str]
) -> None:
    before = failing(world)
    world.record_walk_gap("a gap that names /secret/path")
    assert length(world) == before
    shown = capsys.readouterr().err
    assert "the walk gap was not recorded" in shown and FAILURE in shown
    assert "/secret/path" not in shown


def test_a_restart_note_is_dropped_and_the_turn_end_goes_on(
    world: Scheduler, capsys: pytest.CaptureFixture[str]
) -> None:
    before = failing(world)
    world._note_restart("alpha")  # pyright: ignore[reportPrivateUsage]
    assert length(world) == before
    assert "the restart note of alpha was not recorded" in capsys.readouterr().err
    assert not world._sent  # pyright: ignore[reportPrivateUsage]


def test_an_engine_stop_note_is_dropped_with_nothing_delivered(
    world: Scheduler, capsys: pytest.CaptureFixture[str]
) -> None:
    before = failing(world)
    world._note_engine_stop("alpha", "stopped")  # pyright: ignore[reportPrivateUsage]
    assert length(world) == before
    assert "the engine stop note of alpha was not recorded" in capsys.readouterr().err


def test_a_budget_an_engine_reports_is_dropped_and_not_acted_on(
    world: Scheduler, capsys: pytest.CaptureFixture[str]
) -> None:
    before = failing(world)
    world.credits_spent("alpha", "no credit left")
    assert length(world) == before
    assert "the budget of" in capsys.readouterr().err
    # Stated again once the source recovers, since nothing was recorded.
    world._log.secrets = lambda: []  # pyright: ignore[reportPrivateUsage]
    world.credits_spent("alpha", "no credit left")
    assert length(world) == before + 1


def test_a_task_end_is_not_noted_while_the_source_fails_and_is_when_it_recovers(
    world: Scheduler, capsys: pytest.CaptureFixture[str]
) -> None:
    end = BackgroundEnd("t1", "make gate", "completed")
    before = failing(world)
    assert world.background_ended("alpha", end) is None
    assert "was not recorded" in capsys.readouterr().err
    assert length(world) == before
    world._log.secrets = lambda: []  # pyright: ignore[reportPrivateUsage]
    assert world.background_ended("alpha", end) is not None, "reported again, noted"


def test_a_runs_note_to_its_holder_is_dropped_and_the_sweep_goes_on(
    world: Scheduler, capsys: pytest.CaptureFixture[str]
) -> None:
    run = SimpleNamespace(started=SimpleNamespace(role_id=None, line_id="alpha"))
    before = failing(world)
    world._note_run_holder(cast(Any, run), "Run ended: x")  # pyright: ignore[reportPrivateUsage]
    assert length(world) == before
    assert "the note of a run to alpha was not recorded" in capsys.readouterr().err


# The loop's note of stuck starts.


def operator_notes(scheduler: Scheduler) -> list[str]:
    return [
        message.body
        for message in scheduler._state().messages.values()  # pyright: ignore[reportPrivateUsage]
        if message.sender == HARNESS
        and message.recipient == scheduler._default_operator  # pyright: ignore[reportPrivateUsage]
    ]


def test_the_operator_is_told_once_when_the_stuck_starts_reach_the_bound(
    world: Scheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    count = [0]
    monkeypatch.setattr(spawn, "stranded_count", lambda: count[0])
    world.note_stuck_starts()
    assert operator_notes(world) == []
    count[0] = spawn.MAX_STRANDED
    world.note_stuck_starts()
    world.note_stuck_starts()
    notes = operator_notes(world)
    assert notes == [spawn.stuck_text(spawn.MAX_STRANDED)]
    assert "restart" in notes[0] and "stop or a hold" in notes[0]
    # Slots free themselves, and a later episode is told again.
    count[0] = 0
    world.note_stuck_starts()
    count[0] = spawn.MAX_STRANDED
    world.note_stuck_starts()
    assert len(operator_notes(world)) == 2


def test_a_note_of_stuck_starts_that_cannot_be_recorded_is_tried_again(
    world: Scheduler,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(spawn, "stranded_count", lambda: spawn.MAX_STRANDED)
    failing(world)
    world.note_stuck_starts()
    assert operator_notes(world) == []
    assert "stuck starts were not reported" in capsys.readouterr().err
    world._log.secrets = lambda: []  # pyright: ignore[reportPrivateUsage]
    world.note_stuck_starts()
    assert len(operator_notes(world)) == 1


def test_the_exception_is_an_event_rejected() -> None:
    from dromon.model import EventRejected

    assert issubclass(credentials.SecretsUnavailable, EventRejected)


# Review of 7744806. The tick.


class Quiet:
    """A line engine whose turn ends at once with nothing to say."""

    def __init__(self) -> None:
        self.session_id: str | None = None

    async def run_turn(self, prompt: str) -> TurnReport:
        self.session_id = "session-1"
        return TurnReport("success", 1, "ok", (), None, None, "session-1", None, None)


NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def clock() -> list[datetime]:
    return [NOW]


def due(scheduler: Scheduler, clock: list[datetime], note: str) -> None:
    """Schedule alpha's wake and let the clock pass its due time."""
    scheduler.schedule_wake("alpha", 0, note, minimum=0)
    clock[0] += timedelta(seconds=5)


@pytest.fixture
def ticking(tmp_path: Path, clock: list[datetime]) -> Generator[Scheduler]:
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(
            log,
            lambda spec, tools: Quiet(),
            tick_seconds=0.01,
            wall_clock=lambda: clock[0],
        )
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)
        scheduler.ensure_operator(scheduler._default_operator.operator_id)  # pyright: ignore[reportPrivateUsage]
        yield scheduler


def recorded(scheduler: Scheduler, kind: type) -> list[Any]:
    return [
        entry.event
        for entry in read_entries(scheduler._log.path)  # pyright: ignore[reportPrivateUsage]
        if isinstance(entry.event, kind)
    ]


def test_a_refused_wake_ends_alone_and_the_other_line_still_starts_its_turn(
    ticking: Scheduler, clock: list[datetime], capsys: pytest.CaptureFixture[str]
) -> None:
    """The wake of alpha has text, so it cannot be recorded. Beta's mail was recorded
    earlier, and delivering it has none, so beta's turn begins in the same tick."""
    ticking.send(LineAddress("alpha"), LineAddress("beta"), "hello")
    due(ticking, clock, "later")
    failing(ticking)

    async def scenario() -> None:
        ticking.step()
        ticking.step()
        await asyncio.gather(*ticking._running.values())  # pyright: ignore[reportPrivateUsage]

    asyncio.run(scenario())
    assert ticking.fault is None and ticking.faults == 0, "a refusal is not a fault"
    assert ticking.secret_refusals == 2
    assert [w.line_id for w in recorded(ticking, WakeRequested)] == ["beta"]
    assert "alpha" in ticking._state().scheduled, "the wake stays owed"  # pyright: ignore[reportPrivateUsage]
    shown = capsys.readouterr().err
    assert shown.count("was refused, because the literal secret values") == 1
    assert "the wake of line alpha" in shown and "later" not in shown


def test_the_refused_wake_fires_at_a_later_tick_once_the_source_recovers(
    ticking: Scheduler, clock: list[datetime], capsys: pytest.CaptureFixture[str]
) -> None:
    def owed() -> dict[str, Any]:
        return dict(ticking._state().scheduled)  # pyright: ignore[reportPrivateUsage]

    async def scenario() -> None:
        due(ticking, clock, "later")
        failing(ticking)
        ticking.step()
        assert "alpha" in owed()
        ticking._log.secrets = lambda: []  # pyright: ignore[reportPrivateUsage]
        ticking.step()
        assert "alpha" not in owed(), "the wake fired"
        await asyncio.gather(*ticking._running.values())  # pyright: ignore[reportPrivateUsage]
        # The episode is over, so a later one is said again.
        due(ticking, clock, "again")
        failing(ticking)
        ticking.step()

    asyncio.run(scenario())
    assert [m.body for m in recorded(ticking, MessageSent)] == ["later"]
    assert capsys.readouterr().err.count("was refused") == 2


def test_the_loop_rests_and_goes_on_without_a_fault_while_the_source_fails(
    ticking: Scheduler, clock: list[datetime], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "SECRETS_RETRY_SECONDS", 0.01)
    due(ticking, clock, "later")
    failing(ticking)

    async def scenario() -> None:
        stop = asyncio.Event()
        task = asyncio.create_task(ticking.run(stop))
        await asyncio.sleep(0.3)
        refused = ticking.secret_refusals
        ticking._log.secrets = lambda: []  # pyright: ignore[reportPrivateUsage]
        await asyncio.sleep(0.3)
        stop.set()
        await task
        assert refused >= 2, "the wake was tried again, and not once"

    asyncio.run(scenario())
    assert ticking.faults == 0 and ticking.fault is None
    assert [m.body for m in recorded(ticking, MessageSent)] == ["later"]


def test_another_exception_in_an_action_is_still_a_fault(
    ticking: Scheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard names one failure and hides no other."""

    def broken_update(line_id: str) -> None:
        raise RuntimeError("a bug")

    monkeypatch.setattr(ticking, "_update_line", broken_update)
    with pytest.raises(RuntimeError, match="a bug"):
        ticking.step()
    assert ticking.secret_refusals == 0
