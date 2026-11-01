"""The Antigravity adapter, driven by a scripted Antigravity ACP runtime.

The runtime is not installed on the operator's machine, and no test starts it. The
script follows the Agent Client Protocol's schema and what a second implementation,
T3 Code, reads from the runtime, as ``docs/protocol/ANTIGRAVITY.md`` records.
"""

import asyncio
import json
import shutil
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import antigravity, cli, exits
from dromon.activity import ActivityKind, ActivityLog
from dromon.antigravity import (
    DEFAULT_RUNTIME,
    HARNESS_NAME,
    ISOLATION_REFUSAL,
    PROFILE_AUTH,
    PROFILE_NAME,
    RUNTIME_VARIABLE,
    SIGN_IN_NOTICE,
    AntigravityLine,
    check_profile,
    check_workspace,
    engine_environment,
    line_refusal,
    locate_runtime,
    runtime_version,
    unsupported,
)
from dromon.driver import ENGINE_OF, Action, ActionTag, Credential, Driver, Event
from dromon.driver import Strategy
from dromon.priority import SteeredDriver
from dromon.engine import EngineRefused, TurnReport
from dromon.jsonlines import Connection, ConnectionFactory, NotJsonLine
from dromon.lineconfig import LineConfig, McpServer, WorkdirAccess
from dromon.gitssh import COMMAND_VARIABLE, GitSshAccess, ssh_command
from dromon.sandbox import parse_sandbox
from dromon.scheduler import LineEngine, LineSpec, engine_factory

AGY = "antigravity:gemini-3.8-flash-high"
MODEL = "gemini-3.8-flash-high"
OWN = "dromon__send_message"


def driver(strategy: Strategy = Strategy.SUBSCRIPTION) -> Driver:
    return Driver.for_model(AGY, strategy)


class KeyReported(Driver):
    """The packaged driver, except that it hears a key wherever it is told the
    subscription, so the subscription driver refuses it."""

    def step(self, event: Event) -> Action:
        if event == Event("credential", Credential.SUBSCRIPTION.value):
            event = Event("credential", Credential.API.value)
        return super().step(event)


class InterruptsOnText(Driver):
    """The packaged driver, except that it interrupts the turn at its first text."""

    def step(self, event: Event) -> Action:
        if event.tag == "text":
            return Action(ActionTag.INTERRUPT, "test", 0)
        return super().step(event)


async def _record(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "ok"}]}


SEND: SdkMcpTool[Any] = SdkMcpTool(
    name="send_message", description="Send.", input_schema={}, handler=_record
)


# The scripted runtime.


def initialized(
    *,
    name: object = "antigravity-acp",
    version: object = "1.3.0",
    methods: Sequence[str] = ("oauth-personal",),
    protocol: object = 1,
    resume: bool = True,
) -> dict[str, Any]:
    capabilities: dict[str, Any] = {"loadSession": True, "sessionCapabilities": {}}
    if resume:
        capabilities["sessionCapabilities"]["resume"] = {}
    return {
        "protocolVersion": protocol,
        "agentCapabilities": capabilities,
        "authMethods": [{"id": m, "name": m} for m in methods],
        "agentInfo": {"name": name, "version": version},
    }


def model_option(current: str, values: Sequence[str] = (MODEL,)) -> dict[str, Any]:
    return {
        "id": "model",
        "name": "Model",
        "type": "select",
        "currentValue": current,
        "options": [{"value": v, "name": v} for v in values],
    }


def session(
    *,
    mode: str | None = "default",
    modes: Sequence[str] = ("default", "auto_edit", "yolo"),
    model: str = MODEL,
    models: Sequence[str] = (MODEL,),
    session_id: object = "s1",
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sessionId": session_id,
        "configOptions": [model_option(model, models)],
    }
    if mode is not None:
        result["modes"] = {
            "currentModeId": mode,
            "availableModes": [{"id": m, "name": m} for m in modes],
        }
    return result


def update(body: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "s1", "update": body},
    }


def said(text: str) -> dict[str, Any]:
    return update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        }
    )


RUNNING = "Running send_message"
"""How a second implementation's runtime titles an MCP tool call."""


def tool(
    title: str = "",
    kind: str | None = "read",
    status: str | None = "pending",
    ident: str = "t1",
    tool_name: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "sessionUpdate": "tool_call",
        "toolCallId": ident,
        "title": title,
    }
    if kind is not None:
        body["kind"] = kind
    if status is not None:
        body["status"] = status
    if tool_name is not None:
        body["toolName"] = tool_name
    return update(body)


def own(ident: str = "t1") -> dict[str, Any]:
    """A call of Dromon's own tool, named in its structured field."""
    return tool(RUNNING, kind="other", ident=ident, tool_name=OWN)


def changed(ident: str = "t1", **fields: object) -> dict[str, Any]:
    return update({"sessionUpdate": "tool_call_update", "toolCallId": ident, **fields})


def permission(
    title: str = "",
    ident: str = "t1",
    request: str = "p1",
    tool_name: str | None = None,
) -> dict[str, Any]:
    called: dict[str, Any] = {"toolCallId": ident, "title": title}
    if tool_name is not None:
        called["toolName"] = tool_name
    return {
        "jsonrpc": "2.0",
        "id": request,
        "method": "session/request_permission",
        "params": {
            "sessionId": "s1",
            "toolCall": called,
            "options": [
                {"optionId": "once", "kind": "allow_once", "name": "Allow once"},
                {"optionId": "always", "kind": "allow_always", "name": "Always"},
                {"optionId": "no", "kind": "reject_once", "name": "Deny"},
            ],
        },
    }


SIGN_IN_LINE = object()
"""Stands in the script for the runtime's plain-text sign-in notice."""

OTHER_TEXT = object()
"""Stands in the script for a plain-text line that is not the notice."""


class FakeRuntime:
    """A scripted ``agy_acp_server``. It answers Dromon's requests, plays ``events``
    while a prompt runs, and then answers the prompt with ``stop``."""

    def __init__(
        self,
        events: Sequence[object] = (said("do"), said("ne")),
        *,
        init: dict[str, Any] | None = None,
        auth_error: dict[str, Any] | None = None,
        auth_text: object = None,
        opened: dict[str, Any] | None = None,
        stop: str | None = "end_turn",
        answers_cancel: bool = True,
    ) -> None:
        self.events = list(events)
        self.init = initialized() if init is None else init
        self.auth_error = auth_error
        self.auth_text = auth_text
        self.opened = session() if opened is None else opened
        self.stop_reason = stop
        self.answers_cancel = answers_cancel
        self.outbox: deque[object] = deque()
        self.sent: list[dict[str, Any]] = []
        self.stopped = asyncio.Event()
        self.stops = 0
        self.prompt_id: object = None
        self.started = False
        self.arguments: list[str] = []
        self.cwd = ""
        self.environment: dict[str, str] = {}
        self.blocked_method: str | None = None
        self.activity_delay = 0.0

    def methods(self) -> list[str]:
        return [str(m["method"]) for m in self.sent if "method" in m]

    def request(self, method: str) -> dict[str, Any]:
        return next(m for m in self.sent if m.get("method") == method)

    def answer(self, request_id: object) -> dict[str, Any]:
        return next(
            m for m in self.sent if m.get("id") == request_id and "method" not in m
        )

    def _respond(self, request_id: object, result: object) -> None:
        self.outbox.append({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def send(self, message: Mapping[str, object]) -> None:
        self.sent.append(dict(message))
        method = message.get("method")
        request_id = message.get("id")
        if method == self.blocked_method:
            return
        if method == "initialize":
            self._respond(request_id, self.init)
        elif method == "authenticate":
            if self.auth_text is not None:
                self.outbox.append(self.auth_text)
            elif self.auth_error is not None:
                self.outbox.append(
                    {"jsonrpc": "2.0", "id": request_id, "error": self.auth_error}
                )
            else:
                self._respond(request_id, {})
        elif method in ("session/new", "session/resume"):
            self._respond(request_id, self.opened)
        elif method == "session/set_mode":
            self._respond(request_id, {})
        elif method == "session/set_config_option":
            params: Any = message["params"]
            self._respond(
                request_id, {"configOptions": [model_option(str(params["value"]))]}
            )
        elif method == "session/prompt":
            self.prompt_id = request_id
            self.outbox.extend(self.events)
            if self.stop_reason is not None:
                self._respond(request_id, {"stopReason": self.stop_reason})
        elif (
            method == "session/cancel"
            and self.answers_cancel
            and self.stop_reason is None
            and self.prompt_id is not None
        ):
            self._respond(self.prompt_id, {"stopReason": "cancelled"})

    async def receive(self) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        if self.outbox:
            item = self.outbox.popleft()
            if item is SIGN_IN_LINE:
                raise NotJsonLine(SIGN_IN_NOTICE + b"https://accounts.google.com/x\n")
            if item is OTHER_TEXT:
                raise NotJsonLine(b"Traceback (most recent call last):\n")
            assert isinstance(item, dict)
            message = cast(dict[str, Any], item)
            if message.get("method") == "session/update":
                await asyncio.sleep(self.activity_delay)
            return message
        await self.stopped.wait()
        return None

    async def stop(self) -> None:
        self.stops += 1
        self.outbox.clear()
        self.stopped.set()


# The installation.


def install(base: Path) -> Path:
    """A runtime, its helper and a profile under ``base``, which only tests run."""
    executable = base / "agy_acp_server.par"
    executable.parent.mkdir(parents=True, exist_ok=True)
    for path in (executable, base / HARNESS_NAME):
        path.write_text("#!/bin/sh\nexit 1\n")
        path.chmod(0o755)
    signed(base / PROFILE_NAME)
    return executable


def signed(profile: Path) -> Path:
    """A profile whose settings name the Google account sign-in, as the operator's
    must."""
    path = profile / PROFILE_AUTH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"auth": {"type": "oauth-personal"}}))
    return profile


