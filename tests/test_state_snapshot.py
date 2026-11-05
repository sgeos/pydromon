"""The snapshot a start resumes from. **Round 15, item 4, required by the operator.**

The state is restored from ``events.sqlite.snapshot`` when it can be trusted, and the
log is replayed whole when it cannot, with the reason. Each test compares the state a
start builds with the state a full replay builds, through their canonical encodings,
so the order of every ordered map is compared too."""

import os
import shutil
import sqlite3
from datetime import datetime
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from dromon import statesnapshot
from dromon.daemon import report_start
from dromon.eventlog import EventLog, check_snapshot
from dromon.model import (
    Event,
    LineCreated,
    LineState,
    MessageDelivered,
    MessageSent,
    MessageKind,
    LineAddress,
    OperatorAddress,
    OperatorRegistered,
    State,
)
from dromon.statesnapshot import SnapshotRefused, decode_value, encode_value

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")


def encoded(log: EventLog) -> bytes:
    return statesnapshot.canonical(
        statesnapshot.encode_state(log.state, log.times.maps())
    )


def full(path: Path) -> bytes:
    """The encoding a full replay of the log at ``path`` gives, read from a copy so
    that no snapshot is read or written beside the log itself."""
    copy = path.with_name("full-" + path.name)
    shutil.copyfile(path, copy)
    with EventLog(copy) as log:
        return encoded(log)


def events(count: int, start: int = 0) -> Iterator[Event]:
    for number in range(start, start + count):
        yield MessageSent(f"m-{number}", ANA, ALPHA, MessageKind.NOTE, f"note {number}")
        yield MessageDelivered(f"m-{number}")


def seeded(path: Path, count: int = 3) -> None:
    with EventLog(path, snapshots=True) as log:
        log.append(OperatorRegistered("ana"))
        log.append(LineCreated("alpha", HAIKU, "subscription"))
        for event in events(count):
            log.append(event)


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "events.sqlite"


# The encoding.


def test_the_scenario_state_round_trips_with_its_order(
    scenario: tuple[Event, ...],
) -> None:
    state = State()
    for event in scenario:
        state.apply(event)
    data = encode_value(state, State)
    back = decode_value(data, State)
    assert back == state
    assert statesnapshot.canonical(encode_value(back, State)) == (
        statesnapshot.canonical(data)
    )
    assert isinstance(back, State)
    assert list(back.messages) == list(state.messages)


@pytest.mark.parametrize(
    ("data", "hint", "reason"),
    [
        ({"engine": "x"}, LineCreated, "other fields"),
        ([["a", 1], ["a", 2]], dict[str, int], "names a key twice"),
        (["a", "a"], set[str], "names an item twice"),
        ("nope", MessageKind, "is not a MessageKind"),
        (1, str, "is not a str"),
        (True, int, "is not a int"),
        ({"engine": "", "line_id": "!", "strategy": "x"}, LineCreated, "is refused"),
        (
            {"type": "Nothing", "value": {}},
            LineAddress | OperatorAddress,
            "not a record",
        ),
        ("2026-11-05T03:00:00", datetime, "not aware"),
    ],
)
def test_the_decoder_refuses_any_other_form(
    data: statesnapshot.Json, hint: object, reason: str
) -> None:
    with pytest.raises(SnapshotRefused, match=reason):
        decode_value(data, hint)


def test_the_shape_digest_is_stable() -> None:
    assert statesnapshot.shape_digest() == statesnapshot.shape_digest()
    assert len(statesnapshot.shape_digest()) == 64


# A start.


def test_a_clean_stop_writes_a_private_snapshot_that_the_next_start_uses(
    path: Path,
) -> None:
    seeded(path)
    snapshot = statesnapshot.snapshot_path(path)
    assert snapshot.stat().st_mode & 0o777 == 0o600
    with EventLog(path, snapshots=True) as log:
        assert log.start.snapshot == "used"
        assert (log.start.snapshot_seq, log.start.replayed) == (log.last_seq, 0)
        assert log.start.reason is None
        restored = encoded(log)
    assert restored == full(path)


def test_events_after_the_snapshot_are_replayed_on_it(path: Path) -> None:
    seeded(path)
    with EventLog(path) as log:
        for event in events(2, start=10):
            log.append(event)
    with EventLog(path, snapshots=True) as log:
        assert log.start.snapshot == "used" and log.start.replayed == 4
        restored = encoded(log)
        unanswered = log.times.unanswered_since("m-0")
    assert restored == full(path)
    assert unanswered is not None


