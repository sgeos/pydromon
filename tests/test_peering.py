"""Mail from peers, step 4 of federation phase 1. The remote address, the
``message_received`` event, ``hello`` and ``deliver`` on ``peers.sock``, the receiver's
checks, and the turn allowance of decision 10. The design is
``docs/architecture/FEDERATION.md``."""

import asyncio
import json
import os
import socket
import stat
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import client
from dromon.agents import token_digest
from dromon.codec import DecodeError, decode, encode
from dromon.configview import Overrides
from dromon.daemon import Daemon, PeerSession, tls_peer_connection
from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog
from dromon.federation import (
    FederationConfig,
    ensure_identity,
    parse_federation,
    plan_peers,
)
from dromon.limits import Limits
from dromon.model import (
    AgentAddress,
    EventRejected,
    InvalidField,
    LineAddress,
    LineCreated,
    Message,
    MessageDelivered,
    MessageKind,
    MessagePrioritized,
    MessageReceived,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    PeerDeclared,
    PeerKeyed,
    PeerRemoved,
    RemoteAddress,
    State,
    parse_address,
    parse_any_address,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.peering import (
    RATE_PER_PEER,
    RATE_WINDOW_SECONDS,
    TURN_WINDOW_SECONDS,
    Accepted,
    Busy,
    Code,
    PeerPriority,
    PeerTurns,
    Received,
    Receiver,
    Refused,
    TurnCause,
    choose_version,
    identify,
    outcome_result,
    turn_cause,
)
from dromon.projectconfig import ProjectConfig, declared_operators
from dromon.relay import parse_relays
from dromon.protocol import MAX_LINE_BYTES, Operation, Request, Response
from dromon.scheduler import PRIORITY_DECLINED, LineEngine, LineSpec, Scheduler
from dromon.tls_link import server_context_for, tls_connector

PEER = "keleusma"
KAIZEN = RemoteAddress(PEER, LineAddress("kaizen"))
ALPHA = LineAddress("alpha")
ANA = OperatorAddress("ana")
DECLARED: frozenset[str] = frozenset({"ana"})
"""The operators the tests' project file declares, decision 25 of ``FEDERATION.md``."""
TOKEN = "T" * 43


# Addresses.


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("keleusma:kaizen", RemoteAddress(PEER, LineAddress("kaizen"))),
        ("keleusma:@ana", RemoteAddress(PEER, OperatorAddress("ana"))),
        ("keleusma:&claude", RemoteAddress(PEER, AgentAddress("claude"))),
        ("kaizen", LineAddress("kaizen")),
        ("@ana", OperatorAddress("ana")),
    ],
)
def test_an_address_reads_back_as_written(text: str, expected: object) -> None:
    address = parse_any_address(text)
    assert address == expected
    assert str(address) == text


@pytest.mark.parametrize(
    "text", ["a:b:c", "keleusma:", ":kaizen", "@ana:kaizen", "&x:y", "k:@", "k:a\nb"]
)
def test_a_malformed_remote_address_is_refused(text: str) -> None:
    with pytest.raises(InvalidField):
        parse_any_address(text)


def test_the_local_parser_refuses_a_remote_address() -> None:
    with pytest.raises(InvalidField):
        parse_address("keleusma:kaizen")


def test_a_remote_address_never_reads_as_a_local_operator() -> None:
    remote = str(RemoteAddress(PEER, ANA))
    assert not remote.startswith("@") and not remote.startswith("&")


# The model.


def peer_state(*, removed: bool = False) -> State:
    state = State()
    state.apply(LineCreated("alpha", "claude-haiku-5-5", "subscription"))
    state.apply(OperatorRegistered("ana"))
    state.apply(PeerDeclared(PEER, "unix", "{}"))
    if removed:
        state.apply(PeerRemoved(PEER))
    return state


RECEIVED_AT = "2026-10-28T00:00:00Z"


def received(
    message_id: str = "r1",
    origin_id: str = "o1",
    body: str = "hello",
    recipient: LineAddress | OperatorAddress | AgentAddress = ALPHA,
    reply_to: str | None = None,
    at: str = RECEIVED_AT,
) -> MessageReceived:
    return MessageReceived(message_id, KAIZEN, recipient, body, reply_to, origin_id, at)


def test_received_mail_is_recorded_with_its_key_and_counted_while_it_waits() -> None:
    state = peer_state()
    state.apply(received())
    state.apply(received("r2", "o2", recipient=ANA))
    message = state.messages["r1"]
    assert message == Message(
        KAIZEN, ALPHA, MessageKind.NOTE, "hello", (), None, origin_id="o1"
    )
    assert state.received == {(PEER, "o1"): "r1", (PEER, "o2"): "r2"}
    assert state.waiting_from == {PEER: 2}
    assert state.waiting_for == {ALPHA: 1, ANA: 1}
    state.apply(MessageDelivered("r1"))
    assert state.waiting_from == {PEER: 1}
    assert state.waiting_for == {ANA: 1}
    state.apply(MessageDelivered("r2"))
    assert state.waiting_from == {} and state.waiting_for == {}


