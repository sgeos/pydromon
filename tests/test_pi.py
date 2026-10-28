"""The Pi adapter, driven by a scripted ``pi --mode rpc``.

No test here runs Pi, since a turn spends the operator's subscription. The script
follows Pi 1.4.0's RPC documentation, ``rpc.md``, ``rpc-commands.md`` and ``json.md``,
and the shapes in its ``rpc-types.d.ts``.
"""

import asyncio
import json
import stat
import sys
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import pi
from dromon.activity import ActivityKind, ActivityLog, Observer
from dromon.agents import LineTokens
from dromon.daemon import check_engines
from dromon.driver import (
    ENGINE_OF,
    MODELS,
    Credential,
    Driver,
    Event,
    ActionTag,
    Strategy,
    driver_resource,
)
from dromon.engine import EngineRefused, TurnReport
from dromon.priority import SteeredDriver
from dromon.grok import bridge_command
from dromon.jsonlines import Connection, json_lines_factory
from dromon.lineconfig import LineConfig, WorkdirAccess
from dromon.pi import (
    tool_calls_in,
    DIALOG_REFUSED,
    AGENT_DIR,
    PiLine,
    check_agent_dir,
    credential_of,
    engine_environment,
    extension_path,
    harness_model,
    pi_arguments,
    pi_auth_check,
    unsupported,
)
from dromon.projectconfig import parse
from dromon.sandbox import parse_sandbox
from dromon.scheduler import LineEngine, LineSpec, engine_factory

PI = "pi:openai/gpt-6-luna"
ENGINE_MODEL = "openai/gpt-6-luna"


def driver(strategy: Strategy = Strategy.SUBSCRIPTION) -> Driver:
    return Driver.for_model(PI, strategy)


async def _record(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "ok"}]}


SEND: SdkMcpTool[Any] = SdkMcpTool(
    name="send_message", description="Send.", input_schema={}, handler=_record
)
OWN = "mcp__dromon__send_message"


def usage(input_tokens: int, output: int, *, cost: float = 0.01) -> dict[str, Any]:
    return {
        "input": input_tokens,
        "output": output,
        "cacheRead": 100,
        "cacheWrite": 10,
        "totalTokens": input_tokens + output + 110,
        "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "total": cost,
        },
    }


def text_delta(text: str) -> dict[str, Any]:
    return {
        "type": "message_update",
        "usage": usage(0, 0, cost=0),
        "assistantMessageEvent": {
            "type": "text_delta",
            "contentIndex": 0,
            "delta": text,
        },
    }


def assistant(
    text: str = "done", *, stop: str = "stop", spent: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "provider": "openai",
            "model": "gpt-6-luna",
            "usage": spent or usage(1000, 50),
            "stopReason": stop,
        },
    }


def tool(name: str) -> dict[str, Any]:
    return {
        "type": "tool_execution_start",
        "toolCallId": "call-1",
        "toolName": name,
        "args": {},
    }


TURN_START: dict[str, Any] = {"type": "turn_start"}


def reply(text: str = "done") -> list[dict[str, Any]]:
    return [TURN_START, text_delta(text), assistant(text)]


class FakePi:
    """A scripted ``pi --mode rpc``. It answers commands as Pi documents them, and
    after an accepted prompt it plays ``turn`` and then settles."""

    def __init__(
        self,
        turn: Sequence[dict[str, Any]] = (),
        *,
        model: object = None,
        session_file: str | None = None,
        disposition: str = "started",
        settle: bool = True,
        hang: bool = False,
    ) -> None:
        self.turn = list(turn) if turn else reply()
        self.model: object = (
            {"provider": "openai", "id": "gpt-6-luna"} if model is None else model
        )
        self.session_file = session_file
        self.disposition = disposition
        self.settle = settle
        self.hang = hang
        """Whether the run goes on, its stream open, until it is aborted."""
        self.sent: list[dict[str, Any]] = []
        self.queue: deque[dict[str, Any]] = deque()
        self.arguments: list[str] = []
        self.environment: dict[str, str] = {}
        self.started = False

    def commands(self) -> list[str]:
        return [str(m.get("type")) for m in self.sent]

    async def send(self, message: Mapping[str, object]) -> None:
        sent = dict(message)
        self.sent.append(sent)
        kind = sent.get("type")
        base = {"id": sent.get("id"), "type": "response", "command": kind}
        if kind == "get_state":
            data: dict[str, object] = {
                "thinkingLevel": "medium",
                "isStreaming": False,
                "sessionId": "s1",
                "messageCount": 0,
            }
            if self.model is not None:
                data["model"] = self.model
            if self.session_file is not None:
                data["sessionFile"] = self.session_file
            self.queue.append({**base, "success": True, "data": data})
        elif kind == "prompt":
            self.queue.append(
                {**base, "success": True, "data": {"disposition": self.disposition}}
            )
            if self.disposition == "started":
                self.queue.append({"type": "agent_start"})
                self.queue.extend(self.turn)
                if self.settle and not self.hang:
                    self.queue.append({"type": "agent_end", "willRetry": False})
                    self.queue.append({"type": "agent_settled", "aborted": False})
        elif kind == "abort":
            # Pi ends an aborted run with an aborted assistant message, then settles.
            self.queue = deque(
                m for m in self.queue if m.get("type") not in ("agent_settled",)
            )
            self.queue.append({**base, "success": True})
            if self.settle:
                self.queue.append({"type": "agent_settled", "aborted": True})

    async def receive(self) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        if self.hang:
            while not self.queue:
                await asyncio.sleep(0.01)
        return self.queue.popleft() if self.queue else None

    async def stop(self) -> None:
        raise AssertionError("a Pi run is aborted, never stopped")


