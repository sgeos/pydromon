"""Automatic switching, roles step 3 of ``docs/architecture/ROLES.md``: a role's
failover policy, the declared seat order a failover follows, the planned handoff a low
budget asks for, and the plan limits an engine reports as its provider's budget."""

import asyncio
from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import RateLimitInfo

from dromon import codec
from dromon.driver import ENGINE_OF, Strategy
from dromon.engine import EnginePlanLimit, TurnReport, rejected_limit
from dromon.eventlog import EventLog
from dromon.model import (
    DEFAULT_HANDOFF_BELOW_PERCENT,
    BudgetSource,
    BudgetStated,
    EventRejected,
    FailoverPolicy,
    InvalidField,
    LineAddress,
    MessageKind,
    OperatorAddress,
    OwnershipCause,
    OwnershipChanged,
    Role,
    RoleDeclared,
    RoleNoted,
    RoleNoteKind,
    SeatBound,
    format_due,
)
from dromon.roles import (
    RoleConflict,
    Stamp,
    below_handoff_threshold,
    next_seat,
    plan_failover,
    seat_order,
)
from dromon.scheduler import RECOVERY_CHECKLIST, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
LUNA = "gpt-6-luna"
GROK = "grok-4.7"
CLAUDE = ENGINE_OF[HAIKU]
CODEX = ENGINE_OF[LUNA]
ANA = OperatorAddress("ana")
START = datetime(2026, 10, 27, 9, 0, 0, tzinfo=UTC)
AT = format_due(START)
WORKTREE_COMMIT = "c" * 40
STAMP = Stamp(WORKTREE_COMMIT, "", AT)


class Scripted:
    """A line engine whose turns end normally, or at a plan limit when scripted to."""

    def __init__(self, world: "World", spec: LineSpec) -> None:
        self.world = world
        self.line_id = spec.line_id
        self.session_id: str | None = None

    async def run_turn(self, prompt: str) -> TurnReport:
        self.world.prompts.setdefault(self.line_id, []).append(prompt)
        script = self.world.scripts.get(self.line_id, [])
        if script:
            limit = script.pop(0)
            if limit is not None:
                raise limit
        return TurnReport("success", 1, "ok", (), None, None, "session-1", None, None)


class World:
    """Role core on the project root, declared in the order alpha, beta, gamma,
    delta. Alpha owns it on Claude Code, beta runs on Codex, gamma on Claude Code,
    and delta on Grok and may not own it. Every engine counts as able to confine a
    seat, so the confinement rule stays out of these tests."""

    def __init__(
        self,
        root: Path,
        failover: FailoverPolicy = FailoverPolicy.AUTOMATIC,
        below: int = DEFAULT_HANDOFF_BELOW_PERCENT,
    ) -> None:
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text(WORKTREE_COMMIT + "\n")
        self.log = EventLog(root / "events.sqlite")
        self.now = START
        self.prompts: dict[str, list[str]] = {}
        self.scripts: dict[str, list[EnginePlanLimit | None]] = {}
        self.scheduler = Scheduler(
            self.log,
            lambda spec, tools: Scripted(self, spec),
            tick_seconds=0,
            clock=lambda: 0.0,
            wall_clock=lambda: self.now,
            root=root,
            confining_engines=frozenset(ENGINE_OF.values()),
        )
        self.scheduler.ensure_operator("ana")
        models = {"alpha": HAIKU, "beta": LUNA, "gamma": HAIKU, "delta": GROK}
        for line_id, model in models.items():
            self.scheduler.create_line(line_id, model, Strategy.SUBSCRIPTION)
        order = ("alpha", "beta", "gamma", "delta")
        self.log.append(RoleDeclared("core", ".", 3600, order, failover, below))
        for line_id in order:
            self.log.append(SeatBound(line_id, "core", line_id != "delta"))
        self.log.append(
            OwnershipChanged(
                "core", "alpha", None, OwnershipCause.DECLARED, ANA, None, AT
            )
        )

    def owner(self) -> str | None:
        return self.log.state.roles["core"].owner

    def mail(self, sender: str, recipient: str | OperatorAddress) -> list[str]:
        """The notes, not the questions, from ``sender`` to ``recipient``."""
        to = LineAddress(recipient) if isinstance(recipient, str) else recipient
        return [
            message.body
            for message in self.log.state.messages.values()
            if message.sender == LineAddress(sender)
            and message.recipient == to
            and message.kind is MessageKind.NOTE
        ]

    def state_budget(self, provider: str, percent: int) -> list[str]:
        result = self.scheduler.state_budget(provider, percent, None, "")
        return cast(list[str], result["followed"])


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    target = World(tmp_path / "project")
    yield target
    target.log.close()


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
    async def act() -> None:
        for line_id in line_ids:
            world.scheduler.send(ANA, LineAddress(line_id), "go on")

    run(world, act)


