"""Round 10 re-review of the role commands, findings N1 to N4.

N1 and N2. Every line could write its working directory's ``tmp/``, through
``write_scratch`` and through the Codex read profile, so a line that may only read a
role's worktree could still write a file of its owner there. A line that does not
write its working directory now has a scratch of its own, ``tmp/lines/LINE``.

N3. A seat whose engine cannot confine it runs with its own access while its role is
vacant, and its turn went on after another seat took the role, beside the new owner.
The new owner's first turn now waits for that turn, and for its kept connection.

N4. Worktrees were compared for equality only, so a worktree inside another's was not
refused. ``test_role_crud`` covers the commands, and this file the rule itself."""

import asyncio
import os
from collections.abc import Callable, Coroutine, Iterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon.codex import CodexLine
from dromon.driver import Strategy
from dromon.engine import EngineRefused, TurnReport
from dromon.eventlog import EventLog
from dromon.lineconfig import LineConfig, WorkdirAccess
from dromon.model import (
    LineAddress,
    OperatorAddress,
    OwnershipCause,
    OwnershipChanged,
    RoleDeclared,
    SeatBound,
    format_due,
)
from dromon.roles import (
    LinePlace,
    RolePlace,
    overlapping,
    placement_refusal,
    shared_worktree,
    shared_worktree_refusal,
)
from dromon.scheduler import (
    LINES_SCRATCH_DIRECTORY,
    SCRATCH_DIRECTORY,
    LineEngine,
    LineSpec,
    Scheduler,
    engine_factory,
    own_scratch,
)

HAIKU = "claude-haiku-5-5"
LUNA = "gpt-6-luna"
GROK = "grok-4.7"
ANA = OperatorAddress("ana")
NOW = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
AT = format_due(NOW)
TREE = "tmp/worktrees/core"
OTHER = "tmp/worktrees/other"
WRITER = LineConfig(builtin_tools=("Read", "Edit"))


class Done:
    """A line engine whose turns end at once, after any gate its world sets."""

    def __init__(self, world: "World", spec: LineSpec) -> None:
        self.world = world
        self.line_id = spec.line_id
        self.session_id: str | None = None

    async def run_turn(self, prompt: str) -> TurnReport:
        del prompt
        self.world.events.append(f"{self.line_id} started")
        gate = self.world.gates.get(self.line_id)
        if gate is not None:
            await gate.wait()
        self.world.events.append(f"{self.line_id} ended")
        return TurnReport("success", 1, "ok", (), None, None, "session-1", None, None)


class Kept(Done):
    """A ``Done`` engine that keeps its connection, so a drop closes it."""

    async def retire(self) -> None:
        self.world.events.append(f"{self.line_id} retired")

    def kept_status(self) -> dict[str, object]:
        return {}


class World:
    """Role core on ``TREE`` with seats alpha and beta on Claude Code and gina on Grok,
    whose engine cannot confine a seat yet. Alpha owns the role unless ``vacant``. Line
    delta runs in ``OTHER`` with no writing tool and gamma there with one, and
    neither is a seat."""

    def __init__(self, root: Path, *, vacant: bool = False) -> None:
        (root / TREE).mkdir(parents=True)
        (root / OTHER).mkdir(parents=True)
        self.root = root
        self.log = EventLog(root / "events.sqlite")
        self.events: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.kept: set[str] = set()
        self.scheduler = Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            clock=lambda: 0.0,
            wall_clock=lambda: NOW,
            default_operator="ana",
            root=root,
            protected=(root / ".dromon",),
        )
        self.scheduler.ensure_operator("ana")
        seats = {"alpha": HAIKU, "beta": HAIKU, "gina": GROK}
        for line_id, model in seats.items():
            self.scheduler.create_line(
                line_id, model, Strategy.SUBSCRIPTION, LineConfig(workdir=TREE)
            )
        self.scheduler.create_line(
            "delta", HAIKU, Strategy.SUBSCRIPTION, LineConfig(workdir=OTHER)
        )
        self.scheduler.create_line(
            "gamma", HAIKU, Strategy.SUBSCRIPTION, replace(WRITER, workdir=OTHER)
        )
        self.log.append(RoleDeclared("core", TREE, seats=tuple(seats)))
        for line_id in seats:
            self.log.append(SeatBound(line_id, "core", True))
        if not vacant:
            self.log.append(
                OwnershipChanged(
                    "core", "alpha", None, OwnershipCause.DECLARED, ANA, None, AT
                )
            )

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        del tools
        return Kept(self, spec) if spec.line_id in self.kept else Done(self, spec)

    def scratch(self, line_id: str) -> Path:
        return self.root / TREE / SCRATCH_DIRECTORY / LINES_SCRATCH_DIRECTORY / line_id


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    target = World(tmp_path / "project")
    yield target
    target.log.close()


