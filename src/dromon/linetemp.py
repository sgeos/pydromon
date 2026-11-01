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
would fail that command, and they hold only what a line's own commands wrote.

Porting note. The directory is found with the operating system's temporary directory
and checked with ``os.lstat``, which matches POSIX only.
"""

import errno
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
"""What a line identifier must look like for its name to be a path component."""

TEMP_NAME: Final = "tmp"
CACHE_NAME: Final = "uv-cache"
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
