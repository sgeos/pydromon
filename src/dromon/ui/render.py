"""A model drawn as a frame, rows of styled text that a platform paints as it is.

Layout, from the top. The transcript of the chosen pane, or a panel in its place. A
card for the line's open questions. A status bar. The input rows. Suggestions, or a
hint, at the bottom, where Claude Code shows them.

Every frame has exactly ``height`` rows, no row is wider than ``width``, and the
cursor, when there is one, lies inside the frame. Tests check these invariants.

Trust. Message bodies and engine output are untrusted, and a terminal acts on control
sequences in what it is given. ``clean`` replaces every control character except the
line break before any text reaches a row, so no content can move the cursor, change
colours or retitle a window.

Limitation. A character is taken to fill one column. Characters that fill two, as many
East Asian scripts and some emoji do, make rows that hold them overrun by a column per
such character, which the platform clips.
"""

import functools
import textwrap
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from dromon.activity import ActivityKind
from dromon.ui.commands import KEYS, SPECS, Pane
from dromon.ui.data import Entry, LineSummary, View
from dromon.ui.editor import row_of
from dromon.ui.model import Model, Overlay


class Style(StrEnum):
    PLAIN = "plain"
    DIM = "dim"
    STRONG = "strong"
    ACCENT = "accent"
    TOOL = "tool"
    CODE = "code"
    ERROR = "error"
    QUESTION = "question"
    BAR = "bar"
    SELECTED = "selected"


@dataclass(frozen=True, slots=True)
class Span:
    text: str
    style: Style = Style.PLAIN


type Row = tuple[Span, ...]


@dataclass(frozen=True, slots=True)
class Frame:
    width: int
    height: int
    rows: tuple[Row, ...]
    cursor: tuple[int, int] | None
    """The cursor's row and column, or ``None`` when no input has focus."""
    title: str


SPINNER: Final = "|/-\\"
"""Spinner frames, in ASCII so that every terminal shows them."""
PROMPT: Final = "> "
CONTINUATION: Final = "  "
MAX_INPUT_ROWS: Final = 6
MAX_QUESTION_ROWS: Final = 3
PROMPT_PREVIEW_ROWS: Final = 6
"""Rows of a turn's prompt shown above it. The prompt holds every delivered message."""
MIN_WIDTH: Final = 10


def clean(text: str) -> str:
    """``text`` with tabs expanded and every other control character but the line
    break replaced, so that untrusted text cannot drive the terminal."""
    expanded = text.expandtabs(4)
    return "".join(
        character if character == "\n" or character.isprintable() else "�"
        for character in expanded
    )


def wrap(text: str, width: int, first: str = "", rest: str = "") -> list[str]:
    """``text`` broken into rows of at most ``width``, keeping its own line breaks,
    with ``first`` before the first row and ``rest`` before every other."""
    rows: list[str] = []
    prefix = first
    for paragraph in clean(text).split("\n"):
        room = max(width - len(prefix), 1)
        pieces = textwrap.wrap(
            paragraph,
            room,
            subsequent_indent="",
            drop_whitespace=False,
            replace_whitespace=False,
        ) or [""]
        for piece in pieces:
            rows.append((prefix + piece.rstrip())[:width])
            prefix = rest
            room = max(width - len(prefix), 1)
    return rows


def _rows(texts: list[str], style: Style) -> list[Row]:
    return [(Span(text, style),) for text in texts]


def _markdown(text: str, width: int) -> list[Row]:
    """An engine's text, with fenced code and headings marked, as Claude Code,
    Codex and Grok Build render it. Everything else is plain wrapped text."""
    rows: list[Row] = []
    fenced = False
    first = True
    for line in clean(text).split("\n"):
        lead = "* " if first else "  "
        first = False
        if line.lstrip().startswith("```"):
            fenced = not fenced
            rows.extend(_rows(wrap(line, width, lead, "  "), Style.DIM))
            continue
        if fenced:
            rows.extend(_rows(wrap(line, width, lead, "  "), Style.CODE))
        elif line.startswith("#"):
            rows.extend(_rows(wrap(line, width, lead, "  "), Style.STRONG))
        else:
            rows.extend(_rows(wrap(line, width, lead, "  "), Style.PLAIN))
    return rows


