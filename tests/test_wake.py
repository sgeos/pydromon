"""Scheduled wakes: the ``wake_after`` tool, the ``wake_scheduled`` event, firing the
note as a message from the line to itself, and what survives a restart or a crash."""

import asyncio
import json
from collections.abc import Callable, Coroutine, Generator, Sequence
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import scheduler as scheduler_module
from dromon.codec import DecodeError, decode, encode
from dromon.configview import Overrides
from dromon.daemon import Daemon
from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog, read_entries
from dromon.limits import Limits
from dromon.model import (
    MAX_WAKE_NOTE_BYTES,
    EventRejected,
    InvalidField,
    LineAddress,
    LineCreated,
    MessageKind,
    MessageSent,
    OperatorAddress,
    State,
    WakeScheduled,
    format_due,
    parse_due,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ProjectConfig
from dromon.scheduler import (
    WAKE_TOOL_NAME,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
START = datetime(2026, 10, 26, 9, 0, 0, tzinfo=UTC)


# The event and the model.


def state_with_alpha() -> State:
    state = State()
    state.apply(LineCreated("alpha", HAIKU, "subscription"))
    state.apply(LineCreated("beta", HAIKU, "subscription"))
    return state


@pytest.mark.parametrize(
    ("due", "note", "message"),
    [
        ("2026-10-26 09:30:00Z", "x", "of the form"),
        ("2026-10-26T09:30:00+00:00", "x", "due is 25 bytes"),
        ("2026-10-26T09:30:00.5Z", "x", "due is"),
        ("2026-13-01T00:00:00Z", "x", "not a valid time"),
        ("2026-02-30T00:00:00Z", "x", "not a valid time"),
        ("2026-10-26T09:30:00Z", "x" * (MAX_WAKE_NOTE_BYTES + 1), "note is"),
        ("2026-10-26T09:30:00Z", "\ud800", "not valid Unicode"),
    ],
)
def test_a_malformed_scheduled_wake_is_refused(
    due: str, note: str, message: str
) -> None:
    with pytest.raises(InvalidField, match=message):
        WakeScheduled("alpha", "s1", due, note)


def test_a_note_of_exactly_the_limit_is_accepted_and_round_trips() -> None:
    event = WakeScheduled("alpha", "s1", "2026-10-26T09:30:00Z", "é" * 1000)
    assert decode(encode(event)) == event
    payload = json.loads(encode(event))
    payload["data"]["due"] = "tomorrow"
    with pytest.raises(DecodeError, match="invalid wake_scheduled event"):
        decode(json.dumps(payload))


def test_due_times_are_utc_to_the_second() -> None:
    assert format_due(START) == "2026-10-26T09:00:00Z"
    tokyo = START.astimezone(timezone(timedelta(hours=9)))
    assert format_due(tokyo) == "2026-10-26T09:00:00Z"
    assert parse_due("2026-10-26T09:00:00Z") == START
    with pytest.raises(InvalidField, match="aware"):
        format_due(datetime(2026, 10, 26, 9, 0, 0))


def test_a_new_wake_replaces_the_pending_one_and_its_note_fires_once() -> None:
    state = state_with_alpha()
    state.apply(WakeScheduled("alpha", "s1", "2026-10-26T09:30:00Z", "first"))
    state.apply(WakeScheduled("alpha", "s2", "2026-10-26T10:00:00Z", "second"))
    assert state.scheduled["alpha"].message_id == "s2"
    with pytest.raises(EventRejected, match="cannot message itself"):
        state.check(MessageSent("s1", ALPHA, ALPHA, MessageKind.NOTE, "first"))
    state.apply(MessageSent("s2", ALPHA, ALPHA, MessageKind.NOTE, "second"))
    assert "alpha" not in state.scheduled
    assert state.messages["s2"].sender == ALPHA == state.messages["s2"].recipient
    with pytest.raises(EventRejected, match="exists"):
        state.check(MessageSent("s2", ALPHA, ALPHA, MessageKind.NOTE, "second"))


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (
            MessageSent("s1", ALPHA, ALPHA, MessageKind.NOTE, "other text"),
            "cannot message itself",
        ),
        (
            MessageSent("m9", ALPHA, ALPHA, MessageKind.NOTE, "first"),
            "cannot message itself",
        ),
        (
            MessageSent("s1", LineAddress("beta"), ALPHA, MessageKind.NOTE, "first"),
            "reserved by line alpha's scheduled wake",
        ),
        (
            WakeScheduled("beta", "s1", "2026-10-26T09:30:00Z", "steal"),
            "reserved by line alpha's scheduled wake",
        ),
        (
            WakeScheduled("gamma", "s3", "2026-10-26T09:30:00Z", "x"),
            "no line gamma",
        ),
        (
            WakeScheduled("beta", "m0", "2026-10-26T09:30:00Z", "x"),
            "message m0 exists",
        ),
    ],
)
def test_the_model_keeps_self_messages_to_the_reserved_note(
    event: MessageSent | WakeScheduled, message: str
) -> None:
    state = state_with_alpha()
    state.apply(MessageSent("m0", ALPHA, LineAddress("beta"), MessageKind.NOTE, "hi"))
    state.apply(WakeScheduled("alpha", "s1", "2026-10-26T09:30:00Z", "first"))
    with pytest.raises(EventRejected, match=message):
        state.check(event)


