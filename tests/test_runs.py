"""Supervised runs: the project's ``[runs]`` table, the ``run_started``,
``run_ended`` and ``run_cleared`` events, the supervisor, reconciliation after a
restart and a handoff, a supervisor that died while the daemon was down, a reused
process identifier, a lost run whose command survives, and the review of 2026-10-31,
whose test gaps T1 to T8 are named where each is closed.
``docs/architecture/RUNS.md`` is normative."""

import asyncio
import contextlib
import hashlib
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from collections.abc import Callable, Generator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import runs
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.model import (
    HARNESS,
    MAX_RUN_NOTE_BYTES,
    EventRejected,
    InvalidField,
    LineAddress,
    LineCreated,
    OperatorAddress,
    OperatorRegistered,
    OwnershipCause,
    OwnershipChanged,
    RoleDeclared,
    RoleNoted,
    RoleNoteKind,
    RunCleared,
    RunClearance,
    Run,
    RunEnded,
    RunStarted,
    RunStatus,
    SeatBound,
    State,
)
from dromon.processes import TreeRow
from dromon.projectconfig import ConfigFileError, parse
from dromon.scheduler import (
    RUN_STATUS_TOOL_NAME,
    START_RUN_TOOL_NAME,
    STOP_RUN_TOOL_NAME,
    LineEngine,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
AT = "2026-10-31T09:00:00Z"
LATER = "2026-10-31T09:05:00Z"
ANA = OperatorAddress("ana")
WAIT_SECONDS = 20


# The project file.


def test_the_runs_table_declares_commands_as_arrays() -> None:
    config = parse(
        "version = 1\n[runs.commands]\n"
        "gate = ['./scripts/check.sh', '-m', 'not live']\nbuild = ['make']\n"
    )
    assert config.run_commands == {
        "build": ("make",),
        "gate": ("./scripts/check.sh", "-m", "not live"),
    }
    assert parse("version = 1\n").run_commands == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("commands = 'make'", "runs.commands: must be a table"),
        ("[runs.commands]\nBuild = ['make']", "is not an identifier"),
        ("[runs.commands]\nbuild = 'make'", "must be a non-empty array"),
        ("[runs.commands]\nbuild = []", "must be a non-empty array"),
        ("[runs.commands]\nbuild = ['make', 3]", "which is not text"),
        ("[runs.commands]\nbuild = ['']", "names no program"),
        ('[runs.commands]\nbuild = ["a\\u0000b"]', "without a null"),
        (f"[runs.commands]\nbuild = {['x'] * 65}", "more than 64 arguments"),
        ("shell = true", "runs.shell: is not a known key"),
    ],
)
def test_a_bad_runs_table_is_refused(text: str, message: str) -> None:
    body = text if text.startswith("[") else f"[runs]\n{text}"
    with pytest.raises(ConfigFileError, match=message):
        parse(f"version = 1\n{body}\n")


# The events.


def started(
    run_id: str = "r-1",
    line_id: str = "alpha",
    role_id: str | None = None,
    previous: str | None = None,
    pid: int = 4321,
    process_started: str = AT,
) -> RunStarted:
    return RunStarted(
        run_id,
        line_id,
        role_id,
        previous,
        "gate",
        ("./scripts/check.sh",),
        "/work",
        f"tmp/runs/{run_id}.out",
        pid,
        process_started,
        AT,
    )


def ended(run_id: str = "r-1", status: RunStatus = RunStatus.EXITED) -> RunEnded:
    code = None if status is RunStatus.LOST else 0
    return RunEnded(run_id, status, code, None, None, "", LATER)


def role_state() -> State:
    """Role ``core`` with seats ``alpha``, its owner, and ``beta``, and a line
    ``gamma`` that is no seat."""
    state = State()
    for event in (
        LineCreated("alpha", HAIKU, "subscription"),
        LineCreated("beta", HAIKU, "subscription"),
        LineCreated("gamma", HAIKU, "subscription"),
        OperatorRegistered("ana"),
        RoleDeclared("core", "tmp/worktrees/core"),
        SeatBound("alpha", "core", True),
        SeatBound("beta", "core", True),
        OwnershipChanged("core", "alpha", None, OwnershipCause.DECLARED, ANA, None, AT),
    ):
        state.apply(event)
    return state


def test_a_run_is_its_roles_and_only_the_owner_starts_one() -> None:
    state = role_state()
    with pytest.raises(EventRejected, match="belongs to role core"):
        state.check(started())
    with pytest.raises(EventRejected, match="beta does not own role core"):
        state.check(started(line_id="beta", role_id="core"))
    with pytest.raises(EventRejected, match="gamma's run belongs to no role"):
        state.check(started(line_id="gamma", role_id="core"))
    state.apply(started(role_id="core"))
    state.apply(started("r-g", line_id="gamma"))
    assert state.latest_runs == {("role", "core"): "r-1", ("line", "gamma"): "r-g"}


def test_a_scope_runs_one_at_a_time_and_each_names_its_predecessor() -> None:
    state = role_state()
    state.apply(started(role_id="core"))
    with pytest.raises(EventRejected, match="run r-1 has not ended"):
        state.check(started("r-2", role_id="core", previous="r-1"))
    state.apply(ended())
    with pytest.raises(EventRejected, match="the latest run is r-1, not none"):
        state.check(started("r-2", role_id="core"))
    with pytest.raises(EventRejected, match="run r-1 exists"):
        state.check(started(role_id="core", previous="r-1"))
    with pytest.raises(EventRejected, match="run r-1 has ended"):
        state.check(ended())
    with pytest.raises(EventRejected, match="no run r-9"):
        state.check(ended("r-9"))
    state.apply(started("r-2", role_id="core", previous="r-1"))
    assert state.runs["r-1"].ended == ended()
    assert state.runs["r-2"].ended is None


def test_a_change_of_owner_hands_the_open_run_to_the_successor() -> None:
    """The run stays the role's, so the successor cannot start another while it is
    open, and names it as previous once it has ended."""
    state = role_state()
    state.apply(started(role_id="core"))
    state.apply(
        RoleNoted("core", "e1", "alpha", RoleNoteKind.HANDOFF, "go", "", "", AT)
    )
    state.apply(
        OwnershipChanged(
            "core",
            "beta",
            "alpha",
            OwnershipCause.HANDOFF,
            LineAddress("alpha"),
            "e1",
            AT,
        )
    )
    with pytest.raises(EventRejected, match="run r-1 has not ended"):
        state.check(started("r-2", "beta", "core"))
    with pytest.raises(EventRejected, match="alpha does not own role core"):
        state.check(started("r-2", "alpha", "core", "r-1"))
    state.apply(ended())
    state.apply(started("r-2", "beta", "core", "r-1"))


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: started(pid=1), "pid must be a whole number above 1"),
        (lambda: started(process_started="today"), "process_started"),
        (
            lambda: RunEnded("r-1", RunStatus.LOST, 0, None, None, "", AT),
            "a lost run has no exit code",
        ),
        (
            lambda: RunEnded("r-1", RunStatus.EXITED, 0, "0" * 64, None, "", AT),
            "output_digest is given with output_bytes",
        ),
        (
            lambda: RunEnded("r-1", RunStatus.EXITED, 0, "xyz", 3, "", AT),
            "SHA-256",
        ),
    ],
)
def test_a_malformed_run_event_is_refused(
    make: Callable[[], object], message: str
) -> None:
    with pytest.raises(InvalidField, match=message):
        make()


def test_a_run_needs_an_absolute_workdir_and_a_program() -> None:
    base = started()
    for argv, workdir, message in (
        ((), "/work", "argv must hold"),
        (("",), "/work", "argv names no program"),
        (("make",), "work", "workdir is not absolute"),
    ):
        with pytest.raises(InvalidField, match=message):
            RunStarted(
                base.run_id,
                base.line_id,
                None,
                None,
                "gate",
                argv,
                workdir,
                base.output_file,
                base.pid,
                AT,
                AT,
            )


# Reconciliation, with the process table stood in for.


def write_output(workdir: Path, run: RunStarted, text: bytes) -> str:
    path = workdir / run.output_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text)
    return hashlib.sha256(text).hexdigest()


def run_in(workdir: Path, run_id: str = "r-1", pid: int = 4321) -> RunStarted:
    found = started(run_id, pid=pid)
    return RunStarted(
        found.run_id,
        found.line_id,
        None,
        None,
        found.command,
        found.argv,
        str(workdir),
        found.output_file,
        found.pid,
        found.process_started,
        found.started_at,
    )


def test_a_running_supervisor_is_left_running(tmp_path: Path) -> None:
    run = run_in(tmp_path)
    assert runs.reconcile([run], tmp_path, lambda pid: AT) == []


