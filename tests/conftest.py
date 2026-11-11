"""Fixtures shared across the test suite."""

import gc
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from typing import Any, cast

from dromon import hookpolicy, linetemp, priority, spawn
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
    FailureCause,
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
    LineCostRecorded,
    ProcessesLeft,
    StopUnverified,
    WalkLimited,
    RoleDeclared,
    SealedValue,
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
    CommitRunRefused,
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
    LineConfigured(
        "alpha",
        '{"settings":{"env":{"T":"<sealed>"}}}',
        (SealedValue(("settings", "env", "T"), "5" * 64),),
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
    MessageSent("m8", ALPHA, BEN, NOTE, "corrects m7", reply_to="m7", asks_reply=False),
    MessageDelivered("m8"),
    MessageAcknowledged("m8", "the correction needs no reply"),
    MessageSent("h1", HARNESS, BEN, NOTE, "a note the harness wrote"),
    MessageSent("i1", ALPHA, BEN, MessageKind.ISSUE, "a tool's refusal misleads"),
    AgentRegistered("probe", AgentAccess.MESSAGE, "0" * 64),
    MessageSent("m4", PROBE, BETA, NOTE, "from an agent"),
    MessageCorrelated("m4", "game-7.round-2"),
    TurnCorrelated("beta", ("game-7.round-2",), 1, 1_250_000),
    TurnCorrelated("beta", ("game-7.round-2", "game-8"), 2, None),
    LineTurnEnded("beta", "a" * 40),
    LineTurnEnded("beta", None, True),
    LineTurnEnded("beta", None, None, True),
    LineTurnEnded("beta", None, None, True, FailureCause.ENGINE_REPORTED),
    LineTurnEnded("beta", None, True, True, FailureCause.ENGINE_ERROR, "RuntimeError"),
    DisclosureChecked("beta", "2026-10-28T19:00:00Z", 2),
    DisclosureChecked("beta", "2026-10-28T19:01:00Z", 0, 4096),
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
    LineCostRecorded("beta", 1_250_000),
    LineCostRecorded("beta", 40_000, estimated=True),
    LineCostRecorded("beta", None, between=True),
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
        "d" * 64,
    ),
    OwnershipChanged("core", None, "beta", OwnershipCause.RELINQUISHED, BETA, None, AT),
    OwnershipChanged("core", "alpha", None, OwnershipCause.RECOVERY, ALPHA, None, AT),
    OwnershipChanged("core", "beta", "alpha", OwnershipCause.OPERATOR, BEN, None, AT),
    RoleDeclared("core", "tmp/worktrees/core", 7200),
    RoleHeld("core", RoleHolder.OPERATOR, BEN, AT),
    SeatUnbound("alpha", "core"),
    RoleDeclared("docs", "tmp/worktrees/docs", reviews=("core",)),
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
    LeaseAcquired("gate", "beta", AT, "2026-10-27T08:00:00Z", "the sandbox gate"),
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
        "a" * 40,
        "/work/.dromon/commit-runs/r-2",
    ),
    RunEnded("r-2", RunStatus.LOST, None, None, None, "no receipt", AT),
    RunCleared("r-2", RunClearance.OPERATOR, "ana", AT),
    RunStarted(
        "r-3",
        "alpha",
        None,
        "r-2",
        "build",
        ("make",),
        "/work",
        "tmp/runs/r-3.out",
        4500,
        AT,
        AT,
    ),
    RunEnded("r-3", RunStatus.EXITED, 2, None, None, "", AT, "make: *** [all] Error 2"),
    CommitRunRefused("alpha", None, "build", "a" * 40, "no ref points at it", AT),
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


def _worker_threads() -> list[threading.Thread]:
    return [
        thread
        for thread in threading.enumerate()
        if thread.name == "dromon-processes" and thread.is_alive()
    ]