@pytest.fixture
def vacant(tmp_path: Path) -> Iterator[World]:
    target = World(tmp_path / "project", vacant=True)
    yield target
    target.log.close()


def run(world: World, act: Callable[[], Coroutine[Any, Any, None]]) -> None:
    async def settle() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        await asyncio.sleep(0)
        await act()
        await world.scheduler.idle()
        stop.set()
        await loop

    asyncio.run(settle())


async def until(condition: Callable[[], bool]) -> None:
    """Yield to the loop until ``condition`` holds, at most 500 times."""
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never held")


# N1, write_scratch.


def test_each_line_has_the_scratch_its_access_gives(world: World) -> None:
    parts = world.scheduler.scratch_parts
    own = (SCRATCH_DIRECTORY, LINES_SCRATCH_DIRECTORY)
    assert parts("alpha") == (SCRATCH_DIRECTORY,)
    assert parts("beta") == (*own, "beta")
    assert parts("gina") == (*own, "gina")
    assert parts("delta") == (*own, "delta")
    assert parts("gamma") == (SCRATCH_DIRECTORY,)
    # The scratch follows ownership.
    world.scheduler.give_ownership(ANA, "core", "beta")
    assert parts("beta") == (SCRATCH_DIRECTORY,)
    assert parts("alpha") == (*own, "alpha")
    world.scheduler.give_ownership(ANA, "core", None)
    assert parts("beta") == (*own, "beta")


def test_a_read_seat_writes_only_its_own_scratch(world: World) -> None:
    """The review's probe, made by a seat. The owner's file is left as it was."""
    assert world.scheduler.write_scratch("alpha", "notes.md", "owner") == (
        "tmp/notes.md"
    )
    written = world.scheduler.write_scratch("beta", "notes.md", "seat")
    assert written == "tmp/lines/beta/notes.md"
    assert (world.root / TREE / "tmp/notes.md").read_text() == "owner"
    assert (world.scratch("beta") / "notes.md").read_text() == "seat"
    # A line that is not a seat and has no writing tool has one of its own too.
    assert world.scheduler.write_scratch("delta", "x", "y") == "tmp/lines/delta/x"
    assert (world.root / OTHER / "tmp/lines/delta/x").read_text() == "y"


