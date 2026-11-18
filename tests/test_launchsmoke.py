"""The launch smoke check of ``dromon.launchsmoke``. **Round 18, item 1.**

Every test that starts launchers runs the module's own ``check_launches``, which
conftest otherwise replaces for the whole suite, so that no other test pays for twelve
launches. ``REAL`` is taken at import, before that replacement.

Where ``ps`` cannot run, as in an agent sandbox, the sweep is given a model of the
process table that is built from the launch records and passed through the real
``parse_tree``, so what is verified is the launcher's recognition and the sweep's
choices, and not ``ps``. The ``full-gate`` run reads the real table.
"""

import asyncio
import os
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from dromon import launcher, launchsmoke, spawn
from dromon.driver import ENGINE_OF
from dromon.launcher import LaunchRecord
from dromon.modulecommand import module_command
from dromon.processes import (
    MAX_COMMAND_CHARACTERS,
    TreeReadFailure,
    TreeRow,
    parse_tree,
)

REAL = launchsmoke.check_launches


def ps_runs() -> bool:
    try:
        found = spawn.run(
            ["ps", "-o", "pid=", "-p", str(os.getpid())],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return False
    return found.returncode == 0


def modelled(records: Sequence[LaunchRecord]) -> list[TreeRow]:
    """The table as ``ps`` would print it for ``records``, through ``parse_tree``. A
    launcher leads its session and runs the module command, and its harness is its
    child and leads a group of its own, running the script by its path, which is how
    the launcher starts one. The text is whole and the parser shortens it, as for the
    real table."""
    lines: list[str] = []
    for record in records:
        launch = " ".join(module_command("dromon.launcher", "--stand-in"))
        lines.append(f"{record.launcher} {os.getpid()} {record.launcher} {launch}")
        lines.append(
            f"{record.harness} {record.launcher} {record.harness}"
            f" /bin/sh {record.command}"
        )
    return parse_tree("\n".join(lines))


def table(
    records: Sequence[LaunchRecord],
) -> list[TreeRow] | TreeReadFailure | str:
    if ps_runs():
        return launcher.read_rows()
    return modelled(records)


def test_every_engine_kind_of_the_drivers_is_a_kind_of_the_check() -> None:
    assert set(launchsmoke.KINDS) == set(ENGINE_OF.values())
    assert set(launchsmoke.COMMAND_OF) == set(launchsmoke.KINDS)
    assert len(set(launchsmoke.COMMAND_OF.values())) == len(launchsmoke.KINDS)


def test_a_stand_in_of_every_kind_under_both_policies_survives_the_sweeps() -> None:
    seen: list[list[LaunchRecord]] = []

    def read(records: Sequence[LaunchRecord]) -> list[TreeRow] | TreeReadFailure | str:
        seen.append(list(records))
        return table(records)

    report = asyncio.run(REAL(read=read))
    assert report.unverified is None, report.lines()
    assert report.passed, report.lines()
    assert len(report.results) == len(launchsmoke.KINDS) * len(launchsmoke.POLICIES)
    assert {(r.kind, r.policy) for r in report.results} == {
        (kind, policy) for kind in launchsmoke.KINDS for policy in launchsmoke.POLICIES
    }
    assert report.lines() == [
        f"launch smoke: {len(report.results)} of {len(report.results)} launches"
        f" survived {launchsmoke.SWEEPS} sweeps"
    ]
    assert len(seen[0]) == len(report.results)


def test_the_launchers_and_their_stand_ins_are_gone_when_the_check_returns() -> None:
    records: list[LaunchRecord] = []

    def read(found: Sequence[LaunchRecord]) -> list[TreeRow] | TreeReadFailure | str:
        records[:] = found
        return table(found)

    asyncio.run(REAL(kinds=(launchsmoke.KINDS[1],), read=read))
    assert records
    deadline = time.monotonic() + 10
    pids = [pid for record in records for pid in (record.launcher, record.harness)]
    while time.monotonic() < deadline and any(alive(pid) for pid in pids):
        time.sleep(0.1)
    assert not [pid for pid in pids if alive(pid)]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A child that has exited and has not been reaped still answers.
    try:
        reaped, _ = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return True
    return reaped == 0


def test_the_check_restores_path_and_the_launchers_report_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}/usr/bin")
    launcher.configure(tmp_path)
    try:
        asyncio.run(REAL(kinds=(launchsmoke.KINDS[2],), read=table))
        assert os.environ["PATH"] == f"{tmp_path}{os.pathsep}/usr/bin"
        assert launcher.report_directory() == tmp_path
    finally:
        launcher.configure(None)


