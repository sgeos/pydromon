"""What an attached client shows and does, shared by the curses and graphical fronts.

The client is the front end and the daemon the back end, as in a multiplayer game, so
many clients may attach to different lines, or the same line, at once. A client polls
``line_view`` for what to show and sends operator commands as ordinary protocol
requests. Pushed updates are a later protocol version.

Everything here is pure or talks only to the daemon, so the curses and Tk modules are
thin shells around it, and its behaviour is tested without a terminal.

Commands typed into a client.

- Plain text sends a message to the attached line.
- ``/answer QUESTION TEXT`` answers one of the line's questions.
- ``/ack MESSAGE`` acknowledges a message the line sent the operator.
- ``/release`` releases the line from a conversation hold.
- ``/help`` lists the commands, and ``/quit`` detaches.
"""

import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, cast

from dromon.client import DaemonNotRunning, call
from dromon.paths import ProjectPaths
from dromon.protocol import ErrorCode, Operation, ProtocolError, Response

HELP: Final = (
    "text sends a message to the line.  /answer QUESTION TEXT  /ack MESSAGE"
    "  /release  /help  /quit"
)


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
class Help:
    pass


@dataclass(frozen=True, slots=True)
class Quit:
    pass


@dataclass(frozen=True, slots=True)
class Invalid:
    reason: str


type Command = Say | Answer | Acknowledge | Release | Help | Quit | Invalid


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
                return Invalid("usage: /answer QUESTION TEXT")
            return Answer(question_id, body.strip())
        case "/ack":
            if not rest or " " in rest:
                return Invalid("usage: /ack MESSAGE")
            return Acknowledge(rest)
        case "/release":
            return Release() if not rest else Invalid("usage: /release")
        case "/help":
            return Help()
        case "/quit" | "/exit":
            return Quit()
        case _:
            return Invalid(f"unknown command {word}. {HELP}")


def _text(value: object) -> str:
    return "" if value is None else str(value)


def render(view: dict[str, object], width: int) -> list[str]:
    """The view as lines of at most ``width`` characters, oldest message first."""
    width = max(width, 20)
    line = cast(dict[str, object], view.get("line", {}))
    held = line.get("held_by")
    header = (
        f"{_text(line.get('line_id'))}  {_text(line.get('engine'))}"
        f" {_text(line.get('strategy'))}  {_text(line.get('state'))}"
        f"  tick {_text(view.get('tick'))}  turns {_text(line.get('turns'))}"
        f"  pending {_text(line.get('pending'))}"
    )
    out = textwrap.wrap(header, width) or [""]
    if held is not None:
        out.extend(textwrap.wrap(f"HELD until {held} is answered, or /release", width))
    out.append("-" * width)
    messages = cast(list[dict[str, object]], view.get("messages", []))
    total = view.get("total")
    if isinstance(total, int) and total > len(messages):
        out.append(f"... {total - len(messages)} earlier messages not shown")
    for message in messages:
        title = (
            f"{_text(message.get('message_id'))} {_text(message.get('sender'))}"
            f" -> {_text(message.get('recipient'))} {_text(message.get('kind'))}"
        )
        if message.get("reply_to") is not None:
            title += f" re {_text(message.get('reply_to'))}"
        if message.get("status") is not None:
            title += f" [{_text(message.get('status'))}]"
        out.extend(textwrap.wrap(title, width))
        body = _text(message.get("body"))
        if message.get("truncated") is True:
            body += " ..."
        for paragraph in body.splitlines() or [""]:
            out.extend(
                textwrap.wrap(
                    paragraph, width, initial_indent="  ", subsequent_indent="  "
                )
                or ["  "]
            )
    questions = cast(list[dict[str, object]], view.get("open_questions", []))
    if questions:
        out.append("-" * width)
        out.append("Open questions from this line:")
        for question in questions:
            text = (
                f"{_text(question.get('question_id'))} to"
                f" {_text(question.get('operator'))}: {_text(question.get('text'))}"
            )
            out.extend(textwrap.wrap(text, width, subsequent_indent="  "))
    return [entry[:width] for entry in out]


type Caller = Callable[[Operation, dict[str, object]], Response]


class Attachment:
    """One client attached to one line, speaking for one operator."""

    def __init__(
        self,
        paths: ProjectPaths,
        line_id: str,
        operator: str,
        caller: Caller | None = None,
    ) -> None:
        self.line_id = line_id
        self.operator = operator
        self._call: Caller = caller or (
            lambda operation, args: call(paths.socket, operation, args=args)
        )

    def fetch(self) -> dict[str, object]:
        """The current view, or raise ``DaemonNotRunning`` or ``ProtocolError``."""
        response = self._call(Operation.LINE_VIEW, {"line_id": self.line_id})
        if not response.ok or response.result is None:
            raise ProtocolError(
                response.error_code or ErrorCode.INTERNAL,
                response.error_message or "no view",
            )
        return response.result

    def execute(self, command: Command) -> str:
        """Perform ``command`` and return a one-line status for the client."""
        match command:
            case Say(body):
                return self._send(
                    Operation.SEND,
                    {
                        "recipient": self.line_id,
                        "body": body,
                        "operator": self.operator,
                    },
                    "sent",
                )
            case Answer(question_id, body):
                return self._send(
                    Operation.ANSWER,
                    {
                        "question_id": question_id,
                        "body": body,
                        "operator": self.operator,
                    },
                    f"answered {question_id}",
                )
            case Acknowledge(message_id):
                return self._send(
                    Operation.ACKNOWLEDGE,
                    {"operator": self.operator, "message_id": message_id},
                    f"acknowledged {message_id}",
                )
            case Release():
                return self._send(
                    Operation.RELEASE, {"line_id": self.line_id}, "released"
                )
            case Help():
                return HELP
            case Quit():
                return "detaching"
            case Invalid(reason):
                return reason

    def _send(self, operation: Operation, args: dict[str, object], done: str) -> str:
        try:
            response = self._call(operation, args)
        except DaemonNotRunning:
            return "the daemon is not running"
        except (ProtocolError, TimeoutError, OSError) as error:
            return f"error: {error}"
        if not response.ok:
            return f"refused: {response.error_message}"
        return done
