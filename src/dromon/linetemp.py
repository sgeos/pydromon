"""A temporary directory and a package cache that a sandboxed line may write.

**Round 14, item 6.** A Claude Code line's shell runs inside a sandbox that writes
only the working directory and Claude Code's own temporary directory. Two commands
failed there for want of a writable place. A here-document needs a temporary file,
and refused to start when the variable ``TMPDIR`` named a directory the sandbox
denied, and ``uv`` wrote its cache under the home directory, so the project's gate
needed a workaround on every run. Dromon now makes one directory for each line,
outside the working directory, grants the sandbox writes to it, and sets ``TMPDIR``
and ``UV_CACHE_DIR`` to places inside it.

Why outside the working directory. A line with read access has its working directory
denied to writes, and no later layer can lift that, so a directory under it would not
be writable for the very lines that most need a temporary place. The system temporary
directory is writable and is where Claude Code itself keeps its own.

Trust. Another user of the machine could plant a directory or a symbolic link under
the system temporary directory at the name this module uses, to receive what a line
writes or to redirect it. Each directory is therefore made with mode ``0700``, opened
without following a link, and refused unless the daemon's user owns it and no other
user can reach it. A directory that fails the check is not used, and the line's
shell keeps the temporary directory it had, which the status says.

Facts and hypotheses. **Fact**, seen in a round 14 session, Claude Code sets the
shell's ``TMPDIR`` to a directory of its own under ``/tmp``, and its sandbox grants
that directory. **Hypothesis**, not verified live, a ``TMPDIR`` that Dromon sets is
overridden by Claude Code where it sets its own, in which case ``UV_CACHE_DIR`` and the
write grant still stand and only the here-document case depends on Claude Code. The
two directories survive the daemon, since removing them while a line's command runs
would fail that command, and ``sweep`` removes only what has gone unchanged for a
week. The sandbox also denies the line reads of the directory above its own, which
holds the other lines' directories, and lifts that for its own. Hypothesis, not
verified live, that ``allowRead`` lifts a ``denyRead`` of a directory above it.

Porting note. The directory is found with the operating system's temporary directory
and checked with ``os.lstat``, which matches POSIX only.
"""

import contextlib
import errno
import os
import re
import shutil
import stat
import tempfile
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import Final

NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
"""What a line identifier must look like for its name to be a path component."""

TEMP_NAME: Final = "tmp"
CACHE_NAME: Final = "uv-cache"
MAX_TEMP_AGE_SECONDS: Final = 7 * 24 * 3600
"""How long an entry in a known line's temporary directory may go unchanged before the
start sweep removes it."""
TEMP_VARIABLE: Final = "TMPDIR"
CACHE_VARIABLE: Final = "UV_CACHE_DIR"


class LineTempRefused(OSError):
    """The directory cannot be made or fails the ownership check, for the reason
    given."""


@dataclass(frozen=True, slots=True)
class LineTemp:
    """The directories one line's shell may write, outside its working directory."""

    root: Path
    temp: Path
    cache: Path

    def environment(self) -> dict[str, str]:
        """The variables that point a shell at the directories."""
        return {TEMP_VARIABLE: str(self.temp), CACHE_VARIABLE: str(self.cache)}

    def writable(self) -> list[str]:
        """The sandbox ``allowWrite`` entries, which are the directories themselves."""
        return [str(self.temp), str(self.cache)]

    def others_unreadable(self) -> list[str]:
        """The sandbox ``denyRead`` entry, the directory that holds every line's
        directory, so that a line cannot read what another line's commands wrote.
        Review of 2026-11-01, finding 3."""
        return [str(self.root.parent)]

    def readable(self) -> list[str]:
        """The sandbox ``allowRead`` entry that lifts the denial for the line's own
        directory, which its shell reads, as a here-document's file is."""
        return [str(self.root)]


def base_directory() -> Path:
    """The system temporary directory, resolved, which tests may replace."""
    return Path(os.path.realpath(tempfile.gettempdir()))


def prepare(line_id: str, base: Path | None = None) -> LineTemp:
    """Make the line's directories under ``base`` and return them. Each step is
    created or opened without following a link and checked, so a planted directory or
    link is refused, not used. Raises ``LineTempRefused``."""
    if NAME_PATTERN.fullmatch(line_id) is None or line_id in (".", ".."):
        raise LineTempRefused(f"line identifier {line_id!r} cannot name a directory")
    start = base_directory() if base is None else base
    user = os.getuid()
    names = (f"dromon-{user}", line_id)
    current = start
    for name in names:
        current = _own_directory(current, name, user)
    temp = _own_directory(current, TEMP_NAME, user)
    cache = _own_directory(current, CACHE_NAME, user)
    return LineTemp(current, temp, cache)


def _own_directory(parent: Path, name: str, user: int) -> Path:
    """``parent/name`` as a directory the daemon's user owns alone. It is created with
    mode ``0700`` when missing. A link, a file, another user's directory and one that
    a group or other can reach are refused."""
    path = parent / name
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise LineTempRefused(f"cannot make {path}: {error.strerror}") from error
    try:
        found = os.lstat(path)
    except OSError as error:
        raise LineTempRefused(f"cannot read {path}: {error.strerror}") from error
    if stat.S_ISLNK(found.st_mode):
        raise LineTempRefused(f"{path} is a symbolic link")
    if not stat.S_ISDIR(found.st_mode):
        raise LineTempRefused(f"{path} is not a directory")
    if found.st_uid != user:
        raise LineTempRefused(f"{path} belongs to another user")
    if found.st_mode & 0o077:
        raise LineTempRefused(f"{path} can be reached by another user")
    return path


