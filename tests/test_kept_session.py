"""A Claude Code line that keeps its engine connected between turns, driven by a
scripted connection that behaves as the bundled engine was read to behave.

The script's engine reports its init on the first prompt of a connection, and again
only after ``set_permission_mode``, as Claude Code 2.1.310's engine loop does.
"""

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from dromon.driver import Action, Driver, Event, Strategy, driver_resource
from dromon import launcher, redact
from dromon.activity import ActivityKind, ActivityLog
from dromon.ceiling import HARD_REASON, STOP_REASON, Choice
from dromon.engine import (
    CONSENT_FALLBACK,
    FALLBACK_SUBTYPES,
    HALT_REASON,
    HANDLED_SYSTEM_SUBTYPES,
    HANDOFF_NOTICE,
    HANDOFF_REFUSAL,
    HANDOFF_RESENDS,
    HANDSHAKE_KIND,
    TASKS_CHANGED,
    TurnReport,
    MAX_HANDOFF_TEXT_CHARACTERS,
    handoff_text_notice,
    is_handoff,
    HUMAN_DECISION_REFUSAL,
    HUMAN_DECISION_TOOLS,
    TOOL_SEARCH_PINNED,
    TOOL_SEARCH_VARIABLE,
    claude_cli,
    consent_question,
    fallback_text,
    flag_settings,
    is_handshake,
    BackgroundEnd,
    SelfStartedFinish,
    DIED_NOTE,
    FILES_CHANGED,
    MAX_FINGERPRINT_FILE_BYTES,
    SELF_STARTED_NOTE,
    AgentSession,
    ClaudeCodeLine,
    EngineRefused,
    KeptSession,
    begins_turn,
    closed_note,
    permission_gate,
    permission_mode_of,
    task_ended,
    task_status,
    tree_digest,
    turn_cost,
)
from dromon import engine as engine_module
from dromon.launcher import StopPolicy
from dromon.settings import PINNED_VARIABLES
from dromon.lineconfig import (
    AtMax,
    ToolPolicy,
    DEFAULT_CONFIG,
    ConfigError,
    LineConfig,
    config_from_document,
    decode_config,
    encode_config,
)
from dromon import codex, grok
from dromon.projectconfig import parse
from dromon.configview import LineFacts, Overrides, line_entries, project_layers
from dromon.projectconfig import ProjectConfig
from dromon.limits import DEFAULT_LIMITS

MODEL = "claude-haiku-5-5"
CONTEXT = ToolPermissionContext()


def init(source: str = "none", tools: Sequence[str] = ()) -> SystemMessage:
    return SystemMessage(
        subtype="init",
        data={
            "model": MODEL,
            "claude_code_version": "2.1.310",
            "apiKeySource": source,
            "tools": list(tools),
            "permissionMode": "default",
        },
    )


def result(session: str = "session-1", message: str | None = None) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session,
        total_cost_usd=0.001,
        result=message,
    )


def say(text: str) -> AssistantMessage:
    return AssistantMessage(content=[TextBlock(text)], model=MODEL)


def started(task_id: str) -> TaskStartedMessage:
    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id=task_id,
        description="a background command",
        uuid="u",
        session_id="session-1",
    )


def notified(task_id: str) -> TaskNotificationMessage:
    return TaskNotificationMessage(
        subtype="task_notification",
        data={},
        task_id=task_id,
        status="completed",
        output_file="out.txt",
        summary="done",
        uuid="u",
        session_id="session-1",
    )


class Connection:
    """One scripted engine process. Each prompt takes the next turn of the shared
    script, preceded by an init when one is due."""

    def __init__(self, factory: "Factory") -> None:
        self.factory = factory
        self.stream: asyncio.Queue[Message | None] = asyncio.Queue()
        self.prompts: list[str] = []
        self.modes: list[str] = []
        self.interrupts = 0
        self.closed = False
        self.init_due = True

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)
        turn = self.factory.script.pop(0)
        if self.init_due and self.factory.reports_init:
            self.stream.put_nowait(self.factory.init_message)
        self.init_due = False
        for message in turn:
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
        self.modes.append(mode)
        self.init_due = self.factory.reemits_init

    def die(self) -> None:
        self.stream.put_nowait(None)


class Factory:
    def __init__(self, script: list[list[Message | None]]) -> None:
        self.script = script
        self.connections: list[Connection] = []
        self.options: list[ClaudeAgentOptions] = []
        self.reemits_init = True
        self.reports_init = True
        self.init_message = init()

    def __call__(
        self, options: ClaudeAgentOptions
    ) -> AbstractAsyncContextManager[KeptSession]:
        @asynccontextmanager
        async def connect() -> AsyncGenerator[KeptSession]:
            connection = Connection(self)
            self.connections.append(connection)
            self.options.append(options)
            try:
                yield connection
            finally:
                connection.closed = True
                connection.die()

        return connect()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


KEPT = LineConfig(keep_session=True)


def line(
    factory: Factory,
    tmp_path: Path,
    config: LineConfig = KEPT,
    clock: Clock | None = None,
    poll: float = 3600.0,
    sandbox: dict[str, object] | None = None,
) -> ClaudeCodeLine:
    return ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=config,
        environment={},
        kept_factory=factory,
        clock=clock or Clock(),
        poll_seconds=poll,
        sandbox=sandbox,
    )


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


# Reuse and the per-turn checks.


def test_a_kept_line_sends_its_next_prompt_on_the_same_connection(
    tmp_path: Path,
) -> None:
    factory = Factory([[say("one"), result()], [say("two"), result("session-1")]])
    engine = line(factory, tmp_path)

    async def go() -> None:
        first = await engine.run_turn("first")
        assert engine.kept_status()["kept"] is True
        second = await engine.run_turn("second")
        assert (first.text, second.text) == ("one", "two")
        assert second.capabilities is not None
        await engine.close()

    asyncio.run(go())
    assert len(factory.connections) == 1
    connection = factory.connections[0]
    assert connection.prompts == ["first", "second"]
    # The reused connection was asked to report its init again, in its own mode.
    assert connection.modes == ["default"]
    assert connection.closed


def test_a_line_without_keep_session_does_not_use_a_kept_connection(
    tmp_path: Path,
) -> None:
    factory = Factory([])
    engine = line(factory, tmp_path, config=DEFAULT_CONFIG)
    assert engine.kept_status() == {
        "keep_session": False,
        "kept": False,
        "seconds_to_close": None,
        "background_tasks": 0,
        "tasks": [],
    }
    assert factory.connections == []


