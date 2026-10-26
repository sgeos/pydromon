"""Rebinding a line to another model and strategy: the ``line_rebound`` event, the
scheduler's checks, the operator-only client operation and the project file."""

import asyncio
import json
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli
from dromon import scheduler as scheduler_module
from dromon.agents import DAEMON_TOOLS
from dromon.configview import Overrides
from dromon.daemon import Daemon, ProjectMismatch, apply_project, check_project
from dromon.driver import DriverError, Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog, read_entries
from dromon.lineconfig import ConfigError, LineConfig
from dromon.model import (
    EventRejected,
    LineAddress,
    LineCreated,
    LineOverridden,
    LineRebound,
    LineSession,
    OperatorAddress,
    State,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ProjectConfig, parse
from dromon.protocol import ErrorCode, Response
from dromon.sandbox import parse_sandbox
from dromon.scheduler import LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
OPUS = "claude-opus-5-5"
LUNA = "gpt-6-luna"


# The event.


def test_a_rebind_replaces_the_binding_and_clears_the_session() -> None:
    state = State()
    state.apply(LineCreated("alpha", HAIKU, "subscription"))
    state.apply(LineSession("alpha", "s-1"))
    state.apply(LineRebound("alpha", OPUS, "api"))
    line = state.lines["alpha"]
    assert (line.engine, line.strategy, line.engine_session) == (OPUS, "api", None)
    with pytest.raises(EventRejected, match="already runs"):
        state.check(LineRebound("alpha", OPUS, "api"))
    with pytest.raises(EventRejected, match="no line beta"):
        state.check(LineRebound("beta", OPUS, "api"))


# The scheduler.


class Recorder:
    def __init__(self, spec: LineSpec) -> None:
        self.spec = spec
        self.session_id = spec.session_id
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def run_turn(self, prompt: str) -> TurnReport:
        del prompt
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        self.session_id = self.session_id or "session-1"
        return TurnReport(
            "success", 1, "ok", (), None, None, self.session_id, None, None
        )


class World:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.log = EventLog(path)
        self.engines: list[Recorder] = []
        self.gate: asyncio.Event | None = None
        self.scheduler = Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            wall_clock=lambda: datetime(2026, 10, 26, tzinfo=UTC),
        )
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line(
            "alpha", HAIKU, Strategy.SUBSCRIPTION, LineConfig(builtin_tools=("Bash",))
        )

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
        del tools
        engine = Recorder(spec)
        engine.gate = self.gate
        self.engines.append(engine)
        return engine

    def turn(self) -> None:
        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            self.scheduler.send(OperatorAddress("ana"), LineAddress("alpha"), "work")
            await asyncio.sleep(0)
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(go())


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    target = World(tmp_path / "events.sqlite")
    yield target
    target.log.close()


def test_a_rebound_line_keeps_its_mail_configuration_and_other_overrides(
    world: World,
) -> None:
    scheduler = world.scheduler
    world.turn()
    scheduler.override_line("alpha", "model", OPUS)
    scheduler.override_line("alpha", "effort", "low")
    scheduler.set_leases(["gate"], 600)
    scheduler.acquire_lease("alpha", "gate")
    messages_before = dict(world.log.state.messages)
    scheduler.rebind_line("alpha", LUNA, Strategy.SUBSCRIPTION)
    line = world.log.state.lines["alpha"]
    assert (line.engine, line.strategy, line.engine_session) == (
        LUNA,
        "subscription",
        None,
    )
    assert scheduler.line_overrides("alpha") == {"effort": "low"}
    assert scheduler.baseline_config("alpha") == LineConfig(builtin_tools=("Bash",))
    assert world.log.state.messages == messages_before
    assert scheduler.leases_of("alpha") == []
    assert scheduler.model_of("alpha") == LUNA
    events = [entry.event for entry in read_entries(world.path)]
    assert isinstance(events[-2], LineOverridden) and isinstance(
        events[-1], LineRebound
    )
    world.turn()
    rebuilt = world.engines[-1].spec
    assert (rebuilt.model, rebuilt.session_id) == (LUNA, None)


