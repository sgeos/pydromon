"""Priority mail: a note that ends a line's running turn at a safe point.

**Decided by the operator on 2026-10-27.** An operator or a line may send a line
priority mail, so that a long turn can be redirected. The scheduler records the note
as ``message_sent`` followed by ``message_prioritized``. When the recipient runs a turn,
the turn is asked to end at its next safe point, and the turn that follows leads with
the priority mail and starts without waiting for the line's cooldown or wake tick.

Safe point. Every engine adapter feeds the events of a turn to the line's Keleusma
driver, and ends the turn gracefully when the driver answers ``interrupt``, so the
engine reports the turn and keeps its session. **Decided by the operator on
2026-10-27, after the review of the first version.** A turn may end at one of two
points, and nowhere else.

- After a tool result is delivered and before the next model call, which an adapter
  reports as a ``tool_result`` event once a tool the turn used has finished. It is a
  safe point only when every tool use the turn reported has its result, so a turn with
  calls running in parallel, or a subagent's calls inside a task, is not ended while
  any of them runs.
- At the end of an assistant message that holds no tool use, which an adapter reports
  as a ``message_end`` event whose number is the message's count of tool uses.

A ``text`` or ``thinking`` event is never a safe point, because the same model message
can hold a tool use after it, and the engine may start that tool before the interrupt
lands. The first version ended turns there, and the live check of 2026-10-27 passed
only because the text came in its own message. ``SteeredDriver`` counts the tool uses
still waiting for a result, from the turn's ``turn_start`` on, and asks its steer only
at the two points. The packaged drivers answer ``continue`` to both events, so their
state is unchanged. What each engine reports is in ``docs/protocol/PRIORITY.md``, and in
short it is this.

- Claude Code. A tool result block in the user message the engine echoes once it has
  delivered the result, then the SDK's interrupt request. **Inference, not verified
  live.** The engine echoes the result as it sends the next model call, so the
  interrupt ends that call before it can start a tool.
- Codex. A tool item's completion, then ``turn/interrupt``.
- Grok and Antigravity over ACP. A tool call's update to ``completed`` or ``failed``,
  then ``session/cancel``.
- Pi. ``tool_execution_end``, or a ``message_end`` without tool calls, then Pi's
  ``abort`` command.
- Cursor Agent. A tool call's ``completed`` record, after which the adapter ends the
  process, because Cursor takes no command once it starts.

Only Pi reports the end of a message that holds no tool use. On the other engines such
a message ends the turn, so its end is no point to interrupt at.

A turn that reaches no safe point, such as one that makes no tool call or one long tool
call to its end, is not interrupted. Its priority mail then leads the next turn. A
hibernated line is never interrupted by priority mail, which waits with its other mail.

Rate limit. Priority mail from lines is limited, so that one line cannot keep another
from working. A line may send at most ``MAX_PER_SENDER`` priority notes, and a line may
receive at most ``MAX_PER_RECIPIENT`` from lines, within ``WINDOW_SECONDS``. A note
over either is refused, and nothing is recorded, so the line can send it without
priority. Operator priority mail is unlimited. External agents may not send priority
mail, by default deny, since they have no operator authority.

Peers. Priority mail to a line of a peer counts toward its sender's limit here, and the
peer applies the recipient's limit. Priority mail from a peer follows the rules for a
line whatever the remote sender is, with the whole peer counted as one sender under
``peer_key``, so many sender names cannot multiply the limit. Past either limit the
receiver records the note as ordinary mail rather than refusing it, as
``docs/architecture/FEDERATION.md`` decides.

Porting note. The limiter holds two bounded deques of monotonic times per party, and the
driver override is one comparison per event, so both map onto fixed-capacity rings and
a branch in a Keleusma stream processor.
"""

from collections import deque
from collections.abc import Callable
from typing import Final

from dromon.driver import (
    INITIAL_STATE,
    MESSAGE_END,
    TOOL_RESULT,
    Action,
    ActionTag,
    Driver,
    DriverState,
    Event,
    Strategy,
)

WINDOW_SECONDS: Final = 600.0
"""The window over which a line's priority mail is counted."""

MAX_PER_SENDER: Final = 3
"""Priority notes a line may send within the window, to all recipients."""

MAX_PER_RECIPIENT: Final = 2
"""Priority notes a line may receive from lines within the window."""

CAPACITY: Final = max(MAX_PER_SENDER, MAX_PER_RECIPIENT)
"""How many times a party's deque keeps, enough for either limit."""

PEER_KEY: Final = "peer:{peer_id}"
"""The sender a peer is counted as. A line identifier holds no colon, so no line
shares it."""

REASON: Final = "priority mail {message_id} from {sender}"
"""Why a turn ended for priority mail, as its report's ``interrupted_for`` says."""