def test_a_snapshot_copies_the_received_keys_and_counts() -> None:
    state = peer_state()
    snapshot = state.copy()
    state.apply(received())
    assert snapshot.received == {} and snapshot.waiting_from == {}
    assert snapshot.waiting_for == {}


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (received(origin_id="o1", message_id="r9"), "delivered o1 before, as r1"),
        (received("r1", "o9"), "message r1 exists"),
        (received("r9", "o9", recipient=LineAddress("nobody")), "no line nobody"),
        (received("r9", "o9", recipient=OperatorAddress("bo")), "no operator bo"),
        (received("r9", "o9", reply_to="nothing"), "no message nothing"),
        (received("r9", "o9", reply_to="r1"), "a reply to r1 must go from"),
    ],
)
def test_received_mail_that_does_not_apply_is_rejected(
    event: MessageReceived, reason: str
) -> None:
    state = peer_state()
    state.apply(received())
    with pytest.raises(EventRejected, match=reason):
        state.apply(event)


def test_mail_from_an_undeclared_or_removed_peer_is_rejected() -> None:
    with pytest.raises(EventRejected, match=f"no peer {PEER}"):
        peer_state(removed=True).apply(received())
    state = State()
    state.apply(LineCreated("alpha", "claude-haiku-5-5", "subscription"))
    with pytest.raises(EventRejected, match=f"no peer {PEER}"):
        state.apply(received())


def test_message_sent_never_comes_from_a_remote_party() -> None:
    state = peer_state()
    with pytest.raises(EventRejected, match="message_received"):
        state.apply(MessageSent("m1", KAIZEN, ALPHA, MessageKind.NOTE, "x"))


MALFORMED_RECEIVED: list[Callable[[], object]] = [
    lambda: MessageReceived(
        "r1", cast(RemoteAddress, ALPHA), ALPHA, "x", None, "o", RECEIVED_AT
    ),
    lambda: MessageReceived(
        "r1", KAIZEN, cast(LineAddress, KAIZEN), "x", None, "o", RECEIVED_AT
    ),
    lambda: MessageReceived("r1", KAIZEN, ALPHA, "x", None, "Not An Id", RECEIVED_AT),
    lambda: MessageReceived("r1", KAIZEN, ALPHA, "x", "Bad Id", "o", RECEIVED_AT),
    lambda: MessageReceived("r1", KAIZEN, ALPHA, "x", None, "o", "yesterday"),
    lambda: RemoteAddress(PEER, cast(LineAddress, KAIZEN)),
    lambda: RemoteAddress("Bad Peer", ALPHA),
]


@pytest.mark.parametrize("make", MALFORMED_RECEIVED)
def test_malformed_received_events_are_invalid(make: Callable[[], object]) -> None:
    with pytest.raises(InvalidField):
        make()


def test_the_codec_refuses_a_local_sender_for_received_mail() -> None:
    payload = json.loads(encode(received()))
    payload["data"]["sender"] = "kaizen"
    with pytest.raises(DecodeError, match="not a remote address"):
        decode(json.dumps(payload, sort_keys=True, separators=(",", ":")))


# The receiver.


def config(**keys: object) -> FederationConfig:
    table: dict[str, object] = {
        "socket": "/srv/keleusma/run/peers.sock",
        "inbound": True,
        "reach": ["alpha", "@ana"],
        **keys,
    }
    return parse_federation({"listen": True}, {PEER: table})


class Recorder:
    """The ``receive`` callback, applying each event to a state as the scheduler
    appends it to the log."""

    def __init__(self, state: State) -> None:
        self.state = state
        self.count = 0
        self.priorities: list[PeerPriority] = []

    def __call__(
        self,
        sender: RemoteAddress,
        recipient: LineAddress | OperatorAddress | AgentAddress,
        body: str,
        reply_to: str | None,
        origin_id: str,
        priority: PeerPriority,
    ) -> Received:
        self.count += 1
        self.priorities.append(priority)
        message_id = f"r{self.count}"
        self.state.apply(
            MessageReceived(
                message_id,
                sender,
                recipient,
                body,
                reply_to,
                origin_id,
                RECEIVED_AT,
                priority is not PeerPriority.NONE,
            )
        )
        honoured = priority is PeerPriority.PERMITTED
        if honoured:
            self.state.apply(MessagePrioritized(message_id))
        return Received(message_id, honoured)


def envelope(**keys: object) -> dict[str, object]:
    return {
        "origin_id": "o1",
        "sender": "kaizen",
        "recipient": "alpha",
        "kind": "note",
        "body": "hello",
        "reply_to": None,
        "priority": False,
        **keys,
    }


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def judge(
    state: State,
    federation: FederationConfig,
    args: dict[str, object],
    receiver: Receiver | None = None,
    operators: frozenset[str] = DECLARED,
) -> object:
    return (receiver or Receiver(Clock())).deliver(
        state, federation, operators, PEER, args, Recorder(state)
    )


def test_a_note_is_accepted_once_and_a_resend_is_a_duplicate() -> None:
    state = peer_state()
    receiver = Receiver(Clock())
    recorder = Recorder(state)
    first = receiver.deliver(state, config(), DECLARED, PEER, envelope(), recorder)
    assert first == Accepted("r1", duplicate=False)
    assert state.messages["r1"].sender == KAIZEN
    again = receiver.deliver(state, config(), DECLARED, PEER, envelope(), recorder)
    assert again == Accepted("r1", duplicate=True)
    assert recorder.count == 1


def test_a_resend_is_a_duplicate_even_after_the_policy_closes() -> None:
    state = peer_state()
    receiver = Receiver(Clock())
    recorder = Recorder(state)
    receiver.deliver(state, config(), DECLARED, PEER, envelope(), recorder)
    closed = config(inbound=False)
    assert receiver.deliver(
        state, closed, DECLARED, PEER, envelope(), recorder
    ) == Accepted("r1", duplicate=True)


