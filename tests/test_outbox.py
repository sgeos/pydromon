"""Mail to peers, step 5 of federation phase 1. The outbound ``message_sent``, the
outbox, ``message_returned`` and ``message_unconfirmed``, the sending policy, the
forwarder with its transport seam, the bound on received keys, and the bound before
``hello``. The design is ``docs/architecture/FEDERATION.md``."""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Generator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import daemon as daemon_module
from dromon import model
from dromon.agents import token_digest
from dromon.codec import DecodeError, decode, encode
from dromon.configview import Overrides
from dromon.daemon import (
    ConnectionCap,
    Daemon,
    LineReader,
    PeerSession,
    peer_connection,
    watch_task,
)
from dromon.driver import Strategy
from dromon.eventlog import Entry, EventLog, read_entries
from dromon.federation import (
    FederationConfig,
    PeerDefinition,
    parse_federation,
    plan_peers,
)
from dromon.forwarder import (
    DEFAULT_NAME,
    FIRST_BACKOFF_SECONDS,
    MAX_BACKOFF_SECONDS,
    MAX_BUSY,
    OUTAGE_SECONDS,
    Forwarder,
    Link,
    LinkError,
    PeerForwarder,
    Phase,
    UnixLink,
    announced_name,
    check_outbound,
    check_outbound_priority,
    connect_unix,
    envelope,
    orphaned,
    outage_text,
    sent_times,
)
from dromon.mailbox import sent
from dromon.model import (
    HARNESS,
    Address,
    AgentAddress,
    RECEIVED_KEY_HOURS,
    Event,
    EventRejected,
    LineAddress,
    LineCreated,
    MessageDelivered,
    MessageKind,
    MessageReceived,
    MessageReturned,
    MessageSent,
    MessageUnconfirmed,
    OperatorAddress,
    OperatorRegistered,
    PeerDeclared,
    PeerKeyed,
    PeerRemoved,
    RemoteAddress,
    ReturnCode,
    State,
    returned_note,
    unconfirmed_note,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.peering import MAX_HELLO_LINE_BYTES
from dromon.priority import MAX_PER_SENDER
from dromon.projectconfig import ProjectConfig
from dromon.protocol import (
    MAX_PEER_LINE_BYTES,
    Operation,
    Request,
    Response,
    encode_response,
)
from dromon.scheduler import LineEngine, LineSpec, Scheduler

PEER = "keleusma"
ALPHA = LineAddress("alpha")
BETA = LineAddress("beta")
ANA = OperatorAddress("ana")
KAIZEN = RemoteAddress(PEER, LineAddress("kaizen"))
KAIZEN_ANA = RemoteAddress(PEER, ANA)
TOKEN = "T" * 43
AT = "2026-10-28T00:00:00Z"


# The model.


def outbound_state() -> State:
    state = State()
    state.apply(LineCreated("alpha", "claude-haiku-5-5", "subscription"))
    state.apply(LineCreated("beta", "claude-haiku-5-5", "subscription"))
    state.apply(OperatorRegistered("ana"))
    state.apply(PeerDeclared(PEER, "unix", "{}"))
    return state


def to_peer(
    message_id: str,
    sender: LineAddress | OperatorAddress = ALPHA,
    recipient: RemoteAddress = KAIZEN,
    reply_to: str | None = None,
) -> MessageSent:
    return MessageSent(
        message_id, sender, recipient, MessageKind.NOTE, "x", reply_to=reply_to
    )


def test_mail_to_a_peer_waits_in_its_outbox_in_the_order_sent() -> None:
    state = outbound_state()
    for message_id in ("m1", "m2", "m3"):
        state.apply(to_peer(message_id))
    assert list(state.outbox[PEER]) == ["m1", "m2", "m3"]
    state.apply(MessageDelivered("m1"))
    state.apply(MessageReturned("m2", ReturnCode.REFUSED, "n2"))
    assert list(state.outbox[PEER]) == ["m3"]
    state.apply(MessageUnconfirmed("m3", "n3"))
    assert PEER not in state.outbox


def test_a_copy_of_the_state_has_its_own_outbox() -> None:
    state = outbound_state()
    state.apply(to_peer("m1"))
    copy = state.copy()
    copy.apply(MessageDelivered("m1"))
    assert list(state.outbox[PEER]) == ["m1"]


def test_only_a_note_to_a_declared_peer_joins_an_outbox() -> None:
    state = outbound_state()
    with pytest.raises(EventRejected, match="only notes"):
        state.apply(
            MessageSent("q1", ALPHA, KAIZEN_ANA, MessageKind.QUESTION, "?", ("y",))
        )
    with pytest.raises(EventRejected, match="no peer other"):
        state.apply(to_peer("m1", recipient=RemoteAddress("other", ALPHA)))
    state.apply(PeerRemoved(PEER))
    with pytest.raises(EventRejected, match=f"no peer {PEER}"):
        state.apply(to_peer("m1"))


def test_an_outbox_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model, "MAX_OUTBOX", 2)
    state = outbound_state()
    state.apply(to_peer("m1"))
    state.apply(to_peer("m2"))
    with pytest.raises(EventRejected, match="2 messages already wait"):
        state.apply(to_peer("m3"))
    state.apply(MessageDelivered("m1"))
    state.apply(to_peer("m3"))


def test_a_reply_to_a_peer_follows_the_route_of_the_message_it_answers() -> None:
    state = outbound_state()
    state.apply(MessageReceived("r1", KAIZEN, ALPHA, "hi", None, "o1", AT))
    state.apply(to_peer("m1", reply_to="r1"))
    with pytest.raises(EventRejected, match="must go from"):
        state.apply(to_peer("m2", recipient=KAIZEN_ANA, reply_to="r1"))
    with pytest.raises(EventRejected, match="must go from"):
        state.apply(to_peer("m3", sender=BETA, reply_to="r1"))


def test_returned_mail_from_a_line_notes_the_line() -> None:
    state = outbound_state()
    state.apply(to_peer("m1"))
    state.apply(MessageReturned("m1", ReturnCode.TOO_LARGE, "n1"))
    assert state.messages["m1"].returned is ReturnCode.TOO_LARGE
    note = state.messages["n1"]
    assert (note.sender, note.recipient, note.kind) == (ALPHA, ALPHA, MessageKind.NOTE)
    assert note.body == returned_note("m1", KAIZEN, ReturnCode.TOO_LARGE)
    assert "too_large" in note.body and str(KAIZEN) in note.body


