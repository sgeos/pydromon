"""``dromon.spawn`` bounds a child's start apart from its run. **Round 15.** A start
once stalled for five minutes in ``subprocess``'s ``_execute_child`` on macOS, in
several tests and in continuous integration, as ``dromon.spawn`` records. A stalled
start is stood in for by a ``Popen`` that waits for the test to open a gate, since no
test can make the operating system stall a real one on demand."""

import ast
import asyncio
import contextlib
import io
import os
import resource
import signal
import subprocess
import sys
import time
import threading
import tomllib
from collections.abc import Callable, Generator, Iterator, Mapping
from pathlib import Path
from typing import IO, TYPE_CHECKING

import pytest

from dromon import land, spawn

if TYPE_CHECKING:
    from conftest import StalledStart

SLEEP = ["/bin/sleep", "60"]
"""A command that runs until it is killed."""

BOUND = 0.2
"""The start bound the stall tests use, in seconds."""

PROJECT = Path(__file__).resolve().parent.parent


@pytest.fixture
def stalled(
    stalled_start: "StalledStart", monkeypatch: pytest.MonkeyPatch
) -> "StalledStart":
    monkeypatch.setattr(spawn, "START_SECONDS", BOUND)
    return stalled_start


# A stalled start.


def test_a_start_that_stalls_fails_within_its_bound_with_a_clear_message(
    stalled: "StalledStart",
) -> None:
    before = time.monotonic()
    with pytest.raises(spawn.StartStalled) as caught:
        spawn.start(SLEEP)
    # Derived from the bound, with a wide margin for a loaded runner.
    assert time.monotonic() - before < 25 * BOUND
    message = str(caught.value)
    assert message.startswith(f"sleep did not start within {BOUND:g}s")
    assert "dromon.spawn" in message and "killed if it starts later" in message
    assert stalled.started == []
    assert spawn.stranded_count() == 1


def test_a_stalled_start_is_a_timeout_and_an_os_error() -> None:
    """So a caller that treats a program that cannot be started as a failure treats a
    stall as one, and a caller that did not now fails with a message."""
    assert issubclass(spawn.StartStalled, TimeoutError)
    assert issubclass(spawn.StartStalled, OSError)
    assert not issubclass(spawn.StartStalled, subprocess.TimeoutExpired)


