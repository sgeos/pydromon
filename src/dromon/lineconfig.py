"""Per-line configuration: tools, MCP servers, tool policy, sandbox and settings.

A line's configuration names the built-in Claude Code tools it gets, the MCP servers it
connects to, and which tool calls are allowed. ``allowlist`` allows the given built-in
tools, Dromon's own tools, and every name or ``mcp__server__*`` pattern listed in
``allowed_tools``, and denies everything else. ``allow_all`` allows every tool the
engine offers. ``inherit_account_mcp`` lets Claude Code also load the MCP servers of
the operator's user settings and claude.ai account, which are otherwise excluded.
``sandbox`` is the line's own sandbox document, as ``dromon.sandbox`` defines it,
``settings`` its own engine settings, as ``dromon.settings`` defines them, and
``effort`` replaces the reasoning effort its driver declares.

Instructions. ``claude_code_prompt`` runs a Claude Code line on Claude Code's own system
prompt instead of an empty one, and ``instructions`` names files, such as a project's
``CLAUDE.md``, whose text is appended to the system prompt at the start of each turn. A
relative path is resolved against the project root, as ``rooted_instructions`` says,
and ``~`` against the home directory. Only the paths are stored.

Kept sessions. ``keep_session`` keeps a Claude Code line's engine connected between
turns, so that commands it runs in the background outlive the turn. The connection
closes after ``keep_session_idle_seconds`` without a turn, or after
``keep_session_max_seconds`` while the engine reports background tasks still running.
``keep_session_at_max`` decides what happens at that ceiling, as ``dromon.ceiling``
describes, and ``keep_session_hard_seconds`` bounds every choice. ``dromon.engine``
describes the checks a kept connection still passes at every turn.

Stop policy. ``stop_policy`` decides whether stopping a line's engine, or the daemon,
ends everything the engine started, which is the default, or leaves background
descendants running, as ``dromon.launcher`` describes.

Working directory. ``workdir`` names the directory a line's engine runs in, such as a
git worktree of its own. A relative path is resolved against the project root, and
``~`` against the home directory, when the line is configured and again at each turn,
by ``dromon.workdir``. Only the path is stored, and a line without one runs in the
project root. The line's relative sandbox paths resolve against it, while its relative
instruction paths resolve against the project root, as above. A line that works in a
worktree of another branch therefore names its own ``CLAUDE.md`` by absolute path, or it
reads the file of the branch checked out at the root.

References. Any text of an MCP server may hold ``${NAME}`` or ``${NAME:-default}``,
which the engine replaces with the daemon's environment at the start of each turn, as
Claude Code expands its ``.mcp.json``. Only the reference is stored, so a token can stay
out of the event log. Variables that hold the engine's own credential are refused.

The configuration is stored in the event log as canonical JSON, so it survives a
restart and every change is on record. ``docs/protocol/LINE_CONFIG.md`` is normative.

Security. The configuration is operator authority and arrives only through the client.
An MCP server's ``env`` and ``headers`` may hold credentials. From 2026-11-01 the
scheduler seals each literal value into the store beside the event log before it
stores a configuration, as ``dromon.sealed`` describes, so the log holds a
placeholder. ``allow_all`` and ``inherit_account_mcp`` hand a line every tool they
bring, such as a claude.ai account's mail and file connectors, and are meant for lines
the operator trusts.
"""

import json
import re
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Final, NoReturn, cast

from dromon.launcher import StopPolicy
from dromon.model import ID_PATTERN, InvalidField, parse_any_address
from dromon.sandbox import SandboxConfig, SandboxError, parse_sandbox
from dromon.settings import (
    SettingsConfig,
    SettingsError,
    parse_settings,
    reserved_variable,
)

CONFIG_VERSION: Final = 1

RESERVED_SERVERS: Final = frozenset({"dromon"})
"""Server names Dromon uses for its own tools."""

BUILTIN_PATTERN: Final = re.compile(r"[A-Z][A-Za-z0-9_]{0,63}")
TOOL_PATTERN: Final = re.compile(
    r"mcp__[a-z0-9][a-z0-9._-]{0,63}__(\*|[A-Za-z0-9_.-]{1,128})"
)
MAX_ENTRIES: Final = 256
MAX_TEXT: Final = 4096

MAX_INSTRUCTION_FILES: Final = 16

MAX_PLUGINS: Final = 16

DEFAULT_CONTEXT_WARN_TOKENS: Final = 150_000
"""The context size past which a line's prompt warns. Each engine call re-reads the
whole context, so past this a turn of a few calls re-reads a million tokens, which
the round 4 worker line observed as most of its cost, and it leaves room before an
engine's automatic compaction near a 200,000 token window."""
MIN_CONTEXT_WARN_TOKENS: Final = 1_000
MAX_CONTEXT_WARN_TOKENS: Final = 10_000_000

