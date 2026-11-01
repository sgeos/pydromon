"""The state of a line's worktree, computed by the daemon outside the line's sandbox.

**Round 14, item 7.** ``git status`` run inside a line's sandbox warns about the
protected paths its sandbox denies, so a line cannot tell a clean tree from a scan that
stopped early. The daemon runs outside every engine sandbox, reads those paths, and
says what it could not read as incomplete, never as clean. ``worktree_status`` gives
the commit checked out, the branch, how far the commit is ahead of and behind the
remote's ``main`` as last fetched, and the dirty paths.

Clean, dirty and unknown. ``clean`` is true only when git finished without a word on its
error stream and listed no path. It is false when a path is dirty, whatever else git
said. It is null when git said something and listed no dirty path, which is the case a
sandboxed ``git status`` could not tell from clean. The daemon never fetches, so ahead
and behind describe ``origin/main`` as the last fetch left it, and the answer says so.

Trust. Git runs with the daemon's authority in a directory the line may write, as
``dromon.codetree`` describes for its two read-only commands. Here it also reads the
index and the working files, which ``status`` must. The environment is cleared of
``GIT_`` variables, the system and user configuration are not read, the optional locks
are off so that no index is rewritten, and the file system monitor, which would run a
program, is turned off on the command line. **Assumption and residual.** The
repository's own configuration is still read. A line that can write it could steer git
with a clean or smudge filter that ``.gitattributes`` names, which a sandboxed line
cannot do, since every enabled sandbox denies it that file, as ``dromon.workdir``
describes. A line with no sandbox and write access to its checkout could already run
anything as the daemon's user, so the residual adds no authority to it.

Failure modes. A git that cannot be started, one that runs past its bound, one that
fails, and a directory that is not a checkout all give no status, with the reason, so a
line never reads silence as a clean tree. A missing ``origin/main`` gives no ahead and
behind but still gives the rest. The dirty list is cut at ``MAX_DIRTY`` entries, with
the count of all, and each path is cut at ``MAX_PATH_CHARACTERS``.

Porting note. Each step is a command run with a timeout, which maps onto
``std::process::Command`` in the Rust port and onto an external effect of a bounded
stream processor in Keleusma.
"""

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

GIT_SECONDS: Final = 30.0
"""How long one git command may take. ``status`` reads every file of a large tree."""

MAX_DIRTY: Final = 200
MAX_PATH_CHARACTERS: Final = 300
MAX_INCOMPLETE: Final = 50
MAX_REASON_CHARACTERS: Final = 200
REMOTE_REF: Final = "refs/remotes/origin/main"
REMOTE_NAME: Final = "origin/main"

OBJECT_NAME: Final = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
QUOTED_PATH: Final = re.compile(r"'([^']+)'|\"([^\"]+)\"")


@dataclass(frozen=True, slots=True)
class Ran:
    """What one git command did. ``code`` is 124 for a timeout and 127 for a command
    that could not be started, with the reason in ``stderr``."""

    code: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[Path, Sequence[str]], Ran]


class WorktreeUnavailable(Exception):
    """The worktree's state cannot be given, for the reason in the message."""


def git_environment() -> dict[str, str]:
    """The environment git runs in, which keeps the search path and the home
    directory and nothing that steers git. ``LC_ALL=C`` keeps its messages in the form
    ``incomplete_paths`` reads."""
    kept = {name: os.environ[name] for name in ("PATH", "HOME") if name in os.environ}
    kept.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        }
    )
    return kept


def run_git(directory: Path, arguments: Sequence[str]) -> Ran:
    """The real runner. It never raises for a failed command."""
    command = ["git", "-C", str(directory), "-c", "core.fsmonitor=false", *arguments]
    try:
        done = subprocess.run(  # noqa: S603
            command,
            capture_output=True,
            text=True,
            errors="surrogateescape",
            timeout=GIT_SECONDS,
            check=False,
            env=git_environment(),
        )
    except subprocess.TimeoutExpired:
        return Ran(124, "", f"git did not finish within {GIT_SECONDS:g} seconds")
    except OSError as error:
        return Ran(127, "", f"git cannot be run: {error}")
    return Ran(done.returncode, done.stdout, done.stderr)


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _line_of(run: Ran) -> str:
    said = [line for line in run.stderr.splitlines() if line.strip()]
    return _cut(said[0], MAX_REASON_CHARACTERS) if said else "nothing"


def head_commit(directory: Path, runner: Runner) -> str | None:
    """The commit checked out, or ``None`` for a branch with no commit yet. Raises
    ``WorktreeUnavailable`` when git cannot say, as in a directory that is not a
    checkout."""
    found = runner(directory, ["rev-parse", "--verify", "-q", "HEAD"])
    if found.code == 0:
        name = found.stdout.strip()
        if OBJECT_NAME.fullmatch(name):
            return name
        raise WorktreeUnavailable("git gave a commit name that is not one")
    if found.code == 1 and not found.stderr.strip():
        return None
    raise WorktreeUnavailable(
        f"git rev-parse failed with status {found.code} and said {_line_of(found)}"
    )


