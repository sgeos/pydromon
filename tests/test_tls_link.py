"""The connector of federation's phase 2, over Transport Layer Security."""

import asyncio
import contextlib
import os
import socket
import ssl
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final

import pytest

from dromon.federation import (
    CERTIFICATE_FILENAME,
    FederationConfig,
    FederationError,
    PeerDefinition,
    ensure_identity,
    parse_federation,
)
from dromon.federation_tls import (
    FrameReader,
    IdentityFiles,
    TransportError,
    client_context,
    generate_identity,
    send_frame,
    server_context,
    wrap_server,
)
from dromon.forwarder import Connector, Link, LinkError
from dromon.protocol import (
    MAX_LINE_BYTES,
    Operation,
    Request,
    Response,
    decode_request,
    encode_request,
    encode_response,
)
from dromon.model import PeerDeclared, PeerRemoved, State
from dromon.tls_link import (
    Dial,
    TlsLink,
    inbound_tls,
    peer_connector,
    pinned_peer,
    presented_pin,
    server_context_for,
    tls_connector,
)

PEER: Final = "studio"
CERTIFICATE: Final = "peers/studio.pem"
HELLO: Final = Request("r-1", Operation.HELLO, {"versions": [1], "name": "core"})
TIMEOUT_SECONDS: Final = 3.0


def definition(transport: str = "tls", **keys: object) -> PeerDefinition:
    table: dict[str, object] = (
        {
            "transport": "tls",
            "address": "studio.example.net:7411",
            "pin_sha256": "0123456789abcdef" * 4,
            "certificate": CERTIFICATE,
            "outbound": True,
        }
        if transport == "tls"
        else {"socket": "/nonexistent/run/peers.sock", "outbound": True}
    )
    table.update(keys)
    return parse_federation(None, {PEER: table}).peers[PEER]


class FakeChannel:
    """A channel that answers from a script and records what it was sent."""

    def __init__(self, *answers: bytes | None | Exception) -> None:
        self.answers = list(answers)
        self.sent: list[bytes] = []
        self.closed = 0

    def send(self, frame: bytes) -> None:
        self.sent.append(frame)

    def receive(self) -> bytes | None:
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def close(self) -> None:
        self.closed += 1


def attempt(connect: Connector, found: PeerDefinition) -> Link:
    async def run() -> Link:
        return await connect(PEER, found)

    return asyncio.run(run())


def answer(request_id: str = HELLO.request_id) -> bytes:
    return encode_response(Response(request_id, ok=True, result={"version": 1}))


# The link.


def test_a_link_writes_the_request_and_reads_its_answer() -> None:
    channel = FakeChannel(answer())
    link = TlsLink(channel)
    response = asyncio.run(link.exchange(HELLO))
    assert response == Response(HELLO.request_id, ok=True, result={"version": 1})
    assert channel.sent == [encode_request(HELLO)]
    asyncio.run(link.close())
    assert channel.closed == 1
    assert not link.lock.locked()


@pytest.mark.parametrize(
    ("reply", "match"),
    [
        (None, "without an answer"),
        (b"{" + b" " * MAX_LINE_BYTES + b"}\n", "exceeds"),
        (answer("r-other"), "another request"),
        (b"not json\n", "exchange with the peer failed"),
        (TransportError("the frame deadline expired"), "deadline expired"),
        (ssl.SSLError("tlsv13 alert certificate required"), "certificate required"),
        (ConnectionResetError("reset"), "reset"),
    ],
)
def test_a_link_turns_every_failure_into_a_link_error(
    reply: bytes | None | Exception, match: str
) -> None:
    """A refused client certificate arrives as an alert or a closed connection in
    place of the first answer, and is a transient failure like any other."""
    link = TlsLink(FakeChannel(reply))
    with pytest.raises(LinkError, match=match):
        asyncio.run(link.exchange(HELLO))
    assert not link.lock.locked()


