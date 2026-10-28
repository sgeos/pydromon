"""Round 6 of the session-line experience: ``compact_session`` schedules its own wake,
the prompt's context warning is computed after a compaction, a task note states its
exit code on its own, the opt-in automatic compaction, a compaction's and an
interrupted turn's own costs in ``recent_turns``, and token usage and compaction on
Codex lines."""

import asyncio
from collections import deque
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Generator,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    SdkMcpTool,
    SystemMessage,
    TaskNotificationMessage,
    TextBlock,
    AssistantMessage,
)

from dromon import codex as codex_module
from dromon.codex import (
    CODEX_USAGE_NOTE,
    COMPACT_START,
    COMPACTION_ITEM,
    THREAD_COMPACTED,
    TOKEN_USAGE_UPDATED,
    CodexLine,
    Connection,
    unsupported,
)
from dromon.driver import Driver, Strategy
from dromon.engine import (
    COMPACT_COMMAND,
    INTERRUPTED_KIND,
    SELF_STARTED_KIND,
    ClaudeCodeLine,
    EngineRefused,
    KeptSession,
    TurnReport,
    compaction_note,
    end_details,
)
from dromon.eventlog import EventLog
from dromon.limits import Limits
from dromon.lineconfig import (
    ConfigError,
    LineConfig,
    config_from_document,
    decode_config,
    encode_config,
)
from dromon.model import LineAddress, OperatorAddress
from dromon.scheduler import (
    COMPACT_WAKE_NOTE,
    DEFAULT_COMPACT_WAKE_SECONDS,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
LUNA = "gpt-6-luna"
ANA = OperatorAddress("ana")
START = datetime(2026, 10, 26, 9, 0, 0, tzinfo=UTC)
WARNING = "Your context is about"


# The scheduler, with engines that record what they are asked.


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class Engine:
    """A line's engine that cannot compact, reporting a fixed context."""

    def __init__(self, spec: LineSpec, world: "World") -> None:
        self.spec = spec
        self.world = world
        self.session_id = spec.session_id
        self.prompts: list[str] = []

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        return TurnReport(
            "success",
            1,
            "ok",
            (),
            None,
            None,
            "session-1",
            None,
            0.125,
            context_tokens=self.world.context,
            compaction=self.world.compaction,
            compaction_cost_usd=self.world.compaction_cost,
        )


class Compacting(Engine):
    """An engine that compacts as asked, to ``world.after`` tokens, and records the
    reason and the context line it was given, read after the compaction."""

    def __init__(self, spec: LineSpec, world: "World") -> None:
        super().__init__(spec, world)
        self.reasons: list[str] = []
        self.lines: list[str | None] = []
        self.pending: Callable[[], str | None] | None = None

    def request_compaction(
        self, reason: str, context_line: Callable[[], str | None]
    ) -> None:
        self.reasons.append(reason)
        self.pending = context_line

    async def run_turn(self, prompt: str) -> TurnReport:
        if self.pending is not None:
            self.spec.compacted(
                {
                    "trigger": "manual",
                    "before_tokens": self.world.context,
                    "after_tokens": self.world.after,
                }
            )
            self.lines.append(self.pending())
            self.pending = None
            self.world.context = self.world.after
        return await super().run_turn(prompt)


class World:
    def __init__(self, path: Path, compacting: bool = True) -> None:
        self.log = EventLog(path)
        self.clock = Clock()
        self.compacting = compacting
        self.context: int | None = None
        self.after: int | None = 5_000
        self.compaction: Mapping[str, int | str | None] | None = None
        self.compaction_cost: float | None = None
        self.engines: dict[str, Engine] = {}
        self.scheduler = Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            wall_clock=self.clock,
            limits=Limits(max_autonomous_turns=20, max_cost_usd=1.0),
        )
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Engine:
        del tools
        kind = Compacting if self.compacting else Engine
        engine = kind(spec, self)
        self.engines[spec.line_id] = engine
        return engine

    @property
    def engine(self) -> Engine:
        return self.engines["alpha"]

    def run(self, *bodies: str) -> None:
        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            for body in bodies:
                self.scheduler.send(ANA, LineAddress("alpha"), body)
            await asyncio.sleep(0)
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(go())

    def status(self) -> dict[str, Any]:
        return self.scheduler.line_status("alpha")

    def tool(self, name: str, line_id: str = "alpha") -> SdkMcpTool[Any]:
        return next(t for t in self.scheduler.own_tools(line_id) if t.name == name)

    def call(
        self, name: str, args: dict[str, Any], line_id: str = "alpha"
    ) -> dict[str, Any]:
        return invoke(self.tool(name, line_id), args)


def invoke(tool: SdkMcpTool[Any], args: dict[str, Any]) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        return await tool.handler(args)

    return asyncio.run(go())


def text_of(result: dict[str, Any]) -> str:
    return str(result["content"][0]["text"])


def compactable(world: World, **keys: object) -> None:
    world.scheduler.configure_line(
        "alpha",
        config_from_document(
            {"allow_compact": True, "context_warn_tokens": 1_000_000, **keys}
        ),
    )


def context_at_warning(world: World) -> None:
    """Give direct tool calls a context estimate that satisfies its precondition."""
    warning = world.scheduler.effective_config("alpha").context_warn_tokens
    world.scheduler._context["alpha"] = warning  # pyright: ignore[reportPrivateUsage]