def test_a_receipt_ends_the_run_as_it_says_with_the_outputs_digest(
    tmp_path: Path,
) -> None:
    """A supervisor that finished while the daemon was down left its receipt."""
    run = run_in(tmp_path)
    digest = write_output(tmp_path, run, b"passed\n")
    receipt = runs.Receipt(RunStatus.EXITED, 3, "")
    runs.write_receipt(runs.receipt_path(tmp_path, run.run_id), receipt)
    assert runs.reconcile([run], tmp_path, lambda pid: None) == [
        runs.Finding("r-1", RunStatus.EXITED, 3, "", digest, 7)
    ]


def test_a_supervisor_that_died_while_the_daemon_was_down_is_lost(
    tmp_path: Path,
) -> None:
    run = run_in(tmp_path)
    (finding,) = runs.reconcile([run], tmp_path, lambda pid: None)
    assert (finding.status, finding.exit_code) == (RunStatus.LOST, None)
    assert "ended without a receipt" in finding.note
    assert (finding.output_digest, finding.output_bytes) == (None, None)


def test_a_reused_process_identifier_is_not_taken_for_the_supervisor(
    tmp_path: Path,
) -> None:
    run = run_in(tmp_path)
    (finding,) = runs.reconcile([run], tmp_path, lambda pid: LATER)
    assert finding.status is RunStatus.LOST
    assert (
        f"process identifier 4321 now belongs to another process, started {LATER}"
        in (finding.note)
    )
    with pytest.raises(runs.RunRefused, match="no longer running"):
        runs.signal_run(run, lambda pid: LATER, lambda pid, number: None)


def test_a_receipt_written_as_the_table_is_read_is_still_read(tmp_path: Path) -> None:
    """The supervisor writes its receipt and then exits, so a supervisor found gone
    has its receipt read again before its run is called lost."""
    run = run_in(tmp_path)

    def gone_after_writing(pid: int) -> str | None:
        receipt = runs.Receipt(RunStatus.EXITED, 0, "")
        runs.write_receipt(runs.receipt_path(tmp_path, run.run_id), receipt)
        return None

    (finding,) = runs.reconcile([run], tmp_path, gone_after_writing)
    assert (finding.status, finding.exit_code) == (RunStatus.EXITED, 0)


def test_an_unreadable_table_ends_nothing(tmp_path: Path) -> None:
    def unreadable(pid: int) -> str | None:
        raise runs.TableUnreadable("ps cannot be run")

    with pytest.raises(runs.TableUnreadable):
        runs.reconcile([run_in(tmp_path)], tmp_path, unreadable)


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        b'{"status": "lost", "exit_code": null, "note": ""}',
        b'{"status": "exited", "exit_code": "0", "note": ""}',
        b'{"status": "exited", "exit_code": 0}',
        b'{"status": "exited", "exit_code": 0, "note": "' + b"x" * 5000 + b'"}',
    ],
)
def test_a_malformed_receipt_is_no_receipt(raw: bytes) -> None:
    assert runs.parse_receipt(raw) is None


def test_the_start_time_is_read_in_coordinated_universal_time() -> None:
    assert runs.parse_lstart("Sat Oct 31 09:03:10 2026") == "2026-10-31T09:03:10Z"
    assert runs.parse_lstart("Sun Nov  1 00:00:00 2026") == "2026-11-01T00:00:00Z"
    assert runs.parse_lstart("yesterday") is None


# The output file, which a line can reach.


@pytest.mark.parametrize("step", ["tmp", "tmp/runs"])
def test_a_link_in_the_output_path_is_refused(tmp_path: Path, step: str) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    workdir = tmp_path / "work"
    (workdir / step).parent.mkdir(parents=True, exist_ok=True)
    (workdir / step).symlink_to(elsewhere)
    with pytest.raises(runs.RunRefused, match="A symbolic link there is refused"):
        runs.open_output(workdir, "r-1")
    assert list(elsewhere.iterdir()) == []


def test_an_output_file_that_exists_is_not_reused(tmp_path: Path) -> None:
    descriptor, relative = runs.open_output(tmp_path, "r-1")
    os.close(descriptor)
    assert relative == "tmp/runs/r-1.out"
    with pytest.raises(runs.RunRefused):
        runs.open_output(tmp_path, "r-1")


def test_an_output_replaced_by_a_link_has_no_digest(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_bytes(b"not the run's")
    (tmp_path / "tmp" / "runs").mkdir(parents=True)
    (tmp_path / "tmp" / "runs" / "r-1.out").symlink_to(secret)
    assert runs.output_digest(tmp_path, "tmp/runs/r-1.out") == (None, None)


# The supervisor, with real processes. ``ps`` cannot run inside an agent sandbox, so
# these stand in for the table's start time with whether the process answers a
# signal, which is enough for a supervisor the test has not reaped. One test reads the
# real table where ``ps`` runs.


def ps_runs() -> bool:
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
    not ps_runs(), reason="ps cannot run here, as inside an agent sandbox"
)


def stand_in_start(pid: int) -> str | None:
    """``AT`` while ``pid`` answers a signal, as the start time the table gives."""
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return None
    return AT


def python_command(source: str) -> tuple[str, ...]:
    return (sys.executable, "-c", source)


def launched(
    tmp_path: Path, argv: Sequence[str], run_id: str = "r-1"
) -> tuple[runs.Launched, RunStarted]:
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    found = runs.launch(
        tmp_path, run_id, workdir, argv, {"PATH": "/usr/bin:/bin"}, stand_in_start
    )
    run = RunStarted(
        run_id,
        "alpha",
        None,
        None,
        "gate",
        tuple(argv),
        str(workdir),
        found.output_file,
        found.child.pid,
        found.process_started,
        AT,
    )
    return found, run


def test_a_supervisor_runs_its_command_once_released_and_writes_a_receipt(
    tmp_path: Path,
) -> None:
    found, run = launched(
        tmp_path, python_command("import sys; print('built'); sys.exit(3)")
    )
    assert found.process_started == AT
    assert runs.release(found)
    assert found.child.wait(timeout=WAIT_SECONDS) == 0
    (finding,) = runs.reconcile(
        [run], tmp_path, stand_in_start, {run.run_id: found.child}
    )
    output = (tmp_path / "work" / run.output_file).read_bytes()
    assert output == b"built\n"
    assert finding == runs.Finding(
        run.run_id,
        RunStatus.EXITED,
        3,
        "",
        hashlib.sha256(output).hexdigest(),
        len(output),
    )


def test_a_supervisor_never_released_starts_nothing(tmp_path: Path) -> None:
    """A daemon that dies between the start and the record leaves no run, since
    the supervisor's input closes without the word to go."""
    marker = tmp_path / "work" / "ran"
    found, run = launched(
        tmp_path, python_command(f"open({str(marker)!r}, 'w').close()")
    )
    runs.abandon(found.child)
    assert found.child.returncode == runs.NOT_RECORDED_EXIT
    assert not marker.exists()
    assert runs.read_receipt(tmp_path, run.run_id) is None


def wait_for_file(path: Path) -> None:
    for _ in range(WAIT_SECONDS * 20):
        if path.exists() and path.read_text():
            return
        time.sleep(0.05)
    raise AssertionError(f"{path} was never written")


def test_stop_ends_the_commands_group_and_the_receipt_says_stopped(
    tmp_path: Path,
) -> None:
    pid_file = tmp_path / "work" / "command.pid"
    found, run = launched(
        tmp_path,
        python_command(
            f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid()));"
            " time.sleep(60)"
        ),
    )
    runs.release(found)
    wait_for_file(pid_file)
    runs.signal_run(run, stand_in_start)
    assert found.child.wait(timeout=WAIT_SECONDS) == 0
    receipt = runs.read_receipt(tmp_path, run.run_id)
    assert receipt is not None
    assert (receipt.status, receipt.exit_code) == (RunStatus.STOPPED, -signal.SIGTERM)
    with pytest.raises(ProcessLookupError):
        os.killpg(int(pid_file.read_text()), 0)


def test_a_stop_that_comes_before_the_command_starts_it_never_starts(
    tmp_path: Path,
) -> None:
    """``launch`` returns only once the supervisor's handlers are installed, so a
    stop that arrives at once is still answered with a receipt."""
    marker = tmp_path / "work" / "ran"
    found, run = launched(
        tmp_path, python_command(f"open({str(marker)!r}, 'w').close()")
    )
    runs.signal_run(run, stand_in_start)
    runs.release(found)
    assert found.child.wait(timeout=WAIT_SECONDS) == 0
    assert runs.read_receipt(tmp_path, run.run_id) == runs.Receipt(
        RunStatus.STOPPED, None, "stopped before its command started"
    )
    assert not marker.exists()


