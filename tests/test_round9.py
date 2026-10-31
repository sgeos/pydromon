"""Round 9, the known issues: a fresh session after a reload, the shared working
directory warning, the own tools of an allowlisted line, staleness judged on code,
the read-only replies query, an attached client whose daemon is gone, and the
launcher test's wait. Every engine is scripted, and git runs only in scratch
repositories under ``tmp_path``."""

import argparse
import asyncio
import json
import os
import subprocess
from collections.abc import Generator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import PermissionResultAllow, SdkMcpTool, ToolPermissionContext

from dromon import cli, codetree
from dromon.daemon import Daemon
from dromon.driver import Driver, Strategy
from dromon.engine import ClaudeCodeLine, TurnReport, permission_gate
from dromon.eventlog import EventLog
from dromon.lineconfig import (
    LineConfig,
    can_write_workdir,
    config_from_document,
)
from dromon.limits import Limits
from dromon.model import (
    LineAddress,
    LineCreated,
    LineSessionCleared,
    MessageDelivered,
    MessageKind,
    MessageRead,
    MessageSent,
    OperatorAddress,
    OperatorRegistered,
)
from dromon.paths import project_paths
from dromon.protocol import Operation, Response
from dromon.scheduler import ASK_TOOL_NAME, LineEngine, LineSpec, Scheduler
from dromon.ui import model as ui_model
from dromon.ui import terminal
from dromon.ui.link import SocketDaemon
from dromon.ui.model import Lost, Tick
from dromon.ui.runtime import Runtime, Unreachable, detached_status
from dromon.ui.update import initial, update

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")
ALPHA = LineAddress("alpha")
START = datetime(2026, 10, 31, 9, 0, 0, tzinfo=UTC)
BIG = 900_000
"""A context estimate far past any warning."""


# Item 1. A fresh session after a reload.


class Scripted:
    """An engine that records each prompt and reports a large context, so a line's
    estimate is past its warning after every turn."""

    def __init__(self, spec: LineSpec) -> None:
        self.session_id = spec.session_id
        self.prompts: list[str] = []

    async def run_turn(self, prompt: str) -> TurnReport:
        self.prompts.append(prompt)
        self.session_id = f"session-{len(self.prompts)}"
        return TurnReport(
            "success",
            1,
            "ok",
            (),
            None,
            None,
            self.session_id,
            None,
            0.1,
            context_tokens=BIG,
        )


class World:
    def __init__(
        self, path: Path, root: Path, daemon_commit: str | None = None
    ) -> None:
        self.root = root
        self.log = EventLog(path)
        self.specs: list[LineSpec] = []
        self.engines: list[Scripted] = []
        self.scheduler = Scheduler(
            self.log,
            self.build,
            tick_seconds=0,
            wall_clock=lambda: START,
            limits=Limits(max_autonomous_turns=20, max_cost_usd=10.0),
            root=root,
            protected=(root / ".dromon",),
            daemon_commit=daemon_commit,
            daemon_checkout=None if daemon_commit is None else root / "src" / "dromon",
        )
        self.scheduler.ensure_operator("ana")
        self.scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Scripted:
        del tools
        self.specs.append(spec)
        engine = Scripted(spec)
        self.engines.append(engine)
        return engine

    def run(self, *bodies: str) -> None:
        async def go() -> None:
            stop = asyncio.Event()
            loop = asyncio.create_task(self.scheduler.run(stop))
            for body in bodies:
                self.scheduler.send(ANA, ALPHA, body)
            await asyncio.sleep(0)
            await self.scheduler.idle()
            stop.set()
            await loop

        asyncio.run(go())

    def last_prompt(self) -> str:
        return self.engines[-1].prompts[-1]

    def cleared(self) -> int:
        return sum(isinstance(entry, LineSessionCleared) for entry in self.events())

    def events(self) -> list[object]:
        from dromon.eventlog import read_entries

        return [entry.event for entry in read_entries(self.log.path)]


@pytest.fixture
def world(tmp_path: Path) -> Generator[World]:
    root = tmp_path.resolve() / "project"
    (root / ".dromon").mkdir(parents=True)
    target = World(tmp_path / "events.sqlite", root)
    yield target
    target.log.close()


WARNING = "past this line's warning"


