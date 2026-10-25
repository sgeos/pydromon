"""Per-line tool configuration: built-in tools, MCP servers and the tool policy.

A line's configuration names the built-in Claude Code tools it gets, the MCP servers it
connects to, and which tool calls are allowed. ``allowlist`` allows the given built-in
tools, Dromon's own tools, and every name or ``mcp__server__*`` pattern listed in
``allowed_tools``, and denies everything else. ``allow_all`` allows every tool the
engine offers. ``inherit_account_mcp`` lets Claude Code also load the MCP servers of
the operator's user settings and claude.ai account, which are otherwise excluded.

The configuration is stored in the event log as canonical JSON, so it survives a
restart and every change is on record. ``docs/protocol/LINE_CONFIG.md`` is normative.

Security. The configuration is operator authority and arrives only through the client.
An MCP server's ``env`` and ``headers`` may hold credentials, and they are stored in the
event log, whose file is readable by its owner only. ``allow_all`` and
``inherit_account_mcp`` hand a line every tool they bring, such as a claude.ai
account's mail and file connectors, and are meant for lines the operator trusts.
"""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, NoReturn, cast

from dromon.model import ID_PATTERN

CONFIG_VERSION: Final = 1

RESERVED_SERVERS: Final = frozenset({"dromon"})
"""Server names Dromon uses for its own tools."""

BUILTIN_PATTERN: Final = re.compile(r"[A-Z][A-Za-z0-9_]{0,63}")
TOOL_PATTERN: Final = re.compile(
    r"mcp__[a-z0-9][a-z0-9._-]{0,63}__(\*|[A-Za-z0-9_.-]{1,128})"
)
MAX_ENTRIES: Final = 256
MAX_TEXT: Final = 4096


class ConfigError(ValueError):
    """A line configuration is malformed or unsafe to accept."""


class ToolPolicy(StrEnum):
    ALLOWLIST = "allowlist"
    ALLOW_ALL = "allow_all"


@dataclass(frozen=True, slots=True)
class McpServer:
    """One MCP server, in the shape Claude Code's configuration uses."""

    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    url: str = ""
    headers: tuple[tuple[str, str], ...] = ()

    def sdk_config(self) -> dict[str, object]:
        """The server as the Agent SDK's ``mcp_servers`` entry."""
        if self.transport == "stdio":
            config: dict[str, object] = {"type": "stdio", "command": self.command}
            if self.args:
                config["args"] = list(self.args)
            if self.env:
                config["env"] = dict(self.env)
            return config
        config = {"type": self.transport, "url": self.url}
        if self.headers:
            config["headers"] = dict(self.headers)
        return config


@dataclass(frozen=True, slots=True)
class LineConfig:
    builtin_tools: tuple[str, ...] = ()
    mcp_servers: tuple[tuple[str, McpServer], ...] = ()
    tool_policy: ToolPolicy = ToolPolicy.ALLOWLIST
    allowed_tools: tuple[str, ...] = field(default=())
    inherit_account_mcp: bool = False

    def allows(self, tool_name: str, own_tools: frozenset[str]) -> bool:
        """Whether the policy allows ``tool_name``. Dromon's own tools always pass."""
        if tool_name in own_tools or tool_name in self.builtin_tools:
            return True
        if self.tool_policy is ToolPolicy.ALLOW_ALL:
            return True
        for pattern in self.allowed_tools:
            if pattern.endswith("__*"):
                if tool_name.startswith(pattern[:-1]):
                    return True
            elif tool_name == pattern:
                return True
        return False

    def server_names(self) -> frozenset[str]:
        return frozenset(name for name, _ in self.mcp_servers)


DEFAULT_CONFIG: Final = LineConfig()


def encode_config(config: LineConfig) -> str:
    """The canonical JSON of ``config``, as stored in the event log."""
    document = {
        "v": CONFIG_VERSION,
        "builtin_tools": list(config.builtin_tools),
        "mcp_servers": {
            name: server.sdk_config() for name, server in config.mcp_servers
        },
        "tool_policy": config.tool_policy.value,
        "allowed_tools": list(config.allowed_tools),
        "inherit_account_mcp": config.inherit_account_mcp,
    }
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _fail(message: str) -> NoReturn:
    raise ConfigError(message)


def _reject_number(text: str) -> NoReturn:
    _fail(f"unexpected number {text!r}")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            _fail(f"duplicate key {key!r}")
        document[key] = value
    return document


def _object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{what} is not an object")
    return cast(dict[str, object], value)


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        _fail(f"{what} must be text of 1 to {MAX_TEXT} characters")
    return value