@pytest.fixture
def worlds(tmp_path: Path) -> Generator[Callable[..., World]]:
    """Builds worlds whose event logs are closed after the test, since an unclosed
    SQLite connection warns on Python 3.15 and the gate makes warnings errors."""
    built: list[World] = []

    def build(**keys: bool) -> World:
        target = World(tmp_path / f"events-{len(built)}.sqlite", **keys)
        built.append(target)
        return target

    yield build
    for target in built:
        target.log.close()


@pytest.fixture
def world(worlds: Callable[..., World]) -> World:
    return worlds()


# A. compact_session schedules its own wake.


def test_compact_session_schedules_a_wake_a_minute_ahead_by_default(
    world: World,
) -> None:
    compactable(world)
    context_at_warning(world)
    result = world.call("compact_session", {})
    assert "is_error" not in result
    pending = world.log.state.scheduled["alpha"]
    due = START + timedelta(seconds=DEFAULT_COMPACT_WAKE_SECONDS)
    assert pending.due == due.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert pending.message_id in text_of(result)
    assert pending.note == COMPACT_WAKE_NOTE
    assert world.status()["context"]["compaction_pending"] is True


def test_compact_session_takes_the_seconds_to_wait(world: World) -> None:
    compactable(world)
    context_at_warning(world)
    world.call("compact_session", {"seconds": 300})
    due = START + timedelta(seconds=300)
    assert world.log.state.scheduled["alpha"].due == due.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_compact_session_keeps_a_wake_already_pending(world: World) -> None:
    compactable(world)
    context_at_warning(world)
    world.call("wake_after", {"seconds": 3600, "note": "check the build"})
    before = world.log.state.scheduled["alpha"]
    result = world.call("compact_session", {"seconds": 60})
    assert "stays" in text_of(result)
    assert world.log.state.scheduled["alpha"] == before
    assert world.status()["context"]["compaction_pending"] is True


@pytest.mark.parametrize("seconds", [59, 86_401, "60", True, 60.0])
def test_compact_session_refuses_bad_seconds_and_asks_nothing(
    world: World, seconds: object
) -> None:
    compactable(world)
    result = world.call("compact_session", {"seconds": seconds})
    assert result.get("is_error") is True
    assert "seconds must be a whole number" in text_of(result)
    assert "alpha" not in world.log.state.scheduled
    assert world.status()["context"]["compaction_pending"] is False


def test_compact_session_refuses_an_unknown_argument(world: World) -> None:
    compactable(world)
    result = world.call("compact_session", {"when": 60})
    assert result.get("is_error") is True
    assert "alpha" not in world.log.state.scheduled


def test_compact_session_refused_once_withdrawn_schedules_no_wake(
    world: World,
) -> None:
    compactable(world)
    tool = world.tool("compact_session")
    world.scheduler.configure_line("alpha", LineConfig())
    result = invoke(tool, {})
    assert result.get("is_error") is True
    assert "alpha" not in world.log.state.scheduled


def test_the_wake_starts_the_compacted_turn_without_other_mail(world: World) -> None:
    compactable(world)
    world.run("start")
    context_at_warning(world)
    world.call("compact_session", {})
    world.clock.now = START + timedelta(seconds=61)
    world.run()
    engine = world.engine
    assert isinstance(engine, Compacting)
    # The second turn came from the wake alone, and began with the compaction.
    assert len(engine.prompts) == 2 and len(engine.reasons) == 1
    assert "from alpha, note" in engine.prompts[-1]


# B. The context warning is computed after the compaction.


def test_a_compacted_turn_is_not_told_to_compact_again(world: World) -> None:
    compactable(world)
    world.context = 1_000_000
    context_at_warning(world)
    world.run("grow", "and grow")
    world.call("compact_session", {})
    world.run("work")
    engine = world.engine
    assert isinstance(engine, Compacting)
    assert WARNING not in engine.prompts[-1]
    # The line read after the compaction has no warning to give.
    assert engine.lines == [None]


def test_a_compaction_that_leaves_a_large_context_still_warns(world: World) -> None:
    compactable(world, context_warn_tokens=250_000)
    world.context = 400_000
    world.after = 300_000
    world.run("grow")
    world.run("work")
    engine = world.engine
    assert isinstance(engine, Compacting)
    assert WARNING in engine.prompts[-1]
    (line,) = engine.lines
    assert line is None or "about 300000 tokens" in line


def test_an_engine_that_cannot_compact_gets_the_warning_in_its_prompt(
    worlds: Callable[..., World],
) -> None:
    world = worlds(compacting=False)
    compactable(world, context_warn_tokens=150_000)
    world.context = 400_000
    world.run("grow")
    world.call("compact_session", {})
    world.run("work")
    prompt = world.engine.prompts[-1]
    assert prompt.startswith(WARNING)
    assert prompt.count(WARNING) == 1


def test_the_compaction_note_ends_with_the_context_line() -> None:
    compaction: dict[str, int | str | None] = {
        "trigger": "manual",
        "before_tokens": 10,
        "after_tokens": 2,
    }
    assert compaction_note(compaction, "at your request", "LINE").endswith(
        "After: 2 tokens. LINE"
    )
    assert compaction_note(compaction).endswith("After: 2 tokens.")


# C. The exit code on its own.


