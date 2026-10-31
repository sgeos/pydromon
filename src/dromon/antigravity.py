"""The Antigravity engine adapter, which runs a turn through Antigravity's ACP runtime.

**Unverified, and the runtime is not installed.** Rewritten on 2026-10-26, on the
operator's decision of that day, to drive Google's Antigravity ACP runtime,
``agy_acp_server``, instead of ``agy --print``. The evidence is the Agent Client
Protocol's schema and a second implementation, T3 Code, which runs this runtime for
real. Neither is a live test by Dromon. ``docs/protocol/ANTIGRAVITY.md`` records what
is established, how, and what to check first.

Dromon drives the runtime from outside, as it drives Grok Build, through the shared
client in ``dromon.acp``. Each turn starts the runtime in a process group of its own,
sends ``initialize`` and ``authenticate``, opens or resumes the line's session with
Dromon's tools as its MCP server, and sends one ``session/prompt``.

Installation. Dromon downloads and installs nothing. The operator installs the runtime,
its ``localharness_external`` helper beside it, and signs it in. Dromon finds it at the
path ``DROMON_ANTIGRAVITY_RUNTIME`` names, or at ``DEFAULT_RUNTIME`` under the home
directory, and refuses a missing, incomplete or older runtime before starting anything.
The runtime keeps its profile, sign-in and configuration under ``GEMINI_HOME``, which
Dromon sets to the ``profile`` directory beside the runtime.

Home. **Antigravity review, F7.** The runtime starts with ``HOME`` set to an empty
directory of the turn's own and with ``AGY_ACP_FORCE_FILE_STORAGE`` set, so that its
sign-in is a file in the profile rather than an entry in the keychain, and files the
filter cannot see, such as a cloud sign-in under ``~/.config/gcloud``, are not found
through ``HOME``. **Hypothesis**, from a second implementation, which sets the same
variable and reads the sign-in from the profile.

Isolation. Dromon serves no file system and no terminal, gives the line no built-in
tool, and puts the session in the ``default`` permission mode. Every permission request
is refused unless it is for one of Dromon's own tools, and a tool call that is not one
of them cancels and refuses the turn. Every tool call and every update to it is
checked, whatever its kind, and a call whose name, title or kind changes refuses the
turn. A tool is one of Dromon's own only by its structured ``toolName``, never by its
title. **Inference.** A tool that asks first is denied before it runs. One the runtime
runs without asking is stopped only after it starts, since its documentation does not
say which do not ask. So a daemon still refuses Antigravity lines, and ``dromon probe
--accept-partial-isolation`` runs one turn.

Credentials. Dromon chooses the Google account sign-in, ``oauth-personal``, from the
methods the runtime advertises, refuses a runtime that does not offer it and a profile
set to another method, and starts the runtime without any key or cloud variable. The
runtime keeps its own sign-in, which Dromon never reads. The runtime does not report
which credential it then uses, so the credential stays ``unverified``, put to the
driver before the runtime starts, and only ``dromon probe
--accept-unverified-credential`` puts the subscription in its place.

Usage. **Antigravity review, F9.** A ``usage_update`` is counted where its shape allows,
its cost as a running total for the session and its tokens in context as the context,
and the report labels both as unverified. A turn in which the runtime compacts its
session is refused unless a cost is reported after the compaction.

Porting note. JSON lines over a pipe, sequential, as the other adapters.
"""

import asyncio
import json
import math
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from claude_agent_sdk import SdkMcpTool

from dromon import __version__, acp
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
from dromon.engine import (
    DIFFERENCE_SOURCE,
    CostMark,
    EngineRefused,
    TurnReport,
    read_instructions,
    turn_cost,
)
from dromon.jsonlines import (
    TURN_SECONDS,
    Connection,
    ConnectionFactory,
    NotJsonLine,
    json_lines_factory,
)
from dromon.lineconfig import DEFAULT_CONFIG, LineConfig, WorkdirAccess
from dromon.launcher import STOP_POLICY_VARIABLE
from dromon.processes import MARK_VARIABLE
from dromon.engine_environment import BASE_ENVIRONMENT, allowlisted
from dromon import gitssh
from dromon.gitssh import GitSshAccess

ENGINE_NAME: Final = "antigravity"
"""The engine this adapter runs, as drivers name it."""

RUNTIME_VARIABLE: Final = "DROMON_ANTIGRAVITY_RUNTIME"
"""The variable through which the operator names the runtime executable."""

DEFAULT_RUNTIME: Final = (
    Path(".local") / "share" / "agy-acp-server" / "agy_acp_server.par"
)
"""Where Dromon looks for the runtime when no path is configured, relative to the home
directory. Dromon's own convention, since Google documents no install location."""

HARNESS_NAME: Final = "localharness_external"
"""The helper that must sit beside the runtime, at the same version."""

PROFILE_NAME: Final = "profile"
"""The runtime's profile directory, beside the runtime, which Dromon sets as
``GEMINI_HOME`` so that the runtime reads only what Dromon checks."""

AGENT_NAME: Final = "antigravity-acp"
"""The name the runtime gives itself in ``initialize``. **Inference** from a second
implementation, which writes this name for the runtime."""

MINIMUM_VERSION: Final = (1, 3, 0)
"""The oldest runtime the adapter accepts. **Hypothesis, Antigravity review, F10.** It
is the version a second implementation downloads, taken from that project's code, which
is unverified third-party material that Google's own release has not confirmed. Older
runtimes are untested by any source Dromon can cite."""

