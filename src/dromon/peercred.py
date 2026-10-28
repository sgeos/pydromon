"""The user behind a Unix domain socket connection, read from its peer credentials.

Every socket the daemon serves refuses a connection from a process of another user.
The runtime directory's mode 0700 already keeps other users out, and the check states
that boundary on each connection rather than relying on the directory alone, as
``docs/protocol/CLIENT.md`` records under trust and ``docs/architecture/FEDERATION.md``
requires before daemons connect to each other.

What it proves. Only that the connecting process runs as the daemon's own user. A line
runs as that user too, so the check does not tell a line's shell from the operator's
client. The sandbox, which denies every line the runtime directory, and the tokens of
the line and agent sockets do that.

Failure modes. A connection whose credentials cannot be read is refused, never let
through, and a platform that offers neither interface below refuses to start the daemon,
so the check cannot silently be absent.

Porting hazard. Python decodes neither credential structure, so this module unpacks
them with ``struct`` by their C layouts. macOS and the BSDs give ``struct xucred`` at
level ``SOL_LOCAL``, which is 0, for ``LOCAL_PEERCRED``, whose first two fields are
an unsigned version, ``XUCRED_VERSION`` or 0, and the user identifier. Linux gives
``struct ucred`` for ``SO_PEERCRED``, a pid, a user identifier and a group identifier.
A Rust port uses the ``nix`` crate's ``getpeereid`` and ``PeerCredentials`` instead.
"""

import socket
import struct
from typing import Final, Protocol

SOL_LOCAL: Final = 0
"""The option level of ``LOCAL_PEERCRED`` on macOS and the BSDs, unnamed in Python."""

XUCRED_VERSION: Final = 0
"""The only ``struct xucred`` version this module reads."""

XUCRED_BYTES: Final = 76
"""The size of ``struct xucred``, which holds a version, a user identifier, a group
count and 16 group identifiers, with the padding of its C layout."""

XUCRED_HEAD: Final = struct.Struct("=II")
"""The version and the user identifier, the start of ``struct xucred``."""

UCRED: Final = struct.Struct("=iII")
"""Linux's ``struct ucred``, a pid, a user identifier and a group identifier."""

LOCAL_PEERCRED: Final[int | None] = getattr(socket, "LOCAL_PEERCRED", None)
SO_PEERCRED: Final[int | None] = getattr(socket, "SO_PEERCRED", None)


class PeerCredentialError(Exception):
    """The peer's credentials cannot be read, or this platform offers no way to."""


class CredentialSocket(Protocol):
    """What the check needs of a socket, which ``asyncio``'s transport socket offers."""

    def getsockopt(self, level: int, optname: int, buflen: int, /) -> bytes: ...


def check_supported() -> None:
    """Raise ``PeerCredentialError`` when this platform offers neither interface."""
    if LOCAL_PEERCRED is None and SO_PEERCRED is None:
        raise PeerCredentialError(
            "this platform offers neither LOCAL_PEERCRED nor SO_PEERCRED, so the"
            " daemon cannot check who connects to its sockets, and does not start"
        )


def peer_uid(sock: CredentialSocket) -> int:
    """The user identifier of the process at the other end of ``sock``.

    Raises ``PeerCredentialError`` when the credentials cannot be read or decoded.
    """
    try:
        if LOCAL_PEERCRED is not None:
            raw = sock.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, XUCRED_BYTES)
            if len(raw) < XUCRED_HEAD.size:
                raise PeerCredentialError(
                    f"the peer credentials were {len(raw)} bytes, too short to read"
                )
            version, uid = XUCRED_HEAD.unpack_from(raw)
            if version != XUCRED_VERSION:
                raise PeerCredentialError(
                    f"the peer credentials have version {version}, not {XUCRED_VERSION}"
                )
            return uid
        if SO_PEERCRED is not None:
            raw = sock.getsockopt(socket.SOL_SOCKET, SO_PEERCRED, UCRED.size)
            if len(raw) < UCRED.size:
                raise PeerCredentialError(
                    f"the peer credentials were {len(raw)} bytes, too short to read"
                )
            _pid, uid, _gid = UCRED.unpack_from(raw)
            return uid
    except OSError as error:
        reason = error.strerror or type(error).__name__
        raise PeerCredentialError(
            f"the peer credentials cannot be read: {reason}"
        ) from error
    raise PeerCredentialError(
        "this platform offers neither LOCAL_PEERCRED nor SO_PEERCRED"
    )


def refusal(sock: CredentialSocket, own_uid: int) -> str | None:
    """Why a connection on ``sock`` is refused, or ``None`` when it is from ``own_uid``.

    The reason names user identifiers, which are not secret, and nothing the peer sent.
    """
    try:
        uid = peer_uid(sock)
    except PeerCredentialError as error:
        return str(error)
    if uid != own_uid:
        return f"the connection is from user {uid}, not the daemon's user {own_uid}"
    return None