@pytest.fixture
def home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def runtime(tmp_path: Path) -> Path:
    return install(tmp_path / "runtime")


def line(
    server: FakeRuntime,
    home: Path,
    runtime: Path,
    *,
    config: LineConfig | None = None,
    strategy: Strategy = Strategy.SUBSCRIPTION,
    session_id: str | None = None,
    environment: Mapping[str, str] | None = None,
    observe: ActivityLog | None = None,
    partial: bool = True,
    unverified: bool = True,
    loaded: Driver | None = None,
    tools: bool = False,
    turn_seconds: float | None = None,
    first_output_seconds: float | None = None,
    absolute_turn_seconds: float | None = None,
    cost_mark: tuple[str, float] | None = None,
) -> AntigravityLine:
    @asynccontextmanager
    async def connect(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        server.started = True
        server.arguments = arguments
        server.cwd = str(cwd)
        server.environment = dict(env)
        assert Path(env["TMPDIR"]).is_dir()
        assert list(Path(env["HOME"]).iterdir()) == []
        yield server

    workdir = home.parent / "work"
    workdir.mkdir(exist_ok=True)
    base = {"PATH": "/usr/bin:/bin", RUNTIME_VARIABLE: str(runtime)}
    extra: dict[str, Any] = {}
    if observe is not None:
        extra["observe"] = observe.observer("alpha")
    if turn_seconds is not None:
        extra["turn_seconds"] = turn_seconds
    if first_output_seconds is not None:
        extra["first_output_seconds"] = first_output_seconds
    if absolute_turn_seconds is not None:
        extra["absolute_turn_seconds"] = absolute_turn_seconds
    if tools:
        extra |= {
            "mcp_tools": [SEND],
            "bridge": ["dromon-bridge", "--line", "alpha"],
            "line_token": "tok",  # noqa: S106, a test token
        }
    return AntigravityLine(
        loaded or driver(strategy),
        workdir,
        config=config or LineConfig(),
        session_id=session_id,
        environment=base if environment is None else {**base, **environment},
        home=home,
        connection_factory=connect,
        partial_isolation=partial,
        unverified_credential=unverified,
        cost_mark=cost_mark,
        **extra,
    )


def run(target: AntigravityLine, prompt: str = "hello") -> TurnReport:
    return asyncio.run(target.run_turn(prompt))


# Models and drivers.


def test_the_model_is_engine_qualified_and_names_its_effort() -> None:
    assert ENGINE_OF[AGY] == antigravity.ENGINE_NAME
    for strategy in Strategy:
        loaded = driver(strategy)
        assert loaded.settings.effort == "high"
        assert loaded.step(Event("init", MODEL)).tag is ActionTag.ACCEPT
        for other in (AGY, "gemini-3.1-pro", "gemini-3.8-flash-medium"):
            assert loaded.step(Event("init", other)).tag is ActionTag.REFUSE
    assert "antigravity:gemini-3.1-pro" not in ENGINE_OF


# Refusals before the runtime starts.


def test_every_turn_is_refused_until_partial_isolation_is_accepted(
    home: Path, runtime: Path
) -> None:
    server = FakeRuntime()
    with pytest.raises(EngineRefused, match="--accept-partial-isolation"):
        run(line(server, home, runtime, partial=False))
    assert not server.started
    assert "without approval" in ISOLATION_REFUSAL


def test_the_packaged_drivers_refuse_the_unverified_credential_before_it_starts(
    home: Path, tmp_path: Path
) -> None:
    """Antigravity review, F2. The runtime does not report which credential it uses,
    so the credential is unverified, and no packaged driver accepts it. The refusal
    comes before the runtime is even located."""
    for strategy in Strategy:
        verdict = driver(strategy).step(Event("credential", "unverified"))
        assert verdict.tag is ActionTag.REFUSE
    server = FakeRuntime()
    missing = tmp_path / "nowhere" / "agy_acp_server.par"
    with pytest.raises(EngineRefused, match="refuses the unverified credential"):
        run(line(server, home, missing, unverified=False))
    assert not server.started


def test_the_operators_confirmation_gives_the_driver_the_subscription(
    home: Path, runtime: Path
) -> None:
    server = FakeRuntime()
    report = run(line(server, home, runtime))
    assert report.subtype == "success"
    assert report.credential is Credential.UNVERIFIED
    refused = FakeRuntime()
    keyed = KeyReported.for_model(AGY, Strategy.SUBSCRIPTION)
    with pytest.raises(EngineRefused, match="refuses the subscription the operator"):
        run(line(refused, home, runtime, loaded=keyed))
    assert not refused.started


def test_only_the_subscription_strategy_runs(home: Path, runtime: Path) -> None:
    for strategy in (Strategy.API, Strategy.FLEXIBLE):
        server = FakeRuntime()
        with pytest.raises(EngineRefused, match="only the subscription strategy"):
            run(line(server, home, runtime, strategy=strategy))
        assert not server.started


def test_a_line_takes_its_drivers_effort(home: Path, runtime: Path) -> None:
    server = FakeRuntime()
    with pytest.raises(EngineRefused, match="effort in the model itself"):
        run(line(server, home, runtime, config=LineConfig(effort="low")))
    run(line(FakeRuntime(), home, runtime, config=LineConfig(effort="high")))


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (LineConfig(workdir_access=WorkdirAccess.READ), "read-only working directory"),
        (LineConfig(builtin_tools=("Read",)), "no built-in tools"),
        (
            LineConfig(mcp_servers=(("x", McpServer("stdio", command="x")),)),
            "only Dromon's own MCP",
        ),
        (LineConfig(inherit_account_mcp=True), "only Dromon's own MCP"),
        (LineConfig(claude_code_prompt=True), "own prompt"),
        (LineConfig(project_settings=True), "project settings"),
        (LineConfig(allow_compact=True), "cannot compact"),
        (LineConfig(keep_session=True), "do not keep"),
    ],
)
def test_configurations_an_antigravity_line_refuses(
    config: LineConfig, reason: str
) -> None:
    assert reason in (unsupported(config) or "")
    assert reason in (line_refusal(config) or "")


