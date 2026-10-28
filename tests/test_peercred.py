"""The peer credential check every daemon socket applies, step 1 of federation phase 1.

A socket pair needs no file system path, so these tests run inside the sandbox, which
denies connecting to a Unix domain socket by path. The refusal of another user is
tested by naming a user identifier other than the test's own, since no test can run a
process as another user.
"""

import asyncio
import json
import os
import socket
import struct

import pytest

from dromon import daemon, peercred


class Fixed:
    """A socket whose credentials are the bytes given, or whose read fails."""

    def __init__(self, raw: bytes | None) -> None:
        self.raw = raw
        self.asked: list[tuple[int, int, int]] = []

    def getsockopt(self, level: int, optname: int, buflen: int, /) -> bytes:
        self.asked.append((level, optname, buflen))
        if self.raw is None:
            raise OSError(57, "Socket is not connected")
        return self.raw


def credentials_of(uid: int) -> bytes:
    """The bytes this platform's interface gives for a peer running as ``uid``."""
    if peercred.LOCAL_PEERCRED is not None:
        head = peercred.XUCRED_HEAD.pack(peercred.XUCRED_VERSION, uid)
        return head + bytes(peercred.XUCRED_BYTES - len(head))
    return peercred.UCRED.pack(4242, uid, 20)


def test_this_platform_is_supported() -> None:
    peercred.check_supported()


def test_a_socket_pair_reports_the_test_s_own_user() -> None:
    first, second = socket.socketpair(socket.AF_UNIX)
    with first, second:
        assert peercred.peer_uid(first) == os.getuid()
        assert peercred.refusal(first, os.getuid()) is None


def test_another_user_is_refused_and_named() -> None:
    first, second = socket.socketpair(socket.AF_UNIX)
    other = os.getuid() + 1
    with first, second:
        reason = peercred.refusal(first, other)
    assert reason == (
        f"the connection is from user {os.getuid()}, not the daemon's user {other}"
    )


def test_decoding_follows_the_platform_layout() -> None:
    fixed = Fixed(credentials_of(1234))
    assert peercred.peer_uid(fixed) == 1234
    [(level, optname, buflen)] = fixed.asked
    if peercred.LOCAL_PEERCRED is not None:
        assert (level, optname, buflen) == (
            peercred.SOL_LOCAL,
            peercred.LOCAL_PEERCRED,
            peercred.XUCRED_BYTES,
        )
    else:
        assert (level, optname, buflen) == (
            socket.SOL_SOCKET,
            peercred.SO_PEERCRED,
            peercred.UCRED.size,
        )


def test_unreadable_credentials_are_refused_not_let_through() -> None:
    reason = peercred.refusal(Fixed(None), os.getuid())
    assert reason == "the peer credentials cannot be read: Socket is not connected"


def test_short_credentials_are_refused() -> None:
    reason = peercred.refusal(Fixed(b"\x00\x00"), os.getuid())
    assert reason == "the peer credentials were 2 bytes, too short to read"


@pytest.mark.skipif(
    peercred.LOCAL_PEERCRED is None, reason="struct xucred is macOS and BSD only"
)
def test_an_unknown_xucred_version_is_refused() -> None:
    raw = struct.pack("=II", 7, os.getuid()) + bytes(peercred.XUCRED_BYTES - 8)
    reason = peercred.refusal(Fixed(raw), os.getuid())
    assert reason == "the peer credentials have version 7, not 0"


def test_a_platform_without_either_interface_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(peercred, "LOCAL_PEERCRED", None)
    monkeypatch.setattr(peercred, "SO_PEERCRED", None)
    with pytest.raises(peercred.PeerCredentialError, match="does not start"):
        peercred.check_supported()
    with pytest.raises(peercred.PeerCredentialError, match="neither"):
        peercred.peer_uid(Fixed(credentials_of(os.getuid())))


async def _guard_over_a_pair(own_uid: int) -> tuple[bool, bytes]:
    """Run ``daemon._guarded`` on one end of a pair, and read what the other end got."""
    near, far = socket.socketpair(socket.AF_UNIX)
    served: list[bool] = []

    async def serving() -> None:
        served.append(True)

    _reader, writer = await asyncio.open_unix_connection(sock=near)
    far_reader, far_writer = await asyncio.open_unix_connection(sock=far)
    try:
        await daemon._guarded(writer, own_uid, serving)  # pyright: ignore[reportPrivateUsage]
        writer.close()
        received = await asyncio.wait_for(far_reader.read(), 5)
    finally:
        far_writer.close()
    return bool(served), received


def test_the_daemon_serves_a_connection_from_its_own_user() -> None:
    served, received = asyncio.run(_guard_over_a_pair(os.getuid()))
    assert served
    assert received == b""


def test_the_daemon_refuses_a_connection_from_another_user() -> None:
    other = os.getuid() + 1
    served, received = asyncio.run(_guard_over_a_pair(other))
    assert not served
    response = json.loads(received)
    assert response["ok"] is False
    assert response["error"]["code"] == "rejected"
    assert response["error"]["message"] == (
        "the daemon refused the connection, the connection is from user"
        f" {os.getuid()}, not the daemon's user {other}"
    )
