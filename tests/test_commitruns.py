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
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import commitruns, runs
from dromon.codec import decode, encode
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.lineconfig import LineConfig, WorkdirAccess
from dromon.model import (
    InvalidField,
    OperatorAddress,
    RunStarted,
    RunStatus,
    format_due,
)
from dromon.projectconfig import ConfigFileError, parse
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
AT = "2026-10-31T09:00:00Z"
ANA = OperatorAddress("ana")
WAIT_SECONDS = 20
MAIN = ("refs/remotes/origin/main",)


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
    commit on ``side`` only, and an uncommitted file. The remote-tracking refs
    ``origin/main`` and ``origin/side`` point at them, as a fetch would leave them.
    Returns the repository, the main commit and the side commit."""
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
    git(repository, "update-ref", "refs/remotes/origin/main", main)
    git(repository, "update-ref", "refs/remotes/origin/side", side)
    (repository / "loose.txt").write_text("uncommitted\n")
    return repository, main, side


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str, str]:
    return repository_with_side_branch(tmp_path)


def checked(
    repository: Path,
    commit: str,
    patterns: Sequence[str] = MAIN,
    anchor: commitruns.RefAnchor | None = None,
) -> None:
    """``check_commit`` with the anchor a daemon starting now would read, unless one
    read earlier is given."""
    commitruns.check_commit(
        repository,
        commit,
        patterns,
        git_env(repository.parent),
        anchor=commitruns.ref_anchor(repository) if anchor is None else anchor,
    )


# The project file.


def test_the_runs_table_declares_commit_commands_and_their_refs() -> None:
    config = parse(
        "version = 1\n[runs.commands]\ngate = ['make']\n"
        "[runs.commit_commands]\nreview-gate = ['./scripts/check.sh']\n"
        "[runs.commit_refs]\npatterns = ['refs/remotes/origin/main',"
        " 'refs/remotes/origin/feat/*']\n"
    )
    assert config.run_commands == {"gate": ("make",)}
    assert config.run_commit_commands == {"review-gate": ("./scripts/check.sh",)}
    assert config.run_commit_refs == (
        "refs/remotes/origin/main",
        "refs/remotes/origin/feat/*",
    )
    assert parse("version = 1\n").run_commit_commands == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[runs.commit_commands]\nreview = ['x']", "must name the refs"),
        (
            "[runs.commands]\nreview = ['x']\n[runs.commit_commands]\nreview = ['y']\n"
            "[runs.commit_refs]\npatterns = ['refs/remotes/origin/main']",
            "which runs.commands declares too",
        ),
        ("[runs.commit_refs]\npatterns = 'refs/heads/main'", "array of strings"),
        ("[runs.commit_refs]\npatterns = ['heads/main']", "not under refs/remotes/"),
        ("[runs.commit_refs]\npatterns = ['refs/heads/main']", "not under"),
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
    with pytest.raises(commitruns.CommitRefused, match="is not a full commit"):
        checked(repository, commit)


def test_a_commit_not_in_the_repository_is_refused(repo: tuple[Path, str, str]) -> None:
    repository, _, _ = repo
    with pytest.raises(commitruns.CommitRefused, match="is not a commit"):
        checked(repository, "b" * 40)


def test_a_commit_no_allowed_ref_points_at_is_refused(
    repo: tuple[Path, str, str],
) -> None:
    repository, main, side = repo
    checked(repository, main)
    with pytest.raises(commitruns.CommitRefused, match="no ref matching"):
        checked(repository, side)
    checked(repository, side, ("refs/remotes/origin/*",))


def test_only_a_tip_runs_not_a_commit_a_ref_contains(
    repo: tuple[Path, str, str],
) -> None:
    """Review of 1449c5c, C1. ``side`` contains ``main``, and a local branch points
    at ``main`` still, but no allowed ref points at it, so it is refused."""
    repository, main, side = repo
    git(repository, "update-ref", "refs/remotes/origin/main", side)
    assert git(repository, "merge-base", "--is-ancestor", main, side) == ""
    assert git(repository, "rev-parse", "refs/heads/main") == main
    with pytest.raises(commitruns.CommitRefused, match="Only the tip"):
        checked(repository, main)
    checked(repository, side)


@pytest.mark.parametrize(
    "pattern",
    [
        "refs/heads/main",
        "refs/tags/*",
        "refs/*",
        "refs/remotes/*",
        "refs/remotes/origin",
        "refs/remotes/origin/",
        "refs/remotes/o*/main",
        "refs/notes/commits",
        "refs/stash",
    ],
)
def test_a_pattern_a_line_could_move_is_refused(pattern: str) -> None:
    """Review of 1449c5c, C1. A line's sandbox may write every ref outside
    ``refs/remotes``, so only a pattern naming a remote there is allowed."""
    with pytest.raises(commitruns.CommitRefused, match="not under refs/remotes/"):
        commitruns.check_patterns([pattern])
    allowed = ("refs/remotes/origin/main", "refs/remotes/origin/feat/*")
    assert commitruns.check_patterns(allowed) == allowed


def test_a_repository_without_the_files_backend_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review of 1449c5c, C1. The guards cover the files backend's places only, so
    another ref storage is refused before any ref is read."""
    asked: list[str] = []

    def fake_git(
        arguments: Sequence[str], cwd: Path, environment: Mapping[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        del cwd, environment
        asked.append(arguments[0])
        said = b"reftable\n" if arguments[0] == "config" else b""
        return subprocess.CompletedProcess(list(arguments), 0, said, b"")

    monkeypatch.setattr(commitruns, "_git", fake_git)
    with pytest.raises(commitruns.CommitRefused, match="files backend"):
        commitruns.check_commit(tmp_path, "a" * 40, MAIN, {}, anchor=((1, 1), (1, 2)))
    assert asked == ["cat-file", "config"]


def line_made_refs(repository: Path, side: str) -> Path:
    """A directory a line could make, holding refs in which ``origin/main`` points
    at ``side``, and ``packed-refs`` removed so that git reads only the loose refs."""
    made = repository.parent / "line-refs"
    (made / "remotes" / "origin").mkdir(parents=True)
    (made / "heads").mkdir()
    (made / "remotes" / "origin" / "main").write_text(f"{side}\n")
    (made / "heads" / "main").write_text(f"{side}\n")
    return made


@pytest.mark.parametrize("where", ["refs", "refs/remotes", "refs/remotes/origin"])
def test_a_ref_directory_that_is_a_link_is_refused(
    repo: tuple[Path, str, str], where: str
) -> None:
    """Re-review of 522c146, C1a. With ``where`` a link into a line's directory, git
    reads the line's ``origin/main``, which points at a commit the line chose. The
    control shows git accepts it, and the check refuses it."""
    repository, _, side = repo
    anchor = commitruns.ref_anchor(repository)
    made = line_made_refs(repository, side)
    common = repository / ".git"
    target = common / where
    os.rename(target, repository.parent / "moved")
    source = made / Path(where).relative_to("refs") if where != "refs" else made
    os.symlink(source, target)
    assert git(repository, "rev-parse", "refs/remotes/origin/main") == side
    with pytest.raises(commitruns.CommitRefused, match="not a real directory"):
        checked(repository, side, anchor=anchor)


@pytest.mark.parametrize("where", ["refs", "refs/remotes"])
def test_a_real_directory_moved_into_the_place_of_refs_is_refused(
    repo: tuple[Path, str, str], where: str
) -> None:
    """Final check of 951b9e7, item 2. A real directory of this user's, holding
    a line's ``origin/main``, passes every ``lstat`` check, as the control shows
    when the anchor is read after the swap. The anchor the daemon read at its start
    names the directory that was there, so the commit is refused."""
    repository, _, side = repo
    anchor = commitruns.ref_anchor(repository)
    made = line_made_refs(repository, side)
    common = repository / ".git"
    target = common / where
    os.rename(target, repository.parent / "moved")
    source = made / Path(where).relative_to("refs") if where != "refs" else made
    os.rename(source, target)
    (common / "packed-refs").unlink(missing_ok=True)
    assert git(repository, "rev-parse", "refs/remotes/origin/main") == side
    checked(repository, side)
    with pytest.raises(commitruns.CommitRefused, match="found when it started"):
        checked(repository, side, anchor=anchor)


def test_fetching_packing_and_collecting_keep_the_anchor(
    repo: tuple[Path, str, str],
) -> None:
    """Final check of 951b9e7, item 2. What an operator's fetch, ``git pack-refs``
    and ``git gc`` do to the refs leaves both anchored directories in place, so a
    commit run still starts. Observed with the git these tests run."""
    repository, main, side = repo
    anchor = commitruns.ref_anchor(repository)
    git(repository, "update-ref", "refs/remotes/origin/main", side)
    git(repository, "pack-refs", "--all")
    git(repository, "gc", "--quiet", "--prune=now")
    git(repository, "update-ref", "refs/remotes/origin/main", main)
    assert commitruns.ref_anchor(repository) == anchor
    checked(repository, main, anchor=anchor)


@pytest.mark.parametrize("which", ["packed", "loose"])
def test_a_ref_file_that_is_a_link_is_refused(
    repo: tuple[Path, str, str], which: str
) -> None:
    repository, main, _ = repo
    common = repository / ".git"
    if which == "packed":
        git(repository, "pack-refs", "--all")
        path = common / "packed-refs"
    else:
        path = common / "refs" / "remotes" / "origin" / "main"
    os.rename(path, repository.parent / "moved")
    os.symlink(repository.parent / "moved", path)
    assert git(repository, "rev-parse", "refs/remotes/origin/main") == main
    with pytest.raises(commitruns.CommitRefused, match="not a real file"):
        checked(repository, main)


def test_a_dot_git_that_is_a_link_is_refused(repo: tuple[Path, str, str]) -> None:
    repository, main, _ = repo
    anchor = commitruns.ref_anchor(repository)
    os.rename(repository / ".git", repository.parent / "elsewhere.git")
    os.symlink(repository.parent / "elsewhere.git", repository / ".git")
    assert git(repository, "rev-parse", "refs/remotes/origin/main") == main
    with pytest.raises(commitruns.CommitRefused, match="is a link"):
        checked(repository, main, anchor=anchor)


def test_a_ref_store_of_another_user_is_refused(
    repo: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repository, main, _ = repo
    anchor = commitruns.ref_anchor(repository)
    checked(repository, main, anchor=anchor)
    owner = os.getuid()
    monkeypatch.setattr(commitruns.os, "getuid", lambda: owner + 1)
    with pytest.raises(commitruns.CommitRefused, match="not this user's"):
        checked(repository, main, anchor=anchor)


def test_a_ref_store_swapped_during_the_match_is_refused(
    repo: tuple[Path, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review of 522c146, C1a. The store is read before and after the match, so
    one replaced while git read it refuses the commit, here a ``packed-refs``
    rewritten under a new inode as a fetch or a swap would leave it."""
    repository, main, _ = repo
    git(repository, "pack-refs", "--all")
    anchor = commitruns.ref_anchor(repository)
    real_git = commitruns._git  # pyright: ignore[reportPrivateUsage]

    def swapping_git(
        arguments: Sequence[str], cwd: Path, environment: Mapping[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        result = real_git(arguments, cwd, environment)
        if arguments[0] == "for-each-ref":
            packed = repository / ".git" / "packed-refs"
            copy = packed.with_name("packed-refs.new")
            copy.write_bytes(packed.read_bytes())
            os.replace(copy, packed)
        return result

    monkeypatch.setattr(commitruns, "_git", swapping_git)
    with pytest.raises(commitruns.CommitRefused, match="changed while"):
        checked(repository, main, anchor=anchor)


@pytest.mark.parametrize(
    "name", ["refs/heads/main", "refs/remotes/origin/../x", "refs/remotes//main"]
)
def test_a_matched_ref_outside_the_remotes_cannot_be_checked(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(commitruns.CommitRefused, match="cannot be checked"):
        commitruns._check_ref_path(tmp_path, name)  # pyright: ignore[reportPrivateUsage]


# The checkout.


def test_a_checkout_holds_the_commit_and_nothing_uncommitted(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repository, main, _ = repo
    base = tmp_path / "state" / "commit-runs"
    base.parent.mkdir()
    before = git(repository, "worktree", "list", "--porcelain")
    made = commitruns.make_checkout(base, "r-1", repository, main, git_env(tmp_path))
    checkout = made.tree
    assert made == commitruns.Checkout(base / "r-1" / "tree", base / "r-1" / "home")
    assert (checkout / "gate.txt").read_text() == "committed gate\n"
    assert not (checkout / "loose.txt").exists()
    assert not (checkout / "side.txt").exists()
    assert git(checkout, "rev-parse", "HEAD") == main
    for private in (base, base / "r-1", made.home):
        assert stat.S_IMODE(private.stat().st_mode) == 0o700
    assert list(made.home.iterdir()) == []
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


# What git may run before the command. Review of 1449c5c, C2 and G1.


def test_git_runs_with_the_search_path_alone_and_no_global_configuration() -> None:
    given = {
        "PATH": "/opt/bin",
        "HOME": "/home/operator",
        "SSH_AUTH_SOCK": "/agent",
        "GIT_DIR": "/elsewhere",
        "XDG_CONFIG_HOME": "/config",
    }
    environment = commitruns.git_environment(given)
    assert environment["PATH"] == given["PATH"]
    assert not set(environment) & (set(given) - {"PATH"})
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_SYSTEM"] == os.devnull
    assert environment["GIT_TERMINAL_PROMPT"] == "0"


def filtered_repository(tmp_path: Path, driver: str) -> tuple[Path, str, Path, Path]:
    """A repository whose commit names ``driver`` as the filter of every path and
    carries a ``.lfsconfig`` naming an unroutable address, as a hostile commit
    would, and a configuration file that defines ``driver`` to record each run in
    a marker file. Returns the repository, the commit, the configuration file and
    the marker."""
    repository = tmp_path / f"filtered-{driver}"
    repository.mkdir()
    git(repository, "init", "--quiet", "-b", "main")
    (repository / ".gitattributes").write_text(f"* filter={driver}\n")
    (repository / ".lfsconfig").write_text("[lfs]\n\turl = http://198.51.100.7:9/x\n")
    (repository / "data.txt").write_text("committed bytes\n")
    git(repository, "add", ".gitattributes", ".lfsconfig", "data.txt")
    git(repository, "commit", "--quiet", "-m", "filtered")
    commit = git(repository, "rev-parse", "HEAD")
    git(repository, "update-ref", "refs/remotes/origin/main", commit)
    marker = tmp_path / f"{driver}-ran"
    configuration = tmp_path / f"{driver}.gitconfig"
    configuration.write_text(
        f'[filter "{driver}"]\n'
        f"\tsmudge = \"echo ran >> '{marker}'; cat\"\n"
        f"\tprocess = \"sh -c 'echo ran >> {marker}; exit 1'\"\n"
        "\trequired = true\n"
    )
    return repository, commit, configuration, marker


def checkout_as_git_would(
    repository: Path, commit: str, configuration: Path, target: Path
) -> None:
    """A clone and checkout with ``configuration`` as the global configuration and
    nothing else turned off, as the operator's own git would make them."""
    environment = {**git_env(target.parent), "GIT_CONFIG_GLOBAL": str(configuration)}
    for arguments, cwd in (
        (
            [
                "clone",
                "--quiet",
                "--no-checkout",
                "--shared",
                str(repository),
                str(target),
            ],
            target.parent,
        ),
        (["checkout", "--quiet", "--detach", commit], target),
    ):
        subprocess.run(  # noqa: S603
            ["git", *arguments],  # noqa: S607
            cwd=cwd,
            env=environment,
            capture_output=True,
            check=False,
        )


def test_a_filter_the_operator_configures_globally_never_runs(tmp_path: Path) -> None:
    """The commit names the large file filter, which the operator's global
    configuration defines here, as ``git lfs install`` would. The same checkout
    with that configuration runs it, and a commit run's checkout does not."""
    repository, commit, configuration, marker = filtered_repository(tmp_path, "lfs")
    checkout_as_git_would(repository, commit, configuration, tmp_path / "control")
    assert marker.exists(), "the filter must run without the protections"
    marker.unlink()
    home = tmp_path / "operator-home"
    home.mkdir()
    (home / ".gitconfig").write_text(configuration.read_text())
    base = tmp_path / "commit-runs"
    operator = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(home)}
    made = commitruns.make_checkout(
        base, "r-1", repository, commit, commitruns.git_environment(operator)
    )
    assert not marker.exists()
    assert (made.tree / "data.txt").read_text() == "committed bytes\n"
    assert (made.tree / ".lfsconfig").is_file()


def test_no_path_names_a_filter_even_where_one_is_configured(tmp_path: Path) -> None:
    """The clone's ``info/attributes`` outranks the commit's ``.gitattributes``, so
    a filter defined in any configuration git reads is not run."""
    repository, commit, configuration, marker = filtered_repository(tmp_path, "mark")
    checkout_as_git_would(repository, commit, configuration, tmp_path / "control")
    assert marker.exists(), "the filter must run without the protections"
    marker.unlink()
    environment = {
        **commitruns.git_environment(os.environ),
        "GIT_CONFIG_GLOBAL": str(configuration),
    }
    made = commitruns.make_checkout(
        tmp_path / "commit-runs", "r-1", repository, commit, environment
    )
    assert not marker.exists()
    assert (made.tree / "data.txt").read_text() == "committed bytes\n"


def test_no_hook_runs_in_a_git_call_for_commit_runs(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    """Review of 1449c5c, G1. A ``post-checkout`` hook runs for the same checkout
    without the flags, and not for a git call of commit runs."""
    repository, main, side = repo
    marker = tmp_path / "hook-ran"
    hook = repository / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho ran >> '{marker}'\n")
    hook.chmod(0o755)
    git(repository, "checkout", "--quiet", "--detach", side)
    assert marker.exists(), "the hook must run without the protections"
    marker.unlink()
    result = commitruns._git(  # pyright: ignore[reportPrivateUsage]
        ["checkout", "--quiet", "--detach", main],
        repository,
        commitruns.git_environment(os.environ),
    )
    assert result.returncode == 0
    assert git(repository, "rev-parse", "HEAD") == main
    assert not marker.exists()


def test_a_git_call_past_its_bound_is_killed_with_its_whole_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review of 1449c5c, C3. A stand-in for git starts a child, as git starts a
    filter, and waits. At the bound both are killed, not git alone."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_file = tmp_path / "child.pid"
    stand_in = bin_dir / "git"
    stand_in.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > '{pid_file}'\nwait\n")
    stand_in.chmod(0o755)
    monkeypatch.setattr(commitruns, "GIT_SECONDS", 1.0)
    environment = {"PATH": f"{bin_dir}{os.pathsep}/bin{os.pathsep}/usr/bin"}
    with pytest.raises(commitruns.CommitRefused, match="process group was killed"):
        commitruns._git(  # pyright: ignore[reportPrivateUsage]
            ["status"], tmp_path, environment
        )
    child = int(pid_file.read_text())
    for _ in range(WAIT_SECONDS * 20):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    os.kill(child, signal.SIGKILL)
    raise AssertionError(f"the git call's child {child} outlived the kill")


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
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
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
        wall_clock=clock,
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
        assert (run.commit, run.checkout) == (main, str(checkout / "tree"))
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
        ]:
            with pytest.raises(runs.RunRefused, match=message):
                asyncio.run(scheduler.start_run("alpha", command, "none", commit))
        without = commit_scheduler(log, repository, tmp_path, checkouts=False)
        with pytest.raises(runs.RunRefused, match="makes no checkouts"):
            asyncio.run(without.start_run("alpha", "review", "none", main))
        # None of these held the commit worker, so none is recorded.
        assert log.state.commit_refusals == {}
        with pytest.raises(runs.RunRefused, match="no ref matching"):
            asyncio.run(scheduler.start_run("alpha", "review", "none", side))
        assert log.state.runs == {}
        base = tmp_path / "state" / "commit-runs"
        assert not base.exists() or list(base.iterdir()) == []


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


# Review of 1449c5c, C3 and C4. Starts are limited and isolated, and the command
# finds a fresh home.


@dataclass
class Clock:
    now: datetime = field(default_factory=lambda: datetime(2026, 11, 1, 9, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.now


def test_a_refused_start_is_recorded_and_its_cooldown_outlives_a_restart(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repository, main, side = repo
    clock = Clock()
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = commit_scheduler(log, repository, tmp_path, clock=clock)
        with pytest.raises(runs.RunRefused, match="This is recorded"):
            asyncio.run(scheduler.start_run("alpha", "review", "none", side))
        (refused,) = log.state.commit_refusals.values()
        assert (refused.line_id, refused.role_id) == ("alpha", None)
        assert (refused.command, refused.commit) == ("review", side)
        assert "points at" in refused.reason
        assert refused.refused_at == format_due(clock.now)
    clock.now += timedelta(seconds=commitruns.REFUSED_COOLDOWN_SECONDS - 1)
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = commit_scheduler(log, repository, tmp_path, clock=clock)
        with pytest.raises(runs.RunRefused, match="was refused at"):
            asyncio.run(scheduler.start_run("alpha", "review", "none", main))
        assert log.state.runs == {}
        clock.now += timedelta(seconds=1)
        run = asyncio.run(scheduler.start_run("alpha", "review", "none", main))
        wait_for_supervisor(scheduler, run.run_id)
        check(scheduler)


def test_the_scheduler_anchors_the_refs_when_it_is_made(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    """Final check of 951b9e7, item 2. The scheduler, made as the daemon starts,
    reads the anchor once. A real ``refs/remotes`` moved into place afterwards
    refuses the start and is recorded, while a scheduler made after the swap would
    accept it, as the control shows."""
    repository, _, side = repo
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = commit_scheduler(log, repository, tmp_path)
        made = line_made_refs(repository, side)
        remotes = repository / ".git" / "refs" / "remotes"
        os.rename(remotes, repository.parent / "moved")
        os.rename(made / "remotes", remotes)
        with pytest.raises(runs.RunRefused, match="found when it started"):
            asyncio.run(scheduler.start_run("alpha", "review", "none", side))
        (refused,) = log.state.commit_refusals.values()
        assert "found when it started" in refused.reason
    checked(repository, side)


def test_refs_unreadable_when_the_scheduler_is_made_refuse_every_commit_run(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    """Final check of 951b9e7, item 2. A ``.git`` that is a link when the daemon
    starts leaves no anchor, so a commit run is refused and recorded even once the
    link is gone, until a restart reads the anchor again."""
    repository, main, _ = repo
    moved = repository.parent / "elsewhere.git"
    os.rename(repository / ".git", moved)
    os.symlink(moved, repository / ".git")
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = commit_scheduler(log, repository, tmp_path)
        (repository / ".git").unlink()
        os.rename(moved, repository / ".git")
        with pytest.raises(runs.RunRefused, match="when the daemon started"):
            asyncio.run(scheduler.start_run("alpha", "review", "none", main))
        (refused,) = log.state.commit_refusals.values()
        assert "is a link" in refused.reason
        restarted = commit_scheduler(log, repository, tmp_path, clock=Clock())
        assert restarted._ref_anchor == commitruns.ref_anchor(repository)  # pyright: ignore[reportPrivateUsage]


def test_a_second_start_of_a_scope_is_refused_while_one_is_in_progress(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repository, main, _ = repo
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = commit_scheduler(log, repository, tmp_path)

        async def both() -> tuple[RunStarted | BaseException, ...]:
            return await asyncio.gather(
                scheduler.start_run("alpha", "review", "none", main),
                scheduler.start_run("alpha", "review", "none", main),
                return_exceptions=True,
            )

        first, second = asyncio.run(both())
        assert isinstance(first, RunStarted)
        assert isinstance(second, runs.RunRefused)
        assert "is starting" in str(second)
        assert log.state.commit_refusals == {}
        assert list(log.state.runs) == [first.run_id]
        wait_for_supervisor(scheduler, first.run_id)
        check(scheduler)


def test_the_checkout_is_made_on_the_commit_worker(
    repo: tuple[Path, str, str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The process worker, which serves stops and reconciliation, is never held by
    a commit run's checkout."""
    repository, main, _ = repo
    seen: list[threading.Thread] = []
    real = commitruns.make_checkout

    def recorded(
        base: Path,
        run_id: str,
        repository: Path,
        commit: str,
        environment: Mapping[str, str],
    ) -> commitruns.Checkout:
        seen.append(threading.current_thread())
        return real(base, run_id, repository, commit, environment)

    monkeypatch.setattr(commitruns, "make_checkout", recorded)
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = commit_scheduler(log, repository, tmp_path)
        run = asyncio.run(scheduler.start_run("alpha", "review", "none", main))
        commit_worker = scheduler._commit_worker  # pyright: ignore[reportPrivateUsage]
        process_worker = scheduler._process_worker  # pyright: ignore[reportPrivateUsage]
        commit_thread = commit_worker._thread  # pyright: ignore[reportPrivateUsage]
        process_thread = process_worker._thread  # pyright: ignore[reportPrivateUsage]
        assert seen == [commit_thread]
        assert process_thread is not None and process_thread is not commit_thread
        wait_for_supervisor(scheduler, run.run_id)
        check(scheduler)


HOME_SOURCE = (
    "import os, stat; home = os.environ['HOME'];"
    " print(home, oct(stat.S_IMODE(os.stat(home).st_mode)), os.listdir(home))"
)


def test_a_commit_run_has_a_fresh_private_home_removed_with_its_checkout(
    repo: tuple[Path, str, str], tmp_path: Path
) -> None:
    repository, main, _ = repo
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = commit_scheduler(log, repository, tmp_path)
        scheduler.set_runs({}, {"review": (sys.executable, "-c", HOME_SOURCE)}, MAIN)
        run = asyncio.run(scheduler.start_run("alpha", "review", "none", main))
        home = Path(str(run.checkout)).parent / commitruns.HOME
        assert str(home) != os.environ.get("HOME")
        wait_for_supervisor(scheduler, run.run_id)
        check(scheduler)
        output = (Path(run.workdir) / run.output_file).read_text()
        assert output == f"{home} 0o700 []\n"
        assert not home.exists()
