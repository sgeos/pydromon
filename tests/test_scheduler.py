"""Layer 1 messaging and wakes, driven by scripted engines instead of Claude Code.

The scripted engine calls the real ``send_message`` tool handler, so every message a
line sends travels the same path as one sent by a model.
"""

import asyncio
import gc
import hashlib
import re
import threading
from collections.abc import Callable, Coroutine, Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
import keleusma
from claude_agent_sdk import SdkMcpTool

from dromon import scheduler as scheduler_module
from dromon.activity import ActivityKind
from dromon.driver import Driver, DriverError, Event, Strategy
from dromon.engine import ClaudeCodeLine, EnginePlanLimit, TurnReport
from dromon.eventlog import EventLog, read_entries
from dromon.gitssh import GitSshAccess
from dromon.lineconfig import ConfigError, LineConfig, ToolPolicy, encode_config
from dromon.overrides import OverrideError
from dromon.model import (
    AgentAccess,
    AgentAddress,
    AgentRegistered,
    Correlation,
    EventRejected,
    LineAddress,
    LineOverridden,
    LineReleased,
    LineSession,
    LineState,
    LineStateChanged,
    Message,
    MessageCorrelated,
    MessageDelivered,
    MessageRead,
    MessageKind,
    MessageSent,
    OperatorAddress,
    QuestionStatus,
    TurnCorrelated,
    WakeIssued,
    WakeRequested,
)
from dromon.limits import DEFAULT_LIMITS, Limits
from dromon.mailbox import MailSummary, summary
from dromon.scheduler import LineEngine, LineSpec, Scheduler, compose_prompt
from dromon import priority

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")

type Turn = list[dict[str, Any]]
"""The send_message calls one scripted turn makes."""


class Scripted:
    """A line engine that makes scripted tool calls and records its prompts."""

    def __init__(
        self,
        tools: Sequence[SdkMcpTool[Any]],
        turns: list[Turn],
        spec: LineSpec | None = None,
    ) -> None:
        self.spec = spec
        self.session_id: str | None = spec.session_id if spec is not None else None
        self.tools = {tool.name: tool for tool in tools}
        self.turns = turns
        self.prompts: list[str] = []
        self.results: list[dict[str, Any]] = []
        self.gate: asyncio.Event | None = None
        self.fail = False
        self.plan_limit: str | None = None
        self.cost: float | None = None

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("the engine broke")
        if self.plan_limit is not None:
            raise EnginePlanLimit(self.plan_limit)
        calls = self.turns.pop(0) if self.turns else []
        # Like a real engine, a new session gets an identifier and a resumed one
        # keeps it.
        if self.session_id is None:
            self.session_id = "session-1"
        for call in calls:
            # A call names its tool with the key "tool", and sends by default.
            args = {key: value for key, value in call.items() if key != "tool"}
            tool = self.tools[str(call.get("tool", "send_message"))]
            self.results.append(await tool.handler(args))
        assert self.session_id is not None
        return TurnReport(
            "success", 1, "ok", (), None, None, self.session_id, None, self.cost
        )


