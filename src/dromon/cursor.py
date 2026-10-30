"""The Cursor Agent engine adapter, which runs a turn through ``cursor-agent --print``.

**Unverified against a live turn.** Written on 2026-10-26 against Cursor Agent
2026.10.01's help, the operator's summary of its output-format documentation, and its
installed code, without running a model. ``docs/protocol/CURSOR.md`` records which
facts were read and which were inferred, and lists what to check first.

Dromon drives Cursor Agent from outside. Each turn starts ``cursor-agent --print
--output-format stream-json`` with the prompt as its argument, reads one JSON event per
line until the ``result`` event, and feeds each event to the line's driver. Print mode
takes no command after it starts, so an interrupt ends the process.

Isolation. Print mode always offers Cursor's built-in tools, about forty in this
version, and no option switches them off. Dromon runs Cursor on a configuration
directory of its own, made for the turn, whose permission rules deny every read, write,
shell command and web fetch before the tool runs, as Cursor's installed code shows.
Cursor's other tools, among them subagents, semantic search, web search, image
generation and screen recording, are not covered by any rule Dromon can show, so Dromon
can only end the turn and refuse it once such a tool starts. **The operator
decided on 2026-10-26 that a daemon refuses Cursor lines, and that ``dromon probe``
runs one turn only with ``--accept-partial-isolation``**, which sets
``partial_isolation``.

Credentials. Cursor signs in by itself and keeps its login in the macOS keychain or,
with its file store, in ``~/.cursor/auth.json``, neither of which Dromon reads. A
subscription line's Cursor starts without ``CURSOR_API_KEY`` or ``CURSOR_AUTH_TOKEN``,
and its ``init`` event must report the login as its key source.

Dromon's tools reach Cursor as an MCP server in a plugin directory Dromon writes for the
turn, ``dromon mcp --line`` on ``lines.sock``, with the line's token taken from the
environment by Cursor's ``${env:NAME}`` interpolation, so it is never written to disk.

Porting note. JSON lines over a pipe, sequential, as the other adapters.
"""

import asyncio
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from claude_agent_sdk import SdkMcpTool

from dromon import capabilities
from dromon.activity import ActivityKind, Observer, ignore
from dromon.driver import (
    TOOL_RESULT,
    ActionTag,
    Credential,
    Driver,
    Event,
    Strategy,
    harness_id,
)
from dromon.engine import EngineRefused, TurnReport, read_instructions
from dromon.jsonlines import (
    TURN_SECONDS,
    Connection,
    ConnectionFactory,
    is_cloud_or_credential,
    json_lines_factory,
)
from dromon.lineconfig import DEFAULT_CONFIG, LineConfig, WorkdirAccess
from dromon.launcher import STOP_POLICY_VARIABLE
from dromon.processes import MARK_VARIABLE
from dromon.engine_environment import BASE_ENVIRONMENT, allowlisted
from dromon import gitssh
from dromon.gitssh import GitSshAccess

ENGINE_NAME: Final = "cursor-agent"
"""The engine this adapter runs, as drivers name it."""

MODEL_PREFIX: Final = "cursor-agent:"
COMMAND: Final = "cursor-agent"
MCP_SERVER_NAME: Final = "dromon"
PLUGIN_NAME: Final = "dromon"

API_ENVIRONMENT: Final = frozenset(
    {"CURSOR_API_KEY", "CURSOR_AUTH_TOKEN", "CURSOR_AUTH_TOKEN_EXPIRES_AT"}
)
"""Variables that give Cursor a credential other than its login."""

REFUSED_ENVIRONMENT: Final = ("CURSOR_API_ENDPOINT", "CURSOR_API_URL")
"""Variables that would send Cursor's requests elsewhere."""

DROMON_CONTROLLED: Final = ("CURSOR_CONFIG_DIR", "CURSOR_DATA_DIR", "DROMON_LINE_TOKEN")
"""Variables Dromon sets or clears itself."""

