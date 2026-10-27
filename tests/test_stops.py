"""What the scheduler does when an operator stops a line, when a kept session reaches
its ceiling, and with what an engine left running, driven by scripted engines.

**Rows 1, 2, 5 and 12 of the T3 Code review, and the operator's decisions A and B.**
The tests named for a finding answer the review of the stop policy, F1 to F16.
"""

import asyncio
import json
import os
import signal
import time
from collections.abc import Callable, Coroutine, Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import launcher, redact
from dromon import scheduler as scheduler_module
from dromon.activity import ActivityKind
from dromon.agents import AgentError, LineTokens
from dromon.ceiling import CHOICES, Choice, parse_choice
from dromon.driver import Strategy
from dromon.engine import HALT_REASON, INTERRUPTED_KIND, TurnReport
from dromon.launcher import StopPolicy
from dromon.lineconfig import LineConfig
from dromon.eventlog import EventLog
from dromon.model import (
    EventRejected,
    InvalidField,
    LineAddress,
    MessageKind,
    OperatorAddress,
    ProcessesLeft,
    QuestionStatus,
    StopUnverified,
    WakeScheduled,
)
from dromon.processes import MARK_VARIABLE, UNIDENTIFIABLE, ProcessInfo, TreeRow
from dromon.scheduler import (
    COMPLETED,
    FAILED,
    LAUNCHER_LOST_NOTE,
    STOPPED_REASON,
    SWEPT_CAUSE,
    UNFINISHED_KILL_NOTE,
    UNFINISHED_LEAVE_NOTE,
    WALK_FAILED_NOTE,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
DAEMON = 1000


class Engine:
    """A line engine whose turn waits for its gate, when it has one."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.prompts: list[str] = []
        self.gate: asyncio.Event | None = None
        self.subtype = "success"
        self.fail = False

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("the engine broke")
        self.session_id = "session-1"
        return TurnReport(
            self.subtype, 1, "ok", (), None, None, "session-1", None, None
        )


class Halting(Engine):
    def __init__(self, reason: str | None = HALT_REASON) -> None:
        super().__init__()
        self.reason = reason
        self.halted = 0

    async def halt(self) -> str | None:
        self.halted += 1
        return self.reason


class Deciding(Engine):
    def __init__(self) -> None:
        super().__init__()
        self.answers: list[tuple[str, str]] = []

    async def decide_ceiling(self, question_id: str, answer: str) -> Choice | None:
        self.answers.append((question_id, answer))
        return parse_choice(answer)


class Tokened(Engine):
    def __init__(self) -> None:
        super().__init__()
        self.line_token: str | None = "built"  # noqa: S105
        self.seen: list[str | None] = []

    async def run_turn(self, prompt: str) -> TurnReport:
        self.seen.append(self.line_token)
        return await super().run_turn(prompt)


class Table:
    """The process table, as the scheduler's process tools read it."""

    def __init__(self) -> None:
        self.rows: list[ProcessInfo] = []
        self.envs: dict[int, list[str]] = {}

    def own_processes(self) -> Sequence[ProcessInfo]:
        return self.rows

    def has_variable(self, pid: int, assignment: str) -> bool:
        return assignment in self.envs.get(pid, [])

    def environments(self) -> dict[int, list[str]]:
        return dict(self.envs)

    def send(self, pid: int, signal_number: int) -> None:
        del pid, signal_number


class Keeping(Tokened):
    """A token engine whose process outlives its turn, until ``running`` is false."""

    def __init__(self) -> None:
        super().__init__()
        self.running = True

    def process_running(self) -> bool:
        return self.running


class World:
    def __init__(
        self,
        path: Path,
        engine: Engine,
        *,
        tokens: LineTokens | None = None,
        reports: Path | None = None,
        table: Table | None = None,
        identifiable: bool = True,
        walk_check: Callable[[], str | None] | None = None,
        sweep_read: Callable[[], list[TreeRow] | str] = launcher.read_rows,
        create: bool = True,
    ) -> None:
        self.log = EventLog(path)
        self.engine = engine
        self.table = table or Table()
        self.scheduler = Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            clock=lambda: 0.0,
            line_tokens=tokens,
            report_dir=reports,
            processes=self.table,
            processes_identifiable=identifiable,
            walk_check=walk_check,
            sweep_read=sweep_read,
        )
        if create:
            self.scheduler.ensure_operator("ana")
            self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Engine:
        del spec, tools
        return self.engine


@pytest.fixture
def worlds(tmp_path: Path) -> Iterator[Callable[..., World]]:
    made: list[World] = []

    def make(engine: Engine, **options: Any) -> World:  # noqa: ANN401
        world = World(tmp_path / f"events-{len(made)}.sqlite", engine, **options)
        made.append(world)
        return world

    yield make
    for world in made:
        world.log.close()


async def running(world: World, act: Callable[[], Coroutine[Any, Any, None]]) -> None:
    stop = asyncio.Event()
    loop = asyncio.create_task(world.scheduler.run(stop))
    await asyncio.sleep(0)
    await act()
    await world.scheduler.idle()
    for _ in range(20):
        await asyncio.sleep(0)
    stop.set()
    await loop


