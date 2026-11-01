"""Values of a line's configuration kept out of the event log.

**Required by the operator on 2026-11-01, the design decided by core for review.** An
MCP server's ``env`` and ``headers`` and a settings document's ``env`` may hold
credentials, and ``line_configured`` stored them in the log as written. The log is
owner only, but it is also copied, by ``dromond --check`` and by any backup, and every
view and export of it shows what it holds. So the daemon now seals each literal value
before it appends the event. The value goes to the sealed store, a file beside the
log, and the configuration text holds ``SEALED_TEXT`` in its place. The event names
the value's location and an HMAC-SHA256 of it under a random key kept only in the
store, as ``dromon.model.SealedValue`` says. The daemon unseals the values only when
it builds the line's engine, and when it compares a stored configuration with the
project file's.

What is sealed. Every non-empty value of an MCP server's ``env`` or ``headers`` that
holds no ``${NAME}`` reference, and every non-empty value of a settings document's
``env``. A value that holds a reference is kept as written, since the reference is
how an operator already keeps a token out of the log, and its literal text, such as
``Bearer``, is not the secret. A credential written into a server's ``command``,
``args`` or ``url`` is not sealed and still reaches the log, which is a residual.

Trust. The store is created owner only and is refused when its mode lets anyone else
read it. It holds only values and their keys. Replay never reads it, so the state a
log builds, a client's view and a port all agree without it, and the log alone tells
nothing of a sealed value. Losing the store loses the values, and a line whose values
are missing fails its turn naming each location, never a value, until it is
configured again.

Failure modes. The store is written before the event is appended, so a crash between
the two leaves values no event names, which the next prune removes. A store that
cannot be read or does not verify never unseals a value. The digest is checked on
every read, so a value changed in the store is refused rather than used.

Porting note. HMAC-SHA256, a JSON file replaced atomically, and the same location
steps as the canonical configuration JSON, which a port keeps.
"""

import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, cast

from dromon.lineconfig import (
    ConfigError,
    LineConfig,
    decode_config,
    encode_config,
    references,
)
from dromon.model import SealedValue

SEALED_TEXT: Final = "<sealed>"
"""What a configuration text holds where a value was sealed."""

STORE_VERSION: Final = 1
MAX_STORE_BYTES: Final = 64 * 1024 * 1024
"""The largest store read. A configuration holds at most 256 KiB, so this admits
every value of hundreds of lines at once."""

KEY_BYTES: Final = 32
"""The length of each value's random HMAC key, the digest's own length."""


class SealedError(ConfigError):
    """A sealed value cannot be kept or recovered. Its message names locations and
    files, never a value. It is a ``ConfigError``, so a client that configures a line
    is answered as for any configuration it may not have."""


def store_path(log_path: Path) -> Path:
    """Where the sealed store of the log at ``log_path`` is kept, beside it."""
    return log_path.with_name(log_path.name + ".sealed")


def digest_of(key: bytes, value: str) -> str:
    """The HMAC-SHA256 of ``value`` under ``key``, in lowercase hexadecimal."""
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).hexdigest()


type Place = tuple[str, ...]


def literal_places(config: LineConfig) -> list[tuple[Place, str]]:
    """Each value of ``config`` that sealing takes out of the log, with its
    location, in the order of the locations."""
    document = _document(config)
    found: list[tuple[Place, str]] = []
    servers = cast(dict[str, dict[str, object]], document.get("mcp_servers", {}))
    for server, entry in servers.items():
        for table in ("env", "headers"):
            values = cast(dict[str, str], entry.get(table, {}))
            for key, value in values.items():
                if value and not references(value):
                    found.append((("mcp_servers", server, table, key), value))
    settings = cast(dict[str, object], document.get("settings", {}))
    for key, value in cast(dict[str, str], settings.get("env", {})).items():
        if value:
            found.append((("settings", "env", key), value))
    return sorted(found)


@dataclass(frozen=True, slots=True)
class Sealing:
    """A configuration with its literal values taken out, what the event names of
    them, and what the store must keep, by digest, as the key and the value."""

    config: LineConfig
    sealed: tuple[SealedValue, ...]
    values: Mapping[str, tuple[str, str]]


