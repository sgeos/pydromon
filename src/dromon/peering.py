"""The receiving end of federation, ``hello`` and ``deliver`` on ``peers.sock``.

``docs/architecture/FEDERATION.md`` is the design. A peer daemon connects to this
daemon's ``peers.sock``, names itself by the token this daemon issued for it with
``hello``, and then delivers notes one at a time with ``deliver``. Each delivery is
answered ``accepted``, ``refused`` with a fixed code, or ``busy``. Only ``accepted``
records anything, one ``message_received`` event.

Trust. Everything a peer sends is untrusted data. The peer is named by its token,
never by what it calls itself, and the token proves the peer daemon and not any party
in it, so the receiver enforces ``reach`` itself on every delivery. A refusal never
says whether a party exists, and its codes are fixed, so a peer learns nothing it may
not reach. A remote sender holds no operator authority, and peer mail neither counts
toward a line's autonomous-turn allowance nor resets it. Each peer may instead start
only ``turns_per_hour`` turns for one local line in any hour, counted by
``PeerTurns``.

Memory. The rate limit and the turn allowances are kept in memory, so a restart, which
only the operator causes, resets them. Everything else is derived from the event log.

Porting note. Plain data, fixed codes and bounded loops. The window counters hold at
most their capacity per key, and their keys are bounded by the declared peers and the
lines.
"""

import hmac
import sys
from collections import deque
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from dromon.agents import token_digest
from dromon.federation import TRANSPORT_UNIX, FederationConfig
from dromon.model import (
    ID_PATTERN,
    MAX_TEXT_BYTES,
    AgentAddress,
    InvalidField,
    LineAddress,
    LocalAddress,
    Message,
    MessageKind,
    OperatorAddress,
    RemoteAddress,
    State,
    check_text,
    parse_address,
)

FEDERATION_VERSION: Final = 1
"""The federation protocol this daemon speaks. ``hello`` chooses it from the caller's
list."""
MAX_VERSIONS: Final = 16
"""The most versions a ``hello`` may list."""
MAX_HELLO_TOKEN_BYTES: Final = 256

RATE_WINDOW_SECONDS: Final = 600.0
RATE_PER_PEER: Final = 60
"""Deliveries one peer may make in ``RATE_WINDOW_SECONDS``, the design's default of
decision 7. Every ``deliver`` counts, whatever its answer, so a peer cannot make the
receiver check without limit by sending refused mail."""

TURN_WINDOW_SECONDS: Final = 3600.0
MAX_TURN_CAPACITY: Final = 60
"""The largest ``turns_per_hour`` a peer may have, and so the most turn starts kept for
one peer and line."""

DELIVER_KEYS: Final = frozenset(
    {"origin_id", "sender", "recipient", "kind", "body", "reply_to", "priority"}
)
HELLO_KEYS: Final = frozenset({"versions", "name", "token"})


class Code(StrEnum):
    """Why a peer's mail was refused or returned. Fixed, so a peer cannot put its own
    text into a sender's prompt by refusing."""

    UNKNOWN_PEER = "unknown_peer"
    REFUSED = "refused"
    TOO_LARGE = "too_large"
    UNKNOWN_REPLY = "unknown_reply"
    DISCLOSURE = "disclosure"
    EXPIRED = "expired"
    MALFORMED = "malformed"
    CONFLICT = "conflict"
    FULL = "full"


@dataclass(frozen=True, slots=True)
class Accepted:
    message_id: str
    duplicate: bool
    priority: bool = False
    """Whether the receiver honoured the note's priority, which it does not before
    step 6 of phase 1."""


@dataclass(frozen=True, slots=True)
class Refused:
    code: Code


@dataclass(frozen=True, slots=True)
class Busy:
    retry_seconds: int


type Outcome = Accepted | Refused | Busy


