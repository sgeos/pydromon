"""The Tk platform: it translates Tk key events and paints frames in a window.

The window shows the same frame as the terminal, in a fixed-width font, so the two
clients behave alike and all of their behaviour is tested in the core. What the window
adds is its own. It sizes the frame to the window, colours styles with Tk tags, scrolls
with the mouse wheel, and rings the window's bell. Each window is an independent
client, so an operator may open one per line.

Tk ships with Python, so the window needs no dependency. Only ``key_of`` and
``wheel_rows`` are tested, because a window needs a display that automated tests may
not have.
"""

import time
import tkinter as tk
from tkinter import font as tkfont
from typing import Final

from dromon.ui.keys import Key, Named, from_text
from dromon.ui.render import Frame, Style
from dromon.ui.runtime import Daemon, Projects, Runtime, detached_status

PUMP_MILLISECONDS: Final = 50
COLUMNS: Final = 110
ROWS: Final = 36
WHEEL_ROWS: Final = 3

SHIFT: Final = 0x1
CONTROL: Final = 0x4
ALT: Final = 0x8
"""Alt on X11 and Windows. Tk on macOS reports Option as ``0x10``."""
OPTION: Final = 0x10

KEYSYMS: Final[dict[str, Named]] = {
    "Return": Named.ENTER,
    "KP_Enter": Named.ENTER,
    "BackSpace": Named.BACKSPACE,
    "Delete": Named.DELETE,
    "Left": Named.LEFT,
    "Right": Named.RIGHT,
    "Up": Named.UP,
    "Down": Named.DOWN,
    "Home": Named.HOME,
    "End": Named.END,
    "Prior": Named.PAGE_UP,
    "Next": Named.PAGE_DOWN,
    "Tab": Named.TAB,
    "ISO_Left_Tab": Named.BACK_TAB,
    "Escape": Named.ESCAPE,
}

COLOURS: Final[dict[Style, dict[str, str]]] = {
    Style.PLAIN: {},
    Style.DIM: {"foreground": "#7a7a7a"},
    Style.STRONG: {"foreground": "#e8e8e8"},
    Style.ACCENT: {"foreground": "#5fc7d6"},
    Style.TOOL: {"foreground": "#7fc77f"},
    Style.CODE: {"foreground": "#d7c46a"},
    Style.ERROR: {"foreground": "#ef6b6b"},
    Style.QUESTION: {"foreground": "#d68ad6"},
    Style.BAR: {"foreground": "#1e1e1e", "background": "#bdbdbd"},
    Style.SELECTED: {"foreground": "#1e1e1e", "background": "#5fc7d6"},
}


def key_of(keysym: str, char: str, state: int) -> Key | None:
    """The key a Tk key event stands for, or ``None`` for one with no meaning."""
    if keysym in ("Return", "KP_Enter") and state & (SHIFT | ALT | OPTION):
        return Named.NEWLINE
    if keysym == "Tab" and state & SHIFT:
        return Named.BACK_TAB
    named = KEYSYMS.get(keysym)
    if named is not None:
        return named
    if state & CONTROL and len(keysym) == 1 and keysym.isalpha():
        # Tk on some platforms reports the letter rather than the control character.
        return from_text(chr(ord(keysym.lower()) & 0x1F))
    return from_text(char) if char else None


def wheel_rows(delta: int, num: int) -> int:
    """Rows to scroll for a wheel event, back toward older rows when positive.
    ``delta`` comes from Windows and macOS, and ``num`` four or five from X11."""
    if num == 4:
        return WHEEL_ROWS
    if num == 5:
        return -WHEEL_ROWS
    if delta > 0:
        return WHEEL_ROWS
    if delta < 0:
        return -WHEEL_ROWS
    return 0


class Window:
    def __init__(self, root: tk.Tk, runtime: Runtime) -> None:
        self._root = root
        self._runtime = runtime
        self._font = tkfont.nametofont("TkFixedFont")
        self._text = tk.Text(
            root,
            width=COLUMNS,
            height=ROWS,
            font=self._font,
            wrap="none",
            background="#1e1e1e",
            foreground="#cfcfcf",
            insertbackground="#cfcfcf",
            borderwidth=0,
            highlightthickness=0,
            padx=6,
            pady=6,
            cursor="xterm",
        )
        for style, options in COLOURS.items():
            self._text.tag_configure(style.value, **options)
        self._text.tag_configure("cursor", background="#cfcfcf", foreground="#1e1e1e")
        self._text.pack(fill="both", expand=True)
        self._text.bind("<Key>", self._key)
        self._text.bind("<MouseWheel>", self._wheel)
        self._text.bind("<Button-4>", self._wheel)
        self._text.bind("<Button-5>", self._wheel)
        self._text.focus_set()
        self._drawn: Frame | None = None

    def _key(self, event: "tk.Event[tk.Text]") -> str:
        key = key_of(event.keysym, event.char, int(event.state))
        if key is not None:
            self._runtime.press(key)
            self._pump_once()
        return "break"

    def _wheel(self, event: "tk.Event[tk.Text]") -> str:
        rows = wheel_rows(int(getattr(event, "delta", 0) or 0), int(event.num or 0))
        if rows:
            self._runtime.wheel(rows)
            self._pump_once()
        return "break"

    def _size(self) -> tuple[int, int]:
        columns = max(self._text.winfo_width() - 12, 1) // max(
            self._font.measure("0"), 1
        )
        rows = max(self._text.winfo_height() - 12, 1) // max(
            int(self._font.metrics("linespace")), 1
        )
        return max(columns, 20), max(rows, 6)

    def _pump_once(self) -> None:
        width, height = self._size()
        self._runtime.resize(width, height)
        self._runtime.tick(time.monotonic())
        if self._runtime.finished:
            self._root.destroy()
            return
        frame = self._runtime.frame()
        if frame is not self._drawn:
            self._draw(frame)

    def pump(self) -> None:
        self._pump_once()
        if not self._runtime.finished:
            self._root.after(PUMP_MILLISECONDS, self.pump)

    def _draw(self, frame: Frame) -> None:
        self._drawn = frame
        self._root.title(frame.title)
        text = self._text
        text.configure(state="normal")
        text.delete("1.0", "end")
        for index, row in enumerate(frame.rows):
            for span in row:
                text.insert("end", span.text, (span.style.value,))
            if index + 1 < len(frame.rows):
                text.insert("end", "\n")
        if frame.cursor is not None:
            row, column = frame.cursor
            start = f"{row + 1}.{column}"
            if text.compare(start, ">=", f"{row + 1}.end"):
                text.insert(start, " ")
            text.tag_add("cursor", start, f"{start} + 1 chars")
        text.configure(state="disabled")


def run(
    daemon: Daemon,
    line_id: str,
    operator: str,
    poll_seconds: float,
    projects: Projects | None = None,
    project: str | None = None,
) -> int:
    """Open the window and run it until the operator detaches or closes it."""
    root = tk.Tk()
    runtime = Runtime(
        daemon, line_id, operator, poll_seconds, root.bell, projects, project
    )
    window = Window(root, runtime)
    root.after(0, window.pump)
    root.mainloop()
    return detached_status(runtime, 0)
