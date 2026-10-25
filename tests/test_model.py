"""The data model's field validation, state transitions and rejections."""

from dataclasses import fields
from typing import get_args

import pytest

from dromon.model import (
    LINE_TRANSITIONS,
    MAX_CHOICE_BYTES,
    MAX_CHOICES,
    MAX_TEXT_BYTES,
    Event,
    EventRejected,
    InvalidField,
    LineAddress,
    LineCreated,
    LineState,
    LineStateChanged,
    MessageDelivered,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    QuestionStatus,
    QuestionSuperseded,
    QuestionWithdrawn,
    State,
    WakeIssued,
    WakeRequested,
    parse_address,
)

ENGINE = "claude-opus-5-5"
ALPHA = LineAddress("alpha")
BETA = LineAddress("beta")
ANA = OperatorAddress("ana")
BEN = OperatorAddress("ben")
NOTE = MessageKind.NOTE
QUESTION = MessageKind.QUESTION


def build(*events: Event) -> State:
    state = State()
    for event in events:
        state.apply(event)
    return state


def parties() -> list[Event]:
    return [
        LineCreated("alpha", ENGINE, "subscription"),
        LineCreated("beta", ENGINE, "subscription"),
        OperatorRegistered("ana"),
        OperatorRegistered("ben"),
    ]


def ask(message_id: str = "q", line: LineAddress = BETA) -> MessageSent:
    return MessageSent(message_id, line, ANA, QUESTION, "?")


# The scenario.


def test_the_scenario_uses_every_event_kind(scenario: tuple[Event, ...]) -> None:
    every_kind = set(get_args(Event.__value__))
    assert {type(event) for event in scenario} == every_kind


def test_the_scenario_uses_every_kind_of_party_and_message(
    scenario: tuple[Event, ...],
) -> None:
    sent = [e for e in scenario if isinstance(e, MessageSent)]
    assert {m.kind for m in sent} == set(MessageKind)
    assert {type(m.sender) for m in sent} == {LineAddress, OperatorAddress}
    assert {type(m.recipient) for m in sent} == {LineAddress, OperatorAddress}
    assert (
        len({m.recipient for m in sent if isinstance(m.recipient, OperatorAddress)}) > 1
    )


def test_the_scenario_builds_the_expected_state(scenario: tuple[Event, ...]) -> None:
    state = build(*scenario)
    assert state.lines["alpha"].state is LineState.STOPPED
    assert state.lines["beta"].state is LineState.IDLE
    assert state.operators == {"ana", "ben"}
    assert state.messages["m1"].delivered
    assert state.messages["m1"].status is None
    assert all(wake.issued for wake in state.wakes.values())
    assert state.messages["q1"].status is QuestionStatus.SUPERSEDED
    assert state.messages["q1"].superseded_by == "q2"
    assert state.messages["q2"].status is QuestionStatus.ANSWERED
    assert state.messages["q2"].answered_by == "a2"
    assert state.messages["q3"].status is QuestionStatus.WITHDRAWN
    assert state.messages["m3"].reply_to == "m2"
    assert state.open_questions("ana") == []


def test_applying_the_same_events_twice_gives_equal_states(
    scenario: tuple[Event, ...],
) -> None:
    assert build(*scenario) == build(*scenario)


def test_a_copy_is_independent(scenario: tuple[Event, ...]) -> None:
    state = build(*scenario)
    snapshot = state.copy()
    state.apply(LineCreated("gamma", ENGINE, "subscription"))
    state.apply(OperatorRegistered("cleo"))
    assert "gamma" not in snapshot.lines
    assert "cleo" not in snapshot.operators
    assert snapshot != state


# Addresses.


@pytest.mark.parametrize("address", [ALPHA, ANA, LineAddress("a.b-c_d")])
def test_an_address_round_trips_through_its_text(
    address: LineAddress | OperatorAddress,
) -> None:
    assert parse_address(str(address)) == address


def test_a_line_and_an_operator_with_one_identifier_are_different_addresses() -> None:
    assert LineAddress("ana") != OperatorAddress("ana")
    assert str(LineAddress("ana")) != str(OperatorAddress("ana"))


