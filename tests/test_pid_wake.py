"""Round 14, item 5. ``wake_after`` accepts ``pid:NUMBER``, the task identifier that
``list_processes`` gives a process of the line's own, and the end of that process
cancels the wake and wakes the line, as the end of an engine's task does."""

import asyncio
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import processes, scheduler as scheduler_module
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.model import TaskRecorded
from dromon.processes import (
    MARK_VARIABLE,
    ProcessInfo,
    ProcessRefused,
    pid_of_task,
    pid_task_id,
)
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
DAEMON = 1000


class Table:
    """A process table whose rows and environments a test changes between looks."""

    def __init__(self) -> None:
        self.rows: list[ProcessInfo] = [ProcessInfo(DAEMON, 1, "01:00", "dromond")]
        self.envs: dict[int, list[str]] = {DAEMON: ["PATH=/bin"]}
        self.unreadable = False

    def own_processes(self) -> Sequence[ProcessInfo]:
        return list(self.rows)

    def has_variable(self, pid: int, assignment: str) -> bool:
        return assignment in self.envs.get(pid, ())

    def environments(self) -> dict[int, list[str]]:
        if self.unreadable:
            raise ProcessRefused("ps did not answer within 10 seconds")
        return dict(self.envs)

    def send(self, pid: int, signal_number: int) -> None:
        raise AssertionError("no process is signalled here")

    def start(self, pid: int, mark: str) -> None:
        self.rows.append(ProcessInfo(pid, 1, "00:01", f"sleep {pid}"))
        self.envs[pid] = [f"{MARK_VARIABLE}={mark}"]

    def end(self, pid: int) -> None:
        self.rows = [row for row in self.rows if row.pid != pid]
        self.envs.pop(pid, None)


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start, but {spec.line_id} did")


@pytest.fixture
def table() -> Table:
    return Table()


@pytest.fixture
def scheduler(
    tmp_path: Path, table: Table, monkeypatch: pytest.MonkeyPatch
) -> Generator[Scheduler]:
    monkeypatch.setattr(scheduler_module.os, "getpid", lambda: DAEMON)
    monkeypatch.setattr(scheduler_module, "PID_WATCH_SECONDS", 0.01)
    with EventLog(tmp_path / "events.sqlite") as log:
        found = Scheduler(log, no_engine, hold_wakes=True, processes=table)
        found.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        found.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)
        yield found


async def wake(scheduler: Scheduler, line_id: str, task_id: object) -> tuple[str, bool]:
    tool = next(t for t in scheduler.own_tools(line_id) if t.name == "wake_after")
    result = await tool.handler({"seconds": 60, "note": "look", "task_id": task_id})
    return cast(str, result["content"][0]["text"]), result.get("is_error") is True


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the condition did not come true")


def task_notes(scheduler: Scheduler) -> list[TaskRecorded]:
    state = scheduler._log.state  # pyright: ignore[reportPrivateUsage]
    return list(state.task_records.get("alpha", ()))


def test_a_pid_task_identifier_is_written_and_read_back_exactly() -> None:
    assert pid_task_id(4711) == "pid:4711"
    assert pid_of_task(pid_task_id(4711)) == 4711
    for text in ("4711", "pid:", "pid:-1", "pid: 1", "pid:0", "pid:007", "pid:1.5"):
        assert pid_of_task(text) is None
    assert pid_of_task("pid:١٢") is None


def test_waiting_for_a_process_of_the_lines_own_schedules_a_wake_that_its_end_cancels(
    scheduler: Scheduler, table: Table
) -> None:
    table.start(2001, scheduler.line_mark("alpha"))

    async def act() -> None:
        text, error = await wake(scheduler, "alpha", "pid:2001")
        assert not error and "pid:2001" in text
        pending = scheduler._log.state.scheduled["alpha"]  # pyright: ignore[reportPrivateUsage]
        assert pending.task_id == "pid:2001"
        await asyncio.sleep(0.05)
        assert task_notes(scheduler) == []
        table.end(2001)
        await until(lambda: bool(task_notes(scheduler)))
        notes = task_notes(scheduler)
        assert [note.task_id for note in notes] == ["pid:2001"]
        state = scheduler._log.state  # pyright: ignore[reportPrivateUsage]
        bodies = [m.body for m in state.messages.values() if "pid:2001" in m.body]
        assert len(bodies) == 1
        assert "Your background task pid:2001 ended" in bodies[0]
        assert pending.message_id in bodies[0]
        assert "alpha" not in scheduler._log.state.scheduled  # pyright: ignore[reportPrivateUsage]

    asyncio.run(act())


