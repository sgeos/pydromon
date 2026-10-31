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
a handoff, a recovery or a relinquishment. The file never unbinds a seat or retires a
role, so a seat or a role dropped from the file stays in the log as it was, and a
start or a reload says so.

**Round 10.** An operator creates, changes and retires roles at run time with the
``dromon role`` commands, planned by ``plan_role_set`` and ``plan_role_retire``, which
record ``seat_unbound``, ``role_retired`` and ``role_held``. A role a command changed is
held by the operator, and the file does not change it again until its table agrees
with it, as ``plan_file_roles`` gives and ``ROLES.md`` states. No line can call these,
since they are offered only on the operator's client socket.

The ownership tools and the operator's ownership commands are planned here too, as
plain functions of the state and of the facts the scheduler observes, which a port
carries unchanged. The model checks that a
change is consistent, and ``recovery_ground`` decides whether a seat had the authority
to take a role itself, which the model leaves to the daemon. A seat's access to the
worktree, and whether a seat whose engine cannot confine it may run, are decided here
as well.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from dromon.model import (
    MAX_ROLE_SEATS,
    BudgetStated,
    Event,
    FailoverPolicy,
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
    RoleHeld,
    RoleHolder,
    RoleNoted,
    RoleRetired,
    RoleNoteKind,
    Seat,
    SeatBound,
    SeatUnbound,
    State,
    parse_due,
    role_terms,
)
from dromon.capabilities import Known, TABLES
from dromon.projectconfig import ProjectConfig, RoleDefinition


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


@dataclass(frozen=True, slots=True)
class FilePlan:
    """What a start or a reload records from the project file's roles, and what it
    tells the operator of the roles it leaves as they are."""

    events: tuple[Event, ...]
    notices: tuple[str, ...]


def plan_roles(
    state: State, config: ProjectConfig, by: OperatorAddress, at: str
) -> tuple[Event, ...]:
    """The events of ``plan_file_roles``, without its notices."""
    return plan_file_roles(state, config, by, at).events


def file_differences(
    state: State, role_id: str, definition: RoleDefinition, config: ProjectConfig
) -> tuple[str, ...]:
    """How ``config``'s table for ``role_id`` differs from the role in ``state``,
    term by term and seat by seat, empty when they agree. The file's ``owner`` is
    left out, since the file names only a first owner. Bounded by the seats of the
    two, at most ``2 * MAX_ROLE_SEATS``."""
    role = state.roles[role_id]
    found: list[str] = []
    terms = (
        ("workdir", definition.workdir, role.workdir),
        ("handoff_seconds", definition.handoff_seconds, role.handoff_seconds),
        ("failover", definition.failover.value, role.failover.value),
        (
            "handoff_below_percent",
            definition.handoff_below_percent,
            role.handoff_below_percent,
        ),
        ("seats", ", ".join(definition.seats), ", ".join(role.seats)),
    )
    for name, written, recorded in terms:
        if written != recorded:
            found.append(f"{name} is {written} in the file and {recorded} in the log")
    bound = {
        line_id: seat.may_own
        for line_id, seat in state.seats.items()
        if seat.role_id == role_id
    }
    wanted = {line_id: config.lines[line_id].may_own for line_id in definition.seats}
    for line_id in sorted(set(bound) | set(wanted)):
        if line_id not in bound:
            found.append(f"line {line_id} is a seat in the file and not in the log")
        elif line_id not in wanted:
            found.append(f"line {line_id} is a seat in the log and not in the file")
        elif bound[line_id] != wanted[line_id]:
            found.append(
                f"line {line_id} has may_own {str(wanted[line_id]).lower()} in the"
                f" file and {str(bound[line_id]).lower()} in the log"
            )
    return tuple(found)


