"""The curses platform: it translates curses keys and paints frames in a terminal.

Choice of library. Claude Code renders its terminal interface with a customized Ink, a
React renderer on Node.js, Codex with Ratatui on Crossterm in Rust, and Grok Build with
its own Rust pager crate. Curses is the standard library's counterpart in Python, needs
no dependency, and maps directly onto Ratatui and Crossterm in the Rust port.

Keys. Curses delivers Enter and Control-J alike as a line feed, so both send, and a
line break is typed as Alt or Escape followed by Enter, or as a trailing backslash.
Escape alone waits ``ESCAPE_DELAY_MS`` for a following key, as curses requires to
tell it from the start of an escape sequence. The terminal runs in raw mode, so
Control-C arrives as a key rather than a signal, and Control-Z does not suspend.

Termination. The loop runs until the model finishes, through ``/quit``, Control-D or a
second Control-C, as any interactive program does. Each pass waits at most
``INPUT_TIMEOUT_MS`` for a key and reads at most ``MAX_KEYS_PER_PASS`` keys, so a
paste is drawn once rather than once per character, and the loop never spins.
"""

import curses
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Final, Protocol

from dromon.ui.keys import Key, Named, from_text
from dromon.ui.render import Frame, Style
from dromon.ui.runtime import Daemon, Projects, Runtime, detached_status

INPUT_TIMEOUT_MS: Final = 50
ESCAPE_DELAY_MS: Final = 25
MAX_KEYS_PER_PASS: Final = 512

CURSES_KEYS: Final[Mapping[int, Named]] = {
    curses.KEY_ENTER: Named.ENTER,
    curses.KEY_BACKSPACE: Named.BACKSPACE,
    curses.KEY_DC: Named.DELETE,
    curses.KEY_LEFT: Named.LEFT,
    curses.KEY_RIGHT: Named.RIGHT,
    curses.KEY_UP: Named.UP,
    curses.KEY_DOWN: Named.DOWN,
    curses.KEY_HOME: Named.HOME,
    curses.KEY_END: Named.END,
    curses.KEY_PPAGE: Named.PAGE_UP,
    curses.KEY_NPAGE: Named.PAGE_DOWN,
    curses.KEY_BTAB: Named.BACK_TAB,
}


class Screen(Protocol):
    """The part of a curses window the platform uses, so a test can stand in for it."""

    def getmaxyx(self) -> tuple[int, int]: ...

    def erase(self) -> None: ...

    def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0, /) -> None: ...

    def move(self, y: int, x: int, /) -> None: ...

    def refresh(self) -> None: ...

    def timeout(self, delay: int, /) -> None: ...

    def get_wch(self) -> str | int: ...


def translate(raw: Iterable[str | int]) -> list[Key]:
    """The keys a run of curses input stands for. Escape followed at once by Enter is
    Alt with Enter, which breaks the input into rows."""
    keys: list[Key] = []
    escaped = False
    for item in raw:
        if isinstance(item, int):
            key: Key | None = CURSES_KEYS.get(item)
        elif item in ("\n", "\r"):
            key = Named.ENTER
        else:
            key = from_text(item)
        if escaped and key is Named.ENTER:
            keys[-1] = Named.NEWLINE
            escaped = False
            continue
        escaped = key is Named.ESCAPE
        if key is not None:
            keys.append(key)
    return keys


def palette(has_colors: bool) -> dict[Style, int]:
    """The curses attribute for each style, in colour when the terminal has it."""
    attributes = {
        Style.PLAIN: curses.A_NORMAL,
        Style.DIM: curses.A_DIM,
        Style.STRONG: curses.A_BOLD,
        Style.ACCENT: curses.A_BOLD,
        Style.TOOL: curses.A_NORMAL,
        Style.CODE: curses.A_NORMAL,
        Style.ERROR: curses.A_BOLD,
        Style.QUESTION: curses.A_BOLD,
        Style.BAR: curses.A_REVERSE,
        Style.SELECTED: curses.A_REVERSE | curses.A_BOLD,
    }
    if not has_colors:
        return attributes
    curses.use_default_colors()
    colours = {
        Style.ACCENT: curses.COLOR_CYAN,
        Style.TOOL: curses.COLOR_GREEN,
        Style.CODE: curses.COLOR_YELLOW,
        Style.ERROR: curses.COLOR_RED,
        Style.QUESTION: curses.COLOR_MAGENTA,
    }
    for pair, (style, colour) in enumerate(colours.items(), start=1):
        curses.init_pair(pair, colour, -1)
        attributes[style] |= curses.color_pair(pair)
    return attributes


def draw(screen: Screen, frame: Frame, attributes: Mapping[Style, int]) -> None:
    screen.erase()
    for y, row in enumerate(frame.rows):
        x = 0
        for span in row:
            if x >= frame.width:
                break
            screen.addnstr(y, x, span.text, frame.width - x, attributes[span.style])
            x += len(span.text)
    if frame.cursor is not None:
        screen.move(*frame.cursor)
    screen.refresh()


def _pending(screen: Screen, first: str | int) -> list[str | int]:
    """``first`` and the keys already waiting behind it."""
    raw: list[str | int] = [first]
    screen.timeout(0)
    try:
        for _ in range(MAX_KEYS_PER_PASS - 1):
            try:
                raw.append(screen.get_wch())
            except curses.error:
                break
    finally:
        screen.timeout(INPUT_TIMEOUT_MS)
    return raw


def loop(
    screen: Screen,
    runtime: Runtime,
    attributes: Mapping[Style, int],
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Draw, read keys and run the runtime until the model finishes.

    **Round 9, item 6.** A read with no key waits out the rest of
    ``INPUT_TIMEOUT_MS`` when curses returned at once, as it does once the terminal
    is gone, so a pass never spins. The runtime detaches by itself once its daemon
    has been gone for its bound."""
    screen.timeout(INPUT_TIMEOUT_MS)
    while not runtime.finished:
        height, width = screen.getmaxyx()
        # The last column is left empty, because writing the bottom right cell
        # scrolls some terminals.
        runtime.resize(max(width - 1, 1), max(height, 1))
        runtime.tick(clock())
        if runtime.finished:
            break
        draw(screen, runtime.frame(), attributes)
        began = clock()
        try:
            first = screen.get_wch()
        except curses.error:
            rest = INPUT_TIMEOUT_MS / 1000 - (clock() - began)
            if rest > 0:
                sleep(rest)
            continue
        for key in translate(_pending(screen, first)):
            runtime.press(key)
            if runtime.finished:
                break
    return 0


def run(
    daemon: Daemon,
    line_id: str,
    operator: str,
    poll_seconds: float,
    projects: Projects | None = None,
    project: str | None = None,
) -> int:
    """Attach in the terminal until the operator detaches, or until the daemon has
    been gone for the runtime's bound. Returns an exit status, ``exits.NOT_RUNNING``
    with a message on standard error in the second case."""
    runtimes: list[Runtime] = []

    def main(window: curses.window) -> int:
        window.keypad(True)
        # Raw mode delivers Control-C as a key, so that it can clear the input and
        # detach on a second press, as in Claude Code.
        curses.raw()
        curses.set_escdelay(ESCAPE_DELAY_MS)
        attributes = palette(curses.has_colors())
        runtime = Runtime(
            daemon, line_id, operator, poll_seconds, curses.beep, projects, project
        )
        runtimes.append(runtime)
        return loop(window, runtime, attributes)

    try:
        status = curses.wrapper(main)
    except KeyboardInterrupt:
        return 0
    return detached_status(runtimes[0] if runtimes else None, status)