@pytest.fixture(autouse=True)
def _stopped_scheduler_workers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stop the process workers of every scheduler a test builds, when the test ends,
    and fail the test if a worker thread it started is still alive. **Found on
    2026-11-05.** A scheduler starts up to five ``dromon-processes`` threads on first
    use and stops them only in ``stop_workers``, which the daemon calls and almost no
    test did. In a run on a Linux runner a hang left 99 of them, idle, in one dump,
    with ``faulthandler`` showing at most 100 threads, so the main thread was not in
    it. Idle threads do no harm of themselves, but every child start copies a process
    that holds them, a fork under a hundred threads is slower, and a dump that cannot
    reach the main thread cannot be read. ``stop_workers`` is idempotent, so a test
    that stops its own scheduler is not affected. **Porting note.** Python-only, since
    a Rust worker would be dropped with its owner."""
    before = set(_worker_threads())
    built: list[scheduler_module.Scheduler] = []
    original = scheduler_module.Scheduler.__init__

    def record(self: scheduler_module.Scheduler, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
        original(self, *args, **kwargs)
        built.append(self)

    monkeypatch.setattr(scheduler_module.Scheduler, "__init__", record)
    yield
    for each in built:
        each.stop_workers()
    left = [thread for thread in _worker_threads() if thread not in before]
    for thread in left:
        thread.join(2.0)
    left = [thread for thread in left if thread.is_alive()]
    assert not left, f"{len(left)} process worker threads outlived the test"


SLOW_TEST_SECONDS = 5.0
"""A test at least this long is named in the run's output, as it ends."""
SLOW_STARTS_SEEN: list[str] = []
"""Every start that took ``spawn.SLOW_START_SECONDS`` or more in any test, with the
test's name. **Round 16.** The runs on Linux in continuous integration were slow and
hung in places no one could see, and this list is printed at the end of a run, whether
it passed or not, so the next run shows which starts were slow and how loaded the
machine was."""


STRESS_NOTES: list[str] = []
"""One line for each run of ``tests/test_spawn_stress.py``, printed with the slow
starts at the end of the run."""


def pytest_terminal_summary(terminalreporter: Any) -> None:  # noqa: ANN401
    if STRESS_NOTES:
        terminalreporter.section("child start stress", sep="=")
        for line in STRESS_NOTES:
            terminalreporter.write_line(line)
    if not SLOW_STARTS_SEEN:
        return
    terminalreporter.section("slow child starts", sep="=")
    for line in SLOW_STARTS_SEEN[:200]:
        terminalreporter.write_line(line)
    if len(SLOW_STARTS_SEEN) > 200:
        terminalreporter.write_line(f"... and {len(SLOW_STARTS_SEEN) - 200} more")


