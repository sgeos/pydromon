"""Round 15, item 6. A line asks whether its sandbox allows a read or a write of a path
inside its own scratch directory, and is told which rule decides. It checks the
document the engine was given, performs no input or output on the path, and says that
it is not an operating-system probe."""

import asyncio
import json
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import sandboxprobe
from dromon.driver import Strategy
from dromon.engine import ClaudeCodeLine, TurnReport
from dromon.eventlog import EventLog
from dromon.lineconfig import LineConfig
from dromon.linetemp import LineTemp
from dromon.sandbox import effective, parse_sandbox
from dromon.sandboxprobe import ProbeRefused, probe
from dromon.scheduler import LineEngine, LineSpec, Scheduler


HAIKU = "claude-haiku-5-5"


class FakeClaude(ClaudeCodeLine):
    """A Claude Code line that holds a sandbox document and runs no turn."""

    def __init__(
        self, cwd: Path, sandbox: dict[str, object] | None, temp: LineTemp | None
    ) -> None:
        self.cwd = cwd
        self.sandbox = sandbox
        self.temp = temp
        self.session_id: str | None = None
        self.config = LineConfig()
        self._kept = None
        self._environment: dict[str, str] = {}

    async def run_turn(self, prompt: str) -> TurnReport:
        return TurnReport("success", 1, "ok", (), None, None, "s-1", None, None)

    async def retire(self) -> None:
        return None


def document(root: Path, extra: dict[str, object] | None = None) -> dict[str, object]:
    base: dict[str, object] = {"enabled": True}
    base.update(extra or {})
    found = effective(parse_sandbox(base, operator=True), root, [])
    assert found is not None
    return found


def test_a_write_in_a_granted_directory_is_allowed_with_its_rule(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "work" / "scratch"
    sandbox = document(
        tmp_path / "work", {"filesystem": {"allowWrite": [str(scratch)]}}
    )
    found = probe(sandbox, tmp_path / "work", scratch, "write", "note.txt")
    assert found["result"] == "allowed"
    assert found["operating_system_probe"] is False
    assert "document" in cast(str, found["basis"])
    assert found["path"] == str(scratch / "note.txt")


def test_a_denied_write_names_the_denying_entry_and_outranks_a_grant(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    scratch = work / "scratch"
    sandbox = document(
        work,
        {"filesystem": {"allowWrite": [str(work)], "denyWrite": [str(scratch / "ro")]}},
    )
    found = probe(sandbox, work, scratch, "write", "ro/file")
    assert found["result"] == "denied" and "denyWrite" in cast(str, found["rule"])
    elsewhere = probe(sandbox, work, scratch, "write", "ok/file")
    assert elsewhere["result"] == "allowed"


def test_a_read_is_denied_by_denyread_and_lifted_by_allowread(tmp_path: Path) -> None:
    work = tmp_path / "work"
    scratch = work / "scratch"
    deny = document(work, {"filesystem": {"denyRead": [str(scratch / "hidden")]}})
    assert probe(deny, work, scratch, "read", "hidden/x")["result"] == "denied"
    assert probe(deny, work, scratch, "read", "shown/x")["result"] == "allowed"
    lift = document(
        work,
        {
            "filesystem": {
                "denyRead": [str(scratch)],
                "allowRead": [str(scratch / "own")],
            }
        },
    )
    lifted = probe(lift, work, scratch, "read", "own/y")
    assert lifted["result"] == "allowed" and "lifts" in cast(str, lifted["rule"])
    assert probe(lift, work, scratch, "read", "other")["result"] == "denied"


@pytest.mark.parametrize(
    "text",
    ["", "../escape", "a/../../escape", "/etc/passwd", "x\x00y", "a" * 2000],
)
def test_a_path_outside_scratch_or_malformed_is_refused_unlooked_at(
    tmp_path: Path, text: str
) -> None:
    work = tmp_path / "work"
    sandbox = document(work, {})
    with pytest.raises(ProbeRefused):
        probe(sandbox, work, work / "scratch", "write", text)


def test_an_unknown_operation_and_a_missing_document_are_refused(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    with pytest.raises(ProbeRefused, match="operation"):
        probe(document(work, {}), work, work / "s", "delete", "x")
    with pytest.raises(ProbeRefused, match="no enabled sandbox"):
        probe(None, work, work / "s", "write", "x")
    with pytest.raises(ProbeRefused, match="no enabled sandbox"):
        probe({"enabled": False}, work, work / "s", "write", "x")


def test_the_probe_never_touches_the_named_path(tmp_path: Path) -> None:
    work = tmp_path / "work"
    scratch = work / "scratch"
    sandbox = document(work, {"filesystem": {"allowWrite": [str(scratch)]}})
    probe(sandbox, work, scratch, "write", "created.txt")
    probe(sandbox, work, scratch, "read", "created.txt")
    assert not scratch.exists()


def test_a_link_out_of_scratch_is_followed_for_the_rule_and_never_for_content(
    tmp_path: Path,
) -> None:
    work = tmp_path / "work"
    scratch = work / "scratch"
    scratch.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (scratch / "link").symlink_to(outside)
    sandbox = document(work, {"filesystem": {"allowWrite": [str(scratch)]}})
    found = probe(sandbox, work, scratch, "write", "link/file")
    assert found["result"] == "denied"
    assert not (outside / "file").exists()
    assert sandboxprobe.MAX_PATH_CHARACTERS == 1024


class World:
    def __init__(self, path: Path, sandbox: dict[str, object] | None) -> None:
        self.project = path / "project"
        self.project.mkdir()
        self.sandbox = sandbox
        self.log = EventLog(path / "events.sqlite")
        self.scheduler = Scheduler(
            self.log, self.build, tick_seconds=0, root=self.project
        )
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        engine = FakeClaude(self.project, self.sandbox, None)
        self.scheduler._engines[spec.line_id] = cast(  # pyright: ignore[reportPrivateUsage]
            LineEngine, engine
        )
        return cast(LineEngine, engine)


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    found = World(tmp_path, None)
    found.sandbox = document(found.project, {})
    yield found
    found.log.close()


def call(world: World, **arguments: object) -> tuple[bool, str]:
    world.scheduler.engine_for("alpha")
    tool = world.scheduler.sandbox_probe_tool("alpha")

    async def invoke() -> dict[str, Any]:
        return await tool.handler(arguments)

    answer = asyncio.run(invoke())
    return answer.get("is_error") is True, str(answer["content"][0]["text"])


def test_the_tool_answers_for_the_line_and_a_scratch_relative_path(
    world: World,
) -> None:
    error, text = call(world, operation="write", path="note.txt")
    assert not error
    found = json.loads(text)
    scratch = world.project / "tmp" / "lines" / "alpha"
    assert found["path"] == str(scratch / "note.txt")
    assert found["operating_system_probe"] is False
    assert found["result"] in ("allowed", "denied")


def test_the_tool_refuses_bad_arguments_and_a_line_without_a_document(
    world: World,
) -> None:
    assert call(world, operation="write", path="../x")[0]
    assert call(world, operation="write")[0]
    assert call(world, operation="write", path="x", extra=1)[0]
    world.sandbox = None
    world.scheduler._engines["alpha"].sandbox = None  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]
    error, text = call(world, operation="read", path="x")
    assert error and "no enabled sandbox" in text
