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
names no argument, shows only printable ASCII, and is at most 1600 characters. The stuck
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

import asyncio
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

MAX_REPORT_CHARS: Final = 1600
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

FORK_LOCK_LOOP_SECONDS: Final = 5.0
"""How long a start made on an event loop's own thread waits for the fork lock.
**Review of round 16, finding 4.** The drivers that start children with
``asyncio.create_subprocess_exec`` run ``Popen`` on the loop's thread, so a thread stuck
in a fork held the loop for the whole of ``FORK_LOCK_SECONDS``, which is the stall that
moving the other starts to worker threads was meant to prevent. The wait is shorter
there and ends in the same ``StartStalled``, an ``OSError`` the drivers already treat as
a failed start. **Residual.** The loop can still wait this long, and the lock is a plain
mutex, so a long fork on another thread delays it."""

WATCHDOG_MARGIN_SECONDS: Final = 5.0
SLOW_START_SECONDS: Final = 2.0
"""A start that takes this long is recorded, with where the time went. **Round 16.**
A start of ``git`` takes about twenty milliseconds, so two seconds is a hundred times
the usual and still far below the 30 second bound, which makes the starts that are
slow without stalling visible."""
SLOW_FORK_SECONDS: Final = 1.0
"""A call that forks which takes this long is recorded in the ledger of slow starts,
whoever made it. **Round 16, from the CI logs of pull request 149.** The ledger of
starts saw only ``spawn.start``, and the slow tests of that run started their children
through ``asyncio`` and ``subprocess`` directly, which it did not see. The fork call is
the one place every start passes, so it is timed there."""
LOG_WAIT_SECONDS: Final = 1.0
"""How long a start waits for its diagnostic line to be written to the daemon's
standard error. **Review of round 16, finding 3.** A standard error that blocks, as a
pipe whose reader stopped does, held a start's thread in the line it was printing, and
a good start of 2.2 seconds was reported as stalled after 16. The line is written by a
thread of its own and the start waits this long for it, so a blocked standard error
costs a start at most this."""
LOG_INTERVAL_SECONDS: Final = 1.0
"""At most one slow-start line is written in this time. The ledger still records every
slow start up to ``MAX_SLOW_STARTS_KEPT``, so a loaded host that is slow for every start
does not fill the log."""
MAX_LOG_PENDING: Final = 16
"""The most diagnostic lines queued for a standard error that has not accepted the one
before. **Review of round 16, follow-up 3.** A bound, so that a standard error that
never accepts a line costs a few kilobytes and not the memory of the daemon."""
LOG_DRAIN_LIMIT: Final = 1000
"""The most lines one writer thread writes before it ends, a bound on its loop."""
SNAPSHOT_SECONDS: Final = 5.0
"""How long a start runs before the thread that waits for it records where every thread
of the process is and what processor time each has used. **Round 16, on the operator's
request of 2026-11-11.** The logs of pull request 149 show starts of 19 to 99 seconds
that took almost no processor time in the fork call, at a load of one, and the one dump
there is shows the main thread running Python in the scheduler while three others wait
in system calls. Which thread holds the interpreter lock is then the question, and a
start that ends well before the 30 second bound leaves no dump. A start with a bound of
``SNAPSHOT_SECONDS`` or less takes no snapshot."""
MAX_SNAPSHOT_THREADS: Final = 12
"""The most threads a snapshot lists, the busiest first where the time is known."""
MAX_SLOW_STARTS_KEPT: Final = 64
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


class _ForkTimes:
    """When the thread of a start asked for the fork lock, got it and let it go, on
    ``time.monotonic``'s clock, each ``0.0`` until it happens. **Round 16.** The lock is
    held for the call that forks. With ``vfork`` that call does not return until the
    child has run its program, and with ``fork`` it returns at once and the exec pipe
    is read afterwards, so a stall report that says whether the lock is still held says
    which of the two the thread is stuck in. Written by the one thread of the start and
    read by others, a plain float each, which Python makes atomic."""

    def __init__(self) -> None:
        self.requested = 0.0
        self.acquired = 0.0
        self.released = 0.0
        self.cpu = -1.0
        """The processor time the thread itself used in the fork call, in seconds, or
        ``-1.0`` where not measured. **Round 16, from the CI logs of pull request
        149.** A fork call of several seconds is either the kernel copying a large
        address space, which uses the thread's processor time, or a thread or a machine
        that was not running, which uses none. The wall time alone cannot tell them
        apart."""


