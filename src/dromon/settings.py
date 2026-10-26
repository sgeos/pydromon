"""Engine settings of a line, a strict subset of Claude Code's settings file.

A settings document holds the Claude Code settings that the operator's harnesses were
found to use and that change what a line does, with Claude Code's key names, so that
one can be copied from ``.claude/settings.json``:

- ``permissions``, with ``allow``, ``ask`` and ``deny`` rules such as ``Bash(git *)``,
  ``WebFetch(domain:github.com)`` or ``Read(~/.ssh/**)``.
- ``hooks``, command hooks keyed by event, such as ``SessionStart``.
- ``env``, environment variables for the engine and every command it runs.
- ``attribution``, the text Claude Code appends to commits and pull requests.

Dromon validates the document strictly, merges the project's, a profile's and a line's
documents, and hands the result to Claude Code through its ``--settings`` flag, so that
Claude Code applies each key with its own semantics. ``docs/protocol/SETTINGS.md`` is
normative.

Unattended rules. Claude Code asks a person about a call that matches an ``ask`` rule.
A Dromon line has nobody at the prompt and a question never pauses it, so an ``ask``
rule is handed to the engine as a ``deny`` rule, as Claude Code's ``dontAsk`` mode
treats it. The line can then ask the operator with ``ask_operator``.

Protected paths. Every line's settings deny the built-in file tools Dromon's state and
runtime directories, which hold the event log and its stored credentials, the project
file and the daemon's socket. The sandbox denies them to shell commands only when it
is enabled, and these rules cover ``Read``, ``Edit`` and the tools Claude Code groups
with them whether or not it is. A deny rule wins over every allow rule in Claude Code,
so no layer can lift them.

Trust. Hooks run outside the sandbox with the daemon's privileges, a ``PreToolUse`` hook
can approve a call before Dromon's gate sees it, and an ``allow`` rule approves calls
the same way. These keys therefore carry the trust of a stdio MCP server, which the
project file can already declare. Variables that would change the credential or the
provider, such as ``ANTHROPIC_API_KEY``, are refused in ``env`` everywhere, because
Claude Code applies ``env`` to itself and usage billed per token must never start
silently.

Porting note. Documents travel as canonical JSON, which the Rust port can carry
unchanged. Hook order is significant to a reader and is kept, while rule arrays are
sorted, as in ``dromon.sandbox``.
"""

import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, cast

MAX_ENTRIES: Final = 256
MAX_TEXT: Final = 4096
MAX_HOOK_TIMEOUT: Final = 86400

KEYS: Final = frozenset({"permissions", "hooks", "env", "attribution"})
PERMISSION_KEYS: Final = ("allow", "ask", "deny")
ATTRIBUTION_KEYS: Final = frozenset({"commit", "pr"})

HOOK_EVENTS: Final = frozenset(
    {
        "SessionStart",
        "SessionEnd",
        "UserPromptSubmit",
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "PostToolUseFailure",
        "Notification",
        "SubagentStart",
        "SubagentStop",
        "Stop",
        "StopFailure",
        "PreCompact",
    }
)
"""The hook events of Claude Code that Dromon accepts, as documented in its hooks
reference and as the operator's settings use them."""

RULE_PATTERN: Final = re.compile(
    r"(?:[A-Z][A-Za-z0-9_]{0,63}"
    r"|mcp__[A-Za-z0-9][A-Za-z0-9._-]{0,63}(?:__(?:\*|[A-Za-z0-9_.-]{1,128}))?)"
    r"(?:\([^\x00-\x1f]{1,4000}\))?"
)
"""A permission rule, ``Tool`` or ``Tool(specifier)``. Claude Code parses the
specifier, so Dromon checks only its shape."""

VARIABLE_PATTERN: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")

RESERVED_PREFIXES: Final = (
    "ANTHROPIC_",
    "CLAUDE_CODE_USE_",
    "OPENAI_",
    "CODEX_",
    "XAI_",
)
RESERVED_VARIABLES: Final = frozenset(
    {
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "AWS_BEARER_TOKEN_BEDROCK",
    }
)


def reserved_variable(name: str) -> bool:
    """Whether a line may not set or reference ``name``, because it selects or holds
    the engine's credential or provider."""
    return name in RESERVED_VARIABLES or name.startswith(RESERVED_PREFIXES)


class SettingsError(ValueError):
    """A settings document is malformed or sets something a line may not have.

    ``key`` is the dotted path of the offending key within the document, such as
    ``permissions.deny``, or empty for the document itself.
    """

    def __init__(self, message: str, key: str = "") -> None:
        super().__init__(message)
        self.key = key


def _fail(message: str, key: str = "") -> NoReturn:
    raise SettingsError(message, key)