def test_a_daemon_refuses_every_antigravity_line_for_isolation() -> None:
    assert unsupported(LineConfig()) is None
    assert line_refusal(LineConfig()) == ISOLATION_REFUSAL


def test_the_environment_carries_no_key_profile_or_sign_in_method(
    home: Path, runtime: Path
) -> None:
    hostile = {
        "GEMINI_API_KEY": "k",
        "GEMINI_HOME": "/elsewhere",
        "GOOGLE_API_KEY": "k",
        "GOOGLE_APPLICATION_CREDENTIALS": "/c.json",
        "GOOGLE_GENAI_USE_VERTEXAI": "1",
        "AGY_ACP_ENABLE_OAUTH": "1",
        "AGY_ACP_FORCE_FILE_STORAGE": "0",
        "ANTIGRAVITY_HARNESS_PATH": "/elsewhere/harness",
        "AWS_SECRET_ACCESS_KEY": "s",
        "SOME_API_KEY": "s",
        "DROMON_LINE_TOKEN": "t",
        "BROWSER": "open",
        "TMPDIR": "/shared",
        "DAEMON_ONLY_SENTINEL": "not-allowlisted",
        "HOME": "/elsewhere/home",
        "XDG_CONFIG_HOME": "/elsewhere/home/.config",
        "XDG_DATA_HOME": "/elsewhere/home/.local/share",
        "XDG_STATE_HOME": "/elsewhere/home/.local/state",
        "XDG_CACHE_HOME": "/elsewhere/home/.cache",
    }
    server = FakeRuntime()
    target = line(server, home, runtime, environment={**hostile, "LANG": "C"})
    target.mark = "alpha:mark"
    run(target)
    env = server.environment
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "AGY_ACP_ENABLE_OAUTH"):
        assert name not in env
    for name in ("AWS_SECRET_ACCESS_KEY", "SOME_API_KEY", "DROMON_LINE_TOKEN"):
        assert name not in env
    assert "GOOGLE_GENAI_USE_VERTEXAI" not in env
    assert "DAEMON_ONLY_SENTINEL" not in env
    assert RUNTIME_VARIABLE not in env
    # Antigravity review, F7. The operator's home is out of reach through HOME and
    # the XDG variables, and the sign-in is a file in the profile.
    assert env["AGY_ACP_FORCE_FILE_STORAGE"] == "1"
    for name in antigravity.HOME_VARIABLES[1:]:
        assert name not in env
    assert env["HOME"] != "/elsewhere/home"
    assert Path(env["HOME"]).parent == Path(env["TMPDIR"]).parent
    assert env["HOME"] != env["TMPDIR"]
    assert not Path(env["HOME"]).exists()
    assert env["GEMINI_HOME"] == str(runtime.parent / PROFILE_NAME)
    assert env["ANTIGRAVITY_HARNESS_PATH"] == str(runtime.parent / HARNESS_NAME)
    assert Path(env["BROWSER"]).name == "true"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["LANG"] == "C"
    assert env["DROMON_LINE_MARK"] == "alpha:mark"
    assert env["TMPDIR"] != "/shared"
    assert not Path(env["TMPDIR"]).exists()
    assert server.arguments == []
    assert engine_environment(
        {"GEMINI_HOME": "/x", "PATH": "/b"}, Strategy.SUBSCRIPTION
    ) == {"PATH": "/b"}


# The installation.


def test_a_missing_runtime_is_named_as_the_acp_runtime(tmp_path: Path) -> None:
    home = tmp_path / "home"
    with pytest.raises(EngineRefused) as refused:
        locate_runtime({}, home)
    message = str(refused.value)
    assert "Antigravity ACP runtime is missing" in message
    assert str(home / DEFAULT_RUNTIME) in message
    assert "agy command do not provide it" in message
    found = install((home / DEFAULT_RUNTIME).parent)
    assert locate_runtime({}, home).executable == found
    with pytest.raises(EngineRefused, match="absolute path"):
        locate_runtime({RUNTIME_VARIABLE: "relative/agy"}, home)


def test_an_incomplete_or_unsigned_runtime_is_refused(tmp_path: Path) -> None:
    base = tmp_path / "rt"
    executable = install(base)
    environment = {RUNTIME_VARIABLE: str(executable)}
    assert locate_runtime(environment, tmp_path).profile == base / PROFILE_NAME
    shutil.rmtree(base / PROFILE_NAME)
    with pytest.raises(EngineRefused, match="not been signed in"):
        locate_runtime(environment, tmp_path)
    (base / PROFILE_NAME).mkdir()
    (base / HARNESS_NAME).unlink()
    with pytest.raises(EngineRefused, match="incomplete"):
        locate_runtime(environment, tmp_path)
    executable.chmod(0o644)
    with pytest.raises(EngineRefused, match="missing"):
        locate_runtime(environment, tmp_path)


def test_the_version_is_read_as_three_numbers() -> None:
    assert runtime_version("1.3.0") == (1, 3, 0)
    assert runtime_version("1.12.4-beta") == (1, 12, 4)
    for bad in ("1.3", "v1.3.0", "", None, 13):
        assert runtime_version(bad) is None


@pytest.mark.parametrize("name", antigravity.WORKSPACE_DIRECTORIES)
def test_project_directories_for_antigravity_refuse_the_turn(
    tmp_path: Path, name: str
) -> None:
    """Antigravity review, F3. The home directory's are its own."""
    home = tmp_path / "home"
    workspace = home / "repo" / "sub"
    workspace.mkdir(parents=True)
    check_workspace(workspace, home)
    (home / name).mkdir()
    check_workspace(workspace, home)
    (home / "repo" / name).mkdir()
    with pytest.raises(EngineRefused, match=f"repo/{name}"):
        check_workspace(workspace, home)


def test_a_key_file_in_the_profile_or_home_refuses_the_turn(tmp_path: Path) -> None:
    """Re-review, N2. Existence alone refuses, and Dromon never reads it."""
    profile = signed(tmp_path / "profile")
    home = tmp_path / "home"
    check_profile(profile, home)
    for key in (profile / ".env", home / ".gemini" / ".env"):
        key.parent.mkdir(parents=True, exist_ok=True)
        key.symlink_to(tmp_path / "absent")
        with pytest.raises(EngineRefused, match=r"\.env exists"):
            check_profile(profile, home)
        key.unlink()


@pytest.mark.parametrize(
    ("relative", "key"),
    [("config/hooks.json", "hooks"), ("config/mcp_config.json", "mcpServers")],
)
def test_profile_hooks_and_mcp_servers_refuse_the_turn(
    tmp_path: Path, relative: str, key: str
) -> None:
    profile = signed(tmp_path / "profile")
    path = profile / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({key: {}}))
    check_profile(profile, tmp_path)
    path.write_text("{}")
    check_profile(profile, tmp_path)
    declared: list[dict[str, Any]] = [
        {key: {"x": {"command": "x"}}},
        {key: {}, "other": {"x": {"command": "x"}}},
        {"other": {}},
    ]
    for document in declared:
        path.write_text(json.dumps(document))
        with pytest.raises(EngineRefused, match=f"declares {key} or another key"):
            check_profile(profile, tmp_path)
    path.write_text("[")
    with pytest.raises(EngineRefused, match="not JSON"):
        check_profile(profile, tmp_path)


@pytest.mark.parametrize("relative", ["settings.json", "antigravity-cli/settings.json"])
def test_profile_settings_hold_only_reviewed_keys(
    tmp_path: Path, relative: str
) -> None:
    """Antigravity review, F6. The settings are an allowlist, so a key Dromon has not
    reviewed, such as one that approves every tool, refuses the turn."""
    profile = signed(tmp_path / "profile")
    path = profile / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"$schema": "x", "permissions": {"allow": []}}))
    check_profile(profile, tmp_path)
    path.write_text(json.dumps({"permissions": {"deny": ["command(rm)"]}}))
    check_profile(profile, tmp_path)
    path.write_text(json.dumps({"permissions": {"allow": ["command(git)"]}}))
    with pytest.raises(EngineRefused, match="permissions.allow"):
        check_profile(profile, tmp_path)
    path.write_text(json.dumps({"permissions": ["x"]}))
    with pytest.raises(EngineRefused, match="not an object"):
        check_profile(profile, tmp_path)
    for document, named in (
        ({"autoApprove": True}, "'autoApprove'"),
        ({"yolo": False, "theme": "dark"}, "'theme', 'yolo'"),
        ({"permissions": {"defaultMode": "yolo"}}, "permissions 'defaultMode'"),
    ):
        path.write_text(json.dumps(document))
        with pytest.raises(EngineRefused, match=f"sets {named}.*not reviewed"):
            check_profile(profile, tmp_path)