@pytest.mark.parametrize("real", [False, pytest.param(True, marks=NEEDS_PS)])
def test_a_killed_supervisor_is_found_lost(tmp_path: Path, real: bool) -> None:
    """The supervisor is killed as a crash or ``SIGKILL`` would, so it writes no
    receipt, and the process table no longer shows it. Where ``ps`` runs, the real
    table is read too, for both the start and the reconciliation."""
    pid_file = tmp_path / "work" / "command.pid"
    source = (
        f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid()));"
        " time.sleep(60)"
    )
    found, run = launched(tmp_path, python_command(source))
    start_of = runs.process_start if real else stand_in_start
    if real:
        assert runs.process_start(found.child.pid) is not None
        run = replace(run, process_started=str(runs.process_start(found.child.pid)))
        assert runs.reconcile([run], tmp_path, start_of) == []
    runs.release(found)
    try:
        wait_for_file(pid_file)
        found.child.kill()
        found.child.wait(timeout=WAIT_SECONDS)
        (finding,) = runs.reconcile([run], tmp_path, start_of)
        assert finding.status is RunStatus.LOST
        assert "ended without a receipt" in finding.note
    finally:
        # The command, in a group of its own, outlives its supervisor, which the
        # note says, so the test ends that group itself.
        if pid_file.exists() and pid_file.read_text():
            with contextlib.suppress(OSError):
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)


def test_a_supervisor_whose_command_cannot_start_says_so(tmp_path: Path) -> None:
    found, run = launched(tmp_path, ("/nonexistent/program",))
    runs.release(found)
    found.child.wait(timeout=WAIT_SECONDS)
    receipt = runs.read_receipt(tmp_path, run.run_id)
    assert receipt is not None and receipt.exit_code == 127
    assert "cannot start" in receipt.note


# The scheduler, across a restart and a handoff.


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


