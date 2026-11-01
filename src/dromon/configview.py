"""The settings a client shows, each with the layer its value comes from.

Claude Code's ``/config`` and ``/status``, and Codex's ``/status``, show an operator
what is in force and why. This module computes the same for Dromon's two scopes, the
project and a line, from the layers that ``dromon.overrides`` orders. The daemon calls
it to answer ``settings`` and to find the project-wide values it runs with.

Each entry is a plain object. Entries of kind ``info`` are shown but cannot be changed
from a client, such as a line's strategy or its MCP servers, whose values are shown by
name only, so that no credential in a server's environment or headers is sent to a
client.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

from dromon.driver import ENGINE_OF, Driver, DriverError, Strategy
from dromon.limits import DEFAULT_LIMITS, Limits
from dromon.lineconfig import DEFAULT_CONTEXT_WARN_TOKENS, LineConfig
from dromon.model import OperatorAddress
from dromon.overrides import (
    SPECS,
    Layer,
    Scope,
    Spec,
    Value,
    decimal,
    pick,
    spec_of,
    to_json,
)
from dromon.projectconfig import ProjectConfig
from dromon.scheduler import (
    DEFAULT_OPERATOR,
    DEFAULT_TICK_SECONDS,
    MAX_TICK_SECONDS,
    MAX_WAKE_EVERY_TICKS,
)

COMMAND_LINE: Final = "command line"
OVERRIDE: Final = "override"
PROJECT_FILE: Final = "project file"
DEFAULT: Final = "default"
CONFIGURED: Final = "line configuration"
"""A line's own configuration, from the project file, its creation or ``configure``."""
LINE_FILE: Final = "line in project file"
CREATED: Final = "created"
REBOUND: Final = "rebound"
DRIVER: Final = "driver"


@dataclass(frozen=True, slots=True)
class Overrides:
    """Settings given on the command line, which take precedence over the file and
    over overrides from a client."""

    operator: str | None = None
    tick_seconds: float | None = None
    max_autonomous_turns: int | None = None
    max_cost_usd: float | None = None
    cooldown_seconds: float | None = None


def _limit_values(limits: Limits) -> dict[str, Value]:
    return {
        "limits.max_autonomous_turns": limits.max_autonomous_turns,
        "limits.max_cost_usd": decimal(limits.max_cost_usd),
        "limits.cooldown_seconds": decimal(limits.cooldown_seconds),
    }


def project_layers(
    config: ProjectConfig,
    command_line: Overrides,
    overrides: Mapping[str, Value],
    file_present: bool,
) -> list[Layer]:
    """The project's layers, highest first.

    Limitation. The project file's reader fills omitted limits with their defaults,
    so while a file is present its layer names a limit it may not spell out.
    """
    given: dict[str, Value] = {}
    if command_line.max_autonomous_turns is not None:
        given["limits.max_autonomous_turns"] = command_line.max_autonomous_turns
    if command_line.max_cost_usd is not None:
        given["limits.max_cost_usd"] = decimal(command_line.max_cost_usd)
    if command_line.cooldown_seconds is not None:
        given["limits.cooldown_seconds"] = decimal(command_line.cooldown_seconds)
    if command_line.operator is not None:
        given["daemon.operator"] = command_line.operator
    if command_line.tick_seconds is not None:
        given["daemon.tick_seconds"] = decimal(command_line.tick_seconds)
    file: dict[str, Value] = {}
    if file_present:
        file.update(_limit_values(config.limits))
        file["wake.every_ticks"] = config.wake_every_ticks
        if config.operator is not None:
            file["daemon.operator"] = config.operator
        if config.tick_seconds is not None:
            file["daemon.tick_seconds"] = decimal(config.tick_seconds)
    defaults: dict[str, Value] = {
        **_limit_values(DEFAULT_LIMITS),
        "wake.every_ticks": 1,
        "daemon.operator": DEFAULT_OPERATOR,
        "daemon.tick_seconds": decimal(DEFAULT_TICK_SECONDS),
    }
    return [
        Layer(COMMAND_LINE, given),
        Layer(OVERRIDE, dict(overrides)),
        Layer(PROJECT_FILE, file),
        Layer(DEFAULT, defaults),
    ]


def _picked(key: str, layers: Sequence[Layer]) -> tuple[Value, str]:
    found = pick(key, layers)
    if found is None:
        raise ValueError(f"no layer gives {key}")
    return found


@dataclass(frozen=True, slots=True)
class ProjectValues:
    limits: Limits
    operator: str
    tick_seconds: float
    wake_every_ticks: int


