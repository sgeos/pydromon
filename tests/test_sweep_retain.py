"""A launch record whose harness's start time cannot be read is kept. **Round 19.**

Round 18's audit found that a start time that did not read removed the record, so a live
harness whose launcher had gone was never looked at again. The record is now kept for a
bounded number of sweeps, the count is written into it, and nothing is signalled on a
time that does not read. A time that reads and disagrees is another process, and a
record without one can never be read, so neither is kept. The processes are only rows of
a table here, since the decision under test is the sweep's and not the kernel's.
"""

import asyncio
import json
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from dromon import launcher
from dromon.launcher import MAX_START_RETRIES, RETRY_FIELD, StopPolicy
from dromon.processes import TreeRow

STARTED = 5.0
HARNESS = 4343
ROWS = [
    TreeRow(HARNESS, 1, HARNESS, "sleep 60"),
    TreeRow(HARNESS + 1, HARNESS, HARNESS, "sleep 61"),
]


def write(
    directory: Path,
    *,
    harness_started: object = STARTED,
    retries: object = None,
    policy: StopPolicy = StopPolicy.KILL_ALL,
) -> Path:
    document: dict[str, object] = {
        "v": 1,
        "mark": "line.token",
        "launcher": 4242,
        "harness": HARNESS,
        "command": "sleep",
        "policy": policy.value,
        "done": False,
    }
    if harness_started is not None:
        document["harness_started"] = harness_started
    if retries is not None:
        document[RETRY_FIELD] = retries
    path = directory / f"4242-{time.time_ns()}{launcher.LAUNCH_SUFFIX}"
    path.write_text(json.dumps(document))
    return path


def sweep(directory: Path, rows: Sequence[TreeRow] = ROWS) -> list[launcher.Swept]:
    outcome = asyncio.run(launcher.sweep_launches(directory, lambda: list(rows)))
    assert isinstance(outcome, list)
    return outcome


class Clock:
    """The start time ``start_time`` reads, which a test changes between sweeps, and
    every signal the sweep tries, which a test reads."""

    def __init__(self) -> None:
        self.now: float | None = None
        self.gone = False
        self.sent: list[tuple[str, int, int]] = []

    def read(self, pid: int) -> float | None:
        del pid
        return self.now

    def is_gone(self, pid: int) -> bool:
        del pid
        return self.gone

    def group(
        self, group: int, number: int, started: float, *, leader_defunct: bool = False
    ) -> bool:
        del started, leader_defunct
        self.sent.append(("group", group, number))
        return False

    def one(self, pid: int, number: int, started: float) -> bool:
        del started
        self.sent.append(("one", pid, number))
        return True

    def send(self, pid: int, number: int) -> None:
        self.sent.append(("pid", pid, number))


def dead(pid: int) -> bool:
    """``launcher._alive`` for these tests, which name no real process."""
    del pid
    return False


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    reading = Clock()
    monkeypatch.setattr(launcher, "start_time", reading.read)
    monkeypatch.setattr(launcher, "signal_group_if_started", reading.group)
    monkeypatch.setattr(launcher, "signal_if_started", reading.one)
    monkeypatch.setattr(launcher, "_send", reading.send)
    monkeypatch.setattr(launcher, "process_gone", reading.is_gone)
    monkeypatch.setattr(launcher, "_alive", dead)
    monkeypatch.setattr(launcher, "CHECK_SECONDS", 0.001)
    return reading


def counted(path: Path) -> object:
    return json.loads(path.read_text()).get(RETRY_FIELD)


def test_an_unreadable_time_keeps_the_record_and_counts_the_sweep(
    tmp_path: Path, clock: Clock
) -> None:
    path = write(tmp_path)
    [first] = sweep(tmp_path)
    assert first.skipped is not None
    assert first.skipped.startswith(launcher.UNREADABLE_START)
    assert f"attempt 1 of {MAX_START_RETRIES}" in first.skipped
    assert first.ended == ()
    assert path.exists() and counted(path) == 1
    [second] = sweep(tmp_path)
    assert second.skipped is not None and "attempt 2 of" in second.skipped
    assert counted(path) == 2
    assert clock.sent == []


def test_the_retries_are_bounded_and_nothing_is_ever_signalled(
    tmp_path: Path, clock: Clock
) -> None:
    path = write(tmp_path)
    kept = 0
    found: list[launcher.Swept] = []
    for _ in range(MAX_START_RETRIES + 3):
        found = sweep(tmp_path)
        if not path.exists():
            break
        kept += 1
        assert len(found) == 1
    last = found[0]
    assert kept == MAX_START_RETRIES
    assert not path.exists()
    assert last.skipped is not None
    assert "the record is removed after" in last.skipped
    assert str(MAX_START_RETRIES) in last.skipped
    assert sweep(tmp_path) == []
    assert clock.sent == []


