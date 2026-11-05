"""``dromon.spawn`` bounds a child's start apart from its run. **Round 15.** A start
once stalled for five minutes in ``subprocess``'s ``_execute_child`` on macOS, in
several tests and in continuous integration, as ``dromon.spawn`` records. A stalled
start is stood in for by a ``Popen`` that waits for the test to open a gate, since no
test can make the operating system stall a real one on demand."""

import ast
import os
import signal
import subprocess
import sys
import time
import threading
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

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
