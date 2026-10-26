"""The Codex engine adapter, which runs session line turns through ``codex app-server``.

Dromon drives Codex from outside, as it drives Claude Code. Each turn starts
``codex app-server`` over standard input and output, speaks its JSON-RPC protocol, one
JSON object per line, starts or resumes the line's thread, and feeds every item the
engine reports to the line's Keleusma driver as an event. ``docs/protocol/CODEX.md``
records the protocol facts this module relies on.

Credential policy. Codex signs in by itself, and Dromon never reads, stores or forwards
its login. The account Codex reports, a ChatGPT plan or an API key, is mapped to a
credential and handed to the driver, which accepts or refuses it before any thread
starts. For a subscription driver, Codex is started without the variables that could
make it use an API key, which Dromon can do because it starts the process itself, and
the reported account must still be a ChatGPT plan. A variable that redirects the
provider, and a model provider other than OpenAI, are always refused.

Isolation. Codex loads the operator's user configuration, which on the operator's
machine declares mail, document and calendar MCP servers and approval by a reviewer
model. Every MCP server Codex lists is disabled by name, connector and agent features
are disabled, approvals are never asked, and the shell runs only for a line given
``Bash``. After the thread starts, every MCP server must report itself disabled, no app
may be listed, and the thread must report Dromon's permission profile as active, or
the turn is refused. During the turn, an item of a kind the line was not given
interrupts it. Dromon's own tools, such as ``send_message``, reach the model as Codex
dynamic tools, which Codex calls back through the same connection.

Filesystem. Each thread runs under ``dromon-line``, a Codex permission profile Dromon
defines on the command line. It extends Codex's ``:workspace`` profile, makes the
workspace writable only for a line given ``Edit`` or ``Write``, keeps ``.git`` readable
but not writable, and denies reading and writing Dromon's state and runtime
directories, with network access off. Codex's own read-only sandbox would let a line
read the event log, and its writable sandbox would let a line write it, so neither is
used. A deny is enforced by the operating system and a narrower deny wins over a wider
write, which ``docs/protocol/CODEX.md`` records as verified.

Limits of the adapter. Codex lines take no MCP servers, no sandbox or settings
documents and no built-in tools but ``Bash``, ``Edit`` and ``Write``, and their
configurations are refused otherwise. Codex reports no cost, and has no turn limit, so
the driver's tool budget is the bound. Dynamic tools are an experimental part of the
protocol, and the app-server is documented as experimental.

Porting note. The protocol is JSON lines over a pipe, which the Rust port can speak
unchanged. The adapter is sequential: it reads one message at a time and answers each
server request before reading on.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, cast

from claude_agent_sdk import SdkMcpTool

from dromon import __version__
from dromon.activity import ActivityKind, Observer, ignore
from dromon.driver import ActionTag, Credential, Driver, Event, Strategy
from dromon.engine import EngineRefused, TurnReport, read_instructions
from dromon.lineconfig import DEFAULT_CONFIG, LineConfig

ENGINE_NAME: Final = "codex"
"""The engine this adapter runs, as drivers name it."""

ACCOUNT_CREDENTIALS: Final[dict[str, Credential]] = {
    "chatgpt": Credential.SUBSCRIPTION,
    "apiKey": Credential.API,
}
"""Each account type Codex reports, as a credential. Any other type is refused."""

API_ENVIRONMENT: Final = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")
"""Variables that may make Codex use an API key or another login instead of the plan."""

PROVIDER_ENVIRONMENT: Final = ("OPENAI_BASE_URL",)
"""Variables that may send the engine's requests elsewhere. Always refused."""

DISABLED_FEATURES: Final = (
    "apps",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "goals",
    "hooks",
    "image_generation",
    "in_app_browser",
    "memories",
    "multi_agent",
    "plugins",
    "remote_plugin",
    "skill_mcp_dependency_install",
    "skill_search",
    "sleep_tool",
    "tool_suggest",
    "view_image",
)
"""Codex features that bring tools or connectors a line was not given."""

