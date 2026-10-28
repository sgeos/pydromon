"""Both ends of federation's phase 2, over Transport Layer Security.

``docs/architecture/FEDERATION.md`` is the design, and ``dromon.federation_tls``, the
engines role's module, holds the transport's primitives. This module puts them behind
the forwarder's ``Connector`` seam, so the forwarder's ``hello`` and ``deliver`` are the
same on both transports.

- A ``tls`` peer is reached at its ``address``. This daemon presents the identity that
  ``dromon peer key`` made in ``.dromon/peers``, trusts only the peer's certificate,
  read from its ``certificate`` path under ``.dromon``, and checks the peer's key
  against its ``pin_sha256`` before any frame is written.
- The handshake has an absolute deadline of 10 seconds, after a connection attempt of
  at most 10 seconds. Each later frame, written or read, has its own deadline of
  ``ANSWER_TIMEOUT_SECONDS``.
- ``hello`` carries no token over TLS, since the pinned key names the daemon.
- A refused client certificate shows only as a missing answer, since a TLS 1.3 client
  can finish its handshake before the server judges its certificate. The forwarder
  records delivery only from an accepted answer, so such a refusal delivers nothing.

The listening end. ``server_context_for`` builds one server context that trusts the
certificate of every inbound ``tls`` peer, each checked by ``dromon.federation_tls``,
and the daemon's listener upgrades each connection it has admitted with it. After the
handshake, ``pinned_peer`` names the peer whose ``pin_sha256`` the presented key
matches, so trust in a certificate never names a peer by itself. The context is built
at start, so a peer added or a certificate replaced by a reload is trusted only after
a restart, while a peer removed or a pin changed is refused at its next request.

Threads. ``dromon.federation_tls`` is blocking, so each of its calls runs in a worker
thread with ``asyncio.to_thread``. A link carries one request at a time, and a lock
keeps its close from running while a read or write is under way.

Technical debt. A thread cannot be cancelled, so a stop that comes during a read waits
for that read, at most ``ANSWER_TIMEOUT_SECONDS``, before the link closes. The name of
a peer's host is resolved in the connecting thread without a deadline of its own, as
``socket.create_connection`` does. **Porting note.** The Rust port reaches the same
seam with an asynchronous TLS library and needs neither the threads nor the lock.
"""

import asyncio
import hmac
import socket
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from cryptography import x509

from dromon.federation import (
    TRANSPORT_TLS,
    FederationConfig,
    FederationError,
    PeerDefinition,
    read_certificate,
    read_identity,
)
from dromon.federation_tls import (
    FrameReader,
    IdentityFiles,
    TransportError,
    client_context,
    new_connection_deadline,
    public_key_pin,
    send_frame,
    server_context,
    wrap_client,
)
from dromon.forwarder import (
    ANSWER_TIMEOUT_SECONDS,
    CONNECT_TIMEOUT_SECONDS,
    Connector,
    Link,
    LinkError,
    connect_unix,
)
from dromon.model import State
from dromon.protocol import (
    MAX_LINE_BYTES,
    MAX_PEER_LINE_BYTES,
    ProtocolError,
    Request,
    Response,
    decode_response,
    encode_request,
)

type Dial = Callable[[str, int, float], socket.socket]
"""Opens a connection to a host and port within a timeout in seconds, or raises
``OSError``. A seam, so that tests can reach the connector without a network."""


class Channel(Protocol):
    """A blocking channel of newline-ended frames, which ``TlsLink`` drives from
    worker threads, one call at a time."""

    def send(self, frame: bytes) -> None: ...

    def receive(self) -> bytes | None: ...

    def close(self) -> None: ...


@dataclass(slots=True)
class TlsChannel:
    """A pinned TLS connection and its frame reader."""

    connection: ssl.SSLSocket
    reader: FrameReader

    def send(self, frame: bytes) -> None:
        send_frame(self.connection, frame, frame_timeout_seconds=ANSWER_TIMEOUT_SECONDS)

    def receive(self) -> bytes | None:
        return self.reader.read_frame()

    def close(self) -> None:
        self.reader.close()


@dataclass(slots=True)
class TlsLink:
    """A link over a ``Channel``, whose blocking calls run in worker threads."""

    channel: Channel
    lock: threading.Lock = field(default_factory=threading.Lock)
    """Held by each call on the channel, so that a close waits for a read or write
    under way and never closes a socket another thread is using."""

    async def exchange(self, request: Request) -> Response:
        try:
            frame = encode_request(request, MAX_PEER_LINE_BYTES)
            line = await asyncio.to_thread(self._round, frame)
            if line is None:
                raise LinkError("the peer closed the connection without an answer")
            if len(line) > MAX_LINE_BYTES:
                raise LinkError(
                    f"the peer's answer exceeds the {MAX_LINE_BYTES} bytes an answer"
                    " may take"
                )
            response = decode_response(line)
        except (OSError, ValueError, ProtocolError) as error:
            # ``TransportError`` is a ``ValueError``, and ``ssl.SSLError`` an
            # ``OSError``, so every failure of the transport is caught here.
            raise LinkError(f"the exchange with the peer failed: {error}") from error
        if response.request_id != request.request_id:
            raise LinkError("the peer answered another request")
        return response

    async def close(self) -> None:
        await asyncio.to_thread(self._close)

    def _round(self, frame: bytes) -> bytes | None:
        with self.lock:
            self.channel.send(frame)
            return self.channel.receive()

    def _close(self) -> None:
        with self.lock:
            self.channel.close()


