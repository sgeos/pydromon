"""A snapshot of the state at a sequence number, from which a start resumes replay.

**Round 15, item 4, required by the operator.** ``docs/protocol/EVENT_LOG.md`` designs
it under bounded start, and this module builds it. The snapshot is a cache beside the
log and never the record. A snapshot that cannot be trusted is ignored, with its
reason, and the log is replayed whole, as it always was.

The file, ``events.sqlite.snapshot``, is one JSON object written owner only and
replaced whole. It holds the format version, the log's schema version, the state
version, a digest of the state's shape, the sequence number ``seq`` it was taken at,
the SHA-256 of the log's rows 1 and ``seq``, each its ``recorded_at`` and
``payload``, the state and the recorded times in canonical JSON, and the SHA-256 of
all of the above.

**The encoding is reflective.** Every value of ``dromon.model.State`` is a frozen
record, an enumeration, text, a whole number, a boolean, a time, ``None``, or a
dictionary, set or tuple of those, so one encoder driven by the type hints covers the
whole state. A dictionary is a list of key and value pairs in its insertion order,
since several of the state's maps are ordered sets or least-recently-used orders that
replay depends on, and its keys may be tuples or addresses. A set is a sorted list,
since no code may rely on a set's order. A union of records names the record's class.
The decoder builds each record through its constructor, so every record's own checks
run on the snapshot as on an event.

**The shape digest.** The SHA-256 of the type tree the encoder walks, each record's
name and each field's name and type. A change to the state's shape changes it, so a
snapshot from code of another shape is ignored without anyone raising a version.
``STATE_VERSION`` must still be raised by hand when the meaning of applying an event
changes and the shape does not. A test pins it with the codec's types and a replay, as
``codec_digest`` describes, and ``dromond --check`` and the check after every start
from a snapshot find a difference the pin did not.

**Porting hazard.** The reflection is Python's. A port writes the same encoding with
explicit code for each record, or ignores the file and replays the log, which is
always correct.
"""

import enum
import functools
import hashlib
import json
import os
import stat
import types
import typing
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, cast

from dromon.model import Event, State

FORMAT_VERSION: Final = 1
"""The version of the file's own layout."""

STATE_VERSION: Final = 3
"""Raised by hand whenever applying an event comes to mean something else for a state
of the same shape, since the shape digest cannot see that. **Review of 49236ad, item
3.** ``tests/test_state_version.py`` fails when the types ``codec_digest`` covers, or
the state a fixed history replays to, change while this stays the same, so a raise
cannot be forgotten. Version 2 is the round 15 follow-up, whose events
``line_turn_ended`` and ``brief_set`` gained fields. Version 3 is the merge of
``feat/r15-runs-disclosure``, where ``disclosure_checked`` gained ``checked_bytes`` and
a pass is kept apart from a failure, and ``run_ended`` gained ``output_tail``."""

SUFFIX: Final = ".snapshot"
"""The snapshot of ``events.sqlite`` is ``events.sqlite.snapshot``."""

MAX_SNAPSHOT_BYTES: Final = 1 << 32
"""The largest snapshot read, 4 GiB. A larger file is ignored, not read."""

SNAPSHOT_EVERY: Final = 10_000
"""How many events the log grows by between snapshots the daemon writes. **Decided by
core for review.** A start then replays at most this many events after the snapshot,
about a tenth of a second by the measurements in ``EVENT_LOG.md``, while a snapshot of
a large state is written seldom."""

TIMES_FIELDS: Final = ("line_changed", "message_recorded", "unanswered_recorded")
"""The maps of ``dromon.eventlog.RecordedTimes`` the snapshot keeps."""


class SnapshotRefused(Exception):
    """A snapshot that is not used, with the reason the daemon names."""


type Json = None | bool | int | float | str | list["Json"] | dict[str, "Json"]


def _resolve(hint: object) -> object:
    """``hint`` with any ``type`` alias replaced by its value, and ``None``, as a
    hint such as ``dict[str, None]`` writes it, read as its type."""
    if hint is None:
        return type(None)
    for _ in range(16):
        if isinstance(hint, typing.TypeAliasType):
            hint = hint.__value__
        else:
            return hint
    raise TypeError(f"the alias {hint!r} nests too deeply")


