"""The Claude Code engine adapter, which runs session line turns through the Agent SDK.

Dromon drives the engine from outside. Each turn starts a Claude Code process through
``ClaudeSDKClient``, resumes the line's engine session when one exists, feeds every
message the engine emits to the line's Keleusma driver as an event, and performs the
driver's actions, such as interrupting a turn that exceeds the tool budget.

Credential policy. The operator's only credential is a Claude subscription, and usage
billed per token must never start silently. ``check_subscription_environment`` refuses
to start when the environment selects an API key, a cloud provider or an API profile,
each of which Claude Code would prefer over the subscription. The engine's own init
report is then checked, and a turn whose ``apiKeySource`` is anything but ``"none"`` is
interrupted and refused. ``setting_sources=[]`` keeps user and project settings, such
as an ``apiKeyHelper``, out of the line. Managed policy settings still apply. Dromon
never reads, stores or forwards the sign-in token, which stays in Claude Code's keychain
entry, as Anthropic's terms require.

Failure modes. A refusal by the policy or the driver raises ``EngineRefused`` after the
turn is interrupted. A driver failure raises ``DriverError``. Engine and transport
failures from the SDK propagate unchanged.
"""

import importlib.metadata
import os
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol, cast

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    Message,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from dromon.driver import ActionTag, Driver, Event

SUBSCRIPTION_KEY_SOURCE: Final = "none"
"""The ``apiKeySource`` Claude Code reports for a claude.ai login."""

NON_SUBSCRIPTION_ENVIRONMENT: Final = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
)
"""Variables that make Claude Code use a credential other than the subscription, or a
provider whose ``apiKeySource`` would also read ``"none"``."""


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


def check_subscription_environment(environment: Mapping[str, str]) -> None:
    """Raise ``EngineRefused`` if ``environment`` selects another credential."""
    present = [name for name in NON_SUBSCRIPTION_ENVIRONMENT if environment.get(name)]
    if present:
        raise EngineRefused(
            "the environment selects a credential other than the Claude subscription: "
            + ", ".join(present)
            + ". Unset these variables to run on the subscription."
        )


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


class ClaudeCodeLine:
    """One session line on the Claude Code engine, with its driver.

    ``tools`` lists the built-in tools the line may use, empty by default, so a line
    does nothing but converse until its tools are configured deliberately.
    """

    def __init__(
        self,
        driver: Driver,
        cwd: Path,
        *,
        tools: Sequence[str] = (),
        session_id: str | None = None,
        environment: Mapping[str, str] | None = None,
        session_factory: SessionFactory = _sdk_session,
    ) -> None:
        self.driver = driver
        self.cwd = cwd
        self.tools = tuple(tools)
        self.session_id = session_id
        self._environment = os.environ if environment is None else environment
        self._session_factory = session_factory

    def options(self) -> ClaudeAgentOptions:
        settings = self.driver.settings
        effort = settings.effort
        if effort not in ("low", "medium", "high", "xhigh", "max"):
            raise EngineRefused(f"the driver names an unknown effort {effort!r}")
        return ClaudeAgentOptions(
            model=settings.model,
            tools=list(self.tools),
            allowed_tools=list(self.tools),
            setting_sources=[],
            max_turns=settings.max_turns,
            effort=effort,
            cwd=str(self.cwd),
            resume=self.session_id,
        )

    async def run_turn(self, prompt: str) -> TurnReport:
        """Run one turn for ``prompt`` and return what happened."""
        check_subscription_environment(self._environment)
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
        if capabilities.api_key_source != SUBSCRIPTION_KEY_SOURCE:
            turn.refusal = (
                f"the engine uses the credential {capabilities.api_key_source!r},"
                " not the Claude subscription"
            )
        else:
            action = self.driver.step(Event("init", capabilities.model))
            if action.tag is ActionTag.REFUSE:
                turn.refusal = (
                    f"the driver for {action.text} refuses the engine model"
                    f" {capabilities.model!r}"
                )
        if turn.refusal is not None:
            await session.interrupt()
