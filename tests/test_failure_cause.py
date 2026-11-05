"""The cause of a failed turn. **Round 15 follow-up of 2026-11-05.** A turn could end
as failed with no reason recorded anywhere, since ``line_turn_ended`` carried only the
fact. Each failed turn now records one fixed word for its cause, with the daemon's
exception type or the engine's listed result subtype as its class where one exists,
and never the engine's own text, and ``line_status`` and the daemon log show it. Each
cause is reached here through a real turn of the scheduler where one can be, and
through the classifier where the path cannot be reached without a race."""

import asyncio
import json
from collections.abc import Callable, Generator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.codec import DecodeError, decode, encode
from dromon.driver import Strategy
from dromon.engine import (
    ConfinementBreach,
    EngineClosed,
    EngineRefused,
    TurnReport,
)
from dromon.eventlog import EventLog, read_entries
from dromon.limits import Limits
from dromon.model import (
    FailureCause,
    InvalidField,
    LineAddress,
    LineTurnEnded,
    OperatorAddress,
)
from dromon.scheduler import (
    RESULT_FAILURES,
    LineSpec,
    Scheduler,
    failure_text,
    raised_failure,
    reported_failure,
)

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ENGINE_TEXT = "the engine said something private about /a/path and a token"
"""Text an engine might give, which no recorded cause may carry."""


class Recorder:
    """An engine whose next turn runs the action its world holds, which may raise,
    and otherwise reports the subtype its world holds, ``success`` when unset."""

    def __init__(self, world: "World", spec: LineSpec) -> None:
        self.world = world
        self.spec = spec
        self.session_id = spec.session_id

    async def run_turn(self, prompt: str) -> TurnReport:
        del prompt
        action = self.world.actions.pop(self.spec.line_id, None)
        if action is not None:
            action()
        subtype = self.world.subtypes.pop(self.spec.line_id, "success")
        return TurnReport(subtype, 1, "ok", (), None, None, "session-1", None, 0.0)


class World:
    def __init__(self, path: Path, root: Path) -> None:
        self.path = path
        self.root = root
        self.actions: dict[str, Callable[[], None]] = {}
        self.subtypes: dict[str, str] = {}
        self.build_error: Exception | None = None
        """An error building the engine raises, before any engine runs."""
        self.log = EventLog(path)
        self.scheduler = self.start()
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
        del tools
        if self.build_error is not None:
            raise self.build_error
        return Recorder(self, spec)

    def start(self) -> Scheduler:
        return Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            limits=Limits(max_autonomous_turns=50, max_cost_usd=30.0),
            root=self.root,
            protected=(self.root / ".dromon",),
        )

    def restart(self) -> None:
        self.log.close()
        self.log = EventLog(self.path)
        self.scheduler = self.start()

    def turn(self) -> None:
        """Run one turn of ``alpha``, woken by an operator's note."""

        async def settle() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            await asyncio.sleep(0)
            self.scheduler.send(ANA, LineAddress("alpha"), "go on")
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(settle())

    def status(self, *, detail: bool) -> dict[str, Any]:
        tool = next(
            t for t in self.scheduler.own_tools("alpha") if t.name == "line_status"
        )

        async def invoke() -> dict[str, Any]:
            return await tool.handler({"detail": detail})

        result = asyncio.run(invoke())
        assert result.get("is_error") is not True
        return cast(dict[str, Any], json.loads(result["content"][0]["text"]))

    def ends(self) -> list[LineTurnEnded]:
        return [
            entry.event
            for entry in read_entries(self.path)
            if isinstance(entry.event, LineTurnEnded) and entry.event.line_id == "alpha"
        ]


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    root = tmp_path.resolve() / "project"
    (root / ".dromon").mkdir(parents=True)
    target = World(tmp_path / "events.sqlite", root)
    yield target
    target.log.close()


def raising(error: Exception) -> Callable[[], None]:
    def act() -> None:
        raise error

    return act


def last_end(world: World) -> LineTurnEnded:
    return world.ends()[-1]


def assert_recorded(world: World, cause: FailureCause, error_class: str | None) -> None:
    """The latest turn's end records ``cause`` and ``error_class``, and both views of
    ``line_status`` give them. ``current`` follows the line's own failed flag, which
    the notice of a hold clears, as ``Line.failed`` says."""
    end = last_end(world)
    assert (end.failed, end.failure, end.failure_class) == (True, cause, error_class)
    expected = {
        "cause": cause.value,
        "error_class": error_class,
        "current": world.log.state.lines["alpha"].failed,
    }
    assert world.status(detail=False)["last_failure"] == expected
    assert world.status(detail=True)["last_failure"] == expected


# Each cause through a real turn.


def test_a_turn_that_succeeds_records_no_failure(world: World) -> None:
    world.turn()
    end = last_end(world)
    assert (end.failed, end.failure, end.failure_class) == (None, None, None)
    assert world.status(detail=True)["last_failure"] is None


