"""The Claude Code engine adapter, driven by scripted sessions instead of the SDK.

Only ``test_a_live_turn_on_the_subscription`` reaches the real engine. It uses the
operator's subscription, so it needs both the ``live`` marker selected and
``DROMON_LIVE_TESTS=1``. A later ``-m`` on the command line replaces the configured
deselection, and the variable keeps such a run from calling the engine by accident.
"""

import asyncio
import json
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
from typing import Any, cast

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
from dromon import engine as engine_module
from dromon import hookpolicy
from dromon.driver import Action, Credential, Driver, Event, Strategy, driver_resource
from dromon.launcher import StopPolicy
from dromon.priority import SteeredDriver
from dromon.lineconfig import DEFAULT_CONFIG, LineConfig, McpServer, ToolPolicy
from dromon.engine import (
    API_ENVIRONMENT,
    BILLING_LIMIT,
    ConfinementBreach,
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
    launch_environment,
    view_image_hooks,
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


def result(
    subtype: str = "success", turns: int = 1, message: str | None = None
) -> ResultMessage:
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=turns,
        session_id="session-1",
        total_cost_usd=0.001,
        result=message,
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
    budget_reported: Callable[[int, str | None, str, str], object] = (
        engine_module.ignore_budget_report
    ),
) -> ClaudeCodeLine:
    return ClaudeCodeLine(
        driver or Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        Path("/work"),
        environment=environment or {},
        session_factory=session.factory,
        observe=observe,
        budget_reported=budget_reported,
    )


def run(target: ClaudeCodeLine, prompt: str = "hello") -> TurnReport:
    return asyncio.run(target.run_turn(prompt))


def test_claude_bash_refuses_missing_shell_before_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("dromon.engine_environment.CLAUDE_FALLBACK_SHELLS", ())
    session = Scripted([])
    target = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        Path("/work"),
        config=LineConfig(builtin_tools=("Bash",)),
        environment={},
        session_factory=session.factory,
    )
    with pytest.raises(
        EngineRefused, match="Claude Code shell unavailable: no executable bash or zsh"
    ):
        run(target)
    assert session.options == []


def test_claude_shell_override_is_forwarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module, "claude_cli", lambda: "/bin/claude")

    def fake_launch(target: str, policy: StopPolicy) -> dict[str, str]:
        del policy
        return {"DROMON_TEST_TARGET": target}

    monkeypatch.setattr(
        engine_module.launcher,
        "launch_environment",
        fake_launch,
    )
    environment = launch_environment(None, StopPolicy.KILL_ALL, "/bin/bash")
    assert environment["SHELL"] == "/bin/bash"
    assert environment["CLAUDE_CODE_SHELL"] == "/bin/bash"
    assert environment[engine_module.launcher.relayed("SHELL")] == "/bin/bash"
    assert (
        environment[engine_module.launcher.relayed("CLAUDE_CODE_SHELL")] == "/bin/bash"
    )


def test_claude_line_without_shell_tool_drops_daemon_shell_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module, "claude_cli", lambda: "/bin/claude")

    def fake_launch(target: str, policy: StopPolicy) -> dict[str, str]:
        del policy
        return {"DROMON_TEST_TARGET": target}

    monkeypatch.setattr(engine_module.launcher, "launch_environment", fake_launch)
    environment = launch_environment(None, StopPolicy.KILL_ALL, None)
    assert "SHELL" not in environment
    assert "CLAUDE_CODE_SHELL" not in environment
    assert environment[engine_module.launcher.relayed("SHELL")] == ""
    assert environment[engine_module.launcher.relayed("CLAUDE_CODE_SHELL")] == ""

    filtered = {
        "SHELL": "/daemon/unvalidated/shell",
        "CLAUDE_CODE_SHELL": "/daemon/unvalidated/override",
        **environment,
    }
    engine_module.launcher.unrelayed(filtered)
    assert "SHELL" not in filtered
    assert "CLAUDE_CODE_SHELL" not in filtered


