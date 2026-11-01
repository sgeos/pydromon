"""The processes a line started, found by a mark in their environment, and stopped.

Mark. Every command a line runs carries ``DROMON_LINE_MARK``, the line's identifier
and a random token issued for that line once per daemon run, including one started
with ``nohup`` that outlives its turn. Each line's token is its own, so a line knows
only its own mark and cannot present another line's. A restart issues new tokens, so a
process from an earlier daemon run is no longer listed as the line's.

Scope. ``list_processes`` shows only processes of the daemon's own user that carry the
calling line's mark, without the daemon, the launchers it started directly and the
engine each launcher started, as ``dromon.launcher`` describes, and
``stop_process`` signals only such a process, reading its environment again at the
moment of stopping. Claude Code, Codex and Grok Build scope their own process
controls to the agent's own session in the same way.

Execution. Daemon calls run on one worker thread with a bounded queue. Process-table
inspection can be slow under load, so it must not block the daemon's event loop or
allow an unbounded backlog of requests. Stopping the daemon closes the queue, rejects
new calls and joins each worker for at most one second. A process-table call already in
progress cannot be interrupted; when it returns, its daemon thread exits.

Failure modes. A process whose environment cannot be read is not the line's, so every
check fails closed. A process that ends between the check and the signal is reported
as gone. A pid reused by another process after the check is the race every signal by
pid has. It is narrowed, not removed, by checking immediately before signalling.

Porting note. Reading another process's environment is specific to each operating
system, and lives in ``read_environment`` alone. On macOS it runs ``ps`` with ``-E``,
which appends the environment to the command line, and on Linux it reads
``/proc/PID/environ``. Elsewhere nothing is readable, so no process is a line's. A
port must provide the same call for its platforms. The process listing uses ``ps``
with fields that macOS and Linux share.
"""

import asyncio
import contextlib
import os
import queue
import threading
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Generic, Protocol, TypeVar, cast

from dromon.engine_environment import BASE_ENVIRONMENT, allowlisted

MARK_VARIABLE: Final = "DROMON_LINE_MARK"
"""The environment variable that names the line a process belongs to."""

PROC: Final = Path("/proc")
"""Linux's process file system, a module constant so tests can stand in for it."""

PS_FIELDS: Final = "pid=,ppid=,pgid=,args="
"""The field list ``tree_or_reason`` gives ``ps``."""

PS_START_FIELDS: Final = "pid=,ppid=,pgid=,lstart=,args="
"""The field list of a listing that also gives each process's start time, which a
caller that signals what it finds needs to know the process is still the one found."""

PS_LISTING_MARK: Final = "pid=,ppid=,pgid="
"""What both field lists begin with, which identifies the ``ps`` of a listing."""

START_TOKENS: Final = 5
"""The words of ``lstart``, as in ``Sat Nov  1 07:23:05 2026``, in the C locale."""

UNIDENTIFIABLE: Final = (
    "This platform does not let the daemon read another process's environment, so it"
    " cannot tell which processes are yours, and it neither lists nor stops any. Use"
    " your engine's own background task tools within a turn, or ask an operator."
)
"""What the process tools say when the start-time self-test failed. Observed on macOS,
Darwin 27, where neither ``ps -E`` nor ``KERN_PROCARGS2`` showed another process's
environment."""

PROBE_VARIABLE: Final = "DROMON_PROBE"
PROBE_ATTEMPTS: Final = 20
PROBE_PAUSE_SECONDS: Final = 0.05

MAX_COMMAND_CHARACTERS: Final = 200
PS_TIMEOUT_SECONDS: Final = 10.0

MAX_PENDING_PROCESS_CALLS: Final = 8
"""At most eight process inspections wait behind the one running worker call."""
PROCESS_WORKER_JOIN_SECONDS: Final = 1.0
"""Bound on waiting for a process worker to finish when its daemon stops."""

_Result = TypeVar("_Result")
_MISSING: Final = object()


@dataclass(slots=True)
class _ProcessCall(Generic[_Result]):
    function: Callable[[], _Result]
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[_Result]
    cancelled: threading.Event


@dataclass(frozen=True, slots=True)
class ProcessInfo:
    pid: int
    ppid: int
    elapsed: str
    """As ``ps`` reports it, ``[[days-]hours:]minutes:seconds``."""
    command: str