def plan_file_roles(
    state: State, config: ProjectConfig, by: OperatorAddress, at: str
) -> FilePlan:
    """The events that bring ``state`` in line with ``config``'s roles, each checked
    against the state the ones before it leave, or ``RoleConflict`` naming the role,
    with a notice for each role the file does not change, as ``ROLES.md`` gives.

    ``by`` is the operator a declared ownership is recorded as made by, the project's
    operator, registered first if the log does not know it. ``at`` is its time, in
    the model's due-time format. A line the file declares and the log lacks is assumed
    to be created first, as the daemon does, so the plan can be made before it is.

    **Round 10.** A retired role is never declared again, so its table is left out. A
    role an operator holds, because a ``dromon role`` command changed it, is left as
    it is while its table differs, and returns to the file, with ``role_held``, once
    the table agrees with it. A seat the file names that an operator bound to another
    role is left out. A role the file held and no longer declares stays as it was,
    since the file never retires a role."""
    trial = state.copy()
    for line_id, definition in sorted(config.lines.items()):
        if line_id not in trial.lines:
            trial.apply(
                LineCreated(line_id, definition.model, definition.strategy.value)
            )
    events: list[Event] = []
    notices: list[str] = []

    def add(role_id: str, event: Event) -> None:
        try:
            trial.check(event)
        except EventRejected as error:
            raise RoleConflict(f"role {role_id}: {error}") from error
        trial.apply(event)
        events.append(event)

    def register() -> None:
        if by.operator_id not in trial.operators:
            add("of the file", OperatorRegistered(by.operator_id))

    for role_id, definition in sorted(config.roles.items()):
        retired = trial.retired_roles.get(role_id)
        if retired is not None:
            notices.append(
                f"role {role_id} was retired by {retired.by} at {retired.retired_at},"
                " and a role identifier is never declared again, so the project"
                " file's table for it is not applied. Remove the table from the file"
            )
            continue
        role = trial.roles.get(role_id)
        if role is not None and trial.holder_of(role_id) is RoleHolder.OPERATOR:
            held = trial.role_holders[role_id]
            differences = file_differences(trial, role_id, definition, config)
            if differences:
                notices.append(
                    f"role {role_id} was changed by {held.by} at {held.held_at} with"
                    " dromon role, which the project file does not undo, so its table"
                    " is not applied. "
                    + ". ".join(d[0].upper() + d[1:] for d in differences)
                    + ". Change the file to agree, and the file holds the role again,"
                    f" or change the role with dromon role update {role_id}"
                )
                continue
            register()
            add(role_id, RoleHeld(role_id, RoleHolder.FILE, by, at))
            notices.append(
                f"role {role_id} agrees with the project file again, so the file"
                " holds it from now"
            )
            continue
        declared = RoleDeclared(
            role_id,
            definition.workdir,
            definition.handoff_seconds,
            definition.seats,
            definition.failover,
            definition.handoff_below_percent,
        )
        if role is None or role_terms(role) != role_terms(declared):
            add(role_id, declared)
        for line_id in definition.seats:
            may_own = config.lines[line_id].may_own
            seat = trial.seats.get(line_id)
            if (
                seat is not None
                and seat.role_id != role_id
                and trial.holder_of(seat.role_id) is RoleHolder.OPERATOR
            ):
                notices.append(
                    f"line {line_id} is a seat of role {seat.role_id}, which an"
                    " operator holds, so the project file's seat of role"
                    f" {role_id} for it is not bound"
                )
                continue
            if seat != Seat(role_id, may_own):
                add(role_id, SeatBound(line_id, role_id, may_own))
        role = trial.roles[role_id]
        if definition.owner is None or role.changed is not None:
            continue
        register()
        add(
            role_id,
            OwnershipChanged(
                role_id, definition.owner, None, OwnershipCause.DECLARED, by, None, at
            ),
        )
    for role_id in sorted(set(trial.roles) - set(config.roles)):
        if trial.holder_of(role_id) is RoleHolder.FILE:
            notices.append(
                f"role {role_id} is no longer in the project file, which does not"
                " retire a role, so it stays as it was. Retire it with dromon role"
                f" delete {role_id}"
            )
    return FilePlan(tuple(events), tuple(notices))


@dataclass(frozen=True, slots=True)
class RoleWanted:
    """A role's terms as an operator's command asks for them. ``order`` is the
    declared order a failover follows, and ``seats`` each seat to be bound, with its
    ``may_own``, in that order."""

    workdir: str
    handoff_seconds: int
    failover: FailoverPolicy
    handoff_below_percent: int
    order: tuple[str, ...]
    seats: tuple[tuple[str, bool], ...]


@dataclass(frozen=True, slots=True)
class RoleChange:
    """The events a role command appends, in order, and the lines it made seats of
    the role and the ones it unbound, whose engines are rebuilt, since the role's
    tools are offered only to a seat."""

    events: tuple[Event, ...]
    role_id: str
    bound: tuple[str, ...]
    unbound: tuple[str, ...]


