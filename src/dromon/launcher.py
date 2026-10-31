"""The launcher, a small watcher between the daemon and each harness it starts.

**Rows 1 and 2 of the T3 Code review, and the operator's decision B of 2026-10-26.**
Every engine adapter starts its harness through this module, Claude Code's included,
whose Agent SDK is pointed at the ``dromon-launch`` command. The launcher starts a
session of its own, starts the harness in another session, whose process group the
harness leads and its children join, and then waits for one of three things.

- **The harness exits.** What it left behind is ended or recorded, as below, and the
  launcher exits with the harness's status.
- **The daemon asks it to stop,** with ``SIGTERM``, ``SIGINT`` or ``SIGHUP``. The
  harness is ended, and what it started is ended or recorded.
- **The daemon dies,** by a stop, a crash or ``SIGKILL``. The launcher sees that its
  parent is no longer the daemon that started it, which on macOS and on Linux is what
  happens when a parent exits, and does as for a stop.

Stop policy. ``StopPolicy.KILL_ALL``, the default, ends the harness and every
descendant the launcher can find, by parent and by process group, which fails closed.
``StopPolicy.LEAVE_BACKGROUND`` ends the harness alone and writes the processes it
left running, with their pids and commands, to a report file in the daemon's report
directory, which the daemon records in the event log and in the line's status. A
report written while the daemon is down is recorded when it next starts.

**The harness's own teardown, under ``LEAVE_BACKGROUND``.** A harness can end what it
started when its input closes or when it is asked to stop. **Fact, read from the
bundled Claude Code 2.1.310 on 2026-10-26 and observed live the same day.** Once its
input has closed, Claude Code's print mode kills each background shell after a grace
of 5 seconds, and kills any shell still running when its stream closes. Its process is
the harness, so it does this before any launcher could act, and a graceful close of the
Agent SDK, which closes the input first, ended a background ``sleep`` under
``LEAVE_BACKGROUND`` in the live check. So under that policy the launcher stands
between the daemon's input and the harness's, as ``InputRelay`` says. It copies the
input across and never closes the harness's side. When the daemon's side closes, or a
stop arrives, the launcher first lets the harness run on for ``FLUSH_SECONDS`` with its
input still open, then freezes it, lists its descendants, and kills it with
``SIGKILL``, which no harness can answer by ending what it started. The cost is that
the harness has no orderly exit under this policy. **Review N2, read from the bundled
Claude Code 2.1.310 on 2026-10-26.** Claude Code offers no request that writes out its
session without ending it or starting a turn. It writes its transcript from a queue
that it drains every 100 milliseconds, and as it resumes it seals a last line that a
kill cut short. The grace is several of those drains. Claude Code was resumed after a
``SIGKILL`` in the live check, which is what a later turn needs. Under ``KILL_ALL``
nothing is relayed, and the harness keeps its orderly exit.

**A launcher that cannot finish, review N1.** As the harness starts, the launcher writes
a launch record to the daemon's report directory, naming the line, its own pid, the
harness's pid, which is also its group's, the harness's command and the stop policy,
and marks it done once its stop has finished. Once a launcher has exited, for any
reason, the daemon sweeps the group its record names, as ``sweep_launches`` says, so a
launcher killed while the harness was frozen does not leave it frozen and alive. On
Linux the harness is also given a parent death signal, so it learns at once that its
launcher died, ``SIGTERM`` under ``KILL_ALL`` and ``SIGKILL`` under
``LEAVE_BACKGROUND``. A launcher counts as exited when ``ps`` shows it ``<defunct>`` or
its harness has another parent, not only when its pid no longer answers a signal, and
the daemon then treats the line's engine as failed and closes it. **Third live run of
N1, 2026-10-26.**

Termination. Under ``KILL_ALL`` the launcher's first act is ``SIGSTOP`` to the
harness's group, so that nothing in it exits or starts another process while the
process table is walked. Then ``SIGTERM`` and ``SIGCONT`` go to the group and to every
other descendant the walk found, which reaches one that started a group of its own,
and after ``EXIT_SECONDS`` ``SIGKILL`` goes to what remains. The harness's group does
not hold the launcher, so the launcher can kill all of it and still exit with the
harness's status. The whole stop fits within ``STOP_BUDGET_SECONDS``, inside the Agent
SDK's window, as ``SDK_CLOSE_SECONDS`` records. The daemon signals the launcher and
its group, and kills them when the launcher does not exit within
``LAUNCHER_EXIT_SECONDS``.

The harness is left unreaped until its group has been ended. A process that has not
been reaped keeps its pid, and so its group's identifier, from reuse, so no signal to
the harness or its group can reach an unrelated process. Exit is seen without reaping,
by ``waitid`` with ``WNOWAIT`` on Linux and by ``kqueue`` elsewhere.

**Platform limits, stated plainly.**
- On Linux the launcher is a child subreaper, so a descendant whose parent exited is
  given to the launcher, which the walk then reaches. Elsewhere, macOS included, no
  subreaper exists, and a descendant whose parent exited before the walk, and which
  started a session of its own, is reached by no walk. Only the process mark
  identifies it, which macOS 27 does not let Dromon read. The daemon records this gap
  once in the event log and shows it in every line's status, as ``walk_gap`` says.
- The launcher's parent check runs every ``POLL_SECONDS``, so a launcher outlives a
  dead daemon by at most about that long, and then by the stop's own bound.
- A descendant that is not the launcher's own child can be reaped by its parent, and
  its pid reused, between the walk and a signal. On Linux an orphan is the launcher's
  own child and stays unreaped until the launcher exits. Elsewhere the window remains,
  and is narrowed by repeating the walk before the last round.
- The walk runs ``ps``. Under ``KILL_ALL`` the daemon refuses to start a turn when
  ``ps`` cannot run. A walk that times out is retried within a 0.75-second deadline.
  If it still fails at stop time, the launcher ends the harness's group, writes an
  unverified-stop report and exits unsuccessfully. The daemon records the report in
  the event log. **Review F2.**
- A launcher that is itself killed with ``SIGKILL`` cannot act, and leaves its
  harness running, and stopped if the kill came between ``SIGSTOP`` and ``SIGCONT``.
  The daemon's sweep of its launch record then continues the group and ends or
  records it, but reaches only the harness's group. A descendant in a group of its
  own is reached only by the launcher's walk, which did not finish.

Porting notes. ``report_directory`` is set once per daemon run with ``configure``,
which a port passes explicitly instead. The launcher is a separate Python process,
which a port replaces with a native one. The input relay is a thread, which a port
may replace with the launcher's own poll of both descriptors. It lives as long as the
launcher, which bounds it. **Review N4.** The parent check is a
poll, which a port may replace with ``kqueue`` on macOS and a pidfd or
``PR_SET_PDEATHSIG`` on Linux. The subreaper and the harness's parent death signal
are set with ``prctl`` through ``ctypes``, the latter in a function the child runs
before it starts the harness, and the exit watch uses
``os.waitid`` or ``select.kqueue``, each of which a port calls directly. The answer
to the Agent SDK's version check imports the SDK, which a port replaces with its own
lookup of the Claude Code command.
"""