def summary(text: str) -> TaskNotificationMessage:
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id="t1",
        status="failed",
        output_file="out.output",
        summary=text,
        uuid="u",
        session_id="s",
    )


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ('Background command "Run a failing command" failed with exit code 1', 1),
        ("completed (exit code 0)", 0),
        ('Background command "says exit code 7" failed with exit code 2', 2),
        ('Background command "says exit code 7" was stopped', None),
        ("exit code 3 then more words", None),
        ("", None),
    ],
)
def test_the_exit_code_is_read_only_from_the_end_of_the_summary(
    text: str, code: int | None
) -> None:
    assert end_details(summary(text)) == (code, "out.output")


def test_a_note_gives_the_exit_code_as_its_own_sentence(world: World) -> None:
    from dromon.engine import BackgroundEnd

    world.run("start")
    world.scheduler.background_ended(
        "alpha", BackgroundEnd("t1", "false", "failed", 1, None, False, "")
    )
    world.scheduler.background_ended(
        "alpha", BackgroundEnd("t2", "true", "completed", None, None, False, "")
    )
    notes = [
        m.body
        for m in world.log.state.messages.values()
        if m.sender == LineAddress("alpha")
    ]
    assert notes[0].startswith(
        "Your background task t1 ended with status failed. Its exit code was 1."
    )
    assert notes[1].startswith(
        "Your background task t2 ended with status completed. The engine reported no"
        " exit code for it."
    )


# D. The opt-in automatic compaction.


def test_auto_compact_tokens_round_trips_and_is_omitted_when_unset() -> None:
    config = config_from_document(
        {"allow_compact": True, "auto_compact_tokens": 180_000}
    )
    assert config.auto_compact_tokens == 180_000
    assert decode_config(encode_config(config)) == config
    assert "auto_compact_tokens" not in encode_config(LineConfig(allow_compact=True))
    assert LineConfig().auto_compact_tokens is None


@pytest.mark.parametrize("value", [999, 10_000_001, "180000", True, 1.5])
def test_auto_compact_tokens_refuses_values_out_of_range(value: object) -> None:
    with pytest.raises(ConfigError, match="auto_compact_tokens must be a whole number"):
        config_from_document({"allow_compact": True, "auto_compact_tokens": value})


def test_auto_compact_tokens_needs_allow_compact() -> None:
    with pytest.raises(ConfigError, match="auto_compact_tokens needs allow_compact"):
        config_from_document({"auto_compact_tokens": 180_000})


def test_a_project_line_may_set_auto_compact_tokens() -> None:
    from dromon.projectconfig import parse

    project = parse(
        'version = 1\n[lines.alpha]\nmodel = "claude-haiku-5-5"\n'
        "allow_compact = true\nauto_compact_tokens = 200000\n"
    )
    assert project.lines["alpha"].config.auto_compact_tokens == 200_000


def test_a_turn_past_the_threshold_compacts_first_and_says_why(world: World) -> None:
    compactable(world, auto_compact_tokens=180_000)
    world.context = 200_000
    world.run("grow")
    world.run("work")
    engine = world.engine
    assert isinstance(engine, Compacting)
    (reason,) = engine.reasons
    assert reason.startswith("automatically, because its context of about 200000")
    assert "threshold of 180000" in reason
    assert WARNING not in engine.prompts[-1]
    assert world.status()["context"]["last_compaction"]["cause"] == (
        "auto_compact_tokens"
    )


def test_a_turn_below_the_threshold_does_not_compact(world: World) -> None:
    compactable(world, auto_compact_tokens=180_000)
    world.context = 179_999
    world.run("grow")
    world.run("work")
    engine = world.engine
    assert isinstance(engine, Compacting)
    assert engine.reasons == []


def test_no_automatic_compaction_without_the_key(world: World) -> None:
    compactable(world)
    world.context = 900_000
    world.run("grow")
    world.run("work")
    engine = world.engine
    assert isinstance(engine, Compacting)
    assert engine.reasons == []
    assert WARNING not in engine.prompts[-1]


def test_no_automatic_compaction_once_allow_compact_is_withdrawn(world: World) -> None:
    compactable(world, auto_compact_tokens=180_000)
    world.context = 200_000
    world.run("grow")
    world.scheduler.configure_line("alpha", LineConfig())
    world.run("work")
    engine = world.engine
    assert isinstance(engine, Compacting)
    assert engine.reasons == []


def test_a_compaction_that_cannot_get_below_the_threshold_is_not_repeated(
    world: World,
) -> None:
    compactable(world, auto_compact_tokens=180_000)
    world.context = 400_000
    world.after = 250_000
    world.run("grow")
    world.run("work")
    # The turns after the compaction grow the context past what it left.
    world.context = 260_000
    world.run("more")
    world.run("again")
    engine = world.engine
    assert isinstance(engine, Compacting)
    assert len(engine.reasons) == 1
    # The warning is back, since nothing will compact the context for it.
    assert WARNING not in engine.prompts[-1]


def test_automatic_compaction_resumes_once_a_compaction_gets_below(
    world: World,
) -> None:
    compactable(world, auto_compact_tokens=180_000, context_warn_tokens=250_000)
    world.context = 200_000
    world.after = 250_000
    world.run("grow")
    world.run("work")
    world.after = 5_000
    world.call("compact_session", {})
    world.run("asked")
    world.context = 200_000
    world.run("grown again")
    world.run("work again")
    engine = world.engine
    assert isinstance(engine, Compacting)
    assert len(engine.reasons) == 3
    assert engine.reasons[1] == "at your request"
    assert engine.reasons[2].startswith("automatically")


