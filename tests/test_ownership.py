"""The ownership tools, ``record_role_note``, ``relinquish_ownership`` and
``take_ownership``, with what follows a change of owner: the engines dropped, the
successor woken, the former writer's turn waited for, and each seat told at its next
turn. Roles step 2, ``docs/architecture/ROLES.md``."""

import asyncio
import os
import subprocess
from collections.abc import Callable, Coroutine, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import scheduler as scheduler_module
from dromon.capabilities import Known, TABLES
from dromon.driver import Strategy
from dromon.engine import EnginePlanLimit, TurnReport
from dromon.eventlog import Entry, EventLog, read_entries, replay
from dromon.model import (
    HARNESS,
    MAX_BRIEF_ARTIFACTS,
    MAX_BRIEF_BYTES,
    MAX_BRIEF_FIELD_BYTES,
    MAX_ROLE_NOTE_BYTES,
    BriefSet,
    ProcessesLeft,
    HarnessAddress,
    BudgetSource,
    BudgetStated,
    EventRejected,
    InvalidField,
    LineAddress,
    LineState,
    LineStateChanged,
    LineTurnEnded,
    Message,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OwnershipCause,
    OwnershipChanged,
    RemoteAddress,
    Role,
    RoleDeclared,
    RoleNoteKind,
    SeatBound,
    TaskRecorded,
    format_due,
)
from dromon.roles import (
    CONFINING_ENGINES,
    RoleConflict,
    Stamp,
    plan_relinquish,
    recovery_ground,
    seat_access,
    waits_unconfined,
)
from dromon.scheduler import (
    BRIEF_BEGIN,
    BRIEF_END,
    MAX_TAKE_REASON_BYTES,
    SET_BRIEF_TOOL_NAME,
    RECORD_ROLE_NOTE_TOOL_NAME,
    RECOVERY_CHECKLIST,
    RELINQUISH_OWNERSHIP_TOOL_NAME,
    ROLE_STATUS_TOOL_NAME,
    TAKE_OWNERSHIP_TOOL_NAME,
    MAX_PACKAGE_MAIL_BYTES,
    LineSpec,
    LineToolRefused,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
PROVIDER = "claude-code"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
START = datetime(2026, 10, 27, 9, 0, 0, tzinfo=UTC)
WORKTREE_COMMIT = "c" * 40
DAEMON_COMMIT = "d" * 40
ROLE_TOOLS = (
    ROLE_STATUS_TOOL_NAME,
    RECORD_ROLE_NOTE_TOOL_NAME,
    RELINQUISH_OWNERSHIP_TOOL_NAME,
    TAKE_OWNERSHIP_TOOL_NAME,
)

type Turn = list[dict[str, Any]]


class Scripted:
    """A line engine whose turns make scripted tool calls, may wait at a gate, and
    may end at a plan limit. It records when each of its turns starts and ends."""

    def __init__(
        self, world: "World", spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]
    ) -> None:
        self.world = world
        self.line_id = spec.line_id
        self.session_id: str | None = None
        self.tools = {tool.name: tool for tool in tools}
        self.prompts: list[str] = []
        self.results: list[dict[str, Any]] = []

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        self.world.order.append(f"start {self.line_id}")
        try:
            script = self.world.scripts[self.line_id]
            calls = script.pop(0) if script else []
            for call in calls:
                if call.get("tool") == "plan_limit":
                    raise EnginePlanLimit(
                        "Claude subscription plan limit reached", may_move_role=True
                    )
                if call.get("tool") == "gate":
                    await self.world.gates[self.line_id].wait()
                    continue
                args = {key: value for key, value in call.items() if key != "tool"}
                self.results.append(await self.tools[str(call["tool"])].handler(args))
        finally:
            self.world.order.append(f"end {self.line_id}")
        return TurnReport("success", 1, "ok", (), None, None, "session-1", None, None)


class Kept(Scripted):
    """A scripted engine that keeps its connection, whose closing waits at the
    world's gate for its line."""

    async def retire(self) -> None:
        self.world.order.append(f"closing {self.line_id}")
        await self.world.closing[self.line_id].wait()
        self.world.order.append(f"closed {self.line_id}")

    def kept_status(self) -> dict[str, object]:
        return {"kept": True, "seconds_to_close": None, "background_tasks": 0}


class World:
    """A scheduler over a real event log in a project whose root is a checkout, with
    role core on the root, seats alpha, beta and gamma, alpha its owner and gamma
    unable to own, and a line delta that is a seat of no role. Its engine counts as
    one that can confine a seat, as Claude Code's will once read-only seats merge,
    unless ``confining`` says otherwise."""

    def __init__(
        self, root: Path, confining: frozenset[str] = frozenset({PROVIDER})
    ) -> None:
        self.root = root
        self.confining = confining
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text(WORKTREE_COMMIT + "\n")
        self.log = EventLog(root / "events.sqlite")
        self.now = START
        self.order: list[str] = []
        self.scripts: dict[str, list[Turn]] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.closing: dict[str, asyncio.Event] = {}
        """The lines whose engines keep their connection, with the gate each
        connection's closing waits at."""
        self.engines: dict[str, list[Scripted]] = {}
        self.scheduler = Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            clock=lambda: 0.0,
            wall_clock=lambda: self.now,
            root=root,
            daemon_commit=DAEMON_COMMIT,
            confining_engines=confining,
        )
        self.scheduler.ensure_operator("ana")
        for line_id in ("alpha", "beta", "gamma", "delta"):
            self.scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
            self.scripts[line_id] = []
        for event in (
            RoleDeclared("core", "."),
            SeatBound("alpha", "core", True),
            SeatBound("beta", "core", True),
            SeatBound("gamma", "core", False),
            OwnershipChanged(
                "core",
                "alpha",
                None,
                OwnershipCause.DECLARED,
                ANA,
                None,
                format_due(START),
            ),
        ):
            self.log.append(event)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Scripted:
        kind = Kept if spec.line_id in self.closing else Scripted
        engine = kind(self, spec, tools)
        self.engines.setdefault(spec.line_id, []).append(engine)
        return engine

    def prompts(self, line_id: str) -> list[str]:
        return [p for engine in self.engines.get(line_id, []) for p in engine.prompts]

    def results(self, line_id: str) -> list[dict[str, Any]]:
        return [r for engine in self.engines.get(line_id, []) for r in engine.results]

    def tool(self, line_id: str, name: str) -> SdkMcpTool[Any]:
        return {tool.name: tool for tool in self.scheduler.own_tools(line_id)}[name]

    def call(
        self, line_id: str, name: str, args: dict[str, object]
    ) -> tuple[str, bool]:
        async def invoke() -> dict[str, Any]:
            return await self.tool(line_id, name).handler(args)

        result = asyncio.run(invoke())
        return str(result["content"][0]["text"]), result.get("is_error") is True

    def mail(self, sender: str | HarnessAddress, recipient: str) -> list[str]:
        """The bodies of the delivered notes from ``sender``, a line or the harness,
        to ``recipient``."""
        source = LineAddress(sender) if isinstance(sender, str) else sender
        return [
            message.body
            for message in self.log.state.messages.values()
            if message.sender == source
            and message.recipient == LineAddress(recipient)
            and message.delivered
        ]

    def role(self) -> Role:
        return self.log.state.roles["core"]

    def restart(self) -> None:
        """Reopen the event log and scheduler as a daemon restart would."""
        path = self.log.path
        self.log.close()
        self.log = EventLog(path)
        self.scheduler = Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            clock=lambda: 0.0,
            wall_clock=lambda: self.now,
            root=self.root,
            daemon_commit=DAEMON_COMMIT,
            confining_engines=self.confining,
        )

    def budget(self, percent: int, resets_at: str | None = None) -> None:
        self.log.append(
            BudgetStated(
                PROVIDER,
                percent,
                resets_at,
                BudgetSource.OPERATOR,
                "",
                format_due(START),
            )
        )


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    target = World(tmp_path / "project")
    yield target
    target.log.close()


