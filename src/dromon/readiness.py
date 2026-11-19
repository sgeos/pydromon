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

import contextlib
import hashlib
import os
import selectors
import stat
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, Final

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
"""The most index entries that ``content_digest`` lists and hashes. A larger tree is
answered as one whose content cannot be told."""

MAX_LISTING_BYTES: Final = 128 * 1024 * 1024
"""The most bytes one listing of ``content_digest`` reads, so that a tree of few
entries with very long names is bounded too. A longer listing is answered as one
whose content cannot be told."""

GIT_READ_SECONDS: Final = 20.0
"""How long each git read of ``content_digest`` may run."""

_REGULAR: Final = "100644"
_EXECUTABLE: Final = "100755"
_SYMLINK: Final = "120000"
_GITLINK: Final = "160000"

_READ_SETTINGS: Final = ("-c", "core.fsmonitor=false")
"""Settings given to every git read of ``content_digest``, over the repository's own.
**Round 19 review, item 1.** No read of the digest refreshes the index, and this one
keeps git from running a monitor hook that the repository's configuration names."""


def content_digest(worktree: Path) -> str:
    """The SHA-256 of the content a run in ``worktree`` would test, the path, mode
    and bytes of every file the index tracks, as the files are on disk.

    **Round 19, at core's and ops' request.** ``tree_digest`` covers ``HEAD`` and the
    stamps of the changed paths, so a commit of exactly the uncommitted tree changes
    it, though not one file a run reads changed. The operator committing a line's
    tree dropped its queued gate after hours of waiting. This digest does not depend
    on ``HEAD`` or on what is staged, only on which paths the index tracks and what
    the files hold, so committing the tree, or resetting to a commit that holds it,
    gives the same digest, and a change of any one tracked file gives another.

    **How it is read.** Two reads of git, neither of which writes or runs a program
    that the configuration names. ``git ls-files --stage`` lists the index, and ``git
    hash-object --no-filters --stdin-paths`` hashes every tracked regular file as its
    bytes are. A link is hashed from its target, with its mode, and a tracked path
    whose file is gone is left out. Nothing is written to the object store. ``HEAD``
    is read before and after, and a move between answers that the content cannot be
    told now.

    **Round 19 review, item 1. The configuration does not steer it.** Equality keeps
    a queued run and lets a receipt say it covers the tree, so a difference that
    git's configuration hides would keep a run on content it was not asked for. The
    first form of this digest hashed only what ``git status`` listed as changed, and
    ``git status`` honoured a ``core.fsmonitor`` hook, which hid an edit and ran, a
    clean filter that a tracked ``.gitattributes`` chose, which mapped an edit back
    to its blob and ran, line endings the attributes normalise, ``core.fileMode``
    false, and ``diff.ignoreSubmodules`` or a submodule's ``ignore`` key, which a
    tracked ``.gitmodules`` may carry. Comparing stamps with the index instead does
    not suffice, since any ``git status`` that finds a filtered edit equal records
    its stamps as clean. So no file is trusted to be as committed. Every one is
    hashed from its bytes, without filters, with the mode the file system shows. A
    repository whose index holds a submodule is answered as unknown, since the
    submodule's files are not read. Neither the user's nor the system's
    configuration is loaded. Every difference this finds where git would see none,
    such as line endings a filter would convert, drops a run, the safe direction.

    **Cost.** Each read hashes the whole tracked tree, bounded by
    ``MAX_CONTENT_ENTRIES``, ``MAX_LISTING_BYTES`` and ``GIT_READ_SECONDS`` on each
    read, and a tree too large for those is answered as unknown, so its queued runs
    are dropped on a move of ``HEAD`` as before round 19.

    **Not covered, residuals.** Untracked files are left out, as ``tree_digest``
    leaves them out. A submodule, a tracked path that is now a directory, a tracked
    path holding a newline, which ``--stdin-paths`` cannot carry, a tree past the
    bounds above, and an unborn or unreadable ``HEAD`` raise ``TreeUncovered``, so a
    caller treats the content as unknown and never as unchanged. A file edited while
    it is hashed may be read in either state. ``GIT_CONFIG_GLOBAL`` needs git 2.32 or
    later, and an older git reads the user's configuration, which no longer bears on
    the bytes hashed. The repository's own configuration is read, as git needs it, and
    a line's sandbox guards it. Raises ``ReadinessUnknown`` where git cannot be read.
    Blocking.

    **Porting note.** Plain reads of child processes and a sorted fold, bounded by
    the maxima and a deadline on each read."""
    head = workdir.read_head_commit(worktree)
    top = workdir.checkout_top(worktree)
    if head is None or top is None:
        raise TreeUncovered(
            f"{worktree} has no readable HEAD or checkout top",
            Uncovered.UNSTAMPED_PATH,
        )
    listing: dict[str, tuple[str, str]] = {}
    regular: list[tuple[str, str]] = []
    for path in sorted(_index_paths(top)):
        if "\n" in path:
            raise TreeUncovered(
                f"the tracked path {path!r} holds a newline", Uncovered.UNSTAMPED_PATH
            )
        try:
            found = os.lstat(top / path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            raise TreeUncovered(
                f"the tracked path {path!r} could not be stated: {error}",
                Uncovered.UNSTAMPED_PATH,
            ) from error
        if stat.S_ISLNK(found.st_mode):
            target = os.fsencode(os.readlink(top / path))
            blob = _git_text(
                top, ("hash-object", "--no-filters", "--stdin"), target, 1
            ).strip()
            listing[path] = (_SYMLINK, blob)
        elif stat.S_ISREG(found.st_mode):
            # The mode the file system shows, whatever core.fileMode says.
            mode = _EXECUTABLE if found.st_mode & stat.S_IXUSR else _REGULAR
            regular.append((path, mode))
        else:
            raise TreeUncovered(
                f"the tracked path {path!r} is neither a file nor a link",
                Uncovered.UNSTAMPED_PATH,
            )
    if regular:
        paths = "".join(f"{path}\n" for path, _ in regular)
        blobs = _git_text(
            top,
            ("hash-object", "--no-filters", "--stdin-paths"),
            paths.encode("utf-8", "surrogateescape"),
            len(regular),
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


def _index_paths(top: Path) -> set[str]:
    """Each path the index of the checkout at ``top`` tracks, once, whatever its
    stages. Raises ``TreeUncovered`` past ``MAX_CONTENT_ENTRIES`` entries, counted as
    they are read, and for a submodule, and ``ReadinessUnknown`` for output not in
    ``git ls-files --stage -z`` form."""
    paths: set[str] = set()
    for field in _git_records(top, ("ls-files", "--stage", "-z"), MAX_CONTENT_ENTRIES):
        meta, tab, path = field.partition("\t")
        parts = meta.split(" ")
        if not tab or len(parts) != 3:
            raise ReadinessUnknown(
                "git ls-files gave an entry it should not", ReadFailure.MALFORMED
            )
        if parts[0] == _GITLINK:
            raise TreeUncovered(
                f"the index holds the submodule {path!r}, whose files are not read",
                Uncovered.UNSTAMPED_PATH,
            )
        paths.add(path)
    return paths


def _git_records(top: Path, arguments: tuple[str, ...], limit: int) -> list[str]:
    """The NUL-terminated records git with ``arguments`` writes in ``top``, at most
    ``limit`` of them, decoded as ``_git_text`` decodes."""
    fields = _git_text(top, arguments, None, limit).split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    return fields


def _git_text(
    top: Path, arguments: tuple[str, ...], stdin: bytes | None, limit: int
) -> str:
    """The output of git with ``arguments`` in ``top``, decoded as UTF-8 with any
    other byte kept as a surrogate, as paths may hold any byte. Raises what
    ``_run_git_read`` raises."""
    output = _run_git_read(top, arguments, stdin, limit)
    return output.decode("utf-8", "surrogateescape")


def _read_environment() -> dict[str, str]:
    """The environment of each git read of ``content_digest``. The caller's ``GIT_``
    variables are removed, as for ``git status``, and neither the system's nor the
    user's configuration is loaded, so that neither can steer the digest."""
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    return environment


def _run_git_read(
    top: Path, arguments: tuple[str, ...], stdin: bytes | None, limit: int
) -> bytes:
    """Start one git read for ``content_digest`` and give its output. The one place
    that function starts a child, so that the test suite can stand in for it, as it
    does for ``_run_git_status``. A read without ``stdin`` is a listing, whose
    records are counted as they arrive and refused past ``limit`` with
    ``TreeUncovered``. **Round 19 review, item 2.** The listing of a very large tree
    was collected whole before it was counted. A read with ``stdin`` hashes what it
    is given, so its output is bounded by its input. Raises ``ReadinessUnknown``
    where git cannot start, runs past ``GIT_READ_SECONDS``, or fails."""
    argv = ["git", "-C", str(top), *_READ_SETTINGS, *arguments]
    if stdin is not None:
        return _run_git_hash(argv, arguments[0], stdin)
    deadline = time.monotonic() + GIT_READ_SECONDS
    try:
        process = spawn.start(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_read_environment(),
        )
    except OSError as error:
        raise ReadinessUnknown(
            f"git could not start: {error}", ReadFailure.NOT_STARTED
        ) from error
    stream = process.stdout
    try:
        if stream is None:
            raise ReadinessUnknown(
                f"git {arguments[0]} gave no output pipe", ReadFailure.FAILED
            )
        output = _read_records(stream, limit, GIT_READ_SECONDS, arguments[0])
        code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired as error:
        raise ReadinessUnknown(
            f"git {arguments[0]} ran past {GIT_READ_SECONDS:g} seconds",
            ReadFailure.TIMED_OUT,
        ) from error
    finally:
        if process.poll() is None:
            process.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=GIT_READ_SECONDS)
        if stream is not None:
            stream.close()
    if code != 0:
        raise ReadinessUnknown(
            f"git {arguments[0]} failed with exit code {code}", ReadFailure.FAILED
        )
    return output


def _run_git_hash(argv: list[str], name: str, stdin: bytes) -> bytes:
    """Run ``git hash-object`` with ``stdin`` and give its output."""
    try:
        done = spawn.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=GIT_READ_SECONDS,
            check=False,
            env=_read_environment(),
        )
    except subprocess.TimeoutExpired as error:
        raise ReadinessUnknown(
            f"git {name} ran past {GIT_READ_SECONDS:g} seconds",
            ReadFailure.TIMED_OUT,
        ) from error
    except OSError as error:
        raise ReadinessUnknown(
            f"git could not start: {error}", ReadFailure.NOT_STARTED
        ) from error
    if done.returncode != 0:
        raise ReadinessUnknown(
            f"git {name} failed with exit code {done.returncode}", ReadFailure.FAILED
        )
    return done.stdout


