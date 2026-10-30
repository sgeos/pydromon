"""Round 8 of the session-line experience: a line's own note leaves ``own_notes`` once
a turn of the line has ended after the note was delivered, read from the event log
alone, so every view agrees and the rule holds across a restart."""

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Generator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.daemon import Daemon
from dromon.driver import Strategy
from dromon.engine import BackgroundEnd, TurnReport
from dromon.eventlog import EventLog, read_entries
from dromon.limits import Limits
from dromon.mailbox import MailSummary, listing, summary
from dromon.model import (
    LineAddress,
    LineCreated,
    LineStateChanged,
    LineState,
    LineTurnEnded,
    Message,
    MessageAcknowledged,
    MessageDelivered,
    MessageKind,
    MessageRead,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    State,
)
from dromon.paths import project_paths
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
BETA = LineAddress("beta")
START = datetime(2026, 10, 26, 9, 0, 0, tzinfo=UTC)
COMMIT = "a" * 40
LIST_MAIL_COUNTS = re.compile(
    r"(\d+) unread, (\d+) unanswered and (\d+) notes of your own neither read,"
    r" acknowledged nor shown to a turn that ended"
)
LIST_MAIL_ENTRY = re.compile(r"^(m-[0-9a-f]+) from ", re.MULTILINE)

type Hook = Callable[["World"], Awaitable[None]]


class Scripted:
    """An engine that records each prompt and runs ``world.during`` inside the turn,
    then succeeds, or fails when ``world.fail`` is set."""

    def __init__(self, world: "World", spec: LineSpec) -> None:
        self.world = world
        self.session_id = spec.session_id
        self.prompts: list[str] = []

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        if self.world.during is not None:
            await self.world.during(self.world)
        if self.world.fail:
            raise RuntimeError("the engine failed")
        self.session_id = self.session_id or "session-1"
        return TurnReport(
            "success", 1, "ok", (), None, None, self.session_id, None, 0.1
        )


class World:
    def __init__(self, path: Path, root: Path) -> None:
        self.path = path
        self.root = root
        self.during: Hook | None = None
        self.fail = False
        self.engines: dict[str, Scripted] = {}
        self.inside: list[dict[str, tuple[int, int, int]]] = []
        """Every view's counts, as ``every_view`` gives them, taken inside turns."""
        self.log = EventLog(path)
        self.scheduler = self.start()
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        self.scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Scripted:
        del tools
        engine = self.engines.get(spec.line_id) or Scripted(self, spec)
        self.engines[spec.line_id] = engine
        return engine

    def start(self) -> Scheduler:
        return Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            wall_clock=lambda: START,
            limits=Limits(max_autonomous_turns=20, max_cost_usd=10.0),
            root=self.root,
            protected=(self.root / ".dromon",),
            daemon_commit=COMMIT,
        )

    def restart(self) -> None:
        """Close the log and open it again under a new scheduler, as a restarted
        daemon does, which holds nothing of the old one's memory."""
        self.log.close()
        self.log = EventLog(self.path)
        self.engines.clear()
        self.scheduler = self.start()
        self.scheduler.recover()

    def run(self, *bodies: str, sender: OperatorAddress | LineAddress = ANA) -> None:
        """Send ``bodies`` to alpha, if any, and run until nothing is left to do."""

        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            for body in bodies:
                self.scheduler.send(sender, ALPHA, body)
            await asyncio.sleep(0)
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(go())

    def prompts(self) -> list[str]:
        engine = self.engines.get("alpha")
        return [] if engine is None else engine.prompts

    async def acall(self, name: str, args: dict[str, object] | None = None) -> str:
        tool = next(t for t in self.scheduler.own_tools("alpha") if t.name == name)
        result = await tool.handler(args or {})
        assert result.get("is_error") is not True, result
        return cast(str, result["content"][0]["text"])

    def call(self, name: str, args: dict[str, object] | None = None) -> str:
        return asyncio.run(self.acall(name, args))


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    root = tmp_path.resolve() / "project"
    (root / ".dromon").mkdir(parents=True)
    target = World(tmp_path / "events.sqlite", root)
    yield target
    target.log.close()


def shown_ids(state: State, path: Path, line_id: str) -> set[str]:
    """The messages delivered to ``line_id`` that a turn of it ended after, found by
    walking the event log file in order, apart from ``dromon.mailbox`` and the
    state's turn counts. ``state`` gives only each message's recipient."""
    recipient = LineAddress(line_id)
    waiting: set[str] = set()
    shown: set[str] = set()
    for entry in read_entries(path):
        event = entry.event
        if (
            isinstance(event, MessageDelivered)
            and state.messages[event.message_id].recipient == recipient
        ):
            waiting.add(event.message_id)
        elif isinstance(event, LineTurnEnded) and event.line_id == line_id:
            shown |= waiting
            waiting = set()
    return shown