async def until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until ``condition`` holds, within a bounded number of
    passes, so a test that would hang fails instead."""
    for _ in range(2000):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never held")


def run(world: World, act: Callable[[], Coroutine[Any, Any, None]]) -> None:
    async def settle() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        await asyncio.sleep(0)
        await act()
        await world.scheduler.idle()
        stop.set()
        await loop

    asyncio.run(settle())


def wake(world: World, *line_ids: str) -> None:
    """Send each line a note from the operator and run until every turn ends."""

    async def act() -> None:
        for line_id in line_ids:
            world.scheduler.send(ANA, LineAddress(line_id), "go on")

    run(world, act)


# The model's back_to.


def test_back_to_is_only_a_handoffs_and_names_another_seat() -> None:
    at = format_due(START)
    for cause, owner, back_to in (
        (OwnershipCause.RECOVERY, "beta", "alpha"),
        (OwnershipCause.HANDOFF, "beta", "beta"),
    ):
        with pytest.raises(InvalidField, match="back_to"):
            OwnershipChanged("core", owner, "alpha", cause, ALPHA, "e1", at, back_to)


def test_back_to_names_a_seat_that_may_own(world: World) -> None:
    for back_to, message in (("gamma", "may not own"), ("delta", "not a seat")):
        with pytest.raises(RoleConflict, match=message):
            plan_relinquish(
                world.log.state,
                "alpha",
                "the gate passed",
                "beta",
                back_to,
                "e1",
                Stamp("", "", format_due(START)),
            )


# record_role_note.


def test_a_note_is_stamped_by_the_daemon_with_both_commits(world: World) -> None:
    world.now = START + timedelta(minutes=5)
    text, error = world.call(
        "beta", RECORD_ROLE_NOTE_TOOL_NAME, {"kind": "note", "body": "ready to help"}
    )
    assert not error and "stamped by the daemon" in text
    (entry,) = world.log.state.role_entries.values()
    assert (entry.line_id, entry.kind, entry.body) == (
        "beta",
        RoleNoteKind.NOTE,
        "ready to help",
    )
    assert (entry.workdir_commit, entry.daemon_commit) == (
        WORKTREE_COMMIT,
        DAEMON_COMMIT,
    )
    assert entry.noted_at == "2026-10-27T09:05:00Z"
    assert entry.entry_id in text


def test_a_read_only_seat_can_record_review_findings_with_its_commit(
    world: World,
) -> None:
    text, error = world.call(
        "gamma",
        RECORD_ROLE_NOTE_TOOL_NAME,
        {"kind": "note", "body": "Review found no remaining issues."},
    )
    assert not error and "stamped by the daemon" in text
    entry = next(iter(world.log.state.role_entries.values()))
    assert (entry.line_id, entry.body, entry.kind) == (
        "gamma",
        "Review found no remaining issues.",
        RoleNoteKind.NOTE,
    )
    assert (entry.workdir_commit, entry.daemon_commit) == (
        WORKTREE_COMMIT,
        DAEMON_COMMIT,
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"kind": "recovery", "body": "found it clean"}, "written by its owner"),
        ({"kind": "handoff", "body": "x"}, "kind must be note or recovery"),
        ({"kind": "note", "body": ""}, "must not be empty"),
        ({"kind": "note", "body": "x" * (MAX_ROLE_NOTE_BYTES + 1)}, "bytes"),
        ({"kind": "note", "body": 3}, "body must be text"),
        ({"kind": "note"}, "body must be text"),
        ({"kind": "note", "body": "x", "at": "now"}, "unexpected argument at"),
    ],
)
def test_a_bad_note_is_refused_and_records_nothing(
    world: World, args: dict[str, object], message: str
) -> None:
    text, error = world.call("beta", RECORD_ROLE_NOTE_TOOL_NAME, args)
    assert error and message in text
    assert not world.log.state.role_entries


def test_a_handoff_entry_is_written_only_with_its_handoff(world: World) -> None:
    with pytest.raises(LineToolRefused, match="written by relinquish_ownership"):
        world.scheduler.record_role_note("alpha", RoleNoteKind.HANDOFF, "x")


def test_the_owner_records_a_recovery_and_a_disclosed_term_is_refused(
    world: World,
) -> None:
    assert world.scheduler.record_role_note("alpha", RoleNoteKind.RECOVERY, "clean")
    world.scheduler.set_disclosure_terms(["hidden"])
    with pytest.raises(LineToolRefused):
        world.scheduler.record_role_note("beta", RoleNoteKind.NOTE, "the Hidden word")
    assert len(world.log.state.role_entries) == 1


def test_the_role_tools_are_offered_only_to_a_seat(world: World) -> None:
    names = {tool.name for tool in world.scheduler.own_tools("gamma")}
    assert set(ROLE_TOOLS) <= names
    names = {tool.name for tool in world.scheduler.own_tools("delta")}
    assert not set(ROLE_TOOLS) & names
    with pytest.raises(LineToolRefused, match="seat of no role"):
        world.scheduler.record_role_note("delta", RoleNoteKind.NOTE, "x")
    with pytest.raises(LineToolRefused, match="seat of no role"):
        world.scheduler.take_ownership("delta", "x")


# relinquish_ownership.


def test_a_handoff_records_the_note_and_the_change_and_wakes_the_successor(
    world: World,
) -> None:
    world.scheduler.hibernate("beta")
    text, error = world.call(
        "alpha",
        RELINQUISH_OWNERSHIP_TOOL_NAME,
        {"note": "the gate passed", "to": "beta"},
    )
    assert not error and "passed to beta" in text and "read only" in text
    role = world.role()
    change = role.changed
    assert role.owner == "beta" and change is not None
    assert (change.cause, change.previous, change.by) == (
        OwnershipCause.HANDOFF,
        "alpha",
        ALPHA,
    )
    entry = world.log.state.role_entries[str(change.entry_id)]
    assert (entry.kind, entry.line_id, entry.body) == (
        RoleNoteKind.HANDOFF,
        "alpha",
        "the gate passed",
    )
    assert entry.workdir_commit == WORKTREE_COMMIT
    assert not world.log.state.lines["beta"].hibernated
    wake(world)
    assert len(world.prompts("beta")) == 1
    (note,) = world.mail(HARNESS, "beta")
    assert f"handed role core to you, with handoff entry {entry.entry_id}" in note
    # The harness wrote it, so it is not recorded as from the line that handed over.
    assert world.mail("alpha", "beta") == []


def test_a_relinquish_with_no_successor_leaves_the_role_vacant(world: World) -> None:
    text, error = world.call(
        "alpha", RELINQUISH_OWNERSHIP_TOOL_NAME, {"note": "stopping here"}
    )
    assert not error and "is vacant" in text
    change = world.role().changed
    assert world.role().owner is None and change is not None
    assert change.cause is OwnershipCause.RELINQUISHED
    assert change.entry_id in world.log.state.role_entries


@pytest.mark.parametrize(
    ("line_id", "args", "message"),
    [
        ("beta", {"note": "x", "to": "gamma"}, "does not own role core"),
        ("alpha", {"note": "x", "to": "alpha"}, "to itself"),
        ("alpha", {"note": "x", "to": "gamma"}, "may not own"),
        ("alpha", {"note": "x", "to": "delta"}, "not a seat"),
        ("alpha", {"note": "x", "to": "nobody"}, "not a seat"),
        ("alpha", {"note": "x", "back_to": "alpha"}, "back_to needs a successor"),
        ("alpha", {"note": ""}, "must not be empty"),
        ("alpha", {"note": "x", "to": 3}, "to must be text"),
        ("alpha", {"note": "x", "back_to": 3}, "back_to must be text"),
        ("alpha", {"to": "beta"}, "note must be text"),
        ("alpha", {"note": "x", "when": "now"}, "unexpected argument when"),
    ],
)
def test_a_bad_relinquish_is_refused_and_records_nothing(
    world: World, line_id: str, args: dict[str, object], message: str
) -> None:
    before = world.log.last_seq
    text, error = world.call(line_id, RELINQUISH_OWNERSHIP_TOOL_NAME, args)
    assert error and message in text
    assert world.log.last_seq == before and world.role().owner == "alpha"


def test_a_stopped_successor_is_refused(world: World) -> None:
    world.log.append(LineStateChanged("beta", LineState.STOPPED))
    with pytest.raises(LineToolRefused, match="beta"):
        world.scheduler.relinquish_ownership("alpha", "x", "beta", None)
    assert world.role().owner == "alpha"


def test_back_to_returns_the_role_for_a_short_alternation(world: World) -> None:
    first, _ = world.scheduler.relinquish_ownership(
        "alpha", "over to you", "beta", "alpha"
    )
    handed = world.role().changed
    assert handed is not None and (first.owner, handed.back_to) == ("beta", "alpha")
    second, _ = world.scheduler.relinquish_ownership("beta", "back to you", None, None)
    change = world.role().changed
    assert second.owner == "alpha" and change is not None
    assert (change.cause, change.back_to) == (OwnershipCause.HANDOFF, None)
    assert "returns to alpha" in second.note
    # The return carried no back_to of its own, so the next one leaves it vacant.
    third, _ = world.scheduler.relinquish_ownership("alpha", "done", None, None)
    assert third.owner is None


def test_a_named_successor_overrides_back_to(world: World) -> None:
    world.scheduler.relinquish_ownership("alpha", "over to you", "beta", "alpha")
    world.log.append(SeatBound("delta", "core", True))
    plan, _ = world.scheduler.relinquish_ownership("beta", "to delta", "delta", None)
    assert plan.owner == "delta"


def test_a_return_to_a_seat_that_cannot_own_leaves_the_role_vacant(
    world: World,
) -> None:
    world.scheduler.relinquish_ownership("alpha", "over to you", "beta", "alpha")
    world.log.append(LineStateChanged("alpha", LineState.STOPPED))
    plan, _ = world.scheduler.relinquish_ownership("beta", "back to you", None, None)
    assert plan.owner is None and world.role().owner is None
    assert "cannot take it now" in plan.note


# take_ownership and its grounds.


def test_a_seat_takes_a_vacant_role(world: World) -> None:
    world.scheduler.relinquish_ownership("alpha", "stopping", None, None)
    text, error = world.call("beta", TAKE_OWNERSHIP_TOOL_NAME, {"reason": "next task"})
    assert not error and "because the role is vacant" in text
    change = world.role().changed
    assert world.role().owner == "beta" and change is not None
    assert (change.cause, change.previous, change.by) == (
        OwnershipCause.RECOVERY,
        None,
        LineAddress("beta"),
    )
    entry = world.log.state.role_entries[str(change.entry_id)]
    assert entry.kind is RoleNoteKind.NOTE and "next task" in entry.body


def test_a_seat_cannot_take_from_a_working_owner(world: World) -> None:
    before = world.log.last_seq
    text, error = world.call(
        "beta", TAKE_OWNERSHIP_TOOL_NAME, {"reason": "alpha is out of tokens"}
    )
    assert error and "does not show unable to act" in text
    assert world.log.last_seq == before and world.role().owner == "alpha"


def test_a_spent_provider_budget_lets_a_seat_take(world: World) -> None:
    world.budget(0, "2026-10-28T00:00:00Z")
    plan, ground = world.scheduler.take_ownership("beta", "alpha is spent")
    assert plan.owner == "beta" and "0 percent" in ground


def test_a_budget_whose_reset_passed_is_not_spent(world: World) -> None:
    world.budget(0, "2026-10-27T08:00:00Z")
    with pytest.raises(LineToolRefused, match="does not show unable to act"):
        world.scheduler.take_ownership("beta", "alpha is spent")
    world.budget(5)
    with pytest.raises(LineToolRefused, match="does not show unable to act"):
        world.scheduler.take_ownership("beta", "alpha is spent")


@pytest.mark.parametrize(
    ("line_id", "reason", "message"),
    [
        ("alpha", "mine", "owns role core already"),
        ("gamma", "mine", "may not own"),
        ("beta", "", "1 to 4096 bytes"),
        ("beta", "x" * (MAX_TAKE_REASON_BYTES + 1), "1 to 4096 bytes"),
    ],
)
def test_a_bad_take_is_refused(
    world: World, line_id: str, reason: str, message: str
) -> None:
    world.budget(0)
    with pytest.raises(LineToolRefused, match=message):
        world.scheduler.take_ownership(line_id, reason)
    assert world.role().owner == "alpha"


def test_the_take_tool_checks_its_arguments(world: World) -> None:
    cases: list[tuple[dict[str, object], str]] = [
        ({}, "reason must be text"),
        ({"reason": 3}, "reason must be text"),
        ({"reason": "x", "now": True}, "unexpected argument now"),
    ]
    for args, message in cases:
        text, error = world.call("beta", TAKE_OWNERSHIP_TOOL_NAME, args)
        assert error and message in text


@pytest.mark.parametrize(
    ("owner", "running", "plan_held", "percent", "expected"),
    [
        (None, False, False, None, "the role is vacant"),
        ("alpha", True, True, 0, None),
        ("alpha", False, True, None, "plan limit"),
        ("alpha", False, False, 0, "0 percent"),
        ("alpha", False, False, 1, None),
        ("alpha", False, False, None, None),
    ],
)
def test_the_recovery_grounds_are_the_harness_own(
    owner: str | None,
    running: bool,
    plan_held: bool,
    percent: int | None,
    expected: str | None,
) -> None:
    budget = (
        None
        if percent is None
        else BudgetStated(
            PROVIDER, percent, None, BudgetSource.ENGINE, "", format_due(START)
        )
    )
    ground = recovery_ground(
        Role(".", 3600, owner),
        owner_running=running,
        owner_plan_held=plan_held,
        owner_budget=budget,
        now=START,
    )
    assert (ground is None) if expected is None else (expected in str(ground))


def test_an_owner_held_at_a_plan_limit_can_be_taken_from(world: World) -> None:
    world.scripts["alpha"].append([{"tool": "plan_limit"}])
    wake(world, "alpha")
    assert world.log.state.lines["alpha"].held_by is not None
    plan, ground = world.scheduler.take_ownership("beta", "alpha hit its plan")
    assert plan.owner == "beta" and "plan limit" in ground


def test_an_owner_held_otherwise_cannot_be_taken_from(world: World) -> None:
    world.scheduler.hold("alpha", "took too many turns")
    with pytest.raises(LineToolRefused, match="does not show unable to act"):
        world.scheduler.take_ownership("beta", "alpha is held")


def test_a_take_is_refused_while_the_owner_runs(world: World) -> None:
    world.budget(0)
    world.gates["alpha"] = asyncio.Event()
    world.scripts["alpha"].append([{"tool": "gate"}])
    refusals: list[str] = []

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "go on")
        await until(lambda: bool(world.prompts("alpha")))
        try:
            world.scheduler.take_ownership("beta", "alpha is spent")
        except LineToolRefused as error:
            refusals.append(str(error))
        world.gates["alpha"].set()

    run(world, act)
    assert refusals and "turn in" in refusals[0]
    assert world.role().owner == "alpha"


def test_an_operator_take_waits_for_the_owners_turn_and_wakes_the_new_owner(
    world: World,
) -> None:
    """An operator may always take, but not while the owner has a turn in flight.
    Once it has ended, the take succeeds, the seat given the role is awakened and
    woken by the operator's note, and its turn leads with the recovery checklist."""
    world.scheduler.hibernate("beta")
    world.gates["alpha"] = asyncio.Event()
    world.scripts["alpha"].append([{"tool": "gate"}])
    refusals: list[str] = []

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "go on")
        await until(lambda: bool(world.prompts("alpha")))
        try:
            world.scheduler.give_ownership(ANA, "core", "beta")
        except EventRejected as error:
            refusals.append(str(error))
        world.gates["alpha"].set()
        await until(lambda: world.order[-1:] == ["end alpha"])
        await world.scheduler.idle()
        world.scheduler.give_ownership(ANA, "core", "beta")

    run(world, act)
    assert refusals and "turn in flight. Interrupt it first" in refusals[0]
    change = world.role().changed
    assert change is not None and (change.owner, change.cause) == (
        "beta",
        OwnershipCause.OPERATOR,
    )
    assert not world.log.state.lines["beta"].hibernated
    assert world.order == ["start alpha", "end alpha", "start beta", "end beta"]
    (prompt,) = world.prompts("beta")
    assert RECOVERY_CHECKLIST in prompt


