"""The open TLS listener, ``remote = "open"``, over pyOpenSSL, round 12.

``docs/architecture/FEDERATION.md`` is normative, under open federation. A client from
the standard library talks to ``OpenTlsStream`` over memory BIOs, so the handshake and
the frames after it run in process without a socket. The one test marked ``socket``
reaches the listener's handler over a real TCP connection, which a sandbox deselects.
"""

import asyncio
import hashlib
import json
import socket
import ssl
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

from dromon import daemon as daemon_module
from dromon import open_tls
from dromon.configview import Overrides
from dromon.daemon import Daemon, open_tls_peer_connection, tls_serving
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.federation import (
    PEERS_DIR_NAME,
    SCOPE_REMOTE,
    FederationConfig,
    FederationError,
    parse_federation,
    plan_peers,
    read_identity,
)
from dromon.federation_tls import (
    FrameReader,
    IdentityFiles,
    client_context,
    generate_identity,
    public_key_pin,
    send_frame,
    wrap_client,
)
from dromon.model import LineAddress, RemoteAddress
from dromon.open_tls import OpenTlsError, OpenTlsStream, accepted_pin
from dromon.paths import project_paths
from dromon.peering import MAX_HELLO_LINE_BYTES
from dromon.projectconfig import ProjectConfig
from dromon.scheduler import LineEngine, LineSpec, Scheduler

AT = "2026-10-31T00:00:00Z"
NOW = datetime(2026, 10, 31, tzinfo=UTC)
STEPS = 400
"""The most passes a memory exchange takes, far above a handshake's few."""

type PrivateKey = Ed25519PrivateKey | ec.EllipticCurvePrivateKey


def certificate(
    key: PrivateKey,
    *,
    signer: PrivateKey | None = None,
    issuer: str = "far",
    before: datetime | None = None,
    after: datetime | None = None,
) -> x509.Certificate:
    """A certificate for ``key`` named ``far``, self-signed unless ``signer`` or
    ``issuer`` says otherwise, valid for a year around ``NOW`` by default."""
    signing = signer or key
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "far")]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer)]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(before or NOW - timedelta(days=1))
        .not_valid_after(after or NOW + timedelta(days=365))
    )
    if isinstance(signing, Ed25519PrivateKey):
        return builder.sign(signing, None)
    return builder.sign(signing, hashes.SHA256())


def pin_of(found: x509.Certificate) -> str:
    return hashlib.sha256(
        found.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).hexdigest()


def stored_pair(
    directory: Path,
    key: PrivateKey,
    issued: x509.Certificate,
) -> tuple[Path, Path]:
    """A fresh identity whose files now hold ``key`` and ``issued``. Rewriting the
    files keeps the owner-only modes ``generate_identity`` gave them."""
    made = generate_identity(directory)
    key_bytes = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    certificate_bytes = issued.public_bytes(serialization.Encoding.PEM)
    made.private_key.write_bytes(key_bytes)
    made.certificate.write_bytes(certificate_bytes)
    return made.private_key, made.certificate


