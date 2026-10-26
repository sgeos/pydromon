"""The Dromon data model, as events and the state they build.

Every change to the daemon's durable state is one event. State is never written
directly. It is rebuilt by applying events in order, so the event list is the
specification of the data model. ``docs/protocol/EVENT_LOG.md`` is normative and this
module follows it.

Parties. Session lines, operators and external agents are all addresses. An operator
question is a message from a line to an operator, and the answer is the operator's
reply, so questions travel by the same path as every other message. There may be any
number of operators, each addressed separately. An external agent is a harness outside
the daemon, such as another Claude Code session, registered with an access level and a
token hash, and its messages are untrusted data like a line's.

Trust. The model checks that each event is well formed and applicable to the current
state. It does not authenticate anyone. Attribution, such as which operator sent a
reply, is the daemon's responsibility before it appends an event.

Porting note. Events are frozen plain records, the state is maps of frozen records, and
each event has one explicit check, so each maps onto a Rust enum and a ``match``.
"""

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, assert_never

ID_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
"""Identifiers and engine names. ASCII only, so every port compares them bytewise."""

OPERATOR_PREFIX: Final = "@"
"""Marks an operator address. It never appears in an identifier, so a line address and
an operator address cannot be confused even when their identifiers are equal."""

AGENT_PREFIX: Final = "&"
"""Marks an external agent's address, distinct from both other kinds."""

TOKEN_HASH: Final = re.compile(r"[0-9a-f]{64}")
"""A SHA-256 digest in lowercase hexadecimal, the only form a token is stored in."""

MAX_TEXT_BYTES: Final = 1024 * 1024
"""Upper bound on the UTF-8 size of a message body."""

MAX_CONFIG_BYTES: Final = 256 * 1024
"""Upper bound on the UTF-8 size of a line's stored tool configuration."""

MAX_SESSION_BYTES: Final = 256
"""Upper bound on an engine's session identifier."""

MAX_CHOICES: Final = 16
MAX_CHOICE_BYTES: Final = 1024

MAX_WAKE_NOTE_BYTES: Final = 2000
"""Upper bound on the UTF-8 size of a scheduled wake's note."""

DUE_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"
"""A scheduled wake's due time, Coordinated Universal Time to the second, such as
``2026-10-26T09:30:00Z``. One fixed form, so every port compares it the same way."""

WITHHELD_WAKE_NOTE: Final = (
    "Your scheduled wake fell due. Its note was withheld, because it contains a term"
    " from the project's disclosure list."
)
"""The body a scheduled wake fires with in place of a note that the project's
disclosure list, read again since the wake was scheduled, now covers. Fixed, so it
never reveals the term."""

DUE_PATTERN: Final = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
)


class InvalidField(ValueError):
    """A record field is outside the data model, such as a malformed identifier."""


class EventRejected(ValueError):
    """A well-formed event does not apply to the current state."""


def check_id(name: str, value: object) -> None:
    """Raise ``InvalidField`` unless ``value`` is a valid identifier."""
    if not isinstance(value, str) or ID_PATTERN.fullmatch(value) is None:
        raise InvalidField(f"{name} is not a valid identifier: {value!r}")


def check_optional_id(name: str, value: object) -> None:
    """Raise ``InvalidField`` unless ``value`` is ``None`` or a valid identifier."""
    if value is not None:
        check_id(name, value)


def check_text(name: str, value: object, limit: int = MAX_TEXT_BYTES) -> None:
    """Raise ``InvalidField`` unless ``value`` is encodable text within ``limit`` bytes.

    Lone surrogates are rejected because they have no UTF-8 encoding, and a port reading
    the log as UTF-8 could not represent them.
    """
    if not isinstance(value, str):
        raise InvalidField(f"{name} is not text: {value!r}")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise InvalidField(f"{name} is not valid Unicode text") from error
    if size > limit:
        raise InvalidField(f"{name} is {size} bytes, over the limit of {limit}")


def parse_due(value: str) -> datetime:
    """The instant a due time names, or ``InvalidField`` unless it is a valid time in
    exactly the ``DUE_FORMAT`` form."""
    if DUE_PATTERN.fullmatch(value) is None:
        raise InvalidField(
            f"due is not a time of the form YYYY-MM-DDTHH:MM:SSZ: {value!r}"
        )
    try:
        return datetime.strptime(value, DUE_FORMAT).replace(tzinfo=UTC)
    except ValueError as error:
        raise InvalidField(f"due is not a valid time: {value!r}") from error


