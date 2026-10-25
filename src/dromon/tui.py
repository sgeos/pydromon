"""The curses front end of ``dromon attach``.

A full-screen view of one line: the line's state and recent mail above, a status line,
and an input line at the bottom. It polls the daemon on a fixed interval and sends each
entered line as a command, as ``dromon.attach`` defines.

Choice of library. Claude Code renders its terminal interface with a customized Ink, a
React renderer on Node.js, Codex with Ratatui on Crossterm in Rust, and Grok Build with
its own Rust pager crate. Curses is the standard library's counterpart in Python, needs
no dependency, and maps directly onto Ratatui and Crossterm in the Rust port.

Termination. The loop runs until the operator enters ``/quit`` or presses Control-C, as
any interactive program does. Each pass polls at most once per interval and waits at
most ``INPUT_TIMEOUT_MS`` for a key, so it never spins.
"""

import curses
import time
from collections.abc import Callable
from typing import Final, Protocol

from dromon.attach import Attachment, Quit, parse_input, render
from dromon.client import DaemonNotRunning
from dromon.protocol import ProtocolError

INPUT_TIMEOUT_MS: Final = 100
DEFAULT_POLL_SECONDS: Final = 1.0
BACKSPACES: Final = frozenset({"\x7f", "\b", curses.KEY_BACKSPACE})
ENTERS: Final = frozenset({"\n", "\r", curses.KEY_ENTER})
MAX_INPUT: Final = 16 * 1024


class Screen(Protocol):
    """The part of a curses window the client uses, so a test can stand in for it."""

    def getmaxyx(self) -> tuple[int, int]: ...

    def erase(self) -> None: ...

    def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0, /) -> None: ...

    def refresh(self) -> None: ...

    def timeout(self, delay: int, /) -> None: ...

    def get_wch(self) -> str | int: ...


def run(attachment: Attachment, poll_seconds: float = DEFAULT_POLL_SECONDS) -> int:
    """Attach in the terminal until the operator quits. Returns an exit status."""

    def main(window: curses.window) -> int:
        window.keypad(True)
        return loop(window, attachment, poll_seconds)

    try:
        return curses.wrapper(main)
    except KeyboardInterrupt:
        return 0


def loop(
    screen: Screen,
    attachment: Attachment,
    poll_seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Draw, read keys and run commands until ``/quit``."""
    screen.timeout(INPUT_TIMEOUT_MS)
    buffer = ""
    status = f"attached to {attachment.line_id} as @{attachment.operator}. /help"
    view: dict[str, object] | None = None
    fetched_at: float | None = None
    while True:
        now = clock()
        if fetched_at is None or now - fetched_at >= poll_seconds:
            fetched_at = now
            try:
                view = attachment.fetch()
            except DaemonNotRunning:
                status = "the daemon is not running"
            except (ProtocolError, TimeoutError, OSError) as error:
                status = f"error: {error}"
        draw(screen, view, status, buffer)
        try:
            key = screen.get_wch()
        except curses.error:
            continue
        if key in ENTERS:
            command = parse_input(buffer)
            buffer = ""
            if command is None:
                continue
            if isinstance(command, Quit):
                return 0
            status = attachment.execute(command)
            fetched_at = None
        elif key in BACKSPACES:
            buffer = buffer[:-1]
        elif isinstance(key, str) and key.isprintable() and len(buffer) < MAX_INPUT:
            buffer += key


def draw(
    screen: Screen, view: dict[str, object] | None, status: str, buffer: str
) -> None:
    """Draw the view, the status line and the input line, newest mail at the bottom."""
    height, width = screen.getmaxyx()
    usable = max(width - 1, 1)
    lines = render(view, usable) if view is not None else ["waiting for the daemon"]
    rows = max(height - 2, 0)
    screen.erase()
    for row, text in enumerate(lines[-rows:] if rows else []):
        screen.addnstr(row, 0, text, usable)
    if height >= 2:
        screen.addnstr(height - 2, 0, status.ljust(usable), usable, curses.A_REVERSE)
    if height >= 1:
        prompt = "> " + buffer
        screen.addnstr(height - 1, 0, prompt[-usable:], usable)
    screen.refresh()
