"""The project file's roles, as the events that record them in the log.

``docs/architecture/ROLES.md`` gives the design and ``docs/protocol/PROJECT_CONFIG.md``
the file's tables. The daemon plans the events for the file's roles against a copy of
the log's state, so that a file the model would refuse is refused whole, before any of
it is applied, and then appends the same plan to the log. Planning reads nothing but
its arguments, so a port carries it unchanged.

What the file can and cannot change. A role absent from the log is declared, and one
whose worktree or handoff time changed is declared again, which the model allows only
while the role is vacant if the worktree moved. A seat absent from the log, or whose
``may_own`` changed, is bound. The file's ``owner`` is recorded, with cause
``declared``, only while the role has never had an owner, so a restart does not undo
a handoff, a recovery or a relinquishment. The log has no event that unbinds a seat or
retires a role, so a seat or a role dropped from the file stays in the log as it was.
**Technical debt.** An unbinding event is for a later step, once the operator decides
what becomes of a removed seat's ownership.

The ownership tools and the operator's ownership commands are planned here too, as
plain functions of the state and of the facts the scheduler observes, which a port
carries unchanged. The model checks that a
change is consistent, and ``recovery_ground`` decides whether a seat had the authority
to take a role itself, which the model leaves to the daemon. A seat's access to the
worktree, and whether a seat whose engine cannot confine it may run, are decided here
as well.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from dromon.model import (
    BudgetStated,
    Event,
    EventRejected,
    InvalidField,
    LineAddress,
    LineCreated,
    LineState,
    OperatorAddress,
    OperatorRegistered,
    OwnershipCause,
    OwnershipChanged,
    Role,
    RoleDeclared,
    RoleNoted,
    RoleNoteKind,
    Seat,
    SeatBound,
    State,
    parse_due,
)
from dromon.projectconfig import ProjectConfig


class RoleConflict(ValueError):
    """The file's roles, or an ownership tool's request, cannot be applied to the log
    as it stands."""


@dataclass(frozen=True, slots=True)
class Stamp:
    """What the daemon observes as it records a journal entry: the role worktree's
    commit and its own, each empty where it could not be read, and the time in the
    model's due-time format."""

    workdir_commit: str
    daemon_commit: str
    at: str


@dataclass(frozen=True, slots=True)
class Plan:
    """The events an ownership tool appends, in order, and what they leave."""

    events: tuple[Event, ...]
    role_id: str
    owner: str | None
    previous: str | None
    entry_id: str | None
    note: str
    """What the caller is told beyond the change itself, possibly empty."""


def plan_roles(
    state: State, config: ProjectConfig, by: OperatorAddress, at: str
) -> tuple[Event, ...]:
    """The events that bring ``state`` in line with ``config``'s roles, each checked
    against the state the ones before it leave, or ``RoleConflict`` naming the role.

    ``by`` is the operator a declared ownership is recorded as made by, the project's
    operator, registered first if the log does not know it. ``at`` is its time, in
    the model's due-time format. A line the file declares and the log lacks is assumed
    to be created first, as the daemon does, so the plan can be made before it is."""
    trial = state.copy()
    for line_id, definition in sorted(config.lines.items()):
        if line_id not in trial.lines:
            trial.apply(
                LineCreated(line_id, definition.model, definition.strategy.value)
            )
    events: list[Event] = []

    def add(role_id: str, event: Event) -> None:
        try:
            trial.check(event)
        except EventRejected as error:
            raise RoleConflict(f"role {role_id}: {error}") from error
        trial.apply(event)
        events.append(event)

    for role_id, definition in sorted(config.roles.items()):
        role = trial.roles.get(role_id)
        if role is None or (role.workdir, role.handoff_seconds) != (
            definition.workdir,
            definition.handoff_seconds,
        ):
            add(
                role_id,
                RoleDeclared(role_id, definition.workdir, definition.handoff_seconds),
            )
        for line_id in definition.seats:
            may_own = config.lines[line_id].may_own
            if trial.seats.get(line_id) != Seat(role_id, may_own):
                add(role_id, SeatBound(line_id, role_id, may_own))
        role = trial.roles[role_id]
        if definition.owner is None or role.changed is not None:
            continue
        if by.operator_id not in trial.operators:
            add(role_id, OperatorRegistered(by.operator_id))
        add(
            role_id,
            OwnershipChanged(
                role_id, definition.owner, None, OwnershipCause.DECLARED, by, None, at
            ),
        )
    return tuple(events)