# What follows a change of owner.


def test_a_relinquish_inside_the_owners_turn_takes_effect_when_it_ends(
    world: World, tmp_path: Path
) -> None:
    """Never two writers, and never a change of owner during a turn, by the
    operator's decision of 2026-10-27. The owner relinquishes during its turn, which
    keeps the role until it ends. The change is then made, stamped with the commit
    the turn left, and the former owner's engine is dropped, so its next turn has no
    write access. The successor's first turn starts only after that."""
    world.gates["alpha"] = asyncio.Event()
    world.scripts["alpha"].append(
        [
            {"tool": RELINQUISH_OWNERSHIP_TOOL_NAME, "note": "over", "to": "beta"},
            {"tool": RELINQUISH_OWNERSHIP_TOOL_NAME, "note": "again", "to": "beta"},
            {"tool": "gate"},
        ]
    )
    later = "e" * 40

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "hand the role to beta")
        await until(lambda: len(world.results("alpha")) == 2)
        for _ in range(50):
            await asyncio.sleep(0)
        unchanged = world.role().changed
        assert world.role().owner == "alpha" and unchanged is not None
        assert unchanged.cause is OwnershipCause.DECLARED
        assert world.order == ["start alpha"]
        (tmp_path / "project" / ".git" / "HEAD").write_text(later + "\n")
        world.gates["alpha"].set()

    run(world, act)
    waiting, again = world.results("alpha")
    said = str(waiting["content"][0]["text"])
    assert waiting.get("is_error") is not True
    assert "passes to beta when this turn ends" in said and "read only" in said
    assert again.get("is_error") is True
    assert "already waits" in str(again["content"][0]["text"])
    change = world.role().changed
    assert world.role().owner == "beta" and change is not None
    assert (change.cause, change.previous) == (OwnershipCause.HANDOFF, "alpha")
    entry = world.log.state.role_entries[str(change.entry_id)]
    assert (entry.body, entry.workdir_commit) == ("over", later)
    assert world.order == ["start alpha", "end alpha", "start beta", "end beta"]
    # The former owner's engine was dropped, so its next turn is built anew, and
    # the successor's was first built after the change, as the owner.
    wake(world, "alpha", "beta")
    assert len(world.engines["alpha"]) == 2 and len(world.engines["beta"]) == 1