class World:
    """A scheduler over a real event log, with one scripted engine per line."""

    def __init__(
        self,
        path: Path,
        scripts: dict[str, list[Turn]],
        *,
        limits: Limits = DEFAULT_LIMITS,
        hold_wakes: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = path
        self.log = EventLog(path)
        self.scripts = scripts
        self.engines: dict[str, Scripted] = {}
        self.scheduler = Scheduler(
            self.log,
            self.build,
            limits=limits,
            hold_wakes=hold_wakes,
            tick_seconds=0,
            clock=clock or (lambda: 0.0),
        )
        self.scheduler.ensure_operator("ana")
        for line_id in scripts:
            self.scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Scripted:
        engine = Scripted(tools, self.scripts[spec.line_id], spec)
        self.engines[spec.line_id] = engine
        return engine

    @staticmethod
    def build_nothing(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Scripted:
        raise AssertionError(f"no engine may start, but {spec.line_id} did")

    def engine(self, line_id: str) -> Scripted:
        """The line's engine, built now if the scheduler has not built it yet."""
        self.scheduler.engine_for(line_id)
        return self.engines[line_id]

    def events(self) -> list[object]:
        return [entry.event for entry in read_entries(self.path)]


type MakeWorld = Callable[..., World]


async def call_tool(tool: SdkMcpTool[Any], args: dict[str, Any]) -> dict[str, Any]:
    return await tool.handler(args)


@pytest.fixture
def make_world(tmp_path: Path) -> Iterator[MakeWorld]:
    built: list[World] = []

    def make(
        scripts: dict[str, list[Turn]],
        *,
        limits: Limits = DEFAULT_LIMITS,
        hold_wakes: bool = False,
        clock: Callable[[], float] | None = None,
    ) -> World:
        target = World(
            tmp_path / f"events-{len(built)}.sqlite",
            scripts,
            limits=limits,
            hold_wakes=hold_wakes,
            clock=clock,
        )
        built.append(target)
        return target

    yield make
    for target in built:
        target.log.close()


async def settle(target: World, act: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Run the scheduler, perform ``act``, and wait until nothing more can happen."""
    stop = asyncio.Event()
    loop = asyncio.create_task(target.scheduler.run(stop))
    await asyncio.sleep(0)
    await act()
    await target.scheduler.idle()
    stop.set()
    await loop


def activity_of(scheduler: Scheduler, line_id: str) -> list[tuple[ActivityKind, str]]:
    return [(entry.kind, entry.text) for entry in scheduler.activity.after(line_id, 0)]


def run(target: World, act: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(settle(target, act))


def test_a_finished_lines_vm_is_released_before_python_gc(
    tmp_path: Path,
) -> None:
    class DriverEngine:
        def __init__(self, driver: Driver) -> None:
            self.driver = driver
            self.session_id: str | None = None

        async def run_turn(self, prompt: str) -> TurnReport:
            del prompt
            self.driver.step(Event("turn_start"))
            return TurnReport(
                "success", 1, "ok", (), None, None, "session-1", None, None
            )

    # Review r4, finding 5. The test proves that closing the engines releases the
    # VM without a collection. ``tests/conftest.py`` turns automatic collection off,
    # and nothing here collects, so ``gc.get_objects`` also counts cyclic garbage
    # left by earlier tests. Each check is therefore a difference from the baseline
    # taken here, never a total, and it holds only while the released driver has no
    # reference cycle.
    def vm_count() -> int:
        return sum(isinstance(value, keleusma.VM) for value in gc.get_objects())

    log = EventLog(tmp_path / "driver-lifecycle.sqlite")
    scheduler: Scheduler

    def build(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> DriverEngine:
        del tools
        driver = priority.steered(
            spec.model,
            spec.strategy,
            lambda: scheduler.redirect_reason(spec.line_id),
        )
        return DriverEngine(driver)

    scheduler = Scheduler(log, build, tick_seconds=0)
    baseline = vm_count()
    scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
    engine = cast(DriverEngine, scheduler.engine_for("alpha"))
    driver = engine.driver
    assert vm_count() - baseline == 1
    asyncio.run(engine.run_turn("finished"))

    asyncio.run(scheduler.close_engines())

    assert driver.closed
    assert vm_count() - baseline == 0
    log.close()


def test_dropping_an_engine_without_a_loop_waits_for_its_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unused_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        del spec, tools
        raise AssertionError("the test inserts its engine directly")

    log = EventLog(tmp_path / "driver-no-loop-drop.sqlite")
    scheduler = Scheduler(log, unused_engine)
    loop = asyncio.new_event_loop()
    entered = threading.Event()
    release = threading.Event()
    registered = threading.Event()
    finished = threading.Event()
    closed = threading.Event()
    thread_errors: list[BaseException] = []
    active_driver: list[Driver] = []
    original_close = Driver.close

    def observed_close(driver: Driver) -> None:
        original_close(driver)
        if active_driver and driver is active_driver[0]:
            closed.set()

    monkeypatch.setattr(Driver, "close", observed_close)

    async def active_turn() -> None:
        driver = Driver.for_model(HAIKU, Strategy.SUBSCRIPTION)
        active_driver.append(driver)
        engine = ClaudeCodeLine(driver, Path("/work"))
        scheduler._engines["alpha"] = engine  # pyright: ignore[reportPrivateUsage]
        task = asyncio.current_task()
        assert task is not None
        scheduler._running["alpha"] = cast(asyncio.Task[None], task)  # pyright: ignore[reportPrivateUsage]
        entered.set()
        await asyncio.to_thread(release.wait)
        driver.step(Event("turn_start"))

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        task = loop.create_task(active_turn())

        def collect(error_task: asyncio.Task[None]) -> None:
            try:
                error_task.result()
            except BaseException as error:
                thread_errors.append(error)
            finally:
                finished.set()

        task.add_done_callback(collect)
        loop.run_forever()

    thread = threading.Thread(target=run_loop)
    thread.start()
    try:
        assert entered.wait(5)
        scheduler._drop_engine("alpha")  # pyright: ignore[reportPrivateUsage]
        assert not active_driver[0].closed

        loop.call_soon_threadsafe(registered.set)
        assert registered.wait(5)
        release.set()
        assert finished.wait(5)
        assert closed.wait(5)
        assert not thread_errors
        assert active_driver[0].closed
    finally:
        release.set()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        loop.close()
        log.close()


def send(
    target: World, recipient: str, body: str
) -> Callable[[], Coroutine[Any, Any, None]]:
    async def act() -> None:
        target.scheduler.send(ANA, LineAddress(recipient), body)

    return act


def wakes_for(target: World, line_id: str) -> list[WakeRequested]:
    return [
        e
        for e in target.events()
        if isinstance(e, WakeRequested) and e.line_id == line_id
    ]


def test_a_message_wakes_an_idle_line_and_is_delivered(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    run(target, send(target, "alpha", "hello"))
    events = target.events()
    message_id = next(e.message_id for e in events if isinstance(e, MessageSent))
    wake = wakes_for(target, "alpha")
    assert [w.message_id for w in wake] == [message_id]
    tail = events[-7:]
    assert isinstance(tail[0], WakeRequested)
    assert tail[1] == WakeIssued(wake[0].wake_id)
    assert tail[2] == LineStateChanged("alpha", LineState.RUNNING)
    assert tail[3] == MessageDelivered(message_id)
    assert tail[4] == LineSession("alpha", "session-1")
    assert tail[5] == LineStateChanged("alpha", LineState.IDLE)
    prompt = target.engines["alpha"].prompts[0]
    assert prompt.splitlines()[1] == "1 new message. 1 unread. 1 unanswered."
    assert f"  {message_id} from @ana, note, unread" in prompt
    assert "hello" not in prompt
    assert received(target, prompt) == [("@ana", "hello")]


def test_a_registered_agents_note_wakes_an_idle_line_for_exactly_one_turn(
    make_world: MakeWorld,
) -> None:
    # An external service, such as a game table, tells a line its turn has come
    # through the same send_message tool the agent socket serves.
    target = make_world({"alpha": [[], [], []]})
    table = AgentAddress("table")
    target.log.append(AgentRegistered(table.agent_id, AgentAccess.MESSAGE, ""))
    assert target.log.state.lines["alpha"].state is LineState.IDLE

    async def act() -> None:
        result = await call_tool(
            target.scheduler.send_tool(table), {"to": "alpha", "body": "your move"}
        )
        assert result.get("is_error") is not True

    run(target, act)
    sent = [e for e in target.events() if isinstance(e, MessageSent)]
    assert [(m.sender, m.recipient) for m in sent] == [(table, LineAddress("alpha"))]
    assert [w.message_id for w in wakes_for(target, "alpha")] == [sent[0].message_id]
    prompts = target.engines["alpha"].prompts
    assert len(prompts) == 1
    assert received(target, prompts[0]) == [("&table", "your move")]
    assert target.log.state.lines["alpha"].state is LineState.IDLE
    assert target.log.state.messages[sent[0].message_id].delivered
    turns = target.scheduler.line_status("alpha")["autonomous_turns"]
    assert isinstance(turns, dict)
    assert (turns["allowed"], turns["used"]) == (
        DEFAULT_LIMITS.max_autonomous_turns,
        1,
    )


def test_a_line_sends_only_to_its_recipients_and_itself(
    make_world: MakeWorld,
) -> None:
    # A player at a game table may answer the table but has no private channel to
    # another player, or to an operator.
    turn: Turn = [
        {"to": "&table", "body": "I pass"},
        {"to": "beta", "body": "psst"},
        {"to": "@ana", "body": "hello"},
        {"to": "alpha", "body": "a note to myself"},
    ]
    target = make_world({"beta": [], "alpha": [turn]})
    table = AgentAddress("table")
    target.log.append(AgentRegistered(table.agent_id, AgentAccess.MESSAGE, ""))
    target.scheduler.configure_line("alpha", LineConfig(recipients=(str(table),)))

    async def act() -> None:
        await call_tool(
            target.scheduler.send_tool(table), {"to": "alpha", "body": "your move"}
        )

    run(target, act)
    results = target.engines["alpha"].results
    assert [r.get("is_error") is True for r in results] == [False, True, True, False]
    refusal = results[1]["content"][0]["text"]
    assert refusal == (
        "line alpha may send mail only to &table, by its configuration's recipients,"
        " so not to beta"
    )
    sent = [
        (m.recipient, m.body)
        for m in target.events()
        if isinstance(m, MessageSent) and m.sender == LineAddress("alpha")
    ]
    assert sent == [(table, "I pass"), (LineAddress("alpha"), "a note to myself")]


def test_an_agents_correlated_notes_total_the_turns_they_start(
    make_world: MakeWorld,
) -> None:
    # Queue of 2026-10-28, item A4. Two notes keyed to two tasks start one turn,
    # which counts under both and records that it carried two keys.
    target = make_world({"alpha": [[], []], "beta": []})
    table = AgentAddress("table")
    target.log.append(AgentRegistered(table.agent_id, AgentAccess.MESSAGE, ""))
    engine = target.engine("alpha")
    engine.cost = 0.25
    tool = target.scheduler.send_tool(table)

    async def act() -> None:
        for key in ("game-8", "game-7"):
            result = await call_tool(
                tool, {"to": "alpha", "body": f"move in {key}", "correlation": key}
            )
            assert result.get("is_error") is not True
        await target.scheduler.idle()
        engine.cost = None
        await call_tool(tool, {"to": "alpha", "body": "again", "correlation": "game-7"})
        await call_tool(tool, {"to": "beta", "body": "no key"})

    run(target, act)
    assert len(engine.prompts) == 2
    events = target.events()
    marked = [e for e in events if isinstance(e, MessageCorrelated)]
    assert [e.key for e in marked] == ["game-8", "game-7", "game-7"]
    assert [e for e in events if isinstance(e, TurnCorrelated)] == [
        TurnCorrelated("alpha", ("game-7", "game-8"), 2, 250_000),
        TurnCorrelated("alpha", ("game-7",), 1, None),
    ]
    totals = target.log.state.correlations
    assert totals["game-7"] == Correlation(2, 1, 250_000, 1)
    assert totals["game-8"] == Correlation(1, 1, 250_000, 0)


def test_only_an_agents_note_to_a_line_carries_a_correlation_key(
    make_world: MakeWorld,
) -> None:
    turn: Turn = [{"to": "beta", "body": "keyed", "correlation": "game-7"}]
    target = make_world({"alpha": [turn], "beta": []})
    table = AgentAddress("table")
    target.log.append(AgentRegistered(table.agent_id, AgentAccess.MESSAGE, ""))
    agent_tool = target.scheduler.send_tool(table)
    line_tool = target.scheduler.send_tool("alpha")
    schemas = [
        cast(dict[str, object], tool.input_schema) for tool in (line_tool, agent_tool)
    ]
    assert [
        "correlation" in cast(dict[str, object], schema["properties"])
        for schema in schemas
    ] == [False, True]
    refusals: list[str] = []

    async def act() -> None:
        for args in (
            {"to": "@ana", "body": "x", "correlation": "game-7"},
            {"to": "alpha", "body": "x", "correlation": "Game 7"},
            {"to": "alpha", "body": "x", "correlation": 7},
        ):
            result = await call_tool(agent_tool, args)
            assert result.get("is_error") is True
            refusals.append(result["content"][0]["text"])
        with pytest.raises(EventRejected, match="only an external agent"):
            target.scheduler.send(
                ANA, LineAddress("alpha"), "start", correlation="game-7"
            )
        target.scheduler.send(ANA, LineAddress("alpha"), "start")

    run(target, act)
    assert "carried only to a line" in refusals[0]
    assert "correlation" in refusals[1]
    assert refusals[2] == "correlation must be text"
    assert target.engines["alpha"].results[0].get("is_error") is True
    events = target.events()
    assert not [e for e in events if isinstance(e, MessageCorrelated | TurnCorrelated)]
    assert [m.body for m in events if isinstance(m, MessageSent)] == ["start"]


def test_two_lines_exchange_messages_and_each_recipient_is_woken(
    make_world: MakeWorld,
) -> None:
    target = make_world(
        {
            "alpha": [[{"to": "beta", "body": "ping"}], []],
            "beta": [[{"to": "alpha", "body": "pong"}]],
        },
    )
    run(target, send(target, "alpha", "start"))
    sent = [e for e in target.events() if isinstance(e, MessageSent)]
    ping = next(m for m in sent if m.body == "ping")
    pong = next(m for m in sent if m.body == "pong")
    assert (ping.sender, ping.recipient) == (LineAddress("alpha"), LineAddress("beta"))
    assert (pong.sender, pong.recipient) == (LineAddress("beta"), LineAddress("alpha"))
    assert [w.message_id for w in wakes_for(target, "beta")] == [ping.message_id]
    assert len(wakes_for(target, "alpha")) == 2
    assert received(target, target.engines["beta"].prompts[0]) == [("alpha", "ping")]
    assert all(r.get("is_error") is not True for r in target.engines["alpha"].results)


def test_a_message_for_a_running_line_waits_for_the_next_turn(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[], []]})
    engine = target.engine("alpha")
    gate = asyncio.Event()
    engine.gate = gate

    async def act() -> None:
        target.scheduler.send(ANA, LineAddress("alpha"), "first")
        while not engine.prompts:
            await asyncio.sleep(0)
        target.scheduler.send(ANA, LineAddress("alpha"), "second")
        for _ in range(10):
            await asyncio.sleep(0)
        assert len(engine.prompts) == 1
        assert target.log.state.lines["alpha"].state is LineState.RUNNING
        gate.set()

    run(target, act)
    prompts = engine.prompts
    assert len(prompts) == 2
    assert bodies(target, prompts[0]) == ["first"]
    assert bodies(target, prompts[1]) == ["second"]


def test_messages_waiting_together_are_delivered_in_one_turn(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[]]}, hold_wakes=True)
    target.scheduler.send(ANA, LineAddress("alpha"), "one")
    target.scheduler.send(ANA, LineAddress("alpha"), "two")
    assert len(target.scheduler.pending("alpha")) == 2
    target.scheduler = Scheduler(target.log, target.build, tick_seconds=0)

    async def nothing() -> None:
        return None

    run(target, nothing)
    prompts = target.engines["alpha"].prompts
    assert len(prompts) == 1
    assert bodies(target, prompts[0]) == ["one", "two"]
    assert prompts[0].splitlines()[1] == "2 new messages. 2 unread. 2 unanswered."


def test_held_wakes_record_messages_but_run_nothing(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]}, hold_wakes=True)
    run(target, send(target, "alpha", "hello"))
    assert target.engines == {}
    assert len(target.scheduler.pending("alpha")) == 1


def test_a_runaway_exchange_is_held_and_raises_an_operator_question(
    make_world: MakeWorld,
) -> None:
    forever = [[{"to": "beta", "body": "again"}] for _ in range(50)]
    back = [[{"to": "alpha", "body": "again"}] for _ in range(50)]
    target = make_world(
        {"alpha": forever, "beta": back}, limits=Limits(max_autonomous_turns=3)
    )
    run(target, send(target, "alpha", "go"))
    state = target.log.state
    held = {line_id: line.held_by for line_id, line in state.lines.items()}
    assert held["alpha"] is not None and held["beta"] is not None
    # alpha's question goes to @ana, who messaged it, and beta's to the default
    # operator, because no operator ever messaged beta.
    asked = {"alpha": ANA, "beta": OperatorAddress("operator")}
    for line_id, question_id in held.items():
        assert question_id is not None
        question = state.messages[question_id]
        assert (question.sender, question.recipient) == (
            LineAddress(line_id),
            asked[line_id],
        )
        assert "3 turns in a row without mail from an operator" in question.body
        assert question.choices == ("resume",)
    # The exchange stopped long before the scripts ran out.
    assert len(target.engines["alpha"].prompts) <= 4
    assert len(target.engines["beta"].prompts) <= 4


def test_answering_a_hold_question_releases_the_line(make_world: MakeWorld) -> None:
    forever = [[{"to": "beta", "body": "again"}] for _ in range(50)]
    back = [[{"to": "alpha", "body": "again"}] for _ in range(50)]
    target = make_world(
        {"alpha": forever, "beta": back}, limits=Limits(max_autonomous_turns=2)
    )
    run(target, send(target, "alpha", "go"))
    question_id = target.log.state.lines["beta"].held_by
    assert question_id is not None
    before = len(target.engines["beta"].prompts)

    async def act() -> None:
        target.scheduler.answer(OperatorAddress("operator"), question_id, "resume")

    run(target, act)
    assert len(target.engines["beta"].prompts) > before
    assert any(
        isinstance(e, LineReleased) and e.line_id == "beta" for e in target.events()
    )


def test_release_resumes_a_held_line(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[] for _ in range(10)]})
    target.scheduler.hold("alpha", "was held by a test")
    run(target, send(target, "alpha", "waiting"))
    assert target.engines == {}
    target.scheduler.release("alpha")
    run(target, send(target, "alpha", "more"))
    assert target.log.state.lines["alpha"].held_by is None
    assert target.engines["alpha"].prompts
    with pytest.raises(EventRejected, match="not held"):
        target.scheduler.release("alpha")


def test_a_cost_budget_holds_a_line(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[], [], []]}, limits=Limits(max_cost_usd=0.015))
    target.engine("alpha").cost = 0.01
    run(target, send(target, "alpha", "one"))
    assert target.log.state.lines["alpha"].held_by is None
    run(target, send(target, "alpha", "two"))
    question_id = target.log.state.lines["alpha"].held_by
    assert question_id is not None
    assert "over the budget of $0.01" in target.log.state.messages[question_id].body


def test_a_cooldown_delays_the_next_turn(make_world: MakeWorld) -> None:
    now = [100.0]
    target = make_world(
        {"alpha": [[], []]}, limits=Limits(cooldown_seconds=30), clock=lambda: now[0]
    )
    run(target, send(target, "alpha", "one"))
    assert len(target.engines["alpha"].prompts) == 1

    async def act() -> None:
        target.scheduler.send(ANA, LineAddress("alpha"), "two")
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(target.engines["alpha"].prompts) == 1
        now[0] += 31

    run(target, act)
    assert len(target.engines["alpha"].prompts) == 2


def test_a_hold_asks_the_default_operator_when_none_messaged(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": []}, hold_wakes=True)
    question_id = target.scheduler.hold("alpha", "was held by a test")
    recipient = target.log.state.messages[question_id].recipient
    assert recipient == OperatorAddress("operator")


def test_an_engine_failure_holds_the_line_and_asks_an_operator(
    make_world: MakeWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    """Luna's turn of 2026-10-27. A turn that failed, with no mail from the line to
    an operator during it, reaches an operator as a question on a held line, so it
    is neither lost in the activity log nor retried unseen by the next mail."""
    target = make_world({"alpha": [[], []]})
    target.engine("alpha").fail = True
    run(target, send(target, "alpha", "hello"))
    line = target.log.state.lines["alpha"]
    assert line.state is LineState.IDLE
    assert len(target.engines["alpha"].prompts) == 1
    err = capsys.readouterr().err
    assert "turn failed: the engine broke" in err
    (question_id,) = questions_for(target)
    assert line.held_by == question_id
    question = target.log.state.messages[question_id]
    assert "failed: the engine broke" in question.body
    target.engines["alpha"].fail = False
    run(target, send(target, "alpha", "again"))
    assert len(target.engines["alpha"].prompts) == 1
    target.scheduler.send(ANA, LineAddress("alpha"), "resume", reply_to=question_id)
    run(target, send(target, "alpha", "after"))
    # The answer released the line, and the mail that waited is delivered.
    assert len(target.engines["alpha"].prompts) > 1
    assert target.log.state.lines["alpha"].held_by is None


def test_a_plan_limit_holds_the_line_until_operator_release(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[], []]})
    target.engine("alpha").plan_limit = "Codex plan limit reached; reset time tomorrow"
    run(target, send(target, "alpha", "hello"))
    question_id = target.log.state.lines["alpha"].held_by
    assert question_id is not None
    assert "reset time tomorrow" in target.log.state.messages[question_id].body
    assert len(target.engines["alpha"].prompts) == 1


def test_a_changed_git_ssh_file_refuses_the_turn_and_holds_the_line(
    make_world: MakeWorld, tmp_path: Path
) -> None:
    """Review of 2026-10-29, M1. A line whose sandbox is disabled could rewrite the
    pinned host keys for every line given the option, so each turn first compares
    both files with the digests taken at load. A change refuses the turn before any
    engine runs, holds the line and asks an operator, naming the file and never its
    contents. Answering resumes the line, whose next turn is checked again."""
    hosts, key = tmp_path / "known_hosts", tmp_path / "deploy.pub"
    hosts.write_text("pinned\n")
    key.write_text("key\n")
    for file in (hosts, key):
        file.chmod(0o644)
    access = GitSshAccess(
        "/run/agent.sock",
        str(hosts),
        str(key),
        hashlib.sha256(hosts.read_bytes()).hexdigest(),
        hashlib.sha256(key.read_bytes()).hexdigest(),
    )
    target = make_world({"alpha": [[], [], [], []]})
    target.scheduler.set_git_ssh({"alpha": access})
    run(target, send(target, "alpha", "hello"))
    assert len(target.engines["alpha"].prompts) == 1
    hosts.write_text("swapped-contents\n")
    run(target, send(target, "alpha", "again"))
    assert len(target.engines["alpha"].prompts) == 1
    question_id = target.log.state.lines["alpha"].held_by
    assert question_id is not None
    body = target.log.state.messages[question_id].body
    assert f"the git_ssh file {hosts} changed" in body
    assert "swapped-contents" not in body
    ending = activity_of(target.scheduler, "alpha")[-1]
    assert ending == (
        ActivityKind.TURN_ENDED,
        f"held: the line {body.split('which ', 1)[1].split('. Its mail', 1)[0]}",
    )
    # Resumed with the file still changed, the next turn is refused again.
    target.scheduler.send(ANA, LineAddress("alpha"), "resume", reply_to=question_id)
    run(target, send(target, "alpha", "still"))
    assert len(target.engines["alpha"].prompts) == 1
    again = target.log.state.lines["alpha"].held_by
    assert again is not None and again != question_id
    # Restored, the line runs once it is resumed.
    hosts.write_text("pinned\n")
    target.scheduler.send(ANA, LineAddress("alpha"), "resume", reply_to=again)
    run(target, send(target, "alpha", "restored"))
    assert len(target.engines["alpha"].prompts) > 1
    assert target.log.state.lines["alpha"].held_by is None


def test_an_open_question_never_stops_its_line(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[ask_call(question="?", to="@ana")], []]})
    run(target, send(target, "alpha", "start"))
    (question_id,) = questions_for(target)
    run(target, send(target, "alpha", "meanwhile"))
    # The question is still open, and the line took its next turn anyway.
    assert target.log.state.messages[question_id].status is QuestionStatus.OPEN
    assert len(target.engines["alpha"].prompts) == 2
    assert bodies(target, target.engines["alpha"].prompts[1]) == ["meanwhile"]


def test_recovery_returns_a_line_left_running_to_idle(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    target.log.append(LineStateChanged("alpha", LineState.RUNNING))
    target.scheduler.recover()
    assert target.log.state.lines["alpha"].state is LineState.IDLE


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"to": "nobody", "body": "x"}, "no line nobody"),
        ({"to": "@nobody", "body": "x"}, "no operator nobody"),
        ({"to": "Bad Name", "body": "x"}, "not a valid identifier"),
        ({"to": "alpha", "body": "x", "priority": True}, "end your own turn"),
        ({"to": "@ana", "body": "x", "priority": True}, "goes to a line"),
        ({"to": "beta", "body": "x", "priority": "yes"}, "must be true or false"),
        ({"to": "beta", "body": 3}, "to and body must be text"),
        ({"to": "beta", "body": "x", "reply_to": 3}, "reply_to must be text"),
        ({"to": "beta", "body": "x", "reply_to": "missing"}, "no message missing"),
        ({"to": "beta", "body": "x", "urgent": True}, "unexpected argument urgent"),
    ],
)
def test_a_bad_tool_call_returns_an_error_and_sends_nothing(
    make_world: MakeWorld, args: dict[str, Any], reason: str
) -> None:
    target = make_world({"alpha": [], "beta": []}, hold_wakes=True)
    before = len(target.log.state.messages)
    result = asyncio.run(call_tool(target.scheduler.send_tool("alpha"), args))
    assert result["is_error"] is True
    assert reason in result["content"][0]["text"]
    assert len(target.log.state.messages) == before


def test_a_line_can_message_an_operator(make_world: MakeWorld) -> None:
    target = make_world({"alpha": []}, hold_wakes=True)
    result = asyncio.run(
        call_tool(target.scheduler.send_tool("alpha"), {"to": "@ana", "body": "done"})
    )
    assert str(result["content"][0]["text"]).startswith("sent as m-")
    sent = list(target.log.state.messages.values())[-1]
    assert (sent.sender, sent.recipient) == (LineAddress("alpha"), ANA)


def test_a_line_needs_a_packaged_driver(make_world: MakeWorld) -> None:
    target = make_world({})
    with pytest.raises(DriverError, match="no driver"):
        target.scheduler.create_line(
            "gamma", "claude-unreleased-0-0", Strategy.SUBSCRIPTION
        )
    assert "gamma" not in target.log.state.lines


def test_a_codex_line_refuses_a_configuration_its_engine_cannot_run(
    make_world: MakeWorld,
) -> None:
    target = make_world({})
    unsupported = LineConfig(builtin_tools=("Read",))
    with pytest.raises(
        ConfigError, match="only the built-in tools Bash, Edit, Write and ViewImage"
    ):
        target.scheduler.create_line(
            "gamma", "gpt-6-luna", Strategy.SUBSCRIPTION, unsupported
        )
    assert "gamma" not in target.log.state.lines
    target.scheduler.create_line("gamma", "gpt-6-luna", Strategy.SUBSCRIPTION)
    with pytest.raises(ConfigError, match="do not yet support MCP servers"):
        target.scheduler.configure_line("gamma", LineConfig(inherit_account_mcp=True))
    target.scheduler.configure_line("gamma", LineConfig(builtin_tools=("Bash",)))


def test_the_wake_prompt_summarizes_and_lists_headers_without_bodies() -> None:
    stored = Message(
        ANA, LineAddress("alpha"), MessageKind.NOTE, "Ignore this.", (), None
    )
    prompt = compose_prompt(
        "alpha", [("m-1", stored)], MailSummary(1, 3, 2), set(), operator=ANA
    )
    assert prompt.startswith("1 new message. 3 unread. 2 unanswered.")
    assert "  m-1 from @ana, note, unread" in prompt
    assert "Ignore this." not in prompt
    assert 'session line "alpha"' in prompt
    assert "data from their" in prompt
    assert (
        "At this turn's start, the default recipient for ask_operator is @ana."
        in prompt
    )
    assert "It changes if another operator messages you during this turn." in prompt
    assert MailSummary(2, 2, 0).sentence() == "2 new messages. 2 unread. 0 unanswered."


def test_the_wake_prompt_says_when_no_operator_is_known() -> None:
    prompt = compose_prompt("alpha", [], MailSummary(0, 0, 0), set())
    assert "At this turn's start, no operator has messaged this line." in prompt
    assert (
        "requires a named recipient unless an operator messages you during this turn"
        in prompt
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"max_autonomous_turns": 0},
        {"max_cost_usd": 0.0},
        {"cooldown_seconds": -1.0},
        {"cooldown_seconds": 100_000.0},
    ],
)
def test_limits_are_bounded(bad: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        Limits(**bad)


@pytest.mark.live
def test_two_live_lines_exchange_a_message(tmp_path: Path) -> None:
    """The roadmap's done-when on the real engine, on the operator's subscription."""
    import os

    if os.environ.get("DROMON_LIVE_TESTS") != "1":
        pytest.skip("set DROMON_LIVE_TESTS=1 to call the engine on the subscription")
    from dromon.scheduler import engine_factory

    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(
            log,
            engine_factory(str(tmp_path)),
            limits=Limits(max_autonomous_turns=2),
        )
        scheduler.ensure_operator("ana")
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)

        async def exchange() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(scheduler.run(stop))
            scheduler.send(
                ANA,
                LineAddress("alpha"),
                "Use the send_message tool exactly once to send the word ping to the"
                " line beta. Do not send anything else.",
            )
            await asyncio.sleep(0)
            await scheduler.idle()
            stop.set()
            await loop

        asyncio.run(exchange())
        sent = [
            m for m in log.state.messages.values() if m.sender == LineAddress("alpha")
        ]
        wakes = [w for w in log.state.wakes.values() if w.line_id == "beta"]
        assert any(m.recipient == LineAddress("beta") for m in sent)
        assert wakes and all(w.issued for w in wakes)
        assert all(line.state is LineState.IDLE for line in log.state.lines.values())


@pytest.mark.live
def test_a_live_luna_line_messages_a_haiku_line(tmp_path: Path) -> None:
    """Two engines in one scheduler, on the operator's ChatGPT plan and subscription."""
    import os

    if os.environ.get("DROMON_LIVE_TESTS") != "1":
        pytest.skip("set DROMON_LIVE_TESTS=1 to call the engine on the subscription")
    from dromon.scheduler import engine_factory

    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(
            log,
            engine_factory(str(tmp_path.resolve())),
            limits=Limits(max_autonomous_turns=2),
        )
        scheduler.ensure_operator("ana")
        scheduler.create_line("alpha", "gpt-6-luna", Strategy.SUBSCRIPTION)
        scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)

        async def exchange() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(scheduler.run(stop))
            scheduler.send(
                ANA,
                LineAddress("alpha"),
                "Use the send_message tool exactly once to send the word ping to the"
                " line beta. Do not send anything else.",
            )
            await asyncio.sleep(0)
            await scheduler.idle()
            stop.set()
            await loop

        asyncio.run(exchange())
        reports = dict(scheduler.turn_reports)
        sent = [
            m for m in log.state.messages.values() if m.sender == LineAddress("alpha")
        ]
        assert "alpha" in reports, "the Luna line did not complete a turn"
        assert any(m.recipient == LineAddress("beta") for m in sent)
        assert any(line_id == "beta" for line_id, _ in scheduler.turn_reports)
        # Both real adapters report their turns to the activity tail.
        luna = [kind for kind, _ in activity_of(scheduler, "alpha")]
        haiku = [kind for kind, _ in activity_of(scheduler, "beta")]
        assert ActivityKind.TOOL in luna and luna[-1] is ActivityKind.TURN_ENDED
        assert luna[0] is ActivityKind.TURN_STARTED
        assert haiku[0] is ActivityKind.TURN_STARTED
        assert ActivityKind.TEXT in haiku or ActivityKind.TOOL in haiku
        assert all(line.state is LineState.IDLE for line in log.state.lines.values())


def test_a_message_sent_in_a_tick_reaches_an_idle_line_only_in_the_next(
    make_world: MakeWorld,
) -> None:
    target = make_world(
        {
            "alpha": [[{"to": "beta", "body": "ping"}], []],
            "beta": [[{"to": "alpha", "body": "pong"}]],
        }
    )
    run(target, send(target, "alpha", "start"))
    events = target.events()
    alpha_idle = events.index(LineStateChanged("alpha", LineState.IDLE))
    beta_wake = next(
        i
        for i, e in enumerate(events)
        if isinstance(e, WakeRequested) and e.line_id == "beta"
    )
    ping = next(
        i
        for i, e in enumerate(events)
        if isinstance(e, MessageSent) and e.body == "ping"
    )
    # beta was idle when ping arrived, yet woke only in a later tick.
    assert ping < beta_wake
    assert alpha_idle < beta_wake


def test_each_line_takes_at_most_one_turn_per_tick(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[], []], "beta": [[], []]})

    async def act() -> None:
        target.scheduler.send(ANA, LineAddress("alpha"), "one")
        target.scheduler.send(ANA, LineAddress("beta"), "two")

    run(target, act)
    assert len(target.engines["alpha"].prompts) == 1
    assert len(target.engines["beta"].prompts) == 1
    events = target.events()
    starts = [i for i, e in enumerate(events) if isinstance(e, WakeRequested)]
    # Both lines started in one tick, one after the other in the line order.
    first, second = (events[i] for i in starts)
    assert isinstance(first, WakeRequested) and isinstance(second, WakeRequested)
    assert (first.line_id, second.line_id) == ("alpha", "beta")
    between = events[starts[0] : starts[1]]
    assert not any(
        isinstance(e, LineStateChanged) and e.state is LineState.IDLE for e in between
    )


