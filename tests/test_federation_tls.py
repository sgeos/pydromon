"""Pinned TLS identity and bounded federation framing."""

import socket
import ssl
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final, cast

import pytest
from cryptography import x509
from cryptography.x509.oid import NameOID

from dromon.federation_tls import (
    CERTIFICATE_FILENAME,
    KEY_FILENAME,
    MAX_PEER_CERTIFICATES,
    FrameError,
    FrameTimeout,
    FrameReader,
    HandshakeTimeout,
    OversizedFrame,
    PeerClosed,
    PeerPinError,
    TruncatedFrame,
    TransportError,
    client_context,
    generate_identity,
    new_connection_deadline,
    read_first_response,
    send_frame,
    server_context,
    verify_peer_pin,
    wrap_client,
    wrap_server,
)

TIMEOUT_SECONDS: Final = 3.0


def test_generated_identity_files_are_owner_only_and_pin_its_public_key(
    tmp_path: Path,
) -> None:
    identity = generate_identity(tmp_path / "peer")

    assert identity.private_key.name == KEY_FILENAME
    assert identity.certificate.name == CERTIFICATE_FILENAME
    assert identity.private_key.stat().st_mode & 0o777 == 0o600
    assert identity.certificate.stat().st_mode & 0o777 == 0o600
    assert identity.private_key.parent.stat().st_mode & 0o777 == 0o700
    assert len(identity.pin_sha256) == 64
    assert identity.pin_sha256 == identity.pin_sha256.lower()


def test_generated_certificates_have_distinct_subjects_bound_to_their_pins(
    tmp_path: Path,
) -> None:
    first = generate_identity(tmp_path / "first")
    second = generate_identity(tmp_path / "second")
    first_certificate = x509.load_pem_x509_certificate(first.certificate.read_bytes())
    second_certificate = x509.load_pem_x509_certificate(second.certificate.read_bytes())

    assert first_certificate.subject != second_certificate.subject
    assert first_certificate.subject == first_certificate.issuer
    assert second_certificate.subject == second_certificate.issuer
    assert [
        attribute.value
        for attribute in first_certificate.subject.get_attributes_for_oid(
            NameOID.ORGANIZATION_NAME
        )
    ] == ["Dromon federation peer"]
    assert [
        attribute.value
        for attribute in second_certificate.subject.get_attributes_for_oid(
            NameOID.COMMON_NAME
        )
    ] == [second.pin_sha256]
    assert [
        attribute.value
        for attribute in first_certificate.subject.get_attributes_for_oid(
            NameOID.COMMON_NAME
        )
    ] == [first.pin_sha256]
    assert [
        attribute.value
        for attribute in second_certificate.subject.get_attributes_for_oid(
            NameOID.ORGANIZATION_NAME
        )
    ] == ["Dromon federation peer"]
    for identity, certificate in (
        (first, first_certificate),
        (second, second_certificate),
    ):
        subject_key_identifier = certificate.extensions.get_extension_for_class(
            x509.SubjectKeyIdentifier
        ).value
        assert subject_key_identifier == x509.SubjectKeyIdentifier.from_public_key(
            certificate.public_key()
        )
        assert (
            certificate.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value
            == identity.pin_sha256
        )


def test_identity_generation_does_not_replace_existing_files(tmp_path: Path) -> None:
    directory = tmp_path / "peer"
    original = generate_identity(directory)
    original_key = original.private_key.read_bytes()

    with pytest.raises(TransportError, match="cannot create identity files"):
        generate_identity(directory)

    assert original.private_key.read_bytes() == original_key
    assert original.certificate.is_file()


def test_every_directory_created_for_an_identity_is_private(tmp_path: Path) -> None:
    identity_dir = tmp_path / "new-parent" / "nested" / "peer"

    generate_identity(identity_dir)

    for directory in (
        identity_dir,
        identity_dir.parent,
        identity_dir.parent.parent,
    ):
        assert directory.stat().st_mode & 0o777 == 0o700


def test_identity_key_is_opened_without_following_a_replaced_symlink(
    tmp_path: Path,
) -> None:
    identity = generate_identity(tmp_path / "local")
    attacker = generate_identity(tmp_path / "attacker")
    peer = generate_identity(tmp_path / "peer")
    identity.private_key.unlink()
    identity.private_key.symlink_to(attacker.private_key)

    with pytest.raises(TransportError, match="safely open identity file"):
        client_context(identity, peer.certificate)