_fork_slot = threading.local()
"""``current`` is the ``_ForkTimes`` of the start the thread is making, if any, and
``sweep`` is its ``_Sweep`` when the start leaves the closing of descriptors to the
parent."""


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
        self.fork = _ForkTimes()
        self.ended = 0.0
        self.closing = "the child closes descriptors"
        self.snapshot: str | None = None


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


def _on_a_loop() -> bool:
    """Whether the calling thread is running an event loop, whose start must not wait
    long for the fork lock."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


_FORK_INSTALLED: list[bool] = [False]


def _thread_cpu() -> float:
    """The calling thread's processor time in seconds, or ``-1.0`` where the platform
    does not give it."""
    try:
        return time.thread_time()
    except (OSError, AttributeError):
        return -1.0


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
    times: _ForkTimes | None = getattr(_fork_slot, "current", None)
    if getattr(_fork_slot, "holding", False):
        # Review of round 16, finding 4. The lock is not reentrant, so a start made by
        # the thread that holds it would wait for itself for the whole bound and then
        # report a stall that is not one. No caller nests, which a test asserts, so
        # this is refused at once as the programming error it is.
        raise RuntimeError(
            "a child start was made while this thread was already starting one"
        )
    requested = time.monotonic()
    if times is not None:
        times.requested = requested
    wait = FORK_LOCK_LOOP_SECONDS if _on_a_loop() else FORK_LOCK_SECONDS
    if not _fork_lock.acquire(timeout=wait):
        raise StartStalled(
            f"a child was not started: another start held the fork lock for {wait:g}s"
        )
    _fork_slot.holding = True
    # Timed from here, since a wait for the lock is not a stall in the fork.
    watched.began = time.monotonic()
    watched.deadline = watched.began + FORK_LOCK_SECONDS + WATCHDOG_MARGIN_SECONDS
    if times is not None:
        times.acquired = watched.began
    _watch(watched)
    cpu_began = _thread_cpu()
    sweep: _Sweep | None = getattr(_fork_slot, "sweep", None)
    try:
        if sweep is not None:
            _sweep_inheritable(sweep)
        yield
    finally:
        if sweep is not None:
            _unmark_inheritable(sweep)
        _unwatch(watched)
        released = time.monotonic()
        cpu = _thread_cpu() - cpu_began if cpu_began >= 0.0 else -1.0
        if times is not None:
            times.released = released
            times.cpu = cpu
        _fork_slot.holding = False
        _fork_lock.release()
        _note_slow_fork(
            wait=watched.began - requested,
            held=released - watched.began,
            cpu=cpu,
            lock_wait=wait,
        )


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
    ``posix_spawn``, so that every start forks. **Round 15, an experiment that did not
    help, and is now off unless asked for.** A CI run on Linux stalled a start of
    ``git``, and reading ``/proc`` for the report then blocked for the rest of five
    minutes, which fits a child that is in the middle of ``vfork``. CPython on Linux
    uses ``vfork`` since 3.10 through ``subprocess._USE_VFORK``, and ``glibc``'s
    ``posix_spawn``, which Python 3.15 uses when it can, makes the same kind of child,
    so both were turned off at import, from round 15 to the review of pull request 149.
    **Result.** Linux CI went on timing out with it, on pull request 144's re-run and
    on pull request 149, and the ledger then showed forks of 4.1 and 8.8 seconds in the
    call itself at no pressure, which a fork that copies the page tables of the whole
    daemon makes more likely and ``vfork`` avoids. Two other branches that did not
    have it passed at the same hour, which is one observation and not an experiment.
    It is therefore applied only when ``DROMON_NO_VFORK=1`` is set in the environment,
    so that a run can be made with and without it. **Not known.** Whether either
    setting changes the stall. ``os.posix_spawn`` calls made directly by this
    repository are not changed. Returns whether it changed anything. **Porting
    hazard.** It sets private names in the standard library, which only Python
    allows."""
    if not platform.startswith("linux"):
        return False
    changed = False
    for name in ("_USE_VFORK", "_USE_POSIX_SPAWN"):
        if hasattr(subprocess, name):
            setattr(subprocess, name, False)
            changed = True
    return changed


def apply_vfork_setting(
    environment: Mapping[str, str] | None = None, platform: str = sys.platform
) -> bool:
    """The rule applied at import. ``vfork`` is left as CPython has it unless the
    environment holds ``DROMON_NO_VFORK=1`` exactly. **Review of round 16, follow-up
    2.** It is a function so that a test can pin the Linux default with a platform it
    names, since a test run elsewhere never sees ``vfork`` change. Returns whether it
    changed anything."""
    chosen = os.environ if environment is None else environment
    if chosen.get("DROMON_NO_VFORK") != "1":
        return False
    return disable_vfork(platform)


