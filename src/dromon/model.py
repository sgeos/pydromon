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
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, assert_never

ID_PATTERN: Final = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
"""Identifiers. ASCII only, so every port compares them bytewise."""

MODEL_PATTERN: Final = re.compile(
    r"[a-z0-9][a-z0-9._-]{0,63}(:[a-z0-9][a-z0-9._/-]{0,127})?"
)
"""The model a line runs, the ``engine`` field of ``line_created`` and ``line_rebound``.
It is an identifier, or an engine-qualified name as ``dromon.driver.ENGINE_OF`` gives
it, an identifier, one colon and the harness's own identifier, which may also hold
``/``, as Pi's ``openai/gpt-6-luna`` does.

The colon and the slash are needed so that a line can be created on Pi, Cursor Agent or
Antigravity at all, which the identifier pattern refused until 2026-10-31. Nothing else
changes. The name stays ASCII and lowercase, so every port still compares it bytewise,
it holds at most one colon, so the engine is the text before it, and it never begins
with ``@``, ``&`` or ``!``, so it never reads as an address.

**Assumption.** No harness identifier in the packaged catalogue holds an uppercase
letter or a character outside this set. A model that did would need this pattern
widened, which is a change to the log's grammar."""

OPERATOR_PREFIX: Final = "@"
"""Marks an operator address. It never appears in an identifier, so a line address and
an operator address cannot be confused even when their identifiers are equal."""

AGENT_PREFIX: Final = "&"
"""Marks an external agent's address, distinct from both other kinds."""

TOKEN_HASH: Final = re.compile(r"[0-9a-f]{64}")
"""A SHA-256 digest in lowercase hexadecimal, the only form a token is stored in."""

MAX_TEXT_BYTES: Final = 1024 * 1024
"""Upper bound on the UTF-8 size of a message body."""

MAX_OPEN_ISSUES_PER_LINE: Final = 5
"""At most five unclosed issues from one line may burden an operator."""

ISSUE_RATE_WINDOW_MESSAGES: Final = 20
MAX_ISSUES_PER_LINE_IN_WINDOW: Final = 3
"""At most three issues in a line's last twenty sent messages, including the new one.
This replay-deterministic rate limit does not depend on wall-clock time, so it slows a
line by messages, not by time. ``MAX_OPEN_ISSUES_PER_LINE`` is the bound that holds,
since only an operator frees an open slot."""

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


def check_model(name: str, value: object) -> None:
    """Raise ``InvalidField`` unless ``value`` is a valid model name."""
    if not isinstance(value, str) or MODEL_PATTERN.fullmatch(value) is None:
        raise InvalidField(f"{name} is not a valid model name: {value!r}")


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


type LocalAddress = LineAddress | OperatorAddress | AgentAddress
"""A party of this daemon."""

REMOTE_SEPARATOR: Final = ":"
"""Separates a peer's name from the address of one of its parties. It never appears in
an identifier, so a remote address splits at its first colon without ambiguity."""

LONGEST_EXPIRY_HOURS: Final = 24 * 90
"""The longest a peer's ``expire_hours`` may be, and so the longest any Dromon daemon
resends one message to a peer."""
CLOCK_MARGIN_HOURS: Final = 24
"""Allowance for the difference between two daemons' wall clocks."""
RECEIVED_KEY_HOURS: Final = LONGEST_EXPIRY_HOURS + CLOCK_MARGIN_HOURS
"""How long the key of a received message is kept. Decided by the operator on
2026-10-28, keys older than the longest expiry are dropped, so the set is bounded by
the mail one window can bring. A sender stops resending a message once it expires, so
an older key would never see a resend. **Inference.** A sender whose clock runs more
than the margin behind, or a daemon that is not Dromon, could resend past the window,
and that resend would be recorded as a new message."""
MAX_OUTBOX: Final = 1000
"""The most messages that may wait for one peer. A send past it is refused at once."""


@dataclass(frozen=True, slots=True)
class RemoteAddress:
    """A party of a peer, written ``peer:party`` with the party as the peer writes it.

    It begins with the peer's identifier, never with ``@`` or ``&``, so a remote
    operator can never be read as a local one. One level only, since the party is
    always local to the peer, so no address routes through a second peer.
    """

    peer_id: str
    party: LocalAddress

    def __post_init__(self) -> None:
        check_id("peer_id", self.peer_id)
        check_local_address("party", self.party)

    def __str__(self) -> str:
        return f"{self.peer_id}{REMOTE_SEPARATOR}{self.party}"


type Address = LocalAddress | RemoteAddress

HARNESS_TEXT: Final = "!dromon"
"""How the harness's own address is written. An identifier never begins with ``!``, so
no other address reads as it."""


@dataclass(frozen=True, slots=True)
class HarnessAddress:
    """The harness itself, as the sender of the notes it writes on its own account,
    such as a failover or an outage of a peer. Decided by the operator on 2026-10-28,
    since a note recorded as from a line the line did not write misattributes it.

    No party can claim it. ``parse_address`` and ``parse_any_address`` refuse its text,
    so no client, line, agent or peer input can name it, and only the daemon's own code
    and the decoding of its log construct it. It sends notes and never receives mail,
    so nothing replies to it."""

    def __str__(self) -> str:
        return HARNESS_TEXT


HARNESS: Final = HarnessAddress()

type Sender = Address | HarnessAddress
"""Who a message is from. Only the harness sends without being addressable."""


def parse_address(text: str) -> LocalAddress:
    """Return the local address written as ``text``: ``line``, ``@operator`` or
    ``&agent``. A remote address is refused, as text that is not an address, and so is
    the harness's address, which no input may name."""
    if text == HARNESS_TEXT:
        raise InvalidField("the harness's address is reserved to the harness")
    if text.startswith(OPERATOR_PREFIX):
        return OperatorAddress(text.removeprefix(OPERATOR_PREFIX))
    if text.startswith(AGENT_PREFIX):
        return AgentAddress(text.removeprefix(AGENT_PREFIX))
    return LineAddress(text)


def parse_any_address(text: str) -> Address:
    """Return the address written as ``text``, local or ``peer:party``."""
    peer_id, separator, party = text.partition(REMOTE_SEPARATOR)
    if not separator:
        return parse_address(text)
    return RemoteAddress(peer_id, parse_address(party))


def parse_sender(text: str) -> Sender:
    """Return the sender written as ``text``, which may be the harness. Only the
    decoding of the daemon's own log calls it, never the decoding of any input."""
    if text == HARNESS_TEXT:
        return HARNESS
    return parse_any_address(text)


def check_address(name: str, value: object) -> None:
    """Raise ``InvalidField`` unless ``value`` is an address."""
    if not isinstance(
        value, LineAddress | OperatorAddress | AgentAddress | RemoteAddress
    ):
        raise InvalidField(f"{name} is not an address: {value!r}")


def check_sender(name: str, value: object) -> None:
    """Raise ``InvalidField`` unless ``value`` is an address or the harness."""
    if not isinstance(value, HarnessAddress):
        check_address(name, value)


def check_local_address(name: str, value: object) -> None:
    """Raise ``InvalidField`` unless ``value`` is a local address."""
    if not isinstance(value, LineAddress | OperatorAddress | AgentAddress):
        raise InvalidField(f"{name} is not a local address: {value!r}")


def check_operator_address(name: str, value: object) -> None:
    """Raise ``InvalidField`` unless ``value`` is an operator address."""
    if not isinstance(value, OperatorAddress):
        raise InvalidField(f"{name} is not an operator address: {value!r}")


def check_remote_address(name: str, value: object) -> None:
    """Raise ``InvalidField`` unless ``value`` is a remote address."""
    if not isinstance(value, RemoteAddress):
        raise InvalidField(f"{name} is not a remote address: {value!r}")


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
    ISSUE = "issue"
    """A harness or operator-experience issue a line raises with an operator unasked.
    It asks no answer, and it is open until the operator replies to it or
    acknowledges it. ``docs/protocol/LINE_CONFIG.md`` records why it is a kind of
    mail rather than a tracker. **Rollback hazard.** A daemon older than this kind
    refuses a log that holds one, since its codec knows only notes and questions."""


class ReturnCode(StrEnum):
    """Why mail between daemons was refused or returned. Fixed, so a peer cannot put
    its own text into a sender's prompt by refusing."""

    UNKNOWN_PEER = "unknown_peer"
    REFUSED = "refused"
    TOO_LARGE = "too_large"
    UNKNOWN_REPLY = "unknown_reply"
    DISCLOSURE = "disclosure"
    EXPIRED = "expired"
    MALFORMED = "malformed"
    CONFLICT = "conflict"
    FULL = "full"


RETURN_MEANINGS: Final[dict[ReturnCode, str]] = {
    ReturnCode.UNKNOWN_PEER: "its peer is no longer declared",
    ReturnCode.REFUSED: (
        "the policy of the peer or of this daemon refuses its sender or recipient,"
        " or the recipient does not exist there"
    ),
    ReturnCode.TOO_LARGE: "its body exceeds the limit for the peer",
    ReturnCode.UNKNOWN_REPLY: "the peer does not know the message it replies to",
    ReturnCode.DISCLOSURE: "it now holds a term of the disclosure list",
    ReturnCode.EXPIRED: "it waited too long and no copy of it reached the peer",
    ReturnCode.MALFORMED: "the peer found an address in it malformed",
    ReturnCode.CONFLICT: "the peer holds a different message under its identifier",
    ReturnCode.FULL: "the recipient has as much mail waiting as the peer allows",
}
"""What each code tells the sender of returned mail, in Dromon's own words."""


def returned_note(message_id: str, recipient: "Address", code: ReturnCode) -> str:
    """The note a line receives when its mail to a peer is returned."""
    return (
        f"Your message {message_id} to {recipient} was returned undelivered with the"
        f" code {code.value}, which means {RETURN_MEANINGS[code]}. It was not sent"
        " again."
    )


def unconfirmed_note(message_id: str, recipient: "Address") -> str:
    """The note a line receives when its mail to a peer expired after a copy of it
    may have reached the peer."""
    return (
        f"Your message {message_id} to {recipient} expired after a copy of it may have"
        " reached the peer, so it may or may not have been delivered. It was not sent"
        " again."
    )


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
        check_model("engine", self.engine)
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


MAX_COST_MICRO_USD: Final = 10**15
"""The largest running cost total an event records, in millionths of a dollar."""


@dataclass(frozen=True, slots=True)
class LineCostMarked:
    """The running cost total an engine reported for a line's session after its last
    turn, in whole millionths of a dollar, since the log admits no fractions. It lets
    a restarted daemon measure the next turn from there, because the engine restores
    a resumed session's total. One is recorded after each turn whose engine reports a
    total."""

    line_id: str
    engine_session: str
    total_micro_usd: int

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_text("engine_session", self.engine_session, MAX_SESSION_BYTES)
        if not self.engine_session:
            raise InvalidField("engine_session is empty")
        total = self.total_micro_usd
        if type(total) is not int or not 0 <= total <= MAX_COST_MICRO_USD:
            raise InvalidField(
                f"total_micro_usd must be a whole number from 0 to {MAX_COST_MICRO_USD}"
            )


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
        check_model("engine", self.engine)
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
class LineHibernated:
    """An operator hibernated the line. **Decided by the operator on 2026-10-27.**
    The line keeps its mail, its session and its pending scheduled wake, and starts
    no turn until it is awakened. A turn already running finishes. The operator
    renamed it from mute the same day, and ``line_muted`` still decodes as this."""

    line_id: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)


@dataclass(frozen=True, slots=True)
class LineAwakened:
    """A hibernated line may be woken again, by the mail and wakes it kept.
    ``line_unmuted`` still decodes as this."""

    line_id: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)