SIGN_IN_METHOD: Final = "oauth-personal"
"""The Google account sign-in. **Inference, Antigravity review, F11.** It draws on the
account's plan rather than billing per token, which no source Dromon can cite shows."""

SIGN_IN_NOTICE: Final = b"Open the following link to authenticate the ACP server: "
"""The plain-text line the runtime writes when it is not signed in, as a second
implementation reads it."""

SIGN_IN_REQUIRED: Final = -32000
"""The error code the runtime answers with when it needs a sign-in, as a second
implementation reads it."""

FIRST_OUTPUT_SECONDS: Final = 180.0
"""Maximum time before Antigravity's first model message, including sign-in."""

ABSOLUTE_TURN_SECONDS: Final = 4 * 60 * 60
"""Absolute four hour cap for one Antigravity turn, including active streams."""

SIGN_IN_REFUSAL: Final = (
    "the Antigravity ACP runtime is not signed in. Sign it in by hand with its Google"
    " account sign-in, as docs/protocol/ANTIGRAVITY.md describes, since Dromon never"
    " handles a sign-in"
)

CREDENTIAL_PREFIXES: Final = ("GEMINI_", "AGY_", "ANTIGRAVITY_")
"""Variables through which Antigravity or a Gemini tool may find a key, a cloud project,
a profile or a sign-in method, besides the cloud variables ``is_cloud_or_credential``
names, which include Google's. A line's runtime starts without any of them."""

HOME_VARIABLES: Final = (
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
)
"""**Antigravity review, F7.** Variables through which the runtime could find files
under the operator's home directory. ``HOME`` is replaced with a directory of the
turn's own, and the others are removed."""

FILE_STORAGE: Final = "AGY_ACP_FORCE_FILE_STORAGE"
"""The variable that keeps the runtime's sign-in in a file under ``GEMINI_HOME``, at
``antigravity-acp/acp_token.json``, instead of the keychain, as a second implementation
sets and reads it. With ``HOME`` replaced, the sign-in must not depend on the home
directory."""

DROMON_CONTROLLED: Final = (
    "BROWSER",
    "TMPDIR",
    "PYTHONUNBUFFERED",
    "ELECTRON_RUN_AS_NODE",
    "DROMON_LINE_TOKEN",
    RUNTIME_VARIABLE,
    *HOME_VARIABLES,
)
"""Variables Dromon sets or clears itself."""

WORKSPACE_DIRECTORIES: Final = (".gemini", ".agy", ".antigravity")
"""**Antigravity review, F3.** Directories in which a project may hold settings,
permissions or MCP servers for Antigravity or the Gemini tools it descends from. Any of
them in the working directory or a directory above it, other than the home directory,
refuses the turn."""

PROFILE_HOOKS: Final = (
    (Path("config") / "hooks.json", "hooks"),
    (Path("config") / "mcp_config.json", "mcpServers"),
)
"""Profile files whose hooks or MCP servers could run before Dromon refuses a tool, and
the key that holds them, as a second implementation reads them. Each must be absent, or
hold that key alone and empty."""

PROFILE_SETTINGS: Final = (
    Path("settings.json"),
    Path("antigravity-cli") / "settings.json",
)
"""Profile settings files, in which a setting could approve a tool, choose a credential
or run a command."""

SETTINGS_KEYS: Final = frozenset({"$schema", "permissions"})
"""**Antigravity review, F6.** The only keys a profile settings file may hold. Any
other refuses the turn, since no source Dromon can cite says what the runtime's other
settings do. The list grows only as each key is shown to change nothing that matters,
as Pi's list of safe settings did."""

PERMISSION_KEYS: Final = frozenset({"allow", "deny"})
"""The keys ``permissions`` may hold, with ``allow`` empty. A denial only narrows."""

PROFILE_AUTH: Final = Path("antigravity-acp") / "settings.json"
"""The runtime's own settings, whose ``auth.type`` names the sign-in method, as a second
implementation writes it. It never holds a credential. It must exist, hold ``auth``
alone, and name ``SIGN_IN_METHOD`` alone, since the method the runtime chooses without
it is not shown."""

KEY_FILE: Final = Path(".env")
"""**Re-review, N2.** A file from which a Gemini tool may read a key the environment
filter cannot see, refused by its existence in the profile or in ``~/.gemini``."""

MAX_SETTINGS_BYTES: Final = 1024 * 1024

ASK_MODES: Final = frozenset({"default"})
"""The permission mode in which the runtime asks before a tool that needs approval."""

KNOWN_UPDATES: Final = frozenset(
    {
        "user_message_chunk",
        "agent_message_chunk",
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
        "plan",
        "plan_update",
        "plan_removed",
        "available_commands_update",
        "current_mode_update",
        "config_option_update",
        "session_info_update",
        "usage_update",
        "compaction_update",
        "compaction_summary_chunk",
    }
)
"""The ``session/update`` kinds of ACP version 1. Any other refuses the turn."""

COMPACTIONS: Final = frozenset({"compaction_update", "compaction_summary_chunk"})
"""Updates that show the runtime compacting its session, which is a model call of its
own whose cost must be counted."""

CURRENCY: Final = "USD"
"""The only currency a reported cost is counted in."""

