"""A Claude Code line that keeps its engine connected between turns, driven by a
scripted connection that behaves as the bundled engine was read to behave.

The script's engine reports its init on the first prompt of a connection, and again
only after ``set_permission_mode``, as Claude Code 2.1.310's engine loop does.
"""

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

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
)

from dromon.driver import Driver, Strategy
from dromon.engine import (
    DIED_NOTE,
    MAX_FINGERPRINT_FILE_BYTES,
    SELF_STARTED_NOTE,
    ClaudeCodeLine,
    EngineRefused,
    KeptSession,
    begins_turn,
    closed_note,
    permission_gate,
    permission_mode_of,
    task_ended,
    tree_digest,
)
from dromon import engine as engine_module
from dromon.lineconfig import (
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
            "apiKeySource": source,
            "tools": list(tools),
            "permissionMode": "default",
        },
    )


def result(session: str = "session-1") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=session,
        total_cost_usd=0.001,
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
) -> ClaudeCodeLine:
    return ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        tmp_path,
        config=config,
        environment={},
        kept_factory=factory,
        clock=clock or Clock(),
        poll_seconds=poll,
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
        connection.stream.put_nowait(notified("t1"))
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


def test_codex_and_grok_lines_refuse_keep_session() -> None:
    assert "do not yet keep" in (codex.unsupported(KEPT) or "")
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
        "on, closing after 1800 seconds idle, or 14400 with background tasks running"
    )
