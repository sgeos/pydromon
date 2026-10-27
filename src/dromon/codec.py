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
from typing import Final, NoReturn, assert_never, cast

from dromon.model import (
    Address,
    AgentAccess,
    AgentRegistered,
    AgentRevoked,
    Event,
    InvalidField,
    LineConfigured,
    LineCreated,
    LineHeld,
    LineReleased,
    LineOverridden,
    LineSession,
    LineSessionCleared,
    ContextHandedOff,
    LineRebound,
    ProjectOverridden,
    LineState,
    LineStateChanged,
    MessageDelivered,
    MessageAcknowledged,
    MessageKind,
    MessageRead,
    MessageSent,
    OperatorRegistered,
    QuestionSuperseded,
    QuestionWithdrawn,
    WakeIssued,
    WakeRequested,
    WakeScheduled,
    WakeWithdrawn,
    TaskNoted,
    LineCostMarked,
    ProcessesLeft,
    StopUnverified,
    WalkLimited,
    parse_address,
)

FORMAT_VERSION: Final = 1

MAX_PAYLOAD_BYTES: Final = 8 * 1024 * 1024
"""Upper bound on an encoded event. The largest valid event, a question with a maximal
body and choices whose every character needs a six-byte escape, stays under it."""

KIND_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "line_created": ("line_id", "engine", "strategy"),
    "line_state_changed": ("line_id", "state"),
    "line_configured": ("line_id", "config"),
    "line_session": ("line_id", "engine_session"),
    "line_session_cleared": ("line_id",),
    "line_rebound": ("line_id", "engine", "strategy"),
    "line_overridden": ("line_id", "overrides"),
    "project_overridden": ("overrides",),
    "line_held": ("line_id", "question_id"),
    "line_released": ("line_id",),
    "operator_registered": ("operator_id",),
    "agent_registered": ("agent_id", "access", "token_sha256"),
    "agent_revoked": ("agent_id",),
    "message_sent": (
        "message_id",
        "sender",
        "recipient",
        "kind",
        "body",
        "choices",
        "reply_to",
    ),
    "message_delivered": ("message_id",),
    "message_read": ("message_id",),
    "message_acknowledged": ("message_id",),
    "wake_requested": ("wake_id", "line_id", "message_id"),
    "wake_issued": ("wake_id",),
    "wake_scheduled": ("line_id", "message_id", "due", "note"),
    "wake_withdrawn": ("line_id", "message_id"),
    "task_noted": ("line_id", "message_id", "task_id", "status", "body"),
    "line_cost_marked": ("line_id", "engine_session", "total_micro_usd"),
    "processes_left": ("line_id", "cause", "pids", "commands", "more"),
    "stop_unverified": ("line_id", "note"),
    "walk_limited": ("note",),
    "question_withdrawn": ("message_id",),
    "question_superseded": ("message_id", "superseded_by"),
    "context_handed_off": (
        "line_id",
        "cause",
        "outcome",
        "message_ids",
        "chars",
        "sha256",
    ),
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
            return "line_created", {
                "line_id": event.line_id,
                "engine": event.engine,
                "strategy": event.strategy,
            }
        case LineStateChanged():
            return "line_state_changed", {
                "line_id": event.line_id,
                "state": event.state.value,
            }
        case LineConfigured():
            return "line_configured", {"line_id": event.line_id, "config": event.config}
        case LineHeld():
            return "line_held", {
                "line_id": event.line_id,
                "question_id": event.question_id,
            }
        case LineReleased():
            return "line_released", {"line_id": event.line_id}
        case LineSession():
            return "line_session", {
                "line_id": event.line_id,
                "engine_session": event.engine_session,
            }
        case LineSessionCleared():
            return "line_session_cleared", {"line_id": event.line_id}
        case LineRebound():
            return "line_rebound", {
                "line_id": event.line_id,
                "engine": event.engine,
                "strategy": event.strategy,
            }
        case LineOverridden():
            return "line_overridden", {
                "line_id": event.line_id,
                "overrides": event.overrides,
            }
        case ProjectOverridden():
            return "project_overridden", {"overrides": event.overrides}
        case OperatorRegistered():
            return "operator_registered", {"operator_id": event.operator_id}
        case AgentRegistered():
            return "agent_registered", {
                "agent_id": event.agent_id,
                "access": event.access.value,
                "token_sha256": event.token_sha256,
            }
        case AgentRevoked():
            return "agent_revoked", {"agent_id": event.agent_id}
        case MessageSent():
            return "message_sent", {
                "message_id": event.message_id,
                "sender": str(event.sender),
                "recipient": str(event.recipient),
                "kind": event.kind.value,
                "body": event.body,
                "choices": list(event.choices),
                "reply_to": event.reply_to,
            }
        case MessageDelivered():
            return "message_delivered", {"message_id": event.message_id}
        case MessageRead():
            return "message_read", {"message_id": event.message_id}
        case MessageAcknowledged():
            return "message_acknowledged", {"message_id": event.message_id}
        case WakeRequested():
            return "wake_requested", {
                "wake_id": event.wake_id,
                "line_id": event.line_id,
                "message_id": event.message_id,
            }
        case WakeIssued():
            return "wake_issued", {"wake_id": event.wake_id}
        case WakeScheduled():
            return "wake_scheduled", {
                "line_id": event.line_id,
                "message_id": event.message_id,
                "due": event.due,
                "note": event.note,
            }
        case WakeWithdrawn():
            return "wake_withdrawn", {
                "line_id": event.line_id,
                "message_id": event.message_id,
            }
        case LineCostMarked():
            return "line_cost_marked", {
                "line_id": event.line_id,
                "engine_session": event.engine_session,
                "total_micro_usd": event.total_micro_usd,
            }
        case ProcessesLeft():
            return "processes_left", {
                "line_id": event.line_id,
                "cause": event.cause,
                "pids": list(event.pids),
                "commands": list(event.commands),
                "more": event.more,
            }
        case StopUnverified():
            return "stop_unverified", {"line_id": event.line_id, "note": event.note}
        case WalkLimited():
            return "walk_limited", {"note": event.note}
        case TaskNoted():
            return "task_noted", {
                "line_id": event.line_id,
                "message_id": event.message_id,
                "task_id": event.task_id,
                "status": event.status,
                "body": event.body,
            }
        case QuestionWithdrawn():
            return "question_withdrawn", {"message_id": event.message_id}
        case QuestionSuperseded():
            return "question_superseded", {
                "message_id": event.message_id,
                "superseded_by": event.superseded_by,
            }
        case ContextHandedOff():
            return "context_handed_off", {
                "line_id": event.line_id,
                "cause": event.cause,
                "outcome": event.outcome,
                "message_ids": list(event.message_ids),
                "chars": event.chars,
                "sha256": event.sha256,
            }
        case _:
            assert_never(event)


