"""Staleness judged on the daemon's code, and a daemon older than a line's worktree.

Round 9, item 4, and its review, F4, F5 and F6. Git runs only in scratch repositories
under ``tmp_path``, or is replaced by a fake. Git's answers come from a worker thread,
so a test asks, lets the worker settle, and asks again."""

import asyncio
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Generator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import codetree
from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog
from dromon.limits import Limits
from dromon.model import LineAddress, OperatorAddress
from dromon.scheduler import LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
START = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
SETTLE_SECONDS = codetree.MAX_PENDING * codetree.GIT_SECONDS
"""How long a test waits for the worker, derived from the worker's own bounds. The
worker takes at most ``MAX_PENDING`` questions at a time and a git that is running
answers within ``GIT_SECONDS``, so a worker that has answered nothing in their product
has not merely been slow. The wait ends at once when the questions are answered, so the
bound costs time only when the test would fail.

**Round 14 follow-up.** A fixed 30 seconds failed once on a macOS runner with Python
3.15 and passed on a re-run. **Hypothesis.** A git process that was slow to start, which
``GIT_SECONDS`` does not bound because ``subprocess.run`` starts its timer after the
process exists, held the worker past 30 seconds. The same runner stalled a process start
past five minutes in another test, which is the evidence for it, and the failed log was
not read here. The check is unchanged, since the worker must still answer, and only how
long the test waits to see it is derived."""
STATUS_BOUND_SECONDS = 0.5
"""How long a status may take while git hangs, a tenth of ``GIT_SECONDS``."""


def git(directory: Path, *arguments: str) -> str:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    done = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(directory),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env=environment,
    )
    return done.stdout.strip()


def commit(repository: Path, path: str, text: str, message: str) -> str:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", message)
    return git(repository, "rev-parse", "HEAD")


class Repository:
    """A scratch repository whose commits are, in order, the loaded code, a change
    of documents only, and a change of code."""

    def __init__(self, root: Path, seed: str = "one\n") -> None:
        root.mkdir(parents=True)
        git(root, "init", "-q")
        (root / ".dromon").mkdir()
        self.root = root
        # Two repositories made alike within a second share their commits, so the
        # seed tells them apart.
        self.loaded = commit(root, "src/dromon/a.py", seed, "code")
        self.docs = commit(root, "docs/notes.md", "notes\n", "docs")
        self.code = commit(root, "src/dromon/a.py", "two\n", "more code")

    def checkout(self, revision: str) -> None:
        git(self.root, "checkout", "-q", "--detach", revision)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    return Repository(tmp_path.resolve() / "project")


class Quiet:
    """An engine that records each prompt and reports nothing of note."""

    def __init__(self, spec: LineSpec) -> None:
        self.session_id = spec.session_id
        self.prompts: list[str] = []

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        return TurnReport("success", 1, "ok", (), None, None, "quiet", None, 0.1)


class World:
    def __init__(self, path: Path, root: Path, daemon_commit: str) -> None:
        self.log = EventLog(path)
        self.engines: list[Quiet] = []
        self.scheduler = Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            wall_clock=lambda: START,
            limits=Limits(max_autonomous_turns=20, max_cost_usd=10.0),
            root=root,
            protected=(root / ".dromon",),
            daemon_commit=daemon_commit,
            daemon_checkout=root / "src" / "dromon",
        )
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

    @property
    def trees(self) -> codetree.CodeTrees:
        return self.scheduler._code_trees  # pyright: ignore[reportPrivateUsage]

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Quiet:
        del tools
        engine = Quiet(spec)
        self.engines.append(engine)
        return engine

    def run(self, body: str) -> None:
        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            self.scheduler.send(ANA, ALPHA, body)
            await asyncio.sleep(0)
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(go())

    def last_prompt(self) -> str:
        return self.engines[-1].prompts[-1]


def asked[T](trees: codetree.CodeTrees, call: Callable[[], T]) -> T:
    """``call``'s answer once git has answered every question it raised. Each
    question can raise the next, as ancestry follows the package path, so the call
    is repeated until it queues nothing, a bounded number of times."""
    for _ in range(8):
        value = call()
        if trees.settle(0):
            return value
        assert trees.settle(SETTLE_SECONDS), "git's worker never answered"
    raise AssertionError("the call kept raising questions")


