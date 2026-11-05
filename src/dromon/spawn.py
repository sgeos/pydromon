"""The one place a child process is started, with its start bounded as well as its run.

**Round 15.** ``subprocess.run`` and ``subprocess.Popen`` bound a child only once it
exists. Before that, ``Popen`` waits for the child to reach ``exec`` in an unbounded
read of the pipe that reports a failed start, so a timeout given to ``run`` does not
cover it. **Facts, observed on macOS in 2026.** A full gate was ended by pytest's five
minute limit with its test thread in that read, in a land test on 2026-11-01, in a
staleness test and in a worktree test on 2026-11-05, and ``/bin/ps`` stalled the same
way on 2026-10-29. Each passed on a re-run. These stalls caused most of the flaky runs
in continuous integration.

**What is known.** The read ends only once every copy of the pipe's write end is closed,
and the child closes its copy when it runs its program. A command given a working
directory starts by fork on macOS, never by ``posix_spawn``. Python 3.15 on macOS has no
``pipe2``, so ``os.pipe`` marks the pipe close-on-exec in a second step, after the call
that made it, which releases the interpreter lock. **Hypotheses, neither confirmed.**
One, another thread's spawn that closes no inherited descriptors, as ``os.posix_spawn``
in ``client.start_daemon`` does, forked between those two steps, and its long-lived
child kept the write end. Two, the child stalled before its program ran, as in
``execve`` on a loaded runner. A traceback of every thread at the next hang would tell
them apart, since the first needs a second thread that spawns. **Not found.** The
cause. It does not reproduce in isolation.

**The bound.** ``START_SECONDS`` is the one place the start bound is named. Every
``subprocess.run`` and ``subprocess.Popen`` in ``src`` goes through ``run`` or
``start`` here, and ``tests/test_spawn.py`` fails when a module outside this one calls
either, apart from the entries of ``EXEMPT`` below, which name why. The start happens on
a thread of its own, and the caller waits for it at most ``START_SECONDS``. On overrun
the caller gets ``StartStalled`` at once, with a message that names the program and the
bound, and the thread keeps the started child's hand-over exact under a lock. Either
the caller takes the started child, or it gives up first and the thread then kills and
reaps the child itself and closes its pipes, so nothing the caller gave up on runs on.
``StartStalled`` is a ``TimeoutError``, which is an ``OSError``, so a caller that
already treated a command that could not start as a failure treats a stall the same
way, and one that did not now fails with a message where it hung.

**Limits.** At most ``MAX_STRANDED`` starts are left stuck at once. Past that no further
child is started, and ``StartStalled`` is raised at once, since a host that cannot start
processes is not helped by more of them. **Review of 151cbcd.** The count is of threads
still alive, so a slot frees itself the moment its start returns or fails, and the
scheduler tells the operator in a note, which holds no line, when the count reaches the
bound. A start that never returns holds its slot until the daemon restarts, which the
note says. A read that a stop or a hold depends on, the process table, is started with
``essential``, which the bound does not refuse, so a daemon with stuck starts can still
find and stop a process. It is refused only past ``MAX_STRANDED_ESSENTIAL`` stuck
starts, a ceiling that keeps the threads bounded. A stuck start keeps one blocked
daemon thread, which never holds the interpreter open, and its pipe until the start
ends or the process exits. A caller that closed a descriptor it passed to a start it
was told stalled would leave the late start to fail with ``EBADF``, or in the worst
case to pass a descriptor that another thread had reused, so a caller hands such
descriptors over with ``close_after``, and spawn closes them once the start has ended.
Children are
started from a thread other than the caller's. That matters for a parent death signal
on Linux, which belongs to the thread that forked, so ``launcher.start_harness`` is
exempt.

**Exempt, and the path the first hypothesis names.** ``client.start_daemon`` starts the
daemon with ``os.posix_spawn``, which has no pipe for an exec failure and so nothing to
bound. It is also the call the first hypothesis names as the possible cause of the
stall, and it stays outside this module, so that candidate is neither bounded nor
excluded. A traceback of every thread at the next hang is what would settle it.

**Not covered.** ``asyncio.create_subprocess_exec`` runs ``Popen`` on the event loop's
own thread and cannot be moved to another, so its callers in the engine drivers are
still unbounded, and a stall there stops the daemon's loop. The follow-up is an
asynchronous process adapter built on ``start``, which is a larger change than this
one.

Porting note. A start is one external effect with a deadline, which maps onto
``std::process::Command`` spawned from a thread with a channel in the Rust port, and
onto an external effect of a bounded stream processor in Keleusma. A port that spawns
with ``posix_spawn`` and a working-directory file action, or reports exec failure
another way, has no such read to bound, and the module reduces to its signatures.
"""