SHELL_FEATURES: Final = ("shell_tool", "unified_exec")
"""The features behind Codex's shell, enabled only for a line given ``Bash``."""

SUPPORTED_BUILTINS: Final = frozenset({"Bash", "Edit", "Write"})
"""Built-in tool names a Codex line may be given, in Dromon's vocabulary."""

WRITE_TOOLS: Final = frozenset({"Edit", "Write"})
"""Tools that make the workspace writable, through Codex's file changes."""

PROFILE_NAME: Final = "dromon-line"
"""The Codex permission profile every Dromon thread runs under."""

QUIET_ITEMS: Final = frozenset(
    {"userMessage", "agentMessage", "reasoning", "plan", "contextCompaction"}
)
"""Item kinds that are not tool use."""

BARE_KEY: Final = re.compile(r"[A-Za-z0-9_-]{1,128}")
"""A server name Codex's ``-c`` parser accepts as a bare key."""

MAX_LINE: Final = 64 * 1024 * 1024
"""The longest protocol line Dromon reads, so a runaway engine cannot exhaust memory."""


def unsupported(config: LineConfig) -> str | None:
    """Why a Codex line cannot run ``config``, or ``None`` if it can."""
    extra = sorted(set(config.builtin_tools) - SUPPORTED_BUILTINS)
    if extra:
        return (
            "Codex lines support only the built-in tools Bash, Edit and Write, not "
            + (", ".join(extra))
        )
    if config.mcp_servers or config.inherit_account_mcp:
        return "Codex lines do not yet support MCP servers"
    if config.claude_code_prompt:
        return "Codex lines run on Codex's own instructions, not Claude Code's prompt"
    if config.project_settings:
        return "Codex lines do not load Claude Code's project settings"
    if config.sandbox is not None or config.settings is not None:
        return (
            "Codex lines run under Dromon's Codex permission profile, and take no"
            " sandbox or settings document"
        )
    return None


def engine_environment(
    environment: Mapping[str, str], strategy: Strategy
) -> dict[str, str]:
    """The environment Codex starts with for ``strategy``, or ``EngineRefused`` when
    ``environment`` redirects the provider. A subscription line's Codex never sees the
    variables that could select an API key."""
    present = [name for name in PROVIDER_ENVIRONMENT if environment.get(name)]
    if present:
        raise EngineRefused(
            f"the environment conflicts with the {strategy} driver: "
            + ", ".join(present)
            + ". Unset these variables."
        )
    if strategy is not Strategy.SUBSCRIPTION:
        return dict(environment)
    return {k: v for k, v in environment.items() if k not in API_ENVIRONMENT}


def _toml_string(text: str) -> str:
    """``text`` as a TOML basic string. JSON's escapes are a subset of TOML's."""
    if any(ord(character) < 0x20 for character in text):
        raise EngineRefused(f"the path {text!r} holds a control character")
    return json.dumps(text)


def permission_profile(config: LineConfig, protected: Sequence[Path]) -> str:
    """The ``dromon-line`` profile for ``config``, as an inline TOML table.

    The workspace is writable only with a write tool, ``.git`` is readable but not
    writable, and each ``protected`` directory that exists is denied, as its real path,
    since Codex enforces the path the operating system sees.
    """
    workspace = "write" if WRITE_TOOLS & set(config.builtin_tools) else "read"
    entries = [
        f'":workspace_roots"={{"."="{workspace}", ".git"="read"}}',
        *(
            f'{_toml_string(os.path.realpath(directory))}="deny"'
            for directory in protected
            if os.path.exists(directory)
        ),
    ]
    return f'{{extends=":workspace", filesystem={{{", ".join(entries)}}}}}'


