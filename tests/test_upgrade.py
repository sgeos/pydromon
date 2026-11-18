"""``dromon upgrade-check``. **Round 18, item 1.**

The fixture ``fixtures/eventlog-6567167.sql`` is a log written by the code of
``6567167``, the revision of 2026-10-31, not by this code. It was made by extracting
that revision's ``src/dromon`` and ``tests/conftest.py`` with ``git archive``,
appending that revision's own ``SCENARIO``, which then covered every event kind it
knew, through its own ``EventLog``, then a question and a ``line_held`` with no cause,
as that revision wrote them, and dumping the file with ``sqlite3``'s ``iterdump``
after its ``user_version``. Keeping it as text keeps it reviewable and lets the gate's
hygiene checks read it. The tests check what the command reports for it, that it
writes nothing outside its scratch directory, which it removes, and each refusal."""

import ast
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from dromon import cli, exits, spawn, statesnapshot, upgrade
from dromon.eventlog import EventLog
from dromon.model import (
    LineCreated,
    LineHibernated,
    LineRetired,
    LineAddress,
    OperatorAddress,
    OwnershipCause,
    OwnershipChanged,
    SeatUnbound,
)
from dromon.sealed import store_path
from dromon.upgrade import FloorChange, Outcome, Report
from state_pin import PIN_DIRECTORY, parse_pin

FIXTURE = Path(__file__).parent / "fixtures" / "eventlog-6567167.sql"
OLD_COMMIT = "6567167"
REPOSITORY = Path(__file__).resolve().parents[1]


def old_log(directory: Path) -> Path:
    """The fixture as the log file the old revision left."""
    path = directory / "events.sqlite"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(FIXTURE.read_text(encoding="utf-8"))
    finally:
        connection.close()
    path.chmod(0o600)
    return path


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def by_name(report: Report) -> dict[str, upgrade.Check]:
    return {check.name: check for check in report.checks}


def test_the_old_revision_s_log_opens_under_this_code(tmp_path: Path) -> None:
    log = old_log(tmp_path)
    before = digest(log)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    report = upgrade.run_checks(log, scratch_parent=scratch)
    checks = by_name(report)
    assert report.passed, upgrade.render(report)
    assert checks["copy"].outcome is Outcome.PASS
    assert checks["replay"].outcome is Outcome.PASS
    assert "replayed all 121 events" in checks["replay"].detail
    assert checks["snapshot"].outcome is Outcome.PASS
    assert checks["start"].outcome is Outcome.PASS
    assert "the start checks pass" in checks["start"].detail
    assert checks["floor"].outcome is Outcome.SKIP
    assert digest(log) == before, "the source is never written"
    assert list(scratch.iterdir()) == [], "the scratch directory is removed"


def test_a_line_the_old_daemon_left_held_is_named_with_its_recovery(
    tmp_path: Path,
) -> None:
    report = upgrade.run_checks(old_log(tmp_path), scratch_parent=tmp_path)
    held = by_name(report)["held"]
    assert held.outcome is Outcome.WARN
    assert "beta held by question q9, cause not recorded" in held.detail
    assert "dromon release LINE" in held.detail
    assert "alpha" not in held.detail, "a line released in the history is not held"


