"""The Antigravity adapter, driven by a scripted ``agy`` stream.

Antigravity is not installed on the operator's machine, so the events follow the
operator's summary of its headless documentation, fetched on 2026-10-26.
"""

import asyncio
import json
from collections import deque
from collections.abc import AsyncGenerator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import antigravity, cli, exits
from dromon.activity import ActivityKind, ActivityLog, Observer
from dromon.antigravity import (
    ISOLATION_REFUSAL,
    KEY_FILE,
    SETTINGS,
    SETTINGS_FILES,
    TOOLS_REFUSAL,
    AntigravityLine,
    agy_arguments,
    check_settings,
    check_workspace,
    engine_environment,
    line_refusal,
    unsupported,
)
from dromon.driver import (
    ENGINE_OF,
    Action,
    ActionTag,
    Credential,
    Driver,
    Event,
    Strategy,
)
from dromon.engine import EngineRefused, TurnReport
from dromon.jsonlines import Connection, ConnectionFactory
from dromon.lineconfig import LineConfig
from dromon.sandbox import parse_sandbox
from dromon.scheduler import LineEngine, LineSpec, engine_factory

AGY = "antigravity:gemini-3.1-pro"


def driver(strategy: Strategy = Strategy.SUBSCRIPTION) -> Driver:
    return Driver.for_model(AGY, strategy)


class SubscriptionAssumed(Driver):
    """The packaged driver, except that it takes the unverified credential to be the
    subscription, which no packaged driver does. It lets the stream's handling be
    tested past the credential."""

    def step(self, event: Event) -> Action:
        if event == Event("credential", Credential.UNVERIFIED.value):
            event = Event("credential", Credential.SUBSCRIPTION.value)
        return super().step(event)


async def _record(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "ok"}]}


SEND: SdkMcpTool[Any] = SdkMcpTool(
    name="send_message", description="Send.", input_schema={}, handler=_record
)


LINE_CWD = "the line's working directory"
"""Stands in an init event for the working directory the fake is started in."""


def init(
    model: str = "gemini-3.1-pro",
    mode: str = "default",
    *,
    cwd: str = LINE_CWD,
    tools: object = (),
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "init",
        "cwd": cwd,
        "permission_mode": mode,
        "model": model,
    }
    if tools is not None:
        event["tools"] = [] if tools == () else tools
    return event


def response(text: str, index: int = 1) -> dict[str, Any]:
    return {
        "type": "step_update",
        "step_index": index,
        "state": "ACTIVE",
        "step_type": "agent_response",
        "text_delta": text,
    }


def tool(name: str = "read_file") -> dict[str, Any]:
    return {
        "type": "step_update",
        "step_index": 2,
        "state": "ACTIVE",
        "step_type": "tool",
        "tool_name": name,
    }


def result(status: str = "SUCCESS", conversation: str = "conv-1") -> dict[str, Any]:
    return {
        "type": "result",
        "conversation_id": conversation,
        "status": status,
        "response": "done",
        "duration_seconds": 1.0,
        "num_turns": 1,
        "usage": {"input_tokens": 100, "output_tokens": 5, "model": "x"},
    }