def test_a_child_that_starts_after_its_caller_gave_up_is_killed_and_reaped(
    stalled: "StalledStart",
) -> None:
    with pytest.raises(spawn.StartStalled):
        spawn.start(SLEEP, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    stalled.stall = False
    stalled.gate.set()
    late = stalled.until_reaped()
    assert late.returncode == -signal.SIGKILL
    assert late.stdout is not None and late.stdout.closed
    stalled.until_ended()
    assert spawn.stranded_count() == 0


def test_a_run_whose_start_stalls_fails_and_runs_nothing(
    stalled: "StalledStart", tmp_path: Path
) -> None:
    marker = tmp_path / "ran"
    with pytest.raises(spawn.StartStalled, match="did not start within"):
        spawn.run(
            [sys.executable, "-c", f"open({str(marker)!r}, 'w')"],
            timeout=30,
            capture_output=True,
        )
    stalled.stall = False
    stalled.gate.set()
    stalled.until_reaped()
    # The late child was killed at once, so it never wrote the marker.
    time.sleep(0.2)
    assert not marker.exists()


def test_no_further_child_is_started_while_the_most_allowed_are_stuck(
    stalled: "StalledStart", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spawn, "MAX_STRANDED", 2)
    for _ in range(2):
        with pytest.raises(spawn.StartStalled, match="did not start"):
            spawn.start(SLEEP)
    assert stalled.calls == 2
    before = time.monotonic()
    with pytest.raises(spawn.StartStalled, match="2 earlier starts are still stuck"):
        spawn.start(SLEEP)
    assert time.monotonic() - before < BOUND, "the refusal waited for a bound"
    assert stalled.calls == 2, "a start was made past the bound"
    stalled.stall = False
    stalled.gate.set()
    stalled.until_ended()
    assert spawn.stranded_count() == 0
    with spawn.start(["/bin/echo", "ok"], stdout=subprocess.PIPE, text=True) as child:
        assert child.communicate()[0] == "ok\n"


# Descriptors handed over to a start.


def is_open(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError:
        return False
    return True


def test_descriptors_handed_over_are_closed_once_the_start_has_ended() -> None:
    read, write = os.pipe()
    try:
        child = spawn.start(
            ["/bin/echo", "x"], stdout=write, close_after=(write,), pass_fds=()
        )
        assert not is_open(write)
        child.wait()
    finally:
        os.close(read)


def test_descriptors_of_a_stalled_start_stay_open_until_the_late_start_ends(
    stalled: "StalledStart",
) -> None:
    """The caller is told of the stall while the start still holds the descriptors, so
    a descriptor number another thread reuses is never the one the late child gets."""
    read, write = os.pipe()
    try:
        with pytest.raises(spawn.StartStalled):
            spawn.start(SLEEP, stdout=write, close_after=(write,))
        assert is_open(write), "closed under a start that had not ended"
        stalled.stall = False
        stalled.gate.set()
        stalled.until_reaped()
        stalled.until_ended()
        assert not is_open(write)
    finally:
        os.close(read)


def test_descriptors_are_closed_when_no_start_is_made() -> None:
    read, write = os.pipe()
    try:
        with pytest.raises(ValueError, match="must be above 0"):
            spawn.start(SLEEP, close_after=(write,), start_seconds=0)
        assert not is_open(write)
    finally:
        os.close(read)


def test_descriptors_are_closed_when_the_cap_refuses_the_start(
    stalled: "StalledStart", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spawn, "MAX_STRANDED", 1)
    with pytest.raises(spawn.StartStalled):
        spawn.start(SLEEP)
    read, write = os.pipe()
    try:
        with pytest.raises(spawn.StartStalled, match="still stuck"):
            spawn.start(SLEEP, stdout=write, close_after=(write,))
        assert not is_open(write)
        assert stalled.calls == 1
    finally:
        os.close(read)
        stalled.stall = False
        stalled.gate.set()
        stalled.until_ended()


# A start that ends.


def test_the_caller_keeps_a_child_that_started_by_its_deadline() -> None:
    """The hand-over in one order. The start ends first, so the caller owns the child,
    and nothing kills it."""
    child = spawn.start(SLEEP)
    try:
        time.sleep(0.1)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_a_program_that_cannot_be_started_is_an_os_error_and_not_a_stall(
    tmp_path: Path,
) -> None:
    with pytest.raises(FileNotFoundError):
        spawn.start([str(tmp_path / "missing")])
    with pytest.raises(FileNotFoundError):
        spawn.run([str(tmp_path / "missing")], timeout=5)


def test_a_start_that_raises_other_than_os_error_raises_in_the_caller() -> None:
    with pytest.raises(ValueError, match="illegal environment variable name"):
        spawn.start(["/bin/echo"], env={"A=B": "x"})


@pytest.mark.parametrize("bound", [0.0, -1.0])
def test_a_start_bound_not_above_zero_is_refused(bound: float) -> None:
    with pytest.raises(ValueError, match="must be above 0"):
        spawn.start(["/bin/echo"], start_seconds=bound)


# A run, which does what subprocess.run does.


def test_input_output_and_status_pass_as_subprocess_run_gave_them(
    tmp_path: Path,
) -> None:
    done = spawn.run(
        ["/bin/cat"], timeout=10, input="hello\n", capture_output=True, text=True
    )
    assert (done.returncode, done.stdout, done.stderr) == (0, "hello\n", "")
    failing = ["/bin/sh", "-c", "echo out; echo err >&2; exit 3"]
    done = spawn.run(failing, timeout=10, capture_output=True, text=True)
    assert (done.returncode, done.stdout, done.stderr) == (3, "out\n", "err\n")
    seen = spawn.run(
        ["/bin/pwd", "-P"], timeout=10, cwd=tmp_path, capture_output=True, text=True
    )
    assert seen.stdout == f"{tmp_path.resolve()}\n"
    raw = spawn.run(["/bin/cat"], timeout=10, input=b"\xff", capture_output=True)
    assert raw.stdout == b"\xff"
    assert done.args == failing


def test_check_raises_for_a_failed_command_and_keeps_its_output() -> None:
    with pytest.raises(subprocess.CalledProcessError) as caught:
        spawn.run(
            ["/bin/sh", "-c", "echo out; exit 3"],
            timeout=10,
            check=True,
            capture_output=True,
            text=True,
        )
    assert caught.value.returncode == 3 and caught.value.stdout == "out\n"
    ok = spawn.run(["/usr/bin/true"], timeout=10, check=True)
    assert ok.returncode == 0


def test_a_command_past_its_timeout_is_killed_and_raises_as_subprocess_run_does() -> (
    None
):
    before = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        spawn.run(SLEEP, timeout=0.3, capture_output=True)
    assert time.monotonic() - before < 10


def test_contradictory_arguments_are_refused() -> None:
    with pytest.raises(ValueError, match="input and stdin"):
        spawn.run(["/bin/cat"], timeout=5, input=b"x", stdin=subprocess.DEVNULL)
    with pytest.raises(ValueError, match="capture_output"):
        spawn.run(["/bin/echo"], timeout=5, capture_output=True, stdout=subprocess.PIPE)


def test_the_land_runner_reports_a_stall_as_its_own_status(
    stalled: "StalledStart", tmp_path: Path
) -> None:
    done = land.run_command(SLEEP, tmp_path)
    assert done.code == land.STALLED
    assert done.stderr.startswith(f"sleep did not start within {BOUND:g}s")
    stalled.stall = False
    stalled.gate.set()
    stalled.until_reaped()


# The bound is named once, and nothing else in src starts a child unbounded.

CALLS = {"run", "Popen", "check_output", "check_call", "call", "getoutput"}
ASYNC_CALLS = {"create_subprocess_exec", "create_subprocess_shell"}
ASYNC_KNOWN = {"relay.py", "pi.py", "codex.py", "jsonlines.py", "grok.py"}
"""The modules that start a child with asyncio, which runs ``Popen`` on the event
loop's own thread and cannot be moved to another. They are the known residual that
``dromon.spawn`` names. A new one fails the test below, so the residual cannot grow
unseen."""


def called(path: Path, names: set[str], owner: str) -> list[int]:
    """The lines in ``path`` that call ``owner.NAME`` for a NAME in ``names``."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(path.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in names
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == owner
        ):
            lines.append(node.lineno)
    return lines


def modules() -> list[Path]:
    return sorted((PROJECT / "src" / "dromon").glob("*.py"))


def test_no_module_starts_a_child_without_spawn_but_those_it_names() -> None:
    offenders = {
        path.name: called(path, CALLS, "subprocess")
        for path in modules()
        if path.name != "spawn.py" and called(path, CALLS, "subprocess")
    }
    assert set(offenders) == {"launcher.py"}, offenders
    assert set(spawn.EXEMPT) == {"launcher.py", "client.py"}
    assert called(PROJECT / "src/dromon/client.py", {"posix_spawn"}, "os")
    assert all(reason for reason in spawn.EXEMPT.values())


def test_the_asyncio_starts_are_the_known_residual() -> None:
    found = {path.name for path in modules() if called(path, ASYNC_CALLS, "asyncio")}
    assert found == ASYNC_KNOWN


def test_the_start_bound_is_named_once() -> None:
    """No other module defines a start bound or a stranded-start cap of its own."""
    for path in modules():
        if path.name == "spawn.py":
            continue
        tree = ast.parse(path.read_text())
        names = {
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign | ast.AnnAssign)
            for target in (
                [node.target] if isinstance(node, ast.AnnAssign) else node.targets
            )
            if isinstance(target, ast.Name)
        }
        stray = {"START_GRACE_SECONDS", "MAX_STRANDED", "START_SECONDS"} & names
        # grok.py has a deadline for the engine's whole startup, a different bound.
        assert stray <= ({"START_SECONDS"} if path.name == "grok.py" else set()), (
            path.name,
            stray,
        )


def test_the_start_bound_ends_a_stall_well_before_the_test_run_is_ended() -> None:
    """A stalled start should fail its own test, not end the whole run, which
    pytest's faulthandler does after its timeout."""
    options = tomllib.loads((PROJECT / "pyproject.toml").read_text())
    timeout = options["tool"]["pytest"]["ini_options"]["faulthandler_timeout"]
    assert timeout > spawn.START_SECONDS * 2


# Review of 151cbcd.


def test_a_read_a_stop_depends_on_is_still_started_at_the_bound(
    stalled: "StalledStart", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spawn, "MAX_STRANDED", 1)
    with pytest.raises(spawn.StartStalled, match="did not start"):
        spawn.start(SLEEP)
    with pytest.raises(spawn.StartStalled, match="1 earlier starts are still stuck"):
        spawn.run(["/bin/echo", "x"], timeout=5)
    calls = stalled.calls
    stalled.stall = False
    done = spawn.run(
        ["/bin/echo", "x"], timeout=5, text=True, capture_output=True, essential=True
    )
    assert done.stdout == "x\n" and stalled.calls == calls + 1
    stalled.gate.set()
    stalled.until_ended()


def test_even_an_essential_start_is_refused_past_the_ceiling(
    stalled: "StalledStart", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spawn, "MAX_STRANDED", 1)
    monkeypatch.setattr(spawn, "MAX_STRANDED_ESSENTIAL", 2)
    for _ in range(2):
        with pytest.raises(spawn.StartStalled, match="did not start"):
            spawn.run(["/bin/echo", "x"], timeout=5, essential=True)
    with pytest.raises(spawn.StartStalled, match="2 earlier starts are still stuck"):
        spawn.run(["/bin/echo", "x"], timeout=5, essential=True)
    stalled.gate.set()
    stalled.until_ended()


def test_a_stuck_slot_frees_itself_when_its_thread_ends(
    stalled: "StalledStart", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spawn, "MAX_STRANDED", 1)
    with pytest.raises(spawn.StartStalled):
        spawn.start(SLEEP)
    assert spawn.stranded_count() == 1
    stalled.stall = False
    stalled.gate.set()
    stalled.until_ended()
    assert spawn.stranded_count() == 0
    with spawn.start(["/bin/echo", "x"], stdout=subprocess.PIPE) as child:
        assert child.communicate()[0] == b"x\n"


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_killing_a_child_that_leads_a_group_kills_the_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild"
    child = spawn.start(
        ["/bin/sh", "-c", f"sleep 60 & echo $! > {marker}; wait"],
        start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (
        marker.exists() and marker.read_text().strip()
    ):
        time.sleep(0.01)
    grandchild = int(marker.read_text())
    try:
        assert alive(grandchild)
        spawn._kill(child)  # pyright: ignore[reportPrivateUsage]
        child.wait(10)
        while alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not alive(grandchild), "the grandchild outlived the group's kill"
    finally:
        if alive(grandchild):
            os.kill(grandchild, signal.SIGKILL)


def test_a_child_in_the_daemons_own_group_is_killed_alone() -> None:
    assert os.getpgrp() != 0
    child = spawn.start(SLEEP)
    assert os.getpgid(child.pid) == os.getpgrp(), "the child shares the group"
    spawn._kill(child)  # pyright: ignore[reportPrivateUsage]
    assert child.wait(10) == -signal.SIGKILL
    # This process, which shares the group, is still here to say so.
    assert alive(os.getpid())


def test_a_late_child_that_leads_a_group_is_reaped_with_its_group(
    stalled: "StalledStart", tmp_path: Path
) -> None:
    marker = tmp_path / "grandchild"
    script = f"sleep 60 & echo $! > {marker}; wait"
    with pytest.raises(spawn.StartStalled):
        spawn.start(["/bin/sh", "-c", script], start_new_session=True)
    stalled.stall = False
    stalled.gate.set()
    child = stalled.until_reaped()
    assert child.poll() is not None
    if marker.exists() and marker.read_text().strip():
        grandchild = int(marker.read_text())
        deadline = time.monotonic() + 10
        while alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not alive(grandchild)


def test_a_timed_out_command_keeps_the_tail_of_what_it_wrote() -> None:
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        spawn.run(
            ["/bin/sh", "-c", "echo partial; echo problem >&2; sleep 60"],
            timeout=1,
            text=True,
            capture_output=True,
            start_new_session=True,
        )
    assert caught.value.output == "partial\n"
    assert caught.value.stderr == "problem\n"
    with pytest.raises(subprocess.TimeoutExpired) as raw:
        spawn.run(
            ["/bin/sh", "-c", "echo raw; sleep 60"],
            timeout=1,
            capture_output=True,
            start_new_session=True,
        )
    assert raw.value.output == b"raw\n" and raw.value.stderr == b""


def test_the_tail_of_a_timed_out_command_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawn, "TAIL_LIMIT", 4)
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        spawn.run(
            ["/bin/sh", "-c", "printf abcdefghij; sleep 60"],
            timeout=1,
            text=True,
            capture_output=True,
            start_new_session=True,
        )
    assert caught.value.output == "ghij"
    assert caught.value.stderr == ""


def test_a_command_that_wrote_nothing_has_no_output_in_its_timeout() -> None:
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        spawn.run(SLEEP, timeout=0.3)
    assert caught.value.output is None and caught.value.stderr is None


def test_the_stuck_text_names_the_effect_and_no_program() -> None:
    text = spawn.stuck_text(4)
    assert "4 child process starts are stuck" in text
    assert "stop or a hold" in text and "restarts" in text


# Test isolation. The autouse ``_isolated_spawn_state`` in ``tests/conftest.py`` gives
# each test an empty list of stranded starts. The two tests below run in file order. The
# first strands a live thread and leaves it, and the second finds the count at zero
# while that thread is still alive, so the zero is the fixture's and not the thread's
# end. Run in another order, the second skips and proves nothing.

_LEFT: list[threading.Thread] = []
_RELEASE = threading.Event()


def test_a_stranded_start_left_by_one_test_is_counted_in_it() -> None:
    thread = threading.Thread(target=_RELEASE.wait, daemon=True)
    thread.start()
    _LEFT.append(thread)
    with spawn._stranded_lock:  # pyright: ignore[reportPrivateUsage]
        spawn._stranded.append(thread)  # pyright: ignore[reportPrivateUsage]
    assert spawn.stranded_count() == 1


def test_the_next_test_starts_with_no_stranded_start() -> None:
    try:
        if not _LEFT:
            pytest.skip("the test that strands a start did not run before this one")
        assert _LEFT[0].is_alive(), "the left thread ended, so zero proves nothing"
        assert spawn.stranded_count() == 0
        # A start under the cap is allowed, so the earlier stall does not refuse it.
        assert (
            spawn.run(
                ["/bin/echo", "ok"], timeout=10, capture_output=True, text=True
            ).stdout
            == "ok\n"
        )
    finally:
        _RELEASE.set()


# Stall diagnostics. A stalled start reports what is known of it, so the next stall
# tells the hypotheses in the module's note apart. No test can make the operating
# system stall a real start, so the report is checked on a start held at a gate, on a
# synthetic ``/proc``, and on the real table where the host allows it.


def test_a_stalled_start_reports_where_its_thread_is_and_what_was_in_flight(
    stalled: "StalledStart",
) -> None:
    with pytest.raises(spawn.StartStalled) as caught:
        spawn.start(SLEEP)
    message = str(caught.value)
    assert message.startswith(f"sleep did not start within {BOUND:g}s")
    report = message.split(" Report, ", 1)[1]
    assert len(report) <= spawn.MAX_REPORT_CHARS
    # The thread is in the held ``Popen``, reached from ``spawn``'s own ``target``.
    assert "the start thread is at" in report and "target" in report
    assert "1 starts in flight now and 1 at most during this one" in report
    assert "threads alive" in report and "of the daemon" in report
    soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    assert (
        f"descriptor limit {soft}" in report or "descriptor limit unlimited" in report
    )
    assert "/bin/sleep" not in report, "the report must not name the command"


def test_a_second_stalled_start_reports_the_overlap_with_the_first(
    stalled: "StalledStart",
) -> None:
    with pytest.raises(spawn.StartStalled):
        spawn.start(SLEEP)
    with pytest.raises(spawn.StartStalled) as caught:
        spawn.start(SLEEP)
    assert "2 starts in flight now and 2 at most" in str(caught.value)


def test_the_overlap_is_forgotten_once_a_start_ends() -> None:
    handoff = spawn._Handoff()  # pyright: ignore[reportPrivateUsage]
    spawn._enter(handoff)  # pyright: ignore[reportPrivateUsage]
    assert handoff.overlap == 1
    spawn._leave(handoff)  # pyright: ignore[reportPrivateUsage]
    with spawn._active_lock:  # pyright: ignore[reportPrivateUsage]
        assert handoff not in spawn._active  # pyright: ignore[reportPrivateUsage]


def make_proc(
    root: Path, entries: Mapping[str, tuple[str, str, str, str | None]]
) -> None:
    """A synthetic ``/proc`` with ``self`` and one directory for each entry, given as
    pid -> (comm, state and ppid fields, exe, wchan)."""
    (root / "self").mkdir(parents=True)
    (root / "self" / "stat").write_text("1 (me) S 0")
    (root / "self" / "exe").symlink_to("/opt/python")
    for pid, (comm, fields, exe, wait) in entries.items():
        directory = root / pid
        directory.mkdir()
        (directory / "stat").write_text(f"{pid} ({comm}) {fields} 0 0 0")
        (directory / "exe").symlink_to(exe)
        if wait is not None:
            (directory / "wchan").write_text(wait)


def test_proc_children_tell_a_child_that_has_executed_from_one_that_has_not(
    tmp_path: Path,
) -> None:
    make_proc(
        tmp_path,
        {
            "100": ("python", "S 50", "/opt/python", "do_wait"),
            "101": ("git", "R 50", "/usr/bin/git", "0"),
            "102": ("other", "S 7", "/usr/bin/other", None),
            # A name with spaces and a closing parenthesis, as ``comm`` may hold.
            "103": ("we ) ird", "D 50", "/usr/bin/x", None),
        },
    )
    (tmp_path / "notes").write_text("not a process")
    found = {
        child.pid: child
        for child in spawn._proc_children(tmp_path, 50)  # pyright: ignore[reportPrivateUsage]
    }
    assert set(found) == {100, 101, 103}
    assert found[100].execed is False and found[100].waiting == "do_wait"
    assert found[101].execed is True and found[101].waiting is None
    assert found[103].program == "we ) ird" and found[103].state == "D"


def test_proc_children_are_bounded_and_unreadable_entries_are_skipped(
    tmp_path: Path,
) -> None:
    entries = {
        str(1000 + number): ("c", "S 50", "/usr/bin/c", None) for number in range(20)
    }
    make_proc(tmp_path, entries)
    (tmp_path / "7").mkdir()  # no stat file
    (tmp_path / "8").mkdir()
    (tmp_path / "8" / "stat").write_text("garbage")
    found = spawn._proc_children(tmp_path, 50)  # pyright: ignore[reportPrivateUsage]
    assert len(found) == spawn.MAX_CHILDREN_REPORTED
    assert spawn._proc_children(tmp_path / "missing", 50) == []  # pyright: ignore[reportPrivateUsage]


def unblock(fifo: Path) -> None:
    """Let a reader that waits in the ``open`` of ``fifo`` go, and wait for the
    abandoned report thread to end, so that no test leaves one behind."""
    with contextlib.suppress(OSError):
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and any(
        t.name == "dromon-spawn-report" for t in threading.enumerate()
    ):
        time.sleep(0.02)


def test_a_proc_read_that_blocks_ends_the_report_and_names_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Found by CI on 2026-11-05.** A Linux run hung five minutes in a read of
    ``/proc``. A named pipe stands for the file, since a read of it blocks the same
    way and can be let go afterwards."""
    monkeypatch.setattr(spawn, "DIAGNOSE_SECONDS", 0.3)
    make_proc(tmp_path, {"100": ("git", "R 50", "/usr/bin/git", None)})
    (tmp_path / "200").mkdir()
    fifo = tmp_path / "200" / "stat"
    os.mkfifo(fifo)
    try:
        began = time.monotonic()
        result = spawn._proc_children_bounded(tmp_path, 50)  # pyright: ignore[reportPrivateUsage]
        assert time.monotonic() - began < 5, "the read is left behind, not waited for"
        assert isinstance(result, str)
        assert "did not return within 0.3s" in result and "200/stat" in result
    finally:
        unblock(fifo)


def test_the_stall_report_survives_a_proc_read_that_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spawn, "DIAGNOSE_SECONDS", 0.3)
    make_proc(tmp_path, {})
    (tmp_path / "300").mkdir()
    fifo = tmp_path / "300" / "stat"
    os.mkfifo(fifo)
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    try:
        began = time.monotonic()
        report = spawn.stall_report(thread, spawn._Handoff(), proc=tmp_path)  # pyright: ignore[reportPrivateUsage]
        assert time.monotonic() - began < 5
        assert "did not return" in report and "300/stat" in report
        assert "threads alive" in report, "the rest of the report is still given"
    finally:
        unblock(fifo)


def test_a_proc_read_never_opens_the_files_that_need_a_childs_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``cmdline``, ``environ`` and ``maps`` of a child in the middle of ``vfork``
    can block, so they are named pipes here and the read must not touch them."""
    monkeypatch.setattr(spawn, "DIAGNOSE_SECONDS", 2.0)
    make_proc(tmp_path, {"100": ("git", "R 50", "/usr/bin/git", None)})
    for name in ("cmdline", "environ", "maps", "mem"):
        os.mkfifo(tmp_path / "100" / name)
    result = spawn._proc_children_bounded(tmp_path, 50)  # pyright: ignore[reportPrivateUsage]
    assert isinstance(result, list) and [child.pid for child in result] == [100]


def test_a_child_is_kept_when_its_later_files_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The state of a child is reported even if its ``wchan`` cannot be read."""
    monkeypatch.setattr(spawn, "DIAGNOSE_SECONDS", 0.3)
    make_proc(tmp_path, {"100": ("git", "R 50", "/usr/bin/git", None)})
    (tmp_path / "100" / "wchan").unlink(missing_ok=True)
    fifo = tmp_path / "100" / "wchan"
    os.mkfifo(fifo)
    try:
        result = spawn._proc_children_bounded(tmp_path, 50)  # pyright: ignore[reportPrivateUsage]
        assert isinstance(result, str)
        assert "100/wchan" in result and "pid 100 state R program git" in result
    finally:
        unblock(fifo)


def test_vfork_is_turned_off_on_linux_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "_USE_VFORK", True, raising=False)
    monkeypatch.setattr(subprocess, "_USE_POSIX_SPAWN", True, raising=False)
    assert spawn.disable_vfork("darwin") is False
    assert getattr(subprocess, "_USE_VFORK") is True  # noqa: B009
    assert getattr(subprocess, "_USE_POSIX_SPAWN") is True  # noqa: B009
    assert spawn.disable_vfork("linux") is True
    assert getattr(subprocess, "_USE_VFORK") is False  # noqa: B009
    assert getattr(subprocess, "_USE_POSIX_SPAWN") is False  # noqa: B009


def test_vfork_off_is_harmless_where_the_names_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(subprocess, "_USE_VFORK", raising=False)
    monkeypatch.delattr(subprocess, "_USE_POSIX_SPAWN", raising=False)
    assert spawn.disable_vfork("linux") is False


def import_and_report(environment: dict[str, str]) -> tuple[str, str]:
    """``subprocess._USE_VFORK`` before and after ``dromon.spawn`` is imported, in a
    fresh interpreter, so that an earlier import does not decide it."""
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-I",
            "-c",
            "import subprocess\n"
            "was = getattr(subprocess, '_USE_VFORK', None)\n"
            "import dromon.spawn\n"
            "print(was, getattr(subprocess, '_USE_VFORK', None))",
        ],
        env={
            **{k: v for k, v in os.environ.items() if k != "DROMON_NO_VFORK"},
            **environment,
            "PYTHONPATH": str(Path(spawn.__file__).parents[1]),
        },
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    was, now = done.stdout.split()
    return was, now


def test_importing_the_module_leaves_vfork_on_by_default() -> None:
    """Round 16, the review of pull request 149. The experiment of turning vfork off
    did not stop the stall on Linux, so the default is CPython's own."""
    was, now = import_and_report({})
    assert now == was


def test_importing_the_module_turns_vfork_off_on_linux_when_asked() -> None:
    was, now = import_and_report({"DROMON_NO_VFORK": "1"})
    if sys.platform.startswith("linux") and was != "None":
        assert now == "False"
    else:
        assert now == was, "nothing is changed where vfork is not used"


def test_any_other_value_of_the_variable_leaves_vfork_on() -> None:
    for value in ("0", "", "true", "yes"):
        was, now = import_and_report({"DROMON_NO_VFORK": value})
        assert now == was, value


@pytest.fixture
def vfork_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "_USE_VFORK", True, raising=False)
    monkeypatch.setattr(subprocess, "_USE_POSIX_SPAWN", True, raising=False)


def vfork_flags() -> tuple[object, object]:
    return (
        getattr(subprocess, "_USE_VFORK"),  # noqa: B009
        getattr(subprocess, "_USE_POSIX_SPAWN"),  # noqa: B009
    )


@pytest.mark.usefixtures("vfork_on")
def test_the_linux_default_leaves_vfork_on() -> None:
    """Review of round 16, follow-up 2. With a platform named, the default on Linux is
    checked on every machine, and a change that turns vfork off for everyone, which a
    run on macOS never sees, fails here."""
    assert spawn.apply_vfork_setting({}, "linux") is False
    assert vfork_flags() == (True, True)
    assert spawn.apply_vfork_setting({"PATH": "/usr/bin"}, "linux") is False
    assert vfork_flags() == (True, True)


@pytest.mark.usefixtures("vfork_on")
def test_only_the_exact_value_one_turns_vfork_off_on_linux() -> None:
    for value in ("0", "", "true", "yes", "1 ", "01"):
        assert spawn.apply_vfork_setting({"DROMON_NO_VFORK": value}, "linux") is False
        assert vfork_flags() == (True, True), repr(value)
    assert spawn.apply_vfork_setting({"DROMON_NO_VFORK": "1"}, "linux") is True
    assert vfork_flags() == (False, False)


@pytest.mark.usefixtures("vfork_on")
def test_the_variable_changes_nothing_off_linux() -> None:
    assert spawn.apply_vfork_setting({"DROMON_NO_VFORK": "1"}, "darwin") is False
    assert vfork_flags() == (True, True)


def test_the_import_applies_the_setting_and_does_not_turn_vfork_off_itself() -> None:
    """The module's last statements go through ``apply_vfork_setting``, which reads
    the environment, so an unconditional ``disable_vfork()`` at import is caught on a
    machine where the fresh-interpreter test cannot see it."""
    tree = ast.parse(Path(spawn.__file__).read_text(encoding="utf-8"))
    calls = [
        node.value.func.id
        for node in tree.body
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    ]
    assert "apply_vfork_setting" in calls
    assert "disable_vfork" not in calls
    assert not any(
        isinstance(node, ast.If)
        and any(
            isinstance(inner, ast.Expr)
            and isinstance(inner.value, ast.Call)
            and getattr(inner.value.func, "id", "") == "disable_vfork"
            for inner in node.body
        )
        for node in tree.body
    ), "an import-time branch turns vfork off outside apply_vfork_setting"


def test_proc_children_give_their_processor_time(tmp_path: Path) -> None:
    """The ``utime`` and ``stime`` of ``stat``, twelfth and thirteenth after the
    program name, are shown in milliseconds, which tells a child that waits from one
    that spins."""
    make_proc(tmp_path, {"100": ("git", "S 50", "/usr/bin/git", None)})
    tail = " ".join(["0"] * 9 + ["250", "150"] + ["0"] * 20)
    (tmp_path / "100" / "stat").write_text(f"100 (git) S 50 {tail}")
    per_second = os.sysconf("SC_CLK_TCK")
    found = spawn._proc_children(tmp_path, 50)  # pyright: ignore[reportPrivateUsage]
    assert [c.cpu for c in found] == [f"cpu {400 * 1000 // per_second}ms"]
    assert f"cpu {400 * 1000 // per_second}ms" in spawn._children_text(found)  # pyright: ignore[reportPrivateUsage]


def test_a_stat_without_times_gives_no_processor_time(tmp_path: Path) -> None:
    make_proc(tmp_path, {"100": ("git", "S 50", "/usr/bin/git", None)})
    found = spawn._proc_children(tmp_path, 50)  # pyright: ignore[reportPrivateUsage]
    assert [c.cpu for c in found] == [None]


# The ledger of slow starts and the figures a stall report gives, round 16.


def test_a_slow_start_is_recorded_with_where_the_time_went(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(spawn, "SLOW_START_SECONDS", 0.0)
    child = spawn.start(TRUE)
    child.wait(10)
    notes = spawn.slow_starts()
    assert len(notes) == 1
    note = notes[0]
    assert note.startswith("sh ok after ")
    assert "waiting for the fork lock" in note and "in the fork call" in note
    assert "starts overlapped" in note and "threads" in note
    assert f"dromond: slow start, {note}" in capsys.readouterr().err
    spawn.clear_slow_starts()
    assert spawn.slow_starts() == []


def test_a_start_under_the_slow_threshold_is_not_recorded() -> None:
    spawn.start(TRUE).wait(10)
    assert spawn.slow_starts() == []


def test_a_slow_start_that_fails_is_recorded_as_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawn, "SLOW_START_SECONDS", 0.0)
    with pytest.raises(FileNotFoundError):
        spawn.start(["/nonexistent/program"])
    assert [n.split(" after ")[0] for n in spawn.slow_starts()] == [
        "program FileNotFoundError"
    ]
    spawn.clear_slow_starts()


def test_the_ledger_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spawn, "SLOW_START_SECONDS", 0.0)
    monkeypatch.setattr(spawn, "MAX_SLOW_STARTS_KEPT", 3)
    for _ in range(5):
        spawn.start(TRUE).wait(10)
    assert len(spawn.slow_starts()) == 3
    spawn.clear_slow_starts()


