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

Grok's home. A Grok line runs with ``GROK_HOME`` set to Dromon's own Grok home,
``~/.grok/dromon-home``, never the operator's. Dromon writes its ``config.toml``, which
sets the asking mode and declares no MCP server or hook, and its ``sandbox.toml`` when
either is missing, and before every turn refuses any drift, any policy or trust file
that sets something, and any agents, hooks, plugins, rules, skills or workflows there.
Grok runs with ``HOME`` set to ``USER_HOME`` inside that home, so it cannot read the
operator's Claude Code, Cursor or other harness files, and Dromon refuses a turn when
that directory holds any or a Claude Code settings file there names another mode.
The operator signs Grok in to that home once, by hand, with ``SIGN_IN``.

Sandbox. Grok's custom sandbox profiles live only in ``sandbox.toml`` files, which an
overlay cannot set. Dromon's Grok home defines ``dromon-reader`` and, for a line given
``Edit`` or ``Write``, ``dromon-writer``. Dromon checks before each turn that the
profile denies Dromon's state and runtime directories and that a writer keeps ``.git``
read-only, and selects it with ``GROK_SANDBOX``. Grok documents that a custom
profile it cannot apply stops it from starting. On macOS Grok does not block a child
process's network access under any profile.

Tools and permissions. Grok 1.0.70 reports no permission mode, so Dromon relies on its
home's mode, refuses a turn when a Claude Code settings file Grok reads names another,
refuses a session that reports a mode other than the asking one, refuses one that
loads any MCP server but Dromon's, answers
every permission request from the line's configuration, and
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
import stat
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
from dromon.capabilities import GROK as GROK_CAPABILITIES
from dromon.capabilities import Known
from dromon.driver import TOOL_RESULT, ActionTag, Credential, Driver, Event, Strategy
from dromon.engine import EngineRefused, TurnReport, read_instructions
from dromon import launcher
from dromon.launcher import STOP_POLICY_VARIABLE
from dromon.processes import MARK_VARIABLE
from dromon.engine_environment import BASE_ENVIRONMENT, allowlisted
from dromon import gitssh
from dromon.gitssh import GitSshAccess
from dromon.lineconfig import DEFAULT_CONFIG, LineConfig, WorkdirAccess
from dromon.workdir import git_directories

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

PROFILE_EXTENDS: Final[dict[str, str]] = {
    READER_PROFILE: "read-only",
    WRITER_PROFILE: "workspace",
}

API_ENVIRONMENT: Final = (
    "XAI_API_KEY",
    "GROK_AUTH",
    "GROK_CODE_XAI_API_KEY",
    "GROK_AUTH_PATH",
)
"""Variables that may give Grok a credential other than its cached sign-in."""

HOME_VARIABLES: Final = (
    "HOME",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
)
"""Variables through which Grok could find files under the operator's home directory,
as the Antigravity adapter names them. ``HOME`` is replaced with the line's own, and the
others are removed."""

DROMON_CONTROLLED: Final = (
    "GROK_HOME",
    "GROK_SANDBOX",
    "GROK_CONFIG",
    "GROK_CONFIG_PATH",
    *HOME_VARIABLES,
)
"""Variables Dromon sets or clears itself. An inherited ``GROK_HOME`` or ``HOME`` is
replaced by Dromon's own, never honoured."""

REFUSED_ENVIRONMENT: Final = (
    "XAI_API_BASE_URL",
    "GROK_CLI_CHAT_PROXY_BASE_URL",
    "GROK_AUTH_PROVIDER_COMMAND",
    "GROK_OIDC_ISSUER",
    "GROK_OIDC_CLIENT_ID",
)
"""Variables that would send Grok's requests elsewhere or sign it in through another
flow than the one stored in Dromon's Grok home, from Grok's authentication guide."""

COMPAT_ENVIRONMENT: Final[Mapping[str, str]] = {
    "GROK_CLAUDE_MCPS_ENABLED": "0",
    "GROK_CURSOR_MCPS_ENABLED": "0",
    "GROK_CLAUDE_HOOKS_ENABLED": "0",
    "GROK_CURSOR_HOOKS_ENABLED": "0",
}
"""Variables Grok documents as turning off its scans of Claude Code's and Cursor's MCP
server and hook files, which read the home directory whatever ``GROK_HOME`` is.
Dromon's Grok home sets the same cells to false. **Probe of 2026-10-27.** They narrow
what Grok loads, but the servers Grok reports are checked as well. **Inference, not
verified.** That ``0`` turns a scan off, which Grok's guide implies without giving the
value."""

