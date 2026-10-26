"""Settings an operator changes from a client, layered over the project file.

Claude Code, Codex and Grok Build each let an operator change settings from inside an
interactive session, and keep shared project settings apart from local ones. Dromon
does the same at two scopes. A line override changes one line, and a project override
changes the whole daemon. Both are stored in the event log, so they survive a restart
and every change is on record, and both sit over the project file, which stays the
shared, reviewed layer that the daemon never writes.

Precedence, highest first.

- Project keys. The command line, then the project override, then the project file,
  then the built-in default. The command line wins, as Claude Code's does, so a value
  set from a client while a flag is in force is recorded but shadowed, and the client
  is told so.
- Line keys. The line override, then the line's own configuration from the project
  file or its creation, then, for limits and the wake policy, the project's value.

Values. An override document maps flat keys such as ``limits.max_cost_usd`` to plain
JSON values. Decimal numbers are strings of digits, because the client protocol
carries no floats. Each key is checked against its specification here, and again in
meaning where it is applied, so a stored override is never trusted blindly. A stored
override that no longer applies, such as a model a later version stops packaging, is
skipped and reported rather than stopping the daemon.

What a client cannot change. MCP servers, sandbox documents, engine settings documents,
the teacher, external agent policy and a line's strategy. The first five are long
documents that belong in a reviewed file. The strategy decides whether usage is billed
per token, and such usage must never start from a one-word command, so a line that
should bill differently is created anew.

Porting note. Flat keys and plain values map onto a Rust ``enum`` of keys and a value
``enum``, with no reflection.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from dromon.limits import Limits
from dromon.lineconfig import (
    EFFORTS,
    LineConfig,
    ToolPolicy,
    config_from_document,
    encode_config,
)

MAX_OVERRIDE_BYTES: Final = 64 * 1024
MAX_LIST: Final = 64
MAX_VALUE_TEXT: Final = 1024
DECIMAL: Final = re.compile(r"[0-9]{1,6}(\.[0-9]{1,6})?")


class OverrideError(ValueError):
    """An override names an unknown key or holds a value its key does not accept."""


class Scope(StrEnum):
    LINE = "line"
    PROJECT = "project"


class Kind(StrEnum):
    BOOL = "bool"
    INT = "int"
    DECIMAL = "decimal"
    """A non-negative decimal number, carried as a string of digits."""
    TEXT = "text"
    CHOICE = "choice"
    LIST = "list"
    """A list of texts."""


type Value = bool | int | str | tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Spec:
    key: str
    scope: Scope
    kind: Kind
    summary: str
    choices: tuple[str, ...] = ()
    """The values a choice accepts. Empty for ``model``, whose choices depend on the
    line's engine and are given where the line is known."""


LIMIT_SPECS: Final = (
    ("limits.max_autonomous_turns", Kind.INT, "turns in a row without operator mail"),
    ("limits.max_cost_usd", Kind.DECIMAL, "estimated cost before the line is held"),
    ("limits.cooldown_seconds", Kind.DECIMAL, "least time between a line's turns"),
)

SPECS: Final = (
    Spec("model", Scope.LINE, Kind.CHOICE, "the model, on the line's engine"),
    Spec("effort", Scope.LINE, Kind.CHOICE, "the reasoning effort", EFFORTS),
    Spec("builtin_tools", Scope.LINE, Kind.LIST, "the engine's built-in tools"),
    Spec(
        "tool_policy",
        Scope.LINE,
        Kind.CHOICE,
        "which tool calls are allowed",
        tuple(policy.value for policy in ToolPolicy),
    ),
    Spec("allowed_tools", Scope.LINE, Kind.LIST, "tools an allowlist also allows"),
    Spec(
        "inherit_account_mcp",
        Scope.LINE,
        Kind.BOOL,
        "load the account's own MCP servers",
    ),
    Spec(
        "claude_code_prompt",
        Scope.LINE,
        Kind.BOOL,
        "run on Claude Code's system prompt",
    ),
    Spec("instructions", Scope.LINE, Kind.LIST, "files appended to the system prompt"),
    *(Spec(key, Scope.LINE, kind, summary) for key, kind, summary in LIMIT_SPECS),
    Spec("wake.every_ticks", Scope.LINE, Kind.INT, "ticks between possible wakes"),
    *(Spec(key, Scope.PROJECT, kind, summary) for key, kind, summary in LIMIT_SPECS),
    Spec("wake.every_ticks", Scope.PROJECT, Kind.INT, "ticks between possible wakes"),
    Spec("daemon.operator", Scope.PROJECT, Kind.TEXT, "who hears about held lines"),
    Spec("daemon.tick_seconds", Scope.PROJECT, Kind.DECIMAL, "time between ticks"),
)