def test_a_ledger_that_fails_does_not_disturb_the_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawn, "SLOW_START_SECONDS", 0.0)

    def broken(proc: Path = Path("/proc")) -> str:
        raise RuntimeError("no figures")

    monkeypatch.setattr(spawn, "_machine_text", broken)
    assert spawn.start(TRUE).wait(10) == 0
    assert spawn.slow_starts() == []


def test_the_fork_text_says_whether_the_fork_call_has_returned() -> None:
    handoff = spawn._Handoff()  # pyright: ignore[reportPrivateUsage]
    text = spawn._fork_text  # pyright: ignore[reportPrivateUsage]
    assert text(handoff) == "the thread has not reached the fork"
    handoff.fork.requested = handoff.began + 0.5
    assert "the lock was not yet taken" in text(handoff)
    handoff.fork.acquired = handoff.began + 1.0
    assert "the fork call has not returned" in text(handoff)
    assert "+1.00s" in text(handoff)
    handoff.fork.released = handoff.began + 2.5
    assert "the fork call returned +2.50s" in text(handoff)


def test_a_stall_before_the_fork_says_so(stalled: "StalledStart") -> None:
    """The stand-in stalls before ``Popen`` forks, so the thread has not reached it."""
    with pytest.raises(spawn.StartStalled) as caught:
        spawn.start(SLEEP)
    assert "the thread has not reached the fork" in str(caught.value)