def test_a_time_that_reads_later_is_swept_and_the_record_removed(
    tmp_path: Path, clock: Clock
) -> None:
    """The transient failure the audit named. The first sweep cannot read the time and
    keeps the record, and the second reads it, finds the harness and ends its group."""
    path = write(tmp_path)
    [kept] = sweep(tmp_path)
    assert kept.skipped is not None and path.exists()
    clock.now = STARTED
    [done] = sweep(tmp_path)
    assert done.skipped is None
    assert set(done.ended) == {HARNESS, HARNESS + 1}
    assert not path.exists()
    assert ("group", HARNESS, 15) in clock.sent


def test_a_process_the_system_no_longer_has_is_not_an_unreadable_time(
    tmp_path: Path, clock: Clock
) -> None:
    """A table read a moment ago can list a harness that has since gone, and nothing
    can be read from it. There is nothing to wait for, so the record is swept as one
    whose leader is gone, and removed, and the group's remaining members are ended."""
    clock.gone = True
    path = write(tmp_path)
    [found] = sweep(tmp_path)
    assert found.skipped is None
    assert not path.exists()
    assert ("group", HARNESS, 15) in clock.sent


def test_a_time_that_reads_and_disagrees_removes_the_record_at_once(
    tmp_path: Path, clock: Clock
) -> None:
    clock.now = STARTED + 1000
    path = write(tmp_path)
    [found] = sweep(tmp_path)
    assert found.skipped == "another process now holds the harness's pid"
    assert not path.exists()
    assert clock.sent == []


def test_a_record_without_a_start_time_is_removed_at_once(
    tmp_path: Path, clock: Clock
) -> None:
    """It can never be read, so keeping it would only wait."""
    path = write(tmp_path, harness_started=None)
    [found] = sweep(tmp_path)
    assert found.skipped == launcher.NO_START_TIME
    assert not path.exists()
    assert clock.sent == []


def test_the_count_is_written_whole_beside_the_rest_of_the_record(
    tmp_path: Path, clock: Clock
) -> None:
    del clock
    path = write(tmp_path)
    before = json.loads(path.read_text())
    sweep(tmp_path)
    after = json.loads(path.read_text())
    assert after == {**before, RETRY_FIELD: 1}
    assert not list(tmp_path.glob("*.tmp"))
    assert path.stat().st_mode & 0o077 == 0, "the rewritten record is the owner's alone"
    [record] = launcher.read_launches(tmp_path)
    assert record.start_retries == 1 and record.harness_started == STARTED


def test_a_record_with_no_count_reads_as_none_and_a_count_survives_a_restart(
    tmp_path: Path, clock: Clock
) -> None:
    del clock
    write(tmp_path)
    [fresh] = launcher.read_launches(tmp_path)
    assert fresh.start_retries == 0
    for path in tmp_path.iterdir():
        path.unlink()
    path = write(tmp_path, retries=MAX_START_RETRIES - 1)
    [found] = sweep(tmp_path)
    assert found.skipped is not None
    assert f"attempt {MAX_START_RETRIES} of" in found.skipped
    assert path.exists()
    [last] = sweep(tmp_path)
    assert last.skipped is not None and "removed after" in last.skipped
    assert not path.exists()


@pytest.mark.parametrize(
    "damaged", [-1, MAX_START_RETRIES + 1, 10**9, "3", 1.5, True, [1], {"n": 1}]
)
def test_a_damaged_count_can_only_shorten_the_retries(
    tmp_path: Path, clock: Clock, damaged: object
) -> None:
    path = write(tmp_path, retries=damaged)
    [record] = launcher.read_launches(tmp_path)
    assert record.start_retries == MAX_START_RETRIES
    [found] = sweep(tmp_path)
    assert found.skipped is not None and "removed after" in found.skipped
    assert not path.exists()
    assert clock.sent == []


