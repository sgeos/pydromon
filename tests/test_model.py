"""The data model's field validation, state transitions and rejections."""

from dataclasses import fields
from typing import get_args

import pytest

from dromon.model import (
    LINE_TRANSITIONS,
    MAX_CHOICE_BYTES,
    MAX_CHOICES,
    MAX_TEXT_BYTES,
    OPERATOR,
    Event,
    EventRejected,
    InvalidField,
    LineCreated,
    LineState,
    LineStateChanged,
    MessageDelivered,
    MessageSent,
    QuestionAnswered,
    QuestionAsked,
    QuestionStatus,
    QuestionSuperseded,
    QuestionWithdrawn,
    State,
    WakeCause,
    WakeIssued,
    WakeRequested,
)

ENGINE = "claude-opus-5-5"


def build(*events: Event) -> State:
    state = State()
    for event in events:
        state.apply(event)
    return state


def two_lines() -> list[Event]:
    return [LineCreated("alpha", ENGINE), LineCreated("beta", ENGINE)]


# The scenario.


def test_the_scenario_uses_every_event_kind(scenario: tuple[Event, ...]) -> None:
    every_kind = set(get_args(Event.__value__))
    assert {type(event) for event in scenario} == every_kind


def test_the_scenario_uses_every_wake_cause(scenario: tuple[Event, ...]) -> None:
    causes = {e.cause for e in scenario if isinstance(e, WakeRequested)}
    assert causes == set(WakeCause)


def test_the_scenario_builds_the_expected_state(scenario: tuple[Event, ...]) -> None:
    state = build(*scenario)
    assert state.lines["alpha"].state is LineState.STOPPED
    assert state.lines["beta"].state is LineState.IDLE
    assert state.messages["m1"].delivered
    assert not state.messages["m2"].delivered
    assert all(wake.issued for wake in state.wakes.values())
    assert state.questions["q1"].status is QuestionStatus.SUPERSEDED
    assert state.questions["q1"].superseded_by == "q2"
    assert state.questions["q2"].status is QuestionStatus.ANSWERED
    assert state.questions["q2"].answer == "yes"
    assert state.questions["q3"].status is QuestionStatus.WITHDRAWN


def test_applying_the_same_events_twice_gives_equal_states(
    scenario: tuple[Event, ...],
) -> None:
    assert build(*scenario) == build(*scenario)


def test_a_copy_is_independent(scenario: tuple[Event, ...]) -> None:
    state = build(*scenario)
    snapshot = state.copy()
    state.apply(LineCreated("gamma", ENGINE))
    assert "gamma" not in snapshot.lines
    assert snapshot != state


# Field validation.


@pytest.mark.parametrize(
    "value",
    ["", "Alpha", "-alpha", "a b", "a/b", "a" * 65, "été", OPERATOR, 7],
)
def test_an_invalid_identifier_is_refused(value: object) -> None:
    with pytest.raises(InvalidField, match="line_id"):
        LineCreated(value, ENGINE)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("value", ["a", "0", "a" * 64, "claude-opus-5.5_x"])
def test_a_valid_identifier_is_accepted(value: str) -> None:
    assert LineCreated(value, ENGINE).line_id == value


def test_the_operator_may_send_but_is_not_an_identifier() -> None:
    assert MessageSent("m", OPERATOR, "alpha", "hi").sender == OPERATOR
    with pytest.raises(InvalidField):
        MessageSent("m", "alpha", OPERATOR, "hi")


def test_text_at_the_limit_is_accepted_and_one_byte_more_is_refused() -> None:
    MessageSent("m", "alpha", "beta", "x" * MAX_TEXT_BYTES)
    with pytest.raises(InvalidField, match="over the limit"):
        MessageSent("m", "alpha", "beta", "x" * (MAX_TEXT_BYTES + 1))


