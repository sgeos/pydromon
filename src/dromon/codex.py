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

Kept sessions. With ``keep_session`` the app-server stays up between turns, so its
background terminals outlive a turn, and ``list_background`` and ``stop_background``
reach them. A pump then reads every message and routes responses, every check above
runs again at each turn on the live connection, and any change to what the app-server
was started with opens a new one. ``docs/protocol/CODEX.md`` is normative.

Porting note. The protocol is JSON lines over a pipe, which the Rust port can speak
unchanged. Without a kept session the adapter is sequential: it reads one message at a
time and answers each server request before reading on.
"""

import asyncio
import stat
import contextlib
import hashlib
import json
import os
import sys
import tempfile
import re
import shutil
import subprocess
import time
from collections.abc import (
    AsyncGenerator,
    Awaitable,
    Callable,
    Coroutine,
    Mapping,
    Sequence,
)
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, cast

from claude_agent_sdk import SdkMcpTool

from dromon import __version__
from dromon.activity import ActivityKind, Observer, ignore
from dromon.driver import ActionTag, Credential, Driver, Event, Strategy
from dromon.engine import (
    COMPACTION_REQUESTED,
    DIED_NOTE,
    CompactionRequest,
    MAX_CALLS,
    BackgroundEnd,
    add_usage,
    compaction_note,
    ignore_compaction,
    ignore_end,
    ignore_estimate,
    no_context_line,
    INTERRUPT_TIMEOUT_SECONDS,
    KEEP_POLL_SECONDS,
    EngineRefused,
    TurnReport,
    closed_note,
    read_instructions,
)
from dromon.processes import MARK_VARIABLE
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

LIST_TERMINALS: Final = "thread/backgroundTerminals/list"
TERMINATE_TERMINAL: Final = "thread/backgroundTerminals/terminate"
"""The app-server's methods for a thread's background terminals. **Fact, read from the
installed Codex 0.164.0 binary on 2026-10-26.** They exist beside
``thread/backgroundTerminals/clean``. A listed terminal has seven fields, of which
``itemId``, ``processId``, ``osPid``, ``cpuPercent`` and ``rssKb`` were read. The list
takes three parameters and termination two, whose names beyond ``threadId`` and
``processId`` are an inference. Not run, since a line may not start Codex."""

MAX_TERMINALS: Final = 64

TERMINAL_POLL_SECONDS: Final = 15.0
"""How often, between turns, a kept connection lists the line's own terminals to
notice one that ended. **Fact, read from the installed binary.** The app-server was
not found to report a terminal's end, so it is polled. It is polled only while the
line has a listed terminal, at most once in this many seconds."""

MAX_POLL_FAILURES: Final = 3

MAX_OWN_COMMANDS: Final = 1024
"""How many command identifiers a kept connection remembers as the line's own."""
"""The most background terminals a listing returns."""

REQUEST_TIMEOUT_SECONDS: Final = 30.0

TOKEN_USAGE_UPDATED: Final = "thread/tokenUsage/updated"  # noqa: S105
COMPACT_START: Final = "thread/compact/start"
THREAD_COMPACTED: Final = "thread/compacted"
COMPACTION_ITEM: Final = "contextCompaction"
"""The app-server's token usage notice, compaction request, compaction notice and
compaction item. **Facts, observed live on Codex 0.164.0 on 2026-10-26.** A token
usage notice carries ``threadId``, ``turnId`` and ``tokenUsage``, whose ``total``
and ``last`` give ``totalTokens``, ``inputTokens``, ``cachedInputTokens``,
``cacheWriteInputTokens``, ``outputTokens`` and ``reasoningOutputTokens``, with
``modelContextWindow`` beside them. ``totalTokens`` was ``inputTokens`` plus
``outputTokens``, so the input count includes ``cachedInputTokens``.
``thread/compact/start`` answers at once, and the engine then starts a turn of its
own, whose ``contextCompaction`` item starts and completes before that turn
completes. That build never sent ``thread/compacted``, whose name its binary
holds, as read with ``strings`` and not by running it, so
the item's completion is the compaction's report, and the notice is still accepted
from another build. The
compaction left ``total`` unchanged and gave a ``last`` of ``totalTokens`` alone,
larger than the context before it, with every other count zero, which is not an
engine call and is not read as one. The next call's input was about the context
before. **Inference, not observed.** The thread was too short to shrink.
**Inference.** ``last.totalTokens`` of a
real call is the context the next call re-reads. No cost in money is reported
anywhere, which matches a plan that charges none per call."""

COMPACT_TIMEOUT_SECONDS: Final = 600.0
"""How long a compaction may take before the turn is refused. It is waited for until
its turn completes, as observed, or until a compaction is reported outside any turn,
which a build that sent only the notice might do."""

CODEX_USAGE_KEYS: Final = {
    "totalTokens": "total_tokens",
    "inputTokens": "input_tokens",
    "cachedInputTokens": "cached_input_tokens",
    "cacheWriteInputTokens": "cache_write_input_tokens",
    "outputTokens": "output_tokens",
    "reasoningOutputTokens": "reasoning_output_tokens",
}

CODEX_USAGE_NOTE: Final = (
    "the engine's tokens for this turn, the difference of the thread's running totals,"
    " and per call. Its input_tokens include its cached_input_tokens, since its"
    " total_tokens were observed to be its input_tokens and output_tokens alone. A"
    " compaction adds nothing to the running totals. It reports no cost"
)