def test_a_rebind_without_a_model_override_records_only_the_rebind(
    world: World,
) -> None:
    before = world.log.last_seq
    world.scheduler.rebind_line("alpha", OPUS, Strategy.SUBSCRIPTION)
    events = [entry.event for entry in read_entries(world.path)]
    assert world.log.last_seq == before + 1
    assert events[-1] == LineRebound("alpha", OPUS, "subscription")


def test_a_rebind_to_the_same_binding_or_an_unknown_model_is_refused(
    world: World,
) -> None:
    before = world.log.last_seq
    with pytest.raises(EventRejected, match="would change nothing"):
        world.scheduler.rebind_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
    with pytest.raises(DriverError, match="claude-unknown-9"):
        world.scheduler.rebind_line("alpha", "claude-unknown-9", Strategy.SUBSCRIPTION)
    assert world.log.last_seq == before
    world.scheduler.rebind_line("alpha", HAIKU, Strategy.API)
    assert world.log.state.lines["alpha"].strategy == "api"


def test_a_rebind_to_an_engine_without_an_adapter_is_refused(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler_module, "MODELS", (HAIKU,))
    with pytest.raises(DriverError, match="no adapter yet"):
        world.scheduler.rebind_line("alpha", OPUS, Strategy.SUBSCRIPTION)


def test_a_rebind_the_new_engine_cannot_run_is_refused(world: World) -> None:
    scheduler = world.scheduler
    scheduler.configure_line("alpha", LineConfig(builtin_tools=("Read",)))
    before = world.log.last_seq
    with pytest.raises(ConfigError, match="Codex lines support only"):
        scheduler.rebind_line("alpha", LUNA, Strategy.SUBSCRIPTION)
    scheduler.configure_line("alpha", LineConfig(builtin_tools=("Bash",)))
    scheduler.override_line("alpha", "builtin_tools", ["Grep"])
    before = world.log.last_seq
    with pytest.raises(ConfigError, match="Codex lines support only"):
        scheduler.rebind_line("alpha", LUNA, Strategy.SUBSCRIPTION)
    assert world.log.last_seq == before


def test_a_rebind_is_refused_while_the_line_runs_a_turn(world: World) -> None:
    world.gate = asyncio.Event()
    refused: list[str] = []

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        world.scheduler.send(OperatorAddress("ana"), LineAddress("alpha"), "work")
        while not world.engines or not world.engines[0].started.is_set():
            await asyncio.sleep(0)
        with pytest.raises(EventRejected, match="Interrupt it first") as caught:
            world.scheduler.rebind_line("alpha", OPUS, Strategy.SUBSCRIPTION)
        refused.append(str(caught.value))
        assert world.gate is not None
        world.gate.set()
        await world.scheduler.idle()
        stop.set()
        await loop

    asyncio.run(go())
    assert refused and world.log.state.lines["alpha"].engine == HAIKU


def test_an_unknown_line_cannot_be_rebound(world: World) -> None:
    with pytest.raises(EventRejected, match="no line beta"):
        world.scheduler.rebind_line("beta", OPUS, Strategy.SUBSCRIPTION)


# The project file compares with the current binding.

FILE_HAIKU = "version = 1\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\n"
FILE_OPUS = "version = 1\n[lines.alpha]\nmodel = 'claude-opus-5-5'\n"


def test_the_file_check_compares_with_the_current_binding(world: World) -> None:
    check_project(world.log, parse(FILE_HAIKU))
    world.scheduler.rebind_line("alpha", OPUS, Strategy.SUBSCRIPTION)
    check_project(world.log, parse(FILE_OPUS))
    apply_project(world.scheduler, world.log, parse(FILE_OPUS))
    with pytest.raises(ProjectMismatch, match="rebind the line"):
        check_project(world.log, parse(FILE_HAIKU))


# The client operation, which only an operator has.


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


@contextmanager
def attached(paths: ProjectPaths, project: ProjectConfig) -> Generator[Daemon]:
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line(
            "alpha", HAIKU, Strategy.SUBSCRIPTION, LineConfig(builtin_tools=("Bash",))
        )
        yield Daemon(
            paths,
            log,
            "2026-10-26T00:00:00Z",
            scheduler,
            project=project,
            command_line=Overrides(),
            project_file_present=False,
        )