def test_a_reload_to_fresh_sessions_clears_the_session_and_its_context_at_once(
    world: World,
) -> None:
    """The first turn after the change starts a new session with no warning about
    the old session's context, and ``dromon settings`` shows no session as soon as
    the change is applied, not only after the next turn has begun."""
    world.run("the task")
    assert world.log.state.lines["alpha"].engine_session == "session-1"
    world.run("again")
    assert WARNING in world.last_prompt()
    before = world.cleared()
    world.scheduler.configure_line("alpha", LineConfig(fresh_session_each_turn=True))
    assert world.log.state.lines["alpha"].engine_session is None
    assert world.cleared() == before + 1
    assert world.scheduler.context_status("alpha")["estimated_tokens"] is None
    world.run("the next brief")
    assert world.specs[-1].session_id is None
    assert WARNING not in world.last_prompt()


def test_reconfiguring_a_line_already_fresh_records_no_second_clear(
    world: World,
) -> None:
    world.scheduler.configure_line("alpha", LineConfig(fresh_session_each_turn=True))
    world.run("one")
    before = world.cleared()
    world.scheduler.configure_line(
        "alpha", LineConfig(fresh_session_each_turn=True, effort="low")
    )
    assert world.cleared() == before


def test_a_cleared_or_rebound_session_forgets_the_old_context(world: World) -> None:
    world.run("the task")
    assert world.scheduler.context_status("alpha")["estimated_tokens"] == BIG
    world.scheduler.clear_session("alpha")
    assert world.scheduler.context_status("alpha")["estimated_tokens"] is None
    world.run("carry on")
    assert world.scheduler.context_status("alpha")["estimated_tokens"] == BIG
    world.scheduler.rebind_line("alpha", "claude-opus-5-5", Strategy.SUBSCRIPTION)
    assert world.scheduler.context_status("alpha")["estimated_tokens"] is None


def test_a_line_in_a_turn_is_left_for_its_next_turn_to_clear(world: World) -> None:
    """A running turn records the session it ran in when it ends, so the change
    clears nothing then, and the next turn's start does."""
    world.run("the task")

    async def go() -> None:
        stop = asyncio.Event()
        loop = asyncio.create_task(world.scheduler.run(stop))
        world.scheduler.send(ANA, ALPHA, "work")
        for _ in range(50):
            await asyncio.sleep(0)
            if "alpha" in world.scheduler._running:  # pyright: ignore[reportPrivateUsage]
                world.scheduler.configure_line(
                    "alpha", LineConfig(fresh_session_each_turn=True)
                )
                assert world.log.state.lines["alpha"].engine_session is not None
                break
        await world.scheduler.idle()
        stop.set()
        await loop

    asyncio.run(go())
    world.run("next")
    assert world.specs[-1].session_id is None


# Item 2. The shared working directory warning.


@pytest.mark.parametrize(
    ("document", "writes"),
    [
        ({"builtin_tools": ["Read", "Write"]}, True),
        ({"builtin_tools": ["Bash"]}, True),
        ({"tool_policy": "allow_all"}, True),
        ({"tool_policy": "allowlist", "allowed_tools": ["Edit"]}, True),
        ({"builtin_tools": ["Read", "Grep"]}, False),
        ({"builtin_tools": ["Read", "Bash"], "workdir_access": "read"}, False),
        ({"tool_policy": "allow_all", "workdir_access": "read"}, False),
        (
            {
                "tool_policy": "allowlist",
                "allowed_tools": ["mcp__game__*"],
                "mcp_servers": {"game": {"type": "stdio", "command": "/bin/true"}},
            },
            False,
        ),
    ],
)
def test_a_line_writes_its_workdir_only_with_write_access_and_a_file_tool(
    document: dict[str, object], writes: bool
) -> None:
    assert can_write_workdir(config_from_document(document)) is writes


