"""The stale brief flag with the worktree's uncommitted state, on every turn of a
kept session. **Review of 49236ad, item 4, required by the operator.** The flag read
only ``HEAD``, so a brief set before uncommitted work was discarded or added was
presented as current, and a kept session, which is led with its brief only when its
engine session is new, never saw the flag on an ordinary turn. A brief now keeps the
digest of the worktree's ``git status`` entries, and each turn of a kept session is
headed by the flag while the commit or that digest differs, until the line sets the
brief again. Each test uses a real git worktree."""

import asyncio
import hashlib
import os
import shutil
import subprocess
from collections.abc import Generator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import readiness
from dromon.driver import Strategy
from dromon.engine import TurnReport
from dromon.eventlog import EventLog
from dromon.lineconfig import DEFAULT_CONFIG
from dromon.limits import Limits
from dromon.model import BriefSet, InvalidField, LineAddress, OperatorAddress
from dromon.scheduler import SET_BRIEF_TOOL_NAME, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
WORKTREE = "work"
AT = "2026-11-05T05:00:00Z"
CLEAN = hashlib.sha256(b"").hexdigest()


def git(directory: Path, *arguments: str) -> str:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    done = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "git",
            "-C",
            str(directory),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "commit.gpgsign=false",
            *arguments,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return done.stdout.strip()


def repository(directory: Path) -> Path:
    directory.mkdir(parents=True)
    git(directory, "init", "-q")
    (directory / "kept.txt").write_text("kept\n")
    git(directory, "add", "kept.txt")
    git(directory, "commit", "-q", "-m", "first")
    return directory


# The digest.


def test_a_clean_worktree_has_the_digest_of_no_entries(tmp_path: Path) -> None:
    assert readiness.dirty_digest(repository(tmp_path / "w")) == (CLEAN, 0)


def test_the_digest_follows_which_paths_are_dirty_and_not_their_contents(
    tmp_path: Path,
) -> None:
    worktree = repository(tmp_path / "w")
    (worktree / "new.txt").write_text("one\n")
    added = readiness.dirty_digest(worktree)
    assert added[1] == 1 and added[0] != CLEAN
    (worktree / "new.txt").write_text("two\n")
    assert readiness.dirty_digest(worktree) == added
    (worktree / "kept.txt").write_text("changed\n")
    both = readiness.dirty_digest(worktree)
    assert both[1] == 2 and both[0] not in (CLEAN, added[0])
    git(worktree, "checkout", "-q", "--", "kept.txt")
    (worktree / "new.txt").unlink()
    assert readiness.dirty_digest(worktree) == (CLEAN, 0)


def test_a_directory_git_cannot_read_has_no_digest(tmp_path: Path) -> None:
    with pytest.raises(readiness.ReadinessUnknown) as raised:
        readiness.dirty_digest(tmp_path / "absent")
    assert raised.value.cause is readiness.ReadFailure.FAILED


@pytest.mark.parametrize(
    ("error", "cause"),
    [
        (subprocess.TimeoutExpired("git", 1.0), readiness.ReadFailure.TIMED_OUT),
        (FileNotFoundError("git"), readiness.ReadFailure.NOT_STARTED),
        (
            UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
            readiness.ReadFailure.NOT_TEXT,
        ),
    ],
)
def test_each_failed_read_gives_its_fixed_word(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    cause: readiness.ReadFailure,
) -> None:
    worktree = repository(tmp_path / "w")

    def run(*arguments: object, **options: object) -> object:
        del arguments, options
        raise error

    monkeypatch.setattr(readiness.subprocess, "run", run)
    with pytest.raises(readiness.ReadinessUnknown) as raised:
        readiness.dirty_digest(worktree)
    assert raised.value.cause is cause
    # The word is apart from the message, and the message never supplies it.
    assert cause.value not in str(raised.value)


def test_a_brief_refuses_a_dirty_digest_that_is_not_one() -> None:
    with pytest.raises(InvalidField, match="workdir_dirty"):
        BriefSet("alpha", "task", "", (), "", "", "", AT, "not a digest")
    assert BriefSet("alpha", "task", "", (), "", "", "", AT, CLEAN).workdir_dirty


# The turns.


