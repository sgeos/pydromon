"""What Dromon knows of each Model Context Protocol server of a line.

**Round 17, item 7, from another project's reports, whose details are untrusted.**
A line's servers were shown by name only, so a server that was configured but had
failed to connect, or had connected but offered no tool the model could call, read
the same as one in use. Three states are told apart, each from what Dromon already
holds, with no new network call and no new process.

* **configured** comes from the line's configuration, which is always known. A
  server the engine reports but the configuration does not name came from the
  account, under ``inherit_account_mcp``, or is Dromon's own, and is shown so.
* **connected** comes from the engine's own report at the start of the line's latest
  turn, which Claude Code gives as each server's ``status``. It is ``unknown`` until
  the line runs a turn under this daemon, and for an engine that reports no status.
* **callable** comes from the tools the same report offers the model. A server is
  callable when at least one tool named ``mcp__SERVER__...`` is offered, counting
  each tool for the longest server name it begins with, as ``owner_of`` gives. It is
  ``unknown`` where the report gave no tool list.

**Residuals.** Both reports are the latest turn's, kept in memory, so they are
``unknown`` after a daemon restart until the line runs again, and they say nothing of
a server that failed after the turn began. A tool offered is not a tool that works.
The engine's text is untrusted, so names and statuses are read only in a strict form,
and anything else is left out and counted.
"""

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, cast

UNKNOWN: Final = "unknown"

SERVER_NAME: Final = re.compile(r"[A-Za-z0-9_.-]{1,128}")
"""The form of a server name read from an engine's report."""

STATUS: Final = re.compile(r"[a-z][a-z_-]{0,31}")
"""The form of a status read from an engine's report, such as ``connected``,
``failed``, ``pending`` or ``needs-auth``."""

CONNECTED: Final = "connected"
"""The status Claude Code reports for a server it connected to."""

MAX_SERVERS: Final = 64
"""The most servers read from one report, so the read and the view are bounded."""

DROMON_SERVER: Final = "dromon"
"""The name of Dromon's own in-process server, as ``dromon.engine`` serves it."""


@dataclass(frozen=True, slots=True)
class EngineReport:
    """What the engine said of its servers and tools at a turn's start. ``None``
    where its report held no such list, which is unknown and not empty."""

    servers: tuple[tuple[str, str], ...] | None
    tools: tuple[str, ...] | None


def servers_of(data: Mapping[str, object]) -> tuple[tuple[str, str], ...] | None:
    """Each server's name and status from an engine's start report, in the strict
    forms only, or ``None`` when the report holds no list of them."""
    found = data.get("mcp_servers")
    if not isinstance(found, list):
        return None
    servers: list[tuple[str, str]] = []
    for item in cast(list[object], found)[:MAX_SERVERS]:
        if not isinstance(item, dict):
            continue
        entry = cast(dict[str, object], item)
        name, status = entry.get("name"), entry.get("status")
        if not isinstance(name, str) or SERVER_NAME.fullmatch(name) is None:
            continue
        servers.append(
            (
                name,
                status
                if isinstance(status, str) and STATUS.fullmatch(status)
                else UNKNOWN,
            )
        )
    return tuple(servers)


def server_states(
    configured: Iterable[str], report: EngineReport | None
) -> list[dict[str, str]]:
    """One entry for each configured server, then each other server the engine
    reported, with ``configured``, ``connected`` and ``callable`` each ``yes``,
    ``no`` or ``unknown``, and ``engine_status``, the engine's own word."""
    names = sorted(set(configured))
    reported = dict(report.servers) if report and report.servers is not None else None
    tools = report.tools if report is not None else None
    others = sorted(set(reported or ()) - set(names))
    shown = [*names, *others][:MAX_SERVERS]
    owners = (
        set[str]()
        if tools is None
        else {owner for tool in tools if (owner := owner_of(tool, shown)) is not None}
    )
    states: list[dict[str, str]] = []
    for name in shown:
        status = UNKNOWN if reported is None else reported.get(name)
        states.append(
            {
                "name": name,
                "configured": (
                    "yes"
                    if name in names
                    else "dromon"
                    if name == DROMON_SERVER
                    else "account"
                ),
                "connected": _connected(status),
                "engine_status": (
                    "not reported by the engine" if status is None else status
                ),
                "callable": (
                    UNKNOWN if tools is None else "yes" if name in owners else "no"
                ),
            }
        )
    return states


def owner_of(tool: str, names: Iterable[str]) -> str | None:
    """The server among ``names`` whose prefix ``mcp__NAME__`` the tool's name
    begins with, the longest where several do, or ``None``. **Review of 0db077c,
    finding 5.** Server names may hold ``_``, so with servers ``a`` and ``a__b`` the
    tool ``mcp__a__b__tool`` begins with both prefixes, and it showed both servers
    callable. It is taken as the longer name's tool, which is an inference, since
    the name alone cannot say that ``a`` has no tool ``b__tool``. Only the servers
    shown are candidates, so a tool of a server neither configured nor reported is
    taken as a shorter shown name's. **Hypothesis, not tested against the
    engine.** If Claude Code rewrites other characters of a server's name in its tool
    names, such a server reads as offering no tool."""
    matching = [name for name in names if tool.startswith(f"mcp__{name}__")]
    return max(matching, key=len, default=None)


def _connected(status: str | None) -> str:
    """``yes`` for the engine's connected status, ``unknown`` where no report was
    read or its status had another form, and ``no`` otherwise, including a
    configured server that a report left out, which the engine did not load."""
    if status == UNKNOWN:
        return UNKNOWN
    return "yes" if status == CONNECTED else "no"


def summary(states: list[dict[str, str]]) -> str:
    """The states as one line of text, such as ``files configured, connected,
    callable``, or ``none``."""
    if not states:
        return "none"
    parts: list[str] = []
    for state in states:
        configured = {
            "yes": "configured",
            "dromon": "Dromon's own",
            "account": "from the account",
        }[state["configured"]]
        connected = {
            "yes": "connected",
            "no": f"not connected, {state['engine_status']}",
            UNKNOWN: "connection unknown until a turn runs",
        }[state["connected"]]
        callable_ = {
            "yes": "callable",
            "no": "no tool offered",
            UNKNOWN: "tools unknown",
        }[state["callable"]]
        parts.append(f"{state['name']} {configured}, {connected}, {callable_}")
    return "; ".join(parts)