def test_a_reply_to_the_note_is_not_a_firing() -> None:
    state = state_with_alpha()
    state.apply(WakeScheduled("alpha", "s1", "2026-10-26T09:30:00Z", "first"))
    state.apply(MessageSent("s1", ALPHA, ALPHA, MessageKind.NOTE, "first"))
    state.apply(WakeScheduled("alpha", "s2", "2026-10-26T09:40:00Z", "again"))
    with pytest.raises(EventRejected, match="cannot message itself"):
        state.check(
            MessageSent("s2", ALPHA, ALPHA, MessageKind.NOTE, "again", reply_to="s1")
        )


# The scheduler.


class Clock:
    """A wall clock the test moves by hand."""

    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


class Recorder:
    def __init__(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> None:
        self.spec = spec
        self.session_id = spec.session_id
        self.tools = {tool.name: tool for tool in tools}
        self.prompts: list[str] = []

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        self.session_id = self.session_id or "session-1"
        return TurnReport(
            "success", 1, "ok", (), None, None, self.session_id, None, None
        )


class World:
    def __init__(self, path: Path, clock: Clock, limits: Limits | None = None) -> None:
        self.path = path
        self.clock = clock
        self.engines: dict[str, Recorder] = {}
        self.log = EventLog(path)
        self.limits = limits or Limits()
        self.scheduler = self.restart()
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
        engine = Recorder(spec, tools)
        self.engines[spec.line_id] = engine
        return engine

    def restart(self) -> Scheduler:
        self.scheduler = Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            limits=self.limits,
            wall_clock=self.clock,
        )
        return self.scheduler

    def tool(self, name: str = WAKE_TOOL_NAME) -> SdkMcpTool[Any]:
        found = {t.name: t for t in self.scheduler.own_tools("alpha")}
        return found[name]

    def call(
        self, args: dict[str, object], name: str = WAKE_TOOL_NAME
    ) -> dict[str, Any]:
        tool = self.tool(name)

        async def invoke() -> dict[str, Any]:
            return await tool.handler(args)

        return asyncio.run(invoke())

    def events(self) -> list[object]:
        return [entry.event for entry in read_entries(self.path)]


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    target = World(tmp_path / "events.sqlite", Clock())
    yield target
    target.log.close()


type Act = Callable[[], Coroutine[Any, Any, None]]


async def settle(scheduler: Scheduler, act: Act) -> None:
    stop = asyncio.Event()
    loop = asyncio.create_task(scheduler.run(stop))
    await asyncio.sleep(0)
    await act()
    await scheduler.idle()
    stop.set()
    await loop