def test_the_profile_must_name_the_google_sign_in_alone(tmp_path: Path) -> None:
    """Antigravity review, F6. A missing file refuses, since the method the runtime
    would choose without it is not shown."""
    profile = tmp_path / "profile"
    profile.mkdir()
    with pytest.raises(EngineRefused, match="is missing, so the sign-in method"):
        check_profile(profile, tmp_path)
    path = signed(profile) / PROFILE_AUTH
    check_profile(profile, tmp_path)
    for document in (
        {"auth": {"type": "gemini-api-key"}},
        {"auth": {"type": "agent-platform"}},
    ):
        path.write_text(json.dumps(document))
        with pytest.raises(EngineRefused, match="sets the sign-in method"):
            check_profile(profile, tmp_path)
    for document in (
        {"auth": {"type": "oauth-personal"}, "gcp": {"project": "p"}},
        {"auth": {"type": "oauth-personal", "apiKey": "k"}},
        {"auth": "oauth-personal"},
        {},
    ):
        path.write_text(json.dumps(document))
        with pytest.raises(EngineRefused, match="keys other than auth.type"):
            check_profile(profile, tmp_path)


def test_the_profile_is_checked_before_the_runtime_starts(
    home: Path, runtime: Path
) -> None:
    (runtime.parent / PROFILE_NAME / ".env").write_text("")
    server = FakeRuntime()
    with pytest.raises(EngineRefused, match=r"\.env exists"):
        run(line(server, home, runtime))
    assert not server.started


# The handshake.


@pytest.mark.parametrize(
    ("init", "reason"),
    [
        (initialized(protocol=2), "ACP version 2"),
        (initialized(name="something-else"), "names itself 'something-else'"),
        (initialized(version="1.2.9"), "Dromon needs 1.3.0 or later"),
        (initialized(version="unknown"), "reports version 'unknown'"),
        (initialized(methods=("gemini-api-key",)), "only gemini-api-key"),
        (initialized(methods=()), "only none"),
    ],
)
def test_the_runtime_must_identify_itself_and_offer_the_google_sign_in(
    home: Path, runtime: Path, init: dict[str, Any], reason: str
) -> None:
    server = FakeRuntime(init=init)
    with pytest.raises(EngineRefused, match=reason):
        run(line(server, home, runtime))
    assert "authenticate" not in server.methods()
    assert server.request("initialize")["params"]["clientCapabilities"] == {
        "fs": {"readTextFile": False, "writeTextFile": False},
        "terminal": False,
    }


def test_dromon_chooses_the_google_account_sign_in(home: Path, runtime: Path) -> None:
    server = FakeRuntime(init=initialized(version="1.4.2"))
    run(line(server, home, runtime))
    assert server.request("authenticate")["params"] == {"methodId": "oauth-personal"}
    assert server.methods()[:3] == ["initialize", "authenticate", "session/new"]


@pytest.mark.parametrize("by_text", [True, False])
def test_a_runtime_that_is_not_signed_in_is_refused(
    home: Path, runtime: Path, by_text: bool
) -> None:
    server = (
        FakeRuntime(auth_text=SIGN_IN_LINE)
        if by_text
        else FakeRuntime(auth_error={"code": -32000, "message": "auth"})
    )
    with pytest.raises(EngineRefused, match="not signed in") as refused:
        run(line(server, home, runtime))
    assert "accounts.google.com" not in str(refused.value)
    assert "session/new" not in server.methods()


def test_other_plain_text_and_other_errors_refuse_the_turn(
    home: Path, runtime: Path
) -> None:
    with pytest.raises(EngineRefused, match="not JSON"):
        run(line(FakeRuntime(auth_text=OTHER_TEXT), home, runtime))
    with pytest.raises(EngineRefused, match="refused a request"):
        run(line(FakeRuntime(auth_error={"code": -1, "message": "no"}), home, runtime))


def test_dromons_tools_reach_the_session_as_its_mcp_server(
    home: Path, runtime: Path
) -> None:
    server = FakeRuntime()
    run(line(server, home, runtime, tools=True))
    params = server.request("session/new")["params"]
    assert params["cwd"] == server.cwd
    assert params["mcpServers"] == [
        {
            "name": "dromon",
            "command": "dromon-bridge",
            "args": ["--line", "alpha"],
            "env": [{"name": "DROMON_LINE_TOKEN", "value": "tok"}],
        }
    ]
    assert "DROMON_LINE_TOKEN" not in server.environment
    bare = FakeRuntime()
    run(line(bare, home, runtime))
    assert bare.request("session/new")["params"]["mcpServers"] == []


def test_the_session_must_ask_before_acting(home: Path, runtime: Path) -> None:
    switched = FakeRuntime(opened=session(mode="yolo"))
    run(line(switched, home, runtime))
    assert switched.request("session/set_mode")["params"] == {
        "sessionId": "s1",
        "modeId": "default",
    }
    with pytest.raises(EngineRefused, match="permission mode 'yolo'"):
        run(
            line(
                FakeRuntime(opened=session(mode="yolo", modes=("yolo",))), home, runtime
            )
        )
    with pytest.raises(EngineRefused, match="does not report its permission mode"):
        run(line(FakeRuntime(opened=session(mode=None)), home, runtime))


def test_the_drivers_model_is_selected_and_judged(home: Path, runtime: Path) -> None:
    offered = (MODEL, "gemini-3.8-flash-low")
    chosen = FakeRuntime(opened=session(model="gemini-3.8-flash-low", models=offered))
    run(line(chosen, home, runtime))
    assert chosen.request("session/set_config_option")["params"] == {
        "sessionId": "s1",
        "configId": "model",
        "value": MODEL,
    }
    absent = FakeRuntime(opened=session(model="gemini-4-argon-high", models=()))
    with pytest.raises(EngineRefused, match="refuses the engine model"):
        run(line(absent, home, runtime))
    assert "session/prompt" not in absent.methods()
    plain = FakeRuntime()
    run(line(plain, home, runtime))
    assert "session/set_config_option" not in plain.methods()


def test_an_invalid_session_identifier_is_refused(home: Path, runtime: Path) -> None:
    with pytest.raises(EngineRefused, match="invalid session"):
        run(line(FakeRuntime(opened=session(session_id="--x")), home, runtime))
    with pytest.raises(EngineRefused, match="is invalid"):
        run(line(FakeRuntime(), home, runtime, session_id="--x"))


# Turns.


def test_a_plain_turn_reports_text_and_its_session(home: Path, runtime: Path) -> None:
    workdir = home.parent / "work"
    workdir.mkdir(exist_ok=True)
    (workdir / "notes.md").write_text("Be brief.")
    config = LineConfig(instructions=("notes.md",))
    server = FakeRuntime()
    target = line(server, home, runtime, config=config)
    report = run(target)
    assert report.subtype == "success"
    assert report.text == "done"
    assert report.session_id == "s1" == target.session_id
    assert report.usage is None and report.cost_estimate_usd is None
    prompt = server.request("session/prompt")["params"]
    assert prompt["sessionId"] == "s1"
    assert prompt["prompt"] == [
        {"type": "text", "text": "Contents of notes.md:\n\nBe brief.\n\nhello"}
    ]


