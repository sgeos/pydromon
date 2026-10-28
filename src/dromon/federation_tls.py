"""Pinned TLS transport primitives for federation between Dromon daemons.

This module has no dependency on the federation event model or daemon. The delivery
layer owns addresses, policy, JSON validation and idempotency. This module owns peer
identity files, TLS 1.3 authentication and bounded newline framing.

Each daemon uses one self-signed Ed25519 certificate for both client and server
authentication. A TLS context trusts exactly the configured peer certificate, requires
the other side to present it during the handshake, and then checks the SHA-256 digest
of its SubjectPublicKeyInfo before returning the connection. Certificate names are
not identities. Session tickets are disabled so there is no resumable handshake path
for early data. Python's ssl module exposes no API to enable early data. With TLS 1.3,
the client-side handshake can finish before the server reports that it rejected the
client certificate. A returned client connection is not proof of server acceptance;
the caller must wait for the server's first application response. A read may instead
receive the authentication alert or closed connection.

The identity is Ed25519-only. Both current Python federation peers use the same
``cryptography`` dependency, so a P-256 fallback adds an unneeded algorithm path. The
listener, not this module, must cap concurrent unauthenticated connections at the
design's limit of eight. TLS handshakes have a ten-second absolute deadline, and each
frame has a thirty-second absolute deadline. Callers can share a connection deadline
from ``new_connection_deadline`` across handshake, first response and framing.
"""

import hashlib
import hmac
import math
import os
import re
import selectors
import socket
import ssl
import stat
import time
from contextlib import ExitStack, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO, Final, Literal

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

MAX_FRAME_BYTES: Final = 8_388_608
"""Maximum size of a complete JSON line, including its terminating line feed."""
FRAME_READ_BYTES: Final = 4096
DEFAULT_FRAME_TIMEOUT_SECONDS: Final = 30.0
"""The documented socket idle timeout, applied as an absolute deadline per frame."""
TLS_HANDSHAKE_TIMEOUT_SECONDS: Final = 10.0
"""The proposed 10-second handshake and pre-hello deadline in FEDERATION.md."""
MAX_IDENTITY_FILE_BYTES: Final = 65536
KEY_FILENAME: Final = "identity-key.pem"
CERTIFICATE_FILENAME: Final = "identity-certificate.pem"
CERTIFICATE_VALIDITY_DAYS: Final = 3650
_PIN_PATTERN: Final = re.compile(r"[0-9a-f]{64}\Z")
_TLS_VERSION: Final = ssl.TLSVersion.TLSv1_3


class TransportError(ValueError):
    """A local transport configuration or frame is invalid."""


class PeerPinError(TransportError):
    """The certificate presented by the peer does not have the configured pin."""


class FrameError(TransportError):
    """A peer sent an invalid or incomplete frame."""


class OversizedFrame(FrameError):
    """A frame exceeds ``MAX_FRAME_BYTES``."""


class TruncatedFrame(FrameError):
    """The connection ended before a frame's line feed arrived."""


class FrameTimeout(FrameError):
    """A complete frame was not received before its absolute deadline."""


class HandshakeTimeout(TransportError):
    """The TLS handshake did not finish before its absolute deadline."""


class PeerClosed(TransportError):
    """The peer closed before sending the expected first response."""


@dataclass(frozen=True, slots=True)
class IdentityFiles:
    """An owner's private key, self-signed certificate and public-key pin."""

    private_key: Path
    certificate: Path
    pin_sha256: str


def generate_identity(directory: Path) -> IdentityFiles:
    """Create a private Ed25519 identity in a new, owner-only directory.

    Existing identities are never overwritten. The directory is mode 0700 and each
    file is mode 0600. The returned pin is the lowercase SHA-256 digest of the
    certificate's DER SubjectPublicKeyInfo.
    """
    private_key = Ed25519PrivateKey.generate()
    certificate = _self_signed_certificate(private_key)
    key_bytes = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    certificate_bytes = certificate.public_bytes(serialization.Encoding.PEM)

    directory_fd = _ensure_private_directory(directory)
    key_created = False
    try:
        _create_private_file(directory_fd, KEY_FILENAME, key_bytes)
        key_created = True
        _create_private_file(directory_fd, CERTIFICATE_FILENAME, certificate_bytes)
    except OSError as error:
        if key_created:
            with suppress(OSError):
                os.unlink(KEY_FILENAME, dir_fd=directory_fd)
        raise TransportError(f"cannot create identity files in {directory}") from error
    finally:
        os.close(directory_fd)

    cert_path = directory / CERTIFICATE_FILENAME
    key_path = directory / KEY_FILENAME
    return IdentityFiles(key_path, cert_path, public_key_pin(certificate))


