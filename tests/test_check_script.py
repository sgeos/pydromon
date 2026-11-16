"""Repository hygiene checks, including exact disclosure exceptions."""

from hashlib import sha256
import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest


def executable(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise RuntimeError(f"{name} is required for this test")
    return found


def check_project(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [executable("bash"), "scripts/check.sh", "--hygiene-only"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )


def test_disclosure_exceptions_match_the_exact_path_number_and_line(
    tmp_path: Path,
) -> None:
    root = tmp_path
    (root / "scripts").mkdir()
    (root / "secret").mkdir()
    (root / "tmp").mkdir()
    repository = Path(__file__).parents[1]
    shutil.copy2(repository / "scripts/check.sh", root / "scripts/check.sh")
    (root / "README.md").write_text("# Example\n", encoding="utf-8")
    (root / ".gitignore").write_text("secret/\n", encoding="utf-8")
    (root / "tmp/.gitkeep").write_text("", encoding="utf-8")
    (root / "src").mkdir()
    example = root / "src/example.py"
    accepted_text = 'private = "needle"'
    example.write_text(accepted_text + "\n", encoding="utf-8")
    (root / "secret/disclosure-terms.txt").write_text("needle\n", encoding="utf-8")
    (root / "secret/disclosure-accepted.txt").write_text(
        f"src/example.py\t1\t{accepted_text}\n", encoding="utf-8"
    )

    git = executable("git")
    subprocess.run(  # noqa: S603
        [git, "init", "-q"], cwd=root, check=True
    )
    subprocess.run(  # noqa: S603
        [git, "config", "user.name", "Test"], cwd=root, check=True
    )
    subprocess.run(  # noqa: S603
        [git, "config", "user.email", "test@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(  # noqa: S603
        [git, "add", "."], cwd=root, check=True
    )
    subprocess.run(  # noqa: S603
        [git, "commit", "-qm", "fixture"], cwd=root, check=True
    )

    accepted = check_project(root)
    assert accepted.returncode == 0, accepted.stderr
    assert "accepted path and line match(es)" in accepted.stdout

    changed_text = 'private = "needle changed"'
    example.write_text(changed_text + "\n", encoding="utf-8")
    refused = check_project(root)
    assert refused.returncode != 0
    assert "src/example.py:1" in refused.stderr
    assert changed_text not in refused.stderr

    # Replace the legacy private acceptance with the operator project table.
    (root / "secret/disclosure-accepted.txt").unlink()
    (root / ".dromon").mkdir()
    digest = sha256(changed_text.encode()).hexdigest()
    project_file = root / ".dromon/dromon.toml"
    project_file.write_text(
        f'version = 1\n[daemon.disclosure_accepted]\n"src/example.py" = ["{digest}"]\n',
        encoding="utf-8",
    )
    assert check_project(root).returncode == 0
    # Batch files permit CRLF under repository hygiene. The digest excludes CR.
    batch = root / "src/example.bat"
    batch.write_bytes((changed_text + "\r\n").encode())
    subprocess.run(  # noqa: S603
        [git, "add", "src/example.bat"], cwd=root, check=True
    )
    with project_file.open("a", encoding="utf-8") as handle:
        handle.write(f'"src/example.bat" = ["{digest}"]\n')
    assert check_project(root).returncode == 0
    example.write_text(changed_text + " again\n", encoding="utf-8")
    assert check_project(root).returncode != 0
    project_file.write_text(
        'version = 1\n[daemon.disclosure_accepted]\n"src/example.py" = ["bad"]\n',
        encoding="utf-8",
    )
    assert check_project(root).returncode != 0


# Round 17, item 1. A linked worktree has no secret/ of its own, so the scan reads the
# main checkout's list, and a clone, which has no main checkout, still skips.

TERM = "needle"
LINE = f'private = "{TERM}"'


def git_in(root: Path, *arguments: str) -> None:
    subprocess.run(  # noqa: S603
        [executable("git"), *arguments], cwd=root, check=True, capture_output=True
    )


@pytest.fixture
def main_checkout(tmp_path: Path) -> Path:
    """A repository holding a disclosure list whose term one tracked line holds,
    with no acceptance, so a scan that runs fails."""
    root = tmp_path / "main"
    (root / "scripts").mkdir(parents=True)
    (root / "secret").mkdir()
    (root / "tmp").mkdir()
    (root / "src").mkdir()
    repository = Path(__file__).parents[1]
    shutil.copy2(repository / "scripts/check.sh", root / "scripts/check.sh")
    (root / "README.md").write_text("# Example\n", encoding="utf-8")
    (root / ".gitignore").write_text("secret/\n.dromon/\n", encoding="utf-8")
    (root / "tmp/.gitkeep").write_text("", encoding="utf-8")
    (root / "src/example.py").write_text(LINE + "\n", encoding="utf-8")
    (root / "secret/disclosure-terms.txt").write_text(TERM + "\n", encoding="utf-8")
    git_in(root, "init", "-q")
    git_in(root, "config", "user.name", "Test")
    git_in(root, "config", "user.email", "test@example.invalid")
    git_in(root, "add", ".")
    git_in(root, "commit", "-qm", "fixture")
    return root


@pytest.fixture
def worktree(main_checkout: Path) -> Path:
    linked = main_checkout.parent / "linked"
    git_in(main_checkout, "worktree", "add", "-q", "-b", "feature", str(linked))
    return linked


def shown(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


def test_a_worktree_scans_with_the_main_checkout_s_list(worktree: Path) -> None:
    assert not (worktree / "secret").exists()
    result = check_project(worktree)
    assert result.returncode != 0, shown(result)
    assert "src/example.py:1" in result.stderr
    assert "so the scan uses the main checkout" in result.stdout
    assert TERM not in shown(result)


def test_a_worktree_uses_the_main_checkout_s_acceptance(
    main_checkout: Path, worktree: Path
) -> None:
    """The acceptance comes from the same checkout as the list."""
    (main_checkout / ".dromon").mkdir()
    digest = sha256(LINE.encode()).hexdigest()
    (main_checkout / ".dromon/dromon.toml").write_text(
        f'version = 1\n[daemon.disclosure_accepted]\n"src/example.py" = ["{digest}"]\n',
        encoding="utf-8",
    )
    result = check_project(worktree)
    assert result.returncode == 0, shown(result)
    assert "accepted path and line match(es)" in result.stdout
    (main_checkout / "secret/disclosure-accepted.txt").write_text(
        f"src/example.py\t1\t{LINE}\n", encoding="utf-8"
    )
    (main_checkout / ".dromon/dromon.toml").unlink()
    assert check_project(worktree).returncode == 0


def test_a_worktree_s_own_list_is_used_first(
    main_checkout: Path, worktree: Path
) -> None:
    (worktree / "secret").mkdir()
    (worktree / "secret/disclosure-terms.txt").write_text("absent\n", encoding="utf-8")
    result = check_project(worktree)
    assert result.returncode == 0, shown(result)
    assert "found no listed term" in result.stdout
    assert "main checkout" not in result.stdout


def test_a_clone_still_skips_with_its_message(main_checkout: Path) -> None:
    clone = main_checkout.parent / "clone"
    git_in(main_checkout.parent, "clone", "-q", str(main_checkout), str(clone))
    result = check_project(clone)
    assert result.returncode == 0, shown(result)
    assert (
        "no secret/disclosure-terms.txt, so the disclosure scan was skipped"
        in result.stdout
    )


def test_the_main_checkout_without_a_list_skips(
    main_checkout: Path, worktree: Path
) -> None:
    (main_checkout / "secret/disclosure-terms.txt").unlink()
    result = check_project(worktree)
    assert result.returncode == 0, shown(result)
    assert "so the disclosure scan was skipped" in result.stdout


@pytest.fixture
def refused(main_checkout: Path) -> Iterator[list[Path]]:
    """Paths made unreadable for a test, and readable again after it, so the
    temporary directory can be removed."""
    paths: list[Path] = []
    yield paths
    for path in paths:
        path.chmod(0o644)


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any file"
)
@pytest.mark.parametrize(
    "unreadable",
    [
        "secret/disclosure-terms.txt",
        "secret/disclosure-accepted.txt",
        ".dromon/dromon.toml",
    ],
)
def test_a_refused_read_skips_and_says_why(
    main_checkout: Path, worktree: Path, refused: list[Path], unreadable: str
) -> None:
    """A sandbox refuses a read with a permission error, as an unreadable file
    does. The gate does not fail on it, since it says nothing of the tracked files,
    and does not report a pass either."""
    (main_checkout / ".dromon").mkdir()
    (main_checkout / ".dromon/dromon.toml").write_text(
        "version = 1\n", encoding="utf-8"
    )
    (main_checkout / "secret/disclosure-accepted.txt").write_text(
        "# none\n", encoding="utf-8"
    )
    path = main_checkout / unreadable
    path.chmod(0)
    refused.append(path)
    result = check_project(worktree)
    assert result.returncode == 0, shown(result)
    assert f"SKIPPED the disclosure scan, because {path.resolve()}" in result.stdout
    assert "could not be read, with the error" in result.stdout
    assert "disclosure scan found no listed term" not in result.stdout
    assert TERM not in shown(result)
