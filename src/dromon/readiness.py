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

import hashlib
import os
import stat
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from dromon import spawn, workdir

GIT_STATUS_SECONDS: Final = 20.0
"""How long ``git status`` may run before the report is refused."""

MAX_READY_PATHS: Final = 100
"""The most dirty paths a report names. The rest are counted."""

MAX_READY_PATH_CHARACTERS: Final = 300
"""The longest path a report names whole. A longer one is cut and marked."""


class ReadFailure(StrEnum):
    """Why the worktree's ``git status`` could not be read, in one fixed word.
    **Operator task of 2026-11-05.** A failed read had left a stale brief unflagged
    with no reason given, which read as a current brief. The word is chosen by the
    daemon from where the read stopped, never from git's own text, which names paths
    and may carry anything.

    ``TIMED_OUT``, git ran past ``GIT_STATUS_SECONDS``. ``NOT_STARTED``, git could not
    be started, or its start stalled past ``spawn.START_SECONDS``, since
    ``spawn.StartStalled`` is an ``OSError``. ``FAILED``, git exited with an error,
    or the directory is outside any checkout, where git is not started.
    ``MALFORMED``, git's output was not in the porcelain form. ``NOT_TEXT``, git's
    output could not be decoded. ``NO_WORKDIR``, the line's working directory is
    not known or not allowed, so git was not run."""

    TIMED_OUT = "timed_out"
    NOT_STARTED = "not_started"
    FAILED = "failed"
    MALFORMED = "malformed"
    NOT_TEXT = "not_text"
    NO_WORKDIR = "no_workdir"


class ReadinessUnknown(RuntimeError):
    """The worktree's state could not be read, so no readiness is reported.
    ``cause`` says why in a fixed word, apart from the message, which may name a
    path."""

    def __init__(self, message: str, cause: ReadFailure) -> None:
        super().__init__(message)
        self.cause = cause


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
            raise ReadinessUnknown(
                f"git status gave an entry it should not: {field!r}",
                ReadFailure.MALFORMED,
            )
        status, path = field[:2], field[3:]
        if status[0] in "RC" or status[1] in "RC":
            if index >= len(fields):
                raise ReadinessUnknown(
                    "git status gave a rename with no origin", ReadFailure.MALFORMED
                )
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
    start, runs too long, fails or gives output that is not text, since a report must
    not claim a clean tree it did not read. Blocking, so the scheduler runs it off its
    event loop."""
    return parse_porcelain(_status(worktree))


def dirty_digest(worktree: Path) -> tuple[str, int]:
    """The SHA-256 of ``worktree``'s ``git status`` entries, each path with its two
    status letters in git's own order, and how many entries there are. **Review of
    49236ad, item 4.** A standing brief keeps the digest, so a later turn can tell
    that the uncommitted state changed even where ``HEAD`` did not. The entries are
    hashed whole, unbounded by ``MAX_READY_PATHS``, and the file contents are not
    read, so an edit inside a path that was already dirty leaves the digest as it
    was. Raises ``ReadinessUnknown`` as ``dirty_paths`` does. Blocking."""
    output = _status(worktree)
    return (
        hashlib.sha256(output.encode("utf-8", "surrogateescape")).hexdigest(),
        parse_porcelain(output).total,
    )


MAX_TREE_PATHS: Final = 10_000
"""The most changed tracked paths ``tree_digest`` reads the size and time of. A
tree with more is answered as one whose coverage cannot be told."""


class Uncovered(StrEnum):
    """Why ``tree_digest`` read ``git status`` and still cannot say whether a tree
    changed, in one fixed word. **Round 16 review, findings 3 and 4.**

    ``TOO_MANY_PATHS``, more than ``MAX_TREE_PATHS`` tracked paths are changed, and
    an edit past the cap would not move the digest. ``UNSTAMPED_PATH``, a changed
    tracked path that git does not list as deleted could not be stated, as where
    ``core.worktree`` puts the work tree away from the ``.git`` entry, so an edit
    inside it would not move the digest."""

    TOO_MANY_PATHS = "too_many_paths"
    UNSTAMPED_PATH = "unstamped_path"


class TreeUncovered(RuntimeError):
    """``tree_digest`` cannot give a digest that would move with every edit it
    covers. ``cause`` says why in a fixed word, apart from the message, which may
    name a path. ``dirty`` is how many paths ``git status`` listed, which was read
    before the digest failed, so a receipt can still say that the tree was not clean."""

    def __init__(
        self, message: str, cause: Uncovered, dirty: int | None = None
    ) -> None:
        super().__init__(message)
        self.cause = cause
        self.dirty = dirty


@dataclass(frozen=True, slots=True)
class TreeSnapshot:
    """What one read of ``git status`` gives of a working directory. **Round 18,
    item 2.** ``digest`` is ``tree_digest``'s, and ``dirty`` counts every path the
    status listed, changed or untracked, so ``0`` is a clean tree. Both come from the
    same read, so a receipt's digest and its dirty count describe one moment."""

    digest: str
    dirty: int


