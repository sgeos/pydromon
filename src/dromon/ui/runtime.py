"""The shared driver of both platforms: it owns the model and performs its effects.

A platform calls ``tick`` on its timer, ``press`` for each key, ``wheel`` for the
mouse, and ``resize`` before it draws ``frame``. The runtime passes each event to
``update``, performs the effects that come back through its ``Daemon`` port, and feeds
their results back as events, until none remain.

Blocking. Requests to the daemon are synchronous, as the platforms' event loops are,
so a slow daemon stalls the client for at most the port's timeout. The local socket
normally answers within milliseconds.

Termination. Each dispatch handles at most ``MAX_STEPS`` events. Effects yield at most
one event each, and events that follow a fetch yield no further fetch, so the bound is
never reached in practice, and reaching it drops the rest rather than looping.
"""

from collections import deque
from collections.abc import Callable
from typing import Final, Protocol

from dromon.protocol import Operation, Response
from dromon.ui.commands import (
    Acknowledge,
    Answer,
    DaemonCommand,
    Interrupt,
    Release,
    Say,
)
from dromon.ui.data import activity_of, lines_of, view_of
from dromon.ui.keys import Key
from dromon.ui.model import (
    ActivityLoaded,
    Bell,
    Done,
    Effect,
    Event,
    Exit,
    Failed,
    FetchActivity,
    FetchLines,
    FetchView,
    LinesLoaded,
    Model,
    Pressed,
    Request,
    Resized,
    Tick,
    ViewLoaded,
    Wheel,
)
from dromon.ui.render import Frame, frame
from dromon.ui.update import initial, update

MAX_STEPS: Final = 64


class Unreachable(Exception):
    """The daemon could not be asked, with the reason to show."""


class Daemon(Protocol):
    """The port to the daemon. ``call`` raises ``Unreachable`` and nothing else."""

    def call(self, operation: Operation, args: dict[str, object]) -> Response: ...


def request_of(
    command: DaemonCommand, line_id: str, operator: str
) -> tuple[Operation, dict[str, object], str]:
    """The request a command makes, and the notice to show when it succeeds."""
    match command:
        case Say(body):
            args: dict[str, object] = {
                "recipient": line_id,
                "body": body,
                "operator": operator,
            }
            return Operation.SEND, args, f"sent to {line_id}"
        case Answer(question_id, body):
            args = {"question_id": question_id, "body": body, "operator": operator}
            return Operation.ANSWER, args, f"answered {question_id}"
        case Acknowledge(message_id):
            args = {"operator": operator, "message_id": message_id}
            return Operation.ACKNOWLEDGE, args, f"acknowledged {message_id}"
        case Release():
            return Operation.RELEASE, {"line_id": line_id}, f"released {line_id}"
        case Interrupt():
            return Operation.INTERRUPT, {"line_id": line_id}, f"interrupted {line_id}"


class Runtime:
    def __init__(
        self,
        daemon: Daemon,
        line_id: str,
        operator: str,
        poll_seconds: float,
        bell: Callable[[], None] = lambda: None,
    ) -> None:
        self._daemon = daemon
        self._bell = bell
        self._model = initial(line_id, operator, poll_seconds)
        self._frame: tuple[Model, Frame] | None = None

    @property
    def model(self) -> Model:
        return self._model

    @property
    def finished(self) -> bool:
        return self._model.finished

    def tick(self, now: float) -> None:
        self.dispatch(Tick(now))

    def press(self, key: Key) -> None:
        self.dispatch(Pressed(key))

    def wheel(self, rows: int) -> None:
        self.dispatch(Wheel(rows))

    def resize(self, width: int, height: int) -> None:
        if (width, height) != (self._model.width, self._model.height):
            self.dispatch(Resized(width, height))

    def frame(self) -> Frame:
        """The current frame, drawn again only when the model changed."""
        if self._frame is None or self._frame[0] is not self._model:
            self._frame = (self._model, frame(self._model))
        return self._frame[1]

    def dispatch(self, event: Event) -> None:
        queue: deque[Event] = deque([event])
        for _ in range(MAX_STEPS):
            if not queue:
                return
            self._model, effects = update(self._model, queue.popleft())
            for effect in effects:
                result = self._perform(effect)
                if result is not None:
                    queue.append(result)

    def _perform(self, effect: Effect) -> Event | None:
        match effect:
            case FetchView(line_id):
                response = self._ask(Operation.LINE_VIEW, {"line_id": line_id})
                if isinstance(response, Failed):
                    return response
                return ViewLoaded(line_id, view_of(response))
            case FetchActivity(line_id, after):
                response = self._ask(
                    Operation.LINE_ACTIVITY, {"line_id": line_id, "after": after}
                )
                if isinstance(response, Failed):
                    return response
                return ActivityLoaded(line_id, activity_of(response))
            case FetchLines():
                response = self._ask(Operation.STATUS, {})
                if isinstance(response, Failed):
                    return response
                return LinesLoaded(lines_of(response))
            case Request(command, line_id, operator):
                operation, args, done = request_of(command, line_id, operator)
                response = self._ask(operation, args)
                if isinstance(response, Failed):
                    return response
                if (
                    operation is Operation.INTERRUPT
                    and response.get("interrupted") is False
                ):
                    return Done(f"{line_id} has no turn in flight")
                return Done(done)
            case Bell():
                self._bell()
                return None
            case Exit():
                return None

    def _ask(
        self, operation: Operation, args: dict[str, object]
    ) -> dict[str, object] | Failed:
        try:
            response = self._daemon.call(operation, args)
        except Unreachable as error:
            return Failed(str(error))
        if not response.ok:
            return Failed(f"{operation}: {response.error_message or 'refused'}")
        return response.result or {}
