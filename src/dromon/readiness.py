"""What a successor reports when it says a role is ready: the worktree's ``HEAD`` and
its dirty paths, read together.

**Round 14 task of 2026-11-01.** A successor seat reported a role ready before
ownership had passed to it, and named a commit without the uncommitted work beside
it. ``report_role_ready`` is refused until the caller owns the role, and it answers
with both facts read by the daemon in one call, so a report cannot carry one and
leave the other out.

The paths come from ``git status --porcelain=v1 -z``, run with optional locks off so
the read never takes the index lock a writer may need, and with the caller's
``GIT_`` variables removed, as ``dromon.codetree`` runs git. **Trust.** Paths are
the repository's own names and may hold any byte git allows, so they are shown as
git prints them, bounded in count and length, and never interpreted.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Final

GIT_STATUS_SECONDS: Final = 20.0
"""How long ``git status`` may run before the report is refused."""

MAX_READY_PATHS: Final = 100
"""The most dirty paths a report names. The rest are counted."""

MAX_READY_PATH_CHARACTERS: Final = 300
"""The longest path a report names whole. A longer one is cut and marked."""


class ReadinessUnknown(RuntimeError):
    """The worktree's state could not be read, so no readiness is reported."""


@dataclass(frozen=True, slots=True)
class Dirty:
    """The worktree's uncommitted state, as ``git status`` gives it."""

    paths: tuple[str, ...]
    """Each changed or untracked path with its two status letters, such as
    `` M src/x.py`` or ``?? notes.md``, at most ``MAX_READY_PATHS`` of them."""
    total: int
    """How many paths are dirty in all."""


def parse_porcelain(output: str) -> Dirty:
    """The entries of ``git status --porcelain=v1 -z`` output. A rename or a copy
    is followed by its original path, which is folded into its entry. Raises
    ``ReadinessUnknown`` for output that is not in that form."""
    fields = output.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    shown: list[str] = []
    total = 0
    index = 0
    # Bounded by the number of fields, since each pass consumes one or two.
    while index < len(fields):
        field = fields[index]
        index += 1
        if len(field) < 4 or field[2] != " ":
            raise ReadinessUnknown(f"git status gave an entry it should not: {field!r}")
        status, path = field[:2], field[3:]
        if status[0] in "RC" or status[1] in "RC":
            if index >= len(fields):
                raise ReadinessUnknown("git status gave a rename with no origin")
            path = f"{fields[index]} -> {path}"
            index += 1
        total += 1
        if len(shown) < MAX_READY_PATHS:
            if len(path) > MAX_READY_PATH_CHARACTERS:
                path = path[:MAX_READY_PATH_CHARACTERS] + "..."
            shown.append(f"{status} {path}")
    return Dirty(tuple(shown), total)


def dirty_paths(worktree: Path) -> Dirty:
    """The dirty paths of ``worktree``. Raises ``ReadinessUnknown`` when git cannot
    start, runs too long or fails, since a report must not claim a clean tree it did
    not read. Blocking, so the scheduler runs it off its event loop."""
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        done = subprocess.run(  # noqa: S603
            [  # noqa: S607
                "git",
                "-C",
                str(worktree),
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=normal",
            ],
            capture_output=True,
            text=True,
            timeout=GIT_STATUS_SECONDS,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise ReadinessUnknown(
            f"git status ran past {GIT_STATUS_SECONDS:g} seconds"
        ) from error
    except OSError as error:
        raise ReadinessUnknown(f"git could not start: {error}") from error
    if done.returncode != 0:
        raise ReadinessUnknown(f"git status failed with exit code {done.returncode}")
    return parse_porcelain(done.stdout)
