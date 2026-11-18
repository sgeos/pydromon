"""The operator's override of the machine status, a receipt that says what it tested,
and one deferral that every surface words alike. **Round 18, items 1, 2 and 3.**
``docs/architecture/RUNS.md`` is normative."""

import asyncio
import sys
from collections.abc import Generator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from dromon import readiness, runs
from dromon import scheduler as scheduler_module
from dromon.eventlog import EventLog
from dromon.model import (
    MAX_ADMIT_SECONDS,
    MIN_ADMIT_SECONDS,
    EventRejected,
    InvalidField,
    OperatorAddress,
    RunAdmitted,
    RunEnded,
    RunStarted,
    RunStatus,
    parse_due,
)
from dromon.scheduler import Hold, RunDeferral
from test_queued_runs import AT, Rig
from test_run_tree import (
    AT as TREE_AT,
)
from test_run_tree import (
    Runner,
    repository,
)

ANA = OperatorAddress("ana")


async def run_again(runner: Runner) -> RunStarted:
    """Start ``gate`` after the line's latest run and give the new run's record. The
    helper of ``test_run_tree`` names the run with the greatest identifier, which is
    not the newest, since identifiers are random."""
    latest = runner.log.state.latest_runs.get(("line", "alpha"), "none")
    await runner.call(
        scheduler_module.START_RUN_TOOL_NAME, {"command": "gate", "previous": latest}
    )
    run_id = runner.log.state.latest_runs[("line", "alpha")]
    child = runner.scheduler._run_children.get(run_id)  # pyright: ignore[reportPrivateUsage]
    if child is not None:
        child.wait(timeout=30)
    await runner.scheduler.reconcile_runs()
    return runner.log.state.runs[run_id].started


class OverrideRig(Rig):
    """The queued-run rig, with a second declared command that stays open until it is
    stopped, so a second line's start meets a run that is open."""

    def declare(self, scheduler: scheduler_module.Scheduler) -> None:
        super().declare(scheduler)
        scheduler.set_runs(
            {
                "gate": (sys.executable, "-c", "print('gate passed')"),
                "hold": (sys.executable, "-c", "import time; time.sleep(60)"),
            },
            slot_lease="slot",
        )

    def stale(self) -> None:
        """A status whose last sample is far older than the monitor writes."""
        stamp = int(self.clock.now.timestamp()) - 10_000
        self.status.write_text(
            f"time=2026-11-16T01:00:00-0800\nepoch={stamp}\nlevel=ok\nreasons=none\n\n"
        )
        self.ticks.value += scheduler_module.MACHINE_READ_SECONDS + 1

    def start(self, line: str, command: str = "gate") -> RunStarted:
        latest = self.log.state.latest_runs.get(("line", line), "none")
        return asyncio.run(self.scheduler.start_run(line, command, latest))

    def stop_and_finish(self, run_id: str) -> None:
        asyncio.run(
            self.scheduler.stop_run(self.log.state.runs[run_id].started.line_id, run_id)
        )
        self.finish(run_id)


@pytest.fixture
def rig(tmp_path: Path) -> Generator[OverrideRig]:
    made = OverrideRig(tmp_path.resolve())
    yield made
    for run in list(made.log.state.runs.values()):
        child = made.scheduler._run_children.get(run.started.run_id)  # pyright: ignore[reportPrivateUsage]
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=20)
    made.log.close()


# Item 1, the override.


def test_no_override_is_made_at_critical_and_nothing_is_recorded(
    rig: OverrideRig,
) -> None:
    """Rig starts at critical, where nothing starts."""
    with pytest.raises(EventRejected, match="critical, and no override admits"):
        rig.scheduler.admit_runs(ANA, None, "needed")
    assert rig.log.state.run_admission is None