def test_tls_contexts_use_only_tls_13_and_disable_session_tickets(
    tmp_path: Path,
) -> None:
    local = generate_identity(tmp_path / "local")
    peer = generate_identity(tmp_path / "peer")
    client = client_context(local, peer.certificate)
    server = server_context(peer, local.certificate)

    for context in (client, server):
        assert context.minimum_version is ssl.TLSVersion.TLSv1_3
        assert context.maximum_version is ssl.TLSVersion.TLSv1_3
        assert context.options & ssl.OP_NO_TICKET
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is False
    assert server.num_tickets == 0


def test_python_ssl_pins_self_signed_peer_certificates_and_exchanges_frames(
    tmp_path: Path,
) -> None:
    local = generate_identity(tmp_path / "local")
    peer = generate_identity(tmp_path / "peer")
    client_context_value = client_context(local, peer.certificate)
    server_context_value = server_context(peer, local.certificate)
    tls = _memory_handshake(client_context_value, server_context_value)

    verify_peer_pin(tls.client, peer.pin_sha256)
    verify_peer_pin(tls.server, local.pin_sha256)
    assert tls.client.session is not None
    assert not tls.client.session.has_ticket
    assert not tls.client.session_reused
    assert not tls.server.session_reused
    frame = b'{"operation":"deliver"}\n'
    assert tls.client.write(frame) == len(frame)
    tls.transfer_client_to_server()
    assert tls.server.read(len(frame)) == frame


def test_python_ssl_rejects_a_wrong_pin_after_the_handshake(tmp_path: Path) -> None:
    local = generate_identity(tmp_path / "local")
    peer = generate_identity(tmp_path / "peer")
    tls = _memory_handshake(
        client_context(local, peer.certificate), server_context(peer, local.certificate)
    )

    with pytest.raises(PeerPinError, match="configured pin"):
        verify_peer_pin(tls.client, local.pin_sha256)


def test_python_ssl_trusts_only_the_configured_peer_certificate(
    tmp_path: Path,
) -> None:
    local = generate_identity(tmp_path / "local")
    peer = generate_identity(tmp_path / "peer")
    untrusted = generate_identity(tmp_path / "untrusted")

    with pytest.raises(ssl.SSLError):
        _memory_handshake(
            client_context(local, peer.certificate),
            server_context(untrusted, local.certificate),
        )


def test_one_server_context_can_trust_two_peer_certificates(tmp_path: Path) -> None:
    server_identity = generate_identity(tmp_path / "server")
    first_peer = generate_identity(tmp_path / "first-peer")
    second_peer = generate_identity(tmp_path / "second-peer")
    server = server_context(
        server_identity, [first_peer.certificate, second_peer.certificate]
    )

    for peer in (first_peer, second_peer):
        tls = _memory_handshake(
            client_context(peer, server_identity.certificate), server
        )
        verify_peer_pin(tls.server, peer.pin_sha256)


def test_a_context_requires_a_bounded_nonempty_peer_set(tmp_path: Path) -> None:
    identity = generate_identity(tmp_path / "identity")

    with pytest.raises(TransportError, match="at least one peer certificate"):
        server_context(identity, [])
    with pytest.raises(TransportError, match="at most"):
        server_context(identity, [identity.certificate] * (MAX_PEER_CERTIFICATES + 1))


def test_python_ssl_requires_a_client_certificate(tmp_path: Path) -> None:
    local = generate_identity(tmp_path / "local")
    peer = generate_identity(tmp_path / "peer")

    with pytest.raises(ssl.SSLError):
        _memory_handshake(
            _client_context_without_identity(peer.certificate),
            server_context(peer, local.certificate),
        )


def test_python_ssl_refuses_a_tls_12_only_client(tmp_path: Path) -> None:
    local = generate_identity(tmp_path / "local")
    peer = generate_identity(tmp_path / "peer")
    client = client_context(local, peer.certificate)
    client.minimum_version = ssl.TLSVersion.TLSv1_2
    client.maximum_version = ssl.TLSVersion.TLSv1_2

    with pytest.raises(ssl.SSLError):
        _memory_handshake(client, server_context(peer, local.certificate))


def test_frame_reader_scans_each_byte_once() -> None:
    frame = b"x" * 2048 + b"\n"
    connection = _FakeSocket([bytes((byte,)) for byte in frame])
    reader = FrameReader(cast(socket.socket, connection))

    assert reader.read_frame() == frame
    assert reader.bytes_scanned == len(frame)


