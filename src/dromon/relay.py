"""Forwarding of messages addressed to operators, through pluggable relays.

An operator may be reached through any channel, such as email, a chat service or a
client's question queue. A relay is the adapter for one channel. The daemon routes each
operator to a relay, forwards every undelivered message addressed to that operator, and
records delivery in the event log only after the relay accepts the message.

Delivery is at least once. If the process stops after a relay accepts a message but
before the log records its delivery, the next pass forwards it again. Every notice
carries its message identifier, so a channel or a reader can recognize a repeat.

Trust boundaries.

- Notice bodies are untrusted text from session lines. A relay must pass them to its
  channel as data and never interpret them as commands or markup beyond what the
  channel itself displays.
- Routes and relay configuration are operator configuration, trusted like the daemon's
  own settings. ``CommandRelay`` runs only the argument list it is configured with and
  never passes text through a shell.
- Relays are outbound only for now. Every adapter must eventually support a response
  path, so an operator can reply through the same channel. A reply is attributable to
  an operator only after that path authenticates them. ``docs/protocol/RELAY.md``
  states the required properties, and their design is technical debt.
"""

import asyncio
import contextlib
import json
import os
import signal
import stat
import subprocess
import sys
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol, cast

from dromon.eventlog import EventLog
from dromon.engine_environment import BASE_ENVIRONMENT, allowlisted
from dromon.processes import TreeRow, attached, deepest_first, signal_if_same
from dromon.model import (
    ID_PATTERN,
    MessageDelivered,
    MessageKind,
    OperatorAddress,
    State,
)

NOTICE_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class Notice:
    """One message for one operator, as a relay receives it."""

    message_id: str
    sender: str
    operator_id: str
    kind: MessageKind
    body: str
    choices: tuple[str, ...]
    reply_to: str | None


class RelayError(Exception):
    """A relay could not hand a notice to its channel. The notice stays pending."""


class Relay(Protocol):
    """The adapter for one channel to operators."""

    def forward(self, notice: Notice) -> None:
        """Hand ``notice`` to the channel. Returning means the channel accepted it.

        Raise ``RelayError`` for a failure that should leave the notice pending. Any
        other exception is a defect in the relay and propagates to the caller.
        """
        ...


@dataclass(frozen=True, slots=True)
class ForwardReport:
    delivered: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]
    """Each failed message identifier with the relay's reason."""
    unrouted: tuple[str, ...]
    """Messages whose operator has no relay. They stay pending."""


def pending_notices(state: State) -> list[Notice]:
    """Every undelivered message addressed to an operator, in the order sent."""
    notices: list[Notice] = []
    for message_id, message in state.messages.items():
        recipient = message.recipient
        if message.delivered or not isinstance(recipient, OperatorAddress):
            continue
        notices.append(
            Notice(
                message_id,
                str(message.sender),
                recipient.operator_id,
                message.kind,
                message.body,
                message.choices,
                message.reply_to,
            )
        )
    return notices


def forward_pending(log: EventLog, routes: Mapping[str, Relay]) -> ForwardReport:
    """Forward each pending operator message through its operator's relay.

    One failing relay does not stop the others. Delivery is appended to ``log`` after
    each acceptance, so a later failure cannot undo an earlier record.
    """
    delivered: list[str] = []
    failed: list[tuple[str, str]] = []
    unrouted: list[str] = []
    for notice in pending_notices(log.state):
        relay = routes.get(notice.operator_id)
        if relay is None:
            unrouted.append(notice.message_id)
            continue
        try:
            relay.forward(notice)
        except RelayError as error:
            failed.append((notice.message_id, str(error)))
            continue
        log.append(MessageDelivered(notice.message_id))
        delivered.append(notice.message_id)
    return ForwardReport(tuple(delivered), tuple(failed), tuple(unrouted))


def encode_notice(notice: Notice) -> str:
    """Return ``notice`` as canonical JSON, the input every command relay receives."""
    document = {
        "body": notice.body,
        "choices": list(notice.choices),
        "kind": notice.kind.value,
        "message_id": notice.message_id,
        "operator_id": notice.operator_id,
        "reply_to": notice.reply_to,
        "sender": notice.sender,
        "v": NOTICE_VERSION,
    }
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