HOME_ENTRIES: Final = (
    "agents",
    "cli.json",
    "commands",
    "environment.json",
    "hooks.json",
    "mcp-approvals.json",
    "mcp.json",
    "permissions.json",
    "plugins",
    "rules",
    "sandbox-policies",
    "sandbox.json",
    "settings.json",
    "skills",
    "worktrees.json",
)
"""Entries under ``~/.cursor`` that would add permissions, MCP servers or their
approvals, hooks, plugins, subagents, commands, skills, rules, sandbox policies,
settings or setup commands, as the names Cursor Agent 2026.10.01's code reads show.
Cursor reads ``~/.cursor/mcp.json`` from the home directory whatever its configuration
and data directories. Cursor's own ``cli-config.json``, ``chats``, ``projects`` and
``skills-cursor`` are not among them, since Dromon replaces the configuration and data
directories for the turn and the packaged skills come with Cursor itself."""

DENIED: Final = ("Read(*)", "Write(*)", "Shell(*)", "WebFetch(*)")
"""Permission rules that deny every read, write, shell command and web fetch. Cursor's
matcher turns each ``*`` into any run of characters over the whole path or command."""

CREDENTIALS: Final[dict[str, Credential]] = {
    "login": Credential.SUBSCRIPTION,
    "env": Credential.API,
    "flag": Credential.API,
}
"""Cursor's ``apiKeySource`` values. It reports ``login`` whenever no key was given."""

RECORD_TYPES: Final = frozenset(
    {"system", "user", "assistant", "thinking", "tool_call", "result"}
)
"""The event types Cursor's stream is documented and observed to carry. Any other
refuses the turn, since Dromon cannot tell whether it reports a tool."""

SESSION_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
ABORT_SECONDS: Final = 60

RESUME_REFUSAL: Final = (
    "Cursor lines cannot resume a chat, because each turn runs on an empty data"
    " directory of its own, removed when the turn ends, which holds no earlier chat"
)
"""**Re-review, N1.** A chat lives in Cursor's data directory, which is now made for
the turn and removed with it, so ``--resume`` would find nothing. A multi-turn line
needs a data directory kept for the line and checked as the workspace is, first."""

ISOLATION_REFUSAL: Final = (
    "Cursor Agent runs only in dromon probe with --accept-partial-isolation. Its print"
    " mode always offers built-in tools such as subagents, semantic search, web"
    " search, image generation and screen recording, which no option switches off and"
    " no permission rule Dromon can show covers, so Dromon could only refuse a turn"
    " after such a tool started"
)

LINE_REFUSAL: Final = (
    "Cursor lines are refused in a daemon, by the operator's decision of 2026-10-26,"
    " because Dromon can deny only some of Cursor Agent's built-in tools before they"
    " run. dromon probe --accept-partial-isolation runs one turn"
)


def unsupported(config: LineConfig) -> str | None:
    """Why a Cursor line cannot run ``config``, or ``None`` if it can."""
    if config.workdir_access is WorkdirAccess.READ:
        return "Cursor cannot confine a line to a read-only working directory"
    if config.builtin_tools:
        return (
            "Cursor lines take no built-in tools, because Dromon denies Cursor's own"
            " reads, writes and commands and cannot show that its other tools stay"
            " away from Dromon's state, the operator's credentials or other paths"
        )
    if config.mcp_servers or config.inherit_account_mcp:
        return "Cursor lines do not yet support MCP servers of their own"
    if config.claude_code_prompt:
        return "Cursor lines run on Cursor's own prompt, not Claude Code's"
    if config.project_settings:
        return "Cursor lines do not load Claude Code's project settings"
    if config.plugins:
        return "Cursor lines do not load Claude Code plugins"
    if config.allow_compact:
        return "Cursor lines cannot compact their session through Dromon"
    if config.keep_session:
        return capabilities.kept_session_refusal(capabilities.CURSOR)
    if config.sandbox is not None or config.settings is not None:
        return "Cursor lines take no sandbox or settings document"
    return None


def line_refusal(config: LineConfig) -> str | None:
    """Why a daemon cannot hold a Cursor line with ``config``, which is always."""
    return unsupported(config) or LINE_REFUSAL