def test_unconfirmed_mail_from_a_line_notes_the_line() -> None:
    state = outbound_state()
    state.apply(to_peer("m1"))
    state.apply(MessageUnconfirmed("m1", "n1"))
    assert state.messages["m1"].unconfirmed
    assert state.messages["n1"].body == unconfirmed_note("m1", KAIZEN)
    assert "may or may not" in state.messages["n1"].body


@pytest.mark.parametrize(
    "event",
    [
        MessageReturned("m1", ReturnCode.REFUSED, "n1"),
        MessageUnconfirmed("m1", "n1"),
    ],
)
def test_mail_from_an_operator_has_no_note(
    event: MessageReturned | MessageUnconfirmed,
) -> None:
    state = outbound_state()
    state.apply(to_peer("m1", sender=ANA))
    with pytest.raises(EventRejected, match="not a line"):
        state.apply(event)
    state.apply(replace(event, note_id=None))
    assert "n1" not in state.messages


@pytest.mark.parametrize(
    "ending",
    [
        lambda: MessageReturned("m1", ReturnCode.EXPIRED, "n9"),
        lambda: MessageUnconfirmed("m1", "n9"),
        lambda: MessageDelivered("m1"),
    ],
)
def test_mail_leaves_its_outbox_once(ending: Callable[[], Event]) -> None:
    state = outbound_state()
    state.apply(to_peer("m1"))
    state.apply(MessageReturned("m1", ReturnCode.EXPIRED, "n1"))
    with pytest.raises(EventRejected, match="left its peer's outbox"):
        state.apply(ending())


def test_an_ending_needs_mail_to_a_peer_and_a_new_note() -> None:
    state = outbound_state()
    state.apply(MessageSent("m0", ALPHA, ANA, MessageKind.NOTE, "local"))
    state.apply(to_peer("m1"))
    with pytest.raises(EventRejected, match="not to a peer"):
        state.apply(MessageReturned("m0", ReturnCode.REFUSED, "n1"))
    with pytest.raises(EventRejected, match="owed a note"):
        state.apply(MessageUnconfirmed("m1", None))
    with pytest.raises(EventRejected, match="message m0 exists"):
        state.apply(MessageReturned("m1", ReturnCode.REFUSED, "m0"))
    with pytest.raises(EventRejected, match="no message m9"):
        state.apply(MessageUnconfirmed("m9", None))


def test_a_returned_event_round_trips_and_an_unknown_code_does_not_decode() -> None:
    event = MessageReturned("m1", ReturnCode.CONFLICT, None)
    assert decode(encode(event)) == event
    payload = json.loads(encode(event))
    payload["data"]["code"] = "lost"
    with pytest.raises(DecodeError, match="code"):
        decode(json.dumps(payload))


def test_every_return_code_has_a_meaning() -> None:
    assert set(model.RETURN_MEANINGS) == set(ReturnCode)


# The bound on received keys, decided by the operator on 2026-10-28.


def at(hours: float) -> str:
    instant = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(hours=hours)
    return model.format_due(instant)


def test_a_received_key_is_dropped_once_older_than_the_window() -> None:
    state = outbound_state()
    state.apply(MessageReceived("r1", KAIZEN, ALPHA, "a", None, "o1", at(0)))
    state.apply(MessageReceived("r2", KAIZEN, ALPHA, "b", None, "o2", at(10)))
    state.apply(
        MessageReceived("r3", KAIZEN, ALPHA, "c", None, "o3", at(RECEIVED_KEY_HOURS))
    )
    assert (PEER, "o1") in state.received
    state.apply(
        MessageReceived(
            "r4", KAIZEN, ALPHA, "d", None, "o4", at(RECEIVED_KEY_HOURS + 1)
        )
    )
    assert (PEER, "o1") not in state.received
    assert (PEER, "o1") not in state.received_times
    assert (PEER, "o2") in state.received
    # A resend past the window is recorded again, the cost the decision accepts.
    state.apply(
        MessageReceived(
            "r5", KAIZEN, ALPHA, "a", None, "o1", at(RECEIVED_KEY_HOURS + 2)
        )
    )
    assert state.received[(PEER, "o1")] == "r5"


def test_a_key_within_the_window_still_refuses_a_second_record() -> None:
    state = outbound_state()
    state.apply(MessageReceived("r1", KAIZEN, ALPHA, "a", None, "o1", at(0)))
    with pytest.raises(EventRejected, match="delivered o1 before"):
        state.apply(
            MessageReceived(
                "r2", KAIZEN, ALPHA, "a", None, "o1", at(RECEIVED_KEY_HOURS - 1)
            )
        )


def test_the_window_covers_the_longest_expiry() -> None:
    from dromon.federation import MAX_EXPIRE_HOURS

    assert RECEIVED_KEY_HOURS > MAX_EXPIRE_HOURS


# The sending policy.


def federation(**keys: object) -> FederationConfig:
    table: dict[str, object] = {
        "socket": "/srv/keleusma/run/peers.sock",
        "outbound": True,
        "senders": ["alpha", "@ana"],
        "remote": ["kaizen", "@ana"],
        **keys,
    }
    return parse_federation({}, {PEER: table})


def test_the_policy_lets_a_listed_sender_address_a_listed_party() -> None:
    check_outbound(outbound_state(), federation(), ALPHA, KAIZEN, "x", priority=False)


@pytest.mark.parametrize(
    ("config", "sender", "recipient", "body", "priority", "match"),
    [
        (federation(), ALPHA, RemoteAddress("other", ALPHA), "x", False, "no peer"),
        (federation(outbound=False), ALPHA, KAIZEN, "x", False, "is off"),
        (federation(), BETA, KAIZEN, "x", False, "may not send"),
        (
            federation(),
            ALPHA,
            RemoteAddress(PEER, LineAddress("other")),
            "x",
            False,
            "not a party",
        ),
        (federation(), ALPHA, KAIZEN, "x", True, "priority mail to peer"),
        (federation(max_body_bytes=4), ALPHA, KAIZEN, "é" * 3, False, "exceeds"),
    ],
)
def test_the_policy_refuses_what_it_does_not_list(
    config: FederationConfig,
    sender: LineAddress,
    recipient: RemoteAddress,
    body: str,
    priority: bool,
    match: str,
) -> None:
    with pytest.raises(EventRejected, match=match):
        check_outbound(
            outbound_state(), config, sender, recipient, body, priority=priority
        )


def test_priority_out_lets_a_sender_mark_mail_to_a_peers_line() -> None:
    check_outbound(
        outbound_state(),
        federation(priority_out=True),
        ALPHA,
        KAIZEN,
        "x",
        priority=True,
    )