def outcome_result(outcome: Outcome) -> dict[str, object]:
    """The ``deliver`` answer for ``outcome``, as the wire protocol gives it."""
    match outcome:
        case Accepted():
            return {
                "outcome": "accepted",
                "message_id": outcome.message_id,
                "duplicate": outcome.duplicate,
                "priority": outcome.priority,
            }
        case Refused():
            return {"outcome": "refused", "code": outcome.code.value, "retry": False}
        case Busy():
            return {"outcome": "busy", "retry_seconds": outcome.retry_seconds}


@dataclass(frozen=True, slots=True)
class Envelope:
    """A ``deliver`` request's arguments, checked against the local grammar."""

    origin_id: str
    sender: LocalAddress
    recipient: LocalAddress
    kind: str
    body: str
    reply_to: str | None
    priority: bool


class EnvelopeError(ValueError):
    """A ``deliver`` request is not a well formed envelope, refused as ``malformed``."""


def parse_envelope(args: Mapping[str, object]) -> Envelope:
    """The envelope in ``args``, or ``EnvelopeError``. Both addresses must be local
    addresses as the grammar writes them, so a remote form, a second colon or a
    control character is refused before anything is recorded."""
    if frozenset(args) != DELIVER_KEYS:
        raise EnvelopeError("the envelope does not have exactly the envelope's keys")
    origin_id = args["origin_id"]
    if not isinstance(origin_id, str) or ID_PATTERN.fullmatch(origin_id) is None:
        raise EnvelopeError("origin_id is not an identifier")
    sender = _local(args["sender"], "sender")
    recipient = _local(args["recipient"], "recipient")
    kind = args["kind"]
    if kind not in (MessageKind.NOTE.value, MessageKind.QUESTION.value):
        raise EnvelopeError("kind is not a message kind")
    body = args["body"]
    try:
        check_text("body", body, MAX_TEXT_BYTES)
    except InvalidField as error:
        raise EnvelopeError(str(error)) from error
    reply_to = args["reply_to"]
    if reply_to is not None and (
        not isinstance(reply_to, str) or ID_PATTERN.fullmatch(reply_to) is None
    ):
        raise EnvelopeError("reply_to is not an identifier or null")
    priority = args["priority"]
    if type(priority) is not bool:
        raise EnvelopeError("priority is not a boolean")
    return Envelope(
        origin_id,
        sender,
        recipient,
        cast(str, kind),
        cast(str, body),
        reply_to,
        priority,
    )


def _local(value: object, name: str) -> LocalAddress:
    """``value`` as a local address. ``parse_address`` refuses a colon, since an
    identifier holds none, and a control character for the same reason."""
    if not isinstance(value, str):
        raise EnvelopeError(f"{name} is not text")
    try:
        return parse_address(value)
    except InvalidField as error:
        raise EnvelopeError(f"{name} is not a local address") from error


def identify(state: State, token: object) -> str | None:
    """The peer whose issued token is ``token``, or ``None``. Only a declared ``unix``
    peer that is not removed and holds a token can match. Every peer's digest is
    compared in constant time, so the time taken does not say which peer came close,
    and an undeclared name and a wrong token are refused alike."""
    if not isinstance(token, str) or not 0 < len(token) <= MAX_HELLO_TOKEN_BYTES:
        return None
    digest = token_digest(token)
    found: str | None = None
    for peer_id, peer in sorted(state.peers.items()):
        if peer.removed or not peer.token_sha256 or peer.transport != TRANSPORT_UNIX:
            continue
        if hmac.compare_digest(peer.token_sha256, digest):
            found = peer_id
    return found


def choose_version(versions: object) -> int | None:
    """The federation version to speak with a caller that speaks ``versions``, or
    ``None`` when the list is malformed or holds none this daemon speaks."""
    if not isinstance(versions, list):
        return None
    listed = cast(list[object], versions)
    if not 0 < len(listed) <= MAX_VERSIONS:
        return None
    if any(type(version) is not int for version in listed):
        return None
    return FEDERATION_VERSION if FEDERATION_VERSION in listed else None


