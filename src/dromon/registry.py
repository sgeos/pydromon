"""The per-user registry of daemons open to local federation.

A daemon whose project sets ``[federation] local = "open"`` registers itself here, so
that another daemon of the same user can name it, reach its ``peers.sock`` and tell
its connection from a line's. ``docs/architecture/FEDERATION.md`` is normative, under
open federation.

Location. **Decided by core, for the operator's review.** The directory is
``.local/state/dromon/registry`` under the home directory that the password database
gives the daemon's user, not ``HOME``. Every daemon of one user then agrees on it
whatever environment started it, since a sandbox or a service manager may set ``HOME``
or ``XDG_STATE_HOME`` differently. The path follows the convention for state of the
freedesktop.org base directory specification, and lies outside every project unless a
project's root holds the home directory, which the daemon refuses.

Trust. An entry is written by a daemon of the same user, and is read as that user's
statement, never as proof. The directory must be the user's own with mode 0700, and is
refused if wider, so another user can neither read nor write it. Each entry is a
regular file of mode 0600. What an entry proves rests on a line being unable to write
it, which every line of an open project is, since its sandbox denies the directory.
An unconfined process of the user can write an entry, as it can read a peer's token,
which is why the unconfined-engine rule applies once a project is open.

Staleness. An entry names its daemon's pid and the start time the process table gives
it. An entry whose pid now has another start time, or none, is stale and is ignored,
so a pid reused by another process is never taken for the daemon. A table that cannot
be read makes no entry live.

Porting note. Plain files and one advisory lock, ``flock`` on ``.lock``, which every
platform the port targets offers. The bounds on entries and their size keep each loop
and read bounded.
"""

import fcntl
import json
import os
import pwd
import stat
from collections.abc import Callable, Generator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from dromon.model import ID_PATTERN

REGISTRY_VERSION: Final = 1
DIRECTORY_MODE: Final = 0o700
ENTRY_MODE: Final = 0o600
LOCK_NAME: Final = ".lock"
ENTRY_SUFFIX: Final = ".json"
MAX_ENTRIES: Final = 256
"""The most entries the registry may hold, which bounds every read of it."""
MAX_ENTRY_BYTES: Final = 8192
MAX_PATH_BYTES: Final = 4096
PEER_SOCKET_NAME: Final = "peers.sock"
STATE_DIR_NAME: Final = ".dromon"
ENTRY_KEYS: Final = frozenset({"version", "name", "socket", "root", "pid", "started"})
MAX_STARTED_CHARS: Final = 64

type StartOf = Callable[[int], str | None]
"""When a pid started, by the process table, or ``None`` when no process has it.
``dromon.runs.process_start`` is the daemon's, and raises ``TableUnreadable`` when the
table cannot be read."""


class RegistryError(Exception):
    """The registry cannot be used, or an entry cannot be written, with the reason."""


