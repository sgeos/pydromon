"""Roles, seats, ownership, the role journal and provider budgets in the data model,
their codec, and the ``role_status`` view. ``docs/architecture/ROLES.md``."""

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.codec import DecodeError, decode, encode
from dromon.model import (
    DEFAULT_HANDOFF_SECONDS,
    MAX_HANDOFF_SECONDS,
    MAX_ROLE_NOTE_BYTES,
    MIN_HANDOFF_SECONDS,
    AgentAccess,
    AgentAddress,
    AgentRegistered,
    BudgetSource,
    BudgetStated,
    Event,
    EventRejected,
    InvalidField,
    LineAddress,
    LineCreated,
    LineState,
    LineStateChanged,
    OperatorAddress,
    OperatorRegistered,
    OwnershipCause,
    OwnershipChanged,
    RoleDeclared,
    RoleNoted,
    RoleNoteKind,
    SeatBound,
    State,
)
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.scheduler import (
    ROLE_STATUS_ENTRIES,
    ROLE_STATUS_TOOL_NAME,
    LineEngine,
    LineSpec,
    LineToolRefused,
    Scheduler,
)

ENGINE = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"
AT = "2026-10-27T05:00:00Z"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
BETA = LineAddress("beta")
GAMMA = LineAddress("gamma")
DECLARED = OwnershipCause.DECLARED
HANDOFF = OwnershipCause.HANDOFF
RECOVERY = OwnershipCause.RECOVERY
OPERATOR = OwnershipCause.OPERATOR
RELINQUISHED = OwnershipCause.RELINQUISHED


def changed(
    owner: str | None,
    previous: str | None,
    cause: OwnershipCause,
    by: LineAddress | OperatorAddress | AgentAddress = ANA,
    entry_id: str | None = None,
    role_id: str = "core",
) -> OwnershipChanged:
    return OwnershipChanged(role_id, owner, previous, cause, by, entry_id, AT)


def noted(
    entry_id: str, line_id: str, kind: RoleNoteKind, role_id: str = "core"
) -> RoleNoted:
    return RoleNoted(role_id, entry_id, line_id, kind, "a note", "", "", AT)


def built(*events: Event) -> State:
    """A state with lines alpha, beta and gamma, operator ana, role core whose
    seats are alpha and beta, and role docs, then ``events``."""
    state = State()
    base: tuple[Event, ...] = (
        LineCreated("alpha", ENGINE, "subscription"),
        LineCreated("beta", ENGINE, "subscription"),
        LineCreated("gamma", ENGINE, "subscription"),
        OperatorRegistered("ana"),
        RoleDeclared("core", "tmp/worktrees/core"),
        RoleDeclared("docs", "tmp/worktrees/docs"),
        SeatBound("alpha", "core", True),
        SeatBound("beta", "core", True),
        SeatBound("gamma", "docs", True),
    )
    for event in base + events:
        state.apply(event)
    return state


def owned_by_alpha(*events: Event) -> State:
    return built(changed("alpha", None, DECLARED), *events)


# Field validation.


@pytest.mark.parametrize(
    "workdir",
    ["", "/abs", "a/../b", "a//b", "./a", "a/.", "a\\b", "a\x00b", "..", "x" * 1025],
)
def test_a_workdir_outside_the_project_or_unnormalized_is_refused(
    workdir: str,
) -> None:
    with pytest.raises(InvalidField):
        RoleDeclared("core", workdir)


@pytest.mark.parametrize("workdir", [".", "tmp/worktrees/core", "a.b/c-d"])
def test_a_relative_workdir_or_the_root_is_accepted(workdir: str) -> None:
    assert RoleDeclared("core", workdir).workdir == workdir


def test_the_handoff_bound_defaults_to_an_hour_and_is_bounded() -> None:
    assert RoleDeclared("core", ".").handoff_seconds == DEFAULT_HANDOFF_SECONDS == 3600
    RoleDeclared("core", ".", MIN_HANDOFF_SECONDS)
    RoleDeclared("core", ".", MAX_HANDOFF_SECONDS)
    for seconds in (MIN_HANDOFF_SECONDS - 1, MAX_HANDOFF_SECONDS + 1, True, 60.0):
        with pytest.raises(InvalidField):
            RoleDeclared("core", ".", seconds)  # pyright: ignore[reportArgumentType]


