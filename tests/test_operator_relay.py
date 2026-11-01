"""The operators' relay wired into the daemon, the file sink, and issues from lines.
Round 11, items 1 to 3.

The relayer is driven one ``step`` at a time with a fake clock, so each test states
which pass forwarded what. The command relay runs real short commands, since its
timeout, its process group and the event loop staying free are the behaviours under
test."""

import asyncio
import contextlib
import json
import os
import inspect
import stat
import subprocess
import sys
import time
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.cli import _relay_rows  # pyright: ignore[reportPrivateUsage]
from dromon.codec import decode, encode
from dromon.configview import Overrides
from dromon.daemon import Daemon, ProjectMismatch, protected_dirs
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.mailbox import listing
from dromon.model import (
    EventRejected,
    LineAddress,
    MessageKind,
    MessageSent,
    OperatorAddress,
    State,
)
from dromon.paths import ProjectPaths, project_paths
from dromon import processes as processes_module
from dromon.processes import TreeRow
from dromon.projectconfig import ConfigFileError, ProjectConfig, config_path, parse
import dromon.relay as relay_module
from dromon.relay import (
    FIRST_BACKOFF_SECONDS,
    SWEEP_SECONDS,
    AsyncRelay,
    CommandRelay,
    FileRelay,
    Notice,
    OperatorRelayer,
    RelayConfig,
    RelayDefinition,
    RelayError,
    RelayKind,
    encode_notice,
    sink_refusal,
)
from dromon.scheduler import (
    AGENT_SEND_TOOL_SCHEMA,
    SEND_TOOL_SCHEMA,
    LineEngine,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
BEN = OperatorAddress("ben")


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Recorder:
    """An asynchronous relay that keeps what it accepts, or refuses while told to."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.notices: list[Notice] = []
        self.refuse = refuse

    async def deliver(self, notice: Notice) -> None:
        if self.refuse:
            raise RelayError("channel unavailable")
        self.notices.append(notice)


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


@pytest.fixture
def scheduler(tmp_path: Path) -> Generator[Scheduler]:
    with EventLog(tmp_path / "events.sqlite") as log:
        found = Scheduler(log, no_engine, hold_wakes=True)
        found.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        found.ensure_operator("ana")
        found.ensure_operator("ben")
        yield found


def relayer_for(
    scheduler: Scheduler, routes: dict[str, AsyncRelay], clock: Clock | None = None
) -> OperatorRelayer:
    relayer = OperatorRelayer(scheduler, routes, clock or Clock())
    scheduler.on_operator_mail = relayer.notify
    return relayer


def call(tool: SdkMcpTool[Any], args: dict[str, object]) -> tuple[str, bool]:
    async def invoke() -> dict[str, Any]:
        return await tool.handler(args)

    result = asyncio.run(invoke())
    return cast(str, result["content"][0]["text"]), result.get("is_error") is True


# Item 1. Forwarding through the relayer.


def test_a_relayed_message_is_delivered_and_stays_unread(
    scheduler: Scheduler,
) -> None:
    """Fact. Relay delivery is delivery, not reading, so ``dromon mail --unread``
    still lists the message until the operator reads or acknowledges it."""
    ana = Recorder()
    relayer = relayer_for(scheduler, {"ana": ana})
    question = scheduler.ask("alpha", ANA, "Merge now?", ("yes", "no"))
    relayer.ready.clear()
    note = scheduler.send(LineAddress("alpha"), ANA, "done")
    assert relayer.ready.is_set(), "recording mail to an operator wakes the relayer"
    asyncio.run(relayer.step())
    assert [n.message_id for n in ana.notices] == [question, note]
    messages = scheduler.state.messages
    assert messages[question].delivered and not messages[question].read
    unread, total = listing(scheduler.state, ANA, unread_only=True)
    assert total == 2 and [m for m, _ in unread] == [question, note]
    assert relayer.report()["delivered"] == 2
    asyncio.run(relayer.step())
    assert len(ana.notices) == 2, "a delivered message is not relayed again"


def test_a_failing_relay_keeps_its_notice_and_backs_off(scheduler: Scheduler) -> None:
    clock = Clock()
    ana = Recorder(refuse=True)
    relayer = relayer_for(scheduler, {"ana": ana}, clock)
    message_id = scheduler.send(LineAddress("alpha"), ANA, "done")
    wait = asyncio.run(relayer.step())
    assert wait == FIRST_BACKOFF_SECONDS
    assert not scheduler.state.messages[message_id].delivered
    report = relayer.report()
    assert report["failing"] == 1
    assert report["failures"] == [
        {
            "message_id": message_id,
            "operator": "ana",
            "attempts": 1,
            "reason": "channel unavailable",
            "retry_in_seconds": FIRST_BACKOFF_SECONDS,
        }
    ]
    ana.refuse = False
    asyncio.run(relayer.step())
    assert ana.notices == [], "a notice in its backoff is not tried again"
    clock.now += FIRST_BACKOFF_SECONDS
    assert asyncio.run(relayer.step()) == SWEEP_SECONDS
    assert [n.message_id for n in ana.notices] == [message_id]
    assert scheduler.state.messages[message_id].delivered
    assert relayer.report()["failing"] == 0


def test_the_backoff_doubles_up_to_its_bound(scheduler: Scheduler) -> None:
    clock = Clock()
    relayer = relayer_for(scheduler, {"ana": Recorder(refuse=True)}, clock)
    scheduler.send(LineAddress("alpha"), ANA, "done")
    retries: list[object] = []
    waits: list[float] = []
    for _ in range(9):
        waits.append(asyncio.run(relayer.step()))
        failures = cast(list[dict[str, object]], relayer.report()["failures"])
        retries.append(failures[0]["retry_in_seconds"])
        clock.now += 1000
    assert retries == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0, 300.0]
    assert waits == [5.0, 10.0, 20.0, 40.0, *[SWEEP_SECONDS] * 5], (
        "a pass waits for the backoff or the sweep, whichever ends first"
    )


def test_a_defect_in_a_relay_fails_its_notice_not_the_relayer(
    scheduler: Scheduler, capsys: pytest.CaptureFixture[str]
) -> None:
    class Broken:
        async def deliver(self, notice: Notice) -> None:
            raise KeyError("bug")

    good = Recorder()
    relayer = relayer_for(scheduler, {"ana": Broken(), "ben": good})
    first = scheduler.send(LineAddress("alpha"), ANA, "for ana")
    second = scheduler.send(LineAddress("alpha"), BEN, "for ben")
    asyncio.run(relayer.step())
    assert not scheduler.state.messages[first].delivered
    assert [n.message_id for n in good.notices] == [second]
    assert "the relay failed unexpectedly" in str(relayer.report()["failures"])
    assert "KeyError" in capsys.readouterr().err


def test_a_hung_operator_relay_does_not_hold_another_operator(
    scheduler: Scheduler,
) -> None:
    first = scheduler.send(LineAddress("alpha"), ANA, "for ana")
    second = scheduler.send(LineAddress("alpha"), BEN, "for ben")

    class Hanging:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def deliver(self, notice: Notice) -> None:
            del notice
            self.started.set()
            await self.release.wait()

    class Fast:
        def __init__(self) -> None:
            self.delivered = asyncio.Event()

        async def deliver(self, notice: Notice) -> None:
            assert notice.message_id == second
            self.delivered.set()

    slow, fast = Hanging(), Fast()
    relayer = relayer_for(scheduler, {"ana": slow, "ben": fast})

    async def scenario() -> None:
        task = asyncio.create_task(relayer.step())
        await asyncio.wait_for(slow.started.wait(), 1)
        await asyncio.wait_for(fast.delivered.wait(), 1)
        assert not scheduler.state.messages[first].delivered
        assert scheduler.state.messages[second].delivered
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_operators_sharing_a_relay_keep_their_notices_in_order(
    scheduler: Scheduler,
) -> None:
    first = scheduler.send(LineAddress("alpha"), ANA, "first")
    second = scheduler.send(LineAddress("alpha"), BEN, "second")
    shared = Recorder()
    relayer = relayer_for(scheduler, {"ana": shared, "ben": shared})
    asyncio.run(relayer.step())
    assert [notice.message_id for notice in shared.notices] == [first, second]


def test_mail_for_an_unrouted_operator_waits_for_a_client(
    scheduler: Scheduler,
) -> None:
    ana = Recorder()
    relayer = relayer_for(scheduler, {"ana": ana})
    for_ben = scheduler.send(LineAddress("alpha"), BEN, "for ben")
    asyncio.run(relayer.step())
    assert ana.notices == []
    assert not scheduler.state.messages[for_ben].delivered
    assert relayer.report()["unrouted"] == 1
    scheduler.collect(BEN)
    asyncio.run(relayer.step())
    assert relayer.report()["unrouted"] == 0


def test_a_client_delivery_during_the_relay_is_recorded_once(
    scheduler: Scheduler,
) -> None:
    """A client listing the mailbox while the relay runs delivers the message
    first, and the relay's acceptance then records nothing more, since the log
    refuses a second ``message_delivered``."""

    class Racing:
        def __init__(self) -> None:
            self.notices: list[str] = []

        async def deliver(self, notice: Notice) -> None:
            self.notices.append(notice.message_id)
            scheduler.collect(ANA)

    racing = Racing()
    relayer = relayer_for(scheduler, {"ana": racing})
    first = scheduler.send(LineAddress("alpha"), ANA, "one")
    second = scheduler.send(LineAddress("alpha"), ANA, "two")
    asyncio.run(relayer.step())
    assert racing.notices == [first], "the second was delivered before its turn"
    assert scheduler.state.messages[second].delivered
    assert relayer.report()["delivered"] == 0
    assert scheduler.relayed(first) is False


def test_a_stop_between_acceptance_and_the_record_relays_again(
    tmp_path: Path,
) -> None:
    """At least once. The relay accepts and the relayer is stopped before it
    records the delivery, as a crash would stop it. After a restart the same
    message identifier is relayed again, which lets a reader see the repeat."""
    accepted = asyncio.Event()
    seen: list[str] = []

    class Accepting:
        async def deliver(self, notice: Notice) -> None:
            seen.append(notice.message_id)
            accepted.set()
            await asyncio.Event().wait()

    path = tmp_path / "events.sqlite"
    with EventLog(path) as log:
        first = Scheduler(log, no_engine, hold_wakes=True)
        first.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        first.ensure_operator("ana")
        message_id = first.send(LineAddress("alpha"), ANA, "done")
        relayer = relayer_for(first, {"ana": Accepting()})

        async def crash() -> None:
            task = asyncio.create_task(relayer.step())
            await accepted.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(crash())
        assert not log.state.messages[message_id].delivered
    with EventLog(path) as log:
        second = Scheduler(log, no_engine, hold_wakes=True)
        again = Recorder()
        asyncio.run(relayer_for(second, {"ana": again}).step())
        assert seen == [message_id]
        assert [n.message_id for n in again.notices] == [message_id]
        assert log.state.messages[message_id].delivered


def test_new_routes_apply_from_the_next_pass(scheduler: Scheduler) -> None:
    clock = Clock()
    old, new = Recorder(refuse=True), Recorder()
    relayer = relayer_for(scheduler, {"ana": old}, clock)
    message_id = scheduler.send(LineAddress("alpha"), ANA, "done")
    asyncio.run(relayer.step())
    relayer.ready.clear()
    relayer.set_routes({"ana": new})
    assert relayer.ready.is_set()
    asyncio.run(relayer.step())
    assert [n.message_id for n in new.notices] == [message_id], (
        "a changed route is tried at once, without waiting out the old backoff"
    )


def test_the_relayer_survives_a_failing_pass_and_stops_when_told() -> None:
    class Failing:
        @property
        def state(self) -> State:
            raise RuntimeError("the log is gone")

        def relayed(self, message_id: str) -> bool:
            return False

    async def scenario() -> None:
        stop = asyncio.Event()
        relayer = OperatorRelayer(Failing(), {}, Clock())
        task = asyncio.create_task(relayer.run(stop))
        await asyncio.sleep(0.05)
        assert not task.done()
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


# The command relay off the event loop.


def notice(message_id: str = "m-1", kind: MessageKind = MessageKind.NOTE) -> Notice:
    return Notice(message_id, "alpha", "ana", kind, "body", (), None)


def test_the_command_relay_delivers_without_blocking_the_loop(tmp_path: Path) -> None:
    out = tmp_path / "out.json"
    script = (
        "import sys,time; time.sleep(0.3);"
        f" open({str(out)!r},'w').write(sys.stdin.read())"
    )
    relay = CommandRelay([sys.executable, "-c", script], 5)

    async def scenario() -> int:
        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        counting = asyncio.create_task(ticker())
        await relay.deliver(notice())
        counting.cancel()
        return ticks

    assert asyncio.run(scenario()) >= 10, "the loop ran while the command did"
    assert json.loads(out.read_text()) == json.loads(encode_notice(notice()))


def test_a_hung_command_times_out_and_its_group_is_killed(tmp_path: Path) -> None:
    child = tmp_path / "child.pid"
    script = (
        "import os,subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        f"open({str(child)!r},'w').write(str(p.pid)); time.sleep(60)"
    )
    relay = CommandRelay([sys.executable, "-c", script], 1.0)
    started = time.monotonic()
    with pytest.raises(RelayError, match="timed out after 1.0s"):
        asyncio.run(relay.deliver(notice()))
    assert time.monotonic() - started < 10
    pid = int(child.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the command's child outlived the timeout")


def _ps_runs() -> bool:
    try:
        found = subprocess.run(  # noqa: S603
            ["/bin/ps", "-o", "pid=", "-p", str(os.getpid())],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return False
    return found.returncode == 0


NEEDS_PS = pytest.mark.skipif(
    not _ps_runs(), reason="ps cannot run here, as inside an agent sandbox"
)


def _gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@NEEDS_PS
def test_a_command_child_that_starts_a_session_is_ended_with_the_command(
    tmp_path: Path,
) -> None:
    """Round 15, item 2. A descendant that called ``setsid`` is in a group of its
    own, which a signal to the command's group misses. The walk of the process table
    finds it by ancestry, so the timeout ends it too."""
    child = tmp_path / "escaped.pid"
    marker = tmp_path / "escaped.marker"
    child_code = (
        f"import time; time.sleep(1.5); open({str(marker)!r},'w').write('alive')"
    )
    script = (
        "import subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}],start_new_session=True);"
        f"open({str(child)!r},'w').write(str(p.pid)); time.sleep(30)"
    )
    relay = CommandRelay([sys.executable, "-c", script], 0.5)
    with pytest.raises(RelayError, match="timed out"):
        asyncio.run(relay.deliver(notice()))
    pid = int(child.read_text())
    try:
        assert _gone(pid), "the session leader outlived the command"
        time.sleep(1.5)
        assert not marker.exists(), "the child ran on after the timeout"
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, 9)


@NEEDS_PS
def test_ending_a_command_does_not_reach_a_process_it_did_not_start(
    tmp_path: Path,
) -> None:
    """The walk finds descendants by ancestry alone, so a neighbour in a session of
    its own, which the test starts and the command does not, is left running."""
    neighbour = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    try:
        relay = CommandRelay([sys.executable, "-c", "import time; time.sleep(30)"], 0.3)
        with pytest.raises(RelayError, match="timed out"):
            asyncio.run(relay.deliver(notice()))
        assert neighbour.poll() is None
    finally:
        neighbour.kill()
        neighbour.wait()


def test_the_processes_a_walk_finds_are_killed_and_no_others(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without ``ps``: the walk is stood in for, finding the command's session
    leader and nothing else. The leader is killed, and a neighbour that the walk does
    not name is left running."""
    child = tmp_path / "escaped.pid"
    script = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],"
        "start_new_session=True);"
        f"open({str(child)!r},'w').write(str(p.pid)); time.sleep(60)"
    )
    neighbour = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )

    def walk(root: int) -> list[TreeRow]:
        deadline = time.monotonic() + 3
        while not child.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        pid = int(child.read_text())
        command_pid[0] = root
        return [TreeRow(pid, root, pid, "sleep", STARTED)]

    def identity(pid: int) -> tuple[int, str] | None:
        return (command_pid[0], STARTED) if pid == int(child.read_text()) else None

    command_pid: list[int] = [0]
    monkeypatch.setattr(relay_module, "attached", walk)
    monkeypatch.setattr(processes_module, "identity_of", identity)
    relay = CommandRelay([sys.executable, "-c", script], 1.0)
    try:
        with pytest.raises(RelayError, match="timed out"):
            asyncio.run(relay.deliver(notice()))
        assert _gone(int(child.read_text())), "the process the walk named survived"
        assert neighbour.poll() is None, "a process the walk did not name was killed"
    finally:
        neighbour.kill()
        neighbour.wait()
        with contextlib.suppress(ProcessLookupError, ValueError, OSError):
            os.kill(int(child.read_text()), 9)


