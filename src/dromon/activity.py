"""What each line's engine is doing now, kept in memory for attached clients.

The event log records what happened to the mail. It does not record what an engine
says or which tools it calls during a turn, and it should not, because that output is
large, untrusted and of interest only while someone watches. This module keeps a short,
bounded tail of it per line, so that a client can show a turn as it runs, as Claude
Code, Codex and Grok Build show theirs.

Model. Each entry has an ``entry_id``, which never changes, and a ``seq``, which is
the daemon-wide revision at which the entry last changed. Only the newest entry of a
line can change, when a text chunk extends it, so a client that asks for entries with
``seq`` above the highest it has seen, and replaces entries by ``entry_id``, stays
exact without fetching the whole tail.

Bounds. A line keeps at most ``MAX_ENTRIES`` entries, the oldest dropped first, and an
entry holds at most ``MAX_TEXT`` characters, the rest replaced by a marker. Memory is
therefore bounded by the number of lines.

Trust. Entry text is engine output, which is untrusted. A client shows it as text and
never interprets it as a command.

Volatility. The tail is lost when the daemon stops. That is deliberate, and it is the
event log, not this tail, that a restart replays.

Porting note. The tail is a fixed-capacity ring per line in the Rust port.
"""

from collections import deque
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final, Protocol

MAX_ENTRIES: Final = 256
MAX_TEXT: Final = 4000
TRUNCATED: Final = " [truncated]"


class ActivityKind(StrEnum):
    TURN_STARTED = "turn_started"
    """A turn began. The text is the prompt the line received."""
    TEXT = "text"
    """Text the engine wrote. A streamed chunk extends the entry before it."""
    THINKING = "thinking"
    """The engine reasoned. The text is empty, because reasoning is not shown.
    Consecutive reasoning collapses into one entry."""
    TOOL = "tool"
    """The engine called a tool. The text is the tool's name."""
    TURN_ENDED = "turn_ended"
    """A turn ended. The text says how."""


@dataclass(frozen=True, slots=True)
class Activity:
    entry_id: int
    seq: int
    kind: ActivityKind
    text: str

    def to_json(self) -> dict[str, object]:
        return {
            "entry_id": self.entry_id,
            "seq": self.seq,
            "kind": self.kind.value,
            "text": self.text,
        }


class Observer(Protocol):
    """How an engine reports what it does during a turn. It must not raise.

    ``chunk`` marks text streamed in pieces, which extends the text entry before it.
    Text that arrives as whole messages is not a chunk, so that two messages in a row
    stay two entries.
    """

    def __call__(
        self, kind: ActivityKind, text: str, /, *, chunk: bool = False
    ) -> None: ...


def ignore(kind: ActivityKind, text: str, /, *, chunk: bool = False) -> None:
    """The observer of an engine that nobody watches."""
    del kind, text, chunk


def _bounded(text: str) -> str:
    if len(text) <= MAX_TEXT:
        return text
    return text[: MAX_TEXT - len(TRUNCATED)] + TRUNCATED


class ActivityLog:
    """The bounded tail of every line's activity. One per daemon, on its loop."""

    def __init__(self) -> None:
        self._lines: dict[str, deque[Activity]] = {}
        self._seq = 0
        self._next_id = 0

    @property
    def seq(self) -> int:
        """The highest revision of any entry, or zero before the first."""
        return self._seq

    def record(
        self, line_id: str, kind: ActivityKind, text: str, *, chunk: bool = False
    ) -> None:
        """Add an entry for ``line_id``, or extend its newest one.

        A text ``chunk`` extends a text entry, and reasoning extends a reasoning
        entry, because engines report both in many small pieces.
        """
        tail = self._lines.setdefault(line_id, deque(maxlen=MAX_ENTRIES))
        self._seq += 1
        continues = kind is ActivityKind.THINKING or (
            kind is ActivityKind.TEXT and chunk
        )
        if (
            continues
            and tail
            and tail[-1].kind is kind
            and not tail[-1].text.endswith(TRUNCATED)
        ):
            last = tail.pop()
            tail.append(replace(last, seq=self._seq, text=_bounded(last.text + text)))
            return
        self._next_id += 1
        tail.append(Activity(self._next_id, self._seq, kind, _bounded(text)))

    def observer(self, line_id: str) -> Observer:
        """An observer that records into ``line_id``'s tail."""

        def observe(kind: ActivityKind, text: str, /, *, chunk: bool = False) -> None:
            self.record(line_id, kind, text, chunk=chunk)

        return observe

    def after(self, line_id: str, seq: int) -> list[Activity]:
        """The entries of ``line_id`` changed after revision ``seq``, oldest first."""
        return [entry for entry in self._lines.get(line_id, ()) if entry.seq > seq]
