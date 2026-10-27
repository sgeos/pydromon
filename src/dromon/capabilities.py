"""What each engine can do, as plain data, one table per engine.

**Rows 7 and 10 of the T3 Code review, 2026-10-27.** Parity between engines was spread
across frozensets, ``unsupported()`` functions and prose. Each engine now has one
``EngineTable``, and the refusals and the probe's report that depend on a capability
are derived from it. A port carries the tables across as data.

Unknown is not empty. Each capability is ``YES``, ``NO`` or ``UNKNOWN``, and a minimum
version is ``None`` when Dromon was never run against a live engine of that kind, so no
version can be named as tested. An unknown capability is refused wherever Dromon needs
it, as an absent one is, but it is reported as unknown, so an operator can tell a
limit of the engine from a gap in what Dromon has observed.

Minimum versions. Each adapter refuses an engine that reports a version below the one
it was tested against, at the start of each turn, which the probe runs too. A version
is read as the one run of dot-separated numbers its text holds, so ``2.1.310`` and
``2026.10.01-e373342`` both compare as numbers, and text with two versions is
unreadable. An engine never run live has no minimum, so none is enforced, and the
probe's report says so. An engine whose table names a minimum
but whose report holds no version that can be read is refused, since Dromon cannot
tell it was tested.

Grok, Codex and Antigravity. Their adapters were being changed on another branch when
this module was written, so their tables hold only what this branch can state, and
their rows are to be filled once that branch is merged. Their tables say so.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Known(StrEnum):
    """Whether an engine has a capability, as Dromon has observed or read."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


CAPABILITY_NAMES: Final = (
    "resume",
    "kept_session",
    "own_tools",
    "deny_before_run",
    "cost_report",
    "max_turns",
    "credential_visible",
    "sign_in_check",
)
"""The capabilities each table states, in the order a report lists them."""


@dataclass(frozen=True, slots=True)
class EngineTable:
    """One engine's capabilities and the version it was tested against."""

    engine: str
    display: str
    """The engine's name in prose, as a refusal names it."""
    minimum_version: str | None
    """The lowest version Dromon was run against live, or ``None`` when it never was."""
    tested_on: str | None
    """The calendar date of that run, ``YYYY-MM-DD``, or ``None``."""
    resume: Known
    """Whether a line's engine session can be continued in a later turn."""
    kept_session: Known
    """Whether a connection can be kept between turns."""
    own_tools: Known
    """Whether Dromon's own tools reach the model, through MCP or an extension."""
    deny_before_run: Known
    """Whether every built-in tool is denied before it runs, not stopped after."""
    cost_report: Known
    """Whether the engine reports a cost for each turn."""
    max_turns: Known
    """Whether a bound on model calls in a turn is enforced, by the engine or by
    Dromon's own count."""
    credential_visible: Known
    """Whether Dromon can tell from the engine which credential a turn uses."""
    sign_in_check: Known
    """Whether the engine offers a way to check its sign-in without a turn."""
    pending: str | None = None
    """What is yet to be filled in, for a table this branch could not complete."""

    def capabilities(self) -> dict[str, str]:
        """Each capability by name, in ``CAPABILITY_NAMES`` order."""
        return {name: str(getattr(self, name)) for name in CAPABILITY_NAMES}


_VERSION: Final = re.compile(r"(?<![\w.])v?(\d+(?:\.\d+)*)(?!\w)")
"""A version standing alone, dot-separated numbers after an optional ``v``, not part
of a longer word such as the ``e373342`` of a commit or the ``2`` of ``beta.2``."""


def version_numbers(text: str | None) -> tuple[int, ...] | None:
    """The version ``text`` holds, as its numbers, or ``None`` when it holds none, or
    more than one. Bounded by the text's first 256 characters.

    **Review of 2026-10-27, C3.** The first number anywhere was read, so a report such
    as ``node 20, agy 1.0.0`` gave 20. An adapter reads the version from the field the
    engine names for it, as the Claude Code adapter reads ``claude_code_version``, and
    text that still holds two different versions is unreadable, which an engine with
    a minimum is refused for."""
    if text is None:
        return None
    found = set(_VERSION.findall(text[:256]))
    if len(found) != 1:
        return None
    return tuple(int(part) for part in found.pop().split(".")[:8])


def below(reported: tuple[int, ...], minimum: tuple[int, ...]) -> bool:
    """Whether ``reported`` is lower than ``minimum``, padding the shorter with
    zeros, so ``2.1`` equals ``2.1.0``."""
    width = max(len(reported), len(minimum))
    padded = reported + (0,) * (width - len(reported))
    least = minimum + (0,) * (width - len(minimum))
    return padded < least


def version_refusal(table: EngineTable, reported: str | None) -> str | None:
    """Why ``table``'s engine at the ``reported`` version is refused, or ``None``.
    An engine with no minimum is never refused for its version."""
    if table.minimum_version is None:
        return None
    minimum = version_numbers(table.minimum_version)
    if minimum is None:
        raise ValueError(f"the minimum version {table.minimum_version!r} is malformed")
    found = version_numbers(reported)
    if found is None:
        return (
            f"the engine reported no version Dromon can read, and {table.engine} was"
            f" tested from {table.minimum_version}"
        )
    if below(found, minimum):
        return (
            f"the engine reports version {reported}, below {table.minimum_version},"
            f" the lowest {table.engine} was tested against"
        )
    return None