def test_frame_reader_cuts_off_a_slow_sender_with_an_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr("dromon.federation_tls.time.monotonic", lambda: clock[0])
    connection = _FakeSocket([b"x"] * 8, clock=clock, step_seconds=0.6)
    reader = FrameReader(cast(socket.socket, connection), frame_timeout_seconds=1.0)

    with pytest.raises(FrameTimeout):
        reader.read_frame()

    assert connection.closed
    assert connection.receive_count == 2


def test_overall_connection_deadline_bounds_later_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    monkeypatch.setattr("dromon.federation_tls.time.monotonic", lambda: clock[0])
    connection = _FakeSocket([b"first\n", b"second\n"])
    reader = FrameReader(
        cast(socket.socket, connection),
        frame_timeout_seconds=30.0,
        connection_deadline=1.0,
    )

    assert reader.read_frame() == b"first\n"
    clock[0] = 1.1
    with pytest.raises(FrameTimeout):
        reader.read_frame()

    assert connection.closed
    assert connection.receive_count == 1


def test_new_connection_deadline_is_absolute_and_uses_the_given_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dromon.federation_tls.time.monotonic", lambda: 42.0)

    assert new_connection_deadline(2.5) == 44.5


def test_send_frame_closes_a_connection_when_the_write_deadline_expires() -> None:
    clock = [0.0]
    connection = _FakeSocket([], clock=clock, step_seconds=1.1)

    with pytest.raises(TimeoutError):
        send_frame(
            cast(socket.socket, connection),
            b"{}\n",
            frame_timeout_seconds=1.0,
        )

    assert connection.closed
    assert connection.send_count == 1


@pytest.mark.socket
def test_first_response_is_read_before_the_caller_accepts_a_connection() -> None:
    client, server = socket.socketpair()
    reader = FrameReader(client)
    server.sendall(b'{"hello":"accepted"}\n{"next":true}\n')

    assert read_first_response(reader) == b'{"hello":"accepted"}\n'
    assert reader.read_frame() == b'{"next":true}\n'

    client.close()
    server.close()


@pytest.mark.socket
def test_first_response_closes_when_the_peer_sends_nothing() -> None:
    client, server = socket.socketpair()
    reader = FrameReader(client)
    server.shutdown(socket.SHUT_WR)

    with pytest.raises(PeerClosed):
        read_first_response(reader)

    assert server.recv(1) == b""
    server.close()


@pytest.mark.socket
def test_tls_handshake_deadline_closes_the_socket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local = generate_identity(tmp_path / "local")
    peer = generate_identity(tmp_path / "peer")
    context = server_context(local, peer.certificate)
    server, stalled_client = socket.socketpair()
    monkeypatch.setattr("dromon.federation_tls.TLS_HANDSHAKE_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(HandshakeTimeout):
        wrap_server(server, context, peer.pin_sha256)

    assert stalled_client.recv(1) == b""
    stalled_client.close()


@pytest.mark.socket
def test_invalid_pin_closes_the_unwrapped_socket(tmp_path: Path) -> None:
    local = generate_identity(tmp_path / "local")
    peer = generate_identity(tmp_path / "peer")
    context = client_context(local, peer.certificate)
    client, server = socket.socketpair()

    with pytest.raises(TransportError, match="64 lowercase hexadecimal"):
        wrap_client(client, context, "bad pin")

    assert client.fileno() == -1
    assert server.recv(1) == b""
    server.close()


@pytest.mark.socket
def test_tls_client_and_server_mutually_authenticate_and_exchange_frames(
    tmp_path: Path,
) -> None:
    local = generate_identity(tmp_path / "local")
    peer = generate_identity(tmp_path / "peer")
    client = client_context(local, peer.certificate)
    server = server_context(peer, local.certificate)
    listener = _listener()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_serve_frames, listener, server, local.pin_sha256, 2)
        with socket.create_connection(listener.getsockname(), TIMEOUT_SECONDS) as raw:
            raw.settimeout(TIMEOUT_SECONDS)
            with wrap_client(raw, client, peer.pin_sha256) as tls:
                send_frame(tls, b'{"id":"first"}\n')
                send_frame(tls, b'{"id":"second"}\n')
                assert FrameReader(tls).read_frame() == b'{"ok":true}\n'
        tls_version, frames = future.result(timeout=TIMEOUT_SECONDS)

    listener.close()
    assert tls_version == "TLSv1.3"
    assert frames == (b'{"id":"first"}\n', b'{"id":"second"}\n')


