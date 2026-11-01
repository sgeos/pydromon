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
class SwitchProject:
    """Attach to a line of another project in the fleet index, without restarting the
    client. Round 13, item 4."""

    project: str
    """The project, by its name or its root."""
    line_id: str | None
    """The line, or ``None`` for the attached line's name when the project has it and
    its first line otherwise."""


@dataclass(frozen=True, slots=True)
class ShowProjects:
    pass


@dataclass(frozen=True, slots=True)
class ShowPane:
    pane: Pane


@dataclass(frozen=True, slots=True)
class ShowSettings:
    focus: str | None = None
    """The key to highlight, such as ``model``."""


@dataclass(frozen=True, slots=True)
class Set:
    key: str
    """A line key, or a project key prefixed with ``project.``."""
    text: str | None
    """The value as typed, or ``None`` to return the key to the layers below."""


type Setting = bool | int | str | tuple[str, ...]
"""A setting's value as the daemon takes it. A tuple is sent as a list."""


@dataclass(frozen=True, slots=True)
class SetSetting:
    """A ``Set`` checked against the daemon's settings and ready to send."""

    project: bool
    key: str
    value: Setting | None


@dataclass(frozen=True, slots=True)
class ClearSession:
    pass


@dataclass(frozen=True, slots=True)
class CreateLine:
    line_id: str
    model: str
    strategy: str


@dataclass(frozen=True, slots=True)
class ReloadProject:
    pass


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
    | SwitchProject
    | ShowProjects
    | ShowPane
    | ShowSettings
    | Set
    | ClearSession
    | CreateLine
    | ReloadProject
    | Help
    | Quit
    | Invalid
)

type DaemonCommand = (
    Say
    | Answer
    | Acknowledge
    | Release
    | Interrupt
    | SetSetting
    | ClearSession
    | CreateLine
    | ReloadProject
)
"""The requests a client makes of the daemon. The other commands act on the client,
and ``Set`` becomes a ``SetSetting`` once it is checked."""

PROJECT_PREFIX: Final = "project."
STRATEGIES: Final = ("subscription", "api", "flexible")


class Argument(StrEnum):
    QUESTION = "question"
    MESSAGE = "message"
    LINE = "line"
    PROJECT = "project"
    """A project of the fleet index."""
    NEW_LINE = "new_line"
    """A name not yet in use, which nothing completes."""
    SETTING = "setting"
    LINE_MODEL = "line_model"
    """A model the attached line may switch to."""
    MODEL = "model"
    EFFORT = "effort"
    STRATEGY = "strategy"
    VALUE = "value"
    """A value for the argument before it, such as a choice a question offered or a
    value a setting accepts. It may hold spaces."""


@dataclass(frozen=True, slots=True)
class Spec:
    name: str
    usage: str
    summary: str
    arguments: tuple[Argument, ...] = ()


