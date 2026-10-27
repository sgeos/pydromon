"""The keep-session ceiling of a kept engine connection, for Claude Code and Codex.

**The operator's decision A of 2026-10-26.** A kept connection that has background
work still running when it reaches ``keep_session_max_seconds`` after the line's last
turn does what ``keep_session_at_max`` says.

- ``close`` closes it at once, the behaviour before the choice existed.
- ``ask`` raises an operator question and keeps it until the question is answered
  with ``extend``, which allows another ``keep_session_max_seconds`` and then asks
  again, ``stop``, which closes it at once, or ``let it finish``, which keeps it while
  the work runs.
- ``keep`` keeps it while the work runs.

Every choice is bounded by ``keep_session_hard_seconds`` after the last turn, at which
the connection closes whatever was chosen, so no wait is unbounded. A connection with
no background work closes after ``keep_session_idle_seconds`` as before. A turn starts
the count again, and withdraws a question still open, whose premise it has changed,
while a ``let it finish`` stands for the rest of the connection's life.

An answer that is not one of the three choices, after its case and surrounding space
are set aside, decides nothing, and the connection then closes at the hard bound as an
unanswered one does. The answer still reaches the line as mail.

Porting note. ``Ceiling`` is plain state with a deadline function, and each engine
calls it from its own watch, so it maps onto a small state machine in a port.
"""

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from dromon.launcher import StopPolicy
from dromon.lineconfig import AtMax, LineConfig


class Choice(StrEnum):
    """An operator's answer to a ceiling question."""

    EXTEND = "extend"
    STOP = "stop"
    FINISH = "let it finish"


CHOICES: Final = tuple(choice.value for choice in Choice)

IDLE_REASON: Final = "it waited keep_session_idle_seconds without a turn"
MAX_REASON: Final = (
    "it reached keep_session_max_seconds with background tasks still running"
)
HARD_REASON: Final = (
    "it reached keep_session_hard_seconds, the hard bound, with background tasks"
    " still running"
)
STOP_REASON: Final = (
    "an operator answered stop when it reached keep_session_max_seconds with"
    " background tasks still running"
)
NO_OPERATOR_REASON: Final = (
    "it reached keep_session_max_seconds with background tasks still running, and"
    " no operator could be asked"
)


type Ask = Callable[[int], str | None]
"""Raises an operator question for a connection with the given number of background
tasks, and returns its identifier, or ``None`` when none could be raised."""

type Settle = Callable[[str], object]
"""Withdraws the question with the given identifier, if it is still open."""


def no_operator(tasks: int) -> None:
    """The default for an engine nobody asks for, which therefore closes at the
    ceiling, as ``close`` does."""
    del tasks


def nothing_to_settle(question_id: str) -> None:
    del question_id


def parse_choice(answer: str) -> Choice | None:
    """The choice ``answer`` makes, or ``None`` for an answer that makes none."""
    text = " ".join(answer.split()).lower()
    return next((choice for choice in Choice if choice.value == text), None)


@dataclass(slots=True)
class Ceiling:
    """Where one kept connection stands against its ceiling."""

    config: LineConfig
    ask: Ask = no_operator
    settle: Settle = nothing_to_settle
    question: str | None = None
    """The open question, while one is asked and unanswered."""
    soft: float | None = None
    """The ceiling an ``extend`` moved, in the engine's clock."""
    finishing: bool = False
    """Whether an operator chose ``let it finish``."""

    def deadline(self, last_end: float, busy: bool) -> float:
        """When the connection closes, in the engine's clock, given the end of the
        last turn and whether background work runs."""
        if not busy:
            return last_end + self.config.keep_session_idle_seconds
        hard = last_end + self.config.keep_session_hard_seconds
        if (
            self.finishing
            or self.question is not None
            or self.config.keep_session_at_max is AtMax.KEEP
        ):
            return hard
        soft = (
            self.soft
            if self.soft is not None
            else last_end + self.config.keep_session_max_seconds
        )
        return min(soft, hard)

    def due(self, last_end: float, tasks: int, now: float) -> str | None:
        """At the deadline, the reason to close the connection, or ``None`` when it
        stays, because the deadline has not passed or an operator was just asked.
        Asks at most once per ceiling."""
        busy = tasks > 0
        if now < self.deadline(last_end, busy):
            return None
        if not busy:
            return IDLE_REASON
        if now >= last_end + self.config.keep_session_hard_seconds:
            return HARD_REASON
        if self.config.keep_session_at_max is not AtMax.ASK:
            return MAX_REASON
        question = self.ask(tasks)
        if question is None:
            return NO_OPERATOR_REASON
        self.question = question
        return None

    def decide(self, question_id: str, answer: str, now: float) -> Choice | None:
        """Apply an operator's answer to the open question ``question_id``. Returns
        the choice made, which the engine carries out, or ``None`` when the answer is
        for no open question or makes no choice. ``STOP`` closes the connection,
        which only the engine can do."""
        if question_id != self.question:
            return None
        choice = parse_choice(answer)
        if choice is None:
            return None
        self.question = None
        if choice is Choice.EXTEND:
            self.soft = now + self.config.keep_session_max_seconds
        elif choice is Choice.FINISH:
            self.finishing = True
        return choice

    def turn_ended(self) -> None:
        """Start the count again after a turn, withdrawing an open question."""
        self.soft = None
        self.withdraw()

    def withdraw(self) -> None:
        """Withdraw an open question, as the connection closes or a turn starts."""
        question, self.question = self.question, None
        if question is not None:
            self.settle(question)


QUESTION_MARKER: Final = "'s kept engine session reached keep_session_max_seconds,"
"""The words every ceiling question holds after its line's name, by which ``recover``
tells one in the log. **Review F11.**"""


def is_question(body: str, choices: tuple[str, ...]) -> bool:
    """Whether a question's text and choices are a ceiling question's. A line's own
    question written to match is withdrawn with them at a restart, which takes it no
    further than the line's own question, and is accepted."""
    if tuple(choices) != CHOICES or not body.startswith("Line "):
        return False
    # A line's name holds no apostrophe, so the marker begins at the first one.
    line_id, mark, rest = body.removeprefix("Line ").partition("'")
    return bool(line_id and mark) and (mark + rest).startswith(QUESTION_MARKER)


def question_text(line_id: str, tasks: int, config: LineConfig) -> str:
    """The operator question a ceiling raises. Under ``leave_background`` a close
    leaves the work running, and the question says so rather than promise its end."""
    stop = (
        "stop to close it now, which by the leave_background stop policy leaves its"
        " background work running and records it in the line's status,"
        if config.stop_policy is StopPolicy.LEAVE_BACKGROUND
        else "stop to close it and end its background work now,"
    )
    return (
        f"Line {line_id}'s kept engine session reached keep_session_max_seconds,"
        f" {config.keep_session_max_seconds} seconds after its last turn, with {tasks}"
        " background tasks still running. Answer extend to keep it for another"
        f" {config.keep_session_max_seconds} seconds, {stop}"
        " or let it finish to keep it while the work runs. It"
        " closes at the hard bound of keep_session_hard_seconds,"
        f" {config.keep_session_hard_seconds} seconds after the last turn, whatever"
        " the answer, and stays open until then while no answer comes."
    )