def project_values(layers: Sequence[Layer]) -> ProjectValues:
    """The project-wide values the layers give, or ``ValueError`` or
    ``dromon.model.InvalidField`` for a combination that does not apply."""

    def number(key: str) -> float:
        return float(cast(str, _picked(key, layers)[0]))

    limits = Limits(
        cast(int, _picked("limits.max_autonomous_turns", layers)[0]),
        number("limits.max_cost_usd"),
        number("limits.cooldown_seconds"),
    )
    operator = OperatorAddress(cast(str, _picked("daemon.operator", layers)[0]))
    tick = number("daemon.tick_seconds")
    if not 0 <= tick <= MAX_TICK_SECONDS:
        raise ValueError(f"daemon.tick_seconds must be in [0, {MAX_TICK_SECONDS}]")
    wake = cast(int, _picked("wake.every_ticks", layers)[0])
    if not 1 <= wake <= MAX_WAKE_EVERY_TICKS:
        raise ValueError(f"wake.every_ticks must be in [1, {MAX_WAKE_EVERY_TICKS}]")
    return ProjectValues(limits, operator.operator_id, tick, wake)


def entry(
    spec: Spec,
    value: Value | None,
    source: str,
    choices: Sequence[str] | None = None,
) -> dict[str, object]:
    return {
        "key": spec.key,
        "kind": spec.kind.value,
        "summary": spec.summary,
        "choices": list(spec.choices if choices is None else choices),
        "value": None if value is None else to_json(value),
        "source": source,
        "editable": True,
    }


def info(key: str, summary: str, value: str) -> dict[str, object]:
    return {
        "key": key,
        "kind": "info",
        "summary": summary,
        "choices": [],
        "value": value,
        "source": "",
        "editable": False,
    }


def project_entries(
    layers: Sequence[Layer], config: ProjectConfig, file: str, file_present: bool
) -> list[dict[str, object]]:
    entries = [
        entry(spec, *_picked(spec.key, layers))
        for spec in SPECS
        if spec.scope is Scope.PROJECT
    ]
    teacher = config.teacher
    entries.extend(
        [
            info(
                "file", "the project file", file if file_present else f"{file}, absent"
            ),
            info(
                "lines",
                "lines the file declares",
                ", ".join(sorted(config.lines)) or "none",
            ),
            info("sandbox", "the project's sandbox", _present(config.sandbox)),
            info(
                "daemon.require_sandbox",
                "whether every Claude Code line needs an enabled sandbox",
                "on" if config.require_sandbox else "off",
            ),
            info(
                "daemon.disclosure_terms",
                "the disclosure list messages are checked against, by path only",
                config.disclosure_terms or "none",
            ),
            info(
                "leases",
                "the leases lines may acquire, and how long each lasts",
                (", ".join(config.lease_names) or "none")
                + f", {config.lease_ttl_seconds} seconds",
            ),
            info(
                "settings", "the project's engine settings", _present(config.settings)
            ),
            info(
                "teacher",
                "the local teacher model",
                "none" if teacher is None else teacher.model,
            ),
            info(
                "agents",
                "external agents",
                f"{'listening' if config.agents.listen else 'off'},"
                f" at most {config.agents.max_access.value}",
            ),
        ]
    )
    return entries


def _present(document: object | None) -> str:
    return "none" if document is None else "set, edit the project file to change it"


@dataclass(frozen=True, slots=True)
class LineFacts:
    """What the scheduler knows of a line, which the daemon gathers."""

    line_id: str
    created_model: str
    model: str
    model_choices: tuple[str, ...]
    strategy: str
    session: str | None
    config: LineConfig
    overrides: Mapping[str, Value]
    limits: Limits
    own_limits: bool
    """Whether the project file gives the line limits of its own."""
    wake_every_ticks: int
    own_wake: bool
    rebound: bool = False
    """Whether a rebind set the line's current binding."""


def _driver_effort(model: str, strategy: str) -> str:
    try:
        return Driver.for_model(model, Strategy(strategy)).settings.effort
    except (DriverError, ValueError):
        return "unknown"


COMPACTING_ENGINES: Final = ("claude-code", "codex")
"""The engines whose lines can compact, as ``Scheduler._may_compact`` judges, repeated
here because the view takes no scheduler. A test holds the two equal."""


def sourced(key: str, summary: str, value: str, origin: str) -> dict[str, object]:
    """An entry that is shown and changed only by configuration, with where its value
    comes from, which ``info`` leaves empty."""
    return {**info(key, summary, value), "source": origin}