def test_a_real_fork_leaves_its_times_in_the_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[spawn._Handoff] = []  # pyright: ignore[reportPrivateUsage]
    real = spawn._Handoff  # pyright: ignore[reportPrivateUsage]

    def remember() -> spawn._Handoff:  # pyright: ignore[reportPrivateUsage]
        made = real()
        seen.append(made)
        return made

    monkeypatch.setattr(spawn, "_Handoff", remember)
    spawn.start(TRUE).wait(10)
    times = seen[0].fork
    assert 0.0 < times.requested <= times.acquired <= times.released
    assert seen[0].ended >= times.released


def test_machine_text_gives_load_and_pressure_where_proc_has_them(
    tmp_path: Path,
) -> None:
    (tmp_path / "pressure").mkdir()
    (tmp_path / "pressure" / "cpu").write_text(
        "some avg10=1.50 avg60=0.00 avg300=0.00 total=1\n"
    )
    (tmp_path / "pressure" / "io").write_text(
        "some avg10=42.25 avg60=9.00 avg300=1.00 total=9\n"
        "full avg10=40.00 avg60=0.00 avg300=0.00 total=3\n"
    )
    text = spawn._machine_text(tmp_path)  # pyright: ignore[reportPrivateUsage]
    assert "cpu wait 1.50%" in text and "io wait 42.25%" in text
    assert "memory" not in text, "a file that is absent is left out"
    assert text.startswith("load ") or text == "no load figures"