def test_a_snapshot_of_an_older_state_version_is_ignored_and_the_log_replayed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot written under a pinned version other than this code's, as code
    before a state change wrote it, is not used, and the start replays the log."""
    current = statesnapshot.STATE_VERSION
    older = next(
        pin.version
        for pin in (
            parse_pin(p.read_text(encoding="utf-8")) for p in PIN_DIRECTORY.iterdir()
        )
        if pin.version != current
    )
    log_path = old_log(tmp_path)
    monkeypatch.setattr(statesnapshot, "STATE_VERSION", older)
    with EventLog(log_path, snapshots=True) as log:
        log.append(LineCreated("gamma", "claude-opus-5-5", "subscription"))
    monkeypatch.setattr(statesnapshot, "STATE_VERSION", current)
    assert statesnapshot.snapshot_path(log_path).exists()
    report = upgrade.run_checks(log_path, scratch_parent=tmp_path)
    checks = by_name(report)
    assert report.passed, upgrade.render(report)
    assert "not used" in checks["snapshot"].detail
    assert "replays the whole log" in checks["snapshot"].detail


def test_a_snapshot_of_this_code_is_compared_with_a_full_replay(tmp_path: Path) -> None:
    log_path = old_log(tmp_path)
    with EventLog(log_path, snapshots=True) as log:
        log.append(LineCreated("gamma", "claude-opus-5-5", "subscription"))
    report = upgrade.run_checks(log_path, scratch_parent=tmp_path)
    snapshot = by_name(report)["snapshot"]
    assert snapshot.outcome is Outcome.PASS
    assert "agrees with a full replay" in snapshot.detail
    assert statesnapshot.STATE_VERSION[:16] in snapshot.detail


def test_a_log_holding_a_change_this_code_does_not_know_fails(tmp_path: Path) -> None:
    """What a log past this code's floor looks like, a kind it does not know."""
    log_path = old_log(tmp_path)
    connection = sqlite3.connect(log_path)
    try:
        connection.execute(
            "INSERT INTO events (recorded_at, payload) VALUES (?, ?)",
            ("2026-11-17T00:00:00Z", json.dumps({"kind": "from_the_future"})),
        )
        connection.commit()
    finally:
        connection.close()
    report = upgrade.run_checks(log_path, scratch_parent=tmp_path)
    checks = by_name(report)
    assert not report.passed
    assert checks["replay"].outcome is Outcome.FAIL
    assert "rollback floor" in checks["replay"].detail
    assert checks["start"].outcome is Outcome.FAIL
    assert "held" not in checks, "no state, so no line can be named"


def test_a_project_file_this_code_refuses_fails_the_start_alone(
    tmp_path: Path,
) -> None:
    project = tmp_path / "dromon.toml"
    project.write_text("version = 1\n[lines.alpha]\nmodel = 'no-such-model'\n")
    report = upgrade.run_checks(
        old_log(tmp_path), project_file=project, scratch_parent=tmp_path
    )
    checks = by_name(report)
    assert checks["replay"].outcome is Outcome.PASS
    assert checks["start"].outcome is Outcome.FAIL
    assert "dromond:" in checks["start"].detail
    assert not report.passed


def test_the_given_root_is_only_read(tmp_path: Path) -> None:
    root = tmp_path / "root"
    (root / "tmp" / "worktrees" / "gamma").mkdir(parents=True)
    project = tmp_path / "dromon.toml"
    project.write_text(
        "version = 1\n[lines.gamma]\nmodel = 'claude-opus-5-5'\n"
        "workdir = 'tmp/worktrees/gamma'\n"
    )
    before = sorted(
        (str(p.relative_to(root)), p.stat().st_mtime_ns) for p in root.rglob("*")
    )
    report = upgrade.run_checks(
        old_log(tmp_path), project_file=project, root=root, scratch_parent=tmp_path
    )
    assert by_name(report)["start"].outcome is Outcome.PASS, upgrade.render(report)
    after = sorted(
        (str(p.relative_to(root)), p.stat().st_mtime_ns) for p in root.rglob("*")
    )
    assert after == before


@pytest.mark.parametrize("missing", ["log", "project", "root"])
def test_an_input_that_is_not_there_fails_the_copy(
    tmp_path: Path, missing: str
) -> None:
    log_path = old_log(tmp_path)
    report = upgrade.run_checks(
        tmp_path / "absent.sqlite" if missing == "log" else log_path,
        project_file=tmp_path / "absent.toml" if missing == "project" else None,
        root=tmp_path / "absent" if missing == "root" else None,
        scratch_parent=tmp_path,
    )
    assert by_name(report)["copy"].outcome is Outcome.FAIL
    assert not report.passed


# The rollback floor.

TABLE = """# Event Log

| Change | First commit | Floor on `main` | Branch |
|---|---|---|---|
| `old_kind` | `{first}` | `{first}` | `feat/old`, #1 |
| `new_kind` | `{second}` | `{second}` | `feat/new`, #2 |

After the table.
"""


