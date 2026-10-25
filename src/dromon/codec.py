"""Events as canonical JSON, the payload format of the event log.

An encoded event is the object ``{"data": {...}, "kind": "...", "v": 1}``. Keys are
sorted, there is no insignificant whitespace, and text is UTF-8 without ASCII escaping.
Encoding an event therefore always yields the same string, so a port can be checked
against this one byte for byte. ``docs/protocol/EVENT_LOG.md`` is normative.

Decoding treats its input as untrusted. It accepts exactly the documented keys and
types and nothing else, and every decoded event is validated by its constructor.

Porting hazards. Python's ``json`` module by default accepts duplicate keys, keeping the
last, and accepts ``NaN`` and ``Infinity``. The decoder rejects both, and rejects every
number except the version, which a port must match rather than inherit its parser's
defaults.
"""

import json
from typing import Final, NoReturn, assert_never

from dromon.model import (
    Address,
    Event,
    InvalidField,
    LineCreated,
    LineState,
    LineStateChanged,
    MessageDelivered,
    MessageKind,
    MessageSent,
    OperatorRegistered,
    QuestionSuperseded,
    QuestionWithdrawn,
    WakeIssued,
    WakeRequested,
    parse_address,
)

FORMAT_VERSION: Final = 1

MAX_PAYLOAD_BYTES: Final = 8 * 1024 * 1024
"""Upper bound on an encoded event. The largest valid event, a question with a maximal
body and choices whose every character needs a six-byte escape, stays under it."""

KIND_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "line_created": ("line_id", "engine"),
    "line_state_changed": ("line_id", "state"),
    "operator_registered": ("operator_id",),
    "message_sent": (
        "message_id",
        "sender",
        "recipient",
        "kind",
        "body",
        "choices",
        "blocking",
        "reply_to",
    ),
    "message_delivered": ("message_id",),
    "wake_requested": ("wake_id", "line_id", "message_id"),
    "wake_issued": ("wake_id",),
    "question_withdrawn": ("message_id",),
    "question_superseded": ("message_id", "superseded_by"),
}
"""The wire name of each event kind and its fields, in declaration order."""


class DecodeError(ValueError):
    """A payload is not a valid encoded event."""


