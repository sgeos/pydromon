"""The daemon's request handling, and the real daemon driven over its socket."""

import asyncio
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

from dromon import client, daemon as daemon_module, exits, launcher
from typing import Any, cast

from claude_agent_sdk import SdkMcpTool

from dromon.activity import ActivityKind
from dromon.daemon import AlreadyRunning, Daemon, acquire_lock
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.model import (
    HoldCause,
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
    encode_response,
)
from dromon.scheduler import LineEngine, LineSpec, Scheduler
from dromon.launcher import StopPolicy, TreeRow

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
    log = cast(dict[str, object], response.result["event_log"])
    counts = {
        key: log[key] for key in ("last_seq", "lines", "operators", "open_questions")
    }
    assert counts == {"last_seq": 3, "lines": 1, "operators": 1, "open_questions": 1}
    assert log["events"] == 3 and type(log["bytes"]) is int
    assert log["compaction"] == "none"


def test_status_answers_while_a_process_table_read_hangs(
    paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    reports = paths.state_dir / "left"
    reports.mkdir(parents=True)
    launcher.write_launch(
        reports / f"1{launcher.LAUNCH_SUFFIX}",
        None,
        12345,
        "/bin/sleep",
        StopPolicy.KILL_ALL,
        True,
    )

    def no_answer(_pid: int) -> bool:
        return False

    monkeypatch.setattr(launcher, "_answers", no_answer)
    entered = threading.Event()
    release = threading.Event()

    def hanging_ps() -> list[TreeRow]:
        entered.set()
        release.wait(2)
        return []

    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, report_dir=reports, sweep_read=hanging_ps)
        daemon = Daemon(paths, log, "2026-10-25T00:00:00Z", scheduler)

        async def check() -> None:
            started = time.monotonic()
            sweep = asyncio.create_task(scheduler.sweep())
            assert await asyncio.to_thread(entered.wait, 1)
            response = daemon.respond(b'{"id":"s","op":"status","v":1}\n')
            inspection = await scheduler._process_worker.run(  # pyright: ignore[reportPrivateUsage]
                lambda: "inspection worker remained available"
            )
            elapsed = time.monotonic() - started
            release.set()
            await sweep
            assert response.ok
            assert inspection == "inspection worker remained available"
            assert elapsed < 0.2

        try:
            asyncio.run(check())
        finally:
            release.set()
        assert scheduler.stop_workers()


def test_stopping_a_daemon_joins_its_process_workers(paths: ProjectPaths) -> None:
    paths.state_dir.mkdir(mode=0o700)
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine)
        daemon = Daemon(paths, log, "2026-10-25T00:00:00Z", scheduler)

        async def exercise() -> None:
            await scheduler._process_worker.run(  # pyright: ignore[reportPrivateUsage]
                lambda: None
            )
            await scheduler._sweep_worker.run(  # pyright: ignore[reportPrivateUsage]
                lambda: None
            )
            process_thread = scheduler._process_worker._thread  # pyright: ignore[reportPrivateUsage]
            sweep_thread = scheduler._sweep_worker._thread  # pyright: ignore[reportPrivateUsage]
            response = daemon.respond(b'{"id":"s","op":"stop","v":1}\n')
            assert response.ok and daemon.stop_requested.is_set()
            await scheduler.run(daemon.stop_requested)
            assert process_thread is not None and not process_thread.is_alive()
            assert sweep_thread is not None and not sweep_thread.is_alive()

        asyncio.run(exercise())


