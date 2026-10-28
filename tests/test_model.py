"""The data model's field validation, state transitions and rejections."""

from collections.abc import Callable
from dataclasses import fields
from typing import get_args

import pytest

from dromon import model
from dromon.model import (
    HARNESS,
    HARNESS_TEXT,
    HarnessAddress,
    AgentAccess,
    AgentAddress,
    AgentRegistered,
    RemoteAddress,
    LINE_TRANSITIONS,
    MAX_CHOICE_BYTES,
    MAX_CHOICES,
    MAX_TEXT_BYTES,
    Event,
    EventRejected,
    InvalidField,
    LineAddress,
    LineCreated,
    LineHibernated,
    LineAwakened,
    LineState,
    LineStateChanged,
    MessageAcknowledged,
    MessageDelivered,
    MessageRead,
    MessageKind,
    MessagePrioritized,
    MessageCorrelated,
    TurnCorrelated,
    DisclosureChecked,
    Correlation,
    MAX_COST_MICRO_USD,
    MAX_TURN_KEYS,
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
    parse_any_address,
    parse_sender,
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
    parties = {LineAddress, OperatorAddress, AgentAddress}
    # The harness sends but is never a party anyone addresses.
    assert {type(m.sender) for m in sent} == parties | {HarnessAddress}
    # Only a recipient may be remote, since mail from a peer is message_received.
    assert {type(m.recipient) for m in sent} == parties | {RemoteAddress}
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
    assert state.lines["beta"].engine_session == "0b9f2c6e-session"
    assert state.lines["alpha"].overrides == '{"effort":"low"}'
    assert state.project_overrides == '{"limits.max_cost_usd":"5"}'


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


@pytest.mark.parametrize("parse", [parse_address, parse_any_address])
def test_no_input_can_name_the_harness(parse: Callable[[str], object]) -> None:
    """The operator's decision of 2026-10-28. Clients, lines, agents and peers name
    addresses through these two, so none of them can claim the harness's address,
    and a peer cannot write it as its party."""
    for text in (HARNESS_TEXT, f"keleusma:{HARNESS_TEXT}"):
        with pytest.raises(InvalidField):
            parse(text)


def test_only_the_log_reads_the_harness_as_a_sender() -> None:
    assert parse_sender(HARNESS_TEXT) == HARNESS == HarnessAddress()
    assert parse_sender(str(HARNESS)) is HARNESS
    assert parse_sender("alpha") == ALPHA
    assert parse_sender("keleusma:@ana") == RemoteAddress("keleusma", ANA)


@pytest.mark.parametrize("recipient", [ALPHA, ANA])
def test_the_harness_sends_a_note_to_a_line_or_an_operator(
    recipient: LineAddress | OperatorAddress,
) -> None:
    state = build(*parties(), MessageSent("h", HARNESS, recipient, NOTE, "failed over"))
    assert state.messages["h"].sender == HARNESS


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (MessageSent("h", HARNESS, ANA, QUESTION, "?"), "only notes"),
        (MessageSent("h", HARNESS, ANA, NOTE, "", reply_to="m"), "replies to nothing"),
        (
            MessageSent("h", HARNESS, AgentAddress("probe"), NOTE, ""),
            "only to a line or an operator",
        ),
        (
            MessageSent("h", HARNESS, RemoteAddress("keleusma", ALPHA), NOTE, ""),
            "only to a line or an operator",
        ),
    ],
)
def test_the_harness_asks_answers_and_forwards_nothing(
    event: MessageSent, reason: str
) -> None:
    state = build(
        *parties(),
        AgentRegistered("probe", AgentAccess.MESSAGE, "0" * 64),
        MessageSent("m", ANA, ALPHA, NOTE, "hello"),
    )
    with pytest.raises(EventRejected, match=reason):
        state.check(event)


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
    (
        "operator self message",
        parties(),
        MessageSent("m", ANA, ANA, NOTE, ""),
        "cannot message itself",
    ),
    (
        "agent self message",
        [*parties(), AgentRegistered("probe", AgentAccess.MESSAGE, "")],
        MessageSent("m", AgentAddress("probe"), AgentAddress("probe"), NOTE, ""),
        "cannot message itself",
    ),
    (
        "hibernate twice",
        [*parties(), LineHibernated("alpha")],
        LineHibernated("alpha"),
        "is hibernated",
    ),
    ("awaken awakened", parties(), LineAwakened("alpha"), "is not hibernated"),
    ("hibernate no line", parties(), LineHibernated("x"), "no line"),
    ("prioritize nothing", parties(), MessagePrioritized("m"), "no message m"),
    (
        "prioritize twice",
        [
            *parties(),
            MessageSent("m", ANA, ALPHA, NOTE, "now"),
            MessagePrioritized("m"),
        ],
        MessagePrioritized("m"),
        "is priority mail",
    ),
    (
        "prioritize a question",
        [*parties(), MessageSent("m", ALPHA, ANA, QUESTION, "?")],
        MessagePrioritized("m"),
        "not a note",
    ),
    (
        "prioritize operator mail",
        [*parties(), MessageSent("m", ALPHA, ANA, NOTE, "hi")],
        MessagePrioritized("m"),
        "not to a line",
    ),
    (
        "prioritize delivered mail",
        [
            *parties(),
            MessageSent("m", ANA, ALPHA, NOTE, "now"),
            MessageDelivered("m"),
        ],
        MessagePrioritized("m"),
        "was delivered",
    ),
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
        "reading an undelivered message",
        [*parties(), MessageSent("m", ALPHA, BETA, NOTE, "")],
        MessageRead("m"),
        "was not delivered",
    ),
    (
        "reading twice",
        [
            *parties(),
            MessageSent("m", ALPHA, BETA, NOTE, ""),
            MessageDelivered("m"),
            MessageRead("m"),
        ],
        MessageRead("m"),
        "was read",
    ),
    ("reading an unknown message", parties(), MessageRead("m"), "no message"),
    (
        "acknowledging an undelivered message",
        [*parties(), MessageSent("m", ALPHA, BETA, NOTE, "")],
        MessageAcknowledged("m"),
        "was not delivered",
    ),
    (
        "acknowledging twice",
        [
            *parties(),
            MessageSent("m", ALPHA, BETA, NOTE, ""),
            MessageDelivered("m"),
            MessageAcknowledged("m"),
        ],
        MessageAcknowledged("m"),
        "was acknowledged",
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


def test_a_line_may_send_itself_a_note_that_waits_undelivered() -> None:
    state = build(*parties(), MessageSent("m", ALPHA, ALPHA, NOTE, "later"))
    note = state.messages["m"]
    assert note.sender == note.recipient == ALPHA
    assert not note.delivered and not note.priority


def test_hibernate_and_priority_change_only_their_own_flags() -> None:
    state = build(*parties(), MessageSent("m", ANA, ALPHA, NOTE, "now"))
    before = state.lines["alpha"]
    state.apply(LineHibernated("alpha"))
    assert state.lines["alpha"].hibernated
    state.apply(LineAwakened("alpha"))
    assert state.lines["alpha"] == before
    state.apply(MessagePrioritized("m"))
    assert state.messages["m"].priority
    assert not state.messages["m"].delivered


# Correlation keys. Queue of 2026-10-28, item A4.

TABLE = AgentAddress("table")


def correlated_parties() -> list[Event]:
    return [*parties(), AgentRegistered("table", AgentAccess.MESSAGE, "")]


def test_an_agents_note_to_a_line_takes_one_correlation_key() -> None:
    state = build(*correlated_parties(), MessageSent("m", TABLE, ALPHA, NOTE, "go"))
    state.apply(MessageCorrelated("m", "game-7"))
    assert state.messages["m"].correlation == "game-7"
    with pytest.raises(EventRejected, match="has a correlation key"):
        state.apply(MessageCorrelated("m", "game-8"))
    state.apply(MessageDelivered("m"))
    assert state.messages["m"].correlation == "game-7"


@pytest.mark.parametrize(
    ("sent", "delivered", "match"),
    [
        (MessageSent("m", ANA, ALPHA, NOTE, "go"), False, "not from an agent"),
        (MessageSent("m", ALPHA, ALPHA, NOTE, "go"), False, "not from an agent"),
        (MessageSent("m", TABLE, ANA, NOTE, "go"), False, "not to a line"),
        (MessageSent("m", TABLE, ALPHA, NOTE, "go"), True, "was delivered"),
    ],
)
def test_a_correlation_key_is_refused_off_an_agents_undelivered_note(
    sent: MessageSent, delivered: bool, match: str
) -> None:
    state = build(*correlated_parties(), sent)
    if delivered:
        state.apply(MessageDelivered("m"))
    before = state.messages["m"]
    with pytest.raises(EventRejected, match=match):
        state.apply(MessageCorrelated("m", "game-7"))
    assert state.messages["m"] == before
    with pytest.raises(EventRejected, match="no message"):
        state.apply(MessageCorrelated("absent", "game-7"))


@pytest.mark.parametrize(
    ("keys", "count", "cost", "match"),
    [
        ((), 0, None, "from 1 to"),
        (("b", "a"), 2, None, "distinct and sorted"),
        (("a", "a"), 2, None, "distinct and sorted"),
        (("Game 7",), 1, None, "key"),
        (("a", "b"), 1, None, "at least the keys listed"),
        (("a",), 2, None, "list every key"),
        (("a",), 1, -1, "cost_micro_usd"),
        (("a",), 1, MAX_COST_MICRO_USD + 1, "cost_micro_usd"),
    ],
)
def test_a_bad_turn_correlation_is_refused(
    keys: tuple[str, ...], count: int, cost: int | None, match: str
) -> None:
    with pytest.raises(InvalidField, match=match):
        TurnCorrelated("alpha", keys, count, cost)


def test_a_turn_with_more_keys_than_it_lists_lists_the_most_it_may() -> None:
    keys = tuple(sorted(f"k{n:03d}" for n in range(MAX_TURN_KEYS)))
    event = TurnCorrelated("alpha", keys, MAX_TURN_KEYS + 5, None)
    assert event.key_count - len(event.keys) == 5


def test_turns_total_under_each_key_they_carried() -> None:
    state = build(*parties())
    state.apply(TurnCorrelated("alpha", ("game-7", "game-8"), 2, 250_000))
    state.apply(TurnCorrelated("beta", ("game-7",), 1, None))
    state.apply(TurnCorrelated("alpha", ("game-7",), 1, 1_000_000))
    # A shared turn counts its whole cost under each key, and is named as shared.
    assert state.correlations == {
        "game-8": Correlation(1, 1, 250_000, 0),
        "game-7": Correlation(3, 1, 1_250_000, 1),
    }
    with pytest.raises(EventRejected, match="no line"):
        state.apply(TurnCorrelated("omega", ("game-7",), 1, None))


def test_a_keys_cost_total_is_held_at_the_largest_recorded() -> None:
    state = build(*parties())
    for _ in range(2):
        state.apply(TurnCorrelated("alpha", ("game-7",), 1, MAX_COST_MICRO_USD))
    assert state.correlations["game-7"].cost_micro_usd == MAX_COST_MICRO_USD


def test_the_key_used_least_recently_is_dropped_past_the_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(model, "MAX_CORRELATIONS", 2)
    state = build(*parties())
    for key in ("a", "b", "a", "c"):
        state.apply(TurnCorrelated("alpha", (key,), 1, 1))
    # "b" was used least recently once "a" was used again, so "c" displaced it.
    assert list(state.correlations) == ["a", "c"]
    assert state.correlations["a"].turns == 2
    copied = state.copy()
    copied.apply(TurnCorrelated("alpha", ("d",), 1, 1))
    assert list(state.correlations) == ["a", "c"]


# The disclosure check. Item B2 of 2026-10-28.


@pytest.mark.parametrize(
    ("checked_at", "matched", "match"),
    [
        ("2026-10-28 19:00", 0, "checked_at"),
        ("2026-10-28T19:00:00Z", -1, "matched_lines"),
        ("2026-10-28T19:00:00Z", model.MAX_DISCLOSURE_MATCHES + 1, "matched_lines"),
    ],
)
def test_a_bad_disclosure_check_is_refused(
    checked_at: str, matched: int, match: str
) -> None:
    with pytest.raises(InvalidField, match=match):
        DisclosureChecked("alpha", checked_at, matched)


def test_a_line_keeps_only_its_latest_disclosure_check_times() -> None:
    state = build(*parties())
    times = [
        f"2026-10-28T19:{minute:02d}:00Z"
        for minute in range(model.MAX_DISCLOSURE_CHECKS + 2)
    ]
    for time in times:
        state.apply(DisclosureChecked("alpha", time, 0))
    assert state.disclosure_checks == {
        "alpha": tuple(times[-model.MAX_DISCLOSURE_CHECKS :])
    }
    with pytest.raises(EventRejected, match="no line"):
        state.apply(DisclosureChecked("omega", times[0], 0))
