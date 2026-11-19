"""A queued run kept when ``HEAD`` moves to the same content, and a receipt that covers
it. **Round 19, at core's and ops' request.** The operator committed a line's
uncommitted tree exactly, which moved ``HEAD`` and changed no file a run reads, and the
line's queued gate was dropped after memory pressure had held it for hours.
``readiness.content_digest`` is the content a run tests, however it is split between
``HEAD`` and the working files. ``docs/architecture/RUNS.md`` is normative."""

import asyncio
import os
from pathlib import Path

import pytest

from dromon import readiness
from dromon import scheduler as scheduler_module
from dromon.eventlog import EventLog, read_entries
from dromon.model import (
    EventRejected,
    InvalidField,
    QueueEnd,
    RunDequeued,
    RunQueued,
    RunRetargeted,
)
from test_queued_runs import AT, Rig
from test_run_tree import Runner, git, repository

COMMIT = "a" * 40
OTHER = "b" * 40
DIGEST = "c" * 64


def project(directory: Path) -> Path:
    """A repository with several tracked files, which ignores the daemon's own files,
    so ``git status`` lists only what a test changes."""
    directory.mkdir(parents=True, exist_ok=True)
    git(directory, "init", "-q")
    (directory / ".gitignore").write_text(
        "events.sqlite*\nreports/\nmachine-status\ntmp/\n"
    )
    for name in ("a.txt", "b.txt", "d.txt", "e.txt"):
        (directory / name).write_text(f"{name}\n")
    (directory / "run.sh").write_text("#!/bin/sh\n")
    git(directory, "add", ".")
    git(directory, "commit", "-q", "-m", "first")
    return directory


def head(directory: Path) -> str:
    return git(directory, "rev-parse", "HEAD")


def retargets(rig: Rig) -> list[RunRetargeted]:
    return [
        entry.event
        for entry in read_entries(rig.root / "events.sqlite")
        if isinstance(entry.event, RunRetargeted)
    ]


def dropped(rig: Rig) -> list[RunDequeued]:
    return [
        entry.event
        for entry in read_entries(rig.root / "events.sqlite")
        if isinstance(entry.event, RunDequeued)
        and entry.event.outcome is QueueEnd.DROPPED
    ]


# The content digest.


@pytest.mark.real_git_status
def test_committing_exactly_the_uncommitted_tree_keeps_the_content(
    tmp_path: Path,
) -> None:
    """Every kind of change git status lists, an edit, a staged addition, a staged and
    an unstaged deletion and a rename, committed whole. The stamp digest moves, which
    is the defect, and the content digest does not."""
    work = project(tmp_path / "w")
    clean = readiness.content_digest(work)
    (work / "a.txt").write_text("edited\n")
    (work / "c.txt").write_text("added\n")
    git(work, "add", "c.txt")
    git(work, "rm", "-q", "b.txt")
    (work / "d.txt").unlink()
    git(work, "mv", "e.txt", "f.txt")
    (work / "notes.md").write_text("untracked, never compared\n")
    dirty = readiness.content_digest(work)
    stamps = readiness.tree_digest(work)
    assert dirty != clean
    git(work, "add", "-A", "--", ".", ":!notes.md")
    git(work, "commit", "-q", "-m", "the tree as it was")
    assert readiness.content_digest(work) == dirty
    assert readiness.tree_digest(work) != stamps


@pytest.mark.real_git_status
def test_a_tree_that_differs_by_one_file_has_another_content(tmp_path: Path) -> None:
    work = project(tmp_path / "w")
    (work / "a.txt").write_text("edited\n")
    (work / "b.txt").write_text("edited too\n")
    queued = readiness.content_digest(work)
    git(work, "commit", "-q", "-m", "only a", "--", "a.txt")
    git(work, "checkout", "-q", "--", "b.txt")
    assert readiness.content_digest(work) != queued
    # The one file restored gives the queued content again, committed or not.
    (work / "b.txt").write_text("edited too\n")
    assert readiness.content_digest(work) == queued


@pytest.mark.real_git_status
def test_the_mode_and_a_links_target_are_content(tmp_path: Path) -> None:
    work = project(tmp_path / "w")
    clean = readiness.content_digest(work)
    os.chmod(work / "run.sh", 0o700)
    executable = readiness.content_digest(work)
    assert executable != clean
    git(work, "commit", "-q", "-am", "executable")
    assert readiness.content_digest(work) == executable
    os.symlink("a.txt", work / "link")
    git(work, "add", "link")
    linked = readiness.content_digest(work)
    os.remove(work / "link")
    os.symlink("b.txt", work / "link")
    assert readiness.content_digest(work) != linked
    git(work, "commit", "-q", "-am", "link")
    assert readiness.content_digest(work) != linked