def stand_in_occupied(session: int) -> bool:
    """Whether the supervisor's own group still answers a signal, as the table's
    search of its group and session would find it."""
    try:
        os.killpg(session, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def scheduler_on(
    log: EventLog,
    tmp_path: Path,
    occupied: Callable[[int], bool] = stand_in_occupied,
) -> Scheduler:
    found = Scheduler(
        log,
        no_engine,
        hold_wakes=True,
        wall_clock=Clock(),
        root=tmp_path,
        report_dir=tmp_path / "reports",
        run_start_of=stand_in_start,
        run_occupied=occupied,
    )
    found.set_runs({"gate": python_command("print('gate passed')")})
    return found


@pytest.fixture
def project(tmp_path: Path) -> Generator[Path]:
    (tmp_path / "reports").mkdir()
    yield tmp_path


def wait_for_supervisor(scheduler: Scheduler, run_id: str) -> None:
    children = scheduler._run_children  # pyright: ignore[reportPrivateUsage]
    children[run_id].wait(timeout=WAIT_SECONDS)


def harness_notes(log: EventLog, line_id: str) -> list[str]:
    return [
        message.body
        for message in log.state.messages.values()
        if message.sender == HARNESS and message.recipient == LineAddress(line_id)
    ]


def test_a_restart_reconciles_a_run_that_ended_while_the_daemon_was_down(
    project: Path,
) -> None:
    path = project / "events.sqlite"
    with EventLog(path) as log:
        before = scheduler_on(log, project)
        before.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        run = asyncio.run(before.start_run("alpha", "gate", "none"))
        assert log.state.runs[run.run_id].ended is None
        wait_for_supervisor(before, run.run_id)
    with EventLog(path) as log:
        after = scheduler_on(log, project)
        assert log.state.runs[run.run_id].started == run
        asyncio.run(after.reconcile_runs())
        end = log.state.runs[run.run_id].ended
        assert end is not None
        assert (end.status, end.exit_code, end.output_bytes) == (
            RunStatus.EXITED,
            0,
            len(b"gate passed\n"),
        )
        assert runs.read_receipt(project / "reports", run.run_id) is None
        (note,) = harness_notes(log, "alpha")
        assert f"run {run.run_id} of gate" in note and "exited with exit code 0" in note


def test_a_run_still_running_across_a_restart_stays_open_and_can_be_stopped(
    project: Path,
) -> None:
    """The restarted daemon holds no handle on the supervisor, so it finds it by
    its identifier and start time alone, both to leave it open and to stop it."""
    path = project / "events.sqlite"
    with EventLog(path) as log:
        before = scheduler_on(log, project)
        before.set_runs({"wait": python_command("import time; time.sleep(60)")})
        before.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        run = asyncio.run(before.start_run("alpha", "wait", "none"))
        child = before._run_children[run.run_id]  # pyright: ignore[reportPrivateUsage]
    try:
        with EventLog(path) as log:
            after = scheduler_on(log, project)
            asyncio.run(after.reconcile_runs())
            assert log.state.runs[run.run_id].ended is None
            assert "still running as process" in after.run_status("alpha")
            asyncio.run(after.stop_run("alpha", run.run_id))
            child.wait(timeout=WAIT_SECONDS)
            asyncio.run(after.reconcile_runs())
            end = log.state.runs[run.run_id].ended
            assert end is not None and end.status is RunStatus.STOPPED
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_a_successor_seat_must_read_the_run_before_starting_another(
    project: Path,
) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        for line_id in ("alpha", "beta"):
            scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
        for event in (
            OperatorRegistered("ana"),
            RoleDeclared("core", "."),
            SeatBound("alpha", "core", True),
            SeatBound("beta", "core", True),
            OwnershipChanged(
                "core", "alpha", None, OwnershipCause.DECLARED, ANA, None, AT
            ),
        ):
            log.append(event)
        with pytest.raises(runs.RunRefused, match="only role core's owner"):
            asyncio.run(scheduler.start_run("beta", "gate", "none"))
        first = asyncio.run(scheduler.start_run("alpha", "gate", "none"))
        assert first.role_id == "core"
        log.append(
            OwnershipChanged(
                "core", "beta", "alpha", OwnershipCause.OPERATOR, ANA, None, AT
            )
        )
        with pytest.raises(runs.RunRefused, match=f"run {first.run_id} of gate"):
            asyncio.run(scheduler.start_run("beta", "gate", "none"))
        assert f"previous is {first.run_id}" in scheduler.run_status("beta")
        wait_for_supervisor(scheduler, first.run_id)
        asyncio.run(scheduler.reconcile_runs())
        # The end goes to the role's present owner, not the line that started it.
        assert harness_notes(log, "alpha") == []
        (note,) = harness_notes(log, "beta")
        assert first.run_id in note
        with pytest.raises(runs.RunRefused, match=f"The latest is run {first.run_id}"):
            asyncio.run(scheduler.start_run("beta", "gate", "none"))
        second = asyncio.run(scheduler.start_run("beta", "gate", first.run_id))
        assert second.previous == first.run_id
        wait_for_supervisor(scheduler, second.run_id)
        asyncio.run(scheduler.reconcile_runs())


def test_an_undeclared_command_and_a_reader_are_refused(project: Path) -> None:
    from dromon.lineconfig import LineConfig, WorkdirAccess

    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        with pytest.raises(runs.RunRefused, match="no run command 'deploy'.*gate"):
            asyncio.run(scheduler.start_run("alpha", "deploy", "none"))
        scheduler.configure_line("alpha", LineConfig(workdir_access=WorkdirAccess.READ))
        with pytest.raises(runs.RunRefused, match="which you may only read"):
            asyncio.run(scheduler.start_run("alpha", "gate", "none"))
        assert log.state.runs == {}


def test_the_run_tools_are_offered_only_when_a_command_is_declared(
    project: Path,
) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        names = {tool.name for tool in scheduler.own_tools("alpha")}
        assert {START_RUN_TOOL_NAME, RUN_STATUS_TOOL_NAME, STOP_RUN_TOOL_NAME} <= names
        scheduler.set_runs({})
        names = {tool.name for tool in scheduler.own_tools("alpha")}
        assert START_RUN_TOOL_NAME not in names


def test_the_tools_start_show_and_stop_a_run(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        scheduler.set_runs({"wait": python_command("import time; time.sleep(60)")})
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        tools = {tool.name: tool for tool in scheduler.own_tools("alpha")}

        async def call(name: str, args: dict[str, object]) -> tuple[str, bool]:
            result = await tools[name].handler(args)
            return str(result["content"][0]["text"]), result.get("is_error") is True

        async def scenario() -> None:
            text, error = await call(RUN_STATUS_TOOL_NAME, {})
            assert not error and "no runs yet, so previous is none" in text
            text, error = await call(
                START_RUN_TOOL_NAME, {"command": "wait", "previous": "none"}
            )
            assert not error and text.startswith("started run r-")
            (run_id,) = log.state.runs
            text, error = await call(
                START_RUN_TOOL_NAME, {"command": "wait", "previous": run_id}
            )
            assert error and "has not ended" in text
            text, error = await call(STOP_RUN_TOOL_NAME, {"run_id": "r-other"})
            assert error and "not your scope's open run" in text
            text, error = await call(STOP_RUN_TOOL_NAME, {"run_id": run_id})
            assert not error, text
            wait_for_supervisor(scheduler, run_id)
            await scheduler.reconcile_runs()
            end = log.state.runs[run_id].ended
            assert end is not None and end.status is RunStatus.STOPPED
            text, error = await call(RUN_STATUS_TOOL_NAME, {"verbose": True})
            assert error and "unexpected argument verbose" in text

        asyncio.run(scenario())


# The review of 2026-10-31. A lost run's command may survive, so it blocks its scope
# until no process is left in its supervisor's group or session, or an operator
# clears it. T2.


def lost_state() -> State:
    """Role ``core`` with run ``r-1`` lost and not cleared."""
    state = role_state()
    state.apply(started(role_id="core"))
    state.apply(ended(status=RunStatus.LOST))
    return state


def test_a_lost_run_blocks_its_scope_until_it_is_cleared() -> None:
    state = lost_state()
    assert state.runs["r-1"].blocks
    with pytest.raises(EventRejected, match="run r-1 is lost and not cleared"):
        state.check(started("r-2", role_id="core", previous="r-1"))
    state.apply(RunCleared("r-1", RunClearance.EMPTY, None, LATER))
    assert not state.runs["r-1"].blocks
    state.apply(started("r-2", role_id="core", previous="r-1"))


def test_only_a_lost_run_is_cleared_and_only_once() -> None:
    state = role_state()
    state.apply(started(role_id="core"))
    with pytest.raises(EventRejected, match="run r-1 is not lost"):
        state.check(RunCleared("r-1", RunClearance.EMPTY, None, LATER))
    state.apply(ended())
    assert not state.runs["r-1"].blocks
    with pytest.raises(EventRejected, match="run r-1 is not lost"):
        state.check(RunCleared("r-1", RunClearance.EMPTY, None, LATER))
    with pytest.raises(EventRejected, match="no run r-9"):
        state.check(RunCleared("r-9", RunClearance.EMPTY, None, LATER))
    state = lost_state()
    with pytest.raises(EventRejected, match="no operator ben"):
        state.check(RunCleared("r-1", RunClearance.OPERATOR, "ben", LATER))
    state.apply(RunCleared("r-1", RunClearance.OPERATOR, "ana", LATER))
    with pytest.raises(EventRejected, match="cleared already"):
        state.check(RunCleared("r-1", RunClearance.EMPTY, None, LATER))


@pytest.mark.parametrize(
    ("cause", "operator_id"),
    [(RunClearance.EMPTY, "ana"), (RunClearance.OPERATOR, None)],
)
def test_a_clearance_names_an_operator_exactly_when_one_cleared(
    cause: RunClearance, operator_id: str | None
) -> None:
    with pytest.raises(InvalidField, match="exactly when an operator cleared"):
        RunCleared("r-1", cause, operator_id, LATER)


def test_a_lost_run_settles_only_once_its_session_is_empty() -> None:
    lost = started(pid=4321)
    assert runs.settle([lost], lambda session: True) == []
    assert runs.settle([lost], lambda session: False) == ["r-1"]
    with pytest.raises(runs.TableUnreadable):
        runs.settle([lost], unreadable_occupied)


def unreadable_occupied(session: int) -> bool:
    raise runs.TableUnreadable("ps cannot be run")


def test_the_session_search_finds_a_group_or_a_session_member() -> None:
    def groups() -> list[tuple[int, int]]:
        return [(10, 10), (11, 11), (12, 11)]

    sessions = {10: 10, 11: 11, 12: 500}
    assert runs.session_occupied(10, groups, sessions.__getitem__)
    assert runs.session_occupied(500, groups, sessions.__getitem__)
    assert not runs.session_occupied(600, groups, sessions.__getitem__)

    def gone(pid: int) -> int:
        raise ProcessLookupError(pid)

    assert not runs.session_occupied(500, groups, gone)

    def denied(pid: int) -> int:
        raise PermissionError(pid)

    with pytest.raises(runs.TableUnreadable, match="session of process 10"):
        runs.session_occupied(500, groups, denied)


@pytest.mark.parametrize("text", ["", "12 x\n", "12\n", "1 2 3\n"])
def test_a_process_list_that_cannot_be_parsed_is_unreadable(text: str) -> None:
    with pytest.raises(runs.TableUnreadable):
        runs.parse_groups(text)


def test_the_real_session_search_finds_a_live_session_member(tmp_path: Path) -> None:
    """``getsid`` is asked of each process the stood-in list names, as on macOS,
    whose ``ps`` gives no session."""
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True
    )
    try:
        groups = [(child.pid, child.pid), (os.getpid(), os.getpgid(0))]
        assert runs.session_occupied(child.pid, lambda: groups)
        assert not runs.session_occupied(child.pid, lambda: groups[1:])
    finally:
        child.kill()
        child.wait()


def command_occupied(pid_file: Path) -> Callable[[int], bool]:
    """The real session search over the one process the test knows, the command
    whose identifier ``pid_file`` holds, since ``ps -A`` cannot run in a sandbox."""

    def occupied(session: int) -> bool:
        if not pid_file.exists() or not pid_file.read_text():
            return stand_in_occupied(session)
        pid = int(pid_file.read_text())
        try:
            listed = [(pid, os.getpgid(pid))]
        except ProcessLookupError:
            listed = []
        return stand_in_occupied(session) or runs.session_occupied(
            session, lambda: listed
        )

    return occupied


def test_a_lost_runs_surviving_command_blocks_a_successor_until_it_ends(
    project: Path,
) -> None:
    """T2. The supervisor is killed, so the run is lost, and its command lives on in
    a group of its own within the supervisor's session. The scope stays blocked
    until the command ends, and is then cleared with cause ``empty``."""
    pid_file = project / "command.pid"
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project, command_occupied(pid_file))
        scheduler.set_runs(
            {
                "wait": python_command(
                    f"import os, time; open({str(pid_file)!r}, 'w')"
                    ".write(str(os.getpid())); time.sleep(60)"
                ),
                "gate": python_command("print('gate passed')"),
            }
        )
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        run = asyncio.run(scheduler.start_run("alpha", "wait", "none"))
        child = scheduler._run_children[run.run_id]  # pyright: ignore[reportPrivateUsage]
        try:
            wait_for_file(pid_file)
            child.kill()
            child.wait(timeout=WAIT_SECONDS)
            asyncio.run(scheduler.reconcile_runs())
            found = log.state.runs[run.run_id]
            assert found.ended is not None and found.ended.status is RunStatus.LOST
            assert found.blocks and found.cleared is None
            assert "It blocks its scope" in scheduler.run_status("alpha")
            with pytest.raises(runs.RunRefused, match="is lost and its command may"):
                asyncio.run(scheduler.start_run("alpha", "gate", run.run_id))
        finally:
            with contextlib.suppress(OSError, ValueError):
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
        for _ in range(WAIT_SECONDS * 20):
            asyncio.run(scheduler.reconcile_runs())
            if log.state.runs[run.run_id].cleared is not None:
                break
            time.sleep(0.05)
        cleared = log.state.runs[run.run_id].cleared
        assert cleared == RunCleared(
            run.run_id, RunClearance.EMPTY, None, cleared.cleared_at if cleared else ""
        )
        assert any("Run cleared" in note for note in harness_notes(log, "alpha"))
        second = asyncio.run(scheduler.start_run("alpha", "gate", run.run_id))
        wait_for_supervisor(scheduler, second.run_id)
        asyncio.run(scheduler.reconcile_runs())


def lost_on(log: EventLog, scheduler: Scheduler) -> str:
    """A line ``alpha`` with a lost run, recorded as a restart would find it."""
    scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
    run = replace(run_in(Path("/work")), pid=999_999)
    log.append(run)
    log.append(ended(run.run_id, RunStatus.LOST))
    return run.run_id


def test_an_unreadable_table_keeps_a_lost_run_blocking(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project, unreadable_occupied)
        run_id = lost_on(log, scheduler)
        asyncio.run(scheduler.reconcile_runs())
        assert log.state.runs[run_id].blocks
        with pytest.raises(runs.RunRefused, match="dromon run clear"):
            asyncio.run(scheduler.start_run("alpha", "gate", run_id))


