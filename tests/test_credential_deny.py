"""Round 18, the operator's security fix. Every Claude Code line Dromon builds is
denied the known credential paths under the daemon's real home, for reading and
writing, by real path, in its sandbox for the shell and as ``Read`` and ``Edit`` deny
rules for its file tools, whatever its own documents say, as Codex and Grok lines have
been since round 9. Claude Code keeps none of them, as
``security_paths.CLAUDE_CODE_EXEMPT`` explains.

**Confirmed before the change.** Lines the client created, which take no profile of
the project file, reported the home ssh directory listable and Codex's sign-in file
readable, and the factory's Claude Code branch passed only the protected directories
and the project's secret directories, ``engine_protected``, to the sandbox and the
settings. Each test here builds through ``scheduler.engine_factory``, the one place a
``ClaudeCodeLine`` is made, and the conftest gives every test an empty daemon home of
its own, so what the machine running the tests holds never decides a result."""

import json
import os
from pathlib import Path
from typing import cast

import pytest

from dromon.daemon import check_sandboxes
from dromon.driver import Strategy
from dromon.engine import ClaudeCodeLine
from dromon.gitssh import GitSshAccess
from dromon.lineconfig import LineConfig, WorkdirAccess
from dromon.paths import project_paths
from dromon.projectconfig import parse
from dromon.sandbox import SandboxError, parse_sandbox
from dromon.scheduler import LineSpec, engine_factory
from dromon.security_paths import (
    CLAUDE_CODE_EXEMPT,
    CREDENTIAL_PATHS,
    credential_paths,
)
from dromon.security_paths import daemon_home as real_daemon_home
from dromon.settings import credential_rules, parse_settings

HAIKU = "claude-haiku-5-5"


@pytest.fixture
def home(_empty_daemon_home: Path) -> Path:
    """The daemon home the conftest gives this test, holding a directory, a file and
    a file in a directory from the list."""
    (_empty_daemon_home / ".ssh").mkdir()
    (_empty_daemon_home / ".ssh" / "id_test").write_text("not a key\n")
    (_empty_daemon_home / ".claude.json").write_text("{}\n")
    (_empty_daemon_home / ".codex").mkdir()
    (_empty_daemon_home / ".codex" / "auth.json").write_text("{}\n")
    return _empty_daemon_home


def listed(home: Path) -> tuple[Path, ...]:
    return (home / ".ssh", home / ".claude.json", home / ".codex" / "auth.json")


def built(
    root: Path,
    config: LineConfig | None = None,
    project: dict[str, object] | None = None,
    read_only_paths: tuple[str, ...] = (),
    git_ssh: GitSshAccess | None = None,
) -> ClaudeCodeLine:
    factory = engine_factory(
        str(root),
        project_sandbox=None
        if project is None
        else parse_sandbox(project, operator=True),
        read_only_paths=read_only_paths,
        line_git_ssh=None if git_ssh is None else {"alpha": git_ssh},
    )
    spec = LineSpec("alpha", HAIKU, Strategy.SUBSCRIPTION, config or LineConfig())
    engine = factory(spec, [])
    assert isinstance(engine, ClaudeCodeLine)
    return engine


def rules_of(engine: ClaudeCodeLine) -> set[str]:
    assert engine.settings is not None
    return set(cast(dict[str, list[str]], engine.settings["permissions"])["deny"])


def filesystem_of(engine: ClaudeCodeLine) -> dict[str, list[str]]:
    assert engine.sandbox is not None
    return cast(dict[str, list[str]], engine.sandbox["filesystem"])


def assert_tools_denied(engine: ClaudeCodeLine, paths: tuple[Path, ...]) -> None:
    rules = rules_of(engine)
    for path in paths:
        for tool in ("Read", "Edit"):
            assert f"{tool}(/{path})" in rules
            assert f"{tool}(/{path}/**)" in rules


def assert_shell_denied(engine: ClaudeCodeLine, paths: tuple[Path, ...]) -> None:
    filesystem = filesystem_of(engine)
    for path in paths:
        assert str(path) in filesystem["denyRead"]
        assert str(path) in filesystem["denyWrite"]


# The list itself.


def test_only_entries_that_exist_are_given(tmp_path: Path) -> None:
    assert credential_paths(tmp_path) == ()
    (tmp_path / ".netrc").write_text("")
    (tmp_path / ".aws").mkdir()
    assert credential_paths(tmp_path) == (tmp_path / ".aws", tmp_path / ".netrc")


