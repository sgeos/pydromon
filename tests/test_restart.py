"""Restart to upgrade, driven by scripted engines and without sockets.

A restart is tested in three parts. The scheduler drains, ends turns at a safe point
and leaves notes. The daemon's ``restart`` request starts the watch that stops it for
a successor. ``dromon restart`` polls until a daemon with another process id answers.
The successor is a new ``Scheduler`` on the reopened event log, as the started daemon
builds one, since no test here may start a real daemon.
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Coroutine, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli, client, daemon, exits, restart
from dromon.activity import ActivityKind
from dromon.configview import Overrides
from dromon.daemon import Daemon, build_parser, start_successor, successor_args
from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog, read_entries
from dromon.model import LineAddress, OperatorAddress, RestartNoted
from dromon.paths import ProjectPaths, project_paths
from dromon.protocol import ErrorCode, Operation, Response
from dromon.scheduler import LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
START = datetime(2026, 10, 27, 9, 0, 0, tzinfo=UTC)


class Scripted:
    """A line engine that waits at a gate, if one is set, then reaches
    ``safe_points`` safe points, at each of which it asks to be redirected, as a
    line's ``SteeredDriver`` does at each text or reasoning event, then waits at a
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
        running = world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 60.0)
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
        world.scheduler.begin_restart(restart.Mode.WAIT, "ana", 60.0)
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
    world.scheduler.begin_restart(restart.Mode.WAIT, "ana", 60.0)

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
        world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 30.0)
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
        world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 60.0)
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
        world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 60.0)
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
    world.scheduler.begin_restart(restart.Mode.WAIT, "ana", 60.0)
    with pytest.raises(restart.RestartRefused, match="draining already"):
        world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 60.0)


def test_a_restart_refuses_to_stop_background_tasks_unless_asked(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks = {"alpha": 2}
    monkeypatch.setattr(world.scheduler, "kept_tasks", counter(tasks))
    with pytest.raises(restart.RestartRefused, match="2 background tasks") as refused:
        world.scheduler.begin_restart(restart.Mode.WAIT, "ana", 60.0)
    assert "lines alpha" in str(refused.value)
    assert world.scheduler.restart_status() is None
    world.scheduler.begin_restart(restart.Mode.WAIT, "ana", 60.0, end_tasks=True)
    assert world.scheduler.restart_progress() is restart.Outcome.READY


def test_tasks_that_begin_during_the_drain_abandon_the_restart(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    tasks: dict[str, int] = {}
    monkeypatch.setattr(world.scheduler, "kept_tasks", counter(tasks))
    world.scheduler.begin_restart(restart.Mode.WAIT, "ana", 60.0)
    tasks["beta"] = 1
    assert world.scheduler.restart_progress() is restart.Outcome.ABANDONED
    status = world.scheduler.restart_status()
    assert status is not None and "lines beta" in cast(str, status["reason"])


def test_a_muted_line_is_interrupted_by_a_restart_and_stays_muted_after(
    world: World,
) -> None:
    gate = asyncio.Event()
    world.gates["alpha"] = [gate]
    wake_id, _ = world.scheduler.schedule_wake("beta", 600, "later")
    world.scheduler.mute("beta")

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "work on it")
        await until(lambda: world.scheduler.running_lines() == ["alpha"])
        world.scheduler.mute("alpha")
        world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 60.0)
        gate.set()

    run(world, act)
    assert world.reasons["alpha"] == [restart.REASON]
    world.restart()
    run(world, nothing)
    state = world.log.state
    assert state.lines["alpha"].muted and state.lines["beta"].muted
    assert state.scheduled["beta"].message_id == wake_id
    # The note waits for the unmute, like any mail of a muted line.
    assert len(world.prompts["alpha"]) == 1

    async def unmute() -> None:
        world.scheduler.unmute("alpha")

    run(world, unmute)
    assert len(world.prompts["alpha"]) == 2
    assert "daemon restart" in world.prompts["alpha"][1]


