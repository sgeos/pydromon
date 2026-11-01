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
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Final, Self, cast

from dromon.codec import DecodeError, decode, encode
from dromon.model import (
    Event,
    EventRejected,
    Line,
    LineAwakened,
    LineCreated,
    LineHeld,
    LineHibernated,
    LineReleased,
    LineAddress,
    LineMail,
    LineStateChanged,
    MessageRead,
    MessageReceived,
    MessageSent,
    State,
)

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
        self._times = RecordedTimes()
        try:
            _initialize(self._connection)
            self._state, self._last_seq = _replay(self._connection, self._times)
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

    def line(self, line_id: str) -> Line:
        """The record of ``line_id``, read without the copy ``state`` makes, which
        is safe since a record is frozen. Raises ``KeyError`` for a line the log does
        not hold."""
        return self._state.lines[line_id]

    def line_mail(self, line_id: str) -> LineMail:
        """What ``line_id``'s mail says of it, read without the copy ``state`` makes,
        at a cost that does not grow with the messages the log holds. **Task of
        2026-11-01.**"""
        return self._state.line_mail(line_id)

    @property
    def times(self) -> "RecordedTimes":
        """When the events this log holds were recorded, as far as views need it.
        The model never reads it."""
        return self._times

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
        entry = Entry(expected, recorded_at, event)
        self._times.note(entry, self._state)
        return entry


LINE_CHANGES: Final = (
    LineCreated,
    LineStateChanged,
    LineHeld,
    LineReleased,
    LineHibernated,
    LineAwakened,
)
"""The events that change whether a line is running, idle, held or hibernated, whose
time ``RecordedTimes.line_changed`` keeps."""


class RecordedTimes:
    """The ``recorded_at`` of each line's latest ``LINE_CHANGES`` event and of each
    unread message's first event, kept as the log is replayed and appended to.

    **Task of 2026-10-31.** Lines are shown how long another line has stood as it
    is and how old its oldest unread mail is, and reading the whole log for each
    ``line_status`` would cost a full scan each time. The times are the clock's
    text, never parsed here, and the model never reads them, so replay stays
    deterministic. They follow the clock, which may step backwards, so an age read
    from them can be wrong by as much as the clock was.

    **Task of 2026-11-01, answering review r13.** A message's time is kept only
    while it is in ``State.unread_mail``, the only messages whose age is shown, so
    the map is bounded by the unread mail rather than by every message the log has
    held. Mail enters that index only when it is sent or received, and leaves it
    only when it is read, so these two events keep the map.
    **Porting note.** A port keeps the same two maps beside its state."""

    def __init__(self) -> None:
        self.line_changed: dict[str, str] = {}
        """When each line last started or stopped running, was held or released,
        or was hibernated or awakened, or was created."""
        self.message_recorded: dict[str, str] = {}
        """When each unread message to a line was sent or received, by its
        identifier."""

    def note(self, entry: Entry, state: State) -> None:
        """Note ``entry``'s time, with ``state`` as it stands once the entry is
        applied."""
        event = entry.event
        if isinstance(event, LINE_CHANGES):
            self.line_changed[event.line_id] = entry.recorded_at
        elif isinstance(event, MessageSent | MessageReceived):
            recipient = event.recipient
            if isinstance(recipient, LineAddress) and event.message_id in (
                state.unread_mail.get(recipient.line_id, {})
            ):
                self.message_recorded.setdefault(event.message_id, entry.recorded_at)
        elif isinstance(event, MessageRead):
            self.message_recorded.pop(event.message_id, None)


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


SNAPSHOT_ROWS: Final = 2000
"""The most events a snapshot reads in one range. A range holds the live log's read
lock while it is read, which keeps the daemon's next commit waiting, so ranges are
kept small, by this count and by ``SNAPSHOT_BYTES``."""

SNAPSHOT_BYTES: Final = 1024 * 1024
"""About the most bytes a snapshot reads in one range. A message body may be up to
1 MiB, so 2,000 such events would be 2 GiB, and the count alone would not bound the
lock. A range stops at the first event that brings it to this size, so it reads at
most this much plus one event, which is at most a little over 2 MiB, and always at
least one event, whatever its size."""

SNAPSHOT_PAUSE_SECONDS: Final = 0.001
"""The pause after each range, with no lock held, in which a waiting commit of the
daemon goes through."""


