"""What a line sees of itself and its daemon: ``line_status``, the budget line, repeat
and wake notes, ``read_instructions``, ``write_scratch``, the daemon's commit and the
Codex cache default."""

import asyncio
import hashlib
import json
import os
import re
from collections.abc import Generator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import __version__, disclosure
from dromon.codex import default_cache_home, engine_environment
from dromon.disclosure import load_terms
from dromon.driver import Driver, Strategy
from dromon.engine import CLAUDE_CODE_PROMPT_MARK, ClaudeCodeLine, TurnReport
from dromon.eventlog import EventLog, read_entries
from dromon.lineconfig import (
    ConfigError,
    LineConfig,
    McpServer,
    check_instructions,
    rooted_instructions,
)
from dromon.projectconfig import ConfigFileError, parse
from dromon.limits import Limits
from dromon import scheduler as scheduler_module
from dromon.mailbox import MailSummary, listing, summary
from dromon.model import (
    HARNESS,
    DISCLOSURE_CHECK_WINDOW_SECONDS,
    MAX_DISCLOSURE_CHECKS,
    AgentAddress,
    DisclosureChecked,
    HarnessAddress,
    LineAddress,
    LineCreated,
    LineHeld,
    MessageDelivered,
    Message,
    MessageKind,
    MessageRead,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
    RemoteAddress,
    RoleNoteKind,
    State,
)
from dromon.scheduler import (
    DISCLOSURE_HELD,
    MAX_LINE_STATUS_DEFAULT_BYTES,
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
        self.log_path = path
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


def test_line_status_is_compact_and_details_are_requested(world: World) -> None:
    scheduler = world.scheduler
    secret_config = LineConfig(
        builtin_tools=("Read",),
        mcp_servers=(
            ("jotter", McpServer("stdio", command="jotter", env=(("T", "s3"),))),
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
    turns = status["autonomous_turns"]
    assert (turns["used"], turns["allowed"]) == (0, 5)
    assert status["cost_usd"] == {
        "estimate": "0.2500",
        "basis": "exact",
        "limit": "3.00",
    }
    assert status["scheduled_wake"] == {"due": due, "message_id": message_id}
    assert status["unread"] == 1 and status["pending"] == 0
    assert (status["model"], status["strategy"]) == (HAIKU, "subscription")
    assert status["daemon"]["version"] == __version__
    assert status["commits"]["workdir"] is None
    assert status["commits"]["daemon"] == COMMIT
    assert status["commits"]["cite"] == (
        f"line alpha read its worktree at commit unknown, under the daemon at commit"
        f" {COMMIT}."
    )
    assert "recent_turns" not in status and "other_lines" not in status
    assert len(text.encode("utf-8")) <= 8192
    full_text, full_error = world.call("line_status", {"detail": True, "lists": True})
    assert not full_error
    full = json.loads(full_text)
    assert full["autonomous_turns"]["current_turn_counts"] is None
    assert full["cost_usd"] == {
        "estimate": "0.2500",
        "basis": "exact",
        "unknown_turns": 0,
        "limit": "3.00",
        "note": None,
    }
    assert (full["workdir"], full["workdir_path"]) == (None, str(world.root))
    assert full["engine_session"] == "session-1"
    assert (full["daemon"]["version"], full["daemon"]["commit"]) == (
        __version__,
        COMMIT,
    )
    assert full["kept_session"] == {
        "configured": False,
        "kept": False,
        "closes_at": None,
        "background_tasks": 0,
        "background_tasks_unknown": False,
        "tasks": [],
    }
    assert full["leases"] == []
    assert full["other_lines"] == [
        {
            "line_id": "beta",
            "state": "idle",
            "held": False,
            "hibernated": False,
            "workdir_commit": None,
            "leases": ["gate"],
        }
    ]
    assert full["commits"]["workdir"] is None
    assert full["commits"]["daemon"] == COMMIT
    for secret in ("a private body", "jotter", "s3", "was held for this test"):
        assert secret not in full_text


def test_line_status_refuses_unknown_and_bad_arguments(world: World) -> None:
    text, error = world.call("line_status", {"line": "beta"})
    assert error and "unexpected argument line" in text
    text, error = world.call("line_status", {"detail": "yes"})
    assert error and "detail must be true or false" in text
    text, error = world.call("line_status", {"lists": 1})
    assert error and "lists must be true or false" in text


def test_line_status_requests_long_lists_separately(world: World) -> None:
    detail_text, detail_error = world.call("line_status", {"detail": True})
    assert not detail_error
    detail = json.loads(detail_text)
    assert detail["other_line_count"] == 1
    assert "other_lines" not in detail and "recent_turns" not in detail
    lists_text, lists_error = world.call("line_status", {"lists": True})
    assert not lists_error
    lists = json.loads(lists_text)["lists"]
    assert lists["other_lines"][0]["line_id"] == "beta"
    assert isinstance(lists["recent_turns"], list)


def test_detailed_line_status_shows_the_effective_sandbox_and_shared_git_boundary(
    world: World,
) -> None:
    tree = world.root / "trees" / "line"
    tree.mkdir(parents=True)
    common = world.root / "primary" / ".git"
    gitdir = common / "worktrees" / "line"
    gitdir.mkdir(parents=True)
    (common / "hooks").mkdir()
    (common / "config").write_text("[core]\n")
    (gitdir / "commondir").write_text("../..\n")
    (tree / ".git").write_text(f"gitdir: {gitdir}\n")
    settings: dict[str, object] = {
        "enabled": True,
        "filesystem": {
            "allowWrite": [str(tree)],
            "denyWrite": [str(common / "config"), str(common / "hooks")],
        },
    }
    claude = ClaudeCodeLine(
        Driver.for_model(HAIKU, Strategy.SUBSCRIPTION),
        tree,
        sandbox=settings,
    )
    world.scheduler._engines["alpha"] = claude  # pyright: ignore[reportPrivateUsage]

    text, error = world.call("line_status", {"detail": True})
    assert not error
    status = json.loads(text)["sandbox"]
    assert status["available"] is True
    assert status["enabled"] is True
    assert status["settings"] == settings
    filesystem = cast(dict[str, object], status["filesystem"])
    assert filesystem["default_write_root"] == str(tree)
    assert filesystem["allowWrite"] == [str(tree)]
    assert filesystem["denyWrite"] == [str(common / "config"), str(common / "hooks")]
    assert status["shared_git_directory"] == str(common)
    assert status["shared_git_directory_write_allowed"] is False
    assert status["shared_git_denied_paths"] == [
        str(common / "config"),
        str(common / "hooks"),
    ]

    claude.sandbox = cast(
        dict[str, object],
        {
            "enabled": True,
            "filesystem": {
                "allowWrite": [str(tree), str(common)],
                "denyWrite": [str(common / "hooks"), str(common / "config")],
            },
        },
    )
    text, error = world.call("line_status", {"detail": True})
    assert not error
    status = json.loads(text)["sandbox"]
    assert status["shared_git_directory_write_allowed"] is True


def test_default_line_status_has_a_hard_size_bound(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = world.scheduler.daemon_status
    monkeypatch.setattr(
        world.scheduler,
        "daemon_status",
        lambda: {**original(), "commit": "x" * (MAX_LINE_STATUS_DEFAULT_BYTES * 2)},
    )
    text, error = world.call("line_status")
    assert not error
    assert len(text.encode("utf-8")) <= MAX_LINE_STATUS_DEFAULT_BYTES
    assert json.loads(text)["status_truncated"] is True


@pytest.mark.parametrize("detail", [False, True])
def test_line_status_lists_have_a_hard_size_bound(
    world: World, monkeypatch: pytest.MonkeyPatch, detail: bool
) -> None:
    original = world.scheduler._detailed_line_status  # pyright: ignore[reportPrivateUsage]

    def oversized(line_id: str) -> dict[str, object]:
        status = original(line_id)
        status["other_lines"] = [
            {"line_id": f"beta-{index}-" + "x" * 1_000, "state": "idle"}
            for index in range(20)
        ]
        return status

    monkeypatch.setattr(world.scheduler, "_detailed_line_status", oversized)
    text, error = world.call("line_status", {"detail": detail, "lists": True})
    assert not error
    assert len(text.encode("utf-8")) <= MAX_LINE_STATUS_DEFAULT_BYTES
    result = json.loads(text)
    assert result["status_truncated"] is True
    assert result["lists_truncated"] is True
    assert result["suggested_arguments"] == {"detail": True, "lists": False}


# R2, the budget line.


def test_every_prompt_opens_with_the_budget(world: World) -> None:
    first = world.turn("one")
    assert first.splitlines()[0] == (
        "Autonomous turns in a row without operator mail: 0 of 5."
        " Estimated cost since your last release:"
        " $0.00 of $3.00. A line past either is held. An operator's mail started"
        " this turn, so the count of autonomous turns starts again from 0 when it"
        " ends."
    )
    assert first.splitlines()[1] == "1 new message. 1 unread. 1 unanswered."
    world.scheduler.send(LineAddress("beta"), LineAddress("alpha"), "from beta")
    second = world.turn(sender=LineAddress("beta"))
    assert second.startswith("Autonomous turns in a row without operator mail: 0 of 5.")
    assert second.splitlines()[0].endswith(
        "This turn is autonomous, so the count becomes 1 when it ends."
    )
    third = world.turn("again", sender=LineAddress("beta"))
    assert third.startswith(
        "Autonomous turns in a row without operator mail: 1 of 5."
        " Estimated cost since your last release:"
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
    # The engine reads the path resolved against the project root, and names it so.
    rules = world.root / "CLAUDE.md"
    assert world.call("read_instructions", FULL) == (
        f"Contents of {rules}:\n\nFollow the rules.",
        False,
    )
    scheduler.configure_line(
        "alpha", LineConfig(instructions=("CLAUDE.md",), claude_code_prompt=True)
    )
    text, _ = world.call("read_instructions", FULL)
    assert (
        text
        == f"{CLAUDE_CODE_PROMPT_MARK}\n\nContents of {rules}:\n\nFollow the rules."
    )
    scheduler.configure_line(
        "alpha", LineConfig(instructions=("CLAUDE.md",), project_settings=True)
    )
    text, _ = world.call("read_instructions", FULL)
    assert "project's own CLAUDE.md files" in text
    scheduler.configure_line("alpha", LineConfig(instructions=("MISSING.md",)))
    text, error = world.call("read_instructions", FULL)
    assert error and f"cannot read the instructions {world.root / 'MISSING.md'}" in text


def test_a_codex_line_reads_its_developer_instructions(world: World) -> None:
    (world.root / "AGENTS.md").write_text("Codex rules.\n")
    world.scheduler.rebind_line("alpha", LUNA, Strategy.SUBSCRIPTION)
    world.scheduler.configure_line("alpha", LineConfig(instructions=("AGENTS.md",)))
    assert world.call("read_instructions", FULL) == (
        f"Contents of {world.root / 'AGENTS.md'}:\n\nCodex rules.",
        False,
    )


def test_relative_instructions_resolve_against_the_project_root(world: World) -> None:
    """Luna's turn of 2026-10-27. A line moved to a worktree still reads the file the
    project root holds, and its engine is built with that absolute path, while the
    stored configuration keeps the path as written."""
    (world.root / "LINES.md").write_text("Line rules.\n")
    worktree = world.root / "tmp" / "worktree"
    worktree.mkdir(parents=True)
    (worktree / "ONLY_HERE.md").write_text("Worktree rules.\n")
    config = LineConfig(workdir="tmp/worktree", instructions=("LINES.md",))
    world.scheduler.configure_line("alpha", config)
    rules = world.root / "LINES.md"
    assert world.call("read_instructions", FULL) == (
        f"Contents of {rules}:\n\nLine rules.",
        False,
    )
    assert world.scheduler.effective_config("alpha").instructions == ("LINES.md",)
    assert world.scheduler.engine_config("alpha").instructions == (str(rules),)
    world.scheduler.engine_for("alpha")
    assert world.engines["alpha"].spec.config.instructions == (str(rules),)
    # A file only the workdir holds is no longer found by a relative path.
    world.scheduler.configure_line(
        "alpha", LineConfig(workdir="tmp/worktree", instructions=("ONLY_HERE.md",))
    )
    text, error = world.call("read_instructions", FULL)
    assert error
    assert f"cannot read the instructions {world.root / 'ONLY_HERE.md'}" in text


def test_rooting_keeps_absolute_and_home_paths(tmp_path: Path) -> None:
    config = LineConfig(instructions=("/etc/rules.md", "~/rules.md", "a/b.md"))
    rooted = rooted_instructions(config, tmp_path)
    assert rooted.instructions == (
        "/etc/rules.md",
        "~/rules.md",
        str(tmp_path / "a/b.md"),
    )
    assert rooted_instructions(LineConfig(), tmp_path) == LineConfig()
    plain = LineConfig(instructions=("/etc/rules.md",))
    assert rooted_instructions(plain, tmp_path) is plain


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A project root inside ``tmp_path``, beside a file outside it and a home
    directory that holds a file."""
    root = tmp_path / "project"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "RULES.md").write_text("Rules.\n")
    (tmp_path / "outside.md").write_text("Not the project's.\n")
    home = tmp_path / "home"
    home.mkdir()
    (home / "CLAUDE.md").write_text("The operator's own.\n")
    monkeypatch.setenv("HOME", str(home))
    return root


def test_instructions_inside_the_root_or_named_explicitly_pass(
    project: Path, tmp_path: Path
) -> None:
    check_instructions(
        ("docs/RULES.md", "docs/../docs/RULES.md", "~/CLAUDE.md"), project
    )
    check_instructions((str(tmp_path / "outside.md"),), project)
    (project / "linked.md").symlink_to(project / "docs" / "RULES.md")
    check_instructions(("linked.md",), project)


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("../outside.md", "outside the project root"),
        ("docs/../../outside.md", "outside the project root"),
        ("escape.md", "outside the project root"),
        ("GONE.md", "do not name a regular file"),
        ("docs", "do not name a regular file"),
        ("~/GONE.md", "do not name a regular file"),
        ("/nonexistent/dromon/rules.md", "do not name a regular file"),
    ],
)
def test_instructions_that_escape_or_are_missing_are_refused(
    project: Path, tmp_path: Path, path: str, message: str
) -> None:
    (project / "escape.md").symlink_to(tmp_path / "outside.md")
    with pytest.raises(ConfigError, match=message) as refused:
        check_instructions((path,), project)
    assert path in str(refused.value)


def test_rooting_refuses_a_relative_path_that_now_escapes(
    project: Path, tmp_path: Path
) -> None:
    """A link changed after loading is caught when the engine is built."""
    (project / "escape.md").symlink_to(tmp_path / "outside.md")
    with pytest.raises(ConfigError, match="outside the project root"):
        rooted_instructions(LineConfig(instructions=("escape.md",)), project)


def test_the_project_file_locates_a_refused_instruction_path(project: Path) -> None:
    text = (
        "version = 1\n"
        "[profiles.ruled]\n"
        "instructions = ['../outside.md']\n"
        "[lines.alpha]\n"
        f"model = '{HAIKU}'\n"
        "instructions = ['docs/RULES.md']\n"
        "[lines.beta]\n"
        f"model = '{HAIKU}'\n"
        "profile = 'ruled'\n"
    )
    file = project / ".dromon" / "dromon.toml"
    parse(text.replace("../outside.md", "docs/RULES.md"), file, project)
    parse(text, file)
    with pytest.raises(ConfigFileError) as refused:
        parse(text, file, project)
    assert refused.value.key == "profiles.ruled.instructions"
    assert refused.value.line == 2
    assert "for line beta" in str(refused.value)
    own = text.replace("['docs/RULES.md']", "['GONE.md']")
    with pytest.raises(ConfigFileError) as refused:
        parse(own.replace("../outside.md", "docs/RULES.md"), file, project)
    assert refused.value.key == "lines.alpha.instructions"
    assert refused.value.line == 4


def test_an_override_naming_an_escaping_file_is_refused(world: World) -> None:
    with pytest.raises(ConfigError, match="outside the project root"):
        world.scheduler.override_line("alpha", "instructions", ["../events.sqlite"])
    assert "instructions" not in world.scheduler.line_overrides("alpha")


def test_read_instructions_refuses_a_path_stored_before_the_check(
    world: World,
) -> None:
    world.scheduler.configure_line(
        "alpha", LineConfig(instructions=("../events.sqlite",))
    )
    text, error = world.call("read_instructions", FULL)
    assert error and "outside the project root" in text


def test_a_failure_composing_the_prompt_holds_the_line(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of 2026-10-27, I0-3. A failure before the turn starts fails the turn
    rather than the tick, so the line is held and an operator asked, and the other
    lines are still scheduled."""

    def broken(*_: object, **__: object) -> str:
        raise RuntimeError("the prompt broke")

    monkeypatch.setattr(scheduler_module, "compose_prompt", broken)
    world.scheduler.send(ANA, LineAddress("beta"), "for beta")

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        world.scheduler.send(ANA, LineAddress("alpha"), "for alpha")
        await asyncio.sleep(0)
        await world.scheduler.idle()
        stop.set()
        await loop

    asyncio.run(go())
    assert world.engines == {}
    for line_id in ("alpha", "beta"):
        line = world.log.state.lines[line_id]
        assert line.held_by is not None
        body = world.log.state.messages[line.held_by].body
        assert "failed: the prompt broke" in body
        assert "sent no operator mail during it" in body


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
    # A path and a text are both offered, so the refusal names neither, per review r4
    # finding F1.
    assert (text, error) == (disclosure.REFUSAL, True)
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


def test_line_status_names_the_selected_shell(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.scheduler.configure_line("alpha", LineConfig(builtin_tools=("Bash",)))
    monkeypatch.setenv("SHELL", "/bin/bash")
    text, error = world.call("line_status", {"detail": False, "lists": False})
    assert not error
    shell = json.loads(text)["shell"]
    assert shell["required"] is True
    assert shell["path"] == "/bin/bash"
    assert shell["reason"] is None


def test_line_status_uses_adapter_environment_and_reports_failure(
    world: World, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shell = tmp_path / "bash"
    shell.write_text("#!/bin/sh\nexit 0\n")
    shell.chmod(0o755)
    world.scheduler.configure_line("alpha", LineConfig(builtin_tools=("Bash",)))
    adapter = object.__new__(ClaudeCodeLine)
    object.__setattr__(adapter, "_environment", {"SHELL": str(shell)})
    adapter.sandbox = None
    adapter.cwd = world.root
    adapter.config = LineConfig(builtin_tools=("Bash",))
    object.__setattr__(adapter, "_kept", None)
    cast(dict[str, Any], object.__getattribute__(world.scheduler, "_engines"))[
        "alpha"
    ] = adapter
    monkeypatch.setenv("SHELL", "/daemon/different/shell")

    text, error = world.call("line_status", {"detail": False, "lists": False})
    assert not error
    status = json.loads(text)["shell"]
    assert status == {"required": True, "path": str(shell), "reason": None}

    object.__setattr__(adapter, "_environment", {"SHELL": "/missing/adapter/shell"})
    text, error = world.call("line_status", {"detail": False, "lists": False})
    assert not error
    status = json.loads(text)["shell"]
    assert status == {
        "required": True,
        "path": None,
        "reason": "SHELL must name an executable file",
    }


def test_line_status_does_not_require_shell_when_line_has_no_shell_tool(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world.scheduler.configure_line("alpha", LineConfig(builtin_tools=("Read",)))
    monkeypatch.setenv("SHELL", "/missing/daemon/shell")
    text, error = world.call("line_status", {"detail": False, "lists": False})
    assert not error
    assert json.loads(text)["shell"] == {
        "required": False,
        "path": None,
        "reason": None,
    }


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
    assert (text, error) == (disclosure.REFUSAL, True)
    assert not (world.root / "tmp").exists()


# Item B2 of 2026-10-28, check_disclosure.


def listed(world: World, tmp_path: Path) -> None:
    (tmp_path / "terms").write_text("codename\nsecond term\n")
    world.scheduler.set_disclosure_terms(load_terms(tmp_path / "terms"))


def checks(world: World) -> list[DisclosureChecked]:
    return [
        entry.event
        for entry in read_entries(world.log_path)
        if isinstance(entry.event, DisclosureChecked)
    ]


def test_check_disclosure_answers_pass_or_fail_with_a_count(
    world: World, tmp_path: Path
) -> None:
    listed(world, tmp_path)
    text, error = world.call("check_disclosure", {"text": "clean\nlines"})
    assert not error and text.startswith("pass, 0 lines hold a term")
    body = "the CODENAME\nclean\nCodename and a Second Term\n"
    text, error = world.call("check_disclosure", {"text": body})
    assert not error and text.startswith("fail, 2 lines hold a term")
    (world.root / "notes.md").write_text("one codename line\nclean\n")
    text, error = world.call("check_disclosure", {"path": "notes.md"})
    assert not error and text.startswith("fail, 1 line holds a term")
    left = MAX_DISCLOSURE_CHECKS - 3
    assert f"You have {left} of {MAX_DISCLOSURE_CHECKS} checks left" in text
    assert "codename" not in text.casefold() and "second term" not in text.casefold()
    recorded = checks(world)
    assert [event.matched_lines for event in recorded] == [0, 2, 1]
    assert {event.checked_at for event in recorded} == {"2026-10-26T09:00:00Z"}


def test_check_disclosure_is_limited_per_line_across_a_restart(
    world: World, tmp_path: Path
) -> None:
    listed(world, tmp_path)
    for _ in range(MAX_DISCLOSURE_CHECKS):
        _, error = world.call("check_disclosure", {"text": "clean"})
        assert not error
    world.clock.now += timedelta(seconds=DISCLOSURE_CHECK_WINDOW_SECONDS - 1)
    world.log.close()
    world.log = EventLog(world.log_path)
    world.start()
    listed(world, tmp_path)
    text, error = world.call("check_disclosure", {"text": "codename"})
    assert error and "next is allowed at 2026-10-26T10:00:00Z" in text
    assert len(checks(world)) == MAX_DISCLOSURE_CHECKS
    # The window slides, so each check made an hour ago frees one place.
    world.clock.now += timedelta(seconds=1)
    text, error = world.call("check_disclosure", {"text": "clean"})
    assert not error and f"You have {MAX_DISCLOSURE_CHECKS - 1} of" in text
    world.clock.now += timedelta(seconds=1)
    text, error = world.call("check_disclosure", {"text": "clean"})
    assert not error and f"You have {MAX_DISCLOSURE_CHECKS - 2} of" in text


def test_check_disclosure_refuses_without_a_list_or_one_input(
    world: World, tmp_path: Path
) -> None:
    text, error = world.call("check_disclosure", {"text": "x"})
    assert error
    assert "no disclosure list is configured for this project" in text
    assert "daemon.disclosure_terms" in text
    assert "cannot pass or fail" in text
    listed(world, tmp_path)
    for args in ({}, {"text": "x", "path": "y"}):
        text, error = world.call("check_disclosure", cast(dict[str, object], args))
        assert error and "exactly one of text and path" in text
    big = "x" * (scheduler_module.MAX_CHECKED_BYTES + 1)
    text, error = world.call("check_disclosure", {"text": big})
    assert error and "is over" in text
    assert checks(world) == []


@pytest.mark.parametrize(
    ("path", "message"),
    [
        ("/etc/passwd", "relative path"),
        ("~/notes", "relative path"),
        ("../escape.md", "may not contain .."),
        (".dromon/secret.md", "Dromon protects"),
        ("link.md", "cannot read link.md"),
        ("linked/escape.md", "cannot read linked/escape.md"),
        ("hard.md", "more than one name"),
        ("folder", "not a regular file"),
        ("missing.md", "cannot read missing.md"),
    ],
)
def test_check_disclosure_reads_only_the_lines_own_files(
    world: World, tmp_path: Path, path: str, message: str
) -> None:
    listed(world, tmp_path)
    outside = tmp_path / "escape.md"
    outside.write_text("codename\n")
    (world.root / ".dromon" / "secret.md").write_text("codename\n")
    (world.root / "link.md").symlink_to(outside)
    (world.root / "linked").symlink_to(tmp_path)
    os.link(outside, world.root / "hard.md")
    (world.root / "folder").mkdir()
    text, error = world.call("check_disclosure", {"path": path})
    assert error and message in text
    assert checks(world) == []


# B2, as decided by the operator on 2026-10-28. A refusal for disclosure answers
# the same question a check does, so it spends the same limit.

DISCLOSING_CALLS: list[tuple[str, dict[str, object]]] = [
    ("send_message", {"to": "@ana", "body": "a Codename"}),
    ("ask_operator", {"question": "a Codename?", "choices": []}),
    ("wake_after", {"seconds": 60, "note": "a Codename"}),
    ("write_scratch", {"path": "draft.md", "text": "a Codename"}),
]


def held(world: World) -> list[LineHeld]:
    return [
        entry.event
        for entry in read_entries(world.log_path)
        if isinstance(entry.event, LineHeld)
    ]


@pytest.mark.parametrize(("name", "args"), DISCLOSING_CALLS)
def test_a_refusal_for_disclosure_spends_a_check(
    world: World, tmp_path: Path, name: str, args: dict[str, object]
) -> None:
    listed(world, tmp_path)
    text, error = world.call(name, args)
    field = {
        "send_message": "body",
        "ask_operator": "question",
        "wake_after": "note",
        "write_scratch": None,
    }[name]
    assert error and text == (
        disclosure.REFUSAL
        if field is None
        else f"the {field} field contains a term from the project's disclosure list"
    )
    assert [event.matched_lines for event in checks(world)] == [1]
    text, error = world.call("check_disclosure", {"text": "clean"})
    assert not error and f"You have {MAX_DISCLOSURE_CHECKS - 2} of" in text


def test_the_role_and_proposal_tools_spend_a_check_too(
    world: World, tmp_path: Path
) -> None:
    listed(world, tmp_path)
    scheduler = world.scheduler
    calls = [
        lambda: scheduler.record_role_note("alpha", RoleNoteKind.NOTE, "codename"),
        lambda: scheduler.relinquish_ownership("alpha", "codename", None, None),
        lambda: scheduler.take_ownership("alpha", "codename"),
        lambda: scheduler.propose_change("alpha", "codename", ["a.py"], "patch"),
    ]
    for call in calls:
        with pytest.raises(LineToolRefused, match="contains a term"):
            call()
    assert len(checks(world)) == len(calls)


def test_a_text_that_passes_spends_no_check(world: World, tmp_path: Path) -> None:
    listed(world, tmp_path)
    text, error = world.call("send_message", {"to": "@ana", "body": "clean"})
    assert not error and text.startswith("sent as")
    assert checks(world) == []


def test_a_spent_limit_holds_the_line_and_answers_nothing_more(
    world: World, tmp_path: Path
) -> None:
    listed(world, tmp_path)
    for _ in range(MAX_DISCLOSURE_CHECKS):
        text, error = world.call("send_message", {"to": "@ana", "body": "codename"})
        assert error and text == (
            "the body field contains a term from the project's disclosure list"
        )
    assert world.log.state.lines["alpha"].held_by is None
    text, error = world.call("send_message", {"to": "@ana", "body": "codename"})
    assert error and text == DISCLOSURE_HELD
    question_id = world.log.state.lines["alpha"].held_by
    assert question_id is not None
    question = world.log.state.messages[question_id]
    assert question.kind is MessageKind.QUESTION
    assert "disclosure checks" in question.body
    # Every checked tool now answers alike, so a clean text and a listed one cannot
    # be told apart, and the held turn learns nothing further of the list.
    sent = len(world.log.state.messages)
    clean: list[tuple[str, dict[str, object]]] = [
        ("send_message", {"to": "@ana", "body": "clean"}),
        ("write_scratch", {"path": "clean.md", "text": "clean"}),
        ("check_disclosure", {"text": "clean"}),
    ]
    for name, args in [*DISCLOSING_CALLS, *clean]:
        text, error = world.call(name, args)
        assert error and text == DISCLOSURE_HELD, name
    assert len(world.log.state.messages) == sent
    assert not (world.root / "tmp" / "clean.md").exists()
    assert len(checks(world)) == MAX_DISCLOSURE_CHECKS
    assert len(held(world)) == 1
    # A release lets clean text through, while the window, still full, holds the
    # line again at its next refusal.
    world.scheduler.release("alpha")
    text, error = world.call("send_message", {"to": "@ana", "body": "clean"})
    assert not error and text.startswith("sent as")
    text, error = world.call("send_message", {"to": "@ana", "body": "codename"})
    assert error and text == DISCLOSURE_HELD
    assert len(held(world)) == 2


def test_a_line_held_already_is_not_held_twice(world: World, tmp_path: Path) -> None:
    listed(world, tmp_path)
    world.scheduler.hold("alpha", "passed a test limit")
    for _ in range(MAX_DISCLOSURE_CHECKS):
        world.call("wake_after", {"seconds": 60, "note": "codename"})
    text, error = world.call("wake_after", {"seconds": 60, "note": "clean"})
    assert not error and "codename" not in text
    text, error = world.call("wake_after", {"seconds": 60, "note": "codename"})
    assert error and text == DISCLOSURE_HELD
    assert len(held(world)) == 1
    world.scheduler.release("alpha")
    text, error = world.call("wake_after", {"seconds": 60, "note": "clean"})
    assert not error


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
        return {
            "kept": True,
            "seconds_to_close": 90.0,
            "background_tasks": 2,
            "tasks": [
                {"task_id": "t1", "command": "make gate"},
                {"task_id": "t2", "command": "sleep 300"},
            ],
        }


def kept_engine(world: World) -> KeptRecorder:
    engine = KeptRecorder(LineSpec("alpha", HAIKU, Strategy.SUBSCRIPTION))
    world.engines["alpha"] = engine
    return engine


def test_line_status_reports_a_kept_session_and_its_deadline(world: World) -> None:
    world.scheduler.configure_line("alpha", LineConfig(keep_session=True))
    kept_engine(world)
    world.scheduler.engine_for("alpha")
    status = json.loads(world.call("line_status", {"detail": True, "lists": True})[0])
    assert status["kept_session"] == {
        "configured": True,
        "kept": True,
        "closes_at": "2026-10-26T09:01:30Z",
        "background_tasks": 2,
        "background_tasks_unknown": False,
        "tasks": [
            {"task_id": "t1", "command": "make gate"},
            {"task_id": "t2", "command": "sleep 300"},
        ],
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
    assert f"loaded its code at {status['loaded_at']}" in note


def test_line_status_cites_the_worktree_and_daemon_commits(
    world: World, tmp_path: Path
) -> None:
    """Restart to upgrade, decided on 2026-10-27. A review names both commits, so
    line_status gives each in full and one sentence that cites them."""
    work = checkout(world.root, "c" * 40)
    daemon = checkout(tmp_path / "daemon", COMMIT)
    world.scheduler = Scheduler(
        world.log,
        world.build,
        root=work,
        protected=(work / ".dromon",),
        daemon_commit=COMMIT,
        daemon_checkout=daemon,
    )
    compact_text, compact_error = world.call("line_status")
    assert not compact_error
    compact_commits = json.loads(compact_text)["commits"]
    assert (compact_commits["workdir"], compact_commits["daemon"]) == (
        "c" * 40,
        COMMIT,
    )
    assert compact_commits["cite"].endswith(f"under the daemon at commit {COMMIT}.")

    text, error = world.call("line_status", {"detail": True, "lists": True})
    assert not error
    commits = json.loads(text)["commits"]
    assert (commits["workdir"], commits["daemon"]) == ("c" * 40, COMMIT)
    assert (commits["daemon_checkout"], commits["stale"]) == (COMMIT, False)
    assert commits["cite"] == (
        f"line alpha read its worktree at commit {'c' * 40}, under the daemon at"
        f" commit {COMMIT}."
    )
    others = json.loads(text)["other_lines"]
    assert [other["workdir_commit"] for other in others] == ["c" * 40]
    moved = "d" * 40
    checkout(daemon, moved)
    compact_text, _ = world.call("line_status")
    compact = json.loads(compact_text)
    loaded_at = compact["daemon"]["loaded_at"]
    assert loaded_at == world.scheduler.daemon_status()["loaded_at"]
    stale_tail = (
        f", whose checkout has since moved to {moved} after the daemon loaded its"
        f" code at {loaded_at}."
    )
    assert compact["commits"]["stale"] is True
    assert compact["commits"]["cite"].endswith(stale_tail)
    text, _ = world.call("line_status", {"detail": True, "lists": True})
    commits = json.loads(text)["commits"]
    assert commits["stale"] is True
    assert commits["cite"].endswith(stale_tail)


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
    sources = found["sources"]
    assert found["bytes"] == len(data)
    assert found["sha256"] == hashlib.sha256(data).hexdigest()
    assert sources == [
        {"source": CLAUDE_CODE_PROMPT_MARK, "bytes": None},
        *(
            {
                "source": str(world.root / name),
                "bytes": len((world.root / name).read_bytes()),
            }
            for name in ("CLAUDE.md", "MORE.md")
        ),
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
    assert error and f"cannot read the instructions {world.root / 'MISSING.md'}" in text


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


# The operator's requests after phase 2 of federation, decided on 2026-10-28.

MOVED = "The commit checked out in your worktree moved"
SKIPPED = (
    "The daemon holds no record of the commit checked out in your worktree when an"
    " earlier turn of yours ended"
)


def test_a_turn_is_told_once_when_its_worktree_head_moved(world: World) -> None:
    """A line learns at its next turn that an operator committed or merged its work,
    with the commits before and after. A turn with no move and a worktree outside a
    checkout tell nothing. A first commit after a turn that recorded none is told
    as a skipped check, once."""
    outside = world.turn("one")
    checkout(world.root, "a" * 40)
    appeared = world.turn("two")
    still = world.turn("three")
    checkout(world.root, "b" * 40)
    moved = world.turn("four")
    after = world.turn("five")
    prompts = (outside, appeared, still, moved, after)
    assert all(MOVED not in prompt for prompt in (outside, appeared, still, after))
    assert [SKIPPED in prompt for prompt in prompts] == [
        False,
        True,
        False,
        False,
        False,
    ]
    assert appeared.startswith(f"{SKIPPED}, so it could not check")
    assert f"Your worktree is at {'a' * 12} now" in appeared
    assert moved.startswith(
        f"{MOVED} from {'a' * 12} to {'b' * 12} since your last turn ended."
        f" git log --oneline {'a' * 12}..{'b' * 12} lists the commits between"
    )


def test_a_moved_worktree_notice_survives_a_daemon_restart(world: World) -> None:
    checkout(world.root, "a" * 40)
    world.turn("first")
    world.log.close()
    world.log = EventLog(world.log_path)
    world.start()
    checkout(world.root, "b" * 40)

    prompt = world.turn("after the restart")
    assert prompt.startswith(
        f"{MOVED} from {'a' * 12} to {'b' * 12} since your last turn ended."
        f" git log --oneline {'a' * 12}..{'b' * 12} lists the commits between"
    )


def test_a_line_with_no_turn_end_commit_is_told_the_check_was_skipped_once(
    world: World,
) -> None:
    """Task of 2026-10-29, the core report's request 1. A line whose earlier turns
    ended under a daemon that recorded no turn-end commit, here a log with no
    ``line_turn_ended`` for it, is told once that the check was skipped."""
    checkout(world.root, "a" * 40)
    assert "alpha" not in world.log.state.last_turn_commits
    first = world.turn("first")
    second = world.turn("second")
    assert first.startswith(SKIPPED) and MOVED not in first
    assert SKIPPED not in second and MOVED not in second


ALPHA = LineAddress("alpha")


def expected_counts(state: State, owner: LineAddress) -> tuple[int, int, int]:
    """The unread, unanswered and own-note counts of ``owner``'s mailbox, counted
    here from the messages by their own rule, apart from ``dromon.mailbox``. Own
    notes and the harness's notes ask no reply, and a held line's resume is not
    used in these tests."""
    held = [
        (message_id, message)
        for message_id, message in state.messages.items()
        if message.recipient == owner and message.delivered
    ]
    replied = {
        message.reply_to
        for message in state.messages.values()
        if message.sender == owner and message.reply_to is not None
    }
    others = [(i, m) for i, m in held if m.sender != owner]
    return (
        sum(1 for _, m in others if not m.read),
        sum(
            1
            for i, m in others
            if i not in replied
            and not m.acknowledged
            and not isinstance(m.sender, HarnessAddress)
        ),
        sum(
            1
            for i, m in held
            if m.sender == owner
            and not m.read
            and not m.acknowledged
            and i not in replied
        ),
    )


LIST_MAIL_COUNTS = re.compile(
    r"(\d+) unread, (\d+) unanswered and (\d+) notes of your own neither read nor"
    r" acknowledged"
)
LIST_MAIL_ENTRY = re.compile(r"^(m-[0-9a-f]+) from ", re.MULTILINE)


def listed_ids(world: World, args: dict[str, Any]) -> list[str]:
    """The identifiers ``list_mail`` gives alpha for ``args``, in order."""
    text, error = world.call("list_mail", args)
    assert not error, text
    return LIST_MAIL_ENTRY.findall(text)


def every_view(world: World) -> dict[str, tuple[int, int, int]]:
    """alpha's three counts as each view gives them: ``line_status`` compact and in
    detail, ``list_mail``, and the client's line summaries."""
    compact = json.loads(world.call("line_status")[0])
    detail = json.loads(world.call("line_status", {"detail": True})[0])
    listed = LIST_MAIL_COUNTS.search(world.call("list_mail")[0])
    assert listed is not None
    (client,) = [
        entry
        for entry in world.scheduler.line_summaries()
        if entry["line_id"] == "alpha"
    ]

    def triple(view: dict[str, Any]) -> tuple[int, int, int]:
        return (view["unread"], view["unanswered"], view["own_notes"])

    return {
        "compact": triple(compact),
        "detail": triple(detail),
        "list_mail": (int(listed[1]), int(listed[2]), int(listed[3])),
        "client": triple(dict(client)),
    }


def mixed_turn(world: World) -> str:
    """One turn of alpha woken by an operator, a peer line, the harness and alpha
    itself, and the prompt it began with."""

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        world.scheduler.send(ANA, ALPHA, "from ana")
        world.scheduler.send(LineAddress("beta"), ALPHA, "from beta")
        world.scheduler.send(HARNESS, ALPHA, "from the harness")
        world.scheduler.send(ALPHA, ALPHA, "a note to myself")
        await asyncio.sleep(0)
        await world.scheduler.idle()
        stop.set()
        await loop

    asyncio.run(go())
    return world.engines["alpha"].prompts[-1]


def test_every_view_gives_one_unread_figure_for_a_mixed_mailbox(world: World) -> None:
    """Task of 2026-10-30. The wake prompt, ``line_status``, ``list_mail`` and the
    client give the same counts for the same mailbox, and an own note is counted
    apart from unread, before and after a prompt delivers it, rather than only in
    the prompt as before."""
    before = summary(world.log.state, ALPHA, 0)
    assert set(every_view(world).values()) == {(0, 0, 0)}
    assert before == MailSummary(0, 0, 0, 0)
    prompt = mixed_turn(world)
    state = world.log.state
    expected = expected_counts(state, ALPHA)
    assert expected == (3, 2, 1)
    assert MailSummary(4, *expected).sentence() in prompt
    assert set(every_view(world).values()) == {expected}
    # The prompt still announces the own note, and does not mark it read.
    (own,) = [i for i, m in state.messages.items() if m.sender == ALPHA]
    assert own in prompt
    assert state.messages[own].read is False
    assert not any(
        isinstance(e.event, MessageRead) and e.event.message_id == own
        for e in read_entries(world.log_path)
    )
    # Reading ana's message and the own note moves each count in every view.
    (from_ana,) = [i for i, m in state.messages.items() if m.sender == ANA]
    world.call("read_mail", {"message_id": from_ana})
    world.call("read_mail", {"message_id": own})
    later = world.turn("from ana again")
    expected = expected_counts(world.log.state, ALPHA)
    assert expected == (3, 3, 0)
    assert MailSummary(1, *expected).sentence() in later
    assert set(every_view(world).values()) == {expected}


def test_a_filtered_listing_lists_what_its_count_counts(world: World) -> None:
    """Review r6, finding 1. ``unread_only`` listed own notes while the header beside
    it said they were not unread. Each filter now lists the messages its count
    counts, and an own note is still listed on request."""
    mixed_turn(world)
    state = world.log.state
    unread, _, own_notes = expected_counts(state, ALPHA)
    (own,) = [i for i, m in state.messages.items() if m.sender == ALPHA]
    others = [
        i
        for i, m in state.messages.items()
        if m.recipient == ALPHA and m.sender != ALPHA and not m.read
    ]
    listed_unread = listed_ids(world, {"unread_only": True})
    listed_own = listed_ids(world, {"own_notes_only": True})
    assert listed_unread == others and len(listed_unread) == unread
    assert listed_own == [own] and len(listed_own) == own_notes
    assert own in listed_ids(world, {})
    # The sender filter narrows either one.
    assert listed_ids(world, {"own_notes_only": True, "sender": "beta"}) == []
    assert listed_ids(world, {"unread_only": True, "sender": "@ana"}) == [
        i for i in others if state.messages[i].sender == ANA
    ]


@pytest.mark.parametrize(
    ("args", "said"),
    [
        ({"own_notes_only": "yes"}, "own_notes_only must be true or false"),
        (
            {"unread_only": True, "own_notes_only": True},
            "unread_only and own_notes_only select different messages",
        ),
    ],
)
def test_a_listing_refuses_a_bad_own_notes_filter(
    world: World, args: dict[str, Any], said: str
) -> None:
    text, error = world.call("list_mail", args)
    assert error and said in text
    assert LIST_MAIL_COUNTS.search(text) is None


def test_an_acknowledged_own_note_leaves_the_own_count(world: World) -> None:
    """Review r6, finding 6. An own note a prompt delivered stays unread, and leaves
    ``own_notes`` in every view once the line acknowledges it, as lines do with
    fired wakes, so the count does not grow with every wake."""
    mixed_turn(world)
    (own,) = [i for i, m in world.log.state.messages.items() if m.sender == ALPHA]
    assert set(every_view(world).values()) == {(3, 2, 1)}
    _, error = world.call("acknowledge_mail", {"message_id": own})
    assert not error
    state = world.log.state
    assert state.messages[own].read is False and state.messages[own].acknowledged
    expected = expected_counts(state, ALPHA)
    assert expected == (3, 2, 0)
    assert set(every_view(world).values()) == {expected}
    assert listed_ids(world, {"own_notes_only": True}) == []
    later = world.turn("from ana again")
    assert MailSummary(1, *expected_counts(world.log.state, ALPHA)).sentence() in later
    assert "of your own, such as fired wakes" not in later


def test_only_the_owners_own_notes_are_counted_apart() -> None:
    """Mail from an operator, another line, an agent, a remote party or the harness
    is never left out of unread. Undelivered mail is in no count."""
    senders = {
        "m-operator": ANA,
        "m-line": LineAddress("beta"),
        "m-agent": AgentAddress("helper"),
        "m-remote": RemoteAddress("peer", OperatorAddress("ben")),
        "m-harness": HARNESS,
        "m-own": ALPHA,
        "m-own-read": ALPHA,
        "m-own-acknowledged": ALPHA,
    }
    state = State()
    for message_id, sender in senders.items():
        state.messages[message_id] = Message(
            sender,
            ALPHA,
            MessageKind.NOTE,
            "x",
            (),
            None,
            delivered=True,
            read=message_id == "m-own-read",
            acknowledged=message_id == "m-own-acknowledged",
        )
    state.messages["m-waiting"] = Message(ANA, ALPHA, MessageKind.NOTE, "x", (), None)
    counts = summary(state, ALPHA, 0)
    assert (counts.unread, counts.unanswered, counts.own_notes) == expected_counts(
        state, ALPHA
    )
    assert counts == MailSummary(0, 5, 4, 1)
    # The listing filters select exactly what the counts count.
    unread, total = listing(state, ALPHA, unread_only=True)
    assert [i for i, _ in unread] == [
        "m-operator",
        "m-line",
        "m-agent",
        "m-remote",
        "m-harness",
    ]
    assert total == counts.unread
    own, total = listing(state, ALPHA, own_notes_only=True)
    assert [i for i, _ in own] == ["m-own"] and total == counts.own_notes
    # Another owner's view of the same notes counts them as anyone's mail.
    beta = summary(state, LineAddress("beta"), 0)
    assert beta == MailSummary(0, 0, 0, 0)


@pytest.mark.parametrize(
    ("own_notes", "said"),
    [
        (0, None),
        (
            1,
            "1 note of your own, such as fired wakes, neither read nor"
            " acknowledged, is counted apart",
        ),
        (
            2,
            "2 notes of your own, such as fired wakes, neither read nor"
            " acknowledged, are counted apart",
        ),
    ],
)
def test_the_summary_says_how_many_own_notes_it_counts_apart(
    own_notes: int, said: str | None
) -> None:
    counts = MailSummary(1, 0, 0, own_notes)
    sentence = counts.sentence()
    assert sentence.startswith("1 new message. 0 unread. 0 unanswered.")
    assert (said in sentence) if said else sentence.endswith("unanswered.")
    assert counts.counts() == {"unread": 0, "unanswered": 0, "own_notes": own_notes}


def test_a_refused_listing_gives_no_counts(world: World) -> None:
    text, error = world.call("list_mail", {"limit": 0})
    assert error and LIST_MAIL_COUNTS.search(text) is None
    text, error = world.call("list_mail", {"unread_only": "yes"})
    assert error and LIST_MAIL_COUNTS.search(text) is None
