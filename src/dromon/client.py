"""The client side of the protocol, used by ``dromon`` to reach a project's daemon."""

import os
import secrets
import signal
import socket
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from dromon.paths import ProjectPaths, ensure_private_dir
from dromon.protocol import (
    MAX_LINE_BYTES,
    ErrorCode,
    Operation,
    ProtocolError,
    Request,
    Response,
    decode_response,
    encode_request,
)

REQUEST_TIMEOUT_SECONDS: Final = 5.0
STARTUP_TIMEOUT_SECONDS: Final = 10.0
SHUTDOWN_TIMEOUT_SECONDS: Final = 10.0
POLL_INTERVAL_SECONDS: Final = 0.05


class DaemonNotRunning(Exception):
    """No daemon accepts connections on the project's socket."""


class DaemonFailed(Exception):
    """The daemon could not be started or stopped as asked."""


def call(
    socket_path: Path,
    operation: Operation,
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
    args: dict[str, object] | None = None,
) -> Response:
    """Send one request and return the daemon's response.

    Raises ``DaemonNotRunning`` when nothing listens, ``ProtocolError`` when the reply
    is malformed or answers another request, and ``TimeoutError`` when the daemon is
    too slow.
    """
    request = Request(secrets.token_hex(8), operation, dict(args or {}))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout_seconds)
        try:
            connection.connect(str(socket_path))
        except (FileNotFoundError, ConnectionRefusedError) as error:
            raise DaemonNotRunning(f"no daemon listens on {socket_path}") from error
        connection.sendall(encode_request(request))
        line = _read_line(connection)
    response = decode_response(line)
    if response.request_id != request.request_id:
        raise ProtocolError(
            ErrorCode.BAD_REQUEST,
            f"the response answers {response.request_id!r}, not {request.request_id!r}",
        )
    return response


def _read_line(connection: socket.socket) -> bytes:
    buffer = bytearray()
    # Bounded by the line limit, so the loop ends after at most that many bytes.
    while len(buffer) <= MAX_LINE_BYTES:
        chunk = connection.recv(4096)
        if not chunk:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "the daemon closed mid-response")
        buffer.extend(chunk)
        end = buffer.find(b"\n")
        if end >= 0:
            return bytes(buffer[: end + 1])
    raise ProtocolError(ErrorCode.TOO_LONG, "the response exceeds the line limit")


def is_running(paths: ProjectPaths) -> bool:
    try:
        return call(paths.socket, Operation.PING).ok
    except (DaemonNotRunning, ProtocolError, TimeoutError, OSError):
        return False


def start(paths: ProjectPaths, daemon_args: Sequence[str] = ()) -> int:
    """Start a detached daemon for ``paths`` and return its process id.

    The daemon runs in a new session with no terminal, so it survives the client. Its
    output goes to ``dromond.log`` in the runtime directory. Raises ``DaemonFailed`` if
    it exits or does not answer within the startup timeout.
    """
    ensure_private_dir(paths.state_dir)
    ensure_private_dir(paths.runtime_dir)
    output = os.open(paths.daemon_output, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    null = os.open(os.devnull, os.O_RDONLY)
    argv = [
        sys.executable,
        "-m",
        "dromon.daemon",
        "--project",
        str(paths.root),
        "--runtime-dir",
        str(paths.runtime_dir),
        *daemon_args,
    ]
    try:
        # posix_spawn leaves no subprocess object behind, which a detached daemon
        # would outlive, and starts the child in its own session.
        pid = os.posix_spawn(
            sys.executable,
            argv,
            dict(os.environ),
            file_actions=[
                (os.POSIX_SPAWN_DUP2, null, 0),
                (os.POSIX_SPAWN_DUP2, output, 1),
                (os.POSIX_SPAWN_DUP2, output, 2),
            ],
            setsid=True,
        )
    finally:
        os.close(null)
        os.close(output)
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        finished, status = os.waitpid(pid, os.WNOHANG)
        if finished:
            code = os.waitstatus_to_exitcode(status)
            raise DaemonFailed(
                f"the daemon exited with status {code}. See {paths.daemon_output}"
            )
        if is_running(paths):
            return pid
        time.sleep(POLL_INTERVAL_SECONDS)
    os.kill(pid, signal.SIGTERM)
    raise DaemonFailed(
        f"the daemon did not answer within {STARTUP_TIMEOUT_SECONDS}s."
        f" See {paths.daemon_output}"
    )


def stop(paths: ProjectPaths) -> None:
    """Ask the daemon to stop and wait until its socket is gone.

    Raises ``DaemonNotRunning`` if no daemon answers and ``DaemonFailed`` if it does
    not finish within the shutdown timeout.
    """
    response = call(paths.socket, Operation.STOP)
    if not response.ok:
        raise DaemonFailed(f"the daemon refused to stop: {response.error_message}")
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if not paths.socket.exists():
            return
        time.sleep(POLL_INTERVAL_SECONDS)
    raise DaemonFailed(f"the daemon did not stop within {SHUTDOWN_TIMEOUT_SECONDS}s")