class WindowCounter[K: Hashable]:
    """Counts events per key over a sliding window, at most ``capacity`` per key."""

    def __init__(
        self, clock: Callable[[], float], window_seconds: float, capacity: int
    ) -> None:
        self._clock = clock
        self._window = window_seconds
        self._capacity = capacity
        self._times: dict[K, deque[float]] = {}

    def _recent(self, key: K) -> deque[float]:
        times = self._times.setdefault(key, deque(maxlen=self._capacity))
        now = self._clock()
        # Bounded by the deque's capacity.
        while times and now - times[0] >= self._window:
            times.popleft()
        return times

    def count(self, key: K) -> int:
        return len(self._recent(key))

    def record(self, key: K) -> None:
        self._recent(key).append(self._clock())

    def wait(self, key: K) -> int:
        """Whole seconds until the oldest counted event leaves the window, at least
        one."""
        times = self._recent(key)
        if not times:
            return 1
        return max(1, int(self._window - (self._clock() - times[0]) + 0.999))


type Receive = Callable[[RemoteAddress, LocalAddress, str, str | None, str], str]
"""Records a received note and returns its local identifier, which the scheduler does
so that the note joins the post office."""


class Receiver:
    """Judges each delivery from an identified peer, and records the accepted ones."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._rate: WindowCounter[str] = WindowCounter(
            clock, RATE_WINDOW_SECONDS, RATE_PER_PEER
        )

    def deliver(
        self,
        state: State,
        federation: FederationConfig,
        peer_id: str,
        args: Mapping[str, object],
        receive: Receive,
    ) -> Outcome:
        """Answer one ``deliver`` from ``peer_id``. The checks run in a fixed order.
        The rate limit comes first, so every request counts. The grammar follows, so
        nothing malformed is looked up. A resend of a message already recorded is
        answered next, as a duplicate or a conflict, so that a policy changed since
        the first delivery cannot return mail that was accepted. Then the policy,
        ``reach`` before anything that depends on the recipient, so a party the
        peer may not reach is refused alike whether or not it exists."""
        if self._rate.count(peer_id) >= RATE_PER_PEER:
            return Busy(self._rate.wait(peer_id))
        self._rate.record(peer_id)
        try:
            envelope = parse_envelope(args)
        except EnvelopeError:
            return Refused(Code.MALFORMED)
        sender = RemoteAddress(peer_id, envelope.sender)
        earlier = state.received.get((peer_id, envelope.origin_id))
        if earlier is not None:
            return _resent(state, peer_id, earlier, sender, envelope)
        definition = federation.peers.get(peer_id)
        peer = state.peers.get(peer_id)
        if definition is None or peer is None or peer.removed or not definition.inbound:
            return Refused(Code.REFUSED)
        if envelope.recipient not in definition.reach or not _exists(
            state, envelope.recipient
        ):
            return Refused(Code.REFUSED)
        if envelope.kind != MessageKind.NOTE.value:
            return Refused(Code.REFUSED)
        if len(envelope.body.encode("utf-8")) > definition.max_body_bytes:
            return Refused(Code.TOO_LARGE)
        if envelope.reply_to is not None and not _replies(
            state, envelope.reply_to, sender, envelope.recipient
        ):
            return Refused(Code.UNKNOWN_REPLY)
        if (
            state.waiting_for.get(envelope.recipient, 0)
            >= federation.waiting_per_recipient
            or state.waiting_from.get(peer_id, 0) >= definition.waiting
        ):
            return Refused(Code.FULL)
        message_id = receive(
            sender,
            envelope.recipient,
            envelope.body,
            envelope.reply_to,
            envelope.origin_id,
        )
        return Accepted(message_id, duplicate=False)


def _resent(
    state: State,
    peer_id: str,
    message_id: str,
    sender: RemoteAddress,
    envelope: Envelope,
) -> Outcome:
    """The answer to a resend. The same message is a duplicate, answered with the
    identifier it was given. A different one under the same origin identifier is a
    fault or a forgery at the sending end, refused and reported on standard error."""
    first: Message = state.messages[message_id]
    if (
        first.sender == sender
        and first.recipient == envelope.recipient
        and first.body == envelope.body
        and first.reply_to == envelope.reply_to
        and envelope.kind == MessageKind.NOTE.value
    ):
        return Accepted(message_id, duplicate=True)
    print(
        f"dromond: peer {peer_id} sent a different message under origin identifier"
        f" {envelope.origin_id}, recorded as {message_id}. Refused as a conflict",
        file=sys.stderr,
    )
    return Refused(Code.CONFLICT)


def _exists(state: State, address: LocalAddress) -> bool:
    match address:
        case LineAddress():
            return address.line_id in state.lines
        case OperatorAddress():
            return address.operator_id in state.operators
        case AgentAddress():
            agent = state.agents.get(address.agent_id)
            return agent is not None and not agent.revoked


def _replies(
    state: State, reply_to: str, sender: RemoteAddress, recipient: LocalAddress
) -> bool:
    """Whether ``reply_to`` names a local message from ``recipient`` to ``sender``,
    the only message a reply from that sender may answer."""
    parent = state.messages.get(reply_to)
    return (
        parent is not None and parent.sender == recipient and parent.recipient == sender
    )


class TurnCause(StrEnum):
    """What started a turn, which decides how it counts toward the autonomous-turn
    allowance. Decision 10 of the design."""

    OPERATOR = "operator"
    """An operator's mail woke it, so the count starts again from 0."""
    AUTONOMOUS = "autonomous"
    """Local mail from a line, an agent or the line itself woke it, so it counts."""
    PEER = "peer"
    """Only peer mail woke it, so it neither counts nor resets the count."""