USAGE_NOTE: Final = (
    "unverified: the cost is the difference of the running total for the session"
    " that the runtime's usage_update reported, and the context is its last count of"
    " tokens in context, as the Agent Client Protocol's schema defines them. Neither"
    " has been observed from the runtime"
)

NO_COST_NOTE: Final = (
    "no cost was seen: the runtime reported no cost in dollars for this turn, so"
    " Dromon recorded none. A subscription would explain this, but it has not been"
    " observed, and a turn billed elsewhere would look the same"
)
"""**Re-review, N5.** A turn with no reported cost is accepted, since that is the
subscription's expected case, and its report says so plainly rather than leave the
gap silent."""

CONTEXT_NOTE: Final = (
    " The context is the runtime's last usage_update count of tokens in context,"
    " which has not been observed from the runtime either"
)

STARTED_STATUSES: Final = frozenset({"in_progress", "completed", "failed"})
"""Tool call statuses that mean the tool has begun, so a refusal came after it."""

STOP_REASONS: Final[dict[str, str]] = {
    "end_turn": "success",
    "cancelled": "interrupted",
    "max_tokens": "error",
    "max_turn_requests": "error",
    "refusal": "error",
}
"""ACP's stop reasons as Dromon's turn subtypes."""

QUESTION_PREFIX: Final = "interaction_"
"""Permission requests whose tool call identifier begins so are the runtime's own
questions to its user, not approvals, as a second implementation reads them."""

SESSION_PATTERN: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
VERSION_PATTERN: Final = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:[^0-9].*)?")
ABORT_SECONDS: Final = 60
"""How long a cancelled turn waits for the runtime to answer its prompt before the
runtime is stopped."""

ISOLATION_REFUSAL: Final = (
    "Antigravity runs only in dromon probe with --accept-partial-isolation. Its ACP"
    " runtime asks Dromon before a tool only when the tool needs approval, and its"
    " documentation does not say which built-in tools run without approval, so"
    " Dromon cannot show that a line's reads stay away from its state and the"
    " operator's credentials, and could only refuse a turn after such a tool started"
)


def unsupported(config: LineConfig) -> str | None:
    """Why an Antigravity line cannot run ``config``, or ``None`` if it can."""
    if config.workdir_access is WorkdirAccess.READ:
        return "Antigravity cannot confine a line to a read-only working directory"
    if config.builtin_tools:
        return (
            "Antigravity lines take no built-in tools, because Dromon cannot show that"
            " they stay away from its state or the operator's credentials"
        )
    if config.mcp_servers or config.inherit_account_mcp:
        return "Antigravity lines take only Dromon's own MCP server"
    if config.claude_code_prompt:
        return "Antigravity lines run on Antigravity's own prompt, not Claude Code's"
    if config.project_settings:
        return "Antigravity lines do not load Claude Code's project settings"
    if config.plugins:
        return "Antigravity lines do not load Claude Code plugins"
    if config.allow_compact:
        return "Antigravity lines cannot compact their session through Dromon"
    if config.keep_session:
        return "Antigravity lines do not keep their engine between turns"
    if config.sandbox is not None or config.settings is not None:
        return "Antigravity lines take no sandbox or settings document"
    return None


def line_refusal(config: LineConfig) -> str | None:
    """Why a daemon cannot hold an Antigravity line with ``config``. A daemon refuses
    every such line for the isolation reason, which only ``dromon probe`` may pass."""
    return unsupported(config) or ISOLATION_REFUSAL


def engine_environment(
    environment: Mapping[str, str], strategy: Strategy
) -> dict[str, str]:
    """The environment the runtime starts with, before Dromon adds its own variables,
    without any credential, profile or sign-in variable. Raises ``EngineRefused`` for
    any strategy but a subscription."""
    if strategy is not Strategy.SUBSCRIPTION:
        raise EngineRefused(
            "Antigravity lines support only the subscription strategy, because Dromon"
            " chooses the runtime's Google account sign-in and refuses its key methods"
        )
    return allowlisted(environment, BASE_ENVIRONMENT)


@dataclass(frozen=True, slots=True)
class Runtime:
    """An installed runtime, as Dromon found it."""

    executable: Path
    harness: Path
    profile: Path


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def locate_runtime(environment: Mapping[str, str], home: Path) -> Runtime:
    """The runtime the operator installed, or ``EngineRefused`` naming what is missing.
    The message names the ACP runtime, since Antigravity's app or ``agy`` command does
    not provide it."""
    configured = environment.get(RUNTIME_VARIABLE)
    path = Path(configured).expanduser() if configured else home / DEFAULT_RUNTIME
    if not path.is_absolute():
        raise EngineRefused(f"{RUNTIME_VARIABLE} must be an absolute path, not {path}")
    if not _executable(path):
        raise EngineRefused(
            f"the Antigravity ACP runtime is missing: no executable agy_acp_server at"
            f" {path}. Antigravity's app and its agy command do not provide it. Install"
            f" it as docs/protocol/ANTIGRAVITY.md describes, or name it with"
            f" {RUNTIME_VARIABLE}"
        )
    harness = path.parent / HARNESS_NAME
    if not _executable(harness):
        raise EngineRefused(
            f"the Antigravity ACP runtime at {path} is incomplete: its {HARNESS_NAME}"
            " helper is not beside it"
        )
    profile = path.parent / PROFILE_NAME
    if not profile.is_dir():
        raise EngineRefused(
            f"the Antigravity ACP runtime has no profile at {profile}, so it has not"
            " been signed in. Sign it in by hand, as docs/protocol/ANTIGRAVITY.md"
            " describes"
        )
    return Runtime(path, harness, profile)


