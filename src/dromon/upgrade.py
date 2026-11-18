"""``dromon upgrade-check``, which opens a copy of a project's event log and project
file under the installed code, without a daemon and without engines, and reports what an
upgrade to this code would meet. **Round 18, item 1.** ``docs/architecture/UPGRADE.md``
is the procedure this command serves.

**Why.** A project may run a daemon pinned to an old revision for weeks. Nothing said
whether the code installed now opens that daemon's log, whether its snapshot is used,
whether its project file is still accepted, which lines come back held, and whether
a return to the old revision stays possible once the new daemon has written. Each was
found only by starting the new daemon on the live state, which cannot be undone.

**What it reads, and what it writes.** It reads the log, the snapshot and the sealed
store beside it, the project file, the project root given, and the repository of the
installed code for the rollback floors, and it writes only a private scratch
directory, made owner only and removed before it returns, and removes the scratch
directories of its own earlier runs that were killed, as ``sweep_scratch`` gives. The
log is copied as ``dromond --check`` copies a live log, with ``eventlog.snapshot``,
which never writes its source or anything beside it, so it may be pointed at the live
log, though the procedure copies first. A log in write-ahead mode, or with a hot
journal, is first copied byte for byte into the scratch, as ``copy_log`` gives.
No socket is made, no lock is taken, no engine is built, the teacher is not started,
the registry and the fleet index are not written, and the running daemon is never
asked anything.

**The checks**, each reported apart, so a failure of one does not hide the others.

* ``copy``, the log and what lies beside it copied into the scratch directory.
* ``replay``, the copy opened by this code's ``EventLog``, replaying every event, as
  a start without a usable snapshot does. A log holding a kind, a field or a value
  this code does not know is refused here, which is what a log past this code's
  rollback floor looks like.
* ``snapshot``, the snapshot beside the log compared with a full replay, as
  ``dromond --check`` compares it. One written under another state version is
  ignored, and the start then replays the whole log, which is always correct.
* ``start``, the daemon's own check, ``daemon.run`` with ``check_only``, on the
  copies, with the given root, so a project file this code refuses, a sandbox it
  cannot make, a role it cannot record, and a snapshot that differs, each fail here
  in the daemon's own words.
* ``held``, the lines the started state holds, with their causes, and the lines
  hibernated or retired, which an operator recovers by hand after the upgrade.
* ``floor``, with ``--from-commit``, the one-way changes of the rollback floor table
  and its fragments whose floor the old revision does not contain. Once the new
  daemon writes any of them, the old revision cannot open the log.

**Residuals.** The ``start`` check runs only what ``dromond --check`` runs, so what
that check cannot find, recovery and the process table among it, as
``docs/protocol/RESTART.md`` says, is not found here either. The ``floor`` check reads
the table and fragments in the installed code's repository and asks git, so an
installation without its repository reports the floor as unknown. It lists the
changes the new code may write, not the ones it will write, since most are written
only when a party uses the feature. ``held`` reports the state after the start's own
events and not after turns, so a line held later, by a limit or a failed turn, is
not foreseen.
"""

import contextlib
import fcntl
import io
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from dromon import commitruns, daemon, spawn, statesnapshot
from dromon.eventlog import (
    EventLog,
    LogError,
    check_snapshot,
    hold_snapshot,
    snapshot,
)
from dromon.model import State
from dromon.paths import ProjectPaths
from dromon.projectconfig import config_path
from dromon.sealed import store_path

GIT_SECONDS: Final = 60.0
"""The bound on one git call of the floor check."""

MAX_LISTED: Final = 64
"""The most changes, and the most lines, named in one check's detail."""

FLOOR_TABLE: Final = Path("docs/protocol/EVENT_LOG.md")
FLOOR_FRAGMENTS: Final = Path("docs/protocol/floors")
FLOOR_HEADER: Final = "| Change | First commit | Floor on `main` | Branch |"
DEFAULT_FRAGMENT_PATH: Final = "src/dromon/codec.py"
COMMIT_IN_CELL: Final = re.compile(r"`([0-9a-f]{7,64})`")
FRAGMENT_NAME: Final = re.compile(r"[a-z0-9][a-z0-9._-]*\.md")


