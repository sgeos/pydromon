"""The Claude Code engine adapter, which runs session line turns through the Agent SDK.

Dromon drives the engine from outside. Each turn starts a Claude Code process through
``ClaudeSDKClient``, resumes the line's engine session when one exists, feeds every
message the engine emits to the line's Keleusma driver as an event, and performs the
driver's actions, such as interrupting a turn that exceeds the tool budget.

Credential policy. Each driver fixes a strategy, subscription only, API only or
flexible, and decides at run time whether the credential the engine reports is
acceptable. Usage billed per token must never start silently, so before a turn
``check_environment`` refuses any cloud provider, whose ``apiKeySource`` would read
``"none"`` like a subscription, and for a subscription driver also refuses every
variable that would make Claude Code prefer an API credential. The engine's init
report is then mapped to a credential and handed to the driver, and a turn whose
credential is unrecognized or refused is interrupted. ``setting_sources=[]`` keeps
user and project settings, such as an ``apiKeyHelper``, out of the line. Managed policy
settings still apply. Dromon never reads, stores or forwards the sign-in token, which
stays in Claude Code's keychain entry, as Anthropic's terms require.

Tool isolation. A line's tools come from its ``LineConfig``: the built-in tools it
lists, the MCP servers it names, and Dromon's own in-process tools such as
``send_message``. ``strict_mcp_config=True`` keeps every other MCP server out unless
the configuration sets ``inherit_account_mcp``, which matters because a claude.ai login
otherwise brings the account's connectors, such as mail and file sharing, into the
session. Dromon approves no tool in advance. A call that the engine would ask about
reaches the line's permission gate, which applies the configuration's policy, and a
turn whose engine offers a built-in tool or an MCP server the configuration does not
name is interrupted and refused. The line's own settings may approve calls in advance,
as described under Settings below.

Sandbox. ``sandbox`` is the line's effective sandbox settings, already merged and
resolved by ``dromon.sandbox.effective``, and Claude Code enforces them on shell
commands. With ``setting_sources=[]`` a line has no sandbox but the one given here and
any managed policy. The gate also refuses a shell call that asks to run outside an
enabled sandbox that does not allow it, in case the engine forwards one.

Settings. ``settings`` is the line's effective engine settings, already merged and
resolved by ``dromon.settings.effective``: permission rules, hooks, ``env`` and
``attribution``. They reach Claude Code through ``--settings``, which the SDK also uses
for the sandbox, and Claude Code applies them itself. A deny rule refuses a call before
the gate sees it, and an allow rule approves one without the gate, within the tools
the configuration offers. Fact, verified on 2026-10-25 with Claude Haiku 5.5: with
``setting_sources=[]`` a deny rule refused a Bash call, and a ``UserPromptSubmit`` hook
ran, saw a variable from ``env``, and its output reached the model. MCP server
references such as ``${TOKEN}`` are replaced from the daemon's environment here, at the
start of each turn, so the stored configuration holds only the reference.

Project settings. With ``project_settings`` a line also loads Claude Code's
``project`` setting source, which the SDK documents as ``.claude/settings.json`` under
the working directory, and which it requires for ``CLAUDE.md`` files to load. Dromon's
own settings still arrive through ``--settings``. The engine is still given only the
configured tools, and the init check still refuses any other. A project file must not
run code or grant anything, since an ``allow`` rule, for one, would approve a call
without the permission gate. So at the start of every turn ``check_project_settings``
admits only allowlisted settings keys, allowlisted ``.claude`` entries and skills with
allowlisted front matter, and refuses the turn otherwise. Plugins pass similar
allowlists. A line that loads either needs an enabled sandbox, whatever its tools, in
which the factory denies writes to the working directory's ``.claude`` and to each
plugin, as its ``Edit`` rules do for the built-in editing tools, so the line cannot
change what it loads.

Instructions. With ``claude_code_prompt`` a line runs on Claude Code's own system
prompt, and otherwise on an empty one, which the SDK passes when no prompt is given.
The text of the configuration's ``instructions`` files is appended at each turn, so a
project's ``CLAUDE.md`` reaches a line although ``setting_sources=[]`` keeps Claude Code
from loading it. A missing or oversized file refuses the turn.

Failure modes. A refusal by the policy or the driver raises ``EngineRefused`` after the
turn is interrupted. A driver failure raises ``DriverError``. Engine and transport
failures from the SDK propagate unchanged.
"""

