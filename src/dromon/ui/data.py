"""The daemon's results as typed values, read defensively.

The daemon is trusted, but a client may meet a daemon of another version, so each
reader takes what it understands, puts a neutral value where a field is missing or of
the wrong type, and never raises on content. Text fields carry untrusted message
bodies and engine output, which the client only ever shows.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from dromon.activity import ActivityKind


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, dict) else {}


def _items(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [_mapping(item) for item in cast(list[object], value)]


def _text(data: Mapping[str, object], key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _optional(data: Mapping[str, object], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) else None


def _number(data: Mapping[str, object], key: str) -> int:
    value = data.get(key)
    return value if type(value) is int else 0


@dataclass(frozen=True, slots=True)
class LineSummary:
    line_id: str
    model: str
    strategy: str
    state: str
    pending: int
    turns: int
    cost: str
    held_by: str | None

    @property
    def running(self) -> bool:
        return self.state == "running"


def line_summary(data: Mapping[str, object]) -> LineSummary:
    return LineSummary(
        line_id=_text(data, "line_id"),
        model=_text(data, "engine"),
        strategy=_text(data, "strategy"),
        state=_text(data, "state"),
        pending=_number(data, "pending"),
        turns=_number(data, "turns"),
        cost=_text(data, "cost_estimate_usd"),
        held_by=_optional(data, "held_by"),
    )


def lines_of(status: Mapping[str, object]) -> tuple[LineSummary, ...]:
    """The lines a ``status`` result lists, in its order."""
    return tuple(line_summary(entry) for entry in _items(status.get("lines")))


@dataclass(frozen=True, slots=True)
class Mail:
    message_id: str
    sender: str
    recipient: str
    kind: str
    reply_to: str | None
    status: str | None
    read: bool
    body: str
    truncated: bool


@dataclass(frozen=True, slots=True)
class Question:
    question_id: str
    operator: str
    text: str
    choices: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class View:
    line: LineSummary
    tick: int
    total: int
    mail: tuple[Mail, ...]
    questions: tuple[Question, ...]


def view_of(result: Mapping[str, object]) -> View:
    """A ``line_view`` result."""
    mail = tuple(
        Mail(
            message_id=_text(entry, "message_id"),
            sender=_text(entry, "sender"),
            recipient=_text(entry, "recipient"),
            kind=_text(entry, "kind"),
            reply_to=_optional(entry, "reply_to"),
            status=_optional(entry, "status"),
            read=entry.get("read") is True,
            body=_text(entry, "body"),
            truncated=entry.get("truncated") is True,
        )
        for entry in _items(result.get("messages"))
    )
    questions = tuple(
        Question(
            question_id=_text(entry, "question_id"),
            operator=_text(entry, "operator"),
            text=_text(entry, "text"),
            choices=tuple(
                choice
                for choice in cast(list[object], entry.get("choices") or [])
                if isinstance(choice, str)
            )
            if isinstance(entry.get("choices"), list)
            else (),
        )
        for entry in _items(result.get("open_questions"))
    )
    return View(
        line=line_summary(_mapping(result.get("line"))),
        tick=_number(result, "tick"),
        total=_number(result, "total"),
        mail=mail,
        questions=questions,
    )


@dataclass(frozen=True, slots=True)
class Entry:
    entry_id: int
    seq: int
    kind: ActivityKind
    text: str


@dataclass(frozen=True, slots=True)
class ActivityPage:
    seq: int
    entries: tuple[Entry, ...]
    complete: bool


def activity_of(result: Mapping[str, object]) -> ActivityPage:
    """A ``line_activity`` result. Entries of a kind this client does not know are
    left out."""
    known = {kind.value: kind for kind in ActivityKind}
    entries = tuple(
        Entry(_number(entry, "entry_id"), _number(entry, "seq"), known[kind], text)
        for entry in _items(result.get("entries"))
        if (kind := _text(entry, "kind")) in known
        and isinstance(text := entry.get("text"), str)
    )
    return ActivityPage(
        _number(result, "seq"), entries, result.get("complete") is not False
    )