def test_a_configuration_reaches_the_engine_and_a_change_keeps_the_session(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[], []]})
    first = LineConfig(builtin_tools=("Read",))
    target.scheduler.configure_line("alpha", first)
    run(target, send(target, "alpha", "one"))
    assert target.engines["alpha"].spec is not None
    assert target.engines["alpha"].spec.config == first
    session = target.engines["alpha"].session_id
    assert session is not None
    assert target.log.state.lines["alpha"].engine_session == session
    second = LineConfig(tool_policy=ToolPolicy.ALLOW_ALL)
    target.scheduler.configure_line("alpha", second)
    run(target, send(target, "alpha", "two"))
    rebuilt = target.engines["alpha"].spec
    assert rebuilt is not None
    assert (rebuilt.config, rebuilt.session_id) == (second, session)
    assert target.log.state.lines["alpha"].config == encode_config(second)


def test_a_line_created_with_a_configuration_records_it(make_world: MakeWorld) -> None:
    target = make_world({})
    config = LineConfig(builtin_tools=("Read",))
    target.scheduler.create_line("gamma", HAIKU, Strategy.SUBSCRIPTION, config)
    assert target.log.state.lines["gamma"].config == encode_config(config)
    target.scheduler.create_line("delta", HAIKU, Strategy.SUBSCRIPTION)
    assert target.log.state.lines["delta"].config is None