def test_daemon_run_finally_joins_a_process_worker_with_a_running_call(
    paths: ProjectPaths,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entered = threading.Event()
    release = threading.Event()
    worker_thread: list[threading.Thread] = []

    def blocked_inspection() -> None:
        entered.set()
        release.wait(5)

    async def fake_serve(daemon: Daemon) -> None:
        task = asyncio.create_task(
            daemon.scheduler._process_worker.run(  # pyright: ignore[reportPrivateUsage]
                blocked_inspection
            )
        )
        assert await asyncio.to_thread(entered.wait, 1)
        thread = daemon.scheduler._process_worker._thread  # pyright: ignore[reportPrivateUsage]
        assert thread is not None
        worker_thread.append(thread)
        timer = threading.Timer(0.05, release.set)
        timer.start()
        task.cancel()

    monkeypatch.setattr(daemon_module, "serve", fake_serve)
    monkeypatch.setattr(daemon_module, "environment_reading_works", lambda: False)

    assert daemon_module.run(paths) == exits.OK
    assert worker_thread and not worker_thread[0].is_alive()
    assert "dromond: stopped" in capsys.readouterr().err


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
            {"line_id": "beta", "model": "claude-unreleased-0-0", "strategy": "api"},
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


def seed_question(paths: ProjectPaths) -> str:
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


def seed_mail(paths: ProjectPaths) -> list[str]:
    """Line alpha with three notes to @ana, one of them a reply from her."""
    paths.state_dir.mkdir(mode=0o700, exist_ok=True)
    with EventLog(paths.event_log) as log:
        scheduler = held_scheduler(log)
        scheduler.ensure_operator("ana")
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        alpha, ana = LineAddress("alpha"), OperatorAddress("ana")
        ids = [scheduler.send(alpha, ana, f"note {n}") for n in range(3)]
        scheduler.collect(ana)
        scheduler.send(ana, alpha, "thanks", reply_to=ids[0])
        return ids


def test_mail_lists_an_operators_mailbox_and_delivers_it(paths: ProjectPaths) -> None:
    ids = seed_mail(paths)
    response = request(paths, "mail", {"operator": "ana"})
    assert response.ok and response.result is not None
    result = response.result
    assert (result["total"], result["unread"], result["unanswered"]) == (3, 3, 2)
    assert result["own_notes"] == 0
    entries = cast(list[dict[str, object]], result["messages"])
    assert [e["message_id"] for e in entries] == ids
    assert (entries[0]["outcome"], entries[1]["outcome"]) == ("replied", "unanswered")
    assert "body" not in entries[0]


def test_mail_pages_and_filters(paths: ProjectPaths) -> None:
    ids = seed_mail(paths)
    page = request(paths, "mail", {"operator": "ana", "offset": 1, "limit": 1})
    assert page.result is not None
    entries = cast(list[dict[str, object]], page.result["messages"])
    assert [e["message_id"] for e in entries] == [ids[1]]
    nobody = request(paths, "mail", {"operator": "ana", "sender": "@ben"})
    assert nobody.result is not None and nobody.result["total"] == 0


def test_read_returns_the_body_and_marks_it_read(paths: ProjectPaths) -> None:
    ids = seed_mail(paths)
    response = request(paths, "read", {"operator": "ana", "message_id": ids[1]})
    assert response.result is not None and response.result["body"] == "note 1"
    after = request(paths, "mail", {"operator": "ana", "unread_only": True})
    assert after.result is not None and after.result["total"] == 2


@pytest.mark.parametrize(
    ("op", "args", "code"),
    [
        ("mail", {}, ErrorCode.BAD_REQUEST),
        ("mail", {"operator": "ana", "folder": "x"}, ErrorCode.BAD_REQUEST),
        ("mail", {"operator": "ana", "limit": "5"}, ErrorCode.BAD_REQUEST),
        ("mail", {"operator": "ana", "limit": 500}, ErrorCode.REJECTED),
        ("mail", {"operator": "ana", "unread_only": 1}, ErrorCode.BAD_REQUEST),
        ("read", {"operator": "ana", "message_id": "m-x"}, ErrorCode.REJECTED),
        ("read", {"operator": "ben", "message_id": "FIRST"}, ErrorCode.REJECTED),
    ],
)
def test_a_bad_mail_request_is_refused(
    paths: ProjectPaths, op: str, args: dict[str, object], code: ErrorCode
) -> None:
    ids = seed_mail(paths)
    if args.get("message_id") == "FIRST":
        args = {**args, "message_id": ids[0]}
    response = request(paths, op, args)
    assert (response.ok, response.error_code) == (False, code)


def test_an_operator_acknowledges_and_lines_see_it_in_their_sent_mail(
    paths: ProjectPaths,
) -> None:
    ids = seed_mail(paths)
    assert request(paths, "acknowledge", {"operator": "ana", "message_id": ids[1]}).ok
    mail = request(paths, "mail", {"operator": "ana"})
    assert mail.result is not None
    entries = cast(list[dict[str, object]], mail.result["messages"])
    assert [e["outcome"] for e in entries] == ["replied", "acknowledged", "unanswered"]
    assert mail.result["unanswered"] == 1
    sent = request(paths, "sent", {"operator": "ana"})
    assert sent.result is not None
    (reply,) = cast(list[dict[str, object]], sent.result["messages"])
    assert (reply["recipient"], reply["outcome"]) == ("alpha", "unanswered")


@pytest.mark.parametrize(
    ("op", "args", "code"),
    [
        ("acknowledge", {"operator": "ana", "message_id": "m-x"}, ErrorCode.REJECTED),
        ("acknowledge", {"operator": "ana"}, ErrorCode.BAD_REQUEST),
        ("sent", {}, ErrorCode.BAD_REQUEST),
        ("sent", {"operator": "ana", "unanswered_only": 1}, ErrorCode.BAD_REQUEST),
        ("sent", {"operator": "ana", "limit": 0}, ErrorCode.REJECTED),
    ],
)
def test_a_bad_acknowledge_or_sent_request_is_refused(
    paths: ProjectPaths, op: str, args: dict[str, object], code: ErrorCode
) -> None:
    seed_mail(paths)
    response = request(paths, op, args)
    assert (response.ok, response.error_code) == (False, code)


def test_release_resumes_a_held_line_and_status_shows_holds(
    paths: ProjectPaths,
) -> None:
    paths.state_dir.mkdir(mode=0o700, exist_ok=True)
    with EventLog(paths.event_log) as log:
        scheduler = held_scheduler(log)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        question_id = scheduler.hold(
            "alpha", "was held by a test", cause=HoldCause.AUTONOMOUS_TURNS
        )
    status = request(paths, "status", {})
    assert status.result is not None
    (line,) = cast(list[dict[str, object]], status.result["lines"])
    assert line["held_by"] == question_id
    assert request(paths, "release", {"line_id": "alpha"}).ok
    again = request(paths, "release", {"line_id": "alpha"})
    assert (again.ok, again.error_code) == (False, ErrorCode.REJECTED)
    unknown = request(paths, "release", {"line_id": "nobody"})
    assert (unknown.ok, unknown.error_code) == (False, ErrorCode.REJECTED)


def test_line_view_shows_the_line_its_mail_and_its_questions(
    paths: ProjectPaths,
) -> None:
    seed_question(paths)
    with EventLog(paths.event_log) as log:
        scheduler = held_scheduler(log)
        scheduler.send(OperatorAddress("ana"), LineAddress("alpha"), "hello")
    response = request(paths, "line_view", {"line_id": "alpha"})
    assert response.ok and response.result is not None
    result = response.result
    line = cast(dict[str, object], result["line"])
    assert (line["line_id"], line["state"]) == ("alpha", "idle")
    messages = cast(list[dict[str, object]], result["messages"])
    assert [m["kind"] for m in messages] == ["question", "note"]
    assert messages[1]["body"] == "hello" and messages[1]["truncated"] is False
    questions = cast(list[dict[str, object]], result["open_questions"])
    assert [q["text"] for q in questions] == ["Proceed?"]


def test_line_view_previews_and_budgets_large_mail(paths: ProjectPaths) -> None:
    seed_question(paths)
    with EventLog(paths.event_log) as log:
        scheduler = held_scheduler(log)
        for _ in range(50):
            # Three bytes a character, so the byte budget binds before the count.
            scheduler.send(
                OperatorAddress("ana"), LineAddress("alpha"), "\u65e5" * 5000
            )
    response = request(paths, "line_view", {"line_id": "alpha"})
    assert response.ok and response.result is not None
    messages = cast(list[dict[str, object]], response.result["messages"])
    assert messages and all(m["truncated"] is True for m in messages)
    assert len(messages) < 50
    assert response.result["total"] == 51
    encoded = encode_response(response)
    assert len(encoded) < MAX_LINE_BYTES


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({}, ErrorCode.BAD_REQUEST),
        ({"line_id": "alpha", "limit": 0}, ErrorCode.BAD_REQUEST),
        ({"line_id": "alpha", "limit": 51}, ErrorCode.BAD_REQUEST),
        ({"line_id": "alpha", "extra": 1}, ErrorCode.BAD_REQUEST),
        ({"line_id": "nobody"}, ErrorCode.REJECTED),
    ],
)
def test_a_bad_line_view_is_refused(
    paths: ProjectPaths, args: dict[str, object], code: ErrorCode
) -> None:
    seed_question(paths)
    response = request(paths, "line_view", args)
    assert (response.ok, response.error_code) == (False, code)


