"""Estimated prices, so that a line whose engine reports tokens and no cost is still
bounded by ``max_cost_usd``. **Round 15, item 1, required by the operator.**

Every cost computed here is an estimate, and every view that shows one says so. The
engine's own cost, where it reports one, is always preferred.

**Assumption, not verified.** The prices below were not read from any price list.
They are placeholders chosen by core on 2026-11-05 to be of a plausible order, and
higher rather than lower where unsure, so that an error holds a line early rather
than letting it spend past its limit. ``PRICES_VERIFIED`` stays ``False`` until an
operator replaces them with prices read from the provider, and every estimate says
that its prices are unverified while it is.

Units. Prices are whole millionths of a dollar per million tokens, and costs whole
millionths of a dollar, so no float enters the sum and every port computes the same
figure. A cost is rounded up, so an estimate never falls below the exact product.

Tokens. The Codex adapter reports ``input_tokens``, which include
``cached_input_tokens``, and ``output_tokens``, which include its reasoning tokens,
since its ``total_tokens`` were observed to be input and output alone, as
``dromon.codex.CODEX_USAGE_NOTE`` records. An engine whose usage lacks either count
gets no estimate, and its turn stays of unknown cost.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

TOKENS_PER_PRICE: Final = 1_000_000
"""Prices are per million tokens."""

MAX_TURN_TOKENS: Final = 10**12
"""The largest token count an estimate accepts for one kind of token in one turn. A
count past it is read as a fault of the report, not a turn, and gets no estimate."""


@dataclass(frozen=True, slots=True)
class Price:
    """One model's estimated prices, in millionths of a dollar per million tokens."""

    input_micro: int
    cached_input_micro: int
    output_micro: int

    def __post_init__(self) -> None:
        for name, value in (
            ("input_micro", self.input_micro),
            ("cached_input_micro", self.cached_input_micro),
            ("output_micro", self.output_micro),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a whole number of at least 0")


PRICES_VERIFIED: Final = False
"""Whether the prices below were read from the provider's price list. They were not."""

PRICES: Final[Mapping[str, Price]] = {
    # Codex models, by the names ``dromon.driver.ENGINE_OF`` gives them.
    "gpt-6-luna": Price(500_000, 50_000, 4_000_000),
    "gpt-6.1-terra": Price(2_000_000, 200_000, 16_000_000),
    "gpt-6.1-sol": Price(5_000_000, 500_000, 40_000_000),
}
"""Estimated prices by model. A model not here gets no estimate."""

ESTIMATE_NOTE: Final = (
    "estimated from the engine's token counts and Dromon's price table, whose prices"
    + (" are verified" if PRICES_VERIFIED else " are unverified placeholders")
)


def _count(usage: Mapping[str, int], key: str) -> int | None:
    value = usage.get(key)
    if type(value) is not int or not 0 <= value <= MAX_TURN_TOKENS:
        return None
    return value


def _ceil_div(numerator: int, denominator: int) -> int:
    return -(-numerator // denominator)


def estimate_micro_usd(model: str, usage: Mapping[str, int] | None) -> int | None:
    """The estimated cost of a turn on ``model`` with ``usage``, in millionths of a
    dollar, or ``None`` when the model has no price or the usage lacks input or output
    counts. Cached tokens are priced apart from the rest of the input, and a cached
    count above the input count is read as all of it cached."""
    price = PRICES.get(model)
    if price is None or usage is None:
        return None
    total_input = _count(usage, "input_tokens")
    output = _count(usage, "output_tokens")
    if total_input is None or output is None:
        return None
    cached = _count(usage, "cached_input_tokens") or 0
    cached = min(cached, total_input)
    product = (
        (total_input - cached) * price.input_micro
        + cached * price.cached_input_micro
        + output * price.output_micro
    )
    return _ceil_div(product, TOKENS_PER_PRICE)


def priced(model: str) -> bool:
    """Whether ``model`` has an estimated price."""
    return model in PRICES
