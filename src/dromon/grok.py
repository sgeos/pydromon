"""The Grok Build engine adapter, which runs session line turns through ``grok agent``.

**Unverified against a live turn.** The operator's Grok usage was exhausted when this
adapter was written, on 2026-10-25, so it rests on Grok Build 1.0.70's documentation
and on an ``initialize`` handshake, which runs no model. Wherever a behaviour could
not be confirmed, the adapter refuses the turn rather than assume it.
``docs/protocol/GROK.md`` records which facts were checked and which were not.

Dromon drives Grok from outside. Each turn starts ``grok agent --no-leader stdio``,
which speaks the Agent Client Protocol as JSON-RPC 2.0, one message per line, creates
or loads the line's session, and feeds every update to the line's driver.

Credentials. Grok signs in by itself, and Dromon never reads its login. For a
subscription driver, Grok is started without the variables that would let it use an API
key, and its ``initialize`` answer must name the cached sign-in as its default
authentication method. An API driver is refused, since Dromon cannot yet make Grok
prefer a key over a cached sign-in.

Sandbox. Grok's custom sandbox profiles live only in ``sandbox.toml`` files, which an
overlay cannot set. Dromon therefore requires the operator to define ``dromon-reader``
and, for a line given ``Edit`` or ``Write``, ``dromon-writer``, checks before each turn
that the profile denies Dromon's state and runtime directories and that a writer keeps
``.git`` read-only, and selects it with ``GROK_SANDBOX``. Grok documents that a custom
profile it cannot apply stops it from starting. On macOS Grok does not block a child
process's network access under any profile.

Tools and permissions. Grok loads the operator's configuration, including MCP servers
and a permission mode. Dromon asks for Grok's ask mode, refuses a session that reports
any other mode, answers every permission request from the line's configuration, and
interrupts and refuses a turn in which Grok uses a tool the line was not given. Each
tool call is judged when first seen, and a later update or request that changes its
name, title or kind refuses the turn. Dromon's
own tools reach Grok as an MCP server, ``dromon mcp --line``, through the daemon's line
socket and a token the daemon issued for the line.

The protocol itself, its request loop and the answers Dromon gives, lives in
``dromon.acp``, which the Antigravity adapter shares.

Porting note. The protocol is JSON lines over a pipe, and the adapter is sequential, as
the Codex adapter is.
"""

import asyncio
import json
import os
import shutil
import tomllib
from collections.abc import AsyncGenerator, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, cast

from claude_agent_sdk import SdkMcpTool

from dromon import acp, evidence
from dromon.acp import bridge_command as bridge_command
from dromon.activity import ActivityKind, Observer, ignore
from dromon.driver import ActionTag, Credential, Driver, Event, Strategy
from dromon.engine import EngineRefused, TurnReport, read_instructions
from dromon import launcher
from dromon.launcher import STOP_POLICY_VARIABLE
from dromon.processes import MARK_VARIABLE
from dromon.lineconfig import DEFAULT_CONFIG, LineConfig

ENGINE_NAME: Final = "grok-build"
"""The engine this adapter runs, as drivers name it."""

SUPPORTED_BUILTINS: Final = frozenset(
    {"Bash", "Read", "Grep", "Glob", "Edit", "Write", "WebFetch", "WebSearch"}
)
"""Built-in tool names a Grok line may be given, in Dromon's vocabulary."""

WRITE_TOOLS: Final = frozenset({"Edit", "Write"})

KIND_TOOLS: Final[dict[str, frozenset[str]]] = {
    "read": frozenset({"Read"}),
    "search": frozenset({"Grep", "Glob", "Read"}),
    "edit": WRITE_TOOLS,
    "delete": WRITE_TOOLS,
    "move": WRITE_TOOLS,
    "execute": frozenset({"Bash"}),
    "fetch": frozenset({"WebFetch", "WebSearch"}),
}
"""Each ACP tool kind, and the built-in tools any one of which permits it."""

