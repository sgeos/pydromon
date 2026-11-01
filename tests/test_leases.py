"""Leases: the project's ``[leases]`` table, the ``acquire_lease`` and ``release_lease``
tools, when a lease ends, and what a client sees."""

import asyncio
import json
from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.configview import Overrides
from dromon.daemon import Daemon
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.lineconfig import LineConfig
from dromon.model import (
    EventRejected,
    InvalidField,
    LeaseAcquired,
    LeaseEnd,
    LeaseReleased,
    LineAddress,
    LineCreated,
    LineRebound,
    LineSessionCleared,
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
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import (
    ConfigFileError,
    ProjectConfig,
    config_path,
    load,
    parse,
)
from dromon.scheduler import (
    ACQUIRE_LEASE_TOOL_NAME,
    LEASE_PROOF_CAVEAT,
    LIST_LEASES_TOOL_NAME,
    RELEASE_LEASE_TOOL_NAME,
    LeaseRefused,
    LineEngine,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
START = datetime(2026, 10, 26, 9, 0, 0, tzinfo=UTC)
AT = "2026-10-27T05:00:00Z"
ANA = OperatorAddress("ana")
ALPHA_ADDRESS = LineAddress("alpha")


# The project file.


def test_the_leases_table_declares_names_and_a_time_to_live() -> None:
    config = parse("version = 1\n[leases]\nnames = ['gate', 'ci']\nttl_seconds = 600\n")
    assert (config.lease_names, config.lease_ttl_seconds) == (("ci", "gate"), 600)
    default = parse("version = 1\n")
    assert (default.lease_names, default.lease_ttl_seconds) == ((), 10800)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("names = 'gate'", "leases.names: must be a list"),
        ("names = ['Full Gate']", "is not an identifier"),
        ("names = [3]", "is not an identifier"),
        ("names = ['gate', 'gate']", "names 'gate' twice"),
        ("ttl_seconds = 59", "from 60 to 86400"),
        ("ttl_seconds = 86401", "from 60 to 86400"),
        ("ttl_seconds = 600.0", "from 60 to 86400"),
        ("ttl_seconds = true", "from 60 to 86400"),
        ("holder = 'alpha'", "leases.holder: is not a known key"),
    ],
)
def test_a_bad_leases_table_is_refused(text: str, message: str) -> None:
    with pytest.raises(ConfigFileError, match=message):
        parse(f"version = 1\n[leases]\n{text}\n")


# The scheduler.


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def scheduler(tmp_path: Path, clock: Clock) -> Generator[Scheduler]:
    with EventLog(tmp_path / "events.sqlite") as log:
        found = Scheduler(log, no_engine, hold_wakes=True, wall_clock=clock)
        for line_id in ("alpha", "beta"):
            found.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
        found.set_leases(["gate", "ci"], 600)
        yield found


def test_a_lease_goes_to_one_line_at_a_time(scheduler: Scheduler) -> None:
    assert scheduler.acquire_lease("alpha", "gate") == "2026-10-26T09:10:00Z"
    with pytest.raises(LeaseRefused, match="held by line alpha until 2026-10-26T09:10"):
        scheduler.acquire_lease("beta", "gate")
    assert scheduler.acquire_lease("beta", "ci") == "2026-10-26T09:10:00Z"
    scheduler.release_lease("alpha", "gate")
    assert scheduler.acquire_lease("beta", "gate")
    assert [lease["name"] for lease in scheduler.leases_of("beta")] == ["ci", "gate"]
    assert scheduler.leases_of("alpha") == []