DEFAULT_KEEP_IDLE_SECONDS: Final = 1800
DEFAULT_KEEP_MAX_SECONDS: Final = 14400
MIN_KEEP_SECONDS: Final = 60
MAX_KEEP_SECONDS: Final = 86400
DEFAULT_KEEP_HARD_SECONDS: Final = 86400
"""The hard bound past which no choice at the ceiling keeps a connection, a day."""
MAX_KEEP_HARD_SECONDS: Final = 7 * 86400

EFFORTS: Final = ("low", "medium", "high", "xhigh", "max")
"""The reasoning efforts Claude Code accepts."""

REFERENCE: Final = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]{0,127})(?::-([^}]*))?\}")


class ConfigError(ValueError):
    """A line configuration is malformed or unsafe to accept."""


class UnsetVariable(ConfigError):
    """An MCP server refers to a variable the environment does not set."""


def references(text: str) -> tuple[str, ...]:
    """The variable names ``text`` refers to, or ``ConfigError`` for a ``${`` that does
    not start a well-formed reference, or for a reserved variable.

    The errors give a character position and never any part of ``text``, because
    ``text`` may be a credential that sealing would keep out of the log, and an error
    reaches the client and, for a project file, the daemon's output. A reserved
    name is not echoed either, since a reserved prefix matches names that carry
    arbitrary text after it."""
    names: list[str] = []
    position = text.find("${")
    while position != -1:
        found = REFERENCE.match(text, position)
        if found is None:
            _fail(f"malformed variable reference at character {position + 1}")
        if reserved_variable(found.group(1)):
            _fail(
                "a server may not refer to a reserved variable,"
                f" at character {position + 1}"
            )
        names.append(found.group(1))
        position = text.find("${", found.end())
    return tuple(names)


def expand(text: str, environment: Mapping[str, str]) -> str:
    """``text`` with each reference replaced from ``environment``, or
    ``UnsetVariable`` for an unset variable that has no default."""

    def value(found: re.Match[str]) -> str:
        name, default = found.group(1), found.group(2)
        if name in environment:
            return environment[name]
        if default is not None:
            return default
        raise UnsetVariable(f"the environment does not set {name}")

    return REFERENCE.sub(value, text)


def _check_url(url: str, what: str) -> str:
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        _fail(f"{what} url must use https, or http on the local host")
    return url


class ToolPolicy(StrEnum):
    ALLOWLIST = "allowlist"
    ALLOW_ALL = "allow_all"


class WorkdirAccess(StrEnum):
    """The engine's permission to write inside its working directory."""

    WRITE = "write"
    READ = "read"


class AtMax(StrEnum):
    """What a kept connection does when it reaches ``keep_session_max_seconds`` with
    background work still running. Each is bounded by ``keep_session_hard_seconds``.

    The default is ``CLOSE``, the behaviour before the choice existed, so that no
    stored configuration changes meaning, and so that an operator who is away cannot
    leave work running longer than the ceiling they set. ``ASK`` keeps the work until
    an operator answers, which an absent operator would turn into the hard bound."""

    CLOSE = "close"
    ASK = "ask"
    KEEP = "keep"


@dataclass(frozen=True, slots=True)
class McpServer:
    """One MCP server, in the shape Claude Code's configuration uses."""

    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    url: str = ""
    headers: tuple[tuple[str, str], ...] = ()

    def sdk_config(
        self, environment: Mapping[str, str] | None = None
    ) -> dict[str, object]:
        """The server as the Agent SDK's ``mcp_servers`` entry, with its references
        replaced from ``environment``, or kept as written when it is ``None``.

        Raises ``UnsetVariable`` for an unset variable, and ``ConfigError`` when a
        replaced url no longer meets the transport rule."""
        text: Callable[[str], str] = (
            (lambda value: value)
            if environment is None
            else (lambda value: expand(value, environment))
        )
        if self.transport == "stdio":
            config: dict[str, object] = {"type": "stdio", "command": text(self.command)}
            if self.args:
                config["args"] = [text(arg) for arg in self.args]
            if self.env:
                config["env"] = {name: text(value) for name, value in self.env}
            return config
        url = text(self.url)
        if environment is not None:
            _check_url(url, "the expanded server")
        config = {"type": self.transport, "url": url}
        if self.headers:
            config["headers"] = {name: text(value) for name, value in self.headers}
        return config


def _explicit(path: str) -> bool:
    """Whether ``path`` names its file explicitly, from the home directory or the
    file system's root, rather than relative to the project."""
    return path.startswith("~") or Path(path).is_absolute()


def _contained(path: str, root: Path) -> Path:
    """The real path of the relative ``path`` under ``root``, or ``ConfigError`` when
    it resolves, through ``..`` or a symbolic link, outside the project root."""
    real_root = root.resolve()
    file = (root / path).resolve()
    if not file.is_relative_to(real_root):
        _fail(f"the instructions {path} lie outside the project root")
    return file