# Operator questions.

BEN = OperatorAddress("ben")


HEADER = re.compile(r"^  (\S+) from (\S+), ", re.MULTILINE)


def received(target: World, prompt: str) -> list[tuple[str, str]]:
    """The sender and body of each message a wake prompt announces, in order."""
    messages = target.log.state.messages
    return [
        (str(messages[message_id].sender), messages[message_id].body)
        for message_id, _ in HEADER.findall(prompt)
    ]


def bodies(target: World, prompt: str) -> list[str]:
    return [body for _, body in received(target, prompt)]


def ask_call(**args: object) -> dict[str, Any]:
    return {"tool": "ask_operator", **args}


def questions_for(target: World, operator: OperatorAddress | None = ANA) -> list[str]:
    return [question_id for question_id, _ in target.scheduler.questions(operator)]


def test_a_line_asks_and_the_question_waits_in_its_operators_queue(
    make_world: MakeWorld,
) -> None:
    target = make_world(
        {"alpha": [[ask_call(question="Proceed?", to="@ana", choices=["yes", "no"])]]}
    )
    target.scheduler.ensure_operator("ben")
    run(target, send(target, "alpha", "start"))
    asked = questions_for(target)
    assert len(asked) == 1
    question = target.log.state.messages[asked[0]]
    assert (question.sender, question.recipient) == (LineAddress("alpha"), ANA)
    assert (question.body, question.choices) == ("Proceed?", ("yes", "no"))
    assert questions_for(target, BEN) == []
    assert questions_for(target, None) == asked
    assert target.log.state.messages[asked[0]].delivered