def test_a_reused_connection_that_does_not_report_its_init_is_refused(
    tmp_path: Path,
) -> None:
    factory = Factory([[result()], [say("unchecked"), result()], [result()]])
    factory.reemits_init = False
    engine = line(factory, tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        with pytest.raises(EngineRefused, match="could not be checked"):
            await engine.run_turn("second")
        assert engine.kept_status()["kept"] is False
        # The next turn opens a new connection, which reports its init.
        await engine.run_turn("third")
        await engine.close()

    asyncio.run(go())
    assert len(factory.connections) == 2
    assert factory.connections[0].interrupts == 1
    assert factory.connections[0].closed


def test_a_kept_turn_reporting_missing_sign_in_is_refused_and_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = Factory([[say("Not logged in · Please run /login"), result()]])
    engine = line(factory, tmp_path)
    result_events: list[Event] = []
    original_step = Driver.step

    def record_step(driver: Driver, event: Event) -> Action:
        if event.tag == "result":
            result_events.append(event)
        return original_step(driver, event)

    monkeypatch.setattr(Driver, "step", record_step)

    async def go() -> None:
        with pytest.raises(EngineRefused, match="not signed in"):
            await engine.run_turn("prompt")

    asyncio.run(go())
    assert factory.connections[0].closed
    assert factory.connections[0].interrupts == 1
    assert not result_events


def test_a_reused_connection_runs_the_tool_check_again(tmp_path: Path) -> None:
    factory = Factory([[result()], [say("x"), result()]])
    engine = line(factory, tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        factory.init_message = init(tools=["Bash"])
        with pytest.raises(EngineRefused, match="tools the line was not given: Bash"):
            await engine.run_turn("second")

    asyncio.run(go())
    assert factory.connections[0].closed


def test_a_reused_connection_runs_the_credential_check_again(tmp_path: Path) -> None:
    factory = Factory([[result()], [say("x"), result()]])
    engine = line(factory, tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        factory.init_message = init(source="ANTHROPIC_API_KEY")
        with pytest.raises(EngineRefused, match="refuses the api credential"):
            await engine.run_turn("second")

    asyncio.run(go())
    assert factory.connections[0].closed


def test_the_environment_check_runs_before_a_reused_turn(tmp_path: Path) -> None:
    factory = Factory([[result()]])
    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=KEPT,
        environment={"ANTHROPIC_API_KEY": "k"},
        kept_factory=factory,
    )
    with pytest.raises(EngineRefused, match="ANTHROPIC_API_KEY"):
        asyncio.run(engine.run_turn("first"))
    assert factory.connections == []


def test_a_changed_instruction_file_closes_the_connection(tmp_path: Path) -> None:
    rules = tmp_path / "RULES.md"
    rules.write_text("old rules")
    config = LineConfig(keep_session=True, instructions=("RULES.md",))
    factory = Factory([[result()], [result()]])
    engine = line(factory, tmp_path, config=config)

    async def go() -> None:
        await engine.run_turn("first")
        rules.write_text("new rules")
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert len(factory.connections) == 2
    assert factory.connections[0].closed
    assert (
        factory.connections[1]
        .prompts[0]
        .startswith(
            closed_note(
                "its configuration, instructions, settings or loaded files changed", 0
            )
        )
    )


def test_a_missing_instruction_file_refuses_a_reused_turn(tmp_path: Path) -> None:
    rules = tmp_path / "RULES.md"
    rules.write_text("rules")
    config = LineConfig(keep_session=True, instructions=("RULES.md",))
    factory = Factory([[result()]])
    engine = line(factory, tmp_path, config=config)

    async def go() -> None:
        await engine.run_turn("first")
        rules.unlink()
        with pytest.raises(EngineRefused, match="cannot read the instructions"):
            await engine.run_turn("second")
        assert engine.kept_status()["kept"] is False

    asyncio.run(go())
    assert factory.connections[0].prompts == ["first"]
    assert factory.connections[0].closed


# What a kept connection loaded.


def test_the_tree_digest_follows_every_file(tmp_path: Path) -> None:
    tree = tmp_path / "plugin"
    (tree / "skills" / "a").mkdir(parents=True)
    skill = tree / "skills" / "a" / "SKILL.md"
    skill.write_text("one")
    before = tree_digest(tree)
    assert tree_digest(tree) == before
    skill.write_text("two")
    assert tree_digest(tree) != before
    changed = tree_digest(tree)
    (tree / "skills" / "b").mkdir()
    (tree / "skills" / "b" / "SKILL.md").write_text("two")
    assert tree_digest(tree) != changed
    assert tree_digest(tmp_path / "absent") == tree_digest(tmp_path / "gone")


def test_the_tree_digest_refuses_what_it_cannot_follow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "big").write_bytes(b"x" * (MAX_FINGERPRINT_FILE_BYTES + 1))
    with pytest.raises(EngineRefused, match="too large"):
        tree_digest(tree)
    (tree / "big").unlink()
    for index in range(3):
        (tree / f"f{index}").write_text("x")
    monkeypatch.setattr(engine_module, "MAX_FINGERPRINT_FILES", 2)
    with pytest.raises(EngineRefused, match="too many"):
        tree_digest(tree)


def test_the_fingerprint_ignores_the_session_to_resume(tmp_path: Path) -> None:
    engine = line(Factory([]), tmp_path)
    before = engine.fingerprint(engine.options())
    engine.session_id = "session-9"
    assert engine.fingerprint(engine.options()) == before
    changed: dict[str, object] = {"env": {"A": "1"}}
    engine.settings = changed
    assert engine.fingerprint(engine.options()) != before


# The engine's process.


def test_an_engine_that_dies_between_turns_is_resumed_with_a_note(
    tmp_path: Path,
) -> None:
    factory = Factory([[result()], [result()]])
    engine = line(factory, tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        factory.connections[0].die()
        await settle()
        assert engine.kept_status()["kept"] is False
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert len(factory.connections) == 2
    assert factory.connections[1].prompts == [f"{DIED_NOTE}\n\nsecond"]
    assert factory.options[1].resume == "session-1"


def test_an_engine_that_dies_during_a_turn_fails_the_turn(tmp_path: Path) -> None:
    factory = Factory([[say("partial"), None], [result()]])
    engine = line(factory, tmp_path)

    async def go() -> None:
        with pytest.raises(EngineRefused, match="process ended during the turn"):
            await engine.run_turn("first")
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert factory.connections[1].prompts == [f"{DIED_NOTE}\n\nsecond"]


# Between turns.


def test_a_turn_the_engine_begins_by_itself_is_interrupted_and_noted(
    tmp_path: Path,
) -> None:
    factory = Factory([[result()], [result()]])
    engine = line(factory, tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        connection = factory.connections[0]
        connection.stream.put_nowait(say("I was re-invoked"))
        await settle()
        assert connection.interrupts == 1
        connection.stream.put_nowait(result())
        await settle()
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert len(factory.connections) == 1
    assert factory.connections[0].prompts[1] == f"{SELF_STARTED_NOTE}\n\nsecond"


def test_an_unfinished_self_started_turn_closes_the_connection(tmp_path: Path) -> None:
    factory = Factory([[result()], [result()]])
    engine = line(factory, tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        factory.connections[0].stream.put_nowait(say("still going"))
        await settle()
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert len(factory.connections) == 2
    prompt = factory.connections[1].prompts[0]
    assert SELF_STARTED_NOTE in prompt
    assert "had not finished its previous turn" in prompt


def test_the_gate_denies_every_call_outside_a_checked_turn(tmp_path: Path) -> None:
    config = LineConfig(keep_session=True, builtin_tools=("Read",))
    engine = line(Factory([]), tmp_path, config=config)
    gate = engine.options().can_use_tool
    assert gate is not None

    async def ask() -> object:
        return await gate("Read", {}, CONTEXT)

    denied = asyncio.run(ask())
    assert isinstance(denied, PermissionResultDeny)
    assert "has not started and checked a turn" in denied.message


def test_the_gate_admits_a_call_once_a_reused_turn_is_checked(tmp_path: Path) -> None:
    config = LineConfig(keep_session=True, builtin_tools=("Read",))
    factory = Factory([[result()], []])
    engine = line(factory, tmp_path, config=config)
    decisions: list[object] = []

    async def go() -> None:
        await engine.run_turn("first")
        gate = factory.options[0].can_use_tool
        assert gate is not None
        turn = asyncio.create_task(engine.run_turn("second"))
        await settle()
        # The init arrived, so the reused turn is checked.
        decisions.append(await gate("Read", {}, CONTEXT))
        decisions.append(await gate("Write", {}, CONTEXT))
        factory.connections[0].stream.put_nowait(result())
        await turn
        decisions.append(await gate("Read", {}, CONTEXT))
        await engine.close()

    asyncio.run(go())
    assert isinstance(decisions[0], PermissionResultAllow)
    assert isinstance(decisions[1], PermissionResultDeny)
    assert isinstance(decisions[2], PermissionResultDeny)


def test_permission_gate_with_admits_false_denies_an_allowed_tool() -> None:
    gate = permission_gate(
        LineConfig(builtin_tools=("Read",)), frozenset(), admits=lambda: False
    )
    assert isinstance(asyncio.run(gate("Read", {}, CONTEXT)), PermissionResultDeny)
    open_gate = permission_gate(
        LineConfig(builtin_tools=("Read",)), frozenset(), admits=lambda: True
    )
    assert isinstance(
        asyncio.run(open_gate("Read", {}, CONTEXT)), PermissionResultAllow
    )


# Idle time, background tasks and the ceiling.


def test_an_idle_connection_closes_after_its_idle_time(tmp_path: Path) -> None:
    clock = Clock()
    config = LineConfig(keep_session=True, keep_session_idle_seconds=60)
    factory = Factory([[result()], [result()]])
    engine = line(factory, tmp_path, config=config, clock=clock)

    async def go() -> None:
        await engine.run_turn("first")
        assert engine.seconds_to_close() == 60
        clock.now += 59
        assert not await engine.expire()
        clock.now += 1
        assert await engine.expire()
        assert engine.kept_status()["kept"] is False
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert factory.connections[0].closed
    assert (
        factory.connections[1]
        .prompts[0]
        .startswith(
            closed_note("it waited keep_session_idle_seconds without a turn", 0)
        )
    )


def test_running_background_tasks_keep_it_up_to_the_ceiling(tmp_path: Path) -> None:
    clock = Clock()
    config = LineConfig(
        keep_session=True, keep_session_idle_seconds=60, keep_session_max_seconds=600
    )
    factory = Factory([[started("t1"), started("t2"), result()], [result()]])
    engine = line(factory, tmp_path, config=config, clock=clock)

    async def go() -> None:
        await engine.run_turn("first")
        assert engine.kept_status()["background_tasks"] == 2
        assert engine.seconds_to_close() == 600
        connection = factory.connections[0]
        connection.stream.put_nowait(notified("t1"))
        await settle()
        assert engine.kept_status()["background_tasks"] == 1
        clock.now += 300
        assert not await engine.expire()
        clock.now += 300
        assert await engine.expire()
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert (
        factory.connections[1]
        .prompts[0]
        .startswith(
            closed_note(
                "it reached keep_session_max_seconds with background tasks"
                " still running",
                1,
            )
        )
    )


def test_the_idle_deadline_is_none_during_a_turn(tmp_path: Path) -> None:
    factory = Factory([[]])
    engine = line(factory, tmp_path)

    async def go() -> None:
        turn = asyncio.create_task(engine.run_turn("first"))
        await settle()
        assert engine.seconds_to_close() is None
        assert not await engine.expire()
        factory.connections[0].stream.put_nowait(result())
        await turn
        await engine.close()

    asyncio.run(go())


def test_the_watch_closes_an_idle_connection_by_itself(tmp_path: Path) -> None:
    clock = Clock()
    config = LineConfig(keep_session=True, keep_session_idle_seconds=60)
    factory = Factory([[result()]])
    engine = line(factory, tmp_path, config=config, clock=clock, poll=0.001)

    async def go() -> None:
        await engine.run_turn("first")
        clock.now += 61
        for _ in range(200):
            if factory.connections[0].closed:
                break
            await asyncio.sleep(0.005)

    asyncio.run(go())
    assert factory.connections[0].closed
    assert engine.kept_status()["kept"] is False


# Interrupts and retirement.


def test_an_interrupted_kept_turn_keeps_the_connection(tmp_path: Path) -> None:
    factory = Factory([[], [result()]])
    engine = line(factory, tmp_path)

    async def go() -> None:
        turn = asyncio.create_task(engine.run_turn("first"))
        await settle()
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await settle()
        connection = factory.connections[0]
        assert connection.interrupts == 1
        # The cancelled turn's result is drained, so it does not count as the
        # engine beginning a turn by itself.
        connection.stream.put_nowait(say("tail"))
        connection.stream.put_nowait(result())
        await settle()
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert len(factory.connections) == 1
    assert factory.connections[0].prompts == ["first", "second"]


def test_retiring_during_a_turn_closes_after_it(tmp_path: Path) -> None:
    factory = Factory([[]])
    engine = line(factory, tmp_path)

    async def go() -> None:
        turn = asyncio.create_task(engine.run_turn("first"))
        await settle()
        await engine.retire()
        assert not factory.connections[0].closed
        factory.connections[0].stream.put_nowait(result())
        await turn
        await settle()

    asyncio.run(go())
    assert factory.connections[0].closed


def test_retiring_an_idle_engine_closes_it_now(tmp_path: Path) -> None:
    factory = Factory([[result()]])
    engine = line(factory, tmp_path)

    async def go() -> None:
        await engine.run_turn("first")
        await engine.retire()

    asyncio.run(go())
    assert factory.connections[0].closed


# Helpers.


def test_helpers_classify_engine_messages() -> None:
    assert begins_turn(say("x")) and begins_turn(result()) and begins_turn(init())
    assert not begins_turn(started("t"))
    assert not begins_turn(SystemMessage(subtype="status", data={}))
    assert task_ended(notified("t")) == "t"
    updated = TaskUpdatedMessage(
        subtype="task_updated", data={}, task_id="u", patch={"status": "killed"}
    )
    assert task_ended(updated) == "u"
    running = TaskUpdatedMessage(
        subtype="task_updated", data={}, task_id="u", patch={"status": "running"}
    )
    assert task_ended(running) is None
    assert permission_mode_of({"permissionMode": "plan"}) == "plan"
    assert permission_mode_of({"permissionMode": "invented"}) == "default"
    assert permission_mode_of({}) == "default"


# Configuration.


def test_keep_session_keys_round_trip() -> None:
    config = config_from_document(
        {
            "keep_session": True,
            "keep_session_idle_seconds": 120,
            "keep_session_max_seconds": 7200,
        }
    )
    assert (config.keep_session, config.keep_session_idle_seconds) == (True, 120)
    assert config.keep_session_max_seconds == 7200
    assert decode_config(encode_config(config)) == config
    stored = json.loads(encode_config(DEFAULT_CONFIG))
    assert not {key for key in stored if key.startswith("keep_session")}


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ({"keep_session": "yes"}, "keep_session must be a boolean"),
        ({"keep_session_idle_seconds": 59}, "keep_session_idle_seconds must be"),
        ({"keep_session_idle_seconds": 86401}, "keep_session_idle_seconds must be"),
        ({"keep_session_idle_seconds": True}, "keep_session_idle_seconds must be"),
        ({"keep_session_max_seconds": "1"}, "keep_session_max_seconds must be"),
        (
            {"keep_session_idle_seconds": 600, "keep_session_max_seconds": 300},
            "must not be less than",
        ),
    ],
)
def test_keep_session_keys_are_checked(
    document: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        config_from_document(document)


def test_codex_lines_accept_keep_session_and_grok_lines_refuse_it() -> None:
    assert codex.unsupported(KEPT) is None
    assert "do not yet keep" in (grok.unsupported(KEPT) or "")


def test_the_project_file_sets_keep_session_for_a_line() -> None:
    file = parse(
        "version = 1\n[lines.a]\nmodel = 'claude-haiku-5-5'\nkeep_session = true\n"
        "keep_session_idle_seconds = 300\n"
    )
    config = file.lines["a"].config
    assert (config.keep_session, config.keep_session_idle_seconds) == (True, 300)


def test_the_line_view_shows_keep_session() -> None:
    def value(config: LineConfig) -> object:
        facts = LineFacts(
            "a",
            MODEL,
            MODEL,
            (MODEL,),
            "subscription",
            None,
            config,
            {},
            DEFAULT_LIMITS,
            False,
            1,
            False,
        )
        layers = project_layers(ProjectConfig(), Overrides(), {}, False)
        return next(
            e for e in line_entries(facts, layers) if e["key"] == "keep_session"
        )["value"]

    assert value(DEFAULT_CONFIG) == "off"
    assert value(KEPT) == (
        "on, closing after 1800 seconds idle, or 14400 with background tasks"
        " running, where it does close, and at the latest after 86400"
    )


# Review of round 2. F1, no tool call is admitted before the init check, on any
# connection.


def test_a_fresh_kept_connection_admits_no_call_before_its_init(
    tmp_path: Path,
) -> None:
    config = LineConfig(keep_session=True, builtin_tools=("Read",))
    factory = Factory([[]])
    factory.reports_init = False
    engine = line(factory, tmp_path, config=config)
    decisions: list[object] = []

    async def go() -> None:
        turn = asyncio.create_task(engine.run_turn("first"))
        await settle()
        gate = factory.options[0].can_use_tool
        assert gate is not None
        decisions.append(await gate("Read", {}, CONTEXT))
        factory.connections[0].stream.put_nowait(init())
        await settle()
        decisions.append(await gate("Read", {}, CONTEXT))
        factory.connections[0].stream.put_nowait(result())
        await turn
        await engine.close()

    asyncio.run(go())
    assert isinstance(decisions[0], PermissionResultDeny)
    assert isinstance(decisions[1], PermissionResultAllow)


def test_a_fresh_kept_connection_with_output_before_init_is_refused(
    tmp_path: Path,
) -> None:
    factory = Factory([[say("too early"), init(), result()]])
    factory.reports_init = False
    engine = line(factory, tmp_path)

    async def go() -> None:
        with pytest.raises(EngineRefused, match="before its output"):
            await engine.run_turn("first")

    asyncio.run(go())
    assert factory.connections[0].interrupts == 1
    assert factory.connections[0].closed


class GateFirst:
    """A one-turn session that asks the gate before and after its init."""

    def __init__(self, options: ClaudeAgentOptions, decisions: list[object]) -> None:
        self.options = options
        self.decisions = decisions
        self.interrupts = 0

    async def query(self, prompt: str) -> None:
        del prompt

    async def receive_response(self) -> AsyncIterator[Message]:
        gate = self.options.can_use_tool
        assert gate is not None
        self.decisions.append(await gate("Read", {}, CONTEXT))
        yield init()
        self.decisions.append(await gate("Read", {}, CONTEXT))
        yield result()

    async def interrupt(self) -> None:
        self.interrupts += 1


def test_a_turn_without_keep_session_admits_no_call_before_its_init(
    tmp_path: Path,
) -> None:
    decisions: list[object] = []

    @asynccontextmanager
    async def factory(options: ClaudeAgentOptions) -> AsyncGenerator[AgentSession]:
        yield GateFirst(options, decisions)

    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=LineConfig(builtin_tools=("Read",)),
        environment={},
        session_factory=factory,
    )
    asyncio.run(engine.run_turn("first"))
    assert isinstance(decisions[0], PermissionResultDeny)
    assert isinstance(decisions[1], PermissionResultAllow)
    gate = engine.options().can_use_tool
    assert gate is not None

    async def after() -> object:
        return await gate("Read", {}, CONTEXT)

    assert isinstance(asyncio.run(after()), PermissionResultDeny)


def test_a_turn_without_keep_session_and_output_before_init_is_refused(
    tmp_path: Path,
) -> None:
    class Early(GateFirst):
        async def receive_response(self) -> AsyncIterator[Message]:
            yield say("too early")
            yield result()

    @asynccontextmanager
    async def factory(options: ClaudeAgentOptions) -> AsyncGenerator[AgentSession]:
        yield Early(options, [])

    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        environment={},
        session_factory=factory,
    )
    with pytest.raises(EngineRefused, match="before its output"):
        asyncio.run(engine.run_turn("first"))


# F2, a change to a loaded file closes an idle connection.

SANDBOX: dict[str, object] = {"enabled": True}
LOADS = LineConfig(keep_session=True, project_settings=True)


def project(tmp_path: Path) -> Path:
    (tmp_path / ".claude").mkdir()
    settings = tmp_path / ".claude" / "settings.json"
    settings.write_text("{}")
    return settings


async def wait_closed(connection: Connection) -> None:
    for _ in range(400):
        if connection.closed:
            return
        await asyncio.sleep(0.005)


def test_an_idle_connection_closes_when_a_loaded_file_changes(tmp_path: Path) -> None:
    settings = project(tmp_path)
    factory = Factory([[result()], [result()]])
    engine = line(factory, tmp_path, config=LOADS, poll=0.001, sandbox=SANDBOX)

    async def go() -> None:
        await engine.run_turn("first")
        await asyncio.sleep(0.02)
        assert not factory.connections[0].closed
        settings.write_text('{"$schema": "changed"}')
        await wait_closed(factory.connections[0])
        assert engine.kept_status()["kept"] is False
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert factory.connections[0].closed
    assert factory.connections[1].prompts[0].startswith(closed_note(FILES_CHANGED, 0))


def test_a_loaded_file_that_cannot_be_digested_closes_the_connection(
    tmp_path: Path,
) -> None:
    project(tmp_path)
    factory = Factory([[result()]])
    engine = line(factory, tmp_path, config=LOADS, poll=0.001, sandbox=SANDBOX)

    async def go() -> None:
        await engine.run_turn("first")
        big = tmp_path / ".claude" / "skills"
        big.mkdir()
        (big / "huge").write_bytes(b"x" * (MAX_FINGERPRINT_FILE_BYTES + 1))
        await wait_closed(factory.connections[0])

    asyncio.run(go())
    assert factory.connections[0].closed


def test_a_change_during_a_turn_is_left_to_the_next_turn(tmp_path: Path) -> None:
    settings = project(tmp_path)
    factory = Factory([[], [result()]])
    engine = line(factory, tmp_path, config=LOADS, poll=0.001, sandbox=SANDBOX)

    async def go() -> None:
        turn = asyncio.create_task(engine.run_turn("first"))
        await settle()
        settings.write_text('{"$schema": "changed"}')
        await asyncio.sleep(0.02)
        assert not factory.connections[0].closed
        factory.connections[0].stream.put_nowait(result())
        await turn
        # The next turn's fingerprint differs, so it opens a new connection.
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert len(factory.connections) == 2


# F3, interrupts and closes begun outside a turn are held until they end.


def test_a_settle_task_survives_a_concurrent_retire(tmp_path: Path) -> None:
    import gc

    factory = Factory([[]])
    engine = line(factory, tmp_path)
    release = asyncio.Event()
    calls: list[str] = []

    async def go() -> None:
        turn = asyncio.create_task(engine.run_turn("first"))
        await settle()
        connection = factory.connections[0]

        async def slow_interrupt() -> None:
            calls.append("started")
            await release.wait()
            calls.append("ended")

        connection.interrupt = slow_interrupt  # type: ignore[method-assign]
        await engine.retire()
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        assert len(engine._background) == 2  # pyright: ignore[reportPrivateUsage]
        gc.collect()
        await settle()
        release.set()
        await settle()

    asyncio.run(go())
    assert calls == ["started", "ended"]
    assert factory.connections[0].closed
    assert not engine._background  # pyright: ignore[reportPrivateUsage]


# F4, the watch reports an error and goes on.


def test_the_watch_survives_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clock = Clock()
    config = LineConfig(keep_session=True, keep_session_idle_seconds=60)
    factory = Factory([[result()]])
    engine = line(factory, tmp_path, config=config, clock=clock, poll=0.001)
    real = engine.expire
    failures = [RuntimeError("a fault in the watch")]

    async def flaky() -> bool:
        if failures:
            raise failures.pop()
        return await real()

    engine.expire = flaky  # type: ignore[method-assign]

    async def go() -> None:
        await engine.run_turn("first")
        clock.now += 61
        await wait_closed(factory.connections[0])

    asyncio.run(go())
    assert factory.connections[0].closed
    assert "the watch of a kept session failed and goes on" in capsys.readouterr().err


# A kept line with a shell tool needs an enabled sandbox.


@pytest.mark.parametrize("shell", ["Bash", "Monitor", "PowerShell"])
def test_a_kept_line_with_a_shell_needs_an_enabled_sandbox(
    tmp_path: Path, shell: str
) -> None:
    config = LineConfig(keep_session=True, builtin_tools=(shell, "Read"))
    with pytest.raises(EngineRefused, match=f"has {shell} needs an enabled sandbox"):
        line(Factory([]), tmp_path, config=config).options()
    with pytest.raises(EngineRefused, match="needs an enabled sandbox"):
        line(Factory([]), tmp_path, config=config, sandbox={"enabled": False}).options()
    line(Factory([]), tmp_path, config=config, sandbox=SANDBOX).options()


def test_the_shell_rule_leaves_other_lines_alone(tmp_path: Path) -> None:
    line(Factory([]), tmp_path, config=LineConfig(builtin_tools=("Bash",))).options()
    kept_reader = LineConfig(keep_session=True, builtin_tools=("Read", "TaskStop"))
    line(Factory([]), tmp_path, config=kept_reader).options()


def test_a_later_init_that_passes_does_not_clear_a_refusal(tmp_path: Path) -> None:
    factory = Factory([[init(), say("x"), result()]])
    factory.init_message = init(tools=["Bash"])
    engine = line(factory, tmp_path)

    async def go() -> None:
        with pytest.raises(EngineRefused, match="tools the line was not given"):
            await engine.run_turn("first")

    asyncio.run(go())
    assert factory.connections[0].interrupts == 1


# Round 3, A. The cost of a turn on a kept connection.


def costed(total: float | None) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="session-1",
        total_cost_usd=total,
    )


