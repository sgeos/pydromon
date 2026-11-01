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
    HoldCause,
    InvalidField,
    LineConfigured,
    LineCreated,
    LineHeld,
    LineReleased,
    LineHibernated,
    LineAwakened,
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
    MessagePrioritized,
    MessageCorrelated,
    TurnCorrelated,
    LineTurnEnded,
    DisclosureChecked,
    TaskRecorded,
    MessageKind,
    MessageRead,
    MessageReceived,
    MessageReturned,
    MessageUnconfirmed,
    MessageSent,
    OperatorRegistered,
    QuestionSuperseded,
    QuestionWithdrawn,
    WakeIssued,
    WakeRequested,
    WakeScheduled,
    WakeWithdrawn,
    TaskNoted,
    RestartNoted,
    LineCostMarked,
    ProcessesLeft,
    StopUnverified,
    WalkLimited,
    RoleDeclared,
    SeatBound,
    SeatUnbound,
    RoleRetired,
    LineRetired,
    RoleHeld,
    RoleHolder,
    OwnershipChanged,
    DEFAULT_HANDOFF_BELOW_PERCENT,
    FailoverPolicy,
    OwnershipCause,
    RoleNoted,
    RoleNoteKind,
    BriefSet,
    RequestStamp,
    BudgetStated,
    BudgetHandoffRequested,
    BudgetSource,
    LeaseAcquired,
    LeaseEnd,
    LeaseReleased,
    RunCleared,
    RunClearance,
    RunEnded,
    RunStarted,
    RunStatus,
    PeerDeclared,
    PeerKeyed,
    PeerOpened,
    PeerRemoved,
    LocalAddress,
    OperatorAddress,
    RemoteAddress,
    ReturnCode,
    Sender,
    parse_address,
    parse_any_address,
    parse_sender,
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
    "line_held": ("line_id", "question_id", "cause"),
    "line_released": ("line_id",),
    "line_hibernated": ("line_id",),
    "line_awakened": ("line_id",),
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
        "request",
    ),
    "message_received": (
        "message_id",
        "sender",
        "recipient",
        "body",
        "reply_to",
        "origin_id",
        "received_at",
        "priority_asked",
    ),
    "message_delivered": ("message_id",),
    "message_returned": ("message_id", "code", "note_id"),
    "message_unconfirmed": ("message_id", "note_id"),
    "message_read": ("message_id",),
    "message_acknowledged": ("message_id",),
    "message_prioritized": ("message_id",),
    "message_correlated": ("message_id", "key"),
    "turn_correlated": ("line_id", "keys", "key_count", "cost_micro_usd"),
    "disclosure_checked": ("line_id", "checked_at", "matched_lines"),
    "wake_requested": ("wake_id", "line_id", "message_id"),
    "wake_issued": ("wake_id",),
    "wake_scheduled": ("line_id", "message_id", "due", "note", "task_id", "request"),
    "wake_withdrawn": ("line_id", "message_id"),
    "line_turn_ended": ("line_id", "workdir_commit", "unreported"),
    "task_noted": ("line_id", "message_id", "task_id", "status", "body"),
    "task_recorded": (
        "line_id",
        "task_id",
        "command",
        "status",
        "exit_code",
        "output_file",
        "output_digest",
        "output_bytes",
        "workdir_commit",
        "ended_at",
    ),
    "restart_noted": ("line_id", "message_id", "body"),
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
    "role_declared": (
        "role_id",
        "workdir",
        "handoff_seconds",
        "seats",
        "failover",
        "handoff_below_percent",
    ),
    "seat_bound": ("line_id", "role_id", "may_own"),
    "seat_unbound": ("line_id", "role_id"),
    "role_retired": ("role_id", "by", "retired_at"),
    "line_retired": ("line_id", "by", "retired_at"),
    "role_held": ("role_id", "holder", "by", "held_at"),
    "ownership_changed": (
        "role_id",
        "owner",
        "previous",
        "cause",
        "by",
        "entry_id",
        "changed_at",
        "back_to",
    ),
    "role_noted": (
        "role_id",
        "entry_id",
        "line_id",
        "kind",
        "body",
        "workdir_commit",
        "daemon_commit",
        "noted_at",
    ),
    "brief_set": (
        "line_id",
        "task",
        "decisions",
        "artifacts",
        "next_action",
        "workdir_commit",
        "daemon_commit",
        "set_at",
    ),
    "budget_stated": (
        "provider",
        "remaining_percent",
        "resets_at",
        "source",
        "note",
        "stated_at",
        "window",
    ),
    "budget_handoff_requested": (
        "provider",
        "role_id",
        "remaining_percent",
        "threshold",
        "requested_at",
    ),
    "lease_acquired": ("name", "line_id", "acquired_at", "expires"),
    "lease_released": ("name", "line_id", "cause"),
    "run_started": (
        "run_id",
        "line_id",
        "role_id",
        "previous",
        "command",
        "argv",
        "workdir",
        "output_file",
        "pid",
        "process_started",
        "started_at",
    ),
    "run_ended": (
        "run_id",
        "status",
        "exit_code",
        "output_digest",
        "output_bytes",
        "note",
        "ended_at",
    ),
    "run_cleared": ("run_id", "cause", "operator_id", "cleared_at"),
    "peer_declared": ("peer_id", "transport", "policy"),
    "peer_removed": ("peer_id",),
    "peer_keyed": ("peer_id", "token_sha256"),
    "peer_opened": ("peer_id", "scope", "opened_at"),
}
"""The wire name of each event kind and its fields, in declaration order."""

