"""An operator's create, update and delete of roles while the daemon runs, round 10.

``dromon role create``, ``update`` and ``delete`` go through the client into a real
daemon and scheduler over a real event log. The new events, ``seat_unbound``,
``role_retired`` and ``role_held``, are checked in the model and replayed after a
restart, and a reload of the project file against a role an operator changed reports
what it leaves, as ``docs/architecture/ROLES.md`` states."""

import asyncio
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli, exits
from dromon.agents import DAEMON_TOOLS
from dromon.configview import Overrides
from dromon.daemon import Daemon, ProjectMismatch, apply_roles, check_roles
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.lineconfig import LineConfig, WorkdirAccess, encode_config
from dromon.model import (
    EventRejected,
    LineConfigured,
    LineCreated,
    LineState,
    LineStateChanged,
    OperatorAddress,
    OperatorRegistered,
    OwnershipCause,
    OwnershipChanged,
    RoleDeclared,
    RoleHeld,
    RoleHolder,
    RoleNoted,
    RoleNoteKind,
    RoleRetired,
    SeatBound,
    SeatUnbound,
    State,
    format_due,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ProjectConfig, config_path, parse
from dromon.protocol import Operation, Response
from dromon.roles import plan_file_roles
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
BEN = OperatorAddress("ben")
NOW = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
AT = format_due(NOW)
DOCS = "tmp/worktrees/docs"
TREE = "tmp/worktrees/core"
"""Role core's worktree in the desk. **Round 10 re-review, N4.** It was the project
root, which now holds every other role's worktree, so a second role there is refused."""
WRITER = LineConfig(builtin_tools=("Read", "Edit"))


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


def start_scheduler(log: EventLog, root: Path) -> Scheduler:
    scheduler = Scheduler(
        log,
        no_engine,
        hold_wakes=True,
        wall_clock=lambda: NOW,
        default_operator="ana",
        root=root,
        protected=(root / ".dromon",),
    )
    scheduler.set_leases(["full-gate"], 600)
    return scheduler


class Desk:
    """A daemon over a real event log, with role core on ``TREE``, seats alpha, beta
    and gamma, alpha its owner, beta hibernated and gamma unable to own, a line delta
    there that is a seat of no role, and lines echo and fox in the worktree of a role
    yet to be made. The client's requests reach it directly."""

    def __init__(self, paths: ProjectPaths) -> None:
        self.paths = paths
        (paths.root / DOCS).mkdir(parents=True)
        (paths.root / TREE).mkdir(parents=True)
        self.log = EventLog(paths.event_log)
        self.scheduler = start_scheduler(self.log, paths.root)
        self.daemon = Daemon(
            paths,
            self.log,
            AT,
            self.scheduler,
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=False,
        )
        self.scheduler.ensure_operator("ana")
        for line_id in ("alpha", "beta", "delta"):
            self.scheduler.create_line(
                line_id, HAIKU, Strategy.SUBSCRIPTION, LineConfig(workdir=TREE)
            )
        # gamma may write its working directory, so it is refused leave of an owned
        # role while it runs in the owner's worktree.
        self.scheduler.create_line(
            "gamma", HAIKU, Strategy.SUBSCRIPTION, replace(WRITER, workdir=TREE)
        )
        for line_id in ("echo", "fox"):
            self.scheduler.create_line(
                line_id, HAIKU, Strategy.SUBSCRIPTION, LineConfig(workdir=DOCS)
            )
        for event in (
            RoleDeclared("core", TREE, seats=("alpha", "beta", "gamma")),
            SeatBound("alpha", "core", True),
            SeatBound("beta", "core", True),
            SeatBound("gamma", "core", False),
            OwnershipChanged(
                "core", "alpha", None, OwnershipCause.DECLARED, ANA, None, AT
            ),
        ):
            self.log.append(event)
        self.scheduler.hibernate("beta")

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

    def seats(self, role_id: str) -> dict[str, bool]:
        return {
            line_id: seat.may_own
            for line_id, seat in self.log.state.seats.items()
            if seat.role_id == role_id
        }


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
    def run(*argv: str) -> tuple[int, str, str]:
        """Each command runs as operator ana unless it names another."""
        if argv[:1] == ("role",) and "--operator" not in argv:
            argv = (*argv, "--operator", "ana")
        root = ["--project", str(desk.paths.root)]
        runtime = ["--runtime-dir", str(desk.paths.runtime_dir)]
        status = cli.main([*root, *runtime, *argv])
        captured = capsys.readouterr()
        return status, captured.out, captured.err

    return run


def create_docs(dromon: Run) -> tuple[int, str, str]:
    return dromon(
        "role",
        "create",
        "docs",
        "--workdir",
        DOCS,
        "--seats",
        "echo,fox",
        "--no-own",
        "fox",
        "--handoff-seconds",
        "600",
        "--failover",
        "ask",
        "--handoff-below-percent",
        "20",
        "--operator",
        "ben",
    )


# Create.


def test_create_declares_the_role_binds_its_seats_and_holds_it(
    desk: Desk, dromon: Run
) -> None:
    status, out, err = create_docs(dromon)
    assert status == exits.OK, err
    role = desk.log.state.roles["docs"]
    assert (role.workdir, role.handoff_seconds, role.seats) == (
        DOCS,
        600,
        ("echo", "fox"),
    )
    assert (role.failover.value, role.handoff_below_percent, role.owner) == (
        "ask",
        20,
        None,
    )
    assert desk.seats("docs") == {"echo": True, "fox": False}
    assert desk.log.state.holder_of("docs") is RoleHolder.OPERATOR
    held = desk.log.state.role_holders["docs"]
    assert (held.by, held.held_at) == (BEN, AT)
    assert "held by the operator, since @ben" in out
    assert "made seats: echo, fox" in out
    # Every term shows in status.
    status, out, _ = dromon("role", "status", "docs")
    assert status == exits.OK
    assert "role docs in tmp/worktrees/docs, owner none, vacant" in out
    assert "handoff within 600 seconds" in out
    assert "failover ask" in out and "below 20 percent" in out
    assert "failover order echo, fox" in out
    assert "may not own" in [line for line in out.splitlines() if "seat fox" in line][0]


def test_a_created_seat_is_offered_the_role_tools_and_its_engine_is_rebuilt(
    desk: Desk, dromon: Run
) -> None:
    before = {tool.name for tool in desk.scheduler.own_tools("echo")}
    assert "role_status" not in before
    assert create_docs(dromon)[0] == exits.OK
    after = {tool.name for tool in desk.scheduler.own_tools("echo")}
    assert {"role_status", "take_ownership"} <= after
    notices = [entry.text for entry in desk.scheduler.activity.after("echo", 0)]
    assert any("made a seat of role docs" in text for text in notices)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("core", "--workdir", TREE, "--seats", "delta"), "role core exists"),
        (("docs", "--workdir", DOCS, "--seats", "echo,nobody"), "no line nobody"),
        (("docs", "--workdir", DOCS, "--seats", "echo,delta"), "line delta runs in"),
        (("docs", "--workdir", ".", "--seats", "delta"), "roles core and docs would"),
        (("docs", "--workdir", DOCS, "--seats", ""), "a role has 1 to 16 seats"),
        (("docs", "--workdir", "../out", "--seats", "echo"), "workdir"),
        (("docs", "--workdir", DOCS, "--seats", "echo,echo"), "names a line twice"),
        (("docs", "--workdir", DOCS, "--seats", "echo", "--no-own", "fox"), "fox"),
    ],
)
def test_create_refuses_what_it_cannot_apply(
    desk: Desk, dromon: Run, argv: tuple[str, ...], message: str
) -> None:
    before = desk.log.last_seq
    status, _, err = dromon("role", "create", *argv)
    assert status == exits.FAILURE
    assert message in err
    assert desk.log.last_seq == before
    assert "nobody" not in desk.log.state.lines


