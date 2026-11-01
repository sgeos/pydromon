"""Commit runs, a run of a declared gate on a commit a line names, in a checkout the
daemon makes and removes. **Round 14, item 1.** ``docs/architecture/RUNS.md`` is
normative, under "A gate run that a reading line may start".

A review seat may only read its working directory, so it may not start an ordinary
run, which writes there outside its sandbox. A commit run lets it start a gate
without writing any worktree. The daemon checks that the commit is in the project's
repository and is the tip of a ref the operator allows, clones the repository with
``--no-checkout --shared`` into a private directory in the daemon's state
directory, which no line can reach, and checks out the commit detached. The run's
command runs there, with a fresh private home beside it, and both are removed once
the run no longer blocks its scope.

Every function here blocks, so the scheduler runs each on its own commit worker,
not on the process worker that serves stops and reconciliation.

Trust. Every git call takes its arguments as an array, never a shell string, with
hooks, filters, credential helpers and replace refs turned off, with neither the
operator's global nor the system's git configuration, with an environment of the
search path alone, without a terminal prompt, and with a time bound that kills its
whole process group. A commit is a full hexadecimal object name, never a ref, so it
cannot move between the check and the checkout. **Round 14, review of 1449c5c.** It
must be the tip of a ref under ``refs/remotes/``, which a line's sandbox may not
write, rather than any commit a ref contains, so a commit merged and reverted since,
or one a line put under a branch, does not qualify. The commit's own code runs with
the daemon's authority and outside every sandbox, which the operator accepts by
declaring a commit command, as ``RUNS.md`` argues.

Porting note. The removal relies on ``shutil.rmtree`` taking a directory descriptor
and not following links, which ``avoids_symlink_attacks`` reports. A port uses
``openat`` and ``unlinkat`` with ``AT_SYMLINK_NOFOLLOW``. Where the platform cannot,
commit runs are refused rather than removed unsafely.
"""

import contextlib
import os
import shutil
import signal
import stat
import subprocess
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dromon import workdir
from dromon.model import COMMIT_PATTERN, ID_PATTERN
from dromon.sandbox import SandboxError

GIT_SECONDS: Final = 60.0
"""The bound on one git call. A commit run's start makes at most six, so it holds
the commit worker for at most about six times this."""

REAP_SECONDS: Final = 5.0
"""How long a git call killed at its bound is waited for once its group is sent
``SIGKILL``. A call not reaped by then is left, and the start is refused all the
same."""

REFUSED_COOLDOWN_SECONDS: Final = 120.0
"""How long a scope starts no commit run after a start of its own failed in the
checks or the checkout, which ``commit_run_refused`` records. **Round 14, review of
1449c5c, C3.** A failed start costs the commit worker up to six git calls and
recorded nothing before, so a line could repeat it without bound."""

REMOTE_REFS: Final = "refs/remotes/"
"""The only refs a commit run's commit may be the tip of. A line's sandbox denies
writing them, and the files backend keeps them under ``refs/remotes`` and in
``packed-refs``, which ``workdir.git_write_guards`` guards, so they move only by
the operator's own fetch."""

TREE: Final = "tree"
HOME: Final = "home"
"""The two directories of a run's private directory, the checkout and the home the
command is given."""

MAX_REF_PATTERNS: Final = 16
MAX_REF_PATTERN_BYTES: Final = 256
MAX_MATCHED_REFS: Final = 64
"""The most matched refs whose paths are checked. One that points at the commit is
enough, and the first are checked, so the check is bounded."""
MAX_SWEPT: Final = 4096
"""The most entries one sweep reads, so a directory filled by a fault is still
bounded. What is left is removed by the next sweep."""

DIRECTORY_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
GIT_CONFIG: Final = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "credential.helper=",
    "-c",
    "submodule.recurse=false",
    "-c",
    "filter.lfs.process=",
    "-c",
    "filter.lfs.smudge=",
    "-c",
    "filter.lfs.clean=",
    "-c",
    "filter.lfs.required=false",
)
"""Hooks are off, so neither a hook in the clone nor one the project's repository
names runs before the command does. No file system monitor starts, no credential
helper is asked, submodules are not followed, and the large file filter does
nothing even where the project's repository configures it. The operator's global
and the system configuration are not read at all, as ``git_environment`` gives.
**Round 14, review of 1449c5c, C2.**"""

NO_FILTERS: Final = "* -filter\n"
"""The clone's ``info/attributes``, which outranks every ``.gitattributes`` a commit
carries, so no path names a filter in the checkout."""


class CommitRefused(ValueError):
    """A commit run cannot start, or its checkout cannot be made or removed."""