def activity_request(
    paths: ProjectPaths, args: dict[str, object], texts: Sequence[str] = ()
) -> Response:
    """A ``line_activity`` request to a daemon whose line ``alpha`` said ``texts``."""
    seed_question(paths)
    with EventLog(paths.event_log) as log:
        scheduler = held_scheduler(log)
        for text in texts:
            scheduler.activity.record("alpha", ActivityKind.TEXT, text)
        daemon = Daemon(paths, log, "2026-10-25T00:00:00Z", scheduler)
        request_line = {"id": "r", "op": "line_activity", "v": 1, "args": args}
        return daemon.respond((json.dumps(request_line) + "\n").encode())


def test_line_activity_returns_what_changed_after_a_revision(
    paths: ProjectPaths,
) -> None:
    response = activity_request(paths, {"line_id": "alpha"}, ["one", "two"])
    assert response.ok and response.result is not None
    assert response.result["seq"] == 2 and response.result["complete"] is True
    entries = cast(list[dict[str, object]], response.result["entries"])
    assert [(e["entry_id"], e["kind"], e["text"]) for e in entries] == [
        (1, "text", "one"),
        (2, "text", "two"),
    ]
    later = activity_request(paths, {"line_id": "alpha", "after": 1}, ["one", "two"])
    assert later.result is not None
    assert [
        e["text"] for e in cast(list[dict[str, object]], later.result["entries"])
    ] == ["two"]


