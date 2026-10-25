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
from dromon.lineconfig import ConfigError, LineConfig, config_from_document
from dromon.model import ID_PATTERN
from dromon import settings as engine_settings
from dromon.sandbox import SandboxConfig, SandboxError, merge, parse_sandbox
from dromon.settings import SettingsConfig, SettingsError, parse_settings

CONFIG_FILE_NAME: Final = "dromon.toml"
FILE_VERSION: Final = 1
MAX_WAKE_EVERY: Final = 3600
MAX_FILE_BYTES: Final = 4 * 1024 * 1024

TOP_KEYS: Final = frozenset(
    {"version", "daemon", "limits", "wake", "sandbox", "settings", "profiles", "lines"}
)
DAEMON_KEYS: Final = frozenset({"operator", "tick_seconds"})
LIMIT_KEYS: Final = frozenset(
    {"max_autonomous_turns", "max_cost_usd", "cooldown_seconds"}
)
WAKE_KEYS: Final = frozenset({"every_ticks"})
TOOL_KEYS: Final = frozenset(
    {
        "builtin_tools",
        "mcp_servers",
        "tool_policy",
        "allowed_tools",
        "inherit_account_mcp",
        "effort",
    }
)
LAYERED_KEYS: Final = frozenset({"sandbox", "settings"})
"""Keys whose profile and line tables merge as Claude Code's scopes do."""
LINE_KEYS: Final = (
    frozenset({"profile", "model", "strategy", "limits", "wake"})
    | TOOL_KEYS
    | LAYERED_KEYS
)
PROFILE_KEYS: Final = LINE_KEYS - {"profile"}


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
    lines: dict[str, LineDefinition] = field(default_factory=dict[str, LineDefinition])


def config_path(state_dir: Path) -> Path:
    return state_dir / CONFIG_FILE_NAME


_LOCATION = re.compile(r"\(at line (\d+), column (\d+)\)")


def load(path: Path) -> ProjectConfig:
    """Read and validate the file at ``path``, or raise ``ConfigFileError``."""
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
    return parse(text, path)


def parse(text: str, path: Path = Path(CONFIG_FILE_NAME)) -> ProjectConfig:
    """Validate ``text`` as a project file, for tests and tools that hold the text."""
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
    return _Reader(path, text).project(document)


class _Reader:
    """Validation of one decoded file, which knows where each table starts."""

    def __init__(self, path: Path, text: str) -> None:
        self.path = path
        self.lines = text.splitlines()

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
        limits = self.limits(top.get("limits", {}), "limits", DEFAULT_LIMITS)
        wake = self.wake(top.get("wake", {}), "wake", 1)
        sandbox = self.sandbox(top.get("sandbox"), "sandbox")
        settings = self.settings(top.get("settings"), "settings")
        profiles = self.profiles(top.get("profiles", {}))
        lines_table = top.get("lines", {})
        if not isinstance(lines_table, dict):
            raise self.fail("lines", "must be a table")
        lines: dict[str, LineDefinition] = {}
        for line_id, value in sorted(cast(dict[str, object], lines_table).items()):
            lines[line_id] = self.line(line_id, value, profiles, limits, wake)
        return ProjectConfig(
            operator=operator,
            tick_seconds=tick,
            limits=limits,
            wake_every_ticks=wake,
            sandbox=sandbox,
            settings=settings,
            lines=lines,
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
        tools = {k: v for k, v in merged.items() if k in TOOL_KEYS}
        try:
            config = config_from_document(tools)
        except ConfigError as error:
            raise self.fail(key, str(error)) from error
        # Unlike the tool keys, sandbox and settings tables merge as Claude Code's
        # scopes do, so a line cannot drop a deny entry its profile adds.
        own_sandbox = self.sandbox(own.get("sandbox"), f"{key}.sandbox")
        own_settings = self.settings(own.get("settings"), f"{key}.settings")
        config = replace(
            config,
            sandbox=merge([profile_sandbox, own_sandbox]),
            settings=engine_settings.merge([profile_settings, own_settings]),
        )
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
        )