CHILD_CLOSES_ENV: Final = "DROMON_CHILD_CLOSES_FDS"
"""``1`` makes every start close descriptors in the child as ``subprocess`` does, ``0``
makes every start leave that to the parent, as ``children_close_descriptors`` says. Any
other value, or none, leaves the platform's default. It exists so that a run can be made
with each setting, as ``DROMON_NO_VFORK`` does."""

_cleared_total: list[int] = [0]
"""How many inheritable descriptors the sweeps have made non-inheritable. Written only
inside the fork lock, where the sweeps run, and read by ``cleared_descriptors``."""


class _Sweep:
    """What a start asks of ``forking``, kept in ``_fork_slot.sweep`` for the thread
    that makes the start. ``passing`` are the descriptors the child is to inherit and
    ``cleared`` is set by the sweep, which runs inside the fork lock."""

    def __init__(self, passing: Sequence[int]) -> None:
        self.passing = tuple(passing)
        self.cleared: int | None = None


def children_close_descriptors(
    environment: Mapping[str, str] | None = None, platform: str = sys.platform
) -> bool:
    """Whether a start leaves the closing of descriptors to the child, as
    ``subprocess`` does by default, or does it in the parent. **Round 16, after the
    stuck child of 2026-11-11.** **Facts.** On macOS the child that ``subprocess``
    forks with ``close_fds`` set enumerates ``/dev/fd`` with ``opendir`` and
    ``readdir`` before it runs its program, as the symbols of the Python 3.12.12 and
    3.15.1 builds show, since macOS has no ``close_range``, and the stuck child seen
    from outside during a gate had no processor time and had ``/dev/fd`` open as a
    directory, with its capture pipes already duplicated. **Inference.** It stalled in
    that closing step. **Hypothesis, not confirmed.** The step takes a lock that a
    thread of the daemon held at the fork, since ``opendir`` allocates, and the
    child, a copy of one thread only, can never see it released. That fits zero
    processor time and the stress test's idle threads never reproducing it. So on
    macOS the default is ``False``, and ``spawn`` clears, in the parent, every
    descriptor above 2 that is inheritable and is not passed on purpose, then starts
    the child with ``close_fds`` off, which makes the child skip the enumeration and
    lets ``subprocess`` use ``posix_spawn`` where it can. Python makes every
    descriptor it opens non-inheritable, so the sweep finds only descriptors inherited
    from the daemon's own parent and any a library made inheritable. Elsewhere the
    default is ``True``, because Linux closes by ``close_range`` or a read of the
    directory with a system call, which allocates nothing, and the Linux stalls
    differ. **Not known.** Whether it removes the stall, which is one hypothesis among
    several in this module. **Porting hazard.** A Rust port sets close-on-exec on every
    descriptor it opens, which is the same rule, and needs no sweep."""
    chosen = os.environ if environment is None else environment
    setting = chosen.get(CHILD_CLOSES_ENV)
    if setting == "1":
        return True
    if setting == "0":
        return False
    return not platform.startswith("darwin")


def _open_descriptors() -> list[int] | None:
    """The numbers of this process's open descriptors from the directory the platform
    shows them in, or ``None`` when neither is readable. The descriptor of the
    directory read is in the list and no longer open by the time it is used, which the
    callers allow for. Done in the parent, where allocating is safe."""
    for directory in ("/dev/fd", "/proc/self/fd"):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        return sorted(int(name) for name in names if name.isdigit())
    return None


def _sweep_inheritable(sweep: _Sweep) -> None:
    """Make every descriptor above 2 non-inheritable that is not to be passed, and
    make those to be passed inheritable. **Review of round 16, audit finding 1, 2 and
    3.** It runs only inside ``forking``, with the fork lock held, immediately before
    the call that forks and for the thread that makes the call, so no other start can
    fork while a passed descriptor is inheritable, a start that stalls before its fork
    leaves nothing inheritable, and a descriptor made inheritable while the start waited
    for the lock is cleared. A descriptor that is not open, such as that of the listing
    itself, is skipped. Raises ``OSError``, and so no child is made, when the open
    descriptors cannot be listed, since the start has already told ``Popen`` not to
    close them, and when a descriptor to pass is not open. **Residual.** Another thread
    that makes a descriptor inheritable in the few instructions between the sweep and
    the fork still gives it to the child. Nothing in this repository does."""
    listing = _open_descriptors()
    if listing is None:
        raise OSError("the open descriptors cannot be listed, so no child was started")
    for descriptor in sweep.passing:
        os.set_inheritable(descriptor, True)
    cleared = 0
    for descriptor in listing:
        if descriptor <= 2 or descriptor in sweep.passing:
            continue
        try:
            if os.get_inheritable(descriptor):
                os.set_inheritable(descriptor, False)
                cleared += 1
        except OSError:
            continue
    _cleared_total[0] += cleared
    sweep.cleared = cleared