READER_PROFILE: Final = "dromon-reader"
WRITER_PROFILE: Final = "dromon-writer"
PROFILE_BASES: Final[dict[str, frozenset[str]]] = {
    READER_PROFILE: frozenset({"read-only"}),
    WRITER_PROFILE: frozenset({"workspace", "strict"}),
}
"""The built-in profiles each of Dromon's profiles may extend."""

API_ENVIRONMENT: Final = (
    "XAI_API_KEY",
    "GROK_AUTH",
    "GROK_CODE_XAI_API_KEY",
    "GROK_AUTH_PATH",
)
"""Variables that may give Grok a credential other than its cached sign-in."""

DROMON_CONTROLLED: Final = ("GROK_SANDBOX", "GROK_CONFIG", "GROK_CONFIG_PATH")
"""Variables Dromon sets or clears itself."""

REFUSED_ENVIRONMENT: Final = ("GROK_HOME", "XAI_API_BASE_URL")
"""Variables that would move Grok's configuration or send its requests elsewhere."""

ASK_MODES: Final = frozenset({"default", "ask"})
"""Session modes in which Grok asks before a tool call that is not read-only."""

EFFORTS: Final[dict[str, str]] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "xhigh",
}
"""Dromon's efforts as Grok's, which have no ``max``."""

MAX_LINE: Final = 64 * 1024 * 1024

ABORT_SECONDS: Final = 60
"""**Re-review, N2.** How long a cancelled turn waits for Grok to answer its prompt
before Grok is stopped. Before, a Grok that ignored the cancel held the line for
ever."""

TURN_SECONDS: Final = 3600
"""Maximum inactivity between model or tool events during a turn."""

START_SECONDS: Final = 180
"""Maximum duration of Grok initialization, including sign-in and session setup."""

ABSOLUTE_TURN_SECONDS: Final = 4 * 60 * 60
"""Absolute four hour cap for one Grok turn, even while activity continues."""


def unsupported(config: LineConfig) -> str | None:
    """Why a Grok line cannot run ``config``, or ``None`` if it can."""
    extra = sorted(set(config.builtin_tools) - SUPPORTED_BUILTINS)
    if extra:
        return (
            "Grok lines support the built-in tools "
            + ", ".join(sorted(SUPPORTED_BUILTINS))
            + ", not "
            + ", ".join(extra)
        )
    if config.mcp_servers or config.inherit_account_mcp:
        return "Grok lines do not yet support MCP servers"
    if config.claude_code_prompt:
        return "Grok lines run on Grok's own prompt, not Claude Code's"
    if config.project_settings:
        return "Grok lines do not load Claude Code's project settings"
    if config.plugins:
        return "Grok lines do not load Claude Code plugins"
    if config.allow_compact:
        return "Grok lines cannot compact their session through Dromon"
    if config.keep_session:
        return "Grok lines do not yet keep their engine session between turns"
    if config.sandbox is not None or config.settings is not None:
        return (
            "Grok lines run under a Grok sandbox profile, and take no sandbox or"
            " settings document"
        )
    return None


def engine_environment(
    environment: Mapping[str, str], strategy: Strategy
) -> dict[str, str]:
    """The environment Grok starts with, without credential variables or Dromon's own,
    or ``EngineRefused`` when it would move Grok's configuration or requests."""
    if strategy is Strategy.API:
        raise EngineRefused(
            "Grok lines do not yet support the api strategy, because Dromon cannot make"
            " Grok prefer an API key over a cached sign-in"
        )
    present = [name for name in REFUSED_ENVIRONMENT if environment.get(name)]
    if present:
        raise EngineRefused(
            "the environment conflicts with a Grok line: " + ", ".join(present)
        )
    removed = set(API_ENVIRONMENT) | set(DROMON_CONTROLLED)
    return {k: v for k, v in environment.items() if k not in removed}