ASK_MODES: Final = frozenset({"default", "ask"})
"""Session modes in which Grok asks before a tool call that is not read-only."""

MODE_CATEGORY: Final = "mode"
"""The ACP ``configOptions`` category of a session's permission mode. **Inference.**
ACP names it so. Grok 1.0.70 sends no such option, and its ``x.ai/sessionConfig``
uses the category ``mode`` for reasoning efforts, so that list is never read."""

SERVERS_UPDATED: Final = "x.ai/mcp/servers_updated"
"""Grok's list of the MCP servers a session loaded, as ``mcpServers`` of ``name``,
``source`` and ``type``. **Probe of 2026-10-27.** Grok 1.0.70 sent it before its
``session/new`` answer, with the servers of the user's own configuration, although
Dromon passed none."""
MCP_PROGRESS: Final = "x.ai/mcp/init_progress"
"""Grok's count of the MCP servers a session is connecting, as ``total``."""
MODELS_UPDATE: Final = "x.ai/models/update"
"""Grok's models, as ``currentModelId`` and ``availableModels``."""
SESSION_NOTIFICATION: Final = "x.ai/session_notification"
"""Grok's session notices, of which Dromon reads ``model_changed``."""
SETTINGS_UPDATE: Final = "x.ai/settings/update"
"""Grok's account settings from its service, with a ``permission_mode`` that was
null on 2026-10-27 while the user's ``config.toml`` set ``always-approve``, so it is
not the session's mode and is read only as a reason to refuse."""
IGNORED_NOTIFICATIONS: Final = frozenset(
    {"x.ai/session/setup", "x.ai/announcements/update"}
)
"""Grok notices identified in the probe of 2026-10-27 that carry nothing Dromon acts
on, the phases of opening a session and the product's announcements."""


def extension_method(method: str) -> str:
    """A Grok extension method's name without ACP's ``_`` prefix for extensions,
    which Grok 1.0.70 sends and its guide omits, so both spellings are read alike."""
    return method.removeprefix("_")


# Dromon's own Grok home.

HOME_DIR: Final = Path(".grok") / "dromon-home"
"""Dromon's own Grok home, relative to the home directory, which a Grok line reads as
``GROK_HOME``, following Pi's dedicated agent directory. **Operator's decision of
2026-10-27.** The probe of that day showed that a Grok started on the operator's own
home loads the operator's MCP servers and permission mode, which no flag, overlay or
ACP field overrides, so a Grok line never reads that home."""

USER_HOME: Final = "home"
"""The directory inside Dromon's Grok home that a Grok line reads as ``HOME``, as an
Antigravity line reads one of its own. **Operator's decision of 2026-10-27.** Grok's
permissions guide says it reads ``defaultMode`` from ``~/.claude/settings.json`` with
no setting to stop it, and its rules, skills and hook guides name ``~/.claude``,
``~/.claude.json``, ``~/.cursor`` and ``~/.agents``, so a Grok line never sees the
operator's home through ``HOME``. **Inference from Grok's authentication guide.** The
sign-in is the file ``auth.json`` under ``GROK_HOME``, and the guide names no keychain,
so the sign-in does not depend on ``HOME``. **Not verified.** That Grok finds the home
directory through ``HOME`` rather than the account database."""

_GROK_HOME_TEXT: Final = f"$HOME/{HOME_DIR.as_posix()}"

SIGN_IN: Final = (
    f'mkdir -p -m 700 "{_GROK_HOME_TEXT}/{USER_HOME}" &&'
    f' GROK_HOME="{_GROK_HOME_TEXT}" HOME="{_GROK_HOME_TEXT}/{USER_HOME}" grok login'
)
"""The command with which the operator signs Grok in to Dromon's home, once, by hand.
``grok login`` is the sign-in of Grok's authentication guide, and ``GROK_HOME`` is the
variable its configuration guide documents for moving its directory. ``HOME`` is set as
a turn sets it, so the sign-in reads none of the operator's other harness files and
keeps anything it may write under ``HOME`` where a turn finds it. ``GROK_HOME`` comes
first, so both expand the operator's ``HOME`` in any POSIX shell. Dromon never writes,
reads or copies the credential this stores."""

HOME_CONFIG: Final = (
    "# Dromon's Grok home. Dromon wrote this file and refuses a Grok turn when it\n"
    "# changes. Delete it to have Dromon write it again.\n"
    "\n"
    "[ui]\n"
    'permission_mode = "default"\n'
    "\n"
    "[compat.claude]\n"
    "mcps = false\n"
    "hooks = false\n"
    "\n"
    "[compat.cursor]\n"
    "mcps = false\n"
    "hooks = false\n"
)
"""The whole of the home's ``config.toml``. It sets Grok's asking mode, declares no
MCP server and no hook, and turns off the scans of Claude Code's and Cursor's MCP
servers and hooks. Compared by its parsed contents before every turn, so any key Grok
or anyone else adds is drift."""