def test_a_waiting_relinquish_that_no_longer_applies_keeps_the_role_and_says_so(
    world: World,
) -> None:
    world.gates["alpha"] = asyncio.Event()
    world.scripts["alpha"].append(
        [
            {"tool": RELINQUISH_OWNERSHIP_TOOL_NAME, "note": "over", "to": "beta"},
            {"tool": "gate"},
        ]
    )

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "hand the role to beta")
        await until(lambda: len(world.results("alpha")) == 1)
        world.log.append(LineStateChanged("beta", LineState.STOPPED))
        world.gates["alpha"].set()

    run(world, act)
    assert world.role().owner == "alpha"
    (told,) = [
        message.body
        for message in world.log.state.messages.values()
        if message.sender == HARNESS
        and message.recipient == ANA
        and "did not make the handoff" in message.body
    ]
    assert "could not apply when the turn ended" in told


def test_a_successor_waits_for_the_former_owners_kept_connection_to_close(
    world: World,
) -> None:
    world.closing["alpha"] = asyncio.Event()
    world.scripts["alpha"].append(
        [{"tool": RELINQUISH_OWNERSHIP_TOOL_NAME, "note": "over", "to": "beta"}]
    )

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "hand the role to beta")
        await until(lambda: "closing alpha" in world.order)
        for _ in range(50):
            await asyncio.sleep(0)
        assert "start beta" not in world.order
        world.closing["alpha"].set()
        await until(lambda: "start beta" in world.order)

    run(world, act)
    assert world.order.index("closed alpha") < world.order.index("start beta")


def test_a_seat_that_does_not_own_runs_beside_the_owner(world: World) -> None:
    world.gates["alpha"] = asyncio.Event()
    world.scripts["alpha"].append([{"tool": "gate"}])

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "work")
        await until(lambda: bool(world.prompts("alpha")))
        world.scheduler.send(ANA, LineAddress("beta"), "read along")
        await until(lambda: bool(world.prompts("beta")))
        world.gates["alpha"].set()

    run(world, act)
    assert world.order.index("start beta") < world.order.index("end alpha")


@pytest.fixture
def unconfined(tmp_path: Path) -> Iterator[World]:
    """The world as it is until read-only seats merge, with no engine that can
    confine a seat."""
    target = World(tmp_path / "project", frozenset())
    yield target
    target.log.close()


def seats_waiting(world: World) -> dict[str, object]:
    seats = cast(list[dict[str, object]], world.scheduler.role_view("core")["seats"])
    return {str(seat["line_id"]): seat["waits_unconfined"] for seat in seats}


def test_a_seat_that_cannot_be_confined_waits_while_another_owns(
    unconfined: World,
) -> None:
    wake(unconfined, "alpha", "beta", "gamma", "delta")
    assert unconfined.prompts("alpha") and unconfined.prompts("delta")
    assert not unconfined.prompts("beta") and not unconfined.prompts("gamma")
    waiting = [
        message
        for message in unconfined.log.state.messages.values()
        if message.recipient in (LineAddress("beta"), LineAddress("gamma"))
    ]
    assert len(waiting) == 2 and not any(message.delivered for message in waiting)
    assert seats_waiting(unconfined) == {"alpha": False, "beta": True, "gamma": True}
    # Its mail waits until it owns the role, and then it runs.
    unconfined.scheduler.give_ownership(ANA, "core", "beta")
    wake(unconfined)
    assert unconfined.prompts("beta") and not unconfined.prompts("gamma")
    assert seats_waiting(unconfined) == {"alpha": True, "beta": False, "gamma": True}


def test_every_seat_of_a_vacant_role_runs_so_that_one_can_take_it(
    unconfined: World,
) -> None:
    unconfined.scheduler.give_ownership(ANA, "core", None)
    wake(unconfined, "alpha", "beta", "gamma")
    assert all(unconfined.prompts(line_id) for line_id in ("alpha", "beta", "gamma"))
    assert set(seats_waiting(unconfined).values()) == {False}


def test_a_seat_on_a_confining_engine_does_not_wait(world: World) -> None:
    assert set(seats_waiting(world).values()) == {False}
    assert not world.scheduler.waits_unconfined("delta")


def test_a_seats_access_follows_ownership_and_its_engine_decides_the_wait(
    world: World,
) -> None:
    state = world.log.state
    assert [seat_access(state, line) for line in ("alpha", "beta", "delta")] == [
        "write",
        "read",
        None,
    ]
    assert waits_unconfined(state, "beta", PROVIDER, frozenset())
    assert not waits_unconfined(state, "beta", PROVIDER, frozenset({PROVIDER}))
    assert not waits_unconfined(state, "beta", "codex", frozenset({"codex"}))
    assert waits_unconfined(state, "beta", "codex", frozenset({PROVIDER}))
    # The owner and a line that is not a seat never wait.
    assert not waits_unconfined(state, "alpha", PROVIDER, frozenset())
    assert not waits_unconfined(state, "delta", PROVIDER, frozenset())
    world.scheduler.give_ownership(ANA, "core", None)
    state = world.log.state
    assert seat_access(state, "alpha") == "read"
    assert not waits_unconfined(state, "beta", PROVIDER, frozenset())


def test_confining_engines_follow_the_capability_table() -> None:
    assert (
        frozenset(
            engine
            for engine, table in TABLES.items()
            if table.read_only_workdir is Known.YES
        )
        == CONFINING_ENGINES
    )
    assert "grok-build" not in CONFINING_ENGINES


def test_the_former_and_the_new_owner_are_told_at_their_next_turn(
    world: World,
) -> None:
    world.budget(0)
    world.scheduler.take_ownership("beta", "alpha is spent")
    wake(world, "alpha", "beta")
    (alpha,) = world.prompts("alpha")
    assert alpha.startswith("Role core passed from you to beta by beta")
    assert "with cause recovery" in alpha
    (beta,) = world.prompts("beta")
    assert beta.startswith("You own role core from this turn")
    assert RECOVERY_CHECKLIST in beta
    # Each is told once.
    wake(world, "alpha", "beta")
    assert not world.prompts("alpha")[1].startswith("Role core")
    assert not world.prompts("beta")[1].startswith("You own role")


def test_role_notices_survive_restart_and_reset_for_a_later_change(
    world: World,
) -> None:
    world.scheduler.give_ownership(ANA, "core", "beta")
    assert world.scheduler._role_notice("beta") is not None  # pyright: ignore[reportPrivateUsage]
    assert world.scheduler._role_notice("alpha") is not None  # pyright: ignore[reportPrivateUsage]

    # No turn ended after the change, so replay still owes each seat its notice.
    world.restart()
    assert world.scheduler._role_notice("beta") is not None  # pyright: ignore[reportPrivateUsage]
    assert world.scheduler._role_notice("alpha") is not None  # pyright: ignore[reportPrivateUsage]
    assert world.scheduler._role_notice("beta") is None  # pyright: ignore[reportPrivateUsage]
    assert world.scheduler._role_notice("alpha") is None  # pyright: ignore[reportPrivateUsage]

    for line_id in ("beta", "alpha"):
        world.log.append(LineStateChanged(line_id, LineState.RUNNING))
        world.log.append(LineStateChanged(line_id, LineState.IDLE))
        world.log.append(LineTurnEnded(line_id, WORKTREE_COMMIT))
    assert replay(world.log.path).role_change_ended_turns["core"] == {"alpha", "beta"}
    world.restart()
    assert world.scheduler._role_notice("beta") is None  # pyright: ignore[reportPrivateUsage]
    assert world.scheduler._role_notice("alpha") is None  # pyright: ignore[reportPrivateUsage]

    world.scheduler.give_ownership(ANA, "core", "alpha")
    assert world.scheduler._role_notice("alpha") is not None  # pyright: ignore[reportPrivateUsage]
    assert world.scheduler._role_notice("beta") is not None  # pyright: ignore[reportPrivateUsage]