import importlib.metadata
import json
import os
import re
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    McpSdkServerConfig,
    McpServerConfig,
    Message,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SdkMcpTool,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    SandboxSettings,
    SdkPluginConfig,
    ToolPermissionContext,
    ToolUseBlock,
    create_sdk_mcp_server,
)

from claude_agent_sdk.types import SystemPromptPreset

from dromon.activity import ActivityKind, Observer, ignore
from dromon.driver import ActionTag, Credential, Driver, Event, Strategy
from dromon.lineconfig import DEFAULT_CONFIG, ConfigError, LineConfig
from dromon.sandbox import blocks_unsandboxed

ENGINE_NAME: Final = "claude-code"
"""The engine this adapter runs, as drivers name it."""

PROJECT_SETTINGS_FILE: Final = Path(".claude") / "settings.json"
"""The project settings file Claude Code's ``project`` source reads, under the line's
working directory."""

MAX_PROJECT_SETTINGS_BYTES: Final = 1024 * 1024

PROJECT_SETTINGS_KEYS: Final = frozenset(
    {"$schema", "attribution", "includeCoAuthoredBy"}
)
"""The only keys a project settings file may set for a Dromon line. ``$schema`` names a
schema for editors, and the other two only choose the attribution text written into
commits and pull requests. None grants a permission, widens a sandbox, changes a
credential, environment or model, or runs code. Every other key refuses the turn,
including ``hooks``, ``mcpServers``, ``enableAllProjectMcpServers``,
``enabledMcpjsonServers``, ``statusLine``, ``permissions``, ``sandbox``, ``env`` and the
plugin keys. A line's hooks come from Dromon's own settings document instead."""

CLAUDE_DIR_ENTRIES: Final = frozenset(
    {"settings.json", "settings.local.json", "skills", "CLAUDE.md"}
)
"""The only entries a working directory's ``.claude`` may hold for such a line. The
local settings file is a source Dromon never loads. Others, such as ``commands``,
``agents``, ``hooks`` or ``output-styles``, can grant tools or run commands, so their
presence refuses the turn."""

SKILL_FRONTMATTER_KEYS: Final = frozenset({"name", "description", "license", "version"})
"""The only front matter keys a project skill's ``SKILL.md`` may set. Others, such as
``allowed-tools``, ``hooks`` or ``model``, grant a tool, run code or change the
model."""

PLUGIN_ENTRIES: Final = frozenset(
    {
        ".claude-plugin",
        "skills",
        ".in_use",
        "README",
        "README.md",
        "README.txt",
        "LICENSE",
        "LICENSE.md",
        "LICENSE.txt",
    }
)
"""The only entries a plugin directory may hold for a Dromon line. ``.in_use`` is
Claude Code's own record of the sessions using an installed plugin."""

PLUGIN_MANIFEST_ENTRIES: Final = frozenset({"plugin.json", "marketplace.json"})

PLUGIN_KEYS: Final = frozenset(
    {
        "name",
        "version",
        "description",
        "author",
        "homepage",
        "repository",
        "license",
        "keywords",
    }
)
"""The only keys a plugin's ``plugin.json`` may set. Each describes the plugin and none
names a component, so none grants a tool or runs code. Component keys such as
``hooks``, ``mcpServers``, ``commands``, ``agents``, ``outputStyles``, ``lspServers``
and ``skills``, and any unknown key, refuse the turn. Skills load from the plugin's
own ``skills`` directory, which needs no key."""