def test_claude_plain_line_forwards_no_inherited_shell_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(engine_module, "claude_cli", lambda: "/bin/claude")
    session = Scripted([init(), say(TextBlock("ready")), result()])
    target = line(
        session,
        environment={
            "SHELL": "/daemon/unvalidated/shell",
            "CLAUDE_CODE_SHELL": "/daemon/unvalidated/override",
        },
    )
    run(target)
    environment = session.options[0].env
    assert environment[engine_module.launcher.relayed("SHELL")] == ""
    assert environment[engine_module.launcher.relayed("CLAUDE_CODE_SHELL")] == ""
    assert "SHELL" not in environment
    assert "CLAUDE_CODE_SHELL" not in environment


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


@pytest.mark.parametrize("reported_in", ["assistant", "result"])
def test_a_success_result_reporting_missing_sign_in_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    reported_in: str,
) -> None:
    message = "Not logged in · Please run /login"
    messages: list[Message] = [init()]
    if reported_in == "assistant":
        messages.extend([say(TextBlock(message)), result()])
    else:
        messages.append(result(turns=0, message=message))
    session = Scripted(messages)
    result_events: list[Event] = []
    original_step = Driver.step

    def record_step(driver: Driver, event: Event) -> Action:
        if event.tag == "result":
            result_events.append(event)
        return original_step(driver, event)

    monkeypatch.setattr(Driver, "step", record_step)
    target = line(session)
    with pytest.raises(EngineRefused, match="not signed in"):
        run(target)
    assert session.interrupts == (1 if reported_in == "assistant" else 0)
    assert len(session.prompts) == 1
    assert not result_events


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


def test_the_gate_names_its_allowlist_rule_when_it_refuses_a_tool() -> None:
    gate = permission_gate(DEFAULT_CONFIG, frozenset())
    denied = asyncio.run(gate("Bash", {"command": "ls"}, CONTEXT))
    assert isinstance(denied, PermissionResultDeny)
    assert "tool_policy='allowlist'" in denied.message
    assert "does not allow it" in denied.message


def test_the_gate_names_the_sandbox_rule_when_it_refuses_an_unsandboxed_call() -> None:
    gate = permission_gate(
        DEFAULT_CONFIG,
        frozenset(),
        {"enabled": True, "allowUnsandboxedCommands": False},
    )
    denied = asyncio.run(gate("Bash", {"dangerouslyDisableSandbox": True}, CONTEXT))
    assert isinstance(denied, PermissionResultDeny)
    assert "sandbox.allowUnsandboxedCommands is not true" in denied.message
    assert "dangerouslyDisableSandbox=true" in denied.message


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
    status: RateLimitStatus,
    overage: RateLimitStatus | None = None,
    utilization: float | None = None,
) -> RateLimitEvent:
    info = RateLimitInfo(
        status,
        RESETS,
        "five_hour",
        utilization=utilization,
        overage_status=overage,
    )
    return RateLimitEvent(info, "u-1", "session-1")


def test_a_known_positive_claude_window_reports_its_remaining_budget() -> None:
    reports: list[tuple[int, str | None, str, str]] = []
    target = line(
        Scripted([init(), rate_limit("allowed", utilization=0.425), result()]),
        budget_reported=lambda remaining, reset, note, window: reports.append(
            (remaining, reset, note, window)
        ),
    )
    run(target)
    assert reports == [
        (
            57,
            "2026-10-28T15:00:00Z",
            "Claude reported 57% remaining in its five_hour rate-limit window",
            "five_hour",
        )
    ]


@pytest.mark.parametrize("utilized_percent", range(100))
def test_claude_window_whole_percent_rounding_is_exact(
    utilized_percent: int,
) -> None:
    reports: list[tuple[int, str | None, str, str]] = []
    run(
        line(
            Scripted(
                [
                    init(),
                    rate_limit("allowed", utilization=utilized_percent / 100),
                    result(),
                ]
            ),
            budget_reported=lambda remaining, reset, note, window: reports.append(
                (remaining, reset, note, window)
            ),
        )
    )
    assert reports == [
        (
            100 - utilized_percent,
            "2026-10-28T15:00:00Z",
            "Claude reported "
            f"{100 - utilized_percent}% remaining in its five_hour rate-limit window",
            "five_hour",
        )
    ]


