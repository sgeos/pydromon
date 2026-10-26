"""What a line sees of itself and its daemon: ``line_status``, the budget line, repeat
and wake notes, ``read_instructions``, ``write_scratch``, the daemon's commit and the
Codex cache default."""

import asyncio
import hashlib
import json
import os
from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import __version__
from dromon.codex import default_cache_home, engine_environment
from dromon.disclosure import REFUSAL, load_terms
from dromon.driver import Strategy
from dromon.engine import CLAUDE_CODE_PROMPT_MARK, TurnReport
from dromon.eventlog import EventLog
from dromon.lineconfig import LineConfig, McpServer
from dromon.limits import Limits
from dromon import scheduler as scheduler_module
from dromon.mailbox import summary
from dromon.model import (
    LineAddress,
    LineCreated,
    LineHeld,
    MessageDelivered,
    MessageKind,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    State,
)
from dromon.scheduler import (
    MAX_SCRATCH_BYTES,
    UNRELEASED_NOTE,
    LineSpec,
    LineToolRefused,
    Scheduler,
)
from dromon.workdir import read_head_commit

HAIKU = "claude-haiku-5-5"
LUNA = "gpt-6-luna"
ANA = OperatorAddress("ana")
START = datetime(2026, 10, 26, 9, 0, 0, tzinfo=UTC)
COMMIT = "a" * 40
FULL: dict[str, object] = {"full": True}


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


class Recorder:
    def __init__(self, spec: LineSpec) -> None:
        self.spec = spec
        self.session_id = spec.session_id
        self.prompts: list[str] = []
        self.cost = 0.25

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        self.session_id = self.session_id or "session-1"
        return TurnReport(
            "success", 1, "ok", (), None, None, self.session_id, None, self.cost
        )


class World:
    def __init__(self, path: Path, root: Path) -> None:
        self.root = root
        self.clock = Clock()
        self.log = EventLog(path)
        self.engines: dict[str, Recorder] = {}
        self.scheduler = self.start()
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        self.scheduler.create_line("beta", HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
        del tools
        engine = self.engines.get(spec.line_id) or Recorder(spec)
        self.engines[spec.line_id] = engine
        return engine

    def start(self) -> Scheduler:
        self.scheduler = Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            wall_clock=self.clock,
            limits=Limits(max_autonomous_turns=5, max_cost_usd=3.0),
            root=self.root,
            protected=(self.root / ".dromon",),
            daemon_commit=COMMIT,
        )
        return self.scheduler

    def turn(self, *bodies: str, sender: OperatorAddress | LineAddress = ANA) -> str:
        """Send ``bodies`` to alpha and return the prompt of the turn they wake."""

        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            for body in bodies:
                self.scheduler.send(sender, LineAddress("alpha"), body)
            await asyncio.sleep(0)
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(go())
        return self.engines["alpha"].prompts[-1]

    def call(
        self, name: str, args: dict[str, object] | None = None
    ) -> tuple[str, bool]:
        tool = next(t for t in self.scheduler.own_tools("alpha") if t.name == name)

        async def invoke() -> dict[str, Any]:
            return await tool.handler(args or {})

        result = asyncio.run(invoke())
        return cast(str, result["content"][0]["text"]), result.get("is_error") is True


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    root = tmp_path.resolve() / "project"
    (root / ".dromon").mkdir(parents=True)
    target = World(tmp_path / "events.sqlite", root)
    yield target
    target.log.close()


# R2 to R4, line_status.