def check_instructions(paths: tuple[str, ...], root: Path) -> None:
    """Refuse instruction ``paths`` that a turn could not read safely, as the
    configuration is loaded rather than when a turn starts. A relative path must
    resolve, after symbolic links, inside the project ``root``, so a project file
    cannot reach the operator's other files through it. A path from ``~`` or the root
    of the file system is the operator's explicit choice and may lie anywhere, such as
    ``~/.claude/CLAUDE.md``. Every path must name a regular file that exists. Raises
    ``ConfigError`` naming the file.

    **Review of 2026-10-27, I0-1 and I0-2.** A relative path was joined to the root
    unchecked, and a missing file was found only when a turn failed. A file removed
    after loading still fails its turn, which then holds the line and asks an
    operator."""
    for path in paths:
        file = Path(path).expanduser() if _explicit(path) else _contained(path, root)
        if not file.is_file():
            _fail(f"the instructions {path} do not name a regular file that exists")


def rooted_instructions(config: "LineConfig", root: Path) -> "LineConfig":
    """``config`` with each relative path in ``instructions`` made absolute against
    the project ``root``, as every engine adapter then reads it. A path that begins
    with ``~`` is left for the reader to expand. **Luna's turn of 2026-10-27.** A
    relative path was resolved against the line's working directory, so a line moved
    to a worktree lost a file that only the project root held, and its turn failed
    before it began. The project root is where the configuration is written, so a
    path resolved there means the same for every line, whatever its workdir. Porting
    note, a port resolves the paths once as the configuration is loaded.

    A relative path is checked again here, when an engine is built, and raises
    ``ConfigError`` if it now resolves outside the root, such as after a symbolic link
    in the project was changed since loading."""
    rooted: list[str] = []
    for path in config.instructions:
        if not _explicit(path):
            _contained(path, root)
            path = str(root / path)
        rooted.append(path)
    paths = tuple(rooted)
    return (
        config if paths == config.instructions else replace(config, instructions=paths)
    )


@dataclass(frozen=True, slots=True)
class LineConfig:
    builtin_tools: tuple[str, ...] = ()
    mcp_servers: tuple[tuple[str, McpServer], ...] = ()
    tool_policy: ToolPolicy = ToolPolicy.ALLOWLIST
    allowed_tools: tuple[str, ...] = field(default=())
    inherit_account_mcp: bool = False
    sandbox: SandboxConfig | None = None
    """The line's own sandbox document, before the project's is merged in."""
    settings: SettingsConfig | None = None
    """The line's own engine settings, before the project's are merged in."""
    effort: str | None = None
    """The reasoning effort, or ``None`` for the one the driver declares."""
    claude_code_prompt: bool = False
    """Whether a Claude Code line runs on Claude Code's own system prompt."""
    instructions: tuple[str, ...] = ()
    """Files whose text is appended to the system prompt, in order."""
    workdir: str | None = None
    """The directory the engine runs in, as written, or ``None`` for the project
    root."""
    workdir_access: WorkdirAccess = WorkdirAccess.WRITE
    """Whether the engine may write its workdir, as enforced by its confinement."""
    writable_paths: tuple[str, ...] = ()
    """Absolute directories an operator grants a Codex line permission to write."""
    report_directory: str | None = None
    """A directory inside the line's working directory, under ``tmp/``, that
    ``write_scratch`` may also write to when the call asks for the reports area, so a
    review line writes its report without a shell. **Round 14, item 2.** Relative,
    written with ``/``, never ``tmp`` itself and never under ``tmp/lines``, which
    hold lines' scratch."""
    project_settings: bool = False
    """Whether a Claude Code line also loads Claude Code's project setting source, the
    working directory's ``.claude/settings.json`` and what it brings."""
    plugins: tuple[str, ...] = ()
    """Local plugin directories a Claude Code line loads, absolute or under ``~``, as
    written. Each is checked against an allowlist at every turn."""
    keep_session: bool = False
    """Whether a Claude Code line's engine stays connected between turns."""
    keep_session_idle_seconds: int = DEFAULT_KEEP_IDLE_SECONDS
    """How long a kept connection waits for the next turn before it closes."""
    keep_session_max_seconds: int = DEFAULT_KEEP_MAX_SECONDS
    """How long after the last turn a kept connection may stay open while the engine
    reports background tasks still running, before ``keep_session_at_max`` applies."""
    keep_session_at_max: AtMax = AtMax.CLOSE
    """What the connection does at ``keep_session_max_seconds``."""
    keep_session_hard_seconds: int = DEFAULT_KEEP_HARD_SECONDS
    """How long after the last turn a kept connection may stay open, whatever was
    chosen at its ceiling."""
    stop_policy: StopPolicy = StopPolicy.KILL_ALL
    """Whether stopping the engine ends what it started or leaves its background
    descendants running."""
    context_warn_tokens: int = DEFAULT_CONTEXT_WARN_TOKENS
    """The context size past which the line's prompt warns."""
    allow_compact: bool = False
    """Whether a Claude Code or Codex line has ``compact_session``, to compact its
    own session."""
    auto_compact_tokens: int | None = None
    """The context estimate at or past which a turn of a line with ``allow_compact``
    begins by compacting the session, or ``None`` for never. Set by an operator."""
    context_handoff: bool = True
    """Whether the line's first turn in a new engine session, after a rebind or a
    cleared session, begins with a selection from its mail. Row 13."""
    fresh_session_each_turn: bool = False
    """Whether each turn starts a new engine session without context handoff."""
    recipients: tuple[str, ...] | None = None
    """The parties the line may send mail to, as addresses written canonically, or
    ``None`` for every party. A line may always send itself a note, which opens no
    channel to anyone. Questions to an operator are not limited by it."""

    def __post_init__(self) -> None:
        if self.fresh_session_each_turn:
            if self.keep_session:
                raise ConfigError(
                    "fresh_session_each_turn cannot be combined with keep_session"
                )
            object.__setattr__(self, "context_handoff", False)

    def allows(self, tool_name: str, own_tools: frozenset[str]) -> bool:
        """Whether the policy allows ``tool_name``. Dromon's own tools always pass."""
        if tool_name in own_tools or tool_name in self.builtin_tools:
            return True
        if self.tool_policy is ToolPolicy.ALLOW_ALL:
            return True
        for pattern in self.allowed_tools:
            if pattern.endswith("__*"):
                if tool_name.startswith(pattern[:-1]):
                    return True
            elif tool_name == pattern:
                return True
        return False

    def server_names(self) -> frozenset[str]:
        return frozenset(name for name, _ in self.mcp_servers)


