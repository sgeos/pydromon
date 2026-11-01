"""Round 14, item 3. A line that is not running and has a background job is shown as
idle with that job, in ``dromon status`` and in the other lines' view, and the status
says how the end of the job reaches the line."""

from collections.abc import Generator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"


class FakeKept:
    """The two methods that make an engine a ``KeptEngine``."""

    def __init__(self, tasks: int, kept: bool = True) -> None:
        self.tasks = tasks
        self.kept = kept
        self.broken = False

    async def retire(self) -> None:
        return None

    def kept_status(self) -> dict[str, object]:
        if self.broken:
            raise RuntimeError("the engine cannot say")
        return {"kept": self.kept, "background_tasks": self.tasks}


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start, but {spec.line_id} did")


@pytest.fixture
def scheduler(tmp_path: Path) -> Generator[Scheduler]:
    with EventLog(tmp_path / "events.sqlite") as log:
        found = Scheduler(log, no_engine, hold_wakes=True)
        found.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        found.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)
        yield found


def keep(scheduler: Scheduler, line_id: str, engine: FakeKept) -> None:
    scheduler._engines[line_id] = cast(Any, engine)  # pyright: ignore[reportPrivateUsage]


def summary(scheduler: Scheduler, line_id: str) -> Mapping[str, object]:
    return next(s for s in scheduler.line_summaries() if s["line_id"] == line_id)


def test_a_line_with_nothing_running_has_no_background_job(
    scheduler: Scheduler,
) -> None:
    assert scheduler.background_jobs("alpha") == 0
    assert scheduler.background_ends("alpha") is None
    assert summary(scheduler, "alpha")["background_jobs"] == 0
    others = scheduler.line_status("beta")["other_lines"]
    assert "background_jobs" not in cast(list[dict[str, object]], others)[0]


def test_an_idle_line_with_a_kept_task_is_idle_with_a_background_job(
    scheduler: Scheduler,
) -> None:
    keep(scheduler, "alpha", FakeKept(2))
    assert scheduler.condition("alpha") == "idle"
    assert scheduler.background_jobs("alpha") == 2
    assert summary(scheduler, "alpha")["background_jobs"] == 2
    assert summary(scheduler, "alpha")["condition"] == "idle"
    others = cast(list[dict[str, object]], scheduler.line_status("beta")["other_lines"])
    assert others[0]["condition"] == "idle"
    assert others[0]["background_jobs"] == 2
    own = scheduler.line_status("alpha")
    assert own["background_jobs"] == 2
    ends = cast(str, own["background_ends"])
    assert "cannot say when a job will end" in ends
    assert "wakes the line with a task note" in ends
    compact = scheduler.line_status("alpha", detail=False, lists=False)
    assert compact["background_jobs"] == 2 and compact["background_ends"] == ends


def test_a_line_that_is_running_counts_no_background_job(
    scheduler: Scheduler,
) -> None:
    keep(scheduler, "alpha", FakeKept(1))
    scheduler._running["alpha"] = cast(Any, object())  # pyright: ignore[reportPrivateUsage]
    assert scheduler.background_jobs("alpha") == 0
    scheduler._running.pop("alpha")  # pyright: ignore[reportPrivateUsage]
    assert scheduler.background_jobs("alpha") == 1


def test_an_engine_that_is_not_kept_or_cannot_say_counts_no_job(
    scheduler: Scheduler,
) -> None:
    keep(scheduler, "alpha", FakeKept(3, kept=False))
    assert scheduler.background_jobs("alpha") == 0
    broken = FakeKept(3)
    broken.broken = True
    keep(scheduler, "alpha", broken)
    assert scheduler.background_jobs("alpha") == 0


def test_processes_left_by_an_earlier_turn_count_as_background_jobs(
    scheduler: Scheduler,
) -> None:
    scheduler._detached["alpha"] = (4400, 4401)  # pyright: ignore[reportPrivateUsage]
    keep(scheduler, "alpha", FakeKept(1))
    assert scheduler.background_jobs("alpha") == 2
    keep(scheduler, "alpha", FakeKept(5))
    assert scheduler.background_jobs("alpha") == 5


def test_the_status_names_the_task_a_line_waits_for(scheduler: Scheduler) -> None:
    keep(scheduler, "alpha", FakeKept(1))
    scheduler.schedule_wake("alpha", 60, "look", "task-1")
    pending = scheduler._log.state.scheduled["alpha"]  # pyright: ignore[reportPrivateUsage]
    ends = scheduler.background_ends("alpha")
    assert ends is not None
    assert "waits for task-1" in ends
    assert pending.message_id in ends and pending.due in ends


@pytest.mark.parametrize(
    ("line", "text"),
    [
        (
            {"line_id": "a", "condition": "idle", "background_jobs": 1, "pending": 0},
            "line a, idle with 1 background job, 0 pending",
        ),
        (
            {"line_id": "a", "condition": "idle", "background_jobs": 3, "pending": 2},
            "line a, idle with 3 background jobs, 2 pending",
        ),
        (
            {"line_id": "a", "condition": "idle", "background_jobs": 0, "pending": 0},
            "line a, idle, 0 pending",
        ),
        (
            {"line_id": "a", "condition": "held", "background_jobs": 2, "pending": 0},
            "line a, held, 0 pending",
        ),
        (
            {
                "line_id": "a",
                "condition": "idle",
                "background_jobs": True,
                "pending": 0,
            },
            "line a, idle, 0 pending",
        ),
    ],
)
def test_dromon_status_says_idle_with_a_background_job(
    line: dict[str, object], text: str
) -> None:
    assert cli._line_row(line) == text  # pyright: ignore[reportPrivateUsage]
