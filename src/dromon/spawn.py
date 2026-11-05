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

**Reading of 2026-11-05, after a stall on a Linux runner.** In pull request 142 a start
stalled for 30 seconds on a GitHub Linux runner, so the stall is not peculiar to one
macOS host or to its load. **Facts, read from this repository.** Every start in the
daemon goes through this module, apart from the asyncio starts of the engine drivers and
the relay, which run ``Popen`` on the event loop's thread, and ``client.start_daemon``,
which calls ``os.posix_spawn``. None of them sets ``preexec_fn``, ``close_fds`` or
``restore_signals``, so ``Popen`` closes every descriptor but those it was told to pass.
``start_new_session`` is set by four starts, ``process_group`` by one, ``pass_fds`` by
one, the run supervisor's, and ``cwd`` by several. The only ``preexec_fn`` in ``src`` is
the launcher's, which runs in the launcher's own single-threaded process and so cannot
hold up a start in the daemon. Starts run concurrently from the process worker, the code
tree's worker, the engine drivers on the event loop and the scheduler's tick, and
``spawn`` now counts how many were in flight together.

**Facts, read from ``subprocess.py`` of Python 3.15.1.** A start with a working
directory, a bare program name such as ``git``, a descriptor to pass or a new session
takes the ``_fork_exec`` path, which creates a pipe with ``os.pipe``, forks, and reads
the pipe until every copy of its write end is closed. Only a start with an absolute
program path and none of those options can take ``posix_spawn``, which has no such pipe.
The macOS interpreter's executable imports ``opendir``, ``readdir`` and
``proc_pidinfo``, so its child may call them before the program runs. **Not read in this
review.** The C code of ``_posixsubprocess``. From its documentation and memory, the
child sets the session or group, duplicates the standard descriptors, changes directory,
closes the other descriptors and restores signals before the program runs, and Python
3.10 and later use ``vfork`` on Linux where permitted, which suspends the starting
thread until the child executes. Linux makes the pipe close-on-exec in one call, so the
macOS window between two steps does not apply there.

**Inferences.** A copy of the pipe's write end is held by every child forked while the
pipe exists, until that child executes, so a child that is slow or stuck before its
program runs holds up every other start in the process, not only its own. **Hypotheses,
none confirmed.** Three, beside the two above. A child forked from a process with
several threads waits on a lock that another thread held at the fork, in code the child
must run before it executes. A start's thread never reaches ``Popen`` because another
thread holds the interpreter lock in a call that does not release it, so there is no
child at all. Many children are forked at once and the program's executable is slow to
load.

The stall report below tells these apart. A thread at ``os.read`` in ``_execute_child``
with a child of the daemon that has not executed means the child is stuck before its
program. A thread at ``os.read`` with no such child means another copy of the pipe is
held, and the overlap says whether another start was in flight. A thread in
``_fork_exec`` means the fork has not returned. A thread still in ``target`` means no
fork was made.

**The dump of 2026-11-05, read in full.** The faulthandler dump of that run lists 100
threads and then ``...``, because ``faulthandler`` shows at most 100. **Facts.** Ninety
nine of them are idle ``dromon-processes`` workers of earlier tests' schedulers, which
tests never stopped, and one is the ``asyncio_0`` thread of the stale-brief test, so the
main thread and every older thread are not in the dump. The ``asyncio_0`` thread is at
``threading.py`` line 370 and 665, which are the ``return`` statements after the
blocking acquire, in the layout that matches Python 3.15.1's, so its timed wait had
already returned and it was not sleeping in it. No ``dromon-spawn`` thread is in the
dump, which would be listed before it. Every path of ``target`` sets the event.
**Inference.** The thread that wakes and does not run waits for the interpreter lock,
which another thread holds. **Hypothesis, not confirmed.** That holder is a thread in
``fork_exec``, which as the author recalls holds the lock across ``vfork`` or ``fork``,
so a child that stalls before its program freezes every Python thread, the waiter, the
report and the bound with them. No Python timer can report that. **Two changes follow.**
``watch_starts`` sets ``faulthandler``'s timer, which runs on a thread of its own
without the lock, to dump every thread ``WATCHDOG_MARGIN_SECONDS`` past a start's bound,
and the daemon turns it on. And the tests stop every scheduler's workers, so a dump is
not filled with idle threads and every start copies fewer of them. The timer is one per
process and replaces pytest's, so the tests leave it off, and it is aimed at the oldest
start in flight that has not been dumped, so a second stuck start is shown by the same
dump only if it is stuck by then.