def test_a_new_owners_pre_change_turn_does_not_consume_its_notice_after_restart(
    world: World,
) -> None:
    world.gates["beta"] = asyncio.Event()
    world.scripts["beta"] = [[{"tool": "gate"}]]

    async def give_role_during_beta_turn() -> None:
        world.scheduler.send(ANA, LineAddress("beta"), "start a turn")
        await until(lambda: "start beta" in world.order)
        # A public grant also wakes beta, which would start another turn at once.
        world.log.append(
            OwnershipChanged(
                "core",
                "beta",
                "alpha",
                OwnershipCause.OPERATOR,
                ANA,
                None,
                format_due(world.now),
            )
        )
        world.gates["beta"].set()

    run(world, give_role_during_beta_turn)
    assert "beta" not in world.log.state.role_change_ended_turns["core"]
    world.restart()
    wake(world, "beta")
    assert world.prompts("beta")[1].startswith("You own role core from this turn")


def test_a_former_owners_pre_change_turn_does_not_consume_its_notice_after_restart(
    world: World,
) -> None:
    world.gates["alpha"] = asyncio.Event()
    world.scripts["alpha"] = [[{"tool": "gate"}]]

    async def change_role_during_alpha_turn() -> None:
        world.scheduler.send(ANA, ALPHA, "start a turn")
        await until(lambda: "start alpha" in world.order)
        # The public operator command refuses a change while the current owner is
        # running. This valid event exercises the spanning-turn order directly.
        world.log.append(
            OwnershipChanged(
                "core",
                "beta",
                "alpha",
                OwnershipCause.OPERATOR,
                ANA,
                None,
                format_due(world.now),
            )
        )
        world.gates["alpha"].set()

    run(world, change_role_during_alpha_turn)
    assert "alpha" not in world.log.state.role_change_ended_turns["core"]
    world.restart()
    wake(world, "alpha")
    assert world.prompts("alpha")[1].startswith("Role core passed from you to beta")


def test_a_handoff_tells_the_successor_by_mail_not_by_the_checklist(
    world: World,
) -> None:
    world.scheduler.relinquish_ownership("alpha", "over", "beta", None)
    wake(world, "alpha")
    (beta,) = world.prompts("beta")
    assert RECOVERY_CHECKLIST not in beta
    (note,) = world.mail(HARNESS, "beta")
    assert "handed role core to you" in note
    # The relinquishing seat made the change, so its own next turn is not told of it.
    (alpha,) = world.prompts("alpha")
    assert not alpha.startswith("Role core")


def test_a_new_owner_turn_leads_with_the_journal_and_worktree_package(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_bases: list[str | None] = []

    def snapshot(
        _directory: Path, _base_commit: str | None = None
    ) -> tuple[str, str, tuple[str, ...]]:
        snapshot_bases.append(_base_commit)
        return "## feature\n M src/example.py", "src/example.py | 2 +-", ()

    monkeypatch.setattr(scheduler_module, "_worktree_snapshot", snapshot)
    note = (
        "Task: finish the review.\n"
        "Diff summary: one file.\n"
        "Gate and coverage: 3108 passed and 26 deselected.\n"
        "Unresolved decisions: none.\n"
        "Next action: operator runs socket tests."
    )
    world.scheduler.relinquish_ownership("alpha", note, "beta", None)
    wake(world, "alpha")
    (prompt,) = world.prompts("beta")
    assert prompt.startswith("You own role core from this turn")
    assert "Role transition package" in prompt
    assert 'Task: "finish the review."' in prompt
    assert 'Gate and coverage: "3108 passed and 26 deselected."' in prompt
    assert 'Next action: "operator runs socket tests."' in prompt
    assert 'Worktree status: "## feature\\n M src/example.py"' in prompt
    assert 'Diff summary since the journal entry: "src/example.py | 2 +-"' in prompt
    assert snapshot_bases == [WORKTREE_COMMIT]


def test_a_role_transition_withholds_disclosing_journal_and_snapshot_text(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def snapshot(
        _directory: Path, _base_commit: str | None = None
    ) -> tuple[str, str, tuple[str, ...]]:
        return "## feature\n M hidden_file.py", "hidden_file.py | 2 +-", ()

    monkeypatch.setattr(scheduler_module, "_worktree_snapshot", snapshot)
    note = (
        "Task: hidden task.\n"
        "Diff summary: hidden change.\n"
        "Gate and coverage: 1 passed.\n"
        "Unresolved decisions: none.\n"
        "Next action: continue."
    )
    world.scheduler.relinquish_ownership("alpha", note, "beta", None)
    world.scheduler.set_disclosure_terms(["hidden"])

    wake(world, "alpha")
    (prompt,) = world.prompts("beta")
    assert "hidden" not in prompt
    assert "[withheld because it contains a term from the disclosure list]" in prompt
    assert 'Worktree status: "[withheld' in prompt
    assert 'Diff summary since the journal entry: "[withheld' in prompt
    assert 'Task: "[withheld' in prompt
    assert 'BEGIN ROLE JOURNAL DATA\n"[withheld' in prompt


def test_role_status_shows_back_to(world: World) -> None:
    world.scheduler.relinquish_ownership("alpha", "over", "beta", "alpha")
    change = world.scheduler.role_status("beta")["last_change"]
    assert isinstance(change, dict) and change["back_to"] == "alpha"


def test_a_change_the_model_refuses_is_reported_and_records_nothing(
    world: World,
) -> None:
    """Two seats racing to take a vacant role cannot both have it."""
    world.scheduler.relinquish_ownership("alpha", "stopping", None, None)
    world.log.append(SeatBound("delta", "core", True))
    world.scheduler.take_ownership("beta", "first")
    with pytest.raises(LineToolRefused, match="does not show unable to act"):
        world.scheduler.take_ownership("delta", "second")
    with pytest.raises(EventRejected, match="owned by beta"):
        world.log.append(
            OwnershipChanged(
                "core",
                "delta",
                None,
                OwnershipCause.RECOVERY,
                LineAddress("delta"),
                None,
                format_due(START),
            )
        )


# The standing brief, task of 2026-10-29, with ``set_brief``, ``brief_set``, the
# lead of a turn on a new engine session and the sources of the package's items.

BRIEF_AT = "2026-10-29T05:00:00Z"


def brief_of(world: World, line_id: str) -> BriefSet | None:
    return world.log.state.briefs.get(line_id)


def set_brief(world: World, line_id: str, **args: object) -> tuple[str, bool]:
    return world.call(line_id, SET_BRIEF_TOOL_NAME, args)


# The event.


def test_a_brief_is_stamped_by_the_daemon_and_replaces_the_previous(
    world: World,
) -> None:
    text, failed = set_brief(world, "alpha", task="first")
    assert not failed and "brief set at" in text
    text, failed = set_brief(
        world,
        "alpha",
        task="second",
        decisions="keep the guard",
        artifacts=["src/dromon/federation.py"],
        next_action="run the gate",
    )
    assert not failed
    brief = brief_of(world, "alpha")
    assert brief is not None
    assert (brief.task, brief.decisions, brief.artifacts, brief.next_action) == (
        "second",
        "keep the guard",
        ("src/dromon/federation.py",),
        "run the gate",
    )
    assert (brief.workdir_commit, brief.daemon_commit) == (
        WORKTREE_COMMIT,
        DAEMON_COMMIT,
    )
    assert brief.set_at == "2026-10-27T09:00:00Z"
    assert brief_of(world, "beta") is None


def test_set_brief_is_offered_to_every_line(world: World) -> None:
    # delta is a seat of no role, so it is offered no role tool, but a brief.
    for line_id in ("alpha", "gamma", "delta"):
        names = {tool.name for tool in world.scheduler.own_tools(line_id)}
        assert SET_BRIEF_TOOL_NAME in names


def test_a_brief_survives_replay(world: World, tmp_path: Path) -> None:
    set_brief(world, "beta", task="carry on", artifacts=["tmp/a.md", "docs/b c.md"])
    expected = brief_of(world, "beta")
    world.log.close()
    reopened = EventLog(tmp_path / "project" / "events.sqlite")
    try:
        assert reopened.state.briefs == {"beta": expected}
        assert reopened.state.copy().briefs == reopened.state.briefs
    finally:
        reopened.close()
    # The fixture closes the log again, which a closed log allows.
    world.log = EventLog(tmp_path / "project" / "events.sqlite")


def test_a_brief_for_no_line_is_rejected(world: World) -> None:
    with pytest.raises(Exception, match="no line ghost"):
        world.log.append(BriefSet("ghost", "a task", "", (), "", "", "", BRIEF_AT))


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"task": ""}, "task must not be empty"),
        ({"task": "x" * (MAX_BRIEF_FIELD_BYTES + 1)}, "task"),
        ({"decisions": "x" * (MAX_BRIEF_FIELD_BYTES + 1)}, "decisions"),
        ({"next_action": "x" * (MAX_BRIEF_FIELD_BYTES + 1)}, "next_action"),
        ({"artifacts": ("a",) * (MAX_BRIEF_ARTIFACTS + 1)}, "at most 32 artifacts"),
        ({"artifacts": ("",)}, "non-empty path on one line"),
        ({"artifacts": ("a\nb",)}, "non-empty path on one line"),
        ({"artifacts": ("x" * 1025,)}, "artifact"),
        ({"workdir_commit": "not a commit"}, "workdir_commit"),
        ({"set_at": "yesterday"}, "set_at"),
    ],
)
def test_the_event_refuses_each_unbounded_or_malformed_field(
    fields: dict[str, Any], message: str
) -> None:
    values: dict[str, Any] = {
        "line_id": "alpha",
        "task": "a task",
        "decisions": "",
        "artifacts": (),
        "next_action": "",
        "workdir_commit": "",
        "daemon_commit": "",
        "set_at": BRIEF_AT,
    }
    values.update(fields)
    with pytest.raises(InvalidField, match=message):
        BriefSet(**values)