def test_turn_cost_is_the_difference_within_one_session() -> None:
    assert turn_cost(1.5, "s", None) == 1.5
    assert turn_cost(4.0, "s", ("s", 1.5)) == 2.5
    assert turn_cost(1.5, "s", ("s", 1.5)) == 0.0
    # A fall means the count restarted, so all of it counts.
    assert turn_cost(0.5, "s", ("s", 4.0)) == 0.5
    assert turn_cost(None, "s", ("s", 4.0)) is None


def test_turn_cost_takes_the_whole_total_when_the_session_changes() -> None:
    # The review's case: a restart whose new total has passed the old mark.
    assert turn_cost(2.5, "new", ("old", 2.0)) == 2.5


def test_a_kept_connection_reports_each_turns_own_cost(tmp_path: Path) -> None:
    factory = Factory(
        [[costed(1.0)], [costed(2.5)], [costed(2.5)], [costed(4.0)], [costed(0.75)]]
    )
    engine = line(factory, tmp_path)
    costs: list[float | None] = []

    async def go() -> None:
        for prompt in ("one", "two", "three", "four"):
            costs.append((await engine.run_turn(prompt)).cost_estimate_usd)
        # A new connection starts its own count, from no baseline.
        await engine.close()
        costs.append((await engine.run_turn("five")).cost_estimate_usd)
        await engine.close()

    asyncio.run(go())
    assert costs == [1.0, 1.5, 0.0, 1.5, 0.75]
    assert len(factory.connections) == 2


def test_a_kept_turn_without_a_reported_cost_keeps_the_baseline(
    tmp_path: Path,
) -> None:
    factory = Factory([[costed(1.0)], [costed(None)], [costed(3.0)]])
    engine = line(factory, tmp_path)
    costs: list[float | None] = []

    async def go() -> None:
        for prompt in ("one", "two", "three"):
            costs.append((await engine.run_turn(prompt)).cost_estimate_usd)
        await engine.close()

    asyncio.run(go())
    assert costs == [1.0, None, 2.0]


# Round 3, B. A background task that ends between turns.