def limit(resets_at: str | None = None) -> EnginePlanLimit:
    """A limit read from a shape the Agent SDK types, which may move a role."""
    return EnginePlanLimit(
        "Claude subscription plan limit reached", resets_at, may_move_role=True
    )


# The order a failover follows.


def test_the_next_seat_follows_the_order_round_from_the_owner() -> None:
    order = ("a", "b", "c", "d")
    assert next_seat(order, "b", frozenset({"a", "c"})) == "c"
    assert next_seat(order, "c", frozenset({"a", "b"})) == "a"
    assert next_seat(order, "d", frozenset({"a", "b", "c", "d"})) == "a"
    assert next_seat(order, None, frozenset({"b"})) == "b"
    assert next_seat(order, "x", frozenset({"c"})) == "c"
    # The owner itself is never its own successor, nor is a seat that is not usable.
    assert next_seat(order, "b", frozenset({"b"})) is None
    assert next_seat(order, "b", frozenset()) is None
    assert next_seat((), "b", frozenset({"b"})) is None


def test_the_seat_order_is_the_declared_one_then_any_other_seat(world: World) -> None:
    assert seat_order(world.log.state, "core") == ("alpha", "beta", "gamma", "delta")
    world.log.append(RoleDeclared("core", ".", 3600, ("gamma", "nobody", "alpha")))
    # A name that is not a seat is skipped, and a seat left out follows in bound order.
    assert seat_order(world.log.state, "core") == ("gamma", "alpha", "beta", "delta")


def test_a_budget_below_the_threshold_asks_for_a_handoff() -> None:
    role = Role(".", 3600, "alpha", handoff_below_percent=10)

    def stated(percent: int | None) -> BudgetStated:
        return BudgetStated(CLAUDE, percent, AT, BudgetSource.OPERATOR, "", AT)

    assert below_handoff_threshold(role, stated(9))
    assert below_handoff_threshold(role, stated(1))
    assert not below_handoff_threshold(role, stated(10))
    assert not below_handoff_threshold(role, stated(0))
    assert not below_handoff_threshold(role, stated(None))
    assert not below_handoff_threshold(role, None)
    never = Role(".", 3600, "alpha", handoff_below_percent=0)
    assert not below_handoff_threshold(never, stated(1))


# The model and the codec.


def test_a_failover_is_recorded_under_the_seat_made_owner_with_its_entry() -> None:
    by = LineAddress("beta")
    cause = OwnershipCause.FAILOVER
    OwnershipChanged("core", "beta", "alpha", cause, by, "e1", AT)
    for owner, previous, entry_id, maker in (
        ("beta", None, "e1", by),
        ("beta", "alpha", None, by),
        ("beta", "alpha", "e1", LineAddress("alpha")),
        ("beta", "alpha", "e1", ANA),
        (None, "alpha", "e1", by),
    ):
        with pytest.raises(InvalidField):
            OwnershipChanged("core", owner, previous, cause, maker, entry_id, AT)


def test_a_role_declaration_checks_its_order_policy_and_threshold() -> None:
    with pytest.raises(InvalidField, match="twice"):
        RoleDeclared("core", ".", 3600, ("a", "a"))
    with pytest.raises(InvalidField, match="at most"):
        RoleDeclared("core", ".", 3600, tuple(f"s{n}" for n in range(17)))
    with pytest.raises(InvalidField):
        RoleDeclared("core", ".", 3600, ("not an id",))
    with pytest.raises(InvalidField, match="failover"):
        RoleDeclared("core", ".", 3600, (), cast(FailoverPolicy, "sometimes"))
    for percent in (-1, 101, cast(int, True)):
        with pytest.raises(InvalidField, match="handoff_below_percent"):
            RoleDeclared("core", ".", 3600, (), FailoverPolicy.ASK, percent)


