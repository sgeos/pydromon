"""One ``line_status`` call copies the state once. **Round 14 task of 2026-11-01.**
Each call read the state 27 times, 49 with the lists, and each read copied every
message. The reads of one call now share one copy, and every answer is unchanged."""

import asyncio
import json
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.model import (
    HoldCause,
    LineAddress,
    MessageDelivered,
    OperatorAddress,
    State,
)
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ARGUMENTS: tuple[dict[str, object], ...] = (
    {},
    {"lists": True},
    {"detail": True},
    {"detail": True, "lists": True},
    {"lists": True, "other_lines_offset": 1},
)


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start here, but {spec.line_id} did")


@pytest.fixture
def scheduler(tmp_path: Path) -> Generator[Scheduler]:
    root = tmp_path.resolve() / "project"
    (root / ".dromon").mkdir(parents=True)
    log = EventLog(tmp_path / "events.sqlite", lambda: "2026-11-01T09:00:00.000000Z")
    found = Scheduler(
        log,
        no_engine,
        tick_seconds=0,
        default_operator="ana",
        root=root,
        protected=(root / ".dromon",),
    )
    found.ensure_operator("ana")
    for line_id in ("alpha", "beta", "gamma"):
        found.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
    for index in range(30):
        message = found.send(ANA, LineAddress(("alpha", "beta")[index % 2]), "a note")
        if index % 3:
            log.append(MessageDelivered(message))
    found.hold("gamma", "held for this test", cause=HoldCause.COST)
    found.set_leases(["gate"], 600)
    found.acquire_lease("beta", "gate", "the gate")
    yield found
    log.close()


def status_tool(scheduler: Scheduler) -> SdkMcpTool[Any]:
    return next(t for t in scheduler.own_tools("alpha") if t.name == "line_status")


def status(
    scheduler: Scheduler, args: dict[str, object], tool: SdkMcpTool[Any] | None = None
) -> str:
    tool = status_tool(scheduler) if tool is None else tool

    async def invoke() -> dict[str, Any]:
        return await tool.handler(args)

    result = asyncio.run(invoke())
    assert result.get("is_error") is not True, result
    return str(result["content"][0]["text"])


def without_volatile(text: str) -> dict[str, object]:
    """The response with the fields that read a clock or the process removed, so
    two calls in a row compare equal."""
    loaded = json.loads(text)
    for name in ("checked_at",):
        loaded.pop(name, None)
    return loaded


@pytest.mark.parametrize("args", ARGUMENTS)
def test_every_answer_is_the_same_as_with_a_copy_for_each_read(
    scheduler: Scheduler, monkeypatch: pytest.MonkeyPatch, args: dict[str, object]
) -> None:
    shared = without_volatile(status(scheduler, args))
    log = scheduler._log  # pyright: ignore[reportPrivateUsage]

    def fresh() -> State:
        return log.state

    monkeypatch.setattr(scheduler, "_state", fresh)
    assert without_volatile(status(scheduler, args)) == shared


@pytest.mark.parametrize("args", ARGUMENTS)
def test_one_call_copies_the_state_once(
    scheduler: Scheduler, monkeypatch: pytest.MonkeyPatch, args: dict[str, object]
) -> None:
    copies = {"count": 0}
    original = State.copy

    def counted(self: State) -> State:
        copies["count"] += 1
        return original(self)

    # Built first, since building the tools reads the state too, outside a call.
    tool = status_tool(scheduler)
    monkeypatch.setattr(State, "copy", counted)
    status(scheduler, args, tool)
    assert copies["count"] == 1


def test_a_read_after_the_log_moved_sees_the_new_state(scheduler: Scheduler) -> None:
    log = scheduler._log  # pyright: ignore[reportPrivateUsage]
    scheduler._snapshot = (log.last_seq, log.state)  # pyright: ignore[reportPrivateUsage]
    try:
        before = scheduler._state()  # pyright: ignore[reportPrivateUsage]
        assert scheduler._state() is before  # pyright: ignore[reportPrivateUsage]
        message = scheduler.send(ANA, LineAddress("gamma"), "a later note")
        after = scheduler._state()  # pyright: ignore[reportPrivateUsage]
        assert message in after.messages and message not in before.messages
    finally:
        scheduler._snapshot = None  # pyright: ignore[reportPrivateUsage]


def test_outside_a_status_call_each_read_is_a_fresh_copy(
    scheduler: Scheduler,
) -> None:
    first = scheduler._state()  # pyright: ignore[reportPrivateUsage]
    second = scheduler._state()  # pyright: ignore[reportPrivateUsage]
    assert first is not second
    assert scheduler._snapshot is None  # pyright: ignore[reportPrivateUsage]


def test_a_status_call_that_raises_leaves_no_snapshot(
    scheduler: Scheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("the status could not be built")

    monkeypatch.setattr(scheduler, "_line_status", broken)
    with pytest.raises(RuntimeError):
        scheduler.line_status("alpha")
    assert scheduler._snapshot is None  # pyright: ignore[reportPrivateUsage]