@pytest.mark.socket
def test_a_wrong_pin_closes_the_authenticated_connection(tmp_path: Path) -> None:
    local = generate_identity(tmp_path / "local")
    peer = generate_identity(tmp_path / "peer")
    client = client_context(local, peer.certificate)
    server = server_context(peer, local.certificate)
    listener = _listener()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_serve_one, listener, server, local.pin_sha256)
        with socket.create_connection(listener.getsockname(), TIMEOUT_SECONDS) as raw:
            raw.settimeout(TIMEOUT_SECONDS)
            with pytest.raises(PeerPinError, match="configured pin"):
                wrap_client(raw, client, local.pin_sha256)
        assert future.result(timeout=TIMEOUT_SECONDS) == "TLSv1.3"

    listener.close()


@pytest.mark.socket
def test_server_rejects_client_without_a_certificate(tmp_path: Path) -> None:
    local = generate_identity(tmp_path / "local")
    peer = generate_identity(tmp_path / "peer")
    server = server_context(peer, local.certificate)
    no_certificate = _client_context_without_identity(peer.certificate)
    listener = _listener()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_serve_one, listener, server, local.pin_sha256)
        with socket.create_connection(listener.getsockname(), TIMEOUT_SECONDS) as raw:
            raw.settimeout(TIMEOUT_SECONDS)
            client_tls = no_certificate.wrap_socket(raw, server_hostname=None)
        with pytest.raises(ssl.SSLError):
            future.result(timeout=TIMEOUT_SECONDS)
        try:
            client_tls.settimeout(TIMEOUT_SECONDS)
            try:
                received = client_tls.recv(1)
            except ssl.SSLError:
                pass
            else:
                assert received == b""
        finally:
            client_tls.close()

    listener.close()


@pytest.mark.socket
def test_server_rejects_a_client_that_offers_only_tls_12(tmp_path: Path) -> None:
    local = generate_identity(tmp_path / "local")
    peer = generate_identity(tmp_path / "peer")
    client = client_context(local, peer.certificate)
    client.minimum_version = ssl.TLSVersion.TLSv1_2
    client.maximum_version = ssl.TLSVersion.TLSv1_2
    server = server_context(peer, local.certificate)
    listener = _listener()

    # TLS 1.2 cannot negotiate with this TLS 1.3-only server, so rejection is in the
    # initial handshake. This differs from client-certificate rejection under TLS 1.3.
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_serve_one, listener, server, local.pin_sha256)
        with socket.create_connection(listener.getsockname(), TIMEOUT_SECONDS) as raw:
            raw.settimeout(TIMEOUT_SECONDS)
            with pytest.raises(ssl.SSLError):
                wrap_client(raw, client, peer.pin_sha256)
        with pytest.raises(ssl.SSLError):
            future.result(timeout=TIMEOUT_SECONDS)

    listener.close()


@pytest.mark.socket
def test_framing_preserves_and_passes_replayed_application_frames() -> None:
    left, right = socket.socketpair()
    reader = FrameReader(left)
    frame = b'{"origin_id":"same"}\n'
    send_frame(right, frame)
    send_frame(right, frame)

    assert reader.read_frame() == frame
    assert reader.read_frame() == frame
    left.close()
    right.close()


@pytest.mark.socket
def test_framing_rejects_a_truncated_line() -> None:
    left, right = socket.socketpair()
    reader = FrameReader(left)
    right.sendall(b'{"partial":true}')
    right.shutdown(socket.SHUT_WR)

    with pytest.raises(TruncatedFrame):
        reader.read_frame()

    assert left.fileno() == -1
    assert right.recv(1) == b""
    right.close()


@pytest.mark.socket
def test_framing_rejects_an_oversized_line(monkeypatch: pytest.MonkeyPatch) -> None:
    left, right = socket.socketpair()
    reader = FrameReader(left)
    monkeypatch.setattr("dromon.federation_tls.MAX_FRAME_BYTES", 8)
    right.sendall(b"12345678")

    with pytest.raises(OversizedFrame):
        reader.read_frame()

    assert left.fileno() == -1
    assert right.recv(1) == b""
    right.close()