MARKETPLACE_KEYS: Final = frozenset(
    {"$schema", "name", "description", "owner", "plugins"}
)
MARKETPLACE_ENTRY_KEYS: Final = PLUGIN_KEYS | {"source"}
"""A ``marketplace.json`` beside the manifest may list its plugins, and each entry only
describes one and names its ``source``. An entry can otherwise declare components, so
any other key refuses the turn."""

MAX_SKILLS: Final = 256
MAX_FRONTMATTER_LINES: Final = 256

MCP_SERVER_NAME: Final = "dromon"
"""The name of the in-process MCP server that serves a line's Dromon tools."""

KEY_SOURCES: Final[dict[str, Credential]] = {
    "none": Credential.SUBSCRIPTION,
    "ANTHROPIC_API_KEY": Credential.API,
    "apiKeyHelper": Credential.API,
    "/login managed key": Credential.API,
}
"""Each ``apiKeySource`` Claude Code reports, as a credential. ``"none"`` means a
claude.ai login only because ``check_environment`` has excluded cloud providers."""

PROVIDER_ENVIRONMENT: Final = (
    "ANTHROPIC_FEDERATION_RULE_ID",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
)
"""Variables selecting a provider Dromon does not model. Always refused."""

API_ENVIRONMENT: Final = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_PROFILE",
)
"""Variables that make Claude Code prefer an API credential over the subscription."""

NON_SUBSCRIPTION_ENVIRONMENT: Final = API_ENVIRONMENT + PROVIDER_ENVIRONMENT


class EngineRefused(Exception):
    """The credential policy or the driver refused to run the turn."""


MAX_INSTRUCTION_BYTES: Final = 256 * 1024
"""The largest instruction file a line reads, so a stray file cannot flood a turn."""


def read_instructions(paths: Sequence[str], root: Path) -> str:
    """The text of the instruction files ``paths``, each under a heading naming it,
    with relative paths resolved against ``root``. Raises ``EngineRefused`` for a file
    that cannot be read, is not UTF-8, or exceeds ``MAX_INSTRUCTION_BYTES``."""
    sections: list[str] = []
    for path in paths:
        file = Path(path).expanduser()
        if not file.is_absolute():
            file = root / file
        try:
            with file.open("rb") as handle:
                raw = handle.read(MAX_INSTRUCTION_BYTES + 1)
        except OSError as error:
            raise EngineRefused(
                f"cannot read the instructions {path}: {error}"
            ) from error
        if len(raw) > MAX_INSTRUCTION_BYTES:
            raise EngineRefused(
                f"the instructions {path} exceed {MAX_INSTRUCTION_BYTES} bytes"
            )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise EngineRefused(f"the instructions {path} are not UTF-8") from error
        sections.append(f"Contents of {path}:\n\n{text.strip()}")
    return "\n\n".join(sections)


class AgentSession(Protocol):
    """The part of ``ClaudeSDKClient`` the adapter uses, so tests can substitute it."""

    async def query(self, prompt: str) -> None: ...

    def receive_response(self) -> AsyncIterator[Message]: ...

    async def interrupt(self) -> None: ...


type SessionFactory = Callable[
    [ClaudeAgentOptions], AbstractAsyncContextManager[AgentSession]
]


def _sdk_session(
    options: ClaudeAgentOptions,
) -> AbstractAsyncContextManager[AgentSession]:
    return cast(
        AbstractAsyncContextManager[AgentSession], ClaudeSDKClient(options=options)
    )


def check_environment(environment: Mapping[str, str], strategy: Strategy) -> None:
    """Raise ``EngineRefused`` if ``environment`` conflicts with ``strategy``."""
    refused = (
        NON_SUBSCRIPTION_ENVIRONMENT
        if strategy is Strategy.SUBSCRIPTION
        else PROVIDER_ENVIRONMENT
    )
    present = [name for name in refused if environment.get(name)]
    if present:
        raise EngineRefused(
            f"the environment conflicts with the {strategy} driver: "
            + ", ".join(present)
            + ". Unset these variables or choose another driver."
        )