STARTED = "Sat Nov  1 07:23:05 2026"
"""A start time as a walk reads it, for the rows a test stands in for the walk with."""


@pytest.mark.parametrize("changed", ["start", "parent", "gone"])
def test_a_process_whose_identity_changed_since_the_walk_is_not_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    """Re-review of 4da3e0e. Between the walk and the signal a descendant can end and
    its pid go to a process that is not a descendant. The neighbour here stands for
    that process, and the walk names it. Its start time, its parent or its absence
    differs from the row, so it is left running, and the group is still ended."""
    neighbour = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    command_child = tmp_path / "child.pid"
    script = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        f"open({str(command_child)!r},'w').write(str(p.pid)); time.sleep(60)"
    )
    root: list[int] = [0]

    def walk(pid: int) -> list[TreeRow]:
        root[0] = pid
        return [TreeRow(neighbour.pid, pid, neighbour.pid, "sleep", STARTED)]

    def identity(pid: int) -> tuple[int, str] | None:
        if changed == "gone":
            return None
        if changed == "start":
            return root[0], "Sun Nov  2 00:00:00 2026"
        return root[0] + 1, STARTED

    monkeypatch.setattr(relay_module, "attached", walk)
    monkeypatch.setattr(processes_module, "identity_of", identity)
    relay = CommandRelay([sys.executable, "-c", script], 0.6)
    try:
        with pytest.raises(RelayError, match="timed out"):
            asyncio.run(relay.deliver(notice()))
        assert neighbour.poll() is None, "a process with another identity was killed"
        assert _gone(int(command_child.read_text())), "the group was not ended"
    finally:
        neighbour.kill()
        neighbour.wait()


