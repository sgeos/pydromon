"""Pinned TLS identity and bounded federation framing."""

import socket
import ssl
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final

import pytest

from dromon.federation_tls import (
    CERTIFICATE_FILENAME,
    KEY_FILENAME,
    FrameError,
    FrameReader,
    OversizedFrame,
    PeerPinError,
    TruncatedFrame,
    TransportError,
    client_context,
    generate_identity,
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


def test_identity_generation_does_not_replace_existing_files(tmp_path: Path) -> None:
    directory = tmp_path / "peer"
    original = generate_identity(directory)
    original_key = original.private_key.read_bytes()

    with pytest.raises(TransportError, match="cannot create identity files"):
        generate_identity(directory)

    assert original.private_key.read_bytes() == original_key
    assert original.certificate.is_file()


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
            with pytest.raises(ssl.SSLError):
                no_certificate.wrap_socket(raw, server_hostname=None)
        with pytest.raises(ssl.SSLError):
            future.result(timeout=TIMEOUT_SECONDS)

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

    left.close()
    right.close()


@pytest.mark.socket
def test_framing_rejects_an_oversized_line(monkeypatch: pytest.MonkeyPatch) -> None:
    left, right = socket.socketpair()
    reader = FrameReader(left)
    monkeypatch.setattr("dromon.federation_tls.MAX_FRAME_BYTES", 8)
    right.sendall(b"12345678")

    with pytest.raises(OversizedFrame):
        reader.read_frame()

    left.close()
    right.close()


@pytest.mark.socket
def test_sending_rejects_unbounded_or_multi_line_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left, right = socket.socketpair()
    monkeypatch.setattr("dromon.federation_tls.MAX_FRAME_BYTES", 8)

    with pytest.raises(OversizedFrame):
        send_frame(left, b"12345678\n")
    with pytest.raises(FrameError, match="exactly one final"):
        send_frame(left, b"{}\n{}\n")

    left.close()
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
