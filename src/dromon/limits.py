"""Conversation limits: when the daemon holds a line and asks an operator about it.

Two lines can wake each other without end, and a line can run up cost without anyone
watching. The scheduler therefore counts, for each line, the turns it has taken in a
row without mail from an operator, and the estimated cost of its turns since it was
last released. When either passes its limit, the line is held: it takes no turns, its
mail waits, and the daemon asks an operator whether to resume it. Any reply to that
question releases the line and resets both counts.

The defaults are deliberately simple and expected to be tuned. Per-project values come
from the project configuration of roadmap task 9.

Assumptions and limits.

- The cost is the Agent SDK's client-side estimate. On a subscription it is not a
  charge, but it still measures how much work a line is doing.
- The counts live in memory. A restart resets them, while a hold, which is in the event
  log, survives.
- The cooldown uses the monotonic clock, so it is the one part of scheduling that
  depends on wall time.
"""

from dataclasses import dataclass
from typing import Final

DEFAULT_MAX_AUTONOMOUS_TURNS: Final = 12
DEFAULT_MAX_COST_USD: Final = 2.0
DEFAULT_COOLDOWN_SECONDS: Final = 0.0

MAX_AUTONOMOUS_TURNS: Final = 10_000
MAX_COST_USD: Final = 10_000.0
MAX_COOLDOWN_SECONDS: Final = 86_400.0


@dataclass(frozen=True, slots=True)
class Limits:
    max_autonomous_turns: int = DEFAULT_MAX_AUTONOMOUS_TURNS
    """Turns a line may take in a row without mail from an operator."""
    max_cost_usd: float = DEFAULT_MAX_COST_USD
    """Estimated cost a line may run up between releases."""
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    """The least time between the end of one of a line's turns and the next."""

    def __post_init__(self) -> None:
        if type(self.max_autonomous_turns) is not int or not (
            1 <= self.max_autonomous_turns <= MAX_AUTONOMOUS_TURNS
        ):
            raise ValueError(
                f"max_autonomous_turns must be in [1, {MAX_AUTONOMOUS_TURNS}]"
            )
        if not 0 < self.max_cost_usd <= MAX_COST_USD:
            raise ValueError(f"max_cost_usd must be in (0, {MAX_COST_USD}]")
        if not 0 <= self.cooldown_seconds <= MAX_COOLDOWN_SECONDS:
            raise ValueError(f"cooldown_seconds must be in [0, {MAX_COOLDOWN_SECONDS}]")


DEFAULT_LIMITS: Final = Limits()