def run(world: World, act: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(running(world, act))


def activity(world: World) -> list[tuple[ActivityKind, str]]:
    return [(e.kind, e.text) for e in world.scheduler.activity.after("alpha", 0)]


# Row 1, a stop stops.


def test_an_operators_stop_holds_the_line_and_withdraws_its_wake(
    worlds: Callable[..., World],
) -> None:
    engine = Halting()
    world = worlds(engine)
    world.log.append(
        WakeScheduled("alpha", "m-later", "2099-01-01T00:00:00Z", "look again")
    )
    engine.gate = asyncio.Event()

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "long work")
        while not engine.prompts:
            await asyncio.sleep(0)
        assert world.scheduler.interrupt("alpha") is True
        engine.gate = None
        world.scheduler.send(ANA, LineAddress("alpha"), "more mail")

    run(world, act)
    line = world.log.state.lines["alpha"]
    assert line.held_by is not None
    assert "alpha" not in world.log.state.scheduled
    assert "m-later" in world.log.state.withdrawn_wakes
    question = world.log.state.messages[line.held_by]
    assert STOPPED_REASON in question.body and question.kind is MessageKind.QUESTION
    # Held, so the mail sent after the stop waits.
    assert len(engine.prompts) == 1
    assert engine.halted == 1
    assert (
        ActivityKind.NOTICE,
        f"its engine was closed because {HALT_REASON}",
    ) in activity(world)
    assert world.scheduler.condition("alpha") == "held"

    async def release() -> None:
        world.scheduler.release("alpha")

    run(world, release)
    assert len(engine.prompts) == 2


def test_a_stop_of_a_line_already_held_keeps_its_hold(
    worlds: Callable[..., World],
) -> None:
    engine = Halting(reason=None)
    world = worlds(engine)
    engine.gate = asyncio.Event()

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "long work")
        while not engine.prompts:
            await asyncio.sleep(0)
        held = world.scheduler.hold("alpha", "was held for another reason")
        assert world.scheduler.interrupt("alpha") is True
        assert world.log.state.lines["alpha"].held_by == held

    run(world, act)
    assert engine.halted == 1
    assert not [t for _, t in activity(world) if "engine was closed" in t]


def test_a_stop_with_no_turn_in_flight_changes_nothing(
    worlds: Callable[..., World],
) -> None:
    world = worlds(Halting())
    assert world.scheduler.interrupt("alpha") is False
    assert world.log.state.lines["alpha"].held_by is None


# Decision A, the ceiling's question.


def test_a_ceiling_question_is_asked_answered_and_carried_to_the_engine(
    worlds: Callable[..., World],
) -> None:
    engine = Deciding()
    world = worlds(engine)
    world.scheduler.engine_for("alpha")
    question = world.scheduler.ceiling_reached("alpha", 2)
    assert question is not None
    asked = world.log.state.messages[question]
    # No operator has written to the line, so the project's default is asked.
    assert asked.sender == LineAddress("alpha")
    operator = asked.recipient
    assert isinstance(operator, OperatorAddress)
    assert asked.choices == CHOICES and "2 background tasks" in asked.body

    async def act() -> None:
        world.scheduler.answer(operator, question, "let it finish")

    run(world, act)
    assert engine.answers == [(question, "let it finish")]
    assert (
        ActivityKind.NOTICE,
        "an operator chose let it finish at its kept session's ceiling",
    ) in activity(world)
    # The answer also reaches the line as mail, as every answer does, and wakes it.
    replies = [m for m in world.log.state.messages.values() if m.reply_to == question]
    assert [m.recipient for m in replies] == [LineAddress("alpha")]
    assert replies[0].delivered and len(engine.prompts) == 1


def test_an_answer_that_chooses_nothing_is_said_to_close_at_the_hard_bound(
    worlds: Callable[..., World],
) -> None:
    engine = Deciding()
    world = worlds(engine)
    world.scheduler.engine_for("alpha")
    question = world.scheduler.ceiling_reached("alpha", 1)
    assert question is not None
    operator = world.log.state.messages[question].recipient
    assert isinstance(operator, OperatorAddress)

    async def act() -> None:
        world.scheduler.answer(operator, question, "maybe")

    run(world, act)
    assert engine.answers == [(question, "maybe")]
    assert any("closes at its hard bound" in text for _, text in activity(world))


def test_a_settled_ceiling_question_is_withdrawn_and_no_longer_routed(
    worlds: Callable[..., World],
) -> None:
    engine = Deciding()
    world = worlds(engine)
    world.scheduler.engine_for("alpha")
    question = world.scheduler.ceiling_reached("alpha", 1)
    assert question is not None
    world.scheduler.ceiling_settled(question)
    world.scheduler.ceiling_settled(question)
    assert world.log.state.messages[question].status is QuestionStatus.WITHDRAWN
    assert world.scheduler.ceiling_reached("ghost", 1) is None