POLICY_FILES: Final = ("managed_config.toml", "requirements.toml")
"""Policy files Grok reads from its home, which may add hooks, MCP servers or a pinned
permission mode. **Observed on 2026-10-27, key names only.** Grok keeps both in the
operator's home as empty files, so each must be absent or empty."""

TRUST_FILE: Final = "trusted_folders.toml"
"""Grok's record of trusted folders, which lets a project's hooks, MCP servers,
plugins and permission rules load. It must be absent or list no folder."""

SYSTEM_POLICY_DIR: Final = Path("/etc/grok")
"""Where Grok reads its system policy files, which must be absent or empty too. macOS
device management, which Grok also reads, cannot be checked from here."""

REFUSED_HOME_ENTRIES: Final = (
    "agents",
    "hooks",
    "hooks-paths",
    "installed-plugins",
    "lsp.json",
    "plugins",
    "rules",
    "skills",
    "workflows",
)
"""Entries of a Grok home that could load agents, hooks, plugins, language servers,
rules, skills or workflows. **Observed in Dromon's Grok home on 2026-10-30.** Grok
creates an empty ``hooks-paths`` file and an ``installed-plugins`` directory holding
empty ``*.lock`` files. **Inference, not verified live.** Those empty records load no
hook paths or plugins. Empty regular files and these lock files are allowed; symlinks,
non-empty files and all other directory contents remain refused."""

USER_HOME_HARNESS_ENTRIES: Final = (
    ".agents",
    ".claude",
    ".claude.json",
    ".cursor",
    ".grok",
)
"""Entries of the line's ``HOME`` through which Grok's guide says it loads another
harness's settings, permission mode, MCP servers, hooks, rules or skills, or its own
home when ``GROK_HOME`` is unset. Refused as ``REFUSED_HOME_ENTRIES`` are, after
``claude_mode_refusal`` has read the Claude Code settings there."""

PROJECT_DIRECTORY: Final = ".grok"
"""A project's own Grok directory, refused at every project level by
``check_project``."""

REFUSED_PROJECT_ENTRIES: Final = (PROJECT_DIRECTORY, ".claude", ".cursor", ".agents")
"""Project directories whose settings, rules, skills or hooks Grok could load, but
which Dromon has not read as Grok configuration."""

MAX_HOME_FILE: Final = 1024 * 1024
"""The largest home or settings file Dromon reads."""

CLAUDE_MANAGED_SETTINGS: Final = (
    Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    Path("/etc/claude-code/managed-settings.json"),
)
"""Claude Code's managed settings, which Grok reads for a permission mode."""


def _read_small(file: Path) -> str | None:
    """A home or settings file's text, ``None`` when absent, or ``EngineRefused``."""
    try:
        with file.open("rb") as handle:
            raw = handle.read(MAX_HOME_FILE + 1)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise EngineRefused(f"cannot read {file}: {error}") from error
    if len(raw) > MAX_HOME_FILE:
        raise EngineRefused(f"{file} exceeds {MAX_HOME_FILE} bytes")
    try:
        return raw.decode("utf-8")
    except ValueError as error:
        raise EngineRefused(f"{file} is not UTF-8") from error


def _read_toml(file: Path) -> dict[str, object] | None:
    text = _read_small(file)
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except ValueError as error:
        raise EngineRefused(f"{file} is not TOML: {error}") from error


def _is_empty(document: Mapping[str, object]) -> bool:
    """Whether a TOML document sets nothing, allowing empty top-level tables."""
    return all(value == {} for value in document.values())