DEFAULT_CONFIG: Final = LineConfig()

CLAUDE_CODE_ENGINE: Final = "claude-code"
"""The engine name of Claude Code, as ``dromon.engine.ENGINE_NAME`` gives it, which a
test holds equal, since that module imports this one."""

WRITING_TOOLS: Final = frozenset({"Bash", "Write", "Edit", "NotebookEdit"})
"""The built-in tools with which a Claude Code line can change files, a shell among
them."""

WORKSPACE_WRITE_TOOLS: Final = frozenset({"Edit", "Write"})
"""The built-in tools that make the workspace writable in the sandbox profile of a
Codex or Grok line, as ``codex.WRITE_TOOLS`` and ``grok.WRITE_TOOLS`` are, which a
test holds equal. They are repeated here because both engine modules import this
one."""

SANDBOXED_WRITERS: Final[dict[str, frozenset[str]]] = {
    "codex": WORKSPACE_WRITE_TOOLS,
    "grok-build": WORKSPACE_WRITE_TOOLS,
}
"""The engines whose own sandbox makes the workspace read-only unless the line has one
of these built-in tools, whatever its tool policy, so a shell alone cannot write it."""

UNCONFINED_ENGINES: Final = frozenset({"pi", "cursor-agent", "antigravity"})
"""The engines that cannot confine a line to reading its working directory, and that
refuse ``workdir_access = "read"``, so any line of theirs can write it."""


def can_write_workdir(config: LineConfig, engine: str | None) -> bool:
    """Whether a line with ``config`` on ``engine`` can write its working
    directory, for the warning of ``dromon config`` about writers that share one.

    **Round 9, item 2.** A line writes its working directory only when its access
    is ``write``. A line with ``workdir_access = "read"`` is confined to reading it
    by its engine. On Claude Code it then also needs a tool that changes files, a
    writing built-in or a policy that allows every tool. A line whose only tools
    come from MCP servers is not counted, since such a server is the operator's own
    program with the tools it declares, and not a file tool of the engine.
    **Assumption.** A server that writes the working directory itself is not
    detected, and the warning remains a warning, as it always was.

    **Round 9 review, F16 and F17.** The answer follows each engine's own
    confinement. A Codex or Grok line writes only with ``Edit`` or ``Write``, since
    its sandbox keeps the workspace read-only otherwise, even under ``allow_all``,
    and a shell alone cannot write there. A Pi, Cursor or Antigravity line with
    write access counts as a writer, since those engines cannot confine a line to
    reading. An engine Dromon does not know, given as ``None`` or by a name not
    listed here, is counted as a writer, so the warning errs toward being given."""
    if config.workdir_access is not WorkdirAccess.WRITE:
        return False
    sandboxed = SANDBOXED_WRITERS.get(engine or "")
    if sandboxed is not None:
        return bool(set(config.builtin_tools) & sandboxed)
    if engine in UNCONFINED_ENGINES or engine != CLAUDE_CODE_ENGINE:
        return True
    if config.tool_policy is ToolPolicy.ALLOW_ALL:
        return True
    names = set(config.builtin_tools) | set(config.allowed_tools)
    return bool(names & WRITING_TOOLS)