CONFIG_KEYS: Final = frozenset(
    {
        "effort",
        "builtin_tools",
        "tool_policy",
        "allowed_tools",
        "inherit_account_mcp",
        "claude_code_prompt",
        "instructions",
    }
)
"""Line keys that are fields of the line's configuration."""


def spec_of(scope: Scope, key: str) -> Spec:
    for spec in SPECS:
        if spec.scope is scope and spec.key == key:
            return spec
    raise OverrideError(f"{key} is not a {scope} setting a client can change")


def check(spec: Spec, value: object) -> Value:
    """``value`` as its key accepts it, or ``OverrideError``."""
    match spec.kind:
        case Kind.BOOL:
            if type(value) is not bool:
                raise OverrideError(f"{spec.key} takes true or false")
            return value
        case Kind.INT:
            if type(value) is not int or not 0 <= value <= 1_000_000:
                raise OverrideError(f"{spec.key} takes a whole number")
            return value
        case Kind.DECIMAL:
            if not isinstance(value, str) or DECIMAL.fullmatch(value) is None:
                raise OverrideError(f"{spec.key} takes a decimal number such as 2.5")
            return value
        case Kind.TEXT | Kind.CHOICE:
            if not isinstance(value, str) or not 0 < len(value) <= MAX_VALUE_TEXT:
                raise OverrideError(f"{spec.key} takes a text")
            if spec.choices and value not in spec.choices:
                raise OverrideError(
                    f"{spec.key} takes one of {', '.join(spec.choices)}"
                )
            return value
        case Kind.LIST:
            if not isinstance(value, (list, tuple)):
                raise OverrideError(f"{spec.key} takes a list")
            items = cast(Sequence[object], value)
            if len(items) > MAX_LIST or not all(
                isinstance(item, str) and 0 < len(item) <= MAX_VALUE_TEXT
                for item in items
            ):
                raise OverrideError(
                    f"{spec.key} takes at most {MAX_LIST} non-empty texts"
                )
            return tuple(cast(Sequence[str], items))


def to_json(value: Value) -> object:
    return list(value) if isinstance(value, tuple) else value


def encode(document: Mapping[str, Value]) -> str:
    """The canonical JSON of an override document, as stored in the event log."""
    text = json.dumps(
        {key: to_json(value) for key, value in document.items()},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(text.encode("utf-8")) > MAX_OVERRIDE_BYTES:
        raise OverrideError("the overrides are too large")
    return text


def decode(scope: Scope, text: str | None) -> tuple[dict[str, Value], list[str]]:
    """A stored override document, and a reason for each key it had to skip."""
    if text is None:
        return {}, []
    loaded: object = json.loads(text)
    if not isinstance(loaded, dict):
        return {}, ["the stored overrides are not an object"]
    document: dict[str, Value] = {}
    skipped: list[str] = []
    for key, value in cast(dict[str, object], loaded).items():
        try:
            document[key] = check(spec_of(scope, key), value)
        except OverrideError as error:
            skipped.append(str(error))
    return document, skipped


def decimal(value: float) -> str:
    """``value`` as a decimal string, without trailing zeros."""
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def apply_config(config: LineConfig, overrides: Mapping[str, Value]) -> LineConfig:
    """``config`` with the override's configuration keys, checked as the project
    file's are, or ``dromon.lineconfig.ConfigError``."""
    chosen = {key: value for key, value in overrides.items() if key in CONFIG_KEYS}
    if not chosen:
        return config
    document = cast(dict[str, object], json.loads(encode_config(config)))
    document.pop("v", None)
    document.update((key, to_json(value)) for key, value in chosen.items())
    return config_from_document(document)


def apply_limits(
    limits: Limits, overrides: Mapping[str, Value], prefix: str = "limits."
) -> Limits:
    """``limits`` with the override's limit keys, or ``ValueError``."""

    def number(name: str, base: float) -> float:
        value = overrides.get(prefix + name)
        return base if value is None else float(cast(str, value))

    turns = overrides.get(prefix + "max_autonomous_turns", limits.max_autonomous_turns)
    return Limits(
        cast(int, turns),
        number("max_cost_usd", limits.max_cost_usd),
        number("cooldown_seconds", limits.cooldown_seconds),
    )


def changed(
    document: Mapping[str, Value], key: str, value: Value | None
) -> dict[str, Value]:
    """``document`` with ``key`` set to ``value``, or removed when it is ``None``."""
    updated = dict(document)
    if value is None:
        updated.pop(key, None)
    else:
        updated[key] = value
    return updated


@dataclass(frozen=True, slots=True)
class Layer:
    """One source of values, such as the project file or the command line."""

    source: str
    values: Mapping[str, Value]


def pick(key: str, layers: Sequence[Layer]) -> tuple[Value, str] | None:
    """The value of ``key`` in the first layer that has it, with that layer's name."""
    for layer in layers:
        if key in layer.values:
            return layer.values[key], layer.source
    return None
