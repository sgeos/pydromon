"""Federation between Dromon daemons, the ``[federation]`` and ``[peers]`` tables.

A peer is another Dromon daemon that the operator declares by name in the project file.
``docs/architecture/FEDERATION.md`` is the design, approved by the operator on
2026-10-28 and amended the same day. This module holds what phase 1 has built so far,
the validated peer table and the directories it adds to every line's protected set.
Peer mail, the peer socket, tokens and the events come in later steps.

Trust. The tables are operator configuration, committed with the project, and no line
or agent tool can change them. Each peer's state and runtime directories are denied to
every local line, as its own project's are, so that a line cannot read another
project's event log or reach its peer socket through the file system. A project with
peers refuses lines on an engine that Dromon cannot confine, unless the project file
accepts them, because such a line could read the peer tokens and act as a peer.

Phase 1 is the same machine only. A peer with the ``tls`` transport of phase 2 is
refused until that phase is built, rather than accepted and ignored.

Porting note. Plain data and bounded loops over the file's tables. The bound on a
socket path is that of ``sun_path`` on macOS, the smaller of the two platforms.
"""

import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast

from dromon.model import (
    ID_PATTERN,
    MAX_TEXT_BYTES,
    Address,
    InvalidField,
    parse_address,
)

FEDERATION_KEYS: Final = frozenset(
    {"listen", "accept_unconfined", "waiting_per_recipient"}
)
PEER_KEYS: Final = frozenset(
    {
        "transport",
        "socket",
        "project",
        "inbound",
        "outbound",
        "reach",
        "senders",
        "remote",
        "priority_in",
        "priority_out",
        "max_body_bytes",
        "expire_hours",
        "turns_per_hour",
        "waiting",
    }
)
TRANSPORT_UNIX: Final = "unix"
TRANSPORT_TLS: Final = "tls"
PEER_SOCKET_NAME: Final = "peers.sock"
"""The name of a daemon's federation socket, in its runtime directory. A peer's
``socket`` must end in it, so its parent is the peer's runtime directory."""
STATE_DIR_NAME: Final = ".dromon"

MAX_PEERS: Final = 64
MAX_PARTIES: Final = 256
"""The most addresses one of ``reach``, ``senders`` or ``remote`` may list."""
MAX_SOCKET_PATH_BYTES: Final = 103
"""``sun_path`` holds 104 bytes on macOS and 108 on Linux, with a terminating zero."""
MAX_PROJECT_PATH_BYTES: Final = 4096

DEFAULT_MAX_BODY_BYTES: Final = 65536
DEFAULT_EXPIRE_HOURS: Final = 168
MAX_EXPIRE_HOURS: Final = 24 * 90
DEFAULT_TURNS_PER_HOUR: Final = 6
MAX_TURNS_PER_HOUR: Final = 60
DEFAULT_WAITING: Final = 500
DEFAULT_WAITING_PER_RECIPIENT: Final = 200
MAX_WAITING: Final = 100_000

UNCONFINED_ENGINES: Final = frozenset({"pi", "cursor-agent", "antigravity"})
"""Engines Dromon cannot confine to keep a line from the state directory, by the
operator's decision 9 of 2026-10-28. **Open.** Whether a Claude Code line without an
enabled sandbox counts too awaits the operator, so it is not refused here."""


class FederationError(ValueError):
    """The ``[federation]`` or ``[peers]`` table is malformed, with the key."""


@dataclass(frozen=True, slots=True)
class PeerDefinition:
    """A ``[peers.NAME]`` table. Both directions are off and every list is empty by
    default, so a peer declared with only its socket can do nothing."""

    peer_id: str
    socket: Path
    """The peer's ``peers.sock``, an absolute path. Its parent is the peer's runtime
    directory."""
    project: Path | None = None
    """The peer's project root, an absolute path, whose ``.dromon`` is denied to local
    lines, or ``None`` when the operator does not name it."""
    transport: str = TRANSPORT_UNIX
    inbound: bool = False
    outbound: bool = False
    reach: tuple[Address, ...] = ()
    """The local parties the peer may send to."""
    senders: tuple[Address, ...] = ()
    """The local parties that may send to the peer."""
    remote: tuple[Address, ...] = ()
    """The peer's parties that local senders may address, as the peer writes them."""
    priority_in: bool = False
    priority_out: bool = False
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    expire_hours: int = DEFAULT_EXPIRE_HOURS
    turns_per_hour: int = DEFAULT_TURNS_PER_HOUR
    waiting: int = DEFAULT_WAITING

    def directories(self) -> tuple[Path, ...]:
        """The peer's directories that local lines are denied, its runtime directory
        and, when its project is named, its state directory."""
        runtime = self.socket.parent
        if self.project is None:
            return (runtime,)
        return (runtime, self.project / STATE_DIR_NAME)


