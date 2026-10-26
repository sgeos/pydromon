"""The shared runtime, its daemon port, and the curses and Tk platforms."""

import ast
import curses
import subprocess
import sys
from pathlib import Path

import pytest

import dromon.ui

from dromon.paths import project_paths
from dromon.protocol import ErrorCode, Operation, Response
from dromon.ui import terminal, window
from dromon.ui.commands import Acknowledge, Answer, Interrupt, Release, Say
from dromon.ui.keys import Char, Named
from dromon.ui.link import SocketDaemon
from dromon.ui.render import Style, text_of
from dromon.ui.runtime import MAX_STEPS, Runtime, Unreachable, request_of

LINE: dict[str, object] = {
    "line_id": "alpha",
    "engine": "claude-haiku-5-5",
    "strategy": "subscription",
    "state": "idle",
    "turns": 0,
    "pending": 0,
    "cost_estimate_usd": "0.0000",
    "held_by": None,
}
VIEW: dict[str, object] = {
    "line": LINE,
    "tick": 1,
    "total": 0,
    "messages": [],
    "open_questions": [
        {"question_id": "q-9", "operator": "@ana", "text": "Go?", "choices": []}
    ],
}

CORE = (
    "keys",
    "editor",
    "commands",
    "data",
    "model",
    "settings",
    "render",
    "update",
)
CORE_DEPENDENCIES = {f"dromon.ui.{name}" for name in CORE} | {"dromon.activity"}
FORBIDDEN = frozenset(
    {"curses", "tkinter", "socket", "select", "selectors", "asyncio", "os"}
    | {"subprocess", "threading", "time", "dromon.client", "dromon.protocol"}
)


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            found.add(node.module)
    return found


def test_the_core_performs_no_input_or_output() -> None:
    # The counterpart of a no_std crate. The core imports neither a platform library
    # nor anything that reaches the operating system, and of Dromon only itself and
    # the activity kinds.
    package = Path(dromon.ui.__file__).parent
    for name in CORE:
        imported = imports_of(package / f"{name}.py")
        assert not imported & FORBIDDEN, name
        own = {module for module in imported if module.startswith("dromon")}
        assert own <= CORE_DEPENDENCIES | {"dromon.ui"}, (name, own)


def test_importing_the_core_loads_no_platform() -> None:
    # A fresh interpreter, because this one has already imported the platforms.
    platforms = ("curses", "_curses", "tkinter", "_tkinter", "dromon.client")
    script = (
        "import sys\n"
        + "".join(f"import dromon.ui.{name}\n" for name in CORE)
        + f"print(sorted(set({platforms!r}) & set(sys.modules)))\n"
    )
    found = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert found.stdout.strip() == "[]"


class FakeDaemon:
    """Answers each operation from a table, and records every call."""

    def __init__(self) -> None:
        self.calls: list[tuple[Operation, dict[str, object]]] = []
        self.results: dict[Operation, Response | Exception] = {
            Operation.LINE_VIEW: Response("r", True, result=VIEW),
            Operation.LINE_ACTIVITY: Response(
                "r",
                True,
                result={
                    "seq": 2,
                    "entries": [
                        {"entry_id": 1, "seq": 2, "kind": "text", "text": "hello"}
                    ],
                    "complete": True,
                },
            ),
            Operation.STATUS: Response("r", True, result={"lines": [LINE]}),
        }

    def call(self, operation: Operation, args: dict[str, object]) -> Response:
        self.calls.append((operation, args))
        outcome = self.results.get(operation, Response("r", True, result={}))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def operations(self) -> list[Operation]:
        return [operation for operation, _ in self.calls]


def runtime(daemon: FakeDaemon, bells: list[int] | None = None) -> Runtime:
    ring = (lambda: bells.append(1)) if bells is not None else (lambda: None)
    built = Runtime(daemon, "alpha", "ana", 1.0, ring)
    built.resize(60, 16)
    return built


def test_a_tick_polls_and_loads_the_results() -> None:
    daemon = FakeDaemon()
    client = runtime(daemon)
    client.tick(0.0)
    assert daemon.operations() == [
        Operation.LINE_VIEW,
        Operation.LINE_ACTIVITY,
        Operation.STATUS,
        Operation.SETTINGS,
    ]
    assert daemon.calls[1][1] == {"line_id": "alpha", "after": 0}
    model = client.model
    assert model.view is not None and model.view.line.line_id == "alpha"
    assert [entry.text for entry in model.activity] == ["hello"]
    assert [line.line_id for line in model.lines] == ["alpha"]
    client.tick(0.5)
    assert len(daemon.calls) == 4


