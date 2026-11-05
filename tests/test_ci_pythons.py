"""The Python versions the gate type-checks are those the workflow tests. Round 15."""

import subprocess
import sys
from pathlib import Path

import pytest
from ci_pythons import WorkflowError, versions

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ci_pythons.py"


def test_the_matrix_line_is_read_in_order() -> None:
    text = 'matrix:\n  os: [a, b]\n  python: ["3.12", "3.15"]  # newest\n'
    assert versions(text) == ["3.12", "3.15"]
    assert versions("  python: ['3.13']\n") == ["3.13"]


@pytest.mark.parametrize(
    ("text", "phrase"),
    [
        ("name: CI\n", "found 0"),
        ('python: ["3.12"]\npython: ["3.13"]\n', "found 2"),
        ("python: []\n", "not a MAJOR.MINOR"),
        ('python: ["3.12", "3"]\n', "not a MAJOR.MINOR"),
        ('python: ["3.12", "3.12"]\n', "listed twice"),
        ('python: ["3.12", "latest"]\n', "not a MAJOR.MINOR"),
        ("python:\n  - '3.12'\n", "found 0"),
    ],
)
def test_a_workflow_that_does_not_list_versions_in_the_form_is_an_error(
    text: str, phrase: str
) -> None:
    with pytest.raises(WorkflowError, match=phrase):
        versions(text)


def test_the_real_workflow_lists_versions_and_the_command_prints_them() -> None:
    expected = versions((ROOT / ".github" / "workflows" / "ci.yml").read_text())
    assert expected, "the workflow lists no version"
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(SCRIPT), str(ROOT / ".github/workflows/ci.yml")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0 and result.stdout.split() == expected


def test_the_command_fails_on_a_missing_or_unreadable_workflow(tmp_path: Path) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", str(SCRIPT), str(tmp_path / "absent.yml")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1 and result.stdout == ""
    assert "ci_pythons.py" in result.stderr


def test_the_gate_type_checks_every_listed_version() -> None:
    """The gate reads the list from the script, and runs pyright for each version on
    both platforms, so a version added to the matrix is checked without another edit."""
    gate = (ROOT / "scripts" / "check.sh").read_text()
    assert "scripts/ci_pythons.py" in gate
    assert "--pythonversion" in gate
    assert "--pythonplatform" in gate