async def nothing() -> None:
    return None


def text_of(result: dict[str, Any]) -> str:
    return cast(str, result["content"][0]["text"])


def test_wake_after_schedules_one_wake_rounded_up_to_the_second(world: World) -> None:
    world.clock.now = START + timedelta(microseconds=500_000)
    result = world.call({"seconds": 60, "note": "read the gate's log"})
    assert result.get("is_error") is not True
    pending = world.log.state.scheduled["alpha"]
    assert pending.due == "2026-10-26T09:01:01Z"
    assert pending.note == "read the gate's log"
    assert pending.message_id in text_of(result) and pending.due in text_of(result)
    again = world.call({"seconds": 86400, "note": "tomorrow"})
    assert "replaces the wake that was due at 2026-10-26T09:01:01Z" in text_of(again)
    assert world.log.state.scheduled["alpha"].due == "2026-10-27T09:00:01Z"
    scheduled = [e for e in world.events() if isinstance(e, WakeScheduled)]
    assert len(scheduled) == 2


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"seconds": 59, "note": "x"}, "from 60 to 86400"),
        ({"seconds": 86401, "note": "x"}, "from 60 to 86400"),
        ({"seconds": 60.0, "note": "x"}, "whole number"),
        ({"seconds": True, "note": "x"}, "whole number"),
        ({"seconds": "60", "note": "x"}, "whole number"),
        ({"seconds": 60, "note": 7}, "note must be text"),
        ({"seconds": 60, "note": "x" * (MAX_WAKE_NOTE_BYTES + 1)}, "note is"),
        ({"seconds": 60}, "note must be text"),
        ({"seconds": 60, "note": "x", "when": "now"}, "unexpected argument when"),
    ],
)
def test_wake_after_refuses_bad_arguments_and_records_nothing(
    world: World, args: dict[str, object], message: str
) -> None:
    before = world.log.last_seq
    result = world.call(args)
    assert result["is_error"] is True and message in text_of(result)
    assert world.log.last_seq == before and "alpha" not in world.log.state.scheduled


def test_a_due_wake_fires_as_a_note_from_the_line_and_wakes_it(world: World) -> None:
    world.call({"seconds": 600, "note": "read the gate's log"})
    message_id = world.log.state.scheduled["alpha"].message_id
    world.clock.advance(599)
    asyncio.run(settle(world.scheduler, nothing))
    assert "alpha" not in world.engines
    assert world.scheduler.fire_due_wakes() == []
    world.clock.advance(1)
    asyncio.run(settle(world.scheduler, nothing))
    message = world.log.state.messages[message_id]
    assert (message.sender, message.recipient) == (ALPHA, ALPHA)
    assert message.body == "read the gate's log" and message.delivered
    prompt = world.engines["alpha"].prompts[0]
    assert f"  {message_id} from alpha, note, unread" in prompt
    assert "alpha" not in world.log.state.scheduled
    summary = world.scheduler.line_summaries()[0]
    assert summary["autonomous_turns"] == 1 and summary["scheduled_wake"] is None


def test_a_wake_counts_toward_the_autonomous_limit(tmp_path: Path) -> None:
    target = World(tmp_path / "events.sqlite", Clock(), Limits(max_autonomous_turns=1))
    try:
        target.call({"seconds": 60, "note": "again"})
        target.clock.advance(60)
        asyncio.run(settle(target.scheduler, nothing))
        line = target.log.state.lines["alpha"]
        assert line.held_by is not None
    finally:
        target.log.close()


def test_a_held_lines_wake_fires_and_its_note_waits(world: World) -> None:
    world.scheduler.hold("alpha", "was held for this test")
    world.call({"seconds": 60, "note": "after the hold"})
    message_id = world.log.state.scheduled["alpha"].message_id
    world.clock.advance(60)
    asyncio.run(settle(world.scheduler, nothing))
    assert "alpha" not in world.engines
    assert world.log.state.messages[message_id].delivered is False
    assert [i for i, _ in world.scheduler.pending("alpha")] == [message_id]

    async def release() -> None:
        world.scheduler.release("alpha")

    asyncio.run(settle(world.scheduler, release))
    assert f"  {message_id} from alpha, note" in world.engines["alpha"].prompts[0]