def test_create_needs_a_worktree_and_seats_and_takes_no_may_own(dromon: Run) -> None:
    assert dromon("role", "create", "docs", "--seats", "echo")[0] == exits.USAGE
    assert dromon("role", "create", "docs", "--workdir", DOCS)[0] == exits.USAGE
    status, _, err = dromon(
        "role",
        "create",
        "docs",
        "--workdir",
        DOCS,
        "--seats",
        "echo",
        "--may-own",
        "echo",
    )
    assert status == exits.USAGE and "--may-own is for update" in err


def test_the_operations_refuse_malformed_arguments(desk: Desk) -> None:
    base: dict[str, object] = {"role_id": "docs", "operator": "ana"}
    cases: list[dict[str, object]] = [
        {**base, "workdir": DOCS},
        {**base, "workdir": DOCS, "seats": "echo"},
        {**base, "workdir": DOCS, "seats": ["echo"], "failover": "sometimes"},
        {**base, "workdir": DOCS, "seats": ["echo"], "handoff_seconds": "600"},
        {**base, "workdir": DOCS, "seats": ["echo"], "colour": "red"},
        {**base, "workdir": DOCS, "seats": ["echo"], "no_own": ["echo"], "may_own": []},
    ]
    for args in cases:
        response = desk.ask("role_create", args)
        assert not response.ok, args
    both = {**base, "role_id": "core", "no_own": ["beta"], "may_own": ["beta"]}
    response = desk.ask("role_update", both)
    assert not response.ok and "both name beta" in (response.error_message or "")
    assert "docs" not in desk.log.state.roles


# Update.


def test_update_changes_terms_and_reorders_adds_and_removes_seats(
    desk: Desk, dromon: Run
) -> None:
    status, _, err = dromon(
        "role", "update", "core", "--handoff-seconds", "900", "--operator", "ben"
    )
    assert status == exits.OK, err
    assert desk.log.state.roles["core"].handoff_seconds == 900
    assert desk.log.state.holder_of("core") is RoleHolder.OPERATOR
    # beta cannot write, so it may leave an owned role. delta joins, ahead of alpha.
    status, out, err = dromon("role", "update", "core", "--seats", "delta,alpha,gamma")
    assert status == exits.OK, err
    assert desk.seats("core") == {"alpha": True, "gamma": False, "delta": True}
    assert desk.log.state.roles["core"].seats == ("delta", "alpha", "gamma")
    assert "made seats: delta" in out and "unbound, still lines: beta" in out
    assert "beta" in desk.log.state.lines
    assert "role_status" not in {t.name for t in desk.scheduler.own_tools("beta")}
    # A seat may be barred from owning, and allowed again.
    assert dromon("role", "update", "core", "--no-own", "delta")[0] == exits.OK
    assert desk.seats("core")["delta"] is False
    assert dromon("role", "update", "core", "--may-own", "delta")[0] == exits.OK
    assert desk.seats("core")["delta"] is True


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("--seats", "beta,gamma"), "Vacate the role with dromon role vacate core"),
        (("--no-own", "alpha"), "must give the role up"),
        (("--seats", "alpha,beta"), "Unbound, it would be such a line"),
        (("--workdir", DOCS, "--seats", "echo,fox"), "line alpha owns role core"),
        (("--workdir", DOCS), f"runs in {TREE}, not in the role's worktree"),
        (("--handoff-seconds", "3600"), "role core is so already"),
        (("--seats", "alpha,beta,gamma"), "role core is so already"),
        (("--no-own", "delta"), "name only seats of the role, not delta"),
    ],
)
def test_update_refuses_what_it_cannot_apply(
    desk: Desk, dromon: Run, argv: tuple[str, ...], message: str
) -> None:
    before = desk.log.last_seq
    status, _, err = dromon("role", "update", "core", *argv)
    assert status == exits.FAILURE
    assert message in err
    assert desk.log.last_seq == before
    assert desk.log.state.holder_of("core") is RoleHolder.FILE


def test_update_moves_a_vacant_role_whose_seats_run_in_the_new_worktree(
    desk: Desk, dromon: Run
) -> None:
    assert create_docs(dromon)[0] == exits.OK
    (desk.paths.root / "tmp/worktrees/docs2").mkdir()
    for line_id in ("echo", "fox"):
        desk.scheduler.configure_line(
            line_id, LineConfig(workdir="tmp/worktrees/docs2")
        )
    status, _, err = dromon(
        "role", "update", "docs", "--workdir", "tmp/worktrees/docs2"
    )
    assert status == exits.OK, err
    assert desk.log.state.roles["docs"].workdir == "tmp/worktrees/docs2"


