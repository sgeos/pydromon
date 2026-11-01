"""Round 14, item 4. After a compaction, the turn's prompt names the background tasks
Dromon holds as running, since a compaction can drop what the line knew of them."""

import asyncio
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog
from dromon.model import LineAddress, OperatorAddress
from dromon.scheduler import (
    MAX_ROSTER_TASKS,
    WITHHELD_COMMAND,
    LineEngine,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")


class Kept:
    def __init__(self) -> None:
        self.tasks: list[object] = []
        self.kept = True
        self.prompts: list[str] = []
        self.session_id = None
        self.broken = False

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        return TurnReport("success", 1, "ok", (), None, None, "s-1", None, None)

    async def retire(self) -> None:
        return None

    def kept_status(self) -> dict[str, object]:
        if self.broken:
            raise RuntimeError("cannot say")
        return {
            "kept": self.kept,
            "background_tasks": len(self.tasks),
            "tasks": list(self.tasks),
        }


@pytest.fixture
def scheduler(tmp_path: Path) -> Generator[tuple[Scheduler, Kept]]:
    engine = Kept()

    def build(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        return cast(LineEngine, engine)

    with EventLog(tmp_path / "events.sqlite") as log:
        found = Scheduler(log, build, tick_seconds=0)
        found.ensure_operator("ana")
        found.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        found._engines["alpha"] = cast(Any, engine)  # pyright: ignore[reportPrivateUsage]
        yield found, engine


def task(task_id: str, command: str) -> dict[str, str]:
    return {"task_id": task_id, "command": command}


def lead(found: Scheduler, compacting: bool) -> str | None:
    return found._roster_lead("alpha", compacting)  # pyright: ignore[reportPrivateUsage]


def test_a_compaction_names_the_tasks_dromon_holds_as_running(
    scheduler: tuple[Scheduler, Kept],
) -> None:
    found, engine = scheduler
    engine.tasks = [task("b1", "make gate"), task("b2", 'sleep 9 "x"')]
    text = lead(found, True)
    assert text is not None
    assert "Dromon still holds 2 as running in your kept session" in text
    assert "by identifier b1, b2." in text
    assert "make gate" not in text and "sleep" not in text
    assert "Their ends are noted as they happen." in text


def test_nothing_is_said_without_a_compaction_or_without_tasks(
    scheduler: tuple[Scheduler, Kept],
) -> None:
    found, engine = scheduler
    engine.tasks = [task("b1", "make gate")]
    assert lead(found, False) is None
    engine.tasks = []
    assert lead(found, True) is None


def test_a_compaction_the_engine_made_by_itself_owes_the_roster_next_turn(
    scheduler: tuple[Scheduler, Kept],
) -> None:
    found, engine = scheduler
    engine.tasks = [task("b1", "make gate")]
    found._brief_owed.add("alpha")  # pyright: ignore[reportPrivateUsage]
    assert lead(found, False) is not None


def test_no_command_text_reaches_the_roster_so_it_is_no_disclosure_oracle(
    scheduler: tuple[Scheduler, Kept],
) -> None:
    """Review of 2026-11-01, finding 1. A roster that screened commands let a line
    learn from the withheld words whether a candidate held a listed term. Two tasks
    whose commands differ in that way must give the same roster."""
    found, engine = scheduler
    found._disclosure = ("secretword",)  # pyright: ignore[reportPrivateUsage]
    engine.tasks = [task("b1", "run SECRETWORD now"), task("b2", "make gate")]
    covered = lead(found, True)
    engine.tasks = [task("b1", "run harmless now"), task("b2", "make gate")]
    plain = lead(found, True)
    assert covered is not None and covered == plain
    assert "SECRETWORD" not in covered and WITHHELD_COMMAND not in covered


def test_the_roster_makes_no_disclosure_check(
    scheduler: tuple[Scheduler, Kept], monkeypatch: pytest.MonkeyPatch
) -> None:
    found, engine = scheduler
    calls: list[tuple[str, ...]] = []

    def record(*texts: str) -> bool:
        calls.append(texts)
        return False

    monkeypatch.setattr(found, "discloses", record)
    engine.tasks = [task("b1", "make gate")]
    assert lead(found, True) is not None
    assert calls == []


def test_an_identifier_that_is_not_plain_is_counted_and_not_shown(
    scheduler: tuple[Scheduler, Kept],
) -> None:
    found, engine = scheduler
    engine.tasks = [task("b1", "x"), task("run SECRETWORD now", "y")]
    text = lead(found, True)
    assert text is not None
    assert "holds 2 as running" in text and "by identifier b1." in text
    assert "1 more are not named here." in text
    assert "SECRETWORD" not in text


def test_a_long_roster_is_cut_with_the_count_of_the_rest(
    scheduler: tuple[Scheduler, Kept],
) -> None:
    found, engine = scheduler
    engine.tasks = [task(f"b{n}", "x") for n in range(MAX_ROSTER_TASKS + 3)]
    text = lead(found, True)
    assert text is not None
    assert f"holds {MAX_ROSTER_TASKS + 3} as running" in text
    assert "3 more are not named here." in text
    assert f"b{MAX_ROSTER_TASKS}" not in text


def test_a_pending_wake_for_a_task_is_named(
    scheduler: tuple[Scheduler, Kept],
) -> None:
    found, engine = scheduler
    engine.tasks = [task("b1", "make gate")]
    found.schedule_wake("alpha", 60, "look", "b1")
    wake = found._log.state.scheduled["alpha"]  # pyright: ignore[reportPrivateUsage]
    text = lead(found, True)
    assert text is not None
    assert f"Your pending wake {wake.message_id} waits for task b1." in text


def test_an_engine_that_is_not_kept_or_cannot_say_or_lists_badly_gives_nothing(
    scheduler: tuple[Scheduler, Kept],
) -> None:
    found, engine = scheduler
    engine.tasks = [task("b1", "x")]
    engine.kept = False
    assert lead(found, True) is None
    engine.kept = True
    engine.broken = True
    assert lead(found, True) is None
    engine.broken = False
    engine.tasks = ["not a mapping", {"task_id": "", "command": "x"}, {"command": "y"}]
    assert lead(found, True) is None
    found._engines["alpha"] = cast(Any, object())  # pyright: ignore[reportPrivateUsage]
    assert lead(found, True) is None


def test_the_turn_after_an_engine_compaction_leads_with_the_roster_once(
    scheduler: tuple[Scheduler, Kept],
) -> None:
    found, engine = scheduler
    engine.tasks = [task("b1", "make gate")]
    found.session_compacted(
        "alpha", {"trigger": "auto", "before_tokens": 900, "after_tokens": 100}
    )

    def turn(text: str) -> str:
        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(found.run(stop))
            await asyncio.sleep(0)
            found.send(ANA, LineAddress("alpha"), text)
            await found.idle()
            stop.set()
            await loop

        asyncio.run(go())
        return engine.prompts[-1]

    first = turn("one")
    assert "Dromon still holds 1 as running in your kept session" in first
    second = turn("two")
    assert "Dromon still holds" not in second
