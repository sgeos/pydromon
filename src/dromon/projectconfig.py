"""Per-project configuration, the ``.dromon/dromon.toml`` file.

The file declares a project's session lines, their engines and tools, its conversation
limits and its wake policy. It is built to grow with the project, in the direction of
Kubernetes manifests: profiles are reusable line templates, and every setting a line
has can be given per line. ``docs/protocol/PROJECT_CONFIG.md`` is normative.

Design notes from the settings of other harnesses, reviewed on 2026-10-25.

- Claude Code layers JSON settings, managed over command line over project local over
  shared project over user, merges lists across layers, and reports a broken file with
  its location. Codex and Grok Build use TOML in a user file and a project file, with
  managed and requirements files for organizations, and Codex adds profiles.
- Dromon uses TOML, read with the standard library, and validates strictly: an unknown
  key is an error rather than a silent typo. Every error names the file, and the line
  and column for a syntax error, or the key path and the line of its table otherwise.
- Precedence is command line flags over this file over built-in defaults. User and
  managed layers are not yet specified. A line's own keys replace its profile's whole,
  rather than merging lists, so a line's tools are always readable in one place. The
  ``sandbox`` and ``settings`` tables merge instead, so no layer drops a deny entry.

Trust. The file is operator configuration, committed with the project. It can grant a
line tools, MCP servers, hooks and credentials, so it deserves the review any such file
does. MCP servers may refer to secrets in the daemon's environment instead.
"""

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final, cast

from dromon.driver import MODELS, Strategy
from dromon.limits import DEFAULT_LIMITS, Limits
from dromon.lineconfig import (
    ConfigError,
    LineConfig,
    check_instructions,
    config_from_document,
    references,
)
from dromon.model import (
    DEFAULT_HANDOFF_BELOW_PERCENT,
    DEFAULT_HANDOFF_SECONDS,
    MAX_ROLE_SEATS,
    FailoverPolicy,
    ID_PATTERN,
    MAX_HANDOFF_SECONDS,
    MIN_HANDOFF_SECONDS,
    InvalidField,
    LineSession,
    check_workdir,
)
from dromon import settings as engine_settings
from dromon.sandbox import SandboxConfig, SandboxError, merge, parse_sandbox
from dromon.settings import SettingsConfig, SettingsError, parse_settings
from dromon.agents import OFF as AGENTS_OFF
from dromon.agents import AgentError, AgentPolicy, parse_policy
from dromon.federation import (
    NO_FEDERATION,
    FederationConfig,
    FederationError,
    parse_federation,
)
from dromon.gitssh import GitSshConfig, GitSshError, parse_git_ssh
from dromon.teacher import TeacherConfig, TeacherError, parse_teacher

CONFIG_FILE_NAME: Final = "dromon.toml"
FILE_VERSION: Final = 1
MAX_WAKE_EVERY: Final = 3600
MAX_FILE_BYTES: Final = 4 * 1024 * 1024

