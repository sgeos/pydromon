"""Gate omissions retain reasons while bounding terminal output."""

import os
import select
import sys
import time
from pathlib import Path

import pytest

from dromon import testreport
from dromon import spawn


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
    result = spawn.run(
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
        timeout=300,
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
    result = spawn.run(
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
        timeout=300,
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


# Round 17, item 7. The run says where it is while it is still going.


def _read_all(descriptor: int) -> str:
    chunks: list[bytes] = []
    while True:
        ready, _, _ = select.select([descriptor], [], [], 0.0)
        if not ready:
            break
        chunk = os.read(descriptor, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode(errors="replace")


def test_the_session_line_names_the_running_test_and_the_age_of_the_run() -> None:
    line = testreport.session_line(1234.6, "tests/test_x.py::test_y (call)", 2)
    assert line == (
        "dromond-gate: the run is 1235 seconds old and is at"
        " tests/test_x.py::test_y (call) (report 2)\n"
    )
    assert "between tests" in testreport.session_line(1.0, None, 1)
    assert len(testreport.session_line(1.0, "x" * 5000, 1)) < 400


def test_the_session_watch_reports_while_the_run_goes_on_and_dumps_threads_once() -> (
    None
):
    read, write = os.pipe()
    try:
        watch = testreport.SessionWatch(0.05, 0.05, write)
        watch.start()
        deadline = time.monotonic() + 10
        text = ""
        while time.monotonic() < deadline and text.count("dromond-gate: ") < 2:
            time.sleep(0.02)
            text += _read_all(read)
        watch.stop.set()
        watch.join(10)
        assert not watch.is_alive()
        assert text.count("dromond-gate: ") >= 2
        assert "(report 1)" in text and "(report 2)" in text
        assert "test_the_session_watch_reports" in text, "the running test is named"
        assert text.count("Current thread") + text.count("Thread 0x") >= 1
        first, _, rest = text.partition("(report 2)")
        assert "most recent call first" in first, "the first report has the dump"
        assert "most recent call first" not in rest, "later reports do not repeat it"
    finally:
        os.close(read)
        os.close(write)


def test_the_session_watch_stops_when_asked_before_it_reports() -> None:
    read, write = os.pipe()
    try:
        watch = testreport.SessionWatch(60.0, 60.0, write)
        watch.start()
        watch.stop.set()
        watch.join(10)
        assert not watch.is_alive()
        assert _read_all(read) == ""
    finally:
        os.close(read)
        os.close(write)


def test_the_session_watch_is_bounded_in_reports_and_survives_a_closed_descriptor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(testreport, "MAX_SESSION_REPORTS", 3)
    read, write = os.pipe()
    os.close(write)
    try:
        watch = testreport.SessionWatch(0.01, 0.01, write)
        watch.start()
        watch.join(10)
        assert not watch.is_alive(), "it ended by itself at its bound"
        assert watch.written == 3
        assert testreport.SessionWatch(1.0, 1.0, None).descriptor is None
    finally:
        os.close(read)


def test_a_watch_without_a_descriptor_writes_nothing_and_ends_at_its_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(testreport, "MAX_SESSION_REPORTS", 2)
    watch = testreport.SessionWatch(0.01, 0.01, None)
    watch._report()  # pyright: ignore[reportPrivateUsage]
    assert watch.written == 0


@pytest.mark.parametrize(
    ("value", "expected"),
    [("30", 30.0), ("0.5", 0.5), ("", None), ("x", None), ("0", None), ("-4", None)],
)
def test_the_report_time_comes_from_the_environment_or_the_default(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: float | None
) -> None:
    monkeypatch.setenv(testreport.SESSION_REPORT_ENV, value)
    default = testreport.SESSION_REPORT_SECONDS
    assert testreport._report_seconds() == (  # pyright: ignore[reportPrivateUsage]
        default if expected is None else expected
    )


def test_the_default_report_comes_after_a_healthy_gate_and_before_the_step_limit() -> (
    None
):
    """The workflow's gate step is bounded at 40 minutes and the job at 45. A Linux
    gate that passed took 28, so a healthy run says nothing, and a run that is still
    going at 30 minutes has said where it is, with room for several repeats, before
    the step ends."""
    healthy, step = 28 * 60, 40 * 60
    assert healthy < testreport.SESSION_REPORT_SECONDS < step
    assert (
        step
        > testreport.SESSION_REPORT_SECONDS
        + 3 * testreport.SESSION_REPORT_REPEAT_SECONDS
    )
    assert (
        testreport.SESSION_REPORT_REPEAT_SECONDS * testreport.MAX_SESSION_REPORTS
        < 4 * 3600
    )


def test_a_slow_run_names_its_test_in_the_log_past_capture(tmp_path: Path) -> None:
    """The whole path, in a pytest of its own. The run is made slow by one test, and
    the report reaches the real standard error while that test is still running, which
    pytest's capture would otherwise hold until the end."""
    sample = tmp_path / "test_slow.py"
    sample.write_text(
        "import time\n\ndef test_the_slow_one():\n    time.sleep(2.0)\n",
        encoding="utf-8",
    )
    environment = {**os.environ, testreport.SESSION_REPORT_ENV: "0.4"}
    result = spawn.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "dromon.testreport", str(sample)],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "dromond-gate: the run is " in result.stderr
    assert "test_slow.py::test_the_slow_one" in result.stderr