import contextlib
import os
import signal
import subprocess
import threading
from collections.abc import Mapping, Sequence
from typing import IO, Any, Final, Literal, cast, overload

START_SECONDS: Final = 30.0
"""How long a child may take to start, before its program runs. A start takes
milliseconds, so this is far above any start on a loaded host, and far below the five
minutes after which pytest's faulthandler ends the whole test run."""

MAX_STRANDED: Final = 4
"""The most starts left stuck at once. At the bound no further child is started."""

MAX_STRANDED_ESSENTIAL: Final = 32
"""The most stuck starts at which a start marked ``essential`` is still made. It is
well above ``MAX_STRANDED`` and exists only to bound the threads a failing host can
leave."""

REAP_SECONDS: Final = 10.0
"""How long ``_reap`` waits for a killed child to be reaped. A child that survives
``SIGKILL``, as in an uninterruptible wait, is left to the interpreter's reaper, so the
thread and its slot are not held for ever."""

DRAIN_SECONDS: Final = 1.0
"""How long ``run`` reads what a killed command left in its pipes. A descendant that
holds a pipe open would otherwise hold the read."""

TAIL_LIMIT: Final = 65536
"""The most of a timed-out command's output kept for the caller, from its end, in bytes
or characters."""

EXEMPT: Final = {
    "launcher.py": "a parent death signal on Linux belongs to the thread that forked",
    "client.py": (
        "os.posix_spawn has no pipe to wait on for an exec failure, and it is the call"
        " the first hypothesis names"
    ),
}
"""The modules in ``src`` that start a child without this module, and why. A test holds
this to the modules that really do."""

type _Stream = int | IO[Any] | None
type _Argv = Sequence[str | os.PathLike[str]]


class StartStalled(TimeoutError):
    """A child did not start within the start bound, or too many earlier starts are
    still stuck. It is an ``OSError``, so a caller that treats a command that cannot be
    started as a failure treats this as one."""


_stranded: list[threading.Thread] = []
_stranded_lock = threading.Lock()


def stranded_count() -> int:
    """How many starts are stuck now. Ended threads are forgotten here."""
    with _stranded_lock:
        _stranded[:] = [thread for thread in _stranded if thread.is_alive()]
        return len(_stranded)


def stuck_text(count: int) -> str:
    """The note to the operator when ``count`` starts are stuck at the bound. It names
    the effect and the remedy, and no program or argument."""
    return (
        f"{count} child process starts are stuck, the most dromond allows. It starts no"
        " further child, apart from the reads of the process table that a stop or a"
        " hold needs, so git status, runs and engine helpers are refused with a"
        " message. A start that never returns frees its slot only when the daemon"
        " restarts, and one that returns frees it at once. This is the intermittent"
        " stall in dromon.spawn. Restart the daemon when it suits you, and keep a"
        " traceback of every thread if you can."
    )


