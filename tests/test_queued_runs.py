"""Queued runs. **Round 17, item 1.** A start of a declared run that waits only for the
machine status or the project's slot is kept, in a bounded queue, and begins by itself,
and the line is told when it begins or is dropped. ``docs/architecture/RUNS.md`` is
normative."""

import asyncio
import sys
from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import runs
from dromon import scheduler as scheduler_module
from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog
from dromon.model import (
    HARNESS,
    MAX_QUEUED_RUNS,
    EventRejected,
    HoldCause,
    InvalidField,
    LineState,
    LineStateChanged,
    QueueEnd,
    RunDequeued,
    RunQueued,
    RunStarted,
    State,
)
from dromon.projectconfig import ConfigFileError, ProjectConfig, parse as parse_project
from dromon.scheduler import LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
START = datetime(2026, 11, 16, 9, 0, 0, tzinfo=UTC)
COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
AT = "2026-11-16T09:00:00Z"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class Ticks:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class Recorder:
    def __init__(self, spec: LineSpec) -> None:
        self.spec = spec
        self.session_id = spec.session_id

    async def run_turn(self, prompt: str) -> TurnReport:
        del prompt
        return TurnReport("success", 1, "ok", (), None, None, "s", None, 0.1)


def status_text(clock: Clock, level: str) -> str:
    stamp = int(clock.now.timestamp()) - 5
    return (
        f"time=2026-11-16T01:00:00-0800\nepoch={stamp}\nlevel={level}\n"
        "reasons=swap use is 17000 MB\n\n"
    )


class Rig:
    def __init__(self, root: Path) -> None:
        self.root = root
        (root / "reports").mkdir()
        self.status = root / "machine-status"
        self.clock = Clock()
        self.ticks = Ticks()
        self.log = EventLog(root / "events.sqlite")
        self.scheduler = self.make(self.log)
        self.scheduler.ensure_operator("ana")
        for name in ("alpha", "beta"):
            self.scheduler.create_line(name, HAIKU, Strategy.SUBSCRIPTION)
        self.declare(self.scheduler)
        self.scheduler.set_machine_status(self.status)
        self.set_level("critical")

    def make(self, log: EventLog) -> Scheduler:
        return Scheduler(
            log,
            self.build,
            tick_seconds=0,
            wall_clock=self.clock,
            clock=self.ticks,
            default_operator="ana",
            root=self.root,
            report_dir=self.root / "reports",
            run_start_of=lambda pid: "2026-11-16T09:00:00Z" if pid else None,
            run_occupied=lambda session: False,
        )

    def declare(self, scheduler: Scheduler) -> None:
        scheduler.set_runs(
            {"gate": (sys.executable, "-c", "print('gate passed')")},
            slot_lease="slot",
        )
        scheduler.set_leases(["slot"], 3600)
        scheduler.set_machine_status(self.status)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
        del tools
        return Recorder(spec)

    def set_level(self, level: str) -> None:
        """Write the status, and let the daemon's cached read expire."""
        self.status.write_text(status_text(self.clock, level))
        self.ticks.value += scheduler_module.MACHINE_READ_SECONDS + 1

    def queue(self, line: str = "alpha") -> RunQueued:
        latest = self.log.state.latest_runs.get(("line", line), "none")
        queued = asyncio.run(self.scheduler.start_run(line, "gate", latest, queue=True))
        assert isinstance(queued, RunQueued)
        return queued

    def advance(self) -> None:
        asyncio.run(self.scheduler.advance_run_queue())

    def notes(self, line: str) -> list[str]:
        return [
            message.body
            for message in self.log.state.messages.values()
            if message.sender == HARNESS and str(message.recipient) == line
        ]

    def finish(self, started: str) -> None:
        child = self.scheduler._run_children.get(started)  # pyright: ignore[reportPrivateUsage]
        if child is not None:
            child.wait(timeout=20)
        asyncio.run(self.scheduler.reconcile_runs())