def engine_environment(
    environment: Mapping[str, str], strategy: Strategy
) -> dict[str, str]:
    """The environment Cursor starts with, without Dromon's own variables and, for a
    subscription, without any credential variable. Raises ``EngineRefused`` when it
    would send Cursor's requests elsewhere."""
    present = [name for name in REFUSED_ENVIRONMENT if environment.get(name)]
    if present:
        raise EngineRefused(
            "the environment conflicts with a Cursor line: " + ", ".join(present)
        )
    subscription = strategy is Strategy.SUBSCRIPTION
    chosen = allowlisted(environment, (*BASE_ENVIRONMENT, *API_ENVIRONMENT))
    return {
        k: v
        for k, v in chosen.items()
        if not (subscription and (k in API_ENVIRONMENT or is_cloud_or_credential(k)))
    }


def check_workspace(workspace: Path, home: Path) -> None:
    """Refuse a workspace where Cursor would load configuration Dromon did not write,
    by the existence of files alone. **Cursor review, F2.** Cursor reads many files
    under a project's ``.cursor`` directory, among them permissions, MCP servers and
    their approvals, hooks, sandbox policies, settings, environments, worktree setup
    commands, rules, subagents and skills, so any ``.cursor`` in the working directory
    or a directory above it refuses the turn. The home directory's is Cursor's own,
    and only the entries ``HOME_ENTRIES`` names refuse it."""
    found: list[str] = []
    directory = Path(os.path.realpath(workspace))
    own = Path(os.path.realpath(home))
    for candidate in (directory, *directory.parents):
        if candidate != own and os.path.lexists(candidate / ".cursor"):
            found.append(str(candidate / ".cursor"))
    found.extend(
        str(own / ".cursor" / name)
        for name in HOME_ENTRIES
        if os.path.lexists(own / ".cursor" / name)
    )
    if found:
        raise EngineRefused(
            "Cursor would load configuration Dromon did not write: "
            + ", ".join(sorted(set(found)))
        )


def cli_config(tools: Sequence[str]) -> dict[str, object]:
    """The ``cli-config.json`` of the turn's configuration directory. Only Dromon's
    own tools are allowed, each by exact name."""
    return {
        "version": 1,
        "permissions": {
            "allow": [f"Mcp({MCP_SERVER_NAME}:{tool})" for tool in tools],
            "deny": list(DENIED),
        },
        "approvalMode": "allowlist",
        "sandbox": {"mode": "enabled"},
    }


def plugin_files(bridge: Sequence[str]) -> dict[str, dict[str, object]]:
    """The files of Dromon's plugin, by path relative to the plugin directory. The
    token is a reference Cursor fills in from its environment."""
    return {
        ".cursor-plugin/plugin.json": {"name": PLUGIN_NAME},
        "mcp.json": {
            "mcpServers": {
                MCP_SERVER_NAME: {
                    "command": bridge[0],
                    "args": list(bridge[1:]),
                    "env": {"DROMON_LINE_TOKEN": "${env:DROMON_LINE_TOKEN}"},
                }
            }
        },
    }


def write_configuration(
    directory: Path, tools: Sequence[str], bridge: Sequence[str]
) -> Path | None:
    """Write the turn's configuration into ``directory`` and return the plugin
    directory, or ``None`` when the line has no tools to serve."""
    (directory / "cli-config.json").write_text(json.dumps(cli_config(tools)))
    if not tools or not bridge:
        return None
    plugin = directory / "plugin"
    for relative, document in plugin_files(bridge).items():
        path = plugin / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
    return plugin


def cursor_arguments(
    model: str,
    *,
    workspace: Path,
    plugin: Path | None,
    session_id: str | None,
    prompt: str,
) -> list[str]:
    """The ``cursor-agent`` arguments for one turn, with the prompt after ``--``.
    Never ``--force``, ``--yolo``, ``--approve-mcps``, ``--api-key``, ``--endpoint`` or
    ``--header``, by the operator's decision of 2026-10-26. Without
    ``--approve-mcps`` a plugin's server may stay unapproved, which only a daemon line
    would need, and a daemon refuses Cursor lines."""
    arguments = [
        "--print",
        "--output-format",
        "stream-json",
        "--model",
        model,
        "--workspace",
        str(workspace),
        "--trust",
        "--sandbox",
        "enabled",
    ]
    if plugin is not None:
        arguments += ["--plugin-dir", str(plugin)]
    if session_id is not None:
        arguments += ["--resume", session_id]
    return [*arguments, "--", prompt]