def test_a_request_takes_precedence_over_the_automatic_reason(world: World) -> None:
    compactable(world, auto_compact_tokens=180_000, context_warn_tokens=180_000)
    world.context = 200_000
    world.scheduler._context["alpha"] = 200_000  # pyright: ignore[reportPrivateUsage]
    world.call("compact_session", {})
    world.run("work")
    engine = world.engine
    assert isinstance(engine, Compacting)
    assert engine.reasons == ["at your request"]
    assert world.status()["context"]["last_compaction"]["cause"] == "compact_session"


def test_a_compaction_dromon_did_not_ask_for_is_the_engines(world: World) -> None:
    world.scheduler.session_compacted(
        "alpha", {"trigger": "auto", "before_tokens": 9, "after_tokens": 1}
    )
    assert world.status()["context"]["last_compaction"]["cause"] == "engine"


def test_an_unreported_compaction_leaves_no_cause_for_a_later_one(
    world: World,
) -> None:
    class Silent(Compacting):
        """Asked to compact, and reports no compaction."""

        async def run_turn(self, prompt: str) -> TurnReport:
            self.pending = None
            return await super().run_turn(prompt)

    world.build = lambda spec, tools: world.engines.setdefault(  # type: ignore[method-assign]
        spec.line_id, Silent(spec, world)
    )
    world.scheduler._engine_factory = world.build  # pyright: ignore[reportPrivateUsage]
    compactable(world)
    context_at_warning(world)
    world.call("compact_session", {})
    world.run("work")
    engine = world.engine
    assert isinstance(engine, Silent) and engine.reasons == ["at your request"]
    world.scheduler.session_compacted(
        "alpha", {"trigger": "auto", "before_tokens": 9, "after_tokens": 1}
    )
    assert world.status()["context"]["last_compaction"]["cause"] == "engine"


# E. A compaction's and an interrupted turn's own costs.


def test_recent_turns_shows_the_compaction_on_its_own(world: World) -> None:
    world.compaction = {"trigger": "manual", "before_tokens": 9, "after_tokens": 1}
    world.compaction_cost = 0.05
    world.run("work")
    (entry,) = world.status()["recent_turns"]
    assert entry["kind"] == "turn"
    assert entry["compaction"]["cost_usd"] == "0.0500"
    assert entry["compaction"]["before_tokens"] == 9
    assert "included" in entry["compaction"]["note"]


def test_a_turn_without_a_compaction_says_none(world: World) -> None:
    world.run("work")
    (entry,) = world.status()["recent_turns"]
    assert entry["compaction"] is None


def test_a_cost_between_turns_is_its_own_entry_and_counts(world: World) -> None:
    world.scheduler.spent_between(
        "alpha", SELF_STARTED_KIND, 0.25, {"output_tokens": 3}
    )
    (entry,) = world.status()["recent_turns"]
    assert entry["kind"] == SELF_STARTED_KIND
    assert entry["cost_usd"] == "0.2500"
    assert entry["usage"] == {"output_tokens": 3}
    assert entry.keys() >= {"kind", "cost_usd", "usage", "compaction"}
    assert entry["compaction"] is None
    assert world.status()["cost_usd"]["estimate"] == "0.2500"
    assert world.status()["autonomous_turns"]["used"] == 0


def test_an_unreported_cost_between_turns_is_said_plainly(world: World) -> None:
    world.scheduler.spent_between("alpha", INTERRUPTED_KIND, None, None)
    (entry,) = world.status()["recent_turns"]
    assert entry["kind"] == INTERRUPTED_KIND
    assert entry["cost_note"] == (
        "the engine reported no cost for this turn, so none was counted"
    )
    # Row 8. The cost is unknown, which is not zero.
    cost = world.status()["cost_usd"]
    assert (cost["estimate"], cost["basis"], cost["unknown_turns"]) == (
        None,
        "unknown",
        1,
    )


def test_a_cost_between_turns_past_the_budget_holds_the_line(world: World) -> None:
    world.scheduler.spent_between("alpha", SELF_STARTED_KIND, 1.5, None)
    assert world.log.state.lines["alpha"].held_by is not None


def test_a_cost_for_an_unknown_line_is_ignored(world: World) -> None:
    world.scheduler.spent_between("nobody", SELF_STARTED_KIND, 1.5, None)
    assert world.log.state.lines["alpha"].held_by is None


# E, at the Claude Code engine.


def costed(total: float) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="session-1",
        total_cost_usd=total,
    )


def init() -> SystemMessage:
    return SystemMessage(
        subtype="init",
        data={
            "model": HAIKU,
            "claude_code_version": "2.1.310",
            "apiKeySource": "none",
            "tools": [],
            "permissionMode": "default",
        },
    )


def boundary(before: int, after: int) -> SystemMessage:
    return SystemMessage(
        subtype="compact_boundary",
        data={
            "compact_metadata": {
                "trigger": "manual",
                "pre_tokens": before,
                "post_tokens": after,
            }
        },
    )


