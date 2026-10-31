"""Supervised runs: the project's ``[runs]`` table, the ``run_started`` and
``run_ended`` events, the supervisor, reconciliation after a restart and a handoff,
a supervisor that died while the daemon was down, and a reused process identifier.
``docs/architecture/RUNS.md`` is normative."""

import asyncio
import contextlib
import hashlib
import os
import signal
import subprocess
import sys
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
    RunEnded,
    RunStarted,
    RunStatus,
    SeatBound,
    State,
)
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


def scheduler_on(log: EventLog, tmp_path: Path) -> Scheduler:
    found = Scheduler(
        log,
        no_engine,
        hold_wakes=True,
        wall_clock=Clock(),
        root=tmp_path,
        report_dir=tmp_path / "reports",
        run_start_of=stand_in_start,
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