def test_machine_text_survives_hostile_pressure_files(tmp_path: Path) -> None:
    (tmp_path / "pressure").mkdir()
    (tmp_path / "pressure" / "cpu").write_text("some avg10=\x1b[31m9\n")
    (tmp_path / "pressure" / "io").write_text("garbage")
    text = spawn._machine_text(tmp_path)  # pyright: ignore[reportPrivateUsage]
    assert "\x1b" not in text and "io wait" not in text


PS_TABLE = """\
    1     0 Ss   -      /sbin/launchd
  500     1 S    -      /opt/python3.15
  600   500 S    -      /opt/python3.15
  601   500 S    -      /usr/bin/git
  602   500 U    biowait /path with spaces/tool
  700     1 S    -      /usr/bin/other
not a row
"""


def test_ps_rows_give_the_children_and_whether_each_has_executed() -> None:
    found = spawn._parse_ps(PS_TABLE, 500, 500)  # pyright: ignore[reportPrivateUsage]
    assert [child.pid for child in found] == [600, 601, 602]
    assert [child.execed for child in found] == [False, True, True]
    assert found[2].program == "tool" and found[2].waiting == "biowait"
    assert found[0].waiting is None
    # With this process missing from the table, execution cannot be told.
    unknown = spawn._parse_ps(PS_TABLE, 500, 999)  # pyright: ignore[reportPrivateUsage]
    assert {child.execed for child in unknown} == {None}


def test_the_children_text_shows_no_control_character_and_is_bounded() -> None:
    hostile = spawn._Child(  # pyright: ignore[reportPrivateUsage]
        1, "S\x1b[31m", "evil\nname" + "x" * 500, True, "w\x00"
    )
    text = spawn._children_text([hostile])  # pyright: ignore[reportPrivateUsage]
    assert "\x1b" not in text and "\n" not in text and "\x00" not in text
    assert len(text) < 200
    assert "no child" in spawn._children_text([])  # pyright: ignore[reportPrivateUsage]
    assert "unavailable" in spawn._children_text("unavailable, X")  # pyright: ignore[reportPrivateUsage]


def test_the_report_reads_the_table_where_there_is_no_proc(tmp_path: Path) -> None:
    thread = threading.Thread(target=lambda: None)
    thread.start()
    thread.join()
    seen: list[tuple[int, int]] = []

    def table(parent: int, own: int) -> list[spawn._Child] | str:  # pyright: ignore[reportPrivateUsage]
        seen.append((parent, own))
        return [spawn._Child(77, "S", "stuck", False, "pipe_r")]  # pyright: ignore[reportPrivateUsage]

    report = spawn.stall_report(
        thread,
        spawn._Handoff(),  # pyright: ignore[reportPrivateUsage]
        proc=tmp_path / "absent",
        table=table,
        libproc=lambda parent: "unavailable, test",
    )
    assert seen == [(os.getpid(), os.getpid())]
    assert "pid 77 state S program stuck NOT executed waiting pipe_r" in report
    assert "the start thread is at has ended" in report


def test_a_report_that_fails_does_not_hide_the_stall(tmp_path: Path) -> None:
    thread = threading.Thread(target=lambda: None)

    def broken(parent: int, own: int) -> list[spawn._Child] | str:  # pyright: ignore[reportPrivateUsage]
        raise RuntimeError("no table")

    report = spawn.stall_report(
        thread,
        spawn._Handoff(),  # pyright: ignore[reportPrivateUsage]
        proc=tmp_path / "absent",
        table=broken,
        libproc=lambda parent: "unavailable, test",
    )
    assert report == "the report failed, RuntimeError"


def test_the_report_prefers_libproc_and_falls_back_to_ps(tmp_path: Path) -> None:
    thread = threading.Thread(target=lambda: None)
    asked: list[str] = []

    def table(parent: int, own: int) -> list[spawn._Child] | str:  # pyright: ignore[reportPrivateUsage]
        asked.append("ps")
        return [spawn._Child(1, "S", "from_ps", None, None)]  # pyright: ignore[reportPrivateUsage]

    def libproc(parent: int) -> list[spawn._Child] | str:  # pyright: ignore[reportPrivateUsage]
        asked.append("libproc")
        return [spawn._Child(2, "I", "from_libproc", False, None)]  # pyright: ignore[reportPrivateUsage]

    handoff = spawn._Handoff()  # pyright: ignore[reportPrivateUsage]
    report = spawn.stall_report(
        thread, handoff, proc=tmp_path / "absent", table=table, libproc=libproc
    )
    assert asked == ["libproc"] and "from_libproc" in report and "from_ps" not in report
    asked.clear()
    report = spawn.stall_report(
        thread,
        handoff,
        proc=tmp_path / "absent",
        table=table,
        libproc=lambda parent: "unavailable, test",
    )
    assert asked == ["ps"] and "from_ps" in report


@pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS libproc")
def test_the_real_libproc_shows_a_running_child_without_starting_a_process() -> None:
    child = spawn.start(SLEEP)
    try:
        found = spawn._libproc_children(os.getpid())  # pyright: ignore[reportPrivateUsage]
        assert not isinstance(found, str), found
        mine = [c for c in found if c.pid == child.pid]
        assert mine and mine[0].program == "sleep" and mine[0].execed is True
        assert mine[0].state == "live", "a sleeping child is not shown as running"
        assert mine[0].cpu is not None and "threads running" in mine[0].cpu
        assert " pageins " in mine[0].cpu and " unix syscalls " in mine[0].cpu
        assert " resident " in mine[0].cpu
    finally:
        child.kill()
        child.wait()


@pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS libproc")
def test_libproc_tells_a_spinning_child_from_a_sleeping_one() -> None:
    """**Round 16.** The state letter does not, as a plain sleeping child showed ``R``.
    The processor time and the running threads do."""
    spinner = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "while True: pass"]
    )
    sleeper = spawn.start(SLEEP)
    try:
        # Both are read once the sleeper has settled, since a child that has just
        # started is still running while it loads its program.
        deadline = time.monotonic() + 10
        by_pid: dict[int, spawn._Child] = {}  # pyright: ignore[reportPrivateUsage]
        while time.monotonic() < deadline:
            found = spawn._libproc_children(os.getpid())  # pyright: ignore[reportPrivateUsage]
            assert not isinstance(found, str), found
            by_pid = {c.pid: c for c in found}
            if "0/1 threads running" in (
                by_pid[sleeper.pid].cpu or ""
            ) and "1/1 threads running" in (by_pid[spinner.pid].cpu or ""):
                break
            time.sleep(0.05)
        assert "1/1 threads running" in (by_pid[spinner.pid].cpu or "")
        assert "0/1 threads running" in (by_pid[sleeper.pid].cpu or "")
    finally:
        spinner.kill()
        sleeper.kill()
        spinner.wait()
        sleeper.wait()


def test_libproc_says_why_where_it_does_not_apply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    found = spawn._libproc_children(os.getpid())  # pyright: ignore[reportPrivateUsage]
    assert isinstance(found, str) and found.startswith("unavailable")


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="needs a Linux /proc")
def test_the_real_proc_shows_a_running_child() -> None:
    child = spawn.start(SLEEP)
    try:
        found = spawn._proc_children(Path("/proc"), os.getpid())  # pyright: ignore[reportPrivateUsage]
        mine = [c for c in found if c.pid == child.pid]
        assert mine and mine[0].execed is True and mine[0].program == "sleep"
    finally:
        child.kill()
        child.wait()


def test_the_real_ps_shows_a_running_child_where_ps_runs() -> None:
    child = spawn.start(SLEEP)
    try:
        table = spawn._ps_table(os.getpid(), os.getpid())  # pyright: ignore[reportPrivateUsage]
        if isinstance(table, str):
            pytest.skip(f"ps cannot run here, as inside an agent sandbox: {table}")
        mine = [c for c in table if c.pid == child.pid]
        assert mine and mine[0].program == "sleep" and mine[0].execed is True
    finally:
        child.kill()
        child.wait()


# The watchdog, which writes every thread's traceback while a start is stuck.


@pytest.fixture
def watched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, IO[str]]]:
    """A file the watchdog writes to, with the margin past the bound shortened. The
    watchdog is put off again when the test ends, which also cancels the timer that
    ``faulthandler`` keeps for the whole process, and pytest sets its own at the next
    test."""
    monkeypatch.setattr(spawn, "WATCHDOG_MARGIN_SECONDS", 0.2)
    path = tmp_path / "dump.txt"
    with path.open("w") as file:
        yield path, file
    spawn.watch_starts(None)