import asyncio
import contextlib
import ctypes
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
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

from dromon.processes import MARK_VARIABLE, TreeRow, descendants, tree_or_reason
from dromon.engine_environment import (
    BASE_ENVIRONMENT,
    CLAUDE_ENGINE_ENVIRONMENT,
    TOOL_SEARCH_PINNED,
    TOOL_SEARCH_VARIABLE,
    allowlisted,
)


class StopPolicy(StrEnum):
    """What stopping a line's engine, or the daemon, does to what the engine started.
    ``KILL_ALL``, the default, ends the harness and every descendant Dromon can find,
    which fails closed. ``LEAVE_BACKGROUND`` ends the harness alone and records what
    it left running."""

    KILL_ALL = "kill_all"
    LEAVE_BACKGROUND = "leave_background"


STOP_POLICY_VARIABLE: Final = "DROMON_STOP_POLICY"
"""Set by an adapter in the environment it starts its harness with, and read by the
daemon's side of the launch. The harness never sees it."""
TARGET_VARIABLE: Final = "DROMON_LAUNCH_TARGET"
PARENT_VARIABLE: Final = "DROMON_LAUNCH_PARENT"
REPORT_VARIABLE: Final = "DROMON_LAUNCH_REPORT"
FILTER_ENVIRONMENT_VARIABLE: Final = "DROMON_LAUNCH_FILTER_ENVIRONMENT"
LAUNCH_MARK_VARIABLE: Final = "DROMON_LAUNCH_MARK"
"""The line's process mark for the launcher alone, for a harness that must not carry
the mark itself, as Codex does not. A report names its line by the mark."""
LAUNCH_VARIABLES: Final = (
    STOP_POLICY_VARIABLE,
    TARGET_VARIABLE,
    PARENT_VARIABLE,
    REPORT_VARIABLE,
    FILTER_ENVIRONMENT_VARIABLE,
    LAUNCH_MARK_VARIABLE,
)
"""Variables for the launcher alone, removed before the harness starts."""

RELAYED_VARIABLES: Final = (
    "GIT_SSH_COMMAND",
    "SHELL",
    "CLAUDE_CODE_SHELL",
)
"""Variables a Claude Code line's adapter gives its shell commands, which the
launcher's filter would drop as daemon variables. The adapter sets each under the
name ``relayed`` gives it, and the filter renames it back, so that only the adapter's
value, never the daemon's own, reaches the engine. ``dromon.gitssh`` gives them."""
RELAYED_PREFIX: Final = "DROMON_RELAYED_"


def relayed(name: str) -> str:
    """The name the adapter sets ``name`` under, for the launcher's filter."""
    return RELAYED_PREFIX + name


def unrelayed(environment: dict[str, str]) -> None:
    """Rename each relayed variable in ``environment`` back to its own name, in
    place, dropping an empty one, and drop every variable under its own name first,
    so a daemon's value of it never passes."""
    for name in RELAYED_VARIABLES:
        environment.pop(name, None)
        value = environment.pop(relayed(name), "")
        if value:
            environment[name] = value


SCRIPT_NAME: Final = "dromon-launch"
"""The launcher's command, installed beside the Python interpreter, which the Agent
SDK runs in place of Claude Code."""

VERSION_ARGUMENTS: Final = (("-v",), ("--version",))
"""The arguments of the Agent SDK's version check. **Fact, read from Agent SDK 0.2.174
on 2026-10-26.** The SDK runs ``cli_path -v`` before each connection, with a 2 second
bound and with the daemon's own environment rather than the engine's, so the launcher
is not told its target. It reads the first output for a version, warns when that is
below its minimum, and ignores every failure."""

POLL_SECONDS: Final = 0.5
"""How often the launcher checks its parent. A signal or the harness's exit wakes it
at once."""

SDK_CLOSE_SECONDS: Final = 5.0
"""**Fact, read from Agent SDK 0.2.174 on 2026-10-26.** The SDK's close closes the
engine's standard input and waits 5 seconds for it to exit, then sends ``SIGTERM``
and waits 5 seconds more, then sends ``SIGKILL``. The process it started is here the
launcher, so a launcher that has not finished its stop within 5 seconds of the
``SIGTERM`` is killed, and its harness is left running."""

WALK_SECONDS: Final = 0.5
"""How long one attempt to read the process table may take."""

WALK_DEADLINE_SECONDS: Final = 0.75
"""The total bound for retries of one process-table walk at launcher stop."""

EXIT_SECONDS: Final = 2.0
"""How long what the launcher terminated may take to exit before it is killed."""

REAP_SECONDS: Final = 0.5
"""How long a killed harness may take to be reaped."""

FLUSH_SECONDS: Final = 0.5
"""``LEAVE_BACKGROUND``: how long the harness runs on, its input still open, between
the daemon's close or stop and its freeze, so that it can write out what it queued.
Five of Claude Code's transcript drains, as the module's account of review N2 says."""

STOP_BUDGET_SECONDS: Final = (
    FLUSH_SECONDS + 2 * WALK_DEADLINE_SECONDS + EXIT_SECONDS + REAP_SECONDS
)
"""The longest a launcher's stop takes, the flush grace, two walk deadlines, one wait
and the reap, which must leave a margin within ``SDK_CLOSE_SECONDS``. ``KILL_ALL`` has
no grace, so its stop is shorter."""

LAUNCHER_EXIT_SECONDS: Final = STOP_BUDGET_SECONDS + 2.0
"""How long the daemon waits for a launcher it signalled before it kills it."""

KILLED_EXIT_SECONDS: Final = 2.0
"""How long the daemon waits for a launcher it killed to be reaped."""

CHECK_SECONDS: Final = 0.1
RELAY_CHUNK: Final = 64 * 1024
"""The most bytes the input relay reads at once."""
RELAY_BACKLOG: Final = 1024 * 1024
"""The most bytes the input relay holds that the harness has not yet read. While it
holds that many it reads no more, so it does not see the daemon's close until the
harness reads again. **Review N3.**"""
MAX_LEFT: Final = 64
"""The most processes one report names. The count of the rest is kept."""

MAX_REPORT_BYTES: Final = 64 * 1024
MAX_REPORTS: Final = 256
"""The most report files the daemon reads at once. The rest wait for the next read."""

MAX_KEPT_REPORTS: Final = 256
"""How many read reports the daemon keeps, as the only copy of the whole commands they
name, before it removes the oldest. **Review N9.**"""