**The fork lock, 2026-11-05.** Main failed three pushes running with the stall, on Linux
and once on macOS, after two changes that start children from more threads.
**Inference.** More threads forking together made the stall frequent. So every fork in
the process is now made one at a time. ``install_fork_lock``, run when this module is
imported, puts one lock, ``forking``, around ``subprocess._fork_exec``,
``os.posix_spawn`` and ``os.posix_spawnp``, which every start reaches, whoever makes it,
asyncio's and the engine drivers' included. Nothing else is held across the fork, and a
start that waits ``FORK_LOCK_SECONDS`` for the lock is refused with ``StartStalled`` and
holds no thread. The lock is held while the starting thread is in the call, which with
``vfork`` is until the child has executed, and not while it waits on the exec pipe, so
the bound still covers that wait. **Not known.** Whether this ends the stall. It removes
forks that overlap, which the inference names, and does not touch a single child that
stalls alone, which the watchdog's dump would show. **Porting hazard.** It replaces
names in the standard library, which only Python allows, and a Rust port holds a mutex
around ``Command::spawn``. The watchdog times each fork as well as each start.

**A stall observed with the fork lock in place.** In a sandbox gate on macOS a real
``git`` start stalled at the read of the exec pipe, with one start in flight and two
threads alive in the whole process, and ``ps`` could not be started to show the child.
**Facts.** No other thread of the process was forking, so a child of another start
holding a copy of the pipe cannot explain it, and the fork had returned, so the thread
was not frozen in it. **Inference.** The child was stuck before its program ran, or the
operating system was slow to execute it, in which case the fork lock does not help. The
report now reads children from ``libproc`` on macOS, which needs no process, and says
whether a child is still being created, state ``I``, or has executed.

**The stall report.** A start that stalls raises ``StartStalled`` with a report, and
writes the same to the daemon's standard error. It gives where the start's thread is in
Python, how many starts were in flight now and at most during this one, how many threads
are alive, and each child of the daemon, up to eight, with its process state, its
program name, whether it has executed, and what it waits on. The children come from
``/proc`` on Linux and from ``libproc`` on macOS, which start no process, and from
``ps`` otherwise, on a thread of its own that is left behind after three seconds. It
names no argument, shows only printable ASCII, and is at most 1200 characters. The stuck
child is not killed by searching for it, since a search by parent and program could end
an unrelated child that is about to execute.

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
starts, a ceiling that keeps the threads bounded. **Past the ceiling, review of
7744806.** A stop by pid is refused as well, because it cannot read the process table,
and a restart of the daemon is the only recovery. A stuck start keeps one blocked
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
import faulthandler
import os
import resource
import signal
import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
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

DIAGNOSE_SECONDS: Final = 3.0
"""How long the report of a stalled start waits for the process table. The table is read
on a thread of its own, since the start that stalled may have left the process unable to
start another, and a read that does not end is left behind."""

MAX_REPORT_CHARS: Final = 1200
"""The most of a stall report put in the error and the daemon's log, in characters."""

MAX_CHILDREN_REPORTED: Final = 8
"""The most children of the daemon the report describes."""

MAX_PROC_ENTRIES: Final = 4096
"""The most entries of ``/proc`` the report reads, so a host with a very long process
list does not lengthen a stall."""

FORK_LOCK_SECONDS: Final = 30.0
"""How long a start waits for the lock that serializes forks. A start that waits this
long is refused with ``StartStalled``, so that one stuck fork does not hold every other
thread of the daemon, the event loop's among them."""

WATCHDOG_MARGIN_SECONDS: Final = 5.0
"""How long past a start's own bound the watchdog waits before it dumps the traceback
of every thread. It is after the bound, so a start that stalls is already reported by
``StartStalled`` when the dump is written, and the dump is of the state the report
describes."""

