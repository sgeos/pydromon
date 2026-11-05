"""The ledger of a line's children, seen below its engine and stopped by pid and start
time. Round 15, item 3.

Every process table here is a fake the test writes, so a pid reused by another process
and a child that was given to the system's first process are built, not waited for. No
real process is signalled."""

import asyncio
import json
import os
import signal
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import processes
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.processes import (
    MAX_LEDGER_LINES,
    MAX_LEDGER_PER_LINE,
    ChildLedger,
    ProcessInfo,
    ProcessRefused,
    TreeReadFailure,
    TreeRow,
    below,
    ledger_listing,
    stop_ledgered,
)
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
HARNESS = 1001
FIRST = "Sat Nov  1 07:23:05 2026"
SECOND = "Sat Nov  1 09:41:17 2026"
THIRD = "Sun Nov  2 00:00:01 2026"


def row(
    pid: int,
    ppid: int = HARNESS,
    command: str = "sleep 100",
    started: str = FIRST,
    pgid: int | None = None,
) -> TreeRow:
    return TreeRow(pid, ppid, pid if pgid is None else pgid, command, started)


class Machine:
    """A process table the test edits, with the pid of the engine and a record of the
    signals sent."""

    def __init__(self, rows: Sequence[TreeRow] = ()) -> None:
        self.rows = list(rows)
        self.harness: int | None = HARNESS
        self.failure: TreeReadFailure | None = None
        self.sent: list[tuple[int, int]] = []
        self.identity_calls = 0
        self.reuse_at_signal: dict[int, str] = {}
        self.lost: set[int] = set()
        self.marks: dict[int, list[str]] = {}

    def read(self) -> list[TreeRow] | TreeReadFailure:
        return self.failure if self.failure is not None else list(self.rows)

    def identity(self, pid: int) -> tuple[int, str] | None:
        self.identity_calls += 1
        if pid in self.reuse_at_signal:
            return 1, self.reuse_at_signal[pid]
        found = next((item for item in self.rows if item.pid == pid), None)
        return None if found is None else (found.ppid, found.started)

    def send(self, pid: int, signal_number: int) -> None:
        if pid in self.lost:
            raise ProcessLookupError(pid)
        self.sent.append((pid, signal_number))

    # ProcessTable, for the mark path of an identifiable scheduler.

    def own_processes(self) -> Sequence[ProcessInfo]:
        return [
            ProcessInfo(item.pid, item.ppid, "00:01", item.command)
            for item in self.rows
        ]

    def has_variable(self, pid: int, assignment: str) -> bool:
        return assignment in self.marks.get(pid, [])

    def environments(self) -> dict[int, list[str]]:
        return {item.pid: self.marks.get(item.pid, ["PATH=/bin"]) for item in self.rows}


def stop(
    ledger: ChildLedger,
    machine: Machine,
    pid: int,
    force: bool = False,
    line: str = "alpha",
    forbidden: int = -1,
) -> str:
    return stop_ledgered(
        ledger,
        line,
        pid,
        force,
        read_rows=machine.read,
        identity=machine.identity,
        send=machine.send,
        forbidden=lambda candidate: candidate == forbidden,
    )


# The ledger itself.


def test_the_ledger_keeps_children_with_a_start_time_and_nothing_else() -> None:
    ledger = ChildLedger()
    kept = ledger.record(
        "alpha",
        [
            row(2001),
            row(2002, started=""),
            row(2003, command="<defunct>"),
            row(1, started=FIRST),
        ],
    )
    assert kept == 1
    assert [entry.pid for entry in ledger.entries("alpha")] == [2001]
    assert ledger.entries("beta") == []
    assert ledger.record("alpha", [row(2001)]) == 0, "an unchanged child is not new"


def test_a_pid_given_to_another_process_replaces_the_entry() -> None:
    ledger = ChildLedger()
    ledger.record("alpha", [row(2001)])
    assert ledger.record("alpha", [row(2001, started=SECOND)]) == 1
    entry = ledger.find("alpha", 2001)
    assert entry is not None and entry.started == SECOND