def test_an_override_records_the_operator_its_time_and_its_expiry(
    rig: OverrideRig,
) -> None:
    rig.set_level("elevated")
    admitted = rig.scheduler.admit_runs(ANA, 600, "  memory pressure from outside  ")
    assert rig.log.state.run_admission == admitted
    assert (
        admitted.operator_id == "ana"
        and admitted.reason == "memory pressure from outside"
    )
    assert admitted.admitted_at == AT
    assert parse_due(admitted.expires_at) - parse_due(
        admitted.admitted_at
    ) == timedelta(seconds=600)
    default = rig.scheduler.admit_runs(ANA, None, "")
    assert parse_due(default.expires_at) - parse_due(default.admitted_at) == timedelta(
        seconds=3600
    )


def test_an_elevated_status_defers_a_run_until_the_override_admits_it(
    rig: OverrideRig,
) -> None:
    """Confirmed before the change, which had no override, so the start at elevated
    was refused for as long as the status stayed there."""
    rig.set_level("elevated")
    with pytest.raises(runs.RunRefused, match="deferred"):
        rig.start("alpha")
    admitted = rig.scheduler.admit_runs(ANA, 600, "")
    started = rig.start("alpha")
    assert started.admitted_by == "ana"
    line = rig.scheduler.run_status("alpha")
    assert "under the override of operator ana" in line
    assert (
        "Operator override: ana admitted runs, one at a time, at"
        f" {admitted.admitted_at}" in line
    )
    assert f"until {admitted.expires_at}" in line
    rig.finish(started.run_id)


def test_a_run_the_status_allowed_records_no_override(rig: OverrideRig) -> None:
    rig.set_level("elevated")
    rig.scheduler.admit_runs(ANA, 600, "")
    rig.set_level("ok")
    started = rig.start("alpha")
    assert started.admitted_by is None
    rig.finish(started.run_id)


def test_the_override_admits_one_run_at_a_time(rig: OverrideRig) -> None:
    rig.set_level("elevated")
    rig.scheduler.admit_runs(ANA, 600, "")
    first = rig.start("alpha", "hold")
    with pytest.raises(runs.RunRefused) as refused:
        rig.start("beta")
    text = str(refused.value)
    assert f"run {first.run_id} of line alpha is open" in text
    assert "admits one run at a time" in text
    queued = rig.queue("beta")
    assert queued.line_id == "beta" and first.run_id in queued.reason
    rig.stop_and_finish(first.run_id)
    rig.advance()
    second = next(
        r.started for r in rig.log.state.runs.values() if r.started.line_id == "beta"
    )
    assert second.admitted_by == "ana"
    rig.finish(second.run_id)


def test_two_starts_at_the_same_moment_cannot_both_pass_the_override(
    rig: OverrideRig,
) -> None:
    """Both starts reach the check before either has a record, since each awaits the
    tree and the supervisor. The second sees the first."""
    rig.set_level("elevated")
    rig.scheduler.admit_runs(ANA, 600, "")

    async def both() -> list[object]:
        return list(
            await asyncio.gather(
                rig.scheduler.start_run("alpha", "hold", "none"),
                rig.scheduler.start_run("beta", "hold", "none"),
                return_exceptions=True,
            )
        )

    results = asyncio.run(both())
    started = [r for r in results if isinstance(r, RunStarted)]
    refused = [r for r in results if isinstance(r, runs.RunRefused)]
    assert len(started) == 1 and len(refused) == 1, results
    assert "admits one run at a time" in str(refused[0])
    rig.stop_and_finish(started[0].run_id)


def test_the_override_ends_by_itself_at_its_expiry(rig: OverrideRig) -> None:
    rig.set_level("elevated")
    rig.scheduler.admit_runs(ANA, MIN_ADMIT_SECONDS, "")
    rig.clock.now += timedelta(seconds=MIN_ADMIT_SECONDS + 1)
    rig.set_level("elevated")
    with pytest.raises(runs.RunRefused, match="deferred"):
        rig.start("alpha")
    view = rig.scheduler.admission_view()
    assert view is not None and view["expired"] is True and view["active"] is False
    assert "It has expired." in rig.scheduler.run_status("alpha")


def test_the_operator_ends_the_override_and_a_second_end_is_refused(
    rig: OverrideRig,
) -> None:
    rig.set_level("elevated")
    rig.scheduler.admit_runs(ANA, 600, "")
    ended = rig.scheduler.end_admission(ANA)
    assert ended.operator_id == "ana" and rig.log.state.run_admission is None
    with pytest.raises(runs.RunRefused, match="deferred"):
        rig.start("alpha")
    with pytest.raises(EventRejected, match="no override"):
        rig.scheduler.end_admission(ANA)


