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
its own, so what the machine running the tests holds never decides a result.

**Round 18 follow-up, confirmed before the change by the audit line's review and in
the code.** A line with no sandbox document, or one that left ``enabled`` unset, had no
shell denial at all. ``excludedCommands`` and ``allowUnsandboxedCommands`` true were
accepted. A grant of the home, or a pattern such as ``~/.s*``, passed the refusal. A
path that did not exist at the build was not named, and a home reached through a link
was named only by its real form."""

import json
import os
from pathlib import Path
from typing import cast

import pytest

from dromon.configwarnings import sandbox_warnings
from dromon.daemon import check_sandboxes
from dromon.driver import Strategy
from dromon.engine import ClaudeCodeLine
from dromon.gitssh import GitSshAccess
from dromon import security_paths
from dromon.lineconfig import LineConfig, WorkdirAccess
from dromon.paths import project_paths
from dromon.projectconfig import parse
from dromon.sandbox import SandboxError, parse_sandbox
from dromon.scheduler import LineSpec, engine_factory
from dromon.security_paths import (
    CLAUDE_CODE_EXEMPT,
    CLAUDE_CODE_ONLY_PATHS,
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


def test_every_entry_is_named_whether_or_not_it_exists(tmp_path: Path) -> None:
    """Round 18 follow-up, item 4. A missing entry is named, so a file made after a
    line's build is denied by the build it already has."""
    home = Path(os.path.realpath(tmp_path))
    assert credential_paths(home) == tuple(home / entry for entry in CREDENTIAL_PATHS)
    (home / ".netrc").write_text("")
    assert credential_paths(home) == tuple(home / entry for entry in CREDENTIAL_PATHS)


def test_a_linked_entry_is_given_by_its_name_and_its_real_path(
    tmp_path: Path,
) -> None:
    home, elsewhere = tmp_path / "home", tmp_path / "elsewhere"
    home.mkdir()
    elsewhere.mkdir()
    (home / ".ssh").symlink_to(elsewhere)
    found = credential_paths(home)
    assert found[:2] == (home / ".ssh", elsewhere)
    assert found.count(elsewhere) == 1


def test_a_link_whose_target_is_missing_is_given_by_name_and_target(
    tmp_path: Path,
) -> None:
    (tmp_path / ".netrc").symlink_to(tmp_path / "missing")
    found = credential_paths(tmp_path)
    assert tmp_path / ".netrc" in found and tmp_path / "missing" in found


def test_a_home_reached_through_a_link_is_named_both_ways(tmp_path: Path) -> None:
    """Round 18 follow-up, item 4. Claude Code expands ``~`` with the engine's own
    home, which may be the link's spelling, so the rules name both."""
    real = tmp_path / "real"
    (real / ".ssh").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    found = credential_paths(tmp_path / "link")
    assert found[:2] == (tmp_path / "link" / ".ssh", real / ".ssh")
    assert tmp_path / "link" / ".aws" in found and real / ".aws" in found
    assert len(found) == 2 * len(CREDENTIAL_PATHS)


def test_an_exempt_entry_is_left_out_and_an_unlisted_one_is_refused(
    tmp_path: Path,
) -> None:
    sign_in = Path(".grok/auth.json")
    found = credential_paths(tmp_path, {sign_in})
    assert tmp_path / sign_in not in found
    assert len(found) == len(CREDENTIAL_PATHS) - 1
    with pytest.raises(ValueError, match="not a listed credential path"):
        credential_paths(tmp_path, {Path(".grok/other.json")})


def test_a_relative_home_is_refused() -> None:
    with pytest.raises(ValueError, match="not an absolute path"):
        credential_paths(Path("home"))


def test_claude_code_keeps_no_credential_path() -> None:
    """Its sign-in is read by the engine's own process, which neither the sandbox
    nor the rules confine, so nothing is exempted for it."""
    assert not CLAUDE_CODE_EXEMPT
    assert Path(".claude.json") in CREDENTIAL_PATHS
    assert Path(".claude/.credentials.json") in CREDENTIAL_PATHS


def test_the_daemon_home_is_the_process_home_unresolved() -> None:
    """Round 18 follow-up, item 4. Not resolved, so ``credential_paths`` names a home
    reached through a link both ways."""
    assert real_daemon_home() == Path(os.path.abspath(Path.home()))


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