def _is_union(hint: object) -> bool:
    return typing.get_origin(hint) in (typing.Union, types.UnionType)


@functools.cache
def _hints(kind: type) -> dict[str, object]:
    """``kind``'s field types, read once for each record type, since reading them
    dominated a restore's time before they were kept."""
    return typing.get_type_hints(kind)


@functools.cache
def _field_names(kind: type) -> tuple[str, ...]:
    return tuple(field.name for field in fields(kind))


def encode_value(value: object, hint: object) -> Json:  # noqa: PLR0911
    """``value``, of the type ``hint`` names, as canonical JSON data."""
    hint = _resolve(hint)
    if _is_union(hint):
        return _encode_union(value, hint)
    origin = typing.get_origin(hint)
    if origin is dict or origin is Mapping:
        key_hint, value_hint = typing.get_args(hint)
        mapping = cast(Mapping[object, object], value)
        return [
            [encode_value(key, key_hint), encode_value(item, value_hint)]
            for key, item in mapping.items()
        ]
    if origin in (set, frozenset):
        (item_hint,) = typing.get_args(hint)
        encoded = [encode_value(item, item_hint) for item in cast(set[object], value)]
        return sorted(encoded, key=_sort_key)
    if origin is tuple:
        return _encode_tuple(cast(tuple[object, ...], value), typing.get_args(hint))
    if origin is typing.Literal:
        return cast(Json, value)
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return cast(Json, cast(enum.Enum, value).value)
    if hint is datetime:
        moment = cast(datetime, value)
        if moment.tzinfo is None:
            raise TypeError("a time in a snapshot must be aware")
        return moment.isoformat()
    if isinstance(hint, type) and is_dataclass(hint):
        return _encode_record(value, hint)
    if hint in (str, int, bool, float, type(None)):
        if type(value) is not hint:
            raise TypeError(f"{value!r} is not a {hint!r}")
        return cast(Json, value)
    raise TypeError(f"no snapshot encoding for {hint!r}")


def _sort_key(item: Json) -> str:
    return json.dumps(item, sort_keys=True, separators=(",", ":"))


def _encode_tuple(value: tuple[object, ...], args: tuple[object, ...]) -> Json:
    if len(args) == 2 and args[1] is Ellipsis:
        return [encode_value(item, args[0]) for item in value]
    if len(value) != len(args):
        raise TypeError(f"a tuple of {len(value)} items for {len(args)} types")
    return [encode_value(item, hint) for item, hint in zip(value, args, strict=True)]


def _encode_record(value: object, kind: type) -> Json:
    if type(value) is not kind:
        raise TypeError(f"{value!r} is not a {kind.__name__}")
    hints = _hints(kind)
    return {
        name: encode_value(getattr(value, name), hints[name])
        for name in _field_names(kind)
    }


def _encode_union(value: object, hint: object) -> Json:
    if value is None:
        if type(None) not in typing.get_args(hint):
            raise TypeError(f"None is not a {hint!r}")
        return None
    records = _all_records(hint)
    if type(value) in records:
        return {
            "type": type(value).__name__,
            "value": _encode_record(value, type(value)),
        }
    for arg in (_resolve(arg) for arg in typing.get_args(hint)):
        if arg is type(None) or _is_union(arg):
            continue
        if isinstance(arg, type) and arg in records:
            continue
        try:
            return encode_value(value, arg)
        except TypeError:
            continue
    raise TypeError(f"{value!r} is no member of {hint!r}")


type Decoder = Callable[[Json], object]


def decode_value(data: Json, hint: object) -> object:
    """The value of type ``hint`` that ``encode_value`` encoded as ``data``. Raises
    ``SnapshotRefused`` for data of any other form. The decoder for each type is
    built once and kept, since inspecting the type of every value dominated a
    restore's time."""
    return _decoder(hint)(data)