@pytest.mark.real_git_status
def test_without_trusted_modes_a_mode_change_is_not_content(tmp_path: Path) -> None:
    """With core.fileMode false git keeps the committed mode, and so does the digest."""
    work = project(tmp_path / "w")
    git(work, "config", "core.fileMode", "false")
    clean = readiness.content_digest(work)
    os.chmod(work / "run.sh", 0o700)
    assert readiness.content_digest(work) == clean


@pytest.mark.real_git_status
def test_content_that_cannot_be_told_is_refused_and_never_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    git(unborn, "init", "-q")
    with pytest.raises(readiness.TreeUncovered):
        readiness.content_digest(unborn)
    assert scheduler_module._content_or_none(unborn) is None  # pyright: ignore[reportPrivateUsage]
    work = project(tmp_path / "w")
    (work / "a.txt").write_text("edited\n")
    monkeypatch.setattr(readiness, "MAX_TREE_PATHS", 0)
    with pytest.raises(readiness.TreeUncovered):
        readiness.content_digest(work)
    monkeypatch.setattr(readiness, "MAX_TREE_PATHS", 10)
    monkeypatch.setattr(readiness, "MAX_CONTENT_ENTRIES", 2)
    with pytest.raises(readiness.TreeUncovered):
        readiness.content_digest(work)
    monkeypatch.undo()
    (work / "a.txt").unlink()
    (work / "a.txt").mkdir()
    with pytest.raises(readiness.TreeUncovered, match="neither a file nor a link"):
        readiness.content_digest(work)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(readiness.TreeUncovered):
        readiness.content_digest(outside)


def test_a_git_that_does_not_start_leaves_the_content_unknown(tmp_path: Path) -> None:
    """Not marked, so the suite's stand-in refuses to start git for the reads."""
    work = tmp_path / "w"
    work.mkdir()
    (work / ".git").mkdir()
    (work / ".git" / "HEAD").write_text(COMMIT + "\n")
    with pytest.raises(readiness.ReadinessUnknown):
        readiness.content_digest(work)
    assert scheduler_module._content_or_none(work) is None  # pyright: ignore[reportPrivateUsage]


# The queue.


@pytest.fixture
def git_rig(tmp_path: Path) -> Rig:
    root = tmp_path.resolve()
    made = Rig(root)
    project(root)
    return made


@pytest.mark.real_git_status
def test_a_queued_run_follows_a_commit_of_exactly_its_tree(git_rig: Rig) -> None:
    """Confirmed before the change, which dropped this start with the reason that the
    commit changed. It keeps its place ahead of a later start, records both commits,
    and starts on the new commit once runs are allowed."""
    rig = git_rig
    try:
        (rig.root / "a.txt").write_text("edited\n")
        first = head(rig.root)
        queued = rig.queue("alpha")
        behind = rig.queue("beta")
        assert queued.content == readiness.content_digest(rig.root)
        git(rig.root, "commit", "-q", "-am", "the operator commits the tree")
        second = head(rig.root)
        rig.advance()
        assert not dropped(rig)
        # Both starts were queued for the same tree, so both follow it.
        moved, also = retargets(rig)
        assert (moved.queue_id, moved.from_commit, moved.to_commit) == (
            queued.queue_id,
            first,
            second,
        )
        assert moved.content == queued.content and also.queue_id == behind.queue_id
        assert list(rig.log.state.queued_runs) == [queued.queue_id, behind.queue_id]
        assert rig.log.state.queued_runs[queued.queue_id].workdir_commit == second
        (note,) = [n for n in rig.notes("alpha") if "now targets" in n]
        assert first in note and second in note and "keeps its place" in note
        # A second look finds nothing to move.
        rig.advance()
        assert len(retargets(rig)) == 2
        rig.set_level("ok")
        rig.advance()
        run = next(iter(rig.log.state.runs.values())).started
        assert run.workdir_commit == second and run.content == queued.content
        rig.finish(run.run_id)
    finally:
        rig.log.close()


@pytest.mark.real_git_status
def test_a_commit_that_differs_by_one_file_still_drops_it(git_rig: Rig) -> None:
    rig = git_rig
    try:
        (rig.root / "a.txt").write_text("edited\n")
        first = head(rig.root)
        queued = rig.queue()
        (rig.root / "b.txt").write_text("one more file\n")
        git(rig.root, "commit", "-q", "-am", "more than the queued tree")
        rig.advance()
        assert not retargets(rig) and not rig.log.state.queued_runs
        (gone,) = dropped(rig)
        assert gone.queue_id == queued.queue_id
        assert f"changed from {first} to {head(rig.root)}" in gone.reason
        assert "are not as they were when it was queued" in gone.reason
    finally:
        rig.log.close()