@pytest.mark.parametrize(
    ("status", "utilization"),
    [
        ("rejected", 0.5),
        ("allowed", 1.0),
        ("allowed", -0.1),
        ("allowed", 1.1),
        ("allowed", float("nan")),
        ("allowed", float("inf")),
    ],
)
def test_unknown_or_exhausted_windows_do_not_report_a_positive_budget(
    status: RateLimitStatus, utilization: float
) -> None:
    reports: list[tuple[int, str | None, str, str]] = []
    run(
        line(
            Scripted([init(), rate_limit(status, utilization=utilization), result()]),
            budget_reported=lambda remaining, reset, note, window: reports.append(
                (remaining, reset, note, window)
            ),
        )
    )
    assert reports == []


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


# Round 11. The ViewImage capability on Claude Code.


def test_view_image_is_offered_to_the_engine_as_read() -> None:
    config = LineConfig(builtin_tools=("ViewImage",))
    session = Scripted([init(tools=["Read"]), result()])
    run(configured(config, session))
    assert session.options[0].tools == ["Read"]


def test_a_line_without_view_image_or_read_is_refused_an_engine_that_offers_read() -> (
    None
):
    session = Scripted([init(tools=["Read"]), say(TextBlock("x")), result()])
    with pytest.raises(
        EngineRefused, match="offers tools the line was not given: Read"
    ):
        run(configured(LineConfig(builtin_tools=("Edit",)), session))
    assert session.interrupts == 1


# Round 14, item 5. Every Read on a view-only line meets the rule before the engine's
# own permission handling, because the engine allows a Read in its working directory
# without calling the permission callback.


def hook_decision(
    config: LineConfig, workdir: Path, tool_input: dict[str, Any]
) -> dict[str, Any]:
    hooks = view_image_hooks(config, workdir)
    assert hooks is not None
    (matcher,) = hooks["PreToolUse"]
    assert matcher.matcher == "Read"
    (hook,) = matcher.hooks
    event: dict[str, Any] = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Read",
        "tool_input": tool_input,
        "tool_use_id": "u1",
    }
    return dict(asyncio.run(hook(event, "u1", {"signal": None})))  # type: ignore[arg-type]


def denied(decision: dict[str, Any]) -> str | None:
    output = decision.get("hookSpecificOutput")
    if not output:
        return None
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny"
    return str(output["permissionDecisionReason"])


def test_a_view_only_line_has_a_hook_before_every_read(tmp_path: Path) -> None:
    session = Scripted([init(tools=["Read"]), result()])
    run(configured(LineConfig(builtin_tools=("ViewImage",)), session))
    hooks = session.options[0].hooks
    assert hooks is not None and list(hooks) == ["PreToolUse"]


@pytest.mark.parametrize(
    "tools",
    [(), ("Read",), ("Read", "ViewImage"), ("Bash",), ("Edit", "Write")],
)
def test_no_other_line_has_the_view_image_hook(tools: tuple[str, ...]) -> None:
    assert view_image_hooks(LineConfig(builtin_tools=tools), Path("/work")) is None


def test_the_hook_refuses_what_the_engine_would_read_without_asking(
    tmp_path: Path,
) -> None:
    from imagesamples import png

    config = LineConfig(builtin_tools=("ViewImage",))
    (tmp_path / "word.png").write_bytes(png())
    (tmp_path / "word.txt").write_text("a text file")
    (tmp_path / "renamed.png").write_text("a text file renamed to a png")
    (tmp_path / "big.png").write_bytes(png(5 * 1024 * 1024 + 1))
    (tmp_path / ".dromon").mkdir()
    (tmp_path / ".dromon" / "x.png").write_bytes(png())
    outside = tmp_path.parent / "outside.png"
    outside.write_bytes(png())
    assert denied(hook_decision(config, tmp_path, {"file_path": "word.png"})) is None
    assert (
        denied(
            hook_decision(config, tmp_path, {"file_path": str(tmp_path / "word.png")})
        )
        is None
    )
    for path, why in (
        ("word.txt", "by its name"),
        ("renamed.png", "does not begin"),
        ("big.png", "over the limit"),
        (".dromon/x.png", "never reads"),
        (str(outside), "outside"),
        ("missing.png", "does not exist"),
    ):
        reason = denied(hook_decision(config, tmp_path, {"file_path": path}))
        assert reason is not None and "ViewImage" in reason and why in reason, path
    for given in ({}, {"file_path": None}, {"file_path": 3}, {"path": "word.png"}):
        assert denied(hook_decision(config, tmp_path, given)) is not None