@pytest.mark.parametrize(
    ("sender", "recipient", "match"),
    [
        (ALPHA, KAIZEN_ANA, "goes to a line"),
        (AgentAddress("claude"), KAIZEN, "external agent"),
    ],
)
def test_priority_to_a_peer_follows_the_local_rules(
    sender: Address, recipient: RemoteAddress, match: str
) -> None:
    definition = federation(priority_out=True).peers[PEER]
    with pytest.raises(EventRejected, match=match):
        check_outbound_priority(definition, sender, recipient)


def test_the_policy_refuses_a_removed_peer() -> None:
    state = outbound_state()
    state.apply(PeerRemoved(PEER))
    with pytest.raises(EventRejected, match="no peer"):
        check_outbound(state, federation(), ALPHA, KAIZEN, "x", priority=False)


def test_the_policy_is_the_same_for_a_tls_peer() -> None:
    """Phase 2 step 5. The transport decides how mail travels, never whether it may
    be sent, so a ``tls`` peer passes and fails exactly where a ``unix`` peer does."""
    tls = federation()
    tls.peers[PEER] = replace(tls.peers[PEER], transport="tls")
    check_outbound(outbound_state(), tls, ALPHA, KAIZEN, "x", priority=False)
    tls.peers[PEER] = replace(tls.peers[PEER], outbound=False)
    with pytest.raises(EventRejected, match="mail to peer .* is off"):
        check_outbound(outbound_state(), tls, ALPHA, KAIZEN, "x", priority=False)


# The scheduler and the forwarder, against a scripted peer.


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class WallClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 28, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


type Answer = Response | LinkError


def accepted(duplicate: bool = False) -> Response:
    result: dict[str, object] = {
        "outcome": "accepted",
        "message_id": "m-there",
        "duplicate": duplicate,
        "priority": False,
    }
    return Response("", ok=True, result=result)


def refused(code: str) -> Response:
    return Response(
        "", ok=True, result={"outcome": "refused", "code": code, "retry": False}
    )


def busy(seconds: int) -> Response:
    return Response("", ok=True, result={"outcome": "busy", "retry_seconds": seconds})


WELCOME = Response("", ok=True, result={"version": 1})


class ScriptedPeer:
    """A connector whose links answer from one script, in order."""

    def __init__(self) -> None:
        self.answers: list[Answer] = []
        self.requests: list[Request] = []
        self.connects = 0
        self.down = False
        self.closed = 0

    async def connect(self, peer_id: str, definition: PeerDefinition) -> Link:
        self.connects += 1
        if self.down:
            raise LinkError("the peer is down")
        return ScriptedLink(self)

    def deliveries(self) -> list[dict[str, object]]:
        return [r.args for r in self.requests if r.operation is Operation.DELIVER]


class ScriptedLink:
    def __init__(self, peer: ScriptedPeer) -> None:
        self.peer = peer

    async def exchange(self, request: Request) -> Response:
        self.peer.requests.append(request)
        answer = self.peer.answers.pop(0)
        if isinstance(answer, LinkError):
            raise answer
        return replace(answer, request_id=request.request_id)

    async def close(self) -> None:
        self.peer.closed += 1


class Out:
    def __init__(self, path: Path, config: FederationConfig) -> None:
        self.log = EventLog(path)
        self.scheduler = Scheduler(self.log, no_engine, hold_wakes=True)
        self.scheduler.create_line("alpha", "claude-haiku-5-5", Strategy.SUBSCRIPTION)
        self.scheduler.create_line("beta", "claude-haiku-5-5", Strategy.SUBSCRIPTION)
        self.scheduler.ensure_operator("ana")
        for event in plan_peers(self.log.state, config):
            self.log.append(event)
        self.scheduler.federation = config
        self.config = config
        self.peer = ScriptedPeer()
        self.clock = Clock()
        self.wall = WallClock()
        self.token: str | None = TOKEN
        self.forwarder = self.make_forwarder()

    def make_forwarder(self) -> Forwarder:
        forwarder = Forwarder(
            self.scheduler,
            self.config,
            self.peer.connect,
            lambda peer_id: self.token,
            "core",
            self.clock,
            self.wall,
            {},
        )
        self.scheduler.on_outbox = forwarder.notify
        return forwarder

    @property
    def one(self) -> PeerForwarder:
        return self.forwarder.peers[PEER]

    def step(self) -> float | None:
        return asyncio.run(self.one.step())

    def send(
        self,
        body: str = "hello",
        sender: LineAddress | OperatorAddress = ALPHA,
        to: RemoteAddress = KAIZEN,
        reply_to: str | None = None,
    ) -> str:
        return self.scheduler.send(sender, to, body, reply_to)


@pytest.fixture
def out(tmp_path: Path) -> Generator[Out]:
    world = Out(tmp_path / "events.db", federation())
    yield world
    world.log.close()


def test_a_send_to_a_peer_records_it_and_wakes_the_forwarder(out: Out) -> None:
    message_id = out.send()
    message = out.log.state.messages[message_id]
    assert message.recipient == KAIZEN
    assert list(out.log.state.outbox[PEER]) == [message_id]
    assert out.one.ready.is_set()
    assert out.one.sent_at[message_id] == out.wall.now
    assert message_id not in out.scheduler.deliverable("alpha")


def test_a_send_refused_by_policy_records_nothing(out: Out) -> None:
    before = len(out.log.state.messages)
    with pytest.raises(EventRejected, match="may not send"):
        out.send(sender=BETA)
    assert len(out.log.state.messages) == before


def test_an_operators_mail_to_a_peer_is_held_to_the_disclosure_list(out: Out) -> None:
    out.scheduler.set_disclosure_terms(["secretword"])
    with pytest.raises(EventRejected):
        out.send("about the secretword", sender=ANA)
    assert PEER not in out.log.state.outbox


def test_a_line_sends_to_a_peer_with_its_tool(out: Out) -> None:
    tool = out.scheduler.send_tool("alpha")

    async def call() -> dict[str, Any]:
        return await tool.handler({"to": "keleusma:kaizen", "body": "hi"})

    assert "sent as" in str(asyncio.run(call()))
    assert len(out.log.state.outbox[PEER]) == 1


@pytest.fixture
def urgent(tmp_path: Path) -> Generator[Out]:
    world = Out(tmp_path / "events.db", federation(priority_out=True))
    yield world
    world.log.close()


def test_priority_mail_to_a_peer_is_marked_and_asks_in_its_envelope(
    urgent: Out,
) -> None:
    message_id = urgent.scheduler.send(ALPHA, KAIZEN, "now", priority=True)
    assert urgent.log.state.messages[message_id].priority
    urgent.peer.answers = [WELCOME, accepted()]
    assert urgent.step() == 0.0
    assert urgent.peer.deliveries()[0]["priority"] is True
    assert urgent.log.state.messages[message_id].delivered


