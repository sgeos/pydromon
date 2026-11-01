"""The Grok Build adapter, driven by a scripted Agent Client Protocol server.

No test here reaches Grok, whose usage was exhausted when the adapter was written. The
script follows Grok Build 1.0.70's documentation and its ``initialize`` answer.
"""

import asyncio
import dataclasses
import json
import os
import re
import shutil
import tomllib
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

import pytest
from claude_agent_sdk import SdkMcpTool
from imagesamples import png

from dromon import grok as grok_module
from dromon.activity import ActivityKind, ActivityLog, Observer, ignore
from dromon.agents import AgentError, LineTokens
from dromon.daemon import Daemon
from dromon.driver import Credential, Driver, Strategy
from dromon.priority import SteeredDriver
from dromon.engine import EnginePlanLimit, EngineRefused, TurnReport
from dromon.eventlog import EventLog
from dromon.evidence import IN, OUT, Recording, Replay, read_recording, replay_factory
from dromon.grok import (
    COMPAT_ENVIRONMENT,
    HOME_CONFIG,
    HOME_DIR,
    SIGN_IN,
    READER_PROFILE,
    USER_HOME,
    USER_HOME_HARNESS_ENTRIES,
    WRITER_PROFILE,
    Connection,
    GrokLine,
    bridge_command,
    check_home,
    check_profile,
    check_project,
    check_user_home,
    claude_mode_refusal,
    engine_environment,
    grok_arguments,
    prepare_home,
    profile_name,
    unsupported,
)
from dromon.capabilities import Known
from dromon.lineconfig import LineConfig, WorkdirAccess
from dromon.security_paths import CREDENTIAL_PATHS
from dromon.gitssh import COMMAND_VARIABLE, GitSshAccess, ssh_command
from dromon.mcp import Bridge
from dromon.protocol import Response
from dromon.model import (
    LineAddress,
    LineCreated,
    OperatorAddress,
    OperatorRegistered,
    OwnershipCause,
    OwnershipChanged,
    RoleDeclared,
    SeatBound,
    format_due,
)
from dromon.paths import project_paths
from dromon.sandbox import parse_sandbox
from dromon.scheduler import LineEngine, LineSpec, Scheduler, engine_factory

GROK = "grok-4.7"


def grok(strategy: Strategy = Strategy.SUBSCRIPTION) -> Driver:
    return Driver.for_model(GROK, strategy)


async def _record(args: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": "ok"}]}


SEND: SdkMcpTool[Any] = SdkMcpTool(
    name="send_message", description="Send.", input_schema={}, handler=_record
)


def write_profiles(home: Path, workspace: Path, *, writer_git: bool = True) -> None:
    (home / HOME_DIR).mkdir(mode=0o700, parents=True, exist_ok=True)
    daemon_home = home.parent / "operator-home"
    reader = grok_module.profile_snippet(
        READER_PROFILE, workspace, (workspace / ".dromon",), daemon_home
    )
    writer = grok_module.profile_snippet(
        WRITER_PROFILE, workspace, (workspace / ".dromon",), daemon_home
    )
    if not writer_git:
        writer = "\n".join(
            line for line in writer.splitlines() if not line.startswith("read_only =")
        )
    (home / HOME_DIR / "sandbox.toml").write_text(reader + "\n\n" + writer + "\n")


def operator_home(home: Path) -> Path:
    """A placeholder daemon home, distinct from Grok's isolated line home."""
    return home.parent / "operator-home"


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = (tmp_path / "work").resolve()
    (workspace / ".dromon").mkdir(parents=True)
    (workspace / ".git").mkdir()
    home = tmp_path / "operator-home"
    write_profiles(home, workspace)
    return workspace, home, workspace / ".dromon"


def note(update: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "s1", "update": update},
    }


def chunk(text: str) -> dict[str, Any]:
    return note(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": text},
        }
    )


def tool_call(
    kind: str,
    name: str = "",
    ident: str = "t",
    raw_input: object | None = None,
) -> dict[str, Any]:
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call",
        "toolCallId": ident,
        "kind": kind,
        "toolName": name,
    }
    if raw_input is not None:
        update["rawInput"] = raw_input
    return note(update)


def call_update(ident: object, **fields: object) -> dict[str, Any]:
    """A ``tool_call_update`` for ``ident`` carrying only ``fields``."""
    return note({"sessionUpdate": "tool_call_update", "toolCallId": ident, **fields})


def permission(
    kind: str,
    name: str = "",
    request_id: str = "p1",
    ident: str = "t",
    raw_input: object | None = None,
) -> dict[str, Any]:
    tool_call_fields: dict[str, Any] = {
        "toolCallId": ident,
        "kind": kind,
        "toolName": name,
    }
    if raw_input is not None:
        tool_call_fields["rawInput"] = raw_input
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "s1",
            "toolCall": tool_call_fields,
            "options": [
                {"optionId": "yes", "kind": "allow_once", "name": "Allow"},
                {"optionId": "always", "kind": "allow_always", "name": "Always"},
                {"optionId": "no", "kind": "reject_once", "name": "Reject"},
            ],
        },
    }


def use_tool_input(target: str) -> dict[str, object]:
    return {"tool_name": target, "tool_input": dict[str, object]()}


def model_option(value: str) -> dict[str, object]:
    return {
        "id": "model",
        "category": "model",
        "type": "select",
        "currentValue": value,
        "options": [
            {"value": item, "name": "Grok"} for item in dict.fromkeys((value, GROK))
        ],
    }


