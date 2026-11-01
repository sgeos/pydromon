"""``dromon.syncenv``, round 14, final check of 951b9e7, item 1. uv is replaced by a
fake that makes what uv would make where it is told to, so no test reaches the
network, and the filesystem steps around it are real."""

import os
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from dromon import land, syncenv
from dromon.land import Completed

Hook = Callable[[Sequence[str], Path], None]


def planted(worktree: Path) -> Path:
    """A ``.venv`` as the audit line wrote it, whose ``bin/python`` writes a marker
    when run. Returns the marker's path."""
    marker = worktree.parent / "marker"
    venv = worktree / syncenv.ENVIRONMENT
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\nversion_info = 3.15.0\n")
    python = venv / "bin" / "python"
    python.write_text(f"#!/bin/sh\necho ran >> {marker}\n")
    python.chmod(0o755)
    return marker


class FakeUv:
    """Makes an environment at the path ``uv venv`` names, marked relocatable unless
    ``relocatable`` is false, and fails the step named in ``failing``."""

    def __init__(
        self,
        relocatable: bool = True,
        failing: str | None = None,
        after_sync: Hook | None = None,
    ) -> None:
        self.relocatable = relocatable
        self.failing = failing
        self.after_sync = after_sync
        self.calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        stdin: str | None = None,
        *,
        stream: bool = False,
        timeout: float = land.GIT_SECONDS,
        env: Mapping[str, str] | None = None,
    ) -> Completed:
        del stdin, stream, timeout
        assert env is not None
        self.calls.append((tuple(argv), cwd, dict(env)))
        if argv[1] == self.failing:
            return Completed(2, "", "error: no interpreter found\n")
        if argv[1] == "venv":
            stage = Path(argv[-1])
            (stage / "bin").mkdir(parents=True)
            line = syncenv.RELOCATABLE if self.relocatable else "home = /usr/bin"
            (stage / "pyvenv.cfg").write_text(f"{line}\n")
            (stage / "built").write_text("by the operator's uv\n")
        elif self.after_sync is not None:
            self.after_sync(argv, cwd)
        return Completed(0, "")


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    found = tmp_path / "project" / "tmp" / "worktrees" / "core"
    found.mkdir(parents=True)
    return found


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path / "project" / ".dromon" / "sync-envs"


def test_a_planted_interpreter_is_never_run_and_is_replaced(
    worktree: Path, base: Path
) -> None:
    """The audit line's case. uv is only ever pointed at an environment in the
    private directory, every step runs with that environment and no other, and the
    planted ``.venv`` is moved out and removed unread."""
    marker = planted(worktree)
    uv = FakeUv()
    inherited = {"PATH": "/usr/bin", "VIRTUAL_ENV": str(worktree / ".venv")}
    inherited["CONDA_PREFIX"] = "/opt/conda"
    inherited[syncenv.PROJECT_ENVIRONMENT] = str(worktree / ".venv")
    assert syncenv.sync(worktree, base, uv, inherited) == syncenv.Synced()
    (venv_call, sync_call) = uv.calls
    stage = Path(venv_call[0][-1])
    assert venv_call[0] == syncenv.venv_argv(stage)
    assert "--relocatable" in venv_call[0]
    assert sync_call[0] == syncenv.SYNC_ARGV
    assert sync_call[0][-2:] == ("--link-mode", "copy")
    assert base in stage.parents
    for argv, cwd, env in uv.calls:
        assert cwd == worktree, argv
        assert env == {"PATH": "/usr/bin", syncenv.PROJECT_ENVIRONMENT: str(stage)}
    venv = worktree / syncenv.ENVIRONMENT
    assert (venv / "built").read_text() == "by the operator's uv\n"
    assert not (venv / "bin" / "python").exists()
    assert not marker.exists()
    assert list(base.iterdir()) == []
    assert stat.S_IMODE(base.stat().st_mode) == 0o700


def test_a_worktree_without_an_environment_gets_one(worktree: Path, base: Path) -> None:
    syncenv.sync(worktree, base, FakeUv(), {})
    assert (worktree / syncenv.ENVIRONMENT / "built").is_file()


