"""A line's working directory, its adopted engine session, and the git files its
sandbox guards, from the configuration through the daemon to each engine."""

import asyncio
import json
import os
from collections.abc import Callable, Coroutine, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import cli
from dromon.codex import CodexLine
from dromon.configview import Overrides
from dromon.daemon import Daemon, check_sandboxes, protected_dirs
from dromon.driver import Driver, Strategy
from dromon.engine import ClaudeCodeLine
from dromon.eventlog import EventLog, read_entries
from dromon.grok import GrokLine, bridge_command
from dromon.lineconfig import (
    DEFAULT_CONFIG,
    ConfigError,
    LineConfig,
    config_from_document,
    decode_config,
    encode_config,
)
from dromon.model import (
    LineAddress,
    LineConfigured,
    LineCreated,
    LineSession,
    LineSessionCleared,
    OperatorAddress,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ConfigFileError, ProjectConfig, config_path, parse
from dromon.protocol import ErrorCode, Response
from dromon.sandbox import SandboxError, blocks_unsandboxed, effective, parse_sandbox
from dromon.scheduler import LineEngine, LineSpec, Scheduler, engine_factory
from dromon.engine import EngineRefused, TurnReport
from dromon.workdir import MAX_POINTER_BYTES, git_write_guards, resolve_workdir

HAIKU = "claude-haiku-5-5"
ANA = OperatorAddress("ana")


def real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def linked_worktree(base: Path) -> tuple[Path, Path, Path]:
    """A fake primary checkout with one linked worktree, laid out as git lays it out.
    Returns the primary's git directory, the worktree's git directory and the tree."""
    common = base / "primary" / ".git"
    gitdir = common / "worktrees" / "wt"
    gitdir.mkdir(parents=True)
    (common / "hooks").mkdir()
    (common / "config").write_text("[core]\n")
    (gitdir / "commondir").write_text("../..\n")
    tree = base / "trees" / "wt"
    tree.mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {gitdir}\n")
    return real(common), real(gitdir), real(tree)


# The configuration.


def test_the_configuration_carries_its_workdir_through_the_log() -> None:
    config = config_from_document({"workdir": "../trees/wt"})
    assert config.workdir == "../trees/wt"
    assert decode_config(encode_config(config)) == config
    assert "workdir" not in json.loads(encode_config(DEFAULT_CONFIG))
    tools = LineConfig(builtin_tools=("Read",))
    assert encode_config(tools) == encode_config(
        config_from_document({"builtin_tools": ["Read"]})
    )
    assert '"workdir"' not in encode_config(tools)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "workdir must be text"),
        (3, "workdir must be text"),
        ("a\x00b", "NUL"),
        ("~ana/trees", "another user's home"),
    ],
)
def test_a_malformed_workdir_is_refused(value: object, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        config_from_document({"workdir": value})


# Resolving the directory against the project.


@pytest.fixture
def project(tmp_path: Path) -> tuple[Path, tuple[Path, Path]]:
    root = real(tmp_path) / "project"
    state = root / ".dromon"
    run = state / "run"
    run.mkdir(parents=True)
    (root / "src").mkdir()
    return root, (state, run)


def test_no_workdir_is_the_project_root_unchanged(
    project: tuple[Path, tuple[Path, Path]],
) -> None:
    root, protected = project
    assert resolve_workdir(None, root, protected) is root
    assert resolve_workdir(None, Path("/nowhere"), protected) == Path("/nowhere")


def test_a_workdir_resolves_relative_absolute_and_home_paths(
    project: tuple[Path, tuple[Path, Path]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, protected = project
    assert resolve_workdir("src", root, protected) == root / "src"
    assert resolve_workdir("src/../src/", root, protected) == root / "src"
    assert resolve_workdir(str(root / "src"), root, protected) == root / "src"
    assert resolve_workdir(".", root, protected) == root
    home = real(tmp_path) / "home"
    (home / "trees").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert resolve_workdir("~/trees", root, protected) == home / "trees"
    assert resolve_workdir("~", root, protected) == home
    (root / "link").symlink_to(home / "trees")
    assert resolve_workdir("link", root, protected) == home / "trees"


@pytest.mark.parametrize(
    ("workdir", "message"),
    [
        ("missing", "not an existing directory"),
        ("file.txt", "not an existing directory"),
        (".dromon", "inside a directory Dromon protects"),
        (".dromon/run", "inside a directory Dromon protects"),
        (".dromon/run/deeper", "inside a directory Dromon protects"),
        ("..", "contains a directory Dromon protects"),
        ("into-state", "inside a directory Dromon protects"),
    ],
)
def test_a_workdir_that_is_missing_or_protected_is_refused(
    project: tuple[Path, tuple[Path, Path]], workdir: str, message: str
) -> None:
    root, protected = project
    (root / "file.txt").write_text("x")
    (root / ".dromon" / "run" / "deeper").mkdir()
    (root / "into-state").symlink_to(root / ".dromon")
    with pytest.raises(ConfigError, match=message):
        resolve_workdir(workdir, root, protected)


def test_a_workdir_may_not_contain_a_runtime_directory_kept_elsewhere(
    tmp_path: Path,
) -> None:
    root = real(tmp_path) / "project"
    elsewhere = real(tmp_path) / "elsewhere"
    runtime = elsewhere / "run"
    runtime.mkdir(parents=True)
    root.mkdir()
    protected = (root / ".dromon", runtime)
    with pytest.raises(ConfigError, match="contains a directory Dromon protects"):
        resolve_workdir(str(elsewhere), root, protected)


# Finding the git files to guard.


def test_a_plain_checkout_guards_its_hooks_and_config(tmp_path: Path) -> None:
    checkout = real(tmp_path) / "repo"
    (checkout / ".git").mkdir(parents=True)
    (checkout / "src" / "deep").mkdir(parents=True)
    git = checkout / ".git"
    expected = (git / "config", git / "config.worktree", git / "hooks")
    assert git_write_guards(checkout) == expected
    assert git_write_guards(checkout / "src" / "deep") == expected


def test_a_linked_worktree_guards_the_common_directory_and_its_pointers(
    tmp_path: Path,
) -> None:
    common, gitdir, tree = linked_worktree(tmp_path)
    assert git_write_guards(tree) == tuple(
        sorted(
            {
                common / "hooks",
                common / "config",
                gitdir / "config.worktree",
                gitdir / "commondir",
                tree / ".git",
            }
        )
    )


def test_relative_pointers_are_followed_as_git_follows_them(tmp_path: Path) -> None:
    common, gitdir, tree = linked_worktree(tmp_path)
    (tree / ".git").write_text("gitdir: ../../primary/.git/worktrees/wt\n")
    assert common / "hooks" in git_write_guards(tree)
    (gitdir / "commondir").write_text(f"{common}\n")
    assert common / "config" in git_write_guards(tree)


def test_an_absolute_commondir_names_the_common_directory(tmp_path: Path) -> None:
    _, gitdir, tree = linked_worktree(tmp_path)
    elsewhere = real(tmp_path) / "elsewhere.git"
    elsewhere.mkdir()
    (gitdir / "commondir").write_text(f"{elsewhere}\n")
    guards = git_write_guards(tree)
    assert elsewhere / "hooks" in guards and elsewhere / "config" in guards
    assert real(tmp_path) / "primary" / ".git" / "hooks" not in guards


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "does not name a git directory"),
        ("\n", "does not name a git directory"),
        ("../..\x00/x\n", "contains a NUL character"),
        ("../..\n../..\n", "more than one path"),
    ],
)
def test_a_malformed_commondir_is_refused(
    tmp_path: Path, text: str, message: str
) -> None:
    _, gitdir, tree = linked_worktree(tmp_path)
    (gitdir / "commondir").write_text(text)
    with pytest.raises(SandboxError, match=message):
        git_write_guards(tree)


