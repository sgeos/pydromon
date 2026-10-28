"""The sending end of federation, the outbox and its forwarder.

``docs/architecture/FEDERATION.md`` is the design. A local party sends a note to a
remote address. ``check_outbound`` applies this daemon's outbound policy before
anything is recorded, and the model then records the note with ``message_sent``, which
puts it in its peer's outbox. One ``PeerForwarder`` per declared peer delivers its
outbox in order, one message at a time, and records how each left it.

- ``accepted`` from the peer, whether new or a duplicate, records ``message_delivered``.
- ``refused`` records ``message_returned`` with the peer's code, which is one of the
  fixed codes, so a peer cannot put its own text into a sender's prompt.
- ``busy`` waits as the peer asks, and after ``MAX_BUSY`` answers ends the message as an
  expired one, decided by the operator on 2026-10-28.
- A connection that fails, or an answer that is missing or malformed, is retried with
  backoff, and the message waits at the head of the outbox, so order is kept.
- A message older than its peer's ``expire_hours`` leaves the outbox. It is returned
  with ``expired`` when no copy of it can have reached the peer, and recorded with
  ``message_unconfirmed`` otherwise, decision 12 of the design.

Before each attempt the forwarder checks the message again against the policy and the
disclosure list in force, since a restart may have changed either, and returns it
with ``refused``, ``too_large`` or ``disclosure`` when it no longer passes.

The transport seam. A ``Connector`` opens a ``Link`` to a peer, a channel that carries
one request and its answer at a time. ``connect_unix`` is the same-machine transport of
phase 1. Phase 2 adds a connector over Transport Layer Security, which
``dromon.federation_tls`` provides on its own branch, and the forwarder is unchanged.

Memory. Backoff, the busy count, the times first seen and the marks of copies that
may have reached the peer are kept in memory. A restart resets the backoff, and marks
every waiting message as possibly sent, which errs toward an unknown outcome. Send
times survive a restart, since they are read from the event log's ``recorded_at``.

Porting note. Each peer's forwarder is an explicit state machine, idle, sending or
backing off, whose ``step`` performs one action and says how long to wait, so it maps
onto a stream processor whose input and output the host performs.
"""

import asyncio
import contextlib
import secrets
import sys
import traceback
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, cast

from dromon.eventlog import Entry
from dromon.federation import TRANSPORT_UNIX, FederationConfig, PeerDefinition
from dromon.model import (
    ID_PATTERN,
    Address,
    AgentAddress,
    EventRejected,
    LineAddress,
    Message,
    MessageKind,
    MessageSent,
    RemoteAddress,
    ReturnCode,
    State,
    format_due,
)
from dromon.peering import FEDERATION_VERSION
from dromon.protocol import (
    MAX_LINE_BYTES,
    MAX_PEER_LINE_BYTES,
    Operation,
    ProtocolError,
    Request,
    Response,
    decode_response,
    encode_request,
)

CONNECT_TIMEOUT_SECONDS: Final = 10.0
ANSWER_TIMEOUT_SECONDS: Final = 30.0
"""How long the forwarder waits for a peer's answer before it treats the attempt as
unanswered."""
FIRST_BACKOFF_SECONDS: Final = 5.0
MAX_BACKOFF_SECONDS: Final = 900.0
"""The backoff runs from 5 seconds to 15 minutes, doubling, as the design gives."""
MAX_BUSY: Final = 20
"""The ``busy`` answers one message may receive before it ends as expired."""
OUTAGE_SECONDS: Final = 3600
"""How long the message at the head of a peer's outbox waits before the operator is
told of the outage, as the design gives."""
DEFAULT_NAME: Final = "dromon"
"""The name announced in ``hello`` when the project's directory name is not an
identifier or is covered by the disclosure list."""


class LinkError(Exception):
    """A link to a peer failed, or its answer was missing or malformed. Transient."""


class Link(Protocol):
    """A channel to one peer that carries one request and its answer at a time.

    ``exchange`` returns only the peer's own answer, read from the channel, and raises
    ``LinkError`` when none arrives. A write that succeeded is not an answer. A TLS
    client socket can be writable before the server has accepted the client's
    certificate, as the review of the TLS transport found on 2026-10-28, so a refused
    certificate shows only as a missing answer. The forwarder therefore records a
    message as delivered only from an ``accepted`` answer to its ``deliver``, which
    follows an accepted answer to ``hello``, on every transport."""

    async def exchange(self, request: Request) -> Response: ...

    async def close(self) -> None: ...