def _texts(value: object, what: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(f"{what} is not an array")
    items = cast(list[object], value)
    if len(items) > MAX_ENTRIES:
        _fail(f"{what} has more than {MAX_ENTRIES} entries")
    return tuple(_text(item, what) for item in items)


def _pairs(value: object, what: str) -> tuple[tuple[str, str], ...]:
    document = _object(value, what)
    if len(document) > MAX_ENTRIES:
        _fail(f"{what} has more than {MAX_ENTRIES} entries")
    return tuple(sorted((_text(k, what), _text(v, what)) for k, v in document.items()))


def _server(name: str, value: object) -> McpServer:
    document = _object(value, f"server {name}")
    transport = document.get("type", "stdio")
    if transport == "stdio":
        allowed = {"type", "command", "args", "env"}
        if not set(document) <= allowed or "command" not in document:
            _fail(f"server {name} must have a command and only {sorted(allowed)}")
        return McpServer(
            "stdio",
            command=_text(document["command"], f"server {name} command"),
            args=_texts(document.get("args", []), f"server {name} args"),
            env=_pairs(document.get("env", {}), f"server {name} env"),
        )
    if transport in ("http", "sse"):
        allowed = {"type", "url", "headers"}
        if not set(document) <= allowed or "url" not in document:
            _fail(f"server {name} must have a url and only {sorted(allowed)}")
        url = _text(document["url"], f"server {name} url")
        if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            _fail(f"server {name} url must use https, or http on the local host")
        return McpServer(
            str(transport),
            url=url,
            headers=_pairs(document.get("headers", {}), f"server {name} headers"),
        )
    _fail(f"server {name} has an unknown type {transport!r}")


def parse_servers(value: object) -> tuple[tuple[str, McpServer], ...]:
    """Servers from a mapping of name to server, or a ``{"mcpServers": ...}`` file."""
    document = _object(value, "mcp_servers")
    if set(document) == {"mcpServers"}:
        document = _object(document["mcpServers"], "mcpServers")
    if len(document) > MAX_ENTRIES:
        _fail(f"mcp_servers has more than {MAX_ENTRIES} servers")
    servers: list[tuple[str, McpServer]] = []
    for name, server in sorted(document.items()):
        if ID_PATTERN.fullmatch(name) is None:
            _fail(f"server name {name!r} is not a valid identifier")
        if name in RESERVED_SERVERS:
            _fail(f"server name {name!r} is reserved for Dromon")
        servers.append((name, _server(name, server)))
    return tuple(servers)


def config_from_document(document: Mapping[str, object]) -> LineConfig:
    """A validated configuration from a decoded JSON object, without its version."""
    allowed = {
        "builtin_tools",
        "mcp_servers",
        "tool_policy",
        "allowed_tools",
        "inherit_account_mcp",
    }
    unknown = set(document) - allowed
    if unknown:
        _fail(f"unknown configuration keys {sorted(unknown)}")
    builtin = _texts(document.get("builtin_tools", []), "builtin_tools")
    for name in builtin:
        if BUILTIN_PATTERN.fullmatch(name) is None:
            _fail(f"built-in tool name {name!r} is malformed")
    patterns = _texts(document.get("allowed_tools", []), "allowed_tools")
    for pattern in patterns:
        if (
            BUILTIN_PATTERN.fullmatch(pattern) is None
            and TOOL_PATTERN.fullmatch(pattern) is None
        ):
            _fail(f"allowed tool {pattern!r} is neither a tool name nor mcp__server__*")
    policy_text = document.get("tool_policy", ToolPolicy.ALLOWLIST.value)
    try:
        policy = ToolPolicy(_text(policy_text, "tool_policy"))
    except ValueError:
        _fail(f"unknown tool_policy {policy_text!r}")
    inherit = document.get("inherit_account_mcp", False)
    if type(inherit) is not bool:
        _fail("inherit_account_mcp must be a boolean")
    return LineConfig(
        builtin_tools=tuple(sorted(set(builtin))),
        mcp_servers=parse_servers(document.get("mcp_servers", {})),
        tool_policy=policy,
        allowed_tools=tuple(sorted(set(patterns))),
        inherit_account_mcp=inherit,
    )


def decode_config(text: str) -> LineConfig:
    """The configuration stored as ``text``, checked strictly."""
    try:
        document: object = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except ConfigError:
        raise
    except (ValueError, RecursionError) as error:
        raise ConfigError(f"the configuration is not valid JSON: {error}") from error
    fields = dict(_object(document, "the configuration"))
    version = fields.pop("v", None)
    if type(version) is not int or version != CONFIG_VERSION:
        _fail(f"unsupported configuration version {version!r}")
    return config_from_document(fields)