def credential_of(api_key_source: str) -> Credential | None:
    """The credential behind a reported ``apiKeySource``, or ``None`` if unknown."""
    return KEY_SOURCES.get(api_key_source)


@dataclass(frozen=True, slots=True)
class Capabilities:
    """What the engine reported about itself at the start of a turn."""

    sdk_version: str
    claude_code_version: str
    model: str
    api_key_source: str
    tools: tuple[str, ...]
    permission_mode: str
    capabilities: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TurnReport:
    subtype: str
    num_turns: int
    text: str
    tool_uses: tuple[str, ...]
    interrupted_for: str | None
    credential: Credential | None
    session_id: str
    capabilities: Capabilities | None
    cost_estimate_usd: float | None
    """The SDK's client-side estimate. On a subscription it is not a charge."""


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    items = cast(list[object], value)
    return tuple(item for item in items if isinstance(item, str))


def _capabilities(data: Mapping[str, object]) -> Capabilities:
    def text(key: str) -> str:
        value = data.get(key)
        return value if isinstance(value, str) else ""

    return Capabilities(
        sdk_version=importlib.metadata.version("claude-agent-sdk"),
        claude_code_version=text("claude_code_version"),
        model=text("model"),
        api_key_source=text("apiKeySource"),
        tools=_strings(data.get("tools")),
        permission_mode=text("permissionMode"),
        capabilities=_strings(data.get("capabilities")),
    )


@dataclass(slots=True)
class _Turn:
    """What one turn accumulates while its messages arrive."""

    text: list[str] = field(default_factory=list[str])
    tool_uses: list[str] = field(default_factory=list[str])
    interrupted_for: str | None = None
    refusal: str | None = None
    capabilities: Capabilities | None = None
    credential: Credential | None = None


type PermissionGate = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Coroutine[Any, Any, PermissionResultAllow | PermissionResultDeny],
]


def permission_gate(
    config: LineConfig,
    own_tools: frozenset[str],
    sandbox: Mapping[str, object] | None = None,
) -> PermissionGate:
    """A gate that applies ``config``'s policy and denies every other tool call,
    and every shell call that would leave the ``sandbox`` without its leave."""

    async def gate(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        del context
        if blocks_unsandboxed(sandbox, tool_name, tool_input):
            return PermissionResultDeny(
                message="this line's sandbox does not allow unsandboxed commands"
            )
        if config.allows(tool_name, own_tools):
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=f"{tool_name} is not available to this line"
        )

    return gate


def _read_bounded(file: Path, what: str) -> bytes:
    try:
        with file.open("rb") as handle:
            raw = handle.read(MAX_PROJECT_SETTINGS_BYTES + 1)
    except OSError as error:
        raise EngineRefused(f"cannot read the {what} {file}: {error}") from error
    if len(raw) > MAX_PROJECT_SETTINGS_BYTES:
        raise EngineRefused(f"the {what} {file} is too large")
    return raw


def _check_settings_file(file: Path) -> None:
    raw = _read_bounded(file, "project settings")
    try:
        document: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise EngineRefused(f"the project settings {file} are not JSON") from error
    if not isinstance(document, dict):
        raise EngineRefused(f"the project settings {file} are not a JSON object")
    found = sorted(set(cast(dict[str, object], document)) - PROJECT_SETTINGS_KEYS)
    if found:
        raise EngineRefused(
            f"the project settings {file} set {', '.join(found)}, which a Dromon line"
            f" may not take from them. Only {', '.join(sorted(PROJECT_SETTINGS_KEYS))}"
            " are accepted, and a line's hooks belong in Dromon's settings document"
        )


FRONTMATTER_KEY: Final = re.compile(r"([A-Za-z0-9_-]+):(?:[ \t]+(.*))?")
"""A top-level key as YAML writes one without quotes, at column zero."""

BLOCK_SCALAR: Final = re.compile(r"[|>][+-]?")
"""A block scalar indicator, ``|`` or ``>`` with an optional chomping indicator, such as
``>`` or ``|-``, after which indented lines up to the next line at column zero are the
key's literal text rather than keys. An explicit indentation indicator is refused."""