def expected_own(state: State, path: Path, owner: LineAddress) -> list[str]:
    """``owner``'s own notes that the round 8 rule counts, by its own reading."""
    replied = {
        m.reply_to
        for m in state.messages.values()
        if m.sender == owner and m.reply_to is not None
    }
    shown = shown_ids(state, path, owner.line_id)
    return [
        i
        for i, m in state.messages.items()
        if m.recipient == owner
        and m.sender == owner
        and m.delivered
        and not m.read
        and not m.acknowledged
        and i not in replied
        and i not in shown
    ]


async def every_view(world: World) -> dict[str, tuple[int, int, int]]:
    """alpha's unread, unanswered and own-note counts as each view gives them."""
    compact = json.loads(await world.acall("line_status"))
    detail = json.loads(await world.acall("line_status", {"detail": True}))
    listed = LIST_MAIL_COUNTS.search(await world.acall("list_mail"))
    assert listed is not None
    (client,) = [
        entry
        for entry in world.scheduler.line_summaries()
        if entry["line_id"] == "alpha"
    ]

    def triple(view: dict[str, Any]) -> tuple[int, int, int]:
        return (view["unread"], view["unanswered"], view["own_notes"])

    counts = summary(world.log.state, ALPHA, 0)
    return {
        "summary": (counts.unread, counts.unanswered, counts.own_notes),
        "compact": triple(compact),
        "detail": triple(detail),
        "list_mail": (int(listed[1]), int(listed[2]), int(listed[3])),
        "client": triple(dict(client)),
    }


async def listed_own(world: World) -> list[str]:
    return LIST_MAIL_ENTRY.findall(
        await world.acall("list_mail", {"own_notes_only": True})
    )


def own_count(world: World) -> int:
    """alpha's own-note count, which every view must agree on."""
    views = asyncio.run(every_view(world))
    (counts,) = set(views.values())
    return counts[2]


async def look_inside(world: World) -> None:
    world.inside.append(await every_view(world))


# A. A note shown to a turn leaves the count when that turn ends.


@pytest.mark.parametrize("fail", [False, True], ids=["succeeded", "failed"])
def test_a_note_leaves_the_count_when_the_turn_shown_it_ends(
    world: World, fail: bool
) -> None:
    """The prompt and every view inside the turn count the note the prompt
    delivered, and every view counts it no longer once the turn has ended, whether
    the turn succeeded or failed. The prompt does not mark it read."""
    note = world.scheduler.send(ALPHA, ALPHA, "a note to myself")
    assert own_count(world) == 0
    world.during = look_inside
    world.fail = fail
    world.run()
    state = world.log.state
    (prompt,) = world.prompts()
    assert note in prompt
    inside = expected_own(state, world.path, ALPHA)
    # The views inside the turn and the prompt counted the note.
    (views,) = world.inside
    assert {counts[2] for counts in views.values()} == {1}
    assert MailSummary(1, 0, 0, 1).sentence() in prompt
    # Afterwards the log shows a turn ended after its delivery.
    assert note in shown_ids(state, world.path, "alpha") and inside == []
    assert own_count(world) == len(inside) == 0
    assert asyncio.run(listed_own(world)) == []
    assert state.messages[note].read is False
    assert not any(isinstance(e.event, MessageRead) for e in read_entries(world.path))
    if fail:
        assert world.scheduler.condition("alpha") != "idle"


def test_the_list_inside_the_turn_names_the_note_the_prompt_delivered(
    world: World,
) -> None:
    """``own_notes_only`` lists inside the turn what the count counts."""
    note = world.scheduler.send(ALPHA, ALPHA, "a note to myself")
    seen: list[list[str]] = []

    async def list_inside(world: World) -> None:
        seen.append(await listed_own(world))

    world.during = list_inside
    world.run()
    assert seen == [[note]]
    assert asyncio.run(listed_own(world)) == []