def ask(target: Daemon, args: dict[str, object]) -> Response:
    line = {"id": "r", "op": "rebind_line", "v": 1, "args": args}
    return target.respond((json.dumps(line) + "\n").encode())


def rebind_args(model: str = OPUS, strategy: str = "subscription") -> dict[str, object]:
    return {"line_id": "alpha", "model": model, "strategy": strategy}


def test_rebind_line_moves_the_line_for_an_operator(paths: ProjectPaths) -> None:
    with attached(paths, ProjectConfig()) as target:
        response = ask(target, rebind_args(LUNA))
        assert response.ok, response.error_message
        assert response.result == {
            "line_id": "alpha",
            "model": LUNA,
            "strategy": "subscription",
        }
        (status,) = cast(list[dict[str, object]], target.status()["lines"])
        assert status["engine"] == LUNA


@pytest.mark.parametrize(
    ("project", "args", "code", "message"),
    [
        (
            ProjectConfig(),
            {"line_id": "alpha", "model": OPUS},
            ErrorCode.BAD_REQUEST,
            "",
        ),
        (ProjectConfig(), rebind_args(OPUS, "barter"), ErrorCode.REJECTED, "barter"),
        (ProjectConfig(), rebind_args(HAIKU), ErrorCode.REJECTED, "change nothing"),
        (
            ProjectConfig(),
            {"line_id": "beta", "model": OPUS, "strategy": "subscription"},
            ErrorCode.REJECTED,
            "no line beta",
        ),
        (
            ProjectConfig(sandbox=parse_sandbox({"enabled": True}, operator=False)),
            rebind_args(LUNA),
            ErrorCode.REJECTED,
            "own sandbox",
        ),
        (
            ProjectConfig(require_sandbox=True),
            rebind_args(OPUS),
            ErrorCode.REJECTED,
            "require_sandbox",
        ),
    ],
)
def test_rebind_line_refusals_change_nothing(
    paths: ProjectPaths,
    project: ProjectConfig,
    args: dict[str, object],
    code: ErrorCode,
    message: str,
) -> None:
    with attached(paths, project) as target:
        before = target.log.last_seq
        response = ask(target, args)
        assert (response.ok, response.error_code) == (False, code)
        assert message in (response.error_message or "")
        assert target.log.last_seq == before


def test_external_agents_are_not_offered_a_rebind() -> None:
    names = {spec.name for spec in DAEMON_TOOLS}
    assert not any("rebind" in name for name in names)


def test_dromon_rebind_sends_the_operation(
    tmp_path: Path, short_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, dict[str, object]]] = []

    def capture(
        paths: ProjectPaths, command: str, operation: object, args: dict[str, object]
    ) -> int:
        del paths, operation
        sent.append((command, args))
        return 0

    monkeypatch.setattr(cli, "_request", capture)
    base = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime)]
    argv = [*base, "rebind", "alpha", "--model", OPUS, "--strategy", "api"]
    assert cli.main(argv) == 0
    assert sent == [("rebind", {"line_id": "alpha", "model": OPUS, "strategy": "api"})]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["rebind", "alpha", "--model", OPUS])
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["rebind", "alpha", "--model", "nope", "--strategy", "api"]
        )


def test_the_settings_view_labels_a_rebound_models_source(paths: ProjectPaths) -> None:
    with attached(paths, ProjectConfig()) as target:

        def model_entry() -> dict[str, object]:
            line = {"id": "r", "op": "settings", "v": 1, "args": {"line_id": "alpha"}}
            response = target.respond((json.dumps(line) + "\n").encode())
            assert response.ok and response.result is not None
            entries = cast(list[dict[str, object]], response.result["line"])
            return next(e for e in entries if e["key"] == "model")

        assert (model_entry()["value"], model_entry()["source"]) == (HAIKU, "created")
        assert ask(target, rebind_args(OPUS)).ok
        assert (model_entry()["value"], model_entry()["source"]) == (OPUS, "rebound")
        target.scheduler.override_line("alpha", "model", HAIKU)
        assert model_entry()["source"] == "override"
        assert target.log.state.lines["alpha"].rebound is True