def test_line_status_shows_the_line_its_daemon_and_the_others(world: World) -> None:
    scheduler = world.scheduler
    secret_config = LineConfig(
        builtin_tools=("Read",),
        mcp_servers=(
            ("notes", McpServer("stdio", command="notes", env=(("T", "s3"),))),
        ),
    )
    scheduler.configure_line("alpha", secret_config)
    world.turn("a private body")
    scheduler.set_leases(["gate"], 600)
    scheduler.acquire_lease("beta", "gate")
    message_id, due = scheduler.schedule_wake("alpha", 120, "later")
    question = scheduler.hold("alpha", "was held for this test")
    text, error = world.call("line_status")
    assert not error
    status = json.loads(text)
    assert status["line_id"] == "alpha" and status["held_by"] == question
    assert status["autonomous_turns"] == {"used": 0, "allowed": 5}
    assert status["cost_usd"] == {"estimate": "0.2500", "limit": "3.00"}
    assert status["scheduled_wake"] == {"due": due, "message_id": message_id}
    assert status["unread"] == 1 and status["pending"] == 0
    assert (status["model"], status["strategy"]) == (HAIKU, "subscription")
    assert (status["workdir"], status["workdir_path"]) == (None, str(world.root))
    assert status["engine_session"] == "session-1"
    assert (status["daemon"]["version"], status["daemon"]["commit"]) == (
        __version__,
        COMMIT,
    )
    assert status["kept_session"] == {
        "configured": False,
        "kept": False,
        "closes_at": None,
        "background_tasks": 0,
    }
    assert status["leases"] == []
    assert status["other_lines"] == [
        {"line_id": "beta", "state": "idle", "held": False, "leases": ["gate"]}
    ]
    for secret in ("a private body", "notes", "s3", "was held for this test"):
        assert secret not in text


def test_line_status_takes_no_arguments(world: World) -> None:
    text, error = world.call("line_status", {"line": "beta"})
    assert error and "unexpected argument line" in text


# R2, the budget line.


def test_every_prompt_opens_with_the_budget(world: World) -> None:
    first = world.turn("one")
    assert first.splitlines()[0] == (
        "Autonomous turns used: 0 of 5. Estimated cost since your last release:"
        " $0.00 of $3.00. A line past either is held."
    )
    assert first.splitlines()[1] == "1 new message. 1 unread. 1 unanswered."
    world.scheduler.send(LineAddress("beta"), LineAddress("alpha"), "from beta")
    second = world.turn(sender=LineAddress("beta"))
    assert second.startswith("Autonomous turns used: 0 of 5.")
    third = world.turn("again", sender=LineAddress("beta"))
    assert third.startswith(
        "Autonomous turns used: 1 of 5. Estimated cost since your last release:"
        " $0.50 of $3.00."
    )


# R6, repeats.


def test_a_repeat_within_fifteen_minutes_is_marked(world: World) -> None:
    first = world.turn("please run the gate")
    first_id = next(
        i for i, m in world.log.state.messages.items() if m.body.startswith("please")
    )
    assert "a repeat of" not in first
    world.clock.now += timedelta(minutes=15)
    second = world.turn("please run the gate")
    assert f", a repeat of {first_id}" in second
    world.clock.now += timedelta(minutes=15, seconds=1)
    third = world.turn("please run the gate")
    second_id = list(world.log.state.messages)[-1]
    assert "a repeat of" not in third
    assert second_id not in third.split("New:")[0]


def test_only_the_same_sender_recipient_and_body_make_a_repeat(world: World) -> None:
    world.turn("hello")
    assert "a repeat" not in world.turn("hello there")
    assert "a repeat" not in world.turn("hello", sender=LineAddress("beta"))
    marked = world.turn("hello", sender=LineAddress("beta"))
    assert "a repeat of" in marked


def test_messages_from_before_a_restart_are_not_marked(world: World) -> None:
    world.turn("same")
    world.engines.clear()
    world.start()
    assert "a repeat" not in world.turn("same")


# R7, wakes.


def test_a_fired_wake_says_when_it_was_due_and_delivered(world: World) -> None:
    message_id, due = world.scheduler.schedule_wake("alpha", 60, "check the log")
    world.clock.now += timedelta(seconds=73)

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        await asyncio.sleep(0)
        await world.scheduler.idle()
        stop.set()
        await loop

    asyncio.run(go())
    prompt = world.engines["alpha"].prompts[-1]
    assert (
        f"{message_id} from alpha, note, unread, your scheduled wake, due {due},"
        " delivered 2026-10-26T09:01:13Z"
    ) in prompt
    assert world.log.state.fired_dues[message_id] == due
    world.start()
    assert world.log.state.fired_dues[message_id] == due


# R8, read_instructions.