TOP_KEYS: Final = frozenset(
    {
        "version",
        "daemon",
        "limits",
        "wake",
        "sandbox",
        "settings",
        "teacher",
        "agents",
        "federation",
        "peers",
        "git_ssh",
        "leases",
        "profiles",
        "lines",
        "roles",
    }
)
DAEMON_KEYS: Final = frozenset(
    {"operator", "tick_seconds", "require_sandbox", "disclosure_terms"}
)
LIMIT_KEYS: Final = frozenset(
    {"max_autonomous_turns", "max_cost_usd", "cooldown_seconds"}
)
WAKE_KEYS: Final = frozenset({"every_ticks"})
LEASE_KEYS: Final = frozenset({"names", "ttl_seconds"})
ROLE_KEYS: Final = frozenset(
    {
        "workdir",
        "owner",
        "seats",
        "handoff_seconds",
        "failover",
        "handoff_below_percent",
    }
)
MAX_SEATS: Final = MAX_ROLE_SEATS
"""The most seats one role may declare, a bound on the planning loop and the status."""
DEFAULT_LEASE_TTL: Final = 10800
MIN_LEASE_TTL: Final = 60
MAX_LEASE_TTL: Final = 86400
MAX_LEASES: Final = 256
TOOL_KEYS: Final = frozenset(
    {
        "builtin_tools",
        "mcp_servers",
        "tool_policy",
        "allowed_tools",
        "inherit_account_mcp",
        "effort",
        "claude_code_prompt",
        "instructions",
        "workdir",
        "workdir_access",
        "project_settings",
        "plugins",
        "keep_session",
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
        "secrets",
    }
)
LAYERED_KEYS: Final = frozenset({"sandbox", "settings"})
"""Keys whose profile and line tables merge as Claude Code's scopes do."""
LINE_KEYS: Final = (
    frozenset(
        {
            "profile",
            "model",
            "strategy",
            "limits",
            "wake",
            "session",
            "may_own",
            "git_ssh",
        }
    )
    | TOOL_KEYS
    | LAYERED_KEYS
)
PROFILE_KEYS: Final = LINE_KEYS - {"profile", "session"}
"""A profile has every line key but ``profile``, and but ``session``, since one engine
session cannot continue in several lines."""


class ConfigFileError(ValueError):
    """The project file is malformed, with where it is wrong."""

    def __init__(
        self,
        path: Path,
        message: str,
        *,
        key: str = "",
        line: int | None = None,
        column: int | None = None,
    ) -> None:
        self.path = path
        self.key = key
        self.line = line
        self.column = column
        where = str(path)
        if line is not None:
            where += f":{line}"
            if column is not None:
                where += f":{column}"
        subject = f"{key}: " if key else ""
        super().__init__(f"{where}: {subject}{message}")


@dataclass(frozen=True, slots=True)
class LineDefinition:
    line_id: str
    model: str
    strategy: Strategy
    config: LineConfig
    limits: Limits | None
    """The line's own limits, merged onto the file's ``[limits]``, or ``None``."""
    wake_every_ticks: int | None
    """The line's own wake policy, or ``None`` for the project's."""
    session: str | None = None
    """An existing engine session the line adopts when the daemon creates it."""
    may_own: bool = True
    """Whether the line, as a seat of a role, may own the role. Meaningless for a line
    that is a seat of no role."""
    secret_names: tuple[str, ...] = ()
    """Environment variable names this project grants the line to reference in its
    MCP configuration. Values are read from the daemon environment at turn time."""
    git_ssh: bool = False
    """Whether the line's shell commands get the project's ``[git_ssh]`` agent. Held
    here and not in ``LineConfig``, so it never enters the event log and no client or
    line can set it."""


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    """A ``[roles.NAME]`` table. Its seats are lines the same file declares."""

    role_id: str
    workdir: str
    """The role's worktree, relative to the project root, as ``check_workdir``
    accepts it."""
    seats: tuple[str, ...]
    """The role's seats, in the file's order, distinct and at most ``MAX_SEATS``."""
    owner: str | None = None
    """The seat that owns the role when it is first declared, or ``None`` for a role
    that starts vacant. Used only while the role has never had an owner."""
    handoff_seconds: int = DEFAULT_HANDOFF_SECONDS
    failover: FailoverPolicy = FailoverPolicy.AUTOMATIC
    """Whether the harness passes the role on by itself when its owner cannot act,
    or leaves the change to an operator."""
    handoff_below_percent: int = DEFAULT_HANDOFF_BELOW_PERCENT
    """The stated budget below which the owner is asked to prepare a handoff, or 0
    for never."""


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    operator: str | None = None
    tick_seconds: float | None = None
    limits: Limits = DEFAULT_LIMITS
    wake_every_ticks: int = 1
    sandbox: SandboxConfig | None = None
    """The project's sandbox, merged under every line's own."""
    settings: SettingsConfig | None = None
    """The project's engine settings, merged under every line's own."""
    teacher: TeacherConfig | None = None
    """The project's local teacher model, if it declares one."""
    agents: AgentPolicy = AGENTS_OFF
    """Which external agents may connect, and with what access. Off by default."""
    federation: FederationConfig = NO_FEDERATION
    """The project's peers, other Dromon daemons, none by default."""
    git_ssh: GitSshConfig | None = None
    """The agent socket and pinned hosts the lines with ``git_ssh`` get, or ``None``.
    Off by default."""
    require_sandbox: bool = False
    """Whether every Claude Code line must run with its merged sandbox enabled."""
    disclosure_terms: str | None = None
    """The project's disclosure list, a path relative to the project root, or
    ``None``. Only the path is held here. The daemon reads the terms."""
    lease_names: tuple[str, ...] = ()
    """The leases the project's lines may acquire, by name."""
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL
    """How long an acquired lease lasts unless it is released first."""
    lines: dict[str, LineDefinition] = field(default_factory=dict[str, LineDefinition])
    roles: dict[str, RoleDefinition] = field(default_factory=dict[str, RoleDefinition])