@pytest.mark.parametrize(
    "change",
    [
        {"body": "another"},
        {"recipient": "@ana"},
        {"sender": "other"},
        {"kind": "question"},
    ],
)
def test_a_different_message_under_one_origin_is_a_conflict(
    change: dict[str, object], capsys: pytest.CaptureFixture[str]
) -> None:
    state = peer_state()
    receiver = Receiver(Clock())
    recorder = Recorder(state)
    receiver.deliver(state, config(), DECLARED, PEER, envelope(), recorder)
    outcome = receiver.deliver(
        state, config(), DECLARED, PEER, envelope(**change), recorder
    )
    assert outcome == Refused(Code.CONFLICT)
    assert recorder.count == 1
    assert "origin identifier o1" in capsys.readouterr().err


MALFORMED_ENVELOPES: list[dict[str, object]] = [
    envelope(sender="keleusma:kaizen"),
    envelope(sender="a:b:c"),
    envelope(sender="kai\nzen"),
    envelope(recipient="al\npha"),
    envelope(recipient="@"),
    envelope(recipient="other:alpha"),
    envelope(sender=7),
    envelope(origin_id="Not An Id"),
    envelope(origin_id=None),
    envelope(kind="shout"),
    envelope(body=7),
    envelope(body="\ud800"),
    envelope(reply_to="Bad Id"),
    envelope(priority="yes"),
    envelope(extra=True),
    {key: value for key, value in envelope().items() if key != "priority"},
]


@pytest.mark.parametrize("args", MALFORMED_ENVELOPES)
def test_a_malformed_envelope_is_refused_and_records_nothing(
    args: dict[str, object],
) -> None:
    state = peer_state()
    assert judge(state, config(), args) == Refused(Code.MALFORMED)
    assert state.received == {}


@pytest.mark.parametrize(
    ("federation", "args"),
    [
        (config(inbound=False), envelope()),
        (config(reach=["@ana"]), envelope()),
        (config(reach=["alpha", "@ana", "ghost"]), envelope(recipient="ghost")),
        (config(), envelope(recipient="ghost")),
        (config(), envelope(kind="question")),
        (parse_federation(None, None), envelope()),
    ],
)
def test_policy_refusals_share_one_code(
    federation: FederationConfig, args: dict[str, object]
) -> None:
    state = peer_state()
    assert judge(state, federation, args) == Refused(Code.REFUSED)
    assert state.received == {}


def test_a_party_out_of_reach_and_a_missing_one_are_refused_alike() -> None:
    state = peer_state()
    outside = judge(state, config(reach=["@ana"]), envelope())
    missing = judge(state, config(reach=["ghost"]), envelope(recipient="ghost"))
    assert outside == missing == Refused(Code.REFUSED)


@pytest.mark.parametrize("operator_id", ["ana", "bo"])
def test_an_operator_the_project_does_not_declare_is_refused(operator_id: str) -> None:
    """Decided by the operator on 2026-10-31, decision 25 of ``FEDERATION.md``. ``ana``
    is known from the log and ``bo`` not at all, and neither is declared, so both are
    refused as a missing line is, and nothing is recorded."""
    state = peer_state()
    reach = config(reach=["alpha", f"@{operator_id}"])
    args = envelope(recipient=f"@{operator_id}")
    undeclared = judge(state, reach, args, operators=frozenset({"cy"}))
    missing = judge(state, config(reach=["ghost"]), envelope(recipient="ghost"))
    assert undeclared == missing == Refused(Code.REFUSED)
    assert state.received == {}
    assert ("bo" in state.operators) is False


def test_the_declared_operators_are_the_files_operator_and_its_routes() -> None:
    relays = parse_relays(
        {"note": {"kind": "file", "path": "notes/operators.txt"}},
        {"cy": {"relay": "note"}},
    )
    assert declared_operators(ProjectConfig()) == frozenset()
    assert declared_operators(ProjectConfig(operator="bo")) == {"bo"}
    assert declared_operators(ProjectConfig(relays=relays)) == set(relays.routes)
    both = ProjectConfig(operator="bo", relays=relays)
    assert declared_operators(both) == {"bo", *relays.routes}


def test_a_removed_peer_is_refused() -> None:
    state = peer_state(removed=True)
    assert judge(state, config(), envelope()) == Refused(Code.REFUSED)


def test_a_body_over_the_peer_limit_is_too_large() -> None:
    state = peer_state()
    small = config(max_body_bytes=4)
    assert judge(state, small, envelope(body="12345")) == Refused(Code.TOO_LARGE)
    assert judge(state, small, envelope(body="1234")) == Accepted("r1", False)


def test_a_body_over_the_limit_is_measured_in_utf8_bytes() -> None:
    state = peer_state()
    small = config(max_body_bytes=4)
    assert judge(state, small, envelope(body="ééé")) == Refused(Code.TOO_LARGE)


def test_a_reply_to_an_unknown_message_is_refused() -> None:
    state = peer_state()
    state.apply(MessageSent("m1", ANA, ALPHA, MessageKind.NOTE, "local"))
    for reply_to in ("nothing", "m1"):
        outcome = judge(state, config(), envelope(reply_to=reply_to))
        assert outcome == Refused(Code.UNKNOWN_REPLY)


