"""The client protocol, requests and responses between ``dromon`` and ``dromond``.

Each message is one line of canonical JSON terminated by a newline, over the daemon's
Unix domain socket. A client may send several requests on one connection, and the
daemon answers each in order. ``docs/protocol/CLIENT.md`` is normative.

Decoding treats every line as untrusted, with the same strictness as the event codec.
Porting note. Nothing here depends on Python's ``json`` defaults, which the decoder
overrides, so a port can match it exactly.
"""

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, NoReturn, cast

PROTOCOL_VERSION: Final = 1

MAX_LINE_BYTES: Final = 64 * 1024
"""Upper bound on one request or response line, including its newline."""

MAX_PEER_LINE_BYTES: Final = 8 * 1024 * 1024
"""Upper bound on one line on the peer socket, the bound of an encoded event, so the
largest body a peer's policy may permit fits even when escaping multiplies its size."""

REQUEST_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")


class Operation(StrEnum):
    PING = "ping"
    STATUS = "status"
    STOP = "stop"
    CREATE_LINE = "create_line"
    CONFIGURE_LINE = "configure_line"
    SEND = "send"
    QUESTIONS = "questions"
    ANSWER = "answer"
    MAIL = "mail"
    REPLIES = "replies"
    """Read only. Added in round 9."""
    READ = "read"
    ACKNOWLEDGE = "acknowledge"
    SENT = "sent"
    RELEASE = "release"
    LINE_VIEW = "line_view"
    LINE_ACTIVITY = "line_activity"
    INTERRUPT = "interrupt"
    HIBERNATE = "hibernate"
    """Operator only. Not offered to external agents."""
    AWAKEN = "awaken"
    """Operator only. Not offered to external agents."""
    RESTART = "restart"
    """Operator only. Not offered to external agents."""
    SETTINGS = "settings"
    SET_SETTING = "set_setting"
    CLEAR_SESSION = "clear_session"
    REBIND_LINE = "rebind_line"
    """Operator only. Not offered to external agents."""
    RELOAD_PROJECT = "reload_project"
    ROLES = "roles"
    ROLE_ENTRY = "role_entry"
    ROLE_TAKE = "role_take"
    """Operator only. Not offered to external agents."""
    ROLE_VACATE = "role_vacate"
    """Operator only. Not offered to external agents."""
    ROLE_HANDOFF = "role_handoff"
    """Operator only. Not offered to external agents."""
    ROLE_CREATE = "role_create"
    """Operator only. Not offered to external agents or lines. Added in round 10."""
    ROLE_UPDATE = "role_update"
    """Operator only. Not offered to external agents or lines. Added in round 10."""
    ROLE_DELETE = "role_delete"
    """Operator only. Not offered to external agents or lines. Added in round 10."""
    RETIRE_LINE = "retire_line"
    """Operator only. Not offered to external agents or lines. Added in round 14."""
    RUN_CLEAR = "run_clear"
    """Operator only. Not offered to external agents or lines. Task of 2026-10-31."""
    RUNS = "runs"
    """Read only. Not offered to external agents or lines. Added in round 14."""
    BUDGETS = "budgets"
    BUDGET_SET = "budget_set"
    """Operator only. Not offered to external agents."""
    AGENT_ADD = "agent_add"
    AGENT_REVOKE = "agent_revoke"
    AGENT_LIST = "agent_list"
    PEER_TOKEN = "peer_token"  # noqa: S105, an operation name
    """Operator only. Not offered to external agents."""
    PEER_REVOKE = "peer_revoke"
    """Operator only. Not offered to external agents."""
    PEER_STORE = "peer_store"
    """Operator only. Not offered to external agents."""
    PEER_LIST = "peer_list"
    """Operator only. Not offered to external agents."""
    PEER_KEY = "peer_key"
    """Operator only. Not offered to external agents."""
    AGENT_TOOLS = "agent_tools"
    """Served only on the agent socket."""
    AGENT_CALL = "agent_call"
    """Served only on the agent socket."""
    HELLO = "hello"
    """Served only on the peer socket."""
    DELIVER = "deliver"
    """Served only on the peer socket."""