def _compaction_entries(facts: LineFacts) -> list[dict[str, object]]:
    """``allow_compact`` and the thresholds it works with, each with its source.
    **Round 14, item 1.** ``dromon settings`` showed none of them, so an operator could
    not tell why a line had no ``compact_session``. A value the line's configuration
    sets has the source ``line configuration``, and one it leaves unset is the
    ``default``."""
    config = facts.config
    engine = ENGINE_OF.get(facts.model, "")
    if not config.allow_compact:
        allowed = "off, so the line has no compact_session and no automatic compaction"
    elif engine in COMPACTING_ENGINES:
        allowed = "on, so the line has compact_session and compacts at its warning"
    else:
        allowed = f"on, but {engine or 'its engine'} cannot compact"
    automatic = config.auto_compact_tokens
    return [
        sourced(
            "allow_compact",
            "whether a Claude Code or Codex line has compact_session and compacts"
            " automatically at its warning, changed only by configuration",
            allowed,
            CONFIGURED if config.allow_compact else DEFAULT,
        ),
        sourced(
            "context_warn_tokens",
            "the context size past which the line's prompt warns, changed only by"
            " configuration",
            str(config.context_warn_tokens),
            CONFIGURED
            if config.context_warn_tokens != DEFAULT_CONTEXT_WARN_TOKENS
            else DEFAULT,
        ),
        sourced(
            "auto_compact_tokens",
            "an earlier context size at which a compactable line compacts, changed"
            " only by configuration",
            "none, so the warning is the threshold"
            if automatic is None
            else str(automatic),
            DEFAULT if automatic is None else CONFIGURED,
        ),
    ]


def line_entries(facts: LineFacts, project: Sequence[Layer]) -> list[dict[str, object]]:
    """A line's settings. ``project`` gives the sources of values the line takes from
    the project."""
    overrides = facts.overrides
    config = facts.config

    def source(key: str, otherwise: str) -> str:
        return OVERRIDE if key in overrides else otherwise

    entries: list[dict[str, object]] = [
        entry(
            spec_of(Scope.LINE, "model"),
            facts.model,
            source("model", REBOUND if facts.rebound else CREATED),
            facts.model_choices,
        ),
        info("engine", "the engine the model runs on", ENGINE_OF.get(facts.model, "")),
        info("strategy", "the credential policy, fixed at creation", facts.strategy),
        info(
            "session",
            "the engine session the next turn resumes",
            facts.session or "new",
        ),
        info(
            "workdir",
            "the directory the engine runs in, changed only by configuration",
            facts.config.workdir or "the project root",
        ),
        info(
            "project_settings",
            "whether the line loads Claude Code's project settings, changed only by"
            " configuration",
            "on" if facts.config.project_settings else "off",
        ),
        info(
            "plugins",
            "local plugin directories the line loads, changed only by configuration",
            ", ".join(facts.config.plugins) or "none",
        ),
        info(
            "keep_session",
            "whether the engine stays connected between turns, changed only by"
            " configuration",
            (
                f"on, closing after {facts.config.keep_session_idle_seconds} seconds"
                f" idle, or {facts.config.keep_session_max_seconds} with background"
                f" tasks running, where it does"
                f" {facts.config.keep_session_at_max.value}, and at the latest after"
                f" {facts.config.keep_session_hard_seconds}"
            )
            if facts.config.keep_session
            else "off",
        ),
        *_compaction_entries(facts),
        info(
            "stop_policy",
            "whether stopping the engine ends what it started, changed only by"
            " configuration",
            facts.config.stop_policy.value,
        ),
    ]
    effort = config.effort
    entries.append(
        entry(
            spec_of(Scope.LINE, "effort"),
            effort or _driver_effort(facts.model, facts.strategy),
            source("effort", CONFIGURED if effort else DRIVER),
        )
    )
    fields: dict[str, Value] = {
        "builtin_tools": config.builtin_tools,
        "tool_policy": config.tool_policy.value,
        "allowed_tools": config.allowed_tools,
        "inherit_account_mcp": config.inherit_account_mcp,
        "claude_code_prompt": config.claude_code_prompt,
        "instructions": config.instructions,
    }
    for key, value in fields.items():
        entries.append(entry(spec_of(Scope.LINE, key), value, source(key, CONFIGURED)))
    entries.extend(
        [
            info(
                "mcp_servers",
                "MCP servers, by name",
                ", ".join(sorted(config.server_names())) or "none",
            ),
            info("sandbox", "the line's own sandbox", _present(config.sandbox)),
            info(
                "settings", "the line's own engine settings", _present(config.settings)
            ),
        ]
    )
    limit_values = _limit_values(facts.limits)
    for key, value in limit_values.items():
        if key in overrides:
            origin = OVERRIDE
        elif facts.own_limits:
            origin = LINE_FILE
        else:
            origin = "project, " + _picked(key, project)[1]
        entries.append(entry(spec_of(Scope.LINE, key), value, origin))
    if "wake.every_ticks" in overrides:
        origin = OVERRIDE
    elif facts.own_wake:
        origin = LINE_FILE
    else:
        origin = "project, " + _picked("wake.every_ticks", project)[1]
    entries.append(
        entry(spec_of(Scope.LINE, "wake.every_ticks"), facts.wake_every_ticks, origin)
    )
    return entries