def encode_config(config: LineConfig) -> str:
    """The canonical JSON of ``config``, as stored in the event log."""
    document: dict[str, object] = {
        "v": CONFIG_VERSION,
        "builtin_tools": list(config.builtin_tools),
        "mcp_servers": {
            name: server.sdk_config() for name, server in config.mcp_servers
        },
        "tool_policy": config.tool_policy.value,
        "allowed_tools": list(config.allowed_tools),
        "inherit_account_mcp": config.inherit_account_mcp,
    }
    # Each optional key is omitted when unset, so configurations stored before it
    # existed keep their text.
    if config.sandbox is not None:
        document["sandbox"] = config.sandbox.document()
    if config.settings is not None:
        document["settings"] = config.settings.document()
    if config.effort is not None:
        document["effort"] = config.effort
    if config.claude_code_prompt:
        document["claude_code_prompt"] = True
    if config.instructions:
        document["instructions"] = list(config.instructions)
    if config.workdir is not None:
        document["workdir"] = config.workdir
    if config.workdir_access is not WorkdirAccess.WRITE:
        document["workdir_access"] = config.workdir_access.value
    if config.writable_paths:
        document["writable_paths"] = list(config.writable_paths)
    if config.report_directory is not None:
        document["report_directory"] = config.report_directory
    if config.project_settings:
        document["project_settings"] = True
    if config.plugins:
        document["plugins"] = list(config.plugins)
    if config.keep_session:
        document["keep_session"] = True
    if config.fresh_session_each_turn:
        document["fresh_session_each_turn"] = True
    if config.keep_session_idle_seconds != DEFAULT_KEEP_IDLE_SECONDS:
        document["keep_session_idle_seconds"] = config.keep_session_idle_seconds
    if config.keep_session_max_seconds != DEFAULT_KEEP_MAX_SECONDS:
        document["keep_session_max_seconds"] = config.keep_session_max_seconds
    if config.keep_session_at_max is not AtMax.CLOSE:
        document["keep_session_at_max"] = config.keep_session_at_max.value
    if config.keep_session_hard_seconds != DEFAULT_KEEP_HARD_SECONDS:
        document["keep_session_hard_seconds"] = config.keep_session_hard_seconds
    if config.stop_policy is not StopPolicy.KILL_ALL:
        document["stop_policy"] = config.stop_policy.value
    if config.context_warn_tokens != DEFAULT_CONTEXT_WARN_TOKENS:
        document["context_warn_tokens"] = config.context_warn_tokens
    if config.allow_compact:
        document["allow_compact"] = True
    if config.auto_compact_tokens is not None:
        document["auto_compact_tokens"] = config.auto_compact_tokens
    if not config.context_handoff:
        document["context_handoff"] = False
    if config.recipients is not None:
        document["recipients"] = list(config.recipients)
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


def _fail(message: str) -> NoReturn:
    raise ConfigError(message)


def _reject_number(text: str) -> NoReturn:
    _fail(f"unexpected number {text!r}")


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    document: dict[str, object] = {}
    for key, value in pairs:
        if key in document:
            _fail(f"duplicate key {key!r}")
        document[key] = value
    return document