def test_the_holder_cannot_extend_a_lease_and_only_the_holder_releases(
    scheduler: Scheduler, clock: Clock
) -> None:
    scheduler.acquire_lease("alpha", "gate")
    clock.now += timedelta(seconds=300)
    with pytest.raises(LeaseRefused, match="already, until 2026-10-26T09:10:00Z"):
        scheduler.acquire_lease("alpha", "gate")
    assert scheduler.leases_of("alpha") == [
        {"name": "gate", "expires": "2026-10-26T09:10:00Z"}
    ]
    with pytest.raises(LeaseRefused, match="line beta holds no lease 'gate'"):
        scheduler.release_lease("beta", "gate")
    with pytest.raises(LeaseRefused, match="holds no lease 'ci'"):
        scheduler.release_lease("alpha", "ci")
    clock.now += timedelta(seconds=300, microseconds=1)
    assert scheduler.acquire_lease("alpha", "gate") == "2026-10-26T09:20:01Z"


def test_an_undeclared_lease_is_refused(scheduler: Scheduler) -> None:
    with pytest.raises(LeaseRefused, match="declares no lease 'deploy'.*ci, gate"):
        scheduler.acquire_lease("alpha", "deploy")
    scheduler.set_leases([], 600)
    with pytest.raises(LeaseRefused, match="Declared leases: none"):
        scheduler.acquire_lease("alpha", "gate")


def test_a_lease_ends_when_its_time_to_live_passes(
    scheduler: Scheduler, clock: Clock
) -> None:
    scheduler.acquire_lease("alpha", "gate")
    clock.now += timedelta(seconds=599)
    with pytest.raises(LeaseRefused):
        scheduler.acquire_lease("beta", "gate")
    clock.now += timedelta(seconds=1)
    assert scheduler.leases_of("alpha") == []
    assert scheduler.acquire_lease("beta", "gate")


def test_a_lease_ends_when_its_holders_session_is_cleared_or_moved(
    scheduler: Scheduler,
) -> None:
    scheduler.acquire_lease("alpha", "gate")
    scheduler.acquire_lease("beta", "ci")
    scheduler.clear_session("alpha")
    assert scheduler.leases_of("alpha") == []
    scheduler.configure_line("beta", LineConfig(builtin_tools=("Read",)))
    assert [lease["name"] for lease in scheduler.leases_of("beta")] == ["ci"]
    scheduler.configure_line("beta", LineConfig(workdir="elsewhere"))
    assert scheduler.leases_of("beta") == []
    scheduler.acquire_lease("alpha", "gate")
    scheduler.rebind_line("alpha", "claude-opus-5-5", Strategy.SUBSCRIPTION)
    assert scheduler.leases_of("alpha") == []


def test_undeclaring_a_name_ends_its_lease_and_the_ttl_is_checked(
    scheduler: Scheduler,
) -> None:
    scheduler.acquire_lease("alpha", "gate")
    scheduler.acquire_lease("alpha", "ci")
    scheduler.set_leases(["ci"], 600)
    assert [lease["name"] for lease in scheduler.leases_of("alpha")] == ["ci"]
    for ttl in (59, 86401):
        with pytest.raises(ValueError, match="from 60 to 86400"):
            scheduler.set_leases(["ci"], ttl)


def test_a_lease_is_an_event_with_its_holder_and_both_times(
    scheduler: Scheduler, clock: Clock
) -> None:
    """Task of 2026-10-31. The acquisition is the start rounded up to the second,
    which the expiry counts from."""
    clock.now += timedelta(microseconds=1)
    expires = scheduler.acquire_lease("alpha", "gate")
    log = scheduler._log  # pyright: ignore[reportPrivateUsage]
    assert log.state.leases["gate"] == LeaseAcquired(
        "gate", "alpha", "2026-10-26T09:00:01Z", expires
    )
    assert expires == "2026-10-26T09:10:01Z"
    scheduler.release_lease("alpha", "gate")
    assert "gate" not in log.state.leases


