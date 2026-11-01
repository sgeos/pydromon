"""A worktree's build inputs against ``origin/main``, which ``dromon sync-worktrees``
shows before it syncs. Round 14, audit finding S1, and the re-review of 522c146,
S1b. The comparisons run real git on a temporary repository, with no remote, whose
``origin/main`` is set by hand."""

import os
import subprocess
from pathlib import Path

import pytest

from dromon import buildinputs
from dromon.buildinputs import WHOLE_TREE, BuildInputsUnknown

PROJECT = """\
[project]
name = "demo"

[build-system]
requires = ["uv_build"]
build-backend = "uv_build"

[tool.uv.sources]
foo = { path = "vendor/foo" }
"""

LOCK = """\
version = 1

[[package]]
name = "demo"
source = { editable = "." }

[[package]]
name = "foo"
source = { directory = "vendor/foo" }

[[package]]
name = "bar"
source = { editable = "./libs/bar" }

[[package]]
name = "idna"
source = { registry = "https://pypi.org/simple" }
"""

CONFIG = buildinputs.CONFIG_FILES


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
    """A repository whose ``origin/main`` holds a project built by uv's own backend,
    with a vendored dependency taken from a local path, which has a backend of its
    own, and a second local source the lock alone names."""
    root = tmp_path / "work"
    root.mkdir()
    git(root, "init", "--quiet", "--initial-branch=main")
    (root / "pyproject.toml").write_text(PROJECT)
    (root / "uv.lock").write_text(LOCK)
    (root / "vendor" / "foo").mkdir(parents=True)
    (root / "vendor" / "foo" / "backend.py").write_text("# backend\n")
    (root / "libs" / "bar").mkdir(parents=True)
    (root / "libs" / "bar" / "setup.py").write_text("# setup\n")
    (root / "src.py").write_text("print('code')\n")
    git(root, "add", "-A")
    git(root, "commit", "--quiet", "-m", "base")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def write_project(directory: Path, project: str, lock: str = "version = 1\n") -> None:
    (directory / "pyproject.toml").write_text(project)
    (directory / "uv.lock").write_text(lock)


def test_the_inputs_are_the_configuration_and_each_local_source_once(
    repository: Path,
) -> None:
    found = buildinputs.build_inputs(repository)
    assert found.unbounded is None
    assert found.paths == (*CONFIG, "vendor/foo", "libs/bar")


def test_this_projects_own_inputs_are_bounded() -> None:
    """The real project, built by uv's own backend with no local source, compares
    its configuration files alone, so a worktree that changed only code needs no
    question."""
    root = Path(__file__).resolve().parents[1]
    found = buildinputs.build_inputs(root)
    assert (found.paths, found.unbounded) == (CONFIG, None)


def test_a_project_uv_does_not_build_is_bounded(tmp_path: Path) -> None:
    write_project(tmp_path, '[project]\nname = "demo"\n')
    assert buildinputs.build_inputs(tmp_path) == buildinputs.Inputs(CONFIG)


