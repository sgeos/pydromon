"""Restart to upgrade, driven by scripted engines and without sockets.

A restart is tested in three parts. The scheduler drains, ends turns at a safe point
and leaves notes. The daemon's ``restart`` request starts the watch that stops it for
a successor. ``dromon restart`` polls until a daemon with another process id answers.
The successor is a new ``Scheduler`` on the reopened event log, as the started daemon
builds one, since no test here may start a real daemon.
"""

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli, client, daemon, eventlog, exits, fleet, registry, restart
from dromon.registry import REGISTRY_DIR_ENV
from dromon.activity import ActivityKind
from dromon.configview import Overrides
from dromon.daemon import Daemon, build_parser, start_successor, successor_args
from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog, read_entries, replay
from dromon.model import (
    LineAddress,
    OperatorAddress,
    OperatorRegistered,
    RestartNoted,
)
from dromon.paths import ProjectPaths, ensure_private_dir, project_paths
from dromon.projectconfig import config_path
from dromon.protocol import (
    ErrorCode,
    Operation,
    Request,
    Response,
    decode_response,
    encode_request,
    encode_response,
)
from dromon.scheduler import LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
START = datetime(2026, 10, 27, 9, 0, 0, tzinfo=UTC)


class Scripted:
    """A line engine that waits at a gate, if one is set, then reaches
    ``safe_points`` safe points, at each of which it asks to be redirected, as a
    line's ``SteeredDriver`` does at each of its safe points, then waits at a
    second gate, if one is set, before it reports."""

    def __init__(self, spec: LineSpec, world: "World") -> None:
        self.spec = spec
        self.world = world
        self.session_id = spec.session_id

    async def run_turn(self, prompt: str) -> TurnReport:
        line_id = self.spec.line_id
        self.world.prompts.setdefault(line_id, []).append(prompt)
        self.world.sessions.setdefault(line_id, []).append(self.spec.session_id)
        gates = self.world.gates.get(line_id, [])
        if gates:
            await gates.pop(0).wait()
        reason: str | None = None
        for _ in range(self.world.safe_points):
            reason = self.spec.redirect()
            if reason is not None:
                break
        self.world.reasons.setdefault(line_id, []).append(reason)
        after = self.world.after.get(line_id, [])
        if after:
            await after.pop(0).wait()
        subtype = "success" if reason is None else "error_during_execution"
        return TurnReport(subtype, 1, "ok", (), reason, None, "session-1", None, None)


class World:
    """A scheduler over a real event log, which ``restart`` replaces with a
    successor on the reopened log."""

    def __init__(self, path: Path, lines: Sequence[str]) -> None:
        self.path = path
        self.now = START
        self.monotonic = 0.0
        self.safe_points = 1
        self.gates: dict[str, list[asyncio.Event]] = {}
        self.after: dict[str, list[asyncio.Event]] = {}
        self.prompts: dict[str, list[str]] = {}
        self.sessions: dict[str, list[str | None]] = {}
        self.reasons: dict[str, list[str | None]] = {}
        self.log = EventLog(path)
        self.scheduler = self._scheduler()
        self.scheduler.ensure_operator("ana")
        for line_id in lines:
            self.scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)

    def _scheduler(self) -> Scheduler:
        return Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            clock=lambda: self.monotonic,
            wall_clock=lambda: self.now,
        )

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Scripted:
        del tools
        return Scripted(spec, self)

    def restart(self) -> None:
        """Close the log and start a successor on it, as a restarted daemon does."""
        self.log.close()
        self.log = EventLog(self.path)
        self.scheduler = self._scheduler()
        self.scheduler.recover()

    def endings(self, line_id: str) -> list[str]:
        return [
            entry.text
            for entry in self.scheduler.activity.after(line_id, 0)
            if entry.kind is ActivityKind.TURN_ENDED
        ]

    def notices(self, line_id: str) -> list[str]:
        return [
            entry.text
            for entry in self.scheduler.activity.after(line_id, 0)
            if entry.kind is ActivityKind.NOTICE
        ]


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    target = World(tmp_path / "events.sqlite", ["alpha", "beta"])
    yield target
    target.log.close()


