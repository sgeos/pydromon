"""Gate omissions retain reasons while bounding terminal output."""

import subprocess
import sys
from pathlib import Path


def test_omitted_test_report_is_grouped_and_bounded(tmp_path: Path) -> None:
    sample = tmp_path / "test_omissions.py"
    sample.write_text(
        "import pytest\n"
        '@pytest.mark.parametrize("index", range(100))\n'
        "def test_skip(index):\n"
        '    pytest.skip("sample missing capability")\n'
        "def test_excluded():\n"
        "    pass\n",
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "dromon.testreport",
            "-k",
            "not excluded",
            str(sample),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "sample missing capability" in result.stdout
    assert "97 further tests in this group" in result.stdout
    assert "deselected by" in result.stdout
    assert "test_excluded" in result.stdout
    assert len(result.stdout) < 4000