def test_a_restart_while_a_lease_is_held_keeps_it_until_it_expires(
    tmp_path: Path, clock: Clock
) -> None:
    """Task of 2026-10-31. The restarted daemon reads the log from its file, so the
    holder, the acquisition and the expiry survive, and expiry stays the bound."""
    path = tmp_path / "restart.sqlite"
    with EventLog(path) as log:
        before = Scheduler(log, no_engine, hold_wakes=True, wall_clock=clock)
        for line_id in ("alpha", "beta"):
            before.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
        before.set_leases(["gate", "ci"], 600)
        expires = before.acquire_lease("alpha", "gate")
        held = log.state.leases["gate"]
    clock.now += timedelta(seconds=300)
    with EventLog(path) as log:
        after = Scheduler(log, no_engine, hold_wakes=True, wall_clock=clock)
        after.set_leases(["gate", "ci"], 600)
        assert log.state.leases["gate"] == held
        assert after.leases_of("alpha") == [{"name": "gate", "expires": expires}]
        with pytest.raises(LeaseRefused, match=f"held by line alpha until {expires}"):
            after.acquire_lease("beta", "gate")
        with pytest.raises(LeaseRefused, match="already"):
            after.acquire_lease("alpha", "gate")
        clock.now += timedelta(seconds=300)
        assert after.leases_of("alpha") == []
        assert after.acquire_lease("beta", "gate")
        assert log.state.leases["gate"].line_id == "beta"


def test_a_restart_that_no_longer_declares_a_name_ends_its_lease(
    tmp_path: Path, clock: Clock
) -> None:
    path = tmp_path / "restart.sqlite"
    with EventLog(path) as log:
        before = Scheduler(log, no_engine, hold_wakes=True, wall_clock=clock)
        before.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        before.set_leases(["gate", "ci"], 600)
        before.acquire_lease("alpha", "gate")
        before.acquire_lease("alpha", "ci")
        # One that has expired ends too, so the state holds only declared names.
        clock.now += timedelta(seconds=601)
    with EventLog(path) as log:
        after = Scheduler(log, no_engine, hold_wakes=True, wall_clock=clock)
        after.set_leases(["ci"], 600)
        assert set(log.state.leases) == {"ci"}
    # The end is an event, so a later start sees it without applying the file.
    with EventLog(path) as log:
        assert set(log.state.leases) == {"ci"}


def test_the_log_refuses_a_second_holder_before_expiry_by_the_events_times() -> None:
    state = State()
    state.apply(LineCreated("alpha", HAIKU, "subscription"))
    state.apply(LineCreated("beta", HAIKU, "subscription"))
    first = LeaseAcquired(
        "gate", "alpha", "2026-10-26T09:00:00Z", "2026-10-26T09:10:00Z"
    )
    state.apply(first)
    with pytest.raises(EventRejected, match="held by line alpha until"):
        state.check(
            LeaseAcquired(
                "gate", "beta", "2026-10-26T09:09:59Z", "2026-10-26T09:20:00Z"
            )
        )
    with pytest.raises(EventRejected, match="line beta holds no lease gate"):
        state.check(LeaseReleased("gate", "beta", LeaseEnd.RELEASED))
    with pytest.raises(EventRejected, match="no line"):
        state.check(LeaseAcquired("ci", "gamma", first.acquired_at, first.expires))
    with pytest.raises(InvalidField, match="expires after it is acquired"):
        LeaseAcquired("gate", "beta", first.expires, first.expires)
    state.apply(
        LeaseAcquired("gate", "beta", "2026-10-26T09:10:00Z", "2026-10-26T09:20:00Z")
    )
    assert state.leases["gate"].line_id == "beta"


def owned_role() -> State:
    """A role ``core`` with seats ``alpha``, its owner, and ``beta``, and a lease
    held by each."""
    state = State()
    for event in (
        LineCreated("alpha", HAIKU, "subscription"),
        LineCreated("beta", HAIKU, "subscription"),
        OperatorRegistered("ana"),
        RoleDeclared("core", "tmp/worktrees/core"),
        SeatBound("alpha", "core", True),
        SeatBound("beta", "core", True),
        OwnershipChanged("core", "alpha", None, OwnershipCause.DECLARED, ANA, None, AT),
        LeaseAcquired("gate", "alpha", AT, "2026-10-27T08:00:00Z"),
        LeaseAcquired("device", "alpha", AT, "2026-10-27T09:00:00Z"),
        LeaseAcquired("ci", "beta", AT, "2026-10-27T08:00:00Z"),
    ):
        state.apply(event)
    return state