def test_waiting_mail_is_bounded_per_peer_and_per_recipient() -> None:
    state = peer_state()
    receiver = Receiver(Clock())
    recorder = Recorder(state)
    per_peer = config(waiting=2)
    for origin in ("o1", "o2"):
        outcome = receiver.deliver(
            state, per_peer, DECLARED, PEER, envelope(origin_id=origin), recorder
        )
        assert isinstance(outcome, Accepted)
    full = receiver.deliver(
        state, per_peer, DECLARED, PEER, envelope(origin_id="o3"), recorder
    )
    assert full == Refused(Code.FULL)
    state.apply(MessageDelivered("r1"))
    again = receiver.deliver(
        state, per_peer, DECLARED, PEER, envelope(origin_id="o3"), recorder
    )
    assert isinstance(again, Accepted)
    per_recipient = parse_federation(
        {"waiting_per_recipient": 2},
        {PEER: {"socket": "/s/run/peers.sock", "inbound": True, "reach": ["alpha"]}},
    )
    refused = receiver.deliver(
        state, per_recipient, DECLARED, PEER, envelope(origin_id="o4"), recorder
    )
    assert refused == Refused(Code.FULL)


def test_every_delivery_counts_toward_the_rate_limit() -> None:
    clock = Clock()
    state = peer_state()
    receiver = Receiver(clock)
    recorder = Recorder(state)
    for index in range(RATE_PER_PEER):
        args = envelope(origin_id=f"o{index}") if index % 2 else envelope(kind="x")
        assert not isinstance(
            receiver.deliver(state, config(), DECLARED, PEER, args, recorder), Busy
        )
    busy = receiver.deliver(
        state, config(), DECLARED, PEER, envelope(origin_id="late"), recorder
    )
    assert busy == Busy(int(RATE_WINDOW_SECONDS))
    clock.now += RATE_WINDOW_SECONDS
    accepted = receiver.deliver(
        state, config(), DECLARED, PEER, envelope(origin_id="late"), recorder
    )
    assert isinstance(accepted, Accepted)


def test_each_outcome_has_its_wire_form() -> None:
    assert outcome_result(Accepted("r1", True)) == {
        "outcome": "accepted",
        "message_id": "r1",
        "duplicate": True,
        "priority": False,
    }
    assert outcome_result(Refused(Code.FULL)) == {
        "outcome": "refused",
        "code": "full",
        "retry": False,
    }
    assert outcome_result(Busy(5)) == {"outcome": "busy", "retry_seconds": 5}


def test_a_peer_is_named_only_by_its_token() -> None:
    state = peer_state()
    assert identify(state, TOKEN) is None
    state.apply(PeerKeyed(PEER, token_digest(TOKEN)))
    state.apply(PeerDeclared("other", "unix", "{}"))
    state.apply(PeerKeyed("other", token_digest("O" * 43)))
    assert identify(state, TOKEN) == PEER
    assert identify(state, "O" * 43) == "other"
    for wrong in ("x" * 43, "", 7, None, "T" * 300):
        assert identify(state, wrong) is None
    state.apply(PeerKeyed(PEER, ""))
    assert identify(state, TOKEN) is None
    state.apply(PeerRemoved("other"))
    assert identify(state, "O" * 43) is None


@pytest.mark.parametrize(
    ("versions", "chosen"),
    [([1], 1), ([2, 1], 1), ([2], None), ([], None), (["1"], None), (1, None)],
)
def test_the_version_is_chosen_from_the_callers_list(
    versions: object, chosen: int | None
) -> None:
    assert choose_version(versions) == chosen


# The daemon's peer socket, driven in process.


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


@contextmanager
def attached(paths: ProjectPaths, federation: FederationConfig) -> Generator[Daemon]:
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("alpha", "claude-haiku-5-5", Strategy.SUBSCRIPTION)
        scheduler.ensure_operator("ana")
        for event in plan_peers(log.state, federation):
            log.append(event)
        log.append(PeerKeyed(PEER, token_digest(TOKEN)))
        yield Daemon(
            paths,
            log,
            "2026-10-28T00:00:00Z",
            scheduler,
            project=ProjectConfig(federation=federation),
            command_line=Overrides(),
            project_file_present=False,
        )


def line(op: str, args: dict[str, object] | None = None) -> bytes:
    document: dict[str, object] = {"id": "r", "op": op, "v": 1}
    if args is not None:
        document["args"] = args
    return (json.dumps(document) + "\n").encode()


def hello(token: str = TOKEN, **keys: object) -> bytes:
    return line("hello", {"versions": [1], "name": "keleusma", "token": token, **keys})


def peer_ask(session: PeerSession, request: bytes) -> Response:
    return asyncio.run(session.respond(request))


def test_a_peer_says_hello_and_delivers_a_note(paths: ProjectPaths) -> None:
    with attached(paths, config()) as daemon:
        session = PeerSession(daemon)
        greeted = peer_ask(session, hello())
        assert greeted.ok and greeted.result == {"version": 1}
        delivered = peer_ask(session, line("deliver", envelope()))
        assert delivered.ok and delivered.result is not None
        assert delivered.result["outcome"] == "accepted"
        message_id = cast(str, delivered.result["message_id"])
        message = daemon.log.state.messages[message_id]
        assert (message.sender, message.recipient) == (KAIZEN, ALPHA)
        assert message.origin_id == "o1"
        assert message_id in [mid for mid, _ in daemon.scheduler.pending("alpha")]
        again = peer_ask(session, line("deliver", envelope()))
        assert again.result is not None and again.result["duplicate"] is True


