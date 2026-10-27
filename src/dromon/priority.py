"""Priority mail: a note that ends a line's running turn at a safe point.

**Decided by the operator on 2026-10-27.** An operator or a line may send a line
priority mail, so that a long turn can be redirected. The scheduler records the note
as ``message_sent`` followed by ``message_prioritized``. When the recipient runs a turn,
the turn is asked to end at its next safe point, and the turn that follows leads with
the priority mail and starts without waiting for the line's cooldown or wake tick.

Safe point. Every engine adapter feeds the events of a turn to the line's Keleusma
driver, and ends the turn gracefully when the driver answers ``interrupt``, so the
engine reports the turn and keeps its session. ``SteeredDriver`` answers ``interrupt``
at the first ``text`` or ``thinking`` event after the priority mail arrives, at which
the model is writing or reasoning. It never interrupts at a ``tool_use`` event, since
the tool may already be starting. What that means on each engine is in
``docs/protocol/PRIORITY.md``, and in short it is this.

- Claude Code. A text or thinking block, after which the adapter sends the SDK's
  interrupt request. **Hypothesis, not verified live.** Claude Code streams each
  content block as it completes, so a text block arrives before a tool call that
  follows it in the same message has started.
- Codex. A completed agent message or a started reasoning item, then
  ``turn/interrupt``.
- Grok and Antigravity over ACP. A message or thought chunk, then ``session/cancel``.
- Pi. A text delta, or reasoning as it starts, then Pi's ``abort`` command.
- Cursor Agent. A text or thinking event, after which the adapter ends the process,
  because Cursor takes no command once it starts.

A turn that reaches no such event, such as one long tool call to its end, is not
interrupted. Its priority mail then leads the next turn. A muted line is never
interrupted by priority mail, which waits with its other mail.

Rate limit. Priority mail from lines is limited, so that one line cannot keep another
from working. A line may send at most ``MAX_PER_SENDER`` priority notes, and a line may
receive at most ``MAX_PER_RECIPIENT`` from lines, within ``WINDOW_SECONDS``. A note
over either is refused, and nothing is recorded, so the line can send it without
priority. Operator priority mail is unlimited. External agents may not send priority
mail, by default deny, since they have no operator authority.

Porting note. The limiter holds two bounded deques of monotonic times per party, and the
driver override is one comparison per event, so both map onto fixed-capacity rings and
a branch in a Keleusma stream processor.
"""

from collections import deque
from collections.abc import Callable
from typing import Final

from dromon.driver import (
    INITIAL_STATE,
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

SAFE_POINT_EVENTS: Final = frozenset({"text", "thinking"})
"""The driver events at which a turn may end for priority mail."""

REASON: Final = "priority mail {message_id} from {sender}"
"""Why a turn ended for priority mail, as its report's ``interrupted_for`` says."""


def _no_redirect() -> str | None:
    return None


class SteeredDriver(Driver):
    """A line's driver that also ends the turn for priority mail at a safe point.

    ``redirect`` is asked at each safe point event the real driver lets continue. It
    returns the reason to end the turn, or ``None``. The real driver sees every event
    first, so its state advances exactly as it would without the override."""

    def __init__(self, source: str, *, state: DriverState = INITIAL_STATE) -> None:
        super().__init__(source, state=state)
        self.redirect: Callable[[], str | None] = _no_redirect

    def step(self, event: Event) -> Action:
        action = super().step(event)
        if action.tag is not ActionTag.CONTINUE or event.tag not in SAFE_POINT_EVENTS:
            return action
        reason = self.redirect()
        if reason is None:
            return action
        return Action(ActionTag.INTERRUPT, reason, 0)


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

    def check(self, sender: str, recipient: str) -> None:
        """Raise ``PriorityRefused`` when line ``sender`` may not send line
        ``recipient`` priority mail now."""
        sent = self._recent(self._sent, sender)
        if len(sent) >= MAX_PER_SENDER:
            raise PriorityRefused(
                f"line {sender} sent {len(sent)} priority messages in the last"
                f" {int(WINDOW_SECONDS)} seconds, the most a line may, so this one was"
                f" not sent. Send it without priority, or again in {self._wait(sent)}"
                " seconds"
            )
        received = self._recent(self._received, recipient)
        if len(received) >= MAX_PER_RECIPIENT:
            raise PriorityRefused(
                f"line {recipient} received {len(received)} priority messages from"
                f" lines in the last {int(WINDOW_SECONDS)} seconds, the most a line"
                " may, so this one was not sent. Send it without priority, or again in"
                f" {self._wait(received)} seconds"
            )

    def record(self, sender: str, recipient: str) -> None:
        """Count a priority note that was sent."""
        self._recent(self._sent, sender).append(self._clock())
        self._recent(self._received, recipient).append(self._clock())