def test_a_close_waits_for_the_exchange_under_way() -> None:
    entered, release = threading.Event(), threading.Event()
    order: list[str] = []

    class SlowChannel(FakeChannel):
        def receive(self) -> bytes | None:
            entered.set()
            release.wait(TIMEOUT_SECONDS)
            order.append("received")
            return super().receive()

        def close(self) -> None:
            order.append("closed")

    link = TlsLink(SlowChannel(answer()))

    async def scenario() -> None:
        exchange = asyncio.ensure_future(link.exchange(HELLO))
        await asyncio.to_thread(entered.wait, TIMEOUT_SECONDS)
        closing = asyncio.ensure_future(link.close())
        await asyncio.sleep(0.05)
        assert not closing.done()
        release.set()
        await exchange
        await closing

    asyncio.run(scenario())
    assert order == ["received", "closed"]


# The connector, short of the network.


def refusing(calls: list[tuple[str, int]]) -> Dial:
    def reach(host: str, port: int, timeout: float) -> socket.socket:
        calls.append((host, port))
        raise ConnectionRefusedError("connection refused")

    return reach


def keyed(tmp_path: Path) -> tuple[Path, Path, IdentityFiles]:
    """A state directory with this daemon's identity and the peer's certificate."""
    state_dir = tmp_path / ".dromon"
    store = state_dir / "peers"
    ensure_identity(store)
    peer = generate_identity(tmp_path / "peer")
    (state_dir / CERTIFICATE).write_bytes(peer.certificate.read_bytes())
    os.chmod(state_dir / CERTIFICATE, 0o600)
    return state_dir, store, peer


def test_the_connector_resolves_the_certificate_under_the_state_directory(
    tmp_path: Path,
) -> None:
    state_dir, store, _ = keyed(tmp_path)
    calls: list[tuple[str, int]] = []
    connect = tls_connector(state_dir, store, refusing(calls))
    with pytest.raises(LinkError, match="could not connect to peer studio"):
        attempt(connect, definition())
    assert calls == [("studio.example.net", 7411)]


def test_the_connector_needs_this_daemons_identity(tmp_path: Path) -> None:
    calls: list[tuple[str, int]] = []
    connect = tls_connector(tmp_path, tmp_path / "peers", refusing(calls))
    with pytest.raises(LinkError, match="dromon peer key"):
        attempt(connect, definition())
    assert calls == []


def test_the_connector_refuses_a_damaged_identity(tmp_path: Path) -> None:
    state_dir, store, _ = keyed(tmp_path)
    os.chmod(store / CERTIFICATE_FILENAME, 0o644)
    calls: list[tuple[str, int]] = []
    connect = tls_connector(state_dir, store, refusing(calls))
    with pytest.raises(LinkError, match="identity is unusable"):
        attempt(connect, definition())
    assert calls == []


@pytest.mark.parametrize("damage", ["missing", "readable", "garbage"])
def test_the_connector_refuses_a_peer_certificate_it_cannot_trust(
    tmp_path: Path, damage: str
) -> None:
    """The peer's certificate is checked before any connection is attempted."""
    state_dir, store, _ = keyed(tmp_path)
    path = state_dir / CERTIFICATE
    if damage == "missing":
        path.unlink()
    elif damage == "readable":
        os.chmod(path, 0o644)
    else:
        path.write_bytes(b"not a certificate\n")
    calls: list[tuple[str, int]] = []
    connect = tls_connector(state_dir, store, refusing(calls))
    with pytest.raises(LinkError, match="could not connect to peer studio"):
        attempt(connect, definition())
    assert calls == []


def test_the_tls_connector_refuses_a_unix_peer(tmp_path: Path) -> None:
    connect = tls_connector(tmp_path, tmp_path / "peers", refusing([]))
    with pytest.raises(LinkError, match="not reached over tls"):
        attempt(connect, definition("unix"))


def test_the_daemons_connector_follows_each_peers_transport(tmp_path: Path) -> None:
    connect = peer_connector(tmp_path, tmp_path / "peers")
    with pytest.raises(LinkError, match="dromon peer key"):
        attempt(connect, definition())
    with pytest.raises(LinkError, match="could not connect to peer studio"):
        attempt(connect, definition("unix"))


