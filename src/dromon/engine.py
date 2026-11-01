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
references such as ``${TOKEN}`` are replaced from names granted by the project file
here, at the start of each turn, so the stored configuration holds only the reference
and the secret never enters the engine process environment.

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
from decimal import Decimal
import hashlib
import importlib.metadata
import json
import math
import os
import re
import sys
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Protocol, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIJSONDecodeError,
    HookContext,
    HookInput,
    HookJSONOutput,
    HookMatcher,
    McpSdkServerConfig,
    McpServerConfig,
    Message,
    PermissionMode,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    RateLimitInfo,
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
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
)

from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk._internal.transport import Transport
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from claude_agent_sdk.types import SystemPromptPreset

from dromon import evidence, gitssh, launcher, redact
from dromon.gitssh import GitSshAccess
from dromon.hookpolicy import hook_policy_refusal
from dromon.linetemp import CACHE_VARIABLE, TEMP_VARIABLE, LineTemp
from dromon.imageaccess import claude_tools, image_refusal, view_only
from dromon.capabilities import CLAUDE_CODE, version_refusal
from dromon.activity import ActivityKind, Observer, ignore
from dromon.ceiling import (
    STOP_REASON,
    Ask,
    Ceiling,
    Choice,
    Settle,
    no_operator,
    nothing_to_settle,
)
from dromon.driver import TOOL_RESULT, ActionTag, Credential, Driver, Event, Strategy
from dromon.engine_environment import (
    CLAUDE_SHELL_VARIABLE,
    SHELL_VARIABLE,
    TOOL_SEARCH_PINNED,
    TOOL_SEARCH_VARIABLE,
    selected_claude_shell,
)
from dromon.lineconfig import DEFAULT_CONFIG, ConfigError, LineConfig
from dromon.model import format_due
from dromon.processes import MARK_VARIABLE
from dromon.sandbox import blocks_unsandboxed

ENGINE_NAME: Final = "claude-code"
"""The engine this adapter runs, as drivers name it."""

SIGN_IN_FAILURE: Final = (
    "Claude Code reports that it is not signed in. Sign it in, then release the line."
)
"""The hold reason when Claude Code reports that its user must run ``/login``."""


def reports_missing_sign_in(text: str | None) -> bool:
    """Whether Claude Code's report names its exact missing-login instruction."""
    if text is None:
        return False
    lowered = text.casefold()
    return "not logged in" in lowered and "please run /login" in lowered


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


class ConfinementBreach(EngineRefused):
    """A call escaped the confinement Dromon gave the line, as a ``Read`` that
    completed on a view-only line with no ViewImage hook call. **Round 15, review
    of e9adf91.** The scheduler holds a line whose turn ends this way whether or not
    the line mailed an operator, because the mail exemption covers a failed turn an
    operator may have been told about and not a call that escaped its confinement."""


class EnginePlanLimit(EngineRefused):
    """The subscription reached a plan limit and needs operator release.
    ``resets_at`` is the reset time the engine reported, in the model's
    ``YYYY-MM-DDTHH:MM:SSZ`` form, or ``None`` when it gave none in a known form.

    ``may_move_role`` says whether the shape it was read from may move a role.
    **Decided by the operator on 2026-10-27.** Only a shape observed live, or one the
    Agent SDK types and documents, may record its provider's budget as spent and so
    ground a failover. A limit read from inferred codes or free text holds the line
    and asks, and never moves a role. It defaults to false, so an adapter that does
    not say holds only, which is default deny."""

    def __init__(
        self,
        message: str,
        resets_at: str | None = None,
        *,
        may_move_role: bool = False,
        window: str | None = None,
    ) -> None:
        super().__init__(message)
        self.resets_at = resets_at
        self.may_move_role = may_move_role
        self.window = window


BILLING_ERROR: Final = "billing_error"
"""The error an assistant message carries when the account cannot pay for the call.
**Inference, read from the ``AssistantMessageError`` type of the installed Agent SDK
on 2026-10-27, not observed live.** Dromon counts it as the provider's budget
exhausted, since it is what an account with no usable credit reports."""

BILLING_LIMIT: Final = (
    "Claude reported a billing error, so its account has no usable credit"
)

OVERAGE_IN_USE: Final = frozenset({"allowed", "allowed_warning"})
"""Overage statuses under which a rejected window is still served from overage."""


def rejected_limit(info: RateLimitInfo) -> EnginePlanLimit | None:
    """The plan limit a ``rate_limit_event`` reports, or ``None`` while the account
    can still be served. **Inference, read from the ``RateLimitInfo`` type of the
    installed Agent SDK on 2026-10-27, not observed live.** Its ``status`` is
    ``rejected`` once a window's limit is hit, and its ``overage_status`` says
    whether overage serves the account past it. A reset time in seconds since the
    epoch is kept in the model's form, and one that is not a whole number or out of
    range is left out rather than guessed at."""
    if info.status != "rejected" or info.overage_status in OVERAGE_IN_USE:
        return None
    resets_at: str | None = None
    if type(info.resets_at) is int:
        try:
            resets_at = format_due(datetime.fromtimestamp(info.resets_at, UTC))
        except (OverflowError, OSError, ValueError):
            resets_at = None
    window = info.rate_limit_type or "unnamed"
    suffix = "" if resets_at is None else f", which resets at {resets_at}"
    return EnginePlanLimit(
        f"Claude subscription plan limit reached in its {window} window{suffix}",
        resets_at,
        may_move_role=True,
        window=window if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", window) else None,
    )


def remaining_window_budget(
    info: RateLimitInfo,
) -> tuple[int, str | None, str, str] | None:
    """The positive remaining percentage of the reported Claude rate-limit window.

    ``utilization`` is a fraction of this window, not an account-wide total. A
    rejected window is reported through ``EnginePlanLimit`` after a failed turn, and
    a zero snapshot would trigger failover even when overage is available, so this
    helper reports only a known positive remainder. Invalid or absent values are
    ignored rather than rounded into an asserted budget.
    """
    utilization = info.utilization
    if isinstance(utilization, bool) or not isinstance(utilization, int | float):
        return None
    try:
        finite = math.isfinite(utilization)
    except OverflowError:
        return None
    if (
        not finite
        or not 0 <= utilization < 1
        or info.status not in {"allowed", "allowed_warning"}
    ):
        return None
    remaining = 100 - math.ceil(Decimal(str(utilization)) * 100)
    if not 1 <= remaining <= 100:
        return None
    resets_at: str | None = None
    if type(info.resets_at) is int:
        try:
            resets_at = format_due(datetime.fromtimestamp(info.resets_at, UTC))
        except (OverflowError, OSError, ValueError):
            resets_at = None
    window = info.rate_limit_type or "unnamed"
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", window) is None:
        window = "unnamed"
    return (
        remaining,
        resets_at,
        f"Claude reported {remaining}% remaining in its {window} rate-limit window",
        window,
    )


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


@dataclass
class TurnResult(ResultMessage):
    """A result with the engine's per-turn usage and cost by model, its
    ``model_usage_since_last_result``, which the SDK's parser drops. **Fact, read
    from the engine's own field description on 2026-10-26.** It states the calls the
    process counted since its previous result, nothing is carried from one process to
    another, and the engine marks it internal, so it may be absent."""

    turn_model_usage: dict[str, Any] | None = None


def with_turn_usage(message: Message, data: Mapping[str, Any]) -> Message:
    """``message`` as the SDK parsed it from ``data``, as a ``TurnResult`` when it is
    a result whose raw form carries the per-turn usage."""
    if not isinstance(message, ResultMessage):
        return message
    raw = data.get("model_usage_since_last_result")
    if not isinstance(raw, dict):
        return message
    values = {f.name: getattr(message, f.name) for f in fields(ResultMessage)}
    return TurnResult(**values, turn_model_usage=cast(dict[str, Any], raw))


class _SdkClient:
    """``ClaudeSDKClient`` as both session protocols use it, reading each message
    as the client does, through the SDK's own parser, but keeping the per-turn
    usage of a result. Porting hazard: it reads the client's internal query, so an
    SDK upgrade must check that ``_query.receive_messages`` still yields the raw
    messages that ``ClaudeSDKClient.receive_messages`` parses."""

    def __init__(
        self, options: ClaudeAgentOptions, transport: Transport | None = None
    ) -> None:
        recording = evidence.RECORDING.get()
        if transport is None and recording is not None:
            options, transport = _recorded(options, recording)
        self._client = ClaudeSDKClient(options=options, transport=transport)

    async def __aenter__(self) -> "_SdkClient":
        # A failed connect has already disconnected, which ends the engine's
        # process, as ClaudeSDKClient.connect does in Agent SDK 0.2.174.
        await self._client.connect()
        return self

    async def __aexit__(self, *details: object) -> None:
        await self._client.disconnect()

    async def query(self, prompt: str) -> None:
        await self._client.query(prompt)

    async def interrupt(self) -> None:
        await self._client.interrupt()

    async def set_permission_mode(self, mode: PermissionMode) -> None:
        await self._client.set_permission_mode(mode)

    async def receive_messages(self) -> AsyncIterator[Message]:
        query = self._client._query  # pyright: ignore[reportPrivateUsage]
        if query is None:
            raise EngineRefused("the engine is not connected")
        async for data in cast(AsyncIterator[dict[str, Any]], query.receive_messages()):
            message = parse_message(data)
            if message is not None:
                yield with_turn_usage(message, data)

    async def receive_response(self) -> AsyncIterator[Message]:
        async for message in self.receive_messages():
            yield message
            if isinstance(message, ResultMessage):
                return


async def _no_prompt() -> AsyncIterator[dict[str, Any]]:
    """The prompt stream of a streamed connection, which never yields, as the SDK's
    own connect gives its transport."""
    return
    yield {}


class RecordedTransport(Transport):
    """A transport that records each frame written to and read from ``inner``.
    **Row 7 of the T3 Code review.** Porting hazard: ``Transport`` is the Agent SDK's
    internal interface, which an SDK upgrade must check."""

    def __init__(self, inner: Transport, recording: evidence.Recording) -> None:
        self._inner = inner
        self._recording = recording

    async def connect(self) -> None:
        await self._inner.connect()

    async def write(self, data: str) -> None:
        for frame in _frames_of(data):
            self._recording.frame(evidence.OUT, frame)
        await self._inner.write(data)

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._read()

    async def _read(self) -> AsyncIterator[dict[str, Any]]:
        async for message in self._inner.read_messages():
            self._recording.frame(evidence.IN, message)
            yield message

    async def close(self) -> None:
        await self._inner.close()

    def is_ready(self) -> bool:
        return self._inner.is_ready()

    async def end_input(self) -> None:
        await self._inner.end_input()


def _frames_of(data: str) -> list[dict[str, Any]]:
    """The JSON objects among the lines the SDK writes."""
    found: list[dict[str, Any]] = []
    for line in data.splitlines():
        with contextlib.suppress(ValueError):
            document: object = json.loads(line)
            if isinstance(document, dict):
                found.append(cast(dict[str, Any], document))
    return found