def server_arguments(
    servers: Sequence[str], config: LineConfig, protected: Sequence[Path] = ()
) -> list[str]:
    """The ``codex app-server`` arguments that isolate a line configured as ``config``
    from the operator's ``servers``, from every tool it was not given, and from the
    ``protected`` directories."""
    arguments = ["app-server"]
    for name in sorted(set(servers)):
        # Codex's -c parser takes bare dotted keys only, and rejects a quoted one.
        if BARE_KEY.fullmatch(name) is None:
            raise EngineRefused(
                f"the MCP server name {name!r} cannot be disabled by a Codex override,"
                " so the line cannot be isolated"
            )
        arguments += ["-c", f"mcp_servers.{name}.enabled=false"]
    for feature in DISABLED_FEATURES:
        arguments += ["--disable", feature]
    shell = "enable" if "Bash" in config.builtin_tools else "disable"
    for feature in SHELL_FEATURES:
        arguments += [f"--{shell}", feature]
    arguments += ["-c", 'web_search="disabled"']
    # A bare key, since Codex's -c parser rejects a quoted one, with the profile's
    # quoted keys inside the inline table value.
    arguments += [
        "-c",
        f"permissions.{PROFILE_NAME}={permission_profile(config, protected)}",
    ]
    return arguments


def dynamic_tool(tool: SdkMcpTool[Any]) -> dict[str, object]:
    """One of Dromon's tools as a Codex dynamic tool."""
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
    }


def tool_output(result: Mapping[str, Any]) -> dict[str, object]:
    """A Dromon tool's result as a Codex dynamic tool response."""
    items: list[dict[str, object]] = []
    for block in cast(list[Mapping[str, Any]], result.get("content", [])):
        if block.get("type") == "text":
            items.append({"type": "inputText", "text": str(block.get("text", ""))})
    return {"success": not result.get("is_error", False), "contentItems": items}


class Connection(Protocol):
    """One running app-server, as the adapter uses it, so tests can substitute it."""

    async def send(self, message: Mapping[str, object]) -> None: ...

    async def receive(self) -> dict[str, Any] | None: ...


type ConnectionFactory = Callable[
    [list[str], Path, Mapping[str, str]], AbstractAsyncContextManager[Connection]
]
type ServerLister = Callable[[Mapping[str, str]], Sequence[str]]


class _Process:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    async def send(self, message: Mapping[str, object]) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise EngineRefused("the engine was started without a standard input")
        stdin.write((json.dumps(message) + "\n").encode())
        await stdin.drain()

    async def receive(self) -> dict[str, Any] | None:
        stdout = self._process.stdout
        if stdout is None:
            raise EngineRefused("the engine was started without a standard output")
        line = await stdout.readline()
        if not line:
            return None
        decoded: object = json.loads(line)
        if not isinstance(decoded, dict):
            raise EngineRefused("the engine sent a message that is not an object")
        return cast(dict[str, Any], decoded)


def codex_path() -> str:
    found = shutil.which("codex")
    if found is None:
        raise EngineRefused("the codex command is not installed or not on PATH")
    return found


@asynccontextmanager
async def _app_server(
    arguments: list[str], cwd: Path, environment: Mapping[str, str]
) -> AsyncGenerator[Connection]:
    process = await asyncio.create_subprocess_exec(
        codex_path(),
        *arguments,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=cwd,
        env=dict(environment),
        limit=MAX_LINE,
    )
    try:
        yield _Process(process)
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except TimeoutError:
                process.kill()
                await process.wait()


