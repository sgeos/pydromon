"""Sandboxing of a line's shell commands, in Claude Code's own vocabulary.

A sandbox document is a Claude Code ``sandbox`` settings object, such as
``{"enabled": true, "network": {"allowedDomains": ["github.com"]}}``, with Claude
Code's key names so that an operator can paste one from ``.claude/settings.json``.
Claude Code enforces it on Bash, PowerShell and Monitor commands with Seatbelt on macOS
and bubblewrap on Linux. Dromon validates it strictly, merges the project's document
with the line's, and hands the result to the engine. ``docs/protocol/SANDBOX.md`` is
normative.

Layers. The project file's ``[sandbox]`` table applies to every line. A profile's and a
line's ``sandbox`` tables, or a document given through the client, apply to one line.
Layers merge as Claude Code merges settings scopes: a scalar in the more specific layer
wins, and arrays are united, so no layer removes another layer's entries.

Unattended defaults. Claude Code asks a person before running a command outside the
sandbox, and before reaching a host outside the allowlist, and runs unsandboxed when
the sandbox cannot start. A Dromon line has nobody at the prompt, so when a document
enables the sandbox and leaves these unset, Dromon fills in the secure answers,
``failIfUnavailable``, ``network.strictAllowlist`` and no ``allowUnsandboxedCommands``.

Protected paths. Every enabled sandbox denies reading and writing Dromon's state and
runtime directories, which hold the event log, the project file and the daemon's
socket. A command that could write them could grant its own line tools at the next
start, and one that could reach the socket could act with operator authority. This
mirrors Claude Code's protection of its own settings files, and no layer can lift it.

Trust. The project file is like a repository's ``.claude/settings.json``, so keys that
Claude Code accepts only from user or managed settings, because they remove a boundary,
are refused there and accepted only through the client, which has operator authority.
Keys Claude Code reserves for managed settings are refused everywhere, since Dromon has
no managed layer. Masked credentials and custom tool binaries are not yet supported.

Porting note. Documents travel as canonical JSON, which the Rust port can carry
unchanged. Path resolution uses ``os.path``, which matches POSIX semantics only.
"""

import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn, cast

MAX_ENTRIES: Final = 256
MAX_TEXT: Final = 4096

SHELL_TOOLS: Final = frozenset({"Bash", "PowerShell", "Monitor"})
"""The tools whose commands Claude Code runs inside the sandbox."""

DOMAIN_PATTERN: Final = re.compile(
    r"(\*\.)?[a-z0-9]([a-z0-9-]{0,62})(\.[a-z0-9]([a-z0-9-]{0,62}))*"
)
MACH_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]{1,255}\*?")
COMMAND_PATTERN: Final = re.compile(r"[^\x00-\x1f]{1,256}")


class SandboxError(ValueError):
    """A sandbox document is malformed, or sets a key its source may not set.

    ``key`` is the dotted path of the offending key within the document, such as
    ``network.allowedDomains``, or empty for the document itself.
    """

    def __init__(self, message: str, key: str = "") -> None:
        super().__init__(message)
        self.key = key


def _fail(message: str, key: str = "") -> NoReturn:
    raise SandboxError(message, key)


# The schema. Each key names a checker, and whether only the operator may set it.

type Checker = Callable[[object, str, bool], object]


def _boolean(value: object, what: str, operator: bool) -> object:
    del operator
    if type(value) is not bool:
        _fail(f"{what} must be a boolean")
    return value


def _port(value: object, what: str, operator: bool) -> object:
    del operator
    if type(value) is not int or not 1 <= value <= 65535:
        _fail(f"{what} must be a port number in [1, 65535]")
    return value


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TEXT:
        _fail(f"{what} must be text of 1 to {MAX_TEXT} characters")
    if "\x00" in value:
        _fail(f"{what} must not contain a NUL character")
    return value