CODEX_SELF_STARTED_NOTE: Final = (
    "Dromon note: between your turns the engine began a turn by itself. Only Dromon"
    " starts turns, so it was interrupted, and its tool calls were refused."
)


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
    if config.plugins:
        return "Codex lines do not load Claude Code plugins"
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
    chosen = (
        dict(environment)
        if strategy is not Strategy.SUBSCRIPTION
        else {k: v for k, v in environment.items() if k not in API_ENVIRONMENT}
    )
    if "XDG_CACHE_HOME" not in chosen:
        chosen["XDG_CACHE_HOME"] = ensure_cache_home(default_cache_home())
    return chosen


def default_cache_home() -> str:
    """A cache directory under the system temporary directory, one per user, which
    Codex's workspace profile leaves writable, for tools that honour
    ``XDG_CACHE_HOME``. A cold cache still needs the network, which a Codex line
    lacks."""
    return os.path.join(tempfile.gettempdir(), f"dromon-cache-{os.getuid()}")


def ensure_cache_home(path: str) -> str:
    """Create the default cache directory with mode 0700 if it is missing, and return
    it. Raises ``EngineRefused`` for one that is a link, not a directory, not owned by
    the daemon's user, or writable by its group or others, since on a shared temporary
    directory another user could have made it first and planted cache entries."""
    with contextlib.suppress(FileExistsError):
        os.mkdir(path, 0o700)
    try:
        found = os.lstat(path)
    except OSError as error:
        raise EngineRefused(
            f"cannot use the cache directory {path}: {error}"
        ) from error
    if not stat.S_ISDIR(found.st_mode):
        raise EngineRefused(f"the cache directory {path} is not a directory")
    if found.st_uid != os.getuid():
        raise EngineRefused(f"the cache directory {path} belongs to another user")
    if found.st_mode & 0o022:
        raise EngineRefused(
            f"the cache directory {path} is writable by its group or others"
        )
    return path


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


SESSION_SEPARATOR: Final = ":"
"""Joins a Codex thread identifier and the fingerprint of the tools it started with,
in the engine session Dromon stores. A thread identifier holds no colon."""

TOOLS_CHANGED_NOTE: Final = (
    "Dromon's tools for this line changed since your Codex thread started, and a"
    " resumed thread keeps the tools it started with, so this turn starts a new thread."
    " The earlier thread's context is not available in it. Your mailbox, files and"
    " reports are unchanged."
)


def split_session(session: str | None) -> tuple[str | None, str | None]:
    """The thread to resume and its tools' fingerprint, from a stored session. A
    session without a fingerprint, as stored before fingerprints existed or adopted
    from elsewhere, has none, so its tools count as changed."""
    if session is None:
        return None, None
    thread, separator, fingerprint = session.rpartition(SESSION_SEPARATOR)
    if not separator:
        return session, None
    return thread, fingerprint


def server_arguments(
    servers: Sequence[str],
    config: LineConfig,
    protected: Sequence[Path] = (),
    mark: str | None = None,
) -> list[str]:
    """The ``codex app-server`` arguments that isolate a line configured as ``config``
    from the operator's ``servers``, from every tool it was not given, and from the
    ``protected`` directories, and that give the line's shell commands its process
    ``mark``.

    The mark goes through Codex's ``shell_environment_policy.set``, which applies to
    the commands Codex runs and not to Codex's own processes, so the app-server and its
    helpers never carry it and a line cannot list or stop them."""
    arguments = ["app-server"]
    if mark is not None:
        arguments += [
            "-c",
            f"shell_environment_policy.set.{MARK_VARIABLE}={_toml_string(mark)}",
        ]
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


def identifier(value: object) -> str | None:
    """An engine identifier as text, whether the engine sent text or a whole number,
    or ``None`` for anything else. A listing and an item may differ in how they send
    the same identifier, which round 3 matched only as text."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value and len(value) <= 128:
        return value
    return None


class _RpcError(Exception):
    pass


def breakdown(value: object) -> dict[str, int]:
    """A token usage breakdown's whole-number counts, under Dromon's names."""
    if not isinstance(value, dict):
        return {}
    fields_ = cast(dict[str, object], value)
    return {
        name: count
        for raw, name in CODEX_USAGE_KEYS.items()
        if type(count := fields_.get(raw)) is int and count >= 0
    }


@dataclass(slots=True)
class _Turn:
    text: list[str] = field(default_factory=list[str])
    tool_uses: list[str] = field(default_factory=list[str])
    interrupted_for: str | None = None
    refusal: str | None = None
    turn_id: str | None = None
    status: str = ""
    tools_changed: bool = False
    """Whether this turn started a new thread because Dromon's tools changed."""
    calls: list[dict[str, int]] = field(default_factory=list[dict[str, int]])
    """Each engine call's tokens, the latest ``MAX_CALLS``."""
    first_total: dict[str, int] | None = None
    """The thread's running totals before the turn's first call."""
    last_total: dict[str, int] | None = None
    compacted: bool = False
    """Whether the engine reported a compaction of the thread during the turn."""
    estimate_for: dict[str, int | str | None] | None = None
    """The compaction this turn began with, while its size after is unknown and the
    turn's first engine call has not yet estimated it."""


