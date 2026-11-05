"""The check of a state restored from a snapshot, and the durable snapshot write.
**Review of 49236ad, items 1 and 2, required by the operator.** A snapshot whose
digests are valid but whose state is not a replay of the log was restored at a normal
start and never caught, since only ``dromond --check`` compared it. A start that
restored from a snapshot now replays the log in full off the event loop and compares,
and a mismatch holds every line, names its type in the daemon log and in ``status``,
deletes the snapshot and requires a restart. A snapshot's new name is made durable by
synchronizing its directory after the move."""

import asyncio
import os
import sqlite3
import stat
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli, statesnapshot
from dromon import daemon as daemon_module
from dromon.daemon import Daemon
from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import (
    VERIFY_FAILED,
    VERIFY_NONE,
    VERIFY_PASSED,
    VERIFY_PENDING,
    EventLog,
    Restored,
    RestoreMismatch,
    verify_restored,
)
from dromon.model import (
    LineAddress,
    LineCreated,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    State,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")


def seeded(path: Path) -> None:
    """A log with a line and two notes, closed with a snapshot of its newest event."""
    with EventLog(path, snapshots=True) as log:
        log.append(OperatorRegistered("ana"))
        log.append(LineCreated("alpha", HAIKU, "subscription"))
        for number in range(2):
            log.append(MessageSent(f"m-{number}", ANA, ALPHA, MessageKind.NOTE, "go"))


def rows(path: Path) -> list[tuple[str, str]]:
    connection = sqlite3.connect(path)
    try:
        return [
            (str(recorded_at), str(payload))
            for recorded_at, payload in connection.execute(
                "SELECT recorded_at, payload FROM events ORDER BY seq"
            )
        ]
    finally:
        connection.close()


def forge(
    path: Path,
    *,
    state: State | None = None,
    times: dict[str, dict[str, str]] | None = None,
) -> None:
    """Write a snapshot whose digests are valid and whose state or times are not the
    replay's, as a process that can write the snapshot and not the log could."""
    with EventLog(path) as log:
        replayed = log.state
        recorded = log.times.maps()
        seq = log.last_seq
    stored = rows(path)
    statesnapshot.write(
        statesnapshot.snapshot_path(path),
        schema_version=1,
        seq=seq,
        first_row=statesnapshot.row_digest(*stored[0]),
        nth_row=statesnapshot.row_digest(*stored[seq - 1]),
        state=replayed if state is None else state,
        times=recorded if times is None else times,
    )


def intruded(path: Path) -> State:
    with EventLog(path) as log:
        state = log.state
    state.operators.add("intruder")
    return state


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "events.sqlite"


# The check itself.


def test_a_snapshot_that_agrees_passes_and_lets_snapshots_be_written(
    path: Path, tmp_path: Path
) -> None:
    seeded(path)
    with EventLog(path, snapshots=True) as log:
        assert (log.start.snapshot, log.start.verified) == ("used", VERIFY_PENDING)
        restored = log.restored
        assert restored is not None and restored.seq == log.last_seq
        assert verify_restored(path, tmp_path / "copy.sqlite", restored) is None
        log.restore_checked(None)
        assert (log.start.verified, log.restored, log.restore_failed) == (
            VERIFY_PASSED,
            None,
            False,
        )
        log.append(OperatorRegistered("ben"))
        log.write_snapshot()
        assert log.start.snapshot_seq == log.last_seq


def test_a_digest_valid_snapshot_whose_state_differs_is_caught_at_a_start(
    path: Path, tmp_path: Path
) -> None:
    seeded(path)
    forge(path, state=intruded(path))
    with EventLog(path, snapshots=True) as log:
        # The forged file passes every check ``read`` makes, so it is restored.
        assert log.start.snapshot == "used"
        assert "intruder" in log.state.operators
        restored = log.restored
        assert restored is not None
        found = verify_restored(path, tmp_path / "copy.sqlite", restored)
    assert found == (RestoreMismatch.STATE_DIFFERS, None)


def test_recorded_times_edited_inside_a_valid_file_are_caught(
    path: Path, tmp_path: Path
) -> None:
    seeded(path)
    with EventLog(path) as log:
        times = {name: dict(kept) for name, kept in log.times.maps().items()}
    assert times["line_changed"]
    times["line_changed"]["alpha"] = "2000-01-01T00:00:00.000000Z"
    forge(path, times=times)
    with EventLog(path, snapshots=True) as log:
        restored = log.restored
        assert restored is not None
        found = verify_restored(path, tmp_path / "copy.sqlite", restored)
    assert found == (RestoreMismatch.STATE_DIFFERS, None)


def test_a_log_shorter_than_the_snapshot_is_a_corrupt_log(
    path: Path, tmp_path: Path
) -> None:
    seeded(path)
    with EventLog(path, snapshots=True) as log:
        restored = log.restored
        assert restored is not None
    beyond = Restored(
        restored.seq + 5, restored.first_row, restored.nth_row, restored.digest
    )
    found = verify_restored(path, tmp_path / "copy.sqlite", beyond)
    assert found == (RestoreMismatch.LOG_CORRUPT, None)


def test_a_check_that_cannot_run_fails_closed_with_the_type_only(
    path: Path, tmp_path: Path
) -> None:
    seeded(path)
    taken = tmp_path / "copy.sqlite"
    taken.write_text("already here")
    with EventLog(path, snapshots=True) as log:
        restored = log.restored
        assert restored is not None
        found = verify_restored(path, taken, restored)
    assert found == (RestoreMismatch.CHECK_FAILED, "LogError")


def test_an_exception_type_that_is_not_an_identifier_gives_no_class(
    path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded(path)
    odd = type("not an identifier", (RuntimeError,), {})

    def fail(source: Path, destination: Path) -> None:
        raise odd(f"{source} {destination}")

    monkeypatch.setattr("dromon.eventlog.snapshot", fail)
    with EventLog(path, snapshots=True) as log:
        restored = log.restored
        assert restored is not None
    found = verify_restored(path, tmp_path / "copy.sqlite", restored)
    assert found == (RestoreMismatch.CHECK_FAILED, None)


def test_no_check_is_pending_where_no_snapshot_was_used(path: Path) -> None:
    with EventLog(path, snapshots=True) as log:
        log.append(OperatorRegistered("ana"))
        assert (log.start.verified, log.restored) == (VERIFY_NONE, None)
        with pytest.raises(ValueError, match="no check of a restored state"):
            log.restore_checked(None)
    with EventLog(path) as log:
        assert log.start.verified == VERIFY_NONE


# What a mismatch does to the log.


def test_no_snapshot_is_written_while_the_check_is_pending(path: Path) -> None:
    seeded(path)
    snapshot = statesnapshot.snapshot_path(path)
    before = snapshot.read_bytes()
    with EventLog(path, snapshots=True) as log:
        log.append(OperatorRegistered("ben"))
        log.write_snapshot()
        assert snapshot.read_bytes() == before
    # The stop wrote none either, so the next start checks the same file again.
    assert snapshot.read_bytes() == before
    with EventLog(path, snapshots=True) as log:
        assert log.start.verified == VERIFY_PENDING
        assert log.start.replayed == 1


def test_a_mismatch_deletes_the_snapshot_and_requires_a_restart(path: Path) -> None:
    seeded(path)
    forge(path, state=intruded(path))
    snapshot = statesnapshot.snapshot_path(path)
    with EventLog(path, snapshots=True) as log:
        log.restore_checked((RestoreMismatch.STATE_DIFFERS, None))
        assert not snapshot.exists()
        assert log.restore_failed and log.restored is None
        wire = log.start.wire()
        assert (wire["verified"], wire["mismatch"], wire["mismatch_class"]) == (
            VERIFY_FAILED,
            "state_differs",
            None,
        )
        assert wire["restart_required"] is True
        log.append(OperatorRegistered("ben"))
        log.write_snapshot()
        assert not snapshot.exists()
        with pytest.raises(ValueError, match="no check"):
            log.restore_checked(None)
    # Nor does the stop write one, and the restart replays the whole log, which
    # holds no intruder.
    assert not snapshot.exists()
    with EventLog(path, snapshots=True) as log:
        assert log.start.snapshot == "ignored"
        assert "intruder" not in log.state.operators
        assert log.start.wire()["restart_required"] is False


def test_a_snapshot_that_cannot_be_deleted_is_recorded(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded(path)

    def refuse(self: Path, missing_ok: bool = False) -> None:
        del self, missing_ok
        raise PermissionError("no")

    with EventLog(path, snapshots=True) as log:
        monkeypatch.setattr(Path, "unlink", refuse)
        log.restore_checked((RestoreMismatch.CHECK_FAILED, "OSError"))
        monkeypatch.undo()
        assert log.start.write_failure == (
            "the snapshot could not be deleted: PermissionError"
        )
        assert log.start.wire()["mismatch_class"] == "OSError"
        assert log.restore_failed


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start here, but {spec.line_id} did")


def test_no_line_starts_a_turn_until_the_check_passes(path: Path) -> None:
    seeded(path)
    with EventLog(path, snapshots=True) as log:
        scheduler = Scheduler(log, no_engine, tick_seconds=0)
        scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)
        eligible = scheduler._eligible  # pyright: ignore[reportPrivateUsage]
        # Re-review of 2cede6e. The check is pending, so neither line may start.
        assert log.restore_unconfirmed and not log.restore_failed
        assert not eligible("alpha") and not eligible("beta")
        scheduler.restore_checked(None)
        assert not log.restore_unconfirmed
        assert eligible("alpha") and eligible("beta")


def test_a_start_without_a_snapshot_waits_for_no_check(path: Path) -> None:
    with EventLog(path) as log:
        log.append(LineCreated("alpha", HAIKU, "subscription"))
        scheduler = Scheduler(log, no_engine, tick_seconds=0)
        assert not log.restore_unconfirmed
        assert scheduler._eligible("alpha")  # pyright: ignore[reportPrivateUsage]


def test_a_failed_check_holds_every_line_in_memory(path: Path) -> None:
    seeded(path)
    with EventLog(path, snapshots=True) as log:
        scheduler = Scheduler(log, no_engine, tick_seconds=0)
        scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)
        eligible = scheduler._eligible  # pyright: ignore[reportPrivateUsage]
        scheduler.restore_checked((RestoreMismatch.STATE_DIFFERS, None))
        assert log.restore_unconfirmed and log.restore_failed
        assert not eligible("alpha") and not eligible("beta")
        # The hold is in memory alone, so no line is recorded as held.
        assert all(line.held_by is None for line in log.state.lines.values())
        # A turn the engine starts by itself is admitted only where this holds.
        assert not scheduler._can_start("alpha")  # pyright: ignore[reportPrivateUsage]


# The daemon's check after a start.


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    found.runtime_dir.mkdir(mode=0o700)
    return found


def checked(
    paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> tuple[tuple[RestoreMismatch, str | None] | None, dict[str, Any], str]:
    with EventLog(paths.event_log, snapshots=True) as log:
        daemon = Daemon(
            paths,
            log,
            "2026-11-05T00:00:00Z",
            Scheduler(log, no_engine, hold_wakes=True),
        )
        capsys.readouterr()
        found = asyncio.run(daemon.verify_restore())
        status = daemon.status()
    return found, status, capsys.readouterr().err


def test_the_daemon_confirms_a_snapshot_that_agrees(
    paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    seeded(paths.event_log)
    found, status, said = checked(paths, capsys)
    assert found is None
    assert status["event_log"]["start"]["verified"] == VERIFY_PASSED
    assert "agrees with a full replay" in said
    # The copy of the log was removed with its directory.
    assert list(paths.runtime_dir.iterdir()) == []


def test_the_daemon_holds_every_line_and_says_so_on_a_mismatch(
    paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    seeded(paths.event_log)
    forge(paths.event_log, state=intruded(paths.event_log))
    found, status, said = checked(paths, capsys)
    assert found == (RestoreMismatch.STATE_DIFFERS, None)
    start = status["event_log"]["start"]
    assert (start["mismatch"], start["restart_required"]) == ("state_differs", True)
    assert "mismatch state_differs" in said and "must be restarted" in said
    assert not statesnapshot.snapshot_path(paths.event_log).exists()
    assert list(paths.runtime_dir.iterdir()) == []
    row = cli._restore_row(start)  # pyright: ignore[reportPrivateUsage]
    assert row is not None and row.startswith("RESTART REQUIRED.")
    assert "mismatch state_differs" in row and "class" not in row


def test_the_daemon_fails_closed_when_it_cannot_make_its_scratch(
    paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    seeded(paths.event_log)
    paths.runtime_dir.rmdir()
    found, status, said = checked(paths, capsys)
    assert found == (RestoreMismatch.CHECK_FAILED, "LogError")
    assert status["event_log"]["start"]["restart_required"] is True
    assert "mismatch check_failed, class LogError" in said


class Recorder:
    """An engine that records each prompt it is given and ends the turn at once."""

    def __init__(self, spec: LineSpec, prompts: list[str]) -> None:
        self.session_id = spec.session_id
        self.prompts = prompts

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        return TurnReport("success", 1, "ok", (), None, None, "session-1", None, 0.0)


@pytest.mark.parametrize("forged", [False, True])
def test_mail_waits_while_the_check_runs_and_a_turn_starts_only_after_it(
    paths: ProjectPaths,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    forged: bool,
) -> None:
    """**Re-review of 2cede6e.** The check is held open in its thread while the
    scheduler runs with mail waiting for ``alpha``, which is the window the
    re-review found. No turn starts in it. After a pass the waiting turn starts, and
    after a mismatch none ever does, so no turn is running when the check fails."""
    seeded(paths.event_log)
    if forged:
        forge(paths.event_log, state=intruded(paths.event_log))
    entered = threading.Event()
    release = threading.Event()
    real = daemon_module.verify_restored

    def held(
        log_path: Path, copy_path: Path, restored: Restored
    ) -> tuple[RestoreMismatch, str | None] | None:
        entered.set()
        assert release.wait(10), "the test never released the check"
        return real(log_path, copy_path, restored)

    monkeypatch.setattr(daemon_module, "verify_restored", held)
    prompts: list[str] = []

    def build(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
        del tools
        return Recorder(spec, prompts)

    async def scenario(log: EventLog) -> tuple[list[str], bool]:
        scheduler = Scheduler(log, build, tick_seconds=0)
        daemon = Daemon(paths, log, "2026-11-05T00:00:00Z", scheduler)
        stop = asyncio.Event()
        running = asyncio.create_task(scheduler.run(stop))
        checking = asyncio.create_task(daemon.verify_restore())
        assert await asyncio.to_thread(entered.wait, 10)
        # The scheduler has had many passes with the mail deliverable.
        for _ in range(50):
            await asyncio.sleep(0)
        await scheduler.idle()
        during = list(prompts)
        waiting = bool(scheduler.deliverable("alpha"))
        release.set()
        await checking
        await scheduler.idle()
        stop.set()
        await running
        assert during == [] and waiting
        return prompts, log.restore_failed

    with EventLog(paths.event_log, snapshots=True) as log:
        assert log.start.verified == VERIFY_PENDING
        found, failed = asyncio.run(scenario(log))
    capsys.readouterr()
    assert failed is forged
    assert len(found) == (0 if forged else 1)


def test_the_daemon_checks_nothing_without_a_restored_snapshot(
    paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    with EventLog(paths.event_log) as log:
        log.append(OperatorRegistered("ana"))
    found, status, said = checked(paths, capsys)
    assert found is None and said == ""
    assert status["event_log"]["start"]["verified"] == VERIFY_NONE


@pytest.mark.parametrize(
    "start",
    [
        None,
        "text",
        {"restart_required": False},
        {"restart_required": "yes"},
    ],
)
def test_the_status_row_shows_only_a_required_restart(start: object) -> None:
    assert cli._restore_row(start) is None  # pyright: ignore[reportPrivateUsage]


def test_the_status_row_names_the_class_where_one_is_given() -> None:
    row = cli._restore_row(  # pyright: ignore[reportPrivateUsage]
        {
            "restart_required": True,
            "snapshot_seq": 7,
            "mismatch": "check_failed",
            "mismatch_class": "OSError",
        }
    )
    assert row is not None
    assert "snapshot at event 7" in row and "check_failed, class OSError" in row


# Item 2, the directory is synchronized after the move.


def test_the_directory_is_synchronized_after_the_snapshot_moves_into_place(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    real_replace = os.replace
    real_fsync = os.fsync

    def replace(source: str | Path, target: str | Path) -> None:
        calls.append("replace")
        real_replace(source, target)

    def fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        calls.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "fsync", fsync)
    seeded(path)
    assert calls == ["file", "replace", "directory"]


def test_a_directory_that_cannot_be_synchronized_is_a_recorded_write_failure(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(directory: Path) -> None:
        raise OSError(f"cannot synchronize {directory.name}")

    monkeypatch.setattr(statesnapshot, "sync_directory", refuse)
    with EventLog(path, snapshots=True) as log:
        log.append(OperatorRegistered("ana"))
        log.write_snapshot()
        assert log.start.write_failure is not None
        assert log.start.write_failure.startswith("OSError")
        assert log.start.snapshot_seq == 0


def test_sync_directory_refuses_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(OSError, match="No such file"):
        statesnapshot.sync_directory(tmp_path / "absent")
    statesnapshot.sync_directory(tmp_path)