def test_the_ledger_is_bounded_per_line_and_in_lines() -> None:
    ledger = ChildLedger()
    ledger.record(
        "alpha", [row(2000 + index) for index in range(MAX_LEDGER_PER_LINE + 10)]
    )
    pids = [entry.pid for entry in ledger.entries("alpha")]
    assert len(pids) == MAX_LEDGER_PER_LINE
    assert pids[0] == 2010, "the oldest observed are dropped first"
    for index in range(MAX_LEDGER_LINES + 5):
        ledger.record(f"line{index}", [row(3000 + index)])
    held = [
        index for index in range(MAX_LEDGER_LINES + 5) if ledger.entries(f"line{index}")
    ]
    assert len(held) + (1 if ledger.entries("alpha") else 0) <= MAX_LEDGER_LINES


def test_pruning_drops_what_ended_was_reused_or_changed() -> None:
    ledger = ChildLedger()
    ledger.record(
        "alpha",
        [row(2001), row(2002), row(2003), row(2004), row(2005, command="a")],
    )
    now = [
        row(2001),
        row(2003, started=SECOND),
        row(2004, started=""),
        row(2005, command="b"),
    ]
    assert [entry.pid for entry in ledger.prune("alpha", now)] == [2001]
    assert [entry.pid for entry in ledger.entries("alpha")] == [2001]


def test_a_child_that_left_the_engines_tree_is_still_listed_while_it_runs() -> None:
    """The engine's tree finds the child while its parent stands. When the parent
    exits, the child is given to the system's first process and the walk misses it,
    and the ledger still names it, by pid and start time."""
    ledger = ChildLedger()
    before = [row(HARNESS, 1), row(2001, HARNESS, "zsh -c ./gate.sh"), row(2002, 2001)]
    ledger.record("alpha", below(before, HARNESS, me=900))
    after = [row(HARNESS, 1), row(2002, 1)]
    assert below(after, HARNESS, me=900) == [], "the walk no longer reaches it"
    listed = ledger_listing(ledger, "alpha", after)
    assert [entry["pid"] for entry in listed] == [2002]
    assert listed[0]["basis"] == "ledger" and listed[0]["task_id"] == "pid:2002"
    assert listed[0]["started"] == FIRST


def test_a_reused_pid_is_not_listed() -> None:
    ledger = ChildLedger()
    ledger.record("alpha", [row(2001)])
    reused = [row(2001, 1, "vim", SECOND)]
    assert ledger_listing(ledger, "alpha", reused) == []
    assert ledger.find("alpha", 2001) is None, "the entry is forgotten"


def test_the_daemons_own_pid_is_not_listed() -> None:
    ledger = ChildLedger()
    ledger.record("alpha", [row(2001)])
    assert ledger_listing(ledger, "alpha", [row(2001)], lambda pid: pid == 2001) == []


# Stopping.


def test_a_ledgered_child_is_stopped_by_pid_and_start_time() -> None:
    ledger, machine = ChildLedger(), Machine([row(2001), row(2002)])
    ledger.record("alpha", machine.rows)
    assert "sent SIGTERM to process 2001" in stop(ledger, machine, 2001)
    assert "sent SIGKILL to process 2002" in stop(ledger, machine, 2002, force=True)
    assert machine.sent == [(2001, signal.SIGTERM), (2002, signal.SIGKILL)]


def test_a_pid_that_is_not_in_the_lines_ledger_is_refused() -> None:
    ledger, machine = ChildLedger(), Machine([row(2001), row(2002)])
    ledger.record("beta", [row(2002)])
    for pid in (2001, 2002, 4242):
        with pytest.raises(ProcessRefused, match="is not one of yours"):
            stop(ledger, machine, pid)
    for pid in (1, 0, -3):
        with pytest.raises(ProcessRefused, match="above 1"):
            stop(ledger, machine, pid)
    assert machine.sent == []


