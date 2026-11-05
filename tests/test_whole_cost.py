"""Cost estimated from tokens, and each line's whole cost and turns.

**Round 15, item 1, required by the operator.** A line whose engine reports tokens
and no cost had a cost limit that held nothing, and nothing kept a line's cost or
turns across a release or a restart. Codex's turns are now estimated from their
tokens and a price table, labelled as estimates, and ``line_cost_recorded`` keeps
every line's whole cost, which no release resets."""

import json
from collections.abc import Callable, Generator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import prices
from dromon.cli import _line_row  # pyright: ignore[reportPrivateUsage]
from dromon.codec import DecodeError, decode, encode
from dromon.driver import ENGINE_OF, Strategy
from dromon.engine import INTERRUPTED_KIND
from dromon.eventlog import EventLog
from dromon.limits import Limits
from dromon.peering import TurnCause
from dromon.model import (
    MAX_COST_MICRO_USD,
    HoldCause,
    InvalidField,
    LineCost,
    LineCostRecorded,
    LineCreated,
    RoleDeclared,
    SeatBound,
    State,
)
from dromon.scheduler import (
    TOKEN_PRICED_ENGINES,
    WHOLE_COST_SINCE,
    LineEngine,
    LineSpec,
    Scheduler,
)

CODEX = "gpt-6-luna"
GROK = "grok-4.7"
HAIKU = "claude-haiku-5-5"
USAGE = {"input_tokens": 1_000_000, "cached_input_tokens": 400_000, "output_tokens": 10}


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start here, but {spec.line_id} did")


class World:
    def __init__(self, path: Path, limits: Limits | None = None) -> None:
        self.path = path
        self.limits = limits
        self.log = EventLog(path)
        self.scheduler = self.start()

    def start(self) -> Scheduler:
        if self.limits is None:
            return Scheduler(self.log, no_engine, tick_seconds=0)
        return Scheduler(self.log, no_engine, tick_seconds=0, limits=self.limits)

    def restart(self) -> None:
        self.log.close()
        self.log = EventLog(self.path)
        self.scheduler = self.start()


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    target = World(tmp_path / "events.sqlite")
    target.scheduler.create_line("codex", CODEX, Strategy.SUBSCRIPTION)
    target.scheduler.create_line("grok", GROK, Strategy.SUBSCRIPTION)
    target.scheduler.create_line("claude", HAIKU, Strategy.SUBSCRIPTION)
    yield target
    target.log.close()


def expected_micro(usage: Mapping[str, int], price: prices.Price) -> int:
    """The estimate computed from its definition, rounded up."""
    cached = min(usage.get("cached_input_tokens", 0), usage["input_tokens"])
    product = (
        (usage["input_tokens"] - cached) * price.input_micro
        + cached * price.cached_input_micro
        + usage["output_tokens"] * price.output_micro
    )
    return -(-product // prices.TOKENS_PER_PRICE)


# The price table.


@pytest.mark.parametrize("model", sorted(prices.PRICES))
def test_each_priced_model_is_estimated_from_its_tokens(model: str) -> None:
    found = prices.estimate_micro_usd(model, USAGE)
    assert found == expected_micro(USAGE, prices.PRICES[model])
    assert found is not None and found > 0


def test_a_cost_is_rounded_up_never_down() -> None:
    one = {"input_tokens": 1, "output_tokens": 0}
    assert prices.estimate_micro_usd(CODEX, one) == 1


def test_cached_tokens_past_the_input_count_are_read_as_all_cached() -> None:
    usage = {"input_tokens": 100, "cached_input_tokens": 500, "output_tokens": 0}
    price = prices.PRICES[CODEX]
    assert prices.estimate_micro_usd(CODEX, usage) == -(
        -100 * price.cached_input_micro // prices.TOKENS_PER_PRICE
    )


@pytest.mark.parametrize(
    ("model", "usage"),
    [
        (HAIKU, USAGE),
        (CODEX, None),
        (CODEX, {"output_tokens": 5}),
        (CODEX, {"input_tokens": 5}),
        (CODEX, {"input_tokens": -1, "output_tokens": 5}),
        (CODEX, {"input_tokens": prices.MAX_TURN_TOKENS + 1, "output_tokens": 5}),
    ],
)
def test_no_estimate_without_a_price_or_both_counts(
    model: str, usage: Mapping[str, int] | None
) -> None:
    assert prices.estimate_micro_usd(model, usage) is None


def test_a_negative_price_is_refused() -> None:
    with pytest.raises(ValueError, match="output_micro"):
        prices.Price(1, 1, -1)


def test_the_table_says_whether_its_prices_were_verified() -> None:
    assert prices.PRICES_VERIFIED is False
    assert "unverified" in prices.ESTIMATE_NOTE


# The event.


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (lambda: LineCostRecorded("a", -1), "cost_micro_usd"),
        (lambda: LineCostRecorded("a", MAX_COST_MICRO_USD + 1), "cost_micro_usd"),
        (lambda: LineCostRecorded("a", 1, estimated=False), "estimated must be"),
        (lambda: LineCostRecorded("a", 1, between=False), "between must be"),
        (lambda: LineCostRecorded("a", None, estimated=True), "needs a cost"),
    ],
)
def test_a_malformed_cost_record_is_refused(
    event: Callable[[], LineCostRecorded], message: str
) -> None:
    with pytest.raises(InvalidField, match=message):
        event()