def grok_notice(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """One of Grok's own notices, with the ``_x.ai/`` prefix Grok 1.0.70 sent."""
    return {"jsonrpc": "2.0", "method": f"_x.ai/{name}", "params": params}


DROMON_ONLY: Final = [{"name": "dromon", "source": "acp", "type": "stdio"}]
"""The servers a Grok line may load, Dromon's alone, in the shape of the probe."""

ABSENT: Final = object()
"""A session answer without ``modes``, as Grok 1.0.70 gave."""


class FakeGrok:
    """A scripted ``grok agent stdio``."""

    def __init__(
        self,
        turn: Sequence[dict[str, Any]] = (chunk("done"),),
        *,
        auth: object = "cached_token",
        model: object = GROK,
        modes: object = None,
        stop: str = "end_turn",
        servers: object = DROMON_ONLY,
        notices: Sequence[dict[str, Any]] = (),
    ) -> None:
        self.turn = list(turn)
        self.servers = servers
        """What ``servers_updated`` lists before the session opens, or ``None`` for
        no such notice."""
        self.notices = list(notices)
        """Grok's own notices, sent before the session opens."""
        self.after_set_config_notices: list[dict[str, Any]] = []
        """Grok's notices queued after the model option update."""
        self.extra: dict[str, object] = {}
        """More fields of the session's answer."""
        self.set_config_result: dict[str, object] = {
            "configOptions": [model_option(GROK)]
        }
        self.set_config_error: str | None = None
        self.auth = auth
        self.protocol_version = 1
        self.model = model
        self.modes: object = (
            {"currentModeId": "default", "availableModes": [{"id": "default"}]}
            if modes is None
            else modes
        )
        self.stop = stop
        self.sent: list[dict[str, Any]] = []
        self.queue: deque[dict[str, Any]] = deque()
        self.arguments: list[str] = []
        self.environment: dict[str, str] = {}
        self.prompt_id: object = None
        self.answer_prompt = True
        self.ignores_cancel = False
        self.blocked_method: str | None = None
        self.activity_delay = 0.0
        """Whether this Grok neither answers a cancel nor closes, so waits block."""
        self.stops = 0
        self.prompt_error: dict[str, object] | None = None

    def methods(self) -> list[str]:
        return [m["method"] for m in self.sent if "method" in m]

    def params(self, method: str) -> dict[str, Any]:
        return next(m["params"] for m in self.sent if m.get("method") == method)

    def answers(self) -> list[dict[str, Any]]:
        return [m for m in self.sent if "method" not in m]

    async def send(self, message: Mapping[str, object]) -> None:
        sent = dict(message)
        self.sent.append(sent)
        method = sent.get("method")
        if method == "session/cancel":
            if self.ignores_cancel:
                return
            # An ACP agent ends a cancelled prompt with stopReason cancelled.
            self.queue = deque(m for m in self.queue if m.get("id") != self.prompt_id)
            self.queue.append(
                {
                    "jsonrpc": "2.0",
                    "id": self.prompt_id,
                    "result": {"stopReason": "cancelled"},
                }
            )
            return
        if not isinstance(method, str) or "id" not in sent:
            return
        if method == self.blocked_method:
            return
        reply = {"jsonrpc": "2.0", "id": sent["id"]}
        if method == "initialize":
            self.queue.append(
                {
                    **reply,
                    "result": {
                        "protocolVersion": self.protocol_version,
                        "_meta": {"defaultAuthMethodId": self.auth},
                    },
                }
            )
        elif method in ("session/new", "session/load"):
            self.queue.extend(self.notices)
            if self.servers is not None:
                self.queue.append(
                    grok_notice("mcp/servers_updated", {"mcpServers": self.servers})
                )
            result: dict[str, object] = {}
            if self.modes is not ABSENT:
                result["modes"] = self.modes
            if method == "session/new":
                result["sessionId"] = "s1"
            if self.model is not None:
                result["models"] = {"currentModelId": self.model}
            result.update(self.extra)
            if self.model is not None and "configOptions" not in result:
                result["configOptions"] = [model_option(str(self.model))]
            self.queue.append({**reply, "result": result})
        elif method == "session/set_config_option":
            if self.set_config_error is not None:
                self.queue.append(
                    {
                        **reply,
                        "error": {"code": -32000, "message": self.set_config_error},
                    }
                )
            else:
                self.queue.append({**reply, "result": self.set_config_result})
                self.queue.extend(self.after_set_config_notices)
        elif method == "session/set_mode":
            self.queue.append({**reply, "result": {}})
        elif method == "session/prompt":
            self.prompt_id = sent["id"]
            if self.prompt_error is not None:
                self.queue.append({**reply, "error": self.prompt_error})
                return
            self.queue.extend(self.turn)
            if self.answer_prompt:
                self.queue.append({**reply, "result": {"stopReason": self.stop}})

    async def receive(self) -> dict[str, Any] | None:
        await asyncio.sleep(0)
        if not self.queue and (self.ignores_cancel or self.blocked_method is not None):
            await asyncio.Event().wait()
        message = self.queue.popleft() if self.queue else None
        if message is not None and message.get("method") == "session/update":
            await asyncio.sleep(self.activity_delay)
        return message


def line(
    server: FakeGrok,
    project: tuple[Path, Path, Path],
    *,
    config: LineConfig | None = None,
    driver: Driver | None = None,
    session_id: str | None = None,
    environment: Mapping[str, str] | None = None,
    observe: Observer = ignore,
) -> GrokLine:
    workspace, home, state = project

    @asynccontextmanager
    async def connect(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        del cwd
        server.arguments = arguments
        server.environment = dict(env)
        try:
            yield server
        finally:
            server.stops += 1

    return GrokLine(
        driver or grok(),
        workspace,
        config=config or LineConfig(),
        mcp_tools=[SEND],
        session_id=session_id,
        protected=(state,),
        bridge=["dromon-bridge", "--line", "alpha"],
        line_token="tok",  # noqa: S106, a test token
        environment={} if environment is None else environment,
        home=home,
        connection_factory=connect,
        observe=observe,
    )


def run(target: GrokLine, prompt: str = "hello") -> TurnReport:
    return asyncio.run(target.run_turn(prompt))


# Configuration and environment.


def test_a_grok_line_takes_its_supported_tools_and_nothing_else() -> None:
    assert unsupported(LineConfig(builtin_tools=("Bash", "Read", "Edit"))) is None
    assert "not Agent" in (unsupported(LineConfig(builtin_tools=("Agent",))) or "")
    assert "MCP servers" in (unsupported(LineConfig(inherit_account_mcp=True)) or "")
    assert "Claude Code's" in (unsupported(LineConfig(claude_code_prompt=True)) or "")
    sandboxed = LineConfig(sandbox=parse_sandbox({"enabled": True}, operator=True))
    assert "Grok sandbox profile" in (unsupported(sandboxed) or "")


def test_a_read_only_grok_line_is_refused_until_the_capability_is_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = LineConfig(builtin_tools=("Bash",), workdir_access=WorkdirAccess.READ)
    assert grok_module.GROK_CAPABILITIES.read_only_workdir is not Known.YES
    assert "read-only working directory" in (unsupported(reader) or "")
    assert unsupported(LineConfig(builtin_tools=("Bash",))) is None
    confirmed = dataclasses.replace(
        grok_module.GROK_CAPABILITIES, read_only_workdir=Known.YES
    )
    monkeypatch.setattr(grok_module, "GROK_CAPABILITIES", confirmed)
    assert unsupported(reader) is None


def test_a_read_only_grok_line_refuses_the_turn_before_grok_starts(
    project: tuple[Path, Path, Path],
) -> None:
    server = probed_grok()
    reader = LineConfig(workdir_access=WorkdirAccess.READ)
    with pytest.raises(EngineRefused, match="read-only working directory"):
        run(line(server, project, config=reader))
    assert server.methods() == []


def test_the_environment_drops_credentials_and_refuses_redirection() -> None:
    environment = {
        "PATH": "/bin",
        "XAI_API_KEY": "k",
        "GROK_AUTH": "a",
        "GROK_AUTH_PATH": "/elsewhere/auth.json",
        "GROK_CONFIG": "{}",
        "GROK_HOME": "/elsewhere",
        "GROK_SANDBOX": "off",
        "HOME": "/home/operator",
        "XDG_CONFIG_HOME": "/home/operator/.config",
        "XDG_DATA_HOME": "/home/operator/.local/share",
        "XDG_STATE_HOME": "/home/operator/.local/state",
        "XDG_CACHE_HOME": "/home/operator/.cache",
    }
    assert set(grok_module.HOME_VARIABLES) < set(environment)
    kept = {"PATH": "/bin", "HOME": "/home/operator", **COMPAT_ENVIRONMENT}
    assert engine_environment(environment, Strategy.SUBSCRIPTION) == kept
    assert engine_environment(environment, Strategy.FLEXIBLE) == kept
    with pytest.raises(EngineRefused, match="api strategy"):
        engine_environment(environment, Strategy.API)
    for name in grok_module.REFUSED_ENVIRONMENT:
        with pytest.raises(EngineRefused, match=name):
            engine_environment({name: "x"}, Strategy.SUBSCRIPTION)


def test_the_arguments_name_the_model_and_map_effort() -> None:
    assert grok_arguments(GROK, "high") == [
        "agent",
        "--no-leader",
        "--model",
        GROK,
        "--reasoning-effort",
        "high",
        "stdio",
    ]
    assert grok_arguments(GROK, "max")[5] == "xhigh"


# Dromon's Grok home.


def prepared(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A workspace, its state directory and a Grok home Dromon has prepared."""
    workspace = (tmp_path / "work").resolve()
    (workspace / ".dromon").mkdir(parents=True)
    (workspace / ".git").mkdir()
    grok_home = tmp_path / "home" / HOME_DIR
    prepare_home(
        grok_home,
        workspace,
        (workspace / ".dromon",),
        tmp_path / "operator-home",
    )
    return workspace, workspace / ".dromon", grok_home


def test_the_sign_in_command_names_dromons_grok_home_and_its_home() -> None:
    grok_home = f"$HOME/{HOME_DIR.as_posix()}"
    user_home = f"{grok_home}/{USER_HOME}"
    assert SIGN_IN.startswith(f'mkdir -p -m 700 "{user_home}" && ')
    assert SIGN_IN.endswith(f' GROK_HOME="{grok_home}" HOME="{user_home}" grok login')
    assert HOME_DIR.parts == (".grok", "dromon-home")
    protocol = Path(__file__).parents[1] / "docs" / "protocol" / "GROK.md"
    assert SIGN_IN in protocol.read_text(encoding="utf-8")


def test_a_prepared_home_is_owner_only_and_passes_its_own_checks(
    tmp_path: Path,
) -> None:
    workspace, state, grok_home = prepared(tmp_path)
    assert grok_home.stat().st_mode & 0o077 == 0
    config = grok_home / "config.toml"
    assert config.read_text() == HOME_CONFIG
    assert config.stat().st_mode & 0o077 == 0
    assert not (grok_home / "auth.json").exists()
    check_home(grok_home, tmp_path / "etc")
    home = tmp_path / "home"
    for tools in (("Bash",), ("Edit",)):
        check_profile(
            LineConfig(builtin_tools=tools),
            workspace,
            (state,),
            home,
            grok_home,
            operator_home(home),
        )


def test_the_home_config_asks_and_loads_no_server_or_hook() -> None:
    document = tomllib.loads(HOME_CONFIG)
    assert document["ui"] == {"permission_mode": "default"}
    assert document["ui"]["permission_mode"] in grok_module.ASK_MODES
    assert "mcp_servers" not in document and "hooks" not in document
    for vendor in ("claude", "cursor"):
        assert document["compat"][vendor] == {"mcps": False, "hooks": False}


def test_preparing_never_replaces_an_existing_file(tmp_path: Path) -> None:
    workspace, state, grok_home = prepared(tmp_path)
    (grok_home / "config.toml").write_text("[ui]\nyolo = true\n")
    (grok_home / "sandbox.toml").write_text("# the operator's own\n")
    prepare_home(grok_home, workspace, (state,))
    assert (grok_home / "config.toml").read_text() == "[ui]\nyolo = true\n"
    assert (grok_home / "sandbox.toml").read_text() == "# the operator's own\n"


def test_a_linked_or_shared_home_is_refused(tmp_path: Path) -> None:
    workspace, state, grok_home = prepared(tmp_path)
    linked = tmp_path / "linked"
    linked.symlink_to(grok_home)
    with pytest.raises(EngineRefused, match="symbolic link"):
        prepare_home(linked, workspace, (state,))
    grok_home.chmod(0o770)
    with pytest.raises(EngineRefused, match="other users"):
        prepare_home(grok_home, workspace, (state,))
    file = tmp_path / "file"
    file.write_text("")
    with pytest.raises(EngineRefused, match="cannot create|not a directory"):
        prepare_home(file, workspace, (state,))


def test_a_prepared_home_holds_the_lines_own_empty_home_directory(
    tmp_path: Path,
) -> None:
    _, _, grok_home = prepared(tmp_path)
    user_home = grok_home / USER_HOME
    assert user_home.is_dir() and not user_home.is_symlink()
    assert user_home.stat().st_mode & 0o077 == 0
    assert list(user_home.iterdir()) == []
    check_user_home(user_home)


def test_a_linked_or_shared_line_home_directory_is_refused(tmp_path: Path) -> None:
    workspace, state, grok_home = prepared(tmp_path)
    user_home = grok_home / USER_HOME
    user_home.chmod(0o777)
    with pytest.raises(EngineRefused, match="home directory .* other users"):
        prepare_home(grok_home, workspace, (state,))
    user_home.rmdir()
    user_home.symlink_to(tmp_path / "home")
    with pytest.raises(EngineRefused, match="home directory .* symbolic link"):
        prepare_home(grok_home, workspace, (state,))


@pytest.mark.parametrize("name", USER_HOME_HARNESS_ENTRIES)
def test_another_harness_in_the_line_home_directory_is_refused(
    tmp_path: Path, name: str
) -> None:
    _, _, grok_home = prepared(tmp_path)
    user_home = grok_home / USER_HOME
    (user_home / name).mkdir()
    check_user_home(user_home)
    (user_home / name / "settings.json").write_text("{}")
    with pytest.raises(EngineRefused, match=f"holds {re.escape(name)},"):
        check_user_home(user_home)
    shutil.rmtree(user_home / name)
    (user_home / name).write_text("{}")
    with pytest.raises(EngineRefused, match=f"holds {re.escape(name)},"):
        check_user_home(user_home)


def test_the_line_home_directory_names_the_harnesses_grok_reads() -> None:
    assert set(USER_HOME_HARNESS_ENTRIES) >= {".claude", ".cursor", ".claude.json"}
    guide = (Path(__file__).parents[1] / "docs" / "protocol" / "GROK.md").read_text(
        encoding="utf-8"
    )
    for name in USER_HOME_HARNESS_ENTRIES:
        assert f"`~/{name}`" in guide


@pytest.mark.parametrize(
    "document",
    [
        pytest.param('[ui]\npermission_mode = "always-approve"\n', id="always"),
        pytest.param(HOME_CONFIG + "\n[ui.extra]\nyolo = true\n", id="added"),
        pytest.param(
            HOME_CONFIG + '\n[mcp_servers.notes]\ncommand = "notes"\n', id="server"
        ),
        pytest.param(HOME_CONFIG.replace("hooks = false", "hooks = true"), id="hooks"),
        pytest.param("", id="empty"),
    ],
)
def test_any_drift_in_the_home_config_is_refused(tmp_path: Path, document: str) -> None:
    _, _, grok_home = prepared(tmp_path)
    (grok_home / "config.toml").write_text(document)
    with pytest.raises(EngineRefused, match="not the configuration Dromon wrote"):
        check_home(grok_home, tmp_path / "etc")


def test_the_home_config_is_compared_by_its_contents(tmp_path: Path) -> None:
    _, _, grok_home = prepared(tmp_path)
    (grok_home / "config.toml").write_text(
        "[compat.cursor]\nhooks = false\nmcps = false\n"
        '[ui]\npermission_mode = "default"\n'
        "[compat.claude]\nhooks = false\nmcps = false\n"
    )
    check_home(grok_home, tmp_path / "etc")
    (grok_home / "config.toml").write_text("not toml [")
    with pytest.raises(EngineRefused, match="is not TOML"):
        check_home(grok_home, tmp_path / "etc")
    (grok_home / "config.toml").unlink()
    with pytest.raises(EngineRefused, match="not the configuration Dromon wrote"):
        check_home(grok_home, tmp_path / "etc")


@pytest.mark.parametrize("name", grok_module.POLICY_FILES)
@pytest.mark.parametrize("system", [False, True], ids=["home", "system"])
def test_a_policy_file_may_exist_only_empty(
    tmp_path: Path, name: str, system: bool
) -> None:
    _, _, grok_home = prepared(tmp_path)
    etc = tmp_path / "etc"
    etc.mkdir()
    file = (etc if system else grok_home) / name
    file.write_text("")
    check_home(grok_home, etc)
    file.write_text("[permission]\n")
    check_home(grok_home, etc)
    file.write_text('[ui]\npermission_mode = "always-approve"\n')
    with pytest.raises(EngineRefused, match="sets Grok policy"):
        check_home(grok_home, etc)


def test_a_trusted_folder_is_refused(tmp_path: Path) -> None:
    _, _, grok_home = prepared(tmp_path)
    trust = grok_home / grok_module.TRUST_FILE
    trust.write_text("[folders]\n")
    check_home(grok_home, tmp_path / "etc")
    trust.write_text('[folders."/work"]\ntrusted = true\n')
    with pytest.raises(EngineRefused, match="trusts a folder"):
        check_home(grok_home, tmp_path / "etc")


@pytest.mark.parametrize("name", grok_module.REFUSED_HOME_ENTRIES)
def test_an_entry_that_loads_more_is_refused_unless_an_empty_folder(
    tmp_path: Path, name: str
) -> None:
    _, _, grok_home = prepared(tmp_path)
    entry = grok_home / name
    entry.mkdir()
    check_home(grok_home, tmp_path / "etc")
    (entry / "invented.md").write_text("content")
    with pytest.raises(EngineRefused, match=f"holds {name}"):
        check_home(grok_home, tmp_path / "etc")
    (entry / "invented.md").unlink()
    entry.rmdir()
    entry.write_text("")
    if name == "hooks-paths":
        check_home(grok_home, tmp_path / "etc")
    else:
        with pytest.raises(EngineRefused, match=f"holds {name}"):
            check_home(grok_home, tmp_path / "etc")
    entry.write_text("content")
    with pytest.raises(EngineRefused, match=f"holds {name}"):
        check_home(grok_home, tmp_path / "etc")


def test_installed_plugins_allows_only_empty_regular_lock_files(tmp_path: Path) -> None:
    _, _, grok_home = prepared(tmp_path)
    plugins = grok_home / "installed-plugins"
    plugins.mkdir()
    (plugins / "registry.lock").write_text("")
    check_home(grok_home, tmp_path / "etc")
    (plugins / "registry.lock").write_text("locked")
    with pytest.raises(EngineRefused, match="holds installed-plugins"):
        check_home(grok_home, tmp_path / "etc")
    (plugins / "registry.lock").unlink()
    (plugins / "unexpected.json").write_text("")
    with pytest.raises(EngineRefused, match="holds installed-plugins"):
        check_home(grok_home, tmp_path / "etc")


def test_empty_hooks_paths_file_is_allowed_but_a_symlink_is_not(tmp_path: Path) -> None:
    _, _, grok_home = prepared(tmp_path)
    hooks_paths = grok_home / "hooks-paths"
    hooks_paths.write_text("")
    check_home(grok_home, tmp_path / "etc")
    hooks_paths.unlink()
    target = tmp_path / "hooks-paths"
    target.write_text("")
    hooks_paths.symlink_to(target)
    with pytest.raises(EngineRefused, match="holds hooks-paths"):
        check_home(grok_home, tmp_path / "etc")


def test_installed_plugins_refuses_a_lock_symlink(tmp_path: Path) -> None:
    _, _, grok_home = prepared(tmp_path)
    plugins = grok_home / "installed-plugins"
    plugins.mkdir()
    target = tmp_path / "registry.lock"
    target.write_text("")
    (plugins / "registry.lock").symlink_to(target)
    with pytest.raises(EngineRefused, match="holds installed-plugins"):
        check_home(grok_home, tmp_path / "etc")


def test_failure_to_lstat_a_refused_entry_refuses_the_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, grok_home = prepared(tmp_path)
    entry = grok_home / "installed-plugins"
    entry.mkdir()
    original_lstat = Path.lstat

    def denied_lstat(path: Path) -> os.stat_result:
        if path == entry:
            raise PermissionError("inspection denied")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", denied_lstat)
    with pytest.raises(EngineRefused, match="cannot inspect Grok home entry"):
        check_home(grok_home, tmp_path / "etc")


def test_an_oversized_home_file_is_refused(tmp_path: Path) -> None:
    _, _, grok_home = prepared(tmp_path)
    (grok_home / "config.toml").write_text("#" * (grok_module.MAX_HOME_FILE + 1))
    with pytest.raises(EngineRefused, match="exceeds"):
        check_home(grok_home, tmp_path / "etc")


# Claude Code's settings, which Grok also reads.


def settings(directory: Path, document: object, name: str = "settings.json") -> None:
    (directory / ".claude").mkdir(parents=True, exist_ok=True)
    (directory / ".claude" / name).write_text(json.dumps(document))


def test_no_claude_settings_let_a_grok_turn_run(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    (workspace / ".git").mkdir(parents=True)
    assert claude_mode_refusal(tmp_path / "home", workspace) is None


@pytest.mark.parametrize(
    ("document", "refused"),
    [
        pytest.param({"permissions": {"defaultMode": "auto"}}, "'auto'", id="auto"),
        pytest.param(
            {"permissions": {"defaultMode": "bypassPermissions"}},
            "'bypassPermissions'",
            id="bypass",
        ),
        pytest.param({"defaultMode": "acceptEdits"}, "'acceptEdits'", id="top"),
        pytest.param(
            {"permissions": {"defaultMode": "default"}, "defaultMode": "auto"},
            None,
            id="nested-wins",
        ),
        pytest.param({"permissions": {"defaultMode": "default"}}, None, id="default"),
        pytest.param({"permissions": {"allow": ["Read"]}}, None, id="no-mode"),
        pytest.param(["not", "an", "object"], None, id="list"),
    ],
)
@pytest.mark.parametrize("level", ["user", "user-local", "project", "repository"])
def test_a_claude_settings_mode_other_than_asking_is_refused(
    tmp_path: Path, document: object, refused: str | None, level: str
) -> None:
    home = tmp_path / "home"
    repository = tmp_path / "repo"
    workspace = repository / "packages" / "one"
    workspace.mkdir(parents=True)
    (repository / ".git").mkdir()
    if level == "user":
        settings(home, document)
    elif level == "user-local":
        settings(home, document, "settings.local.json")
    elif level == "project":
        settings(workspace, document, "settings.local.json")
    else:
        settings(repository, document)
    reason = claude_mode_refusal(home, workspace)
    if refused is None:
        assert reason is None
    else:
        assert reason is not None and f"defaultMode to {refused}" in reason


def test_claude_settings_above_the_repository_root_are_not_read(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "outer" / "repo"
    (repository / ".git").mkdir(parents=True)
    settings(tmp_path / "outer", {"permissions": {"defaultMode": "auto"}})
    assert claude_mode_refusal(tmp_path / "home", repository) is None


def test_outside_a_repository_only_the_workspace_settings_are_read(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "outer" / "work"
    workspace.mkdir(parents=True)
    settings(tmp_path / "outer", {"permissions": {"defaultMode": "auto"}})
    assert claude_mode_refusal(tmp_path / "home", workspace) is None
    settings(workspace, {"permissions": {"defaultMode": "auto"}})
    assert claude_mode_refusal(tmp_path / "home", workspace) is not None


def test_unreadable_claude_settings_refuse_the_turn(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    (workspace / ".git").mkdir(parents=True)
    (workspace / ".claude").mkdir()
    (workspace / ".claude" / "settings.json").write_text("{")
    reason = claude_mode_refusal(tmp_path / "home", workspace)
    assert reason is not None and "is not JSON" in reason


# The sandbox profile.


def test_a_reader_and_a_writer_get_their_own_profiles(
    project: tuple[Path, Path, Path],
) -> None:
    workspace, home, state = project
    grok_home = home / HOME_DIR
    reader = check_profile(
        LineConfig(builtin_tools=("Bash",)),
        workspace,
        (state,),
        home,
        grok_home,
        operator_home(home),
    )
    writer = check_profile(
        LineConfig(builtin_tools=("Edit",)),
        workspace,
        (state,),
        home,
        grok_home,
        operator_home(home),
    )
    assert (reader, writer) == (READER_PROFILE, WRITER_PROFILE)


def test_read_only_workdir_uses_the_grok_reader_profile() -> None:
    config = LineConfig(
        builtin_tools=("Bash", "Edit", "Write"),
        workdir_access=WorkdirAccess.READ,
    )
    assert profile_name(config) == READER_PROFILE


def test_a_missing_profile_is_refused_with_the_table_to_add(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    (workspace / ".dromon").mkdir(parents=True)
    home = tmp_path / "home"
    (operator_home(home) / ".ssh").mkdir(parents=True)
    with pytest.raises(EngineRefused) as raised:
        check_profile(
            LineConfig(),
            workspace,
            (workspace / ".dromon",),
            home,
            home / HOME_DIR,
            operator_home(home),
        )
    message = str(raised.value)
    assert f"no Grok sandbox profile {READER_PROFILE}" in message
    assert str(home / HOME_DIR / "sandbox.toml") in message
    assert f"[profiles.{READER_PROFILE}]" in message
    assert 'extends = "read-only"' in message and '".dromon"' in message
    assert "operator-home/.ssh" in message and "operator-home/.aws" not in message
    assert "~/" not in message


def test_a_missing_credential_path_does_not_refuse_a_profile(tmp_path: Path) -> None:
    workspace = (tmp_path / "work").resolve()
    (workspace / ".dromon").mkdir(parents=True)
    home = tmp_path / "home"
    write_reader(home, 'extends = "read-only"\ndeny = [".dromon"]')
    assert (
        check_profile(
            LineConfig(),
            workspace,
            (workspace / ".dromon",),
            home,
            home / HOME_DIR,
            operator_home(home),
        )
        == READER_PROFILE
    )


def test_an_existing_credential_path_must_be_denied(tmp_path: Path) -> None:
    workspace = (tmp_path / "work").resolve()
    state = workspace / ".dromon"
    state.mkdir(parents=True)
    home = tmp_path / "home"
    daemon_home = operator_home(home)
    (daemon_home / ".ssh").mkdir(parents=True)
    write_reader(home, 'extends = "read-only"\ndeny = [".dromon"]')
    with pytest.raises(EngineRefused, match="does not deny .*operator-home/\\.ssh"):
        check_profile(
            LineConfig(), workspace, (state,), home, home / HOME_DIR, daemon_home
        )


def test_credentials_created_later_are_named_in_refusal_advice(tmp_path: Path) -> None:
    workspace = (tmp_path / "work").resolve()
    state = workspace / ".dromon"
    state.mkdir(parents=True)
    home = tmp_path / "home"
    daemon_home = operator_home(home)
    profile_file = home / HOME_DIR / "sandbox.toml"
    profile_file.parent.mkdir(parents=True)
    profile_file.write_text(
        grok_module.profile_snippet(READER_PROFILE, workspace, (state,), daemon_home)
    )
    credential = daemon_home / ".cargo" / "credentials.toml"
    credential.parent.mkdir(parents=True)
    credential.write_text("token = 'placeholder'\n")
    with pytest.raises(EngineRefused) as raised:
        check_profile(
            LineConfig(), workspace, (state,), home, home / HOME_DIR, daemon_home
        )
    message = str(raised.value)
    assert str(credential) in message
    assert str(credential) in message.split("Missing deny entries are ", 1)[1]


def test_grok_profile_derives_existing_credential_denies_from_shared_list(
    tmp_path: Path,
) -> None:
    home = tmp_path / "operator-home"
    existing = home / CREDENTIAL_PATHS[-1]
    existing.parent.mkdir(parents=True)
    existing.write_text("placeholder")
    missing = home / CREDENTIAL_PATHS[0]
    snippet = grok_module.profile_snippet(READER_PROFILE, tmp_path / "work", (), home)
    assert str(existing.resolve()) in snippet
    assert str(missing) not in snippet


def test_a_symlinked_credential_is_denied_by_its_real_path(tmp_path: Path) -> None:
    workspace = (tmp_path / "work").resolve()
    state = workspace / ".dromon"
    state.mkdir(parents=True)
    home = tmp_path / "home"
    daemon_home = operator_home(home)
    ssh = daemon_home / ".ssh"
    ssh.parent.mkdir(parents=True)
    target = tmp_path / "external-ssh"
    target.mkdir()
    ssh.symlink_to(target)
    snippet = grok_module.profile_snippet(
        READER_PROFILE, workspace, (state,), daemon_home
    )
    assert str(target.resolve()) in snippet
    assert str(ssh) not in snippet


def test_profile_exempts_only_groks_sign_in_and_denies_claude_auth(
    tmp_path: Path,
) -> None:
    home = tmp_path / "operator-home"
    grok_auth = home / ".grok" / "auth.json"
    grok_auth.parent.mkdir(parents=True)
    grok_auth.write_text("{}")
    claude_auth = home / ".claude.json"
    claude_auth.write_text("{}")
    snippet = grok_module.profile_snippet(READER_PROFILE, tmp_path / "work", (), home)
    assert ".grok/auth.json" not in snippet
    assert str(claude_auth.resolve()) in snippet


def test_a_dangling_grok_credential_symlink_is_denied(tmp_path: Path) -> None:
    home = tmp_path / "operator-home"
    dangling = home / ".npmrc"
    home.mkdir()
    dangling.symlink_to(tmp_path / "not-mounted" / "npmrc")
    snippet = grok_module.profile_snippet(READER_PROFILE, tmp_path / "work", (), home)
    assert str(dangling.absolute()) in snippet


def test_an_existing_profile_gets_additive_advice_and_keeps_other_tables(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "work").resolve()
    state = workspace / ".dromon"
    state.mkdir(parents=True)
    home = tmp_path / "home"
    file = home / HOME_DIR / "sandbox.toml"
    file.parent.mkdir(parents=True)
    file.write_text(
        f'[profiles.{READER_PROFILE}]\nextends = "read-only"\n'
        '[profiles.other-project]\nextends = "workspace"\ndeny = ["/other"]\n'
    )
    with pytest.raises(EngineRefused) as raised:
        check_profile(
            LineConfig(),
            workspace,
            (state,),
            home,
            home / HOME_DIR,
            operator_home(home),
        )
    message = str(raised.value)
    assert "leave every other profile table and entry as it is" in message
    assert "creating either array if the profile has none" in message
    assert "Missing deny entries" in message
    assert "missing read_only entries are []" in message
    assert "missing read_only entries are []" in message
    assert "Remove unsupported keys" not in message
    assert "Set extends" not in message
    assert f"\n[profiles.{READER_PROFILE}]" not in message
    assert "\n[profiles.other-project]" not in message


def test_existing_profile_advice_lists_only_missing_deny_entries(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "work").resolve()
    state = workspace / ".dromon"
    state.mkdir(parents=True)
    home = tmp_path / "home"
    daemon_home = operator_home(home)
    existing = daemon_home / ".ssh"
    existing.mkdir(parents=True)
    missing = daemon_home / ".netrc"
    missing.write_text("placeholder")
    write_reader(
        home,
        f'extends = "read-only"\ndeny = [".dromon", "{existing}"]',
    )
    with pytest.raises(EngineRefused) as raised:
        check_profile(
            LineConfig(), workspace, (state,), home, home / HOME_DIR, daemon_home
        )
    advice = str(raised.value).split("Missing deny entries are ", 1)[1]
    assert str(missing) in advice
    assert str(existing) not in advice


def write_reader(home: Path, table: str) -> None:
    (home / HOME_DIR).mkdir(parents=True, exist_ok=True)
    (home / HOME_DIR / "sandbox.toml").write_text(
        f"[profiles.{READER_PROFILE}]\n{table}\n"
    )


def test_grok_profile_must_hold_configured_paths_read_only(
    project: tuple[Path, Path, Path],
) -> None:
    workspace, home, state = project
    readonly = workspace / "docs" / "process"
    readonly.mkdir(parents=True)
    daemon_home = operator_home(home)
    write_reader(home, 'extends = "read-only"\ndeny = [".dromon"]')
    config = LineConfig(builtin_tools=("Bash",))
    with pytest.raises(EngineRefused, match="required paths read-only"):
        check_profile(
            config,
            workspace,
            (state,),
            home,
            home / HOME_DIR,
            daemon_home,
            (readonly,),
        )
    write_reader(
        home,
        f'extends = "read-only"\ndeny = [".dromon"]\nread_only = ["{readonly}"]',
    )
    assert (
        check_profile(
            config,
            workspace,
            (state,),
            home,
            home / HOME_DIR,
            daemon_home,
            (readonly,),
        )
        == READER_PROFILE
    )


@pytest.mark.parametrize(
    ("table", "reason"),
    [
        ('extends = "workspace"\ndeny = [".dromon"]', "extends 'workspace'"),
        ('extends = "devbox"\ndeny = [".dromon"]', "extends 'devbox'"),
        ('extends = "read-only"\ndeny = ["**/.dromon"]', "does not deny"),
        ('extends = "read-only"\ndeny = ["other"]', "does not deny"),
        ('extends = "read-only"', "does not deny"),
        (
            'extends = "read-only"\ndeny = [".dromon"]\nread_write = ["/"]',
            "sets read_write",
        ),
        ('extends = "read-only"\ndeny = [".dromon"]\nlater = 1', "sets later"),
    ],
)
def test_an_unsafe_reader_profile_is_refused(
    tmp_path: Path, table: str, reason: str
) -> None:
    workspace = tmp_path / "work"
    (workspace / ".dromon").mkdir(parents=True)
    home = tmp_path / "home"
    write_reader(home, table)
    with pytest.raises(EngineRefused, match=reason) as raised:
        check_profile(
            LineConfig(),
            workspace,
            (workspace / ".dromon",),
            home,
            home / HOME_DIR,
            operator_home(home),
        )
    if reason.startswith("extends"):
        assert "Set extends to 'read-only'" in str(raised.value)
    elif "read_write" in table or "later =" in table:
        assert "Remove unsupported keys from this profile" in str(raised.value)
    else:
        assert "Remove unsupported keys" not in str(raised.value)
        assert "Set extends" not in str(raised.value)


def test_a_writer_must_keep_git_read_only(tmp_path: Path) -> None:
    workspace = (tmp_path / "work").resolve()
    (workspace / ".dromon").mkdir(parents=True)
    (workspace / ".git").mkdir()
    home = tmp_path / "home"
    write_profiles(home, workspace, writer_git=False)
    with pytest.raises(EngineRefused, match="does not hold git directories read-only"):
        check_profile(
            LineConfig(builtin_tools=("Edit",)),
            workspace,
            (workspace / ".dromon",),
            home,
            home / HOME_DIR,
            operator_home(home),
        )


def test_writer_profile_holds_an_ordinary_git_directory_read_only(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "work").resolve()
    (workspace / ".dromon").mkdir(parents=True)
    git = workspace / ".git"
    git.mkdir()
    home = tmp_path / "home"
    write_profiles(home, workspace)
    document = tomllib.loads((home / HOME_DIR / "sandbox.toml").read_text())
    writer = document["profiles"][WRITER_PROFILE]
    assert writer["read_only"] == [str(git)]
    assert (
        check_profile(
            LineConfig(builtin_tools=("Edit",)),
            workspace,
            (workspace / ".dromon",),
            home,
            home / HOME_DIR,
            operator_home(home),
        )
        == WRITER_PROFILE
    )


def linked_worktree(workspace: Path, common: Path, gitdir: Path) -> None:
    workspace.mkdir(parents=True)
    (workspace / ".dromon").mkdir()
    common.mkdir(parents=True)
    gitdir.mkdir(parents=True)
    (gitdir / "commondir").write_text(os.path.relpath(common, gitdir))
    (workspace / ".git").write_text(f"gitdir: {gitdir}\n")


def test_writer_profile_holds_linked_worktree_and_common_git_directories(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worktree"
    common = tmp_path / "repository.git"
    gitdir = common / "worktrees" / "feature"
    linked_worktree(workspace, common, gitdir)
    home = tmp_path / "home"
    write_profiles(home, workspace)
    document = tomllib.loads((home / HOME_DIR / "sandbox.toml").read_text())
    writer = document["profiles"][WRITER_PROFILE]
    assert set(writer["read_only"]) == {str(common), str(gitdir)}
    assert str(workspace / ".git") not in writer["read_only"]
    assert str(gitdir / "commondir") not in writer["read_only"]
    assert (
        check_profile(
            LineConfig(builtin_tools=("Edit",)),
            workspace,
            (workspace / ".dromon",),
            home,
            home / HOME_DIR,
            operator_home(home),
        )
        == WRITER_PROFILE
    )


def test_rewritten_linked_worktree_pointer_is_refused_on_the_next_check(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "worktree"
    common = tmp_path / "repository.git"
    gitdir = common / "worktrees" / "feature"
    linked_worktree(workspace, common, gitdir)
    home = tmp_path / "home"
    write_profiles(home, workspace)
    replacement_common = tmp_path / "replacement.git"
    replacement_gitdir = replacement_common / "worktrees" / "feature"
    replacement_common.mkdir()
    replacement_gitdir.mkdir(parents=True)
    (replacement_gitdir / "commondir").write_text(
        os.path.relpath(replacement_common, replacement_gitdir)
    )
    (workspace / ".git").write_text(f"gitdir: {replacement_gitdir}\n")
    with pytest.raises(EngineRefused, match="does not hold git directories read-only"):
        check_profile(
            LineConfig(builtin_tools=("Edit",)),
            workspace,
            (workspace / ".dromon",),
            home,
            home / HOME_DIR,
            operator_home(home),
        )


def test_deny_entries_resolve_as_grok_documents(tmp_path: Path) -> None:
    workspace = (tmp_path / "work").resolve()
    (workspace / ".dromon").mkdir(parents=True)
    home = (tmp_path / "home").resolve()
    runtime = home / "run"
    runtime.mkdir(parents=True)
    write_reader(home, 'extends = "read-only"\ndeny = [".dromon", "~/run"]')
    protected = (workspace / ".dromon", runtime)
    assert (
        check_profile(
            LineConfig(),
            workspace,
            protected,
            home,
            home / HOME_DIR,
            operator_home(home),
        )
        == READER_PROFILE
    )


def test_neither_the_projects_nor_the_operators_own_profile_is_read(
    tmp_path: Path,
) -> None:
    workspace = (tmp_path / "work").resolve()
    (workspace / ".dromon").mkdir(parents=True)
    table = f'[profiles.{READER_PROFILE}]\nextends = "read-only"\ndeny = [".dromon"]\n'
    (workspace / ".grok").mkdir()
    (workspace / ".grok" / "sandbox.toml").write_text(table)
    home = tmp_path / "home"
    (home / ".grok").mkdir(parents=True)
    (home / ".grok" / "sandbox.toml").write_text(table)
    with pytest.raises(EngineRefused, match="no Grok sandbox profile"):
        check_profile(
            LineConfig(),
            workspace,
            (workspace / ".dromon",),
            home,
            home / HOME_DIR,
            operator_home(home),
        )


# A turn.


def test_a_plain_turn_reports_text_credential_and_session(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok([chunk("hello "), chunk("there")])
    target = line(
        server,
        project,
        environment={"PATH": "/bin", "DAEMON_ONLY_SENTINEL": "not-allowlisted"},
    )
    report = run(target)
    assert (report.subtype, report.text, report.session_id) == (
        "success",
        "hello there",
        "s1:rules-" + grok_module.session_digest(LineConfig(), ""),
    )
    assert report.credential is Credential.SUBSCRIPTION
    assert target.session_id == "s1:rules-" + grok_module.session_digest(
        LineConfig(), ""
    )
    assert "DAEMON_ONLY_SENTINEL" not in server.environment
    assert server.environment["GROK_SANDBOX"] == READER_PROFILE
    init = server.params("initialize")
    assert init["clientCapabilities"] == {
        "fs": {"readTextFile": False, "writeTextFile": False},
        "terminal": False,
    }


def test_an_unstructured_grok_rate_limit_error_is_not_a_plan_limit(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok()
    server.prompt_error = {
        "code": -32000,
        "message": "rate_limit: request rejected; try again later",
    }
    with pytest.raises(EngineRefused) as raised:
        run(line(server, project))
    assert not isinstance(raised.value, EnginePlanLimit)


def test_a_grok_rate_limit_lookalike_does_not_fail_its_role_over(
    project: tuple[Path, Path, Path],
) -> None:
    workspace, _, _ = project
    server = FakeGrok()
    server.prompt_error = {
        "code": -32000,
        "message": "rate_limit: request rejected; try again later",
    }

    class Idle:
        session_id: str | None = None

        async def run_turn(self, prompt: str) -> TurnReport:
            del prompt
            return TurnReport(
                "success", 1, "ok", (), None, None, "idle-session", None, None
            )

    def build(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        del tools
        return line(server, project) if spec.line_id == "alpha" else Idle()

    log = EventLog(workspace / ".dromon" / "grok-role.sqlite")
    scheduler = Scheduler(
        log,
        build,
        tick_seconds=0,
        wall_clock=lambda: datetime(2026, 10, 31, 9, 0, tzinfo=UTC),
        root=workspace,
    )
    scheduler.ensure_operator("ana")
    scheduler.create_line("alpha", GROK, Strategy.SUBSCRIPTION)
    scheduler.create_line("beta", "claude-haiku-5-5", Strategy.SUBSCRIPTION)
    log.append(RoleDeclared("core", ".", 3600, ("alpha", "beta")))
    log.append(SeatBound("alpha", "core", True))
    log.append(SeatBound("beta", "core", True))
    log.append(
        OwnershipChanged(
            "core",
            "alpha",
            None,
            OwnershipCause.DECLARED,
            OperatorAddress("ana"),
            None,
            format_due(datetime(2026, 10, 31, 9, 0, tzinfo=UTC)),
        )
    )

    async def turn() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(scheduler.run(stop))
        scheduler.send(OperatorAddress("ana"), LineAddress("alpha"), "try")
        await scheduler.idle()
        stop.set()
        await loop

    try:
        asyncio.run(turn())
        assert log.state.roles["core"].owner == "alpha"
        assert grok_module.ENGINE_NAME not in log.state.budgets
    finally:
        log.close()


class Tap:
    """The scripted agent, recorded as Grok's own process records."""

    def __init__(self, server: FakeGrok, recording: Recording) -> None:
        self.server = server
        self.recording = recording

    async def send(self, message: Mapping[str, object]) -> None:
        await self.server.send(message)
        self.recording.frame(OUT, message)

    async def receive(self) -> dict[str, Any] | None:
        found = await self.server.receive()
        if found is not None:
            self.recording.frame(IN, found)
        return found


def test_a_recorded_turn_replays_through_the_real_adapter(
    project: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    """A Grok probe records this exchange, and the recording then stands in for
    Grok, so the adapter gives the same report without the engine."""
    workspace, home, state = project
    path = tmp_path / "probe.jsonl"
    recording = Recording(path, grok_module.ENGINE_NAME, GROK)
    # Without a bridge Dromon passes no server, so Grok must report none.
    server = FakeGrok(
        [chunk("hel"), tool_call("other", "dromon__send_message"), chunk("lo")],
        servers=[],
    )

    @asynccontextmanager
    async def tapped(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        del arguments, cwd, env
        yield Tap(server, recording)

    def target(factory: Any) -> GrokLine:  # noqa: ANN401
        return GrokLine(
            grok(),
            workspace,
            config=LineConfig(),
            mcp_tools=[SEND],
            protected=(state,),
            environment={},
            home=home,
            connection_factory=factory,
        )

    live = run(target(tapped))
    recording.close()
    assert (live.text, live.tool_uses) == ("hello", ("dromon__send_message",))
    replay = Replay(read_recording(path), wait_seconds=1.0)
    replayed = run(target(replay_factory(replay)))
    assert replay.finished()
    assert replay.sent == replay.recorded_out
    assert (replayed.subtype, replayed.text, replayed.tool_uses) == (
        live.subtype,
        live.text,
        live.tool_uses,
    )
    assert (replayed.credential, replayed.session_id) == (
        live.credential,
        live.session_id,
    )


def test_the_session_carries_dromons_tools_instructions_and_ask_mode(
    project: tuple[Path, Path, Path],
) -> None:
    workspace, _, _ = project
    (workspace / "RULES.md").write_text("Be careful.\n")
    server = FakeGrok()
    run(line(server, project, config=LineConfig(instructions=("RULES.md",))))
    session = server.params("session/new")
    assert session["cwd"] == str(workspace)
    assert session["mcpServers"] == [
        {
            "name": "dromon",
            "command": "dromon-bridge",
            "args": ["--line", "alpha"],
            "env": [{"name": "DROMON_LINE_TOKEN", "value": "tok"}],
        }
    ]
    assert session["_meta"]["yoloMode"] is False
    assert session["_meta"]["autoMode"] is False
    assert session["_meta"]["rules"] == "Contents of RULES.md:\n\nBe careful."


def test_the_next_turn_loads_the_session(project: tuple[Path, Path, Path]) -> None:
    server = FakeGrok()
    run(
        line(
            server,
            project,
            session_id="s1:rules-" + grok_module.session_digest(LineConfig(), ""),
        )
    )
    assert server.params("session/load")["sessionId"] == "s1"
    assert "session/new" not in server.methods()


@pytest.mark.parametrize(
    ("server", "reason"),
    [
        (FakeGrok(auth="xai_api_key"), "authentication method 'xai_api_key'"),
        (FakeGrok(auth=None), "authentication method None"),
        (
            FakeGrok(model=None),
            "did not report a configOptions model option",
        ),
        (FakeGrok(modes={}), "permission mode None"),
        (
            FakeGrok(
                modes={"currentModeId": "bypassPermissions", "availableModes": []}
            ),
            "permission mode 'bypassPermissions'",
        ),
    ],
)
def test_an_engine_that_cannot_be_confirmed_is_refused_before_the_prompt(
    project: tuple[Path, Path, Path], server: FakeGrok, reason: str
) -> None:
    with pytest.raises(EngineRefused, match=reason):
        run(line(server, project))
    assert "session/prompt" not in server.methods()


@pytest.mark.parametrize(
    "session_id",
    [None, "s1:rules-" + grok_module.session_digest(LineConfig(), "")],
    ids=["new", "resumed"],
)
def test_another_reported_model_is_pinned_to_the_driver_model(
    project: tuple[Path, Path, Path], session_id: str | None
) -> None:
    server = FakeGrok(model="grok-4.6")
    target = line(server, project, session_id=session_id)
    report = run(target)
    request = server.params("session/set_config_option")
    assert request == {
        "sessionId": "s1",
        "configId": "model",
        "value": target.driver.settings.model,
    }
    opening = "session/load" if session_id else "session/new"
    assert opening in server.methods()
    assert ("session/new" if session_id else "session/load") not in server.methods()
    assert (
        server.methods().index(opening)
        < server.methods().index("session/set_config_option")
        < server.methods().index("session/prompt")
    )
    assert report.text == "done"
    assert not server.methods().count("session/cancel")


def test_grok_refusing_the_driver_model_refuses_the_turn(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(model="grok-4.6")
    server.set_config_error = "model is unavailable"
    with pytest.raises(
        EngineRefused,
        match="Grok refused the driver's model.*after reporting 'grok-4.6'",
    ):
        run(line(server, project))
    assert "session/prompt" not in server.methods()


def test_a_different_model_in_set_config_option_answer_is_refused(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(model="grok-4.6")
    server.set_config_result = {"configOptions": [model_option("grok-4.5")]}
    with pytest.raises(
        EngineRefused,
        match="refuses the engine model 'grok-4.5'",
    ):
        run(line(server, project))
    assert "session/prompt" not in server.methods()


def test_a_set_config_option_answer_with_conflicting_model_fields_is_refused(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(model="grok-4.6")
    server.set_config_result = {
        "models": {"currentModelId": GROK},
        "configOptions": [model_option("grok-4.5")],
    }
    with pytest.raises(EngineRefused, match="conflicting or unreadable session models"):
        run(line(server, project))
    assert "session/prompt" not in server.methods()


def test_a_session_with_conflicting_model_fields_is_refused_before_pinning(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(model=GROK)
    server.extra = {"configOptions": [model_option("grok-4.6")]}
    with pytest.raises(EngineRefused, match="conflicting or unreadable session models"):
        run(line(server, project))
    assert "session/set_config_option" not in server.methods()
    assert "session/prompt" not in server.methods()


def test_a_session_with_conflicting_models_refuses_before_it_would_pin(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(model="grok-4.6")
    server.extra = {"configOptions": [model_option(GROK)]}
    with pytest.raises(EngineRefused, match="conflicting or unreadable session models"):
        run(line(server, project))
    assert "session/set_config_option" not in server.methods()
    assert "session/prompt" not in server.methods()


def test_models_without_a_config_option_cannot_be_pinned(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(model="grok-4.6")
    server.extra = {"configOptions": [EFFORT_OPTION]}
    with pytest.raises(
        EngineRefused, match="did not report a configOptions model option"
    ):
        run(line(server, project))
    assert "session/set_config_option" not in server.methods()
    assert "session/prompt" not in server.methods()


def test_a_session_without_reported_modes_rests_on_the_checked_home(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok()
    server.modes = "none"
    assert run(line(server, project)).text == "done"
    assert "session/set_mode" not in server.methods()


def test_an_always_approve_session_is_switched_to_the_ask_mode(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(
        modes={
            "currentModeId": "bypassPermissions",
            "availableModes": [{"id": "default"}, {"id": "bypassPermissions"}],
        }
    )
    run(line(server, project))
    assert server.params("session/set_mode") == {"sessionId": "s1", "modeId": "default"}


def test_permission_requests_follow_the_lines_tools(
    project: tuple[Path, Path, Path],
) -> None:
    reader = LineConfig(builtin_tools=("Read",))
    server = FakeGrok(
        [
            permission("read", request_id="p1", ident="a"),
            permission("other", "dromon__send_message", request_id="p2", ident="b"),
            {"jsonrpc": "2.0", "id": "x", "method": "fs/read_text_file", "params": {}},
            chunk("done"),
        ]
    )
    report = run(line(server, project, config=reader))
    answers = {a["id"]: a for a in server.answers()}
    picked = {
        key: answers[key]["result"]["outcome"]["optionId"] for key in ("p1", "p2")
    }
    assert picked == {"p1": "yes", "p2": "yes"}
    assert answers["x"]["error"]["code"] == -32601
    assert report.tool_uses == ("Read", "dromon__send_message")
    # An ordinary rejection leaves later independent calls available.
    refused = FakeGrok(
        [
            permission("execute", request_id="p1", ident="a"),
            permission("read", request_id="p2", ident="b"),
            permission("other", "gmail__send", request_id="p3", ident="c"),
        ]
    )
    assert run(line(refused, project, config=reader)).tool_uses == ("Read",)
    assert "session/cancel" not in refused.methods()
    answers = {a["id"]: a["result"]["outcome"] for a in refused.answers()}
    assert answers["p1"]["optionId"] == "no"
    assert answers["p2"]["optionId"] == "yes"
    assert answers["p3"]["optionId"] == "no"


@pytest.mark.parametrize(
    ("kind", "name", "raw_input", "options", "intent"),
    [
        (
            "other",
            "use_tool",
            use_tool_input("dromon__send_message"),
            [{"optionId": "broad", "kind": "allow_always"}],
            "allow",
        ),
        (
            "edit",
            "search_replace",
            None,
            [{"optionId": "yes", "kind": "allow_once"}],
            "refuse",
        ),
    ],
)
def test_missing_permission_choice_cancels_and_refuses_with_evidence(
    project: tuple[Path, Path, Path],
    kind: str,
    name: str,
    raw_input: object | None,
    options: list[dict[str, str]],
    intent: str,
) -> None:
    request = permission(kind, name=name, raw_input=raw_input)
    request["params"]["options"] = options
    server = FakeGrok([request])

    with pytest.raises(EngineRefused) as raised:
        run(line(server, project, config=LineConfig(builtin_tools=("Read",))))

    message = str(raised.value)
    expected_tool = "use_tool (dromon__send_message)" if name == "use_tool" else name
    assert expected_tool in message
    assert intent in message
    assert options[0]["kind"] in message
    assert options[0]["optionId"] in message
    answers = server.answers()
    assert answers[0]["result"]["outcome"] == {"outcome": "cancelled"}
    assert server.methods().count("session/cancel") == 1


def test_missing_permission_option_preserves_an_earlier_identity_refusal(
    project: tuple[Path, Path, Path],
) -> None:
    request = permission("other", "different_name")
    request["params"]["options"] = []
    server = FakeGrok([tool_call("other", "dromon__send_message"), request])

    with pytest.raises(EngineRefused) as raised:
        run(line(server, project, config=LineConfig(builtin_tools=("Read",))))

    assert "changed the toolName" in str(raised.value)
    assert "ACP offered no option" not in str(raised.value)
    assert server.answers()[0]["result"]["outcome"] == {"outcome": "cancelled"}


def test_the_turn_is_reported_to_the_observer_as_it_streams(
    project: tuple[Path, Path, Path],
) -> None:
    thought = note(
        {
            "sessionUpdate": "agent_thought_chunk",
            "content": {"type": "text", "text": "hmm"},
        }
    )
    server = FakeGrok(
        [
            thought,
            thought,
            chunk("hello "),
            chunk("there"),
            tool_call("other", "dromon__send_message"),
        ]
    )
    log = ActivityLog()
    run(line(server, project, observe=log.observer("alpha")))
    # Streamed chunks extend one entry, and reasoning is shown only as a mark.
    assert [(entry.kind, entry.text) for entry in log.after("alpha", 0)] == [
        (ActivityKind.THINKING, ""),
        (ActivityKind.TEXT, "hello there"),
        (ActivityKind.TOOL, "dromon__send_message"),
    ]


def test_tool_calls_are_counted_and_others_cancel_the_turn(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(
        [
            tool_call("read", ident="a"),
            tool_call("other", "dromon__send_message", ident="b"),
        ]
    )
    report = run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert report.tool_uses == ("Read", "dromon__send_message")
    blocked = FakeGrok([tool_call("execute")])
    with pytest.raises(EngineRefused, match="not given: execute"):
        run(line(blocked, project, config=LineConfig(builtin_tools=("Read",))))
    assert blocked.params("session/cancel") == {"sessionId": "s1"}
    foreign = FakeGrok([tool_call("other", "gmail__send")])
    with pytest.raises(EngineRefused, match="not given: gmail__send"):
        run(line(foreign, project))


def test_search_tool_is_counted_as_discovery_only(
    project: tuple[Path, Path, Path],
) -> None:
    report = run(line(FakeGrok([tool_call("other", "search_tool")]), project))
    assert report.tool_uses == ("search_tool",)


def test_search_tool_with_a_non_other_kind_is_refused(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok([tool_call("read", "search_tool")])
    with pytest.raises(EngineRefused, match="not given: search_tool"):
        run(line(server, project))


def test_use_tool_calls_only_the_named_line_tool_and_counts_that_tool(
    project: tuple[Path, Path, Path],
) -> None:
    raw = use_tool_input("dromon__send_message")
    server = FakeGrok(
        [
            tool_call("other", "search_tool", ident="search"),
            tool_call("other", "use_tool", ident="invoke", raw_input=raw),
            permission("other", "use_tool", ident="invoke", raw_input=raw),
        ]
    )
    report = run(line(server, project))
    assert report.tool_uses == ("search_tool", "dromon__send_message")
    assert server.answers()[-1]["result"]["outcome"]["optionId"] == "yes"


@pytest.mark.parametrize(
    "raw",
    (
        {"tool_name": "other__send_message", "tool_input": {}},
        {"tool_name": "dromon__not_given", "tool_input": {}},
        {"tool_name": "dromon__send_message"},
        {
            "tool_name": "dromon__send_message",
            "tool_input": [],
        },
        {
            "tool_name": "dromon__send_message",
            "tool_input": {},
            "tool_input_file": "/outside/args.json",
        },
        {
            "tool_name": "dromon__send_message",
            "server_name": "other",
            "tool_input": {},
        },
    ),
)
def test_use_tool_refuses_unlisted_or_ambiguous_structured_targets(
    project: tuple[Path, Path, Path], raw: object
) -> None:
    server = FakeGrok([tool_call("other", "use_tool", raw_input=raw)])
    with pytest.raises(EngineRefused, match="not given: use_tool"):
        run(line(server, project))


def test_use_tool_refuses_a_title_only_target(project: tuple[Path, Path, Path]) -> None:
    event = {
        "sessionUpdate": "tool_call",
        "toolCallId": "t",
        "kind": "other",
        "title": "use_tool",
        "rawInput": use_tool_input("dromon__send_message"),
    }
    with pytest.raises(EngineRefused, match="not given: use_tool"):
        run(line(FakeGrok([note(event)]), project))


def test_use_tool_requires_an_explicit_other_kind(
    project: tuple[Path, Path, Path],
) -> None:
    event = {
        "sessionUpdate": "tool_call",
        "toolCallId": "t",
        "toolName": "use_tool",
        "rawInput": use_tool_input("dromon__send_message"),
    }
    with pytest.raises(EngineRefused, match="not given: use_tool"):
        run(line(FakeGrok([note(event)]), project))


def test_use_tool_target_cannot_change_after_it_is_counted(
    project: tuple[Path, Path, Path],
) -> None:
    original = use_tool_input("dromon__send_message")
    changed = use_tool_input("other__send_message")
    server = FakeGrok(
        [
            tool_call("other", "use_tool", raw_input=original),
            permission("other", "use_tool", raw_input=changed),
        ]
    )
    with pytest.raises(EngineRefused, match="changed the structured input"):
        run(line(server, project))


def test_use_tool_update_cannot_change_raw_input(
    project: tuple[Path, Path, Path],
) -> None:
    original = use_tool_input("dromon__send_message")
    changed = use_tool_input("dromon__read_mail")
    server = FakeGrok(
        [
            tool_call("other", "use_tool", raw_input=original),
            call_update("t", rawInput=changed),
        ]
    )
    with pytest.raises(EngineRefused, match="changed the structured input"):
        run(line(server, project))


def test_reused_tool_call_identifier_is_refused_before_a_second_grant(
    project: tuple[Path, Path, Path],
) -> None:
    raw = use_tool_input("dromon__send_message")
    server = FakeGrok(
        [
            tool_call("other", "use_tool", ident="reused", raw_input=raw),
            permission(
                "other", "use_tool", request_id="first", ident="reused", raw_input=raw
            ),
            tool_call("other", "use_tool", ident="reused", raw_input=raw),
            permission(
                "other", "use_tool", request_id="second", ident="reused", raw_input=raw
            ),
        ]
    )
    with pytest.raises(EngineRefused, match="reused a tool call identifier"):
        run(line(server, project))
    answers = server.answers()
    assert answers[0]["result"]["outcome"]["optionId"] == "yes"
    assert answers[1]["result"]["outcome"]["outcome"] == "cancelled"
    assert server.methods().count("session/cancel") == 1


def test_repeated_permission_request_for_a_granted_call_is_refused(
    project: tuple[Path, Path, Path],
) -> None:
    raw = use_tool_input("dromon__send_message")
    server = FakeGrok(
        [
            tool_call("other", "use_tool", ident="same", raw_input=raw),
            permission(
                "other", "use_tool", request_id="first", ident="same", raw_input=raw
            ),
            permission(
                "other", "use_tool", request_id="again", ident="same", raw_input=raw
            ),
        ]
    )
    with pytest.raises(EngineRefused, match="reused a tool call identifier"):
        run(line(server, project))
    answers = server.answers()
    assert answers[0]["result"]["outcome"]["optionId"] == "yes"
    assert answers[1]["result"]["outcome"]["optionId"] == "no"


def test_a_title_never_names_one_of_dromons_tools(
    project: tuple[Path, Path, Path],
) -> None:
    """Antigravity review, F4, applied to Grok through the shared client. A call that
    names Dromon's tool only in its title is judged by its kind, and refused."""
    titled = {
        "toolCallId": "t",
        "kind": "other",
        "title": "dromon__send_message",
    }
    asked = permission("other")
    asked["params"]["toolCall"] = titled
    server = FakeGrok([asked, chunk("done")])
    assert run(line(server, project)).text == "done"
    assert "session/cancel" not in server.methods()
    answers = {a["id"]: a for a in server.answers()}
    assert answers["p1"]["result"]["outcome"]["optionId"] == "no"
    used = FakeGrok([note({"sessionUpdate": "tool_call", **titled})])
    with pytest.raises(EngineRefused, match="not given: dromon__send_message"):
        run(line(used, project))


def test_no_kind_is_exempt_from_the_tool_check(
    project: tuple[Path, Path, Path],
) -> None:
    """Antigravity review, F1, applied to Grok on 2026-10-26. A ``think`` call was
    once let through uncounted, and is now judged as any other kind, so refused."""
    server = FakeGrok([tool_call("think")])
    with pytest.raises(EngineRefused, match="not given: think"):
        run(line(server, project))
    asked = FakeGrok([permission("think")])
    assert run(line(asked, project)).tool_uses == ()
    assert "session/cancel" not in asked.methods()
    assert asked.answers()[0]["result"]["outcome"]["optionId"] == "no"


@pytest.mark.parametrize(
    ("first", "later", "field"),
    [
        (tool_call("read"), call_update("t", kind="execute"), "kind"),
        (
            tool_call("other", "dromon__send_message"),
            call_update("t", toolName="gmail__send"),
            "toolName",
        ),
        (tool_call("read"), call_update("t", title="anything"), "title"),
        (tool_call("read"), tool_call("execute"), "kind"),
        # Re-review, N3. A field the first report lacked is a change too, so a
        # Grok that adds the kind only in a later update is refused.
        (
            call_update("t", toolName="dromon__send_message"),
            call_update("t", kind="other"),
            "kind",
        ),
    ],
)
def test_a_tool_call_whose_identity_changes_refuses_the_turn(
    project: tuple[Path, Path, Path],
    first: dict[str, Any],
    later: dict[str, Any],
    field: str,
) -> None:
    """Antigravity review, F2 and F3, applied to Grok. A call is judged when first
    seen, so a later update that renames it, retitles it or changes its kind refuses
    the turn, even from a permitted call to another permitted one."""
    server = FakeGrok([first, later, chunk("done")])
    config = LineConfig(builtin_tools=("Read", "Bash"))
    with pytest.raises(EngineRefused, match=f"changed the {field} of its tool call"):
        run(line(server, project, config=config))
    assert server.params("session/cancel") == {"sessionId": "s1"}


def test_a_permission_request_that_changes_a_call_is_rejected(
    project: tuple[Path, Path, Path],
) -> None:
    """A request naming one of Dromon's tools for a call first seen as a read is not
    the call that was checked, so it is rejected and the turn refused."""
    server = FakeGrok(
        [
            tool_call("read"),
            permission("other", "dromon__send_message"),
        ]
    )
    with pytest.raises(EngineRefused, match="changed the toolName of its tool call"):
        run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert server.answers()[0]["result"]["outcome"]["optionId"] == "no"


def test_a_tool_call_without_an_identifier_refuses_the_turn(
    project: tuple[Path, Path, Path],
) -> None:
    for update in (call_update(None, kind="read"), call_update("", kind="read")):
        server = FakeGrok([update])
        with pytest.raises(EngineRefused, match="without an identifier"):
            run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    asked = permission("read")
    del asked["params"]["toolCall"]["toolCallId"]
    server = FakeGrok([asked])
    with pytest.raises(EngineRefused, match="without an identifier"):
        run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert server.answers()[0]["result"]["outcome"]["optionId"] == "no"


def test_a_call_is_judged_and_counted_once_when_first_seen(
    project: tuple[Path, Path, Path],
) -> None:
    """Updates that repeat or leave out the identity fields are the call already
    judged, whether it was first seen as a request or as a notification."""
    server = FakeGrok(
        [
            permission("read", ident="a"),
            tool_call("read", ident="a"),
            call_update("a", status="completed"),
            tool_call("other", "dromon__send_message", ident="b"),
            call_update("b", toolName="dromon__send_message", status="completed"),
            chunk("done"),
        ]
    )
    report = run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert report.tool_uses == ("Read", "dromon__send_message")
    assert "session/cancel" not in server.methods()


def test_tool_call_permission_and_updates_are_counted_once_in_acp_order(
    project: tuple[Path, Path, Path],
) -> None:
    raw = use_tool_input("dromon__send_message")
    server = FakeGrok(
        [
            tool_call("other", "use_tool", ident="call", raw_input=raw),
            permission("other", "use_tool", ident="call", raw_input=raw),
            call_update("call", status="in_progress"),
            call_update("call", status="completed"),
            chunk("done"),
        ]
    )
    report = run(line(server, project))
    assert report.tool_uses == ("dromon__send_message",)
    assert server.answers()[0]["result"]["outcome"]["optionId"] == "yes"
    assert "session/cancel" not in server.methods()


def test_an_update_seen_first_is_judged_by_its_own_fields(
    project: tuple[Path, Path, Path],
) -> None:
    """An update for a call never announced is judged as a new call, and one with no
    kind is of the kind ``other``, which only Dromon's tools may be."""
    server = FakeGrok([call_update("a", kind="execute")])
    with pytest.raises(EngineRefused, match="not given: execute"):
        run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    bare = FakeGrok([call_update("a", status="in_progress")])
    with pytest.raises(EngineRefused, match="not given: an unnamed tool"):
        run(line(bare, project))


def test_the_driver_budget_cancels_a_turn_once(
    project: tuple[Path, Path, Path],
) -> None:
    budget = grok().settings.tool_budget
    server = FakeGrok([tool_call("read", ident=f"t{i}") for i in range(budget + 3)])
    report = run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert report.interrupted_for == "tool_budget"
    assert report.subtype == "cancelled"
    assert server.methods().count("session/cancel") == 1


def test_use_tool_target_counts_toward_the_driver_tool_budget(
    project: tuple[Path, Path, Path],
) -> None:
    budget = grok().settings.tool_budget
    raw = use_tool_input("dromon__send_message")
    server = FakeGrok(
        [
            tool_call("other", "use_tool", ident=f"t{i}", raw_input=raw)
            for i in range(budget + 2)
        ]
    )
    report = run(line(server, project))
    assert report.interrupted_for == "tool_budget"
    assert report.tool_uses
    assert set(report.tool_uses) == {"dromon__send_message"}


def test_a_cancel_grok_ignores_ends_the_turn_and_stops_grok(
    project: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-review, N2. A cancel bounds the wait for Grok's answer, after which the
    connection is left, which stops Grok. An interrupt stays an interrupt, and a
    refusal keeps its own reason."""
    monkeypatch.setattr(grok_module, "ABORT_SECONDS", 0.05)
    budget = grok().settings.tool_budget
    server = FakeGrok([tool_call("read", ident=f"t{i}") for i in range(budget + 1)])
    server.answer_prompt = False
    server.ignores_cancel = True
    report = run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert report.interrupted_for == "tool_budget"
    assert report.subtype == "cancelled"
    assert server.methods().count("session/cancel") == 1
    assert server.stops == 1
    refused = FakeGrok([tool_call("execute")])
    refused.answer_prompt = False
    refused.ignores_cancel = True
    with pytest.raises(EngineRefused, match="not given: execute"):
        run(line(refused, project, config=LineConfig(builtin_tools=("Read",))))
    assert refused.stops == 1


def test_a_closed_connection_refuses_the_turn(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok([chunk("partial")])
    server.answer_prompt = False
    with pytest.raises(EngineRefused, match="closed its connection"):
        run(line(server, project))


def test_grok_start_and_turn_deadlines_are_bounded(
    project: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The bound is half a second rather than a hundredth, since the fake finishes the
    # stages before the stalled one at once but a loaded host may not run them within
    # ten milliseconds, and the error would then name an earlier stage. Found failing
    # under load on 2026-10-29.
    monkeypatch.setattr(grok_module, "START_SECONDS", 0.5)
    stalled_start = FakeGrok()
    stalled_start.blocked_method = "session/new"
    with pytest.raises(EngineRefused, match="start timed out during open session"):
        run(line(stalled_start, project))
    assert stalled_start.stops == 1

    stalled_initialize = FakeGrok()
    stalled_initialize.blocked_method = "initialize"
    with pytest.raises(EngineRefused, match="start timed out during initialize"):
        run(line(stalled_initialize, project))
    assert stalled_initialize.stops == 1

    monkeypatch.setattr(grok_module, "TURN_SECONDS", 0.1)
    stalled_turn = FakeGrok()
    stalled_turn.answer_prompt = False
    stalled_turn.ignores_cancel = True
    with pytest.raises(
        EngineRefused, match="Grok turn had no activity for 0.1 seconds"
    ):
        run(line(stalled_turn, project))
    assert stalled_turn.stops == 1


def test_grok_sliding_deadline_resets_on_activity_and_obeys_absolute_cap(
    project: tuple[Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The active turn's twenty chunks take about 0.4 seconds in all, longer than the
    # 0.3 second deadline, so it ends only if each chunk resets the deadline. Each
    # gap of 0.02 seconds is a fifteenth of it, so a loaded host has room. The capped
    # turn takes about 0.5 seconds against an absolute cap of 0.3, with a sliding
    # deadline of 1 second that no gap comes near, so only the cap can end it.
    monkeypatch.setattr(grok_module, "TURN_SECONDS", 0.3)
    monkeypatch.setattr(grok_module, "ABSOLUTE_TURN_SECONDS", 5.0)
    active = FakeGrok([chunk(str(index % 10)) for index in range(20)])
    active.activity_delay = 0.02
    report = run(line(active, project))
    assert report.text == "0123456789" * 2

    monkeypatch.setattr(grok_module, "TURN_SECONDS", 1.0)
    monkeypatch.setattr(grok_module, "ABSOLUTE_TURN_SECONDS", 0.3)
    capped = FakeGrok([chunk(str(index)) for index in range(10)])
    capped.activity_delay = 0.05
    with pytest.raises(EngineRefused, match="absolute cap of 0.3 seconds"):
        run(line(capped, project))


def test_the_grok_adapter_refuses_a_driver_for_another_engine(
    project: tuple[Path, Path, Path],
) -> None:
    claude = Driver.for_model("claude-haiku-5-5", Strategy.SUBSCRIPTION)
    with pytest.raises(EngineRefused, match="written for the claude-code engine"):
        line(FakeGrok(), project, driver=claude)


# The factory, the line socket and the bridge.


def test_the_factory_builds_a_grok_line_with_its_bridge_and_token(
    tmp_path: Path,
) -> None:
    tokens = LineTokens()
    socket_path = tmp_path / "run" / "lines.sock"
    build = engine_factory(str(tmp_path), line_tokens=tokens, line_socket=socket_path)
    engine: LineEngine = build(LineSpec("alpha", GROK, Strategy.SUBSCRIPTION), [SEND])
    assert isinstance(engine, GrokLine)
    assert engine.line_token == tokens.issue("alpha")
    assert list(engine.bridge) == bridge_command(tmp_path, socket_path.parent, "alpha")
    assert engine.own_tools == frozenset({"dromon__send_message"})


def test_line_tokens_are_per_line_and_checked(tmp_path: Path) -> None:
    tokens = LineTokens()
    alpha = tokens.issue("alpha")
    assert tokens.issue("alpha") == alpha != tokens.issue("beta")
    assert tokens.check("alpha", alpha) == "alpha"
    for line_id, token in (("alpha", "wrong"), ("beta", alpha), ("gamma", alpha)):
        with pytest.raises(AgentError, match="needs its token"):
            tokens.check(line_id, token)
    with pytest.raises(AgentError):
        tokens.issue("Bad Name")


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError("no engine may start in this test")


def test_the_line_socket_serves_only_the_lines_own_tools(
    tmp_path: Path, short_runtime: Path
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700)
    with EventLog(paths.event_log) as log:
        log.append(LineCreated("alpha", GROK, "subscription"))
        log.append(OperatorRegistered("ana"))
        daemon = Daemon(paths, log, "t", Scheduler(log, no_engine, hold_wakes=True))
        token = daemon.line_tokens.issue("alpha")

        def ask(op: str, args: dict[str, object]) -> Response:
            line = (
                json.dumps({"id": "r", "op": op, "v": 1, "args": args}) + "\n"
            ).encode()
            return asyncio.run(daemon.respond_line(line))

        listed = ask("agent_tools", {"line": "alpha", "token": token})
        assert listed.result is not None
        names = {t["name"] for t in cast(list[dict[str, str]], listed.result["tools"])}
        assert names == {
            "send_message",
            "ask_operator",
            "wake_after",
            "acquire_lease",
            "release_lease",
            "line_status",
            "read_instructions",
            "write_scratch",
            "check_disclosure",
            "propose_change",
            "list_processes",
            "stop_process",
            "list_mail",
            "read_mail",
            "acknowledge_mail",
            "list_sent",
            "set_brief",
        }
        sent = ask(
            "agent_call",
            {
                "line": "alpha",
                "token": token,
                "tool": "send_message",
                "arguments": {"to": "@ana", "body": "hi"},
            },
        )
        assert sent.ok and sent.result is not None and not sent.result["is_error"]
        message = list(log.state.messages.values())[-1]
        assert message.sender == LineAddress("alpha")
        assert not ask("agent_tools", {"line": "alpha", "token": "bad"}).ok
        assert not ask("agent_tools", {"agent": "alpha", "token": token}).ok
        refused = ask("status", {}).error_message
        assert refused is not None and refused.startswith("the line socket serves only")
        orphan = daemon.line_tokens.issue("ghost")
        assert ask("agent_tools", {"line": "ghost", "token": orphan}).error_message == (
            "no line ghost"
        )


def test_the_bridge_names_a_line_in_line_mode(short_runtime: Path) -> None:
    bridge = Bridge(short_runtime / "lines.sock", "alpha", "tok", party="line")
    answer = bridge.handle({"id": 1, "method": "initialize", "params": {}})
    assert answer is not None
    instructions = cast(dict[str, Any], answer["result"])["instructions"]
    assert "session line alpha" in instructions
    listed = bridge.handle({"id": 2, "method": "tools/list"})
    assert listed is not None and "not running" in str(listed["error"])


def test_a_grok_line_carries_its_process_mark(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok([chunk("hello")])
    target = line(server, project)
    target.mark = "alpha.token"
    run(target)
    assert server.environment["DROMON_LINE_MARK"] == "alpha.token"


# The safe point of priority mail, decided by the operator on 2026-10-27. A turn ends
# once a tool result is delivered and not at the text before the tool use.

type Steered = Callable[[str, Strategy], tuple[SteeredDriver, list[int]]]
REASON = "priority mail m-1 from @ana"


def test_priority_mail_ends_the_turn_at_the_tool_result_and_not_the_text(
    project: tuple[Path, Path, Path], steered: Steered
) -> None:
    driver, asked = steered(GROK, Strategy.SUBSCRIPTION)
    server = FakeGrok(
        [
            chunk("I will read it"),
            tool_call("read", ident="t1"),
            call_update("t1", status="in_progress"),
            call_update("t1", status="completed"),
            call_update("t1", status="completed"),
            chunk("done"),
        ]
    )
    config = LineConfig(builtin_tools=("Read",))
    report = run(line(server, project, config=config, driver=driver))
    # A second update that the call completed is not a second result.
    assert asked == [1]
    assert report.interrupted_for == REASON
    assert server.methods().count("session/cancel") == 1


# The probe of 2026-10-27, Grok Build 1.0.70. Every value is invented, in the shapes
# the probe recorded.

EFFORT_OPTION: Final = {
    "id": "reasoning_effort",
    "name": "Reasoning Effort",
    "category": "thought_level",
    "type": "select",
    "currentValue": "high",
    "options": [{"value": "high", "name": "High"}],
}


def probed_grok(turn: Sequence[dict[str, Any]] = (chunk("done"),)) -> FakeGrok:
    """A Grok that answers ``session/new`` as 1.0.70 did, with ``models`` and
    ``configOptions`` for the model and effort, and no ``modes``. Its own session
    config lists efforts under the category ``mode``, which is not a permission
    mode."""
    server = FakeGrok(turn, modes=ABSENT)
    server.extra = {
        "configOptions": [
            {
                "id": "model",
                "category": "model",
                "type": "select",
                "currentValue": GROK,
                "options": [{"value": GROK, "name": "Grok"}],
            },
            EFFORT_OPTION,
        ],
        "_meta": {
            "x.ai/sessionConfig": {
                "options": [{"id": "default", "category": "mode", "selected": True}]
            }
        },
    }
    return server


def mode_option(value: str) -> dict[str, object]:
    return {
        "id": "permission_mode",
        "category": "mode",
        "type": "select",
        "currentValue": value,
        "options": [{"value": "default"}, {"value": value}],
    }


def test_a_session_shaped_as_grok_1_0_70_runs_on_dromons_grok_home(
    project: tuple[Path, Path, Path],
) -> None:
    _, home, _ = project
    server = probed_grok()
    assert run(line(server, project)).text == "done"
    assert server.environment["GROK_HOME"] == str(home / HOME_DIR)
    assert (home / HOME_DIR / "config.toml").read_text() == HOME_CONFIG
    assert "session/set_mode" not in server.methods()


def test_the_operators_own_grok_home_is_never_read(
    project: tuple[Path, Path, Path],
) -> None:
    _, home, _ = project
    (home / ".grok" / "config.toml").write_text("[ui]\nyolo = true\n")
    server = probed_grok()
    assert run(line(server, project)).text == "done"
    assert server.environment["GROK_HOME"] != str(home / ".grok")


def test_a_drifted_home_refuses_the_turn_before_grok_starts(
    project: tuple[Path, Path, Path],
) -> None:
    _, home, _ = project
    run(line(probed_grok(), project))
    (home / HOME_DIR / "config.toml").write_text(
        '[ui]\npermission_mode = "always-approve"\n'
    )
    server = probed_grok()
    with pytest.raises(EngineRefused, match="not the configuration Dromon wrote"):
        run(line(server, project))
    assert server.methods() == []


def test_grok_runs_with_its_own_home_directory_not_the_operators(
    project: tuple[Path, Path, Path],
) -> None:
    _, home, _ = project
    settings(home, {"permissions": {"defaultMode": "auto"}})
    environment = {
        "PATH": "/bin",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
    }
    server = probed_grok()
    assert run(line(server, project, environment=environment)).text == "done"
    assert server.environment["HOME"] == str(home / HOME_DIR / USER_HOME)
    assert "XDG_CONFIG_HOME" not in server.environment


def test_a_claude_settings_mode_in_grok_home_directory_refuses_the_turn(
    project: tuple[Path, Path, Path],
) -> None:
    _, home, _ = project
    run(line(probed_grok(), project))
    settings(home / HOME_DIR / USER_HOME, {"permissions": {"defaultMode": "auto"}})
    server = probed_grok()
    with pytest.raises(EngineRefused, match="defaultMode to 'auto'"):
        run(line(server, project))
    assert server.methods() == []


def test_a_project_claude_directory_refuses_before_grok_starts(
    project: tuple[Path, Path, Path],
) -> None:
    workspace, _, _ = project
    settings(workspace, {"permissions": {"defaultMode": "bypassPermissions"}})
    server = probed_grok()
    with pytest.raises(EngineRefused, match="project configuration or instructions"):
        run(line(server, project))
    assert server.methods() == []


@pytest.mark.parametrize("level", ["workspace", "repository"])
@pytest.mark.parametrize("name", [".grok", ".claude", ".cursor", ".agents"])
def test_a_projects_grok_or_harness_directory_is_refused(
    tmp_path: Path, level: str, name: str
) -> None:
    repository = tmp_path / "repo"
    workspace = repository / "packages" / "one"
    workspace.mkdir(parents=True)
    (repository / ".git").mkdir()
    directory = (workspace if level == "workspace" else repository) / name
    check_project(workspace)
    directory.mkdir()
    check_project(workspace)
    (directory / "rules").write_text("unread instructions")
    with pytest.raises(EngineRefused, match=re.escape(str(directory))):
        check_project(workspace)
    shutil.rmtree(directory)
    directory.write_text("")
    with pytest.raises(EngineRefused, match="has not read"):
        check_project(workspace)


def test_a_grok_directory_outside_the_project_is_not_read(tmp_path: Path) -> None:
    repository = tmp_path / "outer" / "repo"
    (repository / ".git").mkdir(parents=True)
    (tmp_path / "outer" / ".grok").mkdir()
    (tmp_path / "outer" / ".grok" / "sandbox.toml").write_text("")
    check_project(repository)
    workspace = tmp_path / "outer" / "work"
    workspace.mkdir()
    check_project(workspace)


@pytest.mark.parametrize("name", [".grok", ".claude", ".cursor", ".agents"])
def test_a_project_harness_directory_refuses_the_turn_before_grok_starts(
    project: tuple[Path, Path, Path], name: str
) -> None:
    workspace, _, _ = project
    directory = workspace / name
    directory.mkdir()
    (directory / "rules").write_text("unread instructions")
    server = probed_grok()
    with pytest.raises(EngineRefused, match="project configuration"):
        run(line(server, project))


def test_claude_cc_writes_marker_is_the_only_nonempty_project_claude_entry_allowed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "work"
    claude = workspace / ".claude"
    marker = claude / ".cc-writes"
    marker.mkdir(parents=True)
    check_project(workspace)

    (marker / "record").write_text("")
    with pytest.raises(EngineRefused, match="project configuration"):
        check_project(workspace)
    (marker / "record").unlink()
    marker.rmdir()

    target = tmp_path / "target"
    target.mkdir()
    marker.symlink_to(target)
    with pytest.raises(EngineRefused, match="project configuration"):
        check_project(workspace)
    marker.unlink()
    marker.mkdir()

    (claude / "settings.json").write_text("{}")
    with pytest.raises(EngineRefused, match="project configuration"):
        check_project(workspace)


def test_a_lone_non_marker_claude_directory_is_refused(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    (workspace / ".claude" / "skills").mkdir(parents=True)
    with pytest.raises(EngineRefused, match="project configuration"):
        check_project(workspace)


def test_cc_writes_marker_must_be_a_real_directory(tmp_path: Path) -> None:
    workspace = tmp_path / "work"
    marker = workspace / ".claude" / ".cc-writes"
    marker.parent.mkdir(parents=True)
    marker.write_text("")
    with pytest.raises(EngineRefused, match="project configuration"):
        check_project(workspace)


def test_another_harness_in_grok_home_directory_refuses_the_turn(
    project: tuple[Path, Path, Path],
) -> None:
    _, home, _ = project
    run(line(probed_grok(), project))
    (home / HOME_DIR / USER_HOME / ".claude.json").write_text("{}")
    server = probed_grok()
    with pytest.raises(EngineRefused, match=r"holds \.claude\.json,"):
        run(line(server, project))
    assert server.methods() == []


def test_a_grok_that_is_not_signed_in_names_the_sign_in_command(
    project: tuple[Path, Path, Path],
) -> None:
    with pytest.raises(EngineRefused) as refused:
        run(line(FakeGrok(auth=None), project))
    assert SIGN_IN in str(refused.value)


def test_a_permission_mode_config_option_is_read_and_confirmed(
    project: tuple[Path, Path, Path],
) -> None:
    server = probed_grok()
    server.extra["configOptions"] = [
        model_option(GROK),
        EFFORT_OPTION,
        mode_option("default"),
    ]
    assert run(line(server, project)).text == "done"
    refused = probed_grok()
    refused.extra["configOptions"] = [
        model_option(GROK),
        mode_option("bypassPermissions"),
    ]
    with pytest.raises(EngineRefused, match="permission mode 'bypassPermissions'"):
        run(line(refused, project))
    assert "session/prompt" not in refused.methods()


INVENTED_SERVER: Final = {
    "name": "garden-notes",
    "source": "local",
    "type": "http",
    "url": "https://notes.example.invalid/mcp",
}


@pytest.mark.parametrize(
    ("servers", "reason"),
    [
        pytest.param(
            [*DROMON_ONLY, INVENTED_SERVER], "loaded 1 MCP servers", id="one-more"
        ),
        pytest.param([INVENTED_SERVER] * 3, "loaded 3 MCP servers", id="others"),
        pytest.param([{"type": "stdio"}], "loaded 1 MCP servers", id="unnamed"),
        pytest.param("all", "shape Dromon does not read", id="malformed"),
        pytest.param(None, "did not report its MCP servers", id="unreported"),
    ],
)
def test_servers_other_than_dromons_refuse_the_turn(
    project: tuple[Path, Path, Path], servers: object, reason: str
) -> None:
    server = FakeGrok(servers=servers)
    with pytest.raises(EngineRefused, match=reason) as refused:
        run(line(server, project))
    # The operator's servers are counted, never named.
    assert "garden-notes" not in str(refused.value)
    assert "session/prompt" not in server.methods()


def test_a_server_list_during_the_turn_cancels_it(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(
        [
            grok_notice("mcp/servers_updated", {"mcpServers": [INVENTED_SERVER]}),
            chunk("late"),
        ]
    )
    with pytest.raises(EngineRefused, match="besides Dromon's"):
        run(line(server, project))
    assert "session/cancel" in server.methods()


def test_without_a_bridge_even_a_server_named_dromon_is_refused(
    project: tuple[Path, Path, Path],
) -> None:
    workspace, home, state = project
    server = FakeGrok()

    @asynccontextmanager
    async def connect(
        arguments: list[str], cwd: Path, env: Mapping[str, str]
    ) -> AsyncGenerator[Connection]:
        del arguments, cwd, env
        yield server

    target = GrokLine(
        grok(),
        workspace,
        protected=(state,),
        environment={},
        home=home,
        connection_factory=connect,
    )
    with pytest.raises(EngineRefused, match="loaded 1 MCP servers"):
        run(target)


@pytest.mark.parametrize(
    ("notice", "reason"),
    [
        pytest.param(
            grok_notice("mcp/init_progress", {"total": 12, "connected": 0}),
            "connecting 12 MCP servers",
            id="progress",
        ),
        pytest.param(
            grok_notice("mcp/init_progress", {"total": "many"}),
            "connecting 'many' MCP servers",
            id="progress-malformed",
        ),
        pytest.param(
            grok_notice("models/update", {"currentModelId": "grok-other"}),
            "model to 'grok-other'",
            id="models",
        ),
        pytest.param(
            grok_notice(
                "session_notification",
                {
                    "sessionId": "s1",
                    "update": {"sessionUpdate": "model_changed", "model_id": None},
                },
            ),
            "model to None",
            id="model-changed",
        ),
        pytest.param(
            grok_notice("settings/update", {"permission_mode": "always-approve"}),
            "settings name the permission mode 'always-approve'",
            id="settings",
        ),
    ],
)
def test_groks_own_notices_refuse_what_dromon_did_not_choose(
    project: tuple[Path, Path, Path], notice: dict[str, Any], reason: str
) -> None:
    server = FakeGrok(notices=[notice])
    with pytest.raises(EngineRefused, match=reason):
        run(line(server, project))
    assert "session/prompt" not in server.methods()


def test_groks_identified_notices_that_agree_let_the_turn_run(
    project: tuple[Path, Path, Path],
) -> None:
    notices: list[dict[str, Any]] = [
        grok_notice("session/setup", {"method": "session/new", "phase": "auth"}),
        grok_notice("announcements/update", {"gen": 1, "announcements": []}),
        grok_notice("mcp/init_progress", {"total": 1, "connected": 0}),
        grok_notice("models/update", {"currentModelId": GROK}),
        grok_notice("models/update", {"availableModels": []}),
        grok_notice("settings/update", {"permission_mode": None, "tips": ["a tip"]}),
        grok_notice(
            "session_notification",
            {"update": {"sessionUpdate": "model_changed", "model_id": GROK}},
        ),
        grok_notice("session_notification", {"update": {"sessionUpdate": "retry"}}),
        # The guide's spelling, without ACP's extension prefix, is read alike.
        {"jsonrpc": "2.0", "method": "x.ai/models/update", "params": {}},
        {"jsonrpc": "2.0", "method": "_x.ai/unknown/notice", "params": {}},
    ]
    server = FakeGrok(notices=notices)
    assert run(line(server, project)).text == "done"


def test_a_model_notice_after_the_model_is_pinned_refuses_the_turn(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(model="grok-4.6")
    server.after_set_config_notices = [
        grok_notice("models/update", {"currentModelId": "grok-other"})
    ]
    with pytest.raises(EngineRefused, match="model to 'grok-other'"):
        run(line(server, project))
    assert "session/prompt" in server.methods()
    assert "session/cancel" in server.methods()


def test_a_held_model_notice_is_superseded_by_a_successful_pin(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(
        model="grok-4.6",
        notices=[grok_notice("models/update", {"currentModelId": "grok-other"})],
    )
    assert run(line(server, project)).text == "done"
    assert server.params("session/set_config_option")["value"] == GROK
    assert "session/cancel" not in server.methods()


def test_an_unknown_grok_request_is_answered_with_an_error(
    project: tuple[Path, Path, Path],
) -> None:
    request = {"jsonrpc": "2.0", "id": "q", "method": "_x.ai/fs/write_file"}
    server = FakeGrok([request, chunk("done")])
    assert run(line(server, project)).text == "done"
    answer = next(a for a in server.answers() if a["id"] == "q")
    assert answer["error"]["code"] == -32601


def test_a_switch_out_of_the_ask_modes_during_the_turn_cancels_it(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(
        [
            note({"sessionUpdate": "current_mode_update", "currentModeId": "default"}),
            note(
                {
                    "sessionUpdate": "current_mode_update",
                    "currentModeId": "bypassPermissions",
                }
            ),
            chunk("late"),
        ]
    )
    with pytest.raises(EngineRefused, match="switched to the permission mode"):
        run(line(server, project))
    assert "session/cancel" in server.methods()


def test_extension_methods_are_read_with_or_without_the_prefix() -> None:
    assert grok_module.extension_method("_x.ai/models/update") == (
        grok_module.MODELS_UPDATE
    )
    assert grok_module.extension_method("x.ai/models/update") == (
        grok_module.MODELS_UPDATE
    )
    assert grok_module.extension_method("session/update") == "session/update"


GIT_SSH = GitSshAccess(
    "/run/agent.sock", "/keys/known_hosts", "/keys/deploy.pub", "0" * 64, "0" * 64
)
"""A resolved ``[git_ssh]`` agent, which no test here connects to."""


def test_a_grok_line_given_git_ssh_carries_its_command_and_no_other(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok([chunk("hello")])
    target = line(server, project)
    target.git_ssh = GIT_SSH
    run(target)
    assert server.environment[COMMAND_VARIABLE] == ssh_command(GIT_SSH)
    plain = FakeGrok([chunk("hello")])
    run(line(plain, project, environment={"PATH": "/bin", COMMAND_VARIABLE: "x"}))
    assert COMMAND_VARIABLE not in plain.environment


# The tool call shape Grok sends, as its session log of 2026-10-31 recorded it. The
# first tool_call has no kind and no toolName, a title equal to the tool's name and
# the tool in _meta["x.ai/tool"], and a later tool_call_update adds the kind,
# rewrites the title and adds a variant to rawInput.


def grok_meta(name: str, namespace: str = "grok_build") -> dict[str, object]:
    return {"x.ai/tool": {"version": 1, "name": name, "namespace": namespace}}


def grok_call(
    name: str, ident: str = "t", raw_input: object | None = None
) -> dict[str, Any]:
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call",
        "toolCallId": ident,
        "title": name,
        "_meta": grok_meta(name),
    }
    if raw_input is not None:
        update["rawInput"] = raw_input
    return note(update)


def grok_permission(ident: str = "t", **fields: object) -> dict[str, Any]:
    request = permission("other", ident=ident)
    tool_call = cast(dict[str, Any], request["params"]["toolCall"])
    del tool_call["kind"], tool_call["toolName"]
    tool_call.update(fields)
    return request


def test_the_observed_search_tool_sequence_is_counted_as_discovery(
    project: tuple[Path, Path, Path],
) -> None:
    query = {"query": "dromon list_mail read_mail send_message", "limit": 10}
    server = FakeGrok(
        [
            grok_call("search_tool", ident="c0", raw_input=query),
            call_update(
                "c0",
                kind="other",
                title='Search tools: "dromon list_mail read_mail send_message"',
                rawInput={"variant": "SearchTool", **query},
                _meta=grok_meta("search_tool"),
            ),
        ]
    )
    assert run(line(server, project)).tool_uses == ("search_tool",)


def test_a_grok_built_in_is_counted_as_the_line_tool_that_permits_it(
    project: tuple[Path, Path, Path],
) -> None:
    given = LineConfig(builtin_tools=("Read",))
    server = FakeGrok([grok_call("read_file"), call_update("t", kind="read")])
    assert run(line(server, project, config=given)).tool_uses == ("Read",)


@pytest.mark.parametrize("name", ["read_file", "spawn_subagent", "ask_user_question"])
def test_a_grok_tool_the_line_was_not_given_is_refused(
    project: tuple[Path, Path, Path], name: str
) -> None:
    given = LineConfig(builtin_tools=("Bash",))
    with pytest.raises(EngineRefused, match=f"not given: {name}"):
        run(line(FakeGrok([grok_call(name)]), project, config=given))


def test_another_namespace_is_not_taken_as_a_grok_tool(
    project: tuple[Path, Path, Path],
) -> None:
    event = grok_call("search_tool")
    event["params"]["update"]["_meta"] = grok_meta("search_tool", "other")
    with pytest.raises(EngineRefused, match="not given: search_tool"):
        run(line(FakeGrok([event]), project))


def test_a_grok_use_tool_runs_only_the_line_tool_it_names(
    project: tuple[Path, Path, Path],
) -> None:
    raw = use_tool_input("dromon__send_message")
    server = FakeGrok(
        [
            grok_call("use_tool", ident="u", raw_input=raw),
            call_update("u", kind="other", rawInput={"variant": "UseTool", **raw}),
            grok_permission(ident="u"),
        ]
    )
    report = run(line(server, project))
    assert report.tool_uses == ("dromon__send_message",)
    assert server.answers()[-1]["result"]["outcome"]["optionId"] == "yes"


@pytest.mark.parametrize(
    "raw",
    (
        {"tool_name": "other__send_message", "tool_input": {}},
        {"tool_name": "dromon__send_message", "tool_input_file": "/outside.json"},
        {"file": "/outside.json"},
        {"tool_name": "dromon__send_message", "tool_input": {}, "variant": 1},
        {
            "tool_name": "dromon__send_message",
            "tool_input": {},
            "variant": "FileBacked",
        },
        {"tool_name": "dromon__send_message", "tool_input": {}, "server": "x"},
    ),
)
def test_a_grok_use_tool_refuses_any_other_target_or_form(
    project: tuple[Path, Path, Path], raw: object
) -> None:
    with pytest.raises(EngineRefused, match="not given: use_tool"):
        run(line(FakeGrok([grok_call("use_tool", raw_input=raw)]), project))


def test_a_grok_use_tool_target_cannot_change_in_an_update(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(
        [
            grok_call("use_tool", raw_input=use_tool_input("dromon__send_message")),
            call_update("t", rawInput=use_tool_input("other__send_message")),
        ]
    )
    with pytest.raises(EngineRefused, match="changed the structured input"):
        run(line(server, project))


@pytest.mark.parametrize(
    "fields",
    ({"_meta": grok_meta("run_terminal_command")}, {"toolName": "search_tool"}),
)
def test_a_grok_call_cannot_change_its_name_in_an_update(
    project: tuple[Path, Path, Path], fields: dict[str, object]
) -> None:
    server = FakeGrok([grok_call("search_tool"), call_update("t", **fields)])
    with pytest.raises(EngineRefused, match="changed the name"):
        run(line(server, project))


def test_a_permission_request_is_judged_by_the_recorded_grok_name(
    project: tuple[Path, Path, Path],
) -> None:
    given = LineConfig(builtin_tools=("Read",))
    granted = FakeGrok([grok_call("read_file"), grok_permission()])
    run(line(granted, project, config=given))
    assert granted.answers()[-1]["result"]["outcome"]["optionId"] == "yes"


def test_a_grok_kind_that_disagrees_with_its_name_is_refused_at_first_sight(
    project: tuple[Path, Path, Path],
) -> None:
    # Review G1. A read-only line must not run a call whose kind says execute, even
    # when its _meta says read_file.
    event = grok_call("read_file")
    event["params"]["update"]["kind"] = "execute"
    given = LineConfig(builtin_tools=("Read",))
    with pytest.raises(EngineRefused, match="not given: read_file"):
        run(line(FakeGrok([event]), project, config=given))


@pytest.mark.parametrize(
    ("name", "kind"), [("read_file", "execute"), ("search_tool", "read")]
)
def test_a_grok_kind_added_later_must_agree_with_its_name(
    project: tuple[Path, Path, Path], name: str, kind: str
) -> None:
    given = LineConfig(builtin_tools=("Read",))
    server = FakeGrok([grok_call(name), call_update("t", kind=kind)])
    with pytest.raises(EngineRefused, match="does not agree"):
        run(line(server, project, config=given))


def test_a_grok_name_added_in_a_later_update_is_refused(
    project: tuple[Path, Path, Path],
) -> None:
    # Review G2. A call first judged by its kind cannot be renamed by a later _meta.
    given = LineConfig(builtin_tools=("Read",))
    server = FakeGrok(
        [
            tool_call("read", ident="t"),
            call_update("t", _meta=grok_meta("run_terminal_command")),
        ]
    )
    with pytest.raises(EngineRefused, match="in a later update"):
        run(line(server, project, config=given))


@pytest.mark.parametrize(
    ("name", "kind", "given"),
    [
        # Review of d29174d, PA. A kind naming another of the line's tools still
        # disagrees with the call's own name.
        ("read_file", "execute", ("Read", "Bash")),
        # PB to PD. A built-in with a kind not in its row of GROK_TOOL_KINDS is
        # refused, which fails closed until Grok's real kinds are recorded.
        ("run_terminal_command", "other", ("Bash",)),
        ("web_search", "read", ("WebSearch",)),
    ],
)
def test_a_grok_kind_must_be_one_its_own_name_allows(
    project: tuple[Path, Path, Path], name: str, kind: str, given: tuple[str, ...]
) -> None:
    event = grok_call(name)
    event["params"]["update"]["kind"] = kind
    config = LineConfig(builtin_tools=given)
    with pytest.raises(EngineRefused, match=f"not given: {name}"):
        run(line(FakeGrok([event]), project, config=config))


@pytest.mark.parametrize(
    ("name", "kind", "given", "counted"),
    [
        ("list_dir", "search", ("Glob",), "Glob"),
        ("web_search", "search", ("WebSearch",), "WebSearch"),
        ("run_terminal_command", "execute", ("Bash",), "Bash"),
    ],
)
def test_a_grok_kind_its_name_allows_is_accepted(
    project: tuple[Path, Path, Path],
    name: str,
    kind: str,
    given: tuple[str, ...],
    counted: str,
) -> None:
    event = grok_call(name)
    event["params"]["update"]["kind"] = kind
    config = LineConfig(builtin_tools=given)
    assert run(line(FakeGrok([event]), project, config=config)).tool_uses == (counted,)


@pytest.mark.parametrize("kind", [["read"], {"kind": "read"}, 3])
def test_a_grok_kind_that_is_not_text_is_refused(
    project: tuple[Path, Path, Path], kind: object
) -> None:
    # Review G-A. A non-string kind once raised TypeError before Dromon answered.
    event = grok_call("read_file")
    event["params"]["update"]["kind"] = kind
    config = LineConfig(builtin_tools=("Read",))
    with pytest.raises(EngineRefused, match="not given: read_file"):
        run(line(FakeGrok([event]), project, config=config))


def test_a_permission_request_whose_kind_disagrees_is_refused(
    project: tuple[Path, Path, Path],
) -> None:
    # Review G-B. The permission path judges the kind against the recorded name too.
    config = LineConfig(builtin_tools=("Read", "Bash"))
    server = FakeGrok([grok_call("read_file"), grok_permission(kind="execute")])
    with pytest.raises(EngineRefused, match="does not agree"):
        run(line(server, project, config=config))


def test_the_observed_list_dir_sequence_is_counted_as_its_line_tool(
    project: tuple[Path, Path, Path],
) -> None:
    # Observed on 2026-10-31. Grok's tool_call_update gives list_dir the kind other,
    # which held a live turn before list_dir allowed it.
    target = {"target_directory": "/workspace/tmp"}
    server = FakeGrok(
        [
            grok_call("list_dir", ident="d", raw_input=target),
            call_update(
                "d",
                kind="other",
                title="List `tmp`",
                rawInput={"variant": "ListDir", **target},
                _meta=grok_meta("list_dir"),
            ),
        ]
    )
    config = LineConfig(builtin_tools=("Read",))
    assert run(line(server, project, config=config)).tool_uses == ("Read",)


def test_a_file_backed_use_tool_is_refused_with_advice_to_pass_it_inline(
    project: tuple[Path, Path, Path],
) -> None:
    # Observed on 2026-10-31. Grok used the documented file-backed form once, and the
    # refusal named only use_tool, so the line could not tell what to change.
    raw = {"tool_name": "dromon__send_message", "tool_input_file": "/scratch/args.json"}
    server = FakeGrok([grok_call("use_tool", raw_input=raw)])
    with pytest.raises(
        EngineRefused, match="not given: use_tool.*inline as tool_input"
    ):
        run(line(server, project))


# Round 11. The ViewImage capability. Grok's read_file is the only tool that shows a
# local image, so ViewImage permits it, narrowed to images. The rawInput key
# target_file is an inference from the tool's definition, not an observed call.

PNG_BYTES = png()
VIEW = LineConfig(builtin_tools=("ViewImage",))


def read_input(path: str) -> dict[str, object]:
    return {"target_file": path}


def test_view_image_lets_a_line_view_a_small_image_and_counts_it(
    project: tuple[Path, Path, Path],
) -> None:
    (project[0] / "shot.png").write_bytes(PNG_BYTES)
    server = FakeGrok(
        [
            grok_call("read_file", raw_input=read_input("shot.png")),
            call_update("t", kind="read"),
            grok_permission(rawInput=read_input("shot.png")),
        ]
    )
    assert run(line(server, project, config=VIEW)).tool_uses == ("ViewImage",)
    assert server.answers()[-1]["result"]["outcome"]["optionId"] == "yes"


@pytest.mark.parametrize(
    ("name", "content", "reason"),
    [
        ("notes.txt", b"text", "by its name"),
        ("fake.png", b"text, not an image", "does not begin"),
        ("big.png", png(5 * 1024 * 1024 + 1), "over the limit"),
        ("../outside.png", b"", "does not exist|outside"),
    ],
)
def test_view_image_refuses_other_files_and_oversize_images(
    project: tuple[Path, Path, Path], name: str, content: bytes, reason: str
) -> None:
    if not name.startswith(".."):
        (project[0] / name).write_bytes(content)
    server = FakeGrok([grok_call("read_file", raw_input=read_input(name))])
    with pytest.raises(EngineRefused, match=f"not given: .*ViewImage.*{reason}"):
        run(line(server, project, config=VIEW))
    assert server.params("session/cancel") == {"sessionId": "s1"}


def test_view_image_refuses_a_first_notice_that_carries_no_input(
    project: tuple[Path, Path, Path],
) -> None:
    # Review of 1045994, I2. Grok serves text through read_file, so a call whose input
    # is not yet known could finish before a later update names the file.
    (project[0] / "shot.png").write_bytes(PNG_BYTES)
    server = FakeGrok(
        [
            grok_call("read_file"),
            call_update("t", kind="read", rawInput=read_input("shot.png")),
        ]
    )
    with pytest.raises(EngineRefused, match="ViewImage|names no file"):
        run(line(server, project, config=VIEW))
    assert server.params("session/cancel") == {"sessionId": "s1"}


def test_a_view_only_line_proceeds_when_the_first_notice_carries_the_input(
    project: tuple[Path, Path, Path],
) -> None:
    # Review of d00da44, T9. The path the fail-closed rule leaves open: the input on
    # the first notice, then updates that do not repeat it.
    (project[0] / "shot.png").write_bytes(PNG_BYTES)
    server = FakeGrok(
        [
            grok_call("read_file", raw_input=read_input("shot.png")),
            call_update("t", kind="read"),
            call_update("t", status="completed"),
        ]
    )
    report = run(line(server, project, config=VIEW))
    assert report.tool_uses == ("ViewImage",)
    assert "session/cancel" not in server.methods()


def test_a_line_with_read_may_start_a_call_before_its_input_is_known(
    project: tuple[Path, Path, Path],
) -> None:
    both = LineConfig(builtin_tools=("Read", "ViewImage"))
    server = FakeGrok([grok_call("read_file"), call_update("t", kind="read")])
    assert run(line(server, project, config=both)).tool_uses == ("Read",)


def test_view_image_stops_a_later_update_that_names_another_file(
    project: tuple[Path, Path, Path],
) -> None:
    (project[0] / "shot.png").write_bytes(PNG_BYTES)
    (project[0] / "notes.txt").write_bytes(b"text")
    server = FakeGrok(
        [
            grok_call("read_file", raw_input=read_input("shot.png")),
            call_update("t", kind="read", rawInput=read_input("notes.txt")),
        ]
    )
    with pytest.raises(EngineRefused, match="ViewImage.*by its name"):
        run(line(server, project, config=VIEW))


def test_view_image_denies_a_permission_request_that_names_no_image(
    project: tuple[Path, Path, Path],
) -> None:
    (project[0] / "notes.txt").write_bytes(b"text")
    (project[0] / "shot.png").write_bytes(PNG_BYTES)
    first = grok_call("read_file", raw_input=read_input("shot.png"))
    requests = (grok_permission(), grok_permission(rawInput=read_input("notes.txt")))
    for request in requests:
        server = FakeGrok([first, request])
        run(line(server, project, config=VIEW))
        assert server.answers()[-1]["result"]["outcome"]["optionId"] == "no"


def test_a_line_with_read_is_not_narrowed_by_view_image(
    project: tuple[Path, Path, Path],
) -> None:
    (project[0] / "notes.txt").write_bytes(b"text")
    both = LineConfig(builtin_tools=("Read", "ViewImage"))
    server = FakeGrok([grok_call("read_file", raw_input=read_input("notes.txt"))])
    assert run(line(server, project, config=both)).tool_uses == ("Read",)


@pytest.mark.parametrize(
    "tools", [(), ("Bash",), ("Edit", "Write"), ("Glob", "Grep", "WebFetch")]
)
def test_a_line_without_view_image_or_read_cannot_view_an_image(
    project: tuple[Path, Path, Path], tools: tuple[str, ...]
) -> None:
    (project[0] / "shot.png").write_bytes(PNG_BYTES)
    server = FakeGrok([grok_call("read_file", raw_input=read_input("shot.png"))])
    with pytest.raises(EngineRefused, match="not given: read_file"):
        run(line(server, project, config=LineConfig(builtin_tools=tools)))


@pytest.mark.parametrize(
    "name", ["image_gen", "image_edit", "image_to_video", "reference_to_video"]
)
def test_view_image_never_permits_image_generation_or_editing(
    project: tuple[Path, Path, Path], name: str
) -> None:
    both = LineConfig(builtin_tools=("Read", "ViewImage"))
    for config in (VIEW, both):
        server = FakeGrok([grok_call(name)])
        with pytest.raises(EngineRefused, match=f"not given: {name}"):
            run(line(server, project, config=config))


def test_the_observed_task_output_sequence_is_counted_as_bash(
    project: tuple[Path, Path, Path],
) -> None:
    # Observed on 2026-10-31. Grok's tool_call_update gives
    # get_command_or_subagent_output the kind other, which held a live turn.
    raw = {"task_ids": ["t1"], "timeout_ms": 180000}
    server = FakeGrok(
        [
            grok_call("get_command_or_subagent_output", ident="o", raw_input=raw),
            call_update(
                "o",
                kind="other",
                title="Get task output",
                rawInput={"variant": "TaskOutput", **raw},
                _meta=grok_meta("get_command_or_subagent_output"),
            ),
        ]
    )
    config = LineConfig(builtin_tools=("Bash",))
    assert run(line(server, project, config=config)).tool_uses == ("Bash",)


def test_a_task_tool_of_kind_other_still_needs_bash(
    project: tuple[Path, Path, Path],
) -> None:
    event = grok_call("kill_command_or_subagent")
    event["params"]["update"]["kind"] = "other"
    config = LineConfig(builtin_tools=("Read",))
    with pytest.raises(EngineRefused, match="not given: kill_command_or_subagent"):
        run(line(FakeGrok([event]), project, config=config))


@pytest.mark.parametrize(
    "status", ["pending", "failed", "in_progress", "completed", "unknown", None]
)
def test_rejected_call_cannot_run_after_permission(
    project: tuple[Path, Path, Path],
    status: str | None,
) -> None:
    denied = permission("execute")
    fields = {} if status is None else {"status": status}
    server = FakeGrok([denied, call_update("t", **fields)])
    with pytest.raises(
        EngineRefused, match="did not confirm that a rejected tool call did not run"
    ):
        run(line(server, project))
    assert server.methods().count("session/cancel") == 1


@pytest.mark.parametrize("status", ["pending", "failed", None])
def test_pending_call_can_be_rejected_and_another_call_can_succeed(
    project: tuple[Path, Path, Path],
    status: str | None,
) -> None:
    announcement = tool_call("execute", ident="blocked")
    announcement["params"]["update"]["status"] = "pending"
    server = FakeGrok(
        [
            announcement,
            permission("execute", ident="blocked", request_id="deny"),
            call_update("blocked", status=status, _meta={"toolNotRun": True}),
            permission(
                "other", "dromon__send_message", ident="good", request_id="allow"
            ),
            chunk("corrected"),
        ]
    )
    report = run(line(server, project))
    assert report.text == "corrected"
    assert report.tool_uses == ("dromon__send_message",)
    assert "session/cancel" not in server.methods()


def test_a_rejected_permission_identifier_cannot_be_reused(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(
        [permission("execute"), permission("execute", request_id="again")]
    )
    with pytest.raises(EngineRefused, match="reused a tool call identifier"):
        run(line(server, project))


def test_unapproved_pending_call_starting_without_permission_stops(
    project: tuple[Path, Path, Path],
) -> None:
    first = tool_call("execute")
    first["params"]["update"]["status"] = "pending"
    server = FakeGrok([first, call_update("t", status="in_progress")])
    with pytest.raises(EngineRefused, match="started a tool the line was not given"):
        run(line(server, project))


@pytest.mark.parametrize("status", ["in_progress", "completed", "failed"])
def test_permission_for_an_already_started_call_stops(
    project: tuple[Path, Path, Path], status: str
) -> None:
    request = permission("execute")
    request["params"]["toolCall"]["status"] = status
    server = FakeGrok([request])
    with pytest.raises(EngineRefused, match="no longer pending"):
        run(line(server, project))
    assert "session/cancel" in server.methods()


def test_driver_budget_does_not_grant_the_request_that_stops_it(
    project: tuple[Path, Path, Path],
) -> None:
    budget = grok().settings.tool_budget
    server = FakeGrok(
        [
            permission("read", ident=f"t{i}", request_id=f"p{i}")
            for i in range(budget + 1)
        ]
    )
    report = run(line(server, project, config=LineConfig(builtin_tools=("Read",))))
    assert report.interrupted_for == "tool_budget"
    assert report.tool_uses
    at_stop = server.answers()[len(report.tool_uses) - 1]
    assert at_stop["result"]["outcome"] == {"outcome": "cancelled"}


@pytest.mark.parametrize("rpc_error", [False, True])
@pytest.mark.parametrize("budget_stop", [False, True])
def test_cancel_transport_failure_keeps_the_original_stop_cause(
    project: tuple[Path, Path, Path], rpc_error: bool, budget_stop: bool
) -> None:
    class BreaksOnCancel(FakeGrok):
        async def send(self, message: Mapping[str, object]) -> None:
            if message.get("method") == "session/cancel":
                self.sent.append(dict(message))
                self.queue.clear()
                if rpc_error:
                    self.queue.append(
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

    budget = grok().settings.tool_budget
    events = (
        [
            permission("read", ident=f"t{i}", request_id=f"p{i}")
            for i in range(budget + 1)
        ]
        if budget_stop
        else [tool_call("read"), call_update("t", kind="execute")]
    )
    server = BreaksOnCancel(events)
    target = line(server, project, config=LineConfig(builtin_tools=("Read",)))
    if budget_stop:
        report = run(target)
        assert report.interrupted_for == "tool_budget"
    else:
        with pytest.raises(EngineRefused, match="changed the kind"):
            run(target)


def test_non_execution_evidence_is_required_again_on_each_update(
    project: tuple[Path, Path, Path],
) -> None:
    server = FakeGrok(
        [
            permission("execute"),
            call_update("t", status="failed", _meta={"toolNotRun": True}),
            call_update("t", status="failed"),
        ]
    )
    with pytest.raises(
        EngineRefused, match="did not confirm that a rejected tool call did not run"
    ):
        run(line(server, project))
    assert server.methods().count("session/cancel") == 1


@pytest.mark.parametrize("stored", ["s1", "s1:rules-incorrect"])
def test_unknown_or_changed_rules_replace_a_grok_session(
    project: tuple[Path, Path, Path], stored: str
) -> None:
    server = FakeGrok()
    target = line(server, project, session_id=stored)
    report = run(target)
    assert "session/load" not in server.methods()
    assert "session/new" in server.methods()
    assert "Dromon replaced your Grok session" in str(server.params("session/prompt"))
    assert report.session_id == "s1:rules-" + grok_module.session_digest(
        LineConfig(), ""
    )


def test_grok_changed_rules_file_starts_a_new_session(
    project: tuple[Path, Path, Path],
) -> None:
    workspace, _, _ = project
    rules = workspace / "RULES.md"
    rules.write_text("Old rules")
    config = LineConfig(instructions=("RULES.md",))
    initial = line(FakeGrok(), project, config=config)
    stored = run(initial).session_id
    rules.write_text("New rules")
    server = FakeGrok()
    run(line(server, project, config=config, session_id=stored))
    assert "session/load" not in server.methods()
    assert "New rules" in str(server.params("session/new"))


@pytest.mark.parametrize("options", [[], [{"value": "grok-4.6", "name": "old"}]])
def test_requested_model_must_be_listed(
    project: tuple[Path, Path, Path], options: list[dict[str, str]]
) -> None:
    server = FakeGrok(model="grok-4.6")
    option = model_option("grok-4.6")
    option["options"] = options
    server.extra["configOptions"] = [option]
    target = line(
        server,
        project,
        session_id="s1:rules-" + grok_module.session_digest(LineConfig(), ""),
    )
    with pytest.raises(
        EngineRefused, match="does not list requested model.*listed values"
    ):
        run(target)
    assert "session/load" in server.methods()
    assert "session/set_config_option" not in server.methods()
    assert "session/prompt" not in server.methods()


@pytest.mark.parametrize("identifier", ["x" * 185, "x" * 186, "é" * 93])
def test_session_identifier_digest_bound(
    project: tuple[Path, Path, Path], identifier: str
) -> None:
    class LongSession(FakeGrok):
        async def send(self, message: Mapping[str, object]) -> None:
            await super().send(message)
            if message.get("method") == "session/new":
                self.queue[-1]["result"]["sessionId"] = identifier

    server = LongSession()
    if len(identifier.encode("utf-8")) <= 185:
        assert len((run(line(server, project)).session_id or "").encode("utf-8")) == 256
    else:
        with pytest.raises(EngineRefused, match="identifier plus rules digest exceeds"):
            run(line(server, project))
        assert "session/prompt" not in server.methods()


@pytest.mark.parametrize("version", [1, 2])
def test_initialize_records_supported_protocol_version(
    project: tuple[Path, Path, Path], version: int
) -> None:
    server = FakeGrok()
    server.protocol_version = version
    target = line(server, project)
    if version == 1:
        run(target)
    else:
        with pytest.raises(EngineRefused, match="ACP version 2"):
            run(target)
        assert "session/new" not in server.methods()
    assert target.protocol_version == version


@pytest.mark.parametrize("fetch", [False, True])
def test_launch_controls_advertised_tools_per_line(
    project: tuple[Path, Path, Path], fetch: bool
) -> None:
    server = FakeGrok()
    config = LineConfig(builtin_tools=("WebFetch",) if fetch else ())
    inherited = {"GROK_SUBAGENTS": "1", "GROK_WEB_FETCH": "1" if not fetch else "0"}
    run(line(server, project, config=config, environment=inherited))
    assert server.environment["GROK_SUBAGENTS"] == "0"
    assert server.environment["GROK_WEB_FETCH"] == ("1" if fetch else "0")


def test_advertised_tool_change_replaces_resumed_session(
    project: tuple[Path, Path, Path],
) -> None:
    old_config = LineConfig(builtin_tools=("WebFetch",))
    stored = run(line(FakeGrok(), project, config=old_config)).session_id
    server = FakeGrok()
    run(line(server, project, session_id=stored))
    assert "session/new" in server.methods()
    assert "session/load" not in server.methods()
    assert server.environment["GROK_WEB_FETCH"] == "0"


@pytest.mark.parametrize("name", ["todo_write", "spawn_subagent", "web_fetch"])
@pytest.mark.parametrize("not_run", [True, False])
def test_unavailable_advertised_tool_is_denied_individually(
    project: tuple[Path, Path, Path], name: str, not_run: bool
) -> None:
    blocked = grok_permission(_meta=grok_meta(name))
    server = FakeGrok(
        [
            blocked,
            call_update(
                "t", status="failed", _meta={"toolNotRun": True} if not_run else {}
            ),
            permission(
                "other", "dromon__send_message", ident="good", request_id="allow"
            ),
            chunk("continued"),
        ]
    )
    if not_run:
        report = run(line(server, project))
        assert report.text == "continued"
        assert report.tool_uses == ("dromon__send_message",)
        assert "session/cancel" not in server.methods()
    else:
        with pytest.raises(
            EngineRefused, match="did not confirm that a rejected tool call did not run"
        ):
            run(line(server, project))
        assert "session/cancel" in server.methods()
    rejected = next(
        answer for answer in server.answers() if answer["id"] == blocked["id"]
    )
    expected = next(
        option["optionId"]
        for option in blocked["params"]["options"]
        if option["kind"] == "reject_once"
    )
    assert rejected["result"]["outcome"]["optionId"] == expected