def seal(config: LineConfig) -> Sealing:
    """``config`` with each of its ``literal_places`` replaced by ``SEALED_TEXT``.
    Each value gets its own random key, so equal values have unrelated digests."""
    places = literal_places(config)
    if not places:
        return Sealing(config, (), {})
    check_sealable(config)
    document = _document(config)
    sealed: list[SealedValue] = []
    values: dict[str, tuple[str, str]] = {}
    for at, value in places:
        key = secrets.token_bytes(KEY_BYTES)
        digest = digest_of(key, value)
        table = _table_of(document, at)
        if table is None:
            _fail(f"the configuration has no table at {_named(at)}")
        table[at[-1]] = SEALED_TEXT
        sealed.append(SealedValue(at, digest))
        values[digest] = (key.hex(), value)
    return Sealing(_config(document), tuple(sealed), values)


def check_sealable(config: LineConfig) -> None:
    """Raise ``SealedError`` when ``config`` gives ``SEALED_TEXT`` as a value."""
    places = literal_places(config)
    shown = [_named(at) for at, value in places if value == SEALED_TEXT]
    if shown:
        # A view shows a sealed value as the placeholder, so a configuration copied
        # from a view and given back would otherwise replace each credential with the
        # placeholder's own text.
        _fail(
            "the configuration holds "
            + SEALED_TEXT
            + " at "
            + ", ".join(shown)
            + ", as a view shows a sealed value; give each value or a reference"
        )


def unseal(
    config: LineConfig, sealed: Iterable[SealedValue], store: "SealedStore"
) -> LineConfig:
    """``config`` with each sealed value put back from ``store``. Raises
    ``SealedError`` naming each location the store cannot give, or the configuration
    no longer holds sealed."""
    wanted = tuple(sealed)
    if not wanted:
        return config
    kept = store.read()
    document = _document(config)
    missing: list[str] = []
    for value in wanted:
        table = _table_of(document, value.at)
        if table is None or table.get(value.at[-1]) != SEALED_TEXT:
            raise SealedError(
                f"the configuration holds no sealed value at {_named(value.at)}"
            )
        entry = kept.get(value.digest)
        if entry is None or not _verifies(value.digest, entry):
            missing.append(_named(value.at))
            continue
        table[value.at[-1]] = entry[1]
    if missing:
        raise SealedError(
            "the sealed store has no valid value for "
            + ", ".join(missing)
            + "; configure the line again to give them"
        )
    return _config(document)


def literal_count(config: LineConfig) -> int:
    """How many values of ``config`` would be sealed, which a configuration the
    log stored before sealing holds as written."""
    return len(literal_places(config))