def test_a_worktree_without_commondir_is_its_own_common_directory(
    tmp_path: Path,
) -> None:
    _, gitdir, tree = linked_worktree(tmp_path)
    (gitdir / "commondir").unlink()
    guards = git_write_guards(tree)
    assert gitdir / "hooks" in guards and gitdir / "config" in guards


def test_a_directory_outside_any_checkout_guards_nothing(tmp_path: Path) -> None:
    assert git_write_guards(real(tmp_path)) == ()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not a pointer\n", "does not name a git directory"),
        ("gitdir:\n", "does not name a git directory"),
        ("gitdir: a\ngitdir: b\n", "more than one path"),
        ("gitdir: a\x00b\n", "contains a NUL character"),
    ],
)
def test_a_malformed_git_file_is_refused(
    tmp_path: Path, text: str, message: str
) -> None:
    _, _, tree = linked_worktree(tmp_path)
    (tree / ".git").write_text(text)
    with pytest.raises(SandboxError, match=message):
        git_write_guards(tree)


def test_an_oversized_binary_or_dangling_pointer_is_refused(tmp_path: Path) -> None:
    _, gitdir, tree = linked_worktree(tmp_path)
    (gitdir / "commondir").write_bytes(b"x" * (MAX_POINTER_BYTES + 1))
    with pytest.raises(SandboxError, match="larger than"):
        git_write_guards(tree)
    (gitdir / "commondir").write_bytes(b"\xff\xfe")
    with pytest.raises(SandboxError, match="not UTF-8"):
        git_write_guards(tree)
    (tree / ".git").unlink()
    (tree / ".git").symlink_to(tmp_path / "gone")
    with pytest.raises(SandboxError, match="neither a git directory nor a git file"):
        git_write_guards(tree)