@pytest.mark.parametrize("tool", ["read_mail", "acknowledge_mail"])
def test_a_note_dealt_with_before_its_turn_ends(world: World, tool: str) -> None:
    """A note read or acknowledged inside the turn leaves the count at once, and the
    turn's end does not bring it back or count it twice."""
    note = world.scheduler.send(ALPHA, ALPHA, "a note to myself")

    async def deal(world: World) -> None:
        await look_inside(world)
        await world.acall(tool, {"message_id": note})
        await look_inside(world)

    world.during = deal
    world.run()
    before, after = world.inside
    assert {counts[2] for counts in before.values()} == {1}
    assert {counts[2] for counts in after.values()} == {0}
    assert own_count(world) == len(expected_own(world.log.state, world.path, ALPHA))
    assert own_count(world) == 0
    kind = MessageRead if tool == "read_mail" else MessageAcknowledged
    assert [
        e.event.message_id
        for e in read_entries(world.path)
        if isinstance(e.event, kind)
    ] == [note]


def test_mail_from_others_is_not_retired_by_a_turn(world: World) -> None:
    """Only own notes leave a count when a turn ends. Mail from an operator or
    another line stays unread and unanswered until the line deals with it."""
    world.scheduler.send(BETA, ALPHA, "from beta")
    world.run("from ana")
    assert len(world.prompts()) == 1
    views = asyncio.run(every_view(world))
    assert set(views.values()) == {(2, 2, 0)}


# B. A restart.


def test_a_restart_between_the_turns_end_and_the_next_count(world: World) -> None:
    """The rule reads the event log alone, so a restarted daemon counts as the old
    one did."""
    world.scheduler.send(ALPHA, ALPHA, "a note to myself")
    world.scheduler.send(ALPHA, ALPHA, "another")
    world.run()
    before = asyncio.run(every_view(world))
    world.restart()
    after = asyncio.run(every_view(world))
    assert before == after
    assert own_count(world) == len(expected_own(world.log.state, world.path, ALPHA))
    assert own_count(world) == 0


def test_a_turn_cut_short_by_a_restart_retires_nothing(world: World) -> None:
    """A turn the daemon died in records no turn end, so its note still counts after
    the restart, and the next turn shows it in its count and retires it."""
    note = world.scheduler.send(ALPHA, ALPHA, "a note to myself")
    world.log.append(LineStateChanged("alpha", LineState.RUNNING))
    world.log.append(MessageDelivered(note))
    world.restart()
    assert world.log.state.lines["alpha"].state is LineState.IDLE
    assert expected_own(world.log.state, world.path, ALPHA) == [note]
    assert own_count(world) == 1
    world.run("from ana")
    (prompt,) = world.prompts()
    assert MailSummary(1, 1, 1, 1).sentence() in prompt
    assert own_count(world) == len(expected_own(world.log.state, world.path, ALPHA))
    assert own_count(world) == 0


# C. Task notes and other notes delivered without a prompt.


def end(
    task_id: str, *, during_turn: bool = False, stopped_by_line: bool = False
) -> BackgroundEnd:
    return BackgroundEnd(
        task_id,
        "make gate",
        "completed",
        during_turn=during_turn,
        stopped_by_line=stopped_by_line,
    )


def test_a_task_note_recorded_during_a_turn_leaves_with_that_turn(
    world: World,
) -> None:
    """Decided in round 8. No prompt delivers a task note recorded during a running
    turn. It counts while that turn runs, and leaves when it ends, since that turn
    learned of the task's end from its engine."""
    noted: list[str | None] = []

    async def task_ends(world: World) -> None:
        noted.append(
            world.scheduler.background_ended("alpha", end("t1", during_turn=True))
        )
        await look_inside(world)

    world.during = task_ends
    world.run("from ana")
    (note,) = noted
    assert note is not None
    assert world.log.state.messages[note].delivered
    (inside,) = world.inside
    assert {counts[2] for counts in inside.values()} == {1}
    assert all(note not in prompt for prompt in world.prompts())
    assert own_count(world) == len(expected_own(world.log.state, world.path, ALPHA))
    assert own_count(world) == 0


def test_a_note_delivered_between_turns_counts_until_the_next_turn_ends(
    world: World,
) -> None:
    """Decided in round 8. The end of a task the line stopped is delivered between
    turns with no wake. It counts until the next turn ends, so that turn sees it in
    its count."""
    note = world.scheduler.background_ended("alpha", end("t2", stopped_by_line=True))
    assert note is not None and world.log.state.messages[note].delivered
    assert own_count(world) == 1
    world.run("from ana")
    (prompt,) = world.prompts()
    assert note not in prompt
    assert MailSummary(1, 1, 1, 1).sentence() in prompt
    assert own_count(world) == len(expected_own(world.log.state, world.path, ALPHA))
    assert own_count(world) == 0