class _Handoff:
    """The place a start's outcome goes, for the thread that made it and the caller that
    waits. Under ``lock`` the outcome is set once, and ``abandoned`` is set once, and
    never both."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.outcome: subprocess.Popen[Any] | BaseException | None = None
        self.abandoned = False


def _kill(process: subprocess.Popen[Any]) -> None:
    """Send ``SIGKILL`` to a child that has not been reaped, and to its process group
    when the child leads one of its own, as it does with ``start_new_session`` or
    ``process_group``. **Review of 151cbcd.** The daemon's own group is never signalled.
    The pid cannot have been reused, since the child is not reaped."""
    with contextlib.suppress(OSError):
        group = os.getpgid(process.pid)
        if group == process.pid and group != os.getpgrp():
            os.killpg(group, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()


def _reap(process: subprocess.Popen[Any]) -> None:
    """Kill and reap a child whose caller gave up on its start, and close its pipes,
    without reading them, since a descendant could hold them open. The wait is bounded
    by ``REAP_SECONDS``."""
    _kill(process)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(REAP_SECONDS)
    for stream in (process.stdin, process.stdout, process.stderr):
        if stream is not None:
            with contextlib.suppress(OSError):
                stream.close()


def _close_all(descriptors: Sequence[int]) -> None:
    for descriptor in descriptors:
        with contextlib.suppress(OSError):
            os.close(descriptor)


def _begin(
    argv: _Argv,
    options: dict[str, Any],
    start_seconds: float | None,
    close_after: Sequence[int],
    essential: bool,
) -> subprocess.Popen[Any]:
    bound = START_SECONDS if start_seconds is None else start_seconds
    name = os.fspath(argv[0]) if argv else "(no program)"
    if not bound > 0:
        _close_all(close_after)
        raise ValueError(f"the start bound must be above 0, not {bound:g}")
    limit = MAX_STRANDED_ESSENTIAL if essential else MAX_STRANDED
    if stranded_count() >= limit:
        _close_all(close_after)
        raise StartStalled(
            f"{name} was not started: {limit} earlier starts are still stuck, and a"
            " stuck start ends only when its thread does or the daemon restarts"
        )
    handoff = _Handoff()

    def target() -> None:
        outcome: subprocess.Popen[Any] | BaseException
        try:
            outcome = subprocess.Popen(argv, **options)  # noqa: S603
        except BaseException as error:  # noqa: BLE001
            # Carried to the caller, which decides what it means.
            outcome = error
        # The descriptors are closed here, once the start has ended, not by a caller
        # that was told the start stalled, who would close them under a late start.
        _close_all(close_after)
        with handoff.lock:
            handoff.outcome = outcome
            abandoned = handoff.abandoned
        handoff.done.set()
        if abandoned and isinstance(outcome, subprocess.Popen):
            _reap(outcome)

    thread = threading.Thread(target=target, name="dromon-spawn", daemon=True)
    thread.start()
    handoff.done.wait(bound)
    with handoff.lock:
        outcome = handoff.outcome
        if outcome is None:
            handoff.abandoned = True
    if outcome is None:
        with _stranded_lock:
            _stranded.append(thread)
        raise StartStalled(
            f"{name} did not start within {bound:g}s, as its start stalled before the"
            " program ran, a known intermittent stall in subprocess on macOS that"
            " dromon.spawn describes. It is killed if it starts later."
        )
    if isinstance(outcome, BaseException):
        raise outcome
    return outcome


def _options(
    stdin: _Stream,
    stdout: _Stream,
    stderr: _Stream,
    cwd: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
    text: bool,
    encoding: str | None,
    errors: str | None,
    start_new_session: bool,
    process_group: int | None,
    pass_fds: Sequence[int],
) -> dict[str, Any]:
    return {
        "stdin": stdin,
        "stdout": stdout,
        "stderr": stderr,
        "cwd": cwd,
        "env": None if env is None else dict(env),
        "text": text or None,
        "encoding": encoding,
        "errors": errors,
        "start_new_session": start_new_session,
        "process_group": process_group,
        "pass_fds": tuple(pass_fds),
    }


def _tail(value: object, text: bool) -> str | bytes | None:
    """The last ``TAIL_LIMIT`` of a command's output, as text for a text command and
    as bytes otherwise, or ``None`` when it wrote none."""
    if value is None:
        return None
    if text and isinstance(value, bytes):
        value = value.decode(errors="replace")
    elif not text and isinstance(value, str):
        value = value.encode(errors="replace")
    return cast("str | bytes", value)[-TAIL_LIMIT:]


def _drain(
    process: subprocess.Popen[Any], expired: subprocess.TimeoutExpired, text: bool
) -> tuple[str | bytes | None, str | bytes | None]:
    """What a killed command wrote, bounded to its tail. It reads for at most
    ``DRAIN_SECONDS`` and falls back to what the timeout had collected, since a
    descendant that holds a pipe open would hold the read."""
    out, err = expired.output, expired.stderr
    try:
        out, err = process.communicate(timeout=DRAIN_SECONDS)
    except subprocess.TimeoutExpired as late:
        out, err = late.output or out, late.stderr or err
    except (OSError, ValueError):
        pass
    return _tail(out, text), _tail(err, text)


@overload
def start(
    argv: _Argv,
    *,
    text: Literal[True],
    stdin: _Stream = None,
    stdout: _Stream = None,
    stderr: _Stream = None,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    encoding: str | None = None,
    errors: str | None = None,
    start_new_session: bool = False,
    process_group: int | None = None,
    pass_fds: Sequence[int] = (),
    close_after: Sequence[int] = (),
    start_seconds: float | None = None,
    essential: bool = False,
) -> subprocess.Popen[str]: ...


@overload
def start(
    argv: _Argv,
    *,
    text: Literal[False] = False,
    stdin: _Stream = None,
    stdout: _Stream = None,
    stderr: _Stream = None,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    encoding: None = None,
    errors: None = None,
    start_new_session: bool = False,
    process_group: int | None = None,
    pass_fds: Sequence[int] = (),
    close_after: Sequence[int] = (),
    start_seconds: float | None = None,
    essential: bool = False,
) -> subprocess.Popen[bytes]: ...


def start(
    argv: _Argv,
    *,
    text: bool = False,
    stdin: _Stream = None,
    stdout: _Stream = None,
    stderr: _Stream = None,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    encoding: str | None = None,
    errors: str | None = None,
    start_new_session: bool = False,
    process_group: int | None = None,
    pass_fds: Sequence[int] = (),
    close_after: Sequence[int] = (),
    start_seconds: float | None = None,
    essential: bool = False,
) -> subprocess.Popen[Any]:
    """Start ``argv`` as ``subprocess.Popen`` does, with the start bounded.
    ``essential`` marks a read that a stop or a hold depends on, which the bound on
    stuck starts does not refuse, as the module says.
    ``close_after`` hands descriptors over that the child is given through ``stdin``,
    ``stdout``, ``stderr`` or ``pass_fds``. They are closed once the start has ended,
    or at once when no start is made, and the caller must not close them. Raises
    ``StartStalled`` when the child is not started within ``start_seconds``, which is
    ``START_SECONDS`` when ``None``, or when ``MAX_STRANDED`` starts are stuck, an
    ``OSError`` when the program cannot be run, and ``ValueError`` for a bound not
    above 0. The caller owns the child from the return, and must wait for it."""
    return _begin(
        argv,
        _options(
            stdin,
            stdout,
            stderr,
            cwd,
            env,
            text,
            encoding,
            errors,
            start_new_session,
            process_group,
            pass_fds,
        ),
        start_seconds,
        close_after,
        essential,
    )


@overload
def run(
    argv: _Argv,
    *,
    timeout: float,
    text: Literal[True],
    input: str | None = None,
    capture_output: bool = False,
    check: bool = False,
    stdin: _Stream = None,
    stdout: _Stream = None,
    stderr: _Stream = None,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    encoding: str | None = None,
    errors: str | None = None,
    start_new_session: bool = False,
    start_seconds: float | None = None,
    essential: bool = False,
) -> subprocess.CompletedProcess[str]: ...


@overload
def run(
    argv: _Argv,
    *,
    timeout: float,
    text: Literal[False] = False,
    input: bytes | None = None,
    capture_output: bool = False,
    check: bool = False,
    stdin: _Stream = None,
    stdout: _Stream = None,
    stderr: _Stream = None,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    encoding: None = None,
    errors: None = None,
    start_new_session: bool = False,
    start_seconds: float | None = None,
    essential: bool = False,
) -> subprocess.CompletedProcess[bytes]: ...


def run(
    argv: _Argv,
    *,
    timeout: float,
    text: bool = False,
    input: str | bytes | None = None,
    capture_output: bool = False,
    check: bool = False,
    stdin: _Stream = None,
    stdout: _Stream = None,
    stderr: _Stream = None,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    encoding: str | None = None,
    errors: str | None = None,
    start_new_session: bool = False,
    start_seconds: float | None = None,
    essential: bool = False,
) -> subprocess.CompletedProcess[Any]:
    """Run ``argv`` as ``subprocess.run`` does, with the start bounded by
    ``start_seconds`` and the run by ``timeout``. Unlike ``subprocess.run`` the timeout
    is required, so no command here waits for ever. A run past ``timeout`` kills the
    child and its group, and raises ``subprocess.TimeoutExpired`` with the last
    ``TAIL_LIMIT`` of what it wrote, a start that stalls raises
    ``StartStalled``, and ``check`` raises ``subprocess.CalledProcessError``. Raises
    ``ValueError`` when ``input`` is given with ``stdin``, or ``capture_output`` with
    ``stdout`` or ``stderr``."""
    if input is not None and stdin is not None:
        raise ValueError("input and stdin may not both be given")
    if capture_output and (stdout is not None or stderr is not None):
        raise ValueError("capture_output may not be used with stdout or stderr")
    pipes = subprocess.PIPE if capture_output else None
    process = start(
        argv,
        text=cast("Literal[True]", text),
        stdin=subprocess.PIPE if input is not None else stdin,
        stdout=pipes or stdout,
        stderr=pipes or stderr,
        cwd=cwd,
        env=env,
        encoding=encoding,
        errors=errors,
        start_new_session=start_new_session,
        start_seconds=start_seconds,
        essential=essential,
    )
    shown = [os.fspath(part) for part in argv]
    with process:
        try:
            out, err = cast(
                "tuple[Any, Any]", process.communicate(cast("Any", input), timeout)
            )
        except subprocess.TimeoutExpired as expired:
            _kill(process)
            kept_out, kept_err = _drain(process, expired, text)
            process.wait()
            raise subprocess.TimeoutExpired(
                shown, timeout, output=kept_out, stderr=kept_err
            ) from None
        except BaseException:
            _kill(process)
            raise
        code = process.poll()
    if code is None:
        raise RuntimeError("a command that was waited for has no status")
    if check and code != 0:
        raise subprocess.CalledProcessError(code, shown, out, err)
    return subprocess.CompletedProcess(shown, code, out, err)
