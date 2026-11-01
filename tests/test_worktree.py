"""Round 14, item 7. ``worktree_status`` gives a line's commit, branch, distance from
``origin/main`` and dirty paths from the daemon, outside the line's sandbox, and names
a path it could not read as incomplete rather than clean."""

import asyncio
import json
import os
import shutil
import subprocess
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import worktree
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.scheduler import LineEngine, LineSpec, Scheduler
from dromon.worktree import (
    Ran,
    WorktreeUnavailable,
    against_origin_main,
    dirty_paths,
    git_environment,
    head_commit,
    incomplete_paths,
    run_git,
    worktree_status,
)

HAIKU = "claude-haiku-5-5"
SHA = "a" * 40

needs_git = pytest.mark.skipif(
    shutil.which("git") is None, reason="git is not installed"
)


def git(directory: Path, *arguments: str) -> str:
    environment = {
        **git_environment(),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(directory), *arguments],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
        env=environment,
    )
    return done.stdout


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    directory = tmp_path / "work"
    directory.mkdir()
    git(directory, "init", "-q", "-b", "feature")
    (directory / "kept.txt").write_text("one\n")
    git(directory, "add", "kept.txt")
    git(directory, "commit", "-q", "-m", "first")
    return directory


class Script:
    """A runner that answers from a table keyed by the git subcommand."""

    def __init__(self, answers: dict[str, Ran]) -> None:
        self.answers = answers
        self.asked: list[list[str]] = []

    def __call__(self, directory: Path, arguments: Sequence[str]) -> Ran:
        self.asked.append(list(arguments))
        return self.answers.get(arguments[0], Ran(0))


def test_the_environment_names_no_steering_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setenv("GIT_EXTERNAL_DIFF", "/bin/evil")
    monkeypatch.setenv("SECRET_TOKEN", "x")
    found = git_environment()
    assert "GIT_DIR" not in found and "GIT_EXTERNAL_DIFF" not in found
    assert "SECRET_TOKEN" not in found
    assert found["GIT_OPTIONAL_LOCKS"] == "0"
    assert found["GIT_CONFIG_NOSYSTEM"] == "1"
    assert found["GIT_CONFIG_GLOBAL"] == os.devnull
    assert found["LC_ALL"] == "C"


@needs_git
def test_a_clean_checkout_is_clean_and_complete(checkout: Path) -> None:
    found = worktree_status(checkout)
    assert found["head"] == git(checkout, "rev-parse", "HEAD").strip()
    assert found["branch"] == "feature"
    assert found["clean"] is True and found["complete"] is True
    assert found["dirty"] == [] and found["dirty_count"] == 0
    assert found["incomplete"] == []
    assert found["origin_main"] == {
        "unavailable": "there is no origin/main in this checkout"
    }
    assert "clean, and every path was read" in cast(str, found["summary"])


@needs_git
def test_dirty_paths_are_listed_with_their_status(checkout: Path) -> None:
    (checkout / "kept.txt").write_text("two\n")
    (checkout / "new.txt").write_text("n\n")
    (checkout / "dir").mkdir()
    (checkout / "dir" / "inner.txt").write_text("i\n")
    git(checkout, "add", "new.txt")
    found = worktree_status(checkout)
    assert found["clean"] is False and found["complete"] is True
    entries = {
        e["path"]: e["status"] for e in cast(list[dict[str, str]], found["dirty"])
    }
    assert entries == {"kept.txt": " M", "new.txt": "A ", "dir/": "??"}
    assert found["dirty_count"] == 3


@needs_git
def test_ahead_and_behind_are_counted_against_the_last_fetched_origin_main(
    checkout: Path,
) -> None:
    git(checkout, "update-ref", "refs/remotes/origin/main", "HEAD")
    (checkout / "a.txt").write_text("a\n")
    git(checkout, "add", "a.txt")
    git(checkout, "commit", "-q", "-m", "ahead")
    other = git(checkout, "rev-parse", "HEAD~1").strip()
    git(checkout, "checkout", "-q", "-b", "other", other)
    (checkout / "b.txt").write_text("b\n")
    git(checkout, "add", "b.txt")
    git(checkout, "commit", "-q", "-m", "side")
    git(checkout, "update-ref", "refs/remotes/origin/main", "feature")
    found = worktree_status(checkout)
    assert found["branch"] == "other"
    assert found["origin_main"] == {
        "ref": "origin/main",
        "ahead": 1,
        "behind": 1,
        "note": (
            "origin/main is as the last fetch left it, since the daemon does not fetch"
        ),
    }