@pytest.mark.parametrize("text", ["", "@", "@@ana", "@Ana", "Alpha", "a b"])
def test_malformed_address_text_is_refused(text: str) -> None:
    with pytest.raises(InvalidField):
        parse_address(text)


def test_operators_are_addressed_separately() -> None:
    state = build(*parties(), ask("q1"), MessageSent("q2", ALPHA, BEN, QUESTION, "?"))
    assert state.open_questions("ana") == ["q1"]
    assert state.open_questions("ben") == ["q2"]


# Field validation.


@pytest.mark.parametrize(
    "value",
    ["", "Alpha", "-alpha", "a b", "a/b", "a" * 65, "été", "@ana", 7],
)
def test_an_invalid_identifier_is_refused(value: object) -> None:
    with pytest.raises(InvalidField, match="line_id"):
        LineCreated(value, ENGINE, "subscription")  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("value", ["a", "0", "a" * 64, "claude-opus-5.5_x"])
def test_a_valid_identifier_is_accepted(value: str) -> None:
    assert LineCreated(value, ENGINE, "subscription").line_id == value


def test_an_address_field_must_hold_an_address() -> None:
    with pytest.raises(InvalidField, match="sender is not an address"):
        MessageSent("m", "alpha", BETA, NOTE, "")  # pyright: ignore[reportArgumentType]


def test_text_at_the_limit_is_accepted_and_one_byte_more_is_refused() -> None:
    MessageSent("m", ALPHA, BETA, NOTE, "x" * MAX_TEXT_BYTES)
    with pytest.raises(InvalidField, match="over the limit"):
        MessageSent("m", ALPHA, BETA, NOTE, "x" * (MAX_TEXT_BYTES + 1))