def test_a_declaration_that_changes_only_the_policy_is_recorded(world: World) -> None:
    same = RoleDeclared("core", ".", 3600, ("alpha", "beta", "gamma", "delta"))
    with pytest.raises(EventRejected, match="declared so already"):
        world.log.append(same)
    world.log.append(
        RoleDeclared(
            "core", ".", 3600, same.seats, FailoverPolicy.ASK, handoff_below_percent=5
        )
    )
    role = world.log.state.roles["core"]
    assert (role.failover, role.handoff_below_percent, role.owner) == (
        FailoverPolicy.ASK,
        5,
        "alpha",
    )


def test_the_new_fields_round_trip_and_an_earlier_declaration_decodes() -> None:
    declared = RoleDeclared("core", ".", 600, ("a", "b"), FailoverPolicy.ASK, 25)
    assert codec.decode(codec.encode(declared)) == declared
    change = OwnershipChanged(
        "core", "b", "a", OwnershipCause.FAILOVER, LineAddress("b"), "e1", AT
    )
    assert codec.decode(codec.encode(change)) == change
    earlier = (
        '{"v":1,"kind":"role_declared","data":'
        '{"role_id":"core","workdir":".","handoff_seconds":600}}'
    )
    assert codec.decode(earlier) == RoleDeclared("core", ".", 600)
    partial = (
        '{"v":1,"kind":"role_declared","data":'
        '{"role_id":"core","workdir":".","handoff_seconds":600,"seats":[]}}'
    )
    with pytest.raises(codec.DecodeError, match="keys"):
        codec.decode(partial)


# Failover.


def test_failover_plan_rests_on_an_entry_the_harness_wrote(world: World) -> None:
    plan = plan_failover(world.log.state, "core", "beta", "it is spent", "e1", STAMP)
    entry, change = plan.events
    assert isinstance(entry, RoleNoted) and entry.kind is RoleNoteKind.NOTE
    assert entry.line_id == "beta" and "recorded by the harness" in entry.body
    assert "because it is spent" in entry.body
    assert isinstance(change, OwnershipChanged)
    assert change.cause is OwnershipCause.FAILOVER and change.entry_id == "e1"
    with pytest.raises(RoleConflict, match="may not own"):
        plan_failover(world.log.state, "core", "delta", "spent", "e2", STAMP)
    world.scheduler.give_ownership(ANA, "core", None)
    with pytest.raises(RoleConflict, match="vacant"):
        plan_failover(world.log.state, "core", "beta", "spent", "e3", STAMP)


def test_two_failovers_planned_from_one_state_cannot_both_apply(world: World) -> None:
    """The review's race. Each change names the owner it replaces, so the second of
    two plans made from the same state is refused by the model and moves nothing."""
    state = world.log.state
    first = plan_failover(state, "core", "beta", "spent", "e1", STAMP)
    second = plan_failover(state, "core", "gamma", "spent", "e2", STAMP)
    for event in first.events:
        world.log.append(event)
    _, change = second.events
    with pytest.raises(EventRejected, match="owned by beta, not alpha"):
        world.log.append(change)
    assert world.owner() == "beta"


def test_an_owner_at_a_plan_limit_fails_over_to_the_next_usable_seat(
    world: World,
) -> None:
    resets = format_due(datetime(2026, 10, 28, 15, 0, 0, tzinfo=UTC))
    world.scripts["alpha"] = [limit(resets)]
    wake(world, "alpha")
    # Its engine's report is the provider's budget, with the reset time it gave.
    budget = world.log.state.budgets[CLAUDE]
    assert (budget.remaining_percent, budget.resets_at, budget.source) == (
        0,
        resets,
        BudgetSource.ENGINE,
    )
    # Beta follows alpha in the order, and runs on another provider.
    assert world.owner() == "beta"
    change = world.log.state.roles["core"].changed
    assert change is not None and change.cause is OwnershipCause.FAILOVER
    (told,) = world.mail("alpha", ANA)
    assert told.startswith("Dromon failed over. Role core passed by failover")
    assert "held at a plan limit" in told
    (woken,) = world.mail("beta", "beta")
    assert "passed role core to you by failover" in woken
    (beta,) = world.prompts["beta"]
    assert beta.startswith("You own role core from this turn, by the harness")
    assert RECOVERY_CHECKLIST in beta
    assert world.log.state.lines["alpha"].held_by is not None