def _from_wire(kind: str, data: dict[str, object]) -> Event:
    match kind:
        case "line_created":
            return LineCreated(
                _str(data, "line_id"), _str(data, "engine"), _str(data, "strategy")
            )
        case "line_state_changed":
            return LineStateChanged(
                _str(data, "line_id"), _member(data, "state", LineState)
            )
        case "line_configured":
            return LineConfigured(_str(data, "line_id"), _str(data, "config"))
        case "line_held":
            return LineHeld(_str(data, "line_id"), _str(data, "question_id"))
        case "line_released":
            return LineReleased(_str(data, "line_id"))
        case "line_session":
            return LineSession(_str(data, "line_id"), _str(data, "engine_session"))
        case "line_session_cleared":
            return LineSessionCleared(_str(data, "line_id"))
        case "line_rebound":
            return LineRebound(
                _str(data, "line_id"), _str(data, "engine"), _str(data, "strategy")
            )
        case "line_overridden":
            return LineOverridden(_str(data, "line_id"), _str(data, "overrides"))
        case "project_overridden":
            return ProjectOverridden(_str(data, "overrides"))
        case "operator_registered":
            return OperatorRegistered(_str(data, "operator_id"))
        case "agent_registered":
            return AgentRegistered(
                _str(data, "agent_id"),
                _member(data, "access", AgentAccess),
                _str(data, "token_sha256"),
            )
        case "agent_revoked":
            return AgentRevoked(_str(data, "agent_id"))
        case "message_sent":
            return MessageSent(
                _str(data, "message_id"),
                _address(data, "sender"),
                _address(data, "recipient"),
                _member(data, "kind", MessageKind),
                _str(data, "body"),
                _choices(data),
                _optional_str(data, "reply_to"),
            )
        case "message_delivered":
            return MessageDelivered(_str(data, "message_id"))
        case "message_read":
            return MessageRead(_str(data, "message_id"))
        case "message_acknowledged":
            return MessageAcknowledged(_str(data, "message_id"))
        case "wake_requested":
            return WakeRequested(
                _str(data, "wake_id"), _str(data, "line_id"), _str(data, "message_id")
            )
        case "wake_issued":
            return WakeIssued(_str(data, "wake_id"))
        case "wake_scheduled":
            return WakeScheduled(
                _str(data, "line_id"),
                _str(data, "message_id"),
                _str(data, "due"),
                _str(data, "note"),
            )
        case "wake_withdrawn":
            return WakeWithdrawn(_str(data, "line_id"), _str(data, "message_id"))
        case "line_cost_marked":
            return LineCostMarked(
                _str(data, "line_id"),
                _str(data, "engine_session"),
                _int(data, "total_micro_usd"),
            )
        case "processes_left":
            return ProcessesLeft(
                _str(data, "line_id"),
                _str(data, "cause"),
                tuple(_list(data, "pids", int)),
                tuple(_list(data, "commands", str)),
                _int(data, "more"),
            )
        case "stop_unverified":
            return StopUnverified(_str(data, "line_id"), _str(data, "note"))
        case "walk_limited":
            return WalkLimited(_str(data, "note"))
        case "task_noted":
            return TaskNoted(
                _str(data, "line_id"),
                _str(data, "message_id"),
                _str(data, "task_id"),
                _str(data, "status"),
                _str(data, "body"),
            )
        case "question_withdrawn":
            return QuestionWithdrawn(_str(data, "message_id"))
        case "question_superseded":
            return QuestionSuperseded(
                _str(data, "message_id"), _str(data, "superseded_by")
            )
        case "context_handed_off":
            return ContextHandedOff(
                _str(data, "line_id"),
                _str(data, "cause"),
                _str(data, "outcome"),
                tuple(_list(data, "message_ids", str)),
                _int(data, "chars"),
                _str(data, "sha256"),
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


def _int(data: dict[str, object], name: str) -> int:
    value = data[name]
    if type(value) is not int:
        raise DecodeError(f"{name} is not a whole number")
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


def _member[T: (LineState, MessageKind, AgentAccess)](
    data: dict[str, object], name: str, kind: type[T]
) -> T:
    value = _str(data, name)
    try:
        return kind(value)
    except ValueError as error:
        raise DecodeError(f"{name} has no value {value!r}") from error


def _list[T: (int, str)](data: dict[str, object], name: str, kind: type[T]) -> list[T]:
    """The array ``name`` of ``data``, each item exactly of ``kind``."""
    value = data[name]
    if not isinstance(value, list):
        raise DecodeError(f"{name} is not an array")
    items: list[object] = value  # pyright: ignore[reportUnknownVariableType]
    found: list[T] = []
    for item in items:
        if type(item) is not kind:
            raise DecodeError(f"an item of {name} is not a {kind.__name__}")
        found.append(cast(T, item))
    return found


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