def branch_name(directory: Path, runner: Runner) -> str | None:
    """The branch checked out, or ``None`` for a detached ``HEAD``."""
    found = runner(directory, ["symbolic-ref", "-q", "--short", "HEAD"])
    name = found.stdout.strip()
    return name if found.code == 0 and name else None


def against_origin_main(directory: Path, runner: Runner) -> dict[str, object]:
    """How many commits ``HEAD`` is ahead of and behind ``origin/main`` as last
    fetched, or why that is not known. Never raises."""
    note = "origin/main is as the last fetch left it, since the daemon does not fetch"
    exists = runner(directory, ["rev-parse", "--verify", "-q", REMOTE_REF])
    if exists.code != 0:
        return {"unavailable": f"there is no {REMOTE_NAME} in this checkout"}
    counted = runner(
        directory, ["rev-list", "--left-right", "--count", f"HEAD...{REMOTE_REF}"]
    )
    fields = counted.stdout.split()
    if (
        counted.code != 0
        or len(fields) != 2
        or not all(field.isdigit() for field in fields)
    ):
        return {
            "unavailable": (
                f"git rev-list failed with status {counted.code} and said"
                f" {_line_of(counted)}"
            )
        }
    return {
        "ref": REMOTE_NAME,
        "ahead": int(fields[0]),
        "behind": int(fields[1]),
        "note": note,
    }


def dirty_paths(output: str) -> list[dict[str, str]]:
    """The entries of ``git status --porcelain=v1 -z --no-renames``, each with its two
    status letters and its path. A malformed record is skipped."""
    found: list[dict[str, str]] = []
    for record in output.split("\x00"):
        if len(record) < 4 or record[2] != " ":
            continue
        found.append(
            {"status": record[:2], "path": _cut(record[3:], MAX_PATH_CHARACTERS)}
        )
    return found


def incomplete_paths(errors: str) -> list[dict[str, object]]:
    """What git said on its error stream, each line as a reason, with the path it
    quotes where it quotes one. Any line counts, since git says nothing when it read
    everything, and a line that names no path still means the scan was cut short."""
    found: list[dict[str, object]] = []
    for line in errors.splitlines():
        if not line.strip():
            continue
        quoted = QUOTED_PATH.search(line)
        path = None
        if quoted is not None:
            path = _cut(quoted.group(1) or quoted.group(2), MAX_PATH_CHARACTERS)
        found.append(
            {"path": path, "reason": _cut(line.strip(), MAX_REASON_CHARACTERS)}
        )
        if len(found) == MAX_INCOMPLETE:
            break
    return found


def worktree_status(directory: Path, runner: Runner = run_git) -> dict[str, object]:
    """The state of the checkout at ``directory``. Raises ``WorktreeUnavailable`` when
    the commit or the status cannot be read at all."""
    head = head_commit(directory, runner)
    branch = branch_name(directory, runner)
    origin = (
        against_origin_main(directory, runner)
        if head is not None
        else {"unavailable": "the branch has no commit yet"}
    )
    scan = runner(
        directory,
        [
            "status",
            "--porcelain=v1",
            "-z",
            "--no-renames",
            "--untracked-files=normal",
        ],
    )
    if scan.code != 0:
        raise WorktreeUnavailable(
            f"git status failed with status {scan.code} and said {_line_of(scan)}"
        )
    dirty = dirty_paths(scan.stdout)
    incomplete = incomplete_paths(scan.stderr)
    complete = not incomplete
    clean: bool | None = False if dirty else (True if complete else None)
    return {
        "worktree": str(directory),
        "head": head,
        "branch": branch,
        "origin_main": origin,
        "clean": clean,
        "complete": complete,
        "dirty_count": len(dirty),
        "dirty": dirty[:MAX_DIRTY],
        "dirty_truncated": len(dirty) > MAX_DIRTY,
        "incomplete": incomplete,
        "summary": _summary(head, branch, clean, len(dirty), incomplete),
    }


def _summary(
    head: str | None,
    branch: str | None,
    clean: bool | None,
    dirty: int,
    incomplete: Sequence[dict[str, object]],
) -> str:
    where = f"branch {branch}" if branch else "a detached HEAD"
    commit = head[:12] if head else "no commit"
    if clean is True:
        state = "clean, and every path was read"
    elif clean is False:
        state = f"{dirty} dirty {'path' if dirty == 1 else 'paths'}"
        if incomplete:
            state += ", and the scan was incomplete, so there may be more"
    else:
        unread = "path" if len(incomplete) == 1 else "paths"
        state = (
            "not known to be clean, since the scan was incomplete and found no dirty"
            f" path, with {len(incomplete)} unread {unread} or warnings"
        )
    return f"{where} at {commit}: {state}."