_REAL_POPEN: Final = subprocess.Popen
"""``subprocess.Popen`` as it was when this module was loaded. The report of a stalled
start reads the process table with it, so that a test which replaces ``Popen`` to stall
a start does not stall the report as well."""

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
    never both. ``overlap`` is the most starts that were in flight at once while this
    one was, itself included, and is read and written under ``_active_lock``."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.outcome: subprocess.Popen[Any] | BaseException | None = None
        self.abandoned = False
        self.overlap = 1
        self.began = time.monotonic()
        self.deadline = 0.0
        self.dumped = False


_active: set[_Handoff] = set()
_watched: set[_Handoff] = set()
"""The starts the watchdog times. Every start in ``_active`` is in it, and so is each
fork, from any thread, that ``_serialized`` makes."""
_active_lock = threading.Lock()


_watch_file: IO[str] | None = None
_armed: _Handoff | None = None


def watch_starts(file: IO[str] | None) -> bool:
    """Write the traceback of every thread to ``file`` when a start is still stuck
    ``WATCHDOG_MARGIN_SECONDS`` past its bound, or stop doing so with ``None``. The
    daemon turns it on, and tests and other callers leave it off. **Round 15.** It is
    ``faulthandler``'s timer, which runs on a thread of its own and does not need the
    interpreter lock, so it fires when a start holds that lock and every Python
    thread, the waiter and the stall report with them, is frozen. Nothing in Python
    can report that, and a hang seen on 2026-11-05 had exactly that shape. A dump from
    it shows the thread that is stuck, and the main thread. ``faulthandler`` keeps one
    timer for the process, so this replaces and is replaced by pytest's own, which is
    why the tests leave it off. Returns whether it is now on, and ``False`` where
    ``file`` has no descriptor."""
    global _watch_file  # noqa: PLW0603
    with _active_lock:
        if file is not None:
            try:
                file.fileno()
            except (OSError, ValueError):
                file = None
        _watch_file = file
        _rearm_locked()
        return _watch_file is not None


def _rearm_locked() -> None:
    """Point the timer at the oldest start in flight that has not been dumped, or
    cancel it. A start already past its deadline is taken to have been dumped, since
    its timer was set for that, and is not dumped again. Held under ``_active_lock``.
    Bounded by the number of starts in flight."""
    global _armed  # noqa: PLW0603
    if _watch_file is None:
        if _armed is not None:
            faulthandler.cancel_dump_traceback_later()
        _armed = None
        return
    now = time.monotonic()
    for each in _watched:
        if each.deadline and each.deadline <= now:
            each.dumped = True
    waiting = [each for each in _watched if each.deadline and not each.dumped]
    if not waiting:
        if _armed is not None:
            faulthandler.cancel_dump_traceback_later()
        _armed = None
        return
    first = min(waiting, key=lambda each: each.deadline)
    if first is _armed:
        return
    faulthandler.dump_traceback_later(
        max(first.deadline - now, 0.001), repeat=False, file=_watch_file, exit=False
    )
    _armed = first


def _enter(handoff: _Handoff) -> None:
    """Note that ``handoff``'s start has begun, and raise the overlap of every start in
    flight, itself included, to the number in flight now."""
    with _active_lock:
        _active.add(handoff)
        _watched.add(handoff)
        now = len(_active)
        for other in _active:
            other.overlap = max(other.overlap, now)
        _rearm_locked()


def _leave(handoff: _Handoff) -> None:
    global _armed  # noqa: PLW0603
    with _active_lock:
        _active.discard(handoff)
        _unwatch_locked(handoff)


def _unwatch_locked(handoff: _Handoff) -> None:
    global _armed  # noqa: PLW0603
    _watched.discard(handoff)
    if _armed is handoff:
        _armed = None
        if _watch_file is not None:
            faulthandler.cancel_dump_traceback_later()
    _rearm_locked()


def _watch(handoff: _Handoff) -> None:
    """Time ``handoff`` for the watchdog without counting it among the starts."""
    with _active_lock:
        _watched.add(handoff)
        _rearm_locked()


def _unwatch(handoff: _Handoff) -> None:
    with _active_lock:
        _unwatch_locked(handoff)


_fork_lock = threading.Lock()
_FORK_INSTALLED: list[bool] = [False]


