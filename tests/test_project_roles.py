"""Roles declared in the project file: the ``[roles]`` tables and ``may_own``, the plan
of events that records them, and the daemon recording them at start and on reload."""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.configview import Overrides
from dromon.daemon import (
    Daemon,
    ProjectMismatch,
    apply_project,
    apply_roles,
    check_roles,
    check_sandboxes,
)
from dromon.eventlog import EventLog
from dromon.lineconfig import ConfigError
from dromon.model import (
    DEFAULT_HANDOFF_BELOW_PERCENT,
    FailoverPolicy,
    Event,
    LineAddress,
    LineCreated,
    LineState,
    LineStateChanged,
    OperatorAddress,
    OperatorRegistered,
    OwnershipCause,
    OwnershipChanged,
    RoleDeclared,
    Seat,
    SeatBound,
    State,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import (
    MAX_SEATS,
    ConfigFileError,
    ProjectConfig,
    RoleDefinition,
    config_path,
    parse,
)
from dromon.roles import RoleConflict, plan_roles
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
AT = "2026-10-27T06:00:00Z"
WORKTREE = "tmp/worktrees/core"

LINES = """
[lines.core]
model = "claude-haiku-5-5"
[lines.core-luna]
model = "claude-haiku-5-5"
[lines.core-grok]
model = "claude-haiku-5-5"
may_own = false
"""


def project(roles: str, lines: str = LINES) -> str:
    return f"version = 1\n{lines}\n{roles}\n"


CORE = f"""
[roles.core]
workdir = "{WORKTREE}"
owner = "core"
seats = ["core", "core-luna", "core-grok"]
"""
VACANT = CORE.replace('owner = "core"\n', "")


# The project file.


def test_a_roles_table_declares_seats_an_owner_and_a_handoff_time() -> None:
    config = parse(project(CORE.replace("]\n", "]\nhandoff_seconds = 7200\n", 1)))
    assert config.roles == {
        "core": RoleDefinition(
            "core", WORKTREE, ("core", "core-luna", "core-grok"), "core", 7200
        )
    }
    assert [config.lines[i].may_own for i in ("core", "core-luna", "core-grok")] == [
        True,
        True,
        False,
    ]


def test_a_role_defaults_to_an_hour_and_may_start_vacant() -> None:
    text = f'[roles.core]\nworkdir = "{WORKTREE}"\nseats = ["core"]\n'
    (role,) = parse(project(text)).roles.values()
    assert (role.owner, role.handoff_seconds) == (None, 3600)


def test_a_seat_without_a_workdir_takes_the_roles_and_one_with_its_own_keeps_it() -> (
    None
):
    lines = LINES.replace(
        '[lines.core-luna]\nmodel = "claude-haiku-5-5"',
        '[lines.core-luna]\nmodel = "claude-haiku-5-5"\nworkdir = "elsewhere"',
    )
    lines += '[lines.other]\nmodel = "claude-haiku-5-5"\n'
    config = parse(project(CORE, lines))
    assert config.lines["core"].config.workdir == WORKTREE
    assert config.lines["core-grok"].config.workdir == WORKTREE
    assert config.lines["core-luna"].config.workdir == "elsewhere"
    assert config.lines["other"].config.workdir is None


def test_may_own_comes_from_a_profile_and_a_line_overrides_it() -> None:
    text = (
        'version = 1\n[profiles.alternate]\nmodel = "claude-haiku-5-5"\n'
        'may_own = false\n[lines.a]\nprofile = "alternate"\n'
        '[lines.b]\nprofile = "alternate"\nmay_own = true\n'
    )
    config = parse(text)
    assert (config.lines["a"].may_own, config.lines["b"].may_own) == (False, True)


def test_a_file_without_roles_has_none() -> None:
    assert parse(project("")).roles == {}


TOO_MANY = ", ".join(f'"s{n}"' for n in range(MAX_SEATS + 1))


@pytest.mark.parametrize(
    ("roles", "message"),
    [
        ('[roles.Core]\nworkdir = "x"\nseats = ["core"]', "not a valid role"),
        ('[roles.core]\nworkdir = "x"\nseats = ["core"]\nlead = "core"', "lead"),
        ('[roles.core]\nseats = ["core"]', "workdir"),
        ('[roles.core]\nworkdir = "/abs"\nseats = ["core"]', "not a relative path"),
        ('[roles.core]\nworkdir = "a/../b"\nseats = ["core"]', "step"),
        ('[roles.core]\nworkdir = "a//b"\nseats = ["core"]', "step"),
        ('[roles.core]\nworkdir = "x"\nseats = "core"', "must be a list of 1"),
        ('[roles.core]\nworkdir = "x"\nseats = []', "must be a list of 1"),
        (f'[roles.core]\nworkdir = "x"\nseats = [{TOO_MANY}]', "must be a list"),
        ('[roles.core]\nworkdir = "x"\nseats = ["ghost"]', "not a line this file"),
        ('[roles.core]\nworkdir = "x"\nseats = [3]', "not a line this file"),
        ('[roles.core]\nworkdir = "x"\nseats = ["core", "core"]', "twice"),
        (
            '[roles.core]\nworkdir = "x"\nseats = ["core"]\nowner = "core-luna"',
            "is not a seat of the role",
        ),
        (
            '[roles.core]\nworkdir = "x"\nseats = ["core-grok"]\nowner = "core-grok"',
            "may_own false",
        ),
        ('[roles.core]\nworkdir = "x"\nseats = ["core"]\nhandoff_seconds = 59', "60"),
        (
            '[roles.core]\nworkdir = "x"\nseats = ["core"]\nhandoff_seconds = 86401',
            "86400",
        ),
        (
            '[roles.core]\nworkdir = "x"\nseats = ["core"]\nhandoff_seconds = 600.0',
            "whole number",
        ),
        (
            '[roles.core]\nworkdir = "x"\nseats = ["core"]\nhandoff_seconds = true',
            "whole number",
        ),
        (
            '[roles.a]\nworkdir = "x"\nseats = ["core"]\n'
            '[roles.b]\nworkdir = "y"\nseats = ["core"]',
            "already a seat of role a",
        ),
        (
            '[roles.core]\nworkdir = "x"\nseats = ["core"]\nfailover = "never"',
            "must be one of automatic, ask",
        ),
        (
            '[roles.core]\nworkdir = "x"\nseats = ["core"]\nfailover = true',
            "must be one of automatic, ask",
        ),
        (
            '[roles.core]\nworkdir = "x"\nseats = ["core"]\n'
            "handoff_below_percent = 101",
            "from 0 to 100",
        ),
        (
            '[roles.core]\nworkdir = "x"\nseats = ["core"]\n'
            "handoff_below_percent = 5.5",
            "from 0 to 100",
        ),
    ],
)
def test_a_bad_roles_table_is_refused(roles: str, message: str) -> None:
    with pytest.raises(ConfigFileError, match=message):
        parse(project(roles))


def test_roles_must_be_a_table() -> None:
    with pytest.raises(ConfigFileError, match="roles: must be a table"):
        parse("version = 1\nroles = 3\n")


def test_may_own_must_be_a_boolean() -> None:
    lines = LINES.replace("may_own = false", 'may_own = "no"')
    with pytest.raises(ConfigFileError, match="may_own: must be a boolean"):
        parse(project("", lines))


# The plan.


def applied(state: State, events: Sequence[Event]) -> State:
    for event in events:
        state.check(event)
        state.apply(event)
    return state


def with_lines(config: ProjectConfig) -> State:
    """A state that holds the file's lines, as ``apply_project`` leaves it."""
    return applied(
        State(),
        [
            LineCreated(i, d.model, d.strategy.value)
            for i, d in sorted(config.lines.items())
        ],
    )


def test_a_new_role_is_declared_bound_and_given_its_first_owner() -> None:
    config = parse(project(CORE))
    events = plan_roles(State(), config, ANA, AT)
    assert events == (
        RoleDeclared("core", WORKTREE, 3600, ("core", "core-luna", "core-grok")),
        SeatBound("core", "core", True),
        SeatBound("core-luna", "core", True),
        SeatBound("core-grok", "core", False),
        OperatorRegistered("ana"),
        OwnershipChanged("core", "core", None, OwnershipCause.DECLARED, ANA, None, AT),
    )
    state = applied(with_lines(config), events)
    assert state.roles["core"].owner == "core"
    assert state.seats["core-grok"] == Seat("core", False)


def test_a_plan_applied_once_plans_nothing_more() -> None:
    config = parse(project(CORE))
    state = applied(with_lines(config), plan_roles(State(), config, ANA, AT))
    assert plan_roles(state, config, ANA, AT) == ()


def test_a_vacant_role_records_no_owner_and_registers_no_operator() -> None:
    config = parse(project(VACANT))
    events = plan_roles(State(), config, ANA, AT)
    assert not any(isinstance(e, OwnershipChanged | OperatorRegistered) for e in events)


def test_a_known_operator_is_not_registered_again() -> None:
    config = parse(project(CORE))
    state = applied(with_lines(config), [OperatorRegistered("ana")])
    events = plan_roles(state, config, ANA, AT)
    assert not any(isinstance(e, OperatorRegistered) for e in events)


def test_a_restart_does_not_undo_a_relinquishment() -> None:
    config = parse(project(CORE))
    state = applied(with_lines(config), plan_roles(State(), config, ANA, AT))
    state = applied(
        state,
        [
            OwnershipChanged(
                "core",
                None,
                "core",
                OwnershipCause.RELINQUISHED,
                LineAddress("core"),
                None,
                AT,
            )
        ],
    )
    assert plan_roles(state, config, ANA, AT) == ()
    assert state.roles["core"].owner is None


def test_a_role_takes_a_failover_policy_and_a_handoff_threshold() -> None:
    (role,) = parse(project(CORE)).roles.values()
    assert (role.failover, role.handoff_below_percent) == (
        FailoverPolicy.AUTOMATIC,
        DEFAULT_HANDOFF_BELOW_PERCENT,
    )
    text = CORE + 'failover = "ask"\nhandoff_below_percent = 0\n'
    (role,) = parse(project(text)).roles.values()
    assert (role.failover, role.handoff_below_percent) == (FailoverPolicy.ASK, 0)


def test_a_changed_order_or_policy_declares_the_role_again() -> None:
    config = parse(project(CORE))
    state = applied(with_lines(config), plan_roles(State(), config, ANA, AT))
    reordered = parse(
        project(
            CORE.replace(
                '["core", "core-luna", "core-grok"]',
                '["core-luna", "core", "core-grok"]',
            )
            + 'failover = "ask"\n'
        )
    )
    assert plan_roles(state, reordered, ANA, AT) == (
        RoleDeclared(
            "core",
            WORKTREE,
            3600,
            ("core-luna", "core", "core-grok"),
            FailoverPolicy.ASK,
        ),
    )


def test_a_changed_handoff_time_declares_the_role_again() -> None:
    config = parse(project(CORE))
    state = applied(with_lines(config), plan_roles(State(), config, ANA, AT))
    longer = parse(project(CORE + "handoff_seconds = 600\n"))
    assert plan_roles(state, longer, ANA, AT) == (
        RoleDeclared("core", WORKTREE, 600, ("core", "core-luna", "core-grok")),
    )


def test_a_moved_worktree_is_refused_while_owned_and_declared_while_vacant() -> None:
    config = parse(project(CORE))
    owned = applied(with_lines(config), plan_roles(State(), config, ANA, AT))
    moved = parse(project(CORE.replace(WORKTREE, "tmp/worktrees/core2")))
    with pytest.raises(RoleConflict, match="role core: .*only while vacant"):
        plan_roles(owned, moved, ANA, AT)
    vacant_file = parse(project(VACANT))
    vacant = applied(with_lines(vacant_file), plan_roles(State(), vacant_file, ANA, AT))
    moved_vacant = parse(project(VACANT.replace(WORKTREE, "tmp/worktrees/core2")))
    events = plan_roles(vacant, moved_vacant, ANA, AT)
    assert events[0] == RoleDeclared(
        "core", "tmp/worktrees/core2", 3600, ("core", "core-luna", "core-grok")
    )


def test_barring_the_current_owner_from_owning_is_refused() -> None:
    config = parse(project(CORE))
    state = applied(with_lines(config), plan_roles(State(), config, ANA, AT))
    barred = parse(
        project(
            VACANT,
            LINES.replace(
                '[lines.core]\nmodel = "claude-haiku-5-5"',
                '[lines.core]\nmodel = "claude-haiku-5-5"\nmay_own = false',
            ),
        )
    )
    with pytest.raises(RoleConflict, match="must give the role up"):
        plan_roles(state, barred, ANA, AT)


def test_a_seat_of_another_role_in_the_log_is_refused() -> None:
    config = parse(project(CORE))
    state = applied(
        with_lines(config),
        [RoleDeclared("docs", "tmp/docs"), SeatBound("core-luna", "docs", True)],
    )
    with pytest.raises(RoleConflict, match="core-luna is a seat of role docs"):
        plan_roles(state, config, ANA, AT)


def test_a_stopped_first_owner_is_refused() -> None:
    config = parse(project(CORE))
    state = applied(with_lines(config), [LineStateChanged("core", LineState.STOPPED)])
    with pytest.raises(RoleConflict, match="role core"):
        plan_roles(state, config, ANA, AT)


def test_planning_changes_nothing_in_the_given_state() -> None:
    config = parse(project(CORE))
    state = with_lines(config)
    before = state.copy()
    plan_roles(state, config, ANA, AT)
    assert (state.roles, state.seats, state.operators) == (
        before.roles,
        before.seats,
        before.operators,
    )


# The daemon.


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


def wall() -> datetime:
    return datetime(2026, 10, 27, 6, 0, 0, tzinfo=UTC)


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    (tmp_path / WORKTREE).mkdir(parents=True)
    return found


def test_the_worktree_must_exist_and_a_seat_must_run_in_it(paths: ProjectPaths) -> None:
    check_sandboxes(parse(project(CORE)), paths)
    missing = parse(project(CORE.replace(WORKTREE, "tmp/worktrees/none")))
    with pytest.raises(ConfigError, match="not an existing directory"):
        check_sandboxes(missing, paths)
    (paths.root / "elsewhere").mkdir()
    lines = LINES.replace(
        '[lines.core-luna]\nmodel = "claude-haiku-5-5"',
        '[lines.core-luna]\nmodel = "claude-haiku-5-5"\nworkdir = "elsewhere"',
    )
    with pytest.raises(ConfigError, match="core-luna is a seat of role core"):
        check_sandboxes(parse(project(CORE, lines)), paths)


def test_a_worktree_in_a_protected_directory_is_refused(paths: ProjectPaths) -> None:
    config = parse(project(CORE.replace(WORKTREE, ".dromon")))
    with pytest.raises(ConfigError, match="protects"):
        check_sandboxes(config, paths)


def test_a_start_records_the_roles_once(paths: ProjectPaths) -> None:
    config = parse(project(CORE))
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(
            log, no_engine, hold_wakes=True, default_operator="ana", wall_clock=wall
        )

        def start() -> None:
            check_roles(scheduler, log, config)
            apply_project(scheduler, log, config)
            apply_roles(scheduler, log, config)

        start()
        state = log.state
        assert state.roles["core"].owner == "core"
        changed = state.roles["core"].changed
        assert changed is not None and (changed.by, changed.changed_at) == (ANA, AT)
        assert sorted(state.seats) == ["core", "core-grok", "core-luna"]
        after = log.last_seq
        start()
        assert log.last_seq == after


def test_a_start_refuses_a_file_whose_roles_conflict_with_the_log(
    paths: ProjectPaths,
) -> None:
    config = parse(project(CORE))
    moved = parse(project(CORE.replace(WORKTREE, "tmp/worktrees/core2")))
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True, wall_clock=wall)
        apply_project(scheduler, log, config)
        apply_roles(scheduler, log, config)
        before = log.last_seq
        with pytest.raises(ProjectMismatch, match="only while vacant"):
            check_roles(scheduler, log, moved)
        with pytest.raises(ProjectMismatch, match="only while vacant"):
            apply_roles(scheduler, log, moved)
        assert log.last_seq == before


def test_a_reload_records_the_roles_and_a_conflicting_one_changes_nothing(
    paths: ProjectPaths,
) -> None:
    file = config_path(paths.state_dir)
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True, wall_clock=wall)
        target = Daemon(
            paths,
            log,
            "2026-10-27T06:00:00Z",
            scheduler,
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=True,
        )

        def ask(op: str) -> tuple[bool, dict[str, object] | None, str | None]:
            line: dict[str, object] = {"id": "r", "op": op, "v": 1, "args": {}}
            response = target.respond((json.dumps(line) + "\n").encode())
            return response.ok, response.result, response.error_message

        file.write_text(project(CORE))
        ok, result, error = ask("reload_project")
        assert ok, error
        assert result is not None and result["roles"] == ["core"]
        assert log.state.roles["core"].owner == "core"
        before = log.last_seq
        (paths.root / "tmp/worktrees/core2").mkdir()
        moved = CORE.replace(WORKTREE, "tmp/worktrees/core2")
        file.write_text(
            project(moved, LINES + '[lines.new]\nmodel = "' + HAIKU + '"\n')
        )
        ok, _, error = ask("reload_project")
        assert not ok and error is not None and "only while vacant" in error
        assert log.last_seq == before
        assert "new" not in log.state.lines