class ReplayTransport(Transport):
    """The engine's side of a recorded Claude Code exchange, for a test that runs the
    real adapter and the real Agent SDK client against it. The SDK names each control
    request it sends at random, so the answer to the recording's ``n``-th control
    request is renamed for the ``n``-th the SDK sent."""

    def __init__(self, replay: evidence.Replay) -> None:
        self._replay = replay
        self._recorded = [
            str(frame["request_id"])
            for frame in replay.recorded_out
            if frame.get("type") == "control_request" and "request_id" in frame
        ]
        self._live: list[str] = []
        self._ready = False

    async def connect(self) -> None:
        self._ready = True

    async def write(self, data: str) -> None:
        for frame in _frames_of(data):
            if frame.get("type") == "control_request" and "request_id" in frame:
                self._live.append(str(frame["request_id"]))
            self._replay.send(frame)

    def read_messages(self) -> AsyncIterator[dict[str, Any]]:
        return self._read()

    async def _read(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            frame = await self._replay.receive()
            if frame is None:
                return
            yield self._renamed(frame)

    def _renamed(self, frame: dict[str, Any]) -> dict[str, Any]:
        response = frame.get("response")
        if frame.get("type") != "control_response" or not isinstance(response, dict):
            return frame
        answer = cast(dict[str, Any], response)
        recorded = answer.get("request_id")
        if recorded not in self._recorded:
            return frame
        index = self._recorded.index(str(recorded))
        if index >= len(self._live):
            return frame
        return {**frame, "response": {**answer, "request_id": self._live[index]}}

    async def close(self) -> None:
        self._ready = False

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        return


def transport_session(make: Callable[[], Transport]) -> SessionFactory:
    """A session factory that runs the real Agent SDK client over the transport
    ``make`` gives, for a test or a replay."""

    def factory(
        options: ClaudeAgentOptions,
    ) -> AbstractAsyncContextManager[AgentSession]:
        return _SdkClient(options, transport=make())

    return factory


def replay_session(replay: evidence.Replay) -> SessionFactory:
    """A session factory that runs the real Agent SDK client against ``replay``."""
    return transport_session(lambda: ReplayTransport(replay))


def _recorded(
    options: ClaudeAgentOptions, recording: evidence.Recording
) -> tuple[ClaudeAgentOptions, Transport]:
    """``options`` with the engine's standard error kept, when the recording asks,
    and a transport that records. The transport is given the options the SDK's own
    connect would give it, with permission prompts over the control protocol when a
    ``can_use_tool`` callback is set, as ``_configure_can_use_tool`` does in Agent SDK
    0.2.174, since a transport given to the client is used as it is."""
    if recording.wants_stderr:

        def keep(line: str) -> None:
            recording.stderr((line + "\n").encode())

        options = replace(options, stderr=keep)
    transported = (
        replace(options, permission_prompt_tool_name="stdio")
        if options.can_use_tool is not None
        else options
    )
    return options, RecordedTransport(
        SubprocessCLITransport(prompt=_no_prompt(), options=transported), recording
    )


def _sdk_session(
    options: ClaudeAgentOptions,
) -> AbstractAsyncContextManager[AgentSession]:
    return _SdkClient(options)


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
    return _SdkClient(options)


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

HALT_REASON: Final = (
    "an operator stopped the line while its engine still ran background work"
)
"""**Row 1 of the T3 Code review.** Why an operator's stop closed a kept connection
whose interrupt settled while the engine still ran work."""


def flag_settings(
    settings: Mapping[str, object] | None, ask: Sequence[str] = ()
) -> dict[str, object]:
    """The settings Dromon passes Claude Code with ``--settings``, its flag
    settings, with the pinned tool search in their env block. **Review F9.**

    **Read from the bundled Claude Code 2.1.310 on 2026-10-26, unverified by a live
    probe.** ``setting_sources=[]`` reaches the engine as ``--setting-sources=``,
    which loads none of the user, project and local settings files, so no env block
    of theirs applies. Claude Code then still copies three env blocks over its
    process environment, in this order. The ``env`` key of its global configuration
    file, ``~/.claude.json``, which no setting source governs. The flag settings.
    The managed policy settings. The process environment alone would lose to the
    first, so the pin is also set here, in the second, which comes after it. The
    third is an administrator's, and Dromon does not override it."""
    merged = dict(settings or {})
    given = merged.get("env")
    block: dict[str, object] = (
        dict(cast(Mapping[str, object], given)) if isinstance(given, Mapping) else {}
    )
    block[TOOL_SEARCH_VARIABLE] = TOOL_SEARCH_PINNED
    merged["env"] = block
    if ask:
        merged["permissions"] = _with_ask_rules(merged.get("permissions"), ask)
    return merged


def _with_ask_rules(given: object, ask: Sequence[str]) -> dict[str, object]:
    """``given``, a settings ``permissions`` block, with each of ``ask`` among its
    ``ask`` rules once. **Round 14 review, item 1.** An ask rule makes the engine
    consult the permission callback, so the gate runs for a ``Read`` that the engine
    would allow inside its working directory without asking. **Hypothesis, to be
    shown live.** An ask rule outranks the engine's allowance for reads in its
    working directory. A block that is not a mapping is replaced, since it could not
    have been valid."""
    block: dict[str, object] = (
        dict(cast(Mapping[str, object], given)) if isinstance(given, Mapping) else {}
    )
    rules = block.get("ask")
    kept = (
        [rule for rule in cast(list[object], rules) if isinstance(rule, str)]
        if isinstance(rules, list)
        else []
    )
    kept.extend(rule for rule in ask if rule not in kept)
    block["ask"] = kept
    return block


HANDLED_SYSTEM_SUBTYPES: Final = frozenset(
    {
        "init",
        "compact_boundary",
        "task_started",
        "task_progress",
        "task_notification",
        "task_updated",
        "background_tasks_changed",
        "hook_started",
        "hook_response",
        "session_state_changed",
        "status",
        "api_retry",
        "thinking_tokens",
    }
)
"""System message subtypes Dromon handles, or knows to carry nothing it must act on.
``thinking_tokens`` was seen live on 2026-10-27 and read from the bundled Claude Code
the same day. It is a progress frame with ``estimated_tokens`` and
``estimated_tokens_delta`` while the model thinks, which the engine sends many times a
turn, and the turn's usage comes from its result, so Dromon takes nothing from it.
**Row 5 of the T3 Code review.** Every other subtype is surfaced in the line's activity,
once per subtype and engine, and is never passed over silently. **Fact, read from the
bundled Claude Code 2.1.310 on 2026-10-26.** The engine can emit about forty
subtypes, most of them for its own interface."""

FALLBACK_SUBTYPES: Final = frozenset(
    {"model_fallback", "model_refusal_fallback", "model_consent_fallback"}
)
"""System message subtypes with which the engine reports that it went on with another
model than the one its init reported. **Review F16, read from the message schemas in
the bundled Claude Code 2.1.310 on 2026-10-26, unverified live.** Each carries
``original_model`` and ``fallback_model``. ``model_fallback`` is for one turn, after
the primary model failed. ``model_refusal_fallback`` retries a refused response.
``model_consent_fallback`` moves the session after a consent prompt, such as one for
usage credits, was declined or could not be honoured. A turn is refused and
interrupted only when ``fallback_model`` is not the model its driver was written for,
compared exactly as the driver compares the init's model, and a missing one counts as
another. A consent fallback is also put to an operator as a question, since the
consent is a human's decision."""

CONSENT_FALLBACK: Final = "model_consent_fallback"

MAX_FALLBACK_FIELD_CHARACTERS: Final = 80


def no_consent_question(text: str) -> None:
    """Where a consent fallback goes when no operator can be asked."""
    del text


def fallback_text(field: object) -> str:
    """A fallback message's model or choice, from engine output, cut to a bound."""
    if not isinstance(field, str) or not field:
        return "an unnamed one"
    return field[:MAX_FALLBACK_FIELD_CHARACTERS]


def consent_question(data: Mapping[str, object], bound_to: str, stopped: bool) -> str:
    """The operator question a consent fallback raises, after the line's name."""
    outcome = (
        f"Dromon stopped the turn, since the line is bound to {bound_to}."
        if stopped
        else f"The turn went on, since {bound_to} is the model the line is bound to."
    )
    return (
        f"asked for consent to use {fallback_text(data.get('original_model'))}, and"
        f" on the answer {fallback_text(data.get('choice'))} moved its session to"
        f" {fallback_text(data.get('fallback_model'))}. {outcome} The consent is"
        " yours to give in Claude Code itself, or the line can be rebound. An answer"
        " here reaches the line as mail."
    )


MAX_SURFACED_SUBTYPES: Final = 64
MAX_SUBTYPE_CHARACTERS: Final = 64

HANDSHAKE_KIND: Final = "handshake"
"""A result with no turn behind it, which is not a turn: one between turns, or a
resumed session's handoff, as ``is_handoff`` says."""

HUMAN_DECISION_TOOLS: Final = frozenset({"AskUserQuestion", "ExitPlanMode"})
"""Built-in tools with which the engine asks the person at its prompt to decide.
**Row 5 of the T3 Code review.** A Dromon line has nobody there, so the gate refuses
them whatever the configuration says, with a refusal that names the durable way."""

HUMAN_DECISION_REFUSAL: Final = (
    "Nobody is at this line's prompt to decide. Ask an operator with ask_operator,"
    " whose answer arrives as mail in a later turn."
)

MAX_FINGERPRINT_FILES: Final = 4096
MAX_FINGERPRINT_FILE_BYTES: Final = 1024 * 1024

DIED_NOTE: Final = (
    "Dromon note: your engine's process ended since your last turn, so this turn"
    " resumes your session in a new process, and every background task of the old"
    " one was lost. Check for partial output before starting again."
)

MAX_MESSAGE_CHARACTERS: Final = 32 * 1024 * 1024
"""The longest single message, in characters, that the Agent SDK reads from Claude
Code, passed as ``ClaudeAgentOptions.max_buffer_size``. The SDK's default of 1 MiB
ended a line's turn, inferred to be on a tool result that carried a file of about
1 MB, and again on a two-line Edit of a 2.5 MB file. The SDK counts the characters
of one line of the engine's output, not its bytes, despite its message.

Claude Code 2.1.310, as bundled with Agent SDK 0.2.174, was read to show why. Its
user message in the stream carries ``tool_use_result``, which its schema describes
as the tool's full output object and not the text the model is sent. The output of
Edit holds ``originalFile``, the file's contents before the edit, as well as the
patch, and the output of a Write that replaces a file holds both the old and the new
content. Its code that empties those fields was found only where it stores copies of
results on disk, not on the path that writes the stream, so an Edit's message is
inferred to be at least as long as the file it edits, whatever the size of the edit.

The bound is 32 MiB, over ten times the largest file a line was seen to edit, which
leaves room for a result that carries a file twice and for the escapes of its JSON
form. Memory is the cost. While one message is read the SDK holds its chunks, the
joined text, the split line and then the parsed object, which is inferred to be
about four copies, so a message at the bound holds some 130 MB of ASCII text, and up
to four times that for text whose characters need four bytes each, until the
message has been handled. Each line reads its own engine, so
the worst case grows with the number of lines whose engines send such a message at
once. A larger bound only moves the failure to a larger file, and a message past
this one ends the turn with ``MESSAGE_TOO_LONG``."""

MESSAGE_TOO_LONG: Final = (
    "the engine sent one message longer than Dromon's limit of"
    f" {MAX_MESSAGE_CHARACTERS} characters, MAX_MESSAGE_CHARACTERS, so the Agent SDK"
    " stopped reading the engine and the turn ended. The likely cause is a tool result"
    " that carries a large file, such as an Edit, a Write or a Read of a file of"
    " several megabytes, since Claude Code's Edit result carries the whole original"
    " file however small the edit."
    " Work on a smaller file, or change a large one with a command that does not"
    " print it."
)
"""Why a turn ended when the SDK refused an engine message over
``MAX_MESSAGE_CHARACTERS``, where before the turn said only that the engine's
process ended."""

TOO_LONG_PREFIX: Final = "JSON message exceeded maximum buffer size"
"""How Agent SDK 0.2.174 begins the error it raises for a message over its buffer
size. Porting hazard: the SDK gives that error no type of its own, so an SDK upgrade
must check that this text still matches, which a test derives from the SDK."""


def message_too_long(error: BaseException) -> bool:
    """Whether ``error`` is the SDK's refusal of an engine message longer than its
    buffer size, as opposed to output it could not decode."""
    return isinstance(error, CLIJSONDecodeError) and error.line.startswith(
        TOO_LONG_PREFIX
    )


SELF_STARTED_NOTE: Final = (
    "Dromon note: between your turns the engine began a turn by itself, most likely"
    " because a background task ended. Only Dromon starts turns, so it was"
    " interrupted, and its tool calls were denied. Read the task's output file to"
    " see how it ended."
)


def closed_note(reason: str, tasks: int, left: bool = False) -> str:
    """The note a line's next prompt carries after Dromon closed its kept
    connection for ``reason`` with ``tasks`` background tasks reported running.
    ``left`` is whether the line's stop policy is ``leave_background``, under which
    Dromon does not end them."""
    if not tasks:
        lost = ""
    elif left:
        lost = (
            f" The {tasks} background tasks the engine reported running were not ended"
            " by Dromon, by the leave_background stop policy. Any still running are"
            " in your line status, and their output files may still grow."
        )
    else:
        lost = (
            f" The {tasks} background tasks the engine reported running were stopped"
            " with it."
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


def is_handshake(message: Message) -> bool:
    """Whether ``message``, arriving between Dromon's turns and outside the tail of
    one, is a result with no turn behind it, as the engine's answer to a resume.
    **Row 5 of the T3 Code review.** A second implementation saw a resumed session
    send a result with ``num_turns`` of zero, which it had taken for a finished turn.
    Whether Claude Code 2.1.310 sends one to Dromon is not verified."""
    return isinstance(message, ResultMessage) and message.num_turns == 0


HANDOFF_RESENDS: Final = 1
"""How many times a turn sends its prompt again after a handoff result."""

HANDOFF_NOTICE: Final = (
    "the resumed engine session ended the turn it carried over from its last process"
    " without running the prompt, so Dromon sent the prompt again"
)

HANDOFF_REFUSAL: Final = (
    "the engine ended the turn again without running its prompt, after the prompt was"
    " sent a second time"
)


def is_handoff(result: ResultMessage) -> bool:
    """Whether ``result``, arriving inside a Dromon turn before any output of the
    model, ended the turn without running its prompt.

    **Review N5.** Any success with ``num_turns`` of zero before the model's output is
    one, whatever its text, since a text alone shows no prompt was run. The text is
    shown as a notice, not taken as the turn's answer. A slash command answered by
    the engine itself would read so too, and Dromon sends none as a prompt.

    **Fact, read from the bundled Claude Code 2.1.310 on 2026-10-26.** A session
    resumed with a turn its last process left unfinished settles that turn on its
    first prompt. Where the carried turn's history is appended without running, or
    there is nothing to run, the engine reports a result of subtype ``success`` with
    ``num_turns`` of zero, an empty ``result`` and no model call, and returns without
    reading the prompt. **Observed live on 2026-10-26.** The first turn after a
    ``SIGKILL`` of the daemon ended so in about 2 seconds, and was recorded as a
    success with no output and no cost. A result that reports an error is not a
    handoff, and stays the turn's honest outcome."""
    return result.num_turns == 0 and result.subtype == "success"


MAX_HANDOFF_TEXT_CHARACTERS: Final = 200
"""How much of a handoff result's text its notice quotes."""


def handoff_text_notice(text: str) -> str:
    """The notice for a handoff result's text, which is engine output, so it is
    quoted and cut."""
    quoted = text[:MAX_HANDOFF_TEXT_CHARACTERS]
    return f"the engine's result for the turn it carried over said {quoted!r}"


TASKS_CHANGED: Final = "background_tasks_changed"
"""The system message subtype with which the engine reports its whole set of live
background tasks. **Fact, read from the message schema in the bundled Claude Code
2.1.310 on 2026-10-26.** It carries ``tasks``, a list of objects each with a
``task_id``, a ``task_type`` and a ``description``, and is sent whenever the set
changes, by a start, an end, a kill or a foreground agent moved to the background. Its
receiver is to replace its own set with it. **Observed live on 2026-10-26** as an
unhandled subtype. The Agent SDK does not narrow its own set by it, since during a turn
a foreground agent that is still running is not in it. Dromon therefore adds from it at
any time, and removes by it only between turns, when every live task is a background
one."""

MAX_LISTED_TASKS: Final = 256
"""The most entries of one task set Dromon reads. A longer set is not used to remove
any task, since its tail is unread."""

STOP_TOOLS: Final = frozenset({"TaskStop", "KillShell", "KillBash"})
"""The engine's tool that stops a background task, under its name and its aliases."""


def task_status(message: Message) -> str:
    """The final status a task's end reports, such as ``completed``, ``failed``,
    ``stopped`` or ``killed``, or ``ended`` when none is given."""
    if isinstance(message, TaskNotificationMessage):
        return str(message.status)
    if isinstance(message, TaskUpdatedMessage):
        status = message.status or message.patch.get("status")
        return status if isinstance(status, str) else "ended"
    return "ended"


def task_ended(message: Message) -> str | None:
    """The background task ``message`` reports ended, or ``None``."""
    if isinstance(message, TaskNotificationMessage):
        return message.task_id
    if isinstance(message, TaskUpdatedMessage):
        status = message.status or message.patch.get("status")
        if status in TERMINAL_TASK_STATUSES:
            return message.task_id
    return None


MAX_TRACKED_COMMANDS: Final = 256
"""How many tool calls' commands a kept connection remembers, to name the background
task each starts, oldest forgotten first."""

MAX_COMMAND_CHARACTERS: Final = 200


@dataclass(frozen=True, slots=True)
class BackgroundEnd:
    """A background task the engine reported finished."""

    task_id: str
    command: str
    """The command its tool call ran, or the engine's description of the task."""
    status: str
    exit_code: int | None = None
    """The exit code the engine's summary gives, when it gives one."""
    output_file: str | None = None
    """The file the engine wrote the task's output to, as it named it."""
    self_started: bool = False
    """Whether the end also made the engine begin a turn by itself."""
    output: str | None = None
    """The task's output as the engine reported it inline, where it does, as Codex
    does for a completed command execution."""
    self_started_admitted: bool = False
    """Whether Dromon admitted that engine-started turn."""
    during_turn: bool = False
    """Whether the engine reported the end while a Dromon turn was running."""
    stopped_by_line: bool = False
    """Whether the line asked the engine to stop this task."""


SELF_START_GRACE_SECONDS: Final = 2.0
"""How long a task's end waits for the turn the engine may begin by itself on it,
so that one note reports both. The engine begins that turn at once, in the
observed case."""


async def self_start_grace() -> None:
    """Wait out ``SELF_START_GRACE_SECONDS``. A test replaces it with a wait it
    releases, so that whether an end is noted before or after the grace does not
    depend on how busy the machine is."""
    await asyncio.sleep(SELF_START_GRACE_SECONDS)


EXIT_CODE: Final = re.compile(r"\bexit code (-?\d{1,5})\)?\.?\s*$")
"""The exit code at the end of the engine's summary of a finished shell task.
**Facts, observed live on 2026-10-26.** The summary is the only place the engine's
messages give it, as ``completed (exit code 0)`` in one case and as ``Background
command "..." failed with exit code 1`` in round 5, which the earlier pattern missed.
Only the end of the summary is read, since the quoted description before it is the
line's own text and could carry a phrase of the same form."""


USAGE_KEYS: Final = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
"""The token counts a turn's report carries, as the engine's result names them.
**Fact, read from the engine's own field description on 2026-10-26.** A result's
``usage`` covers the main agent loop only and is per turn, while its cost covers
subagents and helper calls too, so the tokens explain most, not all, of the cost."""


COMPACT_COMMAND: Final = "/compact"
"""What asks the engine to compact its session, sent as a prompt. **Fact, read from
the bundled Claude Code 2.1.310 on 2026-10-26.** ``compact`` is a built-in command
that runs outside an interactive session, unless ``DISABLE_COMPACT`` is set, and the
engine reports it with a ``compact_boundary`` system message whose
``compact_metadata`` gives ``trigger``, ``manual`` for a typed command,
``pre_tokens`` and, optionally, ``post_tokens``."""

MAX_CALLS: Final = 50
"""How many engine calls one turn's report lists."""

CALL_KEYS: Final = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def record_call(
    calls: dict[str, dict[str, int | bool]], message: AssistantMessage
) -> dict[str, int | bool] | None:
    """Note one engine call's token counts, once per message identifier.
    **Fact, read from Agent SDK 0.2.174.** An assistant message carries the usage of
    the engine call that produced it, and its message identifier."""
    if message.message_id is None or message.usage is None:
        return None
    if message.message_id in calls:
        return None
    counts: dict[str, int | bool] = {
        key: value
        for key in CALL_KEYS
        if type(value := message.usage.get(key)) is int and value >= 0
    }
    counts["subagent"] = message.parent_tool_use_id is not None
    calls[message.message_id] = counts
    while len(calls) > MAX_CALLS:
        del calls[next(iter(calls))]
    return counts


def context_of(call: Mapping[str, int | bool] | None) -> int | None:
    """The context the next call will re-read, from a main-loop call: what it read,
    fresh and cached, and what it wrote."""
    if call is None:
        return None
    total = sum(int(call[key]) for key in CALL_KEYS if key in call)
    return total or None


def compaction_of(metadata: object) -> dict[str, int | str | None]:
    """A compaction's trigger, ``manual`` or ``auto``, and the context before and
    after it, as its metadata reports them."""
    found: dict[str, int | str | None] = {
        "trigger": None,
        "before_tokens": None,
        "after_tokens": None,
    }
    if isinstance(metadata, dict):
        fields_ = cast(dict[str, object], metadata)
        trigger = fields_.get("trigger")
        if trigger in ("manual", "auto"):
            found["trigger"] = cast(str, trigger)
        for key, name in (
            ("pre_tokens", "before_tokens"),
            ("post_tokens", "after_tokens"),
        ):
            value = fields_.get(key)
            if type(value) is int and value >= 0:
                found[name] = value
    return found


COMPACTION_REQUESTED: Final = "at your request"
"""Why a turn began with a compaction, when the line asked for it."""


def no_context_line() -> str | None:
    """The default for an engine whose compaction nobody describes afterwards."""
    return None


@dataclass(frozen=True, slots=True)
class CompactionRequest:
    """A compaction asked of an engine for its next turn: why, for its note, and the
    context warning as it will stand after it."""

    reason: str = COMPACTION_REQUESTED
    context_line: Callable[[], str | None] = no_context_line


def compaction_note(
    compaction: Mapping[str, int | str | None],
    reason: str = COMPACTION_REQUESTED,
    context_line: str | None = None,
    reported: bool | None = None,
) -> str:
    """The note a turn that began with a compaction opens with, for ``reason``, and
    then ``context_line``, the prompt's context warning computed after the
    compaction, where there still is one. ``reported`` says whether the engine
    reported the compaction. When it is ``None`` a report is inferred from a size,
    as Claude Code's boundary gives one, while Codex says so apart, since its report
    carries no size."""
    before, after = compaction.get("before_tokens"), compaction.get("after_tokens")
    if reported is None:
        reported = before is not None or after is not None
    if not reported:
        note = (
            "Dromon note: your session was to be compacted at the start of this turn"
            f" {reason}, but the engine reported no compaction, so it may not have"
            " happened."
        )
    else:
        shown_before = "not reported" if before is None else f"{before} tokens"
        shown_after = "not reported" if after is None else f"{after} tokens"
        note = (
            f"Dromon note: your session was compacted at the start of this turn"
            f" {reason}. Context before: {shown_before}. After: {shown_after}."
        )
    return note if context_line is None else f"{note} {context_line}"


def add_costs(first: float | None, second: float | None) -> float | None:
    if first is None:
        return second
    return first if second is None else first + second


def add_usage(
    first: Mapping[str, int] | None, second: Mapping[str, int] | None
) -> dict[str, int] | None:
    if first is None:
        return None if second is None else dict(second)
    merged = dict(first)
    for key, value in (second or {}).items():
        merged[key] = merged.get(key, 0) + value
    return merged


async def _drain(
    inbox: "asyncio.Queue[Message | None]",
) -> AsyncIterator[Message | None]:
    """A kept turn's messages, until the pump's end-of-stream marker."""
    while True:
        message = await inbox.get()
        yield message
        if message is None or isinstance(message, ResultMessage):
            return


PER_TURN_SOURCE: Final = "per_turn"
"""The cost and tokens are the engine's own per-turn figures, by model, which cover
its subagents and helper calls too."""

DIFFERENCE_SOURCE: Final = "difference"
"""The cost is the difference of running totals, and the tokens are the main loop's
per-turn ``usage``, which leaves out subagents and helper calls."""

MODEL_USAGE_KEYS: Final = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheCreationInputTokens": "cache_creation_input_tokens",
    "cacheReadInputTokens": "cache_read_input_tokens",
}


def turn_usage_of(result: ResultMessage) -> tuple[float, dict[str, int]] | None:
    """The turn's cost and tokens summed over the models of the engine's per-turn
    usage, or ``None`` when the result does not carry it or any entry is malformed,
    so that a malformed report falls back rather than undercounts."""
    if not isinstance(result, TurnResult) or not result.turn_model_usage:
        return None
    cost = 0.0
    tokens: dict[str, int] = {}
    for entry in result.turn_model_usage.values():
        if not isinstance(entry, dict):
            return None
        fields_ = cast(dict[str, object], entry)
        spent = fields_.get("costUSD")
        if isinstance(spent, bool) or not isinstance(spent, int | float):
            return None
        if not 0 <= spent < float("inf"):
            return None
        cost += spent
        for raw, name in MODEL_USAGE_KEYS.items():
            count = fields_.get(raw)
            if type(count) is int and count >= 0:
                tokens[name] = tokens.get(name, 0) + count
    return cost, tokens


def usage_of(usage: Mapping[str, Any] | None) -> dict[str, int] | None:
    """The whole-number token counts of a result's ``usage``, or ``None``."""
    if usage is None:
        return None
    found = {
        key: value
        for key in USAGE_KEYS
        if type(value := usage.get(key)) is int and value >= 0
    }
    return found or None


INITIALIZE_TIMEOUT: Final = "Control request timeout: initialize"
"""What the SDK raises when the engine does not answer its initialize request in
time. **Fact, read from Agent SDK 0.2.174 on 2026-10-26.** It is a plain
``Exception`` with this text, raised from a ``TimeoutError``, so the text is the only
way to tell it apart."""


def initialize_timed_out(error: BaseException) -> bool:
    """Whether ``error`` is the SDK's timeout of the engine's initialize request."""
    return str(error) == INITIALIZE_TIMEOUT


def end_details(message: Message) -> tuple[int | None, str | None]:
    """The exit code and output file a task's end reports, where it reports them."""
    if not isinstance(message, TaskNotificationMessage):
        return None, None
    found = EXIT_CODE.search(message.summary or "")
    output = message.output_file or None
    return (int(found.group(1)) if found else None), output


def ignore_compaction(compaction: Mapping[str, int | str | None]) -> None:
    """The default for an engine whose compactions nobody follows."""
    del compaction


def ignore_estimate(tokens: int) -> None:
    """The default for an engine whose estimates of a compaction's size after nobody
    follows."""
    del tokens


SELF_STARTED_KIND: Final = "self_started"
"""A turn the engine began by itself between Dromon's turns, which Dromon
interrupted."""

INTERRUPTED_KIND: Final = "interrupted"
"""A Dromon turn that was cancelled, whose result arrived after it."""

type SpentBetween = Callable[[str, float | None, Mapping[str, int] | None], object]
"""Where an engine reports what a turn outside Dromon's own cost: its kind, its cost
and its tokens, where the engine reports them."""

type BudgetReported = Callable[[int, str | None, str, str], object]
"""Where an engine reports a positive remaining percentage for one provider window."""


def ignore_budget_report(
    remaining_percent: int, resets_at: str | None, note: str, window: str
) -> None:
    """The default for an engine whose budget snapshots nobody follows."""
    del remaining_percent, resets_at, note, window


type SelfStartedFinish = Callable[[TurnReport | None, Exception | None], object]
"""Where a Dromon scheduler settles a turn the engine began after a task ended."""

type BeginSelfStarted = Callable[[tuple[BackgroundEnd, ...]], SelfStartedFinish | None]
"""Whether the scheduler can admit an engine-started turn, and how it is settled."""


def refuse_self_started(
    ends: tuple[BackgroundEnd, ...],
) -> SelfStartedFinish | None:
    """The default. Engines without a scheduler keep refusing their own turns."""
    del ends
    return None


def ignore_spent(
    kind: str, cost: float | None, usage: Mapping[str, int] | None
) -> None:
    """The default for an engine whose costs between turns nobody follows."""
    del kind, cost, usage


def ignore_end(end: BackgroundEnd) -> None:
    """The default for an engine whose task ends nobody follows."""
    del end


COST_TOLERANCE_USD: Final = 0.000001
"""A recorded total is kept to the millionth of a dollar, so a total within this of
the mark has not fallen."""

type CostMark = tuple[str, float]
"""An engine session and the running cost total its last result reported."""


def turn_cost(total: float | None, session: str, mark: CostMark | None) -> float | None:
    """One turn's cost, from the running ``total`` its result reported for
    ``session`` and the ``mark`` left by the turn before, or ``None`` when the engine
    reported none.

    **Facts.** On a kept connection the total covers every turn since the connection
    opened, observed live on 2026-10-26. On resume the engine restores the session's
    total from a cost-state entry it writes into the session's transcript, and starts
    from zero when there is none, read from its bundled code on 2026-10-26 and
    consistent with what was observed live. So without a kept connection, every turn's
    total also covers the session's earlier turns.

    The difference from the mark is taken only when the mark is for the same session
    and the total has not fallen. A different session, or a fall, means the count
    restarted, and then the whole total is taken, so a restart is never subtracted
    from. **Residual.** A restart that the engine does not show, by a resume that
    finds no cost-state entry, followed by a turn costing more than the mark, would be
    undercounted by the mark."""
    if total is None:
        return None
    if mark is None or mark[0] != session or total < mark[1] - COST_TOLERANCE_USD:
        return total
    return max(0.0, total - mark[1])


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
    capabilities: "Capabilities | None" = None
    """The engine state checked on its last ordinary Dromon turn."""
    self_started_turn: "_Turn | None" = None
    """A turn the kept engine began after a task ended, admitted by Dromon."""
    self_started_finish: SelfStartedFinish | None = None
    """Where Dromon records the outcome of its admitted self-started turn."""
    tasks: dict[str, str] = field(default_factory=dict[str, str])
    """Running background tasks, by identifier, with their commands."""
    commands: dict[str, str] = field(default_factory=dict[str, str])
    """The command of each recent tool call, by tool use identifier, which a task's
    start names. Bounded by ``MAX_TRACKED_COMMANDS``."""
    stopped: set[str] = field(default_factory=set[str])
    """Tasks the line stopped itself in a turn, whose end needs no note."""
    departed: dict[str, str] = field(default_factory=dict[str, str])
    """Tasks the engine's task set no longer holds, with their commands, kept so that
    an end reported after the set still has its note. Bounded by
    ``MAX_TRACKED_COMMANDS``."""
    pending: dict[str, BackgroundEnd] = field(default_factory=dict[str, BackgroundEnd])
    """Ends between turns that wait, up to ``SELF_START_GRACE_SECONDS``, for a turn
    the engine may begin by itself on them."""
    permission_mode: PermissionMode = "default"
    alive: bool = True
    draining: bool = False
    """Whether the pump discards messages until a result, the tail of a turn that
    was cancelled or that the engine began by itself."""
    drained: str = SELF_STARTED_KIND
    """Which of the two the pump is draining, ``SELF_STARTED_KIND`` or
    ``INTERRUPTED_KIND``, so the result it ends with is reported as that kind."""
    ceiling: Ceiling | None = None
    """Where the connection stands against ``keep_session_max_seconds``."""
    ended_by: str | None = None
    """Why the pump stopped reading, when Dromon can say more than that the engine's
    process ended, as ``MESSAGE_TOO_LONG`` does."""


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
    tools: tuple[str, ...] | None
    """The tools the engine offers, or ``None`` when its report held no list, which
    is unknown, not empty. Row 10 of the T3 Code review."""
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
    """The SDK's client-side estimate for this turn alone. On a subscription it is
    not a charge."""
    cost_mark: tuple[str, float] | None = None
    """The session and running total the engine reported, which the scheduler hands
    to a rebuilt engine. ``None`` for an engine whose costs are not running totals."""
    usage: Mapping[str, int] | None = None
    """The engine's token counts for the turn, where it reports them."""
    cost_source: str | None = None
    """What the cost and usage were taken from, ``PER_TURN_SOURCE`` or
    ``DIFFERENCE_SOURCE``, or ``None`` when the engine reported no cost."""
    calls: tuple[Mapping[str, int | bool], ...] = ()
    """Each engine call's token counts, the latest ``MAX_CALLS``, with whether a
    subagent made it. The engine reports cost per turn and model, not per call."""
    context_tokens: int | None = None
    """The context the next turn will re-read, estimated from the last main-loop
    call, or ``None`` where the engine reports no per-call usage."""
    compaction: Mapping[str, int | str | None] | None = None
    """The trigger and the context before and after the turn's latest compaction,
    as the engine reported them, or ``None`` when there was none."""
    compaction_cost_usd: float | None = None
    """The cost of the compaction the turn began with, included in
    ``cost_estimate_usd``, or ``None`` when there was none or no cost was reported."""
    compaction_usage: Mapping[str, int] | None = None
    """The tokens of the compaction the turn began with, included in ``usage``."""
    usage_note: str | None = None
    """What the engine's ``usage`` and ``calls`` mean, where an engine's differ from
    Claude Code's, or ``None`` for Claude Code's."""
    sandbox_mode: str | None = None
    """The sandbox the turn ran in, as the adapter knows it, such as ``enabled`` or
    ``none``, or ``None`` where the adapter does not say. Row 8 of the T3 Code
    review."""


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
        tools=_strings(data["tools"]) if isinstance(data.get("tools"), list) else None,
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
    breach: bool = False
    """Whether the refusal is a confinement breach, which the scheduler holds a line
    for unconditionally."""
    capabilities: Capabilities | None = None
    credential: Credential | None = None
    require_init: bool = True
    """Whether the turn must see the engine's init before the model's output and
    before any tool call is admitted. Every Claude Code turn requires it."""
    calls: dict[str, dict[str, int | bool]] = field(
        default_factory=dict[str, dict[str, int | bool]]
    )
    """Each engine call's token counts, by message identifier, the latest
    ``MAX_CALLS``. One response can arrive as several messages with one identifier
    and the same usage, so each call is counted once."""
    compaction_cost: float | None = None
    compaction_usage: dict[str, int] | None = None
    compaction: dict[str, int | str | None] | None = None
    last_main_call: dict[str, int | bool] | None = None
    """The latest main-loop call since the turn began or the session was last
    compacted, from which the context is estimated."""
    billing: bool = False
    """Whether an assistant message of the turn carried ``BILLING_ERROR``."""
    reads: set[str] = field(default_factory=set[str])
    """The identifiers of the ``Read`` calls a view-only line made and whose results
    have not arrived, which each must have had a call of the ViewImage hook."""


def _refusal_error(turn: _Turn) -> EngineRefused:
    """The error that ends a refused turn, a ``ConfinementBreach`` for a breach."""
    text = turn.refusal if turn.refusal is not None else ""
    return ConfinementBreach(text) if turn.breach else EngineRefused(text)


type PermissionGate = Callable[
    [str, dict[str, Any], ToolPermissionContext],
    Coroutine[Any, Any, PermissionResultAllow | PermissionResultDeny],
]


def permission_gate(
    config: LineConfig,
    own_tools: frozenset[str],
    sandbox: Mapping[str, object] | None = None,
    admits: Callable[[], bool] | None = None,
    workdir: Path | None = None,
) -> PermissionGate:
    """A gate that applies ``config``'s policy and denies every other tool call,
    and every shell call that would leave the ``sandbox`` without its leave. With
    ``admits``, every call is denied while it returns ``False``, as it does on a kept
    connection outside a checked Dromon turn. **Round 11.** For a line with
    ``ViewImage`` and without ``Read``, ``Read`` is offered to the engine for images
    only, so the gate allows it for a path ``imageaccess.image_refusal`` accepts in the
    line's ``workdir`` and denies it otherwise, whatever the tool policy."""

    async def gate(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        del context
        if admits is not None and not admits():
            return PermissionResultDeny(
                message=(
                    "Dromon's session permission gate refuses this call because "
                    "Dromon has not started and checked a turn on this session"
                )
            )
        if blocks_unsandboxed(sandbox, tool_name, tool_input):
            return PermissionResultDeny(
                message=(
                    "Dromon's sandbox rule sandbox.allowUnsandboxedCommands is not"
                    f" true, so it refuses the unsandboxed {tool_name} call because"
                    " dangerouslyDisableSandbox=true"
                )
            )
        if tool_name in HUMAN_DECISION_TOOLS:
            return PermissionResultDeny(
                message=(
                    f"Dromon's human-decision rule refuses {tool_name}. "
                    f"{HUMAN_DECISION_REFUSAL}"
                )
            )
        if tool_name == "Read" and view_only(config.builtin_tools):
            why = image_refusal(
                tool_input.get("file_path"),
                workdir if workdir is not None else Path.cwd(),
            )
            if why is not None:
                return PermissionResultDeny(
                    message=f"Dromon's ViewImage rule refuses this Read: {why}"
                )
            return PermissionResultAllow()
        if config.allows(tool_name, own_tools):
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=(
                f"{tool_name} is not available to this line because its "
                f"tool_policy={config.tool_policy.value!r} does not allow it"
            )
        )

    return gate


VIEW_IMAGE_HOOK_MATCHER: Final = "Read"
"""The tool the ``ViewImage`` hook runs before."""


def view_image_hooks(
    config: LineConfig,
    workdir: Path,
    record: Callable[[str], None] | None = None,
) -> dict[str, list[HookMatcher]] | None:
    """The hooks that hold a view-only line's ``Read`` to images, or ``None`` for any
    other line.

    **Round 14, item 5. The defect this closes.** A line with ``ViewImage`` and
    without ``Read`` is offered ``Read``, and the permission callback allowed it only
    for a path ``imageaccess.image_refusal`` accepts. The operator's live run of
    2026-11-01 showed Claude Code viewing a 6,000,287 byte png over the limit, and
    answering a renamed text file with the engine's own binary-file notice rather than
    Dromon's refusal. **Inference, the cause.** Claude Code allows a ``Read`` inside
    its working directory without calling the permission callback, so the rule ran
    only for paths the engine would ask about. **Fix.** A ``PreToolUse`` hook runs
    before every tool call, before the permission rules and the callback, whatever
    they would decide, so the rule is applied there to every ``Read``, and a
    refusal there is final, since a deny from a hook cannot be overridden by an allow.
    The callback's check stays as a second guard. **Residual.** The check and the
    engine's read are two steps, as ``imageaccess`` says.

    **Review of a9995d8, item 1. Fail closed.** A callback that raises is answered by
    the SDK with an error that Claude Code may treat as a hook that failed, which does
    not block the tool, so the body is wrapped and any exception denies. ``record``
    is called with the tool call's identifier before the decision, so the adapter can
    tell a ``Read`` that completed without the hook having run. A ``record`` that
    raises is also a deny."""
    if not view_only(config.builtin_tools):
        return None

    def deny(why: str) -> HookJSONOutput:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"Dromon's ViewImage rule refuses this Read: {why}"
                ),
            }
        }

    async def hook(
        input_data: HookInput, tool_use_id: str | None, context: HookContext
    ) -> HookJSONOutput:
        del context
        try:
            data = cast(dict[str, Any], input_data)
            identifier = (
                tool_use_id if tool_use_id is not None else data.get("tool_use_id")
            )
            if record is not None and isinstance(identifier, str):
                record(identifier)
            given = data.get("tool_input")
            arguments = cast(dict[str, Any], given) if isinstance(given, dict) else {}
            why = image_refusal(arguments.get("file_path"), workdir)
        except Exception as error:  # noqa: BLE001
            return deny(f"the hook failed with {type(error).__name__}")
        return {} if why is None else deny(why)

    return {"PreToolUse": [HookMatcher(matcher=VIEW_IMAGE_HOOK_MATCHER, hooks=[hook])]}


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


