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
blocking SQLite calls, which are short. Engine work will need its own threads or
processes later, and must then reach the log through this loop.

Porting note. ``fcntl.flock`` and Unix domain sockets are POSIX facilities, available on
every target of the Rust and Keleusma ports except Windows, which is not supported.
"""

import argparse
import asyncio
import contextlib
import fcntl
import os
import signal
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from dromon import __version__, exits
from dromon.eventlog import EventLog, LogError, utc_now
from dromon.model import QuestionStatus
from dromon.paths import PathError, ProjectPaths, ensure_private_dir, project_paths
from dromon.protocol import (
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    ErrorCode,
    Operation,
    ProtocolError,
    Response,
    decode_request,
    encode_response,
    request_id_of,
)

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
        }


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
    try:
        # The umask already gives 0600, and this states it rather than relying on it.
        socket_path.chmod(0o600)
        print(f"dromond: serving {daemon.paths.root} on {socket_path}", file=sys.stderr)
        await daemon.stop_requested.wait()
    finally:
        server.close()
        for writer in list(writers):
            writer.close()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(server.wait_closed(), SHUTDOWN_TIMEOUT_SECONDS)
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def run(paths: ProjectPaths) -> int:
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
            daemon = Daemon(paths, log, utc_now())
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
    return run(paths)


if __name__ == "__main__":
    sys.exit(main())