@needs_git
def test_a_detached_head_has_no_branch(checkout: Path) -> None:
    git(checkout, "checkout", "-q", "--detach")
    found = worktree_status(checkout)
    assert found["branch"] is None
    assert "a detached HEAD" in cast(str, found["summary"])


@needs_git
def test_a_branch_with_no_commit_has_no_head_and_no_distance(tmp_path: Path) -> None:
    directory = tmp_path / "fresh"
    directory.mkdir()
    git(directory, "init", "-q", "-b", "main")
    found = worktree_status(directory)
    assert found["head"] is None and found["branch"] == "main"
    assert found["origin_main"] == {"unavailable": "the branch has no commit yet"}
    assert found["clean"] is True
    assert "no commit" in cast(str, found["summary"])


@needs_git
def test_a_directory_that_is_not_a_checkout_is_unavailable(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeUnavailable, match="git rev-parse failed"):
        worktree_status(plain, _outside(plain))


def _outside(directory: Path) -> worktree.Runner:
    """A runner that finds no repository, as git does outside a checkout, whatever
    enclosing directory the test's temporary tree has."""

    def run(where: Path, arguments: Sequence[str]) -> Ran:
        return Ran(128, "", "fatal: not a git repository (or any parent)\n")

    return run


@needs_git
def test_an_unreadable_directory_makes_the_scan_incomplete_and_never_clean(
    checkout: Path,
) -> None:
    hidden = checkout / "hidden"
    hidden.mkdir()
    (hidden / "inside.txt").write_text("x")
    os.chmod(hidden, 0)
    try:
        if os.access(hidden, os.R_OK):
            pytest.skip("this user can read a directory with no permissions")
        found = worktree_status(checkout)
    finally:
        os.chmod(hidden, 0o700)
    assert found["complete"] is False
    incomplete = cast(list[dict[str, Any]], found["incomplete"])
    assert incomplete and any(entry["path"] for entry in incomplete)
    assert any("hidden" in str(entry["path"]) for entry in incomplete)
    assert found["clean"] is not True
    assert "dirty" in found


def test_a_warning_with_no_dirty_path_is_not_known_to_be_clean() -> None:
    script = Script(
        {
            "rev-parse": Ran(0, SHA + "\n"),
            "symbolic-ref": Ran(0, "feature\n"),
            "status": Ran(
                0,
                "",
                "warning: could not open directory 'secret/': Permission denied\n",
            ),
        }
    )
    found = worktree_status(Path("/w"), script)
    assert found["clean"] is None and found["complete"] is False
    assert found["incomplete"] == [
        {
            "path": "secret/",
            "reason": "warning: could not open directory 'secret/': Permission denied",
        }
    ]
    assert "not known to be clean" in cast(str, found["summary"])


def test_a_dirty_tree_stays_dirty_when_the_scan_was_also_incomplete() -> None:
    script = Script(
        {
            "rev-parse": Ran(0, SHA + "\n"),
            "symbolic-ref": Ran(0, "feature\n"),
            "status": Ran(0, " M a.txt\x00", 'error: open("b"): Permission denied\n'),
        }
    )
    found = worktree_status(Path("/w"), script)
    assert found["clean"] is False and found["complete"] is False
    assert "scan was incomplete" in cast(str, found["summary"])
    assert found["incomplete"] == [
        {"path": "b", "reason": 'error: open("b"): Permission denied'}
    ]


def test_a_failed_status_is_unavailable_with_the_reason() -> None:
    script = Script(
        {
            "rev-parse": Ran(0, SHA + "\n"),
            "status": Ran(128, "", "fatal: index file corrupt\n"),
        }
    )
    with pytest.raises(
        WorktreeUnavailable, match="status 128 and said fatal: index file corrupt"
    ):
        worktree_status(Path("/w"), script)


def test_a_commit_name_that_is_not_one_is_refused() -> None:
    script = Script({"rev-parse": Ran(0, "--output=/x\n")})
    with pytest.raises(WorktreeUnavailable, match="not one"):
        head_commit(Path("/w"), script)


def test_the_distance_failures_are_reasons_and_never_raise() -> None:
    missing = Script({"rev-parse": Ran(1)})
    assert against_origin_main(Path("/w"), missing) == {
        "unavailable": "there is no origin/main in this checkout"
    }
    broken = Script(
        {"rev-parse": Ran(0, SHA), "rev-list": Ran(128, "", "fatal: bad\n")}
    )
    reason = against_origin_main(Path("/w"), broken)["unavailable"]
    assert "status 128 and said fatal: bad" in cast(str, reason)
    garbled = Script({"rev-parse": Ran(0, SHA), "rev-list": Ran(0, "1 x\n")})
    assert "unavailable" in against_origin_main(Path("/w"), garbled)