def test_the_wire_leaves_out_null_flags_and_refuses_false() -> None:
    plain = LineCostRecorded("a", 5)
    data = json.loads(encode(plain))["data"]
    assert data == {"line_id": "a", "cost_micro_usd": 5}
    assert decode(encode(plain)) == plain
    data["estimated"] = False
    with pytest.raises(DecodeError):
        decode(json.dumps({"v": 1, "kind": "line_cost_recorded", "data": data}))


def test_the_state_sums_each_kind_apart_and_holds_at_the_bound() -> None:
    state = State()
    state.apply(LineCreated("a", HAIKU, "subscription"))
    for event in (
        LineCostRecorded("a", 10),
        LineCostRecorded("a", 3, estimated=True),
        LineCostRecorded("a", None),
        LineCostRecorded("a", 7, between=True),
        LineCostRecorded("a", MAX_COST_MICRO_USD),
    ):
        state.apply(event)
    assert state.line_costs["a"] == LineCost(
        turns=4,
        between=1,
        reported_micro_usd=MAX_COST_MICRO_USD,
        estimated_micro_usd=3,
        unknown=1,
    )
    assert state.line_costs["a"].total_micro_usd == MAX_COST_MICRO_USD


def test_a_record_for_an_unknown_line_is_rejected() -> None:
    with pytest.raises(Exception, match="no line"):
        State().apply(LineCostRecorded("nobody", 1))


# The scheduler.


def test_a_codex_spend_is_estimated_counted_and_labelled(world: World) -> None:
    assert ENGINE_OF[CODEX] in TOKEN_PRICED_ENGINES
    assert ENGINE_OF[GROK] not in TOKEN_PRICED_ENGINES
    world.scheduler.spent_between("codex", INTERRUPTED_KIND, None, USAGE)
    micro = expected_micro(USAGE, prices.PRICES[CODEX])
    assert world.log.state.line_costs["codex"].estimated_micro_usd == micro
    status = world.scheduler.cost_status("codex")
    assert status["limit_enforced"] is True
    assert status["estimated_turns"] == 1 and status["basis"] == "exact"
    assert status["estimate"] == f"{micro / 1_000_000:.4f}"
    assert prices.ESTIMATE_NOTE in str(status["note"])
    line = world.scheduler._budget_line(  # pyright: ignore[reportPrivateUsage]
        "codex", TurnCause.AUTONOMOUS
    )
    assert prices.ESTIMATE_NOTE in line and "does not apply" not in line