def _entry(entry: Entry, width: int) -> list[Row]:
    match entry.kind:
        case ActivityKind.TURN_STARTED:
            prompt = wrap(entry.text, width, "  ", "  ")
            rows: list[Row] = [(), (Span("> turn started", Style.ACCENT),)]
            rows.extend(_rows(prompt[:PROMPT_PREVIEW_ROWS], Style.DIM))
            hidden = len(prompt) - PROMPT_PREVIEW_ROWS
            if hidden > 0:
                rows.append((Span(f"  ... {hidden} more rows of prompt", Style.DIM),))
            return rows
        case ActivityKind.TEXT:
            return _markdown(entry.text, width)
        case ActivityKind.THINKING:
            return [(Span("~ thinking", Style.DIM),)]
        case ActivityKind.TOOL:
            return _rows(wrap(entry.text, width, "  - tool ", "    "), Style.TOOL)
        case ActivityKind.TURN_ENDED:
            style = Style.ERROR if entry.text.startswith("failed") else Style.DIM
            return _rows(wrap(entry.text, width, "  turn ended, ", "    "), style)
        case ActivityKind.NOTICE:
            return _rows(wrap(entry.text, width, "  note: ", "    "), Style.DIM)


@functools.lru_cache(maxsize=4)
def _activity(entries: tuple[Entry, ...], gap: bool, width: int) -> tuple[Row, ...]:
    rows: list[Row] = []
    if gap:
        rows.append((Span("earlier activity is not shown", Style.DIM),))
    if not entries:
        rows.append(
            (
                Span(
                    "no activity yet. Turns appear here as the line runs them.",
                    Style.DIM,
                ),
            )
        )
    for entry in entries:
        rows.extend(_entry(entry, width))
    return tuple(rows)


@functools.lru_cache(maxsize=4)
def _mail(view: View | None, width: int) -> tuple[Row, ...]:
    if view is None:
        return ((Span("waiting for the daemon", Style.DIM),),)
    rows: list[Row] = []
    if view.total > len(view.mail):
        rows.append(
            (
                Span(
                    f"{view.total - len(view.mail)} earlier messages not shown",
                    Style.DIM,
                ),
            )
        )
    if not view.mail:
        rows.append((Span("no mail yet", Style.DIM),))
    for mail in view.mail:
        title = f"{mail.message_id} {mail.sender} -> {mail.recipient} {mail.kind}"
        if mail.reply_to is not None:
            title += f" re {mail.reply_to}"
        if mail.status is not None:
            title += f" [{mail.status}]"
        style = Style.STRONG if mail.sender.startswith("@") else Style.ACCENT
        rows.append(())
        rows.extend(_rows(wrap(title, width, "", "  "), style))
        body = mail.body + (" ..." if mail.truncated else "")
        rows.extend(_rows(wrap(body, width, "  ", "  "), Style.PLAIN))
    return tuple(rows)


def _spinner(model: Model) -> str:
    return SPINNER[int(model.now * 8) % len(SPINNER)]


def transcript(model: Model) -> tuple[Row, ...]:
    """Every row of the chosen pane, oldest first."""
    width = max(model.width, MIN_WIDTH)
    if model.pane is Pane.MAIL:
        return _mail(model.view, width)
    rows = _activity(model.activity, model.activity_gap, width)
    if model.running and (
        not model.activity or model.activity[-1].kind is not ActivityKind.TURN_ENDED
    ):
        working = f"{_spinner(model)} working. Escape interrupts the turn."
        rows = (*rows, (Span(working, Style.ACCENT),))
    return rows


def _help(width: int) -> list[Row]:
    rows: list[Row] = [(Span("Commands", Style.STRONG),)]
    span = max(len(spec.usage) for spec in SPECS) + 2
    for spec in SPECS:
        rows.extend(
            _rows(
                wrap(f"{spec.usage.ljust(span)}{spec.summary}", width, "  ", "    "),
                Style.PLAIN,
            )
        )
    rows.append(())
    rows.append((Span("Keys", Style.STRONG),))
    span = max(len(keys) for keys, _ in KEYS) + 2
    for keys, summary in KEYS:
        rows.extend(
            _rows(
                wrap(f"{keys.ljust(span)}{summary}", width, "  ", "    "), Style.PLAIN
            )
        )
    rows.append(())
    rows.append((Span("Escape or Enter closes this panel.", Style.DIM),))
    return rows


