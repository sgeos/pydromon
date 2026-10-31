"""The own tools of a line whose allowlist names only one MCP server's tools.

Round 9, item 3, which was not confirmed as a defect. This pins the account that a
line so configured is given ``ask_operator`` and allowed to call it."""

import asyncio
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultAllow, ToolPermissionContext

from dromon.driver import Driver, Strategy
from dromon.engine import ClaudeCodeLine, permission_gate
from dromon.eventlog import EventLog
from dromon.lineconfig import config_from_document
from dromon.scheduler import ASK_TOOL_NAME, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"


class Unused:
    """An engine the test never runs."""

    def __init__(self, spec: LineSpec) -> None:
        self.session_id = spec.session_id

    async def run_turn(self, prompt: str) -> Any:  # noqa: ANN401
        raise AssertionError(prompt)


def test_an_allowlisted_line_with_one_servers_tools_is_given_ask_operator(
    tmp_path: Path,
) -> None:
    """Not confirmed as a defect. The line's own tools always carry ``ask_operator``,
    its engine serves every one of them, its init check accepts it, and its gate
    allows it, under the configuration the defect was reported for. This pins that
    account, so a change that drops it fails here."""
    document: dict[str, object] = {
        "tool_policy": "allowlist",
        "allowed_tools": ["mcp__game__*"],
        "keep_session": True,
        "allow_compact": True,
        "recipients": ["@operator"],
        "mcp_servers": {"game": {"type": "stdio", "command": "/bin/true"}},
    }
    config = config_from_document(document)
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(log, lambda spec, tools: Unused(spec))
        scheduler.create_line("player", HAIKU, Strategy.SUBSCRIPTION, config)
        tools = scheduler.own_tools("player")
    assert ASK_TOOL_NAME in {tool.name for tool in tools}
    target = ClaudeCodeLine(
        Driver.for_model(HAIKU, Strategy.SUBSCRIPTION),
        tmp_path,
        config=config,
        mcp_tools=tools,
        environment={},
    )
    name = f"mcp__dromon__{ASK_TOOL_NAME}"
    assert name in target.own_tools
    options = target.options()
    assert isinstance(options.mcp_servers, dict)
    assert {"dromon", "game"} <= set(options.mcp_servers)
    assert target._offered_as_configured(name)  # pyright: ignore[reportPrivateUsage]
    gate = permission_gate(config, target.own_tools)
    allowed = asyncio.run(gate(name, {}, ToolPermissionContext()))
    assert isinstance(allowed, PermissionResultAllow)