# The listening end, short of the network.


def listening_config(
    state_dir: Path, peers: dict[str, IdentityFiles], **keys: object
) -> FederationConfig:
    """``tls`` peers, each with its certificate copied under ``state_dir``, built
    without the parser, so that more than one may be inbound."""
    found: dict[str, PeerDefinition] = {}
    for peer_id, identity in peers.items():
        relative = f"peers/{peer_id}.pem"
        (state_dir / relative).write_bytes(identity.certificate.read_bytes())
        os.chmod(state_dir / relative, 0o600)
        table: dict[str, object] = {
            "transport": "tls",
            "address": "studio.example.net:7411",
            "pin_sha256": identity.pin_sha256,
            "certificate": relative,
            "inbound": True,
            "reach": ["alpha"],
            **keys,
        }
        listen = {"tls_listen": "127.0.0.1:7411"} if table["inbound"] else {}
        found[peer_id] = parse_federation(listen, {peer_id: table}).peers[peer_id]
    return FederationConfig(peers=found, tls_listen=("127.0.0.1", 7411))


def handshake(
    client: ssl.SSLContext, server: ssl.SSLContext
) -> tuple[ssl.SSLObject, ssl.SSLObject]:
    """A TLS handshake in memory, so that no socket is needed. Raises
    ``ssl.SSLError`` when either end refuses."""
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    near = client.wrap_bio(client_in, client_out, server_side=False)
    far = server.wrap_bio(server_in, server_out, server_side=True)
    for _ in range(8):
        for end in (near, far):
            with contextlib.suppress(ssl.SSLWantReadError):
                end.do_handshake()
        server_in.write(client_out.read())
        client_in.write(server_out.read())
    return near, far


def identities(
    tmp_path: Path, *names: str
) -> tuple[Path, Path, dict[str, IdentityFiles]]:
    state_dir = tmp_path / ".dromon"
    store = state_dir / "peers"
    ensure_identity(store)
    return state_dir, store, {n: generate_identity(tmp_path / n) for n in names}


def test_the_listener_trusts_its_inbound_peer_and_names_it_by_its_pin(
    tmp_path: Path,
) -> None:
    state_dir, store, peers = identities(tmp_path, "studio")
    config = listening_config(state_dir, peers)
    context = server_context_for(state_dir, store, config)
    local, _ = ensure_identity(store)
    state = State()
    state.apply(PeerDeclared("studio", "tls", "{}"))
    _, far = handshake(client_context(peers["studio"], local.certificate), context)
    pin = presented_pin(far)
    assert pin is not None and pin == peers["studio"].pin_sha256
    assert pinned_peer(config, state, pin) == "studio"
    state.apply(PeerRemoved("studio"))
    assert pinned_peer(config, state, pin) is None


def test_the_listener_refuses_a_key_it_does_not_trust(tmp_path: Path) -> None:
    state_dir, store, peers = identities(tmp_path, "studio", "stranger")
    config = listening_config(state_dir, {"studio": peers["studio"]})
    context = server_context_for(state_dir, store, config)
    local, _ = ensure_identity(store)
    with pytest.raises(ssl.SSLError):
        handshake(client_context(peers["stranger"], local.certificate), context)


def test_one_listener_context_trusts_two_of_these_certificates(tmp_path: Path) -> None:
    """The cause of ``MAX_INBOUND_TLS`` is gone. Each certificate now names its own key
    in its subject and carries a key identifier, so a listener's context trusting two
    peers admits both and tells them apart by pin. Lifting the limit is core's step."""
    state_dir, store, peers = identities(tmp_path, "studio", "workshop")
    context = server_context_for(state_dir, store, listening_config(state_dir, peers))
    assert context.cert_store_stats()["x509"] == len(peers)
    local, _ = ensure_identity(store)
    for name, identity in peers.items():
        _, far = handshake(client_context(identity, local.certificate), context)
        assert presented_pin(far) == identity.pin_sha256, name


