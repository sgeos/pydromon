"""Fixtures shared by the data model, codec, event log and relay tests."""

import pytest

from dromon.model import (
    Event,
    LineAddress,
    LineCreated,
    LineState,
    LineStateChanged,
    MessageDelivered,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    QuestionSuperseded,
    QuestionWithdrawn,
    WakeIssued,
    WakeRequested,
)

ENGINE = "claude-opus-5-5"
ALPHA = LineAddress("alpha")
BETA = LineAddress("beta")
ANA = OperatorAddress("ana")
BEN = OperatorAddress("ben")
NOTE = MessageKind.NOTE
QUESTION = MessageKind.QUESTION

# A valid history that uses every event kind, both message kinds and both address kinds
# as sender and recipient, with two operators. The coverage tests check that it really
# does, so a new event kind cannot be left out of it silently.
SCENARIO: tuple[Event, ...] = (
    LineCreated("alpha", ENGINE),
    LineCreated("beta", ENGINE),
    OperatorRegistered("ana"),
    OperatorRegistered("ben"),
    MessageSent("m1", ALPHA, BETA, NOTE, "hello"),
    WakeRequested("w1", "beta", "m1"),
    WakeIssued("w1"),
    LineStateChanged("beta", LineState.RUNNING),
    MessageDelivered("m1"),
    MessageSent("q1", BETA, ANA, QUESTION, "Proceed?", ("yes", "no"), blocking=True),
    MessageSent(
        "q2", BETA, ANA, QUESTION, "Proceed now?", ("yes", "no"), blocking=True
    ),
    QuestionSuperseded("q1", "q2"),
    LineStateChanged("beta", LineState.IDLE),
    MessageDelivered("q2"),
    MessageSent("a2", ANA, BETA, NOTE, "yes", reply_to="q2"),
    WakeRequested("w2", "beta", "a2"),
    WakeIssued("w2"),
    MessageSent("q3", ALPHA, BEN, QUESTION, "Non-ASCII text ✓ \U0001f40d"),
    QuestionWithdrawn("q3"),
    MessageSent("m2", BEN, ALPHA, NOTE, "from an operator"),
    MessageSent("m3", ALPHA, BEN, NOTE, "thank you", reply_to="m2"),
    LineStateChanged("alpha", LineState.STOPPED),
)


@pytest.fixture
def scenario() -> tuple[Event, ...]:
    return SCENARIO