def public_key_pin(certificate: x509.Certificate) -> str:
    """Return a certificate's lowercase SHA-256 SubjectPublicKeyInfo digest."""
    public_key = certificate.public_key()
    encoded = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return hashlib.sha256(encoded).hexdigest()


def client_context(identity: IdentityFiles, peer_certificate: Path) -> ssl.SSLContext:
    """Build a client context that requires and trusts only ``peer_certificate``."""
    return _context("client", identity, peer_certificate)


def server_context(identity: IdentityFiles, peer_certificate: Path) -> ssl.SSLContext:
    """Build a server context that requires and trusts only ``peer_certificate``."""
    return _context("server", identity, peer_certificate)


def wrap_client(
    connection: socket.socket,
    context: ssl.SSLContext,
    peer_pin_sha256: str,
    *,
    connection_deadline: float | None = None,
) -> ssl.SSLSocket:
    """Complete the client handshake and check the server pin.

    On TLS 1.3, the server may report client-certificate rejection after this call
    returns. Callers must wait for the first application response and treat a TLS alert
    or closed connection as rejection.
    """
    return _wrap_tls(
        connection,
        context,
        peer_pin_sha256,
        server_side=False,
        connection_deadline=connection_deadline,
    )


def wrap_server(
    connection: socket.socket,
    context: ssl.SSLContext,
    peer_pin_sha256: str,
    *,
    connection_deadline: float | None = None,
) -> ssl.SSLSocket:
    """Complete a mutually authenticated server handshake and check the peer pin."""
    return _wrap_tls(
        connection,
        context,
        peer_pin_sha256,
        server_side=True,
        connection_deadline=connection_deadline,
    )


def verify_peer_pin(
    connection: ssl.SSLSocket | ssl.SSLObject, peer_pin_sha256: str
) -> None:
    """Compare a completed TLS peer certificate with its configured public-key pin."""
    _check_pin_syntax(peer_pin_sha256)
    certificate_der = connection.getpeercert(binary_form=True)
    if not certificate_der:
        raise PeerPinError("the peer did not present a certificate")
    try:
        certificate = x509.load_der_x509_certificate(certificate_der)
    except ValueError as error:
        raise PeerPinError("the peer presented an invalid certificate") from error
    actual_pin = public_key_pin(certificate)
    if not hmac.compare_digest(actual_pin, peer_pin_sha256):
        raise PeerPinError("the peer certificate does not match its configured pin")


def send_frame(
    connection: socket.socket,
    frame: bytes,
    *,
    frame_timeout_seconds: float = DEFAULT_FRAME_TIMEOUT_SECONDS,
    connection_deadline: float | None = None,
) -> None:
    """Send one complete bounded frame before its frame or connection deadline."""
    previous_timeout: float | None = None
    timeout_saved = False
    try:
        if not math.isfinite(frame_timeout_seconds) or frame_timeout_seconds <= 0:
            raise ValueError("frame_timeout_seconds must be finite and positive")
        if connection_deadline is not None and not math.isfinite(connection_deadline):
            raise ValueError("connection_deadline must be finite")
        previous_timeout = connection.gettimeout()
        timeout_saved = True
        if not frame or len(frame) > MAX_FRAME_BYTES:
            raise OversizedFrame(f"a frame must be 1 to {MAX_FRAME_BYTES} bytes")
        if frame[-1:] != b"\n" or b"\n" in frame[:-1]:
            raise FrameError("a frame must contain exactly one final line feed")
        deadline = _absolute_deadline(frame_timeout_seconds, connection_deadline)
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise FrameTimeout("the frame deadline expired")
        timeout = (
            remaining_time
            if previous_timeout is None
            else min(remaining_time, previous_timeout)
        )
        connection.settimeout(timeout)
        connection.sendall(frame)
    except BaseException:
        _close_safely(connection)
        raise
    finally:
        if timeout_saved:
            with suppress(OSError):
                connection.settimeout(previous_timeout)