def test_a_long_dirty_list_and_long_paths_are_cut() -> None:
    entries = "".join(f" M f{index}\x00" for index in range(worktree.MAX_DIRTY + 5))
    found = dirty_paths(entries + " M " + "x" * 500 + "\x00")
    assert len(found) == worktree.MAX_DIRTY + 6
    assert found[-1]["path"] == "x" * worktree.MAX_PATH_CHARACTERS + "..."
    script = Script(
        {
            "rev-parse": Ran(0, SHA + "\n"),
            "status": Ran(0, entries),
        }
    )
    status = worktree_status(Path("/w"), script)
    assert status["dirty_count"] == worktree.MAX_DIRTY + 5
    assert len(cast(list[object], status["dirty"])) == worktree.MAX_DIRTY
    assert status["dirty_truncated"] is True


def test_malformed_records_and_blank_errors_are_skipped() -> None:
    assert dirty_paths("\x00x\x00 M\x00??x y\x00") == []
    assert incomplete_paths("\n  \n") == []
    many = "\n".join(
        f"warning: {index}" for index in range(worktree.MAX_INCOMPLETE + 9)
    )
    assert len(incomplete_paths(many)) == worktree.MAX_INCOMPLETE


def test_the_real_runner_reports_a_missing_git_and_a_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def cannot(command: list[str], **_: object) -> object:
        raise FileNotFoundError("git")

    monkeypatch.setattr(subprocess, "run", cannot)
    found = run_git(tmp_path, ["status"])
    assert found.code == 127 and "git cannot be run" in found.stderr

    def slow(command: list[str], **_: object) -> object:
        raise subprocess.TimeoutExpired(command, 1)

    monkeypatch.setattr(subprocess, "run", slow)
    found = run_git(tmp_path, ["status"])
    assert found.code == 124 and "did not finish" in found.stderr


@needs_git
def test_the_real_runner_turns_the_file_system_monitor_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(
        command: list[str], **keywords: object
    ) -> "subprocess.CompletedProcess[str]":
        seen.append(command)
        return real(command, **keywords)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(subprocess, "run", spy)
    run_git(tmp_path, ["--version"])
    assert seen[0][:5] == ["git", "-C", str(tmp_path), "-c", "core.fsmonitor=false"]


# The tool.


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start, but {spec.line_id} did")


@pytest.fixture
def scheduler(tmp_path: Path) -> Generator[Scheduler]:
    with EventLog(tmp_path / "events.sqlite") as log:
        found = Scheduler(log, no_engine, hold_wakes=True, root=tmp_path / "project")
        found.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        yield found


def call(scheduler: Scheduler, args: dict[str, object]) -> tuple[str, bool]:
    tool = next(t for t in scheduler.own_tools("alpha") if t.name == "worktree_status")

    async def invoke() -> dict[str, Any]:
        return await tool.handler(args)

    result = asyncio.run(invoke())
    return cast(str, result["content"][0]["text"]), result.get("is_error") is True


def test_the_tool_gives_the_status_as_json(
    scheduler: Scheduler, tmp_path: Path
) -> None:
    (tmp_path / "project").mkdir()
    scheduler._worktree_runner = Script(  # pyright: ignore[reportPrivateUsage]
        {
            "rev-parse": Ran(0, SHA + "\n"),
            "symbolic-ref": Ran(0, "feature\n"),
            "status": Ran(0, " M a.txt\x00"),
        }
    )
    text, error = call(scheduler, {})
    assert not error
    found = json.loads(text)
    assert found["head"] == SHA and found["clean"] is False
    assert found["dirty"] == [{"status": " M", "path": "a.txt"}]
    assert found["worktree"] == os.path.realpath(tmp_path / "project")


def test_the_tool_takes_no_arguments(scheduler: Scheduler) -> None:
    text, error = call(scheduler, {"path": "x"})
    assert error and "unexpected argument path" in text


def test_the_tool_is_unavailable_with_the_reason_where_git_cannot_say(
    scheduler: Scheduler, tmp_path: Path
) -> None:
    (tmp_path / "project").mkdir()
    scheduler._worktree_runner = Script(  # pyright: ignore[reportPrivateUsage]
        {"rev-parse": Ran(128, "", "fatal: not a git repository\n")}
    )
    text, error = call(scheduler, {})
    assert error
    assert text.startswith("worktree_status is unavailable: git rev-parse failed")
    assert "not a git repository" in text


def test_the_tool_is_unavailable_where_the_directory_does_not_resolve(
    scheduler: Scheduler,
) -> None:
    text, error = call(scheduler, {})
    assert error and text.startswith("worktree_status is unavailable: ")