def test_dromon_config_warns_only_about_lines_that_can_write(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shape the defect was found in: a reader with a shell, a reviewer with a
    shell, a player with only an MCP server's tools, all at the project root, and
    one writer. None of the others can write, so nothing is warned of. A second
    writer is."""
    state = tmp_path / ".dromon"
    state.mkdir()
    lines = (
        "version = 1\n"
        "[lines.research]\nmodel = 'claude-opus-5-5'\n"
        "builtin_tools = ['Read', 'Bash']\nworkdir_access = 'read'\n"
        "[lines.review]\nmodel = 'claude-opus-5-5'\n"
        "builtin_tools = ['Read', 'Grep', 'Bash']\nworkdir_access = 'read'\n"
        "[lines.player]\nmodel = 'claude-haiku-5-5'\ntool_policy = 'allowlist'\n"
        "allowed_tools = ['mcp__game__*']\n"
        "[lines.player.mcp_servers.game]\ntype = 'stdio'\ncommand = '/bin/true'\n"
        "[lines.builder]\nmodel = 'claude-opus-5-5'\nbuiltin_tools = ['Edit']\n"
    )
    (state / "dromon.toml").write_text(lines)
    runtime = ["--runtime-dir", str(short_runtime)]
    assert cli.main(["--project", str(tmp_path), *runtime, "config"]) == 0
    assert "warning:" not in capsys.readouterr().out
    (state / "dromon.toml").write_text(
        lines + "[lines.other]\nmodel = 'claude-haiku-5-5'\nbuiltin_tools = ['Write']\n"
    )
    assert cli.main(["--project", str(tmp_path), *runtime, "config"]) == 0
    assert "warning: writing lines builder, other share workdir" in (
        capsys.readouterr().out
    )


# Item 3. The own tools of an allowlisted line with one MCP server.


def test_an_allowlisted_line_with_one_servers_tools_is_given_ask_operator(
    tmp_path: Path,
) -> None:
    """Not confirmed as a defect. The line's own tools always carry ``ask_operator``,
    its engine serves every one of them, its init check accepts it, and its gate
    allows it, under the configuration the defect was reported for. This pins that
    account, so a change that drops it fails here."""
    document: dict[str, object] = {
        "tool_policy": "allowlist",
        "allowed_tools": ["mcp__game__*"],
        "keep_session": True,
        "allow_compact": True,
        "recipients": ["@operator"],
        "mcp_servers": {"game": {"type": "stdio", "command": "/bin/true"}},
    }
    config = config_from_document(document)
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(log, lambda spec, tools: Scripted(spec))
        scheduler.create_line("player", HAIKU, Strategy.SUBSCRIPTION, config)
        tools = scheduler.own_tools("player")
    assert ASK_TOOL_NAME in {tool.name for tool in tools}
    target = ClaudeCodeLine(
        Driver.for_model(HAIKU, Strategy.SUBSCRIPTION),
        tmp_path,
        config=config,
        mcp_tools=tools,
        environment={},
    )
    name = f"mcp__dromon__{ASK_TOOL_NAME}"
    assert name in target.own_tools
    options = target.options()
    assert isinstance(options.mcp_servers, dict)
    assert {"dromon", "game"} <= set(options.mcp_servers)
    assert target._offered_as_configured(name)  # pyright: ignore[reportPrivateUsage]
    gate = permission_gate(config, target.own_tools)
    allowed = asyncio.run(gate(name, {}, ToolPermissionContext()))
    assert isinstance(allowed, PermissionResultAllow)


# Item 4. Staleness judged on code, and a daemon older than a worktree.


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
            "-c",
            "core.hooksPath=/dev/null",
            *arguments,
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env=environment,
    )
    return done.stdout.strip()


def commit(repository: Path, path: str, text: str, message: str) -> str:
    target = repository / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    git(repository, "add", "-A")
    git(repository, "commit", "-q", "-m", message)
    return git(repository, "rev-parse", "HEAD")


class Repository:
    """A scratch repository whose commits are, in order, the loaded code, a change
    of documents only, and a change of code."""

    def __init__(self, root: Path, seed: str = "one\n") -> None:
        root.mkdir(parents=True)
        git(root, "init", "-q")
        (root / ".dromon").mkdir()
        self.root = root
        # Two repositories made alike within a second share their commits, so the
        # seed tells them apart.
        self.loaded = commit(root, "src/dromon/a.py", seed, "code")
        self.docs = commit(root, "docs/notes.md", "notes\n", "docs")
        self.code = commit(root, "src/dromon/a.py", "two\n", "more code")

    def checkout(self, revision: str) -> None:
        git(self.root, "checkout", "-q", "--detach", revision)


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    return Repository(tmp_path.resolve() / "project")


def test_code_trees_compare_code_and_ancestry_and_refuse_bad_names(
    repository: Repository,
) -> None:
    trees = codetree.CodeTrees()
    root = repository.root
    assert codetree.package_path(root / "src" / "dromon") == "src/dromon"
    assert codetree.package_path(root) == "."
    assert trees.same_code(root, repository.loaded, repository.docs, "src/dromon")
    assert not trees.same_code(root, repository.loaded, repository.code, "src/dromon")
    assert trees.is_ancestor(root, repository.loaded, repository.code) is True
    assert trees.is_ancestor(root, repository.code, repository.loaded) is False
    assert trees.is_ancestor(root, "f" * 40, repository.code) is None
    assert trees.tree(root, "--help", "src") is None
    assert trees.tree(root, repository.loaded, "../outside") is None
    assert trees.tree(root, repository.loaded, "-x") is None


def test_code_trees_cache_answers_and_ask_again_after_a_timeout(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    real = codetree._git  # pyright: ignore[reportPrivateUsage]

    def counted(directory: Path, *arguments: str) -> Any:  # noqa: ANN401
        calls.append(arguments)
        return real(directory, *arguments)

    monkeypatch.setattr(codetree, "_git", counted)
    trees = codetree.CodeTrees()
    for _ in range(3):
        trees.same_code(repository.root, repository.loaded, repository.code, "src")
    assert len(calls) == 2

    def slow(directory: Path, *arguments: str) -> Any:  # noqa: ANN401
        calls.append(arguments)
        raise codetree._Timeout  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(codetree, "_git", slow)
    calls.clear()
    assert trees.tree(repository.root, repository.docs, "src") is None
    assert trees.tree(repository.root, repository.docs, "src") is None
    assert len(calls) == 2


def test_code_trees_stay_bounded(repository: Repository) -> None:
    trees = codetree.CodeTrees()
    for index in range(codetree.MAX_CACHED + 3):
        trees.tree(repository.root, f"{index:040x}", "src")
    assert len(trees._trees) == codetree.MAX_CACHED  # pyright: ignore[reportPrivateUsage]


def staleness(repository: Repository) -> Generator[World]:
    target = World(
        repository.root.parent / "events.sqlite", repository.root, repository.loaded
    )
    yield target
    target.log.close()


@pytest.fixture
def stale_world(repository: Repository) -> Generator[World]:
    yield from staleness(repository)


def test_the_daemon_is_stale_only_when_its_code_moved(
    repository: Repository, stale_world: World
) -> None:
    """Decided by core for review. A move of documents is not stale, a move of code
    is, and moving back to the loaded code clears it."""
    scheduler = stale_world.scheduler
    # The fixture's checkout is at the code change, which the daemon has not loaded.
    status = scheduler.daemon_status()
    assert status["checkout_commit"] == repository.code
    assert (status["stale"], status["code_compared"], status["package"]) == (
        True,
        True,
        "src/dromon",
    )
    note = str(status["stale_note"])
    assert "own checkout, not your worktree" in note and "src/dromon" in note
    repository.checkout(repository.docs)
    status = scheduler.daemon_status()
    assert (status["stale"], status["code_compared"]) == (False, True)
    assert status["stale_note"] is None
    repository.checkout(repository.loaded)
    assert scheduler.daemon_status()["stale"] is False


def test_staleness_falls_back_to_a_moved_head_when_code_cannot_be_compared(
    repository: Repository, stale_world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unknown(directory: Path) -> str | None:
        del directory
        return None

    monkeypatch.setattr(codetree, "package_path", unknown)
    repository.checkout(repository.docs)
    status = stale_world.scheduler.daemon_status()
    assert (status["stale"], status["code_compared"]) == (True, False)
    assert "could not be read" in str(status["stale_note"])
    cite = stale_world.scheduler.commits("alpha")["cite"]
    assert str(cite).endswith("with its code not compared.")


def test_line_status_marks_a_daemon_older_than_the_worktree(
    repository: Repository, stale_world: World
) -> None:
    """The line works at the project root, the daemon's own repository. At the code
    change the daemon is behind it, and at the change of documents it is not."""
    repository.checkout(repository.code)
    commits = stale_world.scheduler.commits("alpha")
    assert commits["daemon_behind_worktree"] is True
    assert "is older than the worktree's" in str(commits["cite"])
    full = cast(dict[str, Any], stale_world.scheduler.line_status("alpha"))
    tool = next(
        tool
        for tool in stale_world.scheduler.own_tools("alpha")
        if tool.name == "line_status"
    )

    async def status() -> dict[str, Any]:
        return await tool.handler({})

    compact = json.loads(cast(str, asyncio.run(status())["content"][0]["text"]))
    assert compact["commits"]["daemon_behind_worktree"] is True
    assert compact["commits"]["cite"] == full["commits"]["cite"]
    repository.checkout(repository.docs)
    assert stale_world.scheduler.daemon_behind("alpha") is False
    repository.checkout(repository.loaded)
    assert stale_world.scheduler.daemon_behind("alpha") is False


def test_a_worktree_of_another_repository_is_not_compared(
    tmp_path: Path, repository: Repository
) -> None:
    other = Repository(tmp_path.resolve() / "other", seed="another\n")
    with EventLog(tmp_path / "events.sqlite") as log:
        scheduler = Scheduler(
            log,
            lambda spec, tools: Scripted(spec),
            root=other.root,
            protected=(other.root / ".dromon",),
            daemon_commit=repository.loaded,
            daemon_checkout=repository.root / "src" / "dromon",
        )
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        assert scheduler.daemon_behind("alpha") is None


def test_the_prompt_says_once_per_commit_that_the_daemon_is_older(
    repository: Repository, stale_world: World
) -> None:
    repository.checkout(repository.code)
    stale_world.run("one")
    told = "The daemon runs code from"
    assert told in stale_world.last_prompt()
    assert "src/dromon" in stale_world.last_prompt()
    stale_world.run("two")
    assert told not in stale_world.last_prompt()
    newer = commit(repository.root, "src/dromon/b.py", "three\n", "newer code")
    stale_world.run("three")
    assert told in stale_world.last_prompt() and newer[:12] in stale_world.last_prompt()


# Item 5. The read-only replies query.


def replies_log(path: Path) -> None:
    """A log in which @ana asked alpha something, alpha replied twice, @ana read the
    first reply, and alpha sent @ana one note that replies to nothing."""
    with EventLog(path) as log:
        log.append(LineCreated("alpha", HAIKU, "subscription"))
        log.append(OperatorRegistered("ana"))
        log.append(MessageSent("m-q", ANA, ALPHA, MessageKind.NOTE, "status?"))
        log.append(MessageDelivered("m-q"))
        for message_id, body in (("m-1", "first"), ("m-2", "second\nline")):
            log.append(
                MessageSent(
                    message_id, ALPHA, ANA, MessageKind.NOTE, body, reply_to="m-q"
                )
            )
        log.append(MessageDelivered("m-1"))
        log.append(MessageRead("m-1"))
        log.append(MessageSent("m-3", ALPHA, ANA, MessageKind.NOTE, "aside"))


def held_daemon(paths: Any, log: EventLog) -> Daemon:  # noqa: ANN401
    def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
        raise AssertionError(spec.line_id)

    return Daemon(
        paths, log, "2026-10-31T00:00:00Z", Scheduler(log, no_engine, hold_wakes=True)
    )


def entries(response: Response) -> list[dict[str, Any]]:
    assert response.result is not None
    return cast(list[dict[str, Any]], response.result["messages"])


def ask(daemon: Daemon, op: str, args: dict[str, object]) -> Response:
    line = json.dumps({"id": "r", "op": op, "v": 1, "args": args}).encode()
    return daemon.respond(line + b"\n")


def test_replies_gives_the_bodies_and_marks_nothing_read(
    tmp_path: Path, short_runtime: Path
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    replies_log(paths.event_log)
    with EventLog(paths.event_log) as log:
        daemon = held_daemon(paths, log)
        response = ask(daemon, "replies", {"operator": "ana", "message_id": "m-q"})
        assert response.ok and response.result is not None
        result = response.result
        assert (result["total"], result["replies"], result["unread"]) == (2, 2, 1)
        messages = entries(response)
        assert [(m["message_id"], m["body"], m["read"]) for m in messages] == [
            ("m-1", "first", True),
            ("m-2", "second\nline", False),
        ]
        assert log.state.messages["m-2"].read is False
        # Listing delivers, as mail does, which is not reading.
        assert log.state.messages["m-2"].delivered is True
        again = ask(
            daemon,
            "replies",
            {"operator": "ana", "message_id": "m-q", "unread_only": True},
        )
        assert again.result is not None
        assert [m["message_id"] for m in entries(again)] == ["m-2"]
        assert again.result["unread"] == 1


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ({"operator": "ana"}, "with operator and message_id"),
        ({"operator": "ana", "message_id": "m-q", "x": 1}, "replies takes"),
        ({"operator": "ana", "message_id": "m-none"}, "no message m-none"),
        ({"operator": "ana", "message_id": "m-q", "limit": 0}, "limit in"),
        ({"operator": "ana", "message_id": "m-q", "offset": -1}, "offset must"),
        ({"operator": "ana", "message_id": "m-q", "unread_only": 1}, "boolean"),
    ],
)
def test_replies_refuses_malformed_arguments(
    tmp_path: Path, short_runtime: Path, args: dict[str, object], reason: str
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    replies_log(paths.event_log)
    with EventLog(paths.event_log) as log:
        response = ask(held_daemon(paths, log), "replies", args)
        assert not response.ok
        assert reason in (response.error_message or "")
        assert log.state.messages["m-2"].read is False


def test_replies_withholds_bodies_past_its_budget(
    tmp_path: Path, short_runtime: Path
) -> None:
    from dromon.daemon import REPLIES_BUDGET_BYTES

    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    replies_log(paths.event_log)
    large = "\x01" * (REPLIES_BUDGET_BYTES // 6 + 1)
    with EventLog(paths.event_log) as log:
        log.append(
            MessageSent("m-4", ALPHA, ANA, MessageKind.NOTE, large, reply_to="m-q")
        )
        response = ask(
            held_daemon(paths, log), "replies", {"operator": "ana", "message_id": "m-q"}
        )
        assert response.result is not None
        last = entries(response)[-1]
        assert (last["message_id"], last["body"]) == ("m-4", None)
        assert last["body_bytes"] == len(large.encode("utf-8"))


def mail_arguments(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "operator": "ana",
        "unread": False,
        "sender": None,
        "offset": 0,
        "limit": 20,
        "replies_to": "m-q",
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_dromon_mail_replies_to_prints_bodies_through_the_replies_request(
    tmp_path: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    asked: list[tuple[Operation, dict[str, object]]] = []

    def fake_call(
        paths: object, name: str, operation: Operation, args: dict[str, object]
    ) -> dict[str, object]:
        asked.append((operation, args))
        return {
            "message_id": "m-q",
            "total": 2,
            "replies": 2,
            "unread": 1,
            "messages": [
                {
                    "message_id": "m-1",
                    "sender": "alpha",
                    "kind": "note",
                    "read": True,
                    "outcome": "unanswered",
                    "body": "first\nsecond",
                    "body_bytes": 12,
                },
                {
                    "message_id": "m-2",
                    "sender": "alpha",
                    "kind": "note",
                    "read": False,
                    "outcome": "unanswered",
                    "body": None,
                    "body_bytes": 99_999,
                },
            ],
        }

    monkeypatch.setattr(cli, "_call", fake_call)
    paths = project_paths(tmp_path, short_runtime)
    assert cli._mail(paths, mail_arguments()) == 0  # pyright: ignore[reportPrivateUsage]
    assert asked == [
        (
            Operation.REPLIES,
            {
                "operator": "ana",
                "message_id": "m-q",
                "unread_only": False,
                "offset": 0,
                "limit": 20,
            },
        )
    ]
    out = capsys.readouterr().out
    assert out.startswith("2 matching of 2 replies to m-q, 1 unread\n")
    assert "m-1 from alpha, note, read\n  first\n  second\n" in out
    assert "dromon read m-2 opens it and marks it read" in out
    assert cli._mail(paths, mail_arguments(sender="alpha")) == 2  # pyright: ignore[reportPrivateUsage]
    assert "does not combine" in capsys.readouterr().err


def test_the_parser_takes_replies_to() -> None:
    parsed = cli.build_parser().parse_args(["mail", "--replies-to", "m-q", "--unread"])
    assert (parsed.replies_to, parsed.unread) == ("m-q", True)


# Item 6. An attached client whose daemon is gone.


class Gone:
    """A daemon port that finds nothing listening, or is answered once ``back``."""

    def __init__(self) -> None:
        self.back = False
        self.calls = 0

    def call(self, operation: Operation, args: dict[str, object]) -> Response:
        del operation, args
        self.calls += 1
        if not self.back:
            raise Unreachable("the daemon is not running", gone=True)
        return Response("r", ok=False, error_message="refused")


def test_a_client_detaches_once_its_daemon_has_been_gone_for_its_bound() -> None:
    daemon = Gone()
    client = Runtime(daemon, "alpha", "ana", 1.0)
    now = 0.0
    polls = 0
    while not client.finished and polls < 100:
        client.tick(now)
        polls += 1
        now += 1.0
    assert client.finished
    # Both bounds hold: at least LOST_POLLS polls and at least LOST_SECONDS.
    assert polls >= ui_model.LOST_POLLS
    assert now - 1.0 >= ui_model.LOST_SECONDS
    message = client.exit_message
    assert message is not None and "the daemon is not running" in message
    assert "so the client detached" in message


def test_one_poll_counts_once_and_a_short_interval_waits_out_the_seconds() -> None:
    model = initial("alpha", "ana", 0.1)
    model, _ = update(model, Tick(0.0))
    for _ in range(10):
        model, _ = update(model, Lost("gone"))
    assert model.lost_polls == 1
    now = 0.0
    for _ in range(ui_model.LOST_POLLS * 2):
        now += 0.25
        model, _ = update(model, Tick(now))
        model, _ = update(model, Lost("gone"))
    assert model.lost_polls > ui_model.LOST_POLLS and not model.finished
    model, _ = update(model, Tick(ui_model.LOST_SECONDS + 0.5))
    model, _ = update(model, Lost("gone"))
    assert model.finished


def test_a_daemon_that_answers_again_resets_the_count() -> None:
    from dromon.ui.model import LinesLoaded

    model = initial("alpha", "ana", 1.0)
    for second in range(ui_model.LOST_POLLS - 1):
        model, _ = update(model, Tick(float(second)))
        model, _ = update(model, Lost("gone"))
    assert model.lost_polls == ui_model.LOST_POLLS - 1
    model, _ = update(model, LinesLoaded(()))
    assert (model.lost_since, model.lost_polls) == (None, 0)


def test_a_slow_daemon_never_detaches_the_client() -> None:
    from dromon.ui.model import Failed

    model = initial("alpha", "ana", 1.0)
    for second in range(100):
        model, _ = update(model, Tick(float(second)))
        model, _ = update(model, Failed("the daemon did not answer: timed out"))
    assert not model.finished and model.lost_polls == 0


def test_the_socket_port_says_gone_only_when_nothing_listens(
    tmp_path: Path, short_runtime: Path
) -> None:
    """The runtime directory does not exist, as after a project was deleted, so
    nothing listens."""
    paths = project_paths(tmp_path, short_runtime)
    with pytest.raises(Unreachable) as raised:
        SocketDaemon(paths).call(Operation.STATUS, {})
    assert raised.value.gone is True
    assert Unreachable("slow").gone is False


def test_the_detached_client_exits_with_its_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dromon import exits

    assert detached_status(None, 0) == 0
    client = Runtime(Gone(), "alpha", "ana", 1.0)
    assert detached_status(client, 0) == 0
    for second in range(30):
        client.tick(float(second))
    assert detached_status(client, 0) == exits.NOT_RUNNING
    assert capsys.readouterr().err.startswith("dromon attach: the daemon is not")


class DeadTerminal:
    """A screen whose reads fail at once, as curses does once the terminal is gone."""

    def __init__(self) -> None:
        self.reads = 0

    def getmaxyx(self) -> tuple[int, int]:
        return (8, 60)

    def erase(self) -> None:
        pass

    def addnstr(self, y: int, x: int, text: str, n: int, attr: int = 0, /) -> None:
        pass

    def move(self, y: int, x: int, /) -> None:
        pass

    def refresh(self) -> None:
        pass

    def timeout(self, delay: int, /) -> None:
        pass

    def get_wch(self) -> str | int:
        import curses

        self.reads += 1
        raise curses.error("no input")


def test_the_curses_loop_paces_dead_reads_and_ends_when_the_daemon_is_gone() -> None:
    """Every read that returns at once waits out the input timeout, and the loop
    ends by itself, where before it spun without end."""
    now = [0.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    screen = DeadTerminal()
    client = Runtime(Gone(), "alpha", "ana", 1.0)
    assert (
        terminal.loop(
            screen, client, terminal.palette(False), clock=lambda: now[0], sleep=sleep
        )
        == 0
    )
    assert client.finished and client.exit_message is not None
    assert slept and all(
        seconds == pytest.approx(terminal.INPUT_TIMEOUT_MS / 1000) for seconds in slept
    )
    assert screen.reads == len(slept)
    # The loop ran for the bound in seconds, so its passes are bounded by it.
    assert len(slept) <= (ui_model.LOST_SECONDS + 2) * 1000 / terminal.INPUT_TIMEOUT_MS