def test_priority_mail_takes_the_safe_point_before_a_restart(world: World) -> None:
    gate = asyncio.Event()
    world.gates["alpha"] = [gate]

    async def act() -> None:
        world.scheduler.send(ANA, ALPHA, "work on it")
        await until(lambda: world.scheduler.running_lines() == ["alpha"])
        world.scheduler.begin_restart(restart.Mode.INTERRUPT, "ana", 60.0)
        world.scheduler.send(ANA, ALPHA, "stop and look", priority=True)
        gate.set()

    run(world, act)
    (reason,) = world.reasons["alpha"]
    assert reason is not None and reason.startswith("priority mail")
    assert restart_notes(world) == []


# Pure parts.


@pytest.mark.parametrize("value", [0, -1, 3601, float("nan"), float("inf"), True, "9"])
def test_check_wait_refuses_what_is_not_a_bounded_wait(value: object) -> None:
    with pytest.raises(restart.RestartRefused):
        restart.check_wait(value)


def test_check_wait_accepts_the_bounds_and_polls_pass_the_deadline() -> None:
    assert restart.check_wait(restart.MAX_WAIT_SECONDS) == restart.MAX_WAIT_SECONDS
    assert restart.check_wait(1) == 1.0
    for wait in (0.05, 1.0, 600.0):
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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = project_paths(tmp_path, tmp_path / "run")
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


def make_daemon(target: World, tmp_path: Path) -> Daemon:
    paths = project_paths(tmp_path, tmp_path / "run")
    return Daemon(
        paths,
        target.log,
        "2026-10-27T09:00:00Z",
        target.scheduler,
        command_line=Overrides(tick_seconds=0.5),
        hold_wakes=True,
    )


def test_restart_drains_then_stops_for_a_successor_with_the_same_options(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restart, "POLL_SECONDS", 1e-6)
    target = make_daemon(world, tmp_path)
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
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(restart, "POLL_SECONDS", 1e-6)
    target = make_daemon(world, tmp_path)
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
        ({"operator": "ana", "wait_seconds": 0}, ErrorCode.BAD_REQUEST, "above 0"),
        ({"operator": "ana", "wait_seconds": "9"}, ErrorCode.BAD_REQUEST, "number"),
        ({"operator": "ana", "end_tasks": "yes"}, ErrorCode.BAD_REQUEST, "boolean"),
        ({"operator": "ana", "force": True}, ErrorCode.BAD_REQUEST, "no force"),
        ({"operator": "@ana"}, ErrorCode.REJECTED, ""),
    ],
)
def test_a_bad_restart_is_refused_and_changes_nothing(
    world: World,
    tmp_path: Path,
    args: dict[str, object],
    code: ErrorCode,
    text: str,
) -> None:
    target = make_daemon(world, tmp_path)

    async def go() -> Response:
        return respond(target, "restart", args)

    response = asyncio.run(go())
    assert not response.ok and response.error_code is code
    assert text in (response.error_message or "")
    assert world.scheduler.restart_status() is None


def test_a_restart_while_one_drains_is_rejected(world: World, tmp_path: Path) -> None:
    target = make_daemon(world, tmp_path)

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
        "wait_seconds": 30.0,
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
        self, socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        del socket
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


def no_sleep(seconds: float) -> None:
    del seconds


@pytest.fixture
def cli_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectPaths:
    monkeypatch.setattr(cli, "RESTART_POLL_SECONDS", 0.0)
    monkeypatch.setattr(cli, "_preflight", lambda: None)
    return project_paths(tmp_path, tmp_path / "run")


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
    assert args == {"operator": "ana", "mode": "interrupt", "wait_seconds": 30.0}
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
    code = cli._restart(cli_paths, namespace(wait_seconds=1.0))  # pyright: ignore[reportPrivateUsage]
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

    def record(
        socket: Path, operation: Operation, args: dict[str, object] | None = None
    ) -> Response:
        sent.append(operation)
        raise AssertionError("no request may be sent")

    monkeypatch.setattr(cli, "call", record)
    monkeypatch.setattr(cli, "_preflight", lambda: "status 1: SyntaxError")
    assert cli._restart(cli_paths, namespace()) == exits.FAILURE  # pyright: ignore[reportPrivateUsage]
    assert sent == []
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
        12.0,
        True,
    )
    plain = cli.build_parser().parse_args(["restart"])
    assert (plain.interrupt, plain.wait_seconds) == (
        False,
        restart.DEFAULT_WAIT_SECONDS,
    )
