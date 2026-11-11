"""Starts of children from several threads at once, to measure the stall that
``dromon.spawn`` describes. **Round 16.** No stall has been reproduced off continuous
integration, so these tests measure and bound what they can. Each run adds a line to
the run's summary, under "child start stress", with the number of starts, how many
stalled, the slowest start and the longest pause a ticker thread saw, which is how long
every Python thread was frozen at once, as a ``vfork`` that waits for a slow program
freezes them. The counts are taken from the environment, so a larger run is possible
on a machine that can spare it, with ``DROMON_STRESS_THREADS`` for the threads:

    DROMON_STRESS_STARTS=20000 uv run pytest tests/test_spawn_stress.py

The default size is small enough for the gate and for a run on the shared machine.
"""

import asyncio
import contextlib
import os
import statistics
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field

import pytest
from conftest import SLOW_TEST_SECONDS, STRESS_NOTES, slow_test_line

from dromon import spawn

STARTS = int(os.environ.get("DROMON_STRESS_STARTS", "160"))
THREADS = int(os.environ.get("DROMON_STRESS_THREADS", "8"))
BOUND = 20.0
"""The bound handed to each start. Below the default 30 seconds, so that a test that
meets a real stall fails before the 300 second timeout of the run."""
PAUSE_LIMIT = 10.0
"""The longest freeze of every Python thread that the tests accept."""
SHELL = ["/bin/sh", "-c", "exit 0"]


