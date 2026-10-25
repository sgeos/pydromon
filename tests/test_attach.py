"""The attached client: parsing, rendering, commands and the curses loop."""

import curses
from pathlib import Path

import pytest

from dromon.attach import (
    HELP,
    Acknowledge,
    Answer,
    Attachment,
    Help,
    Invalid,
    Quit,
    Release,
    Say,
    parse_input,
    render,
)
from dromon.client import DaemonNotRunning
from dromon.paths import ProjectPaths, project_paths
from dromon.protocol import ErrorCode, Operation, Response
from dromon import tui


@pytest.mark.parametrize(
    ("text", "command"),
    [
        ("hello there", Say("hello there")),
        ("  padded  ", Say("padded")),
        ("/answer q-1 yes, go", Answer("q-1", "yes, go")),
        ("/ack m-2", Acknowledge("m-2")),
        ("/release", Release()),
        ("/help", Help()),
        ("/quit", Quit()),
        ("/exit", Quit()),
        ("/answer q-1", Invalid("usage: /answer QUESTION TEXT")),
        ("/ack", Invalid("usage: /ack MESSAGE")),
        ("/ack a b", Invalid("usage: /ack MESSAGE")),
        ("/release now", Invalid("usage: /release")),
    ],
)
def test_input_parses_into_commands(text: str, command: object) -> None:
    assert parse_input(text) == command


def test_empty_input_is_no_command() -> None:
    assert parse_input("   ") is None


def test_an_unknown_command_names_the_help() -> None:
    parsed = parse_input("/frobnicate")
    assert isinstance(parsed, Invalid) and HELP in parsed.reason


VIEW: dict[str, object] = {
    "line": {
        "line_id": "alpha",
        "engine": "claude-haiku-5-5",
        "strategy": "subscription",
        "state": "idle",
        "turns": 3,
        "pending": 1,
        "held_by": "q-9",
    },
    "tick": 42,
    "total": 3,
    "messages": [
        {
            "message_id": "m-1",
            "sender": "@ana",
            "recipient": "alpha",
            "kind": "note",
            "reply_to": None,
            "status": None,
            "body": "first line\nsecond line",
            "truncated": False,
        },
        {
            "message_id": "q-9",
            "sender": "alpha",
            "recipient": "@ana",
            "kind": "question",
            "reply_to": None,
            "status": "open",
            "body": "x" * 30,
            "truncated": True,
        },
    ],
    "open_questions": [
        {"question_id": "q-9", "operator": "@ana", "text": "Resume alpha?"}
    ],
}


def test_the_view_renders_the_line_its_mail_and_its_questions() -> None:
    lines = render(VIEW, 60)
    text = "\n".join(lines)
    assert lines[0].startswith("alpha  claude-haiku-5-5 subscription  idle  tick 42")
    assert "HELD until q-9 is answered, or /release" in text
    assert "... 1 earlier messages not shown" in text
    assert "m-1 @ana -> alpha note" in text
    assert "  first line" in text and "  second line" in text
    assert "q-9 alpha -> @ana question [open]" in text
    assert "x" * 30 + " ..." in text
    assert "Open questions from this line:" in text
    assert all(len(line) <= 60 for line in lines)


def test_rendering_survives_a_tiny_width_and_an_empty_view() -> None:
    assert all(len(line) <= 20 for line in render(VIEW, 5))
    assert render({}, 40)[0] is not None


class FakeDaemon:
    def __init__(self, response: Response | Exception) -> None:
        self.response = response
        self.calls: list[tuple[Operation, dict[str, object]]] = []

    def __call__(self, operation: Operation, args: dict[str, object]) -> Response:
        self.calls.append((operation, args))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def attachment(tmp_path: Path, daemon: FakeDaemon) -> Attachment:
    paths: ProjectPaths = project_paths(tmp_path, Path("/tmp/dr-unused"))  # noqa: S108
    return Attachment(paths, "alpha", "ana", caller=daemon)


OK = Response("r", True, result={})


