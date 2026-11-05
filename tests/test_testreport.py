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


def _run_sample(tmp_path: Path, source: str, *arguments: str) -> str:
    sample = tmp_path / "test_kinds.py"
    sample.write_text(source, encoding="utf-8")
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "dromon.testreport",
            "-o",
            "markers=socket: s\nlive: l",
            *arguments,
            str(sample),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


KINDS_SOURCE = (
    "import pytest\n"
    "@pytest.mark.socket\n"
    "def test_a(): pass\n"
    "@pytest.mark.socket\n"
    "def test_b(): pass\n"
    "@pytest.mark.live\n"
    "def test_c(): pass\n"
    "@pytest.mark.skipif(True, reason='ps cannot run here, as inside a sandbox')\n"
    "def test_d(): pass\n"
    "@pytest.mark.skipif(True, reason='ps cannot run here, as inside a sandbox')\n"
    "def test_e(): pass\n"
    "@pytest.mark.skipif(True, reason='some other reason')\n"
    "def test_f(): pass\n"
    "def test_g(): pass\n"
)


def test_one_line_names_the_socket_live_and_process_table_tests_left_out(
    tmp_path: Path,
) -> None:
    out = _run_sample(tmp_path, KINDS_SOURCE, "-m", "not socket and not live")
    lines = [line for line in out.splitlines() if line.startswith("not run here:")]
    assert len(lines) == 1, out
    line = lines[0]
    assert "2 process-table tests (skipped, ps cannot run here" in line
    assert "0 socket tests (all run)" not in line
    # Deselected by the expression, so the socket and live counts are the marked ones.
    assert "1 live tests (deselected by -m 'not socket and not live'" in line
    assert "2 socket tests (deselected by -m 'not socket and not live'" in line
    # A skip with another reason is not counted among the three.
    assert "some other reason" not in line


def test_the_line_says_all_run_for_a_kind_nothing_left_out(tmp_path: Path) -> None:
    out = _run_sample(tmp_path, "def test_g(): pass\n")
    line = next(x for x in out.splitlines() if x.startswith("not run here:"))
    assert line == (
        "not run here: 0 socket tests (all run), 0 live tests (all run),"
        " 0 process-table tests (all run)"
    )


def test_a_test_marked_socket_and_live_is_counted_once_as_live(tmp_path: Path) -> None:
    source = (
        "import pytest\n@pytest.mark.socket\n@pytest.mark.live\ndef test_x(): pass\n"
    )
    out = _run_sample(tmp_path, source + "def test_y(): pass\n", "-m", "not live")
    line = next(x for x in out.splitlines() if x.startswith("not run here:"))
    assert "1 live tests" in line and "0 socket tests (all run)" in line


def test_every_process_table_skip_in_the_suite_uses_the_shared_reason() -> None:
    """The one line counts these by their reason, so a test that skips for the same
    cause with other words would drop out of the count without a sign."""
    import re

    from dromon.testreport import PROCESS_TABLE_REASON

    tests = Path(__file__).resolve().parent
    pattern = re.compile(r'reason="(ps [^"]*)"')
    found = [
        (path.name, match.group(1))
        for path in sorted(tests.glob("test_*.py"))
        for match in pattern.finditer(path.read_text(encoding="utf-8"))
    ]
    assert found, "no process-table skip was found, so this check checks nothing"
    assert all(reason.startswith(PROCESS_TABLE_REASON) for _, reason in found), found