def test_only_inbound_tls_peers_are_trusted_or_named(tmp_path: Path) -> None:
    state_dir, store, peers = identities(tmp_path, "studio", "workshop")
    config = listening_config(state_dir, {"studio": peers["studio"]})
    closed = listening_config(state_dir, {"workshop": peers["workshop"]}, inbound=False)
    merged = FederationConfig(
        peers={**config.peers, **closed.peers}, tls_listen=config.tls_listen
    )
    assert [peer_id for peer_id, _ in inbound_tls(merged)] == ["studio"]
    state = State()
    state.apply(PeerDeclared("workshop", "tls", "{}"))
    assert pinned_peer(merged, state, peers["workshop"].pin_sha256) is None
    trusted = server_context_for(state_dir, store, merged)
    assert trusted.cert_store_stats()["x509"] == 1


def test_the_listener_needs_an_identity_and_a_usable_certificate(
    tmp_path: Path,
) -> None:
    state_dir, store, peers = identities(tmp_path, "studio")
    config = listening_config(state_dir, peers)
    with pytest.raises(FederationError, match="dromon peer key"):
        server_context_for(state_dir, tmp_path / "empty", config)
    os.chmod(state_dir / "peers/studio.pem", 0o644)
    with pytest.raises(FederationError, match="studio is unusable"):
        server_context_for(state_dir, store, config)
    with pytest.raises(FederationError, match="no tls peer is inbound"):
        server_context_for(state_dir, store, FederationConfig())


def test_no_pin_is_presented_by_what_is_not_a_finished_handshake() -> None:
    assert presented_pin(None) is None
    assert presented_pin("not tls") is None


# Over a real connection, which a sandbox denies.


def serve_one(
    listener: socket.socket,
    identity: IdentityFiles,
    client: IdentityFiles,
    respond: Callable[[Request], bytes],
) -> threading.Thread:
    """Accept one connection, answer its first request, and wait for its close."""

    def run() -> None:
        raw, _ = listener.accept()
        raw.settimeout(TIMEOUT_SECONDS)
        try:
            context = server_context(identity, client.certificate)
            connection = wrap_server(raw, context, client.pin_sha256)
        except (OSError, ValueError):
            return
        reader = FrameReader(connection, frame_timeout_seconds=TIMEOUT_SECONDS)
        frame = reader.read_frame()
        if frame is not None:
            send_frame(connection, respond(decode_request(frame)))
            reader.read_frame()
        reader.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def listening() -> tuple[socket.socket, int]:
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(TIMEOUT_SECONDS)
    return listener, listener.getsockname()[1]


@pytest.mark.socket
def test_a_note_crosses_a_pinned_tls_connection(tmp_path: Path) -> None:
    state_dir, store, peer = keyed(tmp_path)
    local, _ = ensure_identity(store)
    listener, port = listening()
    with listener:
        thread = serve_one(listener, peer, local, lambda r: answer(r.request_id))
        connect = tls_connector(state_dir, store)
        found = definition(address=f"127.0.0.1:{port}", pin_sha256=peer.pin_sha256)

        async def scenario() -> Response:
            link = await connect(PEER, found)
            try:
                return await link.exchange(HELLO)
            finally:
                await link.close()

        response = asyncio.run(scenario())
        thread.join(TIMEOUT_SECONDS)
    assert response.ok and response.result == {"version": 1}


@pytest.mark.socket
def test_a_peer_whose_key_differs_from_its_pin_is_refused(tmp_path: Path) -> None:
    state_dir, store, peer = keyed(tmp_path)
    local, _ = ensure_identity(store)
    impostor = generate_identity(tmp_path / "impostor")
    listener, port = listening()
    with listener:
        # The impostor presents its own key but holds the peer's certificate file,
        # so the handshake fails before any frame is written.
        thread = serve_one(listener, impostor, local, lambda r: answer(r.request_id))
        connect = tls_connector(state_dir, store)
        found = definition(address=f"127.0.0.1:{port}", pin_sha256=peer.pin_sha256)
        with pytest.raises(LinkError, match="could not connect to peer studio"):
            attempt(connect, found)
        thread.join(TIMEOUT_SECONDS)