# The sandbox profile.


def profile_name(config: LineConfig) -> str:
    return WRITER_PROFILE if WRITE_TOOLS & set(config.builtin_tools) else READER_PROFILE


def _load_profiles(file: Path) -> dict[str, dict[str, object]]:
    try:
        document = tomllib.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise EngineRefused(
            f"cannot read the Grok sandbox file {file}: {error}"
        ) from error
    profiles = document.get("profiles", {})
    if not isinstance(profiles, dict):
        raise EngineRefused(f"{file} has a profiles key that is not a table")
    return {
        str(name): cast(dict[str, object], value)
        for name, value in cast(dict[str, object], profiles).items()
        if isinstance(value, dict)
    }


def _entries(profile: Mapping[str, object], key: str) -> list[str]:
    value = profile.get(key, [])
    if not isinstance(value, list):
        return []
    return [entry for entry in cast(list[object], value) if isinstance(entry, str)]


def _resolve(entry: str, workspace: Path, home: Path) -> Path | None:
    """Where a ``deny`` entry points, as Grok documents it, or ``None`` for a glob."""
    if any(character in entry for character in "*?["):
        return None
    if entry == "~" or entry.startswith("~/"):
        return home / entry.removeprefix("~").removeprefix("/")
    path = Path(entry)
    return path if path.is_absolute() else workspace / path


def _covers(paths: Sequence[Path], target: Path) -> bool:
    real = Path(os.path.realpath(target))
    for path in paths:
        guard = Path(os.path.realpath(path))
        if real == guard or guard in real.parents:
            return True
    return False


def profile_snippet(name: str, workspace: Path, protected: Sequence[Path]) -> str:
    """The ``sandbox.toml`` table that defines ``name`` for this project."""
    deny = [".dromon"] + [
        os.path.realpath(p)
        for p in protected
        if not _covers([workspace / ".dromon"], p)
    ]
    lines = [f"[profiles.{name}]"]
    if name == WRITER_PROFILE:
        lines.append('extends = "workspace"')
        lines.append(
            f"read_only = [{json.dumps(os.path.realpath(workspace / '.git'))}]"
        )
    else:
        lines.append('extends = "read-only"')
    lines.append(f"deny = [{', '.join(json.dumps(entry) for entry in deny)}]")
    return "\n".join(lines)


def check_profile(
    config: LineConfig, workspace: Path, protected: Sequence[Path], home: Path
) -> str:
    """The name of the Grok profile the line runs under, after checking that it denies
    every ``protected`` directory and, for a writer, keeps ``.git`` read-only. Raises
    ``EngineRefused`` naming the table to add when it does not.

    Grok uses the user's definition when the user's and the project's files differ, so
    the user's file is checked first.
    """
    name = profile_name(config)
    user = _load_profiles(home / ".grok" / "sandbox.toml")
    project = _load_profiles(workspace / ".grok" / "sandbox.toml")
    profile = user.get(name) or project.get(name)
    advice = (
        f" Add this to ~/.grok/sandbox.toml:\n\n"
        f"{profile_snippet(name, workspace, protected)}"
    )
    if profile is None:
        raise EngineRefused(f"no Grok sandbox profile {name} is defined.{advice}")
    base = profile.get("extends", "workspace")
    if base not in PROFILE_BASES[name]:
        raise EngineRefused(
            f"the Grok sandbox profile {name} extends {base!r}, which Dromon does not"
            f" accept for it.{advice}"
        )
    denied = [
        path
        for entry in _entries(profile, "deny")
        if (path := _resolve(entry, workspace, home)) is not None
    ]
    exposed = [
        str(p) for p in protected if os.path.exists(p) and not _covers(denied, p)
    ]
    if exposed:
        raise EngineRefused(
            f"the Grok sandbox profile {name} does not deny "
            + ", ".join(exposed)
            + "."
            + advice
        )
    git = workspace / ".git"
    if name == WRITER_PROFILE and git.exists():
        held = [Path(entry) for entry in _entries(profile, "read_only")]
        if not _covers(held, git) and not _covers(denied, git):
            raise EngineRefused(
                f"the Grok sandbox profile {name} leaves {git} writable.{advice}"
            )
    return name