def test_a_resumed_session_takes_no_instructions_and_needs_the_capability(
    home: Path, runtime: Path
) -> None:
    server = FakeRuntime()
    run(line(server, home, runtime, session_id="s1"))
    assert "session/new" not in server.methods()
    assert server.request("session/resume")["params"]["sessionId"] == "s1"
    assert server.request("session/prompt")["params"]["prompt"][0]["text"] == "hello"
    unable = FakeRuntime(init=initialized(resume=False))
    with pytest.raises(EngineRefused, match="does not offer to resume"):
        run(line(unable, home, runtime, session_id="s1"))


def test_the_turn_streams_to_the_observer(home: Path, runtime: Path) -> None:
    log = ActivityLog()
    events = [said("done"), update({"sessionUpdate": "agent_thought_chunk"})]
    run(line(FakeRuntime(events), home, runtime, observe=log))
    assert [entry.kind for entry in log.after("alpha", 0)] == [
        ActivityKind.TEXT,
        ActivityKind.THINKING,
    ]


@pytest.mark.parametrize(
    ("stop", "subtype"),
    [("end_turn", "success"), ("max_tokens", "error"), ("refusal", "error")],
)
def test_stop_reasons_become_subtypes(
    home: Path, runtime: Path, stop: str, subtype: str
) -> None:
    assert run(line(FakeRuntime(stop=stop), home, runtime)).subtype == subtype


def test_an_unknown_stop_reason_refuses_the_turn(home: Path, runtime: Path) -> None:
    with pytest.raises(EngineRefused, match="ended its turn for 'paused'"):
        run(line(FakeRuntime(stop="paused"), home, runtime))


def test_dromons_own_tool_is_allowed_once_and_counted(
    home: Path, runtime: Path
) -> None:
    events = [
        own(),
        permission(RUNNING, tool_name=OWN),
        changed(status="in_progress"),
        changed(status="completed", title=RUNNING, kind="other", toolName=OWN),
        own(),
        said("sent"),
    ]
    server = FakeRuntime(events)
    report = run(line(server, home, runtime, tools=True))
    assert report.tool_uses == (OWN,)
    assert server.answer("p1")["result"] == {
        "outcome": {"outcome": "selected", "optionId": "once"}
    }
    assert "session/cancel" not in server.methods()


def test_a_permission_request_that_omits_the_recorded_name_still_names_it(
    home: Path, runtime: Path
) -> None:
    """The request adds nothing that conflicts, so the call is the one recorded."""
    server = FakeRuntime([own(), permission(RUNNING), said("sent")])
    report = run(line(server, home, runtime, tools=True))
    assert report.tool_uses == (OWN,)
    assert server.answer("p1")["result"]["outcome"]["optionId"] == "once"


def test_another_tools_permission_is_refused_before_it_runs(
    home: Path, runtime: Path
) -> None:
    server = FakeRuntime([permission("Read /etc/hosts"), said("x")])
    assert run(line(server, home, runtime, tools=True)).text == "x"
    assert server.answer("p1")["result"] == {
        "outcome": {"outcome": "selected", "optionId": "no"}
    }
    assert "session/cancel" not in server.methods()


@pytest.mark.parametrize(
    ("status", "when"),
    [
        ("in_progress", "after it started"),
        (None, "after"),
    ],
)
def test_another_tool_call_cancels_the_turn(
    home: Path, runtime: Path, status: str | None, when: str
) -> None:
    server = FakeRuntime([tool("view_file", status=status)])
    with pytest.raises(EngineRefused, match=f"stopped {when}"):
        run(line(server, home, runtime))
    assert "session/cancel" in server.methods()


def test_no_kind_exempts_a_tool_call_from_the_check(home: Path, runtime: Path) -> None:
    """Antigravity review, F1. The kind is the runtime's own report, so a call of the
    kind ``think`` titled as a shell command is refused like any other."""
    server = FakeRuntime(
        [tool("rm -rf ~", kind="think", status="in_progress"), said("x")]
    )
    with pytest.raises(EngineRefused, match=r"not given, stopped .*: rm -rf ~"):
        run(line(server, home, runtime, tools=True))
    assert "session/cancel" in server.methods()
    plain = FakeRuntime([tool("plan", kind="think", status="in_progress"), said("x")])
    with pytest.raises(EngineRefused, match="not given"):
        run(line(plain, home, runtime))


def test_a_quiet_looking_call_that_turns_into_a_command_is_refused(
    home: Path, runtime: Path
) -> None:
    """Antigravity review, F2. The call is refused at its first report, and its later
    update, which changes it into a command, is checked again rather than skipped."""
    events = [
        tool("plan", kind="think", ident="k1"),
        changed("k1", title="rm -rf ~", kind="execute", status="in_progress"),
    ]
    server = FakeRuntime(events, stop=None)
    with pytest.raises(EngineRefused, match="changed the title"):
        run(line(server, home, runtime, tools=True))
    assert server.methods().count("session/cancel") == 1


@pytest.mark.parametrize(
    ("fields", "key"),
    [
        ({"toolName": "run_shell_command"}, "toolName"),
        ({"title": "rm -rf ~"}, "title"),
        ({"kind": "execute"}, "kind"),
        ({"toolName": None}, "toolName"),
    ],
)
def test_an_own_call_whose_identity_changes_refuses_the_turn(
    home: Path, runtime: Path, fields: dict[str, object], key: str
) -> None:
    """Antigravity review, F3. Permission was granted for the call as it was, so an
    update that changes its name, title or kind refuses the turn."""
    later = update({"sessionUpdate": "tool_call_update", "toolCallId": "t1", **fields})
    events = [own(), permission(RUNNING, tool_name=OWN), later, said("x")]
    server = FakeRuntime(events)
    with pytest.raises(EngineRefused, match=f"changed the {key} of its tool call"):
        run(line(server, home, runtime, tools=True))
    assert server.answer("p1")["result"]["outcome"]["optionId"] == "once"
    assert "session/cancel" in server.methods()


def test_a_field_the_recorded_call_lacked_is_a_change(
    home: Path, runtime: Path
) -> None:
    """An own call first reported without a kind cannot gain one later."""
    events = [
        tool(RUNNING, kind=None, tool_name=OWN),
        changed(kind="execute"),
    ]
    with pytest.raises(EngineRefused, match="changed the kind"):
        run(line(FakeRuntime(events), home, runtime, tools=True))


def test_a_permission_request_that_changes_a_call_is_rejected(
    home: Path, runtime: Path
) -> None:
    """Antigravity review, F3. A request for a recorded call under another title is
    answered with a rejection, even when it names Dromon's tool."""
    events = [own(), permission("rm -rf ~", tool_name=OWN), said("x")]
    server = FakeRuntime(events)
    with pytest.raises(EngineRefused, match="changed the title"):
        run(line(server, home, runtime, tools=True))
    assert server.answer("p1")["result"]["outcome"]["optionId"] == "no"


def test_a_title_never_names_dromons_tool(home: Path, runtime: Path) -> None:
    """Antigravity review, F4. Only the structured toolName names one of Dromon's
    tools, so a call whose title equals its name is refused, both as a permission
    request and as a call."""
    asked = FakeRuntime([permission(OWN), said("x")])
    assert run(line(asked, home, runtime, tools=True)).tool_uses == ()
    assert "session/cancel" not in asked.methods()
    assert asked.answer("p1")["result"]["outcome"]["optionId"] == "no"
    used = FakeRuntime([tool(OWN, kind="other", status="in_progress"), said("x")])
    with pytest.raises(
        EngineRefused, match=f"not given, stopped after it started: {OWN}"
    ):
        run(line(used, home, runtime, tools=True))


@pytest.mark.parametrize("ident", [None, "", 7])
def test_a_tool_call_without_an_identifier_refuses_the_turn(
    home: Path, runtime: Path, ident: object
) -> None:
    body: dict[str, Any] = {"sessionUpdate": "tool_call", "kind": "other"}
    body |= {"toolName": OWN, "title": RUNNING}
    if ident is not None:
        body["toolCallId"] = ident
    with pytest.raises(EngineRefused, match="tool call without an identifier"):
        run(line(FakeRuntime([update(body)]), home, runtime, tools=True))
    request = permission(RUNNING, tool_name=OWN)
    del request["params"]["toolCall"]["toolCallId"]
    server = FakeRuntime([request])
    with pytest.raises(EngineRefused, match="tool call without an identifier"):
        run(line(server, home, runtime, tools=True))
    assert server.answer("p1")["result"]["outcome"]["optionId"] == "no"