def test_an_engine_that_raises_records_engine_error_and_its_type(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.actions["alpha"] = raising(RuntimeError(ENGINE_TEXT))
    world.turn()
    assert_recorded(world, FailureCause.ENGINE_ERROR, "RuntimeError")
    assert (
        "dromond: line alpha turn ended as failed, cause engine_error, class"
        " RuntimeError"
    ) in capsys.readouterr().err


def test_a_line_not_held_for_its_failure_shows_it_as_current(world: World) -> None:
    """A line that mailed an operator during its failed turn is not held, so it
    stands failed and its failure is current. A refusal, which a closed engine also
    is, sends the line a notice of the stop, which clears the flag, so a plain error
    is used here."""

    def act() -> None:
        world.scheduler.send(LineAddress("alpha"), ANA, "this turn is about to fail")
        raise RuntimeError(ENGINE_TEXT)

    world.actions["alpha"] = act
    world.turn()
    assert world.log.state.lines["alpha"].held_by is None
    assert world.status(detail=True)["last_failure"] == {
        "cause": "engine_error",
        "error_class": "RuntimeError",
        "current": True,
    }


def test_an_engine_that_closes_records_engine_closed(world: World) -> None:
    world.actions["alpha"] = raising(EngineClosed(ENGINE_TEXT))
    world.turn()
    assert_recorded(world, FailureCause.ENGINE_CLOSED, "EngineClosed")


@pytest.mark.parametrize("error", [EngineRefused, ConfinementBreach])
def test_a_refusal_records_refused_and_its_type(
    world: World, error: type[EngineRefused]
) -> None:
    world.actions["alpha"] = raising(error(ENGINE_TEXT))
    world.turn()
    assert_recorded(world, FailureCause.REFUSED, error.__name__)


def test_an_engine_that_cannot_be_built_records_not_started(world: World) -> None:
    world.build_error = OSError(ENGINE_TEXT)
    world.turn()
    assert_recorded(world, FailureCause.NOT_STARTED, "OSError")


def test_a_refusal_before_the_engine_runs_is_a_refusal(world: World) -> None:
    """A refusal names its check, which says more than that the engine never ran."""
    world.build_error = EngineRefused(ENGINE_TEXT)
    world.turn()
    assert_recorded(world, FailureCause.REFUSED, "EngineRefused")


@pytest.mark.parametrize(
    ("subtype", "cause"), sorted(RESULT_FAILURES.items(), key=lambda item: item[0])
)
def test_each_listed_subtype_records_its_cause_and_itself_as_class(
    world: World, subtype: str, cause: FailureCause
) -> None:
    world.subtypes["alpha"] = subtype
    world.turn()
    assert_recorded(world, cause, subtype)


def test_an_unlisted_subtype_records_engine_reported_without_the_engine_text(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.subtypes["alpha"] = ENGINE_TEXT
    world.turn()
    assert_recorded(world, FailureCause.ENGINE_REPORTED, None)
    assert ENGINE_TEXT not in encode(last_end(world))
    assert (
        "dromond: line alpha turn ended as failed, cause engine_reported\n"
        in capsys.readouterr().err
    )


def test_a_failure_after_the_engine_reported_records_unknown(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(line_id: str, report: TurnReport) -> None:
        del line_id, report
        raise KeyError(ENGINE_TEXT)

    monkeypatch.setattr(world.scheduler, "_record_session", broken)
    world.turn()
    assert_recorded(world, FailureCause.UNKNOWN, "KeyError")


def test_a_failed_turn_no_path_named_records_unknown(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    """The end of a turn the scheduler marked failed without naming a cause still
    records one, ``unknown``, so no failed turn is recorded without a cause."""
    world.scheduler._failed.add("alpha")  # pyright: ignore[reportPrivateUsage]
    world.scheduler._end_turn("alpha", None, None)  # pyright: ignore[reportPrivateUsage]
    end = last_end(world)
    assert (end.failed, end.failure, end.failure_class) == (
        True,
        FailureCause.UNKNOWN,
        None,
    )
    assert "cause unknown" in capsys.readouterr().err


# What the line and the log keep.


def test_the_failure_stays_shown_after_a_later_turn_clears_failed(
    world: World,
) -> None:
    world.actions["alpha"] = raising(EngineClosed(ENGINE_TEXT))
    world.turn()
    world.scheduler.release("alpha")
    world.turn()
    assert world.status(detail=True)["last_failure"] == {
        "cause": "engine_closed",
        "error_class": "EngineClosed",
        "current": False,
    }


def test_a_later_failure_replaces_the_earlier(world: World) -> None:
    world.actions["alpha"] = raising(EngineClosed(ENGINE_TEXT))
    world.turn()
    world.scheduler.release("alpha")
    world.subtypes["alpha"] = "error_max_turns"
    world.turn()
    assert_recorded(world, FailureCause.LIMIT, "error_max_turns")


def test_the_failure_survives_a_restart(world: World) -> None:
    world.actions["alpha"] = raising(RuntimeError(ENGINE_TEXT))
    world.turn()
    before = world.status(detail=True)["last_failure"]
    world.restart()
    assert world.status(detail=True)["last_failure"] == before


def test_a_failure_logged_before_causes_reads_as_unknown(world: World) -> None:
    world.log.append(LineTurnEnded("alpha", None, None, True))
    assert world.status(detail=True)["last_failure"] == {
        "cause": "unknown",
        "error_class": None,
        "current": True,
    }


def test_the_daemon_status_summary_gives_the_failure(world: World) -> None:
    world.actions["alpha"] = raising(EngineClosed(ENGINE_TEXT))
    world.turn()
    (summary,) = world.scheduler.line_summaries()
    assert summary["last_failure"] == world.status(detail=True)["last_failure"]
    assert summary["last_failure"] is not None


# The classifier, for the paths a real turn reaches only through a race.


def test_an_error_while_an_operator_stops_the_turn_is_interrupted() -> None:
    """An operator's stop cancels the turn, and a turn cancelled cleanly is not a
    failure. An engine that raises its own error as it is cancelled reaches the
    handler with the line marked interrupted, which only a race in a real turn
    reaches, so the classifier is asked directly."""
    assert raised_failure(
        EngineClosed(ENGINE_TEXT), started=True, returned=False, interrupted=True
    ) == (FailureCause.INTERRUPTED, "EngineClosed")


@pytest.mark.parametrize(
    ("error", "started", "returned", "cause"),
    [
        (RuntimeError(), False, False, FailureCause.NOT_STARTED),
        (RuntimeError(), True, False, FailureCause.ENGINE_ERROR),
        (RuntimeError(), True, True, FailureCause.UNKNOWN),
        (EngineRefused(), False, False, FailureCause.REFUSED),
        (EngineRefused(), True, False, FailureCause.REFUSED),
        (EngineClosed(), True, False, FailureCause.ENGINE_CLOSED),
        (EngineClosed(), True, True, FailureCause.UNKNOWN),
    ],
)
def test_the_stage_and_type_choose_the_cause(
    error: Exception, started: bool, returned: bool, cause: FailureCause
) -> None:
    assert raised_failure(
        error, started=started, returned=returned, interrupted=False
    ) == (cause, type(error).__name__)


def test_a_type_name_that_is_not_an_identifier_gives_no_class() -> None:
    odd = cast(type[Exception], type("not an identifier", (Exception,), {}))
    long = cast(type[Exception], type("E" * 65, (Exception,), {}))
    for kind in (odd, long):
        assert raised_failure(
            kind(), started=True, returned=False, interrupted=False
        ) == (FailureCause.ENGINE_ERROR, None)


def test_an_unlisted_subtype_gives_no_class() -> None:
    assert reported_failure("success but not") == (FailureCause.ENGINE_REPORTED, None)


def test_every_listed_subtype_is_a_valid_class() -> None:
    for subtype, cause in RESULT_FAILURES.items():
        LineTurnEnded("alpha", None, None, True, cause, subtype)


def test_the_log_text_gives_the_class_only_when_there_is_one() -> None:
    assert failure_text((FailureCause.LIMIT, None)) == "limit"
    assert failure_text((FailureCause.LIMIT, "length")) == "limit, class length"


# The record's own checks and its encoding.


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ((None, None, FailureCause.UNKNOWN), "failure is given only with failed"),
        ((None, True, None, "RuntimeError"), "failure_class is given only with"),
        ((None, True, FailureCause.UNKNOWN, "has space"), "failure_class must be"),
        ((None, True, FailureCause.UNKNOWN, "E" * 65), "failure_class must be"),
        ((None, True, FailureCause.UNKNOWN, ""), "failure_class must be"),
        ((None, True, "unknown"), "failure must be a failure cause"),
    ],
)
def test_the_record_refuses_a_malformed_failure(
    fields: tuple[object, ...], message: str
) -> None:
    with pytest.raises(InvalidField, match=message):
        LineTurnEnded("alpha", None, *fields)  # pyright: ignore[reportArgumentType]


def test_the_wire_omits_the_failure_when_null_and_round_trips_it() -> None:
    plain = LineTurnEnded("alpha", None)
    assert "failure" not in json.loads(encode(plain))["data"]
    failed = LineTurnEnded("alpha", None, None, True, FailureCause.LIMIT, "length")
    data = json.loads(encode(failed))["data"]
    assert (data["failure"], data["failure_class"]) == ("limit", "length")
    assert decode(encode(failed)) == failed


def test_the_wire_refuses_a_cause_it_does_not_know() -> None:
    document = json.loads(encode(LineTurnEnded("alpha", None, None, True)))
    document["data"]["failure"] = "the engine's own words"
    with pytest.raises(DecodeError, match="failure has no value"):
        decode(json.dumps(document))
