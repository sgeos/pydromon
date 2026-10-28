"""What each engine can do, as plain data, one table per engine.

**Rows 7 and 10 of the T3 Code review, 2026-10-27.** Parity between engines was spread
across frozensets, ``unsupported()`` functions and prose. Each engine now has one
``EngineTable``, and the refusals and the probe's report that depend on a capability
are derived from it. A port carries the tables across as data.

Unknown is not empty. Each capability is ``YES``, ``NO`` or ``UNKNOWN``, and a minimum
version is ``None`` when Dromon was never run against a live engine of that kind, so no
version can be named as tested, or when its adapter reads no version. An unknown
capability is refused wherever Dromon needs
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
this module was written, and their tables were filled on 2026-10-27, after it was
merged, under the rule of review finding C1. ``own_tools``, ``deny_before_run`` and
``sign_in_check`` are ``yes`` only where a live run showed them. The other capabilities
are as the adapter's code states them, which for an engine never run live no run has
confirmed either, and ``unknown`` where the code itself names its reading an inference.
Each table's docstring says which is which.
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
    "read_only_workdir",
    "cost_report",
    "max_turns",
    "credential_visible",
    "sign_in_check",
    "mcp_http_sse_headers",
)
"""The capabilities each table states, in the order a report lists them."""


@dataclass(frozen=True, slots=True)
class EngineTable:
    """One engine's capabilities and the version it was tested against."""

    engine: str
    display: str
    """The engine's name in prose, as a refusal names it."""
    minimum_version: str | None
    """The lowest version Dromon was run against live and its adapter enforces, or
    ``None`` when it never was run live or its adapter reads no version."""
    tested_on: str | None
    """The calendar date of the live run, ``YYYY-MM-DD``, or ``None`` when there was
    none. Set whenever ``minimum_version`` is."""
    resume: Known
    """Whether a line's engine session can be continued in a later turn."""
    kept_session: Known
    """Whether a connection can be kept between turns."""
    own_tools: Known
    """Whether Dromon's own tools reach the model, through MCP or an extension."""
    deny_before_run: Known
    """Whether every built-in tool is denied before it runs, not stopped after."""
    read_only_workdir: Known
    """Whether the adapter can confine a read-only workdir with a scratch exception."""
    cost_report: Known
    """Whether the engine reports a cost for each turn."""
    max_turns: Known
    """Whether a bound on model calls in a turn is enforced, by the engine or by
    Dromon's own count."""
    credential_visible: Known
    """Whether Dromon can tell from the engine which credential a turn uses."""
    sign_in_check: Known
    """Whether the engine offers a way to check its sign-in without a turn."""
    mcp_http_sse_headers: Known
    """Whether Dromon's adapter accepts HTTP or SSE MCP server headers."""

    def __post_init__(self) -> None:
        if self.minimum_version is not None and self.tested_on is None:
            raise ValueError(f"{self.engine} names a minimum version without its run")

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
    read_only_workdir=Known.YES,
    cost_report=Known.YES,
    max_turns=Known.YES,
    credential_visible=Known.YES,
    sign_in_check=Known.UNKNOWN,
    mcp_http_sse_headers=Known.YES,
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
    read_only_workdir=Known.NO,
    cost_report=Known.YES,
    max_turns=Known.YES,
    credential_visible=Known.YES,
    sign_in_check=Known.UNKNOWN,
    mcp_http_sse_headers=Known.NO,
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
    read_only_workdir=Known.NO,
    cost_report=Known.NO,
    max_turns=Known.NO,
    credential_visible=Known.YES,
    sign_in_check=Known.UNKNOWN,
    mcp_http_sse_headers=Known.NO,
)
"""Cursor Agent, read from its documentation and installed code, never run live, as
``CURSOR.md`` says. Resume is refused, some tools are stopped only after they start,
it reports no cost, and a turn is bounded by time instead of by model calls."""

