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
Every enabled sandbox also denies writing the hooks and configuration of the git
checkout its line works in, which ``dromon.workdir`` finds, because git would run a
planted hook outside the sandbox in the operator's own commit or push.

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
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
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

    Relative paths are resolved against ``root``, the line's working directory, here,
    because their meaning in Claude Code depends on the file that sets them, and Dromon
    sets none.
    """
    if path == "~" or path.startswith("~/") or path.startswith("/"):
        return path
    return os.path.normpath(root / path)


def _with_real_forms(paths: Iterable[str]) -> list[str]:
    """``paths`` sorted, and for each absolute or home-relative one that a symbolic
    link makes differ from its real path, the real path as well. **Round 16, item 5.**
    The sandbox follows a link to the real path before it matches a rule, so an
    ``allowWrite`` entry for a worktree that is a link to another volume granted
    nothing, and a ``denyWrite`` entry for such a path denied nothing. Both forms are
    kept, so a rule that names the link still reads as the operator wrote it. **Failure
    mode.** A link that is changed after the document is built, or created below an
    entry, is not followed here, because the real path is read once, when the line's
    sandbox is built. A path that does not exist resolves as far as it can, which is
    ``os.path.realpath`` without ``strict``. Used for deny lists only. A real form
    in an allow list would let a line that can re-link an allowed directory grant
    itself the target, so ``_grant_real_workdir`` adds the one allow entry that is
    needed, the working directory's, which the operator chose and a line does not
    write."""
    forms: set[str] = set()
    for path in paths:
        forms.add(path)
        expanded = os.path.expanduser(path)
        if expanded.startswith("/"):
            forms.add(os.path.realpath(expanded))
    return sorted(forms)


def folded_path(path: str) -> str:
    """``path`` in Unicode's canonical caseless form, decomposed, folded and decomposed
    again, as the Unicode standard's definition D145 of canonical caseless matching
    gives, so that two spellings a volume treats as one name compare equal.

    **Round 18, addendum 2, item 2.** Folding case alone let a grant spelled in the
    decomposed form of an accented letter, ``e`` and a combining acute accent, pass a
    guard spelled in the composed form, since the two strings differ while a volume
    that ignores the normalisation form opens the same directory for both. Both sides
    of every comparison go through this one function, so neither can be normalised
    and the other not. **Choice.** The canonical form, NFD, and not the compatibility
    form, NFKD, since a volume equates canonical equivalents and not compatibility
    ones, such as a superscript digit and the digit. Case folding itself joins a few
    such pairs, such as a ligature and its letters, which only refuses more.
    **Cost.** As with case, a different directory whose name differs from a guarded
    one only in normalisation form is refused on a volume that keeps the forms apart.
    **Porting note.** A port needs the same Unicode version's tables to fold the same
    names."""
    decomposed = unicodedata.normalize("NFD", path)
    return unicodedata.normalize("NFD", decomposed.casefold())


def _within(path: str, directory: Path) -> bool:
    """Whether ``path``, with a leading ``~`` expanded as Claude Code expands it, lies
    in ``directory``, compared in the form ``folded_path`` gives, for the reasons
    ``_overlaps`` and it give."""
    expanded = os.path.expanduser(path)
    if not expanded.startswith("/"):
        return False
    # Real paths on both sides, so a symbolic link such as macOS's /tmp cannot
    # disguise a protected directory.
    real = folded_path(os.path.realpath(expanded))
    guarded = folded_path(os.path.realpath(directory))
    return os.path.commonpath([real, guarded]) == guarded


GLOB_CHARACTERS: Final = "*?[{"
"""The characters that make a path entry a pattern Claude Code expands."""


def claude_default(sandbox: SandboxConfig | None) -> SandboxConfig:
    """``sandbox`` enabled when no layer says whether it is, and unchanged when one
    does. **Round 18 follow-up, item 1.** A Claude Code line with no sandbox document,
    or with one that leaves ``enabled`` unset, ran its shell unconfined, so every
    credential path the file tools were denied was open to a command. Default deny. The
    enabling layer is merged last, as the read-only mode merges its own, and it sets
    only ``enabled``, so an explicit ``enabled`` false in any layer stays the
    operator's choice, which ``explicitly_disabled`` names for a warning. **Effect.**
    Such a line's shell now writes only where the sandbox allows, its working directory
    by Claude Code's default, and reaches no host, as ``network.strictAllowlist`` is
    filled in with no domain allowed."""
    if sandbox is not None and "enabled" in sandbox.document():
        return sandbox
    enabling = SandboxConfig(canonical({"enabled": True}))
    return (
        enabling if sandbox is None else cast(SandboxConfig, merge([sandbox, enabling]))
    )


def explicitly_disabled(sandbox: SandboxConfig | None) -> bool:
    """Whether a layer of ``sandbox`` set ``enabled`` false, which leaves the line's
    shell able to read every path the sandbox would deny."""
    return sandbox is not None and sandbox.document().get("enabled") is False


def _grant_base(path: str) -> str:
    """The directory below which an allow entry can match, with a leading ``~``
    expanded. A plain path is its own base. A pattern's base is the directory of its
    literal prefix, the text before its first ``GLOB_CHARACTERS``, so ``~/.s*`` has the
    home as its base, since it can match any entry the home holds."""
    expanded = os.path.expanduser(path)
    cut = next(
        (
            index
            for index, character in enumerate(expanded)
            if character in GLOB_CHARACTERS
        ),
        None,
    )
    if cut is None:
        return expanded
    head = expanded[:cut]
    return head[: head.rfind("/") + 1] or "/"


def _overlaps(base: str, guarded: Path) -> bool:
    """Whether ``base`` is ``guarded``, an ancestor of it or inside it, compared by
    real path on both sides and in the folded form ``folded_path`` gives.

    **Round 18, last pass, item 1.** A grant spelled in another case, such as
    ``.SSH`` for ``.ssh``, names the same directory on a volume that ignores case,
    and ``realpath`` keeps the case it was given, so the comparison as text let it
    through. The comparison is folded always, rather than only where the volume is
    found to ignore case, since that is a property of each directory and not of the
    platform, a volume can be mounted from elsewhere, and a test that asks the volume
    would differ from host to host. **Cost.** On a volume that keeps case apart, a
    grant of a different directory whose name differs from a listed path only in case
    is refused too, which an operator can rename around."""
    if not base.startswith("/"):
        return False
    real = folded_path(os.path.realpath(base))
    guard = folded_path(os.path.realpath(guarded))
    return os.path.commonpath([real, guard]) in (real, guard)


def _refuse_ungrantable(
    filesystem: Mapping[str, object], ungrantable: Sequence[Path]
) -> None:
    """Refuse an ``allowRead`` or ``allowWrite`` entry that could lift a denial of a
    path in ``ungrantable``. **Round 18 follow-up, item 3.** **Fact, recorded in
    ``docs/protocol/SANDBOX.md`` from a live probe of 2026-11-01.** An ``allowRead``
    entry lifts a ``denyRead`` above it on Claude Code, and its bundle expands an
    ``allowRead`` pattern into the paths it matches. A grant inside a listed path was
    already refused. A grant of the path itself or of an ancestor, such as the home, and
    a pattern whose literal base is any of those, are now refused too, rather than
    relying on a deny winning, which was observed only for ``allowWrite``. **Residual.**
    A pattern whose base is unrelated cannot match a listed path, since a pattern does
    not climb with ``..``, which ``_resolve`` leaves in no absolute entry."""
    for name in ("allowRead", "allowWrite"):
        for path in cast(list[str], filesystem.get(name, [])):
            base = _grant_base(path)
            for guarded in ungrantable:
                if _overlaps(base, guarded):
                    _fail(
                        f"sandbox.filesystem.{name} entry {path!r} could grant"
                        f" {guarded}, a credential path Dromon denies, since it names"
                        " that path, a directory holding it, a path inside it or a"
                        " pattern that could match it",
                        f"filesystem.{name}",
                    )


def effective(
    sandbox: SandboxConfig | None,
    root: Path,
    protected: Sequence[Path],
    guarded_writes: Sequence[Path] = (),
    ungrantable: Sequence[Path] = (),
) -> dict[str, object] | None:
    """The settings to hand the engine for a line whose merged document is
    ``sandbox``, with relative paths resolved against ``root``, the line's working
    directory, the unattended defaults filled in, ``protected`` denied, and
    ``guarded_writes``, such as a checkout's git hooks, denied to writes. ``None``
    when nothing is set. ``ungrantable`` are paths, such as the credential paths, that
    ``protected`` should also hold, and that no allow entry may name, hold or match.

    Raises ``SandboxError`` when an allowed Unix socket lies in a protected directory,
    when an enabled document grants a protected or ungrantable path, and when an
    enabled document sets ``excludedCommands``.
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
                    resolved = (_resolve(p, root) for p in paths)
                    # Review of round 16, finding 1. Only a deny entry gains its real
                    # form. An allow entry keeps the form the operator wrote, because
                    # a line that can write a link where an allow entry lives, such as
                    # its reports directory, would otherwise grant itself the target
                    # at its next engine build.
                    table[name] = (
                        _with_real_forms(resolved)
                        if name in ("denyWrite", "denyRead")
                        else sorted(set(resolved))
                    )
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
    # Round 18 follow-up, item 2. A command named here runs outside the sandbox and
    # reaches every path it denies, so an enabled document may not name one. Refused
    # rather than dropped, so the operator learns that the command will be confined.
    if document.get("excludedCommands"):
        _fail(
            "sandbox.excludedCommands run outside the sandbox, where every credential"
            " and protected path is readable, so Dromon refuses them. Remove them, or"
            " grant what the command needs inside the sandbox",
            "excludedCommands",
        )
    document.setdefault("failIfUnavailable", True)
    # Round 18 follow-up, item 2. Forced, not defaulted. A command the model asks to
    # run unsandboxed would read every path the sandbox denies, and nobody is at the
    # prompt that would otherwise approve it. A layer that sets it true is overridden,
    # and configwarnings says so.
    document["allowUnsandboxedCommands"] = False
    network = cast(dict[str, object], document.setdefault("network", {}))
    network.setdefault("strictAllowlist", True)
    filesystem = cast(dict[str, object], document.setdefault("filesystem", {}))
    guarded = [os.path.normpath(directory) for directory in protected]
    for name in ("denyRead", "denyWrite"):
        existing = cast(list[str], filesystem.get(name, []))
        filesystem[name] = _with_real_forms({*existing, *guarded})
    _grant_real_workdir(filesystem, root)
    # Denied even where an allowWrite entry grants the directory around them. A deny
    # entry winning over an allow entry was verified live on 2026-10-26, as
    # docs/protocol/SANDBOX.md records.
    written = cast(list[str], filesystem["denyWrite"])
    filesystem["denyWrite"] = _with_real_forms(
        {*written, *(os.path.normpath(path) for path in guarded_writes)}
    )
    for name in ("allowRead", "allowWrite"):
        for path in cast(list[str], filesystem.get(name, [])):
            for directory in protected:
                if _within(path, directory):
                    _fail(
                        f"sandbox.filesystem.{name} entry {path!r} is inside a"
                        " directory Dromon protects"
                    )
    _refuse_ungrantable(filesystem, ungrantable)
    return document


