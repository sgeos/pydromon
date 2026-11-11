"""Whether a run still covers the tree it ran on. **Round 16, item 5, core's request
of round 15.** A gate that passed was read as covering the tree after tracked files
had changed. A run in a working directory records ``readiness.tree_digest`` as it
starts, and ``run_status`` compares it with the digest now for the latest run."""

import asyncio
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import readiness
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.model import InvalidField, RunStarted
from dromon.scheduler import (
    RUN_STATUS_TOOL_NAME,
    START_RUN_TOOL_NAME,
    LineEngine,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
AT = "2026-10-31T09:00:00Z"
DIGEST = "d" * 64
WAIT_SECONDS = 30


def git(directory: Path, *arguments: str) -> str:
    """Run git in ``directory`` for the test's own setup, apart from the daemon's
    reads, which go through ``dromon.spawn``."""
    import subprocess

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
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return done.stdout.strip()


def repository(directory: Path) -> Path:
    """A repository with one tracked file that ignores everything else, so the
    daemon's log and a run's output stay out of ``git status``."""
    directory.mkdir(parents=True, exist_ok=True)
    git(directory, "init", "-q")
    (directory / ".gitignore").write_text("/*\n!/kept.txt\n!/.gitignore\n!/notes.md\n")
    (directory / "kept.txt").write_text("kept\n")
    git(directory, "add", "kept.txt", ".gitignore")
    git(directory, "commit", "-q", "-m", "first")
    return directory


def started(tree: str | None, commit: str | None = None) -> RunStarted:
    return RunStarted(
        "r-1",
        "alpha",
        None,
        None,
        "gate",
        ("./scripts/check.sh",),
        "/work",
        "tmp/runs/r-1.out",
        4321,
        AT,
        AT,
        commit,
        None if commit is None else "/work/.dromon/commit-runs/r-1",
        tree,
    )


# The record.


def test_a_run_records_its_tree_as_a_digest_or_nothing() -> None:
    assert started(DIGEST).tree == DIGEST
    assert started(None).tree is None
    with pytest.raises(InvalidField, match="tree must be null or a SHA-256"):
        started("not a digest")


def test_a_commit_run_records_no_tree() -> None:
    with pytest.raises(InvalidField, match="a commit run records no tree"):
        started(DIGEST, commit="a" * 40)


# The digest.


@pytest.mark.real_git_status
def test_the_tree_digest_follows_head_and_tracked_files_only(tmp_path: Path) -> None:
    worktree = repository(tmp_path / "w")
    clean = readiness.tree_digest(worktree)
    assert readiness.tree_digest(worktree) == clean
    # An untracked file is a residual, left out so a run's output cannot count.
    (worktree / "notes.md").write_text("untracked\n")
    assert readiness.tree_digest(worktree) == clean
    (worktree / "kept.txt").write_text("changed\n")
    changed = readiness.tree_digest(worktree)
    assert changed != clean
    # An edit inside a tracked file that was already changed moves its size.
    (worktree / "kept.txt").write_text("changed again, and longer\n")
    assert readiness.tree_digest(worktree) not in (clean, changed)
    git(worktree, "commit", "-q", "-am", "second")
    committed = readiness.tree_digest(worktree)
    assert committed not in (clean, changed)


@pytest.mark.real_git_status
def test_the_tree_digest_of_a_subdirectory_reads_paths_from_the_top(
    tmp_path: Path,
) -> None:
    worktree = repository(tmp_path / "w")
    below = worktree / "below"
    below.mkdir()
    before = readiness.tree_digest(below)
    (worktree / "kept.txt").write_text("changed\n")
    first = readiness.tree_digest(below)
    (worktree / "kept.txt").write_text("changed, and longer\n")
    assert len({before, first, readiness.tree_digest(below)}) == 3


def test_the_tree_digest_outside_a_checkout_is_unknown(tmp_path: Path) -> None:
    with pytest.raises(readiness.ReadinessUnknown) as raised:
        readiness.tree_digest(tmp_path)
    assert raised.value.cause is readiness.ReadFailure.FAILED


# The run's status.


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


def stand_in_start(pid: int) -> str | None:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return None
    return AT


def stand_in_occupied(session: int) -> bool:
    try:
        os.killpg(session, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Runner:
    """A scheduler whose line ``alpha`` works in ``project`` and runs ``gate``."""

    def __init__(self, project: Path, log: EventLog) -> None:
        (project / "reports").mkdir(exist_ok=True)
        self.log = log
        self.scheduler = Scheduler(
            log,
            no_engine,
            hold_wakes=True,
            wall_clock=lambda: datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC),
            root=project,
            report_dir=project / "reports",
            run_start_of=stand_in_start,
            run_occupied=stand_in_occupied,
        )
        self.scheduler.set_runs({"gate": (sys.executable, "-c", "print('passed')")})
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        self.tools = {tool.name: tool for tool in self.scheduler.own_tools("alpha")}

    async def call(self, name: str, args: dict[str, object]) -> str:
        result = await self.tools[name].handler(args)
        assert result.get("is_error") is not True, result
        return str(result["content"][0]["text"])

    async def run_once(self) -> RunStarted:
        """Start ``gate``, wait for it to end, and give its record."""
        previous = "none" if not self.log.state.runs else max(self.log.state.runs)
        await self.call(START_RUN_TOOL_NAME, {"command": "gate", "previous": previous})
        run_id = max(self.log.state.runs)
        children = self.scheduler._run_children  # pyright: ignore[reportPrivateUsage]
        child = children.get(run_id)
        if child is not None:
            child.wait(timeout=WAIT_SECONDS)
        await self.scheduler.reconcile_runs()
        return self.log.state.runs[run_id].started


@pytest.mark.real_git_status
def test_run_status_says_when_the_latest_run_no_longer_covers_the_tree(
    tmp_path: Path,
) -> None:
    project = repository(tmp_path / "project")
    with EventLog(project / "events.sqlite") as log:
        runner = Runner(project, log)

        async def scenario() -> None:
            run = await runner.run_once()
            assert run.tree == readiness.tree_digest(project)
            text = await runner.call(RUN_STATUS_TOOL_NAME, {})
            assert "The tree is as it was when it started" in text
            assert "NO LONGER COVERS" not in text
            (project / "kept.txt").write_text("changed after the run\n")
            text = await runner.call(RUN_STATUS_TOOL_NAME, {})
            assert "IT NO LONGER COVERS THE TREE" in text
            # A new run covers the tree again.
            await runner.run_once()
            text = await runner.call(RUN_STATUS_TOOL_NAME, {})
            assert "The tree is as it was when it started" in text

        asyncio.run(scenario())


def test_a_run_whose_tree_could_not_be_read_says_its_coverage_is_unknown(
    tmp_path: Path,
) -> None:
    """The project is no checkout, so no tree is read, and no git is started."""
    project = tmp_path / "project"
    project.mkdir()
    with EventLog(project / "events.sqlite") as log:
        runner = Runner(project, log)

        async def scenario() -> None:
            run = await runner.run_once()
            assert run.tree is None
            text = await runner.call(RUN_STATUS_TOOL_NAME, {})
            assert "recorded no digest of the tree" in text
            assert "is unknown" in text

        asyncio.run(scenario())


def test_a_tree_that_cannot_be_read_now_is_unknown_not_covered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = repository(tmp_path / "project")
    with EventLog(project / "events.sqlite") as log:
        runner = Runner(project, log)

        async def scenario() -> None:
            run = await runner.run_once()
            assert run.tree is not None

            def timed_out(worktree: Path) -> str:
                raise readiness.ReadinessUnknown(
                    f"git status ran long in {worktree.name}",
                    readiness.ReadFailure.TIMED_OUT,
                )

            monkeypatch.setattr(readiness, "tree_digest", timed_out)
            text = await runner.call(RUN_STATUS_TOOL_NAME, {})
            assert "Whether it still covers the tree is unknown" in text
            assert "for the reason timed_out" in text
            assert "The tree is as it was" not in text

        asyncio.run(scenario())