def test_a_wake_due_while_the_daemon_was_down_fires_at_the_first_tick(
    world: World,
) -> None:
    world.call({"seconds": 3600, "note": "after the restart"})
    message_id = world.log.state.scheduled["alpha"].message_id
    world.clock.advance(7200)
    restarted = world.restart()
    assert restarted.seconds_until_next_wake() == 0.0
    asyncio.run(settle(restarted, nothing))
    assert world.log.state.messages[message_id].delivered
    assert world.engines["alpha"].prompts


def test_firing_is_one_append_so_a_crash_cannot_fire_twice(world: World) -> None:
    world.call({"seconds": 60, "note": "once"})
    world.clock.advance(60)
    fired = world.scheduler.fire_due_wakes()
    assert len(fired) == 1
    # A crash now loses the sent queue, but the message is in the log, and the wake
    # no longer pending, so a restarted daemon delivers it and does not fire it again.
    restarted = world.restart()
    assert restarted.fire_due_wakes() == []
    assert restarted.seconds_until_next_wake() is None
    asyncio.run(settle(restarted, nothing))
    notes = [
        e for e in world.events() if isinstance(e, MessageSent) and e.body == "once"
    ]
    assert len(notes) == 1 and world.log.state.messages[fired[0]].delivered


def test_the_rest_ends_when_the_next_wake_falls_due(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "MAX_REST_SECONDS", 0.01)
    assert world.scheduler.seconds_until_next_wake() is None
    world.call({"seconds": 120, "note": "timer"})
    assert world.scheduler.seconds_until_next_wake() == 120.0

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        for _ in range(5):
            await asyncio.sleep(0.02)
        assert "alpha" not in world.engines
        world.clock.advance(120)
        for _ in range(500):
            await asyncio.sleep(0.01)
            if "alpha" in world.engines and world.engines["alpha"].prompts:
                break
        stop.set()
        await loop

    asyncio.run(go())
    assert world.engines["alpha"].prompts


def test_every_line_has_the_tool_and_status_shows_the_pending_wake(
    world: World,
) -> None:
    names = [tool.name for tool in world.scheduler.own_tools("alpha")]
    assert names == [
        "send_message",
        "ask_operator",
        WAKE_TOOL_NAME,
        "acquire_lease",
        "release_lease",
        "list_mail",
        "read_mail",
        "acknowledge_mail",
        "list_sent",
    ]
    assert world.scheduler.line_summaries()[0]["scheduled_wake"] is None
    world.call({"seconds": 60, "note": "a long note that the status leaves out"})
    pending = world.log.state.scheduled["alpha"]
    assert world.scheduler.line_summaries()[0]["scheduled_wake"] == {
        "due": pending.due,
        "message_id": pending.message_id,
    }


# The client's view.


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


def test_status_and_line_view_show_the_pending_wake(paths: ProjectPaths) -> None:
    clock = Clock()
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True, wall_clock=clock)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        message_id, due = scheduler.schedule_wake("alpha", 300, "check")
        daemon = Daemon(
            paths,
            log,
            "2026-10-26T09:00:00Z",
            scheduler,
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=False,
        )

        def ask(op: str, args: dict[str, object]) -> dict[str, object]:
            line = {"id": "r", "op": op, "v": 1, "args": args}
            response = daemon.respond((json.dumps(line) + "\n").encode())
            assert response.ok and response.result is not None
            return response.result

        expected = {"due": due, "message_id": message_id}
        (status_line,) = cast(list[dict[str, object]], ask("status", {})["lines"])
        assert status_line["scheduled_wake"] == expected
        view = cast(dict[str, object], ask("line_view", {"line_id": "alpha"})["line"])
        assert view["scheduled_wake"] == expected