def git(repository: Path, *arguments: str) -> str:
    argv = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments]
    return subprocess.run(  # noqa: S603
        argv,  # noqa: S607
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def floor_repository(tmp_path: Path) -> tuple[Path, str, str, str]:
    """A repository of three commits, whose table gives the first two as floors and
    whose fragment's search text the third adds."""
    repository = tmp_path / "repo"
    (repository / "src" / "dromon").mkdir(parents=True)
    (repository / "docs" / "protocol" / "floors").mkdir(parents=True)
    git(repository, "init", "--quiet")
    commits: list[str] = []
    for text in ("one", "two"):
        (repository / "src" / "dromon" / "codec.py").write_text(f"{text}\n")
        git(repository, "add", "-A")
        git(repository, "commit", "--quiet", "-m", text)
        commits.append(git(repository, "rev-parse", "HEAD"))
    (repository / "src" / "dromon" / "codec.py").write_text("two\nthird_field\n")
    (repository / "docs" / "protocol" / "floors" / "feat--third.md").write_text(
        "Change: `third_field`\nSearch: third_field\n"
    )
    (repository / "docs" / "protocol" / "floors" / "README.md").write_text("x\n")
    (repository / "docs" / "protocol" / "EVENT_LOG.md").write_text(
        TABLE.format(first=commits[0][:7], second=commits[1][:7])
    )
    git(repository, "add", "-A")
    git(repository, "commit", "--quiet", "-m", "three")
    return repository, commits[0], commits[1], git(repository, "rev-parse", "HEAD")


def test_the_floor_names_each_change_the_old_revision_cannot_read(
    tmp_path: Path,
) -> None:
    repository, first, second, third = floor_repository(tmp_path)
    oldest = upgrade.check_floor(repository, first)
    assert oldest.outcome is Outcome.WARN
    assert "2 one-way changes are newer" in oldest.detail
    assert "`new_kind`" in oldest.detail and "`third_field`" in oldest.detail
    assert "`old_kind`" not in oldest.detail
    middle = upgrade.check_floor(repository, second)
    assert "1 one-way changes are newer" in middle.detail
    assert "`new_kind`" not in middle.detail
    newest = upgrade.check_floor(repository, third)
    assert newest.outcome is Outcome.PASS


def test_a_floor_the_repository_lacks_is_named_as_unplaced(tmp_path: Path) -> None:
    repository, first, second, _ = floor_repository(tmp_path)
    table = repository / "docs" / "protocol" / "EVENT_LOG.md"
    table.write_text(TABLE.format(first="abcdef1", second=second[:7]))
    check = upgrade.check_floor(repository, first)
    assert "1 changes could not be placed: `old_kind`" in check.detail


def test_the_floor_refuses_an_unknown_commit_and_skips_without_one(
    tmp_path: Path,
) -> None:
    repository, _, _, _ = floor_repository(tmp_path)
    assert upgrade.check_floor(repository, "0" * 40).outcome is Outcome.FAIL
    assert upgrade.check_floor(repository, None).outcome is Outcome.SKIP
    assert upgrade.check_floor(None, "HEAD").outcome is Outcome.WARN
    assert upgrade.check_floor(tmp_path, "HEAD").outcome is Outcome.WARN


@pytest.mark.parametrize("failing", ["first", "last"])
def test_a_git_that_cannot_be_asked_fails_the_floor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """Every git call of the floor check, the last, whether the old revision is an
    ancestor of the installed code, among them, fails the check rather than ending
    the command, as a stalled start or a timeout would. The count of calls is taken
    from a run that succeeds, so the test follows the check's own calls."""
    repository, first, _, _ = floor_repository(tmp_path)
    real = spawn.run
    calls: list[int] = []

    def counted(*arguments: Any, **keywords: Any) -> Any:  # noqa: ANN401
        calls.append(1)
        return real(*arguments, **keywords)

    monkeypatch.setattr(spawn, "run", counted)
    assert upgrade.check_floor(repository, first).outcome is Outcome.WARN
    target = 1 if failing == "first" else len(calls)
    calls.clear()

    def stalls(*arguments: Any, **keywords: Any) -> Any:  # noqa: ANN401
        calls.append(1)
        if len(calls) == target:
            raise spawn.StartStalled("git did not start within the bound")
        return real(*arguments, **keywords)

    monkeypatch.setattr(spawn, "run", stalls)
    check = upgrade.check_floor(repository, first)
    assert check.outcome is Outcome.FAIL
    assert "git could not be asked: git did not start" in check.detail
    assert len(calls) == target


def test_the_table_and_fragments_are_read_in_their_forms() -> None:
    rows = upgrade.table_changes(
        TABLE.format(first="1111111", second="2222222")
        + "| `ignored` | `3333333` | `3333333` | x |\n"
    )
    assert rows == [
        FloorChange("`old_kind`", "1111111"),
        FloorChange("`new_kind`", "2222222"),
    ]
    assert upgrade.table_changes("no table\n") == []
    assert upgrade.fragment_entries(
        "Change: `a`\nSearch: x\n\nChange: `b`\nSearch: y\nPath: src/z.py\n\nNoise\n"
    ) == [("`a`", "x", "src/dromon/codec.py"), ("`b`", "y", "src/z.py")]


def test_this_repository_s_floor_from_the_old_revision(tmp_path: Path) -> None:
    """The real table. The first change newer than ``6567167``, #113, is
    ``peer_opened``, whose floor is #114, and the leases and runs of #108 are older."""
    if upgrade._commit(REPOSITORY, OLD_COMMIT) is None:  # pyright: ignore[reportPrivateUsage]
        pytest.skip(f"{OLD_COMMIT} is not in this clone, as in a shallow one")
    check = upgrade.check_floor(REPOSITORY, OLD_COMMIT)
    assert check.outcome is Outcome.WARN
    assert "`peer_opened`" in check.detail
    assert "`lease_acquired`" not in check.detail
    assert "could not be placed" not in check.detail


def codec_surface(codec: str, model: str) -> dict[str, frozenset[str]]:
    """Each event kind a codec's source reads, with its fields, and each value of
    an enumeration it reads into a field, written ``field=value``. Read from the
    source, so a revision's surface is found without importing its code. The fields
    are ``KIND_FIELDS``, and the enumerations are the ``_member(data, FIELD, ENUM)``
    calls in each case of ``_from_wire``, whose values are the string members of
    ``ENUM`` in ``model.py``."""
    codec_tree = ast.parse(codec)
    fields: dict[str, tuple[str, ...]] = {}
    enums: dict[str, set[tuple[str, str]]] = {}
    for node in ast.walk(codec_tree):
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "KIND_FIELDS"
            and node.value is not None
        ):
            fields = ast.literal_eval(node.value)
        if isinstance(node, ast.FunctionDef) and node.name == "_from_wire":
            for case in (
                case
                for match in ast.walk(node)
                if isinstance(match, ast.Match)
                for case in match.cases
            ):
                pattern = case.pattern
                if not (
                    isinstance(pattern, ast.MatchValue)
                    and isinstance(pattern.value, ast.Constant)
                    and isinstance(pattern.value.value, str)
                ):
                    continue
                read = enums.setdefault(pattern.value.value, set())
                for call in (
                    call
                    for statement in case.body
                    for call in ast.walk(statement)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "_member"
                    and len(call.args) == 3
                ):
                    field, enum = call.args[1], call.args[2]
                    if isinstance(field, ast.Constant) and isinstance(enum, ast.Name):
                        read.add((str(field.value), enum.id))
    values: dict[str, set[str]] = {}
    for node in ast.walk(ast.parse(model)):
        if isinstance(node, ast.ClassDef):
            values[node.name] = {
                statement.value.value
                for statement in node.body
                if isinstance(statement, ast.Assign)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            }
    return {
        kind: frozenset(names)
        | frozenset(
            f"{field}={value}"
            for field, enum in enums.get(kind, set())
            for value in values.get(enum, set())
        )
        for kind, names in fields.items()
    }


