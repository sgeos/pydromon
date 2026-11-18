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

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from dromon import cli, exits, spawn, statesnapshot, upgrade
from dromon.eventlog import EventLog
from dromon.model import LineCreated
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