def test_a_hook_that_gets_no_tool_input_refuses(tmp_path: Path) -> None:
    hooks = view_image_hooks(LineConfig(builtin_tools=("ViewImage",)), tmp_path)
    assert hooks is not None
    hook = hooks["PreToolUse"][0].hooks[0]
    event: Any = {"hook_event_name": "PreToolUse", "tool_name": "Read"}
    decision = cast(dict[str, Any], asyncio.run(hook(event, None, {"signal": None})))  # type: ignore[arg-type]
    assert denied(decision) is not None


# Review of a9995d8, item 1. The hook fails closed, a Read that completed without a
# hook call ends the turn, an ask rule sends every Read to the gate, and a policy that
# switches hooks off refuses a view-only line.


def hook_of(
    config: LineConfig, workdir: Path, record: Callable[[str], None] | None = None
) -> Callable[..., Any]:
    hooks = view_image_hooks(config, workdir, record)
    assert hooks is not None
    return hooks["PreToolUse"][0].hooks[0]


def test_a_hook_that_raises_denies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*arguments: object) -> str:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(engine_module, "image_refusal", broken)
    hook = hook_of(LineConfig(builtin_tools=("ViewImage",)), tmp_path)
    event: Any = {"tool_name": "Read", "tool_input": {"file_path": "a.png"}}
    decision = cast(dict[str, Any], asyncio.run(hook(event, "u1", {"signal": None})))
    reason = denied(decision)
    assert reason is not None and "RuntimeError" in reason
    assert "secret detail" not in reason


def test_a_hook_whose_record_raises_denies(tmp_path: Path) -> None:
    def broken(identifier: str) -> None:
        raise RuntimeError(identifier)

    hook = hook_of(LineConfig(builtin_tools=("ViewImage",)), tmp_path, broken)
    event: Any = {"tool_name": "Read", "tool_input": {"file_path": "a.png"}}
    decision = cast(dict[str, Any], asyncio.run(hook(event, "u1", {"signal": None})))
    assert denied(decision) is not None


def test_a_hook_records_the_call_even_when_it_denies(tmp_path: Path) -> None:
    seen: list[str] = []
    hook = hook_of(LineConfig(builtin_tools=("ViewImage",)), tmp_path, seen.append)
    event: Any = {"tool_name": "Read", "tool_input": {"file_path": "missing.png"}}
    decision = cast(dict[str, Any], asyncio.run(hook(event, "u7", {"signal": None})))
    assert denied(decision) is not None
    # The identifier may also come from the input when the callback is given none.
    event2: Any = {"tool_input": {}, "tool_use_id": "u8"}
    asyncio.run(hook(event2, None, {"signal": None}))
    assert seen == ["u7", "u8"]


def read_result(identifier: str) -> UserMessage:
    return UserMessage(
        content=[ToolResultBlock(tool_use_id=identifier, content="x", is_error=False)]
    )


def read_call(identifier: str, name: str = "Read") -> AssistantMessage:
    return say(ToolUseBlock(id=identifier, name=name, input={"file_path": "a.png"}))


VIEW_ONLY = LineConfig(builtin_tools=("ViewImage",))


def test_a_read_that_completed_without_the_hook_ends_the_turn() -> None:
    session = Scripted(
        [init(tools=["Read"]), read_call("r1"), read_result("r1"), result()]
    )
    target = configured(VIEW_ONLY, session)
    with pytest.raises(
        ConfinementBreach, match="without the ViewImage hook having run"
    ):
        run(target)
    assert session.interrupts == 1


class Hooking(Scripted):
    """A session that, as the engine does, calls the registered hook for a call
    before it delivers that call's result."""

    def __init__(self, messages: Sequence[Message], hooked: Sequence[str]) -> None:
        super().__init__(messages)
        self.hooked = list(hooked)

    async def receive_response(self) -> AsyncIterator[Message]:
        hooks = self.options[-1].hooks
        assert hooks is not None
        hook = hooks["PreToolUse"][0].hooks[0]
        for message in self.messages:
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ToolUseBlock) and block.id in self.hooked:
                        event: Any = {"tool_name": "Read", "tool_input": block.input}
                        await hook(event, block.id, {"signal": None})
            yield message