CLAUDE_CODE: Final = EngineTable(
    engine="claude-code",
    display="Claude Code",
    minimum_version="2.1.310",
    tested_on="2026-10-26",
    resume=Known.YES,
    kept_session=Known.YES,
    own_tools=Known.YES,
    deny_before_run=Known.YES,
    cost_report=Known.YES,
    max_turns=Known.YES,
    credential_visible=Known.YES,
    sign_in_check=Known.UNKNOWN,
)
"""Claude Code, run live through the Agent SDK 0.2.174 on 2026-10-26."""

PI: Final = EngineTable(
    engine="pi",
    display="Pi",
    minimum_version=None,
    tested_on=None,
    resume=Known.YES,
    kept_session=Known.NO,
    own_tools=Known.UNKNOWN,
    deny_before_run=Known.UNKNOWN,
    cost_report=Known.YES,
    max_turns=Known.YES,
    credential_visible=Known.YES,
    sign_in_check=Known.UNKNOWN,
)
"""Pi, read from its documentation and source, never run live, as ``PI.md`` says.
Its turns are bounded by Dromon's count, and its credential is told by ``pi auth
check``. **Review of 2026-10-27, C1.** Whether Dromon's tools reach it through an
extension, whether every built-in tool is denied before it runs, and whether that
check works without a turn were stated as ``yes`` from the documentation alone. Each
is ``unknown`` until a live run shows it, so a Pi line's isolation is reported as
unknown. The other capabilities are left as the adapter's code states them, which
no live run has confirmed either."""

CURSOR: Final = EngineTable(
    engine="cursor-agent",
    display="Cursor",
    minimum_version=None,
    tested_on=None,
    resume=Known.NO,
    kept_session=Known.NO,
    own_tools=Known.YES,
    deny_before_run=Known.NO,
    cost_report=Known.NO,
    max_turns=Known.NO,
    credential_visible=Known.YES,
    sign_in_check=Known.UNKNOWN,
)
"""Cursor Agent, read from its documentation and installed code, never run live, as
``CURSOR.md`` says. Resume is refused, some tools are stopped only after they start,
it reports no cost, and a turn is bounded by time instead of by model calls."""

_PENDING: Final = (
    "to be filled once the branch that changes this adapter is merged, and unknown"
    " until then"
)


def _pending(engine: str, display: str) -> EngineTable:
    return EngineTable(
        engine=engine,
        display=display,
        minimum_version=None,
        tested_on=None,
        resume=Known.UNKNOWN,
        kept_session=Known.UNKNOWN,
        own_tools=Known.UNKNOWN,
        deny_before_run=Known.UNKNOWN,
        cost_report=Known.UNKNOWN,
        max_turns=Known.UNKNOWN,
        credential_visible=Known.UNKNOWN,
        sign_in_check=Known.UNKNOWN,
        pending=_PENDING,
    )


TABLES: Final = {
    table.engine: table
    for table in (
        CLAUDE_CODE,
        PI,
        CURSOR,
        _pending("grok-build", "Grok"),
        _pending("codex", "Codex"),
        _pending("antigravity", "Antigravity"),
    )
}
"""Every engine's table, by its engine name."""


def table_of(engine: str) -> EngineTable:
    """``engine``'s table. Raises ``KeyError`` for an engine Dromon does not have."""
    return TABLES[engine]


def kept_session_refusal(table: EngineTable) -> str | None:
    """Why a line on ``table``'s engine cannot keep its engine between turns, from
    the table, or ``None`` when it can."""
    if table.kept_session is Known.YES:
        return None
    return f"{table.display} lines do not keep their engine between turns"


def isolation_of(table: EngineTable) -> str:
    """``full`` where every built-in tool is denied before it runs, ``partial``
    where some is stopped only after it starts, and ``unknown`` otherwise."""
    if table.deny_before_run is Known.YES:
        return "full"
    if table.deny_before_run is Known.NO:
        return "partial"
    return "unknown"


def report_lines(table: EngineTable) -> list[str]:
    """What the probe reports of ``table``, derived from it alone."""
    if table.minimum_version is not None:
        tested = f"tested from {table.minimum_version} on {table.tested_on}"
    elif table.pending is None:
        # Review of 2026-10-27, C2. Said plainly, so no operator reads the absence
        # of a minimum as a check that passed.
        tested = (
            "never run live, so no minimum version is enforced and any version the"
            " engine reports runs"
        )
    else:
        tested = "never run live, so no minimum version"
    lines = [f"table {table.engine}, {tested}"]
    lines.append(
        "table "
        + ", ".join(f"{name} {value}" for name, value in table.capabilities().items())
    )
    if table.pending is not None:
        lines.append(f"table {table.pending}")
    return lines
