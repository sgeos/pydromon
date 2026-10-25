"""The daemon's request handling, and the real daemon driven over its socket."""

import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from dromon import client, exits
from typing import Any, cast

from claude_agent_sdk import SdkMcpTool

from dromon.daemon import AlreadyRunning, Daemon, acquire_lock
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.model import (
    LineAddress,
    LineCreated,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    QuestionStatus,
)
from dromon.paths import PathError, ProjectPaths, project_paths
from dromon.protocol import (
    MAX_LINE_BYTES,
    ErrorCode,
    Operation,
    Response,
    decode_response,
)
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    return project_paths(tmp_path, short_runtime)


# Request handling without sockets.


def handle(paths: ProjectPaths, line: bytes) -> tuple[Response, bool]:
    paths.state_dir.mkdir(mode=0o700, exist_ok=True)
    with EventLog(paths.event_log) as log:
        log.append(LineCreated("alpha", "claude-opus-5-5", "subscription"))
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
        daemon = Daemon(paths, log, "2026-10-25T00:00:00Z", held_scheduler(log))
        response = daemon.respond(line)
        return response, daemon.stop_requested.is_set()


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


def held_scheduler(log: EventLog) -> Scheduler:
    return Scheduler(log, no_engine, hold_wakes=True)


def request(paths: ProjectPaths, op: str, args: dict[str, object]) -> Response:
    paths.state_dir.mkdir(mode=0o700, exist_ok=True)
    with EventLog(paths.event_log) as log:
        daemon = Daemon(paths, log, "2026-10-25T00:00:00Z", held_scheduler(log))
        line = (json.dumps({"id": "r", "op": op, "v": 1, "args": args}) + "\n").encode()
        return daemon.respond(line)


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
        client.start(paths, HOLD)
    assert not paths.daemon_output.exists()


def test_a_client_finds_no_daemon_without_a_socket(paths: ProjectPaths) -> None:
    with pytest.raises(client.DaemonNotRunning):
        client.call(paths.socket, Operation.PING)
    assert not client.is_running(paths)


# Creating lines and sending messages through the protocol.


def test_create_line_records_the_line(paths: ProjectPaths) -> None:
    response = request(
        paths, "create_line", {"line_id": "beta", "model": HAIKU, "strategy": "api"}
    )
    assert response.ok and response.result == {"line_id": "beta"}
    with EventLog(paths.event_log) as log:
        line = log.state.lines["beta"]
    assert (line.engine, line.strategy) == (HAIKU, "api")


@pytest.mark.parametrize(
    ("args", "code", "reason"),
    [
        ({"line_id": "beta", "model": HAIKU}, ErrorCode.BAD_REQUEST, "arguments"),
        (
            {"line_id": "beta", "model": HAIKU, "strategy": 1},
            ErrorCode.BAD_REQUEST,
            "not a string",
        ),
        (
            {"line_id": "beta", "model": HAIKU, "strategy": "metered"},
            ErrorCode.REJECTED,
            "unknown strategy",
        ),
        (
            {"line_id": "beta", "model": "claude-sonnet-5-5", "strategy": "api"},
            ErrorCode.REJECTED,
            "no driver",
        ),
        (
            {"line_id": "Bad", "model": HAIKU, "strategy": "api"},
            ErrorCode.REJECTED,
            "identifier",
        ),
        (
            {"line_id": "alpha", "model": HAIKU, "strategy": "api"},
            ErrorCode.REJECTED,
            "exists",
        ),
    ],
)
def test_a_bad_create_line_is_refused(
    paths: ProjectPaths, args: dict[str, object], code: ErrorCode, reason: str
) -> None:
    if args.get("line_id") == "alpha":
        request(
            paths,
            "create_line",
            {"line_id": "alpha", "model": HAIKU, "strategy": "api"},
        )
    response = request(paths, "create_line", args)
    assert (response.ok, response.error_code) == (False, code)
    assert reason in (response.error_message or "")