def stdlib_client(
    server: IdentityFiles,
    pair: tuple[Path, Path] | None,
    version: ssl.TLSVersion = ssl.TLSVersion.TLSv1_3,
) -> ssl.SSLContext:
    """A standard-library client context for a key of any type, or for none."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.minimum_version = version
    context.maximum_version = version
    context.load_verify_locations(cadata=server.certificate.read_text())
    if pair is not None:
        key_path, cert_path = pair
        context.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return context


# The certificate rule.


def test_only_a_valid_self_signed_ed25519_certificate_is_accepted() -> None:
    key = Ed25519PrivateKey.generate()
    good = certificate(key)
    assert accepted_pin(good, NOW) == pin_of(good)
    p256 = certificate(ec.generate_private_key(ec.SECP256R1()))
    assert accepted_pin(p256, NOW) is None
    forged = certificate(key, signer=Ed25519PrivateKey.generate())
    assert accepted_pin(forged, NOW) is None
    assert accepted_pin(certificate(key, issuer="authority"), NOW) is None
    assert accepted_pin(good, NOW + timedelta(days=366)) is None
    assert accepted_pin(good, NOW - timedelta(days=2)) is None


def test_the_server_context_needs_its_own_pinned_ed25519_identity(
    tmp_path: Path,
) -> None:
    identity = generate_identity(tmp_path / "own")
    open_tls.open_server_context(identity)
    wrong = IdentityFiles(identity.private_key, identity.certificate, "c" * 64)
    with pytest.raises(FederationError, match="not its pinned identity"):
        open_tls.open_server_context(wrong)
    p256 = ec.generate_private_key(ec.SECP256R1())
    key_path, cert_path = stored_pair(tmp_path / "p256", p256, certificate(p256))
    with pytest.raises(FederationError, match="not Ed25519"):
        open_tls.open_server_context(
            IdentityFiles(key_path, cert_path, public_key_pin(certificate(p256)))
        )


# The handshake over memory.


class Wire:
    """The server's raw writer, whose bytes reach the client's incoming BIO."""

    def __init__(self, incoming: ssl.MemoryBIO) -> None:
        self.incoming = incoming
        self.closed = False

    def write(self, data: bytes) -> None:
        if not self.closed:
            self.incoming.write(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None

    def get_extra_info(self, _name: str, default: object = None) -> object:
        return default


class Client:
    """A standard-library TLS client over memory BIOs, wired to a server's stream."""

    def __init__(self, context: ssl.SSLContext) -> None:
        self.incoming = ssl.MemoryBIO()
        self.outgoing = ssl.MemoryBIO()
        self.tls = context.wrap_bio(self.incoming, self.outgoing, server_side=False)
        self.reader = asyncio.StreamReader()
        self.wire = Wire(self.incoming)
        self.received = bytearray()

    def pump(self) -> None:
        data = self.outgoing.read()
        if data:
            self.reader.feed_data(data)

    async def handshake(self) -> bool:
        """Whether the client finished its side, which a TLS 1.3 client does before
        the server judges its certificate."""
        for _ in range(STEPS):
            try:
                self.tls.do_handshake()
            except ssl.SSLWantReadError:
                self.pump()
                await asyncio.sleep(0)
                continue
            except ssl.SSLError:
                self.pump()
                return False
            self.pump()
            return True
        raise AssertionError("the handshake did not settle")

    def send(self, line: bytes) -> None:
        self.tls.write(line)
        self.pump()

    async def line(self) -> bytes:
        for _ in range(STEPS):
            end = self.received.find(b"\n")
            if end >= 0:
                found = bytes(self.received[: end + 1])
                del self.received[: end + 1]
                return found
            try:
                self.received += self.tls.read(65536)
            except ssl.SSLWantReadError:
                await asyncio.sleep(0)
        raise AssertionError("no line arrived")


async def server_pin(context: ssl.SSLContext, identity: IdentityFiles) -> str:
    client = Client(context)
    stream = OpenTlsStream(
        open_tls.open_server_context(identity),
        client.reader,
        client.wire,
        MAX_HELLO_LINE_BYTES,
    )
    server = asyncio.create_task(stream.handshake())
    if not await client.handshake():
        client.reader.feed_eof()
    return await asyncio.wait_for(server, 5)


def test_an_unknown_ed25519_key_is_accepted_and_named_by_its_pin(
    tmp_path: Path,
) -> None:
    server = generate_identity(tmp_path / "server")
    far = generate_identity(tmp_path / "far")
    context = client_context(far, server.certificate)
    assert asyncio.run(server_pin(context, server)) == far.pin_sha256


@pytest.mark.parametrize("case", ["p256", "none", "expired", "tls12"])
def test_every_other_client_is_refused_in_the_handshake(
    tmp_path: Path, case: str
) -> None:
    server = generate_identity(tmp_path / "server")
    if case == "p256":
        key = ec.generate_private_key(ec.SECP256R1())
        context = stdlib_client(
            server, stored_pair(tmp_path / "c", key, certificate(key))
        )
    elif case == "expired":
        key = Ed25519PrivateKey.generate()
        old = certificate(
            key,
            before=datetime.now(UTC) - timedelta(days=30),
            after=datetime.now(UTC) - timedelta(days=1),
        )
        context = stdlib_client(server, stored_pair(tmp_path / "c", key, old))
    elif case == "tls12":
        key = Ed25519PrivateKey.generate()
        context = stdlib_client(
            server,
            stored_pair(tmp_path / "c", key, certificate(key)),
            ssl.TLSVersion.TLSv1_2,
        )
    else:
        context = stdlib_client(server, None)
    with pytest.raises(OpenTlsError):
        asyncio.run(server_pin(context, server))


