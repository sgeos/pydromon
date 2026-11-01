"""Round 14, item 6. A sandboxed line has a temporary directory and a package cache
that it may write, outside its working directory, and its shell is pointed at them."""

import os
import shutil
import stat
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import launcher, linetemp
from dromon.driver import Strategy
from dromon.engine import ClaudeCodeLine, launch_environment
from dromon.lineconfig import LineConfig, WorkdirAccess
from dromon.linetemp import LineTemp, LineTempRefused, prepare
from dromon.sandbox import parse_sandbox
from dromon.scheduler import LineEngine, LineSpec, engine_factory

HAIKU = "claude-haiku-5-5"


@pytest.fixture(autouse=True)
def _shell_is_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A supervised run starts without SHELL, which a Bash line needs to build its
    options. The tests name one, so they do not depend on the caller's environment."""
    monkeypatch.setenv("SHELL", "/bin/bash")


def mode_of(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_the_directories_are_made_private_and_found_again(tmp_path: Path) -> None:
    found = prepare("alpha", tmp_path)
    user = os.getuid()
    assert found.root == tmp_path / f"dromon-{user}" / "alpha"
    assert found.temp == found.root / "tmp" and found.cache == found.root / "uv-cache"
    for path in (found.root.parent, found.root, found.temp, found.cache):
        assert path.is_dir() and mode_of(path) == 0o700
    (found.temp / "kept").write_text("x")
    assert prepare("alpha", tmp_path) == found
    assert (found.temp / "kept").read_text() == "x"
    other = prepare("beta", tmp_path)
    assert other.root != found.root and other.root.parent == found.root.parent


def test_the_variables_and_the_sandbox_grant_name_the_same_directories(
    tmp_path: Path,
) -> None:
    found = prepare("alpha", tmp_path)
    assert found.environment() == {
        "TMPDIR": str(found.temp),
        "UV_CACHE_DIR": str(found.cache),
    }
    assert found.writable() == [str(found.temp), str(found.cache)]


@pytest.mark.parametrize("name", ["", ".", "..", "a/b", "a b", "-x", "é", "a" * 65])
def test_a_line_identifier_that_cannot_name_a_directory_is_refused(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(LineTempRefused, match="cannot name a directory"):
        prepare(name, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_a_symbolic_link_in_the_way_is_refused_and_not_followed(
    tmp_path: Path,
) -> None:
    user = os.getuid()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    base = tmp_path / "base"
    base.mkdir()
    (base / f"dromon-{user}").symlink_to(elsewhere)
    with pytest.raises(LineTempRefused, match="is a symbolic link"):
        prepare("alpha", base)
    assert list(elsewhere.iterdir()) == []
    (base / f"dromon-{user}").unlink()
    first = prepare("alpha", base)
    first.temp.rmdir()
    first.temp.symlink_to(elsewhere)
    with pytest.raises(LineTempRefused, match="is a symbolic link"):
        prepare("alpha", base)


def test_a_file_or_a_reachable_directory_in_the_way_is_refused(
    tmp_path: Path,
) -> None:
    user = os.getuid()
    (tmp_path / f"dromon-{user}").write_text("x")
    with pytest.raises(LineTempRefused, match="is not a directory"):
        prepare("alpha", tmp_path)
    (tmp_path / f"dromon-{user}").unlink()
    found = prepare("alpha", tmp_path)
    os.chmod(found.cache, 0o750)  # noqa: S103
    with pytest.raises(LineTempRefused, match="can be reached by another user"):
        prepare("alpha", tmp_path)


def test_a_directory_of_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepare("alpha", tmp_path)
    other = os.getuid() + 1
    monkeypatch.setattr(linetemp.os, "getuid", lambda: other)
    with pytest.raises(LineTempRefused, match="belongs to another user"):
        prepare("alpha", tmp_path)


def test_a_base_that_cannot_be_written_is_refused_with_the_reason(
    tmp_path: Path,
) -> None:
    with pytest.raises(LineTempRefused, match="cannot make"):
        prepare("alpha", tmp_path / "missing" / "deeper")


def test_the_status_note_says_whether_the_directories_exist() -> None:
    assert "the line's own directories" in linetemp.note_of(None)
    failure = LineTempRefused("x")
    assert "not made, so the shell keeps its own temporary directory" in (
        linetemp.note_of(failure)
    )


def test_the_launch_environment_points_the_shell_at_the_directories(
    tmp_path: Path,
) -> None:
    found = prepare("alpha", tmp_path)
    plain = launch_environment(None, launcher.StopPolicy.KILL_ALL, None)
    assert "TMPDIR" not in plain
    assert launcher.relayed("UV_CACHE_DIR") in plain
    assert plain[launcher.relayed("UV_CACHE_DIR")] == ""
    given = launch_environment(None, launcher.StopPolicy.KILL_ALL, None, None, found)
    assert given["TMPDIR"] == str(found.temp)
    assert given[launcher.relayed("UV_CACHE_DIR")] == str(found.cache)
    assert "UV_CACHE_DIR" not in given


def test_the_launcher_filter_keeps_only_the_adapters_cache_directory() -> None:
    """A daemon's own UV_CACHE_DIR never reaches the engine, and the adapter's does."""
    environment = {
        "UV_CACHE_DIR": "/daemon/cache",
        launcher.relayed("UV_CACHE_DIR"): "/line/cache",
    }
    launcher.unrelayed(environment)
    assert environment == {"UV_CACHE_DIR": "/line/cache"}
    environment = {"UV_CACHE_DIR": "/daemon/cache"}
    launcher.unrelayed(environment)
    assert environment == {}
    environment = {
        "UV_CACHE_DIR": "/daemon/cache",
        launcher.relayed("UV_CACHE_DIR"): "",
    }
    launcher.unrelayed(environment)
    assert environment == {}


def build(config: LineConfig) -> ClaudeCodeLine:
    project = parse_sandbox({"enabled": True}, operator=True)
    factory = engine_factory("/work/project", project_sandbox=project)
    engine = factory(LineSpec("alpha", HAIKU, Strategy.SUBSCRIPTION, config), [])
    assert isinstance(engine, ClaudeCodeLine)
    return engine


def test_a_sandboxed_line_is_granted_and_pointed_at_its_directories() -> None:
    engine = build(LineConfig(builtin_tools=("Bash",)))
    assert engine.temp is not None
    filesystem = cast(dict[str, list[str]], (engine.sandbox or {})["filesystem"])
    resolved = [os.path.realpath(path) for path in engine.temp.writable()]
    assert set(resolved) <= set(filesystem["allowWrite"])
    environment = engine.options().env
    assert environment["TMPDIR"] == str(engine.temp.temp)
    assert environment[launcher.relayed("UV_CACHE_DIR")] == str(engine.temp.cache)
    assert str(engine.temp.root).startswith(str(linetemp.base_directory()))


def test_a_line_that_may_only_read_gets_directories_outside_its_worktree() -> None:
    engine = build(
        LineConfig(builtin_tools=("Bash",), workdir_access=WorkdirAccess.READ)
    )
    assert engine.temp is not None
    filesystem = cast(dict[str, list[str]], (engine.sandbox or {})["filesystem"])
    assert "/work/project" in filesystem["denyWrite"]
    assert not Path(str(engine.temp.root)).is_relative_to("/work/project")
    assert (
        str(engine.temp.temp) in filesystem["allowWrite"]
        or os.path.realpath(engine.temp.temp) in filesystem["allowWrite"]
    )


def test_a_line_without_a_sandbox_is_given_no_directories() -> None:
    factory = engine_factory("/work/project")
    engine = factory(LineSpec("alpha", HAIKU, Strategy.SUBSCRIPTION, LineConfig()), [])
    assert isinstance(engine, ClaudeCodeLine)
    assert engine.temp is None
    assert "TMPDIR" not in engine.options().env


def test_a_line_still_starts_when_its_directories_cannot_be_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(linetemp, "base_directory", lambda: tmp_path / "missing" / "x")
    engine = build(LineConfig(builtin_tools=("Bash",)))
    assert engine.temp is None
    filesystem = cast(
        dict[str, list[str]], (engine.sandbox or {}).get("filesystem", {})
    )
    assert "allowWrite" not in filesystem
    assert "TMPDIR" not in engine.options().env


def test_a_line_temp_is_plain_data() -> None:
    found = LineTemp(Path("/a"), Path("/a/tmp"), Path("/a/uv-cache"))
    assert found.writable() == ["/a/tmp", "/a/uv-cache"]


# Review of 2026-11-01, finding 2. Removal and the age rule.

WEEK = linetemp.MAX_TEMP_AGE_SECONDS


def fill(root: Path, name: str) -> tuple[Path, Path]:
    found = prepare(name, root)
    return found.temp, found.cache


def test_the_sweep_removes_stale_entries_of_a_known_line_and_keeps_fresh_ones(
    tmp_path: Path,
) -> None:
    temp, cache = fill(tmp_path, "alpha")
    (temp / "old").write_text("x")
    (temp / "fresh").write_text("y")
    (cache / "old-cache").write_text("z")
    nested = temp / "tree"
    nested.mkdir()
    (nested / "inner").write_text("w")
    now = 2_000_000_000.0
    for path in (temp / "old", cache / "old-cache", nested):
        os.utime(path, (now - WEEK - 10, now - WEEK - 10))
    os.utime(temp / "fresh", (now - 5, now - 5))
    swept = linetemp.sweep({"alpha"}, tmp_path, now=now)
    assert sorted(Path(p).name for p in swept.removed) == ["old", "tree"]
    assert swept.failed == ()
    assert not (temp / "old").exists() and not nested.exists()
    assert (temp / "fresh").exists()
    assert (cache / "old-cache").exists(), "a known line's cache is left"


def test_the_sweep_removes_an_unknown_lines_directory_only_when_it_is_stale(
    tmp_path: Path,
) -> None:
    """The base is shared by every daemon of the user, so a line this daemon does not
    know may be another project's, and its fresh files stay."""
    gone_temp, gone_cache = fill(tmp_path, "gone")
    other_temp, other_cache = fill(tmp_path, "other")
    (gone_temp / "a").write_text("x")
    (gone_cache / "b").write_text("y")
    (other_temp / "live").write_text("z")
    now = 2_000_000_000.0
    for path in (gone_temp / "a", gone_cache / "b"):
        os.utime(path, (now - WEEK - 10, now - WEEK - 10))
    os.utime(other_temp / "live", (now - 5, now - 5))
    swept = linetemp.sweep({"alpha"}, tmp_path, now=now)
    assert not gone_temp.parent.exists()
    assert str(gone_temp.parent) in swept.removed
    assert (other_temp / "live").exists() and other_cache.exists()


def test_the_sweep_does_not_follow_a_link(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("keep")
    temp, _ = fill(tmp_path, "alpha")
    link = temp / "link"
    link.symlink_to(outside)
    now = 2_000_000_000.0
    os.utime(link, (now - WEEK - 10, now - WEEK - 10), follow_symlinks=False)
    swept = linetemp.sweep({"alpha"}, tmp_path, now=now)
    assert str(link) in swept.removed
    assert not link.is_symlink() and not link.exists()
    assert (outside / "precious").read_text() == "keep"


def test_the_sweep_leaves_a_parent_that_is_not_the_users_private_directory(
    tmp_path: Path,
) -> None:
    parent = tmp_path / f"dromon-{os.getuid()}"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    (parent / "gone").mkdir()
    swept = linetemp.sweep(set(), tmp_path, now=2_000_000_000.0)
    assert swept.removed == () and len(swept.failed) == 1
    assert (parent / "gone").exists()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    parent.rmdir() if not any(parent.iterdir()) else shutil.rmtree(parent)
    parent.symlink_to(outside)
    swept = linetemp.sweep(set(), tmp_path, now=2_000_000_000.0)
    assert swept.removed == () and len(swept.failed) == 1


def test_the_sweep_has_nothing_to_do_without_a_directory(tmp_path: Path) -> None:
    assert linetemp.sweep({"alpha"}, tmp_path) == linetemp.Swept((), ())


def test_one_failure_does_not_stop_the_other_removals(tmp_path: Path) -> None:
    temp, _ = fill(tmp_path, "alpha")
    locked = temp / "locked"
    locked.mkdir()
    (locked / "inner").write_text("x")
    (temp / "plain").write_text("y")
    now = 2_000_000_000.0
    os.utime(temp / "plain", (now - WEEK - 10, now - WEEK - 10))
    os.utime(locked, (now - WEEK - 10, now - WEEK - 10))
    locked.chmod(0o500)
    try:
        swept = linetemp.sweep({"alpha"}, tmp_path, now=now)
    finally:
        locked.chmod(0o700)
    assert str(temp / "plain") in swept.removed
    assert not (temp / "plain").exists()
    assert any("locked" in note for note in swept.failed)


def test_a_line_that_may_not_read_others_is_denied_the_directory_that_holds_them(
    tmp_path: Path,
) -> None:
    found = prepare("alpha", tmp_path)
    assert found.others_unreadable() == [str(found.root.parent)]
    assert found.readable() == [str(found.root)]
    engine = build(LineConfig(builtin_tools=("Bash",)))
    assert engine.temp is not None
    filesystem = cast(dict[str, list[str]], (engine.sandbox or {})["filesystem"])
    parent = os.path.realpath(engine.temp.root.parent)
    root = os.path.realpath(engine.temp.root)
    assert parent in filesystem["denyRead"]
    assert root in filesystem["allowRead"]
    assert root.startswith(parent + os.sep)


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start, but {spec.line_id} did")


def test_the_daemon_sweeps_at_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dromon.eventlog import EventLog
    from dromon.scheduler import Scheduler

    asked: list[set[str]] = []

    def record(known: set[str], *_: object, **__: object) -> linetemp.Swept:
        asked.append(set(known))
        return linetemp.Swept(("x",), ("y: nope",))

    monkeypatch.setattr(linetemp, "sweep", record)
    with EventLog(tmp_path / "events.sqlite") as log:
        found = Scheduler(log, no_engine)
        found.create_line("alpha", HAIKU, Strategy.SUBSCRIPTION)
        found.recover()
        found.stop_workers()
    assert asked == [{"alpha"}]


def test_a_failed_sweep_does_not_stop_the_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dromon.eventlog import EventLog
    from dromon.scheduler import Scheduler

    def broken(*_: object, **__: object) -> linetemp.Swept:
        raise OSError("disk gone")

    monkeypatch.setattr(linetemp, "sweep", broken)
    with EventLog(tmp_path / "events.sqlite") as log:
        found = Scheduler(log, no_engine)
        found.recover()
        found.stop_workers()
    assert "line temporary sweep failed: disk gone" in capsys.readouterr().err
