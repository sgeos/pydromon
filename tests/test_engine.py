"""The Claude Code engine adapter, driven by scripted sessions instead of the SDK.

Only ``test_a_live_turn_on_the_subscription`` reaches the real engine. It uses the
operator's subscription, so it needs both the ``live`` marker selected and
``DROMON_LIVE_TESTS=1``. A later ``-m`` on the command line replaces the configured
deselection, and the variable keeps such a run from calling the engine by accident.
"""

import asyncio
import re
import os
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    Message,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    RateLimitInfo,
    SdkMcpTool,
    ToolPermissionContext,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    UserMessage,
    ThinkingBlock,
    ToolUseBlock,
)

from claude_agent_sdk.types import RateLimitStatus
from dromon.activity import ActivityKind, ActivityLog, Observer, ignore
from dromon.driver import Credential, Driver, Strategy, driver_resource
from dromon.priority import SteeredDriver
from dromon.lineconfig import DEFAULT_CONFIG, LineConfig, McpServer, ToolPolicy
from dromon.engine import (
    API_ENVIRONMENT,
    BILLING_LIMIT,
    EnginePlanLimit,
    KEY_SOURCES,
    MCP_SERVER_NAME,
    NON_SUBSCRIPTION_ENVIRONMENT,
    PROVIDER_ENVIRONMENT,
    AgentSession,
    ClaudeCodeLine,
    EngineRefused,
    TurnReport,
    check_environment,
    credential_of,
    permission_gate,
)

MODEL = "claude-haiku-5-5"
"""Tests use the least expensive model, as the operator directs."""


