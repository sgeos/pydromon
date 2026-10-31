"""Whether two commits hold the same code, read with git and cached by commit.

**Round 9, item 4.** ``line_status`` called the daemon stale whenever the commit
checked out in its own checkout moved, and a commit that changed only documents moved
it as much as one that changed code. A line in a worktree of its own read the warning
as about its worktree and saw it all session. Staleness is therefore judged on the
daemon's code, the tree of its package directory, and the same comparison says when the
daemon runs code older than a line's worktree.

Cost. ``dromon.workdir`` reads ``HEAD`` from a few small files without running git, as
before, and this module runs git only for a commit it has not seen. Each answer is
cached by its commits, which a moved ``HEAD`` changes, so a prompt or a status costs no
process while ``HEAD`` stays. A call that times out is not cached, so it is asked again.

Trust. The commits come from ``read_head_commit``, which accepts only object names, and
the path is checked here, so no argument can be taken for an option. Git runs with the
daemon's authority in a directory the line may write, so its environment is cleared of
``GIT_`` variables, its optional locks are off, and it is given only commands that read
objects and refs: ``rev-parse`` and ``merge-base``. Neither reads the index or runs a
hook. **Assumption.** A line that can write its checkout's git configuration could
still steer them, as it could any git the operator runs, which is why every sandboxed
line is denied that file, as ``dromon.workdir`` describes.

Failure modes. Every failure, a missing git, a timeout, a commit the repository does not
hold, answers ``None``, which callers report as unknown, never as a difference.

Porting note. The process is started with ``subprocess.run``, which maps onto
``std::process::Command`` with a timeout in the Rust port.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Final

GIT_SECONDS: Final = 5.0
"""How long one git command may take before its answer counts as unknown."""

MAX_CACHED: Final = 256
"""The most answers kept, the oldest dropped first, so the cache stays bounded."""

OBJECT_NAME: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
PACKAGE_PATH: Final = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]{0,255}")


class _Timeout(Exception):
    """A git command ran past ``GIT_SECONDS``, so its answer is not cached."""


def _git(directory: Path, *arguments: str) -> subprocess.CompletedProcess[str] | None:
    """Run git in ``directory``, or ``None`` when it cannot be started. Raises
    ``_Timeout`` when it runs past ``GIT_SECONDS``."""
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(directory), *arguments],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=GIT_SECONDS,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise _Timeout from error
    except OSError:
        return None


def package_path(directory: Path) -> str | None:
    """The path of ``directory`` within its checkout, such as ``src/dromon``, or
    ``None`` outside a checkout or when git cannot say. The checkout's own top gives
    the empty path ``.``."""
    try:
        done = _git(directory, "rev-parse", "--show-prefix")
    except _Timeout:
        return None
    if done is None or done.returncode != 0:
        return None
    prefix = done.stdout.strip().rstrip("/") or "."
    if PACKAGE_PATH.fullmatch(prefix) is None or ".." in prefix.split("/"):
        return None
    return prefix


class CodeTrees:
    """The cached answers of git about the code at commits, for one daemon."""

    def __init__(self) -> None:
        self._trees: dict[tuple[str, str, str], str | None] = {}
        self._ancestry: dict[tuple[str, str, str], bool | None] = {}

    def tree(self, directory: Path, commit: str, path: str) -> str | None:
        """The object name of the tree at ``path`` in ``commit``, as the repository
        holding ``directory`` has it, or ``None`` when it cannot be read."""
        if OBJECT_NAME.fullmatch(commit) is None or not _valid_path(path):
            return None
        key = (str(directory), commit, path)
        if key in self._trees:
            return self._trees[key]
        spec = f"{commit}^{{tree}}" if path == "." else f"{commit}:{path}"
        try:
            done = _git(directory, "rev-parse", "--verify", "--quiet", spec)
        except _Timeout:
            return None
        found: str | None = None
        if done is not None and done.returncode == 0:
            name = done.stdout.strip()
            found = name if OBJECT_NAME.fullmatch(name) else None
        _remember(self._trees, key, found)
        return found

    def is_ancestor(self, directory: Path, older: str, newer: str) -> bool | None:
        """Whether ``older`` is an ancestor of ``newer`` or the same commit, in the
        repository holding ``directory``, or ``None`` when it cannot say, as for a
        commit that repository does not hold."""
        if OBJECT_NAME.fullmatch(older) is None or OBJECT_NAME.fullmatch(newer) is None:
            return None
        key = (str(directory), older, newer)
        if key in self._ancestry:
            return self._ancestry[key]
        try:
            done = _git(directory, "merge-base", "--is-ancestor", older, newer)
        except _Timeout:
            return None
        found = (
            None
            if done is None
            else True
            if done.returncode == 0
            else False
            if done.returncode == 1
            else None
        )
        _remember(self._ancestry, key, found)
        return found

    def same_code(
        self, directory: Path, first: str, second: str, path: str
    ) -> bool | None:
        """Whether ``first`` and ``second`` hold the same tree at ``path``, or ``None``
        when either cannot be read."""
        if first == second:
            return True
        before = self.tree(directory, first, path)
        after = self.tree(directory, second, path)
        if before is None or after is None:
            return None
        return before == after


def _valid_path(path: str) -> bool:
    return PACKAGE_PATH.fullmatch(path) is not None and ".." not in path.split("/")


def _remember[V](
    cache: dict[tuple[str, str, str], V], key: tuple[str, str, str], value: V
) -> None:
    """Keep ``value`` under ``key``, dropping the oldest answer past ``MAX_CACHED``."""
    cache[key] = value
    while len(cache) > MAX_CACHED:
        del cache[next(iter(cache))]