def wait_for_dump(path: Path, seconds: float = 10.0) -> str:
    """The dump, once the watchdog has begun it and had time to finish it. The file is
    polled with ``os.stat`` and not read, since a dump taken while this thread is in
    the middle of opening a file can show that frame as invalid and stop there."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if os.stat(path).st_size:  # noqa: PTH116
            time.sleep(0.5)
            break
        time.sleep(0.02)
    return path.read_text()


def test_a_stuck_start_dumps_every_thread_past_its_bound(
    stalled: "StalledStart", watched: tuple[Path, IO[str]]
) -> None:
    path, file = watched
    assert spawn.watch_starts(file) is True
    with pytest.raises(spawn.StartStalled):
        spawn.start(SLEEP)
    dump = wait_for_dump(path)
    # Found by CI on 2026-11-05. Python 3.12 prints no thread names, only newer
    # versions do, so the stuck start is recognised by its frame, which every
    # supported version prints.
    assert "(most recent call first)" in dump
    assert 'spawn.py", line' in dump and "in target" in dump, (
        "the stuck start's thread, in the frame that makes the child, is in the dump"
    )


def test_the_dump_waits_past_the_bound_and_the_margin(
    stalled: "StalledStart",
    watched: tuple[Path, IO[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, file = watched
    monkeypatch.setattr(spawn, "WATCHDOG_MARGIN_SECONDS", 30.0)
    spawn.watch_starts(file)
    with pytest.raises(spawn.StartStalled):
        spawn.start(SLEEP)
    time.sleep(3 * BOUND)
    assert path.read_text() == "", "no dump before the bound plus the margin"


def test_a_start_that_ends_cancels_the_dump(
    watched: tuple[Path, IO[str]],
) -> None:
    path, file = watched
    spawn.watch_starts(file)
    child = spawn.start(SLEEP)
    child.kill()
    child.wait()
    assert spawn._armed is None  # pyright: ignore[reportPrivateUsage]
    time.sleep(0.6)
    assert path.read_text() == ""


def test_a_watchdog_that_is_off_dumps_nothing(
    stalled: "StalledStart", watched: tuple[Path, IO[str]]
) -> None:
    path, file = watched
    spawn.watch_starts(file)
    assert spawn.watch_starts(None) is False
    with pytest.raises(spawn.StartStalled):
        spawn.start(SLEEP)
    time.sleep(0.8)
    assert path.read_text() == ""
    assert spawn._armed is None  # pyright: ignore[reportPrivateUsage]


def test_a_file_without_a_descriptor_leaves_the_watchdog_off() -> None:
    assert spawn.watch_starts(io.StringIO()) is False
    assert spawn._armed is None  # pyright: ignore[reportPrivateUsage]


def test_the_older_start_is_the_one_the_timer_is_set_for(
    watched: tuple[Path, IO[str]],
) -> None:
    _, file = watched
    spawn.watch_starts(file)
    first, second = spawn._Handoff(), spawn._Handoff()  # pyright: ignore[reportPrivateUsage]
    first.deadline = time.monotonic() + 60
    second.deadline = time.monotonic() + 30
    spawn._enter(first)  # pyright: ignore[reportPrivateUsage]
    assert spawn._armed is first  # pyright: ignore[reportPrivateUsage]
    spawn._enter(second)  # pyright: ignore[reportPrivateUsage]
    # The earlier deadline is the one the timer is set for.
    assert spawn._armed is second  # pyright: ignore[reportPrivateUsage]
    spawn._leave(second)  # pyright: ignore[reportPrivateUsage]
    assert spawn._armed is first  # pyright: ignore[reportPrivateUsage]
    spawn._leave(first)  # pyright: ignore[reportPrivateUsage]
    assert spawn._armed is None  # pyright: ignore[reportPrivateUsage]


def test_a_start_past_its_deadline_is_not_dumped_again(
    watched: tuple[Path, IO[str]],
) -> None:
    _, file = watched
    spawn.watch_starts(file)
    late, other = spawn._Handoff(), spawn._Handoff()  # pyright: ignore[reportPrivateUsage]
    late.deadline = time.monotonic() - 1
    other.deadline = time.monotonic() + 60
    spawn._enter(late)  # pyright: ignore[reportPrivateUsage]
    assert late.dumped and spawn._armed is None  # pyright: ignore[reportPrivateUsage]
    spawn._enter(other)  # pyright: ignore[reportPrivateUsage]
    assert spawn._armed is other  # pyright: ignore[reportPrivateUsage]
    spawn._leave(other)  # pyright: ignore[reportPrivateUsage]
    spawn._leave(late)  # pyright: ignore[reportPrivateUsage]


def test_the_daemon_turns_the_watchdog_on_while_it_serves() -> None:
    source = (PROJECT / "src" / "dromon" / "daemon.py").read_text()
    on = source.index("spawn.watch_starts(sys.stderr)")
    off = source.index("spawn.watch_starts(None)")
    assert on < source.index("await daemon.stop_requested.wait()") < off


# The lock that serializes every fork in the process.

TRUE = ["/bin/sh", "-c", "exit 0"]


def in_a_thread(call: Callable[[], object]) -> tuple[threading.Thread, list[object]]:
    outcome: list[object] = []

    def run() -> None:
        try:
            outcome.append(call())
        except BaseException as error:  # noqa: BLE001
            outcome.append(error)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, outcome


def test_importing_the_module_installs_the_lock_around_every_way_a_child_starts() -> (
    None
):
    """In a fresh interpreter, so that an earlier call of ``install_fork_lock`` in
    this process cannot stand in for the import."""
    code = (
        "import os, subprocess\n"
        "import dromon.spawn\n"
        "for owner, name in ((subprocess, '_fork_exec'), (os, 'posix_spawn'),"
        " (os, 'posix_spawnp')):\n"
        "    assert hasattr(getattr(owner, name), '__wrapped__'), name\n"
        "inner = subprocess._fork_exec\n"
        "assert dromon.spawn.install_fork_lock() is True\n"
        "assert subprocess._fork_exec is inner\n"
    )
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "PYTHONPATH": str(PROJECT / "src")},
    )
    assert done.returncode == 0, done.stderr


def test_a_start_waits_while_another_fork_holds_the_lock() -> None:
    with spawn.forking():
        thread, outcome = in_a_thread(lambda: spawn.start(TRUE).wait())
        thread.join(0.5)
        assert thread.is_alive() and outcome == [], "the start must wait"
    thread.join(10)
    assert outcome == [0]


def test_posix_spawn_waits_while_another_fork_holds_the_lock() -> None:
    def call() -> int:
        pid = os.posix_spawn(TRUE[0], TRUE, os.environ)
        return os.waitpid(pid, 0)[1]

    with spawn.forking():
        thread, outcome = in_a_thread(call)
        thread.join(0.5)
        assert thread.is_alive() and outcome == []
    thread.join(10)
    assert outcome == [0]


def test_an_asyncio_start_waits_while_another_fork_holds_the_lock() -> None:
    async def run() -> int | None:
        process = await asyncio.create_subprocess_exec(*TRUE)
        return await process.wait()

    with spawn.forking():
        thread, outcome = in_a_thread(lambda: asyncio.run(run()))
        thread.join(0.5)
        assert thread.is_alive() and outcome == []
    thread.join(10)
    assert outcome == [0]


def test_a_start_that_waits_too_long_for_the_lock_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawn, "FORK_LOCK_SECONDS", 0.2)
    with spawn.forking(), pytest.raises(spawn.StartStalled) as caught:
        spawn.start(TRUE)
    assert "fork lock" in str(caught.value)
    assert spawn.stranded_count() == 0, "the refused start holds no thread"
    # It did not start a child, so nothing is left to reap.
    child = spawn.start(TRUE)
    assert child.wait(10) == 0


def test_the_lock_is_released_after_a_fork_that_fails() -> None:
    with pytest.raises(FileNotFoundError):
        spawn.start(["/nonexistent/program"])
    assert spawn._fork_lock.acquire(blocking=False)  # pyright: ignore[reportPrivateUsage]
    spawn._fork_lock.release()  # pyright: ignore[reportPrivateUsage]


def test_starts_from_many_threads_fork_one_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock is counted from inside, at its own entry and not at a standard library
    function, since which function ``subprocess`` calls to fork differs by Python
    version and platform. **Found by CI on 2026-11-05.** Python 3.15 on Linux starts
    through ``os.posix_spawn``, not ``_fork_exec``. Every path takes this lock, so the
    entries are never two at once and are at least one for each start."""
    real = spawn.forking
    counts = {"now": 0, "most": 0, "calls": 0}
    guard = threading.Lock()

    @contextlib.contextmanager
    def counting() -> Generator[None]:
        with real():
            with guard:
                counts["now"] += 1
                counts["calls"] += 1
                counts["most"] = max(counts["most"], counts["now"])
            try:
                time.sleep(0.01)
                yield
            finally:
                with guard:
                    counts["now"] -= 1

    monkeypatch.setattr(spawn, "forking", counting)

    def one() -> object:
        return spawn.start(["/bin/sh", "-c", "exit 0"]).wait()

    pool = [in_a_thread(one) for _ in range(12)]
    for thread, _ in pool:
        thread.join(30)
    assert [outcome for _, outcome in pool] == [[0]] * 12
    assert counts["calls"] >= 12 and counts["most"] == 1


