"""``dromon.spawn`` bounds a child's start apart from its run. **Round 15.** A start
once stalled for five minutes in ``subprocess``'s ``_execute_child`` on macOS, in
several tests and in continuous integration, as ``dromon.spawn`` records. A stalled
start is stood in for by a ``Popen`` that waits for the test to open a gate, since no
test can make the operating system stall a real one on demand."""

import ast
import asyncio
import io
import os
import signal
import subprocess
import sys
import time
import threading
import tomllib
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

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
    assert message.startswith(f"/bin/sleep did not start within {BOUND:g}s")
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
    assert done.stderr.startswith(f"/bin/sleep did not start within {BOUND:g}s")
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
    assert message.startswith(f"/bin/sleep did not start within {BOUND:g}s")
    report = message.split(" Report, ", 1)[1]
    assert len(report) <= spawn.MAX_REPORT_CHARS
    # The thread is in the held ``Popen``, reached from ``spawn``'s own ``target``.
    assert "the start thread is at" in report and "target" in report
    assert "1 starts in flight now and 1 at most during this one" in report
    assert "threads alive" in report and "of the daemon" in report
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
        assert mine[0].state in ("R", "S")
    finally:
        child.kill()
        child.wait()


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
    assert "dromon-spawn" in dump, "the stuck start's thread is in the dump"
    assert "(most recent call first)" in dump


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
    """The call that forks is counted from inside the lock, and is never entered by
    two threads at once."""
    installed: Any = getattr(subprocess, "_fork_exec")  # noqa: B009
    inner: Callable[..., Any] = getattr(installed, "__wrapped__")  # noqa: B009
    counts = {"now": 0, "most": 0, "calls": 0}
    guard = threading.Lock()

    def counting(*args: object) -> object:
        with guard:
            counts["now"] += 1
            counts["calls"] += 1
            counts["most"] = max(counts["most"], counts["now"])
        try:
            time.sleep(0.01)
            return inner(*args)
        finally:
            with guard:
                counts["now"] -= 1

    monkeypatch.setattr(subprocess, "_fork_exec", spawn._serialized(counting))  # pyright: ignore[reportPrivateUsage, reportUnknownMemberType, reportAttributeAccessIssue]

    def one() -> object:
        return spawn.start(["/bin/sh", "-c", "exit 0"]).wait()

    pool = [in_a_thread(one) for _ in range(12)]
    for thread, _ in pool:
        thread.join(30)
    assert [outcome for _, outcome in pool] == [[0]] * 12
    assert counts["calls"] == 12 and counts["most"] == 1


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