type Connector = Callable[[str, PeerDefinition], Awaitable[Link]]
"""Opens a link to the peer named by its identifier and definition, or raises
``LinkError``."""


class Outbox(Protocol):
    """What the forwarder needs of the scheduler, which owns the log."""

    @property
    def state(self) -> State: ...

    def discloses(self, *texts: str) -> bool: ...

    def forwarded(self, message_id: str) -> None: ...

    def return_mail(self, message_id: str, code: ReturnCode) -> None: ...

    def leave_unconfirmed(self, message_id: str) -> None: ...

    def outage(self, report: "PeerReport") -> None: ...


def check_outbound(
    state: State,
    federation: FederationConfig,
    sender: Address,
    recipient: RemoteAddress,
    body: str,
    *,
    priority: bool,
) -> None:
    """Raise ``EventRejected`` unless this daemon's policy lets ``sender`` send ``body``
    to ``recipient`` now. A refusal records nothing. The model checks the rest, that
    the peer is declared and that its outbox has room."""
    peer_id = recipient.peer_id
    definition = federation.peers.get(peer_id)
    peer = state.peers.get(peer_id)
    if definition is None or peer is None or peer.removed:
        raise EventRejected(f"no peer {peer_id}")
    if definition.transport != TRANSPORT_UNIX:
        raise EventRejected(
            f"peer {peer_id} is reached over tls, which phase 2 of federation adds"
        )
    if not definition.outbound:
        raise EventRejected(f"mail to peer {peer_id} is off")
    if sender not in definition.senders:
        raise EventRejected(f"{sender} may not send to peer {peer_id}")
    if recipient.party not in definition.remote:
        raise EventRejected(f"{recipient} is not a party of peer {peer_id} to address")
    if priority:
        check_outbound_priority(definition, sender, recipient)
    if len(body.encode("utf-8")) > definition.max_body_bytes:
        raise EventRejected(
            f"the body exceeds the {definition.max_body_bytes} bytes peer {peer_id}"
            " accepts"
        )


def check_outbound_priority(
    definition: PeerDefinition, sender: Address, recipient: RemoteAddress
) -> None:
    """Raise ``EventRejected`` unless ``sender`` may mark mail to ``recipient``
    priority, which needs the peer's ``priority_out``. As locally, only a line
    receives priority mail and an external agent sends none. The sender's rate limit
    is the scheduler's, and the recipient's the peer's."""
    if not definition.priority_out:
        raise EventRejected(
            f"priority mail to peer {recipient.peer_id} is off, so send the note"
            " without priority"
        )
    if not isinstance(recipient.party, LineAddress):
        raise EventRejected("priority mail goes to a line, not to an operator")
    if isinstance(sender, AgentAddress):
        raise EventRejected(
            "an external agent may not send priority mail, which only operators"
            " and lines may send"
        )


def announced_name(root: Path, discloses: Callable[[str], bool]) -> str:
    """The name this daemon gives itself in ``hello``, its project directory's name
    when that is an identifier the disclosure list does not cover. Advisory only, since
    the peer names this daemon by its token."""
    name = root.name.lower()
    if ID_PATTERN.fullmatch(name) is None or discloses(name):
        return DEFAULT_NAME
    return name


def orphaned(state: State, federation: FederationConfig) -> list[str]:
    """The mail waiting for a peer that is removed or no longer declared, which is
    returned with ``unknown_peer``, oldest first within each peer."""
    found: list[str] = []
    for peer_id, waiting in state.outbox.items():
        peer = state.peers.get(peer_id)
        if peer_id not in federation.peers or peer is None or peer.removed:
            found.extend(waiting)
    return found


def sent_times(entries: Iterable[Entry], wanted: set[str]) -> dict[str, datetime]:
    """When each message in ``wanted`` was sent, from its ``message_sent`` entry's
    ``recorded_at``, so that expiry survives a restart. A time that does not parse is
    left out, and the message is then dated from when the forwarder first sees it."""
    found: dict[str, datetime] = {}
    for entry in entries:
        event = entry.event
        if isinstance(event, MessageSent) and event.message_id in wanted:
            with contextlib.suppress(ValueError):
                found[event.message_id] = datetime.fromisoformat(entry.recorded_at)
    return found