def test_a_reused_pid_is_not_signalled_and_is_forgotten() -> None:
    ledger, machine = ChildLedger(), Machine([row(2001)])
    ledger.record("alpha", machine.rows)
    machine.rows = [row(2001, 1, "vim", SECOND)]
    with pytest.raises(ProcessRefused, match="no longer the one recorded"):
        stop(ledger, machine, 2001)
    assert machine.sent == [] and ledger.find("alpha", 2001) is None


def test_the_same_start_time_with_another_command_is_not_signalled() -> None:
    ledger, machine = ChildLedger(), Machine([row(2001)])
    ledger.record("alpha", machine.rows)
    machine.rows = [row(2001, command="vim")]
    with pytest.raises(ProcessRefused, match="no longer the one recorded"):
        stop(ledger, machine, 2001)
    assert machine.sent == []


def test_a_pid_reused_between_the_reading_and_the_signal_is_not_signalled() -> None:
    """The table still shows the recorded process, and the check made just before the
    signal shows another start time."""
    ledger, machine = ChildLedger(), Machine([row(2001)])
    ledger.record("alpha", machine.rows)
    machine.reuse_at_signal[2001] = SECOND
    with pytest.raises(ProcessRefused, match="changed while it was checked"):
        stop(ledger, machine, 2001)
    assert machine.sent == []
    assert machine.identity_calls == 1


def test_a_check_that_cannot_say_does_not_signal() -> None:
    ledger, machine = ChildLedger(), Machine([row(2001)])
    ledger.record("alpha", machine.rows)
    machine.identity = lambda pid: None  # type: ignore[method-assign]
    with pytest.raises(ProcessRefused, match="changed while it was checked"):
        stop(ledger, machine, 2001)
    assert machine.sent == []


def test_a_process_table_that_cannot_be_read_refuses_the_stop() -> None:
    ledger, machine = ChildLedger(), Machine([row(2001)])
    ledger.record("alpha", machine.rows)
    machine.failure = TreeReadFailure("ps cannot be run")
    with pytest.raises(ProcessRefused, match="could not be read: ps cannot be run"):
        stop(ledger, machine, 2001)
    assert machine.sent == []
    assert ledger.find("alpha", 2001) is not None, "not knowing is not an ending"


def test_a_child_that_ended_is_reported_gone_and_forgotten() -> None:
    ledger, machine = ChildLedger(), Machine([row(2001)])
    ledger.record("alpha", machine.rows)
    machine.rows = []
    with pytest.raises(ProcessRefused, match="has already ended"):
        stop(ledger, machine, 2001)
    assert ledger.find("alpha", 2001) is None


def test_a_child_that_ends_at_the_signal_is_reported_gone() -> None:
    ledger, machine = ChildLedger(), Machine([row(2001)])
    ledger.record("alpha", machine.rows)
    machine.lost.add(2001)
    with pytest.raises(ProcessRefused, match="has already ended"):
        stop(ledger, machine, 2001)


def test_a_forbidden_pid_is_never_signalled() -> None:
    ledger, machine = ChildLedger(), Machine([row(2001)])
    ledger.record("alpha", machine.rows)
    with pytest.raises(ProcessRefused, match="is not one of yours"):
        stop(ledger, machine, 2001, forbidden=2001)
    assert machine.sent == []


def test_a_signal_the_system_refuses_is_reported() -> None:
    ledger, machine = ChildLedger(), Machine([row(2001)])
    ledger.record("alpha", machine.rows)

    def refuse(pid: int, number: int) -> None:
        raise PermissionError(pid)

    with pytest.raises(ProcessRefused, match="cannot be signalled"):
        stop_ledgered(
            ledger,
            "alpha",
            2001,
            False,
            read_rows=machine.read,
            identity=machine.identity,
            send=refuse,
        )


# The tools, on a scheduler whose engine and process table are fakes.


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start, but {spec.line_id} did")


