"""The SQLite event log, its replay, and the ways it refuses to be misused."""

import re
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest

from dromon.codec import encode
from dromon.eventlog import (
    SCHEMA_VERSION,
    EventLog,
    LogConflict,
    LogCorrupt,
    LogError,
    read_entries,
    replay,
    utc_now,
)
from dromon.model import (
    Event,
    EventRejected,
    LineCreated,
    LineState,
    LineStateChanged,
    State,
)

ENGINE = "claude-opus-5-5"


def counting_clock() -> Iterator[str]:
    """A deterministic clock, so recorded times are predictable in tests."""
    tick = 0
    while True:
        tick += 1
        yield f"2026-10-25T00:00:{tick:02}Z"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "events.sqlite"


def write(path: Path, events: tuple[Event, ...]) -> State:
    clock = counting_clock()
    with EventLog(path, clock=lambda: next(clock)) as log:
        for event in events:
            log.append(event)
        return log.state


def raw(path: Path) -> closing[sqlite3.Connection]:
    """A direct connection in autocommit mode, closed when its block ends."""
    return closing(sqlite3.connect(path, isolation_level=None))


# Replay rebuilds the same state.


def test_replay_rebuilds_the_state_built_in_memory(
    path: Path, scenario: tuple[Event, ...]
) -> None:
    in_memory = State()
    for event in scenario:
        in_memory.apply(event)
    written = write(path, scenario)
    assert written == in_memory
    assert replay(path) == in_memory


def test_reopening_a_log_restores_its_state_and_sequence(
    path: Path, scenario: tuple[Event, ...]
) -> None:
    written = write(path, scenario)
    with EventLog(path) as log:
        assert log.state == written
        assert log.last_seq == len(scenario)


def test_a_reopened_log_accepts_further_events(
    path: Path, scenario: tuple[Event, ...]
) -> None:
    write(path, scenario)
    with EventLog(path) as log:
        entry = log.append(LineCreated("gamma", ENGINE))
    assert entry.seq == len(scenario) + 1
    assert "gamma" in replay(path).lines


def test_entries_keep_their_order_events_and_times(
    path: Path, scenario: tuple[Event, ...]
) -> None:
    write(path, scenario)
    entries = read_entries(path)
    assert [entry.event for entry in entries] == list(scenario)
    assert [entry.seq for entry in entries] == list(range(1, len(scenario) + 1))
    expected_times = counting_clock()
    assert [entry.recorded_at for entry in entries] == [
        next(expected_times) for _ in scenario
    ]


def test_the_stored_payload_is_the_canonical_encoding(
    path: Path, scenario: tuple[Event, ...]
) -> None:
    write(path, scenario)
    with raw(path) as connection:
        payloads = [row[0] for row in connection.execute("SELECT payload FROM events")]
    assert payloads == [encode(event) for event in scenario]


def test_an_empty_log_replays_to_an_empty_state(path: Path) -> None:
    with EventLog(path) as log:
        assert log.last_seq == 0
    assert replay(path) == State()
    assert read_entries(path) == []


def test_the_state_property_is_a_snapshot(path: Path) -> None:
    with EventLog(path) as log:
        snapshot = log.state
        snapshot.lines.clear()
        log.append(LineCreated("alpha", ENGINE))
        assert "alpha" not in snapshot.lines
        assert "alpha" in log.state.lines


def test_a_new_log_is_private_to_its_owner(path: Path) -> None:
    write(path, (LineCreated("alpha", ENGINE),))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_an_existing_file_keeps_its_mode(path: Path) -> None:
    path.touch(mode=0o640)
    path.chmod(0o640)
    write(path, ())
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_utc_now_is_iso_8601_in_utc() -> None:
    pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z"
    assert re.fullmatch(pattern, utc_now())


# Appends that must not be stored.


def test_a_rejected_event_is_not_stored(path: Path) -> None:
    with EventLog(path) as log:
        log.append(LineCreated("alpha", ENGINE))
        with pytest.raises(EventRejected):
            log.append(LineCreated("alpha", ENGINE))
        with pytest.raises(EventRejected):
            log.append(LineStateChanged("alpha", LineState.IDLE))
        assert log.last_seq == 1
    assert len(read_entries(path)) == 1


