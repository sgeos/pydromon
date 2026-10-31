"""An operator's create, update and delete of roles while the daemon runs, round 10.

``dromon role create``, ``update`` and ``delete`` go through the client into a real
daemon and scheduler over a real event log. The new events, ``seat_unbound``,
``role_retired`` and ``role_held``, are checked in the model and replayed after a
restart, and a reload of the project file against a role an operator changed reports
what it leaves, as ``docs/architecture/ROLES.md`` states."""

import json
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli, exits
from dromon.agents import DAEMON_TOOLS
from dromon.configview import Overrides
from dromon.daemon import Daemon, apply_roles
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.lineconfig import LineConfig
from dromon.model import (
    EventRejected,
    LineCreated,
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
    """A daemon over a real event log, with role core on the project root, seats
    alpha, beta and gamma, alpha its owner, beta hibernated and gamma unable to own,
    a line delta on the root that is a seat of no role, and lines echo and fox in the
    worktree of a role yet to be made. The client's requests reach it directly."""

    def __init__(self, paths: ProjectPaths) -> None:
        self.paths = paths
        (paths.root / DOCS).mkdir(parents=True)
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
            self.scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
        # gamma may write its working directory, so it is refused leave of an owned
        # role while it runs in the owner's worktree.
        self.scheduler.create_line("gamma", HAIKU, Strategy.SUBSCRIPTION, WRITER)
        for line_id in ("echo", "fox"):
            self.scheduler.create_line(
                line_id, HAIKU, Strategy.SUBSCRIPTION, LineConfig(workdir=DOCS)
            )
        for event in (
            RoleDeclared("core", ".", seats=("alpha", "beta", "gamma")),
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
        (("core", "--workdir", ".", "--seats", "delta"), "role core exists"),
        (("docs", "--workdir", DOCS, "--seats", "echo,nobody"), "no line nobody"),
        (("docs", "--workdir", DOCS, "--seats", "echo,delta"), "line delta runs in"),
        (("docs", "--workdir", ".", "--seats", "alpha"), "is a seat of role core"),
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
    # beta is hibernated, so it may leave an owned role. delta joins, ahead of alpha.
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
        (("--seats", "alpha,beta"), "Hibernate it with dromon hibernate gamma"),
        (("--workdir", DOCS, "--seats", "echo,fox"), "line alpha owns role core"),
        (("--workdir", DOCS), "runs in ., not in the role's worktree"),
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