@pytest.mark.parametrize(
    ("command", "operation", "args", "notice"),
    [
        (
            Say("hi"),
            Operation.SEND,
            {"recipient": "alpha", "body": "hi", "operator": "ana"},
            "sent to alpha",
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
        (Release(), Operation.RELEASE, {"line_id": "alpha"}, "released alpha"),
        (Interrupt(), Operation.INTERRUPT, {"line_id": "alpha"}, "interrupted alpha"),
    ],
)
def test_each_daemon_command_is_one_request(
    command: Say | Answer | Acknowledge | Release | Interrupt,
    operation: Operation,
    args: dict[str, object],
    notice: str,
) -> None:
    assert request_of(command, "alpha", "ana") == (operation, args, notice)


def test_typed_text_is_sent_and_the_client_refreshes() -> None:
    daemon = FakeDaemon()
    client = runtime(daemon)
    client.tick(0.0)
    for character in "hi":
        client.press(Char(character))
    client.press(Named.ENTER)
    assert daemon.calls[4] == (
        Operation.SEND,
        {"recipient": "alpha", "body": "hi", "operator": "ana"},
    )
    assert daemon.operations()[5:] == [
        Operation.LINE_VIEW,
        Operation.LINE_ACTIVITY,
        Operation.STATUS,
        Operation.SETTINGS,
    ]
    assert client.model.notice == "sent to alpha"


def test_refusals_and_an_unreachable_daemon_become_notices() -> None:
    daemon = FakeDaemon()
    daemon.results[Operation.SEND] = Response(
        "r", False, error_code=ErrorCode.REJECTED, error_message="no line alpha"
    )
    client = runtime(daemon)
    client.press(Char("x"))
    client.press(Named.ENTER)
    assert client.model.alarm and client.model.notice == "send: no line alpha"
    daemon.results[Operation.LINE_VIEW] = Unreachable("the daemon is not running")
    client.tick(0.0)
    assert client.model.notice == "the daemon is not running" and client.model.alarm


def test_an_interrupt_without_a_turn_says_so() -> None:
    daemon = FakeDaemon()
    daemon.results[Operation.INTERRUPT] = Response(
        "r", True, result={"line_id": "alpha", "interrupted": False}
    )
    client = runtime(daemon)
    for character in "/interrupt":
        client.press(Char(character))
    client.press(Named.ESCAPE)
    client.press(Named.ENTER)
    assert client.model.notice == "alpha has no turn in flight"


def test_a_new_question_rings_the_platform_bell() -> None:
    daemon = FakeDaemon()
    quiet = dict(VIEW, open_questions=[])
    daemon.results[Operation.LINE_VIEW] = Response("r", True, result=quiet)
    bells: list[int] = []
    client = runtime(daemon, bells)
    client.tick(0.0)
    daemon.results[Operation.LINE_VIEW] = Response("r", True, result=VIEW)
    client.tick(5.0)
    assert bells == [1]


def test_the_frame_is_drawn_again_only_when_the_model_changes() -> None:
    client = runtime(FakeDaemon())
    first = client.frame()
    assert client.frame() is first
    client.press(Char("a"))
    assert client.frame() is not first


def test_dispatch_is_bounded() -> None:
    assert MAX_STEPS > 4


def test_the_socket_port_reports_a_missing_daemon(
    tmp_path: Path, short_runtime: Path
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    with pytest.raises(Unreachable, match="daemon"):
        SocketDaemon(paths).call(Operation.PING, {})


# The curses platform.


def test_curses_keys_translate_into_core_keys() -> None:
    assert terminal.translate([curses.KEY_UP, curses.KEY_PPAGE, curses.KEY_BTAB]) == [
        Named.UP,
        Named.PAGE_UP,
        Named.BACK_TAB,
    ]
    assert terminal.translate(["\n", "\r", "a", "\x17"]) == [
        Named.ENTER,
        Named.ENTER,
        Char("a"),
        Named.CTRL_W,
    ]
    assert terminal.translate(["\x1b", "\r", "\x1b", "b"]) == [
        Named.NEWLINE,
        Named.ESCAPE,
        Char("b"),
    ]
    assert terminal.translate([curses.KEY_RESIZE, "\x00"]) == []


def test_without_colours_every_style_still_has_an_attribute() -> None:
    attributes = terminal.palette(False)
    assert set(attributes) == set(Style)
    assert attributes[Style.BAR] & curses.A_REVERSE


class FakeScreen:
    def __init__(self, keys: list[str | int | None], size: tuple[int, int]) -> None:
        self.keys = keys
        self.size = size
        self.rows: dict[int, str] = {}
        self.cursor: tuple[int, int] | None = None
        self.delay = -1
        self.frames = 0

    def getmaxyx(self) -> tuple[int, int]:
        return self.size

    def erase(self) -> None:
        self.rows = {}

    def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0, /) -> None:
        assert 0 <= y < self.size[0] and x + len(text[:n]) < self.size[1]
        row = self.rows.get(y, "").ljust(x)
        self.rows[y] = row[:x] + text[:n]

    def move(self, y: int, x: int, /) -> None:
        self.cursor = (y, x)

    def refresh(self) -> None:
        self.frames += 1

    def timeout(self, delay: int, /) -> None:
        self.delay = delay

    def get_wch(self) -> str | int:
        if not self.keys and self.delay == 0:
            raise curses.error("nothing waiting")
        if not self.keys:
            raise AssertionError("the loop read past the scripted keys")
        key = self.keys.pop(0)
        if key is None:
            raise curses.error("no input")
        return key


def test_the_curses_loop_sends_typed_text_and_detaches() -> None:
    daemon = FakeDaemon()
    client = Runtime(daemon, "alpha", "ana", 1000.0)
    # A pause, a word with a typo, Enter, a pause, then Control-D.
    keys: list[str | int | None] = [None, *"hi!", "\x7f", "\n", None, None, "\x04"]
    screen = FakeScreen(keys, (12, 50))
    attributes = terminal.palette(False)
    assert terminal.loop(screen, client, attributes, clock=lambda: 0.0) == 0
    sent = [args for operation, args in daemon.calls if operation is Operation.SEND]
    assert sent == [{"recipient": "alpha", "body": "hi", "operator": "ana"}]
    assert screen.rows[11].startswith("sent to alpha")
    assert screen.rows[10].startswith("> ") and screen.cursor == (10, 2)
    assert screen.delay == terminal.INPUT_TIMEOUT_MS
    assert client.finished


def test_the_curses_loop_shows_a_missing_daemon() -> None:
    daemon = FakeDaemon()
    for operation in (Operation.LINE_VIEW, Operation.LINE_ACTIVITY, Operation.STATUS):
        daemon.results[operation] = Unreachable("the daemon is not running")
    screen = FakeScreen([None, "\x04"], (8, 60))
    client = Runtime(daemon, "alpha", "ana", 1000.0)
    terminal.loop(screen, client, terminal.palette(False), clock=lambda: 0.0)
    assert screen.rows[7].startswith("the daemon is not running")
    assert "connecting" in screen.rows[5]


# The Tk platform.


@pytest.mark.parametrize(
    ("keysym", "char", "state", "key"),
    [
        ("Return", "\r", 0, Named.ENTER),
        ("Return", "\r", window.SHIFT, Named.NEWLINE),
        ("Return", "\r", window.OPTION, Named.NEWLINE),
        ("Tab", "\t", window.SHIFT, Named.BACK_TAB),
        ("ISO_Left_Tab", "", 0, Named.BACK_TAB),
        ("Prior", "", 0, Named.PAGE_UP),
        ("Escape", "\x1b", 0, Named.ESCAPE),
        ("a", "\x01", window.CONTROL, Named.CTRL_A),
        ("o", "", window.CONTROL, Named.CTRL_O),
        ("a", "a", 0, Char("a")),
        ("Shift_L", "", window.SHIFT, None),
        ("F5", "", 0, None),
    ],
)
def test_tk_keys_translate_into_core_keys(
    keysym: str, char: str, state: int, key: object
) -> None:
    assert window.key_of(keysym, char, state) == key


def test_the_wheel_scrolls_by_a_few_rows_on_every_platform() -> None:
    assert window.wheel_rows(120, 0) == window.WHEEL_ROWS
    assert window.wheel_rows(-1, 0) == -window.WHEEL_ROWS
    assert window.wheel_rows(0, 4) == window.WHEEL_ROWS
    assert window.wheel_rows(0, 5) == -window.WHEEL_ROWS
    assert window.wheel_rows(0, 0) == 0


def test_every_style_has_a_tk_colour() -> None:
    assert set(window.COLOURS) == set(Style)


def test_the_status_bar_row_reads_as_text() -> None:
    client = runtime(FakeDaemon())
    client.tick(0.0)
    rows = [text_of(row) for row in client.frame().rows]
    assert any(row.startswith(" alpha | idle") for row in rows)