def test_a_critical_status_is_never_admitted_even_under_an_override(
    rig: OverrideRig,
) -> None:
    rig.set_level("elevated")
    rig.scheduler.admit_runs(ANA, 600, "")
    rig.set_level("critical")
    with pytest.raises(runs.RunRefused, match="cannot admit a run at critical"):
        rig.start("alpha")
    view = rig.scheduler.admission_view()
    assert view is not None and view["critical"] is True and view["active"] is False
    assert "does not apply while the status is critical" in rig.scheduler.run_status(
        "alpha"
    )
    # It applies again once the status falls to elevated, within its expiry.
    rig.set_level("elevated")
    started = rig.start("alpha")
    assert started.admitted_by == "ana"
    rig.finish(started.run_id)


def test_a_status_that_cannot_be_relied_on_counts_as_elevated_and_is_admitted(
    rig: OverrideRig,
) -> None:
    rig.stale()
    with pytest.raises(runs.RunRefused, match="deferred"):
        rig.start("alpha")
    rig.scheduler.admit_runs(ANA, 600, "the monitor is down")
    started = rig.start("alpha")
    assert started.admitted_by == "ana"
    rig.finish(started.run_id)


@pytest.mark.parametrize(
    "seconds", [MIN_ADMIT_SECONDS - 1, MAX_ADMIT_SECONDS + 1, 0, -5]
)
def test_a_duration_outside_the_bounds_is_refused(
    rig: OverrideRig, seconds: int
) -> None:
    rig.set_level("elevated")
    with pytest.raises(EventRejected, match="an override lasts"):
        rig.scheduler.admit_runs(ANA, seconds, "")
    assert rig.log.state.run_admission is None


def test_a_boolean_is_not_a_duration(rig: OverrideRig) -> None:
    rig.set_level("elevated")
    with pytest.raises(EventRejected, match="an override lasts"):
        rig.scheduler.admit_runs(ANA, True, "")  # noqa: FBT003


def test_a_reason_over_the_limit_is_refused(rig: OverrideRig) -> None:
    rig.set_level("elevated")
    with pytest.raises(EventRejected, match="over 512 bytes"):
        rig.scheduler.admit_runs(ANA, 600, "x" * 513)


def test_a_daemon_that_reads_no_status_has_nothing_to_override(
    rig: OverrideRig,
) -> None:
    rig.scheduler.set_machine_status(None)
    with pytest.raises(EventRejected, match="reads no machine status"):
        rig.scheduler.admit_runs(ANA, 600, "")


def test_the_override_is_in_the_log_and_survives_a_restart(rig: OverrideRig) -> None:
    rig.set_level("elevated")
    admitted = rig.scheduler.admit_runs(ANA, 600, "kept")
    rig.log.close()
    reopened = EventLog(rig.root / "events.sqlite")
    try:
        assert reopened.state.run_admission == admitted
        again = rig.make(reopened)
        rig.declare(again)
        assert again.admission_view() is not None
        started = asyncio.run(again.start_run("alpha", "gate", "none"))
        assert started.admitted_by == "ana"
        child = again._run_children.get(started.run_id)  # pyright: ignore[reportPrivateUsage]
        if child is not None:
            child.wait(timeout=20)
    finally:
        reopened.close()


def test_a_run_admitted_has_a_valid_span() -> None:
    with pytest.raises(InvalidField, match="an override lasts"):
        RunAdmitted("ana", AT, AT, "")
    with pytest.raises(InvalidField, match="an override lasts"):
        RunAdmitted("ana", AT, "2026-11-17T09:00:00Z", "")


def test_the_override_reason_is_scrubbed_as_other_free_text_is(
    rig: OverrideRig,
) -> None:
    rig.set_level("elevated")
    token = "ghp_" + "a" * 36
    rig.scheduler.admit_runs(ANA, 600, f"the token is {token}")
    assert token not in str(rig.log.state.run_admission)
    raw = (rig.root / "events.sqlite").read_bytes()
    assert token.encode() not in raw