def bash(tool_use_id: str, command: str) -> AssistantMessage:
    return AssistantMessage(
        content=[
            ToolUseBlock(
                id=tool_use_id,
                name="Bash",
                input={"command": command, "run_in_background": True},
            )
        ],
        model=MODEL,
    )


def stop_call(task_id: str) -> AssistantMessage:
    return AssistantMessage(
        content=[ToolUseBlock(id="stop", name="TaskStop", input={"task_id": task_id})],
        model=MODEL,
    )


def launched(task_id: str, tool_use_id: str | None) -> TaskStartedMessage:
    return TaskStartedMessage(
        subtype="task_started",
        data={},
        task_id=task_id,
        description="the engine's description",
        uuid="u",
        session_id="session-1",
        tool_use_id=tool_use_id,
        task_type="local_bash",
    )


def background_line(
    factory: Factory, tmp_path: Path, ends: list[BackgroundEnd]
) -> ClaudeCodeLine:
    return ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=LineConfig(keep_session=True, builtin_tools=("Bash",)),
        environment={},
        sandbox={"enabled": True},
        kept_factory=factory,
        clock=Clock(),
        poll_seconds=3600.0,
        task_ended=ends.append,
    )


def test_a_task_that_ends_between_turns_is_reported_once(tmp_path: Path) -> None:
    factory = Factory([[bash("tu1", "make gate"), launched("t1", "tu1"), result()]])
    ends: list[BackgroundEnd] = []
    engine = background_line(factory, tmp_path, ends)

    async def go() -> None:
        await engine.run_turn("first")
        assert engine.kept_status()["tasks"] == [
            {"task_id": "t1", "command": "make gate"}
        ]
        connection = factory.connections[0]
        connection.stream.put_nowait(notified("t1"))
        connection.stream.put_nowait(
            TaskUpdatedMessage(
                subtype="task_updated",
                data={},
                task_id="t1",
                patch={"status": "killed"},
            )
        )
        await settle()
        await engine.close()

    asyncio.run(go())
    assert ends == [BackgroundEnd("t1", "make gate", "completed", None, "out.txt")]


def test_a_task_without_a_known_command_is_named_by_its_description(
    tmp_path: Path,
) -> None:
    factory = Factory([[launched("t2", None), result()]])
    ends: list[BackgroundEnd] = []
    engine = background_line(factory, tmp_path, ends)

    async def go() -> None:
        await engine.run_turn("first")
        factory.connections[0].stream.put_nowait(
            TaskUpdatedMessage(
                subtype="task_updated",
                data={},
                task_id="t2",
                patch={"status": "failed"},
            )
        )
        await settle()
        await engine.close()

    asyncio.run(go())
    assert ends == [BackgroundEnd("t2", "the engine's description", "failed")]


def test_ends_inside_a_turn_or_asked_for_by_the_line_are_recorded_without_waking(
    tmp_path: Path,
) -> None:
    factory = Factory(
        [
            [
                bash("tu1", "sleep 1"),
                launched("t1", "tu1"),
                notified("t1"),
                bash("tu2", "sleep 300"),
                launched("t2", "tu2"),
                result(),
            ],
            [stop_call("t2"), result()],
        ]
    )
    ends: list[BackgroundEnd] = []
    engine = background_line(factory, tmp_path, ends)

    async def go() -> None:
        await engine.run_turn("first")
        await engine.run_turn("second")
        # The stop the line asked for lands after its turn.
        factory.connections[0].stream.put_nowait(notified("t2"))
        await settle()
        await engine.close()

    asyncio.run(go())
    assert ends == [
        BackgroundEnd("t1", "sleep 1", "completed", None, "out.txt", during_turn=True),
        BackgroundEnd(
            "t2", "sleep 300", "completed", None, "out.txt", stopped_by_line=True
        ),
    ]


def test_a_task_triggered_turn_can_be_admitted_and_reported(
    tmp_path: Path,
) -> None:
    factory = Factory([[started("t1"), result()]])
    ended: list[BackgroundEnd] = []
    settled: list[tuple[TurnReport | None, Exception | None]] = []

    def begin(ends: tuple[BackgroundEnd, ...]) -> SelfStartedFinish | None:
        assert len(ends) == 1
        assert ends[0].self_started

        def finish(report: TurnReport | None, error: Exception | None) -> None:
            settled.append((report, error))

        return finish

    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=LineConfig(keep_session=True, builtin_tools=("Read",)),
        environment={},
        kept_factory=factory,
        clock=Clock(),
        poll_seconds=3600.0,
        task_ended=ended.append,
        begin_self_started=begin,
    )

    async def go() -> None:
        await engine.run_turn("first")
        gate = factory.options[0].can_use_tool
        assert gate is not None
        denied = await gate("Read", {}, CONTEXT)
        assert isinstance(denied, PermissionResultDeny)
        connection = factory.connections[0]
        connection.stream.put_nowait(notified("t1"))
        connection.stream.put_nowait(say("the task finished"))
        await settle()
        allowed = await gate("Read", {}, CONTEXT)
        assert isinstance(allowed, PermissionResultAllow)
        connection.stream.put_nowait(result())
        await settle()
        assert connection.interrupts == 0
        assert ended == []
        assert len(settled) == 1
        report, error = settled[0]
        assert error is None
        assert report is not None and report.text == "the task finished"
        await engine.close()

    asyncio.run(go())


def test_a_task_triggered_turn_uses_the_fresh_driver_tool_budget(
    tmp_path: Path,
) -> None:
    resource = driver_resource(MODEL, Strategy.SUBSCRIPTION)
    source = resources.files("dromon.drivers").joinpath(resource).read_text()
    budgeted = re.sub(
        r"fn tool_budget\(\) -> Word \{ \d+ \}",
        "fn tool_budget() -> Word { 0 }",
        source,
    )
    assert budgeted != source
    factory = Factory([[result()]])
    settled: list[tuple[TurnReport | None, Exception | None]] = []

    def begin(ends: tuple[BackgroundEnd, ...]) -> SelfStartedFinish | None:
        assert len(ends) == 1 and ends[0].self_started

        def finish(report: TurnReport | None, error: Exception | None) -> None:
            settled.append((report, error))

        return finish

    engine = ClaudeCodeLine(
        Driver(budgeted),
        tmp_path,
        config=LineConfig(keep_session=True, builtin_tools=("Read",)),
        environment={},
        kept_factory=factory,
        clock=Clock(),
        poll_seconds=3600.0,
        begin_self_started=begin,
    )

    async def go() -> None:
        await engine.run_turn("first")
        connection = factory.connections[0]
        connection.stream.put_nowait(notified("t1"))
        connection.stream.put_nowait(
            AssistantMessage(
                content=[ToolUseBlock(id="call-1", name="Read", input={})],
                model=MODEL,
            )
        )
        connection.stream.put_nowait(result())
        await settle()
        assert connection.interrupts == 1
        assert len(settled) == 1
        report, error = settled[0]
        assert error is None
        assert report is not None and report.interrupted_for == "tool_budget"
        await engine.close()

    asyncio.run(go())


def test_task_status_reads_each_report() -> None:
    assert task_status(notified("t")) == "completed"
    assert (
        task_status(
            TaskUpdatedMessage(
                subtype="task_updated", data={}, task_id="t", patch={"status": "killed"}
            )
        )
        == "killed"
    )
    assert (
        task_status(
            TaskUpdatedMessage(subtype="task_updated", data={}, task_id="t", patch={})
        )
        == "ended"
    )
    assert task_status(say("x")) == "ended"


# Review of round 3. F1 and F2, the cost mark across sessions and connections.


def session_result(session: str, total: float) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session,
        total_cost_usd=total,
    )


def test_a_new_connection_on_the_same_session_takes_the_difference(
    tmp_path: Path,
) -> None:
    # The engine restores the session's total on resume, so the new connection's
    # first total continues from the old one.
    factory = Factory([[session_result("s", 2.0)], [session_result("s", 2.75)]])
    engine = line(factory, tmp_path)
    costs: list[float | None] = []

    async def go() -> None:
        costs.append((await engine.run_turn("one")).cost_estimate_usd)
        await engine.close()
        costs.append((await engine.run_turn("two")).cost_estimate_usd)
        await engine.close()

    asyncio.run(go())
    assert costs == [2.0, 0.75]
    assert factory.options[1].resume == "s"


def test_a_changed_session_takes_the_whole_total(tmp_path: Path) -> None:
    factory = Factory([[session_result("s", 2.0)], [session_result("t", 2.5)]])
    engine = line(factory, tmp_path)
    costs: list[float | None] = []

    async def go() -> None:
        costs.append((await engine.run_turn("one")).cost_estimate_usd)
        costs.append((await engine.run_turn("two")).cost_estimate_usd)
        await engine.close()

    asyncio.run(go())
    assert costs == [2.0, 2.5]


def test_a_rebuilt_engine_measures_from_the_mark_it_is_given(tmp_path: Path) -> None:
    factory = Factory([[session_result("s", 5.25)]])
    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=KEPT,
        environment={},
        kept_factory=factory,
        cost_mark=("s", 5.0),
    )

    async def go() -> None:
        report = await engine.run_turn("one")
        assert report.cost_estimate_usd == 0.25
        assert report.cost_mark == ("s", 5.25)
        await engine.close()

    asyncio.run(go())


class OneTurn:
    """A session that is not kept, for one turn with a given total."""

    def __init__(self, session: str, total: float) -> None:
        self.session = session
        self.total = total

    async def query(self, prompt: str) -> None:
        del prompt

    async def receive_response(self) -> AsyncIterator[Message]:
        yield init()
        yield session_result(self.session, self.total)

    async def interrupt(self) -> None:
        pass


def test_a_line_without_keep_session_also_takes_the_difference(
    tmp_path: Path,
) -> None:
    # Each turn is a new process that resumes the session and restores its total.
    totals = iter([1.0, 1.5, 2.25])

    @asynccontextmanager
    async def factory(options: ClaudeAgentOptions) -> AsyncGenerator[AgentSession]:
        del options
        yield OneTurn("s", next(totals))

    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        environment={},
        session_factory=factory,
    )
    costs = [asyncio.run(engine.run_turn(p)).cost_estimate_usd for p in "abc"]
    assert costs == [1.0, 0.5, 0.75]


# Round 4. B, one note for a task end and the turn the engine began on it.


def test_a_self_start_after_a_task_end_folds_into_its_note(tmp_path: Path) -> None:
    factory = Factory(
        [[bash("tu1", "sleep 90"), launched("t1", "tu1"), result()], [result()]]
    )
    ends: list[BackgroundEnd] = []
    engine = background_line(factory, tmp_path, ends)

    async def go() -> None:
        await engine.run_turn("first")
        connection = factory.connections[0]
        connection.stream.put_nowait(notified("t1"))
        connection.stream.put_nowait(say("re-invoked"))
        await settle()
        assert connection.interrupts == 1
        connection.stream.put_nowait(result())
        await settle()
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert ends == [BackgroundEnd("t1", "sleep 90", "completed", None, "out.txt", True)]
    # No separate note: the second prompt is the prompt alone.
    assert factory.connections[0].prompts[1] == "second"


