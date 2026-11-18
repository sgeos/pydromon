"""The bounded start on the event loop. **Round 18, item 3.**

``spawn.subprocess_exec`` used ``asyncio``'s own start, whose ``Popen`` runs on the
loop's thread, so a start that stalled before its program ran stopped the loop and no
bound could end the wait. It now starts through ``spawn.start``, on a thread of its own,
and wraps the child as ``BoundedProcess``. The first two tests are the defect, shown
with ``asyncio``'s own start as a control and then with the new one.
"""

import asyncio
import subprocess
import sys
import threading
import time
from typing import Any

import pytest

from dromon import spawn

TRUE = ["/usr/bin/true"]
ECHO = [
    sys.executable,
    "-I",
    "-c",
    "import sys\nfor line in sys.stdin:\n    sys.stdout.write(line.upper())\n"
    "    sys.stdout.flush()\n",
]
SLEEP = [sys.executable, "-I", "-c", "import time; time.sleep(60)"]


def blocking_popen(
    monkeypatch: pytest.MonkeyPatch, seconds: float
) -> list[subprocess.Popen[bytes]]:
    """Make every ``Popen`` block for ``seconds`` before it starts its child, as a fork
    that stalls does, and return the children it then made."""
    real = spawn._REAL_POPEN  # pyright: ignore[reportPrivateUsage]
    made: list[subprocess.Popen[bytes]] = []

    class Slow(real):  # type: ignore[valid-type, misc]
        """A ``Popen`` whose start takes ``seconds``, and still a ``Popen``, since the
        late-start path of ``spawn`` tests the outcome's type."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            time.sleep(seconds)
            super().__init__(*args, **kwargs)
            made.append(self)

    monkeypatch.setattr(spawn.subprocess, "Popen", Slow)
    return made


async def longest_gap(seconds: float) -> float:
    """The longest time between two turns of the loop over ``seconds``."""
    longest = 0.0
    end = time.monotonic() + seconds
    last = time.monotonic()
    while time.monotonic() < end:
        await asyncio.sleep(0.01)
        now = time.monotonic()
        longest = max(longest, now - last)
        last = now
    return longest


def test_asyncios_own_start_stops_the_loop_for_as_long_as_the_fork_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control, which is the defect. A stall of 0.6 seconds in the fork is a gap of
    about that long in a loop that is only sleeping."""
    blocking_popen(monkeypatch, 0.6)

    async def scenario() -> float:
        watching = asyncio.ensure_future(longest_gap(1.5))
        await asyncio.sleep(0.2)
        process = await asyncio.create_subprocess_exec(*TRUE)
        await process.wait()
        return await watching

    assert asyncio.run(scenario()) >= 0.4


def test_the_bounded_start_leaves_the_loop_running_while_the_fork_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocking_popen(monkeypatch, 0.6)

    async def scenario() -> float:
        watching = asyncio.ensure_future(longest_gap(1.5))
        await asyncio.sleep(0.2)
        process = await spawn.subprocess_exec(*TRUE)
        assert await process.wait() == 0
        return await watching

    assert asyncio.run(scenario()) < 0.3


def test_a_start_that_stalls_past_its_bound_raises_at_the_bound_on_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = blocking_popen(monkeypatch, 1.5)

    async def scenario() -> tuple[float, float]:
        watching = asyncio.ensure_future(longest_gap(1.0))
        began = time.monotonic()
        with pytest.raises(spawn.StartStalled):
            await spawn.subprocess_exec(*SLEEP, start_seconds=0.3)
        return time.monotonic() - began, await watching

    waited, gap = asyncio.run(scenario())
    assert waited < 1.0, "the start ended at its bound and not when the fork returned"
    assert gap < 0.3, "the loop kept running while the start was stuck"
    # The child that starts late was ended and reaped by ``spawn.start``.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not made:
        time.sleep(0.05)
    assert made
    assert made[0].wait(timeout=10) is not None


def test_a_caller_cancelled_while_the_child_starts_does_not_leave_it_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = blocking_popen(monkeypatch, 0.6)

    async def scenario() -> None:
        task = asyncio.ensure_future(spawn.subprocess_exec(*SLEEP))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # The loop stays up until the child has been made and ended.
        await asyncio.sleep(1.2)

    asyncio.run(scenario())
    assert len(made) == 1
    assert made[0].wait(timeout=10) is not None


def test_a_child_whose_streams_cannot_be_connected_is_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made: list[subprocess.Popen[Any]] = []
    real = spawn._REAL_POPEN  # pyright: ignore[reportPrivateUsage]

    def record(*args: object, **kwargs: object) -> subprocess.Popen[Any]:
        child = real(*args, **kwargs)  # type: ignore[arg-type]  # noqa: S603  # direct on purpose
        made.append(child)
        return child

    monkeypatch.setattr(spawn.subprocess, "Popen", record)

    async def refuse(self: spawn.BoundedProcess, limit: int) -> None:
        del self, limit
        raise RuntimeError("no streams")

    monkeypatch.setattr(spawn.BoundedProcess, "connect", refuse)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="no streams"):
            await spawn.subprocess_exec(*SLEEP, stdout=subprocess.PIPE)

    asyncio.run(scenario())
    assert made[0].wait(timeout=10) is not None