def auth(answer: Mapping[str, object] | None = None) -> pi.AuthChecker:
    async def check(
        provider: str, environment: Mapping[str, str]
    ) -> Mapping[str, object]:
        del environment
        return answer or {"status": "ready", "provider": provider, "authType": "oauth"}

    return check


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / AGENT_DIR / "sessions").mkdir(parents=True)
    write_settings(home / AGENT_DIR)
    return home


def write_settings(agent_dir: Path, **settings: object) -> None:
    """The agent directory's settings, with cache warming off as Dromon requires."""
    document = {"cacheWarming": "off", **settings}
    (agent_dir / "settings.json").write_text(json.dumps(document))


def line(
    server: FakePi,
    home: Path,
    *,
    config: LineConfig | None = None,
    strategy: Strategy = Strategy.SUBSCRIPTION,
    session_id: str | None = None,
    environment: Mapping[str, str] | None = None,
    answer: Mapping[str, object] | None = None,
    observe: Observer | None = None,
    compacted: Callable[[Mapping[str, int | str | None]], object] | None = None,
    tools: bool = True,
    turn_seconds: float | None = None,
) -> PiLine:
    @asynccontextmanager
    async def connect(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        del cwd
        server.started = True
        server.arguments = arguments
        server.environment = dict(env)
        yield server

    workdir = home.parent / "work"
    workdir.mkdir(exist_ok=True)
    extra: dict[str, Any] = {}
    if observe is not None:
        extra["observe"] = observe
    if compacted is not None:
        extra["compacted"] = compacted
    if turn_seconds is not None:
        extra["turn_seconds"] = turn_seconds
    return PiLine(
        driver(strategy),
        workdir,
        config=config or LineConfig(),
        mcp_tools=[SEND],
        session_id=session_id,
        bridge=["dromon-bridge", "--line", "alpha"] if tools else [],
        line_token="tok" if tools else None,  # noqa: S106, a test token
        environment={"PATH": "/usr/bin"} if environment is None else environment,
        home=home,
        connection_factory=connect,
        auth_check=auth(answer),
        extension=Path("/packaged/pi_extension.js"),
        **extra,
    )


def run(target: PiLine, prompt: str = "hello") -> TurnReport:
    return asyncio.run(target.run_turn(prompt))


# Names, configuration and environment.


def test_pi_models_are_named_by_engine_and_harness_identifier() -> None:
    assert ENGINE_OF[PI] == pi.ENGINE_NAME
    assert PI in MODELS
    assert harness_model(PI) == ENGINE_MODEL
    for wrong in ("gpt-6-luna", "pi:gpt-6-luna", "pi:/x", "pi:openai/"):
        with pytest.raises(EngineRefused, match="not a Pi model name"):
            harness_model(wrong)


def test_every_packaged_driver_has_its_own_resource_name() -> None:
    names = [driver_resource(model, s) for model in ENGINE_OF for s in Strategy]
    assert len(names) == len(set(names))
    assert driver_resource(PI, Strategy.API) == "pi__openai__gpt_6_luna_api.kel"


@pytest.mark.parametrize("strategy", list(Strategy))
def test_the_pi_drivers_accept_only_the_model_pi_reports(strategy: Strategy) -> None:
    loaded = driver(strategy)
    assert loaded.settings.engine == "pi"
    assert loaded.step(Event("init", ENGINE_MODEL)).tag is ActionTag.ACCEPT
    assert loaded.step(Event("init", PI)).tag is ActionTag.REFUSE
    assert loaded.step(Event("init", "openai/gpt-6-sol")).tag is ActionTag.REFUSE


def test_a_pi_line_takes_no_builtin_tools_and_none_of_claude_codes_features() -> None:
    assert unsupported(LineConfig()) is None
    assert "read-only working directory" in (
        unsupported(LineConfig(workdir_access=WorkdirAccess.READ)) or ""
    )
    assert "no sandbox" in (unsupported(LineConfig(builtin_tools=("Read",))) or "")
    assert "MCP servers" in (unsupported(LineConfig(inherit_account_mcp=True)) or "")
    assert "Claude Code's" in (unsupported(LineConfig(claude_code_prompt=True)) or "")
    assert "compact" in (unsupported(LineConfig(allow_compact=True)) or "")
    assert "keep" in (unsupported(LineConfig(keep_session=True)) or "")
    sandboxed = LineConfig(sandbox=parse_sandbox({"enabled": True}, operator=True))
    assert "sandbox" in (unsupported(sandboxed) or "")


def test_a_subscription_environment_has_no_credential_variables() -> None:
    environment = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "OPENAI_API_KEY": "k",
        "ANTHROPIC_FEDERATION_RULE_ID": "r",
        "AWS_PROFILE": "p",
        "SOME_NEW_PROVIDER_API_KEY": "k",
        "GITHUB_TOKEN": "t",
        "CLOUDSDK_CONFIG": "/c",
        "AWS_EC2_METADATA_SERVICE_ENDPOINT": "http://elsewhere",
        "AZURE_OPENAI_ENDPOINT": "https://elsewhere",
        "LLAMA_BASE_URL": "http://elsewhere",
        "PI_CODING_AGENT_DIR": "/elsewhere",
        "DROMON_LINE_TOKEN": "stale",
        # Cursor review, F4, applied to Pi: any cloud provider's variable.
        "AWS_SHARED_CREDENTIALS_FILE": "/c",
        "AWS_DEFAULT_REGION": "r",
        "AZURE_CLIENT_SECRET": "s",
        "ARM_CLIENT_SECRET": "s",
        "OCI_CLI_KEY_FILE": "/k",
        "DATABASE_PASSWORD": "p",
    }
    chosen = engine_environment(environment, Strategy.SUBSCRIPTION)
    # Pi review, F5: endpoints and credential locations go with the keys.
    assert chosen == {"PATH": "/usr/bin", "HOME": "/home/x"}
    metered = engine_environment(environment, Strategy.API)
    assert metered["OPENAI_API_KEY"] == "k"
    assert "PI_CODING_AGENT_DIR" not in metered
    with pytest.raises(EngineRefused, match="PI_PACKAGE_DIR"):
        engine_environment({"PI_PACKAGE_DIR": "/x"}, Strategy.SUBSCRIPTION)