REPORT_SUFFIX: Final = ".json"
READ_SUFFIX: Final = ".read"
"""A report the daemon has recorded, kept under this suffix and readable by its owner
alone."""
LAUNCH_SUFFIX: Final = ".launch"
"""A launcher's record of its harness, as ``write_launch`` says."""
MAX_LAUNCHES: Final = 256
"""The most launch records one sweep reads. The rest wait for the next sweep."""
MAX_LAUNCH_BYTES: Final = 4096

LAUNCHER_NAMES: Final = ("dromon.launcher", SCRIPT_NAME)
"""What a running launcher's command line holds, as ``python -m dromon.launcher`` or
as the ``dromon-launch`` script, by which the sweep tells that one still runs."""

EXITED: Final = "exited"
STOPPED: Final = "stopped"
ORPHANED: Final = "orphaned"
CAUSES: Final = frozenset({EXITED, STOPPED, ORPHANED})
"""Why a launcher ended its harness: the harness exited by itself, the daemon asked,
by a signal or by closing the harness's input, or the daemon died."""

PR_SET_CHILD_SUBREAPER: Final = 36
"""The ``prctl`` option, from Linux's ``prctl.h``, that makes a process the reaper of
its orphaned descendants."""

PR_SET_PDEATHSIG: Final = 1
"""The ``prctl`` option, from Linux's ``prctl.h``, that signals a process when the
thread that started it exits."""

WALK_GAP: Final = (
    "This platform has no child subreaper. A process an engine started, which started"
    " a session of its own and whose parent then exited, may exist beyond the"
    " launcher's walk of the process table, and is then neither ended nor reported."
)
"""What the daemon records once and shows in every line's status where
``walk_gap`` finds the gap."""


_report_directory: Path | None = None


def configure(directory: Path | None) -> None:
    """Name the directory every launcher this process starts writes its reports to,
    or none, in which case a launcher that leaves processes running says so on its
    standard error only. The daemon calls this once as it starts."""
    global _report_directory  # noqa: PLW0603
    _report_directory = directory


def report_directory() -> Path | None:
    return _report_directory


def walk_gap(platform: str = sys.platform) -> str | None:
    """``WALK_GAP`` where ``platform`` has no child subreaper, else ``None``."""
    return None if platform.startswith("linux") else WALK_GAP


def walk_unavailable() -> str | None:
    """Why the process table cannot be walked here, or ``None`` when it can. The
    daemon asks before each turn under ``KILL_ALL``, which refuses the turn rather
    than end only what it can reach without the walk. **Review F2.** It allows ``ps``
    the ordinary bound, not ``WALK_SECONDS``, since it asks whether ``ps`` can run at
    all, and a slow answer on a loaded machine must not refuse a turn."""
    found = tree_or_reason(os.getuid())
    return found if isinstance(found, str) else None


def policy_of(environment: Mapping[str, str]) -> StopPolicy:
    """The stop policy ``environment`` names, ``KILL_ALL`` when it names none, and
    ``KILL_ALL`` for anything that is not a policy's exact name, which fails closed."""
    value = environment.get(STOP_POLICY_VARIABLE, "")
    return (
        StopPolicy.LEAVE_BACKGROUND
        if value == StopPolicy.LEAVE_BACKGROUND.value
        else StopPolicy.KILL_ALL
    )


def launch_environment(target: str, policy: StopPolicy) -> dict[str, str]:
    """The variables that tell a launcher what to start, whom to watch and what to
    do, which the launcher removes before the harness starts."""
    variables = {
        TARGET_VARIABLE: target,
        PARENT_VARIABLE: str(os.getpid()),
        STOP_POLICY_VARIABLE: policy.value,
    }
    directory = report_directory()
    if directory is not None:
        variables[REPORT_VARIABLE] = str(directory)
    return variables


def wrap(
    command: Sequence[str], environment: Mapping[str, str]
) -> tuple[list[str], dict[str, str], StopPolicy]:
    """``command``, with its executable first, as the launcher starts it: the
    arguments, the environment and the stop policy, read from ``environment``."""
    if not command:
        raise ValueError("a launch needs a command")
    policy = policy_of(environment)
    # The adapter's own launch variables are replaced, but for the mark it gives the
    # launcher alone, which the launcher reads and removes.
    launched = {
        k: v
        for k, v in environment.items()
        if k not in LAUNCH_VARIABLES or k == LAUNCH_MARK_VARIABLE
    }
    launched.update(launch_environment(command[0], policy))
    arguments = [sys.executable, "-I", "-m", "dromon.launcher", *command[1:]]
    return arguments, launched, policy


def script_path() -> str:
    """The ``dromon-launch`` command beside the running interpreter. Raises
    ``FileNotFoundError`` when it is not installed there."""
    path = Path(sys.executable).parent / SCRIPT_NAME
    if not path.is_file():
        raise FileNotFoundError(f"{path} is not installed")
    return str(path)


def claude_command() -> str:
    """The Claude Code command the Agent SDK would run, found as the SDK finds it,
    which the launcher starts in its place. Porting hazard: it calls the SDK
    transport's own lookup, which an SDK upgrade must check, and the import is here
    so that a launcher of another harness never loads the SDK."""
    from claude_agent_sdk import ClaudeAgentOptions  # noqa: PLC0415
    from claude_agent_sdk._internal.transport.subprocess_cli import (  # noqa: PLC0415
        SubprocessCLITransport,
    )

    return SubprocessCLITransport(prompt="", options=ClaudeAgentOptions())._find_cli()  # pyright: ignore[reportPrivateUsage]


def version_target(arguments: Sequence[str]) -> str | None:
    """For the Agent SDK's version check, which runs the launcher with ``-v`` alone
    and without its target, the Claude Code the SDK would have run. ``None`` for any
    other arguments, or when Claude Code cannot be found. **Review F8.**"""
    if tuple(arguments) not in VERSION_ARGUMENTS:
        return None
    try:
        return claude_command()
    except Exception:  # noqa: BLE001
        return None


def version_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """The short-lived environment for the Agent SDK's version-only CLI check."""
    selected = allowlisted(environment, BASE_ENVIRONMENT)
    selected[TOOL_SEARCH_VARIABLE] = TOOL_SEARCH_PINNED
    return selected


# The daemon's side.


def signal_group(group: int, number: int) -> bool:
    """Send ``number`` to every process in ``group``, or with zero only ask whether any
    remains. Returns whether any remained."""
    try:
        os.killpg(group, number)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A member that changed its credentials cannot be signalled, and macOS also
        # reports a group of zombies so. Neither is a process Dromon can end.
        return False
    return True


def signal_launch(pid: int, number: int, policy: StopPolicy) -> None:
    """Signal a launcher the daemon has not reaped, which carries out the stop policy
    itself. Under ``KILL_ALL`` its group is signalled as well, which holds the
    launcher alone once it has started its harness in a session of its own, and
    anything that joined it. The launcher itself is signalled by pid too, which
    reaches it before it has made its own group. **Review F3.**"""
    if policy is StopPolicy.KILL_ALL:
        signal_group(pid, number)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, number)


