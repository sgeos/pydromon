"""The processes a line started, found by a mark in their environment, and stopped.

Mark. Every command a line runs carries ``DROMON_LINE_MARK``, the line's identifier
and a random token issued for that line once per daemon run, including one started
with ``nohup`` that outlives its turn. Each line's token is its own, so a line knows
only its own mark and cannot present another line's. A restart issues new tokens, so a
process from an earlier daemon run is no longer listed as the line's.

Scope. ``list_processes`` shows only processes of the daemon's own user that carry the
calling line's mark, without the daemon and the engines it started directly, and
``stop_process`` signals only such a process, reading its environment again at the
moment of stopping. Claude Code, Codex and Grok Build scope their own process
controls to the agent's own session in the same way.

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

import os
import time
import secrets
import signal
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

MARK_VARIABLE: Final = "DROMON_LINE_MARK"
"""The environment variable that names the line a process belongs to."""

PROC: Final = Path("/proc")
"""Linux's process file system, a module constant so tests can stand in for it."""

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
        environments = {}
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
    child is always stopped. Any failure means the readers cannot be trusted, which
    the tools then report rather than returning an empty listing."""
    token = secrets.token_hex(16)
    assignment = f"{PROBE_VARIABLE}={token}"
    try:
        child = subprocess.Popen(  # noqa: S603
            ["/bin/sleep", "5"],  # noqa: S607
            env={**os.environ, PROBE_VARIABLE: token},
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


def _excluded(process: ProcessInfo, daemon_pid: int) -> bool:
    """The daemon and the engines it started directly are never a line's to stop."""
    return process.pid == daemon_pid or process.ppid == daemon_pid


def line_processes(
    table: ProcessTable, mark: str, daemon_pid: int
) -> list[dict[str, object]]:
    """The line's processes, with pid, elapsed time and command cut to
    ``MAX_COMMAND_CHARACTERS``, oldest pid first."""
    assignment = f"{MARK_VARIABLE}={mark}"
    environments = table.environments()
    found: list[dict[str, object]] = []
    for process in sorted(table.own_processes(), key=lambda p: p.pid):
        if _excluded(process, daemon_pid):
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


def stop_line_process(
    table: ProcessTable, mark: str, daemon_pid: int, pid: int, force: bool
) -> str:
    """Send ``SIGTERM``, or ``SIGKILL`` with ``force``, to ``pid`` if it is the line's,
    checking its mark again now. Returns what was done, or raises ``ProcessRefused``."""
    if type(pid) is not int or pid <= 1:
        raise ProcessRefused("pid must be a whole number above 1")
    process = next((p for p in table.own_processes() if p.pid == pid), None)
    if process is None:
        raise ProcessRefused(f"process {pid} is not one of yours")
    if _excluded(process, daemon_pid):
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