def format_due(instant: datetime) -> str:
    """``instant``, which must be aware, as a due time."""
    if instant.tzinfo is None:
        raise InvalidField("a due time needs an aware instant")
    return instant.astimezone(UTC).strftime(DUE_FORMAT)


def check_member(name: str, value: object, kind: type[StrEnum]) -> None:
    """Raise ``InvalidField`` unless ``value`` is a member of ``kind``."""
    if not isinstance(value, kind):
        raise InvalidField(f"{name} is not a {kind.__name__}: {value!r}")


def check_choices(value: object) -> None:
    """Raise ``InvalidField`` unless ``value`` holds distinct, bounded choices."""
    if not isinstance(value, tuple):
        raise InvalidField(f"choices is not a tuple: {value!r}")
    choices: tuple[object, ...] = value  # pyright: ignore[reportUnknownVariableType]
    if len(choices) > MAX_CHOICES:
        raise InvalidField(f"choices has more than {MAX_CHOICES} entries")
    for choice in choices:
        check_text("choice", choice, MAX_CHOICE_BYTES)
    if len(set(choices)) != len(choices):
        raise InvalidField("choices repeat an entry")


# Addresses.


@dataclass(frozen=True, slots=True)
class LineAddress:
    line_id: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)

    def __str__(self) -> str:
        return self.line_id


@dataclass(frozen=True, slots=True)
class OperatorAddress:
    operator_id: str

    def __post_init__(self) -> None:
        check_id("operator_id", self.operator_id)

    def __str__(self) -> str:
        return OPERATOR_PREFIX + self.operator_id


@dataclass(frozen=True, slots=True)
class AgentAddress:
    agent_id: str

    def __post_init__(self) -> None:
        check_id("agent_id", self.agent_id)

    def __str__(self) -> str:
        return AGENT_PREFIX + self.agent_id


type Address = LineAddress | OperatorAddress | AgentAddress


def parse_address(text: str) -> Address:
    """Return the address written as ``text``: ``line``, ``@operator`` or ``&agent``."""
    if text.startswith(OPERATOR_PREFIX):
        return OperatorAddress(text.removeprefix(OPERATOR_PREFIX))
    if text.startswith(AGENT_PREFIX):
        return AgentAddress(text.removeprefix(AGENT_PREFIX))
    return LineAddress(text)


def check_address(name: str, value: object) -> None:
    """Raise ``InvalidField`` unless ``value`` is an address."""
    if not isinstance(value, LineAddress | OperatorAddress | AgentAddress):
        raise InvalidField(f"{name} is not an address: {value!r}")


# Enumerations.


class LineState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPED = "stopped"


LINE_TRANSITIONS: Final[frozenset[tuple[LineState, LineState]]] = frozenset(
    {
        (LineState.IDLE, LineState.RUNNING),
        (LineState.RUNNING, LineState.IDLE),
        (LineState.IDLE, LineState.STOPPED),
        (LineState.RUNNING, LineState.STOPPED),
        (LineState.STOPPED, LineState.IDLE),
    }
)
"""Every permitted change of a line's state. A change to the same state is not one."""


class MessageKind(StrEnum):
    NOTE = "note"
    QUESTION = "question"


class AgentAccess(StrEnum):
    """What an external agent may do. Each level includes every level before it."""

    NONE = "none"
    OBSERVE = "observe"
    MESSAGE = "message"
    MANAGE = "manage"
    OPERATOR = "operator"

    @property
    def rank(self) -> int:
        return ACCESS_ORDER.index(self)


ACCESS_ORDER: Final = (
    AgentAccess.NONE,
    AgentAccess.OBSERVE,
    AgentAccess.MESSAGE,
    AgentAccess.MANAGE,
    AgentAccess.OPERATOR,
)


class QuestionStatus(StrEnum):
    OPEN = "open"
    ANSWERED = "answered"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


# Events. Each validates its own fields on construction, so an event that exists is
# well formed, whether it was built in code or decoded from the log.


@dataclass(frozen=True, slots=True)
class LineCreated:
    """A new line, with the engine model and the driver strategy it runs on."""

    line_id: str
    engine: str
    strategy: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_id("engine", self.engine)
        check_id("strategy", self.strategy)