@pytest.mark.parametrize(
    ("cause", "by"),
    [
        (OwnershipCause.HANDOFF, LineAddress("alpha")),
        (OwnershipCause.OPERATOR, OperatorAddress("ana")),
        (OwnershipCause.RECOVERY, OperatorAddress("ana")),
    ],
)
def test_a_change_of_owner_passes_the_previous_owners_leases_with_their_times(
    cause: OwnershipCause, by: LineAddress | OperatorAddress
) -> None:
    """Decided on 2026-10-31. The successor holds the resource its predecessor was
    using, with the same expiry, so nothing else takes it in between and nothing
    renews it."""
    state = owned_role()
    before = dict(state.leases)
    entry = None
    if cause is OwnershipCause.HANDOFF:
        entry = "e1"
        state.apply(
            RoleNoted("core", entry, "alpha", RoleNoteKind.HANDOFF, "go", "", "", AT)
        )
    state.apply(OwnershipChanged("core", "beta", "alpha", cause, by, entry, AT))
    assert {name: lease.line_id for name, lease in state.leases.items()} == {
        "gate": "beta",
        "device": "beta",
        "ci": "beta",
    }
    for name in ("gate", "device"):
        assert (state.leases[name].acquired_at, state.leases[name].expires) == (
            before[name].acquired_at,
            before[name].expires,
        )
    state.apply(LeaseReleased("gate", "beta", LeaseEnd.RELEASED))
    with pytest.raises(EventRejected, match="line alpha holds no lease device"):
        state.check(LeaseReleased("device", "alpha", LeaseEnd.RELEASED))


def test_a_change_of_owner_passes_a_lease_taken_before_the_line_was_a_seat() -> None:
    """Decided on 2026-10-31, review R4. A lease records its line and no role, so
    every lease of the former owner passes, one taken for another purpose too, as
    ``ROLES.md`` says. A lease left behind could not be released by its holder's
    successor, and Dromon cannot tell which resource a lease served."""
    state = State()
    for event in (
        LineCreated("alpha", HAIKU, "subscription"),
        LineCreated("beta", HAIKU, "subscription"),
        OperatorRegistered("ana"),
        LeaseAcquired("printer", "alpha", AT, "2026-10-27T09:00:00Z"),
        RoleDeclared("core", "tmp/worktrees/core"),
        SeatBound("alpha", "core", True),
        SeatBound("beta", "core", True),
        OwnershipChanged("core", "alpha", None, OwnershipCause.DECLARED, ANA, None, AT),
        OwnershipChanged(
            "core", "beta", "alpha", OwnershipCause.OPERATOR, ANA, None, AT
        ),
    ):
        state.apply(event)
    assert state.leases["printer"].line_id == "beta"


def test_a_role_left_vacant_leaves_its_former_owners_leases_with_it() -> None:
    state = owned_role()
    state.apply(
        OwnershipChanged(
            "core", None, "alpha", OwnershipCause.RELINQUISHED, ALPHA_ADDRESS, None, AT
        )
    )
    assert state.leases["gate"].line_id == "alpha"
    state.apply(
        OwnershipChanged("core", "beta", None, OwnershipCause.OPERATOR, ANA, None, AT)
    )
    assert state.leases["gate"].line_id == "alpha"
    assert state.leases["ci"].line_id == "beta"


def test_a_cleared_session_or_rebind_ends_the_leases_in_the_log_itself() -> None:
    """The model ends them as it applies the event, so a replay agrees with the
    daemon that recorded it and no second event is needed."""
    state = owned_role()
    state.apply(LineSessionCleared("alpha"))
    assert set(state.leases) == {"ci"}
    state.apply(LineRebound("beta", "claude-opus-5-5", "subscription"))
    assert state.leases == {}