def _object(value: object, what: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{what} is not an object")
    return cast(dict[str, object], value)


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        _fail(f"{what} must be text of 1 to {MAX_TEXT} characters")
    return value


def _workdir(value: object) -> str:
    """A working directory as written. Whether it exists and where it lies is
    checked against the project by ``dromon.workdir``, since this module has no
    project."""
    text = _text(value, "workdir")
    if "\x00" in text:
        _fail("workdir must not contain a NUL character")
    if text.startswith("~") and text != "~" and not text.startswith("~/"):
        _fail("workdir may start with ~ only as ~ or ~/, not as another user's home")
    return text


MAX_REPORT_DEPTH: Final = 8
REPORT_ROOT: Final = "tmp"
REPORT_RESERVED: Final = ("lines", "worktrees", "runs", "probes")
"""The directories of ``tmp/`` that Dromon or another role keeps. ``lines`` holds each
line's own scratch, ``worktrees`` the roles' worktrees, ``runs`` the supervised runs'
output and ``probes`` the recorded engine fixtures. **Review of a9995d8, item 3.** A
report directory in or under any of them would write what another line or role owns,
and on a volume that ignores case, ``tmp/Lines`` is ``tmp/lines``, so names are compared
after normalization and case folding."""


def _folded(part: str) -> str:
    """A path component as a volume that ignores case, or Unicode form, would see it."""
    return unicodedata.normalize("NFKC", part).casefold()


def _report_directory(value: object) -> str:
    """A report directory as written, relative to the working directory and inside
    its ``tmp/``. Refused when absolute, when any component is empty, ``.`` or ``..``,
    when it is ``tmp`` itself, outside ``tmp``, under ``tmp/lines``, or deeper than
    ``MAX_REPORT_DEPTH`` components. A bare ``\\`` is refused, since a path here is
    written with ``/``."""
    text = _text(value, "report_directory")
    if "\x00" in text or "\\" in text:
        _fail("report_directory must not contain a NUL character or a backslash")
    parts = text.split("/")
    if text.startswith("/") or any(part in ("", ".", "..") for part in parts):
        _fail(
            "report_directory must be a relative path with no empty, . or .. component"
        )
    if len(parts) > MAX_REPORT_DEPTH:
        _fail(f"report_directory has more than {MAX_REPORT_DEPTH} components")
    if parts[0] != REPORT_ROOT or len(parts) < 2:
        _fail(f"report_directory must be a directory under {REPORT_ROOT}/")
    if _folded(parts[1]) in REPORT_RESERVED:
        _fail(
            f"report_directory may not be under {REPORT_ROOT}/ at any of"
            f" {', '.join(REPORT_RESERVED)}, in any letter case, which hold lines'"
            " scratch, roles' worktrees and recorded runs and probes"
        )
    if any(part.startswith(".") or part.endswith((".", " ")) for part in parts[1:]):
        _fail(
            "report_directory may not have a component that starts with . or ends"
            " with . or a space"
        )
    return text


def _seconds(document: Mapping[str, object], key: str, default: int) -> int:
    value = document.get(key, default)
    if type(value) is not int or not MIN_KEEP_SECONDS <= value <= MAX_KEEP_SECONDS:
        _fail(
            f"{key} must be a whole number from {MIN_KEEP_SECONDS} to"
            f" {MAX_KEEP_SECONDS}"
        )
    return value


def _choice[C: StrEnum](
    document: Mapping[str, object], key: str, kind: type[C], default: C
) -> C:
    """The member of ``kind`` that ``document`` names under ``key``, or ``default``.
    Anything but one of the members' exact names is refused."""
    value = document.get(key, default.value)
    names = [member.value for member in kind]
    if not isinstance(value, str) or value not in names:
        _fail(f"{key} must be one of {names}, found {value!r}")
    return kind(value)


def _server_text(value: object, what: str) -> str:
    text = _text(value, what)
    try:
        references(text)
    except ConfigError as error:
        _fail(f"{what}: {error}")
    return text


def _texts(
    value: object, what: str, check: Callable[[object, str], str] = _text
) -> tuple[str, ...]:
    if not isinstance(value, list):
        _fail(f"{what} is not an array")
    items = cast(list[object], value)
    if len(items) > MAX_ENTRIES:
        _fail(f"{what} has more than {MAX_ENTRIES} entries")
    return tuple(check(item, what) for item in items)


def _pairs(value: object, what: str) -> tuple[tuple[str, str], ...]:
    document = _object(value, what)
    if len(document) > MAX_ENTRIES:
        _fail(f"{what} has more than {MAX_ENTRIES} entries")
    pairs: list[tuple[str, str]] = []
    for key, item in document.items():
        name = _text(key, what)
        # The key names the location, as the value itself may never be echoed.
        pairs.append((name, _server_text(item, f"{what} {name}")))
    return tuple(sorted(pairs))


def _server(name: str, value: object) -> McpServer:
    document = _object(value, f"server {name}")
    transport = document.get("type", "stdio")
    if transport == "stdio":
        allowed = {"type", "command", "args", "env"}
        if not set(document) <= allowed or "command" not in document:
            _fail(f"server {name} must have a command and only {sorted(allowed)}")
        return McpServer(
            "stdio",
            command=_server_text(document["command"], f"server {name} command"),
            args=_texts(document.get("args", []), f"server {name} args", _server_text),
            env=_pairs(document.get("env", {}), f"server {name} env"),
        )
    if transport in ("http", "sse"):
        allowed = {"type", "url", "headers"}
        if not set(document) <= allowed or "url" not in document:
            _fail(f"server {name} must have a url and only {sorted(allowed)}")
        url = _server_text(document["url"], f"server {name} url")
        # A url that starts with a reference is checked once it is expanded, as
        # every url is at the start of a turn.
        if not url.startswith("${"):
            _check_url(url, f"server {name}")
        return McpServer(
            str(transport),
            url=url,
            headers=_pairs(document.get("headers", {}), f"server {name} headers"),
        )
    _fail(f"server {name} has an unknown type {transport!r}")


def parse_servers(value: object) -> tuple[tuple[str, McpServer], ...]:
    """Servers from a mapping of name to server, or a ``{"mcpServers": ...}`` file."""
    document = _object(value, "mcp_servers")
    if set(document) == {"mcpServers"}:
        document = _object(document["mcpServers"], "mcpServers")
    if len(document) > MAX_ENTRIES:
        _fail(f"mcp_servers has more than {MAX_ENTRIES} servers")
    servers: list[tuple[str, McpServer]] = []
    for name, server in sorted(document.items()):
        if ID_PATTERN.fullmatch(name) is None:
            _fail(f"server name {name!r} is not a valid identifier")
        if name in RESERVED_SERVERS:
            _fail(f"server name {name!r} is reserved for Dromon")
        servers.append((name, _server(name, server)))
    return tuple(servers)


def config_from_document(document: Mapping[str, object]) -> LineConfig:
    """A validated configuration from a decoded JSON object, without its version."""
    allowed = {
        "builtin_tools",
        "mcp_servers",
        "tool_policy",
        "allowed_tools",
        "inherit_account_mcp",
        "sandbox",
        "settings",
        "effort",
        "claude_code_prompt",
        "instructions",
        "workdir",
        "workdir_access",
        "writable_paths",
        "report_directory",
        "project_settings",
        "plugins",
        "keep_session",
        "fresh_session_each_turn",
        "keep_session_idle_seconds",
        "keep_session_max_seconds",
        "keep_session_at_max",
        "keep_session_hard_seconds",
        "stop_policy",
        "context_warn_tokens",
        "allow_compact",
        "auto_compact_tokens",
        "context_handoff",
        "recipients",
    }
    unknown = set(document) - allowed
    if unknown:
        _fail(f"unknown configuration keys {sorted(unknown)}")
    builtin = _texts(document.get("builtin_tools", []), "builtin_tools")
    for name in builtin:
        if BUILTIN_PATTERN.fullmatch(name) is None:
            _fail(f"built-in tool name {name!r} is malformed")
    patterns = _texts(document.get("allowed_tools", []), "allowed_tools")
    for pattern in patterns:
        if (
            BUILTIN_PATTERN.fullmatch(pattern) is None
            and TOOL_PATTERN.fullmatch(pattern) is None
        ):
            _fail(f"allowed tool {pattern!r} is neither a tool name nor mcp__server__*")
    policy_text = document.get("tool_policy", ToolPolicy.ALLOWLIST.value)
    try:
        policy = ToolPolicy(_text(policy_text, "tool_policy"))
    except ValueError:
        _fail(f"unknown tool_policy {policy_text!r}")
    inherit = document.get("inherit_account_mcp", False)
    if type(inherit) is not bool:
        _fail("inherit_account_mcp must be a boolean")
    sandbox: SandboxConfig | None = None
    if "sandbox" in document:
        try:
            sandbox = parse_sandbox(document["sandbox"], operator=True)
        except SandboxError as error:
            _fail(str(error))
    settings: SettingsConfig | None = None
    if "settings" in document:
        try:
            settings = parse_settings(document["settings"])
        except SettingsError as error:
            _fail(str(error))
    effort = document.get("effort")
    if effort is not None and effort not in EFFORTS:
        _fail(f"effort must be one of {list(EFFORTS)}, found {effort!r}")
    prompt = document.get("claude_code_prompt", False)
    if type(prompt) is not bool:
        _fail("claude_code_prompt must be a boolean")
    instructions = _texts(document.get("instructions", []), "instructions")
    if len(instructions) > MAX_INSTRUCTION_FILES:
        _fail(f"instructions names more than {MAX_INSTRUCTION_FILES} files")
    if len(set(instructions)) != len(instructions):
        _fail("instructions names a file twice")
    workdir = _workdir(document["workdir"]) if "workdir" in document else None
    workdir_access = _choice(
        document, "workdir_access", WorkdirAccess, WorkdirAccess.WRITE
    )
    writable_paths = _texts(document.get("writable_paths", []), "writable_paths")
    if len(writable_paths) != len(set(writable_paths)):
        _fail("writable_paths names a directory twice")
    for path in writable_paths:
        if not Path(path).is_absolute():
            _fail(f"writable path {path!r} must be absolute")
    report_directory = (
        _report_directory(document["report_directory"])
        if "report_directory" in document
        else None
    )
    project_settings = document.get("project_settings", False)
    if type(project_settings) is not bool:
        _fail("project_settings must be a boolean")
    plugins = _texts(document.get("plugins", []), "plugins")
    if len(plugins) > MAX_PLUGINS:
        _fail(f"plugins names more than {MAX_PLUGINS} directories")
    if len(set(plugins)) != len(plugins):
        _fail("plugins names a directory twice")
    for plugin in plugins:
        if "\x00" in plugin or not (
            plugin.startswith("/") or plugin == "~" or plugin.startswith("~/")
        ):
            _fail(f"plugin {plugin!r} must be an absolute path or start with ~/")
    keep = document.get("keep_session", False)
    if type(keep) is not bool:
        _fail("keep_session must be a boolean")
    fresh_session = document.get("fresh_session_each_turn", False)
    if type(fresh_session) is not bool:
        _fail("fresh_session_each_turn must be a boolean")
    handoff = document.get("context_handoff", True)
    if type(handoff) is not bool:
        _fail("context_handoff must be a boolean")
    if fresh_session and keep:
        _fail("fresh_session_each_turn cannot be combined with keep_session")
    if fresh_session and "context_handoff" in document and handoff:
        _fail("fresh_session_each_turn requires context_handoff to be false")
    idle = _seconds(document, "keep_session_idle_seconds", DEFAULT_KEEP_IDLE_SECONDS)
    ceiling = _seconds(document, "keep_session_max_seconds", DEFAULT_KEEP_MAX_SECONDS)
    if ceiling < idle:
        _fail(
            "keep_session_max_seconds must not be less than keep_session_idle_seconds"
        )
    at_max = _choice(document, "keep_session_at_max", AtMax, AtMax.CLOSE)
    hard = document.get("keep_session_hard_seconds", DEFAULT_KEEP_HARD_SECONDS)
    if type(hard) is not int or not MIN_KEEP_SECONDS <= hard <= MAX_KEEP_HARD_SECONDS:
        _fail(
            f"keep_session_hard_seconds must be a whole number from {MIN_KEEP_SECONDS}"
            f" to {MAX_KEEP_HARD_SECONDS}"
        )
    if hard < ceiling:
        _fail(
            "keep_session_hard_seconds must not be less than keep_session_max_seconds"
        )
    stop_policy = _choice(document, "stop_policy", StopPolicy, StopPolicy.KILL_ALL)
    warn = document.get("context_warn_tokens", DEFAULT_CONTEXT_WARN_TOKENS)
    if (
        type(warn) is not int
        or not MIN_CONTEXT_WARN_TOKENS <= warn <= MAX_CONTEXT_WARN_TOKENS
    ):
        _fail(
            f"context_warn_tokens must be a whole number from {MIN_CONTEXT_WARN_TOKENS}"
            f" to {MAX_CONTEXT_WARN_TOKENS}"
        )
    compact = document.get("allow_compact", False)
    if type(compact) is not bool:
        _fail("allow_compact must be a boolean")
    auto = document.get("auto_compact_tokens")
    if auto is not None and (
        type(auto) is not int
        or not MIN_CONTEXT_WARN_TOKENS <= auto <= MAX_CONTEXT_WARN_TOKENS
    ):
        _fail(
            f"auto_compact_tokens must be a whole number from {MIN_CONTEXT_WARN_TOKENS}"
            f" to {MAX_CONTEXT_WARN_TOKENS}"
        )
    if auto is not None and not compact:
        # An automatic compaction is a compaction the line could not have asked for.
        _fail("auto_compact_tokens needs allow_compact")
    return LineConfig(
        builtin_tools=tuple(sorted(set(builtin))),
        mcp_servers=parse_servers(document.get("mcp_servers", {})),
        tool_policy=policy,
        allowed_tools=tuple(sorted(set(patterns))),
        inherit_account_mcp=inherit,
        sandbox=sandbox,
        settings=settings,
        effort=cast(str | None, effort),
        claude_code_prompt=prompt,
        instructions=instructions,
        workdir=workdir,
        workdir_access=workdir_access,
        writable_paths=tuple(sorted(writable_paths)),
        report_directory=report_directory,
        project_settings=project_settings,
        plugins=plugins,
        keep_session=keep,
        fresh_session_each_turn=fresh_session,
        keep_session_idle_seconds=idle,
        keep_session_max_seconds=ceiling,
        keep_session_at_max=at_max,
        keep_session_hard_seconds=hard,
        stop_policy=stop_policy,
        context_warn_tokens=warn,
        allow_compact=compact,
        auto_compact_tokens=auto,
        context_handoff=False if fresh_session else handoff,
        recipients=(
            _recipients(document["recipients"]) if "recipients" in document else None
        ),
    )


def _recipients(value: object) -> tuple[str, ...]:
    """Addresses, local or remote, each written canonically, distinct and sorted. An
    empty list is allowed, and leaves the line only its notes to itself."""
    found: set[str] = set()
    for text in _texts(value, "recipients"):
        try:
            address = parse_any_address(text)
        except InvalidField as error:
            _fail(f"recipients: {text!r} is not an address, {error}")
        found.add(str(address))
    return tuple(sorted(found))


def decode_config(text: str) -> LineConfig:
    """The configuration stored as ``text``, checked strictly."""
    try:
        document: object = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except ConfigError:
        raise
    except (ValueError, RecursionError) as error:
        raise ConfigError(f"the configuration is not valid JSON: {error}") from error
    fields = dict(_object(document, "the configuration"))
    version = fields.pop("v", None)
    if type(version) is not int or version != CONFIG_VERSION:
        _fail(f"unsupported configuration version {version!r}")
    return config_from_document(fields)