def test_without_to_a_question_goes_to_the_operator_who_last_messaged(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[ask_call(question="Which one?")]]})
    target.scheduler.ensure_operator("ben")
    alpha = target.engine("alpha")
    gate = asyncio.Event()
    alpha.gate = gate

    async def act() -> None:
        target.scheduler.send(ANA, LineAddress("alpha"), "first")
        while not alpha.prompts:
            await asyncio.sleep(0)
        assert "default recipient for ask_operator is @ana" in alpha.prompts[0]
        target.scheduler.send(BEN, LineAddress("alpha"), "second")
        gate.set()

    run(target, act)
    assert questions_for(target, BEN) and not questions_for(target, ANA)


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"question": "?", "to": "alpha"}, "not an operator address"),
        ({"question": "?", "to": "@nobody"}, "no operator nobody"),
        ({"question": "?"}, "no operator has messaged this line"),
        ({"question": 3, "to": "@ana"}, "question must be text"),
        ({"question": "?", "to": "@ana", "choices": "yes"}, "choices must be a list"),
        ({"question": "?", "to": "@ana", "blocking": True}, "unexpected argument"),
        ({"question": "?", "to": "@ana", "choices": ["a", "a"]}, "repeat"),
    ],
)
def test_a_bad_question_returns_an_error_and_asks_nothing(
    make_world: MakeWorld, args: dict[str, Any], reason: str
) -> None:
    target = make_world({"alpha": []}, hold_wakes=True)
    result = asyncio.run(call_tool(target.scheduler.ask_tool("alpha"), args))
    assert result["is_error"] is True
    assert reason in result["content"][0]["text"]
    assert questions_for(target, None) == []


