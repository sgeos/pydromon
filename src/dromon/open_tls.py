"""The listening end of open remote federation, over pyOpenSSL.

``docs/architecture/FEDERATION.md`` is the design. With ``remote = "open"``, the TLS
listener accepts a client key it does not already trust, and the key's pin names the
peer. Python's ``ssl`` cannot do this. **Fact,** probed on 2026-10-31 with Python 3.12
and OpenSSL 3.5.4. A server context that requires or requests a client certificate
refuses a self-signed one it does not trust, and the module offers no callback to
decide otherwise. pyOpenSSL offers that callback, so this module, and only it, uses
pyOpenSSL. The allowlist listener stays on the standard library, unchanged.

Rules.

- TLS 1.3 only, with session tickets and the server's session cache off, so the
  server keeps no session a client could resume to skip its certificate. pyOpenSSL
  exposes no control of the number of tickets, so OpenSSL may still send a stateful
  ticket, which names no session the server kept.
- The client must present a certificate. It must be a single self-signed Ed25519
  certificate inside its validity period, as ``dromon peer key`` makes. Every other key
  type, a chain, an expired certificate and a bad self-signature are refused, in the
  verify callback and again after the handshake.
- The pin is the SHA-256 digest of the certificate's SubjectPublicKeyInfo, as for a
  declared peer, so a declared peer's key names that peer here too.

Threads. **Decided by core, accepted by the operator on 2026-10-31,** in place of the
worker-thread handshake first proposed. This module drives pyOpenSSL over memory BIOs on
the event loop, as ``asyncio`` drives the standard library's ``ssl``, so a connection
holds no thread at all. A thread cannot be cancelled at the handshake's deadline, and
each later read would hold one for as long as the peer is idle. The handshake is
bounded by ``TLS_HANDSHAKE_TIMEOUT_SECONDS`` and counted under the listener's
connection cap by the caller, as the direction asked. The handshake's signature and
key exchange run on the loop, which is inference about cost, not measured, at well
under a millisecond for Ed25519 and X25519.

**Porting note.** The Rust port uses an asynchronous TLS library with a custom client
certificate verifier, such as rustls's ``ClientCertVerifier``, and needs no BIOs.
"""

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Final, Protocol

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from OpenSSL import SSL, crypto

from dromon.federation import FederationError, read_certificate, read_store_key
from dromon.federation_tls import IdentityFiles

CHUNK_BYTES: Final = 16384
"""The most ciphertext read from the connection, or plaintext taken from the TLS
session, at once."""
SELF_SIGNED_ERRORS: Final = frozenset({0, 18})
"""The verification results the callback lets pass at depth zero, ``X509_V_OK`` and
``X509_V_ERR_DEPTH_ZERO_SELF_SIGNED_CERT``. Every other result, such as an expired
certificate, refuses the handshake."""


class RawReader(Protocol):
    """The connection's own stream of ciphertext, an ``asyncio.StreamReader``."""

    async def read(self, n: int = -1) -> bytes: ...


class RawWriter(Protocol):
    """The connection's own sink of ciphertext, an ``asyncio.StreamWriter``."""

    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


class OpenTlsError(ConnectionError):
    """The handshake or a record failed, or the client's certificate was refused. A
    ``ConnectionError``, so that a connection's server ends it as it ends a reset."""


def accepted_pin(
    certificate: x509.Certificate, now: datetime | None = None
) -> str | None:
    """The pin of a client certificate this listener accepts, or ``None``. It must be
    self-signed with a valid signature, hold an Ed25519 key, and be inside its validity
    period at ``now``."""
    public_key = certificate.public_key()
    if not isinstance(public_key, Ed25519PublicKey):
        return None
    if certificate.subject != certificate.issuer:
        return None
    try:
        certificate.verify_directly_issued_by(certificate)
    except (InvalidSignature, ValueError, TypeError):
        return None
    moment = now or datetime.now(UTC)
    if not (
        certificate.not_valid_before_utc <= moment <= certificate.not_valid_after_utc
    ):
        return None
    encoded = public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return hashlib.sha256(encoded).hexdigest()


def _verify(
    _connection: SSL.Connection,
    certificate: crypto.X509,
    error: int,
    depth: int,
    _ok: int,
) -> bool:
    """pyOpenSSL's verify callback. Only the leaf, at depth zero, may pass, and only
    with no error but its being self-signed."""
    if depth != 0 or error not in SELF_SIGNED_ERRORS:
        return False
    return accepted_pin(certificate.to_cryptography()) is not None