def _list(value: object, what: str) -> list[object]:
    if not isinstance(value, list):
        _fail(f"{what} must be an array")
    items = cast(list[object], value)
    if len(items) > MAX_ENTRIES:
        _fail(f"{what} has more than {MAX_ENTRIES} entries")
    return items


def _texts_matching(pattern: re.Pattern[str], kind: str) -> Checker:
    def check(value: object, what: str, operator: bool) -> object:
        del operator
        found: set[str] = set()
        for item in _list(value, what):
            text = _text(item, what)
            if pattern.fullmatch(text) is None:
                _fail(f"{what} entry {text!r} is not a valid {kind}")
            found.add(text)
        return sorted(found)

    return check


def _paths(value: object, what: str, operator: bool) -> object:
    del operator
    return sorted({_text(item, what) for item in _list(value, what)})


def _violations(value: object, what: str, operator: bool) -> object:
    del operator
    if not isinstance(value, dict):
        _fail(f"{what} must be a table of command pattern to strings")
    table = cast(dict[str, object], value)
    if len(table) > MAX_ENTRIES:
        _fail(f"{what} has more than {MAX_ENTRIES} entries")
    return {
        _text(pattern, what): sorted({_text(s, what) for s in _list(texts, what)})
        for pattern, texts in table.items()
    }


def _credentials_of(field: str) -> Checker:
    def check(value: object, what: str, operator: bool) -> object:
        del operator
        entries: dict[str, dict[str, object]] = {}
        for item in _list(value, what):
            if not isinstance(item, dict):
                _fail(f"{what} entries must be tables")
            entry = cast(dict[str, object], item)
            if set(entry) != {field, "mode"}:
                _fail(f"{what} entries must have exactly {field} and mode")
            if entry["mode"] != "deny":
                _fail(f"{what} mode must be deny. Masking is not yet supported")
            name = _text(entry[field], f"{what} {field}")
            entries[name] = {field: name, "mode": "deny"}
        return [entries[name] for name in sorted(entries)]

    return check


@dataclass(frozen=True, slots=True)
class Key:
    check: Checker | None
    """``None`` for a table, whose keys are in ``TABLES``."""
    operator_only: bool = False


BOOLEAN: Final = Key(_boolean)
OPERATOR_BOOLEAN: Final = Key(_boolean, operator_only=True)
TABLE: Final = Key(None)

TABLES: Final[Mapping[str, Mapping[str, Key]]] = {
    "": {
        "enabled": BOOLEAN,
        "failIfUnavailable": BOOLEAN,
        "allowUnsandboxedCommands": BOOLEAN,
        "autoAllowBashIfSandboxed": BOOLEAN,
        "excludedCommands": Key(_texts_matching(COMMAND_PATTERN, "command")),
        "enableWeakerNestedSandbox": BOOLEAN,
        "enableWeakerNetworkIsolation": BOOLEAN,
        "ignoreViolations": Key(_violations),
        "allowNestedUserNamespaces": OPERATOR_BOOLEAN,
        "allowAppleEvents": OPERATOR_BOOLEAN,
        "filesystem": TABLE,
        "network": TABLE,
        "credentials": TABLE,
    },
    "filesystem": {
        "allowWrite": Key(_paths),
        "denyWrite": Key(_paths),
        "denyRead": Key(_paths),
        "allowRead": Key(_paths),
        "disabled": OPERATOR_BOOLEAN,
    },
    "network": {
        "allowedDomains": Key(_texts_matching(DOMAIN_PATTERN, "domain")),
        "deniedDomains": Key(_texts_matching(DOMAIN_PATTERN, "domain")),
        "strictAllowlist": BOOLEAN,
        "allowUnixSockets": Key(_paths),
        "allowAllUnixSockets": OPERATOR_BOOLEAN,
        "allowLocalBinding": BOOLEAN,
        "allowMachLookup": Key(_texts_matching(MACH_PATTERN, "service name")),
        "httpProxyPort": Key(_port),
        "socksProxyPort": Key(_port),
        "tlsTerminate": OPERATOR_BOOLEAN,
    },
    "credentials": {
        "files": Key(_credentials_of("path")),
        "envVars": Key(_credentials_of("name")),
    },
}