def _unmark_inheritable(sweep: _Sweep) -> None:
    """Make the passed descriptors non-inheritable again where they are still open,
    when the call that forks has returned or failed. Never raises."""
    for descriptor in sweep.passing:
        with contextlib.suppress(OSError):
            os.set_inheritable(descriptor, False)


def cleared_descriptors() -> int:
    """How many inheritable descriptors the sweeps have cleared in this process."""
    return _cleared_total[0]


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
    cpu: str | None = None
    """What tells a child that waits from one that spins, the processor time it has
    used and, where known, how many of its threads are running. **Round 16.** A state
    letter alone does not, since macOS reports ``live`` for any process that exists."""


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


def _thread_ticks(native_id: int) -> float | None:
    """Seconds of processor time the thread with the system's id has used, from
    ``/proc/self/task`` on Linux, or ``None`` where that is not readable."""
    text = _read_text(Path("/proc/self/task") / str(native_id) / "stat")
    if text is None:
        return None
    try:
        fields = text[text.rindex(")") + 2 :].split()
        ticks = int(fields[11]) + int(fields[12])
        per_second = os.sysconf("SC_CLK_TCK")
    except (ValueError, IndexError, OSError):
        return None
    return ticks / per_second if per_second > 0 else None


def thread_snapshot() -> str:
    """Every thread of the process in one line, with its name, the processor time it
    has used where the platform says, and its innermost three frames. **Round 16.** It
    shows which thread runs Python while others wait. A thread whose innermost frame
    is a call that waits on the operating system, such as ``open`` or ``read``, may
    equally be waiting to take the interpreter lock back, which a snapshot cannot tell
    apart, so the processor times are what separates a thread that runs from one that
    waits. At most ``MAX_SNAPSHOT_THREADS`` threads, the busiest first. Never raises."""
    try:
        frames = sys._current_frames()  # pyright: ignore[reportPrivateUsage]
        known = {thread.ident: thread for thread in threading.enumerate()}
        rows: list[tuple[float, str]] = []
        for ident, frame in frames.items():
            thread = known.get(ident)
            name = thread.name if thread is not None else f"thread-{ident}"
            used = None if thread is None else _thread_ticks(thread.native_id or 0)
            steps = traceback.extract_stack(frame, limit=3)
            where = " <- ".join(
                f"{Path(step.filename).name}:{step.lineno} {step.name}"
                for step in reversed(steps)
            )
            cpu = "" if used is None else f" cpu {used:.1f}s"
            rows.append((-1.0 if used is None else used, f"{name}{cpu} at {where}"))
        rows.sort(key=lambda row: row[0], reverse=True)
        listed = [text for _, text in rows[:MAX_SNAPSHOT_THREADS]]
        more = len(rows) - len(listed)
        suffix = f", and {more} more" if more > 0 else ""
        return _clean("; ".join(listed) + suffix, 1500)
    except Exception as error:  # noqa: BLE001
        return f"the threads could not be listed: {type(error).__name__}"


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
        cpu = _proc_cpu(fields)
        seen.append(_Child(int(name), fields[0], program, None, None, cpu))
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
            cpu,
        )
        if len(seen) >= MAX_CHILDREN_REPORTED:
            break
    return seen


def _proc_cpu(fields: list[str]) -> str | None:
    """Processor time of a child from the fields of its ``stat`` after the program
    name, where ``utime`` and ``stime``, in clock ticks, are the twelfth and thirteenth.
    **Round 16.** A child that waits has used almost none and one that spins has used
    as much as it has lived."""
    try:
        ticks = int(fields[11]) + int(fields[12])
        per_second = os.sysconf("SC_CLK_TCK")
    except (IndexError, ValueError, OSError):
        return None
    if per_second <= 0:
        return None
    return f"cpu {ticks * 1000 // per_second}ms"


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
_PROC_TASKINFO: Final = 4
_PROC_STATES: Final = {1: "I", 2: "live", 3: "S", 4: "T", 5: "Z"}
"""``pbi_status`` of macOS's ``proc_bsdinfo``. ``I`` is a process being created, one
forked and not yet running a program. **Round 16, a correction.** Status 2 is shown as
``live`` and not as ``R``, because the kernel reports it for any process that exists.
A plain ``sleep`` child showed ``R`` in a test, so it does not mean running, and an
earlier reading of a stalled child as running was unfounded."""