@pytest.fixture
def stale_world(repository: Repository) -> Generator[World]:
    target = World(
        repository.root.parent / "events.sqlite", repository.root, repository.loaded
    )
    yield target
    target.log.close()


# The cache of git's answers.


def test_code_trees_compare_code_and_ancestry_and_refuse_bad_names(
    repository: Repository,
) -> None:
    trees = codetree.CodeTrees()
    root = repository.root
    package = root / "src" / "dromon"
    assert asked(trees, lambda: trees.package_path(package)) == "src/dromon"
    assert asked(trees, lambda: trees.package_path(root)) == "."
    loaded, docs, code = repository.loaded, repository.docs, repository.code
    assert asked(trees, lambda: trees.same_code(root, loaded, docs, "src/dromon"))
    assert not asked(trees, lambda: trees.same_code(root, loaded, code, "src/dromon"))
    assert asked(trees, lambda: trees.is_ancestor(root, loaded, code)) is True
    assert asked(trees, lambda: trees.is_ancestor(root, code, loaded)) is False
    assert asked(trees, lambda: trees.is_ancestor(root, "f" * 40, code)) is None
    # Refused before any question is queued.
    assert trees.tree(root, "--help", "src") is None
    assert trees.tree(root, loaded, "../outside") is None
    assert trees.tree(root, loaded, "-x") is None
    assert trees.settle(0)