def test_update_of_an_unknown_role_fails(dromon: Run) -> None:
    status, _, err = dromon("role", "update", "nothing", "--handoff-seconds", "900")
    assert status == exits.FAILURE and "no role nothing" in err


# Delete.


def test_delete_of_an_owned_role_is_refused_until_it_is_vacated(
    desk: Desk, dromon: Run
) -> None:
    before = desk.log.last_seq
    status, _, err = dromon("role", "delete", "core")
    assert status == exits.FAILURE
    assert "line alpha owns role core. Vacate the role" in err
    assert desk.log.last_seq == before
    assert dromon("role", "vacate", "core")[0] == exits.OK
    status, out, err = dromon("role", "delete", "core", "--operator", "ben")
    assert status == exits.OK, err
    assert "role core retired at" in out
    assert "seats alpha, beta, gamma unbound and still lines" in out
    state = desk.log.state
    assert "core" not in state.roles and not desk.seats("core")
    assert {"alpha", "beta", "gamma"} <= set(state.lines)
    assert state.lines["beta"].hibernated
    retired = state.retired_roles["core"]
    assert (retired.by, retired.retired_at) == (BEN, AT)


def test_a_deleted_role_keeps_its_journal_and_its_seats_keep_their_leases(
    desk: Desk, dromon: Run
) -> None:
    desk.log.append(
        RoleNoted("core", "e1", "gamma", RoleNoteKind.NOTE, "ready", "", "", AT)
    )
    expires = desk.scheduler.acquire_lease("gamma", "full-gate")
    assert dromon("role", "vacate", "core")[0] == exits.OK
    assert dromon("role", "delete", "core")[0] == exits.OK
    # The lease is the line's, for a resource of the project, so it outlives the role.
    assert desk.scheduler.leases_of("gamma") == [
        {"name": "full-gate", "expires": expires}
    ]
    status, out, _ = dromon("role", "entry", "e1")
    assert status == exits.OK and "entry e1 of role core" in out and "ready" in out
    status, out, _ = dromon("role")
    assert status == exits.OK
    assert "no roles are declared" in out
    assert "retired role core, by @ana at" in out
    status, _, err = dromon("role", "status", "core")
    assert status == exits.FAILURE and "role core was retired by @ana" in err


def test_a_retired_identifier_is_never_declared_again(desk: Desk, dromon: Run) -> None:
    assert dromon("role", "vacate", "core")[0] == exits.OK
    assert dromon("role", "delete", "core")[0] == exits.OK
    status, _, err = dromon(
        "role", "create", "core", "--workdir", ".", "--seats", "delta"
    )
    assert status == exits.FAILURE
    assert "never declared again. Choose another name" in err
    status, _, err = dromon("role", "update", "core", "--handoff-seconds", "900")
    assert status == exits.FAILURE and "retired by @ana" in err
    status, _, err = dromon("role", "delete", "core")
    assert status == exits.FAILURE and "role core was retired" in err


# A restart replays the new events.


def test_a_restart_replays_every_change(desk: Desk, dromon: Run) -> None:
    assert create_docs(dromon)[0] == exits.OK
    assert dromon("role", "update", "core", "--seats", "alpha,gamma,delta")[0] == 0
    assert dromon("role", "vacate", "docs")[0] == exits.FAILURE  # vacant already
    state = desk.log.state
    expected = (
        dict(state.roles),
        dict(state.seats),
        dict(state.role_holders),
        dict(state.retired_roles),
    )
    desk.log.close()
    with EventLog(desk.paths.event_log) as replayed:
        found = replayed.state
        assert (
            found.roles,
            found.seats,
            found.role_holders,
            found.retired_roles,
        ) == expected
    desk.log = EventLog(desk.paths.event_log)


def test_a_restart_replays_a_retirement(desk: Desk, dromon: Run) -> None:
    assert dromon("role", "vacate", "core")[0] == exits.OK
    assert dromon("role", "delete", "core")[0] == exits.OK
    desk.log.close()
    with EventLog(desk.paths.event_log) as replayed:
        state = replayed.state
        assert "core" not in state.roles
        assert state.retired_roles["core"].by == ANA
        assert not [s for s in state.seats.values() if s.role_id == "core"]
        assert "e1" not in state.role_entries
        scheduler = start_scheduler(replayed, desk.paths.root)
        assert scheduler.retired_roles_view() == [
            {"role_id": "core", "by": "@ana", "retired_at": AT}
        ]
    desk.log = EventLog(desk.paths.event_log)


# The model's own refusals.


def model_state() -> State:
    state = State()
    for event in (
        OperatorRegistered("ana"),
        LineCreated("alpha", HAIKU, "subscription"),
        LineCreated("beta", HAIKU, "subscription"),
        RoleDeclared("core", "."),
        SeatBound("alpha", "core", True),
        SeatBound("beta", "core", True),
        OwnershipChanged("core", "alpha", None, OwnershipCause.DECLARED, ANA, None, AT),
    ):
        state.check(event)
        state.apply(event)
    return state


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (SeatUnbound("alpha", "core"), "vacated or handed over before the seat"),
        (SeatUnbound("gamma", "core"), "not a seat of role core"),
        (SeatUnbound("alpha", "docs"), "no role docs"),
        (RoleRetired("core", ANA, AT), "vacated or handed over before it is retired"),
        (RoleRetired("core", BEN, AT), "ben"),
        (RoleHeld("core", RoleHolder.FILE, ANA, AT), "held by the file already"),
        (RoleHeld("docs", RoleHolder.OPERATOR, ANA, AT), "no role docs"),
    ],
)
def test_the_model_refuses(event: Any, message: str) -> None:  # noqa: ANN401
    with pytest.raises(EventRejected, match=message):
        model_state().check(event)