def test_send_records_a_message_from_the_operator(paths: ProjectPaths) -> None:
    request(
        paths, "create_line", {"line_id": "alpha", "model": HAIKU, "strategy": "api"}
    )
    response = request(
        paths, "send", {"recipient": "alpha", "body": "hello", "operator": "ana"}
    )
    assert response.ok and response.result is not None
    message_id = response.result["message_id"]
    with EventLog(paths.event_log) as log:
        message = log.state.messages[str(message_id)]
        assert "ana" in log.state.operators
    assert (str(message.sender), str(message.recipient), message.body) == (
        "@ana",
        "alpha",
        "hello",
    )
    assert not message.delivered


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"recipient": "alpha", "body": "x"}, ErrorCode.BAD_REQUEST),
        ({"recipient": "nobody", "body": "x", "operator": "ana"}, ErrorCode.REJECTED),
        ({"recipient": "alpha", "body": "x", "operator": "Ana!"}, ErrorCode.REJECTED),
        (
            {"recipient": "alpha", "body": "x", "operator": "ana", "reply_to": "m-x"},
            ErrorCode.REJECTED,
        ),
        ({"recipient": "alpha", "body": 3, "operator": "ana"}, ErrorCode.BAD_REQUEST),
    ],
)
def test_a_bad_send_is_refused(
    paths: ProjectPaths, args: dict[str, object], code: ErrorCode
) -> None:
    request(
        paths, "create_line", {"line_id": "alpha", "model": HAIKU, "strategy": "api"}
    )
    response = request(paths, "send", args)
    assert (response.ok, response.error_code) == (False, code)


def test_create_line_accepts_a_configuration(paths: ProjectPaths) -> None:
    config = {"builtin_tools": ["Read"], "allowed_tools": ["mcp__docs__*"]}
    args: dict[str, object] = {
        "line_id": "beta",
        "model": HAIKU,
        "strategy": "api",
        "config": config,
    }
    assert request(paths, "create_line", args).ok
    with EventLog(paths.event_log) as log:
        stored = log.state.lines["beta"].config
    assert stored is not None and '"builtin_tools":["Read"]' in stored


def test_configure_line_replaces_the_configuration(paths: ProjectPaths) -> None:
    request(
        paths, "create_line", {"line_id": "alpha", "model": HAIKU, "strategy": "api"}
    )
    config = {"tool_policy": "allow_all"}
    response = request(paths, "configure_line", {"line_id": "alpha", "config": config})
    assert response.ok
    with EventLog(paths.event_log) as log:
        stored = log.state.lines["alpha"].config
    assert stored is not None and '"tool_policy":"allow_all"' in stored


@pytest.mark.parametrize(
    ("op", "args", "code"),
    [
        ("configure_line", {"line_id": "alpha"}, ErrorCode.BAD_REQUEST),
        ("configure_line", {"line_id": "nobody", "config": {}}, ErrorCode.REJECTED),
        ("configure_line", {"line_id": "alpha", "config": []}, ErrorCode.BAD_REQUEST),
        (
            "configure_line",
            {"line_id": "alpha", "config": {"tool_policy": "trust_me"}},
            ErrorCode.REJECTED,
        ),
        (
            "create_line",
            {
                "line_id": "beta",
                "model": HAIKU,
                "strategy": "api",
                "config": {"mcp_servers": {"dromon": {"command": "x"}}},
            },
            ErrorCode.REJECTED,
        ),
    ],
)
def test_a_bad_configuration_is_refused(
    paths: ProjectPaths, op: str, args: dict[str, object], code: ErrorCode
) -> None:
    request(
        paths, "create_line", {"line_id": "alpha", "model": HAIKU, "strategy": "api"}
    )
    response = request(paths, op, args)
    assert (response.ok, response.error_code) == (False, code)


def seed_question(paths: ProjectPaths, *, blocking: bool = False) -> str:
    """A log holding line alpha and one open question from it to @ana."""
    paths.state_dir.mkdir(mode=0o700, exist_ok=True)
    with EventLog(paths.event_log) as log:
        scheduler = held_scheduler(log)
        scheduler.ensure_operator("ana")
        if "alpha" not in log.state.lines:
            scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        return scheduler.ask(
            "alpha",
            OperatorAddress("ana"),
            "Proceed?",
            ("yes", "no"),
            blocking=blocking,
        )


def test_questions_lists_open_questions_for_an_operator(paths: ProjectPaths) -> None:
    question_id = seed_question(paths)
    mine = request(paths, "questions", {"operator": "ana"})
    theirs = request(paths, "questions", {"operator": "ben"})
    every = request(paths, "questions", {})
    assert mine.result == {
        "questions": [
            {
                "question_id": question_id,
                "line": "alpha",
                "operator": "@ana",
                "text": "Proceed?",
                "choices": ["yes", "no"],
                "blocking": False,
            }
        ]
    }
    assert theirs.result == {"questions": []}
    assert every.result == mine.result