def test_the_text_limit_counts_utf8_bytes_not_characters() -> None:
    snake = "\U0001f40d"
    assert len(snake.encode("utf-8")) == 4
    with pytest.raises(InvalidField, match="over the limit"):
        MessageSent("m", "alpha", "beta", snake * (MAX_TEXT_BYTES // 4 + 1))


def test_a_lone_surrogate_is_refused() -> None:
    with pytest.raises(InvalidField, match="Unicode"):
        MessageSent("m", "alpha", "beta", "\ud800")


def test_text_must_be_a_string() -> None:
    with pytest.raises(InvalidField, match="not text"):
        MessageSent("m", "alpha", "beta", b"hi")  # pyright: ignore[reportArgumentType]


def test_blocking_must_be_a_boolean_not_an_integer() -> None:
    with pytest.raises(InvalidField, match="boolean"):
        QuestionAsked("q", "alpha", "?", (), 1)  # pyright: ignore[reportArgumentType]


def test_choices_are_bounded_in_count_and_size_and_distinct() -> None:
    QuestionAsked("q", "a", "?", tuple(str(n) for n in range(MAX_CHOICES)), True)
    with pytest.raises(InvalidField, match="entries"):
        QuestionAsked(
            "q", "a", "?", tuple(str(n) for n in range(MAX_CHOICES + 1)), True
        )
    with pytest.raises(InvalidField, match="over the limit"):
        QuestionAsked("q", "a", "?", ("x" * (MAX_CHOICE_BYTES + 1),), True)
    with pytest.raises(InvalidField, match="repeat"):
        QuestionAsked("q", "a", "?", ("yes", "yes"), True)


def test_choices_must_be_a_tuple_of_text() -> None:
    with pytest.raises(InvalidField, match="tuple"):
        QuestionAsked("q", "a", "?", ["yes"], True)  # pyright: ignore[reportArgumentType]
    with pytest.raises(InvalidField, match="not text"):
        QuestionAsked("q", "a", "?", (1,), True)  # pyright: ignore[reportArgumentType]


def test_enumerated_fields_must_be_members() -> None:
    with pytest.raises(InvalidField, match="LineState"):
        LineStateChanged("a", "idle")  # pyright: ignore[reportArgumentType]
    with pytest.raises(InvalidField, match="WakeCause"):
        WakeRequested("w", "a", "message", "m")  # pyright: ignore[reportArgumentType]


def test_every_event_is_immutable() -> None:
    event = LineCreated("alpha", ENGINE)
    with pytest.raises(AttributeError):
        event.line_id = "beta"  # pyright: ignore[reportAttributeAccessIssue]
    assert [f.name for f in fields(event)] == ["line_id", "engine"]


# Line state transitions.


@pytest.mark.parametrize("old", list(LineState))
@pytest.mark.parametrize("new", list(LineState))
def test_line_transitions_follow_the_table(old: LineState, new: LineState) -> None:
    state = build(LineCreated("a", ENGINE))
    # Reach ``old`` by the shortest permitted path from idle.
    paths = {
        LineState.IDLE: [],
        LineState.RUNNING: [LineState.RUNNING],
        LineState.STOPPED: [LineState.STOPPED],
    }
    for step in paths[old]:
        state.apply(LineStateChanged("a", step))
    change = LineStateChanged("a", new)
    if (old, new) in LINE_TRANSITIONS:
        state.apply(change)
        assert state.lines["a"].state is new
    else:
        with pytest.raises(EventRejected, match="cannot go"):
            state.apply(change)
        assert state.lines["a"].state is old


def test_no_transition_keeps_the_same_state() -> None:
    assert all(old is not new for old, new in LINE_TRANSITIONS)


def test_a_blocked_line_cannot_start_a_turn() -> None:
    state = build(
        LineCreated("a", ENGINE), QuestionAsked("q", "a", "?", (), blocking=True)
    )
    assert state.is_blocked("a")
    with pytest.raises(EventRejected, match="blocked"):
        state.apply(LineStateChanged("a", LineState.RUNNING))


def test_a_non_blocking_question_does_not_block() -> None:
    state = build(
        LineCreated("a", ENGINE), QuestionAsked("q", "a", "?", (), blocking=False)
    )
    assert not state.is_blocked("a")
    state.apply(LineStateChanged("a", LineState.RUNNING))


# Rejections. Each case is a valid history followed by one event that must not apply.

REJECTIONS: list[tuple[str, list[Event], Event, str]] = [
    ("duplicate line", two_lines(), LineCreated("alpha", ENGINE), "exists"),
    ("unknown line state", [], LineStateChanged("x", LineState.RUNNING), "no line"),
    (
        "duplicate message",
        [*two_lines(), MessageSent("m", "alpha", "beta", "")],
        MessageSent("m", "alpha", "beta", ""),
        "exists",
    ),
    ("unknown recipient", two_lines(), MessageSent("m", "alpha", "x", ""), "no line"),
    ("unknown sender", two_lines(), MessageSent("m", "x", "beta", ""), "no line"),
    ("self message", two_lines(), MessageSent("m", "alpha", "alpha", ""), "itself"),
    ("unknown delivery", two_lines(), MessageDelivered("m"), "no message"),
    (
        "second delivery",
        [*two_lines(), MessageSent("m", "alpha", "beta", ""), MessageDelivered("m")],
        MessageDelivered("m"),
        "was delivered",
    ),
    (
        "duplicate wake",
        [
            *two_lines(),
            MessageSent("m", "alpha", "beta", ""),
            WakeRequested("w", "beta", WakeCause.MESSAGE, "m"),
        ],
        WakeRequested("w", "beta", WakeCause.MESSAGE, "m"),
        "exists",
    ),
    (
        "wake for a stopped line",
        [
            *two_lines(),
            MessageSent("m", "alpha", "beta", ""),
            LineStateChanged("beta", LineState.STOPPED),
        ],
        WakeRequested("w", "beta", WakeCause.MESSAGE, "m"),
        "stopped",
    ),
    (
        "wake for another line's message",
        [*two_lines(), MessageSent("m", "alpha", "beta", "")],
        WakeRequested("w", "alpha", WakeCause.MESSAGE, "m"),
        "not for line",
    ),
    (
        "wake for an unknown message",
        two_lines(),
        WakeRequested("w", "beta", WakeCause.MESSAGE, "m"),
        "no message",
    ),
    (
        "wake for an unknown question",
        two_lines(),
        WakeRequested("w", "beta", WakeCause.ANSWER, "q"),
        "no question",
    ),
    (
        "wake for an unanswered question",
        [*two_lines(), QuestionAsked("q", "beta", "?", (), True)],
        WakeRequested("w", "beta", WakeCause.ANSWER, "q"),
        "not answered",
    ),
    (
        "wake for another line's answer",
        [
            *two_lines(),
            QuestionAsked("q", "beta", "?", (), True),
            QuestionAnswered("q", "ok"),
        ],
        WakeRequested("w", "alpha", WakeCause.ANSWER, "q"),
        "not from line",
    ),
    ("unknown wake issued", two_lines(), WakeIssued("w"), "no wake"),
    (
        "wake issued twice",
        [
            *two_lines(),
            MessageSent("m", "alpha", "beta", ""),
            WakeRequested("w", "beta", WakeCause.MESSAGE, "m"),
            WakeIssued("w"),
        ],
        WakeIssued("w"),
        "was issued",
    ),
    (
        "wake issued to a running line",
        [
            *two_lines(),
            MessageSent("m", "alpha", "beta", ""),
            WakeRequested("w", "beta", WakeCause.MESSAGE, "m"),
            LineStateChanged("beta", LineState.RUNNING),
        ],
        WakeIssued("w"),
        "not idle",
    ),
    (
        "wake issued to a blocked line",
        [
            *two_lines(),
            MessageSent("m", "alpha", "beta", ""),
            WakeRequested("w", "beta", WakeCause.MESSAGE, "m"),
            QuestionAsked("q", "beta", "?", (), True),
        ],
        WakeIssued("w"),
        "blocked",
    ),
    (
        "duplicate question",
        [*two_lines(), QuestionAsked("q", "beta", "?", (), True)],
        QuestionAsked("q", "beta", "?", (), True),
        "exists",
    ),
    (
        "question from a stopped line",
        [*two_lines(), LineStateChanged("beta", LineState.STOPPED)],
        QuestionAsked("q", "beta", "?", (), True),
        "stopped",
    ),
    ("answer to an unknown question", [], QuestionAnswered("q", "ok"), "no question"),
    (
        "second answer",
        [
            *two_lines(),
            QuestionAsked("q", "beta", "?", (), True),
            QuestionAnswered("q", "ok"),
        ],
        QuestionAnswered("q", "again"),
        "is answered",
    ),
    (
        "withdrawal of an answered question",
        [
            *two_lines(),
            QuestionAsked("q", "beta", "?", (), True),
            QuestionAnswered("q", "ok"),
        ],
        QuestionWithdrawn("q"),
        "is answered",
    ),
    (
        "self supersession",
        [*two_lines(), QuestionAsked("q", "beta", "?", (), True)],
        QuestionSuperseded("q", "q"),
        "itself",
    ),
    (
        "supersession by an unknown question",
        [*two_lines(), QuestionAsked("q", "beta", "?", (), True)],
        QuestionSuperseded("q", "r"),
        "no question",
    ),
    (
        "supersession by a closed question",
        [
            *two_lines(),
            QuestionAsked("q", "beta", "?", (), True),
            QuestionAsked("r", "beta", "?", (), True),
            QuestionWithdrawn("r"),
        ],
        QuestionSuperseded("q", "r"),
        "is withdrawn",
    ),
    (
        "supersession across lines",
        [
            *two_lines(),
            QuestionAsked("q", "beta", "?", (), True),
            QuestionAsked("r", "alpha", "?", (), True),
        ],
        QuestionSuperseded("q", "r"),
        "another line",
    ),
]


@pytest.mark.parametrize(
    ("history", "event", "reason"),
    [case[1:] for case in REJECTIONS],
    ids=[case[0] for case in REJECTIONS],
)
def test_an_inapplicable_event_is_rejected_and_changes_nothing(
    history: list[Event], event: Event, reason: str
) -> None:
    state = build(*history)
    before = state.copy()
    with pytest.raises(EventRejected, match=reason):
        state.apply(event)
    assert state == before


def test_check_never_mutates(scenario: tuple[Event, ...]) -> None:
    state = State()
    for event in scenario:
        before = state.copy()
        state.check(event)
        assert state == before
        state.apply(event)