class ProcessTable(Protocol):
    """The operating system's processes, as the line tools need them."""

    def own_processes(self) -> Sequence[ProcessInfo]:
        """Every process of the daemon's user."""
        ...

    def has_variable(self, pid: int, assignment: str) -> bool:
        """Whether ``pid``'s environment holds exactly ``assignment``, such as
        ``DROMON_LINE_MARK=alpha.token``. ``False`` when it cannot be read."""
        ...

    def environments(self) -> dict[int, list[str]]:
        """The readable environments of the daemon's user's processes, by pid, read
        in one platform call where the platform allows. A process whose environment
        cannot be read is absent."""
        ...

    def send(self, pid: int, signal_number: int) -> None:
        """Signal ``pid``. Raises ``ProcessLookupError`` or ``PermissionError``."""
        ...


class ProcessRefused(ValueError):
    """A process tool cannot do what the line asked."""


@dataclass(frozen=True, slots=True)
class TreeReadFailure:
    reason: str
    timed_out: bool = False

    def __str__(self) -> str:
        return self.reason


class ProcessWorker:
    """Run blocking process-table inspections on one daemon thread and bounded queue.

    The queue contains no more than ``MAX_PENDING_PROCESS_CALLS`` waiting calls, in
    addition to the one call being run. A full queue refuses new inspections instead
    of leaving an unbounded number of daemon tasks waiting for the worker. A queued
    call is skipped if its caller cancels before execution. A call already running
    cannot be stopped and may finish after its caller is cancelled.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[_ProcessCall[Any] | None] = queue.Queue(
            maxsize=MAX_PENDING_PROCESS_CALLS
        )
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False

    async def run(self, function: Callable[[], _Result]) -> _Result:
        """Run ``function`` with its arguments, or refuse if the queue is full."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_Result] = loop.create_future()
        call = _ProcessCall(function, loop, future, threading.Event())
        with self._lock:
            if self._closed:
                raise ProcessRefused("the process worker is stopped")
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._work, name="dromon-processes", daemon=True
                )
                self._thread.start()
            try:
                self._queue.put_nowait(call)
            except queue.Full as error:
                raise ProcessRefused("the process inspection queue is full") from error
        try:
            return await future
        except asyncio.CancelledError:
            call.cancelled.set()
            raise

    def close(self, timeout: float = PROCESS_WORKER_JOIN_SECONDS) -> bool:
        """Reject new calls, cancel queued calls and join the worker for a bounded time.

        A call already running cannot be interrupted. ``False`` means it did not
        finish within ``timeout``; its daemon thread will exit after that call returns.
        The daemon's normal stop path joins each worker, but cannot guarantee this
        within the bound when an in-flight call is still running.
        """
        if timeout < 0:
            raise ValueError("worker join timeout must not be negative")
        with self._lock:
            thread = self._thread
            if not self._closed:
                self._closed = True
                if thread is not None:
                    while True:
                        try:
                            call = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        self._queue.task_done()
                        if call is not None:
                            call.cancelled.set()
                            self._deliver(
                                call,
                                error=ProcessRefused("the process worker is stopped"),
                            )
                    self._queue.put_nowait(None)
        if thread is None:
            return True
        if thread is threading.current_thread():
            return False
        thread.join(timeout)
        return not thread.is_alive()

    def _work(self) -> None:
        while True:
            call = self._queue.get()
            if call is None:
                self._queue.task_done()
                return
            try:
                if call.cancelled.is_set():
                    continue
                result = call.function()
            except BaseException as error:
                self._deliver(call, error=error)
            else:
                self._deliver(call, result=result)
            finally:
                self._queue.task_done()

    @staticmethod
    def _deliver(
        call: _ProcessCall[_Result],
        *,
        result: object = _MISSING,
        error: BaseException | None = None,
    ) -> None:
        def finish() -> None:
            if call.future.done():
                return
            if error is not None:
                call.future.set_exception(error)
            else:
                call.future.set_result(cast(_Result, result))

        with contextlib.suppress(RuntimeError):
            call.loop.call_soon_threadsafe(finish)