def test_a_line_with_no_sandbox_document_is_given_an_enabled_one(
    tmp_path: Path, home: Path
) -> None:
    """Round 18 follow-up, item 1. Default deny, so the shell is held too."""
    engine = built(tmp_path)
    assert engine.sandbox is not None
    assert engine.sandbox["enabled"] is True
    assert engine.sandbox["allowUnsandboxedCommands"] is False
    assert engine.sandbox["failIfUnavailable"] is True
    assert cast(dict[str, object], engine.sandbox["network"])["strictAllowlist"] is True
    assert_shell_denied(engine, listed(home))
    assert_tools_denied(engine, listed(home))


def test_a_sandbox_that_leaves_enabled_unset_is_enabled(
    tmp_path: Path, home: Path
) -> None:
    engine = built(tmp_path, project={"network": {"allowedDomains": ["pypi.org"]}})
    assert engine.sandbox is not None and engine.sandbox["enabled"] is True
    network = cast(dict[str, list[str]], engine.sandbox["network"])
    assert network["allowedDomains"] == ["pypi.org"]
    assert_shell_denied(engine, listed(home))


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


def test_a_disabled_sandbox_is_warned_of_at_the_build(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    del home
    built(tmp_path, project={"enabled": False})
    assert "alpha runs Claude Code with its sandbox disabled" in capsys.readouterr().err
    built(tmp_path)
    assert "disabled" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "project",
    [
        pytest.param({"enabled": True}, id="enabled"),
        pytest.param(None, id="absent"),
        pytest.param({"enabled": False}, id="disabled"),
    ],
)
def test_a_kept_session_s_options_carry_the_denials(
    tmp_path: Path, home: Path, project: dict[str, object] | None
) -> None:
    """A kept connection is opened with the options of its line's build, so the
    options the engine is started with are checked, not only the build's fields. A
    kept session with a shell needs an enabled sandbox, so the disabled case has the
    file tools alone."""
    sandboxed = project is None or project["enabled"] is True
    tools = ("Read", "Edit", "Bash") if sandboxed else ("Read", "Edit")
    config = LineConfig(keep_session=True, builtin_tools=tools)
    engine = built(tmp_path, config, project)
    options = engine.options()
    flags = cast(dict[str, object], json.loads(cast(str, options.settings)))
    deny = set(cast(dict[str, list[str]], flags["permissions"])["deny"])
    for path in listed(home):
        assert f"Read(/{path})" in deny and f"Edit(/{path}/**)" in deny
    sandbox = cast(dict[str, object], options.sandbox)
    if sandboxed:
        filesystem = cast(dict[str, list[str]], sandbox["filesystem"])
        assert str(home / ".ssh") in filesystem["denyRead"]
    else:
        assert sandbox == {"enabled": False}


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


def test_a_path_that_does_not_exist_yet_is_denied(tmp_path: Path, home: Path) -> None:
    """Round 18 follow-up, item 4. Denied in both layers before it is made, and a
    link made there later is denied by its target from the next build."""
    missing = home / ".aws"
    assert not os.path.lexists(missing)
    engine = built(tmp_path, project={"enabled": True})
    assert_shell_denied(engine, (missing,))
    assert_tools_denied(engine, (missing,))
    target = tmp_path / "aws-elsewhere"
    target.mkdir()
    missing.symlink_to(target)
    assert_shell_denied(built(tmp_path, project={"enabled": True}), (missing, target))


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


@pytest.mark.parametrize("key", ["allowRead", "allowWrite"])
@pytest.mark.parametrize(
    "granted",
    [
        pytest.param("{home}", id="the-home"),
        pytest.param("{parent}", id="above-the-home"),
        pytest.param("/", id="the-root"),
        pytest.param("{home}/.s*", id="star-prefix"),
        pytest.param("{home}/.s?h", id="question-mark"),
        pytest.param("{home}/*", id="every-entry"),
        pytest.param("{home}/.[a-z]sh", id="bracket"),
        pytest.param("{home}/{{.ssh,.aws}}", id="brace"),
        pytest.param("{home}/.ssh", id="a-missing-path-itself"),
        pytest.param("{home}/.config/gh/*.yml", id="pattern-inside-one"),
    ],
)
def test_a_grant_that_could_reach_one_is_refused(
    tmp_path: Path, key: str, granted: str
) -> None:
    """Round 18 follow-up, item 3. An allow entry lifts a deny entry above it on
    Claude Code, so a grant of the path, of a directory holding it, inside it, or a
    pattern whose literal base is one of those, is refused. The home here is empty,
    so the paths are refused whether or not they exist."""
    home = security_paths.daemon_home()
    entry = granted.format(home=home, parent=home.parent)
    own = parse_sandbox({"filesystem": {key: [entry]}}, operator=False)
    with pytest.raises(SandboxError, match="credential path|Dromon protects"):
        built(tmp_path, LineConfig(sandbox=own))


