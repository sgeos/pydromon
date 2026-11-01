"""Every packaged model can be recorded as a line's model.

The ``engine`` field of ``line_created`` and ``line_rebound`` was checked against the
identifier pattern, which refuses the colon and slash of the engine-qualified names of
Pi, Cursor Agent and Antigravity, so no line could be created on those engines. The
tests go through ``Scheduler.create_line`` and ``rebind_line`` for each packaged model,
through the log's replay, and through the edges of the model-name grammar.
"""

import json
import sqlite3
from collections.abc import Callable, Generator, Sequence
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import antigravity, cursor
from dromon.codec import encode
from dromon.driver import ENGINE_OF, MODELS, Strategy, harness_id
from dromon.engine import TurnReport
from dromon.eventlog import EventLog, LogCorrupt, replay
from dromon.lineconfig import DEFAULT_CONFIG, ConfigError, LineConfig
from dromon.model import (
    ID_PATTERN,
    InvalidField,
    LineCreated,
    LineRebound,
    State,
)
from dromon.scheduler import LineSpec, Scheduler, cost_bound_refusal

HAIKU = "claude-haiku-5-5"
QUALIFIED = tuple(model for model in MODELS if ":" in model)
"""The engine-qualified models, those the identifier pattern refuses."""

LINE_REFUSALS: dict[str, Callable[[LineConfig], str | None]] = {
    cursor.ENGINE_NAME: cursor.line_refusal,
    antigravity.ENGINE_NAME: antigravity.line_refusal,
}
"""The engines whose adapters refuse a daemon's line with any configuration."""


def daemon_refusal(model: str) -> str | None:
    """Why a daemon refuses a line on ``model`` with the default configuration."""
    check = LINE_REFUSALS.get(ENGINE_OF[model])
    return None if check is None else check(DEFAULT_CONFIG)


HELD = tuple(model for model in MODELS if daemon_refusal(model) is None)
PROBE_ONLY = tuple(model for model in MODELS if daemon_refusal(model) is not None)


class Unused:
    """An engine no test runs, since only the recorded binding is under test."""

    def __init__(self, spec: LineSpec) -> None:
        self.spec = spec
        self.session_id = spec.session_id

    async def run_turn(self, prompt: str) -> TurnReport:
        raise AssertionError(f"no turn runs in these tests: {prompt!r}")