class Outcome(StrEnum):
    """How one check came out. ``WARN`` asks the operator to act but does not fail."""

    PASS = "pass"  # noqa: S105, an outcome, not a secret
    WARN = "warn"
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    outcome: Outcome
    detail: str


@dataclass(frozen=True, slots=True)
class Report:
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        """Whether no check failed."""
        return all(check.outcome is not Outcome.FAIL for check in self.checks)


@dataclass(frozen=True, slots=True)
class Copied:
    """The copies in the scratch directory. ``through`` is the last event copied,
    and ``held`` the copy of the snapshot, or ``None`` with ``held_refusal`` saying
    why none is used."""

    log: Path
    through: int
    held: Path | None
    held_refusal: str | None


def copy_inputs(
    log_file: Path, project_file: Path | None, state_dir: Path
) -> tuple[Check, Copied | None]:
    """Copy the log, its snapshot and its sealed store, and the project file, into
    ``state_dir`` under the names a start reads there. Never writes a source."""
    if not log_file.is_file():
        return Check("copy", Outcome.FAIL, f"{log_file} is not a file"), None
    if project_file is not None and not project_file.is_file():
        return Check("copy", Outcome.FAIL, f"{project_file} is not a file"), None
    log_copy = state_dir / "events.sqlite"
    held = state_dir / "held-snapshot.json"
    try:
        refusal = hold_snapshot(statesnapshot.snapshot_path(log_file), held)
        through, how = copy_log(log_file, log_copy, state_dir.parent / "recovered")
        sealed = store_path(log_file)
        if sealed.is_dir():
            shutil.copytree(sealed, store_path(log_copy), symlinks=True)
        elif sealed.is_file():
            shutil.copy2(sealed, store_path(log_copy), follow_symlinks=False)
        if project_file is not None:
            shutil.copyfile(project_file, config_path(state_dir))
        if refusal is None:
            # The start's own check reads the snapshot beside the log it is given.
            shutil.copyfile(held, statesnapshot.snapshot_path(log_copy))
    except (LogError, OSError) as error:
        return Check("copy", Outcome.FAIL, f"the copy failed: {error}"), None
    said = f"copied {through} events into a private scratch directory{how.detail}"
    copied = Copied(log_copy, through, None if refusal else held, refusal)
    outcome = Outcome.WARN if how is LogForm.HOT_JOURNAL else Outcome.PASS
    return Check("copy", outcome, said), copied


class LogForm(StrEnum):
    """How the log was found, which decides how it is copied."""

    ROLLBACK = "rollback"
    WAL = "wal"
    HOT_JOURNAL = "hot_journal"

    @property
    def detail(self) -> str:
        """What the copy check adds to its detail for this form."""
        match self:
            case LogForm.ROLLBACK:
                return ""
            case LogForm.WAL:
                return (
                    ". The log is in SQLite's write-ahead mode, which Dromon never"
                    " writes, so it and its -wal file were copied byte for byte into"
                    " the scratch directory and opened there, and nothing was written"
                    " beside it"
                )
            case LogForm.HOT_JOURNAL:
                return (
                    ". A hot journal lies beside the log, as a crash leaves, so the"
                    " log and its -journal file were copied byte for byte into the"
                    " scratch directory and rolled back there, as the next start"
                    " would. Copy the -journal file with the log whenever one is"
                    " there, since a log copied without it can be torn"
                )


WAL_HEADER: Final = slice(18, 20)
"""The bytes of a SQLite database header holding its file format write and read
versions, each 2 in write-ahead mode and 1 in rollback mode."""


def sidecar(log_file: Path, suffix: str) -> Path:
    """A file SQLite keeps beside ``log_file``, such as its ``-journal``."""
    return log_file.with_name(log_file.name + suffix)


def in_wal_mode(log_file: Path) -> bool:
    """Whether ``log_file`` is in write-ahead mode, by its header or a ``-wal`` file
    beside it. Raises ``OSError``."""
    with log_file.open("rb") as reading:
        header = reading.read(WAL_HEADER.stop)
    return 2 in header[WAL_HEADER] or sidecar(log_file, "-wal").exists()