def test_a_link_in_place_of_the_own_scratch_is_refused(
    world: World, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (world.root / TREE / "tmp/lines").mkdir(parents=True)
    world.scratch("beta").symlink_to(outside)
    with pytest.raises(Exception, match="resolves outside tmp/ through a link"):
        world.scheduler.write_scratch("beta", "x", "y")
    (world.root / TREE / "tmp/lines").rename(world.root / TREE / "tmp/moved")
    (world.root / TREE / "tmp/lines").symlink_to(outside)
    with pytest.raises(Exception, match="resolves outside tmp/ through a link"):
        world.scheduler.write_scratch("beta", "x", "y")
    assert list(outside.iterdir()) == []


def test_a_seats_scratch_moves_when_it_gains_its_role_and_back(world: World) -> None:
    """Review of ebedeed, G2. The scratch follows ownership both ways, through
    ``write_scratch`` itself, and a file written under one scratch stays there."""
    write = world.scheduler.write_scratch
    assert write("beta", "notes.md", "as a seat") == "tmp/lines/beta/notes.md"
    world.scheduler.give_ownership(ANA, "core", "beta")
    assert write("beta", "notes.md", "as the owner") == "tmp/notes.md"
    assert write("alpha", "notes.md", "as a seat") == "tmp/lines/alpha/notes.md"
    world.scheduler.give_ownership(ANA, "core", "alpha")
    assert write("beta", "later.md", "a seat again") == "tmp/lines/beta/later.md"
    tree = world.root / TREE
    assert (tree / "tmp/notes.md").read_text() == "as the owner"
    assert (world.scratch("beta") / "notes.md").read_text() == "as a seat"
    assert (world.scratch("beta") / "later.md").read_text() == "a seat again"
    assert (world.scratch("alpha") / "notes.md").read_text() == "as a seat"


def test_write_access_without_a_writing_tool_has_a_scratch_of_its_own(
    world: World,
) -> None:
    """Review of ebedeed, G3. Write access alone does not make a line a writer, so a
    line with it and no writing tool has its own scratch, unless it owns its role."""
    parts = world.scheduler.scratch_parts
    own = (SCRATCH_DIRECTORY, LINES_SCRATCH_DIRECTORY)
    shell = LineConfig(workdir=OTHER, builtin_tools=("Bash",))
    world.scheduler.create_line("hotel", HAIKU, Strategy.SUBSCRIPTION, shell)
    world.scheduler.create_line("india", LUNA, Strategy.SUBSCRIPTION, shell)
    for line_id in ("delta", "hotel", "india"):
        config = world.scheduler.effective_config(line_id)
        assert config.workdir_access is WorkdirAccess.WRITE
        assert not set(config.builtin_tools) & {"Edit", "Write"}
    # On Claude Code a shell writes, and on Codex it does not.
    assert (parts("delta"), parts("hotel"), parts("india")) == (
        (*own, "delta"),
        (SCRATCH_DIRECTORY,),
        (*own, "india"),
    )
    assert world.scheduler.write_scratch("india", "x", "y") == "tmp/lines/india/x"
    # An owner with write access and no writing tool keeps the owner's scratch.
    assert world.scheduler.effective_config("alpha").builtin_tools == ()
    assert parts("alpha") == (SCRATCH_DIRECTORY,)


def test_the_tool_says_where_each_scratch_is(world: World) -> None:
    tool = next(
        t for t in world.scheduler.own_tools("beta") if t.name == "write_scratch"
    )
    assert "tmp/lines/<your line id>/" in tool.description
    assert "The owner's scratch is tmp/" in tool.description


# N2, the Codex read profile.


def test_the_own_scratch_is_made_without_following_a_link(tmp_path: Path) -> None:
    made = own_scratch(tmp_path, "beta")
    assert made == tmp_path / "tmp/lines/beta" and made.is_dir()
    assert own_scratch(tmp_path, "beta") == made
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "tmp/lines").rename(tmp_path / "moved")
    (tmp_path / "tmp/lines").symlink_to(outside)
    with pytest.raises(EngineRefused, match="own scratch cannot be made"):
        own_scratch(tmp_path, "beta")
    assert list(outside.iterdir()) == []
    with pytest.raises(EngineRefused, match="cannot open the working directory"):
        own_scratch(tmp_path / "missing", "beta")


def test_a_codex_read_line_is_built_with_its_own_scratch(tmp_path: Path) -> None:
    (tmp_path / TREE).mkdir(parents=True)
    build = engine_factory(str(tmp_path))
    reader = LineConfig(workdir=TREE, workdir_access=WorkdirAccess.READ)
    engine = build(LineSpec("beta", LUNA, Strategy.SUBSCRIPTION, reader), [])
    assert isinstance(engine, CodexLine)
    assert engine.scratch is not None
    assert os.path.realpath(engine.scratch) == os.path.realpath(
        tmp_path / TREE / "tmp/lines/beta"
    )
    assert engine.scratch.is_dir()
    writer = LineConfig(workdir=TREE, builtin_tools=("Edit",))
    engine = build(LineSpec("alpha", LUNA, Strategy.SUBSCRIPTION, writer), [])
    assert isinstance(engine, CodexLine) and engine.scratch is None
    assert not (tmp_path / TREE / "tmp/lines/alpha").exists()