RefAnchor = tuple[tuple[int, int], tuple[int, int]]
"""The device and inode of the common git directory's ``refs`` and ``refs/remotes``,
read when the daemon starts. ``(0, 0)`` stands for a ``refs/remotes`` that did not
exist then."""


@dataclass(frozen=True, slots=True)
class Checkout:
    """A commit run's checkout, ``tree``, and the empty private home its command is
    given, ``home``, both in the run's own private directory."""

    tree: Path
    home: Path


def supported() -> bool:
    """Whether a checkout can be removed without following a link in it."""
    return shutil.rmtree.avoids_symlink_attacks


def git_environment(base: Mapping[str, str]) -> dict[str, str]:
    """The environment of every git call for commit runs, which keeps only
    ``base``'s search path. **Round 14, review of 1449c5c, C2.** Neither the
    operator's global nor the system's configuration is read, so a filter or a
    credential helper configured there is not reached, as a commit's
    ``.gitattributes`` and ``.lfsconfig`` reached them before. No home is given, no
    attribute file of the system is read, replace refs are ignored, the large file
    filter skips its download, and git's prompt for credentials is off, since no one
    answers it."""
    return {
        "PATH": base.get("PATH", os.defpath),
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git(
    arguments: Sequence[str], cwd: Path, environment: Mapping[str, str]
) -> subprocess.CompletedProcess[bytes]:
    """Run git in a session of its own, so that a call killed at its bound is killed
    with every process it started, such as a filter, rather than the first alone.
    Raises ``CommitRefused``."""
    argv = ["git", *GIT_CONFIG, *arguments]
    try:
        process = subprocess.Popen(  # noqa: S603
            argv,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise CommitRefused(f"git cannot run: {error}") from error
    try:
        stdout, stderr = process.communicate(timeout=GIT_SECONDS)
    except subprocess.TimeoutExpired as error:
        _kill_group(process)
        raise CommitRefused(
            f"git {arguments[0]} took longer than {GIT_SECONDS:g} seconds, so its"
            " process group was killed"
        ) from error
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Send ``SIGKILL`` to ``process``'s group, which ``start_new_session`` made it
    the leader of, wait at most ``REAP_SECONDS`` for it, and close its pipes, which
    a survivor outside the group may still hold. Never raises."""
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except OSError:
        with contextlib.suppress(OSError):
            process.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=REAP_SECONDS)
    for pipe in (process.stdout, process.stderr):
        if pipe is not None:
            pipe.close()


def _said(result: subprocess.CompletedProcess[bytes]) -> str:
    """The first line git wrote on its error output, bounded, for a refusal."""
    text = result.stderr.decode(errors="replace").strip().splitlines()
    return text[0][:200] if text else f"exit status {result.returncode}"


def check_patterns(patterns: Sequence[str]) -> tuple[str, ...]:
    """``patterns`` as the project file may declare them, each a ref pattern under
    ``refs/remotes/`` naming a remote. Raises ``CommitRefused`` naming the first that
    is not. **Round 14, review of 1449c5c, C1.** A line's sandbox may write
    ``refs/heads``, ``refs/tags`` and every other ref outside ``refs/remotes``, so a
    pattern there would let a line choose the code a reading seat runs outside every
    sandbox."""
    if not 0 < len(patterns) <= MAX_REF_PATTERNS:
        raise CommitRefused(f"declare 1 to {MAX_REF_PATTERNS} ref patterns")
    for pattern in patterns:
        if (
            len(pattern.encode()) > MAX_REF_PATTERN_BYTES
            or any(ord(character) < 0x21 for character in pattern)
            or ".." in pattern
        ):
            raise CommitRefused(
                f"{pattern!r} is not a ref pattern of at most"
                f" {MAX_REF_PATTERN_BYTES} bytes without spaces or '..'"
            )
        remote, _, rest = pattern.removeprefix(REMOTE_REFS).partition("/")
        if (
            not pattern.startswith(REMOTE_REFS)
            or not remote
            or not rest
            or any(character in remote for character in "*?[\\")
        ):
            raise CommitRefused(
                f"{pattern!r} is not under {REMOTE_REFS}REMOTE/, the only refs a"
                " line's sandbox may not write. Refs under refs/heads, refs/tags or"
                " elsewhere can be moved by a line, so they cannot choose what a"
                " commit run executes"
            )
    return tuple(patterns)


def check_commit(
    repository: Path,
    commit: str,
    patterns: Sequence[str],
    environment: Mapping[str, str],
    *,
    anchor: RefAnchor,
) -> None:
    """Refuse ``commit`` unless it is a full object name of a commit in
    ``repository`` that a ref matching one of ``patterns`` points at now, in a
    repository whose refs are kept by the files backend. Raises ``CommitRefused``.
    **Round 14, review of 1449c5c, C1.** Only a tip qualifies, not every commit a
    ref contains, so a commit since reverted or fixed does not run.

    **Round 14, re-review of 522c146, C1a.** The checkout's ``.git``, the common git
    directory, its ``refs``, ``refs/remotes`` and ``packed-refs``, and each directory
    and loose file of a matched ref must be real and this user's, not links, checked
    with ``lstat``. With ``refs`` replaced by a link to a line's directory, git read a
    line's ref as ``origin/main``. The store is read again after the match, and a
    change refuses the commit, so a swap during the match is caught too.

    **Round 14, final check of 951b9e7, item 2.** A real directory a line moved into
    the place of ``refs`` or ``refs/remotes`` passes the ``lstat`` checks, since it
    is neither a link nor another user's. ``anchor`` holds the identities of both as
    ``ref_anchor`` read them when the daemon started, and a commit is refused when
    either differs. An operator's fetch and ``git gc`` rewrite files inside them and
    replace neither directory, so they do not trip it. **Residual.** A swap made
    before the daemon started is anchored as found, and only the sandbox keeps a
    line from renaming ``refs``, as ``SANDBOX.md`` gives."""
    if COMMIT_PATTERN.fullmatch(commit) is None:
        raise CommitRefused(
            f"{commit!r} is not a full commit, 40 or 64 lowercase hexadecimal"
            " characters. Name a commit, never a branch, so it cannot move"
        )
    found = _git(["cat-file", "-e", f"{commit}^{{commit}}"], repository, environment)
    if found.returncode != 0:
        raise CommitRefused(f"{commit} is not a commit in the project's repository")
    storage = _git(
        ["config", "--get", "extensions.refStorage"], repository, environment
    )
    backend = storage.stdout.decode(errors="replace").strip().lower()
    if storage.returncode not in (0, 1) or backend not in ("", "files"):
        raise CommitRefused(
            "the project's repository does not keep its refs in the files backend,"
            " so a line's sandbox cannot be kept from moving refs/remotes"
        )
    common = _common_directory(repository)
    before = _ref_store(common)
    if before[:2] != anchor:
        raise CommitRefused(
            f"the refs or refs/remotes directory of {common} is not the one the"
            " daemon found when it started, so no commit run is trusted. Check them,"
            " then restart the daemon"
        )
    tips = _git(
        ["for-each-ref", "--format=%(refname)", f"--points-at={commit}", *patterns],
        repository,
        environment,
    )
    if tips.returncode != 0:
        raise CommitRefused(f"the refs at {commit} cannot be read: {_said(tips)}")
    matched = tips.stdout.decode(errors="replace").split()
    if not matched:
        raise CommitRefused(
            f"no ref matching {', '.join(patterns)} points at {commit}. Only the tip"
            " of an allowed ref runs, so name the commit such a ref holds now"
        )
    for name in matched[:MAX_MATCHED_REFS]:
        _check_ref_path(common, name)
    if _ref_store(common) != before:
        raise CommitRefused(
            f"the refs of {common} changed while {commit} was checked, so it is"
            " refused. Start it again once no fetch is running"
        )


def ref_anchor(repository: Path) -> RefAnchor:
    """The identities of ``refs`` and ``refs/remotes`` in the common git directory
    of ``repository``, each checked as ``check_commit`` checks it, for the daemon to
    read once at its start and give to every ``check_commit``. Round 14, final check
    of 951b9e7, item 2. Raises ``CommitRefused``."""
    store = _ref_store(_common_directory(repository))
    return (store[0], store[1])


def _common_directory(repository: Path) -> Path:
    """The common git directory of ``repository``, found as the sandbox guards find
    it, after checking that the checkout's ``.git`` is not a link. Raises
    ``CommitRefused``."""
    dot = repository / ".git"
    try:
        found = os.lstat(dot)
    except OSError as error:
        raise CommitRefused(f"{dot} cannot be read: {error}") from error
    if stat.S_ISLNK(found.st_mode) or found.st_uid != os.getuid():
        raise CommitRefused(f"{dot} is a link or not this user's, so its refs are not")
    try:
        common = workdir.git_common_directory(repository)
    except SandboxError as error:
        raise CommitRefused(str(error)) from error
    if common is None:
        raise CommitRefused(f"{repository} is not a git checkout")
    _real_entry(common, directory=True, required=True)
    return common


def _real_entry(path: Path, *, directory: bool, required: bool) -> tuple[int, int]:
    """The device and inode of ``path``, read with ``lstat``, after checking that it
    is a directory or a regular file as ``directory`` says, owned by this user, and
    not a link. ``(0, 0)`` stands for a path that does not exist, which is refused
    when ``required``. Raises ``CommitRefused``. **Round 14, re-review of 522c146,
    C1a.** A link would let git read refs from a directory a line made."""
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        if required:
            raise CommitRefused(f"{path} does not exist") from None
        return (0, 0)
    except OSError as error:
        raise CommitRefused(f"{path} cannot be read: {error}") from error
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        stat.S_ISLNK(found.st_mode)
        or not kind(found.st_mode)
        or found.st_uid != os.getuid()
    ):
        raise CommitRefused(
            f"{path} is not a real {'directory' if directory else 'file'} of this"
            " user, so the refs a commit run names cannot be trusted"
        )
    return (found.st_dev, found.st_ino)


def _ref_store(common: Path) -> tuple[tuple[int, int], ...]:
    """The identities of ``refs``, ``refs/remotes`` and ``packed-refs`` in the common
    git directory, each checked by ``_real_entry``. ``refs`` must exist, the other two
    may not. Raises ``CommitRefused``."""
    refs = common / "refs"
    return (
        _real_entry(refs, directory=True, required=True),
        _real_entry(refs / "remotes", directory=True, required=False),
        _real_entry(common / "packed-refs", directory=False, required=False),
    )


def _check_ref_path(common: Path, name: str) -> None:
    """Check each directory of the matched ref ``name`` below ``refs/remotes``, and
    its loose file where there is one, with ``_real_entry``. A ref only in
    ``packed-refs`` has no file. Raises ``CommitRefused``."""
    parts = name.split("/")
    if (
        not name.startswith(REMOTE_REFS)
        or len(name.encode()) > MAX_REF_PATTERN_BYTES
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise CommitRefused(f"the matched ref {name!r} cannot be checked")
    path = common / "refs" / "remotes"
    for part in parts[2:-1]:
        path = path / part
        _real_entry(path, directory=True, required=False)
    _real_entry(path / parts[-1], directory=False, required=False)


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
) -> Checkout:
    """A detached checkout of ``commit`` at ``base/run_id/tree``, made with a clone
    that writes nothing into ``repository``, and an empty home at
    ``base/run_id/home``, each with mode 0700. Raises ``CommitRefused``, leaving
    nothing behind, when any step fails, or when the checkout's ``HEAD`` is not the
    commit. ``environment`` is given to git as it is, so the caller passes
    ``git_environment``'s."""
    if ID_PATTERN.fullmatch(run_id) is None:
        raise CommitRefused(f"{run_id!r} is not a run identifier")
    _private_base(base)
    run_dir = base / run_id
    try:
        os.mkdir(run_dir, 0o700)
    except OSError as error:
        raise CommitRefused(f"{run_dir} cannot be made: {error}") from error
    found = Checkout(run_dir / TREE, run_dir / HOME)
    try:
        try:
            os.mkdir(found.home, 0o700)
        except OSError as error:
            raise CommitRefused(f"{found.home} cannot be made: {error}") from error
        clone = [
            "clone",
            "--quiet",
            "--no-checkout",
            "--shared",
            "--",
            str(repository),
            str(found.tree),
        ]
        _step(clone, run_dir, environment, commit)
        _no_filters(found.tree)
        _step(
            ["checkout", "--quiet", "--detach", commit], found.tree, environment, commit
        )
        head = _git(["rev-parse", "HEAD"], found.tree, environment)
        if head.returncode != 0 or head.stdout.decode().strip() != commit:
            raise CommitRefused(f"the checkout of {commit} does not hold it")
    except CommitRefused:
        remove_checkout(base, run_id)
        raise
    return found


def _step(
    arguments: Sequence[str], cwd: Path, environment: Mapping[str, str], commit: str
) -> None:
    """Run one git step of a checkout. Raises ``CommitRefused`` when it fails."""
    result = _git(arguments, cwd, environment)
    if result.returncode != 0:
        raise CommitRefused(f"git {arguments[0]} of {commit} failed: {_said(result)}")


def _no_filters(tree: Path) -> None:
    """Write ``NO_FILTERS`` as the new clone's ``info/attributes``, before anything
    is checked out. Raises ``CommitRefused``."""
    info = tree / ".git" / "info"
    try:
        info.mkdir(mode=0o700, exist_ok=True)
        (info / "attributes").write_text(NO_FILTERS)
    except OSError as error:
        raise CommitRefused(
            f"filters cannot be turned off in {tree}: {error}"
        ) from error


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