def test_an_operator_clears_a_lost_run_and_is_recorded(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project, lambda session: True)
        run_id = lost_on(log, scheduler)
        asyncio.run(scheduler.reconcile_runs())
        assert log.state.runs[run_id].blocks
        cleared = scheduler.clear_run(ANA, run_id)
        assert (cleared.cause, cleared.operator_id) == (RunClearance.OPERATOR, "ana")
        assert "Cleared by operator ana" in scheduler.run_status("alpha")
        with pytest.raises(EventRejected, match="cleared already"):
            scheduler.clear_run(ANA, run_id)
        with pytest.raises(EventRejected, match="no run r-9"):
            scheduler.clear_run(ANA, "r-9")
    with EventLog(project / "events.sqlite") as log:
        assert log.state.runs[run_id].cleared == cleared


def test_run_clear_is_an_operator_command(
    project: Path, short_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operation through the client, which only an operator reaches."""
    import json

    from dromon import cli
    from dromon.configview import Overrides
    from dromon.daemon import Daemon
    from dromon.model import format_due
    from dromon.paths import project_paths
    from dromon.projectconfig import ProjectConfig
    from dromon.protocol import Operation, Response

    paths = project_paths(project, short_runtime)
    paths.state_dir.mkdir(mode=0o700)
    with EventLog(paths.event_log) as log:
        scheduler = scheduler_on(log, project, lambda session: True)
        run_id = lost_on(log, scheduler)
        daemon = Daemon(
            paths,
            log,
            format_due(datetime(2026, 10, 31, 9, tzinfo=UTC)),
            scheduler,
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=False,
        )

        def call(
            socket: Path, operation: Operation, args: dict[str, object] | None = None
        ) -> Response:
            del socket
            line = {"id": "r", "op": operation.value, "v": 1, "args": args or {}}
            return daemon.respond((json.dumps(line) + "\n").encode())

        monkeypatch.setattr(cli, "call", call)
        refused = call(paths.socket, Operation.RUN_CLEAR, {"run_id": run_id})
        assert refused.error_code == "bad_request"
        base = ("--project", str(project), "--runtime-dir", str(short_runtime))
        assert cli.main([*base, "run", "clear", run_id, "--operator", "ben"]) == 0
        cleared = log.state.runs[run_id].cleared
        assert cleared is not None and cleared.operator_id == "ben"
        assert cli.main([*base, "run", "clear", run_id, "--operator", "ben"]) != 0


# T1. A named pipe at the output path.


def test_a_named_pipe_at_the_output_path_has_no_digest_and_does_not_block(
    tmp_path: Path,
) -> None:
    """Review R1. Without ``O_NONBLOCK`` the open waits for a writer, and with it
    the process worker. This fails on 09003fa, where the reading never returns."""
    (tmp_path / "tmp" / "runs").mkdir(parents=True)
    pipe = tmp_path / "tmp" / "runs" / "r-1.out"
    os.mkfifo(pipe)
    found: list[tuple[str | None, int | None]] = []
    reader = threading.Thread(
        target=lambda: found.append(runs.output_digest(tmp_path, "tmp/runs/r-1.out")),
        daemon=True,
    )
    reader.start()
    reader.join(timeout=5)
    if reader.is_alive():
        # Let the blocked reading return before failing, so no thread is left.
        os.close(os.open(pipe, os.O_WRONLY | os.O_NONBLOCK))
        reader.join(timeout=5)
        pytest.fail("reading a named pipe's digest blocked")
    assert found == [(None, None)]


# T3. A read-only seat that owns its role.


def test_a_read_only_seat_that_owns_its_role_starts_and_stops_no_run(
    project: Path,
) -> None:
    """Review R3. A run writes the worktree outside the sandbox, so owning the role
    is not enough."""
    from dromon.lineconfig import LineConfig, WorkdirAccess

    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        for event in (
            OperatorRegistered("ana"),
            RoleDeclared("core", "."),
            SeatBound("alpha", "core", True),
            OwnershipChanged(
                "core", "alpha", None, OwnershipCause.DECLARED, ANA, None, AT
            ),
        ):
            log.append(event)
        scheduler.configure_line("alpha", LineConfig(workdir_access=WorkdirAccess.READ))
        with pytest.raises(runs.RunRefused, match="which you may only read"):
            asyncio.run(scheduler.start_run("alpha", "gate", "none"))
        with pytest.raises(runs.RunRefused, match="which you may only read"):
            asyncio.run(scheduler.stop_run("alpha", "r-1"))
        assert log.state.runs == {}


# T4. A word to go that cannot be delivered.


def test_a_release_to_a_supervisor_that_is_gone_is_false(tmp_path: Path) -> None:
    found, _ = launched(tmp_path, python_command("pass"))
    found.child.kill()
    found.child.wait(timeout=WAIT_SECONDS)
    assert not runs.release(found)


def test_a_run_whose_supervisor_cannot_be_told_to_start_is_recorded_stopped(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review R6. The caller is told that nothing started, and the log says so
    rather than calling the run lost with a command that may still run."""
    marker = project / "ran"
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        scheduler.set_runs(
            {"touch": python_command(f"open({str(marker)!r}, 'w').close()")}
        )
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

        def undelivered(launched: runs.Launched) -> bool:
            return False

        monkeypatch.setattr(runs, "release", undelivered)
        with pytest.raises(runs.RunRefused, match="its command never started"):
            asyncio.run(scheduler.start_run("alpha", "touch", "none"))
        (run,) = log.state.runs.values()
        assert run.ended is not None
        assert (run.ended.status, run.ended.exit_code) == (RunStatus.STOPPED, None)
        assert not run.blocks
        assert not marker.exists()
        assert run.started.run_id not in scheduler._run_children  # pyright: ignore[reportPrivateUsage]


# T5. Two starts at once.


def test_two_starts_at_once_record_one_run_and_start_one_command(
    project: Path,
) -> None:
    """Both pass the scheduler's checks before either is recorded, so the log
    refuses the second, and its supervisor is abandoned without the word to go."""
    marker = project / "ran"
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        scheduler.set_runs(
            {"mark": python_command(f"open({str(marker)!r}, 'a').write('ran\\n')")}
        )
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

        async def both() -> tuple[RunStarted | BaseException, ...]:
            return await asyncio.gather(
                scheduler.start_run("alpha", "mark", "none"),
                scheduler.start_run("alpha", "mark", "none"),
                return_exceptions=True,
            )

        results = asyncio.run(both())
        recorded = [r for r in results if isinstance(r, RunStarted)]
        refused = [r for r in results if isinstance(r, EventRejected)]
        assert len(recorded) == 1 and len(refused) == 1, results
        assert "has not ended" in str(refused[0])
        assert list(log.state.runs) == [recorded[0].run_id]
        wait_for_supervisor(scheduler, recorded[0].run_id)
        assert marker.read_text() == "ran\n"


# T6. A process table that refuses.


@pytest.mark.parametrize(
    ("code", "stderr", "absent"),
    [
        (1, "", True),
        (0, "", True),
        (1, "ps: permission denied\n", False),
        (2, "", False),
    ],
)
def test_ps_with_no_output_is_absent_only_when_it_says_nothing(
    monkeypatch: pytest.MonkeyPatch, code: int, stderr: str, absent: bool
) -> None:
    def fake(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["/bin/ps"], code, "", stderr)

    monkeypatch.setattr(runs.subprocess, "run", fake)
    if absent:
        assert runs.process_start(4321) is None
    else:
        with pytest.raises(runs.TableUnreadable, match=f"status {code}"):
            runs.process_start(4321)


def test_a_denied_table_ends_no_live_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["/bin/ps"], 1, "", "ps: not permitted\n")

    monkeypatch.setattr(runs.subprocess, "run", denied)
    with pytest.raises(runs.TableUnreadable):
        runs.reconcile([run_in(tmp_path)], tmp_path)
    with pytest.raises(runs.TableUnreadable):
        runs.process_groups()


# T8. What a declared command runs.