def open_server_context(identity: IdentityFiles) -> SSL.Context:
    """A server context that presents this daemon's identity and accepts any client
    certificate ``accepted_pin`` accepts. Raises ``FederationError`` when the identity
    does not load, is not Ed25519, or does not match its pin."""
    try:
        certificate = x509.load_pem_x509_certificate(
            read_certificate(identity.certificate)
        )
        key = serialization.load_pem_private_key(
            read_store_key(identity.private_key), password=None
        )
    except (ValueError, TypeError) as error:
        raise FederationError(
            f"this daemon's identity does not load: {error}"
        ) from error
    if not isinstance(key, Ed25519PrivateKey):
        raise FederationError("this daemon's identity key is not Ed25519")
    pin = accepted_pin(certificate)
    if pin is None or not hmac.compare_digest(pin, identity.pin_sha256):
        raise FederationError("this daemon's certificate is not its pinned identity")
    context = SSL.Context(SSL.TLS_METHOD)
    context.set_min_proto_version(SSL.TLS1_3_VERSION)
    context.set_max_proto_version(SSL.TLS1_3_VERSION)
    context.set_options(SSL.OP_NO_TICKET)
    context.set_session_cache_mode(SSL.SESS_CACHE_OFF)
    try:
        context.use_certificate(certificate)
        context.use_privatekey(key)
        context.check_privatekey()
    except SSL.Error as error:
        raise FederationError(f"this daemon's identity is unusable: {error}") from error
    context.set_verify(SSL.VERIFY_PEER | SSL.VERIFY_FAIL_IF_NO_PEER_CERT, _verify)
    return context


class OpenTlsStream:
    """One accepted connection's TLS session, driven over memory BIOs from the
    connection's own stream. It reads and writes as ``asyncio`` streams do, so a
    connection's server serves it as it serves any other.

    Bounds. Each read takes at most ``CHUNK_BYTES`` of ciphertext, and plaintext is
    taken only while a line is incomplete and below ``limit``, so a peer cannot make
    the daemon buffer more than ``limit`` plus a chunk. Each loop either returns,
    raises, or consumes bytes the peer sent, and the caller's deadline ends a peer that
    sends nothing."""

    def __init__(
        self,
        context: SSL.Context,
        reader: RawReader,
        writer: RawWriter,
        limit: int,
    ) -> None:
        if limit < 1:
            raise ValueError(f"a line limit must be at least 1, not {limit}")
        self._tls = SSL.Connection(context, None)
        self._tls.set_accept_state()
        self._reader = reader
        self._writer = writer
        self._limit = limit
        self._buffer = bytearray()
        self._ended = False

    async def handshake(self) -> str:
        """Finish the server's handshake and return the client key's pin. Raises
        ``OpenTlsError``. The caller bounds it with ``asyncio.wait_for``."""
        while True:
            try:
                self._tls.do_handshake()
            except SSL.WantReadError:
                self._flush()
                if not await self._take():
                    raise OpenTlsError(
                        "the client closed inside the handshake"
                    ) from None
                continue
            except SSL.Error as error:
                self._flush()
                raise OpenTlsError(f"the handshake failed: {error}") from error
            self._flush()
            break
        presented = self._tls.get_peer_certificate(as_cryptography=True)
        pin = None if presented is None else accepted_pin(presented)
        if pin is None:
            raise OpenTlsError(
                "the client presented no certificate this daemon accepts"
            )
        return pin

    async def readline(self) -> bytes:
        """The next line with its newline, or what remains at the end, which is empty or
        lacks the newline. Raises ``ValueError`` past the limit, as a stream does."""
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0:
                if end + 1 > self._limit:
                    raise ValueError(f"the line exceeds {self._limit} bytes")
                line = bytes(self._buffer[: end + 1])
                del self._buffer[: end + 1]
                return line
            if len(self._buffer) >= self._limit:
                raise ValueError(f"the line exceeds {self._limit} bytes")
            if not await self._fill():
                rest = bytes(self._buffer)
                self._buffer.clear()
                return rest

    async def read(self, n: int = -1) -> bytes:
        """At most ``n`` bytes, or at most a chunk when ``n`` is negative, and empty at
        the end."""
        most = CHUNK_BYTES if n < 0 else n
        if not self._buffer and most > 0 and not await self._fill():
            return b""
        taken = bytes(self._buffer[:most])
        del self._buffer[:most]
        return taken

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        try:
            while view:
                sent = self._tls.send(view[:CHUNK_BYTES].tobytes())
                view = view[sent:]
                self._flush()
        except SSL.Error as error:
            raise OpenTlsError(f"a record could not be written: {error}") from error

    async def drain(self) -> None:
        await self._writer.drain()

    def close(self) -> None:
        if not self._ended:
            self._ended = True
            try:
                self._tls.shutdown()
                self._flush()
            except SSL.Error:
                pass
        self._writer.close()

    async def wait_closed(self) -> None:
        await self._writer.wait_closed()

    async def _fill(self) -> bool:
        """Take more plaintext into the buffer, and whether any came before the end."""
        while True:
            try:
                data = self._tls.recv(CHUNK_BYTES)
            except SSL.WantReadError:
                self._flush()
                if not await self._take():
                    return False
                continue
            except SSL.ZeroReturnError:
                return False
            except SSL.Error as error:
                raise OpenTlsError(f"a record could not be read: {error}") from error
            self._flush()
            if not data:
                return False
            self._buffer += data
            return True

    async def _take(self) -> bool:
        """Feed the session the next ciphertext, and whether any came."""
        data = await self._reader.read(CHUNK_BYTES)
        if not data:
            return False
        self._tls.bio_write(data)
        return True

    def _flush(self) -> None:
        """Write out whatever ciphertext the session has made. Bounded, since each
        pass drains the session's outgoing buffer by up to a chunk."""
        while True:
            try:
                data = self._tls.bio_read(CHUNK_BYTES)
            except SSL.WantReadError:
                return
            if not data:
                return
            self._writer.write(data)