def test_line_activity_keeps_the_newest_entries_within_the_budget(
    paths: ProjectPaths,
) -> None:
    # Three bytes a character, so the byte budget binds well before the tail's end.
    texts = ["\u65e5" * 3000] * 40
    response = activity_request(paths, {"line_id": "alpha"}, texts)
    assert response.ok and response.result is not None
    entries = cast(list[dict[str, object]], response.result["entries"])
    assert 0 < len(entries) < 40 and response.result["complete"] is False
    assert entries[-1]["entry_id"] == 40
    assert len(encode_response(response)) < MAX_LINE_BYTES


@pytest.mark.parametrize(
    ("args", "code"),
    [
        ({}, ErrorCode.BAD_REQUEST),
        ({"line_id": "alpha", "after": -1}, ErrorCode.BAD_REQUEST),
        ({"line_id": "alpha", "after": "1"}, ErrorCode.BAD_REQUEST),
        ({"line_id": "alpha", "after": True}, ErrorCode.BAD_REQUEST),
        ({"line_id": "alpha", "extra": 1}, ErrorCode.BAD_REQUEST),
        ({"line_id": "nobody"}, ErrorCode.REJECTED),
    ],
)
def test_a_bad_line_activity_request_is_refused(
    paths: ProjectPaths, args: dict[str, object], code: ErrorCode
) -> None:
    response = activity_request(paths, args)
    assert (response.ok, response.error_code) == (False, code)


def test_interrupt_reports_whether_a_turn_was_in_flight(paths: ProjectPaths) -> None:
    seed_question(paths)
    response = request(paths, "interrupt", {"line_id": "alpha"})
    assert response.ok and response.result == {"line_id": "alpha", "interrupted": False}
    unknown = request(paths, "interrupt", {"line_id": "nobody"})
    assert (unknown.ok, unknown.error_code) == (False, ErrorCode.REJECTED)
    bad = request(paths, "interrupt", {})
    assert (bad.ok, bad.error_code) == (False, ErrorCode.BAD_REQUEST)


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

    question_id = seed_question(paths)
    base = ["--project", str(paths.root), "--runtime-dir", str(paths.runtime_dir)]
    pid = client.start(paths, HOLD)
    try:
        assert cli.main([*base, "questions", "--operator", "ana"]) == exits.OK
        listed = capsys.readouterr().out
        assert f"{question_id} from alpha to @ana" in listed
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


@pytest.mark.socket
def test_an_operator_looks_through_mail_from_the_command_line(
    paths: ProjectPaths, capsys: pytest.CaptureFixture[str]
) -> None:
    from dromon import cli

    ids = seed_mail(paths)
    base = ["--project", str(paths.root), "--runtime-dir", str(paths.runtime_dir)]
    pid = client.start(paths, HOLD)
    try:
        capsys.readouterr()
        assert cli.main([*base, "mail", "--operator", "ana", "--unread"]) == exits.OK
        listed = capsys.readouterr().out
        assert listed.startswith("3 matching, 3 unread, 2 unanswered")
        assert f"{ids[0]} from alpha, note, unread, replied" in listed
        assert cli.main([*base, "read", ids[2], "--operator", "ana"]) == exits.OK
        assert capsys.readouterr().out.rstrip().endswith("note 2")
        assert cli.main([*base, "ack", ids[2], "--operator", "ana"]) == exits.OK
        assert cli.main([*base, "mail", "--operator", "ana"]) == exits.OK
        assert (
            f"{ids[2]} from alpha, note, read, acknowledged" in capsys.readouterr().out
        )
        assert cli.main([*base, "sent", "--operator", "ana"]) == exits.OK
        assert "to alpha, note, waiting, unanswered" in capsys.readouterr().out
    finally:
        client.stop(paths)
        reap(pid)


