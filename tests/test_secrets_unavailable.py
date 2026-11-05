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

Left to the loop's fault handling, which pauses and tries again, and not tested here,
are the delivery of an owed wake and the forwarder's outage note. Both retry, since
their state is unchanged by a refusal.
"""

import asyncio
from collections.abc import Generator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from dromon import credentials, spawn
from dromon.driver import Strategy
from dromon.engine import BackgroundEnd
from dromon.eventlog import EventLog
from dromon.model import HARNESS, LineAddress
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