def test_a_declared_operator_receives_before_sending_anything(
    paths: ProjectPaths,
) -> None:
    """Decided by the operator on 2026-10-31, decision 25 of ``FEDERATION.md``. ``bo``
    is declared by the project file and has never sent, so the log does not hold
    ``bo`` until the first note, which records ``bo`` and is then accepted."""
    federation = config(reach=["alpha", "@bo"])
    bo = OperatorAddress("bo")
    with attached(paths, federation) as daemon:
        daemon.project = ProjectConfig(operator=bo.operator_id, federation=federation)
        assert bo.operator_id not in daemon.log.state.operators
        session = PeerSession(daemon)
        assert peer_ask(session, hello()).ok
        delivered = peer_ask(session, line("deliver", envelope(recipient=str(bo))))
        assert delivered.ok and delivered.result is not None
        assert delivered.result["outcome"] == "accepted", delivered.result
        message = daemon.log.state.messages[cast(str, delivered.result["message_id"])]
        assert (message.sender, message.recipient) == (KAIZEN, bo)
        assert bo.operator_id in daemon.log.state.operators


def test_an_operator_known_only_from_the_log_is_refused_over_the_socket(
    paths: ProjectPaths,
) -> None:
    """``attached`` records ``ana`` in the log, as a send through the daemon would,
    and its project file declares no operator."""
    with attached(paths, config()) as daemon:
        assert declared_operators(daemon.project) == frozenset()
        assert ANA.operator_id in daemon.log.state.operators
        session = PeerSession(daemon)
        assert peer_ask(session, hello()).ok
        refused = peer_ask(session, line("deliver", envelope(recipient=str(ANA))))
        assert refused.result is not None and refused.result["code"] == "refused"
        assert daemon.log.state.received == {}


def test_a_refused_note_to_a_new_operator_records_neither(
    paths: ProjectPaths,
) -> None:
    """The scheduler records an operator the log does not hold together with the
    note, so a note the log refuses leaves no operator behind."""
    with attached(paths, config()) as daemon:
        before = daemon.log.last_seq
        stranger = RemoteAddress("nobody", parse_address("kaizen"))
        with pytest.raises(EventRejected, match="no peer"):
            daemon.scheduler.receive(stranger, OperatorAddress("bo"), "hi", None, "o1")
        assert "bo" not in daemon.log.state.operators
        assert daemon.log.last_seq == before


def test_deliver_needs_a_hello_and_hello_is_given_once(paths: ProjectPaths) -> None:
    with attached(paths, config()) as daemon:
        session = PeerSession(daemon)
        early = peer_ask(session, line("deliver", envelope()))
        assert not early.ok and "hello first" in str(early.error_message)
        assert peer_ask(session, hello()).ok
        twice = peer_ask(session, hello())
        assert not twice.ok and "already" in str(twice.error_message)
        assert daemon.log.state.received == {}


def test_an_unknown_peer_and_a_wrong_token_are_refused_alike(
    paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    with attached(paths, config()) as daemon:
        wrong = peer_ask(PeerSession(daemon), hello("W" * 43))
        unknown = peer_ask(PeerSession(daemon), hello("W" * 43, name="nobody"))
        assert not wrong.ok and not unknown.ok
        assert wrong.error_message == unknown.error_message == "the peer was refused"
        assert "refused a peer's hello" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("request_line", "code"),
    [
        (hello(versions=[2]), "unsupported_version"),
        (hello(name="Not An Id"), "bad_request"),
        (line("hello", {"versions": [1], "token": TOKEN}), "bad_request"),
        (hello(extra=1), "bad_request"),
        (line("status"), "unknown_operation"),
        (line("send", {"to": "alpha", "body": "x"}), "unknown_operation"),
        (b"not json\n", "bad_request"),
    ],
)
def test_the_peer_socket_refuses_anything_else(
    paths: ProjectPaths, request_line: bytes, code: str
) -> None:
    with attached(paths, config()) as daemon:
        response = peer_ask(PeerSession(daemon), request_line)
        assert not response.ok and response.error_code == code


def test_a_token_revoked_while_connected_delivers_nothing_more(
    paths: ProjectPaths,
) -> None:
    with attached(paths, config()) as daemon:
        session = PeerSession(daemon)
        assert peer_ask(session, hello()).ok
        daemon.log.append(PeerKeyed(PEER, ""))
        refused = peer_ask(session, line("deliver", envelope()))
        assert not refused.ok and refused.error_message == "the peer was refused"
        assert session.peer_id is None
        assert daemon.log.state.received == {}


def test_a_new_token_issued_while_connected_ends_the_session(
    paths: ProjectPaths,
) -> None:
    with attached(paths, config()) as daemon:
        session = PeerSession(daemon)
        assert peer_ask(session, hello()).ok
        daemon.log.append(PeerKeyed(PEER, token_digest("N" * 43)))
        refused = peer_ask(session, line("deliver", envelope()))
        assert not refused.ok and daemon.log.state.received == {}


PIN = "0123456789abcdef" * 4


def tls_config(**keys: object) -> FederationConfig:
    table: dict[str, object] = {
        "transport": "tls",
        "address": "studio.example.net:7411",
        "pin_sha256": PIN,
        "certificate": "peers/keleusma.pem",
        "inbound": True,
        "reach": ["alpha", "@ana"],
        **keys,
    }
    listen = {"tls_listen": "127.0.0.1:7411"} if table["inbound"] else {}
    return parse_federation(listen, {PEER: table})


def tls_hello(**keys: object) -> bytes:
    return line("hello", {"versions": [1], "name": "keleusma", **keys})


def test_over_tls_the_pin_names_the_peer_and_hello_takes_no_token(
    paths: ProjectPaths,
) -> None:
    with attached(paths, tls_config()) as daemon:
        session = PeerSession(daemon, pin_sha256=PIN)
        greeted = peer_ask(session, tls_hello())
        assert greeted.ok and session.peer_id == PEER
        delivered = peer_ask(session, line("deliver", envelope()))
        assert delivered.result is not None
        assert delivered.result["outcome"] == "accepted"
        with_token = peer_ask(PeerSession(daemon, pin_sha256=PIN), hello())
        assert not with_token.ok and with_token.error_code == "bad_request"