def claude_cli() -> str:
    """The Claude Code command the Agent SDK would run, found as the SDK finds it,
    which the launcher starts in its place. The launcher answers the SDK's version
    check from the same lookup, so the two cannot name different commands."""
    try:
        return launcher.claude_command()
    except Exception as error:  # noqa: BLE001
        raise EngineRefused(f"Claude Code cannot be found: {error}") from error


def launch_environment(
    mark: str | None,
    stop_policy: launcher.StopPolicy,
    shell: str | None,
    git_ssh: GitSshAccess | None = None,
    temp: LineTemp | None = None,
) -> dict[str, str]:
    """The variables a Claude Code line's engine starts with, beside the daemon's own:
    the pinned tool search, the line's process mark, what its launcher needs, and the
    variables of ``git_ssh`` under the names ``launcher.relayed`` gives them.

    Each relayed name is set, empty for a line without the option, so a value the
    daemon's own environment holds under that name, which the Agent SDK merges in
    beneath these, never reaches the engine.

    **Round 14, item 6.** A line given ``temp`` has ``TMPDIR`` and ``UV_CACHE_DIR``
    set to directories its sandbox may write, so that a here-document and the gate
    need no workaround. ``TMPDIR`` is a base variable the launcher's filter keeps, and
    ``UV_CACHE_DIR`` travels as a relayed variable, so only this value reaches the
    engine and never the daemon's own."""
    environment = {TOOL_SEARCH_VARIABLE: TOOL_SEARCH_PINNED}
    if shell is not None:
        environment[SHELL_VARIABLE] = shell
        environment[CLAUDE_SHELL_VARIABLE] = shell
    environment.update(launcher.launch_environment(claude_cli(), stop_policy))
    environment[launcher.FILTER_ENVIRONMENT_VARIABLE] = "1"
    given = gitssh.environment(git_ssh)
    if temp is not None:
        wanted = temp.environment()
        environment[TEMP_VARIABLE] = wanted[TEMP_VARIABLE]
        given[CACHE_VARIABLE] = wanted[CACHE_VARIABLE]
    if shell is not None:
        given[SHELL_VARIABLE] = shell
        given[CLAUDE_SHELL_VARIABLE] = shell
    for name in launcher.RELAYED_VARIABLES:
        environment[launcher.relayed(name)] = given.get(name, "")
    if mark is not None:
        environment[MARK_VARIABLE] = mark
    return environment