@pytest.mark.parametrize(
    "granted",
    [
        pytest.param("{home}/.cache/uv", id="a-cache"),
        pytest.param("{home}/.cache/*", id="a-pattern-elsewhere"),
        pytest.param("{home}/.sshx", id="a-sibling-name"),
        pytest.param("{elsewhere}/*", id="outside-the-home"),
    ],
)
def test_a_grant_that_cannot_reach_one_is_accepted(
    tmp_path: Path, granted: str
) -> None:
    home = security_paths.daemon_home()
    entry = granted.format(home=home, elsewhere=tmp_path / "elsewhere")
    own = parse_sandbox({"filesystem": {"allowWrite": [entry]}}, operator=False)
    engine = built(tmp_path, LineConfig(sandbox=own))
    assert entry in filesystem_of(engine)["allowWrite"]
    assert_shell_denied(engine, (home / ".ssh",))


def test_excluded_commands_are_refused_for_a_line_sandboxed_by_default(
    tmp_path: Path,
) -> None:
    """Round 18 follow-up, item 2. A command named there runs outside the sandbox,
    where every credential path is readable."""
    own = parse_sandbox({"excludedCommands": ["git"]}, operator=False)
    with pytest.raises(SandboxError, match="excludedCommands run outside"):
        built(tmp_path, LineConfig(sandbox=own))
    disabled = parse_sandbox(
        {"enabled": False, "excludedCommands": ["git"]}, operator=False
    )
    assert built(tmp_path, LineConfig(sandbox=disabled)).sandbox is not None


def test_unsandboxed_commands_are_forced_off(tmp_path: Path) -> None:
    """Round 18 follow-up, item 2. Even from the client, which has operator
    authority, and even where the project's layer sets it."""
    own = parse_sandbox({"allowUnsandboxedCommands": True}, operator=True)
    for engine in (
        built(tmp_path, LineConfig(sandbox=own)),
        built(tmp_path, project={"enabled": True, "allowUnsandboxedCommands": True}),
    ):
        assert engine.sandbox is not None
        assert engine.sandbox["allowUnsandboxedCommands"] is False


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


@pytest.mark.parametrize(
    "layer",
    [
        pytest.param("[sandbox.filesystem]\nallowRead = [{home!r}]\n", id="home"),
        pytest.param("[sandbox.filesystem]\nallowRead = [{glob!r}]\n", id="glob"),
        pytest.param("[sandbox]\nexcludedCommands = ['git']\n", id="excluded"),
    ],
)
def test_the_daemon_checks_a_line_sandboxed_by_default(
    tmp_path: Path, short_runtime: Path, layer: str
) -> None:
    """Round 18 follow-up, items 1 to 3. The project file sets no ``enabled``, so the
    line is checked with the sandbox its build gives it, and refused as the build
    refuses it. The same layer with ``enabled`` false is accepted."""
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir()
    home = str(security_paths.daemon_home())
    text = layer.format(home=home, glob=home + "/.s*")
    line = "[lines.a]\nmodel = 'claude-haiku-5-5'\n"
    with pytest.raises(SandboxError, match="credential path|excludedCommands"):
        check_sandboxes(parse("version = 1\n" + text + line), paths)
    if text.startswith("[sandbox]\n"):
        disabled = text.replace("[sandbox]\n", "[sandbox]\nenabled = false\n", 1)
    else:
        disabled = "[sandbox]\nenabled = false\n" + text
    check_sandboxes(parse("version = 1\n" + disabled + line), paths)


def test_the_warnings_name_a_disabled_line_and_an_overridden_key() -> None:
    config = parse(
        "version = 1\n[sandbox]\nallowUnsandboxedCommands = true\n"
        "[lines.a]\nmodel = 'claude-haiku-5-5'\n"
        "[lines.a.sandbox]\nenabled = false\nallowUnsandboxedCommands = true\n"
        "[lines.b]\nmodel = 'claude-haiku-5-5'\n"
    )
    found = sandbox_warnings(config)
    assert len(found) == 3
    assert found[0].startswith("the project's sandbox sets allowUnsandboxedCommands")
    assert found[1].startswith("line a's sandbox sets allowUnsandboxedCommands")
    assert found[2].startswith("line a runs Claude Code with its sandbox disabled")
    assert (
        sandbox_warnings(parse("version = 1\n[lines.b]\nmodel = 'claude-haiku-5-5'\n"))
        == []
    )