def test_a_row_with_no_start_time_is_never_signalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeping = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:

        def identity(pid: int) -> tuple[int, str]:
            return os.getpid(), ""

        monkeypatch.setattr(processes_module, "identity_of", identity)
        row = TreeRow(sleeping.pid, os.getpid(), sleeping.pid, "sleep")
        assert processes_module.signal_if_same(row, 9) is False
        assert sleeping.poll() is None
    finally:
        sleeping.kill()
        sleeping.wait()


def test_a_cancellation_while_ending_the_group_closes_the_pipe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-review of 4da3e0e. A second cancellation that arrives while the group is
    being ended must not skip the close of the read descriptor, which left its loop
    reader behind. The walk is slow, so the cancellation lands inside it, and the
    command must still end."""

    def slow(root: int) -> list[TreeRow] | None:
        time.sleep(1.0)
        return []

    monkeypatch.setattr(relay_module, "attached", slow)
    relay = CommandRelay([sys.executable, "-c", "import time; time.sleep(30)"], 0.3)
    pids: list[int] = []
    real_exec = asyncio.create_subprocess_exec

    async def recording(*arguments: object, **options: object) -> object:
        process = await real_exec(*arguments, **options)  # pyright: ignore[reportArgumentType, reportUnknownVariableType]
        pids.append(process.pid)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        return process  # pyright: ignore[reportUnknownVariableType]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", recording)

    async def scenario() -> None:
        task = asyncio.create_task(relay.deliver(notice()))
        # The timeout is 0.3 seconds, so the group is being ended soon after.
        await asyncio.sleep(0.6)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    asyncio.run(scenario())
    before = _open_descriptors()
    asyncio.run(scenario())
    assert _open_descriptors() == before
    assert pids and all(_gone(pid) for pid in pids)


def test_the_loop_stays_responsive_while_each_descendant_is_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-review of 6ce43e8. Each check runs ``ps`` for up to a second, and the event
    loop serves every line, so the checks run in a thread. Each stands in here with a
    sleep of 0.4 seconds, three in all, and a ticker on the loop must never wait
    anything like that long. The command must still end."""
    rows = [
        TreeRow(50_000 + number, 1, 50_000 + number, "x", "t") for number in range(3)
    ]
    signalled: list[int] = []

    def walk(root: int) -> list[TreeRow] | None:
        return rows

    def slow_signal(row: TreeRow, number: int) -> bool:
        time.sleep(0.4)
        signalled.append(row.pid)
        return False

    monkeypatch.setattr(relay_module, "attached", walk)
    monkeypatch.setattr(relay_module, "signal_if_same", slow_signal)
    relay = CommandRelay([sys.executable, "-c", "import time; time.sleep(30)"], 0.3)
    gaps: list[float] = []

    async def scenario() -> None:
        stop = asyncio.Event()

        async def ticker() -> None:
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        watching = asyncio.create_task(ticker())
        try:
            with pytest.raises(RelayError, match="timed out"):
                await relay.deliver(notice())
        finally:
            stop.set()
            await watching

    started = time.monotonic()
    asyncio.run(scenario())
    assert time.monotonic() - started >= 1.2, "the checks did not all run"
    assert sorted(signalled) == sorted(row.pid for row in rows)
    assert gaps and max(gaps) < 0.3, f"the loop stalled for {max(gaps):.2f} seconds"