def envelope(state: State, message_id: str, message: Message) -> dict[str, object]:
    """The ``deliver`` arguments for ``message``. Each address is written as its own
    daemon writes it, and ``reply_to`` carries the peer's own identifier for the
    message answered. Raises ``EventRejected`` when that message holds none, which
    the model's reply rule prevents."""
    recipient = message.recipient
    if not isinstance(recipient, RemoteAddress):
        raise EventRejected(f"message {message_id} is not to a peer")
    reply_to: str | None = None
    if message.reply_to is not None:
        parent = state.messages.get(message.reply_to)
        if parent is None or parent.origin_id is None:
            raise EventRejected(f"message {message.reply_to} came from no peer")
        reply_to = parent.origin_id
    return {
        "origin_id": message_id,
        "sender": str(message.sender),
        "recipient": str(recipient.party),
        "kind": MessageKind.NOTE.value,
        "body": message.body,
        "reply_to": reply_to,
        "priority": message.priority,
    }


@dataclass(slots=True)
class UnixLink:
    """A link over a peer's ``peers.sock``."""

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def exchange(self, request: Request) -> Response:
        try:
            self.writer.write(encode_request(request, MAX_PEER_LINE_BYTES))
            await self.writer.drain()
            line = await asyncio.wait_for(
                self.reader.readline(), ANSWER_TIMEOUT_SECONDS
            )
            if not line.endswith(b"\n"):
                raise LinkError("the peer closed the connection without an answer")
            response = decode_response(line)
        except (OSError, TimeoutError, ValueError, ProtocolError) as error:
            raise LinkError(f"the exchange with the peer failed: {error}") from error
        if response.request_id != request.request_id:
            raise LinkError("the peer answered another request")
        return response

    async def close(self) -> None:
        self.writer.close()
        with contextlib.suppress(OSError):
            await self.writer.wait_closed()


async def connect_unix(peer_id: str, definition: PeerDefinition) -> Link:
    """Open a link to ``peer_id``'s ``peers.sock``. A ``tls`` peer waits for phase 2,
    and is refused here as a transient failure, so its mail waits and expires."""
    if definition.transport != TRANSPORT_UNIX:
        raise LinkError(f"peer {peer_id} is reached over tls, which phase 2 adds")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(definition.socket), limit=MAX_LINE_BYTES),
            CONNECT_TIMEOUT_SECONDS,
        )
    except (OSError, TimeoutError) as error:
        raise LinkError(f"could not connect to peer {peer_id}: {error}") from error
    return UnixLink(reader, writer)


class Phase(StrEnum):
    IDLE = "idle"
    SENDING = "sending"
    BACKING_OFF = "backing_off"


@dataclass(frozen=True, slots=True)
class PeerReport:
    """One peer's delivery as ``status`` and ``line_status`` show it, so that an
    outage is seen while it lasts rather than when its mail expires."""

    peer_id: str
    phase: Phase
    waiting: int
    oldest: str | None
    """The message at the head of the outbox, which every later one waits behind."""
    oldest_sent_at: datetime | None
    last_contact: datetime | None
    """When the peer last answered anything, ``hello`` included, since the daemon
    started."""
    backoff_seconds: int
    """Whole seconds, since the client protocol carries no floats."""
    fault: str | None
    faults: int

    def wire(self, *, detail: bool) -> dict[str, object]:
        """The report as a status result carries it. A line's view leaves out the
        backoff and the fault, whose text may name the peer's socket or a path of
        this daemon, which the project file holds and a line may not read."""
        found: dict[str, object] = {
            "peer": self.peer_id,
            "state": self.phase.value,
            "waiting": self.waiting,
            "oldest": self.oldest,
            "oldest_sent_at": _instant(self.oldest_sent_at),
            "last_contact": _instant(self.last_contact),
        }
        if detail:
            found["backoff_seconds"] = self.backoff_seconds
            found["fault"] = self.fault
            found["faults"] = self.faults
        return found


def outage_text(report: PeerReport) -> str:
    """The outage note for ``report``. It leaves out the fault, whose text may name a
    path, and points to ``dromon status``, which shows it to the operator alone."""
    waiting = (
        "1 message waits" if report.waiting == 1 else f"{report.waiting} messages wait"
    )
    contact = (
        "It has not answered since the daemon started."
        if report.last_contact is None
        else f"It last answered at {format_due(report.last_contact)}."
    )
    return (
        f"Dromon has not delivered mail to peer {report.peer_id} for an hour."
        f" {waiting}, the oldest {report.oldest} since"
        f" {_instant(report.oldest_sent_at)}. {contact} dromon status shows the"
        " peer's state and any fault. No further note is sent for this outage, which"
        " ends when the peer accepts mail again."
    )


def _instant(value: datetime | None) -> str | None:
    return None if value is None else format_due(value)


