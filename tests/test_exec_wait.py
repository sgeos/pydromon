"""The exec-status wait of a start is timed. **Round 18.**

A direct ``subprocess.Popen`` in a test hung for five minutes in the read of the exec
status pipe, after the fork had returned, which neither the bound of ``spawn.start``
nor the ledger of slow forks saw. ``spawn`` now queues every fork that returns and
looks, ``EXEC_WAIT_SECONDS`` later, at the frames of the starting thread. These tests
make a real child that is stuck between its fork and its exec, with ``preexec_fn``,
which runs in the child at that point.
"""

import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from dromon import spawn

SLEEP_BEFORE_EXEC = 1.5


def _slow_exec() -> None:
    time.sleep(SLEEP_BEFORE_EXEC)


def _wait_for(condition: object, seconds: float = 10.0) -> bool:
    assert callable(condition)
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return bool(condition())


@pytest.fixture
def sampled(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Replace the sampler with one that records its pid, so the test neither runs
    the operating system's sampler nor depends on it."""
    seen: list[int] = []

    def fake(pid: int, **options: object) -> str:
        del options
        seen.append(pid)
        return "sampled by the test"

    monkeypatch.setattr(spawn, "sample_process", fake)
    monkeypatch.setattr(spawn, "EXEC_WAIT_SECONDS", 0.3)
    return seen


def _exec_entries() -> list[str]:
    return [line for line in spawn.slow_starts() if line.startswith("exec wait")]


def test_a_child_stuck_between_fork_and_exec_is_sampled_and_recorded(
    sampled: list[int],
) -> None:
    child = subprocess.Popen(  # noqa: S603  # direct on purpose, the start under test
        [sys.executable, "-I", "-c", "pass"], preexec_fn=_slow_exec
    )
    try:
        assert child.wait(30) == 0
    finally:
        child.kill()
    assert _wait_for(lambda: bool(_exec_entries()))
    [entry] = _exec_entries()
    assert str(child.pid) in entry
    assert "not executed within" in entry
    assert "sampled by the test" in entry
    assert sampled == [child.pid]


def test_the_report_is_made_while_the_start_is_still_waiting() -> None:
    """The point of the change. A start that never returns is reported, so the entry
    exists before the call does."""
    sampled_pids: list[int] = []
    release = threading.Event()

    def fake(pid: int, **options: object) -> str:
        del options
        sampled_pids.append(pid)
        release.set()
        return "stack"

    started: list[subprocess.Popen[bytes]] = []

    def start() -> None:
        started.append(
            subprocess.Popen(  # noqa: S603  # direct on purpose
                [sys.executable, "-I", "-c", "pass"], preexec_fn=_slow_exec_long
            )
        )

    original = (spawn.sample_process, spawn.EXEC_WAIT_SECONDS)
    spawn.sample_process = fake  # type: ignore[assignment]
    spawn.EXEC_WAIT_SECONDS = 0.3
    try:
        thread = threading.Thread(target=start, name="test-start")
        thread.start()
        assert release.wait(20), "no report while the start was still waiting"
        assert not started, "the start had already returned"
        thread.join(60)
    finally:
        spawn.sample_process, spawn.EXEC_WAIT_SECONDS = original
    assert started and started[0].wait(30) == 0


def _slow_exec_long() -> None:
    time.sleep(2.5)


def test_a_start_that_executes_in_time_leaves_no_entry_and_no_sample(
    sampled: list[int],
) -> None:
    child = subprocess.Popen(  # noqa: S603  # direct on purpose
        [sys.executable, "-I", "-c", "pass"]
    )
    assert child.wait(30) == 0
    time.sleep(0.8)
    assert _exec_entries() == []
    assert sampled == []


def test_a_thread_that_has_left_the_start_is_not_in_the_exec_wait() -> None:
    ident = threading.get_ident()
    assert not spawn._in_exec_wait(ident, -1)  # pyright: ignore[reportPrivateUsage]
    with spawn._exec_lock:  # pyright: ignore[reportPrivateUsage]
        spawn._exec_tokens[ident] = 7  # pyright: ignore[reportPrivateUsage]
    try:
        # The token matches and this thread is not inside ``_execute_child``.
        assert not spawn._in_exec_wait(ident, 7)  # pyright: ignore[reportPrivateUsage]
        # A later fork by the thread has changed the token.
        assert not spawn._in_exec_wait(ident, 6)  # pyright: ignore[reportPrivateUsage]
    finally:
        with spawn._exec_lock:  # pyright: ignore[reportPrivateUsage]
            spawn._exec_tokens.pop(ident, None)  # pyright: ignore[reportPrivateUsage]


def test_a_fork_call_that_raises_leaves_no_token_and_nothing_queued() -> None:
    def refuse(*args: object, **kwargs: object) -> int:
        del args, kwargs
        raise OSError("fork failed")

    before = len(spawn._exec_pending)  # pyright: ignore[reportPrivateUsage]
    watched = spawn._exec_watched(refuse)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(OSError, match="fork failed"):
        watched(["x"])
    assert threading.get_ident() not in spawn._exec_tokens  # pyright: ignore[reportPrivateUsage]
    assert len(spawn._exec_pending) == before  # pyright: ignore[reportPrivateUsage]


def test_forks_past_the_watch_limit_are_not_queued(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(spawn, "MAX_EXEC_WATCHED", 0)
    child = subprocess.Popen([sys.executable, "-I", "-c", "pass"])  # noqa: S603
    assert child.wait(30) == 0
    assert threading.get_ident() not in spawn._exec_tokens  # pyright: ignore[reportPrivateUsage]


def test_the_wrapper_keeps_the_original_fork_call_reachable() -> None:
    wrapped: Any = getattr(subprocess, "_fork_exec")  # noqa: B009
    inner = getattr(wrapped, "__wrapped__")  # noqa: B009
    assert hasattr(inner, "__wrapped__"), "the fork lock is still inside the watch"
