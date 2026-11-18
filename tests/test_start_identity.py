"""The start time as a process's identity. **Round 18, item 4.**

A pid is reused, so a name or a number cannot say that the process that holds it is the
one the daemon meant. The kernel's start time can, and these tests give each decision
that used the name alone a real process whose pid and name are right and whose start
time is not, and a real process whose name has changed and whose start time has not.
The processes are real, since a pid that was only written into a table cannot be reused.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from dromon import launcher, processes, spawn
from dromon.launcher import LaunchRecord, StopPolicy
from dromon.processes import (
    LSTART_FORMAT,
    TreeRow,
    parse_proc_stat_start,
    same_start,
    start_time,
)

SUPPORTED = sys.platform in processes.START_TIME_PLATFORMS
needs_start_time = pytest.mark.skipif(
    not SUPPORTED, reason="the platform gives no start time"
)


def sleeper(*, group: bool = False, replaced: bool = False) -> subprocess.Popen[bytes]:
    argv = ["/bin/sh", "-c", "exec sleep 60"] if replaced else ["sleep", "60"]
    child = spawn.start(argv, start_new_session=group)
    # A zombie still answers a signal, so the child is reaped as soon as it ends.
    threading.Thread(target=child.wait, daemon=True).start()
    return child


@pytest.fixture
def children() -> Iterator[list[subprocess.Popen[bytes]]]:
    made: list[subprocess.Popen[bytes]] = []
    yield made
    for child in made:
        if child.poll() is None:
            child.kill()
        child.wait()


def make(
    children: list[subprocess.Popen[bytes]], **kwargs: bool
) -> subprocess.Popen[bytes]:
    child = sleeper(**kwargs)
    children.append(child)
    return child


def dead_pid() -> int:
    done = spawn.start(["true"])
    done.wait()
    return done.pid


def still_running(child: subprocess.Popen[bytes]) -> bool:
    """Whether ``child`` runs, a moment after a signal that would have ended it, which
    a signal does not do at once, so that a refused signal is not taken for one the
    child had not yet acted on."""
    time.sleep(0.4)
    return child.poll() is None


def gone(child: subprocess.Popen[bytes], seconds: float = 10.0) -> bool:
    try:
        child.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def listed(pid: int, command: str, started: float | None = None) -> TreeRow:
    text = (
        ""
        if started is None
        else time.strftime(LSTART_FORMAT.replace("%d", "%e"), time.localtime(started))
    )
    return TreeRow(pid, 1, pid, command, text)


def record_file(
    directory: Path,
    launcher_pid: int,
    harness: int,
    command: str,
    *,
    policy: StopPolicy = StopPolicy.KILL_ALL,
    launcher_started: object = None,
    harness_started: object = None,
) -> Path:
    document: dict[str, object] = {
        "v": 1,
        "mark": "line.token",
        "launcher": launcher_pid,
        "harness": harness,
        "command": command,
        "policy": policy.value,
        "done": False,
    }
    if launcher_started is not None:
        document["launcher_started"] = launcher_started
    if harness_started is not None:
        document["harness_started"] = harness_started
    path = directory / f"{launcher_pid}-{time.time_ns()}{launcher.LAUNCH_SUFFIX}"
    path.write_text(json.dumps(document))
    return path


# The reading.


def test_a_linux_stat_line_gives_its_start_time_whatever_the_name_holds() -> None:
    after = " ".join(["S", "1", "1", "1", "0", "-1"] + ["0"] * 13 + ["500", "0"])
    for name in ("sleep", "a b", "x) y (z", ")("):
        text = f"123 ({name}) {after}\n"
        assert parse_proc_stat_start(text, 1000.0, 100.0) == 1000.0 + 500 / 100.0


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no parenthesis here",
        "1 (x) S 1 1",
        "1 (x) " + " ".join(["S"] + ["0"] * 18 + ["nope"]),
    ],
)
def test_a_stat_line_that_is_not_in_the_form_gives_nothing(text: str) -> None:
    assert parse_proc_stat_start(text, 1000.0, 100.0) is None


def test_a_stat_line_with_no_clock_rate_gives_nothing() -> None:
    after = " ".join(["S"] + ["0"] * 18 + ["5"])
    assert parse_proc_stat_start(f"1 (x) {after}", 1000.0, 0.0) is None


@needs_start_time
def test_a_process_has_one_start_time_that_does_not_move(
    children: list[subprocess.Popen[bytes]],
) -> None:
    before = time.time()
    child = make(children)
    first = start_time(child.pid)
    time.sleep(0.2)
    second = start_time(child.pid)
    assert first is not None
    assert same_start(first, second)
    # The start is when the child began, not after the parent read it.
    assert before - 2 <= first <= time.time() + 1


@needs_start_time
def test_a_process_started_later_has_a_later_start_time(
    children: list[subprocess.Popen[bytes]],
) -> None:
    first = make(children)
    time.sleep(0.3)
    second = make(children)
    one, two = start_time(first.pid), start_time(second.pid)
    assert one is not None and two is not None
    assert two > one
    assert not same_start(one, two)


@needs_start_time
@pytest.mark.parametrize("pid", [0, -1, 1 << 30])
def test_a_pid_that_is_not_a_process_has_no_start_time(pid: int) -> None:
    assert start_time(pid) is None


@needs_start_time
def test_an_ended_process_has_no_start_time() -> None:
    assert start_time(dead_pid()) is None


def test_a_missing_reading_is_never_a_match() -> None:
    assert not same_start(None, 5.0)
    assert not same_start(5.0, None)
    assert not same_start(None, None)
    assert same_start(5.0, 5.0 + processes.START_TOLERANCE_SECONDS / 2)
    assert not same_start(5.0, 5.0 + processes.START_TOLERANCE_SECONDS * 2)


# The signal.


@needs_start_time
def test_a_process_is_signalled_only_while_it_started_when_the_record_says(
    children: list[subprocess.Popen[bytes]],
) -> None:
    child = make(children)
    started = start_time(child.pid)
    assert started is not None
    assert not processes.signal_if_started(child.pid, signal.SIGTERM, started - 100)
    assert not processes.signal_if_started(child.pid, signal.SIGTERM, started + 100)
    assert still_running(child)
    assert processes.signal_if_started(child.pid, signal.SIGTERM, started)
    assert gone(child)


@needs_start_time
def test_a_signal_to_a_pid_that_is_gone_or_is_the_first_process_is_refused() -> None:
    assert not processes.signal_if_started(dead_pid(), signal.SIGTERM, 1.0)
    assert not processes.signal_if_started(1, signal.SIGTERM, 1.0)
    assert not processes.signal_if_started(0, signal.SIGTERM, 1.0)


@needs_start_time
def test_a_group_is_signalled_unless_its_leader_is_another_process(
    children: list[subprocess.Popen[bytes]],
) -> None:
    child = make(children, group=True)
    started = start_time(child.pid)
    assert started is not None
    # The pid now belongs to a process that started later than the group's leader.
    assert not processes.signal_group_if_started(child.pid, signal.SIGTERM, started - 5)
    assert not processes.signal_group_if_started(child.pid, 0, started + 5)
    assert still_running(child)
    assert processes.signal_group_if_started(child.pid, 0, started)
    assert processes.signal_group_if_started(child.pid, signal.SIGTERM, started)
    assert gone(child)


def test_a_group_with_no_leader_and_no_members_is_not_signalled() -> None:
    assert not processes.signal_group_if_started(dead_pid(), signal.SIGTERM, 1.0)
    assert not processes.signal_group_if_started(1, signal.SIGTERM, 1.0)


def test_a_group_whose_leader_has_gone_is_still_signalled_while_it_has_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A group outlives its leader, so no leader is no reason to refuse."""
    sent: list[tuple[int, int]] = []

    def unread(pid: int) -> None:
        del pid

    def record(group: int, number: int) -> None:
        sent.append((group, number))

    monkeypatch.setattr(processes, "start_time", unread)
    monkeypatch.setattr(os, "killpg", record)
    assert processes.signal_group_if_started(4242, signal.SIGTERM, 1.0)
    assert sent == [(4242, signal.SIGTERM)]


