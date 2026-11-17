"""Round 17, review of 0db077c, finding 1, decided by the operator. The project's
``secret`` directories, at the project root and in the line's working directory, are
denied to Claude Code lines for reading and writing, by real path, whatever the line's
own documents say, as they have been to Codex and Grok lines since round 9.

The shell is held by the sandbox document's ``denyRead`` and ``denyWrite``, which
apply only when the sandbox is enabled, and the built-in file tools by the settings'
``deny`` rules, which apply either way. A layer that grants a secret directory back
through ``allowRead`` or ``allowWrite`` is refused, at the line's build and, for the
project file, when ``dromond`` starts. A line's own ``allow`` rule in its settings
cannot reopen it, since Claude Code applies a ``deny`` rule first."""

import os
from pathlib import Path
from typing import cast

import pytest

from dromon.daemon import check_sandboxes
from dromon.driver import Strategy
from dromon.engine import ClaudeCodeLine
from dromon.lineconfig import LineConfig
from dromon.paths import project_paths
from dromon.projectconfig import parse
from dromon.sandbox import SandboxError, parse_sandbox
from dromon.scheduler import LineSpec, engine_factory
from dromon.settings import parse_settings

HAIKU = "claude-haiku-5-5"


def layout(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """A project root and a worktree outside it, each with its own ``secret``."""
    root = tmp_path / "project"
    worktree = tmp_path / "worktrees" / "line"
    (root / "secret").mkdir(parents=True)
    (worktree / "secret").mkdir(parents=True)
    return root, worktree, root / "secret", worktree / "secret"


def built(
    root: Path,
    config: LineConfig,
    project: dict[str, object] | None = None,
) -> ClaudeCodeLine:
    factory = engine_factory(
        str(root),
        project_sandbox=None
        if project is None
        else parse_sandbox(project, operator=True),
    )
    engine = factory(LineSpec("alpha", HAIKU, Strategy.SUBSCRIPTION, config), [])
    assert isinstance(engine, ClaudeCodeLine)
    return engine


def denied(engine: ClaudeCodeLine) -> tuple[set[str], set[str], set[str]]:
    """The sandbox's read and write denials and the settings' deny rules."""
    assert engine.sandbox is not None and engine.settings is not None
    filesystem = cast(dict[str, list[str]], engine.sandbox["filesystem"])
    permissions = cast(dict[str, list[str]], engine.settings["permissions"])
    return (
        set(filesystem["denyRead"]),
        set(filesystem["denyWrite"]),
        set(permissions["deny"]),
    )


def test_both_secret_directories_are_denied_to_a_sandboxed_claude_line(
    tmp_path: Path,
) -> None:
    root, worktree, root_secret, own_secret = layout(tmp_path)
    engine = built(root, LineConfig(workdir=str(worktree)), {"enabled": True})
    read, write, rules = denied(engine)
    for secret in (root_secret, own_secret):
        real = os.path.realpath(secret)
        assert real in read and real in write
        assert f"Read(/{real}/**)" in rules and f"Edit(/{real}/**)" in rules


def test_the_file_tools_are_denied_them_without_a_sandbox(tmp_path: Path) -> None:
    """A line with no enabled sandbox has no shell denial at all, as for every
    protected directory, and its file tools are still kept out."""
    root, worktree, root_secret, own_secret = layout(tmp_path)
    engine = built(root, LineConfig(workdir=str(worktree)))
    assert engine.sandbox is None
    assert engine.settings is not None
    rules = cast(dict[str, list[str]], engine.settings["permissions"])["deny"]
    for secret in (root_secret, own_secret):
        assert f"Read(/{os.path.realpath(secret)}/**)" in rules


def test_a_link_to_a_secret_directory_is_denied_by_its_real_path(
    tmp_path: Path,
) -> None:
    root, _, root_secret, _ = layout(tmp_path)
    linked = tmp_path / "linked"
    linked.symlink_to(root)
    engine = built(linked, LineConfig(), {"enabled": True})
    read, write, _ = denied(engine)
    real = os.path.realpath(root_secret)
    assert real in read and real in write


def test_an_absent_secret_directory_adds_nothing(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    engine = built(root, LineConfig(), {"enabled": True})
    assert engine.sandbox is not None
    filesystem = cast(dict[str, list[str]], engine.sandbox["filesystem"])
    assert not any(path.endswith("/secret") for path in filesystem["denyRead"])
    assert engine.settings is None


@pytest.mark.parametrize("key", ["allowRead", "allowWrite"])
@pytest.mark.parametrize("which", ["root", "own"])
def test_a_line_s_sandbox_that_grants_a_secret_directory_is_refused(
    tmp_path: Path, key: str, which: str
) -> None:
    root, worktree, root_secret, own_secret = layout(tmp_path)
    secret = root_secret if which == "root" else own_secret
    own = parse_sandbox(
        {"filesystem": {key: [str(secret / "terms.txt")]}}, operator=False
    )
    with pytest.raises(SandboxError, match="directory Dromon protects"):
        built(root, LineConfig(workdir=str(worktree), sandbox=own), {"enabled": True})


def test_a_line_s_settings_cannot_allow_what_is_denied(tmp_path: Path) -> None:
    """Claude Code applies ``deny`` before ``allow``, so the rule stays effective."""
    root, worktree, root_secret, _ = layout(tmp_path)
    real = os.path.realpath(root_secret)
    settings = parse_settings({"permissions": {"allow": [f"Read(/{real}/**)"]}})
    engine = built(
        root, LineConfig(workdir=str(worktree), settings=settings), {"enabled": True}
    )
    assert engine.settings is not None
    permissions = cast(dict[str, list[str]], engine.settings["permissions"])
    assert f"Read(/{real}/**)" in permissions["deny"]


def test_the_daemon_refuses_a_project_file_that_grants_a_secret_directory(
    tmp_path: Path, short_runtime: Path
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir()
    (tmp_path / "secret").mkdir()
    line = "[lines.a]\nmodel = 'claude-haiku-5-5'\n"
    check_sandboxes(parse("version = 1\n[sandbox]\nenabled = true\n" + line), paths)
    for granted in (
        "[sandbox]\nenabled = true\n[sandbox.filesystem]\nallowRead = ['secret']\n",
        f"{line}[lines.a.sandbox]\nenabled = true\n"
        "[lines.a.sandbox.filesystem]\nallowWrite = ['secret/x']\n",
    ):
        text = "version = 1\n" + granted + ("" if "lines.a" in granted else line)
        with pytest.raises(SandboxError, match="directory Dromon protects"):
            check_sandboxes(parse(text), paths)