class SealedStore:
    """The owner-only file of sealed values beside an event log. One daemon writes
    it, as one daemon writes its log.

    A store that is not ``writable`` reads the file and never changes it, which is
    how ``dromond --check`` reads the live daemon's store while it works on a copy
    of the log. What the check seals is then kept nowhere, which a check, whose
    copy is discarded, never needs."""

    def __init__(self, path: Path, *, writable: bool = True) -> None:
        self.path = path
        self.writable = writable

    def read(self) -> dict[str, tuple[str, str]]:
        """Every value kept, by digest, as its key in hexadecimal and the value. A
        store that does not exist holds none. Raises ``SealedError`` for a store that
        others may read, is too large, or is malformed."""
        # O_NOFOLLOW refuses a symbolic link put in the store's place, and O_NONBLOCK
        # keeps a named pipe there from blocking the open until a writer appears.
        # Either needs write access to the state directory, which a line has not.
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise SealedError(f"cannot open the sealed store {self.path}") from error
        # The type is checked on the descriptor before a file object is made of it,
        # since Python refuses a directory there with its own error, not this one.
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            _fail(f"the sealed store {self.path} is not a regular file")
        with os.fdopen(descriptor, "rb") as file:
            status = os.fstat(file.fileno())
            if status.st_mode & 0o077:
                _fail(
                    f"the sealed store {self.path} may be read by others than its"
                    " owner; make it owner only"
                )
            if status.st_size > MAX_STORE_BYTES:
                _fail(f"the sealed store {self.path} is over {MAX_STORE_BYTES} bytes")
            data = file.read(MAX_STORE_BYTES + 1)
        return _parsed(data, self.path)

    def add(self, values: Mapping[str, tuple[str, str]]) -> None:
        """Keep ``values`` beside those already kept. Raises ``SealedError``."""
        if values and self.writable:
            self._write({**self.read(), **values})

    def retain(self, digests: Iterable[str]) -> None:
        """Forget every value whose digest ``digests`` does not name, which no
        current configuration needs. Raises ``SealedError``."""
        if not self.writable:
            return
        keep = set(digests)
        kept = self.read()
        if set(kept) <= keep:
            return
        self._write({d: entry for d, entry in kept.items() if d in keep})

    def _write(self, values: Mapping[str, tuple[str, str]]) -> None:
        """Replace the store whole, through an owner-only file in the same
        directory, so a reader sees the old store or the new one."""
        text = json.dumps(
            {
                "v": STORE_VERSION,
                "values": {
                    d: {"key": key, "value": value}
                    for d, (key, value) in sorted(values.items())
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        directory = self.path.parent
        try:
            # mkstemp creates the file readable and writable by its owner only.
            descriptor, name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=directory
            )
        except OSError as error:
            raise SealedError(f"cannot write the sealed store {self.path}") from error
        try:
            with os.fdopen(descriptor, "wb") as file:
                file.write(text.encode("utf-8"))
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.path)
        except OSError as error:
            Path(name).unlink(missing_ok=True)
            raise SealedError(f"cannot write the sealed store {self.path}") from error
        _sync_directory(directory)


def _sync_directory(directory: Path) -> None:
    """Make a replacement in ``directory`` durable where the platform allows it."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _parsed(data: bytes, path: Path) -> dict[str, tuple[str, str]]:
    try:
        document: object = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        _fail(f"the sealed store {path} is not valid JSON")
    if not isinstance(document, dict):
        _fail(f"the sealed store {path} is not an object")
    top = cast(dict[str, object], document)
    if set(top) != {"v", "values"} or top["v"] != STORE_VERSION:
        _fail(f"the sealed store {path} is not of version {STORE_VERSION}")
    entries = top["values"]
    if not isinstance(entries, dict):
        _fail(f"the sealed store {path} holds no table of values")
    found: dict[str, tuple[str, str]] = {}
    for digest, entry in cast(dict[str, object], entries).items():
        if not isinstance(entry, dict):
            _fail(f"the sealed store {path} holds a malformed value")
        fields = cast(dict[str, object], entry)
        key, value = fields.get("key"), fields.get("value")
        if set(fields) != {"key", "value"} or not (
            isinstance(key, str) and isinstance(value, str)
        ):
            _fail(f"the sealed store {path} holds a malformed value")
        found[digest] = (key, value)
    return found


def _verifies(digest: str, entry: tuple[str, str]) -> bool:
    """Whether ``entry``'s value has ``digest`` under its own key."""
    try:
        key = bytes.fromhex(entry[0])
    except ValueError:
        return False
    if len(key) != KEY_BYTES:
        return False
    return hmac.compare_digest(digest_of(key, entry[1]), digest)


def _document(config: LineConfig) -> dict[str, object]:
    return cast(dict[str, object], json.loads(encode_config(config)))


def _config(document: Mapping[str, object]) -> LineConfig:
    return decode_config(json.dumps(document, ensure_ascii=False))


def _table_of(document: dict[str, object], at: Place) -> dict[str, object] | None:
    """The table that holds the last step of ``at``, or ``None``. The steps are a
    location ``SealedValue`` admits, so there are at most three to walk."""
    table: dict[str, object] = document
    for step in at[:-1]:
        inner = table.get(step)
        if not isinstance(inner, dict):
            return None
        table = cast(dict[str, object], inner)
    return table


def _named(at: Place) -> str:
    return ".".join(at)


def _fail(message: str) -> NoReturn:
    raise SealedError(message)