UNSUPPORTED: Final[Mapping[str, str]] = {
    "bwrapPath": "Claude Code accepts it only from managed settings",
    "socatPath": "Claude Code accepts it only from managed settings",
    "ripgrep": "it names a binary to run, which Dromon does not yet support",
    "filesystem.allowManagedReadPathsOnly": "it applies only to managed settings",
    "network.allowManagedDomainsOnly": "it applies only to managed settings",
    "credentials.awsPairs": "credential re-signing is not yet supported",
    "credentials.sigv4": "credential re-signing is not yet supported",
    "credentials.allowPlaintextInject": "credential masking is not yet supported",
}


def _table(value: object, path: str, operator: bool) -> dict[str, object]:
    what = f"sandbox.{path}" if path else "sandbox"
    if not isinstance(value, dict):
        _fail(f"{what} must be a table")
    document = cast(dict[str, object], value)
    keys = TABLES[path]
    checked: dict[str, object] = {}
    for name, item in sorted(document.items()):
        dotted = f"{path}.{name}" if path else name
        if dotted in UNSUPPORTED:
            _fail(
                f"sandbox.{dotted} is not supported, because {UNSUPPORTED[dotted]}",
                dotted,
            )
        key = keys.get(name)
        if key is None:
            _fail(
                f"sandbox.{dotted} is not a known key. Known keys are {sorted(keys)}",
                dotted,
            )
        if key.operator_only and not operator:
            _fail(
                f"sandbox.{dotted} may be set only through the client, as Claude"
                " Code accepts it only from user or managed settings",
                dotted,
            )
        if key.check is None:
            checked[name] = _table(item, dotted, operator)
            continue
        try:
            checked[name] = key.check(item, f"sandbox.{dotted}", operator)
        except SandboxError as error:
            raise SandboxError(str(error), dotted) from error
    return checked


def canonical(document: Mapping[str, object]) -> str:
    return json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    )


@dataclass(frozen=True, slots=True)
class SandboxConfig:
    """A validated sandbox document, held as canonical JSON so it compares and hashes.

    Construct one with ``parse_sandbox``, never directly.
    """

    text: str

    def document(self) -> dict[str, object]:
        return cast(dict[str, object], json.loads(self.text))

    @property
    def enabled(self) -> bool:
        return self.document().get("enabled") is True


def parse_sandbox(value: object, *, operator: bool) -> SandboxConfig:
    """Validate ``value`` as a sandbox document from a source with or without
    operator authority, or raise ``SandboxError``."""
    return SandboxConfig(canonical(_table(value, "", operator)))


def sandbox_from_settings(value: object) -> SandboxConfig:
    """The sandbox of a client-supplied file. A file with a top-level ``sandbox`` key,
    such as a Claude Code settings file, contributes only that key."""
    document: object = value
    if isinstance(value, dict):
        table = cast(dict[str, object], value)
        if "sandbox" in table:
            document = table["sandbox"]
    return parse_sandbox(document, operator=True)


def _merged(base: object, over: object) -> object:
    """``over`` merged onto ``base``: tables recursively, arrays united, scalars
    replaced. Both are validated documents of the same shape."""
    if isinstance(base, dict) and isinstance(over, dict):
        result = dict(cast(dict[str, object], base))
        for name, value in cast(dict[str, object], over).items():
            result[name] = _merged(result[name], value) if name in result else value
        return result
    if isinstance(base, list) and isinstance(over, list):
        united: dict[str, object] = {}
        for item in [*cast(list[object], base), *cast(list[object], over)]:
            united[json.dumps(item, sort_keys=True)] = item
        return [united[key] for key in sorted(united)]
    return over


