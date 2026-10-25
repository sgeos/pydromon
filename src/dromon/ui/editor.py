"""The input line: text with a cursor, the usual editing keys, and a history.

The bindings are the readline ones that Claude Code, Codex and Grok Build all accept.
Control-A and Control-E move to the start and end of the row, Control-B and Control-F
move by a character, Control-U and Control-K cut before and after the cursor,
Control-W cuts the word before it, and a newline key breaks the row. Up and Down move
between rows of a multi-row input, and otherwise walk the history.

Every function returns a new value, and none can grow the text past ``MAX_INPUT``.
"""

from dataclasses import dataclass, replace
from typing import Final

MAX_INPUT: Final = 16 * 1024
"""Characters an input may hold. A message body has its own, larger, daemon limit."""
MAX_HISTORY: Final = 200


@dataclass(frozen=True, slots=True)
class Editor:
    text: str = ""
    cursor: int = 0
    """An index into ``text``, from zero to its length."""

    def __post_init__(self) -> None:
        if not 0 <= self.cursor <= len(self.text):
            raise ValueError(f"cursor {self.cursor} is outside the text")


def insert(editor: Editor, text: str) -> Editor:
    """``text`` inserted at the cursor, cut to fit ``MAX_INPUT``."""
    room = MAX_INPUT - len(editor.text)
    piece = text[: max(room, 0)]
    return Editor(
        editor.text[: editor.cursor] + piece + editor.text[editor.cursor :],
        editor.cursor + len(piece),
    )


def backspace(editor: Editor) -> Editor:
    if editor.cursor == 0:
        return editor
    return Editor(
        editor.text[: editor.cursor - 1] + editor.text[editor.cursor :],
        editor.cursor - 1,
    )


def delete(editor: Editor) -> Editor:
    if editor.cursor == len(editor.text):
        return editor
    return replace(
        editor, text=editor.text[: editor.cursor] + editor.text[editor.cursor + 1 :]
    )


def left(editor: Editor) -> Editor:
    return replace(editor, cursor=max(editor.cursor - 1, 0))


def right(editor: Editor) -> Editor:
    return replace(editor, cursor=min(editor.cursor + 1, len(editor.text)))


def _row_start(editor: Editor) -> int:
    return editor.text.rfind("\n", 0, editor.cursor) + 1


def _row_end(editor: Editor) -> int:
    found = editor.text.find("\n", editor.cursor)
    return len(editor.text) if found < 0 else found


def home(editor: Editor) -> Editor:
    return replace(editor, cursor=_row_start(editor))


def end(editor: Editor) -> Editor:
    return replace(editor, cursor=_row_end(editor))


def cut_before(editor: Editor) -> Editor:
    """Control-U, which cuts the row from its start to the cursor."""
    start = _row_start(editor)
    return Editor(editor.text[:start] + editor.text[editor.cursor :], start)


def cut_after(editor: Editor) -> Editor:
    """Control-K, which cuts the row from the cursor to its end."""
    return replace(
        editor, text=editor.text[: editor.cursor] + editor.text[_row_end(editor) :]
    )


def cut_word(editor: Editor) -> Editor:
    """Control-W, which cuts the spaces before the cursor and the word before them."""
    start = editor.cursor
    while start > 0 and editor.text[start - 1].isspace():
        start -= 1
    while start > 0 and not editor.text[start - 1].isspace():
        start -= 1
    return Editor(editor.text[:start] + editor.text[editor.cursor :], start)


def row_of(editor: Editor) -> tuple[int, int]:
    """The cursor's row in the text and its column in that row."""
    before = editor.text[: editor.cursor]
    return before.count("\n"), editor.cursor - _row_start(editor)


def vertical(editor: Editor, rows: int) -> Editor | None:
    """The cursor moved ``rows`` rows down, or up when negative, keeping its column
    where the row is long enough, or ``None`` when no such row exists."""
    lines = editor.text.split("\n")
    row, column = row_of(editor)
    target = row + rows
    if not 0 <= target < len(lines):
        return None
    offset = sum(len(line) + 1 for line in lines[:target])
    return replace(editor, cursor=offset + min(column, len(lines[target])))


@dataclass(frozen=True, slots=True)
class History:
    entries: tuple[str, ...] = ()
    """Submitted inputs, oldest first, without repeats in a row."""
    index: int | None = None
    """The entry shown, or ``None`` while editing a fresh input."""
    draft: str = ""
    """The fresh input, kept while the history is walked."""


def remember(history: History, text: str) -> History:
    """``history`` with ``text`` submitted, and walking reset."""
    entries = history.entries
    if text.strip() and (not entries or entries[-1] != text):
        entries = (*entries, text)[-MAX_HISTORY:]
    return History(entries)


def older(history: History, current: str) -> tuple[History, str] | None:
    """One step back, with the text to show, or ``None`` at the oldest entry."""
    if not history.entries or history.index == 0:
        return None
    if history.index is None:
        index = len(history.entries) - 1
        return History(history.entries, index, current), history.entries[index]
    index = history.index - 1
    return replace(history, index=index), history.entries[index]


def newer(history: History) -> tuple[History, str] | None:
    """One step forward, with the text to show, or ``None`` on the fresh input."""
    if history.index is None:
        return None
    if history.index + 1 >= len(history.entries):
        return History(history.entries), history.draft
    index = history.index + 1
    return replace(history, index=index), history.entries[index]