def test_may_own_is_a_boolean() -> None:
    with pytest.raises(InvalidField):
        SeatBound("alpha", "core", 1)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("owner", "previous", "cause", "by", "entry_id"),
    [
        ("alpha", "alpha", OPERATOR, ANA, None),
        (None, None, OPERATOR, ANA, None),
        (None, "alpha", HANDOFF, ALPHA, "e1"),
        (None, "alpha", RECOVERY, ALPHA, None),
        (None, None, DECLARED, ANA, None),
        ("beta", "alpha", RELINQUISHED, ALPHA, None),
        ("alpha", "beta", DECLARED, ANA, None),
        ("beta", None, HANDOFF, ALPHA, "e1"),
        ("beta", "alpha", HANDOFF, ALPHA, None),
        ("alpha", None, DECLARED, ALPHA, None),
        ("alpha", "beta", OPERATOR, BETA, None),
        ("beta", "alpha", HANDOFF, BETA, "e1"),
        (None, "alpha", RELINQUISHED, ANA, None),
        ("beta", "alpha", RECOVERY, ALPHA, None),
        ("beta", "alpha", RECOVERY, AgentAddress("probe"), None),
    ],
)
def test_an_inconsistent_ownership_change_is_refused(
    owner: str | None,
    previous: str | None,
    cause: OwnershipCause,
    by: LineAddress | OperatorAddress | AgentAddress,
    entry_id: str | None,
) -> None:
    with pytest.raises(InvalidField):
        changed(owner, previous, cause, by, entry_id)


def test_an_ownership_change_needs_a_valid_time() -> None:
    with pytest.raises(InvalidField):
        OwnershipChanged("core", "alpha", None, DECLARED, ANA, None, "2026-10-27")


def test_a_journal_entry_is_bounded_nonempty_and_stamped_with_commits() -> None:
    RoleNoted("core", "e1", "alpha", RoleNoteKind.NOTE, "x", "a" * 40, "b" * 64, AT)
    with pytest.raises(InvalidField):
        RoleNoted("core", "e1", "alpha", RoleNoteKind.NOTE, "", "", "", AT)
    with pytest.raises(InvalidField):
        RoleNoted(
            "core",
            "e1",
            "alpha",
            RoleNoteKind.NOTE,
            "x" * (MAX_ROLE_NOTE_BYTES + 1),
            "",
            "",
            AT,
        )
    for commit in ("A" * 40, "a" * 39, "g" * 40, "HEAD"):
        with pytest.raises(InvalidField):
            RoleNoted("core", "e1", "alpha", RoleNoteKind.NOTE, "x", commit, "", AT)


def test_a_budget_states_a_percentage_or_a_reset() -> None:
    BudgetStated("codex", 0, None, BudgetSource.ENGINE, "", AT)
    BudgetStated("codex", None, AT, BudgetSource.OPERATOR, "", AT)
    for percent in (-1, 101, True):
        with pytest.raises(InvalidField):
            BudgetStated("codex", percent, None, BudgetSource.OPERATOR, "", AT)
    with pytest.raises(InvalidField):
        BudgetStated("codex", None, None, BudgetSource.OPERATOR, "", AT)
    with pytest.raises(InvalidField):
        BudgetStated("codex", 5, "tomorrow", BudgetSource.OPERATOR, "", AT)


# State checks.


def test_a_role_is_declared_again_only_to_change_it() -> None:
    state = built()
    with pytest.raises(EventRejected, match="so already"):
        state.check(RoleDeclared("core", "tmp/worktrees/core"))
    state.apply(RoleDeclared("core", "tmp/worktrees/core", 7200))
    assert state.roles["core"].handoff_seconds == 7200
    state.apply(RoleDeclared("core", "tmp/worktrees/other", 7200))
    assert state.roles["core"].workdir == "tmp/worktrees/other"


def test_an_owned_role_keeps_its_worktree() -> None:
    state = owned_by_alpha()
    with pytest.raises(EventRejected, match="only while vacant"):
        state.check(RoleDeclared("core", "tmp/worktrees/other"))
    state.apply(RoleDeclared("core", "tmp/worktrees/core", 120))
    assert state.roles["core"].owner == "alpha"