def world(
    tmp_path: Path, machine: Machine, *, identifiable: bool
) -> Generator[Scheduler]:
    with EventLog(tmp_path / "events.sqlite") as log:
        found = Scheduler(
            log,
            no_engine,
            hold_wakes=True,
            processes=machine,
            processes_identifiable=identifiable,
        )
        found.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        found.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)
        found._tree_read = machine.read  # pyright: ignore[reportPrivateUsage]
        found._identity_read = machine.identity  # pyright: ignore[reportPrivateUsage]
        found._harness_read = lambda line_id: (  # pyright: ignore[reportPrivateUsage]
            machine.harness if line_id == "alpha" else None
        )
        yield found


@pytest.fixture
def blind(tmp_path: Path) -> Generator[tuple[Scheduler, Machine]]:
    machine = Machine(
        [
            row(HARNESS, 1, "claude"),
            row(2001, HARNESS, "zsh -c ./gate.sh"),
            row(2002, 2001, "sleep 100"),
        ]
    )
    for scheduler in world(tmp_path, machine, identifiable=False):
        yield scheduler, machine


def call(
    scheduler: Scheduler, line_id: str, name: str, args: dict[str, object]
) -> tuple[str, bool]:
    tool = next(t for t in scheduler.own_tools(line_id) if t.name == name)

    async def invoke() -> dict[str, Any]:
        return await tool.handler(args)

    result = asyncio.run(invoke())
    return cast(str, result["content"][0]["text"]), result.get("is_error") is True


def listed(scheduler: Scheduler, line_id: str = "alpha") -> list[dict[str, Any]]:
    text, error = call(scheduler, line_id, "list_processes", {})
    assert not error, text
    return cast(list[dict[str, Any]], json.loads(text))


def test_where_the_mark_cannot_be_read_the_tool_lists_what_was_seen_below_the_engine(
    blind: tuple[Scheduler, Machine],
) -> None:
    scheduler, machine = blind
    found = listed(scheduler)
    assert [entry["pid"] for entry in found] == [2001, 2002]
    assert {entry["basis"] for entry in found} == {"ledger"}
    assert HARNESS not in [entry["pid"] for entry in found], "not the engine itself"
    # The shell exits, the child is given to the system's first process, the engine
    # ends. The ledger still names the child, by its pid and start time.
    machine.rows = [row(2002, 1, "sleep 100")]
    machine.harness = None
    assert [entry["pid"] for entry in listed(scheduler)] == [2002]
    done, error = call(scheduler, "alpha", "stop_process", {"pid": 2002})
    assert not error and "sent SIGTERM to process 2002" in done
    assert machine.sent == [(2002, signal.SIGTERM)]


def test_a_daemonised_child_is_stopped_after_the_engine_ended(
    blind: tuple[Scheduler, Machine],
) -> None:
    scheduler, machine = blind
    listed(scheduler)
    machine.rows = [row(2002, 1, "sleep 100")]
    machine.harness = None
    done, error = call(scheduler, "alpha", "stop_process", {"pid": 2002, "force": True})
    assert not error and "SIGKILL" in done
    assert machine.sent == [(2002, signal.SIGKILL)]


def test_a_reused_pid_is_neither_listed_nor_stopped_through_the_tools(
    blind: tuple[Scheduler, Machine],
) -> None:
    scheduler, machine = blind
    listed(scheduler)
    machine.harness = None
    machine.rows = [row(2002, 1, "vim notes.txt", SECOND)]
    text, error = call(scheduler, "alpha", "stop_process", {"pid": 2002})
    assert error and "no longer the one recorded" in text
    text, error = call(scheduler, "alpha", "list_processes", {})
    assert (text, error) == (
        processes.unavailable("list_processes", processes.UNIDENTIFIABLE),
        True,
    )
    assert machine.sent == []


def test_another_lines_child_is_not_listed_or_stopped(
    blind: tuple[Scheduler, Machine],
) -> None:
    scheduler, machine = blind
    listed(scheduler)
    text, error = call(scheduler, "beta", "list_processes", {})
    assert error and text == processes.unavailable(
        "list_processes", processes.UNIDENTIFIABLE
    )
    text, error = call(scheduler, "beta", "stop_process", {"pid": 2002})
    assert error and text == processes.unavailable(
        "stop_process", processes.UNIDENTIFIABLE
    )
    assert machine.sent == []