class ErrorCode(StrEnum):
    BAD_REQUEST = "bad_request"
    UNSUPPORTED_VERSION = "unsupported_version"
    UNKNOWN_OPERATION = "unknown_operation"
    TOO_LONG = "too_long"
    REJECTED = "rejected"
    """The request is well formed but does not apply, such as a message to no line."""
    INTERNAL = "internal"


class ProtocolError(ValueError):
    """A line is not a valid protocol message."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Request:
    request_id: str
    operation: Operation
    args: dict[str, object] = field(default_factory=dict[str, object])
    """Operation arguments. Each operation checks its own, strictly."""


@dataclass(frozen=True, slots=True)
class Response:
    request_id: str
    ok: bool
    result: dict[str, object] | None = None
    error_code: ErrorCode | None = None
    error_message: str | None = None


def _dumps(document: dict[str, object], limit: int = MAX_LINE_BYTES) -> bytes:
    text = json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )
    line = (text + "\n").encode("utf-8")
    if len(line) > limit:
        raise ProtocolError(ErrorCode.TOO_LONG, f"the line exceeds {limit}")
    found = _float_at(document)
    if found is not None:
        # The decoder refuses every float, so a sender that let one through would
        # only fail at the other end, as ``dromon status`` once did on a rate.
        raise ProtocolError(
            ErrorCode.INTERNAL,
            f"{found} is a float, which the protocol does not carry",
        )
    return line


def _float_at(document: dict[str, object]) -> str | None:
    """The path of the first float in ``document``, or ``None``. Called only once
    ``json.dumps`` has refused any cycle and the line limit has bounded the size, so
    the walk visits a finite tree and ends."""
    pending: list[tuple[str, object]] = [("", document)]
    while pending:
        path, value = pending.pop()
        if isinstance(value, float):
            return path or "the message"
        if isinstance(value, dict):
            items = cast(dict[object, object], value).items()
            pending.extend(
                (f"{path}.{key}" if path else str(key), v) for key, v in items
            )
        elif isinstance(value, list | tuple):
            members = cast(list[object] | tuple[object, ...], value)
            pending.extend((f"{path}[{index}]", v) for index, v in enumerate(members))
    return None


def _reject_number(text: str) -> NoReturn:
    raise ProtocolError(ErrorCode.BAD_REQUEST, f"non-integer number {text!r}")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            raise ProtocolError(ErrorCode.BAD_REQUEST, f"duplicate key {key!r}")
        document[key] = value
    return document


def _loads(line: bytes, limit: int = MAX_LINE_BYTES) -> dict[str, object]:
    if len(line) > limit:
        raise ProtocolError(ErrorCode.TOO_LONG, f"the line exceeds {limit}")
    try:
        text = line.decode("utf-8")
        document: object = json.loads(
            text,
            object_pairs_hook=_object_without_duplicates,
            parse_constant=_reject_number,
            parse_float=_reject_number,
        )
    except ProtocolError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ProtocolError(
            ErrorCode.BAD_REQUEST, f"not valid JSON: {error}"
        ) from error
    if not isinstance(document, dict):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "the message is not an object")
    checked: dict[str, object] = document  # pyright: ignore[reportUnknownVariableType]
    return checked


def _version(document: dict[str, object]) -> None:
    version = document.get("v")
    if type(version) is not int or version != PROTOCOL_VERSION:
        raise ProtocolError(
            ErrorCode.UNSUPPORTED_VERSION, f"unsupported protocol version {version!r}"
        )


def _request_id(document: dict[str, object]) -> str:
    value = document.get("id")
    if not isinstance(value, str) or REQUEST_ID_PATTERN.fullmatch(value) is None:
        raise ProtocolError(ErrorCode.BAD_REQUEST, f"invalid request id {value!r}")
    return value


def encode_request(request: Request, limit: int = MAX_LINE_BYTES) -> bytes:
    document: dict[str, object] = {
        "id": request.request_id,
        "op": request.operation.value,
        "v": PROTOCOL_VERSION,
    }
    if request.args:
        document["args"] = request.args
    return _dumps(document, limit)


def decode_request(line: bytes, limit: int = MAX_LINE_BYTES) -> Request:
    """Return the request in ``line``, or raise ``ProtocolError`` with a code. Only the
    peer socket gives a ``limit`` above ``MAX_LINE_BYTES``."""
    document = _loads(line, limit)
    _version(document)
    request_id = _request_id(document)
    if set(document) not in ({"id", "op", "v"}, {"args", "id", "op", "v"}):
        raise ProtocolError(
            ErrorCode.BAD_REQUEST, f"the request has keys {sorted(document)}"
        )
    raw_args = document.get("args", {})
    if not isinstance(raw_args, dict):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "args is not an object")
    args = cast(dict[str, object], raw_args)
    operation = document["op"]
    if not isinstance(operation, str):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "op is not a string")
    try:
        return Request(request_id, Operation(operation), args)
    except ValueError as error:
        raise ProtocolError(
            ErrorCode.UNKNOWN_OPERATION, f"unknown operation {operation!r}"
        ) from error


def request_id_of(line: bytes, limit: int = MAX_LINE_BYTES) -> str:
    """Return the request id in a line that failed to decode, or ``""`` if none.

    Lets an error response name the request it answers whenever the id is readable.
    The parse here accepts floats, so that a request refused for holding one is still
    answered under its own id, and the client reports the refusal rather than a
    mismatched id.
    """
    if len(line) > limit:
        return ""
    try:
        document: object = json.loads(
            line.decode("utf-8"), object_pairs_hook=_object_without_duplicates
        )
    except (ProtocolError, UnicodeDecodeError, ValueError, RecursionError):
        return ""
    if not isinstance(document, dict):
        return ""
    try:
        return _request_id(cast(dict[str, object], document))
    except ProtocolError:
        return ""


def encode_response(response: Response, limit: int = MAX_LINE_BYTES) -> bytes:
    document: dict[str, object] = {
        "id": response.request_id,
        "ok": response.ok,
        "v": PROTOCOL_VERSION,
    }
    if response.ok:
        document["result"] = response.result if response.result is not None else {}
    else:
        code = response.error_code or ErrorCode.INTERNAL
        document["error"] = {
            "code": code.value,
            "message": response.error_message or "",
        }
    return _dumps(document, limit)


def decode_response(line: bytes, limit: int = MAX_LINE_BYTES) -> Response:
    """Return the response in ``line``. The daemon is trusted less than the operator,
    so its responses are checked like any other input."""
    document = _loads(line, limit)
    _version(document)
    request_id = document.get("id")
    if not isinstance(request_id, str):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "the response id is not a string")
    ok = document.get("ok")
    if type(ok) is not bool:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "ok is not a boolean")
    if ok:
        if set(document) != {"id", "ok", "result", "v"}:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "malformed success response")
        result = document["result"]
        if not isinstance(result, dict):
            raise ProtocolError(ErrorCode.BAD_REQUEST, "result is not an object")
        checked: dict[str, object] = result  # pyright: ignore[reportUnknownVariableType]
        return Response(request_id, True, result=checked)
    if set(document) != {"error", "id", "ok", "v"}:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "malformed error response")
    error = document["error"]
    if not isinstance(error, dict):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "malformed error object")
    fields: dict[str, object] = error  # pyright: ignore[reportUnknownVariableType]
    if set(fields) != {"code", "message"}:
        raise ProtocolError(ErrorCode.BAD_REQUEST, "malformed error object")
    code = fields["code"]
    message = fields["message"]
    if not isinstance(code, str) or not isinstance(message, str):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "malformed error fields")
    try:
        error_code = ErrorCode(code)
    except ValueError as failure:
        raise ProtocolError(
            ErrorCode.BAD_REQUEST, f"unknown error code {code!r}"
        ) from failure
    return Response(request_id, False, error_code=error_code, error_message=message)
