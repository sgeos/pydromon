"""Building a role worktree's environment for ``dromon sync-worktrees`` without
running an interpreter a line wrote. Round 14, final check of 951b9e7, item 1.

**The hole.** ``uv sync`` in a worktree queries the interpreter in the worktree's
``.venv`` before anything else, and a line can write that directory. The audit line
wrote ``.venv/pyvenv.cfg`` and an executable ``.venv/bin/python``, and ``uv sync
--locked`` ran it as the operator, while the build inputs showed no difference.

**The design.** uv never reads the worktree's ``.venv``. Each sync makes a fresh
private directory in the state directory's ``sync-envs``, which no line's sandbox
can write. It creates a relocatable environment there with ``uv venv
--relocatable``, and installs into it with ``uv sync --locked`` while
``UV_PROJECT_ENVIRONMENT`` names it. Only then is the worktree's ``.venv`` renamed
into the private directory and the new environment renamed into its place. The
private directory, with the old environment in it, is removed without following
links, and nothing in the old environment is run or read. The scripts of a
relocatable environment find their interpreter relative to themselves, so it works
once moved.

Facts, observed with uv 0.13.5 on macOS on 2026-11-01. Neither step ran a planted
``.venv/bin/python`` in the worktree, while a plain ``uv sync`` there did. ``uv
sync`` ignored ``UV_VENV_RELOCATABLE`` and made an environment whose scripts named
its absolute path, so the environment is made first. A moved environment ran
``pytest``, and ``uv sync --locked`` in the worktree afterwards installed nothing.

**What it does to the gate's own uv run.** Nothing changes for a line. The gate's
``uv sync --locked`` and its ``uv run`` use the worktree's ``.venv`` as before, and
find it current. A line may change that environment afterwards, and the next sync
moves it aside and removes it unread.

**Link mode.** Packages are copied, not linked. On Linux uv hard links from its
cache by default, and a line that wrote a hard-linked file in its ``.venv`` would
change the operator's cache, which the operator's own environments run. On macOS uv
clones by default, which shares no writable file, so copying there costs only disk,
about 360 megabytes for this project.

**Failure modes.** Each one leaves the worktree unsynced and is reported.
- A step of uv fails or outlives ``SYNC_SECONDS``.
- uv recreates the environment without ``relocatable = true``, as it would for an
  interpreter that does not satisfy the project, which is refused, since its
  scripts would not run once moved.
- A line makes a new ``.venv`` between the two renames, so the second fails and the
  worktree is left without an environment until the next sync.
- The state directory and the worktree lie on different file systems, so a rename
  fails.
- A command running from the old environment during the swap keeps its open files,
  and may fail on a module it had not loaded yet, so sync while no line runs a gate.

**Residuals.** uv still reads the worktree's ``pyproject.toml``, ``uv.lock``,
``uv.toml`` and ``.python-version`` and builds the project, which ``buildinputs``
shows first. A line whose sandbox is disabled can write the state directory too.

**Porting.** ``tempfile.mkdtemp``, ``os.rename`` and a removal that follows no link
map onto ``mkdtemp``, ``rename`` and ``unlinkat`` in Rust.
"""

import os
import shutil
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

from dromon.land import Completed
from dromon.paths import PathError, ensure_private_dir

ENVIRONMENT: Final = ".venv"
"""The environment uv makes in a project, which a line's gate uses."""

PROJECT_ENVIRONMENT: Final = "UV_PROJECT_ENVIRONMENT"

DROPPED: Final = ("VIRTUAL_ENV", "CONDA_PREFIX", PROJECT_ENVIRONMENT)
"""Inherited variables that would point uv at another environment."""

RELOCATABLE: Final = "relocatable = true"
"""The line of ``pyvenv.cfg`` that ``uv venv --relocatable`` writes."""

SYNC_SECONDS: Final = 600.0
"""How long each step of uv may take in one worktree."""

SYNC_ARGV: Final = ("uv", "sync", "--locked", "--link-mode", "copy")

MAX_CONFIG_BYTES: Final = 65536
"""The most of ``pyvenv.cfg`` read, so a fault cannot make the read unbounded."""