@dataclass(frozen=True, slots=True)
class FederationConfig:
    """The project's ``[federation]`` table and its peers. Empty by default."""

    listen: bool = False
    accept_unconfined: bool = False
    waiting_per_recipient: int = DEFAULT_WAITING_PER_RECIPIENT
    peers: dict[str, PeerDefinition] = field(default_factory=dict[str, PeerDefinition])


NO_FEDERATION: Final = FederationConfig()


def parse_federation(federation: object, peers: object) -> FederationConfig:
    """Validate the ``[federation]`` and ``[peers]`` tables, either of which may be
    ``None`` when the file omits it, or raise ``FederationError`` naming the key."""
    table = _table({} if federation is None else federation, "federation")
    _known(table, "federation", FEDERATION_KEYS)
    listen = _flag(table, "federation", "listen")
    unconfined = _flag(table, "federation", "accept_unconfined")
    per_recipient = _count(
        table,
        "federation.waiting_per_recipient",
        "waiting_per_recipient",
        DEFAULT_WAITING_PER_RECIPIENT,
        MAX_WAITING,
    )
    found: dict[str, PeerDefinition] = {}
    named = _table({} if peers is None else peers, "peers")
    if len(named) > MAX_PEERS:
        raise FederationError(f"peers declares more than {MAX_PEERS} peers")
    for peer_id, value in sorted(named.items()):
        found[peer_id] = _peer(peer_id, value)
    return FederationConfig(listen, unconfined, per_recipient, found)


def _peer(peer_id: str, value: object) -> PeerDefinition:
    key = f"peers.{peer_id}"
    if ID_PATTERN.fullmatch(peer_id) is None:
        raise FederationError(f"{key} is not a valid peer identifier")
    table = _table(value, key)
    _known(table, key, PEER_KEYS)
    transport = table.get("transport", TRANSPORT_UNIX)
    if transport == TRANSPORT_TLS:
        raise FederationError(
            f"{key}.transport tls belongs to phase 2, which is not yet built"
        )
    if transport != TRANSPORT_UNIX:
        raise FederationError(f"{key}.transport must be {TRANSPORT_UNIX}")
    socket = _absolute(table.get("socket"), f"{key}.socket", MAX_SOCKET_PATH_BYTES)
    if socket.name != PEER_SOCKET_NAME:
        raise FederationError(f"{key}.socket must name a file {PEER_SOCKET_NAME}")
    raw_project = table.get("project")
    project = (
        None
        if raw_project is None
        else _absolute(raw_project, f"{key}.project", MAX_PROJECT_PATH_BYTES)
    )
    return PeerDefinition(
        peer_id=peer_id,
        socket=socket,
        project=project,
        transport=TRANSPORT_UNIX,
        inbound=_flag(table, key, "inbound"),
        outbound=_flag(table, key, "outbound"),
        reach=_parties(table, key, "reach"),
        senders=_parties(table, key, "senders"),
        remote=_parties(table, key, "remote"),
        priority_in=_flag(table, key, "priority_in"),
        priority_out=_flag(table, key, "priority_out"),
        max_body_bytes=_count(
            table,
            f"{key}.max_body_bytes",
            "max_body_bytes",
            DEFAULT_MAX_BODY_BYTES,
            MAX_TEXT_BYTES,
        ),
        expire_hours=_count(
            table,
            f"{key}.expire_hours",
            "expire_hours",
            DEFAULT_EXPIRE_HOURS,
            MAX_EXPIRE_HOURS,
        ),
        turns_per_hour=_count(
            table,
            f"{key}.turns_per_hour",
            "turns_per_hour",
            DEFAULT_TURNS_PER_HOUR,
            MAX_TURNS_PER_HOUR,
        ),
        waiting=_count(
            table, f"{key}.waiting", "waiting", DEFAULT_WAITING, MAX_WAITING
        ),
    )