def tree_digest(worktree: Path) -> str:
    """The SHA-256 of the state of ``worktree`` that a run or a gate covered: the
    commit at ``HEAD``, the ``git status`` entries of tracked paths, and the size and
    modification time of each. **Round 16, item 5, at core's request of round 15.**
    A run records it as it starts, and ``run_status`` compares it with the digest
    now, so a gate that passed on a tree since changed is not read as covering it.
    Raises ``ReadinessUnknown`` as ``dirty_paths`` does. Blocking.

    **Covered.** A commit, a tracked path that becomes changed or clean, and an
    edit inside a tracked path already changed, which moves its size or time.
    **Not covered, a residual.** Untracked paths are left out whole, new ones and
    edited ones alike, since a run's own output in an untracked directory that is
    not ignored would otherwise make every run read as no longer covering its
    tree. A new test file is therefore not seen until it is added. An edit that
    keeps a file's size and restores its modification time is not seen either.

    **Round 16 review, findings 3 and 4.** A digest that an edit could leave as it
    was is not given. Past ``MAX_TREE_PATHS`` changed tracked paths, or where a
    changed tracked path that git does not list as deleted cannot be stated, this
    raises ``TreeUncovered``, and the run's coverage reads as unknown. The second
    covers a work tree that ``core.worktree`` puts elsewhere, which ``checkout_top``
    does not follow, and a path deleted between the status and the stat. **A
    residual.** Where ``core.worktree`` is set and a file of the same name happens
    to lie beside the ``.git`` entry, that file is stated instead."""
    return tree_snapshot(worktree).digest


def tree_snapshot(worktree: Path) -> TreeSnapshot:
    """``tree_digest``'s digest with the count of every path ``git status`` listed,
    from one read of the status. **Round 18, item 2.** Raises what ``tree_digest``
    does, and a ``TreeUncovered`` carries the count. Blocking."""
    output = _status(worktree)
    head = workdir.read_head_commit(worktree) or ""
    top = workdir.checkout_top(worktree)
    entries = _tracked_changes(output)
    dirty = parse_porcelain(output).total
    if len(entries) > MAX_TREE_PATHS:
        raise TreeUncovered(
            f"{len(entries)} tracked paths are changed in {worktree}, more than"
            f" {MAX_TREE_PATHS}",
            Uncovered.TOO_MANY_PATHS,
            dirty,
        )
    digest = hashlib.sha256()
    digest.update(head.encode("ascii", "replace") + b"\0")
    digest.update(f"{len(entries)}\0".encode("ascii"))
    for entry, path in entries:
        digest.update(entry.encode("utf-8", "surrogateescape") + b"\0")
        try:
            digest.update(_path_stamp(top, entry[:2], path))
        except TreeUncovered as error:
            error.dirty = dirty
            raise
    return TreeSnapshot(digest.hexdigest(), dirty)


