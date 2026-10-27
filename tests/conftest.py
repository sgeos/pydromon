"""Fixtures shared across the test suite."""

import shutil
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from dromon import priority
from dromon.driver import Strategy
from dromon.lineconfig import LineConfig, encode_config
from dromon.model import (
    AgentAccess,
    AgentAddress,
    AgentRegistered,
    AgentRevoked,
    ContextHandedOff,
    Event,
    LineAddress,
    LineConfigured,
    LineHeld,
    LineReleased,
    LineMuted,
    LineUnmuted,
    LineOverridden,
    LineSession,
    LineSessionCleared,
    LineRebound,
    LineCreated,
    LineState,
    LineStateChanged,
    MessageAcknowledged,
    MessagePrioritized,
    MessageDelivered,
    MessageRead,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    ProjectOverridden,
    QuestionSuperseded,
    QuestionWithdrawn,
    WakeIssued,
    WakeRequested,
    WakeScheduled,
    WakeWithdrawn,
    TaskNoted,
    RestartNoted,
    LineCostMarked,
    ProcessesLeft,
    StopUnverified,
    WalkLimited,
)

ENGINE = "claude-opus-5-5"
ALPHA = LineAddress("alpha")
BETA = LineAddress("beta")
ANA = OperatorAddress("ana")
BEN = OperatorAddress("ben")
PROBE = AgentAddress("probe")
NOTE = MessageKind.NOTE
QUESTION = MessageKind.QUESTION

# A valid history that uses every event kind, both message kinds and every address kind
# as sender and recipient, with two operators. The coverage tests check that it really
# does, so a new event kind cannot be left out of it silently.
SCENARIO: tuple[Event, ...] = (
    LineCreated("alpha", ENGINE, "subscription"),
    LineCreated("beta", ENGINE, "subscription"),
    LineConfigured(
        "beta",
        encode_config(
            LineConfig(builtin_tools=("Read",), allowed_tools=("mcp__docs__*",))
        ),
    ),
    LineSession("beta", "0b9f2c6e-session"),
    LineSessionCleared("beta"),
    ContextHandedOff("beta", "cleared", "opted_out", (), 0, ""),
    LineSession("beta", "0b9f2c6e-session"),
    LineOverridden("alpha", '{"effort":"low"}'),
    ProjectOverridden('{"limits.max_cost_usd":"5"}'),
    OperatorRegistered("ana"),
    OperatorRegistered("ben"),
    MessageSent("m1", ALPHA, BETA, NOTE, "hello"),
    WakeRequested("w1", "beta", "m1"),
    WakeIssued("w1"),
    LineStateChanged("beta", LineState.RUNNING),
    MessageDelivered("m1"),
    MessageRead("m1"),
    MessageAcknowledged("m1"),
    MessageSent("q1", BETA, ANA, QUESTION, "Proceed?", ("yes", "no")),
    MessageSent("q2", BETA, ANA, QUESTION, "Proceed now?", ("yes", "no")),
    QuestionSuperseded("q1", "q2"),
    LineStateChanged("beta", LineState.IDLE),
    MessageDelivered("q2"),
    MessageSent("a2", ANA, BETA, NOTE, "yes", reply_to="q2"),
    WakeRequested("w2", "beta", "a2"),
    WakeIssued("w2"),
    MessageSent("q4", ALPHA, ANA, QUESTION, "Resume alpha?", ("resume",)),
    LineHeld("alpha", "q4"),
    MessageSent("a4", ANA, ALPHA, NOTE, "resume", reply_to="q4"),
    LineReleased("alpha"),
    MessageSent("q3", ALPHA, BEN, QUESTION, "Non-ASCII text ✓ \U0001f40d"),
    QuestionWithdrawn("q3"),
    MessageSent("m2", BEN, ALPHA, NOTE, "from an operator"),
    MessagePrioritized("m2"),
    LineMuted("alpha"),
    LineUnmuted("alpha"),
    MessageSent("m6", ALPHA, ALPHA, NOTE, "a note to my next turn"),
    MessageSent("m3", ALPHA, BEN, NOTE, "thank you", reply_to="m2"),
    AgentRegistered("probe", AgentAccess.MESSAGE, "0" * 64),
    MessageSent("m4", PROBE, BETA, NOTE, "from an agent"),
    MessageSent("m5", BETA, PROBE, NOTE, "to an agent", reply_to="m4"),
    AgentRegistered("guest", AgentAccess.OBSERVE, ""),
    AgentRevoked("guest"),
    WakeScheduled("beta", "s1", "2026-10-26T09:30:00Z", "read the gate's log"),
    WakeScheduled("beta", "s2", "2026-10-26T10:00:00Z", "read it again"),
    MessageSent("s2", BETA, BETA, NOTE, "read it again"),
    WakeScheduled("beta", "s3", "2026-10-26T10:30:00Z", "compact wake"),
    WakeWithdrawn("beta", "s3"),
    TaskNoted("beta", "n1", "b8k2", "completed", "Your background task b8k2 ended."),
    RestartNoted("beta", "n2", "Your last turn ended at a safe point for a restart."),
    LineCostMarked("beta", "session-b", 1_250_000),
    ProcessesLeft("beta", "stopped", (4242, 4243), ("sleep 600", "make watch"), 1),
    WalkLimited("descendants may exist beyond the walk"),
    StopUnverified("beta", "the launcher exited before its stop finished"),
    LineRebound("alpha", "claude-haiku-5-5", "subscription"),
    ContextHandedOff("alpha", "rebound", "given", ("q4", "m3"), 812, "a" * 64),
    LineStateChanged("alpha", LineState.STOPPED),
)


STEERED_REASON = "priority mail m-1 from @ana"
"""The reason the ``steered`` fixture's driver gives to end a turn."""


@pytest.fixture
def steered() -> Callable[[str, Strategy], tuple[priority.SteeredDriver, list[int]]]:
    """A steered driver for a model and strategy whose steer always asks to end the
    turn for priority mail, and the list of the driver's tool-use count each time it
    was asked, the first part of every packaged driver's state. An adapter test can
    so tell a safe point after a tool result from one at the text before it."""

    def make(
        model: str, strategy: Strategy
    ) -> tuple[priority.SteeredDriver, list[int]]:
        asked: list[int] = []
        driver = priority.steered(model, strategy, lambda: None)

        def steer() -> str:
            asked.append(driver.state[0])
            return STEERED_REASON

        driver.redirect = steer
        return driver, asked

    return make


@pytest.fixture
def scenario() -> tuple[Event, ...]:
    return SCENARIO


@pytest.fixture
def short_runtime() -> Iterator[Path]:
    """A runtime directory path that does not exist yet, short enough for a socket.

    Pytest's own temporary paths can exceed the Unix socket path limit.
    """
    parent = Path(tempfile.mkdtemp(prefix="dr"))
    yield parent / "run"
    shutil.rmtree(parent, ignore_errors=True)
