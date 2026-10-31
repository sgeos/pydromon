"""A line's engine session cleared by a configuration, between turns and during one.

Round 9, item 1, and its review, F1. A cleared session forgets its context estimate,
a configuration that turns ``fresh_session_each_turn`` on clears the session at once,
and a turn that ends after its line's session was cleared records neither its
session, its context estimate nor a compaction wake. Every engine is scripted."""

import asyncio
from collections.abc import Callable, Generator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog, read_entries
from dromon.lineconfig import LineConfig
from dromon.limits import Limits
from dromon.model import LineAddress, LineSessionCleared, OperatorAddress
from dromon.scheduler import LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
START = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
BIG = 900_000
"""A context estimate far past any warning."""
WARNING = "past this line's warning"


class Scripted:
    """An engine that records each prompt and reports a large context, so a line's
    estimate is past its warning after every turn. While its world holds a gate, a
    turn waits on it, so a test can change the line during the turn."""

    def __init__(self, spec: LineSpec, world: "World") -> None:
        self.session_id = spec.session_id
        self.prompts: list[str] = []
        self.world = world

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        gate = self.world.gate
        if gate is not None:
            self.world.waiting = True
            await gate.wait()
        self.session_id = f"session-{len(self.prompts)}"
        return TurnReport(
            "success",
            1,
            "ok",
            (),
            None,
            None,
            self.session_id,
            None,
            0.1,
            context_tokens=BIG,
        )


class World:
    def __init__(self, path: Path, root: Path) -> None:
        self.root = root
        self.log = EventLog(path)
        self.specs: list[LineSpec] = []
        self.engines: list[Scripted] = []
        self.gate: asyncio.Event | None = None
        self.waiting = False
        self.scheduler = Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            wall_clock=lambda: START,
            limits=Limits(max_autonomous_turns=20, max_cost_usd=10.0),
            root=root,
            protected=(root / ".dromon",),
        )
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Scripted:
        del tools
        self.specs.append(spec)
        engine = Scripted(spec, self)
        self.engines.append(engine)
        return engine

    def run(self, *bodies: str) -> None:
        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            for body in bodies:
                self.scheduler.send(ANA, ALPHA, body)
            await asyncio.sleep(0)
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(go())

    def run_changing(self, body: str, change: Callable[[], None]) -> None:
        """Run one turn for ``body``, and call ``change`` while it runs."""

        async def go() -> None:
            stop = asyncio.Event()
            self.gate = asyncio.Event()
            self.waiting = False
            loop = asyncio.create_task(self.scheduler.run(stop))
            self.scheduler.send(ANA, ALPHA, body)
            # Bounded, since the turn reaches its engine within a few passes.
            for _ in range(1000):
                if self.waiting:
                    break
                await asyncio.sleep(0)
            assert self.waiting, "the turn never reached its engine"
            assert "alpha" in self.scheduler._running  # pyright: ignore[reportPrivateUsage]
            change()
            self.gate.set()
            self.gate = None
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(go())

    def last_prompt(self) -> str:
        return self.engines[-1].prompts[-1]

    def cleared(self) -> int:
        return sum(isinstance(entry, LineSessionCleared) for entry in self.events())

    def events(self) -> list[object]:
        return [entry.event for entry in read_entries(self.log.path)]

    def compaction_wake(self) -> bool:
        """Whether a wake is scheduled for the line, or a compaction wake is held."""
        held = self.scheduler._context_compaction_wake_ids  # pyright: ignore[reportPrivateUsage]
        return "alpha" in self.log.state.scheduled or "alpha" in held


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    root = tmp_path.resolve() / "project"
    (root / ".dromon").mkdir(parents=True)
    (root / "moved").mkdir()
    target = World(tmp_path / "events.sqlite", root)
    yield target
    target.log.close()


