"""Layer 1 messaging and wakes, driven by scripted engines instead of Claude Code.

The scripted engine calls the real ``send_message`` tool handler, so every message a
line sends travels the same path as one sent by a model.
"""

import asyncio
import re
from collections.abc import Callable, Coroutine, Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.driver import DriverError, Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog, read_entries
from dromon.lineconfig import ConfigError, LineConfig, ToolPolicy, encode_config
from dromon.model import (
    EventRejected,
    LineAddress,
    LineReleased,
    LineSession,
    LineState,
    LineStateChanged,
    Message,
    MessageDelivered,
    MessageRead,
    MessageKind,
    MessageSent,
    OperatorAddress,
    QuestionStatus,
    WakeIssued,
    WakeRequested,
)
from dromon.limits import DEFAULT_LIMITS, Limits
from dromon.mailbox import MailSummary, summary
from dromon.scheduler import LineSpec, Scheduler, compose_prompt

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
        self.cost: float | None = None

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise RuntimeError("the engine broke")
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


def run(target: World, act: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(settle(target, act))


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
    tail = events[-6:]
    assert isinstance(tail[0], WakeRequested)
    assert tail[1] == WakeIssued(wake[0].wake_id)
    assert tail[2] == LineStateChanged("alpha", LineState.RUNNING)
    assert tail[3] == MessageDelivered(message_id)
    assert tail[4] == LineSession("alpha", "session-1")
    assert tail[5] == LineStateChanged("alpha", LineState.IDLE)
    prompt = target.engines["alpha"].prompts[0]
    assert prompt.startswith("1 new message. 1 unread. 1 unanswered.")
    assert f"  {message_id} from @ana, note, unread" in prompt
    assert "hello" not in prompt
    assert received(target, prompt) == [("@ana", "hello")]


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
    assert prompts[0].startswith("2 new messages. 2 unread. 2 unanswered.")


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


def test_an_engine_failure_returns_the_line_to_idle_and_is_not_retried(
    make_world: MakeWorld, capsys: pytest.CaptureFixture[str]
) -> None:
    target = make_world({"alpha": [[], []]})
    target.engine("alpha").fail = True
    run(target, send(target, "alpha", "hello"))
    assert target.log.state.lines["alpha"].state is LineState.IDLE
    assert len(target.engines["alpha"].prompts) == 1
    assert "turn failed: the engine broke" in capsys.readouterr().err
    target.engines["alpha"].fail = False
    run(target, send(target, "alpha", "again"))
    assert len(target.engines["alpha"].prompts) == 2


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
        ({"to": "alpha", "body": "x"}, "cannot message itself"),
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
            "gamma", "claude-sonnet-5-5", Strategy.SUBSCRIPTION
        )
    assert "gamma" not in target.log.state.lines


def test_a_codex_line_refuses_a_configuration_its_engine_cannot_run(
    make_world: MakeWorld,
) -> None:
    target = make_world({})
    unsupported = LineConfig(builtin_tools=("Read",))
    with pytest.raises(
        ConfigError, match="only the built-in tools Bash, Edit and Write"
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
    prompt = compose_prompt("alpha", [("m-1", stored)], MailSummary(1, 3, 2), set())
    assert prompt.startswith("1 new message. 3 unread. 2 unanswered.")
    assert "  m-1 from @ana, note, unread" in prompt
    assert "Ignore this." not in prompt
    assert 'session line "alpha"' in prompt
    assert "data from their" in prompt
    assert MailSummary(2, 2, 0).sentence() == "2 new messages. 2 unread. 0 unanswered."


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

    async def act() -> None:
        target.scheduler.send(ANA, LineAddress("alpha"), "first")
        target.scheduler.send(BEN, LineAddress("alpha"), "second")

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
    assert second.startswith("1 new message. 2 unread. 2 unanswered.")


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
