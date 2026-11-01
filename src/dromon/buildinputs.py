"""A worktree's build inputs against the project's base, which ``dromon
sync-worktrees`` shows before it syncs. Round 14, from the audit's finding S1.

``uv sync`` installs the project itself, so it runs the build backend that the
worktree's ``pyproject.toml`` names, from the paths that file lists in
``backend-path``, and it installs whatever ``uv.lock`` records, from any index, URL,
git or local source the lock names, building each local one with its own backend. A
line can write all of them in its role's worktree. A sync therefore runs code a line
wrote, as the operator, outside any sandbox and with the network. This module finds
what differs from the base, so the operator sees it and decides before the sync, as
``dromon land`` already shows its plan and asks.

**Round 14, re-review of 522c146, S1b.** The inputs were ``pyproject.toml``,
``uv.lock`` and the backend paths alone, so an edited backend of a dependency taken
from a local path ran in a sync with no difference shown. The inputs are now every
file uv reads for the project's configuration in the worktree, ``pyproject.toml``,
``uv.lock``, ``uv.toml``, ``.python-version`` and ``.python-versions``, and every local
source directory ``pyproject.toml`` or ``uv.lock`` names. Where the files a build
reads cannot be bounded, the whole worktree is compared instead, with the reason. That
is so for a project file uv might read where Python's reader cannot, a workspace, a
build backend loaded from the worktree, a backend other than uv's own that builds
the project, and an index, link, cache, interpreter mirror or source in a local
place.

Trust. Everything read here is written by a line and is untrusted. The diff is only
printed, never interpreted, and git runs without a shell, without external diff or
text conversion programs and without the file system monitor, which a repository's
configuration could otherwise name as a command to run.

Residual, stated. A file git ignores inside a compared directory is not listed, since
listing ignored files would list every virtual environment and cache. A build backend
installed from an index through ``build-system.requires`` is shown only as the change
to ``pyproject.toml`` that names it. Configuration uv reads from outside the worktree,
such as the operator's own ``uv.toml``, the environment, and a parent directory's
project, which for a worktree under ``tmp/worktrees`` is the operator's checkout, is
not compared. The worktree's virtual environment is ignored by git and not compared,
though uv runs its interpreter. **Hypothesis, not tested.** A line that writes
``.venv`` could have a sync run code of its own that way. Porting note: ``tomllib`` is
Python's own reader, and a port needs a TOML reader that also rejects duplicate
keys."""

import posixpath
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from dromon import land

BASE: Final = "origin/main"
"""What a worktree's build inputs are compared with, the remote base that every
branch merges into. A worktree whose inputs equal it needs no question."""

PROJECT_FILE: Final = "pyproject.toml"
UV_CONFIG: Final = "uv.toml"

CONFIG_FILES: Final = (
    PROJECT_FILE,
    land.LOCK_FILE,
    UV_CONFIG,
    ".python-version",
    ".python-versions",
)
"""The files uv reads in the project's directory for its configuration and the
interpreter it chooses, compared whether or not they exist."""

WHOLE_TREE: Final = "."
"""The pathspec compared when the build inputs cannot be bounded."""

INERT_BACKENDS: Final = frozenset({"uv_build"})
"""Build backends that run no file of the project when they build it. uv's own
backend reads ``pyproject.toml`` and copies the package's files, and has no build
scripts or plugins. **Inference, from uv's documentation of its build backend.** Any
other backend, setuptools' ``setup.py`` or a hatchling hook for example, may run or
read any file of the project, so the whole worktree is compared."""

LOCATION_KEYS: Final = (
    "find-links",
    "index-url",
    "extra-index-url",
    "index",
    "cache-dir",
    "python-install-mirror",
    "pypy-install-mirror",
    "python-downloads-json-url",
)
"""Settings of ``[tool.uv]`` or ``uv.toml`` that name a place packages, built
wheels or interpreters come from. A local place there is something a line can fill."""

REMOTE_PREFIXES: Final = (
    "https://",
    "http://",
    "ssh://",
    "git://",
    "git+https://",
    "git+ssh://",
)
"""The forms of a location that lies outside the worktree's machine. Anything else,
a path or a ``file://`` address included, is taken as local."""

LOCAL_SOURCE_KEYS: Final = ("editable", "directory", "path", "virtual")
"""The keys of a ``uv.lock`` package's ``source`` that name a local path."""

REMOTE_SOURCE_KEYS: Final = ("registry", "url", "git")
"""The keys of a ``source`` that name a location, which must then be remote."""

MAX_BACKEND_PATHS: Final = 16
"""The most ``backend-path`` entries read. More is refused, so the pathspec is
bounded."""

MAX_LOCAL_SOURCES: Final = 64
"""The most local source directories compared. More is refused."""

MAX_SHOWN_LINES: Final = 400
"""The most diff lines printed for one worktree. The rest is counted, with the git
command that shows it whole."""

MAX_UNTRACKED_SHOWN: Final = 50

