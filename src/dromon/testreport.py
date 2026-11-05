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

from collections import Counter

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


def pytest_sessionstart() -> None:
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