class Recorder:
    def __init__(self, world: "World", spec: LineSpec) -> None:
        self.world = world
        self.session_id = spec.session_id

    async def run_turn(self, prompt: str) -> TurnReport:
        self.world.prompts.append(prompt)
        return TurnReport("success", 1, "ok", (), None, None, "session-1", None, 0.0)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path.resolve() / "project"
        (self.root / ".dromon").mkdir(parents=True)
        self.worktree = repository(self.root / WORKTREE)
        self.prompts: list[str] = []
        self.answers: list[str] = []
        self.log = EventLog(tmp_path / "events.sqlite")
        self.scheduler = Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            limits=Limits(max_autonomous_turns=50, max_cost_usd=30.0),
            root=self.root,
            protected=(self.root / ".dromon",),
        )
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        self.scheduler.configure_line(
            "alpha", replace(DEFAULT_CONFIG, workdir=WORKTREE)
        )

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
        del tools
        return Recorder(self, spec)

    def set_brief(self) -> BriefSet:
        tools = self.scheduler.own_tools("alpha")
        tool = next(t for t in tools if t.name == SET_BRIEF_TOOL_NAME)

        async def invoke() -> dict[str, Any]:
            return await tool.handler({"task": "the task"})

        result = asyncio.run(invoke())
        assert result.get("is_error") is not True, result
        self.answers.append(str(result["content"][0]["text"]))
        brief = self.log.state.briefs["alpha"]
        assert brief is not None
        return brief

    def turn(self) -> str:
        """Run one turn of ``alpha``, woken by an operator's note, and return its
        prompt."""

        async def settle() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            await asyncio.sleep(0)
            self.scheduler.send(ANA, LineAddress("alpha"), "go on")
            await self.scheduler.idle()
            stop.set()
            await loop

        before = len(self.prompts)
        asyncio.run(settle())
        assert len(self.prompts) == before + 1
        return self.prompts[-1]

    def notice(self, led_brief: bool = False) -> str | None:
        return asyncio.run(
            self.scheduler._stale_brief_notice("alpha", led_brief)  # pyright: ignore[reportPrivateUsage]
        )


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    target = World(tmp_path)
    yield target
    target.log.close()