def test_git_variables_in_the_daemons_environment_do_not_change_an_answer(
    repository: Repository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 9 re-review, mutation H. ``codetree._git`` clears every ``GIT_``
    variable, so a ``GIT_DIR`` or ``GIT_INDEX_FILE`` in the daemon's environment
    cannot point its questions at another repository. Here they name a decoy, which
    an unfiltered git does answer from, and the answers for the real repository are
    those it gives with neither variable set."""
    root = repository.root
    package = root / "src" / "dromon"
    loaded, docs, code = repository.loaded, repository.docs, repository.code

    def answers() -> tuple[object, ...]:
        trees = codetree.CodeTrees()
        return (
            asked(trees, lambda: trees.package_path(package)),
            asked(trees, lambda: trees.same_code(root, loaded, docs, "src/dromon")),
            asked(trees, lambda: trees.same_code(root, loaded, code, "src/dromon")),
            asked(trees, lambda: trees.is_ancestor(root, loaded, code)),
            asked(trees, lambda: trees.is_ancestor(root, code, loaded)),
        )

    for name in [name for name in os.environ if name.startswith("GIT_")]:
        monkeypatch.delenv(name)
    clean = answers()
    assert clean == ("src/dromon", True, False, True, False)
    decoy = Repository(tmp_path.resolve() / "decoy", seed="decoy\n")
    monkeypatch.setenv("GIT_DIR", str(decoy.root / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy.root / ".git" / "index"))
    # The variables take effect for a git that keeps them, so the test would see
    # the filter go.
    unfiltered = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "rev-parse", "HEAD"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert unfiltered.stdout.strip() == decoy.code != repository.code
    assert answers() == clean


def test_an_answer_is_given_without_waiting_and_kept(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    real = codetree._git  # pyright: ignore[reportPrivateUsage]

    def counted(directory: Path, *arguments: str) -> Any:  # noqa: ANN401
        calls.append(arguments)
        return real(directory, *arguments)

    monkeypatch.setattr(codetree, "_git", counted)
    trees = codetree.CodeTrees()
    root, loaded, code = repository.root, repository.loaded, repository.code
    # The first call answers at once, unknown, and queues both trees.
    assert trees.same_code(root, loaded, code, "src") is None
    assert trees.settle(SETTLE_SECONDS)
    for _ in range(3):
        assert trees.same_code(root, loaded, code, "src") is False
    assert len(calls) == 2


@pytest.mark.parametrize("failure", ["timeout", "no git", "error status"])
def test_an_answer_git_could_not_give_is_kept_for_a_bounded_time(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """Round 9 review, F6. A timeout, a git that could not be started, and an error
    status are each kept for ``RETRY_SECONDS``, not forever and not never, so a hung
    git is not started at every poll, and a git that recovers is asked again."""
    calls: list[tuple[str, ...]] = []
    real = codetree._git  # pyright: ignore[reportPrivateUsage]

    def failing(directory: Path, *arguments: str) -> Any:  # noqa: ANN401
        calls.append(arguments)
        if failure == "timeout":
            raise codetree._Timeout  # pyright: ignore[reportPrivateUsage]
        if failure == "no git":
            return None
        return subprocess.CompletedProcess(arguments, 128, "", "fatal")

    monkeypatch.setattr(codetree, "_git", failing)
    now = [1000.0]
    trees = codetree.CodeTrees(clock=lambda: now[0])
    root, docs = repository.root, repository.docs
    assert trees.tree(root, docs, "src") is None
    assert trees.settle(SETTLE_SECONDS)
    assert len(calls) == 1
    now[0] += codetree.RETRY_SECONDS - 1
    assert trees.tree(root, docs, "src") is None
    assert trees.settle(0) and len(calls) == 1
    monkeypatch.setattr(codetree, "_git", real)
    now[0] += 2
    # Past the bound, the question is asked again, and the real git answers.
    assert asked(trees, lambda: trees.tree(root, docs, "src")) is not None
    assert len(calls) == 1


def test_a_failure_inside_the_worker_leaves_the_answer_unknown(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(directory: Path, *arguments: str) -> Any:  # noqa: ANN401
        raise RuntimeError("unexpected")

    monkeypatch.setattr(codetree, "_git", broken)
    trees = codetree.CodeTrees()
    root, loaded, code = repository.root, repository.loaded, repository.code
    assert asked(trees, lambda: trees.is_ancestor(root, loaded, code)) is None
    # The worker ended cleanly, and a later question is still answered.
    assert trees.tree(root, loaded, "src") is None
    assert trees.settle(SETTLE_SECONDS)


def test_code_trees_stay_bounded(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    def answering(directory: Path, *arguments: str) -> Any:  # noqa: ANN401
        return subprocess.CompletedProcess(arguments, 0, "a" * 40 + "\n", "")

    monkeypatch.setattr(codetree, "_git", answering)
    trees = codetree.CodeTrees()
    for index in range(codetree.MAX_CACHED + 3):
        trees.tree(repository.root, f"{index:040x}", "src")
        assert trees.settle(SETTLE_SECONDS)
    assert len(trees._answers) == codetree.MAX_CACHED  # pyright: ignore[reportPrivateUsage]


class Hung:
    """A git that sleeps until it is released, or for ``hold`` seconds at most, which is
    ``GIT_SECONDS`` unless a test asks for longer, as a git on a stalled volume does
    before its timeout. It records that it was asked and whether it has answered."""

    def __init__(self, hold: float = codetree.GIT_SECONDS) -> None:
        self.release = threading.Event()
        self.entered = threading.Event()
        self.answered = False
        self._hold = hold
        # Taken before the test replaces it with this fake.
        self.real = codetree._git  # pyright: ignore[reportPrivateUsage]

    def __call__(self, directory: Path, *arguments: str) -> Any:  # noqa: ANN401
        self.entered.set()
        try:
            if not self.release.wait(self._hold):
                raise codetree._Timeout  # pyright: ignore[reportPrivateUsage]
            return self.real(directory, *arguments)
        finally:
            self.answered = True


def test_the_queue_of_questions_is_bounded(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    hung = Hung()
    monkeypatch.setattr(codetree, "_git", hung)
    trees = codetree.CodeTrees()
    try:
        for index in range(codetree.MAX_PENDING * 2):
            assert trees.tree(repository.root, f"{index:040x}", "src") is None
        queued = len(trees._queue)  # pyright: ignore[reportPrivateUsage]
        assert queued <= codetree.MAX_PENDING
    finally:
        hung.release.set()
    assert trees.settle(SETTLE_SECONDS)


def test_a_status_never_waits_on_a_hung_git(
    repository: Repository, stale_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 9 review, F4. While git hangs, ``daemon_status``, ``commits`` and the
    line's ``line_status`` answer within a small bound, say that the code is not
    compared, and say what the code shows once git answers."""
    hung = Hung()
    monkeypatch.setattr(codetree, "_git", hung)
    scheduler = stale_world.scheduler
    tool = next(t for t in scheduler.own_tools("alpha") if t.name == "line_status")

    async def line_status() -> dict[str, Any]:
        return await tool.handler({})

    repository.checkout(repository.docs)
    try:
        for _ in range(3):
            began = time.monotonic()
            status = scheduler.daemon_status()
            commits = scheduler.commits("alpha")
            compact = asyncio.run(line_status())
            assert time.monotonic() - began < STATUS_BOUND_SECONDS
            assert (status["stale"], status["code_compared"]) == (True, False)
            assert "has not been read from git" in str(status["stale_note"])
            assert commits["daemon_behind_worktree"] is None
            assert compact["content"]
    finally:
        hung.release.set()
    status = asked(stale_world.trees, scheduler.daemon_status)
    assert (status["stale"], status["code_compared"]) == (False, True)


# Staleness and the daemon behind a worktree.


def test_the_daemon_is_stale_only_when_its_code_moved(
    repository: Repository, stale_world: World
) -> None:
    """Decided by core for review. A move of documents is not stale, a move of code
    is, and moving back to the loaded code clears it."""
    scheduler = stale_world.scheduler
    # The fixture's checkout is at the code change, which the daemon has not loaded.
    status = asked(stale_world.trees, scheduler.daemon_status)
    assert status["checkout_commit"] == repository.code
    assert (status["stale"], status["code_compared"], status["package"]) == (
        True,
        True,
        "src/dromon",
    )
    note = str(status["stale_note"])
    assert "own checkout, not your worktree" in note and "src/dromon" in note
    repository.checkout(repository.docs)
    status = asked(stale_world.trees, scheduler.daemon_status)
    assert (status["stale"], status["code_compared"]) == (False, True)
    assert status["stale_note"] is None
    repository.checkout(repository.loaded)
    assert scheduler.daemon_status()["stale"] is False


def test_staleness_falls_back_to_a_moved_head_when_code_cannot_be_compared(
    repository: Repository, stale_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unknown(directory: Path) -> tuple[str | None, bool]:
        del directory
        return None, False

    monkeypatch.setattr(codetree, "_ask_package", unknown)
    repository.checkout(repository.docs)
    status = asked(stale_world.trees, stale_world.scheduler.daemon_status)
    assert (status["stale"], status["code_compared"]) == (True, False)
    assert "has not been read from git" in str(status["stale_note"])
    cite = stale_world.scheduler.commits("alpha")["cite"]
    assert str(cite).endswith("with its code not compared.")


def test_line_status_marks_a_daemon_older_than_the_worktree(
    repository: Repository, stale_world: World
) -> None:
    """The line works at the project root, the daemon's own repository. At the code
    change the daemon is behind it, and at the change of documents it is not."""
    scheduler = stale_world.scheduler
    repository.checkout(repository.code)
    commits = asked(stale_world.trees, lambda: scheduler.commits("alpha"))
    assert commits["daemon_behind_worktree"] is True
    assert "is older than the worktree's" in str(commits["cite"])
    full = cast(dict[str, Any], scheduler.line_status("alpha"))
    tool = next(t for t in scheduler.own_tools("alpha") if t.name == "line_status")

    async def status() -> dict[str, Any]:
        return await tool.handler({})

    compact = json.loads(cast(str, asyncio.run(status())["content"][0]["text"]))
    assert compact["commits"]["daemon_behind_worktree"] is True
    assert compact["commits"]["cite"] == full["commits"]["cite"]
    repository.checkout(repository.docs)
    behind = asked(stale_world.trees, lambda: scheduler.daemon_behind("alpha"))
    assert behind is False
    repository.checkout(repository.loaded)
    assert scheduler.daemon_behind("alpha") is False


def test_a_worktree_of_another_repository_is_not_compared(
    tmp_path: Path, repository: Repository
) -> None:
    other = Repository(tmp_path.resolve() / "other", seed="another\n")
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(
            log,
            lambda spec, tools: Quiet(spec),
            root=other.root,
            protected=(other.root / ".dromon",),
            daemon_commit=repository.loaded,
            daemon_checkout=repository.root / "src" / "dromon",
        )
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        trees = scheduler._code_trees  # pyright: ignore[reportPrivateUsage]
        assert asked(trees, lambda: scheduler.daemon_behind("alpha")) is None


def test_a_worktree_that_does_not_descend_from_the_daemon_is_not_compared(
    tmp_path: Path, repository: Repository
) -> None:
    """Round 9 review, F5, found by its probe. A worktree on a branch unrelated to
    the daemon's commit, with other code under the package, and a worktree at a
    commit older than the daemon's, are neither behind nor not behind, so the
    answer is ``None``, where it was ``False``."""
    root = repository.root
    git(root, "checkout", "-q", "--orphan", "unrelated")
    git(root, "rm", "-rq", "--cached", ".")
    unrelated = commit(root, "src/dromon/a.py", "elsewhere\n", "unrelated code")
    assert unrelated not in (repository.loaded, repository.code)
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(
            log,
            lambda spec, tools: Quiet(spec),
            root=root,
            protected=(root / ".dromon",),
            daemon_commit=repository.loaded,
            daemon_checkout=root / "src" / "dromon",
        )
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        trees = scheduler._code_trees  # pyright: ignore[reportPrivateUsage]
        assert asked(trees, lambda: scheduler.daemon_behind("alpha")) is None
        commits = asked(trees, lambda: scheduler.commits("alpha"))
        assert commits["daemon_behind_worktree"] is None
        assert "is older than the worktree's" not in str(commits["cite"])
    with EventLog(tmp_path / "older.sqlite") as log:
        scheduler = Scheduler(
            log,
            lambda spec, tools: Quiet(spec),
            root=root,
            protected=(root / ".dromon",),
            daemon_commit=repository.code,
            daemon_checkout=root / "src" / "dromon",
        )
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        repository.checkout(repository.loaded)
        trees = scheduler._code_trees  # pyright: ignore[reportPrivateUsage]
        assert asked(trees, lambda: scheduler.daemon_behind("alpha")) is None


def test_the_prompt_says_once_per_commit_that_the_daemon_is_older(
    repository: Repository, stale_world: World
) -> None:
    """The notice needs git's answer, which a prompt does not wait for, so the test
    lets git answer before each turn. Round 9 review, F4."""
    scheduler = stale_world.scheduler
    repository.checkout(repository.code)
    asked(stale_world.trees, lambda: scheduler.daemon_behind("alpha"))
    stale_world.run("one")
    told = "The daemon runs code from"
    assert told in stale_world.last_prompt()
    assert "src/dromon" in stale_world.last_prompt()
    stale_world.run("two")
    assert told not in stale_world.last_prompt()
    newer = commit(repository.root, "src/dromon/b.py", "three\n", "newer code")
    asked(stale_world.trees, lambda: scheduler.daemon_behind("alpha"))
    stale_world.run("three")
    assert told in stale_world.last_prompt() and newer[:12] in stale_world.last_prompt()


def test_a_prompt_does_not_wait_for_git_and_tells_the_turn_after(
    repository: Repository, stale_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 9 review, F4. While git hangs, the turn starts without the notice, and
    the turn after git answers leads with it."""
    # Held well past the turn, so a slow runner does not end the hang by itself. A turn
    # that waited for git would wait out the hold, and git would have ended by then.
    hung = Hung(hold=4 * codetree.GIT_SECONDS)
    monkeypatch.setattr(codetree, "_git", hung)
    repository.checkout(repository.code)
    try:
        stale_world.run("one")
        answered_in_the_turn = hung.answered
    finally:
        hung.release.set()
    # The turn began and ended while git was outstanding, however long that took. The
    # earlier check, that the turn took less than ``GIT_SECONDS``, measured the
    # runner as much as the code, and git must also have been asked at all.
    assert not answered_in_the_turn
    assert hung.entered.wait(SETTLE_SECONDS)
    told = "The daemon runs code from"
    assert told not in stale_world.last_prompt()
    asked(stale_world.trees, lambda: stale_world.scheduler.daemon_behind("alpha"))
    stale_world.run("two")
    assert told in stale_world.last_prompt()