class CommandRelay:
    """A relay that runs a configured command once per notice.

    The notice arrives on standard input as the JSON of ``encode_notice``, followed by
    a newline. Exit status 0 means the channel accepted it. Any other status, a timeout
    or a failure to start the command is a ``RelayError``. This lets a short script
    send email, post to a chat service or write to a file without Dromon knowing the
    channel.

    Porting note. A port needs only process spawning with an argument vector, standard
    input, a pipe for standard error that it can close itself, and a timeout, which
    every target platform has. The standard error pipe is made by Dromon and closed
    after the command exits, with public interfaces only.
    """

    def __init__(
        self,
        argv: Sequence[str],
        timeout_seconds: float = 30.0,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        if not argv or not all(argv):
            raise ValueError("argv must be a non-empty sequence of non-empty strings")
        if not 0 < timeout_seconds <= 3600:
            raise ValueError("timeout_seconds must be in (0, 3600]")
        self._argv = tuple(argv)
        self._timeout = timeout_seconds
        self._environment = os.environ if environment is None else environment

    def forward(self, notice: Notice) -> None:
        payload = encode_notice(notice) + "\n"
        try:
            # The argument vector is operator configuration, and no shell is involved,
            # so notice text cannot change what runs.
            completed = subprocess.run(  # noqa: S603
                self._argv,
                input=payload.encode("utf-8"),
                capture_output=True,
                env=allowlisted(self._environment, BASE_ENVIRONMENT),
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RelayError(
                f"the relay command timed out after {self._timeout}s"
            ) from error
        except OSError as error:
            raise RelayError(f"the relay command could not start: {error}") from error
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()[:200]
            raise RelayError(
                f"the relay command exited with status {completed.returncode}: {detail}"
            )

    async def deliver(self, notice: Notice) -> None:
        """Hand ``notice`` to the command without blocking the event loop.

        The command runs in its own process group. A timeout or daemon stop kills that
        group and every descendant a walk of the process table finds below the command,
        including one that started a session of its own. It does not reach a
        descendant whose parent had exited, as ``_end_group`` says. Standard output is
        discarded and at most ``STDERR_KEPT`` bytes of standard error are kept. The
        command's exit status determines acceptance; a descendant holding standard error
        open does not extend the wait after the command exits. Raises ``RelayError`` as
        ``forward`` does."""
        payload = (encode_notice(notice) + "\n").encode("utf-8")
        tail = _StderrTail(asyncio.get_running_loop())
        try:
            # As in ``forward``, the argument vector is operator configuration and no
            # shell is involved.
            process = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=tail.write,
                env=allowlisted(self._environment, BASE_ENVIRONMENT),
                start_new_session=True,
            )
        except OSError as error:
            tail.close()
            raise RelayError(f"the relay command could not start: {error}") from error
        except BaseException:
            tail.close()
            raise
        finally:
            # The command holds its own copy now, or never started.
            tail.release_write()
        try:
            await asyncio.wait_for(_feed(process, payload), self._timeout)
            detail = tail.finish()
        except TimeoutError as error:
            try:
                await _end_group(process)
            finally:
                # A cancellation during the end wait must not leave the descriptor and
                # the loop's reader behind. **Re-review of 4da3e0e.**
                tail.close()
            raise RelayError(
                f"the relay command timed out after {self._timeout}s"
            ) from error
        except BaseException:
            try:
                await _end_group(process)
            finally:
                tail.close()
            raise
        if process.returncode != 0:
            text = detail.decode("utf-8", errors="replace").strip()[:200]
            raise RelayError(
                f"the relay command exited with status {process.returncode}: {text}"
            )


STDERR_KEPT: Final = 4096
"""The bytes of a relay command's standard error kept for its failure's reason."""

STDERR_CHUNK: Final = 65536
STDERR_DRAIN_READS: Final = 16
"""The most reads that empty the pipe once the command has exited. Each takes at most
``STDERR_CHUNK`` bytes, so a descendant that keeps writing cannot extend the drain."""


class _StderrTail:
    """The read end of a pipe Dromon makes for a command's standard error, and its
    first ``STDERR_KEPT`` bytes.

    **Round 15, item 1.** The command once wrote to a pipe that asyncio's subprocess
    transport owned, and the end of the wait closed it through the transport's
    private attribute, so a descendant holding the write end could not hold the
    result. Dromon owns this pipe, so it closes it itself, with public interfaces
    only: ``os.pipe``, a descriptor passed as ``stderr``, and the loop's
    ``add_reader``. **Porting note.** A port needs a pipe it can close on its own
    schedule, which every platform has.

    **Behaviour kept.** Bytes the command wrote before it exited are read before the
    pipe is closed, up to ``STDERR_DRAIN_READS`` reads, and a descendant that still
    holds the write end is not waited for."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._read, self.write = os.pipe()
        self._kept = bytearray()
        self._reading = True
        try:
            os.set_blocking(self._read, False)
            loop.add_reader(self._read, self._pull)
        except BaseException:
            # A loop that cannot watch the pipe must not leave both ends open.
            self._reading = False
            os.close(self._read)
            os.close(self.write)
            self.write = -1
            raise

    def release_write(self) -> None:
        """Close the daemon's copy of the write end, once the command has its own."""
        if self.write >= 0:
            os.close(self.write)
            self.write = -1

    def _pull(self) -> bool:
        """Read once. ``True`` when nothing more can be read now, at the end of the
        stream or with the pipe empty."""
        if not self._reading:
            return True
        try:
            chunk = os.read(self._read, STDERR_CHUNK)
        except BlockingIOError:
            return True
        except OSError:
            chunk = b""
        if not chunk:
            self._loop.remove_reader(self._read)
            return True
        if len(self._kept) < STDERR_KEPT:
            self._kept += chunk[: STDERR_KEPT - len(self._kept)]
        return False

    def finish(self) -> bytes:
        """Take what the pipe holds, close it, and return the kept bytes."""
        for _ in range(STDERR_DRAIN_READS):
            if self._pull():
                break
        self.close()
        return bytes(self._kept)

    def close(self) -> None:
        """Close both ends. Safe to call again."""
        self.release_write()
        if self._reading:
            self._reading = False
            self._loop.remove_reader(self._read)
            os.close(self._read)


async def _feed(process: "asyncio.subprocess.Process", payload: bytes) -> None:
    """Feed the command its input and wait for it to exit. Standard error is read by
    the ``_StderrTail`` the caller made, and not waited for."""
    stdin = process.stdin
    if stdin is None:
        raise RelayError("the relay command has no input pipe")
    try:
        stdin.write(payload)
        await stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        stdin.close()
    await process.wait()


ATTACHED_WAIT_SECONDS: Final = 3.0
"""How long ending a command waits for the walk that finds its descendants."""


async def _end_group(process: "asyncio.subprocess.Process") -> None:
    """Kill the command's process group and its descendants, and reap the command.

    **Round 15, item 2.** The walk finds the descendants by ancestry, which includes
    one that called ``setsid`` and so is in a group of its own, and each is killed
    with the group. A descendant whose parent had already exited, or that is started
    after the walk, is not found. An unreadable table or a walk that takes longer than
    ``ATTACHED_WAIT_SECONDS`` leaves the group kill alone.

    **Why the group is not frozen first, as the launcher freezes the harness's.**
    Observed on Python 3.15 on macOS. Stopping the group with ``SIGSTOP`` makes the
    event loop's child watcher call a blocking ``os.waitpid`` for the stopped
    command, which blocks the loop until the command exits, which only the kill ends.
    The loop therefore deadlocks. The cost of not freezing is that a descendant can
    fork between the walk and the kill, and that child is not found.

    Process groups are POSIX, and a port to another platform needs its own way to end
    the group and its descendants."""
    if process.returncode is None:
        rows: list[TreeRow] = []
        try:
            try:
                walked = await asyncio.wait_for(
                    asyncio.to_thread(attached, process.pid), ATTACHED_WAIT_SECONDS
                )
            except TimeoutError:
                walked = None
            rows = walked or []
        finally:
            try:
                # Before the group, which orphans what it ends. Each is checked against
                # the walk just before its signal, as ``signal_if_same`` says.
                for row in deepest_first(rows):
                    signal_if_same(row, signal.SIGKILL)
            finally:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
    await asyncio.shield(process.wait())


class FileRelay:
    """A relay that appends each notice to one file as a line of JSON, so an
    operator's session can follow it with ``tail -f``. Round 11, item 2.

    The file is created readable and writable by its owner alone, and it is refused,
    and the notice left pending, when it is outside a protected state or runtime
    directory, is a symbolic link, is not a regular file, belongs to another user or
    is open to group or others. **Fact.** The file grows without bound. Truncating it
    is the operator's, and ``tail -F`` follows a truncated file.

    Porting note. ``O_NOFOLLOW``, ``geteuid`` and permission bits are POSIX."""

    def __init__(
        self,
        path: Path,
        state_runtime_dirs: Sequence[Path],
    ) -> None:
        if not path.is_absolute():
            raise ValueError("a file relay's path must be absolute")
        self._path = path
        self._state_runtime_dirs = tuple(state_runtime_dirs)

    @property
    def path(self) -> Path:
        return self._path

    def forward(self, notice: Notice) -> None:
        refusal = sink_refusal(self._path, self._state_runtime_dirs)
        if refusal is not None:
            raise RelayError(refusal)
        line = (encode_notice(notice) + "\n").encode("utf-8")
        flags = (
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | os.O_NOFOLLOW
            | os.O_CLOEXEC
            | os.O_NONBLOCK
        )
        try:
            descriptor = os.open(self._path, flags, 0o600)
        except OSError as error:
            raise RelayError(f"the notice file could not be opened: {error}") from error
        try:
            found = os.fstat(descriptor)
            if not stat.S_ISREG(found.st_mode):
                raise RelayError("the notice file is not a regular file")
            if found.st_uid != os.geteuid():
                raise RelayError("the notice file belongs to another user")
            if found.st_mode & 0o077:
                raise RelayError(
                    "the notice file is open to group or others, so it is not written"
                )
            written = 0
            # Bounded by the line's length, since each write moves at least one byte.
            while written < len(line):
                written += os.write(descriptor, line[written:])
            os.fsync(descriptor)
        except OSError as error:
            raise RelayError(
                f"the notice file could not be written: {error}"
            ) from error
        finally:
            os.close(descriptor)

    async def deliver(self, notice: Notice) -> None:
        """Append ``notice``. **Assumption.** One short append to a local file does
        not stall the event loop noticeably, so it runs on the loop. A file on a
        hung network mount would stall it, which is why the path is documented as
        local."""
        self.forward(notice)


def sink_refusal(path: Path, state_runtime_dirs: Sequence[Path]) -> str | None:
    """Why a notice file at ``path`` may not be written, or ``None``.

    **Decided by core for review, round 11.** A notice file may not lie in any line's
    protected state or runtime directory, which every enabled engine sandbox denies
    to lines. A line without a sandbox reaches the file there, since it runs as the
    daemon's user."""
    parent = Path(os.path.realpath(path.parent))
    real = parent / path.name
    if not parent.is_dir():
        return f"the notice file's directory {path.parent} does not exist"
    if not any(
        _encloses(Path(os.path.realpath(directory)), real)
        for directory in state_runtime_dirs
    ):
        return (
            f"the notice file {path} must lie under a Dromon protected state or"
            " runtime directory, which every enabled engine sandbox denies to lines"
        )
    return None


def _encloses(outer: Path, inner: Path) -> bool:
    return inner == outer or outer in inner.parents


class AsyncRelay(Protocol):
    """A relay the daemon's relayer drives, which must not block the event loop."""

    async def deliver(self, notice: Notice) -> None:
        """As ``Relay.forward``, awaited."""
        ...


# The project file's relays and routes. Round 11, item 1.


class RelayKind(StrEnum):
    COMMAND = "command"
    FILE = "file"


DEFAULT_TIMEOUT_SECONDS: Final = 30.0
MAX_RELAYS: Final = 32
MAX_ARGUMENTS: Final = 64
MAX_ARGUMENT_BYTES: Final = 4096
RELAY_KEYS: Final = frozenset({"kind", "command", "path", "timeout_seconds"})
OPERATOR_KEYS: Final = frozenset({"relay"})


@dataclass(frozen=True, slots=True)
class RelayDefinition:
    """One ``[relays.NAME]`` table."""

    kind: RelayKind
    command: tuple[str, ...] = ()
    """The argument vector of a command relay."""
    path: str | None = None
    """The notice file of a file relay, as written, absolute or relative to the
    project root, or under ``~``."""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class RelayConfig:
    relays: dict[str, RelayDefinition] = field(
        default_factory=dict[str, RelayDefinition]
    )
    routes: dict[str, str] = field(default_factory=dict[str, str])
    """Each operator identifier with the name of its relay."""


NO_RELAYS: Final = RelayConfig()


class RelayConfigError(ValueError):
    """A ``[relays]`` or ``[operators]`` table is not valid. ``key`` places it."""

    def __init__(self, key: str, message: str) -> None:
        super().__init__(f"{key} {message}")
        self.key = key
        self.reason = message


def _tables(value: object, key: str) -> dict[str, dict[str, object]]:
    if not isinstance(value, dict):
        raise RelayConfigError(key, "must be a table")
    found: dict[str, dict[str, object]] = {}
    for name, table in cast(dict[object, object], value).items():
        if not isinstance(name, str) or ID_PATTERN.fullmatch(name) is None:
            raise RelayConfigError(f"{key}.{name}", "is not an identifier")
        if not isinstance(table, dict):
            raise RelayConfigError(f"{key}.{name}", "must be a table")
        found[name] = cast(dict[str, object], table)
    return found


def parse_relays(relays: object, operators: object) -> RelayConfig:
    """The ``[relays]`` and ``[operators]`` tables, either absent as ``None``.
    Raises ``RelayConfigError``."""
    definitions: dict[str, RelayDefinition] = {}
    tables = {} if relays is None else _tables(relays, "relays")
    if len(tables) > MAX_RELAYS:
        raise RelayConfigError("relays", f"holds more than {MAX_RELAYS} relays")
    for name, table in sorted(tables.items()):
        definitions[name] = _relay(f"relays.{name}", table)
    routes: dict[str, str] = {}
    operator_tables = {} if operators is None else _tables(operators, "operators")
    for operator_id, table in sorted(operator_tables.items()):
        key = f"operators.{operator_id}"
        unknown = sorted(set(table) - OPERATOR_KEYS)
        if unknown:
            raise RelayConfigError(f"{key}.{unknown[0]}", "is not a known key")
        relay = table.get("relay")
        if not isinstance(relay, str) or relay not in definitions:
            raise RelayConfigError(
                f"{key}.relay", f"must name a relay of [relays], found {relay!r}"
            )
        routes[operator_id] = relay
    return RelayConfig(definitions, routes)


def _relay(key: str, table: dict[str, object]) -> RelayDefinition:
    unknown = sorted(set(table) - RELAY_KEYS)
    if unknown:
        raise RelayConfigError(f"{key}.{unknown[0]}", "is not a known key")
    raw_kind = table.get("kind", RelayKind.COMMAND.value)
    if raw_kind not in {kind.value for kind in RelayKind}:
        raise RelayConfigError(f"{key}.kind", "must be command or file")
    kind = RelayKind(cast(str, raw_kind))
    timeout = table.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if (
        not isinstance(timeout, int | float)
        or isinstance(timeout, bool)
        or not 0 < timeout <= 3600
    ):
        raise RelayConfigError(
            f"{key}.timeout_seconds", "must be a number in (0, 3600]"
        )
    if kind is RelayKind.FILE:
        if "command" in table:
            raise RelayConfigError(f"{key}.command", "is for a command relay")
        path = table.get("path")
        if not isinstance(path, str) or not path or "\x00" in path:
            raise RelayConfigError(f"{key}.path", "must be a path to a file")
        return RelayDefinition(kind, path=path, timeout_seconds=float(timeout))
    if "path" in table:
        raise RelayConfigError(f"{key}.path", "is for a file relay")
    command = table.get("command")
    if (
        not isinstance(command, list)
        or not 1 <= len(cast(list[object], command)) <= MAX_ARGUMENTS
    ):
        raise RelayConfigError(
            f"{key}.command", f"must be a list of 1 to {MAX_ARGUMENTS} arguments"
        )
    argv: list[str] = []
    for argument in cast(list[object], command):
        if (
            not isinstance(argument, str)
            or not argument
            or "\x00" in argument
            or len(argument.encode("utf-8")) > MAX_ARGUMENT_BYTES
        ):
            raise RelayConfigError(
                f"{key}.command",
                f"must hold non-empty text arguments of at most {MAX_ARGUMENT_BYTES}"
                " bytes",
            )
        argv.append(argument)
    return RelayDefinition(kind, command=tuple(argv), timeout_seconds=float(timeout))


def sink_path(definition: RelayDefinition, root: Path) -> Path:
    """The absolute notice file of a file relay, before symbolic links resolve."""
    if definition.path is None:
        raise ValueError("a command relay has no notice file")
    written = definition.path
    if written == "~" or written.startswith("~/"):
        return Path(os.path.expanduser(written))
    given = Path(written)
    return given if given.is_absolute() else root / given


def build_routes(
    config: RelayConfig,
    root: Path,
    state_runtime_dirs: Sequence[Path],
) -> dict[str, AsyncRelay]:
    """Each routed operator's relay. Raises ``RelayConfigError`` for a file relay
    whose notice file is refused now, so a start or a reload names it, rather than
    the first notice."""
    built: dict[str, AsyncRelay] = {}
    for name, definition in sorted(config.relays.items()):
        if definition.kind is RelayKind.FILE:
            path = sink_path(definition, root)
            refusal = sink_refusal(path, state_runtime_dirs)
            if refusal is not None:
                raise RelayConfigError(f"relays.{name}.path", refusal)
            built[name] = FileRelay(path, state_runtime_dirs)
        else:
            built[name] = CommandRelay(definition.command, definition.timeout_seconds)
    return {operator: built[name] for operator, name in config.routes.items()}


# The daemon's relayer. Round 11, item 1.

FIRST_BACKOFF_SECONDS: Final = 5.0
MAX_BACKOFF_SECONDS: Final = 300.0
SWEEP_SECONDS: Final = 60.0
"""How often a pass runs with no notice, to forward mail that a path recorded
without telling the relayer, and unrouted mail once a route appears."""
MAX_REPORTED: Final = 10


class RelayMail(Protocol):
    """What the relayer needs of the scheduler."""

    @property
    def state(self) -> State: ...

    def relayed(self, message_id: str) -> bool: ...


@dataclass(slots=True)
class _Failure:
    operator_id: str
    reason: str
    attempts: int
    not_before: float


class OperatorRelayer:
    """Forwards operators' undelivered mail through their relays, as ``RELAY.md``
    specifies, one notice at a time per relay instance, from a bounded set of tasks.

    - A pass runs when mail to an operator is recorded, when the routes change, when
      a failed notice's backoff ends, and every ``SWEEP_SECONDS`` in any case.
    - A notice its relay accepts is recorded delivered through ``RelayMail.relayed``,
      unless a client delivered it while the relay ran.
    - A notice its relay refuses stays undelivered and is tried again after a
      backoff that doubles from ``FIRST_BACKOFF_SECONDS`` to ``MAX_BACKOFF_SECONDS``.
    - A notice whose operator has no route stays undelivered and is counted.

    Memory. Backoff and failures are kept in memory, so a restart tries every
    undelivered notice at once. Delivery is at least once, since a stop between a
    relay's acceptance and the record forwards the notice again.

    Porting note. ``step`` is one bounded pass over the mail and says how long to
    wait, so the relayer maps onto a stream processor whose host performs the
    relay."""

    def __init__(
        self,
        mail: RelayMail,
        routes: Mapping[str, AsyncRelay],
        clock: Callable[[], float],
    ) -> None:
        self._mail = mail
        self._routes = dict(routes)
        self._clock = clock
        self._failures: dict[str, _Failure] = {}
        self._unrouted = 0
        self.delivered = 0
        """Notices recorded delivered by this relayer since the daemon started."""
        self.ready = asyncio.Event()
        self.ready.set()

    def notify(self) -> None:
        self.ready.set()

    def set_routes(self, routes: Mapping[str, AsyncRelay]) -> None:
        """Take the routes of a reloaded project file. A notice already handed to
        its old relay finishes there. Failures are forgotten at every reload, changed
        routes or not, so a changed route is tried at once."""
        self._routes = dict(routes)
        self._failures.clear()
        self.ready.set()

    @property
    def routed(self) -> tuple[str, ...]:
        return tuple(sorted(self._routes))

    async def step(self) -> float:
        """One forwarding pass, and how long until the next unless notified."""
        unrouted = 0
        now = self._clock()
        per_relay: dict[int, tuple[AsyncRelay, list[Notice]]] = {}
        for notice in pending_notices(self._mail.state):
            relay = self._routes.get(notice.operator_id)
            if relay is None:
                unrouted += 1
                continue
            group = per_relay.setdefault(id(relay), (relay, []))
            group[1].append(notice)

        async def forward_relay(relay: AsyncRelay, notices: list[Notice]) -> None:
            for notice in notices:
                failure = self._failures.get(notice.message_id)
                if failure is not None and failure.not_before > now:
                    continue
                current = self._mail.state.messages.get(notice.message_id)
                if current is None or current.delivered:
                    continue
                try:
                    await relay.deliver(notice)
                except RelayError as error:
                    self._fail(notice, str(error))
                    continue
                except Exception as error:  # noqa: BLE001
                    traceback.print_exception(error, file=sys.stderr)
                    self._fail(notice, f"the relay failed unexpectedly: {error!r}")
                    continue
                self._failures.pop(notice.message_id, None)
                if self._mail.relayed(notice.message_id):
                    self.delivered += 1

        await asyncio.gather(
            *(
                forward_relay(relay, notices)
                for _, (relay, notices) in sorted(per_relay.items())
            )
        )
        self._unrouted = unrouted
        # Mail that is no longer pending, delivered by a client or not, is forgotten.
        pending = {notice.message_id for notice in pending_notices(self._mail.state)}
        for message_id in [m for m in self._failures if m not in pending]:
            del self._failures[message_id]
        waits = [f.not_before - self._clock() for f in self._failures.values()]
        return max(0.0, min([SWEEP_SECONDS, *waits]))

    def _fail(self, notice: Notice, reason: str) -> None:
        earlier = self._failures.get(notice.message_id)
        attempts = 1 if earlier is None else earlier.attempts + 1
        backoff = min(FIRST_BACKOFF_SECONDS * 2 ** (attempts - 1), MAX_BACKOFF_SECONDS)
        self._failures[notice.message_id] = _Failure(
            notice.operator_id, reason, attempts, self._clock() + backoff
        )
        print(
            f"dromond: the relay for @{notice.operator_id} refused"
            f" {notice.message_id}: {reason}",
            file=sys.stderr,
        )

    def report(self) -> dict[str, object]:
        """The relayer's state for ``dromon status``, with at most ``MAX_REPORTED``
        failing notices, oldest attempt first."""
        failing = sorted(self._failures.items(), key=lambda item: item[1].not_before)
        return {
            "routed": list(self.routed),
            "delivered": self.delivered,
            "unrouted": self._unrouted,
            "failing": len(failing),
            "failures": [
                {
                    "message_id": message_id,
                    "operator": failure.operator_id,
                    "attempts": failure.attempts,
                    "reason": failure.reason,
                    "retry_in_seconds": max(
                        0.0, round(failure.not_before - self._clock(), 1)
                    ),
                }
                for message_id, failure in failing[:MAX_REPORTED]
            ],
        }

    async def run(self, stop: asyncio.Event) -> None:
        """Forward until ``stop`` is set. A pass that raises is reported and waited
        out, so the relayer neither dies nor spins."""
        while not stop.is_set():
            self.ready.clear()
            try:
                wait = await self.step()
            except Exception as error:  # noqa: BLE001
                traceback.print_exception(error, file=sys.stderr)
                wait = FIRST_BACKOFF_SECONDS
            waits = [
                asyncio.ensure_future(stop.wait()),
                asyncio.ensure_future(self.ready.wait()),
            ]
            await asyncio.wait(waits, timeout=wait, return_when=asyncio.FIRST_COMPLETED)
            for pending in waits:
                pending.cancel()