async def until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until ``condition`` holds, within a bounded number of
    passes, so a test that would hang fails instead."""
    for _ in range(4000):
        if condition():
            return
        await asyncio.sleep(0.0005)
    raise AssertionError("the condition never held")


def run(target: World, act: Callable[[], Coroutine[Any, Any, None]]) -> None:
    async def settle() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(target.scheduler.run(stop))
        await asyncio.sleep(0)
        await act()
        await target.scheduler.idle()
        stop.set()
        await loop

    asyncio.run(settle())


def restart_notes(target: World) -> list[RestartNoted]:
    return [
        entry.event
        for entry in read_entries(target.path)
        if isinstance(entry.event, RestartNoted)
    ]


async def nothing() -> None:
    pass


def counter(tasks: dict[str, int]) -> Callable[[str], int]:
    """A stand-in for ``Scheduler.kept_tasks`` that reads ``tasks``."""

    def count(line_id: str) -> int:
        return tasks.get(line_id, 0)

    return count


# The scheduler's drain.


def test_interrupt_ends_a_turn_at_a_safe_point_and_the_successor_resumes_it(
    world: World,
) -> None:
    gate = asyncio.Event()
    world.gates["alpha"] = [gate]

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "work on it")
        await until(lambda: world.scheduler.running_lines() == ["alpha"])
        running = world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 60)
        assert running == ["alpha"]
        assert world.scheduler.restart_progress() is restart.Outcome.WAITING
        gate.set()
        await until(lambda: not world.scheduler.running_lines())
        assert world.scheduler.restart_progress() is restart.Outcome.READY

    run(world, act)
    assert world.reasons["alpha"] == [restart.REASON]
    assert world.endings("alpha")[-1] == (
        "error_during_execution, ended at a safe point for a daemon restart"
    )
    assert any("asked its turn to end" in text for text in world.notices("alpha"))
    (note,) = restart_notes(world)
    assert note.line_id == "alpha"
    assert note.body == restart.NOTE.format(operator="ana")
    # The draining daemon starts no turn for the note.
    assert len(world.prompts["alpha"]) == 1

    world.restart()
    run(world, nothing)
    assert len(world.prompts["alpha"]) == 2
    assert note.message_id in world.prompts["alpha"][1]
    assert "daemon restart" in world.prompts["alpha"][1]
    assert world.sessions["alpha"] == [None, "session-1"]


def test_wait_lets_a_turn_end_by_itself_and_leaves_no_note(world: World) -> None:
    gate = asyncio.Event()
    world.gates["alpha"] = [gate]

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "work on it")
        await until(lambda: world.scheduler.running_lines() == ["alpha"])
        world.scheduler.begin_restart(restart.Mode.WAIT, "ana", 60)
        gate.set()
        await until(lambda: not world.scheduler.running_lines())
        assert world.scheduler.restart_progress() is restart.Outcome.READY

    run(world, act)
    assert world.reasons["alpha"] == [None]
    assert world.endings("alpha") == ["success"]
    assert restart_notes(world) == []


def test_a_drain_starts_no_turn_and_fires_no_wake_until_it_is_abandoned(
    world: World,
) -> None:
    message_id, _ = world.scheduler.schedule_wake("beta", 60, "look again")
    world.now = START + timedelta(seconds=120)
    world.scheduler.begin_restart(restart.Mode.WAIT, "ana", 60)

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "hello")

    run(world, act)
    assert world.prompts == {}
    assert world.scheduler.seconds_until_next_wake() is None
    assert world.log.state.scheduled["beta"].message_id == message_id
    assert world.scheduler.restart_progress() is restart.Outcome.READY

    async def abandon() -> None:
        world.scheduler.abandon_restart("a test abandoned it")

    run(world, abandon)
    assert len(world.prompts["alpha"]) == 1
    assert "your scheduled wake, due" in world.prompts["beta"][0]
    assert world.scheduler.restart_status() == {
        "state": "abandoned",
        "mode": "wait",
        "operator": "ana",
        "reason": "a test abandoned it",
    }


def test_a_turn_with_no_safe_point_by_the_deadline_abandons_the_restart(
    world: World,
) -> None:
    gate = asyncio.Event()
    world.gates["alpha"] = [gate]
    world.safe_points = 0

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "a long tool call")
        await until(lambda: world.scheduler.running_lines() == ["alpha"])
        world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 30)
        status = world.scheduler.restart_status()
        assert status is not None
        assert (status["state"], status["seconds_left"]) == ("waiting", 30)
        assert status["running"] == ["alpha"]
        world.monotonic = 29.0
        assert world.scheduler.restart_progress() is restart.Outcome.WAITING
        world.monotonic = 30.0
        assert world.scheduler.restart_progress() is restart.Outcome.ABANDONED
        # The turn is not cut. It runs on and ends by itself.
        assert world.scheduler.running_lines() == ["alpha"]
        gate.set()

    run(world, act)
    assert world.endings("alpha") == ["success"]
    status = world.scheduler.restart_status()
    assert status is not None and status["state"] == "abandoned"
    reason = cast(str, status["reason"])
    assert "lines alpha did not end within 30 seconds" in reason
    assert "could not be honoured" in reason
    assert any("was abandoned" in text for text in world.notices("alpha"))
    assert restart_notes(world) == []


def test_a_restart_abandoned_before_the_safe_point_asks_nothing(
    world: World,
) -> None:
    gate = asyncio.Event()
    world.gates["alpha"] = [gate]

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "work on it")
        await until(lambda: world.scheduler.running_lines() == ["alpha"])
        world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 60)
        world.scheduler.abandon_restart("a test abandoned it")
        gate.set()

    run(world, act)
    assert world.reasons["alpha"] == [None]
    assert restart_notes(world) == []


def test_a_turn_ended_for_a_restart_then_abandoned_is_told_so_and_woken(
    world: World,
) -> None:
    gate = asyncio.Event()
    after = asyncio.Event()
    world.gates["alpha"] = [gate]
    world.after["alpha"] = [after]

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "work on it")
        await until(lambda: world.scheduler.running_lines() == ["alpha"])
        world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 60)
        gate.set()
        await until(lambda: "alpha" in world.reasons)
        # The engine was asked to end, and the restart is abandoned before the
        # turn's report arrives.
        world.scheduler.abandon_restart("a test abandoned it")
        after.set()
        await until(lambda: not world.scheduler.running_lines())
        world.monotonic = 10_000.0

    run(world, act)
    assert world.reasons["alpha"][0] == restart.REASON
    (note,) = restart_notes(world)
    assert note.body == restart.ABANDONED_NOTE.format(operator="ana")
    assert len(world.prompts["alpha"]) == 2
    assert note.message_id in world.prompts["alpha"][1]


def test_a_second_restart_is_refused_while_one_drains(world: World) -> None:
    world.scheduler.begin_restart(restart.Mode.WAIT, "ana", 60)
    with pytest.raises(restart.RestartRefused, match="draining already"):
        world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 60)


def test_a_restart_refuses_to_stop_background_tasks_unless_asked(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = {"alpha": 2}
    monkeypatch.setattr(world.scheduler, "kept_tasks", counter(tasks))
    with pytest.raises(restart.RestartRefused, match="2 background tasks") as refused:
        world.scheduler.begin_restart(restart.Mode.WAIT, "ana", 60)
    assert "lines alpha" in str(refused.value)
    assert world.scheduler.restart_status() is None
    world.scheduler.begin_restart(restart.Mode.WAIT, "ana", 60, end_tasks=True)
    assert world.scheduler.restart_progress() is restart.Outcome.READY


def test_tasks_that_begin_during_the_drain_abandon_the_restart(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks: dict[str, int] = {}
    monkeypatch.setattr(world.scheduler, "kept_tasks", counter(tasks))
    world.scheduler.begin_restart(restart.Mode.WAIT, "ana", 60)
    tasks["beta"] = 1
    assert world.scheduler.restart_progress() is restart.Outcome.ABANDONED
    status = world.scheduler.restart_status()
    assert status is not None and "lines beta" in cast(str, status["reason"])


def test_a_hibernated_line_is_interrupted_by_a_restart_and_stays_hibernated_after(
    world: World,
) -> None:
    gate = asyncio.Event()
    world.gates["alpha"] = [gate]
    wake_id, _ = world.scheduler.schedule_wake("beta", 600, "later")
    world.scheduler.hibernate("beta")

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "work on it")
        await until(lambda: world.scheduler.running_lines() == ["alpha"])
        world.scheduler.hibernate("alpha")
        world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 60)
        gate.set()

    run(world, act)
    assert world.reasons["alpha"] == [restart.REASON]
    world.restart()
    run(world, nothing)
    state = world.log.state
    assert state.lines["alpha"].hibernated and state.lines["beta"].hibernated
    assert state.scheduled["beta"].message_id == wake_id
    # The note waits for the awaken, like any mail of a hibernated line.
    assert len(world.prompts["alpha"]) == 1

    async def awaken() -> None:
        world.scheduler.awaken("alpha")

    run(world, awaken)
    assert len(world.prompts["alpha"]) == 2
    assert "daemon restart" in world.prompts["alpha"][1]


def test_priority_mail_takes_the_safe_point_before_a_restart(world: World) -> None:
    gate = asyncio.Event()
    world.gates["alpha"] = [gate]

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "work on it")
        await until(lambda: world.scheduler.running_lines() == ["alpha"])
        world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 60)
        world.scheduler.send(ANA, ALPHA, "stop and look", priority=True)
        gate.set()

    run(world, act)
    (reason,) = world.reasons["alpha"]
    assert reason is not None and reason.startswith("priority mail")
    assert restart_notes(world) == []


# Pure parts.


@pytest.mark.parametrize(
    "value", [0, -1, 3601, 1.5, 30.0, float("nan"), float("inf"), True, "9"]
)
def test_check_wait_refuses_what_is_not_a_bounded_wait(value: object) -> None:
    with pytest.raises(restart.RestartRefused):
        restart.check_wait(value)


def test_check_wait_accepts_the_bounds_and_polls_pass_the_deadline() -> None:
    assert restart.check_wait(restart.MAX_WAIT_SECONDS) == restart.MAX_WAIT_SECONDS
    assert restart.check_wait(1) == 1
    for wait in (1, restart.DEFAULT_WAIT_SECONDS, restart.MAX_WAIT_SECONDS):
        assert restart.polls(wait) * restart.POLL_SECONDS > wait


def test_successor_args_read_back_as_the_same_command_line() -> None:
    given = Overrides(
        operator="ana",
        tick_seconds=0.1,
        max_autonomous_turns=7,
        max_cost_usd=2.5,
        cooldown_seconds=1.0 / 3.0,
    )
    parsed = build_parser().parse_args(successor_args(given, True))
    assert parsed.hold_wakes is True
    assert (
        Overrides(
            operator=parsed.operator,
            tick_seconds=parsed.tick_seconds,
            max_autonomous_turns=parsed.max_autonomous_turns,
            max_cost_usd=parsed.max_cost_usd,
            cooldown_seconds=parsed.cooldown_seconds,
        )
        == given
    )
    assert successor_args(Overrides(), False) == []


def test_start_successor_reports_a_successor_that_does_not_start(
    paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    started: list[Sequence[str]] = []

    def start(paths: ProjectPaths, args: Sequence[str]) -> int:
        started.append(args)
        return 4242

    monkeypatch.setattr(daemon.client, "start", start)
    assert start_successor(paths, ["--hold-wakes"]) == exits.OK
    assert started == [["--hold-wakes"]]
    assert "the successor is pid 4242" in capsys.readouterr().err

    def fail(paths: ProjectPaths, args: Sequence[str]) -> int:
        raise client.DaemonFailed("the daemon exited with status 1")

    monkeypatch.setattr(daemon.client, "start", fail)
    assert start_successor(paths, []) == exits.FAILURE
    error = capsys.readouterr().err
    assert "did not start: the daemon exited with status 1" in error
    assert "No daemon runs" in error


# The daemon's request.


def respond(target: Daemon, op: str, args: dict[str, object]) -> Response:
    line = (json.dumps({"id": "r", "op": op, "v": 1, "args": args}) + "\n").encode()
    return target.respond(line)


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    """The project's paths. Their runtime directory is the shared short one, because
    ``project_paths`` refuses a socket path over macOS's limit, which a runtime
    directory under pytest's own temporary path can pass."""
    return project_paths(tmp_path, short_runtime)


