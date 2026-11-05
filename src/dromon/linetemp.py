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

Facts and hypotheses. **Fact**, observed live on 2026-11-01 under Claude Code with
Sonnet 5.5, the shell's ``TMPDIR`` is ``/tmp/claude-UID``, set by Claude Code and not
the directory Dromon names, while ``UV_CACHE_DIR`` is Dromon's. A here-document to
``$TMPDIR`` worked there. **Inference**, that directory is shared by every Claude Code
line of the user, so lines may read each other's temporary files in it, which Dromon
does not control. **Round 15, item 2.** The turn-start note therefore names both
directories and says which one the shell has, through ``claude_shell_tmpdir``. The
two directories survive the daemon, since removing them while a line's command runs
would fail that command, and ``sweep`` removes only what has gone unchanged for a
week. The sandbox also denies the line reads of the directory above its own, which
holds the other lines' directories, and lifts that for its own. **Fact**, observed
live, the denial of another line's directory holds and ``allowRead`` lifts it for the
line's own.

**Round 15, item 5. A byte bound, applied between turns.** The sandbox has no quota,
so the daemon measures each directory before a line's turn starts and, when it holds
more than ``MAX_TEMP_BYTES`` or ``MAX_CACHE_BYTES``, removes whole top-level entries,
oldest change first, until it holds at most half the bound. The line is told in that
turn's prompt what was removed. The measure is allocated bytes with ``os.lstat`` and no
following of links, at most ``MAX_WALK_ENTRIES`` entries, and a directory that holds
more than that is treated as over the bound, which fails safe. **Residual.** A line can
exceed the bound within a turn, as far as the disk allows, and the bound is checked
only at a turn start. A file in use by a command that outlives the turn can be removed,
which the line is told. Hard links to one file count once for each name. The bounds are
constants, not a project setting. **Inference.** Removing the oldest entries is safe for
a temporary directory and for a cache that can be rebuilt, and unsafe for a line that
keeps state there on purpose, which the sandbox note does not promise to keep.

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
CLAUDE_TMPDIR_PREFIX: Final = "/tmp/claude-"  # noqa: S108
MAX_TEMP_BYTES: Final = 2**30
"""The most a line's ``tmp`` directory may hold at a turn start, one gibibyte."""
MAX_CACHE_BYTES: Final = 4 * 2**30
"""The most a line's package cache may hold at a turn start, four gibibytes."""
MAX_WALK_ENTRIES: Final = 100_000
"""The most entries one measure visits, so the work is bounded. A directory with more
is treated as over its bound."""
MAX_TOP_ENTRIES: Final = 10_000
"""The most top-level entries one trim considers."""
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


def claude_shell_tmpdir(user: int) -> str:
    """The ``TMPDIR`` a Claude Code line's shell has, whatever Dromon sets, which
    Claude Code replaces with a directory of its own for the user. **Fact**, observed
    once, on 2026-11-01, so it is an observation of a Claude Code behaviour and not a
    contract. The status says it is observed, and a Claude Code that changes it makes
    the note wrong until this is updated."""
    return f"{CLAUDE_TMPDIR_PREFIX}{user}"


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

    **Size is bounded by ``trim``, not here.** The sandbox grants write access to a
    directory and has no quota, and the operating system gives an unprivileged process
    none for a directory on a shared file system, so the bound is applied by the
    daemon between turns, as ``trim`` says. What bounds the growth during a turn is
    nothing, and this sweep still removes what has gone unchanged for a week.

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


@dataclass(frozen=True, slots=True)
class Trimmed:
    """What ``trim`` did to one directory. ``removed`` is the entries taken out and
    ``freed`` the bytes they held, and ``incomplete`` says the walk limit was reached,
    so the sizes are lower bounds."""

    place: str
    bound: int
    removed: tuple[str, ...]
    freed: int
    incomplete: bool
    failed: tuple[str, ...]