RENAMED_KINDS: Final[dict[str, str]] = {
    "line_muted": "line_hibernated",
    "line_unmuted": "line_awakened",
}
"""Former wire names that still decode, each as the kind that replaced it. Mute was
renamed hibernate on 2026-10-27, after event logs, the team's own among them, had
recorded the old kinds. Encoding writes only the new names, so a log holds both once
it is appended to after the rename. A port carries the same table."""


class DecodeError(ValueError):
    """A payload is not a valid encoded event."""


def encode(event: Event) -> str:
    """Return the canonical JSON encoding of ``event``."""
    kind, data = _to_wire(event)
    data = _without_nulls(kind, data)
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
    if isinstance(kind, str):
        kind = RENAMED_KINDS.get(kind, kind)
    if not isinstance(kind, str) or kind not in KIND_FIELDS:
        raise DecodeError(f"unknown event kind {kind!r}")
    data = _object(
        _upgraded(kind, envelope["data"]), f"the {kind} data", KIND_FIELDS[kind]
    )
    try:
        return _from_wire(kind, data)
    except InvalidField as error:
        raise DecodeError(f"invalid {kind} event: {error}") from error


ROLE_DECLARED_FIRST_FIELDS: Final = frozenset({"role_id", "workdir", "handoff_seconds"})
"""The fields of a ``role_declared`` written before 2026-10-27's step 3 of roles,
which added the seat order, the failover policy and the handoff threshold."""


def _upgraded(kind: str, data: object) -> object:
    """``data`` with the defaults of fields added to its kind since it was written,
    so a log written before them still decodes. A port carries the same defaults."""
    if not isinstance(data, dict):
        return data
    fields = cast(dict[str, object], data)
    if kind == "role_declared" and frozenset(fields) == ROLE_DECLARED_FIRST_FIELDS:
        return {
            **fields,
            "seats": [],
            "failover": FailoverPolicy.AUTOMATIC.value,
            "handoff_below_percent": DEFAULT_HANDOFF_BELOW_PERCENT,
        }
    if kind == "wake_scheduled" and "task_id" not in fields:
        fields = {**fields, "task_id": None}
    if kind == "budget_stated" and "window" not in fields:
        fields = {**fields, "window": None}
    if kind in OMITTED_WHEN_NULL:
        fields = {
            **{name: None for name in OMITTED_WHEN_NULL[kind] if name not in fields},
            **fields,
        }
    return fields


OMITTED_WHEN_NULL: Final[dict[str, tuple[str, ...]]] = {
    "message_sent": ("request",),
    "wake_scheduled": ("request",),
    "line_turn_ended": ("unreported",),
    "budget_stated": ("window",),
    "line_held": ("cause",),
}
"""The fields left out of the wire when they are null, which a log written before
them never held, so decoding reads an absent one as null. **Task of 2026-10-29,
round 5.** The request key of a keyed call is recorded in the event of its act, and
an event of a call without a key is written as before it. The ``window`` field was
added to ``budget_stated`` on 2026-10-31, and ``cause`` to ``line_held`` the same
day. **Porting hazard.** A port must omit these
fields when null, not write them as null, so that its log stays readable by a daemon
that predates them, and must read an absent one as null. A daemon predating ``window``
does not read an event that names one."""


