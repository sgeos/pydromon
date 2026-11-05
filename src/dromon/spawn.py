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
processes is not helped by more of them. A stuck start keeps one blocked daemon thread,
which never holds the interpreter open, and its pipe until the start ends or the
process exits. A caller that closed a descriptor it passed to a start it was told
stalled would leave the late start to fail with ``EBADF``, or in the worst case to
pass a descriptor that another thread had reused, so a caller hands such descriptors
over with ``close_after``, and spawn closes them once the start has ended. Children are
started from a thread other than the caller's. That matters for a parent death signal
on Linux, which belongs to the thread that forked, so ``launcher.start_harness`` is
exempt.

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

EXEMPT: Final = {
    "launcher.py": "a parent death signal on Linux belongs to the thread that forked",
    "client.py": "os.posix_spawn has no pipe to wait on for an exec failure",
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


class _Handoff:
    """The place a start's outcome goes, for the thread that made it and the caller that
    waits. Under ``lock`` the outcome is set once, and ``abandoned`` is set once, and
    never both."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.outcome: subprocess.Popen[Any] | BaseException | None = None
        self.abandoned = False


def _reap(process: subprocess.Popen[Any]) -> None:
    """Kill and reap a child whose caller gave up on its start, and close its pipes,
    without reading them, since a descendant could hold them open."""
    with contextlib.suppress(OSError):
        process.kill()
    process.wait()
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
) -> subprocess.Popen[Any]:
    bound = START_SECONDS if start_seconds is None else start_seconds
    name = os.fspath(argv[0]) if argv else "(no program)"
    if not bound > 0:
        _close_all(close_after)
        raise ValueError(f"the start bound must be above 0, not {bound:g}")
    if stranded_count() >= MAX_STRANDED:
        _close_all(close_after)
        raise StartStalled(
            f"{name} was not started: {MAX_STRANDED} earlier starts are still stuck"
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
) -> subprocess.Popen[Any]:
    """Start ``argv`` as ``subprocess.Popen`` does, with the start bounded.
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
) -> subprocess.CompletedProcess[Any]:
    """Run ``argv`` as ``subprocess.run`` does, with the start bounded by
    ``start_seconds`` and the run by ``timeout``. Unlike ``subprocess.run`` the timeout
    is required, so no command here waits for ever. A run past ``timeout`` kills the
    child and raises ``subprocess.TimeoutExpired``, a start that stalls raises
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
    )
    with process:
        try:
            out, err = cast(
                "tuple[Any, Any]", process.communicate(cast("Any", input), timeout)
            )
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        except BaseException:
            process.kill()
            raise
        code = process.poll()
    if code is None:
        raise RuntimeError("a command that was waited for has no status")
    shown = [os.fspath(part) for part in argv]
    if check and code != 0:
        raise subprocess.CalledProcessError(code, shown, out, err)
    return subprocess.CompletedProcess(shown, code, out, err)