class Kept:
    """One scripted kept engine connection."""

    def __init__(self, script: list[list[Message]]) -> None:
        self.script = script
        self.stream: asyncio.Queue[Message | None] = asyncio.Queue()
        self.prompts: list[str] = []
        self.interrupts = 0

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)
        self.stream.put_nowait(init())
        for message in self.script.pop(0):
            self.stream.put_nowait(message)

    async def receive_messages(self) -> AsyncIterator[Message]:
        while True:
            message = await self.stream.get()
            if message is None:
                return
            yield message

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def set_permission_mode(self, mode: Any) -> None:  # noqa: ANN401
        del mode


def kept_line(
    connection: Kept,
    tmp_path: Path,
    spent: list[tuple[str, float | None, Mapping[str, int] | None]],
) -> ClaudeCodeLine:
    def factory(
        options: ClaudeAgentOptions,
    ) -> AbstractAsyncContextManager[KeptSession]:
        del options

        @asynccontextmanager
        async def connect() -> AsyncGenerator[KeptSession]:
            try:
                yield connection
            finally:
                connection.stream.put_nowait(None)

        return connect()

    return ClaudeCodeLine(
        Driver.for_model(HAIKU, Strategy.SUBSCRIPTION),
        tmp_path,
        config=LineConfig(keep_session=True),
        environment={},
        kept_factory=factory,
        poll_seconds=3600.0,
        spent_between=lambda kind, cost, usage: spent.append((kind, cost, usage)),
    )


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def test_a_compaction_reports_its_own_cost(tmp_path: Path) -> None:
    connection = Kept([[boundary(180_000, 20_000), costed(0.5)], [costed(0.75)]])
    engine = kept_line(connection, tmp_path, [])

    async def go() -> TurnReport:
        engine.request_compaction("at your request", lambda: "AFTER")
        report = await engine.run_turn("work")
        await engine.close()
        return report

    report = asyncio.run(go())
    assert connection.prompts[0] == COMPACT_COMMAND
    assert connection.prompts[1].startswith(
        "Dromon note: your session was compacted at the start of this turn at your"
        " request. Context before: 180000 tokens. After: 20000 tokens. AFTER"
    )
    assert report.compaction_cost_usd == pytest.approx(0.5)
    assert report.cost_estimate_usd == pytest.approx(0.75)


def test_a_self_started_turns_result_is_reported_as_its_own_cost(
    tmp_path: Path,
) -> None:
    connection = Kept([[costed(0.5)], [costed(0.9)]])
    spent: list[tuple[str, float | None, Mapping[str, int] | None]] = []
    engine = kept_line(connection, tmp_path, spent)

    async def go() -> TurnReport:
        await engine.run_turn("first")
        connection.stream.put_nowait(
            AssistantMessage(content=[TextBlock("by myself")], model=HAIKU)
        )
        await settle()
        connection.stream.put_nowait(costed(0.75))
        await settle()
        report = await engine.run_turn("second")
        await engine.close()
        return report

    report = asyncio.run(go())
    assert connection.interrupts == 1
    ((kind, cost, _),) = spent
    assert kind == SELF_STARTED_KIND and cost == pytest.approx(0.25)
    # The mark moved past the self-started turn, so it is not counted twice.
    assert report.cost_estimate_usd == pytest.approx(0.15)


def test_an_interrupted_turns_late_result_is_reported_as_interrupted(
    tmp_path: Path,
) -> None:
    connection = Kept([[], [costed(0.9)]])
    spent: list[tuple[str, float | None, Mapping[str, int] | None]] = []
    engine = kept_line(connection, tmp_path, spent)

    async def go() -> None:
        turn = asyncio.create_task(engine.run_turn("first"))
        await settle()
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await settle()
        connection.stream.put_nowait(costed(0.4))
        await settle()
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    ((kind, cost, _),) = spent
    assert kind == INTERRUPTED_KIND and cost == pytest.approx(0.4)


def test_a_failing_cost_callback_does_not_end_the_connection(tmp_path: Path) -> None:
    connection = Kept([[costed(0.5)], [costed(0.9)]])

    def fail(kind: str, cost: float | None, usage: Mapping[str, int] | None) -> None:
        del kind, cost, usage
        raise RuntimeError("boom")

    engine = kept_line(connection, tmp_path, [])
    engine._spent_between = fail  # pyright: ignore[reportPrivateUsage]

    async def go() -> None:
        await engine.run_turn("first")
        connection.stream.put_nowait(
            AssistantMessage(content=[TextBlock("by myself")], model=HAIKU)
        )
        connection.stream.put_nowait(costed(0.75))
        await settle()
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert len(connection.prompts) == 2


# F. Codex lines.


def notice(method: str, **params: object) -> dict[str, Any]:
    return {"method": method, "params": params}


def usage(total: int, last: int, thread: str = "th") -> dict[str, Any]:
    def breakdown(tokens: int) -> dict[str, int]:
        return {
            "totalTokens": tokens,
            "inputTokens": tokens - 10,
            "cachedInputTokens": tokens // 2,
            "cacheWriteInputTokens": 0,
            "outputTokens": 10,
            "reasoningOutputTokens": 4,
        }

    return notice(
        TOKEN_USAGE_UPDATED,
        threadId=thread,
        turnId="t1",
        tokenUsage={
            "total": breakdown(total),
            "last": breakdown(last),
            "modelContextWindow": 272_000,
        },
    )


def said(text: str) -> dict[str, Any]:
    return notice(
        "item/completed", item={"type": "agentMessage", "id": "m", "text": text}
    )