def measure(directory: Path, limit: int | None = None) -> tuple[int, bool]:
    """The allocated bytes under ``directory`` and whether the walk was complete.
    Links are counted, not followed, and at most ``limit`` entries are visited. A
    missing directory holds nothing, and an unreadable one is counted as it was read."""
    bound = MAX_WALK_ENTRIES if limit is None else limit
    total = 0
    visited = 0
    pending = [directory]
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    visited += 1
                    if visited > bound:
                        return total, False
                    try:
                        found = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += _allocated(found)
                    if stat.S_ISDIR(found.st_mode):
                        pending.append(Path(entry.path))
        except OSError:
            continue
    return total, True


def _allocated(found: os.stat_result) -> int:
    blocks = getattr(found, "st_blocks", None)
    return blocks * 512 if isinstance(blocks, int) else found.st_size


def trim(place: Path, bound: int, user: int | None = None) -> Trimmed:
    """Keep ``place`` within ``bound`` bytes by removing top-level entries, oldest
    change first, until it holds at most half of ``bound``. Nothing is removed while it
    holds at most ``bound`` and the walk is complete. Links are unlinked, not followed,
    and an entry another user owns is left and reported. Considers at most
    ``MAX_TOP_ENTRIES`` entries, which leaves a directory with more of them over its
    bound, as the report says through ``failed``."""
    owner = os.getuid() if user is None else user
    total, complete = measure(place)
    if complete and total <= bound:
        return Trimmed(place.name, bound, (), 0, False, ())
    try:
        names = os.listdir(place)
    except OSError as error:
        return Trimmed(place.name, bound, (), 0, not complete, (f"{place}: {error}",))
    failed: list[str] = []
    if len(names) > MAX_TOP_ENTRIES:
        failed.append(
            f"{place}: more than {MAX_TOP_ENTRIES} entries, oldest considered"
        )
    dated: list[tuple[float, str]] = []
    for name in names:
        try:
            dated.append((os.lstat(place / name).st_mtime, name))
        except OSError as error:
            failed.append(f"{place / name}: {error.strerror}")
    dated.sort()
    removed: list[str] = []
    freed = 0
    target = bound // 2
    since, batch = 0, 1
    for _, name in dated[:MAX_TOP_ENTRIES]:
        if complete and total <= target:
            break
        path = place / name
        size, _ = measure(path)
        size += _entry_size(path)
        done: list[str] = []
        _remove(path, owner, done, failed)
        if done:
            removed.append(name)
            freed += size
            total = max(0, total - size)
        since += 1
        if not complete and since >= batch:
            # Measured again after 1, 2, 4 and so on removals, so the walk limit
            # bounds the work at a few measures and not one for each entry.
            total, complete = measure(place)
            since, batch = 0, batch * 2
    return Trimmed(
        place.name, bound, tuple(removed), freed, not complete, tuple(failed)
    )


def _entry_size(path: Path) -> int:
    try:
        return _allocated(os.lstat(path))
    except OSError:
        return 0


def trim_line(temp: LineTemp) -> tuple[Trimmed, Trimmed]:
    """Trim the line's temporary directory and its package cache to their bounds."""
    return trim(temp.temp, MAX_TEMP_BYTES), trim(temp.cache, MAX_CACHE_BYTES)


def trim_notice(found: tuple[Trimmed, ...]) -> str | None:
    """The sentence a turn's prompt carries when ``trim`` removed or failed to remove
    something, or ``None`` when it did neither. It names no file, since a name is text
    a command chose."""
    parts: list[str] = []
    for item in found:
        if item.removed:
            parts.append(
                f"{len(item.removed)} oldest entries of {item.place}"
                f" ({item.freed} bytes) because it held more than {item.bound} bytes"
                + (
                    ", counted as far as the walk limit allowed"
                    if item.incomplete
                    else ""
                )
            )
        elif item.incomplete or item.failed:
            parts.append(
                f"nothing from {item.place}, which is over its bound of {item.bound}"
                " bytes or could not be measured, and may fail your next write"
            )
    if not parts:
        return None
    return "Dromon removed " + "; ".join(parts) + ", before this turn."