CODEX: Final = EngineTable(
    engine="codex",
    display="Codex",
    minimum_version=None,
    tested_on="2026-10-25",
    resume=Known.YES,
    kept_session=Known.YES,
    own_tools=Known.YES,
    deny_before_run=Known.YES,
    read_only_workdir=Known.YES,
    cost_report=Known.NO,
    max_turns=Known.NO,
    credential_visible=Known.YES,
    sign_in_check=Known.UNKNOWN,
    mcp_http_sse_headers=Known.NO,
)
"""Codex, run live with Codex 0.164.0 and GPT-6-Luna on the ChatGPT plan on
2026-10-25, as ``CODEX.md`` records. A resumed thread and Dromon's own tools, as
dynamic tools, were observed then, and the account type is read from ``account/read``.

**Kept sessions, observed live.** At the operator's decision of 2026-10-27 after the
review's T1, the evidence is named here. The luna line has run with ``keep_session`` on
throughout since round 3, decided on 2026-10-26, and the ``codexkept`` line in the
operator's scratch project ran the live probes of round 7, of the hold and of
compaction, on 2026-10-26, each on a kept app-server and thread.

**Denial before running, observed for some tool classes only.** At the operator's
decision of 2026-10-27 after the review's T2, yes rests on these observations of
2026-10-25 with Codex 0.164.0 on macOS, as ``CODEX.md`` records. Every tool the line
was not given is switched off before the thread starts, which ``config/read`` and the
MCP server listing showed. Under the permission profile, edits to ``.dromon`` and
``.git`` were rejected before they were written, a shell read of ``.dromon`` failed,
and writing the home directory, reaching the network, and renaming, linking or
changing the mode of ``.dromon`` failed through ``codex sandbox``. Every other tool
class, such as a read outside the workspace or a web search, was not observed, and its
denial rests on the permission profile's documented behaviour. Codex emits no item for
a call it refuses, so Dromon cannot count such a call. An item of a kind no one
expected still interrupts the turn, as a second guard.
Codex reports no cost and has no turn limit, so the driver's ``max_turns`` is not
enforced. No check of the sign-in without a turn is known.

**No minimum version.** The adapter reads no version from the app-server, so none can
be enforced, although 0.164.0 is the version run live. A probe recording shows where
the app-server reports its version, and the minimum follows from it."""

GROK: Final = EngineTable(
    engine="grok-build",
    display="Grok",
    minimum_version=None,
    tested_on=None,
    resume=Known.YES,
    kept_session=Known.NO,
    own_tools=Known.UNKNOWN,
    deny_before_run=Known.UNKNOWN,
    read_only_workdir=Known.UNKNOWN,
    cost_report=Known.NO,
    max_turns=Known.NO,
    credential_visible=Known.UNKNOWN,
    sign_in_check=Known.UNKNOWN,
    mcp_http_sse_headers=Known.NO,
)
"""Grok Build, never run for a turn, as ``GROK.md`` records. An ``initialize`` answer
from Grok Build 1.0.70, which runs no model, was observed, and it advertised
``loadSession``, which the adapter uses to resume. Kept sessions are refused, and Grok
reports no cost and the driver's ``max_turns`` is not enforced. Whether Dromon's tools
reach Grok under the ``toolName`` the adapter requires is the first step of
``GROK.md``'s verify-first list, so ``own_tools`` is unknown, as for Pi. Whether every
built-in tool asks before it runs in the ``default`` mode is not documented, so
``deny_before_run`` is unknown. Grok names its cached sign-in, and that it is a
subscription is an inference, so ``credential_visible`` is unknown. The adapter
selects the ``dromon-reader`` profile for a read-only workdir, but no live run has shown
that profile deny a write to the worktree, so ``read_only_workdir`` is unknown. **Review
of read-only seats, finding 3.** The adapter refuses a read-only working directory while
this is not ``YES``, so a probe that confirms the profile lifts the refusal here.
**Second round, findings 1 and 2.**"""

ANTIGRAVITY: Final = EngineTable(
    engine="antigravity",
    display="Antigravity",
    minimum_version=None,
    tested_on=None,
    resume=Known.YES,
    kept_session=Known.NO,
    own_tools=Known.UNKNOWN,
    deny_before_run=Known.NO,
    read_only_workdir=Known.NO,
    cost_report=Known.UNKNOWN,
    max_turns=Known.NO,
    credential_visible=Known.NO,
    sign_in_check=Known.UNKNOWN,
    mcp_http_sse_headers=Known.NO,
)
"""Antigravity's ACP runtime, never run live and not installed, as
``ANTIGRAVITY.md`` records. The adapter resumes a session the runtime offers to
resume, and refuses kept sessions. A built-in tool that the runtime runs without asking
is stopped only after it starts, which is why a daemon refuses Antigravity lines.
Whether Dromon's tools reach it is unknown, as for Pi. A cost from ``usage_update`` is
counted but was never observed, so ``cost_report`` is unknown. No bound on model calls
is enforced. The runtime does not report which credential it uses, so the credential
is ``unverified``. The adapter's own floor, ``antigravity.MINIMUM_VERSION``, was taken
from a second implementation, not from a live run, so it is not this table's
minimum."""


TABLES: Final = {
    table.engine: table for table in (CLAUDE_CODE, PI, CURSOR, GROK, CODEX, ANTIGRAVITY)
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
    # Review of 2026-10-27, C2. The absence of a minimum is said plainly, so no
    # operator reads it as a check that passed.
    unenforced = (
        "no minimum version is enforced and any version the engine reports runs"
    )
    if table.minimum_version is not None:
        tested = f"tested from {table.minimum_version} on {table.tested_on}"
    elif table.tested_on is not None:
        tested = (
            f"run live on {table.tested_on}, but no version is read, so {unenforced}"
        )
    else:
        tested = f"never run live, so {unenforced}"
    return [
        f"table {table.engine}, {tested}",
        "table "
        + ", ".join(f"{name} {value}" for name, value in table.capabilities().items()),
    ]