def test_an_answer_wakes_the_line_in_a_later_tick(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[ask_call(question="Proceed?", to="@ana")], []]})
    run(target, send(target, "alpha", "start"))
    (question_id,) = questions_for(target)

    async def act() -> None:
        target.scheduler.answer(ANA, question_id, "yes")

    run(target, act)
    prompts = target.engines["alpha"].prompts
    assert len(prompts) == 2
    assert f"from @ana, note, replying to {question_id}, unread" in prompts[1]
    assert bodies(target, prompts[1]) == ["yes"]
    question = target.log.state.messages[question_id]
    assert question.status is QuestionStatus.ANSWERED
    assert questions_for(target) == []


def test_asking_and_ending_the_turn_lets_the_answer_arrive_as_mail(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[ask_call(question="Wait?", to="@ana")], []]})
    run(target, send(target, "alpha", "start"))
    (question_id,) = questions_for(target)
    assert len(target.engines["alpha"].prompts) == 1

    async def act() -> None:
        target.scheduler.answer(ANA, question_id, "go")

    run(target, act)
    prompts = target.engines["alpha"].prompts
    assert len(prompts) == 2
    assert f"replying to {question_id}" in prompts[1]
    assert bodies(target, prompts[1]) == ["go"]


def test_only_the_asked_operator_may_answer_and_only_once(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[ask_call(question="?", to="@ana")]]})
    target.scheduler.ensure_operator("ben")
    run(target, send(target, "alpha", "start"))
    (question_id,) = questions_for(target)
    with pytest.raises(EventRejected, match="must go from @ana"):
        target.scheduler.answer(BEN, question_id, "no")
    target.scheduler.answer(ANA, question_id, "yes")
    with pytest.raises(EventRejected, match="is answered"):
        target.scheduler.answer(ANA, question_id, "again")
    with pytest.raises(EventRejected, match="no question"):
        target.scheduler.answer(ANA, "q-missing", "x")


def test_an_open_question_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite"
    with EventLog(path) as log:
        first = Scheduler(log, World.build_nothing, tick_seconds=0)
        first.ensure_operator("ana")
        first.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        question_id = first.ask("alpha", ANA, "Still there?")
    with EventLog(path) as log:
        restarted = Scheduler(log, World.build_nothing, tick_seconds=0)
        assert [i for i, _ in restarted.questions(ANA)] == [question_id]


def test_a_restarted_scheduler_resumes_each_lines_engine_session(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[], []]})
    run(target, send(target, "alpha", "one"))
    session = target.log.state.lines["alpha"].engine_session
    assert session is not None
    target.engines.clear()
    target.scheduler = Scheduler(target.log, target.build, tick_seconds=0)
    run(target, send(target, "alpha", "two"))
    rebuilt = target.engines["alpha"].spec
    assert rebuilt is not None and rebuilt.session_id == session


def test_an_unchanged_session_is_not_recorded_again(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[], []]})
    run(target, send(target, "alpha", "one"))
    run(target, send(target, "alpha", "two"))
    recorded = [e for e in target.events() if isinstance(e, LineSession)]
    assert [e.engine_session for e in recorded] == ["session-1"]


def test_a_changed_session_is_recorded(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[], []]})
    engine = target.engine("alpha")
    run(target, send(target, "alpha", "one"))
    engine.session_id = "session-2"
    run(target, send(target, "alpha", "two"))
    recorded = [e for e in target.events() if isinstance(e, LineSession)]
    assert [e.engine_session for e in recorded] == ["session-1", "session-2"]


@pytest.mark.live
def test_a_live_line_asks_and_is_woken_by_the_answer(tmp_path: Path) -> None:
    """Roadmap task 6 on the real engine, on the operator's subscription."""
    import os

    if os.environ.get("DROMON_LIVE_TESTS") != "1":
        pytest.skip("set DROMON_LIVE_TESTS=1 to call the engine on the subscription")
    from dromon.scheduler import engine_factory

    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(
            log,
            engine_factory(str(tmp_path)),
            limits=Limits(max_autonomous_turns=2),
        )
        scheduler.ensure_operator("ana")
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

        async def settle_once(act: Callable[[], object]) -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(scheduler.run(stop))
            act()
            await asyncio.sleep(0)
            await scheduler.idle()
            stop.set()
            await loop

        asyncio.run(
            settle_once(
                lambda: scheduler.send(
                    ANA,
                    LineAddress("alpha"),
                    "Call the ask_operator tool once, to @ana, asking: Proceed?, then"
                    " end your turn. Send nothing else.",
                )
            )
        )
        asked = scheduler.questions(ANA)
        assert len(asked) == 1
        question_id = asked[0][0]
        asyncio.run(settle_once(lambda: scheduler.answer(ANA, question_id, "yes")))
        assert log.state.messages[question_id].status is QuestionStatus.ANSWERED
        assert len(scheduler.turn_reports) == 2


def test_a_busy_line_is_skipped_while_others_proceed(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[{"to": "beta", "body": "ping"}]], "beta": [[], []]})
    alpha = target.engine("alpha")
    gate = asyncio.Event()
    alpha.gate = gate

    async def act() -> None:
        target.scheduler.send(ANA, LineAddress("alpha"), "slow work")
        target.scheduler.send(ANA, LineAddress("beta"), "quick work")
        while len(target.engine("beta").prompts) < 1:
            await asyncio.sleep(0)
        for _ in range(50):
            await asyncio.sleep(0)
        # alpha is still in its turn, yet ticks went on and beta finished its turn.
        assert target.log.state.lines["alpha"].state is LineState.RUNNING
        assert target.log.state.lines["beta"].state is LineState.IDLE
        gate.set()

    run(target, act)
    assert target.scheduler.tick > 2
    assert len(target.engines["beta"].prompts) == 2
    assert received(target, target.engines["beta"].prompts[1]) == [("alpha", "ping")]


def test_mail_sent_mid_turn_is_delivered_while_the_sender_still_works(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[]], "beta": [[]]})
    alpha = target.engine("alpha")
    gate = asyncio.Event()
    alpha.gate = gate

    async def act() -> None:
        target.scheduler.send(ANA, LineAddress("alpha"), "start")
        while not alpha.prompts:
            await asyncio.sleep(0)
        target.scheduler.send(LineAddress("alpha"), LineAddress("beta"), "early news")
        while not target.engine("beta").prompts:
            await asyncio.sleep(0)
        assert target.log.state.lines["alpha"].state is LineState.RUNNING
        gate.set()

    run(target, act)
    assert bodies(target, target.engines["beta"].prompts[0]) == ["early news"]


def test_the_tick_cadence_must_be_bounded(tmp_path: Path) -> None:
    with EventLog(tmp_path / "events.sqlite") as log:
        for bad in (-1.0, 61.0):
            with pytest.raises(ValueError, match="tick_seconds"):
                Scheduler(log, World.build_nothing, tick_seconds=bad)


# Mailboxes and the mail tools.


def mail_call(tool: str, **args: object) -> dict[str, Any]:
    return {"tool": tool, **args}


def tool_text(result: dict[str, Any]) -> str:
    return str(result["content"][0]["text"])


def test_a_line_lists_and_reads_its_mail_and_reading_marks_it(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[mail_call("list_mail", unread_only=True)]]})

    async def act() -> None:
        target.scheduler.send(ANA, LineAddress("alpha"), "first")
        target.scheduler.send(ANA, LineAddress("alpha"), "second")

    run(target, act)
    engine = target.engines["alpha"]
    listing_text = tool_text(engine.results[0])
    assert listing_text.startswith("2 of 2 matching messages, from 0.")
    first_id = next(
        i for i, m in target.log.state.messages.items() if m.body == "first"
    )
    read = asyncio.run(
        call_tool(target.scheduler.read_mail_tool("alpha"), {"message_id": first_id})
    )
    assert tool_text(read).endswith("\n\nfirst")
    assert target.log.state.messages[first_id].read
    again = asyncio.run(
        call_tool(target.scheduler.list_mail_tool("alpha"), {"unread_only": True})
    )
    assert tool_text(again).startswith("1 of 1 matching messages")