def test_the_text_limit_counts_utf8_bytes_not_characters() -> None:
    snake = "\U0001f40d"
    assert len(snake.encode("utf-8")) == 4
    with pytest.raises(InvalidField, match="over the limit"):
        MessageSent("m", ALPHA, BETA, NOTE, snake * (MAX_TEXT_BYTES // 4 + 1))


def test_a_lone_surrogate_is_refused() -> None:
    with pytest.raises(InvalidField, match="Unicode"):
        MessageSent("m", ALPHA, BETA, NOTE, "\ud800")


def test_text_must_be_a_string() -> None:
    with pytest.raises(InvalidField, match="not text"):
        MessageSent("m", ALPHA, BETA, NOTE, b"hi")  # pyright: ignore[reportArgumentType]


def test_only_a_question_has_choices() -> None:
    with pytest.raises(InvalidField, match="only a question"):
        MessageSent("m", ALPHA, BETA, NOTE, "", choices=("a",))


def test_choices_are_bounded_in_count_and_size_and_distinct() -> None:
    def question(choices: tuple[str, ...]) -> MessageSent:
        return MessageSent("q", BETA, ANA, QUESTION, "?", choices)

    question(tuple(str(n) for n in range(MAX_CHOICES)))
    with pytest.raises(InvalidField, match="entries"):
        question(tuple(str(n) for n in range(MAX_CHOICES + 1)))
    with pytest.raises(InvalidField, match="over the limit"):
        question(("x" * (MAX_CHOICE_BYTES + 1),))
    with pytest.raises(InvalidField, match="repeat"):
        question(("yes", "yes"))


def test_choices_must_be_a_tuple_of_text() -> None:
    with pytest.raises(InvalidField, match="tuple"):
        MessageSent("q", BETA, ANA, QUESTION, "?", ["yes"])  # pyright: ignore[reportArgumentType]
    with pytest.raises(InvalidField, match="not text"):
        MessageSent("q", BETA, ANA, QUESTION, "?", (1,))  # pyright: ignore[reportArgumentType]


def test_enumerated_fields_must_be_members() -> None:
    with pytest.raises(InvalidField, match="LineState"):
        LineStateChanged("a", "idle")  # pyright: ignore[reportArgumentType]
    with pytest.raises(InvalidField, match="MessageKind"):
        MessageSent("m", ALPHA, BETA, "note", "")  # pyright: ignore[reportArgumentType]


def test_an_invalid_reply_reference_is_refused() -> None:
    with pytest.raises(InvalidField, match="reply_to"):
        MessageSent("m", ALPHA, BETA, NOTE, "", reply_to="Bad Id")


def test_every_event_is_immutable() -> None:
    event = LineCreated("alpha", ENGINE, "subscription")
    with pytest.raises(AttributeError):
        event.line_id = "beta"  # pyright: ignore[reportAttributeAccessIssue]
    assert [f.name for f in fields(event)] == ["line_id", "engine", "strategy"]


# Line state transitions.


@pytest.mark.parametrize("old", list(LineState))
@pytest.mark.parametrize("new", list(LineState))
def test_line_transitions_follow_the_table(old: LineState, new: LineState) -> None:
    state = build(LineCreated("a", ENGINE, "subscription"))
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


def test_an_open_question_never_stops_its_line() -> None:
    state = build(
        *parties(),
        ask(),
        MessageSent("m", ALPHA, BETA, NOTE, "meanwhile"),
        WakeRequested("w", "beta", "m"),
        WakeIssued("w"),
        LineStateChanged("beta", LineState.RUNNING),
    )
    assert state.messages["q"].status is QuestionStatus.OPEN
    assert state.lines["beta"].state is LineState.RUNNING


def test_an_answer_can_wake_the_asking_line() -> None:
    state = build(
        *parties(), ask(), MessageSent("a", ANA, BETA, NOTE, "ok", reply_to="q")
    )
    state.apply(WakeRequested("w", "beta", "a"))
    state.apply(WakeIssued("w"))
    assert state.messages["q"].status is QuestionStatus.ANSWERED


# Rejections. Each case is a valid history followed by one event that must not apply.

REJECTIONS: list[tuple[str, list[Event], Event, str]] = [
    (
        "duplicate line",
        parties(),
        LineCreated("alpha", ENGINE, "subscription"),
        "exists",
    ),
    ("duplicate operator", parties(), OperatorRegistered("ana"), "exists"),
    ("unknown line state", [], LineStateChanged("x", LineState.RUNNING), "no line"),
    (
        "duplicate message",
        [*parties(), MessageSent("m", ALPHA, BETA, NOTE, "")],
        MessageSent("m", ALPHA, BETA, NOTE, ""),
        "exists",
    ),
    (
        "unknown line recipient",
        parties(),
        MessageSent("m", ALPHA, LineAddress("x"), NOTE, ""),
        "no line",
    ),
    (
        "unknown operator recipient",
        parties(),
        MessageSent("m", ALPHA, OperatorAddress("x"), NOTE, ""),
        "no operator",
    ),
    (
        "unknown sender",
        parties(),
        MessageSent("m", LineAddress("x"), BETA, NOTE, ""),
        "no line",
    ),
    ("self message", parties(), MessageSent("m", ALPHA, ALPHA, NOTE, ""), "itself"),
    (
        "question from an operator",
        parties(),
        MessageSent("q", ANA, BETA, QUESTION, "?"),
        "only a line",
    ),
    (
        "question to a line",
        parties(),
        MessageSent("q", ALPHA, BETA, QUESTION, "?"),
        "addressed to an operator",
    ),
    (
        "question from a stopped line",
        [*parties(), LineStateChanged("beta", LineState.STOPPED)],
        ask(),
        "stopped",
    ),
    (
        "reply to an unknown message",
        parties(),
        MessageSent("a", ANA, BETA, NOTE, "", reply_to="q"),
        "no message",
    ),
    (
        "answer from another operator",
        [*parties(), ask()],
        MessageSent("a", BEN, BETA, NOTE, "", reply_to="q"),
        "must go from @ana to beta",
    ),
    (
        "answer to another line",
        [*parties(), ask()],
        MessageSent("a", ANA, ALPHA, NOTE, "", reply_to="q"),
        "must go from @ana to beta",
    ),
    (
        "second answer",
        [*parties(), ask(), MessageSent("a", ANA, BETA, NOTE, "", reply_to="q")],
        MessageSent("b", ANA, BETA, NOTE, "", reply_to="q"),
        "is answered",
    ),
    (
        "answer to a withdrawn question",
        [*parties(), ask(), QuestionWithdrawn("q")],
        MessageSent("a", ANA, BETA, NOTE, "", reply_to="q"),
        "is withdrawn",
    ),
    (
        "unknown delivery",
        parties(),
        MessageDelivered("m"),
        "no message",
    ),
    (
        "second delivery",
        [*parties(), MessageSent("m", ALPHA, BETA, NOTE, ""), MessageDelivered("m")],
        MessageDelivered("m"),
        "was delivered",
    ),
    (
        "duplicate wake",
        [
            *parties(),
            MessageSent("m", ALPHA, BETA, NOTE, ""),
            WakeRequested("w", "beta", "m"),
        ],
        WakeRequested("w", "beta", "m"),
        "exists",
    ),
    (
        "wake for a stopped line",
        [
            *parties(),
            MessageSent("m", ALPHA, BETA, NOTE, ""),
            LineStateChanged("beta", LineState.STOPPED),
        ],
        WakeRequested("w", "beta", "m"),
        "stopped",
    ),
    (
        "wake for another line's message",
        [*parties(), MessageSent("m", ALPHA, BETA, NOTE, "")],
        WakeRequested("w", "alpha", "m"),
        "not for line",
    ),
    (
        "wake for a message to an operator",
        [*parties(), ask()],
        WakeRequested("w", "beta", "q"),
        "not for line",
    ),
    (
        "wake for an unknown message",
        parties(),
        WakeRequested("w", "beta", "m"),
        "no message",
    ),
    ("unknown wake issued", parties(), WakeIssued("w"), "no wake"),
    (
        "wake issued twice",
        [
            *parties(),
            MessageSent("m", ALPHA, BETA, NOTE, ""),
            WakeRequested("w", "beta", "m"),
            WakeIssued("w"),
        ],
        WakeIssued("w"),
        "was issued",
    ),
    (
        "wake issued to a running line",
        [
            *parties(),
            MessageSent("m", ALPHA, BETA, NOTE, ""),
            WakeRequested("w", "beta", "m"),
            LineStateChanged("beta", LineState.RUNNING),
        ],
        WakeIssued("w"),
        "not idle",
    ),
    (
        "withdrawal of a note",
        [*parties(), MessageSent("m", ALPHA, BETA, NOTE, "")],
        QuestionWithdrawn("m"),
        "not a question",
    ),
    (
        "withdrawal of an answered question",
        [*parties(), ask(), MessageSent("a", ANA, BETA, NOTE, "", reply_to="q")],
        QuestionWithdrawn("q"),
        "is answered",
    ),
    ("self supersession", [*parties(), ask()], QuestionSuperseded("q", "q"), "itself"),
    (
        "supersession by an unknown question",
        [*parties(), ask()],
        QuestionSuperseded("q", "r"),
        "no message",
    ),
    (
        "supersession by a closed question",
        [*parties(), ask(), ask("r"), QuestionWithdrawn("r")],
        QuestionSuperseded("q", "r"),
        "is withdrawn",
    ),
    (
        "supersession across lines",
        [*parties(), ask(), ask("r", ALPHA)],
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


def test_a_reply_to_a_note_reverses_its_route() -> None:
    state = build(*parties(), MessageSent("m", ALPHA, BETA, NOTE, ""))
    with pytest.raises(EventRejected, match="must go from beta to alpha"):
        state.apply(MessageSent("r", ANA, ALPHA, NOTE, "", reply_to="m"))
    state.apply(MessageSent("r", BETA, ALPHA, NOTE, "", reply_to="m"))
    assert state.messages["m"].status is None


def test_check_never_mutates(scenario: tuple[Event, ...]) -> None:
    state = State()
    for event in scenario:
        before = state.copy()
        state.check(event)
        assert state == before
        state.apply(event)
