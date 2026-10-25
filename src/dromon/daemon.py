"""The ``dromond`` command, the Dromon daemon for one project.

The daemon holds the project's event log as its single writer and serves the client
protocol of ``docs/protocol/CLIENT.md`` on a Unix domain socket in a private runtime
directory. It runs in the foreground. ``dromon start`` detaches it.

Lifecycle and failure modes.

- A non-blocking exclusive lock on ``dromond.lock`` admits one daemon per runtime
  directory. A second daemon exits with ``ALREADY_RUNNING`` and touches nothing.
- Holding the lock proves that no other daemon owns the socket path, so a socket file
  left by a crash is safely replaced.
- A ``stop`` request, SIGTERM or SIGINT ends the loop. Open connections are closed,
  the socket file is removed and the event log is closed. The lock file is left in
  place, because removing a lock file races with the next daemon taking it.
- Each connection has an idle timeout and a line limit, so a stalled or hostile client
  cannot hold the daemon or exhaust its memory.

Concurrency. Everything runs on one asyncio event loop thread, including the event log's
blocking SQLite calls, which are short, and the scheduler's turns, which wait on their
engine processes without blocking the loop.

Authority. Whoever can connect to the socket runs as the operator's user, so a request
carries operator authority and names the operator it speaks for. Messages from lines
arrive through their ``send_message`` tool and never through the socket.

Porting note. ``fcntl.flock`` and Unix domain sockets are POSIX facilities, available on
every target of the Rust and Keleusma ports except Windows, which is not supported.
"""

import argparse
import asyncio
import json
import contextlib
import fcntl
import os
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, cast

from dromon import __version__, exits
from dromon.driver import DriverError, Strategy
from dromon.eventlog import EventLog, LogError, utc_now
from dromon.limits import DEFAULT_LIMITS, Limits
from dromon.mailbox import MAX_PAGE, listing, outcome, replied_ids, sent, summary
from dromon.lineconfig import (
    DEFAULT_CONFIG,
    ConfigError,
    LineConfig,
    config_from_document,
)
from dromon.model import (
    EventRejected,
    InvalidField,
    LineAddress,
    Message,
    OperatorAddress,
    QuestionStatus,
    parse_address,
)
from dromon.paths import PathError, ProjectPaths, ensure_private_dir, project_paths
from dromon.protocol import (
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    ErrorCode,
    Operation,
    ProtocolError,
    Request,
    Response,
    decode_request,
    encode_response,
    request_id_of,
)
from dromon.scheduler import (
    DEFAULT_OPERATOR,
    DEFAULT_TICK_SECONDS,
    MAX_TICK_SECONDS,
    Scheduler,
    claude_code_engine,
)

VIEW_LIMIT: Final = 50
"""The most messages one ``line_view`` returns."""

VIEW_PREVIEW_CHARACTERS: Final = 600

VIEW_BUDGET_BYTES: Final = 40_000
"""Room for messages in a ``line_view`` response, below the 64 KiB line limit."""

IDLE_TIMEOUT_SECONDS: Final = 30.0
"""A connection that sends nothing for this long is closed."""

SHUTDOWN_TIMEOUT_SECONDS: Final = 5.0


class AlreadyRunning(Exception):
    """Another daemon holds the project's lock."""