def test_a_task_end_without_a_self_start_is_noted_after_the_grace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine_module, "SELF_START_GRACE_SECONDS", 0.01)
    factory = Factory([[bash("tu1", "make gate"), launched("t1", "tu1"), result()]])
    ends: list[BackgroundEnd] = []
    engine = background_line(factory, tmp_path, ends)

    async def go() -> None:
        await engine.run_turn("first")
        connection = factory.connections[0]
        connection.stream.put_nowait(
            TaskUpdatedMessage(
                subtype="task_updated",
                data={},
                task_id="t1",
                patch={"status": "completed"},
            )
        )
        await settle()
        # The notice that follows the update completes the waiting end.
        connection.stream.put_nowait(
            TaskNotificationMessage(
                subtype="task_notification",
                data={},
                task_id="t1",
                status="completed",
                output_file="t1.output",
                summary='Background command "x" completed (exit code 3)',
                uuid="u",
                session_id="session-1",
            )
        )
        await settle()
        assert ends == []
        await asyncio.sleep(0.05)
        await engine.close()

    asyncio.run(go())
    assert ends == [
        BackgroundEnd("t1", "make gate", "completed", 3, "t1.output", False)
    ]


def test_a_self_start_without_a_task_end_keeps_its_own_note(tmp_path: Path) -> None:
    factory = Factory([[result()], [result()]])
    ends: list[BackgroundEnd] = []
    engine = background_line(factory, tmp_path, ends)

    async def go() -> None:
        await engine.run_turn("first")
        factory.connections[0].stream.put_nowait(say("by itself"))
        factory.connections[0].stream.put_nowait(result())
        await settle()
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert ends == []
    assert factory.connections[0].prompts[1].startswith(SELF_STARTED_NOTE)


def test_end_details_read_the_exit_code_from_the_summary() -> None:
    from dromon.engine import end_details

    def notice(summary: str, output: str) -> TaskNotificationMessage:
        return TaskNotificationMessage(
            subtype="task_notification",
            data={},
            task_id="t",
            status="failed",
            output_file=output,
            summary=summary,
            uuid="u",
            session_id="s",
        )

    assert end_details(notice("x failed (exit code -2)", "/o")) == (-2, "/o")
    assert end_details(notice("x completed", "")) == (None, None)
    assert end_details(say("x")) == (None, None)


# E, the tokens behind a turn.


def test_a_report_carries_the_turns_main_loop_tokens(tmp_path: Path) -> None:
    from dromon.engine import usage_of

    message = ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="s",
        total_cost_usd=0.5,
        usage={
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 30,
            "cache_creation_input_tokens": True,
            "server_tool_use": {"web_search_requests": 0},
        },
    )
    factory = Factory([[message]])
    engine = line(factory, tmp_path)

    async def go() -> None:
        report = await engine.run_turn("one")
        assert report.usage == {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 30,
        }
        await engine.close()

    asyncio.run(go())
    assert usage_of(None) is None
    assert usage_of({"input_tokens": -1}) is None


# A, a mark recorded to the millionth still matches.


def test_a_rounded_mark_does_not_read_as_a_restart() -> None:
    assert turn_cost(0.1234565, "s", ("s", 0.123457)) == 0.0
    assert turn_cost(0.2, "s", ("s", 0.123457)) == pytest.approx(0.076543)


# I, one more start after an initialize timeout.


def test_an_initialize_timeout_is_retried_once(tmp_path: Path) -> None:
    from dromon.activity import ActivityKind
    from dromon.engine import INITIALIZE_TIMEOUT

    starts: list[int] = []
    seen: list[tuple[ActivityKind, str]] = []

    def watch(kind: ActivityKind, text: str, *, chunk: bool = False) -> None:
        del chunk
        seen.append((kind, text))

    @asynccontextmanager
    async def flaky(options: ClaudeAgentOptions) -> AsyncGenerator[AgentSession]:
        del options
        starts.append(1)
        if len(starts) == 1:
            raise Exception(INITIALIZE_TIMEOUT)  # noqa: TRY002
        yield OneTurn("s", 0.5)

    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        environment={},
        session_factory=flaky,
        observe=watch,
    )
    report = asyncio.run(engine.run_turn("a"))
    assert report.cost_estimate_usd == 0.5 and len(starts) == 2
    assert seen[0][0] is ActivityKind.NOTICE and "once more" in seen[0][1]


def test_a_second_initialize_timeout_or_another_failure_ends_the_turn(
    tmp_path: Path,
) -> None:
    from dromon.engine import INITIALIZE_TIMEOUT

    failures = [Exception(INITIALIZE_TIMEOUT), Exception(INITIALIZE_TIMEOUT)]

    @asynccontextmanager
    async def timing_out(options: ClaudeAgentOptions) -> AsyncGenerator[AgentSession]:
        del options
        raise failures.pop()
        yield OneTurn("s", 0.5)

    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        environment={},
        session_factory=timing_out,
    )
    with pytest.raises(Exception, match="Control request timeout"):
        asyncio.run(engine.run_turn("a"))
    assert failures == []
    other = [RuntimeError("no engine"), Exception(INITIALIZE_TIMEOUT)]

    @asynccontextmanager
    async def broken(options: ClaudeAgentOptions) -> AsyncGenerator[AgentSession]:
        del options
        raise other.pop(0)
        yield OneTurn("s", 0.5)

    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        environment={},
        session_factory=broken,
    )
    with pytest.raises(RuntimeError, match="no engine"):
        asyncio.run(engine.run_turn("a"))
    assert len(other) == 1


def test_a_kept_connection_retries_an_initialize_timeout(tmp_path: Path) -> None:
    from dromon.engine import INITIALIZE_TIMEOUT

    factory = Factory([[result()]])
    attempts: list[int] = []

    def flaky(options: ClaudeAgentOptions) -> AbstractAsyncContextManager[KeptSession]:
        attempts.append(1)
        if len(attempts) == 1:

            @asynccontextmanager
            async def fail() -> AsyncGenerator[KeptSession]:
                raise Exception(INITIALIZE_TIMEOUT)  # noqa: TRY002
                yield cast(KeptSession, None)

            return fail()
        return factory(options)

    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=KEPT,
        environment={},
        kept_factory=flaky,
    )

    async def go() -> None:
        await engine.run_turn("one")
        await engine.close()

    asyncio.run(go())
    assert len(attempts) == 2 and len(factory.connections) == 1


# Review of round 4. F2, a failed start leaves no engine process behind.


def test_a_failed_sdk_connect_closes_its_transport_before_raising() -> None:
    """The retry after an initialize timeout relies on this: ClaudeSDKClient's
    connect disconnects on any failure after the process starts, which closes the
    transport and so ends the process, before it raises."""
    from claude_agent_sdk import ClaudeSDKClient
    from claude_agent_sdk._internal.transport import Transport

    events: list[str] = []

    class Failing(Transport):
        async def connect(self) -> None:
            events.append("connect")

        async def write(self, data: str) -> None:
            del data
            events.append("write")
            raise RuntimeError("the engine stopped answering")

        def read_messages(self) -> AsyncIterator[dict[str, Any]]:
            async def nothing() -> AsyncIterator[dict[str, Any]]:
                await asyncio.Event().wait()
                yield {}

            return nothing()

        async def close(self) -> None:
            events.append("close")

        def is_ready(self) -> bool:
            return True

        async def end_input(self) -> None:
            events.append("end_input")

    async def go() -> None:
        client = ClaudeSDKClient(options=ClaudeAgentOptions(), transport=Failing())
        with pytest.raises(Exception):  # noqa: B017, PT011
            await client.connect()

    asyncio.run(go())
    assert events[0] == "connect" and "close" in events
    assert events.index("close") > events.index("write")


# The engine's per-turn usage, which the SDK's parser drops.


def raw_result(per_turn: object) -> dict[str, Any]:
    return {
        "type": "result",
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 1,
        "session_id": "s",
        "total_cost_usd": 9.0,
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "model_usage_since_last_result": per_turn,
    }


PER_TURN = {
    "claude-opus-5-5": {
        "inputTokens": 100,
        "outputTokens": 50,
        "cacheReadInputTokens": 1000,
        "cacheCreationInputTokens": 10,
        "costUSD": 0.25,
    },
    "claude-haiku-5-5": {"inputTokens": 5, "outputTokens": 5, "costUSD": 0.01},
}


def test_the_sdks_parse_keeps_the_per_turn_usage_through_the_wrapper() -> None:
    from claude_agent_sdk._internal.message_parser import parse_message

    from dromon.engine import TurnResult, turn_usage_of, with_turn_usage

    data = raw_result(PER_TURN)
    message = with_turn_usage(cast(Message, parse_message(data)), data)
    assert isinstance(message, TurnResult)
    assert message.total_cost_usd == 9.0 and message.session_id == "s"
    found = turn_usage_of(message)
    assert found is not None
    cost, tokens = found
    assert cost == pytest.approx(0.26)
    assert tokens == {
        "input_tokens": 105,
        "output_tokens": 55,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 10,
    }
    plain = raw_result(None)
    assert not isinstance(
        with_turn_usage(cast(Message, parse_message(plain)), plain), TurnResult
    )


@pytest.mark.parametrize(
    "per_turn",
    [
        {"m": {"costUSD": -1.0}},
        {"m": {"costUSD": "0.1"}},
        {"m": {"costUSD": True}},
        {"m": {"costUSD": float("inf")}},
        {"m": "not an entry"},
        {},
    ],
)
def test_a_malformed_per_turn_usage_falls_back(per_turn: dict[str, Any]) -> None:
    from claude_agent_sdk._internal.message_parser import parse_message

    from dromon.engine import turn_usage_of, with_turn_usage

    data = raw_result(per_turn)
    message = with_turn_usage(cast(Message, parse_message(data)), data)
    assert turn_usage_of(cast(ResultMessage, message)) is None


def test_a_turn_prefers_the_per_turn_cost_and_moves_the_mark(tmp_path: Path) -> None:
    from claude_agent_sdk._internal.message_parser import parse_message

    from dromon.engine import DIFFERENCE_SOURCE, PER_TURN_SOURCE, with_turn_usage

    with_usage = raw_result(PER_TURN)
    without = raw_result(None)
    without["total_cost_usd"] = 9.5
    factory = Factory(
        [
            [with_turn_usage(cast(Message, parse_message(with_usage)), with_usage)],
            [cast(Message, parse_message(without))],
        ]
    )
    engine = line(factory, tmp_path)

    async def go() -> None:
        first = await engine.run_turn("one")
        assert first.cost_estimate_usd == pytest.approx(0.26)
        assert first.cost_source == PER_TURN_SOURCE
        assert first.usage is not None and first.usage["input_tokens"] == 105
        second = await engine.run_turn("two")
        # Without the per-turn figures, the difference from the mark is taken.
        assert second.cost_estimate_usd == pytest.approx(0.5)
        assert second.cost_source == DIFFERENCE_SOURCE
        assert second.usage == {"input_tokens": 1, "output_tokens": 2}
        await engine.close()

    asyncio.run(go())


def test_the_wrapper_reads_a_real_clients_messages_with_their_per_turn_usage() -> None:
    """Drives ClaudeSDKClient through a scripted transport, so an SDK upgrade that
    changes the internal query the wrapper reads fails here."""
    import json as json_module

    from claude_agent_sdk._internal.transport import Transport

    from dromon.engine import TurnResult, _SdkClient  # pyright: ignore[reportPrivateUsage]

    class Scripted(Transport):
        def __init__(self) -> None:
            self.out: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            self.closed = False

        async def connect(self) -> None:
            pass

        async def write(self, data: str) -> None:
            message = json_module.loads(data)
            if message.get("type") == "control_request":
                self.out.put_nowait(
                    {
                        "type": "control_response",
                        "response": {
                            "subtype": "success",
                            "request_id": message["request_id"],
                            "response": {},
                        },
                    }
                )
            elif message.get("type") == "user":
                self.out.put_nowait(raw_result(PER_TURN))

        def read_messages(self) -> AsyncIterator[dict[str, Any]]:
            async def messages() -> AsyncIterator[dict[str, Any]]:
                while not self.closed:
                    yield await self.out.get()

            return messages()

        async def close(self) -> None:
            self.closed = True

        def is_ready(self) -> bool:
            return True

        async def end_input(self) -> None:
            pass

    transport = Scripted()
    received: list[Message] = []

    async def go() -> None:
        async with _SdkClient(ClaudeAgentOptions(), transport) as session:
            await session.query("hello")
            async for message in session.receive_response():
                received.append(message)

    asyncio.run(go())
    assert len(received) == 1 and isinstance(received[0], TurnResult)
    assert received[0].turn_model_usage == PER_TURN