# Round 18, last pass.


@pytest.mark.parametrize("key", ["allowRead", "allowWrite"])
@pytest.mark.parametrize(
    "granted",
    [
        pytest.param("{home}/.SSH", id="a-listed-directory"),
        pytest.param("{home}/.Ssh/id_test", id="inside-one"),
        pytest.param("{upper}", id="the-home"),
        pytest.param("{home}/.CONFIG/*", id="a-pattern-above-one"),
        pytest.param("{home}/LIBRARY", id="above-a-claude-only-one"),
    ],
)
def test_a_grant_in_another_case_is_refused(
    tmp_path: Path, key: str, granted: str
) -> None:
    """Round 18, last pass, item 1. Confirmed before the change, a grant of the
    ``.SSH`` spelling was accepted, and a volume that ignores case names the ssh
    directory by it. The comparison is folded on every host, so this holds on a
    volume that keeps case apart too."""
    home = security_paths.daemon_home()
    entry = granted.format(home=home, upper=str(home).upper())
    own = parse_sandbox({"filesystem": {key: [entry]}}, operator=False)
    with pytest.raises(SandboxError, match="credential path|Dromon protects"):
        built(tmp_path, LineConfig(sandbox=own))


def test_a_grant_of_a_protected_directory_in_another_case_is_refused(
    tmp_path: Path,
) -> None:
    """Round 18, last pass, item 1. The refusal of a grant inside a protected
    directory, such as the project's secret directory, is folded as well."""
    (tmp_path / "secret").mkdir()
    own = parse_sandbox(
        {"filesystem": {"allowRead": [str(tmp_path / "SECRET" / "x")]}},
        operator=False,
    )
    with pytest.raises(SandboxError, match="directory Dromon protects"):
        built(tmp_path, LineConfig(sandbox=own))
    other = parse_sandbox(
        {"filesystem": {"allowRead": [str(tmp_path / "secrets")]}}, operator=False
    )
    built(tmp_path, LineConfig(sandbox=other))


def test_the_claude_code_only_paths_are_denied_to_claude_lines_alone(
    tmp_path: Path,
) -> None:
    """Round 18, last pass, item 5. Denied in the shell and to the file tools of a
    Claude Code line, missing or not, and not part of the list Codex and Grok lines
    take."""
    home = security_paths.daemon_home()
    only = tuple(home / entry for entry in CLAUDE_CODE_ONLY_PATHS)
    assert {str(entry) for entry in CLAUDE_CODE_ONLY_PATHS} == {
        "Library/Keychains",
        ".config/gcloud",
        ".azure",
    }
    assert not set(CLAUDE_CODE_ONLY_PATHS) & set(CREDENTIAL_PATHS)
    assert set(only) <= set(security_paths.claude_credentials())
    assert not set(only) & set(credential_paths(home))
    engine = built(tmp_path)
    assert_shell_denied(engine, only)
    assert_tools_denied(engine, only)


def test_grok_is_not_denied_the_claude_code_only_paths(tmp_path: Path) -> None:
    """Round 18, last pass, item 5. Grok's own binary reads two of them, so its
    profile's list is unchanged."""
    from dromon import grok

    home = Path(os.path.realpath(tmp_path))
    for entry in CLAUDE_CODE_ONLY_PATHS:
        (home / entry).mkdir(parents=True)
    (home / ".ssh").mkdir()
    found = grok._credential_paths(home)  # pyright: ignore[reportPrivateUsage]
    assert home / ".ssh" in found
    assert not {home / entry for entry in CLAUDE_CODE_ONLY_PATHS} & set(found)


@pytest.mark.parametrize(
    "granted",
    [
        pytest.param("{home}/Library", id="above-the-keychains"),
        pytest.param("{home}/Library/Keychains/login.keychain-db", id="a-keychain"),
        pytest.param("{home}/.config/gcloud", id="gcloud"),
        pytest.param("{home}/.azure/*", id="inside-azure"),
        pytest.param("{home}/Lib*", id="a-pattern-above-them"),
    ],
)
def test_a_grant_of_a_claude_code_only_path_is_refused(
    tmp_path: Path, granted: str
) -> None:
    home = security_paths.daemon_home()
    entry = granted.format(home=home)
    own = parse_sandbox({"filesystem": {"allowRead": [entry]}}, operator=False)
    with pytest.raises(SandboxError, match="credential path|Dromon protects"):
        built(tmp_path, LineConfig(sandbox=own))