def test_a_child_is_written_to_and_read_from_through_its_streams() -> None:
    async def scenario() -> list[bytes]:
        process = await spawn.subprocess_exec(
            *ECHO, stdin=subprocess.PIPE, stdout=subprocess.PIPE
        )
        assert process.returncode is None
        assert process.stdin is not None and process.stdout is not None
        seen: list[bytes] = []
        for word in (b"alpha", b"beta"):
            process.stdin.write(word + b"\n")
            await process.stdin.drain()
            seen.append(await asyncio.wait_for(process.stdout.readline(), 30))
        process.stdin.close()
        assert await asyncio.wait_for(process.stdout.read(), 30) == b""
        assert await asyncio.wait_for(process.wait(), 30) == 0
        assert process.returncode == 0
        return seen

    assert asyncio.run(scenario()) == [b"ALPHA\n", b"BETA\n"]


def test_communicate_sends_input_and_collects_both_streams() -> None:
    program = (
        "import sys\n"
        "sys.stdout.write(sys.stdin.read().upper())\n"
        "sys.stderr.write('warned')\n"
    )

    async def scenario() -> tuple[bytes, bytes, int | None]:
        process = await spawn.subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            program,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(process.communicate(b"quiet"), 30)
        return out, err, process.returncode

    assert asyncio.run(scenario()) == (b"QUIET", b"warned", 0)


