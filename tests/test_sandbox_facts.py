"""Round 14, item 8. A turn states the sandbox facts a line keeps rediscovering,
whether ``ps`` is allowed, where scratch and temporary files go, and what network is
configured, once and again only when they change, after a compaction, or for a line
that starts a fresh session each turn."""

import asyncio
import json
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import scheduler as scheduler_module
from dromon.driver import Strategy
from dromon.engine import ClaudeCodeLine, TurnReport
from dromon.eventlog import EventLog
from dromon.lineconfig import LineConfig
from dromon.linetemp import LineTemp
from dromon.model import LineAddress, OperatorAddress
from dromon.sandbox import effective, parse_sandbox
from dromon.scheduler import LineEngine, LineSpec, Scheduler

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")


class FakeClaude(ClaudeCodeLine):
    """A Claude Code line that holds a sandbox document and records its prompts."""

    def __init__(
        self, cwd: Path, sandbox: dict[str, object] | None, temp: LineTemp | None
    ) -> None:
        self.cwd = cwd
        self.sandbox = sandbox
        self.temp = temp
        self.prompts: list[str] = []
        self.session_id: str | None = None
        self.config = LineConfig()
        self._kept = None
        self._environment: dict[str, str] = {}

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        return TurnReport("success", 1, "ok", (), None, None, "s-1", None, None)

    async def retire(self) -> None:
        return None


def document(root: Path, extra: dict[str, object] | None = None) -> dict[str, object]:
    base: dict[str, object] = {
        "enabled": True,
        "network": {"allowedDomains": ["pypi.org", "github.com"]},
    }
    base.update(extra or {})
    found = effective(parse_sandbox(base, operator=True), root, [])
    assert found is not None
    return found


class World:
    def __init__(
        self, path: Path, project: Path, sandbox: dict[str, object] | None
    ) -> None:
        self.project = project
        self.temp = LineTemp(path / "t", path / "t" / "tmp", path / "t" / "uv-cache")
        self.engines: dict[str, FakeClaude] = {}
        self.sandbox = sandbox
        self.log = EventLog(path / "events.sqlite")
        self.scheduler = Scheduler(self.log, self.build, tick_seconds=0, root=project)
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        engine = FakeClaude(self.project, self.sandbox, self.temp)
        self.engines[spec.line_id] = engine
        return cast(LineEngine, engine)

    def turn(self, text: str = "x") -> str:
        async def act() -> None:
            self.scheduler.send(ANA, LineAddress("alpha"), text)

        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            await asyncio.sleep(0)
            await act()
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(go())
        return self.engines["alpha"].prompts[-1]


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    project = tmp_path / "project"
    project.mkdir()
    found = World(tmp_path, project, None)
    found.sandbox = document(
        project, {"filesystem": {"allowWrite": found.temp.writable()}}
    )
    yield found
    found.log.close()


def test_the_first_turn_states_the_facts_and_a_later_one_does_not(world: World) -> None:
    first = world.turn()
    assert "Your sandbox, as the daemon configured it for this turn." in first
    assert "reaches only github.com, pypi.org." in first
    scratch = world.project / "tmp" / "lines" / "alpha"
    assert f"Scratch is {scratch}, which your shell may write." in first
    assert (
        f"TMPDIR is {world.temp.temp} and UV_CACHE_DIR is {world.temp.cache}" in first
    )
    second = world.turn("again")
    assert "Your sandbox," not in second


@pytest.mark.parametrize(
    ("platform", "phrase"),
    [
        (
            "darwin",
            "ps is refused in your shell on this platform, so use list_processes",
        ),
        ("linux", "ps is of unknown use in your shell here"),
    ],
)
def test_ps_is_stated_by_platform_and_never_guessed_as_allowed(
    world: World, monkeypatch: pytest.MonkeyPatch, platform: str, phrase: str
) -> None:
    monkeypatch.setattr(scheduler_module.sys, "platform", platform)
    assert phrase in world.turn()


def test_a_change_in_the_facts_is_stated_again(world: World) -> None:
    world.turn()
    assert "Your sandbox," not in world.turn("again")
    world.sandbox = document(world.project, {"network": {"allowedDomains": ["a.org"]}})
    world.engines["alpha"].sandbox = world.sandbox
    third = world.turn("changed")
    assert "reaches only a.org." in third


def test_a_fresh_session_each_turn_states_them_every_turn(world: World) -> None:
    world.scheduler.configure_line("alpha", LineConfig(fresh_session_each_turn=True))
    assert "Your sandbox," in world.turn()
    assert "Your sandbox," in world.turn("again")


def test_a_line_with_no_sandbox_is_told_it_is_not_confined(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    found = World(tmp_path, project, None)
    try:
        prompt = found.turn()
    finally:
        found.log.close()
    assert "not confined by a Dromon sandbox, so ps runs" in prompt
    assert "Network, as configured" not in prompt


def test_a_line_whose_workdir_is_denied_to_writes_is_told_so(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    sandbox = document(project, {"filesystem": {"denyWrite": [str(project)]}})
    found = World(tmp_path, project, sandbox)
    try:
        prompt = found.turn()
    finally:
        found.log.close()
    assert "which your shell may not write." in prompt.split("Scratch is ")[1]


def test_a_line_without_directories_keeps_the_engines_temporary_directory(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    found = World(tmp_path, project, document(project))
    found.engines["alpha"] = FakeClaude(project, found.sandbox, None)
    found.scheduler._engines["alpha"] = found.engines["alpha"]  # pyright: ignore[reportPrivateUsage]
    try:
        facts = found.scheduler.sandbox_facts("alpha")
        text = found.scheduler._sandbox_facts_text("alpha")  # pyright: ignore[reportPrivateUsage]
    finally:
        found.log.close()
    assert facts is not None and facts["temporary"] is None
    assert text is not None
    assert "keeps the temporary directory the engine gave it" in text


def test_an_engine_that_is_not_claude_code_is_told_nothing(tmp_path: Path) -> None:
    class Other:
        session_id = None
        prompts: list[str] = []

        async def run_turn(self, prompt: str) -> TurnReport:
            self.prompts.append(prompt)
            return TurnReport("success", 1, "ok", (), None, None, "s", None, None)

        async def retire(self) -> None:
            return None

    with EventLog(tmp_path / "events.sqlite") as log:
        target = Scheduler(
            log, lambda spec, tools: cast(LineEngine, Other()), tick_seconds=0
        )
        target.ensure_operator("ana")
        target.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(target.run(stop))
            await asyncio.sleep(0)
            target.send(ANA, LineAddress("alpha"), "x")
            await target.idle()
            stop.set()
            await loop

        asyncio.run(go())
        assert "Your sandbox," not in Other.prompts[-1]
        assert target.sandbox_facts("alpha") is None


def test_line_status_gives_the_facts_whole(world: World) -> None:
    world.turn()
    world.scheduler._engines["alpha"] = world.engines["alpha"]  # pyright: ignore[reportPrivateUsage]
    facts = world.scheduler.line_status("alpha")["sandbox_facts"]
    assert isinstance(facts, dict)
    text = json.dumps(facts)
    assert facts["shell_sandboxed"] is True
    assert facts["network"] == {
        "restricted": True,
        "allowed_domains": ["github.com", "pypi.org"],
    }
    assert cast(dict[str, object], facts["scratch"])["shell_may_write"] is True
    assert str(world.temp.temp) in text
    assert "not probes" in cast(str, facts["note"])