# The tools.


def tools_of(scheduler: Scheduler, line_id: str) -> dict[str, SdkMcpTool[Any]]:
    return {tool.name: tool for tool in scheduler.own_tools(line_id)}


def call(tool: SdkMcpTool[Any], args: dict[str, object]) -> tuple[str, bool]:
    async def invoke() -> dict[str, Any]:
        return await tool.handler(args)

    result = asyncio.run(invoke())
    return cast(str, result["content"][0]["text"]), result.get("is_error") is True


def test_the_tools_acquire_report_the_holder_and_release(scheduler: Scheduler) -> None:
    alpha, beta = tools_of(scheduler, "alpha"), tools_of(scheduler, "beta")
    text, error = call(alpha[ACQUIRE_LEASE_TOOL_NAME], {"name": "gate"})
    assert not error and "you hold lease gate until 2026-10-26T09:10:00Z" in text
    text, error = call(beta[ACQUIRE_LEASE_TOOL_NAME], {"name": "gate"})
    assert error and "held by line alpha" in text
    text, error = call(beta[RELEASE_LEASE_TOOL_NAME], {"name": "gate"})
    assert error and "holds no lease" in text
    text, error = call(alpha[RELEASE_LEASE_TOOL_NAME], {"name": "gate"})
    assert error is False
    assert text.startswith("released lease gate. Releasing it does not end processes")


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"name": 3}, "name must be text"),
        ({}, "name must be text"),
        ({"name": "gate", "for": "ever"}, "unexpected argument for"),
        ({"name": "deploy"}, "declares no lease"),
    ],
)
def test_the_lease_tools_refuse_bad_arguments(
    scheduler: Scheduler, args: dict[str, object], message: str
) -> None:
    for name in (ACQUIRE_LEASE_TOOL_NAME, RELEASE_LEASE_TOOL_NAME):
        text, error = call(tools_of(scheduler, "alpha")[name], args)
        if name == RELEASE_LEASE_TOOL_NAME and message == "declares no lease":
            message = "holds no lease"
        assert error and message in text
    assert scheduler.leases_of("alpha") == []


def test_status_shows_held_leases(scheduler: Scheduler) -> None:
    scheduler.acquire_lease("beta", "gate")
    summaries = {str(s["line_id"]): s for s in scheduler.line_summaries()}
    assert summaries["alpha"]["leases"] == []
    assert summaries["beta"]["leases"] == [
        {"name": "gate", "expires": "2026-10-26T09:10:00Z"}
    ]


# The daemon applies the table at reload and shows it.


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


def test_a_reload_declares_the_leases_and_settings_show_them(
    paths: ProjectPaths,
) -> None:
    file = config_path(paths.state_dir)
    file.write_text("version = 1\n[leases]\nnames = ['gate']\nttl_seconds = 120\n")
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        target = Daemon(
            paths,
            log,
            "2026-10-26T00:00:00Z",
            scheduler,
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=True,
        )

        def ask(op: str, args: dict[str, object]) -> dict[str, object]:
            line = {"id": "r", "op": op, "v": 1, "args": args}
            response = target.respond((json.dumps(line) + "\n").encode())
            assert response.ok and response.result is not None, response.error_message
            return response.result

        with pytest.raises(LeaseRefused):
            scheduler.acquire_lease("alpha", "gate")
        ask("reload_project", {})
        assert scheduler.acquire_lease("alpha", "gate")
        entries = cast(list[dict[str, object]], ask("settings", {})["project"])
        leases = next(e for e in entries if e["key"] == "leases")
        assert (leases["kind"], leases["value"]) == ("info", "gate, 120 seconds")
        (status,) = cast(list[dict[str, object]], ask("status", {})["lines"])
        assert cast(list[dict[str, object]], status["leases"])[0]["name"] == "gate"
        assert load(file).lease_names == ("gate",)