MAX_CONTENT_ENTRIES: Final = 200_000
"""The most entries of ``HEAD``'s tree that ``content_digest`` lists. A larger tree
is answered as one whose content cannot be told."""

GIT_READ_SECONDS: Final = 20.0
"""How long each git read of ``content_digest`` may run."""

_REGULAR: Final = "100644"
_EXECUTABLE: Final = "100755"
_SYMLINK: Final = "120000"
_GITLINK: Final = "160000"


def content_digest(worktree: Path) -> str:
    """The SHA-256 of the content a run in ``worktree`` would test, the path, mode
    and git object of every tracked file, whether it is committed or not.

    **Round 19, at core's and ops' request.** ``tree_digest`` covers ``HEAD`` and the
    stamps of the changed paths, so a commit of exactly the uncommitted tree changes
    it, though not one file a run reads changed. The operator committing a line's
    tree dropped its queued gate after hours of waiting. This digest does not depend
    on how the content is split between ``HEAD`` and the working files. It is the
    tree ``HEAD`` names with each path ``git status`` lists as changed replaced by
    its file now, or removed where the file is gone, so committing that tree, or
    resetting to a commit that holds it, gives the same digest, and a change of any
    one tracked file gives another.

    **How it is read.** Four reads of git, none of which writes, ``git status``,
    ``git ls-tree -r`` of the commit ``HEAD`` names, ``git config core.fileMode``,
    and ``git hash-object --stdin-paths`` for the changed regular files, which
    applies the repository's clean filters as ``git add`` would. A link is hashed
    from its target. Nothing is written to the object store. ``HEAD`` is read before
    and after, and a move between answers that the content cannot be told now.

    **Not covered, residuals.** Untracked files are left out, as ``tree_digest``
    leaves them out. A change to a submodule, a path that is now a directory, a path
    holding a newline, which ``--stdin-paths`` cannot carry, more than
    ``MAX_TREE_PATHS`` changed paths or ``MAX_CONTENT_ENTRIES`` entries, and an
    unborn or unreadable ``HEAD`` raise ``TreeUncovered``, so a caller treats the
    content as unknown and never as unchanged. A path marked assume-unchanged or
    skip-worktree is not listed by ``git status`` and is read from ``HEAD``. Raises
    ``ReadinessUnknown`` where git cannot be read. Blocking.

    **Porting note.** Plain reads of child processes and a sorted fold, bounded by
    the two maxima."""
    head = workdir.read_head_commit(worktree)
    top = workdir.checkout_top(worktree)
    if head is None or top is None:
        raise TreeUncovered(
            f"{worktree} has no readable HEAD or checkout top",
            Uncovered.UNSTAMPED_PATH,
        )
    entries = _tracked_changes(_status(worktree))
    if len(entries) > MAX_TREE_PATHS:
        raise TreeUncovered(
            f"{len(entries)} tracked paths are changed in {worktree}, more than"
            f" {MAX_TREE_PATHS}",
            Uncovered.TOO_MANY_PATHS,
        )
    listing = _tree_listing(top, head)
    file_mode = _git_text(top, ("config", "--bool", "core.fileMode"), ok=(0, 1))
    trust_mode = file_mode.strip() != "false"
    changed: set[str] = set()
    for entry, path in entries:
        changed.add(path)
        if "\0" in entry:
            changed.add(entry.split("\0", 1)[1])
    regular: list[tuple[str, str]] = []
    for path in sorted(changed):
        if listing.get(path, ("", ""))[0] == _GITLINK or "\n" in path:
            raise TreeUncovered(
                f"the changed path {path!r} is a submodule or holds a newline",
                Uncovered.UNSTAMPED_PATH,
            )
        try:
            found = os.lstat(top / path)
        except FileNotFoundError:
            listing.pop(path, None)
            continue
        except (OSError, ValueError) as error:
            raise TreeUncovered(
                f"the changed path {path!r} could not be stated: {error}",
                Uncovered.UNSTAMPED_PATH,
            ) from error
        if stat.S_ISLNK(found.st_mode):
            target = os.fsencode(os.readlink(top / path))
            blob = _git_text(top, ("hash-object", "--stdin"), stdin=target).strip()
            listing[path] = (_SYMLINK, blob)
        elif stat.S_ISREG(found.st_mode):
            mode = _EXECUTABLE if found.st_mode & stat.S_IXUSR else _REGULAR
            before = listing.get(path, ("", ""))[0]
            if not trust_mode and before in (_REGULAR, _EXECUTABLE):
                mode = before
            regular.append((path, mode))
        else:
            raise TreeUncovered(
                f"the changed path {path!r} is neither a file nor a link",
                Uncovered.UNSTAMPED_PATH,
            )
    if regular:
        paths = "".join(f"{path}\n" for path, _ in regular)
        blobs = _git_text(
            top,
            ("hash-object", "--stdin-paths"),
            stdin=paths.encode("utf-8", "surrogateescape"),
        ).split()
        if len(blobs) != len(regular):
            raise ReadinessUnknown(
                "git hash-object gave a count of objects it should not",
                ReadFailure.MALFORMED,
            )
        for (path, mode), blob in zip(regular, blobs, strict=True):
            listing[path] = (mode, blob)
    if workdir.read_head_commit(worktree) != head:
        raise ReadinessUnknown(
            f"HEAD moved in {worktree} while its content was read", ReadFailure.FAILED
        )
    digest = hashlib.sha256(b"content\0")
    for path in sorted(listing):
        mode, name = listing[path]
        digest.update(f"{mode} {name} {path}\0".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


def _tree_listing(top: Path, commit: str) -> dict[str, tuple[str, str]]:
    """Each entry of ``commit``'s whole tree, its path to its mode and object name.
    Raises ``TreeUncovered`` past ``MAX_CONTENT_ENTRIES`` entries, and
    ``ReadinessUnknown`` for output not in ``git ls-tree -z`` form."""
    output = _git_text(top, ("ls-tree", "-r", "-z", "--full-tree", commit))
    fields = output.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) > MAX_CONTENT_ENTRIES:
        raise TreeUncovered(
            f"{commit} holds {len(fields)} entries, more than {MAX_CONTENT_ENTRIES}",
            Uncovered.TOO_MANY_PATHS,
        )
    listing: dict[str, tuple[str, str]] = {}
    for field in fields:
        meta, tab, path = field.partition("\t")
        parts = meta.split(" ")
        if not tab or len(parts) != 3:
            raise ReadinessUnknown(
                "git ls-tree gave an entry it should not", ReadFailure.MALFORMED
            )
        listing[path] = (parts[0], parts[2])
    return listing