def test_reading_twice_records_one_read(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    run(target, send(target, "alpha", "hello"))
    (message_id,) = [
        i for i, m in target.log.state.messages.items() if m.body == "hello"
    ]
    tool = target.scheduler.read_mail_tool("alpha")
    asyncio.run(call_tool(tool, {"message_id": message_id}))
    asyncio.run(call_tool(tool, {"message_id": message_id}))
    reads = [e for e in target.events() if isinstance(e, MessageRead)]
    assert reads == [MessageRead(message_id)]


def test_a_line_can_read_its_own_sent_mail_but_not_another_lines_sent_mail(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[]], "beta": [[]]})
    alpha = LineAddress("alpha")
    beta = LineAddress("beta")
    sent = target.scheduler.send(alpha, ANA, "sent by alpha")
    other = target.scheduler.send(beta, ANA, "sent by beta")
    tool = target.scheduler.read_mail_tool("alpha")

    opened = asyncio.run(call_tool(tool, {"message_id": sent}))
    assert "sent by alpha" in tool_text(opened)
    assert not target.log.state.messages[sent].read
    assert not any(
        isinstance(event, MessageRead) and event.message_id == sent
        for event in target.events()
    )

    refused = asyncio.run(call_tool(tool, {"message_id": other}))
    assert refused.get("isError", refused.get("is_error")) is True
    assert "no message" in tool_text(refused)


def test_mail_can_be_paged_and_filtered_by_sender(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]], "beta": []}, hold_wakes=True)
    target.scheduler.ensure_operator("ben")
    for n in range(5):
        target.scheduler.send(ANA, LineAddress("alpha"), f"ana {n}")
    target.scheduler.send(BEN, LineAddress("alpha"), "ben 0")
    for message_id, message in list(target.log.state.messages.items()):
        if message.recipient == LineAddress("alpha"):
            target.log.append(MessageDelivered(message_id))
    tool = target.scheduler.list_mail_tool("alpha")
    page = tool_text(asyncio.run(call_tool(tool, {"offset": 2, "limit": 2})))
    assert page.startswith("2 of 6 matching messages, from 2.")
    only_ben = tool_text(asyncio.run(call_tool(tool, {"sender": "@ben"})))
    assert only_ben.startswith("1 of 1 matching messages")


@pytest.mark.parametrize(
    ("tool", "args", "reason"),
    [
        ("list_mail", {"limit": 0}, "limit"),
        ("list_mail", {"limit": 51}, "limit"),
        ("list_mail", {"offset": -1}, "offset"),
        ("list_mail", {"offset": "1"}, "integers"),
        ("list_mail", {"unread_only": 1}, "unread_only"),
        ("list_mail", {"sender": "Bad Name"}, "identifier"),
        ("list_mail", {"folder": "x"}, "unexpected argument folder"),
        ("read_mail", {"message_id": "m-missing"}, "no message m-missing"),
        ("read_mail", {"message_id": 3}, "message_id must be text"),
    ],
)
def test_a_bad_mail_call_returns_an_error(
    make_world: MakeWorld, tool: str, args: dict[str, Any], reason: str
) -> None:
    target = make_world({"alpha": []}, hold_wakes=True)
    builders = {
        "list_mail": target.scheduler.list_mail_tool,
        "read_mail": target.scheduler.read_mail_tool,
    }
    result = asyncio.run(call_tool(builders[tool]("alpha"), args))
    assert result["is_error"] is True
    assert reason in tool_text(result)


def test_a_line_cannot_read_another_mailbox(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]], "beta": []})
    run(target, send(target, "alpha", "private"))
    (message_id,) = [
        i for i, m in target.log.state.messages.items() if m.body == "private"
    ]
    result = asyncio.run(
        call_tool(target.scheduler.read_mail_tool("beta"), {"message_id": message_id})
    )
    assert result["is_error"] is True
    assert not target.log.state.messages[message_id].read


def test_doing_nothing_with_mail_is_valid(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[], []]})
    run(target, send(target, "alpha", "one"))
    run(target, send(target, "alpha", "two"))
    second = target.engines["alpha"].prompts[1]
    # The first message was neither read nor answered, and nothing forced either.
    assert second.splitlines()[1] == "1 new message. 2 unread. 2 unanswered."


# Acknowledgements and sent mail.


def test_an_acknowledgement_answers_without_a_reply_and_the_sender_sees_it(
    make_world: MakeWorld,
) -> None:
    target = make_world({"alpha": [[]], "beta": [[]]}, hold_wakes=True)
    alpha, beta = LineAddress("alpha"), LineAddress("beta")
    acked = target.scheduler.send(alpha, beta, "fyi")
    replied = target.scheduler.send(alpha, beta, "a question")
    waiting = target.scheduler.send(alpha, beta, "another")
    for message_id in (acked, replied, waiting):
        target.log.append(MessageDelivered(message_id))
    result = asyncio.run(
        call_tool(target.scheduler.acknowledge_mail_tool("beta"), {"message_id": acked})
    )
    assert tool_text(result) == f"acknowledged {acked}"
    target.scheduler.send(beta, alpha, "the answer", reply_to=replied)
    sent_text = tool_text(
        asyncio.run(call_tool(target.scheduler.list_sent_tool("alpha"), {}))
    )
    assert sent_text.splitlines() == [
        "3 of 3 sent messages, from 0.",
        f"{acked} to beta, note, delivered, acknowledged",
        f"{replied} to beta, note, delivered, replied",
        f"{waiting} to beta, note, delivered, unanswered",
    ]
    only = tool_text(
        asyncio.run(
            call_tool(
                target.scheduler.list_sent_tool("alpha"), {"unanswered_only": True}
            )
        )
    )
    assert only.splitlines()[1:] == [f"{waiting} to beta, note, delivered, unanswered"]
    counts = summary(target.log.state, beta, 0)
    assert counts.unanswered == 1


def test_acknowledging_does_not_close_a_question(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[ask_call(question="?", to="@ana")]]})
    run(target, send(target, "alpha", "start"))
    (question_id,) = questions_for(target)
    target.scheduler.acknowledge(ANA, question_id)
    assert target.log.state.messages[question_id].acknowledged
    assert questions_for(target) == [question_id]


@pytest.mark.parametrize(
    ("tool", "args", "reason"),
    [
        ("acknowledge_mail", {"message_id": "m-missing"}, "no message m-missing"),
        ("acknowledge_mail", {"message_id": 1}, "message_id must be text"),
        ("acknowledge_mail", {"message_id": "x", "why": "y"}, "unexpected argument"),
        ("list_sent", {"limit": 0}, "limit"),
        ("list_sent", {"unanswered_only": "yes"}, "unanswered_only"),
    ],
)
def test_a_bad_acknowledgement_or_sent_call_returns_an_error(
    make_world: MakeWorld, tool: str, args: dict[str, Any], reason: str
) -> None:
    target = make_world({"alpha": []}, hold_wakes=True)
    builders = {
        "acknowledge_mail": target.scheduler.acknowledge_mail_tool,
        "list_sent": target.scheduler.list_sent_tool,
    }
    result = asyncio.run(call_tool(builders[tool]("alpha"), args))
    assert result["is_error"] is True
    assert reason in tool_text(result)


def test_a_line_cannot_acknowledge_another_mailbox(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]], "beta": []})
    run(target, send(target, "alpha", "private"))
    (message_id,) = [
        i for i, m in target.log.state.messages.items() if m.body == "private"
    ]
    with pytest.raises(EventRejected, match="no message"):
        target.scheduler.acknowledge(LineAddress("beta"), message_id)


# Activity and interrupts, for attached clients.


def activity(target: World, line_id: str) -> list[tuple[ActivityKind, str]]:
    return activity_of(target.scheduler, line_id)