async def sweep_group(group: int) -> None:
    """End what remains of a launcher's process group once it has exited, with a
    termination, a bounded wait and then a kill."""
    if not signal_group(group, signal.SIGTERM):
        return
    for _ in range(int(EXIT_SECONDS / CHECK_SECONDS)):
        await asyncio.sleep(CHECK_SECONDS)
        if not signal_group(group, 0):
            return
    signal_group(group, signal.SIGKILL)


async def end(
    process: asyncio.subprocess.Process,
    policy: StopPolicy,
    *,
    close_input: bool,
    input_seconds: float = EXIT_SECONDS,
) -> None:
    """End a launched harness. With ``close_input`` its standard input is closed
    first, which asks a harness such as Pi for an orderly exit within
    ``input_seconds``. Then the launcher is signalled, as ``signal_launch`` does, and
    killed if it does not exit, and under ``KILL_ALL`` the rest of its group too.
    Every wait is bounded. A launcher that survives its kill is reported on the
    daemon's standard error and left, since nothing more can be done to it."""
    if close_input and process.stdin is not None and not process.stdin.is_closing():
        process.stdin.close()
        if process.returncode is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=input_seconds)
    if process.returncode is None:
        signal_launch(process.pid, signal.SIGTERM, policy)
        try:
            await asyncio.wait_for(process.wait(), timeout=LAUNCHER_EXIT_SECONDS)
        except TimeoutError:
            signal_launch(process.pid, signal.SIGKILL, policy)
            try:
                await asyncio.wait_for(process.wait(), timeout=KILLED_EXIT_SECONDS)
            except TimeoutError:
                print(
                    f"dromond: launcher {process.pid} did not exit after SIGKILL",
                    file=sys.stderr,
                )
                return
    if policy is StopPolicy.KILL_ALL:
        await sweep_group(process.pid)


@dataclass(frozen=True, slots=True)
class LeftReport:
    """What one launcher left running, as the daemon reads it back."""

    line_id: str
    cause: str
    processes: tuple[tuple[int, str], ...]
    """Each process left running, with its pid and command, at most ``MAX_LEFT``."""
    more: int = 0
    """How many more were left than are named."""
    listed: bool = True
    """Whether the launcher could read the process table at its stop. When it could
    not, what it ended or listed may not be all. **Review F2.**"""


def line_of(mark: object) -> str | None:
    """The line a process mark names, its text before the last dot, or ``None``."""
    if not isinstance(mark, str) or "." not in mark:
        return None
    line_id = mark.rsplit(".", 1)[0]
    return line_id or None


def parse_report(raw: bytes) -> LeftReport | None:
    """A report file's content, or ``None`` for one that is malformed. A report is
    data from a process the daemon started, so every field is checked."""
    if len(raw) > MAX_REPORT_BYTES:
        return None
    try:
        document: object = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    fields = cast(dict[str, object], document)
    line_id = line_of(fields.get("mark"))
    cause = fields.get("cause")
    entries = fields.get("processes")
    more = fields.get("more", 0)
    listed = fields.get("listed", True)
    if line_id is None or cause not in CAUSES or not isinstance(entries, list):
        return None
    if type(more) is not int or more < 0 or not isinstance(listed, bool):
        return None
    processes: list[tuple[int, str]] = []
    for item in cast(list[object], entries)[:MAX_LEFT]:
        if not isinstance(item, dict):
            return None
        entry = cast(dict[str, object], item)
        pid, command = entry.get("pid"), entry.get("command")
        if type(pid) is not int or pid <= 1 or not isinstance(command, str):
            return None
        processes.append((pid, command[:200]))
    return LeftReport(line_id, str(cause), tuple(processes), more, listed)


def collect_reports(directory: Path | None) -> list[LeftReport]:
    """Read the report files in ``directory``, at most ``MAX_REPORTS``, oldest name
    first. Each one read is kept under ``READ_SUFFIX``, as the only copy of the whole
    commands it names, and the oldest beyond ``MAX_KEPT_REPORTS`` are removed. A
    malformed one is removed and skipped, with a note on the daemon's standard error.
    A missing directory has none."""
    if directory is None:
        return []
    try:
        names = sorted(
            entry.name
            for entry in os.scandir(directory)
            if entry.name.endswith(REPORT_SUFFIX) and entry.is_file()
        )
    except OSError:
        return []
    reports: list[LeftReport] = []
    for name in names[:MAX_REPORTS]:
        path = directory / name
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_REPORT_BYTES + 1)
        except OSError:
            continue
        report = parse_report(raw)
        try:
            if report is None:
                path.unlink()
            else:
                path.replace(path.with_suffix(READ_SUFFIX))
        except OSError:
            # A report that cannot be moved aside would be read again, so it is
            # skipped this time and tried again at the next read.
            continue
        if report is None:
            print(f"dromond: a launcher report was malformed: {name}", file=sys.stderr)
            continue
        reports.append(report)
    if reports:
        prune_read(directory)
    return reports


def prune_read(directory: Path) -> None:
    """Remove the oldest read reports beyond ``MAX_KEPT_REPORTS``, by modification
    time. A file that cannot be listed or removed is left."""
    try:
        kept = [
            (entry.stat().st_mtime_ns, entry.name)
            for entry in os.scandir(directory)
            if entry.name.endswith(READ_SUFFIX) and entry.is_file()
        ]
    except OSError:
        return
    kept.sort()
    for _, name in kept[: max(0, len(kept) - MAX_KEPT_REPORTS)]:
        with contextlib.suppress(OSError):
            (directory / name).unlink()


@dataclass(frozen=True, slots=True)
class LaunchRecord:
    """A launcher's record of the harness it started, as the daemon reads it back.
    **Review N1.**"""

    path: Path
    line_id: str | None
    launcher: int
    harness: int
    """The harness's pid, which is also its group's identifier."""
    command: str
    """The harness's executable, by which the sweep checks that a process holding the
    harness's pid is still the harness."""
    policy: StopPolicy
    done: bool
    """Whether the launcher finished its stop."""
    launched_ns: int | None = None
    """When the launcher wrote the record, in nanoseconds of the wall clock, from the
    record's name, or ``None`` when the name does not carry it."""


def launched_at(path: Path) -> int | None:
    """The time in a launch record's name, ``<launcher pid>-<nanoseconds>.launch``,
    as ``main`` writes it, or ``None`` for another name."""
    _, _, stamp = path.name.removesuffix(LAUNCH_SUFFIX).partition("-")
    return int(stamp) if stamp.isascii() and stamp.isdigit() else None