def test_an_estimated_cost_past_the_limit_holds_the_line(tmp_path: Path) -> None:
    world = World(tmp_path / "events.sqlite", Limits(max_cost_usd=0.1))
    try:
        world.scheduler.create_line("codex", CODEX, Strategy.SUBSCRIPTION)
        world.scheduler.spent_between("codex", INTERRUPTED_KIND, None, USAGE)
        assert world.log.state.lines["codex"].held_by is not None
    finally:
        world.log.close()


def test_a_line_without_tokens_keeps_an_unknown_cost(world: World) -> None:
    world.scheduler.spent_between("grok", INTERRUPTED_KIND, None, None)
    world.scheduler.spent_between("codex", INTERRUPTED_KIND, None, None)
    for line_id in ("grok", "codex"):
        assert world.log.state.line_costs[line_id].unknown == 1
    assert world.scheduler.cost_status("grok")["limit_enforced"] is False
    assert world.scheduler.cost_status("codex")["estimate"] == "unknown"


def test_a_reported_cost_is_never_replaced_by_an_estimate(world: World) -> None:
    world.scheduler.spent_between("codex", INTERRUPTED_KIND, 0.25, USAGE)
    found = world.log.state.line_costs["codex"]
    assert (found.reported_micro_usd, found.estimated_micro_usd) == (250_000, 0)


def test_the_whole_cost_survives_a_release_and_a_restart(world: World) -> None:
    world.scheduler.spent_between("claude", INTERRUPTED_KIND, 0.5, None)
    world.scheduler.spent_between("codex", INTERRUPTED_KIND, None, USAGE)
    before = {
        line_id: world.scheduler.whole_cost(line_id) for line_id in ("claude", "codex")
    }
    world.scheduler.hold("claude", "was held for this test", cause=HoldCause.COST)
    world.scheduler.release("claude")
    assert world.scheduler.cost_status("claude")["estimate"] == "0.0000"
    assert world.scheduler.whole_cost("claude") == before["claude"]
    world.restart()
    for line_id, whole in before.items():
        assert world.scheduler.whole_cost(line_id) == whole
    assert before["claude"] == {
        "usd": "0.500000",
        "reported_usd": "0.500000",
        "estimated_usd": "0.000000",
        "turns": 0,
        "between_turns": 1,
        "unknown_cost": 0,
        "since": WHOLE_COST_SINCE,
    }


def test_status_and_role_views_give_the_whole_cost(world: World) -> None:
    world.scheduler.spent_between("claude", INTERRUPTED_KIND, 0.5, None)
    summaries = {
        str(entry["line_id"]): entry for entry in world.scheduler.line_summaries()
    }
    assert summaries["claude"]["cost_whole"] == world.scheduler.whole_cost("claude")
    row = _line_row(summaries["claude"])
    assert "$0.500000 in 0 turns in all" in row
    world.scheduler.spent_between("codex", INTERRUPTED_KIND, None, USAGE)
    world.scheduler.spent_between("codex", INTERRUPTED_KIND, None, None)
    codex_row = _line_row(
        next(
            entry
            for entry in world.scheduler.line_summaries()
            if entry["line_id"] == "codex"
        )
    )
    assert "of it estimated" in codex_row and "1 of unknown cost" in codex_row


def test_a_row_from_an_older_daemon_names_no_whole_cost() -> None:
    row = _line_row({"line_id": "a", "condition": "idle", "pending": 0})
    assert row == "line a, idle, 0 pending"


def test_role_status_gives_each_seat_its_whole_cost(world: World) -> None:
    world.log.append(RoleDeclared("core", "."))
    world.log.append(SeatBound("claude", "core", True))
    world.log.append(SeatBound("codex", "core", True))
    world.scheduler.spent_between("codex", INTERRUPTED_KIND, None, USAGE)
    seats = cast(list[dict[str, object]], world.scheduler.role_view("core")["seats"])
    by_line = {str(seat["line_id"]): seat["cost_whole"] for seat in seats}
    assert by_line == {
        line_id: world.scheduler.whole_cost(line_id) for line_id in ("claude", "codex")
    }