def test_an_inferred_limit_holds_and_asks_but_never_moves_the_role(
    world: World,
) -> None:
    """The operator's decision of 2026-10-27. A limit read from inferred codes or
    free text holds its line and asks, records no budget, and grounds neither a
    failover nor a seat's own take."""
    world.scripts["alpha"] = [EnginePlanLimit("Codex plan limit reached")]
    wake(world, "alpha")
    assert world.owner() == "alpha"
    assert CLAUDE not in world.log.state.budgets
    question = world.log.state.lines["alpha"].held_by
    assert question is not None
    asked = world.log.state.messages[question].body
    assert "never observed live" in asked and "moved no role" in asked
    assert world.mail("alpha", ANA) == []
    assert world.scheduler.fail_over_if_unable("alpha") is None
    assert world.owner() == "alpha"
    # The operator may still move it.
    world.scheduler.give_ownership(ANA, "core", "beta")
    assert world.owner() == "beta"


def test_only_the_observed_and_typed_shapes_may_move_a_role() -> None:
    assert not EnginePlanLimit("unknown").may_move_role
    info = RateLimitInfo(status="rejected", resets_at=None, rate_limit_type=None)
    typed = rejected_limit(info)
    assert typed is not None and typed.may_move_role


def test_a_failover_passes_over_seats_that_are_not_usable(world: World) -> None:
    world.scheduler.hibernate("beta")
    world.scripts["alpha"] = [limit()]
    wake(world, "alpha")
    # Gamma is on alpha's provider, whose budget the limit recorded as spent, and
    # delta may not own the role, so no seat is usable.
    assert world.owner() == "alpha"
    (told,) = world.mail("alpha", ANA)
    assert told.startswith("Dromon could not fail over. Role core stays with alpha")
    # A further trigger with nothing changed is not told again.
    assert world.scheduler.fail_over_if_unable("alpha") is not None
    assert len(world.mail("alpha", ANA)) == 1
    # Once a seat is usable, a statement of the budget passes the role to it.
    world.scheduler.awaken("beta")
    followed = world.state_budget(CLAUDE, 0)
    change = world.log.state.roles["core"].changed
    assert change is not None and world.owner() == "beta"
    assert followed == [
        "Role core passed by failover from alpha to beta, because its owner alpha"
        f" is held at a plan limit its engine reported, with entry {change.entry_id}."
    ]


def test_under_ask_no_owner_changes(tmp_path: Path) -> None:
    world = World(tmp_path / "project", FailoverPolicy.ASK)
    try:
        world.scripts["alpha"] = [limit()]
        wake(world, "alpha")
        assert world.owner() == "alpha"
        assert world.log.state.lines["alpha"].held_by is not None
        assert not world.mail("beta", "beta")
        (said,) = world.state_budget(CLAUDE, 0)
        assert said.startswith("Role core stays with alpha, because its failover")
    finally:
        world.log.close()


def test_a_budget_stated_spent_fails_the_owner_over(world: World) -> None:
    (said,) = world.state_budget(CLAUDE, 0)
    assert "from alpha to beta, because its owner alpha's provider budget" in said
    assert world.owner() == "beta"
    # A statement for another provider moves nothing.
    assert world.state_budget("grok-build", 0) == []


def test_a_low_budget_asks_the_owner_to_prepare_a_handoff(world: World) -> None:
    assert world.state_budget(CLAUDE, 10) == []
    (said,) = world.state_budget(CLAUDE, 9)
    assert said.startswith("Role core's owner alpha was asked to prepare a handoff")
    (asked,) = world.mail("alpha", "alpha")
    assert "prepare a handoff of role core to beta" in asked
    assert "relinquish_ownership with to set to beta" in asked
    assert world.owner() == "alpha"


def test_a_low_budget_with_no_usable_seat_asks_nothing(world: World) -> None:
    # Beta's provider is spent, gamma is hibernated, and delta may not own the role.
    world.state_budget(CODEX, 0)
    world.scheduler.hibernate("gamma")
    (said,) = world.state_budget(CLAUDE, 5)
    assert said.endswith("since no other seat is usable.")
    assert not world.mail("alpha", "alpha")