def test_the_total_size_is_bounded_though_each_field_fits() -> None:
    field = "x" * MAX_BRIEF_FIELD_BYTES
    paths = tuple(f"{index:03}" + "p" * 1000 for index in range(MAX_BRIEF_ARTIFACTS))
    assert 3 * MAX_BRIEF_FIELD_BYTES < MAX_BRIEF_BYTES
    with pytest.raises(InvalidField, match=f"at most {MAX_BRIEF_BYTES} bytes"):
        BriefSet("alpha", field, field, paths, field, "", "", BRIEF_AT)
    BriefSet("alpha", field, field, (), field, "", "", BRIEF_AT)


# The tool's refusals record nothing.


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"task": "a", "owner": "me"}, "owner"),
        ({"task": 3}, "must be text"),
        ({"task": "a", "artifacts": "src"}, "list of paths"),
        ({"task": "a", "artifacts": [1]}, "list of paths"),
        ({"task": "x" * (MAX_BRIEF_FIELD_BYTES + 1)}, "task"),
        ({"task": "a", "artifacts": ["a"] * (MAX_BRIEF_ARTIFACTS + 1)}, "at most"),
    ],
)
def test_the_tool_refuses_unknown_keys_wrong_types_and_oversized_fields(
    world: World, args: dict[str, object], message: str
) -> None:
    text, failed = world.call("alpha", SET_BRIEF_TOOL_NAME, args)
    assert failed and message in text
    assert brief_of(world, "alpha") is None


def test_a_brief_with_a_disclosed_term_is_refused_and_counted(world: World) -> None:
    world.scheduler.set_disclosure_terms(["hidden"])
    for args in (
        {"task": "the hidden task"},
        {"task": "a", "decisions": "hidden"},
        {"task": "a", "next_action": "hidden"},
        {"task": "a", "artifacts": ["docs/hidden.md"]},
    ):
        text, failed = set_brief(world, "alpha", **args)
        assert failed and "hidden" not in text
    assert brief_of(world, "alpha") is None
    assert len(world.log.state.disclosure_checks["alpha"]) == 4
    with pytest.raises(LineToolRefused):
        world.scheduler.set_brief("alpha", "hidden")


# The lead of a turn whose engine session is new.


def test_the_first_turn_on_a_new_engine_leads_with_the_brief(world: World) -> None:
    set_brief(world, "alpha", task="lift the bound", next_action="run the gate")
    wake(world, "alpha")
    wake(world, "alpha")
    first, second = world.prompts("alpha")
    assert first.startswith("Your engine session is new since your last turn")
    assert f"{BRIEF_BEGIN}\n" in first and f"\n{BRIEF_END}" in first
    assert '"task": "lift the bound"' in first
    assert BRIEF_BEGIN not in second


def test_a_dropped_engine_and_a_compaction_each_bring_the_lead_back(
    world: World,
) -> None:
    set_brief(world, "alpha", task="lift the bound")
    wake(world, "alpha")
    # The engine compacted the session by itself during the turn.
    world.scheduler.session_compacted("alpha", {"trigger": "auto"})
    wake(world, "alpha")
    wake(world, "alpha")
    # A rebind, a cleared session or a restart leaves the line with no engine.
    world.scheduler._drop_engine("alpha")  # pyright: ignore[reportPrivateUsage]
    wake(world, "alpha")
    leads = [BRIEF_BEGIN in prompt for prompt in world.prompts("alpha")]
    assert leads == [True, True, False, True]


def test_a_turn_that_fails_before_its_engine_gets_the_prompt_keeps_the_lead_owed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review r4, finding 1. The engine is kept, so only the owed mark of its own
    compaction calls for the lead, and the failed turn must not spend it."""
    set_brief(world, "alpha", task="lift the bound")
    wake(world, "alpha")
    world.scheduler.session_compacted("alpha", {"trigger": "auto"})
    walks: list[str] = []

    async def refused_walk(line_id: str) -> None:
        walks.append(line_id)
        raise RuntimeError("refused before the engine ran")

    monkeypatch.setattr(world.scheduler, "_check_walk", refused_walk)
    wake(world, "alpha")
    assert walks == ["alpha"]
    assert len(world.prompts("alpha")) == 1
    monkeypatch.undo()
    world.scheduler.release("alpha")
    wake(world, "alpha")
    wake(world, "alpha")
    leads = [BRIEF_BEGIN in prompt for prompt in world.prompts("alpha")]
    assert leads == [True, True, False]


def test_a_line_with_no_brief_gets_no_lead(world: World) -> None:
    wake(world, "alpha")
    (prompt,) = world.prompts("alpha")
    assert BRIEF_BEGIN not in prompt


def test_a_brief_that_now_discloses_is_withheld_from_the_lead(world: World) -> None:
    set_brief(world, "alpha", task="the hidden task")
    world.scheduler.set_disclosure_terms(["hidden"])
    wake(world, "alpha")
    (prompt,) = world.prompts("alpha")
    assert "hidden" not in prompt
    assert f"{BRIEF_BEGIN}\n[withheld because" in prompt


MAIL_SINCE = " Operator mail has arrived since this brief was set at"


def note_from(world: World, sender: LineAddress, line_id: str) -> None:
    """Wake ``line_id`` with a note from ``sender``, not from an operator."""

    async def act() -> None:
        world.scheduler.send(sender, LineAddress(line_id), "not an operator's")

    run(world, act)


def test_a_lead_says_when_operator_mail_is_newer_than_the_brief(world: World) -> None:
    """Task of 2026-10-30. The review line's brief was stale after a compaction,
    since its task had changed in mail since the brief was set."""
    set_brief(world, "alpha", task="lift the bound")
    brief = brief_of(world, "alpha")
    assert brief is not None
    world.now = START + timedelta(minutes=5)
    wake(world, "alpha")
    (first,) = world.prompts("alpha")
    sentence = (
        f"{MAIL_SINCE} {brief.set_at}, the newest at {format_due(world.now)},"
        " so the task may have changed in that mail.\n"
    )
    assert sentence in first
    assert first.index(sentence) < first.index(BRIEF_BEGIN)
    # The brief itself is left as the line wrote it.
    assert brief_of(world, "alpha") == brief


def test_a_lead_says_nothing_of_mail_when_the_brief_is_newer(world: World) -> None:
    wake(world, "alpha")
    world.now = START + timedelta(minutes=5)
    set_brief(world, "alpha", task="lift the bound")
    world.scheduler._drop_engine("alpha")  # pyright: ignore[reportPrivateUsage]
    note_from(world, LineAddress("beta"), "alpha")
    note_from(world, LineAddress("alpha"), "alpha")
    leads = [p for p in world.prompts("alpha") if BRIEF_BEGIN in p]
    assert len(leads) == 1
    assert MAIL_SINCE not in leads[0]
    assert "alpha" not in world.log.state.mail_since_brief


def test_mail_since_the_brief_is_rebuilt_from_the_log_alone(world: World) -> None:
    """Only an operator's message counts, a new brief clears it, and replay
    agrees."""
    set_brief(world, "alpha", task="first")
    note_from(world, LineAddress("beta"), "alpha")
    assert "alpha" not in world.log.state.mail_since_brief
    wake(world, "alpha", "beta")
    newest = world.log.state.mail_since_brief["alpha"]
    assert world.log.state.messages[newest].sender == ANA
    assert world.log.state.mail_since_brief["beta"] != newest
    assert replay(world.log.path).mail_since_brief == world.log.state.mail_since_brief
    set_brief(world, "alpha", task="second")
    assert "alpha" not in world.log.state.mail_since_brief
    assert "alpha" not in replay(world.log.path).mail_since_brief


def test_after_a_restart_the_time_is_read_from_the_log(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The time of mail sent before a restart comes from its entry's
    ``recorded_at``, and a log that has no entry for it says so."""
    set_brief(world, "alpha", task="lift the bound")
    world.scheduler.send(ANA, LineAddress("alpha"), "a new task")
    newest = world.log.state.mail_since_brief["alpha"]
    (entry,) = [
        e
        for e in read_entries(world.log.path)
        if isinstance(e.event, MessageSent) and e.event.message_id == newest
    ]
    recorded = format_due(datetime.fromisoformat(entry.recorded_at))
    # A restart forgets the time this process kept.
    world.scheduler._operator_mail_at.clear()  # pyright: ignore[reportPrivateUsage]
    lead = world.scheduler._brief_lead("alpha", False)  # pyright: ignore[reportPrivateUsage]
    assert lead is not None and f"the newest at {recorded}," in lead
    world.scheduler._operator_mail_at.clear()  # pyright: ignore[reportPrivateUsage]

    scans: list[Path] = []

    def no_entries(path: Path) -> list[Entry]:
        scans.append(path)
        return []

    monkeypatch.setattr(scheduler_module, "read_entries", no_entries)
    lead = world.scheduler._brief_lead("alpha", False)  # pyright: ignore[reportPrivateUsage]
    assert lead is not None
    assert "the newest at a time the event log does not give," in lead
    # Review r6, finding 7. The miss is kept, so later views do not read again.
    again = world.scheduler._brief_lead("alpha", False)  # pyright: ignore[reportPrivateUsage]
    status = cast(dict[str, Any], world.scheduler.line_status("alpha")["brief"])
    assert again == lead
    assert status["operator_mail_since"] == {"message_id": newest, "sent_at": None}
    assert scans == [world.log.path]
    # Newer operator mail sent by this process brings its own time, with no read.
    world.scheduler.send(ANA, LineAddress("alpha"), "another task")
    later = world.scheduler._brief_lead("alpha", False)  # pyright: ignore[reportPrivateUsage]
    assert later is not None and f"the newest at {format_due(world.now)}," in later
    assert scans == [world.log.path]


