"""Keys as the core sees them, independent of the platform that read them.

A platform translates its own key events into ``Key`` values. Control characters
arrive the same way from curses and from Tk, so ``from_text`` translates them for both.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Named(StrEnum):
    ENTER = "enter"
    NEWLINE = "newline"
    """A line break inside the input, from Shift or Alt with Enter, or Control-J."""
    BACKSPACE = "backspace"
    DELETE = "delete"
    LEFT = "left"
    RIGHT = "right"
    UP = "up"
    DOWN = "down"
    HOME = "home"
    END = "end"
    PAGE_UP = "page_up"
    PAGE_DOWN = "page_down"
    TAB = "tab"
    BACK_TAB = "back_tab"
    ESCAPE = "escape"
    CTRL_A = "ctrl_a"
    CTRL_B = "ctrl_b"
    CTRL_C = "ctrl_c"
    CTRL_D = "ctrl_d"
    CTRL_E = "ctrl_e"
    CTRL_F = "ctrl_f"
    CTRL_K = "ctrl_k"
    CTRL_L = "ctrl_l"
    CTRL_N = "ctrl_n"
    CTRL_O = "ctrl_o"
    CTRL_P = "ctrl_p"
    CTRL_U = "ctrl_u"
    CTRL_W = "ctrl_w"


@dataclass(frozen=True, slots=True)
class Char:
    """One printable character."""

    text: str

    def __post_init__(self) -> None:
        if len(self.text) != 1 or not self.text.isprintable():
            raise ValueError(f"not one printable character: {self.text!r}")


type Key = Named | Char

CONTROLS: Final[dict[str, Named]] = {
    "\r": Named.ENTER,
    "\n": Named.NEWLINE,
    "\x7f": Named.BACKSPACE,
    "\b": Named.BACKSPACE,
    "\t": Named.TAB,
    "\x1b": Named.ESCAPE,
    "\x01": Named.CTRL_A,
    "\x02": Named.CTRL_B,
    "\x03": Named.CTRL_C,
    "\x04": Named.CTRL_D,
    "\x05": Named.CTRL_E,
    "\x06": Named.CTRL_F,
    "\x0b": Named.CTRL_K,
    "\x0c": Named.CTRL_L,
    "\x0e": Named.CTRL_N,
    "\x0f": Named.CTRL_O,
    "\x10": Named.CTRL_P,
    "\x15": Named.CTRL_U,
    "\x17": Named.CTRL_W,
}
"""Control characters with a meaning. A terminal sends Enter as a carriage return or a
line feed depending on its mode, so a platform that cannot tell them apart maps both
to ``ENTER`` before calling ``from_text``."""


def from_text(text: str) -> Key | None:
    """The key one character stands for, or ``None`` for one with no meaning."""
    if len(text) != 1:
        return None
    named = CONTROLS.get(text)
    if named is not None:
        return named
    return Char(text) if text.isprintable() else None
