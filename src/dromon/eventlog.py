"""The append-only event log, stored in SQLite.

The log is the daemon's only durable state. Opening a log replays every event into a
``State``, and appending an event checks it against that state before it is stored, so
the stored sequence is always one that replays without error. ``replay`` rebuilds the
state from a log file alone, which is how recovery and a newly attached client agree
with the daemon. ``docs/protocol/EVENT_LOG.md`` is normative.

Assumptions and failure modes.

- One process writes a log. An append checks inside its transaction that no other
  writer has appended since this process last read, and raises ``LogConflict`` if one
  has, rather than storing an event checked against stale state.
- Triggers reject ``UPDATE`` and ``DELETE``, so the log is append-only through SQLite.
  Anyone with write access to the file can still alter it, so the triggers guard against
  mistakes, not against an adversary. File permissions are the security boundary.
- A row that fails to decode or to apply makes the whole log unreadable, reported as
  ``LogCorrupt`` with its sequence number. Nothing skips a bad row silently.
- A new log file is created readable and writable by its owner only, since it holds
  message bodies and operator answers. SQLite gives its journal the same mode. An
  existing file keeps its mode, and the directory's permissions are the caller's
  concern.
- ``recorded_at`` is metadata from an injected clock. It is stored but never read by the
  model, so replay is deterministic whatever the clock returned.
"""

import os
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self

from dromon.codec import DecodeError, decode, encode
from dromon.model import Event, EventRejected, State

SCHEMA_VERSION: Final = 1
"""Stored in ``PRAGMA user_version``. A log with any other version is refused."""

_SCHEMA: Final = (
    """CREATE TABLE events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at TEXT NOT NULL,
        payload TEXT NOT NULL
    ) STRICT""",
    """CREATE TRIGGER events_no_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'the event log is append-only'); END""",
    """CREATE TRIGGER events_no_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'the event log is append-only'); END""",
    f"PRAGMA user_version = {SCHEMA_VERSION}",
)

type Clock = Callable[[], str]


def utc_now() -> str:
    """Return the current time in Coordinated Universal Time, ISO 8601 with ``Z``."""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class LogError(Exception):
    """The event log cannot be used."""


class LogCorrupt(LogError):
    """A stored row cannot be replayed."""


class LogConflict(LogError):
    """Another writer appended to the log since this process last read it."""


@dataclass(frozen=True, slots=True)
class Entry:
    seq: int
    recorded_at: str
    event: Event


class EventLog:
    """An open event log and the state its events build.

    Opening a path that does not exist creates an empty log. Not safe to share between
    threads. Use one instance per process and one writer per log file.
    """

    def __init__(self, path: Path, clock: Clock = utc_now) -> None:
        _create_private(path)
        self._connection = sqlite3.connect(path, isolation_level=None)
        self._clock = clock
        self.path = path
        try:
            _initialize(self._connection)
            self._state, self._last_seq = _replay(self._connection)
        except BaseException:
            self._connection.close()
            raise
        self.opened_seq = self._last_seq
        """The newest sequence number when this process opened the log."""
        self.opened_bytes = self.size_bytes()
        """The log's size when this process opened it, or ``None`` if unknown."""

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @property
    def state(self) -> State:
        """A snapshot of the current state. Changing it does not change the log."""
        return self._state.copy()

    def size_bytes(self) -> int | None:
        """The bytes the log takes on disk, its database file and any SQLite write
        ahead log or rollback journal beside it, or ``None`` when a file cannot be
        read. Row 14 of the T3 Code review. Nothing compacts the log yet."""
        total = 0
        for suffix in ("", "-wal", "-journal"):
            try:
                total += self.path.with_name(self.path.name + suffix).stat().st_size
            except FileNotFoundError:
                continue
            except OSError:
                return None
        return total

    @property
    def last_seq(self) -> int:
        """The sequence number of the newest event, or 0 for an empty log."""
        return self._last_seq

    def append(self, event: Event) -> Entry:
        """Check ``event`` against the current state, store it, then apply it.

        Raises ``EventRejected`` if the event does not apply, and ``LogConflict`` if
        another writer appended first. In both cases nothing is stored and the state is
        unchanged.
        """
        self._state.check(event)
        payload = encode(event)
        recorded_at = _timestamp(self._clock)
        expected = self._last_seq + 1
        with _transaction(self._connection, "BEGIN IMMEDIATE") as connection:
            newest = _newest_seq(connection)
            if newest != self._last_seq:
                raise LogConflict(
                    f"the log is at {newest}, but this process read {self._last_seq}"
                )
            cursor = connection.execute(
                "INSERT INTO events (recorded_at, payload) VALUES (?, ?)",
                (recorded_at, payload),
            )
            if cursor.lastrowid != expected:
                raise LogCorrupt(f"the log assigned {cursor.lastrowid}, not {expected}")
        self._state.apply(event)
        self._last_seq = expected
        return Entry(expected, recorded_at, event)


