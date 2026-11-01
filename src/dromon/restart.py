"""Restart to upgrade: stop the daemon and start it again from the installed code.

**Decided by the operator on 2026-10-27.** It is always possible to close and reopen
the harness so that every line's session migrates to the latest installed version.
``docs/protocol/RESTART.md`` specifies the behaviour, and in short it is this.

- A restart first drains. From the request on, no turn starts and no scheduled wake
  fires, so mail and wakes wait for the restarted daemon in the event log.
- ``wait`` lets each running turn end by itself. ``interrupt`` also asks each running
  turn to end at its next safe point, the same point that priority mail uses, as
  ``dromon.priority`` describes, so the engine reports the turn and keeps its session.
  A line whose turn the restart ended receives a ``restart_noted`` note, which wakes it
  once the restarted daemon runs.
- Both are bounded by the request's wait. A turn still running at the bound is one
  whose interrupt could not be honoured, such as a turn in one long tool call, and the
  restart is then abandoned, so no turn is cut part way. The daemon starts turns again,
  and ``dromon stop`` stays the way to end turns at once.
- A restart also refuses, before it drains and again before it stops, while kept engine
  sessions run background tasks it would stop, unless the request accepts that.
- Once no turn runs, the daemon stops as ``stop`` does, then starts its successor with
  the same command-line options, after it has released its lock and its sockets. The
  successor reads the event log, so sessions, hibernations, scheduled wakes and mail
  survive.

Porting note. The plan is plain data with a deadline on a monotonic clock, and the
watch is a poll bounded by a count derived from the wait, so both map onto a state
machine in a port.
"""

import math
import shutil
import stat
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from dromon.gitssh import AGENT_VARIABLE
from dromon.registry import REGISTRY_DIR_ENV

START_ENVIRONMENT: Final = (REGISTRY_DIR_ENV, AGENT_VARIABLE)
"""The environment variables a start's checks read, which a restart's successor
inherits from the running daemon. **Round 14, item 6.** ``dromon restart``'s
preflight runs with the daemon's values of them rather than the client's, so a
refusal that depends on either is found before the stop. **Assumption.** No other
variable changes what a start's checks decide. ``HOME`` moves the default registry,
but a client and its daemon run as one user, and ``DROMON_RUNTIME_DIR`` is replaced
by the ``--runtime-dir`` the preflight passes."""

CHECK_PREFIX: Final = "preflight-"
"""The prefix of the private directory a preflight copies the event log into, under
the runtime directory."""

CHECK_SECONDS: Final = 120.0
"""How long a preflight may take before ``dromon restart`` ends it. It replays the
whole event log, as a start does. A preflight directory older than this is left over
from a check that was killed."""

MAX_SWEPT: Final = 1000
"""The most runtime-directory entries one sweep looks at, so it is bounded."""


def sweep_checks(runtime_dir: Path, older_than_seconds: float | None) -> int:
    """Remove the preflight directories left in ``runtime_dir``, and say how many.

    **Decided by the operator on 2026-10-31, after audit's finding P2.** A check
    removes its own directory as it ends, but a check that is killed, as at the
    client's timeout, cannot, and its copy holds message bodies. A sweep therefore
    runs when a client's check times out, at each check, and at each start. A check
    sweeps only directories older than ``older_than_seconds``, so two clients
    checking at once do not remove each other's copy, and ``None`` sweeps them all,
    as a start holding the lock, or a client whose own check it has just killed,
    does. Only directories are removed, never a link, and a directory that cannot be
    removed is left for the next sweep."""
    now = time.time()
    swept = 0
    try:
        entries = sorted(runtime_dir.iterdir())[:MAX_SWEPT]
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(CHECK_PREFIX):
            continue
        try:
            found = entry.lstat()
        except OSError:
            continue
        if not stat.S_ISDIR(found.st_mode):
            continue
        if older_than_seconds is not None and now - found.st_mtime < older_than_seconds:
            continue
        shutil.rmtree(entry, ignore_errors=True)
        swept += 0 if entry.exists() else 1
    return swept


DEFAULT_WAIT_SECONDS: Final = 600
"""How long a restart waits for running turns to end, unless the request says. Whole
seconds, because the client protocol carries no floats."""

MAX_WAIT_SECONDS: Final = 3600
"""The longest wait a restart request may ask for."""

POLL_SECONDS: Final = 0.1
"""How often the daemon checks whether a draining restart may stop."""

REASON: Final = "a daemon restart"
"""Why a turn ended for a restart, as its report's ``interrupted_for`` says."""

NOTE: Final = (
    "Your last turn ended at a safe point for a daemon restart, which operator"
    " {operator} asked for so that the daemon runs its newly installed code. Your"
    " engine session resumes in this turn. Continue the work where it stopped."
)
"""The ``restart_noted`` body that wakes a line whose turn a restart ended."""

ABANDONED_NOTE: Final = (
    "Your last turn ended at a safe point for a daemon restart, which operator"
    " {operator} asked for and which was then abandoned, so the daemon did not"
    " restart. Your engine session resumes in this turn. Continue the work where it"
    " stopped."
)
"""The ``restart_noted`` body when the restart was abandoned as the turn ended."""


class Mode(StrEnum):
    """How a restart treats the turns running when it is asked for."""

    WAIT = "wait"
    INTERRUPT = "interrupt"


class Outcome(StrEnum):
    """Where a restart stands, as the daemon's watch reads it."""

    WAITING = "waiting"
    READY = "ready"
    ABANDONED = "abandoned"


class RestartRefused(ValueError):
    """A restart that may not begin or continue, with the reason."""


@dataclass(frozen=True, slots=True)
class Plan:
    """A restart in progress. ``deadline`` is on the scheduler's monotonic clock."""

    mode: Mode
    operator: str
    wait_seconds: int
    deadline: float
    end_tasks: bool


def check_wait(value: object) -> int:
    """``value`` as a restart's wait in whole seconds, from 1 to ``MAX_WAIT_SECONDS``.
    Raises ``RestartRefused`` for anything else, including a boolean, which Python
    would otherwise count as a number. The protocol decoder already refuses a float,
    and this refuses one passed in directly."""
    if type(value) is not int:
        raise RestartRefused("wait_seconds is not a whole number")
    if not 0 < value <= MAX_WAIT_SECONDS:
        raise RestartRefused(f"wait_seconds must be from 1 to {MAX_WAIT_SECONDS}")
    return value


def polls(wait_seconds: int) -> int:
    """How many polls the daemon's watch makes at most, enough to pass the deadline
    of a plan with ``wait_seconds``, so its loop is bounded."""
    return math.ceil(wait_seconds / POLL_SECONDS) + 2