def read_environments(uid: int) -> dict[int, list[str]]:
    """Every readable environment of ``uid``'s processes, by pid. On macOS one ``ps``
    call returns them all, and on Linux each ``/proc/PID/environ`` is a file read, not
    a process. Elsewhere none is readable. See the porting note."""
    if sys.platform == "darwin":
        try:
            found = subprocess.run(  # noqa: S603
                [  # noqa: S607
                    "/bin/ps",
                    "-wwE",
                    "-U",
                    str(uid),
                    "-o",
                    "pid=,command=",
                ],
                capture_output=True,
                text=True,
                timeout=PS_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return {}
        environments: dict[int, list[str]] = {}
        for line in found.stdout.splitlines():
            fields = line.split()
            if fields and fields[0].isdigit():
                environments[int(fields[0])] = fields[1:]
        return environments
    if sys.platform.startswith("linux"):
        environments: dict[int, list[str]] = {}
        for entry in PROC.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
            except OSError:
                continue
            found_entries = read_environment(int(entry.name))
            if found_entries is not None:
                environments[int(entry.name)] = found_entries
        return environments
    return {}


def read_environment(pid: int) -> list[str] | None:
    """``pid``'s environment as its entries, or ``None`` when it cannot be read. See
    the porting note. On macOS the entries come split at white space from a line that
    also holds the command's arguments, so an argument written exactly as an
    assignment counts as one, which a line can only do to its own processes."""
    if sys.platform.startswith("linux"):
        try:
            raw = (PROC / str(pid) / "environ").read_bytes()
        except OSError:
            return None
        return raw.decode("utf-8", "replace").split("\x00")
    if sys.platform == "darwin":
        try:
            found = subprocess.run(  # noqa: S603
                ["/bin/ps", "-wwE", "-o", "command=", "-p", str(pid)],  # noqa: S607
                capture_output=True,
                text=True,
                timeout=PS_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if found.returncode != 0:
            return None
        return found.stdout.split()
    return None


class SystemProcesses:
    """The real process table of the daemon's user."""

    def own_processes(self) -> Sequence[ProcessInfo]:
        try:
            found = subprocess.run(  # noqa: S603
                [  # noqa: S607
                    "ps",
                    "-ww",
                    "-U",
                    str(os.getuid()),
                    "-o",
                    "pid=,ppid=,etime=,args=",
                ],
                capture_output=True,
                text=True,
                timeout=PS_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProcessRefused(f"cannot list processes: {error}") from error
        return parse_ps(found.stdout)

    def has_variable(self, pid: int, assignment: str) -> bool:
        entries = read_environment(pid)
        return entries is not None and assignment in entries

    def environments(self) -> dict[int, list[str]]:
        return read_environments(os.getuid())

    def send(self, pid: int, signal_number: int) -> None:
        os.kill(pid, signal_number)


def environment_reading_works() -> bool:
    """Whether this platform lets the daemon read another process's environment, as
    the process tools need. A short child is started with a known variable, and both
    readers, the listing's and the stop's, must see it within about a second. The
    child receives only the common engine environment and that variable, and is always
    stopped. Any failure means the readers cannot be trusted, which the tools then
    report rather than returning an empty listing."""
    token = secrets.token_hex(16)
    assignment = f"{PROBE_VARIABLE}={token}"
    try:
        child = subprocess.Popen(  # noqa: S603
            ["/bin/sleep", "5"],  # noqa: S607
            env={**allowlisted(os.environ, BASE_ENVIRONMENT), PROBE_VARIABLE: token},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    try:
        # The child's environment is its own only once it has executed, so a few
        # short attempts cover the moment between the fork and the exec.
        for _ in range(PROBE_ATTEMPTS):
            listed = assignment in read_environments(os.getuid()).get(child.pid, ())
            single = read_environment(child.pid)
            if listed and single is not None and assignment in single:
                return True
            time.sleep(PROBE_PAUSE_SECONDS)
        return False
    finally:
        child.kill()
        child.wait()


def parse_ps(output: str) -> list[ProcessInfo]:
    """Rows of ``ps -o pid=,ppid=,etime=,args=``. A malformed row is skipped."""
    rows: list[ProcessInfo] = []
    for line in output.splitlines():
        fields = line.split(None, 3)
        if len(fields) < 3:
            continue
        try:
            pid, ppid = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        command = fields[3] if len(fields) == 4 else ""
        rows.append(ProcessInfo(pid, ppid, fields[2], command))
    return rows


def _excluded(
    process: ProcessInfo, daemon_pid: int, launched: frozenset[int] = frozenset()
) -> bool:
    """The daemon, the launchers it started directly and the engines each launcher
    started are never a line's to stop. ``launched`` holds the pids of the daemon's
    direct children, whose children are the engines."""
    return (
        process.pid == daemon_pid
        or process.ppid == daemon_pid
        or process.ppid in launched
    )


def _launched(processes: Sequence[ProcessInfo], daemon_pid: int) -> frozenset[int]:
    return frozenset(p.pid for p in processes if p.ppid == daemon_pid)


@dataclass(frozen=True, slots=True)
class TreeRow:
    """A process with its group, as the launcher walks the process tree."""

    pid: int
    ppid: int
    pgid: int
    command: str
    started: str = ""
    """The process's start time as ``ps`` prints it, or empty when the listing did not
    ask. **Round 15, re-review.** A pid is reused, and a process is the same one only
    if its start time is also unchanged, so a caller that signals a row checks this."""


def parse_tree(output: str, with_start: bool = False) -> list[TreeRow]:
    """Rows of ``ps -o pid=,ppid=,pgid=,args=``, or with ``with_start`` of ``ps -o
    pid=,ppid=,pgid=,lstart=,args=``. A malformed row is skipped."""
    rows: list[TreeRow] = []
    first = 3 + (START_TOKENS if with_start else 0)
    for line in output.splitlines():
        fields = line.split(None, first)
        if len(fields) < first:
            continue
        try:
            pid, ppid, pgid = int(fields[0]), int(fields[1]), int(fields[2])
        except ValueError:
            continue
        started = " ".join(fields[3:first])
        command = fields[first] if len(fields) == first + 1 else ""
        rows.append(TreeRow(pid, ppid, pgid, command[:MAX_COMMAND_CHARACTERS], started))
    return rows


def _ps_environment() -> dict[str, str]:
    """The environment ``ps`` runs in. The C locale gives ``lstart`` the same five
    words on every host, and gives a start time read twice the same text."""
    return {**os.environ, "LC_ALL": "C"}


def read_tree(
    uid: int, timeout: float = PS_TIMEOUT_SECONDS, with_start: bool = False
) -> list[TreeRow] | None:
    """Every process of ``uid`` with its parent and group, or ``None`` when ``ps``
    cannot be run, fails or takes longer than ``timeout``. ``None`` is never an empty
    tree, since a caller that ends what it finds must know that it found nothing
    because it could not look."""
    found = tree_or_reason(uid, timeout, with_start)
    return None if isinstance(found, TreeReadFailure) else found


def tree_or_reason(
    uid: int, timeout: float = PS_TIMEOUT_SECONDS, with_start: bool = False
) -> list[TreeRow] | TreeReadFailure:
    """Every process of ``uid`` with its parent and group, or why ``ps`` could not
    list them. ``ps`` takes these fields alike on macOS and Linux."""
    try:
        found = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ps",
                "-ww",
                "-U",
                str(uid),
                "-o",
                PS_START_FIELDS if with_start else PS_FIELDS,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_ps_environment(),
        )
    except OSError as error:
        return TreeReadFailure(f"ps cannot be run: {error}")
    except subprocess.TimeoutExpired:
        return TreeReadFailure(
            f"ps did not answer within {timeout:g} seconds", timed_out=True
        )
    if found.returncode != 0:
        said = found.stderr.strip().splitlines()
        detail = said[0][:MAX_COMMAND_CHARACTERS] if said else "nothing"
        return TreeReadFailure(
            f"ps failed with status {found.returncode} and said {detail}"
        )
    return parse_tree(found.stdout, with_start)


def descendants(rows: Sequence[TreeRow], root: int) -> list[TreeRow]:
    """Every process below ``root`` in ``rows`` by parent, and every member of a
    group that ``root`` or one of those leads, without ``root``, lowest pid first.
    The walk visits each row at most once, so it ends within ``len(rows)`` passes.

    **Platform limit.** A process whose parent has already exited has been given to
    the system's first process, or a subreaper, so its tree no longer leads here.
    Only the process mark can find it, which this platform may not let Dromon read."""
    children: dict[int, list[TreeRow]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row)
    found: dict[int, TreeRow] = {}
    frontier = [root]
    while frontier:
        parent = frontier.pop()
        for row in children.get(parent, ()):
            if row.pid != root and row.pid not in found:
                found[row.pid] = row
                frontier.append(row.pid)
    groups = {root} | {row.pgid for row in found.values() if row.pgid == row.pid}
    for row in rows:
        if row.pgid in groups and row.pid != root and row.pid not in found:
            found[row.pid] = row
    return sorted(found.values(), key=lambda row: row.pid)


ATTACHED_SECONDS: Final = 2.0
"""How long one walk for ``attached`` may take. A walk that takes longer finds
nothing, and the caller ends what it can reach without it."""


def attached(root: int, timeout: float = ATTACHED_SECONDS) -> list[TreeRow] | None:
    """The processes of the daemon's user that descend from ``root`` by parent, with
    the members of the groups those lead, whatever session each is in. ``None`` when
    the process table cannot be read, which is never an empty list. Blocking, so a
    caller on the event loop runs it in a thread.

    **Round 15, item 2.** This is how a relay command and a supervised run find a
    descendant that called ``setsid``, which a signal to the command's group misses.
    It cannot reach another line's or the operator's processes, because a process is
    found only by its ancestry from ``root`` and a process in another session cannot
    join a group a descendant leads. **Not the line mark.** The mark is read from the
    environment, which this platform may not let the daemon read, as
    ``environment_reading_works`` finds, and which a descendant can clear with ``env
    -i``. The ancestry cannot be cleared. **Limit.** A descendant whose parent has
    exited is no longer below ``root``, except on Linux where the caller is a child
    subreaper, as the launcher is and a run's supervisor is not, so it is not found.
    The caller's own process and ``root`` are never in the result."""
    rows = read_tree(os.getuid(), timeout, with_start=True)
    if rows is None:
        return None
    me = os.getpid()
    return [
        row
        for row in descendants(rows, root)
        if row.pid not in (me, root) and not _is_own_listing(row, me)
    ]


def _is_own_listing(row: TreeRow, me: int) -> bool:
    """Whether ``row`` is the ``ps`` that ``read_tree`` ran for ``me``. It is a child
    of the caller and lists itself, so a walk from the caller would find it among its
    descendants, count it as one outside the command's group and signal a process that
    is gone. **Round 15.** Found when a receipt named a descendant that was this."""
    return row.ppid == me and PS_LISTING_MARK in row.command


def line_processes(
    table: ProcessTable, mark: str, daemon_pid: int
) -> list[dict[str, object]]:
    """The line's processes, with pid, elapsed time and command cut to
    ``MAX_COMMAND_CHARACTERS``, oldest pid first."""
    assignment = f"{MARK_VARIABLE}={mark}"
    environments = table.environments()
    found: list[dict[str, object]] = []
    processes = table.own_processes()
    launched = _launched(processes, daemon_pid)
    for process in sorted(processes, key=lambda p: p.pid):
        if _excluded(process, daemon_pid, launched):
            continue
        if assignment not in environments.get(process.pid, ()):
            continue
        found.append(
            {
                "pid": process.pid,
                "elapsed": process.elapsed,
                "command": process.command[:MAX_COMMAND_CHARACTERS],
            }
        )
    return found


def marked_processes(
    table: ProcessTable, daemon_pid: int
) -> dict[str, list[tuple[int, str]]]:
    """Every process of the daemon's user that carries a line's mark, of this run or
    an earlier one, with its pid and its command cut to ``MAX_COMMAND_CHARACTERS``,
    by the line the mark names, oldest pid first. The daemon, its launchers and their
    engines are left out. Only readable environments are searched."""
    prefix = f"{MARK_VARIABLE}="
    environments = table.environments()
    processes = table.own_processes()
    launched = _launched(processes, daemon_pid)
    found: dict[str, list[tuple[int, str]]] = {}
    for process in sorted(processes, key=lambda p: p.pid):
        if _excluded(process, daemon_pid, launched):
            continue
        for entry in environments.get(process.pid, ()):
            if not entry.startswith(prefix):
                continue
            mark = entry[len(prefix) :]
            line_id = mark.rsplit(".", 1)[0] if "." in mark else ""
            if line_id:
                found.setdefault(line_id, []).append(
                    (process.pid, process.command[:MAX_COMMAND_CHARACTERS])
                )
            break
    return found


def stop_line_process(
    table: ProcessTable, mark: str, daemon_pid: int, pid: int, force: bool
) -> str:
    """Send ``SIGTERM``, or ``SIGKILL`` with ``force``, to ``pid`` if it is the line's,
    checking its mark again now. Returns what was done, or raises ``ProcessRefused``."""
    if type(pid) is not int or pid <= 1:
        raise ProcessRefused("pid must be a whole number above 1")
    processes = table.own_processes()
    process = next((p for p in processes if p.pid == pid), None)
    if process is None:
        raise ProcessRefused(f"process {pid} is not one of yours")
    if _excluded(process, daemon_pid, _launched(processes, daemon_pid)):
        raise ProcessRefused(f"process {pid} is the daemon or a line's engine")
    if not table.has_variable(pid, f"{MARK_VARIABLE}={mark}"):
        raise ProcessRefused(f"process {pid} is not one of yours")
    chosen = signal.SIGKILL if force else signal.SIGTERM
    try:
        table.send(pid, chosen)
    except ProcessLookupError as error:
        raise ProcessRefused(f"process {pid} has already ended") from error
    except PermissionError as error:
        raise ProcessRefused(f"process {pid} cannot be signalled") from error
    return f"sent {chosen.name} to process {pid}"


IDENTITY_SECONDS: Final = 1.0
"""How long the check of one process's identity may take."""


def identity_of(pid: int, timeout: float = IDENTITY_SECONDS) -> tuple[int, str] | None:
    """The parent and the start time of ``pid`` now, in the form a row from
    ``attached`` has them, or ``None`` when the process is gone or ``ps`` cannot say.
    Not knowing is never the same as knowing, so a caller that signals only a process
    it can show to be unchanged treats ``None`` as changed."""
    try:
        found = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "ps",
                "-ww",
                "-p",
                str(pid),
                "-o",
                "ppid=,lstart=",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_ps_environment(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    fields = found.stdout.split()
    if found.returncode != 0 or len(fields) != 1 + START_TOKENS:
        return None
    try:
        return int(fields[0]), " ".join(fields[1:])
    except ValueError:
        return None


def deepest_first(rows: Sequence[TreeRow]) -> list[TreeRow]:
    """``rows`` ordered so that no row comes before one of its descendants in
    ``rows``, deepest first and by pid within a depth. **Round 15, re-review.** A
    signal to a parent can end it, and a child that outlives it is given to the
    system's first process, so the parent check of ``signal_if_same`` would turn the
    child away. Children are therefore signalled before their parents. The depth is
    found by following parents within ``rows``, at most ``len(rows)`` steps each."""
    by_pid = {row.pid: row for row in rows}

    def depth(row: TreeRow) -> int:
        steps = 0
        current = row
        while steps < len(rows) and current.ppid in by_pid:
            current = by_pid[current.ppid]
            steps += 1
        return steps

    return sorted(rows, key=lambda row: (-depth(row), row.pid))


def signal_if_same(
    row: TreeRow,
    number: int,
    identity: Callable[[int], tuple[int, str] | None] | None = None,
) -> bool:
    """Signal ``row``'s process only if its parent and start time are still the ones
    the walk found, and say whether it was signalled. **Round 15, re-review.** A
    process that ended and was reaped between the walk and the signal can have its pid
    given to one that is not a descendant. A new process has another start time, so
    the check turns the signal away, and a row without a start time, which the
    listing did not ask for, is never signalled. The check is itself a reading and
    leaves a window of a few milliseconds, which is smaller and not zero. A process
    whose parent exited in the meantime has another parent and is turned away too,
    which is the platform limit stated for ``attached``."""
    if row.started == "":
        return False
    now = (identity if identity is not None else identity_of)(row.pid)
    if now != (row.ppid, row.started):
        return False
    try:
        os.kill(row.pid, number)
    except (ProcessLookupError, PermissionError):
        return False
    return True
