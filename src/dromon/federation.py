"""Federation between Dromon daemons, the ``[federation]`` and ``[peers]`` tables.

A peer is another Dromon daemon that the operator declares by name in the project file.
``docs/architecture/FEDERATION.md`` is the design, approved by the operator on
2026-10-28 and amended the same day. This module holds what phase 1 has built so far,
the validated peer table, the directories it adds to every line's protected set, the
peer events that record the table at start, and the token store under
``.dromon/peers``. The receiving end of peer mail is ``dromon.peering``, and the
sending end comes in a later step.

Trust. The tables are operator configuration, committed with the project, and no line
or agent tool can change them. Each peer's state and runtime directories are denied to
every local line, as its own project's are, so that a line cannot read another
project's event log or reach its peer socket through the file system. A project with
peers refuses lines on an engine that Dromon cannot confine, unless the project file
accepts them, because such a line could read the peer tokens and act as a peer.

Tokens. A peer presents the token this daemon issued for it, of which only the SHA-256
is recorded, by ``peer_keyed``. The token the peer issued for this daemon is kept in
``.dromon/peers/NAME.token``, mode 0600 in a directory of mode 0700, and the daemon
refuses to start when either is wider or is not its user's. The store is denied to
every sandboxed line with the rest of the state directory, and is not protected from
an unconfined process of the same user, as the design's trust section states.

Phase 1 is the same machine only. A peer with the ``tls`` transport of phase 2 is
refused until that phase is built, rather than accepted and ignored.

Porting note. Plain data and bounded loops over the file's tables. The bound on a
socket path is that of ``sun_path`` on macOS, the smaller of the two platforms.
"""

import json
import os
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast

from dromon.model import (
    ID_PATTERN,
    MAX_TEXT_BYTES,
    LocalAddress,
    Event,
    EventRejected,
    InvalidField,
    PeerDeclared,
    PeerRemoved,
    State,
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
operator's decision 9 of 2026-10-28."""

SANDBOXED_ENGINES: Final = frozenset({"claude-code"})
"""Engines confined only while the line's merged sandbox is enabled, so that a line on
one without it is unconfined too, by the operator's decision of 2026-10-28 on the first
open point of the design."""

PEERS_DIR_NAME: Final = "peers"
"""The token store, a directory in the state directory."""
TOKEN_SUFFIX: Final = ".token"  # noqa: S105, a file name, not a secret
MAX_TOKEN_BYTES: Final = 256
TOKEN_PATTERN: Final = re.compile(r"[A-Za-z0-9_-]{16,256}")
"""A stored token, as ``secrets.token_urlsafe`` writes one. No whitespace and no line
break, so the file holds exactly the token."""
MAX_STORE_ENTRIES: Final = 4 * MAX_PEERS
"""The most entries the store's check reads, which bounds its loop. A store with more is
refused, since each peer needs one file and a temporary one only while it is written."""
STORE_DIR_MODE: Final = 0o700
STORE_FILE_MODE: Final = 0o600


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
    reach: tuple[LocalAddress, ...] = ()
    """The local parties the peer may send to."""
    senders: tuple[LocalAddress, ...] = ()
    """The local parties that may send to the peer."""
    remote: tuple[LocalAddress, ...] = ()
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


def _parties(table: dict[str, object], key: str, name: str) -> tuple[LocalAddress, ...]:
    """A list of local addresses, distinct and sorted, none of them remote."""
    value = table.get(name, [])
    if not isinstance(value, list) or len(cast(list[object], value)) > MAX_PARTIES:
        raise FederationError(
            f"{key}.{name} must be a list of at most {MAX_PARTIES} addresses"
        )
    found: dict[str, LocalAddress] = {}
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
    federation: FederationConfig, line_id: str, engine: str | None, sandboxed: bool
) -> str | None:
    """Why a line of ``engine`` may not run in a project with these peers, or
    ``None`` when it may. ``sandboxed`` says whether the line's merged sandbox is
    enabled, which confines an engine of ``SANDBOXED_ENGINES``. Decision 9 of the
    design, and the operator's decision on its first open point."""
    if not federation.peers or federation.accept_unconfined:
        return None
    if engine in SANDBOXED_ENGINES and not sandboxed:
        return (
            f"line {line_id} runs on {engine} without an enabled sandbox, so Dromon"
            " cannot confine it, and the project declares peers. Enable its sandbox,"
            " or set accept_unconfined = true in [federation] to accept that the line"
            " could act as a peer"
        )
    if engine not in UNCONFINED_ENGINES:
        return None
    return (
        f"line {line_id} runs on {engine}, which Dromon cannot confine, and the"
        " project declares peers. Set accept_unconfined = true in [federation]"
        " to accept that the line could act as a peer"
    )