class FrameReader:
    """Read complete bounded lines under absolute per-frame deadlines.

    The scan cursor advances as new bytes arrive, so an incomplete line is scanned
    once rather than from its beginning after every read.
    """

    def __init__(
        self,
        connection: socket.socket,
        *,
        frame_timeout_seconds: float = DEFAULT_FRAME_TIMEOUT_SECONDS,
        connection_deadline: float | None = None,
    ) -> None:
        try:
            if not math.isfinite(frame_timeout_seconds) or frame_timeout_seconds <= 0:
                raise ValueError("frame_timeout_seconds must be finite and positive")
            if connection_deadline is not None and not math.isfinite(
                connection_deadline
            ):
                raise ValueError("connection_deadline must be finite")
        except BaseException:
            _close_safely(connection)
            raise
        self._connection = connection
        self._buffer = bytearray()
        self._scan_offset = 0
        self._scan_bytes = 0
        self._frame_timeout_seconds = frame_timeout_seconds
        self._connection_deadline = connection_deadline

    def read_frame(self) -> bytes | None:
        """Return one line, ``None`` at a clean EOF, or raise for malformed framing."""
        previous_timeout: float | None = None
        timeout_saved = False
        try:
            deadline = _absolute_deadline(
                self._frame_timeout_seconds, self._connection_deadline
            )
            previous_timeout = self._connection.gettimeout()
            timeout_saved = True
            while True:
                remaining_time = deadline - time.monotonic()
                if remaining_time <= 0:
                    raise FrameTimeout("the frame deadline expired")
                timeout = (
                    remaining_time
                    if previous_timeout is None
                    else min(remaining_time, previous_timeout)
                )
                self._connection.settimeout(timeout)

                end = self._buffer.find(b"\n", self._scan_offset)
                if end >= 0:
                    frame_size = end + 1
                    self._scan_bytes += frame_size - self._scan_offset
                    self._scan_offset = frame_size
                    if frame_size > MAX_FRAME_BYTES:
                        raise OversizedFrame(f"a frame exceeds {MAX_FRAME_BYTES} bytes")
                    frame = bytes(self._buffer[:frame_size])
                    del self._buffer[:frame_size]
                    self._scan_offset -= frame_size
                    return frame

                self._scan_bytes += len(self._buffer) - self._scan_offset
                self._scan_offset = len(self._buffer)
                if len(self._buffer) >= MAX_FRAME_BYTES:
                    raise OversizedFrame(f"a frame exceeds {MAX_FRAME_BYTES} bytes")

                remaining_bytes = MAX_FRAME_BYTES - len(self._buffer)
                chunk = self._connection.recv(min(FRAME_READ_BYTES, remaining_bytes))
                if not chunk:
                    if self._buffer:
                        raise TruncatedFrame("the connection ended inside a frame")
                    return None
                self._buffer.extend(chunk)
        except BaseException:
            _close_safely(self._connection)
            raise
        finally:
            if timeout_saved:
                with suppress(OSError):
                    self._connection.settimeout(previous_timeout)

    def close(self) -> None:
        """Close the transport when the caller rejects or abandons the connection."""
        _close_safely(self._connection)

    @property
    def bytes_scanned(self) -> int:
        """Number of buffered bytes searched for line boundaries."""
        return self._scan_bytes


def read_first_response(reader: FrameReader) -> bytes:
    """Read the first server response after the client's ``hello`` frame.

    The caller must validate that the response accepts the hello. Do not send or record
    a delivery before this first response has been read and validated. A delivery is not
    accepted until its own response is read and validated. This read also observes a
    TLS 1.3 alert sent after the client's handshake completed.
    """
    response = reader.read_frame()
    if response is None:
        reader.close()
        raise PeerClosed("the peer closed before its first application response")
    return response