def test_another_lines_turn_retires_nothing_of_alpha(world: World) -> None:
    """A turn's end retires only notes of the line whose turn it was."""
    note = world.scheduler.send(ALPHA, ALPHA, "a note to myself")
    world.log.append(MessageDelivered(note))
    world.log.append(LineStateChanged("beta", LineState.RUNNING))
    world.log.append(LineStateChanged("beta", LineState.IDLE))
    world.log.append(LineTurnEnded("beta", None))
    assert own_count(world) == 1


# D. The operator's mailbox and the client's mail request.


def operator_log(path: Path) -> None:
    """A log in which alpha sent @ana two notes and ended a turn after both were
    delivered, and @ana opened one."""
    with EventLog(path) as log:
        log.append(LineCreated("alpha", HAIKU, "subscription"))
        log.append(OperatorRegistered("ana"))
        for message_id in ("m-1", "m-2"):
            log.append(MessageSent(message_id, ALPHA, ANA, MessageKind.NOTE, "hi"))
            log.append(MessageDelivered(message_id))
        log.append(MessageRead("m-2"))
        log.append(LineStateChanged("alpha", LineState.RUNNING))
        log.append(LineStateChanged("alpha", LineState.IDLE))
        log.append(LineTurnEnded("alpha", None))


def test_an_operators_mailbox_is_not_retired_by_a_lines_turn(tmp_path: Path) -> None:
    """Mail to an operator records no turn count, so a line's turn retires none of
    it. An operator cannot send itself a note, so its ``own_notes`` is 0, and the
    client's ``mail`` request gives the counts ``summary`` gives."""
    from dromon.scheduler import Scheduler as Held

    paths = project_paths(tmp_path, tmp_path / "run")
    paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    operator_log(paths.event_log)
    with EventLog(paths.event_log) as log:
        state = log.state
        assert {state.messages[i].turn_ends_at_delivery for i in ("m-1", "m-2")} == {
            None
        }
        counts = summary(state, ANA, 0)
        unread = [
            i for i, m in state.messages.items() if m.recipient == ANA and not m.read
        ]
        assert (counts.unread, counts.unanswered, counts.own_notes) == (
            len(unread),
            2,
            0,
        )
        assert listing(state, ANA, own_notes_only=True) == ([], 0)

        def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
            raise AssertionError(spec.line_id)

        daemon = Daemon(
            paths, log, "2026-10-25T00:00:00Z", Held(log, no_engine, hold_wakes=True)
        )
        line = json.dumps(
            {"id": "r", "op": "mail", "v": 1, "args": {"operator": "ana"}}
        ).encode()
        response = daemon.respond(line + b"\n")
        assert response.ok and response.result is not None
        result = response.result
        assert (result["unread"], result["unanswered"], result["own_notes"]) == (
            counts.unread,
            counts.unanswered,
            counts.own_notes,
        )


# E. The model.


def test_replay_rebuilds_the_turn_ends_and_a_copy_keeps_them(world: World) -> None:
    """The state the daemon holds equals the state replayed from its log, and a copy
    is independent of the state it was taken from."""
    world.scheduler.send(ALPHA, ALPHA, "a note to myself")
    world.run()
    world.run("from ana")
    replayed = State()
    for entry in read_entries(world.path):
        replayed.apply(entry.event)
    live = world.log.state
    assert replayed.turn_end_counts == live.turn_end_counts == {"alpha": 2}
    assert {i: m.turn_ends_at_delivery for i, m in replayed.messages.items()} == {
        i: m.turn_ends_at_delivery for i, m in live.messages.items()
    }
    copied = live.copy()
    copied.turn_end_counts["alpha"] = 99
    assert live.turn_end_counts == {"alpha": 2}


def test_a_message_records_its_lines_turn_ends_at_delivery() -> None:
    """Mail to a line records how many of its turns had ended, and mail to anyone
    else records nothing, so a line's turn cannot retire it."""
    state = State()
    state.apply(LineCreated("alpha", HAIKU, "subscription"))
    state.apply(OperatorRegistered("ana"))
    state.apply(LineStateChanged("alpha", LineState.RUNNING))
    state.apply(LineStateChanged("alpha", LineState.IDLE))
    state.apply(LineTurnEnded("alpha", None))
    state.apply(MessageSent("m-1", ALPHA, ALPHA, MessageKind.NOTE, "x"))
    state.apply(MessageSent("m-2", ALPHA, ANA, MessageKind.NOTE, "x"))
    assert state.messages["m-1"].turn_ends_at_delivery is None
    for message_id in ("m-1", "m-2"):
        state.apply(MessageDelivered(message_id))
    delivered: dict[str, Message] = state.messages
    assert delivered["m-1"].turn_ends_at_delivery == state.turn_end_counts["alpha"]
    assert delivered["m-2"].turn_ends_at_delivery is None