class SyncFailed(Exception):
    """A worktree's environment could not be built or put in place. The message
    says why, and the worktree's own environment was not run."""


class Runner(Protocol):
    """``land.run_command``, which also takes the environment of the command."""

    def __call__(
        self,
        argv: Sequence[str],
        cwd: Path,
        stdin: str | None = None,
        *,
        stream: bool = False,
        timeout: float = ...,
        env: Mapping[str, str] | None = None,
    ) -> Completed: ...


@dataclass(frozen=True, slots=True)
class Synced:
    """A sync that put its environment in place. ``left`` names a private
    directory that could not be removed, which a later sync does not reuse."""

    left: str | None = None


def venv_argv(stage: Path) -> tuple[str, ...]:
    return ("uv", "venv", "--relocatable", "--quiet", str(stage))


def environment(stage: Path, inherited: Mapping[str, str]) -> dict[str, str]:
    """The environment of both steps of uv, the operator's own without the
    variables that name another environment, and with ``stage`` as the project
    environment."""
    found = {name: value for name, value in inherited.items() if name not in DROPPED}
    found[PROJECT_ENVIRONMENT] = str(stage)
    return found


def sync(
    worktree: Path,
    base: Path,
    run: Runner,
    inherited: Mapping[str, str] | None = None,
) -> Synced:
    """Build the environment of ``worktree`` in a fresh private directory under
    ``base`` and put it in place of the worktree's ``.venv``, as the module says.
    Raises ``SyncFailed``."""
    if not shutil.rmtree.avoids_symlink_attacks:
        raise SyncFailed(
            "this platform cannot remove an environment without following links"
        )
    try:
        ensure_private_dir(base)
        private = Path(tempfile.mkdtemp(prefix="sync-", dir=base))
    except (PathError, OSError) as error:
        raise SyncFailed(f"no private directory to build in, {error}") from error
    try:
        _build_and_swap(worktree, private, run, inherited or dict(os.environ))
    finally:
        left = _remove(private)
    return Synced(left)


def _build_and_swap(
    worktree: Path, private: Path, run: Runner, inherited: Mapping[str, str]
) -> None:
    stage = private / "env"
    variables = environment(stage, inherited)
    for argv in (venv_argv(stage), SYNC_ARGV):
        done = run(argv, worktree, timeout=SYNC_SECONDS, env=variables)
        if done.code != 0:
            reason = (done.stderr or done.stdout).strip().splitlines()
            raise SyncFailed(
                f"{' '.join(argv[:3])} failed with status {done.code}:"
                f" {reason[-1] if reason else 'no output'}"
            )
    _check_relocatable(stage)
    target = worktree / ENVIRONMENT
    try:
        os.rename(target, private / "old")
    except FileNotFoundError:
        pass
    except OSError as error:
        raise SyncFailed(f"{target} cannot be moved aside: {error}") from error
    try:
        os.rename(stage, target)
    except OSError as error:
        raise SyncFailed(
            f"the new environment cannot be moved to {target}, which may have been"
            f" made again meanwhile, so the worktree has none: {error}"
        ) from error


def _check_relocatable(stage: Path) -> None:
    """Refuse an environment whose ``pyvenv.cfg`` lacks ``relocatable = true``."""
    config = stage / "pyvenv.cfg"
    try:
        found = os.lstat(config)
        if not stat.S_ISREG(found.st_mode):
            raise SyncFailed(f"{config} is not a regular file")
        with config.open("rb") as handle:
            text = handle.read(MAX_CONFIG_BYTES).decode(errors="replace")
    except OSError as error:
        raise SyncFailed(f"{config} cannot be read: {error}") from error
    if RELOCATABLE not in (line.strip() for line in text.splitlines()):
        raise SyncFailed(
            f"uv made {stage} without {RELOCATABLE}, so its scripts would not run"
            " once moved"
        )


def _remove(private: Path) -> str | None:
    """Remove ``private`` without following links. Returns it when anything is
    left, which is reported rather than raised, since the swap is already done."""
    failures: list[str] = []

    def failed(function: object, path: str, error: BaseException) -> None:
        del function
        failures.append(f"{path}: {error}")

    shutil.rmtree(private, onexc=failed)
    return f"{private} was left, {failures[0]}" if failures else None
