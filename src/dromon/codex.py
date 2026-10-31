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
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any, Final, Protocol, cast

from claude_agent_sdk import SdkMcpTool

from dromon import __version__
from dromon.activity import ActivityKind, Observer, ignore
from dromon.driver import TOOL_RESULT, ActionTag, Credential, Driver, Event, Strategy
from dromon.engine import (
    COMPACTION_REQUESTED,
    DIED_NOTE,
    HALT_REASON,
    CompactionRequest,
    EnginePlanLimit,
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
from dromon import evidence, launcher, redact
from dromon.ceiling import (
    STOP_REASON,
    Ask,
    Ceiling,
    Choice,
    Settle,
    no_operator,
    nothing_to_settle,
)
from dromon.launcher import STOP_POLICY_VARIABLE, StopPolicy
from dromon.processes import MARK_VARIABLE
from dromon.security_paths import CREDENTIAL_PATHS, existing_real_paths
from dromon.engine_environment import (
    BASE_ENVIRONMENT,
    SHELL_VARIABLE,
    ShellSelectionError,
    allowlisted,
    selected_shell,
)
from dromon import gitssh
from dromon.gitssh import GitSshAccess
from dromon.imageaccess import VIEW_IMAGE, image_refusal
from dromon.lineconfig import DEFAULT_CONFIG, LineConfig, WorkdirAccess
from dromon.model import InvalidField, format_due
from dromon.workdir import git_directories

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
)
"""Codex features that bring tools or connectors a line was not given."""

IMAGE_FEATURE: Final = "view_image"
"""The feature behind Codex's tool that shows a local image to the model. **Round 11.**
It was in ``DISABLED_FEATURES``. It is now disabled unless the line has ``ViewImage``,
which enables it, and ``image_generation`` stays in ``DISABLED_FEATURES`` for every
line."""

IMAGE_ITEM: Final = "imageView"
"""The thread item Codex emits for a ``view_image`` call. **Fact**, the string is in the
Codex 0.164.0 binary beside ``imageGeneration``, read on 2026-10-31. **Inference**, not
observed live, that the item carries the image's file in a string field ``path``."""

SHELL_FEATURES: Final = ("shell_tool", "unified_exec")
"""The features behind Codex's shell, enabled only for a line given ``Bash``."""

SUPPORTED_BUILTINS: Final = frozenset({"Bash", "Edit", "Write", VIEW_IMAGE})
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
MAX_TERMINAL_PAGES: Final = 16
"""Bound a cursor walk and fail closed if Codex still has another page."""

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
OUTPUT_CLOSE_SECONDS: Final = 2.0
"""Bound the wait for a live app-server whose output pipe has closed."""

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
            "Codex lines support only the built-in tools Bash, Edit, Write and"
            " ViewImage, not " + (", ".join(extra))
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
    environment: Mapping[str, str], strategy: Strategy, *, require_shell: bool = False
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
    shell: str | None = None
    if require_shell:
        try:
            shell = selected_shell(environment)
        except ShellSelectionError as error:
            raise EngineRefused(f"Codex shell unavailable: {error}") from error
    chosen = allowlisted(
        environment, (*BASE_ENVIRONMENT, *API_ENVIRONMENT, "XDG_CACHE_HOME")
    )
    if shell is not None:
        chosen[SHELL_VARIABLE] = shell
    if strategy is Strategy.SUBSCRIPTION:
        chosen = {k: v for k, v in chosen.items() if k not in API_ENVIRONMENT}
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


def permission_profile(
    config: LineConfig,
    protected: Sequence[Path],
    write_protected: Sequence[Path] = (),
) -> str:
    """The ``dromon-line`` profile for ``config``, as an inline TOML table.

    The workspace is writable only with a write tool, ``.git`` is readable but not
    writable, each existing protected path is denied by its real path, and each
    validated operator-granted directory is writable.
    """
    workspace = (
        "write"
        if config.workdir_access is WorkdirAccess.WRITE
        and WRITE_TOOLS & set(config.builtin_tools)
        else "read"
    )
    scratch = ', "tmp"="write"' if config.workdir_access is WorkdirAccess.READ else ""
    granted = validate_writable_paths(
        config.writable_paths,
        (*protected, *write_protected),
        config.builtin_tools,
    )
    denied = existing_real_paths(
        [
            *protected,
            *(Path.home() / relative for relative in CREDENTIAL_PATHS),
        ]
    )
    entries = [
        f'":workspace_roots"={{"."="{workspace}", ".git"="read"{scratch}}}',
        *(f'{_toml_string(str(directory))}="deny"' for directory in denied),
        *(f'{_toml_string(str(directory))}="write"' for directory in granted),
    ]
    return f'{{extends=":workspace", filesystem={{{", ".join(entries)}}}}}'