def test_a_stream_that_was_not_piped_is_none_and_the_child_still_runs() -> None:
    async def scenario() -> tuple[object, object, object, int]:
        process = await spawn.subprocess_exec(
            *TRUE,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        code = await process.wait()
        return process.stdin, process.stdout, process.stderr, code

    assert asyncio.run(scenario()) == (None, None, None, 0)


def test_a_signal_ends_the_child_and_a_signal_after_its_exit_is_refused() -> None:
    async def scenario() -> list[int]:
        killed = await spawn.subprocess_exec(*SLEEP)
        killed.kill()
        terminated = await spawn.subprocess_exec(*SLEEP)
        terminated.terminate()
        codes = [
            await asyncio.wait_for(killed.wait(), 30),
            await asyncio.wait_for(terminated.wait(), 30),
        ]
        for process in (killed, terminated):
            with pytest.raises(ProcessLookupError):
                process.kill()
            with pytest.raises(ProcessLookupError):
                process.terminate()
        return codes

    assert asyncio.run(scenario()) == [-9, -15]


def test_a_line_longer_than_the_limit_is_refused_as_asyncio_refuses_it() -> None:
    program = "import sys\nsys.stdout.write('x' * 5000 + '\\n')\n"

    async def scenario() -> None:
        process = await spawn.subprocess_exec(
            sys.executable, "-I", "-c", program, stdout=subprocess.PIPE, limit=100
        )
        assert process.stdout is not None
        with pytest.raises(ValueError, match="limit"):
            await asyncio.wait_for(process.stdout.readline(), 30)
        await process.wait()

    asyncio.run(scenario())


def test_the_exit_is_waited_for_by_a_thread_that_ends_with_the_child() -> None:
    def waiters() -> int:
        return sum(1 for t in threading.enumerate() if t.name == "dromon-child-wait")

    async def scenario() -> tuple[int, int]:
        before = waiters()
        process = await spawn.subprocess_exec(*SLEEP)
        during = waiters()
        process.kill()
        await asyncio.wait_for(process.wait(), 30)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and waiters() > before:
            await asyncio.sleep(0.05)
        return during - before, waiters() - before

    assert asyncio.run(scenario()) == (1, 0)


def test_the_bounded_process_gives_what_the_modules_that_start_children_use() -> None:
    """Every member of ``ChildProcess`` is on both the bounded process and asyncio's
    own, so a module typed against the protocol takes either."""
    members: list[str] = [
        "pid",
        "returncode",
        "stdin",
        "stdout",
        "stderr",
        "wait",
        "communicate",
        "send_signal",
        "terminate",
        "kill",
    ]
    bounded: Any = spawn.BoundedProcess
    for name in members:
        assert hasattr(asyncio.subprocess.Process, name) or name in {
            "pid",
            "stdin",
            "stdout",
            "stderr",
        }
        assert hasattr(bounded, name) or name in {"pid", "stdin", "stdout", "stderr"}


def test_the_pipe_to_a_child_that_exited_is_closed_though_a_descendant_holds_it() -> (
    None
):
    """Found by the gate of 2026-11-18, as a ``ResourceWarning`` from the collection of
    a writer that a launch smoke check never closed. A launcher's harness holds the
    pipe after the launcher has gone, so the pipe does not close by itself, and the
    writer must be closed when the child exits."""
    program = "sleep 5 <&0 & exit 0"

    async def scenario() -> tuple[bool, int]:
        process = await spawn.subprocess_exec(
            "/bin/sh", "-c", program, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL
        )
        assert process.stdin is not None
        code = await asyncio.wait_for(process.wait(), 30)
        await asyncio.sleep(0)
        return process.stdin.transport.is_closing(), code

    assert asyncio.run(scenario()) == (True, 0)


# Audit's finding of 2026-11-18. The starts had shared the loop's default executor.


async def timed_to_thread() -> float:
    """How long an unrelated call through ``asyncio.to_thread`` takes."""
    began = time.monotonic()
    assert await asyncio.to_thread(lambda: 7) == 7
    return time.monotonic() - began


def test_the_default_executor_is_starved_by_blocking_calls_which_is_the_control() -> (
    None
):
    async def scenario() -> float:
        loop = asyncio.get_running_loop()
        held = [loop.run_in_executor(None, time.sleep, 0.8) for _ in range(40)]
        await asyncio.sleep(0.1)
        delay = await timed_to_thread()
        await asyncio.gather(*held)
        return delay

    assert asyncio.run(scenario()) >= 0.4


def test_forty_slow_starts_do_not_delay_an_unrelated_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocking_popen(monkeypatch, 0.8)

    async def scenario() -> tuple[float, list[int | None]]:
        starts = [
            asyncio.ensure_future(spawn.subprocess_exec(*TRUE)) for _ in range(40)
        ]
        await asyncio.sleep(0.1)
        delay = await timed_to_thread()
        processes = await asyncio.gather(*starts)
        return delay, [await process.wait() for process in processes]

    delay, codes = asyncio.run(scenario())
    assert delay < 0.3, "the unrelated call waited for the starts"
    assert codes == [0] * 40


def test_the_starts_run_on_threads_of_their_own() -> None:
    names: list[str] = []
    real = spawn._start_claimed  # pyright: ignore[reportPrivateUsage]

    def recording(
        claim: object, argv: list[str], options: dict[str, Any]
    ) -> subprocess.Popen[bytes] | None:
        names.append(threading.current_thread().name)
        return real(claim, argv, options)  # type: ignore[arg-type]

    async def scenario() -> None:
        process = await spawn.subprocess_exec(*TRUE)
        assert await process.wait() == 0

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(spawn, "_start_claimed", recording)
        asyncio.run(scenario())
    assert names and all(name.startswith("dromon-start") for name in names)
    assert spawn.START_WORKERS > 0


def test_starts_past_the_limit_are_refused_and_the_count_returns_to_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    blocking_popen(monkeypatch, 0.6)
    monkeypatch.setattr(spawn, "MAX_ASYNC_STARTS", 2)

    async def scenario() -> list[int]:
        first = asyncio.ensure_future(spawn.subprocess_exec(*TRUE))
        second = asyncio.ensure_future(spawn.subprocess_exec(*TRUE))
        await asyncio.sleep(0.1)
        with pytest.raises(spawn.StartStalled, match="already running or waiting"):
            await spawn.subprocess_exec(*TRUE)
        return [await (await first).wait(), await (await second).wait()]

    assert asyncio.run(scenario()) == [0, 0]
    assert spawn._async_starts[0] == 0  # pyright: ignore[reportPrivateUsage]


def test_a_start_that_fails_gives_its_place_back() -> None:
    async def scenario() -> None:
        with pytest.raises(OSError, match="No such file"):
            await spawn.subprocess_exec("/no/such/program")

    before = spawn._async_starts[0]  # pyright: ignore[reportPrivateUsage]
    asyncio.run(scenario())
    assert spawn._async_starts[0] == before  # pyright: ignore[reportPrivateUsage]


def test_a_caller_cancelled_as_the_loop_ends_does_not_leave_the_child_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The loop is gone when the child is made, so no callback of the loop can end it.
    The thread that made it does, because the claim says the caller is gone."""
    made = blocking_popen(monkeypatch, 0.6)

    async def scenario() -> None:
        task = asyncio.ensure_future(spawn.subprocess_exec(*SLEEP))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not made:
        time.sleep(0.05)
    assert made
    assert made[0].wait(timeout=10) is not None


def test_a_child_made_before_the_caller_is_cancelled_is_ended_by_the_claim() -> None:
    claim = spawn._Claim()  # pyright: ignore[reportPrivateUsage]
    popen = spawn.start(SLEEP)
    assert claim.offer(popen)
    claim.abandon()
    assert popen.wait(timeout=10) is not None
    late = spawn.start(SLEEP)
    assert not claim.offer(late), "a child offered after the cancellation is refused"
    assert late.wait(timeout=10) is not None


def test_a_child_the_caller_has_taken_is_not_ended_by_a_later_cancellation() -> None:
    claim = spawn._Claim()  # pyright: ignore[reportPrivateUsage]
    popen = spawn.start(SLEEP)
    try:
        assert claim.offer(popen)
        claim.take()
        claim.abandon()
        assert popen.poll() is None
    finally:
        popen.kill()
        popen.wait(timeout=10)
