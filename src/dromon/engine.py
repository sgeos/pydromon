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

Kept sessions. With ``keep_session`` the connection stays open between turns, so
background commands outlive a turn. Every check above still runs at each turn, and a
digest of the options and of the loaded plugin and ``.claude`` files must match the
connection's, or it is closed and opened anew. Claude Code 2.1.310 reports its init on
a connection's first prompt and again only after a model or permission mode change, a
fact read from its bundled code, so a reused turn first sends the unchanged permission
mode and is refused if no init arrives before the model's output. A pump task reads
the connection between turns, counts background tasks, and interrupts a turn the
engine begins by itself, while the gate denies every call outside a checked turn,
and on every turn, kept or not, before its init check. Between turns the watch closes
the connection when a plugin or ``.claude`` file the engine loaded changes.
``docs/protocol/LINE_CONFIG.md`` is normative.

Failure modes. A refusal by the policy or the driver raises ``EngineRefused`` after the
turn is interrupted. A driver failure raises ``DriverError``. Engine and transport
failures from the SDK propagate unchanged.
"""

import asyncio
import contextlib
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
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
    PermissionMode,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SdkMcpTool,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    SandboxSettings,
    SdkPluginConfig,
    TaskNotificationMessage,
    TaskStartedMessage,
    TaskUpdatedMessage,
    TERMINAL_TASK_STATUSES,
    ToolPermissionContext,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
)

from claude_agent_sdk.types import SystemPromptPreset

from dromon.activity import ActivityKind, Observer, ignore
from dromon.driver import ActionTag, Credential, Driver, Event, Strategy
from dromon.lineconfig import DEFAULT_CONFIG, ConfigError, LineConfig
from dromon.processes import MARK_VARIABLE
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
    sections = [
        f"Contents of {path}:\n\n{_instruction_text(path, root).strip()}"
        for path in paths
    ]
    return "\n\n".join(sections)


def instruction_sizes(paths: Sequence[str], root: Path) -> list[tuple[str, int]]:
    """Each instruction file of ``paths`` with its size in bytes, read as
    ``read_instructions`` reads it, and refused as it refuses."""
    return [
        (path, len(_instruction_text(path, root).encode("utf-8"))) for path in paths
    ]


def _instruction_text(path: str, root: Path) -> str:
    file = Path(path).expanduser()
    if not file.is_absolute():
        file = root / file
    try:
        with file.open("rb") as handle:
            raw = handle.read(MAX_INSTRUCTION_BYTES + 1)
    except OSError as error:
        raise EngineRefused(f"cannot read the instructions {path}: {error}") from error
    if len(raw) > MAX_INSTRUCTION_BYTES:
        raise EngineRefused(
            f"the instructions {path} exceed {MAX_INSTRUCTION_BYTES} bytes"
        )
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EngineRefused(f"the instructions {path} are not UTF-8") from error


CLAUDE_CODE_PROMPT_MARK: Final = "[Claude Code's own system prompt]"
"""Stands for Claude Code's system prompt in a composed view, since Dromon never holds
that prompt's text."""


def composed_instructions(engine: str, config: LineConfig, cwd: Path) -> str:
    """The instructions a line on ``engine`` with ``config`` receives at its next turn,
    composed as its adapter composes them. The Claude Code adapter passes them as the
    system prompt, after Claude Code's own when ``claude_code_prompt`` is set. The Codex
    adapter passes them as developer instructions, and the Grok adapter as rules. Files
    the engine itself loads, such as project ``CLAUDE.md`` files, are not part of it.
    Raises ``EngineRefused`` as ``read_instructions`` does."""
    text = read_instructions(config.instructions, cwd)
    if engine == ENGINE_NAME and config.claude_code_prompt:
        return (
            f"{CLAUDE_CODE_PROMPT_MARK}\n\n{text}" if text else CLAUDE_CODE_PROMPT_MARK
        )
    return text


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


class KeptSession(Protocol):
    """The part of ``ClaudeSDKClient`` a kept connection uses, so tests can substitute
    it. ``receive_messages`` reads every message, between turns too, and
    ``set_permission_mode`` asks the engine to report its init again."""

    async def query(self, prompt: str) -> None: ...

    def receive_messages(self) -> AsyncIterator[Message]: ...

    async def interrupt(self) -> None: ...

    async def set_permission_mode(self, mode: PermissionMode) -> None: ...