@dataclass(frozen=True, slots=True)
class MessagePrioritized:
    """A note to a line is priority mail, which ends the line's running turn at its
    next safe point and leads the turn that follows. Appended right after the
    ``message_sent`` it marks, while the note is undelivered."""

    message_id: str

    def __post_init__(self) -> None:
        check_id("message_id", self.message_id)


MAX_TURN_KEYS: Final = 64
"""The most correlation keys one ``turn_correlated`` lists."""

MAX_TURN_KEY_COUNT: Final = 1_000_000
"""The largest count of keys one turn may record as carried."""

MAX_CORRELATIONS: Final = 4096
"""The most correlation keys the state totals. The key used least recently is
dropped to make room for a new one, so a long-running daemon stays bounded."""


@dataclass(frozen=True, slots=True)
class MessageCorrelated:
    """An external agent's note to a line carries a correlation key, which names the
    agent's own task, so the turns the note starts can be totalled under it.
    Appended right after the ``message_sent`` it marks, while the note is
    undelivered. **Queue of 2026-10-28, item A4.** The key has the form of an
    identifier."""

    message_id: str
    key: str

    def __post_init__(self) -> None:
        check_id("message_id", self.message_id)
        check_id("key", self.key)


@dataclass(frozen=True, slots=True)
class TurnCorrelated:
    """A line's turn ended that delivered at least one correlated note, with the
    keys those notes carried and the turn's cost in whole millionths of a dollar,
    or ``None`` when the engine reported none. ``key_count`` is how many distinct
    keys the turn carried, so a reader sees that a turn counted under two keys cost
    its amount once, not twice. ``keys`` lists them sorted, the first
    ``MAX_TURN_KEYS`` when there were more."""

    line_id: str
    keys: tuple[str, ...]
    key_count: int
    cost_micro_usd: int | None

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        if not 1 <= len(self.keys) <= MAX_TURN_KEYS:
            raise InvalidField(f"keys must list from 1 to {MAX_TURN_KEYS} keys")
        for key in self.keys:
            check_id("key", key)
        if list(self.keys) != sorted(set(self.keys)):
            raise InvalidField("keys must be distinct and sorted")
        count = self.key_count
        if type(count) is not int or not len(self.keys) <= count <= MAX_TURN_KEY_COUNT:
            raise InvalidField("key_count must count at least the keys listed")
        if count > len(self.keys) and len(self.keys) < MAX_TURN_KEYS:
            raise InvalidField(
                f"keys must list every key when the turn carried at most"
                f" {MAX_TURN_KEYS}"
            )
        cost = self.cost_micro_usd
        if cost is not None and (
            type(cost) is not int or not 0 <= cost <= MAX_COST_MICRO_USD
        ):
            raise InvalidField(
                f"cost_micro_usd must be null or a whole number from 0 to"
                f" {MAX_COST_MICRO_USD}"
            )


@dataclass(frozen=True, slots=True)
class LineTurnEnded:
    """The last checked-out commit observed as a line's turn ended."""

    line_id: str
    workdir_commit: str | None
    unreported: bool | None = None
    """``True`` when the engine returned no report for the turn, as when its prompt
    could not be composed, a check before the engine failed, the engine raised or the
    turn was cancelled. The daemon then cannot say that the prompt reached the engine.
    ``None`` otherwise, and in a log written before the field. **Round 8 review, ON1,
    of 2026-10-30.** Such a turn retires no own note, as
    ``State.reported_turn_ends`` says. ``False`` is refused, so the fact has one
    encoding and the wire omits it when null."""

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        if self.workdir_commit is not None:
            check_commit("workdir_commit", self.workdir_commit)
        if self.unreported is not None and self.unreported is not True:
            raise InvalidField("unreported must be true or null")


MAX_DISCLOSURE_CHECKS: Final = 10
"""The most ``check_disclosure`` calls one line may make within
``DISCLOSURE_CHECK_WINDOW_SECONDS``, and the most check times the state keeps for a
line. **Proposed for item B2 of 2026-10-28.** Each pass or fail answer says whether a
text holds a listed term, so an unbounded tool would let a line test candidate words
one by one and recover the list."""

DISCLOSURE_CHECK_WINDOW_SECONDS: Final = 3600
"""The window ``MAX_DISCLOSURE_CHECKS`` applies to."""

MAX_DISCLOSURE_MATCHES: Final = 1_000_000
"""The largest count of matching lines one ``disclosure_checked`` records."""


@dataclass(frozen=True, slots=True)
class DisclosureChecked:
    """A line checked a text or a file against the project's disclosure list, at
    ``checked_at``, and ``matched_lines`` of its lines held a listed term, or a line
    tool refused a text for disclosure, which the operator decided on 2026-10-28
    counts as a check. Neither the text, its name nor any term is recorded.
    **Item B2 of 2026-10-28.** The
    record makes the rate limit survive a restart of the daemon, and shows an
    operator how often each line asked."""

    line_id: str
    checked_at: str
    matched_lines: int

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_instant("checked_at", self.checked_at)
        count = self.matched_lines
        if type(count) is not int or not 0 <= count <= MAX_DISCLOSURE_MATCHES:
            raise InvalidField(
                f"matched_lines must be a whole number from 0 to"
                f" {MAX_DISCLOSURE_MATCHES}"
            )


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


PEER_TRANSPORTS: Final = frozenset({"unix", "tls"})
"""The transports of ``docs/architecture/FEDERATION.md``. Phase 1 builds ``unix``."""

MAX_PEER_POLICY_BYTES: Final = 64 * 1024
"""Upper bound on a peer's recorded policy, which lists at most three times 256
addresses of at most 66 bytes each, with room for the rest."""


@dataclass(frozen=True, slots=True)
class PeerDeclared:
    """A peer daemon that the project file declares, or a change to its transport or
    policy. Recorded at start from the project file. The policy is canonical JSON text
    that the federation module writes and reads, and never holds a token, so the model
    keeps it whole and does not interpret it."""

    peer_id: str
    transport: str
    policy: str

    def __post_init__(self) -> None:
        check_id("peer_id", self.peer_id)
        if self.transport not in PEER_TRANSPORTS:
            raise InvalidField(f"transport is not a peer transport: {self.transport!r}")
        check_text("policy", self.policy, MAX_PEER_POLICY_BYTES)


@dataclass(frozen=True, slots=True)
class PeerRemoved:
    """The project file no longer declares the peer. Its token digest is cleared, so a
    peer declared again needs a new token."""

    peer_id: str

    def __post_init__(self) -> None:
        check_id("peer_id", self.peer_id)


@dataclass(frozen=True, slots=True)
class PeerKeyed:
    """The SHA-256 of the token this daemon issued for the peer to present, or an empty
    digest that revokes the token. Only the digest is ever stored."""

    peer_id: str
    token_sha256: str

    def __post_init__(self) -> None:
        check_id("peer_id", self.peer_id)
        if self.token_sha256 and TOKEN_HASH.fullmatch(self.token_sha256) is None:
            raise InvalidField("token_sha256 is not a SHA-256 hex digest")


MAX_REQUEST_KEY_CHARS: Final = 128
"""The longest request key a keyed tool call may give, in characters."""
MAX_REQUEST_KEYS_PER_PARTY: Final = 256
"""How many request keys the state keeps for each party and tool, the oldest
forgotten first."""
REQUEST_SEND: Final = "send_message"
REQUEST_ASK: Final = "ask_operator"
REQUEST_WAKE: Final = "wake_after"
"""The tools whose keyed calls the state remembers, named as the line tools are."""


@dataclass(frozen=True, slots=True)
class RequestStamp:
    """The ``request_key`` a keyed call of ``send_message``, ``ask_operator`` or
    ``wake_after`` gave, with the SHA-256 digest of its other arguments. **Task of
    2026-10-29, round 5.** It is recorded in the event the call appended, so no crash
    can leave the act without its key, and a retry after a daemon restart repeats
    the first result instead of acting again."""

    key: str
    digest: str

    def __post_init__(self) -> None:
        check_text("request key", self.key)
        if (
            not 1 <= len(self.key) <= MAX_REQUEST_KEY_CHARS
            or not self.key.isprintable()
        ):
            raise InvalidField(
                f"request key must be text of 1 to {MAX_REQUEST_KEY_CHARS} printable"
                " characters"
            )
        check_text("request digest", self.digest, 64)
        if DIGEST_PATTERN.fullmatch(self.digest) is None:
            raise InvalidField("request digest must be a SHA-256 hex digest")


@dataclass(frozen=True, slots=True)
class MessageSent:
    """A message between any two parties, or a note from the harness to a line or an
    operator. A question is a message of kind question from a line to an operator, and
    a reply names the message it answers."""

    message_id: str
    sender: Sender
    recipient: Address
    kind: MessageKind
    body: str
    choices: tuple[str, ...] = ()
    reply_to: str | None = None
    request: RequestStamp | None = None
    """The request key of the ``send_message`` or ``ask_operator`` call that sent
    it, or ``None``, absent from the wire, when the call gave none."""

    def __post_init__(self) -> None:
        check_id("message_id", self.message_id)
        check_sender("sender", self.sender)
        check_address("recipient", self.recipient)
        check_member("kind", self.kind, MessageKind)
        check_text("body", self.body)
        check_choices(self.choices)
        check_optional_id("reply_to", self.reply_to)
        if self.kind is MessageKind.NOTE and self.choices:
            raise InvalidField("only a question has choices")


@dataclass(frozen=True, slots=True)
class MessageReceived:
    """A note a peer delivered, from one of its parties to a local one.

    One event records the message and its idempotency key, the peer and the peer's own
    identifier for the message, so a crash cannot record one without the other. Only
    notes cross between daemons, and ``reply_to`` names a local message, the one the
    remote sender answers. ``received_at`` dates the key, which is kept for
    ``RECEIVED_KEY_HOURS`` only, as the operator decided on 2026-10-28.
    """

    message_id: str
    sender: RemoteAddress
    recipient: LocalAddress
    body: str
    reply_to: str | None
    origin_id: str
    received_at: str
    priority_asked: bool = False
    """Whether the peer's envelope asked for priority. A ``message_prioritized``
    follows when the receiver honours it, so a note that asked and is not priority
    mail had its priority declined, which its header says."""

    def __post_init__(self) -> None:
        check_id("message_id", self.message_id)
        check_remote_address("sender", self.sender)
        check_local_address("recipient", self.recipient)
        check_text("body", self.body)
        check_optional_id("reply_to", self.reply_to)
        check_id("origin_id", self.origin_id)
        check_instant("received_at", self.received_at)


@dataclass(frozen=True, slots=True)
class MessageDelivered:
    """The message reached its recipient's mailbox, by a turn, a relay or a client.
    For a recipient of a peer, the peer accepted it."""

    message_id: str

    def __post_init__(self) -> None:
        check_id("message_id", self.message_id)


@dataclass(frozen=True, slots=True)
class MessageReturned:
    """Mail to a peer left its outbox undelivered, for the fixed reason ``code``.

    When a line sent it, the same event records a note from the line to itself under
    ``note_id``, as ``task_noted`` does, so its next turn says what was returned and
    why. Mail an operator or an agent sent has no note, and shows as returned where
    sent mail is listed.
    """

    message_id: str
    code: ReturnCode
    note_id: str | None

    def __post_init__(self) -> None:
        check_id("message_id", self.message_id)
        check_member("code", self.code, ReturnCode)
        check_optional_id("note_id", self.note_id)