@functools.cache
def _decoder(hint: object) -> Decoder:  # noqa: PLR0911
    hint = _resolve(hint)
    if _is_union(hint):
        return _union_decoder(hint)
    origin = typing.get_origin(hint)
    if origin is dict or origin is Mapping:
        key_hint, value_hint = typing.get_args(hint)
        return _map_decoder(_decoder(key_hint), _decoder(value_hint))
    if origin in (set, frozenset):
        (item_hint,) = typing.get_args(hint)
        return _set_decoder(_decoder(item_hint), frozen=origin is frozenset)
    if origin is tuple:
        args: tuple[object, ...] = typing.get_args(hint)
        if len(args) == 2 and args[1] is Ellipsis:
            return _many_decoder(_decoder(args[0]))
        return _fixed_decoder(tuple(_decoder(arg) for arg in args))
    if origin is typing.Literal:
        return _literal_decoder(typing.get_args(hint))
    if isinstance(hint, type) and issubclass(hint, enum.Enum):
        return _enum_decoder(hint)
    if hint is datetime:
        return _decode_time
    if isinstance(hint, type) and is_dataclass(hint):
        return _record_decoder(hint)
    if hint in (str, int, bool, float, type(None)):
        return _plain_decoder(cast(type, hint))
    raise SnapshotRefused(f"no snapshot encoding for {hint!r}")


def _list(data: Json) -> list[Json]:
    if not isinstance(data, list):
        raise SnapshotRefused("a list was expected")
    return data


def _map_decoder(key: Decoder, value: Decoder) -> Decoder:
    def decode(data: Json) -> object:
        decoded: dict[object, object] = {}
        for pair in _list(data):
            items = _list(pair)
            if len(items) != 2:
                raise SnapshotRefused("a map entry is not a key and a value")
            name = key(items[0])
            if name in decoded:
                raise SnapshotRefused("a map names a key twice")
            decoded[name] = value(items[1])
        return decoded

    return decode


def _set_decoder(item: Decoder, *, frozen: bool) -> Decoder:
    def decode(data: Json) -> object:
        items = [item(entry) for entry in _list(data)]
        found = set(items)
        if len(found) != len(items):
            raise SnapshotRefused("a set names an item twice")
        return frozenset(found) if frozen else found

    return decode


def _many_decoder(item: Decoder) -> Decoder:
    return lambda data: tuple(item(entry) for entry in _list(data))


def _fixed_decoder(items: tuple[Decoder, ...]) -> Decoder:
    def decode(data: Json) -> object:
        entries = _list(data)
        if len(entries) != len(items):
            raise SnapshotRefused("a tuple has the wrong number of items")
        return tuple(item(entry) for item, entry in zip(items, entries, strict=True))

    return decode


def _literal_decoder(allowed: tuple[object, ...]) -> Decoder:
    def decode(data: Json) -> object:
        if data not in allowed:
            raise SnapshotRefused(f"a {type(data).__name__} is not one of {allowed!r}")
        return data

    return decode


def _enum_decoder(kind: type[enum.Enum]) -> Decoder:
    def decode(data: Json) -> object:
        try:
            return kind(data)
        except ValueError as error:
            raise SnapshotRefused(
                f"a {type(data).__name__} is not a {kind.__name__}"
            ) from error

    return decode


def _decode_time(data: Json) -> object:
    if not isinstance(data, str):
        raise SnapshotRefused("a time is not text")
    try:
        moment = datetime.fromisoformat(data)
    except ValueError as error:
        raise SnapshotRefused("a text is not a time") from error
    if moment.tzinfo is None:
        raise SnapshotRefused("a time is not aware")
    return moment


def _plain_decoder(kind: type) -> Decoder:
    def decode(data: Json) -> object:
        if type(data) is not kind:
            raise SnapshotRefused(f"a {type(data).__name__} is not a {kind.__name__}")
        return data

    return decode


def _record_decoder(kind: type) -> Decoder:
    names = _field_names(kind)
    hints = _hints(kind)
    # Built on first use, since a record's fields may name the record's own union.
    decoders: dict[str, Decoder] = {}

    def decode(data: Json) -> object:
        if not isinstance(data, dict):
            raise SnapshotRefused(f"a {kind.__name__} is not an object")
        if len(data) != len(names) or any(name not in data for name in names):
            raise SnapshotRefused(f"a {kind.__name__} has other fields")
        if not decoders:
            decoders.update((name, _decoder(hints[name])) for name in names)
        values = {name: decoders[name](data[name]) for name in names}
        try:
            return kind(**values)
        except (ValueError, TypeError) as error:
            raise SnapshotRefused(
                f"a {kind.__name__} is refused by its own checks"
            ) from error

    return decode