def test_an_owner_in_flight_fails_over_when_its_turn_ends(world: World) -> None:
    gate = asyncio.Event()
    said: list[str] = []

    class Gated(Scripted):
        async def run_turn(self, prompt: str) -> TurnReport:
            await gate.wait()
            return await super().run_turn(prompt)

    def build(spec: LineSpec, tools: object) -> Scripted:
        return Gated(world, spec) if spec.line_id == "alpha" else Scripted(world, spec)

    world.scheduler._engine_factory = build  # pyright: ignore[reportPrivateUsage]

    async def act() -> None:
        world.scheduler.send(ANA, LineAddress("alpha"), "go on")
        for _ in range(200):
            if "alpha" in world.scheduler.running_lines():
                break
            await asyncio.sleep(0)
        said.extend(world.state_budget(CLAUDE, 0))
        assert world.owner() == "alpha"
        gate.set()

    run(world, act)
    assert said == [
        "Role core's owner alpha has a turn in flight, so the role fails over when"
        " that turn ends."
    ]
    assert world.owner() == "beta"


def test_role_view_shows_the_order_policy_and_next_seat(world: World) -> None:
    view = world.scheduler.role_view("core")
    assert (view["failover"], view["handoff_below_percent"], view["next_seat"]) == (
        "automatic",
        DEFAULT_HANDOFF_BELOW_PERCENT,
        "beta",
    )
    seats = cast(list[dict[str, object]], view["seats"])
    assert [(seat["line_id"], seat["usable"]) for seat in seats] == [
        ("alpha", True),
        ("beta", True),
        ("gamma", True),
        ("delta", False),
    ]


# The engines' reports.


def test_a_rejected_window_without_overage_is_a_plan_limit() -> None:
    resets = int(datetime(2026, 10, 28, 15, 0, 0, tzinfo=UTC).timestamp())
    found = rejected_limit(RateLimitInfo("rejected", resets, "five_hour"))
    assert found is not None
    assert found.resets_at == "2026-10-28T15:00:00Z"
    assert "five_hour window, which resets at 2026-10-28T15:00:00Z" in str(found)
    unnamed = rejected_limit(RateLimitInfo("rejected"))
    assert unnamed is not None and unnamed.resets_at is None
    assert "in its unnamed window" in str(unnamed)
    for info in (
        RateLimitInfo("allowed", resets),
        RateLimitInfo("allowed_warning", resets),
        RateLimitInfo("rejected", resets, overage_status="allowed"),
        RateLimitInfo("rejected", resets, overage_status="allowed_warning"),
    ):
        assert rejected_limit(info) is None
    # Overage that is itself rejected serves nothing.
    spent = RateLimitInfo("rejected", resets, overage_status="rejected")
    assert rejected_limit(spent) is not None


def test_a_reset_time_out_of_range_is_left_out() -> None:
    found = rejected_limit(RateLimitInfo("rejected", 10**20))
    assert found is not None and found.resets_at is None


def test_a_spent_codex_credit_states_its_provider_spent_once(world: World) -> None:
    world.scheduler.credits_spent("beta", "Codex reported no credit left")
    budget = world.log.state.budgets[CODEX]
    assert (budget.remaining_percent, budget.resets_at, budget.source) == (
        0,
        None,
        BudgetSource.ENGINE,
    )
    assert budget.note == "Codex reported no credit left"
    # The engine repeats its report, which states nothing new while the budget is spent.
    world.now = START.replace(hour=10)
    world.scheduler.credits_spent("beta", "again")
    assert world.log.state.budgets[CODEX] == budget
    # Alpha, the owner, is on another provider, so only beta stops being usable.
    assert world.owner() == "alpha"
    assert world.scheduler.next_seat("core") == "gamma"
    # A line the log does not know states nothing.
    world.scheduler.credits_spent("nobody", "spent")


def test_an_owner_out_of_credit_between_turns_fails_over_at_once(
    world: World,
) -> None:
    world.scheduler.give_ownership(ANA, "core", "beta")
    world.scheduler.credits_spent("beta", "Codex reported no credit left")
    assert world.owner() == "gamma"
    change = world.log.state.roles["core"].changed
    assert change is not None and change.cause is OwnershipCause.FAILOVER
    (told,) = world.mail("beta", ANA)
    assert "from beta to gamma, because its owner beta's provider budget" in told


def test_a_reset_time_in_another_form_is_dropped_from_the_budget(
    world: World,
) -> None:
    world.scheduler.plan_limit_reached("alpha", limit("tomorrow"))
    budget = world.log.state.budgets[CLAUDE]
    assert budget.remaining_percent == 0 and budget.resets_at is None
    assert budget.note == "Claude subscription plan limit reached"