type KeptSessionFactory = Callable[
    [ClaudeAgentOptions], AbstractAsyncContextManager[KeptSession]
]


def _sdk_kept_session(
    options: ClaudeAgentOptions,
) -> AbstractAsyncContextManager[KeptSession]:
    return cast(
        AbstractAsyncContextManager[KeptSession], ClaudeSDKClient(options=options)
    )


KEEP_POLL_SECONDS: Final = 5.0
"""How often a kept connection's watch looks at its idle deadline and, between turns,
at the files its engine loaded."""

SHELL_TOOLS: Final = frozenset({"Bash", "Monitor", "PowerShell"})
"""Built-in tools that run commands, which a kept line may give only inside an enabled
sandbox, since what they start in the background outlives the turn. ``Monitor``
streams a command's output, which is an inference from its description."""

FILES_CHANGED: Final = "files its engine loaded changed between turns"

INTERRUPT_TIMEOUT_SECONDS: Final = 10.0
"""How long an interrupt of a kept connection may take before it is closed instead."""

MAX_FINGERPRINT_FILES: Final = 4096
MAX_FINGERPRINT_FILE_BYTES: Final = 1024 * 1024

DIED_NOTE: Final = (
    "Dromon note: your engine's process ended since your last turn, so this turn"
    " resumes your session in a new process, and every background task of the old"
    " one was lost. Check for partial output before starting again."
)

SELF_STARTED_NOTE: Final = (
    "Dromon note: between your turns the engine began a turn by itself, most likely"
    " because a background task ended. Only Dromon starts turns, so it was"
    " interrupted, and its tool calls were denied. Read the task's output file to"
    " see how it ended."
)


def closed_note(reason: str, tasks: int) -> str:
    """The note a line's next prompt carries after Dromon closed its kept
    connection for ``reason`` with ``tasks`` background tasks reported running."""
    lost = (
        f" The {tasks} background tasks the engine reported running were stopped"
        " with it."
        if tasks
        else ""
    )
    return (
        f"Dromon note: your kept engine session was closed because {reason}, so this"
        f" turn resumes it in a new process.{lost}"
    )


def tree_digest(directory: Path) -> str:
    """A SHA-256 digest of every regular file's path and content under
    ``directory``, without following links, so that a kept connection notices a
    change to what its engine loaded. A missing directory has a digest of its own.
    Raises ``EngineRefused`` past ``MAX_FINGERPRINT_FILES`` files or for a file over
    ``MAX_FINGERPRINT_FILE_BYTES``, since a change there could not be noticed."""
    digest = hashlib.sha256()
    if not directory.is_dir():
        digest.update(b"absent")
        return digest.hexdigest()
    count = 0
    for parent, directories, files in os.walk(directory, followlinks=False):
        directories.sort()
        for name in sorted(files):
            count += 1
            if count > MAX_FINGERPRINT_FILES:
                raise EngineRefused(
                    f"{directory} holds more than {MAX_FINGERPRINT_FILES} files, too"
                    " many to check between turns of a kept session"
                )
            file = Path(parent) / name
            relative = str(file.relative_to(directory)).encode("utf-8", "replace")
            digest.update(len(relative).to_bytes(8, "big") + relative)
            if file.is_symlink() or not file.is_file():
                digest.update(b"special")
                continue
            try:
                with file.open("rb") as handle:
                    raw = handle.read(MAX_FINGERPRINT_FILE_BYTES + 1)
            except OSError as error:
                raise EngineRefused(f"cannot read {file}: {error}") from error
            if len(raw) > MAX_FINGERPRINT_FILE_BYTES:
                raise EngineRefused(
                    f"{file} exceeds {MAX_FINGERPRINT_FILE_BYTES} bytes, too large to"
                    " check between turns of a kept session"
                )
            digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


PERMISSION_MODES: Final[dict[str, PermissionMode]] = {
    "default": "default",
    "acceptEdits": "acceptEdits",
    "plan": "plan",
    "bypassPermissions": "bypassPermissions",
    "dontAsk": "dontAsk",
    "auto": "auto",
}