# The connection.


class Connection(Protocol):
    async def send(self, message: Mapping[str, object]) -> None: ...

    async def receive(self) -> dict[str, Any] | None: ...


type ConnectionFactory = Callable[
    [list[str], Path, Mapping[str, str]], AbstractAsyncContextManager[Connection]
]


class _Process:
    def __init__(
        self,
        process: asyncio.subprocess.Process,
        recording: evidence.Recording | None = None,
    ) -> None:
        self._process = process
        self._recording = recording

    async def send(self, message: Mapping[str, object]) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise EngineRefused("the engine was started without a standard input")
        stdin.write((json.dumps(message) + "\n").encode())
        await stdin.drain()
        if self._recording is not None:
            self._recording.frame(evidence.OUT, message)

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
        record = cast(dict[str, Any], decoded)
        if self._recording is not None:
            self._recording.frame(evidence.IN, record)
        return record


@asynccontextmanager
async def _grok_agent(
    arguments: list[str], cwd: Path, environment: Mapping[str, str]
) -> AsyncGenerator[Connection]:
    found = shutil.which("grok")
    if found is None:
        raise EngineRefused("the grok command is not installed or not on PATH")
    command, launched, policy = launcher.wrap([found, *arguments], environment)
    recording = evidence.RECORDING.get()
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=evidence.STDERR_TARGET,
        cwd=cwd,
        env=launched,
        limit=MAX_LINE,
        start_new_session=True,
    )
    draining = evidence.start_draining(process, recording)
    try:
        yield _Process(process, recording)
    finally:
        # The launcher ends the agent and, by the line's stop policy, its group and
        # every descendant, which Grok's detached tool groups need.
        await launcher.end(process, policy, close_input=False)
        await evidence.stop_draining(draining)


@dataclass(slots=True)
class _Turn:
    text: list[str] = field(default_factory=list[str])
    tool_uses: list[str] = field(default_factory=list[str])
    calls: dict[str, dict[str, object]] = field(
        default_factory=dict[str, dict[str, object]]
    )
    """Each tool call's identity as first recorded, by its identifier."""
    interrupted_for: str | None = None
    refusal: str | None = None
    cancelled: bool = False
    absolute_deadline: float | None = None


def grok_arguments(model: str, effort: str) -> list[str]:
    """The ``grok`` arguments for one turn. The model and effort are agent options,
    placed before the transport, as Grok documents."""
    return [
        "agent",
        "--no-leader",
        "--model",
        model,
        "--reasoning-effort",
        EFFORTS[effort],
        "stdio",
    ]