def _without_nulls(kind: str, data: dict[str, object]) -> dict[str, object]:
    """``data`` without the fields ``OMITTED_WHEN_NULL`` names for ``kind`` that
    are null."""
    omitted = OMITTED_WHEN_NULL.get(kind, ())
    return {
        name: value
        for name, value in data.items()
        if not (name in omitted and value is None)
    }


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
                "cause": None if event.cause is None else event.cause.value,
            }
        case LineReleased():
            return "line_released", {"line_id": event.line_id}
        case LineHibernated():
            return "line_hibernated", {"line_id": event.line_id}
        case LineAwakened():
            return "line_awakened", {"line_id": event.line_id}
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
                "request": _request_wire(event.request),
            }
        case MessageReceived():
            return "message_received", {
                "message_id": event.message_id,
                "sender": str(event.sender),
                "recipient": str(event.recipient),
                "body": event.body,
                "reply_to": event.reply_to,
                "origin_id": event.origin_id,
                "received_at": event.received_at,
                "priority_asked": event.priority_asked,
            }
        case MessageDelivered():
            return "message_delivered", {"message_id": event.message_id}
        case MessageReturned():
            return "message_returned", {
                "message_id": event.message_id,
                "code": event.code.value,
                "note_id": event.note_id,
            }
        case MessageUnconfirmed():
            return "message_unconfirmed", {
                "message_id": event.message_id,
                "note_id": event.note_id,
            }
        case MessageRead():
            return "message_read", {"message_id": event.message_id}
        case MessageAcknowledged():
            return "message_acknowledged", {"message_id": event.message_id}
        case MessagePrioritized():
            return "message_prioritized", {"message_id": event.message_id}
        case MessageCorrelated():
            return "message_correlated", {
                "message_id": event.message_id,
                "key": event.key,
            }
        case TurnCorrelated():
            return "turn_correlated", {
                "line_id": event.line_id,
                "keys": list(event.keys),
                "key_count": event.key_count,
                "cost_micro_usd": event.cost_micro_usd,
            }
        case LineTurnEnded():
            return "line_turn_ended", {
                "line_id": event.line_id,
                "workdir_commit": event.workdir_commit,
                "unreported": event.unreported,
            }
        case TaskRecorded():
            return "task_recorded", {
                "line_id": event.line_id,
                "task_id": event.task_id,
                "command": event.command,
                "status": event.status,
                "exit_code": event.exit_code,
                "output_file": event.output_file,
                "output_digest": event.output_digest,
                "output_bytes": event.output_bytes,
                "workdir_commit": event.workdir_commit,
                "ended_at": event.ended_at,
            }
        case DisclosureChecked():
            return "disclosure_checked", {
                "line_id": event.line_id,
                "checked_at": event.checked_at,
                "matched_lines": event.matched_lines,
            }
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
                "task_id": event.task_id,
                "request": _request_wire(event.request),
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
        case RestartNoted():
            return "restart_noted", {
                "line_id": event.line_id,
                "message_id": event.message_id,
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
        case RoleDeclared():
            return "role_declared", {
                "role_id": event.role_id,
                "workdir": event.workdir,
                "handoff_seconds": event.handoff_seconds,
                "seats": list(event.seats),
                "failover": event.failover.value,
                "handoff_below_percent": event.handoff_below_percent,
            }
        case SeatBound():
            return "seat_bound", {
                "line_id": event.line_id,
                "role_id": event.role_id,
                "may_own": event.may_own,
            }
        case SeatUnbound():
            return "seat_unbound", {"line_id": event.line_id, "role_id": event.role_id}
        case RoleRetired():
            return "role_retired", {
                "role_id": event.role_id,
                "by": str(event.by),
                "retired_at": event.retired_at,
            }
        case LineRetired():
            return "line_retired", {
                "line_id": event.line_id,
                "by": str(event.by),
                "retired_at": event.retired_at,
            }
        case RoleHeld():
            return "role_held", {
                "role_id": event.role_id,
                "holder": event.holder.value,
                "by": str(event.by),
                "held_at": event.held_at,
            }
        case OwnershipChanged():
            return "ownership_changed", {
                "role_id": event.role_id,
                "owner": event.owner,
                "previous": event.previous,
                "cause": event.cause.value,
                "by": str(event.by),
                "entry_id": event.entry_id,
                "changed_at": event.changed_at,
                "back_to": event.back_to,
            }
        case RoleNoted():
            return "role_noted", {
                "role_id": event.role_id,
                "entry_id": event.entry_id,
                "line_id": event.line_id,
                "kind": event.kind.value,
                "body": event.body,
                "workdir_commit": event.workdir_commit,
                "daemon_commit": event.daemon_commit,
                "noted_at": event.noted_at,
            }
        case BriefSet():
            return "brief_set", {
                "line_id": event.line_id,
                "task": event.task,
                "decisions": event.decisions,
                "artifacts": list(event.artifacts),
                "next_action": event.next_action,
                "workdir_commit": event.workdir_commit,
                "daemon_commit": event.daemon_commit,
                "set_at": event.set_at,
            }
        case BudgetStated():
            return "budget_stated", {
                "provider": event.provider,
                "remaining_percent": event.remaining_percent,
                "resets_at": event.resets_at,
                "source": event.source.value,
                "note": event.note,
                "stated_at": event.stated_at,
                "window": event.window,
            }
        case BudgetHandoffRequested():
            return "budget_handoff_requested", {
                "provider": event.provider,
                "role_id": event.role_id,
                "remaining_percent": event.remaining_percent,
                "threshold": event.threshold,
                "requested_at": event.requested_at,
            }
        case LeaseAcquired():
            return "lease_acquired", {
                "name": event.name,
                "line_id": event.line_id,
                "acquired_at": event.acquired_at,
                "expires": event.expires,
            }
        case LeaseReleased():
            return "lease_released", {
                "name": event.name,
                "line_id": event.line_id,
                "cause": event.cause.value,
            }
        case RunStarted():
            return "run_started", {
                "run_id": event.run_id,
                "line_id": event.line_id,
                "role_id": event.role_id,
                "previous": event.previous,
                "command": event.command,
                "argv": list(event.argv),
                "workdir": event.workdir,
                "output_file": event.output_file,
                "pid": event.pid,
                "process_started": event.process_started,
                "started_at": event.started_at,
            }
        case RunEnded():
            return "run_ended", {
                "run_id": event.run_id,
                "status": event.status.value,
                "exit_code": event.exit_code,
                "output_digest": event.output_digest,
                "output_bytes": event.output_bytes,
                "note": event.note,
                "ended_at": event.ended_at,
            }
        case RunCleared():
            return "run_cleared", {
                "run_id": event.run_id,
                "cause": event.cause.value,
                "operator_id": event.operator_id,
                "cleared_at": event.cleared_at,
            }
        case PeerDeclared():
            return "peer_declared", {
                "peer_id": event.peer_id,
                "transport": event.transport,
                "policy": event.policy,
            }
        case PeerRemoved():
            return "peer_removed", {"peer_id": event.peer_id}
        case PeerKeyed():
            return "peer_keyed", {
                "peer_id": event.peer_id,
                "token_sha256": event.token_sha256,
            }
        case PeerOpened():
            return "peer_opened", {
                "peer_id": event.peer_id,
                "scope": event.scope,
                "opened_at": event.opened_at,
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
            return LineHeld(
                _str(data, "line_id"),
                _str(data, "question_id"),
                None if data["cause"] is None else _member(data, "cause", HoldCause),
            )
        case "line_released":
            return LineReleased(_str(data, "line_id"))
        case "line_hibernated":
            return LineHibernated(_str(data, "line_id"))
        case "line_awakened":
            return LineAwakened(_str(data, "line_id"))
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
                _sender(data, "sender"),
                _any_address(data, "recipient"),
                _member(data, "kind", MessageKind),
                _str(data, "body"),
                _choices(data),
                _optional_str(data, "reply_to"),
                _request(data),
            )
        case "message_received":
            return MessageReceived(
                _str(data, "message_id"),
                _remote_address(data, "sender"),
                _address(data, "recipient"),
                _str(data, "body"),
                _optional_str(data, "reply_to"),
                _str(data, "origin_id"),
                _str(data, "received_at"),
                _bool(data, "priority_asked"),
            )
        case "message_delivered":
            return MessageDelivered(_str(data, "message_id"))
        case "message_returned":
            return MessageReturned(
                _str(data, "message_id"),
                _member(data, "code", ReturnCode),
                _optional_str(data, "note_id"),
            )
        case "message_unconfirmed":
            return MessageUnconfirmed(
                _str(data, "message_id"), _optional_str(data, "note_id")
            )
        case "message_read":
            return MessageRead(_str(data, "message_id"))
        case "message_acknowledged":
            return MessageAcknowledged(_str(data, "message_id"))
        case "message_prioritized":
            return MessagePrioritized(_str(data, "message_id"))
        case "message_correlated":
            return MessageCorrelated(_str(data, "message_id"), _str(data, "key"))
        case "turn_correlated":
            return TurnCorrelated(
                _str(data, "line_id"),
                tuple(_list(data, "keys", str)),
                _int(data, "key_count"),
                _optional_int(data, "cost_micro_usd"),
            )
        case "line_turn_ended":
            return LineTurnEnded(
                _str(data, "line_id"),
                _optional_str(data, "workdir_commit"),
                _optional_bool(data, "unreported"),
            )
        case "task_recorded":
            return TaskRecorded(
                _str(data, "line_id"),
                _str(data, "task_id"),
                _str(data, "command"),
                _str(data, "status"),
                _optional_int(data, "exit_code"),
                _optional_str(data, "output_file"),
                _optional_str(data, "output_digest"),
                _optional_int(data, "output_bytes"),
                _str(data, "workdir_commit"),
                _str(data, "ended_at"),
            )
        case "disclosure_checked":
            return DisclosureChecked(
                _str(data, "line_id"),
                _str(data, "checked_at"),
                _int(data, "matched_lines"),
            )
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
                _optional_str(data, "task_id"),
                _request(data),
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
        case "restart_noted":
            return RestartNoted(
                _str(data, "line_id"), _str(data, "message_id"), _str(data, "body")
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
        case "role_declared":
            return RoleDeclared(
                _str(data, "role_id"),
                _str(data, "workdir"),
                _int(data, "handoff_seconds"),
                tuple(_list(data, "seats", str)),
                _member(data, "failover", FailoverPolicy),
                _int(data, "handoff_below_percent"),
            )
        case "seat_bound":
            return SeatBound(
                _str(data, "line_id"), _str(data, "role_id"), _bool(data, "may_own")
            )
        case "seat_unbound":
            return SeatUnbound(_str(data, "line_id"), _str(data, "role_id"))
        case "role_retired":
            return RoleRetired(
                _str(data, "role_id"),
                _operator(data, "by"),
                _str(data, "retired_at"),
            )
        case "line_retired":
            return LineRetired(
                _str(data, "line_id"),
                _operator(data, "by"),
                _str(data, "retired_at"),
            )
        case "role_held":
            return RoleHeld(
                _str(data, "role_id"),
                _member(data, "holder", RoleHolder),
                _operator(data, "by"),
                _str(data, "held_at"),
            )
        case "ownership_changed":
            return OwnershipChanged(
                _str(data, "role_id"),
                _optional_str(data, "owner"),
                _optional_str(data, "previous"),
                _member(data, "cause", OwnershipCause),
                _address(data, "by"),
                _optional_str(data, "entry_id"),
                _str(data, "changed_at"),
                _optional_str(data, "back_to"),
            )
        case "role_noted":
            return RoleNoted(
                _str(data, "role_id"),
                _str(data, "entry_id"),
                _str(data, "line_id"),
                _member(data, "kind", RoleNoteKind),
                _str(data, "body"),
                _str(data, "workdir_commit"),
                _str(data, "daemon_commit"),
                _str(data, "noted_at"),
            )
        case "brief_set":
            return BriefSet(
                _str(data, "line_id"),
                _str(data, "task"),
                _str(data, "decisions"),
                tuple(_list(data, "artifacts", str)),
                _str(data, "next_action"),
                _str(data, "workdir_commit"),
                _str(data, "daemon_commit"),
                _str(data, "set_at"),
            )
        case "budget_stated":
            return BudgetStated(
                _str(data, "provider"),
                _optional_int(data, "remaining_percent"),
                _optional_str(data, "resets_at"),
                _member(data, "source", BudgetSource),
                _str(data, "note"),
                _str(data, "stated_at"),
                _optional_str(data, "window"),
            )
        case "budget_handoff_requested":
            return BudgetHandoffRequested(
                _str(data, "provider"),
                _str(data, "role_id"),
                _int(data, "remaining_percent"),
                _int(data, "threshold"),
                _str(data, "requested_at"),
            )
        case "lease_acquired":
            return LeaseAcquired(
                _str(data, "name"),
                _str(data, "line_id"),
                _str(data, "acquired_at"),
                _str(data, "expires"),
            )
        case "lease_released":
            return LeaseReleased(
                _str(data, "name"),
                _str(data, "line_id"),
                _member(data, "cause", LeaseEnd),
            )
        case "run_started":
            return RunStarted(
                _str(data, "run_id"),
                _str(data, "line_id"),
                _optional_str(data, "role_id"),
                _optional_str(data, "previous"),
                _str(data, "command"),
                tuple(_list(data, "argv", str)),
                _str(data, "workdir"),
                _str(data, "output_file"),
                _int(data, "pid"),
                _str(data, "process_started"),
                _str(data, "started_at"),
            )
        case "run_ended":
            return RunEnded(
                _str(data, "run_id"),
                _member(data, "status", RunStatus),
                _optional_int(data, "exit_code"),
                _optional_str(data, "output_digest"),
                _optional_int(data, "output_bytes"),
                _str(data, "note"),
                _str(data, "ended_at"),
            )
        case "run_cleared":
            return RunCleared(
                _str(data, "run_id"),
                _member(data, "cause", RunClearance),
                _optional_str(data, "operator_id"),
                _str(data, "cleared_at"),
            )
        case "peer_declared":
            return PeerDeclared(
                _str(data, "peer_id"), _str(data, "transport"), _str(data, "policy")
            )
        case "peer_removed":
            return PeerRemoved(_str(data, "peer_id"))
        case "peer_keyed":
            return PeerKeyed(_str(data, "peer_id"), _str(data, "token_sha256"))
        case "peer_opened":
            return PeerOpened(
                _str(data, "peer_id"), _str(data, "scope"), _str(data, "opened_at")
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


def _optional_int(data: dict[str, object], name: str) -> int | None:
    value = data[name]
    if value is None:
        return None
    if type(value) is not int:
        raise DecodeError(f"{name} is not a whole number or null")
    return value


def _bool(data: dict[str, object], name: str) -> bool:
    value = data[name]
    if type(value) is not bool:
        raise DecodeError(f"{name} is not true or false")
    return value


def _optional_bool(data: dict[str, object], name: str) -> bool | None:
    value = data[name]
    if value is None:
        return None
    if type(value) is not bool:
        raise DecodeError(f"{name} is not true, false or null")
    return value


def _optional_str(data: dict[str, object], name: str) -> str | None:
    value = data[name]
    if value is None:
        return None
    if not isinstance(value, str):
        raise DecodeError(f"{name} is not a string or null")
    return value


def _request_wire(request: RequestStamp | None) -> dict[str, object] | None:
    if request is None:
        return None
    return {"key": request.key, "digest": request.digest}


def _request(data: dict[str, object]) -> RequestStamp | None:
    """The request stamp in ``data``, or ``None`` when it has none."""
    value = data["request"]
    if value is None:
        return None
    stamp = _object(value, "the request", ("digest", "key"))
    return RequestStamp(_str(stamp, "key"), _str(stamp, "digest"))


def _address(data: dict[str, object], name: str) -> LocalAddress:
    return parse_address(_str(data, name))


def _operator(data: dict[str, object], name: str) -> OperatorAddress:
    address = parse_address(_str(data, name))
    if not isinstance(address, OperatorAddress):
        raise DecodeError(f"{name} is not an operator address")
    return address


def _any_address(data: dict[str, object], name: str) -> Address:
    return parse_any_address(_str(data, name))


def _sender(data: dict[str, object], name: str) -> Sender:
    """A sender as the log records it, which may be the harness. The log is the
    daemon's own, so this is the one decoding that may construct the harness."""
    return parse_sender(_str(data, name))


def _remote_address(data: dict[str, object], name: str) -> RemoteAddress:
    address = parse_any_address(_str(data, name))
    if not isinstance(address, RemoteAddress):
        raise DecodeError(f"{name} is not a remote address")
    return address


def _member[
    T: (
        LineState,
        HoldCause,
        MessageKind,
        AgentAccess,
        OwnershipCause,
        RoleNoteKind,
        BudgetSource,
        FailoverPolicy,
        ReturnCode,
        RoleHolder,
        LeaseEnd,
        RunStatus,
        RunClearance,
    )
](data: dict[str, object], name: str, kind: type[T]) -> T:
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