def _write_new(file: Path, text: str) -> None:
    """Write ``file`` only if it does not exist, owner-only, never through a link."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(file, flags, 0o600)
    except FileExistsError:
        return
    except OSError as error:
        raise EngineRefused(f"cannot write {file}: {error}") from error
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def _own_directory(directory: Path, label: str) -> None:
    """Create ``directory`` owner-only if missing, and refuse a link, a file, or one
    other users may write."""
    if directory.is_symlink():
        raise EngineRefused(
            f"{label} {directory} is a symbolic link, which Grok's sandbox refuses and"
            " Dromon does not follow"
        )
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise EngineRefused(f"cannot create {label} {directory}: {error}") from error
    if not directory.is_dir():
        raise EngineRefused(f"{label} {directory} is not a directory")
    if directory.stat().st_mode & 0o022:
        raise EngineRefused(
            f"{label} {directory} may be written by other users. Make it owner-only,"
            " as Grok's guide asks of a home that holds a sign-in."
        )


def prepare_home(
    grok_home: Path,
    workspace: Path,
    protected: Sequence[Path],
    daemon_home: Path | None = None,
) -> None:
    """Create Dromon's Grok home and the line's ``HOME`` inside it, owner-only, and
    write the Grok home's ``config.toml`` and ``sandbox.toml`` when either is missing.
    An existing file is never replaced, so drift is refused by ``check_home``,
    ``check_user_home`` and ``check_profile`` rather than repaired. Nothing here
    writes, reads or copies a credential.

    Porting note. ``O_NOFOLLOW`` and the mode bits are POSIX, as Grok's sandbox is."""
    _own_directory(grok_home, "Dromon's Grok home")
    _own_directory(grok_home / USER_HOME, "a Grok line's home directory")
    _write_new(grok_home / "config.toml", HOME_CONFIG)
    _write_new(
        grok_home / "sandbox.toml",
        profile_snippet(READER_PROFILE, workspace, protected, daemon_home)
        + "\n\n"
        + profile_snippet(WRITER_PROFILE, workspace, protected, daemon_home)
        + "\n",
    )


def check_home(grok_home: Path, system: Path = SYSTEM_POLICY_DIR) -> None:
    """Refuse Dromon's Grok home when its configuration has drifted from Dromon's,
    when a policy or trust file sets anything, or when it holds an entry that would
    load something the line was not given. ``auth.json`` is never opened."""
    config = grok_home / "config.toml"
    document = _read_toml(config)
    if document != tomllib.loads(HOME_CONFIG):
        raise EngineRefused(
            f"{config} is not the configuration Dromon wrote for its Grok home."
            " Delete it to have Dromon write it again."
        )
    for file in [grok_home / name for name in POLICY_FILES] + [
        system / name for name in POLICY_FILES
    ]:
        policy = _read_toml(file)
        if policy is not None and not _is_empty(policy):
            raise EngineRefused(
                f"{file} sets Grok policy, which may add hooks, MCP servers or a"
                " permission mode a Grok line was not given"
            )
    trust = _read_toml(grok_home / TRUST_FILE)
    if trust is not None and not _is_empty(trust):
        raise EngineRefused(
            f"{grok_home / TRUST_FILE} trusts a folder, which would let a project's"
            " hooks, MCP servers and permission rules load for a Grok line"
        )
    found = _loading_entries(grok_home, REFUSED_HOME_ENTRIES, allow_empty_records=True)
    if found:
        raise EngineRefused(
            f"Dromon's Grok home {grok_home} holds " + ", ".join(found) + ", which"
            " would give a line agents, hooks, plugins, rules, skills or workflows it"
            " was not given"
        )


def check_user_home(user_home: Path) -> None:
    """Refuse the line's ``HOME`` when it holds another harness's files, which Grok
    would load. Run after ``claude_mode_refusal``, which names a mode there first."""
    found = _loading_entries(user_home, USER_HOME_HARNESS_ENTRIES)
    if found:
        raise EngineRefused(
            f"a Grok line's home directory {user_home} holds " + ", ".join(found) + ","
            " from which Grok would load another harness's settings, servers, hooks,"
            " rules or skills"
        )


def _loading_entries(
    directory: Path, names: Sequence[str], *, allow_empty_records: bool = False
) -> list[str]:
    """Entries that may load configuration, allowing only known empty Grok records.

    ``hooks-paths`` may be an empty regular file. ``installed-plugins`` may hold only
    empty regular ``*.lock`` files. Never follow symbolic links. Empty directories are
    allowed because Grok may create them."""
    found: list[str] = []
    for name in names:
        entry = directory / name
        try:
            info = entry.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise EngineRefused(
                f"cannot inspect Grok home entry {entry}: {error}"
            ) from error
        if stat.S_ISLNK(info.st_mode):
            found.append(name)
            continue
        if not allow_empty_records:
            if stat.S_ISDIR(info.st_mode):
                try:
                    if any(True for _ in entry.iterdir()):
                        found.append(name)
                except OSError as error:
                    raise EngineRefused(
                        f"cannot inspect Grok home entry {entry}: {error}"
                    ) from error
            else:
                found.append(name)
            continue
        if stat.S_ISREG(info.st_mode):
            if name != "hooks-paths" or info.st_size != 0:
                found.append(name)
            continue
        if not stat.S_ISDIR(info.st_mode):
            found.append(name)
            continue
        try:
            children = list(entry.iterdir())
        except OSError as error:
            raise EngineRefused(
                f"cannot inspect Grok home entry {entry}: {error}"
            ) from error
        if not children:
            continue
        if name != "installed-plugins":
            found.append(name)
            continue
        allowed_locks = True
        for child in children:
            try:
                child_info = child.lstat()
            except OSError as error:
                raise EngineRefused(
                    f"cannot inspect Grok home entry {child}: {error}"
                ) from error
            if not (
                child.name.endswith(".lock")
                and stat.S_ISREG(child_info.st_mode)
                and child_info.st_size == 0
            ):
                allowed_locks = False
                break
        if not allowed_locks:
            found.append(name)
    return found