def _libproc_cpu(lib: Any, pid: int) -> str | None:  # noqa: ANN401
    """Processor time and running threads of ``pid`` from ``proc_taskinfo``, whose
    times are in units of ``mach_timebase_info``, or ``None`` where it cannot be read.
    The layout is six unsigned 64-bit values and twelve signed 32-bit ones, 96 bytes,
    which was checked on macOS by timing a spinning child and a sleeping one, so a
    different size is refused and not guessed at."""
    import ctypes  # noqa: PLC0415

    raw = (ctypes.c_char * 96)()
    if lib.proc_pidinfo(pid, _PROC_TASKINFO, 0, raw, 96) != 96:
        return None
    big = ctypes.cast(raw, ctypes.POINTER(ctypes.c_uint64))
    small = ctypes.cast(ctypes.addressof(raw) + 48, ctypes.POINTER(ctypes.c_int32))
    scale = (ctypes.c_uint32 * 2)()
    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    if system.mach_timebase_info(scale) != 0 or scale[1] == 0:
        return None
    nanos = (int(big[2]) + int(big[3])) * scale[0] // scale[1]
    # Round 16. The counters that tell a child that waits for memory from one that
    # waits for a lock, which are page faults, pages read in, copy-on-write faults,
    # Unix system calls made and context switches, with the resident size in megabytes.
    return (
        f"cpu {nanos // 1_000_000}ms {int(small[10])}/{int(small[9])} threads running,"
        f" resident {int(big[1]) // 1_000_000}MB, faults {int(small[1])}"
        f" pageins {int(small[2])} cow {int(small[3])},"
        f" unix syscalls {int(small[7])} switches {int(small[8])}"
    )


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
                    _libproc_cpu(lib, pid),
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
        + ("" if child.cpu is None else f" {_clean(child.cpu, 40)}")
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
            _fork_text(handoff),
            _children_text(children),
            _machine_text(),
            _descriptor_limit_text(),
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


_slow_starts: list[str] = []
_slow_lock = threading.Lock()


