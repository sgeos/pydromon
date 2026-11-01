"""Supervised runs, a project-declared command a line starts that outlives its turn,
its seat and the daemon. ``docs/architecture/RUNS.md`` is normative.

The supervisor. ``python -m dromon.runs`` is the launcher's counterpart for a run. The
launcher ends its harness when the daemon dies, which is right for an engine and wrong
for a long build, so a run has a supervisor of its own instead. The daemon starts it in
a session of its own, so no stop of the daemon's group reaches it, and it starts the
command in a process group of its own, waits for it, and writes a receipt with how it
ended to the daemon's report directory. A supervisor exits without starting anything
unless the daemon writes ``GO`` to its input, which the daemon does only once the run
is in the event log, so a daemon that dies between the start and the record leaves no
unrecorded run behind.

Identity. The event log records the supervisor's process identifier and the start time
the process table gives for it. A later process that reuses the identifier has another
start time, so the record is never taken for it. **Platform limit.** ``ps`` gives the
start time to the second, so a reuse within the same second is not told apart. That
needs the identifier to come round within a second, which this design takes as
negligible and does not guard against.

Reconciliation. ``reconcile`` runs on the scheduler's process worker, never on the
event loop. For each run that has not ended, a receipt ends it as the receipt says. A
supervisor that is gone without a receipt, or whose identifier now names another
process, ends it as ``lost``. A process table that cannot be read ends nothing, so a
failure to look is never taken for a death. A lost run's command may still run, so it
keeps its scope blocked until ``settle`` finds no process in its supervisor's group or
session, or an operator clears it.

Output. The command's output goes to ``tmp/runs/RUN.out`` in its working directory,
where the role's seats can read it. A line may write that directory, and the daemon
runs outside any sandbox, so the file is opened one directory at a time with
``O_NOFOLLOW``, relative to the directory before it. A symbolic link put in the way is
refused, and no swap after a check can redirect the open. Its digest is read the same
way, without blocking, and only from a regular file, so the digest is of what the file
held when the end was recorded and a named pipe there cannot stall the reading.

Trust. A declared command runs with the daemon's authority and outside the engine's
sandbox, in a working directory that a writing line can change. A line that can
rewrite a script a declared command runs can therefore run code outside its sandbox.
Declaring a command is the operator's decision to accept that, as ``RUNS.md`` says.

Porting note. The supervisor is a Python process, which a port replaces with a native
one. ``os.open`` with ``dir_fd`` is ``openat``, and the start time is read with ``ps``,
whose ``lstart`` field macOS and Linux share. A port may read ``/proc/PID/stat`` on
Linux or ``proc_pidinfo`` on macOS instead.
"""

import contextlib
import hashlib
import json
import os
import select
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, cast

from dromon.processes import TreeRow, attached
from dromon.model import (
    MAX_EXIT_CODE,
    MAX_RUN_NOTE_BYTES,
    ID_PATTERN,
    RunStarted,
    RunStatus,
    format_due,
)

READY: Final = b"R"
"""What a supervisor writes once its stop handlers are installed."""
READY_SECONDS: Final = 10.0

GO: Final = b"1"
"""What the daemon writes to a supervisor's input once its run is recorded."""

NOT_RECORDED_EXIT: Final = 3
"""The supervisor's status when its input closed without ``GO``."""

OUTPUT_DIRECTORY: Final = ("tmp", "runs")
OUTPUT_SUFFIX: Final = ".out"
RECEIPT_SUFFIX: Final = ".run"
MAX_RECEIPT_BYTES: Final = 4096

STOP_GRACE_SECONDS: Final = 10.0
"""How long a stopped command's group has after ``SIGTERM`` before ``SIGKILL``."""
POLL_SECONDS: Final = 0.1
"""How often a supervisor looks whether its command has ended, unless a stop wakes
it sooner."""
STRAY_POLL_SECONDS: Final = 0.2
"""How often a stopped supervisor, whose command has ended, looks whether the
descendants outside the command's group have gone as well."""

MAX_DIGEST_BYTES: Final = 2**30
"""The largest output whose digest is read. A larger one is recorded with its size
alone, so a reconciliation cannot hold the process worker for long."""

DIGEST_CHUNK: Final = 1 << 20
PS_TIMEOUT_SECONDS: Final = 10.0
LSTART_FORMAT: Final = "%a %b %d %H:%M:%S %Y"
PS_ENVIRONMENT: Final = {"LC_ALL": "C", "TZ": "UTC", "PATH": "/usr/bin:/bin"}
"""``ps`` writes ``lstart`` in the C locale's form and in this time zone, so a
reading does not depend on the daemon's locale or zone."""

DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC


class RunRefused(ValueError):
    """A run cannot be started, read or stopped as asked."""


class TableUnreadable(RuntimeError):
    """The process table could not be read, so nothing about a run is concluded."""


@dataclass(frozen=True, slots=True)
class Receipt:
    """How a supervisor's command ended, as it wrote it."""

    status: RunStatus
    exit_code: int | None
    note: str


@dataclass(frozen=True, slots=True)
class Finding:
    """What reconciliation concluded about a run that has ended."""

    run_id: str
    status: RunStatus
    exit_code: int | None
    note: str
    output_digest: str | None
    output_bytes: int | None


@dataclass(slots=True)
class Launched:
    """A supervisor the daemon started and has not yet released with ``GO``."""

    child: subprocess.Popen[bytes]
    output_file: str
    process_started: str


# The daemon's side.


def _open_beneath(workdir: Path, parts: Sequence[str], create: bool) -> int:
    """A descriptor of ``workdir/parts...``, each step opened relative to the one
    before with ``O_NOFOLLOW``, and created first when ``create`` is set. Raises
    ``OSError`` when a step is a link, not a directory, or cannot be opened."""
    descriptor = os.open(workdir, DIRECTORY_FLAGS)
    try:
        for part in parts:
            if create:
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, 0o755, dir_fd=descriptor)
            following = os.open(
                part, DIRECTORY_FLAGS | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = following
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def open_output(workdir: Path, run_id: str) -> tuple[int, str]:
    """Create the run's output file under ``workdir`` and return a descriptor open
    for writing, with its path relative to ``workdir``. Raises ``RunRefused`` when a
    step is a link or the file exists already."""
    name = run_id + OUTPUT_SUFFIX
    try:
        directory = _open_beneath(workdir, OUTPUT_DIRECTORY, create=True)
        try:
            output = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o644,
                dir_fd=directory,
            )
        finally:
            os.close(directory)
    except OSError as error:
        raise RunRefused(
            f"the run's output cannot be created under {'/'.join(OUTPUT_DIRECTORY)}"
            f" in {workdir}: {error.strerror or error}. A symbolic link there is"
            " refused"
        ) from error
    return output, "/".join((*OUTPUT_DIRECTORY, name))