def test_without_snapshots_nothing_is_read_or_written(path: Path) -> None:
    with EventLog(path) as log:
        log.append(OperatorRegistered("ana"))
        assert log.start.snapshot == "off"
    assert not statesnapshot.snapshot_path(path).exists()


def reopened(path: Path) -> EventLog:
    log = EventLog(path, snapshots=True)
    assert log.start.snapshot == "ignored"
    assert encoded(log) == full(path)
    return log


def test_a_missing_snapshot_replays_the_whole_log(path: Path) -> None:
    with EventLog(path) as log:
        log.append(OperatorRegistered("ana"))
    log = reopened(path)
    try:
        assert log.start.reason == "there is no snapshot"
    finally:
        log.close()


def corrupt(path: Path) -> None:
    snapshot = statesnapshot.snapshot_path(path)
    raw = snapshot.read_bytes().replace(b'"note 0"', b'"note X"')
    snapshot.write_bytes(raw)


def loosen(path: Path) -> None:
    os.chmod(statesnapshot.snapshot_path(path), 0o644)


def replace_with_link(path: Path) -> None:
    snapshot = statesnapshot.snapshot_path(path)
    target = path.with_name("elsewhere")
    snapshot.rename(target)
    snapshot.symlink_to(target)


def replace_with_directory(path: Path) -> None:
    snapshot = statesnapshot.snapshot_path(path)
    snapshot.unlink()
    snapshot.mkdir()


def garble(path: Path) -> None:
    snapshot = statesnapshot.snapshot_path(path)
    snapshot.write_bytes(b"not json")
    os.chmod(snapshot, 0o600)


@pytest.mark.parametrize(
    ("spoil", "reason"),
    [
        (corrupt, "digest does not verify"),
        (loosen, "readable by others"),
        (replace_with_link, "cannot be opened"),
        (replace_with_directory, "not a regular file"),
        (garble, "not JSON"),
    ],
)
def test_a_snapshot_that_cannot_be_trusted_is_ignored_with_its_reason(
    path: Path, spoil: Callable[[Path], None], reason: str
) -> None:
    seeded(path)
    spoil(path)
    with EventLog(path, snapshots=True) as log:
        assert log.start.snapshot == "ignored"
        assert reason in str(log.start.reason)
        restored = encoded(log)
    assert restored == full(path)


