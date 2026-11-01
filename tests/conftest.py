"""Fixtures shared across the test suite."""

import gc
import shutil
import tempfile
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from typing import cast

from dromon import priority
from dromon import scheduler as scheduler_module
from dromon.processes import ProcessInfo
from dromon.fleet import HOSTS_FILE_ENV
from dromon.registry import REGISTRY_DIR_ENV
from dromon.driver import Strategy
from dromon.lineconfig import LineConfig, encode_config
from dromon.model import (
    HARNESS,
    AgentAccess,
    AgentAddress,
    AgentRegistered,
    AgentRevoked,
    ContextHandedOff,
    Event,
    LineAddress,
    LineConfigured,
    HoldCause,
    LineHeld,
    LineReleased,
    LineHibernated,
    LineAwakened,
    LineOverridden,
    LineSession,
    LineSessionCleared,
    LineRebound,
    LineCreated,
    LineState,
    LineStateChanged,
    MessageAcknowledged,
    MessagePrioritized,
    MessageCorrelated,
    TurnCorrelated,
    LineTurnEnded,
    DisclosureChecked,
    MessageDelivered,
    MessageRead,
    MessageReceived,
    MessageReturned,
    MessageUnconfirmed,
    ReturnCode,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    ProjectOverridden,
    RemoteAddress,
    QuestionSuperseded,
    QuestionWithdrawn,
    WakeIssued,
    WakeRequested,
    WakeScheduled,
    WakeWithdrawn,
    TaskNoted,
    TaskRecorded,
    RestartNoted,
    LineCostMarked,
    ProcessesLeft,
    StopUnverified,
    WalkLimited,
    RoleDeclared,
    SeatBound,
    SeatUnbound,
    RoleHeld,
    RoleHolder,
    RoleRetired,
    LineRetired,
    OwnershipChanged,
    OwnershipCause,
    RoleNoted,
    BriefSet,
    RequestStamp,
    RoleNoteKind,
    BudgetStated,
    BudgetHandoffRequested,
    BudgetSource,
    LeaseAcquired,
    LeaseEnd,
    LeaseReleased,
    RunCleared,
    RunClearance,
    RunEnded,
    RunStarted,
    RunStatus,
    PeerDeclared,
    PeerKeyed,
    PeerOpened,
    PeerRemoved,
)