def _claude_settings_files(home: Path, workspace: Path) -> list[Path]:
    """The Claude Code settings files Grok's guide says it reads a mode from, the
    user's under ``home``, the managed ones, and each project level from the workspace
    up to its repository's root, or the workspace alone outside a repository. ``home``
    is the ``HOME`` Grok runs with, the line's own, not the operator's."""
    files = [
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        *CLAUDE_MANAGED_SETTINGS,
    ]
    for directory in _project_levels(workspace):
        files.append(directory / ".claude" / "settings.json")
        files.append(directory / ".claude" / "settings.local.json")
    return files


def _project_levels(workspace: Path) -> list[Path]:
    """Each directory from the workspace up to its repository's root, or the
    workspace alone outside a repository, as Grok's guides say it reads project
    files. The walk is bounded by the depth of the path."""
    levels: list[Path] = []
    for directory in (workspace, *workspace.parents):
        levels.append(directory)
        if os.path.lexists(directory / ".git"):
            return levels
    return [workspace]


def check_project(workspace: Path) -> None:
    """Refuse a turn when any project level holds an unread Grok or harness directory.
    Grok's guides describe loading configuration, hooks, rules or skills from these
    directories, which a worktree could supply. Dromon refuses them whole rather than
    reading their contents, as the Antigravity adapter refuses its workspace
    directories. **Operator decision, 2026-10-28.**"""
    found = [
        str(directory / name)
        for directory in _project_levels(workspace)
        for name in REFUSED_PROJECT_ENTRIES
        if _loading_entries(directory, (name,))
    ]
    if found:
        raise EngineRefused(
            "Grok would load project configuration or instructions Dromon has not"
            " read from " + ", ".join(found)
        )


def claude_mode_refusal(home: Path, workspace: Path) -> str | None:
    """Why a Claude Code settings file refuses a Grok turn, or ``None``. Grok's guide
    says it reads ``defaultMode`` from these files and does not say whether its own
    ``permission_mode`` outranks them, so a file that names any mode but Grok's
    asking one refuses the turn. **Not verified live.** Which one wins."""
    for file in _claude_settings_files(home, workspace):
        text = _read_small(file)
        if text is None:
            continue
        try:
            document: object = json.loads(text)
        except ValueError:
            return f"{file} is not JSON, and Grok reads a permission mode from it"
        if not isinstance(document, dict):
            continue
        settings = cast(dict[str, object], document)
        permissions = settings.get("permissions")
        mode = (
            cast(dict[str, object], permissions).get("defaultMode")
            if isinstance(permissions, dict)
            and "defaultMode" in cast(dict[str, object], permissions)
            else settings.get("defaultMode")
        )
        if mode is not None and mode not in ASK_MODES:
            return (
                f"{file} sets Claude Code's defaultMode to {mode!r}, which Grok also"
                " reads, and Grok's guide does not say whether Dromon's Grok home"
                " outranks it"
            )
    return None


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
    """Why a Grok line cannot run ``config``, or ``None`` if it can.

    **Review of read-only seats, second round, findings 1 and 2.** A read-only working
    directory is refused until a live probe shows the ``dromon-reader`` profile deny a
    write to the worktree and ``capabilities.GROK`` records ``read_only_workdir`` as
    known. The profile rests on Grok's built-in ``read-only`` base, which Dromon names
    but cannot inspect."""
    if (
        config.workdir_access is WorkdirAccess.READ
        and GROK_CAPABILITIES.read_only_workdir is not Known.YES
    ):
        return (
            "Grok cannot yet confine a line to a read-only working directory, because"
            " no live probe has shown its read-only sandbox base deny a write there"
        )
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
    kept = allowlisted(environment, BASE_ENVIRONMENT)
    return {**kept, **COMPAT_ENVIRONMENT}


# The sandbox profile.