def test_an_unreadable_git_file_is_refused(tmp_path: Path) -> None:
    _, _, tree = linked_worktree(tmp_path)
    (tree / ".git").chmod(0)
    try:
        if os.access(tree / ".git", os.R_OK):
            pytest.skip("this user can read a file without permission bits")
        with pytest.raises(SandboxError, match="cannot read"):
            git_write_guards(tree)
    finally:
        (tree / ".git").chmod(0o600)


def test_an_enabled_sandbox_denies_writing_the_guarded_files(tmp_path: Path) -> None:
    common, _, tree = linked_worktree(tmp_path)
    guards = git_write_guards(tree)
    granted = parse_sandbox(
        {"enabled": True, "filesystem": {"allowWrite": [str(common)]}}, operator=True
    )
    document = effective(granted, tree, (), guards)
    assert document is not None
    filesystem = cast(dict[str, list[str]], document["filesystem"])
    assert set(map(str, guards)) <= set(filesystem["denyWrite"])
    assert filesystem["allowWrite"] == [str(common)]
    assert not set(map(str, guards)) & set(filesystem.get("denyRead", []))
    disabled = parse_sandbox({"enabled": False}, operator=True)
    assert effective(disabled, tree, (), guards) == {"enabled": False}


# The engines.


def haiku_spec(config: LineConfig) -> LineSpec:
    return LineSpec("alpha", HAIKU, Strategy.SUBSCRIPTION, config)


def test_a_claude_line_runs_in_its_workdir_with_its_paths_resolved_there(
    tmp_path: Path,
) -> None:
    root = real(tmp_path) / "project"
    (root / ".dromon").mkdir(parents=True)
    common, _, tree = linked_worktree(root)
    (tree / "CLAUDE.md").write_text("The worktree's rules.\n")
    protected = (root / ".dromon",)
    build = engine_factory(
        str(root),
        project_sandbox=parse_sandbox(
            {"enabled": True, "filesystem": {"allowWrite": ["out"]}}, operator=False
        ),
        protected=protected,
    )
    config = LineConfig(workdir="trees/wt", instructions=("CLAUDE.md",))
    engine = build(haiku_spec(config), [])
    assert isinstance(engine, ClaudeCodeLine)
    assert engine.cwd == tree
    assert engine.options().cwd == str(tree)
    assert (
        engine.options().system_prompt
        == "Contents of CLAUDE.md:\n\nThe worktree's rules."
    )
    sandbox = cast(dict[str, dict[str, list[str]]], engine.sandbox)
    assert sandbox["filesystem"]["allowWrite"] == [str(tree / "out")]
    assert str(common / "hooks") in sandbox["filesystem"]["denyWrite"]
    assert str(root / ".dromon") in sandbox["filesystem"]["denyRead"]
    default = build(haiku_spec(DEFAULT_CONFIG), [])
    assert isinstance(default, ClaudeCodeLine) and default.cwd == root


def test_read_access_forces_claude_confinement_and_denies_file_writes(
    tmp_path: Path,
) -> None:
    root = real(tmp_path)
    config = config_from_document(
        {
            "workdir_access": "read",
            "builtin_tools": ["Bash", "Edit", "Write"],
        }
    )
    engine = engine_factory(str(root))(haiku_spec(config), [])
    assert isinstance(engine, ClaudeCodeLine)
    sandbox = cast(dict[str, object], engine.sandbox)
    assert sandbox["enabled"] is True
    filesystem = cast(dict[str, list[str]], sandbox["filesystem"])
    assert str(root) in filesystem["denyWrite"]
    settings = cast(dict[str, object], engine.settings)
    permissions = cast(dict[str, list[str]], settings["permissions"])
    assert "Write" in permissions["deny"]
    assert "NotebookEdit" in permissions["deny"]
    assert f"Edit(/{root}/**)" in permissions["deny"]


def test_read_access_refuses_unconfined_claude_mcp_tools(tmp_path: Path) -> None:
    root = real(tmp_path)
    config = config_from_document(
        {
            "workdir_access": "read",
            "mcp_servers": {"files": {"command": "file-server"}},
        }
    )
    with pytest.raises(EngineRefused, match="cannot confine configured MCP"):
        engine_factory(str(root))(haiku_spec(config), [])


# One case per refusal branch of a read-only Claude line's configuration. Review of
# read-only seats, finding 2.
READ_ACCESS_REFUSALS: Final = [
    pytest.param({"inherit_account_mcp": True}, "cannot confine", id="account-mcp"),
    pytest.param({"tool_policy": "allow_all"}, "allow-all", id="allow-all"),
    pytest.param(
        {"allowed_tools": ["mcp__files__*"]}, "additional MCP", id="allowed-mcp"
    ),
    pytest.param({"project_settings": True}, "project settings", id="project"),
    pytest.param({"plugins": ["~/plugins/one"]}, "plugins", id="plugins"),
    pytest.param(
        {
            "settings": {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [{"type": "command", "command": "touch x"}]}
                    ]
                }
            }
        },
        "command hooks",
        id="hooks",
    ),
    pytest.param(
        {"sandbox": {"enabled": True, "allowUnsandboxedCommands": True}},
        "allowUnsandboxedCommands",
        id="unsandboxed",
    ),
    pytest.param(
        {"sandbox": {"enabled": True, "failIfUnavailable": False}},
        "failIfUnavailable",
        id="fail-open",
    ),
    pytest.param(
        {"sandbox": {"enabled": True, "excludedCommands": ["git"]}},
        "excludedCommands",
        id="excluded",
    ),
    pytest.param(
        {"sandbox": {"enabled": True, "filesystem": {"disabled": True}}},
        "filesystem.disabled",
        id="filesystem-off",
    ),
]


