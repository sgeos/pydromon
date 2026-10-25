"""What the operator types, as commands, with completion and help.

Plain text sends a message to the attached line. A slash begins a command, as in
Claude Code, Codex and Grok Build, and Tab completes command names and their
arguments from what the client last fetched.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Pane(StrEnum):
    ACTIVITY = "activity"
    """What the line's engine is doing, turn by turn."""
    MAIL = "mail"
    """The messages the line sent and received."""


@dataclass(frozen=True, slots=True)
class Say:
    body: str


@dataclass(frozen=True, slots=True)
class Answer:
    question_id: str
    body: str


@dataclass(frozen=True, slots=True)
class Acknowledge:
    message_id: str


@dataclass(frozen=True, slots=True)
class Release:
    pass


@dataclass(frozen=True, slots=True)
class Interrupt:
    pass


@dataclass(frozen=True, slots=True)
class Switch:
    line_id: str


@dataclass(frozen=True, slots=True)
class ShowLines:
    pass


@dataclass(frozen=True, slots=True)
class ShowPane:
    pane: Pane


@dataclass(frozen=True, slots=True)
class Help:
    pass


@dataclass(frozen=True, slots=True)
class Quit:
    pass


@dataclass(frozen=True, slots=True)
class Invalid:
    reason: str


type Command = (
    Say
    | Answer
    | Acknowledge
    | Release
    | Interrupt
    | Switch
    | ShowLines
    | ShowPane
    | Help
    | Quit
    | Invalid
)

type DaemonCommand = Say | Answer | Acknowledge | Release | Interrupt
"""The commands that are requests to the daemon. The rest act on the client."""


class Argument(StrEnum):
    QUESTION = "question"
    MESSAGE = "message"
    LINE = "line"


@dataclass(frozen=True, slots=True)
class Spec:
    name: str
    usage: str
    summary: str
    argument: Argument | None = None


SPECS: Final = (
    Spec("/answer", "/answer QUESTION TEXT", "answer a question", Argument.QUESTION),
    Spec("/ack", "/ack MESSAGE", "acknowledge a message", Argument.MESSAGE),
    Spec("/release", "/release", "release the line from a hold"),
    Spec("/interrupt", "/interrupt", "stop the turn in flight, as Escape does"),
    Spec("/line", "/line LINE", "attach to another line", Argument.LINE),
    Spec("/lines", "/lines", "list the daemon's lines"),
    Spec("/activity", "/activity", "show what the engine is doing"),
    Spec("/mail", "/mail", "show the line's messages"),
    Spec("/help", "/help", "show commands and keys"),
    Spec("/quit", "/quit", "detach, as /exit does"),
)

KEYS: Final = (
    ("Enter", "send the input, or pick the highlighted suggestion"),
    ("Shift or Alt with Enter, Ctrl-J", "break the input into rows"),
    ("Tab, Shift-Tab", "complete a command, or move through suggestions"),
    ("Up, Down", "move between input rows, or walk the history"),
    ("Ctrl-A, Ctrl-E", "go to the start or end of the row"),
    ("Ctrl-U, Ctrl-K, Ctrl-W", "cut before or after the cursor, or the word"),
    ("PageUp, PageDown", "scroll the transcript"),
    ("Escape", "close a panel, clear suggestions, or interrupt the turn"),
    ("Ctrl-O", "switch between activity and mail"),
    ("Ctrl-N, Ctrl-P", "attach to the next or previous line"),
    ("Ctrl-L", "redraw and refresh now"),
    ("Ctrl-C", "clear the input, or press twice to detach"),
    ("Ctrl-D", "detach when the input is empty"),
)

MAX_SUGGESTIONS: Final = 8


def _usage(name: str) -> str:
    for spec in SPECS:
        if spec.name == name:
            return f"usage: {spec.usage}"
    raise KeyError(name)


def parse_input(text: str) -> Command | None:
    """The command ``text`` names, or ``None`` for empty input."""
    stripped = text.strip()
    if not stripped:
        return None
    if not stripped.startswith("/"):
        return Say(stripped)
    word, _, rest = stripped.partition(" ")
    rest = rest.strip()
    match word:
        case "/answer":
            question_id, _, body = rest.partition(" ")
            if not question_id or not body.strip():
                return Invalid(_usage(word))
            return Answer(question_id, body.strip())
        case "/ack":
            if not rest or " " in rest:
                return Invalid(_usage(word))
            return Acknowledge(rest)
        case "/line":
            if not rest or " " in rest:
                return Invalid(_usage(word))
            return Switch(rest)
        case "/release" | "/interrupt" | "/lines" | "/activity" | "/mail":
            if rest:
                return Invalid(_usage(word))
            match word:
                case "/release":
                    return Release()
                case "/interrupt":
                    return Interrupt()
                case "/lines":
                    return ShowLines()
                case "/activity":
                    return ShowPane(Pane.ACTIVITY)
                case _:
                    return ShowPane(Pane.MAIL)
        case "/help" | "/?":
            return Help()
        case "/quit" | "/exit":
            return Quit()
        case _:
            return Invalid(f"unknown command {word}. /help lists the commands")


@dataclass(frozen=True, slots=True)
class Choices:
    """What arguments may complete to, from the client's last fetch."""

    lines: tuple[tuple[str, str], ...] = ()
    """Each line's identifier and a description."""
    questions: tuple[tuple[str, str], ...] = ()
    answers: tuple[tuple[str, tuple[str, ...]], ...] = ()
    """Each question's identifier and the choices its asker offered."""
    messages: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Suggestion:
    text: str
    """The whole input once the suggestion is taken."""
    label: str
    detail: str


def suggest(text: str, choices: Choices) -> tuple[Suggestion, ...]:
    """Completions of ``text``, which is the whole input, at most ``MAX_SUGGESTIONS``.

    Only a single-row input that begins with a slash completes, and only its command
    name, its first argument, or for ``/answer`` the choices the question offered.
    """
    if not text.startswith("/") or "\n" in text:
        return ()
    word, space, rest = text.partition(" ")
    if not space:
        found = [
            Suggestion(
                spec.name + (" " if spec.argument else ""), spec.usage, spec.summary
            )
            for spec in SPECS
            if spec.name.startswith(word)
        ]
        return tuple(found[:MAX_SUGGESTIONS])
    if " " in rest:
        if word != "/answer":
            return ()
        question_id, _, partial = rest.partition(" ")
        offered = dict(choices.answers).get(question_id, ())
        found = [
            Suggestion(f"{word} {question_id} {choice}", choice, "a choice offered")
            for choice in offered
            if choice.startswith(partial)
        ]
        return tuple(found[:MAX_SUGGESTIONS])
    spec = next((spec for spec in SPECS if spec.name == word), None)
    if spec is None or spec.argument is None:
        return ()
    pool = {
        Argument.LINE: choices.lines,
        Argument.QUESTION: choices.questions,
        Argument.MESSAGE: choices.messages,
    }[spec.argument]
    trailing = " " if spec.argument is Argument.QUESTION else ""
    found = [
        Suggestion(f"{word} {ident}{trailing}", ident, detail)
        for ident, detail in pool
        if ident.startswith(rest)
    ]
    return tuple(found[:MAX_SUGGESTIONS])
