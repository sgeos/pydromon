"""Every path that offers a line or an agent ``send_message`` accepts its
``request_key``. **Task of 2026-10-29, round 5.** The review line reported that
``send_message`` refused the key in two reports while ``wake_after`` accepted it.
Each path below lists Dromon's own tool schema and passes the arguments through
unchanged to the one handler, so a retried call on any of them sends once.

The paths are the Claude Agent SDK's in-process server for Claude Code lines, Codex's
dynamic tools, the line socket that the Model Context Protocol bridge serves to
Codex, Grok, Pi, Cursor and Antigravity lines, the bridge itself, and the agent
socket for external agents. No test here opens a socket."""

import asyncio
import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import claude_agent_sdk
import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.agents import Gateway
from dromon.codex import dynamic_tool
from dromon.daemon import Daemon
from dromon.eventlog import EventLog
from dromon.mcp import Bridge
from dromon.model import (
    AgentAccess,
    AgentAddress,
    AgentRegistered,
    LineAddress,
    LineCreated,
    OperatorRegistered,
)
from dromon.paths import project_paths
from dromon.protocol import Operation, Response
from dromon.requestkeys import REPEATED, REQUEST_KEY
from dromon.scheduler import (
    ASK_TOOL_NAME,
    SEND_TOOL_NAME,
    WAKE_TOOL_NAME,
    LineEngine,
    LineSpec,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
KEYED = (SEND_TOOL_NAME, ASK_TOOL_NAME, WAKE_TOOL_NAME)


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError("no engine may start in this test")


@pytest.fixture
def daemon(tmp_path: Path, short_runtime: Path) -> Iterator[Daemon]:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700)
    with EventLog(paths.event_log) as log:
        log.append(LineCreated("alpha", HAIKU, "subscription"))
        log.append(OperatorRegistered("ana"))
        log.append(AgentRegistered("probe", AgentAccess.MESSAGE, "0" * 64))
        yield Daemon(paths, log, "t", Scheduler(log, no_engine, hold_wakes=True))


def properties(schema: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], cast(Mapping[str, object], schema)["properties"])


def sent_by(daemon: Daemon, sender: LineAddress | AgentAddress) -> list[str]:
    return [
        message_id
        for message_id, message in daemon.log.state.messages.items()
        if message.sender == sender
    ]


def call(tool: SdkMcpTool[Any], arguments: dict[str, Any]) -> dict[str, Any]:
    async def handle() -> dict[str, Any]:
        return await tool.handler(arguments)

    return asyncio.run(handle())


def test_the_in_process_server_lists_the_key_for_claude_code(daemon: Daemon) -> None:
    """The Agent SDK sends a dictionary schema unchanged, which its own helper
    shows. **Assumption.** The helper is private to the SDK, so an upgrade that
    renames it fails this test rather than hiding a change."""
    build = claude_agent_sdk._build_input_schema  # pyright: ignore[reportPrivateUsage]
    for tool in daemon.line_tools("alpha"):
        if tool.name in KEYED:
            assert REQUEST_KEY in properties(build(tool))


def test_codex_dynamic_tools_list_the_key_and_pass_it_through(daemon: Daemon) -> None:
    tools = {tool.name: tool for tool in daemon.line_tools("alpha")}
    for name in KEYED:
        assert REQUEST_KEY in properties(dynamic_tool(tools[name])["inputSchema"])
    arguments = {"to": "@ana", "body": "hi", REQUEST_KEY: "k"}
    first = call(tools[SEND_TOOL_NAME], arguments)
    again = call(tools[SEND_TOOL_NAME], arguments)
    assert not again.get("is_error")
    assert again["content"][0]["text"] == first["content"][0]["text"] + REPEATED
    assert len(sent_by(daemon, LineAddress("alpha"))) == 1


def test_the_line_socket_lists_the_key_and_a_retried_send_sends_once(
    daemon: Daemon,
) -> None:
    """The path of every engine that reaches its line's tools through the bridge."""
    token = daemon.line_tokens.issue("alpha")

    def ask(op: str, args: dict[str, object]) -> Response:
        line = (json.dumps({"id": "r", "op": op, "v": 1, "args": args}) + "\n").encode()
        return asyncio.run(daemon.respond_line(line))

    listed = ask("agent_tools", {"line": "alpha", "token": token})
    assert listed.result is not None
    schemas = {
        tool["name"]: tool["inputSchema"]
        for tool in cast(list[dict[str, Any]], listed.result["tools"])
    }
    for name in KEYED:
        assert REQUEST_KEY in properties(schemas[name])
    texts: list[str] = []
    for _ in range(2):
        sent = ask(
            "agent_call",
            {
                "line": "alpha",
                "token": token,
                "tool": SEND_TOOL_NAME,
                "arguments": {"to": "@ana", "body": "hi", REQUEST_KEY: "k"},
            },
        )
        assert sent.ok and sent.result is not None and not sent.result["is_error"]
        texts.append(cast(list[dict[str, str]], sent.result["content"])[0]["text"])
    assert texts[1] == texts[0] + REPEATED
    assert len(sent_by(daemon, LineAddress("alpha"))) == 1


def test_the_bridge_passes_the_key_through_unchanged(
    short_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = Bridge(short_runtime / "lines.sock", "alpha", "tok", party="line")
    forwarded: list[tuple[Operation, Mapping[str, object]]] = []

    def daemon_call(operation: Operation, extra: Mapping[str, object]) -> object:
        forwarded.append((operation, extra))
        return {"content": [{"type": "text", "text": "sent as m-1"}]}

    monkeypatch.setattr(bridge, "_daemon", daemon_call)
    arguments = {"to": "@ana", "body": "hi", REQUEST_KEY: "k"}
    answer = bridge.handle(
        {
            "id": 1,
            "method": "tools/call",
            "params": {"name": SEND_TOOL_NAME, "arguments": arguments},
        }
    )
    assert answer is not None and "error" not in answer
    assert forwarded == [
        (Operation.AGENT_CALL, {"tool": SEND_TOOL_NAME, "arguments": arguments})
    ]


def test_the_agent_socket_lists_the_key_and_a_retried_send_sends_once(
    daemon: Daemon,
) -> None:
    gateway = Gateway(daemon.agents, daemon)
    schemas = {
        str(tool["name"]): tool["inputSchema"]
        for tool in gateway.tools(AgentAccess.MESSAGE)
    }
    assert REQUEST_KEY in properties(schemas[SEND_TOOL_NAME])
    arguments = {"to": "alpha", "body": "hi", REQUEST_KEY: "k"}
    results = [
        asyncio.run(
            gateway.call("probe", AgentAccess.MESSAGE, SEND_TOOL_NAME, arguments)
        )
        for _ in range(2)
    ]
    texts = [
        cast(list[dict[str, str]], result["content"])[0]["text"] for result in results
    ]
    assert not any(result["is_error"] for result in results)
    assert texts[1] == texts[0] + REPEATED
    assert len(sent_by(daemon, AgentAddress("probe"))) == 1