def test_a_role_with_seats_is_not_retired_and_a_retired_one_not_declared() -> None:
    state = model_state()
    vacate = OwnershipChanged(
        "core", None, "alpha", OwnershipCause.OPERATOR, ANA, None, AT
    )
    state.apply(vacate)
    with pytest.raises(EventRejected, match="still has seats alpha, beta"):
        state.check(RoleRetired("core", ANA, AT))
    for event in (
        SeatUnbound("alpha", "core"),
        SeatUnbound("beta", "core"),
        RoleRetired("core", ANA, AT),
    ):
        state.check(event)
        state.apply(event)
    with pytest.raises(EventRejected, match="never declared again"):
        state.check(RoleDeclared("core", "."))
    with pytest.raises(EventRejected, match="no role core"):
        state.check(SeatBound("alpha", "core", True))
    assert state.copy().retired_roles == state.retired_roles


def test_an_unbound_seat_after_an_ownership_change_leaves_no_notice_record() -> None:
    state = model_state()
    state.apply(SeatUnbound("beta", "core"))
    assert "beta" not in state.role_change_turn_starts.get("core", {})


# The project file against a role an operator changed.

LINES = """
[lines.core]
model = "claude-haiku-5-5"
[lines.core-luna]
model = "claude-haiku-5-5"
[lines.other]
model = "claude-haiku-5-5"
workdir = "."
"""
WORKTREE = "tmp/worktrees/core"
CORE = f"""
[roles.core]
workdir = "{WORKTREE}"
seats = ["core", "core-luna"]
"""


def project(roles: str, lines: str = LINES) -> str:
    return f"version = 1\n{lines}\n{roles}\n"


class Reloads:
    """A daemon whose project file the test writes and reloads."""

    def __init__(self, paths: ProjectPaths, log: EventLog) -> None:
        self.paths = paths
        self.log = log
        self.file = config_path(paths.state_dir)
        self.daemon = Daemon(
            paths,
            log,
            AT,
            start_scheduler(log, paths.root),
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=True,
        )

    def ask(self, op: str, args: dict[str, object] | None = None) -> Response:
        line: dict[str, object] = {"id": "r", "op": op, "v": 1, "args": args or {}}
        return self.daemon.respond((json.dumps(line) + "\n").encode())

    def reload(self, roles: str) -> list[str]:
        self.file.write_text(project(roles))
        response = self.ask("reload_project")
        assert response.ok, response.error_message
        assert response.result is not None
        notices = cast(list[object], response.result["role_notices"])
        return [str(notice) for notice in notices]


@pytest.fixture
def reloads(tmp_path: Path, short_runtime: Path) -> Iterator[Reloads]:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700)
    (tmp_path / WORKTREE).mkdir(parents=True)
    with EventLog(paths.event_log) as log:
        yield Reloads(paths, log)


def test_a_reload_leaves_a_role_an_operator_changed_and_says_so(
    reloads: Reloads,
) -> None:
    assert reloads.reload(CORE) == []
    assert reloads.log.state.roles["core"].handoff_seconds == 3600
    response = reloads.ask(
        "role_update",
        {"role_id": "core", "operator": "ben", "handoff_seconds": 900},
    )
    assert response.ok, response.error_message
    before = reloads.log.last_seq
    notices = reloads.reload(CORE)
    assert reloads.log.last_seq == before
    assert reloads.log.state.roles["core"].handoff_seconds == 900
    assert len(notices) == 1
    assert "role core was changed by @ben" in notices[0]
    assert "Handoff_seconds is 3600 in the file and 900 in the log" in notices[0]
    # A file changed to agree holds the role again, and a later change applies.
    agreeing = CORE + "handoff_seconds = 900\n"
    notices = reloads.reload(agreeing)
    assert notices == [
        "role core agrees with the project file again, so the file holds it from now"
    ]
    assert reloads.log.state.holder_of("core") is RoleHolder.FILE
    assert reloads.reload(CORE + "handoff_seconds = 1200\n") == []
    assert reloads.log.state.roles["core"].handoff_seconds == 1200


def test_a_reload_names_each_seat_that_differs(reloads: Reloads) -> None:
    assert reloads.reload(CORE) == []
    response = reloads.ask(
        "role_update",
        {"role_id": "core", "operator": "ana", "seats": ["core"], "no_own": []},
    )
    assert response.ok, response.error_message
    notices = reloads.reload(CORE)
    assert "Seats is core, core-luna in the file and core in the log" in notices[0]
    assert "Line core-luna is a seat in the file and not in the log" in notices[0]
    assert "core-luna" not in reloads.log.state.seats


def test_a_reload_does_not_declare_a_retired_role_and_says_so(
    reloads: Reloads,
) -> None:
    assert reloads.reload(CORE) == []
    response = reloads.ask("role_delete", {"role_id": "core", "operator": "ana"})
    assert response.ok, response.error_message
    before = reloads.log.last_seq
    notices = reloads.reload(CORE)
    assert reloads.log.last_seq == before
    assert "core" not in reloads.log.state.roles
    assert notices == [
        f"role core was retired by @ana at {AT}, and a role identifier is never"
        " declared again, so the project file's table for it is not applied."
        " Remove the table from the file"
    ]


def test_a_reload_of_a_file_that_dropped_a_role_keeps_it_and_says_so(
    reloads: Reloads,
) -> None:
    assert reloads.reload(CORE) == []
    notices = reloads.reload("")
    assert "core" in reloads.log.state.roles
    assert notices == [
        "role core is no longer in the project file, which does not retire a role,"
        " so it stays as it was. Retire it with dromon role delete core"
    ]


def test_a_role_only_in_the_log_is_left_without_a_notice(reloads: Reloads) -> None:
    assert reloads.reload("") == []
    response = reloads.ask(
        "role_create",
        {"role_id": "desk", "operator": "ana", "workdir": ".", "seats": ["other"]},
    )
    assert response.ok, response.error_message
    assert reloads.reload("") == []
    assert reloads.log.state.holder_of("desk") is RoleHolder.OPERATOR