# Item 3, one deferral.


def surfaces(rig: OverrideRig) -> tuple[RunDeferral, str, str, str]:
    """The deferral and the three places that word it, a refused start, a queued
    run's reason and ``run_status``."""
    deferral = rig.scheduler._run_deferral("alpha")  # pyright: ignore[reportPrivateUsage]
    assert deferral is not None
    with pytest.raises(runs.RunRefused) as refused:
        rig.start("alpha")
    queued = rig.queue("alpha")
    return (
        deferral,
        str(refused.value),
        queued.reason,
        rig.scheduler.run_status("alpha"),
    )


def test_the_machine_hold_is_worded_once_on_every_surface(rig: OverrideRig) -> None:
    """Confirmed before the change, which gave the start, the queue and ``run_status``
    each their own wording and ``run_status`` none of the cause."""
    deferral, refusal, reason, status = surfaces(rig)
    assert deferral.hold is Hold.MACHINE
    assert deferral.reason in refusal and deferral.resumes in refusal
    assert deferral.text() in reason
    assert deferral.reason in status and deferral.resumes in status
    assert "A start now would be deferred" in status
    assert (
        "critical" in deferral.resumes
        and "cannot admit a run at critical" in deferral.resumes
    )


def test_the_slot_hold_is_worded_once_on_every_surface(rig: OverrideRig) -> None:
    rig.set_level("ok")
    rig.scheduler.acquire_lease("beta", "slot", "the gate")
    deferral, refusal, reason, status = surfaces(rig)
    assert deferral.hold is Hold.SLOT
    assert deferral.reason == "the slot, lease slot, is held by line beta"
    held = rig.log.state.leases["slot"]
    assert held.expires in deferral.resumes and "releases it" in deferral.resumes
    assert deferral.reason in refusal and deferral.resumes in refusal
    assert deferral.text() in reason
    assert deferral.reason in status and deferral.resumes in status


def test_the_override_hold_is_worded_once_on_every_surface(rig: OverrideRig) -> None:
    rig.set_level("elevated")
    rig.scheduler.admit_runs(ANA, 600, "")
    first = rig.start("beta", "hold")
    deferral, refusal, reason, status = surfaces(rig)
    assert deferral.hold is Hold.ADMISSION
    assert first.run_id in deferral.reason
    assert deferral.reason in refusal and deferral.resumes in refusal
    assert deferral.text() in reason
    assert deferral.reason in status and deferral.resumes in status
    rig.stop_and_finish(first.run_id)


def test_a_monitor_that_stopped_is_worded_once_with_how_it_is_restored(
    rig: OverrideRig,
) -> None:
    rig.stale()
    deferral, refusal, _, status = surfaces(rig)
    assert deferral.hold is Hold.MACHINE
    for text in (refusal, status):
        assert "monitor looks stopped" in text
        assert "dromon run admit" in text
    assert deferral.resumes in refusal and deferral.resumes in status


def test_nothing_held_back_is_said_so_and_the_turn_notice_agrees(
    rig: OverrideRig,
) -> None:
    rig.set_level("ok")
    assert "Nothing holds a start back now." in rig.scheduler.run_status("alpha")
    assert rig.scheduler._run_deferral("alpha") is None  # pyright: ignore[reportPrivateUsage]
    rig.set_level("elevated")
    notice = rig.scheduler._machine_notice("alpha")  # pyright: ignore[reportPrivateUsage]
    deferral = rig.scheduler._run_deferral("alpha")  # pyright: ignore[reportPrivateUsage]
    assert notice is not None and deferral is not None
    assert deferral.resumes in notice and "start nothing heavy" in notice


def test_the_operator_is_told_once_with_the_same_words(rig: OverrideRig) -> None:
    rig.set_level("elevated")
    with pytest.raises(runs.RunRefused):
        rig.start("alpha")
    with pytest.raises(runs.RunRefused):
        rig.start("beta")
    deferral = rig.scheduler._run_deferral("alpha")  # pyright: ignore[reportPrivateUsage]
    assert deferral is not None
    told = [
        m.body for m in rig.log.state.messages.values() if "deferred a run" in m.body
    ]
    assert len(told) == 1 and deferral.reason in told[0] and deferral.resumes in told[0]