@pytest.mark.real_git_status
def test_a_start_queued_without_its_content_is_dropped_as_before(
    git_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = git_rig
    try:

        async def unknown(line_id: str) -> None:
            del line_id

        monkeypatch.setattr(rig.scheduler, "_content_of", unknown)
        (rig.root / "a.txt").write_text("edited\n")
        queued = rig.queue()
        assert queued.content is None
        git(rig.root, "commit", "-q", "-am", "the same tree")
        rig.advance()
        (gone,) = dropped(rig)
        assert "was not recorded as it was queued" in gone.reason
    finally:
        rig.log.close()


@pytest.mark.real_git_status
def test_content_that_cannot_be_read_after_the_move_drops_it(
    git_rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = git_rig
    try:
        (rig.root / "a.txt").write_text("edited\n")
        rig.queue()
        git(rig.root, "commit", "-q", "-am", "the same tree")
        monkeypatch.setattr(readiness, "MAX_CONTENT_ENTRIES", 0)
        rig.advance()
        assert not retargets(rig)
        (gone,) = dropped(rig)
        assert "or cannot be compared now" in gone.reason
    finally:
        rig.log.close()


# The events.


def test_the_retarget_refuses_what_it_cannot_hold() -> None:
    assert RunRetargeted("q-1", None, COMMIT, DIGEST, AT).from_commit is None
    with pytest.raises(InvalidField, match="to_commit must be a full commit"):
        RunRetargeted("q-1", COMMIT, "main", DIGEST, AT)
    with pytest.raises(InvalidField, match="from_commit must be null"):
        RunRetargeted("q-1", "main", COMMIT, DIGEST, AT)
    with pytest.raises(InvalidField, match="moves to another commit"):
        RunRetargeted("q-1", COMMIT, COMMIT, DIGEST, AT)
    with pytest.raises(InvalidField, match="content must be a SHA-256"):
        RunRetargeted("q-1", COMMIT, OTHER, "short", AT)
    with pytest.raises(InvalidField, match="a commit command has none"):
        RunQueued("q-1", "a", "gate", None, COMMIT, (), "/w", None, AT, "x", DIGEST)
    with pytest.raises(InvalidField, match="content must be null"):
        RunQueued("q-1", "a", "gate", None, None, (), "/w", None, AT, "x", "short")


def test_the_state_retargets_only_the_start_it_was_queued_as(tmp_path: Path) -> None:
    rig = Rig(tmp_path.resolve())
    try:
        check_retargets(rig)
    finally:
        rig.log.close()


def check_retargets(rig: Rig) -> None:
    state = rig.log.state
    with pytest.raises(EventRejected, match="no queued run"):
        state.check(RunRetargeted("q-none", None, COMMIT, DIGEST, AT))
    queued = RunQueued(
        "q-1", "alpha", "gate", None, None, (), "/w", COMMIT, AT, "x", DIGEST
    )
    rig.log.append(queued)
    state = rig.log.state
    with pytest.raises(EventRejected, match="targets"):
        state.check(RunRetargeted("q-1", OTHER, COMMIT[:39] + "c", DIGEST, AT))
    with pytest.raises(EventRejected, match="queued for other content"):
        state.check(RunRetargeted("q-1", COMMIT, OTHER, "d" * 64, AT))
    rig.log.append(RunRetargeted("q-1", COMMIT, OTHER, DIGEST, AT))
    assert rig.log.state.queued_runs["q-1"].workdir_commit == OTHER
    commit_run = RunQueued("q-2", "beta", "gate", None, COMMIT, (), "/w", None, AT, "x")
    rig.log.append(commit_run)
    with pytest.raises(EventRejected, match="commit command"):
        rig.log.state.check(RunRetargeted("q-2", None, OTHER, DIGEST, AT))


# The receipt.


@pytest.mark.real_git_status
def test_a_receipt_covers_a_new_head_with_the_same_tree(tmp_path: Path) -> None:
    """Confirmed before the change, whose receipt said IT NO LONGER COVERS THE TREE
    after the tree it ran was committed exactly."""
    work = repository(tmp_path / "project")
    (work / "kept.txt").write_text("changed\n")
    with EventLog(work / "events.sqlite") as log:
        runner = Runner(work, log)

        async def scenario() -> None:
            run = await runner.run_once()
            assert run.content == readiness.content_digest(work)
            git(work, "commit", "-q", "-am", "the tree the run ran")
            text = await runner.call(scheduler_module.RUN_STATUS_TOOL_NAME, {})
            assert "so it covers the tree now" in text
            assert "NO LONGER COVERS" not in text
            (work / "kept.txt").write_text("changed again\n")
            text = await runner.call(scheduler_module.RUN_STATUS_TOOL_NAME, {})
            assert "IT NO LONGER COVERS THE TREE" in text
            assert "covers the tree now" not in text

        asyncio.run(scenario())


def test_a_receipt_without_content_is_worded_as_before() -> None:
    from test_run_tree import started

    run = started("d" * 64)
    assert run.content is None
    text = scheduler_module._tree_text(run, "e" * 64, "f" * 64)  # pyright: ignore[reportPrivateUsage]
    assert "IT NO LONGER COVERS THE TREE" in text