def test_a_file_seat_an_operator_bound_to_another_role_is_left_out() -> None:
    config = parse(project(CORE.replace('"core-luna"]', '"core-luna", "other"]')))
    state = State()
    for event in (
        OperatorRegistered("ana"),
        LineCreated("other", HAIKU, "subscription"),
        RoleDeclared("desk", "."),
        SeatBound("other", "desk", True),
        RoleHeld("desk", RoleHolder.OPERATOR, ANA, AT),
    ):
        state.apply(event)
    plan = plan_file_roles(state, config, ANA, AT)
    bound = [e for e in plan.events if isinstance(e, SeatBound)]
    assert [e.line_id for e in bound] == ["core", "core-luna"]
    assert plan.notices == (
        "line other is a seat of role desk, which an operator holds, so the project"
        " file's seat of role core for it is not bound",
    )


def test_a_start_reports_the_same_notices_as_a_reload(reloads: Reloads) -> None:
    """A start applies the file as a reload does, through ``apply_roles``, which
    returns the notices the daemon prints."""
    assert reloads.reload(CORE) == []
    response = reloads.ask(
        "role_update", {"role_id": "core", "operator": "ana", "handoff_seconds": 900}
    )
    assert response.ok, response.error_message
    scheduler = start_scheduler(reloads.log, reloads.paths.root)
    notices = apply_roles(scheduler, reloads.log, parse(project(CORE)))
    assert len(notices) == 1 and "role core was changed by @ana" in notices[0]
    assert reloads.log.state.roles["core"].handoff_seconds == 900