def parse_launch(path: Path, raw: bytes) -> LaunchRecord | None:
    """A launch record's content, or ``None`` for one that is malformed. A record is
    data from a process the daemon started, so every field is checked."""
    if len(raw) > MAX_LAUNCH_BYTES:
        return None
    try:
        document: object = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    fields = cast(dict[str, object], document)
    launcher, harness = fields.get("launcher"), fields.get("harness")
    command, policy = fields.get("command"), fields.get("policy")
    done = fields.get("done")
    if type(launcher) is not int or type(harness) is not int:
        return None
    if launcher <= 1 or harness <= 1 or not isinstance(done, bool):
        return None
    if not isinstance(command, str) or not command:
        return None
    if policy not in {member.value for member in StopPolicy}:
        return None
    return LaunchRecord(
        path,
        line_of(fields.get("mark")),
        launcher,
        harness,
        command,
        StopPolicy(str(policy)),
        done,
        launched_at(path),
    )


def read_launches(directory: Path | None) -> list[LaunchRecord]:
    """The launch records in ``directory``, at most ``MAX_LAUNCHES``. A malformed one
    is removed, with a note on the daemon's standard error."""
    if directory is None:
        return []
    try:
        names = sorted(
            entry.name
            for entry in os.scandir(directory)
            if entry.name.endswith(LAUNCH_SUFFIX) and entry.is_file()
        )
    except OSError:
        return []
    records: list[LaunchRecord] = []
    for name in names[:MAX_LAUNCHES]:
        path = directory / name
        try:
            with path.open("rb") as handle:
                raw = handle.read(MAX_LAUNCH_BYTES + 1)
        except OSError:
            continue
        record = parse_launch(path, raw)
        if record is None:
            with contextlib.suppress(OSError):
                path.unlink()
            print(f"dromond: a launch record was malformed: {name}", file=sys.stderr)
            continue
        records.append(record)
    return records


@dataclass(frozen=True, slots=True)
class Swept:
    """What the daemon's sweep did for one launch record, once its launcher had
    gone. **Review N1.**"""

    line_id: str | None
    policy: StopPolicy
    finished: bool
    """Whether the launcher had finished its stop."""
    ended: tuple[int, ...]
    """The processes the sweep signalled to end: under ``KILL_ALL`` every one it
    found, and under ``LEAVE_BACKGROUND`` the harness alone, if its launcher did not
    finish."""
    left: tuple[TreeRow, ...]
    """What still ran after the sweep and is recorded: under ``KILL_ALL`` what
    survived it, and under ``LEAVE_BACKGROUND`` what an unfinished launcher could
    not report."""
    skipped: str | None = None
    """Why the sweep touched nothing, when the harness's pid is no longer the
    harness's."""
    launched_ns: int | None = field(default=None, compare=False)
    """The record's ``launched_ns``, by which the daemon tells whether the launcher
    was its line's present engine's."""


DEFUNCT: Final = "<defunct>"
"""What ``ps`` shows in place of the command of a process that has exited and has not
been reaped, on macOS and on Linux alike."""


def launcher_running(
    rows: Sequence[TreeRow], pid: int, harness: int | None = None, command: str = ""
) -> bool:
    """Whether ``rows`` hold a launcher under ``pid`` that still holds its harness. A
    pid reused by any other process is not one, nor a launcher that has exited and
    not been reaped, nor one whose ``harness``, recognised by ``command``, now has
    another parent, since a running launcher stays its harness's parent until it
    reaps it. **Third live run of N1, 2026-10-26.**"""
    row = next((row for row in rows if row.pid == pid), None)
    if row is None or DEFUNCT in row.command:
        return False
    if not any(name in row.command for name in LAUNCHER_NAMES):
        return False
    held = next((row for row in rows if row.pid == harness), None)
    if held is None or command not in held.command:
        return True
    return held.ppid == pid