@pytest.fixture
def rig(tmp_path: Path) -> Generator[Rig]:
    made = Rig(tmp_path.resolve())
    yield made
    made.log.close()


# Keeping the start.


def test_a_deferred_start_is_kept_by_the_queue_and_refused_without_it(
    rig: Rig,
) -> None:
    """Confirmed against the code before the change, which refused every deferred
    start and left the line to retry."""
    with pytest.raises(runs.RunRefused, match="deferred"):
        asyncio.run(rig.scheduler.start_run("alpha", "gate", "none"))
    queued = rig.queue()
    assert rig.log.state.queued_runs == {queued.queue_id: queued}
    assert not rig.log.state.runs
    assert "the machine status defers runs" in queued.reason
    assert (queued.command, queued.previous, queued.commit) == ("gate", None, None)


def test_the_position_and_the_reason_are_in_run_status(rig: Rig) -> None:
    first = rig.queue("alpha")
    second = rig.queue("beta")
    text = rig.scheduler.run_status("alpha")
    assert f"queued run {first.queue_id} of gate, position 1 of 2" in text
    assert "it waits, since the machine status defers runs" in text
    other = rig.scheduler.run_status("beta")
    assert f"queued run {second.queue_id} of gate, position 2 of 2" in other


def test_a_line_with_a_queued_run_starts_no_other_and_the_queue_is_bounded(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued = rig.queue()
    with pytest.raises(runs.RunRefused, match="a run of yours is queued"):
        asyncio.run(rig.scheduler.start_run("alpha", "gate", "none", queue=True))
    assert list(rig.log.state.queued_runs) == [queued.queue_id]
    assert MAX_QUEUED_RUNS == 8
    for number in range(MAX_QUEUED_RUNS - 1):
        name = f"line{number}"
        rig.scheduler.create_line(name, HAIKU, Strategy.SUBSCRIPTION)
        asyncio.run(rig.scheduler.start_run(name, "gate", "none", queue=True))
    assert len(rig.log.state.queued_runs) == MAX_QUEUED_RUNS
    rig.scheduler.create_line("extra", HAIKU, Strategy.SUBSCRIPTION)
    with pytest.raises(runs.RunRefused, match=f"holds {MAX_QUEUED_RUNS}"):
        asyncio.run(rig.scheduler.start_run("extra", "gate", "none", queue=True))
    assert len(rig.log.state.queued_runs) == MAX_QUEUED_RUNS


def test_a_refusal_that_is_not_a_wait_stands_with_the_queue(rig: Rig) -> None:
    with pytest.raises(runs.RunRefused, match="declares no run command"):
        asyncio.run(rig.scheduler.start_run("alpha", "nothing", "none", queue=True))
    with pytest.raises(runs.RunRefused, match="previous must name"):
        asyncio.run(rig.scheduler.start_run("alpha", "gate", "r-nope", queue=True))
    assert not rig.log.state.queued_runs


def test_with_nothing_holding_it_back_the_queue_option_starts_at_once(
    rig: Rig,
) -> None:
    rig.set_level("ok")
    started = asyncio.run(rig.scheduler.start_run("alpha", "gate", "none", queue=True))
    assert isinstance(started, RunStarted)
    assert not rig.log.state.queued_runs
    rig.finish(started.run_id)


# Beginning it.


def test_a_queued_run_starts_by_itself_and_the_line_is_told(rig: Rig) -> None:
    queued = rig.queue()
    rig.advance()
    assert rig.log.state.queued_runs, "the machine still defers runs"
    assert not rig.log.state.runs
    rig.set_level("ok")
    rig.advance()
    (run,) = rig.log.state.runs.values()
    assert run.started.line_id == "alpha" and run.started.command == "gate"
    assert not rig.log.state.queued_runs
    (note,) = rig.notes("alpha")
    assert (
        f"queued run {queued.queue_id} of gate started as run {run.started.run_id}"
        in note
    )
    rig.finish(run.started.run_id)
    assert any("exited with exit code 0" in text for text in rig.notes("alpha"))


def test_queued_runs_start_in_order_one_at_a_look(rig: Rig) -> None:
    first = rig.queue("alpha")
    rig.queue("beta")
    rig.set_level("ok")
    rig.advance()
    (run,) = rig.log.state.runs.values()
    assert run.started.line_id == "alpha"
    assert list(rig.log.state.queued_runs) != [first.queue_id]
    assert [q.line_id for q in rig.log.state.queued_runs.values()] == ["beta"]
    rig.finish(run.started.run_id)
    rig.advance()
    assert [r.started.line_id for r in rig.log.state.runs.values()] == ["alpha", "beta"]
    for run in rig.log.state.runs.values():
        rig.finish(run.started.run_id)


def test_a_queued_run_survives_a_restart(rig: Rig) -> None:
    queued = rig.queue()
    rig.log.close()
    reopened = EventLog(rig.root / "events.sqlite")
    try:
        again = rig.make(reopened)
        rig.declare(again)
        assert reopened.state.queued_runs == {queued.queue_id: queued}
        rig.set_level("ok")
        asyncio.run(again.advance_run_queue())
        (run,) = reopened.state.runs.values()
        assert run.started.line_id == "alpha"
        child = again._run_children.get(run.started.run_id)  # pyright: ignore[reportPrivateUsage]
        if child is not None:
            child.wait(timeout=20)
    finally:
        reopened.close()


def test_the_loop_begins_a_queued_run_without_being_asked(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "RUN_QUEUE_POLL_SECONDS", 0.05)
    rig.queue()
    rig.set_level("ok")

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(rig.scheduler.run(stop))
        for _ in range(200):
            await asyncio.sleep(0.05)
            if rig.log.state.runs:
                break
        stop.set()
        await loop

    asyncio.run(go())
    (run,) = rig.log.state.runs.values()
    assert not rig.log.state.queued_runs
    rig.finish(run.started.run_id)


# The slot.


def test_a_run_waits_while_another_line_holds_the_slot(rig: Rig) -> None:
    rig.set_level("ok")
    rig.scheduler.acquire_lease("beta", "slot", "the gate")
    with pytest.raises(
        runs.RunRefused, match="the slot, lease slot, is held by line beta"
    ):
        asyncio.run(rig.scheduler.start_run("alpha", "gate", "none"))
    queued = rig.queue()
    assert "the slot, lease slot, is held by line beta" in queued.reason
    rig.advance()
    assert not rig.log.state.runs
    rig.scheduler.release_lease("beta", "slot")
    rig.advance()
    (run,) = rig.log.state.runs.values()
    assert run.started.line_id == "alpha"
    rig.finish(run.started.run_id)


def test_the_holder_of_the_slot_is_not_held_back_by_it(rig: Rig) -> None:
    rig.set_level("ok")
    rig.scheduler.acquire_lease("alpha", "slot", "the gate")
    started = asyncio.run(rig.scheduler.start_run("alpha", "gate", "none"))
    rig.finish(started.run_id)
    assert rig.log.state.runs


def test_an_expired_slot_holds_nothing_back(rig: Rig) -> None:
    rig.set_level("ok")
    rig.scheduler.acquire_lease("beta", "slot", "the gate", 60)
    queued = rig.queue()
    rig.clock.now += timedelta(seconds=120)
    rig.advance()
    assert queued.queue_id not in rig.log.state.queued_runs
    (run,) = rig.log.state.runs.values()
    rig.finish(run.started.run_id)


# Dropping it.


def dropped(rig: Rig, queue_id: str) -> RunDequeued:
    # The log is the record, so the event is read from it.
    from dromon.eventlog import read_entries

    found = [
        entry.event
        for entry in read_entries(rig.root / "events.sqlite")
        if isinstance(entry.event, RunDequeued) and entry.event.queue_id == queue_id
    ]
    (event,) = found
    assert event.outcome is QueueEnd.DROPPED
    return event


def test_a_release_drops_the_lines_queued_run_with_an_event_and_a_note(
    rig: Rig,
) -> None:
    queued = rig.queue()
    rig.scheduler.hold("alpha", "passed a limit", cause=HoldCause.AUTONOMOUS_TURNS)
    assert rig.log.state.queued_runs, "a hold alone keeps the queued run"
    rig.scheduler.release("alpha")
    assert not rig.log.state.queued_runs
    assert dropped(rig, queued.queue_id).reason == "an operator released its line"
    (note,) = rig.notes("alpha")
    assert "was dropped and will not start, since an operator released its line" in note


@pytest.mark.parametrize("how", ["stop", "hibernate"])
def test_a_line_that_stops_or_hibernates_loses_its_queued_run(
    rig: Rig, how: str
) -> None:
    queued = rig.queue()
    if how == "stop":
        rig.log.append(LineStateChanged("alpha", LineState.STOPPED))
        expected = "its line was stopped"
    else:
        rig.scheduler.hibernate("alpha")
        expected = "its line was hibernated"
    rig.advance()
    assert dropped(rig, queued.queue_id).reason == expected
    assert not rig.log.state.queued_runs


def test_a_change_of_the_worktrees_commit_drops_the_queued_run(rig: Rig) -> None:
    git = rig.root / ".git"
    git.mkdir()
    (git / "HEAD").write_text(COMMIT + "\n")
    queued = rig.queue()
    assert queued.workdir_commit == COMMIT
    rig.advance()
    assert queued.queue_id in rig.log.state.queued_runs
    (git / "HEAD").write_text(OTHER_COMMIT + "\n")
    rig.advance()
    reason = dropped(rig, queued.queue_id).reason
    assert f"changed from {COMMIT} to {OTHER_COMMIT}" in reason
    assert not rig.log.state.queued_runs and not rig.log.state.runs


def test_edits_without_a_commit_do_not_drop_it(rig: Rig) -> None:
    queued = rig.queue()
    (rig.root / "changed.txt").write_text("edit made while queued\n")
    rig.advance()
    assert queued.queue_id in rig.log.state.queued_runs


def test_a_change_of_the_working_directory_drops_the_queued_run(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued = rig.queue()
    elsewhere = rig.root / "elsewhere"
    elsewhere.mkdir()

    def relocated(line_id: str) -> Path:
        del line_id
        return elsewhere

    monkeypatch.setattr(rig.scheduler, "_workdir_of", relocated)
    rig.advance()
    assert "its working directory changed from" in dropped(rig, queued.queue_id).reason


def test_a_start_that_the_run_refuses_is_dropped_with_its_reason(rig: Rig) -> None:
    queued = rig.queue()
    rig.set_level("ok")
    rig.scheduler.set_runs({"other": (sys.executable, "-c", "pass")})
    rig.advance()
    reason = dropped(rig, queued.queue_id).reason
    assert "its start was refused" in reason and "declares no run command" in reason
    (note,) = rig.notes("alpha")
    assert "its start was refused" in note


def test_the_line_withdraws_its_queued_run_with_stop_run(rig: Rig) -> None:
    queued = rig.queue()
    asyncio.run(rig.scheduler.stop_run("alpha", queued.queue_id))
    assert dropped(rig, queued.queue_id).reason == "you withdrew it"
    assert not rig.log.state.queued_runs
    assert rig.notes("alpha") == []
    queued = rig.queue()
    with pytest.raises(runs.RunRefused, match="not your scope's open run"):
        asyncio.run(rig.scheduler.stop_run("beta", queued.queue_id))
    assert queued.queue_id in rig.log.state.queued_runs


# The tool.


async def call(tool: SdkMcpTool[Any], args: dict[str, Any]) -> dict[str, Any]:
    return await tool.handler(args)


def test_the_start_run_tool_says_it_queued_and_where(rig: Rig) -> None:
    tool = next(t for t in rig.scheduler._run_tools("alpha") if t.name == "start_run")  # pyright: ignore[reportPrivateUsage]
    result = asyncio.run(call(tool, {"command": "gate", "previous": "none"}))
    text = result["content"][0]["text"]
    assert not result.get("is_error")
    assert text.startswith("queued, not started: queued run q-")
    assert "position 1 of 1" in text and "stop_run with q-" in text
    again = asyncio.run(call(tool, {"command": "gate", "previous": "none"}))
    assert (
        again.get("is_error")
        and "queued, so no other starts" in again["content"][0]["text"]
    )


# The records.


def test_the_events_refuse_what_they_cannot_hold() -> None:
    with pytest.raises(InvalidField, match="arguments are not sorted"):
        RunQueued(
            "q-1",
            "a",
            "gate",
            None,
            None,
            (("b", "1"), ("a", "2")),
            "/w",
            None,
            AT,
            "x",
        )
    with pytest.raises(InvalidField, match="given twice"):
        RunQueued(
            "q-1",
            "a",
            "gate",
            None,
            None,
            (("a", "1"), ("a", "2")),
            "/w",
            None,
            AT,
            "x",
        )
    with pytest.raises(InvalidField, match="full hexadecimal"):
        RunQueued("q-1", "a", "gate", None, "main", (), "/w", None, AT, "x")
    with pytest.raises(InvalidField, match="run_id is given exactly"):
        RunDequeued("q-1", QueueEnd.STARTED, None, "", AT)
    with pytest.raises(InvalidField, match="run_id is given exactly"):
        RunDequeued("q-1", QueueEnd.DROPPED, "r-1", "x", AT)


def test_the_state_refuses_a_second_entry_for_a_line_and_an_unknown_exit(
    rig: Rig,
) -> None:
    queued = rig.queue()
    state: State = rig.log.state
    again = RunQueued(
        "q-other", "alpha", "gate", None, None, (), queued.workdir, None, AT, "x"
    )
    with pytest.raises(EventRejected, match="has a queued run already"):
        state.check(again)
    with pytest.raises(EventRejected, match="no queued run"):
        state.check(RunDequeued("q-none", QueueEnd.DROPPED, None, "x", AT))
    with pytest.raises(EventRejected, match="no run r-none"):
        state.check(RunDequeued(queued.queue_id, QueueEnd.STARTED, "r-none", "", AT))
    with pytest.raises(EventRejected, match="is in use"):
        state.check(
            RunQueued(
                queued.queue_id, "beta", "gate", None, None, (), "/w", None, AT, "x"
            )
        )


# The project file.


def project(tmp_path: Path, body: str) -> ProjectConfig:
    return parse_project("version = 1\n" + body, tmp_path / "p.toml")


def test_the_project_file_names_the_slot_lease(tmp_path: Path) -> None:
    config = project(
        tmp_path, '[leases]\nnames = ["slot"]\n[runs]\nslot_lease = "slot"\n'
    )
    assert config.run_slot_lease == "slot"
    assert project(tmp_path, "").run_slot_lease is None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('[runs]\nslot_lease = "slot"\n', "which leases.names does not declare"),
        ('[leases]\nnames = ["a"]\n[runs]\nslot_lease = "slot"\n', "does not declare"),
        (
            '[leases]\nnames = ["slot"]\n[runs]\nslot_lease = 3\n',
            "must be a lease name",
        ),
        (
            '[leases]\nnames = ["slot"]\n[runs]\nslot_lease = "Bad Name"\n',
            "must be a lease name",
        ),
    ],
)
def test_the_project_file_refuses_a_slot_that_is_not_a_declared_lease(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(ConfigFileError, match=message):
        project(tmp_path, body)
