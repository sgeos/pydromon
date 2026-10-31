"""Whether two commits hold the same code, read with git and cached by commit.

**Round 9, item 4.** ``line_status`` called the daemon stale whenever the commit
checked out in its own checkout moved, and a commit that changed only documents moved
it as much as one that changed code. A line in a worktree of its own read the warning
as about its worktree and saw it all session. Staleness is therefore judged on the
daemon's code, the tree of its package directory, and the same comparison says when the
daemon runs code older than a line's worktree.

Cost. ``dromon.workdir`` reads ``HEAD`` from a few small files without running git, as
before, and this module runs git only for a question it has not answered. Each answer is
cached by its commits, which a moved ``HEAD`` changes, so a prompt or a status costs no
process while ``HEAD`` stays.

**Round 9 review, F4 and F6.** No caller waits on git. A question answers at once from
the cache, and a question not yet answered answers ``None`` and is queued for one
worker thread, which runs git off the event loop, so a git that hangs on a stalled
volume delays only that answer, never a status or a prompt. A definite answer is kept
until ``MAX_CACHED`` pushes it out. An answer git could not give, a timeout, a git that
could not be started, or an error status, is kept for ``RETRY_SECONDS`` and then asked
again, so a hung git is neither run at every poll nor taken as unknown until a restart.

Trust. The commits come from ``read_head_commit``, which accepts only object names, and
the path is checked here, so no argument can be taken for an option. Git runs with the
daemon's authority in a directory the line may write, so its environment is cleared of
``GIT_`` variables, its optional locks are off, and it is given only commands that read
objects and refs: ``rev-parse`` and ``merge-base``. Neither reads the index or runs a
hook. **Assumption.** A line that can write its checkout's git configuration could
still steer them, as it could any git the operator runs, which is why every sandboxed
line is denied that file, as ``dromon.workdir`` describes.

Failure modes. Every failure, a missing git, a timeout, a commit the repository does not
hold, and a question still queued, answers ``None``, which callers report as unknown,
never as a difference. The worker is a daemon thread, so it never holds the daemon's
exit. A git it is running when the daemon exits is left to end by itself, which a
command that only reads does without harm.

Porting note. The worker is a thread with a lock, which maps onto a ``std::thread``
with a channel in the Rust port, and onto an external effect of a bounded stream
processor in Keleusma. The process is started with ``subprocess.run``, which maps onto
``std::process::Command`` with a timeout.
"""

import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

GIT_SECONDS: Final = 5.0
"""How long one git command may take before its answer counts as unknown."""

RETRY_SECONDS: Final = 60.0
"""How long an answer git could not give is kept before it is asked again. A git that
hangs holds the worker for ``GIT_SECONDS`` at each attempt, so one attempt a minute for
each question keeps the worker idle most of the time and runs at most one such git at
a time, while a git that recovers is seen within a minute. A status poll every second
would otherwise start a git at every poll."""

MAX_CACHED: Final = 256
"""The most answers kept, the oldest dropped first, so the cache stays bounded."""

MAX_PENDING: Final = 16
"""The most questions queued for the worker. A question past it is not queued, and is
queued at a later call once the queue has room, so a burst of new commits cannot grow
the queue without bound."""

OBJECT_NAME: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
PACKAGE_PATH: Final = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]{0,255}")

type _Kind = Literal["tree", "ancestor", "package"]
type _Key = tuple[_Kind, str, str, str]
type _Value = str | bool | None


class _Timeout(Exception):
    """A git command ran past ``GIT_SECONDS``, so its answer is kept only for
    ``RETRY_SECONDS``."""


@dataclass(frozen=True, slots=True)
class _Answer:
    value: _Value
    expires: float | None
    """When the answer is asked again, or ``None`` for a definite answer."""


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


def _ask_package(directory: Path) -> tuple[str | None, bool]:
    """The path of ``directory`` within its checkout, and whether the answer is
    definite."""
    try:
        done = _git(directory, "rev-parse", "--show-prefix")
    except _Timeout:
        return None, False
    if done is None or done.returncode != 0:
        return None, False
    prefix = done.stdout.strip().rstrip("/") or "."
    if not _valid_path(prefix):
        return None, True
    return prefix, True


def _ask_tree(directory: Path, commit: str, path: str) -> tuple[str | None, bool]:
    """The tree at ``path`` in ``commit``, and whether the answer is definite. A
    commit the repository does not hold may yet be fetched, so it is asked again."""
    spec = f"{commit}^{{tree}}" if path == "." else f"{commit}:{path}"
    try:
        done = _git(directory, "rev-parse", "--verify", "--quiet", spec)
    except _Timeout:
        return None, False
    if done is None or done.returncode != 0:
        return None, False
    name = done.stdout.strip()
    return (name, True) if OBJECT_NAME.fullmatch(name) else (None, False)