def record_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Each digest read the scheduler makes from now on, as its count and digest or
    as the error it raised, in order. **Flake of 2026-11-05.** The kept-session test
    once failed in continuous integration on Linux with Python 3.15, because a turn
    after a new file was written did not lead with the notice. Nothing is put in
    front of the notice in that test, so the notice was absent. ``dirty_state``
    turns a failed read into no comparison and keeps no reason, so the failure could
    not say whether the read at ``set_brief`` failed, the read at the turn failed,
    or git did not list the new file. The record goes into each assertion message,
    so the next failure names the read. This adds diagnosis and is not a fix, since
    the cause is still unknown."""
    reads: list[str] = []
    original = readiness.dirty_digest

    def recorded(worktree: Path) -> tuple[str, int]:
        try:
            digest = original(worktree)
        except Exception as error:
            reads.append(f"raised {type(error).__name__}: {error}")
            raise
        reads.append(f"{digest[1]} dirty, {digest[0][:12]}")
        return digest

    monkeypatch.setattr(readiness, "dirty_digest", recorded)
    return reads


def test_set_brief_stamps_the_dirty_digest_it_read(world: World) -> None:
    (world.worktree / "new.txt").write_text("one\n")
    brief = world.set_brief()
    assert brief.workdir_dirty == readiness.dirty_digest(world.worktree)[0]
    assert brief.workdir_commit == git(world.worktree, "rev-parse", "HEAD")


def test_a_brief_whose_worktree_git_cannot_read_has_no_digest(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(worktree: Path) -> tuple[str, int]:
        raise readiness.ReadinessUnknown(
            f"no git in {worktree.name}", readiness.ReadFailure.FAILED
        )

    monkeypatch.setattr(readiness, "dirty_digest", fail)
    assert world.set_brief().workdir_dirty is None
    assert world.scheduler.dirty_state("alpha") is readiness.ReadFailure.FAILED
    # The line is told once, when it sets the brief, that later turns cannot say.
    assert "for the reason failed" in world.answers[-1]
    assert "commit alone" in world.answers[-1]


def test_a_brief_whose_digest_was_read_says_nothing_of_a_failure(
    world: World,
) -> None:
    world.set_brief()
    assert "could not read" not in world.answers[-1]


def test_a_worktree_no_longer_allowed_has_its_own_word(world: World) -> None:
    shutil.rmtree(world.worktree)
    assert world.scheduler.dirty_state("alpha") is readiness.ReadFailure.NO_WORKDIR


def test_a_brief_still_current_heads_no_turn(world: World) -> None:
    world.set_brief()
    world.turn()
    assert "STALE BRIEF." not in world.turn()


def test_a_kept_session_is_told_on_each_turn_until_the_brief_is_set_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = record_reads(monkeypatch)
    assert world.set_brief().workdir_dirty == CLEAN, reads
    world.turn()
    (world.worktree / "new.txt").write_text("one\n")
    first = world.turn()
    assert first.startswith("STALE BRIEF."), (reads, first[:300])
    assert "uncommitted changes differ" in first and "1 dirty path now" in first
    assert "moved from commit" not in first
    # The engine is kept, so this turn is not led with the brief, and is told again.
    second = world.turn()
    assert second.startswith("STALE BRIEF."), (reads, second[:300])
    assert "BEGIN STANDING BRIEF DATA" not in second
    world.set_brief()
    assert "STALE BRIEF." not in world.turn(), reads


def test_discarded_work_flags_the_brief(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads = record_reads(monkeypatch)
    (world.worktree / "kept.txt").write_text("work in progress\n")
    assert world.set_brief().workdir_dirty not in (None, CLEAN), reads
    world.turn()
    git(world.worktree, "checkout", "-q", "--", "kept.txt")
    text = world.turn()
    assert text.startswith("STALE BRIEF."), (reads, text[:300])
    assert "0 dirty paths now" in text


def test_a_commit_flags_the_brief_on_each_kept_turn(world: World) -> None:
    world.set_brief()
    world.turn()
    before = git(world.worktree, "rev-parse", "HEAD")
    git(world.worktree, "commit", "-q", "--allow-empty", "-m", "second")
    after = git(world.worktree, "rev-parse", "HEAD")
    for _ in range(2):
        text = world.turn()
        assert text.startswith("STALE BRIEF.")
        assert f"moved from commit {before[:12]} to {after[:12]}" in text
        assert "uncommitted" not in text.split("\n\n")[0]


def test_a_turn_led_by_a_stale_brief_is_not_told_twice(world: World) -> None:
    world.set_brief()
    git(world.worktree, "commit", "-q", "--allow-empty", "-m", "second")
    # The first turn builds the engine, so it is led with the brief, which already
    # says the commit moved.
    text = world.turn()
    assert text.count("STALE BRIEF.") == 1
    assert "BEGIN STANDING BRIEF DATA" in text
    assert world.notice(led_brief=True) is None
    assert world.notice(led_brief=False) is not None


def test_a_led_turn_is_still_told_of_changed_uncommitted_work(world: World) -> None:
    world.set_brief()
    git(world.worktree, "commit", "-q", "--allow-empty", "-m", "second")
    (world.worktree / "new.txt").write_text("one\n")
    found = world.notice(led_brief=True)
    assert found is not None and "uncommitted changes differ" in found
    assert "moved from commit" not in found
    both = world.notice(led_brief=False)
    assert both is not None and "moved from commit" in both
    assert " and its uncommitted changes differ" in both


def test_a_fresh_session_each_turn_is_never_told(world: World) -> None:
    world.set_brief()
    world.scheduler.configure_line(
        "alpha",
        replace(DEFAULT_CONFIG, workdir=WORKTREE, fresh_session_each_turn=True),
    )
    (world.worktree / "new.txt").write_text("one\n")
    assert world.notice() is None


def test_a_line_without_a_brief_is_never_told(world: World) -> None:
    (world.worktree / "new.txt").write_text("one\n")
    assert world.notice() is None


def test_a_brief_without_a_digest_compares_the_commit_alone(world: World) -> None:
    head = git(world.worktree, "rev-parse", "HEAD")
    world.log.append(BriefSet("alpha", "the task", "", (), "", head, "", AT))
    (world.worktree / "new.txt").write_text("one\n")
    assert world.notice() is None
    git(world.worktree, "add", "new.txt")
    git(world.worktree, "commit", "-q", "-m", "second")
    found = world.notice()
    assert found is not None and "moved from commit" in found


def test_an_unreadable_worktree_now_says_the_brief_currency_is_unknown(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.set_brief()
    (world.worktree / "new.txt").write_text("one\n")
    assert world.notice() is not None

    def unread(line_id: str) -> readiness.ReadFailure:
        del line_id
        return readiness.ReadFailure.TIMED_OUT

    monkeypatch.setattr(world.scheduler, "dirty_state", unread)
    found = world.notice()
    assert found is not None and found.startswith("BRIEF CURRENCY UNKNOWN.")
    assert "for the reason timed_out" in found and "STALE BRIEF." not in found


def fail_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every later digest read fail as git does outside a repository, the
    injected failure that reproduced the flake of 2026-11-05."""

    def fail(worktree: Path) -> tuple[str, int]:
        raise readiness.ReadinessUnknown(
            f"git status failed with exit code 128 in {worktree.name}",
            readiness.ReadFailure.FAILED,
        )

    monkeypatch.setattr(readiness, "dirty_digest", fail)


def test_a_failed_read_heads_each_kept_turn_until_git_reads_again(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.set_brief()
    world.turn()
    with monkeypatch.context() as failing:
        fail_reads(failing)
        for _ in range(2):
            text = world.turn()
            assert text.startswith("BRIEF CURRENCY UNKNOWN."), text[:300]
            assert "for the reason failed" in text
            assert "exit code 128" not in text
    # The read works again and the state is as the brief recorded it.
    assert "BRIEF CURRENCY UNKNOWN." not in world.turn()


def test_a_failed_read_beside_a_moved_commit_says_both(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.set_brief()
    world.turn()
    git(world.worktree, "commit", "-q", "--allow-empty", "-m", "second")
    fail_reads(monkeypatch)
    text = world.turn()
    head = text.split("\n\n")[0]
    assert head.startswith("STALE BRIEF.") and "moved from commit" in head
    assert "for the reason failed" in head and "is unknown" in head
    assert "BRIEF CURRENCY UNKNOWN." not in text


def test_a_brief_without_a_digest_is_not_told_of_a_failed_read(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    fail_reads(monkeypatch)
    world.set_brief()
    world.turn()
    # The brief names no digest, so the read is not made and nothing is unknown
    # beyond what set_brief already said.
    assert world.notice() is None