@dataclass(frozen=True, slots=True)
class LineStateChanged:
    line_id: str
    state: LineState

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_member("state", self.state, LineState)


@dataclass(frozen=True, slots=True)
class LineConfigured:
    """A line's tool configuration, as the canonical JSON of ``dromon.lineconfig``.

    The model checks only that it is bounded text. The daemon validates its meaning
    before appending, and decodes it again when it builds the line's engine.
    """

    line_id: str
    config: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_text("config", self.config, MAX_CONFIG_BYTES)


@dataclass(frozen=True, slots=True)
class LineSession:
    """The engine session a line's next turn resumes, recorded after each turn.

    It lets a restarted daemon continue a line's conversation, so an answer reaches a
    line that still remembers its question.
    """

    line_id: str
    engine_session: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_text("engine_session", self.engine_session, MAX_SESSION_BYTES)
        if not self.engine_session:
            raise InvalidField("engine_session is empty")


@dataclass(frozen=True, slots=True)
class LineSessionCleared:
    """A line's next turn starts a new engine session, as an operator asked.

    The engine keeps the old session. Dromon only stops resuming it.
    """

    line_id: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)


@dataclass(frozen=True, slots=True)
class LineRebound:
    """A line now runs ``engine``, a model, with the driver ``strategy``, as an operator
    asked. Its identity, mailbox, configuration and overrides stay, and its next turn
    starts a new engine session."""

    line_id: str
    engine: str
    strategy: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_id("engine", self.engine)
        check_id("strategy", self.strategy)


@dataclass(frozen=True, slots=True)
class LineOverridden:
    """The settings an operator changed for a line from a client, as the canonical
    JSON of ``dromon.overrides``, replacing the line's earlier overrides whole."""

    line_id: str
    overrides: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_text("overrides", self.overrides, MAX_CONFIG_BYTES)


@dataclass(frozen=True, slots=True)
class ProjectOverridden:
    """The settings an operator changed for the whole project from a client,
    replacing the earlier project overrides whole."""

    overrides: str

    def __post_init__(self) -> None:
        check_text("overrides", self.overrides, MAX_CONFIG_BYTES)


@dataclass(frozen=True, slots=True)
class LineHeld:
    """The daemon stopped waking a line until an operator answers ``question_id``."""

    line_id: str
    question_id: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_id("question_id", self.question_id)


@dataclass(frozen=True, slots=True)
class LineReleased:
    """A held line may be woken again."""

    line_id: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)


@dataclass(frozen=True, slots=True)
class OperatorRegistered:
    operator_id: str

    def __post_init__(self) -> None:
        check_id("operator_id", self.operator_id)