def test_a_table_that_cannot_be_read_is_not_the_end_of_the_process(
    scheduler: Scheduler, table: Table
) -> None:
    table.start(2001, scheduler.line_mark("alpha"))

    async def act() -> None:
        _, error = await wake(scheduler, "alpha", "pid:2001")
        assert not error
        table.unreadable = True
        await asyncio.sleep(0.08)
        assert task_notes(scheduler) == []
        table.unreadable = False
        await asyncio.sleep(0.05)
        assert task_notes(scheduler) == []
        table.end(2001)
        await until(lambda: bool(task_notes(scheduler)))

    asyncio.run(act())


def test_a_line_cannot_wait_for_a_process_that_is_not_its_own(
    scheduler: Scheduler, table: Table
) -> None:
    table.start(2001, scheduler.line_mark("beta"))
    table.start(2002, scheduler.line_mark("alpha"))
    table.end(2002)

    async def act() -> None:
        for task_id, reason in (
            ("pid:2001", "is not one of your running processes"),
            ("pid:2002", "is not one of your running processes"),
            ("pid:3333", "is not one of your running processes"),
            (f"pid:{DAEMON}", "is not one of your running processes"),
        ):
            text, error = await wake(scheduler, "alpha", task_id)
            assert error and reason in text
        assert "alpha" not in scheduler._log.state.scheduled  # pyright: ignore[reportPrivateUsage]
        text, error = await wake(scheduler, "alpha", "pid:abc")
        assert error and "must name one of your running background tasks" in text

    asyncio.run(act())


def test_waiting_for_a_process_is_unavailable_where_the_table_cannot_be_read(
    scheduler: Scheduler, table: Table
) -> None:
    table.unreadable = True

    async def act() -> None:
        text, error = await wake(scheduler, "alpha", "pid:2001")
        assert error
        assert text == (
            "wake_after is unavailable: ps did not answer within 10 seconds"
        )

    asyncio.run(act())


def test_waiting_for_a_process_is_unavailable_where_processes_cannot_be_identified(
    tmp_path: Path, table: Table
) -> None:
    with EventLog(tmp_path / "events.sqlite") as log:
        blind = Scheduler(
            log,
            no_engine,
            hold_wakes=True,
            processes=table,
            processes_identifiable=False,
        )
        blind.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

        async def act() -> None:
            text, error = await wake(blind, "alpha", "pid:2001")
            assert error
            assert text == processes.unavailable("wake_after", processes.UNIDENTIFIABLE)

        asyncio.run(act())


def test_a_later_wake_replaces_the_watch_and_the_watch_ends_with_its_wake(
    scheduler: Scheduler, table: Table
) -> None:
    table.start(2001, scheduler.line_mark("alpha"))
    table.start(2002, scheduler.line_mark("alpha"))

    async def act() -> None:
        await wake(scheduler, "alpha", "pid:2001")
        first = scheduler._pid_watchers["alpha"]  # pyright: ignore[reportPrivateUsage]
        await wake(scheduler, "alpha", "pid:2002")
        await asyncio.sleep(0)
        assert first.cancelled() or first.done()
        table.end(2001)
        await asyncio.sleep(0.08)
        assert task_notes(scheduler) == []
        table.end(2002)
        await until(lambda: bool(task_notes(scheduler)))
        assert [note.task_id for note in task_notes(scheduler)] == ["pid:2002"]

    asyncio.run(act())


def test_an_ordinary_wake_removes_an_earlier_watch(
    scheduler: Scheduler, table: Table
) -> None:
    table.start(2001, scheduler.line_mark("alpha"))

    async def act() -> None:
        await wake(scheduler, "alpha", "pid:2001")
        first = scheduler._pid_watchers["alpha"]  # pyright: ignore[reportPrivateUsage]
        text, error = await wake(scheduler, "alpha", None)
        assert not error, text
        await asyncio.sleep(0)
        assert "alpha" not in scheduler._pid_watchers  # pyright: ignore[reportPrivateUsage]
        assert first.cancelled() or first.done()
        table.end(2001)
        await asyncio.sleep(0.08)
        assert task_notes(scheduler) == []

    asyncio.run(act())
