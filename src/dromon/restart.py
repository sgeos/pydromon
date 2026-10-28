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
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

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
