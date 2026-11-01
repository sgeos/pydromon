"""Bounded gate reporting of tests omitted from execution."""

from collections import Counter

import pytest
from _pytest.terminal import TerminalReporter

_MAX_GROUPS = 20
_MAX_EXAMPLES = 3
_groups: dict[str, list[str]] = {}
_counts: Counter[str] = Counter()


def pytest_sessionstart() -> None:
    _groups.clear()
    _counts.clear()


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


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.skipped:
        reason = report.longrepr
        text = reason[2] if isinstance(reason, tuple) else str(reason)
        _record(text, report.nodeid)


def pytest_collectreport(report: pytest.CollectReport) -> None:
    if report.skipped:
        _record(str(report.longrepr), report.nodeid)


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