def test_the_status_view_and_the_run_status_agree_on_the_hold(rig: OverrideRig) -> None:
    rig.set_level("elevated")
    view = rig.scheduler.machine_status_view()
    assert view["defers_runs"] is True
    deferral = rig.scheduler._run_deferral("alpha")  # pyright: ignore[reportPrivateUsage]
    assert deferral is not None
    assert "elevated" in deferral.reason


# Item 2, what a receipt says it tested.

FINAL = b"5162 passed, 10 skipped, 48 deselected in 231.5s\n"


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (FINAL, (("passed", 5162), ("skipped", 10), ("deselected", 48))),
        (
            b"=== 1 failed, 3 passed, 1 error, 2 warnings in 1.2s ===\n",
            (("failed", 1), ("passed", 3), ("errors", 1), ("warnings", 2)),
        ),
        (
            b"SKIPPED [1] tests/x.py:3: why\n=== short test summary info ===\n"
            b"SKIPPED [1] tests/x.py:3: why\n7 passed, 1 skipped in 2s\n",
            (("passed", 7), ("skipped", 1)),
        ),
        (b"1 xfailed, 2 xpassed in 0.1s\n", (("xfailed", 1), ("xpassed", 2))),
    ],
)
def test_the_counts_are_read_from_the_final_line(
    output: bytes, expected: tuple[tuple[str, int], ...]
) -> None:
    assert runs.gate_counts(output) == expected


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"no summary here\n",
        b"=== short test summary info ===\nFAILED tests/x.py::t\n",
        f"{10**9 + 1} passed in 1s\n".encode(),
    ],
)
def test_no_counts_are_given_without_a_final_line_or_with_a_forged_number(
    output: bytes,
) -> None:
    assert runs.gate_counts(output) is None


def test_the_counts_and_the_quoted_summary_come_from_one_line() -> None:
    output = (
        b"1 passed in 1s\n=== short test summary info ===\n3 passed, 4 failed in 2s\n"
    )
    summary = runs.gate_summary(output)
    counts = runs.gate_counts(output)
    assert summary == "3 passed, 4 failed" and counts == (("passed", 3), ("failed", 4))


def test_a_run_end_holds_counts_that_are_names_and_numbers_only() -> None:
    ended = RunEnded(
        "r-1", RunStatus.EXITED, 0, None, None, "", AT, coverage=(("passed", 1),)
    )
    assert ended.coverage == (("passed", 1),)
    with pytest.raises(InvalidField, match="twice or not at all"):
        RunEnded(
            "r-1", RunStatus.EXITED, 0, None, None, "", AT, coverage=(("evil", 1),)
        )
    with pytest.raises(InvalidField, match="twice or not at all"):
        RunEnded(
            "r-1",
            RunStatus.EXITED,
            0,
            None,
            None,
            "",
            AT,
            coverage=(("passed", 1), ("passed", 2)),
        )
    with pytest.raises(InvalidField, match="not a count"):
        RunEnded(
            "r-1", RunStatus.EXITED, 0, None, None, "", AT, coverage=(("passed", -1),)
        )
    with pytest.raises(InvalidField, match="has a result"):
        RunEnded(
            "r-1", RunStatus.LOST, None, None, None, "", AT, coverage=(("passed", 1),)
        )
    with pytest.raises(InvalidField, match="has a result"):
        RunEnded("r-1", RunStatus.EXITED, 0, None, None, "", AT, coverage=())


