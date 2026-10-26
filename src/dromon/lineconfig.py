"""Per-line configuration: tools, MCP servers, tool policy, sandbox and settings.

A line's configuration names the built-in Claude Code tools it gets, the MCP servers it
connects to, and which tool calls are allowed. ``allowlist`` allows the given built-in
tools, Dromon's own tools, and every name or ``mcp__server__*`` pattern listed in
``allowed_tools``, and denies everything else. ``allow_all`` allows every tool the
engine offers. ``inherit_account_mcp`` lets Claude Code also load the MCP servers of
the operator's user settings and claude.ai account, which are otherwise excluded.
``sandbox`` is the line's own sandbox document, as ``dromon.sandbox`` defines it,
``settings`` its own engine settings, as ``dromon.settings`` defines them, and
``effort`` replaces the reasoning effort its driver declares.

Instructions. ``claude_code_prompt`` runs a Claude Code line on Claude Code's own system
prompt instead of an empty one, and ``instructions`` names files, such as a project's
``CLAUDE.md``, whose text is appended to the system prompt at the start of each turn. A
relative path is resolved against the line's working directory, and ``~`` against the
home directory. Only the paths are stored.

Working directory. ``workdir`` names the directory a line's engine runs in, such as a
git worktree of its own. A relative path is resolved against the project root, and
``~`` against the home directory, when the line is configured and again at each turn,
by ``dromon.workdir``. Only the path is stored, and a line without one runs in the
project root. The line's relative instruction and sandbox paths resolve against it.

References. Any text of an MCP server may hold ``${NAME}`` or ``${NAME:-default}``,
which the engine replaces with the daemon's environment at the start of each turn, as
Claude Code expands its ``.mcp.json``. Only the reference is stored, so a token can stay
out of the event log. Variables that hold the engine's own credential are refused.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final, NoReturn, cast

from dromon.model import ID_PATTERN
from dromon.sandbox import SandboxConfig, SandboxError, parse_sandbox
from dromon.settings import (
    SettingsConfig,
    SettingsError,
    parse_settings,
    reserved_variable,
)

CONFIG_VERSION: Final = 1

RESERVED_SERVERS: Final = frozenset({"dromon"})
"""Server names Dromon uses for its own tools."""

BUILTIN_PATTERN: Final = re.compile(r"[A-Z][A-Za-z0-9_]{0,63}")
TOOL_PATTERN: Final = re.compile(
    r"mcp__[a-z0-9][a-z0-9._-]{0,63}__(\*|[A-Za-z0-9_.-]{1,128})"
)
MAX_ENTRIES: Final = 256
MAX_TEXT: Final = 4096

MAX_INSTRUCTION_FILES: Final = 16

EFFORTS: Final = ("low", "medium", "high", "xhigh", "max")
"""The reasoning efforts Claude Code accepts."""

REFERENCE: Final = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]{0,127})(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """A line configuration is malformed or unsafe to accept."""


class UnsetVariable(ConfigError):
    """An MCP server refers to a variable the environment does not set."""


def references(text: str) -> tuple[str, ...]:
    """The variable names ``text`` refers to, or ``ConfigError`` for a ``${`` that does
    not start a well-formed reference, or for a reserved variable."""
    names: list[str] = []
    position = text.find("${")
    while position != -1:
        found = REFERENCE.match(text, position)
        if found is None:
            _fail(f"malformed variable reference at {text[position : position + 16]!r}")
        if reserved_variable(found.group(1)):
            _fail(f"a server may not refer to the variable {found.group(1)}")
        names.append(found.group(1))
        position = text.find("${", found.end())
    return tuple(names)


def expand(text: str, environment: Mapping[str, str]) -> str:
    """``text`` with each reference replaced from ``environment``, or
    ``UnsetVariable`` for an unset variable that has no default."""

    def value(found: re.Match[str]) -> str:
        name, default = found.group(1), found.group(2)
        if name in environment:
            return environment[name]
        if default is not None:
            return default
        raise UnsetVariable(f"the environment does not set {name}")

    return REFERENCE.sub(value, text)


def _check_url(url: str, what: str) -> str:
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        _fail(f"{what} url must use https, or http on the local host")
    return url


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

    def sdk_config(
        self, environment: Mapping[str, str] | None = None
    ) -> dict[str, object]:
        """The server as the Agent SDK's ``mcp_servers`` entry, with its references
        replaced from ``environment``, or kept as written when it is ``None``.

        Raises ``UnsetVariable`` for an unset variable, and ``ConfigError`` when a
        replaced url no longer meets the transport rule."""
        text: Callable[[str], str] = (
            (lambda value: value)
            if environment is None
            else (lambda value: expand(value, environment))
        )
        if self.transport == "stdio":
            config: dict[str, object] = {"type": "stdio", "command": text(self.command)}
            if self.args:
                config["args"] = [text(arg) for arg in self.args]
            if self.env:
                config["env"] = {name: text(value) for name, value in self.env}
            return config
        url = text(self.url)
        if environment is not None:
            _check_url(url, "the expanded server")
        config = {"type": self.transport, "url": url}
        if self.headers:
            config["headers"] = {name: text(value) for name, value in self.headers}
        return config


@dataclass(frozen=True, slots=True)
class LineConfig:
    builtin_tools: tuple[str, ...] = ()
    mcp_servers: tuple[tuple[str, McpServer], ...] = ()
    tool_policy: ToolPolicy = ToolPolicy.ALLOWLIST
    allowed_tools: tuple[str, ...] = field(default=())
    inherit_account_mcp: bool = False
    sandbox: SandboxConfig | None = None
    """The line's own sandbox document, before the project's is merged in."""
    settings: SettingsConfig | None = None
    """The line's own engine settings, before the project's are merged in."""
    effort: str | None = None
    """The reasoning effort, or ``None`` for the one the driver declares."""
    claude_code_prompt: bool = False
    """Whether a Claude Code line runs on Claude Code's own system prompt."""
    instructions: tuple[str, ...] = ()
    """Files whose text is appended to the system prompt, in order."""
    workdir: str | None = None
    """The directory the engine runs in, as written, or ``None`` for the project
    root."""
    project_settings: bool = False
    """Whether a Claude Code line also loads Claude Code's project setting source, the
    working directory's ``.claude/settings.json`` and what it brings."""

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
    document: dict[str, object] = {
        "v": CONFIG_VERSION,
        "builtin_tools": list(config.builtin_tools),
        "mcp_servers": {
            name: server.sdk_config() for name, server in config.mcp_servers
        },
        "tool_policy": config.tool_policy.value,
        "allowed_tools": list(config.allowed_tools),
        "inherit_account_mcp": config.inherit_account_mcp,
    }
    # Each optional key is omitted when unset, so configurations stored before it
    # existed keep their text.
    if config.sandbox is not None:
        document["sandbox"] = config.sandbox.document()
    if config.settings is not None:
        document["settings"] = config.settings.document()
    if config.effort is not None:
        document["effort"] = config.effort
    if config.claude_code_prompt:
        document["claude_code_prompt"] = True
    if config.instructions:
        document["instructions"] = list(config.instructions)
    if config.workdir is not None:
        document["workdir"] = config.workdir
    if config.project_settings:
        document["project_settings"] = True
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


def _workdir(value: object) -> str:
    """A working directory as written. Whether it exists and where it lies is
    checked against the project by ``dromon.workdir``, since this module has no
    project."""
    text = _text(value, "workdir")
    if "\x00" in text:
        _fail("workdir must not contain a NUL character")
    if text.startswith("~") and text != "~" and not text.startswith("~/"):
        _fail("workdir may start with ~ only as ~ or ~/, not as another user's home")
    return text


def _server_text(value: object, what: str) -> str:
    text = _text(value, what)
    try:
        references(text)
    except ConfigError as error:
        _fail(f"{what}: {error}")
    return text


def _texts(
    value: object, what: str, check: Callable[[object, str], str] = _text
) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(f"{what} is not an array")
    items = cast(list[object], value)
    if len(items) > MAX_ENTRIES:
        _fail(f"{what} has more than {MAX_ENTRIES} entries")
    return tuple(check(item, what) for item in items)


def _pairs(value: object, what: str) -> tuple[tuple[str, str], ...]:
    document = _object(value, what)
    if len(document) > MAX_ENTRIES:
        _fail(f"{what} has more than {MAX_ENTRIES} entries")
    return tuple(
        sorted((_text(k, what), _server_text(v, what)) for k, v in document.items())
    )


def _server(name: str, value: object) -> McpServer:
    document = _object(value, f"server {name}")
    transport = document.get("type", "stdio")
    if transport == "stdio":
        allowed = {"type", "command", "args", "env"}
        if not set(document) <= allowed or "command" not in document:
            _fail(f"server {name} must have a command and only {sorted(allowed)}")
        return McpServer(
            "stdio",
            command=_server_text(document["command"], f"server {name} command"),
            args=_texts(document.get("args", []), f"server {name} args", _server_text),
            env=_pairs(document.get("env", {}), f"server {name} env"),
        )
    if transport in ("http", "sse"):
        allowed = {"type", "url", "headers"}
        if not set(document) <= allowed or "url" not in document:
            _fail(f"server {name} must have a url and only {sorted(allowed)}")
        url = _server_text(document["url"], f"server {name} url")
        # A url that starts with a reference is checked once it is expanded, as
        # every url is at the start of a turn.
        if not url.startswith("${"):
            _check_url(url, f"server {name}")
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
        "sandbox",
        "settings",
        "effort",
        "claude_code_prompt",
        "instructions",
        "workdir",
        "project_settings",
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
    sandbox: SandboxConfig | None = None
    if "sandbox" in document:
        try:
            sandbox = parse_sandbox(document["sandbox"], operator=True)
        except SandboxError as error:
            _fail(str(error))
    settings: SettingsConfig | None = None
    if "settings" in document:
        try:
            settings = parse_settings(document["settings"])
        except SettingsError as error:
            _fail(str(error))
    effort = document.get("effort")
    if effort is not None and effort not in EFFORTS:
        _fail(f"effort must be one of {list(EFFORTS)}, found {effort!r}")
    prompt = document.get("claude_code_prompt", False)
    if type(prompt) is not bool:
        _fail("claude_code_prompt must be a boolean")
    instructions = _texts(document.get("instructions", []), "instructions")
    if len(instructions) > MAX_INSTRUCTION_FILES:
        _fail(f"instructions names more than {MAX_INSTRUCTION_FILES} files")
    if len(set(instructions)) != len(instructions):
        _fail("instructions names a file twice")
    workdir = _workdir(document["workdir"]) if "workdir" in document else None
    project_settings = document.get("project_settings", False)
    if type(project_settings) is not bool:
        _fail("project_settings must be a boolean")
    return LineConfig(
        builtin_tools=tuple(sorted(set(builtin))),
        mcp_servers=parse_servers(document.get("mcp_servers", {})),
        tool_policy=policy,
        allowed_tools=tuple(sorted(set(patterns))),
        inherit_account_mcp=inherit,
        sandbox=sandbox,
        settings=settings,
        effort=cast(str | None, effort),
        claude_code_prompt=prompt,
        instructions=instructions,
        workdir=workdir,
        project_settings=project_settings,
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
