"""Pinned TLS transport primitives for federation between Dromon daemons.

This module has no dependency on the federation event model or daemon. The delivery
layer owns addresses, policy, JSON validation and idempotency. This module owns peer
identity files, TLS 1.3 authentication and bounded newline framing.

Each daemon uses one self-signed Ed25519 certificate for both client and server
authentication. A TLS context trusts exactly the configured peer certificate, requires
the other side to present it during the handshake, and then checks the SHA-256 digest
of its SubjectPublicKeyInfo before returning the connection. Certificate names are
not identities. Session tickets are disabled so there is no resumable handshake path
for early data. Python's ssl module exposes no API to enable early data.
"""

import hashlib
import hmac
import os
import re
import socket
import ssl
import stat
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

MAX_FRAME_BYTES: Final = 8_388_608
"""Maximum size of a complete JSON line, including its terminating line feed."""
FRAME_READ_BYTES: Final = 4096
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

    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise TransportError(f"cannot create identity directory {directory}") from error

    directory_fd = _open_private_directory(directory)
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
    connection: socket.socket, context: ssl.SSLContext, peer_pin_sha256: str
) -> ssl.SSLSocket:
    """Complete a mutually authenticated client handshake and check the peer pin."""
    _check_pin_syntax(peer_pin_sha256)
    try:
        wrapped = context.wrap_socket(connection, server_side=False)
    except (OSError, ssl.SSLError):
        connection.close()
        raise
    return _check_wrapped_pin(wrapped, peer_pin_sha256)


def wrap_server(
    connection: socket.socket, context: ssl.SSLContext, peer_pin_sha256: str
) -> ssl.SSLSocket:
    """Complete a mutually authenticated server handshake and check the peer pin."""
    _check_pin_syntax(peer_pin_sha256)
    try:
        wrapped = context.wrap_socket(connection, server_side=True)
    except (OSError, ssl.SSLError):
        connection.close()
        raise
    return _check_wrapped_pin(wrapped, peer_pin_sha256)


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


def send_frame(connection: socket.socket, frame: bytes) -> None:
    """Send one complete line-delimited frame after checking its size and boundary."""
    if not frame or len(frame) > MAX_FRAME_BYTES:
        raise OversizedFrame(f"a frame must be 1 to {MAX_FRAME_BYTES} bytes")
    if frame[-1:] != b"\n" or b"\n" in frame[:-1]:
        raise FrameError("a frame must contain exactly one final line feed")
    connection.sendall(frame)


class FrameReader:
    """Read complete bounded lines without consuming bytes from the next frame."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._buffer = bytearray()

    def read_frame(self) -> bytes | None:
        """Return one line, ``None`` at a clean EOF, or raise for malformed framing."""
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0:
                frame_size = end + 1
                if frame_size > MAX_FRAME_BYTES:
                    raise OversizedFrame(f"a frame exceeds {MAX_FRAME_BYTES} bytes")
                frame = bytes(self._buffer[:frame_size])
                del self._buffer[:frame_size]
                return frame

            if len(self._buffer) >= MAX_FRAME_BYTES:
                raise OversizedFrame(f"a frame exceeds {MAX_FRAME_BYTES} bytes")

            remaining = MAX_FRAME_BYTES - len(self._buffer)
            chunk = self._connection.recv(min(FRAME_READ_BYTES, remaining))
            if not chunk:
                if self._buffer:
                    raise TruncatedFrame("the connection ended inside a frame")
                return None
            self._buffer.extend(chunk)


def _context(
    role: Literal["client", "server"],
    identity: IdentityFiles,
    peer_certificate_path: Path,
) -> ssl.SSLContext:
    _check_private_file(identity.private_key)
    _check_private_file(identity.certificate)
    _check_private_file(peer_certificate_path)
    local_certificate = _load_one_certificate(identity.certificate)
    peer_certificate = _load_one_certificate(peer_certificate_path)
    _validate_self_signed(local_certificate, identity.private_key)
    _validate_self_signed(peer_certificate)
    if not hmac.compare_digest(public_key_pin(local_certificate), identity.pin_sha256):
        raise TransportError("the local identity pin does not match its certificate")

    protocol = ssl.PROTOCOL_TLS_CLIENT if role == "client" else ssl.PROTOCOL_TLS_SERVER
    context = ssl.SSLContext(protocol)
    context.minimum_version = _TLS_VERSION
    context.maximum_version = _TLS_VERSION
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = False
    context.load_cert_chain(
        certfile=str(identity.certificate), keyfile=str(identity.private_key)
    )
    context.load_verify_locations(cafile=str(peer_certificate_path))
    context.options |= ssl.OP_NO_TICKET
    if role == "server":
        context.num_tickets = 0
    return context


def _self_signed_certificate(private_key: Ed25519PrivateKey) -> x509.Certificate:
    now = datetime.now(UTC)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Dromon federation peer")]
    )
    return (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
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
    certificate: x509.Certificate, private_key_path: Path | None = None
) -> None:
    if certificate.subject != certificate.issuer:
        raise TransportError("a peer certificate must be self-signed")
    try:
        certificate.verify_directly_issued_by(certificate)
    except (InvalidSignature, ValueError) as error:
        raise TransportError(
            "a peer certificate has an invalid self-signature"
        ) from error
    if private_key_path is not None:
        key = _load_private_key(private_key_path)
        expected = key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        actual = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if not hmac.compare_digest(expected, actual):
            raise TransportError("the identity key does not match its certificate")


def _load_private_key(path: Path) -> Ed25519PrivateKey:
    try:
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as error:
        raise TransportError(f"cannot read private key {path}") from error
    if not isinstance(key, Ed25519PrivateKey):
        raise TransportError("the identity key must be Ed25519")
    return key


def _load_one_certificate(path: Path) -> x509.Certificate:
    try:
        pem = path.read_bytes()
        certificates = x509.load_pem_x509_certificates(pem)
    except (OSError, ValueError) as error:
        raise TransportError(f"cannot read certificate {path}") from error
    if len(certificates) != 1:
        raise TransportError("a peer certificate file must contain exactly one cert")
    return certificates[0]


def _check_private_file(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise TransportError(f"cannot inspect identity file {path}") from error
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise TransportError(
            f"identity file {path} must be owned by this user and "
            "inaccessible to group and others"
        )


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
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        os.close(descriptor)
        raise TransportError(
            f"identity directory {path} must be owned by this user and "
            "inaccessible to group and others"
        )
    return descriptor


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


def _check_wrapped_pin(connection: ssl.SSLSocket, expected_pin: str) -> ssl.SSLSocket:
    try:
        verify_peer_pin(connection, expected_pin)
    except PeerPinError:
        connection.close()
        raise
    return connection