def test_a_linked_entry_is_given_by_its_name_and_its_real_path(
    tmp_path: Path,
) -> None:
    home, elsewhere = tmp_path / "home", tmp_path / "elsewhere"
    home.mkdir()
    elsewhere.mkdir()
    (home / ".ssh").symlink_to(elsewhere)
    assert credential_paths(home) == (home / ".ssh", elsewhere)


def test_a_link_whose_target_is_missing_is_still_given_by_name(
    tmp_path: Path,
) -> None:
    (tmp_path / ".netrc").symlink_to(tmp_path / "missing")
    assert tmp_path / ".netrc" in credential_paths(tmp_path)


def test_a_home_reached_through_a_link_is_named_by_its_real_path(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    (real / ".ssh").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    assert credential_paths(tmp_path / "link") == (real / ".ssh",)


def test_an_exempt_entry_is_left_out_and_an_unlisted_one_is_refused(
    tmp_path: Path,
) -> None:
    (tmp_path / ".grok").mkdir()
    (tmp_path / ".grok" / "auth.json").write_text("{}")
    sign_in = Path(".grok/auth.json")
    assert credential_paths(tmp_path, {sign_in}) == ()
    with pytest.raises(ValueError, match="not a listed credential path"):
        credential_paths(tmp_path, {Path(".grok/other.json")})


def test_claude_code_keeps_no_credential_path() -> None:
    """Its sign-in is read by the engine's own process, which neither the sandbox
    nor the rules confine, so nothing is exempted for it."""
    assert not CLAUDE_CODE_EXEMPT
    assert Path(".claude.json") in CREDENTIAL_PATHS
    assert Path(".claude/.credentials.json") in CREDENTIAL_PATHS


def test_the_daemon_home_is_the_real_path_of_the_process_home() -> None:
    assert real_daemon_home() == Path(os.path.realpath(Path.home()))


def test_credential_rules_name_each_path_and_what_lies_under_it(
    tmp_path: Path,
) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real")
    rules = credential_rules([tmp_path / "link"])
    for form in (tmp_path / "link", tmp_path / "real"):
        for tool in ("Read", "Edit"):
            assert f"{tool}(/{form})" in rules and f"{tool}(/{form}/**)" in rules


# Every path that builds a Claude Code line.


def test_a_line_with_an_enabled_sandbox_is_denied_them_in_both(
    tmp_path: Path, home: Path
) -> None:
    engine = built(tmp_path, project={"enabled": True})
    assert_shell_denied(engine, listed(home))
    assert_tools_denied(engine, listed(home))


def test_a_line_with_no_sandbox_keeps_its_file_tools_out(
    tmp_path: Path, home: Path
) -> None:
    engine = built(tmp_path)
    assert engine.sandbox is None
    assert_tools_denied(engine, listed(home))


def test_a_line_whose_sandbox_is_disabled_keeps_its_file_tools_out(
    tmp_path: Path, home: Path
) -> None:
    """The shell then runs as the daemon's user, as it does for every protected
    path, and only the file tools are held."""
    engine = built(
        tmp_path,
        project={"enabled": True},
        config=LineConfig(sandbox=parse_sandbox({"enabled": False}, operator=False)),
    )
    assert engine.sandbox is not None and engine.sandbox.get("enabled") is False
    assert_tools_denied(engine, listed(home))


@pytest.mark.parametrize("sandboxed", [True, False])
def test_a_kept_session_s_options_carry_the_denials(
    tmp_path: Path, home: Path, sandboxed: bool
) -> None:
    """A kept connection is opened with the options of its line's build, so the
    options the engine is started with are checked, not only the build's fields. A
    kept session with a shell needs an enabled sandbox, so the unsandboxed case has
    the file tools alone."""
    tools = ("Read", "Edit", "Bash") if sandboxed else ("Read", "Edit")
    config = LineConfig(keep_session=True, builtin_tools=tools)
    engine = built(tmp_path, config, {"enabled": True} if sandboxed else None)
    options = engine.options()
    flags = cast(dict[str, object], json.loads(cast(str, options.settings)))
    deny = set(cast(dict[str, list[str]], flags["permissions"])["deny"])
    for path in listed(home):
        assert f"Read(/{path})" in deny and f"Edit(/{path}/**)" in deny
    if sandboxed:
        sandbox = cast(dict[str, object], options.sandbox)
        filesystem = cast(dict[str, list[str]], sandbox["filesystem"])
        assert str(home / ".ssh") in filesystem["denyRead"]
    else:
        assert options.sandbox is None


def test_a_line_that_only_reads_its_workdir_is_denied_them(
    tmp_path: Path, home: Path
) -> None:
    engine = built(
        tmp_path,
        LineConfig(workdir_access=WorkdirAccess.READ, builtin_tools=("Read", "Bash")),
    )
    assert_shell_denied(engine, listed(home))
    assert_tools_denied(engine, listed(home))


def test_a_line_with_read_only_paths_is_denied_them(tmp_path: Path, home: Path) -> None:
    (tmp_path / "docs").mkdir()
    engine = built(
        tmp_path,
        LineConfig(builtin_tools=("Read", "Edit", "Bash")),
        {"enabled": True},
        read_only_paths=("docs",),
    )
    assert_shell_denied(engine, listed(home))
    assert_tools_denied(engine, listed(home))


def test_an_empty_home_adds_nothing(tmp_path: Path) -> None:
    engine = built(tmp_path)
    assert engine.settings is None


def test_a_path_made_after_a_build_is_denied_from_the_next(
    tmp_path: Path, home: Path
) -> None:
    assert (
        str(home / ".aws")
        not in filesystem_of(built(tmp_path, project={"enabled": True}))["denyRead"]
    )
    (home / ".aws").mkdir()
    assert (
        str(home / ".aws")
        in filesystem_of(built(tmp_path, project={"enabled": True}))["denyRead"]
    )


def test_a_line_s_own_allow_rule_cannot_reopen_one(tmp_path: Path, home: Path) -> None:
    """Claude Code applies a ``deny`` rule before an ``allow`` rule."""
    settings = parse_settings({"permissions": {"allow": [f"Read(/{home}/.ssh/**)"]}})
    engine = built(tmp_path, LineConfig(settings=settings))
    assert f"Read(/{home / '.ssh'}/**)" in rules_of(engine)


# A document that grants one back.


@pytest.mark.parametrize("key", ["allowRead", "allowWrite"])
@pytest.mark.parametrize(
    "granted", [".ssh", ".ssh/id_test", ".claude.json", ".codex/auth.json"]
)
def test_a_line_s_sandbox_that_grants_one_is_refused(
    tmp_path: Path, home: Path, key: str, granted: str
) -> None:
    own = parse_sandbox({"filesystem": {key: [str(home / granted)]}}, operator=False)
    with pytest.raises(SandboxError, match="directory Dromon protects"):
        built(tmp_path, LineConfig(sandbox=own), {"enabled": True})


def test_a_grant_of_the_home_around_them_is_not_refused_and_loses(
    tmp_path: Path, home: Path
) -> None:
    """A grant of a directory that holds a credential path is not inside it, and the
    deny entry wins over it, as verified live on 2026-10-26."""
    own = parse_sandbox({"filesystem": {"allowRead": [str(home)]}}, operator=False)
    engine = built(tmp_path, LineConfig(sandbox=own), {"enabled": True})
    assert_shell_denied(engine, listed(home))


def test_an_agent_socket_inside_one_is_refused(tmp_path: Path, home: Path) -> None:
    access = GitSshAccess(
        socket=str(home / ".ssh" / "agent.sock"),
        known_hosts="/keys/known_hosts",
        public_key="/keys/deploy.pub",
        known_hosts_sha256="0" * 64,
        public_key_sha256="0" * 64,
    )
    with pytest.raises(SandboxError, match="Dromon protects"):
        built(tmp_path, project={"enabled": True}, git_ssh=access)


def test_the_daemon_refuses_a_project_file_that_grants_one(
    tmp_path: Path, home: Path, short_runtime: Path
) -> None:
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir()
    line = "[lines.a]\nmodel = 'claude-haiku-5-5'\n"
    check_sandboxes(parse("version = 1\n[sandbox]\nenabled = true\n" + line), paths)
    ssh = str(home / ".ssh")
    for granted in (
        f"[sandbox]\nenabled = true\n[sandbox.filesystem]\nallowRead = [{ssh!r}]\n",
        f"{line}[lines.a.sandbox]\nenabled = true\n"
        f"[lines.a.sandbox.filesystem]\nallowWrite = [{ssh + '/x'!r}]\n",
    ):
        text = "version = 1\n" + granted + ("" if "lines.a" in granted else line)
        with pytest.raises(SandboxError, match="directory Dromon protects"):
            check_sandboxes(parse(text), paths)
