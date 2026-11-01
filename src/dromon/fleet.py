"""The operator's fleet, every daemon of this user on this machine and the hosts the
operator reaches by SSH. ``docs/architecture/FLEET.md`` is normative.

The index. Every daemon writes one entry at start and removes it at stop, whatever its
federation mode, so the client can list the daemons and reach each through its own
operator socket. An entry is the daemon's statement of where its socket is, never an
authority. A command reaches a daemon only by connecting to that socket, which only
this user can reach, and sends the operation the single-project command sends, so the
fleet adds no authority. Operator authority never crosses federation.

Location. **Decided by core, for the operator's review.** The index is the directory
``index`` inside the registry of round 12, whatever its federation mode, so the one
override ``DROMON_REGISTRY_DIR`` moves both, every test that sets it never touches the
real index, and the trust rules of the registry, a directory of this user with mode
0700 and entries of mode 0600, hold for it unchanged. It is a directory of its own
rather than more entries in the registry, because a registry entry says a daemon is
open to local federation and other daemons act on it, and an index entry says only
where a daemon's operator socket is.

Staleness. An entry names the daemon's pid and its start time from the process table.
An entry whose pid has another start time, or none, is stale, and one whose table
cannot be read is unknown. Neither is ever reached. A live entry is reached only after
the daemon answering its socket says the pid and the project root the entry names, so
a socket that another daemon now holds is never taken for the one indexed.

Porting note. Plain files, one advisory lock and the bounded reads of
``dromon.registry``.
"""

import hashlib
import json
import os
import pwd
import stat
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, cast

from dromon import registry
from dromon.model import ID_PATTERN

INDEX_DIR_NAME: Final = "index"
INDEX_VERSION: Final = 1
INDEX_KEYS: Final = frozenset(
    {"version", "name", "root", "socket", "pid", "started", "commit"}
)
OPERATOR_SOCKET_NAME: Final = "dromond.sock"
MAX_NAME_BYTES: Final = 255
"""The longest project name, a directory's name on every file system the port
targets."""
COMMIT_CHARS: Final = frozenset("0123456789abcdef")
MAX_COMMIT_CHARS: Final = 64