@dataclass(frozen=True, slots=True)
class MessageUnconfirmed:
    """Mail to a peer left its outbox when a copy of it may have reached the peer
    without an answer, so whether it was delivered is unknown. Decision 12 of the
    federation design. ``note_id`` is as for ``MessageReturned``."""

    message_id: str
    note_id: str | None

    def __post_init__(self) -> None:
        check_id("message_id", self.message_id)
        check_optional_id("note_id", self.note_id)


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
    task_id: str | None = None
    request: RequestStamp | None = None
    """The request key of the ``wake_after`` call that scheduled it, or ``None``,
    absent from the wire, when the call gave none."""

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_id("message_id", self.message_id)
        check_text("due", self.due, len("YYYY-MM-DDTHH:MM:SSZ"))
        parse_due(self.due)
        check_text("note", self.note, MAX_WAKE_NOTE_BYTES)
        if self.task_id is not None:
            check_text("task_id", self.task_id, MAX_TASK_ID_BYTES)


type RequestedAct = MessageSent | WakeScheduled
"""An event that records the act of a keyed tool call."""


def request_slot(event: RequestedAct) -> tuple[str, str]:
    """The party and tool whose keys ``event``'s request key belongs to. A note is
    ``send_message``'s and a question ``ask_operator``'s, by the sender's address,
    and a wake is ``wake_after``'s, by its line."""
    if isinstance(event, WakeScheduled):
        return event.line_id, REQUEST_WAKE
    tool = REQUEST_ASK if event.kind is MessageKind.QUESTION else REQUEST_SEND
    return str(event.sender), tool


@dataclass(frozen=True, slots=True)
class WakeWithdrawn:
    """The daemon withdrew a line's pending scheduled wake before it fell due, so it
    never fires. Its ``message_id`` is kept, and no later message, task note or
    scheduled wake may take it. Only the daemon records it, when the wake
    ``compact_session`` scheduled has lost its purpose because an earlier turn
    already made the compaction it was for."""

    line_id: str
    message_id: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_id("message_id", self.message_id)


MAX_TASK_STATUS_BYTES: Final = 32
MAX_TASK_NOTE_BYTES: Final = 8192
"""The largest task-end note, which fits a quoted command and the head of the task's
output."""
MAX_TASK_ID_BYTES: Final = 128


@dataclass(frozen=True, slots=True)
class TaskNoted:
    """A note from a line to itself saying that a background task of its engine
    ended. An end between turns wakes the line unless its engine has already begun
    an admitted turn on it. An end during a turn or for a task the line stopped is
    recorded as delivered without a wake. Only the daemon records it, when the line's
    engine reports the end, and recording it is the whole of sending, so it needs no
    separate ``message_sent``. If a pending wake names this task, applying this event
    withdraws that wake atomically with the task note. A line may also send itself
    a note with ``message_sent``, and the daemon records ``restart_noted``."""

    line_id: str
    message_id: str
    task_id: str
    status: str
    body: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_id("message_id", self.message_id)
        check_text("task_id", self.task_id, MAX_TASK_ID_BYTES)
        check_text("status", self.status, MAX_TASK_STATUS_BYTES)
        check_text("body", self.body, MAX_TASK_NOTE_BYTES)


MAX_TASK_RECORDS: Final = 10
"""How many of each line's latest ``task_recorded`` events the state keeps."""

MAX_TASK_COMMAND_BYTES: Final = 2048
MAX_TASK_PATH_BYTES: Final = 1024
MAX_TASK_OUTPUT_BYTES: Final = 2**53
MAX_EXIT_CODE: Final = 2**31
DIGEST_PATTERN: Final = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class TaskRecorded:
    """A background task of a line's engine ended between turns, with its command,
    final status, exit code, output file and the SHA-256 digest and size of its
    output where the daemon could read it, the commit checked out where the line
    works, empty when none was read, and when the daemon learned of the end.
    **Item B3 of 2026-10-28.** It lets the other seats of a role check a gate the
    owner ran without running it again. It is recorded for every end the engine
    reports, whether or not a note wakes the line, and never wakes it."""

    line_id: str
    task_id: str
    command: str
    status: str
    exit_code: int | None
    output_file: str | None
    output_digest: str | None
    output_bytes: int | None
    workdir_commit: str
    ended_at: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_text("task_id", self.task_id, MAX_TASK_ID_BYTES)
        check_text("command", self.command, MAX_TASK_COMMAND_BYTES)
        check_text("status", self.status, MAX_TASK_STATUS_BYTES)
        code = self.exit_code
        if code is not None and (
            type(code) is not int or not -MAX_EXIT_CODE <= code < MAX_EXIT_CODE
        ):
            raise InvalidField("exit_code must be null or a 32-bit whole number")
        if self.output_file is not None:
            check_text("output_file", self.output_file, MAX_TASK_PATH_BYTES)
        digest = self.output_digest
        if digest is not None:
            check_text("output_digest", digest, 64)
            if DIGEST_PATTERN.fullmatch(digest) is None:
                raise InvalidField("output_digest must be null or a SHA-256 hex digest")
        size = self.output_bytes
        if size is not None and (
            type(size) is not int or not 0 <= size <= MAX_TASK_OUTPUT_BYTES
        ):
            raise InvalidField("output_bytes must be null or a whole number")
        if (digest is None) != (size is None):
            raise InvalidField("output_digest and output_bytes are given together")
        check_commit("workdir_commit", self.workdir_commit)
        check_instant("ended_at", self.ended_at)


MAX_RESTART_NOTE_BYTES: Final = 2048
"""The largest note that a restart ended a line's turn."""


@dataclass(frozen=True, slots=True)
class RestartNoted:
    """A note from a line to itself saying that a daemon restart ended its turn at a
    safe point, which wakes the line once the restarted daemon runs, so the line's
    work continues. Only the daemon records it, as the turn ends, and recording it is
    the whole of sending, as for ``TaskNoted``. Restart to upgrade, decided by the
    operator on 2026-10-27."""

    line_id: str
    message_id: str
    body: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_id("message_id", self.message_id)
        check_text("body", self.body, MAX_RESTART_NOTE_BYTES)


MAX_LEFT_PROCESSES: Final = 64
MAX_LEFT_COMMAND_BYTES: Final = 1024
MAX_LEFT_MORE: Final = 1_000_000
LEFT_CAUSES: Final = frozenset({"exited", "stopped", "orphaned", "detached", "swept"})
"""Why processes were left: the engine exited, Dromon stopped it, or the daemon died,
each under the ``leave_background`` stop policy, or they had left the engine's
process tree and were found by the line's process mark, or the daemon's sweep of a
launcher that had exited found them still running. **Review N1.**"""


@dataclass(frozen=True, slots=True)
class ProcessesLeft:
    """Processes a line's engine left running, with their pids and commands, which
    the line's status shows. Only the daemon records it, from a launcher's report or
    from the process mark. It is a record, and does not make the processes Dromon's
    to end."""

    line_id: str
    cause: str
    pids: tuple[int, ...]
    commands: tuple[str, ...]
    more: int = 0
    """How many more were left than are named."""

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        if self.cause not in LEFT_CAUSES:
            raise InvalidField(f"cause must be one of {sorted(LEFT_CAUSES)}")
        if len(self.pids) != len(self.commands):
            raise InvalidField("pids and commands must be as many")
        if not self.pids or len(self.pids) > MAX_LEFT_PROCESSES:
            raise InvalidField(f"pids must name 1 to {MAX_LEFT_PROCESSES} processes")
        for pid in self.pids:
            if type(pid) is not int or not 1 < pid < 2**31:
                raise InvalidField("a pid must be a whole number above 1")
        for command in self.commands:
            check_text("command", command, MAX_LEFT_COMMAND_BYTES)
        if type(self.more) is not int or not 0 <= self.more <= MAX_LEFT_MORE:
            raise InvalidField(f"more must be a whole number from 0 to {MAX_LEFT_MORE}")


MAX_STOP_NOTE_BYTES: Final = 1024


@dataclass(frozen=True, slots=True)
class StopUnverified:
    """A stop of a line's engine that Dromon could not verify was complete, which
    the line's status shows as the latest: the launcher could not read the process
    table, or exited before it finished its stop and was swept by the daemon. Only
    the daemon records it. **Review F2 and N1.**"""

    line_id: str
    note: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_text("note", self.note, MAX_STOP_NOTE_BYTES)
        if not self.note:
            raise InvalidField("note must not be empty")


MAX_WALK_LIMIT_BYTES: Final = 1024


@dataclass(frozen=True, slots=True)
class WalkLimited:
    """What the daemon's platform keeps the launcher's walk of the process table from
    reaching, which every line's status shows, or empty text where nothing is beyond
    it. Only the daemon records it, as it starts, and only when it differs from the
    log's, so a platform's gap is recorded once. **Review F1.**"""

    note: str

    def __post_init__(self) -> None:
        check_text("note", self.note, MAX_WALK_LIMIT_BYTES)


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


HANDOFF_CAUSES: Final = frozenset({"rebound", "cleared"})
HANDOFF_OUTCOMES: Final = frozenset({"given", "opted_out"})
MAX_HANDOFF_MESSAGES: Final = 64


@dataclass(frozen=True, slots=True)
class ContextHandedOff:
    """A line's first turn in a new engine session, after ``line_rebound`` or
    ``line_session_cleared``, ran with a context handoff, a selection from the line's
    mail, or ran without one because the line opted out. **Row 13 of the T3 Code
    review.** It records which messages were selected, and the size and digest of the
    text the prompt carried, so the selection can be checked against the log. Only
    the daemon records it, once the engine ran the turn, which ends the handoff."""

    line_id: str
    cause: str
    outcome: str
    message_ids: tuple[str, ...]
    chars: int
    sha256: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        if self.cause not in HANDOFF_CAUSES:
            raise InvalidField(f"cause must be one of {sorted(HANDOFF_CAUSES)}")
        if self.outcome not in HANDOFF_OUTCOMES:
            raise InvalidField(f"outcome must be one of {sorted(HANDOFF_OUTCOMES)}")
        if len(self.message_ids) > MAX_HANDOFF_MESSAGES:
            raise InvalidField(f"at most {MAX_HANDOFF_MESSAGES} messages are selected")
        for message_id in self.message_ids:
            check_id("message_id", message_id)
        if type(self.chars) is not int or not 0 <= self.chars <= MAX_TEXT_BYTES:
            raise InvalidField(
                f"chars must be a whole number from 0 to {MAX_TEXT_BYTES}"
            )
        given = self.outcome == "given"
        if given and TOKEN_HASH.fullmatch(self.sha256) is None:
            raise InvalidField("sha256 is not a SHA-256 hex digest")
        if not given and (self.sha256 or self.chars or self.message_ids):
            raise InvalidField("a handoff the line opted out of selects nothing")


# Roles, seats and ownership. ``docs/architecture/ROLES.md``, approved by the operator
# on 2026-10-27. A role is a line of work with one worktree, its seats are lines, and
# at most one seat owns the role, which is to say may write its worktree.

MAX_WORKDIR_BYTES: Final = 1024
DEFAULT_HANDOFF_SECONDS: Final = 3600
"""How long an owner asked to hand a role over has before the operator is asked
whether to recover it instead. One hour, configurable per role, as the operator
decided on 2026-10-27."""
MIN_HANDOFF_SECONDS: Final = 60
MAX_HANDOFF_SECONDS: Final = 86400
DEFAULT_HANDOFF_BELOW_PERCENT: Final = 10
"""The stated budget below which an owner is asked to prepare a handoff, as the
operator asked on 2026-10-27. **Assumption.** Ten percent is a starting value the
operator may change per role. Zero turns the planned handoff by budget off."""
MAX_ROLE_SEATS: Final = 16
"""The most seats a role's declared order names, a bound on every walk of it."""
MAX_ROLE_NOTE_BYTES: Final = 64 * 1024
"""The largest journal entry, which fits the handoff template with room to spare."""
MAX_BUDGET_NOTE_BYTES: Final = 1024
COMMIT_PATTERN: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
"""A git commit, SHA-1 or SHA-256, in lowercase hexadecimal."""