def test_an_empty_timestamp_is_refused_and_nothing_is_stored(path: Path) -> None:
    with EventLog(path, clock=lambda: "") as log:
        with pytest.raises(LogError, match="timestamp"):
            log.append(LineCreated("alpha", ENGINE))
        assert log.state == State()
    assert read_entries(path) == []


def test_a_second_writer_is_detected(path: Path) -> None:
    with EventLog(path) as first, EventLog(path) as second:
        first.append(LineCreated("alpha", ENGINE))
        with pytest.raises(LogConflict, match="at 1"):
            second.append(LineCreated("beta", ENGINE))
        assert second.state == State()
        assert second.last_seq == 0
    assert [e.event for e in read_entries(path)] == [LineCreated("alpha", ENGINE)]


# The log is append-only.


def test_rows_cannot_be_updated(path: Path, scenario: tuple[Event, ...]) -> None:
    write(path, scenario)
    with raw(path) as connection, pytest.raises(sqlite3.IntegrityError, match="append"):
        connection.execute("UPDATE events SET payload = '{}' WHERE seq = 1")
    assert replay(path) == write(path.with_name("copy.sqlite"), scenario)


def test_rows_cannot_be_deleted(path: Path, scenario: tuple[Event, ...]) -> None:
    write(path, scenario)
    with raw(path) as connection, pytest.raises(sqlite3.IntegrityError, match="append"):
        connection.execute("DELETE FROM events")
    assert len(read_entries(path)) == len(scenario)


# Files that are not usable logs.


def test_a_foreign_database_is_refused(path: Path) -> None:
    with raw(path) as connection:
        connection.execute("CREATE TABLE notes (text TEXT)")
    with pytest.raises(LogError, match="not a Dromon event log"):
        EventLog(path)


def test_an_unsupported_schema_version_is_refused(path: Path) -> None:
    write(path, ())
    with raw(path) as connection:
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(LogError, match="unsupported schema version"):
        EventLog(path)
    with pytest.raises(LogError, match="unsupported schema version"):
        replay(path)


def test_replaying_a_missing_file_is_an_error_and_creates_nothing(path: Path) -> None:
    with pytest.raises(LogError, match="no event log"):
        replay(path)
    assert not path.exists()


def test_replay_never_writes(path: Path, scenario: tuple[Event, ...]) -> None:
    write(path, scenario)
    before = path.read_bytes()
    replay(path)
    read_entries(path)
    assert path.read_bytes() == before


# Corruption is reported with its sequence number, never skipped.


def insert(path: Path, payload: str, seq: int | None = None) -> None:
    with raw(path) as connection:
        if seq is None:
            connection.execute(
                "INSERT INTO events (recorded_at, payload) VALUES ('t', ?)", (payload,)
            )
        else:
            connection.execute(
                "INSERT INTO events (seq, recorded_at, payload) VALUES (?, 't', ?)",
                (seq, payload),
            )


@pytest.mark.parametrize("reader", [replay, read_entries, EventLog])
def test_a_payload_that_does_not_decode_is_corruption(
    path: Path, reader: Callable[[Path], object], scenario: tuple[Event, ...]
) -> None:
    write(path, scenario)
    insert(path, '{"v": 1}')
    with pytest.raises(LogCorrupt, match=f"event {len(scenario) + 1} does not decode"):
        reader(path)


def test_an_event_that_does_not_apply_is_corruption(path: Path) -> None:
    write(path, (LineCreated("alpha", ENGINE),))
    insert(path, encode(LineCreated("alpha", ENGINE)))
    with pytest.raises(LogCorrupt, match="event 2 does not apply"):
        replay(path)


def test_a_gap_in_the_sequence_is_corruption(path: Path) -> None:
    write(path, (LineCreated("alpha", ENGINE),))
    insert(path, encode(LineCreated("beta", ENGINE)), seq=5)
    with pytest.raises(LogCorrupt, match="skips from 1 to 5"):
        replay(path)