def test_any_failure_to_ask_at_the_ceiling_is_no_question(
    worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """F13. Not only a rejected event, so the connection closes at its hard bound."""
    world = worlds(Deciding())

    def broken(*_: object, **__: object) -> str:
        raise RuntimeError("the log is unwell")

    monkeypatch.setattr(world.scheduler, "ask", broken)
    assert world.scheduler.ceiling_reached("alpha", 1) is None


def test_a_restart_withdraws_ceiling_questions_left_open(tmp_path: Path) -> None:
    """F11. The connection a ceiling question was about ended with the earlier run,
    while the line's own questions stay open."""
    path = tmp_path / "events.sqlite"
    first = World(path, Deciding())
    question = first.scheduler.ceiling_reached("alpha", 2)
    own = first.scheduler.ask("alpha", ANA, "which branch?", ("main", "next"))
    first.log.close()
    second = World(path, Deciding(), create=False)
    try:
        assert second.log.state.messages[question or ""].status is QuestionStatus.OPEN
        second.scheduler.recover()
        messages = second.log.state.messages
        assert messages[question or ""].status is QuestionStatus.WITHDRAWN
        assert messages[own].status is QuestionStatus.OPEN
    finally:
        second.log.close()


def test_only_a_ceiling_question_is_told_from_the_log() -> None:
    from dromon.ceiling import is_question, question_text

    text = question_text("alpha", 3, LineConfig())
    assert is_question(text, CHOICES)
    assert not is_question(text, ("extend", "stop"))
    assert not is_question("Line alpha asks which branch", CHOICES)
    assert not is_question("Line 's kept engine session", CHOICES)
    assert not is_question("which branch?", CHOICES)


def test_a_consent_fallback_becomes_an_operator_question(
    worlds: Callable[..., World],
) -> None:
    """F16."""
    world = worlds(Engine())
    question = world.scheduler.consent_fallback("alpha", "asked for consent.")
    assert question is not None
    message = world.log.state.messages[question]
    assert message.kind is MessageKind.QUESTION
    assert message.body == "Line alpha's engine asked for consent."
    assert isinstance(message.recipient, OperatorAddress)
    assert world.scheduler.consent_fallback("ghost", "x") is None


# Row 12, line tokens per engine session.


def test_a_token_is_revoked_as_its_engine_process_ends_with_the_turn(
    worlds: Callable[..., World],
) -> None:
    """F14. Every engine that uses tokens today ends its process within its turn,
    so each turn starts a process with a token of its own, revoked as it ends."""
    tokens = LineTokens()
    engine = Tokened()
    world = worlds(engine, tokens=tokens)

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "one")
        await world.scheduler.idle()
        world.scheduler.send(ANA, LineAddress("alpha"), "two")

    run(world, act)
    first, second = engine.seen
    assert first is not None and second is not None
    assert len({"built", first, second}) == 3
    for token in (first, second):
        with pytest.raises(AgentError):
            tokens.check("alpha", token)


def test_a_token_outlives_the_turn_while_its_engine_process_runs(
    worlds: Callable[..., World],
) -> None:
    """F14. A token belongs to the engine's process. A process kept between turns
    keeps its token, which works between turns, and is refused once the process
    ends."""
    tokens = LineTokens()
    engine = Keeping()
    world = worlds(engine, tokens=tokens)

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "one")
        await world.scheduler.idle()
        world.scheduler.send(ANA, LineAddress("alpha"), "two")

    run(world, act)
    first, second = engine.seen
    assert first is not None and first == second != "built"
    assert tokens.check("alpha", first) == "alpha"
    engine.running = False
    with pytest.raises(AgentError, match="process has ended"):
        tokens.check("alpha", first)


def test_a_kept_token_is_revoked_when_its_engine_is_discarded(
    worlds: Callable[..., World],
) -> None:
    tokens = LineTokens()
    engine = Keeping()
    world = worlds(engine, tokens=tokens)

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "one")

    run(world, act)
    (token,) = engine.seen
    assert token is not None and tokens.check("alpha", token) == "alpha"
    world.scheduler.configure_line(
        "alpha", LineConfig(stop_policy=StopPolicy.LEAVE_BACKGROUND)
    )
    with pytest.raises(AgentError, match="needs its token"):
        tokens.check("alpha", token)


def test_an_engine_without_a_token_is_given_none(
    worlds: Callable[..., World],
) -> None:
    tokens = LineTokens()
    engine = Tokened()
    engine.line_token = None
    world = worlds(engine, tokens=tokens)

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "x")

    run(world, act)
    assert engine.seen == [None]


# Decision B and row 2, what was left running.


def write_report(directory: Path, name: str, mark: str, pids: list[int]) -> None:
    document = {
        "v": 1,
        "mark": mark,
        "cause": "orphaned",
        "processes": [{"pid": pid, "command": f"sleep {pid}"} for pid in pids],
        "more": 0,
    }
    (directory / name).write_text(json.dumps(document))