def test_on_linux_the_process_is_held_by_a_descriptor_before_it_is_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pidfd names the process, not the number, so a pid reused after the check
    cannot receive the signal. Run with fakes, since only Linux has one."""
    events: list[str] = []
    monkeypatch.setattr(processes.sys, "platform", "linux")

    def opened(pid: int) -> int:
        events.append("open")
        return 77

    def sent(descriptor: int, number: int) -> None:
        events.append(f"send {descriptor} {number}")

    def closed(descriptor: int) -> None:
        events.append(f"close {descriptor}")

    monkeypatch.setattr(os, "pidfd_open", opened, raising=False)
    monkeypatch.setattr(signal, "pidfd_send_signal", sent, raising=False)
    monkeypatch.setattr(os, "close", closed)

    def reading(pid: int) -> float:
        events.append("read")
        return 5.0

    def never(pid: int, number: int) -> None:
        raise AssertionError("the pid was signalled by number")

    monkeypatch.setattr(processes, "start_time", reading)
    monkeypatch.setattr(os, "kill", never)
    assert processes.signal_if_started(4242, signal.SIGTERM, 5.0)
    assert events == ["open", "read", f"send 77 {signal.SIGTERM}", "close 77"]
    events.clear()
    assert not processes.signal_if_started(4242, signal.SIGTERM, 9.0)
    assert events == ["open", "read", "close 77"]


def test_on_linux_a_pid_that_cannot_be_opened_is_not_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(processes.sys, "platform", "linux")

    def refuse(pid: int) -> int:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "pidfd_open", refuse, raising=False)

    def nothing(descriptor: int, number: int) -> None:
        del descriptor, number

    monkeypatch.setattr(signal, "pidfd_send_signal", nothing, raising=False)
    assert not processes.signal_if_started(4242, signal.SIGTERM, 5.0)


# The row.


@needs_start_time
def test_a_listed_process_is_taken_for_the_same_one_only_by_its_start_time(
    children: list[subprocess.Popen[bytes]],
) -> None:
    child = make(children)
    started = start_time(child.pid)
    assert started is not None
    assert processes.row_is_same_process(listed(child.pid, "sleep", started), started)
    assert not processes.row_is_same_process(
        listed(child.pid, "sleep", started - 60), started
    )
    assert not processes.row_is_same_process(listed(child.pid, "sleep", started), None)
    # A row listed without a start time is taken as it was before the check existed.
    assert processes.row_is_same_process(listed(child.pid, "sleep"), started)
    assert processes.row_is_same_process(listed(child.pid, "sleep"), None)


@needs_start_time
def test_the_sweep_does_not_signal_a_listed_process_whose_pid_was_given_away(
    children: list[subprocess.Popen[bytes]],
) -> None:
    child = make(children)
    started = start_time(child.pid)
    assert started is not None
    launcher._signal_found(listed(child.pid, "sleep", started - 60), signal.SIGTERM)  # pyright: ignore[reportPrivateUsage]
    assert still_running(child)
    launcher._signal_found(listed(child.pid, "sleep", started), signal.SIGTERM)  # pyright: ignore[reportPrivateUsage]
    assert gone(child)


# The records.


def test_a_launch_record_carries_the_start_times_it_was_written_with(
    tmp_path: Path,
) -> None:
    target = tmp_path / f"{os.getpid()}-{time.time_ns()}{launcher.LAUNCH_SUFFIX}"
    launcher.write_launch(
        target,
        "line.token",
        4242,
        "/bin/sleep",
        StopPolicy.KILL_ALL,
        done=False,
        launcher_started=12.5,
        harness_started=13.25,
    )
    [found] = launcher.read_launches(tmp_path)
    assert (found.launcher_started, found.harness_started) == (12.5, 13.25)


def test_a_launch_record_without_start_times_has_none(tmp_path: Path) -> None:
    target = tmp_path / f"{os.getpid()}-{time.time_ns()}{launcher.LAUNCH_SUFFIX}"
    launcher.write_launch(
        target, None, 4242, "/bin/sleep", StopPolicy.LEAVE_BACKGROUND, done=True
    )
    [found] = launcher.read_launches(tmp_path)
    assert (found.launcher_started, found.harness_started) == (None, None)


@pytest.mark.parametrize(
    "bad", ["soon", None, -1, 0, float("inf"), float("nan"), True, [1.0], {"a": 1}]
)
def test_a_start_time_that_is_not_a_positive_number_is_taken_as_absent(
    tmp_path: Path, bad: object
) -> None:
    record_file(tmp_path, 4242, 4243, "/bin/sleep", harness_started=bad)
    # What survives the round trip is what the reader sees, and none of it is trusted.
    [found] = launcher.read_launches(tmp_path)
    assert found.harness_started is None


@needs_start_time
def test_the_launcher_records_when_it_and_its_harness_started(tmp_path: Path) -> None:
    """Through the real launcher and its real module command."""
    reports = tmp_path / "left"
    reports.mkdir(mode=0o700)
    before = launcher.report_directory()
    launcher.configure(reports)
    command, launched, _ = launcher.wrap(["/bin/sleep", "30"], dict(os.environ))
    process = spawn.start(command, env=launched, start_new_session=True)
    try:
        deadline = time.monotonic() + 30
        found: list[LaunchRecord] = []
        while not found and time.monotonic() < deadline:
            time.sleep(0.1)
            found = launcher.read_launches(reports)
        assert found, "the launcher wrote no record"
        record = found[0]
        assert record.launcher == process.pid
        assert same_start(record.launcher_started, start_time(process.pid))
        assert same_start(record.harness_started, start_time(record.harness))
    finally:
        launcher.configure(before)
        process.terminate()
        process.wait()


# The decisions.


@needs_start_time
def test_a_launcher_pid_that_went_to_another_launcher_is_not_a_running_launcher(
    children: list[subprocess.Popen[bytes]],
) -> None:
    """The name and the marker match, since another launcher holds the pid, and the
    harness is gone. Before round 18 this was taken as running, and its record, and
    with it the group it left, was never swept."""
    other = make(children)
    started = start_time(other.pid)
    assert started is not None
    rows = [listed(other.pid, "python -I -m dromon.launcher")]
    assert launcher.launcher_running(rows, other.pid, None, "")
    assert launcher.launcher_running(
        rows, other.pid, None, "", launcher_started=started
    )
    assert not launcher.launcher_running(
        rows, other.pid, None, "", launcher_started=started - 30
    )


@needs_start_time
def test_a_harness_that_changed_its_name_is_still_held_to_its_launcher(
    children: list[subprocess.Popen[bytes]],
) -> None:
    """Its command no longer holds the recorded executable, as for a script engine that
    replaces itself with a native program, so the name could not tell it from a pid that
    was reused, and the launcher was taken as running whoever its parent was."""
    harness = make(children, replaced=True)
    started = start_time(harness.pid)
    assert started is not None
    row = TreeRow(harness.pid, 1, harness.pid, "sleep 60")
    launcher_row = listed(4242, "python -I -m dromon.launcher")
    rows: Sequence[TreeRow] = [launcher_row, row]
    # Its parent is not the launcher, so the launcher has gone, if this is the harness.
    assert launcher.launcher_running(rows, 4242, harness.pid, "/bin/sh")
    assert not launcher.launcher_running(
        rows, 4242, harness.pid, "/bin/sh", harness_started=started
    )
    # Another process at that pid, whose start time differs, is not held to anything.
    assert launcher.launcher_running(
        rows, 4242, harness.pid, "/bin/sh", harness_started=started - 30
    )


def swept(directory: Path, rows: Sequence[TreeRow]) -> list[launcher.Swept]:
    outcome = asyncio.run(launcher.sweep_launches(directory, lambda: list(rows)))
    assert isinstance(outcome, list)
    return outcome


@needs_start_time
def test_a_process_with_the_recorded_name_and_another_start_is_not_the_harness(
    tmp_path: Path, children: list[subprocess.Popen[bytes]]
) -> None:
    """Confirmed first as the name-only path, which is the sweep before round 18 and
    what a record without a start time still gets: the name matches, so the process is
    ended. With the start time the sweep leaves it alone."""
    child = make(children, group=True)
    started = start_time(child.pid)
    assert started is not None
    rows = [TreeRow(child.pid, 1, child.pid, "sleep 60")]
    dead = dead_pid()

    record_file(tmp_path, dead, child.pid, "sleep", harness_started=started - 100)
    [skipped] = swept(tmp_path, rows)
    assert skipped.skipped == "another process now holds the harness's pid"
    assert skipped.ended == ()
    assert still_running(child)

    record_file(tmp_path, dead, child.pid, "sleep")
    [fooled] = swept(tmp_path, rows)
    assert fooled.skipped is None
    assert fooled.ended == (child.pid,)
    assert gone(child)


@needs_start_time
@pytest.mark.parametrize("policy", list(StopPolicy))
def test_a_harness_that_changed_its_name_is_swept_when_its_launcher_is_gone(
    tmp_path: Path, children: list[subprocess.Popen[bytes]], policy: StopPolicy
) -> None:
    child = make(children, group=True, replaced=True)
    started = start_time(child.pid)
    assert started is not None
    rows = [TreeRow(child.pid, 1, child.pid, "sleep 60")]
    dead = dead_pid()

    record_file(tmp_path, dead, child.pid, "/bin/sh", policy=policy)
    [kept] = swept(tmp_path, rows)
    assert kept.skipped == "another process now holds the harness's pid"
    assert still_running(child)

    record_file(
        tmp_path, dead, child.pid, "/bin/sh", policy=policy, harness_started=started
    )
    [done] = swept(tmp_path, rows)
    assert done.skipped is None
    if policy is StopPolicy.KILL_ALL:
        assert done.ended == (child.pid,)
        assert gone(child)
    else:
        # The harness's launcher did not finish, so it is killed as the launcher would.
        assert done.ended == (child.pid,)
        assert gone(child)


@needs_start_time
def test_a_record_whose_launcher_pid_went_to_another_launcher_is_swept(
    tmp_path: Path, children: list[subprocess.Popen[bytes]]
) -> None:
    other = make(children)
    started = start_time(other.pid)
    assert started is not None
    rows = [listed(other.pid, "python -I -m dromon.launcher")]
    harness = dead_pid()

    kept = tmp_path / "kept"
    kept.mkdir()
    record = record_file(kept, other.pid, harness, "sleep")
    assert swept(kept, rows) == []
    assert record.exists()

    given = tmp_path / "given"
    given.mkdir()
    record = record_file(
        given, other.pid, harness, "sleep", launcher_started=started - 30
    )
    [done] = swept(given, rows)
    assert done.ended == ()
    assert not record.exists()


def test_the_daemons_table_is_read_with_start_times(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    def read(uid: int, *args: object, **kwargs: object) -> list[TreeRow]:
        seen.append(kwargs)
        return []

    monkeypatch.setattr(launcher, "tree_or_reason", read)
    assert launcher.read_rows() == []
    assert seen == [{"with_start": True}]
