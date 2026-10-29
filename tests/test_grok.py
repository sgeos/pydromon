"""The Grok Build adapter, driven by a scripted Agent Client Protocol server.

No test here reaches Grok, whose usage was exhausted when the adapter was written. The
script follows Grok Build 1.0.70's documentation and its ``initialize`` answer.
"""

import asyncio
import dataclasses
import json
import re
import shutil
import tomllib
from collections import deque
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import grok as grok_module
from dromon.activity import ActivityKind, ActivityLog, Observer, ignore
from dromon.agents import AgentError, LineTokens
from dromon.daemon import Daemon
from dromon.driver import Credential, Driver, Strategy
from dromon.priority import SteeredDriver
from dromon.engine import EngineRefused, TurnReport
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
from dromon.mcp import Bridge
from dromon.protocol import Response
from dromon.model import LineAddress, LineCreated, OperatorRegistered
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
    git = json.dumps(str((workspace / ".git").resolve()))
    read_only = f"read_only = [{git}]\n" if writer_git else ""
    (home / HOME_DIR / "sandbox.toml").write_text(
        f'[profiles.{READER_PROFILE}]\nextends = "read-only"\ndeny = [".dromon"]\n'
        f'[profiles.{WRITER_PROFILE}]\nextends = "workspace"\n{read_only}'
        'deny = [".dromon"]\n'
    )


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace = (tmp_path / "work").resolve()
    (workspace / ".dromon").mkdir(parents=True)
    (workspace / ".git").mkdir()
    home = tmp_path / "home"
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


def tool_call(kind: str, name: str = "", ident: str = "t") -> dict[str, Any]:
    return note(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": ident,
            "kind": kind,
            "toolName": name,
        }
    )


def call_update(ident: object, **fields: object) -> dict[str, Any]:
    """A ``tool_call_update`` for ``ident`` carrying only ``fields``."""
    return note({"sessionUpdate": "tool_call_update", "toolCallId": ident, **fields})