def _check_skill(file: Path) -> None:
    """Refuse a skill whose front matter could set anything outside the allowlist.

    The front matter is the block between a first line ``---``, after any byte order
    mark, and the next line ``---``. Each line in it must be blank, a comment, or an
    allowlisted key without quotes at column zero. An indented line is accepted only
    as the text of a block scalar such as ``description: >``, where YAML reads it as
    text. Anything else, an indented or quoted key included, refuses the turn. This is
    stricter than YAML, deliberately, since no YAML parser is used."""
    text = _read_bounded(file, "skill").decode("utf-8", "replace")
    lines = text.removeprefix("\ufeff").splitlines()
    if not lines or lines[0].rstrip() != "---":
        return
    in_block = False
    for number, line in enumerate(lines[1 : 1 + MAX_FRONTMATTER_LINES], start=2):
        if line.rstrip() == "---":
            return
        if not line.strip():
            continue
        if in_block and line[0] in " \t":
            continue
        in_block = False
        if line.lstrip().startswith("#"):
            continue
        key = FRONTMATTER_KEY.fullmatch(line.rstrip())
        if key is None:
            raise EngineRefused(
                f"the skill {file} has front matter line {number} that is neither"
                " blank, a comment nor a key at column zero"
            )
        if key.group(1) not in SKILL_FRONTMATTER_KEYS:
            raise EngineRefused(
                f"the skill {file} sets {key.group(1)} in its front matter, which a"
                " Dromon line may not take from a project or plugin"
            )
        value = (key.group(2) or "").strip()
        in_block = BLOCK_SCALAR.fullmatch(value) is not None
    raise EngineRefused(f"the skill {file} has front matter that does not end")


def _json_object(file: Path, what: str) -> dict[str, object]:
    raw = _read_bounded(file, what)
    try:
        document: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise EngineRefused(f"the {what} {file} is not JSON") from error
    if not isinstance(document, dict):
        raise EngineRefused(f"the {what} {file} is not a JSON object")
    return cast(dict[str, object], document)


def _refuse_keys(found: set[str], allowed: frozenset[str], where: str) -> None:
    extra = sorted(found - allowed)
    if extra:
        raise EngineRefused(
            f"{where} sets {', '.join(extra)}, which a Dromon line may not load"
        )


def _check_skills(skills: Path) -> None:
    if not os.path.lexists(skills):
        return
    if not skills.is_dir():
        raise EngineRefused(f"{skills} is not a directory")
    names = sorted(os.listdir(skills))
    if len(names) > MAX_SKILLS:
        raise EngineRefused(f"{skills} holds more than {MAX_SKILLS} skills")
    for name in names:
        skill = skills / name / "SKILL.md"
        if not (skills / name).is_dir() or not skill.is_file():
            raise EngineRefused(
                f"{skills} holds {name}, which is not a skill directory with a SKILL.md"
            )
        _check_skill(skill)


def _check_source(source: object, directory: Path, where: str) -> None:
    """Refuse a listing entry whose ``source`` is not a relative path that stays in the
    plugin directory, such as ``./``. A remote or absolute source would fetch or load
    a plugin no allowlist saw."""
    if not isinstance(source, str) or not source:
        raise EngineRefused(f"{where} lists a plugin whose source is not a path")
    if source.startswith(("/", "~")) or "://" in source or "\x00" in source:
        raise EngineRefused(f"{where} lists a plugin whose source is not relative")
    real = Path(os.path.realpath(directory / source))
    if real != directory and directory not in real.parents:
        raise EngineRefused(f"{where} lists a plugin whose source leaves the plugin")


def resolve_plugin(path: str) -> Path:
    """The real directory a configured plugin path names, with ``~`` expanded."""
    return Path(os.path.realpath(os.path.expanduser(path)))