def _seat_of(state: State, line_id: str) -> tuple[str, Seat, Role]:
    """The role ``line_id`` is a seat of, its seat and the role, or ``RoleConflict``."""
    seat = state.seats.get(line_id)
    if seat is None:
        raise RoleConflict(f"line {line_id} is a seat of no role")
    return seat.role_id, seat, state.roles[seat.role_id]


def _checked(state: State, events: list[Event]) -> tuple[Event, ...]:
    """``events``, each checked against the state the ones before it leave, on a
    copy, or ``RoleConflict`` with the model's reason. Nothing is applied to
    ``state``."""
    trial = state.copy()
    for event in events:
        try:
            trial.check(event)
        except EventRejected as error:
            raise RoleConflict(str(error)) from error
        trial.apply(event)
    return tuple(events)


def _entry(
    role_id: str,
    entry_id: str,
    line_id: str,
    kind: RoleNoteKind,
    body: str,
    stamp: Stamp,
) -> RoleNoted:
    try:
        return RoleNoted(
            role_id,
            entry_id,
            line_id,
            kind,
            body,
            stamp.workdir_commit,
            stamp.daemon_commit,
            stamp.at,
        )
    except InvalidField as error:
        raise RoleConflict(str(error)) from error


def plan_note(
    state: State,
    line_id: str,
    kind: RoleNoteKind,
    body: str,
    entry_id: str,
    stamp: Stamp,
) -> Plan:
    """A journal entry by ``line_id`` in its role. A handoff entry is written only by
    ``plan_relinquish``, with the change it rests on, so that a handoff entry never
    stands without the handoff it records."""
    role_id, _, role = _seat_of(state, line_id)
    if kind is RoleNoteKind.HANDOFF:
        raise RoleConflict(
            "a handoff entry is written by relinquish_ownership, with the handoff"
        )
    entry = _entry(role_id, entry_id, line_id, kind, body, stamp)
    events = _checked(state, [entry])
    return Plan(events, role_id, role.owner, role.owner, entry_id, "")


def returning_to(role: Role, line_id: str) -> str | None:
    """The seat a relinquish by ``line_id`` returns ``role`` to when it names no
    successor, which is the ``back_to`` of the handoff that gave it the role."""
    change = role.changed
    if (
        change is None
        or change.cause is not OwnershipCause.HANDOFF
        or change.owner != line_id
    ):
        return None
    return change.back_to


def plan_relinquish(
    state: State,
    line_id: str,
    note: str,
    to: str | None,
    back_to: str | None,
    entry_id: str,
    stamp: Stamp,
) -> Plan:
    """The owner ``line_id`` gives its role up, with the handoff note ``note``, to the
    seat ``to``, or with no successor named to the seat the handoff that gave it the
    role said it returns to, or else leaving the role vacant. A return to a seat that
    can no longer own the role leaves the role vacant instead, and says so, because the
    owner must always be able to give a role up. ``back_to`` needs a successor."""
    role_id, _, role = _seat_of(state, line_id)
    if role.owner != line_id:
        raise RoleConflict(
            f"line {line_id} does not own role {role_id}, which"
            f" {role.owner or 'no seat'} owns"
        )
    if to == line_id:
        raise RoleConflict("a seat does not hand a role to itself")
    entry = _entry(role_id, entry_id, line_id, RoleNoteKind.HANDOFF, note, stamp)
    me = LineAddress(line_id)
    said = ""
    successor = to
    if successor is None:
        successor = returning_to(role, line_id)
        if successor is not None:
            said = f"The role returns to {successor}, as its handoff to you said."
    if back_to is not None and successor is None:
        raise RoleConflict("back_to needs a successor, named with to")
    if successor is not None:
        try:
            change = OwnershipChanged(
                role_id,
                successor,
                line_id,
                OwnershipCause.HANDOFF,
                me,
                entry_id,
                stamp.at,
                back_to,
            )
        except InvalidField as error:
            raise RoleConflict(str(error)) from error
        try:
            events = _checked(state, [entry, change])
        except RoleConflict as error:
            if to is not None:
                raise
            said = (
                f"The role was to return to {successor}, which cannot take it now,"
                f" so it is vacant instead: {error}"
            )
            successor = None
        else:
            return Plan(events, role_id, successor, line_id, entry_id, said)
    vacant = OwnershipChanged(
        role_id, None, line_id, OwnershipCause.RELINQUISHED, me, entry_id, stamp.at
    )
    events = _checked(state, [entry, vacant])
    return Plan(events, role_id, None, line_id, entry_id, said)