def test_a_run_started_holds_a_dirty_count_or_nothing_and_no_commit_run_has_one() -> (
    None
):
    base: dict[str, Any] = {
        "run_id": "r-1",
        "line_id": "alpha",
        "role_id": None,
        "previous": None,
        "command": "gate",
        "argv": ("true",),
        "workdir": "/work",
        "output_file": "tmp/runs/r-1.out",
        "pid": 4321,
        "process_started": TREE_AT,
        "started_at": TREE_AT,
    }
    assert RunStarted(**base, dirty_paths=0).dirty_paths == 0  # pyright: ignore[reportArgumentType]
    with pytest.raises(InvalidField, match="dirty_paths must be null"):
        RunStarted(**base, dirty_paths=-1)  # pyright: ignore[reportArgumentType]
    with pytest.raises(InvalidField, match="dirty_paths must be null"):
        RunStarted(**base, dirty_paths=True)  # pyright: ignore[reportArgumentType]
    with pytest.raises(InvalidField, match="dirty_paths must be null"):
        RunStarted(  # pyright: ignore[reportArgumentType]
            **base, commit="a" * 40, checkout="/work/c", dirty_paths=0
        )


@pytest.mark.real_git_status
def test_a_receipt_says_whether_the_tested_tree_was_clean_and_its_digest(
    tmp_path: Path,
) -> None:
    """Confirmed before the change, whose receipt said only that the run had any
    uncommitted changes, never whether it had any, and showed no digest."""
    project = repository(tmp_path / "project")
    with EventLog(project / "events.sqlite") as log:
        runner = Runner(project, log)

        async def scenario() -> None:
            clean = await run_again(runner)
            assert clean.dirty_paths == 0 and clean.tree is not None
            text = await runner.call(scheduler_module.RUN_STATUS_TOOL_NAME, {})
            assert "with a clean tree as it started" in text
            assert f"tree digest {clean.tree}" in text
            (project / "kept.txt").write_text("changed\n")
            (project / "notes.md").write_text("untracked\n")
            dirty = await run_again(runner)
            assert dirty.dirty_paths == 2 and dirty.tree != clean.tree
            text = await runner.call(scheduler_module.RUN_STATUS_TOOL_NAME, {})
            assert "with 2 changed or untracked paths as it started" in text
            assert "so it did not test the commit alone" in text
            assert f"tree digest {dirty.tree}" in text

        asyncio.run(scenario())


def test_a_tree_that_could_not_be_read_is_unknown_and_never_clean(
    tmp_path: Path,
) -> None:
    """The project is no checkout, so git is not started."""
    project = tmp_path / "project"
    project.mkdir()
    with EventLog(project / "events.sqlite") as log:
        runner = Runner(project, log)

        async def scenario() -> None:
            run = await run_again(runner)
            assert run.dirty_paths is None and run.tree is None
            text = await runner.call(scheduler_module.RUN_STATUS_TOOL_NAME, {})
            assert "with its uncommitted state unknown" in text
            assert "with a clean tree" not in text
            assert "with no tree digest recorded" in text

        asyncio.run(scenario())


