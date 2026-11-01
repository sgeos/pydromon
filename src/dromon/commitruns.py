"""Commit runs, a run of a declared gate on a commit a line names, in a checkout the
daemon makes and removes. **Round 14, item 1.** ``docs/architecture/RUNS.md`` is
normative, under "A gate run that a reading line may start".

A review seat may only read its working directory, so it may not start an ordinary
run, which writes there outside its sandbox. A commit run lets it start a gate
without writing any worktree. The daemon checks that the commit is in the project's
repository and that a ref the operator allows contains it, clones the repository
with ``--no-checkout --shared`` into a private directory in the daemon's state
directory, which no line can reach, and checks out the commit detached. The run's
command runs there, and the checkout is removed once the run no longer blocks its
scope.

Every function here blocks, so the scheduler runs each on its process worker.

Trust. Every git call takes its arguments as an array, never a shell string, with
hooks turned off, without a terminal prompt and with a time bound. A commit is a
full hexadecimal object name, never a ref, so it cannot move between the check and
the checkout. The commit's own code runs with the daemon's authority and outside
every sandbox, which the operator accepts by declaring a commit command, as
``RUNS.md`` argues.

Porting note. The removal relies on ``shutil.rmtree`` taking a directory descriptor
and not following links, which ``avoids_symlink_attacks`` reports. A port uses
``openat`` and ``unlinkat`` with ``AT_SYMLINK_NOFOLLOW``. Where the platform cannot,
commit runs are refused rather than removed unsafely.
"""

import os
import shutil
import stat
import subprocess
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Final

from dromon.model import COMMIT_PATTERN, ID_PATTERN

GIT_SECONDS: Final = 60.0
"""The bound on one git call. It also bounds how long a commit run's start holds the
process worker, which serves stops and reconciliation, at about four times this."""

MAX_REF_PATTERNS: Final = 16
MAX_REF_PATTERN_BYTES: Final = 256
MAX_SWEPT: Final = 4096
"""The most entries one sweep reads, so a directory filled by a fault is still
bounded. What is left is removed by the next sweep."""

DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
GIT_CONFIG: Final = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "protocol.file.allow=always",
)
"""Hooks are off, so neither a hook the operator configured globally nor one in the
clone runs before the command does. ``protocol.file.allow`` lets the local clone read
the project's repository whatever the global setting says."""


class CommitRefused(ValueError):
    """A commit run cannot start, or its checkout cannot be made or removed."""


def supported() -> bool:
    """Whether a checkout can be removed without following a link in it."""
    return shutil.rmtree.avoids_symlink_attacks


def git_environment(base: Mapping[str, str]) -> dict[str, str]:
    """``base`` with git's prompt for credentials turned off, since no one answers
    it and the call would wait out its bound."""
    return {**base, "GIT_TERMINAL_PROMPT": "0"}