def test_reports_of_what_was_left_are_recorded_and_shown(
    tmp_path: Path, worlds: Callable[..., World]
) -> None:
    reports = tmp_path / "left"
    reports.mkdir()
    world = worlds(Engine(), reports=reports)
    write_report(reports, "1-1.json", "alpha.0123", [4242, 4243])
    write_report(reports, "2-1.json", "ghost.0123", [5000])
    world.scheduler.recover()
    left = world.log.state.lines["alpha"].left
    assert left == ProcessesLeft(
        "alpha", "orphaned", (4242, 4243), ("sleep 4242", "sleep 4243"), 0
    )
    # Read reports are kept aside, as the only copy of the whole commands.
    assert sorted(path.name for path in reports.iterdir()) == ["1-1.read", "2-1.read"]
    status = world.scheduler.line_status("alpha")["processes"]
    assert isinstance(status, dict)
    assert status["stop_policy"] == "kill_all"
    assert status["left"] == {
        "cause": "orphaned",
        "processes": [
            {"pid": 4242, "command": "sleep 4242"},
            {"pid": 4243, "command": "sleep 4243"},
        ],
        "more": 0,
        "note": (
            "processes Dromon left running and no longer ends, as recorded, each as"
            " its executable's name and its arguments with secrets masked"
        ),
    }
    assert (
        ActivityKind.NOTICE,
        "processes were left running, orphaned, with pids 4242, 4243",
    ) in activity(world)


def test_a_report_written_during_a_turn_is_recorded_as_it_ends(
    tmp_path: Path, worlds: Callable[..., World]
) -> None:
    reports = tmp_path / "left"
    reports.mkdir()
    world = worlds(Engine(), reports=reports)
    write_report(reports, "1-1.json", "alpha.0123", [4242])

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "x")

    run(world, act)
    left = world.log.state.lines["alpha"].left
    assert left is not None and left.pids == (4242,)


def marked(world: World, *pids: int) -> None:
    mark = f"{MARK_VARIABLE}={world.scheduler.line_mark('alpha')}"
    world.table.rows = [ProcessInfo(DAEMON, 1, "1:00", "dromond")] + [
        ProcessInfo(pid, 1, "0:10", f"nohup work {pid}") for pid in pids
    ]
    world.table.envs = {pid: [mark] for pid in pids}