def _context(
    role: Literal["client", "server"],
    identity: IdentityFiles,
    peer_certificate_path: Path,
) -> ssl.SSLContext:
    with ExitStack() as files:
        key_file = files.enter_context(_open_checked_file(identity.private_key))
        local_certificate_file = files.enter_context(
            _open_checked_file(identity.certificate)
        )
        peer_certificate_file = files.enter_context(
            _open_checked_file(peer_certificate_path)
        )
        key_bytes = _read_bounded_file(key_file, identity.private_key)
        local_certificate_bytes = _read_bounded_file(
            local_certificate_file, identity.certificate
        )
        peer_certificate_bytes = _read_bounded_file(
            peer_certificate_file, peer_certificate_path
        )
        key_file.seek(0)
        local_certificate_file.seek(0)
        local_certificate = _load_one_certificate(
            local_certificate_bytes, identity.certificate
        )
        peer_certificate = _load_one_certificate(
            peer_certificate_bytes, peer_certificate_path
        )
        private_key = _load_private_key(key_bytes, identity.private_key)
        _validate_self_signed(local_certificate, private_key)
        _validate_self_signed(peer_certificate)
        if not hmac.compare_digest(
            public_key_pin(local_certificate), identity.pin_sha256
        ):
            raise TransportError(
                "the local identity pin does not match its certificate"
            )

        protocol = (
            ssl.PROTOCOL_TLS_CLIENT if role == "client" else ssl.PROTOCOL_TLS_SERVER
        )
        context = ssl.SSLContext(protocol)
        context.minimum_version = _TLS_VERSION
        context.maximum_version = _TLS_VERSION
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = False
        context.load_cert_chain(
            certfile=_descriptor_path(local_certificate_file.fileno()),
            keyfile=_descriptor_path(key_file.fileno()),
        )
        try:
            peer_certificate_pem = peer_certificate_bytes.decode("ascii")
            context.load_verify_locations(cadata=peer_certificate_pem)
        except (UnicodeDecodeError, ssl.SSLError) as error:
            raise TransportError("cannot load the pinned peer certificate") from error
        context.options |= ssl.OP_NO_TICKET
        if role == "server":
            context.num_tickets = 0
        return context


def _wrap_tls(
    connection: socket.socket,
    context: ssl.SSLContext,
    peer_pin_sha256: str,
    *,
    server_side: bool,
    connection_deadline: float | None,
) -> ssl.SSLSocket:
    wrapped: ssl.SSLSocket | None = None
    try:
        _check_pin_syntax(peer_pin_sha256)
        if connection_deadline is not None and not math.isfinite(connection_deadline):
            raise ValueError("connection_deadline must be finite")
        previous_timeout = connection.gettimeout()
        deadline = _absolute_deadline(
            TLS_HANDSHAKE_TIMEOUT_SECONDS, connection_deadline
        )
        connection.setblocking(False)
        wrapped = context.wrap_socket(
            connection,
            server_side=server_side,
            do_handshake_on_connect=False,
        )
        wrapped.setblocking(False)
        with selectors.DefaultSelector() as selector:
            selector.register(wrapped, selectors.EVENT_READ | selectors.EVENT_WRITE)
            while True:
                try:
                    wrapped.do_handshake()
                    if time.monotonic() >= deadline:
                        raise HandshakeTimeout("the TLS handshake deadline expired")
                    break
                except ssl.SSLWantReadError:
                    selector.modify(wrapped, selectors.EVENT_READ)
                except ssl.SSLWantWriteError:
                    selector.modify(wrapped, selectors.EVENT_WRITE)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HandshakeTimeout("the TLS handshake deadline expired")
                selector.select(remaining)
                if time.monotonic() >= deadline:
                    raise HandshakeTimeout("the TLS handshake deadline expired")
        verify_peer_pin(wrapped, peer_pin_sha256)
        wrapped.settimeout(previous_timeout)
        return wrapped
    except BaseException:
        if wrapped is None:
            _close_safely(connection)
        else:
            _close_safely(wrapped)
        raise


def _self_signed_certificate(private_key: Ed25519PrivateKey) -> x509.Certificate:
    now = datetime.now(UTC)
    public_key = private_key.public_key()
    public_key_der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    subject_pin = hashlib.sha256(public_key_der).hexdigest()
    subject = x509.Name(
        [
            x509.NameAttribute(
                NameOID.COMMON_NAME,
                f"Dromon federation peer {subject_pin}",
            )
        ]
    )
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=CERTIFICATE_VALIDITY_DAYS))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]
            ),
            critical=False,
        )
        .sign(private_key, algorithm=None)
    )