def config_path(state_dir: Path) -> Path:
    return state_dir / CONFIG_FILE_NAME


_LOCATION = re.compile(r"\(at line (\d+), column (\d+)\)")


def load(path: Path, root: Path | None = None) -> ProjectConfig:
    """Read and validate the file at ``path``, or raise ``ConfigFileError``. With
    the project ``root``, each line's instruction files are checked as
    ``check_instructions`` says, and a refusal names the line and the file's line."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ConfigFileError(path, f"cannot be read: {error}") from error
    if len(raw) > MAX_FILE_BYTES:
        raise ConfigFileError(path, f"is larger than {MAX_FILE_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ConfigFileError(path, "is not UTF-8 text") from error
    return parse(text, path, root)


def parse(
    text: str, path: Path = Path(CONFIG_FILE_NAME), root: Path | None = None
) -> ProjectConfig:
    """Validate ``text`` as a project file, for tests and tools that hold the text.
    Instruction files are checked only when ``root`` is given."""
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        found = _LOCATION.search(str(error))
        raise ConfigFileError(
            path,
            _LOCATION.sub("", str(error)).strip(),
            line=int(found.group(1)) if found else None,
            column=int(found.group(2)) if found else None,
        ) from error
    return _Reader(path, text, root).project(document)


class _Reader:
    """Validation of one decoded file, which knows where each table starts."""

    def __init__(self, path: Path, text: str, root: Path | None) -> None:
        self.path = path
        self.lines = text.splitlines()
        self.root = root

    def fail(self, key: str, message: str) -> ConfigFileError:
        return ConfigFileError(self.path, message, key=key, line=self.line_of(key))

    def line_of(self, key: str) -> int | None:
        """The line of the table or key ``key`` names, if one is found."""
        parts = key.split(".")
        for size in range(len(parts), 0, -1):
            table = ".".join(parts[:size])
            pattern = re.compile(rf"^\s*\[\[?\s*{re.escape(table)}\s*\]\]?\s*(#.*)?$")
            for number, text in enumerate(self.lines, start=1):
                if pattern.match(text):
                    return number
        name = parts[-1]
        assignment = re.compile(rf"^\s*{re.escape(name)}\s*=")
        for number, text in enumerate(self.lines, start=1):
            if assignment.match(text):
                return number
        return None

    def table(
        self, value: object, key: str, allowed: frozenset[str]
    ) -> dict[str, object]:
        if not isinstance(value, dict):
            raise self.fail(key, "must be a table")
        document = cast(dict[str, object], value)
        unknown = sorted(set(document) - allowed)
        if unknown:
            raise self.fail(
                f"{key}.{unknown[0]}" if key else unknown[0],
                f"is not a known key. Known keys are {sorted(allowed)}",
            )
        return document

    def project(self, document: dict[str, object]) -> ProjectConfig:
        top = self.table(document, "", TOP_KEYS)
        version = top.get("version")
        if type(version) is not int or version != FILE_VERSION:
            raise self.fail("version", f"must be {FILE_VERSION}, found {version!r}")
        daemon = self.table(top.get("daemon", {}), "daemon", DAEMON_KEYS)
        operator = daemon.get("operator")
        if operator is not None and (
            not isinstance(operator, str) or ID_PATTERN.fullmatch(operator) is None
        ):
            raise self.fail("daemon.operator", "must be an operator identifier")
        raw_tick = daemon.get("tick_seconds")
        tick: float | None = None
        if raw_tick is not None:
            if not isinstance(raw_tick, int | float) or isinstance(raw_tick, bool):
                raise self.fail("daemon.tick_seconds", "must be a number in [0, 60]")
            tick = float(raw_tick)
            if not 0 <= tick <= 60:
                raise self.fail("daemon.tick_seconds", "must be a number in [0, 60]")
        require_sandbox = daemon.get("require_sandbox", False)
        if type(require_sandbox) is not bool:
            raise self.fail("daemon.require_sandbox", "must be a boolean")
        terms = daemon.get("disclosure_terms")
        if terms is not None and (
            not isinstance(terms, str) or not terms or "\x00" in terms
        ):
            raise self.fail("daemon.disclosure_terms", "must be a path to a file")
        limits = self.limits(top.get("limits", {}), "limits", DEFAULT_LIMITS)
        wake = self.wake(top.get("wake", {}), "wake", 1)
        sandbox = self.sandbox(top.get("sandbox"), "sandbox")
        settings = self.settings(top.get("settings"), "settings")
        teacher: TeacherConfig | None = None
        if "teacher" in top:
            try:
                teacher = parse_teacher(top["teacher"])
            except TeacherError as error:
                raise self.fail("teacher", str(error)) from error
        agents = AGENTS_OFF
        if "agents" in top:
            try:
                agents = parse_policy(top["agents"])
            except AgentError as error:
                raise self.fail("agents", str(error)) from error
        try:
            federation = parse_federation(top.get("federation"), top.get("peers"))
        except FederationError as error:
            # Each message begins with the key it is about, which places the line.
            raise self.fail(str(error).split(" ", 1)[0], str(error)) from error
        git_ssh: GitSshConfig | None = None
        if "git_ssh" in top:
            try:
                git_ssh = parse_git_ssh(top["git_ssh"])
            except GitSshError as error:
                raise self.fail(error.key, str(error)) from error
        lease_names, lease_ttl = self.leases(top.get("leases", {}))
        profiles = self.profiles(top.get("profiles", {}))
        lines_table = top.get("lines", {})
        if not isinstance(lines_table, dict):
            raise self.fail("lines", "must be a table")
        lines: dict[str, LineDefinition] = {}
        for line_id, value in sorted(cast(dict[str, object], lines_table).items()):
            lines[line_id] = self.line(line_id, value, profiles, limits, wake)
            if lines[line_id].git_ssh and git_ssh is None:
                raise self.fail(
                    f"lines.{line_id}.git_ssh",
                    "is true, but the file has no [git_ssh] table to give the line",
                )
        roles = self.roles(top.get("roles", {}), lines)
        return ProjectConfig(
            operator=operator,
            tick_seconds=tick,
            limits=limits,
            wake_every_ticks=wake,
            sandbox=sandbox,
            settings=settings,
            teacher=teacher,
            agents=agents,
            federation=federation,
            git_ssh=git_ssh,
            require_sandbox=require_sandbox,
            disclosure_terms=terms,
            lease_names=lease_names,
            lease_ttl_seconds=lease_ttl,
            lines=lines,
            roles=roles,
        )

    def limits(self, value: object, key: str, base: Limits) -> Limits:
        document = self.table(value, key, LIMIT_KEYS)
        turns = document.get("max_autonomous_turns", base.max_autonomous_turns)
        cost = document.get("max_cost_usd", base.max_cost_usd)
        cooldown = document.get("cooldown_seconds", base.cooldown_seconds)
        if type(turns) is not int:
            raise self.fail(f"{key}.max_autonomous_turns", "must be an integer")
        for name, number in (("max_cost_usd", cost), ("cooldown_seconds", cooldown)):
            if type(number) not in (int, float):
                raise self.fail(f"{key}.{name}", "must be a number")
        try:
            return Limits(turns, float(cast(float, cost)), float(cast(float, cooldown)))
        except ValueError as error:
            raise self.fail(key, str(error)) from error

    def leases(self, value: object) -> tuple[tuple[str, ...], int]:
        """The ``[leases]`` table, as its names and their time to live."""
        document = self.table(value, "leases", LEASE_KEYS)
        names = document.get("names", [])
        if not isinstance(names, list) or len(cast(list[object], names)) > MAX_LEASES:
            raise self.fail("leases.names", f"must be a list of at most {MAX_LEASES}")
        found: list[str] = []
        for name in cast(list[object], names):
            if not isinstance(name, str) or ID_PATTERN.fullmatch(name) is None:
                raise self.fail("leases.names", f"{name!r} is not an identifier")
            if name in found:
                raise self.fail("leases.names", f"names {name!r} twice")
            found.append(name)
        ttl = document.get("ttl_seconds", DEFAULT_LEASE_TTL)
        if type(ttl) is not int or not MIN_LEASE_TTL <= ttl <= MAX_LEASE_TTL:
            raise self.fail(
                "leases.ttl_seconds",
                f"must be a whole number from {MIN_LEASE_TTL} to {MAX_LEASE_TTL}",
            )
        return tuple(sorted(found)), ttl

    def roles(
        self, value: object, lines: dict[str, LineDefinition]
    ) -> dict[str, RoleDefinition]:
        """The ``[roles]`` tables. A seat that sets no ``workdir`` of its own takes the
        role's, which is written into ``lines`` so that the line runs there. One that
        sets its own must name the same directory, which the daemon checks once it
        can resolve both."""
        if not isinstance(value, dict):
            raise self.fail("roles", "must be a table")
        found: dict[str, RoleDefinition] = {}
        seated: dict[str, str] = {}
        for role_id, table in sorted(cast(dict[str, object], value).items()):
            role = self.role(role_id, table, lines)
            for line_id in role.seats:
                if line_id in seated:
                    raise self.fail(
                        f"roles.{role_id}.seats",
                        f"line {line_id} is already a seat of role {seated[line_id]}",
                    )
                seated[line_id] = role_id
                line = lines[line_id]
                if line.config.workdir is None:
                    config = replace(line.config, workdir=role.workdir)
                    lines[line_id] = replace(line, config=config)
            found[role_id] = role
        return found

    def role(
        self, role_id: str, value: object, lines: Mapping[str, LineDefinition]
    ) -> RoleDefinition:
        key = f"roles.{role_id}"
        if ID_PATTERN.fullmatch(role_id) is None:
            raise self.fail(key, "is not a valid role identifier")
        table = self.table(value, key, ROLE_KEYS)
        workdir = table.get("workdir")
        try:
            check_workdir(workdir)
        except InvalidField as error:
            raise self.fail(f"{key}.workdir", str(error)) from error
        seats = table.get("seats")
        if not isinstance(seats, list) or not 1 <= len(cast(list[object], seats)) <= (
            MAX_SEATS
        ):
            raise self.fail(
                f"{key}.seats", f"must be a list of 1 to {MAX_SEATS} line identifiers"
            )
        names: list[str] = []
        for seat in cast(list[object], seats):
            if not isinstance(seat, str) or seat not in lines:
                raise self.fail(
                    f"{key}.seats", f"{seat!r} is not a line this file declares"
                )
            if seat in names:
                raise self.fail(f"{key}.seats", f"names {seat!r} twice")
            names.append(seat)
        owner = table.get("owner")
        if owner is not None:
            if not isinstance(owner, str) or owner not in names:
                raise self.fail(f"{key}.owner", f"{owner!r} is not a seat of the role")
            if not lines[owner].may_own:
                raise self.fail(
                    f"{key}.owner", f"line {owner} has may_own false, so cannot own it"
                )
        seconds = table.get("handoff_seconds", DEFAULT_HANDOFF_SECONDS)
        if (
            type(seconds) is not int
            or not MIN_HANDOFF_SECONDS <= seconds <= MAX_HANDOFF_SECONDS
        ):
            raise self.fail(
                f"{key}.handoff_seconds",
                f"must be a whole number from {MIN_HANDOFF_SECONDS} to"
                f" {MAX_HANDOFF_SECONDS}",
            )
        failover = table.get("failover", FailoverPolicy.AUTOMATIC.value)
        if failover not in tuple(FailoverPolicy):
            raise self.fail(
                f"{key}.failover",
                f"must be one of {', '.join(p.value for p in FailoverPolicy)}",
            )
        below = table.get("handoff_below_percent", DEFAULT_HANDOFF_BELOW_PERCENT)
        if type(below) is not int or not 0 <= below <= 100:
            raise self.fail(
                f"{key}.handoff_below_percent", "must be a whole number from 0 to 100"
            )
        return RoleDefinition(
            role_id=role_id,
            workdir=cast(str, workdir),
            seats=tuple(names),
            owner=owner,
            handoff_seconds=seconds,
            failover=FailoverPolicy(cast(str, failover)),
            handoff_below_percent=below,
        )

    def wake(self, value: object, key: str, base: int) -> int:
        document = self.table(value, key, WAKE_KEYS)
        every = document.get("every_ticks", base)
        if type(every) is not int or not 1 <= every <= MAX_WAKE_EVERY:
            raise self.fail(
                f"{key}.every_ticks", f"must be an integer in [1, {MAX_WAKE_EVERY}]"
            )
        return every

    def sandbox(self, value: object, key: str) -> SandboxConfig | None:
        """A sandbox table from the file, which lacks operator authority."""
        if value is None:
            return None
        try:
            return parse_sandbox(value, operator=False)
        except SandboxError as error:
            where = f"{key}.{error.key}" if error.key else key
            raise ConfigFileError(
                self.path, str(error), key=key, line=self.line_of(where)
            ) from error

    def settings(self, value: object, key: str) -> SettingsConfig | None:
        """An engine settings table from the file."""
        if value is None:
            return None
        try:
            return parse_settings(value)
        except SettingsError as error:
            where = f"{key}.{error.key}" if error.key else key
            raise ConfigFileError(
                self.path, str(error), key=key, line=self.line_of(where)
            ) from error

    def profiles(self, value: object) -> dict[str, dict[str, object]]:
        if not isinstance(value, dict):
            raise self.fail("profiles", "must be a table")
        found: dict[str, dict[str, object]] = {}
        for name, profile in cast(dict[str, object], value).items():
            if ID_PATTERN.fullmatch(name) is None:
                raise self.fail(f"profiles.{name}", "is not a valid profile name")
            found[name] = self.table(profile, f"profiles.{name}", PROFILE_KEYS)
        return found

    def line(
        self,
        line_id: str,
        value: object,
        profiles: Mapping[str, dict[str, object]],
        limits: Limits,
        wake: int,
    ) -> LineDefinition:
        key = f"lines.{line_id}"
        if ID_PATTERN.fullmatch(line_id) is None:
            raise self.fail(key, "is not a valid line identifier")
        own = self.table(value, key, LINE_KEYS)
        merged: dict[str, object] = {}
        profile_sandbox: SandboxConfig | None = None
        profile_settings: SettingsConfig | None = None
        profile_name = own.get("profile")
        if profile_name is not None:
            if not isinstance(profile_name, str) or profile_name not in profiles:
                raise self.fail(f"{key}.profile", f"names no profile: {profile_name!r}")
            merged.update(profiles[profile_name])
            profile_sandbox = self.sandbox(
                profiles[profile_name].get("sandbox"),
                f"profiles.{profile_name}.sandbox",
            )
            profile_settings = self.settings(
                profiles[profile_name].get("settings"),
                f"profiles.{profile_name}.settings",
            )
        merged.update({k: v for k, v in own.items() if k != "profile"})
        model = merged.get("model")
        if model not in MODELS:
            raise self.fail(
                f"{key}.model", f"must be one of {list(MODELS)}, found {model!r}"
            )
        strategy_text = merged.get("strategy", Strategy.SUBSCRIPTION.value)
        try:
            strategy = Strategy(cast(str, strategy_text))
        except ValueError as error:
            choices = [s.value for s in Strategy]
            raise self.fail(f"{key}.strategy", f"must be one of {choices}") from error
        tools = {k: v for k, v in merged.items() if k in TOOL_KEYS and k != "secrets"}
        try:
            config = config_from_document(tools)
        except ConfigError as error:
            raise self.fail(key, str(error)) from error
        if self.root is not None:
            try:
                check_instructions(config.instructions, self.root)
            except ConfigError as error:
                where = f"{key}.instructions"
                if "instructions" not in own:
                    where = f"profiles.{profile_name}.instructions"
                raise self.fail(where, f"for line {line_id}, {error}") from error
        raw_secrets = merged.get("secrets", [])
        if (
            not isinstance(raw_secrets, list)
            or len(cast(list[object], raw_secrets)) > 256
        ):
            raise self.fail(f"{key}.secrets", "must be a list of at most 256 names")
        secret_names: list[str] = []
        for name in cast(list[object], raw_secrets):
            if (
                not isinstance(name, str)
                or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name) is None
            ):
                raise self.fail(
                    f"{key}.secrets",
                    f"{name!r} is not an environment variable name",
                )
            if name in secret_names:
                raise self.fail(f"{key}.secrets", f"names {name!r} twice")
            secret_names.append(name)
        used_secrets = {
            name
            for _, server in config.mcp_servers
            for text in (
                server.command,
                *server.args,
                *(value for _, value in server.env),
                server.url,
                *(value for _, value in server.headers),
            )
            for name in references(text)
        }
        denied_secrets = sorted(used_secrets - set(secret_names))
        if denied_secrets:
            raise self.fail(
                f"{key}.secrets",
                "does not grant MCP variable(s) " + ", ".join(denied_secrets),
            )
        # Unlike the tool keys, sandbox and settings tables merge as Claude Code's
        # scopes do, so a line cannot drop a deny entry its profile adds.
        own_sandbox = self.sandbox(own.get("sandbox"), f"{key}.sandbox")
        own_settings = self.settings(own.get("settings"), f"{key}.settings")
        config = replace(
            config,
            sandbox=merge([profile_sandbox, own_sandbox]),
            settings=engine_settings.merge([profile_settings, own_settings]),
        )
        may_own = merged.get("may_own", True)
        if type(may_own) is not bool:
            raise self.fail(f"{key}.may_own", "must be a boolean")
        git_ssh = merged.get("git_ssh", False)
        if type(git_ssh) is not bool:
            raise self.fail(f"{key}.git_ssh", "must be a boolean")
        session = own.get("session")
        if session is not None:
            if not isinstance(session, str):
                raise self.fail(f"{key}.session", "must be text")
            try:
                LineSession(line_id, session)
            except InvalidField as error:
                raise self.fail(f"{key}.session", str(error)) from error
        return LineDefinition(
            line_id=line_id,
            model=cast(str, model),
            strategy=strategy,
            config=config,
            limits=None
            if "limits" not in merged
            else self.limits(merged["limits"], f"{key}.limits", limits),
            wake_every_ticks=None
            if "wake" not in merged
            else self.wake(merged["wake"], f"{key}.wake", wake),
            session=session,
            may_own=may_own,
            secret_names=tuple(sorted(secret_names)),
            git_ssh=git_ssh,
        )