_GIT: Final = (
    "git",
    "--no-optional-locks",
    "--literal-pathspecs",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.quotePath=false",
)

Table = dict[str, object]


class BuildInputsUnknown(Exception):
    """The build inputs cannot be compared, with the reason. The worktree is then not
    synced, since what the sync would run is not known."""


@dataclass(frozen=True, slots=True)
class Inputs:
    """What is compared for a worktree."""

    paths: tuple[str, ...]
    """The pathspecs, the configuration files and each local source directory, or
    ``WHOLE_TREE`` alone."""
    unbounded: str | None = None
    """Why the whole worktree is compared, or ``None`` when it is not."""


@dataclass(frozen=True, slots=True)
class Differences:
    """How a worktree's build inputs differ from ``BASE``."""

    paths: tuple[str, ...]
    """The pathspecs compared, as ``Inputs.paths``."""
    diff: str
    """``git diff BASE`` of those paths in the working tree, committed or not."""
    untracked: tuple[str, ...]
    """Files under those paths git does not track and does not ignore."""
    unbounded: str | None = None
    """Why the whole worktree was compared, or ``None``."""

    @property
    def differ(self) -> bool:
        return bool(self.diff.strip()) or bool(self.untracked)


def _table(value: object) -> Table:
    """``value`` as a table, or an empty one when it is not a table."""
    return cast(Table, value) if isinstance(value, dict) else {}