ENGINE = "claude-opus-5-5"
ALPHA = LineAddress("alpha")
BETA = LineAddress("beta")
ANA = OperatorAddress("ana")
BEN = OperatorAddress("ben")
PROBE = AgentAddress("probe")
KAIZEN = RemoteAddress("keleusma", LineAddress("kaizen"))
KAIZEN_ANA = RemoteAddress("keleusma", OperatorAddress("ana"))
NOTE = MessageKind.NOTE
QUESTION = MessageKind.QUESTION
AT = "2026-10-27T05:00:00Z"

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
    LineHibernated("alpha"),
    LineAwakened("alpha"),
    MessageSent("q5", ALPHA, ANA, QUESTION, "Resume alpha again?", ("resume",)),
    LineHeld("alpha", "q5", HoldCause.COST),
    MessageSent("a5", ANA, ALPHA, NOTE, "resume", reply_to="q5"),
    LineReleased("alpha"),
    MessageSent("m6", ALPHA, ALPHA, NOTE, "a note to my next turn"),
    MessageSent("m3", ALPHA, BEN, NOTE, "thank you", reply_to="m2"),
    MessageSent("m7", ALPHA, BEN, NOTE, "keyed", request=RequestStamp("k1", "b" * 64)),
    MessageSent("h1", HARNESS, BEN, NOTE, "a note the harness wrote"),
    MessageSent("i1", ALPHA, BEN, MessageKind.ISSUE, "a tool's refusal misleads"),
    AgentRegistered("probe", AgentAccess.MESSAGE, "0" * 64),
    MessageSent("m4", PROBE, BETA, NOTE, "from an agent"),
    MessageCorrelated("m4", "game-7.round-2"),
    TurnCorrelated("beta", ("game-7.round-2",), 1, 1_250_000),
    TurnCorrelated("beta", ("game-7.round-2", "game-8"), 2, None),
    LineTurnEnded("beta", "a" * 40),
    LineTurnEnded("beta", None, True),
    DisclosureChecked("beta", "2026-10-28T19:00:00Z", 2),
    MessageSent("m5", BETA, PROBE, NOTE, "to an agent", reply_to="m4"),
    AgentRegistered("guest", AgentAccess.OBSERVE, ""),
    AgentRevoked("guest"),
    WakeScheduled("beta", "s1", "2026-10-26T09:30:00Z", "read the gate's log"),
    WakeScheduled(
        "beta",
        "s2",
        "2026-10-26T10:00:00Z",
        "read it again",
        request=RequestStamp("w1", "c" * 64),
    ),
    MessageSent("s2", BETA, BETA, NOTE, "read it again"),
    WakeScheduled("beta", "s3", "2026-10-26T10:30:00Z", "compact wake"),
    WakeWithdrawn("beta", "s3"),
    TaskNoted("beta", "n1", "b8k2", "completed", "Your background task b8k2 ended."),
    TaskRecorded(
        "beta",
        "b8k2",
        "./scripts/check.sh",
        "completed",
        0,
        "/work/tasks/b8k2.output",
        "0" * 64,
        1024,
        "a" * 40,
        "2026-10-28T19:05:00Z",
    ),
    TaskRecorded(
        "beta",
        "b8k3",
        "make",
        "failed",
        None,
        None,
        None,
        None,
        "",
        "2026-10-28T19:06:00Z",
    ),
    RestartNoted("beta", "n2", "Your last turn ended at a safe point for a restart."),
    LineCostMarked("beta", "session-b", 1_250_000),
    ProcessesLeft("beta", "stopped", (4242, 4243), ("sleep 600", "make watch"), 1),
    WalkLimited("descendants may exist beyond the walk"),
    StopUnverified("beta", "the launcher exited before its stop finished"),
    LineRebound("alpha", "claude-haiku-5-5", "subscription"),
    ContextHandedOff("alpha", "rebound", "given", ("q4", "m3"), 812, "a" * 64),
    RoleDeclared("core", "tmp/worktrees/core"),
    SeatBound("alpha", "core", True),
    SeatBound("beta", "core", False),
    SeatBound("beta", "core", True),
    OwnershipChanged("core", "alpha", None, OwnershipCause.DECLARED, ANA, None, AT),
    RoleNoted("core", "e1", "beta", RoleNoteKind.NOTE, "ready to take it", "", "", AT),
    RoleNoted(
        "core",
        "e2",
        "alpha",
        RoleNoteKind.HANDOFF,
        "the gate passed",
        "a" * 40,
        "b" * 64,
        AT,
    ),
    OwnershipChanged(
        "core", "beta", "alpha", OwnershipCause.HANDOFF, ALPHA, "e2", AT, "alpha"
    ),
    RoleNoted("core", "e3", "beta", RoleNoteKind.RECOVERY, "found clean", "", "", AT),
    BriefSet("beta", "lift the bound", "", (), "", "", "", AT),
    BriefSet(
        "beta",
        "lift the bound",
        "keep the guard",
        ("src/dromon/federation.py", "docs/a b.md"),
        "run the gate",
        "a" * 40,
        "b" * 64,
        AT,
    ),
    OwnershipChanged("core", None, "beta", OwnershipCause.RELINQUISHED, BETA, None, AT),
    OwnershipChanged("core", "alpha", None, OwnershipCause.RECOVERY, ALPHA, None, AT),
    OwnershipChanged("core", "beta", "alpha", OwnershipCause.OPERATOR, BEN, None, AT),
    RoleDeclared("core", "tmp/worktrees/core", 7200),
    RoleHeld("core", RoleHolder.OPERATOR, BEN, AT),
    SeatUnbound("alpha", "core"),
    RoleDeclared("docs", "tmp/worktrees/docs"),
    SeatBound("alpha", "docs", True),
    RoleHeld("core", RoleHolder.FILE, ANA, AT),
    SeatUnbound("alpha", "docs"),
    RoleRetired("docs", BEN, AT),
    LineCreated("delta", ENGINE, "subscription"),
    LineHibernated("delta"),
    LineRetired("delta", BEN, AT),
    BudgetStated(
        "claude-code", 40, "2026-10-28T00:00:00Z", BudgetSource.OPERATOR, "", AT
    ),
    BudgetStated("codex", 0, None, BudgetSource.ENGINE, "a plan limit", AT),
    BudgetStated(
        "claude-code",
        8,
        "2026-10-28T15:00:00Z",
        BudgetSource.ENGINE,
        "five hour",
        AT,
        "five_hour",
    ),
    BudgetHandoffRequested("claude-code", "core", 8, 10, AT),
    LeaseAcquired("gate", "beta", AT, "2026-10-27T08:00:00Z"),
    LeaseReleased("gate", "beta", LeaseEnd.RELEASED),
    LeaseAcquired("device", "beta", AT, "2026-10-27T08:00:00Z"),
    LeaseReleased("device", "beta", LeaseEnd.UNDECLARED),
    RunStarted(
        "r-1",
        "beta",
        "core",
        None,
        "gate",
        ("./scripts/check.sh", "-m", "not live"),
        "/work/tmp/worktrees/core",
        "tmp/runs/r-1.out",
        4321,
        AT,
        AT,
    ),
    RunEnded("r-1", RunStatus.EXITED, 0, "0" * 64, 2048, "", AT),
    RunStarted(
        "r-2",
        "alpha",
        None,
        None,
        "build",
        ("make",),
        "/work",
        "tmp/runs/r-2.out",
        4400,
        AT,
        AT,
    ),
    RunEnded("r-2", RunStatus.LOST, None, None, None, "no receipt", AT),
    RunCleared("r-2", RunClearance.OPERATOR, "ana", AT),
    PeerDeclared("keleusma", "unix", '{"inbound":true}'),
    PeerKeyed("keleusma", "c" * 64),
    MessageReceived("r1", KAIZEN, BETA, "from a peer", None, "m-origin-1", AT, True),
    MessagePrioritized("r1"),
    MessageReceived("r2", KAIZEN_ANA, ANA, "from a peer's operator", None, "m-o2", AT),
    MessageDelivered("r1"),
    MessageSent("p1", BETA, KAIZEN, NOTE, "a reply to a peer", reply_to="r1"),
    MessagePrioritized("p1"),
    MessageSent("p2", ANA, KAIZEN, NOTE, "an operator's note to a peer"),
    MessageSent("p3", BETA, KAIZEN_ANA, NOTE, "a line's note to a peer's operator"),
    MessageDelivered("p1"),
    MessageReturned("p2", ReturnCode.REFUSED, None),
    MessageUnconfirmed("p3", "n3"),
    PeerDeclared("keleusma", "unix", '{"inbound":false}'),
    PeerKeyed("keleusma", ""),
    PeerRemoved("keleusma"),
    PeerOpened("tidepool", "local", AT),
    PeerOpened("f" * 64, "remote", AT),
    PeerDeclared("tidepool", "unix", '{"inbound":true}'),
    LineStateChanged("alpha", LineState.STOPPED),
)