def test_a_relative_program_resolves_in_the_working_directory(tmp_path: Path) -> None:
    """``RUNS.md`` says so, since that is where a writing line could change it."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    script = workdir / "build.sh"
    script.write_text('#!/bin/sh\necho built in "$PWD"\n')
    script.chmod(0o755)
    found, run = launched(tmp_path, ("./build.sh",))
    runs.release(found)
    assert found.child.wait(timeout=WAIT_SECONDS) == 0
    receipt = runs.read_receipt(tmp_path, run.run_id)
    assert receipt == runs.Receipt(RunStatus.EXITED, 0, "")
    output = (workdir / run.output_file).read_text()
    assert output == f"built in {os.path.realpath(workdir)}\n"


def test_the_log_records_exactly_the_declared_argv(project: Path) -> None:
    declared = python_command("import sys; print(sys.argv)")
    path = project / "events.sqlite"
    with EventLog(path) as log:
        scheduler = scheduler_on(log, project)
        scheduler.set_runs({"show": declared})
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        run = asyncio.run(scheduler.start_run("alpha", "show", "none"))
        assert run.argv == declared
        wait_for_supervisor(scheduler, run.run_id)
    with EventLog(path) as log:
        assert log.state.runs[run.run_id].started.argv == declared


# Review R7. The supervisor signals the group only while the command is unreaped.


def test_a_command_that_ignores_sigterm_is_killed_after_the_grace(
    tmp_path: Path,
) -> None:
    ready = tmp_path / "ready"
    child = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            f" open({str(ready)!r}, 'w').write('1'); time.sleep(60)",
        ],
        process_group=0,
    )
    try:
        wait_for_file(ready)
        supervision = runs.Supervision()
        supervision.stopping.set()
        assert supervision.wait(child, grace=0.2) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_a_command_that_ends_unstopped_is_never_signalled(tmp_path: Path) -> None:
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", "pass"], process_group=0
    )
    assert runs.Supervision().wait(child) == 0


# Round 14, item 2. Receipts that name what the gate skipped, and failed runs linked
# to the run that corrected them.


QUIET_GATE = b"""\
........s....
=========================== short test summary info ============================
SKIPPED [8] tests/test_launcher.py:470: ps cannot run here, as inside an agent sandbox
SKIPPED [2] tests/test_launcher.py:120: only Linux has a child subreaper
5162 passed, 10 skipped, 48 deselected in 231.52s (0:03:51)
"""


def test_the_summary_names_the_counts_and_each_skip_reason() -> None:
    assert runs.gate_summary(QUIET_GATE) == (
        "5162 passed, 10 skipped, 48 deselected, skipped [8]"
        " tests/test_launcher.py:470: ps cannot run here, as inside an agent"
        " sandbox; [2] tests/test_launcher.py:120: only Linux has a child subreaper"
    )


def test_only_the_last_summary_block_is_read() -> None:
    """Audit of round 14. A green final line printed after pytest's own block is
    not taken, and a mark with no final line after it gives no summary."""
    failing = (
        b"short test summary info\nFAILED tests/t.py::x - boom\n"
        b"1 failed, 9 passed in 2.0s\n"
    )
    assert (
        runs.gate_summary(failing + b"5000 passed in 1.00s\n") == "1 failed, 9 passed"
    )
    assert runs.gate_summary(failing + b"short test summary info\n") is None
    # Without a mark the last final line is taken, with no reasons.
    assert runs.gate_summary(b"SKIPPED [1] a.py:1: why\n3 passed in 1s\n") == (
        "3 passed"
    )


def test_a_skip_reason_is_cut_after_its_place() -> None:
    reason = "r" * (runs.MAX_REASON_CHARS + 20)
    tail = (
        f"short test summary info\nSKIPPED [1] a.py:7: {reason}\n"
        "1 passed, 1 skipped in 1s\n"
    )
    summary = runs.gate_summary(tail.encode())
    assert summary == (
        f"1 passed, 1 skipped, skipped [1] a.py:7: {'r' * runs.MAX_REASON_CHARS}..."
    )


def test_the_summary_reads_the_last_final_line_with_or_without_its_rule() -> None:
    ruled = b"===== 3 failed, 1 passed in 0.5s =====\n"
    assert runs.gate_summary(ruled) == "3 failed, 1 passed"
    # An earlier run's summary in the same output, as from a gate run twice, is
    # replaced by the last, with only the last section's reasons.
    twice = QUIET_GATE + b"short test summary info\n7 passed in 1.0s\n"
    assert runs.gate_summary(twice) == "7 passed"
    assert runs.gate_summary(b"ruff: All checks passed!\n") is None
    assert runs.gate_summary(b"") is None


def test_the_summary_is_bounded_and_printable() -> None:
    count = runs.MAX_SKIP_REASONS + 3
    reasons = b"short test summary info\n" + b"".join(
        b"SKIPPED [1] tests/t.py:%d: reason\x1b\x07 " % n + b"x" * 400 + b"\n"
        for n in range(count)
    )
    summary = runs.gate_summary(reasons + b"1 passed, %d skipped in 2s\n" % count)
    assert summary is not None
    assert summary.endswith("; and 3 more")
    assert summary.count("[1] tests/t.py:") == runs.MAX_SKIP_REASONS
    assert "\x1b" not in summary and "\x07" not in summary
    longest = max(len(part) for part in summary.split("; "))
    assert longest <= runs.MAX_REASON_CHARS + len("1 passed, 11 skipped, skipped ") + 30


def test_a_note_with_a_summary_fits_the_event_and_is_labelled_unverified() -> None:
    assert runs.note_with_summary("as it was", None) == "as it was"
    assert runs.note_with_summary("", "1 passed") == (
        f'{runs.SUMMARY_LABEL} "1 passed"'
    )
    assert runs.SUMMARY_LABEL.startswith("Unverified")
    long = runs.note_with_summary("é" * MAX_RUN_NOTE_BYTES, "1 passed")
    assert len(long.encode()) <= MAX_RUN_NOTE_BYTES
    RunEnded("r-1", RunStatus.EXITED, 0, None, None, long, LATER)


def test_the_tail_is_read_as_the_digest_is_and_only_from_a_regular_file(
    tmp_path: Path,
) -> None:
    run = run_in(tmp_path)
    write_output(tmp_path, run, b"a" * runs.TAIL_BYTES + QUIET_GATE)
    tail = runs.output_tail(tmp_path, run.output_file)
    assert tail is not None and len(tail) == runs.TAIL_BYTES
    assert tail.endswith(QUIET_GATE)
    assert runs.output_tail(tmp_path, "tmp/runs/missing.out") is None
    pipe = tmp_path / "tmp" / "runs" / "pipe.out"
    os.mkfifo(pipe)
    assert runs.output_tail(tmp_path, "tmp/runs/pipe.out") is None
    (tmp_path / "tmp" / "runs" / "link.out").symlink_to(tmp_path / run.output_file)
    assert runs.output_tail(tmp_path, "tmp/runs/link.out") is None


def test_a_receipt_carries_the_outputs_summary_into_the_finding(
    tmp_path: Path,
) -> None:
    run = run_in(tmp_path)
    digest = write_output(tmp_path, run, QUIET_GATE)
    runs.write_receipt(
        runs.receipt_path(tmp_path, run.run_id), runs.Receipt(RunStatus.EXITED, 0, "")
    )
    (finding,) = runs.reconcile([run], tmp_path, lambda pid: None)
    assert finding.output_digest == digest
    assert finding.summary == runs.gate_summary(QUIET_GATE)


def test_a_lost_run_shows_no_summary_however_green_its_output(
    tmp_path: Path,
) -> None:
    """Audit of round 14. A lost run has no exit code, so a green summary in its
    output, which the line could have printed, is never shown as its result."""
    run = run_in(tmp_path)
    digest = write_output(tmp_path, run, QUIET_GATE)
    (finding,) = runs.reconcile([run], tmp_path, lambda pid: None)
    assert finding.status is RunStatus.LOST
    assert finding.output_digest == digest
    assert finding.summary is None


def a_run(
    run_id: str,
    status: RunStatus | None,
    code: int | None = None,
    command: str = "gate",
) -> Run:
    begun = replace(started(run_id), command=command)
    end = (
        None
        if status is None
        else RunEnded(run_id, status, code, None, None, "", LATER)
    )
    return Run(begun, end)


def test_failed_runs_are_linked_to_the_next_pass_of_their_command() -> None:
    scope = [
        a_run("r-1", RunStatus.EXITED, 0),
        a_run("r-2", RunStatus.EXITED, 1),
        a_run("r-3", RunStatus.STOPPED),
        a_run("r-4", RunStatus.EXITED, 0, command="device"),
        a_run("r-5", RunStatus.LOST),
        a_run("r-6", RunStatus.EXITED, 0),
        a_run("r-7", RunStatus.EXITED, 2),
        a_run("r-8", None),
    ]
    assert runs.corrections(scope) == {"r-6": ("r-2", "r-3", "r-5")}
    assert [runs.failed(run) for run in scope] == [
        False,
        True,
        True,
        False,
        True,
        False,
        True,
        False,
    ]


def test_run_status_names_the_correction_both_ways(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        previous = None
        for number, code in enumerate((1, 0, 0), start=1):
            run_id = f"r-{number}"
            log.append(started(run_id, previous=previous))
            log.append(RunEnded(run_id, RunStatus.EXITED, code, None, None, "", LATER))
            previous = run_id
        shown = scheduler.run_status("alpha").splitlines()
    assert shown[0].startswith("previous is r-3")
    assert shown[1].startswith("run r-3 ") and "orrect" not in shown[1]
    assert shown[2].endswith("It corrects the failed runs r-1")
    assert shown[3].endswith("Corrected by run r-2")


# Round 14, item 11. The runs an operator reads without the log.


def runs_in_two_scopes(log: EventLog, scheduler: Scheduler) -> int:
    """Line ``alpha`` with ``MAX_RUNS_SHOWN + 2`` ended runs, the first failing,
    and line ``beta`` with one open run. Returns how many runs ``alpha`` has."""
    from dromon.scheduler import MAX_RUNS_SHOWN

    scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
    scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)
    count = MAX_RUNS_SHOWN + 2
    previous = None
    for number in range(1, count + 1):
        run_id = f"r-{number}"
        log.append(started(run_id, previous=previous))
        code = 1 if number == 1 else 0
        log.append(RunEnded(run_id, RunStatus.EXITED, code, None, None, "", LATER))
        previous = run_id
    log.append(started("r-b", line_id="beta"))
    return count


def test_runs_view_gives_each_scope_newest_first_and_bounded(project: Path) -> None:
    from dromon.scheduler import MAX_RUNS_SHOWN

    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        count = runs_in_two_scopes(log, scheduler)
        views = scheduler.runs_view(None)
    alpha = [view for view in views if view["scope"] == "line alpha"]
    beta = [view for view in views if view["scope"] == "line beta"]
    assert [view["scope"] for view in views] == ["line alpha"] * len(alpha) + [
        "line beta"
    ]
    expected = [f"r-{number}" for number in range(count, count - MAX_RUNS_SHOWN, -1)]
    assert [view["run_id"] for view in alpha] == expected
    newest = alpha[0]
    assert (newest["command"], newest["line_id"], newest["state"]) == (
        "gate",
        "alpha",
        "exited",
    )
    assert (newest["exit_code"], newest["ended_at"]) == (0, LATER)
    assert newest["output_file"] == f"tmp/runs/r-{count}.out"
    assert newest["previous"] == f"r-{count - 1}"
    # r-2 corrected r-1, which is no longer shown, and the link is still given.
    assert alpha[-1]["run_id"] == "r-3" and alpha[-1]["corrects"] == []
    assert beta[0]["state"] == "running" and beta[0]["blocks"] is True
    assert (beta[0]["ended_at"], beta[0]["exit_code"]) == (None, None)


def test_runs_view_of_one_run_names_its_correction_and_refuses_an_unknown(
    project: Path,
) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project)
        runs_in_two_scopes(log, scheduler)
        (first,) = scheduler.runs_view("r-1")
        (second,) = scheduler.runs_view("r-2")
        with pytest.raises(EventRejected, match="no run r-0"):
            scheduler.runs_view("r-0")
    assert (first["exit_code"], first["corrected_by"]) == (1, "r-2")
    assert second["corrects"] == ["r-1"] and second["corrected_by"] is None


def test_runs_view_of_a_cleared_lost_run_names_who_cleared_it(project: Path) -> None:
    with EventLog(project / "events.sqlite") as log:
        scheduler = scheduler_on(log, project, lambda session: True)
        run_id = lost_on(log, scheduler)
        (blocking,) = scheduler.runs_view(run_id)
        scheduler.clear_run(ANA, run_id)
        (cleared,) = scheduler.runs_view(run_id)
    assert (blocking["state"], blocking["blocks"], blocking["exit_code"]) == (
        "lost",
        True,
        None,
    )
    assert (cleared["blocks"], cleared["cleared_by"]) == (False, "ana")
    assert cleared["cleared_at"] is not None


def test_dromon_run_status_prints_runs_and_one_receipt(
    project: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The ``runs`` request through the client, with its refusals."""
    import json

    from dromon import cli
    from dromon.configview import Overrides
    from dromon.daemon import Daemon
    from dromon.model import format_due
    from dromon.paths import project_paths
    from dromon.projectconfig import ProjectConfig
    from dromon.protocol import Operation, Response

    paths = project_paths(project, short_runtime)
    paths.state_dir.mkdir(mode=0o700)
    base = ("--project", str(project), "--runtime-dir", str(short_runtime))
    with EventLog(paths.event_log) as log:
        scheduler = scheduler_on(log, project)
        daemon = Daemon(
            paths,
            log,
            format_due(datetime(2026, 10, 31, 9, tzinfo=UTC)),
            scheduler,
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=False,
        )

        def call(
            socket: Path, operation: Operation, args: dict[str, object] | None = None
        ) -> Response:
            del socket
            line = {"id": "r", "op": operation.value, "v": 1, "args": args or {}}
            return daemon.respond((json.dumps(line) + "\n").encode())

        monkeypatch.setattr(cli, "call", call)
        assert cli.main([*base, "run", "status"]) == 0
        assert capsys.readouterr().out == "no runs yet\n"
        count = runs_in_two_scopes(log, scheduler)
        refused = call(paths.socket, Operation.RUNS, {"run_id": 7})
        assert refused.error_code == "bad_request"
        assert cli.main([*base, "run", "status"]) == 0
        listed = capsys.readouterr().out.splitlines()
        assert cli.main([*base, "run", "status", "r-1"]) == 0
        one = capsys.readouterr().out.splitlines()
        assert cli.main([*base, "run", "status", "r-0"]) != 0
        assert "no run r-0" in capsys.readouterr().err
        assert cli.main([*base, "run", "clear"]) == 2
        assert "name the run to clear" in capsys.readouterr().err
    assert listed[0] == "line alpha"
    assert listed[1] == (
        f"  r-{count} gate by alpha, started {AT}, exited with exit code 0 at"
        f" {LATER}, output /work/tmp/runs/r-{count}.out"
    )
    assert "line beta" in listed
    assert listed[-1].endswith(
        "still running as process 4321, output /work/tmp/runs/r-b.out"
    )
    assert one[0].startswith("r-1 gate by alpha,")
    assert one[0].endswith(", corrected by r-2")
    assert one[1:] == ["  scope: line alpha", "  previous: none"]