def _show_now(lines: list[str], request: pytest.FixtureRequest) -> None:
    """Write ``lines`` to the real terminal at once, past pytest's capture, so that a
    run that later hangs and is killed has already shown them, as its summary will
    not. Never raises."""
    if not lines:
        return
    try:
        manager: Any = request.config.pluginmanager.getplugin("capturemanager")
        with manager.global_and_fixture_disabled():
            for line in lines[:5]:
                print(f"slow child start in {line}", file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001, S110
        pass


def slow_test_line(current: str, took: float) -> str | None:
    """The line that names a test that took ``SLOW_TEST_SECONDS`` or more, with the
    load of the machine, or ``None`` for a quicker one."""
    if took < SLOW_TEST_SECONDS:
        return None
    machine = spawn._machine_text()  # pyright: ignore[reportPrivateUsage]
    return f"{current}: slow test, {took:.1f}s, {machine}"


@pytest.fixture(autouse=True)
def _isolated_spawn_state(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[None]:
    """Every test starts with no stranded start, and the real list is put back when it
    ends. **Round 15.** ``dromon.spawn`` counts the starts that stalled past its bound
    in one list for the whole process. Under load a real ``git`` start can stall past
    the bound of 30 seconds in any test, and that one stall left the count raised, so
    later tests that count stranded starts or start a child past the cap failed for a
    cause that was not theirs. The bound is not raised, and a test that meets a real
    stall still fails by itself. A test that checks the counts sets its own, through
    ``stalled_start`` or by patching ``MAX_STRANDED``. The list is replaced by name, as
    ``spawn`` reads it at each call, and ``monkeypatch`` puts the original back."""
    monkeypatch.setattr(spawn, "_stranded", [])
    # Found on 2026-11-05. A real stall left its thread in the set of starts in flight,
    # so every later test counted it, and the three that count the overlap failed. The
    # stuck thread removes itself from whichever set is current when it ends.
    monkeypatch.setattr(spawn, "_active", set[Any]())
    monkeypatch.setattr(spawn, "_watched", set[Any]())
    monkeypatch.setattr(spawn, "_armed", None)
    # Round 16. The slow starts of this test, handed to the run's summary at its end.
    own: list[str] = []
    monkeypatch.setattr(spawn, "_slow_starts", own)
    current = os.environ.get("PYTEST_CURRENT_TEST", "a test").split(" ")[0]
    began = time.monotonic()
    yield
    lines = [f"{current}: {line}" for line in own]
    # Round 16. A test that took long is named at once, past capture, because a run
    # that is later killed by its timeout lists nothing at its end. On 2026-11-11 a
    # Linux run spent three minutes in a file that takes under a second elsewhere, and
    # the log could not say which test, since a file's line is written only when the
    # next file begins.
    slow = slow_test_line(current, time.monotonic() - began)
    if slow is not None:
        lines.append(slow)
    SLOW_STARTS_SEEN.extend(lines)
    _show_now(lines, request)


class StalledStart:
    """Stands in for a start that stalls before the program runs, which no test can
    make the operating system do on demand. While ``stall`` is true, every
    ``subprocess.Popen`` waits for ``gate``, at most a minute, as the read of the exec
    pipe waits in a real stall, and then starts the real program. ``calls`` counts the
    starts attempted, and ``started`` holds each child that did start. **Round 15.**
    See ``dromon.spawn``."""

    def __init__(self) -> None:
        self.gate = threading.Event()
        self.stall = True
        self.calls = 0
        self.started: list[subprocess.Popen[Any]] = []

    def until_ended(self, seconds: float = 10.0) -> None:
        """Wait until no start is stuck, within ``seconds``."""
        deadline = time.monotonic() + seconds
        while spawn.stranded_count() and time.monotonic() < deadline:
            time.sleep(0.01)

    def until_reaped(self, seconds: float = 10.0) -> subprocess.Popen[Any]:
        """The one child started, once it has ended and its pipes are closed, which
        the late-start reap does last. Fails the test when that takes longer than
        ``seconds``."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.started:
                child = self.started[0]
                pipes = [child.stdin, child.stdout, child.stderr]
                # Found on 2026-11-05. A child with no pipe has none to be closed, so
                # the pipes alone said "reaped" before the late-start reap had
                # killed it, and a test that then polled failed one run in six.
                if child.poll() is not None and all(
                    pipe is None or pipe.closed for pipe in pipes
                ):
                    return child
            time.sleep(0.01)
        pytest.fail("the late child was not reaped in time")


@pytest.fixture
def stalled_start(monkeypatch: pytest.MonkeyPatch) -> Iterator[StalledStart]:
    fake = StalledStart()

    class Gated(subprocess.Popen[Any]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401
            fake.calls += 1
            if fake.stall:
                fake.gate.wait(60)
            super().__init__(*args, **kwargs)
            fake.started.append(self)

    monkeypatch.setattr(subprocess, "Popen", Gated)
    yield fake
    fake.stall = False
    fake.gate.set()
    fake.until_ended()
    for child in fake.started:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.fixture(autouse=True)
def _no_machine_hook_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the machine's own managed Claude Code policy out of every test, since a
    view-only line is refused where that policy switches hooks off. A test of the
    reader names its directories. Round 14."""
    monkeypatch.setattr(hookpolicy, "MANAGED_DIRECTORIES", ())


@pytest.fixture(autouse=True)
def _private_line_temp(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Give every test's lines temporary directories of its own, under the test's
    own directory, since a line the factory builds with an enabled sandbox makes
    them, and no test may write the machine's shared temporary directory. **Round 14,
    item 6.**"""
    base = tmp_path_factory.mktemp("linetemp")
    monkeypatch.setattr(linetemp, "base_directory", lambda: base)


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