def _ask_ancestor(directory: Path, older: str, newer: str) -> tuple[bool | None, bool]:
    """Whether ``older`` is an ancestor of ``newer``, and whether the answer is
    definite. Git's status is 0 for yes and 1 for no, and any other for a question it
    could not answer, such as for a commit the repository does not hold."""
    try:
        done = _git(directory, "merge-base", "--is-ancestor", older, newer)
    except _Timeout:
        return None, False
    if done is None or done.returncode not in (0, 1):
        return None, False
    return done.returncode == 0, True


class CodeTrees:
    """The cached answers of git about the code at commits, for one daemon. Every
    method answers at once, and ``None`` for a question git has not answered yet.

    Concurrency. The cache and the queue are shared by the caller's thread and the
    worker, under one lock that is never held while git runs."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._settled = threading.Condition(self._lock)
        self._answers: dict[_Key, _Answer] = {}
        self._queue: deque[_Key] = deque()
        self._queued: set[_Key] = set()
        self._running: _Key | None = None
        self._worker: threading.Thread | None = None

    def package_path(self, directory: Path) -> str | None:
        """The path of ``directory`` within its checkout, such as ``src/dromon``, or
        ``None`` outside a checkout or when git has not said. The checkout's own top
        gives the empty path ``.``."""
        found = self._lookup(("package", str(directory), "", ""))
        return found if isinstance(found, str) else None

    def tree(self, directory: Path, commit: str, path: str) -> str | None:
        """The object name of the tree at ``path`` in ``commit``, as the repository
        holding ``directory`` has it, or ``None`` when it has not been read."""
        if OBJECT_NAME.fullmatch(commit) is None or not _valid_path(path):
            return None
        found = self._lookup(("tree", str(directory), commit, path))
        return found if isinstance(found, str) else None

    def is_ancestor(self, directory: Path, older: str, newer: str) -> bool | None:
        """Whether ``older`` is an ancestor of ``newer`` or the same commit, in the
        repository holding ``directory``, or ``None`` when it cannot say yet, as for
        a commit that repository does not hold."""
        if OBJECT_NAME.fullmatch(older) is None or OBJECT_NAME.fullmatch(newer) is None:
            return None
        found = self._lookup(("ancestor", str(directory), older, newer))
        return found if isinstance(found, bool) else None

    def same_code(
        self, directory: Path, first: str, second: str, path: str
    ) -> bool | None:
        """Whether ``first`` and ``second`` hold the same tree at ``path``, or ``None``
        when either has not been read."""
        if first == second:
            return True
        before = self.tree(directory, first, path)
        after = self.tree(directory, second, path)
        if before is None or after is None:
            return None
        return before == after

    def settle(self, seconds: float) -> bool:
        """Wait up to ``seconds`` for every queued question to be answered, and say
        whether they were. For tests and for a caller off the event loop. The daemon
        itself never waits. The wait is on the real clock, not the injected one."""
        deadline = time.monotonic() + seconds
        with self._settled:
            while self._queue or self._running is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._settled.wait(left)
            return True

    def _lookup(self, key: _Key) -> _Value:
        """The answer kept for ``key``, or ``None`` after queueing the question when
        none is kept or the kept one has expired."""
        with self._lock:
            kept = self._answers.get(key)
            if kept is not None and (
                kept.expires is None or self._clock() < kept.expires
            ):
                return kept.value
            if (
                key not in self._queued
                and key != self._running
                and len(self._queue) < MAX_PENDING
            ):
                self._queue.append(key)
                self._queued.add(key)
                if self._worker is None:
                    self._worker = threading.Thread(
                        target=self._work, name="dromon-codetree", daemon=True
                    )
                    self._worker.start()
            return None if kept is None else kept.value

    def _work(self) -> None:
        """Answer queued questions until none is left, then end. Each pass takes one
        question, so the loop ends once the queue is empty, and a later miss starts a
        new worker."""
        while True:
            with self._lock:
                if not self._queue:
                    self._worker = None
                    self._settled.notify_all()
                    return
                key = self._queue.popleft()
                self._queued.discard(key)
                self._running = key
            value, definite = _answer(key)
            with self._lock:
                expires = None if definite else self._clock() + RETRY_SECONDS
                self._answers[key] = _Answer(value, expires)
                while len(self._answers) > MAX_CACHED:
                    del self._answers[next(iter(self._answers))]
                self._running = None
                self._settled.notify_all()


def _answer(key: _Key) -> tuple[_Value, bool]:
    """Ask git the question ``key`` names. Any failure is an answer that is not
    definite, so the worker never ends on one."""
    kind, directory, first, second = key
    try:
        match kind:
            case "package":
                return _ask_package(Path(directory))
            case "tree":
                return _ask_tree(Path(directory), first, second)
            case "ancestor":
                return _ask_ancestor(Path(directory), first, second)
    except Exception:  # noqa: BLE001
        # The worker's boundary. A failure here leaves the question unknown for
        # RETRY_SECONDS rather than ending the worker with questions queued.
        return None, False


def _valid_path(path: str) -> bool:
    return PACKAGE_PATH.fullmatch(path) is not None and ".." not in path.split("/")