STEERED_REASON = "priority mail m-1 from @ana"
"""The reason the ``steered`` fixture's driver gives to end a turn."""


# Collection on the main thread only. **Found on 2026-10-29,** when a Pi test failed
# twice in one gate and passed alone. A Keleusma virtual machine may be dropped only on
# the thread that made it. A driver a test leaves open sits in a reference cycle until
# the cycle collector runs, and the collector runs in whichever thread allocates when
# its threshold is crossed. In a test that starts a subprocess through asyncio, that
# can be asyncio's child-watcher thread, and the warning that follows is charged to
# whichever test is running. Automatic collection is therefore off for the session,
# and the main thread collects after every COLLECT_EVERY tests, so each leak is
# finalized on the thread that made it. A collection after every test cost about 40
# milliseconds each, which is two and a half minutes over the suite. A test that needs
# a collection within itself calls gc.collect, as before. The daemon closes its drivers
# explicitly and keeps automatic collection, so this is a property of the tests only.
COLLECT_EVERY = 25
_tests_since_collection = [0]


@pytest.fixture(autouse=True, scope="session")
def _no_automatic_collection() -> Iterator[None]:
    gc.disable()
    try:
        yield
    finally:
        gc.enable()
        gc.collect()


@pytest.fixture(autouse=True)
def _collect_on_the_main_thread() -> Iterator[None]:
    yield
    _tests_since_collection[0] += 1
    if _tests_since_collection[0] >= COLLECT_EVERY:
        _tests_since_collection[0] = 0
        gc.collect()


class NoProcesses:
    """A process table with no processes, which a test's scheduler is given in place
    of the operating system's. **Found on 2026-10-29.** Two full gates hung for five
    minutes in ``report_detached``, which ran the real ``/bin/ps`` from a test's
    scheduler and stuck in the child's start, before the call's own timeout applies.
    A test that needs the real table marks itself ``real_processes``."""

    def own_processes(self) -> Sequence[ProcessInfo]:
        return []

    def has_variable(self, pid: int, assignment: str) -> bool:
        return False

    def environments(self) -> dict[int, list[str]]:
        return {}

    def send(self, pid: int, signal_number: int) -> None:
        raise ProcessLookupError(pid)


@pytest.fixture(autouse=True)
def _no_real_process_table(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = cast(pytest.Item, request.node)  # pyright: ignore[reportUnknownMemberType]
    if node.get_closest_marker("real_processes") is None:
        monkeypatch.setattr(scheduler_module, "SystemProcesses", NoProcesses)


@pytest.fixture(autouse=True)
def _private_registry(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give every test a registry of open federation of its own, which a daemon the
    test starts as a process inherits with the environment. **Found on 2026-10-31.**
    The first socket run of two open daemons registered them in the user's real
    registry, and no test may write outside its temporary directories. Not created
    here, so that the daemon's own creation of it with mode 0700 is what a test sees."""
    monkeypatch.setenv(
        REGISTRY_DIR_ENV, str(tmp_path_factory.mktemp("registry") / "registry")
    )
    # The fleet index lies inside the registry, so it is private too, and so is the
    # hosts file of the fleet. Round 13.
    monkeypatch.setenv(
        HOSTS_FILE_ENV, str(tmp_path_factory.mktemp("hosts") / "hosts.toml")
    )


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