def test_answer_records_the_operators_reply(paths: ProjectPaths) -> None:
    question_id = seed_question(paths)
    response = request(
        paths, "answer", {"question_id": question_id, "body": "yes", "operator": "ana"}
    )
    assert response.ok
    with EventLog(paths.event_log) as log:
        assert log.state.messages[question_id].status is QuestionStatus.ANSWERED
    assert request(paths, "questions", {"operator": "ana"}).result == {"questions": []}


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({"question_id": "q-x", "body": "y", "operator": "ana"}, ErrorCode.REJECTED),
        ({"question_id": "Q", "body": "y", "operator": "ben"}, ErrorCode.REJECTED),
        ({"question_id": "q-x", "body": "y"}, ErrorCode.BAD_REQUEST),
    ],
)
def test_a_bad_answer_is_refused(
    paths: ProjectPaths, args: dict[str, object], code: ErrorCode
) -> None:
    question_id = seed_question(paths)
    if args.get("question_id") == "Q":
        args = {**args, "question_id": question_id}
    response = request(paths, "answer", args)
    assert (response.ok, response.error_code) == (False, code)
    with EventLog(paths.event_log) as log:
        assert "ben" not in log.state.operators


# The real daemon, started detached and driven over its socket. Wakes are held, so no
# engine ever starts and no test touches the subscription.

HOLD = ["--hold-wakes"]


@pytest.fixture
def running(paths: ProjectPaths) -> Iterator[tuple[ProjectPaths, int]]:
    pid = client.start(paths, HOLD)
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
    pid = client.start(paths, HOLD)
    try:
        assert client.is_running(paths)
    finally:
        client.stop(paths)
        reap(pid)


@pytest.mark.socket
def test_the_daemon_reports_events_already_in_the_log(paths: ProjectPaths) -> None:
    paths.state_dir.mkdir(mode=0o700)
    with EventLog(paths.event_log) as log:
        log.append(LineCreated("alpha", "claude-opus-5-5", "subscription"))
    pid = client.start(paths, HOLD)
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


@pytest.mark.socket
def test_lines_and_messages_through_the_command_line(
    running: tuple[ProjectPaths, int], capsys: pytest.CaptureFixture[str]
) -> None:
    from dromon import cli

    paths, _ = running
    base = ["--project", str(paths.root), "--runtime-dir", str(paths.runtime_dir)]
    assert cli.main([*base, "create", "alpha"]) == exits.OK
    assert cli.main([*base, "create", "beta", "--strategy", "flexible"]) == exits.OK
    assert cli.main([*base, "send", "alpha", "hello", "--operator", "ana"]) == exits.OK
    assert cli.main([*base, "send", "nobody", "hello"]) == exits.FAILURE
    capsys.readouterr()
    status = client.call(paths.socket, Operation.STATUS)
    assert status.result is not None
    summaries = cast(list[dict[str, object]], status.result["lines"])
    by_id = {str(summary["line_id"]): summary for summary in summaries}
    assert by_id["alpha"]["pending"] == 1
    assert by_id["alpha"]["state"] == "idle"
    assert by_id["beta"]["strategy"] == "flexible"
    assert by_id["beta"]["pending"] == 0


@pytest.mark.socket
def test_an_open_question_survives_a_daemon_restart_and_is_answered(
    paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    from dromon import cli

    question_id = seed_question(paths, blocking=True)
    base = ["--project", str(paths.root), "--runtime-dir", str(paths.runtime_dir)]
    pid = client.start(paths, HOLD)
    try:
        assert cli.main([*base, "questions", "--operator", "ana"]) == exits.OK
        listed = capsys.readouterr().out
        assert f"{question_id} from alpha to @ana, blocking" in listed
        assert "choices: yes | no" in listed
    finally:
        client.stop(paths)
        reap(pid)
    pid = client.start(paths, HOLD)
    try:
        assert cli.main([*base, "questions", "--operator", "ana"]) == exits.OK
        assert question_id in capsys.readouterr().out
        assert cli.main([*base, "answer", question_id, "yes", "--operator", "ana"]) == 0
        assert cli.main([*base, "questions", "--all"]) == exits.OK
        assert "no open questions" in capsys.readouterr().out
    finally:
        client.stop(paths)
        reap(pid)
    with EventLog(paths.event_log) as log:
        assert log.state.messages[question_id].status is QuestionStatus.ANSWERED
        assert not log.state.is_blocked("alpha")