@pytest.mark.parametrize(
    ("project", "lock", "uv_toml", "reason"),
    [
        (
            '[build-system]\nbuild-backend = "uv_build"\nbackend-path = ["tools"]\n',
            "",
            None,
            "backend-path loads a build backend",
        ),
        ('[tool.uv.workspace]\nmembers = ["packages/*"]\n', "", None, "workspace"),
        (
            '[build-system]\nbuild-backend = "hatchling.build"\n',
            "",
            None,
            "hatchling.build may run any file",
        ),
        ("[tool.uv]\npackage = true\n", "", None, "setuptools, uv's default"),
        ('[tool.uv]\nfind-links = ["./wheels"]\n', "", None, "sets find-links"),
        (
            '[[tool.uv.index]]\nname = "here"\nurl = "file:///srv/index"\n',
            "",
            None,
            "[tool.uv] sets index",
        ),
        ("", "", 'cache-dir = "cache"\n', "uv.toml sets cache-dir"),
        ("", "", 'python-install-mirror = "./py"\n', "python-install-mirror"),
        (
            '[tool.uv.sources]\nfoo = { git = "file:///srv/foo.git" }\n',
            "",
            None,
            "takes foo from a local git",
        ),
        (
            "[tool.uv.sources]\nfoo = [{ workspace = true }]\n",
            "",
            None,
            "takes foo from a uv workspace",
        ),
        (
            "",
            '[[package]]\nname = "idna"\nsource = { registry = "../wheels" }\n',
            None,
            "installs idna from a local registry",
        ),
        ("[project\n", "", None, "pyproject.toml cannot be read as TOML"),
        ("", "version = \n", None, "uv.lock cannot be read as TOML"),
        ("", "", "cache-dir = \n", "uv.toml cannot be read as TOML"),
    ],
)
def test_inputs_that_cannot_be_bounded_compare_the_whole_tree(
    tmp_path: Path, project: str, lock: str, uv_toml: str | None, reason: str
) -> None:
    """Re-review of 522c146, S1b. Each of these lets a build read files no fixed
    list names, so the whole worktree is compared, with the reason."""
    write_project(
        tmp_path, f'[project]\nname = "demo"\n{project}', f"version = 1\n{lock}"
    )
    if project.startswith("[project\n"):
        (tmp_path / "pyproject.toml").write_text(project)
    if uv_toml is not None:
        (tmp_path / "uv.toml").write_text(uv_toml)
    found = buildinputs.build_inputs(tmp_path)
    assert found.paths == (WHOLE_TREE,)
    assert found.unbounded is not None and reason in found.unbounded


@pytest.mark.parametrize(
    "setting",
    [
        '[tool.uv]\nfind-links = ["https://example.invalid/wheels"]\n',
        '[[tool.uv.index]]\nname = "there"\nurl = "https://example.invalid/simple"\n',
        '[tool.uv.sources]\nfoo = { git = "https://example.invalid/foo.git" }\n',
    ],
)
def test_a_remote_location_keeps_the_inputs_bounded(
    tmp_path: Path, setting: str
) -> None:
    """The controls of the test above, each with a remote place."""
    write_project(tmp_path, f'[project]\nname = "demo"\n{setting}')
    assert buildinputs.build_inputs(tmp_path).unbounded is None


@pytest.mark.parametrize(
    ("project", "lock"),
    [
        ('[tool.uv.sources]\nfoo = { path = "../outside" }\n', ""),
        ("", '[[package]]\nname = "foo"\nsource = { directory = "/srv/foo" }\n'),
        ("", '[[package]]\nname = "foo"\nsource = { path = 3 }\n'),
    ],
)
def test_a_local_source_outside_the_worktree_or_malformed_is_refused(
    tmp_path: Path, project: str, lock: str
) -> None:
    write_project(
        tmp_path, f'[project]\nname = "demo"\n{project}', f"version = 1\n{lock}"
    )
    with pytest.raises(BuildInputsUnknown, match="source"):
        buildinputs.build_inputs(tmp_path)


def test_too_many_local_sources_are_refused(tmp_path: Path) -> None:
    packages = "".join(
        f'[[package]]\nname = "p{n}"\nsource = {{ directory = "d{n}" }}\n'
        for n in range(buildinputs.MAX_LOCAL_SOURCES + 1)
    )
    write_project(tmp_path, '[project]\nname = "demo"\n', f"version = 1\n{packages}")
    with pytest.raises(BuildInputsUnknown, match="more than"):
        buildinputs.build_inputs(tmp_path)


@pytest.mark.parametrize(
    "backend_path",
    ['["../outside"]', '["/etc"]', '["a/../../b"]', '[""]', "[3]", '"build_tools"'],
)
def test_a_backend_path_outside_the_worktree_or_malformed_is_refused(
    tmp_path: Path, backend_path: str
) -> None:
    write_project(tmp_path, f"[build-system]\nbackend-path = {backend_path}\n")
    with pytest.raises(BuildInputsUnknown, match="backend-path"):
        buildinputs.build_inputs(tmp_path)


