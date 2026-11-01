"""A turn built before a change of owner does not run on with its old access.

**Round 14 task of 2026-11-01.** A brief that arrived before a role was given made
the new owner's first turn read-only, since a turn's permissions are fixed when its
engine is built. A change of owner now ends each running turn whose line's access it
changed at the turn's next safe point, and the next turn is built with the new
access."""

import asyncio
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.model import (
    HarnessAddress,
    LineAddress,
    OperatorAddress,
    RoleDeclared,
    SeatBound,
)
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")


class Roles:
    """Role core with seats alpha and beta, which may own it, and gamma, which may
    not, and nobody owning it yet. A line is marked running with a task that is
    never run, so the scheduler sees a turn in flight without an engine."""

    def __init__(self, path: Path) -> None:
        self.log = EventLog(path)

        def build(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
            raise AssertionError("no turn runs here")

        self.scheduler = Scheduler(self.log, build, tick_seconds=0)
        self.scheduler.ensure_operator("ana")
        for line_id in ("alpha", "beta", "gamma"):
            self.scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
        for event in (
            RoleDeclared("core", "tmp/worktrees/core", 1800),
            SeatBound("alpha", "core", True),
            SeatBound("beta", "core", True),
            SeatBound("gamma", "core", False),
        ):
            self.log.append(event)
        self.loop = asyncio.new_event_loop()
        self.tasks: list[asyncio.Task[None]] = []

    def running(self, line_id: str) -> None:
        async def turn() -> None:
            return None

        task = self.loop.create_task(turn())
        self.tasks.append(task)
        self.scheduler._running[line_id] = task  # pyright: ignore[reportPrivateUsage]

    def stopped(self, line_id: str) -> None:
        self.scheduler._running.pop(line_id)  # pyright: ignore[reportPrivateUsage]

    def close(self) -> None:
        for task in self.tasks:
            self.loop.run_until_complete(task)
        self.loop.close()
        self.log.close()


@pytest.fixture
def roles(tmp_path: Path) -> Generator[Roles]:
    target = Roles(tmp_path / "events.sqlite")
    yield target
    target.close()


def test_a_seat_given_the_role_mid_turn_ends_that_turn_once(roles: Roles) -> None:
    roles.running("beta")
    roles.scheduler.give_ownership(ANA, "core", "beta")
    reason = roles.scheduler.redirect_reason("beta")
    assert reason is not None
    assert "role core, from no seat to beta" in reason
    assert "next turn is built with the new access" in reason
    assert roles.scheduler.redirect_reason("beta") is None
    # The operator's note wakes the fresh turn once this one has ended.
    assert any(
        message.recipient == LineAddress("beta") and message.sender == ANA
        for message in roles.log.state.messages.values()
    )


def test_a_line_not_running_has_no_turn_to_end(roles: Roles) -> None:
    roles.scheduler.give_ownership(ANA, "core", "alpha")
    for line_id in ("alpha", "beta", "gamma"):
        assert roles.scheduler.redirect_reason(line_id) is None


def test_a_seat_that_takes_the_role_in_its_turn_ends_it_and_is_woken(
    roles: Roles,
) -> None:
    roles.running("beta")
    roles.scheduler.take_ownership("beta", "the role is vacant")
    reason = roles.scheduler.redirect_reason("beta")
    assert reason is not None and "to beta" in reason
    notes = [
        message.body
        for message in roles.log.state.messages.values()
        if message.recipient == LineAddress("beta")
        and isinstance(message.sender, HarnessAddress)
    ]
    assert any("You took role core" in body for body in notes)


def test_a_taker_not_running_gets_no_note(roles: Roles) -> None:
    roles.scheduler.take_ownership("beta", "the role is vacant")
    assert not any(
        "You took role core" in message.body
        for message in roles.log.state.messages.values()
    )


def test_an_unconfined_seat_running_when_the_role_gains_an_owner_is_ended(
    roles: Roles, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unconfined(line_id: str) -> bool:
        return line_id == "gamma"

    monkeypatch.setattr(roles.scheduler, "_unconfined", unconfined)
    roles.running("gamma")
    roles.scheduler.give_ownership(ANA, "core", "alpha")
    reason = roles.scheduler.redirect_reason("gamma")
    assert reason is not None and "from no seat to alpha" in reason


def test_a_confined_seat_running_beside_the_change_runs_on(roles: Roles) -> None:
    roles.running("gamma")
    roles.scheduler.give_ownership(ANA, "core", "alpha")
    assert roles.scheduler.redirect_reason("gamma") is None


def test_the_reason_lapses_once_the_turn_is_no_longer_running(roles: Roles) -> None:
    roles.running("beta")
    roles.scheduler.give_ownership(ANA, "core", "beta")
    roles.stopped("beta")
    assert roles.scheduler.redirect_reason("beta") is None


def test_the_change_of_access_is_asked_before_priority_mail(roles: Roles) -> None:
    roles.running("beta")
    roles.scheduler.send(ANA, LineAddress("beta"), "stop and read", priority=True)
    roles.scheduler.give_ownership(ANA, "core", "beta")
    first = roles.scheduler.redirect_reason("beta")
    assert first is not None and "change of owner" in first
    second = roles.scheduler.redirect_reason("beta")
    assert second is not None and "change of owner" not in second