def note_of(failure: OSError | None) -> str:
    """What the status says of the line's temporary place."""
    if failure is None:
        return "the line's own directories under the system temporary directory"
    reason = failure.strerror if failure.errno != errno.EPERM else "not permitted"
    return (
        f"not made, so the shell keeps its own temporary directory: {reason or failure}"
    )


@dataclass(frozen=True, slots=True)
class Swept:
    """What the start sweep removed, and what it could not."""

    removed: tuple[str, ...]
    failed: tuple[str, ...]


def sweep(
    known: Collection[str],
    base: Path | None = None,
    *,
    now: float | None = None,
    max_age: float = MAX_TEMP_AGE_SECONDS,
) -> Swept:
    """Remove stale entries from the line directories, and the directories of lines
    that no longer exist once they are empty, before the first engine of this run
    starts. **Review of 2026-11-01, finding 2.**

    In a known line's ``tmp`` directory each entry not changed for ``max_age`` seconds
    is removed, and its cache is left, since its purpose is to last. A directory whose
    name is not a line in ``known`` is treated alike in ``tmp`` and in the cache, and
    is then removed if both are empty. **Why age and not the line's absence alone.**
    ``dromon-UID`` is shared by every daemon of the user, so a project's daemon does
    not know the lines of another's. Removing a directory for being unknown would end
    the other daemon's commands, so only what has gone unchanged for a week is removed.
    **Residual.** A cache that a line only reads keeps its old modification times and
    is removed after a week, which costs its speed and nothing else.

    **There is no byte bound, and none is possible here.** The sandbox grants write
    access to a directory and has no quota, and the operating system gives an
    unprivileged process none for a directory on a shared file system. What bounds the
    growth is the age rule and this sweep at each start, and a line can still fill the
    disk between starts. **Residual.** The daemon does not measure the size, since a
    measure without an action would only report what a full disk reports already.

    The model has no event that retires or deletes a line, so no removal happens at
    one. The parent and each entry are checked with ``os.lstat`` and removed without
    following a link, a link being unlinked and not entered. A parent that does not
    exist is not an error, and one that is a link, or not the daemon's own, is left
    alone and reported. A failure to remove one entry is reported and does not stop
    the others."""
    start = base_directory() if base is None else base
    user = os.getuid()
    parent = start / f"dromon-{user}"
    try:
        found = os.lstat(parent)
    except FileNotFoundError:
        return Swept((), ())
    except OSError as error:
        return Swept((), (f"{parent}: {error.strerror}",))
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != user or found.st_mode & 0o077:
        return Swept((), (f"{parent}: not a private directory of this user",))
    clock = time.time() if now is None else now
    removed: list[str] = []
    failed: list[str] = []
    try:
        names = sorted(os.listdir(parent))
    except OSError as error:
        return Swept((), (f"{parent}: {error.strerror}",))
    for name in names:
        entry = parent / name
        owned = NAME_PATTERN.fullmatch(name) is not None and name in known
        places = (TEMP_NAME,) if owned else (TEMP_NAME, CACHE_NAME)
        for place in places:
            _sweep_stale(entry / place, user, clock - max_age, removed, failed)
        if not owned:
            _remove_if_empty(entry, (TEMP_NAME, CACHE_NAME), removed)
    return Swept(tuple(removed), tuple(failed))


def _sweep_stale(
    directory: Path, user: int, before: float, removed: list[str], failed: list[str]
) -> None:
    """Remove each entry of ``directory`` last changed before ``before``."""
    try:
        inside = sorted(os.listdir(directory))
    except FileNotFoundError:
        return
    except OSError as error:
        failed.append(f"{directory}: {error.strerror}")
        return
    for item in inside:
        path = directory / item
        try:
            changed = os.lstat(path).st_mtime
        except OSError as error:
            failed.append(f"{path}: {error.strerror}")
            continue
        if changed < before:
            _remove(path, user, removed, failed)


def _remove_if_empty(entry: Path, places: tuple[str, ...], removed: list[str]) -> None:
    """Remove ``entry`` and its ``places`` when nothing is left in any of them. One
    that still holds something keeps all of them, so a line's empty cache is not
    taken from under its commands."""
    for place in places:
        try:
            if os.listdir(entry / place):
                return
        except FileNotFoundError:
            continue
        except OSError:
            return
    for place in places:
        with contextlib.suppress(OSError):
            os.rmdir(entry / place)
    try:
        os.rmdir(entry)
    except OSError:
        return
    removed.append(str(entry))


def _remove(path: Path, user: int, removed: list[str], failed: list[str]) -> None:
    """Remove ``path`` whatever it is, without following a link, if the daemon's user
    owns it. A tree is removed by ``shutil.rmtree``, which does not follow links."""
    try:
        found = os.lstat(path)
        if found.st_uid != user:
            failed.append(f"{path}: belongs to another user")
            return
        if stat.S_ISDIR(found.st_mode):
            shutil.rmtree(path)
        else:
            os.unlink(path)
    except OSError as error:
        failed.append(f"{path}: {error.strerror or error}")
        return
    removed.append(str(path))
