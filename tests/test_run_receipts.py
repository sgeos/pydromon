"""Task of 2026-11-05, item 2, run receipts. A run's end is recorded once its
supervisor exits, ``run_status`` never shows a finished output as running,
``wake_after`` waits for a run, and a failed run keeps the tail of its output.
``docs/architecture/RUNS.md`` is normative. The helpers mirror those of
``test_runs.py``, which no test imports."""

import asyncio
import os
import sys
import time
from collections.abc import Generator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import runs
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.model import (
    HARNESS,
    MAX_RUN_TAIL_BYTES,
    InvalidField,
    LineAddress,
    RunEnded,
    RunStatus,
)
from dromon.scheduler import (
    RUN_STATUS_TOOL_NAME,
    LineEngine,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
AT = "2026-10-31T09:00:00Z"
WAIT_SECONDS = 20


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


def stand_in_start(pid: int) -> str | None:
    """``AT`` while ``pid`` answers a signal, as the start time the table gives."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return None
    return AT


def stand_in_occupied(session: int) -> bool:
    try:
        os.killpg(session, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def python_command(source: str) -> tuple[str, ...]:
    return (sys.executable, "-c", source)


FAILING = python_command(
    "import sys\n"
    "print('collecting')\n"
    "print('\\x1b[31mE   assert 1 == 2\\x1b[0m')\n"
    "print('FAILED tests/test_x.py::test_y')\n"
    "sys.exit(1)"
)


@pytest.fixture
def project(tmp_path: Path) -> Generator[Path]:
    (tmp_path / "reports").mkdir()
    yield tmp_path


def scheduler_on(log: EventLog, root: Path) -> Scheduler:
    found = Scheduler(
        log,
        no_engine,
        hold_wakes=True,
        wall_clock=Clock(),
        root=root,
        report_dir=root / "reports",
        run_start_of=stand_in_start,
        run_occupied=stand_in_occupied,
    )
    found.set_runs(
        {
            "gate": python_command("print('gate passed')"),
            "failing": FAILING,
            "wait": python_command("import time; time.sleep(60)"),
        }
    )
    found.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
    return found


def wait_for_supervisor(scheduler: Scheduler, run_id: str) -> None:
    children = scheduler._run_children  # pyright: ignore[reportPrivateUsage]
    child = children.get(run_id)
    if child is not None:
        child.wait(timeout=WAIT_SECONDS)


def harness_notes(log: EventLog, line_id: str) -> list[str]:
    return [
        message.body
        for message in log.state.messages.values()
        if message.sender == HARNESS and message.recipient == LineAddress(line_id)
    ]


def tool(scheduler: Scheduler, name: str) -> SdkMcpTool[Any]:
    tools = {found.name: found for found in scheduler.own_tools("alpha")}
    return tools[name]


async def call(handler: SdkMcpTool[Any], args: dict[str, object]) -> tuple[str, bool]:
    result = await handler.handler(args)
    return str(result["content"][0]["text"]), result.get("is_error") is True


# The tail of a failed run.


def test_a_failure_tail_keeps_the_last_printable_lines() -> None:
    raw = b"".join(f"line {n}\n".encode() for n in range(30))
    tail = runs.failure_tail(raw + b"\n\n   \n")
    assert tail is not None
    lines = tail.splitlines()
    assert lines == [f"line {n}" for n in range(30 - runs.MAX_TAIL_LINES, 30)]


def test_a_failure_tail_strips_control_characters_and_keeps_indentation() -> None:
    tail = runs.failure_tail(b"Traceback\n    raise \x1b[31mError\x07\x00\r\n")
    assert tail == "Traceback\n    raise [31mError"


def test_a_failure_tail_is_bounded_in_bytes_at_a_line_boundary() -> None:
    wide = ("é" * runs.MAX_SUMMARY_LINE + "\n").encode() * runs.MAX_TAIL_LINES
    tail = runs.failure_tail(wide)
    assert tail is not None
    assert len(tail.encode()) <= MAX_RUN_TAIL_BYTES
    # Whole lines only, each cut to the summary bound.
    assert all(line == "é" * runs.MAX_SUMMARY_LINE for line in tail.splitlines())
    assert len(tail.splitlines()) < runs.MAX_TAIL_LINES


def test_an_output_with_nothing_printable_has_no_tail() -> None:
    assert runs.failure_tail(b"") is None
    assert runs.failure_tail(b"\n \n\x1b\x07\n") is None


def test_a_run_end_refuses_a_tail_over_its_bound_or_with_a_control_character() -> None:
    with pytest.raises(InvalidField, match="over the limit"):
        RunEnded("r-1", RunStatus.EXITED, 1, None, None, "", AT, "x" * 2049)
    with pytest.raises(InvalidField, match="printable"):
        RunEnded("r-1", RunStatus.EXITED, 1, None, None, "", AT, "a\x1bb")
    kept = RunEnded("r-1", RunStatus.EXITED, 1, None, None, "", AT, "a\n b")
    assert kept.output_tail == "a\n b"


def test_a_failed_run_keeps_its_tail_and_a_passed_run_none(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        failed = asyncio.run(scheduler.start_run("alpha", "failing", "none"))
        wait_for_supervisor(scheduler, failed.run_id)
        asyncio.run(scheduler.reconcile_runs())
        ended = log.state.runs[failed.run_id].ended
        assert ended is not None and ended.exit_code == 1
        assert ended.output_tail == (
            "collecting\n[31mE   assert 1 == 2[0m\nFAILED tests/test_x.py::test_y"
        )
        (note,) = harness_notes(log, "alpha")
        assert f"{runs.TAIL_LABEL}:\n| collecting\n" in note
        assert note.endswith("| FAILED tests/test_x.py::test_y")
        status = scheduler.run_status("alpha")
        assert runs.TAIL_LABEL in status and "| FAILED tests/test_x.py" in status
        passed = asyncio.run(scheduler.start_run("alpha", "gate", failed.run_id))
        wait_for_supervisor(scheduler, passed.run_id)
        asyncio.run(scheduler.reconcile_runs())
        ended = log.state.runs[passed.run_id].ended
        assert ended is not None and ended.exit_code == 0
        assert ended.output_tail is None
        # Only the latest run's tail is shown, and it passed.
        assert runs.TAIL_LABEL not in scheduler.run_status("alpha")
        (view,) = scheduler.runs_view(failed.run_id)
        assert view["output_tail"] == log.state.runs[failed.run_id].ended.output_tail  # type: ignore[union-attr]


# The end is recorded as soon as the output is complete.


def test_a_run_end_is_recorded_once_its_supervisor_exits(project: Path) -> None:
    """No reconciliation is asked for. The watcher sees the supervisor exit and
    records the end while the loop runs, well within the loop's rest."""
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)

        async def scenario() -> float:
            started = await scheduler.start_run("alpha", "gate", "none")
            began = time.monotonic()
            for _ in range(WAIT_SECONDS * 20):
                if log.state.runs[started.run_id].ended is not None:
                    return time.monotonic() - began
                await asyncio.sleep(0.05)
            raise AssertionError("the end was not recorded")

        elapsed = asyncio.run(scenario())
        assert elapsed < WAIT_SECONDS
        (run,) = log.state.runs.values()
        assert run.ended is not None and run.ended.exit_code == 0
        (note,) = harness_notes(log, "alpha")
        assert note.startswith(f"Run ended: run {run.started.run_id} of gate")