def test_the_check_fails_where_the_sweep_takes_a_live_launcher_for_an_exited_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pull request 157's defect, put back. Markers are again looked for in the text
    that ``parse_tree`` shortened, which the isolated module command's bootstrap
    pushes the launcher's name out of. The check must fail, and must name the cause."""
    launch = " ".join(module_command("dromon.launcher", "--stand-in"))
    offset = launch.index("dromon.launcher")
    if offset < MAX_COMMAND_CHARACTERS:
        pytest.skip(f"the marker is at {offset}, inside the {MAX_COMMAND_CHARACTERS}")

    def truncated(self: TreeRow, marker: str) -> bool:
        return marker in self.command

    monkeypatch.setattr(TreeRow, "has_marker", truncated)
    report = asyncio.run(REAL(kinds=(launchsmoke.KINDS[2],), read=table))
    assert not report.passed
    assert report.unverified is None
    assert len(report.failed) == len(launchsmoke.POLICIES)
    for result in report.failed:
        assert "launch record" in result.detail or "launcher_running" in result.detail
    assert any("FAILED" in line for line in report.lines())


def test_an_unreadable_process_table_is_unverified_and_not_a_pass() -> None:
    def read(records: Sequence[LaunchRecord]) -> TreeReadFailure:
        del records
        return TreeReadFailure("ps cannot be run: not permitted")

    report = asyncio.run(REAL(kinds=(launchsmoke.KINDS[2],), read=read))
    assert report.unverified == "ps cannot be run: not permitted"
    assert not report.passed
    assert report.results == ()
    [line] = report.lines()
    assert line.startswith("launch smoke: UNVERIFIED")
    assert "not permitted" in line


def test_a_start_that_fails_is_a_failure_and_does_not_wait_for_the_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing() -> str:
        raise FileNotFoundError("no launcher here")

    monkeypatch.setattr(launcher, "script_path", missing)
    began = time.monotonic()
    report = asyncio.run(REAL(kinds=(launchsmoke.KINDS[0],), read=table))
    assert time.monotonic() - began < launchsmoke.RECORD_SECONDS / 2
    assert not report.passed
    assert any("no launcher here" in result.detail for result in report.failed)


def test_a_launch_with_no_record_is_a_failure_that_says_so() -> None:
    results = launchsmoke.judge(
        [], [], {launchsmoke.KINDS[0]: Path("/x/claude")}, [], launchsmoke.KINDS[:1]
    )
    assert len(results) == len(launchsmoke.POLICIES)
    assert all(not r.survived for r in results)
    assert all("0 launch records" in r.detail for r in results)


def test_an_empty_report_is_not_a_pass() -> None:
    assert not launchsmoke.SmokeReport(()).passed
    assert launchsmoke.SmokeReport(()).lines()[0].startswith("launch smoke: 0 of 0")


def test_the_main_exit_status_tells_a_pass_a_failure_and_an_unverified_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    good = launchsmoke.SmokeResult(
        launchsmoke.KINDS[0], launcher.StopPolicy.KILL_ALL, True, "survived"
    )
    bad = launchsmoke.SmokeResult(
        launchsmoke.KINDS[0], launcher.StopPolicy.KILL_ALL, False, "gone"
    )
    for report, status in (
        (launchsmoke.SmokeReport((good,)), 0),
        (launchsmoke.SmokeReport((good, bad)), 1),
        (launchsmoke.SmokeReport((), "no ps"), 2),
    ):

        async def fake(
            report: launchsmoke.SmokeReport = report,
        ) -> launchsmoke.SmokeReport:
            return report

        monkeypatch.setattr(launchsmoke, "check_launches", fake)
        assert launchsmoke.main() == status
    assert "UNVERIFIED" in capsys.readouterr().out