def copy_log(log_file: Path, log_copy: Path, recovered: Path) -> tuple[int, LogForm]:
    """Copy the log into ``log_copy`` without writing anything beside ``log_file``,
    and give the last event copied and the form the log was found in. Raises
    ``LogError`` or ``OSError``.

    **Review of 513928b, finding 2.** ``eventlog.snapshot`` attaches its source
    read-only, which never writes a log in rollback mode, as Dromon keeps it, and so
    may read a live one. SQLite still makes ``-shm`` and ``-wal`` files beside a log
    in write-ahead mode that it opens, even read-only, and cannot open one at all in a
    directory it may not write, and it refuses read-only a log whose hot journal it
    would have to roll back. **Fact, observed on 2026-11-18.** Such a log, and its
    ``-wal`` or ``-journal``, is therefore copied byte for byte into ``recovered``, a
    private directory of the scratch, opened there read-write, which checkpoints the
    one and rolls back the other, as a start would, and only then copied as any log
    is. A hot journal is taken only after the read-only copy fails, since a live
    daemon's commit leaves a ``-journal`` briefly and the read-only copy reads past
    it. **Assumption.** A log in write-ahead mode, or one with a hot journal, is not
    being written while it is copied. Dromon never writes the first, and the second
    is left only by a daemon that has stopped. **Residual.** A plain file copy of a
    log that is being written can be torn, and the replay check then fails or, where
    the tear falls between whole events, copies fewer."""
    if not in_wal_mode(log_file):
        try:
            return snapshot(log_file, log_copy), LogForm.ROLLBACK
        except LogError:
            journal = sidecar(log_file, "-journal")
            if not (journal.is_file() and journal.stat().st_size > 0):
                raise
            log_copy.unlink(missing_ok=True)
        form = LogForm.HOT_JOURNAL
    else:
        form = LogForm.WAL
    os.mkdir(recovered, 0o700)
    private = recovered / "events.sqlite"
    shutil.copyfile(log_file, private)
    for suffix in ("-wal", "-journal"):
        if sidecar(log_file, suffix).is_file():
            shutil.copyfile(sidecar(log_file, suffix), sidecar(private, suffix))
    try:
        opening = sqlite3.connect(private, isolation_level=None)
        try:
            opening.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            opening.execute("PRAGMA journal_mode = DELETE").fetchone()
        finally:
            opening.close()
    except sqlite3.Error as error:
        raise LogError(f"cannot recover the copy of {log_file}: {error}") from error
    return snapshot(private, log_copy), form


def check_replay(log_copy: Path, replay_copy: Path) -> tuple[Check, State | None]:
    """Open a second copy of the log with every event replayed, so the copy the
    start reads stays as copied."""
    try:
        snapshot(log_copy, replay_copy)
        with EventLog(replay_copy, snapshots=False) as log:
            state = log.state
            replayed, seconds = log.start.replayed, log.start.seconds
    except (LogError, ValueError, OSError) as error:
        return (
            Check(
                "replay",
                Outcome.FAIL,
                f"this code cannot open the log: {error}. A log holding a change this"
                " code does not know is refused, so this code is older than the log's"
                " rollback floor, or the log is damaged",
            ),
            None,
        )
    said = f"replayed all {replayed} events in {round(seconds * 1000)} ms"
    return Check("replay", Outcome.PASS, said), state


def check_snapshot_of(copied: Copied) -> Check:
    """The snapshot beside the log against a full replay of the copy."""
    if copied.held is None:
        said = copied.held_refusal or "no snapshot"
        return Check(
            "snapshot",
            Outcome.PASS,
            f"{said}. A start replays the whole log, which is always correct",
        )
    try:
        agrees, said = check_snapshot(copied.held, copied.log, through=copied.through)
    except (LogError, ValueError, OSError) as error:
        return Check("snapshot", Outcome.FAIL, f"the snapshot check failed: {error}")
    if said.startswith("the snapshot is not used"):
        said += ". A start replays the whole log, which is always correct"
    version = f"this code's state version is {statesnapshot.STATE_VERSION[:16]}"
    return Check(
        "snapshot", Outcome.PASS if agrees else Outcome.FAIL, f"{said}, {version}"
    )