def permission_mode_of(data: Mapping[str, object]) -> PermissionMode:
    """The permission mode an init reports, to be sent back unchanged so that the
    engine reports its init again, or ``"default"`` for one the SDK does not name."""
    mode = data.get("permissionMode")
    return PERMISSION_MODES.get(mode, "default") if isinstance(mode, str) else "default"


def begins_turn(message: Message) -> bool:
    """Whether ``message``, arriving between Dromon's turns, shows that the engine
    began a turn by itself. A background task's report, or any other system notice,
    does not."""
    if isinstance(message, SystemMessage):
        return message.subtype == "init"
    return isinstance(
        message, AssistantMessage | UserMessage | StreamEvent | ResultMessage
    )


def task_ended(message: Message) -> str | None:
    """The background task ``message`` reports ended, or ``None``."""
    if isinstance(message, TaskNotificationMessage):
        return message.task_id
    if isinstance(message, TaskUpdatedMessage):
        status = message.status or message.patch.get("status")
        if status in TERMINAL_TASK_STATUSES:
            return message.task_id
    return None


@dataclass(slots=True)
class _Connection:
    """A kept connection to the engine, and what its pump has seen."""

    stack: AsyncExitStack
    session: KeptSession
    fingerprint: str
    last_end: float
    files: str = ""
    """The digest of the files the engine loaded, as the connection opened."""
    pump: "asyncio.Task[None] | None" = None
    watch: "asyncio.Task[None] | None" = None
    inbox: "asyncio.Queue[Message | None] | None" = None
    tasks: set[str] = field(default_factory=set[str])
    permission_mode: PermissionMode = "default"
    alive: bool = True
    draining: bool = False
    """Whether the pump discards messages until a result, the tail of a turn that
    was cancelled or that the engine began by itself."""


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
    require_init: bool = True
    """Whether the turn must see the engine's init before the model's output and
    before any tool call is admitted. Every Claude Code turn requires it."""


type PermissionGate = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Coroutine[Any, Any, PermissionResultAllow | PermissionResultDeny],
]


