"""The shared machine's status, as the machine's monitor writes it, and what Dromon does
about it. **Round 16, item 3, at the admin session's suggestion.**

**Why.** One machine is shared by many sessions, and one of them once took most of its
memory without seeing that it did. A monitor outside every sandbox samples the machine
once a minute and writes a plain text file that any session may read, with a level of
``ok``, ``elevated`` or ``critical`` and the reasons for it. Dromon reads that file
before it starts a supervised run, a commit run or a line's turn, so a gate does not
start on a machine that cannot bear one, and a turn does not add to a machine that is
failing.

**What it reads.** The header of the file, the ``key=value`` lines before its first
blank line, and only ``level``, ``reasons`` and ``epoch``. Dromon never runs the
monitor's command and makes no network call, so a machine without the monitor needs
nothing. The reader is a pure function of the file's text and the time, which a port
carries over unchanged. **Porting note.** The file's path comes from the project file,
and the port reads the same text.

**Decisions.** At ``critical`` nothing starts, neither a run nor a turn. At
``elevated`` a run is deferred, and a turn proceeds with a warning in its prompt. A
status that is stale, missing or unreadable counts as ``elevated``, and says why. A
level raised only by free disk space does not defer a run, because a gate uses
processor and memory and not disk, so a run is deferred at ``critical`` by any other
reason, and a turn is deferred at ``critical`` whatever the reason. **Inference.** The
operator's wording lets a disk only ``critical`` either defer a turn or not, and the
stricter reading is taken, since a machine whose disk is nearly full is better left
alone by every session until the operator has seen the notice. It is one line to
change, in ``decide``.

**Failure modes.** A file that cannot be opened, is not a regular file, is larger than
``MAX_BYTES``, holds no ``level`` Dromon knows or no ``epoch`` it can read is
``unreadable`` and is handled as ``elevated``. A reason Dromon does not recognise is
not disk, so it defers a run, which is the safe direction. The file is the monitor's
own and may hold anything, so every reason is cut to a printable, bounded line before
it reaches a note. **Bounded read.** The scheduler makes the read on a worker thread, at
most once every ``dromon.scheduler.MACHINE_READ_SECONDS``, and waits for it at most
``dromon.scheduler.MACHINE_WAIT_SECONDS``, so a file on a mount that hangs holds the
worker and not the loop, and the status is ``unreadable`` until the read returns.
**Residual.** The stuck worker lasts as long as the mount does. The status describes
the machine as it was up to ``STALE_SECONDS`` ago, so a start the status allowed can
still land on a machine that has just failed. A defer holds only starts, and what
already runs is never touched, since only the operator decides to stop work.
"""

import os
import re
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

STALE_SECONDS: Final = 180
"""How old the monitor's sample may be before the status is stale, which is the age
the monitor's own reader uses, since its sampler runs once a minute."""

MAX_BYTES: Final = 64 * 1024
"""The most of the file that is read. The header is a few hundred bytes, and the
tables of the largest sessions follow it."""

MAX_REASONS: Final = 8
MAX_REASON_CHARS: Final = 160
"""How many reasons, and how many characters of each, are kept."""

KNOWN_LEVELS: Final = frozenset({"ok", "elevated", "critical"})


class Kind(StrEnum):
    """What a reason is about. ``INFO`` raises nothing, as ``could not read`` does."""

    MEMORY = "memory"
    SWAP = "swap"
    LOAD = "load"
    DISK = "disk"
    UNKNOWN = "unknown"
    INFO = "info"


