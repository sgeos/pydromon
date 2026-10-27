"""The operator's role and budget commands, ``dromon role`` and ``dromon budget``,
through the client into a real daemon and scheduler over a real event log, and the
operations they send. Roles step 2, ``docs/architecture/ROLES.md``."""

import json
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli, exits
from dromon.configview import Overrides
from dromon.daemon import Daemon
from dromon.driver import ENGINE_OF, Strategy
from dromon.eventlog import EventLog
from dromon.model import (
    BudgetSource,
    LineAddress,
    LineCreated,
    OperatorAddress,
    OperatorRegistered,
    OwnershipCause,
    OwnershipChanged,
    RoleDeclared,
    RoleNoted,
    RoleNoteKind,
    SeatBound,
    State,
    format_due,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ProjectConfig
from dromon.protocol import Operation, Response
from dromon.roles import RoleConflict, plan_give
from dromon.scheduler import HANDOFF_TEMPLATE, LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
PROVIDER = "claude-code"
ANA = OperatorAddress("ana")
BEN = OperatorAddress("ben")
NOW = datetime(2026, 10, 27, 9, 0, 0, tzinfo=UTC)
AT = "2026-10-27T08:00:00Z"


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


class Desk:
    """A daemon over a real event log, with role core on the project root, seats
    alpha, beta and gamma, alpha its owner, beta muted and gamma unable to own, and a
    line delta that is a seat of no role. The client's requests reach it directly."""

    def __init__(self, paths: ProjectPaths) -> None:
        self.paths = paths
        self.log = EventLog(paths.event_log)
        self.scheduler = Scheduler(
            self.log, no_engine, hold_wakes=True, wall_clock=lambda: NOW
        )
        self.daemon = Daemon(
            paths,
            self.log,
            format_due(NOW),
            self.scheduler,
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=False,
        )
        self.scheduler.ensure_operator("ana")
        for line_id in ("alpha", "beta", "gamma", "delta"):
            self.scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
        for event in (
            RoleDeclared("core", "."),
            SeatBound("alpha", "core", True),
            SeatBound("beta", "core", True),
            SeatBound("gamma", "core", False),
            OwnershipChanged(
                "core", "alpha", None, OwnershipCause.DECLARED, ANA, None, AT
            ),
        ):
            self.log.append(event)
        self.scheduler.mute("beta")

    def call(
        self, socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        del socket
        line: dict[str, object] = {
            "id": "r",
            "op": operation.value,
            "v": 1,
            "args": args or {},
        }
        return self.daemon.respond((json.dumps(line) + "\n").encode())

    def ask(self, op: str, args: dict[str, object]) -> Response:
        return self.call(self.paths.socket, Operation(op), args)

    def notes(self, sender: OperatorAddress, line_id: str) -> list[str]:
        return [
            message.body
            for message in self.log.state.messages.values()
            if message.sender == sender and message.recipient == LineAddress(line_id)
        ]


@pytest.fixture
def desk(
    tmp_path: Path, short_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Desk]:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700)
    target = Desk(paths)
    monkeypatch.setattr(cli, "call", target.call)
    yield target
    target.log.close()


type Run = Callable[..., tuple[int, str, str]]


@pytest.fixture
def dromon(desk: Desk, capsys: pytest.CaptureFixture[str]) -> Run:
    """Run ``dromon`` against the desk, returning the exit status, the output and
    the error output."""

    def run(*argv: str) -> tuple[int, str, str]:
        root = ["--project", str(desk.paths.root)]
        runtime = ["--runtime-dir", str(desk.paths.runtime_dir)]
        status = cli.main([*root, *runtime, *argv])
        captured = capsys.readouterr()
        return status, captured.out, captured.err

    return run


# dromon role status and entry.


def test_role_status_shows_the_owner_the_seats_and_the_last_change(
    dromon: Run,
) -> None:
    status, out, _ = dromon("role")
    assert status == exits.OK
    lines = out.splitlines()
    assert lines[0] == "role core in ., owner alpha, handoff within 3600 seconds"
    assert "last change declared by @ana" in lines[1]
    seats = [line for line in lines if line.startswith("  seat ")]
    assert [line.split()[1] for line in seats] == ["alpha", "beta", "gamma"]
    assert "owner" in seats[0] and "muted" in seats[1] and "may not own" in seats[2]
    # No engine confines a seat yet, so the seats that do not own wait.
    waits = "waits, its engine cannot confine it"
    assert [waits in line for line in seats] == [False, True, True]
    named = dromon("role", "status", "core")
    assert named == (exits.OK, out, "")


def test_role_status_of_an_unknown_role_fails(dromon: Run) -> None:
    status, _, err = dromon("role", "status", "nothing")
    assert status == exits.FAILURE and "no role nothing" in err


def test_role_entry_prints_one_entry_with_its_body(desk: Desk, dromon: Run) -> None:
    desk.log.append(
        RoleNoted("core", "e1", "beta", RoleNoteKind.NOTE, "ready", "a" * 40, "", AT)
    )
    status, out, _ = dromon("role", "entry", "e1")
    assert status == exits.OK
    header, body = out.splitlines()
    assert header.startswith("entry e1 of role core, a note by beta")
    assert "daemon unknown" in header and body == "ready"
    missing = dromon("role", "entry", "e2")
    assert missing[0] == exits.FAILURE and "no journal entry e2" in missing[2]


# dromon role take and vacate.


def test_an_operator_gives_a_role_and_the_seat_is_unmuted_and_told(
    desk: Desk, dromon: Run
) -> None:
    status, out, _ = dromon(
        "role", "take", "core", "--seat", "beta", "--operator", "ben"
    )
    assert (status, out) == (exits.OK, "role core passed from alpha to beta\n")
    role = desk.log.state.roles["core"]
    change = role.changed
    assert role.owner == "beta" and change is not None
    assert (change.cause, change.by, change.previous) == (
        OwnershipCause.OPERATOR,
        BEN,
        "alpha",
    )
    assert change.changed_at == format_due(NOW)
    assert "ben" in desk.log.state.operators
    assert not desk.log.state.lines["beta"].muted
    (note,) = desk.notes(BEN, "beta")
    assert "gave you role core" in note and "recovery checklist" in note


@pytest.mark.parametrize(
    ("seat", "message"),
    [
        ("gamma", "may not own"),
        ("alpha", "owned by alpha already"),
        ("delta", "not a seat"),
        ("nobody", "nobody is not a seat of role core"),
    ],
)
def test_an_operator_take_the_model_refuses_records_nothing(
    desk: Desk, dromon: Run, seat: str, message: str
) -> None:
    before = desk.log.last_seq
    status, _, err = dromon("role", "take", "core", "--seat", seat, "--operator", "ben")
    assert status == exits.FAILURE and message in err, err
    assert desk.log.last_seq == before
    assert "ben" not in desk.log.state.operators


def test_a_take_of_an_unknown_role_is_refused(dromon: Run) -> None:
    status, _, err = dromon("role", "take", "nothing", "--seat", "beta")
    assert status == exits.FAILURE and "no role nothing" in err


def test_an_operator_vacates_a_role_and_a_second_vacate_is_refused(
    desk: Desk, dromon: Run
) -> None:
    status, out, _ = dromon("role", "vacate", "core", "--operator", "ana")
    assert (status, out) == (exits.OK, "role core passed from alpha to no seat\n")
    change = desk.log.state.roles["core"].changed
    assert change is not None and change.owner is None
    assert change.cause is OwnershipCause.OPERATOR
    assert desk.notes(ANA, "alpha") == []
    again = dromon("role", "vacate", "core")
    assert again[0] == exits.FAILURE and "vacant already" in again[2]


def test_a_give_is_planned_against_the_log_and_refused_while_the_owner_runs() -> None:
    state = State()
    for event in (
        LineCreated("alpha", HAIKU, "subscription"),
        LineCreated("beta", HAIKU, "subscription"),
        OperatorRegistered("ana"),
        RoleDeclared("core", "."),
        SeatBound("alpha", "core", True),
        SeatBound("beta", "core", True),
        OwnershipChanged("core", "alpha", None, OwnershipCause.DECLARED, ANA, None, AT),
    ):
        state.check(event)
        state.apply(event)
    with pytest.raises(RoleConflict, match="alpha has a turn in flight. Interrupt"):
        plan_give(state, "core", "beta", ANA, AT, owner_running=True)
    plan = plan_give(state, "core", "beta", BEN, AT, owner_running=False)
    assert [type(event) for event in plan.events] == [
        OperatorRegistered,
        OwnershipChanged,
    ]
    assert (plan.owner, plan.previous, plan.entry_id) == ("beta", "alpha", None)
    assert state.roles["core"].owner == "alpha"
    with pytest.raises(RoleConflict, match="no role other"):
        plan_give(state, "other", "beta", ANA, AT, owner_running=False)


@pytest.mark.parametrize(
    "argv",
    [
        ("role", "take", "core"),
        ("role", "take"),
        ("role", "vacate"),
        ("role", "entry"),
        ("role", "handoff", "core"),
    ],
)
def test_a_role_command_missing_what_it_needs_is_a_usage_error(
    desk: Desk, dromon: Run, argv: tuple[str, ...]
) -> None:
    before = desk.log.last_seq
    status, _, err = dromon(*argv)
    assert status == exits.USAGE and "dromon role" in err
    assert desk.log.last_seq == before


# dromon role handoff.


def test_a_handoff_request_is_priority_mail_to_the_owner(
    desk: Desk, dromon: Run
) -> None:
    status, out, _ = dromon(
        "role", "handoff", "core", "--to", "beta", "--operator", "ana"
    )
    assert status == exits.OK
    assert out.startswith("asked alpha to hand role core to beta, in priority mail m-")
    (message_id,) = [
        found
        for found, message in desk.log.state.messages.items()
        if message.recipient == LineAddress("alpha")
    ]
    message = desk.log.state.messages[message_id]
    assert message.priority
    assert "hand role core to beta" in message.body
    assert "relinquish_ownership with to set to beta" in message.body
    assert HANDOFF_TEMPLATE in message.body
    assert desk.log.state.roles["core"].owner == "alpha"


def test_a_handoff_request_to_a_muted_owner_says_it_waits(
    desk: Desk, dromon: Run
) -> None:
    desk.scheduler.mute("alpha")
    status, out, _ = dromon("role", "handoff", "core", "--to", "beta")
    assert status == exits.OK
    assert out.splitlines()[1] == "alpha is muted, so it reads the request once unmuted"


@pytest.mark.parametrize(
    ("to", "message"),
    [
        ("gamma", "may not own"),
        ("alpha", "owns role core already"),
        ("delta", "not a seat of role core"),
    ],
)
def test_a_handoff_request_that_could_not_apply_sends_nothing(
    desk: Desk, dromon: Run, to: str, message: str
) -> None:
    before = desk.log.last_seq
    status, _, err = dromon("role", "handoff", "core", "--to", to, "--operator", "ben")
    assert status == exits.FAILURE and message in err, err
    assert desk.log.last_seq == before


def test_a_handoff_request_for_a_vacant_role_is_refused(
    desk: Desk, dromon: Run
) -> None:
    assert dromon("role", "vacate", "core")[0] == exits.OK
    status, _, err = dromon("role", "handoff", "core", "--to", "beta")
    assert status == exits.FAILURE and "vacant, so no owner can hand it over" in err


# dromon budget.


def test_an_operator_states_a_budget_and_the_list_shows_it_spent(
    desk: Desk, dromon: Run
) -> None:
    status, out, _ = dromon(
        "budget",
        "set",
        PROVIDER,
        "--percent",
        "0",
        "--resets-at",
        "2026-10-28T00:00:00Z",
        "--note",
        "weekly cap",
    )
    assert status == exits.OK
    assert out.startswith(f"{PROVIDER}: 0 percent, resets 2026-10-28T00:00:00Z")
    stated = desk.log.state.budgets[PROVIDER]
    assert (stated.source, stated.note, stated.stated_at) == (
        BudgetSource.OPERATOR,
        "weekly cap",
        format_due(NOW),
    )
    status, out, _ = dromon("budget")
    assert status == exits.OK
    first, providers = out.splitlines()
    assert first.endswith("spent now, weekly cap")
    assert providers == f"providers {', '.join(sorted(set(ENGINE_OF.values())))}"
    status, out, _ = dromon("role")
    (alpha,) = [line for line in out.splitlines() if line.startswith("  seat alpha")]
    assert "budget 0 percent, resets 2026-10-28T00:00:00Z, stated by operator" in alpha


def test_a_budget_with_a_passed_reset_is_not_spent(desk: Desk, dromon: Run) -> None:
    status, _, _ = dromon(
        "budget",
        "set",
        PROVIDER,
        "--percent",
        "0",
        "--resets-at",
        "2026-10-27T08:00:00Z",
    )
    assert status == exits.OK
    status, out, _ = dromon("budget", "list")
    assert status == exits.OK and "spent now" not in out


def test_an_empty_budget_list_says_so(dromon: Run) -> None:
    status, out, _ = dromon("budget")
    assert status == exits.OK and out.splitlines()[0] == "no budget is stated"


@pytest.mark.parametrize(
    ("argv", "status", "message"),
    [
        (("budget", "set"), exits.USAGE, "name the provider"),
        (("budget", "set", PROVIDER), exits.USAGE, "give --percent, --resets-at"),
        (
            ("budget", "set", "acme", "--percent", "5"),
            exits.FAILURE,
            "no provider acme",
        ),
        (
            ("budget", "set", PROVIDER, "--percent", "101"),
            exits.FAILURE,
            "from 0 to 100",
        ),
        (
            ("budget", "set", PROVIDER, "--resets-at", "tomorrow"),
            exits.FAILURE,
            "resets_at is not a valid time",
        ),
    ],
)
def test_a_budget_statement_that_does_not_apply_records_nothing(
    desk: Desk, dromon: Run, argv: tuple[str, ...], status: int, message: str
) -> None:
    before = desk.log.last_seq
    found, _, err = dromon(*argv)
    assert found == status and message in err, err
    assert desk.log.last_seq == before


# The operations themselves.


@pytest.mark.parametrize(
    ("op", "args"),
    [
        ("role_take", {"role_id": "core", "operator": "ana"}),
        ("role_take", {"role_id": "core", "seat": 1, "operator": "ana"}),
        ("role_vacate", {"role_id": "core"}),
        ("role_handoff", {"role_id": "core", "to": "beta"}),
        ("role_entry", {}),
        ("roles", {"role_id": 7}),
        ("roles", {"role": "core"}),
        ("budgets", {"provider": PROVIDER}),
        ("budget_set", {"remaining_percent": 5}),
        ("budget_set", {"provider": PROVIDER, "remaining_percent": "5"}),
        ("budget_set", {"provider": PROVIDER, "remaining_percent": True}),
        ("budget_set", {"provider": PROVIDER, "remaining_percent": 5, "by": "ana"}),
        ("budget_set", {"provider": PROVIDER, "resets_at": 5}),
    ],
)
def test_an_operation_with_wrong_arguments_is_a_bad_request(
    desk: Desk, op: str, args: dict[str, object]
) -> None:
    before = desk.log.last_seq
    response = desk.ask(op, args)
    assert not response.ok and response.error_code == "bad_request", response
    assert desk.log.last_seq == before


def test_roles_lists_every_role_by_identifier(desk: Desk) -> None:
    desk.log.append(RoleDeclared("archive", "."))
    response = desk.ask("roles", {})
    assert response.ok and response.result is not None
    roles = cast(list[dict[str, object]], response.result["roles"])
    assert [role["role_id"] for role in roles] == ["archive", "core"]