def test_line_status_names_operator_mail_since_the_brief(world: World) -> None:
    """Review r6, finding 8. A turn that does not lead with the brief sees the same
    fact in ``line_status``, in detail and compact, and a new brief clears it."""
    set_brief(world, "alpha", task="lift the bound")

    def mail_since() -> tuple[object, object]:
        detail = cast(dict[str, Any], world.scheduler.line_status("alpha")["brief"])
        compact = cast(
            dict[str, Any],
            world.scheduler.line_status("alpha", detail=False, lists=False)["brief"],
        )
        return detail["operator_mail_since"], compact["operator_mail_since"]

    assert mail_since() == (None, None)
    note_from(world, LineAddress("beta"), "alpha")
    assert mail_since() == (None, None)
    world.now = START + timedelta(minutes=5)
    message_id = world.scheduler.send(ANA, LineAddress("alpha"), "a new task")
    expected = {"message_id": message_id, "sent_at": format_due(world.now)}
    assert world.log.state.mail_since_brief["alpha"] == message_id
    assert mail_since() == (expected, expected)
    set_brief(world, "alpha", task="the new task")
    assert mail_since() == (None, None)


# line_status.


def test_line_status_shows_the_brief_as_untrusted_data(world: World) -> None:
    assert world.scheduler.line_status("alpha")["brief"] is None
    long_task = "t" * 1000
    set_brief(world, "alpha", task=long_task, artifacts=["a.md", "b.md"])
    full = cast(dict[str, Any], world.scheduler.line_status("alpha")["brief"])
    assert "untrusted" in full["trust"]
    assert full["untrusted_data"]["task"] == long_task
    assert full["workdir_commit"] == WORKTREE_COMMIT
    compact = cast(
        dict[str, Any],
        world.scheduler.line_status("alpha", detail=False, lists=False)["brief"],
    )
    assert compact["untrusted_data"] == {
        "task": long_task[:400],
        "next_action": "",
        "artifact_count": 2,
    }


# The role transition package.


def taken_by_beta(world: World) -> str:
    """beta takes the spent owner's role and returns its first prompt's package."""
    world.budget(0)
    world.scheduler.take_ownership("beta", "alpha is spent")
    wake(world, "beta")
    (prompt,) = world.prompts("beta")
    return prompt


type Snapshot = Callable[[Path, str | None], tuple[str, str, tuple[str, ...]]]


def fake_snapshot(
    status: str = "## feature\n M src/example.py",
    unreadable: tuple[str, ...] = (),
) -> Snapshot:
    def fake(
        _directory: Path, _base_commit: str | None = None
    ) -> tuple[str, str, tuple[str, ...]]:
        return status, "src/example.py | 2 +-", unreadable

    return fake


def test_the_package_without_a_brief_marks_each_field_unknown(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "_worktree_snapshot", fake_snapshot())
    prompt = taken_by_beta(world)
    assert 'Task: "unknown"; unknown, stated in neither' in prompt
    assert 'Gate and coverage: "unknown"; unknown, stated in neither' in prompt
    assert "and not observed by the daemon" in prompt
    assert 'Next action: "unknown"; unknown' in prompt
    assert 'Artifacts: "unknown"; unknown, named in no brief' in prompt
    assert (
        'Worktree status: "## feature\\n M src/example.py"; observed by the daemon'
        " at 2026-10-27T09:00:00Z" in prompt
    )


# Fields the daemon observed for a former owner that stated nothing.

GATE_DIGEST = "d" * 64


def mail(world: World, sender: OperatorAddress | LineAddress, body: str) -> str:
    """Send alpha ``body`` from ``sender`` and run until alpha's turn delivers it,
    returning the message identifier."""
    sent: list[str] = []

    async def act() -> None:
        sent.append(world.scheduler.send(sender, ALPHA, body))

    run(world, act)
    return sent[0]


def record_task(world: World, command: str = "./scripts/check.sh") -> None:
    world.log.append(
        TaskRecorded(
            "alpha",
            "bt1",
            command,
            "completed",
            0,
            None,
            GATE_DIGEST,
            120,
            WORKTREE_COMMIT,
            format_due(START),
        )
    )