def test_read_instructions_returns_what_the_adapter_composes(world: World) -> None:
    (world.root / "CLAUDE.md").write_text("Follow the rules.\n")
    scheduler = world.scheduler
    text, error = world.call("read_instructions", FULL)
    assert (text, error) == (
        "This line's next turn receives no instructions from Dromon.",
        False,
    )
    scheduler.configure_line("alpha", LineConfig(instructions=("CLAUDE.md",)))
    assert world.call("read_instructions", FULL) == (
        "Contents of CLAUDE.md:\n\nFollow the rules.",
        False,
    )
    scheduler.configure_line(
        "alpha", LineConfig(instructions=("CLAUDE.md",), claude_code_prompt=True)
    )
    text, _ = world.call("read_instructions", FULL)
    assert (
        text
        == f"{CLAUDE_CODE_PROMPT_MARK}\n\nContents of CLAUDE.md:\n\nFollow the rules."
    )
    scheduler.configure_line(
        "alpha", LineConfig(instructions=("CLAUDE.md",), project_settings=True)
    )
    text, _ = world.call("read_instructions", FULL)
    assert "project's own CLAUDE.md files" in text
    scheduler.configure_line("alpha", LineConfig(instructions=("MISSING.md",)))
    text, error = world.call("read_instructions", FULL)
    assert error and "cannot read the instructions MISSING.md" in text


def test_a_codex_line_reads_its_developer_instructions(world: World) -> None:
    (world.root / "AGENTS.md").write_text("Codex rules.\n")
    world.scheduler.rebind_line("alpha", LUNA, Strategy.SUBSCRIPTION)
    world.scheduler.configure_line("alpha", LineConfig(instructions=("AGENTS.md",)))
    assert world.call("read_instructions", FULL) == (
        "Contents of AGENTS.md:\n\nCodex rules.",
        False,
    )


# R5, write_scratch.


def test_write_scratch_writes_under_tmp_creating_directories(world: World) -> None:
    text, error = world.call(
        "write_scratch", {"path": "reports/round1.md", "text": "Report.\n"}
    )
    assert (text, error) == (
        "wrote tmp/reports/round1.md in your working directory",
        False,
    )
    assert (world.root / "tmp" / "reports" / "round1.md").read_text() == "Report.\n"
    world.call("write_scratch", {"path": "reports/round1.md", "text": "Again.\n"})
    assert (world.root / "tmp" / "reports" / "round1.md").read_text() == "Again.\n"
    world.scheduler.configure_line("alpha", LineConfig(workdir="tree"))
    (world.root / "tree").mkdir()
    world.call("write_scratch", {"path": "x.txt", "text": "in the tree"})
    assert (world.root / "tree" / "tmp" / "x.txt").read_text() == "in the tree"


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/etc/passwd", "relative path under tmp/"),
        ("~/notes", "relative path under tmp/"),
        ("", "relative path under tmp/"),
        ("a\x00b", "relative path under tmp/"),
        ("../escape", "may not contain .."),
        ("reports/../../escape", "may not contain .."),
    ],
)
def test_write_scratch_refuses_paths_outside_tmp(
    world: World, path: str, message: str
) -> None:
    text, error = world.call("write_scratch", {"path": path, "text": "x"})
    assert error and message in text
    assert not (world.root / "escape").exists()