def test_a_loop_that_cannot_watch_the_pipe_leaves_no_descriptor() -> None:
    class Refusing:
        def add_reader(self, *arguments: object) -> None:
            raise RuntimeError("no reader")

    before = _open_descriptors()
    with pytest.raises(RuntimeError, match="no reader"):
        relay_module._StderrTail(Refusing())  # pyright: ignore[reportPrivateUsage, reportArgumentType]
    assert _open_descriptors() == before


def test_a_walk_that_fails_still_kills_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process table that cannot be read leaves the group kill alone, which still
    ends the command and its group."""
    child = tmp_path / "child.pid"
    script = (
        "import subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']);"
        f"open({str(child)!r},'w').write(str(p.pid)); time.sleep(60)"
    )

    def unreadable(root: int) -> list[TreeRow] | None:
        return None

    monkeypatch.setattr(relay_module, "attached", unreadable)
    relay = CommandRelay([sys.executable, "-c", script], 0.5)
    with pytest.raises(RelayError, match="timed out"):
        asyncio.run(relay.deliver(notice()))
    assert _gone(int(child.read_text()))


def test_success_does_not_wait_for_a_child_holding_stderr(tmp_path: Path) -> None:
    child = tmp_path / "child.pid"
    child_code = "import time; time.sleep(20)"
    script = (
        "import subprocess,sys;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        f"open({str(child)!r},'w').write(str(p.pid));"
        "sys.exit(0)"
    )
    relay = CommandRelay([sys.executable, "-c", script], 2.0)
    started = time.monotonic()
    asyncio.run(relay.deliver(notice()))
    assert time.monotonic() - started < 1.0
    with contextlib.suppress(ProcessLookupError):
        os.kill(int(child.read_text()), 9)


def test_a_failing_command_reports_its_status_and_bounded_detail() -> None:
    script = "import sys; sys.stderr.write('x' * 100000); sys.exit(3)"
    relay = CommandRelay([sys.executable, "-c", script], 5)
    with pytest.raises(RelayError, match="exited with status 3") as caught:
        asyncio.run(relay.deliver(notice()))
    assert len(str(caught.value)) < 300


def _open_descriptors() -> int:
    return len(os.listdir("/dev/fd"))


def test_the_relay_reaches_no_private_attribute_of_asyncio() -> None:
    """Round 15, item 1. The stderr pipe is Dromon's own, closed with public
    interfaces, so nothing reads a transport's private attribute."""
    assert "._transport" not in inspect.getsource(relay_module)