def test_a_silent_package_takes_task_and_gate_from_what_the_daemon_observed(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task of 2026-10-29. alpha set no brief and wrote no entry, so Task comes
    from its latest operator mail and Gate and coverage from its latest task
    record. The fields neither covers stay unknown."""
    monkeypatch.setattr(scheduler_module, "_worktree_snapshot", fake_snapshot())
    message_id = mail(world, ANA, "Build the observed package.")
    record_task(world, "./scripts/old.sh")
    record_task(world)
    prompt = taken_by_beta(world)
    assert (
        f'Task: "Build the observed package."; operator mail {message_id} from'
        " @ana, the latest delivered to alpha, observed by the daemon, which records"
        " no time for mail. It may not state the task"
    ) in prompt
    gate = (
        '{"command": "./scripts/check.sh", "status": "completed", "exit_code": 0,'
        f' "output_digest": "{GATE_DIGEST}", "output_bytes": 120,'
        f' "workdir_commit": "{WORKTREE_COMMIT}"}}'
    )
    assert (
        f"Gate and coverage: {gate}; observed by the daemon at 2026-10-27T09:00:00Z,"
        ' when background task "bt1" of alpha ended. It is that line\'s latest'
        " recorded task and may not be a gate"
    ) in prompt
    assert 'Next action: "unknown"; unknown' in prompt
    assert 'Unresolved decisions: "unknown"; unknown' in prompt


def test_a_line_with_no_task_record_leaves_the_gate_unknown(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "_worktree_snapshot", fake_snapshot())
    mail(world, ANA, "the task")
    assert "alpha" not in world.log.state.task_records
    prompt = taken_by_beta(world)
    assert 'Task: "the task"; operator mail' in prompt
    assert 'Gate and coverage: "unknown"; unknown, stated in neither' in prompt


def test_another_lines_mail_never_fills_the_task(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an operator holds operator authority, so the latest operator mail is
    taken over newer mail from a line, and mail from lines alone fills nothing."""
    monkeypatch.setattr(scheduler_module, "_worktree_snapshot", fake_snapshot())
    mail(world, LineAddress("gamma"), "a task from a line")
    alone = world.scheduler._observed_fields("alpha")  # pyright: ignore[reportPrivateUsage]
    assert "Task" not in alone
    operator_id = mail(world, ANA, "the operator's task")
    mail(world, LineAddress("gamma"), "a newer task from a line")
    prompt = taken_by_beta(world)
    assert f'Task: "the operator\'s task"; operator mail {operator_id}' in prompt
    assert "a newer task from a line" not in prompt
    assert "a task from a line" not in prompt


def test_a_remote_operators_mail_never_fills_the_task(world: World) -> None:
    """Review r4, findings F2 and F5. A peer's operator is a ``RemoteAddress``, which
    is never read as a local operator, so its mail fills nothing. The message is put
    in the state directly, since declaring a peer is beside the point here."""
    world.log.state.messages["m-remote"] = Message(
        RemoteAddress("studio", OperatorAddress("ana")),
        ALPHA,
        MessageKind.NOTE,
        "a task from another daemon's operator",
        (),
        None,
        delivered=True,
    )
    observed = world.scheduler._observed_fields("alpha")  # pyright: ignore[reportPrivateUsage]
    assert "Task" not in observed


def test_operator_mail_is_cut_to_its_first_bytes(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "_worktree_snapshot", fake_snapshot())
    mail(world, ANA, "x" * (MAX_PACKAGE_MAIL_BYTES + 50))
    prompt = taken_by_beta(world)
    shown = "x" * MAX_PACKAGE_MAIL_BYTES
    assert f'Task: "{shown} [first {MAX_PACKAGE_MAIL_BYTES} bytes]"' in prompt
    assert "x" * (MAX_PACKAGE_MAIL_BYTES + 1) not in prompt


def test_a_disclosed_term_in_observed_fields_is_withheld(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "_worktree_snapshot", fake_snapshot())
    message_id = mail(world, ANA, "the hidden task")
    record_task(world, "./scripts/hidden.sh")
    world.scheduler.set_disclosure_terms(["hidden"])
    prompt = taken_by_beta(world)
    assert "hidden" not in prompt
    assert "[withheld because" in prompt
    assert f"operator mail {message_id} from @ana" in prompt
    assert 'when background task "bt1" of alpha ended' in prompt


def test_the_package_takes_what_the_journal_lacks_from_the_former_brief(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "_worktree_snapshot", fake_snapshot())
    world.scheduler.record_role_note(
        "alpha", RoleNoteKind.NOTE, "Task: from the journal."
    )
    set_brief(
        world,
        "alpha",
        task="from the brief",
        decisions="keep the guard",
        artifacts=["tmp/report.md"],
        next_action="run the gate",
    )
    prompt = taken_by_beta(world)
    # beta's take wrote the latest entry, which states no field, so each comes from
    # alpha's brief.
    assert (
        'Task: "from the brief"; reported by alpha in its standing brief at'
        " 2026-10-27T09:00:00Z, since the journal entry does not state it"
    ) in prompt
    assert 'Unresolved decisions: "keep the guard"; reported by alpha' in prompt
    assert 'Next action: "run the gate"; reported by alpha' in prompt
    assert 'Artifacts: ["tmp/report.md"]; reported by alpha' in prompt
    assert 'Gate and coverage: "unknown"; unknown' in prompt


def test_a_field_the_journal_states_comes_from_the_journal(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "_worktree_snapshot", fake_snapshot())
    set_brief(world, "alpha", task="from the brief", next_action="from the brief")
    world.scheduler.relinquish_ownership(
        "alpha", "Task: from the journal.", "beta", None
    )
    wake(world, "alpha")
    (prompt,) = world.prompts("beta")
    entry = world.log.state.roles["core"].changed
    assert entry is not None and entry.entry_id is not None
    assert (
        f'Task: "from the journal."; reported by alpha in journal entry'
        f" {entry.entry_id} at 2026-10-27T09:00:00Z"
    ) in prompt
    assert 'Next action: "from the brief"; reported by alpha in its standing' in prompt


def test_an_unreadable_path_is_never_reported_absent_or_untracked(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        scheduler_module, "_worktree_snapshot", fake_snapshot(unreadable=(".dromon/",))
    )
    prompt = taken_by_beta(world)
    assert (
        'Paths the daemon could not read: [".dromon/"]; git reported them'
        " unreadable at 2026-10-27T09:00:00Z. Each may exist and be tracked,"
        " untracked or ignored, so none is reported absent or untracked"
    ) in prompt


def test_a_worktree_the_daemon_cannot_read_is_marked_so(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        scheduler_module, "_worktree_snapshot", fake_snapshot(status="unavailable")
    )
    prompt = taken_by_beta(world)
    assert (
        'Worktree status: "unavailable"; the daemon could not read it at'
        " 2026-10-27T09:00:00Z, which says nothing of what the worktree holds"
    ) in prompt


def test_the_former_owners_processes_are_observed_or_unknown(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "_worktree_snapshot", fake_snapshot())
    scheduler = world.scheduler
    scheduler._processes_identifiable = False  # pyright: ignore[reportPrivateUsage]
    assert 'Former owner\'s processes: "unknown"; unknown, since this platform' in (
        scheduler._former_processes("alpha")  # pyright: ignore[reportPrivateUsage]
    )
    scheduler._processes_identifiable = True  # pyright: ignore[reportPrivateUsage]
    assert '"none recorded"; observed by the daemon' in (
        scheduler._former_processes("alpha")  # pyright: ignore[reportPrivateUsage]
    )
    assert '"unknown"; unknown, since the role names no former owner' in (
        scheduler._former_processes(None)  # pyright: ignore[reportPrivateUsage]
    )
    world.log.append(ProcessesLeft("alpha", "exited", (41, 42), ("a", "b"), 1))
    assert '"3 left running"; observed by the daemon when the engine ended' in (
        scheduler._former_processes("alpha")  # pyright: ignore[reportPrivateUsage]
    )
    prompt = taken_by_beta(world)
    assert 'Former owner\'s processes: "3 left running"' in prompt


# Git's unreadable paths.


def test_unreadable_paths_are_read_from_gits_warnings() -> None:
    stderr = (
        "warning: could not open directory 'closed/': Permission denied\n"
        "something else\n"
        'error: open("secret.txt"): Permission denied\n'
    )
    assert scheduler_module._unreadable_paths(stderr) == (  # pyright: ignore[reportPrivateUsage]
        "closed/",
        "secret.txt",
    )
    many = "\n".join(
        f"could not open directory 'd{index}/': Permission denied"
        for index in range(40)
    )
    assert len(scheduler_module._unreadable_paths(many)) == (  # pyright: ignore[reportPrivateUsage]
        scheduler_module.MAX_UNREADABLE_PATHS
    )


def test_a_quote_in_an_unreadable_name_is_recorded_whole() -> None:
    """Review r4, finding 3. The first two warnings are as git gave them on
    2026-10-29 for unreadable directories named ``say"hi`` and ``it's``, unescaped.
    The third is the file form written to match, and was not observed. The last
    two names hold git's closing sequence and are recorded only up to it."""
    stderr = (
        "warning: could not open directory 'say\"hi/': Permission denied\n"
        "warning: could not open directory 'it's/': Permission denied\n"
        'error: open("say"hi.txt"): Permission denied\n'
        "warning: could not open directory 'a': b/': Permission denied\n"
        'error: open("c")d"): Permission denied\n'
    )
    assert scheduler_module._unreadable_paths(stderr) == (  # pyright: ignore[reportPrivateUsage]
        'say"hi/',
        "it's/",
        'say"hi.txt',
        "a",
        "c",
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every directory")
def test_a_real_unreadable_directory_is_reported_unreadable(tmp_path: Path) -> None:
    """Fact, observed with git on 2026-10-29. git status leaves an unreadable
    directory out of its output and exits 0, so only its warning names it."""
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    git = ["git", "-C", str(checkout), "-c", "user.email=a@b", "-c", "user.name=a"]
    subprocess.run([*git, "init", "-q"], check=True)  # noqa: S603
    (checkout / "a").write_text("a\n")
    subprocess.run([*git, "add", "a"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-qm", "a"], check=True)  # noqa: S603
    closed = checkout / "closed"
    closed.mkdir()
    (closed / "b").write_text("b\n")
    closed.chmod(0)
    try:
        status, _, unreadable = scheduler_module._worktree_snapshot(checkout)  # pyright: ignore[reportPrivateUsage]
    finally:
        closed.chmod(0o755)
    assert "closed" not in status
    assert unreadable == ("closed/",)