def test_an_unreadable_table_is_unavailable_and_never_an_empty_list(
    blind: tuple[Scheduler, Machine],
) -> None:
    scheduler, machine = blind
    listed(scheduler)
    machine.failure = TreeReadFailure("ps did not answer within 10 seconds")
    text, error = call(scheduler, "alpha", "list_processes", {})
    assert error and text.startswith("list_processes is unavailable")
    assert "ps did not answer" in text
    text, error = call(scheduler, "alpha", "stop_process", {"pid": 2002})
    assert error and "could not be read" in text
    assert machine.sent == []


def test_the_daemons_own_pid_is_not_ledgered(
    blind: tuple[Scheduler, Machine],
) -> None:
    scheduler, machine = blind
    me = os.getpid()
    machine.rows = [row(HARNESS, 1, "claude"), row(me, HARNESS, "dromond")]
    assert call(scheduler, "alpha", "list_processes", {})[1] is True
    assert call(scheduler, "alpha", "stop_process", {"pid": me})[1] is True
    assert machine.sent == []


def test_without_an_engine_or_a_ledger_no_process_is_run(
    blind: tuple[Scheduler, Machine],
) -> None:
    scheduler, machine = blind
    reads: list[str] = []
    scheduler._tree_read = lambda: reads.append("read") or []  # pyright: ignore[reportPrivateUsage]
    machine.harness = None
    assert call(scheduler, "alpha", "list_processes", {})[1] is True
    assert reads == []


def test_where_the_mark_can_be_read_the_ledger_adds_only_what_it_does_not_show(
    tmp_path: Path,
) -> None:
    machine = Machine([row(HARNESS, 1, "claude"), row(2001, HARNESS, "env -i sleep 9")])
    for scheduler in world(tmp_path, machine, identifiable=True):
        mark = scheduler.line_mark("alpha")
        machine.marks[2001] = [f"{processes.MARK_VARIABLE}={mark}"]
        found = listed(scheduler)
        # Marked and ledgered, so named once, by the mark.
        assert [entry["pid"] for entry in found] == [2001]
        assert "basis" not in found[0]
        # A child that cleared its environment is named by the ledger alone.
        machine.rows.append(row(2003, HARNESS, "env -i sleep 10", SECOND))
        found = listed(scheduler)
        assert [(e["pid"], e.get("basis")) for e in found] == [
            (2001, None),
            (2003, "ledger"),
        ]


def test_the_end_of_a_turn_records_the_children_below_a_standing_engine(
    blind: tuple[Scheduler, Machine],
) -> None:
    scheduler, machine = blind
    ledger = scheduler._children  # pyright: ignore[reportPrivateUsage]
    asyncio.run(scheduler._note_children("alpha"))  # pyright: ignore[reportPrivateUsage]
    assert [entry.pid for entry in ledger.entries("alpha")] == [2001, 2002]
    # An engine that has ended adds nothing and removes nothing.
    machine.harness = None
    machine.rows = []
    asyncio.run(scheduler._note_children("alpha"))  # pyright: ignore[reportPrivateUsage]
    assert [entry.pid for entry in ledger.entries("alpha")] == [2001, 2002]


def test_a_failed_reading_at_a_turns_end_is_dropped_and_a_gone_line_is_skipped(
    blind: tuple[Scheduler, Machine],
) -> None:
    scheduler, machine = blind
    machine.failure = TreeReadFailure("ps cannot be run")
    asyncio.run(scheduler._note_children("alpha"))  # pyright: ignore[reportPrivateUsage]
    asyncio.run(scheduler._note_children("nobody"))  # pyright: ignore[reportPrivateUsage]
    assert scheduler._children.entries("alpha") == []  # pyright: ignore[reportPrivateUsage]