def _read_records(stream: IO[bytes], limit: int, seconds: float, name: str) -> bytes:
    """Read ``stream`` to its end within ``seconds``, counting NUL-terminated
    records as they arrive. Raises ``TreeUncovered`` as soon as more than ``limit``
    records or ``MAX_LISTING_BYTES`` bytes have arrived, without waiting for the
    rest, and ``ReadinessUnknown`` once ``seconds`` pass before the end."""
    deadline = time.monotonic() + seconds
    chunks: list[bytes] = []
    records = 0
    size = 0
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        # Bounded, since each pass reads at least one byte or ends, and the bytes
        # are capped below MAX_LISTING_BYTES, and by the deadline.
        for _ in range(MAX_LISTING_BYTES + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                break
            chunk = os.read(stream.fileno(), 65536)
            if not chunk:
                return b"".join(chunks)
            records += chunk.count(b"\0")
            size += len(chunk)
            if records > limit:
                raise TreeUncovered(
                    f"git {name} listed more than {limit} entries",
                    Uncovered.TOO_MANY_PATHS,
                )
            if size > MAX_LISTING_BYTES:
                raise TreeUncovered(
                    f"git {name} listed more than {MAX_LISTING_BYTES} bytes",
                    Uncovered.TOO_MANY_PATHS,
                )
            chunks.append(chunk)
    raise ReadinessUnknown(
        f"git {name} ran past {seconds:g} seconds", ReadFailure.TIMED_OUT
    )


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