def _union_decoder(hint: object) -> Decoder:
    args = [_resolve(arg) for arg in typing.get_args(hint)]
    nullable = type(None) in args
    records = {kind.__name__: kind for kind in _all_records(hint)}
    others = [
        _decoder(arg)
        for arg in args
        if arg is not type(None)
        and not _is_union(arg)
        and not (isinstance(arg, type) and arg in records.values())
    ]

    def decode(data: Json) -> object:
        if data is None:
            if not nullable:
                raise SnapshotRefused(f"null is not a {hint!r}")
            return None
        if isinstance(data, dict) and records and set(data) == {"type", "value"}:
            name = data["type"]
            if not isinstance(name, str) or name not in records:
                raise SnapshotRefused(f"{name!r} is not a record of {hint!r}")
            return _decoder(records[name])(data["value"])
        for other in others:
            try:
                return other(data)
            except SnapshotRefused:
                continue
        raise SnapshotRefused(f"a {type(data).__name__} is no member of {hint!r}")

    return decode


@functools.cache
def _all_records(hint: object) -> tuple[type, ...]:
    """The records of ``hint``, a union, and of the unions it names, in order."""
    found: list[type] = []
    pending: list[object] = [hint]
    for _ in range(64):
        if not pending:
            return tuple(found)
        current = _resolve(pending.pop(0))
        args: tuple[object, ...] = typing.get_args(current)
        for member in args:
            resolved: object = _resolve(member)
            if _is_union(resolved):
                pending.append(resolved)
                continue
            kind = resolved if isinstance(resolved, type) else None
            if kind is not None and is_dataclass(kind):
                found.append(kind)
    raise TypeError(f"the union {hint!r} nests too deeply")


def shape_digest() -> str:
    """The SHA-256 of the state's type tree, each record's name with each field's name
    and type, in a fixed order, so a change of shape changes it."""
    lines: list[str] = []
    seen: set[type] = set()
    pending: list[object] = [State]
    for _ in range(4096):
        if not pending:
            break
        hint = _resolve(pending.pop())
        if isinstance(hint, type) and is_dataclass(hint):
            if hint in seen:
                continue
            seen.add(hint)
            hints = _hints(hint)
            for field in fields(hint):
                lines.append(f"{hint.__name__}.{field.name}: {hints[field.name]!r}")
                pending.append(hints[field.name])
        elif isinstance(hint, type) and issubclass(hint, enum.Enum):
            lines.append(f"{hint.__name__} = {[member.value for member in hint]!r}")
        else:
            pending.extend(arg for arg in typing.get_args(hint) if arg is not Ellipsis)
    else:
        raise TypeError("the state's type tree is larger than the bound")
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest()


def hint_text(hint: object) -> str:
    """``hint`` written the same way on every Python version, a record or an
    enumeration by its name, a generic as its origin's name and its arguments, a
    union as its members' texts sorted, and a literal as its values. **Review of
    49236ad, item 3.** ``shape_digest`` uses Python's own text of a hint, which the
    pin of ``codec_digest`` cannot, since the gate runs on 3.12 and 3.15. Raises
    ``TypeError`` for a hint no state or event uses. Recursion is bounded by how
    deeply the model's types nest."""
    hint = _resolve(hint)
    if hint is type(None):
        return "None"
    if _is_union(hint):
        return " | ".join(sorted(hint_text(arg) for arg in typing.get_args(hint)))
    origin = typing.get_origin(hint)
    args: tuple[object, ...] = typing.get_args(hint)
    if origin is typing.Literal:
        return f"Literal[{', '.join(repr(arg) for arg in args)}]"
    if origin is not None:
        named = getattr(origin, "__name__", None)
        if not isinstance(named, str):
            raise TypeError(f"no text for the generic {hint!r}")
        written = ", ".join(
            "..." if arg is Ellipsis else hint_text(arg) for arg in args
        )
        return f"{named}[{written}]"
    if isinstance(hint, type):
        return hint.__name__
    raise TypeError(f"no text for {hint!r}")