def turn_cause(messages: Iterable[Message]) -> TurnCause:
    """How a turn that delivers ``messages`` counts. Operator mail outweighs the rest,
    and local mail outweighs peer mail."""
    senders = [message.sender for message in messages]
    if any(isinstance(sender, OperatorAddress) for sender in senders):
        return TurnCause.OPERATOR
    if senders and all(isinstance(sender, RemoteAddress) for sender in senders):
        return TurnCause.PEER
    return TurnCause.AUTONOMOUS


class PeerTurns:
    """Each peer's allowance of turns for each local line, ``turns_per_hour`` in any
    hour. A peer without an allowance, such as one no longer declared, may start
    none."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._allowance: dict[str, int] = {}
        self._starts: WindowCounter[tuple[str, str]] = WindowCounter(
            clock, TURN_WINDOW_SECONDS, MAX_TURN_CAPACITY
        )

    def set_allowances(self, allowances: Mapping[str, int]) -> None:
        """Replace each peer's turns per hour. Each must be within the capacity."""
        for peer_id, turns in allowances.items():
            if not 0 <= turns <= MAX_TURN_CAPACITY:
                raise ValueError(
                    f"peer {peer_id} turns_per_hour must be in [0, {MAX_TURN_CAPACITY}]"
                )
        self._allowance = dict(allowances)

    def may_start(self, peer_id: str, line_id: str) -> bool:
        allowed = self._allowance.get(peer_id, 0)
        return self._starts.count((peer_id, line_id)) < allowed

    def starters(self, line_id: str, messages: Iterable[Message]) -> list[str]:
        """The peers whose mail among ``messages`` may start a turn of ``line_id``,
        in name order."""
        peers = {
            message.sender.peer_id
            for message in messages
            if isinstance(message.sender, RemoteAddress)
        }
        return [peer for peer in sorted(peers) if self.may_start(peer, line_id)]

    def may_wake(self, line_id: str, messages: Iterable[Message]) -> bool:
        """Whether ``messages`` may start a turn of ``line_id``. Local mail always
        may, and peer mail only within its peer's allowance."""
        listed = list(messages)
        if any(not isinstance(message.sender, RemoteAddress) for message in listed):
            return True
        return bool(self.starters(line_id, listed))

    def record(self, line_id: str, peers: Iterable[str]) -> None:
        """Count a turn of ``line_id`` that the mail of ``peers`` started."""
        for peer_id in peers:
            self._starts.record((peer_id, line_id))