def acquire_lock(path: Path) -> int:
    """Take the exclusive lock at ``path`` and return its descriptor."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise AlreadyRunning(f"another daemon holds {path}") from error
    except BaseException:
        os.close(descriptor)
        raise
    os.ftruncate(descriptor, 0)
    os.write(descriptor, f"{os.getpid()}\n".encode())
    return descriptor


@dataclass(slots=True)
class Daemon:
    """The request handling of one daemon, separate from its sockets for testing."""

    paths: ProjectPaths
    log: EventLog
    started_at: str
    scheduler: Scheduler
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)

    def respond(self, line: bytes) -> Response:
        """Answer one request line. Never raises for anything a client can send."""
        try:
            request = decode_request(line)
        except ProtocolError as error:
            return Response(
                request_id_of(line),
                ok=False,
                error_code=error.code,
                error_message=str(error),
            )
        match request.operation:
            case Operation.PING:
                return Response(request.request_id, ok=True, result={})
            case Operation.STATUS:
                return Response(request.request_id, ok=True, result=self.status())
            case Operation.STOP:
                self.stop_requested.set()
                return Response(request.request_id, ok=True, result={"stopping": True})
            case (
                Operation.CREATE_LINE
                | Operation.CONFIGURE_LINE
                | Operation.SEND
                | Operation.QUESTIONS
                | Operation.ANSWER
                | Operation.MAIL
                | Operation.READ
                | Operation.ACKNOWLEDGE
                | Operation.SENT
                | Operation.RELEASE
                | Operation.LINE_VIEW
            ):
                return self._command(request)

    def _command(self, request: Request) -> Response:
        """Run a request that changes state, turning each refusal into an error."""
        try:
            match request.operation:
                case Operation.CREATE_LINE:
                    result = self._create_line(request.args)
                case Operation.CONFIGURE_LINE:
                    result = self._configure_line(request.args)
                case Operation.QUESTIONS:
                    result = self._questions(request.args)
                case Operation.ANSWER:
                    result = self._answer(request.args)
                case Operation.MAIL:
                    result = self._mail(request.args)
                case Operation.READ:
                    result = self._read(request.args)
                case Operation.ACKNOWLEDGE:
                    result = self._acknowledge(request.args)
                case Operation.SENT:
                    result = self._sent(request.args)
                case Operation.RELEASE:
                    result = self._release(request.args)
                case Operation.LINE_VIEW:
                    result = self._line_view(request.args)
                case _:
                    result = self._send(request.args)
        except ProtocolError as error:
            return Response(
                request.request_id,
                False,
                error_code=error.code,
                error_message=str(error),
            )
        except (InvalidField, EventRejected, DriverError, ConfigError) as error:
            return Response(
                request.request_id,
                False,
                error_code=ErrorCode.REJECTED,
                error_message=str(error),
            )
        return Response(request.request_id, True, result=result)

    def _create_line(self, args: dict[str, object]) -> dict[str, object]:
        texts = {key: value for key, value in args.items() if key != "config"}
        line_id, model, strategy = _text_args(texts, ("line_id", "model", "strategy"))
        try:
            chosen = Strategy(strategy)
        except ValueError as error:
            raise DriverError(f"unknown strategy {strategy!r}") from error
        config = _config_arg(args) if "config" in args else DEFAULT_CONFIG
        self.scheduler.create_line(line_id, model, chosen, config)
        return {"line_id": line_id}

    def _configure_line(self, args: dict[str, object]) -> dict[str, object]:
        texts = {key: value for key, value in args.items() if key != "config"}
        (line_id,) = _text_args(texts, ("line_id",))
        if "config" not in args:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "configure_line needs a config")
        if line_id not in self.log.state.lines:
            raise EventRejected(f"no line {line_id}")
        self.scheduler.configure_line(line_id, _config_arg(args))
        return {"line_id": line_id}

    def _questions(self, args: dict[str, object]) -> dict[str, object]:
        """Open questions for ``operator``, or for every operator when it is omitted."""
        names = ("operator",) if "operator" in args else ()
        values = _text_args(args, names)
        operator = OperatorAddress(values[0]) if values else None
        found = self.scheduler.questions(operator)
        return {"questions": [_question_summary(i, m) for i, m in found]}

    def _answer(self, args: dict[str, object]) -> dict[str, object]:
        question_id, body, operator = _text_args(
            args, ("question_id", "body", "operator")
        )
        message_id = self.scheduler.answer(OperatorAddress(operator), question_id, body)
        return {"message_id": message_id}

    def _mail(self, args: dict[str, object]) -> dict[str, object]:
        """A page of an operator's mailbox, with its summary. Listing delivers."""
        allowed = {"operator", "unread_only", "sender", "offset", "limit"}
        if "operator" not in args or not set(args) <= allowed:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"mail takes {sorted(allowed)}, with operator"
            )
        (operator_text,) = _text_args({"operator": args["operator"]}, ("operator",))
        unread_only = args.get("unread_only", False)
        offset = args.get("offset", 0)
        limit = args.get("limit", MAX_PAGE)
        sender = args.get("sender")
        if type(unread_only) is not bool:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "unread_only is not a boolean")
        if type(offset) is not int or type(limit) is not int:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "offset and limit are integers")
        if sender is not None and not isinstance(sender, str):
            raise ProtocolError(ErrorCode.BAD_REQUEST, "sender is not a string")
        owner = OperatorAddress(operator_text)
        self.scheduler.collect(owner)
        try:
            page, total = listing(
                self.log.state,
                owner,
                unread_only=unread_only,
                sender=None if sender is None else parse_address(sender),
                offset=offset,
                limit=limit,
            )
        except ValueError as error:
            raise InvalidField(str(error)) from error
        replied = replied_ids(self.log.state, owner)
        counts = summary(self.log.state, owner, 0)
        return {
            "total": total,
            "unread": counts.unread,
            "unanswered": counts.unanswered,
            "messages": [
                {
                    "message_id": message_id,
                    "sender": str(message.sender),
                    "kind": message.kind.value,
                    "reply_to": message.reply_to,
                    "read": message.read,
                    "outcome": outcome(message_id, message, replied),
                }
                for message_id, message in page
            ],
        }

    def _line_view(self, args: dict[str, object]) -> dict[str, object]:
        """What an attached client shows: the line, its recent mail and questions.

        Bodies are previews, and the newest messages are kept within a byte budget,
        so the response always fits one protocol line however large the mail is.
        """
        allowed = {"line_id", "limit"}
        if "line_id" not in args or not set(args) <= allowed:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"line_view takes {sorted(allowed)}"
            )
        (line_id,) = _text_args({"line_id": args["line_id"]}, ("line_id",))
        limit = args.get("limit", VIEW_LIMIT)
        if type(limit) is not int or not 1 <= limit <= VIEW_LIMIT:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"limit must be in [1, {VIEW_LIMIT}]"
            )
        state = self.log.state
        if line_id not in state.lines:
            raise EventRejected(f"no line {line_id}")
        address = LineAddress(line_id)
        involved = [
            (message_id, message)
            for message_id, message in state.messages.items()
            if address in (message.sender, message.recipient)
        ]
        summaries = {
            str(entry["line_id"]): entry for entry in self.scheduler.line_summaries()
        }
        messages: list[dict[str, object]] = []
        budget = VIEW_BUDGET_BYTES
        for message_id, message in reversed(involved[-limit:]):
            preview = message.body[:VIEW_PREVIEW_CHARACTERS]
            entry: dict[str, object] = {
                "message_id": message_id,
                "sender": str(message.sender),
                "recipient": str(message.recipient),
                "kind": message.kind.value,
                "reply_to": message.reply_to,
                "status": None if message.status is None else message.status.value,
                "delivered": message.delivered,
                "read": message.read,
                "body": preview,
                "truncated": len(preview) < len(message.body),
            }
            budget -= len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
            if budget < 0:
                break
            messages.append(entry)
        messages.reverse()
        questions = [
            _question_summary(message_id, message)
            for message_id, message in involved
            if message.status is QuestionStatus.OPEN and message.sender == address
        ]
        return {
            "line": dict(summaries[line_id]),
            "tick": self.scheduler.tick,
            "messages": messages,
            "total": len(involved),
            "open_questions": questions[-VIEW_LIMIT:],
        }

    def _release(self, args: dict[str, object]) -> dict[str, object]:
        (line_id,) = _text_args(args, ("line_id",))
        self.scheduler.release(line_id)
        return {"line_id": line_id}

    def _acknowledge(self, args: dict[str, object]) -> dict[str, object]:
        operator, message_id = _text_args(args, ("operator", "message_id"))
        self.scheduler.acknowledge(OperatorAddress(operator), message_id)
        return {"message_id": message_id}

    def _sent(self, args: dict[str, object]) -> dict[str, object]:
        """A page of the mail an operator has sent, with each message's fate."""
        allowed = {"operator", "unanswered_only", "offset", "limit"}
        if "operator" not in args or not set(args) <= allowed:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"sent takes {sorted(allowed)}, with operator"
            )
        (operator_text,) = _text_args({"operator": args["operator"]}, ("operator",))
        unanswered_only = args.get("unanswered_only", False)
        offset = args.get("offset", 0)
        limit = args.get("limit", MAX_PAGE)
        if type(unanswered_only) is not bool:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, "unanswered_only is not a boolean"
            )
        if type(offset) is not int or type(limit) is not int:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "offset and limit are integers")
        try:
            page, total = sent(
                self.log.state,
                OperatorAddress(operator_text),
                unanswered_only=unanswered_only,
                offset=offset,
                limit=limit,
            )
        except ValueError as error:
            raise InvalidField(str(error)) from error
        return {
            "total": total,
            "messages": [
                {
                    "message_id": status.message_id,
                    "recipient": status.recipient,
                    "kind": status.kind,
                    "delivered": status.delivered,
                    "read": status.read,
                    "outcome": status.outcome,
                }
                for status in page
            ],
        }

    def _read(self, args: dict[str, object]) -> dict[str, object]:
        operator, message_id = _text_args(args, ("operator", "message_id"))
        message = self.scheduler.read(OperatorAddress(operator), message_id)
        return {
            "message_id": message_id,
            "sender": str(message.sender),
            "kind": message.kind.value,
            "reply_to": message.reply_to,
            "choices": list(message.choices),
            "body": message.body,
        }

    def _send(self, args: dict[str, object]) -> dict[str, object]:
        optional = ("reply_to",) if "reply_to" in args else ()
        values = _text_args(args, ("recipient", "body", "operator", *optional))
        recipient, body, operator = values[:3]
        reply_to = values[3] if optional else None
        self.scheduler.ensure_operator(OperatorAddress(operator).operator_id)
        message_id = self.scheduler.send(
            OperatorAddress(operator), parse_address(recipient), body, reply_to
        )
        return {"message_id": message_id}

    def status(self) -> dict[str, object]:
        state = self.log.state
        open_questions = sum(
            1 for m in state.messages.values() if m.status is QuestionStatus.OPEN
        )
        return {
            "daemon_version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "project": str(self.paths.root),
            "started_at": self.started_at,
            "event_log": {
                "last_seq": self.log.last_seq,
                "lines": len(state.lines),
                "operators": len(state.operators),
                "open_questions": open_questions,
            },
            "tick": self.scheduler.tick,
            "lines": self.scheduler.line_summaries(),
        }