def profile_name(config: LineConfig) -> str:
    return (
        WRITER_PROFILE
        if config.workdir_access is WorkdirAccess.WRITE
        and WRITE_TOOLS & set(config.builtin_tools)
        else READER_PROFILE
    )


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


GROK_CREDENTIAL_PATHS: Final = (
    Path(".ssh"),
    Path(".aws"),
    Path(".codex/auth.json"),
    Path(".cargo/credentials.toml"),
    Path(".pi/agent/auth.json"),
)
"""Operator-home credentials Grok profiles must deny, when each path exists."""


def _credential_paths(daemon_home: Path) -> tuple[Path, ...]:
    """The configured credential paths under the daemon's real home directory."""
    real_home = Path(os.path.realpath(daemon_home))
    return tuple(real_home / relative for relative in GROK_CREDENTIAL_PATHS)


def profile_snippet(
    name: str,
    workspace: Path,
    protected: Sequence[Path],
    daemon_home: Path | None = None,
) -> str:
    """The full ``sandbox.toml`` table for a new Dromon profile."""
    daemon_home = Path.home() if daemon_home is None else daemon_home
    deny = [".dromon"] + [
        os.path.realpath(p)
        for p in protected
        if not _covers([workspace / ".dromon"], p)
    ]
    deny.extend(
        str(path) for path in _credential_paths(daemon_home) if os.path.lexists(path)
    )
    lines = [f"[profiles.{name}]"]
    if name == WRITER_PROFILE:
        lines.append('extends = "workspace"')
        git = git_directories(workspace)
        directories = () if git is None else tuple(sorted(set(git)))
        lines.append(
            "read_only = ["
            + ", ".join(json.dumps(str(path)) for path in directories)
            + "]"
        )
    else:
        lines.append('extends = "read-only"')
    lines.append(f"deny = [{', '.join(json.dumps(entry) for entry in deny)}]")
    return "\n".join(lines)


PROFILE_KEYS: Final = frozenset({"extends", "deny", "read_only", "restrict_network"})
"""The keys a Dromon profile may set. ``read_write`` would let a reader write, so it
and any key Grok adds later are refused."""