def test_a_lines_priority_mail_to_peers_counts_toward_its_limit(urgent: Out) -> None:
    for _ in range(MAX_PER_SENDER):
        urgent.scheduler.send(ALPHA, KAIZEN, "now", priority=True)
    before = len(urgent.log.state.messages)
    with pytest.raises(EventRejected, match="priority messages"):
        urgent.scheduler.send(ALPHA, KAIZEN, "now", priority=True)
    assert len(urgent.log.state.messages) == before
    # An operator's priority mail is unlimited, as it is locally.
    for _ in range(MAX_PER_SENDER + 1):
        urgent.scheduler.send(ANA, KAIZEN, "now", priority=True)


def test_the_forwarder_says_hello_then_delivers_in_order(out: Out) -> None:
    first, second = out.send("one"), out.send("two")
    out.peer.answers = [WELCOME, accepted(), accepted(duplicate=True)]
    assert out.step() == 0.0
    assert out.step() == 0.0
    assert out.step() is None
    hello = out.peer.requests[0]
    assert hello.operation is Operation.HELLO
    assert hello.args == {"versions": [1], "name": "core", "token": TOKEN}
    assert [d["origin_id"] for d in out.peer.deliveries()] == [first, second]
    assert out.peer.deliveries()[0] == {
        "origin_id": first,
        "sender": "alpha",
        "recipient": "kaizen",
        "kind": "note",
        "body": "one",
        "reply_to": None,
        "priority": False,
    }
    assert out.log.state.messages[first].delivered
    assert out.log.state.messages[second].delivered
    assert out.peer.connects == 1 and out.one.phase is Phase.IDLE
    statuses, _ = sent(out.log.state, ALPHA)
    assert {s.delivery for s in statuses} == {"delivered to peer"}


@pytest.mark.parametrize("stored", [TOKEN, None])
def test_hello_over_tls_carries_no_token(tmp_path: Path, stored: str | None) -> None:
    """The pinned key names the daemon over TLS, so a token stored for the peer is
    not sent, and none is needed."""
    config = parse_federation(
        {},
        {
            PEER: {
                "transport": "tls",
                "address": "studio.example.net:7411",
                "pin_sha256": "0123456789abcdef" * 4,
                "certificate": "peers/keleusma.pem",
                "outbound": True,
                "senders": ["alpha"],
                "remote": ["kaizen"],
            }
        },
    )
    world = Out(tmp_path / "events.db", config)
    try:
        world.token = stored
        world.peer.answers = [WELCOME]
        asyncio.run(world.one.open_link())
        assert world.peer.requests[0].args == {"versions": [1], "name": "core"}
    finally:
        world.log.close()


def test_only_an_answer_records_delivery(out: Out) -> None:
    """A link that took the writes but never answered, as a TLS link does when the
    server refuses the client's certificate, delivers nothing."""
    message_id = out.send()
    out.peer.answers = [LinkError("no answer to hello")]
    assert out.step() == FIRST_BACKOFF_SECONDS
    assert out.peer.deliveries() == []
    assert message_id not in out.one.possibly_sent
    out.clock.now += FIRST_BACKOFF_SECONDS
    out.peer.answers = [WELCOME, LinkError("no answer to deliver")]
    assert out.step() == 2 * FIRST_BACKOFF_SECONDS
    assert len(out.peer.deliveries()) == 1
    assert message_id in out.one.possibly_sent
    assert not out.log.state.messages[message_id].delivered
    assert list(out.log.state.outbox[PEER]) == [message_id]


def test_a_unix_link_closed_without_an_answer_fails() -> None:
    async def scenario() -> None:
        reader = asyncio.StreamReader()
        reader.feed_eof()
        written = Written()
        link = UnixLink(reader, cast(asyncio.StreamWriter, written))
        try:
            await link.exchange(Request("f-1", Operation.HELLO, {"versions": [1]}))
        finally:
            assert written.data.endswith(b"\n")

    with pytest.raises(LinkError, match="without an answer"):
        asyncio.run(scenario())


def test_a_reply_carries_the_peers_own_identifier(out: Out) -> None:
    out.log.append(MessageReceived("r1", KAIZEN, ALPHA, "hi", None, "o-there", AT))
    out.send("back", reply_to="r1")
    out.peer.answers = [WELCOME, accepted()]
    out.step()
    assert out.peer.deliveries()[0]["reply_to"] == "o-there"


def test_a_refusal_returns_the_mail_with_its_code_and_notes_the_line(
    out: Out,
) -> None:
    message_id = out.send()
    out.peer.answers = [WELCOME, refused("full")]
    assert out.step() == 0.0
    message = out.log.state.messages[message_id]
    assert message.returned is ReturnCode.FULL
    notes = [
        (mid, m)
        for mid, m in out.scheduler.pending("alpha")
        if m.sender == ALPHA and "full" in m.body
    ]
    assert len(notes) == 1
    statuses, _ = sent(out.log.state, ALPHA)
    assert "returned full" in {s.delivery for s in statuses}


def test_an_operators_returned_mail_shows_where_sent_mail_is_listed(
    out: Out,
) -> None:
    message_id = out.send(sender=ANA, to=KAIZEN_ANA)
    out.peer.answers = [WELCOME, refused("refused")]
    out.step()
    statuses, _ = sent(out.log.state, ANA)
    assert [(s.message_id, s.delivery) for s in statuses] == [
        (message_id, "returned refused")
    ]


@pytest.mark.parametrize(
    "answer",
    [
        refused("a code of its own"),
        Response("", ok=True, result={"outcome": "lost"}),
        busy(cast(int, "soon")),
    ],
)
def test_a_malformed_answer_backs_off_and_may_have_arrived(
    out: Out, answer: Response
) -> None:
    message_id = out.send()
    out.peer.answers = [WELCOME, answer]
    assert out.step() == FIRST_BACKOFF_SECONDS
    assert message_id in out.one.possibly_sent
    assert out.peer.closed == 1 and out.one.link is None
    assert not out.log.state.messages[message_id].delivered


def test_backoff_doubles_to_its_ceiling_and_resets_on_success(out: Out) -> None:
    out.send()
    out.peer.down = True
    waits: list[float | None] = []
    for _ in range(10):
        waits.append(out.step())
        out.clock.now += cast(float, waits[-1])
    assert waits[:3] == [5.0, 10.0, 20.0]
    assert waits[-1] == MAX_BACKOFF_SECONDS
    out.peer.down = False
    out.peer.answers = [WELCOME, accepted()]
    assert out.step() == 0.0
    assert out.one.backoff == 0.0


