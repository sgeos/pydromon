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
def test_a_mode_change_is_content_whatever_core_filemode_says(tmp_path: Path) -> None:
    """**Round 19 review, item 1.** With core.fileMode false git keeps the committed
    mode, and the digest kept it too, so a mode change was equal. A mode the file
    system shows is a difference, which drops a run and never keeps one wrongly."""
    work = project(tmp_path / "w")
    git(work, "config", "core.fileMode", "false")
    clean = readiness.content_digest(work)
    os.chmod(work / "run.sh", 0o700)
    assert readiness.content_digest(work) != clean
    os.chmod(work / "run.sh", 0o600)
    assert readiness.content_digest(work) == clean


# The repository's and the user's git configuration, round 19 review, item 1. Each
# setting below made the digest equal for content that differs, and the first two
# made the daemon run a program the configuration names.


def hook_marked(tmp_path: Path, name: str) -> tuple[Path, Path]:
    """The path of a program at ``tmp_path / name``, and of the mark it leaves."""
    mark = tmp_path / f"{name}.ran"
    program = tmp_path / name
    return program, mark


@pytest.mark.real_git_status
def test_a_monitor_hook_neither_hides_an_edit_nor_runs(tmp_path: Path) -> None:
    """A ``core.fsmonitor`` hook that reports no change hid an edited tracked file from
    ``git status``, and ran as the digest was read."""
    work = project(tmp_path / "w")
    hook, mark = hook_marked(tmp_path, "monitor")
    hook.write_text(f"#!/bin/sh\ntouch '{mark}'\nprintf 'token\\0'\n")
    os.chmod(hook, 0o700)
    git(work, "config", "core.fsmonitor", str(hook))
    git(work, "status", "--porcelain")
    clean = readiness.content_digest(work)
    mark.unlink(missing_ok=True)
    (work / "a.txt").write_text("hidden\n")
    assert git(work, "status", "--porcelain") == ""
    mark.unlink()
    assert readiness.content_digest(work) != clean
    assert not mark.exists()