@pytest.mark.parametrize(("document", "reason"), READ_ACCESS_REFUSALS)
def test_read_access_refuses_each_way_out_of_the_claude_sandbox(
    tmp_path: Path, document: dict[str, object], reason: str
) -> None:
    root = real(tmp_path)
    config = config_from_document({"workdir_access": "read", **document})
    with pytest.raises(EngineRefused, match=reason):
        engine_factory(str(root))(haiku_spec(config), [])
    # The same configuration with write access is accepted, so the refusal is the
    # read access's own.
    writer = config_from_document(document)
    assert isinstance(engine_factory(str(root))(haiku_spec(writer), []), ClaudeCodeLine)


@pytest.mark.parametrize(
    "sandbox",
    [
        pytest.param({"allowUnsandboxedCommands": True}, id="unsandboxed"),
        pytest.param({"failIfUnavailable": False}, id="fail-open"),
    ],
)
def test_read_access_refuses_a_widening_project_sandbox(
    tmp_path: Path, sandbox: dict[str, object]
) -> None:
    root = real(tmp_path)
    project = parse_sandbox({"enabled": True, **sandbox}, operator=False)
    # A line layer that sets the secure value does not hide the project's widening.
    line = config_from_document(
        {
            "workdir_access": "read",
            "sandbox": {"allowUnsandboxedCommands": False, "failIfUnavailable": True},
        }
    )
    with pytest.raises(EngineRefused, match=next(iter(sandbox))):
        engine_factory(str(root), project_sandbox=project)(haiku_spec(line), [])


def test_read_access_forces_the_secure_sandbox_keys(tmp_path: Path) -> None:
    root = real(tmp_path)
    # A line with no sandbox of its own, and one that turns the sandbox off, both get
    # the keys set explicitly rather than through the unattended defaults.
    for document in ({}, {"sandbox": {"enabled": False}}):
        config = config_from_document({"workdir_access": "read", **document})
        engine = engine_factory(str(root))(haiku_spec(config), [])
        assert isinstance(engine, ClaudeCodeLine)
        sandbox = cast(dict[str, object], engine.sandbox)
        assert sandbox["enabled"] is True
        assert sandbox["allowUnsandboxedCommands"] is False
        assert sandbox["failIfUnavailable"] is True
        assert blocks_unsandboxed(
            sandbox, "Bash", {"command": "touch x", "dangerouslyDisableSandbox": True}
        )


def test_an_unsandboxed_line_does_not_read_the_git_pointers(tmp_path: Path) -> None:
    root = real(tmp_path)
    _, _, tree = linked_worktree(root)
    (tree / ".git").write_text("broken\n")
    engine = engine_factory(str(root))(haiku_spec(LineConfig(workdir=str(tree))), [])
    assert isinstance(engine, ClaudeCodeLine) and engine.sandbox is None
    sandboxed = engine_factory(
        str(root), project_sandbox=parse_sandbox({"enabled": True}, operator=False)
    )
    with pytest.raises(SandboxError, match="does not name a git directory"):
        sandboxed(haiku_spec(LineConfig(workdir=str(tree))), [])


def test_a_workdir_gone_or_protected_at_build_ends_the_turn(tmp_path: Path) -> None:
    root = real(tmp_path)
    (root / ".dromon").mkdir()
    build = engine_factory(str(root), protected=(root / ".dromon",))
    with pytest.raises(ConfigError, match="not an existing directory"):
        build(haiku_spec(LineConfig(workdir="gone")), [])
    with pytest.raises(ConfigError, match="Dromon protects"):
        build(haiku_spec(LineConfig(workdir=".dromon")), [])


def test_codex_and_grok_lines_run_in_their_workdir(tmp_path: Path) -> None:
    root = real(tmp_path)
    (root / "wt").mkdir()
    socket_path = root / "run" / "lines.sock"
    from dromon.agents import LineTokens

    build = engine_factory(str(root), line_tokens=LineTokens(), line_socket=socket_path)
    config = LineConfig(workdir="wt")
    codex = build(LineSpec("alpha", "gpt-6-luna", Strategy.SUBSCRIPTION, config), [])
    assert isinstance(codex, CodexLine) and codex.cwd == root / "wt"
    grok = build(LineSpec("beta", "grok-4.7", Strategy.SUBSCRIPTION, config), [])
    assert isinstance(grok, GrokLine) and grok.cwd == root / "wt"
    assert list(grok.bridge) == bridge_command(root, socket_path.parent, "beta")