def test_new_mail_does_not_cut_a_backoff_short(out: Out) -> None:
    out.send()
    out.peer.down = True
    assert out.step() == FIRST_BACKOFF_SECONDS
    out.clock.now += 1
    assert out.step() == FIRST_BACKOFF_SECONDS - 1
    assert out.peer.connects == 1 and out.one.phase is Phase.BACKING_OFF


def test_a_refused_hello_and_a_missing_token_back_off(
    out: Out, capsys: pytest.CaptureFixture[str]
) -> None:
    message_id = out.send()
    out.peer.answers = [Response("", ok=False, error_code=None, error_message="no")]
    assert out.step() == FIRST_BACKOFF_SECONDS
    assert out.peer.closed == 1
    out.token = None
    out.clock.now += 100
    assert out.step() == 2 * FIRST_BACKOFF_SECONDS
    assert out.peer.connects == 1
    assert message_id not in out.one.possibly_sent
    assert "no token is stored" in capsys.readouterr().err


def test_busy_waits_as_asked_and_ends_the_mail_after_the_cap(out: Out) -> None:
    message_id = out.send()
    # Each busy answer drops the link, so each retry says hello again.
    out.peer.answers = [
        answer for _ in range(MAX_BUSY) for answer in (WELCOME, busy(30))
    ]
    for _ in range(MAX_BUSY - 1):
        assert out.step() == 30.0
        out.clock.now += 30
    assert out.step() == 0.0
    # Every copy was answered, so none can have been recorded there.
    assert out.log.state.messages[message_id].returned is ReturnCode.EXPIRED


def test_a_busy_wait_is_bounded_by_the_backoff_ceiling(out: Out) -> None:
    out.send()
    out.peer.answers = [WELCOME, busy(10**6)]
    assert out.step() == MAX_BACKOFF_SECONDS
    # The link is dropped for the wait, which outlasts the receiver's idle timeout.
    assert out.one.link is None and out.peer.closed == 1


def test_expiry_returns_mail_that_never_left(out: Out) -> None:
    message_id = out.send()
    out.peer.down = True
    out.step()
    out.wall.now += timedelta(hours=out.config.peers[PEER].expire_hours)
    assert out.step() == 0.0
    assert out.log.state.messages[message_id].returned is ReturnCode.EXPIRED


def test_expiry_after_an_unanswered_copy_is_unconfirmed(out: Out) -> None:
    message_id = out.send()
    out.peer.answers = [WELCOME, LinkError("the answer was lost")]
    out.step()
    out.peer.answers = [WELCOME, busy(5)]
    out.clock.now += FIRST_BACKOFF_SECONDS
    out.step()
    # The busy answer does not clear the earlier unanswered copy.
    assert message_id in out.one.possibly_sent
    out.wall.now += timedelta(hours=out.config.peers[PEER].expire_hours)
    out.step()
    message = out.log.state.messages[message_id]
    assert message.unconfirmed and message.returned is None
    statuses, _ = sent(out.log.state, ALPHA)
    # The line's note about it is mail from the line to itself, listed too.
    found = {status.message_id: status.delivery for status in statuses}
    assert found[message_id] == "unconfirmed"


def test_mail_waiting_at_start_may_have_left_before_the_restart(out: Out) -> None:
    message_id = out.send()
    restarted = out.make_forwarder()
    assert message_id in restarted.peers[PEER].possibly_sent


def test_mail_is_checked_again_before_it_leaves(out: Out) -> None:
    first = out.send("fine")
    second = out.send("now covered by the list")
    third = out.send("x" * 64)
    out.scheduler.set_disclosure_terms(["covered"])
    out.one.definition = replace(out.one.definition, max_body_bytes=32)
    out.peer.answers = [WELCOME, accepted()]
    for _ in range(3):
        out.step()
    state = out.log.state
    assert state.messages[first].delivered
    assert state.messages[second].returned is ReturnCode.DISCLOSURE
    assert state.messages[third].returned is ReturnCode.TOO_LARGE
    later = out.send("later")
    out.one.definition = replace(out.one.definition, outbound=False)
    out.step()
    assert out.log.state.messages[later].returned is ReturnCode.REFUSED


def test_the_forwarder_runs_until_stopped(out: Out) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        running = asyncio.create_task(out.forwarder.run(stop))
        await asyncio.sleep(0)
        out.peer.answers = [WELCOME, accepted()]
        message_id = out.send()
        for _ in range(100):
            await asyncio.sleep(0)
            if out.log.state.messages[message_id].delivered:
                break
        assert out.log.state.messages[message_id].delivered
        stop.set()
        await asyncio.wait_for(running, 1)

    asyncio.run(scenario())
    assert out.peer.closed == 1


class Faulty(Exception):
    """An exception no rule of the forwarder expects."""


def test_the_watch_keeps_a_faulty_forwarder_running(
    out: Out, capsys: pytest.CaptureFixture[str]
) -> None:
    async def broken(peer_id: str, definition: PeerDefinition) -> Link:
        raise Faulty("a bug in the connector")

    out.one.connect = broken

    async def scenario() -> tuple[bool, str | None, int, float]:
        stop = asyncio.Event()
        running = asyncio.create_task(out.forwarder.run(stop))
        message_id = out.send()
        for _ in range(100):
            await asyncio.sleep(0)
            if out.one.faults:
                break
        seen = (running.done(), out.one.fault, out.one.faults, out.one.backoff)
        stop.set()
        await asyncio.wait_for(running, 1)
        assert list(out.log.state.outbox[PEER]) == [message_id]
        return seen

    done, fault, faults, backoff = asyncio.run(scenario())
    assert not done
    assert fault == "Faulty: a bug in the connector" and faults == 1
    assert backoff == FIRST_BACKOFF_SECONDS
    err = capsys.readouterr().err
    assert "the forwarder for peer keleusma failed unexpectedly" in err
    assert "Traceback" in err and "a bug in the connector" in err


def test_an_accepted_message_clears_the_fault(out: Out) -> None:
    out.send()
    assert asyncio.run(out.one.fault_found(Faulty("once"))) == FIRST_BACKOFF_SECONDS
    assert out.one.fault == "Faulty: once"
    out.clock.now += FIRST_BACKOFF_SECONDS
    out.peer.answers = [WELCOME, accepted()]
    assert out.step() == 0.0
    assert out.one.fault is None and out.one.faults == 1


def test_a_link_that_fails_to_close_is_forgotten(
    out: Out, capsys: pytest.CaptureFixture[str]
) -> None:
    class Unclosable(ScriptedLink):
        async def close(self) -> None:
            raise Faulty("stuck")

    out.one.link = Unclosable(out.peer)
    asyncio.run(out.one.drop_link())
    assert out.one.link is None
    assert "closing the link to peer keleusma failed" in capsys.readouterr().err