def test_a_codex_shell_with_write_access_is_granted_only_its_own_scratch(
    tmp_path: Path,
) -> None:
    """Review of ebedeed, G3. Write access with no writing tool leaves a Codex
    workspace read, so the line is built and granted its own scratch, as
    ``write_scratch`` gives it, and nothing else of the workdir."""
    import tomllib

    from dromon.codex import checked_scratch, server_arguments

    (tmp_path / TREE).mkdir(parents=True)
    build = engine_factory(str(tmp_path))
    shell = LineConfig(workdir=TREE, builtin_tools=("Bash",))
    assert shell.workdir_access is WorkdirAccess.WRITE
    engine = build(LineSpec("india", LUNA, Strategy.SUBSCRIPTION, shell), [])
    assert isinstance(engine, CodexLine) and engine.scratch is not None
    grant = checked_scratch(engine.cwd, engine.scratch)
    arguments = server_arguments([], shell, (), scratch=grant)
    profile = next(a for a in arguments if a.startswith("permissions."))
    text = profile.split("=", 1)[1]
    filesystem = tomllib.loads(f"profile = {text}")["profile"]["filesystem"]
    assert filesystem[":workspace_roots"] == {".": "read", ".git": "read"}
    written = sorted(k for k, v in filesystem.items() if v == "write")
    assert written == [os.path.realpath(tmp_path / TREE / "tmp/lines/india")]


# N3, a turn begun while the role was vacant.


def test_a_turn_writes_as_the_owner_or_as_an_unconfined_seat_of_a_vacant_role(
    world: World,
) -> None:
    writes = world.scheduler._turn_writes  # pyright: ignore[reportPrivateUsage]
    assert [writes(line) for line in ("alpha", "beta", "gina", "delta")] == [
        True,
        False,
        False,
        False,
    ]
    world.scheduler.give_ownership(ANA, "core", None)
    assert [writes(line) for line in ("alpha", "beta", "gina", "delta")] == [
        False,
        False,
        True,
        False,
    ]


def test_a_new_owner_waits_for_an_unconfined_seats_turn_begun_while_vacant(
    vacant: World,
) -> None:
    """The review's failure scenario. Before the fix alpha started beside gina."""
    gate = asyncio.Event()
    vacant.gates["gina"] = gate

    async def act() -> None:
        vacant.scheduler.send(ANA, LineAddress("gina"), "go on")
        await until(lambda: "gina started" in vacant.events)
        vacant.scheduler.take_ownership("alpha", "the role is vacant")
        assert vacant.log.state.roles["core"].owner == "alpha"
        vacant.scheduler.send(ANA, LineAddress("alpha"), "go on")
        for _ in range(50):
            await asyncio.sleep(0)
        assert "alpha started" not in vacant.events
        assert vacant.scheduler._waits_for_writer("alpha")  # pyright: ignore[reportPrivateUsage]
        gate.set()
        await until(lambda: "alpha ended" in vacant.events)

    run(vacant, act)
    assert vacant.events.index("gina ended") < vacant.events.index("alpha started")
    # Gina's next turn waits while alpha owns the role.
    assert vacant.scheduler.waits_unconfined("gina")


def test_a_new_owner_waits_for_an_unconfined_seats_kept_connection(
    vacant: World,
) -> None:
    """A kept connection of a seat built while the role was vacant closes before
    the new owner's first turn, as a former owner's does."""
    vacant.kept.add("gina")

    async def act() -> None:
        vacant.scheduler.send(ANA, LineAddress("gina"), "go on")
        await until(lambda: "gina ended" in vacant.events)
        await vacant.scheduler.idle()
        vacant.scheduler.take_ownership("alpha", "the role is vacant")
        vacant.scheduler.send(ANA, LineAddress("alpha"), "go on")
        await until(lambda: "alpha ended" in vacant.events)

    run(vacant, act)
    assert vacant.events.index("gina retired") < vacant.events.index("alpha started")