def check_plugin(directory: Path) -> None:
    """Refuse the turn unless the plugin at ``directory`` grants nothing and runs
    nothing: allowlisted entries, a ``plugin.json`` of descriptive keys only, an
    optional ``marketplace.json`` that only describes, and skills whose front matter
    passes the project skill allowlist. Raises ``EngineRefused``, naming the plugin
    and the key or entry, never a value."""
    if not directory.is_dir():
        raise EngineRefused(f"the plugin {directory} is not a directory")
    try:
        entries = set(os.listdir(directory))
    except OSError as error:
        raise EngineRefused(f"cannot list the plugin {directory}: {error}") from error
    extra = sorted(entries - PLUGIN_ENTRIES)
    if extra:
        raise EngineRefused(
            f"the plugin {directory} holds {', '.join(extra)}, which a Dromon line"
            " may not load"
        )
    manifest_dir = directory / ".claude-plugin"
    if not manifest_dir.is_dir():
        raise EngineRefused(f"the plugin {directory} has no .claude-plugin directory")
    found = sorted(set(os.listdir(manifest_dir)) - PLUGIN_MANIFEST_ENTRIES)
    if found:
        raise EngineRefused(
            f"the plugin {directory} holds .claude-plugin/{', '.join(found)}, which a"
            " Dromon line may not load"
        )
    manifest = manifest_dir / "plugin.json"
    if not manifest.is_file():
        raise EngineRefused(f"the plugin {directory} has no plugin.json")
    document = _json_object(manifest, "plugin manifest")
    _refuse_keys(set(document), PLUGIN_KEYS, f"the plugin {directory}")
    marketplace = manifest_dir / "marketplace.json"
    if os.path.lexists(marketplace):
        listing = _json_object(marketplace, "plugin marketplace")
        where = f"the plugin {directory} marketplace"
        _refuse_keys(set(listing), MARKETPLACE_KEYS, where)
        listed = listing.get("plugins", [])
        if not isinstance(listed, list):
            raise EngineRefused(f"{where} lists its plugins in something not a list")
        for entry in cast(list[object], listed):
            if not isinstance(entry, dict):
                raise EngineRefused(f"{where} lists a plugin that is not an object")
            listed_entry = cast(dict[str, object], entry)
            _refuse_keys(set(listed_entry), MARKETPLACE_ENTRY_KEYS, where)
            _check_source(listed_entry.get("source"), directory, where)
    _check_skills(directory / "skills")


def check_project_settings(cwd: Path) -> None:
    """Refuse the turn unless what Claude Code's project source would load under
    ``cwd`` grants nothing and runs nothing: a settings file of allowlisted keys only,
    a ``.claude`` of allowlisted entries only, and skills whose front matter sets only
    allowlisted keys. A missing ``.claude`` is fine. Raises ``EngineRefused``, naming
    the file and the key or entry, never a value."""
    directory = cwd / ".claude"
    if not os.path.lexists(directory):
        return
    if not directory.is_dir():
        raise EngineRefused(f"{directory} is not a directory")
    try:
        entries = sorted(os.listdir(directory))
    except OSError as error:
        raise EngineRefused(f"cannot list {directory}: {error}") from error
    extra = [name for name in entries if name not in CLAUDE_DIR_ENTRIES]
    if extra:
        raise EngineRefused(
            f"{directory} holds {', '.join(extra)}, which a Dromon line loading"
            " project settings may not take. Only"
            f" {', '.join(sorted(CLAUDE_DIR_ENTRIES))} are accepted"
        )
    settings = cwd / PROJECT_SETTINGS_FILE
    if os.path.lexists(settings):
        _check_settings_file(settings)
    _check_skills(directory / "skills")


def mcp_server_of(tool_name: str) -> str | None:
    """The server an MCP tool name belongs to, or ``None`` for a built-in tool."""
    if not tool_name.startswith("mcp__"):
        return None
    server, separator, _ = tool_name.removeprefix("mcp__").partition("__")
    return server if separator else None