class ClaudeCodeLine:
    """One session line on the Claude Code engine, with its driver.

    ``config`` gives the line's built-in tools, MCP servers and tool policy, and
    ``mcp_tools`` the in-process tools Dromon serves it. Both are empty by default, so
    a line does nothing but converse until its tools are configured deliberately.
    """

    temp: LineTemp | None = None
    """The directories the sandbox lets the shell write for temporary files and the
    package cache, set by ``__init__``. A class default, so a subclass that does not
    call it has none."""

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
        task_ended: Callable[[BackgroundEnd], object] = ignore_end,
        begin_self_started: BeginSelfStarted = refuse_self_started,
        cost_mark: CostMark | None = None,
        compacted: Callable[
            [Mapping[str, int | str | None]], object
        ] = ignore_compaction,
        spent_between: SpentBetween = ignore_spent,
        budget_reported: BudgetReported = ignore_budget_report,
        ceiling_reached: Ask = no_operator,
        ceiling_settled: Settle = nothing_to_settle,
        consent_asked: Callable[[str], object] = no_consent_question,
        secret_names: Callable[[], Sequence[str]] | Sequence[str] = (),
        git_ssh: GitSshAccess | None = None,
        temp: LineTemp | None = None,
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
        self._secret_names = (
            secret_names if callable(secret_names) else lambda: tuple(secret_names)
        )
        self._session_factory = session_factory
        self._observe = observe
        self.mark = mark
        """The process mark every process of this line inherits, if issued."""
        self.git_ssh = git_ssh
        """The agent and pinned hosts of the project's ``[git_ssh]`` table, if the
        line was given them."""
        self.temp = temp
        """The temporary directory and package cache the sandbox lets the line's shell
        write, if it has an enabled sandbox and they could be made."""
        self._kept_factory = kept_factory
        self._task_ended = task_ended
        self._begin_self_started_callback = begin_self_started
        self._compacted = compacted
        """Called the moment the engine reports a compaction of the session."""
        self.cost_mark = cost_mark
        self._request: CompactionRequest | None = None
        """The compaction the next turn begins with, if any. Each turn takes it as
        it starts, so a turn refused before its compaction point does not leave it
        to a later turn."""
        self._spent_between = spent_between
        """Called with the cost of a result that ends a turn outside Dromon's own."""
        self._budget_reported = budget_reported
        """Called when the engine reports positive remaining provider-window budget."""
        self._usage: dict[str, int] | None = None
        self._source: str | None = None
        """What the last measured turn's cost and usage came from."""
        """The last session and running total, which the scheduler keeps across
        rebuilds of the engine, so a rebuilt engine does not count the session's
        earlier turns again."""
        """Called for each background task that ends between turns, so the
        scheduler can wake the line."""
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._limit: EnginePlanLimit | None = None
        """The plan limit the engine's latest ``rate_limit_event`` reported, if it
        reported one, which a failed turn is held at. The engine reports a change of
        status, not each turn, so the latest is kept across turns. **Assumption.** A
        rebuilt engine starts with none, so a failure before its first report holds
        the line as any failure does."""
        self._kept: _Connection | None = None
        self._turn: _Turn | None = None
        """The turn in progress, if any, whose init check the gate waits for."""
        self._notes: list[str] = []
        """Notes the next prompt carries, about what happened to the kept connection."""
        self._retired = False
        self._background: set[asyncio.Task[None]] = set()
        """Interrupts and closes begun outside a turn. asyncio holds only weak
        references to tasks, so each is kept here until it ends."""
        self._ceiling_reached = ceiling_reached
        self._ceiling_settled = ceiling_settled
        self._consent_asked = consent_asked
        """Where a consent fallback is put to an operator, as ``FALLBACK_SUBTYPES``
        says."""
        """Where a kept connection's ceiling asks an operator, and withdraws the
        question, as ``dromon.ceiling`` describes."""
        self._surfaced: set[str] = set()
        """System message subtypes already surfaced, so each is surfaced once."""
        self._hooked: set[str] = set()
        """The tool call identifiers the ViewImage hook has seen and the adapter has
        not yet matched with a completed ``Read``."""

    def _hook_called(self, tool_use_id: str) -> None:
        """Note that the ViewImage hook ran for ``tool_use_id``. Bounded by the
        number of ``Read`` calls in one turn, which ``max_turns`` bounds."""
        self._hooked.add(tool_use_id)

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
        mcp_environment = {
            name: self._environment[name]
            for name in self._secret_names()
            if name in self._environment
        }
        for name, server in self.config.mcp_servers:
            try:
                servers[name] = server.sdk_config(mcp_environment)
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
        if view_only(self.config.builtin_tools):
            # Review of a9995d8, item 1. The hook is the confinement, so a line is not
            # started where Dromon can see that hooks are switched off. The set is
            # emptied in place, since a kept connection holds the hook that fills it.
            switched_off = hook_policy_refusal(self.settings)
            if switched_off is not None:
                raise EngineRefused(
                    f"a view-only line is held to images by a hook, and {switched_off}"
                )
            self._hooked.clear()
        plugins: list[SdkPluginConfig] = []
        for written in self.config.plugins:
            directory = resolve_plugin(written)
            check_plugin(directory)
            plugins.append({"type": "local", "path": str(directory)})
        return ClaudeAgentOptions(
            model=settings.model,
            tools=claude_tools(self.config.builtin_tools),
            mcp_servers=cast(dict[str, McpServerConfig], servers),
            strict_mcp_config=not self.config.inherit_account_mcp,
            hooks=cast(Any, view_image_hooks(self.config, self.cwd, self._hook_called)),
            can_use_tool=permission_gate(
                self.config,
                self.own_tools,
                self.sandbox,
                self._admits,
                self.cwd,
            ),
            setting_sources=["project"] if self.config.project_settings else [],
            plugins=plugins,
            max_turns=settings.max_turns,
            effort=effort,
            cwd=str(self.cwd),
            resume=self.session_id,
            sandbox=cast(SandboxSettings, self.sandbox),
            settings=json.dumps(
                flag_settings(
                    self.settings,
                    (VIEW_IMAGE_HOOK_MATCHER,)
                    if view_only(self.config.builtin_tools)
                    else (),
                )
            ),
            env=launch_environment(
                self.mark,
                self.config.stop_policy,
                self._selected_shell(),
                self.git_ssh,
                self.temp,
            ),
            cli_path=self._launcher(),
            system_prompt=self._system_prompt(),
            max_buffer_size=MAX_MESSAGE_CHARACTERS,
        )

    def _selected_shell(self) -> str | None:
        if not (SHELL_TOOLS & set(self.config.builtin_tools)):
            return None
        try:
            return selected_claude_shell(self._environment)
        except ValueError as error:
            raise EngineRefused(f"Claude Code shell unavailable: {error}") from error

    @property
    def environment_for_status(self) -> Mapping[str, str]:
        """The environment this adapter uses to choose its shell."""
        return self._environment

    def _leaves_background(self) -> bool:
        """Whether the line's stop policy leaves its background work running when its
        engine is closed."""
        return self.config.stop_policy is launcher.StopPolicy.LEAVE_BACKGROUND

    @staticmethod
    def _launcher() -> str:
        """The launcher command the SDK runs in place of Claude Code, which ends the
        engine and, by the line's stop policy, what it started, when the daemon stops
        it or dies. Its absence refuses the turn."""
        try:
            return launcher.script_path()
        except FileNotFoundError as error:
            raise EngineRefused(
                f"the Dromon launcher is not installed: {error}"
            ) from error

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
        request, self._request = self._request, None
        try:
            check_environment(self._environment, self.driver.settings.strategy)
            options = self.options()
        except Exception:
            await self.close()
            raise
        if self.config.keep_session:
            return await self._kept_turn(prompt, options, request)
        self.driver.step(Event("turn_start"))
        turn = _Turn()
        result: ResultMessage | None = None
        self._turn = turn
        try:
            async with AsyncExitStack() as stack:
                session = await self._enter(stack, self._session_factory, options)
                if request is not None and self._compaction_allowed(turn):
                    await session.query(COMPACT_COMMAND)
                    note = await self._run_compaction(
                        session, turn, aiter(session.receive_response()), request
                    )
                    prompt = f"{note}\n\n{prompt}"
                if turn.refusal is not None:
                    raise _refusal_error(turn)
                answered = False
                # Bounded by HANDOFF_RESENDS, past which _handed_off refuses.
                for resends in range(HANDOFF_RESENDS + 1):
                    await session.query(prompt)
                    async for message in session.receive_response():
                        if isinstance(message, ResultMessage):
                            result = message
                            self._refuse_missing_sign_in(message.result, turn)
                            break
                        answered = answered or isinstance(
                            message, AssistantMessage | StreamEvent
                        )
                        await self._handle(session, message, turn)
                    if result is None or not self._handed_off(
                        result, turn, answered, resends
                    ):
                        break
                    result = None
                if result is not None and turn.refusal is None:
                    self.driver.step(Event("result", result.subtype, result.num_turns))
        except CLIJSONDecodeError as error:
            if message_too_long(error):
                raise EngineRefused(MESSAGE_TOO_LONG) from error
            raise
        finally:
            self._turn = None
        cost = None if result is None else self._measure(result)
        if turn.refusal is not None:
            raise _refusal_error(turn)
        self._check_limit(result, turn)
        return self._report(result, turn, cost)

    def _check_limit(self, result: ResultMessage | None, turn: _Turn) -> None:
        """Raise ``EnginePlanLimit`` when the turn shows the provider's budget
        exhausted, which is an assistant message with a billing error, or a turn
        that ended in error while the engine's latest report was a rejected limit. A
        turn that succeeded is kept, even past a limit, so its work is counted."""
        if turn.billing:
            raise EnginePlanLimit(BILLING_LIMIT, may_move_role=True)
        failed = result is None or result.is_error
        if failed and self._limit is not None:
            raise EnginePlanLimit(
                str(self._limit), self._limit.resets_at, may_move_role=True
            )

    @staticmethod
    def _refuse_missing_sign_in(text: str | None, turn: _Turn) -> None:
        if turn.refusal is None and reports_missing_sign_in(text):
            turn.refusal = SIGN_IN_FAILURE

    async def _enter[S](
        self,
        stack: AsyncExitStack,
        factory: Callable[[ClaudeAgentOptions], AbstractAsyncContextManager[S]],
        options: ClaudeAgentOptions,
    ) -> S:
        """Start the engine. A start that times out at its initialize request is
        tried once more, and the activity log says so, since on a loaded host the
        engine can miss the SDK's wait while still starting. Any other failure, and
        a second timeout, end the turn as before."""
        try:
            return await stack.enter_async_context(factory(options))
        except Exception as error:
            if not initialize_timed_out(error):
                raise
        self._observe(
            ActivityKind.NOTICE,
            "the engine's start timed out at initialize, so Dromon starts it once more",
        )
        return await stack.enter_async_context(factory(options))

    def request_compaction(
        self,
        reason: str = COMPACTION_REQUESTED,
        context_line: Callable[[], str | None] = no_context_line,
    ) -> None:
        """Compact the line's own session at the start of its next turn, whose note
        gives ``reason`` and then what ``context_line`` returns once the compaction
        has been recorded."""
        self._request = CompactionRequest(reason, context_line)

    @staticmethod
    def _compaction_allowed(turn: _Turn) -> bool:
        """Whether the turn may begin with its compaction: not when an init seen
        before it was refused, in which case no ``/compact`` is sent, and the
        request, which the turn took as it started, is spent. The engine reports its
        init only with its reply to a prompt, so normally the compaction's own reply
        carries it, and the init check then refuses and interrupts the compaction as
        it would an ordinary prompt, which is no weaker than a turn without one."""
        return turn.refusal is None

    async def _run_compaction(
        self,
        session: AgentSession | KeptSession,
        turn: _Turn,
        messages: AsyncIterator[Message | None],
        request: CompactionRequest,
    ) -> str:
        """Read the messages of a ``/compact`` sent at the start of the turn, through
        the turn's own checks, and return the note the turn's prompt opens with."""
        result: ResultMessage | None = None
        # Bounded by the engine's result for the command, or the end of its stream.
        async for message in messages:
            if message is None:
                break
            if isinstance(message, ResultMessage):
                result = message
                break
            await self._handle(session, message, turn)
        if result is not None:
            turn.compaction_cost = self._measure(result)
            turn.compaction_usage = self._usage
        if turn.compaction is None:
            turn.compaction = compaction_of(None)
        try:
            context_line = request.context_line()
        except Exception as error:  # noqa: BLE001
            print(f"dromond: the context line failed: {error}", file=sys.stderr)
            context_line = None
        return compaction_note(turn.compaction, request.reason, context_line)

    def _compaction_seen(self, turn: _Turn, metadata: object) -> None:
        """Note a compaction, asked for or automatic, the moment the engine reports
        it: the turn's estimate starts again from the compacted context, and the
        scheduler hears of it at once, so a status read in the same turn shows it."""
        turn.compaction = compaction_of(metadata)
        turn.last_main_call = None
        try:
            self._compacted(dict(turn.compaction))
        except Exception as error:  # noqa: BLE001
            print(
                f"dromond: a compaction could not be recorded: {error}", file=sys.stderr
            )

    def _context_after(self, turn: _Turn) -> int | None:
        """The context the next turn will re-read: the last main-loop call since the
        turn began or the session was compacted, or the size the compaction left."""
        found = context_of(turn.last_main_call)
        if found is not None or turn.compaction is None:
            return found
        after = turn.compaction.get("after_tokens")
        return after if isinstance(after, int) else None

    def _measure(self, result: ResultMessage) -> float | None:
        """The turn's own cost from its result, moving the cost mark on. The
        engine's per-turn usage is preferred where the result carries it, since it
        needs no mark, and the difference of running totals is the fallback."""
        total = result.total_cost_usd
        cost = turn_cost(total, result.session_id, self.cost_mark)
        if total is not None:
            self.cost_mark = (result.session_id, total)
        per_turn = turn_usage_of(result)
        if per_turn is not None:
            self._source = PER_TURN_SOURCE
            self._usage = per_turn[1]
            return per_turn[0]
        self._source = None if cost is None else DIFFERENCE_SOURCE
        self._usage = usage_of(result.usage)
        return cost

    def _report(
        self,
        result: ResultMessage | None,
        turn: _Turn,
        cost: float | None,
    ) -> TurnReport:
        """The turn's report, with ``cost`` the turn's own, which ``_measure`` took
        from the engine's running total."""
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
            cost_estimate_usd=add_costs(turn.compaction_cost, cost),
            cost_mark=self.cost_mark,
            usage=add_usage(turn.compaction_usage, self._usage),
            cost_source=self._source,
            calls=tuple(dict(call) for call in turn.calls.values()),
            context_tokens=self._context_after(turn),
            compaction=turn.compaction,
            compaction_cost_usd=turn.compaction_cost,
            compaction_usage=turn.compaction_usage,
            sandbox_mode=(
                "enabled"
                if self.sandbox is not None and self.sandbox.get("enabled") is True
                else "disabled"
            ),
        )

    async def _handle(
        self, session: AgentSession | KeptSession, message: Message, turn: _Turn
    ) -> None:
        if isinstance(message, SystemMessage) and message.subtype == "init":
            turn.capabilities = _capabilities(message.data)
            await self._check_init(session, turn.capabilities, turn)
        elif (
            isinstance(message, SystemMessage) and message.subtype == "compact_boundary"
        ):
            self._compaction_seen(turn, message.data.get("compact_metadata"))
        elif (
            isinstance(message, SystemMessage) and message.subtype in FALLBACK_SUBTYPES
        ):
            self._surface(message.subtype)
            await self._fallen_back(session, message, turn)
        elif isinstance(message, SystemMessage):
            self._surface(message.subtype)
        elif isinstance(message, RateLimitEvent):
            self._limit = rejected_limit(message.rate_limit_info)
            budget = remaining_window_budget(message.rate_limit_info)
            if budget is not None:
                self._budget_reported(*budget)
        elif isinstance(message, AssistantMessage):
            if message.error == BILLING_ERROR:
                turn.billing = True
            text = "".join(
                block.text for block in message.content if isinstance(block, TextBlock)
            )
            if turn.refusal is None and reports_missing_sign_in(text):
                turn.refusal = SIGN_IN_FAILURE
                await session.interrupt()
                return
            if turn.require_init and turn.capabilities is None:
                if turn.refusal is None:
                    turn.refusal = (
                        "the engine did not report its tools and credential before"
                        " its output, so the turn could not be checked"
                    )
                    await session.interrupt()
                return
            counts = record_call(turn.calls, message)
            if counts is not None and not counts["subagent"]:
                turn.last_main_call = counts
            for block in message.content:
                if isinstance(block, TextBlock):
                    turn.text.append(block.text)
                    self._observe(ActivityKind.TEXT, block.text)
                    event = Event("text", "", len(block.text))
                elif isinstance(block, ToolUseBlock):
                    turn.tool_uses.append(block.name)
                    if block.name == VIEW_IMAGE_HOOK_MATCHER and view_only(
                        self.config.builtin_tools
                    ):
                        turn.reads.add(block.id)
                    self._observe(ActivityKind.TOOL, block.name)
                    event = Event("tool_use", block.name)
                elif isinstance(block, ThinkingBlock):
                    self._observe(ActivityKind.THINKING, "")
                    event = Event("thinking")
                else:
                    continue
                await self._act(session, event, turn)
        elif isinstance(message, UserMessage) and isinstance(message.content, list):
            # The engine echoes a tool's result as it delivers it to the model, so
            # this is the safe point before the next model call that priority mail
            # takes, as ``dromon.priority`` describes.
            for block in message.content:
                if isinstance(block, ToolResultBlock):
                    await self._read_hooked(session, block.tool_use_id, turn)
                    await self._act(session, Event(TOOL_RESULT), turn)

    async def _read_hooked(
        self, session: AgentSession | KeptSession, tool_use_id: str, turn: _Turn
    ) -> None:
        """Refuse and interrupt a turn in which a view-only line's ``Read`` completed
        without the ViewImage hook having run for it. **Review of a9995d8, item 1.**
        The hook runs before the tool, so by the time its result arrives the hook has
        been called. If it was not, the engine skipped it, as it would if hooks were
        switched off, and the confinement did not apply to that call. The refusal
        ends the turn and the scheduler holds a line whose turn is refused."""
        if tool_use_id not in turn.reads:
            return
        turn.reads.discard(tool_use_id)
        if tool_use_id in self._hooked:
            self._hooked.discard(tool_use_id)
            return
        turn.breach = True
        if turn.refusal is None:
            turn.refusal = (
                "a Read completed on a view-only line without the ViewImage hook"
                " having run for it, so Dromon cannot show that the call was held to"
                " an image"
            )
            await session.interrupt()

    async def _act(
        self, session: AgentSession | KeptSession, event: Event, turn: _Turn
    ) -> None:
        action = self.driver.step(event)
        if action.tag is ActionTag.INTERRUPT and turn.interrupted_for is None:
            turn.interrupted_for = action.text
            await session.interrupt()

    async def _fallen_back(
        self, session: AgentSession | KeptSession, message: SystemMessage, turn: _Turn
    ) -> None:
        """Refuse and interrupt a turn the engine moved to a model its driver was not
        written for, and put a consent fallback to an operator. **Review F16.**"""
        bound_to = self.driver.settings.model
        moved_to = message.data.get("fallback_model")
        stopped = moved_to != bound_to
        if message.subtype == CONSENT_FALLBACK:
            try:
                self._consent_asked(consent_question(message.data, bound_to, stopped))
            except Exception as error:  # noqa: BLE001
                print(f"dromond: a consent question failed: {error}", file=sys.stderr)
        if not stopped or turn.refusal is not None:
            return
        turn.refusal = (
            f"the engine reported {message.subtype}, which moved the turn from"
            f" {fallback_text(message.data.get('original_model'))} to"
            f" {fallback_text(moved_to)}, not the model {bound_to} its driver was"
            " written for"
        )
        await session.interrupt()

    def _surface(self, subtype: str) -> None:
        """Report a system message subtype Dromon does not handle in the line's
        activity, once per subtype, so that none is passed over silently. Handled
        subtypes are not reported. The subtype is the engine's text, so it is quoted
        and cut."""
        if subtype in HANDLED_SYSTEM_SUBTYPES or subtype in self._surfaced:
            return
        if len(self._surfaced) >= MAX_SURFACED_SUBTYPES:
            return
        self._surfaced.add(subtype)
        shown = json.dumps(subtype[:MAX_SUBTYPE_CHARACTERS], ensure_ascii=True)
        self._observe(
            ActivityKind.NOTICE,
            f"the engine sent a system message of subtype {shown}, which Dromon does"
            " not handle",
        )
        print(f"dromond: an unhandled system message subtype {shown}", file=sys.stderr)

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
            return tool_name in claude_tools(self.config.builtin_tools)
        if server == MCP_SERVER_NAME:
            return tool_name in self.own_tools
        return server in self.config.server_names() or self.config.inherit_account_mcp

    def _refusal(self, capabilities: Capabilities, turn: _Turn) -> str | None:
        """Why the driver or the policy refuses this engine, or ``None`` to proceed."""
        # Row 7 of the T3 Code review. An engine older than the one Dromon was tested
        # against is refused before anything else is judged.
        too_old = version_refusal(CLAUDE_CODE, capabilities.claude_code_version or None)
        if too_old is not None:
            return too_old
        if capabilities.tools is None:
            # Row 10. A report without its list of tools is unknown, and an empty
            # list would pass the check below that it cannot make.
            return (
                "the engine did not report its tools, so the turn could not be checked"
            )
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
        ceiling = self._ceiling_of(connection)
        return ceiling.deadline(connection.last_end, bool(connection.tasks)) - (
            self._clock()
        )

    def _ceiling_of(self, connection: _Connection) -> Ceiling:
        if connection.ceiling is None:
            connection.ceiling = Ceiling(
                self.config, self._ceiling_reached, self._ceiling_settled
            )
        return connection.ceiling

    def kept_status(self) -> dict[str, object]:
        """Whether the engine's connection is kept, for ``line_status``."""
        connection = self._kept
        alive = connection is not None and connection.alive
        return {
            "keep_session": self.config.keep_session,
            "kept": alive,
            "seconds_to_close": self.seconds_to_close(),
            "background_tasks": len(connection.tasks) if connection and alive else 0,
            "tasks": (
                [
                    {"task_id": task_id, "command": command}
                    for task_id, command in sorted(connection.tasks.items())
                ]
                if connection and alive
                else []
            ),
        }

    async def expire(self) -> bool:
        """Close the kept connection if its deadline has passed, or ask an operator
        at its ceiling as ``keep_session_at_max`` says, and say whether it was
        closed. The watch calls this, and tests may call it with their clock."""
        connection = self._kept
        if connection is None or not connection.alive or self._turn is not None:
            return False
        reason = self._ceiling_of(connection).due(
            connection.last_end, len(connection.tasks), self._clock()
        )
        if reason is None:
            return False
        await self.close(reason)
        return True

    async def decide_ceiling(self, question_id: str, answer: str) -> Choice | None:
        """Carry out an operator's answer to the kept connection's ceiling question,
        and return the choice made, or ``None`` when it made none."""
        connection = self._kept
        if connection is None:
            return None
        choice = self._ceiling_of(connection).decide(question_id, answer, self._clock())
        if choice is Choice.STOP:
            await self.close(STOP_REASON)
        return choice

    async def halt(self) -> str | None:
        """Finish an operator's stop, once the stopped turn has ended. **Row 1 of the
        T3 Code review.** The interrupt the cancelled turn began is awaited, within
        its own bound. If it failed, it has closed the connection already. If it
        settled while the engine still runs background work, or has not finished
        the stopped turn, the connection is closed here, which ends the engine and,
        by the line's stop policy, what it started. Returns the reason it closed the
        connection, or ``None``."""
        pending = list(self._background)
        if pending:
            await asyncio.wait(pending, timeout=INTERRUPT_TIMEOUT_SECONDS + 1)
        connection = self._kept
        if connection is None or not connection.alive:
            return None
        if not connection.tasks and not connection.draining:
            return None
        await self.close(HALT_REASON)
        return HALT_REASON

    async def retire(self) -> None:
        """Close the kept connection for good, once any turn in progress has ended.
        The scheduler calls this when it discards the engine."""
        self._retired = True
        if self._turn is None:
            await self.close()

    @staticmethod
    def _withdraw_ceiling(connection: _Connection) -> None:
        """Withdraw the connection's open ceiling question, if any. A failure is
        reported and goes no further, since the pump calls this as it ends."""
        if connection.ceiling is None:
            return
        try:
            connection.ceiling.withdraw()
        except Exception as error:  # noqa: BLE001
            print(
                f"dromond: a ceiling question was not withdrawn: {error}",
                file=sys.stderr,
            )

    async def close(self, reason: str | None = None) -> None:
        """Close the kept connection, if any, which ends the engine's process and its
        background tasks. With ``reason`` the next prompt says why."""
        connection = self._kept
        if connection is None:
            return
        self._kept = None
        self._withdraw_ceiling(connection)
        if connection.pending:
            self._flush(connection, self_started=False)
        if reason is not None:
            self._notes.append(
                closed_note(reason, len(connection.tasks), self._leaves_background())
            )
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
            session = await self._enter(stack, self._kept_factory, options)
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

    async def interrupt_self_started(self) -> None:
        """Interrupt a task-triggered turn that Dromon had admitted, then discard
        its tail while still accounting for any cost the result reports."""
        connection = self._kept
        if connection is None or connection.self_started_turn is None:
            return
        connection.self_started_turn = None
        connection.self_started_finish = None
        self._turn = None
        connection.draining = True
        connection.drained = INTERRUPTED_KIND
        connection.last_end = self._clock()
        self._ceiling_of(connection).turn_ended()
        if not await self._interrupt(connection):
            await self.close("Dromon could not interrupt its self-started turn")

    async def _pump(self, connection: _Connection) -> None:
        """Read every message of the connection. During a turn each goes to the
        turn. Between turns a background task's report is tracked. A turn the engine
        starts after a task end is admitted only when the scheduler can account for
        it as an ordinary turn; other self-starts are interrupted and noted."""
        try:
            async for message in connection.session.receive_messages():
                self._track(connection, message)
                inbox = connection.inbox
                if inbox is not None:
                    inbox.put_nowait(message)
                    if isinstance(message, ResultMessage):
                        connection.inbox = None
                    continue
                if connection.self_started_turn is not None:
                    if isinstance(message, ResultMessage):
                        await self._finish_self_started(connection, message, None)
                    else:
                        try:
                            await self._handle(
                                connection.session,
                                message,
                                connection.self_started_turn,
                            )
                        except Exception as error:  # noqa: BLE001
                            await self._finish_self_started(connection, None, error)
                    continue
                if connection.draining:
                    if isinstance(message, ResultMessage):
                        connection.draining = False
                        self._spent_alone(message, connection.drained)
                    continue
                if isinstance(message, SystemMessage):
                    self._surface(message.subtype)
                if isinstance(message, ResultMessage) and is_handshake(message):
                    # Row 5 of the T3 Code review. Not a turn, so neither noted nor
                    # interrupted, while a cost it may carry still counts.
                    self._spent_alone(message, HANDSHAKE_KIND)
                    continue
                if not begins_turn(message):
                    continue
                connection.draining = not isinstance(message, ResultMessage)
                connection.drained = SELF_STARTED_KIND
                if connection.pending:
                    # The turn is the engine's answer to those ends, so their notes
                    # say so. If the scheduler admits it, its ordinary accounting
                    # replaces the separate wake.
                    if await self._admit_self_started(connection, message):
                        continue
                    self._flush(connection, self_started=True)
                elif SELF_STARTED_NOTE not in self._notes:
                    self._notes.append(SELF_STARTED_NOTE)
                if isinstance(message, ResultMessage):
                    self._spent_alone(message, SELF_STARTED_KIND)
                if not await self._interrupt(connection):
                    return
        except Exception as error:  # noqa: BLE001
            # The engine's process or its transport ended. The connection is
            # marked dead below, and the next turn resumes in a new process. A
            # message too long for the SDK is named, so the turn can say why.
            if message_too_long(error):
                connection.ended_by = MESSAGE_TOO_LONG
        finally:
            if connection.self_started_finish is not None:
                await self._finish_self_started(
                    connection,
                    None,
                    EngineRefused(
                        connection.ended_by
                        or "the engine ended during its self-started turn"
                    ),
                )
            connection.alive = False
            if connection.inbox is not None:
                connection.inbox.put_nowait(None)
            # Review F12. A connection that dies without a close withdraws its
            # ceiling question here, since nothing can act on an answer now.
            self._withdraw_ceiling(connection)

    async def _admit_self_started(
        self, connection: _Connection, first: Message
    ) -> bool:
        """Ask the scheduler to own the engine's task-triggered turn before its
        output or tools can proceed."""
        ends = tuple(
            replace(end, self_started=True) for end in connection.pending.values()
        )
        try:
            finish = self._begin_self_started_callback(ends)
        except Exception as error:  # noqa: BLE001
            print(
                f"dromond: the scheduler could not admit a self-started turn: {error}",
                file=sys.stderr,
            )
            return False
        if finish is None:
            return False
        connection.pending.clear()
        connection.draining = False
        connection.self_started_turn = _Turn(
            capabilities=connection.capabilities,
            require_init=connection.capabilities is None,
        )
        connection.self_started_finish = finish
        self._turn = connection.self_started_turn
        self.driver.step(Event("turn_start"))
        try:
            await self._handle(connection.session, first, connection.self_started_turn)
        except Exception as error:  # noqa: BLE001
            await self._finish_self_started(connection, None, error)
        return True

    async def _finish_self_started(
        self,
        connection: _Connection,
        result: ResultMessage | None,
        error: Exception | None,
    ) -> None:
        """Finish and report an admitted turn, including its checked tool calls and
        measured result, so the scheduler applies its ordinary limits."""
        turn = connection.self_started_turn
        finish = connection.self_started_finish
        if turn is None or finish is None:
            return
        report: TurnReport | None = None
        if error is None:
            try:
                if result is None:
                    raise EngineRefused(
                        "the engine ended its self-started turn without a result"
                    )
                self._refuse_missing_sign_in(result.result, turn)
                if turn.refusal is not None:
                    raise _refusal_error(turn)
                if turn.capabilities is None:
                    raise EngineRefused(
                        "the engine did not report its tools and credential before"
                        " the self-started turn"
                    )
                self.driver.step(Event("result", result.subtype, result.num_turns))
                cost = self._measure(result)
                self._check_limit(result, turn)
                report = self._report(result, turn, cost)
            except Exception as caught:  # noqa: BLE001
                error = caught
        connection.self_started_turn = None
        connection.self_started_finish = None
        connection.last_end = self._clock()
        connection.draining = False
        self._turn = None
        self._ceiling_of(connection).turn_ended()
        try:
            finish(report, error)
        except Exception as callback_error:  # noqa: BLE001
            print(
                f"dromond: the self-started turn outcome could not be recorded:"
                f" {callback_error}",
                file=sys.stderr,
            )
        if error is not None:
            await self.close()
        elif self._retired:
            self._spawn(self.close())

    def _handed_off(
        self, result: ResultMessage, turn: _Turn, answered: bool, resends: int
    ) -> bool:
        """Whether ``result`` is a handoff, as ``is_handoff`` says, after which the
        turn sends its prompt again. Its cost counts as a handshake's. A turn already
        refused or interrupted is not resent, and a second handoff refuses the turn,
        since a success that ran nothing would misreport it. **Item 6 of the live
        check of 2026-10-26.**"""
        if (
            answered
            or turn.refusal is not None
            or turn.interrupted_for is not None
            or not is_handoff(result)
        ):
            return False
        self._spent_alone(result, HANDSHAKE_KIND)
        if result.result:
            self._observe(ActivityKind.NOTICE, handoff_text_notice(result.result))
        if resends >= HANDOFF_RESENDS:
            raise EngineRefused(HANDOFF_REFUSAL)
        self._observe(ActivityKind.NOTICE, HANDOFF_NOTICE)
        return True

    def _spent_alone(self, result: ResultMessage, kind: str) -> None:
        """Report the cost of a result that ends a turn outside Dromon's own, so it
        counts toward the line's budget and is shown on its own, and move the cost
        mark past it."""
        cost = self._measure(result)
        try:
            self._spent_between(kind, cost, self._usage)
        except Exception as error:  # noqa: BLE001
            print(f"dromond: a cost could not be recorded: {error}", file=sys.stderr)

    def _track(self, connection: _Connection, message: Message) -> None:
        """Follow the connection's background tasks: the command each tool call
        runs, each task's start, a stop the line asked for, and every end, which is
        passed to ``task_ended`` once per task unless it waits briefly to see whether
        the engine starts a turn on it."""
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if not isinstance(block, ToolUseBlock):
                    continue
                command = block.input.get("command")
                if isinstance(command, str):
                    # Review N10. Masked as it is taken in, since a task's command
                    # reaches the line's status and its end's note, which is logged.
                    connection.commands[block.id] = redact.mask(command)[
                        :MAX_COMMAND_CHARACTERS
                    ]
                    while len(connection.commands) > MAX_TRACKED_COMMANDS:
                        del connection.commands[next(iter(connection.commands))]
                stopped = block.input.get("task_id") or block.input.get("shell_id")
                if block.name in STOP_TOOLS and isinstance(stopped, str):
                    if len(connection.stopped) >= MAX_TRACKED_COMMANDS:
                        connection.stopped.clear()
                    connection.stopped.add(stopped)
            return
        if isinstance(message, TaskStartedMessage):
            command = connection.commands.get(message.tool_use_id or "")
            connection.tasks[message.task_id] = (
                command or redact.mask(message.description)[:MAX_COMMAND_CHARACTERS]
            )
            return
        if isinstance(message, SystemMessage) and message.subtype == TASKS_CHANGED:
            self._reconcile(connection, message.data)
            return
        ended = task_ended(message)
        if ended is None:
            return
        exit_code, output = end_details(message)
        waiting = connection.pending.get(ended)
        if waiting is not None:
            # A second report of the same end, such as the notice after the update,
            # completes the first.
            connection.pending[ended] = replace(
                waiting,
                exit_code=waiting.exit_code if exit_code is None else exit_code,
                output_file=waiting.output_file or output,
            )
            return
        command = connection.tasks.pop(ended, None)
        departed = connection.departed.pop(ended, None)
        command = command or departed
        between = connection.inbox is None and connection.self_started_turn is None
        asked = ended in connection.stopped
        connection.stopped.discard(ended)
        if command is None:
            command = "a background task"
        if not between or asked:
            end = BackgroundEnd(
                ended,
                command,
                task_status(message),
                exit_code,
                output,
                during_turn=not between,
                stopped_by_line=asked,
            )
            try:
                self._task_ended(end)
            except Exception as error:  # noqa: BLE001
                print(
                    f"dromond: a background task's end could not be recorded: {error}",
                    file=sys.stderr,
                )
            return
        connection.pending[ended] = BackgroundEnd(
            ended, command, task_status(message), exit_code, output
        )
        self._spawn(self._flush_later(connection))

    @staticmethod
    def _reconcile(connection: _Connection, data: Mapping[str, Any]) -> None:
        """Follow the engine's whole set of live background tasks, as ``TASKS_CHANGED``
        says. A task it names that Dromon did not see start is added under its
        description. Between turns a task it no longer names is moved to
        ``departed``, so it stops counting as running while its end, if reported
        later, is still noted. A malformed or overlong set changes nothing that it
        cannot vouch for."""
        listed = data.get("tasks")
        if not isinstance(listed, list):
            return
        entries = cast(list[object], listed)
        named: set[str] = set()
        for entry in entries[:MAX_LISTED_TASKS]:
            if not isinstance(entry, Mapping):
                continue
            task = cast(Mapping[str, object], entry)
            task_id = task.get("task_id")
            if not isinstance(task_id, str) or not task_id:
                continue
            named.add(task_id)
            if (
                task_id in connection.tasks
                or len(connection.tasks) >= MAX_TRACKED_COMMANDS
            ):
                continue
            description = task.get("description")
            connection.tasks[task_id] = (
                redact.mask(description)[:MAX_COMMAND_CHARACTERS]
                if isinstance(description, str) and description
                else "a background task"
            )
        if connection.inbox is not None or len(entries) > MAX_LISTED_TASKS:
            return
        for task_id in [known for known in connection.tasks if known not in named]:
            connection.departed[task_id] = connection.tasks.pop(task_id)
            while len(connection.departed) > MAX_TRACKED_COMMANDS:
                del connection.departed[next(iter(connection.departed))]

    def _flush(self, connection: _Connection, self_started: bool) -> None:
        """Report every waiting end, once each."""
        waiting = list(connection.pending.values())
        connection.pending.clear()
        for end in waiting:
            try:
                self._task_ended(replace(end, self_started=self_started))
            except Exception as error:  # noqa: BLE001
                # The scheduler's failure to note a task must not end the connection.
                print(
                    f"dromond: a background task's end could not be noted: {error}",
                    file=sys.stderr,
                )

    async def _flush_later(self, connection: _Connection) -> None:
        await self_start_grace()
        if connection.pending:
            self._flush(connection, self_started=False)

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

    async def _kept_turn(
        self,
        prompt: str,
        options: ClaudeAgentOptions,
        request: CompactionRequest | None = None,
    ) -> TurnReport:
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
            if request is not None and self._compaction_allowed(turn):
                await connection.session.query(COMPACT_COMMAND)
                note = await self._run_compaction(
                    connection.session, turn, _drain(inbox), request
                )
                prompt = f"{note}\n\n{prompt}"
                # The pump let go of the inbox at the compaction's result.
                inbox = asyncio.Queue()
                connection.inbox = inbox
            if turn.refusal is not None:
                raise _refusal_error(turn)
            await connection.session.query(prompt)
            answered = False
            resends = 0
            # Bounded by the engine's result, which its max_turns caps, or by the
            # pump's end-of-stream marker, and by HANDOFF_RESENDS.
            while True:
                message = await inbox.get()
                if message is None:
                    break
                if isinstance(message, ResultMessage):
                    self._refuse_missing_sign_in(message.result, turn)
                    if self._handed_off(message, turn, answered, resends):
                        resends += 1
                        # The pump let go of the inbox at the result.
                        connection.inbox = inbox
                        await connection.session.query(prompt)
                        continue
                    result = message
                    if turn.refusal is None:
                        self.driver.step(
                            Event("result", message.subtype, message.num_turns)
                        )
                    break
                answered = answered or isinstance(
                    message, AssistantMessage | StreamEvent
                )
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    connection.permission_mode = permission_mode_of(message.data)
                await self._handle(connection.session, message, turn)
        except asyncio.CancelledError:
            if result is None and connection.alive:
                connection.inbox = None
                connection.draining = True
                connection.drained = INTERRUPTED_KIND
                self._settle(connection)
            raise
        except Exception as error:
            await self.close()
            if connection.ended_by is not None:
                # A compaction that ended the pump leaves the prompt's own query to
                # fail on the dead connection, which would hide the cause.
                self._notes.append(DIED_NOTE)
                raise EngineRefused(connection.ended_by) from error
            raise
        finally:
            self._turn = None
            connection.inbox = None
            connection.last_end = self._clock()
            self._ceiling_of(connection).turn_ended()
            if self._retired:
                self._spawn(self.close())
        if result is None:
            await self.close()
            self._notes.append(DIED_NOTE)
            if turn.refusal is not None:
                raise _refusal_error(turn)
            raise EngineRefused(
                connection.ended_by or "the engine's process ended during the turn"
            )
        cost = self._measure(result)
        if turn.refusal is not None:
            await self.close()
            raise _refusal_error(turn)
        self._check_limit(result, turn)
        if turn.capabilities is not None:
            connection.capabilities = turn.capabilities
        return self._report(result, turn, cost)