def encode(event: Event) -> str:
    """Return the canonical JSON encoding of ``event``."""
    kind, data = _to_wire(event)
    text = json.dumps(
        {"data": data, "kind": kind, "v": FORMAT_VERSION},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise InvalidField(
            f"the encoded {kind} event exceeds {MAX_PAYLOAD_BYTES} bytes"
        )
    return text


def decode(payload: str) -> Event:
    """Return the event encoded in ``payload``, or raise ``DecodeError``."""
    try:
        size = len(payload.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise DecodeError("the payload is not valid Unicode text") from error
    if size > MAX_PAYLOAD_BYTES:
        raise DecodeError(f"the payload is {size} bytes, over {MAX_PAYLOAD_BYTES}")
    try:
        document: object = json.loads(
            payload,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_number,
            parse_float=_reject_number,
        )
    except RecursionError as error:
        raise DecodeError("the payload nests too deeply") from error
    except ValueError as error:
        raise DecodeError(f"the payload is not valid JSON: {error}") from error
    envelope = _object(document, "the payload", ("data", "kind", "v"))
    version = envelope["v"]
    if type(version) is not int or version != FORMAT_VERSION:
        raise DecodeError(f"unsupported format version {version!r}")
    kind = envelope["kind"]
    if not isinstance(kind, str) or kind not in KIND_FIELDS:
        raise DecodeError(f"unknown event kind {kind!r}")
    data = _object(envelope["data"], f"the {kind} data", KIND_FIELDS[kind])
    try:
        return _from_wire(kind, data)
    except InvalidField as error:
        raise DecodeError(f"invalid {kind} event: {error}") from error


def _to_wire(event: Event) -> tuple[str, dict[str, object]]:
    match event:
        case LineCreated():
            return "line_created", {"line_id": event.line_id, "engine": event.engine}
        case LineStateChanged():
            return "line_state_changed", {
                "line_id": event.line_id,
                "state": event.state.value,
            }
        case OperatorRegistered():
            return "operator_registered", {"operator_id": event.operator_id}
        case MessageSent():
            return "message_sent", {
                "message_id": event.message_id,
                "sender": str(event.sender),
                "recipient": str(event.recipient),
                "kind": event.kind.value,
                "body": event.body,
                "choices": list(event.choices),
                "blocking": event.blocking,
                "reply_to": event.reply_to,
            }
        case MessageDelivered():
            return "message_delivered", {"message_id": event.message_id}
        case WakeRequested():
            return "wake_requested", {
                "wake_id": event.wake_id,
                "line_id": event.line_id,
                "message_id": event.message_id,
            }
        case WakeIssued():
            return "wake_issued", {"wake_id": event.wake_id}
        case QuestionWithdrawn():
            return "question_withdrawn", {"message_id": event.message_id}
        case QuestionSuperseded():
            return "question_superseded", {
                "message_id": event.message_id,
                "superseded_by": event.superseded_by,
            }
        case _:
            assert_never(event)


def _from_wire(kind: str, data: dict[str, object]) -> Event:
    match kind:
        case "line_created":
            return LineCreated(_str(data, "line_id"), _str(data, "engine"))
        case "line_state_changed":
            return LineStateChanged(
                _str(data, "line_id"), _member(data, "state", LineState)
            )
        case "operator_registered":
            return OperatorRegistered(_str(data, "operator_id"))
        case "message_sent":
            return MessageSent(
                _str(data, "message_id"),
                _address(data, "sender"),
                _address(data, "recipient"),
                _member(data, "kind", MessageKind),
                _str(data, "body"),
                _choices(data),
                _bool(data, "blocking"),
                _optional_str(data, "reply_to"),
            )
        case "message_delivered":
            return MessageDelivered(_str(data, "message_id"))
        case "wake_requested":
            return WakeRequested(
                _str(data, "wake_id"), _str(data, "line_id"), _str(data, "message_id")
            )
        case "wake_issued":
            return WakeIssued(_str(data, "wake_id"))
        case "question_withdrawn":
            return QuestionWithdrawn(_str(data, "message_id"))
        case "question_superseded":
            return QuestionSuperseded(
                _str(data, "message_id"), _str(data, "superseded_by")
            )
        case _:
            raise DecodeError(f"unknown event kind {kind!r}")


# Field readers. Each checks the JSON type only. The event constructor checks the value.


def _object(value: object, what: str, keys: tuple[str, ...]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DecodeError(f"{what} is not an object")
    document: dict[str, object] = value  # pyright: ignore[reportUnknownVariableType]
    if set(document) != set(keys):
        raise DecodeError(
            f"{what} has keys {sorted(document)}, expected {sorted(keys)}"
        )
    return document


def _str(data: dict[str, object], name: str) -> str:
    value = data[name]
    if not isinstance(value, str):
        raise DecodeError(f"{name} is not a string")
    return value


def _optional_str(data: dict[str, object], name: str) -> str | None:
    value = data[name]
    if value is None:
        return None
    if not isinstance(value, str):
        raise DecodeError(f"{name} is not a string or null")
    return value


def _address(data: dict[str, object], name: str) -> Address:
    return parse_address(_str(data, name))


def _bool(data: dict[str, object], name: str) -> bool:
    value = data[name]
    if type(value) is not bool:
        raise DecodeError(f"{name} is not a boolean")
    return value


def _member[T: (LineState, MessageKind)](
    data: dict[str, object], name: str, kind: type[T]
) -> T:
    value = _str(data, name)
    try:
        return kind(value)
    except ValueError as error:
        raise DecodeError(f"{name} has no value {value!r}") from error


def _choices(data: dict[str, object]) -> tuple[str, ...]:
    value = data["choices"]
    if not isinstance(value, list):
        raise DecodeError("choices is not an array")
    items: list[object] = value  # pyright: ignore[reportUnknownVariableType]
    choices: list[str] = []
    for item in items:
        if not isinstance(item, str):
            raise DecodeError("a choice is not a string")
        choices.append(item)
    return tuple(choices)


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise DecodeError(f"duplicate key {key!r}")
        document[key] = value
    return document


def _reject_number(text: str) -> NoReturn:
    raise DecodeError(f"non-integer number {text!r}")