def test_the_kept_stderr_is_what_the_command_wrote_before_it_exited() -> None:
    script = "import sys; sys.stderr.write('refused: ' + 'y' * 50); sys.exit(4)"
    relay = CommandRelay([sys.executable, "-c", script], 5)
    with pytest.raises(RelayError, match="exited with status 4: refused: y") as caught:
        asyncio.run(relay.deliver(notice()))
    assert "y" * 50 in str(caught.value)


def test_a_descendant_that_keeps_writing_stderr_neither_delays_nor_grows_the_result(
    tmp_path: Path,
) -> None:
    child = tmp_path / "writer.pid"
    child_code = (
        "import sys,time\n"
        "while True:\n"
        "    try: sys.stderr.write('z' * 4096); sys.stderr.flush()\n"
        "    except Exception: break\n"
        "    time.sleep(0.001)\n"
    )
    script = (
        "import subprocess,sys;"
        f"p=subprocess.Popen([sys.executable,'-c',{child_code!r}]);"
        f"open({str(child)!r},'w').write(str(p.pid));"
        "sys.stderr.write('last words'); sys.exit(5)"
    )
    relay = CommandRelay([sys.executable, "-c", script], 5.0)
    started = time.monotonic()
    with pytest.raises(RelayError, match="exited with status 5") as caught:
        asyncio.run(relay.deliver(notice()))
    assert time.monotonic() - started < 2.0
    assert len(str(caught.value)) < 300
    with contextlib.suppress(ProcessLookupError):
        os.kill(int(child.read_text()), 9)


def test_delivering_leaves_no_descriptor_open_on_any_path(tmp_path: Path) -> None:
    ok = CommandRelay([sys.executable, "-c", "import sys; sys.stdin.read()"], 5)
    failing = CommandRelay([sys.executable, "-c", "import sys; sys.exit(2)"], 5)
    hung = CommandRelay([sys.executable, "-c", "import time; time.sleep(30)"], 0.3)
    missing = CommandRelay([str(tmp_path / "absent")], 5)

    async def attempt(relay: CommandRelay) -> None:
        with contextlib.suppress(RelayError):
            await relay.deliver(notice())

    async def rounds() -> None:
        for relay in (ok, failing, hung, missing):
            await attempt(relay)

    asyncio.run(rounds())
    before = _open_descriptors()
    for _ in range(3):
        asyncio.run(rounds())
    assert _open_descriptors() == before