@dataclass
class Tally:
    """What the worker threads found, each appended to under ``lock``."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    seconds: list[float] = field(default_factory=lambda: [])
    stalled: int = 0
    errors: list[str] = field(default_factory=lambda: [])
    bad_exits: int = 0
    pauses: list[float] = field(default_factory=lambda: [])


def _timed(tally: Tally, make: Callable[[], int]) -> None:
    began = time.monotonic()
    try:
        code = make()
    except spawn.StartStalled:
        with tally.lock:
            tally.stalled += 1
        return
    except Exception as error:  # noqa: BLE001
        with tally.lock:
            tally.errors.append(f"{type(error).__name__}: {error}"[:200])
        return
    with tally.lock:
        tally.seconds.append(time.monotonic() - began)
        if code != 0:
            tally.bad_exits += 1


def _start_and_wait() -> int:
    return spawn.start(SHELL, start_seconds=BOUND).wait(BOUND)


def _run_with_pipes() -> int:
    done = spawn.run(
        ["/bin/sh", "-c", "echo out; echo err >&2"],
        capture_output=True,
        text=True,
        timeout=BOUND,
        check=False,
    )
    return done.returncode


def _direct_posix_spawn() -> int:
    pid = os.posix_spawn(SHELL[0], SHELL, os.environ)
    return os.waitpid(pid, 0)[1]


def _asyncio_start() -> int:
    async def go() -> int | None:
        process = await asyncio.create_subprocess_exec(*SHELL)
        return await process.wait()

    return asyncio.run(go()) or 0


KINDS: list[Callable[[], int]] = [
    _start_and_wait,
    _run_with_pipes,
    _start_and_wait,
    _direct_posix_spawn,
    _run_with_pipes,
    _asyncio_start,
]
"""The mix of starts, which takes the paths a daemon takes, ``spawn.start``,
``spawn.run`` with pipes, ``os.posix_spawn`` called directly, and ``asyncio``."""


def _worker(tally: Tally, number: int, count: int) -> None:
    for step in range(count):
        _timed(tally, KINDS[(number + step) % len(KINDS)])


def _ticker(tally: Tally, stop: threading.Event) -> None:
    """Sleeps ten milliseconds at a time and records how long past that it slept. A
    thread that cannot get the interpreter lock back overshoots, and when every thread
    is frozen at once by a call that holds the lock the overshoot is the freeze."""
    while not stop.is_set():
        before = time.monotonic()
        time.sleep(0.01)
        late = time.monotonic() - before - 0.01
        if late > 0.05:
            with tally.lock:
                tally.pauses.append(late)


def _stress(label: str) -> Tally:
    tally = Tally()
    stop = threading.Event()
    ticker = threading.Thread(target=_ticker, args=(tally, stop), daemon=True)
    ticker.start()
    each = max(STARTS // THREADS, 1)
    workers = [
        threading.Thread(target=_worker, args=(tally, n, each), daemon=True)
        for n in range(THREADS)
    ]
    began = time.monotonic()
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(BOUND * 4 + each * 2)
    elapsed = time.monotonic() - began
    stop.set()
    ticker.join(5)
    alive = sum(worker.is_alive() for worker in workers)
    ordered = sorted(tally.seconds)
    p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))] if ordered else 0.0
    median = statistics.median(ordered) if ordered else 0.0
    STRESS_NOTES.append(
        f"{label}: {each * THREADS} starts from {THREADS} threads in {elapsed:.1f}s,"
        f" {tally.stalled} stalled, {len(tally.errors)} failed,"
        f" start median {median * 1000:.0f}ms p99 {p99 * 1000:.0f}ms"
        f" max {max(ordered, default=0.0) * 1000:.0f}ms,"
        f" longest pause of all threads {max(tally.pauses, default=0.0) * 1000:.0f}ms,"
        f" {sys.platform} vfork={getattr(subprocess, '_USE_VFORK', None)}"
        f" posix_spawn={getattr(subprocess, '_USE_POSIX_SPAWN', None)},"
        f" {spawn._machine_text()}"  # pyright: ignore[reportPrivateUsage]
    )
    assert alive == 0, f"{alive} worker threads did not end"
    return tally


@contextlib.contextmanager
def _without_the_fork_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[None]:
    """The module as it was before round 15, where starts overlap."""

    @contextlib.contextmanager
    def nothing() -> Generator[None]:
        yield

    monkeypatch.setattr(spawn, "forking", nothing)
    yield


def test_starts_from_several_threads_neither_stall_nor_fail() -> None:
    tally = _stress("locked")
    assert tally.stalled == 0, "a start stalled, see the daemon's report above"
    assert tally.errors == []
    assert tally.bad_exits == 0
    assert len(tally.seconds) == (STARTS // THREADS or 1) * THREADS
    assert max(tally.pauses, default=0.0) < PAUSE_LIMIT, (
        "every thread was frozen at once for too long"
    )


def test_the_same_starts_without_the_fork_lock_are_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The comparison for the fork lock. It does not fail on a stall, since a stall
    here is what it measures, only on a child that is lost or a thread that does not
    end, and a run that finds stalls says so in the summary."""
    with _without_the_fork_lock(monkeypatch):
        tally = _stress("unlocked")
    assert tally.bad_exits == 0
    assert tally.errors == []


def test_a_stalled_start_in_the_stress_is_counted_not_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tally itself, with a start that raises ``StartStalled`` and one that
    raises another error, so that a run that stalls reports it."""
    tally = Tally()

    def stalled() -> int:
        raise spawn.StartStalled("stand-in")

    def broken() -> int:
        raise RuntimeError("stand-in")

    def bad() -> int:
        return 3

    for make in (stalled, broken, bad):
        _timed(tally, make)
    assert tally.stalled == 1
    assert tally.errors == ["RuntimeError: stand-in"]
    assert tally.bad_exits == 1


def test_a_slow_test_is_named_with_the_load_and_a_quick_one_is_not() -> None:
    assert slow_test_line("tests/x.py::quick", SLOW_TEST_SECONDS - 0.1) is None
    line = slow_test_line("tests/x.py::slow", SLOW_TEST_SECONDS + 12.34)
    assert line is not None
    assert line.startswith("tests/x.py::slow: slow test, ")
    assert f"{SLOW_TEST_SECONDS + 12.34:.1f}s" in line
    assert spawn._machine_text() in line  # pyright: ignore[reportPrivateUsage]