def test_each_turn_is_bracketed_in_the_line_activity(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    run(target, send(target, "alpha", "hello"))
    recorded = activity(target, "alpha")
    assert [kind for kind, _ in recorded] == [
        ActivityKind.TURN_STARTED,
        ActivityKind.TURN_ENDED,
    ]
    assert recorded[0][1] == target.engines["alpha"].prompts[0]
    assert recorded[1][1] == "success"


def test_a_failed_turn_says_why_in_the_activity(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    target.engine("alpha").fail = True
    run(target, send(target, "alpha", "hello"))
    assert activity(target, "alpha")[-1] == (
        ActivityKind.TURN_ENDED,
        "failed: the engine broke",
    )


def test_an_operator_interrupts_a_turn_in_flight(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    engine = target.engine("alpha")
    engine.gate = asyncio.Event()
    outcomes: list[bool] = []

    async def act() -> None:
        assert target.scheduler.interrupt("alpha") is False
        target.scheduler.send(ANA, LineAddress("alpha"), "long work")
        while not engine.prompts:
            await asyncio.sleep(0)
        outcomes.append(target.scheduler.interrupt("alpha"))
        for _ in range(5):
            await asyncio.sleep(0)
        outcomes.append(target.scheduler.interrupt("alpha"))

    run(target, act)
    assert outcomes == [True, False]
    assert target.log.state.lines["alpha"].state is LineState.IDLE
    assert target.scheduler.turn_reports == []
    assert activity(target, "alpha")[-1] == (
        ActivityKind.TURN_ENDED,
        "interrupted by an operator",
    )
    with pytest.raises(EventRejected, match="no line"):
        target.scheduler.interrupt("nobody")


# Settings an operator changes from a client.


def test_a_line_override_reaches_its_next_engine(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    first = target.engine("alpha")
    target.scheduler.override_line("alpha", "effort", "high")
    target.scheduler.override_line("alpha", "builtin_tools", ["Read"])
    rebuilt = target.engine("alpha")
    assert rebuilt is not first and rebuilt.spec is not None
    assert rebuilt.spec.config.effort == "high"
    assert rebuilt.spec.config.builtin_tools == ("Read",)
    assert target.scheduler.baseline_config("alpha") == LineConfig()
    target.scheduler.override_line("alpha", "effort", None)
    assert target.scheduler.effective_config("alpha").effort is None
    assert target.scheduler.line_overrides("alpha") == {"builtin_tools": ("Read",)}


def test_a_line_switches_models_only_within_its_engine(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    assert target.scheduler.model_choices("alpha") == (
        "claude-opus-5-5",
        "claude-haiku-5-5",
        "claude-fable-5-1",
        "claude-sonnet-5-5",
    )
    target.scheduler.override_line("alpha", "model", "claude-opus-5-5")
    assert target.scheduler.model_of("alpha") == "claude-opus-5-5"
    spec = target.engine("alpha").spec
    assert spec is not None and spec.model == "claude-opus-5-5"
    (summary,) = target.scheduler.line_summaries()
    assert summary["engine"] == "claude-opus-5-5"
    with pytest.raises(OverrideError, match="create a new line"):
        target.scheduler.override_line("alpha", "model", "gpt-6-luna")
    assert target.scheduler.model_of("alpha") == "claude-opus-5-5"


def test_limit_and_wake_overrides_sit_over_the_project(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    scheduler = target.scheduler
    scheduler.override_line("alpha", "limits.max_cost_usd", "9")
    scheduler.override_line("alpha", "wake.every_ticks", 4)
    assert scheduler.limits_for("alpha").max_cost_usd == 9.0
    assert scheduler.limits_for("alpha").max_autonomous_turns == (
        DEFAULT_LIMITS.max_autonomous_turns
    )
    assert scheduler.wake_every_for("alpha") == 4
    with pytest.raises(OverrideError, match="every_ticks"):
        scheduler.override_line("alpha", "wake.every_ticks", 0)
    with pytest.raises(ValueError, match="max_cost_usd"):
        scheduler.override_line("alpha", "limits.max_cost_usd", "0")
    assert scheduler.limits_for("alpha").max_cost_usd == 9.0


def test_refused_overrides_change_nothing(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    scheduler = target.scheduler
    before = len(target.events())
    for key, value in (
        ("effort", "extreme"),
        ("colour", "red"),
        ("builtin_tools", ["x y"]),
    ):
        with pytest.raises((OverrideError, ConfigError)):
            scheduler.override_line("alpha", key, value)
    with pytest.raises(EventRejected, match="no line"):
        scheduler.override_line("nobody", "effort", "low")
    assert len(target.events()) == before


def test_a_stored_override_that_no_longer_applies_is_skipped(
    make_world: MakeWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    target = make_world({"alpha": [[]]})
    target.log.append(LineOverridden("alpha", '{"effort":"extreme","colour":1}'))
    assert target.scheduler.effective_config("alpha") == LineConfig()
    target.scheduler.line_summaries()
    target.scheduler.line_summaries()
    reported = capsys.readouterr().err
    assert reported.count("colour") == 1 and "an override is skipped" in reported


def test_clearing_a_session_starts_the_next_turn_fresh(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[], []]})
    run(target, send(target, "alpha", "first"))
    assert target.log.state.lines["alpha"].engine_session == "session-1"
    target.scheduler.clear_session("alpha")
    assert target.log.state.lines["alpha"].engine_session is None
    spec = target.engine("alpha").spec
    assert spec is not None and spec.session_id is None
    with pytest.raises(EventRejected, match="no line"):
        target.scheduler.clear_session("nobody")


def test_a_session_is_not_cleared_mid_turn(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    engine = target.engine("alpha")
    gate = asyncio.Event()
    engine.gate = gate
    refused: list[str] = []

    async def act() -> None:
        target.scheduler.send(ANA, LineAddress("alpha"), "work")
        while not engine.prompts:
            await asyncio.sleep(0)
        with pytest.raises(EventRejected, match="Interrupt it first") as caught:
            target.scheduler.clear_session("alpha")
        refused.append(str(caught.value))
        gate.set()

    run(target, act)
    assert refused


def test_project_values_change_while_running(make_world: MakeWorld) -> None:
    target = make_world({"alpha": [[]]})
    scheduler = target.scheduler
    scheduler.set_project(Limits(3, 1.0, 0.0), "ben", 0.5, 2)
    assert scheduler.limits_for("alpha") == Limits(3, 1.0, 0.0)
    assert scheduler.wake_every_for("alpha") == 2
    with pytest.raises(ValueError, match="wake_every_ticks"):
        scheduler.set_project(DEFAULT_LIMITS, "ben", 0.5, 0)
    with pytest.raises(ValueError, match="tick_seconds"):
        scheduler.set_project(DEFAULT_LIMITS, "ben", 99.0, 1)


class Faulty(Exception):
    """An exception no rule of the scheduler expects."""


def test_the_watch_keeps_a_faulty_scheduler_running(
    make_world: MakeWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(scheduler_module, "FIRST_FAULT_PAUSE_SECONDS", 0.0)
    world = make_world({"alpha": [[]]})
    original = world.scheduler.step
    raised: list[int] = []

    def broken() -> None:
        if not raised:
            raised.append(1)
            raise Faulty("a bug in a tick")
        original()

    monkeypatch.setattr(world.scheduler, "step", broken)
    run(world, send(world, "alpha", "hello"))
    # The tick after the fault ran the turn, and its completion cleared the fault.
    assert raised == [1] and "alpha" in world.engines
    assert world.scheduler.fault_status() == {"fault": None, "faults": 1}
    err = capsys.readouterr().err
    assert "the scheduler failed unexpectedly, fault 1" in err
    assert "Traceback" in err and "a bug in a tick" in err


def test_the_schedulers_pause_doubles_to_its_bound(make_world: MakeWorld) -> None:
    world = make_world({})

    async def pauses() -> list[float]:
        stop = asyncio.Event()
        stop.set()
        found: list[float] = []
        for _ in range(8):
            await world.scheduler._fault_found(Faulty("x"), stop)  # pyright: ignore[reportPrivateUsage]
            found.append(world.scheduler._fault_pause)  # pyright: ignore[reportPrivateUsage]
        return found

    assert asyncio.run(pauses()) == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]
    assert scheduler_module.MAX_FAULT_PAUSE_SECONDS == 60.0
    assert world.scheduler.fault_status() == {"fault": "Faulty: x", "faults": 8}


def test_dromon_status_prints_the_schedulers_fault_only_while_it_lasts() -> None:
    from dromon import cli

    row = cli._scheduler_row  # pyright: ignore[reportPrivateUsage]
    assert row({"fault": None, "faults": 3}) is None
    assert row({"fault": "Faulty: x", "faults": 3}) == (
        "scheduler fault Faulty: x, 3 since the daemon started"
    )