@pytest.mark.socket
def test_sending_rejects_unbounded_or_multi_line_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dromon.federation_tls.MAX_FRAME_BYTES", 8)
    left, right = socket.socketpair()

    with pytest.raises(OversizedFrame):
        send_frame(left, b"12345678\n")
    assert left.fileno() == -1
    assert right.recv(1) == b""
    right.close()

    left, right = socket.socketpair()
    with pytest.raises(FrameError, match="exactly one final"):
        send_frame(left, b"{}\n{}\n")
    assert left.fileno() == -1
    assert right.recv(1) == b""
    right.close()


def _listener() -> socket.socket:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.settimeout(TIMEOUT_SECONDS)
    try:
        listener.bind(("127.0.0.1", 0))
    except PermissionError as error:
        listener.close()
        pytest.skip(f"sandbox denies loopback TCP bind: {error}")
    listener.listen(1)
    return listener


def _serve_one(listener: socket.socket, context: ssl.SSLContext, pin: str) -> str:
    raw, _ = listener.accept()
    raw.settimeout(TIMEOUT_SECONDS)
    with wrap_server(raw, context, pin) as tls:
        version = tls.version()
        if version is None:
            raise AssertionError("TLS handshake completed without a version")
        return version


def _serve_frames(
    listener: socket.socket,
    context: ssl.SSLContext,
    pin: str,
    count: int,
) -> tuple[str, tuple[bytes | None, ...]]:
    raw, _ = listener.accept()
    raw.settimeout(TIMEOUT_SECONDS)
    with wrap_server(raw, context, pin) as tls:
        version = tls.version()
        if version is None:
            raise AssertionError("TLS handshake completed without a version")
        reader = FrameReader(tls)
        frames = tuple(reader.read_frame() for _ in range(count))
        send_frame(tls, b'{"ok":true}\n')
        return version, frames


def _client_context_without_identity(peer_certificate: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(cafile=str(peer_certificate))
    return context


class _MemoryTlsPair:
    def __init__(
        self,
        client: ssl.SSLObject,
        server: ssl.SSLObject,
        client_in: ssl.MemoryBIO,
        client_out: ssl.MemoryBIO,
        server_in: ssl.MemoryBIO,
        server_out: ssl.MemoryBIO,
    ) -> None:
        self.client = client
        self.server = server
        self._client_in = client_in
        self._client_out = client_out
        self._server_in = server_in
        self._server_out = server_out

    def transfer_client_to_server(self) -> None:
        data = self._client_out.read()
        if data:
            self._server_in.write(data)


def _memory_handshake(
    client_context_value: ssl.SSLContext,
    server_context_value: ssl.SSLContext,
) -> _MemoryTlsPair:
    client_in = ssl.MemoryBIO()
    client_out = ssl.MemoryBIO()
    server_in = ssl.MemoryBIO()
    server_out = ssl.MemoryBIO()
    client = client_context_value.wrap_bio(client_in, client_out, server_side=False)
    server = server_context_value.wrap_bio(server_in, server_out, server_side=True)
    for _ in range(100):
        for tls_object, outgoing, incoming in (
            (client, client_out, server_in),
            (server, server_out, client_in),
        ):
            with suppress(ssl.SSLWantReadError, ssl.SSLWantWriteError):
                tls_object.do_handshake()
            data = outgoing.read()
            if data:
                incoming.write(data)
        if client.version() is not None and server.version() is not None:
            return _MemoryTlsPair(
                client, server, client_in, client_out, server_in, server_out
            )
    raise AssertionError("TLS handshake did not complete within 100 turns")


class _FakeSocket:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        clock: list[float] | None = None,
        step_seconds: float = 0.0,
    ) -> None:
        self._chunks = chunks
        self._clock = clock
        self._step_seconds = step_seconds
        self._timeout: float | None = None
        self.closed = False
        self.receive_count = 0
        self.send_count = 0

    def gettimeout(self) -> float | None:
        return self._timeout

    def settimeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def recv(self, size: int) -> bytes:
        self.receive_count += 1
        if self._clock is not None:
            self._clock[0] += self._step_seconds
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if len(chunk) <= size:
            return chunk
        self._chunks.insert(0, chunk[size:])
        return chunk[:size]

    def sendall(self, data: bytes) -> None:
        self.send_count += 1
        if self._clock is not None:
            self._clock[0] += self._step_seconds
        if self._timeout is not None and self._step_seconds > self._timeout:
            raise TimeoutError("fake send exceeded its socket deadline")

    def close(self) -> None:
        self.closed = True