def snapshot(
    source: Path,
    destination: Path,
    *,
    rows: int = SNAPSHOT_ROWS,
    max_bytes: int = SNAPSHOT_BYTES,
    pause_seconds: float = SNAPSHOT_PAUSE_SECONDS,
) -> None:
    """Copy the log at ``source`` to ``destination`` as it stood when the copy
    began, never writing ``source``, so a check can open the copy and append to it
    while a daemon still writes the original. ``destination`` must not exist, and is
    created owner only, since the log holds message bodies and answers.

    **Decided by the operator on 2026-10-31, after audit's re-review of P1.** The
    log keeps SQLite's rollback journal, in which a reader's lock keeps a writer's
    commit waiting, and SQLite's online backup starts again from the first page
    whenever the source is written between its steps, so on a busy, large log it
    would never finish. The copy is therefore made from the schema and the rows. The
    schema's statements and ``user_version`` are copied exactly. The newest sequence
    number is read once, and the events up to it are copied in ranges by sequence
    number, each at most ``rows`` events and about ``max_bytes`` bytes, read through
    one cursor that is closed before the range is written, with a pause of
    ``pause_seconds`` and no lock after it. The table is append only, its triggers
    refuse an update or a delete, so a range once read never changes, and an event
    appended during the copy lies above the bound and is left out. The copy
    therefore finishes in at most as many ranges as there are events below the
    bound, and the daemon's commits wait at most for one range to be read, a little
    over 2 MiB at the defaults. A snapshot can still delay a daemon's write by that
    much.

    **The source is attached read-only**, with ``mode=ro``. A transaction the copy
    begins with ``BEGIN IMMEDIATE`` would otherwise take the write lock of every
    attached database, the live log's included, and keep the daemon's commits
    waiting for the whole copy. A test pins this.

    A ``source`` that does not exist, or holds no schema yet, leaves the copy empty,
    and opening ``destination`` then creates an empty log, as a first start would.
    The schema version is not checked here, so the code that opens the copy refuses
    an unknown one in its own words. Any table other than ``events``, which no
    version holds yet, is copied whole in one statement. The schema's statements are
    the log's own, from an owner-only file the daemon wrote, and are trusted as the
    log is. Raises ``LogError`` when the source cannot be read or the copy cannot be
    written.

    Porting note. The copy is plain SQL over an attached database, which a port
    that keeps SQLite keeps. A port with another store copies the events below a
    sequence bound read once, as here."""
    if rows < 1 or max_bytes < 1:
        raise ValueError("a snapshot copies at least one row at a time")
    if not source.exists():
        return
    try:
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError as error:
        raise LogError(f"cannot create a copy of the log at {destination}") from error
    os.close(descriptor)
    try:
        copying = sqlite3.connect(
            destination.resolve().as_uri(), uri=True, isolation_level=None
        )
        try:
            copying.execute(
                "ATTACH DATABASE ? AS source", (f"{source.resolve().as_uri()}?mode=ro",)
            )
            tables = _copy_schema(copying)
            if "events" in tables:
                _copy_events(copying, rows, max_bytes, pause_seconds)
            for table in sorted(tables - {"events"}):
                # The name is the log's own, from its schema, quoted as an
                # identifier, which SQL cannot take as a parameter.
                name = table.replace('"', '""')
                copying.execute(
                    f'INSERT INTO main."{name}" SELECT * FROM source."{name}"'  # noqa: S608
                )
            copying.execute("DETACH DATABASE source")
        finally:
            copying.close()
    except sqlite3.Error as error:
        raise LogError(f"cannot copy the event log at {source}: {error}") from error


def _copy_schema(copying: sqlite3.Connection) -> set[str]:
    """Create the attached source's schema in the copy, statement for statement,
    with its ``user_version``, and give the names of its tables. SQLite's own
    objects, such as ``sqlite_sequence``, are made by SQLite itself."""
    found: list[tuple[str, str, str]] = copying.execute(
        "SELECT type, name, sql FROM source.sqlite_schema"
        " WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        " ORDER BY rowid"
    ).fetchall()
    version: int = copying.execute("PRAGMA source.user_version").fetchone()[0]
    with _transaction(copying, "BEGIN IMMEDIATE") as creating:
        for _, _, statement in found:
            creating.execute(statement)
        creating.execute(f"PRAGMA main.user_version = {int(version)}")
    return {name for kind, name, _ in found if kind == "table"}


def _copy_events(
    copying: sqlite3.Connection, rows: int, max_bytes: int, pause_seconds: float
) -> None:
    """Copy the events up to the newest sequence number when the copy begins, in
    ranges of at most ``rows`` events and about ``max_bytes`` bytes. Each range is
    read through one cursor, which holds the live log's read lock, and the cursor is
    closed once the range is read, before anything is written, so the lock is held
    only while a range is read. A range always takes one event, however large, so
    every range copies at least one and the loop, bounded by the count of events,
    ends. A copy that stops short of the bound is refused."""
    bound: int = copying.execute(
        "SELECT coalesce(max(seq), 0) FROM source.events"
    ).fetchone()[0]
    copied = 0
    for _ in range(bound):
        if copied >= bound:
            break
        reading = copying.execute(
            "SELECT seq, * FROM source.events WHERE seq > ? AND seq <= ?"
            " ORDER BY seq LIMIT ?",
            (copied, bound, rows),
        )
        batch: list[tuple[object, ...]] = []
        last = copied
        size = 0
        try:
            for row in reading:
                found = tuple(cast(tuple[object, ...], row))
                if not isinstance(found[0], int) or found[0] <= last:
                    raise LogError("the log's events are not in sequence order")
                last = found[0]
                batch.append(found[1:])
                size += _row_bytes(found[1:])
                if size >= max_bytes:
                    break
        finally:
            reading.close()
        if not batch:
            break
        marks = ", ".join("?" * len(batch[0]))
        with _transaction(copying, "BEGIN IMMEDIATE") as writing:
            writing.executemany(f"INSERT INTO main.events VALUES ({marks})", batch)  # noqa: S608
        copied = last
        time.sleep(pause_seconds)
    if copied != bound:
        raise LogError(f"the copy of the log stopped at event {copied} of {bound}")


def _row_bytes(row: tuple[object, ...]) -> int:
    """About how many bytes ``row`` takes, as UTF-8 for text."""
    total = 0
    for value in row:
        if isinstance(value, str):
            total += len(value.encode("utf-8", "surrogatepass"))
        elif isinstance(value, bytes):
            total += len(value)
        else:
            total += 8
    return total


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


def _replay(
    connection: sqlite3.Connection, times: RecordedTimes | None = None
) -> tuple[State, int]:
    """Apply every stored event to an empty state inside one read transaction, and
    note their times in ``times`` when it is given."""
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
            if times is not None:
                times.note(entry, state)
            last_seq = entry.seq
    return state, last_seq