@dataclass(frozen=True, slots=True)
class Entry:
    """One daemon open to local federation."""

    name: str
    """The federation name, ``[federation] name``, unique among live entries."""
    socket: Path
    """The daemon's ``peers.sock``, an absolute path."""
    root: Path
    """The daemon's project root, whose ``.dromon`` is denied to other projects'
    lines."""
    pid: int
    started: str
    """The daemon's start time as the process table gives it."""

    def __post_init__(self) -> None:
        if ID_PATTERN.fullmatch(self.name) is None:
            raise RegistryError(f"{self.name!r} is not a federation name")
        _check_path(self.socket, "socket")
        _check_path(self.root, "root")
        if self.socket.name != PEER_SOCKET_NAME:
            raise RegistryError(f"socket must name a file {PEER_SOCKET_NAME}")
        if type(self.pid) is not int or self.pid <= 0:
            raise RegistryError("pid must be a positive whole number")
        if not 0 < len(self.started) <= MAX_STARTED_CHARS:
            raise RegistryError("started must be a short text")

    def text(self) -> str:
        """The entry as canonical JSON, as it is written."""
        return json.dumps(
            {
                "version": REGISTRY_VERSION,
                "name": self.name,
                "socket": str(self.socket),
                "root": str(self.root),
                "pid": self.pid,
                "started": self.started,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )

    def directories(self) -> tuple[Path, Path]:
        """The daemon's runtime directory, which holds its sockets, and its state
        directory, which holds its event log and secrets."""
        return self.socket.parent, self.root / STATE_DIR_NAME


def _check_path(path: Path, name: str) -> None:
    text = str(path)
    if not text.startswith("/") or "\x00" in text:
        raise RegistryError(f"{name} must be an absolute path")
    if len(text.encode("utf-8")) > MAX_PATH_BYTES:
        raise RegistryError(f"{name} must be a path of at most {MAX_PATH_BYTES} bytes")
    if any(step in (".", "..") for step in text.split("/")[1:]):
        raise RegistryError(f"{name} has a . or .. step")


def parse_entry(text: str) -> Entry:
    """The entry ``text`` holds. Raises ``RegistryError`` when it is malformed."""
    try:
        document = json.loads(text)
    except ValueError as error:
        raise RegistryError("the entry is not JSON") from error
    if not isinstance(document, dict):
        raise RegistryError("the entry is not an object")
    fields = cast(dict[str, object], document)
    if frozenset(fields) != ENTRY_KEYS:
        raise RegistryError(f"the entry must hold exactly {sorted(ENTRY_KEYS)}")
    if fields["version"] != REGISTRY_VERSION:
        raise RegistryError(f"the entry is not version {REGISTRY_VERSION}")
    name, socket, root, pid, started = (
        fields["name"],
        fields["socket"],
        fields["root"],
        fields["pid"],
        fields["started"],
    )
    if not (
        isinstance(name, str)
        and isinstance(socket, str)
        and isinstance(root, str)
        and type(pid) is int
        and isinstance(started, str)
    ):
        raise RegistryError("an entry field has the wrong type")
    return Entry(name, Path(socket), Path(root), pid, started)


def default_directory() -> Path:
    """The registry of the daemon's user, under the home directory the password
    database gives, as the module's docstring decides."""
    home = pwd.getpwuid(os.getuid()).pw_dir
    return Path(home) / ".local" / "state" / "dromon" / "registry"


def check_directory(directory: Path) -> None:
    """Refuse a registry that is not a directory of this user with mode 0700, read
    with ``lstat`` so that a symbolic link is refused. Raises ``RegistryError``."""
    try:
        found = directory.lstat()
    except OSError as error:
        raise RegistryError(f"the registry {directory} cannot be read") from error
    if not stat.S_ISDIR(found.st_mode):
        raise RegistryError(f"the registry {directory} is not a directory")
    if found.st_uid != os.getuid():
        raise RegistryError(f"the registry {directory} is not this user's")
    mode = stat.S_IMODE(found.st_mode)
    if mode != DIRECTORY_MODE:
        raise RegistryError(
            f"the registry {directory} has mode {mode:o}, and must have mode"
            f" {DIRECTORY_MODE:o} so that only its owner reaches it"
        )


def ensure_directory(directory: Path) -> None:
    """Create the registry with mode 0700 when it is missing, then check it. A
    directory that exists with a wider mode is refused, never narrowed, since the
    operator may have widened it on purpose. Raises ``RegistryError``."""
    try:
        directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
    except OSError as error:
        raise RegistryError(f"the registry {directory} cannot be made") from error
    check_directory(directory)


@contextmanager
def _locked(directory: Path) -> Generator[None]:
    """Hold the registry's advisory lock, so two daemons registering one name at once
    cannot both take it, and a stale entry is replaced once."""
    try:
        descriptor = os.open(
            directory / LOCK_NAME,
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            ENTRY_MODE,
        )
    except OSError as error:
        raise RegistryError(f"the registry {directory} cannot be locked") from error
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def entry_path(directory: Path, name: str) -> Path:
    return directory / f"{name}{ENTRY_SUFFIX}"


def read_entry(directory: Path, name: str) -> Entry | None:
    """The entry for ``name``, or ``None`` when there is none or it is not a regular
    file of this user with mode 0600 holding a valid entry for that name. It is
    opened without following a link and without blocking, so a named pipe put there
    returns at once."""
    if ID_PATTERN.fullmatch(name) is None:
        return None
    try:
        descriptor = os.open(
            entry_path(directory, name),
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
        )
    except OSError:
        return None
    try:
        found = os.fstat(descriptor)
        if (
            not stat.S_ISREG(found.st_mode)
            or found.st_uid != os.getuid()
            or stat.S_IMODE(found.st_mode) != ENTRY_MODE
            or found.st_size > MAX_ENTRY_BYTES
        ):
            return None
        data = os.read(descriptor, MAX_ENTRY_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    try:
        entry = parse_entry(data.decode("utf-8"))
    except (UnicodeDecodeError, RegistryError):
        return None
    return entry if entry.name == name else None


def is_live(entry: Entry, start_of: StartOf) -> bool:
    """Whether ``entry``'s daemon still runs, by its pid and start time. Lets the
    table's error through, so a caller fails closed."""
    return start_of(entry.pid) == entry.started


def entries(directory: Path) -> list[Entry]:
    """Every valid entry, live or stale, in name order. Raises ``RegistryError`` when
    the directory fails its check or holds more than ``MAX_ENTRIES`` names, so a
    caller that must see every entry, as the protected directories must, fails
    closed rather than missing one."""
    check_directory(directory)
    names: list[str] = []
    try:
        with os.scandir(directory) as listing:
            for item in listing:
                if not item.name.endswith(ENTRY_SUFFIX):
                    continue
                if len(names) >= MAX_ENTRIES:
                    raise RegistryError(
                        f"the registry {directory} holds more than {MAX_ENTRIES}"
                        " entries"
                    )
                names.append(item.name.removesuffix(ENTRY_SUFFIX))
    except OSError as error:
        raise RegistryError(f"the registry {directory} cannot be listed") from error
    found = [read_entry(directory, name) for name in sorted(names)]
    return [entry for entry in found if entry is not None]


def find(directory: Path, name: str, pid: int, start_of: StartOf) -> Entry | None:
    """The live entry for ``name`` whose daemon is ``pid``, or ``None``. A table that
    cannot be read finds none."""
    try:
        check_directory(directory)
    except RegistryError:
        return None
    entry = read_entry(directory, name)
    if entry is None or entry.pid != pid:
        return None
    try:
        return entry if is_live(entry, start_of) else None
    except Exception:  # noqa: BLE001, any failure to read the table finds nothing
        return None


def live_entry(directory: Path, name: str, start_of: StartOf) -> Entry | None:
    """The live entry for ``name``, or ``None``, for a daemon that sends to it."""
    try:
        check_directory(directory)
    except RegistryError:
        return None
    entry = read_entry(directory, name)
    if entry is None:
        return None
    try:
        return entry if is_live(entry, start_of) else None
    except Exception:  # noqa: BLE001, any failure to read the table finds nothing
        return None


def register(directory: Path, entry: Entry, start_of: StartOf) -> None:
    """Write ``entry``, replacing a stale entry of its name. Raises ``RegistryError``
    when a live daemon holds the name, or when the table cannot say whether the
    holder lives, so a name is never taken from a daemon that may still run."""
    ensure_directory(directory)
    path = entry_path(directory, entry.name)
    with _locked(directory):
        if path.is_symlink() or path.exists():
            held = read_entry(directory, entry.name)
            if held is not None:
                try:
                    alive = is_live(held, start_of)
                except Exception as error:
                    raise RegistryError(
                        f"cannot tell whether pid {held.pid}, which holds the"
                        f" federation name {entry.name}, still runs"
                    ) from error
                if alive:
                    raise RegistryError(
                        f"the federation name {entry.name} is held by the live"
                        f" daemon of {held.root}, pid {held.pid}"
                    )
            try:
                path.unlink()
            except OSError as error:
                raise RegistryError(
                    f"the stale entry {path} cannot be removed"
                ) from error
        data = entry.text().encode("utf-8")
        try:
            descriptor = os.open(
                path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                ENTRY_MODE,
            )
        except OSError as error:
            raise RegistryError(f"the entry {path} cannot be written") from error
        try:
            os.fchmod(descriptor, ENTRY_MODE)
            os.write(descriptor, data)
            os.fsync(descriptor)
        except OSError as error:
            os.close(descriptor)
            with suppress(OSError):
                path.unlink()
            raise RegistryError(f"the entry {path} cannot be written") from error
        os.close(descriptor)


def unregister(directory: Path, entry: Entry) -> bool:
    """Remove ``entry`` if the registry still holds exactly it, so a daemon never
    removes a successor's entry. Says whether it removed one, and never raises."""
    try:
        check_directory(directory)
        with _locked(directory):
            if read_entry(directory, entry.name) != entry:
                return False
            entry_path(directory, entry.name).unlink()
            return True
    except (OSError, RegistryError):
        return False