def check_start(paths: ProjectPaths) -> tuple[Check, State | None]:
    """The daemon's own start check on the copies, its words captured."""
    seen: list[State] = []
    words = io.StringIO()
    try:
        with contextlib.redirect_stderr(words):
            code = daemon.run(
                paths, check_only=True, observe=lambda log: seen.append(log.state)
            )
    except Exception as error:  # noqa: BLE001 - every refusal is reported, not raised
        return Check("start", Outcome.FAIL, f"the start check raised {error!r}"), None
    said = " ".join(line.strip() for line in words.getvalue().splitlines() if line)
    if code != 0:
        return Check("start", Outcome.FAIL, said or f"exit code {code}"), None
    return Check("start", Outcome.PASS, said), seen[-1] if seen else None


def held_lines(state: State) -> Check:
    """The lines the state holds, hibernates or retires."""
    held = sorted(
        (line_id, line.held_by, line.hold_cause)
        for line_id, line in state.lines.items()
        if line.held_by is not None and line_id not in state.retired_lines
    )
    hibernated = sorted(
        line_id
        for line_id, line in state.lines.items()
        if line.hibernated and line_id not in state.retired_lines
    )
    parts = [
        f"{line_id} held by question {question}, cause"
        f" {cause.value if cause is not None else 'not recorded'}"
        for line_id, question, cause in held[:MAX_LISTED]
    ]
    parts += [f"{line_id} hibernated" for line_id in hibernated[:MAX_LISTED]]
    if not parts:
        return Check("held", Outcome.PASS, "no line is held or hibernated")
    return Check(
        "held",
        Outcome.WARN,
        "; ".join(parts)
        + ". These stay so after the upgrade. Release a held line with dromon release"
        " LINE, or by answering its question, once its cause is understood, and"
        " awaken a hibernated one with dromon awaken LINE",
    )


@dataclass(frozen=True, slots=True)
class FloorChange:
    """One one-way change. ``floor`` is the first commit that opens it, from the
    table, or the first commit that wrote it, for a fragment not yet released."""

    change: str
    floor: str | None


def table_changes(text: str) -> list[FloorChange]:
    """The rows of the rollback floor table, with the floor commit of each."""
    lines = text.splitlines()
    try:
        start = lines.index(FLOOR_HEADER) + 2
    except ValueError:
        return []
    found: list[FloorChange] = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 4:
            continue
        floors = COMMIT_IN_CELL.findall(cells[2])
        found.append(FloorChange(cells[0], floors[-1] if floors else None))
    return found


def fragment_entries(text: str) -> list[tuple[str, str, str]]:
    """Each entry of a floor fragment, as its change, search text and path."""
    entries: list[tuple[str, str, str]] = []
    for block in text.strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
        if "Change" in fields and "Search" in fields:
            path = fields.get("Path", DEFAULT_FRAGMENT_PATH)
            entries.append((fields["Change"], fields["Search"], path))
    return entries


def _git(
    repository: Path, arguments: Sequence[str]
) -> subprocess.CompletedProcess[str]:
    """One git call in ``repository``, with no configuration of the operator's, no
    hook and no prompt, bounded in time. Started through ``dromon.spawn``, as every
    child is, so it shares the fork lock and the start bound. A start that stalls
    raises ``spawn.StartStalled``, an ``OSError``, which the caller handles."""
    argv = ["git", *commitruns.GIT_CONFIG, *arguments]
    return spawn.run(
        argv,
        cwd=repository,
        env=commitruns.git_environment(os.environ),
        capture_output=True,
        text=True,
        timeout=GIT_SECONDS,
        check=False,
    )


def _commit(repository: Path, name: str) -> str | None:
    found = _git(repository, ["rev-parse", "--verify", "--quiet", f"{name}^{{commit}}"])
    return found.stdout.strip() if found.returncode == 0 else None


def _contains(repository: Path, ancestor: str, descendant: str) -> bool | None:
    """Whether ``descendant`` contains ``ancestor``, or ``None`` when git cannot
    say, such as for a commit a shallow clone lacks."""
    found = _git(repository, ["merge-base", "--is-ancestor", ancestor, descendant])
    return {0: True, 1: False}.get(found.returncode)