def test_a_key_no_inbound_peer_pins_is_refused(paths: ProjectPaths) -> None:
    with attached(paths, tls_config()) as daemon:
        refused = peer_ask(PeerSession(daemon, pin_sha256="f" * 64), tls_hello())
        assert not refused.ok and refused.error_message == "the peer was refused"
        daemon.project = ProjectConfig(federation=tls_config(inbound=False))
        closed = peer_ask(PeerSession(daemon, pin_sha256=PIN), tls_hello())
        assert not closed.ok and closed.error_message == "the peer was refused"


def test_a_token_never_names_a_tls_peer_on_the_socket(paths: ProjectPaths) -> None:
    with attached(paths, tls_config()) as daemon:
        refused = peer_ask(PeerSession(daemon), hello())
        assert not refused.ok and refused.error_message == "the peer was refused"


def test_a_pin_changed_while_connected_delivers_nothing_more(
    paths: ProjectPaths,
) -> None:
    with attached(paths, tls_config()) as daemon:
        session = PeerSession(daemon, pin_sha256=PIN)
        assert peer_ask(session, tls_hello()).ok
        daemon.project = ProjectConfig(federation=tls_config(pin_sha256="f" * 64))
        refused = peer_ask(session, line("deliver", envelope()))
        assert not refused.ok and refused.error_message == "the peer was refused"
        assert session.peer_id is None
        assert daemon.log.state.received == {}


@pytest.mark.socket
def test_a_note_crosses_the_tls_listener(paths: ProjectPaths, tmp_path: Path) -> None:
    """The connector of step 3 against the listener of step 4, over loopback."""
    server_identity, _ = ensure_identity(paths.state_dir / "peers")
    sender_dir = tmp_path / "sender"
    sender_identity, _ = ensure_identity(sender_dir / "peers")
    for source, target in (
        (sender_identity.certificate, paths.state_dir / "peers" / "keleusma.pem"),
        (server_identity.certificate, sender_dir / "listener.pem"),
    ):
        target.write_bytes(source.read_bytes())
        target.chmod(0o600)
    config = tls_config(pin_sha256=sender_identity.pin_sha256)
    with attached(paths, config) as daemon:
        context = server_context_for(paths.state_dir, paths.state_dir / "peers", config)

        async def scenario() -> tuple[Response, Response]:
            server = await asyncio.start_server(
                lambda r, w: tls_peer_connection(daemon, r, w, context),
                "127.0.0.1",
                0,
            )
            port = server.sockets[0].getsockname()[1]
            definition = parse_federation(
                None,
                {
                    PEER: {
                        "transport": "tls",
                        "address": f"127.0.0.1:{port}",
                        "pin_sha256": server_identity.pin_sha256,
                        "certificate": "listener.pem",
                        "outbound": True,
                    }
                },
            ).peers[PEER]
            try:
                link = await tls_connector(sender_dir, sender_dir / "peers")(
                    PEER, definition
                )
                try:
                    greeted = await link.exchange(
                        Request("h", Operation.HELLO, {"versions": [1], "name": "k"})
                    )
                    delivered = await link.exchange(
                        Request("d", Operation.DELIVER, envelope())
                    )
                finally:
                    await link.close()
            finally:
                server.close()
                await server.wait_closed()
            return greeted, delivered

        greeted, delivered = asyncio.run(scenario())
        assert greeted.ok
        assert delivered.result is not None
        assert delivered.result["outcome"] == "accepted"


def test_a_peer_line_may_exceed_the_client_line_limit(paths: ProjectPaths) -> None:
    body = "x" * MAX_LINE_BYTES
    with attached(paths, config(max_body_bytes=2 * MAX_LINE_BYTES)) as daemon:
        session = PeerSession(daemon)
        assert peer_ask(session, hello()).ok
        response = peer_ask(session, line("deliver", envelope(body=body)))
        assert response.result is not None
        assert response.result["outcome"] == "accepted"


@pytest.mark.parametrize("op", ["hello", "deliver"])
def test_peer_operations_are_not_served_on_the_other_sockets(
    paths: ProjectPaths, op: str
) -> None:
    with attached(paths, config()) as daemon:
        request_line = hello() if op == "hello" else line("deliver", envelope())
        operator = daemon.respond(request_line)
        agent = asyncio.run(daemon.respond_agent(request_line))
        lines = asyncio.run(daemon.respond_line(request_line))
        for response in (operator, agent, lines):
            assert not response.ok and response.error_code == "unknown_operation"
        assert daemon.log.state.received == {}


# Decision 10, the turn allowance.


def note(sender: object) -> Message:
    return Message(cast(Any, sender), ALPHA, MessageKind.NOTE, "x", (), None)


def test_a_turn_counts_by_what_woke_it() -> None:
    line_note = note(LineAddress("beta"))
    agent_note = note(AgentAddress("probe"))
    operator_note = note(ANA)
    peer_note = note(KAIZEN)
    assert turn_cause([peer_note]) is TurnCause.PEER
    assert turn_cause([peer_note, peer_note]) is TurnCause.PEER
    assert turn_cause([peer_note, line_note]) is TurnCause.AUTONOMOUS
    assert turn_cause([peer_note, agent_note]) is TurnCause.AUTONOMOUS
    assert turn_cause([peer_note, operator_note]) is TurnCause.OPERATOR
    assert turn_cause([line_note]) is TurnCause.AUTONOMOUS
    assert turn_cause([]) is TurnCause.AUTONOMOUS