def runtime_version(text: object) -> tuple[int, int, int] | None:
    """The version a runtime reports, or ``None`` when it is not ``X.Y.Z``."""
    if not isinstance(text, str):
        return None
    match = VERSION_PATTERN.fullmatch(text)
    if match is None:
        return None
    return int(match[1]), int(match[2]), int(match[3])


def check_workspace(workspace: Path, home: Path) -> None:
    """Refuse a workspace where Antigravity might load settings Dromon has not read, by
    the existence of ``WORKSPACE_DIRECTORIES`` alone."""
    directory = Path(os.path.realpath(workspace))
    own = Path(os.path.realpath(home))
    found = [
        str(candidate / name)
        for candidate in (directory, *directory.parents)
        if candidate != own
        for name in WORKSPACE_DIRECTORIES
        if os.path.lexists(candidate / name)
    ]
    if found:
        raise EngineRefused(
            "Antigravity might load settings Dromon has not read: " + ", ".join(found)
        )


def _read_object(path: Path) -> dict[str, Any] | None:
    """The JSON object at ``path``, ``None`` when nothing is there, or
    ``EngineRefused`` when it cannot be read as one."""
    if not os.path.lexists(path):
        return None
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SETTINGS_BYTES + 1)
    except OSError as error:
        raise EngineRefused(f"cannot read {path}: {error}") from error
    if len(raw) > MAX_SETTINGS_BYTES:
        raise EngineRefused(f"{path} exceeds {MAX_SETTINGS_BYTES} bytes")
    try:
        document: object = json.loads(raw)
    except ValueError as error:
        raise EngineRefused(f"{path} is not JSON") from error
    if not isinstance(document, dict):
        raise EngineRefused(f"{path} is not a JSON object")
    return cast(dict[str, Any], document)


def check_profile(profile: Path, home: Path) -> None:
    """Refuse a profile whose files could give the runtime a key, a hook, an MCP server,
    an approval or a sign-in method Dromon did not choose. Dromon reads only these
    files, never the runtime's sign-in. The ``.env`` in the real ``~/.gemini`` is
    still refused although ``HOME`` is replaced, in case the runtime finds the home
    directory from the account rather than from ``HOME``."""
    for key in (profile / KEY_FILE, home / ".gemini" / KEY_FILE):
        if os.path.lexists(key):
            raise EngineRefused(
                f"{key} exists, from which Antigravity might read a key billed per"
                " token that Dromon cannot see"
            )
    for relative, name in PROFILE_HOOKS:
        document = _read_object(profile / relative)
        if document is None:
            continue
        if set(document) - {name} or document.get(name):
            raise EngineRefused(
                f"{profile / relative} declares {name} or another key, which could run"
                " before Dromon refuses a tool"
            )
    for relative in PROFILE_SETTINGS:
        document = _read_object(profile / relative)
        if document is not None:
            _check_settings(profile / relative, document)
    path = profile / PROFILE_AUTH
    document = _read_object(path)
    if document is None:
        raise EngineRefused(
            f"{path} is missing, so the sign-in method the runtime would choose is not"
            f" shown. Create it naming {SIGN_IN_METHOD}, as"
            " docs/protocol/ANTIGRAVITY.md describes"
        )
    auth: object = document.get("auth")
    fields = cast(dict[str, Any], auth) if isinstance(auth, dict) else {}
    if set(document) != {"auth"} or set(fields) != {"type"}:
        raise EngineRefused(
            f"{path} holds keys other than auth.type, such as a cloud project, which"
            f" Dromon has not reviewed, so it does not name {SIGN_IN_METHOD} alone"
        )
    if fields["type"] != SIGN_IN_METHOD:
        raise EngineRefused(
            f"{path} sets the sign-in method {fields['type']!r}, not {SIGN_IN_METHOD}"
        )


def _check_settings(path: Path, document: Mapping[str, Any]) -> None:
    """Refuse a profile settings file that holds a key outside ``SETTINGS_KEYS``, or
    permissions that approve a tool."""
    unknown = sorted(set(document) - SETTINGS_KEYS)
    if unknown:
        raise EngineRefused(
            f"{path} sets {', '.join(repr(key) for key in unknown)}, which Dromon has"
            " not reviewed, so it might approve a tool, choose a credential or run a"
            " command"
        )
    permissions: object = document.get("permissions", {})
    if not isinstance(permissions, dict):
        raise EngineRefused(f"{path} has permissions that are not an object")
    granted = cast(dict[str, Any], permissions)
    unknown = sorted(set(granted) - PERMISSION_KEYS)
    if unknown:
        raise EngineRefused(
            f"{path} sets permissions {', '.join(repr(key) for key in unknown)}, which"
            " Dromon has not reviewed"
        )
    if granted.get("allow"):
        raise EngineRefused(
            f"{path} approves tools in permissions.allow, which would reach a line"
            " Dromon did not give them to"
        )