def _answers(pid: int) -> bool:
    """Whether ``pid`` is a process of the daemon's user. Another user's process, as a
    reused pid can be, is not a launcher the daemon started."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def read_rows() -> list[TreeRow] | str:
    """The daemon's user's processes, or why they cannot be read."""
    return tree_or_reason(os.getuid())


async def sweep_launches(
    directory: Path | None,
    read: Callable[[], list[TreeRow] | str] = read_rows,
) -> list[Swept] | str:
    """Sweep the harness's group of every launch record whose launcher has exited,
    then remove the record, and say what each sweep did. Returns why the process
    table could not be read instead, in which case nothing is touched and every
    record waits for the next sweep, since the sweep must not signal a group it
    cannot first check.

    Under ``KILL_ALL`` the group and the harness's descendants are sent ``SIGTERM``
    and ``SIGCONT``, which wakes a group its launcher left frozen, and after
    ``EXIT_SECONDS`` ``SIGKILL``. Under ``LEAVE_BACKGROUND`` a harness whose launcher
    did not finish is killed, as its launcher would have, and its group is sent
    ``SIGCONT``. A record whose harness's pid now runs another command, or is
    claimed by a record whose launcher still runs, is a reused pid, and its group is
    not touched.

    The table is read whenever some record is unfinished, or its launcher no longer
    answers a signal, so a sweep costs one ``ps`` while any engine runs. A launcher
    that answers a signal is not taken as running, because a killed launcher the
    daemon started still answers one until it is reaped. **Third live run of N1,
    2026-10-26.** A kept connection's launcher killed with ``SIGKILL`` still answered
    for over 80 seconds, and its record was never swept while the daemon ran. That it
    stayed unreaped is an inference, which a probe of the Agent SDK's process start
    did not reproduce, since there the child was reaped at once. ``launcher_running``
    therefore also takes a launcher that is ``<defunct>``, or whose harness has
    another parent, as exited."""
    records = read_launches(directory)
    suspect = [
        record for record in records if not record.done or not _answers(record.launcher)
    ]
    if not suspect:
        return []
    rows = read()
    if isinstance(rows, str):
        return rows
    running = [
        record
        for record in records
        if record not in suspect
        or launcher_running(rows, record.launcher, record.harness, record.command)
    ]
    claimed = {record.harness for record in running}
    swept: list[Swept] = []
    for record in records:
        if record in running:
            continue
        swept.append(await _sweep_one(record, rows, record.harness in claimed))
        with contextlib.suppress(OSError):
            record.path.unlink()
    return swept


async def _sweep_one(
    record: LaunchRecord, rows: Sequence[TreeRow], claimed: bool
) -> Swept:
    harness = record.harness
    leader = next((row for row in rows if row.pid == harness), None)
    skipped = None
    if claimed:
        skipped = "another launch's harness now holds its pid"
    elif leader is not None and record.command not in leader.command:
        skipped = "another process now holds the harness's pid"
    found = {row.pid: row for row in rows if row.pgid == harness}
    if leader is not None:
        found.update({row.pid: row for row in descendants(rows, harness)})
        found[harness] = leader
    if skipped is not None or not found:
        return Swept(
            record.line_id,
            record.policy,
            record.done,
            (),
            (),
            skipped,
            record.launched_ns,
        )
    if record.policy is StopPolicy.KILL_ALL:
        return await _end_found(record, found)
    ended: tuple[int, ...] = ()
    if not record.done and leader is not None:
        _send(harness, signal.SIGKILL)
        ended = (harness,)
    signal_group(harness, signal.SIGCONT)
    left = (
        ()
        if record.done
        else tuple(
            row for pid, row in sorted(found.items()) if pid != harness and _alive(pid)
        )
    )
    return Swept(
        record.line_id,
        record.policy,
        record.done,
        ended,
        left,
        launched_ns=record.launched_ns,
    )


async def _end_found(record: LaunchRecord, found: Mapping[int, TreeRow]) -> Swept:
    """``KILL_ALL``: end the group and what else was found, within a bound."""
    harness = record.harness
    others = sorted(pid for pid, row in found.items() if row.pgid != harness)
    signal_group(harness, signal.SIGTERM)
    signal_group(harness, signal.SIGCONT)
    for pid in others:
        _send(pid, signal.SIGTERM)
        _send(pid, signal.SIGCONT)
    for _ in range(int(EXIT_SECONDS / CHECK_SECONDS)):
        if not signal_group(harness, 0) and not any(_alive(pid) for pid in others):
            break
        await asyncio.sleep(CHECK_SECONDS)
    signal_group(harness, signal.SIGKILL)
    for pid in others:
        if _alive(pid):
            _send(pid, signal.SIGKILL)
    await asyncio.sleep(CHECK_SECONDS)
    left = tuple(row for pid, row in sorted(found.items()) if _alive(pid))
    return Swept(
        record.line_id,
        record.policy,
        record.done,
        tuple(sorted(found)),
        left,
        launched_ns=record.launched_ns,
    )


# The launcher's side.


def become_subreaper() -> bool:
    """On Linux, make the launcher the reaper of its orphaned descendants, so a
    process whose parent exited is given to the launcher, and the walk still reaches
    it. Returns whether it is one. Elsewhere no such call exists. **Review F1.**"""
    if sys.platform != "linux":
        return False
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        done = libc.prctl(
            ctypes.c_int(PR_SET_CHILD_SUBREAPER),
            ctypes.c_ulong(1),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
        )
    except (OSError, AttributeError):
        return False
    return done == 0


def death_signal(policy: StopPolicy) -> signal.Signals:
    """The signal the harness receives on Linux when its launcher dies. ``SIGTERM``
    under ``KILL_ALL`` asks for the harness's orderly exit, which ends what it
    started. ``SIGKILL`` under ``LEAVE_BACKGROUND`` ends the harness alone, as the
    launcher would have."""
    return signal.SIGTERM if policy is StopPolicy.KILL_ALL else signal.SIGKILL


def arm_death_signal(policy: StopPolicy) -> Callable[[], None] | None:
    """On Linux, what the harness's child process runs before the harness starts,
    which sets its parent death signal, as ``death_signal`` says, and sends it at
    once if the launcher already died. ``None`` elsewhere, or where ``prctl`` cannot
    be found. **Review N1.** The C library is loaded here, before the fork, so the
    child calls a function it already holds."""
    if sys.platform != "linux":
        return None
    try:
        prctl = ctypes.CDLL(None, use_errno=True).prctl
    except (OSError, AttributeError):
        return None
    number = death_signal(policy)
    parent = os.getpid()

    def arm() -> None:
        prctl(
            ctypes.c_int(PR_SET_PDEATHSIG),
            ctypes.c_ulong(number),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
            ctypes.c_ulong(0),
        )
        if os.getppid() != parent:
            os.kill(os.getpid(), number)

    return arm


if sys.platform == "linux":

    class ExitWatch:
        """Whether the harness has exited, seen without reaping it, so that its pid
        and its group's identifier stay its own until the launcher reaps it last.
        Linux answers with ``waitid`` and ``WNOWAIT``. **Review F4.** Once seen, an
        exit stays seen."""

        def __init__(self, pid: int) -> None:
            self._pid = pid
            self._exited = False

        def exited(self) -> bool:
            if not self._exited:
                try:
                    found = os.waitid(
                        os.P_PID, self._pid, os.WEXITED | os.WNOHANG | os.WNOWAIT
                    )
                except ChildProcessError:
                    found = True
                self._exited = found is not None
            return self._exited

        def close(self) -> None:
            return

else:

    class ExitWatch:
        """Whether the harness has exited, seen without reaping it, so that its pid
        and its group's identifier stay its own until the launcher reaps it last.
        macOS has no ``waitid`` in Python, and answers through ``kqueue``. **Review
        F4.** Once seen, an exit stays seen."""

        def __init__(self, pid: int) -> None:
            self._exited = False
            self._queue = select.kqueue()
            event = select.kevent(
                pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                self._queue.control([event], 0, 0)
            except ProcessLookupError:
                # The kernel refuses to watch a process that has already exited.
                self._exited = True

        def exited(self) -> bool:
            if not self._exited and self._queue.control(None, 1, 0):
                self._exited = True
            return self._exited

        def close(self) -> None:
            self._queue.close()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _send(pid: int, number: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, number)


def _harness_tree(me: int, harness: int) -> list[TreeRow] | None:
    """The harness's descendants by parent and by group, found from the launcher,
    which on Linux includes the orphans it adopted, and the members of the harness's
    own group, without the launcher and the harness. ``None`` when the process table
    cannot be read, which is never an empty tree."""
    deadline = time.monotonic() + WALK_DEADLINE_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        rows = tree_or_reason(os.getuid(), min(WALK_SECONDS, remaining))
        if not isinstance(rows, str):
            break
        if not rows.startswith("ps did not answer within "):
            return None
    found = {row.pid: row for row in descendants(rows, me)}
    found.update({row.pid: row for row in rows if row.pgid == harness})
    found.pop(me, None)
    found.pop(harness, None)
    return [found[pid] for pid in sorted(found)]


def _reap_adopted(pids: set[int]) -> set[int]:
    """Reap those of ``pids`` that are the launcher's own children and have exited,
    the orphans a subreaper adopted, and return them, since a reaped pid may be
    reused and must not be signalled again. The harness is never among ``pids``."""
    reaped: set[int] = set()
    for pid in sorted(pids):
        try:
            done, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue
        if done == pid:
            reaped.add(pid)
    return reaped


def _wait_gone(exits: ExitWatch, pids: set[int]) -> set[int]:
    """Wait up to ``EXIT_SECONDS`` for the harness and ``pids`` to end, and return
    those of ``pids`` not reaped here."""
    tracked = set(pids)
    for _ in range(int(EXIT_SECONDS / CHECK_SECONDS)):
        tracked -= _reap_adopted(tracked)
        if exits.exited() and not any(_alive(pid) for pid in tracked):
            break
        time.sleep(CHECK_SECONDS)
    return tracked


def end_tree(me: int, harness: int, exits: ExitWatch) -> bool:
    """``KILL_ALL``: freeze the harness's group, walk the tree, terminate the group
    and every descendant found, then kill what remains. Returns whether both walks
    could read the process table. The harness is unreaped throughout, so its group's
    identifier cannot have been reused. **Review F4 and F5.**"""
    # The first act is a signal to the whole group, so that nothing in it exits, and
    # so orphans a child, or starts another, while the table is read.
    signal_group(harness, signal.SIGSTOP)
    before = _harness_tree(me, harness)
    found = {row.pid for row in before or ()}
    signal_group(harness, signal.SIGTERM)
    for pid in sorted(found):
        _send(pid, signal.SIGTERM)
    signal_group(harness, signal.SIGCONT)
    for pid in sorted(found):
        _send(pid, signal.SIGCONT)
    tracked = _wait_gone(exits, found)
    after = _harness_tree(me, harness)
    reaped = found - tracked
    remaining = tracked | ({row.pid for row in after or ()} - reaped)
    signal_group(harness, signal.SIGKILL)
    for pid in sorted(remaining):
        if _alive(pid):
            _send(pid, signal.SIGKILL)
    return before is not None and after is not None


def end_harness(
    me: int, harness: int, exits: ExitWatch, grace: float = 0.0
) -> tuple[list[TreeRow], bool]:
    """``LEAVE_BACKGROUND``: let the harness run on for ``grace`` seconds, then freeze
    it alone, list its descendants, and kill it. Returns the descendants still
    running, by the walk before and one after, and whether both walks could read the
    table. The harness is killed, not asked to stop, since a harness asked to stop may
    end what it started, as the module's account of Claude Code says, which is what
    this policy is chosen to prevent. The grace is its chance to write out what it
    queued, with its input still open. **Review N2.**"""
    for _ in range(int(grace / CHECK_SECONDS)):
        if exits.exited():
            break
        time.sleep(CHECK_SECONDS)
    if not exits.exited():
        _send(harness, signal.SIGSTOP)
    before = _harness_tree(me, harness)
    if not exits.exited():
        _send(harness, signal.SIGKILL)
    for _ in range(int(EXIT_SECONDS / CHECK_SECONDS)):
        if exits.exited():
            break
        time.sleep(CHECK_SECONDS)
    after = _harness_tree(me, harness)
    rows = {row.pid: row for row in before or ()}
    rows.update({row.pid: row for row in after or ()})
    left = [row for pid, row in sorted(rows.items()) if _alive(pid)]
    return left, before is not None and after is not None


def _write_private(target: Path, document: Mapping[str, object]) -> Path | None:
    """Write ``document`` to ``target``, readable by its owner only, under a
    temporary name first so that the daemon never reads half of one, and replace
    whatever ``target`` held."""
    partial = target.with_name(f"{target.name}.part")
    try:
        descriptor = os.open(
            partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        partial.replace(target)
    except OSError as error:
        print(f"dromon-launch: cannot write {target.name}: {error}", file=sys.stderr)
        return None
    return target


def write_report(
    directory: str,
    mark: str | None,
    cause: str,
    left: Sequence[TreeRow],
    listed: bool = True,
) -> Path | None:
    """Write what was left to a new file in ``directory``, and whether the process
    table could be read, which a report with no processes may say alone. **Review
    F2.**"""
    document = {
        "v": 1,
        "mark": mark,
        "cause": cause,
        "processes": [
            {"pid": row.pid, "command": row.command} for row in left[:MAX_LEFT]
        ],
        "more": max(0, len(left) - MAX_LEFT),
        "listed": listed,
    }
    target = Path(directory) / f"{os.getpid()}-{time.time_ns()}{REPORT_SUFFIX}"
    return _write_private(target, document)


def write_launch(
    target: Path,
    mark: str | None,
    harness: int,
    command: str,
    policy: StopPolicy,
    done: bool,
) -> Path | None:
    """Write or rewrite the launcher's record of its harness, as the module's account
    of review N1 says."""
    document = {
        "v": 1,
        "mark": mark,
        "launcher": os.getpid(),
        "harness": harness,
        "command": command,
        "policy": policy.value,
        "done": done,
    }
    return _write_private(target, document)


def relayable(descriptor: int) -> bool:
    """Whether ``descriptor`` is a pipe or a socket, whose end means the daemon closed
    the harness's input. A harness started with its input on the null device, or on
    a file, has no input to close, and is not relayed, since that input ends at once."""
    try:
        mode = os.fstat(descriptor).st_mode
    except OSError:
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)


class InputRelay:
    """``LEAVE_BACKGROUND``: copies the daemon's input to the harness's in a thread of
    its own, and never closes the harness's side, so the harness cannot take the
    daemon's close as its cue to end what it started. The end of the daemon's input
    sets ``closed`` and wakes the launcher, which then stops the harness itself.

    **Review N3.** The harness's side is written without blocking, and what the harness
    has not yet read is held, up to ``RELAY_BACKLOG``, so a harness that stops reading
    does not keep the relay from seeing the daemon's close. Past that bound the relay
    reads no more, and so sees no close, until the harness reads again. The parent
    check and the stop signals still reach the launcher then. What is held when the
    daemon's side closes is still written, while the harness lives."""

    def __init__(self, source: int, wake: int) -> None:
        self.source = source
        self.wake = wake
        self.harness_input, self._sink = os.pipe()
        self.closed = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        """Start copying, once the harness holds its side, which the launcher then
        lets go of."""
        os.close(self.harness_input)
        os.set_blocking(self._sink, False)
        self._thread.start()

    def _run(self) -> None:
        # Bounded by the daemon's input, which ends at its close or its death, and
        # then by the backlog, which the harness reads or which ends with it.
        backlog = bytearray()
        open_ = True
        while open_ or backlog:
            reading = [self.source] if open_ and len(backlog) < RELAY_BACKLOG else []
            writing = [self._sink] if backlog else []
            try:
                readable, writable, _ = select.select(reading, writing, [])
            except OSError:
                return
            if writable and not self._write(backlog):
                # The harness is gone, which the exit watch sees by itself.
                return
            if readable:
                try:
                    chunk = os.read(self.source, RELAY_CHUNK)
                except OSError:
                    chunk = b""
                if chunk:
                    backlog += chunk
                else:
                    open_ = False
                    self._close_seen()

    def _write(self, backlog: bytearray) -> bool:
        """Write what the harness's side takes now, and say whether it still can."""
        try:
            written = os.write(self._sink, backlog)
        except BlockingIOError:
            return True
        except OSError:
            return False
        del backlog[:written]
        return True

    def _close_seen(self) -> None:
        self.closed.set()
        with contextlib.suppress(OSError):
            os.write(self.wake, b"\0")