def test_a_read_with_a_hook_call_is_not_refused() -> None:
    session = Hooking(
        [
            init(tools=["Read"]),
            read_call("r1"),
            read_result("r1"),
            read_call("r2"),
            read_result("r2"),
            result(),
        ],
        hooked=["r1", "r2"],
    )
    report = run(configured(VIEW_ONLY, session))
    assert report.interrupted_for is None
    assert session.interrupts == 0


def test_only_the_read_that_skipped_the_hook_is_refused() -> None:
    session = Hooking(
        [
            init(tools=["Read"]),
            read_call("r1"),
            read_result("r1"),
            read_call("r2"),
            read_result("r2"),
            result(),
        ],
        hooked=["r1"],
    )
    with pytest.raises(ConfinementBreach, match="without the ViewImage hook"):
        run(configured(VIEW_ONLY, session))


def test_other_tools_and_other_lines_need_no_hook_call() -> None:
    session = Scripted(
        [init(tools=["Read"]), read_call("e1", "Echo"), read_result("e1"), result()]
    )
    assert run(configured(VIEW_ONLY, session)).interrupted_for is None
    plain = Scripted(
        [init(tools=["Read"]), read_call("r1"), read_result("r1"), result()]
    )
    run(configured(LineConfig(builtin_tools=("Read",)), plain))
    assert plain.interrupts == 0


def test_a_view_only_line_asks_before_every_read() -> None:
    session = Scripted([init(tools=["Read"]), result()])
    run(configured(VIEW_ONLY, session))
    settings = json.loads(str(session.options[0].settings))
    assert settings["permissions"]["ask"] == ["Read"]


def test_the_ask_rule_joins_the_operator_rules_once() -> None:
    merged = engine_module.flag_settings(
        {"permissions": {"ask": ["Bash", "Read", 3], "deny": ["Edit"]}}, ("Read",)
    )
    assert merged["permissions"] == {"ask": ["Bash", "Read"], "deny": ["Edit"]}
    replaced = engine_module.flag_settings({"permissions": "nonsense"}, ("Read",))
    assert replaced["permissions"] == {"ask": ["Read"]}


@pytest.mark.parametrize("tools", [(), ("Read",), ("Read", "ViewImage")])
def test_no_other_line_asks_before_a_read(tools: tuple[str, ...]) -> None:
    offered = ["Read"] if tools else []
    session = Scripted([init(tools=offered), result()])
    run(configured(LineConfig(builtin_tools=tools), session))
    assert "permissions" not in json.loads(str(session.options[0].settings))


@pytest.mark.parametrize("key", ["disableAllHooks", "allowManagedHooksOnly"])
def test_a_managed_policy_that_switches_hooks_off_refuses_a_view_only_line(
    key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "managed-settings.json").write_text(json.dumps({key: True}))
    monkeypatch.setattr(hookpolicy, "MANAGED_DIRECTORIES", (tmp_path,))
    session = Scripted([init(tools=["Read"]), result()])
    with pytest.raises(EngineRefused, match=f"sets {key}") as refused:
        run(configured(VIEW_ONLY, session))
    # A refusal before the turn is not a breach, which the scheduler holds for
    # unconditionally. The line did nothing outside its confinement.
    assert not isinstance(refused.value, ConfinementBreach)
    assert session.options == []
    # A line that is not view-only is not affected by it.
    other = Scripted([init(tools=["Read"]), result()])
    run(configured(LineConfig(builtin_tools=("Read",)), other))


def test_the_operators_settings_document_cannot_switch_hooks_off() -> None:
    session = Scripted([init(tools=["Read"]), result()])
    target = ClaudeCodeLine(
        Driver.for_model(MODEL, Strategy.SUBSCRIPTION),
        Path("/work"),
        config=VIEW_ONLY,
        environment={},
        session_factory=session.factory,
        settings={"disableAllHooks": True},
    )
    with pytest.raises(EngineRefused, match="settings document sets disableAllHooks"):
        run(target)