class FleetError(Exception):
    """The index or a target cannot be used, with the reason to show."""


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """One daemon of this user, as it described itself at start."""

    name: str
    """The project's name, the last step of its root, by which a command names it."""
    root: Path
    socket: Path
    """The daemon's operator socket, ``dromond.sock``, an absolute path."""
    pid: int
    started: str
    """The daemon's start time as the process table gives it."""
    commit: str | None
    """The commit the daemon loaded its code from, or ``None`` when unknown."""

    def __post_init__(self) -> None:
        check_name(self.name)
        for path, what in ((self.root, "root"), (self.socket, "socket")):
            try:
                registry.check_absolute(str(path), what)
            except registry.RegistryError as error:
                raise FleetError(str(error)) from error
        # Review of 7ff033f, G3. A name other than the root's last step would let a
        # writer of the index publish a live daemon under a name of its choosing, so
        # the operator's command for one project would reach another.
        if self.name != self.root.name:
            raise FleetError(
                f"the name {self.name!r} is not the last step of the root {self.root}"
            )
        if self.socket.name != OPERATOR_SOCKET_NAME:
            raise FleetError(f"socket must name a file {OPERATOR_SOCKET_NAME}")
        if type(self.pid) is not int or self.pid <= 0:
            raise FleetError("pid must be a positive whole number")
        if not 0 < len(self.started) <= registry.MAX_STARTED_CHARS:
            raise FleetError("started must be a short text")
        if self.commit is not None and (
            not 0 < len(self.commit) <= MAX_COMMIT_CHARS
            or not set(self.commit) <= COMMIT_CHARS
        ):
            raise FleetError("commit must be a commit name in hexadecimal")

    @property
    def key(self) -> str:
        return entry_key(self.root)

    def text(self) -> str:
        """The entry as canonical JSON, as it is written."""
        return json.dumps(
            {
                "version": INDEX_VERSION,
                "name": self.name,
                "root": str(self.root),
                "socket": str(self.socket),
                "pid": self.pid,
                "started": self.started,
                "commit": self.commit,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


def check_name(name: str) -> None:
    """Refuse a project name that is empty, too long, or holds a control character or
    a slash, so that it prints on one row and never reads as a path."""
    if not name or len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise FleetError(f"a project name must be 1 to {MAX_NAME_BYTES} bytes")
    if "/" in name or any(ord(char) < 0x20 or ord(char) == 0x7F for char in name):
        raise FleetError("a project name holds a slash or a control character")


def entry_key(root: Path) -> str:
    """The file name of ``root``'s entry, a digest of the root, so one project has one
    entry whatever its name, and the name never becomes a path."""
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:32]


def parse_index_entry(text: str) -> IndexEntry:
    """The entry ``text`` holds. Raises ``FleetError`` when it is malformed."""
    try:
        document = json.loads(text)
    except ValueError as error:
        raise FleetError("the entry is not JSON") from error
    if not isinstance(document, dict):
        raise FleetError("the entry is not an object")
    fields = cast(dict[str, object], document)
    if frozenset(fields) != INDEX_KEYS:
        raise FleetError(f"the entry must hold exactly {sorted(INDEX_KEYS)}")
    if fields["version"] != INDEX_VERSION:
        raise FleetError(f"the entry is not version {INDEX_VERSION}")
    name, root, socket = fields["name"], fields["root"], fields["socket"]
    pid, started, commit = fields["pid"], fields["started"], fields["commit"]
    if not (
        isinstance(name, str)
        and isinstance(root, str)
        and isinstance(socket, str)
        and type(pid) is int
        and isinstance(started, str)
        and (commit is None or isinstance(commit, str))
    ):
        raise FleetError("an entry field has the wrong type")
    return IndexEntry(name, Path(root), Path(socket), pid, started, commit)


def index_directory(registry_dir: Path) -> Path:
    """The index inside the registry ``registry_dir``, as the docstring decides."""
    return registry_dir / INDEX_DIR_NAME


def configured_index(environ: Mapping[str, str]) -> Path:
    """The index this user's daemons and clients use, under ``DROMON_REGISTRY_DIR``
    when ``environ`` sets it. Raises ``FleetError`` for a malformed override, which
    never falls back to the real index."""
    try:
        return index_directory(registry.configured_directory(environ))
    except registry.RegistryError as error:
        raise FleetError(str(error)) from error


def denied_indexes(registry_dir: Path) -> tuple[Path, ...]:
    """The indexes every line is denied, the one in use and the default one, by their
    real paths, so a daemon given another index cannot let its lines write the one
    clients read."""
    reals = dict.fromkeys(
        os.path.realpath(each)
        for each in (
            index_directory(registry_dir),
            index_directory(registry.default_directory()),
        )
    )
    return tuple(Path(real) for real in reals)


def ensure_index(registry_dir: Path) -> Path:
    """Create the index in ``registry_dir`` with mode 0700 when it is missing, and
    return it. Raises ``FleetError`` when it cannot be made or is wider.

    **Review of 7ff033f, G5.** Codex's profile denies a protected path only when it
    exists, and Grok's check requires only those that exist, so the daemon makes the
    index before any engine is built, and every line's sandbox then denies it. Only
    the index in use is made. **Limit.** A daemon given ``DROMON_REGISTRY_DIR`` leaves
    the default index unmade, so a Codex or Grok line of such a daemon could make the
    default one, which a client without the override reads. Making it would reach
    outside the override, which no test may do."""
    directory = index_directory(registry_dir)
    try:
        registry.ensure_directory(directory)
    except registry.RegistryError as error:
        raise FleetError(f"the fleet index {directory}: {error}") from error
    return directory


def _entry_path(directory: Path, key: str) -> Path:
    return directory / f"{key}{registry.ENTRY_SUFFIX}"


def write_index_entry(directory: Path, entry: IndexEntry) -> None:
    """Write ``entry``, replacing any entry of its root. **Decided by core.** The
    daemon that starts last for a root wins, since one project runs one daemon under
    its lock, and an entry left by a daemon that died is stale anyway. Raises
    ``FleetError``."""
    try:
        registry.ensure_directory(directory)
        path = _entry_path(directory, entry.key)
        with registry.locked(directory):
            if path.is_symlink() or path.exists():
                path.unlink()
            registry.write_private(path, entry.text().encode("utf-8"))
    except (OSError, registry.RegistryError) as error:
        raise FleetError(f"the fleet index {directory}: {error}") from error


def remove_index_entry(directory: Path, entry: IndexEntry) -> bool:
    """Remove ``entry`` if the index still holds exactly it, so a daemon never removes
    a successor's. Says whether it removed one, and never raises."""
    try:
        registry.check_directory(directory)
        with registry.locked(directory):
            path = _entry_path(directory, entry.key)
            if read_index_entry(path) != entry:
                return False
            path.unlink()
            return True
    except (OSError, registry.RegistryError):
        return False


def read_index_entry(path: Path) -> IndexEntry | None:
    """The valid entry at ``path``, whose file name must be its root's key, or
    ``None``."""
    data = registry.read_private(path)
    if data is None:
        return None
    try:
        entry = parse_index_entry(data.decode("utf-8"))
    except (UnicodeDecodeError, FleetError):
        return None
    return entry if path.name == f"{entry.key}{registry.ENTRY_SUFFIX}" else None


def entry_of(directory: Path, root: Path) -> IndexEntry | None:
    """The valid entry the index holds for ``root``, or ``None``. Raises
    ``FleetError`` when the index fails its check."""
    if not os.path.lexists(directory):
        return None
    try:
        registry.check_directory(directory)
    except registry.RegistryError as error:
        raise FleetError(str(error)) from error
    return read_index_entry(_entry_path(directory, entry_key(root)))


def read_index(directory: Path) -> list[IndexEntry]:
    """Every valid entry, live or stale, by name and then root. An index not yet made
    is empty. Raises ``FleetError`` when it fails its check or holds more than
    ``registry.MAX_ENTRIES`` entries."""
    if not os.path.lexists(directory):
        return []
    try:
        registry.check_directory(directory)
    except registry.RegistryError as error:
        raise FleetError(str(error)) from error
    paths: list[Path] = []
    try:
        with os.scandir(directory) as listing:
            for item in listing:
                if not item.name.endswith(registry.ENTRY_SUFFIX):
                    continue
                if len(paths) >= registry.MAX_ENTRIES:
                    raise FleetError(
                        f"the fleet index {directory} holds more than"
                        f" {registry.MAX_ENTRIES} entries"
                    )
                paths.append(directory / item.name)
    except OSError as error:
        raise FleetError(f"the fleet index {directory} cannot be listed") from error
    found = [read_index_entry(path) for path in sorted(paths)]
    entries = [entry for entry in found if entry is not None]
    return sorted(entries, key=lambda entry: (entry.name, str(entry.root)))


class Liveness(StrEnum):
    LIVE = "live"
    STALE = "stale"
    """The pid has another start time, or no process has it."""
    UNKNOWN = "unknown"
    """The process table could not be read, so the entry is not trusted."""


def liveness(entry: IndexEntry, start_of: registry.StartOf) -> Liveness:
    """Whether ``entry``'s daemon still runs, by its pid and start time."""
    try:
        started = start_of(entry.pid)
    except Exception:  # noqa: BLE001, any failure to read the table is unknown
        return Liveness.UNKNOWN
    return Liveness.LIVE if started == entry.started else Liveness.STALE


@dataclass(frozen=True, slots=True)
class Indexed:
    entry: IndexEntry
    liveness: Liveness


def survey(directory: Path, start_of: registry.StartOf) -> list[Indexed]:
    """Every entry of the index with its liveness. Raises ``FleetError``."""
    return [
        Indexed(entry, liveness(entry, start_of)) for entry in read_index(directory)
    ]


def split_target(text: str, what: str) -> tuple[str, str]:
    """``PROJECT:ITEM`` split at its last colon, since no identifier holds one, so a
    project's root may be given as an absolute path. Raises ``FleetError``."""
    project, colon, item = text.rpartition(":")
    if not colon or not project or not item:
        raise FleetError(f"{text!r} is not PROJECT:{what}")
    return project, item


def resolve(found: list[Indexed], project: str) -> IndexEntry:
    """The live entry ``project`` names, by its name or its root. Raises
    ``FleetError`` when none does, when the one it names is stale or unknown, or when
    two live daemons share the name, which their roots then tell apart."""
    named = [
        indexed
        for indexed in found
        if project in (indexed.entry.name, str(indexed.entry.root))
    ]
    live = [indexed.entry for indexed in named if indexed.liveness is Liveness.LIVE]
    if len(live) == 1:
        return live[0]
    if len(live) > 1:
        roots = ", ".join(str(entry.root) for entry in live)
        raise FleetError(
            f"{project} names more than one daemon, {roots}. Give its root"
        )
    if not named:
        raise FleetError(f"no daemon in the fleet index is named {project}")
    states = sorted({indexed.liveness.value for indexed in named})
    raise FleetError(
        f"the index entry for {project} is {' and '.join(states)}, so nothing is sent"
    )


def verify(entry: IndexEntry, status: Mapping[str, object]) -> None:
    """Refuse a daemon whose ``status`` does not give the pid and project root that
    ``entry`` names, so a socket another daemon now holds is never taken for it. The
    entry's name is checked again here, though every entry is checked when it is made,
    so that the rule holds for the entry that is used, wherever it came from."""
    if (
        entry.name != entry.root.name
        or status.get("pid") != entry.pid
        or status.get("project") != str(entry.root)
    ):
        raise FleetError(
            f"the daemon answering {entry.socket} is not the one the index names for"
            f" {entry.name}, so nothing is sent"
        )


# The hosts of other machines, which the operator reaches through the operator's own
# SSH. Only the configuration is implemented, as docs/architecture/FLEET.md says.

HOSTS_FILE_ENV: Final = "DROMON_HOSTS_FILE"
HOST_KEYS: Final = frozenset({"destination", "remote_dromon", "remote_registry"})
MAX_HOSTS: Final = 64
MAX_HOSTS_FILE_BYTES: Final = 64 * 1024
MAX_DESTINATION_BYTES: Final = 255
DEFAULT_REMOTE_DROMON: Final = "dromon"


@dataclass(frozen=True, slots=True)
class Host:
    """A machine the operator reaches through the operator's own SSH. It holds no
    credential, and Dromon stores none. SSH finds the key, the agent and the known
    hosts as it always does."""

    name: str
    destination: str
    """What ``ssh`` is given as its destination, a host or an alias of the operator's
    SSH configuration, passed as one argument after ``--`` and never through a
    shell."""
    remote_dromon: str = DEFAULT_REMOTE_DROMON
    """The ``dromon`` command on the host."""
    remote_registry: str | None = None
    """The host's ``DROMON_REGISTRY_DIR``, or ``None`` for its default."""


def default_hosts_file() -> Path:
    home = pwd.getpwuid(os.getuid()).pw_dir
    return Path(home) / ".config" / "dromon" / "hosts.toml"


def configured_hosts_file(environ: Mapping[str, str]) -> Path:
    """``DROMON_HOSTS_FILE`` when ``environ`` sets it, which must be absolute, and
    the default otherwise. Raises ``FleetError``."""
    given = environ.get(HOSTS_FILE_ENV)
    if given is None:
        return default_hosts_file()
    try:
        registry.check_absolute(given, HOSTS_FILE_ENV)
    except registry.RegistryError as error:
        raise FleetError(str(error)) from error
    return Path(given)


def read_hosts(path: Path) -> dict[str, Host]:
    """The hosts ``path`` names, none when it is missing. Raises ``FleetError`` when
    it is not a regular file of this user that group and others cannot write, or is
    malformed, so a file another user could change never directs a command."""
    try:
        found = path.lstat()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise FleetError(f"the hosts file {path} cannot be read") from error
    if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid():
        raise FleetError(f"the hosts file {path} is not a regular file of this user")
    if stat.S_IMODE(found.st_mode) & 0o022:
        raise FleetError(
            f"the hosts file {path} may be written by group or others. Restrict it to"
            " 600 or 644"
        )
    if found.st_size > MAX_HOSTS_FILE_BYTES:
        raise FleetError(f"the hosts file {path} exceeds {MAX_HOSTS_FILE_BYTES} bytes")
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise FleetError(f"the hosts file {path}: {error}") from error
    return parse_hosts(document)


def parse_hosts(document: Mapping[str, object]) -> dict[str, Host]:
    """The ``[hosts.NAME]`` tables of ``document``. Raises ``FleetError``."""
    unknown = sorted(set(document) - {"hosts"})
    if unknown:
        raise FleetError(f"{unknown[0]} is not a key of the hosts file")
    tables = document.get("hosts", {})
    if not isinstance(tables, dict):
        raise FleetError("hosts must be a table of [hosts.NAME] tables")
    named = cast(dict[str, object], tables)
    if len(named) > MAX_HOSTS:
        raise FleetError(f"the hosts file names more than {MAX_HOSTS} hosts")
    hosts: dict[str, Host] = {}
    for name in sorted(named):
        hosts[name] = _host(name, named[name])
    return hosts


def _host(name: str, table: object) -> Host:
    if ID_PATTERN.fullmatch(name) is None:
        raise FleetError(f"hosts.{name} is not a name of lowercase letters and digits")
    if not isinstance(table, dict):
        raise FleetError(f"hosts.{name} must be a table")
    fields = cast(dict[str, object], table)
    unknown = sorted(set(fields) - HOST_KEYS)
    if unknown:
        raise FleetError(
            f"hosts.{name}.{unknown[0]} is not a known key. A host holds no credential"
        )
    destination = fields.get("destination")
    if not isinstance(destination, str) or not _plain(
        destination, MAX_DESTINATION_BYTES
    ):
        raise FleetError(
            f"hosts.{name}.destination must be a host or an SSH alias, without spaces"
            " or control characters, that does not begin with -"
        )
    remote = fields.get("remote_dromon", DEFAULT_REMOTE_DROMON)
    if not isinstance(remote, str) or not _plain(remote, registry.MAX_PATH_BYTES):
        raise FleetError(f"hosts.{name}.remote_dromon must be one command word")
    remote_registry = fields.get("remote_registry")
    if remote_registry is not None:
        if not isinstance(remote_registry, str):
            raise FleetError(f"hosts.{name}.remote_registry must be an absolute path")
        try:
            registry.check_absolute(remote_registry, f"hosts.{name}.remote_registry")
        except registry.RegistryError as error:
            raise FleetError(str(error)) from error
    return Host(name, destination, remote, remote_registry)


def _plain(text: str, most: int) -> bool:
    """Whether ``text`` is one word that no program reads as an option."""
    return (
        0 < len(text.encode("utf-8")) <= most
        and not text.startswith("-")
        and not any(char.isspace() or ord(char) < 0x20 for char in text)
    )