def test_a_reload_to_fresh_sessions_clears_the_session_and_its_context_at_once(
    world: World,
) -> None:
    """The first turn after the change starts a new session with no warning about
    the old session's context, and ``dromon settings`` shows no session as soon as
    the change is applied, not only after the next turn has begun."""
    world.run("the task")
    assert world.log.state.lines["alpha"].engine_session == "session-1"
    world.run("again")
    assert WARNING in world.last_prompt()
    before = world.cleared()
    world.scheduler.configure_line("alpha", LineConfig(fresh_session_each_turn=True))
    assert world.log.state.lines["alpha"].engine_session is None
    assert world.cleared() == before + 1
    assert world.scheduler.context_status("alpha")["estimated_tokens"] is None
    world.run("the next brief")
    assert world.specs[-1].session_id is None
    assert WARNING not in world.last_prompt()


def test_reconfiguring_a_line_already_fresh_records_no_second_clear(
    world: World,
) -> None:
    world.scheduler.configure_line("alpha", LineConfig(fresh_session_each_turn=True))
    world.run("one")
    before = world.cleared()
    world.scheduler.configure_line(
        "alpha", LineConfig(fresh_session_each_turn=True, effort="low")
    )
    assert world.cleared() == before


def test_a_cleared_or_rebound_session_forgets_the_old_context(world: World) -> None:
    world.run("the task")
    assert world.scheduler.context_status("alpha")["estimated_tokens"] == BIG
    world.scheduler.clear_session("alpha")
    assert world.scheduler.context_status("alpha")["estimated_tokens"] is None
    world.run("carry on")
    assert world.scheduler.context_status("alpha")["estimated_tokens"] == BIG
    world.scheduler.rebind_line("alpha", "claude-opus-5-5", Strategy.SUBSCRIPTION)
    assert world.scheduler.context_status("alpha")["estimated_tokens"] is None


def test_a_workdir_moved_during_a_turn_keeps_none_of_that_turns_context(
    world: World,
) -> None:
    """Round 9 review, F1, found by its probe. The turn ends after the move, and its
    estimate, which belongs to the session of the old directory, was stored again,
    so the line warned and could be woken to compact a session it no longer had."""
    compacting = LineConfig(allow_compact=True)
    world.scheduler.configure_line("alpha", compacting)
    world.run_changing(
        "work",
        lambda: world.scheduler.configure_line(
            "alpha", LineConfig(allow_compact=True, workdir="moved")
        ),
    )
    assert world.log.state.lines["alpha"].engine_session is None
    assert world.scheduler.context_status("alpha")["estimated_tokens"] is None
    assert not world.compaction_wake()
    # The scripted engine never compacts, so compaction is turned off before the
    # next turn, which would otherwise be followed by a wake for its own estimate.
    world.scheduler.configure_line("alpha", LineConfig(workdir="moved"))
    world.run("next")
    assert world.specs[-1].session_id is None
    assert WARNING not in world.engines[-1].prompts[0]


def test_a_reload_to_fresh_sessions_during_a_turn_clears_at_once(
    world: World,
) -> None:
    """Round 9 review, F1. The session is cleared as the change applies, and the turn
    in flight records neither its session nor its estimate when it ends, so no
    compaction wake is raised for the session the line no longer resumes."""
    world.run("the task")
    assert world.log.state.lines["alpha"].engine_session == "session-1"
    before = world.cleared()
    seen: list[str | None] = []

    def reload() -> None:
        world.scheduler.configure_line(
            "alpha", LineConfig(fresh_session_each_turn=True, allow_compact=True)
        )
        seen.append(world.log.state.lines["alpha"].engine_session)

    world.run_changing("work", reload)
    assert seen == [None]
    assert world.cleared() == before + 1
    assert world.log.state.lines["alpha"].engine_session is None
    assert world.scheduler.context_status("alpha")["estimated_tokens"] is None
    assert not world.compaction_wake()
    world.scheduler.configure_line("alpha", LineConfig(fresh_session_each_turn=True))
    world.run("next")
    assert world.specs[-1].session_id is None
    assert WARNING not in world.engines[-1].prompts[0]


def test_a_turn_with_no_change_still_records_its_session_and_context(
    world: World,
) -> None:
    """The negative case of F1. A turn whose session was not cleared keeps both."""
    world.run_changing("work", lambda: None)
    assert world.log.state.lines["alpha"].engine_session == "session-1"
    assert world.scheduler.context_status("alpha")["estimated_tokens"] == BIG