@dataclass(frozen=True, slots=True)
class MachineStatus:
    """The status as read. ``level`` is ``ok``, ``elevated`` or ``critical`` for a
    fresh file, and ``stale``, ``missing`` or ``unreadable`` otherwise, with
    ``problem`` saying how."""

    level: str
    reasons: tuple[str, ...] = ()
    age_seconds: int | None = None
    problem: str | None = None

    @property
    def effective(self) -> str:
        """The level acted on. A status that cannot be relied on is ``elevated``."""
        return self.level if self.level in KNOWN_LEVELS else "elevated"

    def kinds(self) -> frozenset[Kind]:
        """What the reasons that raise the level are about. A level raised for a
        reason Dromon does not know, or for none, or one that cannot be relied on,
        is ``UNKNOWN``."""
        if self.level == "ok":
            return frozenset()
        if self.level not in KNOWN_LEVELS:
            return frozenset({Kind.UNKNOWN})
        found = {kind_of(reason) for reason in self.reasons} - {Kind.INFO}
        return frozenset(found) if found else frozenset({Kind.UNKNOWN})

    def describe(self) -> str:
        """One sentence naming the status, for a note or a refusal. Fixed words and
        the bounded reasons, and never the file's path."""
        if self.level == "missing":
            return (
                "the machine status file is missing, which is treated as elevated"
                f" ({self.problem})"
            )
        if self.level == "unreadable":
            return (
                "the machine status file cannot be relied on, which is treated as"
                f" elevated ({self.problem})"
            )
        if self.level == "stale":
            return (
                f"the machine status is stale, its last sample being {self.age_seconds}"
                " seconds old, which is treated as elevated"
            )
        if self.level == "ok":
            return f"the machine status is ok, as of {self.age_seconds} seconds ago"
        why = "; ".join(self.reasons) if self.reasons else "no reason given"
        return (
            f"the machine status is {self.level} ({why}), as of"
            f" {self.age_seconds} seconds ago"
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """What the status allows. ``warn`` says a turn that proceeds carries a warning."""

    defer_runs: bool = False
    defer_turns: bool = False
    warn: bool = False


NO_DECISION: Final = Decision()


def kind_of(reason: str) -> Kind:
    """What one reason of the monitor is about, by its fixed phrases. The swap
    reasons are tested before the disk ones, since one of them names both."""
    text = reason.casefold()
    if text.startswith("could not read"):
        return Kind.INFO
    if "memory pressure" in text:
        return Kind.MEMORY
    if "swap" in text:
        return Kind.SWAP
    if re.search(r"\bload\b", text):
        return Kind.LOAD
    if "data volume" in text or "disk" in text:
        return Kind.DISK
    return Kind.UNKNOWN


def decide(status: MachineStatus) -> Decision:
    """What ``status`` allows, as the module's text sets out."""
    level = status.effective
    if level == "ok":
        return NO_DECISION
    only_disk = status.kinds() == frozenset({Kind.DISK})
    return Decision(
        defer_runs=not only_disk,
        defer_turns=level == "critical",
        warn=level != "critical",
    )


def _printable(raw: str) -> str:
    """``raw`` without characters that are not printable, cut to a bounded line."""
    return "".join(c for c in raw if c.isprintable()).strip()[:MAX_REASON_CHARS]


def parse(
    text: str, now_epoch: float, stale_seconds: int = STALE_SECONDS
) -> MachineStatus:
    """The status in ``text``, read at ``now_epoch``, Unix seconds. The header is the
    lines before the first blank one, the first of each key counts, and a line without
    ``=`` is ignored."""
    header: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        key, separator, value = line.partition("=")
        if separator and key.strip() not in header:
            header[key.strip()] = value.strip()
    level = header.get("level", "")
    if level not in KNOWN_LEVELS:
        return MachineStatus("unreadable", problem="it names no level Dromon knows")
    try:
        epoch = int(header.get("epoch", ""))
    except ValueError:
        return MachineStatus("unreadable", problem="it names no sample time")
    age = int(now_epoch) - epoch
    if age < -stale_seconds:
        return MachineStatus("unreadable", problem="its sample time is in the future")
    age = max(age, 0)
    if age > stale_seconds:
        return MachineStatus("stale", age_seconds=age)
    raw = header.get("reasons", "")
    reasons = tuple(
        reason
        for part in raw.split(";")
        if (reason := _printable(part)) and reason != "none"
    )[:MAX_REASONS]
    return MachineStatus(level, reasons, age)


def read(
    path: Path, now_epoch: float, stale_seconds: int = STALE_SECONDS
) -> MachineStatus:
    """The status in the file at ``path``. A file that is absent is ``missing`` and
    one that cannot be read as a small regular file is ``unreadable``, and neither
    raises. Opened without blocking and checked to be a regular file first, so a
    named pipe cannot hold the caller."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return MachineStatus("missing", problem="no file is there")
    except OSError as error:
        return MachineStatus("unreadable", problem=error.strerror or "cannot open it")
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return MachineStatus("unreadable", problem="it is not a regular file")
        raw = os.read(descriptor, MAX_BYTES + 1)
    except OSError as error:
        return MachineStatus("unreadable", problem=error.strerror or "cannot read it")
    finally:
        os.close(descriptor)
    if len(raw) > MAX_BYTES:
        return MachineStatus("unreadable", problem="it is larger than expected")
    return parse(raw.decode("utf-8", "replace"), now_epoch, stale_seconds)