# Round 5. A and C, per-call usage and the context estimate.


def call(
    message_id: str, read: int, written: int = 10, parent: str | None = None
) -> AssistantMessage:
    return AssistantMessage(
        content=[TextBlock("x")],
        model=MODEL,
        parent_tool_use_id=parent,
        usage={
            "input_tokens": 5,
            "output_tokens": written,
            "cache_read_input_tokens": read,
            "cache_creation_input_tokens": 100,
        },
        message_id=message_id,
    )


def test_each_engine_call_is_counted_once_and_the_context_estimated(
    tmp_path: Path,
) -> None:
    factory = Factory(
        [
            [
                call("a", 1000),
                call("a", 1000),
                call("s", 50, parent="tu"),
                call("b", 2000),
                result(),
            ]
        ]
    )
    engine = line(factory, tmp_path)

    async def go() -> None:
        report = await engine.run_turn("one")
        assert [c["cache_read_input_tokens"] for c in report.calls] == [1000, 50, 2000]
        assert [c["subagent"] for c in report.calls] == [False, True, False]
        # The last main-loop call: 5 fresh, 2000 cached, 100 cache written, 10 out.
        assert report.context_tokens == 2115
        await engine.close()

    asyncio.run(go())


def test_calls_are_bounded_and_a_turn_without_them_estimates_nothing() -> None:
    from dromon.engine import MAX_CALLS, context_of, record_call

    calls: dict[str, dict[str, int | bool]] = {}
    for index in range(MAX_CALLS + 5):
        record_call(calls, call(f"m{index}", index))
    assert len(calls) == MAX_CALLS and next(iter(calls)) == "m5"
    record_call(calls, AssistantMessage(content=[], model=MODEL))
    assert len(calls) == MAX_CALLS
    assert context_of(None) is None
    assert context_of({"subagent": False}) is None
    assert context_of({"input_tokens": 9, "subagent": False}) == 9


# B, compaction at the start of a turn.


def boundary(before: object, after: object = None) -> SystemMessage:
    metadata: dict[str, object] = {"trigger": "manual", "pre_tokens": before}
    if after is not None:
        metadata["post_tokens"] = after
    return SystemMessage(
        subtype="compact_boundary", data={"compact_metadata": metadata}
    )


def test_a_kept_turn_compacts_first_and_says_before_and_after(tmp_path: Path) -> None:
    from dromon.engine import COMPACT_COMMAND

    factory = Factory([[boundary(180_000, 20_000), costed(0.5)], [costed(0.75)]])
    engine = line(factory, tmp_path)

    async def go() -> None:
        engine.request_compaction()
        report = await engine.run_turn("work")
        assert report.compaction == {
            "trigger": "manual",
            "before_tokens": 180_000,
            "after_tokens": 20_000,
        }
        # Both the compaction's and the prompt's costs count.
        assert report.cost_estimate_usd == pytest.approx(0.75)
        await engine.close()

    asyncio.run(go())
    prompts = factory.connections[0].prompts
    assert prompts[0] == COMPACT_COMMAND
    assert prompts[1].startswith(
        "Dromon note: your session was compacted at the start of this turn at your"
        " request. Context before: 180000 tokens. After: 20000 tokens."
    )
    assert prompts[1].endswith("work")


def test_a_compaction_the_engine_does_not_report_is_said_plainly(
    tmp_path: Path,
) -> None:
    sent: list[str] = []

    class Compacting(OneTurn):
        async def query(self, prompt: str) -> None:
            sent.append(prompt)

        async def receive_response(self) -> AsyncIterator[Message]:
            if len(sent) == 1:
                yield init()
            yield session_result("s", 0.1 * len(sent))

    @asynccontextmanager
    async def factory(options: ClaudeAgentOptions) -> AsyncGenerator[AgentSession]:
        del options
        yield Compacting("s", 0.0)

    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        environment={},
        session_factory=factory,
    )
    engine.request_compaction()
    report = asyncio.run(engine.run_turn("work"))
    assert sent[0] == "/compact"
    assert "the engine reported no compaction, so it may not have happened" in sent[1]
    assert report.compaction == {
        "trigger": None,
        "before_tokens": None,
        "after_tokens": None,
    }
    # The request is spent.
    asyncio.run(engine.run_turn("again"))
    assert sent[2] == "again"


def test_a_refused_compaction_sends_no_prompt(tmp_path: Path) -> None:
    factory = Factory([[boundary(10), result()], [result()]])
    factory.init_message = init(tools=["Bash"])
    engine = line(factory, tmp_path)

    async def go() -> None:
        engine.request_compaction()
        with pytest.raises(EngineRefused, match="tools the line was not given"):
            await engine.run_turn("work")

    asyncio.run(go())
    assert factory.connections[0].prompts == ["/compact"]


def test_compaction_metadata_is_read_defensively() -> None:
    from dromon.engine import compaction_note, compaction_of

    assert compaction_of({"pre_tokens": 5, "post_tokens": -1, "trigger": "x"}) == {
        "trigger": None,
        "before_tokens": 5,
        "after_tokens": None,
    }
    assert compaction_of("nonsense") == {
        "trigger": None,
        "before_tokens": None,
        "after_tokens": None,
    }
    assert "After: not reported." in compaction_note({"before_tokens": 5})


# Round 5 live fixes: a compaction is recorded when it happens, and the context
# estimate follows it.


def auto_boundary(before: int, after: int) -> SystemMessage:
    return SystemMessage(
        subtype="compact_boundary",
        data={
            "compact_metadata": {
                "trigger": "auto",
                "pre_tokens": before,
                "post_tokens": after,
            }
        },
    )


def test_a_compaction_is_reported_before_the_prompt_is_sent(tmp_path: Path) -> None:
    factory = Factory([[boundary(30_074, 3_655), result()], [result()]])
    seen: list[tuple[dict[str, object], list[str]]] = []
    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=KEPT,
        environment={},
        kept_factory=factory,
        compacted=lambda info: seen.append(
            (dict(info), list(factory.connections[0].prompts))
        ),
    )

    async def go() -> None:
        engine.request_compaction()
        report = await engine.run_turn("work")
        # No call followed the compaction, so the estimate is what it left.
        assert report.context_tokens == 3_655
        await engine.close()

    asyncio.run(go())
    assert seen == [
        (
            {"trigger": "manual", "before_tokens": 30_074, "after_tokens": 3_655},
            ["/compact"],
        )
    ]


def test_the_estimate_counts_only_calls_after_a_compaction(tmp_path: Path) -> None:
    seen: list[dict[str, object]] = []
    factory = Factory(
        [
            [
                call("before", 29_000),
                auto_boundary(29_891, 4_000),
                call("after", 4_100),
                result(),
            ]
        ]
    )
    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=KEPT,
        environment={},
        kept_factory=factory,
        compacted=lambda info: seen.append(dict(info)),
    )

    async def go() -> None:
        report = await engine.run_turn("work")
        # 5 fresh, 4100 cached, 100 cache written, 10 written by the call after.
        assert report.context_tokens == 4_215
        assert report.compaction is not None and report.compaction["trigger"] == "auto"
        await engine.close()

    asyncio.run(go())
    assert seen == [{"trigger": "auto", "before_tokens": 29_891, "after_tokens": 4_000}]


def test_a_failing_compaction_record_does_not_end_the_turn(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    factory = Factory([[auto_boundary(10, 5), result()]])

    def broken(info: object) -> None:
        del info
        raise RuntimeError("no record")

    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=KEPT,
        environment={},
        kept_factory=factory,
        compacted=broken,
    )

    async def go() -> None:
        await engine.run_turn("work")
        await engine.close()

    asyncio.run(go())
    assert "a compaction could not be recorded: no record" in capsys.readouterr().err


# Review of round 5. F2, no compaction after a refused init.


def test_no_compaction_is_sent_once_the_init_was_refused(tmp_path: Path) -> None:
    from dromon.engine import _Turn  # pyright: ignore[reportPrivateUsage]

    refused = _Turn()
    refused.refusal = "the engine offers tools the line was not given: Bash"
    # Round 6 review, F2. The request is the turn's own, taken as it starts, so a
    # refused turn leaves nothing for a later one, as test_round6.py checks.
    assert not ClaudeCodeLine._compaction_allowed(refused)  # pyright: ignore[reportPrivateUsage]
    assert ClaudeCodeLine._compaction_allowed(_Turn())  # pyright: ignore[reportPrivateUsage]


# Rows 1, 4 and 5 of the T3 Code review, and the operator's decisions A and B.


class Asked:
    """Stands for the scheduler at a connection's ceiling."""

    def __init__(self) -> None:
        self.asked: list[int] = []
        self.withdrawn: list[str] = []

    def ask(self, tasks: int) -> str:
        self.asked.append(tasks)
        return f"q{len(self.asked)}"

    def settle(self, question_id: str) -> None:
        self.withdrawn.append(question_id)


ASK = LineConfig(
    keep_session=True,
    keep_session_idle_seconds=60,
    keep_session_max_seconds=600,
    keep_session_hard_seconds=3600,
    keep_session_at_max=AtMax.ASK,
)


def asking(
    factory: Factory, tmp_path: Path, clock: Clock, asked: Asked
) -> ClaudeCodeLine:
    return ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=ASK,
        environment={},
        kept_factory=factory,
        clock=clock,
        poll_seconds=3600.0,
        ceiling_reached=asked.ask,
        ceiling_settled=asked.settle,
    )


def test_a_kept_line_asks_at_its_ceiling_and_carries_out_the_answer(
    tmp_path: Path,
) -> None:
    clock = Clock()
    asked = Asked()
    factory = Factory([[started("t1"), result()], [result()]])
    engine = asking(factory, tmp_path, clock, asked)

    async def go() -> None:
        await engine.run_turn("first")
        clock.now += 600
        assert not await engine.expire()
        assert asked.asked == [1]
        assert engine.seconds_to_close() == 3000
        assert await engine.decide_ceiling("q9", "stop") is None
        assert await engine.decide_ceiling("q1", "extend") is Choice.EXTEND
        assert engine.seconds_to_close() == 600
        clock.now += 600
        assert not await engine.expire()
        assert asked.asked == [1, 1]
        assert await engine.decide_ceiling("q2", "stop") is Choice.STOP
        assert engine.kept_status()["kept"] is False
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert factory.connections[0].closed
    note = closed_note(STOP_REASON, 1)
    assert factory.connections[1].prompts[0].startswith(note)


def test_an_unanswered_ceiling_closes_at_the_hard_bound_and_withdraws(
    tmp_path: Path,
) -> None:
    clock = Clock()
    asked = Asked()
    factory = Factory([[started("t1"), result()]])
    engine = asking(factory, tmp_path, clock, asked)

    async def go() -> None:
        await engine.run_turn("first")
        clock.now += 600
        assert not await engine.expire()
        clock.now += 2999
        assert not await engine.expire()
        clock.now += 1
        assert await engine.expire()

    asyncio.run(go())
    assert asked.withdrawn == ["q1"]
    assert engine.kept_status()["kept"] is False
    assert closed_note(HARD_REASON, 1) in engine._notes  # pyright: ignore[reportPrivateUsage]