def test_a_seat_needs_its_line_and_role_and_holds_one_role() -> None:
    state = built()
    with pytest.raises(EventRejected, match="no line"):
        state.check(SeatBound("delta", "core", True))
    with pytest.raises(EventRejected, match="no role"):
        state.check(SeatBound("alpha", "ops", True))
    with pytest.raises(EventRejected, match="is a seat of role core"):
        state.check(SeatBound("alpha", "docs", True))
    with pytest.raises(EventRejected, match="so already"):
        state.check(SeatBound("alpha", "core", True))
    state.apply(SeatBound("alpha", "core", False))
    assert not state.seats["alpha"].may_own


def test_an_owner_cannot_be_barred_from_owning() -> None:
    state = owned_by_alpha()
    with pytest.raises(EventRejected, match="give the role up"):
        state.check(SeatBound("alpha", "core", False))


def test_ownership_changes_only_from_the_current_owner() -> None:
    state = owned_by_alpha()
    racing = changed("beta", None, OPERATOR)
    with pytest.raises(EventRejected, match="owned by alpha, not no seat"):
        state.check(racing)
    state.apply(changed("beta", "alpha", OPERATOR))
    with pytest.raises(EventRejected, match="owned by beta, not alpha"):
        state.check(changed(None, "alpha", OPERATOR))


def test_two_takes_that_race_cannot_both_apply() -> None:
    """Both name the same previous owner, so the second is refused once the first
    applies, the compare and swap the design relies on."""
    state = owned_by_alpha(changed(None, "alpha", RELINQUISHED, ALPHA))
    first = changed("alpha", None, RECOVERY, ALPHA)
    second = changed("beta", None, RECOVERY, BETA)
    state.check(first)
    state.check(second)
    state.apply(first)
    with pytest.raises(EventRejected, match="owned by alpha"):
        state.check(second)


def test_an_owner_is_a_live_seat_of_the_role_that_may_own() -> None:
    state = built()
    with pytest.raises(EventRejected, match="not a seat of role core"):
        state.check(changed("gamma", None, DECLARED))
    state.apply(SeatBound("beta", "core", False))
    with pytest.raises(EventRejected, match="may not own"):
        state.check(changed("beta", None, DECLARED))
    state.apply(LineStateChanged("alpha", LineState.STOPPED))
    with pytest.raises(EventRejected, match="stopped"):
        state.check(changed("alpha", None, DECLARED))


def test_a_change_by_a_line_is_by_a_seat_of_the_role() -> None:
    state = built()
    with pytest.raises(EventRejected, match="no role"):
        state.check(changed("alpha", None, DECLARED, role_id="ops"))
    vacant = owned_by_alpha(changed(None, "alpha", RELINQUISHED, ALPHA))
    with pytest.raises(EventRejected, match="gamma is not a seat of role core"):
        vacant.check(changed("gamma", None, RECOVERY, GAMMA))
    with pytest.raises(EventRejected, match="beta is not a seat of role docs"):
        vacant.check(changed("beta", None, RECOVERY, BETA, role_id="docs"))


def test_a_change_by_an_unknown_party_is_refused() -> None:
    state = built()
    with pytest.raises(EventRejected, match="no operator ben"):
        state.check(changed("alpha", None, DECLARED, OperatorAddress("ben")))
    state.apply(AgentRegistered("probe", AgentAccess.OPERATOR, ""))
    with pytest.raises(InvalidField):
        changed("alpha", None, RECOVERY, AgentAddress("probe"))


def test_a_handoff_rests_on_the_owners_latest_handoff_entry() -> None:
    state = owned_by_alpha()
    with pytest.raises(EventRejected, match="no entry e1"):
        state.check(changed("beta", "alpha", HANDOFF, ALPHA, "e1"))
    state.apply(noted("e1", "alpha", RoleNoteKind.NOTE))
    with pytest.raises(EventRejected, match="not the latest entry"):
        state.check(changed("beta", "alpha", HANDOFF, ALPHA, "e1"))
    state.apply(noted("e2", "alpha", RoleNoteKind.HANDOFF))
    state.apply(noted("e3", "beta", RoleNoteKind.NOTE))
    with pytest.raises(EventRejected, match="not the latest entry"):
        state.check(changed("beta", "alpha", HANDOFF, ALPHA, "e2"))
    state.apply(noted("e4", "alpha", RoleNoteKind.HANDOFF))
    state.apply(changed("beta", "alpha", HANDOFF, ALPHA, "e4"))
    role = state.roles["core"]
    assert role.owner == "beta"
    assert role.changed is not None and role.changed.entry_id == "e4"