# The listener's handler with a daemon.


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


REMOTE_OPEN = {
    "tls_listen": "127.0.0.1:7411",
    "remote": "open",
    "allowed_networks": ["127.0.0.0/8", "::1/128"],
    "open": {"reach": ["core"], "remote_reach": ["@ana"]},
}


@contextmanager
def daemon_for(
    root: Path, runtime: Path, federation: FederationConfig
) -> Generator[Daemon]:
    root.mkdir(parents=True, exist_ok=True)
    paths = project_paths(root, runtime)
    paths.state_dir.mkdir(mode=0o700, exist_ok=True)
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("core", "claude-haiku-5-5", Strategy.SUBSCRIPTION)
        scheduler.ensure_operator("ana")
        for event in plan_peers(log.state, federation):
            log.append(event)
        scheduler.federation = federation
        daemon_module.open_federation(scheduler, log, federation)
        yield Daemon(
            paths,
            log,
            AT,
            scheduler,
            # The file declares ``ana``, whom a peer may reach, decision 25.
            project=ProjectConfig(operator="ana", federation=federation),
            command_line=Overrides(),
            project_file_present=False,
        )


def frame(op: str, args: dict[str, object], request_id: str = "r") -> bytes:
    return (
        json.dumps({"id": request_id, "op": op, "v": 1, "args": args}) + "\n"
    ).encode()


def note(recipient: str, origin_id: str) -> dict[str, object]:
    return {
        "origin_id": origin_id,
        "sender": "alpha",
        "recipient": recipient,
        "kind": "note",
        "body": "hello across machines",
        "reply_to": None,
        "priority": False,
    }


async def converse(
    daemon: Daemon, context: ssl.SSLContext, lines: Sequence[bytes]
) -> list[dict[str, Any]]:
    """Serve one connection through the listener's handler and send ``lines``,
    returning each answer."""
    client = Client(context)
    identity = read_identity(daemon.paths.state_dir / PEERS_DIR_NAME)
    assert identity is not None
    serving = asyncio.create_task(
        open_tls_peer_connection(
            daemon,
            client.reader,
            client.wire,
            open_tls.open_server_context(identity),
        )
    )
    assert await client.handshake()
    answers: list[dict[str, Any]] = []
    for line in lines:
        client.send(line)
        answers.append(json.loads(await client.line()))
    client.reader.feed_eof()
    await asyncio.wait_for(serving, 5)
    return answers


def test_an_unknown_key_reaches_only_remote_reach_through_the_listener(
    tmp_path: Path, short_runtime: Path
) -> None:
    federation = parse_federation(REMOTE_OPEN, None)
    with daemon_for(tmp_path / "harbor", short_runtime, federation) as daemon:
        server = generate_identity(daemon.paths.state_dir / PEERS_DIR_NAME)
        far = generate_identity(tmp_path / "far")
        context = client_context(far, server.certificate)
        hello, to_core, to_ana = asyncio.run(
            converse(
                daemon,
                context,
                [
                    frame("hello", {"versions": [1], "name": "far"}, "h"),
                    frame("deliver", note("core", "o1"), "d1"),
                    frame("deliver", note("@ana", "o2"), "d2"),
                ],
            )
        )
        assert hello["ok"], hello
        assert to_core["result"]["code"] == "refused"
        assert to_ana["result"]["outcome"] == "accepted"
        peer = daemon.log.state.peers[far.pin_sha256]
        assert peer.scope == SCOPE_REMOTE
        senders = [
            message.sender
            for message in daemon.log.state.messages.values()
            if isinstance(message.sender, RemoteAddress)
        ]
        assert senders == [RemoteAddress(far.pin_sha256, LineAddress("alpha"))]