def _no_role(state: State, role_id: str) -> RoleConflict:
    """The refusal for a role that is not in ``state``, which says when it was
    retired if it was."""
    retired = state.retired_roles.get(role_id)
    if retired is None:
        return RoleConflict(f"no role {role_id}")
    return RoleConflict(
        f"role {role_id} was retired by {retired.by} at {retired.retired_at}"
    )


def plan_role_set(
    state: State,
    role_id: str,
    wanted: RoleWanted,
    by: OperatorAddress,
    at: str,
    *,
    create: bool,
    outside: Mapping[str, str],
    unbind_refused: Mapping[str, str],
) -> RoleChange:
    """The operator ``by`` creates ``role_id`` with ``wanted``, or changes it to
    ``wanted``, which may add, remove and reorder its seats. The role is held by the
    operator from then on, as ``plan_file_roles`` respects. No line is created and
    no line's configuration changes, so a seat must already run in the role's
    worktree, and ``outside`` gives where each wanted seat that does not runs.
    ``unbind_refused`` gives why a seat may not be unbound now, which the scheduler
    decides. The owner is never unbound, nor barred from owning, so its role is
    vacated or handed over first. Raises ``RoleConflict``, recording nothing."""
    role = state.roles.get(role_id)
    if create:
        if role_id in state.retired_roles:
            raise RoleConflict(
                f"{_no_role(state, role_id)}, and a role identifier is never declared"
                " again. Choose another name"
            )
        if role is not None:
            raise RoleConflict(
                f"role {role_id} exists. Change it with dromon role update {role_id}"
            )
    elif role is None:
        raise _no_role(state, role_id)
    if not 1 <= len(wanted.seats) <= MAX_ROLE_SEATS:
        raise RoleConflict(
            f"a role has 1 to {MAX_ROLE_SEATS} seats. Retire it with dromon role"
            " delete instead of leaving it none"
        )
    for line_id, _ in wanted.seats:
        if line_id not in state.lines:
            raise RoleConflict(
                f"no line {line_id}. A role command creates no line, so create it"
                " first with dromon create"
            )
        if line_id in outside:
            raise RoleConflict(
                f"line {line_id} runs in {outside[line_id]}, not in the role's"
                f" worktree {wanted.workdir}. A role command changes no line, so move"
                f" it first with dromon configure {line_id}"
            )
    current = {
        line_id: seat.may_own
        for line_id, seat in state.seats.items()
        if seat.role_id == role_id
    }
    kept = {line_id for line_id, _ in wanted.seats}
    removed = (
        []
        if role is None
        else [line_id for line_id in seat_order(state, role_id) if line_id not in kept]
    )
    if role is not None and role.owner in removed:
        raise RoleConflict(
            f"line {role.owner} owns role {role_id}. Vacate the role with dromon role"
            f" vacate {role_id}, or hand it over, before its seat is removed"
        )
    for line_id in removed:
        if line_id in unbind_refused:
            raise RoleConflict(unbind_refused[line_id])
    events: list[Event] = [SeatUnbound(line_id, role_id) for line_id in removed]
    try:
        declared = RoleDeclared(
            role_id,
            wanted.workdir,
            wanted.handoff_seconds,
            wanted.order,
            wanted.failover,
            wanted.handoff_below_percent,
        )
    except InvalidField as error:
        raise RoleConflict(str(error)) from error
    if role is None or role_terms(role) != role_terms(declared):
        events.append(declared)
    bound: list[str] = []
    for line_id, may_own in wanted.seats:
        if current.get(line_id) != may_own:
            events.append(SeatBound(line_id, role_id, may_own))
            if line_id not in current:
                bound.append(line_id)
    if not events:
        raise RoleConflict(f"role {role_id} is so already")
    if by.operator_id not in state.operators:
        events.insert(0, OperatorRegistered(by.operator_id))
    if state.holder_of(role_id) is not RoleHolder.OPERATOR:
        events.append(RoleHeld(role_id, RoleHolder.OPERATOR, by, at))
    return RoleChange(_checked(state, events), role_id, tuple(bound), tuple(removed))