def done(turn_id: str = "t1") -> dict[str, Any]:
    return notice(
        "turn/completed", threadId="th", turn={"id": turn_id, "status": "completed"}
    )


class Server:
    """A scripted app-server, which plays ``turns`` after each ``turn/start`` and
    ``compaction`` after ``thread/compact/start``."""

    def __init__(
        self,
        turns: list[list[dict[str, Any]]],
        compaction: Sequence[dict[str, Any]] = (),
        *,
        stall: bool = False,
    ) -> None:
        self.turns = turns
        self.compaction = list(compaction)
        self.stall = stall
        self.apps: list[object] = []
        self.sent: list[dict[str, Any]] = []
        self.queue: deque[dict[str, Any]] = deque()

    def methods(self) -> list[str]:
        return [m["method"] for m in self.sent if "method" in m]

    def prompt(self) -> str:
        sent = [m for m in self.sent if m.get("method") == "turn/start"][-1]
        return str(sent["params"]["input"][0]["text"])

    async def send(self, message: Mapping[str, object]) -> None:
        sent = dict(message)
        self.sent.append(sent)
        method = sent.get("method")
        if not isinstance(method, str) or "id" not in sent:
            return
        self.queue.append({"id": sent["id"], "result": self.result(method)})
        if method == "turn/start":
            self.queue.append(notice("turn/started", turn={"id": "t1"}))
            self.queue.extend(self.turns.pop(0))
        if method == COMPACT_START:
            self.queue.extend(self.compaction)
        if method == "turn/interrupt":
            self.queue.append(done())

    def result(self, method: str) -> dict[str, Any]:
        if method == "account/read":
            return {"account": {"type": "chatgpt", "planType": "pro"}}
        if method in ("thread/start", "thread/resume"):
            return {
                "thread": {"id": "th"},
                "model": LUNA,
                "modelProvider": "openai",
                "activePermissionProfile": {"id": "dromon-line"},
            }
        if method == "mcpServerStatus/list":
            return {"data": []}
        if method == "app/list":
            return {"data": list(self.apps)}
        if method == "turn/start":
            return {"turn": {"id": "t1"}}
        return {}

    async def receive(self) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        if self.queue:
            return self.queue.popleft()
        if self.stall:
            await asyncio.Event().wait()
        return None