def test_a_turn_withdraws_an_open_ceiling_question(tmp_path: Path) -> None:
    clock = Clock()
    asked = Asked()
    factory = Factory([[started("t1"), result()], [result()]])
    engine = asking(factory, tmp_path, clock, asked)

    async def go() -> None:
        await engine.run_turn("first")
        clock.now += 600
        await engine.expire()
        await engine.run_turn("second")
        assert engine.seconds_to_close() == 600
        await engine.close()

    asyncio.run(go())
    assert asked.withdrawn == ["q1"]


def test_an_operators_stop_closes_a_connection_that_still_runs_work(
    tmp_path: Path,
) -> None:
    """Row 1. The interrupt settles, but a background task still runs, so the stop
    escalates to closing the engine."""
    factory = Factory([[started("t1")], [result()]])
    engine = line(factory, tmp_path)

    async def go() -> str | None:
        turn = asyncio.create_task(engine.run_turn("first"))
        await settle()
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn
        reason = await engine.halt()
        await engine.run_turn("second")
        await engine.close()
        return reason

    assert asyncio.run(go()) == HALT_REASON
    assert factory.connections[0].closed and factory.connections[0].interrupts == 1
    assert factory.connections[1].prompts[0].startswith(closed_note(HALT_REASON, 1))


def test_an_operators_stop_that_settled_without_work_keeps_the_connection(
    tmp_path: Path,
) -> None:
    factory = Factory([[say("working")]])
    engine = line(factory, tmp_path)

    async def go() -> str | None:
        turn = asyncio.create_task(engine.run_turn("first"))
        await settle()
        turn.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await turn
        # The engine answers the interrupt with the cancelled turn's result.
        factory.connections[0].stream.put_nowait(result())
        await settle()
        reason = await engine.halt()
        assert engine.kept_status()["kept"] is True
        await engine.close()
        return reason

    assert asyncio.run(go()) is None
    assert factory.connections[0].interrupts == 1


def test_a_stop_without_a_connection_has_nothing_to_halt(tmp_path: Path) -> None:
    assert asyncio.run(line(Factory([]), tmp_path).halt()) is None


def test_a_result_with_no_turn_behind_it_is_not_a_turn(tmp_path: Path) -> None:
    """Row 5. A result between turns with ``num_turns`` of zero, as a resume's
    answer, is neither noted as a turn the engine began nor interrupted, and its
    cost, if any, is still counted, as a handshake."""
    spent: list[str] = []
    factory = Factory([[result()], [result()]])
    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=KEPT,
        environment={},
        kept_factory=factory,
        clock=Clock(),
        poll_seconds=3600.0,
        spent_between=lambda kind, cost, usage: spent.append(kind),
    )

    async def go() -> None:
        await engine.run_turn("first")
        handshake = result()
        handshake.num_turns = 0
        factory.connections[0].stream.put_nowait(handshake)
        await settle()
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert spent == [HANDSHAKE_KIND]
    assert factory.connections[0].interrupts == 0
    assert SELF_STARTED_NOTE not in factory.connections[0].prompts[1]
    assert is_handshake(result()) is False


def observed(factory: Factory, tmp_path: Path, log: ActivityLog) -> ClaudeCodeLine:
    return ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=KEPT,
        environment={},
        kept_factory=factory,
        clock=Clock(),
        poll_seconds=3600.0,
        observe=log.observer("alpha"),
    )


def notices(log: ActivityLog) -> list[str]:
    return [e.text for e in log.after("alpha", 0) if e.kind is ActivityKind.NOTICE]


def test_an_unhandled_system_message_is_surfaced_once(tmp_path: Path) -> None:
    """Row 5. Never passed over silently, and never repeated, while handled
    subtypes, the background task reports among them, are not surfaced."""
    odd = SystemMessage(subtype="turn_duration", data={})
    status = SystemMessage(subtype="status", data={})
    # Seen live on 2026-10-27, a progress estimate while the model thinks.
    thinking = SystemMessage(
        subtype="thinking_tokens",
        data={"estimated_tokens": 120, "estimated_tokens_delta": 40},
    )
    factory = Factory(
        [[odd, status, thinking, odd, started("t1"), result()], [odd, result()]]
    )
    log = ActivityLog()
    engine = observed(factory, tmp_path, log)

    async def go() -> None:
        await engine.run_turn("first")
        factory.connections[0].stream.put_nowait(
            SystemMessage(subtype="ui_toast", data={})
        )
        await settle()
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert notices(log) == [
        'the engine sent a system message of subtype "turn_duration", which Dromon'
        " does not handle",
        'the engine sent a system message of subtype "ui_toast", which Dromon does'
        " not handle",
    ]
    assert {
        "init",
        "task_started",
        "compact_boundary",
        "thinking_tokens",
    } <= HANDLED_SYSTEM_SUBTYPES


def fallback(subtype: str, moved_to: str | None) -> SystemMessage:
    data: dict[str, Any] = {"original_model": "claude-fable-5-1"}
    if moved_to is not None:
        data["fallback_model"] = moved_to
    if subtype == CONSENT_FALLBACK:
        data["choice"] = "cancelled"
    return SystemMessage(subtype=subtype, data=data)


def test_a_fallback_to_another_model_refuses_the_turn(tmp_path: Path) -> None:
    """Review F16. The model the engine went on with is not the driver's, or is not
    named, which counts as another."""
    for subtype in sorted(FALLBACK_SUBTYPES):
        for moved_to in ("claude-opus-5-5", None):
            factory = Factory([[fallback(subtype, moved_to), result()]])
            log = ActivityLog()
            engine = observed(factory, tmp_path, log)
            with pytest.raises(EngineRefused, match=f"not the model {MODEL}"):
                asyncio.run(engine.run_turn("first"))
            assert factory.connections[0].interrupts == 1
            assert len(notices(log)) == 1


def test_a_fallback_to_the_driver_s_own_model_goes_on(tmp_path: Path) -> None:
    """Review F16. Only a model the driver was not written for is refused."""
    for subtype in sorted(FALLBACK_SUBTYPES):
        factory = Factory([[fallback(subtype, MODEL), result()]])
        log = ActivityLog()
        engine = observed(factory, tmp_path, log)
        asyncio.run(engine.run_turn("first"))
        assert factory.connections[0].interrupts == 0
        assert len(notices(log)) == 1


def test_a_consent_fallback_is_put_to_an_operator(tmp_path: Path) -> None:
    """Review F16. The consent is a human's decision, so it becomes a question,
    whether or not the turn goes on."""
    for moved_to, stopped in (("claude-opus-5-5", True), (MODEL, False)):
        asked: list[str] = []
        factory = Factory([[fallback(CONSENT_FALLBACK, moved_to), result()]])
        engine = ClaudeCodeLine(
            Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
            tmp_path,
            config=KEPT,
            environment={},
            kept_factory=factory,
            clock=Clock(),
            poll_seconds=3600.0,
            consent_asked=asked.append,
        )
        if stopped:
            with pytest.raises(EngineRefused):
                asyncio.run(engine.run_turn("first"))
        else:
            asyncio.run(engine.run_turn("first"))
        (text,) = asked
        assert text == consent_question(
            fallback(CONSENT_FALLBACK, moved_to).data, MODEL, stopped
        )
        assert f"moved its session to {moved_to}" in text
        assert ("Dromon stopped the turn" in text) is stopped


def test_a_failing_consent_question_does_not_end_the_turn(tmp_path: Path) -> None:
    def broken(text: str) -> None:
        raise RuntimeError("no operator")

    factory = Factory([[fallback(CONSENT_FALLBACK, MODEL), result()]])
    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=KEPT,
        environment={},
        kept_factory=factory,
        clock=Clock(),
        poll_seconds=3600.0,
        consent_asked=broken,
    )
    asyncio.run(engine.run_turn("first"))


def test_fallback_fields_are_bounded() -> None:
    assert fallback_text("x" * 500) == "x" * 80
    for missing in (None, "", 7):
        assert fallback_text(missing) == "an unnamed one"


def test_a_connection_that_dies_withdraws_its_ceiling_question(
    tmp_path: Path,
) -> None:
    """Review F12. The engine's process ends without a close."""
    clock = Clock()
    asked = Asked()
    factory = Factory([[started("t1"), result()]])
    engine = asking(factory, tmp_path, clock, asked)

    async def go() -> None:
        await engine.run_turn("first")
        clock.now += 600
        await engine.expire()
        assert asked.asked == [1] and asked.withdrawn == []
        factory.connections[0].die()
        await settle()
        assert asked.withdrawn == ["q1"]
        await engine.close()

    asyncio.run(go())
    assert asked.withdrawn == ["q1"]


def test_every_claude_line_pins_tool_search_and_runs_under_the_launcher(
    tmp_path: Path,
) -> None:
    """Row 4, and decision B. The pin holds whatever the daemon's own environment
    says, since the engine's environment takes the line's values over it."""
    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        environment={"ENABLE_TOOL_SEARCH": "auto"},
    )
    options = engine.options()
    assert options.env[TOOL_SEARCH_VARIABLE] == TOOL_SEARCH_PINNED == "true"
    assert options.cli_path == launcher.script_path()
    assert options.env[launcher.TARGET_VARIABLE] == claude_cli()
    assert options.env[launcher.STOP_POLICY_VARIABLE] == "kill_all"
    leave = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=LineConfig(stop_policy=StopPolicy.LEAVE_BACKGROUND),
    )
    assert leave.options().env[launcher.STOP_POLICY_VARIABLE] == "leave_background"
    assert TOOL_SEARCH_VARIABLE in PINNED_VARIABLES


def test_the_pin_is_also_in_the_flag_settings_env_block(tmp_path: Path) -> None:
    """Review F9. Claude Code copies its global configuration's env block over its
    process environment, and the flag settings' env block after that."""
    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        settings={"env": {"SCCACHE_DIR": "/cache", TOOL_SEARCH_VARIABLE: "auto"}},
    )
    text = engine.options().settings
    assert text is not None
    assert json.loads(text)["env"] == {
        "SCCACHE_DIR": "/cache",
        TOOL_SEARCH_VARIABLE: TOOL_SEARCH_PINNED,
    }
    assert flag_settings(None) == {"env": {TOOL_SEARCH_VARIABLE: TOOL_SEARCH_PINNED}}
    assert flag_settings({"env": "odd"}) == {
        "env": {TOOL_SEARCH_VARIABLE: TOOL_SEARCH_PINNED}
    }


def test_a_missing_launcher_refuses_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing() -> str:
        raise FileNotFoundError("dromon-launch is not installed")

    monkeypatch.setattr(launcher, "script_path", missing)
    engine = ClaudeCodeLine(Driver.for_model(MODEL, Strategy.SUBSCRIPTION), tmp_path)
    with pytest.raises(EngineRefused, match="launcher is not installed"):
        engine.options()


def test_the_gate_refuses_every_request_for_a_human_decision() -> None:
    """Row 5. Even a line configured with the tools, whatever its policy."""
    config = LineConfig(
        builtin_tools=tuple(sorted(HUMAN_DECISION_TOOLS)),
        tool_policy=ToolPolicy.ALLOW_ALL,
    )
    gate = permission_gate(config, frozenset())
    for name in sorted(HUMAN_DECISION_TOOLS):
        verdict = asyncio.run(gate(name, {}, CONTEXT))
        assert isinstance(verdict, PermissionResultDeny)
        assert verdict.message == HUMAN_DECISION_REFUSAL
        assert "ask_operator" in verdict.message
    allowed = asyncio.run(gate("Read", {}, CONTEXT))
    assert isinstance(allowed, PermissionResultAllow)