class _Log:
    """The state of the diagnostic lines, replaced by name in tests. ``pending`` holds
    the lines not yet written, each with whether it is ``limited``, ``writer`` is the
    one thread that drains it, and ``last`` is the monotonic time of the last limited
    line accepted."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.pending: list[tuple[str, bool]] = []
        self.writer: threading.Thread | None = None
        self.last = -LOG_INTERVAL_SECONDS
        self.dropped = 0


_log = _Log()


def _write_line(text: str) -> None:
    # A diagnostic line must not disturb the start it describes.
    with contextlib.suppress(Exception):
        print(text, file=sys.stderr)


def _queue_line(log: _Log, text: str, limited: bool, now: float) -> bool:
    """Add a line to ``log.pending``, with ``log.lock`` held, and say whether it was
    kept. A limited line closer than ``LOG_INTERVAL_SECONDS`` to the last is refused.
    When the queue is full a limited line is dropped first, the oldest of them, and an
    unlimited line only when every queued line is unlimited, so a stall report is not
    lost behind slow-start lines. Every drop is counted."""
    if limited:
        if now - log.last < LOG_INTERVAL_SECONDS:
            return False
        log.last = now
    if len(log.pending) >= MAX_LOG_PENDING:
        for index, (_, was_limited) in enumerate(log.pending):
            if was_limited:
                del log.pending[index]
                log.dropped += 1
                break
        else:
            if limited:
                log.dropped += 1
                return False
            del log.pending[0]
            log.dropped += 1
    log.pending.append((text, limited))
    return True


def _drain_log(log: _Log) -> None:
    """The writer thread. It writes the queued lines in order and ends when none are
    left, deciding that under the lock so that a line queued as it ends starts a new
    writer. It handles at most ``LOG_DRAIN_LIMIT`` lines and then ends, and a later
    line starts another."""
    for _ in range(LOG_DRAIN_LIMIT):
        with log.lock:
            if not log.pending:
                log.writer = None
                return
            text, _limited = log.pending.pop(0)
        _write_line(text)
    with log.lock:
        log.writer = None


def _say(text: str, limited: bool, wait: float | None = None) -> None:
    """Queue ``text`` for the daemon's standard error and wait for the writer thread at
    most ``LOG_WAIT_SECONDS``, or ``wait`` when given. **Review of round 16, finding
    3.** At most one writer thread exists, so a standard error that never accepts a
    line holds one thread and is not a thread for each start. **Follow-up 3.** A line
    that arrives while another is being written is queued, up to ``MAX_LOG_PENDING``,
    and is not dropped, because a stall report was lost behind a slow-start line. When
    the queue is full a limited line goes first, see ``_queue_line``. ``limited`` also
    refuses the line when one was accepted less than ``LOG_INTERVAL_SECONDS`` ago. Never
    raises, and never waits past the bound."""
    now = time.monotonic()
    log = _log
    with log.lock:
        if not _queue_line(log, text, limited, now):
            return
        writer = log.writer
        if writer is None:
            writer = threading.Thread(
                target=_drain_log, args=(log,), name="dromon-spawn-log", daemon=True
            )
            log.writer = writer
            starting = True
        else:
            starting = False
    try:
        if starting:
            writer.start()
        writer.join(LOG_WAIT_SECONDS if wait is None else wait)
    except RuntimeError:
        # No thread could be made. The line stays queued for the next writer, and the
        # slot is freed here, since ``writer`` is set before it starts so that two
        # callers cannot make two.
        if starting:
            with log.lock:
                if log.writer is writer:
                    log.writer = None


def slow_starts() -> list[str]:
    """The slow starts recorded in this process, at most ``MAX_SLOW_STARTS_KEPT``,
    oldest first. **Round 16.**"""
    with _slow_lock:
        return list(_slow_starts)


def clear_slow_starts() -> None:
    with _slow_lock:
        _slow_starts.clear()


def _since(handoff: _Handoff, moment: float) -> str:
    return "never" if moment == 0.0 else f"+{moment - handoff.began:.2f}s"


def _fork_text(handoff: _Handoff) -> str:
    """Where the start's thread is among the steps of a start, from when it asked for
    the fork lock and let it go, which says whether it is in the call that forks or
    waiting for the program after it. Times are since the start began."""
    times = handoff.fork
    if times.requested == 0.0:
        return "the thread has not reached the fork"
    taken = (
        "the lock was not yet taken"
        if times.acquired == 0.0
        else (f"the lock was taken {_since(handoff, times.acquired)}")
    )
    if times.acquired != 0.0 and times.released == 0.0:
        return (
            f"asked for the fork lock {_since(handoff, times.requested)}, {taken},"
            " and the fork call has not returned"
        )
    return (
        f"asked for the fork lock {_since(handoff, times.requested)}, {taken}, and"
        f" the fork call returned {_since(handoff, times.released)}"
    )


def _pressure(proc: Path, name: str) -> str | None:
    """The ``some`` line of Linux's pressure stall information for ``name``, which
    says how long tasks waited for the resource, as ``avg10`` percent."""
    text = _read_text(proc / "pressure" / name)
    if text is None:
        return None
    for line in text.splitlines():
        if line.startswith("some "):
            for field in line.split()[1:]:
                if field.startswith("avg10="):
                    return f"{name} wait {_clean(field[6:], 8)}%"
    return None


def _status_kb(text: str, key: str) -> int | None:
    """The number of kilobytes on the ``key`` line of a ``/proc/<pid>/status`` text."""
    for line in text.splitlines():
        if line.startswith(key + ":"):
            fields = line.split()
            if len(fields) >= 2 and fields[1].isdigit():
                return int(fields[1])
            return None
    return None


def _process_text(proc: Path) -> str | None:
    """The size of this process, which a fork copies the page tables of, and how many
    threads it has. **Round 16, from the CI logs of pull request 149.** The pull
    request's reviewer inferred that a fork of seconds at no pressure is the copy of a
    large address space, which this shows, with the processor time of the fork call."""
    text = _read_text(proc / "self" / "status")
    if text is None:
        return None
    resident, virtual = _status_kb(text, "VmRSS"), _status_kb(text, "VmSize")
    if resident is None or virtual is None:
        return None
    return f"parent resident {resident // 1024}MB virtual {virtual // 1024}MB"


def _steal_text(proc: Path) -> str | None:
    """The processor time, in seconds since the machine started, that a virtual
    machine's host took from it, the eighth figure of the first line of ``stat``. A
    runner whose host is busy shows it here, as the load and the pressure figures do
    not, and the difference between two lines is what happened between them."""
    text = _read_text(proc / "stat")
    if text is None:
        return None
    fields = text.splitlines()[0].split() if text else []
    if len(fields) >= 9 and fields[0] == "cpu" and fields[8].isdigit():
        return f"cpu steal total {int(fields[8]) / 100:.1f}s"
    return None


def _throttle_text(cgroup: Path) -> str | None:
    """How often and how long the processor quota of this control group stopped the
    process, from ``cpu.stat``, which the pressure figures do not show as waiting."""
    text = _read_text(cgroup / "cpu.stat")
    if text is None:
        return None
    found: dict[str, int] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1].isdigit():
            found[fields[0]] = int(fields[1])
    if "nr_throttled" not in found or "throttled_usec" not in found:
        return None
    return (
        f"cgroup throttled {found['nr_throttled']} times for"
        f" {found['throttled_usec'] / 1e6:.1f}s"
    )


def _machine_text(
    proc: Path = Path("/proc"), cgroup: Path = Path("/sys/fs/cgroup")
) -> str:
    """The load of the machine, which tells a slow machine from a stuck child.
    **Round 16.** The load averages from ``os.getloadavg`` and, where ``/proc`` has
    them, the share of the last ten seconds that tasks waited for the processor, for
    input and output and for memory. A runner that is starved shows it here."""
    parts: list[str] = []
    try:
        one, five, fifteen = os.getloadavg()
        parts.append(f"load {one:.1f} {five:.1f} {fifteen:.1f}")
    except (OSError, AttributeError):
        pass
    for name in ("cpu", "io", "memory"):
        found = _pressure(proc, name)
        if found is not None:
            parts.append(found)
    for extra in (_process_text(proc), _steal_text(proc), _throttle_text(cgroup)):
        if extra is not None:
            parts.append(extra)
    return ", ".join(parts) if parts else "no load figures"


def _note_slow_start(
    handoff: _Handoff, name: str, outcome: subprocess.Popen[Any] | BaseException
) -> str | None:
    """Record a start that took at least ``SLOW_START_SECONDS``, whether it succeeded,
    failed or was abandoned, with where the time went and how loaded the machine was,
    and return the line to write, or ``None``. Called by the start's own thread, after
    the call returned. **Review of round 16, finding 3.** The line is not written here,
    since the caller of this function writes it after the outcome is handed over, so a
    standard error that blocks cannot hold a start that has already succeeded. Never
    raises."""
    try:
        total = handoff.ended - handoff.began
        if total < SLOW_START_SECONDS:
            return None
        times = handoff.fork
        waited = max(times.acquired - times.requested, 0.0) if times.acquired else 0.0
        held = max(times.released - times.acquired, 0.0) if times.released else 0.0
        rest = max(total - waited - held, 0.0)
        kind = type(outcome).__name__ if isinstance(outcome, BaseException) else "ok"
        text = _clean(
            f"{Path(name).name} {kind} after {total:.1f}s, {waited:.1f}s waiting for"
            f" the fork lock, {held:.1f}s in the fork call, {rest:.1f}s elsewhere,"
            f" {handoff.overlap} starts overlapped, {threading.active_count()} threads,"
            f" {handoff.closing}, {_machine_text()}",
            700,
        )
        if handoff.snapshot is not None:
            text = f"{text}, threads at {SNAPSHOT_SECONDS:g}s: {handoff.snapshot}"
        with _slow_lock:
            if len(_slow_starts) < MAX_SLOW_STARTS_KEPT:
                _slow_starts.append(text)
        return f"dromond: slow start, {text}"
    except Exception:  # noqa: BLE001, S110
        # A record must not disturb the start it describes.
        pass
    return None


def _note_slow_fork(*, wait: float, held: float, cpu: float, lock_wait: float) -> None:
    """Record, and write once in a while, a call that forks which took at least
    ``SLOW_FORK_SECONDS``, whoever made the start. **Round 16, from the CI logs of
    pull request 149.** It says how long the call took, how much of that was the
    thread's own processor time, how long the thread waited for the fork lock, and the
    size and load of the machine. The thread did not use its time when the processor
    time is near zero, and the kernel copied a large process when it is near the
    whole. Called after the lock is released, so nothing here holds a fork. Writes
    without waiting, since the caller may be the event loop. Never raises."""
    try:
        if held < SLOW_FORK_SECONDS:
            return
        used = "not measured" if cpu < 0.0 else f"{cpu:.1f}s of its own processor time"
        text = _clean(
            f"fork call {held:.1f}s, {used}, {wait:.1f}s waiting for the fork lock"
            f" (bound {lock_wait:g}s), thread {threading.current_thread().name},"
            f" {threading.active_count()} threads, {_machine_text()}",
            700,
        )
        with _slow_lock:
            if len(_slow_starts) < MAX_SLOW_STARTS_KEPT:
                _slow_starts.append(text)
        _say(f"dromond: slow fork, {text}", limited=True, wait=0.0)
    except Exception:  # noqa: BLE001, S110
        # A record must not disturb the start it describes.
        pass


def _begin(
    argv: _Argv,
    options: dict[str, Any],
    start_seconds: float | None,
    close_after: Sequence[int],
    essential: bool,
) -> subprocess.Popen[Any]:
    bound = START_SECONDS if start_seconds is None else start_seconds
    name = os.fspath(argv[0]) if argv else "(no program)"
    # Review of round 16, finding 5. Messages show the program's base name only, since
    # the full path of a program under a home directory would reach the log and a
    # ``StartStalled`` message that a caller may pass on.
    shown = Path(name).name or name
    if not bound > 0:
        _close_all(close_after)
        raise ValueError(f"the start bound must be above 0, not {bound:g}")
    limit = MAX_STRANDED_ESSENTIAL if essential else MAX_STRANDED
    if stranded_count() >= limit:
        _close_all(close_after)
        raise StartStalled(
            f"{shown} was not started: {limit} earlier starts are still stuck, and a"
            " stuck start ends only when its thread does or the daemon restarts"
        )
    handoff = _Handoff()
    handoff.deadline = handoff.began + bound + WATCHDOG_MARGIN_SECONDS

    def target() -> None:
        outcome: subprocess.Popen[Any] | BaseException
        _enter(handoff)
        _fork_slot.current = handoff.fork
        sweep: _Sweep | None = None
        try:
            chosen: dict[str, Any] = options
            if not children_close_descriptors():
                # Review of round 16, follow-up. The sweep runs in ``forking``, which
                # only the wrapped fork call enters, so without the wrapper the child
                # would be told not to close and nothing would clear or pass a thing.
                if not _FORK_INSTALLED[0]:
                    handoff.closing = "the child closes descriptors, no fork lock"
                elif _open_descriptors() is None:
                    handoff.closing = "the child closes descriptors, none listed"
                else:
                    # Done by ``forking``, inside the fork lock, just before the fork.
                    sweep = _Sweep(options["pass_fds"])
                    _fork_slot.sweep = sweep
                    chosen = {**options, "close_fds": False, "pass_fds": ()}
            outcome = subprocess.Popen(argv, **chosen)  # noqa: S603
            if sweep is not None and sweep.cleared is None:
                # The child exists but was not given its descriptors, or was given
                # strays, because the call that forked did not enter ``forking``.
                # Refused as a failed start, never run on.
                _reap(outcome)
                raise OSError(
                    f"{shown} was not started: the descriptor sweep did not run"
                    " before the fork"
                )
        except BaseException as error:  # noqa: BLE001
            # Carried to the caller, which decides what it means.
            outcome = error
        finally:
            _fork_slot.current = None
            _fork_slot.sweep = None
            handoff.ended = time.monotonic()
            _leave(handoff)
            if sweep is not None and sweep.cleared is not None:
                handoff.closing = f"{sweep.cleared} descriptors cleared in the parent"
        slow = _note_slow_start(handoff, name, outcome)
        # The descriptors are closed here, once the start has ended, not by a caller
        # that was told the start stalled, who would close them under a late start.
        _close_all(close_after)
        with handoff.lock:
            handoff.outcome = outcome
            abandoned = handoff.abandoned
        handoff.done.set()
        if abandoned and isinstance(outcome, subprocess.Popen):
            _reap(outcome)
        if slow is not None:
            _say(slow, limited=True)

    thread = threading.Thread(target=target, name="dromon-spawn", daemon=True)
    thread.start()
    first = min(SNAPSHOT_SECONDS, bound)
    if not handoff.done.wait(first) and bound > first:
        handoff.snapshot = thread_snapshot()
        _say(
            f"dromond: {shown} still starting after {first:g}s, threads: "
            f"{handoff.snapshot}",
            limited=True,
        )
        handoff.done.wait(bound - first)
    with handoff.lock:
        outcome = handoff.outcome
        if outcome is None:
            handoff.abandoned = True
    if outcome is None:
        with _stranded_lock:
            _stranded.append(thread)
        report = stall_report(thread, handoff)
        message = (
            f"{shown} did not start within {bound:g}s, as its start stalled before the"
            " program ran, a known intermittent stall in subprocess that"
            " dromon.spawn describes. It is killed if it starts later."
            f" Report, {report}"
        )
        _say(f"dromond: {message}", limited=False)
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
apply_vfork_setting()