def codex_line(
    server: Server,
    compactions: list[Mapping[str, int | str | None]] | None = None,
    config: LineConfig | None = None,
) -> CodexLine:
    @asynccontextmanager
    async def connect(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        del arguments, cwd, env
        yield server

    sink = [] if compactions is None else compactions
    return CodexLine(
        Driver.for_model(LUNA, Strategy.SUBSCRIPTION),
        Path("/work"),
        config=config or LineConfig(allow_compact=True),
        environment={},
        connection_factory=connect,
        server_lister=lambda env: [],
        compacted=sink.append,
    )


def test_a_codex_turn_reports_its_calls_tokens_and_context() -> None:
    server = Server([[usage(5_000, 1_000), usage(6_500, 1_500), said("done"), done()]])
    report = asyncio.run(codex_line(server).run_turn("hello"))
    assert [call["total_tokens"] for call in report.calls] == [1_000, 1_500]
    assert report.context_tokens == 1_500
    assert report.usage is not None and report.usage["total_tokens"] == 2_500
    assert report.usage_note == CODEX_USAGE_NOTE
    assert report.cost_estimate_usd is None


def test_a_codex_turn_without_usage_notices_reports_none() -> None:
    report = asyncio.run(codex_line(Server([[said("done"), done()]])).run_turn("hi"))
    assert report.calls == () and report.usage is None and report.context_tokens is None


def test_a_codex_usage_notice_for_another_thread_is_ignored() -> None:
    server = Server([[usage(5_000, 1_000, thread="other"), said("done"), done()]])
    report = asyncio.run(codex_line(server).run_turn("hello"))
    assert report.calls == () and report.context_tokens is None


def compaction_item(method: str, thread: str = "th") -> dict[str, Any]:
    return notice(
        method, threadId=thread, turnId="c1", item={"type": COMPACTION_ITEM, "id": "k"}
    )


def compaction_usage(total: int, size: int) -> dict[str, Any]:
    """The usage notice observed during a compaction: the running totals unchanged
    and a ``last`` of a total alone, every other count zero."""
    zero = {
        "totalTokens": size,
        "inputTokens": 0,
        "cachedInputTokens": 0,
        "cacheWriteInputTokens": 0,
        "outputTokens": 0,
        "reasoningOutputTokens": 0,
    }
    notice_ = usage(total, 0)
    notice_["params"]["tokenUsage"]["last"] = zero
    return notice_


OBSERVED_COMPACTION: Final = [
    notice("turn/started", turn={"id": "c1"}),
    compaction_item("item/started"),
    compaction_usage(9_000, 12_000),
    compaction_item("item/completed"),
    done("c1"),
]
"""The order Codex 0.164.0 was observed to report a compaction in, live."""


def compacted_after_first_turn(
    compaction: list[dict[str, Any]],
) -> tuple[Server, list[Mapping[str, int | str | None]], TurnReport]:
    server = Server(
        [[usage(9_000, 8_000), said("one"), done()], [said("two"), done()]],
        compaction,
    )
    compactions: list[Mapping[str, int | str | None]] = []
    target = codex_line(server, compactions)

    async def go() -> TurnReport:
        await target.run_turn("first")
        target.request_compaction("at your request", lambda: None)
        return await target.run_turn("second")

    return server, compactions, asyncio.run(go())


def test_a_codex_line_compacts_its_thread_before_the_prompt() -> None:
    server, compactions, report = compacted_after_first_turn(OBSERVED_COMPACTION)
    assert server.methods().count(COMPACT_START) == 1
    assert server.methods().index(COMPACT_START) < len(server.methods()) - 1
    expected = {
        "trigger": None,
        "before_tokens": 8_000,
        "after_tokens": None,
        "after_estimate_tokens": None,
    }
    assert compactions == [expected]
    assert report.compaction == expected
    # Round 6 review, F3. No call was seen in the compaction, so its tokens are
    # unknown, not measured zeros.
    assert report.compaction_usage is None
    assert server.prompt().startswith(
        "Dromon note: your session was compacted at the start of this turn at your"
        " request. Context before: 8000 tokens. After: not reported."
    )
    assert server.prompt().endswith("second")
    assert report.text == "two"


def test_a_codex_compaction_usage_notice_is_not_an_engine_call() -> None:
    _, _, report = compacted_after_first_turn(OBSERVED_COMPACTION)
    assert all(call["total_tokens"] != 12_000 for call in report.calls)
    assert report.context_tokens is None


def test_a_codex_compaction_notice_is_still_accepted() -> None:
    server, compactions, _ = compacted_after_first_turn(
        [
            notice("turn/started", turn={"id": "c1"}),
            notice(THREAD_COMPACTED, threadId="th", turnId="c1"),
            usage(9_500, 500),
            done("c1"),
        ]
    )
    expected = {
        "trigger": None,
        "before_tokens": 8_000,
        "after_tokens": 500,
        "after_estimate_tokens": None,
    }
    assert compactions == [expected]
    assert "After: 500 tokens." in server.prompt()


def test_a_codex_compaction_of_another_thread_is_not_the_lines() -> None:
    server, compactions, _ = compacted_after_first_turn(
        [
            notice("turn/started", turn={"id": "c1"}),
            compaction_item("item/started", thread="other"),
            compaction_item("item/completed", thread="other"),
            notice(THREAD_COMPACTED, threadId="other", turnId="c1"),
            done("c1"),
        ]
    )
    assert compactions == []
    assert "reported no compaction" in server.prompt()


def test_a_codex_compaction_the_engine_does_not_report_is_said_plainly() -> None:
    server = Server([[said("two"), done()]], [done("c1")])
    compactions: list[Mapping[str, int | str | None]] = []
    target = codex_line(server, compactions)
    target.request_compaction("at your request", lambda: "STILL LARGE")

    report = asyncio.run(target.run_turn("second"))
    assert compactions == []
    assert report.compaction == {
        "trigger": None,
        "before_tokens": None,
        "after_tokens": None,
        "after_estimate_tokens": None,
    }
    assert "reported no compaction" in server.prompt()
    assert "STILL LARGE" in server.prompt()


def test_a_codex_compaction_that_uses_a_tool_refuses_the_turn() -> None:
    server = Server(
        [[said("two"), done()]],
        [
            notice("turn/started", turn={"id": "c1"}),
            notice("item/started", item={"type": "commandExecution", "id": "i"}),
            done("c1"),
        ],
    )
    target = codex_line(server)
    target.request_compaction("at your request", lambda: None)
    with pytest.raises(EngineRefused, match="not given"):
        asyncio.run(target.run_turn("second"))
    assert "turn/start" not in server.methods()


def test_a_codex_compaction_that_never_ends_refuses_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_module, "COMPACT_TIMEOUT_SECONDS", 0.05)
    server = Server([[said("two"), done()]], stall=True)
    target = codex_line(server)
    target.request_compaction("at your request", lambda: None)
    with pytest.raises(EngineRefused, match="did not finish compacting"):
        asyncio.run(target.run_turn("second"))
    assert "turn/start" not in server.methods()


def test_a_codex_line_without_a_request_does_not_compact() -> None:
    server = Server([[said("one"), done()]])
    asyncio.run(codex_line(server).run_turn("first"))
    assert COMPACT_START not in server.methods()


def test_codex_lines_may_compact_and_have_the_tool(
    worlds: Callable[..., World],
) -> None:
    assert unsupported(LineConfig(allow_compact=True)) is None
    world = worlds()
    world.scheduler.create_line("luna", LUNA, Strategy.SUBSCRIPTION)
    world.scheduler.configure_line("luna", LineConfig(allow_compact=True))
    names = [t.name for t in world.scheduler.own_tools("luna")]
    assert "compact_session" in names
    world.scheduler.configure_line("luna", LineConfig())
    names = [t.name for t in world.scheduler.own_tools("luna")]
    assert "compact_session" not in names


# Review of round 6.