def test_an_entry_of_another_role_cannot_carry_a_change() -> None:
    state = owned_by_alpha(noted("g1", "gamma", RoleNoteKind.NOTE, "docs"))
    with pytest.raises(EventRejected, match="no entry g1"):
        state.check(changed("beta", "alpha", OPERATOR, entry_id="g1"))


def test_a_handoff_or_recovery_entry_is_by_the_owner_and_a_note_by_any_seat() -> None:
    state = owned_by_alpha()
    for kind in (RoleNoteKind.HANDOFF, RoleNoteKind.RECOVERY):
        with pytest.raises(EventRejected, match="written by its owner"):
            state.check(noted("e1", "beta", kind))
        state.check(noted("e1", "alpha", kind))
    state.check(noted("e1", "beta", RoleNoteKind.NOTE))
    with pytest.raises(EventRejected, match="not a seat"):
        state.check(noted("e1", "gamma", RoleNoteKind.NOTE))
    with pytest.raises(EventRejected, match="no role"):
        state.check(noted("e1", "alpha", RoleNoteKind.NOTE, "ops"))
    state.apply(noted("e1", "beta", RoleNoteKind.NOTE))
    with pytest.raises(EventRejected, match="exists"):
        state.check(noted("e1", "alpha", RoleNoteKind.NOTE))
    assert state.latest_entry("core") == "e1"
    assert state.latest_entry("docs") is None


def test_a_vacant_role_takes_no_handoff_or_recovery_entry() -> None:
    state = built()
    with pytest.raises(EventRejected, match="written by its owner"):
        state.check(noted("e1", "alpha", RoleNoteKind.RECOVERY))


def test_a_budget_replaces_the_providers_earlier_one() -> None:
    state = built()
    state.apply(BudgetStated("codex", 30, None, BudgetSource.OPERATOR, "", AT))
    state.apply(BudgetStated("codex", 0, None, BudgetSource.ENGINE, "limit", AT))
    assert state.budgets["codex"].remaining_percent == 0
    assert state.budgets["codex"].source is BudgetSource.ENGINE


def test_a_copy_is_independent_of_the_roles() -> None:
    state = owned_by_alpha()
    snapshot = state.copy()
    state.apply(changed("beta", "alpha", OPERATOR))
    state.apply(SeatBound("alpha", "core", False))
    state.apply(noted("e1", "beta", RoleNoteKind.NOTE))
    state.apply(BudgetStated("codex", 3, None, BudgetSource.OPERATOR, "", AT))
    assert snapshot.roles["core"].owner == "alpha"
    assert snapshot.seats["alpha"].may_own
    assert not snapshot.role_entries and not snapshot.budgets


# The codec.


@pytest.mark.parametrize(
    ("kind", "data", "reason"),
    [
        ("seat_bound", {"line_id": "a", "role_id": "r", "may_own": 1}, "true or false"),
        (
            "role_declared",
            {"role_id": "r", "workdir": ".", "handoff_seconds": True},
            "whole number",
        ),
        (
            "budget_stated",
            {
                "provider": "codex",
                "remaining_percent": "5",
                "resets_at": None,
                "source": "operator",
                "note": "",
                "stated_at": AT,
            },
            "whole number or null",
        ),
        (
            "ownership_changed",
            {
                "role_id": "r",
                "owner": "a",
                "previous": None,
                "cause": "taken",
                "by": "@ana",
                "entry_id": None,
                "changed_at": AT,
                "back_to": None,
            },
            "no value 'taken'",
        ),
        (
            "role_noted",
            {
                "role_id": "r",
                "entry_id": "e",
                "line_id": "a",
                "kind": "diary",
                "body": "x",
                "workdir_commit": "",
                "daemon_commit": "",
                "noted_at": AT,
            },
            "no value 'diary'",
        ),
    ],
)
def test_a_malformed_role_event_is_refused(
    kind: str, data: dict[str, object], reason: str
) -> None:
    payload = json.dumps({"data": data, "kind": kind, "v": 1})
    with pytest.raises(DecodeError, match=reason):
        decode(payload)