@dataclass(frozen=True, slots=True)
class AgentRegistered:
    """An external agent, with its access and the SHA-256 of its token, or with an
    empty hash for an agent admitted without a token. Registering an agent again
    replaces its access and hash, and restores it if it was revoked."""

    agent_id: str
    access: AgentAccess
    token_sha256: str

    def __post_init__(self) -> None:
        check_id("agent_id", self.agent_id)
        check_member("access", self.access, AgentAccess)
        if self.token_sha256 and TOKEN_HASH.fullmatch(self.token_sha256) is None:
            raise InvalidField("token_sha256 is not a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class AgentRevoked:
    """The agent may no longer connect, send or receive."""

    agent_id: str

    def __post_init__(self) -> None:
        check_id("agent_id", self.agent_id)


@dataclass(frozen=True, slots=True)
class MessageSent:
    """A message between any two parties. A question is a message of kind question
    from a line to an operator, and a reply names the message it answers."""

    message_id: str
    sender: Address
    recipient: Address
    kind: MessageKind
    body: str
    choices: tuple[str, ...] = ()
    reply_to: str | None = None

    def __post_init__(self) -> None:
        check_id("message_id", self.message_id)
        check_address("sender", self.sender)
        check_address("recipient", self.recipient)
        check_member("kind", self.kind, MessageKind)
        check_text("body", self.body)
        check_choices(self.choices)
        check_optional_id("reply_to", self.reply_to)
        if self.kind is MessageKind.NOTE and self.choices:
            raise InvalidField("only a question has choices")


@dataclass(frozen=True, slots=True)
class MessageDelivered:
    """The message reached its recipient's mailbox, by a turn, a relay or a client."""

    message_id: str

    def __post_init__(self) -> None:
        check_id("message_id", self.message_id)


@dataclass(frozen=True, slots=True)
class MessageAcknowledged:
    """The recipient marked the message dealt with, without sending a reply.

    The sender sees the difference between an acknowledgement and a reply, because a
    sender may still be expecting a reply. A question stays open until it is answered
    or withdrawn, whether or not it is acknowledged.
    """

    message_id: str

    def __post_init__(self) -> None:
        check_id("message_id", self.message_id)


@dataclass(frozen=True, slots=True)
class MessageRead:
    """The recipient opened the message. Reading never obliges an answer."""

    message_id: str

    def __post_init__(self) -> None:
        check_id("message_id", self.message_id)


@dataclass(frozen=True, slots=True)
class WakeRequested:
    wake_id: str
    line_id: str
    message_id: str

    def __post_init__(self) -> None:
        check_id("wake_id", self.wake_id)
        check_id("line_id", self.line_id)
        check_id("message_id", self.message_id)


@dataclass(frozen=True, slots=True)
class WakeIssued:
    wake_id: str

    def __post_init__(self) -> None:
        check_id("wake_id", self.wake_id)


@dataclass(frozen=True, slots=True)
class WakeScheduled:
    """A line's one scheduled wake, replacing any earlier one it had.

    When ``due`` has passed, the daemon records ``note`` as a ``message_sent`` from the
    line to itself, under the ``message_id`` reserved here, which wakes the line as any
    mail does, or records ``WITHHELD_WAKE_NOTE`` instead when the note now discloses a
    listed term. Recording that one message is the whole of firing, so a crash either
    leaves the wake pending or fired, never both.
    """

    line_id: str
    message_id: str
    due: str
    note: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_id("message_id", self.message_id)
        check_text("due", self.due, len("YYYY-MM-DDTHH:MM:SSZ"))
        parse_due(self.due)
        check_text("note", self.note, MAX_WAKE_NOTE_BYTES)


@dataclass(frozen=True, slots=True)
class QuestionWithdrawn:
    message_id: str

    def __post_init__(self) -> None:
        check_id("message_id", self.message_id)


@dataclass(frozen=True, slots=True)
class QuestionSuperseded:
    message_id: str
    superseded_by: str

    def __post_init__(self) -> None:
        check_id("message_id", self.message_id)
        check_id("superseded_by", self.superseded_by)


type Event = (
    LineCreated
    | LineStateChanged
    | LineConfigured
    | LineSession
    | LineSessionCleared
    | LineRebound
    | LineOverridden
    | ProjectOverridden
    | LineHeld
    | LineReleased
    | OperatorRegistered
    | AgentRegistered
    | AgentRevoked
    | MessageSent
    | MessageDelivered
    | MessageRead
    | MessageAcknowledged
    | WakeRequested
    | WakeIssued
    | WakeScheduled
    | QuestionWithdrawn
    | QuestionSuperseded
)


# State records. They are frozen and replaced whole on change, so a shallow copy of the
# state maps is an independent snapshot.


@dataclass(frozen=True, slots=True)
class Line:
    engine: str
    strategy: str
    state: LineState
    config: str | None = None
    """The latest ``line_configured`` text, or ``None`` for the default."""
    engine_session: str | None = None
    """The engine session the line's next turn resumes, if one was recorded."""
    held_by: str | None = None
    """The question whose answer releases the line, while the line is held."""
    overrides: str | None = None
    """The latest ``line_overridden`` text, or ``None`` when nothing was overridden."""


@dataclass(frozen=True, slots=True)
class Agent:
    access: AgentAccess
    token_sha256: str
    """Empty for an agent admitted without a token."""
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class Message:
    sender: Address
    recipient: Address
    kind: MessageKind
    body: str
    choices: tuple[str, ...]
    reply_to: str | None
    delivered: bool = False
    read: bool = False
    acknowledged: bool = False
    status: QuestionStatus | None = None
    """The question's lifecycle, or ``None`` for a note."""
    answered_by: str | None = None
    superseded_by: str | None = None


@dataclass(frozen=True, slots=True)
class Wake:
    line_id: str
    message_id: str
    issued: bool


@dataclass(frozen=True, slots=True)
class ScheduledWake:
    """A line's pending scheduled wake, until its note is recorded."""

    message_id: str
    due: str
    note: str


@dataclass(slots=True)
class State:
    """Everything the event log records, keyed by identifier.

    Identifiers are never reused within their kind, even after a line stops or a
    question closes, so a resent event is rejected rather than applied twice.
    """

    lines: dict[str, Line] = field(default_factory=dict[str, Line])
    operators: set[str] = field(default_factory=set[str])
    messages: dict[str, Message] = field(default_factory=dict[str, Message])
    wakes: dict[str, Wake] = field(default_factory=dict[str, Wake])
    agents: dict[str, Agent] = field(default_factory=dict[str, Agent])
    project_overrides: str | None = None
    """The latest ``project_overridden`` text, or ``None``."""
    scheduled: dict[str, ScheduledWake] = field(
        default_factory=dict[str, ScheduledWake]
    )
    """Each line's pending scheduled wake, keyed by line. At most one per line."""

    def copy(self) -> "State":
        """Return an independent snapshot. The records are frozen, so maps suffice."""
        return State(
            dict(self.lines),
            set(self.operators),
            dict(self.messages),
            dict(self.wakes),
            dict(self.agents),
            self.project_overrides,
            dict(self.scheduled),
        )

    def open_questions(self, operator_id: str) -> list[str]:
        """The open questions addressed to ``operator_id``, in the order asked."""
        operator = OperatorAddress(operator_id)
        return [
            message_id
            for message_id, m in self.messages.items()
            if m.recipient == operator and m.status is QuestionStatus.OPEN
        ]

    def check(self, event: Event) -> None:
        """Raise ``EventRejected`` unless ``event`` applies to this state.

        Never mutates, so a caller can check, persist and only then apply.
        """
        match event:
            case LineCreated():
                if event.line_id in self.lines:
                    raise EventRejected(f"line {event.line_id} exists")
            case LineStateChanged():
                self._check_line_state(event)
            case (
                LineConfigured()
                | LineSession()
                | LineSessionCleared()
                | LineOverridden()
            ):
                self._line(event.line_id)
            case ProjectOverridden():
                pass
            case LineRebound():
                line = self._line(event.line_id)
                if (line.engine, line.strategy) == (event.engine, event.strategy):
                    raise EventRejected(
                        f"line {event.line_id} already runs {event.engine} with the"
                        f" {event.strategy} strategy"
                    )
            case LineHeld():
                if self._line(event.line_id).held_by is not None:
                    raise EventRejected(f"line {event.line_id} is held")
                question = self._open_question(event.question_id)
                if question.sender != LineAddress(event.line_id):
                    raise EventRejected(
                        f"question {event.question_id} is not from line {event.line_id}"
                    )
            case LineReleased():
                if self._line(event.line_id).held_by is None:
                    raise EventRejected(f"line {event.line_id} is not held")
            case OperatorRegistered():
                if event.operator_id in self.operators:
                    raise EventRejected(f"operator {event.operator_id} exists")
            case AgentRegistered():
                pass
            case AgentRevoked():
                agent = self.agents.get(event.agent_id)
                if agent is None or agent.revoked:
                    raise EventRejected(f"no agent {event.agent_id} to revoke")
            case MessageSent():
                self._check_message_sent(event)
            case MessageDelivered():
                if self._message(event.message_id).delivered:
                    raise EventRejected(f"message {event.message_id} was delivered")
            case MessageAcknowledged():
                message = self._message(event.message_id)
                if not message.delivered:
                    raise EventRejected(f"message {event.message_id} was not delivered")
                if message.acknowledged:
                    raise EventRejected(f"message {event.message_id} was acknowledged")
            case MessageRead():
                message = self._message(event.message_id)
                if not message.delivered:
                    raise EventRejected(f"message {event.message_id} was not delivered")
                if message.read:
                    raise EventRejected(f"message {event.message_id} was read")
            case WakeRequested():
                self._check_wake_requested(event)
            case WakeIssued():
                self._check_wake_issued(event)
            case WakeScheduled():
                self._check_wake_scheduled(event)
            case QuestionWithdrawn():
                self._open_question(event.message_id)
            case QuestionSuperseded():
                self._check_superseded(event)
            case _:
                assert_never(event)

    def apply(self, event: Event) -> None:
        """Check ``event``, then apply it. A rejection leaves the state unchanged."""
        self.check(event)
        match event:
            case LineCreated():
                self.lines[event.line_id] = Line(
                    event.engine, event.strategy, LineState.IDLE
                )
            case LineStateChanged():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, state=event.state)
            case LineConfigured():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, config=event.config)
            case LineHeld():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, held_by=event.question_id)
            case LineReleased():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, held_by=None)
            case LineSession():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(
                    line, engine_session=event.engine_session
                )
            case LineSessionCleared():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, engine_session=None)
            case LineRebound():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(
                    line,
                    engine=event.engine,
                    strategy=event.strategy,
                    engine_session=None,
                )
            case LineOverridden():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, overrides=event.overrides)
            case ProjectOverridden():
                self.project_overrides = event.overrides
            case OperatorRegistered():
                self.operators.add(event.operator_id)
            case AgentRegistered():
                self.agents[event.agent_id] = Agent(event.access, event.token_sha256)
            case AgentRevoked():
                agent = self.agents[event.agent_id]
                self.agents[event.agent_id] = replace(agent, revoked=True)
            case MessageSent():
                self._apply_message_sent(event)
            case MessageDelivered():
                message = self.messages[event.message_id]
                self.messages[event.message_id] = replace(message, delivered=True)
            case MessageRead():
                message = self.messages[event.message_id]
                self.messages[event.message_id] = replace(message, read=True)
            case MessageAcknowledged():
                message = self.messages[event.message_id]
                self.messages[event.message_id] = replace(message, acknowledged=True)
            case WakeRequested():
                self.wakes[event.wake_id] = Wake(
                    event.line_id, event.message_id, issued=False
                )
            case WakeIssued():
                wake = self.wakes[event.wake_id]
                self.wakes[event.wake_id] = replace(wake, issued=True)
            case WakeScheduled():
                self.scheduled[event.line_id] = ScheduledWake(
                    event.message_id, event.due, event.note
                )
            case QuestionWithdrawn():
                self._close(event.message_id, QuestionStatus.WITHDRAWN)
            case QuestionSuperseded():
                self._close(
                    event.message_id,
                    QuestionStatus.SUPERSEDED,
                    superseded_by=event.superseded_by,
                )
            case _:
                assert_never(event)

    # Lookups that turn a missing identifier into a rejection.

    def _line(self, line_id: str) -> Line:
        line = self.lines.get(line_id)
        if line is None:
            raise EventRejected(f"no line {line_id}")
        return line

    def _live_line(self, line_id: str) -> Line:
        line = self._line(line_id)
        if line.state is LineState.STOPPED:
            raise EventRejected(f"line {line_id} is stopped")
        return line

    def _require_party(self, address: Address) -> None:
        match address:
            case LineAddress():
                self._line(address.line_id)
            case OperatorAddress():
                if address.operator_id not in self.operators:
                    raise EventRejected(f"no operator {address.operator_id}")
            case AgentAddress():
                agent = self.agents.get(address.agent_id)
                if agent is None or agent.revoked:
                    raise EventRejected(f"no agent {address.agent_id}")
            case _:
                assert_never(address)

    def _message(self, message_id: str) -> Message:
        message = self.messages.get(message_id)
        if message is None:
            raise EventRejected(f"no message {message_id}")
        return message

    def _open_question(self, message_id: str) -> Message:
        message = self._message(message_id)
        if message.kind is not MessageKind.QUESTION:
            raise EventRejected(f"message {message_id} is not a question")
        if message.status is not QuestionStatus.OPEN:
            raise EventRejected(f"question {message_id} is {message.status}")
        return message

    # Per-event checks.

    def _check_line_state(self, event: LineStateChanged) -> None:
        line = self._line(event.line_id)
        if (line.state, event.state) not in LINE_TRANSITIONS:
            raise EventRejected(
                f"line {event.line_id} cannot go from {line.state} to {event.state}"
            )

    def _reserver(self, message_id: str) -> str | None:
        """The line whose pending scheduled wake reserves ``message_id``, if any."""
        for line_id, pending in self.scheduled.items():
            if pending.message_id == message_id:
                return line_id
        return None

    def _is_firing(self, event: MessageSent) -> bool:
        """Whether ``event`` is the note of its sender's pending scheduled wake, the
        only message a party may send itself."""
        if not isinstance(event.sender, LineAddress) or event.sender != event.recipient:
            return False
        pending = self.scheduled.get(event.sender.line_id)
        return (
            pending is not None
            and pending.message_id == event.message_id
            and event.kind is MessageKind.NOTE
            and event.body in (pending.note, WITHHELD_WAKE_NOTE)
            and event.reply_to is None
        )

    def _check_message_sent(self, event: MessageSent) -> None:
        if event.message_id in self.messages:
            raise EventRejected(f"message {event.message_id} exists")
        self._require_party(event.sender)
        self._require_party(event.recipient)
        firing = self._is_firing(event)
        if event.sender == event.recipient and not firing:
            raise EventRejected(f"{event.sender} cannot message itself")
        reserver = self._reserver(event.message_id)
        if reserver is not None and not firing:
            raise EventRejected(
                f"message {event.message_id} is reserved by line {reserver}'s"
                " scheduled wake"
            )
        if event.kind is MessageKind.QUESTION:
            if not isinstance(event.sender, LineAddress):
                raise EventRejected("only a line asks a question")
            if not isinstance(event.recipient, OperatorAddress):
                raise EventRejected("a question is addressed to an operator")
            self._live_line(event.sender.line_id)
        if event.reply_to is not None:
            self._check_reply(event, event.reply_to)

    def _check_reply(self, event: MessageSent, reply_to: str) -> None:
        parent = self._message(reply_to)
        if (parent.sender, parent.recipient) != (event.recipient, event.sender):
            raise EventRejected(
                f"a reply to {reply_to} must go from {parent.recipient}"
                f" to {parent.sender}"
            )
        if parent.kind is MessageKind.QUESTION:
            if parent.status is not QuestionStatus.OPEN:
                raise EventRejected(f"question {reply_to} is {parent.status}")
            if event.kind is MessageKind.QUESTION:
                raise EventRejected(f"an answer to {reply_to} cannot be a question")

    def _check_wake_requested(self, event: WakeRequested) -> None:
        if event.wake_id in self.wakes:
            raise EventRejected(f"wake {event.wake_id} exists")
        self._live_line(event.line_id)
        message = self._message(event.message_id)
        if message.recipient != LineAddress(event.line_id):
            raise EventRejected(
                f"message {event.message_id} is not for line {event.line_id}"
            )

    def _check_wake_issued(self, event: WakeIssued) -> None:
        wake = self.wakes.get(event.wake_id)
        if wake is None:
            raise EventRejected(f"no wake {event.wake_id}")
        if wake.issued:
            raise EventRejected(f"wake {event.wake_id} was issued")
        line = self._line(wake.line_id)
        if line.state is not LineState.IDLE:
            raise EventRejected(f"line {wake.line_id} is {line.state}, not idle")

    def _check_wake_scheduled(self, event: WakeScheduled) -> None:
        self._line(event.line_id)
        if event.message_id in self.messages:
            raise EventRejected(f"message {event.message_id} exists")
        reserver = self._reserver(event.message_id)
        if reserver is not None and reserver != event.line_id:
            raise EventRejected(
                f"message {event.message_id} is reserved by line {reserver}'s"
                " scheduled wake"
            )

    def _check_superseded(self, event: QuestionSuperseded) -> None:
        old = self._open_question(event.message_id)
        if event.superseded_by == event.message_id:
            raise EventRejected(f"question {event.message_id} cannot supersede itself")
        new = self._open_question(event.superseded_by)
        if new.sender != old.sender:
            raise EventRejected(
                f"question {event.superseded_by} is from another line than"
                f" {event.message_id}"
            )

    # Mutations.

    def _apply_message_sent(self, event: MessageSent) -> None:
        if self._is_firing(event) and isinstance(event.sender, LineAddress):
            del self.scheduled[event.sender.line_id]
        status = QuestionStatus.OPEN if event.kind is MessageKind.QUESTION else None
        self.messages[event.message_id] = Message(
            event.sender,
            event.recipient,
            event.kind,
            event.body,
            event.choices,
            event.reply_to,
            status=status,
        )
        if event.reply_to is not None:
            parent = self.messages[event.reply_to]
            if parent.status is QuestionStatus.OPEN:
                self.messages[event.reply_to] = replace(
                    parent, status=QuestionStatus.ANSWERED, answered_by=event.message_id
                )

    def _close(
        self,
        message_id: str,
        status: QuestionStatus,
        *,
        superseded_by: str | None = None,
    ) -> None:
        question = self.messages[message_id]
        self.messages[message_id] = replace(
            question, status=status, superseded_by=superseded_by
        )