def policy_text(peer: PeerDefinition) -> str:
    """The peer's policy as canonical JSON, which ``peer_declared`` records. It holds
    every key of the peer's table but its transport, which the event holds apart, and
    never a token, since none is in the table."""
    document: dict[str, object] = {
        "socket": str(peer.socket),
        "project": None if peer.project is None else str(peer.project),
        "inbound": peer.inbound,
        "outbound": peer.outbound,
        "reach": [str(address) for address in peer.reach],
        "senders": [str(address) for address in peer.senders],
        "remote": [str(address) for address in peer.remote],
        "priority_in": peer.priority_in,
        "priority_out": peer.priority_out,
        "max_body_bytes": peer.max_body_bytes,
        "expire_hours": peer.expire_hours,
        "turns_per_hour": peer.turns_per_hour,
        "waiting": peer.waiting,
    }
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def plan_peers(state: State, federation: FederationConfig) -> tuple[Event, ...]:
    """The events that bring ``state``'s peers in line with ``federation``, in the order
    of the peers' names. A peer that is new, removed earlier, or changed in transport or
    policy is declared, and a peer the log holds and the file no longer declares is
    removed. Raises ``FederationError`` when an event does not apply, which a valid
    table and a valid log never cause."""
    trial = state.copy()
    events: list[Event] = []
    wanted: list[Event] = []
    for peer_id, peer in sorted(federation.peers.items()):
        declared = PeerDeclared(peer_id, peer.transport, policy_text(peer))
        known = trial.peers.get(peer_id)
        if (
            known is None
            or known.removed
            or (known.transport, known.policy) != (peer.transport, declared.policy)
        ):
            wanted.append(declared)
    for peer_id, known in sorted(trial.peers.items()):
        if not known.removed and peer_id not in federation.peers:
            wanted.append(PeerRemoved(peer_id))
    for event in wanted:
        try:
            trial.apply(event)
        except EventRejected as error:
            raise FederationError(str(error)) from error
        events.append(event)
    return tuple(events)


def check_token_store(directory: Path) -> None:
    """Refuse a token store that is not a directory of this user with mode 0700, or
    that holds anything other than regular files of this user with mode 0600. A store
    that does not exist passes, since no token is stored yet. Raises
    ``FederationError`` naming the path, never a token.

    The check reads modes with ``lstat``, so a symbolic link is refused rather than
    followed. **Inference.** It runs at start, and a change to the modes while the
    daemon runs is seen only at the next start or the next write."""
    try:
        info = os.lstat(directory)
    except FileNotFoundError:
        return
    _check_owned(directory, info, stat.S_ISDIR, "a directory", STORE_DIR_MODE)
    seen = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            seen += 1
            if seen > MAX_STORE_ENTRIES:
                raise FederationError(
                    f"{directory} holds more than {MAX_STORE_ENTRIES} entries"
                )
            path = Path(entry.path)
            _check_owned(
                path, os.lstat(path), stat.S_ISREG, "a regular file", STORE_FILE_MODE
            )


def _check_owned(
    path: Path,
    info: os.stat_result,
    is_kind: Callable[[int], bool],
    what: str,
    mode: int,
) -> None:
    if not is_kind(info.st_mode):
        raise FederationError(f"{path} must be {what}")
    if info.st_uid != os.getuid():
        raise FederationError(f"{path} must belong to this user")
    found = stat.S_IMODE(info.st_mode)
    if found & ~mode:
        raise FederationError(f"{path} has mode {found:04o}, wider than {mode:04o}")


def token_path(directory: Path, peer_id: str) -> Path:
    """Where the token the peer issued for this daemon is stored."""
    if ID_PATTERN.fullmatch(peer_id) is None:
        raise FederationError(f"{peer_id!r} is not a valid peer identifier")
    return directory / f"{peer_id}{TOKEN_SUFFIX}"


def store_token(directory: Path, peer_id: str, token: str) -> Path:
    """Store the token the peer issued for this daemon, replacing any earlier one, and
    return its path. The directory is created with mode 0700 when missing and checked,
    and the file is written whole to a temporary name with mode 0600 and then renamed,
    so a crash leaves the old token or the new one and never a part. Raises
    ``FederationError``, whose text never holds the token."""
    final = token_path(directory, peer_id)
    if TOKEN_PATTERN.fullmatch(token) is None:
        raise FederationError(
            "a peer token is 16 to 256 letters, digits, - or _, as dromon peer token"
            " prints one"
        )
    try:
        directory.mkdir(mode=STORE_DIR_MODE, exist_ok=True)
    except OSError as error:
        raise FederationError(
            f"{directory} could not be made: {error.strerror}"
        ) from error
    check_token_store(directory)
    temporary = directory / f".{peer_id}{TOKEN_SUFFIX}.new"
    try:
        temporary.unlink(missing_ok=True)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            STORE_FILE_MODE,
        )
        try:
            os.write(descriptor, token.encode("ascii"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, final)
    except OSError as error:
        raise FederationError(
            f"{final} could not be written: {error.strerror}"
        ) from error
    return final


def stored_token(directory: Path, peer_id: str) -> str | None:
    """The token stored for the peer, or ``None`` when none is. Raises
    ``FederationError`` for a file that is not a regular file of this user with mode
    0600, or that does not hold a token, without quoting its content."""
    path = token_path(directory, peer_id)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    _check_owned(path, info, stat.S_ISREG, "a regular file", STORE_FILE_MODE)
    if info.st_size > MAX_TOKEN_BYTES:
        raise FederationError(f"{path} does not hold a token")
    try:
        token = path.read_bytes().decode("ascii")
    except (OSError, UnicodeDecodeError) as error:
        raise FederationError(f"{path} does not hold a token") from error
    if TOKEN_PATTERN.fullmatch(token) is None:
        raise FederationError(f"{path} does not hold a token")
    return token