def _question_summary(message_id: str, message: Message) -> dict[str, object]:
    return {
        "question_id": message_id,
        "line": str(message.sender),
        "operator": str(message.recipient),
        "text": message.body,
        "choices": list(message.choices),
    }


def _config_arg(args: dict[str, object]) -> LineConfig:
    value = args["config"]
    if not isinstance(value, dict):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "config is not an object")
    return config_from_document(cast(dict[str, object], value))


def _text_args(args: dict[str, object], names: tuple[str, ...]) -> tuple[str, ...]:
    """Return exactly the named text arguments, or raise ``ProtocolError``."""
    if set(args) != set(names):
        raise ProtocolError(
            ErrorCode.BAD_REQUEST,
            f"the arguments are {sorted(args)}, expected {sorted(names)}",
        )
    values: list[str] = []
    for name in names:
        value = args[name]
        if not isinstance(value, str):
            raise ProtocolError(ErrorCode.BAD_REQUEST, f"{name} is not a string")
        values.append(value)
    return tuple(values)


async def _write(writer: asyncio.StreamWriter, response: Response) -> None:
    writer.write(encode_response(response))
    await writer.drain()


async def _connection(
    daemon: Daemon, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Serve requests on one connection until EOF, timeout, error or stop."""
    try:
        while not daemon.stop_requested.is_set():
            try:
                line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT_SECONDS)
            except TimeoutError:
                break
            except ValueError:
                # The stream reader raises this when a line exceeds its limit. The rest
                # of the line is still buffered, so the connection cannot continue.
                await _write(
                    writer,
                    Response(
                        "",
                        ok=False,
                        error_code=ErrorCode.TOO_LONG,
                        error_message=f"the line exceeds {MAX_LINE_BYTES} bytes",
                    ),
                )
                break
            if not line:
                break
            if not line.endswith(b"\n"):
                await _write(
                    writer,
                    Response(
                        request_id_of(line),
                        ok=False,
                        error_code=ErrorCode.BAD_REQUEST,
                        error_message="the connection closed inside a line",
                    ),
                )
                break
            await _write(writer, daemon.respond(line))
    except (ConnectionError, BrokenPipeError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, BrokenPipeError):
            await writer.wait_closed()


async def serve(daemon: Daemon) -> None:
    """Listen on the project's socket until a stop is requested."""
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, daemon.stop_requested.set)
    writers: set[asyncio.StreamWriter] = set()

    async def accept(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        writers.add(writer)
        try:
            await _connection(daemon, reader, writer)
        finally:
            writers.discard(writer)

    socket_path = daemon.paths.socket
    server = await asyncio.start_unix_server(
        accept, path=str(socket_path), limit=MAX_LINE_BYTES
    )
    daemon.scheduler.recover()
    scheduling = asyncio.create_task(daemon.scheduler.run(daemon.stop_requested))
    try:
        # The umask already gives 0600, and this states it rather than relying on it.
        socket_path.chmod(0o600)
        print(f"dromond: serving {daemon.paths.root} on {socket_path}", file=sys.stderr)
        await daemon.stop_requested.wait()
    finally:
        daemon.stop_requested.set()
        await scheduling
        server.close()
        for writer in list(writers):
            writer.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(server.wait_closed(), SHUTDOWN_TIMEOUT_SECONDS)
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def run(
    paths: ProjectPaths,
    *,
    limits: Limits = DEFAULT_LIMITS,
    default_operator: str = DEFAULT_OPERATOR,
    hold_wakes: bool = False,
    tick_seconds: float = DEFAULT_TICK_SECONDS,
) -> int:
    """Run the daemon for ``paths`` in the foreground and return its exit status."""
    os.umask(0o077)
    try:
        ensure_private_dir(paths.state_dir)
        ensure_private_dir(paths.runtime_dir)
        lock = acquire_lock(paths.lock)
    except AlreadyRunning as error:
        print(f"dromond: {error}", file=sys.stderr)
        return exits.ALREADY_RUNNING
    except PathError as error:
        print(f"dromond: {error}", file=sys.stderr)
        return exits.FAILURE
    try:
        with EventLog(paths.event_log) as log:
            scheduler = Scheduler(
                log,
                claude_code_engine(str(paths.root)),
                limits=limits,
                default_operator=default_operator,
                hold_wakes=hold_wakes,
                tick_seconds=tick_seconds,
            )
            daemon = Daemon(paths, log, utc_now(), scheduler)
            asyncio.run(serve(daemon))
    except LogError as error:
        print(f"dromond: {error}", file=sys.stderr)
        return exits.FAILURE
    finally:
        with contextlib.suppress(FileNotFoundError):
            paths.socket.unlink()
        os.close(lock)
    print("dromond: stopped", file=sys.stderr)
    return exits.OK


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``dromond``."""
    parser = argparse.ArgumentParser(
        prog="dromond",
        description="The Dromon multi-agent harness daemon, run in the foreground.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--project", type=Path, default=Path(), help="project root, default ."
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=None,
        help="directory for the socket and lock, default .dromon/run",
    )
    parser.add_argument(
        "--max-autonomous-turns",
        type=int,
        default=DEFAULT_LIMITS.max_autonomous_turns,
        help="turns a line may take without operator mail before it is held,"
        " default %(default)s",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=DEFAULT_LIMITS.max_cost_usd,
        help="estimated cost a line may run up before it is held, default %(default)s",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=float,
        default=DEFAULT_LIMITS.cooldown_seconds,
        help="the least time between a line's turns, default %(default)s",
    )
    parser.add_argument(
        "--operator",
        default=DEFAULT_OPERATOR,
        help="who is asked about a held line no operator has messaged,"
        " default %(default)s",
    )
    parser.add_argument(
        "--hold-wakes",
        action="store_true",
        help="record messages but wake no line, for maintenance and tests",
    )
    parser.add_argument(
        "--tick-seconds",
        type=float,
        default=DEFAULT_TICK_SECONDS,
        help="the time between ticks while work is in flight, default %(default)s",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``dromond`` and return its exit status."""
    arguments = build_parser().parse_args(argv)
    project: Path = arguments.project
    runtime_dir: Path | None = arguments.runtime_dir
    try:
        paths = project_paths(project, runtime_dir)
    except PathError as error:
        print(f"dromond: {error}", file=sys.stderr)
        return exits.FAILURE
    try:
        limits = Limits(
            max_autonomous_turns=arguments.max_autonomous_turns,
            max_cost_usd=arguments.max_cost_usd,
            cooldown_seconds=arguments.cooldown_seconds,
        )
        default_operator = OperatorAddress(arguments.operator).operator_id
    except (ValueError, InvalidField) as error:
        print(f"dromond: {error}", file=sys.stderr)
        return exits.USAGE
    hold: bool = arguments.hold_wakes
    tick: float = arguments.tick_seconds
    if not 0 <= tick <= MAX_TICK_SECONDS:
        print(
            f"dromond: --tick-seconds must be in [0, {MAX_TICK_SECONDS}]",
            file=sys.stderr,
        )
        return exits.USAGE
    return run(
        paths,
        limits=limits,
        default_operator=default_operator,
        hold_wakes=hold,
        tick_seconds=tick,
    )


if __name__ == "__main__":
    sys.exit(main())