def test_the_watchdog_times_a_fork_that_does_not_return(
    watched: tuple[Path, IO[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    path, file = watched
    monkeypatch.setattr(spawn, "FORK_LOCK_SECONDS", 0.2)
    spawn.watch_starts(file)
    with spawn.forking():
        dump = wait_for_dump(path)
        assert "(most recent call first)" in dump and "test_spawn.py" in dump
    assert spawn._armed is None  # pyright: ignore[reportPrivateUsage]


def test_the_report_names_the_descriptor_limit_it_finds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    limits: list[tuple[int, int]] = [(321, hard)]

    def finds(_which: int) -> tuple[int, int]:
        return limits[0]

    def refuses(_which: int) -> tuple[int, int]:
        raise ValueError("no such limit")

    monkeypatch.setattr(resource, "getrlimit", finds)
    assert spawn._descriptor_limit_text() == "descriptor limit 321"  # pyright: ignore[reportPrivateUsage]
    limits[0] = (resource.RLIM_INFINITY, hard)
    assert spawn._descriptor_limit_text() == "descriptor limit unlimited"  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(resource, "getrlimit", refuses)
    assert spawn._descriptor_limit_text() == "descriptor limit unknown"  # pyright: ignore[reportPrivateUsage]


# Review of round 16, finding 3. A standard error that blocks.


class BlockedStderr:
    """A diagnostic writer that does not return until released, as a write to a pipe
    whose reader stopped does."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.lines: list[str] = []

    def write(self, text: str) -> None:
        self.lines.append(text)
        self.release.wait(30)


@pytest.fixture
def blocked_stderr(monkeypatch: pytest.MonkeyPatch) -> Iterator[BlockedStderr]:
    blocked = BlockedStderr()
    monkeypatch.setattr(spawn, "_write_line", blocked.write)
    monkeypatch.setattr(spawn, "LOG_WAIT_SECONDS", 0.5)
    yield blocked
    blocked.release.set()
    writer = spawn._log.writer  # pyright: ignore[reportPrivateUsage]
    if writer is not None:
        writer.join(5)


def test_a_blocked_standard_error_does_not_hold_a_start_that_succeeded(
    monkeypatch: pytest.MonkeyPatch, blocked_stderr: BlockedStderr
) -> None:
    """The slow-start line used to be written before the outcome was handed over, so a
    blocked standard error held a good start until the caller gave up on it. The line
    is now written after the handoff, so the caller has its child at once. The wait
    for the line is 0.5 seconds here, so a start that waited for it would take at least
    that, and this one must return in well under it."""
    monkeypatch.setattr(spawn, "SLOW_START_SECONDS", 0.0)
    before = time.monotonic()
    child = spawn.start(TRUE)
    took = time.monotonic() - before
    child.wait(10)
    assert took < spawn.LOG_WAIT_SECONDS
    assert spawn.stranded_count() == 0
    spawn.clear_slow_starts()


def test_a_blocked_standard_error_holds_at_most_one_writer_and_queues_the_rest(
    monkeypatch: pytest.MonkeyPatch, blocked_stderr: BlockedStderr
) -> None:
    monkeypatch.setattr(spawn, "SLOW_START_SECONDS", 0.0)
    monkeypatch.setattr(spawn, "LOG_INTERVAL_SECONDS", 0.0)
    for _ in range(4):
        spawn.start(TRUE).wait(10)
    writers = [t for t in threading.enumerate() if t.name == "dromon-spawn-log"]
    assert len(writers) == 1
    assert len(blocked_stderr.lines) == 1
    # Every start is still recorded, whether or not its line was written.
    assert len(spawn.slow_starts()) == 4
    spawn.clear_slow_starts()


def test_a_stall_report_is_not_dropped_behind_a_slow_start_line(
    monkeypatch: pytest.MonkeyPatch, blocked_stderr: BlockedStderr
) -> None:
    """Review of round 16, follow-up 3. A line that arrives while another is being
    written is queued and written afterwards, in order, and is not dropped."""
    spawn._say("dromond: slow start, one", limited=True, wait=0.1)  # pyright: ignore[reportPrivateUsage]
    spawn._say("dromond: sleep did not start within 30s", limited=False, wait=0.1)  # pyright: ignore[reportPrivateUsage]
    assert blocked_stderr.lines == ["dromond: slow start, one"]
    blocked_stderr.release.set()
    writer = spawn._log.writer  # pyright: ignore[reportPrivateUsage]
    if writer is not None:
        writer.join(10)
    assert blocked_stderr.lines == [
        "dromond: slow start, one",
        "dromond: sleep did not start within 30s",
    ]
    assert spawn._log.writer is None  # pyright: ignore[reportPrivateUsage]
    assert spawn._log.pending == []  # pyright: ignore[reportPrivateUsage]


def test_the_queue_is_bounded_and_drops_limited_lines_first(
    blocked_stderr: BlockedStderr,
) -> None:
    """The writer is held on the first line. Unlimited lines then fill the queue, and a
    limited line that finds it full is dropped, while an unlimited line that finds only
    unlimited lines drops the oldest. Every drop is counted."""
    log = spawn._log  # pyright: ignore[reportPrivateUsage]
    spawn._say("first", limited=False, wait=0.1)  # pyright: ignore[reportPrivateUsage]
    for number in range(spawn.MAX_LOG_PENDING):
        spawn._say(f"u{number}", limited=False, wait=0.0)  # pyright: ignore[reportPrivateUsage]
    assert len(log.pending) == spawn.MAX_LOG_PENDING
    spawn._say("limited", limited=True, wait=0.0)  # pyright: ignore[reportPrivateUsage]
    assert ("limited", True) not in log.pending
    assert log.dropped == 1
    spawn._say("newest", limited=False, wait=0.0)  # pyright: ignore[reportPrivateUsage]
    assert len(log.pending) == spawn.MAX_LOG_PENDING
    assert log.pending[0][0] == "u1" and log.pending[-1][0] == "newest"
    assert log.dropped == 2


def test_a_queued_limited_line_goes_before_an_unlimited_one_when_full() -> None:
    log = spawn._Log()  # pyright: ignore[reportPrivateUsage]
    queue = spawn._queue_line  # pyright: ignore[reportPrivateUsage]
    for number in range(spawn.MAX_LOG_PENDING):
        assert queue(log, f"n{number}", number == 3, 100.0 + number * 10) is True
    assert queue(log, "report", False, 1000.0) is True
    texts = [text for text, _ in log.pending]
    assert "n3" not in texts and texts[-1] == "report"
    assert len(texts) == spawn.MAX_LOG_PENDING and log.dropped == 1


def test_a_limited_line_inside_the_interval_is_refused_and_not_counted() -> None:
    log = spawn._Log()  # pyright: ignore[reportPrivateUsage]
    queue = spawn._queue_line  # pyright: ignore[reportPrivateUsage]
    assert queue(log, "a", True, 100.0) is True
    assert queue(log, "b", True, 100.0 + spawn.LOG_INTERVAL_SECONDS / 2) is False
    assert queue(log, "c", False, 100.0) is True
    assert [t for t, _ in log.pending] == ["a", "c"] and log.dropped == 0


def test_a_line_queued_as_the_writer_ends_is_still_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The writer decides that it is finished under the lock, so a line queued after
    that starts a new writer and is not stranded."""
    written: list[str] = []
    monkeypatch.setattr(spawn, "_write_line", written.append)
    for number in range(20):
        spawn._say(f"line {number}", limited=False, wait=5.0)  # pyright: ignore[reportPrivateUsage]
    deadline = time.monotonic() + 10
    while len(written) < 20 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert written == [f"line {number}" for number in range(20)]


def test_the_slow_start_line_is_written_at_most_once_in_the_interval(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(spawn, "SLOW_START_SECONDS", 0.0)
    monkeypatch.setattr(spawn, "LOG_INTERVAL_SECONDS", 60.0)
    for _ in range(3):
        spawn.start(TRUE).wait(10)
    assert capsys.readouterr().err.count("dromond: slow start, ") == 1
    assert len(spawn.slow_starts()) == 3
    spawn.clear_slow_starts()


def test_a_blocked_standard_error_does_not_hold_the_caller_of_a_stalled_start(
    stalled: "StalledStart", blocked_stderr: BlockedStderr
) -> None:
    """The stall message is written by a thread of its own, so the caller is told of
    the stall within its bound and the wait for the line, and not when the line is
    written."""
    before = time.monotonic()
    with pytest.raises(spawn.StartStalled):
        spawn.start(SLEEP)
    assert time.monotonic() - before < BOUND + spawn.LOG_WAIT_SECONDS + 5
    assert any("did not start within" in line for line in blocked_stderr.lines)


def test_a_writer_that_raises_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The error path. A standard error that is closed fails each write, and the start
    it describes goes on."""

    class Closed:
        def write(self, text: str) -> int:
            raise ValueError("I/O operation on closed file")

        def flush(self) -> None:
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(spawn, "SLOW_START_SECONDS", 0.0)
    monkeypatch.setattr(sys, "stderr", Closed())
    spawn.start(TRUE).wait(10)
    assert len(spawn.slow_starts()) == 1
    spawn.clear_slow_starts()


# Review of round 16, finding 4. The fork lock and the event loop.


def test_a_start_made_while_the_thread_is_starting_one_is_refused_at_once() -> None:
    """The lock is not reentrant, so a nested start used to wait for itself for the
    whole bound and report a stall. It is now refused at once, and the lock is free
    afterwards, so the error leaves nothing held."""
    before = time.monotonic()
    with (
        spawn.forking(),
        pytest.raises(RuntimeError, match="already starting one"),
        spawn.forking(),
    ):
        pytest.fail("a nested start must not enter")
    assert time.monotonic() - before < 5
    # The error did not leave the lock or the flag held.
    with spawn.forking():
        pass
    spawn.start(TRUE).wait(10)


def test_no_start_path_nests_a_start() -> None:
    """Every way this process starts a child, one after another on one thread, none
    nests a start inside another. A nested one raises the error above, so this fails
    if any path of ``spawn``, ``subprocess``, ``os.posix_spawn`` or asyncio does."""
    spawn.start(TRUE).wait(10)
    subprocess.run(TRUE, check=True, timeout=30)  # noqa: S603
    subprocess.Popen(TRUE).wait(10)  # noqa: S603
    pid = os.posix_spawn("/bin/sh", ["sh", "-c", "exit 0"], os.environ)
    os.waitpid(pid, 0)

    async def scenario() -> None:
        child = await asyncio.create_subprocess_exec(*TRUE)
        await asyncio.wait_for(child.wait(), 30)

    asyncio.run(scenario())


def test_a_start_on_the_event_loop_waits_a_shorter_time_for_the_fork_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Another thread holds the lock, as a stuck fork does. A start on the loop's own
    thread gives up after the loop bound, which here is 0.3 seconds against a bound of
    30 for the worker threads, and ends in ``StartStalled``, an ``OSError`` the drivers
    treat as a failed start. A start on a plain thread waits the longer bound."""
    monkeypatch.setattr(spawn, "FORK_LOCK_LOOP_SECONDS", 0.3)
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with spawn.forking():
            held.set()
            release.wait(30)

    thread = threading.Thread(target=holder, name="holds-the-fork-lock")
    thread.start()
    assert held.wait(10)
    try:

        async def scenario() -> float:
            began = time.monotonic()
            with pytest.raises(spawn.StartStalled, match="held the fork lock for 0.3s"):
                await asyncio.create_subprocess_exec(*TRUE)
            return time.monotonic() - began

        took = asyncio.run(scenario())
        assert took < 10
    finally:
        release.set()
        thread.join(10)
    assert not spawn._fork_lock.locked()  # pyright: ignore[reportPrivateUsage]


def test_a_plain_thread_start_waits_for_the_lock_and_succeeds_when_it_is_freed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same rule. A thread that is not a loop's waits for a
    holder that ends soon, which the shorter loop bound would not allow."""
    monkeypatch.setattr(spawn, "FORK_LOCK_LOOP_SECONDS", 0.05)
    held = threading.Event()

    def holder() -> None:
        with spawn.forking():
            held.set()
            time.sleep(0.5)

    thread = threading.Thread(target=holder, name="holds-the-fork-lock")
    thread.start()
    assert held.wait(10)
    spawn.start(TRUE).wait(10)
    thread.join(10)


def test_the_stall_message_names_the_program_by_its_base_name_only(
    stalled: "StalledStart",
) -> None:
    """Review of round 16, finding 5. The full path of a program under a home directory
    reached the log and the ``StartStalled`` message. Only the base name does."""
    with pytest.raises(spawn.StartStalled) as caught:
        spawn.start(["/home/someone/private-dir/prog", "--flag"])
    message = str(caught.value)
    assert message.startswith(f"prog did not start within {BOUND:g}s")
    assert "private-dir" not in message and "someone" not in message


def test_the_refusal_past_the_stranded_cap_names_the_program_by_its_base_name_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawn, "MAX_STRANDED", 0)
    with pytest.raises(spawn.StartStalled) as caught:
        spawn.start(["/home/someone/private-dir/prog"])
    message = str(caught.value)
    assert message.startswith("prog was not started")
    assert "private-dir" not in message


# Round 16, from the CI logs of pull request 149. Every fork call is timed.


def test_machine_text_gives_the_size_of_the_process_steal_and_throttling(
    tmp_path: Path,
) -> None:
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "status").write_text(
        "Name:\tpython\nVmSize:\t  2097152 kB\nVmRSS:\t  307200 kB\nThreads:\t5\n"
    )
    (tmp_path / "stat").write_text("cpu  1 2 3 4 5 6 7 1234 0 0\ncpu0 1\n")
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cpu.stat").write_text(
        "usage_usec 9\nnr_periods 100\nnr_throttled 7\nthrottled_usec 2500000\n"
    )
    text = spawn._machine_text(tmp_path, cgroup)  # pyright: ignore[reportPrivateUsage]
    assert "parent resident 300MB virtual 2048MB" in text
    assert "cpu steal total 12.3s" in text
    assert "cgroup throttled 7 times for 2.5s" in text


def test_machine_text_survives_hostile_size_steal_and_throttle_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "self").mkdir()
    (tmp_path / "self" / "status").write_text("VmRSS:\t\x1b[31m\nVmSize:\tmany kB\n")
    (tmp_path / "stat").write_text("cpu  a b c\n")
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cpu.stat").write_text("nr_throttled x\nthrottled_usec\n")
    text = spawn._machine_text(tmp_path, cgroup)  # pyright: ignore[reportPrivateUsage]
    assert "\x1b" not in text
    assert "resident" not in text and "steal" not in text and "throttled" not in text


def test_machine_text_without_proc_or_cgroup_gives_what_remains(
    tmp_path: Path,
) -> None:
    text = spawn._machine_text(tmp_path / "none", tmp_path / "none")  # pyright: ignore[reportPrivateUsage]
    assert "resident" not in text and "steal" not in text and "throttled" not in text


def test_a_slow_fork_call_is_recorded_whoever_started_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The ledger saw only ``spawn.start``, and the slow tests of pull request 149
    started their children through ``asyncio`` and ``subprocess`` directly. The fork
    call is timed where every start passes."""
    monkeypatch.setattr(spawn, "SLOW_FORK_SECONDS", 0.0)

    async def scenario() -> None:
        child = await asyncio.create_subprocess_exec(*TRUE)
        await asyncio.wait_for(child.wait(), 30)

    asyncio.run(scenario())
    notes = spawn.slow_starts()
    assert notes, "an asyncio start was not recorded"
    assert notes[0].startswith("fork call ")
    assert "of its own processor time" in notes[0]
    assert "waiting for the fork lock" in notes[0] and "thread " in notes[0]
    assert "dromond: slow fork, " in capsys.readouterr().err
    spawn.clear_slow_starts()
    subprocess.run(TRUE, check=True, timeout=30)  # noqa: S603
    assert len(spawn.slow_starts()) == 1, "a subprocess.run start was not recorded"
    spawn.clear_slow_starts()


def test_a_fork_call_under_the_threshold_is_not_recorded() -> None:
    subprocess.run(TRUE, check=True, timeout=30)  # noqa: S603
    assert spawn.slow_starts() == []


def test_the_fork_call_records_the_threads_own_processor_time() -> None:
    """Spinning inside the fork lock uses the thread's processor time and sleeping
    inside it does not, so the figure tells a thread that worked from one that
    waited, which the wall time cannot. **Review of round 16, follow-up 1.** The spin
    runs until the thread's own clock has advanced by a fixed amount, with a long wall
    bound only against a hang, so a loaded machine slows the test and cannot fail it.
    The sleeper is compared with the spinner and not with a constant."""
    wanted = 0.05
    busy, idle = spawn._ForkTimes(), spawn._ForkTimes()  # pyright: ignore[reportPrivateUsage]
    spawn._fork_slot.current = busy  # pyright: ignore[reportPrivateUsage]
    try:
        with spawn.forking():
            began = time.thread_time()
            bound = time.monotonic() + 60.0
            while time.thread_time() - began < wanted and time.monotonic() < bound:
                pass
        spawn._fork_slot.current = idle  # pyright: ignore[reportPrivateUsage]
        with spawn.forking():
            time.sleep(0.2)
    finally:
        spawn._fork_slot.current = None  # pyright: ignore[reportPrivateUsage]
    assert busy.cpu >= wanted
    assert 0.0 <= idle.cpu < busy.cpu


def test_a_slow_fork_note_does_not_wait_for_a_blocked_standard_error(
    monkeypatch: pytest.MonkeyPatch, blocked_stderr: BlockedStderr
) -> None:
    """The caller may be the event loop, so the note is written without waiting."""
    monkeypatch.setattr(spawn, "SLOW_FORK_SECONDS", 0.0)
    before = time.monotonic()
    subprocess.run(TRUE, check=True, timeout=30)  # noqa: S603
    assert time.monotonic() - before < spawn.LOG_WAIT_SECONDS
    spawn.clear_slow_starts()
