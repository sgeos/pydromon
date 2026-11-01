"""A worktree's build inputs against the project's base, which ``dromon
sync-worktrees`` shows before it syncs. Round 14, from the audit's finding S1.

``uv sync`` installs the project itself, so it runs the build backend that the
worktree's ``pyproject.toml`` names, from the paths that file lists in
``backend-path``, and it installs whatever ``uv.lock`` records, from any index, URL or
git source the lock names. A line can write all three in its role's worktree. A sync
therefore runs code a line wrote, as the operator, outside any sandbox and with the
network. This module finds what differs from the base, so the operator sees it and
decides before the sync, as ``dromon land`` already shows its plan and asks.

Trust. Everything read here is written by a line and is untrusted. The diff is only
printed, never interpreted, and git runs without a shell, without external diff or
text conversion programs and without the file system monitor, which a repository's
configuration could otherwise name as a command to run.

Residual, stated. A file git ignores inside a backend path is not listed, since
listing ignored files would list every virtual environment and cache. A build backend
installed from an index through ``build-system.requires`` is shown only as the change
to ``pyproject.toml`` that names it. Porting note: ``tomllib`` is Python's own reader,
and a port needs a TOML reader that also rejects duplicate keys."""

import posixpath
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from dromon import land

BASE: Final = "origin/main"
"""What a worktree's build inputs are compared with, the remote base that every
branch merges into. A worktree whose inputs equal it needs no question."""

PROJECT_FILE: Final = "pyproject.toml"

MAX_BACKEND_PATHS: Final = 16
"""The most ``backend-path`` entries read. More is refused, so the pathspec is
bounded."""

MAX_SHOWN_LINES: Final = 400
"""The most diff lines printed for one worktree. The rest is counted, with the git
command that shows it whole."""

MAX_UNTRACKED_SHOWN: Final = 50

_GIT: Final = (
    "git",
    "--no-optional-locks",
    "--literal-pathspecs",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.quotePath=false",
)


class BuildInputsUnknown(Exception):
    """The build inputs cannot be compared, with the reason. The worktree is then not
    synced, since what the sync would run is not known."""


@dataclass(frozen=True, slots=True)
class Differences:
    """How a worktree's build inputs differ from ``BASE``."""

    paths: tuple[str, ...]
    """The pathspecs compared, ``pyproject.toml``, ``uv.lock`` and each backend path."""
    diff: str
    """``git diff BASE`` of those paths in the working tree, committed or not."""
    untracked: tuple[str, ...]
    """Files under those paths git does not track and does not ignore."""

    @property
    def differ(self) -> bool:
        return bool(self.diff.strip()) or bool(self.untracked)


def _backend_entry(entry: object, index: int) -> str:
    """One ``backend-path`` entry as a pathspec inside the worktree, or a refusal."""
    if not isinstance(entry, str) or not entry or "\x00" in entry:
        raise BuildInputsUnknown(
            f"{PROJECT_FILE} build-system.backend-path entry {index} is not a path"
        )
    normal = posixpath.normpath(entry.replace("\\", "/"))
    if normal.startswith("/") or normal == ".." or normal.startswith("../"):
        raise BuildInputsUnknown(
            f"{PROJECT_FILE} build-system.backend-path entry {entry!r} lies outside"
            " the worktree"
        )
    return normal


def build_paths(worktree: Path) -> tuple[str, ...]:
    """The paths whose content ``uv sync`` builds or installs from, ``pyproject.toml``,
    ``uv.lock`` and each ``backend-path`` directory, in that order without repeats.

    A ``pyproject.toml`` that cannot be read or parsed gives the two files alone,
    since uv cannot build from it either, and its diff is still shown. A backend path
    outside the worktree raises ``BuildInputsUnknown``."""
    found = [PROJECT_FILE, land.LOCK_FILE]
    try:
        parsed = tomllib.loads((worktree / PROJECT_FILE).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return tuple(found)
    system = parsed.get("build-system")
    entries: object = (
        cast(dict[str, object], system).get("backend-path", [])
        if isinstance(system, dict)
        else []
    )
    if not isinstance(entries, list):
        raise BuildInputsUnknown(
            f"{PROJECT_FILE} build-system.backend-path is not a list"
        )
    listed = cast(list[object], entries)
    if len(listed) > MAX_BACKEND_PATHS:
        raise BuildInputsUnknown(
            f"{PROJECT_FILE} lists more than {MAX_BACKEND_PATHS} backend paths"
        )
    for index, entry in enumerate(listed):
        path = _backend_entry(entry, index)
        if path not in found:
            found.append(path)
    return tuple(found)


def _git(run: land.Runner, worktree: Path, *args: str) -> str:
    done = run([*_GIT, *args], worktree)
    if done.code != 0:
        reason = (done.stderr or done.stdout).strip().splitlines()
        raise BuildInputsUnknown(
            f"git {args[0]} against {BASE} failed with status {done.code}:"
            f" {reason[-1] if reason else 'no output'}"
        )
    return done.stdout


def differences(worktree: Path, run: land.Runner = land.run_command) -> Differences:
    """Compare the worktree's build inputs with ``BASE``, in the working tree, so an
    uncommitted change counts, since ``uv sync`` reads the files as they are."""
    paths = build_paths(worktree)
    diff = _git(
        run,
        worktree,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        BASE,
        "--",
        *paths,
    )
    listed = _git(
        run, worktree, "ls-files", "--others", "--exclude-standard", "-z", "--", *paths
    )
    untracked = tuple(sorted(name for name in listed.split("\x00") if name))
    return Differences(paths, diff, untracked)


def show(found: Differences, worktree: Path, say: Callable[[str], None]) -> None:
    """Print the differences, bounded, with the command that shows them whole."""
    lines = found.diff.splitlines()
    for line in lines[:MAX_SHOWN_LINES]:
        say(f"    {line}")
    if len(lines) > MAX_SHOWN_LINES:
        say(
            f"    ... {len(lines) - MAX_SHOWN_LINES} more lines. See them with"
            f" git -C {worktree} diff {BASE} -- {' '.join(found.paths)}"
        )
    for name in found.untracked[:MAX_UNTRACKED_SHOWN]:
        say(f"    untracked: {name}")
    if len(found.untracked) > MAX_UNTRACKED_SHOWN:
        more = len(found.untracked) - MAX_UNTRACKED_SHOWN
        say(f"    ... {more} more untracked files")