def mcp_tool_name(tool: SdkMcpTool[object]) -> str:
    """The name under which Claude Code exposes one of Dromon's in-process tools."""
    return f"mcp__{MCP_SERVER_NAME}__{tool.name}"


class ClaudeCodeLine:
    """One session line on the Claude Code engine, with its driver.

    ``config`` gives the line's built-in tools, MCP servers and tool policy, and
    ``mcp_tools`` the in-process tools Dromon serves it. Both are empty by default, so
    a line does nothing but converse until its tools are configured deliberately.
    """

    def __init__(
        self,
        driver: Driver,
        cwd: Path,
        *,
        config: LineConfig = DEFAULT_CONFIG,
        mcp_tools: Sequence[SdkMcpTool[object]] = (),
        session_id: str | None = None,
        sandbox: Mapping[str, object] | None = None,
        settings: Mapping[str, object] | None = None,
        environment: Mapping[str, str] | None = None,
        session_factory: SessionFactory = _sdk_session,
        observe: Observer = ignore,
    ) -> None:
        if driver.settings.engine != ENGINE_NAME:
            raise EngineRefused(
                f"the driver for {driver.settings.model} is written for the"
                f" {driver.settings.engine} engine, not {ENGINE_NAME}"
            )
        self.driver = driver
        self.cwd = cwd
        self.config = config
        self.mcp_tools = tuple(mcp_tools)
        self.session_id = session_id
        self.sandbox = None if sandbox is None else dict(sandbox)
        self.settings = None if settings is None else dict(settings)
        self._environment = os.environ if environment is None else environment
        self._session_factory = session_factory
        self._observe = observe

    @property
    def own_tools(self) -> frozenset[str]:
        """The names of Dromon's in-process tools for this line."""
        return frozenset(mcp_tool_name(tool) for tool in self.mcp_tools)

    def options(self) -> ClaudeAgentOptions:
        settings = self.driver.settings
        effort = self.config.effort or settings.effort
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            raise EngineRefused(f"the driver names an unknown effort {effort!r}")
        servers: dict[str, McpSdkServerConfig | dict[str, object]] = {}
        for name, server in self.config.mcp_servers:
            try:
                servers[name] = server.sdk_config(self._environment)
            except ConfigError as error:
                raise EngineRefused(
                    f"the MCP server {name} cannot start: {error}"
                ) from error
        if self.mcp_tools:
            servers[MCP_SERVER_NAME] = create_sdk_mcp_server(
                MCP_SERVER_NAME, tools=list(self.mcp_tools)
            )
        loads = self.config.project_settings or self.config.plugins
        sandboxed = self.sandbox is not None and self.sandbox.get("enabled") is True
        if loads and not sandboxed:
            raise EngineRefused(
                "a line that loads project settings or plugins needs an enabled"
                " sandbox, whatever its tools, so that what it loads is write"
                " protected by the sandbox and not by permission rules alone"
            )
        if self.config.project_settings:
            check_project_settings(self.cwd)
        plugins: list[SdkPluginConfig] = []
        for written in self.config.plugins:
            directory = resolve_plugin(written)
            check_plugin(directory)
            plugins.append({"type": "local", "path": str(directory)})
        return ClaudeAgentOptions(
            model=settings.model,
            tools=list(self.config.builtin_tools),
            mcp_servers=cast(dict[str, McpServerConfig], servers),
            strict_mcp_config=not self.config.inherit_account_mcp,
            can_use_tool=permission_gate(self.config, self.own_tools, self.sandbox),
            setting_sources=["project"] if self.config.project_settings else [],
            plugins=plugins,
            max_turns=settings.max_turns,
            effort=effort,
            cwd=str(self.cwd),
            resume=self.session_id,
            sandbox=cast(SandboxSettings, self.sandbox),
            settings=None if self.settings is None else json.dumps(self.settings),
            system_prompt=self._system_prompt(),
        )

    def _system_prompt(self) -> str | SystemPromptPreset | None:
        appended = read_instructions(self.config.instructions, self.cwd)
        if self.config.claude_code_prompt:
            preset: SystemPromptPreset = {"type": "preset", "preset": "claude_code"}
            if appended:
                preset["append"] = appended
            return preset
        return appended or None

    async def run_turn(self, prompt: str) -> TurnReport:
        """Run one turn for ``prompt`` and return what happened."""
        check_environment(self._environment, self.driver.settings.strategy)
        options = self.options()
        self.driver.step(Event("turn_start"))
        turn = _Turn()
        result: ResultMessage | None = None
        async with self._session_factory(options) as session:
            await session.query(prompt)
            async for message in session.receive_response():
                if isinstance(message, ResultMessage):
                    result = message
                    self.driver.step(
                        Event("result", message.subtype, message.num_turns)
                    )
                    break
                await self._handle(session, message, turn)
        if turn.refusal is not None:
            raise EngineRefused(turn.refusal)
        if result is None:
            raise EngineRefused("the engine ended the turn without a result")
        self.session_id = result.session_id
        return TurnReport(
            subtype=result.subtype,
            num_turns=result.num_turns,
            text="".join(turn.text),
            tool_uses=tuple(turn.tool_uses),
            interrupted_for=turn.interrupted_for,
            credential=turn.credential,
            session_id=result.session_id,
            capabilities=turn.capabilities,
            cost_estimate_usd=result.total_cost_usd,
        )

    async def _handle(
        self, session: AgentSession, message: Message, turn: _Turn
    ) -> None:
        if isinstance(message, SystemMessage) and message.subtype == "init":
            turn.capabilities = _capabilities(message.data)
            await self._check_init(session, turn.capabilities, turn)
        elif isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    turn.text.append(block.text)
                    self._observe(ActivityKind.TEXT, block.text)
                    event = Event("text", "", len(block.text))
                elif isinstance(block, ToolUseBlock):
                    turn.tool_uses.append(block.name)
                    self._observe(ActivityKind.TOOL, block.name)
                    event = Event("tool_use", block.name)
                elif isinstance(block, ThinkingBlock):
                    self._observe(ActivityKind.THINKING, "")
                    event = Event("thinking")
                else:
                    continue
                action = self.driver.step(event)
                if action.tag is ActionTag.INTERRUPT and turn.interrupted_for is None:
                    turn.interrupted_for = action.text
                    await session.interrupt()

    async def _check_init(
        self, session: AgentSession, capabilities: Capabilities, turn: _Turn
    ) -> None:
        turn.refusal = self._refusal(capabilities, turn)
        if turn.refusal is not None:
            await session.interrupt()

    def _offered_as_configured(self, tool_name: str) -> bool:
        """Whether the engine may offer ``tool_name`` under this configuration."""
        server = mcp_server_of(tool_name)
        if server is None:
            return tool_name in self.config.builtin_tools
        if server == MCP_SERVER_NAME:
            return tool_name in self.own_tools
        return server in self.config.server_names() or self.config.inherit_account_mcp

    def _refusal(self, capabilities: Capabilities, turn: _Turn) -> str | None:
        """Why the driver or the policy refuses this engine, or ``None`` to proceed."""
        unexpected = sorted(
            name for name in capabilities.tools if not self._offered_as_configured(name)
        )
        if unexpected:
            return "the engine offers tools the line was not given: " + ", ".join(
                unexpected
            )
        credential = credential_of(capabilities.api_key_source)
        if credential is None:
            return (
                f"the engine reports an unrecognized credential source"
                f" {capabilities.api_key_source!r}"
            )
        turn.credential = credential
        verdict = self.driver.step(Event("credential", credential.value))
        if verdict.tag is ActionTag.REFUSE:
            return f"the {verdict.text} driver refuses the {credential} credential"
        verdict = self.driver.step(Event("init", capabilities.model))
        if verdict.tag is ActionTag.REFUSE:
            return (
                f"the driver for {verdict.text} refuses the engine model"
                f" {capabilities.model!r}"
            )
        return None