# Round 15, item 2. A stop also ends the descendants below the supervisor that called
# ``setsid``. These stand in for the walk, since ``ps`` cannot run inside an agent
# sandbox, and one test at the end reads the real table where ``ps`` runs.


def sleeper(
    *, ignore_term: bool = False, own_session: bool = True
) -> subprocess.Popen[bytes]:
    code = "import signal, time;"
    if ignore_term:
        code += " signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    code += " time.sleep(60)"
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-c", code],
        start_new_session=own_session,
        process_group=None if own_session else 0,
    )


STARTED = "Sat Nov  1 07:23:05 2026"
"""A start time as a walk reads it, for the rows a test stands in for the walk with."""


def row_of(process: subprocess.Popen[bytes], parent: int = 1) -> TreeRow:
    return TreeRow(process.pid, parent, process.pid, "sleep", STARTED)


def unchanged(pid: int) -> tuple[int, str]:
    """The identity every ``row_of`` row still has, parent 1 and ``STARTED``."""
    return 1, STARTED


def finish(*processes: subprocess.Popen[bytes]) -> None:
    for process in processes:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_a_stop_ends_a_descendant_in_a_session_of_its_own() -> None:
    command = sleeper(own_session=False)
    stray = sleeper()
    try:
        supervision = runs.Supervision(
            lambda: [] if stray.poll() is not None else [row_of(stray)], unchanged
        )
        supervision.stopping.set()
        assert supervision.wait(command, grace=5.0) == -signal.SIGTERM
        assert stray.wait(timeout=WAIT_SECONDS) == -signal.SIGTERM
        assert supervision.outside == {stray.pid}
        assert supervision.note() == ", with 1 descendants outside its group"
    finally:
        finish(command, stray)


def test_the_walk_is_made_before_the_group_is_signalled() -> None:
    """The group signal ends the command and orphans what it left, so a walk made
    after it misses the descendant it is for. The full-gate run of 2026-11-01 failed
    for this. Here the first walk of a stop must see the command still running, and
    the stray found by it is ended although the command died of the group signal."""
    command = sleeper(own_session=False)
    stray = sleeper()
    seen: list[int | None] = []

    def walk() -> list[TreeRow] | None:
        seen.append(command.poll())
        # After the command is gone the stray is no longer below the supervisor, as
        # it is not once the system's first process has been given it.
        return [row_of(stray)] if command.poll() is None else []

    try:
        supervision = runs.Supervision(walk, unchanged)
        supervision.stopping.set()
        assert supervision.wait(command, grace=5.0) == -signal.SIGTERM
        assert seen[0] is None
        assert stray.wait(timeout=WAIT_SECONDS) == -signal.SIGTERM
        assert supervision.outside == {stray.pid}
    finally:
        finish(command, stray)


