"""Bound polling while a turn waits for an external worker."""

import asyncio
from pathlib import Path
from typing import cast

import pytest

from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.model import LineAddress, OperatorAddress
from dromon.scheduler import Scheduler, engine_factory


def test_running_recipient_does_not_drive_zero_period_ticks(tmp_path: Path) -> None:
    async def exercise() -> None:
        with EventLog(tmp_path / "events.sqlite") as log:
            scheduler = Scheduler(log, engine_factory(str(tmp_path)), tick_seconds=0)
            scheduler.create_line("alpha", "claude-haiku-5-5", Strategy.SUBSCRIPTION)
            scheduler.ensure_operator("ana")
            scheduler.send(OperatorAddress("ana"), LineAddress("alpha"), "pending")
            gate = asyncio.Event()
            turn = asyncio.create_task(gate.wait())
            scheduler._running["alpha"] = cast(asyncio.Task[None], turn)  # pyright: ignore[reportPrivateUsage]
            stop = asyncio.Event()
            runner = asyncio.create_task(scheduler.run(stop))
            try:
                # Repeated loop opportunities distinguish notification waiting from
                # a self-scheduling zero-period loop without wall-clock timing.
                for _ in range(100):
                    await asyncio.sleep(0)
                assert scheduler.tick <= 2
            finally:
                stop.set()
                await runner

    asyncio.run(exercise())


def test_idle_does_not_poll_state_while_a_turn_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def exercise() -> None:
        with EventLog(tmp_path / "events.sqlite") as log:
            scheduler = Scheduler(log, engine_factory(str(tmp_path)), tick_seconds=0)
            gate = asyncio.Event()
            turn = asyncio.create_task(gate.wait())
            scheduler._running["alpha"] = cast(asyncio.Task[None], turn)  # pyright: ignore[reportPrivateUsage]
            calls = 0

            def waiting() -> bool:
                nonlocal calls
                calls += 1
                return not turn.done()

            monkeypatch.setattr(scheduler, "_work_waiting", waiting)
            idle = asyncio.create_task(scheduler.idle())
            try:
                for _ in range(100):
                    await asyncio.sleep(0)
                assert not idle.done()
                assert calls <= 2
                gate.set()
                await idle
            finally:
                gate.set()
                await turn
                idle.cancel()
                await asyncio.gather(idle, return_exceptions=True)
                scheduler.stop_workers()

    asyncio.run(exercise())