def plan_role_retire(
    state: State, role_id: str, by: OperatorAddress, at: str
) -> RoleChange:
    """The operator ``by`` retires ``role_id``, which unbinds each of its seats first,
    as the model requires. An owned role is refused, so it is vacated or handed over
    first. The seats stay lines, the journal stays in the log, and the identifier is
    never declared again. Raises ``RoleConflict``, recording nothing."""
    role = state.roles.get(role_id)
    if role is None:
        raise _no_role(state, role_id)
    if role.owner is not None:
        raise RoleConflict(
            f"line {role.owner} owns role {role_id}. Vacate the role with dromon role"
            f" vacate {role_id}, or hand it over, before it is deleted"
        )
    seats = seat_order(state, role_id)
    events: list[Event] = [SeatUnbound(line_id, role_id) for line_id in seats]
    if by.operator_id not in state.operators:
        events.insert(0, OperatorRegistered(by.operator_id))
    try:
        events.append(RoleRetired(role_id, by, at))
    except InvalidField as error:
        raise RoleConflict(str(error)) from error
    return RoleChange(_checked(state, events), role_id, (), seats)


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
``WorkdirAccess`` string enumeration, so the merge can pass one where the other is
read."""

CONFINING_ENGINES: Final[frozenset[str]] = frozenset(
    engine for engine, table in TABLES.items() if table.read_only_workdir is Known.YES
)
"""The engines whose adapter can make a role's worktree read only to a seat.
Empty is default deny, so a seat that does not own its role does not run beside an
owner until its engine can confine it. The set is derived from the capability table,
so Grok, whose entry is unknown, stays out until an operator probe confirms it."""


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
    hibernated line's does, until it owns the role, the role is vacant, or its engine
    can confine it. A seat of a vacant role runs, so that it can take the role."""
    seat = state.seats.get(line_id)
    if seat is None:
        return False
    owner = state.roles[seat.role_id].owner
    return owner is not None and owner != line_id and engine not in confining


def seat_order(state: State, role_id: str) -> tuple[str, ...]:
    """``role_id``'s seats in the order a failover follows, which is the declared
    order with any name that is not a seat of the role left out, and then any other
    seat in the order it was bound, so a seat bound by a client is not lost. Bounded
    by the seats in the log."""
    bound = [line for line, seat in state.seats.items() if seat.role_id == role_id]
    declared = [line for line in state.roles[role_id].seats if line in bound]
    return (*declared, *(line for line in bound if line not in declared))


def next_seat(
    order: tuple[str, ...], after: str | None, usable: frozenset[str]
) -> str | None:
    """The first seat of ``order`` in ``usable`` after ``after``, wrapping round, so
    the seats after the owner come before the ones ahead of it, or ``None`` when no
    seat is usable. ``after`` absent from the order starts at the front. The owner
    itself is never chosen."""
    start = order.index(after) + 1 if after in order else 0
    for step in range(len(order)):
        candidate = order[(start + step) % len(order)]
        if candidate != after and candidate in usable:
            return candidate
    return None


def plan_failover(
    state: State,
    role_id: str,
    to: str,
    ground: str,
    entry_id: str,
    stamp: Stamp,
) -> Plan:
    """The harness passes ``role_id`` from its owner to ``to``, on ``ground``, which
    is what it observed of the owner. The entry the change rests on says the harness
    wrote it, since the model takes entries only from seats and this one is filed
    under the seat made owner. ``RoleConflict`` when the model refuses the change."""
    role = state.roles.get(role_id)
    if role is None:
        raise RoleConflict(f"no role {role_id}")
    if role.owner is None:
        raise RoleConflict(f"role {role_id} is vacant, so it has no owner to fail over")
    body = (
        f"Failover, recorded by the harness and not written by {to}. Role {role_id}"
        f" passed from {role.owner} to {to}, the next usable seat in the declared"
        f" order, because {ground}."
    )
    entry = _entry(role_id, entry_id, to, RoleNoteKind.NOTE, body, stamp)
    try:
        change = OwnershipChanged(
            role_id,
            to,
            role.owner,
            OwnershipCause.FAILOVER,
            LineAddress(to),
            entry_id,
            stamp.at,
        )
    except InvalidField as error:
        raise RoleConflict(str(error)) from error
    events = _checked(state, [entry, change])
    return Plan(events, role_id, to, role.owner, entry_id, "")


def below_handoff_threshold(role: Role, budget: BudgetStated | None) -> bool:
    """Whether ``budget`` asks ``role``'s owner to prepare a handoff, which is a
    stated percentage above 0 and below the role's threshold. A threshold of 0 never
    asks, and 0 percent is a failover's ground, not a planned handoff's."""
    if budget is None or budget.remaining_percent is None:
        return False
    return 0 < budget.remaining_percent < role.handoff_below_percent