def tool_of(tool_call: Mapping[str, Any]) -> tuple[str, Mapping[str, Any]]:
    """The kind of a ``tool_call`` and its value, in either shape Cursor may write,
    ``{"KIND": {...}}`` as its documentation shows or ``{"tool": {"case": KIND,
    "value": {...}}}`` as its message objects hold."""
    inner = tool_call.get("tool")
    if isinstance(inner, dict):
        case = cast(dict[str, Any], inner)
        value = case.get("value")
        return str(case.get("case") or ""), (
            cast(dict[str, Any], value) if isinstance(value, dict) else {}
        )
    for kind, value in tool_call.items():
        if kind.endswith("ToolCall") or kind == "function":
            return kind, cast(dict[str, Any], value) if isinstance(value, dict) else {}
    return "", {}


@dataclass(slots=True)
class _Turn:
    text: list[str] = field(default_factory=list[str])
    tool_uses: list[str] = field(default_factory=list[str])
    interrupted_for: str | None = None
    refusal: str | None = None
    credential: Credential | None = None
    session_id: str | None = None
    result: dict[str, Any] | None = None
    stopped: bool = False
    thinking: bool = False


class CursorLine:
    """One session line on the Cursor Agent engine, with its driver."""

    def __init__(
        self,
        driver: Driver,
        cwd: Path,
        *,
        config: LineConfig = DEFAULT_CONFIG,
        mcp_tools: Sequence[SdkMcpTool[Any]] = (),
        session_id: str | None = None,
        bridge: Sequence[str] = (),
        line_token: str | None = None,
        environment: Mapping[str, str] | None = None,
        home: Path | None = None,
        connection_factory: ConnectionFactory | None = None,
        observe: Observer = ignore,
        mark: str | None = None,
        git_ssh: GitSshAccess | None = None,
        partial_isolation: bool = False,
        turn_seconds: float = TURN_SECONDS,
    ) -> None:
        if driver.settings.engine != ENGINE_NAME:
            raise EngineRefused(
                f"the driver for {driver.settings.model} is written for the"
                f" {driver.settings.engine} engine, not {ENGINE_NAME}"
            )
        self.driver = driver
        self.cwd = cwd
        self.config = config
        self.bridge = tuple(bridge)
        self.line_token = line_token
        reachable = bool(self.bridge) and line_token is not None
        self.tool_names = tuple(sorted(tool.name for tool in mcp_tools if reachable))
        self.own_tools = frozenset(
            f"mcp__{MCP_SERVER_NAME}__{name}" for name in self.tool_names
        )
        self.session_id = session_id
        self._environment = os.environ if environment is None else environment
        self._home = Path.home() if home is None else home
        self._connect = connection_factory or json_lines_factory(
            COMMAND, keep_input=False
        )
        self._observe = observe
        self.mark = mark
        self.git_ssh = git_ssh
        """The agent and pinned hosts of the project's ``[git_ssh]`` table, if the
        line was given them."""
        """The process mark every process of this line inherits, if issued."""
        self.partial_isolation = partial_isolation
        """Whether the operator accepts that Cursor's uncovered tools are refused only
        after they start. Until then every turn is refused."""
        self.turn_seconds = turn_seconds
        """How long a turn may run before Cursor is stopped and the turn refused."""
        self._connection: Connection | None = None

    async def run_turn(self, prompt: str) -> TurnReport:
        """Run one turn for ``prompt`` and return what happened."""
        if not self.partial_isolation:
            raise EngineRefused(ISOLATION_REFUSAL)
        reason = unsupported(self.config)
        if reason is not None:
            raise EngineRefused(reason)
        settings = self.driver.settings
        if self.config.effort not in (None, settings.effort):
            raise EngineRefused(
                "Cursor names a model's effort in the model itself, so a Cursor line"
                " takes its driver's effort"
            )
        if self.session_id is not None:
            raise EngineRefused(RESUME_REFUSAL)
        environment = engine_environment(self._environment, settings.strategy)
        environment.update(gitssh.environment(self.git_ssh))
        check_workspace(self.cwd, self._home)
        if self.mark is not None:
            environment[MARK_VARIABLE] = self.mark
        environment[STOP_POLICY_VARIABLE] = self.config.stop_policy.value
        if self.tool_names and self.line_token is not None:
            environment["DROMON_LINE_TOKEN"] = self.line_token
        # Every turn opens a new chat, so it carries the line's instructions.
        instructions = read_instructions(self.config.instructions, self.cwd)
        text = f"{instructions}\n\n{prompt}" if instructions else prompt
        turn = _Turn()
        with tempfile.TemporaryDirectory(prefix="dromon-cursor-") as directory:
            plugin = write_configuration(Path(directory), self.tool_names, self.bridge)
            environment["CURSOR_CONFIG_DIR"] = directory
            # Cursor review, F7. The data directory holds per-project data and
            # approvals Dromon has not read, so the turn has an empty one of its own.
            data = Path(directory) / "data"
            data.mkdir()
            environment["CURSOR_DATA_DIR"] = str(data)
            arguments = cursor_arguments(
                harness_id(settings.model),
                workspace=self.cwd,
                plugin=plugin,
                session_id=self.session_id,
                prompt=text,
            )
            async with self._connect(arguments, self.cwd, environment) as connection:
                self._connection = connection
                try:
                    await self._converse(turn)
                finally:
                    self._connection = None
        return self._report(turn)

    def _report(self, turn: _Turn) -> TurnReport:
        if turn.refusal is not None:
            raise EngineRefused(turn.refusal)
        if turn.credential is None:
            raise EngineRefused("the engine ended before it reported its credential")
        if turn.interrupted_for is not None:
            subtype = "interrupted"
        elif turn.result is None:
            raise EngineRefused("the engine ended without a result")
        elif turn.result.get("subtype") == "success" and not turn.result.get(
            "is_error"
        ):
            subtype = "success"
        else:
            subtype = "error"
        if turn.session_id is not None:
            self.session_id = turn.session_id
        self.driver.step(Event("result", subtype, 1))
        return TurnReport(
            subtype=subtype,
            num_turns=1,
            text="".join(turn.text),
            tool_uses=tuple(turn.tool_uses),
            interrupted_for=turn.interrupted_for,
            credential=turn.credential,
            session_id=self.session_id or "",
            capabilities=None,
            cost_estimate_usd=None,
        )

    # The stream.

    def _open(self) -> Connection:
        if self._connection is None:
            raise EngineRefused("the engine connection is not open")
        return self._connection

    async def _converse(self, turn: _Turn) -> None:
        """Read the stream until its result, its end, or the turn's deadline, after
        which Cursor is stopped and the stream read for at most ``ABORT_SECONDS``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.turn_seconds
        while turn.result is None:
            if turn.stopped:
                seconds = float(ABORT_SECONDS)
            else:
                seconds = max(0.0, deadline - loop.time())
            try:
                record = await asyncio.wait_for(self._open().receive(), seconds)
            except TimeoutError:
                if turn.stopped:
                    return
                await self._stop(
                    turn,
                    f"the turn passed its deadline of {self.turn_seconds:g} seconds",
                )
                continue
            if record is None:
                return
            await self._handle(record, turn)

    async def _handle(self, record: dict[str, Any], turn: _Turn) -> None:
        """One event. Records are read and dropped, never stored, since a completed
        shell call carries a snapshot of the engine's environment."""
        kind = record.get("type")
        if kind == "system" and record.get("subtype") == "init":
            await self._init(record, turn)
            return
        if turn.stopped:
            return
        if kind not in RECORD_TYPES:
            await self._stop(
                turn, f"the engine sent a record Dromon does not know: {kind!r}"
            )
            return
        if turn.credential is None:
            await self._stop(turn, f"the engine sent {kind!r} before its init event")
            return
        if kind == "thinking":
            if not turn.thinking:
                turn.thinking = True
                self._observe(ActivityKind.THINKING, "")
                await self._act(Event("thinking"), turn)
            return
        turn.thinking = False
        if kind == "assistant":
            await self._text(cast(dict[str, Any], record.get("message") or {}), turn)
        elif kind == "tool_call":
            await self._tool(
                cast(dict[str, Any], record.get("tool_call") or {}),
                turn,
                started=record.get("subtype") == "started",
                completed=record.get("subtype") == "completed",
            )
        elif kind == "result":
            turn.result = {
                "subtype": record.get("subtype"),
                "is_error": record.get("is_error"),
            }
            reported = record.get("session_id")
            if isinstance(reported, str) and SESSION_PATTERN.fullmatch(reported):
                turn.session_id = turn.session_id or reported

    async def _init(self, record: dict[str, Any], turn: _Turn) -> None:
        if turn.credential is not None:
            await self._stop(turn, "the engine reported a second init event")
            return
        source = record.get("apiKeySource")
        credential = CREDENTIALS.get(source) if isinstance(source, str) else None
        if credential is None:
            await self._stop(turn, f"the engine reports the key source {source!r}")
            return
        turn.credential = credential
        verdict = self.driver.step(Event("credential", credential.value))
        if verdict.tag is ActionTag.REFUSE:
            await self._stop(
                turn, f"the {verdict.text} driver refuses the {credential} credential"
            )
            return
        model = record.get("model")
        name = model if isinstance(model, str) else ""
        verdict = self.driver.step(Event("init", name))
        if verdict.tag is ActionTag.REFUSE:
            await self._stop(
                turn, f"the driver for {verdict.text} refuses the engine model {name!r}"
            )
            return
        reported = record.get("session_id")
        session = reported if isinstance(reported, str) else None
        # Unreached while a resume is refused, re-review N1, and kept for when one
        # is permitted.
        if self.session_id is not None and session != self.session_id:
            await self._stop(
                turn, f"the engine opened the chat {session!r}, not {self.session_id}"
            )
            return
        if session is not None and SESSION_PATTERN.fullmatch(session):
            turn.session_id = session
        self.driver.step(Event("turn_start"))

    async def _text(self, message: dict[str, Any], turn: _Turn) -> None:
        for block in cast(list[object], message.get("content") or []):
            if not isinstance(block, dict):
                continue
            entry = cast(dict[str, Any], block)
            if entry.get("type") != "text":
                continue
            text = str(entry.get("text") or "")
            turn.text.append(text)
            self._observe(ActivityKind.TEXT, text, chunk=True)
            await self._act(Event("text", "", len(text)), turn)

    async def _tool(
        self,
        tool_call: dict[str, Any],
        turn: _Turn,
        *,
        started: bool,
        completed: bool = False,
    ) -> None:
        """A tool call. Every subtype is checked, so a tool reported only when it
        completes is still refused, and only a ``started`` one is counted. A
        ``completed`` one is the safe point before the next model call that priority
        mail takes, as ``dromon.priority`` describes."""
        kind, value = tool_of(tool_call)
        name = kind or "an unnamed tool"
        if kind == "mcpToolCall":
            raw = value.get("args")
            arguments = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
            provider = str(arguments.get("providerIdentifier") or "")
            name = f"mcp__{provider}__{arguments.get('toolName') or ''}"
        if name not in self.own_tools:
            await self._stop(
                turn, f"the engine used a tool the line was not given: {name}"
            )
            return
        if not started:
            if completed:
                await self._act(Event(TOOL_RESULT), turn)
            return
        turn.tool_uses.append(name)
        self._observe(ActivityKind.TOOL, name)
        await self._act(Event("tool_use", name), turn)

    async def _act(self, event: Event, turn: _Turn) -> None:
        action = self.driver.step(event)
        if action.tag is ActionTag.INTERRUPT and turn.interrupted_for is None:
            turn.interrupted_for = action.text
            await self._stop(turn, None)

    async def _stop(self, turn: _Turn, refusal: str | None) -> None:
        if refusal is not None and turn.refusal is None:
            turn.refusal = refusal
        if turn.stopped:
            return
        turn.stopped = True
        await self._open().stop()