def _git(
    arguments: Sequence[str], cwd: Path, environment: Mapping[str, str]
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(  # noqa: S603
            ["git", *GIT_CONFIG, *arguments],  # noqa: S607
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=GIT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise CommitRefused(
            f"git {arguments[0]} took longer than {GIT_SECONDS:g} seconds"
        ) from error
    except OSError as error:
        raise CommitRefused(f"git cannot run: {error}") from error


def _said(result: subprocess.CompletedProcess[bytes]) -> str:
    """The first line git wrote on its error output, bounded, for a refusal."""
    text = result.stderr.decode(errors="replace").strip().splitlines()
    return text[0][:200] if text else f"exit status {result.returncode}"


def check_patterns(patterns: Sequence[str]) -> tuple[str, ...]:
    """``patterns`` as the project file may declare them, each a ref pattern under
    ``refs/``. Raises ``CommitRefused`` naming the first that is not."""
    if not 0 < len(patterns) <= MAX_REF_PATTERNS:
        raise CommitRefused(f"declare 1 to {MAX_REF_PATTERNS} ref patterns")
    for pattern in patterns:
        if (
            not pattern.startswith("refs/")
            or len(pattern.encode()) > MAX_REF_PATTERN_BYTES
            or any(ord(character) < 0x21 for character in pattern)
            or ".." in pattern
        ):
            raise CommitRefused(
                f"{pattern!r} is not a ref pattern under refs/ of at most"
                f" {MAX_REF_PATTERN_BYTES} bytes without spaces or '..'"
            )
    return tuple(patterns)


def check_commit(
    repository: Path,
    commit: str,
    patterns: Sequence[str],
    environment: Mapping[str, str],
) -> None:
    """Refuse ``commit`` unless it is a full object name of a commit in
    ``repository`` that a ref matching one of ``patterns`` contains. Raises
    ``CommitRefused``."""
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise CommitRefused(
            f"{commit!r} is not a full commit, 40 or 64 lowercase hexadecimal"
            " characters. Name a commit, never a branch, so it cannot move"
        )
    found = _git(["cat-file", "-e", f"{commit}^{{commit}}"], repository, environment)
    if found.returncode != 0:
        raise CommitRefused(f"{commit} is not a commit in the project's repository")
    contained = _git(
        ["for-each-ref", "--format=%(refname)", f"--contains={commit}", *patterns],
        repository,
        environment,
    )
    if contained.returncode != 0:
        raise CommitRefused(
            f"the refs containing {commit} cannot be read: {_said(contained)}"
        )
    if not contained.stdout.strip():
        raise CommitRefused(
            f"no ref matching {', '.join(patterns)} contains {commit}, so the project"
            " does not let a run check it out"
        )


def _private_base(base: Path) -> None:
    """Make ``base`` with mode 0700, or check the one there is a directory, not a
    link, owned by this user and private. Raises ``CommitRefused``."""
    try:
        os.mkdir(base, 0o700)
    except FileExistsError:
        pass
    except OSError as error:
        raise CommitRefused(f"{base} cannot be made: {error}") from error
    try:
        found = os.lstat(base)
    except OSError as error:
        raise CommitRefused(f"{base} cannot be read: {error}") from error
    if (
        not stat.S_ISDIR(found.st_mode)
        or found.st_uid != os.getuid()
        or found.st_mode & 0o077
    ):
        raise CommitRefused(
            f"{base} is not a private directory of this user, so no checkout is made"
            " there"
        )


def make_checkout(
    base: Path,
    run_id: str,
    repository: Path,
    commit: str,
    environment: Mapping[str, str],
) -> Path:
    """A detached checkout of ``commit`` at ``base/run_id``, made with a clone that
    writes nothing into ``repository``. Raises ``CommitRefused``, leaving nothing
    behind, when any step fails, or when the checkout's ``HEAD`` is not the commit."""
    if ID_PATTERN.fullmatch(run_id) is None:
        raise CommitRefused(f"{run_id!r} is not a run identifier")
    _private_base(base)
    target = base / run_id
    try:
        os.mkdir(target, 0o700)
    except OSError as error:
        raise CommitRefused(f"{target} cannot be made: {error}") from error
    try:
        steps: tuple[tuple[Sequence[str], Path], ...] = (
            (
                [
                    "clone",
                    "--quiet",
                    "--no-checkout",
                    "--shared",
                    "--",
                    str(repository),
                    str(target),
                ],
                base,
            ),
            (["checkout", "--quiet", "--detach", commit], target),
        )
        for arguments, cwd in steps:
            result = _git(arguments, cwd, environment)
            if result.returncode != 0:
                raise CommitRefused(
                    f"git {arguments[0]} of {commit} failed: {_said(result)}"
                )
        head = _git(["rev-parse", "HEAD"], target, environment)
        if head.returncode != 0 or head.stdout.decode().strip() != commit:
            raise CommitRefused(f"the checkout of {commit} does not hold it")
    except CommitRefused:
        remove_checkout(base, run_id)
        raise
    return target


def remove_checkout(base: Path, run_id: str) -> None:
    """Remove ``base/run_id`` without following any link in it or at it, or do
    nothing when it is gone. Raises ``CommitRefused`` where the platform cannot
    remove without following links, or the removal fails."""
    if ID_PATTERN.fullmatch(run_id) is None:
        raise CommitRefused(f"{run_id!r} is not a run identifier")
    _remove_entry(base, run_id)


def sweep(base: Path, keep: Collection[str]) -> list[str]:
    """Remove every entry of ``base`` that ``keep`` does not name, reading at most
    ``MAX_SWEPT`` entries, and return the names removed. An entry that is not a run
    identifier is removed too, by its name, never followed. Raises
    ``CommitRefused``."""
    try:
        with os.scandir(base) as entries:
            names = [
                entry.name for _, entry in zip(range(MAX_SWEPT), entries, strict=False)
            ]
    except FileNotFoundError:
        return []
    except OSError as error:
        raise CommitRefused(f"{base} cannot be read: {error}") from error
    removed: list[str] = []
    for name in sorted(names):
        if name in keep:
            continue
        _remove_entry(base, name)
        removed.append(name)
    return removed


def _remove_entry(base: Path, name: str) -> None:
    """Remove the entry ``name`` of ``base``, a directory with everything in it or
    anything else by unlinking it, without following a link at ``base``, at the
    entry or in it, or do nothing when either is gone. Raises ``CommitRefused``
    where the platform cannot remove without following links, or the removal
    fails."""
    if "/" in name or name in ("", ".", ".."):
        raise CommitRefused(f"{name!r} cannot be removed by name")
    if not supported():
        raise CommitRefused(
            "this platform cannot remove a checkout without following links"
        )
    try:
        directory = os.open(base, DIRECTORY_FLAGS)
    except FileNotFoundError:
        return
    except OSError as error:
        raise CommitRefused(f"{base} cannot be opened: {error}") from error
    try:
        found = os.lstat(name, dir_fd=directory)
        if stat.S_ISDIR(found.st_mode):
            shutil.rmtree(name, dir_fd=directory)
        else:
            os.unlink(name, dir_fd=directory)
    except FileNotFoundError:
        return
    except OSError as error:
        raise CommitRefused(f"{base / name} cannot be removed: {error}") from error
    finally:
        os.close(directory)
