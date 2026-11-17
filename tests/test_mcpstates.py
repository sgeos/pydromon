"""Round 17, item 7. What Dromon knows of each MCP server, from the configuration
and the engine's own report, with untrusted text read in strict forms only."""

from typing import Any

import pytest

from dromon import mcpstates
from dromon.engine import _capabilities  # pyright: ignore[reportPrivateUsage]
from dromon.mcpstates import EngineReport, server_states, servers_of, summary


def test_the_engine_s_servers_are_read_in_their_strict_forms() -> None:
    data: dict[str, Any] = {
        "mcp_servers": [
            {"name": "files", "status": "connected"},
            {"name": "auth", "status": "needs-auth"},
            {"name": "odd", "status": "Weird\x1b[0m"},
            {"name": "bad name\n", "status": "connected"},
            {"name": 3, "status": "connected"},
            "not an object",
            {"status": "connected"},
        ]
    }
    assert servers_of(data) == (
        ("files", "connected"),
        ("auth", "needs-auth"),
        ("odd", "unknown"),
    )


@pytest.mark.parametrize("found", [None, "files", {"files": "connected"}, 3])
def test_a_report_without_a_list_is_unknown_not_empty(found: object) -> None:
    data: dict[str, object] = {} if found is None else {"mcp_servers": found}
    assert servers_of(data) is None


def test_the_read_is_bounded() -> None:
    many = [{"name": f"s{n}", "status": "connected"} for n in range(500)]
    found = servers_of({"mcp_servers": many})
    assert found is not None and len(found) == mcpstates.MAX_SERVERS


def test_without_a_report_only_the_configuration_is_known() -> None:
    assert server_states({"b", "a"}, None) == [
        {
            "name": name,
            "configured": "yes",
            "connected": "unknown",
            "engine_status": "unknown",
            "callable": "unknown",
        }
        for name in ("a", "b")
    ]


def test_a_configured_server_the_engine_left_out_is_not_connected() -> None:
    (state,) = server_states({"files"}, EngineReport((), ()))
    assert (state["connected"], state["engine_status"], state["callable"]) == (
        "no",
        "not reported by the engine",
        "no",
    )


def test_servers_and_tools_are_judged_apart() -> None:
    report = EngineReport(None, ("mcp__files__read",))
    (state,) = server_states({"files"}, report)
    assert (state["connected"], state["callable"]) == ("unknown", "yes")
    report = EngineReport((("files", "connected"),), None)
    (state,) = server_states({"files"}, report)
    assert (state["connected"], state["callable"]) == ("yes", "unknown")


def test_a_tool_of_a_server_with_a_longer_name_is_not_counted() -> None:
    report = EngineReport((("files", "connected"),), ("mcp__files2__read",))
    (state,) = server_states({"files"}, report)
    assert state["callable"] == "no"


def test_a_tool_belongs_to_the_longest_server_name_it_begins_with() -> None:
    """Review of 0db077c, finding 5, the audit's case. Both ``a`` and ``a__b`` read
    as callable from the one tool ``mcp__a__b__tool``."""
    servers = (("a", "connected"), ("a__b", "connected"))
    states = {
        s["name"]: s["callable"]
        for s in server_states(
            {"a", "a__b"}, EngineReport(servers, ("mcp__a__b__tool",))
        )
    }
    assert states == {"a": "no", "a__b": "yes"}
    both = EngineReport(servers, ("mcp__a__b__tool", "mcp__a__read"))
    assert {s["callable"] for s in server_states({"a", "a__b"}, both)} == {"yes"}


@pytest.mark.parametrize(
    ("tool", "owner"),
    [
        ("mcp__a__b__tool", "a__b"),
        ("mcp__a__read", "a"),
        ("mcp__a__b", "a"),
        ("mcp__c__read", None),
        ("a__b__tool", None),
    ],
)
def test_the_owner_of_a_tool(tool: str, owner: str | None) -> None:
    assert mcpstates.owner_of(tool, ["a", "a__b"]) == owner


def test_servers_from_the_account_and_dromon_s_own_are_named_so() -> None:
    report = EngineReport((("dromon", "connected"), ("drive", "pending")), ())
    states = {s["name"]: s for s in server_states(set(), report)}
    assert states["dromon"]["configured"] == "dromon"
    assert (states["drive"]["configured"], states["drive"]["connected"]) == (
        "account",
        "no",
    )


def test_the_summary_reads_each_state() -> None:
    report = EngineReport((("a", "connected"), ("b", "failed")), ("mcp__a__x",))
    assert summary(server_states({"a", "b", "c"}, report)) == (
        "a configured, connected, callable;"
        " b configured, not connected, failed, no tool offered;"
        " c configured, not connected, not reported by the engine, no tool offered"
    )
    assert summary(server_states({"a"}, None)) == (
        "a configured, connection unknown until a turn runs, tools unknown"
    )
    assert summary([]) == "none"


def test_the_engine_s_start_report_carries_the_servers() -> None:
    data: dict[str, object] = {
        "tools": ["Read", "mcp__files__read"],
        "mcp_servers": [{"name": "files", "status": "connected"}],
    }
    found = _capabilities(data)
    assert found.mcp_servers == (("files", "connected"),)
    assert _capabilities({"tools": []}).mcp_servers is None