def _validate_self_signed(
    certificate: x509.Certificate, private_key: Ed25519PrivateKey | None = None
) -> None:
    if certificate.subject != certificate.issuer:
        raise TransportError("a peer certificate must be self-signed")
    try:
        certificate.verify_directly_issued_by(certificate)
    except (InvalidSignature, ValueError) as error:
        raise TransportError(
            "a peer certificate has an invalid self-signature"
        ) from error
    if private_key is not None:
        expected = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        actual = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if not hmac.compare_digest(expected, actual):
            raise TransportError("the identity key does not match its certificate")


def _load_private_key(data: bytes, path: Path) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as error:
        raise TransportError(f"cannot read private key {path}") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise TransportError("the identity key must be Ed25519")
    return key


def _load_one_certificate(data: bytes, path: Path) -> x509.Certificate:
    try:
        certificates = x509.load_pem_x509_certificates(data)
    except ValueError as error:
        raise TransportError(f"cannot read certificate {path}") from error
    if len(certificates) != 1:
        raise TransportError("a peer certificate file must contain exactly one cert")
    return certificates[0]


def _open_checked_file(path: Path) -> BinaryIO:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TransportError(f"cannot safely open identity file {path}") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise TransportError(
                f"identity file {path} must be owned by this user and "
                "inaccessible to group and others"
            )
        return os.fdopen(descriptor, "rb")
    except BaseException:
        with suppress(OSError):
            os.close(descriptor)
        raise


def _read_bounded_file(file: BinaryIO, path: Path) -> bytes:
    data = file.read(MAX_IDENTITY_FILE_BYTES + 1)
    if len(data) > MAX_IDENTITY_FILE_BYTES:
        raise TransportError(f"identity file {path} exceeds the size limit")
    return data


def _descriptor_path(descriptor: int) -> str:
    for root in ("/dev/fd", "/proc/self/fd"):
        if os.path.isdir(root):
            return f"{root}/{descriptor}"
    raise TransportError("the platform has no file-descriptor path for TLS key loading")


def _open_private_directory(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise TransportError(f"cannot safely open identity directory {path}") from error
    info = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        os.close(descriptor)
        raise TransportError(
            f"identity directory {path} must be owned by this user with mode 700"
        )
    return descriptor


def _ensure_private_directory(path: Path) -> int:
    missing: list[Path] = []
    current = path
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            if current.parent == current:
                raise TransportError(f"cannot find a parent for {path}") from None
            current = current.parent
            continue
        except OSError as error:
            raise TransportError(f"cannot inspect identity path {current}") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise TransportError(
                f"identity path component {current} is not a directory"
            )
        break

    for directory in reversed(missing):
        with suppress(FileExistsError):
            directory.mkdir(mode=0o700)
        descriptor = _open_private_directory(directory)
        os.close(descriptor)
    return _open_private_directory(path)


def _create_private_file(directory_fd: int, name: str, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        try:
            os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise OSError("identity file is not a regular file owned by this user")
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("identity file write made no progress")
                view = view[written:]
            os.fsync(descriptor)
        except BaseException:
            with suppress(OSError):
                os.unlink(name, dir_fd=directory_fd)
            raise
    finally:
        os.close(descriptor)


def _check_pin_syntax(pin: str) -> None:
    if _PIN_PATTERN.fullmatch(pin) is None:
        raise TransportError("a peer pin must be 64 lowercase hexadecimal characters")


def _absolute_deadline(
    timeout_seconds: float, connection_deadline: float | None
) -> float:
    deadline = time.monotonic() + timeout_seconds
    if connection_deadline is not None:
        deadline = min(deadline, connection_deadline)
    return deadline


def new_connection_deadline(
    timeout_seconds: float = TLS_HANDSHAKE_TIMEOUT_SECONDS,
) -> float:
    """Return an absolute monotonic connection deadline, ten seconds by default.

    Pass the same value to TLS wrapping, ``FrameReader`` and ``send_frame`` to bound
    connection setup and its first application exchange as one operation.
    """
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    return time.monotonic() + timeout_seconds


def _close_safely(connection: socket.socket) -> None:
    with suppress(OSError):
        connection.close()