@dataclass(slots=True)
class _Turn:
    text: list[str] = field(default_factory=list[str])
    tool_uses: list[str] = field(default_factory=list[str])
    calls: dict[str, dict[str, object]] = field(
        default_factory=dict[str, dict[str, object]]
    )
    """Each tool call's identity as first recorded, by its identifier."""
    finished: set[str] = field(default_factory=set[str])
    """Tool calls whose update reported them finished, so each result is fed to the
    driver once."""
    subtype: str | None = None
    interrupted_for: str | None = None
    refusal: str | None = None
    cancelled: bool = False
    cost_total: float | None = None
    """The session's running cost, from its latest ``usage_update`` in dollars."""
    context: int | None = None
    """The tokens in context, from the latest ``usage_update``."""
    uncounted_compaction: bool = False
    """Whether the runtime compacted with no cost reported since."""
    first_output_seen: bool = False
    absolute_deadline: float | None = None


class AntigravityLine:
    """One session line on the Antigravity engine, with its driver."""

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
        unverified_credential: bool = False,
        turn_seconds: float = TURN_SECONDS,
        first_output_seconds: float = FIRST_OUTPUT_SECONDS,
        absolute_turn_seconds: float = ABSOLUTE_TURN_SECONDS,
        cost_mark: CostMark | None = None,
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
        """How the runtime names the line's own tools."""
        self.session_id = session_id
        self.bridge = tuple(bridge)
        self.line_token = line_token
        self._environment = os.environ if environment is None else environment
        self._home = Path.home() if home is None else home
        self._connect = connection_factory
        self._observe = observe
        self.mark = mark
        self.git_ssh = git_ssh
        """The agent and pinned hosts of the project's ``[git_ssh]`` table, if the
        line was given them."""
        """The process mark every process of this line inherits, if issued."""
        self.partial_isolation = partial_isolation
        """Whether the operator accepts that a tool the runtime runs without asking is
        stopped only after it starts. Until then every turn is refused."""
        self.unverified_credential = unverified_credential
        """Whether the operator has confirmed by hand that the runtime is signed in to
        a plan and does not bill per token, so the driver is given the subscription in
        place of the unverified credential. Until then every turn is refused."""
        self.turn_seconds = turn_seconds
        self.absolute_turn_seconds = absolute_turn_seconds
        """How long a turn may run before the runtime is stopped and the turn
        refused."""
        self.cost_mark = cost_mark
        """The session and running cost the runtime last reported, from which the next
        turn's cost is the difference."""
        self._client: acp.Client | None = None
        self._session: str | None = None
        self._stage = "startup"
        self.first_output_seconds = first_output_seconds

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
                "Antigravity names a model's effort in the model itself, so an"
                " Antigravity line takes its driver's effort"
            )
        environment = engine_environment(self._environment, settings.strategy)
        environment.update(gitssh.environment(self.git_ssh))
        self._credential()
        runtime = locate_runtime(self._environment, self._home)
        check_workspace(self.cwd, self._home)
        check_profile(runtime.profile, self._home)
        if self.session_id is not None and not SESSION_PATTERN.fullmatch(
            self.session_id
        ):
            raise EngineRefused(
                f"the line's Antigravity session {self.session_id!r} is invalid"
            )
        browser = shutil.which("true", path=environment.get("PATH")) or "/usr/bin/true"
        if not _executable(Path(browser)):
            raise EngineRefused("no true command to stand in for a sign-in browser")
        if self.mark is not None:
            environment[MARK_VARIABLE] = self.mark
        environment[STOP_POLICY_VARIABLE] = self.config.stop_policy.value
        connect = self._connect or json_lines_factory(
            str(runtime.executable), keep_input=True
        )
        turn = _Turn()
        with tempfile.TemporaryDirectory(prefix="dromon-antigravity-") as directory:
            own_home = Path(directory) / "home"
            scratch = Path(directory) / "tmp"
            own_home.mkdir(mode=0o700)
            scratch.mkdir(mode=0o700)
            environment |= {
                "GEMINI_HOME": str(runtime.profile),
                "ANTIGRAVITY_HARNESS_PATH": str(runtime.harness),
                # Antigravity review, F7. The operator's home directory is out of
                # reach through HOME, and the sign-in is a file in the profile.
                "HOME": str(own_home),
                FILE_STORAGE: "1",
                # A browser the runtime opens for a sign-in would bypass the operator,
                # so its sign-in address goes to a command that does nothing.
                "BROWSER": browser,
                "PYTHONUNBUFFERED": "1",
                "ELECTRON_RUN_AS_NODE": "1",
                # The runtime unpacks itself on each launch, so it does so in a
                # directory of the turn's own, removed with the turn.
                "TMPDIR": str(scratch),
            }
            async with connect([], self.cwd, environment) as connection:
                await self._run(connection, prompt, turn)
        return self._report(turn)

    def _credential(self) -> None:
        """Put the credential to the driver before the runtime starts."""
        credential = (
            Credential.SUBSCRIPTION
            if self.unverified_credential
            else Credential.UNVERIFIED
        )
        verdict = self.driver.step(Event("credential", credential.value))
        if verdict.tag is not ActionTag.REFUSE:
            return
        if credential is Credential.UNVERIFIED:
            raise EngineRefused(
                f"the {verdict.text} driver refuses the unverified credential. The"
                " Antigravity ACP runtime does not report which credential it uses, so"
                " Dromon cannot tell its sign-in from a key before it bills. dromon"
                " probe --accept-unverified-credential runs one turn once the operator"
                " has confirmed by hand that the runtime is signed in to a plan"
            )
        raise EngineRefused(
            f"the {verdict.text} driver refuses the subscription the operator confirmed"
        )

    async def _run(self, connection: Connection, prompt: str, turn: _Turn) -> None:
        self._stage = "initialize"

        async def dispatch(message: dict[str, Any]) -> None:
            await self._dispatch(message, turn)

        client = acp.Client(connection, dispatch)
        client.deadline = asyncio.get_running_loop().time() + self.first_output_seconds
        self._client = client
        try:
            await self._converse(prompt, turn)
        except TimeoutError:
            if not turn.cancelled:
                if not turn.first_output_seen:
                    turn.refusal = (
                        "Antigravity timed out during "
                        f"{self._stage} before its first output after "
                        f"{self.first_output_seconds:g} seconds"
                    )
                else:
                    absolute = (
                        turn.absolute_deadline is not None
                        and asyncio.get_running_loop().time() >= turn.absolute_deadline
                    )
                    await self._cancel(
                        turn,
                        (
                            "the turn reached its absolute cap of "
                            f"{self.absolute_turn_seconds:g} seconds"
                            if absolute
                            else "the turn had no activity for "
                            f"{self.turn_seconds:g} seconds"
                        ),
                    )
            elif turn.refusal is None and turn.interrupted_for is None:
                turn.refusal = "the engine did not finish cancelling its turn"
            await connection.stop()
        except NotJsonLine as error:
            if error.head.startswith(SIGN_IN_NOTICE):
                raise EngineRefused(SIGN_IN_REFUSAL) from error
            raise
        except acp.RpcError as error:
            if error.code == SIGN_IN_REQUIRED:
                raise EngineRefused(SIGN_IN_REFUSAL) from error
            raise EngineRefused(f"the engine refused a request: {error}") from error
        finally:
            self._client = None
            self._session = None

    async def _converse(self, prompt: str, turn: _Turn) -> None:
        client = self._rpc()
        self._stage = "initialize"
        start = await client.call(
            "initialize",
            {
                "protocolVersion": acp.PROTOCOL_VERSION,
                "clientCapabilities": acp.CLIENT_CAPABILITIES,
                "clientInfo": {"name": "dromon", "version": __version__},
            },
        )
        refusal = _initialize_refusal(start)
        if refusal is not None:
            turn.refusal = refusal
            return
        self._stage = "sign-in"
        await client.call("authenticate", {"methodId": SIGN_IN_METHOD})
        self._stage = "session setup"
        result = await self._open_session(start, turn)
        if turn.refusal is not None or self._session is None:
            return
        refusal = await acp.ensure_mode(
            client, self._session, result, ASK_MODES, "default"
        )
        if refusal is not None:
            turn.refusal = refusal
            return
        model = await self._choose_model(result)
        verdict = self.driver.step(Event("init", model or ""))
        if verdict.tag is ActionTag.REFUSE:
            turn.refusal = (
                f"the driver for {verdict.text} refuses the engine model {model!r}"
            )
            return
        text = prompt
        if self.session_id is None:
            instructions = read_instructions(self.config.instructions, self.cwd)
            text = f"{instructions}\n\n{prompt}" if instructions else prompt
        self.driver.step(Event("turn_start"))
        turn.absolute_deadline = (
            asyncio.get_running_loop().time() + self.absolute_turn_seconds
        )
        self._stage = "first model output"
        answer = await client.call(
            "session/prompt",
            {"sessionId": self._session, "prompt": [{"type": "text", "text": text}]},
        )
        reason = answer.get("stopReason")
        turn.subtype = STOP_REASONS.get(str(reason))
        if turn.subtype is None and turn.refusal is None:
            turn.refusal = f"the engine ended its turn for {reason!r}"
        if self.session_id is None:
            self.session_id = self._session

    async def _open_session(
        self, start: Mapping[str, Any], turn: _Turn
    ) -> dict[str, Any]:
        params: dict[str, object] = {
            "cwd": str(self.cwd),
            "mcpServers": acp.mcp_servers(self.bridge, self.line_token),
        }
        client = self._rpc()
        if self.session_id is None:
            result = await client.call("session/new", params)
            session = result.get("sessionId")
            if not isinstance(session, str) or not SESSION_PATTERN.fullmatch(session):
                turn.refusal = f"the engine opened an invalid session {session!r}"
                return result
            self._session = session
            return result
        capabilities = cast(dict[str, Any], start.get("agentCapabilities") or {})
        sessions = cast(dict[str, Any], capabilities.get("sessionCapabilities") or {})
        if sessions.get("resume") is None:
            turn.refusal = "the engine does not offer to resume a session"
            return {}
        self._session = self.session_id
        return await client.call(
            "session/resume", {**params, "sessionId": self.session_id}
        )

    async def _choose_model(self, result: Mapping[str, Any]) -> str | None:
        """The model the session runs, after selecting the driver's model when the
        session offers it and runs another. The driver judges the result."""
        reported = acp.reported_model(result)
        wanted = harness_id(self.driver.settings.model)
        if reported == wanted or wanted not in acp.model_choices(result):
            return reported
        changed = await self._rpc().call(
            "session/set_config_option",
            {"sessionId": self._session, "configId": "model", "value": wanted},
        )
        return acp.reported_model(changed)

    def _report(self, turn: _Turn) -> TurnReport:
        # A refused turn leaves the mark where it was, so that the next turn's
        # difference still covers what this one cost.
        if turn.refusal is not None:
            raise EngineRefused(turn.refusal)
        if turn.uncounted_compaction:
            raise EngineRefused(
                "the engine compacted its session and reported no cost after it, so"
                " Dromon cannot count what the compaction cost"
            )
        session = self.session_id or ""
        cost = turn_cost(turn.cost_total, session, self.cost_mark)
        if turn.cost_total is not None:
            self.cost_mark = (session, turn.cost_total)
        if turn.interrupted_for is not None:
            subtype = "interrupted"
        else:
            subtype = turn.subtype or "error"
        self.driver.step(Event("result", subtype, 1))
        if cost is not None:
            note = USAGE_NOTE
        elif turn.context is not None:
            note = NO_COST_NOTE + "." + CONTEXT_NOTE
        else:
            note = NO_COST_NOTE
        return TurnReport(
            subtype=subtype,
            num_turns=1,
            text="".join(turn.text),
            tool_uses=tuple(turn.tool_uses),
            interrupted_for=turn.interrupted_for,
            credential=Credential.UNVERIFIED,
            session_id=session,
            capabilities=None,
            cost_estimate_usd=cost,
            cost_mark=None if turn.cost_total is None else self.cost_mark,
            cost_source=None if cost is None else DIFFERENCE_SOURCE,
            context_tokens=turn.context,
            usage_note=note,
        )

    # The protocol.

    def _rpc(self) -> acp.Client:
        if self._client is None:
            raise EngineRefused("the engine connection is not open")
        return self._client

    async def _dispatch(self, message: dict[str, Any], turn: _Turn) -> None:
        method = message.get("method")
        if not isinstance(method, str):
            await self._cancel(turn, "the engine sent a message Dromon did not expect")
            return
        params = cast(dict[str, Any], message.get("params") or {})
        if "id" in message:
            if method == "session/request_permission":
                await self._permission(message["id"], params, turn)
                return
            await self._rpc().refuse(message["id"], method)
            await self._cancel(
                turn, f"the engine asked for {method}, which Dromon does not serve"
            )
        elif method == "session/update":
            await self._update(cast(dict[str, Any], params.get("update") or {}), turn)
        else:
            await self._cancel(
                turn, f"the engine sent a notification Dromon does not know: {method}"
            )

    async def _permission(
        self, request_id: object, params: dict[str, Any], turn: _Turn
    ) -> None:
        raw: object = params.get("toolCall")
        tool_call = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        options = params.get("options")
        identifier = tool_call.get("toolCallId")
        question = isinstance(identifier, str) and identifier.startswith(
            QUESTION_PREFIX
        )
        if question or turn.cancelled:
            # ACP requires every request pending after a cancel to be answered as
            # cancelled, which grants nothing.
            await self._rpc().respond(request_id, {"outcome": {"outcome": "cancelled"}})
            if not turn.cancelled:
                await self._cancel(
                    turn, "the engine asked a question Dromon cannot answer"
                )
            return
        refusal = acp.identity_refusal(identifier, tool_call, turn.calls)
        # With no field changed, the call is what was recorded plus what the request
        # adds, so a request that omits the recorded name still names the call.
        known = turn.calls.get(str(identifier), {})
        permitted = (
            refusal is None and acp.tool_name({**known, **tool_call}) in self.own_tools
        )
        response, missing_option = acp.permission_result(
            options, permitted, acp.tool_label(tool_call)
        )
        await self._rpc().respond(request_id, response)
        if missing_option is not None:
            refusal = missing_option
        if refusal is not None:
            await self._cancel(turn, refusal)
            return
        await self._record(str(identifier), tool_call, turn)
        if not permitted:
            await self._cancel(
                turn,
                "the engine asked to use a tool the line was not given, which Dromon"
                f" refused before it ran: {acp.tool_label(tool_call)}",
            )

    async def _update(self, update: dict[str, Any], turn: _Turn) -> None:
        kind = update.get("sessionUpdate")
        if kind not in KNOWN_UPDATES:
            await self._cancel(
                turn, f"the engine sent an update Dromon does not know: {kind!r}"
            )
            return
        if kind in (
            "agent_message_chunk",
            "agent_thought_chunk",
            "tool_call",
            "tool_call_update",
        ):
            turn.first_output_seen = True
            if self._client is not None:
                now = asyncio.get_running_loop().time()
                absolute = turn.absolute_deadline or (now + self.turn_seconds)
                self._client.deadline = min(now + self.turn_seconds, absolute)
        if kind == "agent_message_chunk":
            content = cast(dict[str, Any], update.get("content") or {})
            text = content.get("text")
            if content.get("type") == "text" and isinstance(text, str) and text:
                turn.text.append(text)
                self._observe(ActivityKind.TEXT, text, chunk=True)
                await self._act(Event("text", "", len(text)), turn)
        elif kind == "agent_thought_chunk":
            self._observe(ActivityKind.THINKING, "")
            await self._act(Event("thinking"), turn)
        elif kind in ("tool_call", "tool_call_update"):
            await self._tool(update, turn)
        elif kind == "current_mode_update":
            mode = update.get("currentModeId")
            if mode not in ASK_MODES:
                await self._cancel(
                    turn, f"the engine switched to the permission mode {mode!r}"
                )
        elif kind == "config_option_update":
            # Antigravity review, F5. Any model value but the driver's refuses.
            wanted = harness_id(self.driver.settings.model)
            for value in acp.model_values(update):
                if value != wanted:
                    await self._cancel(
                        turn, f"the engine switched to the model {value!r}"
                    )
        elif kind == "usage_update":
            _count_usage(update, turn)
        elif kind in COMPACTIONS:
            turn.uncounted_compaction = True

    async def _tool(self, update: dict[str, Any], turn: _Turn) -> None:
        """Check a tool call or an update to one. Antigravity review, F1 to F3. Every
        one is checked, whatever its kind and whether or not it was permitted
        before."""
        identifier = update.get("toolCallId")
        refusal = acp.identity_refusal(identifier, update, turn.calls)
        if refusal is not None:
            await self._cancel(turn, refusal)
            return
        await self._record(str(identifier), update, turn)
        recorded = turn.calls[str(identifier)]
        if acp.tool_name(recorded) in self.own_tools:
            if acp.newly_finished(str(identifier), update, turn.finished):
                # The safe point before the next model call that priority mail
                # takes, as ``dromon.priority`` describes.
                await self._act(Event(TOOL_RESULT), turn)
            return
        started = update.get("status") in STARTED_STATUSES or "status" not in update
        when = "after it started" if started else "before it ran"
        await self._cancel(
            turn,
            "the engine used a tool the line was not given, stopped"
            f" {when}: {acp.tool_label(recorded)}",
        )

    async def _record(
        self, identifier: str, fields: Mapping[str, Any], turn: _Turn
    ) -> None:
        """Record a tool call the first time it is seen, and count it then if it is one
        of the line's own tools. ``acp.identity_refusal`` has passed it."""
        if identifier in turn.calls:
            return
        turn.calls[identifier] = acp.identity(fields)
        name = acp.tool_name(fields)
        if name in self.own_tools:
            turn.tool_uses.append(name)
            self._observe(ActivityKind.TOOL, name)
            await self._act(Event("tool_use", name), turn)

    async def _act(self, event: Event, turn: _Turn) -> None:
        action = self.driver.step(event)
        if action.tag is ActionTag.INTERRUPT and turn.interrupted_for is None:
            turn.interrupted_for = action.text
            await self._cancel(turn, None)

    async def _cancel(self, turn: _Turn, refusal: str | None) -> None:
        """Cancel the turn once, and wait at most ``ABORT_SECONDS`` for the runtime to
        answer its prompt before it is stopped."""
        if refusal is not None and turn.refusal is None:
            turn.refusal = refusal
        if turn.cancelled:
            return
        turn.cancelled = True
        client = self._rpc()
        bound = asyncio.get_running_loop().time() + ABORT_SECONDS
        client.deadline = (
            bound if client.deadline is None else min(client.deadline, bound)
        )
        if self._session is not None:
            await client.notify("session/cancel", {"sessionId": self._session})