def test_a_count_that_cannot_be_written_removes_the_record(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A count that never advanced would never bound the retries."""

    def refuse(source: object, target: object) -> None:
        raise PermissionError("read only")

    monkeypatch.setattr(launcher.os, "replace", refuse)
    path = write(tmp_path)
    [found] = sweep(tmp_path)
    assert found.skipped is not None and "removed after 0 kept sweeps" in found.skipped
    assert not path.exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert clock.sent == []


@pytest.mark.parametrize(
    "content", [b"not json", b"[1, 2]", b"x" * (launcher.MAX_LAUNCH_BYTES + 1)]
)
def test_a_record_that_changed_under_the_sweep_is_not_kept(
    tmp_path: Path, clock: Clock, content: bytes
) -> None:
    """Between the read and the write the file is read again, and one that no longer
    parses, or has grown past the bound, is removed and not rewritten."""
    path = write(tmp_path)
    [record] = launcher.read_launches(tmp_path)
    path.write_bytes(content)
    assert launcher._write_retries(record, 1) is False  # pyright: ignore[reportPrivateUsage]
    assert path.read_bytes() == content
    assert not list(tmp_path.glob("*.tmp"))
    assert clock.sent == []


def test_a_running_launcher_keeps_its_record_without_a_count(
    tmp_path: Path, clock: Clock
) -> None:
    """The launcher's own record is not the sweep's to count while it still runs."""
    del clock
    rows = [
        TreeRow(4242, 1, 4242, "python -I -m dromon.launcher"),
        TreeRow(HARNESS, 4242, HARNESS, "sleep 60"),
    ]
    path = write(tmp_path)
    assert sweep(tmp_path, rows) == []
    assert counted(path) is None


def test_two_records_are_counted_apart(tmp_path: Path, clock: Clock) -> None:
    del clock
    first = write(tmp_path)
    time.sleep(0.001)
    second = write(tmp_path, retries=4)
    sweep(tmp_path)
    assert counted(first) == 1
    assert counted(second) == 5


def planted_name(path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fix the random part of the temporary name, so a test can plant the name."""

    def fixed(size: int) -> str:
        return "ab" * size

    monkeypatch.setattr(launcher.secrets, "token_hex", fixed)
    return path.with_name(f"{path.name}.{launcher.os.getpid()}.{'ab' * 8}.tmp")


def test_a_planted_link_at_the_temporary_name_is_not_followed(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round 19, audit. The temporary file is opened with ``O_EXCL`` and ``O_NOFOLLOW``,
    so a link in its place is refused and the file it points to is not written. The
    link is not ours, since this call did not create it, so it is left."""
    path = write(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("precious")
    link = planted_name(path, monkeypatch)
    link.symlink_to(victim)
    [record] = launcher.read_launches(tmp_path)
    assert launcher._write_retries(record, 1) is False  # pyright: ignore[reportPrivateUsage]
    assert victim.read_text() == "precious"
    assert link.is_symlink()
    assert counted(path) is None
    [found] = sweep(tmp_path)
    assert found.skipped is not None and "removed after" in found.skipped
    assert victim.read_text() == "precious"
    assert clock.sent == []


def test_a_file_already_at_the_temporary_name_is_not_truncated_or_removed(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clock
    path = write(tmp_path)
    planted = planted_name(path, monkeypatch)
    planted.write_text("someone else's")
    [record] = launcher.read_launches(tmp_path)
    assert launcher._write_retries(record, 1) is False  # pyright: ignore[reportPrivateUsage]
    assert planted.read_text() == "someone else's"


def test_the_temporary_names_differ_and_the_data_is_flushed_before_the_replace(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clock
    order: list[str] = []
    names: list[str] = []
    real_replace = launcher.os.replace
    real_fsync = launcher.os.fsync

    def fsync(descriptor: int) -> None:
        order.append("fsync")
        real_fsync(descriptor)

    def replace(source: object, target: object) -> None:
        order.append("replace")
        names.append(Path(str(source)).name)
        real_replace(str(source), str(target))

    monkeypatch.setattr(launcher.os, "fsync", fsync)
    monkeypatch.setattr(launcher.os, "replace", replace)
    path = write(tmp_path)
    sweep(tmp_path)
    sweep(tmp_path)
    assert order == ["fsync", "replace", "fsync", "replace"]
    assert len(set(names)) == 2 and all(name.endswith(".tmp") for name in names)
    assert counted(path) == 2
    assert not list(tmp_path.glob("*.tmp"))


def test_a_temporary_file_that_was_created_is_removed_when_the_flush_fails(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    del clock

    def refuse(descriptor: int) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(launcher.os, "fsync", refuse)
    path = write(tmp_path)
    [found] = sweep(tmp_path)
    assert found.skipped is not None and "removed after" in found.skipped
    assert not path.exists()
    assert not list(tmp_path.glob("*.tmp"))
