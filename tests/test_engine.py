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

from dromon.driver import Credential, Driver, Strategy, driver_resource
from dromon.engine import (
    API_ENVIRONMENT,
    KEY_SOURCES,
    NON_SUBSCRIPTION_ENVIRONMENT,
    PROVIDER_ENVIRONMENT,
    AgentSession,
    ClaudeCodeLine,
    EngineRefused,
    TurnReport,
    check_environment,
    credential_of,
)

MODEL = "claude-haiku-5-5"
"""Tests use the least expensive model, as the operator directs."""


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
        driver or Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
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
    assert target.driver.state == (0, 1, 1)


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
    resource = driver_resource(MODEL, Strategy.SUBSCRIPTION)
    text = resources.files("dromon.drivers").joinpath(resource).read_text()
    budgeted = text.replace(
        "fn tool_budget() -> Word { 20 }", "fn tool_budget() -> Word { 1 }"
    )
    assert budgeted != text
    driver = Driver(budgeted)
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
def test_a_subscription_driver_refuses_any_other_credential_before_starting(
    variable: str,
) -> None:
    session = Scripted([init(), result()])
    with pytest.raises(EngineRefused, match=variable):
        run(line(session, environment={variable: "1"}))
    assert session.prompts == []
    assert session.options == []


@pytest.mark.parametrize("strategy", [Strategy.API, Strategy.FLEXIBLE])
@pytest.mark.parametrize("variable", PROVIDER_ENVIRONMENT)
def test_every_driver_refuses_a_cloud_provider_before_starting(
    strategy: Strategy, variable: str
) -> None:
    with pytest.raises(EngineRefused, match=variable):
        check_environment({variable: "1"}, strategy)


@pytest.mark.parametrize("strategy", [Strategy.API, Strategy.FLEXIBLE])
@pytest.mark.parametrize("variable", API_ENVIRONMENT)
def test_metered_drivers_allow_an_api_credential_in_the_environment(
    strategy: Strategy, variable: str
) -> None:
    check_environment({variable: "1"}, strategy)


def test_an_empty_variable_does_not_count() -> None:
    check_environment({"ANTHROPIC_API_KEY": ""}, Strategy.SUBSCRIPTION)


def test_every_known_key_source_maps_to_a_credential() -> None:
    assert credential_of("none") is Credential.SUBSCRIPTION
    assert {credential_of(source) for source in KEY_SOURCES} == set(Credential)
    assert credential_of("something new") is None


CREDENTIAL_CASES: list[tuple[Strategy, str, bool]] = [
    (Strategy.SUBSCRIPTION, "none", True),
    (Strategy.SUBSCRIPTION, "ANTHROPIC_API_KEY", False),
    (Strategy.SUBSCRIPTION, "apiKeyHelper", False),
    (Strategy.SUBSCRIPTION, "/login managed key", False),
    (Strategy.API, "none", False),
    (Strategy.API, "ANTHROPIC_API_KEY", True),
    (Strategy.FLEXIBLE, "none", True),
    (Strategy.FLEXIBLE, "apiKeyHelper", True),
]


@pytest.mark.parametrize(("strategy", "source", "accepted"), CREDENTIAL_CASES)
def test_the_driver_decides_on_the_credential_the_engine_reports(
    strategy: Strategy, source: str, accepted: bool
) -> None:
    session = Scripted([init(source=source), say(TextBlock("x")), result()])
    target = line(session, Driver.for_model(MODEL, strategy))
    expected = credential_of(source)
    if accepted:
        assert run(target).credential is expected
        assert session.interrupts == 0
    else:
        with pytest.raises(EngineRefused, match=f"the {strategy} driver refuses"):
            run(target)
        assert session.interrupts == 1


def test_an_unrecognized_credential_source_is_refused() -> None:
    session = Scripted([init(source=""), result()])
    with pytest.raises(EngineRefused, match="unrecognized credential source"):
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
    target = ClaudeCodeLine(Driver.for_model(MODEL, Strategy.SUBSCRIPTION), Path.cwd())
    report = asyncio.run(target.run_turn("Reply with the single word: ready"))
    assert report.subtype == "success"
    assert report.capabilities is not None
    assert report.credential is Credential.SUBSCRIPTION
    assert report.capabilities.model == MODEL