@pytest.mark.socket
def test_several_clients_attach_to_lines_at_once(paths: ProjectPaths) -> None:
    from dromon.ui.keys import Char, Named
    from dromon.ui.link import SocketDaemon
    from dromon.ui.runtime import Runtime

    seed_question(paths)
    pid = client.start(paths, HOLD)
    try:
        first = Runtime(SocketDaemon(paths), "alpha", "ana", 1000.0)
        second = Runtime(SocketDaemon(paths), "alpha", "ben", 1000.0)
        for attached, text in ((first, "from the first"), (second, "from the second")):
            for character in text:
                attached.press(Char(character))
            attached.press(Named.ENTER)
            assert attached.model.notice == "sent to alpha"
        first.press(Named.CTRL_L)
        second.press(Named.CTRL_L)
        for attached in (first, second):
            view = attached.model.view
            assert view is not None and view.line.line_id == "alpha"
            bodies = [mail.body for mail in view.mail]
            assert bodies[-2:] == ["from the first", "from the second"]
            assert attached.model.lines and attached.model.activity == ()
            assert not attached.model.alarm, attached.model.notice
    finally:
        client.stop(paths)
        reap(pid)


def test_hibernate_and_awaken_record_the_change_once_and_show_in_status(
    paths: ProjectPaths,
) -> None:
    request(
        paths, "create_line", {"line_id": "alpha", "model": HAIKU, "strategy": "api"}
    )
    first = request(paths, "hibernate", {"line_id": "alpha"})
    assert first.ok and first.result == {
        "line_id": "alpha",
        "hibernated": True,
        "changed": True,
        "running": False,
    }
    again = request(paths, "hibernate", {"line_id": "alpha"})
    assert again.ok and again.result is not None and again.result["changed"] is False
    status = request(paths, "status", {})
    assert status.result is not None
    (line,) = cast(list[dict[str, object]], status.result["lines"])
    assert (line["hibernated"], line["condition"]) == (True, "hibernated")
    off = request(paths, "awaken", {"line_id": "alpha"})
    assert off.ok and off.result == {
        "line_id": "alpha",
        "hibernated": False,
        "changed": True,
    }
    with EventLog(paths.event_log) as log:
        assert not log.state.lines["alpha"].hibernated


@pytest.mark.parametrize(
    ("op", "args", "code"),
    [
        ("hibernate", {"line_id": "nobody"}, ErrorCode.REJECTED),
        ("awaken", {"line_id": "nobody"}, ErrorCode.REJECTED),
        ("hibernate", {}, ErrorCode.BAD_REQUEST),
        ("hibernate", {"line_id": "alpha", "extra": "x"}, ErrorCode.BAD_REQUEST),
    ],
)
def test_hibernate_refuses_a_bad_request(
    paths: ProjectPaths, op: str, args: dict[str, object], code: ErrorCode
) -> None:
    request(
        paths, "create_line", {"line_id": "alpha", "model": HAIKU, "strategy": "api"}
    )
    response = request(paths, op, args)
    assert (response.ok, response.error_code) == (False, code)


def test_send_with_priority_marks_the_note(paths: ProjectPaths) -> None:
    request(
        paths, "create_line", {"line_id": "alpha", "model": HAIKU, "strategy": "api"}
    )
    args: dict[str, object] = {
        "recipient": "alpha",
        "body": "stop that",
        "operator": "ana",
        "priority": True,
    }
    response = request(paths, "send", args)
    assert response.ok and response.result is not None
    assert response.result["priority"] is True
    with EventLog(paths.event_log) as log:
        assert log.state.messages[str(response.result["message_id"])].priority