SPECS: Final = (
    Spec(
        "/answer",
        "/answer QUESTION TEXT",
        "answer a question",
        (Argument.QUESTION, Argument.VALUE),
    ),
    Spec("/ack", "/ack MESSAGE", "acknowledge a message", (Argument.MESSAGE,)),
    Spec("/release", "/release", "release the line from a hold"),
    Spec("/interrupt", "/interrupt", "stop the turn in flight, as Escape does"),
    Spec("/config", "/config", "show and edit the line's and project's settings"),
    Spec("/model", "/model [MODEL]", "switch the line's model", (Argument.LINE_MODEL,)),
    Spec("/effort", "/effort [LEVEL]", "set the reasoning effort", (Argument.EFFORT,)),
    Spec(
        "/set",
        "/set KEY VALUE",
        "change a setting, project.KEY for the project",
        (Argument.SETTING, Argument.VALUE),
    ),
    Spec("/unset", "/unset KEY", "return a setting to its layers", (Argument.SETTING,)),
    Spec("/clear", "/clear", "start the line's next turn in a new session"),
    Spec(
        "/create",
        "/create LINE MODEL [STRATEGY]",
        "create a line, on the subscription by default",
        (Argument.NEW_LINE, Argument.MODEL, Argument.STRATEGY),
    ),
    Spec("/reload", "/reload", "read the project file again"),
    Spec("/line", "/line LINE", "attach to another line", (Argument.LINE,)),
    Spec("/lines", "/lines", "list the daemon's lines"),
    Spec(
        "/project",
        "/project PROJECT[:LINE]",
        "attach to a line of another project in the fleet index",
        (Argument.PROJECT,),
    ),
    Spec("/projects", "/projects", "list the projects of the fleet index"),
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

MAX_SUGGESTIONS: Final = 10


def _usage(name: str) -> str:
    for spec in SPECS:
        if spec.name == name:
            return f"usage: {spec.usage}"
    raise KeyError(name)


def _project(rest: str) -> SwitchProject | Invalid:
    """``/project PROJECT[:LINE]``. **Review of 7ff033f, G4.** The rest of the input
    is the target whole, spaces and all, so a root that holds a space stays one root,
    and the line follows the last colon, as the fleet commands take ``PROJECT:LINE``.
    No line identifier holds a colon, so a root that holds one is given with its line,
    or with a final colon and no line."""
    project, colon, line_id = rest.rpartition(":")
    if not colon:
        project, line_id = rest, ""
    project = project.strip()
    if not project or " " in line_id.strip():
        return Invalid(_usage("/project"))
    return SwitchProject(project, line_id.strip() or None)


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
        case "/project":
            return _project(rest)
        case "/model" | "/effort":
            key = word[1:]
            if not rest:
                return ShowSettings(key)
            if " " in rest:
                return Invalid(_usage(word))
            return Set(key, rest)
        case "/set":
            key, _, value = rest.partition(" ")
            if not key:
                return Invalid(_usage(word))
            return Set(key, value.strip())
        case "/unset":
            if not rest or " " in rest:
                return Invalid(_usage(word))
            return Set(rest, None)
        case "/create":
            parts = rest.split()
            if len(parts) not in (2, 3):
                return Invalid(_usage(word))
            strategy = parts[2] if len(parts) == 3 else STRATEGIES[0]
            return CreateLine(parts[0], parts[1], strategy)
        case "/config" | "/clear" | "/reload":
            if rest:
                return Invalid(_usage(word))
            match word:
                case "/config":
                    return ShowSettings()
                case "/clear":
                    return ClearSession()
                case _:
                    return ReloadProject()
        case "/release" | "/interrupt" | "/lines" | "/projects" | "/activity" | "/mail":
            if rest:
                return Invalid(_usage(word))
            match word:
                case "/release":
                    return Release()
                case "/interrupt":
                    return Interrupt()
                case "/lines":
                    return ShowLines()
                case "/projects":
                    return ShowProjects()
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
    """What arguments may complete to, from the client's last fetch. Each pool holds
    a value and a description."""

    lines: tuple[tuple[str, str], ...] = ()
    projects: tuple[tuple[str, str], ...] = ()
    questions: tuple[tuple[str, str], ...] = ()
    messages: tuple[tuple[str, str], ...] = ()
    settings: tuple[tuple[str, str], ...] = ()
    """Each editable setting's key, project keys with their prefix."""
    line_models: tuple[tuple[str, str], ...] = ()
    models: tuple[tuple[str, str], ...] = ()
    efforts: tuple[tuple[str, str], ...] = ()
    values: tuple[tuple[str, str, tuple[str, ...]], ...] = ()
    """For a command and its first argument, the values that may follow, such as an
    ``/answer`` question's choices or a ``/set`` key's accepted values."""

    def pool(self, argument: Argument) -> tuple[tuple[str, str], ...]:
        match argument:
            case Argument.QUESTION:
                return self.questions
            case Argument.MESSAGE:
                return self.messages
            case Argument.LINE:
                return self.lines
            case Argument.PROJECT:
                return self.projects
            case Argument.SETTING:
                return self.settings
            case Argument.LINE_MODEL:
                return self.line_models
            case Argument.MODEL:
                return self.models
            case Argument.EFFORT:
                return self.efforts
            case Argument.STRATEGY:
                return tuple((strategy, "") for strategy in STRATEGIES)
            case Argument.NEW_LINE | Argument.VALUE:
                return ()


@dataclass(frozen=True, slots=True)
class Suggestion:
    text: str
    """The whole input once the suggestion is taken."""
    label: str
    detail: str


def suggest(text: str, choices: Choices) -> tuple[Suggestion, ...]:
    """Completions of ``text``, which is the whole input, at most ``MAX_SUGGESTIONS``.

    Only a single-row input that begins with a slash completes. The command name
    completes, then each argument from its pool, and a ``VALUE`` argument from the
    values its preceding argument offers.
    """
    if not text.startswith("/") or "\n" in text:
        return ()
    word, space, rest = text.partition(" ")
    if not space:
        found = [
            Suggestion(
                spec.name + (" " if spec.arguments else ""), spec.usage, spec.summary
            )
            for spec in SPECS
            if spec.name.startswith(word)
        ]
        return tuple(found[:MAX_SUGGESTIONS])
    spec = next((spec for spec in SPECS if spec.name == word), None)
    if spec is None or not spec.arguments:
        return ()
    words = rest.split(" ")
    index = len(words) - 1
    if index >= 1 and len(spec.arguments) > 1 and spec.arguments[1] is Argument.VALUE:
        first, _, partial = rest.partition(" ")
        offered = next(
            (
                v
                for command, key, v in choices.values
                if (command, key) == (word, first)
            ),
            (),
        )
        found = [
            Suggestion(f"{word} {first} {value}", value, "")
            for value in offered
            if value.startswith(partial)
        ]
        return tuple(found[:MAX_SUGGESTIONS])
    if index >= len(spec.arguments):
        return ()
    head = " ".join([word, *words[:-1]]) + " "
    trailing = " " if index + 1 < len(spec.arguments) else ""
    found = [
        Suggestion(f"{head}{value}{trailing}", value, detail)
        for value, detail in choices.pool(spec.arguments[index])
        if value.startswith(words[-1])
    ]
    return tuple(found[:MAX_SUGGESTIONS])