def make_daemon(target: World, paths: ProjectPaths) -> Daemon:
    return Daemon(
        paths,
        target.log,
        "2026-10-27T09:00:00Z",
        target.scheduler,
        command_line=Overrides(tick_seconds=0.5),
        hold_wakes=True,
    )


def test_restart_drains_then_stops_for_a_successor_with_the_same_options(
    world: World, paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restart, "POLL_SECONDS", 1e-6)
    target = make_daemon(world, paths)
    gate = asyncio.Event()
    world.gates["alpha"] = [gate]

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        world.scheduler.send(ANA, ALPHA, "work on it")
        await until(lambda: world.scheduler.running_lines() == ["alpha"])
        response = respond(target, "restart", {"operator": "ana", "mode": "interrupt"})
        assert response.ok and response.result is not None
        assert response.result["running"] == ["alpha"]
        assert response.result["mode"] == "interrupt"
        assert response.result["wait_seconds"] == restart.DEFAULT_WAIT_SECONDS
        status = target.status()
        assert cast(dict[str, object], status["restart"])["state"] == "waiting"
        assert "daemon" in status
        gate.set()
        await until(target.stop_requested.is_set)
        stop.set()
        await loop

    asyncio.run(go())
    assert target.successor == ["--tick-seconds", "0.5", "--hold-wakes"]
    assert len(restart_notes(world)) == 1


def test_an_abandoned_restart_does_not_stop_the_daemon(
    world: World, paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restart, "POLL_SECONDS", 1e-6)
    target = make_daemon(world, paths)
    gate = asyncio.Event()
    world.gates["alpha"] = [gate]
    world.safe_points = 0

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        world.scheduler.send(ANA, ALPHA, "a long tool call")
        await until(lambda: world.scheduler.running_lines() == ["alpha"])
        response = respond(target, "restart", {"operator": "ana", "wait_seconds": 5})
        assert response.ok
        world.monotonic = 5.0
        await until(lambda: not target._watches)  # pyright: ignore[reportPrivateUsage]
        gate.set()
        await world.scheduler.idle()
        stop.set()
        await loop

    asyncio.run(go())
    assert not target.stop_requested.is_set()
    assert target.successor is None
    status = target.status()
    assert cast(dict[str, object], status["restart"])["state"] == "abandoned"


@pytest.mark.parametrize(
    ("args", "code", "text"),
    [
        ({}, ErrorCode.BAD_REQUEST, "operator"),
        (
            {"operator": "ana", "mode": "now"},
            ErrorCode.BAD_REQUEST,
            "wait or interrupt",
        ),
        ({"operator": "ana", "wait_seconds": 0}, ErrorCode.BAD_REQUEST, "from 1"),
        ({"operator": "ana", "wait_seconds": "9"}, ErrorCode.BAD_REQUEST, "number"),
        ({"operator": "ana", "end_tasks": "yes"}, ErrorCode.BAD_REQUEST, "boolean"),
        ({"operator": "ana", "force": True}, ErrorCode.BAD_REQUEST, "no force"),
        ({"operator": "@ana"}, ErrorCode.REJECTED, ""),
    ],
)
def test_a_bad_restart_is_refused_and_changes_nothing(
    world: World,
    paths: ProjectPaths,
    args: dict[str, object],
    code: ErrorCode,
    text: str,
) -> None:
    target = make_daemon(world, paths)

    async def go() -> Response:
        return respond(target, "restart", args)

    response = asyncio.run(go())
    assert not response.ok and response.error_code is code
    assert text in (response.error_message or "")
    assert world.scheduler.restart_status() is None


