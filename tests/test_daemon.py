"""The daemon's request handling, and the real daemon driven over its socket."""

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from dromon import client, exits
from dromon.daemon import Daemon, acquire_lock, AlreadyRunning
from dromon.eventlog import EventLog
from dromon.model import (
    LineAddress,
    LineCreated,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
)
from dromon.paths import PathError, ProjectPaths, project_paths
from dromon.protocol import (
    MAX_LINE_BYTES,
    ErrorCode,
    Operation,
    Response,
    decode_response,
)


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    return project_paths(tmp_path, short_runtime)


# Request handling without sockets.


def handle(paths: ProjectPaths, line: bytes) -> tuple[Response, bool]:
    paths.state_dir.mkdir(mode=0o700, exist_ok=True)
    with EventLog(paths.event_log) as log:
        log.append(LineCreated("alpha", "claude-opus-5-5"))
        log.append(OperatorRegistered("ana"))
        log.append(
            MessageSent(
                "q",
                LineAddress("alpha"),
                OperatorAddress("ana"),
                MessageKind.QUESTION,
                "?",
            )
        )
        daemon = Daemon(paths, log, "2026-10-25T00:00:00Z")
        response = daemon.respond(line)
        return response, daemon.stop_requested.is_set()


def test_ping_answers_with_an_empty_result(paths: ProjectPaths) -> None:
    response, stopping = handle(paths, b'{"id":"p","op":"ping","v":1}\n')
    assert response == Response("p", True, result={})
    assert not stopping


def test_status_reports_the_event_log(paths: ProjectPaths) -> None:
    response, _ = handle(paths, b'{"id":"s","op":"status","v":1}\n')
    assert response.ok and response.result is not None
    assert response.result["pid"] == os.getpid()
    assert response.result["project"] == str(paths.root)
    assert response.result["event_log"] == {
        "last_seq": 3,
        "lines": 1,
        "operators": 1,
        "open_questions": 1,
    }


def test_stop_answers_then_requests_the_stop(paths: ProjectPaths) -> None:
    response, stopping = handle(paths, b'{"id":"x","op":"stop","v":1}\n')
    assert response.result == {"stopping": True}
    assert stopping


def test_a_bad_request_gets_an_error_response_not_an_exception(
    paths: ProjectPaths,
) -> None:
    response, stopping = handle(paths, b'{"id":"b","op":"reboot","v":1}\n')
    assert (response.request_id, response.ok, response.error_code) == (
        "b",
        False,
        ErrorCode.UNKNOWN_OPERATION,
    )
    assert not stopping


def test_a_second_lock_holder_is_refused(paths: ProjectPaths) -> None:
    paths.runtime_dir.mkdir(mode=0o700)
    first = acquire_lock(paths.lock)
    try:
        with pytest.raises(AlreadyRunning):
            acquire_lock(paths.lock)
        assert paths.lock.read_text().strip() == str(os.getpid())
    finally:
        os.close(first)


def test_start_refuses_an_unsafe_runtime_directory(paths: ProjectPaths) -> None:
    paths.runtime_dir.mkdir(mode=0o700)
    paths.runtime_dir.chmod(0o755)
    with pytest.raises(PathError, match="Restrict it to 700"):
        client.start(paths)
    assert not paths.daemon_output.exists()


def test_a_client_finds_no_daemon_without_a_socket(paths: ProjectPaths) -> None:
    with pytest.raises(client.DaemonNotRunning):
        client.call(paths.socket, Operation.PING)
    assert not client.is_running(paths)


# The real daemon, started detached and driven over its socket.


@pytest.fixture
def running(paths: ProjectPaths) -> Iterator[tuple[ProjectPaths, int]]:
    pid = client.start(paths)
    yield paths, pid
    if client.is_running(paths):
        client.stop(paths)
    reap(pid)


def reap(pid: int) -> int | None:
    """Wait briefly for a daemon this process spawned, returning its exit status."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            finished, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return None
        if finished:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.05)
    os.kill(pid, signal.SIGKILL)
    return None


def exchange(paths: ProjectPaths, *lines: bytes) -> list[bytes]:
    """Send raw lines on one connection and collect the reply lines until it closes."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(paths.socket))
        for raw in lines:
            connection.sendall(raw)
        connection.shutdown(socket.SHUT_WR)
        received = b""
        while chunk := connection.recv(65536):
            received += chunk
    return received.splitlines(keepends=True)


