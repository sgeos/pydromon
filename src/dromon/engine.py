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

Tool isolation. A line sees only the tools it is given. ``tools`` lists the built-in
tools, empty by default. ``mcp_tools`` are in-process tools served by Dromon, such as
``send_message``. ``strict_mcp_config=True`` keeps every other MCP server out, which
matters because a claude.ai login otherwise brings the account's connectors, such as
mail and file sharing, into the session. No tool is approved in advance. Every call
reaches the line's permission gate, which allows exactly the given tools and denies
everything else, and a turn whose engine reports any tool beyond the given ones is
interrupted and refused.

Failure modes. A refusal by the policy or the driver raises ``EngineRefused`` after the
turn is interrupted. A driver failure raises ``DriverError``. Engine and transport
failures from the SDK propagate unchanged.
"""

import importlib.metadata
import os
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
    Message,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SdkMcpTool,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolUseBlock,
    create_sdk_mcp_server,
)

from dromon.driver import ActionTag, Credential, Driver, Event, Strategy

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


def permission_gate(allowed: frozenset[str]) -> PermissionGate:
    """A gate that allows exactly ``allowed`` and denies every other tool call."""

    async def gate(
        tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        del tool_input, context
        if tool_name in allowed:
            return PermissionResultAllow()
        return PermissionResultDeny(
            message=f"{tool_name} is not available to this line"
        )

    return gate


def mcp_tool_name(tool: SdkMcpTool[object]) -> str:
    """The name under which Claude Code exposes one of Dromon's in-process tools."""
    return f"mcp__{MCP_SERVER_NAME}__{tool.name}"


class ClaudeCodeLine:
    """One session line on the Claude Code engine, with its driver.

    ``tools`` lists the built-in tools the line may use, and ``mcp_tools`` the
    in-process tools Dromon serves it. Both are empty by default, so a line does
    nothing but converse until its tools are configured deliberately.
    """

    def __init__(
        self,
        driver: Driver,
        cwd: Path,
        *,
        tools: Sequence[str] = (),
        mcp_tools: Sequence[SdkMcpTool[object]] = (),
        session_id: str | None = None,
        environment: Mapping[str, str] | None = None,
        session_factory: SessionFactory = _sdk_session,
    ) -> None:
        self.driver = driver
        self.cwd = cwd
        self.tools = tuple(tools)
        self.mcp_tools = tuple(mcp_tools)
        self.session_id = session_id
        self._environment = os.environ if environment is None else environment
        self._session_factory = session_factory

    @property
    def expected_tools(self) -> frozenset[str]:
        """Every tool name the engine may report for this line."""
        return frozenset(self.tools) | {mcp_tool_name(tool) for tool in self.mcp_tools}

    def options(self) -> ClaudeAgentOptions:
        settings = self.driver.settings
        effort = settings.effort
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            raise EngineRefused(f"the driver names an unknown effort {effort!r}")
        servers: dict[str, McpSdkServerConfig] = {}
        if self.mcp_tools:
            servers[MCP_SERVER_NAME] = create_sdk_mcp_server(
                MCP_SERVER_NAME, tools=list(self.mcp_tools)
            )
        return ClaudeAgentOptions(
            model=settings.model,
            tools=list(self.tools),
            mcp_servers=dict(servers),
            strict_mcp_config=True,
            can_use_tool=permission_gate(self.expected_tools),
            setting_sources=[],
            max_turns=settings.max_turns,
            effort=effort,
            cwd=str(self.cwd),
            resume=self.session_id,
        )

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
                    event = Event("text", "", len(block.text))
                elif isinstance(block, ToolUseBlock):
                    turn.tool_uses.append(block.name)
                    event = Event("tool_use", block.name)
                elif isinstance(block, ThinkingBlock):
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

    def _refusal(self, capabilities: Capabilities, turn: _Turn) -> str | None:
        """Why the driver or the policy refuses this engine, or ``None`` to proceed."""
        unexpected = sorted(set(capabilities.tools) - self.expected_tools)
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
