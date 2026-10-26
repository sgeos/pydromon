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
    RELEASE_LEASE_TOOL_NAME,
    LeaseRefused,
    LineEngine,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
START = datetime(2026, 10, 26, 9, 0, 0, tzinfo=UTC)


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


def test_leases_live_in_memory_so_a_restart_drops_them(
    scheduler: Scheduler, clock: Clock
) -> None:
    scheduler.acquire_lease("alpha", "gate")
    log = scheduler._log  # pyright: ignore[reportPrivateUsage]
    restarted = Scheduler(log, no_engine, hold_wakes=True, wall_clock=clock)
    restarted.set_leases(["gate", "ci"], 600)
    assert restarted.leases_of("alpha") == []
    assert restarted.acquire_lease("beta", "gate")


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
    assert (text, error) == ("released lease gate", False)


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