class GrokLine:
    """One session line on the Grok Build engine, with its driver."""

    def __init__(
        self,
        driver: Driver,
        cwd: Path,
        *,
        config: LineConfig = DEFAULT_CONFIG,
        mcp_tools: Sequence[SdkMcpTool[Any]] = (),
        session_id: str | None = None,
        protected: Sequence[Path] = (),
        bridge: Sequence[str] = (),
        line_token: str | None = None,
        environment: Mapping[str, str] | None = None,
        home: Path | None = None,
        connection_factory: ConnectionFactory = _grok_agent,
        observe: Observer = ignore,
        mark: str | None = None,
    ) -> None:
        if driver.settings.engine != ENGINE_NAME:
            raise EngineRefused(
                f"the driver for {driver.settings.model} is written for the"
                f" {driver.settings.engine} engine, not {ENGINE_NAME}"
            )
        self.driver = driver
        self.cwd = cwd
        self.config = config
        self.own_tools = acp.own_tool_names([tool.name for tool in mcp_tools])
        self.session_id = session_id
        self.protected = tuple(protected)
        self.bridge = tuple(bridge)
        self.line_token = line_token
        self._environment = os.environ if environment is None else environment
        self._home = Path.home() if home is None else home
        self._connect = connection_factory
        self._observe = observe
        self.mark = mark
        """The process mark every process of this line inherits, if issued."""
        self._client: acp.Client | None = None
        self.session_id_in_use: str | None = None

    async def run_turn(self, prompt: str) -> TurnReport:
        """Run one turn for ``prompt`` and return what happened."""
        strategy = self.driver.settings.strategy
        environment = engine_environment(self._environment, strategy)
        if self.mark is not None:
            environment[MARK_VARIABLE] = self.mark
        environment[STOP_POLICY_VARIABLE] = self.config.stop_policy.value
        reason = unsupported(self.config)
        if reason is not None:
            raise EngineRefused(reason)
        environment["GROK_SANDBOX"] = check_profile(
            self.config, self.cwd, self.protected, self._home
        )
        effort = self.config.effort or self.driver.settings.effort
        if effort not in EFFORTS:
            raise EngineRefused(f"the driver names an unknown effort {effort!r}")
        arguments = grok_arguments(self.driver.settings.model, effort)
        turn = _Turn()
        credential: Credential | None = None
        session_id: str | None = None
        stop = "refused"
        async with self._connect(arguments, self.cwd, environment) as connection:

            async def dispatch(message: dict[str, Any]) -> None:
                await self._dispatch(message, turn)

            self._client = acp.Client(connection, dispatch)
            try:
                self._client.deadline = (
                    asyncio.get_running_loop().time() + START_SECONDS
                )
                stage = "initialize"
                try:
                    credential = await self._start(turn)
                    stage = "open session"
                    session_id = await self._open_session(turn)
                except TimeoutError as error:
                    raise EngineRefused(
                        f"Grok start timed out during {stage}"
                    ) from error
                if turn.refusal is None:
                    stop = await self._prompt(session_id, prompt, turn)
            except acp.RpcError as error:
                raise EngineRefused(f"the engine refused a request: {error}") from error
            except TimeoutError:
                # Closing the connection stops Grok if its turn exceeds the bound.
                if turn.refusal is None and turn.interrupted_for is None:
                    now = asyncio.get_running_loop().time()
                    turn.refusal = (
                        f"Grok turn reached its absolute cap of "
                        f"{ABSOLUTE_TURN_SECONDS} seconds"
                        if turn.absolute_deadline is not None
                        and now >= turn.absolute_deadline
                        else f"Grok turn had no activity for {TURN_SECONDS} seconds"
                    )
                stop = "cancelled"
            finally:
                self._client = None
        if turn.refusal is not None:
            raise EngineRefused(turn.refusal)
        if credential is None or session_id is None:
            raise EngineRefused("the engine did not finish cancelling its turn")
        self.session_id = session_id
        subtype = "success" if stop == "end_turn" else stop
        self.driver.step(Event("result", subtype, 1))
        return TurnReport(
            subtype=subtype,
            num_turns=1,
            text="".join(turn.text),
            tool_uses=tuple(turn.tool_uses),
            interrupted_for=turn.interrupted_for,
            credential=credential,
            session_id=session_id,
            capabilities=None,
            cost_estimate_usd=None,
        )

    # The protocol.

    def _rpc(self) -> acp.Client:
        if self._client is None:
            raise EngineRefused("the engine connection is not open")
        return self._client

    async def _dispatch(self, message: dict[str, Any], turn: _Turn) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            return
        if method == "session/update" or method == "session/request_permission":
            self._extend_turn_deadline(turn)
        params = cast(dict[str, Any], message.get("params") or {})
        if "id" in message:
            await self._answer(message["id"], method, params, turn)
        elif method == "session/update":
            await self._update(cast(dict[str, Any], params.get("update") or {}), turn)

    def _permits(self, tool_call: Mapping[str, Any]) -> tuple[bool, str]:
        """Whether the line may make ``tool_call``, and the tool name to count it as.

        **Antigravity review, F4.** One of the line's own tools is recognised only by
        the structured ``toolName``, never by a title, which a file name or the model
        could make equal to it. A call with only a title is judged by its kind, and
        any other kind than a built-in tool's is refused. No kind is exempt, since
        the review's F1 was applied to Grok on 2026-10-26."""
        name = acp.tool_name(tool_call)
        if name in self.own_tools:
            return True, name
        kind = str(tool_call.get("kind") or "other")
        allowed = KIND_TOOLS.get(kind)
        if allowed is None:
            return False, acp.tool_label(tool_call)
        given = allowed & set(self.config.builtin_tools)
        return bool(given), min(given) if given else kind

    async def _answer(
        self, request_id: object, method: str, params: dict[str, Any], turn: _Turn
    ) -> None:
        if method == "session/request_permission":
            raw: object = params.get("toolCall")
            tool_call = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
            if turn.cancelled:
                # ACP requires every request pending after a cancel to be answered
                # as cancelled, which grants nothing.
                await self._rpc().respond(
                    request_id, {"outcome": {"outcome": "cancelled"}}
                )
                return
            identifier = tool_call.get("toolCallId")
            refusal = acp.identity_refusal(identifier, tool_call, turn.calls)
            # With no field changed, the call is what was recorded plus what the
            # request adds, so a request that omits the recorded kind keeps it.
            known = turn.calls.get(str(identifier), {})
            permitted = refusal is None and self._permits({**known, **tool_call})[0]
            options = cast(list[dict[str, Any]], params.get("options") or [])
            await self._rpc().respond(
                request_id, acp.permission_result(options, permitted)
            )
            if refusal is not None:
                await self._cancel(turn, refusal)
            else:
                await self._record(str(identifier), tool_call, turn)
        else:
            await self._rpc().refuse(request_id, method)

    async def _update(self, update: dict[str, Any], turn: _Turn) -> None:
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = cast(dict[str, Any], update.get("content") or {})
            text = str(content.get("text") or "")
            turn.text.append(text)
            self._observe(ActivityKind.TEXT, text, chunk=True)
            await self._act(Event("text", "", len(text)), turn)
        elif kind == "agent_thought_chunk":
            self._observe(ActivityKind.THINKING, "")
            await self._act(Event("thinking"), turn)
        elif kind in ("tool_call", "tool_call_update"):
            # Antigravity review, F1 to F3, applied to Grok on 2026-10-26. No kind is
            # exempt, and every update to a call is compared with its record.
            identifier = update.get("toolCallId")
            refusal = acp.identity_refusal(identifier, update, turn.calls)
            if refusal is not None:
                await self._cancel(turn, refusal)
                return
            await self._record(str(identifier), update, turn)

    async def _record(
        self, identifier: str, fields: Mapping[str, Any], turn: _Turn
    ) -> None:
        """Record a tool call the first time it is seen and judge it then, counting
        it if it is permitted and cancelling the turn if not. A later update or
        permission request for it has passed ``acp.identity_refusal``, so it is the
        call already judged."""
        if identifier in turn.calls:
            return
        turn.calls[identifier] = acp.identity(fields)
        permitted, name = self._permits(fields)
        if not permitted:
            await self._cancel(
                turn, f"the engine used a tool the line was not given: {name}"
            )
            return
        turn.tool_uses.append(name)
        self._observe(ActivityKind.TOOL, name)
        await self._act(Event("tool_use", name), turn)

    async def _act(self, event: Event, turn: _Turn) -> None:
        action = self.driver.step(event)
        if action.tag is ActionTag.INTERRUPT and turn.interrupted_for is None:
            turn.interrupted_for = action.text
            await self._cancel(turn, None)

    async def _cancel(self, turn: _Turn, refusal: str | None) -> None:
        if refusal is not None and turn.refusal is None:
            turn.refusal = refusal
        if turn.cancelled or self.session_id_in_use is None:
            return
        turn.cancelled = True
        client = self._rpc()
        bound = asyncio.get_running_loop().time() + ABORT_SECONDS
        client.deadline = (
            bound if client.deadline is None else min(client.deadline, bound)
        )
        await client.notify("session/cancel", {"sessionId": self.session_id_in_use})

    # The steps of a turn.

    async def _start(self, turn: _Turn) -> Credential:
        result = await self._rpc().call(
            "initialize",
            {
                "protocolVersion": acp.PROTOCOL_VERSION,
                "clientCapabilities": acp.CLIENT_CAPABILITIES,
            },
        )
        if result.get("protocolVersion") != acp.PROTOCOL_VERSION:
            raise EngineRefused(
                f"the engine speaks ACP version {result.get('protocolVersion')!r}"
            )
        meta = cast(dict[str, Any], result.get("_meta") or {})
        method = meta.get("defaultAuthMethodId")
        if method != "cached_token":
            raise EngineRefused(
                f"the engine reports the authentication method {method!r}, not its"
                " cached sign-in"
            )
        # Inference, not verified live: the cached sign-in is a Grok subscription.
        credential = Credential.SUBSCRIPTION
        verdict = self.driver.step(Event("credential", credential.value))
        if verdict.tag is ActionTag.REFUSE:
            raise EngineRefused(
                f"the {verdict.text} driver refuses the {credential} credential"
            )
        return credential

    async def _open_session(self, turn: _Turn) -> str:
        meta: dict[str, object] = {"yoloMode": False, "autoMode": False}
        instructions = read_instructions(self.config.instructions, self.cwd)
        if instructions:
            meta["rules"] = instructions
        params: dict[str, object] = {
            "cwd": str(self.cwd),
            "mcpServers": acp.mcp_servers(self.bridge, self.line_token),
            "_meta": meta,
        }
        if self.session_id is None:
            result = await self._rpc().call("session/new", params)
            session_id = str(result.get("sessionId") or "")
        else:
            result = await self._rpc().call(
                "session/load", {**params, "sessionId": self.session_id}
            )
            session_id = self.session_id
        if not session_id:
            raise EngineRefused("the engine started a session without an identifier")
        self.session_id_in_use = session_id
        model = acp.reported_model(result)
        verdict = self.driver.step(Event("init", model or ""))
        if verdict.tag is ActionTag.REFUSE:
            turn.refusal = (
                f"the driver for {verdict.text} refuses the engine model {model!r}"
            )
            return session_id
        # Refuse the turn unless the session asks before acting, switching to
        # ``default`` when Grok offers it.
        refusal = await acp.ensure_mode(
            self._rpc(), session_id, result, ASK_MODES, "default"
        )
        if refusal is not None:
            turn.refusal = refusal
        return session_id

    async def _prompt(self, session_id: str, prompt: str, turn: _Turn) -> str:
        self.driver.step(Event("turn_start"))
        now = asyncio.get_running_loop().time()
        turn.absolute_deadline = now + ABSOLUTE_TURN_SECONDS
        self._rpc().deadline = min(now + TURN_SECONDS, turn.absolute_deadline)
        result = await self._rpc().call(
            "session/prompt",
            {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
        )
        return str(result.get("stopReason") or "")

    def _extend_turn_deadline(self, turn: _Turn) -> None:
        """Slide the inactivity bound on model or tool activity, under the cap."""
        if turn.absolute_deadline is None:
            return
        client = self._rpc()
        client.deadline = min(
            asyncio.get_running_loop().time() + TURN_SECONDS,
            turn.absolute_deadline,
        )