class _Routed:
    """A kept app-server connection. A pump reads every message. A response to a
    request made outside the turn's own loop, such as a background terminal listing
    asked for by a tool during the turn, resolves that request's future, so the turn
    cannot deadlock on it. During a turn every other message goes to the turn, which
    reads it through ``receive``, and between turns ``between`` handles it."""

    def __init__(
        self,
        inner: Connection,
        between: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.inner = inner
        self._writing = asyncio.Lock()
        """Writes come from the turn, the pump, a tool's request and an interrupt
        task, so each message is written whole under this lock."""
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.active = False
        self.alive = True
        self._between = between

    async def send(self, message: Mapping[str, object]) -> None:
        async with self._writing:
            await self.inner.send(message)

    async def receive(self) -> dict[str, Any] | None:
        return await self.queue.get()

    async def pump(self) -> None:
        try:
            while True:
                message = await self.inner.receive()
                if message is None:
                    return
                request_id = message.get("id")
                if (
                    "method" not in message
                    and isinstance(request_id, int)
                    and request_id in self.pending
                ):
                    future = self.pending.pop(request_id)
                    if not future.done():
                        future.set_result(message)
                elif self.active:
                    self.queue.put_nowait(message)
                else:
                    await self._between(message)
        except Exception:  # noqa: BLE001, S110
            # The engine's process or its pipe ended, which marks the connection
            # dead below, and the next turn starts a new one.
            pass
        finally:
            self.alive = False
            self.queue.put_nowait(None)
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(
                        EngineRefused("the engine closed its connection")
                    )
            self.pending.clear()


@dataclass(slots=True)
class _Kept:
    stack: AsyncExitStack
    routed: _Routed
    fingerprint: str
    last_end: float
    pump: "asyncio.Task[None] | None" = None
    watch: "asyncio.Task[None] | None" = None
    started: bool = False
    """Whether the connection has been initialized, which only its first turn does."""
    terminals: list[dict[str, object]] = field(default_factory=list[dict[str, object]])
    """The line's own background terminals in the last listing. The one source of
    both ``list_background`` and the status, and of the ceiling."""
    listed_at: float | None = None
    """When ``terminals`` was listed, on the monotonic clock."""
    engine_listed: int = 0
    """How many terminals the engine listed in all, the line's or not, for
    diagnosis."""
    stopped: set[str] = field(default_factory=set[str])
    """Terminals the line stopped with ``stop_background``, whose end needs no note."""
    finished: dict[str, tuple[int | None, str | None]] = field(
        default_factory=dict[str, tuple[int | None, str | None]]
    )
    """The exit code and output of each of the line's commands the engine reported
    completed, by process and item identifier, bounded with ``own``."""
    item_of: dict[str, str] = field(default_factory=dict[str, str])
    """Each listed terminal's item identifier, by process identifier."""
    waiting: dict[str, tuple[str, float]] = field(
        default_factory=dict[str, tuple[str, float]]
    )
    """Ends found by a listing that wait for the engine's completion report, with
    their command and when they were found."""
    poll_failures: int = 0
    """Consecutive failed listings between turns. Polling stops after
    ``MAX_POLL_FAILURES`` until the next turn ends."""
    commands: dict[str, str] = field(default_factory=dict[str, str])
    """The command each of the line's own command executions ran, by its process
    and item identifiers, so a terminal is named even when a listing gives no
    command."""
    own: dict[str, None] = field(default_factory=dict[str, None])
    """The process and item identifiers of the commands the line's own turns ran on
    this connection, from the items the engine reported, oldest first, bounded by
    ``MAX_OWN_COMMANDS``. A listed terminal is the line's only if it matches one, so
    the line never depends on the engine's scoping by thread."""


def turn_usage(turn: _Turn) -> dict[str, int] | None:
    """A turn's tokens, the difference of the thread's running totals across it, or
    ``None`` when the engine reported no call in it. A compaction's notice moves no
    total and is no call, so its tokens are unknown rather than measured zeros."""
    if not turn.calls or turn.last_total is None or turn.first_total is None:
        return None
    first = turn.first_total
    return {
        key: max(0, value - first.get(key, 0)) for key, value in turn.last_total.items()
    }


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
        mark: str | None = None,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = KEEP_POLL_SECONDS,
        task_ended: Callable[[BackgroundEnd], object] = ignore_end,
        compacted: Callable[
            [Mapping[str, int | str | None]], object
        ] = ignore_compaction,
        compaction_estimated: Callable[[int], object] = ignore_estimate,
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
        self.mark = mark
        """The process mark every process of this line inherits, if issued."""
        self._next_id = 0
        self._thread_id = ""
        self._connection: Connection | None = None
        self._clock = clock
        self._poll_seconds = poll_seconds
        self._task_ended = task_ended
        self._kept: _Kept | None = None
        self._notes: list[str] = []
        self._retired = False
        self._in_turn = False
        self._background: set[asyncio.Task[None]] = set()
        self._compacted = compacted
        self._estimated = compaction_estimated
        self._request: CompactionRequest | None = None
        """The compaction the next turn begins with, if any, which each turn takes
        as it starts, as on a Claude Code line."""
        self._context: int | None = None
        """The context the last engine call's usage gave, kept on this engine."""

    def request_compaction(
        self,
        reason: str = COMPACTION_REQUESTED,
        context_line: Callable[[], str | None] = no_context_line,
    ) -> None:
        """Compact the line's thread at the start of its next turn, as a Claude Code
        line's ``request_compaction`` does."""
        self._request = CompactionRequest(reason, context_line)

    async def _compact_thread(
        self, thread_id: str, turn: _Turn, request: CompactionRequest
    ) -> tuple[str, tuple[dict[str, int | str | None], _Turn]]:
        """Compact the thread before the turn's prompt, after every check of the turn
        has passed, and return the note the prompt opens with and what was reported.
        The compaction's own messages go to a turn of their own, so its turn
        notices cannot end or name the line's turn, and its refusal ends the line's.
        It is waited for until the engine reports the compaction and any turn it
        started has completed, or until its turn completes without one, at most
        ``COMPACT_TIMEOUT_SECONDS``."""
        before = self._context
        work = _Turn()

        async def wait() -> None:
            await self._call(COMPACT_START, {"threadId": thread_id}, work)
            # Bounded by the timeout below.
            while not work.status and not (work.compacted and work.turn_id is None):
                await self._dispatch(await self._receive(), work)

        try:
            await asyncio.wait_for(wait(), COMPACT_TIMEOUT_SECONDS)
        except TimeoutError as error:
            raise EngineRefused(
                f"the engine did not finish compacting within {COMPACT_TIMEOUT_SECONDS}"
                " seconds"
            ) from error
        if work.refusal is not None and turn.refusal is None:
            turn.refusal = work.refusal
        after = work.calls[-1].get("total_tokens") if work.calls else None
        compaction: dict[str, int | str | None] = {
            "trigger": "manual" if work.compacted else None,
            "before_tokens": before if work.compacted else None,
            "after_tokens": after if work.compacted else None,
            "after_estimate_tokens": None,
        }
        if work.compacted:
            self._context = after
            if after is None:
                turn.estimate_for = compaction
            try:
                self._compacted(dict(compaction))
            except Exception as error:  # noqa: BLE001
                print(
                    f"dromond: a compaction could not be recorded: {error}",
                    file=sys.stderr,
                )
        try:
            context_line = request.context_line()
        except Exception as error:  # noqa: BLE001
            print(f"dromond: the context line failed: {error}", file=sys.stderr)
            context_line = None
        note = compaction_note(compaction, request.reason, context_line)
        return note, (compaction, work)

    async def run_turn(self, prompt: str) -> TurnReport:
        """Run one turn for ``prompt`` and return what happened. A check that
        refuses the turn also closes a kept connection."""
        request, self._request = self._request, None
        try:
            environment = engine_environment(
                self._environment, self.driver.settings.strategy
            )
            reason = unsupported(self.config)
            if reason is not None:
                raise EngineRefused(reason)
            arguments = server_arguments(
                self._list_servers(environment), self.config, self.protected, self.mark
            )
            instructions = read_instructions(self.config.instructions, self.cwd)
        except Exception:
            await self.close()
            raise
        if self.config.keep_session:
            return await self._kept_turn(
                prompt, arguments, environment, instructions, request
            )
        turn = _Turn()
        compaction: tuple[dict[str, int | str | None], _Turn] | None = None
        async with self._connect(arguments, self.cwd, environment) as connection:
            self._connection = connection
            try:
                credential = await self._start(turn)
                thread_id = await self._open_thread(turn)
                if turn.refusal is None:
                    await self._check_isolation(thread_id, turn)
                if turn.refusal is None and request is not None:
                    note, compaction = await self._compact_thread(
                        thread_id, turn, request
                    )
                    prompt = f"{note}\n\n{prompt}"
                if turn.refusal is None:
                    await self._run(thread_id, prompt, turn)
            except _RpcError as error:
                raise EngineRefused(f"the engine refused a request: {error}") from error
            finally:
                self._connection = None
        if turn.refusal is not None:
            raise EngineRefused(turn.refusal)
        return self._report(turn, credential, thread_id, compaction)

    def _report(
        self,
        turn: _Turn,
        credential: Credential,
        thread_id: str,
        compaction: tuple[dict[str, int | str | None], _Turn] | None = None,
    ) -> TurnReport:
        session = f"{thread_id}{SESSION_SEPARATOR}{self.tools_fingerprint()}"
        self.session_id = session
        subtype = "success" if turn.status == "completed" else turn.status
        self.driver.step(Event("result", subtype, 1))
        return TurnReport(
            subtype=subtype,
            num_turns=1,
            text="".join(turn.text),
            tool_uses=tuple(turn.tool_uses),
            interrupted_for=turn.interrupted_for,
            credential=credential,
            session_id=session,
            capabilities=None,
            cost_estimate_usd=None,
            usage=add_usage(
                None if compaction is None else turn_usage(compaction[1]),
                turn_usage(turn),
            ),
            calls=tuple(dict(call) for call in turn.calls),
            context_tokens=self._context,
            compaction=None if compaction is None else compaction[0],
            compaction_usage=None if compaction is None else turn_usage(compaction[1]),
            usage_note=CODEX_USAGE_NOTE,
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
        if method in ("item/started", "item/completed"):
            self._remember_command(
                cast(dict[str, Any], params.get("item") or {}),
                completed=method == "item/completed",
            )
        if method == "item/started":
            await self._item_started(
                cast(dict[str, Any], params.get("item") or {}), turn
            )
        elif method == "item/completed":
            item = cast(dict[str, Any], params.get("item") or {})
            if item.get("type") == COMPACTION_ITEM and self._this_thread(params):
                turn.compacted = True
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
        elif method == TOKEN_USAGE_UPDATED:
            self._usage_seen(params, turn)
        elif method == THREAD_COMPACTED and self._this_thread(params):
            turn.compacted = True

    def _this_thread(self, params: Mapping[str, Any]) -> bool:
        """Whether a notice is about the line's own thread, or names none."""
        thread = params.get("threadId")
        return thread is None or thread == self._thread_id

    def _usage_seen(self, params: Mapping[str, Any], turn: _Turn) -> None:
        """Note one engine call's tokens, the thread's running totals and the context
        the next call re-reads, from a token usage notice."""
        if not self._this_thread(params):
            return
        usage = params.get("tokenUsage")
        if not isinstance(usage, dict):
            return
        fields_ = cast(dict[str, object], usage)
        last = breakdown(fields_.get("last"))
        total = breakdown(fields_.get("total"))
        if not any(count for key, count in last.items() if key != "total_tokens"):
            # A compaction's notice gives a total alone, which no call made.
            last = {}
        if last:
            turn.calls.append(last)
            del turn.calls[:-MAX_CALLS]
            context = last.get("total_tokens")
            self._context = context if context else self._context
            self._estimate_after(turn, last)
        if total:
            if turn.first_total is None:
                turn.first_total = {
                    key: max(0, value - last.get(key, 0))
                    for key, value in total.items()
                }
            turn.last_total = total

    def _estimate_after(self, turn: _Turn, call: Mapping[str, int]) -> None:
        """Estimate the size a compaction left from the first engine call after it in
        the same turn, when the engine did not report the size, and report the
        estimate at once. It is the call's input, which holds the compacted thread
        and the turn's prompt, and so is an upper bound on the size after. Codex
        0.164.0 was observed live not to report the size after, so this is the only
        estimate a turn can give before its end."""
        compaction = turn.estimate_for
        if compaction is None:
            return
        tokens = call.get("input_tokens") or call.get("total_tokens")
        if not tokens:
            return
        turn.estimate_for = None
        compaction["after_estimate_tokens"] = tokens
        try:
            self._estimated(tokens)
        except Exception as error:  # noqa: BLE001
            print(
                f"dromond: a compaction estimate could not be recorded: {error}",
                file=sys.stderr,
            )

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
        return await self._credential(turn)

    async def _credential(self, turn: _Turn) -> Credential:
        """The account the engine reports, as a credential the driver accepts. A kept
        connection asks again at every turn."""
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
        resumable, fingerprint = split_session(self.session_id)
        if resumable is not None and fingerprint != self.tools_fingerprint():
            # thread/resume takes no dynamicTools in this Codex, so a thread keeps the
            # tools it was started with, and changed tools need a new thread.
            turn.tools_changed = True
            resumable = None
        if resumable is None:
            settings["dynamicTools"] = [
                dynamic_tool(t) for t in self.mcp_tools.values()
            ]
            result = await self._call("thread/start", settings, turn)
        else:
            result = await self._call(
                "thread/resume", {**settings, "threadId": resumable}, turn
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

    def tools_fingerprint(self) -> str:
        """A short digest of the names of Dromon's tools for this line, stored with
        the thread so a resumed thread can be checked against today's tools."""
        names = "\n".join(sorted(self.mcp_tools))
        return hashlib.sha256(names.encode("utf-8")).hexdigest()[:16]

    async def _run(self, thread_id: str, prompt: str, turn: _Turn) -> None:
        if turn.tools_changed:
            prompt = f"{TOOLS_CHANGED_NOTE}\n\n{prompt}"
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
        # The response names the line's own turn, so it is preferred to a turn
        # notice, which could be another turn's, such as a compaction's.
        turn.turn_id = (
            cast(str | None, cast(dict[str, Any], started.get("turn") or {}).get("id"))
            or turn.turn_id
        )
        while not turn.status:
            await self._dispatch(await self._receive(), turn)

    # A kept connection. Porting note: as for Claude Code lines, the pump and the
    # watch are asyncio tasks that share the connection's fields with the turn
    # without a lock, which is sound only because asyncio switches tasks at awaits.

    def fingerprint(
        self,
        arguments: Sequence[str],
        environment: Mapping[str, str],
        instructions: str,
    ) -> str:
        """A digest of everything a kept app-server was started and its thread opened
        with, which a turn compares before reusing it. The environment holds
        credentials, so only its digest is kept, in memory."""
        document = {
            "arguments": list(arguments),
            "environment": sorted(environment.items()),
            "cwd": str(self.cwd),
            "instructions": instructions,
            "tools": self.tools_fingerprint(),
            "model": self.driver.settings.model,
            "effort": self.config.effort or self.driver.settings.effort,
            "builtin_tools": list(self.config.builtin_tools),
        }
        text = json.dumps(document, sort_keys=True)
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def seconds_to_close(self) -> float | None:
        kept = self._kept
        if kept is None or not kept.routed.alive or self._in_turn:
            return None
        allowed = (
            self.config.keep_session_max_seconds
            if kept.terminals
            else self.config.keep_session_idle_seconds
        )
        return kept.last_end + allowed - self._clock()

    def kept_status(self) -> dict[str, object]:
        kept = self._kept
        alive = kept is not None and kept.routed.alive
        terminals = kept.terminals if kept is not None and alive else []
        return {
            "keep_session": self.config.keep_session,
            "kept": alive,
            "seconds_to_close": self.seconds_to_close(),
            "background_tasks": len(terminals),
            "tasks": [
                {"task_id": str(t.get("process_id")), "command": str(t.get("command"))}
                for t in terminals
            ],
            "tasks_listed_seconds_ago": (
                None
                if kept is None or not alive or kept.listed_at is None
                else round(self._clock() - kept.listed_at)
            ),
            "engine_listed": kept.engine_listed if kept is not None and alive else 0,
        }

    async def expire(self) -> bool:
        remaining = self.seconds_to_close()
        kept = self._kept
        if kept is None or remaining is None or remaining > 0:
            return False
        await self.close(
            "it reached keep_session_max_seconds with background terminals running"
            if kept.terminals
            else "it waited keep_session_idle_seconds without a turn"
        )
        return True

    async def retire(self) -> None:
        self._retired = True
        if not self._in_turn:
            await self.close()

    async def close(self, reason: str | None = None) -> None:
        """Close the kept app-server, which ends its background terminals. With
        ``reason`` the next prompt says why."""
        kept = self._kept
        if kept is None:
            return
        self._kept = None
        if reason is not None:
            self._notes.append(closed_note(reason, len(kept.terminals)))
        current = asyncio.current_task()
        helpers = [
            t for t in (kept.pump, kept.watch) if t is not None and t is not current
        ]
        for task in helpers:
            task.cancel()
        await asyncio.gather(*helpers, return_exceptions=True)
        with contextlib.suppress(Exception):
            await kept.stack.aclose()

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.get_running_loop().create_task(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _open_kept(
        self,
        arguments: list[str],
        environment: Mapping[str, str],
        fingerprint: str,
    ) -> _Kept:
        stack = AsyncExitStack()
        try:
            inner = await stack.enter_async_context(
                self._connect(arguments, self.cwd, environment)
            )
        except BaseException:
            await stack.aclose()
            raise
        kept = _Kept(stack, _Routed(inner, self._between), fingerprint, self._clock())
        loop = asyncio.get_running_loop()
        kept.pump = loop.create_task(kept.routed.pump())
        kept.watch = loop.create_task(self._watch(kept))
        self._kept = kept
        return kept

    async def _between(self, message: dict[str, Any]) -> None:
        """A message between turns. A request is refused, so no tool runs and no
        approval is given outside a turn, and a turn the engine began by itself is
        interrupted and noted."""
        kept = self._kept
        if kept is None:
            return
        method = message.get("method")
        if "id" in message and isinstance(method, str):
            answer: dict[str, object] = (
                {"id": message["id"], "result": {"decision": "decline"}}
                if method.endswith("/requestApproval")
                else {
                    "id": message["id"],
                    "error": {
                        "code": -32601,
                        "message": "Dromon has not started a turn",
                    },
                }
            )
            await kept.routed.send(answer)
            return
        if method == "item/completed":
            params = cast(dict[str, Any], message.get("params") or {})
            self._remember_command(
                cast(dict[str, Any], params.get("item") or {}), completed=True
            )
            return
        if method == "turn/started":
            started = cast(dict[str, Any], message.get("params") or {})
            turn_id = cast(dict[str, Any], started.get("turn") or {}).get("id")
            if CODEX_SELF_STARTED_NOTE not in self._notes:
                self._notes.append(CODEX_SELF_STARTED_NOTE)
            if not isinstance(turn_id, str) or not turn_id or not self._thread_id:
                # An interrupt needs the turn's identifier, so without one the only
                # sure way to stop the turn is to end the app-server.
                await self.close("the engine began a turn by itself without naming it")
                return
            self._next_id += 1
            await kept.routed.send(
                {
                    "id": self._next_id,
                    "method": "turn/interrupt",
                    "params": {"threadId": self._thread_id, "turnId": turn_id},
                }
            )

    async def _watch(self, kept: _Kept) -> None:
        while kept is self._kept and kept.routed.alive:
            await asyncio.sleep(self._poll_seconds)
            if kept is not self._kept:
                return
            try:
                if await self.expire():
                    return
                await self._poll_terminals(kept)
            except Exception as error:  # noqa: BLE001
                print(
                    f"dromond: the watch of a kept session failed and goes on: {error}",
                    file=sys.stderr,
                )

    async def _poll_terminals(self, kept: _Kept) -> None:
        """Between turns, list the line's own terminals again when one was listed
        and the last listing is ``TERMINAL_POLL_SECONDS`` old, so that an end wakes
        the line. Bounded by that interval and by ``MAX_POLL_FAILURES``."""
        if kept.waiting and not self._in_turn:
            self._post_ends(kept)
        if (
            self._in_turn
            or not kept.terminals
            or kept.poll_failures >= MAX_POLL_FAILURES
        ):
            return
        if (
            kept.listed_at is not None
            and self._clock() - kept.listed_at < TERMINAL_POLL_SECONDS
        ):
            return
        try:
            await self.list_background()
        except EngineRefused:
            kept.poll_failures += 1
            return
        kept.poll_failures = 0

    async def request(
        self, method: str, params: Mapping[str, object]
    ) -> dict[str, Any]:
        """Call ``method`` on the kept app-server, during a turn or between turns,
        and return its result. Raises ``EngineRefused``."""
        kept = self._kept
        if kept is None or not kept.routed.alive or not kept.started:
            raise EngineRefused("this line keeps no engine session now")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        kept.routed.pending[request_id] = future
        try:
            await kept.routed.send(
                {"id": request_id, "method": method, "params": params}
            )
            message = await asyncio.wait_for(future, REQUEST_TIMEOUT_SECONDS)
        except TimeoutError as error:
            raise EngineRefused(f"the engine did not answer {method}") from error
        finally:
            kept.routed.pending.pop(request_id, None)
        if "error" in message:
            raise EngineRefused(f"the engine refused {method}: {message['error']}")
        result: object = message.get("result")
        return cast(dict[str, Any], result) if isinstance(result, dict) else {}

    def _remember_command(
        self, item: dict[str, Any], *, completed: bool = False
    ) -> None:
        """Note a command the line's own turn ran as the line's, by its process and
        item identifiers, on a kept connection, and, when the engine reports it
        completed, its exit code and output. A completion, in a turn or between
        turns, is the engine's report of an item the line's turn started, so it is
        noted only when its item identifier is already the line's, which also makes
        a process identifier first seen on the completion the line's. A process
        identifier alone does not suffice, since it names a process, not the item.
        Dromon does not rely on the engine scoping items to the thread."""
        kept = self._kept
        if kept is None or item.get("type") != "commandExecution":
            return
        command = item.get("command")
        names = [
            name
            for name in (identifier(item.get("processId")), identifier(item.get("id")))
            if name is not None
        ]
        item_id = identifier(item.get("id"))
        if completed and (item_id is None or item_id not in kept.own):
            return
        for value in names:
            if value not in kept.own:
                self._evict(kept)
            kept.own[value] = None
            if isinstance(command, str) and command:
                kept.commands[value] = command[:200]
            if completed:
                code = item.get("exitCode")
                output = item.get("aggregatedOutput")
                kept.finished[value] = (
                    code if type(code) is int else None,
                    output if isinstance(output, str) else None,
                )

    def _evict(self, kept: _Kept) -> None:
        """Make room for one identifier at ``MAX_OWN_COMMANDS`` by forgetting the
        oldest ones, with their commands and completions, but never one that names a
        terminal still listed or an end still waiting to be noted. Bounded by one
        pass over the identifiers."""
        if len(kept.own) < MAX_OWN_COMMANDS:
            return
        keep = set(kept.waiting) | {str(t["process_id"]) for t in kept.terminals}
        keep |= {kept.item_of[name] for name in keep if name in kept.item_of}
        for name in list(kept.own):
            if len(kept.own) < MAX_OWN_COMMANDS:
                return
            if name in keep:
                continue
            del kept.own[name]
            kept.commands.pop(name, None)
            kept.finished.pop(name, None)

    async def list_background(self) -> list[dict[str, object]]:
        """The background terminals of the line's own thread. Raises
        ``EngineRefused``."""
        result = await self.request(LIST_TERMINALS, {"threadId": self._thread_id})
        # The listing's field name was not read, so its one list is taken.
        listed: list[object] = []
        for value in result.values():
            if isinstance(value, list):
                listed = cast(list[object], value)
                break
        terminals: list[dict[str, object]] = []
        for entry in listed[:MAX_TERMINALS]:
            if not isinstance(entry, dict):
                continue
            fields = cast(dict[str, object], entry)
            if not self._is_own(fields):
                continue
            command = next(
                (
                    fields[key]
                    for key in ("command", "commandLine", "cmd")
                    if isinstance(fields.get(key), str)
                ),
                self._own_command(fields),
            )
            item_id = identifier(fields.get("itemId"))
            process = identifier(fields.get("processId")) or ""
            if item_id is not None and self._kept is not None:
                self._kept.item_of[process] = item_id
            terminals.append(
                {
                    "process_id": process,
                    "os_pid": fields.get("osPid")
                    if isinstance(fields.get("osPid"), int)
                    else None,
                    "command": str(command)[:200],
                }
            )
        kept = self._kept
        if kept is not None:
            self._note_ends(kept, terminals)
            kept.terminals = terminals
            kept.listed_at = self._clock()
            kept.engine_listed = min(len(listed), MAX_TERMINALS)
        return terminals

    def _note_ends(self, kept: _Kept, terminals: list[dict[str, object]]) -> None:
        """Find, between turns, each of the line's terminals that the last listing
        held and this one does not, unless the line stopped it itself, and wait for
        its completion report, up to one polling interval, before noting it."""
        now = {str(t["process_id"]) for t in terminals}
        for gone in kept.terminals:
            process_id = str(gone["process_id"])
            if process_id in now:
                continue
            if process_id in kept.stopped:
                kept.stopped.discard(process_id)
                continue
            if self._in_turn:
                continue
            kept.waiting[process_id] = (str(gone.get("command") or ""), self._clock())
        self._post_ends(kept)

    def _post_ends(self, kept: _Kept) -> None:
        """Note each waiting end whose completion the engine reported, or that has
        waited ``TERMINAL_POLL_SECONDS``, with what is known of it."""
        for process_id, (command, found_at) in list(kept.waiting.items()):
            finished = kept.finished.get(process_id) or kept.finished.get(
                kept.item_of.get(process_id, "")
            )
            if finished is None and self._clock() - found_at < TERMINAL_POLL_SECONDS:
                continue
            del kept.waiting[process_id]
            code, output = finished or (None, None)
            try:
                self._task_ended(
                    BackgroundEnd(
                        process_id,
                        command,
                        "completed" if code is not None else "ended",
                        exit_code=code,
                        output=output,
                    )
                )
            except Exception as error:  # noqa: BLE001
                print(
                    f"dromond: a background terminal's end could not be noted: {error}",
                    file=sys.stderr,
                )

    def _own_command(self, fields: Mapping[str, object]) -> str:
        """The command the line's own item gave for a listed terminal, or empty."""
        kept = self._kept
        if kept is None:
            return ""
        for key in ("processId", "itemId"):
            name = identifier(fields.get(key))
            if name is not None and name in kept.commands:
                return kept.commands[name]
        return ""

    def _is_own(self, fields: Mapping[str, object]) -> bool:
        """Whether a listed terminal was started by the line's own turn on this
        connection, by its process or item identifier."""
        kept = self._kept
        if kept is None:
            return False
        names = {identifier(fields.get("processId")), identifier(fields.get("itemId"))}
        return any(name is not None and name in kept.own for name in names)

    async def stop_background(self, process_id: str) -> str:
        """Terminate one background terminal of the line's own thread, which must be
        in a fresh listing. Raises ``EngineRefused``."""
        if not process_id or len(process_id) > 128:
            raise EngineRefused("process_id must be text of 1 to 128 characters")
        listed = await self.list_background()
        if process_id not in {str(t["process_id"]) for t in listed}:
            raise EngineRefused(f"{process_id} is not one of your background terminals")
        if self._kept is not None:
            self._kept.stopped.add(process_id)
        await self.request(
            TERMINATE_TERMINAL, {"threadId": self._thread_id, "processId": process_id}
        )
        with contextlib.suppress(EngineRefused):
            await self.list_background()
        return f"terminated background terminal {process_id}"

    async def _kept_turn(
        self,
        prompt: str,
        arguments: list[str],
        environment: Mapping[str, str],
        instructions: str,
        request: CompactionRequest | None = None,
    ) -> TurnReport:
        """A turn on the kept app-server, opened anew when there is none, when it
        ended, or when what it was started with changed. On a reused connection every
        check of a new one runs again: the account, the thread's provider, model and
        permission profile through ``thread/resume`` of the loaded thread, whose
        overrides the engine ignores, and the MCP server and app listings."""
        fingerprint = self.fingerprint(arguments, environment, instructions)
        kept = self._kept
        if kept is not None:
            if not kept.routed.alive:
                await self.close()
                self._notes.append(DIED_NOTE)
            elif kept.fingerprint != fingerprint:
                await self.close(
                    "its configuration, instructions, tools or environment changed"
                )
        kept = self._kept or await self._open_kept(arguments, environment, fingerprint)
        if self._notes:
            prompt = "\n\n".join([*self._notes, prompt])
            self._notes.clear()
        turn = _Turn()
        compaction: tuple[dict[str, int | str | None], _Turn] | None = None
        kept.routed.active = True
        self._connection = kept.routed
        self._in_turn = True
        try:
            credential = await (
                self._credential(turn) if kept.started else self._start(turn)
            )
            kept.started = True
            thread_id = await self._open_thread(turn)
            if turn.refusal is None:
                await self._check_isolation(thread_id, turn)
            if turn.refusal is None and request is not None:
                note, compaction = await self._compact_thread(thread_id, turn, request)
                prompt = f"{note}\n\n{prompt}"
            if turn.refusal is None:
                await self._run(thread_id, prompt, turn)
        except asyncio.CancelledError:
            if turn.turn_id is not None and not turn.status:
                self._spawn(self._interrupt_kept(kept, turn.turn_id))
            raise
        except _RpcError as error:
            await self.close()
            raise EngineRefused(f"the engine refused a request: {error}") from error
        except Exception:
            died = not kept.routed.alive
            await self.close()
            if died:
                self._notes.append(DIED_NOTE)
            raise
        finally:
            self._in_turn = False
            self._connection = None
            kept.routed.active = False
            kept.last_end = self._clock()
            kept.poll_failures = 0
            while not kept.routed.queue.empty():
                leftover = kept.routed.queue.get_nowait()
                if leftover is not None:
                    await self._between(leftover)
            if self._retired:
                self._spawn(self.close())
        if turn.refusal is not None:
            await self.close()
            raise EngineRefused(turn.refusal)
        # A listing that fails leaves the count empty, so the shorter idle time
        # applies.
        with contextlib.suppress(EngineRefused):
            await self.list_background()
        return self._report(turn, credential, thread_id, compaction)

    async def _interrupt_kept(self, kept: _Kept, turn_id: str) -> None:
        """After an operator's interrupt, interrupt the engine's turn, and close the
        connection if that cannot be sent."""
        self._next_id += 1
        try:
            await asyncio.wait_for(
                kept.routed.send(
                    {
                        "id": self._next_id,
                        "method": "turn/interrupt",
                        "params": {"threadId": self._thread_id, "turnId": turn_id},
                    }
                ),
                INTERRUPT_TIMEOUT_SECONDS,
            )
        except Exception:  # noqa: BLE001
            if kept is self._kept:
                await self.close()