# Round 14. The lease inventory.


def inventory(scheduler: Scheduler, line_id: str = "beta") -> dict[str, Any]:
    text, error = call(tools_of(scheduler, line_id)[LIST_LEASES_TOOL_NAME], {})
    assert not error, text
    return cast(dict[str, Any], json.loads(text))


def test_the_inventory_lists_every_live_lease_with_its_holder_and_note(
    scheduler: Scheduler,
) -> None:
    alpha = tools_of(scheduler, "alpha")
    text, error = call(
        alpha[ACQUIRE_LEASE_TOOL_NAME], {"name": "gate", "note": "the full gate"}
    )
    assert not error, text
    listed = inventory(scheduler)
    assert listed["leases"] == [
        {
            "name": "gate",
            "holder": "alpha",
            "acquired_at": "2026-10-26T09:00:00Z",
            "expires": "2026-10-26T09:10:00Z",
            "note": "the full gate",
        }
    ]
    assert listed["free"] == ["ci"]
    assert listed["caveat"] == LEASE_PROOF_CAVEAT
    assert inventory(scheduler, "alpha") == listed


def test_a_lease_without_a_note_has_no_note_field(scheduler: Scheduler) -> None:
    scheduler.acquire_lease("alpha", "ci")
    (entry,) = inventory(scheduler)["leases"]
    assert "note" not in entry


def test_an_expired_or_released_lease_leaves_the_inventory(
    scheduler: Scheduler, clock: Clock
) -> None:
    scheduler.acquire_lease("alpha", "gate")
    scheduler.acquire_lease("beta", "ci")
    scheduler.release_lease("beta", "ci")
    assert [e["name"] for e in inventory(scheduler)["leases"]] == ["gate"]
    clock.now += timedelta(seconds=600)
    listed = inventory(scheduler)
    assert listed["leases"] == []
    assert listed["free"] == ["ci", "gate"]


def test_a_released_lease_is_never_offered_as_proof_processes_ended() -> None:
    assert "never shows that processes" in LEASE_PROOF_CAVEAT


@pytest.mark.parametrize(
    ("note", "refusal"),
    [(7, "note must be text"), ("   ", "note is empty"), ("x" * 501, "note")],
)
def test_a_bad_note_is_refused_and_takes_nothing(
    scheduler: Scheduler, note: object, refusal: str
) -> None:
    text, error = call(
        tools_of(scheduler, "alpha")[ACQUIRE_LEASE_TOOL_NAME],
        {"name": "gate", "note": note},
    )
    assert error and refusal in text
    assert inventory(scheduler)["leases"] == []


def test_a_note_holding_a_disclosed_term_is_refused(
    scheduler: Scheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(line_id: str, *fields: tuple[str, str]) -> str | None:
        del line_id
        return f"refused {fields[0][0]}"

    monkeypatch.setattr(scheduler, "refuse_disclosed", refuse)
    text, error = call(
        tools_of(scheduler, "alpha")[ACQUIRE_LEASE_TOOL_NAME],
        {"name": "gate", "note": "a listed term"},
    )
    assert (text, error) == ("refused note", True)
    assert inventory(scheduler)["leases"] == []


def test_the_inventory_takes_no_arguments(scheduler: Scheduler) -> None:
    text, error = call(
        tools_of(scheduler, "alpha")[LIST_LEASES_TOOL_NAME], {"name": "gate"}
    )
    assert error and "name" in text


def test_a_note_passes_with_the_lease_on_a_change_of_owner() -> None:
    state = owned_role()
    note = "the shared device"
    state.apply(LeaseAcquired("bench", "alpha", AT, "2026-10-27T08:00:00Z", note))
    state.apply(
        OwnershipChanged(
            "core",
            "beta",
            "alpha",
            OwnershipCause.OPERATOR,
            ANA,
            None,
            AT,
        )
    )
    assert state.leases["bench"].line_id == "beta"
    assert state.leases["bench"].note == note