def test_too_many_backend_paths_are_refused(tmp_path: Path) -> None:
    listed = ", ".join(f'"d{n}"' for n in range(buildinputs.MAX_BACKEND_PATHS + 1))
    write_project(tmp_path, f"[build-system]\nbackend-path = [{listed}]\n")
    with pytest.raises(BuildInputsUnknown, match="more than"):
        buildinputs.build_inputs(tmp_path)


def test_a_worktree_equal_to_the_base_does_not_differ(repository: Path) -> None:
    (repository / "src.py").write_text("print('changed code')\n")
    found = buildinputs.differences(repository)
    assert not found.differ


def test_an_uncommitted_lock_change_differs(repository: Path) -> None:
    with (repository / "uv.lock").open("a") as lock:
        lock.write("# elsewhere\n")
    found = buildinputs.differences(repository)
    assert found.differ
    assert "+# elsewhere" in found.diff


def test_an_edited_backend_of_a_local_source_differs(repository: Path) -> None:
    """Re-review of 522c146, S1b. The vendored dependency's own backend, edited and
    committed with no change to ``pyproject.toml`` or ``uv.lock``, was not shown."""
    (repository / "vendor" / "foo" / "backend.py").write_text("import os  # changed\n")
    git(repository, "commit", "--quiet", "-am", "change the vendored backend")
    found = buildinputs.differences(repository)
    assert "vendor/foo/backend.py" in found.diff
    (repository / "libs" / "bar" / "hook.py").write_text("# new\n")
    assert buildinputs.differences(repository).untracked == ("libs/bar/hook.py",)


PINNED_CONFIG = (
    "pyproject.toml",
    "uv.lock",
    "uv.toml",
    ".python-version",
    ".python-versions",
)


def test_the_compared_configuration_is_pinned() -> None:
    """Final check of 951b9e7, item 3. The other tests take the compared files from
    the module, so a file dropped from it passed them all."""
    assert buildinputs.CONFIG_FILES == PINNED_CONFIG


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("uv.toml", "native-tls = true\n"),
        (".python-version", "3.12\n"),
        (".python-versions", "3.12\n"),
    ],
)
def test_a_new_configuration_file_differs(
    repository: Path, name: str, text: str
) -> None:
    """Final check of 951b9e7, item 3. Each file is valid, so the inputs stay
    bounded and the file is found because it is compared, not because an unreadable
    ``uv.toml`` made the whole tree compared."""
    (repository / name).write_text(text)
    found = buildinputs.differences(repository)
    assert found.unbounded is None
    assert found.untracked == (name,)
    assert found.differ


def test_a_changed_uv_toml_differs(repository: Path) -> None:
    (repository / "uv.toml").write_text("native-tls = true\n")
    git(repository, "add", "uv.toml")
    git(repository, "commit", "--quiet", "-m", "configure uv")
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    (repository / "uv.toml").write_text("native-tls = false\n")
    found = buildinputs.differences(repository)
    assert found.unbounded is None
    assert "+native-tls = false" in found.diff


def test_an_unbounded_worktree_differs_in_any_file_and_says_why(
    repository: Path,
) -> None:
    text = PROJECT.replace('"uv_build"\n\n', '"hatchling.build"\n\n')
    (repository / "pyproject.toml").write_text(text)
    git(repository, "commit", "--quiet", "-am", "another backend")
    git(repository, "update-ref", "refs/remotes/origin/main", "HEAD")
    assert not buildinputs.differences(repository).differ
    (repository / "src.py").write_text("print('changed code')\n")
    found = buildinputs.differences(repository)
    assert found.paths == (WHOLE_TREE,) and "src.py" in found.diff
    said: list[str] = []
    buildinputs.show(found, repository, said.append)
    assert said[0] == (
        "    the whole worktree is compared, since its build backend hatchling.build"
        " may run any file of the project"
    )


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