def _request_id() -> str:
    return f"f-{secrets.token_hex(6)}"


@dataclass(slots=True)
class PeerForwarder:
    """Delivers one peer's outbox in order. ``step`` takes one action."""

    peer_id: str
    definition: PeerDefinition
    outbox: Outbox
    connect: Connector
    token: Callable[[], str | None]
    """Reads the token this daemon presents to the peer, ``None`` when none is
    stored. Read at each connection, so a token stored while the daemon runs is used
    at the next one."""
    name: str
    clock: Callable[[], float]
    wall_clock: Callable[[], datetime]
    sent_at: dict[str, datetime] = field(default_factory=dict[str, datetime])
    possibly_sent: set[str] = field(default_factory=set[str])
    """Messages a copy of which may have reached the peer without an answer."""
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    phase: Phase = Phase.IDLE
    link: Link | None = None
    backoff: float = 0.0
    not_before: float = 0.0
    busy: int = 0
    busy_for: str | None = None
    fault: str | None = None
    """The last unexpected exception, until a message is accepted again."""
    faults: int = 0
    """Unexpected exceptions since the daemon started."""
    last_contact: datetime | None = None
    outage_told: bool = False
    """Whether the operator was told of the outage under way, kept in memory, so a
    restart may tell once more."""

    def report(self) -> PeerReport:
        state = self.outbox.state
        waiting = state.outbox.get(self.peer_id, {})
        oldest = next(iter(waiting), None)
        return PeerReport(
            self.peer_id,
            self.phase,
            len(waiting),
            oldest,
            None if oldest is None else self.sent_at.get(oldest),
            self.last_contact,
            round(self.backoff),
            self.fault,
            self.faults,
        )

    def notify(self, message_id: str) -> None:
        """A message joined the outbox now."""
        self.sent_at.setdefault(message_id, self.wall_clock())
        self.ready.set()

    def head(self) -> tuple[str, Message] | None:
        state = self.outbox.state
        waiting = state.outbox.get(self.peer_id)
        if not waiting:
            return None
        message_id = next(iter(waiting))
        return message_id, state.messages[message_id]

    async def step(self) -> float | None:
        """Take one action and return the seconds to wait before the next, zero to
        go on at once, or ``None`` to wait until mail arrives."""
        head = self.head()
        if head is None:
            await self.drop_link()
            self.phase = Phase.IDLE
            return None
        message_id, message = head
        if self.judge(message_id, message):
            return 0.0
        self.watch_outage(message_id)
        now = self.clock()
        if now < self.not_before:
            self.phase = Phase.BACKING_OFF
            return self.not_before - now
        self.phase = Phase.SENDING
        try:
            link = await self.open_link()
        except LinkError as error:
            return await self.fail(str(error))
        request = Request(
            _request_id(),
            Operation.DELIVER,
            envelope(self.outbox.state, message_id, message),
        )
        # From here a copy may reach the peer whatever happens to its answer. An
        # answer that says nothing was recorded clears the mark only when no earlier
        # copy went unanswered.
        earlier = message_id in self.possibly_sent
        self.possibly_sent.add(message_id)
        try:
            response = await link.exchange(request)
        except LinkError as error:
            return await self.fail(str(error))
        return await self.answer(message_id, response, earlier)

    def judge(self, message_id: str, message: Message) -> bool:
        """End ``message`` here if it has expired, or no longer passes the policy or
        the disclosure list, and say whether it ended."""
        sent = self.sent_at.setdefault(message_id, self.wall_clock())
        if self.wall_clock() - sent >= timedelta(hours=self.definition.expire_hours):
            self.expire(message_id)
            return True
        recipient = cast(RemoteAddress, message.recipient)
        code: ReturnCode | None = None
        if (
            not self.definition.outbound
            or message.sender not in self.definition.senders
            or recipient.party not in self.definition.remote
        ):
            code = ReturnCode.REFUSED
        elif len(message.body.encode("utf-8")) > self.definition.max_body_bytes:
            code = ReturnCode.TOO_LARGE
        elif self.outbox.discloses(message.body, str(message.sender)):
            code = ReturnCode.DISCLOSURE
        if code is None:
            return False
        self.end(message_id)
        self.outbox.return_mail(message_id, code)
        return True

    def watch_outage(self, message_id: str) -> None:
        """Tell the operator once, when ``message_id`` at the head of the outbox has
        waited ``OUTAGE_SECONDS``, and no more until the peer accepts mail again.
        Checked at each step, so a note comes at most one backoff late. A note that
        fails is said on standard error and not tried again for this outage, so that
        telling never holds up delivery."""
        if self.outage_told:
            return
        sent = self.sent_at.get(message_id)
        if sent is None or self.wall_clock() - sent < timedelta(seconds=OUTAGE_SECONDS):
            return
        self.outage_told = True
        try:
            self.outbox.outage(self.report())
        except Exception as error:  # noqa: BLE001
            print(
                f"dromond: the outage note for peer {self.peer_id} failed: {error!r}",
                file=sys.stderr,
            )

    def expire(self, message_id: str) -> None:
        """End ``message_id`` as expired, unknown when a copy may have arrived."""
        possibly = message_id in self.possibly_sent
        self.end(message_id)
        if possibly:
            self.outbox.leave_unconfirmed(message_id)
        else:
            self.outbox.return_mail(message_id, ReturnCode.EXPIRED)

    def end(self, message_id: str) -> None:
        """Forget what was kept for ``message_id``, which leaves the outbox."""
        self.sent_at.pop(message_id, None)
        self.possibly_sent.discard(message_id)
        if self.busy_for == message_id:
            self.busy, self.busy_for = 0, None

    async def open_link(self) -> Link:
        """The open link, or a new one past ``hello``. Raises ``LinkError``."""
        if self.link is not None:
            return self.link
        token = self.token()
        if token is None and self.definition.transport == TRANSPORT_UNIX:
            raise LinkError(f"no token is stored for peer {self.peer_id}")
        link = await self.connect(self.peer_id, self.definition)
        args: dict[str, object] = {"versions": [FEDERATION_VERSION], "name": self.name}
        if token is not None:
            args["token"] = token
        try:
            response = await link.exchange(
                Request(_request_id(), Operation.HELLO, args)
            )
        except LinkError:
            await link.close()
            raise
        self.last_contact = self.wall_clock()
        version = None if response.result is None else response.result.get("version")
        if not response.ok or version != FEDERATION_VERSION:
            await link.close()
            raise LinkError(
                f"peer {self.peer_id} refused hello"
                + ("" if response.error_code is None else f", {response.error_code}")
            )
        self.link = link
        return link

    async def drop_link(self) -> None:
        """Close the link, if any. A link whose close fails is forgotten all the
        same, and the failure is said on standard error, so that closing can never
        end the forwarder."""
        link, self.link = self.link, None
        if link is None:
            return
        try:
            await link.close()
        except Exception as error:  # noqa: BLE001
            print(
                f"dromond: closing the link to peer {self.peer_id} failed: {error!r}",
                file=sys.stderr,
            )

    async def fault_found(self, error: Exception) -> float:
        """The watch. An exception that no rule above expects is a fault in this
        forwarder, not in the peer. It is written to standard error with its
        traceback and kept in ``fault`` until a message is accepted again, and the
        forwarder backs off as after a transient failure, so it neither dies, which
        would leave the outbox waiting unseen until shutdown, nor spins.

        Assumption. A fault raised after a ``deliver`` was written leaves the
        message marked as possibly sent, as ``step`` marked it, so the retry is a
        duplicate the peer answers from its idempotency key. A fault that recurs on
        every attempt holds the outbox at its head until the message expires, and
        the peer's report in ``status`` shows the fault to the operator."""
        self.fault = f"{type(error).__name__}: {error}"
        self.faults += 1
        trace = "".join(traceback.format_exception(error)).rstrip()
        print(
            f"dromond: the forwarder for peer {self.peer_id} failed unexpectedly,"
            f" fault {self.faults}, and backs off\n{trace}",
            file=sys.stderr,
        )
        return await self.fail(self.fault)

    async def fail(self, reason: str) -> float:
        """Back off after a transient failure, closing the link."""
        print(
            f"dromond: delivery to peer {self.peer_id} failed: {reason}",
            file=sys.stderr,
        )
        await self.drop_link()
        self.backoff = (
            FIRST_BACKOFF_SECONDS
            if self.backoff == 0
            else min(self.backoff * 2, MAX_BACKOFF_SECONDS)
        )
        self.not_before = self.clock() + self.backoff
        self.phase = Phase.BACKING_OFF
        return self.backoff

    async def answer(self, message_id: str, response: Response, earlier: bool) -> float:
        """Record what the peer's answer to ``message_id`` decides. ``earlier`` says
        whether a copy sent before this one may have reached the peer unanswered."""
        self.last_contact = self.wall_clock()
        result = response.result if response.ok else None
        outcome = None if result is None else result.get("outcome")
        if result is not None and outcome == "accepted":
            self.backoff = 0.0
            self.fault = None
            self.outage_told = False
            self.end(message_id)
            self.outbox.forwarded(message_id)
            return 0.0
        if result is not None and outcome == "refused":
            code = _code(result.get("code"))
            if code is not None:
                self.backoff = 0.0
                self.end(message_id)
                self.outbox.return_mail(message_id, code)
                return 0.0
        if result is not None and outcome == "busy":
            retry = result.get("retry_seconds")
            if type(retry) is int:
                if not earlier:
                    self.possibly_sent.discard(message_id)
                # The receiver closes a connection idle for 30 seconds, and a copy
                # written into a closed one would count as unanswered, so the link
                # is dropped for the wait.
                await self.drop_link()
                return self.wait_busy(message_id, retry)
        if not response.ok:
            # An error answer says the peer recorded nothing.
            if not earlier:
                self.possibly_sent.discard(message_id)
            return await self.fail(f"the peer answered {response.error_code}")
        return await self.fail("the peer's answer was malformed")

    def wait_busy(self, message_id: str, retry: int) -> float:
        """Wait as a busy peer asks, within the backoff's bounds, until the message's
        ``MAX_BUSY``-th busy answer ends it."""
        if self.busy_for != message_id:
            self.busy, self.busy_for = 0, message_id
        self.busy += 1
        if self.busy >= MAX_BUSY:
            self.expire(message_id)
            return 0.0
        wait = float(min(max(retry, 1), int(MAX_BACKOFF_SECONDS)))
        self.not_before = self.clock() + wait
        self.phase = Phase.BACKING_OFF
        return wait

    async def run(self, stop: asyncio.Event) -> None:
        """Deliver until ``stop`` is set. New mail ends an idle wait, and never a
        backoff, which would reorder nothing but would defeat the backoff."""
        try:
            while not stop.is_set():
                self.ready.clear()
                try:
                    wait = await self.step()
                except EventRejected as error:
                    # The log refused what the forwarder recorded, which is a fault
                    # here. Backing off keeps it from spinning.
                    wait = await self.fail(f"the log refused a record: {error}")
                except Exception as error:  # noqa: BLE001
                    wait = await self.fault_found(error)
                if wait == 0:
                    await asyncio.sleep(0)
                    continue
                waits = [asyncio.ensure_future(stop.wait())]
                if wait is None:
                    waits.append(asyncio.ensure_future(self.ready.wait()))
                await asyncio.wait(
                    waits, timeout=wait, return_when=asyncio.FIRST_COMPLETED
                )
                for pending in waits:
                    pending.cancel()
        finally:
            await self.drop_link()