def dial(host: str, port: int, timeout: float) -> socket.socket:
    """Open a Transmission Control Protocol connection to ``host`` and ``port``."""
    return socket.create_connection((host, port), timeout)


def open_channel(
    reach: Dial,
    address: tuple[str, int],
    identity: IdentityFiles,
    certificate: Path,
    pin_sha256: str,
) -> TlsChannel:
    """Connect, complete the handshake and check the peer's pin. Blocking. Raises
    ``OSError`` or ``ValueError``, and leaves no socket open when it does."""
    context = client_context(identity, certificate)
    host, port = address
    raw = reach(host, port, CONNECT_TIMEOUT_SECONDS)
    # ``wrap_client`` closes ``raw`` when the handshake or the pin fails.
    connection = wrap_client(
        raw, context, pin_sha256, connection_deadline=new_connection_deadline()
    )
    reader = FrameReader(connection, frame_timeout_seconds=ANSWER_TIMEOUT_SECONDS)
    return TlsChannel(connection, reader)


def tls_connector(state_dir: Path, store: Path, reach: Dial = dial) -> Connector:
    """A connector to ``tls`` peers. ``state_dir`` is the ``.dromon`` that each peer's
    ``certificate`` path is relative to, and ``store`` holds this daemon's identity.
    The identity is read at each connection, so one that ``dromon peer key`` makes
    while the daemon runs is used at the next attempt."""

    async def connect(peer_id: str, definition: PeerDefinition) -> Link:
        address, pin, certificate = (
            definition.address,
            definition.pin_sha256,
            definition.certificate,
        )
        if (
            definition.transport != TRANSPORT_TLS
            or address is None
            or pin is None
            or certificate is None
        ):
            raise LinkError(f"peer {peer_id} is not reached over tls")
        try:
            identity = await asyncio.to_thread(read_identity, store)
        except (FederationError, OSError) as error:
            raise LinkError(f"this daemon's identity is unusable: {error}") from error
        if identity is None:
            raise LinkError(
                f"this daemon has no identity to present to peer {peer_id},"
                " which dromon peer key makes"
            )
        try:
            channel = await asyncio.to_thread(
                open_channel, reach, address, identity, state_dir / certificate, pin
            )
        except (OSError, ValueError) as error:
            raise LinkError(f"could not connect to peer {peer_id}: {error}") from error
        return TlsLink(channel)

    return connect


def peer_connector(state_dir: Path, store: Path) -> Connector:
    """The daemon's connector, which reaches each peer over its own transport."""
    tls = tls_connector(state_dir, store)

    async def connect(peer_id: str, definition: PeerDefinition) -> Link:
        if definition.transport == TRANSPORT_TLS:
            return await tls(peer_id, definition)
        return await connect_unix(peer_id, definition)

    return connect


def inbound_tls(federation: FederationConfig) -> list[tuple[str, PeerDefinition]]:
    """The declared ``tls`` peers that may deliver here, in name order."""
    return [
        (peer_id, definition)
        for peer_id, definition in sorted(federation.peers.items())
        if definition.transport == TRANSPORT_TLS and definition.inbound
    ]


def server_context_for(
    state_dir: Path, store: Path, federation: FederationConfig
) -> ssl.SSLContext:
    """A server context with this daemon's identity that trusts every inbound ``tls``
    peer's certificate and requires one of them. Each certificate is checked by
    ``server_context``, which builds the context for the first, and the rest are
    then added to it. Raises ``FederationError`` when there is no identity, no inbound
    ``tls`` peer, or a file that does not pass."""
    peers = inbound_tls(federation)
    if not peers:
        raise FederationError("no tls peer is inbound, so nothing listens over tls")
    identity = read_identity(store)
    if identity is None:
        raise FederationError(
            "this daemon has no identity to present over tls, which dromon peer key"
            " makes"
        )
    context: ssl.SSLContext | None = None
    for peer_id, definition in peers:
        if definition.certificate is None:
            raise FederationError(f"peer {peer_id} names no certificate")
        path = state_dir / definition.certificate
        try:
            checked = server_context(identity, path)
            if context is None:
                context = checked
            else:
                context.load_verify_locations(
                    cadata=read_certificate(path).decode("ascii")
                )
        except (TransportError, ssl.SSLError, UnicodeDecodeError) as error:
            raise FederationError(
                f"the certificate of peer {peer_id} is unusable: {error}"
            ) from error
    if context is None:
        raise FederationError("no tls peer is inbound, so nothing listens over tls")
    return context


def presented_pin(ssl_object: object) -> str | None:
    """The pin of the key the other end presented in a finished handshake, or
    ``None`` when it presented none or one that does not load."""
    if not isinstance(ssl_object, ssl.SSLObject | ssl.SSLSocket):
        return None
    der = ssl_object.getpeercert(binary_form=True)
    if not der:
        return None
    try:
        return public_key_pin(x509.load_der_x509_certificate(der))
    except ValueError:
        return None


def pinned_peer(federation: FederationConfig, state: State, pin: str) -> str | None:
    """The inbound ``tls`` peer, declared and not removed, whose ``pin_sha256`` is
    ``pin``, or ``None``. Every pin is compared in constant time, as tokens are,
    although a pin is not a secret. The parser refuses two peers with one pin."""
    found: str | None = None
    for peer_id, definition in inbound_tls(federation):
        peer = state.peers.get(peer_id)
        if peer is None or peer.removed or definition.pin_sha256 is None:
            continue
        if hmac.compare_digest(definition.pin_sha256, pin):
            found = peer_id
    return found
