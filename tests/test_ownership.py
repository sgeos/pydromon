"""The ownership tools, ``record_role_note``, ``relinquish_ownership`` and
``take_ownership``, with what follows a change of owner: the engines dropped, the
successor woken, the former writer's turn waited for, and each seat told at its next
turn. Roles step 2, ``docs/architecture/ROLES.md``."""

import asyncio
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
from dromon.eventlog import EventLog
from dromon.model import (
    MAX_ROLE_NOTE_BYTES,
    BudgetSource,
    BudgetStated,
    EventRejected,
    InvalidField,
    LineAddress,
    LineState,
    LineStateChanged,
    OperatorAddress,
    OwnershipCause,
    OwnershipChanged,
    Role,
    RoleDeclared,
    RoleNoteKind,
    SeatBound,
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
    MAX_TAKE_REASON_BYTES,
    RECORD_ROLE_NOTE_TOOL_NAME,
    RECOVERY_CHECKLIST,
    RELINQUISH_OWNERSHIP_TOOL_NAME,
    ROLE_STATUS_TOOL_NAME,
    TAKE_OWNERSHIP_TOOL_NAME,
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

    def mail(self, sender: str, recipient: str) -> list[str]:
        """The bodies of the delivered notes from ``sender`` to ``recipient``."""
        return [
            message.body
            for message in self.log.state.messages.values()
            if message.sender == LineAddress(sender)
            and message.recipient == LineAddress(recipient)
            and message.delivered
        ]

    def role(self) -> Role:
        return self.log.state.roles["core"]

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
    (note,) = world.mail("alpha", "beta")
    assert f"handed role core to you, with handoff entry {entry.entry_id}" in note


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
        if message.sender == ALPHA
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


def test_a_handoff_tells_the_successor_by_mail_not_by_the_checklist(
    world: World,
) -> None:
    world.scheduler.relinquish_ownership("alpha", "over", "beta", None)
    wake(world, "alpha")
    (beta,) = world.prompts("beta")
    assert RECOVERY_CHECKLIST not in beta
    (note,) = world.mail("alpha", "beta")
    assert "handed role core to you" in note
    # The relinquishing seat made the change, so its own next turn is not told of it.
    (alpha,) = world.prompts("alpha")
    assert not alpha.startswith("Role core")


def test_a_new_owner_turn_leads_with_the_journal_and_worktree_package(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot_bases: list[str | None] = []

    def snapshot(_directory: Path, _base_commit: str | None = None) -> tuple[str, str]:
        snapshot_bases.append(_base_commit)
        return "## feature\n M src/example.py", "src/example.py | 2 +-"

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
    def snapshot(_directory: Path, _base_commit: str | None = None) -> tuple[str, str]:
        return "## feature\n M hidden_file.py", "hidden_file.py | 2 +-"

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