def watch(
    exits: ExitWatch,
    parent: int,
    wakeup: int,
    stopping: list[int],
    closed: threading.Event | None = None,
) -> str:
    """Wait until the harness exits, a stop signal arrives, the relayed input closes
    or the parent dies, and say which. Each pass waits at most ``POLL_SECONDS``. The
    loop has no bound of its own, since the launcher lives as long as its harness,
    and each of these ends it."""
    while True:
        if exits.exited():
            return EXITED
        if stopping or (closed is not None and closed.is_set()):
            return STOPPED
        if os.getppid() != parent:
            return ORPHANED
        readable, _, _ = select.select([wakeup], [], [], POLL_SECONDS)
        if readable:
            with contextlib.suppress(BlockingIOError, InterruptedError):
                os.read(wakeup, 512)


def reap(child: subprocess.Popen[bytes]) -> int:
    """Reap the harness, the launcher's last act on it, and return the status the
    launcher exits with. A death by signal ``n`` is ``128 + n``, as a shell gives
    it, since the launcher cannot die by the same signal and still report."""
    try:
        child.wait(timeout=REAP_SECONDS)
    except subprocess.TimeoutExpired:
        return 1
    code = child.returncode
    return 128 - code if code < 0 else code


def main(argv: Sequence[str] | None = None) -> int:
    """Run as the launcher. The harness is ``DROMON_LAUNCH_TARGET`` with ``argv``,
    or the command line's arguments."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    target = os.environ.get(TARGET_VARIABLE)
    if not target:
        version = version_target(arguments)
        if version is None:
            print(f"dromon-launch: {TARGET_VARIABLE} is not set", file=sys.stderr)
            return 127
        # A version query starts nothing that lasts, so it needs no watcher.
        try:
            os.execve(  # noqa: S606
                version,
                [version, *arguments],
                version_environment(os.environ),
            )
        except OSError as error:
            print(f"dromon-launch: cannot start {version}: {error}", file=sys.stderr)
            return 127
    if os.environ.get(FILTER_ENVIRONMENT_VARIABLE) == "1":
        # The Agent SDK merges its host environment into options.env. Replace this
        # launcher image with one carrying only the inputs its watcher and Claude
        # Code need, before it creates any child process.
        environment = allowlisted(
            os.environ,
            (
                *BASE_ENVIRONMENT,
                *CLAUDE_ENGINE_ENVIRONMENT,
                MARK_VARIABLE,
                *LAUNCH_VARIABLES,
                *(relayed(name) for name in RELAYED_VARIABLES),
            ),
        )
        environment.pop(FILTER_ENVIRONMENT_VARIABLE, None)
        unrelayed(environment)
        try:
            os.execve(  # noqa: S606
                sys.executable,
                [sys.executable, "-I", "-m", "dromon.launcher", *arguments],
                environment,
            )
        except OSError as error:
            print(
                f"dromon-launch: cannot re-exec with the filtered environment: {error}",
                file=sys.stderr,
            )
            return 127
    policy = policy_of(os.environ)
    parent_text = os.environ.get(PARENT_VARIABLE, "")
    parent = int(parent_text) if parent_text.isdigit() else os.getppid()
    report = os.environ.get(REPORT_VARIABLE) or None
    mark = os.environ.get(LAUNCH_MARK_VARIABLE) or os.environ.get(MARK_VARIABLE)
    environment = {k: v for k, v in os.environ.items() if k not in LAUNCH_VARIABLES}
    with contextlib.suppress(OSError):
        # Fails when the launcher already leads a session, as when the daemon
        # started it with a session of its own, which serves as well.
        os.setsid()
    if sys.platform == "linux" and not become_subreaper():
        print("dromon-launch: cannot become a child subreaper", file=sys.stderr)
    me = os.getpid()
    wakeup, notify = os.pipe()
    os.set_blocking(wakeup, False)
    os.set_blocking(notify, False)
    stopping: list[int] = []
    signal.set_wakeup_fd(notify)
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, lambda n, _: stopping.append(n))
    signal.signal(signal.SIGCHLD, lambda n, _: None)
    relay = (
        InputRelay(os.dup(0), notify)
        if policy is StopPolicy.LEAVE_BACKGROUND and relayable(0)
        else None
    )
    try:
        # The harness leads a group of its own, apart from the launcher's, so the
        # launcher can signal all of it, even with SIGKILL, and survive to report.
        # The relay's thread has not started, so the child is forked from a single
        # thread, as a function run before the harness starts requires.
        child = subprocess.Popen(  # noqa: S603
            [target, *arguments],
            env=environment,
            start_new_session=True,
            stdin=None if relay is None else relay.harness_input,
            preexec_fn=arm_death_signal(policy),  # noqa: PLW1509
        )
    except OSError as error:
        print(f"dromon-launch: cannot start {target}: {error}", file=sys.stderr)
        return 127
    if relay is not None:
        relay.start()
    record = (
        None
        if report is None
        else write_launch(
            Path(report) / f"{me}-{time.time_ns()}{LAUNCH_SUFFIX}",
            mark,
            child.pid,
            target,
            policy,
            done=False,
        )
    )
    exits = ExitWatch(child.pid)
    # The harness holds the daemon's pipes, and the launcher lets go of its own
    # copies, so the daemon sees their ends exactly when the harness's side ends.
    null = os.open(os.devnull, os.O_RDWR)
    os.dup2(null, 0)
    os.dup2(null, 1)
    os.close(null)
    closed = None if relay is None else relay.closed
    cause = watch(exits, parent, wakeup, stopping, closed)
    stop_verified = True
    if policy is StopPolicy.KILL_ALL:
        stop_verified = end_tree(me, child.pid, exits)
        if not stop_verified:
            print(
                "dromon-launch: the process table could not be read, so stopping"
                " the harness tree is unverified",
                file=sys.stderr,
            )
            if report is not None:
                write_report(report, mark, cause, [], listed=False)
    else:
        grace = 0.0 if cause == EXITED else FLUSH_SECONDS
        left, listed = end_harness(me, child.pid, exits, grace)
        _report_left(report, mark, cause, left, listed)
    exits.close()
    if record is not None:
        write_launch(record, mark, child.pid, target, policy, done=True)
    status = reap(child)
    return 1 if policy is StopPolicy.KILL_ALL and not stop_verified else status


def _report_left(
    report: str | None,
    mark: str | None,
    cause: str,
    left: Sequence[TreeRow],
    listed: bool,
) -> None:
    """Say what ``LEAVE_BACKGROUND`` left running, on standard error and to the
    daemon's report directory, and whether the walk could be made."""
    if not listed:
        print(
            "dromon-launch: the process table could not be read, so what the"
            " harness left running may not all be listed",
            file=sys.stderr,
        )
    if not left and listed:
        return
    where = None if report is None else write_report(report, mark, cause, left, listed)
    if left:
        pids = ", ".join(str(row.pid) for row in left[:MAX_LEFT])
        print(
            f"dromon-launch: left {len(left)} processes running by the stop policy"
            f" ({pids}), reported to {where or 'no report file'}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    sys.exit(main())