@pytest.mark.parametrize(
    ("args", "code", "reason"),
    [
        (
            {"recipient": "@ben", "body": "x", "operator": "ana", "priority": True},
            ErrorCode.REJECTED,
            "goes to a line",
        ),
        (
            {"recipient": "alpha", "body": "x", "operator": "ana", "priority": "yes"},
            ErrorCode.BAD_REQUEST,
            "priority is not a boolean",
        ),
    ],
)
def test_send_with_priority_refuses_and_records_nothing(
    paths: ProjectPaths, args: dict[str, object], code: ErrorCode, reason: str
) -> None:
    request(
        paths, "create_line", {"line_id": "alpha", "model": HAIKU, "strategy": "api"}
    )
    response = request(paths, "send", args)
    assert (response.ok, response.error_code) == (False, code)
    assert reason in (response.error_message or "")
    with EventLog(paths.event_log) as log:
        assert log.state.messages == {}
        assert "ana" not in log.state.operators


def test_create_limits_are_durable(paths: ProjectPaths) -> None:
    args: dict[str, object] = {
        "line_id": "beta",
        "model": HAIKU,
        "strategy": "api",
        "limits": {
            "max_autonomous_turns": 40,
            "max_cost_usd": "30",
            "cooldown_seconds": "2",
        },
    }
    assert request(paths, "create_line", args).ok
    with EventLog(paths.event_log) as log:
        scheduler = held_scheduler(log)
        limits = scheduler.limits_for("beta")
        assert limits.max_autonomous_turns == 40
        assert limits.max_cost_usd == 30
        assert limits.cooldown_seconds == 2


@pytest.mark.parametrize(
    "limits",
    [
        {"max_cost_usd": "0"},
        {"max_autonomous_turns": 0},
        {"cooldown_seconds": "-1"},
        {"unknown": 1},
        [],
    ],
)
def test_bad_create_limits_leave_no_line(paths: ProjectPaths, limits: object) -> None:
    args: dict[str, object] = {
        "line_id": "beta",
        "model": HAIKU,
        "strategy": "api",
        "limits": limits,
    }
    assert not request(paths, "create_line", args).ok
    with EventLog(paths.event_log) as log:
        assert "beta" not in log.state.lines


def test_create_override_encoding_failure_leaves_no_line(
    paths: ProjectPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dromon.overrides import OverrideError, Value
    from collections.abc import Mapping

    def refuse_encoding(document: Mapping[str, Value]) -> str:
        raise OverrideError("cannot encode the initial limits")

    monkeypatch.setattr(daemon_module, "encode_override", refuse_encoding)
    response = request(
        paths,
        "create_line",
        {
            "line_id": "beta",
            "model": HAIKU,
            "strategy": "api",
            "limits": {"max_cost_usd": "30"},
        },
    )
    assert not response.ok
    assert "cannot encode" in (response.error_message or "")
    with EventLog(paths.event_log) as log:
        assert "beta" not in log.state.lines


@pytest.mark.parametrize("omit", ["read_only", "fleet", "none"])
def test_start_checks_every_logged_grok_profile(
    paths: ProjectPaths, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, omit: str
) -> None:
    from dromon import grok
    from dromon.projectconfig import ProjectConfig
    from dromon.sandbox import SandboxError
    from dromon.security_paths import project_secret_paths

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    grok_home = home / grok.HOME_DIR
    grok_home.mkdir(parents=True)
    readonly = paths.root / "docs"
    readonly.mkdir()
    config = ProjectConfig(read_only_paths=("docs",))
    registry_dir = tmp_path / "registry"
    protected = daemon_module.protected_dirs(paths, config.federation, registry_dir)
    declared = tuple(p for p in protected if omit != "fleet" or "index" not in p.parts)
    snippet = grok.profile_snippet(
        grok.READER_PROFILE,
        paths.root,
        (*declared, *project_secret_paths(paths.root, paths.root)),
        home,
        () if omit == "read_only" else (readonly,),
    )
    (grok_home / "sandbox.toml").write_text(snippet)
    paths.state_dir.mkdir(exist_ok=True)
    with EventLog(paths.event_log) as log:
        log.append(LineCreated("alpha", HAIKU, "subscription"))
        log.append(LineCreated("beta", "grok-4.7", "subscription"))
        scheduler = held_scheduler(log)
        if omit == "none":
            daemon_module.check_grok_profiles(config, paths, scheduler, registry_dir)
        else:
            with pytest.raises(SandboxError, match="Grok line beta.*dromon-reader"):
                daemon_module.check_grok_profiles(
                    config, paths, scheduler, registry_dir
                )