class FailoverPolicy(StrEnum):
    """What the harness does when a role's owner cannot act."""

    AUTOMATIC = "automatic"
    """The harness passes the role to the next usable seat in the declared order."""
    ASK = "ask"
    """The harness changes no owner, and an operator decides."""


class OwnershipCause(StrEnum):
    DECLARED = "declared"
    """The project file named the first owner."""
    HANDOFF = "handoff"
    """The owner passed the role to a successor, with a handoff entry."""
    RECOVERY = "recovery"
    """A seat or an operator took the role while its owner could not act."""
    OPERATOR = "operator"
    """An operator gave the role to a seat, or left it vacant."""
    RELINQUISHED = "relinquished"
    """The owner gave the role up with no successor."""
    FAILOVER = "failover"
    """The harness itself passed the role from an owner it observed unable to act to
    the next usable seat, under the role's automatic failover policy."""


class RoleHolder(StrEnum):
    """What decides a role's terms and seats, which ``role_held`` records. A role
    with no ``role_held`` is held by the file, as every role was before round 10."""

    FILE = "file"
    """The project file, which a start and a reload apply to the role."""
    OPERATOR = "operator"
    """An operator's ``dromon role`` command, which the file no longer undoes."""


class RoleNoteKind(StrEnum):
    HANDOFF = "handoff"
    RECOVERY = "recovery"
    NOTE = "note"


class BudgetSource(StrEnum):
    OPERATOR = "operator"
    ENGINE = "engine"


def check_instant(name: str, value: object) -> None:
    """Raise ``InvalidField`` unless ``value`` is a time in the ``DUE_FORMAT`` form.
    The model never reads the log's own ``recorded_at``, so an event whose time
    matters carries it."""
    check_text(name, value, len("YYYY-MM-DDTHH:MM:SSZ"))
    if not isinstance(value, str):  # Unreachable, since check_text refuses it.
        raise InvalidField(f"{value!r} is not text")
    try:
        parse_due(value)
    except InvalidField as error:
        raise InvalidField(
            f"{name} is not a valid time of the form YYYY-MM-DDTHH:MM:SSZ: {value!r}"
        ) from error


def check_workdir(value: object) -> None:
    """Raise ``InvalidField`` unless ``value`` is a relative path inside the project,
    written with ``/`` and without ``.`` or ``..`` steps, or ``.`` for the project
    root itself. The daemon resolves it and refuses a protected directory."""
    check_text("workdir", value, MAX_WORKDIR_BYTES)
    if not isinstance(value, str):  # Unreachable, since check_text refuses it.
        raise InvalidField(f"{value!r} is not text")
    if value == ".":
        return
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        raise InvalidField(f"workdir is not a relative path: {value!r}")
    if any(step in ("", ".", "..") for step in value.split("/")):
        raise InvalidField(f"workdir has an empty, . or .. step: {value!r}")


def check_commit(name: str, value: object) -> None:
    """Raise ``InvalidField`` unless ``value`` is a commit, or empty where the daemon
    could not read one."""
    check_text(name, value, 64)
    if not isinstance(value, str):  # Unreachable, since check_text refuses it.
        raise InvalidField(f"{value!r} is not text")
    if value and COMMIT_PATTERN.fullmatch(value) is None:
        raise InvalidField(f"{name} is not a commit: {value!r}")


@dataclass(frozen=True, slots=True)
class RoleDeclared:
    """A role and its worktree, relative to the project root, with how long a
    planned handoff may take, its seats in the declared order a failover follows,
    its failover policy, and the stated budget below which its owner is asked to
    prepare a handoff. Declaring a role again changes these, and is refused when
    nothing changes or when the worktree would change while a seat owns it. The
    order may name a line before it is bound, since the seats are bound after the
    role, and a failover skips any name that is not a seat of the role."""

    role_id: str
    workdir: str
    handoff_seconds: int = DEFAULT_HANDOFF_SECONDS
    seats: tuple[str, ...] = ()
    failover: FailoverPolicy = FailoverPolicy.AUTOMATIC
    handoff_below_percent: int = DEFAULT_HANDOFF_BELOW_PERCENT

    def __post_init__(self) -> None:
        check_id("role_id", self.role_id)
        check_workdir(self.workdir)
        if type(self.seats) is not tuple or len(self.seats) > MAX_ROLE_SEATS:
            raise InvalidField(f"seats must be a list of at most {MAX_ROLE_SEATS}")
        for seat in self.seats:
            check_id("seats", seat)
        if len(set(self.seats)) != len(self.seats):
            raise InvalidField("seats names a line twice")
        check_member("failover", self.failover, FailoverPolicy)
        percent = self.handoff_below_percent
        if type(percent) is not int or not 0 <= percent <= 100:
            raise InvalidField(
                "handoff_below_percent must be a whole number from 0 to 100"
            )
        seconds = self.handoff_seconds
        if type(seconds) is not int or not (
            MIN_HANDOFF_SECONDS <= seconds <= MAX_HANDOFF_SECONDS
        ):
            raise InvalidField(
                "handoff_seconds must be a whole number from"
                f" {MIN_HANDOFF_SECONDS} to {MAX_HANDOFF_SECONDS}"
            )


@dataclass(frozen=True, slots=True)
class SeatBound:
    """A line becomes a seat of a role, or a seat's ``may_own`` changes. A seat whose
    ``may_own`` is false may read and advise but never own. A line is a seat of at
    most one role."""

    line_id: str
    role_id: str
    may_own: bool

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_id("role_id", self.role_id)
        if type(self.may_own) is not bool:
            raise InvalidField("may_own must be true or false")


@dataclass(frozen=True, slots=True)
class SeatUnbound:
    """A line stops being a seat of its role, by an operator's command. The line stays
    a line, with its configuration, mailbox, leases and state, and only the role's
    tools and its place in the role leave it. An owner cannot be unbound, so its
    role is vacated or handed over first. Round 10."""

    line_id: str
    role_id: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        check_id("role_id", self.role_id)


@dataclass(frozen=True, slots=True)
class RoleRetired:
    """An operator retires a role that is vacant and has no seat left. Its journal
    stays in the log, readable entry by entry, and its identifier is never declared
    again, as every identifier of the log is never reused. Round 10."""

    role_id: str
    by: OperatorAddress
    retired_at: str

    def __post_init__(self) -> None:
        check_id("role_id", self.role_id)
        check_operator_address("by", self.by)
        check_instant("retired_at", self.retired_at)


@dataclass(frozen=True, slots=True)
class RoleHeld:
    """Whether the project file or an operator's commands decide a role's terms and
    seats from now. A command that changes a role records ``operator``, and a start or
    a reload records ``file`` once the file's table agrees with the role again, as
    ``docs/architecture/ROLES.md`` gives. ``by`` is the operator who made the change,
    or the project's operator for the file. Round 10."""

    role_id: str
    holder: RoleHolder
    by: OperatorAddress
    held_at: str

    def __post_init__(self) -> None:
        check_id("role_id", self.role_id)
        check_member("holder", self.holder, RoleHolder)
        check_operator_address("by", self.by)
        check_instant("held_at", self.held_at)


@dataclass(frozen=True, slots=True)
class OwnershipChanged:
    """A role's owner changes from ``previous`` to ``owner``, either of which may be
    ``None`` for a vacant role. It applies only while ``previous`` is the current
    owner, so of two changes that race only the first applies. ``by`` is the seat or
    operator that made it, and ``entry_id`` the journal entry it rests on, which a
    handoff requires. The model checks the change is consistent. The daemon checks
    that ``by`` had the authority, such as harness-observed exhaustion for a seat's
    own recovery. ``back_to``, only on a handoff, is the seat the role returns to
    when ``owner`` next relinquishes without naming a successor, for a short
    alternation between two seats."""

    role_id: str
    owner: str | None
    previous: str | None
    cause: OwnershipCause
    by: LocalAddress
    entry_id: str | None
    changed_at: str
    back_to: str | None = None

    def __post_init__(self) -> None:
        check_id("role_id", self.role_id)
        check_optional_id("owner", self.owner)
        check_optional_id("previous", self.previous)
        check_member("cause", self.cause, OwnershipCause)
        check_local_address("by", self.by)
        check_optional_id("entry_id", self.entry_id)
        check_instant("changed_at", self.changed_at)
        check_optional_id("back_to", self.back_to)
        if self.owner == self.previous:
            raise InvalidField("owner and previous are the same")
        if self.back_to is not None and (
            self.cause is not OwnershipCause.HANDOFF or self.back_to == self.owner
        ):
            raise InvalidField("back_to is a handoff's, and names another seat")
        cause = self.cause
        if self.owner is None and cause not in (
            OwnershipCause.RELINQUISHED,
            OwnershipCause.OPERATOR,
        ):
            raise InvalidField(f"a {cause} change names an owner")
        if cause is OwnershipCause.RELINQUISHED and self.owner is not None:
            raise InvalidField("a relinquished role has no owner")
        if cause is OwnershipCause.DECLARED and self.previous is not None:
            raise InvalidField("a declared owner replaces no owner")
        if cause is OwnershipCause.HANDOFF and (
            self.previous is None or self.entry_id is None
        ):
            raise InvalidField("a handoff names the previous owner and its entry")
        operator_only = (OwnershipCause.DECLARED, OwnershipCause.OPERATOR)
        if cause in operator_only and not isinstance(self.by, OperatorAddress):
            raise InvalidField(f"a {cause} change is made by an operator")
        from_owner = (OwnershipCause.HANDOFF, OwnershipCause.RELINQUISHED)
        if cause in from_owner and (
            self.previous is None or self.by != LineAddress(self.previous)
        ):
            raise InvalidField(f"a {cause} change is made by the previous owner")
        if (
            cause is OwnershipCause.RECOVERY
            and isinstance(self.by, LineAddress)
            and self.by.line_id != self.owner
        ):
            raise InvalidField("a seat recovers a role only for itself")
        # The log has no address for the harness itself, so a failover names the
        # seat it made owner, and its cause and the entry it rests on say the
        # harness made it.
        if cause is OwnershipCause.FAILOVER and (
            self.previous is None
            or self.entry_id is None
            or self.by != LineAddress(str(self.owner))
        ):
            raise InvalidField(
                "a failover names the previous owner and its entry, and is recorded"
                " by the seat made owner"
            )
        if cause is OwnershipCause.RECOVERY and isinstance(self.by, AgentAddress):
            raise InvalidField("an external agent does not recover a role")


@dataclass(frozen=True, slots=True)
class RoleNoted:
    """An entry in a role's journal by one of its seats. A handoff or recovery entry
    is written by the current owner, a note by any seat. The daemon stamps both
    commits itself, read from ``HEAD``, so the stamp is observed and not claimed,
    and leaves one empty when it cannot read it."""

    role_id: str
    entry_id: str
    line_id: str
    kind: RoleNoteKind
    body: str
    workdir_commit: str
    daemon_commit: str
    noted_at: str

    def __post_init__(self) -> None:
        check_id("role_id", self.role_id)
        check_id("entry_id", self.entry_id)
        check_id("line_id", self.line_id)
        check_member("kind", self.kind, RoleNoteKind)
        check_text("body", self.body, MAX_ROLE_NOTE_BYTES)
        if not self.body:
            raise InvalidField("body must not be empty")
        check_commit("workdir_commit", self.workdir_commit)
        check_commit("daemon_commit", self.daemon_commit)
        check_instant("noted_at", self.noted_at)