def test_an_ownership_change_carries_its_addresses_as_text() -> None:
    event = changed("beta", "alpha", HANDOFF, ALPHA, "e4")
    document = json.loads(encode(event))
    assert document["data"]["by"] == "alpha"
    assert json.loads(encode(changed("alpha", None, DECLARED)))["data"]["by"] == "@ana"
    assert decode(encode(event)) == event


# The role_status view and tool.


def run_tool(tool: SdkMcpTool[Any], args: dict[str, object]) -> dict[str, Any]:
    async def invoke() -> dict[str, Any]:
        return await tool.handler(args)

    return asyncio.run(invoke())


def scheduler_with_roles(tmp_path: Path) -> tuple[EventLog, Scheduler]:
    log = EventLog(tmp_path / "events.sqlite")

    def build(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        raise AssertionError("no turn runs here")

    scheduler = Scheduler(log, build, tick_seconds=0)
    scheduler.ensure_operator("ana")
    for line_id in ("alpha", "beta", "gamma"):
        scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
    for event in (
        RoleDeclared("core", "tmp/worktrees/core", 1800),
        SeatBound("alpha", "core", True),
        SeatBound("beta", "core", False),
        changed("alpha", None, DECLARED),
        *(noted(f"e{index}", "beta", RoleNoteKind.NOTE) for index in range(6)),
        RoleNoted(
            "core", "e6", "alpha", RoleNoteKind.HANDOFF, "the next step", "", "", AT
        ),
        BudgetStated("claude-code", 25, None, BudgetSource.OPERATOR, "stated", AT),
    ):
        log.append(event)
    return log, scheduler


def test_role_status_shows_the_role_its_seats_and_the_latest_entries(
    tmp_path: Path,
) -> None:
    log, scheduler = scheduler_with_roles(tmp_path)
    try:
        view = scheduler.role_status("beta")
        assert view["role_id"] == "core"
        assert view["workdir"] == "tmp/worktrees/core"
        assert view["handoff_seconds"] == 1800
        assert view["owner"] == "alpha"
        assert view["you_own"] is False and view["you_may_own"] is False
        assert view["last_change"] == {
            "owner": "alpha",
            "previous": None,
            "cause": "declared",
            "by": "@ana",
            "entry_id": None,
            "changed_at": AT,
            "back_to": None,
        }
        seats = cast(list[dict[str, object]], view["seats"])
        assert [seat["line_id"] for seat in seats] == ["alpha", "beta"]
        assert [seat["owner"] for seat in seats] == [True, False]
        assert seats[0]["provider"] == "claude-code"
        assert seats[0]["condition"] == "idle"
        budget = cast(dict[str, object], seats[0]["budget"])
        assert budget["remaining_percent"] == 25 and budget["source"] == "operator"
        journal = cast(list[dict[str, object]], view["journal"])
        assert len(journal) == ROLE_STATUS_ENTRIES
        assert journal[-1]["entry_id"] == "e6"
        assert journal[-1]["body"] == "the next step"
        assert all("body" not in entry for entry in journal[:-1])
        assert journal[0]["chars"] == len("a note")
        assert scheduler.role_status("alpha")["you_own"] is True
        # The view crosses the client protocol, which carries no floats.
        json.dumps(view, allow_nan=False)
    finally:
        log.close()


def test_role_status_is_offered_only_to_a_seat(tmp_path: Path) -> None:
    log, scheduler = scheduler_with_roles(tmp_path)
    try:
        names = [tool.name for tool in scheduler.own_tools("alpha")]
        assert ROLE_STATUS_TOOL_NAME in names
        assert ROLE_STATUS_TOOL_NAME not in [
            tool.name for tool in scheduler.own_tools("gamma")
        ]
        with pytest.raises(LineToolRefused, match="seat of no role"):
            scheduler.role_status("gamma")
        tool = scheduler.role_status_tool("gamma")
        refused = run_tool(tool, {})
        assert refused["is_error"] is True
        found = {t.name: t for t in scheduler.own_tools("beta")}
        shown = run_tool(found[ROLE_STATUS_TOOL_NAME], {})
        assert json.loads(shown["content"][0]["text"])["owner"] == "alpha"
        unexpected = run_tool(found[ROLE_STATUS_TOOL_NAME], {"x": 1})
        assert unexpected["is_error"] is True
    finally:
        log.close()