def _code(value: object) -> ReturnCode | None:
    if not isinstance(value, str):
        return None
    try:
        return ReturnCode(value)
    except ValueError:
        return None


class Forwarder:
    """One ``PeerForwarder`` for each declared peer, with the outbox's notices."""

    def __init__(
        self,
        outbox: Outbox,
        federation: FederationConfig,
        connect: Connector,
        token: Callable[[str], str | None],
        name: str,
        clock: Callable[[], float],
        wall_clock: Callable[[], datetime],
        sent_at: Mapping[str, datetime],
    ) -> None:
        """Every message waiting at start is marked as possibly sent, since a copy
        may have left before the restart."""
        self.peers: dict[str, PeerForwarder] = {}
        state = outbox.state
        for peer_id, definition in sorted(federation.peers.items()):
            waiting = state.outbox.get(peer_id, {})
            self.peers[peer_id] = PeerForwarder(
                peer_id,
                definition,
                outbox,
                connect,
                _token_for(token, peer_id),
                name,
                clock,
                wall_clock,
                sent_at={m: sent_at[m] for m in waiting if m in sent_at},
                possibly_sent=set(waiting),
            )

    def reports(self) -> list[PeerReport]:
        """Each declared peer's report, in name order."""
        return [self.peers[peer_id].report() for peer_id in sorted(self.peers)]

    def notify(self, peer_id: str, message_id: str) -> None:
        forwarder = self.peers.get(peer_id)
        if forwarder is not None:
            forwarder.notify(message_id)

    async def run(self, stop: asyncio.Event) -> None:
        await asyncio.gather(*(peer.run(stop) for peer in self.peers.values()))


def _token_for(
    token: Callable[[str], str | None], peer_id: str
) -> Callable[[], str | None]:
    return lambda: token(peer_id)