MAX_BRIEF_FIELD_BYTES: Final = 4096
"""The largest text field of a standing brief."""
MAX_BRIEF_ARTIFACTS: Final = 32
"""The most artifact paths a standing brief names."""
MAX_BRIEF_PATH_BYTES: Final = 1024
"""The longest artifact path a standing brief names."""
MAX_BRIEF_BYTES: Final = 16 * 1024
"""The largest standing brief, its text fields and paths together, so every place
that shows a brief is bounded by one size whatever the fields hold."""


@dataclass(frozen=True, slots=True)
class BriefSet:
    """A line's standing brief, its current task, open decisions, artifact paths and
    next action, replacing its previous brief. **Task of 2026-10-29.** It carries a
    line's task across a new engine session and a change of a role's owner, which
    mail alone did not. The text is the line's own and untrusted. The daemon stamps
    both commits and the time itself, leaving a commit empty when it cannot read it.
    An artifact path is a single line of text, since every view shows it on one."""

    line_id: str
    task: str
    decisions: str
    artifacts: tuple[str, ...]
    next_action: str
    workdir_commit: str
    daemon_commit: str
    set_at: str

    def __post_init__(self) -> None:
        check_id("line_id", self.line_id)
        for name, value in (
            ("task", self.task),
            ("decisions", self.decisions),
            ("next_action", self.next_action),
        ):
            check_text(name, value, MAX_BRIEF_FIELD_BYTES)
        if not self.task:
            raise InvalidField("task must not be empty")
        if len(self.artifacts) > MAX_BRIEF_ARTIFACTS:
            raise InvalidField(f"at most {MAX_BRIEF_ARTIFACTS} artifacts are named")
        for path in self.artifacts:
            check_text("artifact", path, MAX_BRIEF_PATH_BYTES)
            if not path or "\n" in path or "\r" in path:
                raise InvalidField("an artifact is a non-empty path on one line")
        size = sum(
            len(text.encode("utf-8"))
            for text in (self.task, self.decisions, self.next_action, *self.artifacts)
        )
        if size > MAX_BRIEF_BYTES:
            raise InvalidField(
                f"a brief is at most {MAX_BRIEF_BYTES} bytes, not {size}"
            )
        check_commit("workdir_commit", self.workdir_commit)
        check_commit("daemon_commit", self.daemon_commit)
        check_instant("set_at", self.set_at)


@dataclass(frozen=True, slots=True)
class BudgetStated:
    """What remains of a provider's plan, as an operator states it or an engine
    reports it at a plan limit, replacing the provider's earlier statement. It is a
    snapshot with its time, never adjusted by later turns, since a turn's cost does
    not convert reliably to a plan's percentage."""

    provider: str
    remaining_percent: int | None
    resets_at: str | None
    source: BudgetSource
    note: str
    stated_at: str

    def __post_init__(self) -> None:
        check_id("provider", self.provider)
        percent = self.remaining_percent
        if percent is not None and (
            type(percent) is not int or not 0 <= percent <= 100
        ):
            raise InvalidField("remaining_percent must be a whole number from 0 to 100")
        if self.resets_at is not None:
            check_instant("resets_at", self.resets_at)
        if percent is None and self.resets_at is None:
            raise InvalidField(
                "a budget states a remaining percentage, a reset or both"
            )
        check_member("source", self.source, BudgetSource)
        check_text("note", self.note, MAX_BUDGET_NOTE_BYTES)
        check_instant("stated_at", self.stated_at)


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
    | LineHibernated
    | LineAwakened
    | OperatorRegistered
    | AgentRegistered
    | AgentRevoked
    | MessageSent
    | MessageReceived
    | MessageDelivered
    | MessageReturned
    | MessageUnconfirmed
    | MessageRead
    | MessageAcknowledged
    | MessagePrioritized
    | MessageCorrelated
    | TurnCorrelated
    | LineTurnEnded
    | DisclosureChecked
    | WakeRequested
    | WakeIssued
    | WakeScheduled
    | WakeWithdrawn
    | TaskNoted
    | TaskRecorded
    | RestartNoted
    | LineCostMarked
    | ProcessesLeft
    | StopUnverified
    | WalkLimited
    | QuestionWithdrawn
    | QuestionSuperseded
    | ContextHandedOff
    | RoleDeclared
    | SeatBound
    | SeatUnbound
    | RoleRetired
    | RoleHeld
    | OwnershipChanged
    | RoleNoted
    | BriefSet
    | BudgetStated
    | PeerDeclared
    | PeerRemoved
    | PeerKeyed
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
    rebound: bool = False
    """Whether ``line_rebound``, rather than ``line_created``, set the current
    binding."""
    cost_mark: tuple[str, int] | None = None
    """The session and running cost total, in millionths of a dollar, of the line's
    latest ``line_cost_marked``."""
    left: ProcessesLeft | None = None
    """The line's latest ``processes_left``."""
    unverified: StopUnverified | None = None
    """The line's latest ``stop_unverified``."""
    handoff: str | None = None
    """Why the line's next turn starts a new engine session and is owed a context
    handoff, ``rebound`` or ``cleared``, until ``context_handed_off`` ends it."""
    hibernated: bool = False
    """Whether an operator hibernated the line, from ``line_hibernated`` until
    ``line_awakened``."""


@dataclass(frozen=True, slots=True)
class Agent:
    access: AgentAccess
    token_sha256: str
    """Empty for an agent admitted without a token."""
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class Message:
    sender: Sender
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
    priority: bool = False
    """Whether ``message_prioritized`` marked the note as priority mail."""
    restart_note: bool = False
    """Whether ``restart_noted`` recorded the note, so its header can say so in the
    restarted daemon, which holds nothing else of the restart."""
    origin_id: str | None = None
    """The peer's own identifier for a message ``message_received`` recorded, or
    ``None`` for local mail."""
    returned: ReturnCode | None = None
    """Why ``message_returned`` took mail to a peer from its outbox, if it did."""
    unconfirmed: bool = False
    """Whether ``message_unconfirmed`` took mail to a peer from its outbox."""
    priority_asked: bool = False
    """Whether the peer that delivered the note asked for priority, as
    ``message_received`` records."""
    correlation: str | None = None
    """The key ``message_correlated`` gave the note, if any."""
    reported_ends_at_delivery: int | None = None
    """The recipient line's count of reported turn ends, ``State.reported_turn_ends``,
    when ``message_delivered`` was applied, or ``None`` until then and for mail to any
    other party. **Round 8 task of 2026-10-30.** A later count means that a turn of
    the line whose engine reported it ended after the message reached the line, which
    retires an own note from ``own_notes``, as ``dromon.mailbox.counts_own_note``
    says."""


@dataclass(frozen=True, slots=True)
class Correlation:
    """The totals of one correlation key, from every ``turn_correlated`` that
    listed it. A turn's whole cost counts under each key it carried, so the totals
    of several keys may count one turn more than once, and ``shared_turns`` says how
    many of these turns carried more than one key."""

    turns: int = 0
    shared_turns: int = 0
    cost_micro_usd: int = 0
    """The sum of the known costs, held at ``MAX_COST_MICRO_USD``."""
    unknown_cost_turns: int = 0
    """The turns whose engine reported no cost, which may still have cost
    something."""


@dataclass(frozen=True, slots=True)
class Peer:
    """A declared peer, as the latest ``peer_declared`` and ``peer_keyed`` left it."""

    transport: str
    policy: str
    token_sha256: str = ""
    """The digest of the token the peer presents, or empty when none is issued."""
    removed: bool = False


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
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class Role:
    workdir: str
    handoff_seconds: int
    owner: str | None = None
    """The seat that may write the role's worktree, or ``None`` while it is vacant."""
    changed: OwnershipChanged | None = None
    """The role's latest ``ownership_changed``, which a former owner's next turn is
    told of."""
    seats: tuple[str, ...] = ()
    """The declared order of the role's seats, which a failover follows."""
    failover: FailoverPolicy = FailoverPolicy.AUTOMATIC
    handoff_below_percent: int = DEFAULT_HANDOFF_BELOW_PERCENT


@dataclass(frozen=True, slots=True)
class Seat:
    role_id: str
    may_own: bool


type RoleTerms = tuple[str, int, tuple[str, ...], FailoverPolicy, int]