def merge(layers: Sequence[SandboxConfig | None]) -> SandboxConfig | None:
    """The layers merged from least to most specific, or ``None`` if none is set."""
    present = [layer for layer in layers if layer is not None]
    if not present:
        return None
    document: object = {}
    for layer in present:
        document = _merged(document, layer.document())
    return SandboxConfig(canonical(cast(dict[str, object], document)))


def _resolve(path: str, root: Path) -> str:
    """An absolute path, or a home-relative one that Claude Code expands.

    Relative paths are resolved against the project root here, because their meaning
    in Claude Code depends on the file that sets them, and Dromon sets none.
    """
    if path == "~" or path.startswith("~/") or path.startswith("/"):
        return path
    return os.path.normpath(root / path)


def _within(path: str, directory: Path) -> bool:
    """Whether ``path``, with a leading ``~`` expanded as Claude Code expands it, lies
    in ``directory``."""
    expanded = os.path.expanduser(path)
    if not expanded.startswith("/"):
        return False
    # Real paths on both sides, so a symbolic link such as macOS's /tmp cannot
    # disguise a protected directory.
    real, guarded = os.path.realpath(expanded), os.path.realpath(directory)
    return os.path.commonpath([real, guarded]) == guarded


def effective(
    sandbox: SandboxConfig | None, root: Path, protected: Sequence[Path]
) -> dict[str, object] | None:
    """The settings to hand the engine for a line whose merged document is
    ``sandbox``, with relative paths resolved against ``root``, the unattended
    defaults filled in and ``protected`` denied. ``None`` when nothing is set.

    Raises ``SandboxError`` when an allowed Unix socket lies in a protected directory.
    """
    if sandbox is None:
        return None
    document = sandbox.document()
    for table_name in ("filesystem", "network"):
        table = document.get(table_name)
        if isinstance(table, dict):
            table = cast(dict[str, object], table)
            for name in ("allowWrite", "denyWrite", "denyRead", "allowRead"):
                if name in table:
                    paths = cast(list[str], table[name])
                    table[name] = sorted({_resolve(p, root) for p in paths})
            if "allowUnixSockets" in table:
                sockets = sorted(
                    {
                        _resolve(p, root)
                        for p in cast(list[str], table["allowUnixSockets"])
                    }
                )
                for socket in sockets:
                    for directory in protected:
                        if _within(socket, directory):
                            _fail(
                                f"sandbox.network.allowUnixSockets entry {socket!r}"
                                " is inside a directory Dromon protects"
                            )
                table["allowUnixSockets"] = sockets
    if document.get("enabled") is not True:
        return document
    document.setdefault("failIfUnavailable", True)
    document.setdefault("allowUnsandboxedCommands", False)
    network = cast(dict[str, object], document.setdefault("network", {}))
    network.setdefault("strictAllowlist", True)
    filesystem = cast(dict[str, object], document.setdefault("filesystem", {}))
    guarded = [os.path.normpath(directory) for directory in protected]
    for name in ("denyRead", "denyWrite"):
        existing = cast(list[str], filesystem.get(name, []))
        filesystem[name] = sorted({*existing, *guarded})
    for name in ("allowRead", "allowWrite"):
        for path in cast(list[str], filesystem.get(name, [])):
            for directory in protected:
                if _within(path, directory):
                    _fail(
                        f"sandbox.filesystem.{name} entry {path!r} is inside a"
                        " directory Dromon protects"
                    )
    return document


def blocks_unsandboxed(
    settings: Mapping[str, object] | None,
    tool_name: str,
    tool_input: Mapping[str, object],
) -> bool:
    """Whether a shell call asks to leave an enabled sandbox that does not allow it."""
    if settings is None or settings.get("enabled") is not True:
        return False
    if tool_name not in SHELL_TOOLS:
        return False
    if tool_input.get("dangerouslyDisableSandbox") is not True:
        return False
    return settings.get("allowUnsandboxedCommands") is not True