class FakeAgy:
    """A scripted ``agy --output-format stream-json``."""

    def __init__(
        self, events: Sequence[dict[str, Any]] | None = None, *, hang: bool = False
    ) -> None:
        self.queue: deque[dict[str, Any]] = deque(
            [init(), response("do"), response("ne"), result()]
            if events is None
            else events
        )
        self.hang = hang
        """Whether the stream stays open after its events until it is stopped."""
        self.stopped = asyncio.Event()
        self.arguments: list[str] = []
        self.environment: dict[str, str] = {}
        self.cwd = ""
        self.stops = 0
        self.started = False

    async def send(self, message: Mapping[str, object]) -> None:
        raise AssertionError("print mode takes no input")

    async def receive(self) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        if self.queue:
            record = self.queue.popleft()
            if record.get("cwd") == LINE_CWD:
                record = {**record, "cwd": self.cwd}
            return record
        if self.hang:
            await self.stopped.wait()
        return None

    async def stop(self) -> None:
        self.stops += 1
        self.queue.clear()
        self.stopped.set()


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def line(
    server: FakeAgy,
    home: Path,
    *,
    config: LineConfig | None = None,
    strategy: Strategy = Strategy.SUBSCRIPTION,
    session_id: str | None = None,
    environment: Mapping[str, str] | None = None,
    observe: Observer | None = None,
    partial: bool = True,
    tools: Sequence[SdkMcpTool[Any]] = (),
    assumed: bool = True,
    turn_seconds: float | None = None,
    unverified: bool = False,
    loaded: Driver | None = None,
) -> AntigravityLine:
    @asynccontextmanager
    async def connect(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        server.cwd = str(cwd)
        server.started = True
        server.arguments = arguments
        server.environment = dict(env)
        yield server

    workdir = home.parent / "work"
    workdir.mkdir(exist_ok=True)
    extra: dict[str, Any] = {} if observe is None else {"observe": observe}
    if turn_seconds is not None:
        extra["turn_seconds"] = turn_seconds
    if loaded is None:
        loaded = (
            SubscriptionAssumed.for_model(AGY, strategy)
            if assumed
            else driver(strategy)
        )
    return AntigravityLine(
        loaded,
        workdir,
        config=config or LineConfig(),
        mcp_tools=list(tools),
        session_id=session_id,
        environment={"PATH": "/usr/bin"} if environment is None else environment,
        home=home,
        connection_factory=connect,
        partial_isolation=partial,
        unverified_credential=unverified,
        **extra,
    )


def run(target: AntigravityLine, prompt: str = "hello") -> TurnReport:
    return asyncio.run(target.run_turn(prompt))


def test_the_model_is_engine_qualified() -> None:
    assert ENGINE_OF[AGY] == antigravity.ENGINE_NAME
    for strategy in Strategy:
        loaded = driver(strategy)
        assert loaded.step(Event("init", "gemini-3.1-pro")).tag is ActionTag.ACCEPT
        assert loaded.step(Event("init", AGY)).tag is ActionTag.REFUSE


def test_every_turn_is_refused_until_partial_isolation_is_accepted(
    home: Path,
) -> None:
    server = FakeAgy()
    with pytest.raises(EngineRefused, match="--accept-partial-isolation"):
        run(line(server, home, partial=False))
    assert not server.started
    assert "approval" in ISOLATION_REFUSAL


def test_the_packaged_drivers_refuse_the_unverified_credential_before_agy_starts(
    home: Path,
) -> None:
    """Antigravity review, F2. The stream names no credential source, so the
    credential is unverified, and no packaged driver accepts it."""
    for strategy in Strategy:
        verdict = driver(strategy).step(Event("credential", "unverified"))
        assert verdict.tag is ActionTag.REFUSE
    server = FakeAgy()
    with pytest.raises(EngineRefused, match="refuses the unverified credential"):
        run(line(server, home, assumed=False))
    assert not server.started


class KeyReported(Driver):
    """The packaged driver, except that it hears a key wherever it is told the
    subscription, so the subscription driver refuses it."""

    def step(self, event: Event) -> Action:
        if event == Event("credential", Credential.SUBSCRIPTION.value):
            event = Event("credential", Credential.API.value)
        return super().step(event)


def test_the_operators_confirmation_gives_the_packaged_driver_the_subscription(
    home: Path,
) -> None:
    """The operator's decision of 2026-10-26. With the confirmation the packaged
    driver runs the turn, which still reports the credential as unverified."""
    server = FakeAgy()
    report = run(line(server, home, assumed=False, unverified=True))
    assert server.started
    assert report.subtype == "success"
    assert report.credential is Credential.UNVERIFIED
    refused = FakeAgy()
    keyed = KeyReported.for_model(AGY, Strategy.SUBSCRIPTION)
    with pytest.raises(EngineRefused, match="refuses the subscription the operator"):
        run(line(refused, home, unverified=True, loaded=keyed))
    assert not refused.started


def test_the_operators_confirmation_does_not_pass_the_isolation_refusal(
    home: Path,
) -> None:
    server = FakeAgy()
    with pytest.raises(EngineRefused, match="--accept-partial-isolation"):
        run(line(server, home, partial=False, assumed=False, unverified=True))
    assert not server.started


def test_dromons_tools_cannot_reach_an_antigravity_line(home: Path) -> None:
    server = FakeAgy()
    with pytest.raises(EngineRefused, match="MCP server"):
        run(line(server, home, tools=[SEND]))
    assert not server.started
    assert line_refusal(LineConfig()) == TOOLS_REFUSAL
    assert "built-in" in (line_refusal(LineConfig(builtin_tools=("Read",))) or "")


def test_a_line_takes_no_builtin_tools_or_claude_code_features() -> None:
    assert unsupported(LineConfig()) is None
    assert "built-in" in (unsupported(LineConfig(builtin_tools=("Bash",))) or "")
    assert "compact" in (unsupported(LineConfig(allow_compact=True)) or "")
    sandboxed = LineConfig(sandbox=parse_sandbox({"enabled": True}, operator=True))
    assert "sandbox" in (unsupported(sandboxed) or "")


def test_only_a_subscription_without_google_credentials_runs() -> None:
    environment = {
        "PATH": "/usr/bin",
        "GEMINI_API_KEY": "k",
        "GOOGLE_APPLICATION_CREDENTIALS": "/sa.json",
        "GOOGLE_CLOUD_PROJECT": "p",
        "CLOUDSDK_CONFIG": "/c",
        "OTHER_API_KEY": "k",
        "DROMON_LINE_TOKEN": "t",
        "AWS_ACCESS_KEY_ID": "a",
        "AWS_SECRET_ACCESS_KEY": "s",
        "AZURE_CLIENT_SECRET": "s",
        "ARM_CLIENT_SECRET": "s",
    }
    assert engine_environment(environment, Strategy.SUBSCRIPTION) == {
        "PATH": "/usr/bin"
    }
    for strategy in (Strategy.API, Strategy.FLEXIBLE):
        with pytest.raises(EngineRefused, match="only the subscription"):
            engine_environment(environment, strategy)


@pytest.mark.parametrize("relative", SETTINGS_FILES)
def test_settings_that_approve_tools_are_refused(home: Path, relative: Path) -> None:
    """Re-review, N2, adds the Gemini tools' own user settings."""
    check_settings(home)
    path = home / relative
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permissions": {"allow": []}, "theme": "x"}))
    check_settings(home)
    path.write_text(json.dumps({"permissions": {"allow": ["command(git)"]}}))
    with pytest.raises(EngineRefused, match="permissions.allow"):
        check_settings(home)
    path.write_text(json.dumps({"permissions": ["x"]}))
    with pytest.raises(EngineRefused, match="not an object"):
        check_settings(home)
    path.write_text("{")
    with pytest.raises(EngineRefused, match="not JSON"):
        check_settings(home)


def test_a_gemini_key_file_refuses_the_turn_before_agy_starts(home: Path) -> None:
    """Re-review, N2. The file's existence alone refuses, whatever it holds, and the
    operator's confirmation of the credential does not pass it."""
    assert SETTINGS in SETTINGS_FILES and KEY_FILE.parent == SETTINGS.parents[1]
    key = home / KEY_FILE
    key.parent.mkdir(parents=True)
    key.write_text("")
    with pytest.raises(EngineRefused, match=r"\.env exists"):
        check_settings(home)
    server = FakeAgy()
    with pytest.raises(EngineRefused, match=r"\.env exists"):
        run(line(server, home, assumed=False, unverified=True))
    assert not server.started
    key.unlink()
    key.symlink_to(home / "absent")
    with pytest.raises(EngineRefused, match=r"\.env exists"):
        check_settings(home)


@pytest.mark.parametrize("name", antigravity.WORKSPACE_DIRECTORIES)
def test_a_workspace_that_may_configure_antigravity_is_refused(
    tmp_path: Path, name: str
) -> None:
    """Antigravity review, F3. The home directory's own are checked by
    ``check_settings`` alone."""
    home = tmp_path / "home"
    workspace = home / "repo" / "sub"
    workspace.mkdir(parents=True)
    (home / name).mkdir()
    check_workspace(workspace, home)
    (home / "repo" / name).mkdir()
    with pytest.raises(EngineRefused, match=rf"repo/\{name}"):
        check_workspace(workspace, home)
    server = FakeAgy()
    target = line(server, home)
    (target.cwd / name).mkdir()
    with pytest.raises(EngineRefused, match="has not read"):
        run(target)
    assert not server.started


def test_the_arguments_attach_the_prompt_and_map_effort() -> None:
    arguments = agy_arguments(
        "gemini-3.1-pro", "max", conversation="conv-1", prompt="-dash"
    )
    assert arguments[:2] == ["--output-format", "stream-json"]
    assert arguments[arguments.index("--effort") + 1] == "high"
    assert arguments[arguments.index("--conversation") + 1] == "conv-1"
    assert arguments[arguments.index("--print-timeout") + 1] == "60m"
    assert "--sandbox" in arguments
    assert arguments[-1] == "--prompt=-dash"
    assert "--dangerously-skip-permissions" not in arguments
    assert "--conversation" not in agy_arguments(
        "m", "low", conversation=None, prompt="p"
    )


def test_a_plain_turn_reports_text_usage_and_conversation(home: Path) -> None:
    target = line(FakeAgy(), home)
    report = run(target)
    assert report.subtype == "success"
    assert report.text == "done"
    assert report.credential is Credential.UNVERIFIED
    assert report.session_id == "conv-1" == target.session_id
    assert report.usage == {"input_tokens": 100, "output_tokens": 5}
    assert report.usage_note == antigravity.ANTIGRAVITY_USAGE_NOTE
    assert report.cost_estimate_usd is None


def test_instructions_open_a_new_conversation_only(home: Path) -> None:
    workdir = home.parent / "work"
    workdir.mkdir(exist_ok=True)
    (workdir / "notes.md").write_text("Be brief.")
    config = LineConfig(instructions=("notes.md",))
    server = FakeAgy()
    run(line(server, home, config=config))
    assert (
        server.arguments[-1] == "--prompt=Contents of notes.md:\n\nBe brief.\n\nhello"
    )
    resumed = FakeAgy()
    run(line(resumed, home, config=config, session_id="conv-1"))
    assert resumed.arguments[-1] == "--prompt=hello"


def test_a_continued_conversation_must_be_the_same(home: Path) -> None:
    server = FakeAgy([init(), result(conversation="conv-2")])
    with pytest.raises(EngineRefused, match="continued"):
        run(line(server, home, session_id="conv-1"))
    with pytest.raises(EngineRefused, match="invalid"):
        run(line(FakeAgy(), home, session_id="--x"))


def test_an_unexpected_model_or_mode_stops_the_turn(home: Path) -> None:
    server = FakeAgy([init(model="gemini-3.8-flash"), result()])
    with pytest.raises(EngineRefused, match="refuses the engine model"):
        run(line(server, home))
    assert server.stops == 1
    with pytest.raises(EngineRefused, match="permission mode"):
        run(line(FakeAgy([init(mode="skip_permissions"), result()]), home))


def test_init_must_offer_no_tool_and_run_in_the_lines_directory(home: Path) -> None:
    """Antigravity review, F4."""
    for event, reason in (
        (init(tools=["read_file"]), "offers tools"),
        (init(tools=None), "offers tools"),
        (init(tools="all"), "offers tools"),
        (init(cwd="/elsewhere"), "runs in '/elsewhere'"),
        ({**init(), "cwd": None}, "runs in None"),
    ):
        server = FakeAgy([event, response("x"), result()])
        with pytest.raises(EngineRefused, match=reason):
            run(line(server, home))
        assert server.stops == 1


@pytest.mark.parametrize(
    "event",
    [
        tool("read_file"),
        tool("run_command"),
        {**response("x"), "subagent_info": {"a": 1}},
        {**response("x"), "subagent_info": None},
        {**response("x"), "tool_info": {"kind": "read"}},
        {**response("x"), "tool_name": ""},
    ],
)
def test_any_tool_or_subagent_stops_and_refuses_the_turn(
    home: Path, event: dict[str, Any]
) -> None:
    server = FakeAgy([init(), event, result()])
    with pytest.raises(EngineRefused, match="not given|subagent"):
        run(line(server, home))
    assert server.stops == 1


@pytest.mark.parametrize(
    "event",
    [
        {**response("x"), "step_type": "thinking"},
        {**response("x"), "step_type": "tool_call"},
        {k: v for k, v in response("x").items() if k != "step_type"},
        {"type": "progress"},
        {"type": None},
    ],
)
def test_a_record_or_step_of_an_unknown_shape_is_refused(
    home: Path, event: dict[str, Any]
) -> None:
    """Antigravity review, F1. Only shapes the adapter names pass."""
    server = FakeAgy([init(), event, result()])
    with pytest.raises(EngineRefused, match="does not know"):
        run(line(server, home))
    assert server.stops == 1


def test_a_turn_past_its_deadline_is_stopped_and_refused(home: Path) -> None:
    server = FakeAgy([init(), response("still going")], hang=True)
    with pytest.raises(EngineRefused, match="deadline of 0.05 seconds"):
        run(line(server, home, turn_seconds=0.05))
    assert server.stops == 1


def test_statuses_and_missing_events_are_reported(home: Path) -> None:
    for status, subtype in (("ERROR", "error"), ("CANCELED", "interrupted")):
        report = run(line(FakeAgy([init(), result(status)]), home))
        assert report.subtype == subtype
    with pytest.raises(EngineRefused, match="without a result"):
        run(line(FakeAgy([init(), response("x")]), home))
    with pytest.raises(EngineRefused, match="before its init event"):
        run(line(FakeAgy([response("x"), init(), result()]), home))
    with pytest.raises(EngineRefused, match="ended before its init event"):
        run(line(FakeAgy([]), home))


def test_the_turn_streams_to_the_observer(home: Path) -> None:
    log = ActivityLog()
    run(line(FakeAgy(), home, observe=log.observer("alpha")))
    assert [(entry.kind, entry.text) for entry in log.after("alpha", 0)] == [
        (ActivityKind.TEXT, "done")
    ]


def test_the_factory_builds_a_refusing_line(tmp_path: Path) -> None:
    build = engine_factory(str(tmp_path))
    engine: LineEngine = build(LineSpec("probe", AGY, Strategy.SUBSCRIPTION), [])
    assert isinstance(engine, AntigravityLine)
    assert engine.partial_isolation is False
    with pytest.raises(EngineRefused, match="partial-isolation"):
        asyncio.run(engine.run_turn("ready?"))
    accepting = engine_factory(str(tmp_path), accept_partial_isolation=True)
    probe = accepting(LineSpec("probe", AGY, Strategy.SUBSCRIPTION), [])
    assert isinstance(probe, AntigravityLine) and probe.partial_isolation is True
    assert probe.unverified_credential is False
    confirming = engine_factory(
        str(tmp_path),
        accept_partial_isolation=True,
        accept_unverified_credential=True,
    )
    confirmed = confirming(LineSpec("probe", AGY, Strategy.SUBSCRIPTION), [])
    assert isinstance(confirmed, AntigravityLine)
    assert confirmed.unverified_credential is True
    sandboxed = engine_factory(
        str(tmp_path), project_sandbox=parse_sandbox({"enabled": True}, operator=True)
    )
    with pytest.raises(EngineRefused, match="Antigravity lines take no sandbox"):
        sandboxed(LineSpec("probe", AGY, Strategy.SUBSCRIPTION), [])


def test_the_probes_flag_reaches_antigravity_and_its_credential_refuses(
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The flag is accepted for an Antigravity model, and the turn is then refused on
    its unverified credential before ``agy`` is started."""
    base = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "probe"]
    assert cli.main([*base, "--model", AGY]) == exits.FAILURE
    assert "--accept-partial-isolation" in capsys.readouterr().err
    flagged = [*base, "--model", AGY, "--accept-partial-isolation"]
    assert cli.main(flagged) == exits.FAILURE
    assert "unverified credential" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main([*base, "--help"])
    assert "Antigravity's tools" in " ".join(capsys.readouterr().out.split())


def test_the_probes_credential_flag_is_refused_outside_its_scope(
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Only an Antigravity model, and only with ``--accept-partial-isolation``."""
    base = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "probe"]
    flag = "--accept-unverified-credential"
    cursor_model = next(m for m, e in ENGINE_OF.items() if e == "cursor-agent")
    haiku = [*base, "--model", "claude-haiku-5-5", flag]
    cursor = [*base, "--model", cursor_model, "--accept-partial-isolation", flag]
    for flagged in (haiku, cursor):
        assert cli.main(flagged) == exits.FAILURE
        assert "applies only to Antigravity models" in capsys.readouterr().err
    assert cli.main([*base, "--model", AGY, flag]) == exits.FAILURE
    assert "only together with --accept-partial-isolation" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main([*base, "--help"])
    shown = " ".join(capsys.readouterr().out.split())
    assert flag in shown and "step 2 of the verify-first list" in shown
    # Re-review, N4. Neither flag restricts the strategy.
    assert "--strategy api still bills per token" in shown
    assert "It does not change the strategy" in shown


def test_the_probe_runs_antigravity_with_both_flags(
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The scripted ``agy`` stands in for the harness, and the home directory is a
    fresh one, so no real settings or harness are reached."""
    server = FakeAgy()

    @asynccontextmanager
    async def connect(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        server.cwd = str(cwd)
        server.started = True
        yield server

    def factory(command: str, *, keep_input: bool) -> ConnectionFactory:
        assert command == antigravity.COMMAND and not keep_input
        return connect

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(antigravity, "json_lines_factory", factory)
    base = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "probe"]
    flags = ["--accept-partial-isolation", "--accept-unverified-credential"]
    assert cli.main([*base, "--model", AGY, *flags]) == exits.OK
    shown = capsys.readouterr().out
    assert server.started
    assert "credential unverified" in shown and "turn success" in shown