def test_a_cancelled_delivery_ends_the_command_and_closes_the_pipe() -> None:
    relay = CommandRelay([sys.executable, "-c", "import time; time.sleep(30)"], 30)

    async def scenario() -> None:
        task = asyncio.create_task(relay.deliver(notice()))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    before = _open_descriptors()
    asyncio.run(scenario())
    assert _open_descriptors() == before


def test_a_missing_command_cannot_start(tmp_path: Path) -> None:
    relay = CommandRelay([str(tmp_path / "absent")], 5)
    with pytest.raises(RelayError, match="could not start"):
        asyncio.run(relay.deliver(notice()))


# Item 2. The file sink.


def test_the_file_sink_appends_one_line_per_notice_owner_only(tmp_path: Path) -> None:
    protected = tmp_path / ".dromon" / "run"
    protected.mkdir(parents=True)
    path = protected / "notices.jsonl"
    relay = FileRelay(path, [protected])
    asyncio.run(relay.deliver(notice("m-1")))
    asyncio.run(relay.deliver(notice("m-2", MessageKind.ISSUE)))
    lines = path.read_text().splitlines()
    assert [json.loads(line)["message_id"] for line in lines] == ["m-1", "m-2"]
    assert json.loads(lines[1])["kind"] == "issue"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_file_sink_refuses_a_link_and_an_open_file(tmp_path: Path) -> None:
    protected = tmp_path / ".dromon" / "run"
    protected.mkdir(parents=True)
    target = protected / "elsewhere"
    target.write_text("")
    target.chmod(0o600)
    link = protected / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(RelayError, match="could not be opened"):
        FileRelay(link, [protected]).forward(notice())
    shared = protected / "shared.jsonl"
    shared.write_text("")
    shared.chmod(0o644)
    with pytest.raises(RelayError, match="open to group or others"):
        FileRelay(shared, [protected]).forward(notice())
    assert shared.read_text() == "" and target.read_text() == ""


def test_the_file_sink_does_not_block_opening_a_fifo(tmp_path: Path) -> None:
    protected = tmp_path / ".dromon" / "run"
    protected.mkdir(parents=True)
    fifo = protected / "notices.jsonl"
    os.mkfifo(fifo)
    started = time.monotonic()
    with pytest.raises(RelayError, match="could not be opened"):
        FileRelay(fifo, [protected]).forward(notice())
    assert time.monotonic() - started < 1.0


def test_the_file_sink_must_be_under_a_protected_directory(tmp_path: Path) -> None:
    workdir = tmp_path / "work"
    (workdir / "deep").mkdir(parents=True)
    protected = workdir / ".dromon"
    protected.mkdir()
    inside = workdir / "deep" / "notices.jsonl"
    assert "protected state or runtime" in str(sink_refusal(inside, [protected]))
    assert sink_refusal(protected / "notices.jsonl", [protected]) is None
    assert "protected state or runtime" in str(
        sink_refusal(tmp_path / "notices.jsonl", [protected])
    )
    assert "does not exist" in str(sink_refusal(tmp_path / "absent" / "n.jsonl", []))
    alias = tmp_path / "alias"
    alias.symlink_to(workdir)
    assert sink_refusal(alias / "notices.jsonl", []) is not None
    sink = protected / "notices.jsonl"
    relay = FileRelay(sink, [protected])
    relay.forward(notice("m-1"))
    relay.forward(notice("m-2"))
    assert len(sink.read_text().splitlines()) == 2


# The project file.


def test_the_relays_and_operators_tables_parse() -> None:
    config = parse(
        "version = 1\n"
        "[relays.mail]\ncommand = ['/usr/local/bin/notify', '--to', 'ana']\n"
        "timeout_seconds = 10\n"
        "[relays.tail]\nkind = 'file'\npath = '.dromon/run/notices.jsonl'\n"
        "[operators.ana]\nrelay = 'tail'\n[operators.ben]\nrelay = 'mail'\n"
    )
    assert config.relays == RelayConfig(
        {
            "mail": RelayDefinition(
                RelayKind.COMMAND,
                command=("/usr/local/bin/notify", "--to", "ana"),
                timeout_seconds=10.0,
            ),
            "tail": RelayDefinition(
                RelayKind.FILE, path=".dromon/run/notices.jsonl", timeout_seconds=30.0
            ),
        },
        {"ana": "tail", "ben": "mail"},
    )
    assert parse("version = 1\n").relays == RelayConfig()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[relays.a]\ncommand = []\n", "relays.a.command: must be a list of 1 to 64"),
        ("[relays.a]\ncommand = ['']\n", "non-empty text arguments"),
        ("[relays.a]\ncommand = 'notify'\n", "must be a list"),
        ("[relays.a]\ncommand = ['n']\ntimeout_seconds = 0\n", "(0, 3600]"),
        ("[relays.a]\ncommand = ['n']\ntimeout_seconds = 3601\n", "(0, 3600]"),
        ("[relays.a]\ncommand = ['n']\ntimeout_seconds = true\n", "(0, 3600]"),
        ("[relays.a]\ncommand = ['n']\npath = 'x'\n", "is for a file relay"),
        ("[relays.a]\nkind = 'file'\n", "relays.a.path: must be a path"),
        ("[relays.a]\nkind = 'file'\npath = 'x'\ncommand = ['n']\n", "command relay"),
        ("[relays.a]\nkind = 'mail'\n", "must be command or file"),
        ("[relays.a]\ncommand = ['n']\nshell = true\n", "relays.a.shell: is not"),
        ("[relays.A]\ncommand = ['n']\n", "is not an identifier"),
        ("[operators.ana]\nrelay = 'none'\n", "operators.ana.relay: must name a relay"),
        (
            "[relays.a]\ncommand = ['n']\n[operators.ana]\nrelay = 'a'\nvia = 'x'\n",
            "operators.ana.via: is not a known key",
        ),
        ("relays = 3\n", "relays: must be a table"),
    ],
)
def test_a_bad_relays_table_is_refused(text: str, message: str) -> None:
    with pytest.raises(
        ConfigFileError, match=message.replace("(", r"\(").replace(")", r"\)")
    ):
        parse(f"version = 1\n{text}")