def test_a_request_after_a_cancel_is_answered_as_cancelled(
    home: Path, runtime: Path
) -> None:
    """ACP requires it, and a cancelled outcome grants nothing, even for Dromon's own
    tool. The first refusal stands."""
    asked = permission(RUNNING, ident="t2", tool_name=OWN)
    server = FakeRuntime([tool("view_file", status="in_progress"), asked])
    with pytest.raises(EngineRefused, match="view_file"):
        run(line(server, home, runtime, tools=True))
    assert server.answer("p1")["result"] == {"outcome": {"outcome": "cancelled"}}
    loaded = InterruptsOnText.for_model(AGY, Strategy.SUBSCRIPTION)
    again = permission(RUNNING, tool_name=OWN)
    interrupted = FakeRuntime([said("x"), again], stop=None)
    report = run(line(interrupted, home, runtime, tools=True, loaded=loaded))
    assert report.subtype == "interrupted"
    assert interrupted.answer("p1")["result"] == {"outcome": {"outcome": "cancelled"}}


@pytest.mark.parametrize(
    ("tool_name", "options", "intent"),
    [
        (OWN, [{"optionId": "broad", "kind": "allow_always"}], "allow"),
        ("shell", [{"optionId": "yes", "kind": "allow_once"}], "refuse"),
    ],
)
def test_missing_permission_choice_cancels_and_refuses_with_evidence(
    home: Path,
    runtime: Path,
    tool_name: str,
    options: list[dict[str, str]],
    intent: str,
) -> None:
    request = permission("running a tool", tool_name=tool_name)
    request["params"]["options"] = options
    server = FakeRuntime([request])

    with pytest.raises(EngineRefused) as raised:
        run(line(server, home, runtime, tools=True))

    message = str(raised.value)
    assert f"tool '{tool_name}'" in message
    assert intent in message
    assert options[0]["kind"] in message
    assert options[0]["optionId"] in message
    assert server.answer("p1")["result"] == {"outcome": {"outcome": "cancelled"}}
    assert server.methods().count("session/cancel") == 1


def test_missing_permission_option_preserves_an_earlier_identity_refusal(
    home: Path, runtime: Path
) -> None:
    request = permission(RUNNING, tool_name=OWN)
    request["params"]["toolCall"]["title"] = "changed title"
    request["params"]["options"] = []
    server = FakeRuntime([own(), request])

    with pytest.raises(EngineRefused) as raised:
        run(line(server, home, runtime, tools=True))

    assert "changed the title" in str(raised.value)
    assert "ACP offered no option" not in str(raised.value)
    assert server.answer("p1")["result"] == {"outcome": {"outcome": "cancelled"}}


def test_a_question_from_the_runtime_is_cancelled_and_refuses_the_turn(
    home: Path, runtime: Path
) -> None:
    server = FakeRuntime([permission("Which?", ident="interaction_1")])
    with pytest.raises(EngineRefused, match="question Dromon cannot answer"):
        run(line(server, home, runtime))
    assert server.answer("p1")["result"] == {"outcome": {"outcome": "cancelled"}}


@pytest.mark.parametrize(
    ("event", "reason"),
    [
        (update({"sessionUpdate": "agent_dance"}), "update Dromon does not know"),
        (
            {"jsonrpc": "2.0", "id": "q", "method": "fs/read_text_file", "params": {}},
            "asked for fs/read_text_file",
        ),
        (
            {"jsonrpc": "2.0", "method": "x.ai/task_started", "params": {}},
            "notification Dromon does not know",
        ),
        ({"jsonrpc": "2.0", "id": 99, "result": {}}, "did not expect"),
        (
            update({"sessionUpdate": "current_mode_update", "currentModeId": "yolo"}),
            "switched to the permission mode 'yolo'",
        ),
        (
            update(
                {
                    "sessionUpdate": "config_option_update",
                    "configOptions": [model_option("gemini-3.8-flash-low")],
                }
            ),
            "switched to the model",
        ),
    ],
)
def test_unknown_or_unsafe_messages_refuse_the_turn(
    home: Path, runtime: Path, event: dict[str, Any], reason: str
) -> None:
    server = FakeRuntime([event])
    with pytest.raises(EngineRefused, match=reason):
        run(line(server, home, runtime))
    assert "session/cancel" in server.methods()
    if "id" in event and "method" in event:
        assert server.answer("q")["error"]["code"] == -32601


def test_known_informational_updates_pass(home: Path, runtime: Path) -> None:
    events = [
        update({"sessionUpdate": "available_commands_update", "availableCommands": []}),
        update({"sessionUpdate": "plan", "entries": []}),
        update({"sessionUpdate": "current_mode_update", "currentModeId": "default"}),
        said("ok"),
    ]
    assert run(line(FakeRuntime(events), home, runtime)).subtype == "success"


def configured(**fields: object) -> dict[str, Any]:
    return update({"sessionUpdate": "config_option_update", **fields})


@pytest.mark.parametrize(
    ("event", "shown"),
    [
        (configured(configId="model", value="gemini-4-argon-high"), "argon"),
        (configured(configId="model"), "None"),
        (configured(configOptions=[{"id": "model", "currentValue": 5}]), "5"),
        (configured(configOptions="model"), "'model'"),
        (configured(models={"currentModelId": "gemini-3.8-flash-low"}), "low"),
        (
            configured(configOptions=[model_option(MODEL), model_option("x")]),
            "'x'",
        ),
    ],
)
def test_any_model_update_but_the_drivers_refuses_the_turn(
    home: Path, runtime: Path, event: dict[str, Any], shown: str
) -> None:
    """Antigravity review, F5. The update's own fields are read as well as the
    schema's, and a value that cannot be read is not the driver's model."""
    server = FakeRuntime([event, said("x")])
    with pytest.raises(EngineRefused, match=f"switched to the model .*{shown}"):
        run(line(server, home, runtime))
    assert "session/cancel" in server.methods()


def test_a_model_update_that_keeps_the_drivers_model_passes(
    home: Path, runtime: Path
) -> None:
    events = [
        configured(configOptions=[model_option(MODEL)]),
        configured(configId="model", value=MODEL),
        configured(configOptions=[{"id": "mode", "currentValue": "default"}]),
        said("ok"),
    ]
    assert run(line(FakeRuntime(events), home, runtime)).subtype == "success"


def used(tokens: object = 1200, cost: object = None) -> dict[str, Any]:
    body: dict[str, Any] = {"sessionUpdate": "usage_update", "used": tokens}
    body["size"] = 1_000_000
    if cost is not None:
        body["cost"] = cost
    return update(body)


def dollars(amount: object) -> dict[str, object]:
    return {"amount": amount, "currency": "USD"}


def test_usage_is_counted_where_its_shape_allows_and_labelled(
    home: Path, runtime: Path
) -> None:
    """Antigravity review, F9. The cost is the session's running total, so a second
    turn on the same session counts the difference, and both are labelled
    unverified."""
    first = FakeRuntime([used(800, dollars(0.25)), used(1200, dollars(0.5))])
    target = line(first, home, runtime)
    report = run(target)
    assert report.cost_estimate_usd == 0.5
    assert report.cost_mark == ("s1", 0.5)
    assert report.cost_source == "difference"
    assert report.context_tokens == 1200
    assert report.usage_note is not None and "unverified" in report.usage_note
    second = FakeRuntime([used(2000, dollars(0.75))])
    rebuilt = line(second, home, runtime, session_id="s1", cost_mark=report.cost_mark)
    again = run(rebuilt)
    assert again.cost_estimate_usd == 0.25
    assert rebuilt.cost_mark == ("s1", 0.75)
    other = FakeRuntime([used(2000, dollars(0.75))], opened=session(session_id="s2"))
    fresh = run(line(other, home, runtime, cost_mark=("s1", 0.5)))
    assert fresh.cost_estimate_usd == 0.75