def budget_spent(budget: BudgetStated | None, now: datetime) -> bool:
    """Whether ``budget`` says its provider has nothing left now, which is a
    statement of 0 percent whose reset time, if it gave one, has not passed. A
    statement is a snapshot, so a passed reset ends what it says."""
    if budget is None or budget.remaining_percent != 0:
        return False
    return budget.resets_at is None or now < parse_due(budget.resets_at)


def recovery_ground(
    role: Role,
    *,
    owner_running: bool,
    owner_plan_held: bool,
    owner_budget: BudgetStated | None,
    now: datetime,
) -> str | None:
    """Why a seat may take ``role`` itself, by what the harness itself observes of
    its owner, or ``None``. These are the first three rows of the recovery table in
    ``ROLES.md``. A line's own claim that its owner cannot act is never a ground.
    The facts are the scheduler's: whether the owner has a turn in flight, whether
    it is held at a plan limit its engine reported, and its provider's budget."""
    if role.owner is None:
        return "the role is vacant"
    if owner_running:
        return None
    if owner_plan_held:
        return f"its owner {role.owner} is held at a plan limit its engine reported"
    if budget_spent(owner_budget, now):
        return f"its owner {role.owner}'s provider budget is recorded as 0 percent"
    return None


def plan_take(
    state: State,
    line_id: str,
    reason: str,
    ground: str | None,
    entry_id: str,
    stamp: Stamp,
) -> Plan:
    """``line_id`` takes its role for itself, with ``reason``, on the harness's
    ``ground`` from ``recovery_ground``. The reason and the ground are recorded as a
    note entry, which the change rests on, so a later reader sees why."""
    role_id, seat, role = _seat_of(state, line_id)
    if role.owner == line_id:
        raise RoleConflict(f"line {line_id} owns role {role_id} already")
    if not seat.may_own:
        raise RoleConflict(f"line {line_id} may not own role {role_id}")
    if ground is None:
        raise RoleConflict(
            f"role {role_id} is owned by {role.owner}, which the harness does not show"
            " unable to act. A seat takes a role itself only while it is vacant, while"
            " its owner is held at a plan limit, or while its owner's provider budget"
            " is recorded as 0 percent, and never while its owner has a turn in"
            " flight. Ask an operator, who can always give it."
        )
    body = f"take_ownership, because {ground}. The seat's reason: {reason}"
    entry = _entry(role_id, entry_id, line_id, RoleNoteKind.NOTE, body, stamp)
    change = OwnershipChanged(
        role_id,
        line_id,
        role.owner,
        OwnershipCause.RECOVERY,
        LineAddress(line_id),
        entry_id,
        stamp.at,
    )
    events = _checked(state, [entry, change])
    return Plan(events, role_id, line_id, role.owner, entry_id, "")