# The daemon: start, reload and status.


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


def file_relay_project(path: str) -> str:
    return (
        "version = 1\n"
        f"[relays.tail]\nkind = 'file'\npath = '{path}'\n"
        "[operators.ana]\nrelay = 'tail'\n"
    )


def make_daemon(paths: ProjectPaths, log: EventLog, config: ProjectConfig) -> Daemon:
    scheduler = Scheduler(
        log,
        no_engine,
        hold_wakes=True,
        root=paths.root,
        protected=protected_dirs(paths),
    )
    return Daemon(
        paths,
        log,
        "2026-10-31T00:00:00Z",
        scheduler,
        project=config,
        command_line=Overrides(),
        project_file_present=True,
    )


def ask(daemon: Daemon, op: str, args: dict[str, object]) -> dict[str, object]:
    line = {"id": "r", "op": op, "v": 1, "args": args}
    response = daemon.respond((json.dumps(line) + "\n").encode())
    assert response.ok and response.result is not None, response.error_message
    return response.result


def refusal(daemon: Daemon, op: str, args: dict[str, object]) -> str:
    line = {"id": "r", "op": op, "v": 1, "args": args}
    response = daemon.respond((json.dumps(line) + "\n").encode())
    assert not response.ok
    return str(response.error_message)


def test_a_reload_moves_the_route_and_status_shows_the_relays(
    paths: ProjectPaths, tmp_path: Path
) -> None:
    outside = paths.state_dir / "relay"
    outside.mkdir()
    first, second = outside / "first.jsonl", outside / "second.jsonl"
    file = config_path(paths.state_dir)
    file.write_text(file_relay_project(str(first)))
    with EventLog(paths.event_log) as log:
        daemon = make_daemon(paths, log, parse(file.read_text(), paths.root))
        relayer = daemon.operator_relayer()
        daemon.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        daemon.scheduler.ensure_operator("ana")
        one = daemon.scheduler.send(LineAddress("alpha"), ANA, "one")
        asyncio.run(relayer.step())
        file.write_text(file_relay_project(str(second)))
        ask(daemon, "reload_project", {})
        two = daemon.scheduler.send(LineAddress("alpha"), ANA, "two")
        asyncio.run(relayer.step())
        status = cast(dict[str, object], ask(daemon, "status", {})["relays"])
    assert [json.loads(x)["message_id"] for x in first.read_text().splitlines()] == [
        one
    ]
    assert [json.loads(x)["message_id"] for x in second.read_text().splitlines()] == [
        two
    ]
    assert status["routed"] == ["ana"] and status["delivered"] == 2
    assert _relay_rows(status) == [
        "relays for @ana, 2 relayed since the start, 0 waiting unrouted, 0 failing"
    ]


def test_a_sink_outside_protected_state_is_refused_at_start_and_reload(
    paths: ProjectPaths,
) -> None:
    file = config_path(paths.state_dir)
    file.write_text(file_relay_project("notices.jsonl"))
    refused = parse(file.read_text(), paths.root)
    with EventLog(paths.event_log) as log:
        daemon = make_daemon(paths, log, ProjectConfig())
        daemon.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        with pytest.raises(ProjectMismatch, match="relays.tail.path"):
            daemon.relay_routes(refused)
        daemon.operator_relayer()
        assert "protected state or runtime" in refusal(daemon, "reload_project", {})
        assert daemon.project == ProjectConfig(), "a refused reload changes nothing"
        file.write_text(file_relay_project(".dromon/run/notices.jsonl"))
        (paths.state_dir / "run").mkdir(mode=0o700, exist_ok=True)
        ask(daemon, "reload_project", {})


def test_a_file_sink_outside_protected_state_is_refused_even_in_a_line_workdir(
    paths: ProjectPaths, tmp_path: Path
) -> None:
    """A line the reloaded file declares counts, though the log does not hold it
    yet, so a file cannot bring a line and a notice file into one directory."""
    sink = tmp_path / "share"
    sink.mkdir()
    config = parse(
        file_relay_project("share/notices.jsonl")
        + f"[lines.beta]\nmodel = '{HAIKU}'\nworkdir = 'share'\n",
        paths.root,
    )
    with EventLog(paths.event_log) as log:
        daemon = make_daemon(paths, log, ProjectConfig())
        with pytest.raises(ProjectMismatch, match="protected state or runtime"):
            daemon.relay_routes(config)


# Item 3. Issues from lines.