# The scheduler: adopting a session, and starting afresh when the directory moves.


class Recorder:
    """A line engine that records the session it was built with and reports one."""

    def __init__(self, spec: LineSpec) -> None:
        self.spec = spec
        self.session_id = spec.session_id
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def run_turn(self, prompt: str) -> TurnReport:
        del prompt
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.session_id is None:
            self.session_id = "session-new"
        return TurnReport(
            "success", 1, "ok", (), None, None, self.session_id, None, None
        )


class Engines:
    def __init__(self) -> None:
        self.built: list[Recorder] = []
        self.gate: asyncio.Event | None = None

    def build(self, spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> Recorder:
        del tools
        engine = Recorder(spec)
        engine.gate = self.gate
        self.built.append(engine)
        return engine


type Act = Callable[[], Coroutine[Any, Any, None]]


async def settle(scheduler: Scheduler, act: Act) -> None:
    stop = asyncio.Event()
    loop = asyncio.create_task(scheduler.run(stop))
    await asyncio.sleep(0)
    await act()
    await scheduler.idle()
    stop.set()
    await loop


def wake(scheduler: Scheduler, line_id: str) -> Act:
    async def act() -> None:
        scheduler.send(ANA, LineAddress(line_id), "work")

    return act


@pytest.fixture
def world(tmp_path: Path) -> Generator[tuple[EventLog, Scheduler, Engines, Path]]:
    path = tmp_path / "events.sqlite"
    engines = Engines()
    with EventLog(path) as log:
        scheduler = Scheduler(log, engines.build, tick_seconds=0)
        scheduler.ensure_operator("ana")
        yield log, scheduler, engines, path


def test_a_line_created_with_a_session_continues_it(
    world: tuple[EventLog, Scheduler, Engines, Path],
) -> None:
    log, scheduler, engines, path = world
    config = LineConfig(workdir="trees/wt")
    scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION, config, "s-old")
    events = [entry.event for entry in read_entries(path)]
    created = events.index(LineCreated("alpha", HAIKU, "subscription"))
    assert events[created + 1] == LineSession("alpha", "s-old")
    assert events[created + 2] == LineConfigured("alpha", encode_config(config))
    assert not any(isinstance(e, LineSessionCleared) for e in events)
    asyncio.run(settle(scheduler, wake(scheduler, "alpha")))
    assert engines.built[0].spec.session_id == "s-old"
    assert log.state.lines["alpha"].engine_session == "s-old"


def test_a_malformed_session_refuses_the_line_before_it_exists(
    world: tuple[EventLog, Scheduler, Engines, Path],
) -> None:
    from dromon.model import InvalidField

    log, scheduler, _, _ = world
    before = log.last_seq
    with pytest.raises(InvalidField, match="engine_session is empty"):
        scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION, engine_session="")
    with pytest.raises(InvalidField):
        scheduler.create_line(
            "alpha", HAIKU, Strategy.SUBSCRIPTION, engine_session="x" * 100_000
        )
    assert log.last_seq == before and "alpha" not in log.state.lines


def test_moving_a_line_starts_a_new_session_and_other_changes_keep_it(
    world: tuple[EventLog, Scheduler, Engines, Path],
) -> None:
    log, scheduler, engines, path = world
    scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
    asyncio.run(settle(scheduler, wake(scheduler, "alpha")))
    assert log.state.lines["alpha"].engine_session == "session-new"
    scheduler.configure_line("alpha", LineConfig(builtin_tools=("Read",)))
    assert log.state.lines["alpha"].engine_session == "session-new"
    moved = LineConfig(builtin_tools=("Read",), workdir="trees/wt")
    scheduler.configure_line("alpha", moved)
    assert log.state.lines["alpha"].engine_session is None
    scheduler.configure_line("alpha", moved)
    scheduler.configure_line("alpha", LineConfig(builtin_tools=("Read",)))
    cleared = [
        e
        for e in (x.event for x in read_entries(path))
        if isinstance(e, LineSessionCleared)
    ]
    assert len(cleared) == 2
    asyncio.run(settle(scheduler, wake(scheduler, "alpha")))
    assert engines.built[-1].spec.session_id is None


def test_a_move_during_a_turn_does_not_record_that_turns_session(
    world: tuple[EventLog, Scheduler, Engines, Path],
) -> None:
    log, scheduler, engines, _ = world
    engines.gate = asyncio.Event()
    scheduler.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)

    async def act() -> None:
        scheduler.send(ANA, LineAddress("alpha"), "work")
        while not engines.built or not engines.built[0].started.is_set():
            await asyncio.sleep(0)
        scheduler.configure_line("alpha", LineConfig(workdir="trees/wt"))
        assert engines.gate is not None
        engines.gate.set()

    asyncio.run(settle(scheduler, act))
    assert log.state.lines["alpha"].engine_session is None
    engines.gate = None
    asyncio.run(settle(scheduler, wake(scheduler, "alpha")))
    assert engines.built[-1].spec.session_id is None
    assert log.state.lines["alpha"].engine_session == "session-new"


