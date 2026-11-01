"""``land.run_command`` bounds a command's start apart from its run. **Found on
2026-11-01.** A land test hung for five minutes in ``subprocess``'s ``_execute_child``
on macOS in continuous integration, as ``land.CommandStart`` records. A stalled start
is stood in for by a ``Popen`` that waits for the test to open a gate, since no test
can make the operating system stall a real one on demand."""

import signal
import subprocess
import threading
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from dromon import land

SLEEP = ("/bin/sleep", "60")
"""A command that runs until it is killed."""


def gated_popen(
    gate: threading.Event, started: list[subprocess.Popen[str]]
) -> type[subprocess.Popen[str]]:
    """A ``Popen`` whose start waits for ``gate``, at most ten seconds, and which
    keeps each command it started in ``started``."""

    class Gated(subprocess.Popen[str]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            assert gate.wait(10), "the test never opened the gate"
            super().__init__(*args, **kwargs)
            started.append(self)

    return Gated


def patch_popen(monkeypatch: pytest.MonkeyPatch, popen: type[object]) -> None:
    """Give ``land`` a ``subprocess`` whose ``Popen`` is ``popen``, leaving the real
    module to every other caller in the process."""
    monkeypatch.setattr(
        land,
        "subprocess",
        SimpleNamespace(
            Popen=popen, PIPE=subprocess.PIPE, TimeoutExpired=subprocess.TimeoutExpired
        ),
    )


def until_reaped(started: list[subprocess.Popen[str]]) -> subprocess.Popen[str]:
    """The one command started, once its pipes are closed, which the reap does last,
    within ten seconds."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if started and started[0].stdout is not None and started[0].stdout.closed:
            return started[0]
        time.sleep(0.01)
    pytest.fail("the late command was not reaped within ten seconds")


def test_a_start_that_stalls_fails_fast_and_its_late_command_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    started: list[subprocess.Popen[str]] = []
    patch_popen(monkeypatch, gated_popen(gate, started))
    before = time.monotonic()
    done = land.run_command(SLEEP, tmp_path, start_seconds=0.2)
    assert time.monotonic() - before < 5
    assert done.code == land.STALLED
    assert done.stderr.startswith("/bin/sleep did not start within 0.2s")
    assert "killed if it starts later" in done.stderr
    assert started == []
    gate.set()
    process = until_reaped(started)
    assert process.returncode == -signal.SIGKILL


def test_the_caller_keeps_a_command_that_started_by_its_deadline(
    tmp_path: Path,
) -> None:
    """The hand-over in one order. The start ends first, so the caller owns the
    command, and nothing kills it."""
    start = land.CommandStart(
        SLEEP, tmp_path, piped_input=False, stream=False, env=None
    )
    start.run()
    process = start.wait(0)
    assert isinstance(process, subprocess.Popen)
    try:
        assert process.poll() is None
    finally:
        process.kill()
        process.communicate()


def test_a_command_that_starts_after_its_caller_gave_up_is_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hand-over in the other order. The caller gives up first, so the start
    kills and reaps the command it makes, and closes its pipes."""
    gate = threading.Event()
    gate.set()
    started: list[subprocess.Popen[str]] = []
    patch_popen(monkeypatch, gated_popen(gate, started))
    start = land.CommandStart(SLEEP, tmp_path, piped_input=True, stream=False, env=None)
    assert start.wait(0) is None
    start.run()
    (process,) = started
    assert process.returncode == -signal.SIGKILL
    for stream in (process.stdin, process.stdout, process.stderr):
        assert stream is not None
        assert stream.closed


def test_input_output_and_status_pass_as_subprocess_run_gave_them(
    tmp_path: Path,
) -> None:
    assert land.run_command(["/bin/cat"], tmp_path, "hello\n") == land.Completed(
        0, "hello\n", ""
    )
    failing = ["/bin/sh", "-c", "echo out; echo err >&2; exit 3"]
    assert land.run_command(failing, tmp_path) == land.Completed(3, "out\n", "err\n")
    seen = land.run_command(["/bin/pwd", "-P"], tmp_path)
    assert seen.stdout == f"{tmp_path.resolve()}\n"


def test_a_start_that_raises_other_than_os_error_raises_in_the_caller(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="illegal environment variable name"):
        land.run_command(["/bin/echo"], tmp_path, env={"A=B": "x"})


@pytest.mark.parametrize("bound", [0.0, -1.0])
def test_a_start_bound_not_above_zero_is_refused(tmp_path: Path, bound: float) -> None:
    with pytest.raises(ValueError, match="must be above 0"):
        land.run_command(["/bin/echo"], tmp_path, start_seconds=bound)


def test_the_start_bound_ends_a_stall_well_before_the_test_run_is_ended() -> None:
    """A stalled start should fail its own test, not end the whole run, which
    pytest's faulthandler does after its timeout."""
    project = Path(__file__).resolve().parent.parent
    options = tomllib.loads((project / "pyproject.toml").read_text())
    timeout = options["tool"]["pytest"]["ini_options"]["faulthandler_timeout"]
    assert timeout > land.START_SECONDS * 2