def test_a_restart_while_one_drains_is_rejected(
    world: World, paths: ProjectPaths
) -> None:
    target = make_daemon(world, paths)

    async def go() -> tuple[Response, Response]:
        first = respond(target, "restart", {"operator": "ana"})
        second = respond(target, "restart", {"operator": "ana"})
        world.scheduler.abandon_restart("the test is over")
        return first, second

    first, second = asyncio.run(go())
    assert first.ok
    assert second.error_code is ErrorCode.REJECTED
    assert "draining already" in (second.error_message or "")


# dromon restart.


def namespace(**given: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "interrupt": False,
        "wait_seconds": 30,
        "end_tasks": False,
        "operator": "ana",
    }
    values.update(given)
    return argparse.Namespace(**values)


class Daemons:
    """Scripted answers for ``cli.call``: the old daemon's status, its answer to
    ``restart``, then the statuses the polls read."""

    def __init__(self, restarted: dict[str, object], polls: list[object]) -> None:
        self.restarted = restarted
        self.polls = polls
        self.sent: list[tuple[Operation, dict[str, object]]] = []
        self.status_calls = 0

    def __call__(
        self,
        socket: Path,
        operation: Operation,
        args: dict[str, object] | None = None,
        *,
        timeout_seconds: float = client.REQUEST_TIMEOUT_SECONDS,
    ) -> Response:
        del socket, timeout_seconds
        self.sent.append((operation, dict(args or {})))
        if operation is Operation.RESTART:
            return Response("r", ok=True, result=self.restarted)
        self.status_calls += 1
        if self.status_calls == 1:
            return Response("r", ok=True, result=status(1, "a" * 40))
        found = self.polls.pop(0) if self.polls else status(1, "a" * 40)
        if isinstance(found, Exception):
            raise found
        return Response("r", ok=True, result=cast(dict[str, object], found))


def status(
    pid: int, commit: str, state: Mapping[str, object] | None = None
) -> dict[str, object]:
    return {"pid": pid, "daemon": {"commit": commit}, "restart": state}


def test_the_restart_report_names_the_checkout_and_warns_when_it_differs() -> None:
    """Round 14, item 6. The loaded commit and the checkout's, and a warning when
    they differ, as when the checkout moved while the successor started."""
    before = status(1, "a" * 40)
    same = {"pid": 2, "daemon": {"commit": "b" * 40, "checkout_commit": "b" * 40}}
    moved = {"pid": 2, "daemon": {"commit": "b" * 40, "checkout_commit": "c" * 40}}
    said = cli._restarted(before, same)  # pyright: ignore[reportPrivateUsage]
    assert f"Its checkout is at {'b' * 40}" in said
    assert "Warning" not in said
    said = cli._restarted(before, moved)  # pyright: ignore[reportPrivateUsage]
    assert f"Its checkout is at {'c' * 40}" in said
    assert "Warning: the loaded commit differs from the checkout's" in said
    said = cli._restarted(before, status(2, "b" * 40))  # pyright: ignore[reportPrivateUsage]
    assert "Its checkout is at unknown" in said and "Warning" not in said


def no_sleep(seconds: float) -> None:
    del seconds


@pytest.fixture
def cli_paths(paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch) -> ProjectPaths:
    monkeypatch.setattr(cli, "RESTART_POLL_SECONDS", 0.0)
    monkeypatch.setattr(cli, "_preflight", passes)
    monkeypatch.setattr(cli, "_ask_start_context", no_start_context)
    return paths


def no_start_context(paths: ProjectPaths, command: str) -> None:
    """A daemon that gives no start context, so these tests send what they did."""
    del paths, command