@pytest.mark.parametrize("name", ["STATE_VERSION", "FORMAT_VERSION"])
def test_a_snapshot_of_another_version_is_ignored(
    path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    seeded(path)
    monkeypatch.setattr(statesnapshot, name, getattr(statesnapshot, name) + 1)
    with EventLog(path) as log:
        before = encoded(log)
    with EventLog(path, snapshots=True) as log:
        assert "is not this daemon's" in str(log.start.reason)
        assert encoded(log) == before
        log._snapshots = False  # pyright: ignore[reportPrivateUsage]


def test_a_snapshot_of_another_shape_is_ignored(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded(path)
    monkeypatch.setattr(statesnapshot, "shape_digest", lambda: "0" * 64)
    with EventLog(path, snapshots=True) as log:
        assert log.start.reason == "the snapshot's shape is not this daemon's"
        log._snapshots = False  # pyright: ignore[reportPrivateUsage]


def test_a_snapshot_past_the_logs_end_is_ignored(tmp_path: Path) -> None:
    longer = tmp_path / "longer" / "events.sqlite"
    shorter = tmp_path / "shorter" / "events.sqlite"
    longer.parent.mkdir()
    shorter.parent.mkdir()
    with EventLog(longer) as log:
        log.append(OperatorRegistered("ana"))
    shutil.copyfile(longer, shorter)
    with EventLog(longer, snapshots=True) as log:
        log.append(OperatorRegistered("ben"))
    shutil.copyfile(
        statesnapshot.snapshot_path(longer), statesnapshot.snapshot_path(shorter)
    )
    with EventLog(shorter, snapshots=True) as log:
        assert log.start.reason == "the log has no row 2, so it is another log"
        assert log.state.operators == {"ana"}
        log._snapshots = False  # pyright: ignore[reportPrivateUsage]


def test_a_snapshot_of_another_log_is_ignored(tmp_path: Path) -> None:
    first = tmp_path / "first" / "events.sqlite"
    second = tmp_path / "second" / "events.sqlite"
    for target, name in ((first, "ana"), (second, "ben")):
        target.parent.mkdir()
        with EventLog(target, snapshots=True) as log:
            log.append(OperatorRegistered(name))
    shutil.copyfile(
        statesnapshot.snapshot_path(first), statesnapshot.snapshot_path(second)
    )
    with EventLog(second, snapshots=True) as log:
        assert log.start.reason == "the log's row 1 differs from the snapshot's"
        assert log.state.operators == {"ben"}
        log._snapshots = False  # pyright: ignore[reportPrivateUsage]


# Writing.


def test_a_snapshot_is_written_as_the_log_grows(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(statesnapshot, "SNAPSHOT_EVERY", 3)
    with EventLog(path, snapshots=True) as log:
        for name in ("a", "b", "c", "d"):
            log.append(OperatorRegistered(name))
        assert log.start.snapshot_seq == 3
        log._snapshots = False  # pyright: ignore[reportPrivateUsage]
    with EventLog(path, snapshots=True) as log:
        assert (log.start.snapshot_seq, log.start.replayed) == (3, 1)
        log._snapshots = False  # pyright: ignore[reportPrivateUsage]


def test_a_snapshot_that_cannot_be_written_is_recorded_not_raised(
    path: Path,
) -> None:
    statesnapshot.snapshot_path(path).mkdir()
    with EventLog(path, snapshots=True) as log:
        log.append(OperatorRegistered("ana"))
        log.write_snapshot()
        assert log.start.write_failure is not None
        assert log.start.snapshot_seq == 0
        assert log.start.wire()["write_failure"] == log.start.write_failure


# The check and the start report.


def test_the_check_passes_a_snapshot_that_agrees(path: Path) -> None:
    seeded(path)
    copy = path.with_name("copy.sqlite")
    shutil.copyfile(path, copy)
    agrees, said = check_snapshot(statesnapshot.snapshot_path(path), copy)
    assert agrees and "agrees with a full replay" in said


def test_the_check_passes_without_a_snapshot_and_says_why(path: Path) -> None:
    with EventLog(path) as log:
        log.append(OperatorRegistered("ana"))
    agrees, said = check_snapshot(statesnapshot.snapshot_path(path), path)
    assert agrees and "there is no snapshot" in said


def test_the_check_fails_a_snapshot_that_differs_from_the_replay(path: Path) -> None:
    seeded(path)
    with EventLog(path) as log:
        state = log.state
        times = log.times.maps()
        seq = log.last_seq
    state.operators.add("intruder")
    loaded = statesnapshot.read(
        statesnapshot.snapshot_path(path), 1, lambda number: _rows(path)[number - 1]
    )
    statesnapshot.write(
        statesnapshot.snapshot_path(path),
        schema_version=1,
        seq=seq,
        first_row=loaded.first_row,
        nth_row=loaded.nth_row,
        state=state,
        times=times,
    )
    agrees, said = check_snapshot(statesnapshot.snapshot_path(path), path)
    assert not agrees and "differs from a full replay" in said


def _rows(path: Path) -> list[tuple[str, str]]:
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


def test_a_start_names_how_it_rebuilt_the_state(
    path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with EventLog(path, snapshots=True) as log:
        report_start(log, path, check_only=False)
        log.append(OperatorRegistered("ana"))
    assert "snapshot was ignored: there is no snapshot" in capsys.readouterr().err
    with EventLog(path, snapshots=True) as log:
        report_start(log, path, check_only=False)
        log._snapshots = False  # pyright: ignore[reportPrivateUsage]
    assert "restored the state from the snapshot at event 1" in capsys.readouterr().err
    with EventLog(path) as log:
        report_start(log, path, check_only=True)
    assert "compares the snapshot beside" in capsys.readouterr().err


@pytest.mark.parametrize("hint", [int, LineState, datetime])
def test_a_refusal_names_the_type_and_never_the_value(hint: object) -> None:
    """The reason reaches the daemon status, which lines read, so it never repeats
    a stored value, which may be any text the log holds."""
    with pytest.raises(SnapshotRefused) as caught:
        decode_value("a stored value", hint)
    assert "a stored value" not in str(caught.value)
