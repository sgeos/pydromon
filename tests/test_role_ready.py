"""A successor reports a role ready only once it owns it, with ``HEAD`` and the dirty
paths together. **Round 14 task of 2026-11-01.**"""

import asyncio
import os
import subprocess
from collections.abc import Generator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import readiness
from dromon.driver import Strategy
from dromon.lineconfig import DEFAULT_CONFIG
from dromon.eventlog import EventLog
from dromon.model import (
    HarnessAddress,
    OperatorAddress,
    RoleDeclared,
    SeatBound,
)
from dromon.readiness import (
    MAX_READY_PATH_CHARACTERS,
    MAX_READY_PATHS,
    ReadinessUnknown,
    parse_porcelain,
)
from dromon.scheduler import (
    REPORT_ROLE_READY_TOOL_NAME,
    LineEngine,
    LineSpec,
    LineToolRefused,
    Scheduler,
)

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
WORKTREE = "tmp/worktrees/core"


# The parser.


def test_porcelain_entries_are_read_with_their_status() -> None:
    dirty = parse_porcelain(" M src/a.py\0?? notes.md\0A  new.py\0")
    assert dirty == readiness.Dirty((" M src/a.py", "?? notes.md", "A  new.py"), 3)


def test_a_rename_folds_its_origin_into_its_entry() -> None:
    dirty = parse_porcelain("R  b.py\0a.py\0 M c.py\0")
    assert dirty.paths == ("R  a.py -> b.py", " M c.py")
    assert dirty.total == 2


def test_clean_output_has_no_paths() -> None:
    assert parse_porcelain("") == readiness.Dirty((), 0)


@pytest.mark.parametrize("output", ["XY\0", "MX_x\0", "R  b.py\0"])
def test_output_not_in_the_form_is_refused(output: str) -> None:
    with pytest.raises(ReadinessUnknown):
        parse_porcelain(output)


def test_paths_are_bounded_in_count_and_length() -> None:
    long = "x" * (MAX_READY_PATH_CHARACTERS + 10)
    entries = "".join(f"?? f{index}\0" for index in range(MAX_READY_PATHS + 5))
    dirty = parse_porcelain(f" M {long}\0{entries}")
    assert dirty.total == MAX_READY_PATHS + 6
    assert len(dirty.paths) == MAX_READY_PATHS
    assert dirty.paths[0] == f" M {long[:MAX_READY_PATH_CHARACTERS]}..."


# The daemon.


def git(directory: Path, *arguments: str) -> None:
    environment = {
        name: value for name, value in os.environ.items() if not name.startswith("GIT_")
    }
    subprocess.run(  # noqa: S603
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
        env=environment,
    )


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start here, but {spec.line_id} did")


class Role:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path.resolve() / "project"
        self.worktree = self.root / WORKTREE
        self.worktree.mkdir(parents=True)
        (self.root / ".dromon").mkdir()
        git(self.worktree, "init", "-q")
        (self.worktree / "kept.txt").write_text("kept\n")
        git(self.worktree, "add", "kept.txt")
        git(self.worktree, "commit", "-q", "-m", "first")
        self.log = EventLog(tmp_path / "events.sqlite")
        self.scheduler = Scheduler(
            self.log,
            no_engine,
            tick_seconds=0,
            default_operator="ana",
            root=self.root,
            protected=(self.root / ".dromon",),
        )
        self.scheduler.ensure_operator("ana")
        for line_id in ("alpha", "beta"):
            self.scheduler.create_line(line_id, HAIKU, Strategy.SUBSCRIPTION)
            self.scheduler.configure_line(
                line_id, replace(DEFAULT_CONFIG, workdir=WORKTREE)
            )
        for event in (
            RoleDeclared("core", WORKTREE, 1800),
            SeatBound("alpha", "core", True),
            SeatBound("beta", "core", True),
        ):
            self.log.append(event)

    def ready(self, line_id: str) -> str:
        return asyncio.run(self.scheduler.report_role_ready(line_id))


@pytest.fixture
def role(tmp_path: Path) -> Generator[Role]:
    target = Role(tmp_path)
    yield target
    target.log.close()


def test_a_vacant_role_is_not_reported_ready(role: Role) -> None:
    with pytest.raises(LineToolRefused, match="has not passed to you, since it has no"):
        role.ready("beta")


def test_a_seat_that_does_not_own_the_role_is_refused(role: Role) -> None:
    role.scheduler.give_ownership(ANA, "core", "alpha")
    with pytest.raises(LineToolRefused, match="it is owned by alpha"):
        role.ready("beta")
    assert not any(
        isinstance(message.sender, HarnessAddress)
        and "reports it ready" in message.body
        for message in role.log.state.messages.values()
    )


def test_a_line_that_is_no_seat_is_refused(role: Role) -> None:
    role.scheduler.create_line("gamma", HAIKU, Strategy.SUBSCRIPTION)
    with pytest.raises(LineToolRefused, match="a seat of no role"):
        role.ready("gamma")


def test_the_owner_reports_head_and_dirty_paths_together(role: Role) -> None:
    role.scheduler.give_ownership(ANA, "core", "beta")
    (role.worktree / "kept.txt").write_text("changed\n")
    (role.worktree / "new.txt").write_text("new\n")
    report = role.ready("beta")
    head = readiness_head(role.worktree)
    assert f"at HEAD {head} and 2 dirty paths:\n M kept.txt\n?? new.txt" in report
    (sent,) = [
        message
        for message in role.log.state.messages.values()
        if isinstance(message.sender, HarnessAddress) and message.recipient == ANA
    ]
    assert sent.body == report


def test_a_clean_worktree_says_so(role: Role) -> None:
    role.scheduler.give_ownership(ANA, "core", "beta")
    assert role.ready("beta").endswith("and no dirty paths")


def test_a_worktree_git_cannot_read_is_refused(
    role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    role.scheduler.give_ownership(ANA, "core", "beta")

    def fail(worktree: Path) -> readiness.Dirty:
        raise ReadinessUnknown(f"git status failed in {worktree.name}")

    monkeypatch.setattr(readiness, "dirty_paths", fail)
    with pytest.raises(LineToolRefused, match="no readiness is reported"):
        role.ready("beta")


def test_a_former_writer_still_in_flight_is_refused(
    role: Role, monkeypatch: pytest.MonkeyPatch
) -> None:
    role.scheduler.give_ownership(ANA, "core", "beta")

    def waits(line_id: str) -> bool:
        del line_id
        return True

    monkeypatch.setattr(role.scheduler, "_waits_for_writer", waits)
    with pytest.raises(LineToolRefused, match="still in flight"):
        role.ready("beta")


def test_the_tool_is_offered_to_seats_and_takes_no_arguments(role: Role) -> None:
    tools = {tool.name: tool for tool in role.scheduler.own_tools("beta")}
    tool = tools[REPORT_ROLE_READY_TOOL_NAME]

    async def invoke(args: dict[str, object]) -> dict[str, Any]:
        return await tool.handler(args)

    result = asyncio.run(invoke({"x": 1}))
    assert result.get("is_error") is True
    result = asyncio.run(invoke({}))
    assert result.get("is_error") is True
    assert "has not passed to you" in cast(str, result["content"][0]["text"])
    role.scheduler.create_line("gamma", HAIKU, Strategy.SUBSCRIPTION)
    names = {tool.name for tool in role.scheduler.own_tools("gamma")}
    assert REPORT_ROLE_READY_TOOL_NAME not in names


def readiness_head(worktree: Path) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()