def test_a_peer_starts_at_most_its_turns_per_hour_for_one_line() -> None:
    clock = Clock()
    turns = PeerTurns(clock)
    turns.set_allowances({PEER: 2})
    peer_mail = [note(KAIZEN)]
    for _ in range(2):
        assert turns.may_wake("alpha", peer_mail)
        turns.record("alpha", turns.starters("alpha", peer_mail))
    assert not turns.may_wake("alpha", peer_mail)
    assert turns.may_wake("beta", peer_mail)
    assert turns.may_wake("alpha", [*peer_mail, note(LineAddress("beta"))])
    clock.now += TURN_WINDOW_SECONDS
    assert turns.may_wake("alpha", peer_mail)


def test_an_undeclared_peer_starts_no_turn() -> None:
    turns = PeerTurns(Clock())
    assert not turns.may_wake("alpha", [note(KAIZEN)])
    with pytest.raises(ValueError, match="turns_per_hour"):
        turns.set_allowances({PEER: 61})


class Quiet:
    """A line engine that records its prompts and does nothing."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        return TurnReport("success", 1, "ok", (), None, None, "session-1", None, None)


def used(target: "World") -> int:
    status = target.scheduler.line_status("alpha")
    turns = cast(dict[str, object], status["autonomous_turns"])
    return cast(int, turns["used"])


class World:
    def __init__(self, path: Path, clock: Clock, limits: Limits) -> None:
        self.log = EventLog(path)
        self.engine = Quiet()
        self.scheduler = Scheduler(
            self.log,
            lambda spec, tools: cast(LineEngine, self.engine),
            limits=limits,
            tick_seconds=0,
            clock=clock,
        )
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", "claude-haiku-5-5", Strategy.SUBSCRIPTION)
        self.scheduler.create_line("beta", "claude-haiku-5-5", Strategy.SUBSCRIPTION)
        self.log.append(PeerDeclared(PEER, "unix", "{}"))
        self.scheduler.peer_turns.set_allowances({PEER: 1})
        self.origin = 0

    def from_peer(self) -> None:
        self.origin += 1
        self.scheduler.receive(KAIZEN, ALPHA, "from a peer", None, f"o{self.origin}")

    def settle(self, act: Callable[[], object]) -> None:
        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            await asyncio.sleep(0)
            act()
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(go())


@pytest.fixture
def world(tmp_path: Path) -> Generator[tuple[World, Clock]]:
    clock = Clock()
    target = World(
        tmp_path / "events.sqlite",
        clock,
        Limits(max_autonomous_turns=2, cooldown_seconds=0),
    )
    yield target, clock
    target.log.close()


def test_peer_mail_neither_counts_nor_resets_the_autonomous_count(
    world: tuple[World, Clock],
) -> None:
    target, clock = world
    target.settle(
        lambda: target.scheduler.send(LineAddress("beta"), ALPHA, "from a line")
    )
    assert used(target) == 1
    target.settle(target.from_peer)
    assert len(target.engine.prompts) == 2
    assert "Only a peer's mail started this turn" in target.engine.prompts[-1]
    assert "stays at 1" in target.engine.prompts[-1]
    assert used(target) == 1
    clock.now += TURN_WINDOW_SECONDS
    target.settle(target.from_peer)
    assert used(target) == 1
    assert target.log.state.lines["alpha"].held_by is None


def test_peer_mail_past_its_allowance_waits_for_another_turn(
    world: tuple[World, Clock],
) -> None:
    target, clock = world
    target.settle(target.from_peer)
    assert len(target.engine.prompts) == 1
    target.settle(target.from_peer)
    assert len(target.engine.prompts) == 1
    waiting = [m for m in target.log.state.messages.values() if not m.delivered]
    assert [m.sender for m in waiting] == [KAIZEN]
    target.settle(lambda: target.scheduler.send(ANA, ALPHA, "from an operator"))
    assert len(target.engine.prompts) == 2
    assert f"from {KAIZEN}" in target.engine.prompts[-1]
    assert used(target) == 0
    target.settle(target.from_peer)
    assert len(target.engine.prompts) == 2
    clock.now += TURN_WINDOW_SECONDS
    target.settle(lambda: None)
    assert len(target.engine.prompts) == 3


# The socket, which a sandbox denies.


@pytest.mark.socket
def test_a_peer_delivers_through_peers_sock(
    tmp_path: Path, short_runtime: Path
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700)
    (paths.state_dir / "dromon.toml").write_text(
        "version = 1\n[federation]\nlisten = true\n"
        f'[peers.{PEER}]\nsocket = "/srv/keleusma/run/peers.sock"\n'
        'inbound = true\nreach = ["&probe"]\n'
    )
    pid = client.start(paths, ["--hold-wakes"])
    try:
        assert stat.S_IMODE(paths.peer_socket.stat().st_mode) == 0o600
        client.call(
            paths.socket,
            Operation.AGENT_ADD,
            args={"agent": "probe", "access": "message"},
        )
        issued = client.call(paths.socket, Operation.PEER_TOKEN, args={"peer": PEER})
        assert issued.ok and issued.result is not None
        token = cast(str, issued.result["token"])
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.connect(str(paths.peer_socket))
            stream = raw.makefile("rwb")
            stream.write(hello(token))
            stream.write(line("deliver", envelope(recipient="&probe")))
            stream.flush()
            greeted = json.loads(stream.readline())
            delivered = json.loads(stream.readline())
        assert greeted["ok"] and greeted["result"] == {"version": 1}
        assert delivered["result"]["outcome"] == "accepted"
    finally:
        if client.is_running(paths):
            client.stop(paths)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            finished, _ = os.waitpid(pid, os.WNOHANG)
            if finished:
                break
            time.sleep(0.05)
    assert not paths.peer_socket.exists()


@pytest.mark.socket
def test_no_peer_socket_exists_unless_the_project_listens(
    tmp_path: Path, short_runtime: Path
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700)
    (paths.state_dir / "dromon.toml").write_text("version = 1\n")
    pid = client.start(paths, ["--hold-wakes"])
    try:
        assert paths.socket.exists()
        assert not paths.peer_socket.exists()
    finally:
        if client.is_running(paths):
            client.stop(paths)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            finished, _ = os.waitpid(pid, os.WNOHANG)
            if finished:
                break
            time.sleep(0.05)


# Priority mail from a peer, step 6 of phase 1.


@pytest.mark.parametrize(
    ("keys", "asked", "expected", "honoured"),
    [
        ({}, False, PeerPriority.NONE, False),
        ({}, True, PeerPriority.DECLINED, False),
        ({"priority_in": True}, True, PeerPriority.PERMITTED, True),
        ({"priority_in": True}, False, PeerPriority.NONE, False),
    ],
)
def test_the_receiver_passes_on_what_priority_the_policy_permits(
    keys: dict[str, object],
    asked: bool,
    expected: PeerPriority,
    honoured: bool,
) -> None:
    state = peer_state()
    recorder = Recorder(state)
    outcome = Receiver(Clock()).deliver(
        state, config(**keys), DECLARED, PEER, envelope(priority=asked), recorder
    )
    assert outcome == Accepted("r1", False, honoured)
    assert recorder.priorities == [expected]
    message = state.messages["r1"]
    assert (message.priority_asked, message.priority) == (asked, honoured)


def test_a_resend_is_answered_with_the_priority_first_recorded() -> None:
    state = peer_state()
    receiver = Receiver(Clock())
    recorder = Recorder(state)
    permitting = config(priority_in=True)
    receiver.deliver(
        state, permitting, DECLARED, PEER, envelope(priority=True), recorder
    )
    again = receiver.deliver(
        state, config(), DECLARED, PEER, envelope(priority=True), recorder
    )
    assert again == Accepted("r1", True, True)
    assert recorder.count == 1


def test_a_received_event_keeps_whether_priority_was_asked() -> None:
    event = received()
    asked = MessageReceived(
        "r2", KAIZEN, ALPHA, "x", None, "o2", RECEIVED_AT, priority_asked=True
    )
    for each in (event, asked):
        assert decode(encode(each)) == each


def test_a_peer_counts_as_one_sender_and_each_line_keeps_its_limit(
    paths: ProjectPaths,
) -> None:
    with attached(paths, config(priority_in=True)) as daemon:
        scheduler = daemon.scheduler
        for line_id in ("beta", "gamma"):
            scheduler.create_line(line_id, "claude-haiku-5-5", Strategy.SUBSCRIPTION)
        other = RemoteAddress(PEER, LineAddress("other"))

        def receive(sender: RemoteAddress, line_id: str, origin_id: str) -> bool:
            return scheduler.receive(
                sender,
                LineAddress(line_id),
                "x",
                None,
                origin_id,
                PeerPriority.PERMITTED,
            ).priority

        # The recipient's limit of two from lines, then the peer's of three, whatever
        # the remote sender's name.
        outcomes = [
            receive(KAIZEN, "alpha", "o1"),
            receive(other, "alpha", "o2"),
            receive(KAIZEN, "alpha", "o3"),
            receive(other, "beta", "o4"),
            receive(KAIZEN, "gamma", "o5"),
        ]
        assert outcomes == [True, True, False, True, False]
        state = daemon.log.state
        for origin_id, honoured in zip(
            ("o1", "o2", "o3", "o4", "o5"), outcomes, strict=True
        ):
            message = state.messages[state.received[(PEER, origin_id)]]
            assert message.priority_asked and message.priority == honoured


def test_priority_mail_goes_only_to_a_line(paths: ProjectPaths) -> None:
    with attached(paths, config(priority_in=True)) as daemon:
        received_note = daemon.scheduler.receive(
            KAIZEN, ANA, "x", None, "o1", PeerPriority.PERMITTED
        )
        assert not received_note.priority
        message = daemon.log.state.messages[received_note.message_id]
        assert message.priority_asked and not message.priority


@pytest.mark.parametrize(("priority_in", "honoured"), [(True, True), (False, False)])
def test_a_note_says_whether_its_peers_priority_was_honoured(
    paths: ProjectPaths, priority_in: bool, honoured: bool
) -> None:
    with attached(paths, config(priority_in=priority_in)) as daemon:
        session = PeerSession(daemon)
        assert peer_ask(session, hello()).ok
        delivered = peer_ask(session, line("deliver", envelope(priority=True)))
        assert delivered.result is not None
        assert delivered.result["priority"] is honoured
        message_id = cast(str, delivered.result["message_id"])
        notes = daemon.scheduler.mail_notes(message_id)
        assert notes == ("priority" if honoured else PRIORITY_DECLINED)
        ordinary = peer_ask(session, line("deliver", envelope(origin_id="o2")))
        assert ordinary.result is not None
        plain = cast(str, ordinary.result["message_id"])
        assert daemon.scheduler.mail_notes(plain) == ""