async def _echo(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": str(args)}]}


ECHO: SdkMcpTool[Any] = SdkMcpTool(
    name="echo", description="Echo.", input_schema={"type": "object"}, handler=_echo
)
CONTEXT = ToolPermissionContext()


def init(
    model: str = MODEL, source: str = "none", tools: list[str] | None = None
) -> SystemMessage:
    return SystemMessage(
        subtype="init",
        data={
            "model": model,
            "apiKeySource": source,
            "claude_code_version": "2.1.310",
            "tools": tools or [],
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
    observe: Observer = ignore,
) -> ClaudeCodeLine:
    return ClaudeCodeLine(
        driver or Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        Path("/work"),
        environment=environment or {},
        session_factory=session.factory,
        observe=observe,
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


def test_the_turn_is_reported_to_the_observer_as_it_runs() -> None:
    session = Scripted(
        [
            init(),
            say(ThinkingBlock("", ""), TextBlock("rea"), TextBlock("dy")),
            say(ToolUseBlock("t1", "mcp__dromon__send", {})),
            result(),
        ]
    )
    log = ActivityLog()
    run(line(session, observe=log.observer("alpha")))
    # Whole text blocks are not chunks, so each stays its own entry.
    assert [(entry.kind, entry.text) for entry in log.after("alpha", 0)] == [
        (ActivityKind.THINKING, ""),
        (ActivityKind.TEXT, "rea"),
        (ActivityKind.TEXT, "dy"),
        (ActivityKind.TOOL, "mcp__dromon__send"),
    ]


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
    assert options.env is not None
    assert options.env["ENABLE_TOOL_SEARCH"] == "true"


def test_the_claude_code_adapter_refuses_a_driver_for_another_engine() -> None:
    codex = Driver.for_model("gpt-6-luna", Strategy.SUBSCRIPTION)
    with pytest.raises(EngineRefused, match="written for the codex engine"):
        ClaudeCodeLine(codex, Path("/work"))


def test_the_next_turn_resumes_the_engine_session() -> None:
    session = Scripted([init(), result()])
    target = line(session)
    run(target)
    run(target)
    assert session.options[1].resume == "session-1"


def test_the_driver_budget_interrupts_a_turn_once() -> None:
    resource = driver_resource(MODEL, Strategy.SUBSCRIPTION)
    text = resources.files("dromon.drivers").joinpath(resource).read_text()
    budgeted = re.sub(
        r"fn tool_budget\(\) -> Word \{ \d+ \}", "fn tool_budget() -> Word { 1 }", text
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
    # Claude Code names its key source, so its credential is never unverified.
    known = set(Credential) - {Credential.UNVERIFIED}
    assert {credential_of(source) for source in KEY_SOURCES} == known
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


# Tool isolation.


def test_every_line_is_isolated_from_other_mcp_servers_and_prompts() -> None:
    session = Scripted([init(), result()])
    run(line(session))
    options = session.options[0]
    assert options.strict_mcp_config is True
    assert options.mcp_servers == {}
    assert options.allowed_tools == []
    assert options.can_use_tool is not None


def test_a_line_with_dromon_tools_serves_and_allows_exactly_them() -> None:
    session = Scripted([init(tools=["mcp__dromon__echo"]), result()])
    target = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        Path("/work"),
        mcp_tools=[ECHO],
        environment={},
        session_factory=session.factory,
    )
    run(target)
    options = session.options[0]
    assert options.allowed_tools == []
    assert target.own_tools == {"mcp__dromon__echo"}
    assert isinstance(options.mcp_servers, dict)
    assert set(options.mcp_servers) == {MCP_SERVER_NAME}


def test_an_engine_offering_unexpected_tools_is_refused() -> None:
    session = Scripted(
        [
            init(tools=["mcp__claude_ai_Gmail__send_message"]),
            say(TextBlock("x")),
            result(),
        ]
    )
    with pytest.raises(EngineRefused, match="mcp__claude_ai_Gmail__send_message"):
        run(line(session))
    assert session.interrupts == 1


def test_the_gate_allows_exactly_the_given_tools() -> None:
    gate = permission_gate(DEFAULT_CONFIG, frozenset({"mcp__dromon__echo"}))
    allowed = asyncio.run(gate("mcp__dromon__echo", {}, CONTEXT))
    denied = asyncio.run(gate("Bash", {"command": "ls"}, CONTEXT))
    assert isinstance(allowed, PermissionResultAllow)
    assert isinstance(denied, PermissionResultDeny)
    assert "Bash is not available" in denied.message


# Per-line configuration.

GITHUB = McpServer("stdio", command="gh-mcp")


def configured(
    config: LineConfig,
    session: Scripted,
    *,
    environment: Mapping[str, str] | None = None,
    secret_names: tuple[str, ...] = (),
) -> ClaudeCodeLine:
    return ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        Path("/work"),
        config=config,
        environment=environment or {},
        secret_names=secret_names,
        session_factory=session.factory,
    )


def test_configured_servers_and_built_in_tools_reach_the_engine() -> None:
    config = LineConfig(builtin_tools=("Read",), mcp_servers=(("github", GITHUB),))
    session = Scripted([init(tools=["Read", "mcp__github__create_issue"]), result()])
    run(configured(config, session))
    options = session.options[0]
    assert options.tools == ["Read"]
    assert options.mcp_servers == {"github": {"type": "stdio", "command": "gh-mcp"}}
    assert options.strict_mcp_config is True


def test_mcp_secret_resolution_is_scoped_to_the_line_grant() -> None:
    config = LineConfig(
        mcp_servers=(
            (
                "docs",
                McpServer(
                    "http",
                    url="https://docs.example/mcp",
                    headers=(("Authorization", "Bearer ${LINE_A_TOKEN}"),),
                ),
            ),
        )
    )
    session = Scripted([init(), result()])
    run(
        configured(
            config,
            session,
            environment={"LINE_A_TOKEN": "a-secret", "LINE_B_TOKEN": "b-secret"},
            secret_names=("LINE_A_TOKEN",),
        )
    )
    options = session.options[0]
    assert options.mcp_servers == {
        "docs": {
            "type": "http",
            "url": "https://docs.example/mcp",
            "headers": {"Authorization": "Bearer a-secret"},
        }
    }
    assert "LINE_A_TOKEN" not in options.env
    assert "LINE_B_TOKEN" not in options.env


def test_a_line_cannot_resolve_another_lines_secret() -> None:
    config = LineConfig(
        mcp_servers=(
            (
                "docs",
                McpServer(
                    "http",
                    url="https://docs.example/mcp",
                    headers=(("Authorization", "Bearer ${LINE_B_TOKEN}"),),
                ),
            ),
        )
    )
    session = Scripted([])
    with pytest.raises(EngineRefused, match="does not set LINE_B_TOKEN"):
        run(
            configured(
                config,
                session,
                environment={"LINE_A_TOKEN": "a-secret", "LINE_B_TOKEN": "b-secret"},
                secret_names=("LINE_A_TOKEN",),
            )
        )
    assert session.options == []


def test_inheriting_the_account_turns_off_strict_mcp_and_accepts_its_tools() -> None:
    config = LineConfig(inherit_account_mcp=True)
    session = Scripted([init(tools=["mcp__claude_ai_Gmail__send_message"]), result()])
    run(configured(config, session))
    assert session.options[0].strict_mcp_config is False


@pytest.mark.parametrize(
    ("offered", "expected"),
    [
        ("Bash", "Bash"),
        ("mcp__other__tool", "mcp__other__tool"),
        ("mcp__dromon__unknown", "mcp__dromon__unknown"),
    ],
)
def test_an_engine_offering_more_than_the_configuration_is_refused(
    offered: str, expected: str
) -> None:
    config = LineConfig(builtin_tools=("Read",), mcp_servers=(("github", GITHUB),))
    session = Scripted([init(tools=["Read", offered]), result()])
    with pytest.raises(EngineRefused, match=expected):
        run(configured(config, session))


def test_the_gate_applies_the_line_policy() -> None:
    allowlist = permission_gate(
        LineConfig(allowed_tools=("mcp__github__*",)), frozenset()
    )
    allow_all = permission_gate(
        LineConfig(tool_policy=ToolPolicy.ALLOW_ALL), frozenset()
    )
    assert isinstance(
        asyncio.run(allowlist("mcp__github__create_issue", {}, CONTEXT)),
        PermissionResultAllow,
    )
    assert isinstance(asyncio.run(allowlist("Bash", {}, CONTEXT)), PermissionResultDeny)
    assert isinstance(
        asyncio.run(allow_all("Bash", {}, CONTEXT)), PermissionResultAllow
    )


# The safe point of priority mail, decided by the operator on 2026-10-27. A turn ends
# once a tool result is delivered and not at the text before the tool use.

type Steered = Callable[[str, Strategy], tuple[SteeredDriver, list[int]]]
REASON = "priority mail m-1 from @ana"


def test_priority_mail_ends_the_turn_at_the_tool_result_and_not_the_text(
    steered: Steered,
) -> None:
    driver, asked = steered(MODEL, Strategy.SUBSCRIPTION)
    session = Scripted(
        [
            init(),
            say(TextBlock("I will read it"), tool("Read")),
            UserMessage(content=[ToolResultBlock(tool_use_id="t-Read", content="ok")]),
            say(TextBlock("done")),
            result("error_during_execution"),
        ]
    )
    report = run(line(session, driver))
    assert asked == [1]
    assert report.interrupted_for == REASON
    assert session.interrupts == 1


# Plan limits, roles step 3. A rejected window holds a turn that failed, and a billing
# error holds any turn, as ``rejected_limit`` and ``BILLING_ERROR`` describe.

RESETS = 1_793_199_600
"""2026-10-28T15:00:00Z in seconds since the epoch."""


def rate_limit(
    status: RateLimitStatus, overage: RateLimitStatus | None = None
) -> RateLimitEvent:
    info = RateLimitInfo(status, RESETS, "five_hour", overage_status=overage)
    return RateLimitEvent(info, "u-1", "session-1")


def failed() -> ResultMessage:
    message = result("success")
    message.is_error = True
    return message


def test_a_failed_turn_after_a_rejected_window_is_a_plan_limit() -> None:
    session = Scripted([init(), rate_limit("rejected"), failed()])
    with pytest.raises(EnginePlanLimit, match="five_hour window") as raised:
        run(line(session))
    assert raised.value.resets_at == "2026-10-28T15:00:00Z"


def test_a_turn_that_succeeds_past_a_rejected_window_is_kept() -> None:
    session = Scripted([init(), rate_limit("rejected"), say(TextBlock("ok")), result()])
    target = line(session)
    assert run(target).text == "ok"
    # The engine reports a change of status, not each turn, so the next turn that
    # fails is held at the limit without a report of its own.
    session.messages = list[Message]([init(), failed()])
    with pytest.raises(EnginePlanLimit):
        run(target)


def test_a_window_served_by_overage_or_allowed_again_is_no_limit() -> None:
    for messages in (
        [init(), rate_limit("rejected", "allowed"), failed()],
        [init(), rate_limit("rejected"), rate_limit("allowed"), failed()],
        [init(), failed()],
    ):
        report = run(line(Scripted(messages)))
        assert report.subtype == "success"


def test_a_billing_error_is_a_plan_limit_even_in_a_turn_that_succeeds() -> None:
    billing = AssistantMessage(content=[], model=MODEL, error="billing_error")
    session = Scripted([init(), billing, result()])
    with pytest.raises(EnginePlanLimit, match=BILLING_LIMIT) as raised:
        run(line(session))
    assert raised.value.resets_at is None
    # Another error is not classified, so the turn ends as the engine ended it.
    other = AssistantMessage(content=[], model=MODEL, error="rate_limit")
    assert run(line(Scripted([init(), other, result()]))).subtype == "success"