def check_profile(
    config: LineConfig,
    workspace: Path,
    protected: Sequence[Path],
    home: Path,
    grok_home: Path,
    daemon_home: Path | None = None,
) -> str:
    """The name of the Grok profile the line runs under, after checking that it denies
    every ``protected`` directory and, for a writer, keeps ``.git`` read-only. Raises
    ``EngineRefused`` naming the table to add when it does not.

    The profile is read from Dromon's Grok home alone. A project's ``.grok``, where a
    project's ``sandbox.toml`` would live, is refused by ``check_project``, so no
    project file can replace it. The reader's ``extends`` names Grok's built-in
    ``read-only`` base, whose denial of writes Dromon cannot inspect, which is why a
    read-only working directory is refused by ``unsupported``. ``home`` resolves a
    ``~`` in a ``deny`` entry, and is the line's own ``HOME``, at which Grok's sandbox
    guide anchors ``~``. **Inference, not verified.**
    That Grok reads ``sandbox.toml`` from ``GROK_HOME``, as it does ``config.toml``.
    """
    daemon_home = Path.home() if daemon_home is None else daemon_home
    name = profile_name(config)
    file = grok_home / "sandbox.toml"
    profiles = _load_profiles(file)
    profile = profiles.get(name)
    full_snippet = profile_snippet(name, workspace, protected, daemon_home)
    if profile is None:
        advice = f" Add this new table to {file}:\n\n{full_snippet}"
    else:
        deny_values = [".dromon"] + [
            str(Path(os.path.realpath(path)))
            for path in protected
            if not _covers([workspace / ".dromon"], path)
        ]
        deny_values.extend(
            str(path)
            for path in _credential_paths(daemon_home)
            if os.path.lexists(path)
        )
        git = git_directories(workspace)
        read_only_values = (
            []
            if name != WRITER_PROFILE or git is None
            else [str(path) for path in sorted(set(git))]
        )
        advice = "".join(
            (
                " Update only the ",
                repr(name),
                " profile in ",
                str(file),
                ". Add missing entries to its existing deny and read_only arrays, "
                "leaving every other profile table and entry as it is. Required "
                "deny entries are ",
                json.dumps(deny_values),
                "; required read_only entries are ",
                json.dumps(read_only_values),
            )
        )
    if profile is None:
        raise EngineRefused(f"no Grok sandbox profile {name} is defined.{advice}")
    unknown = sorted(set(profile) - PROFILE_KEYS)
    if unknown:
        raise EngineRefused(
            f"the Grok sandbox profile {name} sets " + ", ".join(unknown) + ", which"
            f" Dromon does not accept for it.{advice} Remove unsupported keys from"
            " this profile."
        )
    base = profile.get("extends", "workspace")
    if base not in PROFILE_BASES[name]:
        raise EngineRefused(
            f"the Grok sandbox profile {name} extends {base!r}, which Dromon does not"
            f" accept for it.{advice} Set extends to {PROFILE_EXTENDS[name]!r} in this"
            " profile."
        )
    denied = [
        path
        for entry in _entries(profile, "deny")
        if (path := _resolve(entry, workspace, home)) is not None
    ]
    required_deny = [
        *protected,
        *(path for path in _credential_paths(daemon_home) if os.path.lexists(path)),
    ]
    exposed = [
        str(path)
        for path in required_deny
        if os.path.lexists(path) and not _covers(denied, path)
    ]
    if exposed:
        raise EngineRefused(
            f"the Grok sandbox profile {name} does not deny "
            + ", ".join(exposed)
            + "."
            + advice
        )
    git = git_directories(workspace)
    if name == WRITER_PROFILE and git is not None:
        held = [Path(entry) for entry in _entries(profile, "read_only")]
        required_git_dirs = tuple(set(git))
        unheld = [path for path in required_git_dirs if not _covers(held, path)]
        if unheld:
            raise EngineRefused(
                f"the Grok sandbox profile {name} does not hold git directories"
                " read-only: " + ", ".join(map(str, unheld)) + "." + advice
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
    finished: set[str] = field(default_factory=set[str])
    """Tool calls whose update reported them finished, so each result is fed to the
    driver once."""
    interrupted_for: str | None = None
    refusal: str | None = None
    cancelled: bool = False
    absolute_deadline: float | None = None
    servers_reported: bool = False
    """Whether Grok listed the session's MCP servers, which a prompt requires."""


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
        git_ssh: GitSshAccess | None = None,
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
        self.git_ssh = git_ssh
        """The agent and pinned hosts of the project's ``[git_ssh]`` table, if the
        line was given them."""
        """The process mark every process of this line inherits, if issued."""
        self._client: acp.Client | None = None
        self.session_id_in_use: str | None = None

    async def run_turn(self, prompt: str) -> TurnReport:
        """Run one turn for ``prompt`` and return what happened."""
        strategy = self.driver.settings.strategy
        environment = engine_environment(self._environment, strategy)
        environment.update(gitssh.environment(self.git_ssh))
        if self.mark is not None:
            environment[MARK_VARIABLE] = self.mark
        environment[STOP_POLICY_VARIABLE] = self.config.stop_policy.value
        reason = unsupported(self.config)
        if reason is not None:
            raise EngineRefused(reason)
        grok_home = self._home / HOME_DIR
        user_home = grok_home / USER_HOME
        prepare_home(grok_home, self.cwd, self.protected, self._home)
        check_home(grok_home)
        check_project(self.cwd)
        claude_mode = claude_mode_refusal(user_home, self.cwd)
        if claude_mode is not None:
            raise EngineRefused(claude_mode)
        check_user_home(user_home)
        environment["GROK_HOME"] = str(grok_home)
        environment["HOME"] = str(user_home)
        environment["GROK_SANDBOX"] = check_profile(
            self.config, self.cwd, self.protected, user_home, grok_home, self._home
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
        else:
            refusal = self._notice_refusal(extension_method(method), params, turn)
            if refusal is not None:
                await self._cancel(turn, refusal)

    def _allowed_servers(self) -> frozenset[str]:
        return frozenset(
            str(server["name"])
            for server in acp.mcp_servers(self.bridge, self.line_token)
        )

    def _notice_refusal(
        self, method: str, params: Mapping[str, Any], turn: _Turn
    ) -> str | None:
        """Why one of Grok's own notifications refuses the turn, or ``None``.
        **Probe of 2026-10-27.** A notice not identified here is passed over, as
        before, since a notification cannot be refused, and a request not identified
        is still answered with an error."""
        if method == SERVERS_UPDATED:
            turn.servers_reported = True
            return self._servers_refusal(params.get("mcpServers"))
        if method == MCP_PROGRESS:
            total = params.get("total")
            allowed = len(self._allowed_servers())
            if total is not None and (type(total) is not int or total > allowed):
                return (
                    f"Grok is connecting {total!r} MCP servers, and a Grok line has"
                    f" only Dromon's, {allowed}"
                )
            return None
        if method == MODELS_UPDATE:
            if "currentModelId" in params:
                return self._model_refusal(params.get("currentModelId"))
            return None
        if method == SESSION_NOTIFICATION:
            update = params.get("update")
            if (
                isinstance(update, dict)
                and cast(dict[str, Any], update).get("sessionUpdate") == "model_changed"
            ):
                return self._model_refusal(cast(dict[str, Any], update).get("model_id"))
            return None
        if method == SETTINGS_UPDATE:
            mode = params.get("permission_mode")
            if mode is not None and mode not in ASK_MODES:
                return f"Grok's settings name the permission mode {mode!r}"
            return None
        return None

    def _servers_refusal(self, servers: object) -> str | None:
        """Why a list of loaded MCP servers refuses the turn, or ``None`` when it
        holds Dromon's own server at most. Other servers are counted and never
        named, since their names are the operator's configuration."""
        if not isinstance(servers, list):
            return "Grok reported its MCP servers in a shape Dromon does not read"
        allowed = self._allowed_servers()
        others = 0
        for server in cast(list[object], servers):
            name = (
                cast(dict[str, Any], server).get("name")
                if isinstance(server, dict)
                else None
            )
            if not isinstance(name, str) or name not in allowed:
                others += 1
        if others:
            return (
                f"Grok loaded {others} MCP servers besides Dromon's, from the user's"
                " own configuration, and a Grok line may have only Dromon's tools"
            )
        return None

    def _model_refusal(self, model: object) -> str | None:
        """Why a model Grok reports during the session refuses the turn. The driver
        runs Grok with ``--model``, so any other model, or one that is not a name,
        is not the model the driver accepted."""
        expected = self.driver.settings.model
        if model != expected:
            return f"Grok switched the session's model to {model!r}, not {expected!r}"
        return None

    def _mode_refusal(self, result: Mapping[str, Any]) -> str | None:
        """Why the session's permission mode refuses the turn, read from a
        ``configOptions`` entry of the category ``mode``, or ``None``. **Probe of
        2026-10-27.** Grok 1.0.70 reports no mode, and no ACP field, flag or overlay
        sets one, so a session that reports none rests on the checks made before
        Grok started. Dromon's Grok home sets the asking mode, no policy file or
        Claude Code settings file names another, and ``session/new`` asks for
        neither ``yoloMode`` nor ``autoMode``. **Not verified live.** That Grok then
        runs in that mode, which it cannot report."""
        for option in cast(list[Any], result.get("configOptions") or []):
            if not isinstance(option, dict):
                continue
            entry = cast(dict[str, Any], option)
            if entry.get("category") == MODE_CATEGORY:
                current = entry.get("currentValue")
                if current in ASK_MODES:
                    return None
                return f"the engine runs in the permission mode {current!r}"
        return None

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
        elif kind == "current_mode_update":
            # A session that leaves the asking modes mid-turn no longer asks Dromon.
            mode = update.get("currentModeId")
            if mode not in ASK_MODES:
                await self._cancel(
                    turn, f"the engine switched to the permission mode {mode!r}"
                )
        elif kind in ("tool_call", "tool_call_update"):
            # Antigravity review, F1 to F3, applied to Grok on 2026-10-26. No kind is
            # exempt, and every update to a call is compared with its record.
            identifier = update.get("toolCallId")
            refusal = acp.identity_refusal(identifier, update, turn.calls)
            if refusal is not None:
                await self._cancel(turn, refusal)
                return
            await self._record(str(identifier), update, turn)
            if acp.newly_finished(str(identifier), update, turn.finished):
                # The safe point before the next model call that priority mail
                # takes, as ``dromon.priority`` describes.
                await self._act(Event(TOOL_RESULT), turn)

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
                " cached sign-in. Sign Grok in to Dromon's Grok home once, by hand,"
                f" with {SIGN_IN}"
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
        if turn.refusal is not None:
            return session_id
        if not turn.servers_reported:
            turn.refusal = (
                "Grok did not report its MCP servers, so Dromon cannot confirm that"
                " a Grok line has only Dromon's tools"
            )
            return session_id
        model = acp.reported_model(result)
        verdict = self.driver.step(Event("init", model or ""))
        if verdict.tag is ActionTag.REFUSE:
            turn.refusal = (
                f"the driver for {verdict.text} refuses the engine model {model!r}"
            )
            return session_id
        # Refuse the turn unless the session asks before acting, switching to
        # ``default`` when Grok offers it through ACP's ``modes``.
        if isinstance(result.get("modes"), dict):
            refusal = await acp.ensure_mode(
                self._rpc(), session_id, result, ASK_MODES, "default"
            )
        else:
            refusal = self._mode_refusal(result)
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