def fragment_changes(repository: Path) -> list[FloorChange]:
    """The changes of the floor fragments, each with the oldest commit that changed
    how often its search text occurs in its path, as ``assemble_floors.py`` finds it."""
    directory = repository / FLOOR_FRAGMENTS
    if not directory.is_dir():
        return []
    found: list[FloorChange] = []
    for path in sorted(directory.iterdir()):
        if path.name == "README.md" or FRAGMENT_NAME.fullmatch(path.name) is None:
            continue
        for change, search, where in fragment_entries(path.read_text(encoding="utf-8")):
            log = _git(
                repository,
                ["log", "--reverse", "--format=%H", "-S", search, "--", where],
            )
            first = (
                log.stdout.split()[0]
                if log.returncode == 0 and log.stdout.split()
                else None
            )
            found.append(FloorChange(change, first))
    return found


def placed_changes(repository: Path, old: str) -> tuple[list[str], list[str]]:
    """The one-way changes of the table and the fragments whose floor the commit
    ``old`` does not contain, and those whose floor git cannot place, each by the
    name the table or fragment gives. Raises ``OSError`` or
    ``subprocess.TimeoutExpired`` when git cannot be asked."""
    changes = table_changes((repository / FLOOR_TABLE).read_text(encoding="utf-8"))
    changes += fragment_changes(repository)
    newer: list[str] = []
    unknown: list[str] = []
    for change in changes:
        held = (
            None if change.floor is None else _contains(repository, change.floor, old)
        )
        if held is None:
            unknown.append(change.change)
        elif not held:
            newer.append(change.change)
    return newer, unknown


def check_floor(repository: Path | None, from_commit: str | None) -> Check:
    """The one-way changes whose floor the old revision ``from_commit`` lacks."""
    if from_commit is None:
        return Check(
            "floor", Outcome.SKIP, "no --from-commit, so the floor is not read"
        )
    if repository is None or not (repository / FLOOR_TABLE).is_file():
        return Check(
            "floor",
            Outcome.WARN,
            "the installed code's repository is not found, so the floor is unknown."
            " Name it with --repository",
        )
    try:
        old = _commit(repository, from_commit)
        if old is None:
            return Check("floor", Outcome.FAIL, f"{from_commit} is not a commit here")
        head = _commit(repository, "HEAD")
        newer, unknown = placed_changes(repository, old)
        behind = head is not None and _contains(repository, old, head) is False
    except (OSError, subprocess.TimeoutExpired) as error:
        return Check("floor", Outcome.FAIL, f"git could not be asked: {error}")
    parts: list[str] = []
    if behind:
        parts.append(f"{from_commit} is not an ancestor of the installed code")
    if newer:
        parts.append(
            f"{len(newer)} one-way changes are newer than {from_commit}, so once this"
            " code writes any of them the old revision cannot open the log, and a"
            " rollback needs the copy taken before the upgrade: "
            + "; ".join(newer[:MAX_LISTED])
        )
    if unknown:
        parts.append(
            f"{len(unknown)} changes could not be placed: "
            + "; ".join(unknown[:MAX_LISTED])
        )
    if not parts:
        return Check(
            "floor",
            Outcome.PASS,
            f"every one-way change is contained in {from_commit}, so it can open a log"
            " this code writes",
        )
    return Check("floor", Outcome.WARN, ". ".join(parts))


def installed_repository() -> Path | None:
    """The repository of the code installed now, where it runs from a checkout."""
    candidate = Path(__file__).resolve().parents[2]
    return candidate if (candidate / ".git").exists() else None


SCRATCH_PREFIX: Final = "dromon-upgrade-"
SCRATCH_LOCK: Final = "lock"
UNLOCKED_STALE_SECONDS: Final = 3600.0
"""How old a scratch directory without a lock file must be before a sweep removes
it. A run makes its lock file at once, so only a directory of code before the sweep,
or one whose run was killed between the two steps, lacks one."""
MAX_SWEPT: Final = 64
"""The most entries of the temporary directory a sweep removes in one run."""


def _lock_scratch(scratch: Path) -> int | None:
    """Make and lock the scratch directory's lock file, held until the run closes
    it. The kernel releases the lock when the process ends in any way, ``kill -9``
    included, so a sweep that can take it knows no run uses the directory. ``None``
    where it cannot be made, which leaves the directory to the age rule."""
    try:
        descriptor = os.open(
            scratch / SCRATCH_LOCK, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600
        )
    except OSError:
        return None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    return descriptor


