"""Bounded gate reporting of tests omitted from execution.

**Round 15, item 2.** Besides the grouped list, the summary ends with one line that
names what was not run here and why, with counts, for the three kinds the gate leaves
out on purpose. Socket tests are deselected inside an agent sandbox, which denies
binding a Unix domain socket. Live tests are deselected unless asked for, because they
spend a subscription. Process-table tests are skipped where ``ps`` cannot run. A green
gate that left out a kind says so on one line, so it cannot be read as having run it.
The process-table kind is recognised by its skip reason, ``PROCESS_TABLE_REASON``,
which every such test shares, and a test in ``tests/test_testreport.py`` finds any that
does not. **Limit.** A skip with another reason is counted in the grouped list only.
"""

import faulthandler
import os
import threading
import time
from collections import Counter
from typing import Final

import pytest
from _pytest.terminal import TerminalReporter

_MAX_GROUPS = 20
_MAX_EXAMPLES = 3
PROCESS_TABLE_REASON = "ps cannot run here"
"""The start of the skip reason of a test that needs the real process table."""

_KINDS = ("socket", "live", "process-table")
_groups: dict[str, list[str]] = {}
_counts: Counter[str] = Counter()
_kinds: Counter[str] = Counter()
_why: dict[str, str] = {}


SESSION_REPORT_SECONDS: Final = 1800.0
"""When the run first says where it is, in seconds after it began. **Round 17, item 7.**
A macOS job was cancelled at the job's limit and left no log that named the test it
was in, since the summary comes at the end and the per-test fault bound of five
minutes does not fire for a run made of many tests that are each only slow. Thirty
minutes is after the slowest healthy gate seen, 28 minutes on Linux, so a healthy run
says nothing, and ten minutes before the workflow's step limit of 40."""

SESSION_REPORT_REPEAT_SECONDS: Final = 120.0
"""How often it says so again."""

SESSION_REPORT_ENV: Final = "DROMON_SESSION_REPORT_SECONDS"
"""A number of seconds that replaces ``SESSION_REPORT_SECONDS``, for a test of this."""

MAX_SESSION_REPORTS: Final = 20
"""The most reports a run writes, so a run that hangs for hours cannot fill a log."""


def _copy_stderr() -> int | None:
    """A copy of the real standard error, taken when this module loads, which is before
    pytest starts capturing, since the module is loaded by ``-p``. The report must reach
    the log while a test runs, and capture would take it."""
    try:
        return os.dup(2)
    except OSError:
        return None


_STDERR_COPY: Final = _copy_stderr()


def session_line(elapsed: float, current: str | None, number: int) -> str:
    """The report line, naming the running test and the time since the run began. The
    name of a test is a path and a function name, and carries no value."""
    where = current if current else "no test, collecting or between tests"
    return (
        f"dromond-gate: the run is {elapsed:.0f} seconds old and is at {where[:200]}"
        f" (report {number})\n"
    )


class SessionWatch(threading.Thread):
    """Say, from a thread of its own, where the run is, past capture and unbuffered,
    first after ``first`` seconds and then every ``repeat`` seconds, with the traceback
    of every thread the first time. Ends when ``stop`` is set. **Round 17, item 7.** A
    job that the runner cancels at its limit keeps what was written to its log up to
    then, so a line written while the run is still going is what a cancelled job can
    show. Never raises into the run."""

    def __init__(
        self, first: float, repeat: float, descriptor: int | None = _STDERR_COPY
    ) -> None:
        super().__init__(name="dromon-session-watch", daemon=True)
        self.first = first
        self.repeat = repeat
        self.descriptor = descriptor
        self.stop = threading.Event()
        self.began = time.monotonic()
        self.written = 0

    def run(self) -> None:
        wait = self.first
        while self.written < MAX_SESSION_REPORTS and not self.stop.wait(wait):
            wait = self.repeat
            self._report()

    def _report(self) -> None:
        descriptor = self.descriptor
        if descriptor is None:
            return
        self.written += 1
        try:
            line = session_line(
                time.monotonic() - self.began,
                os.environ.get("PYTEST_CURRENT_TEST"),
                self.written,
            )
            os.write(descriptor, line.encode(errors="replace"))
            if self.written == 1:
                faulthandler.dump_traceback(file=descriptor, all_threads=True)
        except (OSError, ValueError):
            return


_watch: SessionWatch | None = None


def _report_seconds() -> float:
    try:
        value = float(os.environ.get(SESSION_REPORT_ENV, ""))
    except ValueError:
        return SESSION_REPORT_SECONDS
    return value if value > 0 else SESSION_REPORT_SECONDS


def pytest_sessionstart() -> None:
    global _watch  # noqa: PLW0603
    if _watch is not None:
        _watch.stop.set()
    _watch = SessionWatch(_report_seconds(), SESSION_REPORT_REPEAT_SECONDS)
    _watch.start()
    _groups.clear()
    _counts.clear()
    _kinds.clear()
    _why.clear()


def _record(reason: str, nodeid: str) -> None:
    reason = reason[:240]
    if reason not in _groups and len(_groups) >= _MAX_GROUPS:
        reason = "additional omission reasons"
    _counts[reason] += 1
    examples = _groups.setdefault(reason, [])
    if len(examples) < _MAX_EXAMPLES:
        examples.append(nodeid[:240])


def pytest_deselected(items: list[pytest.Item]) -> None:
    for item in items:
        expression = item.config.option.markexpr
        keyword = item.config.option.keyword
        _record(f"deselected by -m {expression!r} and -k {keyword!r}", item.nodeid)
        # A test marked both is counted once, as live, which is the stronger reason.
        for kind in ("live", "socket"):
            if item.get_closest_marker(kind) is not None:
                _kinds[kind] += 1
                _why[kind] = f"deselected by -m {expression!r} and -k {keyword!r}"
                break


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.skipped:
        reason = report.longrepr
        text = reason[2] if isinstance(reason, tuple) else str(reason)
        _record(text, report.nodeid)
        # pytest writes a skip reason as "Skipped: REASON".
        why = text.removeprefix("Skipped: ")
        if why.startswith(PROCESS_TABLE_REASON):
            _kinds["process-table"] += 1
            _why["process-table"] = f"skipped, {why[:120]}"


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.skipped:
        _record(str(report.longrepr), report.nodeid)


def omission_line() -> str:
    """The one line, for the kinds in ``_KINDS`` in that order. A kind with none
    omitted is named with a count of zero, so the line always says all three."""
    parts = [
        f"{_kinds[kind]} {kind} tests"
        + (f" ({_why[kind]})" if _kinds[kind] else " (all run)")
        for kind in _KINDS
    ]
    return "not run here: " + ", ".join(parts)


def pytest_terminal_summary(terminalreporter: TerminalReporter) -> None:
    if _groups:
        terminalreporter.write_sep("=", "omitted tests, grouped and bounded")
    for reason, examples in _groups.items():
        terminalreporter.write_line(f"{_counts[reason]} tests | {reason}")
        for nodeid in examples:
            terminalreporter.write_line(f"  {nodeid}")
        remaining = _counts[reason] - len(examples)
        if remaining:
            terminalreporter.write_line(f"  {remaining} further tests in this group")
    terminalreporter.write_line(omission_line())


def pytest_sessionfinish() -> None:
    if _watch is not None:
        _watch.stop.set()