def permission(
    kind: str, name: str = "", request_id: str = "p1", ident: str = "t"
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "s1",
            "toolCall": {"toolCallId": ident, "kind": kind, "toolName": name},
            "options": [
                {"optionId": "yes", "kind": "allow_once", "name": "Allow"},
                {"optionId": "always", "kind": "allow_always", "name": "Always"},
                {"optionId": "no", "kind": "reject_once", "name": "Reject"},
            ],
        },
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
        self.extra: dict[str, object] = {}
        """More fields of the session's answer."""
        self.auth = auth
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
                        "protocolVersion": 1,
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
            self.queue.append({**reply, "result": result})
        elif method == "session/set_mode":
            self.queue.append({**reply, "result": {}})
        elif method == "session/prompt":
            self.prompt_id = sent["id"]
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
    prepare_home(grok_home, workspace, (workspace / ".dromon",))
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
            LineConfig(builtin_tools=tools), workspace, (state,), home, grok_home
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
    (entry / "invented.md").write_text("")
    with pytest.raises(EngineRefused, match=f"holds {name}"):
        check_home(grok_home, tmp_path / "etc")
    (entry / "invented.md").unlink()
    entry.rmdir()
    entry.write_text("")
    with pytest.raises(EngineRefused, match=f"holds {name}"):
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
        LineConfig(builtin_tools=("Bash",)), workspace, (state,), home, grok_home
    )
    writer = check_profile(
        LineConfig(builtin_tools=("Edit",)), workspace, (state,), home, grok_home
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
    with pytest.raises(EngineRefused) as raised:
        check_profile(
            LineConfig(), workspace, (workspace / ".dromon",), home, home / HOME_DIR
        )
    message = str(raised.value)
    assert f"no Grok sandbox profile {READER_PROFILE}" in message
    assert str(home / HOME_DIR / "sandbox.toml") in message
    assert f"[profiles.{READER_PROFILE}]" in message
    assert 'extends = "read-only"' in message and '".dromon"' in message


def write_reader(home: Path, table: str) -> None:
    (home / HOME_DIR).mkdir(parents=True, exist_ok=True)
    (home / HOME_DIR / "sandbox.toml").write_text(
        f"[profiles.{READER_PROFILE}]\n{table}\n"
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
    with pytest.raises(EngineRefused, match=reason):
        check_profile(
            LineConfig(), workspace, (workspace / ".dromon",), home, home / HOME_DIR
        )


def test_a_writer_must_keep_git_read_only(tmp_path: Path) -> None:
    workspace = (tmp_path / "work").resolve()
    (workspace / ".dromon").mkdir(parents=True)
    (workspace / ".git").mkdir()
    home = tmp_path / "home"
    write_profiles(home, workspace, writer_git=False)
    with pytest.raises(EngineRefused, match="leaves .*\\.git writable"):
        check_profile(
            LineConfig(builtin_tools=("Edit",)),
            workspace,
            (workspace / ".dromon",),
            home,
            home / HOME_DIR,
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
        check_profile(LineConfig(), workspace, protected, home, home / HOME_DIR)
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
            LineConfig(), workspace, (workspace / ".dromon",), home, home / HOME_DIR
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
        "s1",
    )
    assert report.credential is Credential.SUBSCRIPTION
    assert target.session_id == "s1"
    assert "DAEMON_ONLY_SENTINEL" not in server.environment
    assert server.environment["GROK_SANDBOX"] == READER_PROFILE
    init = server.params("initialize")
    assert init["clientCapabilities"] == {
        "fs": {"readTextFile": False, "writeTextFile": False},
        "terminal": False,
    }


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
    run(line(server, project, session_id="s1"))
    assert server.params("session/load")["sessionId"] == "s1"
    assert "session/new" not in server.methods()


@pytest.mark.parametrize(
    ("server", "reason"),
    [
        (FakeGrok(auth="xai_api_key"), "authentication method 'xai_api_key'"),
        (FakeGrok(auth=None), "authentication method None"),
        (FakeGrok(model="grok-4.5"), "refuses the engine model 'grok-4.5'"),
        (FakeGrok(model=None), "refuses the engine model None"),
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
    # A refused request cancels the turn, and ACP requires every request after a
    # cancel to be answered as cancelled, which grants nothing.
    refused = FakeGrok(
        [
            permission("execute", request_id="p1", ident="a"),
            permission("read", request_id="p2", ident="b"),
            permission("other", "gmail__send", request_id="p3", ident="c"),
        ]
    )
    with pytest.raises(EngineRefused, match="not given: execute"):
        run(line(refused, project, config=reader))
    answers = {a["id"]: a["result"]["outcome"] for a in refused.answers()}
    assert answers["p1"]["optionId"] == "no"
    assert answers["p2"] == answers["p3"] == {"outcome": "cancelled"}


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
    with pytest.raises(EngineRefused, match="not given: dromon__send_message"):
        run(line(server, project))
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
    with pytest.raises(EngineRefused, match="not given: think"):
        run(line(asked, project))
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
    monkeypatch.setattr(grok_module, "START_SECONDS", 0.01)
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
    monkeypatch.setattr(grok_module, "TURN_SECONDS", 0.1)
    monkeypatch.setattr(grok_module, "ABSOLUTE_TURN_SECONDS", 0.5)
    active = FakeGrok([chunk(str(index)) for index in range(6)])
    active.activity_delay = 0.02
    report = run(line(active, project))
    assert report.text == "012345"

    monkeypatch.setattr(grok_module, "ABSOLUTE_TURN_SECONDS", 0.15)
    capped = FakeGrok([chunk(str(index)) for index in range(10)])
    capped.activity_delay = 0.02
    with pytest.raises(EngineRefused, match="absolute cap of 0.15 seconds"):
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
    assert server.methods() == []


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
    server.extra["configOptions"] = [EFFORT_OPTION, mode_option("default")]
    assert run(line(server, project)).text == "done"
    refused = probed_grok()
    refused.extra["configOptions"] = [mode_option("bypassPermissions")]
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