@pytest.mark.parametrize(
    ("command", "operation", "args", "status"),
    [
        (
            Say("hi"),
            Operation.SEND,
            {"recipient": "alpha", "body": "hi", "operator": "ana"},
            "sent",
        ),
        (
            Answer("q-1", "yes"),
            Operation.ANSWER,
            {"question_id": "q-1", "body": "yes", "operator": "ana"},
            "answered q-1",
        ),
        (
            Acknowledge("m-1"),
            Operation.ACKNOWLEDGE,
            {"operator": "ana", "message_id": "m-1"},
            "acknowledged m-1",
        ),
        (Release(), Operation.RELEASE, {"line_id": "alpha"}, "released"),
    ],
)
def test_each_command_becomes_one_request(
    tmp_path: Path,
    command: object,
    operation: Operation,
    args: dict[str, object],
    status: str,
) -> None:
    daemon = FakeDaemon(OK)
    target = attachment(tmp_path, daemon)
    assert target.execute(command) == status  # pyright: ignore[reportArgumentType]
    assert daemon.calls == [(operation, args)]


def test_local_commands_send_nothing(tmp_path: Path) -> None:
    daemon = FakeDaemon(OK)
    target = attachment(tmp_path, daemon)
    assert target.execute(Help()) == HELP
    assert target.execute(Invalid("nope")) == "nope"
    assert daemon.calls == []


def test_refusals_and_failures_become_status_text(tmp_path: Path) -> None:
    refused = Response(
        "r", False, error_code=ErrorCode.REJECTED, error_message="no line"
    )
    assert attachment(tmp_path, FakeDaemon(refused)).execute(Say("x")) == (
        "refused: no line"
    )
    down = FakeDaemon(DaemonNotRunning("down"))
    assert attachment(tmp_path, down).execute(Say("x")) == "the daemon is not running"


def test_fetch_returns_the_view_or_raises(tmp_path: Path) -> None:
    daemon = FakeDaemon(Response("r", True, result=VIEW))
    assert attachment(tmp_path, daemon).fetch() == VIEW
    assert daemon.calls == [(Operation.LINE_VIEW, {"line_id": "alpha"})]
    from dromon.protocol import ProtocolError

    refused = Response(
        "r", False, error_code=ErrorCode.REJECTED, error_message="no line"
    )
    with pytest.raises(ProtocolError, match="no line"):
        attachment(tmp_path, FakeDaemon(refused)).fetch()


# The curses loop, with a fake screen and scripted keys.


class FakeScreen:
    def __init__(self, keys: list[str | int | None], size: tuple[int, int]) -> None:
        self.keys = keys
        self.size = size
        self.rows: dict[int, str] = {}
        self.frames = 0

    def getmaxyx(self) -> tuple[int, int]:
        return self.size

    def erase(self) -> None:
        self.rows = {}

    def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0, /) -> None:
        assert x == 0 and len(text[:n]) <= self.size[1]
        self.rows[y] = text[:n]

    def refresh(self) -> None:
        self.frames += 1

    def timeout(self, delay: int, /) -> None:
        assert delay > 0

    def get_wch(self) -> str | int:
        key = self.keys.pop(0)
        if key is None:
            raise curses.error("no input")
        return key


def test_the_curses_loop_sends_typed_text_and_quits(tmp_path: Path) -> None:
    daemon = FakeDaemon(Response("r", True, result=VIEW))
    target = attachment(tmp_path, daemon)
    keys: list[str | int | None] = [None, *"hi!", "\x7f", "\n", *"/quit", "\n"]
    screen = FakeScreen(keys, (12, 50))
    assert tui.loop(screen, target, poll_seconds=1000.0, clock=lambda: 0.0) == 0
    sent = [args for operation, args in daemon.calls if operation is Operation.SEND]
    assert sent == [{"recipient": "alpha", "body": "hi", "operator": "ana"}]
    assert screen.rows[10].startswith("sent")
    assert screen.rows[11] == "> /quit"


def test_the_curses_loop_reports_a_missing_daemon(tmp_path: Path) -> None:
    daemon = FakeDaemon(DaemonNotRunning("down"))
    screen = FakeScreen([*"/quit", "\n"], (6, 40))
    assert tui.loop(screen, attachment(tmp_path, daemon), 1000.0, lambda: 0.0) == 0
    assert screen.rows[0] == "waiting for the daemon"
    assert screen.rows[4].startswith("the daemon is not running")


def test_the_graphical_front_end_imports() -> None:
    from dromon import gui

    assert callable(gui.run)