def passes(
    argv: Sequence[str],
    runtime_dir: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> None:
    """A preflight that passes, for the tests of what follows it."""
    del argv, runtime_dir, environment


def test_dromon_restart_waits_for_a_new_pid_and_names_both_commits(
    cli_paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemons = Daemons(
        {"restarting": True, "running": ["alpha"], "pid": 1},
        [
            status(1, "a" * 40, {"state": "waiting"}),
            client.DaemonNotRunning("gone"),
            status(2, "b" * 40),
        ],
    )
    monkeypatch.setattr(cli, "call", daemons)
    code = cli._restart(cli_paths, namespace(interrupt=True))  # pyright: ignore[reportPrivateUsage]
    assert code == exits.OK
    (operation, args) = daemons.sent[1]
    assert operation is Operation.RESTART
    assert args == {"operator": "ana", "mode": "interrupt", "wait_seconds": 30}
    out = capsys.readouterr().out
    assert "draining, with 1 turns running, for at most 30 seconds" in out
    assert f"restarted, pid 2, at commit {'b' * 40}, which was {'a' * 40}" in out


def test_dromon_restart_says_when_the_commit_did_not_change(
    cli_paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    daemons = Daemons({"restarting": True, "running": []}, [status(2, "a" * 40)])
    monkeypatch.setattr(cli, "call", daemons)
    code = cli._restart(cli_paths, namespace(end_tasks=True))  # pyright: ignore[reportPrivateUsage]
    assert code == exits.OK
    assert daemons.sent[1][1]["end_tasks"] is True
    assert daemons.sent[1][1]["mode"] == "wait"
    assert "the same commit as before" in capsys.readouterr().out


def test_dromon_restart_reports_an_abandoned_restart(
    cli_paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    abandoned = {"state": "abandoned", "reason": "the turns of lines alpha did not end"}
    daemons = Daemons(
        {"restarting": True, "running": ["alpha"]}, [status(1, "a" * 40, abandoned)]
    )
    monkeypatch.setattr(cli, "call", daemons)
    code = cli._restart(cli_paths, namespace())  # pyright: ignore[reportPrivateUsage]
    assert code == exits.FAILURE
    err = capsys.readouterr().err
    assert "abandoned, so the daemon runs on as before" in err
    assert "lines alpha did not end" in err


def test_dromon_restart_gives_up_once_its_bound_passes(
    cli_paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli, "RESTART_POLL_SECONDS", 1000.0)
    monkeypatch.setattr(cli.time, "sleep", no_sleep)
    daemons = Daemons({"restarting": True, "running": []}, [])
    monkeypatch.setattr(cli, "call", daemons)
    code = cli._restart(cli_paths, namespace(wait_seconds=1))  # pyright: ignore[reportPrivateUsage]
    assert code == exits.FAILURE
    assert "no restarted daemon answered" in capsys.readouterr().err
    assert daemons.status_calls >= 2


def test_dromon_restart_refused_by_the_daemon_is_a_failure(
    cli_paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refuse(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        if operation is Operation.STATUS:
            return Response("r", ok=True, result=status(1, "a" * 40))
        return Response(
            "r",
            ok=False,
            error_code=ErrorCode.REJECTED,
            error_message="a restart is draining already",
        )

    monkeypatch.setattr(cli, "call", refuse)
    assert cli._restart(cli_paths, namespace()) == exits.FAILURE  # pyright: ignore[reportPrivateUsage]
    assert "dromon restart: a restart is draining already" in capsys.readouterr().err


def test_dromon_restart_without_a_daemon_reports_not_running(
    cli_paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    def absent(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        raise client.DaemonNotRunning("no socket")

    monkeypatch.setattr(cli, "call", absent)
    assert cli._restart(cli_paths, namespace()) == exits.NOT_RUNNING  # pyright: ignore[reportPrivateUsage]


def test_dromon_restart_stops_nothing_when_the_installed_code_does_not_start(
    cli_paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    sent: list[Operation] = []
    checked: list[list[str]] = []

    def record(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        sent.append(operation)
        if operation is not Operation.STATUS:
            raise AssertionError("only the status may be asked for")
        found = status(1, "a" * 40)
        found["options"] = ["--hold-wakes"]
        return Response("r", ok=True, result=found)

    def refuse(
        argv: Sequence[str],
        runtime_dir: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> str:
        del environment
        assert runtime_dir == cli_paths.runtime_dir
        checked.append(list(argv))
        return "status 1: SyntaxError"

    monkeypatch.setattr(cli, "call", record)
    monkeypatch.setattr(cli, "_preflight", refuse)
    assert cli._restart(cli_paths, namespace()) == exits.FAILURE  # pyright: ignore[reportPrivateUsage]
    assert sent == [Operation.STATUS]
    assert checked and checked[0][-2:] == ["--hold-wakes", "--check"]
    assert "was not stopped: status 1: SyntaxError" in capsys.readouterr().err


def test_the_preflight_reports_a_failing_and_a_passing_command() -> None:
    failing = (
        sys.executable,
        "-c",
        "import sys; print('broken', file=sys.stderr); sys.exit(3)",
    )
    assert cli._preflight(failing) == "status 3: broken"  # pyright: ignore[reportPrivateUsage]
    assert cli._preflight((sys.executable, "-c", "pass")) is None  # pyright: ignore[reportPrivateUsage]
    missing = cli._preflight(("/nonexistent/python",))  # pyright: ignore[reportPrivateUsage]
    assert missing is not None


def test_the_restart_subcommand_parses_its_options() -> None:
    parsed = cli.build_parser().parse_args(
        ["restart", "--interrupt", "--wait-seconds", "12", "--end-tasks"]
    )
    assert (parsed.interrupt, parsed.wait_seconds, parsed.end_tasks) == (
        True,
        12,
        True,
    )
    assert type(parsed.wait_seconds) is int
    plain = cli.build_parser().parse_args(["restart"])
    assert (plain.interrupt, plain.wait_seconds) == (
        False,
        restart.DEFAULT_WAIT_SECONDS,
    )


# Over the wire. The live check of 2026-10-27 found ``dromon restart`` failing at
# once, because its wait went out as a float, and its status read failing once the
# daemon's event-log rate was a fraction. The tests above give ``cli.call`` scripted
# answers, so neither reached the codec.


def test_dromon_restart_crosses_the_wire_to_a_daemon_whose_rates_are_known(
    world: World,
    paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(restart, "POLL_SECONDS", 1e-6)
    monkeypatch.setattr(cli, "RESTART_POLL_SECONDS", 0.0)
    monkeypatch.setattr(cli, "_preflight", passes)
    target = make_daemon(world, paths)
    world.monotonic = 67.0
    events = world.log.last_seq - world.log.opened_seq
    assert events * 3600 % 67 != 0, "the rate must have a fraction"
    sent: list[Operation] = []

    def wire(
        socket: Path,
        operation: Operation,
        args: dict[str, object] | None = None,
        *,
        timeout_seconds: float = client.REQUEST_TIMEOUT_SECONDS,
    ) -> Response:
        del socket, timeout_seconds
        sent.append(operation)
        request = encode_request(Request(f"w{len(sent)}", operation, dict(args or {})))
        response = decode_response(encode_response(target.respond(request)))
        if operation is Operation.RESTART:
            # The successor answers from here on, under another process id.
            monkeypatch.setattr(os, "getpid", lambda: 4242)
        return response

    monkeypatch.setattr(cli, "call", wire)

    async def go() -> int:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        code = cli._restart(target.paths, namespace(wait_seconds=600))  # pyright: ignore[reportPrivateUsage]
        await until(target.stop_requested.is_set)
        stop.set()
        await loop
        return code

    assert asyncio.run(go()) == exits.OK
    assert sent[:3] == [Operation.STATUS, Operation.START_CONTEXT, Operation.RESTART]
    out = capsys.readouterr().out
    assert "for at most 600 seconds" in out
    assert "restarted, pid 4242" in out


def test_a_response_that_cannot_be_encoded_becomes_an_internal_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    class Writer:
        def __init__(self) -> None:
            self.data = bytearray()

        def write(self, data: bytes) -> None:
            self.data.extend(data)

        async def drain(self) -> None:
            return None

    writer = Writer()
    response = Response("r-1", ok=True, result={"rate": 0.5})
    asyncio.run(daemon._write(cast(asyncio.StreamWriter, writer), response))  # pyright: ignore[reportPrivateUsage]
    answer = decode_response(bytes(writer.data))
    assert (answer.request_id, answer.ok) == ("r-1", False)
    assert answer.error_code is ErrorCode.INTERNAL
    assert "result.rate is a float" in (answer.error_message or "")
    assert "could not be encoded" in capsys.readouterr().err


# The preflight. ``dromon restart`` runs the installed daemon's start checks in a
# process of their own, against the project file and a copy of the event log, before
# it asks the running daemon for anything that stops it. The incident of 2026-10-31
# was a project the new code refused only because of lines that were in the log and
# not in the file, which no check of the file alone could see. These tests use a
# contradiction of the same kind, a line whose model the log and the file disagree
# on, which neither the file nor the log shows alone.


CONTRADICTING: Final = 'version = 1\n[lines.alpha]\nmodel = "claude-opus-5-5"\n'
"""A project file that gives ``alpha`` another model than the log does."""

AGREEING: Final = (
    'version = 1\n[lines.alpha]\nmodel = "claude-haiku-5-5"\n'
    '[lines.gamma]\nmodel = "claude-haiku-5-5"\n'
)
"""A project file that agrees with the log and declares a new line ``gamma``, which
a start would create and a check creates only in its copy."""


@pytest.fixture
def live(paths: ProjectPaths) -> Iterator[World]:
    """A running daemon's world, whose log is the project's own event log."""
    ensure_private_dir(paths.state_dir)
    target = World(paths.event_log, ["alpha"])
    yield target
    target.log.close()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def leftovers(paths: ProjectPaths) -> list[str]:
    return sorted(
        p.name
        for p in paths.runtime_dir.iterdir()
        if p.name.startswith(restart.CHECK_PREFIX)
    )


def aged(directory: Path, seconds: float) -> Path:
    """Make ``directory``, dated ``seconds`` ago."""
    directory.mkdir(mode=0o700)
    then = time.time() - seconds
    os.utime(directory, (then, then))
    return directory


def schema_of(path: Path) -> tuple[list[tuple[str, str]], int]:
    connection = sqlite3.connect(path)
    try:
        found = connection.execute(
            "SELECT name, sql FROM sqlite_schema WHERE sql IS NOT NULL ORDER BY name"
        ).fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        return found, version
    finally:
        connection.close()


def test_a_snapshot_copies_in_ranges_and_keeps_the_schema_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit's finding P1. The copy holds the live log's read lock one range at a
    time, so the daemon's commits wait for a range, not for the whole copy."""
    source = tmp_path / "events.sqlite"
    world = World(source, [f"line{n}" for n in range(10)])
    world.log.close()
    pauses: list[float] = []
    monkeypatch.setattr(eventlog.time, "sleep", pauses.append)
    copy = tmp_path / "copy.sqlite"
    eventlog.snapshot(source, copy, rows=3, pause_seconds=0.25)
    assert len(pauses) > 3 and set(pauses) == {0.25}
    assert replay(copy) == replay(source)
    assert read_entries(copy) == read_entries(source)
    assert schema_of(copy) == schema_of(source)
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    with EventLog(copy) as reopened:
        reopened.append(OperatorRegistered("zed"))
        assert reopened.last_seq == len(read_entries(source)) + 1
    connection = sqlite3.connect(copy)
    try:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            connection.execute("DELETE FROM events")
    finally:
        connection.close()


def test_a_snapshot_finishes_while_the_daemon_writes_and_leaves_its_writes_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source is written between every two ranges, as a busy daemon writes. A
    backup would start again at each write. The copy holds the log as it stood when
    the copy began, and finishes."""
    source = tmp_path / "events.sqlite"
    world = World(source, [f"line{n}" for n in range(10)])
    before = read_entries(source)
    written: list[str] = []

    def daemon_writes(seconds: float) -> None:
        del seconds
        line_id = f"during{len(written)}"
        world.scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
        written.append(line_id)

    monkeypatch.setattr(eventlog.time, "sleep", daemon_writes)
    copy = tmp_path / "copy.sqlite"
    try:
        eventlog.snapshot(source, copy, rows=2)
    finally:
        world.log.close()
    assert len(written) >= len(before) // 2
    assert read_entries(copy) == before
    assert len(read_entries(source)) > len(before)
    assert not set(written) & set(replay(copy).lines)


def test_a_snapshot_copies_a_table_a_later_version_adds_whole(
    tmp_path: Path,
) -> None:
    source = tmp_path / "events.sqlite"
    world = World(source, ["alpha"])
    world.log.close()
    connection = sqlite3.connect(source)
    try:
        connection.execute('CREATE TABLE "odd ""name""" (x INTEGER)')
        connection.executemany('INSERT INTO "odd ""name""" VALUES (?)', [(1,), (2,)])
        connection.commit()
    finally:
        connection.close()
    copy = tmp_path / "copy.sqlite"
    eventlog.snapshot(source, copy)
    assert schema_of(copy) == schema_of(source)
    connection = sqlite3.connect(copy)
    try:
        found = connection.execute('SELECT x FROM "odd ""name""" ORDER BY x').fetchall()
    finally:
        connection.close()
    assert found == [(1,), (2,)]
    assert read_entries(copy) == read_entries(source)


def test_a_snapshot_copies_at_least_one_row_at_a_time(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="at least one row"):
        eventlog.snapshot(tmp_path / "a", tmp_path / "b", rows=0)
    with pytest.raises(ValueError, match="at least one row"):
        eventlog.snapshot(tmp_path / "a", tmp_path / "b", max_bytes=0)


def test_a_snapshot_attaches_the_live_log_read_only(tmp_path: Path) -> None:
    """Another connection holds the live log's write lock, as a daemon in the
    middle of a commit does. Attached read-only, the source takes no part in the
    copy's own ``BEGIN IMMEDIATE``, so the copy goes on. Attached without
    ``mode=ro``, that transaction would wait for the source's write lock, and here
    fail as locked, as was observed on 2026-10-31."""
    source = tmp_path / "events.sqlite"
    world = World(source, ["alpha"])
    world.log.close()
    holder = sqlite3.connect(source, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        eventlog.snapshot(source, tmp_path / "copy.sqlite")
        assert time.monotonic() - started < 4.0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert read_entries(tmp_path / "copy.sqlite") == read_entries(source)


def test_a_snapshot_range_is_bounded_by_bytes_as_well_as_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A body may be up to 1 MiB, so a count of rows alone would not bound how long
    a range holds the lock. A range stops at the event that reaches the byte bound,
    and always takes one."""
    source = tmp_path / "events.sqlite"
    world = World(source, [f"line{n}" for n in range(5)])
    world.log.close()
    events = len(read_entries(source))
    pauses: list[float] = []
    monkeypatch.setattr(eventlog.time, "sleep", pauses.append)
    eventlog.snapshot(source, tmp_path / "one.sqlite", max_bytes=1)
    assert len(pauses) == events
    pauses.clear()
    eventlog.snapshot(source, tmp_path / "all.sqlite")
    assert len(pauses) == 1
    assert read_entries(tmp_path / "one.sqlite") == read_entries(source)
    assert read_entries(tmp_path / "all.sqlite") == read_entries(source)


def test_a_sweep_removes_old_check_directories_and_nothing_else(
    tmp_path: Path,
) -> None:
    """Audit's finding P2."""
    old = aged(tmp_path / f"{restart.CHECK_PREFIX}old", restart.CHECK_SECONDS + 5)
    (old / "events.sqlite").write_text("a copy")
    then = time.time() - restart.CHECK_SECONDS - 5
    os.utime(old, (then, then))
    young = aged(tmp_path / f"{restart.CHECK_PREFIX}young", 1)
    other = aged(tmp_path / "keep-me", restart.CHECK_SECONDS + 5)
    plain = tmp_path / f"{restart.CHECK_PREFIX}file"
    plain.write_text("not a directory")
    target = aged(tmp_path / "target", restart.CHECK_SECONDS + 5)
    link = tmp_path / f"{restart.CHECK_PREFIX}link"
    link.symlink_to(target)
    assert restart.sweep_checks(tmp_path, restart.CHECK_SECONDS) == 1
    assert not old.exists()
    assert young.exists() and other.exists() and plain.exists()
    assert link.is_symlink() and target.exists()
    assert restart.sweep_checks(tmp_path, None) == 1
    assert not young.exists()
    assert restart.sweep_checks(tmp_path / "absent", None) == 0


def test_a_check_sweeps_what_a_killed_check_left(
    live: World, paths: ProjectPaths
) -> None:
    config_path(paths.state_dir).write_text(AGREEING)
    ensure_private_dir(paths.runtime_dir)
    killed = aged(
        paths.runtime_dir / f"{restart.CHECK_PREFIX}killed", restart.CHECK_SECONDS + 5
    )
    running = aged(paths.runtime_dir / f"{restart.CHECK_PREFIX}running", 1)
    assert daemon.run(paths, check_only=True) == exits.OK
    assert not killed.exists()
    assert running.exists()


def test_a_check_writes_nothing_in_the_operator_s_fleet_index(
    live: World,
    paths: ProjectPaths,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start makes the fleet index, and a check does not, as it registers nothing
    in the open-federation registry. The entry is written by
    ``fleet_registration``, which a check returns before, and which needs the
    process table, which an agent sandbox denies, so the entry is not asserted
    here."""
    registry_dir = tmp_path / "registry"
    monkeypatch.setenv(REGISTRY_DIR_ENV, str(registry_dir))
    config_path(paths.state_dir).write_text(AGREEING)
    assert daemon.run(paths, check_only=True) == exits.OK
    assert not fleet.index_directory(registry_dir).exists()

    async def no_serve(target: Daemon) -> None:
        del target

    live.log.close()
    monkeypatch.setattr(daemon, "serve", no_serve)
    monkeypatch.setattr(daemon, "environment_reading_works", lambda: False)
    assert daemon.run(paths) == exits.OK
    assert fleet.index_directory(registry_dir).is_dir()
    live.log = EventLog(paths.event_log)


def test_a_daemon_given_a_registry_directory_registers_there_only(
    paths: ProjectPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 14, item 7, decided by ops for the operator's review. A daemon given
    ``DROMON_REGISTRY_DIR`` makes the index there and leaves the default one
    untouched, since the override is an isolation boundary that every test relies
    on. A client given the same override finds it."""
    default = tmp_path / "home" / ".local" / "state" / "dromon" / "registry"
    monkeypatch.setattr(registry, "default_directory", lambda: default)
    given = tmp_path / "given"
    monkeypatch.setenv(REGISTRY_DIR_ENV, str(given))

    async def no_serve(target: Daemon) -> None:
        assert target.registry_dir == given

    monkeypatch.setattr(daemon, "serve", no_serve)
    monkeypatch.setattr(daemon, "environment_reading_works", lambda: False)
    assert daemon.run(paths) == exits.OK
    assert fleet.index_directory(given).is_dir()
    assert not default.exists()


def test_a_start_sweeps_every_check_directory(
    paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_serve(target: Daemon) -> None:
        del target

    monkeypatch.setattr(daemon, "serve", no_serve)
    monkeypatch.setattr(daemon, "environment_reading_works", lambda: False)
    ensure_private_dir(paths.runtime_dir)
    young = aged(paths.runtime_dir / f"{restart.CHECK_PREFIX}young", 1)
    assert daemon.run(paths) == exits.OK
    assert not young.exists()


def test_a_preflight_that_times_out_sweeps_its_copy(
    paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    ensure_private_dir(paths.runtime_dir)
    left = aged(paths.runtime_dir / f"{restart.CHECK_PREFIX}left", 1)

    def slow(*args: object, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired("dromond", restart.CHECK_SECONDS)

    monkeypatch.setattr(cli.subprocess, "run", slow)
    found = cli._preflight(["dromond"], paths.runtime_dir)  # pyright: ignore[reportPrivateUsage]
    assert found is not None and "timed out" in found
    assert not left.exists()


def test_the_check_refuses_what_only_the_log_contradicts_and_writes_nothing(
    live: World, paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path(paths.state_dir).write_text(CONTRADICTING)
    ensure_private_dir(paths.runtime_dir)
    lock = daemon.acquire_lock(paths.lock)
    paths.socket.write_text("the old daemon's socket")
    before = digest(paths.event_log)
    try:
        code = daemon.run(paths, check_only=True)
    finally:
        os.close(lock)
    assert code == exits.FAILURE
    err = capsys.readouterr().err
    assert "dromond: line alpha runs claude-haiku-5-5" in err
    assert digest(paths.event_log) == before
    assert paths.socket.read_text() == "the old daemon's socket"
    assert leftovers(paths) == []


def test_the_check_passes_and_leaves_the_log_as_it_was(
    live: World, paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path(paths.state_dir).write_text(AGREEING)
    before = digest(paths.event_log)
    assert daemon.run(paths, check_only=True) == exits.OK
    assert daemon.CHECK_PASSED in capsys.readouterr().err
    assert digest(paths.event_log) == before
    assert "gamma" not in replay(paths.event_log).lines
    assert leftovers(paths) == []


def test_the_check_of_a_project_with_no_log_yet_passes(
    paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_private_dir(paths.state_dir)
    config_path(paths.state_dir).write_text(AGREEING)
    assert daemon.run(paths, check_only=True) == exits.OK
    assert not paths.event_log.exists()
    assert daemon.CHECK_PASSED in capsys.readouterr().err


def test_a_log_the_new_code_cannot_read_is_refused_in_its_own_words(
    paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    ensure_private_dir(paths.state_dir)
    paths.event_log.write_bytes(b"not a database at all, but long enough" * 40)
    assert daemon.run(paths, check_only=True) == exits.FAILURE
    assert "dromond:" in capsys.readouterr().err
    assert leftovers(paths) == []


def test_the_status_names_the_options_a_successor_and_a_preflight_take(
    world: World, paths: ProjectPaths
) -> None:
    target = make_daemon(world, paths)
    assert target.status()["options"] == ["--tick-seconds", "0.5", "--hold-wakes"]


def test_the_preflight_runs_the_daemon_s_check_with_the_running_options(
    paths: ProjectPaths,
) -> None:
    argv = cli._preflight_argv(paths, ["--tick-seconds", "0.5"])  # pyright: ignore[reportPrivateUsage]
    assert argv == [
        sys.executable,
        "-m",
        "dromon.daemon",
        "--project",
        str(paths.root),
        "--runtime-dir",
        str(paths.runtime_dir),
        "--tick-seconds",
        "0.5",
        "--check",
    ]
    assert cli._options_of({"options": ["--hold-wakes", 3]}) == []  # pyright: ignore[reportPrivateUsage]
    assert cli._options_of({"options": "--hold-wakes"}) == []  # pyright: ignore[reportPrivateUsage]
    assert cli._options_of({}) == []  # pyright: ignore[reportPrivateUsage]
    assert cli._options_of({"options": ["--hold-wakes"]}) == ["--hold-wakes"]  # pyright: ignore[reportPrivateUsage]


def test_the_daemon_gives_its_interpreter_and_start_variables_on_the_client_socket(
    world: World, paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 14, item 6. The successor runs on the daemon's interpreter with the
    daemon's environment, so the preflight asks for both."""
    target = make_daemon(world, paths)
    monkeypatch.setenv(REGISTRY_DIR_ENV, "/elsewhere/registry")
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    response = target.respond(encode_request(Request("c", Operation.START_CONTEXT, {})))
    assert response.ok and response.result is not None
    assert response.result == {
        "interpreter": sys.executable,
        "environment": {REGISTRY_DIR_ENV: "/elsewhere/registry", "SSH_AUTH_SOCK": None},
    }
    refused = target.respond(
        encode_request(Request("d", Operation.START_CONTEXT, {"extra": "x"}))
    )
    assert not refused.ok
    # Neither the status nor any agent tool carries it.
    status = target.status()
    assert "interpreter" not in status and "environment" not in status


def test_the_preflight_takes_the_daemon_s_interpreter_and_variables(
    paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = cli._start_context(  # pyright: ignore[reportPrivateUsage]
        {
            "interpreter": "/opt/daemon/bin/python",
            "environment": {
                REGISTRY_DIR_ENV: "/daemon/registry",
                "SSH_AUTH_SOCK": None,
            },
        }
    )
    assert context is not None
    argv = cli._preflight_argv(paths, [], context.interpreter)  # pyright: ignore[reportPrivateUsage]
    assert argv[0] == "/opt/daemon/bin/python"
    monkeypatch.setenv(REGISTRY_DIR_ENV, "/client/registry")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/client/agent.sock")
    monkeypatch.setenv("KEPT", "yes")
    environment = cli._preflight_environment(context)  # pyright: ignore[reportPrivateUsage]
    assert environment[REGISTRY_DIR_ENV] == "/daemon/registry"
    assert "SSH_AUTH_SOCK" not in environment
    assert environment["KEPT"] == "yes"
    assert cli._preflight_environment(None)["SSH_AUTH_SOCK"] == "/client/agent.sock"  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "result",
    [
        {"interpreter": "python", "environment": {}},
        {"interpreter": 3, "environment": {}},
        {"interpreter": "/bin/python", "environment": []},
        {"interpreter": "/bin/python", "environment": {"PATH": "/evil"}},
        {"interpreter": "/bin/python", "environment": {REGISTRY_DIR_ENV: 3}},
        {},
    ],
)
def test_a_start_context_in_another_form_is_not_used(result: dict[str, object]) -> None:
    assert cli._start_context(result) is None  # pyright: ignore[reportPrivateUsage]


def test_the_preflight_runs_with_the_environment_given() -> None:
    probe = (
        sys.executable,
        "-c",
        "import os, sys; sys.exit(0 if os.environ.get('ONLY') == 'given' else 4)",
    )
    assert cli._preflight(probe, None, {"ONLY": "given"}) is None  # pyright: ignore[reportPrivateUsage]
    assert cli._preflight(probe, None, {"ONLY": "other"}) == "status 4: no output"  # pyright: ignore[reportPrivateUsage]


def test_an_older_daemon_without_start_context_leaves_the_client_s_own(
    paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def older(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        del socket, args
        assert operation is Operation.START_CONTEXT
        return Response(
            "r",
            ok=False,
            error_code=ErrorCode.UNKNOWN_OPERATION,
            error_message="unknown operation start_context",
        )

    monkeypatch.setattr(cli, "call", older)
    assert cli._ask_start_context(paths, "restart") is None  # pyright: ignore[reportPrivateUsage]
    assert "does not say its interpreter and start environment" in (
        capsys.readouterr().out
    )


def test_the_preflight_quotes_the_daemon_s_own_refusal() -> None:
    noisy = (
        sys.executable,
        "-c",
        "import sys; print('warming up', file=sys.stderr);"
        " print('dromond: the project file is refused: x', file=sys.stderr);"
        " sys.exit(1)",
    )
    found = cli._preflight(noisy)  # pyright: ignore[reportPrivateUsage]
    assert found == "status 1: dromond: the project file is refused: x"


def over_the_wire(
    target: Daemon, monkeypatch: pytest.MonkeyPatch, sent: list[Operation]
) -> None:
    """Give ``cli.call`` the daemon's own answers, through the codec, and make a
    successor answer under another process id once ``restart`` is sent."""

    def wire(
        socket: Path,
        operation: Operation,
        args: dict[str, object] | None = None,
        *,
        timeout_seconds: float = client.REQUEST_TIMEOUT_SECONDS,
    ) -> Response:
        del socket, timeout_seconds
        sent.append(operation)
        request = encode_request(Request(f"w{len(sent)}", operation, dict(args or {})))
        response = decode_response(encode_response(target.respond(request)))
        if operation is Operation.RESTART:
            monkeypatch.setattr(os, "getpid", lambda: 4242)
        return response

    monkeypatch.setattr(cli, "call", wire)


def test_a_restart_the_preflight_refuses_leaves_the_old_daemon_serving(
    live: World,
    paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The preflight here is the real one, the installed daemon run with
    ``--check`` in a process of its own."""
    config_path(paths.state_dir).write_text(CONTRADICTING)
    target = make_daemon(live, paths)
    sent: list[Operation] = []
    over_the_wire(target, monkeypatch, sent)
    before = digest(paths.event_log)

    async def go() -> int:
        return cli._restart(paths, namespace())  # pyright: ignore[reportPrivateUsage]

    assert asyncio.run(go()) == exits.FAILURE
    err = capsys.readouterr().err
    assert "the installed daemon refuses to start, so the running one was not" in err
    assert "dromond: line alpha runs claude-haiku-5-5" in err
    assert sent == [Operation.STATUS, Operation.START_CONTEXT]
    assert live.scheduler.restart_status() is None
    assert not target.stop_requested.is_set()
    still = target.respond(encode_request(Request("again", Operation.STATUS, {})))
    assert still.ok and still.result is not None
    assert still.result["pid"] == os.getpid()
    assert digest(paths.event_log) == before


def test_a_restart_the_preflight_passes_goes_on_to_the_successor(
    live: World,
    paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(restart, "POLL_SECONDS", 1e-6)
    monkeypatch.setattr(cli, "RESTART_POLL_SECONDS", 0.0)
    config_path(paths.state_dir).write_text(AGREEING)
    target = make_daemon(live, paths)
    sent: list[Operation] = []
    over_the_wire(target, monkeypatch, sent)

    async def go() -> int:
        stop = asyncio.Event()
        loop = asyncio.create_task(live.scheduler.run(stop))
        code = cli._restart(paths, namespace(wait_seconds=60))  # pyright: ignore[reportPrivateUsage]
        await until(target.stop_requested.is_set)
        stop.set()
        await loop
        return code

    assert asyncio.run(go()) == exits.OK
    assert sent[:3] == [Operation.STATUS, Operation.START_CONTEXT, Operation.RESTART]
    assert "restarted, pid 4242" in capsys.readouterr().out
    assert "gamma" not in replay(paths.event_log).lines