def test_write_scratch_refuses_links_out_and_link_targets(
    world: World, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    scratch = world.root / "tmp"
    scratch.mkdir()
    (scratch / "out").symlink_to(outside)
    text, error = world.call("write_scratch", {"path": "out/x.txt", "text": "x"})
    assert error and "resolves outside tmp/" in text
    assert not (outside / "x.txt").exists()
    (scratch / "link.txt").symlink_to(outside / "target.txt")
    text, error = world.call("write_scratch", {"path": "link.txt", "text": "x"})
    assert error and "symbolic link" in text
    assert not (outside / "target.txt").exists()
    (scratch / "folder").mkdir()
    text, error = world.call("write_scratch", {"path": "folder", "text": "x"})
    assert error and "is a directory" in text
    scratch.rename(world.root / "old-tmp")
    (world.root / "tmp").symlink_to(outside)
    text, error = world.call("write_scratch", {"path": "x.txt", "text": "x"})
    assert error and "link elsewhere" in text


def test_write_scratch_refuses_large_or_disclosing_text_and_bad_arguments(
    world: World, tmp_path: Path
) -> None:
    text, error = world.call(
        "write_scratch", {"path": "big", "text": "x" * (MAX_SCRATCH_BYTES + 1)}
    )
    assert error and "over 262144 bytes" in text
    (tmp_path / "terms").write_text("codename\n")
    world.scheduler.set_disclosure_terms(load_terms(tmp_path / "terms"))
    text, error = world.call("write_scratch", {"path": "n", "text": "the Codename"})
    assert (text, error) == (REFUSAL, True)
    assert not (world.root / "tmp" / "n").exists()
    text, error = world.call("write_scratch", {"path": 3, "text": "x"})
    assert error and "must be text" in text
    text, error = world.call("write_scratch", {"path": "a", "text": "x", "mode": "a"})
    assert error and "unexpected argument mode" in text


def test_tools_that_need_the_project_root_say_so_without_one(tmp_path: Path) -> None:
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(log, lambda spec, tools: Recorder(spec), hold_wakes=True)
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        with pytest.raises(LineToolRefused, match="does not know its project root"):
            scheduler.write_scratch("alpha", "x", "y")
        with pytest.raises(LineToolRefused, match="does not know its project root"):
            scheduler.line_instructions("alpha")
        assert scheduler.line_status("alpha")["workdir_path"] is None


# R3, the daemon's commit.


def make_repository(base: Path) -> Path:
    git = base / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    return git


def test_the_commit_is_read_from_head_refs_and_packed_refs(tmp_path: Path) -> None:
    git = make_repository(tmp_path / "repo")
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text(f"{'b' * 40}\n")
    (tmp_path / "repo" / "src").mkdir()
    assert read_head_commit(tmp_path / "repo" / "src") == "b" * 40
    (git / "refs" / "heads" / "main").unlink()
    (git / "packed-refs").write_text(
        f"# pack-refs with: peeled\n{'c' * 40} refs/heads/main\n"
    )
    assert read_head_commit(tmp_path / "repo") == "c" * 40
    (git / "HEAD").write_text(f"{'d' * 40}\n")
    assert read_head_commit(tmp_path / "repo") == "d" * 40


def test_a_linked_worktree_reads_refs_from_the_common_directory(
    tmp_path: Path,
) -> None:
    common = make_repository(tmp_path / "primary")
    (common / "refs" / "heads" / "feature").write_text(f"{'e' * 40}\n")
    gitdir = common / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/feature\n")
    (gitdir / "commondir").write_text("../..\n")
    tree = tmp_path / "wt"
    tree.mkdir()
    (tree / ".git").write_text(f"gitdir: {gitdir}\n")
    assert read_head_commit(tree) == "e" * 40


@pytest.mark.parametrize(
    "head",
    ["ref: refs/heads/../../etc", "ref: HEAD", "garbage", "", "ref: refs/heads/gone"],
)
def test_an_unreadable_commit_is_none(tmp_path: Path, head: str) -> None:
    git = make_repository(tmp_path / "repo")
    (git / "HEAD").write_text(head)
    assert read_head_commit(tmp_path / "repo") is None


def test_outside_a_checkout_or_with_a_broken_pointer_the_commit_is_none(
    tmp_path: Path,
) -> None:
    assert read_head_commit(tmp_path) is None
    (tmp_path / "broken").mkdir()
    (tmp_path / "broken" / ".git").write_text("not a pointer\n")
    assert read_head_commit(tmp_path / "broken") is None


# R9, the Codex cache.


def test_codex_lines_get_a_writable_cache_unless_one_is_set() -> None:
    chosen = engine_environment({"PATH": "/bin"}, Strategy.SUBSCRIPTION)
    assert chosen["XDG_CACHE_HOME"] == default_cache_home()
    assert default_cache_home().startswith(os.path.join(os.path.realpath("/"), ""))
    assert f"dromon-cache-{os.getuid()}" in default_cache_home()
    import tempfile

    assert default_cache_home().startswith(tempfile.gettempdir())
    kept = engine_environment({"XDG_CACHE_HOME": "/mine"}, Strategy.SUBSCRIPTION)
    assert kept["XDG_CACHE_HOME"] == "/mine"


# Review round 1: the write walk, the path check, the repeat memory, the cache.


def test_a_link_swapped_in_mid_walk_cannot_redirect_the_write(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory is created, then replaced by a link to outside before it is
    opened, as a racing process would. The open without following refuses it."""
    outside = tmp_path / "outside"
    outside.mkdir()
    real_mkdir = os.mkdir
    swapped: list[str] = []

    def racing_mkdir(
        name: str, mode: int = 0o777, *, dir_fd: int | None = None
    ) -> None:
        real_mkdir(name, mode, dir_fd=dir_fd)
        if name == "x":
            target = world.root / "tmp" / "x"
            target.rmdir()
            target.symlink_to(outside)
            swapped.append(name)

    monkeypatch.setattr(os, "mkdir", racing_mkdir)
    text, error = world.call("write_scratch", {"path": "x/y.txt", "text": "z"})
    assert swapped == ["x"]
    assert error and "resolves outside tmp/ through a link" in text
    assert list(outside.iterdir()) == []


def test_a_file_in_the_way_of_a_directory_is_refused(world: World) -> None:
    (world.root / "tmp").mkdir()
    (world.root / "tmp" / "plain").write_text("a file")
    text, error = world.call("write_scratch", {"path": "plain/y.txt", "text": "z"})
    assert error and "crosses a file" in text
    (world.root / "tmp").rename(world.root / "old")
    (world.root / "tmp").write_text("a file")
    text, error = world.call("write_scratch", {"path": "y.txt", "text": "z"})
    assert error and "not a directory" in text


def test_the_disclosure_check_covers_the_scratch_path(
    world: World, tmp_path: Path
) -> None:
    (tmp_path / "terms").write_text("codename\n")
    world.scheduler.set_disclosure_terms(load_terms(tmp_path / "terms"))
    text, error = world.call(
        "write_scratch", {"path": "the-codename-notes.md", "text": "clean"}
    )
    assert (text, error) == (REFUSAL, True)
    assert not (world.root / "tmp").exists()


def test_the_repeat_memory_is_pruned_after_the_window(world: World) -> None:
    scheduler = world.scheduler
    for number in range(5):
        scheduler.send(ANA, LineAddress("alpha"), f"note {number}")
    recent = scheduler._recent  # pyright: ignore[reportPrivateUsage]
    order = scheduler._recent_order  # pyright: ignore[reportPrivateUsage]
    assert len(recent) == 5 and len(order) == 5
    world.clock.now += timedelta(minutes=15, seconds=1)
    scheduler.send(ANA, LineAddress("alpha"), "note 0")
    assert len(recent) == 1 and len(order) == 1
    world.turn()
    prompt = world.engines["alpha"].prompts[-1]
    assert "a repeat of" not in prompt
    assert scheduler._repeats == {}  # pyright: ignore[reportPrivateUsage]


def test_messages_to_operators_are_not_remembered(world: World) -> None:
    world.scheduler.send(LineAddress("alpha"), ANA, "a report")
    assert world.scheduler._recent == {}  # pyright: ignore[reportPrivateUsage]


def test_the_codex_cache_is_private_and_refused_when_unsafe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stat as stat_module

    from dromon.codex import ensure_cache_home
    from dromon.engine import EngineRefused

    fresh = str(tmp_path / "fresh")
    assert ensure_cache_home(fresh) == fresh
    assert stat_module.S_IMODE(os.stat(fresh).st_mode) == 0o700
    assert ensure_cache_home(fresh) == fresh
    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    with pytest.raises(EngineRefused, match="writable by its group or others"):
        ensure_cache_home(str(shared))
    (tmp_path / "link").symlink_to(tmp_path / "fresh")
    with pytest.raises(EngineRefused, match="is not a directory"):
        ensure_cache_home(str(tmp_path / "link"))
    (tmp_path / "file").write_text("x")
    with pytest.raises(EngineRefused, match="is not a directory"):
        ensure_cache_home(str(tmp_path / "file"))
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    with pytest.raises(EngineRefused, match="belongs to another user"):
        ensure_cache_home(fresh)


# Round 2, a kept engine session in line_status, and its retirement.


class KeptRecorder(Recorder):
    def __init__(self, spec: LineSpec) -> None:
        super().__init__(spec)
        self.retired = 0

    async def retire(self) -> None:
        self.retired += 1

    def kept_status(self) -> dict[str, object]:
        return {"kept": True, "seconds_to_close": 90.0, "background_tasks": 2}


def kept_engine(world: World) -> KeptRecorder:
    engine = KeptRecorder(LineSpec("alpha", HAIKU, Strategy.SUBSCRIPTION))
    world.engines["alpha"] = engine
    return engine


def test_line_status_reports_a_kept_session_and_its_deadline(world: World) -> None:
    world.scheduler.configure_line("alpha", LineConfig(keep_session=True))
    kept_engine(world)
    world.scheduler.engine_for("alpha")
    status = json.loads(world.call("line_status")[0])
    assert status["kept_session"] == {
        "configured": True,
        "kept": True,
        "closes_at": "2026-10-26T09:01:30Z",
        "background_tasks": 2,
    }


def test_discarding_a_kept_engine_retires_it(world: World) -> None:
    engine = kept_engine(world)

    async def go() -> None:
        world.scheduler.engine_for("alpha")
        world.scheduler.clear_session("alpha")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(go())
    assert engine.retired == 1


def test_discarding_an_engine_outside_a_loop_needs_no_retirement(world: World) -> None:
    engine = kept_engine(world)
    world.scheduler.engine_for("alpha")
    world.scheduler.configure_line("alpha", LineConfig(keep_session=True))
    assert engine.retired == 0


def test_stopping_the_scheduler_retires_kept_engines(world: World) -> None:
    engine = kept_engine(world)
    world.turn("hello")
    assert engine.retired == 1


# Round 2, the daemon's staleness.


def checkout(directory: Path, commit: str) -> Path:
    (directory / ".git").mkdir(parents=True, exist_ok=True)
    (directory / ".git" / "HEAD").write_text(commit + "\n")
    return directory


def test_daemon_status_says_when_the_checkout_moved(
    world: World, tmp_path: Path
) -> None:
    daemon = checkout(tmp_path / "daemon", COMMIT)
    scheduler = Scheduler(
        world.log, world.build, daemon_commit=COMMIT, daemon_checkout=daemon
    )
    status = scheduler.daemon_status()
    assert (status["stale"], status["checkout_commit"]) == (False, COMMIT)
    assert status["stale_note"] is None
    assert status["version_note"] == (
        UNRELEASED_NOTE if __version__ == "0.0.0" else None
    )
    moved = "b" * 40
    checkout(daemon, moved)
    status = scheduler.daemon_status()
    assert (status["stale"], status["checkout_commit"]) == (True, moved)
    note = cast(str, status["stale_note"])
    assert COMMIT[:12] in note and moved[:12] in note and "restarts" in note


def test_staleness_is_unknown_without_a_checkout_or_a_start_commit(
    world: World, tmp_path: Path
) -> None:
    assert world.scheduler.daemon_status()["stale"] is None
    daemon = checkout(tmp_path / "daemon", COMMIT)
    scheduler = Scheduler(world.log, world.build, daemon_checkout=daemon)
    status = scheduler.daemon_status()
    assert (status["stale"], status["checkout_commit"]) == (None, COMMIT)


# Round 2, the read_instructions summary.


def test_read_instructions_summarises_by_default(world: World) -> None:
    (world.root / "CLAUDE.md").write_text("Follow the rules.\n")
    (world.root / "MORE.md").write_text("More.\n")
    world.scheduler.configure_line(
        "alpha",
        LineConfig(instructions=("CLAUDE.md", "MORE.md"), claude_code_prompt=True),
    )
    full, error = world.call("read_instructions", FULL)
    assert not error
    text, error = world.call("read_instructions")
    assert not error
    found = json.loads(text)
    data = full.encode("utf-8")
    assert found["bytes"] == len(data)
    assert found["sha256"] == hashlib.sha256(data).hexdigest()
    assert found["sources"] == [
        {"source": CLAUDE_CODE_PROMPT_MARK, "bytes": None},
        {"source": "CLAUDE.md", "bytes": len((world.root / "CLAUDE.md").read_bytes())},
        {"source": "MORE.md", "bytes": len((world.root / "MORE.md").read_bytes())},
    ]
    assert "full set to true" in found["full"]
    assert world.call("read_instructions", {"full": False}) == (text, False)


def test_read_instructions_names_what_the_engine_loads_itself(world: World) -> None:
    world.scheduler.configure_line("alpha", LineConfig(project_settings=True))
    found = json.loads(world.call("read_instructions")[0])
    assert found["sources"] == [
        {
            "source": "the project's own CLAUDE.md files, loaded by the engine",
            "bytes": None,
        }
    ]


def test_read_instructions_refuses_bad_arguments_and_missing_files(
    world: World,
) -> None:
    text, error = world.call("read_instructions", {"full": "yes"})
    assert error and "full must be true or false" in text
    text, error = world.call("read_instructions", {"whole": True})
    assert error and "unexpected argument whole" in text
    world.scheduler.configure_line("alpha", LineConfig(instructions=("MISSING.md",)))
    text, error = world.call("read_instructions")
    assert error and "cannot read the instructions MISSING.md" in text


# Round 2, replies to hold questions.


def test_a_reply_to_a_hold_question_is_not_counted_unanswered(world: World) -> None:
    question = world.scheduler.hold("alpha", "was held for this test")
    operator = cast(OperatorAddress, world.log.state.messages[question].recipient)
    reply = world.scheduler.answer(operator, question, "resume")
    request = world.scheduler.send(ANA, LineAddress("alpha"), "a real request")
    for message_id in (reply, request):
        world.log.append(MessageDelivered(message_id))
    counts = summary(world.log.state, LineAddress("alpha"), 0)
    assert (counts.unread, counts.unanswered) == (2, 1)


def test_an_ordinary_reply_to_a_lines_question_still_counts(world: World) -> None:
    question = world.scheduler.ask("alpha", ANA, "which branch?")
    reply = world.scheduler.answer(ANA, question, "main")
    world.log.append(MessageDelivered(reply))
    assert summary(world.log.state, LineAddress("alpha"), 0).unanswered == 1


def test_hold_questions_are_part_of_the_state_and_its_copy() -> None:
    state = State()
    state.apply(OperatorRegistered("ana"))
    state.apply(LineCreated("alpha", HAIKU, "subscription"))
    state.apply(
        MessageSent(
            "q-1", LineAddress("alpha"), ANA, MessageKind.QUESTION, "held", ("resume",)
        )
    )
    state.apply(LineHeld("alpha", "q-1"))
    assert state.hold_questions == {"q-1"}
    copied = state.copy()
    state.hold_questions.add("q-2")
    assert copied.hold_questions == {"q-1"}


# Round 2, list_mail notes.


def test_list_mail_names_the_message_a_repeat_repeats(world: World) -> None:
    world.turn("please run the gate")
    first_id = list(world.log.state.messages)[-1]
    world.clock.now += timedelta(minutes=1)
    world.turn("please run the gate")
    second_id = list(world.log.state.messages)[-1]
    text, error = world.call("list_mail")
    assert not error
    entries = {line.split(" ")[0]: line for line in text.splitlines()[1:]}
    assert "repeat_of" not in entries[first_id]
    assert entries[second_id].endswith(f", repeat_of {first_id}")


def test_list_mail_gives_a_fired_wake_its_due_and_delivery_times(
    world: World,
) -> None:
    _, due = world.scheduler.schedule_wake("alpha", 60, "check the log")
    world.clock.now += timedelta(seconds=73)

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        await asyncio.sleep(0)
        await world.scheduler.idle()
        stop.set()
        await loop

    asyncio.run(go())
    text, _ = world.call("list_mail")
    assert text.splitlines()[-1].endswith(
        f", scheduled wake due {due}, delivered 2026-10-26T09:01:13Z"
    )
    world.start()
    text, _ = world.call("list_mail")
    assert text.splitlines()[-1].endswith(f", scheduled wake due {due}")


def test_list_mail_notes_stay_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scheduler_module, "MAX_REMEMBERED_NOTES", 2)
    store: dict[str, str] = {}
    for key in ("a", "b", "c"):
        scheduler_module._bounded_put(store, key, key.upper())  # pyright: ignore[reportPrivateUsage]
    assert store == {"b": "B", "c": "C"}