# The project file.


def test_the_project_file_gives_a_workdir_and_a_session() -> None:
    config = parse(
        "version = 1\n[profiles.p]\nmodel = 'claude-haiku-5-5'\nworkdir = 'trees'\n"
        "[lines.a]\nprofile = 'p'\nsession = 's-1'\n"
        "[lines.b]\nprofile = 'p'\nworkdir = '/abs/wt'\n"
    )
    assert config.lines["a"].config.workdir == "trees"
    assert config.lines["a"].session == "s-1"
    assert config.lines["b"].config.workdir == "/abs/wt"
    assert config.lines["b"].session is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (
            "[profiles.p]\nmodel = 'claude-haiku-5-5'\nsession = 's'\n",
            "profiles.p.session: is not a known key",
        ),
        ("[lines.a]\nmodel = 'claude-haiku-5-5'\nsession = 3\n", "must be text"),
        ("[lines.a]\nmodel = 'claude-haiku-5-5'\nsession = ''\n", "is empty"),
        ("[lines.a]\nmodel = 'claude-haiku-5-5'\nworkdir = ''\n", "workdir"),
    ],
)
def test_a_bad_workdir_or_session_in_the_file_is_refused(
    text: str, message: str
) -> None:
    with pytest.raises(ConfigFileError, match=message):
        parse("version = 1\n" + text)


def test_the_daemon_refuses_a_file_whose_workdir_is_not_allowed(
    tmp_path: Path, short_runtime: Path
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir()
    head = "version = 1\n[lines.a]\nmodel = 'claude-haiku-5-5'\n"
    with pytest.raises(ConfigError, match="not an existing directory"):
        check_sandboxes(parse(head + "workdir = 'gone'\n"), paths)
    with pytest.raises(ConfigError, match="Dromon protects"):
        check_sandboxes(parse(head + "workdir = '.dromon'\n"), paths)
    _, _, tree = linked_worktree(tmp_path)
    (tree / ".git").write_text("broken\n")
    check_sandboxes(parse(head + f"workdir = '{tree}'\n"), paths)
    with pytest.raises(SandboxError, match="does not name a git directory"):
        check_sandboxes(
            parse(
                "version = 1\n[sandbox]\nenabled = true\n"
                f"[lines.a]\nmodel = 'claude-haiku-5-5'\nworkdir = '{tree}'\n"
            ),
            paths,
        )


# The daemon's operations.


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


class Client:
    def __init__(self, daemon: Daemon) -> None:
        self.daemon = daemon

    def ask(self, op: str, args: dict[str, object]) -> Response:
        line = {"id": "r", "op": op, "v": 1, "args": args}
        return self.daemon.respond((json.dumps(line) + "\n").encode())

    def ok(self, op: str, args: dict[str, object]) -> dict[str, object]:
        response = self.ask(op, args)
        assert response.ok, response.error_message
        assert response.result is not None
        return response.result

    def refused(self, op: str, args: dict[str, object]) -> Response:
        before = self.daemon.log.last_seq
        response = self.ask(op, args)
        assert not response.ok
        assert self.daemon.log.last_seq == before
        return response

    def workdir_entry(self, line_id: str) -> dict[str, object]:
        result = self.ok("settings", {"line_id": line_id})
        entries = cast(list[dict[str, object]], result["line"])
        return next(e for e in entries if e["key"] == "workdir")


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    (found.root / "wt").mkdir()
    return found


@contextmanager
def attached(paths: ProjectPaths) -> Generator[Client]:
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        daemon = Daemon(
            paths,
            log,
            "2026-10-25T00:00:00Z",
            scheduler,
            project=ProjectConfig(),
            command_line=Overrides(),
            project_file_present=False,
        )
        yield Client(daemon)


def create_args(**extra: object) -> dict[str, object]:
    return {"line_id": "alpha", "model": HAIKU, "strategy": "subscription", **extra}


def test_create_line_adopts_a_session_and_takes_a_workdir(
    paths: ProjectPaths,
) -> None:
    with attached(paths) as client:
        client.ok(
            "create_line",
            create_args(engine_session="s-1", config={"workdir": "wt"}),
        )
        line = client.daemon.log.state.lines["alpha"]
        assert line.engine_session == "s-1"
        assert decode_config(cast(str, line.config)).workdir == "wt"
        entry = client.workdir_entry("alpha")
        assert (entry["kind"], entry["value"], entry["editable"]) == (
            "info",
            "wt",
            False,
        )
        client.ok("create_line", {**create_args(), "line_id": "beta"})
        assert client.workdir_entry("beta")["value"] == "the project root"


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        ({"engine_session": ""}, ErrorCode.REJECTED),
        ({"engine_session": 7}, ErrorCode.BAD_REQUEST),
        ({"config": {"workdir": "gone"}}, ErrorCode.REJECTED),
        ({"config": {"workdir": ".dromon"}}, ErrorCode.REJECTED),
        ({"session": "s-1"}, ErrorCode.BAD_REQUEST),
    ],
)
def test_create_line_refuses_a_bad_session_or_workdir(
    paths: ProjectPaths, extra: dict[str, object], code: ErrorCode
) -> None:
    with attached(paths) as client:
        response = client.refused("create_line", create_args(**extra))
        assert response.error_code is code
        assert "alpha" not in client.daemon.log.state.lines


