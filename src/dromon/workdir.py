"""A line's working directory, and the git files its sandbox must never write.

Working directory. Each line's engine runs in its ``workdir``, so that a line can work
in a git worktree of its own while the daemon's state stays in the project. The default
is the project root. A configured directory is resolved, a relative path against the
project root and ``~`` against the home directory, and must be an existing directory.
It may not lie inside or at the project's state or runtime directory, which hold the
event log, the project file and the daemon's socket, and it may not contain either of
them unless it is the project root itself. The check runs when a line is configured and
again whenever its engine is built, since the directory can vanish in between.
``docs/protocol/LINE_CONFIG.md`` is normative.

Git safety. A command that writes a repository's ``hooks`` or ``config`` plants code
that git later runs outside every sandbox, in the operator's own commit or push. So
every enabled Claude Code sandbox denies writing those files of the checkout its line
works in, whether or not the line was granted the git directory. The checkout's git
directories are found by reading ``.git`` and following ``gitdir:`` and ``commondir``,
as git does, without running git, so the check costs no process and cannot be steered
by the line's environment. For a linked worktree the pointers themselves are guarded
too, because rewriting either would let a line hand git a directory of its own making.

Failure modes. A workdir that does not resolve to an allowed directory raises
``ConfigError``. A ``.git`` file or ``commondir`` file that cannot be read or is
malformed raises ``SandboxError``, so the line's turn ends before any command runs
rather than running without the guard. Git itself refuses such a checkout, so nothing
legitimate is lost. ``core.hooksPath`` set in the operator's global configuration, and
``GIT_DIR`` in the environment, are outside this module's view.

Porting note. Path resolution uses ``os.path`` and matches POSIX semantics only. The
search for ``.git`` walks a finite list of parents, so it terminates.
"""

import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final, NoReturn

from dromon.lineconfig import ConfigError
from dromon.sandbox import SandboxError

MAX_POINTER_BYTES: Final = 4096
"""The largest ``.git`` or ``commondir`` file read. Git writes one short line."""

GITDIR_PREFIX: Final = "gitdir:"


def _refuse(message: str) -> NoReturn:
    raise ConfigError(message)


def _encloses(outer: Path, inner: Path) -> bool:
    """Whether ``inner`` is ``outer`` or lies inside it. Both are real paths."""
    return inner == outer or outer in inner.parents


def resolve_workdir(workdir: str | None, root: Path, protected: Sequence[Path]) -> Path:
    """The directory a line with ``workdir`` runs in, as a real path, or ``root``
    unchanged when it is ``None``. Raises ``ConfigError`` for a directory that does
    not exist, or that is, lies in or contains a ``protected`` directory, which only
    the project root itself may contain."""
    if workdir is None:
        return root
    if workdir == "~" or workdir.startswith("~/"):
        given = Path(os.path.expanduser(workdir))
    else:
        given = Path(workdir)
        if not given.is_absolute():
            given = root / given
    real = Path(os.path.realpath(given))
    if not real.is_dir():
        _refuse(f"workdir {workdir!r} is not an existing directory")
    project = Path(os.path.realpath(root))
    for directory in protected:
        guarded = Path(os.path.realpath(directory))
        if _encloses(guarded, real):
            _refuse(f"workdir {workdir!r} is inside a directory Dromon protects")
        if _encloses(real, guarded) and real != project:
            _refuse(f"workdir {workdir!r} contains a directory Dromon protects")
    return real


def _pointer(file: Path, prefix: str) -> str:
    """The path a git pointer file holds, after ``prefix``, or ``SandboxError``."""
    try:
        with file.open("rb") as handle:
            raw = handle.read(MAX_POINTER_BYTES + 1)
    except OSError as error:
        raise SandboxError(f"cannot read {file}: {error}") from error
    if len(raw) > MAX_POINTER_BYTES:
        raise SandboxError(f"{file} is larger than {MAX_POINTER_BYTES} bytes")
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise SandboxError(f"{file} is not UTF-8 text") from error
    if not text.startswith(prefix) or not text[len(prefix) :].strip():
        raise SandboxError(f"{file} does not name a git directory")
    if "\x00" in text:
        raise SandboxError(f"{file} contains a NUL character")
    if "\n" in text:
        raise SandboxError(f"{file} holds more than one path")
    return text[len(prefix) :].strip()