def test_run_status_never_shows_a_finished_output_as_running(project: Path) -> None:
    """The run's supervisor exited while no loop ran, so no watcher recorded it,
    and the tool reconciles before it answers."""
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        started = asyncio.run(scheduler.start_run("alpha", "gate", "none"))
        wait_for_supervisor(scheduler, started.run_id)
        assert "still running" in scheduler.run_status("alpha")
        text, error = asyncio.run(call(tool(scheduler, RUN_STATUS_TOOL_NAME), {}))
        assert not error
        assert "still running" not in text
        assert f"run {started.run_id} of gate" in text and "exit code 0" in text


def test_fresh_runs_follows_a_reconciliation_under_way(project: Path) -> None:
    """A reconciliation under way may have read before the receipt, so one more
    follows it, and both are awaited."""
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        started = asyncio.run(scheduler.start_run("alpha", "gate", "none"))
        wait_for_supervisor(scheduler, started.run_id)
        calls: list[int] = []
        original = scheduler.reconcile_runs

        async def counted() -> None:
            calls.append(len(calls))
            if len(calls) == 1:
                # The first reads nothing, as one that ran before the receipt.
                return
            await original()

        scheduler.reconcile_runs = counted  # type: ignore[method-assign]

        async def scenario() -> None:
            scheduler.check_runs()
            await scheduler.fresh_runs()

        asyncio.run(scenario())
        assert calls == [0, 1]
        assert log.state.runs[started.run_id].ended is not None


# wake_after waits for a run.


def test_a_wake_waits_for_a_run_and_its_end_cancels_it(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        started = asyncio.run(scheduler.start_run("alpha", "failing", "none"))
        wake = scheduler.wake_tool("alpha")
        text, error = asyncio.run(
            call(wake, {"seconds": 600, "note": "read it", "task_id": started.run_id})
        )
        assert not error, text
        assert f"cancelled if task {started.run_id} ends" in text
        pending = log.state.scheduled["alpha"]
        assert pending.task_id == started.run_id
        wait_for_supervisor(scheduler, started.run_id)
        asyncio.run(scheduler.reconcile_runs())
        assert "alpha" not in log.state.scheduled
        (note,) = harness_notes(log, "alpha")
        assert "exited with exit code 1" in note
        assert f"This end cancels your wake {pending.message_id}" in note
        assert pending.message_id not in log.state.messages


def test_a_wake_for_an_ended_run_is_refused_with_its_receipt(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        started = asyncio.run(scheduler.start_run("alpha", "gate", "none"))
        wait_for_supervisor(scheduler, started.run_id)
        asyncio.run(scheduler.reconcile_runs())
        text, error = asyncio.run(
            call(
                scheduler.wake_tool("alpha"),
                {"seconds": 600, "note": "x", "task_id": started.run_id},
            )
        )
        assert error and "has ended already" in text and "exit code 0" in text
        assert "alpha" not in log.state.scheduled


def test_a_wake_for_another_scopes_run_is_refused(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)
        started = asyncio.run(scheduler.start_run("alpha", "wait", "none"))
        try:
            text, error = asyncio.run(
                call(
                    scheduler.wake_tool("beta"),
                    {"seconds": 600, "note": "x", "task_id": started.run_id},
                )
            )
            assert error and "not a run of your scope" in text
            assert "beta" not in log.state.scheduled
        finally:
            asyncio.run(scheduler.stop_run("alpha", started.run_id))
            wait_for_supervisor(scheduler, started.run_id)


def test_an_unknown_task_id_still_names_the_run_choice(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        text, error = asyncio.run(
            call(
                scheduler.wake_tool("alpha"),
                {"seconds": 600, "note": "x", "task_id": "r-unknown"},
            )
        )
        assert error and "an open run of your scope" in text