def test_configure_line_moves_a_line_and_refuses_a_bad_workdir(
    paths: ProjectPaths,
) -> None:
    with attached(paths) as client:
        client.ok("create_line", create_args(engine_session="s-1"))
        for workdir in ("gone", ".dromon", str(paths.runtime_dir)):
            response = client.refused(
                "configure_line", {"line_id": "alpha", "config": {"workdir": workdir}}
            )
            assert response.error_code is ErrorCode.REJECTED
        assert client.daemon.log.state.lines["alpha"].engine_session == "s-1"
        client.ok("configure_line", {"line_id": "alpha", "config": {"workdir": "wt"}})
        assert client.daemon.log.state.lines["alpha"].engine_session is None


def test_naming_the_project_root_explicitly_starts_a_new_session(
    paths: ProjectPaths,
) -> None:
    """The comparison is of the stored text, as LINE_CONFIG.md documents, so a first
    explicit workdir naming the directory the line already ran in still moves it."""
    with attached(paths) as client:
        client.ok("create_line", create_args(engine_session="s-1"))
        root = str(paths.root)
        client.ok("configure_line", {"line_id": "alpha", "config": {"workdir": root}})
        assert client.daemon.log.state.lines["alpha"].engine_session is None
        client.daemon.log.append(LineSession("alpha", "s-2"))
        client.ok("configure_line", {"line_id": "alpha", "config": {"workdir": root}})
        assert client.daemon.log.state.lines["alpha"].engine_session == "s-2"
        client.ok("configure_line", {"line_id": "alpha", "config": {"workdir": "."}})
        assert client.daemon.log.state.lines["alpha"].engine_session is None


def test_a_client_cannot_set_the_workdir(paths: ProjectPaths) -> None:
    with attached(paths) as client:
        client.ok("create_line", create_args())
        response = client.refused(
            "set_setting",
            {"scope": "line", "line_id": "alpha", "key": "workdir", "value": "wt"},
        )
        assert response.error_code is ErrorCode.REJECTED


def test_a_reload_moves_a_line_and_adopts_sessions_only_for_new_lines(
    paths: ProjectPaths,
) -> None:
    file = config_path(paths.state_dir)
    with attached(paths) as client:
        client.ok("create_line", create_args(engine_session="s-1"))
        file.write_text(
            "version = 1\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\nsession = 's-9'\n"
            "[lines.beta]\nmodel = 'claude-haiku-5-5'\nsession = 's-2'\n"
        )
        client.ok("reload_project", {})

        def lines() -> dict[str, Any]:
            return dict(client.daemon.log.state.lines)

        assert lines()["alpha"].engine_session == "s-1"
        assert lines()["beta"].engine_session == "s-2"
        file.write_text(
            "version = 1\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\nworkdir = 'wt'\n"
        )
        client.ok("reload_project", {})
        assert lines()["alpha"].engine_session is None
        file.write_text(
            "version = 1\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\nworkdir = 'gone'\n"
        )
        response = client.refused("reload_project", {})
        assert response.error_code is ErrorCode.REJECTED
        assert decode_config(cast(str, lines()["alpha"].config)).workdir == "wt"
        assert protected_dirs(paths)[0] == paths.state_dir.resolve()


# The client.


def test_the_client_sends_a_workdir_and_a_session(
    tmp_path: Path, short_runtime: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, dict[str, object]]] = []

    def capture(
        paths: ProjectPaths, command: str, operation: object, args: dict[str, object]
    ) -> int:
        del paths, operation
        sent.append((command, args))
        return 0

    monkeypatch.setattr(cli, "_request", capture)
    project_root = str(tmp_path)
    runtime = ["--runtime-dir", str(short_runtime)]
    assert (
        cli.main(
            [
                "--project",
                project_root,
                *runtime,
                "create",
                "alpha",
                "--workdir",
                "../trees/wt",
                "--session",
                "s-1",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "--project",
                project_root,
                *runtime,
                "configure",
                "alpha",
                "--workdir",
                "wt",
            ]
        )
        == 0
    )
    assert sent == [
        (
            "create",
            {
                "line_id": "alpha",
                "model": HAIKU,
                "strategy": "subscription",
                "config": {
                    "builtin_tools": [],
                    "allowed_tools": [],
                    "tool_policy": "allowlist",
                    "inherit_account_mcp": False,
                    "workdir": "../trees/wt",
                },
                "engine_session": "s-1",
            },
        ),
        (
            "configure",
            {
                "line_id": "alpha",
                "config": {
                    "builtin_tools": [],
                    "allowed_tools": [],
                    "tool_policy": "allowlist",
                    "inherit_account_mcp": False,
                    "workdir": "wt",
                },
            },
        ),
    ]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["configure", "alpha", "--session", "s-1"])