def validate_writable_paths(
    configured: Sequence[str],
    protected: Sequence[Path],
    tools: Sequence[str],
) -> tuple[Path, ...]:
    """Validate the absolute directories an operator grants a Codex line to write.

    A grant may not overlap a protected directory or known credential path. The checks
    use real paths so a symbolic link cannot make an overlapping grant look separate.
    """
    if configured and not WRITE_TOOLS.intersection(tools):
        raise EngineRefused("writable_paths requires the Edit or Write tool")
    home = Path.home()
    credential_guards = tuple(
        Path(os.path.realpath(home / relative)) for relative in CREDENTIAL_PATHS
    )
    guards = (
        *existing_real_paths(list(protected)),
        *credential_guards,
        *existing_real_paths([home / relative for relative in CREDENTIAL_PATHS]),
    )
    result: list[Path] = []
    for entry in configured:
        path = Path(entry)
        if not path.is_absolute():
            raise EngineRefused(f"writable path {entry!r} must be absolute")
        try:
            real = path.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise EngineRefused(
                f"writable path {entry!r} does not exist: {error}"
            ) from error
        if not real.is_dir():
            raise EngineRefused(f"writable path {entry!r} is not a directory")
        for guard in guards:
            if real == guard or real in guard.parents or guard in real.parents:
                raise EngineRefused(
                    f"writable path {entry!r} overlaps a protected path {guard}"
                )
        result.append(real)
    return tuple(sorted(set(result)))


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
    variables: Mapping[str, str] | None = None,
    write_protected: Sequence[Path] = (),
) -> list[str]:
    """The ``codex app-server`` arguments that isolate a line configured as ``config``
    from the operator's ``servers``, from every tool it was not given, and from the
    ``protected`` directories, and that give the line's shell commands its process
    ``mark`` and the further ``variables``, such as ``dromon.gitssh.environment``
    gives.

    The mark goes through Codex's ``shell_environment_policy.set``, which applies to
    the commands Codex runs and not to Codex's own processes, so the app-server and its
    helpers never carry it and a line cannot list or stop them. The further variables
    go the same way, so Codex's own policy cannot drop them."""
    arguments = ["app-server"]
    if mark is not None:
        arguments += [
            "-c",
            f"shell_environment_policy.set.{MARK_VARIABLE}={_toml_string(mark)}",
        ]
    for name, value in sorted((variables or {}).items()):
        if BARE_KEY.fullmatch(name) is None:
            raise EngineRefused(f"the variable name {name!r} is not a bare TOML key")
        arguments += [
            "-c",
            f"shell_environment_policy.set.{name}={_toml_string(value)}",
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
    image = "enable" if VIEW_IMAGE in config.builtin_tools else "disable"
    arguments += [f"--{image}", IMAGE_FEATURE]
    shell = "enable" if "Bash" in config.builtin_tools else "disable"
    for feature in SHELL_FEATURES:
        arguments += [f"--{shell}", feature]
    arguments += ["-c", 'web_search="disabled"']
    # A bare key, since Codex's -c parser rejects a quoted one, with the profile's
    # quoted keys inside the inline table value.
    arguments += [
        "-c",
        f"permissions.{PROFILE_NAME}="
        f"{permission_profile(config, protected, write_protected)}",
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
            if self._process.returncode is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._process.wait(), OUTPUT_CLOSE_SECONDS)
            return None
        decoded: object = json.loads(line)
        if not isinstance(decoded, dict):
            raise EngineRefused("the engine sent a message that is not an object")
        record = cast(dict[str, Any], decoded)
        if self._recording is not None:
            self._recording.frame(evidence.IN, record)
        return record


def codex_path() -> str:
    found = shutil.which("codex")
    if found is None:
        raise EngineRefused("the codex command is not installed or not on PATH")
    return found


@asynccontextmanager
async def _app_server(
    arguments: list[str], cwd: Path, environment: Mapping[str, str]
) -> AsyncGenerator[Connection]:
    command, launched, policy = launcher.wrap([codex_path(), *arguments], environment)
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
        # The launcher ends the app-server and, by the line's stop policy, its group
        # and every descendant.
        await launcher.end(process, policy, close_input=False)
        await evidence.stop_draining(draining)


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


PLAN_LIMIT_CODES: Final = frozenset(
    {
        "plan_limit_reached",
        "plan_limit_exceeded",
        "usage_limit_reached",
        "usage_limit_exceeded",
    }
)
"""Inferred app-server limit codes, unverified against a live error."""


def _plan_limit(error: object) -> str | None:
    """Recognize a structured limit code or specific limit wording.

    **Inference, not observed live.** The words below are plausible app-server
    representations. A live error is needed to confirm the vocabulary. So the
    ``EnginePlanLimit`` raised from it holds the line and asks, and never moves a
    role, by the operator's decision of 2026-10-27.
    """
    if not isinstance(error, dict):
        return None
    fields = cast(dict[str, object], error)
    raw_code = fields.get("code")
    if isinstance(raw_code, str) and raw_code:
        if raw_code.strip().lower() not in PLAN_LIMIT_CODES:
            return None
        return _plan_limit_message(fields)
    raw_message = fields.get("message")
    if not isinstance(raw_message, str):
        return None
    message = " ".join(raw_message.lower().replace("_", " ").split())
    terms = (
        "plan limit",
        "usage limit",
        "reached your limit",
        "reached the usage limit",
        "quota exceeded",
    )
    if not any(term in message for term in terms):
        return None
    return _plan_limit_message(fields)


def _plan_limit_message(fields: Mapping[str, object]) -> str:
    """Format the limit and its structured reset time, where reported."""
    reset: object = None
    data = fields.get("data")
    if isinstance(data, dict):
        detail = cast(dict[str, object], data)
        reset = next(
            (
                detail[key]
                for key in (
                    "reset_at",
                    "resets_at",
                    "reset_time",
                    "resetsAt",
                    "resetTime",
                )
                if key in detail
            ),
            None,
        )
    if reset is None:
        reset = next(
            (
                fields[key]
                for key in (
                    "reset_at",
                    "resets_at",
                    "reset_time",
                    "resetsAt",
                    "resetTime",
                )
                if key in fields
            ),
            None,
        )
    suffix = f"; reset time {reset}" if isinstance(reset, (str, int, float)) else ""
    return f"Codex subscription plan limit reached{suffix}"


USAGE_LIMIT_EXCEEDED: Final = "usageLimitExceeded"
"""The ``codexErrorInfo`` of an error object at the end of a plan's credit, in camel
case. Observed 2026-10-27 by the operator, from a Codex app-server probe with no credit
left, in both the ``error`` notice and the failed turn's ``turn.error``."""

RATE_LIMITS_UPDATED: Final = "account/rateLimits/updated"
"""The notice whose ``rateLimits.credits`` reported no credit left, observed
2026-10-27 before the turn failed, with ``hasCredits`` and ``unlimited`` false."""

RESET_WORDING: Final = re.compile(
    r"try again at ([A-Z][a-z]{2}) ([0-9]{1,2})(?:st|nd|rd|th), ([0-9]{4})"
    r" ([0-9]{1,2}):([0-9]{2}) ([AP]M)"
)
"""The reset time in the observed limit message, such as ``try again at Nov 1st, 2026
10:08 PM``. The wording is the only source of the time, so reading it is an inference
about the wording, which a change to it defeats, leaving the reset unknown."""

MONTHS: Final = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
"""English month abbreviations, matched here rather than by ``strptime``, whose
``%b`` follows the process locale."""


def reset_from_message(message: str, zone: tzinfo | None = None) -> str | None:
    """The reset time a Codex limit message states, in the model's due-time form, or
    ``None`` where the message does not match ``RESET_WORDING`` or names no real time.

    **Assumption.** The message names no time zone. It is read in ``zone``, by
    default the local zone of the daemon's host, which is the host the app-server
    runs on, on the inference that Codex formats the time in its host's local zone.
    A wrong zone moves the reset by whole hours, which only delays or hastens when a
    stated budget stops counting as spent. Porting note: the local zone is the
    operating system's, which the Rust port reads by its own means."""
    found = RESET_WORDING.search(message)
    if found is None or found.group(1) not in MONTHS:
        return None
    hour = int(found.group(4))
    if not 1 <= hour <= 12:
        return None
    hour = hour % 12 + (12 if found.group(6) == "PM" else 0)
    try:
        local = datetime(
            int(found.group(3)),
            MONTHS.index(found.group(1)) + 1,
            int(found.group(2)),
            hour,
            int(found.group(5)),
            tzinfo=zone,
        )
        return format_due(local if zone is not None else local.astimezone())
    except (OverflowError, ValueError, InvalidField):
        return None


def ignore_credits(note: str) -> None:
    """Outside a daemon, a spent credit has nowhere to be recorded."""


def usage_limit(error: object) -> EnginePlanLimit | None:
    """The plan limit an error object reports by ``USAGE_LIMIT_EXCEEDED``, with the
    reset time its message states, or ``None``. Only the observed shape counts, a
    ``codexErrorInfo`` of exactly that text."""
    if not isinstance(error, dict):
        return None
    fields = cast(dict[str, object], error)
    if fields.get("codexErrorInfo") != USAGE_LIMIT_EXCEEDED:
        return None
    message = fields.get("message")
    resets_at = reset_from_message(message) if isinstance(message, str) else None
    suffix = "" if resets_at is None else f", which resets at {resets_at}"
    return EnginePlanLimit(
        f"Codex usage limit exceeded{suffix}", resets_at, may_move_role=True
    )


def credits_spent(params: Mapping[str, Any]) -> str | None:
    """A note saying the account has no credit left, when a ``RATE_LIMITS_UPDATED``
    notice reports ``hasCredits`` and ``unlimited`` both false and its ``primary``
    and ``secondary`` windows present and null, or ``None``.

    **Operator's rule, 2026-10-27.** That shape, which is the one observed, is the
    provider's budget at 0, which the harness can act on before any turn fails. The
    operator narrowed it the same day to require both windows null, because whether
    an account with allowance left in a plan window also reports ``hasCredits`` false
    is unknown. A notice with a window, or without the window keys, is therefore not
    read as spent, and a plan whose windows are spent is left to the turn's
    ``usageLimitExceeded`` error."""
    limits = params.get("rateLimits")
    if not isinstance(limits, dict):
        return None
    reported_limits = cast(dict[str, object], limits)
    for window in ("primary", "secondary"):
        if window not in reported_limits or reported_limits[window] is not None:
            return None
    credits = reported_limits.get("credits")
    if not isinstance(credits, dict):
        return None
    reported = cast(dict[str, object], credits)
    if reported.get("hasCredits") is not False:
        return None
    if reported.get("unlimited") is not False:
        return None
    limit_id = reported_limits.get("limitId")
    named = (
        f" for its {limit_id} limit"
        if isinstance(limit_id, str) and 0 < len(limit_id) <= 64
        else ""
    )
    return f"Codex reported no credit left{named}, with hasCredits and unlimited false"


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
    limit: EnginePlanLimit | None = None
    """The plan limit an error notice or the turn's own error reported, which ends a
    turn that did not complete as a plan limit rather than as a failure."""
    turn_id: str | None = None
    status: str = ""
    interrupt_sent: bool = False
    """Whether Dromon already sent this turn's single interrupt request."""
    refused_items: set[str] = field(default_factory=set[str])
    """Identifiers of refused items already counted, regardless of notice order."""
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
                        EngineRefused(
                            "the Codex app-server output closed; pending requests "
                            "failed"
                        )
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
    terminals_unknown: bool = False
    """A failed listing means the last known terminal set may be incomplete."""
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
    ceiling: Ceiling | None = None
    """Where the connection stands against ``keep_session_max_seconds``."""
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
        git_ssh: GitSshAccess | None = None,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = KEEP_POLL_SECONDS,
        task_ended: Callable[[BackgroundEnd], object] = ignore_end,
        compacted: Callable[
            [Mapping[str, int | str | None]], object
        ] = ignore_compaction,
        compaction_estimated: Callable[[int], object] = ignore_estimate,
        ceiling_reached: Ask = no_operator,
        ceiling_settled: Settle = nothing_to_settle,
        credits_spent: Callable[[str], object] = ignore_credits,
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
        self.git_ssh = git_ssh
        """The agent and pinned hosts of the project's ``[git_ssh]`` table, if the
        line was given them."""
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
        self._ceiling_reached = ceiling_reached
        self._ceiling_settled = ceiling_settled
        self._credits_spent = credits_spent
        self._request: CompactionRequest | None = None
        """The compaction the next turn begins with, if any, which each turn takes
        as it starts, as on a Claude Code line."""
        self._context: int | None = None
        """The context the last engine call's usage gave, kept on this engine."""

    @property
    def environment_for_status(self) -> Mapping[str, str]:
        """The environment this adapter uses to choose its shell."""
        return self._environment

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
        turn.limit = turn.limit or work.limit
        after = work.calls[-1].get("total_tokens") if work.calls else None
        compaction: dict[str, int | str | None] = {
            # Codex reports no trigger, so none is made up for it.
            "trigger": None,
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
        note = compaction_note(
            compaction, request.reason, context_line, reported=work.compacted
        )
        return note, (compaction, work)

    async def run_turn(self, prompt: str) -> TurnReport:
        """Run one turn for ``prompt`` and return what happened. A check that
        refuses the turn also closes a kept connection."""
        request, self._request = self._request, None
        try:
            reason = unsupported(self.config)
            if reason is not None:
                raise EngineRefused(reason)
            environment = engine_environment(
                self._environment,
                self.driver.settings.strategy,
                # unsupported() limits Codex built-ins to Bash, Edit, Write and
                # ViewImage, so Codex offers neither Claude Code's Monitor nor
                # PowerShell tool.
                require_shell="Bash" in self.config.builtin_tools,
            )
            # For the launcher, which removes both before the app-server starts and
            # names the line by its mark in what it reports.
            environment[STOP_POLICY_VARIABLE] = self.config.stop_policy.value
            if self.mark is not None:
                environment[launcher.LAUNCH_MARK_VARIABLE] = self.mark
            variables = gitssh.environment(self.git_ssh)
            if SHELL_VARIABLE in environment:
                variables[SHELL_VARIABLE] = environment[SHELL_VARIABLE]
            arguments = server_arguments(
                self._list_servers(environment),
                self.config,
                self.protected,
                self.mark,
                variables,
                git_directories(self.cwd) or (),
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
        if turn.limit is not None and turn.status != "completed":
            raise turn.limit
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
                    error = message["error"]
                    limit = _plan_limit(error)
                    if limit is not None:
                        raise EnginePlanLimit(limit)
                    raise _RpcError(f"{method}: {error}")
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
            elif self._tool_name(item) is not None:
                # The safe point before the next model call that priority mail takes,
                # as ``dromon.priority`` describes.
                await self._act(Event(TOOL_RESULT), turn)
            elif str(item.get("type") or "") not in QUIET_ITEMS:
                await self._refuse_item(item, turn)
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
            turn.limit = turn.limit or usage_limit(completed.get("error"))
        elif method == "error" and self._this_thread(params):
            turn.limit = turn.limit or usage_limit(params.get("error"))
        elif method == RATE_LIMITS_UPDATED:
            self._rate_limits(params)
        elif method == TOKEN_USAGE_UPDATED:
            self._usage_seen(params, turn)
        elif method == THREAD_COMPACTED and self._this_thread(params):
            turn.compacted = True

    def _rate_limits(self, params: Mapping[str, Any]) -> None:
        """Report an account with no credit left to the scheduler at once, in a turn
        or between turns, as ``credits_spent`` reads it."""
        note = credits_spent(params)
        if note is None:
            return
        try:
            self._credits_spent(note)
        except Exception as error:  # noqa: BLE001
            print(
                f"dromond: a spent Codex credit could not be recorded: {error}",
                file=sys.stderr,
            )

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
        name = self._tool_name(item)
        if name is None:
            await self._refuse_item(item, turn)
            return
        turn.tool_uses.append(name)
        self._observe(ActivityKind.TOOL, name)
        await self._act(Event("tool_use", name), turn)

    async def _refuse_item(self, item: Mapping[str, Any], turn: _Turn) -> None:
        """Count and refuse an unavailable item once across start and completion."""
        kind = str(item.get("type") or "")
        identifier = item.get("id")
        if isinstance(identifier, str) and identifier in turn.refused_items:
            return
        if isinstance(identifier, str):
            turn.refused_items.add(identifier)
        # The driver's budget counts refused calls even when Codex reports only
        # item/completed. Identifiers deduplicate either notice order.
        await self._act(Event("tool_use", kind), turn)
        reason = f"the engine used a tool the line was not given: {kind}"
        if kind == IMAGE_ITEM and VIEW_IMAGE in self.config.builtin_tools:
            reason = f"the line may not view that image: {self._image_refusal(item)}"
        await self._interrupt(turn, reason)

    def _tool_name(self, item: Mapping[str, Any]) -> str | None:
        """The tool an item is a use of, as the driver counts it, or ``None`` for an
        item that is no tool the line was given."""
        kind = str(item.get("type") or "")
        if kind == "dynamicToolCall" and str(item.get("tool")) in self.mcp_tools:
            return str(item.get("tool"))
        if kind == "commandExecution" and "Bash" in self.config.builtin_tools:
            return "Bash"
        if kind == "fileChange" and WRITE_TOOLS & set(self.config.builtin_tools):
            return "Edit"
        if kind == IMAGE_ITEM and VIEW_IMAGE in self.config.builtin_tools:
            return None if self._image_refusal(item) else VIEW_IMAGE
        return None

    def _image_refusal(self, item: Mapping[str, Any]) -> str | None:
        """Why the image an ``imageView`` item names may not be viewed, or ``None``.
        **Limit.** Codex emits the item as it handles the call, so this stops the turn
        and cannot be shown to prevent the read. What Codex may read is bounded first
        by the permission profile."""
        return image_refusal(item.get("path"), self.cwd)

    async def _act(self, event: Event, turn: _Turn) -> None:
        action = self.driver.step(event)
        if action.tag is ActionTag.INTERRUPT and turn.interrupted_for is None:
            turn.interrupted_for = action.text
            await self._interrupt(turn, None)

    async def _interrupt(self, turn: _Turn, refusal: str | None) -> None:
        if refusal is not None and turn.refusal is None:
            turn.refusal = refusal
        if turn.interrupt_sent or turn.turn_id is None or turn.status:
            return
        turn.interrupt_sent = True
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
        names = "\n".join([self.config.workdir_access.value, *sorted(self.mcp_tools)])
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
        if kept.terminals_unknown:
            return kept.last_end + self.config.keep_session_hard_seconds - self._clock()
        deadline = self._ceiling_of(kept).deadline(kept.last_end, bool(kept.terminals))
        return deadline - self._clock()

    def _ceiling_of(self, kept: _Kept) -> Ceiling:
        if kept.ceiling is None:
            kept.ceiling = Ceiling(
                self.config, self._ceiling_reached, self._ceiling_settled
            )
        return kept.ceiling

    def kept_status(self) -> dict[str, object]:
        kept = self._kept
        alive = kept is not None and kept.routed.alive
        terminals = kept.terminals if kept is not None and alive else []
        return {
            "keep_session": self.config.keep_session,
            "kept": alive,
            "seconds_to_close": self.seconds_to_close(),
            "background_tasks": len(terminals),
            "background_tasks_unknown": bool(
                kept is not None and alive and kept.terminals_unknown
            ),
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
        """Close the kept app-server at its deadline, or ask an operator at its
        ceiling, as ``dromon.ceiling`` describes, and say whether it was closed."""
        kept = self._kept
        if kept is None or not kept.routed.alive or self._in_turn:
            return False
        if kept.terminals_unknown:
            if self._clock() < (kept.last_end + self.config.keep_session_hard_seconds):
                return False
            await self.close(
                "its background terminal listing remained unknown until the hard bound"
            )
            return True
        reason = self._ceiling_of(kept).due(
            kept.last_end, len(kept.terminals), self._clock()
        )
        if reason is None:
            return False
        await self.close(reason)
        return True

    async def decide_ceiling(self, question_id: str, answer: str) -> Choice | None:
        """Carry out an operator's answer to the ceiling question."""
        kept = self._kept
        if kept is None:
            return None
        choice = self._ceiling_of(kept).decide(question_id, answer, self._clock())
        if choice is Choice.STOP:
            await self.close(STOP_REASON)
        return choice

    async def halt(self) -> str | None:
        """Finish an operator's stop once the stopped turn has ended, as
        ``ClaudeCodeLine.halt`` does. A failed interrupt has closed the app-server
        already. One that still lists background terminals is closed here."""
        pending = list(self._background)
        if pending:
            await asyncio.wait(pending, timeout=INTERRUPT_TIMEOUT_SECONDS + 1)
        kept = self._kept
        if kept is None or not kept.routed.alive or not kept.terminals:
            return None
        await self.close(HALT_REASON)
        return HALT_REASON

    async def retire(self) -> None:
        self._retired = True
        if not self._in_turn:
            await self.close()

    @staticmethod
    def _withdraw_ceiling(kept: _Kept) -> None:
        """Withdraw the connection's open ceiling question, if any. A failure is
        reported and goes no further, since the caller may be a done callback."""
        if kept.ceiling is None:
            return
        try:
            kept.ceiling.withdraw()
        except Exception as error:  # noqa: BLE001
            print(
                f"dromond: a ceiling question was not withdrawn: {error}",
                file=sys.stderr,
            )

    async def close(self, reason: str | None = None) -> None:
        """Close the kept app-server, which ends its background terminals. With
        ``reason`` the next prompt says why."""
        kept = self._kept
        if kept is None:
            return
        self._kept = None
        self._withdraw_ceiling(kept)
        if reason is not None:
            left = self.config.stop_policy is StopPolicy.LEAVE_BACKGROUND
            self._notes.append(closed_note(reason, len(kept.terminals), left))
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
        # Review F12. A connection that dies without a close withdraws its ceiling
        # question as the pump ends, since nothing can act on an answer now.
        kept.pump.add_done_callback(lambda _: self._withdraw_ceiling(kept))
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
        if method == RATE_LIMITS_UPDATED:
            self._rate_limits(cast(dict[str, Any], message.get("params") or {}))
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
            or (not kept.terminals and not kept.terminals_unknown)
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
        except EnginePlanLimit:
            raise
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
            limit = _plan_limit(message["error"])
            if limit is not None:
                raise EnginePlanLimit(limit)
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
                # Review N10, masked as for Claude Code's tasks.
                kept.commands[value] = redact.mask(command)[:200]
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
        try:
            return await self._list_background()
        except EngineRefused:
            if self._kept is not None:
                self._kept.terminals_unknown = True
            raise

    async def _list_background(self) -> list[dict[str, object]]:
        """Complete a cursor walk before changing the last known terminal state."""
        listed: list[object] = []
        cursor: object = None
        for _page in range(MAX_TERMINAL_PAGES):
            params: dict[str, object] = {"threadId": self._thread_id}
            if cursor is not None:
                params["cursor"] = cursor
            result = await self.request(LIST_TERMINALS, params)
            data = result.get("data")
            if not isinstance(data, list):
                raise EngineRefused("the engine returned no background terminal page")
            page_data = cast(list[object], data)
            cursor = result.get("nextCursor")
            if len(listed) + len(page_data) > MAX_TERMINALS:
                raise EngineRefused(
                    "the background terminal listing exceeds its 64-terminal "
                    "bound, so terminal state is unknown"
                )
            listed.extend(page_data)
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor:
                raise EngineRefused("the engine returned an invalid terminal cursor")
            if len(listed) == MAX_TERMINALS:
                raise EngineRefused(
                    "the background terminal listing reached its 64-terminal bound "
                    "with another page, so terminal state is unknown"
                )
        else:
            raise EngineRefused(
                "the background terminal listing is unfinished after "
                f"{MAX_TERMINAL_PAGES} pages, so terminal state is unknown"
            )
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
            kept.terminals_unknown = False
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
        try:
            await self.list_background()
        except EnginePlanLimit:
            raise
        except EngineRefused:
            pass
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
            self._ceiling_of(kept).turn_ended()
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
        if turn.limit is not None and turn.status != "completed":
            await self.close("Codex reported a subscription plan limit")
            raise turn.limit
        try:
            await self.list_background()
        except EnginePlanLimit:
            await self.close("Codex reported a subscription plan limit")
            raise
        except EngineRefused:
            # Keep the last known state marked unknown. The idle deadline must not
            # treat a failed listing as an empty set of background terminals.
            pass
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