@contextlib.contextmanager
def forking() -> Generator[None]:
    """Hold the one lock every child start in this process takes, for the call that
    forks. **Round 15.** Two threads that fork at the same moment each hand the other
    a copy of every descriptor open at the time, and a lock or an allocator held by a
    thread that is not the forking one is held for ever in the child. A stall seen on
    Linux and on macOS only appeared once several threads started children together, so
    the starts are made one at a time. Nothing else is held across the fork, and a
    start that waits ``FORK_LOCK_SECONDS`` for the lock raises ``StartStalled`` instead
    of waiting for a start that is stuck. It is also timed by the watchdog."""
    watched = _Handoff()
    if not _fork_lock.acquire(timeout=FORK_LOCK_SECONDS):
        raise StartStalled(
            f"a child was not started: another start held the fork lock for"
            f" {FORK_LOCK_SECONDS:g}s"
        )
    # Timed from here, since a wait for the lock is not a stall in the fork.
    watched.began = time.monotonic()
    watched.deadline = watched.began + FORK_LOCK_SECONDS + WATCHDOG_MARGIN_SECONDS
    _watch(watched)
    try:
        yield
    finally:
        _unwatch(watched)
        _fork_lock.release()


def _serialized(call: Callable[..., Any]) -> Callable[..., Any]:
    def serialized(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        with forking():
            return call(*args, **kwargs)

    setattr(serialized, "__wrapped__", call)  # noqa: B010
    return serialized


def install_fork_lock() -> bool:
    """Put ``forking`` around ``subprocess``'s call that forks, ``_fork_exec``, and
    around ``os.posix_spawn`` and ``os.posix_spawnp``, which ``subprocess`` and
    ``asyncio`` and the engine drivers reach, so that no two threads start a child at
    once, whoever starts it. Idempotent. Returns whether the lock is in place. It is
    done at import of this module, which the daemon and ``subprocess`` users here
    load. **Porting hazard.** It replaces names in the standard library, which only
    Python allows, and relies on ``subprocess._fork_exec``, which is private and
    present in Python 3.12 to 3.15. Where it is absent the lock covers ``spawn.start``
    alone, which is what ``test_every_fork_is_serialized`` checks for. A Rust port
    holds one mutex around ``Command::spawn``."""
    if _FORK_INSTALLED[0]:
        return True
    installed = False
    inner = getattr(subprocess, "_fork_exec", None)
    if callable(inner):
        setattr(subprocess, "_fork_exec", _serialized(inner))  # noqa: B010
        installed = True
    for name in ("posix_spawn", "posix_spawnp"):
        spawner = getattr(os, name, None)
        if callable(spawner):
            setattr(os, name, _serialized(spawner))
    _FORK_INSTALLED[0] = installed
    return installed


def disable_vfork(platform: str = sys.platform) -> bool:
    """On Linux, stop ``subprocess`` from starting children with ``vfork`` or
    ``posix_spawn``, so that every start forks. **Round 15, an experiment.** A CI run on
    Linux stalled a start of ``git``, and reading ``/proc`` for the report then blocked
    for the rest of five minutes, which fits a child that is in the middle of ``vfork``,
    where the parent thread is suspended and the child shares its memory until it runs
    its program. CPython on Linux uses ``vfork`` since 3.10 through
    ``subprocess._USE_VFORK``, and ``glibc``'s ``posix_spawn``, which Python 3.15 uses
    when it can, makes the same kind of child, so both are turned off. If Linux CI
    then stops stalling, ``vfork`` is the cause and this is the fix. If it does not, it
    is not, and it should be taken out, since a fork copies the page tables of the
    whole daemon and is slower. **Not known.** Whether it helps. It is not applied
    elsewhere. ``os.posix_spawn`` calls made directly by this repository are not
    changed. Returns whether it changed anything. **Porting hazard.** It sets private
    names in the standard library, which only Python allows."""
    if not platform.startswith("linux"):
        return False
    changed = False
    for name in ("_USE_VFORK", "_USE_POSIX_SPAWN"):
        if hasattr(subprocess, name):
            setattr(subprocess, name, False)
            changed = True
    return changed


@dataclass(frozen=True)
class _Child:
    """A child of this process, as the process table shows it. ``execed`` is whether
    it runs a program other than this process's own, which a child forked and not yet
    executed does not, and ``None`` where that cannot be told."""

    pid: int
    state: str
    program: str
    execed: bool | None
    waiting: str | None


def _clean(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, with anything but printable ASCII shown as
    a question mark, since process names are not trusted."""
    return "".join(c if " " <= c <= "~" else "?" for c in text[:limit])


def _where(thread: threading.Thread) -> str:
    """Where the start's thread is now in Python, the last four frames, innermost
    first. A thread in ``os.read`` inside ``subprocess`` waits for the child to execute
    its program, one in ``_fork_exec`` has not been given the child's pid, and one in
    ``target`` has not reached ``Popen``, as when the interpreter lock is held
    elsewhere."""
    if not thread.is_alive():
        return "has ended"
    frame = sys._current_frames().get(thread.ident or 0)  # pyright: ignore[reportPrivateUsage]
    if frame is None:
        return "has no frame"
    steps = traceback.extract_stack(frame, limit=4)
    return " <- ".join(
        f"{Path(step.filename).name}:{step.lineno} {step.name}"
        for step in reversed(steps)
    )


def _proc_children(
    root: Path,
    parent: int,
    at: list[str] | None = None,
    found: list[_Child] | None = None,
) -> list[_Child]:
    """The children of ``parent`` read from a Linux ``/proc`` at ``root``, without
    starting a process, which is what a stalled start may no longer be able to do. A
    child's program is its ``comm``, and it has executed when its ``exe`` differs from
    this process's own. Reads at most ``MAX_PROC_ENTRIES`` entries. Only ``stat``,
    ``exe`` and ``wchan`` are read, never ``cmdline``, ``environ`` or ``maps``, which
    need the memory of a process that may be in the middle of ``vfork``. **Round 15.**
    A CI run on Linux hung five minutes inside the read of one of these files, so
    ``at`` is set to the path about to be read and a child is added to ``found`` as
    soon as its ``stat`` is read, for ``_proc_children_bounded`` to report when the
    read does not return."""
    seen = [] if found is None else found
    here = [""] if at is None else at
    here[0] = str(root / "self" / "exe")
    own = _read_link(root / "self" / "exe")
    try:
        names = os.listdir(root)
    except OSError:
        return seen
    for name in names[:MAX_PROC_ENTRIES]:
        if not name.isdigit():
            continue
        try:
            here[0] = str(root / name / "stat")
            stat = (root / name / "stat").read_text(errors="replace")
        except OSError:
            continue
        close = stat.rfind(")")
        open_ = stat.find("(")
        fields = stat[close + 2 :].split()
        if close < 0 or open_ < 0 or len(fields) < 2 or not fields[1].isdigit():
            continue
        if int(fields[1]) != parent:
            continue
        program = stat[open_ + 1 : close]
        seen.append(_Child(int(name), fields[0], program, None, None))
        here[0] = str(root / name / "exe")
        exe = _read_link(root / name / "exe")
        here[0] = str(root / name / "wchan")
        wait = _read_text(root / name / "wchan")
        seen[-1] = _Child(
            int(name),
            fields[0],
            program,
            None if own is None or exe is None else exe != own,
            None if wait in (None, "", "0") else wait,
        )
        if len(seen) >= MAX_CHILDREN_REPORTED:
            break
    return seen


def _proc_children_bounded(root: Path, parent: int) -> list[_Child] | str:
    """``_proc_children`` on a thread of its own, left behind after
    ``DIAGNOSE_SECONDS`` where a read does not return, so the report never blocks. A
    read can wait for ever on a child in the middle of ``vfork`` or ``exec``, and it
    cannot be interrupted, so the thread is abandoned and is a daemon. The text then
    names the file it was reading and the children found before it, which is itself
    evidence of where the child is."""
    at = [""]
    found: list[_Child] = []
    ended = threading.Event()

    def read() -> None:
        try:
            _proc_children(root, parent, at, found)
        except Exception:  # noqa: BLE001, S110
            pass
        finally:
            ended.set()

    reader = threading.Thread(target=read, name="dromon-spawn-report", daemon=True)
    reader.start()
    if ended.wait(DIAGNOSE_SECONDS):
        return list(found)
    where = Path(at[0])
    shown = "/".join(where.parts[-2:]) if where.parts[-2:] else at[0]
    return (
        f"reading /proc/{_clean(shown, 40)} did not return within"
        f" {DIAGNOSE_SECONDS:g}s, {_children_text(list(found))}"
    )


def _read_link(path: Path) -> str | None:
    try:
        return os.readlink(path)
    except OSError:
        return None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace").strip()
    except OSError:
        return None


def _parse_ps(text: str, parent: int, own: int) -> list[_Child]:
    """The children of ``parent`` from ``ps -A -o pid=,ppid=,stat=,wchan=,comm=``. A
    child has executed when its program differs from the row of ``own``, this
    process. Rows that do not parse are skipped, and at most
    ``MAX_CHILDREN_REPORTED`` are returned."""
    rows: list[tuple[int, int, str, str, str]] = []
    for line in text.splitlines():
        fields = line.split(None, 4)
        if len(fields) == 5 and fields[0].isdigit() and fields[1].isdigit():
            rows.append(
                (int(fields[0]), int(fields[1]), fields[2], fields[3], fields[4])
            )
    mine = next((row[4] for row in rows if row[0] == own), None)
    found = [
        _Child(
            pid,
            state,
            Path(program).name,
            None if mine is None else program != mine,
            None if wait in ("-", "") else wait,
        )
        for pid, ppid, state, wait, program in rows
        if ppid == parent
    ]
    return found[:MAX_CHILDREN_REPORTED]


def _ps_table(parent: int, own: int) -> list[_Child] | str:
    """The children of this process from ``ps``, or the reason it cannot be read. It
    runs on a thread of its own and is left behind after ``DIAGNOSE_SECONDS``, since
    the process that stalled may be unable to start ``ps`` as well."""
    result: list[list[_Child] | str] = []

    def read() -> None:
        try:
            done = _REAL_POPEN(  # noqa: S603
                ["/bin/ps", "-A", "-o", "pid=,ppid=,stat=,wchan=,comm="],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            out, _ = done.communicate(timeout=DIAGNOSE_SECONDS)
        except (OSError, subprocess.SubprocessError) as error:
            result.append(f"unavailable, {type(error).__name__}")
            return
        result.append(_parse_ps(out, parent, own))

    reader = threading.Thread(target=read, name="dromon-spawn-report", daemon=True)
    reader.start()
    reader.join(DIAGNOSE_SECONDS + 1)
    return result[0] if result else "unavailable, the read did not end"


_PROC_PIDTBSDINFO: Final = 3
_PROC_STATES: Final = {1: "I", 2: "R", 3: "S", 4: "T", 5: "Z"}
"""``pbi_status`` of macOS's ``proc_bsdinfo`` as the letters ``ps`` uses. ``I`` is a
process being created, one forked and not yet running a program."""


def _libproc_children(parent: int) -> list[_Child] | str:
    """The children of ``parent`` on macOS from ``libproc``, which reads the process
    table through the kernel and starts no process, so it works where ``ps`` cannot be
    started, as in an agent's sandbox, and where the stall has left the process
    unable to start one. A child has executed when its path differs from this
    process's own. At most ``MAX_CHILDREN_REPORTED`` are read. Gives the reason as text
    where it cannot read."""
    if sys.platform != "darwin":
        return "unavailable, not macOS"
    try:
        import ctypes  # noqa: PLC0415

        lib = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        ids = (ctypes.c_int * MAX_CHILDREN_REPORTED)()
        count = int(lib.proc_listchildpids(parent, ids, ctypes.sizeof(ids)))
        if count < 0:
            return "unavailable, proc_listchildpids failed"
        path = ctypes.create_string_buffer(4096)
        own = lib.proc_pidpath(os.getpid(), path, len(path)) > 0 and path.value
        found: list[_Child] = []
        for pid in list(ids)[: min(count, MAX_CHILDREN_REPORTED)]:
            if pid <= 0:
                continue
            info = (ctypes.c_uint32 * 34)()
            if (
                lib.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, info, ctypes.sizeof(info))
                <= 0
            ):
                continue
            status = int(info[1])
            name = bytes(ctypes.string_at(ctypes.addressof(info) + 48, 16)).split(
                b"\0"
            )[0]
            has_path = lib.proc_pidpath(pid, path, len(path)) > 0
            found.append(
                _Child(
                    pid,
                    _PROC_STATES.get(status, "?"),
                    name.decode(errors="replace"),
                    None if not own or not has_path else path.value != own,
                    None,
                )
            )
        return found
    except (OSError, AttributeError, ValueError) as error:
        return f"unavailable, {type(error).__name__}"


def _children_text(table: list[_Child] | str) -> str:
    if isinstance(table, str):
        return f"children of the daemon {table}"
    if not table:
        return "no child of the daemon in the process table"
    shown = ", ".join(
        f"pid {child.pid} state {_clean(child.state, 8)}"
        f" program {_clean(child.program, 40)}"
        + {None: "", True: " executed", False: " NOT executed"}[child.execed]
        + ("" if child.waiting is None else f" waiting {_clean(child.waiting, 24)}")
        for child in table
    )
    return f"children of the daemon {shown}"


def _descriptor_limit_text() -> str:
    """The soft limit on open descriptors, since a child that closes every descriptor
    up to it before its program runs takes time in proportion to it. **Round 15.**
    Timed on macOS with the soft limit at 1,048,576 and at 256, idle and with twice
    as many busy processes as cores, a start took about 20 milliseconds either way, so
    that is not the cause there, and the report shows the limit to rule it in or out
    where a stall is seen."""
    try:
        soft = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (OSError, ValueError):
        return "descriptor limit unknown"
    if soft < 0 or soft == resource.RLIM_INFINITY:
        return "descriptor limit unlimited"
    return f"descriptor limit {soft}"


def stall_report(
    thread: threading.Thread,
    handoff: _Handoff,
    *,
    proc: Path = Path("/proc"),
    table: Callable[[int, int], list[_Child] | str] = _ps_table,
    libproc: Callable[[int], list[_Child] | str] = _libproc_children,
) -> str:
    """What is known of a start that has stalled, bounded to ``MAX_REPORT_CHARS``, for
    the error and the daemon's log. **Round 15, stall diagnostics.** It says where the
    start's thread is in Python, how many starts were in flight with it, how many
    threads are alive, and what the daemon's children are doing, from ``/proc`` where
    that exists, from ``libproc`` on macOS and from ``ps`` otherwise. Together they
    tell the hypotheses in the module's note apart. It names no argument, since a
    command line may carry a secret, and it never raises."""
    try:
        with _active_lock:
            now, most = len(_active), handoff.overlap
        if (proc / "self" / "stat").exists():
            children: list[_Child] | str = _proc_children_bounded(proc, os.getpid())
        else:
            children = libproc(os.getpid())
            if isinstance(children, str):
                children = table(os.getpid(), os.getpid())
        parts = [
            f"the start thread is at {_where(thread)}",
            f"{now} starts in flight now and {most} at most during this one",
            f"{threading.active_count()} threads alive",
            _descriptor_limit_text(),
            _children_text(children),
        ]
        return _clean("; ".join(parts), MAX_REPORT_CHARS)
    except Exception as error:  # noqa: BLE001
        # A report must not hide the stall it describes.
        return f"the report failed, {type(error).__name__}"


def _kill(process: subprocess.Popen[Any]) -> None:
    """Send ``SIGKILL`` to a child that has not been reaped, and to its process group
    when the child leads one of its own, as it does with ``start_new_session`` or
    ``process_group``. **Review of 151cbcd.** The daemon's own group is never signalled.
    **Review of 7744806.** A child that shares the daemon's group is killed alone, so
    its descendants are not reached.
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
    handoff.deadline = handoff.began + bound + WATCHDOG_MARGIN_SECONDS

    def target() -> None:
        outcome: subprocess.Popen[Any] | BaseException
        _enter(handoff)
        try:
            outcome = subprocess.Popen(argv, **options)  # noqa: S603
        except BaseException as error:  # noqa: BLE001
            # Carried to the caller, which decides what it means.
            outcome = error
        finally:
            _leave(handoff)
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
        report = stall_report(thread, handoff)
        message = (
            f"{name} did not start within {bound:g}s, as its start stalled before the"
            " program ran, a known intermittent stall in subprocess that"
            " dromon.spawn describes. It is killed if it starts later."
            f" Report, {report}"
        )
        print(f"dromond: {message}", file=sys.stderr)
        raise StartStalled(message)
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


install_fork_lock()
disable_vfork()