def plan_give(
    state: State,
    role_id: str,
    seat: str | None,
    by: OperatorAddress,
    at: str,
    *,
    owner_running: bool,
) -> Plan:
    """The operator ``by`` gives ``role_id`` to ``seat``, or leaves it vacant when
    ``seat`` is ``None``, registered first if the log does not know it. An operator
    may always take a role, but never while its owner has a turn in flight, since a
    turn keeps the access it started with, so the operator who must take it now
    interrupts the owner first. Whether the owner runs is the scheduler's fact."""
    role = state.roles.get(role_id)
    if role is None:
        raise RoleConflict(f"no role {role_id}")
    if seat == role.owner:
        held = "vacant" if seat is None else f"owned by {seat}"
        raise RoleConflict(f"role {role_id} is {held} already")
    if owner_running:
        raise RoleConflict(
            f"role {role_id}'s owner {role.owner} has a turn in flight. Interrupt it"
            " first, then take the role"
        )
    events: list[Event] = []
    if by.operator_id not in state.operators:
        events.append(OperatorRegistered(by.operator_id))
    try:
        change = OwnershipChanged(
            role_id, seat, role.owner, OwnershipCause.OPERATOR, by, None, at
        )
    except InvalidField as error:
        raise RoleConflict(str(error)) from error
    events.append(change)
    return Plan(_checked(state, events), role_id, seat, role.owner, None, "")


def check_handoff_request(state: State, role_id: str, to: str) -> str:
    """The owner an operator asks to hand ``role_id`` to ``to``, or ``RoleConflict``
    when the handoff asked for could not apply, because the role is vacant, or ``to``
    owns it already, is not a seat of it, may not own it or is stopped. The handoff
    itself is the owner's, so this checks the request and changes nothing."""
    role = state.roles.get(role_id)
    if role is None:
        raise RoleConflict(f"no role {role_id}")
    if role.owner is None:
        raise RoleConflict(
            f"role {role_id} is vacant, so no owner can hand it over. Take it for"
            " the seat instead"
        )
    if to == role.owner:
        raise RoleConflict(f"line {to} owns role {role_id} already")
    seat = state.seats.get(to)
    if seat is None or seat.role_id != role_id:
        raise RoleConflict(f"line {to} is not a seat of role {role_id}")
    if not seat.may_own:
        raise RoleConflict(f"line {to} may not own role {role_id}")
    if state.lines[to].state is LineState.STOPPED:
        raise RoleConflict(f"line {to} is stopped")
    return role.owner


type Access = Literal["write", "read"]
"""A seat's access to its role's worktree. The values are those of the engines role's
``WorkdirAccess``, a string enumeration on the unmerged ``feat/read-only-seats``
branch, so the merge can pass one where the other is read."""

CONFINING_ENGINES: Final[frozenset[str]] = frozenset()
"""The engines whose adapter can make a role's worktree read only to a seat.
**Technical debt.** It is empty until ``feat/read-only-seats`` merges, since no engine
here applies a read-only access yet, and then becomes the engines whose capability
table entry gives ``read_only_workdir`` as yes. Empty is default deny, so a seat that
does not own its role does not run beside an owner until its engine can confine it."""


def seat_access(state: State, line_id: str) -> Access | None:
    """``line_id``'s access to its role's worktree, which is write while it owns the
    role and read otherwise, also while the role is vacant, or ``None`` for a line that
    is not a seat. Ownership is read as a turn starts, so a turn keeps the access its
    engine was built with."""
    seat = state.seats.get(line_id)
    if seat is None:
        return None
    return "write" if state.roles[seat.role_id].owner == line_id else "read"


def waits_unconfined(
    state: State, line_id: str, engine: str, confining: frozenset[str]
) -> bool:
    """Whether ``line_id`` may not start a turn because it is a seat of a role that
    another seat owns and its ``engine`` is not in ``confining``, so nothing would
    keep it from writing the owner's worktree. **Default deny.** Its mail waits, as a
    muted line's does, until it owns the role, the role is vacant, or its engine can
    confine it. A seat of a vacant role runs, so that it can take the role."""
    seat = state.seats.get(line_id)
    if seat is None:
        return False
    owner = state.roles[seat.role_id].owner
    return owner is not None and owner != line_id and engine not in confining