def role_terms(role: Role | RoleDeclared) -> RoleTerms:
    """What a declaration of a role sets, so a declaration that changes none of it
    is known as such."""
    return (
        role.workdir,
        role.handoff_seconds,
        role.seats,
        role.failover,
        role.handoff_below_percent,
    )


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
    fired_dues: dict[str, str] = field(default_factory=dict[str, str])
    """The due time of each scheduled wake that has fired, keyed by its note's message
    identifier, so a turn can say when the wake was due."""
    hold_questions: set[str] = field(default_factory=set[str])
    """Every question Dromon asked on a line's behalf when it held the line, so an
    operator's reply to one is not counted as mail the line must answer."""

    withdrawn_wakes: set[str] = field(default_factory=set[str])
    """The identifier of every withdrawn scheduled wake, which no later message,
    task note or scheduled wake may take, so a reply reference to it cannot reach
    another message. **Round 7 review, F1.**"""
    walk_limit: str = ""
    """The latest ``walk_limited`` note, empty when none is recorded."""
    roles: dict[str, Role] = field(default_factory=dict[str, Role])
    seats: dict[str, Seat] = field(default_factory=dict[str, Seat])
    """Each seat's binding, keyed by line. A line is a seat of at most one role."""
    role_entries: dict[str, RoleNoted] = field(default_factory=dict[str, RoleNoted])
    """Every role's journal entries, keyed by entry, in the order they were noted."""
    budgets: dict[str, BudgetStated] = field(default_factory=dict[str, BudgetStated])
    """Each provider's latest stated budget, keyed by provider."""
    peers: dict[str, Peer] = field(default_factory=dict[str, Peer])
    """Every peer ever declared, keyed by peer, including removed ones."""
    received: dict[tuple[str, str], str] = field(
        default_factory=dict[tuple[str, str], str]
    )
    """The idempotency keys of ``message_received``, each peer and origin identifier
    with the local identifier it was given, in the order received. A key older than
    ``RECEIVED_KEY_HOURS`` is dropped when a later message is received, so the set is
    bounded by the mail of that window."""
    received_times: dict[tuple[str, str], datetime] = field(
        default_factory=dict[tuple[str, str], datetime]
    )
    """When each key in ``received`` was received, from its event."""
    outbox: dict[str, dict[str, None]] = field(
        default_factory=dict[str, dict[str, None]]
    )
    """Each peer's outbox, the mail to its parties neither delivered, returned nor
    unconfirmed, in the order sent. A peer with none has no entry. The inner maps are
    ordered sets."""
    waiting_from: dict[str, int] = field(default_factory=dict[str, int])
    """How many messages received from each peer are not yet delivered. A peer with
    none has no entry."""
    waiting_for: dict[LocalAddress, int] = field(
        default_factory=dict[LocalAddress, int]
    )
    """How many messages received from all peers each local party has not yet been
    delivered. A party with none has no entry."""
    correlations: dict[str, Correlation] = field(default_factory=dict[str, Correlation])
    """The totals of each correlation key, the key used least recently first, at most
    ``MAX_CORRELATIONS``."""
    disclosure_checks: dict[str, tuple[str, ...]] = field(
        default_factory=dict[str, tuple[str, ...]]
    )
    """When each line last checked a text against the disclosure list, oldest first,
    at most ``MAX_DISCLOSURE_CHECKS`` times per line, for the rate limit."""
    task_records: dict[str, tuple[TaskRecorded, ...]] = field(
        default_factory=dict[str, tuple[TaskRecorded, ...]]
    )
    """Each line's latest background task ends, oldest first, at most
    ``MAX_TASK_RECORDS`` per line, which its role's seats read."""
    last_turn_commits: dict[str, str | None] = field(
        default_factory=dict[str, str | None]
    )
    """The worktree commit recorded as each line's latest turn ended."""
    turn_start_counts: dict[str, int] = field(default_factory=dict[str, int])
    """Each line's number of recorded turn starts, reconstructed from
    ``line_state_changed`` events to ``running``."""
    reported_turn_ends: dict[str, int] = field(default_factory=dict[str, int])
    """Each line's number of reported turn ends, reconstructed from the
    ``line_turn_ended`` events that are not ``unreported``. The daemon records the
    event however the turn ended, and a failed or held turn whose engine still
    returned a report counts. **Round 8 task of 2026-10-30.** Compared with a
    message's ``reported_ends_at_delivery``, it says whether such a turn ended after
    the message was delivered, from the order of events alone, so replay agrees.
    **Round 8 review, ON1.** A turn with no report is left out, because its engine may
    never have received the notes delivered into it."""
    role_change_turn_starts: dict[str, dict[str, int]] = field(
        default_factory=dict[str, dict[str, int]]
    )
    """Each seat's turn-start count when its role last changed owner. A turn counts
    toward its notice only if its end follows a greater count."""
    role_change_ended_turns: dict[str, set[str]] = field(
        default_factory=dict[str, set[str]]
    )
    """Seats that ended a turn started after each role's latest ownership change.
    The event order rebuilds this across a restart, so a role notice is repeated only
    if that seat has not ended a post-change turn."""

    briefs: dict[str, BriefSet] = field(default_factory=dict[str, BriefSet])
    """Each line's latest standing brief, keyed by line. A line with none has no
    entry."""
    mail_since_brief: dict[str, str] = field(default_factory=dict[str, str])
    """The newest message from an operator to each line sent after the line's latest
    ``brief_set``, keyed by line, or no entry when none was. **Task of 2026-10-30.**
    A turn that leads with the brief says so, since the task may have changed in
    that mail. Built from the order of events alone, so replay agrees."""

    requests: dict[tuple[str, str], dict[str, RequestedAct]] = field(
        default_factory=dict[tuple[str, str], dict[str, RequestedAct]]
    )
    """The keyed calls that acted, by party and tool, then by key, each with the
    event that recorded the act, the oldest first, at most
    ``MAX_REQUEST_KEYS_PER_PARTY`` for each party and tool. Replay rebuilds it from
    the events alone, so the bound holds after a restart as before it."""

    retired_roles: dict[str, RoleRetired] = field(
        default_factory=dict[str, RoleRetired]
    )
    """Every retired role, keyed by role, whose identifier is never declared again.
    Bounded by the roles ever declared. Round 10."""
    role_holders: dict[str, RoleHeld] = field(default_factory=dict[str, RoleHeld])
    """Each role's latest ``role_held``. A role with no entry is held by the file.
    Round 10."""

    def holder_of(self, role_id: str) -> RoleHolder:
        """What decides ``role_id``'s terms and seats now."""
        held = self.role_holders.get(role_id)
        return RoleHolder.FILE if held is None else held.holder

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
            dict(self.fired_dues),
            set(self.hold_questions),
            set(self.withdrawn_wakes),
            self.walk_limit,
            dict(self.roles),
            dict(self.seats),
            dict(self.role_entries),
            dict(self.budgets),
            dict(self.peers),
            dict(self.received),
            dict(self.received_times),
            {peer_id: dict(waiting) for peer_id, waiting in self.outbox.items()},
            dict(self.waiting_from),
            dict(self.waiting_for),
            dict(self.correlations),
            dict(self.disclosure_checks),
            dict(self.task_records),
            dict(self.last_turn_commits),
            dict(self.turn_start_counts),
            dict(self.reported_turn_ends),
            {
                role_id: dict(line_counts)
                for role_id, line_counts in self.role_change_turn_starts.items()
            },
            {
                role_id: set(line_ids)
                for role_id, line_ids in self.role_change_ended_turns.items()
            },
            dict(self.briefs),
            dict(self.mail_since_brief),
            {slot: dict(acts) for slot, acts in self.requests.items()},
            dict(self.retired_roles),
            dict(self.role_holders),
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
            case LineHibernated():
                if self._line(event.line_id).hibernated:
                    raise EventRejected(f"line {event.line_id} is hibernated")
            case LineAwakened():
                if not self._line(event.line_id).hibernated:
                    raise EventRejected(f"line {event.line_id} is not hibernated")
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
                self._check_request(event)
            case MessageReceived():
                self._check_message_received(event)
            case MessageDelivered():
                message = self._message(event.message_id)
                if message.delivered:
                    raise EventRejected(f"message {event.message_id} was delivered")
                if message.returned is not None or message.unconfirmed:
                    raise EventRejected(
                        f"message {event.message_id} left its peer's outbox"
                    )
            case MessageReturned():
                self._check_outbox_end(event.message_id, event.note_id)
            case MessageUnconfirmed():
                self._check_outbox_end(event.message_id, event.note_id)
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
            case MessagePrioritized():
                self._check_prioritized(event)
            case MessageCorrelated():
                self._check_correlated(event)
            case (
                TurnCorrelated()
                | LineTurnEnded()
                | DisclosureChecked()
                | TaskRecorded()
            ):
                self._line(event.line_id)
            case WakeRequested():
                self._check_wake_requested(event)
            case WakeIssued():
                self._check_wake_issued(event)
            case WakeScheduled():
                self._check_wake_scheduled(event)
                self._check_request(event)
            case WakeWithdrawn():
                self._line(event.line_id)
                pending = self.scheduled.get(event.line_id)
                if pending is None or pending.message_id != event.message_id:
                    raise EventRejected(
                        f"line {event.line_id} has no pending scheduled wake"
                        f" {event.message_id}"
                    )
            case TaskNoted():
                self._check_task_noted(event)
            case RestartNoted():
                self._check_noted(event.line_id, event.message_id)
            case LineCostMarked():
                self._line(event.line_id)
            case ProcessesLeft() | StopUnverified():
                self._line(event.line_id)
            case WalkLimited():
                pass
            case QuestionWithdrawn():
                self._open_question(event.message_id)
            case QuestionSuperseded():
                self._check_superseded(event)
            case ContextHandedOff():
                line = self._line(event.line_id)
                if line.handoff != event.cause:
                    raise EventRejected(
                        f"line {event.line_id} is owed no handoff for {event.cause}"
                    )
            case RoleDeclared():
                self._check_role_declared(event)
            case SeatBound():
                self._check_seat_bound(event)
            case SeatUnbound():
                self._check_seat_unbound(event)
            case RoleRetired():
                self._check_role_retired(event)
            case RoleHeld():
                self._role(event.role_id)
                self._require_party(event.by)
                if self.holder_of(event.role_id) is event.holder:
                    raise EventRejected(
                        f"role {event.role_id} is held by the {event.holder} already"
                    )
            case OwnershipChanged():
                self._check_ownership_changed(event)
            case RoleNoted():
                self._check_role_noted(event)
            case BriefSet():
                self._line(event.line_id)
            case BudgetStated():
                pass
            case PeerDeclared():
                peer = self.peers.get(event.peer_id)
                if (
                    peer is not None
                    and not peer.removed
                    and (peer.transport, peer.policy) == (event.transport, event.policy)
                ):
                    raise EventRejected(f"peer {event.peer_id} is declared as that")
            case PeerRemoved():
                self._peer(event.peer_id)
            case PeerKeyed():
                peer = self._peer(event.peer_id)
                if not event.token_sha256 and not peer.token_sha256:
                    raise EventRejected(f"peer {event.peer_id} has no token to revoke")
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
                if event.state is LineState.RUNNING:
                    self.turn_start_counts[event.line_id] = (
                        self.turn_start_counts.get(event.line_id, 0) + 1
                    )
            case LineConfigured():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, config=event.config)
            case LineHeld():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, held_by=event.question_id)
                self.hold_questions.add(event.question_id)
            case LineReleased():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, held_by=None)
            case LineHibernated():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, hibernated=True)
            case LineAwakened():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, hibernated=False)
            case LineSession():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(
                    line, engine_session=event.engine_session
                )
            case LineSessionCleared():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(
                    line, engine_session=None, handoff="cleared"
                )
            case LineRebound():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(
                    line,
                    engine=event.engine,
                    strategy=event.strategy,
                    engine_session=None,
                    rebound=True,
                    handoff="rebound",
                )
            case ContextHandedOff():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, handoff=None)
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
                self._remember_request(event)
                if isinstance(event.sender, OperatorAddress) and isinstance(
                    event.recipient, LineAddress
                ):
                    self.mail_since_brief[event.recipient.line_id] = event.message_id
            case MessageReceived():
                self._apply_message_received(event)
            case MessageDelivered():
                message = self.messages[event.message_id]
                ends = (
                    self.reported_turn_ends.get(message.recipient.line_id, 0)
                    if isinstance(message.recipient, LineAddress)
                    else None
                )
                self.messages[event.message_id] = replace(
                    message, delivered=True, reported_ends_at_delivery=ends
                )
                if isinstance(message.sender, RemoteAddress) and not isinstance(
                    message.recipient, RemoteAddress
                ):
                    _decrement(self.waiting_from, message.sender.peer_id)
                    _decrement(self.waiting_for, message.recipient)
                self._leave_outbox(event.message_id, message)
            case MessageReturned():
                message = self.messages[event.message_id]
                self.messages[event.message_id] = replace(message, returned=event.code)
                self._leave_outbox(event.message_id, message)
                self._note_end(
                    message,
                    event.note_id,
                    returned_note(event.message_id, message.recipient, event.code),
                )
            case MessageUnconfirmed():
                message = self.messages[event.message_id]
                self.messages[event.message_id] = replace(message, unconfirmed=True)
                self._leave_outbox(event.message_id, message)
                self._note_end(
                    message,
                    event.note_id,
                    unconfirmed_note(event.message_id, message.recipient),
                )
            case MessageRead():
                message = self.messages[event.message_id]
                self.messages[event.message_id] = replace(message, read=True)
            case MessageAcknowledged():
                message = self.messages[event.message_id]
                self.messages[event.message_id] = replace(message, acknowledged=True)
            case MessagePrioritized():
                message = self.messages[event.message_id]
                self.messages[event.message_id] = replace(message, priority=True)
            case MessageCorrelated():
                message = self.messages[event.message_id]
                self.messages[event.message_id] = replace(
                    message, correlation=event.key
                )
            case TurnCorrelated():
                self._total_correlated(event)
            case LineTurnEnded():
                self.last_turn_commits[event.line_id] = event.workdir_commit
                if event.unreported is None:
                    self.reported_turn_ends[event.line_id] = (
                        self.reported_turn_ends.get(event.line_id, 0) + 1
                    )
                seat = self.seats.get(event.line_id)
                if (
                    seat is not None
                    and self.roles[seat.role_id].changed is not None
                    and self.turn_start_counts.get(event.line_id, 0)
                    > self.role_change_turn_starts.get(seat.role_id, {}).get(
                        event.line_id, 0
                    )
                ):
                    self.role_change_ended_turns.setdefault(seat.role_id, set()).add(
                        event.line_id
                    )
            case TaskRecorded():
                records = self.task_records.get(event.line_id, ())
                self.task_records[event.line_id] = (*records, event)[-MAX_TASK_RECORDS:]
            case DisclosureChecked():
                times = self.disclosure_checks.get(event.line_id, ())
                self.disclosure_checks[event.line_id] = (*times, event.checked_at)[
                    -MAX_DISCLOSURE_CHECKS:
                ]
            case WakeRequested():
                self.wakes[event.wake_id] = Wake(
                    event.line_id, event.message_id, issued=False
                )
            case WakeIssued():
                wake = self.wakes[event.wake_id]
                self.wakes[event.wake_id] = replace(wake, issued=True)
            case WakeScheduled():
                self.scheduled[event.line_id] = ScheduledWake(
                    event.message_id, event.due, event.note, event.task_id
                )
                self._remember_request(event)
            case WakeWithdrawn():
                del self.scheduled[event.line_id]
                self.withdrawn_wakes.add(event.message_id)
            case LineCostMarked():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(
                    line, cost_mark=(event.engine_session, event.total_micro_usd)
                )
            case ProcessesLeft():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, left=event)
            case StopUnverified():
                line = self.lines[event.line_id]
                self.lines[event.line_id] = replace(line, unverified=event)
            case WalkLimited():
                self.walk_limit = event.note
            case TaskNoted():
                pending = self.scheduled.get(event.line_id)
                if (
                    pending is not None
                    and pending.task_id is not None
                    and pending.task_id == event.task_id
                ):
                    del self.scheduled[event.line_id]
                    self.withdrawn_wakes.add(pending.message_id)
                address = LineAddress(event.line_id)
                self.messages[event.message_id] = Message(
                    address,
                    address,
                    MessageKind.NOTE,
                    event.body,
                    (),
                    None,
                )
            case RestartNoted():
                address = LineAddress(event.line_id)
                self.messages[event.message_id] = Message(
                    address,
                    address,
                    MessageKind.NOTE,
                    event.body,
                    (),
                    None,
                    restart_note=True,
                )
            case QuestionWithdrawn():
                self._close(event.message_id, QuestionStatus.WITHDRAWN)
            case QuestionSuperseded():
                self._close(
                    event.message_id,
                    QuestionStatus.SUPERSEDED,
                    superseded_by=event.superseded_by,
                )
            case RoleDeclared():
                role = self.roles.get(event.role_id)
                self.roles[event.role_id] = (
                    Role(
                        event.workdir,
                        event.handoff_seconds,
                        seats=event.seats,
                        failover=event.failover,
                        handoff_below_percent=event.handoff_below_percent,
                    )
                    if role is None
                    else replace(
                        role,
                        workdir=event.workdir,
                        handoff_seconds=event.handoff_seconds,
                        seats=event.seats,
                        failover=event.failover,
                        handoff_below_percent=event.handoff_below_percent,
                    )
                )
            case SeatBound():
                self.seats[event.line_id] = Seat(event.role_id, event.may_own)
                if (
                    self.roles[event.role_id].changed is not None
                    and event.role_id in self.role_change_turn_starts
                ):
                    self.role_change_turn_starts[event.role_id][event.line_id] = (
                        self.turn_start_counts.get(event.line_id, 0)
                    )
            case SeatUnbound():
                del self.seats[event.line_id]
                self.role_change_turn_starts.get(event.role_id, {}).pop(
                    event.line_id, None
                )
                self.role_change_ended_turns.get(event.role_id, set()).discard(
                    event.line_id
                )
            case RoleRetired():
                del self.roles[event.role_id]
                self.role_change_turn_starts.pop(event.role_id, None)
                self.role_change_ended_turns.pop(event.role_id, None)
                self.role_holders.pop(event.role_id, None)
                self.retired_roles[event.role_id] = event
            case RoleHeld():
                self.role_holders[event.role_id] = event
            case OwnershipChanged():
                role = self.roles[event.role_id]
                self.roles[event.role_id] = replace(
                    role, owner=event.owner, changed=event
                )
                self.role_change_turn_starts[event.role_id] = {
                    line_id: self.turn_start_counts.get(line_id, 0)
                    for line_id, seat in self.seats.items()
                    if seat.role_id == event.role_id
                }
                self.role_change_ended_turns[event.role_id] = set()
            case RoleNoted():
                self.role_entries[event.entry_id] = event
            case BriefSet():
                self.briefs[event.line_id] = event
                self.mail_since_brief.pop(event.line_id, None)
            case BudgetStated():
                self.budgets[event.provider] = event
            case PeerDeclared():
                peer = self.peers.get(event.peer_id)
                self.peers[event.peer_id] = (
                    Peer(event.transport, event.policy)
                    if peer is None or peer.removed
                    else replace(peer, transport=event.transport, policy=event.policy)
                )
            case PeerRemoved():
                peer = self.peers[event.peer_id]
                self.peers[event.peer_id] = replace(peer, token_sha256="", removed=True)
            case PeerKeyed():
                peer = self.peers[event.peer_id]
                self.peers[event.peer_id] = replace(
                    peer, token_sha256=event.token_sha256
                )
            case _:
                assert_never(event)

    # Lookups that turn a missing identifier into a rejection.

    def _peer(self, peer_id: str) -> Peer:
        """A declared peer that is not removed."""
        peer = self.peers.get(peer_id)
        if peer is None or peer.removed:
            raise EventRejected(f"no peer {peer_id}")
        return peer

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
            case RemoteAddress():
                # The remote party's existence is the peer's to check.
                self._peer(address.peer_id)
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
        """Whether ``event`` is the note of its sender's pending scheduled wake, which
        clears the wake when it applies."""
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
        self._check_withdrawn(event.message_id)
        if isinstance(event.sender, RemoteAddress):
            raise EventRejected("mail from a peer is recorded by message_received")
        if isinstance(event.sender, HarnessAddress):
            self._check_harness_note(event)
        if isinstance(event.recipient, RemoteAddress):
            # The daemon's outbound policy is checked before the append, since it is
            # configuration rather than log. The log holds what replay needs.
            if event.kind is not MessageKind.NOTE:
                raise EventRejected("only notes go to a peer")
            peer_id = event.recipient.peer_id
            if len(self.outbox.get(peer_id, {})) >= MAX_OUTBOX:
                raise EventRejected(
                    f"{MAX_OUTBOX} messages already wait for peer {peer_id}"
                )
        if not isinstance(event.sender, HarnessAddress):
            self._require_party(event.sender)
        self._require_party(event.recipient)
        firing = self._is_firing(event)
        # Decided by the operator on 2026-10-27. A line may send itself a note, for a
        # note to a later turn or to test a tool. No operator or agent may, since no
        # turn of theirs would receive it.
        if event.sender == event.recipient and not isinstance(
            event.sender, LineAddress
        ):
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
        if event.kind is MessageKind.ISSUE:
            if not isinstance(event.sender, LineAddress):
                raise EventRejected("only a line raises an issue")
            if not isinstance(event.recipient, OperatorAddress):
                raise EventRejected("an issue is raised with an operator")
            if event.reply_to is not None or event.choices:
                raise EventRejected("an issue opens a thread, and has no choices")
            self._check_issue_limits(event.sender.line_id)
        if event.reply_to is not None:
            self._check_reply(event, event.reply_to)

    def _check_issue_limits(self, line_id: str) -> None:
        """Bound a line's open issues and issue rate using replayed message order."""
        sender = LineAddress(line_id)
        replies = {
            (message.sender, message.reply_to)
            for message in self.messages.values()
            if message.reply_to is not None
        }
        open_issues = sum(
            message.kind is MessageKind.ISSUE
            and message.sender == sender
            and not message.acknowledged
            and (message.recipient, message_id) not in replies
            for message_id, message in self.messages.items()
        )
        if open_issues >= MAX_OPEN_ISSUES_PER_LINE:
            raise EventRejected(
                f"at most {MAX_OPEN_ISSUES_PER_LINE} issues may remain open;"
                " wait for an operator reply or acknowledgment"
            )
        recent = [
            message for message in self.messages.values() if message.sender == sender
        ][-(ISSUE_RATE_WINDOW_MESSAGES - 1) :]
        issue_count = sum(message.kind is MessageKind.ISSUE for message in recent)
        if issue_count >= MAX_ISSUES_PER_LINE_IN_WINDOW:
            raise EventRejected(
                f"at most {MAX_ISSUES_PER_LINE_IN_WINDOW} issues are allowed in"
                f" {ISSUE_RATE_WINDOW_MESSAGES} sent messages; wait for a reply"
            )

    def _check_harness_note(self, event: MessageSent) -> None:
        """The harness sends notes on its own account to a line or an operator of
        this daemon. It asks nothing and answers nothing, since no party writes to
        it, and it sends nothing to a peer, whose envelope would carry an address the
        peer could not answer."""
        if event.kind is not MessageKind.NOTE:
            raise EventRejected("the harness sends only notes")
        if event.reply_to is not None:
            raise EventRejected("the harness replies to nothing")
        if not isinstance(event.recipient, LineAddress | OperatorAddress):
            raise EventRejected("the harness writes only to a line or an operator")

    def _check_message_received(self, event: MessageReceived) -> None:
        if event.message_id in self.messages:
            raise EventRejected(f"message {event.message_id} exists")
        self._check_withdrawn(event.message_id)
        reserver = self._reserver(event.message_id)
        if reserver is not None:
            raise EventRejected(
                f"message {event.message_id} is reserved by line {reserver}'s"
                " scheduled wake"
            )
        peer_id = event.sender.peer_id
        self._peer(peer_id)
        earlier = self.received.get((peer_id, event.origin_id))
        if earlier is not None:
            raise EventRejected(
                f"peer {peer_id} delivered {event.origin_id} before, as {earlier}"
            )
        self._require_party(event.recipient)
        if event.reply_to is not None:
            parent = self._message(event.reply_to)
            if (parent.sender, parent.recipient) != (event.recipient, event.sender):
                raise EventRejected(
                    f"a reply to {event.reply_to} must go from {parent.recipient}"
                    f" to {parent.sender}"
                )

    def _check_outbox_end(self, message_id: str, note_id: str | None) -> None:
        """Mail may leave its peer's outbox once, by delivery, return or expiry. A
        line sender is owed a note under ``note_id``, a new identifier, and any other
        sender is not."""
        message = self._message(message_id)
        if not isinstance(message.recipient, RemoteAddress):
            raise EventRejected(f"message {message_id} is not to a peer")
        if message.delivered or message.returned is not None or message.unconfirmed:
            raise EventRejected(f"message {message_id} left its peer's outbox")
        if not isinstance(message.sender, LineAddress):
            if note_id is not None:
                raise EventRejected(f"{message.sender} is not a line, so has no note")
            return
        if note_id is None:
            raise EventRejected(f"line {message.sender} is owed a note")
        if note_id in self.messages:
            raise EventRejected(f"message {note_id} exists")
        self._check_withdrawn(note_id)
        reserver = self._reserver(note_id)
        if reserver is not None:
            raise EventRejected(
                f"message {note_id} is reserved by line {reserver}'s scheduled wake"
            )

    def _check_prioritized(self, event: MessagePrioritized) -> None:
        message = self._message(event.message_id)
        if message.priority:
            raise EventRejected(f"message {event.message_id} is priority mail")
        if message.kind is not MessageKind.NOTE:
            raise EventRejected(f"message {event.message_id} is not a note")
        recipient = message.recipient
        if isinstance(recipient, RemoteAddress):
            # Mail to a peer's line is marked while it waits in the outbox, so the
            # forwarder's envelope asks the peer for priority.
            if message.returned is not None or message.unconfirmed:
                raise EventRejected(f"message {event.message_id} left its outbox")
            recipient = recipient.party
        if not isinstance(recipient, LineAddress):
            raise EventRejected(f"message {event.message_id} is not to a line")
        if message.delivered:
            raise EventRejected(f"message {event.message_id} was delivered")

    def _check_correlated(self, event: MessageCorrelated) -> None:
        message = self._message(event.message_id)
        if message.correlation is not None:
            raise EventRejected(f"message {event.message_id} has a correlation key")
        if not isinstance(message.sender, AgentAddress):
            raise EventRejected(f"message {event.message_id} is not from an agent")
        if message.kind is not MessageKind.NOTE:
            raise EventRejected(f"message {event.message_id} is not a note")
        if not isinstance(message.recipient, LineAddress):
            raise EventRejected(f"message {event.message_id} is not to a line")
        if message.delivered:
            raise EventRejected(f"message {event.message_id} was delivered")

    def _total_correlated(self, event: TurnCorrelated) -> None:
        """Add a turn to each key it lists, moving each to the most recent end, and
        drop the keys used least recently beyond ``MAX_CORRELATIONS``."""
        shared = 1 if event.key_count > 1 else 0
        known = event.cost_micro_usd
        for key in event.keys:
            total = self.correlations.pop(key, Correlation())
            self.correlations[key] = Correlation(
                total.turns + 1,
                total.shared_turns + shared,
                min(total.cost_micro_usd + (known or 0), MAX_COST_MICRO_USD),
                total.unknown_cost_turns + (1 if known is None else 0),
            )
        # Bounded by MAX_TURN_KEYS, the most keys one event can add. The order is the
        # dictionary's insertion order, a porting hazard, since a Rust port needs an
        # ordered map to drop the same keys on replay.
        while len(self.correlations) > MAX_CORRELATIONS:
            del self.correlations[next(iter(self.correlations))]

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

    def _check_withdrawn(self, message_id: str) -> None:
        if message_id in self.withdrawn_wakes:
            raise EventRejected(
                f"message {message_id} was the identifier of a withdrawn wake"
            )

    def _check_wake_scheduled(self, event: WakeScheduled) -> None:
        self._line(event.line_id)
        if event.message_id in self.messages:
            raise EventRejected(f"message {event.message_id} exists")
        self._check_withdrawn(event.message_id)
        reserver = self._reserver(event.message_id)
        if reserver is not None and reserver != event.line_id:
            raise EventRejected(
                f"message {event.message_id} is reserved by line {reserver}'s"
                " scheduled wake"
            )

    def _check_request(self, event: RequestedAct) -> None:
        """A keyed act needs a party that may hold keys, and a key its party and tool
        have not kept, since a call that repeats a kept key repeats its result and
        records nothing. The harness and the wake that fires never give a key."""
        if event.request is None:
            return
        if isinstance(event, MessageSent) and isinstance(event.sender, HarnessAddress):
            raise EventRejected("the harness gives no request key")
        if isinstance(event, MessageSent) and self._is_firing(event):
            raise EventRejected("a firing wake gives no request key")
        slot = request_slot(event)
        if event.request.key in self.requests.get(slot, {}):
            party, tool = slot
            raise EventRejected(
                f"request key {event.request.key!r} of {party} for {tool} acted before"
            )

    def _remember_request(self, event: RequestedAct) -> None:
        """Keep ``event`` as the act of its request key, forgetting the oldest key of
        its party and tool beyond ``MAX_REQUEST_KEYS_PER_PARTY``."""
        if event.request is None:
            return
        acts = self.requests.setdefault(request_slot(event), {})
        acts[event.request.key] = event
        while len(acts) > MAX_REQUEST_KEYS_PER_PARTY:
            del acts[next(iter(acts))]

    def _check_task_noted(self, event: TaskNoted) -> None:
        self._check_noted(event.line_id, event.message_id)

    def _check_noted(self, line_id: str, message_id: str) -> None:
        """A note the daemon records from a line to itself needs the line, and a
        new identifier that no scheduled wake reserves or withdrew."""
        self._line(line_id)
        if message_id in self.messages:
            raise EventRejected(f"message {message_id} exists")
        self._check_withdrawn(message_id)
        reserver = self._reserver(message_id)
        if reserver is not None:
            raise EventRejected(
                f"message {message_id} is reserved by line {reserver}'s scheduled wake"
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

    def _role(self, role_id: str) -> Role:
        role = self.roles.get(role_id)
        if role is None:
            raise EventRejected(f"no role {role_id}")
        return role

    def _seat(self, line_id: str, role_id: str) -> Seat:
        """The seat ``line_id`` holds in ``role_id``, or a rejection."""
        seat = self.seats.get(line_id)
        if seat is None or seat.role_id != role_id:
            raise EventRejected(f"line {line_id} is not a seat of role {role_id}")
        return seat

    def _check_role_declared(self, event: RoleDeclared) -> None:
        retired = self.retired_roles.get(event.role_id)
        if retired is not None:
            raise EventRejected(
                f"role {event.role_id} was retired by {retired.by} at"
                f" {retired.retired_at}, and a role identifier is never declared again"
            )
        role = self.roles.get(event.role_id)
        if role is None:
            return
        if role_terms(role) == role_terms(event):
            raise EventRejected(f"role {event.role_id} is declared so already")
        if role.workdir != event.workdir and role.owner is not None:
            raise EventRejected(
                f"role {event.role_id} changes its worktree only while vacant,"
                f" and {role.owner} owns it"
            )

    def _check_seat_bound(self, event: SeatBound) -> None:
        self._line(event.line_id)
        role = self._role(event.role_id)
        seat = self.seats.get(event.line_id)
        if seat is not None and seat.role_id != event.role_id:
            raise EventRejected(
                f"line {event.line_id} is a seat of role {seat.role_id}"
            )
        if seat is not None and seat.may_own == event.may_own:
            raise EventRejected(
                f"line {event.line_id} is bound to role {event.role_id} so already"
            )
        if not event.may_own and role.owner == event.line_id:
            raise EventRejected(
                f"line {event.line_id} owns role {event.role_id}, so it must give"
                " the role up before it can be barred from owning it"
            )

    def _check_seat_unbound(self, event: SeatUnbound) -> None:
        role = self._role(event.role_id)
        self._seat(event.line_id, event.role_id)
        if role.owner == event.line_id:
            raise EventRejected(
                f"line {event.line_id} owns role {event.role_id}, so the role is"
                " vacated or handed over before the seat is unbound"
            )

    def _check_role_retired(self, event: RoleRetired) -> None:
        role = self._role(event.role_id)
        self._require_party(event.by)
        if role.owner is not None:
            raise EventRejected(
                f"role {event.role_id} is owned by {role.owner}, so it is vacated or"
                " handed over before it is retired"
            )
        bound = sorted(
            line for line, seat in self.seats.items() if seat.role_id == event.role_id
        )
        if bound:
            raise EventRejected(
                f"role {event.role_id} still has seats {', '.join(bound)}, which are"
                " unbound before it is retired"
            )

    def _check_ownership_changed(self, event: OwnershipChanged) -> None:
        role = self._role(event.role_id)
        if role.owner != event.previous:
            raise EventRejected(
                f"role {event.role_id} is owned by {role.owner or 'no seat'},"
                f" not {event.previous or 'no seat'}"
            )
        self._require_party(event.by)
        if isinstance(event.by, LineAddress):
            self._seat(event.by.line_id, event.role_id)
        if event.owner is not None:
            if not self._seat(event.owner, event.role_id).may_own:
                raise EventRejected(
                    f"line {event.owner} may not own role {event.role_id}"
                )
            self._live_line(event.owner)
        if (
            event.back_to is not None
            and not self._seat(event.back_to, event.role_id).may_own
        ):
            raise EventRejected(
                f"line {event.back_to} may not own role {event.role_id}"
            )
        if event.entry_id is not None:
            entry = self.role_entries.get(event.entry_id)
            if entry is None or entry.role_id != event.role_id:
                raise EventRejected(
                    f"role {event.role_id} has no entry {event.entry_id}"
                )
            if event.cause is OwnershipCause.HANDOFF and (
                entry.kind is not RoleNoteKind.HANDOFF
                or entry.line_id != event.previous
                or self.latest_entry(event.role_id) != event.entry_id
            ):
                raise EventRejected(
                    f"entry {event.entry_id} is not the latest entry of role"
                    f" {event.role_id}, a handoff by {event.previous}"
                )

    def latest_entry(self, role_id: str) -> str | None:
        """The identifier of ``role_id``'s latest journal entry, if it has one."""
        for entry_id in reversed(self.role_entries):
            if self.role_entries[entry_id].role_id == role_id:
                return entry_id
        return None

    def _check_role_noted(self, event: RoleNoted) -> None:
        if event.entry_id in self.role_entries:
            raise EventRejected(f"entry {event.entry_id} exists")
        role = self._role(event.role_id)
        self._seat(event.line_id, event.role_id)
        if event.kind is not RoleNoteKind.NOTE and role.owner != event.line_id:
            raise EventRejected(
                f"a {event.kind} entry of role {event.role_id} is written by its"
                f" owner, not {event.line_id}"
            )

    # Mutations.

    def _apply_message_sent(self, event: MessageSent) -> None:
        if self._is_firing(event) and isinstance(event.sender, LineAddress):
            fired = self.scheduled.pop(event.sender.line_id)
            self.fired_dues[event.message_id] = fired.due
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
        if isinstance(event.recipient, RemoteAddress):
            self.outbox.setdefault(event.recipient.peer_id, {})[event.message_id] = None

    def _leave_outbox(self, message_id: str, message: Message) -> None:
        if not isinstance(message.recipient, RemoteAddress):
            return
        peer_id = message.recipient.peer_id
        waiting = self.outbox.get(peer_id)
        if waiting is None:
            return
        waiting.pop(message_id, None)
        if not waiting:
            del self.outbox[peer_id]

    def _note_end(self, message: Message, note_id: str | None, body: str) -> None:
        """Record the note a line sender is owed when its mail leaves the outbox
        undelivered, from the line to itself."""
        if note_id is None or not isinstance(message.sender, LineAddress):
            return
        self.messages[note_id] = Message(
            message.sender, message.sender, MessageKind.NOTE, body, (), None
        )

    def _forget_old_keys(self, now: datetime) -> None:
        """Drop the received keys older than ``RECEIVED_KEY_HOURS`` before ``now``,
        oldest first. Keys are kept in the order received, so the loop stops at the
        first key young enough. A wall clock set back can leave an old key behind a
        younger one until that one ages too, which keeps a key longer, never
        shorter. Bounded by the number of keys."""
        cutoff = now - timedelta(hours=RECEIVED_KEY_HOURS)
        for _ in range(len(self.received_times)):
            key = next(iter(self.received_times))
            if self.received_times[key] >= cutoff:
                return
            del self.received_times[key]
            self.received.pop(key, None)

    def _apply_message_received(self, event: MessageReceived) -> None:
        peer_id = event.sender.peer_id
        received_at = parse_due(event.received_at)
        self._forget_old_keys(received_at)
        self.messages[event.message_id] = Message(
            event.sender,
            event.recipient,
            MessageKind.NOTE,
            event.body,
            (),
            event.reply_to,
            origin_id=event.origin_id,
            priority_asked=event.priority_asked,
        )
        self.received[(peer_id, event.origin_id)] = event.message_id
        self.received_times[(peer_id, event.origin_id)] = received_at
        self.waiting_from[peer_id] = self.waiting_from.get(peer_id, 0) + 1
        self.waiting_for[event.recipient] = self.waiting_for.get(event.recipient, 0) + 1

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


def _decrement[K](counts: dict[K, int], key: K) -> None:
    """Lower ``key``'s count by one, removing it at zero, so that a map of waiting
    counts holds only the parties and peers with mail waiting."""
    left = counts.get(key, 0) - 1
    if left > 0:
        counts[key] = left
    else:
        counts.pop(key, None)