def test_dromon_config_names_a_lines_workdir(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / ".dromon"
    state.mkdir()
    (state / "dromon.toml").write_text(
        "version = 1\n[lines.a]\nmodel = 'claude-haiku-5-5'\nworkdir = 'wt'\n"
    )
    runtime = ["--runtime-dir", str(short_runtime)]
    assert cli.main(["--project", str(tmp_path), *runtime, "config"]) == 0
    assert ", in wt" in capsys.readouterr().out


def test_dromon_config_warns_when_writing_lines_share_workdir(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / ".dromon"
    state.mkdir()
    (state / "dromon.toml").write_text(
        "version = 1\n"
        "[lines.a]\nmodel = 'claude-haiku-5-5'\nbuiltin_tools = ['Read', 'Write']\n"
        "[lines.b]\nmodel = 'claude-opus-5-5'\nbuiltin_tools = ['Bash']\n"
        "[lines.reader]\nmodel = 'claude-haiku-5-5'\nbuiltin_tools = ['Read']\n"
    )
    runtime = ["--runtime-dir", str(short_runtime)]
    assert cli.main(["--project", str(tmp_path), *runtime, "config"]) == 0
    output = capsys.readouterr().out
    assert "warning: writing lines a, b share workdir" in output
    assert "reader" not in output.split("warning:", maxsplit=1)[1]


def test_dromon_config_does_not_warn_about_seats_of_one_role(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Seats of one role share its worktree by design, and only the owner writes there.
    # A writing line outside the role in the same worktree is still a second writer.
    state = tmp_path / ".dromon"
    state.mkdir()
    (tmp_path / "wt").mkdir()
    seats = (
        "version = 1\n"
        "[lines.core]\nmodel = 'claude-opus-5-5'\nbuiltin_tools = ['Bash']\n"
        "[lines.core-luna]\nmodel = 'claude-haiku-5-5'\nbuiltin_tools = ['Write']\n"
        "[roles.core]\nworkdir = 'wt'\nseats = ['core', 'core-luna']\n"
    )
    (state / "dromon.toml").write_text(seats)
    runtime = ["--runtime-dir", str(short_runtime)]
    assert cli.main(["--project", str(tmp_path), *runtime, "config"]) == 0
    assert "warning:" not in capsys.readouterr().out

    (state / "dromon.toml").write_text(
        seats + "[lines.other]\nmodel = 'claude-haiku-5-5'\n"
        "builtin_tools = ['Edit']\nworkdir = 'wt'\n"
    )
    assert cli.main(["--project", str(tmp_path), *runtime, "config"]) == 0
    output = capsys.readouterr().out
    assert "warning: writing lines core, core-luna, other share workdir" in output


def test_dromon_config_resolves_workdir_symlinks_for_shared_writer_warning(
    tmp_path: Path, short_runtime: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / ".dromon"
    state.mkdir()
    actual = tmp_path / "worktree"
    actual.mkdir()
    (tmp_path / "alias").symlink_to(actual, target_is_directory=True)
    (state / "dromon.toml").write_text(
        "version = 1\n"
        "[lines.alpha]\nmodel = 'claude-haiku-5-5'\n"
        "builtin_tools = ['Read', 'Write']\nworkdir = 'worktree'\n"
        "[lines.beta]\nmodel = 'claude-opus-5-5'\n"
        "builtin_tools = ['Bash']\nworkdir = 'alias'\n"
    )
    runtime = ["--runtime-dir", str(short_runtime)]
    assert cli.main(["--project", str(tmp_path), *runtime, "config"]) == 0
    output = capsys.readouterr().out
    assert "warning: writing lines alpha, beta share workdir" in output
    assert f"share workdir {actual}" in output


def test_a_claude_line_reads_instructions_from_its_workdir(tmp_path: Path) -> None:
    (tmp_path / "wt").mkdir()
    (tmp_path / "wt" / "CLAUDE.md").write_text("Here.\n")
    driver = Driver.for_model(HAIKU, Strategy.SUBSCRIPTION)
    line = ClaudeCodeLine(
        driver, tmp_path / "wt", config=LineConfig(instructions=("CLAUDE.md",))
    )
    assert line.options().system_prompt == "Contents of CLAUDE.md:\n\nHere."