@pytest.mark.parametrize(
    "granted", ["{home}/Library/Caches", "{home}/.config/gcloudx", "{home}/.azurex"]
)
def test_a_grant_beside_a_claude_code_only_path_is_accepted(
    tmp_path: Path, granted: str
) -> None:
    entry = granted.format(home=security_paths.daemon_home())
    own = parse_sandbox({"filesystem": {"allowWrite": [entry]}}, operator=False)
    assert (
        entry in filesystem_of(built(tmp_path, LineConfig(sandbox=own)))["allowWrite"]
    )


@pytest.mark.parametrize(
    "extra", [Path("/abs"), Path("../up"), Path("a/../../up"), Path()]
)
def test_an_extra_entry_outside_the_home_is_refused(
    tmp_path: Path, extra: Path
) -> None:
    with pytest.raises(ValueError, match="not a path within the home"):
        credential_paths(tmp_path, extra=(extra,))


def test_an_extra_entry_may_be_exempted(tmp_path: Path) -> None:
    home = Path(os.path.realpath(tmp_path))
    extra = (Path(".azure"),)
    assert home / ".azure" in credential_paths(home, extra=extra)
    assert home / ".azure" not in credential_paths(home, set(extra), extra)


def test_the_daemon_checks_a_project_sandbox_with_no_lines(
    tmp_path: Path, short_runtime: Path
) -> None:
    """Round 18, last pass, from the audit line's re-check. A project file that
    declares no line has its own sandbox checked for grants above a credential path,
    which no line's check then covers."""
    paths = project_paths(tmp_path, short_runtime)
    paths.state_dir.mkdir()
    home = str(security_paths.daemon_home())
    text = (
        "version = 1\n[sandbox]\nenabled = true\n"
        f"[sandbox.filesystem]\nallowRead = [{home!r}]\n"
    )
    with pytest.raises(SandboxError, match="credential path"):
        check_sandboxes(parse(text), paths)
    check_sandboxes(parse(text.replace(home, home + "/.cache/uv")), paths)


# Round 18, addendum 2, item 2.

COMPOSED = "café"
DECOMPOSED = "café"


@pytest.fixture
def accented_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A daemon home whose name holds an accented letter in the composed form, with
    an ssh directory in it, as the audit line's spelling gap needed."""
    base = Path(os.path.realpath(tmp_path_factory.mktemp("accented")))
    accented = base / COMPOSED
    (accented / ".ssh").mkdir(parents=True)
    monkeypatch.setattr(security_paths, "daemon_home", lambda: accented)
    return accented


@pytest.mark.parametrize(
    "suffix",
    [
        pytest.param("", id="the-home"),
        pytest.param("/.ssh", id="a-listed-directory"),
        pytest.param("/.ssh/id_test", id="inside-one"),
        pytest.param("/.S*", id="a-pattern-in-another-case-too"),
    ],
)
def test_a_grant_in_another_normalisation_form_is_refused(
    tmp_path: Path, accented_home: Path, suffix: str
) -> None:
    """Round 18, addendum 2, item 2. Confirmed before the change, a grant spelled with
    a decomposed accent was accepted under a home spelled with a composed one, while
    a volume that ignores the normalisation form opens the same directory for both.
    The comparison is normalised on both sides, on every host."""
    spelled = str(accented_home.parent / DECOMPOSED) + suffix
    assert spelled != str(accented_home) + suffix
    own = parse_sandbox({"filesystem": {"allowRead": [spelled]}}, operator=False)
    with pytest.raises(SandboxError, match="credential path|Dromon protects"):
        built(tmp_path, LineConfig(sandbox=own))


def test_a_grant_of_a_name_that_only_looks_alike_is_accepted(
    tmp_path: Path, accented_home: Path
) -> None:
    """The negative path. A name without the accent is another name in every form, so
    a grant of it is not refused."""
    plain = str(accented_home.parent / "cafe")
    own = parse_sandbox({"filesystem": {"allowRead": [plain]}}, operator=False)
    built(tmp_path, LineConfig(sandbox=own))


def test_a_protected_directory_in_another_normalisation_form_is_refused(
    tmp_path: Path,
) -> None:
    root = tmp_path / COMPOSED
    (root / "secret").mkdir(parents=True)
    spelled = str(tmp_path / DECOMPOSED / "secret" / "x")
    own = parse_sandbox({"filesystem": {"allowRead": [spelled]}}, operator=False)
    with pytest.raises(SandboxError, match="directory Dromon protects"):
        built(root, LineConfig(sandbox=own))