def codec_digest() -> str:
    """The SHA-256 of the types of the state and of every event the codec writes,
    as ``type_lines`` writes them. **Review of 49236ad, item 3.** A change of meaning
    that keeps the state's shape is invisible to a start, so
    ``tests/test_state_version.py`` pins this digest with ``STATE_VERSION`` and a
    replay of a fixed history, and fails when either changes without a new version.
    Changing an event's fields is taken as a possible change of meaning."""
    return hashlib.sha256(
        "\n".join(type_lines((State, Event))).encode("utf-8")
    ).hexdigest()


def type_lines(roots: tuple[object, ...]) -> list[str]:
    """Each record reachable from ``roots`` with each field's name and type as
    ``hint_text`` writes it, and each enumeration with its values, sorted."""
    lines: set[str] = set()
    seen: set[object] = set()
    pending: list[object] = list(roots)
    for _ in range(8192):
        if not pending:
            break
        hint = _resolve(pending.pop())
        if hint in seen:
            continue
        seen.add(hint)
        if isinstance(hint, type) and is_dataclass(hint):
            hints = _hints(hint)
            for field in fields(hint):
                written = hint_text(hints[field.name])
                lines.add(f"{hint.__name__}.{field.name}: {written}")
                pending.append(hints[field.name])
        elif isinstance(hint, type) and issubclass(hint, enum.Enum):
            lines.add(f"{hint.__name__} = {[member.value for member in hint]!r}")
        elif typing.get_origin(hint) is not typing.Literal:
            pending.extend(arg for arg in typing.get_args(hint) if arg is not Ellipsis)
    else:
        raise TypeError("the type tree is larger than the bound")
    return sorted(lines)


def canonical(data: Json) -> bytes:
    """``data`` as canonical JSON, sorted keys and no spaces, in UTF-8."""
    return json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def row_digest(recorded_at: str, payload: str) -> str:
    """The SHA-256 of one stored row, its ``recorded_at`` and its ``payload``."""
    return hashlib.sha256(canonical([recorded_at, payload])).hexdigest()


def encode_state(state: State, times: Mapping[str, Mapping[str, str]]) -> Json:
    """The state and the recorded times as the snapshot's data."""
    return {
        "state": encode_value(state, State),
        "times": {
            name: [[key, value] for key, value in times[name].items()]
            for name in TIMES_FIELDS
        },
    }


@dataclass(frozen=True, slots=True)
class Loaded:
    """A snapshot that was read and checked against its own digest."""

    seq: int
    first_row: str
    nth_row: str
    state: State
    times: dict[str, dict[str, str]]
    digest: str
    """The digest the file names, which it was checked against."""


def snapshot_path(log_path: Path) -> Path:
    return log_path.with_name(log_path.name + SUFFIX)


def write(
    path: Path,
    *,
    schema_version: int,
    seq: int,
    first_row: str,
    nth_row: str,
    state: State,
    times: Mapping[str, Mapping[str, str]],
) -> None:
    """Write a snapshot to ``path``, owner only, replacing any earlier one whole. The
    file is written beside it under another name and moved into place, so a reader
    finds either the old snapshot or the new one, never a part.

    **Review of 49236ad, item 2.** The directory is synchronized after the move, so
    the new name is durable once this returns. A crash before the move leaves the
    earlier snapshot or none, and may leave the ``.writing`` file, which no start
    reads. A crash after the move and before the directory is synchronized may lose
    the new name on some file systems, which leaves the earlier snapshot or none.
    Either way a start finds a whole snapshot that was the state at its own ``seq``,
    or none, and replays the events after it, or the whole log. A directory that
    cannot be synchronized raises ``OSError``, and the caller records it."""
    body = _body(
        schema_version=schema_version,
        seq=seq,
        first_row=first_row,
        nth_row=nth_row,
        state=state,
        times=times,
    )
    body["digest"] = hashlib.sha256(canonical(body)).hexdigest()
    temporary = path.with_name(path.name + ".writing")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical(body))
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    sync_directory(path.parent)


def sync_directory(directory: Path) -> None:
    """Synchronize ``directory``'s entries to the disk, so a name moved into it
    survives a crash. Raises ``OSError``. **Porting note.** Opening a directory to
    synchronize it is the POSIX form, which Linux and macOS, the platforms the gate
    runs on, both allow. A port to a platform without it says what it does instead."""
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _body(
    *,
    schema_version: int,
    seq: int,
    first_row: str,
    nth_row: str,
    state: State,
    times: Mapping[str, Mapping[str, str]],
) -> dict[str, Json]:
    """The snapshot's object without its digest, as ``write`` stores it."""
    return {
        "format": FORMAT_VERSION,
        "schema": schema_version,
        "state_version": STATE_VERSION,
        "shape": shape_digest(),
        "seq": seq,
        "first_row": first_row,
        "nth_row": nth_row,
        **cast(dict[str, Json], encode_state(state, times)),
    }


