"""Fixtures shared by the data model, codec and event log tests."""

import pytest

from dromon.model import (
    OPERATOR,
    Event,
    LineCreated,
    LineState,
    LineStateChanged,
    MessageDelivered,
    MessageSent,
    QuestionAnswered,
    QuestionAsked,
    QuestionSuperseded,
    QuestionWithdrawn,
    WakeCause,
    WakeIssued,
    WakeRequested,
)

ENGINE = "claude-opus-5-5"

# A valid history that uses every event kind and both wake causes. The coverage tests
# check that it really does, so a new event kind cannot be left out of it silently.
SCENARIO: tuple[Event, ...] = (
    LineCreated("alpha", ENGINE),
    LineCreated("beta", ENGINE),
    MessageSent("m1", "alpha", "beta", "hello"),
    WakeRequested("w1", "beta", WakeCause.MESSAGE, "m1"),
    WakeIssued("w1"),
    LineStateChanged("beta", LineState.RUNNING),
    MessageDelivered("m1"),
    QuestionAsked("q1", "beta", "Proceed?", ("yes", "no"), blocking=True),
    QuestionAsked("q2", "beta", "Proceed now?", ("yes", "no"), blocking=True),
    QuestionSuperseded("q1", "q2"),
    LineStateChanged("beta", LineState.IDLE),
    QuestionAnswered("q2", "yes"),
    WakeRequested("w2", "beta", WakeCause.ANSWER, "q2"),
    WakeIssued("w2"),
    QuestionAsked("q3", "alpha", "Non-ASCII text ✓ \U0001f40d", (), blocking=False),
    QuestionWithdrawn("q3"),
    MessageSent("m2", OPERATOR, "alpha", "from the operator"),
    LineStateChanged("alpha", LineState.STOPPED),
)


@pytest.fixture
def scenario() -> tuple[Event, ...]:
    return SCENARIO