def test_each_kind_the_codec_reads_anew_has_a_newer_floor_change() -> None:
    """Review of 513928b, finding 1. A floor fragment whose search text was older
    than its change placed it as older than ``6567167``, so the expiry causes of
    ``lease_released`` were not listed. This compares the codec of that revision
    with the codec here, kinds, fields and enumeration values, and requires every
    kind that gained any of them to be named by a change the floor check lists as
    newer. **Residual.** A kind that gained two one-way changes passes when only one
    is placed correctly, and values read outside ``_from_wire``'s cases, such as in
    a helper, are not compared."""
    if upgrade._commit(REPOSITORY, OLD_COMMIT) is None:  # pyright: ignore[reportPrivateUsage]
        pytest.skip(f"{OLD_COMMIT} is not in this clone, as in a shallow one")

    def source(revision: str | None, name: str) -> str:
        if revision is None:
            return (REPOSITORY / "src" / "dromon" / name).read_text(encoding="utf-8")
        return subprocess.run(  # noqa: S603, a fixed git command of the test's own
            ["git", "show", f"{revision}:src/dromon/{name}"],  # noqa: S607
            cwd=REPOSITORY,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout

    old = codec_surface(source(OLD_COMMIT, "codec.py"), source(OLD_COMMIT, "model.py"))
    now = codec_surface(source(None, "codec.py"), source(None, "model.py"))
    grown = {
        kind: sorted(surface - old.get(kind, frozenset()))
        for kind, surface in now.items()
        if surface - old.get(kind, frozenset())
    }
    assert grown, "the codec has not changed since the old revision"
    assert {"peer_opened", "lease_released"} <= set(grown)
    old_commit = upgrade._commit(REPOSITORY, OLD_COMMIT)  # pyright: ignore[reportPrivateUsage]
    assert old_commit is not None
    newer, unknown = upgrade.placed_changes(REPOSITORY, old_commit)
    assert unknown == []
    unnamed = {
        kind: added
        for kind, added in grown.items()
        if not any(f"`{kind}`" in name or f"`{kind}." in name for name in newer)
    }
    assert unnamed == {}, f"no newer floor change names these kinds: {unnamed}"


# Review of 513928b, findings 2, 3 and 5.


def inventory(directory: Path) -> dict[str, str]:
    """Each entry of ``directory`` with the digest of a file's bytes, so a file made,
    removed or written beside the input shows."""
    return {
        entry.name: digest(entry) if entry.is_file() else "directory"
        for entry in sorted(directory.iterdir())
    }


@contextlib.contextmanager
def read_only(directory: Path, closed: bool) -> Generator[None]:
    """``directory`` with its write permission taken, where ``closed``."""
    if closed:
        directory.chmod(0o500)
    try:
        yield
    finally:
        directory.chmod(0o700)


def wal_log(directory: Path) -> Path:
    """The fixture written in write-ahead mode and copied with its ``-wal`` file
    while its writer is open, so the events lie in the ``-wal`` file, not yet
    checkpointed, as a foreign copy of a live log in that mode can leave them."""
    making = directory / "making"
    making.mkdir()
    source = making / "events.sqlite"
    connection = sqlite3.connect(source, isolation_level=None)
    try:
        assert connection.execute("PRAGMA journal_mode = WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint = 0")
        connection.executescript(FIXTURE.read_text(encoding="utf-8"))
        given = directory / "given"
        given.mkdir()
        for name in ("events.sqlite", "events.sqlite-wal"):
            shutil.copyfile(making / name, given / name)
    finally:
        connection.close()
    assert (given / "events.sqlite-wal").stat().st_size > 0
    return given / "events.sqlite"


@pytest.mark.parametrize("closed", [False, True], ids=["writable", "read-only"])
def test_a_log_in_write_ahead_mode_is_read_without_writing_beside_it(
    tmp_path: Path, closed: bool
) -> None:
    """Finding 2. SQLite made ``-shm`` and ``-wal`` beside such a log, and could not
    open it at all in a directory it may not write."""
    log = wal_log(tmp_path)
    before = inventory(log.parent)
    with read_only(log.parent, closed):
        report = upgrade.run_checks(log, scratch_parent=tmp_path)
    checks = by_name(report)
    assert report.passed, upgrade.render(report)
    assert checks["copy"].outcome is Outcome.PASS
    assert "write-ahead mode" in checks["copy"].detail
    assert "replayed all 121 events" in checks["replay"].detail
    assert inventory(log.parent) == before


def hot_journal_log(directory: Path) -> Path:
    """The fixture with a hot journal beside it, as a daemon killed while it
    committed leaves. A transaction appending events spills its pages into the file
    before it commits, and the file and its journal are copied at that moment."""
    live = directory / "live"
    live.mkdir()
    source = old_log(live)
    given = directory / "given"
    given.mkdir()
    connection = sqlite3.connect(source, isolation_level=None)
    try:
        connection.execute("PRAGMA cache_size = 1")
        connection.execute("BEGIN IMMEDIATE")
        for _ in range(2000):
            connection.execute(
                "INSERT INTO events (recorded_at, payload) VALUES (?, ?)",
                ("2026-11-18 00:00:00+00:00", "not an event " * 40),
            )
        for name in ("events.sqlite", "events.sqlite-journal"):
            shutil.copyfile(live / name, given / name)
        connection.execute("ROLLBACK")
    finally:
        connection.close()
    assert (given / "events.sqlite-journal").stat().st_size > 0
    return given / "events.sqlite"


@pytest.mark.parametrize("closed", [False, True], ids=["writable", "read-only"])
def test_a_hot_journal_is_rolled_back_in_the_scratch_and_named(
    tmp_path: Path, closed: bool
) -> None:
    """Review of 513928b, finding 4. SQLite refuses to read a log with a hot journal
    read-only. The copy rolls it back in the scratch, as the next start would, and
    warns that the journal must be copied with the log."""
    log = hot_journal_log(tmp_path)
    before = inventory(log.parent)
    with read_only(log.parent, closed):
        report = upgrade.run_checks(log, scratch_parent=tmp_path)
    checks = by_name(report)
    assert report.passed, upgrade.render(report)
    assert checks["copy"].outcome is Outcome.WARN
    assert "hot journal" in checks["copy"].detail
    assert "replayed all 121 events" in checks["replay"].detail
    assert inventory(log.parent) == before


def test_a_log_that_cannot_be_read_without_a_journal_still_fails(
    tmp_path: Path,
) -> None:
    """A log that is not a database, with no journal beside it, fails the copy as
    before, rather than being taken for one with a hot journal."""
    log = tmp_path / "events.sqlite"
    log.write_bytes(b"not a database" * 100)
    copy = by_name(upgrade.run_checks(log, scratch_parent=tmp_path))["copy"]
    assert copy.outcome is Outcome.FAIL


def killed_scratch(parent: Path, age: float | None = None) -> Path:
    """A scratch directory as a run killed with ``kill -9`` leaves it, holding a copy
    of a log, aged ``age`` seconds where given."""
    left = Path(tempfile.mkdtemp(prefix=upgrade.SCRATCH_PREFIX, dir=parent))
    old_log(left)
    if age is not None:
        then = time.time() - age
        os.utime(left, (then, then))
    return left


def test_a_scratch_directory_a_killed_check_left_is_removed_by_the_next(
    tmp_path: Path,
) -> None:
    """Finding 3. A run's lock is released when its process ends in any way, so the
    next run finds the directory unheld and removes it, and says so."""
    left = killed_scratch(tmp_path)
    (left / upgrade.SCRATCH_LOCK).touch(mode=0o600)
    report = upgrade.run_checks(old_log(tmp_path), scratch_parent=tmp_path)
    assert not left.exists()
    scratch = by_name(report)["scratch"]
    assert left.name in scratch.detail and "a killed check left" in scratch.detail
    assert [entry.name for entry in tmp_path.iterdir()] == ["events.sqlite"]


def test_a_scratch_directory_a_running_check_holds_is_left_alone(
    tmp_path: Path,
) -> None:
    held = killed_scratch(tmp_path)
    lock = upgrade._lock_scratch(held)  # pyright: ignore[reportPrivateUsage]
    assert lock is not None
    try:
        assert upgrade.sweep_scratch(tmp_path) == []
        assert held.exists()
    finally:
        os.close(lock)
    assert upgrade.sweep_scratch(tmp_path) == [held.name]


def test_only_this_user_s_private_unlocked_old_scratch_directories_are_swept(
    tmp_path: Path,
) -> None:
    """A directory with no lock file is removed only once it is older than the
    bound, and a link, a file or a directory others may read is never removed."""
    recent = killed_scratch(tmp_path)
    stale = killed_scratch(tmp_path, age=upgrade.UNLOCKED_STALE_SECONDS * 2)
    target = tmp_path / "elsewhere"
    target.mkdir(mode=0o700)
    (target / "kept").touch()
    linked = tmp_path / f"{upgrade.SCRATCH_PREFIX}link"
    linked.symlink_to(target, target_is_directory=True)
    plain = tmp_path / f"{upgrade.SCRATCH_PREFIX}file"
    plain.touch()
    shared = killed_scratch(tmp_path, age=upgrade.UNLOCKED_STALE_SECONDS * 2)
    shared.chmod(0o755)
    assert upgrade.sweep_scratch(tmp_path) == [stale.name]
    assert recent.exists() and shared.exists() and plain.exists()
    assert linked.is_symlink() and (target / "kept").exists()
    assert upgrade.sweep_scratch(tmp_path / "absent") == []


def test_links_in_the_sealed_store_are_copied_as_links(tmp_path: Path) -> None:
    """Finding 5. A link in the sealed store, or the store itself as a link, is
    copied as the link, so the copy never reads what it names."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("never copied\n")
    directory = tmp_path / "directory"
    directory.mkdir()
    log = old_log(directory)
    store = store_path(log)
    store.mkdir()
    (store / "key").symlink_to(outside / "secret")
    state = tmp_path / "state-directory"
    state.mkdir()
    check, copied = upgrade.copy_inputs(log, None, state)
    assert check.outcome is Outcome.PASS and copied is not None
    assert (store_path(copied.log) / "key").is_symlink()
    filed = tmp_path / "filed"
    filed.mkdir()
    log = old_log(filed)
    store_path(log).symlink_to(outside / "secret")
    state = tmp_path / "state-file"
    state.mkdir()
    check, copied = upgrade.copy_inputs(log, None, state)
    assert check.outcome is Outcome.PASS and copied is not None
    assert store_path(copied.log).is_symlink()


def test_a_retired_line_is_not_named_as_held_or_hibernated(tmp_path: Path) -> None:
    """Finding 5. A retired line stays in the log, held or hibernated as it was,
    and is not one to recover."""
    log_path = old_log(tmp_path)
    with EventLog(log_path) as log:
        assert log.state.lines["beta"].held_by == "q9"
        before = upgrade.held_lines(log.state)
        log.append(LineHibernated("beta"))
        log.append(
            OwnershipChanged(
                "core",
                None,
                "beta",
                OwnershipCause.RELINQUISHED,
                LineAddress("beta"),
                None,
                "2026-11-18T00:00:00Z",
            )
        )
        log.append(SeatUnbound("beta", "core"))
        log.append(LineRetired("beta", OperatorAddress("ana"), "2026-11-18T00:00:00Z"))
        after = upgrade.held_lines(log.state)
        assert log.state.lines["beta"].held_by == "q9"
        assert log.state.lines["beta"].hibernated
    assert "beta" in before.detail
    assert after.outcome is Outcome.PASS
    assert "beta" not in after.detail


def test_a_snapshot_that_differs_from_the_replay_fails_the_check(
    tmp_path: Path,
) -> None:
    """Finding 5. A snapshot of this code's version whose state differs from a full
    replay, as code that applies events differently without a new state version
    writes, fails the snapshot check and the start."""
    log_path = old_log(tmp_path)
    with EventLog(log_path, snapshots=True) as log:
        log.append(LineCreated("gamma", "claude-opus-5-5", "subscription"))
    with EventLog(log_path) as log:
        state, times, seq = log.state, log.times.maps(), log.last_seq
    state.operators.add("intruder")
    held = statesnapshot.snapshot_path(log_path)
    connection = sqlite3.connect(log_path)
    try:
        rows = [
            (str(at), str(payload))
            for at, payload in connection.execute(
                "SELECT recorded_at, payload FROM events ORDER BY seq"
            )
        ]
    finally:
        connection.close()
    loaded = statesnapshot.read(held, 1, lambda number: rows[number - 1])
    statesnapshot.write(
        held,
        schema_version=1,
        seq=seq,
        first_row=loaded.first_row,
        nth_row=loaded.nth_row,
        state=state,
        times=times,
    )
    report = upgrade.run_checks(log_path, scratch_parent=tmp_path)
    checks = by_name(report)
    assert not report.passed
    assert checks["snapshot"].outcome is Outcome.FAIL
    assert "differs from a full replay" in checks["snapshot"].detail


# The command.


def test_the_command_prints_each_check_and_its_exit_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_path = old_log(tmp_path)
    assert cli.main(["upgrade-check", "--from-log", str(log_path)]) == exits.OK
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("copy: pass:")
    assert out.splitlines()[-1] == "upgrade-check: no check failed"
    assert cli.main(["upgrade-check", "--from-log", str(log_path), "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["passed"] is True
    assert [c["name"] for c in document["checks"]] == [
        "copy",
        "replay",
        "snapshot",
        "start",
        "held",
        "floor",
    ]
    missing = str(tmp_path / "absent.sqlite")
    assert cli.main(["upgrade-check", "--from-log", missing]) == exits.FAILURE
    assert "upgrade-check: a check failed" in capsys.readouterr().out


def test_the_command_needs_no_project_of_its_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It runs where the project root given to the client does not exist, since it
    reads only the copies it is given."""
    log_path = old_log(tmp_path)
    monkeypatch.chdir(tmp_path)
    code = cli.main(
        [
            "--project",
            str(tmp_path / "nowhere"),
            "upgrade-check",
            "--from-log",
            str(log_path),
        ]
    )
    assert code == exits.OK