def test_a_line_raises_an_issue_and_the_operator_lists_and_closes_it(
    scheduler: Scheduler,
) -> None:
    text, error = call(
        scheduler.send_tool("alpha"),
        {"to": "@ana", "body": "list_mail hides notes", "issue": True},
    )
    assert not error
    issue_id = text.removeprefix("raised as issue ")
    other = scheduler.send(LineAddress("alpha"), ANA, "a plain note")
    assert scheduler.state.messages[issue_id].kind is MessageKind.ISSUE
    scheduler.collect(ANA)
    found, total = listing(scheduler.state, ANA, issues_only=True)
    assert (total, [m for m, _ in found]) == (1, [issue_id])
    scheduler.acknowledge(ANA, issue_id)
    assert listing(scheduler.state, ANA, issues_only=True)[1] == 0
    second = scheduler.send(LineAddress("alpha"), ANA, "x", issue=True)
    scheduler.collect(ANA)
    scheduler.send(ANA, LineAddress("alpha"), "fixed", reply_to=second)
    assert listing(scheduler.state, ANA, issues_only=True)[1] == 0, (
        "a reply closes an issue too"
    )
    assert scheduler.state.messages[other].kind is MessageKind.NOTE


def test_a_line_cannot_exceed_its_open_issue_limit(scheduler: Scheduler) -> None:
    for index in range(5):
        if index:
            for _ in range(20):
                scheduler.send(LineAddress("alpha"), LineAddress("alpha"), "spacing")
        scheduler.send(LineAddress("alpha"), ANA, f"issue {index}", issue=True)
    for _ in range(20):
        scheduler.send(LineAddress("alpha"), LineAddress("alpha"), "spacing")
    with pytest.raises(EventRejected, match="wait for an operator reply"):
        scheduler.send(LineAddress("alpha"), ANA, "one too many", issue=True)


def test_a_line_cannot_exceed_its_issue_rate(scheduler: Scheduler) -> None:
    for index in range(3):
        issue_id = scheduler.send(
            LineAddress("alpha"), ANA, f"issue {index}", issue=True
        )
        scheduler.send(ANA, LineAddress("alpha"), "resolved", reply_to=issue_id)
    with pytest.raises(EventRejected, match="wait for a reply"):
        scheduler.send(LineAddress("alpha"), ANA, "rate limited", issue=True)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"to": "alpha", "body": "x", "issue": True}, "only a line raises an issue"),
        ({"to": "@ana", "body": "x", "issue": "yes"}, "issue must be true or false"),
    ],
)
def test_an_issue_goes_only_to_an_operator(
    scheduler: Scheduler, args: dict[str, object], message: str
) -> None:
    text, error = call(scheduler.send_tool("alpha"), args)
    assert error and message in text


def test_an_issue_is_never_a_reply_and_only_a_line_raises_one(
    scheduler: Scheduler,
) -> None:
    note = scheduler.send(ANA, LineAddress("alpha"), "hello")
    with pytest.raises(EventRejected, match="opens a thread"):
        scheduler.state.check(
            MessageSent(
                "m-x", LineAddress("alpha"), ANA, MessageKind.ISSUE, "x", reply_to=note
            )
        )
    with pytest.raises(EventRejected, match="only a line raises an issue"):
        scheduler.send(ANA, LineAddress("alpha"), "x", issue=True)


def test_an_agent_is_not_offered_the_issue_flag() -> None:
    line_properties = cast(dict[str, object], SEND_TOOL_SCHEMA["properties"])
    agent_properties = cast(dict[str, object], AGENT_SEND_TOOL_SCHEMA["properties"])
    assert "issue" in line_properties and "issue" not in agent_properties
    assert set(agent_properties) == set(line_properties) - {"issue"} | {"correlation"}


def test_an_issue_survives_the_codec_and_reaches_the_relay(
    scheduler: Scheduler,
) -> None:
    event = MessageSent("m-1", LineAddress("alpha"), ANA, MessageKind.ISSUE, "x")
    assert decode(encode(event)) == event
    ana = Recorder()
    relayer = relayer_for(scheduler, {"ana": ana})
    scheduler.send(LineAddress("alpha"), ANA, "a tool misleads", issue=True)
    asyncio.run(relayer.step())
    assert [n.kind for n in ana.notices] == [MessageKind.ISSUE]


def test_the_mail_query_lists_issues(paths: ProjectPaths) -> None:
    with EventLog(paths.event_log) as log:
        daemon = make_daemon(paths, log, ProjectConfig())
        daemon.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        daemon.scheduler.ensure_operator("ana")
        issue = daemon.scheduler.send(LineAddress("alpha"), ANA, "x", issue=True)
        daemon.scheduler.send(LineAddress("alpha"), ANA, "y")
        result = ask(daemon, "mail", {"operator": "ana", "issues_only": True})
        assert result["total"] == 1
        entries = cast(list[dict[str, object]], result["messages"])
        assert [(e["message_id"], e["kind"]) for e in entries] == [(issue, "issue")]
        assert "booleans" in refusal(
            daemon, "mail", {"operator": "ana", "issues_only": 1}
        )


def test_status_names_each_failing_notice_and_is_quiet_without_relays() -> None:
    assert _relay_rows({"routed": [], "unrouted": 0}) == []
    rows = _relay_rows(
        {
            "routed": ["ana"],
            "delivered": 0,
            "unrouted": 2,
            "failing": 1,
            "failures": [
                {
                    "message_id": "m-1",
                    "operator": "ana",
                    "attempts": 3,
                    "reason": "the relay command timed out after 30.0s",
                    "retry_in_seconds": 20.0,
                }
            ],
        }
    )
    assert rows == [
        "relays for @ana, 0 relayed since the start, 2 waiting unrouted, 1 failing",
        "  m-1 for @ana, attempt 3, again in 20.0s:"
        " the relay command timed out after 30.0s",
    ]