def test_the_outer_watch_says_how_a_task_ended(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        watch = watch_task("forwarder", stop)

        async def failing() -> None:
            raise Faulty("escaped")

        async def quiet() -> None:
            return None

        async def waiting() -> None:
            await asyncio.Event().wait()

        tasks = [asyncio.create_task(failing()), asyncio.create_task(quiet())]
        for task in tasks:
            task.add_done_callback(watch)
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
        assert capsys.readouterr().err.count("dromond: the forwarder ended") == 2
        stop.set()
        later = [asyncio.create_task(quiet()), asyncio.create_task(waiting())]
        for task in later:
            task.add_done_callback(watch)
        later[1].cancel()
        await asyncio.gather(*later, return_exceptions=True)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert capsys.readouterr().err == ""


# Peer state in status and line_status, step 6 of phase 1.


def test_a_peers_report_shows_what_waits_and_when_it_last_answered(out: Out) -> None:
    quiet = out.forwarder.reports()[0]
    assert (quiet.peer_id, quiet.phase, quiet.waiting, quiet.oldest) == (
        PEER,
        Phase.IDLE,
        0,
        None,
    )
    assert quiet.last_contact is None
    first = out.send("one")
    out.send("two")
    out.peer.down = True
    out.step()
    down = out.one.report()
    assert (down.phase, down.waiting, down.oldest) == (Phase.BACKING_OFF, 2, first)
    assert down.oldest_sent_at == out.wall.now and down.last_contact is None
    assert down.backoff_seconds == FIRST_BACKOFF_SECONDS
    out.peer.down = False
    out.clock.now += FIRST_BACKOFF_SECONDS
    out.wall.now += timedelta(minutes=1)
    out.peer.answers = [WELCOME, refused("refused")]
    out.step()
    after = out.one.report()
    assert after.last_contact == out.wall.now and after.waiting == 1


def outage_notes(out: Out) -> list[str]:
    """The notes the harness sent the default operator."""
    return [
        message.body
        for message in out.log.state.messages.values()
        if message.sender == HARNESS
        and message.recipient == out.scheduler.default_operator
    ]


def test_an_outage_is_told_once_after_an_hour_until_mail_is_accepted(
    out: Out,
) -> None:
    first = out.send("one")
    out.peer.down = True
    out.step()
    # Not yet an hour.
    out.wall.now += timedelta(seconds=OUTAGE_SECONDS - 1)
    out.clock.now += FIRST_BACKOFF_SECONDS
    out.step()
    assert outage_notes(out) == []
    out.wall.now += timedelta(seconds=1)
    out.clock.now += 2 * FIRST_BACKOFF_SECONDS
    out.step()
    (told,) = outage_notes(out)
    assert told.startswith(f"Dromon has not delivered mail to peer {PEER} for an hour.")
    assert f"1 message waits, the oldest {first}" in told
    # Later attempts of the same outage tell nothing more.
    out.clock.now += MAX_BACKOFF_SECONDS
    out.step()
    assert len(outage_notes(out)) == 1
    # Accepted mail ends the outage, so the next one is told again.
    out.peer.down = False
    out.clock.now += MAX_BACKOFF_SECONDS
    out.peer.answers = [WELCOME, accepted()]
    assert out.step() == 0.0
    assert not out.one.outage_told
    asyncio.run(out.one.drop_link())
    out.send("two")
    out.peer.down = True
    out.step()
    out.wall.now += timedelta(seconds=OUTAGE_SECONDS)
    out.clock.now += MAX_BACKOFF_SECONDS
    out.step()
    assert len(outage_notes(out)) == 2


def test_a_refusal_does_not_end_an_outage(out: Out) -> None:
    """The design ends an outage only when the peer accepts mail, so a peer that
    answers but refuses every message is still told of once."""
    out.send("one")
    out.send("two")
    out.wall.now += timedelta(seconds=OUTAGE_SECONDS)
    out.peer.answers = [WELCOME, refused("refused")]
    out.step()
    assert len(outage_notes(out)) == 1
    assert out.one.outage_told


def test_an_outage_note_that_fails_never_holds_up_delivery(
    out: Out, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(report: object) -> None:
        raise Faulty("no log")

    out.scheduler.outage = broken  # pyright: ignore[reportAttributeAccessIssue]
    out.send()
    out.wall.now += timedelta(seconds=OUTAGE_SECONDS)
    out.peer.answers = [WELCOME, accepted()]
    assert out.step() == 0.0
    assert out.log.state.outbox.get(PEER, {}) == {}
    assert "the outage note for peer keleusma failed" in capsys.readouterr().err


def test_the_outage_note_says_what_waits_and_when_the_peer_last_answered(
    out: Out,
) -> None:
    out.send("one")
    out.send("two")
    report = out.one.report()
    text = outage_text(report)
    assert "2 messages wait" in text
    assert "It has not answered since the daemon started." in text
    contact = out.wall.now
    out.one.last_contact = contact
    assert f"It last answered at {model.format_due(contact)}." in outage_text(
        out.one.report()
    )
    # The fault is left to dromon status, since its text may name a path.
    out.one.fault = "Faulty: at /some/path"
    assert "/some/path" not in outage_text(out.one.report())


def test_a_lines_view_of_a_peer_leaves_out_the_fault(out: Out) -> None:
    out.one.fault, out.one.faults = "Faulty: at /some/path", 1
    report = out.one.report()
    operator_view = report.wire(detail=True)
    line_view = report.wire(detail=False)
    assert operator_view["fault"] == "Faulty: at /some/path"
    assert "fault" not in line_view and "backoff_seconds" not in line_view
    assert {k: v for k, v in operator_view.items() if k in line_view} == line_view


def test_line_status_shows_only_the_peers_a_line_may_send_to(out: Out) -> None:
    out.scheduler.peer_reports = out.forwarder.reports
    out.send("one")
    alpha = out.scheduler.line_status("alpha", detail=False, lists=False)
    assert alpha["peers"] == [out.one.report().wire(detail=False)]
    assert out.scheduler.line_status("beta")["peers"] == []


def test_dromon_status_prints_a_row_for_each_peer() -> None:
    from dromon import cli

    waiting = {
        "peer": PEER,
        "state": "backing_off",
        "waiting": 2,
        "oldest": "m-1",
        "oldest_sent_at": AT,
        "last_contact": None,
        "fault": "Faulty: x",
    }
    assert cli._peer_row(waiting) == (  # pyright: ignore[reportPrivateUsage]
        f"peer {PEER}, backing_off, 2 waiting, the oldest m-1 since {AT},"
        " no contact since the daemon started, fault Faulty: x"
    )
    quiet = {"peer": PEER, "state": "idle", "waiting": 0, "last_contact": AT}
    assert cli._peer_row(quiet) == (  # pyright: ignore[reportPrivateUsage]
        f"peer {PEER}, idle, nothing waiting, last contact {AT}"
    )


def test_mail_for_a_removed_peer_is_orphaned(out: Out) -> None:
    message_id = out.send()
    assert orphaned(out.log.state, out.config) == []
    out.log.append(PeerRemoved(PEER))
    assert orphaned(out.log.state, out.config) == [message_id]
    assert orphaned(out.log.state, FederationConfig()) == [message_id]


def test_send_times_are_read_from_the_log(out: Out, tmp_path: Path) -> None:
    message_id = out.send()
    entries = read_entries(tmp_path / "events.db")
    times = sent_times(entries, {message_id, "m-none"})
    assert set(times) == {message_id}
    bad = Entry(1, "not a time", to_peer("m-bad"))
    assert sent_times([bad], {"m-bad"}) == {}


def test_the_envelope_needs_mail_to_a_peer(out: Out) -> None:
    message_id = out.scheduler.send(ALPHA, ANA, "local")
    with pytest.raises(EventRejected, match="not to a peer"):
        envelope(out.log.state, message_id, out.log.state.messages[message_id])


@pytest.mark.parametrize(
    ("directory", "covered", "name"),
    [
        ("pydromon", False, "pydromon"),
        ("PyDromon", False, "pydromon"),
        ("pydromon", True, DEFAULT_NAME),
        ("two words", False, DEFAULT_NAME),
    ],
)
def test_the_announced_name(directory: str, covered: bool, name: str) -> None:
    assert announced_name(Path("/srv") / directory, lambda text: covered) == name


# The peer socket's bound before hello, decided by the operator on 2026-10-28.


class Written:
    """A stream writer that keeps what is written."""

    def __init__(self) -> None:
        self.data = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def answers(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.data.decode().splitlines()]


def request(op: str, args: dict[str, object] | None = None) -> bytes:
    document: dict[str, object] = {"id": "r", "op": op, "v": 1}
    if args is not None:
        document["args"] = args
    return (json.dumps(document) + "\n").encode()


HELLO = request("hello", {"versions": [1], "name": "keleusma", "token": TOKEN})


def deliver(body: str = "hi", origin_id: str = "o1") -> bytes:
    return request(
        "deliver",
        {
            "origin_id": origin_id,
            "sender": "kaizen",
            "recipient": "alpha",
            "kind": "note",
            "body": body,
            "reply_to": None,
            "priority": False,
        },
    )


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


@pytest.fixture
def listening(paths: ProjectPaths) -> Generator[Daemon]:
    config = parse_federation(
        {"listen": True},
        {
            PEER: {
                "socket": "/srv/keleusma/run/peers.sock",
                "inbound": True,
                "reach": ["alpha"],
                "max_body_bytes": 2 * MAX_HELLO_LINE_BYTES,
            }
        },
    )
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("alpha", "claude-haiku-5-5", Strategy.SUBSCRIPTION)
        for event in plan_peers(log.state, config):
            log.append(event)
        log.append(PeerKeyed(PEER, token_digest(TOKEN)))
        yield Daemon(
            paths,
            log,
            AT,
            scheduler,
            project=ProjectConfig(federation=config),
            command_line=Overrides(),
        )


def serve_lines(
    daemon: Daemon, data: bytes, limit: int = MAX_HELLO_LINE_BYTES
) -> Written:
    async def scenario() -> Written:
        reader = asyncio.StreamReader(limit=limit)
        reader.feed_data(data)
        reader.feed_eof()
        written = Written()
        await peer_connection(
            daemon, reader, cast(asyncio.StreamWriter, written), PeerSession(daemon)
        )
        assert written.closed
        return written

    return asyncio.run(scenario())


def test_an_identified_peer_may_send_lines_larger_than_the_hello_line(
    listening: Daemon,
) -> None:
    body = "x" * (MAX_HELLO_LINE_BYTES + 10)
    written = serve_lines(listening, HELLO + deliver(body) + request("ping"))
    answers = written.answers()
    assert [a["ok"] for a in answers] == [True, True, True]
    assert answers[1]["result"]["outcome"] == "accepted"


def test_anything_but_an_accepted_hello_first_closes_the_connection(
    listening: Daemon,
) -> None:
    for first in (request("ping"), deliver(), request("hello", {"versions": [9]})):
        written = serve_lines(listening, first + HELLO + deliver())
        assert len(written.answers()) == 1
    assert listening.log.state.received == {}


def test_a_hello_line_past_its_bound_is_refused_and_closed(listening: Daemon) -> None:
    oversized = request(
        "hello",
        {"versions": [1], "name": "k", "token": "T" * MAX_HELLO_LINE_BYTES},
    )
    written = serve_lines(listening, oversized + HELLO)
    answers = written.answers()
    assert len(answers) == 1 and answers[0]["error"]["code"] == "too_long"


def test_a_silent_connection_is_closed_at_the_hello_timeout(
    listening: Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon_module, "HELLO_TIMEOUT_SECONDS", 0.01)

    async def scenario() -> Written:
        reader = asyncio.StreamReader(limit=MAX_HELLO_LINE_BYTES)
        written = Written()
        await peer_connection(
            listening,
            reader,
            cast(asyncio.StreamWriter, written),
            PeerSession(listening),
        )
        return written

    written = asyncio.run(scenario())
    assert written.closed and written.data == b""


def test_a_revoked_token_closes_the_connection(listening: Daemon) -> None:
    async def scenario() -> Written:
        reader = asyncio.StreamReader(limit=MAX_HELLO_LINE_BYTES)
        reader.feed_data(HELLO)
        written = Written()
        serving = asyncio.create_task(
            peer_connection(
                listening,
                reader,
                cast(asyncio.StreamWriter, written),
                PeerSession(listening),
            )
        )
        for _ in range(20):
            await asyncio.sleep(0)
        listening.log.append(PeerKeyed(PEER, ""))
        reader.feed_data(deliver() + deliver(origin_id="o2"))
        await asyncio.wait_for(serving, 1)
        return written

    answers = asyncio.run(scenario()).answers()
    assert (
        len(answers) == 2 and answers[1]["error"]["message"] == "the peer was refused"
    )


def test_the_line_reader_splits_lines_and_bounds_them() -> None:
    async def scenario() -> list[bytes]:
        reader = asyncio.StreamReader(limit=8)
        reader.feed_data(b"one\ntwo\nthree")
        reader.feed_eof()
        lines = LineReader(reader, 16)
        return [await lines.readline() for _ in range(4)]

    assert asyncio.run(scenario()) == [b"one\n", b"two\n", b"three", b""]

    async def too_long() -> None:
        reader = asyncio.StreamReader(limit=8)
        reader.feed_data(b"x" * 40 + b"\n")
        await LineReader(reader, 16).readline()

    with pytest.raises(ValueError, match="16 bytes"):
        asyncio.run(too_long())


def test_the_connection_cap_turns_away_connections_past_its_limit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def scenario() -> tuple[Written, Written, int, int]:
        cap = ConnectionCap(1, "peer connections")
        release = asyncio.Event()
        served: list[str] = []

        def serving(name: str) -> Callable[[], Awaitable[None]]:
            async def serve() -> None:
                served.append(name)
                await release.wait()

            return serve

        first, second, third = Written(), Written(), Written()
        holding = asyncio.create_task(
            cap.admit(cast(asyncio.StreamWriter, first), serving("first"))
        )
        await asyncio.sleep(0)
        await cap.admit(cast(asyncio.StreamWriter, second), serving("second"))
        during = cap.active
        release.set()
        await holding
        await cap.admit(cast(asyncio.StreamWriter, third), serving("third"))
        assert served == ["first", "third"]
        return first, second, during, cap.active

    first, second, during, after = asyncio.run(scenario())
    assert during == 1 and after == 0
    assert first.data == b"" and not first.closed
    answers = second.answers()
    assert second.closed and len(answers) == 1
    assert answers[0]["error"]["code"] == "rejected"
    assert "at most 1 peer connections" in answers[0]["error"]["message"]
    assert "refused a connection" in capsys.readouterr().err


def test_the_connection_cap_releases_a_slot_when_serving_fails() -> None:
    async def scenario() -> int:
        cap = ConnectionCap(1, "peer connections")

        async def failing() -> None:
            raise ConnectionError("gone")

        with pytest.raises(ConnectionError):
            await cap.admit(cast(asyncio.StreamWriter, Written()), failing)
        return cap.active

    assert asyncio.run(scenario()) == 0


def test_a_connection_cap_must_admit_one() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        ConnectionCap(0, "peer connections")


def test_the_peer_line_limit_is_larger_than_the_hello_line() -> None:
    assert MAX_HELLO_LINE_BYTES < MAX_PEER_LINE_BYTES


@pytest.mark.socket
def test_a_note_crosses_between_two_daemons_over_the_peer_socket(
    listening: Daemon, tmp_path: Path
) -> None:
    peer_path = listening.paths.peer_socket
    peer_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = federation(socket=str(peer_path), remote=["alpha"])
    sender = Out(tmp_path / "sender.db", config)
    forwarder = Forwarder(
        sender.scheduler,
        config,
        connect_unix,
        lambda peer_id: TOKEN,
        "core",
        time.monotonic,
        lambda: datetime.now(UTC),
        {},
    )

    async def scenario() -> str:
        server = await asyncio.start_unix_server(
            lambda reader, writer: peer_connection(
                listening, reader, writer, PeerSession(listening)
            ),
            path=str(peer_path),
            limit=MAX_HELLO_LINE_BYTES,
        )
        try:
            message_id = sender.scheduler.send(
                ALPHA, RemoteAddress(PEER, ALPHA), "across", None
            )
            one = forwarder.peers[PEER]
            assert await one.step() == 0.0
            await one.drop_link()
            return message_id
        finally:
            server.close()
            await server.wait_closed()

    try:
        message_id = asyncio.run(scenario())
        assert sender.log.state.messages[message_id].delivered
        arrived = [
            m
            for m in listening.log.state.messages.values()
            if m.origin_id == message_id
        ]
        assert len(arrived) == 1
        assert arrived[0].sender == RemoteAddress(PEER, ALPHA)
        assert arrived[0].body == "across"
    finally:
        sender.log.close()


def test_the_daemon_returns_orphaned_mail_and_dates_waiting_mail(
    paths: ProjectPaths,
) -> None:
    config = federation()
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("alpha", "claude-haiku-5-5", Strategy.SUBSCRIPTION)
        for event in plan_peers(log.state, config):
            log.append(event)
        scheduler.federation = config
        kept = scheduler.send(ALPHA, KAIZEN, "kept")
        log.append(PeerDeclared("gone", "unix", "{}"))
        log.append(to_peer("m-lost", recipient=RemoteAddress("gone", ALPHA)))
        log.append(PeerRemoved("gone"))
        daemon = Daemon(
            paths,
            log,
            AT,
            scheduler,
            project=ProjectConfig(federation=config),
            command_line=Overrides(),
        )
        forwarder = daemon.forwarder()
        assert log.state.messages["m-lost"].returned is ReturnCode.UNKNOWN_PEER
        one = forwarder.peers[PEER]
        assert kept in one.sent_at and kept in one.possibly_sent
        assert scheduler.on_outbox == forwarder.notify


def test_an_operator_sends_to_a_peer_through_the_daemon(
    paths: ProjectPaths,
) -> None:
    config = federation()
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        for event in plan_peers(log.state, config):
            log.append(event)
        scheduler.federation = config
        daemon = Daemon(
            paths,
            log,
            AT,
            scheduler,
            project=ProjectConfig(federation=config),
            command_line=Overrides(),
        )
        args: dict[str, object] = {
            "recipient": "keleusma:@ana",
            "body": "hi",
            "operator": "ana",
        }
        sent_ok = daemon.respond(request("send", args))
        assert sent_ok.ok and sent_ok.result is not None
        assert list(log.state.outbox[PEER]) == [sent_ok.result["message_id"]]
        urgent = daemon.respond(request("send", {**args, "priority": True}))
        assert not urgent.ok and "priority" in str(urgent.error_message)
        daemon.forwarder()
        status = daemon.respond(request("status"))
        assert status.result is not None
        peers = cast(list[dict[str, object]], status.result["peers"])
        assert [(p["peer"], p["waiting"], p["faults"]) for p in peers] == [(PEER, 1, 0)]
        assert status.result["scheduler"] == {"fault": None, "faults": 0}
        # The client protocol refuses a float, so the whole status must encode.
        encode_response(status)
        listed = daemon.respond(request("sent", {"operator": "ana"}))
        assert listed.result is not None
        messages = cast(list[dict[str, object]], listed.result["messages"])
        assert messages[0]["delivery"] == "waiting for peer"
