"""Repository hygiene checks, including exact disclosure exceptions."""

from hashlib import sha256
import shutil
import subprocess
from pathlib import Path


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
    example.write_text(changed_text + " again\n", encoding="utf-8")
    assert check_project(root).returncode != 0
    project_file.write_text(
        'version = 1\n[daemon.disclosure_accepted]\n"src/example.py" = ["bad"]\n',
        encoding="utf-8",
    )
    assert check_project(root).returncode != 0