def _table(value: object, key: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise FederationError(f"{key} must be a table")
    return cast(dict[str, object], value)


def _known(table: dict[str, object], key: str, allowed: frozenset[str]) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise FederationError(
            f"{key}.{unknown[0]} is not a known key. Known keys are {sorted(allowed)}"
        )


def _flag(table: dict[str, object], key: str, name: str) -> bool:
    value = table.get(name, False)
    if type(value) is not bool:
        raise FederationError(f"{key}.{name} must be a boolean")
    return value


def _count(
    table: dict[str, object], key: str, name: str, default: int, most: int
) -> int:
    value = table.get(name, default)
    if type(value) is not int or not 1 <= value <= most:
        raise FederationError(f"{key} must be a whole number from 1 to {most}")
    return value


def _absolute(value: object, key: str, most: int) -> Path:
    """An absolute path, written without ``.`` or ``..`` steps, so that the directory
    denied is the one the operator wrote."""
    if not isinstance(value, str) or not value.startswith("/"):
        raise FederationError(f"{key} must be an absolute path")
    if "\x00" in value:
        raise FederationError(f"{key} holds a zero byte")
    if len(value.encode("utf-8")) > most:
        raise FederationError(f"{key} must be a path of at most {most} bytes")
    steps = value.split("/")[1:]
    if any(step in (".", "..") for step in steps):
        raise FederationError(f"{key} has a . or .. step")
    return Path(value)


def _parties(table: dict[str, object], key: str, name: str) -> tuple[Address, ...]:
    """A list of local addresses, distinct and sorted, none of them remote."""
    value = table.get(name, [])
    if not isinstance(value, list) or len(cast(list[object], value)) > MAX_PARTIES:
        raise FederationError(
            f"{key}.{name} must be a list of at most {MAX_PARTIES} addresses"
        )
    found: dict[str, Address] = {}
    for item in cast(list[object], value):
        if not isinstance(item, str):
            raise FederationError(f"{key}.{name} holds {item!r}, not an address")
        try:
            address = parse_address(item)
        except InvalidField as error:
            raise FederationError(
                f"{key}.{name} holds {item!r}, which is not an address"
            ) from error
        if item in found:
            raise FederationError(f"{key}.{name} names {item!r} twice")
        found[item] = address
    return tuple(found[text] for text in sorted(found))


def peer_directories(
    federation: FederationConfig, own: Sequence[Path]
) -> tuple[Path, ...]:
    """Every declared peer's directories, resolved, in the order of the peers' names.
    Raises ``FederationError`` when a peer's directory is one of this daemon's
    ``own`` protected directories, which would make the project its own peer, or when
    two peers share one."""
    mine = {Path(os.path.realpath(directory)) for directory in own}
    seen: dict[Path, str] = {}
    found: list[Path] = []
    for peer_id, peer in sorted(federation.peers.items()):
        for directory in peer.directories():
            real = Path(os.path.realpath(directory))
            if real in mine:
                raise FederationError(
                    f"peers.{peer_id} names this project's own directory {real}"
                )
            if real in seen:
                raise FederationError(
                    f"peers.{peer_id} and peers.{seen[real]} share the directory {real}"
                )
            seen[real] = peer_id
            found.append(real)
    return tuple(found)


def unconfined_refusal(
    federation: FederationConfig, line_id: str, engine: str | None
) -> str | None:
    """Why a line of ``engine`` may not run in a project with these peers, or
    ``None`` when it may. Decision 9 of the design."""
    if not federation.peers or federation.accept_unconfined:
        return None
    if engine not in UNCONFINED_ENGINES:
        return None
    return (
        f"line {line_id} runs on {engine}, which Dromon cannot confine, and the"
        " project declares peers. Set accept_unconfined = true in [federation]"
        " to accept that the line could act as a peer"
    )