@pytest.mark.socket
def test_start_status_and_stop_through_the_real_daemon(
    running: tuple[ProjectPaths, int],
) -> None:
    paths, pid = running
    status = client.call(paths.socket, Operation.STATUS)
    assert status.ok and status.result is not None
    assert status.result["pid"] == pid
    client.stop(paths)
    assert not paths.socket.exists()
    assert reap(pid) == exits.OK
    assert "dromond: stopped" in paths.daemon_output.read_text()


@pytest.mark.socket
def test_the_socket_and_directories_are_private(
    running: tuple[ProjectPaths, int],
) -> None:
    paths, _ = running
    assert stat.S_ISSOCK(paths.socket.stat().st_mode)
    assert stat.S_IMODE(paths.socket.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.runtime_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.state_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.event_log.stat().st_mode) == 0o600


@pytest.mark.socket
def test_a_connection_survives_a_bad_request(running: tuple[ProjectPaths, int]) -> None:
    paths, _ = running
    replies = exchange(paths, b"not json\n", b'{"id":"p","op":"ping","v":1}\n')
    first, second = (decode_response(reply) for reply in replies)
    assert first.error_code is ErrorCode.BAD_REQUEST
    assert second == Response("p", True, result={})


@pytest.mark.socket
def test_an_oversized_line_is_refused_and_closes_the_connection(
    running: tuple[ProjectPaths, int],
) -> None:
    paths, _ = running
    replies = exchange(paths, b"x" * (MAX_LINE_BYTES + 10) + b"\n")
    assert [decode_response(r).error_code for r in replies] == [ErrorCode.TOO_LONG]
    assert client.is_running(paths)


@pytest.mark.socket
def test_a_line_cut_off_by_the_client_is_refused(
    running: tuple[ProjectPaths, int],
) -> None:
    paths, _ = running
    replies = exchange(paths, b'{"id":"c","op":"ping","v":1}')
    response = decode_response(replies[0])
    assert (response.request_id, response.error_code) == ("c", ErrorCode.BAD_REQUEST)


@pytest.mark.socket
def test_a_second_daemon_exits_without_disturbing_the_first(
    running: tuple[ProjectPaths, int],
) -> None:
    paths, _ = running
    # A fixed argument vector built from the interpreter and test paths.
    second = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "dromon.daemon",
            "--project",
            str(paths.root),
            "--runtime-dir",
            str(paths.runtime_dir),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert second.returncode == exits.ALREADY_RUNNING
    assert client.is_running(paths)


@pytest.mark.socket
def test_sigterm_shuts_down_cleanly(running: tuple[ProjectPaths, int]) -> None:
    paths, pid = running
    os.kill(pid, signal.SIGTERM)
    assert reap(pid) == exits.OK
    assert not paths.socket.exists()


@pytest.mark.socket
def test_a_stale_socket_file_is_replaced(paths: ProjectPaths) -> None:
    paths.runtime_dir.mkdir(mode=0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(paths.socket))
    stale.close()
    assert paths.socket.exists()
    assert not client.is_running(paths)
    pid = client.start(paths)
    try:
        assert client.is_running(paths)
    finally:
        client.stop(paths)
        reap(pid)


@pytest.mark.socket
def test_the_daemon_reports_events_already_in_the_log(paths: ProjectPaths) -> None:
    paths.state_dir.mkdir(mode=0o700)
    with EventLog(paths.event_log) as log:
        log.append(LineCreated("alpha", "claude-opus-5-5"))
    pid = client.start(paths)
    try:
        status = client.call(paths.socket, Operation.STATUS)
        assert status.result is not None
        assert json.loads(json.dumps(status.result["event_log"]))["lines"] == 1
    finally:
        client.stop(paths)
        reap(pid)


@pytest.mark.socket
def test_stop_when_nothing_runs_reports_not_running(paths: ProjectPaths) -> None:
    with pytest.raises(client.DaemonNotRunning):
        client.stop(paths)