def _grant_real_workdir(filesystem: dict[str, object], root: Path) -> None:
    """Write access to the real path of the line's working directory when that
    directory is a symbolic link, **Round 16, item 5.** The engine grants the working
    directory by the path it was given, and the sandbox then follows a link to another
    volume and finds the grant does not cover the real path, so the line could not
    write its own worktree. A working directory that is not a link adds nothing, and a
    ``denyWrite`` entry still wins over this one, as it does over any ``allowWrite``."""
    real = os.path.realpath(root)
    if real == os.path.normpath(root):
        return
    granted = cast(list[str], filesystem.get("allowWrite", []))
    if real not in granted:
        filesystem["allowWrite"] = sorted({*granted, real})


LINUX_SANDBOX_TOOLS: Final = ("bwrap", "socat")
"""The commands Claude Code's sandbox needs on Linux, found on the engine's ``PATH``.
**Fact, read from the bundled Claude Code 2.1.327 on 2026-11-18.** Its dependency
check reports ``bubblewrap (bwrap) not installed`` and ``socat not installed`` when
either is missing there, and with ``failIfUnavailable`` true it then refuses to run a
command. Its seatbelt sandbox is built into the other supported platform, and needs
nothing installed. A missing seccomp filter only weakens the sandbox and is not
checked."""


def host_unavailable_reason(
    system: str, which: Callable[[str], str | None]
) -> str | None:
    """Why Claude Code's sandbox cannot start on a host whose ``platform.system()``
    is ``system`` and whose commands ``which`` finds, or ``None`` when nothing is
    known to be missing. **Round 18, last pass, item 7.** A Claude Code line that no
    layer enables or disables now gets an enabled sandbox with ``failIfUnavailable``
    true, so on such a host its first turn fails, which a start check can say before
    any turn. **Assumption.** The engine runs with the daemon's ``PATH``, as the
    launcher passes it. **Residual.** Only missing commands are found. A host whose
    kernel refuses the user namespaces bubblewrap needs, as some containers do, passes
    this check and still fails at the first turn, since finding that would start a
    sandbox."""
    if system != "Linux":
        return None
    missing = [name for name in LINUX_SANDBOX_TOOLS if which(name) is None]
    if not missing:
        return None
    return (
        "Claude Code's sandbox cannot start on this host, since "
        + " and ".join(missing)
        + (" is" if len(missing) == 1 else " are")
        + " not on the daemon's PATH"
    )


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