def _against(text: str, base: Path) -> Path:
    path = Path(text)
    return Path(os.path.realpath(path if path.is_absolute() else base / path))


def _git_dirs(directory: Path) -> tuple[Path, Path, Path, bool] | None:
    """The git directory, the common git directory, the ``.git`` entry and whether it
    is a linked worktree, for the checkout holding ``directory``, or ``None`` outside
    any checkout. Raises ``SandboxError`` for a pointer file that cannot be followed."""
    real = Path(os.path.realpath(directory))
    for checkout in (real, *real.parents):
        dot = checkout / ".git"
        if dot.is_dir():
            gitdir, linked = Path(os.path.realpath(dot)), False
        elif dot.is_file():
            gitdir = _against(_pointer(dot, GITDIR_PREFIX), checkout)
            linked = True
        elif os.path.lexists(dot):
            raise SandboxError(f"{dot} is neither a git directory nor a git file")
        else:
            continue
        commondir = gitdir / "commondir"
        common = (
            _against(_pointer(commondir, ""), gitdir) if commondir.is_file() else gitdir
        )
        return gitdir, common, dot, linked
    return None


def git_write_guards(directory: Path) -> tuple[Path, ...]:
    """The git files a sandboxed line working in ``directory`` must never write, as
    real paths, or nothing when ``directory`` is not in a git checkout.

    These are the common git directory's ``hooks`` and ``config``, and the worktree's
    own ``config.worktree``. For a linked worktree, whose ``.git`` is a file, they also
    include that file and the worktree's ``commondir``. Raises ``SandboxError`` for a
    pointer file that cannot be followed."""
    found = _git_dirs(directory)
    if found is None:
        return ()
    gitdir, common, dot, linked = found
    guards = {common / "hooks", common / "config", gitdir / "config.worktree"}
    if linked:
        guards |= {dot, gitdir / "commondir"}
    return tuple(sorted(guards))


def git_common_directory(directory: Path) -> Path | None:
    """The checkout's shared git directory, or ``None`` outside a checkout.

    For a linked worktree this is the common directory shared by all worktrees.
    Pointer files are read with the same bounds and validation as ``git_write_guards``.
    """
    found = _git_dirs(directory)
    return None if found is None else found[1]


def git_directories(directory: Path) -> tuple[Path, Path] | None:
    """The checkout's worktree and common git directories, or ``None`` outside git.

    A linked worktree has a private directory and a shared common directory. An
    ordinary checkout uses the same directory for both. Pointer files are read with
    the bounds and validation of ``_git_dirs``.
    """
    found = _git_dirs(directory)
    return None if found is None else (found[0], found[1])


OBJECT_NAME: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
"""A commit's object name, SHA-1 or SHA-256, in lowercase hexadecimal."""

REF_NAME: Final = re.compile(r"refs/[A-Za-z0-9._/-]{1,255}")


def _small_text(file: Path) -> str | None:
    try:
        with file.open("rb") as handle:
            raw = handle.read(MAX_POINTER_BYTES + 1)
        if len(raw) > MAX_POINTER_BYTES:
            return None
        return raw.decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None


def read_head_commit(directory: Path) -> str | None:
    """The commit the checkout holding ``directory`` has checked out, read from its
    ``HEAD``, its loose refs and ``packed-refs`` without running git, or ``None`` when
    it is not in a checkout or the commit cannot be read. Never raises, because the
    answer is informational. A ref name with ``..`` or outside ``refs/`` is not
    followed."""
    try:
        found = _git_dirs(directory)
    except SandboxError:
        return None
    if found is None:
        return None
    gitdir, common, _, _ = found
    head = _small_text(gitdir / "HEAD")
    if head is None:
        return None
    if OBJECT_NAME.fullmatch(head):
        return head
    if not head.startswith("ref:"):
        return None
    ref = head.removeprefix("ref:").strip()
    if REF_NAME.fullmatch(ref) is None or ".." in ref:
        return None
    for base in (gitdir, common):
        value = _small_text(base / ref)
        if value is not None and OBJECT_NAME.fullmatch(value):
            return value
    try:
        with (common / "packed-refs").open("rb") as handle:
            packed = handle.read(1024 * 1024).decode("utf-8", "replace")
    except OSError:
        return None
    for line in packed.splitlines():
        name, _, value = line.partition(" ")
        if value == ref and OBJECT_NAME.fullmatch(name):
            return name
    return None