@pytest.mark.real_git_status
def test_a_tree_whose_digest_cannot_be_told_still_counts_its_dirty_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A changed tracked path that cannot be stated leaves no digest, and the status
    that was read is still a fact, so the receipt says the tree was not clean."""
    project = repository(tmp_path / "project")
    (project / "kept.txt").write_text("changed\n")
    monkeypatch.setattr(readiness, "MAX_TREE_PATHS", 0)
    assert scheduler_module._tree_state(project) == (None, 1)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.real_git_status
def test_one_read_gives_the_digest_and_the_count_together(tmp_path: Path) -> None:
    worktree = repository(tmp_path / "w")
    clean = readiness.tree_snapshot(worktree)
    assert clean.dirty == 0 and clean.digest == readiness.tree_digest(worktree)
    (worktree / "notes.md").write_text("untracked\n")
    untracked = readiness.tree_snapshot(worktree)
    assert untracked.dirty == 1 and untracked.digest == clean.digest
    (worktree / "kept.txt").write_text("changed\n")
    changed = readiness.tree_snapshot(worktree)
    assert changed.dirty == 2 and changed.digest != clean.digest


def test_a_finished_run_shows_the_counts_its_output_gave(rig: OverrideRig) -> None:
    rig.set_level("ok")
    rig.scheduler.set_runs(
        {
            "gate": (
                sys.executable,
                "-c",
                "print('5162 passed, 10 skipped, 48 deselected in 231.5s')",
            )
        },
        slot_lease="slot",
    )
    started = rig.start("alpha")
    rig.finish(started.run_id)
    ended = rig.log.state.runs[started.run_id].ended
    assert ended is not None
    assert ended.coverage == (("passed", 5162), ("skipped", 10), ("deselected", 48))
    text = rig.scheduler.run_status("alpha")
    assert "Coverage counts of the output's own final line, unverified:" in text
    assert "5162 passed, 10 skipped, 48 deselected" in text
    view = next(
        v for v in rig.scheduler.runs_view(None) if v["run_id"] == started.run_id
    )
    assert view["coverage"] == {"passed": 5162, "skipped": 10, "deselected": 48}
    assert view["dirty_paths"] is None and view["admitted_by"] is None


def test_a_run_whose_output_has_no_final_line_has_no_counts(rig: OverrideRig) -> None:
    rig.set_level("ok")
    started = rig.start("alpha")
    rig.finish(started.run_id)
    ended = rig.log.state.runs[started.run_id].ended
    assert ended is not None and ended.coverage is None
    assert "Coverage counts" not in rig.scheduler.run_status("alpha")


# The operator's commands.


def test_the_override_is_an_operator_command_through_the_client(
    rig: OverrideRig,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from dromon import cli
    from dromon.configview import Overrides
    from dromon.daemon import Daemon
    from dromon.model import format_due
    from dromon.paths import project_paths
    from dromon.projectconfig import ProjectConfig
    from dromon.protocol import Operation, Response

    paths = project_paths(rig.root, short_runtime)
    paths.state_dir.mkdir(mode=0o700)
    daemon = Daemon(
        paths,
        rig.log,
        format_due(rig.clock.now),
        rig.scheduler,
        project=ProjectConfig(),
        command_line=Overrides(),
        project_file_present=False,
    )

    def call(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        del socket
        line = {"id": "r", "op": operation.value, "v": 1, "args": args or {}}
        return daemon.respond((json.dumps(line) + "\n").encode())

    monkeypatch.setattr(cli, "call", call)
    base = ("--project", str(rig.root), "--runtime-dir", str(short_runtime))
    rig.set_level("elevated")
    bads: list[dict[str, object]] = [
        {},
        {"operator": "ana", "seconds": "600"},
        {"operator": "ana", "x": 1},
    ]
    for bad in bads:
        assert call(paths.socket, Operation.RUN_ADMIT, bad).error_code == "bad_request"
    assert (
        call(
            paths.socket, Operation.RUN_ADMIT, {"operator": "ana", "seconds": True}
        ).error_code
        == "bad_request"
    )
    assert rig.log.state.run_admission is None
    assert cli.main([*base, "run", "admit", "--seconds", "600", "--reason", "x"]) == 0
    assert "admits runs, one at a time" in capsys.readouterr().out
    admitted = rig.log.state.run_admission
    assert admitted is not None and admitted.reason == "x"
    assert cli.main([*base, "run", "status"]) == 0
    shown = capsys.readouterr().out
    assert "override: " in shown and "and it admits runs now" in shown
    assert cli.main([*base, "run", "admit", "--seconds", "5"]) != 0
    assert cli.main([*base, "run", "admit-end"]) == 0
    assert rig.log.state.run_admission is None
    assert cli.main([*base, "run", "admit-end"]) != 0
    rig.set_level("critical")
    assert cli.main([*base, "run", "admit"]) != 0
    assert rig.log.state.run_admission is None


def test_the_run_view_of_the_operator_shows_the_receipt(rig: OverrideRig) -> None:
    from dromon import cli

    rig.set_level("elevated")
    rig.scheduler.admit_runs(ANA, 600, "")
    started = rig.start("alpha")
    rig.finish(started.run_id)
    (view,) = [
        v for v in rig.scheduler.runs_view(None) if v["run_id"] == started.run_id
    ]
    details = cli._run_details(view)  # pyright: ignore[reportPrivateUsage]
    assert "admitted by the override of operator ana" in details
    assert any(line.startswith("tested: worktree commit") for line in details)
    assert any(line.startswith("tree digest: ") for line in details)