def _count_usage(update: Mapping[str, Any], turn: _Turn) -> None:
    """Count a ``usage_update`` where its shape allows. **Antigravity review, F9.**
    ACP's schema gives ``used``, the tokens in context, ``size``, the context window,
    and an optional ``cost`` with an ``amount`` and a ``currency``, the session's
    running total. Only a whole, non-negative ``used`` and a finite, non-negative
    amount in dollars are counted, and anything else is left uncounted."""
    used = update.get("used")
    if type(used) is int and used >= 0:
        turn.context = used
    cost = update.get("cost")
    if not isinstance(cost, dict):
        return
    entry = cast(dict[str, Any], cost)
    amount = entry.get("amount")
    if entry.get("currency") != CURRENCY or type(amount) not in (int, float):
        return
    total = float(cast(float, amount))
    if math.isfinite(total) and total >= 0:
        turn.cost_total = total
        turn.uncounted_compaction = False


def _initialize_refusal(start: Mapping[str, Any]) -> str | None:
    """Why the runtime's ``initialize`` answer refuses the turn, or ``None``."""
    if start.get("protocolVersion") != acp.PROTOCOL_VERSION:
        return f"the engine speaks ACP version {start.get('protocolVersion')!r}"
    info = cast(dict[str, Any], start.get("agentInfo") or {})
    if info.get("name") != AGENT_NAME:
        return (
            f"the engine names itself {info.get('name')!r}, not the Antigravity ACP"
            f" runtime {AGENT_NAME}"
        )
    version = runtime_version(info.get("version"))
    if version is None or version < MINIMUM_VERSION:
        wanted = ".".join(str(part) for part in MINIMUM_VERSION)
        return (
            f"the Antigravity ACP runtime reports version {info.get('version')!r}, and"
            f" Dromon needs {wanted} or later"
        )
    methods = {
        str(cast(dict[str, Any], method).get("id"))
        for method in cast(list[Any], start.get("authMethods") or [])
        if isinstance(method, dict)
    }
    if SIGN_IN_METHOD not in methods:
        offered = ", ".join(sorted(methods)) or "none"
        return (
            f"the Antigravity ACP runtime does not offer the Google account sign-in"
            f" {SIGN_IN_METHOD}, only {offered}, and Dromon does not use a key"
        )
    return None