def replay(path: Path) -> State:
    """Rebuild the state from the log at ``path``, opened read-only."""
    connection = _connect_read_only(path)
    try:
        state, _ = _replay(connection)
        return state
    finally:
        connection.close()


def read_entries(path: Path) -> list[Entry]:
    """Return every entry of the log at ``path`` in order, opened read-only.

    Each entry is decoded but not applied. The whole log is loaded into memory, and a
    paged reader is technical debt for when logs grow large.
    """
    connection = _connect_read_only(path)
    try:
        with _transaction(connection, "BEGIN") as reading:
            return list(_entries(reading))
    finally:
        connection.close()


def _create_private(path: Path) -> None:
    """Create ``path`` as an empty owner-only file unless it already exists."""
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(descriptor)


def _connect_read_only(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise LogError(f"no event log at {path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        version = _user_version(connection)
        if version != SCHEMA_VERSION:
            raise LogError(f"unsupported schema version {version}")
    except BaseException:
        connection.close()
        raise
    return connection


class _transaction:
    """Begin a transaction, commit it on success and roll it back on any exception.

    The connection is in autocommit mode, so every transaction boundary is explicit here
    rather than implied by the driver.
    """

    def __init__(self, connection: sqlite3.Connection, begin: str) -> None:
        self._connection = connection
        self._begin = begin

    def __enter__(self) -> sqlite3.Connection:
        self._connection.execute(self._begin)
        return self._connection

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._connection.execute("ROLLBACK" if kind is not None else "COMMIT")


def _timestamp(clock: Clock) -> str:
    # The clock is trusted code, so its type is left to the type checker. An empty
    # string is still refused, since it would make the metadata meaningless.
    recorded_at = clock()
    if not recorded_at:
        raise LogError(f"the clock returned {recorded_at!r}, not a timestamp")
    return recorded_at


def _user_version(connection: sqlite3.Connection) -> int:
    version: int = connection.execute("PRAGMA user_version").fetchone()[0]
    return version


def _newest_seq(connection: sqlite3.Connection) -> int:
    query = "SELECT coalesce(max(seq), 0) FROM events"
    newest: int = connection.execute(query).fetchone()[0]
    return newest


def _initialize(connection: sqlite3.Connection) -> None:
    with _transaction(connection, "BEGIN IMMEDIATE") as creating:
        version = _user_version(creating)
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            raise LogError(f"unsupported schema version {version}")
        objects: int = creating.execute(
            "SELECT count(*) FROM sqlite_schema"
        ).fetchone()[0]
        if objects != 0:
            raise LogError("the file is a database, but not a Dromon event log")
        for statement in _SCHEMA:
            creating.execute(statement)


def _entries(connection: sqlite3.Connection) -> Iterator[Entry]:
    """Yield each stored entry in order, checking that the sequence has no gaps."""
    rows = connection.execute(
        "SELECT seq, recorded_at, payload FROM events ORDER BY seq"
    )
    expected = 1
    for seq, recorded_at, payload in rows:
        if seq != expected:
            raise LogCorrupt(f"the log skips from {expected - 1} to {seq}")
        try:
            event = decode(payload)
        except DecodeError as error:
            raise LogCorrupt(f"event {seq} does not decode: {error}") from error
        yield Entry(seq, recorded_at, event)
        expected += 1


def _replay(connection: sqlite3.Connection) -> tuple[State, int]:
    """Apply every stored event to an empty state inside one read transaction."""
    state = State()
    last_seq = 0
    with _transaction(connection, "BEGIN") as reading:
        for entry in _entries(reading):
            try:
                state.apply(entry.event)
            except EventRejected as error:
                raise LogCorrupt(
                    f"event {entry.seq} does not apply: {error}"
                ) from error
            last_seq = entry.seq
    return state, last_seq