def output_digest(workdir: Path, output_file: str) -> tuple[str | None, int | None]:
    """The SHA-256 digest and size of a run's output, read as ``open_output``
    opens it, or both ``None`` when it cannot be read as a regular file. The digest
    is ``None`` alone for an output over ``MAX_DIGEST_BYTES``.

    A line can put a named pipe or a device at the name, so the file is opened with
    ``O_NONBLOCK``, which makes opening a pipe with no writer return at once, and is
    read only once ``fstat`` shows a regular file. Without it the open waits for a
    writer, and with it the process worker, for as long as the line likes."""
    *parts, name = output_file.split("/")
    try:
        directory = _open_beneath(workdir, parts, create=False)
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                dir_fd=directory,
            )
        finally:
            os.close(directory)
    except OSError:
        return None, None
    with os.fdopen(descriptor, "rb", buffering=0) as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode):
            return None, None
        if status.st_size > MAX_DIGEST_BYTES:
            return None, status.st_size
        digest = hashlib.sha256()
        size = 0
        for _ in range(MAX_DIGEST_BYTES // DIGEST_CHUNK + 1):
            chunk = handle.read(DIGEST_CHUNK)
            if not chunk:
                return digest.hexdigest(), size
            digest.update(chunk)
            size += len(chunk)
            if size > MAX_DIGEST_BYTES:
                return None, size
    return None, size


def parse_lstart(text: str) -> str | None:
    """``ps``'s ``lstart`` in the C locale and in Coordinated Universal Time, as an
    instant, or ``None`` when it is not one."""
    try:
        moment = datetime.strptime(" ".join(text.split()), LSTART_FORMAT)
    except ValueError:
        return None
    return format_due(moment.replace(tzinfo=UTC))


def process_start(pid: int) -> str | None:
    """When ``pid`` started, by the process table, or ``None`` when no process has
    it. Raises ``TableUnreadable`` when ``ps`` cannot answer, which is never taken
    for an absent process."""
    try:
        found = subprocess.run(  # noqa: S603
            ["/bin/ps", "-o", "lstart=", "-p", str(pid)],
            capture_output=True,
            text=True,
            env=PS_ENVIRONMENT,
            timeout=PS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TableUnreadable(f"ps cannot be run: {error}") from error
    text = found.stdout.strip()
    if not text:
        # ``ps -p`` answers a missing process with status 1 and nothing on either
        # stream, on macOS and on Linux. A status 1 with a complaint, such as a
        # denial, is a failure to look, never taken for an absent process.
        if found.returncode in (0, 1) and not found.stderr.strip():
            return None
        raise TableUnreadable(
            f"ps failed with status {found.returncode}: {found.stderr.strip()[:200]}"
        )
    started = parse_lstart(text)
    if started is None:
        raise TableUnreadable(f"ps gave a start time it could not parse: {text!r}")
    return started


def parse_groups(text: str) -> list[tuple[int, int]]:
    """``ps -A -o pid=,pgid=`` as pairs of a process and its group. Raises
    ``TableUnreadable`` for a line that is not two whole numbers, or for no lines,
    since the table always shows ``ps`` itself."""
    pairs: list[tuple[int, int]] = []
    for line in text.splitlines():
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 2 or not all(field.isdigit() for field in fields):
            raise TableUnreadable(f"ps gave a line it could not parse: {line!r}")
        pairs.append((int(fields[0]), int(fields[1])))
    if not pairs:
        raise TableUnreadable("ps listed no processes")
    return pairs


def process_groups() -> list[tuple[int, int]]:
    """Every process with its group, by the process table. Raises
    ``TableUnreadable`` when ``ps`` cannot answer."""
    try:
        found = subprocess.run(  # noqa: S603
            ["/bin/ps", "-A", "-o", "pid=,pgid="],
            capture_output=True,
            text=True,
            env=PS_ENVIRONMENT,
            timeout=PS_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise TableUnreadable(f"ps cannot be run: {error}") from error
    if found.returncode != 0:
        raise TableUnreadable(
            f"ps failed with status {found.returncode}: {found.stderr.strip()[:200]}"
        )
    return parse_groups(found.stdout)


def session_occupied(
    session: int,
    groups: Callable[[], list[tuple[int, int]]] = process_groups,
    session_of: Callable[[int], int] = os.getsid,
) -> bool:
    """Whether any process is in the process group or the session ``session``. A
    supervisor starts its own session, so its identifier names both, and its
    command's group lies within that session. Raises ``TableUnreadable`` when the
    table cannot be read or a process's session cannot be asked, so a failure to
    look never clears a run.

    **Limits.** A process that leaves the session with ``setsid`` is not found. A
    later process that reuses the identifier as a group or session leader is taken
    for the run's, which keeps the scope blocked until an operator clears it, the
    safe side. ``ps`` on macOS does not give a session identifier, so each
    process's session is asked with ``getsid``."""
    for pid, group in groups():
        if group == session:
            return True
        try:
            if session_of(pid) == session:
                return True
        except ProcessLookupError:
            continue
        except OSError as error:
            raise TableUnreadable(
                f"the session of process {pid} cannot be read: {error}"
            ) from error
    return False


def settle(
    runs: Sequence[RunStarted],
    occupied: Callable[[int], bool] = session_occupied,
) -> list[str]:
    """Which of ``runs``, each lost and not cleared, has no process left in its
    supervisor's group or session, and so may be cleared. Raises
    ``TableUnreadable``, clearing nothing. Blocking, so it runs on the process
    worker."""
    return [run.run_id for run in runs if not occupied(run.pid)]


def receipt_path(directory: Path, run_id: str) -> Path:
    if ID_PATTERN.fullmatch(run_id) is None:
        raise RunRefused(f"{run_id!r} is not a run identifier")
    return directory / (run_id + RECEIPT_SUFFIX)


def parse_receipt(raw: bytes) -> Receipt | None:
    """A receipt's content, or ``None`` for one that is malformed. It is data from
    a process the daemon started, so every field is checked."""
    if len(raw) > MAX_RECEIPT_BYTES:
        return None
    try:
        document: object = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    fields = cast(dict[str, object], document)
    status, code, note = (
        fields.get("status"),
        fields.get("exit_code"),
        fields.get("note"),
    )
    if status not in (RunStatus.EXITED.value, RunStatus.STOPPED.value):
        return None
    if code is not None and (
        type(code) is not int or not -MAX_EXIT_CODE <= code < MAX_EXIT_CODE
    ):
        return None
    if not isinstance(note, str) or len(note.encode()) > MAX_RUN_NOTE_BYTES:
        return None
    return Receipt(RunStatus(str(status)), code, note)


def read_receipt(directory: Path, run_id: str) -> Receipt | None:
    """The receipt for ``run_id``, or ``None`` when there is none. A malformed one
    is ``None`` too, so its run ends as ``lost`` once its supervisor is gone."""
    try:
        with receipt_path(directory, run_id).open("rb") as handle:
            raw = handle.read(MAX_RECEIPT_BYTES + 1)
    except OSError:
        return None
    return parse_receipt(raw)


def remove_receipt(directory: Path, run_id: str) -> None:
    with contextlib.suppress(OSError):
        receipt_path(directory, run_id).unlink()


def launch(
    directory: Path,
    run_id: str,
    workdir: Path,
    argv: Sequence[str],
    environment: Mapping[str, str],
    start_of: Callable[[int], str | None] = process_start,
) -> Launched:
    """Open the run's output, start its supervisor, wait until it says it is ready,
    so that a stop can no longer find it without its handlers, and read the
    supervisor's start time. It then waits for ``GO``. Raises ``RunRefused`` and
    leaves nothing running when any step fails. Blocking, so it runs on the process
    worker."""
    output, output_file = open_output(workdir, run_id)
    ready_read, ready_write = os.pipe()
    try:
        child = subprocess.Popen(  # noqa: S603
            [
                sys.executable,
                "-I",
                "-m",
                "dromon.runs",
                str(receipt_path(directory, run_id)),
                run_id,
                str(workdir),
                str(ready_write),
                "--",
                *argv,
            ],
            pass_fds=(ready_write,),
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=output,
            cwd="/",
            env=dict(environment),
            start_new_session=True,
        )
    except OSError as error:
        os.close(ready_read)
        raise RunRefused(f"the run's supervisor cannot start: {error}") from error
    finally:
        os.close(output)
        os.close(ready_write)
    try:
        readable, _, _ = select.select([ready_read], [], [], READY_SECONDS)
        ready = bool(readable) and os.read(ready_read, len(READY)) == READY
    finally:
        os.close(ready_read)
    if not ready:
        abandon(child)
        raise RunRefused(
            f"the run's supervisor did not become ready within {READY_SECONDS:g}"
            " seconds"
        )
    try:
        started = start_of(child.pid)
    except TableUnreadable as error:
        abandon(child)
        raise RunRefused(
            f"the run's supervisor cannot be identified: {error}"
        ) from error
    if started is None:
        abandon(child)
        raise RunRefused("the run's supervisor ended before it could be identified")
    return Launched(child, output_file, started)


def release(launched: Launched) -> bool:
    """Tell a recorded run's supervisor to start its command. ``False`` only when
    the word to go was not delivered, so the supervisor cannot have started it and
    exits once its input closes. A failure to close after delivery is ``True``."""
    stdin = launched.child.stdin
    if stdin is None:
        return False
    try:
        stdin.write(GO)
        stdin.flush()
    except (OSError, ValueError):
        delivered = False
    else:
        delivered = True
    # Closing flushes again and may fail the same way, but closes the pipe anyway.
    with contextlib.suppress(OSError, ValueError):
        stdin.close()
    return delivered


def abandon(child: subprocess.Popen[bytes]) -> None:
    """End a supervisor whose run was not recorded, by closing its input without
    ``GO``, and reap it."""
    if child.stdin is not None:
        with contextlib.suppress(OSError):
            child.stdin.close()
    try:
        child.wait(timeout=PS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()


def reconcile(
    runs: Sequence[RunStarted],
    directory: Path,
    start_of: Callable[[int], str | None] = process_start,
    children: Mapping[str, subprocess.Popen[bytes]] | None = None,
) -> list[Finding]:
    """What has become of each run in ``runs``, none of which has ended in the log.
    A run still running is left out. Raises ``TableUnreadable`` when the process
    table cannot be read, so no run is ended on a failure to look. Blocking, so it
    runs on the process worker.

    A supervisor the daemon started in this run is reaped first, so one that has
    exited is not seen as a process that has not been reaped. The receipt is read
    again after the table shows the supervisor gone, since it writes the receipt
    and then exits."""
    found: list[Finding] = []
    for run in runs:
        child = None if children is None else children.get(run.run_id)
        if child is not None:
            child.poll()
        receipt = read_receipt(directory, run.run_id)
        if receipt is None:
            started = start_of(run.pid)
            if started == run.process_started:
                continue
            receipt = read_receipt(directory, run.run_id)
            if receipt is None:
                note = (
                    "its supervisor ended without a receipt, as when it is killed or"
                    " the machine restarts, so the command may still run or may have"
                    " ended in any way"
                    if started is None
                    else f"its process identifier {run.pid} now belongs to another"
                    f" process, started {started}, so its supervisor is gone without"
                    " a receipt"
                )
                found.append(
                    Finding(run.run_id, RunStatus.LOST, None, note, *_digest(run))
                )
                continue
        found.append(
            Finding(
                run.run_id,
                receipt.status,
                receipt.exit_code,
                receipt.note,
                *_digest(run),
            )
        )
    return found


def _digest(run: RunStarted) -> tuple[str | None, int | None]:
    return output_digest(Path(run.workdir), run.output_file)


def signal_run(
    run: RunStarted,
    start_of: Callable[[int], str | None] = process_start,
    send: Callable[[int, int], None] = os.kill,
) -> None:
    """Ask a run's supervisor to stop its command, after checking that its
    identifier still names the supervisor. Raises ``RunRefused`` when it does not,
    or ``TableUnreadable``. The supervisor ends the command's group and writes a
    ``stopped`` receipt. Blocking, so it runs on the process worker.

    **Accepted window.** The check and the signal are two calls, and POSIX offers
    no way to signal a process only if it still has a start time. A supervisor that
    ends between them, with its identifier reused by then, sends ``SIGTERM`` to the
    later process. That needs the identifier to come round within the time ``ps``
    takes to answer. A port on Linux may close it with a process descriptor."""
    if start_of(run.pid) != run.process_started:
        raise RunRefused(
            f"run {run.run_id}'s supervisor is no longer running, so nothing was"
            " signalled. Its end is recorded at the next reconciliation"
        )
    try:
        send(run.pid, signal.SIGTERM)
    except ProcessLookupError as error:
        raise RunRefused(f"run {run.run_id}'s supervisor has just ended") from error


# The supervisor's side.


def write_receipt(path: Path, receipt: Receipt) -> None:
    """Write ``receipt`` whole, under a temporary name first, so the daemon never
    reads half of one."""
    document = {
        "status": receipt.status.value,
        "exit_code": receipt.exit_code,
        "note": receipt.note,
    }
    partial = path.with_name(f".{path.name}.{os.getpid()}")
    descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(document, handle)
        handle.flush()
        os.fsync(handle.fileno())
    partial.replace(path)


def _below_this_process() -> list[TreeRow] | None:
    """What descends from the supervisor, by ``dromon.processes.attached``."""
    return attached(os.getpid())


class Supervision:
    """The supervisor's stop, which its signal handlers share. Installed before the
    supervisor says it is ready, so no stop can find it without them.

    A handler only notes the stop. The thread that waits for the command signals its
    group, and only between two polls that found the command unreaped. The group's
    identifier is the command's process identifier, which cannot be reused while the
    command is unreaped, so no signal reaches a later group of the same identifier.
    **Review of 2026-10-31, R7.** A timer thread once sent ``SIGKILL`` on its own, and
    could do so after the command was reaped.

    **Round 15, item 2. Descendants in a session of their own.** A signal to the
    command's group misses a descendant that called ``setsid``. A stop therefore also
    walks the process table for what descends from the supervisor, by parent, and
    signals those, at ``SIGTERM`` and again at ``SIGKILL``. The walk comes before the
    group signal, because the signal orphans what the command left. Each signal goes
    to the result of a walk made just before it and never to a process identifier
    remembered from an earlier walk, so a process that ended and whose identifier was
    reused is not signalled unless it descends from the supervisor. **Limits.** A
    descendant whose parent has exited is no longer found, because this supervisor is
    not a child subreaper, which would leave every orphan unreaped until the run ends.
    The launcher is one because it reaps. A stop does not run when the supervisor
    itself is killed. A command that ends on its own is not followed by a walk, so a
    descendant it leaves is the lost-run check's to find, as ``session_occupied``
    says."""

    def __init__(
        self, walk: Callable[[], list[TreeRow] | None] = _below_this_process
    ) -> None:
        self.stopping = threading.Event()
        self._walk = walk
        self.outside: set[int] = set()
        """The descendants a stop signalled that were outside the command's group."""
        self.unreadable = False
        """Whether a walk could not read the process table."""

    def install(self) -> None:
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(number, self.stop)

    def stop(self, _signal: int, _frame: object) -> None:
        self.stopping.set()

    def wait(
        self,
        child: subprocess.Popen[bytes],
        grace: float = STOP_GRACE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> int:
        """Wait for ``child`` and return its status. Once a stop is noted, its group
        and the descendants below the supervisor are sent ``SIGTERM`` and, ``grace``
        seconds later, ``SIGKILL``. A command that ends first is not returned until
        the descendants outside its group are gone or ``grace`` has passed."""
        terminated_at: float | None = None
        killed = False
        while True:
            code = child.poll()
            if self.stopping.is_set():
                if terminated_at is None:
                    terminated_at = clock()
                    self._end(child, code, signal.SIGTERM)
                elif not killed and clock() - terminated_at >= grace:
                    killed = True
                    self._end(child, code, signal.SIGKILL)
            if code is not None:
                if not self.stopping.is_set() or killed:
                    return code
                left = self._walk()
                if not left:
                    return code
            self.stopping.wait(POLL_SECONDS if code is None else STRAY_POLL_SECONDS)

    def _end(
        self, child: subprocess.Popen[bytes], code: int | None, number: int
    ) -> None:
        """Walk the process table, then signal the command's group while the command
        is unreaped, then whatever the walk found. **The order matters.** The group
        signal ends the command, and a descendant that outlives it is given to the
        system's first process at that moment and is no longer below this one, so a
        walk made after the signal misses exactly the descendant it is for. The
        full-gate run of 2026-11-01 showed it. The walk is made at most a moment
        before the signals, so none goes to a process identifier from an earlier
        walk."""
        found = self._walk()
        if code is None:
            _signal_group(child.pid, number)
        if found is None:
            self.unreadable = True
            return
        for row in found:
            if row.pgid != child.pid:
                self.outside.add(row.pid)
            _signal_pid(row.pid, number)

    def note(self) -> str:
        """What a stop did beyond the command's group, for the receipt."""
        parts: list[str] = []
        if self.outside:
            parts.append(f"{len(self.outside)} descendants outside its group")
        if self.unreadable:
            parts.append(
                "the process table could not be read, so descendants outside its"
                " group may remain"
            )
        return "" if not parts else ", with " + " and ".join(parts)


def _signal_group(group: int, number: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(group, number)


def _signal_pid(pid: int, number: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, number)


def supervise(
    workdir: str, command: Sequence[str], supervision: Supervision
) -> Receipt:
    """Run ``command`` in ``workdir`` in a process group of its own and return how
    it ended, or that it was stopped before it started."""
    if supervision.stopping.is_set():
        return Receipt(RunStatus.STOPPED, None, "stopped before its command started")
    try:
        child = subprocess.Popen(  # noqa: S603
            list(command), cwd=workdir, stdin=subprocess.DEVNULL, process_group=0
        )
    except OSError as error:
        return Receipt(
            RunStatus.EXITED, 127, f"the command cannot start: {error}"[:512]
        )
    code = supervision.wait(child)
    if supervision.stopping.is_set():
        return Receipt(
            RunStatus.STOPPED,
            code,
            "stop_run asked its supervisor to end it" + supervision.note(),
        )
    return Receipt(RunStatus.EXITED, code, "")


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m dromon.runs RECEIPT RUN WORKDIR READY_FD -- COMMAND...``. It says
    ``READY`` on ``READY_FD`` once its handlers are installed, then waits for
    ``GO`` on its input."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) < 6 or arguments[4] != "--" or not arguments[3].isdigit():
        print("usage: python -m dromon.runs RECEIPT RUN WORKDIR READY_FD -- COMMAND...")
        return 2
    receipt, workdir, command = Path(arguments[0]), arguments[2], arguments[5:]
    supervision = Supervision()
    supervision.install()
    ready = int(arguments[3])
    with contextlib.suppress(OSError):
        os.write(ready, READY)
    with contextlib.suppress(OSError):
        os.close(ready)
    if os.read(0, len(GO)) != GO:
        return NOT_RECORDED_EXIT
    os.close(0)
    write_receipt(receipt, supervise(workdir, command, supervision))
    return 0


if __name__ == "__main__":
    sys.exit(main())