def _no_redirect() -> str | None:
    return None


class SteeredDriver(Driver):
    """A line's driver that also ends the turn for priority mail at a safe point.

    ``redirect`` is asked only at a safe point that the real driver lets continue. It
    returns the reason to end the turn, or ``None``. The real driver sees every event
    first, so its state advances exactly as it would without the override.

    ``pending`` counts the turn's tool uses still waiting for a result. A result that
    no tool use announced cannot take it below zero. A tool use whose result never
    arrives keeps every later point unsafe, so the turn is not ended, which errs on
    the side of the tool."""

    def __init__(self, source: str, *, state: DriverState = INITIAL_STATE) -> None:
        super().__init__(source, state=state)
        self.redirect: Callable[[], str | None] = _no_redirect
        self.pending = 0

    def close(self) -> None:
        """Break the scheduler callback edge before releasing the thread-affine VM."""
        self.redirect = _no_redirect
        super().close()

    def step(self, event: Event) -> Action:
        action = super().step(event)
        if event.tag == "turn_start":
            self.pending = 0
        elif event.tag == "tool_use":
            self.pending += 1
        elif event.tag == TOOL_RESULT:
            self.pending = max(0, self.pending - 1)
        if action.tag is not ActionTag.CONTINUE or not self._safe(event):
            return action
        reason = self.redirect()
        if reason is None:
            return action
        return Action(ActionTag.INTERRUPT, reason, 0)

    def _safe(self, event: Event) -> bool:
        """Whether the turn may end at ``event``, with ``pending`` already updated."""
        if self.pending != 0:
            return False
        if event.tag == TOOL_RESULT:
            return True
        return event.tag == MESSAGE_END and event.number == 0


def steered(
    model: str, strategy: Strategy, redirect: Callable[[], str | None]
) -> SteeredDriver:
    """The packaged driver for ``model`` and ``strategy``, steered by ``redirect``."""
    driver = SteeredDriver.for_model(model, strategy)
    if not isinstance(driver, SteeredDriver):
        raise TypeError("the driver was not built as a SteeredDriver")
    driver.redirect = redirect
    return driver


class PriorityRefused(ValueError):
    """A line's priority note is over the rate limit."""


class PriorityLimiter:
    """Counts lines' priority mail by sender and by recipient over the window."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._sent: dict[str, deque[float]] = {}
        self._received: dict[str, deque[float]] = {}

    def _recent(self, store: dict[str, deque[float]], key: str) -> deque[float]:
        times = store.setdefault(key, deque(maxlen=CAPACITY))
        now = self._clock()
        # Bounded by the deque's capacity.
        while times and now - times[0] >= WINDOW_SECONDS:
            times.popleft()
        return times

    def _wait(self, times: deque[float]) -> int:
        """Whole seconds until the oldest counted note leaves the window."""
        return max(1, int(WINDOW_SECONDS - (self._clock() - times[0]) + 0.999))

    def check(self, sender: str, recipient: str | None) -> None:
        """Raise ``PriorityRefused`` when line ``sender`` may not send line
        ``recipient`` priority mail now. ``None`` is a line of a peer, whose limit
        the peer applies."""
        sent = self._recent(self._sent, sender)
        if len(sent) >= MAX_PER_SENDER:
            raise PriorityRefused(
                f"line {sender} sent {len(sent)} priority messages in the last"
                f" {int(WINDOW_SECONDS)} seconds, the most a line may, so this one was"
                f" not sent. Send it without priority, or again in {self._wait(sent)}"
                " seconds"
            )
        if recipient is None:
            return
        received = self._recent(self._received, recipient)
        if len(received) >= MAX_PER_RECIPIENT:
            raise PriorityRefused(
                f"line {recipient} received {len(received)} priority messages from"
                f" lines in the last {int(WINDOW_SECONDS)} seconds, the most a line"
                " may, so this one was not sent. Send it without priority, or again in"
                f" {self._wait(received)} seconds"
            )

    def allows(self, sender: str, recipient: str) -> bool:
        """Whether ``sender`` may send line ``recipient`` priority mail now, as
        ``check`` decides, for a receiver that declines rather than refuses."""
        return (
            len(self._recent(self._sent, sender)) < MAX_PER_SENDER
            and len(self._recent(self._received, recipient)) < MAX_PER_RECIPIENT
        )

    def record(self, sender: str, recipient: str | None) -> None:
        """Count a priority note that was sent. ``None`` is a line of a peer."""
        self._recent(self._sent, sender).append(self._clock())
        if recipient is not None:
            self._recent(self._received, recipient).append(self._clock())


def peer_key(peer_id: str) -> str:
    """The sender ``peer_id`` is counted as."""
    return PEER_KEY.format(peer_id=peer_id)