def test_a_linked_environment_is_moved_out_without_following_the_link(
    worktree: Path, base: Path, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep").write_text("not the worktree's\n")
    os.symlink(elsewhere, worktree / syncenv.ENVIRONMENT)
    syncenv.sync(worktree, base, FakeUv(), {})
    assert (elsewhere / "keep").read_text() == "not the worktree's\n"
    venv = worktree / syncenv.ENVIRONMENT
    assert not venv.is_symlink()
    assert (venv / "built").is_file()


@pytest.mark.parametrize("step", ["venv", "sync"])
def test_a_failed_step_leaves_the_worktree_as_it_was(
    worktree: Path, base: Path, step: str
) -> None:
    planted(worktree)
    with pytest.raises(syncenv.SyncFailed, match="failed with status 2"):
        syncenv.sync(worktree, base, FakeUv(failing=step), {})
    assert (worktree / syncenv.ENVIRONMENT / "bin" / "python").is_file()
    assert list(base.iterdir()) == []


def test_an_environment_that_is_not_relocatable_is_refused(
    worktree: Path, base: Path
) -> None:
    """Its scripts would name the private directory's path, and fail once moved."""
    planted(worktree)
    with pytest.raises(syncenv.SyncFailed, match="without relocatable = true"):
        syncenv.sync(worktree, base, FakeUv(relocatable=False), {})
    assert (worktree / syncenv.ENVIRONMENT / "bin" / "python").is_file()
    assert list(base.iterdir()) == []


def test_a_line_that_makes_an_environment_between_the_renames_loses_the_race(
    worktree: Path, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``.venv`` made after the old one is moved aside makes the second rename
    fail, so nothing a line made is run, and the worktree is reported unsynced."""
    planted(worktree)
    real_rename = os.rename

    def racing_rename(source: Path, destination: Path) -> None:
        real_rename(source, destination)
        if Path(source) == worktree / syncenv.ENVIRONMENT:
            planted(worktree)

    monkeypatch.setattr(syncenv.os, "rename", racing_rename)
    with pytest.raises(syncenv.SyncFailed, match="made again meanwhile"):
        syncenv.sync(worktree, base, FakeUv(), {})
    assert not (worktree / syncenv.ENVIRONMENT / "built").exists()
    assert list(base.iterdir()) == []


def test_a_wide_or_linked_base_is_refused(
    worktree: Path, base: Path, tmp_path: Path
) -> None:
    base.mkdir(parents=True, mode=0o755)
    base.chmod(0o755)
    uv = FakeUv()
    with pytest.raises(syncenv.SyncFailed, match="no private directory"):
        syncenv.sync(worktree, base, uv, {})
    base.rmdir()
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    os.symlink(target, base)
    with pytest.raises(syncenv.SyncFailed, match="no private directory"):
        syncenv.sync(worktree, base, uv, {})
    assert uv.calls == []


def test_a_platform_that_follows_links_when_removing_is_refused(
    worktree: Path, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(syncenv.shutil.rmtree, "avoids_symlink_attacks", False)
    with pytest.raises(syncenv.SyncFailed, match="without following links"):
        syncenv.sync(worktree, base, FakeUv(), {})


def test_what_cannot_be_removed_is_reported_not_raised(
    worktree: Path, base: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_rmtree(
        path: Path, onexc: Callable[[object, str, BaseException], None]
    ) -> None:
        onexc(None, str(path), PermissionError("denied"))

    monkeypatch.setattr(syncenv.shutil, "rmtree", failing_rmtree)
    monkeypatch.setattr(failing_rmtree, "avoids_symlink_attacks", True, raising=False)
    synced = syncenv.sync(worktree, base, FakeUv(), {})
    assert synced.left is not None
    assert "was left" in synced.left
    assert "denied" in synced.left
    assert (worktree / syncenv.ENVIRONMENT / "built").is_file()


def test_the_runner_gives_a_command_the_environment_it_is_given(
    tmp_path: Path,
) -> None:
    source = "import os; print(os.environ.get('SYNC_PROBE'), 'HOME' in os.environ)"
    done = land.run_command(
        (sys.executable, "-c", source), tmp_path, env={"SYNC_PROBE": "given"}
    )
    assert (done.code, done.stdout) == (0, "given False\n")