def build(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Unused:
    del tools
    return Unused(spec)


@pytest.fixture
def scheduler(tmp_path: Path) -> Generator[Scheduler]:
    log = EventLog(tmp_path / "events.sqlite")
    yield Scheduler(
        log,
        build,
        tick_seconds=0,
        wall_clock=lambda: datetime(2026, 10, 31, tzinfo=UTC),
    )
    log.close()


def test_the_qualified_models_are_the_ones_the_identifier_pattern_refuses() -> None:
    """The defect's premise, derived from the catalogue rather than listed, so a model
    added later is covered by the tests below."""
    refused = tuple(m for m in MODELS if ID_PATTERN.fullmatch(m) is None)
    assert refused == QUALIFIED
    assert {ENGINE_OF[m] for m in QUALIFIED} == {"pi", "cursor-agent", "antigravity"}
    assert {ENGINE_OF[m] for m in PROBE_ONLY} == set(LINE_REFUSALS)
    assert any(m in HELD for m in QUALIFIED)


@pytest.mark.parametrize("strategy", list(Strategy))
@pytest.mark.parametrize("model", HELD)
def test_a_line_can_be_created_on_every_packaged_model_a_daemon_may_hold(
    scheduler: Scheduler, model: str, strategy: Strategy
) -> None:
    refusal = cost_bound_refusal(ENGINE_OF[model], strategy)
    if refusal is not None:
        # Review of a9995d8, item 4. The api strategy is refused where no cost is
        # reported, since nothing would bound what the line spends.
        with pytest.raises(ConfigError, match="reports no cost"):
            scheduler.create_line("alpha", model, strategy)
        assert "alpha" not in scheduler.state.lines
        return
    scheduler.create_line("alpha", model, strategy)
    line = scheduler.state.lines["alpha"]
    assert (line.engine, line.strategy) == (model, strategy.value)
    assert scheduler.model_of("alpha") == model


@pytest.mark.parametrize("model", PROBE_ONLY)
def test_a_probe_only_model_is_refused_for_isolation_not_for_its_name(
    scheduler: Scheduler, model: str
) -> None:
    """Cursor Agent and Antigravity lines are refused on purpose, before the event is
    built, so the name pattern was a second refusal hidden behind the first. The name
    alone is now accepted, so lifting the isolation refusal needs no grammar change."""
    reason = daemon_refusal(model)
    assert reason is not None
    with pytest.raises(ConfigError) as refused:
        scheduler.create_line("alpha", model, Strategy.SUBSCRIPTION)
    assert str(refused.value) == reason
    assert "alpha" not in scheduler.state.lines
    assert LineCreated("alpha", model, "subscription").engine == model


@pytest.mark.parametrize("model", tuple(m for m in QUALIFIED if m in HELD))
def test_a_line_can_be_rebound_onto_and_off_a_qualified_model(
    scheduler: Scheduler, model: str
) -> None:
    scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
    scheduler.rebind_line("alpha", model, Strategy.SUBSCRIPTION)
    assert scheduler.state.lines["alpha"].engine == model
    scheduler.rebind_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
    assert scheduler.state.lines["alpha"].engine == HAIKU


@pytest.mark.parametrize("model", QUALIFIED)
def test_a_log_holding_a_qualified_model_replays(tmp_path: Path, model: str) -> None:
    path = tmp_path / "events.sqlite"
    with EventLog(path) as log:
        log.append(LineCreated("alpha", model, "subscription"))
        log.append(LineRebound("alpha", HAIKU, "api"))
        log.append(LineRebound("alpha", model, "api"))
    assert replay(path).lines["alpha"].engine == model
    with EventLog(path) as reopened:
        assert reopened.state.lines["alpha"].engine == model


def raw_created(engine: str) -> str:
    """A ``line_created`` payload with any ``engine`` text, as a hand-written or
    foreign log could hold it, since ``encode`` builds only valid events."""
    document = json.loads(encode(LineCreated("alpha", HAIKU, "subscription")))
    document["data"]["engine"] = engine
    return json.dumps(document)


MALFORMED = (
    "",
    "Pi:openai/gpt-6-luna",
    "pi:OpenAI/gpt-6-luna",
    "pi:",
    ":gpt-6-luna",
    "pi:/gpt-6-luna",
    "pi:a:b",
    "pi/openai:gpt-6-luna",
    "@ana",
    "&agent",
    "!dromon",
    "pi:openai/gpt 6",
    "pi:openai/gpt-6-luné",
    "e" * 65,
    "pi:" + "m" * 129,
)


@pytest.mark.parametrize("engine", MALFORMED)
def test_a_malformed_model_name_is_refused_and_a_log_holding_it_is_corrupt(
    tmp_path: Path, engine: str
) -> None:
    with pytest.raises(InvalidField, match="engine is not a valid model name"):
        LineCreated("alpha", engine, "subscription")
    with pytest.raises(InvalidField, match="engine is not a valid model name"):
        LineRebound("alpha", engine, "subscription")
    path = tmp_path / "events.sqlite"
    EventLog(path).close()
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        connection.execute(
            "INSERT INTO events (recorded_at, payload) VALUES ('t', ?)",
            (raw_created(engine),),
        )
    with pytest.raises(LogCorrupt, match="event 1 does not decode"):
        replay(path)


def test_the_longest_model_names_are_accepted() -> None:
    """The bounds are inclusive, 64 bytes for the engine part and 128 for the
    harness's identifier, the latter derived as the part after the colon."""
    plain = "e" * 64
    qualified = "e" * 64 + ":" + "m" * 128
    assert harness_id(qualified) == "m" * 128
    state = State()
    state.apply(LineCreated("alpha", plain, "subscription"))
    state.apply(LineCreated("beta", qualified, "subscription"))
    assert (state.lines["alpha"].engine, state.lines["beta"].engine) == (
        plain,
        qualified,
    )


def test_the_line_and_strategy_fields_are_still_identifiers() -> None:
    """Only the model field was widened."""
    with pytest.raises(InvalidField, match="line_id is not a valid identifier"):
        LineCreated("a:b", HAIKU, "subscription")
    with pytest.raises(InvalidField, match="strategy is not a valid identifier"):
        LineCreated("alpha", HAIKU, "sub/scription")
    with pytest.raises(InvalidField, match="line_id is not a valid identifier"):
        LineRebound("a/b", HAIKU, "subscription")