@pytest.mark.real_git_status
def test_a_clean_filter_neither_hides_an_edit_nor_runs(tmp_path: Path) -> None:
    """A clean filter that a tracked ``.gitattributes`` names, and that maps the edited
    file back to its committed blob, hid the edit and ran as the digest was read."""
    work = project(tmp_path / "w")
    (work / "a.txt").write_text("ab\n")
    (work / ".gitattributes").write_text("a.txt filter=back\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "filtered")
    filter_program, mark = hook_marked(tmp_path, "filter")
    filter_program.write_text(f"#!/bin/sh\ntouch '{mark}'\ntr X b\n")
    os.chmod(filter_program, 0o700)
    git(work, "config", "filter.back.clean", str(filter_program))
    clean = readiness.content_digest(work)
    (work / "a.txt").write_text("aX\n")
    os.utime(work / "a.txt", ns=(1, 1))
    assert git(work, "status", "--porcelain") == ""
    mark.unlink()
    assert readiness.content_digest(work) != clean
    assert not mark.exists()


@pytest.mark.real_git_status
def test_a_line_ending_the_attributes_normalise_is_content(tmp_path: Path) -> None:
    """A working file with CRLF line endings, which the attributes normalise to the
    committed LF blob, was hashed through the filters and equal."""
    work = project(tmp_path / "w")
    (work / ".gitattributes").write_text("*.txt text eol=lf\n")
    git(work, "add", ".gitattributes")
    git(work, "commit", "-q", "-m", "line endings")
    clean = readiness.content_digest(work)
    (work / "a.txt").write_bytes(b"a.txt\r\n")
    assert readiness.content_digest(work) != clean


def submodule_project(directory: Path) -> Path:
    """``project`` with a submodule at ``sub``, committed."""
    work = project(directory)
    inner = work / "sub"
    inner.mkdir()
    git(inner, "init", "-q")
    (inner / "s.txt").write_text("s\n")
    git(inner, "add", ".")
    git(inner, "commit", "-q", "-m", "inner")
    git(work, "add", "sub")
    git(work, "commit", "-q", "-m", "submodule")
    return work


@pytest.mark.real_git_status
@pytest.mark.parametrize("ignored_by", ["diff", "config", "gitmodules"])
def test_a_moved_submodule_is_never_unchanged_whatever_ignores_it(
    tmp_path: Path, ignored_by: str
) -> None:
    """``diff.ignoreSubmodules``, ``submodule.NAME.ignore`` in configuration and the
    same key in a tracked ``.gitmodules``, which a line may write, each hid a
    submodule moved to another commit. The digest does not read a submodule's files,
    so it answers a repository holding one as unknown, before the move and after."""
    work = submodule_project(tmp_path / "w")
    if ignored_by == "diff":
        git(work, "config", "diff.ignoreSubmodules", "all")
    else:
        # The configuration's key applies once .gitmodules names the submodule.
        ignore = "\tignore = all\n" if ignored_by == "gitmodules" else ""
        (work / ".gitmodules").write_text(
            f'[submodule "sub"]\n\tpath = sub\n\turl = ./sub\n{ignore}'
        )
        git(work, "add", ".gitmodules")
        git(work, "commit", "-q", "-m", "name it")
        if ignored_by == "config":
            git(work, "config", "submodule.sub.ignore", "all")
    with pytest.raises(readiness.TreeUncovered, match="submodule"):
        readiness.content_digest(work)
    git(work / "sub", "commit", "-q", "--allow-empty", "-m", "moved")
    assert git(work, "status", "--porcelain") == ""
    with pytest.raises(readiness.TreeUncovered, match="submodule"):
        readiness.content_digest(work)


@pytest.mark.real_git_status
def test_the_user_s_and_the_system_s_configuration_are_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user configuration git cannot parse fails every read that loads it, so a
    digest read with it in place shows that it was not loaded."""
    work = project(tmp_path / "w")
    clean = readiness.content_digest(work)
    home = tmp_path / "home"
    (home / ".config" / "git").mkdir(parents=True)
    (home / ".gitconfig").write_text("[core\nnot a configuration\n")
    (home / ".config" / "git" / "config").write_text("[core\nnot either\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    (work / "a.txt").write_text("edited\n")
    edited = readiness.content_digest(work)
    assert edited != clean
    environment = readiness._read_environment()  # pyright: ignore[reportPrivateUsage]
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull


# The refusals and checks that audit's mutations left unpinned, round 19 review,
# item 3.


@pytest.mark.real_git_status
def test_a_tracked_path_holding_a_newline_is_refused(tmp_path: Path) -> None:
    """``--stdin-paths`` cannot carry a newline, so such a path is never hashed as
    another one, committed or edited."""
    work = project(tmp_path / "w")
    (work / "two\nlines.txt").write_text("one\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "newline")
    with pytest.raises(readiness.TreeUncovered, match="newline"):
        readiness.content_digest(work)
    (work / "two\nlines.txt").write_text("two\n")
    with pytest.raises(readiness.TreeUncovered, match="newline"):
        readiness.content_digest(work)


@pytest.mark.real_git_status
def test_head_moving_while_the_content_is_read_leaves_it_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit made between the reads would mix two trees, so it is not answered."""
    work = project(tmp_path / "w")
    (work / "a.txt").write_text("edited\n")
    real = readiness._run_git_read  # pyright: ignore[reportPrivateUsage]

    def committing(
        top: Path, arguments: tuple[str, ...], stdin: bytes | None, limit: int
    ) -> bytes:
        if arguments[0] == "hash-object":
            git(work, "commit", "-q", "--allow-empty", "-m", "meanwhile")
        return real(top, arguments, stdin, limit)

    monkeypatch.setattr(readiness, "_run_git_read", committing)
    with pytest.raises(readiness.ReadinessUnknown, match="HEAD moved"):
        readiness.content_digest(work)


# The bounded read, round 19 review, item 2.


def test_a_listing_past_its_bound_is_refused_while_it_is_read() -> None:
    """The writer never closes the pipe, so a reader that collected the whole output
    before counting would wait for its deadline and answer a timeout instead."""
    reading, writing = os.pipe()
    try:
        os.write(writing, b"one\0two\0three\0")
        with (
            os.fdopen(reading, "rb", buffering=0) as stream,
            pytest.raises(readiness.TreeUncovered, match="more than 2"),
        ):
            readiness._read_records(stream, 2, 30.0, "ls-tree")  # pyright: ignore[reportPrivateUsage]
    finally:
        os.close(writing)


def test_a_listing_that_does_not_end_is_answered_as_a_timeout() -> None:
    reading, writing = os.pipe()
    try:
        os.write(writing, b"one\0")
        with (
            os.fdopen(reading, "rb", buffering=0) as stream,
            pytest.raises(readiness.ReadinessUnknown) as raised,
        ):
            readiness._read_records(stream, 2, 0.2, "ls-tree")  # pyright: ignore[reportPrivateUsage]
        assert raised.value.cause is readiness.ReadFailure.TIMED_OUT
    finally:
        os.close(writing)


def test_a_listing_within_its_bound_is_read_whole() -> None:
    reading, writing = os.pipe()
    os.write(writing, b"one\0two\0")
    os.close(writing)
    with os.fdopen(reading, "rb", buffering=0) as stream:
        found = readiness._read_records(stream, 2, 30.0, "ls-tree")  # pyright: ignore[reportPrivateUsage]
    assert found == b"one\0two\0"


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
    monkeypatch.setattr(readiness, "MAX_CONTENT_ENTRIES", 2)
    with pytest.raises(readiness.TreeUncovered, match="more than 2"):
        readiness.content_digest(work)
    monkeypatch.setattr(readiness, "MAX_CONTENT_ENTRIES", 200)
    monkeypatch.setattr(readiness, "MAX_LISTING_BYTES", 8)
    with pytest.raises(readiness.TreeUncovered, match="more than 8 bytes"):
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