def _git_text(
    top: Path,
    arguments: tuple[str, ...],
    stdin: bytes | None = None,
    ok: tuple[int, ...] = (0,),
) -> str:
    """The output of git with ``arguments`` in ``top``, decoded as UTF-8 with any
    other byte kept as a surrogate, as paths may hold any byte. Raises
    ``ReadinessUnknown`` where git cannot start, runs too long, or exits with a code
    outside ``ok``."""
    output = _run_git_read(top, arguments, stdin, ok)
    return output.decode("utf-8", "surrogateescape")


def _run_git_read(
    top: Path, arguments: tuple[str, ...], stdin: bytes | None, ok: tuple[int, ...]
) -> bytes:
    """Start one git read for ``content_digest`` and give its output. The one place
    that function starts a child, so that the test suite can stand in for it, as it
    does for ``_run_git_status``. Its environment is the one ``git status`` gets."""
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        done = spawn.run(
            ["git", "-C", str(top), *arguments],
            input=b"" if stdin is None else stdin,
            capture_output=True,
            timeout=GIT_READ_SECONDS,
            check=False,
            env=environment,
        )
    except subprocess.TimeoutExpired as error:
        raise ReadinessUnknown(
            f"git {arguments[0]} ran past {GIT_READ_SECONDS:g} seconds",
            ReadFailure.TIMED_OUT,
        ) from error
    except OSError as error:
        raise ReadinessUnknown(
            f"git could not start: {error}", ReadFailure.NOT_STARTED
        ) from error
    if done.returncode not in ok:
        raise ReadinessUnknown(
            f"git {arguments[0]} failed with exit code {done.returncode}",
            ReadFailure.FAILED,
        )
    return done.stdout