def permission_gate(
    config: LineConfig,
    own_tools: frozenset[str],
    sandbox: Mapping[str, object] | None = None,
    admits: Callable[[], bool] | None = None,
) -> PermissionGate:
    """A gate that applies ``config``'s policy and denies every other tool call,
    and every shell call that would leave the ``sandbox`` without its leave. With
    ``admits``, every call is denied while it returns ``False``, as it does on a kept
    connection outside a checked Dromon turn."""

    async def gate(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        del context
        if admits is not None and not admits():
            return PermissionResultDeny(
                message="Dromon has not started and checked a turn on this session"
            )
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
        kept_factory: KeptSessionFactory = _sdk_kept_session,
        observe: Observer = ignore,
        mark: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = KEEP_POLL_SECONDS,
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
        self.mark = mark
        """The process mark every process of this line inherits, if issued."""
        self._kept_factory = kept_factory
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._kept: _Connection | None = None
        self._turn: _Turn | None = None
        """The turn in progress, if any, whose init check the gate waits for."""
        self._notes: list[str] = []
        """Notes the next prompt carries, about what happened to the kept connection."""
        self._retired = False
        self._background: set[asyncio.Task[None]] = set()
        """Interrupts and closes begun outside a turn. asyncio holds only weak
        references to tasks, so each is kept here until it ends."""

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
        shells = sorted(SHELL_TOOLS & set(self.config.builtin_tools))
        if self.config.keep_session and shells and not sandboxed:
            raise EngineRefused(
                "a line that keeps its session and has "
                + ", ".join(shells)
                + " needs an enabled sandbox, since what it starts in the background"
                " outlives its turn"
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
            can_use_tool=permission_gate(
                self.config,
                self.own_tools,
                self.sandbox,
                self._admits,
            ),
            setting_sources=["project"] if self.config.project_settings else [],
            plugins=plugins,
            max_turns=settings.max_turns,
            effort=effort,
            cwd=str(self.cwd),
            resume=self.session_id,
            sandbox=cast(SandboxSettings, self.sandbox),
            settings=None if self.settings is None else json.dumps(self.settings),
            env={} if self.mark is None else {MARK_VARIABLE: self.mark},
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
        """Run one turn for ``prompt`` and return what happened. A check that
        refuses the turn also closes a kept connection, whose engine may already have
        loaded what the check refused."""
        try:
            check_environment(self._environment, self.driver.settings.strategy)
            options = self.options()
        except Exception:
            await self.close()
            raise
        if self.config.keep_session:
            return await self._kept_turn(prompt, options)
        self.driver.step(Event("turn_start"))
        turn = _Turn()
        result: ResultMessage | None = None
        self._turn = turn
        try:
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
        finally:
            self._turn = None
        if turn.refusal is not None:
            raise EngineRefused(turn.refusal)
        return self._report(result, turn)

    def _report(self, result: ResultMessage | None, turn: _Turn) -> TurnReport:
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
        self, session: AgentSession | KeptSession, message: Message, turn: _Turn
    ) -> None:
        if isinstance(message, SystemMessage) and message.subtype == "init":
            turn.capabilities = _capabilities(message.data)
            await self._check_init(session, turn.capabilities, turn)
        elif isinstance(message, AssistantMessage):
            if turn.require_init and turn.capabilities is None:
                if turn.refusal is None:
                    turn.refusal = (
                        "the engine did not report its tools and credential before"
                        " its output, so the turn could not be checked"
                    )
                    await session.interrupt()
                return
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
        self,
        session: AgentSession | KeptSession,
        capabilities: Capabilities,
        turn: _Turn,
    ) -> None:
        # A refusal stands, so a later init, even one that passes, cannot clear it.
        if turn.refusal is not None:
            return
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

    # A kept connection. Porting note: the pump and the watch are asyncio tasks
    # beside the turn, and the turn and the pump share the connection's fields
    # without a lock, which is sound only because asyncio switches tasks at awaits.

    def _admits(self) -> bool:
        """Whether a tool call may reach the gate's policy now: only inside a Dromon
        turn whose init check has passed."""
        turn = self._turn
        if turn is None or turn.refusal is not None:
            return False
        return not turn.require_init or turn.capabilities is not None

    def fingerprint(self, options: ClaudeAgentOptions) -> str:
        """A digest of everything a connection was opened with, and of the files the
        engine loaded, so a turn can tell whether a kept connection still matches.
        The session to resume and the gate are left out, since neither changes what
        the engine was given. Raises ``EngineRefused`` as ``tree_digest`` does."""
        servers: dict[str, object] = {}
        if isinstance(options.mcp_servers, dict):
            for name, server in options.mcp_servers.items():
                servers[name] = (
                    sorted(self.own_tools) if name == MCP_SERVER_NAME else server
                )
        plugins = [str(plugin["path"]) for plugin in options.plugins]
        document: dict[str, object] = {
            "model": options.model,
            "tools": options.tools,
            "servers": servers,
            "strict_mcp_config": options.strict_mcp_config,
            "setting_sources": options.setting_sources,
            "plugins": plugins,
            "plugin_files": [tree_digest(Path(path)) for path in plugins],
            "claude_dir": (
                tree_digest(self.cwd / ".claude")
                if self.config.project_settings
                else None
            ),
            "max_turns": options.max_turns,
            "effort": options.effort,
            "cwd": str(options.cwd),
            "sandbox": options.sandbox,
            "settings": options.settings,
            "env": options.env,
            "system_prompt": options.system_prompt,
        }
        text = json.dumps(document, sort_keys=True, default=str)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def loaded_files_digest(self) -> str:
        """A digest of the files the engine loads itself, every plugin tree and the
        working directory's ``.claude`` under ``project_settings``, which the watch
        compares between turns. Raises ``EngineRefused`` as ``tree_digest`` does."""
        trees = [resolve_plugin(path) for path in self.config.plugins]
        if self.config.project_settings:
            trees.append(self.cwd / ".claude")
        digests = [f"{tree}={tree_digest(tree)}" for tree in trees]
        return hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()

    def seconds_to_close(self) -> float | None:
        """How long the kept connection has before it closes, or ``None`` when there
        is none, it has ended, or a turn is in progress."""
        connection = self._kept
        if connection is None or not connection.alive or self._turn is not None:
            return None
        allowed = (
            self.config.keep_session_max_seconds
            if connection.tasks
            else self.config.keep_session_idle_seconds
        )
        return connection.last_end + allowed - self._clock()

    def kept_status(self) -> dict[str, object]:
        """Whether the engine's connection is kept, for ``line_status``."""
        connection = self._kept
        alive = connection is not None and connection.alive
        return {
            "keep_session": self.config.keep_session,
            "kept": alive,
            "seconds_to_close": self.seconds_to_close(),
            "background_tasks": len(connection.tasks) if connection and alive else 0,
        }

    async def expire(self) -> bool:
        """Close the kept connection if its deadline has passed, and say whether it
        was closed. The watch calls this, and tests may call it with their clock."""
        remaining = self.seconds_to_close()
        connection = self._kept
        if connection is None or remaining is None or remaining > 0:
            return False
        await self.close(
            "it reached keep_session_max_seconds with background tasks still running"
            if connection.tasks
            else "it waited keep_session_idle_seconds without a turn"
        )
        return True

    async def retire(self) -> None:
        """Close the kept connection for good, once any turn in progress has ended.
        The scheduler calls this when it discards the engine."""
        self._retired = True
        if self._turn is None:
            await self.close()

    async def close(self, reason: str | None = None) -> None:
        """Close the kept connection, if any, which ends the engine's process and its
        background tasks. With ``reason`` the next prompt says why."""
        connection = self._kept
        if connection is None:
            return
        self._kept = None
        if reason is not None:
            self._notes.append(closed_note(reason, len(connection.tasks)))
        current = asyncio.current_task()
        helpers = [
            task
            for task in (connection.pump, connection.watch)
            if task is not None and task is not current
        ]
        for task in helpers:
            task.cancel()
        await asyncio.gather(*helpers, return_exceptions=True)
        # The engine may already be gone, and closing must still forget it.
        with contextlib.suppress(Exception):
            await connection.stack.aclose()

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        """Run ``work`` as a task held in ``_background`` until it ends."""
        task = asyncio.get_running_loop().create_task(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _connect(
        self, options: ClaudeAgentOptions, fingerprint: str
    ) -> _Connection:
        # Digested before the engine starts, so a refusal leaves nothing open.
        files = self.loaded_files_digest()
        stack = AsyncExitStack()
        try:
            session = await stack.enter_async_context(self._kept_factory(options))
        except BaseException:
            await stack.aclose()
            raise
        connection = _Connection(stack, session, fingerprint, self._clock(), files)
        loop = asyncio.get_running_loop()
        connection.pump = loop.create_task(self._pump(connection))
        connection.watch = loop.create_task(self._watch(connection))
        self._kept = connection
        return connection

    async def _interrupt(self, connection: _Connection) -> bool:
        try:
            await asyncio.wait_for(
                connection.session.interrupt(), INTERRUPT_TIMEOUT_SECONDS
            )
        except Exception:  # noqa: BLE001
            # Any failure leaves the engine in an unknown state, which the caller
            # resolves by closing the connection.
            return False
        return True

    async def _pump(self, connection: _Connection) -> None:
        """Read every message of the connection. During a turn each goes to the
        turn. Between turns a background task's report is tracked, and the start of
        a turn the engine began by itself is interrupted and noted."""
        try:
            async for message in connection.session.receive_messages():
                ended = task_ended(message)
                if ended is not None:
                    connection.tasks.discard(ended)
                elif isinstance(message, TaskStartedMessage):
                    connection.tasks.add(message.task_id)
                inbox = connection.inbox
                if inbox is not None:
                    inbox.put_nowait(message)
                    if isinstance(message, ResultMessage):
                        connection.inbox = None
                    continue
                if connection.draining:
                    if isinstance(message, ResultMessage):
                        connection.draining = False
                    continue
                if not begins_turn(message):
                    continue
                connection.draining = not isinstance(message, ResultMessage)
                if SELF_STARTED_NOTE not in self._notes:
                    self._notes.append(SELF_STARTED_NOTE)
                if not await self._interrupt(connection):
                    return
        except Exception:  # noqa: BLE001, S110
            # The engine's process or its transport ended. The connection is
            # marked dead below, and the next turn resumes in a new process.
            pass
        finally:
            connection.alive = False
            if connection.inbox is not None:
                connection.inbox.put_nowait(None)

    async def _watch(self, connection: _Connection) -> None:
        """Close the kept connection when, between turns, a file its engine loaded
        changes, and at its deadline. An error is reported and the watch goes on,
        since a dead watch would leave both unenforced. Ends with the connection."""
        while connection is self._kept and connection.alive:
            await asyncio.sleep(self._poll_seconds)
            if connection is not self._kept:
                return
            try:
                if await self._files_changed(connection):
                    await self.close(FILES_CHANGED)
                    return
                await self.expire()
            except Exception as error:  # noqa: BLE001
                print(
                    f"dromond: the watch of a kept session failed and goes on: {error}",
                    file=sys.stderr,
                )

    async def _files_changed(self, connection: _Connection) -> bool:
        """Whether, between turns, the files the engine loaded no longer match the
        connection's digest, or can no longer be digested. A turn in progress has
        passed its checks, and the next turn compares them again."""
        if not (self.config.plugins or self.config.project_settings):
            return False
        if self._turn is not None:
            return False
        # On the event loop's thread, not in a worker. A Keleusma driver's virtual
        # machine may be dropped only on the thread that made it, and a collection
        # in a worker thread could drop one. The reads are bounded by tree_digest.
        try:
            current = self.loaded_files_digest()
        except EngineRefused:
            current = None
        return (
            connection is self._kept
            and self._turn is None
            and current != connection.files
        )

    def _settle(self, connection: _Connection) -> None:
        """After a cancelled turn, interrupt the engine in a task of its own, since
        the cancelled turn must not wait, and close the connection if that fails."""

        async def settle() -> None:
            if not await self._interrupt(connection) and connection is self._kept:
                await self.close()

        self._spawn(settle())

    async def _kept_turn(self, prompt: str, options: ClaudeAgentOptions) -> TurnReport:
        """Run a turn on the kept connection, opening one when there is none or when
        the one there no longer matches. Every check of a fresh turn has run by now
        in ``options``. On a reused connection the engine is asked to report its init
        again, and the turn is refused if it does not, before any tool call is
        admitted."""
        fingerprint = self.fingerprint(options)
        connection = self._kept
        if connection is not None:
            if not connection.alive:
                await self.close()
                self._notes.append(DIED_NOTE)
            elif connection.draining:
                await self.close("the engine had not finished its previous turn")
            elif connection.fingerprint != fingerprint:
                await self.close(
                    "its configuration, instructions, settings or loaded files changed"
                )
        reused = self._kept is not None
        self.driver.step(Event("turn_start"))
        connection = self._kept or await self._connect(options, fingerprint)
        turn = _Turn()
        if self._notes:
            prompt = "\n\n".join([*self._notes, prompt])
            self._notes.clear()
        inbox: asyncio.Queue[Message | None] = asyncio.Queue()
        connection.inbox = inbox
        self._turn = turn
        result: ResultMessage | None = None
        try:
            if reused:
                await connection.session.set_permission_mode(connection.permission_mode)
            await connection.session.query(prompt)
            # Bounded by the engine's result, which its max_turns caps, or by the
            # pump's end-of-stream marker.
            while True:
                message = await inbox.get()
                if message is None:
                    break
                if isinstance(message, ResultMessage):
                    result = message
                    self.driver.step(
                        Event("result", message.subtype, message.num_turns)
                    )
                    break
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    connection.permission_mode = permission_mode_of(message.data)
                await self._handle(connection.session, message, turn)
        except asyncio.CancelledError:
            if result is None and connection.alive:
                connection.inbox = None
                connection.draining = True
                self._settle(connection)
            raise
        except Exception:
            await self.close()
            raise
        finally:
            self._turn = None
            connection.inbox = None
            connection.last_end = self._clock()
            if self._retired:
                self._spawn(self.close())
        if result is None:
            await self.close()
            self._notes.append(DIED_NOTE)
            raise EngineRefused("the engine's process ended during the turn")
        if turn.refusal is not None:
            await self.close()
            raise EngineRefused(turn.refusal)
        return self._report(result, turn)