# The agent directory.


def test_a_missing_agent_directory_is_refused_with_how_to_sign_in(
    tmp_path: Path,
) -> None:
    with pytest.raises(EngineRefused, match="PI_CODING_AGENT_DIR=.* pi, then /login"):
        check_agent_dir(tmp_path / "absent")


@pytest.mark.parametrize(
    "name", ["mcp.json", "models.json", "SYSTEM.md", "APPEND_SYSTEM.md"]
)
def test_an_agent_directory_that_adds_servers_or_providers_is_refused(
    tmp_path: Path, name: str
) -> None:
    write_settings(tmp_path)
    (tmp_path / name).write_text("{}")
    with pytest.raises(EngineRefused, match=name):
        check_agent_dir(tmp_path)


def test_only_safe_settings_are_accepted(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    write_settings(tmp_path, defaultProvider="openai", theme="dark")
    check_agent_dir(tmp_path)
    for unsafe in ("packages", "shellCommandPrefix", "httpProxy", "sessionDir"):
        write_settings(tmp_path, theme="dark", **{unsafe: "x"})
        with pytest.raises(EngineRefused, match=unsafe):
            check_agent_dir(tmp_path)
    settings.write_text("[]")
    with pytest.raises(EngineRefused, match="not a JSON object"):
        check_agent_dir(tmp_path)
    settings.write_text("{")
    with pytest.raises(EngineRefused, match="not JSON"):
        check_agent_dir(tmp_path)


def test_cache_warming_must_be_switched_off(tmp_path: Path) -> None:
    with pytest.raises(EngineRefused, match="settings.json is missing.*cacheWarming"):
        check_agent_dir(tmp_path)
    for value in ("streaming", "idle", None):
        (tmp_path / "settings.json").write_text(json.dumps({"cacheWarming": value}))
        with pytest.raises(EngineRefused, match="cache warming off"):
            check_agent_dir(tmp_path)
    (tmp_path / "settings.json").write_text(json.dumps({"theme": "dark"}))
    with pytest.raises(EngineRefused, match="cache warming off"):
        check_agent_dir(tmp_path)
    write_settings(tmp_path)
    check_agent_dir(tmp_path)


def test_the_credential_store_is_never_opened(tmp_path: Path) -> None:
    write_settings(tmp_path)
    store = tmp_path / "auth.json"
    store.write_text("{}")
    store.chmod(0)
    try:
        check_agent_dir(tmp_path)
    finally:
        store.chmod(stat.S_IRUSR | stat.S_IWUSR)


# The command line.


def test_the_arguments_switch_off_every_other_source_of_tools() -> None:
    arguments = pi_arguments(
        ENGINE_MODEL,
        "max",
        agent_dir=Path("/a"),
        extension=Path("/e.js"),
        tools=[OWN],
        session_file="/a/sessions/s.jsonl",
        instructions="Be brief.",
    )
    for flag in (
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--no-context-files",
        "--no-approve",
    ):
        assert flag in arguments
    assert arguments[:2] == ["--mode", "rpc"]
    assert arguments[arguments.index("--model") + 1] == ENGINE_MODEL
    assert arguments[arguments.index("--thinking") + 1] == "max"
    assert arguments[arguments.index("--session-dir") + 1] == "/a/sessions"
    assert arguments[arguments.index("--tools") + 1] == OWN
    assert arguments[arguments.index("--session") + 1] == "/a/sessions/s.jsonl"
    assert arguments[arguments.index("--append-system-prompt") + 1] == "Be brief."
    loaded = [arguments[i + 1] for i, a in enumerate(arguments) if a == "--extension"]
    assert loaded == ["/e.js", "builtin:mcp"]
    assert "--no-tools" not in arguments


def test_a_line_without_tools_gets_none_and_no_mcp() -> None:
    arguments = pi_arguments(
        ENGINE_MODEL,
        "medium",
        agent_dir=Path("/a"),
        extension=Path("/e.js"),
        tools=[],
        session_file=None,
        instructions="",
    )
    assert "--no-tools" in arguments and "--no-mcp" in arguments
    assert "builtin:mcp" not in arguments and "--tools" not in arguments
    assert "--session" not in arguments and "--append-system-prompt" not in arguments


def test_the_packaged_extension_registers_dromon_and_guards_every_call() -> None:
    source = extension_path().read_text(encoding="utf-8")
    assert 'pi.registerMcpServer("dromon"' in source
    assert 'pi.on("tool_call"' in source
    assert "block: true" in source
    for variable in ("DROMON_PI_TOOLS", "DROMON_PI_BRIDGE", "DROMON_LINE_TOKEN"):
        assert variable in source
        assert variable in pi.DROMON_CONTROLLED


# Credentials.


def test_a_subscription_sign_in_and_an_api_key_are_told_apart() -> None:
    ready = {"status": "ready", "provider": "openai"}
    assert credential_of("openai", {**ready, "authType": "oauth"}) is (
        Credential.SUBSCRIPTION
    )
    assert credential_of("openai", {**ready, "authType": "api_key"}) is Credential.API
    with pytest.raises(EngineRefused, match="cannot tell"):
        credential_of(
            "openrouter",
            {"status": "ready", "provider": "openrouter", "authType": "oauth"},
        )
    with pytest.raises(EngineRefused, match="no usable credential"):
        credential_of("openai", {"status": "not_ready", "reason": "x"})
    with pytest.raises(EngineRefused, match="answered for"):
        credential_of("openai", {**ready, "provider": "xai", "authType": "oauth"})
    # Pi review, F7: a Claude plan through Pi is not taken to be a subscription.
    claude = {"status": "ready", "provider": "anthropic", "authType": "oauth"}
    with pytest.raises(EngineRefused, match="cannot tell"):
        credential_of("anthropic", claude)


def test_a_subscription_line_refuses_an_api_key_before_pi_starts(home: Path) -> None:
    server = FakePi()
    answer = {"status": "ready", "provider": "openai", "authType": "api_key"}
    with pytest.raises(EngineRefused, match="refuses the api credential"):
        run(line(server, home, answer=answer))
    assert not server.started


def test_an_api_line_runs_on_an_api_key(home: Path) -> None:
    server = FakePi()
    answer = {"status": "ready", "provider": "openai", "authType": "api_key"}
    report = run(line(server, home, strategy=Strategy.API, answer=answer))
    assert report.credential is Credential.API


def write_fake(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(f"#!{sys.executable}\n{body}")
    script.chmod(0o755)
    return script


def test_the_auth_check_asks_pi_without_printing_the_credential(
    tmp_path: Path,
) -> None:
    record = tmp_path / "argv.json"
    write_fake(
        tmp_path / "bin",
        "pi",
        "import json, os, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps("
        "[sys.argv[1:], os.environ.get('PI_CODING_AGENT_DIR')]))\n"
        "print('Warning: something')\n"
        'print(json.dumps({"status": "ready", "provider": "openai", '
        '"authType": "oauth"}))\n',
    )
    environment = {"PATH": str(tmp_path / "bin"), "PI_CODING_AGENT_DIR": "/agent"}
    answer = asyncio.run(pi_auth_check("openai", environment))
    assert answer["authType"] == "oauth"
    argv, agent = json.loads(record.read_text())
    assert argv == ["auth", "check", "--provider", "openai", "--json", "--no-refresh"]
    assert "--credentials" not in argv
    assert agent == "/agent"


def test_an_auth_check_that_is_not_json_is_refused(tmp_path: Path) -> None:
    write_fake(tmp_path / "bin", "pi", "print('ready')\n")
    with pytest.raises(EngineRefused, match="not answer in JSON"):
        asyncio.run(pi_auth_check("openai", {"PATH": str(tmp_path / "bin")}))
    with pytest.raises(EngineRefused, match="not installed"):
        asyncio.run(pi_auth_check("openai", {"PATH": str(tmp_path / "none")}))


# Turns.


def test_a_plain_turn_reports_text_usage_cost_and_credential(home: Path) -> None:
    session = home / AGENT_DIR / "sessions" / "s1.jsonl"
    session.write_text("")
    server = FakePi(session_file=str(session))
    target = line(server, home)
    report = run(target, "the wake prompt")
    assert report.subtype == "success"
    assert report.text == "done"
    assert report.credential is Credential.SUBSCRIPTION
    assert report.num_turns == 1
    assert report.usage == {
        "input_tokens": 1000,
        "output_tokens": 50,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 10,
    }
    assert report.cost_estimate_usd == pytest.approx(0.01)
    assert report.context_tokens == 1110
    assert report.calls[0]["subagent"] is False
    assert report.usage_note == pi.PI_USAGE_NOTE
    assert report.session_id == str(session) == target.session_id
    assert server.commands() == ["get_state", "prompt"]
    assert server.sent[1]["message"] == "the wake prompt"


def test_a_turn_that_reports_no_usage_says_so(home: Path) -> None:
    ended = assistant("x")
    del ended["message"]["usage"]
    report = run(line(FakePi([TURN_START, ended]), home))
    assert report.usage is None and report.cost_estimate_usd is None
    assert report.context_tokens is None and report.usage_note is None


def test_the_environment_carries_the_agent_directory_tools_and_token(
    home: Path,
) -> None:
    server = FakePi()
    target = line(
        server,
        home,
        environment={
            "PATH": "/usr/bin",
            "OPENAI_API_KEY": "k",
            "DAEMON_ONLY_SENTINEL": "not-allowlisted",
        },
    )
    target.mark = "alpha.mark"
    run(target)
    env = server.environment
    assert env["PI_CODING_AGENT_DIR"] == str(home / AGENT_DIR)
    assert json.loads(env["DROMON_PI_TOOLS"]) == [OWN]
    assert json.loads(env["DROMON_PI_BRIDGE"]) == ["dromon-bridge", "--line", "alpha"]
    assert env["DROMON_LINE_TOKEN"] == "tok"  # noqa: S105, a test token
    assert env["DROMON_LINE_MARK"] == "alpha.mark"
    assert "OPENAI_API_KEY" not in env
    assert "DAEMON_ONLY_SENTINEL" not in env
    assert server.arguments[server.arguments.index("--tools") + 1] == OWN


def test_a_line_outside_a_daemon_has_no_tools(home: Path) -> None:
    server = FakePi()
    target = line(server, home, tools=False)
    assert target.own_tools == frozenset()
    run(target)
    assert json.loads(server.environment["DROMON_PI_TOOLS"]) == []
    assert "DROMON_PI_BRIDGE" not in server.environment
    assert "--no-tools" in server.arguments


def test_a_new_session_is_kept_only_once_pi_has_written_it(home: Path) -> None:
    unwritten = home / AGENT_DIR / "sessions" / "lazy.jsonl"
    target = line(FakePi(session_file=str(unwritten)), home)
    report = run(target)
    assert target.session_id is None and report.session_id == ""


def test_the_next_turn_resumes_the_session_file(home: Path) -> None:
    session = home / AGENT_DIR / "sessions" / "s1.jsonl"
    session.write_text("")
    server = FakePi(session_file=str(session))
    run(line(server, home, session_id=str(session)))
    assert server.arguments[server.arguments.index("--session") + 1] == str(
        session.resolve()
    )


def test_a_session_pi_did_not_open_is_refused_before_the_prompt(home: Path) -> None:
    session = home / AGENT_DIR / "sessions" / "s1.jsonl"
    other = home / AGENT_DIR / "sessions" / "s2.jsonl"
    session.write_text("")
    other.write_text("")
    server = FakePi(session_file=str(other))
    with pytest.raises(EngineRefused, match="opened the session"):
        run(line(server, home, session_id=str(session)))
    assert "prompt" not in server.commands()


def test_a_session_that_is_gone_or_elsewhere_is_refused(
    home: Path, tmp_path: Path
) -> None:
    gone = home / AGENT_DIR / "sessions" / "gone.jsonl"
    server = FakePi()
    with pytest.raises(EngineRefused, match="is gone"):
        run(line(server, home, session_id=str(gone)))
    elsewhere = tmp_path / "elsewhere.jsonl"
    elsewhere.write_text("")
    with pytest.raises(EngineRefused, match="is not under"):
        run(line(server, home, session_id=str(elsewhere)))
    assert not server.started


def test_an_unexpected_model_is_refused_before_the_prompt(home: Path) -> None:
    server = FakePi(model={"provider": "openai", "id": "gpt-6-sol"})
    with pytest.raises(EngineRefused, match="refuses the engine model"):
        run(line(server, home))
    assert server.commands() == ["get_state"]


def test_a_prompt_pi_did_not_run_is_refused(home: Path) -> None:
    with pytest.raises(EngineRefused, match="did not start a run"):
        run(line(FakePi(disposition="handled"), home))


def test_the_turn_streams_to_the_observer(home: Path) -> None:
    log = ActivityLog()
    thinking = {
        "type": "message_update",
        "usage": usage(0, 0),
        "assistantMessageEvent": {"type": "thinking_start", "contentIndex": 0},
    }
    turn = [TURN_START, thinking, text_delta("he"), text_delta("llo"), tool(OWN)]
    turn.append(assistant("hello"))
    run(line(FakePi(turn), home, observe=log.observer("alpha")))
    # Streamed deltas extend one entry, and reasoning is shown only as a mark.
    assert [(entry.kind, entry.text) for entry in log.after("alpha", 0)] == [
        (ActivityKind.THINKING, ""),
        (ActivityKind.TEXT, "hello"),
        (ActivityKind.TOOL, OWN),
    ]


def test_dromons_own_tools_are_counted(home: Path) -> None:
    report = run(line(FakePi([TURN_START, tool(OWN), *reply()]), home))
    assert report.tool_uses == (OWN,)


@pytest.mark.parametrize(
    "name", ["bash", "read", "mcp__other__x", "list_mcp_resources"]
)
def test_any_other_tool_aborts_and_refuses_the_turn(home: Path, name: str) -> None:
    server = FakePi([TURN_START, tool(name), *reply()])
    with pytest.raises(EngineRefused, match=f"not given: {name}"):
        run(line(server, home))
    assert server.commands().count("abort") == 1


def test_the_tool_budget_aborts_the_turn_once(home: Path) -> None:
    answer = {"status": "ready", "provider": "openai", "authType": "api_key"}
    budget = driver(Strategy.API).settings.tool_budget
    server = FakePi([TURN_START, *[tool(OWN)] * (budget + 3), *reply()])
    report = run(line(server, home, strategy=Strategy.API, answer=answer))
    assert report.interrupted_for == "tool_budget"
    assert report.subtype == "interrupted"
    assert server.commands().count("abort") == 1


def test_the_drivers_max_turns_is_enforced(home: Path) -> None:
    answer = {"status": "ready", "provider": "openai", "authType": "api_key"}
    limit = driver(Strategy.API).settings.max_turns
    turns = [event for _ in range(limit + 1) for event in reply()]
    server = FakePi(turns)
    report = run(line(server, home, strategy=Strategy.API, answer=answer))
    assert report.interrupted_for == "max_turns"
    assert server.commands().count("abort") == 1


def test_a_run_that_never_settles_after_an_abort_is_refused(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pi, "ABORT_SECONDS", 0.05)
    server = FakePi([TURN_START, tool("bash")], settle=False)

    async def stall() -> dict[str, Any] | None:
        if server.queue:
            return server.queue.popleft()
        await asyncio.sleep(1)
        return None

    server.receive = stall  # type: ignore[method-assign]
    with pytest.raises(EngineRefused, match="not given: bash"):
        run(line(server, home))


def test_an_error_stop_is_reported(home: Path) -> None:
    report = run(line(FakePi([TURN_START, assistant("", stop="error")]), home))
    assert report.subtype == "error"


def test_dialogs_from_extensions_are_cancelled(home: Path) -> None:
    dialog = {"type": "extension_ui_request", "id": "u1", "method": "confirm"}
    notice = {"type": "extension_ui_request", "id": "u2", "method": "notify"}
    server = FakePi([TURN_START, dialog, notice, *reply()])
    log = ActivityLog()
    run(line(server, home, observe=log.observer("alpha")))
    answers = [m for m in server.sent if m.get("type") == "extension_ui_response"]
    assert answers == [{"type": "extension_ui_response", "id": "u1", "cancelled": True}]
    # Row 5 of the T3 Code review. The refused request is surfaced, and the notice,
    # which asks for no decision, is not.
    notices = [
        entry.text
        for entry in log.after("alpha", 0)
        if entry.kind is ActivityKind.NOTICE
    ]
    assert notices == [DIALOG_REFUSED.format("confirm")]


def test_an_extension_error_refuses_the_turn(home: Path) -> None:
    failure = {"type": "extension_error", "extensionPath": "/e.js", "error": "boom"}
    with pytest.raises(EngineRefused, match="extension failed"):
        run(line(FakePi([TURN_START, failure, *reply()]), home))


def test_pis_own_compaction_is_reported_at_once(home: Path) -> None:
    seen: list[Mapping[str, int | str | None]] = []
    ended = {
        "type": "compaction_end",
        "reason": "threshold",
        "result": {
            "tokensBefore": 150000,
            "estimatedTokensAfter": 32000,
            "usage": usage(140000, 2000, cost=0.5),
        },
        "aborted": False,
        "willRetry": False,
    }
    report = run(line(FakePi([ended, *reply()]), home, compacted=seen.append))
    assert seen == [
        {
            "trigger": "auto",
            "before_tokens": 150000,
            "after_tokens": None,
            "after_estimate_tokens": 32000,
            "after_estimate_note": pi.PI_AFTER_ESTIMATE_NOTE,
        }
    ]
    # Pi review, F2: the summary call's tokens and cost are the turn's too.
    assert len(report.calls) == 2
    assert report.usage is not None
    assert report.usage["input_tokens"] == 140000 + 1000
    assert report.cost_estimate_usd == pytest.approx(0.51)
    # The context estimate is the last assistant call's, not the summary's.
    assert report.context_tokens == 1110


def test_a_closed_connection_refuses_the_turn(home: Path) -> None:
    server = FakePi([TURN_START], settle=False)
    with pytest.raises(EngineRefused, match="closed its connection"):
        run(line(server, home))


def instructions_of(text: str) -> Callable[[Sequence[str], Path], str]:
    def read(paths: Sequence[str], root: Path) -> str:
        del paths, root
        return text

    return read


def test_instructions_reach_pi_as_text_and_never_as_a_file_name(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workdir = home.parent / "work"
    workdir.mkdir(exist_ok=True)
    (workdir / "notes.md").write_text("Be brief.")
    config = LineConfig(instructions=("notes.md",))
    server = FakePi()
    run(line(server, home, config=config))
    passed = server.arguments[server.arguments.index("--append-system-prompt") + 1]
    assert passed == "Contents of notes.md:\n\nBe brief."
    # Pi would read a value that names a file, so such text is refused.
    monkeypatch.setattr(pi, "read_instructions", instructions_of("notes.md"))
    with pytest.raises(EngineRefused, match="name a file"):
        run(line(FakePi(), home, config=config))
    monkeypatch.setattr(pi, "MAX_PROMPT_ARGUMENT_BYTES", 4)
    monkeypatch.setattr(pi, "read_instructions", instructions_of("too long"))
    with pytest.raises(EngineRefused, match="exceed 4 bytes"):
        run(line(FakePi(), home, config=config))


def test_the_adapter_refuses_a_driver_for_another_engine(home: Path) -> None:
    claude = Driver.for_model("claude-haiku-5-5", Strategy.SUBSCRIPTION)
    with pytest.raises(EngineRefused, match="written for the claude-code engine"):
        PiLine(claude, home)


# The factory and the project.


def test_the_factory_builds_a_pi_line_with_its_bridge_and_token(tmp_path: Path) -> None:
    tokens = LineTokens()
    socket_path = tmp_path / "run" / "lines.sock"
    build = engine_factory(str(tmp_path), line_tokens=tokens, line_socket=socket_path)
    engine: LineEngine = build(LineSpec("alpha", PI, Strategy.SUBSCRIPTION), [SEND])
    assert isinstance(engine, PiLine)
    assert engine.line_token == tokens.issue("alpha")
    assert list(engine.bridge) == bridge_command(tmp_path, socket_path.parent, "alpha")
    assert engine.own_tools == frozenset({OWN})


def test_a_project_sandbox_never_reaches_a_pi_line(tmp_path: Path) -> None:
    sandbox = parse_sandbox({"enabled": True}, operator=True)
    build = engine_factory(str(tmp_path), project_sandbox=sandbox)
    with pytest.raises(EngineRefused, match="Pi lines take no sandbox"):
        build(LineSpec("alpha", PI, Strategy.SUBSCRIPTION), [])
    project = parse(
        f"version = 1\n[sandbox]\nenabled = true\n[lines.a]\nmodel = '{PI}'\n"
    )
    with pytest.raises(ValueError, match="the lines a, which run on engines other"):
        check_engines(project)


def test_a_turn_past_its_deadline_is_aborted_and_refused(home: Path) -> None:
    """Cursor review, F6, applied to Pi. The run is aborted as an interrupt is."""
    server = FakePi([TURN_START, text_delta("still going")], hang=True)
    with pytest.raises(EngineRefused, match="deadline of 0.2 seconds"):
        run(line(server, home, turn_seconds=0.2))
    assert server.commands() == ["get_state", "prompt", "abort"]


# The process.


def test_json_lines_are_split_on_line_feeds_alone(tmp_path: Path) -> None:
    separator = " "
    write_fake(
        tmp_path / "bin",
        "harness",
        "import json, sys\n"
        f"sys.stdout.write(json.dumps({{'text': 'a{separator}b'}}, ensure_ascii=False)"
        " + '\\r\\n')\n"
        "sys.stdout.write('[1]\\n')\n",
    )
    factory = json_lines_factory("harness", keep_input=False)

    async def read() -> tuple[dict[str, Any] | None, str]:
        async with factory([], tmp_path, {"PATH": str(tmp_path / "bin")}) as harness:
            first = await harness.receive()
            try:
                await harness.receive()
            except EngineRefused as error:
                return first, str(error)
            return first, ""

    first, refusal = asyncio.run(read())
    assert first == {"text": f"a{separator}b"}
    assert "not an object" in refusal


def test_a_harness_that_is_not_installed_is_refused(tmp_path: Path) -> None:
    factory = json_lines_factory("no-such-harness", keep_input=True)

    async def start() -> None:
        async with factory([], tmp_path, {"PATH": str(tmp_path)}):
            pass

    with pytest.raises(EngineRefused, match="not installed"):
        asyncio.run(start())


def test_a_cancelled_turn_keeps_the_cost_pi_reported_for_it(home: Path) -> None:
    """Review N8 of the stop policy's re-review. An operator's interrupt cancels the
    turn, and the cost Pi reported for its calls until then is given once, for the
    line's budget."""
    server = FakePi(
        [TURN_START, assistant("partial", spent=usage(10, 5, cost=0.2))], hang=True
    )
    target = line(server, home)

    async def go() -> None:
        turn = asyncio.ensure_future(target.run_turn("long work"))
        # Bounded: the fake's queue holds three messages after the prompt's answer.
        for _ in range(1000):
            if server.started and not server.queue and "prompt" in server.commands():
                break
            await asyncio.sleep(0.001)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

    asyncio.run(go())
    assert target.take_interrupted_cost() == pytest.approx(0.2)
    assert target.take_interrupted_cost() is None


# The safe point of priority mail, decided by the operator on 2026-10-27. A turn ends
# once a tool result is delivered and not at the text before the tool use.

type Steered = Callable[[str, Strategy], tuple[SteeredDriver, list[int]]]
REASON = "priority mail m-1 from @ana"


def finished(name: str) -> dict[str, Any]:
    return {
        "type": "tool_execution_end",
        "toolCallId": "call-1",
        "toolName": name,
        "result": {},
        "isError": False,
    }


def test_priority_mail_ends_the_turn_at_the_tool_result_and_not_the_text(
    home: Path, steered: Steered
) -> None:
    server = FakePi(
        [
            TURN_START,
            text_delta("I will send it"),
            assistant("I will send it", stop="toolUse"),
            tool(OWN),
            finished(OWN),
            *reply(),
        ]
    )
    target = line(server, home)
    target.driver, asked = steered(PI, Strategy.SUBSCRIPTION)
    report = run(target)
    # The message that stopped for a tool use was not asked, its result was. The
    # fake plays on after the abort, where the steer may be asked again, as the
    # scheduler's steer answers only once.
    assert asked[0] == 1
    assert report.interrupted_for == REASON
    assert server.commands().count("abort") == 1


def test_priority_mail_ends_the_turn_at_a_message_without_a_tool_call(
    home: Path, steered: Steered
) -> None:
    server = FakePi(reply())
    target = line(server, home)
    target.driver, asked = steered(PI, Strategy.SUBSCRIPTION)
    report = run(target)
    assert asked == [0]
    assert report.interrupted_for == REASON


def test_a_message_counts_its_tool_calls_by_block_and_by_stop_reason() -> None:
    call: dict[str, object] = {"type": "toolCall", "id": "c1", "name": OWN}
    text = {"type": "text", "text": "x"}
    assert tool_calls_in({"content": [text]}) == 0
    assert tool_calls_in({"content": [text, call, call]}) == 2
    assert tool_calls_in({"content": [text], "stopReason": "toolUse"}) == 1
    assert tool_calls_in({"content": "not a list"}) == 0