@pytest.mark.parametrize(
    "cost",
    [
        {"amount": 1.0, "currency": "EUR"},
        dollars(True),
        dollars(-1),
        dollars(float("inf")),
        dollars("1"),
        "1.0",
    ],
)
def test_usage_of_another_shape_is_left_uncounted(
    home: Path, runtime: Path, cost: object
) -> None:
    report = run(line(FakeRuntime([used(-1, cost), said("x")]), home, runtime))
    assert report.cost_estimate_usd is None and report.cost_mark is None
    assert report.context_tokens is None
    assert report.usage_note == antigravity.NO_COST_NOTE


def test_a_turn_without_a_reported_cost_says_so_plainly(
    home: Path, runtime: Path
) -> None:
    """Re-review, N5. The turn is accepted, as a subscription's would be, and its
    note says that no cost was seen, whether or not a context was counted."""
    plain = run(line(FakeRuntime([said("x")]), home, runtime))
    assert plain.cost_estimate_usd is None
    assert plain.usage_note == antigravity.NO_COST_NOTE
    assert plain.usage_note.startswith("no cost was seen")
    sized = run(line(FakeRuntime([used(900, None), said("x")]), home, runtime))
    assert sized.cost_estimate_usd is None and sized.context_tokens == 900
    assert sized.usage_note is not None
    assert sized.usage_note.startswith(antigravity.NO_COST_NOTE)
    assert antigravity.CONTEXT_NOTE in sized.usage_note


@pytest.mark.parametrize("kind", sorted(antigravity.COMPACTIONS))
def test_a_turn_that_compacts_is_refused_unless_its_cost_is_counted(
    home: Path, runtime: Path, kind: str
) -> None:
    """Antigravity review, F9. A compaction is a model call of its own, so a cost
    must be reported after it, and the refused turn leaves the mark unmoved."""
    compacted = update({"sessionUpdate": kind, "compactionId": "c1"})
    before = FakeRuntime([used(10, dollars(0.1)), compacted, said("x")])
    target = line(before, home, runtime)
    with pytest.raises(EngineRefused, match="compacted its session"):
        run(target)
    assert target.cost_mark is None
    shapeless = FakeRuntime([compacted, used(10, {"amount": 1, "currency": "EUR"})])
    with pytest.raises(EngineRefused, match="compacted its session"):
        run(line(shapeless, home, runtime))
    after = FakeRuntime([compacted, used(10, dollars(0.2)), said("x")])
    assert run(line(after, home, runtime)).cost_estimate_usd == 0.2


def test_a_driver_interrupt_cancels_and_waits_for_the_prompt(
    home: Path, runtime: Path
) -> None:
    server = FakeRuntime([said("x")], stop=None)
    loaded = InterruptsOnText.for_model(AGY, Strategy.SUBSCRIPTION)
    report = run(line(server, home, runtime, loaded=loaded))
    assert report.subtype == "interrupted" and report.interrupted_for == "test"
    assert server.methods().count("session/cancel") == 1
    assert server.stops == 0