def test_a_descendant_that_ignores_sigterm_is_killed_after_the_grace() -> None:
    command = sleeper(own_session=False)
    stray = sleeper(ignore_term=True)
    time.sleep(0.3)
    try:
        supervision = runs.Supervision(
            lambda: [] if stray.poll() is not None else [row_of(stray)], unchanged
        )
        supervision.stopping.set()
        started = time.monotonic()
        supervision.wait(command, grace=0.5)
        assert stray.wait(timeout=WAIT_SECONDS) == -signal.SIGKILL
        assert time.monotonic() - started >= 0.5, "the grace was kept"
    finally:
        finish(command, stray)


def test_a_command_that_ends_first_is_not_returned_while_a_descendant_runs() -> None:
    """The command ends on ``SIGTERM`` at once, and a descendant that ignores it
    keeps the supervisor until the grace ends and ``SIGKILL`` follows."""
    command = sleeper(own_session=False)
    stray = sleeper(ignore_term=True)
    time.sleep(0.3)
    try:
        supervision = runs.Supervision(
            lambda: [] if stray.poll() is not None else [row_of(stray)], unchanged
        )
        supervision.stopping.set()
        started = time.monotonic()
        assert supervision.wait(command, grace=0.6) == -signal.SIGTERM
        assert time.monotonic() - started >= 0.6
        assert stray.wait(timeout=WAIT_SECONDS) == -signal.SIGKILL
    finally:
        finish(command, stray)


@pytest.mark.parametrize("changed", ["start", "parent", "gone"])
def test_a_process_whose_identity_changed_since_the_walk_is_not_signalled(
    changed: str,
) -> None:
    """Re-review of 4da3e0e. Between the walk and the signal the descendant can end
    and its identifier go to a process that is not one. The stray here stands for that
    process. Its start time or parent differs from the row, or ``ps`` cannot say, so it
    is left running and is not counted among the descendants ended."""
    command = sleeper(own_session=False)
    stray = sleeper()
    time.sleep(0.3)

    def identity(pid: int) -> tuple[int, str] | None:
        if changed == "gone":
            return None
        if changed == "start":
            return 1, "Sun Nov  2 00:00:00 2026"
        return 2, STARTED

    try:
        supervision = runs.Supervision(lambda: [row_of(stray)], identity)
        supervision.stopping.set()
        assert supervision.wait(command, grace=0.3) == -signal.SIGTERM
        assert stray.poll() is None, "a process with another identity was signalled"
        assert supervision.outside == set()
        assert supervision.note() == ""
    finally:
        finish(command, stray)


def test_the_identity_is_checked_before_the_group_is_signalled() -> None:
    """The group signal ends the command, and a descendant it leaves has another parent
    from then on, so a check made after it would find every orphan changed. The first
    check of a stop must see the command still running."""
    command = sleeper(own_session=False)
    stray = sleeper()
    time.sleep(0.3)
    seen: list[int | None] = []

    def identity(pid: int) -> tuple[int, str]:
        seen.append(command.poll())
        return 1, STARTED

    try:
        supervision = runs.Supervision(
            lambda: [] if command.poll() is not None else [row_of(stray)], identity
        )
        supervision.stopping.set()
        supervision.wait(command, grace=5.0)
        assert seen and seen[0] is None
        assert stray.wait(timeout=WAIT_SECONDS) == -signal.SIGTERM
    finally:
        finish(command, stray)


def test_a_child_is_signalled_before_its_parent_so_it_is_not_orphaned_first() -> None:
    """Round 15, re-review. With the real process table the parent of a descendant in a
    session of its own is the command. A signal to the command first ended it, the
    descendant's parent became the system's first process, and the check turned the
    descendant away. The identity here changes once the parent has ended."""
    parent = sleeper(own_session=False)
    child = sleeper(ignore_term=True)
    time.sleep(0.3)
    parent_row = TreeRow(parent.pid, 1, parent.pid, "sleep", STARTED)
    child_row = TreeRow(child.pid, parent.pid, child.pid, "sleep", STARTED)

    checked: list[int] = []

    def identity(pid: int) -> tuple[int, str] | None:
        """The parent's check comes just before its signal, so a child checked after
        it has been orphaned by that signal."""
        checked.append(pid)
        if pid == child.pid:
            return (1 if parent.pid in checked else parent.pid), STARTED
        return 1, STARTED

    try:
        supervision = runs.Supervision(lambda: [parent_row, child_row], identity)
        supervision.stopping.set()
        supervision._end(parent, parent.poll(), signal.SIGTERM)  # pyright: ignore[reportPrivateUsage]
        assert parent.wait(timeout=WAIT_SECONDS) == -signal.SIGTERM
        assert child_row.pid in supervision.outside
    finally:
        finish(parent, child)


def test_a_signal_goes_only_to_what_the_latest_walk_found() -> None:
    """The first walk names the stray, and the second names nothing, as when the stray
    ended and its identifier was reused. The remembered identifier is not signalled."""
    command = sleeper(own_session=False)
    stray = sleeper(ignore_term=True)
    time.sleep(0.3)
    answers: list[list[TreeRow]] = [[row_of(stray)]]

    def walk() -> list[TreeRow]:
        return answers.pop(0) if answers else []

    try:
        supervision = runs.Supervision(walk, unchanged)
        supervision.stopping.set()
        assert supervision.wait(command, grace=0.3) == -signal.SIGTERM
        assert stray.poll() is None, "a process the latest walk did not name was killed"
    finally:
        finish(command, stray)


def test_a_process_the_walk_does_not_name_is_left_alone() -> None:
    command = sleeper(own_session=False)
    neighbour = sleeper()
    try:
        supervision = runs.Supervision(lambda: [])
        supervision.stopping.set()
        supervision.wait(command, grace=0.3)
        assert neighbour.poll() is None
        assert supervision.outside == set()
        assert supervision.note() == ""
    finally:
        finish(command, neighbour)


def test_an_unreadable_table_still_ends_the_group_and_the_receipt_says_so() -> None:
    command = sleeper(own_session=False)
    try:
        supervision = runs.Supervision(lambda: None)
        supervision.stopping.set()
        assert supervision.wait(command, grace=0.3) == -signal.SIGTERM
        assert supervision.unreadable
        assert "could not be read" in supervision.note()
    finally:
        finish(command)


def test_a_stop_that_never_comes_never_walks_the_table() -> None:
    def walk() -> list[TreeRow]:
        raise AssertionError("no stop was noted, so nothing may be walked")

    child = subprocess.Popen([sys.executable, "-c", "pass"], process_group=0)  # noqa: S603
    assert runs.Supervision(walk, unchanged).wait(child) == 0


def test_the_receipt_of_a_stopped_run_names_the_descendants_it_ended(
    tmp_path: Path,
) -> None:
    stray = sleeper()
    try:
        supervision = runs.Supervision(
            lambda: [] if stray.poll() is not None else [row_of(stray)], unchanged
        )
        stopper = threading.Timer(0.5, supervision.stopping.set)
        stopper.start()
        receipt = runs.supervise(
            str(tmp_path), python_command("import time; time.sleep(60)"), supervision
        )
        assert receipt.status is RunStatus.STOPPED
        assert receipt.note.startswith("stop_run asked its supervisor to end it, with")
        assert len(receipt.note.encode()) <= MAX_RUN_NOTE_BYTES
    finally:
        finish(stray)


@NEEDS_PS
def test_a_real_stop_ends_a_command_child_that_called_setsid(tmp_path: Path) -> None:
    """With the real process table. The command starts a child in a session of its
    own, so the group kill alone misses it."""
    stray_file = tmp_path / "work" / "stray.pid"
    found, run = launched(
        tmp_path,
        python_command(
            "import subprocess, sys, time;"
            "p = subprocess.Popen("
            "[sys.executable, '-c', 'import time; time.sleep(60)'],"
            " start_new_session=True);"
            f"open({str(stray_file)!r}, 'w').write(str(p.pid)); time.sleep(60)"
        ),
    )
    runs.release(found)
    wait_for_file(stray_file)
    stray = int(stray_file.read_text())
    try:
        runs.signal_run(run, stand_in_start)
        assert found.child.wait(timeout=WAIT_SECONDS) == 0
        receipt = runs.read_receipt(tmp_path, run.run_id)
        assert receipt is not None and receipt.status is RunStatus.STOPPED
        assert "descendants outside its group" in receipt.note
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(stray, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the child in a session of its own outlived the stop")
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(stray, 9)