def list_servers(environment: Mapping[str, str]) -> Sequence[str]:
    """The names of every MCP server Codex would load, from ``codex mcp list``."""
    completed = subprocess.run(  # noqa: S603
        [codex_path(), "mcp", "list", "--json"],
        capture_output=True,
        text=True,
        env=dict(environment),
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise EngineRefused("codex mcp list failed, so the line cannot be isolated")
    decoded: object = json.loads(completed.stdout)
    if not isinstance(decoded, list):
        raise EngineRefused("codex mcp list returned something other than a list")
    names: list[str] = []
    for entry in cast(list[object], decoded):
        name = (
            cast(dict[str, object], entry).get("name")
            if isinstance(entry, dict)
            else None
        )
        if not isinstance(name, str):
            raise EngineRefused("codex mcp list returned a server without a name")
        names.append(name)
    return names


class _RpcError(Exception):
    pass


@dataclass(slots=True)
class _Turn:
    text: list[str] = field(default_factory=list[str])
    tool_uses: list[str] = field(default_factory=list[str])
    interrupted_for: str | None = None
    refusal: str | None = None
    turn_id: str | None = None
    status: str = ""


class CodexLine:
    """One session line on the Codex engine, with its driver."""

    def __init__(
        self,
        driver: Driver,
        cwd: Path,
        *,
        config: LineConfig = DEFAULT_CONFIG,
        mcp_tools: Sequence[SdkMcpTool[Any]] = (),
        session_id: str | None = None,
        protected: Sequence[Path] = (),
        environment: Mapping[str, str] | None = None,
        connection_factory: ConnectionFactory = _app_server,
        server_lister: ServerLister = list_servers,
        observe: Observer = ignore,
    ) -> None:
        if driver.settings.engine != ENGINE_NAME:
            raise EngineRefused(
                f"the driver for {driver.settings.model} is written for the"
                f" {driver.settings.engine} engine, not {ENGINE_NAME}"
            )
        self.driver = driver
        self.cwd = cwd
        self.config = config
        self.mcp_tools = {tool.name: tool for tool in mcp_tools}
        self.session_id = session_id
        self.protected = tuple(protected)
        self._environment = os.environ if environment is None else environment
        self._connect = connection_factory
        self._list_servers = server_lister
        self._observe = observe
        self._next_id = 0
        self._thread_id = ""
        self._connection: Connection | None = None

    async def run_turn(self, prompt: str) -> TurnReport:
        """Run one turn for ``prompt`` and return what happened."""
        environment = engine_environment(
            self._environment, self.driver.settings.strategy
        )
        reason = unsupported(self.config)
        if reason is not None:
            raise EngineRefused(reason)
        arguments = server_arguments(
            self._list_servers(environment), self.config, self.protected
        )
        turn = _Turn()
        async with self._connect(arguments, self.cwd, environment) as connection:
            self._connection = connection
            try:
                credential = await self._start(turn)
                thread_id = await self._open_thread(turn)
                if turn.refusal is None:
                    await self._check_isolation(thread_id, turn)
                if turn.refusal is None:
                    await self._run(thread_id, prompt, turn)
            except _RpcError as error:
                raise EngineRefused(f"the engine refused a request: {error}") from error
            finally:
                self._connection = None
        if turn.refusal is not None:
            raise EngineRefused(turn.refusal)
        self.session_id = thread_id
        subtype = "success" if turn.status == "completed" else turn.status
        self.driver.step(Event("result", subtype, 1))
        return TurnReport(
            subtype=subtype,
            num_turns=1,
            text="".join(turn.text),
            tool_uses=tuple(turn.tool_uses),
            interrupted_for=turn.interrupted_for,
            credential=credential,
            session_id=thread_id,
            capabilities=None,
            cost_estimate_usd=None,
        )

    # The protocol.

    def _open(self) -> Connection:
        if self._connection is None:
            raise EngineRefused("the engine connection is not open")
        return self._connection

    async def _send(self, message: Mapping[str, object]) -> None:
        await self._open().send(message)

    async def _receive(self) -> dict[str, Any]:
        message = await self._open().receive()
        if message is None:
            raise EngineRefused("the engine closed its connection during the turn")
        return message

    async def _call(
        self, method: str, params: Mapping[str, object], turn: _Turn
    ) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        await self._send({"id": request_id, "method": method, "params": params})
        while True:
            message = await self._receive()
            if message.get("id") == request_id and "method" not in message:
                if "error" in message:
                    raise _RpcError(f"{method}: {message['error']}")
                result: object = message.get("result")
                return cast(dict[str, Any], result) if isinstance(result, dict) else {}
            await self._dispatch(message, turn)

    async def _dispatch(self, message: dict[str, Any], turn: _Turn) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        params = cast(dict[str, Any], message.get("params") or {})
        if "id" in message:
            await self._answer(message["id"], method, params)
        else:
            await self._notice(method, params, turn)

    async def _answer(
        self, request_id: object, method: str, params: dict[str, Any]
    ) -> None:
        """Answer a request from the engine. Only Dromon's own tools are served, and
        every approval is declined, since the line runs with approvals never asked."""
        if method == "item/tool/call":
            tool = self.mcp_tools.get(str(params.get("tool")))
            if tool is None:
                result = tool_output(
                    {
                        "content": [{"type": "text", "text": "no such tool"}],
                        "is_error": True,
                    }
                )
            else:
                arguments = params.get("arguments")
                result = tool_output(
                    await tool.handler(
                        cast(dict[str, Any], arguments)
                        if isinstance(arguments, dict)
                        else {}
                    )
                )
            await self._send({"id": request_id, "result": result})
        elif method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            await self._send({"id": request_id, "result": {"decision": "decline"}})
        else:
            await self._send(
                {
                    "id": request_id,
                    "error": {
                        "code": -32601,
                        "message": f"Dromon does not serve {method}",
                    },
                }
            )

    async def _notice(self, method: str, params: dict[str, Any], turn: _Turn) -> None:
        if method == "item/started":
            await self._item_started(
                cast(dict[str, Any], params.get("item") or {}), turn
            )
        elif method == "item/completed":
            item = cast(dict[str, Any], params.get("item") or {})
            if item.get("type") == "agentMessage":
                text = str(item.get("text") or "")
                turn.text.append(text)
                self._observe(ActivityKind.TEXT, text)
                await self._act(Event("text", "", len(text)), turn)
        elif method == "model/rerouted":
            model = str(params.get("toModel") or "")
            verdict = self.driver.step(Event("init", model))
            if verdict.tag is ActionTag.REFUSE:
                await self._interrupt(
                    turn, f"the engine rerouted the turn to the model {model!r}"
                )
        elif method == "turn/started":
            started = cast(dict[str, Any], params.get("turn") or {})
            turn.turn_id = turn.turn_id or cast(str | None, started.get("id"))
        elif method == "turn/completed":
            completed = cast(dict[str, Any], params.get("turn") or {})
            turn.status = str(completed.get("status") or "")

    async def _item_started(self, item: dict[str, Any], turn: _Turn) -> None:
        kind = str(item.get("type") or "")
        if kind in QUIET_ITEMS:
            if kind == "reasoning":
                self._observe(ActivityKind.THINKING, "")
                await self._act(Event("thinking"), turn)
            return
        if kind == "dynamicToolCall" and str(item.get("tool")) in self.mcp_tools:
            name = str(item.get("tool"))
        elif kind == "commandExecution" and "Bash" in self.config.builtin_tools:
            name = "Bash"
        elif kind == "fileChange" and WRITE_TOOLS & set(self.config.builtin_tools):
            name = "Edit"
        else:
            await self._interrupt(
                turn, f"the engine used a tool the line was not given: {kind}"
            )
            return
        turn.tool_uses.append(name)
        self._observe(ActivityKind.TOOL, name)
        await self._act(Event("tool_use", name), turn)

    async def _act(self, event: Event, turn: _Turn) -> None:
        action = self.driver.step(event)
        if action.tag is ActionTag.INTERRUPT and turn.interrupted_for is None:
            turn.interrupted_for = action.text
            await self._interrupt(turn, None)

    async def _interrupt(self, turn: _Turn, refusal: str | None) -> None:
        if refusal is not None and turn.refusal is None:
            turn.refusal = refusal
        if turn.turn_id is None or turn.status:
            return
        self._next_id += 1
        await self._send(
            {
                "id": self._next_id,
                "method": "turn/interrupt",
                "params": {"threadId": self._thread_id, "turnId": turn.turn_id},
            }
        )

    # The steps of a turn.

    async def _start(self, turn: _Turn) -> Credential:
        await self._call(
            "initialize",
            {
                "clientInfo": {"name": "dromon", "version": __version__},
                "capabilities": {"experimentalApi": True},
            },
            turn,
        )
        await self._send({"method": "initialized"})
        account = (await self._call("account/read", {}, turn)).get("account")
        kind = (
            cast(dict[str, Any], account).get("type")
            if isinstance(account, dict)
            else None
        )
        credential = ACCOUNT_CREDENTIALS.get(str(kind))
        if credential is None:
            raise EngineRefused(f"the engine reports an unrecognized account {kind!r}")
        verdict = self.driver.step(Event("credential", credential.value))
        if verdict.tag is ActionTag.REFUSE:
            raise EngineRefused(
                f"the {verdict.text} driver refuses the {credential} credential"
            )
        return credential

    async def _open_thread(self, turn: _Turn) -> str:
        settings: dict[str, object] = {
            "model": self.driver.settings.model,
            "cwd": str(self.cwd),
            "permissions": PROFILE_NAME,
            "approvalPolicy": "never",
            "approvalsReviewer": "user",
        }
        instructions = read_instructions(self.config.instructions, self.cwd)
        if instructions:
            settings["developerInstructions"] = instructions
        if self.session_id is None:
            settings["dynamicTools"] = [
                dynamic_tool(t) for t in self.mcp_tools.values()
            ]
            result = await self._call("thread/start", settings, turn)
        else:
            result = await self._call(
                "thread/resume", {**settings, "threadId": self.session_id}, turn
            )
        thread = cast(dict[str, Any], result.get("thread") or {})
        thread_id = str(thread.get("id") or "")
        if not thread_id:
            raise EngineRefused("the engine started a thread without an identifier")
        self._thread_id = thread_id
        provider = result.get("modelProvider")
        if provider != "openai":
            turn.refusal = f"the engine reports the model provider {provider!r}"
            return thread_id
        active = result.get("activePermissionProfile")
        active_id = (
            cast(dict[str, Any], active).get("id") if isinstance(active, dict) else None
        )
        if active_id != PROFILE_NAME:
            turn.refusal = (
                f"the engine runs the permission profile {active_id!r},"
                f" not {PROFILE_NAME}"
            )
            return thread_id
        model = str(result.get("model") or "")
        verdict = self.driver.step(Event("init", model))
        if verdict.tag is ActionTag.REFUSE:
            turn.refusal = (
                f"the driver for {verdict.text} refuses the engine model {model!r}"
            )
        return thread_id

    async def _check_isolation(self, thread_id: str, turn: _Turn) -> None:
        servers = await self._call(
            "mcpServerStatus/list", {"threadId": thread_id}, turn
        )
        live = sorted(
            str(entry.get("name"))
            for entry in cast(list[dict[str, Any]], servers.get("data") or [])
            if entry.get("runtimeStatus") != "disabled" or entry.get("tools")
        )
        if live:
            turn.refusal = (
                "the engine loads MCP servers the line was not given: "
                + ", ".join(live)
            )
            return
        apps = await self._call("app/list", {}, turn)
        if apps.get("data"):
            turn.refusal = "the engine offers apps the line was not given"

    async def _run(self, thread_id: str, prompt: str, turn: _Turn) -> None:
        self.driver.step(Event("turn_start"))
        started = await self._call(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "effort": self.config.effort or self.driver.settings.effort,
            },
            turn,
        )
        turn.turn_id = turn.turn_id or cast(
            str | None, cast(dict[str, Any], started.get("turn") or {}).get("id")
        )
        while not turn.status:
            await self._dispatch(await self._receive(), turn)
