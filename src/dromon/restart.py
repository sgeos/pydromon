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
  once the restarted daemon runs. **Round 16, item 3, and its review's finding 1.**
  The note follows the reason the turn's report names, so a turn whose report names
  another stop, or none, is not woken.
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

import hashlib
import hmac
import math
import re
import shutil
import stat
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

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

CHECK_SECONDS: Final = 1800.0
"""How long a preflight may take before ``dromon restart`` ends it. It replays the
whole event log and checks configured engine startups without prompts. A preflight
directory older than this is left over from a check that was killed."""

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


# Round 16, item 6. A restart keeps the running daemon's secrets.

NONCE_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
"""The key a client gives ``start_context`` for its digests, 32 bytes as exactly 64
lowercase hexadecimal digits. **Review of 16de365.** An earlier pattern allowed any
length from 32 to 128 digits, so an odd length passed it and then failed in
``bytes.fromhex``, which dropped the connection rather than refusing the request."""

DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}")
"""A client's digest of one value, an HMAC with SHA-256 in lowercase hexadecimal."""

SECRET_NAME_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
"""A name a comparison may carry, the form ``dromon.projectconfig`` accepts in a
line's ``secrets``, so a name printed in a warning cannot hold a control
character."""

MAX_COMPARED_SECRETS: Final = 256
"""The most names one comparison carries. The daemon lists at most this many granted
names, in name order, and refuses a request that carries more, so both the request
and its answer are bounded."""


class SecretVerdict(StrEnum):
    """The daemon's answer for one name a client compares. Nothing in it can be
    compared with a guess offline."""

    SAME = "same"
    DIFFERS = "differs"
    NOT_GRANTED = "not_granted"


def secret_digest(nonce: str, value: str | None) -> str | None:
    """A keyed digest of one variable's value, or ``None`` when it is unset.

    **Round 16, item 6, and the review of 16de365.** A restart's successor inherits
    the running daemon's environment, so a secret exported again in the operator's
    shell does not reach it. The client digests its own value under a key it chose
    for that one request and sends the digest, and the daemon digests its own value
    under the same key and answers only whether the two agree, as
    ``compare_secrets`` gives. HMAC with SHA-256 from the standard library, never a
    construction of our own. The first form had the daemon return its digests, which
    let whoever held the client socket test guesses offline without limit, since the
    holder chose the key. ``nonce`` must match ``NONCE_PATTERN``, and anything else
    raises ``ValueError``."""
    if NONCE_PATTERN.fullmatch(nonce) is None:
        raise ValueError("nonce must be 64 lowercase hexadecimal digits")
    if value is None:
        return None
    raw = value.encode("utf-8", "surrogateescape")
    return hmac.new(bytes.fromhex(nonce), raw, hashlib.sha256).hexdigest()


def secret_names(granted: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """Every name a line is granted, once each, in name order, cut to
    ``MAX_COMPARED_SECRETS``. Names, never values, and the project file already
    holds them."""
    found = {name for names in granted.values() for name in names}
    return tuple(sorted(found))[:MAX_COMPARED_SECRETS]


def secret_digests(
    names: Sequence[str], environment: Mapping[str, str], nonce: str
) -> dict[str, str | None]:
    """The digest of each of ``names`` as ``environment`` holds it, which the client
    sends."""
    return {name: secret_digest(nonce, environment.get(name)) for name in names}


def check_compared(found: object) -> dict[str, str | None]:
    """``found`` as a client's digests, each name in ``SECRET_NAME_PATTERN`` form with
    a digest in ``DIGEST_PATTERN`` form or ``None`` for an unset value, at most
    ``MAX_COMPARED_SECRETS`` of them. Raises ``ValueError`` for anything else, which
    the daemon answers as a bad request."""
    if not isinstance(found, dict):
        raise ValueError("secrets must be an object")
    entries = cast(dict[object, object], found)
    if len(entries) > MAX_COMPARED_SECRETS:
        raise ValueError(f"secrets may name at most {MAX_COMPARED_SECRETS} variables")
    compared: dict[str, str | None] = {}
    for name, digest in entries.items():
        if not isinstance(name, str) or SECRET_NAME_PATTERN.fullmatch(name) is None:
            raise ValueError("a name in secrets is not an environment variable name")
        if digest is not None and (
            not isinstance(digest, str) or DIGEST_PATTERN.fullmatch(digest) is None
        ):
            raise ValueError(f"the digest for {name} is not 64 hexadecimal digits")
        compared[name] = digest
    return compared


def compare_secrets(
    theirs: Mapping[str, str | None],
    granted: Mapping[str, Sequence[str]],
    environment: Mapping[str, str],
    nonce: str,
) -> dict[str, SecretVerdict]:
    """The daemon's verdict on each name a client compares. A name no line is granted
    is answered ``not_granted`` without its value being read, so a caller cannot
    probe the rest of the daemon's environment. A granted name is ``same`` when both
    sides are unset or both digests agree, compared in constant time, and ``differs``
    otherwise, so a name set on one side only differs too.

    **Residual.** Each request still tests one guess for each granted name, so a
    socket holder could guess online, one request at a time. That holder already has
    operator authority, as ``docs/protocol/CLIENT.md`` records, and no request rate is
    limited."""
    allowed = frozenset(name for names in granted.values() for name in names)
    verdicts: dict[str, SecretVerdict] = {}
    for name, digest in theirs.items():
        if name not in allowed:
            verdicts[name] = SecretVerdict.NOT_GRANTED
            continue
        mine = secret_digest(nonce, environment.get(name))
        agree = (
            mine is None and digest is None
            if mine is None or digest is None
            else hmac.compare_digest(mine, digest)
        )
        verdicts[name] = SecretVerdict.SAME if agree else SecretVerdict.DIFFERS
    return verdicts