def test_the_client_prints_each_reload_notice(
    reloads: Reloads,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert reloads.reload(CORE) == []
    reloads.file.write_text(project(""))

    def call(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        del socket
        return reloads.ask(operation.value, args)

    monkeypatch.setattr(cli, "call", call)
    root = ["--project", str(reloads.paths.root)]
    runtime = ["--runtime-dir", str(reloads.paths.runtime_dir)]
    assert cli.main([*root, *runtime, "reload"]) == exits.OK
    out = capsys.readouterr().out
    assert "notice: role core is no longer in the project file" in out


# No line and no external agent can change a role.


def test_no_line_tool_and_no_agent_tool_changes_a_role(desk: Desk) -> None:
    names = {tool.name for tool in desk.scheduler.own_tools("alpha")}
    names |= {spec.name for spec in DAEMON_TOOLS}
    for word in ("create", "update", "delete", "retire", "declare", "bind"):
        assert not [name for name in names if "role" in name and word in name], word
    assert all("role" not in spec.name for spec in DAEMON_TOOLS)


# Round 10 review. The one-writer rule is the invariant, and every path that can break
# it refuses. Each test below failed on 99184f7, which the review read.


def in_flight(desk: Desk, line_id: str) -> None:
    """Mark a turn of ``line_id`` as in flight, as a running turn's task does."""
    running = desk.scheduler._running  # pyright: ignore[reportPrivateUsage]
    running[line_id] = cast("asyncio.Task[None]", object())


def test_a_hibernated_seat_with_a_turn_in_flight_is_refused_leave(desk: Desk) -> None:
    """F1, the review's first probe. Hibernating lets the turn finish, so it no longer
    exempts the seat."""
    desk.scheduler.hibernate("gamma")
    in_flight(desk, "gamma")
    refusals = desk.scheduler._unbind_refusals("core")  # pyright: ignore[reportPrivateUsage]
    assert "line gamma has a turn in flight" in refusals["gamma"]


def test_the_command_refuses_a_hibernated_seat_with_a_turn_in_flight(
    desk: Desk, dromon: Run
) -> None:
    """F1, the review's second probe, through ``dromon role update``."""
    desk.scheduler.hibernate("gamma")
    in_flight(desk, "gamma")
    before = desk.log.last_seq
    status, _, err = dromon("role", "update", "core", "--seats", "alpha,beta")
    assert status == exits.FAILURE
    assert "line gamma has a turn in flight" in err
    assert "dromon interrupt gamma" in err
    assert desk.log.last_seq == before and "gamma" in desk.seats("core")


def test_a_running_seat_is_refused_for_its_turn_first(desk: Desk, dromon: Run) -> None:
    """F1, the review's control, which now names the turn rather than hibernation."""
    in_flight(desk, "gamma")
    status, _, err = dromon("role", "update", "core", "--seats", "alpha,beta")
    assert status == exits.FAILURE and "line gamma has a turn in flight" in err


def test_a_seat_of_a_vacant_role_with_a_turn_in_flight_is_not_unbound(
    desk: Desk, dromon: Run
) -> None:
    """F7. The rule holds for a role nobody owns, by update and by delete, since the
    turn goes on with the tools and access it began with."""
    assert dromon("role", "vacate", "core")[0] == exits.OK
    in_flight(desk, "beta")
    before = desk.log.last_seq
    status, _, err = dromon("role", "update", "core", "--seats", "alpha,gamma")
    assert status == exits.FAILURE and "line beta has a turn in flight" in err
    status, _, err = dromon("role", "delete", "core")
    assert status == exits.FAILURE and "line beta has a turn in flight" in err
    assert desk.log.last_seq == before and "core" in desk.log.state.roles


def test_an_idle_writer_is_refused_leave_until_it_cannot_write(
    desk: Desk, dromon: Run
) -> None:
    """F1 and F4. A hibernated writer, unbound, would be a writer beside the owner
    once awakened, so it is refused until it is read only."""
    desk.scheduler.hibernate("gamma")
    status, _, err = dromon("role", "update", "core", "--seats", "alpha,beta")
    assert status == exits.FAILURE
    assert f"line gamma could write {TREE}, the worktree of role core, which" in err
    reader = LineConfig(
        builtin_tools=("Read", "Edit"), workdir_access=WorkdirAccess.READ
    )
    desk.scheduler.configure_line("gamma", reader)
    status, _, err = dromon("role", "update", "core", "--seats", "alpha,beta")
    assert status == exits.OK, err
    assert "gamma" not in desk.seats("core")


def test_a_second_role_may_not_share_a_worktree(desk: Desk, dromon: Run) -> None:
    """F3, the review's probe, by create, by update and by real path."""
    before = desk.log.last_seq
    status, _, err = dromon(
        "role", "create", "dup", "--workdir", TREE, "--seats", "delta"
    )
    assert status == exits.FAILURE
    assert f"roles core and dup would share the worktree {TREE}, so two owners" in err
    (desk.paths.root / "tmp/alias").symlink_to(desk.paths.root / TREE)
    status, _, err = dromon(
        "role", "create", "dup", "--workdir", "tmp/alias", "--seats", "delta"
    )
    assert status == exits.FAILURE and "roles core and dup would share" in err
    assert desk.log.last_seq == before
    assert create_docs(dromon)[0] == exits.OK
    assert dromon("role", "vacate", "core")[0] == exits.OK
    status, _, err = dromon("role", "update", "core", "--workdir", DOCS)
    assert status == exits.FAILURE and "roles core and docs would share" in err


def test_a_role_worktree_may_not_lie_inside_another_or_hold_it(
    desk: Desk, dromon: Run
) -> None:
    """Round 10 re-review, N4. Worktrees were compared for equality only, so a role
    inside another's worktree, or one holding it, had two owners for the inner one.
    Each direction is refused, by create and by update, and by real path."""
    inner = f"{TREE}/sub"
    (desk.paths.root / inner).mkdir()
    (desk.paths.root / "tmp/inner-alias").symlink_to(desk.paths.root / inner)
    before = desk.log.last_seq
    for workdir in (inner, "tmp/inner-alias", ".", "tmp/worktrees"):
        status, _, err = dromon(
            "role", "create", "dup", "--workdir", workdir, "--seats", "delta"
        )
        assert status == exits.FAILURE, workdir
        assert "roles core and dup would have worktrees one inside the other" in err
    assert desk.log.last_seq == before
    assert create_docs(dromon)[0] == exits.OK
    status, _, err = dromon("role", "update", "docs", "--workdir", inner)
    assert status == exits.FAILURE
    assert "roles core and docs would have worktrees one inside the other" in err
    assert desk.log.state.roles["docs"].workdir == DOCS


def test_a_writer_inside_an_owned_worktree_is_refused_and_waits(
    desk: Desk, dromon: Run
) -> None:
    """Round 10 re-review, N4. A writer that is not a seat, in a directory inside the
    owner's worktree, was neither refused nor held. It is now, and the owner's first
    turn waits for one in flight, as for a writer in the worktree itself."""
    inner = f"{TREE}/sub"
    (desk.paths.root / inner).mkdir()
    with pytest.raises(EventRejected, match=f"which overlaps {TREE}, the worktree"):
        desk.scheduler.configure_line("fox", replace(WRITER, workdir=inner))
    # A reader there is allowed, and one a log made before the rule waits.
    desk.scheduler.configure_line("fox", LineConfig(workdir=inner))
    assert desk.scheduler.beside_owner("fox") is None
    desk.log.append(
        LineConfigured("fox", encode_config(replace(WRITER, workdir=inner)))
    )
    assert desk.scheduler.beside_owner("fox") == "core"
    assert not desk.scheduler._eligible("fox")  # pyright: ignore[reportPrivateUsage]
    in_flight(desk, "fox")
    assert desk.scheduler._waits_for_writer("alpha")  # pyright: ignore[reportPrivateUsage]


def test_the_owner_may_not_be_configured_out_of_the_role_worktree(desk: Desk) -> None:
    """F2, the review's probe, by the scheduler and by the client's operation."""
    before = desk.log.last_seq
    with pytest.raises(EventRejected, match="line alpha owns role core, so it runs"):
        desk.scheduler.configure_line("alpha", LineConfig(workdir=DOCS))
    response = desk.ask(
        "configure_line", {"line_id": "alpha", "config": {"workdir": DOCS}}
    )
    assert not response.ok
    assert "Vacate the role with dromon role vacate core" in str(response.error_message)
    assert desk.log.last_seq == before


def test_a_seat_that_runs_elsewhere_does_not_gain_its_role(
    desk: Desk, dromon: Run
) -> None:
    """F2. A seat that does not own its role may move, as a vacant role's seats do
    before the role moves, but it gains the role only in the worktree."""
    desk.scheduler.awaken("beta")
    desk.scheduler.configure_line("beta", LineConfig(workdir=DOCS))
    assert "beta" not in desk.scheduler.usable_seats("core")
    status, _, err = dromon("role", "handoff", "core", "--to", "beta")
    assert status == exits.FAILURE and "so it cannot own the role" in err
    assert dromon("role", "vacate", "core")[0] == exits.OK
    status, _, err = dromon("role", "take", "core", "--seat", "beta")
    assert status == exits.FAILURE
    assert "line beta runs in tmp/worktrees/docs, not in role core's worktree" in err
    assert desk.log.state.roles["core"].owner is None


def test_a_writer_that_is_not_a_seat_may_not_join_an_owned_worktree(
    desk: Desk,
) -> None:
    """F4, the review's probe, by configure, by create and by an override."""
    before = desk.log.last_seq
    with pytest.raises(EventRejected, match=f"line fox could write {TREE}, the work"):
        desk.scheduler.configure_line("fox", replace(WRITER, workdir=TREE))
    # On the project root, hotel holds the worktree, round 10 re-review, N4.
    with pytest.raises(
        EventRejected, match=f"line hotel could write ., which overlaps {TREE}, the"
    ):
        desk.scheduler.create_line("hotel", HAIKU, Strategy.SUBSCRIPTION, WRITER)
    assert "hotel" not in desk.log.state.lines
    with pytest.raises(EventRejected, match=f"line delta could write {TREE}, the"):
        desk.scheduler.override_line("delta", "builtin_tools", ["Read", "Edit"])
    assert desk.log.last_seq == before
    # A line confined to reading may join, and so may any line once the role is vacant.
    desk.scheduler.configure_line(
        "fox",
        LineConfig(
            workdir=TREE,
            builtin_tools=("Read", "Edit"),
            workdir_access=WorkdirAccess.READ,
        ),
    )


def test_a_writer_left_beside_an_owner_waits_and_is_shown(
    desk: Desk, dromon: Run
) -> None:
    """F4. A log made before the rule can hold such a line, which waits, and the
    owner's first turn waits for one in flight."""
    desk.log.append(LineConfigured("delta", encode_config(WRITER)))
    assert desk.scheduler.beside_owner("delta") == "core"
    assert not desk.scheduler._eligible("delta")  # pyright: ignore[reportPrivateUsage]
    status, out, _ = dromon("role", "status", "core")
    assert status == exits.OK
    assert (
        "line delta waits, since it could write the worktree and is not a seat" in out
    )
    in_flight(desk, "delta")
    assert desk.scheduler._waits_for_writer("alpha")  # pyright: ignore[reportPrivateUsage]


def test_a_seat_is_not_moved_between_roles_by_a_command(
    desk: Desk, dromon: Run
) -> None:
    """The review's test gap. A seat of one role is refused as a seat of another,
    first for where it runs and then by the model."""
    assert create_docs(dromon)[0] == exits.OK
    status, _, err = dromon(
        "role", "update", "core", "--seats", "alpha,beta,gamma,echo"
    )
    assert status == exits.FAILURE
    assert "line echo runs in tmp/worktrees/docs, not in the role's worktree" in err
    desk.scheduler.configure_line("echo", LineConfig(workdir=TREE))
    before = desk.log.last_seq
    status, _, err = dromon(
        "role", "update", "core", "--seats", "alpha,beta,gamma,echo"
    )
    assert status == exits.FAILURE and "line echo is a seat of role docs" in err
    assert desk.log.last_seq == before
    assert desk.seats("docs") == {"echo": True, "fox": False}


def test_a_seat_that_does_not_own_its_role_is_built_read_only(desk: Desk) -> None:
    """F7, confirmed. The confining engines were derived from the capability table,
    but a seat's engine was built with its own access, so gamma wrote beside alpha."""
    assert desk.scheduler.engine_config("gamma").workdir_access is WorkdirAccess.READ
    assert desk.scheduler.engine_config("alpha").workdir_access is WorkdirAccess.WRITE
    assert desk.scheduler.engine_config("delta").workdir_access is WorkdirAccess.WRITE
    desk.scheduler.give_ownership(ANA, "core", None)
    assert desk.scheduler.engine_config("alpha").workdir_access is WorkdirAccess.READ


def test_a_role_returning_to_the_file_records_its_first_owner_in_one_pass() -> None:
    """F5, the review's probe. The pass that returns the role to the file records the
    file's first owner too, rather than at the next start or reload."""
    config = parse(project(CORE + 'owner = "core"\n'))
    state = State()
    for event in (
        OperatorRegistered("ana"),
        LineCreated("core", HAIKU, "subscription"),
        LineCreated("core-luna", HAIKU, "subscription"),
        LineCreated("other", HAIKU, "subscription"),
        RoleDeclared("core", WORKTREE, seats=("core", "core-luna")),
        SeatBound("core", "core", True),
        SeatBound("core-luna", "core", True),
        RoleHeld("core", RoleHolder.OPERATOR, ANA, AT),
    ):
        state.apply(event)
    plan = plan_file_roles(state, config, ANA, AT)
    assert [type(event) for event in plan.events] == [RoleHeld, OwnershipChanged]
    for event in plan.events:
        state.apply(event)
    assert state.roles["core"].owner == "core"
    assert state.holder_of("core") is RoleHolder.FILE


def test_role_status_shows_the_hold_and_how_the_file_differs(
    reloads: Reloads,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """F6. A notice reaches only the operator who started or reloaded, so the role's
    status shows the difference too, to the client and to a seat's role_status."""
    assert reloads.reload(CORE) == []
    response = reloads.ask(
        "role_update", {"role_id": "core", "operator": "ben", "handoff_seconds": 900}
    )
    assert response.ok, response.error_message
    assert len(reloads.reload(CORE)) == 1
    response = reloads.ask("roles", {"role_id": "core"})
    assert response.ok and response.result is not None
    role = cast(list[dict[str, object]], response.result["roles"])[0]
    difference = "handoff_seconds is 3600 in the file and 900 in the log"
    assert (role["held_by"], role["in_project_file"]) == ("operator", True)
    assert role["file_differences"] == [difference]

    def call(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        del socket
        return reloads.ask(operation.value, args)

    monkeypatch.setattr(cli, "call", call)
    root = ["--project", str(reloads.paths.root)]
    runtime = ["--runtime-dir", str(reloads.paths.runtime_dir)]
    assert cli.main([*root, *runtime, "role", "status", "core"]) == exits.OK
    out = capsys.readouterr().out
    assert f"the project file is not applied, since {difference}" in out
    assert reloads.reload(CORE + "handoff_seconds = 900\n")
    response = reloads.ask("roles", {"role_id": "core"})
    assert response.result is not None
    role = cast(list[dict[str, object]], response.result["roles"])[0]
    assert (role["held_by"], role["file_differences"]) == ("file", [])


def refused_reload(reloads: Reloads, text: str) -> str:
    """Write ``text`` as the project file, reload, and return the refusal, checking
    that nothing was recorded."""
    reloads.file.write_text(text)
    before = reloads.log.last_seq
    response = reloads.ask("reload_project")
    assert not response.ok
    assert reloads.log.last_seq == before
    return str(response.error_message)


def test_a_file_with_two_roles_on_one_worktree_is_refused_whole(
    reloads: Reloads,
) -> None:
    """F3, for the file, at a reload, naming both roles."""
    twin = CORE.replace('"core", "core-luna"', '"core"') + (
        f'[roles.twin]\nworkdir = "{WORKTREE}"\nseats = ["core-luna"]\n'
    )
    error = refused_reload(reloads, project(twin))
    assert f"roles core and twin would share the worktree {WORKTREE}" in error
    assert not reloads.log.state.roles and not reloads.log.state.lines


def test_a_file_role_on_a_role_only_in_the_log_is_refused(reloads: Reloads) -> None:
    """F3, between the file and a role an operator created."""
    assert reloads.reload("") == []
    response = reloads.ask(
        "role_create",
        {"role_id": "desk", "operator": "ana", "workdir": ".", "seats": ["other"]},
    )
    assert response.ok, response.error_message
    error = refused_reload(reloads, project(CORE.replace(WORKTREE, ".")))
    assert "roles core and desk would share the worktree ." in error


WRITING_OTHER = LINES.replace(
    'workdir = "."', f'workdir = "{WORKTREE}"\nbuiltin_tools = ["Read", "Edit"]'
)


def test_a_file_writer_beside_the_files_owner_is_refused_at_reload_and_start(
    reloads: Reloads,
) -> None:
    """F4, for the file. The refusal names the line and why, at a reload, and at a
    start through ``check_roles``, before anything is applied."""
    text = project(CORE + 'owner = "core"\n', WRITING_OTHER)
    error = refused_reload(reloads, text)
    assert "the project file is refused: line other could write" in error
    assert "the worktree of role core, which core owns" in error
    scheduler = start_scheduler(reloads.log, reloads.paths.root)
    with pytest.raises(ProjectMismatch, match="line other could write"):
        check_roles(scheduler, reloads.log, parse(text))
    # Without an owner the file applies, and the line waits once the role is given.
    reloads.file.write_text(project(CORE, WRITING_OTHER))
    response = reloads.ask("reload_project")
    assert response.ok, response.error_message
    scheduler = reloads.daemon.scheduler
    assert scheduler.beside_owner("other") is None
    scheduler.give_ownership(ANA, "core", "core")
    assert scheduler.beside_owner("other") == "core"


# Round 14, item 4. Retiring a line.


def test_a_retired_line_is_hibernated_and_takes_no_turn_mail_or_seat(
    desk: Desk, dromon: Run
) -> None:
    status, out, _ = dromon("retire", "echo", "--operator", "ana")
    assert status == exits.OK, out
    assert f"dromon retire: line echo retired at {AT}" in out
    state = desk.log.state
    assert state.retired_lines["echo"].by == ANA
    assert state.lines["echo"].hibernated
    assert desk.scheduler.condition("echo") == "retired"
    refused = "line echo was retired by @ana at"
    sent = desk.ask("send", {"recipient": "echo", "body": "hi", "operator": "ana"})
    assert not sent.ok and refused in str(sent.error_message)
    with pytest.raises(EventRejected, match=refused):
        desk.scheduler.awaken("echo")
    for event in (
        LineStateChanged("echo", LineState.RUNNING),
        SeatBound("echo", "core", True),
        LineCreated("echo", HAIKU, "subscription"),
    ):
        with pytest.raises(EventRejected):
            state.check(event)
    # The line and the mail it had stay in the log.
    assert "echo" in state.lines


def test_a_retirement_refused_records_nothing(desk: Desk, dromon: Run) -> None:
    def refused(line_id: str) -> str:
        before = desk.log.last_seq
        response = desk.ask("retire_line", {"line_id": line_id, "operator": "ana"})
        assert not response.ok
        assert desk.log.last_seq == before
        return str(response.error_message)

    # A seat is not hibernated by a refused retirement, since both events are
    # checked before either is appended.
    assert "is a seat of role core, which is unbound" in refused("alpha")
    assert not desk.log.state.lines["alpha"].hibernated
    assert "no line nobody" in refused("nobody")
    desk.log.append(LineStateChanged("fox", LineState.RUNNING))
    assert "line fox is in a turn" in refused("fox")
    desk.daemon.project = parse(project("", '[lines.echo]\nmodel = "claude-haiku-5-5"'))
    assert "is declared in the project file" in refused("echo")
    desk.daemon.project = ProjectConfig()
    status, _, _ = dromon("retire", "echo", "--operator", "ana")
    assert status == exits.OK
    assert "line echo was retired by @ana" in refused("echo")
    status, _, err = dromon("retire", "echo", "--operator", "ana")
    assert status != exits.OK and "was retired" in err


def test_a_retired_writer_leaves_the_one_writer_rule_and_its_name_is_not_reused(
    reloads: Reloads,
) -> None:
    """A line only the log knows, which could write an owned role's worktree,
    refuses every file until it is retired, as on 2026-10-31, and a file may then
    not declare it again."""
    lines = LINES.replace("[lines.other]", "[lines.unused]")
    owned = project(CORE + 'owner = "core"\n', lines)
    reloads.file.write_text(project(CORE, lines))
    assert reloads.ask("reload_project").ok
    scheduler = reloads.daemon.scheduler
    scheduler.create_line(
        "stray", HAIKU, Strategy.SUBSCRIPTION, replace(WRITER, workdir=WORKTREE)
    )
    assert "line stray could write" in refused_reload(reloads, owned)
    scheduler.retire_line(ANA, "stray")
    reloads.file.write_text(owned)
    response = reloads.ask("reload_project")
    assert response.ok, response.error_message
    declared = project(CORE, lines + '[lines.stray]\nmodel = "claude-haiku-5-5"\n')
    error = refused_reload(reloads, declared)
    assert "line stray was retired by @ana" in error
    assert "may not declare it" in error


def test_status_names_a_line_the_file_dropped_and_a_retired_one(
    reloads: Reloads,
) -> None:
    reloads.file.write_text(project(CORE))
    assert reloads.ask("reload_project").ok
    scheduler = reloads.daemon.scheduler
    for line_id in ("dropped", "gone"):
        scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION, LineConfig())
    scheduler.retire_line(ANA, "gone")
    summaries = {
        str(entry["line_id"]): entry for entry in reloads.daemon.line_summaries()
    }
    assert summaries["core"]["declared"] is True
    assert summaries["dropped"]["declared"] is False
    rows = {line_id: cli._line_row(entry) for line_id, entry in summaries.items()}  # pyright: ignore[reportPrivateUsage]
    assert "not in the project file" in rows["dropped"]
    assert "not in the project file" not in rows["core"]
    assert rows["gone"].startswith("line gone, retired,")
    assert "hibernated" not in rows["gone"]
    assert "not in the project file" not in rows["gone"]
    # A daemon from before round 14 gives no declared, which is then not shown.
    older = {k: v for k, v in summaries["dropped"].items() if k != "declared"}
    assert "project file" not in cli._line_row(older)  # pyright: ignore[reportPrivateUsage]
    status = reloads.ask("status")
    assert status.ok and status.result is not None
    retired = cast(list[dict[str, object]], status.result["retired_lines"])
    assert [entry["line_id"] for entry in retired] == ["gone"]