def _read_toml(file: Path, *, required: bool) -> Table | None:
    """The parsed file, an empty table for a missing file that is not ``required``,
    or ``None`` when it cannot be read or parsed."""
    try:
        return tomllib.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None if required else {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None


def _inside(entry: object, what: str) -> str:
    """``entry`` as a normal relative pathspec inside the worktree, or a refusal
    naming ``what``."""
    if not isinstance(entry, str) or not entry or "\x00" in entry:
        raise BuildInputsUnknown(f"{what} {entry!r} is not a path")
    normal = posixpath.normpath(entry.replace("\\", "/"))
    if normal.startswith("/") or normal == ".." or normal.startswith("../"):
        raise BuildInputsUnknown(f"{what} {entry!r} lies outside the worktree")
    return normal


def _backend_paths(system: Table) -> list[str]:
    """The ``backend-path`` entries, each checked to lie inside the worktree."""
    entries = system.get("backend-path", [])
    if not isinstance(entries, list):
        raise BuildInputsUnknown(
            f"{PROJECT_FILE} build-system.backend-path is not a list"
        )
    listed = cast(list[object], entries)
    if len(listed) > MAX_BACKEND_PATHS:
        raise BuildInputsUnknown(
            f"{PROJECT_FILE} lists more than {MAX_BACKEND_PATHS} backend paths"
        )
    return [
        _inside(entry, f"{PROJECT_FILE} build-system.backend-path entry {index}")
        for index, entry in enumerate(listed)
    ]


def _remote(value: object) -> bool:
    return isinstance(value, str) and value.startswith(REMOTE_PREFIXES)


def _local_location(key: str, value: object) -> bool:
    """Whether the setting ``key`` names a local place. A value of a form not known
    counts as local, so it is never passed over."""
    if key == "cache-dir":
        return True
    if key == "index":
        indexes = cast(list[object], value) if isinstance(value, list) else [value]
        return any(not _remote(_table(index).get("url")) for index in indexes)
    values = cast(list[object], value) if isinstance(value, list) else [value]
    return any(not _remote(item) for item in values)


def _source_specs(tool_uv: Table) -> list[tuple[str, Table]]:
    """Each ``[tool.uv.sources]`` entry with its name, a list of tables flattened."""
    found: list[tuple[str, Table]] = []
    for name, value in _table(tool_uv.get("sources")).items():
        specs = cast(list[object], value) if isinstance(value, list) else [value]
        found.extend((name, _table(spec)) for spec in specs)
    return found


def _lock_sources(lock: Table) -> list[tuple[str, Table]]:
    """Each ``uv.lock`` package's name and ``source`` table."""
    packages = lock.get("package", [])
    if not isinstance(packages, list):
        return []
    found: list[tuple[str, Table]] = []
    for package in cast(list[object], packages):
        entry = _table(package)
        found.append((str(entry.get("name", "?")), _table(entry.get("source"))))
    return found


def _unbounded(
    system: Table | None, tool_uv: Table, uv_toml: Table, lock: Table
) -> str | None:
    """Why the files a build reads cannot be bounded, or ``None``."""
    built_system = system or {}
    if built_system.get("backend-path"):
        return (
            "build-system.backend-path loads a build backend from the worktree,"
            " which may read any of its files"
        )
    if "workspace" in tool_uv:
        return "it declares a uv workspace, whose members uv reads and builds"
    package = tool_uv.get("package")
    built = package if isinstance(package, bool) else system is not None
    backend = built_system.get("build-backend")
    if built and backend not in INERT_BACKENDS:
        named = backend if isinstance(backend, str) else "setuptools, uv's default"
        return f"its build backend {named} may run any file of the project"
    for where, table in (("[tool.uv]", tool_uv), (UV_CONFIG, uv_toml)):
        for key in LOCATION_KEYS:
            if key in table and _local_location(key, table[key]):
                return f"{where} sets {key} to a local place"
    for name, spec in _source_specs(tool_uv):
        if "workspace" in spec:
            return f"[tool.uv.sources] takes {name} from a uv workspace"
        for key in ("git", "url"):
            if key in spec and not _remote(spec[key]):
                return f"[tool.uv.sources] takes {name} from a local {key}"
    for name, source in _lock_sources(lock):
        for key in REMOTE_SOURCE_KEYS:
            if key in source and not _remote(source[key]):
                return f"{land.LOCK_FILE} installs {name} from a local {key}"
    return None


def _local_sources(tool_uv: Table, lock: Table) -> list[str]:
    """Every local source path ``[tool.uv.sources]`` or ``uv.lock`` names, other than
    the project itself, each checked to lie inside the worktree."""
    named: list[tuple[str, object]] = [
        (f"[tool.uv.sources] path of {name}", spec["path"])
        for name, spec in _source_specs(tool_uv)
        if "path" in spec
    ]
    named.extend(
        (f"{land.LOCK_FILE} {key} source of {name}", source[key])
        for name, source in _lock_sources(lock)
        for key in LOCAL_SOURCE_KEYS
        if key in source
    )
    found: list[str] = []
    for what, entry in named:
        path = _inside(entry, what)
        if path != WHOLE_TREE and path not in found:
            found.append(path)
    if len(found) > MAX_LOCAL_SOURCES:
        raise BuildInputsUnknown(
            f"more than {MAX_LOCAL_SOURCES} local sources are named"
        )
    return found


def build_inputs(worktree: Path) -> Inputs:
    """What ``uv sync`` reads and builds in ``worktree``, as pathspecs, or the whole
    worktree with the reason when that cannot be bounded. A backend path or a local
    source outside the worktree raises ``BuildInputsUnknown``, since no diff against
    the base shows it."""
    project = _read_toml(worktree / PROJECT_FILE, required=True)
    uv_toml = _read_toml(worktree / UV_CONFIG, required=False)
    lock = _read_toml(worktree / land.LOCK_FILE, required=False)
    if project is None or uv_toml is None or lock is None:
        unread = (
            PROJECT_FILE
            if project is None
            else UV_CONFIG
            if uv_toml is None
            else land.LOCK_FILE
        )
        return Inputs(
            (WHOLE_TREE,),
            f"{unread} cannot be read as TOML here, and uv may read it otherwise",
        )
    raw_system = project.get("build-system")
    system = _table(raw_system) if raw_system is not None else None
    _backend_paths(system or {})
    tool_uv = _table(_table(project.get("tool")).get("uv"))
    reason = _unbounded(system, tool_uv, uv_toml, lock)
    if reason is not None:
        return Inputs((WHOLE_TREE,), reason)
    sources = _local_sources(tool_uv, lock)
    return Inputs((*CONFIG_FILES, *sources))


def _git(run: land.Runner, worktree: Path, *args: str) -> str:
    done = run([*_GIT, *args], worktree)
    if done.code != 0:
        reason = (done.stderr or done.stdout).strip().splitlines()
        raise BuildInputsUnknown(
            f"git {args[0]} against {BASE} failed with status {done.code}:"
            f" {reason[-1] if reason else 'no output'}"
        )
    return done.stdout


def differences(worktree: Path, run: land.Runner = land.run_command) -> Differences:
    """Compare the worktree's build inputs with ``BASE``, in the working tree, so an
    uncommitted change counts, since ``uv sync`` reads the files as they are."""
    inputs = build_inputs(worktree)
    diff = _git(
        run,
        worktree,
        "diff",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        BASE,
        "--",
        *inputs.paths,
    )
    listed = _git(
        run,
        worktree,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        *inputs.paths,
    )
    untracked = tuple(sorted(name for name in listed.split("\x00") if name))
    return Differences(inputs.paths, diff, untracked, inputs.unbounded)


def show(found: Differences, worktree: Path, say: Callable[[str], None]) -> None:
    """Print the differences, bounded, with the command that shows them whole."""
    if found.unbounded is not None:
        say(f"    the whole worktree is compared, since {found.unbounded}")
    lines = found.diff.splitlines()
    for line in lines[:MAX_SHOWN_LINES]:
        say(f"    {line}")
    if len(lines) > MAX_SHOWN_LINES:
        say(
            f"    ... {len(lines) - MAX_SHOWN_LINES} more lines. See them with"
            f" git -C {worktree} diff {BASE} -- {' '.join(found.paths)}"
        )
    for name in found.untracked[:MAX_UNTRACKED_SHOWN]:
        say(f"    untracked: {name}")
    if len(found.untracked) > MAX_UNTRACKED_SHOWN:
        more = len(found.untracked) - MAX_UNTRACKED_SHOWN
        say(f"    ... {more} more untracked files")