def body_digest(
    *,
    schema_version: int,
    seq: int,
    first_row: str,
    nth_row: str,
    state: State,
    times: Mapping[str, Mapping[str, str]],
) -> str:
    """The digest ``write`` would store for this state and these times. **Review of
    49236ad, item 1.** A start that restored from a snapshot compares it with the
    digest of a full replay, so the restored file need not be kept or read again."""
    return hashlib.sha256(
        canonical(
            _body(
                schema_version=schema_version,
                seq=seq,
                first_row=first_row,
                nth_row=nth_row,
                state=state,
                times=times,
            )
        )
    ).hexdigest()


def read(
    path: Path, schema_version: int, row: Callable[[int], tuple[str, str] | None]
) -> Loaded:
    """The snapshot at ``path``, checked, or ``SnapshotRefused`` with the reason.
    ``row`` gives a stored row of the log by sequence number, or ``None`` past its
    end. Each check of the design runs, in order: the file is a regular file of the
    owner's alone within the bound, its digest verifies, its versions and shape are
    this code's, its ``seq`` lies within the log, and the log's rows 1 and ``seq``
    have the digests it names."""
    raw = _read_file(path)
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SnapshotRefused("the snapshot is not JSON") from error
    if not isinstance(body, dict):
        raise SnapshotRefused("the snapshot is not an object")
    data = cast(dict[str, Json], body)
    claimed = data.pop("digest", None)
    if claimed != hashlib.sha256(canonical(data)).hexdigest():
        raise SnapshotRefused("the snapshot's digest does not verify")
    for name, expected in (
        ("format", FORMAT_VERSION),
        ("schema", schema_version),
        ("state_version", STATE_VERSION),
        ("shape", shape_digest()),
    ):
        if data.get(name) != expected:
            raise SnapshotRefused(f"the snapshot's {name} is not this daemon's")
    seq = data.get("seq")
    first_row = data.get("first_row")
    nth_row = data.get("nth_row")
    if type(seq) is not int or seq < 1:
        raise SnapshotRefused("the snapshot's seq is not a positive whole number")
    if not isinstance(first_row, str) or not isinstance(nth_row, str):
        raise SnapshotRefused("the snapshot's row digests are not text")
    for number, digest in ((1, first_row), (seq, nth_row)):
        stored = row(number)
        if stored is None:
            raise SnapshotRefused(f"the log has no row {number}, so it is another log")
        if row_digest(*stored) != digest:
            raise SnapshotRefused(f"the log's row {number} differs from the snapshot's")
    state = decode_value(data.get("state"), State)
    times = _decode_times(data.get("times"))
    return Loaded(
        seq, first_row, nth_row, cast(State, state), times, cast(str, claimed)
    )


def _read_file(path: Path) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as error:
        raise SnapshotRefused("there is no snapshot") from error
    except OSError as error:
        raise SnapshotRefused(f"the snapshot cannot be opened: {error}") from error
    try:
        found = os.fstat(descriptor)
        if not stat.S_ISREG(found.st_mode):
            raise SnapshotRefused("the snapshot is not a regular file")
        if found.st_mode & 0o077:
            raise SnapshotRefused("the snapshot is readable by others than its owner")
        if found.st_size > MAX_SNAPSHOT_BYTES:
            raise SnapshotRefused("the snapshot is larger than the bound")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            return handle.read(MAX_SNAPSHOT_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _decode_times(data: Json) -> dict[str, dict[str, str]]:
    if not isinstance(data, dict) or sorted(data) != sorted(TIMES_FIELDS):
        raise SnapshotRefused("the snapshot's times are not the recorded times")
    decoded: dict[str, dict[str, str]] = {}
    for name in TIMES_FIELDS:
        value = decode_value(data[name], dict[str, str])
        decoded[name] = cast(dict[str, str], value)
    return decoded