def test_f1_an_unsized_codex_compaction_holds_automatic_compaction(
    worlds: Callable[..., World],
) -> None:
    server = Server(
        [
            [usage(200_000, 190_000), said("one"), done()],
            # Compacted first, to a size not reported, and still past the threshold.
            [usage(400_000, 195_000), said("two"), done()],
            [usage(600_000, 196_000), said("three"), done()],
            # A context below the threshold releases the hold.
            [usage(700_000, 100_000), said("four"), done()],
            [usage(900_000, 185_000), said("five"), done()],
            [said("six"), done()],
        ],
        OBSERVED_COMPACTION,
    )
    world = worlds()

    def build(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> CodexLine:
        del tools
        return CodexLine(
            Driver.for_model(LUNA, Strategy.SUBSCRIPTION),
            Path("/work"),
            config=spec.config,
            environment={},
            connection_factory=lambda arguments, cwd, env: served(server),
            server_lister=lambda env: [],
            compacted=spec.compacted,
        )

    world.scheduler._engine_factory = build  # pyright: ignore[reportPrivateUsage]
    world.scheduler.create_line("luna", LUNA, Strategy.SUBSCRIPTION)
    world.scheduler.configure_line(
        "luna",
        config_from_document(
            {
                "allow_compact": True,
                "context_warn_tokens": 250_000,
                "auto_compact_tokens": 180_000,
            }
        ),
    )
    counts: list[int] = []
    held: list[bool] = []
    for body in ("one", "two", "three", "four", "five", "six"):
        run_line(world, "luna", body)
        counts.append(server.methods().count(COMPACT_START))
        context = cast(
            dict[str, object], world.scheduler.line_status("luna")["context"]
        )
        held.append(context["auto_compact_held"] is True)
    assert counts == [0, 1, 1, 1, 1, 2]
    assert held == [False, True, True, False, False, True]


@asynccontextmanager
async def served(server: Server) -> AsyncGenerator[Connection]:
    yield server


def run_line(world: World, line_id: str, body: str) -> None:
    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        world.scheduler.send(ANA, LineAddress(line_id), body)
        await asyncio.sleep(0)
        await world.scheduler.idle()
        stop.set()
        await loop

    asyncio.run(go())


def test_f1_a_reported_size_past_the_threshold_holds_until_a_lower_estimate(
    world: World,
) -> None:
    compactable(world, auto_compact_tokens=180_000)
    world.context = 400_000
    world.after = 250_000
    world.run("grow")
    world.run("work")
    assert world.status()["context"]["auto_compact_held"] is True
    world.context = 100_000
    world.run("small")
    assert world.status()["context"]["auto_compact_held"] is False
    world.context = 200_000
    world.run("grown")
    world.run("again")
    engine = world.engine
    assert isinstance(engine, Compacting)
    assert len(engine.reasons) == 2


def test_f2_a_refused_claude_turn_leaves_no_compaction_for_the_next(
    tmp_path: Path,
) -> None:
    connection = Kept([[costed(0.5)]])
    engine = kept_line(connection, tmp_path, [])
    # A line that loads project settings needs an enabled sandbox, so the turn is
    # refused at its options, before its compaction point.
    engine.config = LineConfig(keep_session=True, project_settings=True)

    async def go() -> None:
        engine.request_compaction("at your request", lambda: None)
        with pytest.raises(EngineRefused, match="needs an enabled sandbox"):
            await engine.run_turn("refused")
        engine.config = LineConfig(keep_session=True)
        await engine.run_turn("next")
        await engine.close()

    asyncio.run(go())
    assert connection.prompts == ["next"]


def test_f2_a_refused_codex_turn_leaves_no_compaction_for_the_next() -> None:
    server = Server([[said("next"), done()]], OBSERVED_COMPACTION)
    server.apps = [{"id": "app"}]
    target = codex_line(server)
    target.request_compaction("at your request", lambda: None)
    with pytest.raises(EngineRefused, match="offers apps"):
        asyncio.run(target.run_turn("refused"))
    server.apps = []
    report = asyncio.run(target.run_turn("next"))
    assert COMPACT_START not in server.methods()
    assert report.compaction is None
    assert not server.prompt().startswith("Dromon note")


def test_f3_a_codex_turn_with_only_a_compaction_notice_has_unknown_usage() -> None:
    server = Server([[compaction_usage(9_000, 12_000), said("x"), done()]])
    report = asyncio.run(codex_line(server).run_turn("hi"))
    assert report.usage is None and report.calls == ()


def test_f4_the_wake_note_says_a_restart_loses_the_request() -> None:
    assert "restart" in COMPACT_WAKE_NOTE


# The round 7 report of luna. The trigger is only what the engine reported.


def test_a_codex_compaction_reports_no_trigger_and_says_why() -> None:
    from dromon.scheduler import trigger_note

    _, compactions, report = compacted_after_first_turn(OBSERVED_COMPACTION)
    assert compactions[0]["trigger"] is None
    assert report.compaction is not None and report.compaction["trigger"] is None
    assert "reports no trigger" in trigger_note(None)
    assert "cause says why" in trigger_note("manual")
    assert "by itself" in trigger_note("auto")
    assert "does not know" in trigger_note("other")


def test_a_codex_compaction_without_an_earlier_estimate_is_still_reported() -> None:
    server = Server([[said("two"), done()]], OBSERVED_COMPACTION)
    target = codex_line(server)
    target.request_compaction("at your request", lambda: None)
    asyncio.run(target.run_turn("second"))
    assert "reported no compaction" not in server.prompt()
    assert "Context before: not reported. After: not reported." in server.prompt()


def test_an_unreported_codex_compaction_is_still_said_plainly() -> None:
    server = Server([[said("two"), done()]], [done("c1")])
    target = codex_line(server)
    target.request_compaction("at your request", lambda: None)
    asyncio.run(target.run_turn("second"))
    assert "reported no compaction" in server.prompt()