def _table(value: object, what: str, key: str) -> dict[str, object]:
    if not isinstance(value, dict):
        _fail(f"{what} must be a table", key)
    table = cast(dict[str, object], value)
    if len(table) > MAX_ENTRIES:
        _fail(f"{what} has more than {MAX_ENTRIES} entries", key)
    return table


def _list(value: object, what: str, key: str) -> list[object]:
    if not isinstance(value, list):
        _fail(f"{what} must be an array", key)
    items = cast(list[object], value)
    if len(items) > MAX_ENTRIES:
        _fail(f"{what} has more than {MAX_ENTRIES} entries", key)
    return items


def _text(value: object, what: str, key: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > MAX_TEXT or not (value or empty):
        low = 0 if empty else 1
        _fail(f"{what} must be text of {low} to {MAX_TEXT} characters", key)
    if "\x00" in value:
        _fail(f"{what} must not contain a NUL character", key)
    return value


def _known(
    table: Mapping[str, object], known: frozenset[str], what: str, key: str
) -> None:
    for name in sorted(table):
        if name not in known:
            dotted = f"{key}.{name}" if key else name
            _fail(
                f"{what}.{name} is not a known key. Known keys are {sorted(known)}",
                dotted,
            )


def _permissions(value: object) -> dict[str, object]:
    table = _table(value, "settings.permissions", "permissions")
    _known(table, frozenset(PERMISSION_KEYS), "settings.permissions", "permissions")
    checked: dict[str, object] = {}
    for name in PERMISSION_KEYS:
        if name not in table:
            continue
        key = f"permissions.{name}"
        what = f"settings.{key}"
        rules: set[str] = set()
        for item in _list(table[name], what, key):
            rule = _text(item, what, key)
            if RULE_PATTERN.fullmatch(rule) is None:
                _fail(f"{what} entry {rule!r} is not Tool or Tool(specifier)", key)
            rules.add(rule)
        checked[name] = sorted(rules)
    return checked


def _hook(value: object, what: str, key: str) -> dict[str, object]:
    table = _table(value, what, key)
    _known(table, frozenset({"type", "command", "timeout"}), what, key)
    if table.get("type") != "command":
        _fail(f"{what}.type must be command, the only hook type Dromon supports", key)
    hook: dict[str, object] = {
        "type": "command",
        "command": _text(table.get("command"), f"{what}.command", key),
    }
    if "timeout" in table:
        timeout = table["timeout"]
        if type(timeout) is not int or not 1 <= timeout <= MAX_HOOK_TIMEOUT:
            _fail(f"{what}.timeout must be seconds in [1, {MAX_HOOK_TIMEOUT}]", key)
        hook["timeout"] = timeout
    return hook


def _hooks(value: object) -> dict[str, object]:
    table = _table(value, "settings.hooks", "hooks")
    checked: dict[str, object] = {}
    for event in sorted(table):
        key = f"hooks.{event}"
        what = f"settings.{key}"
        if event not in HOOK_EVENTS:
            _fail(
                f"{what} is not a known hook event. Known events are"
                f" {sorted(HOOK_EVENTS)}",
                key,
            )
        groups: list[object] = []
        for item in _list(table[event], what, key):
            group = _table(item, f"{what} entry", key)
            _known(group, frozenset({"matcher", "hooks"}), f"{what} entry", key)
            checked_group: dict[str, object] = {}
            if "matcher" in group:
                checked_group["matcher"] = _text(
                    group["matcher"], f"{what} matcher", key, empty=True
                )
            hooks = _list(group.get("hooks"), f"{what} hooks", key)
            if not hooks:
                _fail(f"{what} hooks must not be empty", key)
            checked_group["hooks"] = [_hook(h, f"{what} hook", key) for h in hooks]
            if checked_group not in groups:
                groups.append(checked_group)
        checked[event] = groups
    return checked


def _env(value: object) -> dict[str, object]:
    table = _table(value, "settings.env", "env")
    checked: dict[str, object] = {}
    for name, item in sorted(table.items()):
        key = f"env.{name}"
        if VARIABLE_PATTERN.fullmatch(name) is None:
            _fail(f"settings.env name {name!r} is not a variable name", key)
        if reserved_variable(name):
            _fail(
                f"settings.env may not set {name}, which selects or holds the engine's"
                " credential or provider",
                key,
            )
        checked[name] = _text(item, f"settings.{key}", key, empty=True)
    return checked


def _attribution(value: object) -> dict[str, object]:
    table = _table(value, "settings.attribution", "attribution")
    _known(table, ATTRIBUTION_KEYS, "settings.attribution", "attribution")
    return {
        name: _text(
            item, f"settings.attribution.{name}", f"attribution.{name}", empty=True
        )
        for name, item in sorted(table.items())
    }


CHECKS: Final = {
    "permissions": _permissions,
    "hooks": _hooks,
    "env": _env,
    "attribution": _attribution,
}


def canonical(document: Mapping[str, object]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


@dataclass(frozen=True, slots=True)
class SettingsConfig:
    """A validated settings document, held as canonical JSON so it compares and hashes.

    Construct one with ``parse_settings``, never directly.
    """

    text: str

    def document(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.text))

    def summary(self) -> str:
        """A short description for ``dromon config``, which never shows values."""
        document = self.document()
        permissions = cast(dict[str, list[str]], document.get("permissions", {}))
        rules = sum(len(found) for found in permissions.values())
        hooks = cast(
            dict[str, list[dict[str, list[object]]]], document.get("hooks", {})
        )
        commands = sum(len(g["hooks"]) for groups in hooks.values() for g in groups)
        variables = len(cast(dict[str, str], document.get("env", {})))
        parts = [f"{rules} permission rules", f"{commands} hooks", f"{variables} env"]
        if "attribution" in document:
            parts.append("attribution")
        return ", ".join(parts)


def parse_settings(value: object) -> SettingsConfig:
    """Validate ``value`` as a settings document, or raise ``SettingsError``."""
    table = _table(value, "settings", "")
    _known(table, KEYS, "settings", "")
    checked = {name: CHECKS[name](item) for name, item in table.items()}
    return SettingsConfig(canonical(checked))


def split_settings_file(value: object) -> tuple[SettingsConfig | None, object | None]:
    """A client-supplied Claude Code settings file, as its settings and its raw
    ``sandbox`` value, either ``None`` when absent. ``$schema`` is ignored, and every
    other key outside the supported subset is an error."""
    table = dict(_table(value, "the settings file", ""))
    table.pop("$schema", None)
    sandbox = table.pop("sandbox", None)
    return (parse_settings(table) if table else None), sandbox


def _merged_hooks(base: object, over: object) -> object:
    result = {k: list(v) for k, v in cast(dict[str, list[object]], base).items()}
    for event, groups in cast(dict[str, list[object]], over).items():
        found = result.setdefault(event, [])
        found.extend(g for g in groups if g not in found)
    return result


def _merged(name: str, base: object, over: object) -> object:
    if name == "hooks":
        return _merged_hooks(base, over)
    combined = dict(cast(dict[str, object], base))
    for key, value in cast(dict[str, object], over).items():
        if name == "permissions" and key in combined:
            united = {*cast(list[str], combined[key]), *cast(list[str], value)}
            combined[key] = sorted(united)
        else:
            combined[key] = value
    return combined


def merge(layers: Sequence[SettingsConfig | None]) -> SettingsConfig | None:
    """The layers merged from least to most specific, or ``None`` if none is set.

    Rules are united and hooks appended, as Claude Code merges its scopes, so no layer
    removes another's. A variable or attribution text in a more specific layer
    replaces the less specific one.
    """
    present = [layer for layer in layers if layer is not None]
    if not present:
        return None
    document: dict[str, object] = {}
    for layer in present:
        for name, value in layer.document().items():
            document[name] = (
                _merged(name, document[name], value) if name in document else value
            )
    return SettingsConfig(canonical(document))


def protected_rules(protected: Sequence[Path]) -> list[str]:
    """Deny rules that keep the built-in file tools out of ``protected``.

    Claude Code reads a rule path that starts with ``//`` as absolute. Each directory
    is given as written and as its real path, since a rule matches the path a tool is
    handed and a symbolic link such as macOS's ``/tmp`` can stand in for either.
    """
    return _rules(protected, ("Read", "Edit"))


def _rules(directories: Sequence[Path], tools: Sequence[str]) -> list[str]:
    rules: set[str] = set()
    for directory in directories:
        for path in {os.path.normpath(directory), os.path.realpath(directory)}:
            if not path.startswith("/"):
                _fail(f"the protected directory {path!r} is not absolute")
            for tool in tools:
                rules.add(f"{tool}(/{path}/**)")
    return sorted(rules)


def effective(
    settings: SettingsConfig | None,
    protected: Sequence[Path],
    write_protected: Sequence[Path] = (),
) -> dict[str, object] | None:
    """The settings to hand the engine for a line whose merged document is
    ``settings``: ``ask`` rules become ``deny`` rules, ``protected`` is denied to the
    built-in file tools, and ``write_protected``, such as a working directory's
    ``.claude``, to the tools that write files. ``None`` when there is nothing to hand
    over."""
    document = {} if settings is None else settings.document()
    permissions = cast(dict[str, list[str]], document.pop("permissions", {}))
    deny = {
        *permissions.get("deny", []),
        *permissions.get("ask", []),
        *protected_rules(protected),
        *_rules(write_protected, ("Edit",)),
    }
    handed: dict[str, object] = {}
    if permissions.get("allow"):
        handed["allow"] = permissions["allow"]
    if deny:
        handed["deny"] = sorted(deny)
    if handed:
        document["permissions"] = handed
    return document or None
