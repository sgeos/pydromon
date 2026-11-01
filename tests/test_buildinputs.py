"""A worktree's build inputs against ``origin/main``, which ``dromon sync-worktrees``
shows before it syncs. Round 14, audit finding S1. The comparisons run real git on a
temporary repository, with no remote, whose ``origin/main`` is set by hand."""

import os
import subprocess
from pathlib import Path

import pytest

from dromon import buildinputs
from dromon.buildinputs import BuildInputsUnknown

BACKEND_PROJECT = """\
[project]
name = "demo"

[build-system]
requires = []
build-backend = "backend"
backend-path = ["build_tools", "./build_tools", "other"]
"""


def git(repository: Path, *args: str) -> str:
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    return subprocess.run(  # noqa: S603
        ["git", "-c", "core.hooksPath=/dev/null", *args],  # noqa: S607
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A repository whose ``origin/main`` holds a project with a local backend."""
    root = tmp_path / "work"
    root.mkdir()
    git(root, "init", "--quiet", "--initial-branch=main")
    (root / "pyproject.toml").write_text(BACKEND_PROJECT)
    (root / "uv.lock").write_text("version = 1\n")
    (root / "build_tools").mkdir()
    (root / "build_tools" / "backend.py").write_text("# backend\n")
    (root / "src.py").write_text("print('code')\n")
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "base")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def test_build_paths_names_the_files_and_each_backend_path_once(
    repository: Path,
) -> None:
    assert buildinputs.build_paths(repository) == (
        "pyproject.toml",
        "uv.lock",
        "build_tools",
        "other",
    )


@pytest.mark.parametrize(
    "backend_path",
    ['["../outside"]', '["/etc"]', '["a/../../b"]', '[""]', "[3]", '"build_tools"'],
)
def test_a_backend_path_outside_the_worktree_or_malformed_is_refused(
    tmp_path: Path, backend_path: str
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f"[build-system]\nbackend-path = {backend_path}\n"
    )
    with pytest.raises(BuildInputsUnknown, match="backend-path"):
        buildinputs.build_paths(tmp_path)


def test_too_many_backend_paths_are_refused(tmp_path: Path) -> None:
    listed = ", ".join(f'"d{n}"' for n in range(buildinputs.MAX_BACKEND_PATHS + 1))
    (tmp_path / "pyproject.toml").write_text(
        f"[build-system]\nbackend-path = [{listed}]\n"
    )
    with pytest.raises(BuildInputsUnknown, match="more than"):
        buildinputs.build_paths(tmp_path)


def test_an_unparsable_project_file_still_compares_the_two_files(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text("[build-system\n")
    assert buildinputs.build_paths(tmp_path) == ("pyproject.toml", "uv.lock")


def test_a_worktree_equal_to_the_base_does_not_differ(repository: Path) -> None:
    (repository / "src.py").write_text("print('changed code')\n")
    found = buildinputs.differences(repository)
    assert not found.differ


def test_an_uncommitted_lock_change_differs(repository: Path) -> None:
    (repository / "uv.lock").write_text("version = 1\nsource = 'elsewhere'\n")
    found = buildinputs.differences(repository)
    assert found.differ
    assert "+source = 'elsewhere'" in found.diff


def test_a_committed_backend_change_differs(repository: Path) -> None:
    (repository / "build_tools" / "backend.py").write_text("import os  # changed\n")
    git(repository, "commit", "--quiet", "-am", "change the backend")
    found = buildinputs.differences(repository)
    assert "build_tools/backend.py" in found.diff


def test_a_new_untracked_backend_file_differs(repository: Path) -> None:
    (repository / "other").mkdir()
    (repository / "other" / "hook.py").write_text("# new\n")
    found = buildinputs.differences(repository)
    assert found.untracked == ("other/hook.py",)
    assert found.differ


def test_no_base_raises(repository: Path) -> None:
    git(repository, "update-ref", "-d", "refs/remotes/origin/main")
    with pytest.raises(BuildInputsUnknown, match="git diff against origin/main"):
        buildinputs.differences(repository)


def test_show_bounds_the_diff_and_names_the_command(tmp_path: Path) -> None:
    lines = [f"+line {n}" for n in range(buildinputs.MAX_SHOWN_LINES + 5)]
    untracked = tuple(f"f{n}" for n in range(buildinputs.MAX_UNTRACKED_SHOWN + 2))
    found = buildinputs.Differences(
        ("pyproject.toml", "uv.lock"), "\n".join(lines), untracked
    )
    said: list[str] = []
    buildinputs.show(found, tmp_path, said.append)
    assert len(said) == buildinputs.MAX_SHOWN_LINES + 1 + (
        buildinputs.MAX_UNTRACKED_SHOWN + 1
    )
    assert "5 more lines" in said[buildinputs.MAX_SHOWN_LINES]
    assert (
        "diff origin/main -- pyproject.toml uv.lock"
        in said[buildinputs.MAX_SHOWN_LINES]
    )
    assert said[-1].endswith("2 more untracked files")