def test_a_cancel_the_runtime_ignores_ends_the_process(
    home: Path, runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(antigravity, "ABORT_SECONDS", 0.05)
    server = FakeRuntime([said("x")], stop=None, answers_cancel=False)
    loaded = InterruptsOnText.for_model(AGY, Strategy.SUBSCRIPTION)
    report = run(line(server, home, runtime, loaded=loaded))
    assert report.subtype == "interrupted"
    assert server.stops == 1


def test_a_turn_past_its_deadline_is_cancelled_and_refused(
    home: Path, runtime: Path
) -> None:
    server = FakeRuntime([said("x")], stop=None, answers_cancel=False)
    with pytest.raises(EngineRefused, match="no activity for 0.05 seconds"):
        run(line(server, home, runtime, turn_seconds=0.05))
    assert "session/cancel" in server.methods()
    assert server.stops == 1


def test_no_first_output_has_a_bounded_stage_named_failure(
    home: Path, runtime: Path
) -> None:
    # The bound is half a second rather than a hundredth, since the fake finishes the
    # stages before the stalled one at once but a loaded host may not run them within
    # ten milliseconds, and the error would then name an earlier stage. Found failing
    # under load on 2026-10-29.
    server = FakeRuntime([], stop=None)
    target = line(server, home, runtime, first_output_seconds=0.5)
    with pytest.raises(
        EngineRefused, match="first model output before its first output"
    ):
        run(target)
    assert server.stops == 1

    stalled_sign_in = FakeRuntime()
    stalled_sign_in.blocked_method = "authenticate"
    target = line(stalled_sign_in, home, runtime, first_output_seconds=0.5)
    with pytest.raises(EngineRefused, match="sign-in before its first output"):
        run(target)
    assert stalled_sign_in.stops == 1


def test_stage_is_reset_for_each_antigravity_turn(home: Path, runtime: Path) -> None:
    server = FakeRuntime()
    target = line(server, home, runtime, first_output_seconds=0.5)
    run(target)
    server.blocked_method = "initialize"
    with pytest.raises(EngineRefused, match="initialize before its first output"):
        run(target)


def test_antigravity_sliding_deadline_has_an_absolute_cap(
    home: Path, runtime: Path
) -> None:
    active = FakeRuntime([said(str(index)) for index in range(6)])
    active.activity_delay = 0.02
    report = run(
        line(
            active,
            home,
            runtime,
            turn_seconds=0.1,
            absolute_turn_seconds=0.5,
        )
    )
    assert report.text == "012345"

    capped = FakeRuntime([said(str(index)) for index in range(10)])
    capped.activity_delay = 0.02
    with pytest.raises(EngineRefused, match="absolute cap of 0.15 seconds"):
        run(
            line(
                capped,
                home,
                runtime,
                turn_seconds=0.1,
                absolute_turn_seconds=0.15,
            )
        )


# The factory and the probe.


def test_the_factory_builds_a_refusing_line(tmp_path: Path) -> None:
    build = engine_factory(str(tmp_path))
    engine: LineEngine = build(LineSpec("probe", AGY, Strategy.SUBSCRIPTION), [])
    assert isinstance(engine, AntigravityLine)
    assert engine.partial_isolation is False
    assert engine.unverified_credential is False
    with pytest.raises(EngineRefused, match="partial-isolation"):
        asyncio.run(engine.run_turn("ready?"))
    confirming = engine_factory(
        str(tmp_path),
        accept_partial_isolation=True,
        accept_unverified_credential=True,
    )
    confirmed = confirming(LineSpec("probe", AGY, Strategy.SUBSCRIPTION), [])
    assert isinstance(confirmed, AntigravityLine)
    assert confirmed.partial_isolation and confirmed.unverified_credential
    marked = build(
        LineSpec("probe", AGY, Strategy.SUBSCRIPTION, cost_mark=("s1", 0.5)), []
    )
    assert isinstance(marked, AntigravityLine) and marked.cost_mark == ("s1", 0.5)
    sandboxed = engine_factory(
        str(tmp_path), project_sandbox=parse_sandbox({"enabled": True}, operator=True)
    )
    with pytest.raises(EngineRefused, match="Antigravity lines take no sandbox"):
        sandboxed(LineSpec("probe", AGY, Strategy.SUBSCRIPTION), [])


def test_the_probes_flags_reach_antigravity_and_its_credential_refuses(
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The isolation flag reaches an Antigravity model, and the turn is then refused on
    its unverified credential before anything is located or started."""
    base = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "probe"]
    assert cli.main([*base, "--model", AGY]) == exits.FAILURE
    assert "--accept-partial-isolation" in capsys.readouterr().err
    flagged = [*base, "--model", AGY, "--accept-partial-isolation"]
    assert cli.main(flagged) == exits.FAILURE
    assert "unverified credential" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main([*base, "--help"])
    shown = " ".join(capsys.readouterr().out.split())
    assert "Antigravity's tools are refused before they run" in shown
    assert "--strategy api still bills per token" in shown
    assert "Antigravity ACP runtime" in shown and "step 3 of the verify-first" in shown


def test_the_probes_credential_flag_is_refused_outside_its_scope(
    tmp_path: Path,
    short_runtime: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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


def test_the_probe_runs_the_runtime_with_both_flags(
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The scripted runtime stands in for the real one, which is located under a fresh
    home directory, so no real runtime, profile or setting is reached."""
    server = FakeRuntime()

    @asynccontextmanager
    async def connect(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        server.started = True
        server.environment = dict(env)
        yield server

    located: list[str] = []

    def factory(command: str, *, keep_input: bool) -> ConnectionFactory:
        assert keep_input
        located.append(command)
        return connect

    home = tmp_path / "home"
    executable = install((home / DEFAULT_RUNTIME).parent)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv(RUNTIME_VARIABLE, raising=False)
    monkeypatch.setattr(antigravity, "json_lines_factory", factory)
    base = ["--project", str(tmp_path), "--runtime-dir", str(short_runtime), "probe"]
    flags = ["--accept-partial-isolation", "--accept-unverified-credential"]
    assert cli.main([*base, "--model", AGY, *flags]) == exits.OK
    shown = capsys.readouterr().out
    assert server.started and located == [str(executable)]
    assert "credential unverified" in shown and "turn success" in shown


# The safe point of priority mail, decided by the operator on 2026-10-27. A turn ends
# once a tool result is delivered and not at the text before the tool use.

type Steered = Callable[[str, Strategy], tuple[SteeredDriver, list[int]]]
REASON = "priority mail m-1 from @ana"


def test_priority_mail_ends_the_turn_at_the_tool_result_and_not_the_text(
    home: Path, runtime: Path, steered: Steered
) -> None:
    driver, asked = steered(AGY, Strategy.SUBSCRIPTION)
    events = [
        said("I will send it"),
        own(),
        permission(RUNNING, tool_name=OWN),
        changed(status="in_progress"),
        changed(status="completed", title=RUNNING, kind="other", toolName=OWN),
        said("sent"),
    ]
    server = FakeRuntime(events, stop=None)
    report = run(line(server, home, runtime, tools=True, loaded=driver))
    assert asked == [1]
    assert report.interrupted_for == REASON
    assert server.methods().count("session/cancel") == 1


GIT_SSH = GitSshAccess(
    "/run/agent.sock", "/keys/known_hosts", "/keys/deploy.pub", "0" * 64, "0" * 64
)
"""A resolved ``[git_ssh]`` agent, which no test here connects to."""


def test_an_antigravity_line_given_git_ssh_carries_its_command_and_no_other(
    home: Path, runtime: Path
) -> None:
    server = FakeRuntime()
    target = line(server, home, runtime)
    target.git_ssh = GIT_SSH
    run(target)
    assert server.environment[COMMAND_VARIABLE] == ssh_command(GIT_SSH)
    plain = FakeRuntime()
    run(line(plain, home, runtime, environment={COMMAND_VARIABLE: "x"}))
    assert COMMAND_VARIABLE not in plain.environment


@pytest.mark.parametrize(
    "status", ["pending", "failed", "in_progress", "completed", "unknown", None]
)
def test_a_rejected_call_must_not_execute(
    home: Path,
    runtime: Path,
    status: str | None,
) -> None:
    fields = {} if status is None else {"status": status}
    server = FakeRuntime([permission("blocked"), changed(**fields)])
    with pytest.raises(
        EngineRefused, match="did not confirm that a rejected tool call did not run"
    ):
        run(line(server, home, runtime, tools=True))
    assert server.methods().count("session/cancel") == 1


@pytest.mark.parametrize("status", ["pending", "failed", None])
def test_pending_denial_leaves_a_corrected_call_available(
    home: Path, runtime: Path, status: str | None
) -> None:
    server = FakeRuntime(
        [
            tool("blocked"),
            permission("blocked"),
            changed(status=status, _meta={"toolNotRun": True}),
            own("good"),
            permission(RUNNING, ident="good", tool_name=OWN, request="p2"),
            said("corrected"),
        ]
    )
    report = run(line(server, home, runtime, tools=True))
    assert report.text == "corrected" and report.tool_uses == (OWN,)
    assert server.answer("p1")["result"]["outcome"]["optionId"] == "no"
    assert server.answer("p2")["result"]["outcome"]["optionId"] == "once"
    assert "session/cancel" not in server.methods()


@pytest.mark.parametrize("tool_name", [None, OWN])
def test_permission_identifiers_are_single_use(
    home: Path, runtime: Path, tool_name: str | None
) -> None:
    server = FakeRuntime(
        [
            permission(RUNNING, tool_name=tool_name),
            permission(RUNNING, tool_name=tool_name, request="again"),
        ]
    )
    with pytest.raises(EngineRefused, match="reused a tool call identifier"):
        run(line(server, home, runtime, tools=True))
    assert server.methods().count("session/cancel") == 1


def test_pending_call_cannot_start_without_approval(home: Path, runtime: Path) -> None:
    server = FakeRuntime([tool("blocked"), changed(status="in_progress")])
    with pytest.raises(EngineRefused, match="not given, stopped after it started"):
        run(line(server, home, runtime, tools=True))


@pytest.mark.parametrize("status", ["in_progress", "completed", "failed"])
def test_permission_for_a_non_pending_call_stops(
    home: Path, runtime: Path, status: str
) -> None:
    request = permission("blocked")
    request["params"]["toolCall"]["status"] = status
    server = FakeRuntime([request])
    with pytest.raises(EngineRefused, match="no longer pending"):
        run(line(server, home, runtime, tools=True))
    assert "session/cancel" in server.methods()


def test_driver_budget_is_checked_before_permission_is_granted(
    home: Path, runtime: Path
) -> None:
    budget = driver(Strategy.SUBSCRIPTION).settings.tool_budget
    server = FakeRuntime(
        [
            permission(RUNNING, ident=f"t{i}", request=f"p{i}", tool_name=OWN)
            for i in range(budget + 1)
        ]
    )
    report = run(line(server, home, runtime, tools=True))
    assert report.interrupted_for == "tool_budget"
    assert report.tool_uses
    at_stop = server.answer(f"p{len(report.tool_uses) - 1}")
    assert at_stop["result"]["outcome"] == {"outcome": "cancelled"}


@pytest.mark.parametrize("rpc_error", [False, True])
@pytest.mark.parametrize("budget_stop", [False, True])
def test_cancel_transport_failure_keeps_the_original_stop_cause(
    home: Path, runtime: Path, rpc_error: bool, budget_stop: bool
) -> None:
    class BreaksOnCancel(FakeRuntime):
        async def send(self, message: Mapping[str, object]) -> None:
            if message.get("method") == "session/cancel":
                self.sent.append(dict(message))
                self.outbox.clear()
                self.stopped.set()
                if rpc_error:
                    self.outbox.append(
                        {
                            "jsonrpc": "2.0",
                            "id": self.prompt_id,
                            "error": {
                                "code": -32000,
                                "message": "cancel transport failed",
                            },
                        }
                    )
                return
            await super().send(message)

    budget = driver().settings.tool_budget
    events = (
        [
            permission(RUNNING, ident=f"t{i}", request=f"p{i}", tool_name=OWN)
            for i in range(budget + 1)
        ]
        if budget_stop
        else [own(), changed(kind="execute")]
    )
    server = BreaksOnCancel(events)
    target = line(server, home, runtime, tools=True)
    if budget_stop:
        report = run(target)
        assert report.interrupted_for == "tool_budget"
    else:
        with pytest.raises(EngineRefused, match="changed the kind"):
            run(target)


def test_non_execution_evidence_is_required_again_on_each_update(
    home: Path,
    runtime: Path,
) -> None:
    server = FakeRuntime(
        [
            permission("blocked"),
            changed(status="failed", _meta={"toolNotRun": True}),
            changed(status="failed"),
        ]
    )
    with pytest.raises(
        EngineRefused, match="did not confirm that a rejected tool call did not run"
    ):
        run(line(server, home, runtime, tools=True))
    assert server.methods().count("session/cancel") == 1