def test_a_silent_client_is_closed_at_the_handshake_deadline(
    tmp_path: Path, short_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon_module, "TLS_HANDSHAKE_TIMEOUT_SECONDS", 0.05)
    federation = parse_federation(REMOTE_OPEN, None)
    with daemon_for(tmp_path / "harbor", short_runtime, federation) as daemon:
        server = generate_identity(daemon.paths.state_dir / PEERS_DIR_NAME)
        context = client_context(server, server.certificate)
        closed: list[bool] = []

        async def silent() -> None:
            client = Client(context)
            await asyncio.wait_for(
                open_tls_peer_connection(
                    daemon,
                    client.reader,
                    client.wire,
                    open_tls.open_server_context(server),
                ),
                5,
            )
            closed.append(client.wire.closed)

        asyncio.run(silent())
        assert closed == [True]
        assert not daemon.log.state.peers


def test_the_listener_takes_pyopenssl_only_while_remote_is_open(
    tmp_path: Path, short_runtime: Path
) -> None:
    federation = parse_federation(REMOTE_OPEN, None)
    with daemon_for(tmp_path / "open", short_runtime / "o", federation) as daemon:
        with pytest.raises(FederationError, match="no identity"):
            tls_serving(daemon)
        generate_identity(daemon.paths.state_dir / PEERS_DIR_NAME)
        assert tls_serving(daemon).__name__ == "open_serving"
    far = generate_identity(tmp_path / "far")
    allowlist = parse_federation(
        {"tls_listen": "127.0.0.1:7411"},
        {
            "far": {
                "transport": "tls",
                "address": "far.example.net:7411",
                "pin_sha256": far.pin_sha256,
                "certificate": "far.pem",
                "inbound": True,
            }
        },
    )
    with daemon_for(tmp_path / "closed", short_runtime / "c", allowlist) as daemon:
        generate_identity(daemon.paths.state_dir / PEERS_DIR_NAME)
        copied = daemon.paths.state_dir / "far.pem"
        copied.write_bytes(far.certificate.read_bytes())
        copied.chmod(0o600)
        assert tls_serving(daemon).__name__ == "pinned_serving"


def test_a_line_over_the_limit_is_refused_by_the_stream(tmp_path: Path) -> None:
    server = generate_identity(tmp_path / "server")
    far = generate_identity(tmp_path / "far")
    context = client_context(far, server.certificate)

    async def overlong() -> None:
        client = Client(context)
        stream = OpenTlsStream(
            open_tls.open_server_context(server), client.reader, client.wire, 8
        )
        handshake = asyncio.create_task(stream.handshake())
        assert await client.handshake()
        await asyncio.wait_for(handshake, 5)
        client.send(b"0123456789\n")
        with pytest.raises(ValueError, match="exceeds 8 bytes"):
            await asyncio.wait_for(stream.readline(), 5)

    asyncio.run(overlong())


# Over a real connection.


@pytest.mark.socket
def test_an_unknown_key_says_hello_over_a_real_connection(
    tmp_path: Path, short_runtime: Path
) -> None:
    federation = parse_federation(REMOTE_OPEN, None)
    with daemon_for(tmp_path / "harbor", short_runtime, federation) as daemon:
        server = generate_identity(daemon.paths.state_dir / PEERS_DIR_NAME)
        far = generate_identity(tmp_path / "far")
        answers: list[bytes] = []

        def call(port: int) -> None:
            raw = socket.create_connection(("127.0.0.1", port), 5)
            connection = wrap_client(
                raw, client_context(far, server.certificate), server.pin_sha256
            )
            send_frame(connection, frame("hello", {"versions": [1], "name": "far"}))
            found = FrameReader(connection).read_frame()
            answers.append(found or b"")
            connection.close()

        async def scenario() -> None:
            context = open_tls.open_server_context(server)

            async def accept(
                reader: asyncio.StreamReader, writer: asyncio.StreamWriter
            ) -> None:
                await open_tls_peer_connection(daemon, reader, writer, context)

            listening = await asyncio.start_server(accept, "127.0.0.1", 0)
            port = listening.sockets[0].getsockname()[1]
            caller = threading.Thread(target=call, args=(port,))
            caller.start()
            await asyncio.to_thread(caller.join, 10)
            listening.close()
            await listening.wait_closed()

        asyncio.run(scenario())
        assert answers and json.loads(answers[0])["ok"]
        assert daemon.log.state.peers[far.pin_sha256].scope == SCOPE_REMOTE