def _abandoned(entry: Path, now: float) -> bool:
    """Whether ``entry`` is a scratch directory of this user that no run holds."""
    try:
        found = entry.lstat()
    except OSError:
        return False
    if (
        not stat.S_ISDIR(found.st_mode)
        or found.st_uid != os.getuid()
        or stat.S_IMODE(found.st_mode) != 0o700
    ):
        return False
    try:
        descriptor = os.open(entry / SCRATCH_LOCK, os.O_RDWR | os.O_NOFOLLOW)
    except FileNotFoundError:
        return now - found.st_mtime > UNLOCKED_STALE_SECONDS
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    finally:
        os.close(descriptor)
    return True


def sweep_scratch(parent: Path) -> list[str]:
    """Remove the scratch directories of this user's earlier runs that no run
    holds, and give their names.

    **Review of 513928b, finding 3.** A run removes its scratch directory when it
    returns, but a run killed with ``kill -9`` left one, holding a full copy of the
    log, which holds message bodies and answers, until someone found it. Each run
    therefore removes, before its own, every ``dromon-upgrade-*`` entry of the same
    parent that is a directory, not a link, owned by this user with mode 0700, and
    whose lock it can take, or that has no lock file and is older than
    ``UNLOCKED_STALE_SECONDS``. A directory another run holds is left alone. Nothing
    is removed where ``shutil.rmtree`` cannot resist links, since the parent may be
    shared. **Residual.** A copy is left until the next run in the same temporary
    directory, which the procedure's document says. **Porting hazard.** ``flock`` is
    Unix's. A port uses its platform's advisory lock."""
    if not shutil.rmtree.avoids_symlink_attacks:
        return []
    try:
        with os.scandir(parent) as entries:
            names = sorted(
                entry.name
                for _, entry in zip(range(MAX_SWEPT * 64), entries, strict=False)
                if entry.name.startswith(SCRATCH_PREFIX)
            )
    except OSError:
        return []
    now = time.time()
    removed: list[str] = []
    for name in names[:MAX_SWEPT]:
        entry = parent / name
        if _abandoned(entry, now):
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                removed.append(name)
    return removed


def run_checks(
    log_file: Path,
    *,
    project_file: Path | None = None,
    root: Path | None = None,
    from_commit: str | None = None,
    repository: Path | None = None,
    scratch_parent: Path | None = None,
) -> Report:
    """Every check, on copies in a scratch directory removed before returning."""
    checks: list[Check] = []
    parent = Path(scratch_parent or tempfile.gettempdir())
    swept = sweep_scratch(parent)
    if swept:
        checks.append(
            Check(
                "scratch",
                Outcome.PASS,
                f"removed {len(swept)} scratch directories that a killed check left in"
                f" {parent}, each holding a copy of a log: " + ", ".join(swept),
            )
        )
    scratch = Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=parent))
    lock = _lock_scratch(scratch)
    try:
        state_dir = scratch / "state"
        state_dir.mkdir(mode=0o700)
        if root is None:
            root = scratch / "root"
            root.mkdir(mode=0o700)
        if not root.is_dir():
            checks.append(
                Check("copy", Outcome.FAIL, f"the root {root} is not a directory")
            )
            return Report(tuple(checks))
        copied_check, copied = copy_inputs(log_file, project_file, state_dir)
        checks.append(copied_check)
        if copied is not None:
            replay_check, state = check_replay(copied.log, scratch / "replay.sqlite")
            checks.append(replay_check)
            checks.append(check_snapshot_of(copied))
            paths = ProjectPaths(root.resolve(), state_dir, scratch / "run")
            start_check, started = check_start(paths)
            checks.append(start_check)
            final = started or state
            if final is not None:
                checks.append(held_lines(final))
        checks.append(check_floor(repository or installed_repository(), from_commit))
        return Report(tuple(checks))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
        if lock is not None:
            os.close(lock)


def render(report: Report) -> str:
    """The report as text, one check to a line, then the verdict."""
    lines = [
        f"{check.name}: {check.outcome.value}: {check.detail}"
        for check in report.checks
    ]
    lines.append(
        "upgrade-check: no check failed"
        if report.passed
        else "upgrade-check: a check failed"
    )
    return "\n".join(lines) + "\n"