# Item 6 of the live check of 2026-10-26, a resumed session's handoff result.


def handoff() -> ResultMessage:
    """The shape observed live and read from the bundled engine: a success with no
    turn, no text and no cost, which ran nothing."""
    return ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=0,
        session_id="session-1",
        total_cost_usd=0.0,
        result="",
    )


def counted(
    factory: Factory, tmp_path: Path, spent: list[str], log: ActivityLog
) -> ClaudeCodeLine:
    return ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=KEPT,
        environment={},
        kept_factory=factory,
        clock=Clock(),
        poll_seconds=3600.0,
        spent_between=lambda kind, cost, usage: spent.append(kind),
        observe=log.observer("alpha"),
    )


def test_a_handoff_inside_a_turn_sends_the_prompt_again(tmp_path: Path) -> None:
    factory = Factory([[handoff()], [say("answered"), result()]])
    spent: list[str] = []
    log = ActivityLog()
    engine = counted(factory, tmp_path, spent, log)

    async def go() -> TurnReport:
        report = await engine.run_turn("read your mail")
        await engine.close()
        return report

    report = asyncio.run(go())
    assert (report.text, report.num_turns) == ("answered", 1)
    assert factory.connections[0].prompts == ["read your mail", "read your mail"]
    assert spent == [HANDSHAKE_KIND]
    assert notices(log) == [HANDOFF_NOTICE]
    assert is_handoff(handoff()) and not is_handoff(result())


def test_a_second_handoff_refuses_the_turn(tmp_path: Path) -> None:
    factory = Factory([[handoff()], [handoff()]])
    spent: list[str] = []
    engine = counted(factory, tmp_path, spent, ActivityLog())

    async def go() -> None:
        with pytest.raises(EngineRefused, match="without running its prompt"):
            await engine.run_turn("read your mail")
        assert engine.kept_status()["kept"] is False

    asyncio.run(go())
    assert len(factory.connections[0].prompts) == 1 + HANDOFF_RESENDS
    assert spent == [HANDSHAKE_KIND, HANDSHAKE_KIND]
    assert HANDOFF_REFUSAL.startswith("the engine ended the turn again")


def test_a_zero_turn_result_that_is_not_a_handoff_stays_the_outcome(
    tmp_path: Path,
) -> None:
    """An error with no turn is the turn's honest outcome, and a zero-turn result
    after the model's output is the turn's own."""
    failed = handoff()
    failed.subtype = "error_during_execution"
    failed.is_error = True
    after_output = handoff()
    factory = Factory([[failed], [say("partial"), after_output]])
    spent: list[str] = []
    engine = counted(factory, tmp_path, spent, ActivityLog())

    async def go() -> list[TurnReport]:
        reports = [await engine.run_turn(f"turn {n}") for n in range(2)]
        await engine.close()
        return reports

    reports = asyncio.run(go())
    assert [r.subtype for r in reports] == ["error_during_execution", "success"]
    assert reports[1].text == "partial"
    assert factory.connections[0].prompts == ["turn 0", "turn 1"]
    assert spent == []


def test_a_worded_zero_turn_success_before_output_is_a_handoff(
    tmp_path: Path,
) -> None:
    """Review N5. A short text, such as a resumed session's own note, shows no more
    than an empty one that the prompt was run, so the prompt is sent again and the
    text is shown as a notice, not taken as the answer."""
    worded = handoff()
    worded.result = "resumed"
    factory = Factory([[worded], [say("answered"), result()]])
    spent: list[str] = []
    log = ActivityLog()
    engine = counted(factory, tmp_path, spent, log)

    async def go() -> TurnReport:
        report = await engine.run_turn("read your mail")
        await engine.close()
        return report

    report = asyncio.run(go())
    assert report.text == "answered"
    assert factory.connections[0].prompts == ["read your mail", "read your mail"]
    assert spent == [HANDSHAKE_KIND]
    assert notices(log) == [handoff_text_notice("resumed"), HANDOFF_NOTICE]
    assert is_handoff(worded)


def test_a_handoff_text_is_quoted_and_cut() -> None:
    long = "x" * (MAX_HANDOFF_TEXT_CHARACTERS + 50)
    notice = handoff_text_notice(long)
    assert repr("x" * MAX_HANDOFF_TEXT_CHARACTERS) in notice
    assert "x" * (MAX_HANDOFF_TEXT_CHARACTERS + 1) not in notice


class Batches:
    """A one-shot session whose every prompt takes the next batch of the script."""

    def __init__(self, script: list[list[Message]]) -> None:
        self.script = script
        self.prompts: list[str] = []

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_response(self) -> AsyncIterator[Message]:
        for message in self.script.pop(0):
            yield message

    async def interrupt(self) -> None:
        return None

    def factory(
        self, options: ClaudeAgentOptions
    ) -> AbstractAsyncContextManager[AgentSession]:
        del options

        @asynccontextmanager
        async def opened() -> AsyncGenerator[AgentSession]:
            yield self

        return opened()


def test_a_handoff_on_a_one_shot_turn_sends_the_prompt_again(tmp_path: Path) -> None:
    session = Batches([[init(), handoff()], [say("answered"), result()]])
    spent: list[str] = []
    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=DEFAULT_CONFIG,
        environment={},
        session_factory=session.factory,
        spent_between=lambda kind, cost, usage: spent.append(kind),
    )
    report = asyncio.run(engine.run_turn("read your mail"))
    assert report.text == "answered"
    assert session.prompts == ["read your mail", "read your mail"]
    assert spent == [HANDSHAKE_KIND]
    # The driver saw one turn start and one result.
    assert engine.driver.state == (0, 1, 1)


def test_a_second_handoff_on_a_one_shot_turn_refuses_it(tmp_path: Path) -> None:
    session = Batches([[init(), handoff()], [handoff()]])
    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=DEFAULT_CONFIG,
        environment={},
        session_factory=session.factory,
    )
    with pytest.raises(EngineRefused, match="without running its prompt"):
        asyncio.run(engine.run_turn("read your mail"))
    assert len(session.prompts) == 1 + HANDOFF_RESENDS


# Item 8 of the live check of 2026-10-26, the engine's whole task set.


def task_set(*tasks: tuple[str, str]) -> SystemMessage:
    """``background_tasks_changed`` in the shape of the bundled engine's schema."""
    return SystemMessage(
        subtype=TASKS_CHANGED,
        data={
            "type": "system",
            "subtype": TASKS_CHANGED,
            "tasks": [
                {"task_id": task_id, "task_type": "local_bash", "description": text}
                for task_id, text in tasks
            ],
            "uuid": "u",
            "session_id": "session-1",
        },
    )


def test_the_task_set_removes_ended_tasks_between_turns_and_keeps_their_note(
    tmp_path: Path,
) -> None:
    factory = Factory(
        [[bash("tu1", "sleep 611"), launched("t1", "tu1"), task_set(), result()]]
    )
    ends: list[BackgroundEnd] = []
    engine = background_line(factory, tmp_path, ends)

    async def go() -> None:
        await engine.run_turn("first")
        # During the turn a set without t1 removed nothing.
        assert engine.kept_status()["background_tasks"] == 1
        connection = factory.connections[0]
        connection.stream.put_nowait(task_set())
        await settle()
        assert engine.kept_status()["background_tasks"] == 0
        connection.stream.put_nowait(notified("t1"))
        await settle()
        await engine.close()

    asyncio.run(go())
    assert ends == [BackgroundEnd("t1", "sleep 611", "completed", None, "out.txt")]


def test_the_task_set_adds_a_task_whose_start_was_not_seen(tmp_path: Path) -> None:
    factory = Factory([[task_set(("t9", "a monitor")), result()]])
    log = ActivityLog()
    engine = observed(factory, tmp_path, log)

    async def go() -> None:
        await engine.run_turn("first")
        assert engine.kept_status()["tasks"] == [
            {"task_id": "t9", "command": "a monitor"}
        ]
        await engine.close()

    asyncio.run(go())
    assert TASKS_CHANGED in HANDLED_SYSTEM_SUBTYPES
    assert not any(TASKS_CHANGED in text for text in notices(log))


def test_a_malformed_task_set_changes_nothing(tmp_path: Path) -> None:
    factory = Factory([[bash("tu1", "sleep 611"), launched("t1", "tu1"), result()]])
    engine = background_line(factory, tmp_path, [])
    malformed = [
        SystemMessage(subtype=TASKS_CHANGED, data={}),
        SystemMessage(subtype=TASKS_CHANGED, data={"tasks": "none"}),
        # Overlong, so its unread tail might name t1.
        SystemMessage(subtype=TASKS_CHANGED, data={"tasks": [7] * 300}),
    ]

    async def go() -> None:
        await engine.run_turn("first")
        for message in malformed:
            factory.connections[0].stream.put_nowait(message)
        await settle()
        assert engine.kept_status()["background_tasks"] == 1
        # Entries that are not tasks are skipped, and the rest still count.
        factory.connections[0].stream.put_nowait(
            SystemMessage(
                subtype=TASKS_CHANGED,
                data={"tasks": [7, {"task_id": ""}, {"task_id": "t1"}]},
            )
        )
        await settle()
        assert engine.kept_status()["background_tasks"] == 1
        await engine.close()

    asyncio.run(go())


# Item 7 of the live check of 2026-10-26, the wording under leave_background.


def test_the_closed_note_says_what_the_stop_policy_did() -> None:
    assert "were stopped with it" in closed_note("a reason", 2)
    left = closed_note("a reason", 2, left=True)
    assert "were not ended by Dromon" in left and "stopped with it" not in left
    assert closed_note("a reason", 0, left=True) == closed_note("a reason", 0)


def test_a_kept_line_under_leave_background_says_its_tasks_were_left(
    tmp_path: Path,
) -> None:
    factory = Factory(
        [[bash("tu1", "sleep 613"), launched("t1", "tu1"), result()], [result()]]
    )
    engine = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=LineConfig(
            keep_session=True,
            builtin_tools=("Bash",),
            stop_policy=StopPolicy.LEAVE_BACKGROUND,
        ),
        environment={},
        sandbox={"enabled": True},
        kept_factory=factory,
        clock=Clock(),
        poll_seconds=3600.0,
    )

    async def go() -> None:
        await engine.run_turn("first")
        await engine.close("a reason")
        await engine.run_turn("second")
        await engine.close()

    asyncio.run(go())
    assert (
        factory.connections[1]
        .prompts[0]
        .startswith(closed_note("a reason", 1, left=True))
    )


# Review N10 of the stop policy's re-review. A task's command and description reach
# the line's status and its end's note, which is logged, so secrets are masked.


def test_a_task_s_command_and_description_are_masked_as_taken_in(
    tmp_path: Path,
) -> None:
    carried = "curl -H 'Authorization: Bearer s3cret' https://ana:pw@example.com/x"
    factory = Factory(
        [
            [
                bash("tu1", carried),
                launched("t1", "tu1"),
                task_set(("t1", carried), ("t2", "deploy --token tok123")),
                result(),
            ]
        ]
    )
    engine = background_line(factory, tmp_path, [])

    async def go() -> list[dict[str, str]]:
        await engine.run_turn("first")
        tasks = cast(list[dict[str, str]], engine.kept_status()["tasks"])
        await engine.close()
        return tasks

    tasks = asyncio.run(go())
    assert tasks == [
        {"task_id": "t1", "command": redact.mask(carried)},
        {"task_id": "t2", "command": "deploy --token ***"},
    ]
    shown = json.dumps(tasks)
    for leaked in ("s3cret", "ana:pw", "tok123"):
        assert leaked not in shown
