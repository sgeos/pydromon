"""Commit runs, round 14, item 1: a declared gate run on a commit a line names, in a
checkout the daemon makes in its state directory and removes once the run no longer
blocks its scope. ``docs/architecture/RUNS.md`` is normative.

Each test makes a real git repository in its temporary directory, with git's global
and system configuration left out, so the operator's own settings change nothing."""

import asyncio
import contextlib
import os
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import commitruns, runs
from dromon.codec import decode, encode
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.lineconfig import LineConfig, WorkdirAccess
from dromon.model import InvalidField, OperatorAddress, RunStarted, RunStatus
from dromon.projectconfig import ConfigFileError, parse
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
AT = "2026-10-31T09:00:00Z"
ANA = OperatorAddress("ana")
WAIT_SECONDS = 20
MAIN = ("refs/heads/main",)


def git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
    }


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        cwd=repository,
        env=git_env(repository.parent),
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def repository_with_side_branch(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository with a commit on ``main``, which holds ``gate.txt``, and a later
    commit on ``side`` only, and an uncommitted file. Returns the repository, the
    main commit and the side commit."""
    repository = tmp_path / "project"
    repository.mkdir()
    git(repository, "init", "--quiet", "-b", "main")
    (repository / "gate.txt").write_text("committed gate\n")
    git(repository, "add", "gate.txt")
    git(repository, "commit", "--quiet", "-m", "main")
    main = git(repository, "rev-parse", "HEAD")
    git(repository, "checkout", "--quiet", "-b", "side")
    (repository / "side.txt").write_text("side\n")
    git(repository, "add", "side.txt")
    git(repository, "commit", "--quiet", "-m", "side")
    side = git(repository, "rev-parse", "HEAD")
    git(repository, "checkout", "--quiet", "main")
    (repository / "loose.txt").write_text("uncommitted\n")
    return repository, main, side


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    return repository_with_side_branch(tmp_path)


# The project file.


def test_the_runs_table_declares_commit_commands_and_their_refs() -> None:
    config = parse(
        "version = 1\n[runs.commands]\ngate = ['make']\n"
        "[runs.commit_commands]\nreview-gate = ['./scripts/check.sh']\n"
        "[runs.commit_refs]\npatterns = ['refs/heads/main', 'refs/heads/feat/*']\n"
    )
    assert config.run_commands == {"gate": ("make",)}
    assert config.run_commit_commands == {"review-gate": ("./scripts/check.sh",)}
    assert config.run_commit_refs == ("refs/heads/main", "refs/heads/feat/*")
    assert parse("version = 1\n").run_commit_commands == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[runs.commit_commands]\nreview = ['x']", "must name the refs"),
        (
            "[runs.commands]\nreview = ['x']\n[runs.commit_commands]\nreview = ['y']\n"
            "[runs.commit_refs]\npatterns = ['refs/heads/main']",
            "which runs.commands declares too",
        ),
        ("[runs.commit_refs]\npatterns = 'refs/heads/main'", "array of strings"),
        ("[runs.commit_refs]\npatterns = ['heads/main']", "not a ref pattern"),
        ("[runs.commit_refs]\npatterns = ['refs/../x']", "not a ref pattern"),
        ("[runs.commit_refs]\npatterns = ['refs/a b']", "not a ref pattern"),
        ("[runs.commit_refs]\nwhere = []", "is not a known key"),
        ("[runs.commit_commands]\nreview = []", "must be a non-empty array"),
    ],
)
def test_a_bad_commit_runs_table_is_refused(text: str, message: str) -> None:
    with pytest.raises(ConfigFileError, match=message):
        parse(f"version = 1\n{text}\n")


# The event.


def test_run_started_names_a_full_commit_and_its_checkout_together() -> None:
    def started(commit: str | None, checkout: str | None) -> RunStarted:
        return RunStarted(
            "r-1",
            "alpha",
            None,
            None,
            "review",
            ("x",),
            "/work",
            "tmp/runs/r-1.out",
            4321,
            AT,
            AT,
            commit,
            checkout,
        )

    run = started("a" * 40, "/state/commit-runs/r-1")
    assert decode(encode(run)) == run
    plain = started(None, None)
    assert "commit" not in encode(plain)
    assert decode(encode(plain)) == plain
    for commit, checkout, message in [
        ("main", "/c", "full hexadecimal commit"),
        ("A" * 40, "/c", "full hexadecimal commit"),
        ("a" * 40, None, "exactly when commit is"),
        (None, "/c", "exactly when commit is"),
        ("a" * 40, "relative", "not absolute"),
    ]:
        with pytest.raises(InvalidField, match=message):
            started(commit, checkout)


# Checking a commit.


@pytest.mark.parametrize("commit", ["main", "abc123", "A" * 40, "g" * 40, "a" * 41])
def test_a_name_or_a_short_commit_is_refused(
    repo: tuple[Path, str, str], commit: str
) -> None:
    repository, _, _ = repo
    environment = git_env(repository.parent)
    with pytest.raises(commitruns.CommitRefused, match="is not a full commit"):
        commitruns.check_commit(repository, commit, MAIN, environment)


def test_a_commit_not_in_the_repository_is_refused(repo: tuple[Path, str, str]) -> None:
    repository, _, _ = repo
    environment = git_env(repository.parent)
    with pytest.raises(commitruns.CommitRefused, match="is not a commit"):
        commitruns.check_commit(repository, "b" * 40, MAIN, environment)


def test_a_commit_no_allowed_ref_contains_is_refused(
    repo: tuple[Path, str, str],
) -> None:
    repository, main, side = repo
    environment = git_env(repository.parent)
    commitruns.check_commit(repository, main, MAIN, environment)
    with pytest.raises(commitruns.CommitRefused, match="no ref matching"):
        commitruns.check_commit(repository, side, MAIN, environment)
    commitruns.check_commit(repository, side, ("refs/heads/*",), environment)


# The checkout.


def test_a_checkout_holds_the_commit_and_nothing_uncommitted(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repository, main, _ = repo
    base = tmp_path / "state" / "commit-runs"
    base.parent.mkdir()
    before = git(repository, "worktree", "list", "--porcelain")
    checkout = commitruns.make_checkout(
        base, "r-1", repository, main, git_env(tmp_path)
    )
    assert checkout == base / "r-1"
    assert (checkout / "gate.txt").read_text() == "committed gate\n"
    assert not (checkout / "loose.txt").exists()
    assert not (checkout / "side.txt").exists()
    assert git(checkout, "rev-parse", "HEAD") == main
    assert stat.S_IMODE(base.stat().st_mode) == 0o700
    assert stat.S_IMODE(checkout.stat().st_mode) == 0o700
    # The clone wrote no worktree into the project's repository.
    assert git(repository, "worktree", "list", "--porcelain") == before


def test_a_failed_checkout_leaves_nothing(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repository, _, _ = repo
    base = tmp_path / "commit-runs"
    with pytest.raises(commitruns.CommitRefused, match="checkout of"):
        commitruns.make_checkout(base, "r-1", repository, "c" * 40, git_env(tmp_path))
    assert list(base.iterdir()) == []


def test_a_wide_or_linked_base_is_refused(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repository, main, _ = repo
    wide = tmp_path / "wide"
    wide.mkdir(mode=0o755)
    wide.chmod(0o755)
    with pytest.raises(commitruns.CommitRefused, match="not a private directory"):
        commitruns.make_checkout(wide, "r-1", repository, main, git_env(tmp_path))
    linked = tmp_path / "linked"
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    linked.symlink_to(target)
    with pytest.raises(commitruns.CommitRefused, match="not a private directory"):
        commitruns.make_checkout(linked, "r-1", repository, main, git_env(tmp_path))
    assert list(target.iterdir()) == []


def test_removal_follows_no_link_in_or_at_a_checkout(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep\n")
    base = tmp_path / "commit-runs"
    base.mkdir(mode=0o700)
    inner = base / "r-1" / "nested"
    inner.mkdir(parents=True)
    (inner / "link").symlink_to(outside)
    (base / "r-2").symlink_to(outside)
    commitruns.remove_checkout(base, "r-1")
    commitruns.remove_checkout(base, "r-2")
    commitruns.remove_checkout(base, "r-3")
    assert list(base.iterdir()) == []
    assert (outside / "keep.txt").read_text() == "keep\n"
    with pytest.raises(commitruns.CommitRefused, match="not a run identifier"):
        commitruns.remove_checkout(base, "../outside")


def test_the_sweep_removes_what_no_run_keeps(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    base = tmp_path / "commit-runs"
    base.mkdir(mode=0o700)
    (base / "r-1").mkdir()
    (base / "r-2").mkdir()
    (base / "r-2" / "file").write_text("kept\n")
    (base / "stray file").write_text("stray\n")
    (base / "r-3").symlink_to(outside)
    removed = commitruns.sweep(base, {"r-2"})
    assert removed == ["r-1", "r-3", "stray file"]
    assert [path.name for path in base.iterdir()] == ["r-2"]
    assert outside.is_dir()
    assert commitruns.sweep(tmp_path / "absent", set()) == []


# The scheduler.


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


GATE_SOURCE = (
    "import os; print(open('gate.txt').read().strip(), os.path.exists('loose.txt'),"
    " os.environ['DROMON_RUN_ID'])"
)


def commit_scheduler(
    log: EventLog,
    repository: Path,
    tmp_path: Path,
    checkouts: bool = True,
    occupied: Callable[[int], bool] = stand_in_occupied,
) -> Scheduler:
    """A scheduler for the project at ``repository``, whose line ``alpha`` may only
    read its working directory, with a commit command ``review`` and an ordinary
    command ``gate``. The line is made unless the log has it."""
    reports = tmp_path / "reports"
    reports.mkdir(exist_ok=True)
    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    scheduler = Scheduler(
        log,
        no_engine,
        hold_wakes=True,
        root=repository,
        report_dir=reports,
        run_start_of=stand_in_start,
        run_occupied=occupied,
        checkout_dir=(state / "commit-runs") if checkouts else None,
    )
    scheduler.set_runs(
        {"gate": (sys.executable, "-c", "print('gate')")},
        {"review": (sys.executable, "-c", GATE_SOURCE)},
        MAIN,
    )
    if "alpha" not in log.state.lines:
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        scheduler.configure_line("alpha", LineConfig(workdir_access=WorkdirAccess.READ))
    return scheduler


def wait_for_supervisor(scheduler: Scheduler, run_id: str) -> None:
    children = scheduler._run_children  # pyright: ignore[reportPrivateUsage]
    children[run_id].wait(timeout=WAIT_SECONDS)


def check(scheduler: Scheduler) -> None:
    asyncio.run(scheduler._check_runs())  # pyright: ignore[reportPrivateUsage]


def test_a_reading_line_runs_a_commit_command_and_its_checkout_is_removed(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repository, main, _ = repo
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = commit_scheduler(log, repository, tmp_path)
        with pytest.raises(runs.RunRefused, match="which you may only read"):
            asyncio.run(scheduler.start_run("alpha", "gate", "none"))
        run = asyncio.run(scheduler.start_run("alpha", "review", "none", main))
        checkout = tmp_path / "state" / "commit-runs" / run.run_id
        assert (run.commit, run.checkout) == (main, str(checkout))
        assert checkout.is_dir()
        assert f"on commit {main}" in scheduler.run_status("alpha")
        wait_for_supervisor(scheduler, run.run_id)
        check(scheduler)
        ended = log.state.runs[run.run_id].ended
        assert ended is not None
        assert (ended.status, ended.exit_code) == (RunStatus.EXITED, 0)
        assert not checkout.exists()
        output = (Path(run.workdir) / run.output_file).read_text()
        assert output == f"committed gate False {run.run_id}\n"
        assert Path(run.workdir) == repository.resolve()


def test_a_commit_run_is_refused_before_anything_is_made(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repository, main, side = repo
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = commit_scheduler(log, repository, tmp_path)
        for command, commit, message in [
            ("review", None, "name the full commit"),
            ("gate", main, "takes no commit"),
            ("other", main, "Commit commands: review"),
            ("review", "main", "is not a full commit"),
            ("review", side, "no ref matching"),
        ]:
            with pytest.raises(runs.RunRefused, match=message):
                asyncio.run(scheduler.start_run("alpha", command, "none", commit))
        assert log.state.runs == {}
        base = tmp_path / "state" / "commit-runs"
        assert not base.exists() or list(base.iterdir()) == []
        without = commit_scheduler(log, repository, tmp_path, checkouts=False)
        with pytest.raises(runs.RunRefused, match="makes no checkouts"):
            asyncio.run(without.start_run("alpha", "review", "none", main))


def test_a_lost_commit_run_keeps_its_checkout_until_it_is_cleared(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    """The supervisor is killed without a receipt, as a machine restart would, while
    the table is taken to show its session occupied, so the run is lost and blocks.
    Its command, which says its own process identifier, is ended by the test."""
    repository, main, _ = repo
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = commit_scheduler(
            log, repository, tmp_path, occupied=lambda session: True
        )
        source = "import os, time; print(os.getpid(), flush=True); time.sleep(60)"
        scheduler.set_runs({}, {"review": (sys.executable, "-c", source)}, MAIN)
        run = asyncio.run(scheduler.start_run("alpha", "review", "none", main))
        checkout = Path(str(run.checkout))
        output = Path(run.workdir) / run.output_file
        children = scheduler._run_children  # pyright: ignore[reportPrivateUsage]
        child = children.pop(run.run_id)
        command = said_pid(output)
        try:
            child.kill()
            child.wait(timeout=WAIT_SECONDS)
            check(scheduler)
            ended = log.state.runs[run.run_id].ended
            assert ended is not None and ended.status is RunStatus.LOST
            assert log.state.runs[run.run_id].blocks
            assert checkout.is_dir()
        finally:
            with contextlib.suppress(ProcessLookupError):
                os.kill(command, signal.SIGKILL)
        scheduler.clear_run(ANA, run.run_id)
        check(scheduler)
        assert not checkout.exists()


def said_pid(output: Path) -> int:
    """The process identifier the command printed first, waited for in bounded
    steps."""
    for _ in range(WAIT_SECONDS * 20):
        text = output.read_text().strip()
        if text:
            return int(text.split()[0])
        time.sleep(0.05)
    raise AssertionError(f"{output} stayed empty")


def test_a_start_sweeps_checkouts_no_run_names(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    """A checkout left by a crash before its run was recorded has no run, and the
    first check after a start removes it."""
    repository, _, _ = repo
    base = tmp_path / "state" / "commit-runs"
    base.mkdir(parents=True, mode=0o700)
    (base / "r-left").mkdir()
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = commit_scheduler(log, repository, tmp_path)
        check(scheduler)
    assert list(base.iterdir()) == []


def test_the_start_run_tool_takes_a_commit(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repository, main, _ = repo
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = commit_scheduler(log, repository, tmp_path)
        tools = {tool.name: tool for tool in scheduler.own_tools("alpha")}
        start = tools["start_run"].handler
        refused = asyncio.run(
            start({"command": "review", "previous": "none", "commit": 7})
        )
        assert refused.get("is_error") is True
        found = asyncio.run(
            start({"command": "review", "previous": "none", "commit": main})
        )
        assert f"on commit {main}" in found["content"][0]["text"]
        (run_id,) = log.state.runs
        wait_for_supervisor(scheduler, run_id)
        check(scheduler)
        assert "Commit commands, which take a commit: review" in scheduler.run_status(
            "alpha"
        )