def test_a_confined_seat_of_a_vacant_role_does_not_hold_the_new_owner(
    vacant: World,
) -> None:
    """Beta runs on Claude Code, which builds it read only, so alpha does not wait
    for its turn."""
    gate = asyncio.Event()
    vacant.gates["beta"] = gate

    async def act() -> None:
        vacant.scheduler.send(ANA, LineAddress("beta"), "go on")
        await until(lambda: "beta started" in vacant.events)
        vacant.scheduler.take_ownership("alpha", "the role is vacant")
        assert not vacant.scheduler._waits_for_writer("alpha")  # pyright: ignore[reportPrivateUsage]
        gate.set()

    run(vacant, act)


# N4, nested worktrees.


def test_overlapping_is_equality_or_nesting_either_way() -> None:
    root = Path("/p")
    assert overlapping(root, root)
    assert overlapping(root, root / "a")
    assert overlapping(root / "a" / "b", root / "a")
    assert not overlapping(root / "a", root / "ab")
    assert not overlapping(root / "a", root / "b")


def test_shared_worktree_finds_a_nested_pair_by_identifier() -> None:
    root = Path("/p")
    assert shared_worktree({"b": root / "x", "a": root / "y"}) is None
    assert shared_worktree({"c": root / "x", "b": root / "x" / "in", "a": root}) == (
        "a",
        "b",
    )
    assert "one inside the other" in shared_worktree_refusal("b", "a", "x", True)
    assert "would share the worktree x" in shared_worktree_refusal("b", "a", "x")


def test_the_nested_refusals_read_in_full() -> None:
    """Review of ebedeed, G4. The whole text of each nested refusal, since an
    operator acts on it, beside the equal case it extends."""
    assert shared_worktree_refusal("docs", "core", "tmp/worktrees/core", True) == (
        "roles core and docs would have worktrees one inside the other,"
        " tmp/worktrees/core among them, so two owners could write the inner one."
        " Give each role a worktree outside the other's"
    )
    assert shared_worktree_refusal("docs", "core", "tmp/worktrees/core") == (
        "roles core and docs would share the worktree tmp/worktrees/core, so two"
        " owners could write it. Give each role its own worktree"
    )
    root = Path("/p")
    places = {"core": RolePlace(TREE, root / TREE, "alpha")}
    nested = placement_refusal(
        "gamma", LinePlace(f"{TREE}/sub", root / TREE / "sub", True, None), places
    )
    assert nested == (
        f"line gamma could write {TREE}/sub, which overlaps {TREE}, the worktree of"
        " role core, which alpha owns, and is not a seat of the role. Make it a seat"
        " with dromon role update core, configure it with workdir_access read, or"
        " give it another workdir"
    )
    equal = placement_refusal("gamma", LinePlace(TREE, root / TREE, True, None), places)
    assert equal == (
        f"line gamma could write {TREE}, the worktree of role core, which alpha owns,"
        " and is not a seat of the role. Make it a seat with dromon role update core,"
        " configure it with workdir_access read, or give it another workdir"
    )


def test_a_writer_holding_or_inside_an_owned_worktree_is_refused() -> None:
    root = Path("/p")
    places = {"core": RolePlace(TREE, root / TREE, "alpha")}
    for directory, written in ((root, "."), (root / TREE / "sub", f"{TREE}/sub")):
        refusal = placement_refusal(
            "gamma", LinePlace(written, directory, True, None), places
        )
        assert refusal is not None
        assert f"which overlaps {TREE}, the worktree of role core" in refusal
        reader = LinePlace(written, directory, False, None)
        assert placement_refusal("gamma", reader, places) is None
    vacant = {"core": RolePlace(TREE, root / TREE, None)}
    writer = LinePlace(".", root, True, None)
    assert placement_refusal("gamma", writer, vacant) is None
    beside = LinePlace(OTHER, root / OTHER, True, None)
    assert placement_refusal("gamma", beside, places) is None
