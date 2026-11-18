"""Round 18, item 1. The bounded index of a line's own evidence that a new engine
session is given, as ``dromon.evidence`` specifies."""

from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, NoReturn

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import evidence_index as evidence
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.evidence_index import MAX_EVIDENCE_BYTES, cut_text, evidence_index
from dromon.model import (
    LineAddress,
    Message,
    MessageKind,
    OperatorAddress,
    QuestionStatus,
    RoleNoted,
    RoleNoteKind,
    Run,
    RunEnded,
    RunStarted,
    RunStatus,
    State,
)
from dromon.scheduler import LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
AT = "2026-11-18T07:00:00Z"
ALPHA, ANA = LineAddress("alpha"), OperatorAddress("ana")
COMMIT = "c" * 40


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> NoReturn:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


class World:
    def __init__(self, path: Path) -> None:
        self.log = EventLog(path)
        self.scheduler = Scheduler(self.log, no_engine, hold_wakes=True)
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        self.scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    found = World(tmp_path / "events.sqlite")
    yield found
    found.log.close()


def run(run_id: str, line_id: str, exit_code: int | None) -> Run:
    started = RunStarted(
        run_id,
        line_id,
        None,
        None,
        "full-gate",
        ("./scripts/check.sh",),
        "/work",
        f"tmp/runs/{run_id}.out",
        4321,
        AT,
        AT,
        workdir_commit=COMMIT,
    )
    ended = (
        None
        if exit_code is None
        else RunEnded(run_id, RunStatus.EXITED, exit_code, None, None, "", AT)
    )
    return Run(started, ended)


def note(entry_id: str, line_id: str, body: str) -> RoleNoted:
    return RoleNoted(
        "core", entry_id, line_id, RoleNoteKind.NOTE, body, COMMIT, COMMIT, AT
    )


def question(body: str) -> Message:
    return Message(
        ALPHA, ANA, MessageKind.QUESTION, body, (), None, status=QuestionStatus.OPEN
    )


def with_evidence(world: World) -> tuple[State, list[str]]:
    send = world.scheduler.send
    reports = [send(ALPHA, ANA, f"report {n}\nsecond line") for n in range(7)]
    send(LineAddress("beta"), ANA, "not alpha's")
    state = world.log.state
    state = replace(
        state,
        messages={**state.messages, "m-question0001": question("may I merge?")},
        open_asked={"alpha": {"m-question0001": None}},
        runs={
            "r-old": run("r-old", "alpha", 1),
            "r-beta": run("r-beta", "beta", 0),
            "r-new": run("r-new", "alpha", 0),
            "r-open": run("r-open", "alpha", None),
        },
        role_entries={
            "e-mine": note("e-mine", "alpha", "please add mail --since"),
            "e-other": note("e-other", "beta", "not alpha's note"),
        },
    )
    return state, reports


def test_the_index_names_the_lines_own_evidence_newest_first(
    world: World,
) -> None:
    state, reports = with_evidence(world)
    index = evidence_index(state, "alpha")
    assert index is not None and index.startswith("EVIDENCE INDEX.")
    # The newest notes to operators, at most MAX_REPORTS, first line only.
    shown = [m for m in reports if m in index]
    assert shown == reports[-evidence.MAX_REPORTS :]
    assert index.index(reports[-1]) < index.index(reports[-2])
    assert "second line" not in index and "not alpha's" not in index
    # Runs of alpha only, newest first, with the commit each tested.
    assert "r-beta" not in index
    assert index.index("r-open") < index.index("r-new") < index.index("r-old")
    assert f"commit {COMMIT[:12]}" in index and "with exit code 1" in index
    assert "still open" in index
    assert "m-question0001" in index and "may I merge?" in index
    assert "e-mine" in index and "e-other" not in index
    assert len(index.encode("utf-8")) <= MAX_EVIDENCE_BYTES


def test_a_line_with_no_evidence_or_an_unknown_line_has_no_index(
    world: World,
) -> None:
    state = world.log.state
    assert evidence_index(state, "alpha") is None
    assert evidence_index(state, "nobody") is None


def test_the_index_is_bounded_and_says_what_it_left_out(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    long = "x" * 5000
    for _ in range(evidence.MAX_REPORTS):
        world.scheduler.send(ALPHA, ANA, long)
    state = world.log.state
    monkeypatch.setattr(evidence, "MAX_EVIDENCE_BYTES", 600)
    index = evidence_index(state, "alpha")
    assert index is not None
    assert len(index.encode("utf-8")) <= 600
    assert "further entries were left out" in index
    # Each quote is cut on its own bound, so no single text fills the index.
    assert long not in index and "..." in index


def test_a_cut_keeps_whole_characters_and_the_first_line() -> None:
    assert cut_text("short\nmore", 100) == "short"
    cut = cut_text("é" * 100, 11)
    assert cut.endswith("...") and len(cut.encode("utf-8")) <= 11
    assert cut_text("   ", 10) == ""


def test_a_new_session_without_a_brief_is_given_the_index(world: World) -> None:
    """The line holds no engine, as in its first turn after a restart, so the index
    is owed, and with no brief set it is given apart from the brief's lead, which
    stays empty, so it can end the prompt rather than head it."""
    scheduler = world.scheduler
    assert scheduler._index_without_brief("alpha", False) is None  # pyright: ignore[reportPrivateUsage]
    sent = scheduler.send(ALPHA, ANA, "my report")
    index = scheduler._index_without_brief("alpha", False)  # pyright: ignore[reportPrivateUsage]
    assert index is not None and index.startswith("EVIDENCE INDEX.") and sent in index
    assert scheduler._brief_lead("alpha", False) is None  # pyright: ignore[reportPrivateUsage]