def _tracked_changes(output: str) -> list[tuple[str, str]]:
    """Each ``git status --porcelain=v1 -z`` entry other than an untracked one, as
    its status letters with its path and any origin, and the path it names now, in
    git's order. Raises ``ReadinessUnknown`` for output not in that form, as
    ``parse_porcelain`` does."""
    parse_porcelain(output)
    fields = output.split("\0")
    found: list[tuple[str, str]] = []
    index = 0
    # Bounded by the number of fields, since each pass consumes one or two. The
    # form was checked above, so each entry is at least four characters.
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        status, path = field[:2], field[3:]
        entry = field
        if status[0] in "RC" or status[1] in "RC":
            entry = f"{field}\0{fields[index]}"
            index += 1
        if status != "??":
            found.append((entry, path))
    return found


def _path_stamp(top: Path | None, status: str, path: str) -> bytes:
    """The size and modification time of ``path`` under ``top``, as bytes for the
    digest, or a fixed mark for a path whose ``status`` letters name a deletion and
    which is absent. Raises ``TreeUncovered`` for any other path that cannot be
    stated, since a fixed mark there would not move with an edit."""
    if top is not None:
        try:
            found = os.lstat(top / path)
        except (OSError, ValueError):
            pass
        else:
            return f"{found.st_size} {found.st_mtime_ns}\0".encode("ascii")
        if "D" in status:
            return b"absent\0"
    raise TreeUncovered(
        f"the changed path {path!r} could not be stated under the checkout's top",
        Uncovered.UNSTAMPED_PATH,
    )


def _status(worktree: Path) -> str:
    """The output of ``git status --porcelain=v1 -z`` in ``worktree``.

    **Round 16, item 1.** Bisecting runs put a rise in stalled starts in continuous
    integration at the change that read this on every ``set_brief`` and on every
    turn of a kept session. A directory the file system shows to be outside any
    checkout is answered ``FAILED`` here, as git would answer it, without a start.

    **Decided by core, for review.** The digest is not cached on ``HEAD``, the
    index and the worktree's own modification time, as the task offered. An edit
    in place of a tracked file below the top level changes none of the three, so a
    cached digest would hide the very change the stale-brief notice reports. The
    read stays lazy instead, made only for a brief that names a digest, and only
    when a turn is about to show the notice or ``set_brief`` records one."""
    if workdir.outside_checkout(worktree):
        raise ReadinessUnknown(
            f"{worktree} is not in a git checkout, so git status was not run",
            ReadFailure.FAILED,
        )
    return _run_git_status(worktree)


def _run_git_status(worktree: Path) -> str:
    """Start ``git status`` in ``worktree`` and give its output. The one place this
    module starts a child, so that the test suite can stand in for it. **Round 16,
    item 1.** The suite's ``_no_real_git_status`` fixture answers a clean tree here
    for every test not marked ``real_git_status``."""
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        done = spawn.run(
            [
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
            f"git status ran past {GIT_STATUS_SECONDS:g} seconds",
            ReadFailure.TIMED_OUT,
        ) from error
    except OSError as error:
        raise ReadinessUnknown(
            f"git could not start: {error}", ReadFailure.NOT_STARTED
        ) from error
    except UnicodeError as error:
        # Paths may hold any byte git allows, and the output is decoded as text.
        raise ReadinessUnknown(
            "git status gave output that is not text", ReadFailure.NOT_TEXT
        ) from error
    if done.returncode != 0:
        raise ReadinessUnknown(
            f"git status failed with exit code {done.returncode}", ReadFailure.FAILED
        )
    return done.stdout
