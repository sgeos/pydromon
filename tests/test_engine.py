"""The Claude Code engine adapter, driven by scripted sessions instead of the SDK.

Only ``test_a_live_turn_on_the_subscription`` reaches the real engine. It uses the
operator's subscription, so it needs both the ``live`` marker selected and
``DROMON_LIVE_TESTS=1``. A later ``-m`` on the command line replaces the configured
deselection, and the variable keeps such a run from calling the engine by accident.
"""

import asyncio
import os
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from importlib import resources
from pathlib import Path

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)

from dromon.driver import Driver
from dromon.engine import (
    NON_SUBSCRIPTION_ENVIRONMENT,
    AgentSession,
    ClaudeCodeLine,
    EngineRefused,
    TurnReport,
    check_subscription_environment,
)

MODEL = "claude-opus-5-5"


def init(model: str = MODEL, source: str = "none") -> SystemMessage:
    return SystemMessage(
        subtype="init",
        data={
            "model": model,
            "apiKeySource": source,
            "claude_code_version": "2.1.310",
            "tools": [],
            "permissionMode": "default",
            "capabilities": ["interrupt_receipt_v1"],
        },
    )


def result(subtype: str = "success", turns: int = 1) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=turns,
        session_id="session-1",
        total_cost_usd=0.001,
    )


def say(*blocks: TextBlock | ToolUseBlock | ThinkingBlock) -> AssistantMessage:
    return AssistantMessage(content=list(blocks), model=MODEL)


def tool(name: str) -> ToolUseBlock:
    return ToolUseBlock(id=f"t-{name}", name=name, input={})


class Scripted:
    """A session that replays messages and records what the adapter did."""

    def __init__(self, messages: Sequence[Message]) -> None:
        self.messages = list(messages)
        self.prompts: list[str] = []
        self.interrupts = 0
        self.options: list[ClaudeAgentOptions] = []

    async def query(self, prompt: str) -> None:
        self.prompts.append(prompt)

    async def receive_response(self) -> AsyncIterator[Message]:
        for message in self.messages:
            yield message

    async def interrupt(self) -> None:
        self.interrupts += 1

    def factory(
        self, options: ClaudeAgentOptions
    ) -> AbstractAsyncContextManager[AgentSession]:
        self.options.append(options)

        @asynccontextmanager
        async def opened() -> AsyncGenerator[AgentSession]:
            yield self

        return opened()


def line(
    session: Scripted,
    driver: Driver | None = None,
    environment: Mapping[str, str] | None = None,
) -> ClaudeCodeLine:
    return ClaudeCodeLine(
        driver or Driver.for_model(MODEL),
        Path("/work"),
        environment=environment or {},
        session_factory=session.factory,
    )


def run(target: ClaudeCodeLine, prompt: str = "hello") -> TurnReport:
    return asyncio.run(target.run_turn(prompt))


def test_a_plain_turn_reports_text_capabilities_and_session() -> None:
    session = Scripted(
        [
            init(),
            say(ThinkingBlock("", ""), TextBlock("rea"), TextBlock("dy")),
            result(),
        ]
    )
    target = line(session)
    report = run(target)
    assert session.prompts == ["hello"]
    assert report.text == "ready"
    assert (report.subtype, report.num_turns, report.session_id) == (
        "success",
        1,
        "session-1",
    )
    assert report.capabilities is not None
    assert report.capabilities.api_key_source == "none"
    assert report.capabilities.claude_code_version == "2.1.310"
    assert report.interrupted_for is None
    assert session.interrupts == 0
    assert target.driver.state == (0, 1)


def test_options_follow_the_driver_and_isolate_the_line() -> None:
    session = Scripted([init(), result()])
    target = line(session)
    run(target)
    options = session.options[0]
    settings = target.driver.settings
    assert options.model == settings.model
    assert options.effort == settings.effort
    assert options.max_turns == settings.max_turns
    assert options.setting_sources == []
    assert options.tools == []
    assert options.allowed_tools == []
    assert options.resume is None
    assert options.cwd == "/work"


def test_the_next_turn_resumes_the_engine_session() -> None:
    session = Scripted([init(), result()])
    target = line(session)
    run(target)
    run(target)
    assert session.options[1].resume == "session-1"


def test_the_driver_budget_interrupts_a_turn_once() -> None:
    text = resources.files("dromon.drivers").joinpath("claude_opus_5_5.kel").read_text()
    driver = Driver(
        text.replace(
            "fn tool_budget() -> Word { 40 }", "fn tool_budget() -> Word { 1 }"
        )
    )
    session = Scripted(
        [
            init(),
            say(tool("Read")),
            say(tool("Bash"), tool("Edit")),
            result("error_during_execution"),
        ]
    )
    report = run(line(session, driver))
    assert report.interrupted_for == "tool_budget"
    assert session.interrupts == 1
    assert report.tool_uses == ("Read", "Bash", "Edit")


@pytest.mark.parametrize("variable", NON_SUBSCRIPTION_ENVIRONMENT)
def test_a_non_subscription_environment_is_refused_before_starting(
    variable: str,
) -> None:
    session = Scripted([init(), result()])
    with pytest.raises(EngineRefused, match=variable):
        run(line(session, environment={variable: "1"}))
    assert session.prompts == []
    assert session.options == []


def test_an_empty_variable_does_not_count() -> None:
    check_subscription_environment({"ANTHROPIC_API_KEY": ""})


@pytest.mark.parametrize(
    "source", ["ANTHROPIC_API_KEY", "apiKeyHelper", "/login managed key", ""]
)
def test_an_engine_using_another_credential_is_interrupted_and_refused(
    source: str,
) -> None:
    session = Scripted([init(source=source), say(TextBlock("x")), result()])
    with pytest.raises(EngineRefused, match="not the Claude subscription"):
        run(line(session))
    assert session.interrupts == 1


def test_an_engine_running_another_model_is_refused_by_the_driver() -> None:
    session = Scripted([init(model="claude-sonnet-5-5"), result()])
    with pytest.raises(
        EngineRefused, match="refuses the engine model 'claude-sonnet-5-5'"
    ):
        run(line(session))
    assert session.interrupts == 1


def test_a_turn_without_a_result_is_refused() -> None:
    with pytest.raises(EngineRefused, match="without a result"):
        run(line(Scripted([init(), say(TextBlock("x"))])))


@pytest.mark.live
def test_a_live_turn_on_the_subscription() -> None:
    if os.environ.get("DROMON_LIVE_TESTS") != "1":
        pytest.skip("set DROMON_LIVE_TESTS=1 to call the engine on the subscription")
    target = ClaudeCodeLine(Driver.for_model(MODEL), Path.cwd())
    report = asyncio.run(target.run_turn("Reply with the single word: ready"))
    assert report.subtype == "success"
    assert report.capabilities is not None
    assert report.capabilities.api_key_source == "none"
    assert report.capabilities.model == MODEL