def test_detached_processes_found_by_the_mark_are_recorded_once(
    worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    monkeypatch.setattr(os, "getpid", lambda: DAEMON)
    world = worlds(Engine())
    marked(world, 4300, 4301)

    async def check() -> None:
        await world.scheduler.report_detached("alpha")
        await world.scheduler.report_detached("alpha")

    asyncio.run(check())
    left = [e for e in world.log.state.lines.values() if e.left is not None]
    assert len(left) == 1
    found = world.log.state.lines["alpha"].left
    assert found is not None and found.cause == "detached"
    assert found.pids == (4300, 4301)
    events = [
        n for k, n in activity(world) if k is ActivityKind.NOTICE and "detached" in n
    ]
    assert len(events) == 1
    marked(world)
    asyncio.run(world.scheduler.report_detached("alpha"))
    marked(world, 4300, 4301)
    asyncio.run(world.scheduler.report_detached("alpha"))
    events = [n for _, n in activity(world) if "detached" in n]
    assert len(events) == 2


def test_where_the_platform_hides_descendants_the_gap_is_recorded_once(
    worlds: Callable[..., World],
) -> None:
    """F1. Where the mark cannot be read and no subreaper exists, a detached process
    cannot be found, and the line's status and the event log say so, once."""
    world = worlds(Engine(), identifiable=False)
    marked(world, 4300)
    asyncio.run(world.scheduler.report_detached("alpha"))
    assert world.log.state.lines["alpha"].left is None
    world.scheduler.record_walk_gap(launcher.WALK_GAP)
    recorded = world.log.last_seq
    world.scheduler.record_walk_gap(launcher.WALK_GAP)
    assert world.log.last_seq == recorded
    assert world.log.state.walk_limit == launcher.WALK_GAP
    status = world.scheduler.line_status("alpha")["processes"]
    assert isinstance(status, dict)
    assert status["walk_limit"] == launcher.WALK_GAP
    assert status["note"] == UNIDENTIFIABLE
    world.scheduler.record_walk_gap(None)
    assert world.log.last_seq == recorded + 1
    assert world.scheduler.line_status("alpha")["processes"]["walk_limit"] is None  # type: ignore[index]


def test_the_startup_scan_records_survivors_of_an_earlier_run(
    worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1. A process an earlier run's engine left, which still carries the line's
    mark of that run, is recorded at start where the mark can be read."""
    import os

    monkeypatch.setattr(os, "getpid", lambda: DAEMON)
    world = worlds(Engine())
    world.table.rows = [
        ProcessInfo(DAEMON, 1, "1:00", "dromond"),
        ProcessInfo(4400, 1, "9:00", "setsid sleep 86400"),
        ProcessInfo(4401, 1, "9:00", "a ghost line's work"),
        ProcessInfo(4402, DAEMON, "0:01", "a launcher of this run"),
    ]
    world.table.envs = {
        4400: [f"{MARK_VARIABLE}=alpha.earlier"],
        4401: [f"{MARK_VARIABLE}=ghost.earlier"],
        4402: [f"{MARK_VARIABLE}=alpha.earlier"],
    }
    world.scheduler.recover()
    left = world.log.state.lines["alpha"].left
    assert left == ProcessesLeft("alpha", "detached", (4400,), ("setsid sleep 86400",))
    blind = worlds(Engine(), identifiable=False, table=world.table)
    blind.scheduler.recover()
    assert blind.log.state.lines["alpha"].left is None


def test_a_kill_all_turn_is_refused_where_the_table_cannot_be_walked(
    worlds: Callable[..., World],
) -> None:
    """F2. A failed walk is never an empty tree, so the turn does not start."""
    engine = Engine()
    asked: list[int] = []

    def denied() -> str:
        asked.append(1)
        return "ps cannot be run: denied"

    world = worlds(engine, walk_check=denied)

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "x")

    run(world, act)
    assert engine.prompts == []
    assert asked == [1]
    ending = activity(world)[-1]
    assert ending[0] is ActivityKind.TURN_ENDED
    assert ending[1].startswith("failed: the line's stop policy is kill_all")
    assert ending[1].endswith("ps cannot be run: denied")


def test_a_leave_background_turn_needs_no_walk(
    worlds: Callable[..., World],
) -> None:
    engine = Engine()
    world = worlds(engine, walk_check=lambda: "ps cannot be run: denied")
    world.scheduler.configure_line(
        "alpha", LineConfig(stop_policy=StopPolicy.LEAVE_BACKGROUND)
    )

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "x")

    run(world, act)
    assert len(engine.prompts) == 1


def test_a_kill_all_turn_starts_where_the_table_can_be_walked(
    worlds: Callable[..., World],
) -> None:
    engine = Engine()
    world = worlds(engine, walk_check=lambda: None)

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "x")

    run(world, act)
    assert len(engine.prompts) == 1


# Row 5, honest outcomes and conditions.


def test_a_turn_that_did_not_succeed_is_shown_as_failed(
    worlds: Callable[..., World],
) -> None:
    engine = Engine()
    engine.subtype = "error_max_turns"
    world = worlds(engine)

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "x")

    run(world, act)
    assert activity(world)[-1] == (
        ActivityKind.TURN_ENDED,
        "failed: the engine ended the turn as error_max_turns",
    )
    turns = world.scheduler.line_status("alpha")["recent_turns"]
    assert isinstance(turns, list)
    assert turns[-1]["outcome"] == FAILED
    assert turns[-1]["subtype"] == "error_max_turns"


def test_a_successful_turn_is_completed(worlds: Callable[..., World]) -> None:
    world = worlds(Engine())

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "x")

    run(world, act)
    turns = world.scheduler.line_status("alpha")["recent_turns"]
    assert isinstance(turns, list) and turns[-1]["outcome"] == COMPLETED
    assert activity(world)[-1] == (ActivityKind.TURN_ENDED, "success")


def test_a_lines_condition_keeps_its_states_apart(
    worlds: Callable[..., World],
) -> None:
    engine = Engine()
    world = worlds(engine)
    assert world.scheduler.condition("alpha") == "idle"
    world.scheduler.ask("alpha", ANA, "which branch?")
    assert world.scheduler.condition("alpha") == "waiting"
    assert world.scheduler.line_status("alpha")["condition"] == "waiting"
    engine.fail = True

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "x")

    run(world, act)
    assert world.scheduler.condition("alpha") == "failed"
    world.scheduler.hold("alpha", "was held")
    assert world.scheduler.condition("alpha") == "held"


# The event that records it.


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"cause": "lost"}, "cause must be one of"),
        ({"pids": (4242,), "commands": ()}, "as many"),
        ({"pids": (), "commands": ()}, "1 to 64"),
        ({"pids": (1,), "commands": ("init",)}, "above 1"),
        ({"pids": (True,), "commands": ("x",)}, "above 1"),
        ({"more": -1}, "more must be"),
        ({"commands": ("x" * 2000,)}, "command"),
    ],
)
def test_a_record_of_what_was_left_is_checked(
    fields: dict[str, Any], message: str
) -> None:
    values: dict[str, Any] = {
        "line_id": "alpha",
        "cause": "stopped",
        "pids": (4242,),
        "commands": ("sleep 600",),
        "more": 0,
    }
    values.update(fields)
    with pytest.raises(InvalidField, match=message):
        ProcessesLeft(**values)


# Re-review of the stop policy, F2, N1, N8 and N9.


def write_left(directory: Path, name: str, document: dict[str, object]) -> None:
    (directory / name).write_text(
        json.dumps({"v": 1, "mark": "alpha.0123", "more": 0, **document})
    )


def test_a_launcher_s_failed_walk_is_recorded_as_an_unverified_stop(
    tmp_path: Path, worlds: Callable[..., World]
) -> None:
    """F2. In the event log and the line's status, not only on standard error."""
    reports = tmp_path / "left"
    reports.mkdir()
    world = worlds(Engine(), reports=reports)
    write_left(
        reports, "1-1.json", {"cause": "stopped", "processes": [], "listed": False}
    )
    world.scheduler.recover()
    line = world.log.state.lines["alpha"]
    assert line.unverified == StopUnverified("alpha", WALK_FAILED_NOTE)
    assert line.left is None
    status = world.scheduler.line_status("alpha")["processes"]
    assert isinstance(status, dict) and status["stop_unverified"] == WALK_FAILED_NOTE
    assert (ActivityKind.NOTICE, WALK_FAILED_NOTE) in activity(world)


def test_a_left_command_is_recorded_masked_and_kept_whole_aside(
    tmp_path: Path, worlds: Callable[..., World]
) -> None:
    """N9. The log and the status hold the executable's name and masked arguments.
    The whole command stays in the read report alone."""
    reports = tmp_path / "left"
    reports.mkdir()
    world = worlds(Engine(), reports=reports)
    command = "/usr/bin/curl -H 'Authorization: Bearer s3cret' https://ana:pw@h/x"
    write_left(
        reports,
        "1-1.json",
        {"cause": "stopped", "processes": [{"pid": 4242, "command": command}]},
    )
    world.scheduler.recover()
    left = world.log.state.lines["alpha"].left
    assert left is not None and left.commands == (redact.summary(command),)
    assert left.commands[0].startswith("curl -H")
    shown = json.dumps(world.scheduler.line_status("alpha")["processes"])
    for leaked in ("s3cret", "ana:pw", "/usr/bin/"):
        assert leaked not in shown and leaked not in left.commands[0]
    assert "s3cret" in (reports / "1-1.read").read_text()


class Signals:
    """The launcher's signalling, recorded instead of sent, so a sweep in these tests
    reaches no real process."""

    def __init__(self, alive: set[int]) -> None:
        self.sent: list[tuple[str, int, int]] = []
        self.alive = alive

    def group(self, group: int, number: int) -> bool:
        self.sent.append(("group", group, number))
        return number != 0 and group in self.alive

    def send(self, pid: int, number: int) -> None:
        self.sent.append(("pid", pid, number))

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(launcher, "signal_group", self.group)
        monkeypatch.setattr(launcher, "_send", self.send)
        monkeypatch.setattr(launcher, "_alive", self.is_alive)
        monkeypatch.setattr(launcher, "CHECK_SECONDS", 0.001)


HARNESS_ROWS = [
    TreeRow(4242, 1, 4242, "/opt/claude --print"),
    TreeRow(4243, 4242, 4242, "/bin/sleep 600 --token abc"),
]


def write_launch(
    directory: Path,
    policy: StopPolicy,
    done: bool = False,
    *,
    launcher_pid: int = 9,
    launched_ns: int = 1,
) -> Path:
    path = directory / f"{launcher_pid}-{launched_ns}{launcher.LAUNCH_SUFFIX}"
    path.write_text(
        json.dumps(
            {
                "v": 1,
                "mark": "alpha.0123",
                "launcher": launcher_pid,
                "harness": 4242,
                "command": "/opt/claude",
                "policy": policy.value,
                "done": done,
            }
        )
    )
    return path


def test_a_swept_launch_under_leave_background_is_recorded(
    tmp_path: Path, worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1. The launcher exited before it finished, so the daemon killed the harness,
    continued its group, and records what still ran, masked."""
    reports = tmp_path / "left"
    reports.mkdir()
    signals = Signals({4242, 4243})
    signals.install(monkeypatch)
    world = worlds(Engine(), reports=reports, sweep_read=lambda: HARNESS_ROWS)
    record = write_launch(reports, StopPolicy.LEAVE_BACKGROUND)
    asyncio.run(world.scheduler.sweep())
    assert ("pid", 4242, signal.SIGKILL) in signals.sent
    assert ("group", 4242, signal.SIGCONT) in signals.sent
    line = world.log.state.lines["alpha"]
    assert line.unverified == StopUnverified("alpha", UNFINISHED_LEAVE_NOTE)
    assert line.left == ProcessesLeft(
        "alpha", SWEPT_CAUSE, (4243,), ("sleep 600 --token ***",), 0
    )
    assert not record.exists()


def test_a_swept_launch_under_kill_all_is_ended_and_recorded(
    tmp_path: Path, worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1. The group is terminated and continued, and killed past the bound. What
    survives the kill is recorded as left."""
    reports = tmp_path / "left"
    reports.mkdir()
    signals = Signals({4243})
    signals.install(monkeypatch)
    world = worlds(Engine(), reports=reports, sweep_read=lambda: HARNESS_ROWS)
    write_launch(reports, StopPolicy.KILL_ALL)
    asyncio.run(world.scheduler.sweep())
    sent = [(kind, number) for kind, _, number in signals.sent if number != 0]
    assert sent[:2] == [("group", signal.SIGTERM), ("group", signal.SIGCONT)]
    assert ("group", signal.SIGKILL) in sent
    line = world.log.state.lines["alpha"]
    assert line.unverified == StopUnverified(
        "alpha", UNFINISHED_KILL_NOTE.format(count=2)
    )
    assert line.left is not None and line.left.pids == (4243,)


def test_the_scheduler_sweeps_at_rest_and_as_it_stops(
    tmp_path: Path, worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """N1. A launcher that exited between turns is swept without waiting for a turn,
    and a finished one leaves no record."""
    reports = tmp_path / "left"
    reports.mkdir()
    Signals(set()).install(monkeypatch)
    world = worlds(Engine(), reports=reports, sweep_read=lambda: HARNESS_ROWS)
    record = write_launch(reports, StopPolicy.KILL_ALL, done=True)

    async def act() -> None:
        return None

    run(world, act)
    assert not record.exists()
    assert world.log.state.lines["alpha"].unverified is None


def test_a_sweep_that_cannot_read_the_table_waits_and_says_so_once(
    tmp_path: Path,
    worlds: Callable[..., World],
    capsys: pytest.CaptureFixture[str],
) -> None:
    reports = tmp_path / "left"
    reports.mkdir()
    world = worlds(Engine(), reports=reports, sweep_read=lambda: "ps is missing")
    record = write_launch(reports, StopPolicy.KILL_ALL)
    asyncio.run(world.scheduler.sweep())
    asyncio.run(world.scheduler.sweep())
    assert record.exists()
    assert capsys.readouterr().err.count("ps is missing") == 1


class Retiring(Engine):
    """An engine that keeps its connection, until the scheduler retires it."""

    def __init__(self) -> None:
        super().__init__()
        self.retired = 0

    async def retire(self) -> None:
        self.retired += 1

    def kept_status(self) -> dict[str, object]:
        return {"kept": True, "seconds_to_close": None, "background_tasks": 0}


def lost_launcher(pid: int, gone: str) -> TreeRow:
    """The row of a launcher that is gone in one of the two ways a sweep tells.
    Either it exited and was not reaped, so its pid still answers a signal, or a
    row under its pid is a launcher that no longer holds its harness."""
    if gone == "defunct":
        return TreeRow(pid, DAEMON, pid, launcher.DEFUNCT)
    return TreeRow(pid, DAEMON, pid, "/venv/bin/python /venv/bin/dromon-launch")


@pytest.mark.parametrize("gone", ["defunct", "harness reparented"])
def test_a_kept_engine_whose_launcher_died_is_failed_closed_and_swept(
    gone: str,
    tmp_path: Path,
    worlds: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Third live run of N1. The launcher's pid, the test's own here, still answers a
    signal, as a killed launcher the daemon has not reaped does, and the harness
    keeps the engine's pipes open. The sweep still finds the launcher gone, ends the
    harness's group, records the unfinished stop, and closes the engine as failed."""
    reports = tmp_path / "left"
    reports.mkdir()
    signals = Signals({4243})
    signals.install(monkeypatch)
    me = os.getpid()
    rows = [lost_launcher(me, gone), *HARNESS_ROWS]
    engine = Retiring()
    world = worlds(engine, reports=reports, sweep_read=lambda: rows)

    async def act() -> None:
        world.scheduler.engine_for("alpha")
        assert world.scheduler.kept_session("alpha")["kept"] is True
        write_launch(
            reports, StopPolicy.KILL_ALL, launcher_pid=me, launched_ns=time.time_ns()
        )
        await world.scheduler.sweep()
        await world.scheduler.close_engines()

    asyncio.run(act())
    assert ("group", 4242, signal.SIGTERM) in signals.sent
    assert ("group", 4242, signal.SIGKILL) in signals.sent
    assert engine.retired == 1
    assert world.scheduler.kept_session("alpha")["kept"] is False
    line = world.log.state.lines["alpha"]
    assert line.unverified == StopUnverified(
        "alpha", UNFINISHED_KILL_NOTE.format(count=2)
    )
    assert (ActivityKind.NOTICE, LAUNCHER_LOST_NOTE) in activity(world)
    assert not any(reports.glob(f"*{launcher.LAUNCH_SUFFIX}"))


def test_a_launcher_that_still_holds_its_harness_is_not_swept(
    tmp_path: Path, worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running launcher is its harness's parent, so its record waits and its
    engine is left alone."""
    reports = tmp_path / "left"
    reports.mkdir()
    signals = Signals({4242, 4243})
    signals.install(monkeypatch)
    me = os.getpid()
    rows = [
        TreeRow(me, DAEMON, me, "/venv/bin/python /venv/bin/dromon-launch"),
        TreeRow(4242, me, 4242, "/opt/claude --print"),
        HARNESS_ROWS[1],
    ]
    engine = Retiring()
    world = worlds(engine, reports=reports, sweep_read=lambda: rows)
    world.scheduler.engine_for("alpha")
    record = write_launch(
        reports, StopPolicy.KILL_ALL, launcher_pid=me, launched_ns=time.time_ns()
    )
    asyncio.run(world.scheduler.sweep())
    assert record.exists()
    assert signals.sent == []
    assert world.scheduler.kept_session("alpha")["kept"] is True
    assert world.log.state.lines["alpha"].unverified is None


def test_a_former_engine_s_launcher_is_swept_without_closing_the_present_one(
    tmp_path: Path, worlds: Callable[..., World], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record written before the present engine was built was a former engine's,
    so its sweep is recorded and the present engine stays."""
    reports = tmp_path / "left"
    reports.mkdir()
    Signals({4243}).install(monkeypatch)
    engine = Retiring()
    world = worlds(engine, reports=reports, sweep_read=lambda: HARNESS_ROWS)
    before = time.time_ns()
    world.scheduler.engine_for("alpha")
    write_launch(reports, StopPolicy.KILL_ALL, launched_ns=before - 1)
    asyncio.run(world.scheduler.sweep())
    assert world.log.state.lines["alpha"].unverified is not None
    assert world.scheduler.kept_session("alpha")["kept"] is True
    assert engine.retired == 0
    assert (ActivityKind.NOTICE, LAUNCHER_LOST_NOTE) not in activity(world)


def test_an_idle_daemon_sweeps_a_kept_engine_s_gone_launcher_at_rest(
    tmp_path: Path,
    worlds: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fourth live run of N1. The line is idle with a kept engine, no wake is pending
    and no turn or status call comes, so only the scheduler's own rest can start the
    sweep. The rest is bounded even then. Its bound is shortened here, and the
    scheduler's clock is fixed, so the wall clock never moves."""
    monkeypatch.setattr(scheduler_module, "MAX_REST_SECONDS", 0.05)
    reports = tmp_path / "left"
    reports.mkdir()
    signals = Signals({4243})
    signals.install(monkeypatch)
    engine = Retiring()
    world = worlds(engine, reports=reports, sweep_read=lambda: HARNESS_ROWS)
    world.scheduler.engine_for("alpha")
    seen: list[bool] = []

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        await asyncio.sleep(0.2)
        assert world.scheduler.seconds_until_next_wake() is None
        # The launcher of the kept connection is gone, between turns.
        record = write_launch(reports, StopPolicy.KILL_ALL, launched_ns=time.time_ns())
        for _ in range(300):
            await asyncio.sleep(0.01)
            if not record.exists() and engine.retired:
                break
        seen.append(not record.exists() and engine.retired == 1)
        stop.set()
        await loop

    asyncio.run(go())
    assert seen == [True]
    assert ("group", 4242, signal.SIGKILL) in signals.sent
    assert world.log.state.lines["alpha"].unverified == StopUnverified(
        "alpha", UNFINISHED_KILL_NOTE.format(count=2)
    )
    assert (ActivityKind.NOTICE, LAUNCHER_LOST_NOTE) in activity(world)
    assert "swept the launch record of alpha, finished False" in capsys.readouterr().err


def test_a_sweep_that_fails_or_recovers_says_so(
    tmp_path: Path,
    worlds: Callable[..., World],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failure in recording what a sweep did is said, as a failure to read the
    table is, and so is the table's being read again."""
    reports = tmp_path / "left"
    reports.mkdir()
    Signals({4243}).install(monkeypatch)
    answers: list[list[TreeRow] | str] = ["ps is missing", HARNESS_ROWS]
    world = worlds(Engine(), reports=reports, sweep_read=lambda: answers.pop(0))
    write_launch(reports, StopPolicy.KILL_ALL)

    def broken(swept: launcher.Swept) -> None:
        del swept
        raise RuntimeError("the record broke")

    monkeypatch.setattr(world.scheduler, "_record_swept", broken)
    asyncio.run(world.scheduler.sweep())
    asyncio.run(world.scheduler.sweep())
    err = capsys.readouterr().err
    assert "cannot be read: ps is missing" in err
    assert "the process table can be read again" in err
    assert "sweep of launch records failed: RuntimeError('the record broke')" in err


class Costing(Engine):
    """An engine that measured a cost before its turn was cancelled. Review N8."""

    def __init__(self, cost: float | None) -> None:
        super().__init__()
        self.cost = cost
        self.taken = 0

    def take_interrupted_cost(self) -> float | None:
        self.taken += 1
        cost, self.cost = self.cost, None
        return cost


@pytest.mark.parametrize("cost", [0.75, None])
def test_an_interrupted_turn_s_measured_cost_counts_toward_the_budget(
    worlds: Callable[..., World], cost: float | None
) -> None:
    engine = Costing(cost)
    world = worlds(engine)
    engine.gate = asyncio.Event()

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "long work")
        while not engine.prompts:
            await asyncio.sleep(0)
        assert world.scheduler.interrupt("alpha") is True

    run(world, act)
    status = world.scheduler.line_status("alpha")
    assert engine.taken == 1
    estimate = status["cost_usd"]["estimate"]  # type: ignore[index]
    recent = cast(list[dict[str, object]], status["recent_turns"])
    if cost is None:
        assert estimate == "0.0000" and recent == []
        return
    assert estimate == "0.7500"
    assert [(e["kind"], e["cost_usd"]) for e in recent] == [
        (INTERRUPTED_KIND, "0.7500")
    ]


def test_an_unverified_stop_is_checked_and_needs_its_line() -> None:
    with pytest.raises(InvalidField, match="note must not be empty"):
        StopUnverified("alpha", "")
    with pytest.raises(InvalidField):
        StopUnverified("alpha", "x" * 2000)
    with pytest.raises(InvalidField):
        StopUnverified("not a line id!", "a note")


def test_an_unverified_stop_for_a_missing_line_is_refused(
    worlds: Callable[..., World],
) -> None:
    world = worlds(Engine())
    with pytest.raises(EventRejected):
        world.log.append(StopUnverified("ghost", WALK_FAILED_NOTE))