def _settings(model: Model, width: int) -> tuple[list[Row], int]:
    """The settings panel, and the index of the highlighted row in it."""
    rows: list[Row] = [
        (
            Span(
                "Settings. Up and Down choose, Enter edits, Escape closes.",
                Style.STRONG,
            ),
        )
    ]
    settings = model.settings
    if settings is None:
        rows.append((Span("waiting for the daemon", Style.DIM),))
        return rows, 0
    span = max(
        (len(entry.key) for entry in (*settings.line, *settings.project)), default=8
    )
    span = min(span + 2, max(width // 3, 8))
    selected = 0
    index = 0
    for project, title in ((False, f"Line {settings.line_id}"), (True, "Project")):
        rows.append(())
        rows.append((Span(title, Style.ACCENT),))
        prefix = "project." if project else ""
        for entry in settings.project if project else settings.line:
            source = f"  [{entry.source}]" if entry.source else ""
            text = clean(
                f"  {(prefix + entry.key)[: span - 1].ljust(span)}{entry.shown}{source}"
            )
            if index == model.setting_row:
                style = Style.SELECTED
                selected = len(rows)
            else:
                style = Style.PLAIN if entry.editable else Style.DIM
            rows.append((Span(text.replace("\n", " ")[:width], style),))
            index += 1
    return rows, selected


def _lines(model: Model, width: int) -> list[Row]:
    rows: list[Row] = [
        (
            Span(
                "Lines. Up and Down choose, Enter attaches, Escape closes.",
                Style.STRONG,
            ),
        )
    ]
    if not model.lines:
        rows.append(
            (Span("the daemon has no lines, or has not answered yet", Style.DIM),)
        )
    for index, line in enumerate(model.lines):
        mark = "*" if line.line_id == model.line_id else " "
        text = (
            f"{mark} {line.line_id:<16} {line.state:<8} {line.model}"
            f"  turns {line.turns}  pending {line.pending}"
            + ("  held" if line.held_by else "")
        )[:width]
        style = Style.SELECTED if index == model.picked else Style.PLAIN
        rows.append((Span(text, style),))
    return rows


def _questions(view: View | None, width: int) -> list[Row]:
    if view is None or not view.questions:
        return []
    rows: list[Row] = []
    shown = view.questions[-MAX_QUESTION_ROWS:]
    hidden = len(view.questions) - len(shown)
    if hidden:
        rows.append((Span(f"? and {hidden} earlier open questions", Style.QUESTION),))
    for question in shown:
        text = f"? {question.question_id} to {question.operator}: {question.text}"
        if question.choices:
            text += "  [" + " / ".join(question.choices) + "]"
        rows.append((Span(clean(text).replace("\n", " ")[:width], Style.QUESTION),))
    return rows


def _cost(line: LineSummary) -> str:
    """The line's cost for the bar, with unknown kept apart from zero."""
    if not line.cost:
        return "cost unknown" if line.cost_unknown else ""
    unknown = f" + {line.cost_unknown} unknown" if line.cost_unknown else ""
    return f"${line.cost}{unknown}"


def _bar(model: Model, width: int) -> Row:
    view = model.view
    if view is None:
        text = f" {model.line_id} | connecting | @{model.operator}"
    else:
        line = view.line
        state = line.state + (f" {_spinner(model)}" if line.running else "")
        # The most urgent parts come first, because a narrow bar cuts the rest.
        parts = [
            f" {line.line_id}",
            state,
            "HELD, /release" if line.held_by else "",
            f"{len(view.questions)} open questions" if view.questions else "",
            line.model,
            f"turns {line.turns}",
            _cost(line),
            f"pending {line.pending}",
            model.pane.value,
            f"{model.scroll} rows below" if model.scroll else "",
            f"@{model.operator}",
        ]
        text = " | ".join(part for part in parts if part)
    return (Span(text[:width].ljust(width), Style.BAR),)


def _input(model: Model, width: int) -> tuple[list[Row], tuple[int, int]]:
    """The input rows and the cursor's position among them."""
    room = max(width - len(PROMPT), 1)
    rows: list[str] = []
    cursor = (0, len(PROMPT))
    row, column = row_of(model.editor)
    for index, line in enumerate(clean(model.editor.text).split("\n")):
        pieces = [line[start : start + room] for start in range(0, len(line), room)]
        if index == row:
            # A cursor at the end of a full row sits at the start of the next.
            cursor = (len(rows) + column // room, len(PROMPT) + column % room)
        for piece in pieces or [""]:
            rows.append((PROMPT if not rows else CONTINUATION) + piece)
        if index == row and column // room >= len(pieces or [""]):
            rows.append(CONTINUATION)
    top = max(cursor[0] - MAX_INPUT_ROWS + 1, 0)
    shown = rows[top : top + MAX_INPUT_ROWS]
    return _rows(shown, Style.PLAIN), (cursor[0] - top, cursor[1])


def _footer(model: Model, width: int) -> list[Row]:
    if model.suggestions:
        rows: list[Row] = []
        span = max(len(s.label) for s in model.suggestions) + 2
        for index, suggestion in enumerate(model.suggestions):
            text = clean(f"  {suggestion.label.ljust(span)}{suggestion.detail}")
            style = Style.SELECTED if index == model.selected else Style.DIM
            rows.append((Span(text.replace("\n", " ")[:width], style),))
        return rows
    if model.notice:
        style = Style.ERROR if model.alarm else Style.DIM
        return [(Span(clean(model.notice).replace("\n", " ")[:width], style),)]
    return [(Span("? for shortcuts, / for commands", Style.DIM),)]


@dataclass(frozen=True, slots=True)
class _Layout:
    top: list[Row]
    """Every row below the transcript, top to bottom."""
    cursor: tuple[int, int]
    """The cursor's position within ``top``."""
    area: int
    """Rows left for the transcript or a panel."""


def _layout(model: Model) -> _Layout:
    width = max(model.width, 1)
    input_rows, (row, column) = _input(model, width)
    footer = _footer(model, width)
    below = [*_questions(model.view, width), _bar(model, width)]
    if model.height < len(below) + len(input_rows) + len(footer) + 1:
        below, footer = [], []
    rows = [*below, *input_rows, *footer]
    rows = rows[-model.height :] if model.height > 0 else []
    offset = len(rows) - len(footer) - len(input_rows)
    return _Layout(rows, (offset + row, column), max(model.height - len(rows), 0))


def scroll_limit(model: Model) -> int:
    """The most rows the transcript can scroll back."""
    if model.overlay is not Overlay.NONE:
        return 0
    return max(len(transcript(model)) - _layout(model).area, 0)


def page(model: Model) -> int:
    """Rows a page key scrolls, one less than the transcript area so that a row of
    context stays in view."""
    return max(_layout(model).area - 1, 1)


def frame(model: Model) -> Frame:
    """The model drawn at its size."""
    width = max(model.width, 1)
    layout = _layout(model)
    match model.overlay:
        case Overlay.HELP:
            body = _help(width)[: layout.area]
        case Overlay.LINES:
            panel = _lines(model, width)
            start = max(model.picked + 2 - layout.area, 0)
            body = panel[:1] + panel[1 + start :][: max(layout.area - 1, 0)]
        case Overlay.SETTINGS:
            panel, selected = _settings(model, width)
            # The title stays, and the rest scrolls to keep the highlighted row shown.
            start = max(selected + 1 - layout.area + 1, 1)
            body = panel[:1] + panel[start:][: max(layout.area - 1, 0)]
        case Overlay.NONE:
            rows = transcript(model)
            scroll = min(model.scroll, max(len(rows) - layout.area, 0))
            end = len(rows) - scroll
            body = list(rows[max(end - layout.area, 0) : end])
    padding: list[Row] = [() for _ in range(layout.area - len(body))]
    body = padding + body if model.overlay is Overlay.NONE else body + padding
    rows = tuple(tuple(_fit(row, width)) for row in [*body[: layout.area], *layout.top])
    row, column = layout.cursor
    cursor = (layout.area + row, min(column, width - 1))
    inside = 0 <= cursor[0] < model.height and model.overlay is Overlay.NONE
    return Frame(
        width,
        model.height,
        rows,
        cursor if inside else None,
        f"dromon {model.line_id}",
    )


def _fit(row: Row, width: int) -> list[Span]:
    """``row`` cut to ``width`` columns."""
    fitted: list[Span] = []
    room = width
    for span in row:
        if room <= 0:
            break
        fitted.append(Span(span.text[:room], span.style))
        room -= len(span.text[:room])
    return fitted


def text_of(row: Row) -> str:
    return "".join(span.text for span in row)
