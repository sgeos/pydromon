"""The ``[git_ssh]`` option, which forwards one agent socket and pinned host keys to
the lines the project file names. Required by the operator on 2026-10-29.

A real agent socket needs a bound Unix domain socket, which the agent sandbox denies,
so most tests give ``os.stat`` a socket's mode for one path. The test that binds one
is marked ``socket``."""

import base64
import hashlib
import hmac
import os
import shlex
import socket
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from dromon import cli, exits, gitssh, launcher
from dromon.daemon import git_ssh_access
from dromon.driver import Driver, Strategy
from dromon.engine import ClaudeCodeLine
from dromon.gitssh import (
    COMMAND_VARIABLE,
    HOST,
    INHERIT,
    GitSshAccess,
    GitSshConfig,
    GitSshError,
    changed,
    environment,
    parse_git_ssh,
    pins,
    resolve,
    sandbox_layer,
    ssh_command,
    well_formed,
)
from dromon.lineconfig import LineConfig
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ConfigFileError, ProjectConfig, parse
from dromon.sandbox import SandboxError, parse_sandbox
from dromon.scheduler import LineSpec, engine_factory


def blob(*fields: bytes) -> str:
    """The base64 of a key blob made of ``fields``, each length prefixed."""
    return base64.b64encode(
        b"".join(len(field).to_bytes(4, "big") + field for field in fields)
    ).decode()


KEY_BLOB = blob(b"ssh-ed25519", bytes(range(32)))
"""A key of the right shape, not GitHub's, whose keys stay out of the code."""
HOSTS_LINE = f"{HOST} ssh-ed25519 {KEY_BLOB}\n"
PUBLIC_KEY = f"ssh-ed25519 {KEY_BLOB} deploy@example\n"
ALPHA = "version = 1\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\n"

MODELS = {
    "claude": "claude-haiku-5-5",
    "codex": "gpt-6-luna",
    "grok": "grok-4.7",
    "pi": "pi:openai/gpt-6-luna",
    "cursor": "cursor-agent:gpt-5.6-luna-medium",
    "antigravity": "antigravity:gemini-3.8-flash-high",
}


def fake_socket(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    *,
    uid: int | None = None,
    directory_mode: int = 0o700,
    directory_uid: int | None = None,
) -> str:
    """Make ``os.stat`` in ``dromon.gitssh`` see a socket at ``path``, owned by
    ``uid`` or the operator, in a directory of ``directory_mode`` owned by
    ``directory_uid`` or the operator, and return the socket's real path."""
    real = os.path.realpath(path)
    original: Callable[..., os.stat_result] = os.stat
    owner = os.getuid() if uid is None else uid
    directory_owner = os.getuid() if directory_uid is None else directory_uid

    def fake(target: str | Path, *args: object, **kwargs: object) -> os.stat_result:
        if str(target) == real:
            return os.stat_result(
                (stat.S_IFSOCK | 0o600, 0, 0, 1, owner, 0, 0, 0, 0, 0)
            )
        if str(target) == os.path.dirname(real):
            return os.stat_result(
                (stat.S_IFDIR | directory_mode, 0, 0, 1, directory_owner, 0, 0, 0, 0, 0)
            )
        return original(target, *args, **kwargs)

    monkeypatch.setattr(gitssh.os, "stat", fake)
    return real


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> str:
    """A short socket path, as an agent's is, that ``os.stat`` sees as a socket."""
    return fake_socket(monkeypatch, f"/tmp/dromon-test-agent-{os.getpid()}.sock")  # noqa: S108


@pytest.fixture
def files(tmp_path: Path) -> Path:
    """A project root with a ``known_hosts`` pinning github.com and a public key."""
    (tmp_path / "ssh").mkdir()
    (tmp_path / "ssh" / "known_hosts").write_text(HOSTS_LINE)
    (tmp_path / "ssh" / "deploy.pub").write_text(PUBLIC_KEY)
    for name in ("known_hosts", "deploy.pub"):
        (tmp_path / "ssh" / name).chmod(0o644)
    return tmp_path


def config(agent: str = INHERIT) -> GitSshConfig:
    return GitSshConfig(
        agent=agent, known_hosts="ssh/known_hosts", public_key="ssh/deploy.pub"
    )


def table(agent: str) -> str:
    return (
        f"[git_ssh]\nagent = {agent!r}\nknown_hosts = 'ssh/known_hosts'\n"
        "public_key = 'ssh/deploy.pub'\n"
    )


# The project file.


def test_the_option_is_off_by_default() -> None:
    project = parse("version = 1\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\n")
    assert project.git_ssh is None
    assert project.lines["alpha"].git_ssh is False
    assert ProjectConfig().git_ssh is None
    assert environment(None) == {}


def test_a_line_or_a_profile_enables_it_only_beside_the_table() -> None:
    text = (
        "version = 1\n"
        + table(INHERIT)
        + "[profiles.pusher]\nmodel = 'claude-haiku-5-5'\ngit_ssh = true\n"
        "[lines.alpha]\nprofile = 'pusher'\n"
        "[lines.beta]\nprofile = 'pusher'\ngit_ssh = false\n"
        "[lines.gamma]\nmodel = 'claude-haiku-5-5'\ngit_ssh = true\n"
    )
    project = parse(text)
    assert project.git_ssh == config(INHERIT)
    assert {i: line.git_ssh for i, line in project.lines.items()} == {
        "alpha": True,
        "beta": False,
        "gamma": True,
    }
    with pytest.raises(ConfigFileError, match="no \\[git_ssh\\] table") as raised:
        parse(ALPHA + "git_ssh = true\n")
    assert raised.value.key == "lines.alpha.git_ssh"
    with pytest.raises(ConfigFileError, match="must be a boolean"):
        parse(ALPHA + "git_ssh = 1\n")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("not a table", "must be a table"),
        ({"agent": INHERIT, "known_hosts": "k"}, "public_key is required"),
        (
            {"agent": INHERIT, "known_hosts": "k", "public_key": "p", "key": "x"},
            "git_ssh.key is not a known key",
        ),
        ({"agent": "agent.sock", "known_hosts": "k", "public_key": "p"}, "absolute"),
        ({"agent": INHERIT, "known_hosts": "~/k", "public_key": "p"}, "relative"),
        ({"agent": INHERIT, "known_hosts": "", "public_key": "p"}, "1 to 4096"),
        ({"agent": INHERIT, "known_hosts": "k", "public_key": 3}, "1 to 4096"),
    ],
)
def test_a_malformed_table_is_refused(value: object, message: str) -> None:
    with pytest.raises(GitSshError, match=message):
        parse_git_ssh(value)


@pytest.mark.parametrize("character", ['"', "'", "\\", "$", "%", "\n", "\x00"])
def test_a_path_ssh_would_reinterpret_is_refused(character: str) -> None:
    with pytest.raises(GitSshError, match="must not hold"):
        parse_git_ssh(
            {"agent": f"/run/a{character}b", "known_hosts": "k", "public_key": "p"}
        )


def test_the_file_places_a_refusal_on_its_key() -> None:
    with pytest.raises(ConfigFileError) as raised:
        parse("version = 1\n" + table("x"))
    assert raised.value.key == "git_ssh.agent"
    # The reader names the line of the key's table, as for every other table.
    assert raised.value.line == 2


# Resolution against the file system.


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_both_socket_forms_resolve(files: Path, agent: str) -> None:
    named = resolve(config(agent), files, {})
    inherited = resolve(config(INHERIT), files, {"SSH_AUTH_SOCK": agent})
    hosts, key = files / "ssh" / "known_hosts", files / "ssh" / "deploy.pub"
    assert (
        named
        == inherited
        == GitSshAccess(
            socket=agent,
            known_hosts=os.path.realpath(hosts),
            public_key=os.path.realpath(key),
            known_hosts_sha256=digest(hosts),
            public_key_sha256=digest(key),
        )
    )
    absolute = GitSshConfig(
        agent=agent,
        known_hosts=str(files / "ssh" / "known_hosts"),
        public_key=str(files / "ssh" / "deploy.pub"),
    )
    assert resolve(absolute, Path("/elsewhere"), {}) == named


def test_an_inherited_agent_needs_the_daemons_socket(files: Path) -> None:
    with pytest.raises(GitSshError, match="has no SSH_AUTH_SOCK"):
        resolve(config(INHERIT), files, {})
    with pytest.raises(GitSshError, match="has no SSH_AUTH_SOCK"):
        resolve(config(INHERIT), files, {"SSH_AUTH_SOCK": ""})
    with pytest.raises(GitSshError, match="not absolute"):
        resolve(config(INHERIT), files, {"SSH_AUTH_SOCK": "agent.sock"})


def test_a_named_path_must_be_an_existing_socket_of_the_operator(
    files: Path, monkeypatch: pytest.MonkeyPatch, short_runtime: Path
) -> None:
    # Pytest's own temporary paths can pass the socket length limit outside the
    # agent sandbox, so the paths that must reach the later checks are kept short.
    short = short_runtime.parent
    missing, plain = short / "missing.sock", short / "plain"
    plain.write_text("")
    for path in (missing, plain):
        assert len(os.path.realpath(path).encode()) <= gitssh.MAX_SOCKET_BYTES
    with pytest.raises(GitSshError, match="does not exist"):
        resolve(config(str(missing)), files, {})
    with pytest.raises(GitSshError, match="is not a socket"):
        resolve(config(str(plain)), files, {})
    other = fake_socket(monkeypatch, "/tmp/dromon-other.sock", uid=os.getuid() + 1)  # noqa: S108
    with pytest.raises(GitSshError, match="belongs to another user"):
        resolve(config(other), files, {})
    with pytest.raises(GitSshError, match="longer than 103 bytes"):
        resolve(config("/" + "s" * 103), files, {})


def test_a_socket_in_a_directory_another_may_write_is_refused(
    files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of 2026-10-29, L5. Another user who may write the directory could put
    a socket of their own in the socket's place after the check."""
    path = "/run/dromon-test/agent.sock"
    for mode, owner in (
        (0o777 | stat.S_ISVTX, None),
        (0o770, None),
        (0o702, None),
        (0o700, os.getuid() + 1),
    ):
        fake_socket(monkeypatch, path, directory_mode=mode, directory_uid=owner)
        with pytest.raises(GitSshError, match="which another user may write"):
            resolve(config(path), files, {})
    for owner in (None, 0):
        fake_socket(monkeypatch, path, directory_mode=0o755, directory_uid=owner)
        assert resolve(config(path), files, {}).socket == os.path.realpath(path)


@pytest.mark.socket
def test_a_bound_socket_is_accepted(files: Path, short_runtime: Path) -> None:
    short_runtime.mkdir(mode=0o700)
    path = str(short_runtime / "agent.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(path)
        assert resolve(config(path), files, {}).socket == os.path.realpath(path)
    finally:
        server.close()
        os.unlink(path)


def test_known_hosts_must_exist_and_pin_github(files: Path, agent: str) -> None:
    hosts = files / "ssh" / "known_hosts"
    refusals = {
        "": "holds no key for github.com",
        f"*.com ssh-ed25519 {KEY_BLOB}\n": "holds no key",
        f"!{HOST} ssh-ed25519 {KEY_BLOB}\n": "holds no key",
        f"@revoked {HOST} ssh-ed25519 {KEY_BLOB}\n": "holds no key",
        f"# {HOST} ssh-ed25519 {KEY_BLOB}\n": "holds no key",
        f"{HOST} ssh-ed25519 not*base64\n": "holds no key",
        f"{HOST} ssh-ed25519 AAAA\n": "holds no key",
        f"{HOST} ssh-foo {blob(b'ssh-foo', bytes(32))}\n": "holds no key",
        f"{HOST} ssh-rsa {KEY_BLOB}\n": "holds no key",
        f"!{HOST},{HOST} ssh-ed25519 {KEY_BLOB}\n": "holds no key",
        f"!gitlab.com,{HOST} ssh-ed25519 {KEY_BLOB}\n": "holds no key",
        f"{HOST}\n": "holds no key",
        f"gitlab.com ssh-ed25519 {KEY_BLOB}\n": "holds no key",
    }
    for text, message in refusals.items():
        hosts.write_text(text)
        with pytest.raises(GitSshError, match=message):
            resolve(config(agent), files, {})
    hosts.unlink()
    with pytest.raises(GitSshError, match="known_hosts .* does not exist"):
        resolve(config(agent), files, {})
    hosts.write_text(HOSTS_LINE)
    hosts.chmod(0o664)
    with pytest.raises(GitSshError, match="writable by no one else"):
        resolve(config(agent), files, {})
    hosts.chmod(0o644)
    resolve(config(agent), files, {})


def test_a_hashed_or_listed_name_pins_the_host() -> None:
    salt = b"0123456789abcdefghij"
    digest = hmac.new(salt, HOST.encode(), hashlib.sha1).digest()
    hashed = f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(digest).decode()}"
    assert pins(f"{hashed} ssh-ed25519 {KEY_BLOB}\n")
    wrong = hmac.new(salt, b"gitlab.com", hashlib.sha1).digest()
    other = f"|1|{base64.b64encode(salt).decode()}|{base64.b64encode(wrong).decode()}"
    assert not pins(f"{other} ssh-ed25519 {KEY_BLOB}\n")
    assert not pins(f"|1|bad|hash ssh-ed25519 {KEY_BLOB}\n")
    assert pins(f"gitlab.com,GitHub.com ssh-ed25519 {KEY_BLOB}\n")


def test_the_public_key_must_be_one_public_key(files: Path, agent: str) -> None:
    key = files / "ssh" / "deploy.pub"
    for text, message in {
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n": "holds a private key",
        PUBLIC_KEY + PUBLIC_KEY: "exactly one well-formed public key line",
        "": "exactly one well-formed public key line",
        "ssh-ed25519\n": "exactly one well-formed public key line",
        "ssh-ed25519 not*base64\n": "exactly one well-formed public key line",
        "ssh-ed25519 AAAA\n": "exactly one well-formed public key line",
        f"ecdsa-sha2-nistp256 {KEY_BLOB}\n": "exactly one well-formed",
        f"ssh-dss {blob(b'ssh-dss', b'p', b'q', b'g', b'y')}\n": "well-formed",
    }.items():
        key.write_text(text)
        with pytest.raises(GitSshError, match=message):
            resolve(config(agent), files, {})
    key.write_bytes(b"\xff\xfe")
    with pytest.raises(GitSshError, match="not UTF-8"):
        resolve(config(agent), files, {})


POINT = {
    curve: b"\x04" + bytes(size - 1) for curve, size in gitssh.ECDSA_POINT_BYTES.items()
}


@pytest.mark.parametrize(
    ("key_type", "fields"),
    [
        ("ssh-ed25519", [bytes(32)]),
        ("sk-ssh-ed25519@openssh.com", [bytes(32), b"ssh:"]),
        ("ssh-rsa", [b"\x01\x00\x01", b"\x00" + bytes(256)]),
        ("ecdsa-sha2-nistp256", [b"nistp256", POINT["nistp256"]]),
        ("ecdsa-sha2-nistp384", [b"nistp384", POINT["nistp384"]]),
        ("ecdsa-sha2-nistp521", [b"nistp521", POINT["nistp521"]]),
        (
            "sk-ecdsa-sha2-nistp256@openssh.com",
            [b"nistp256", POINT["nistp256"], b"ssh:"],
        ),
    ],
)
def test_each_accepted_key_type_parses_and_its_damage_is_refused(
    key_type: str, fields: list[bytes]
) -> None:
    """Review of 2026-10-29, M2 and L2. The blob's own type must be the field's,
    from the explicit set, with data of that type's shape and nothing after it."""
    assert key_type in gitssh.KEY_TYPES
    name = key_type.encode()
    assert well_formed(key_type, blob(name, *fields))
    assert not well_formed(key_type, blob(name, *fields[:-1]))
    assert not well_formed(key_type, blob(name, *fields, b"extra"))
    assert not well_formed(key_type, blob(name, *fields)[:-4])
    other = next(iter(sorted(gitssh.KEY_TYPES - {key_type})))
    assert not well_formed(other, blob(name, *fields))
    assert not well_formed(key_type, blob(other.encode(), *fields))
    raw = base64.b64decode(blob(name, *fields))
    assert not well_formed(key_type, base64.b64encode(raw + b"\x00\x00").decode())


def test_a_malformed_key_body_is_refused() -> None:
    assert not well_formed("ssh-ed25519", "AAAA")
    assert not well_formed("ssh-ed25519", blob(b"ssh-ed25519", bytes(31)))
    assert not well_formed("ssh-rsa", blob(b"ssh-rsa", b"", b"n"))
    assert not well_formed(
        "ecdsa-sha2-nistp256",
        blob(b"ecdsa-sha2-nistp256", b"nistp384", POINT["nistp256"]),
    )
    assert not well_formed(
        "ecdsa-sha2-nistp256",
        blob(b"ecdsa-sha2-nistp256", b"nistp256", b"\x02" + POINT["nistp256"][1:]),
    )
    assert not well_formed("ssh-foo", blob(b"ssh-foo", bytes(32)))
    assert not well_formed(
        "ssh-ed25519-cert-v01@openssh.com", blob(b"ssh-ed25519-cert-v01@openssh.com")
    )
    many = blob(b"ssh-ed25519", *([b"x"] * gitssh.MAX_BLOB_FIELDS))
    assert not well_formed("ssh-ed25519", many)


def test_a_change_to_either_file_after_load_is_detected(
    files: Path, agent: str
) -> None:
    """Review of 2026-10-29, M1. The digests taken at load are compared before each
    turn, and anything but the same contents of a file of the operator's counts."""
    access = resolve(config(agent), files, {})
    assert changed(access) is None
    hosts, key = files / "ssh" / "known_hosts", files / "ssh" / "deploy.pub"
    hosts.write_text(
        HOSTS_LINE + f"{HOST} ssh-ed25519 {blob(b'ssh-ed25519', bytes(32))}\n"
    )
    assert changed(access) == access.known_hosts
    hosts.write_text(HOSTS_LINE)
    assert changed(access) is None
    key.chmod(0o666)
    assert changed(access) == access.public_key
    key.chmod(0o644)
    key.unlink()
    assert changed(access) == access.public_key
    key.write_text(PUBLIC_KEY)
    key.chmod(0o644)
    assert changed(access) is None


def test_a_file_inside_a_protected_directory_is_refused(
    files: Path, agent: str
) -> None:
    with pytest.raises(GitSshError, match="which Dromon protects"):
        resolve(config(agent), files, {}, protected=(files / "ssh",))
    resolve(config(agent), files, {}, protected=(files / "elsewhere",))


# The command and the variables.


def access_at(directory: Path, socket_path: str) -> GitSshAccess:
    return GitSshAccess(
        socket=socket_path,
        known_hosts=str(directory / "known_hosts"),
        public_key=str(directory / "deploy.pub"),
        known_hosts_sha256="0" * 64,
        public_key_sha256="0" * 64,
    )


def test_the_command_pins_hosts_and_offers_one_key() -> None:
    access = access_at(Path("/keys"), "/run/agent.sock")
    words = shlex.split(ssh_command(access))
    assert words[:3] == ["ssh", "-F", "/dev/null"]
    options = words[4::2]
    assert words[3::2] == ["-o"] * len(options)
    assert options == [
        f'IdentityAgent="{access.socket}"',
        f'IdentityFile="{access.public_key}"',
        "IdentitiesOnly=yes",
        "StrictHostKeyChecking=yes",
        f'UserKnownHostsFile="{access.known_hosts}"',
        "GlobalKnownHostsFile=/dev/null",
        "UpdateHostKeys=no",
        "BatchMode=yes",
    ]
    assert environment(access) == {COMMAND_VARIABLE: ssh_command(access)}


def test_a_space_in_a_path_survives_the_shell_git_uses() -> None:
    """git runs the command with ``sh -c``, so the words a real shell splits it into
    are compared with the words intended."""
    access = access_at(Path("/key files/a b"), "/run/my agent.sock")
    command = ssh_command(access)
    echoed = subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", "printf '%s\\n' " + command.removeprefix("ssh ")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert ["ssh", *echoed.stdout.splitlines()] == shlex.split(command)
    assert f'IdentityAgent="{access.socket}"' in echoed.stdout.splitlines()


# The sandbox.


ROOT = Path("/work/project")
STATE, RUN = ROOT / ".dromon", Path("/run/dromon")


def built(
    access: GitSshAccess | None,
    model: str = MODELS["claude"],
    project_sandbox: object = None,
    own: object = None,
) -> object:
    build = engine_factory(
        str(ROOT),
        project_sandbox=None
        if project_sandbox is None
        else parse_sandbox(project_sandbox, operator=False),
        protected=(STATE, RUN),
        line_git_ssh={} if access is None else {"alpha": access},
    )
    sandbox = None if own is None else parse_sandbox(own, operator=False)
    return build(
        LineSpec("alpha", model, Strategy.SUBSCRIPTION, LineConfig(sandbox=sandbox)),
        [],
    )


def network_of(engine: object) -> dict[str, object]:
    assert isinstance(engine, ClaudeCodeLine) and engine.sandbox is not None
    return cast(dict[str, object], engine.sandbox["network"])


def filesystem_of(engine: object) -> dict[str, object]:
    assert isinstance(engine, ClaudeCodeLine) and engine.sandbox is not None
    return cast(dict[str, object], engine.sandbox["filesystem"])


def test_an_enabled_sandbox_admits_the_socket_and_host_and_keeps_protection() -> None:
    access = access_at(Path("/keys"), "/run/agent.sock")
    project = {"enabled": True, "network": {"allowedDomains": ["pypi.org"]}}
    plain = built(None, project_sandbox=project)
    given = built(access, project_sandbox=project)
    assert network_of(given)["allowUnixSockets"] == [access.socket]
    assert network_of(given)["allowedDomains"] == sorted(["pypi.org", HOST])
    assert "allowUnixSockets" not in network_of(plain)
    assert network_of(given)["strictAllowlist"] is True
    plain_files, given_files = filesystem_of(plain), filesystem_of(given)
    assert given_files["denyRead"] == plain_files["denyRead"]
    assert set(cast(list[str], given_files["denyWrite"])) == set(
        cast(list[str], plain_files["denyWrite"])
    ) | {access.known_hosts, access.public_key}
    assert {str(STATE), str(RUN)} <= set(cast(list[str], given_files["denyRead"]))
    assert isinstance(given, ClaudeCodeLine) and given.git_ssh == access


def test_a_disabled_or_absent_sandbox_gets_no_layer() -> None:
    access = access_at(Path("/keys"), "/run/agent.sock")
    absent = built(access)
    assert isinstance(absent, ClaudeCodeLine) and absent.sandbox is None
    disabled = built(access, own={"enabled": False})
    assert isinstance(disabled, ClaudeCodeLine)
    assert disabled.sandbox == {"enabled": False}
    assert disabled.git_ssh == access


def test_a_socket_inside_a_protected_directory_is_refused_by_the_sandbox() -> None:
    access = access_at(Path("/keys"), str(RUN / "agent.sock"))
    with pytest.raises(SandboxError, match="Dromon protects"):
        built(access, project_sandbox={"enabled": True})


def test_a_sandbox_that_hides_the_files_from_ssh_is_refused() -> None:
    access = access_at(Path("/keys"), "/run/agent.sock")
    with pytest.raises(SandboxError, match="denies reading /keys/known_hosts"):
        built(
            access,
            project_sandbox={"enabled": True},
            own={"filesystem": {"denyRead": ["/keys"]}},
        )


def test_the_layer_alone_holds_only_the_three_grants() -> None:
    access = access_at(Path("/keys"), "/run/agent.sock")
    assert sandbox_layer(access).document() == {
        "network": {"allowUnixSockets": [access.socket], "allowedDomains": [HOST]},
        "filesystem": {"denyWrite": sorted([access.known_hosts, access.public_key])},
    }


@pytest.mark.parametrize("engine", sorted(MODELS))
def test_the_factory_gives_every_engine_the_access(engine: str) -> None:
    access = access_at(Path("/keys"), "/run/agent.sock")
    # The factory returns the LineEngine protocol, which does not name git_ssh.
    assert getattr(built(access, MODELS[engine]), "git_ssh") == access  # noqa: B009
    assert getattr(built(None, MODELS[engine]), "git_ssh") is None  # noqa: B009


def test_a_claude_line_relays_its_command_past_the_launchers_filter(
    tmp_path: Path,
) -> None:
    """The Agent SDK merges the daemon's environment beneath the engine's, and the
    launcher's filter keeps only allowlisted names, so the command travels under a
    Dromon name, set empty for a line without the option."""
    access = access_at(Path("/keys"), "/run/agent.sock")
    driver = Driver.for_model(MODELS["claude"], Strategy.SUBSCRIPTION)
    relayed = launcher.relayed(COMMAND_VARIABLE)
    daemon = {COMMAND_VARIABLE: "ssh -i stolen", relayed: "x"}
    given = ClaudeCodeLine(driver, tmp_path, environment=daemon, git_ssh=access)
    plain = ClaudeCodeLine(driver, tmp_path, environment=daemon)
    assert tuple(environment(access)) == launcher.RELAYED_VARIABLES
    assert given.options().env[relayed] == ssh_command(access)
    assert plain.options().env[relayed] == ""
    assert COMMAND_VARIABLE not in given.options().env


def test_the_relayed_name_in_the_daemons_environment_never_reaches_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of 2026-10-29, L3. **Fact, read from the pinned Agent SDK 0.2.174,**
    ``SubprocessCLITransport.connect`` builds the engine's environment as the
    daemon's ``os.environ``, then ``CLAUDE_CODE_ENTRYPOINT``, then
    ``options.env``, so ``options.env`` wins over the daemon's own values. The
    adapter sets the relayed name for every line, empty without the option, so a
    relayed name the daemon's environment holds never wins. The merge is copied here
    in the SDK's order, and the launcher's filter then sees what the engine would."""
    relayed = launcher.relayed(COMMAND_VARIABLE)
    monkeypatch.setenv(relayed, "ssh -i /operator/key")
    monkeypatch.setenv(COMMAND_VARIABLE, "ssh -i /operator/key")
    driver = Driver.for_model(MODELS["claude"], Strategy.SUBSCRIPTION)
    access = access_at(Path("/keys"), "/run/agent.sock")
    for git_ssh, expected in ((None, None), (access, ssh_command(access))):
        line = ClaudeCodeLine(driver, tmp_path, git_ssh=git_ssh)
        merged = {
            **os.environ,
            "CLAUDE_CODE_ENTRYPOINT": "sdk-py",
            **line.options().env,
        }
        launcher.unrelayed(merged)
        assert merged.get(COMMAND_VARIABLE) == expected


def test_the_filter_renames_the_relayed_command_and_drops_the_daemons() -> None:
    relayed = launcher.relayed(COMMAND_VARIABLE)
    given = {"PATH": "/bin", COMMAND_VARIABLE: "daemon", relayed: "line"}
    launcher.unrelayed(given)
    assert given == {"PATH": "/bin", COMMAND_VARIABLE: "line"}
    plain = {"PATH": "/bin", COMMAND_VARIABLE: "daemon", relayed: ""}
    launcher.unrelayed(plain)
    assert plain == {"PATH": "/bin"}


# The daemon and dromon config.


def project_with(
    agent: str, *, sandbox: str = "", line: str = "git_ssh = true\n"
) -> str:
    return (
        "version = 1\n"
        + table(agent)
        + sandbox
        + "[lines.alpha]\nmodel = 'claude-haiku-5-5'\nbuiltin_tools = ['Bash']\n"
        + line
        + "[lines.beta]\nmodel = 'claude-haiku-5-5'\n"
    )


def paths_for(root: Path, runtime: Path) -> ProjectPaths:
    paths = project_paths(root, runtime)
    paths.state_dir.mkdir(mode=0o700, exist_ok=True)
    return paths


def test_the_daemon_resolves_the_lines_it_gives(
    files: Path, agent: str, short_runtime: Path
) -> None:
    paths = paths_for(files, short_runtime)
    given = git_ssh_access(parse(project_with(agent)), paths, {})
    assert given == {"alpha": resolve(config(agent), files, {})}
    unused = parse(project_with(agent, line=""))
    assert git_ssh_access(unused, paths, {}) == {}
    assert git_ssh_access(ProjectConfig(), paths, {}) == {}
    broken = parse(project_with("/tmp/dromon-no-such.sock", line=""))  # noqa: S108
    with pytest.raises(GitSshError, match="does not exist"):
        git_ssh_access(broken, paths, {})


def test_the_daemon_refuses_a_line_sandbox_that_hides_the_files(
    files: Path, agent: str, short_runtime: Path
) -> None:
    paths = paths_for(files, short_runtime)
    hiding = (
        "[sandbox]\nenabled = true\n"
        f"[sandbox.filesystem]\ndenyRead = [{str(files / 'ssh')!r}]\n"
    )
    with pytest.raises(GitSshError, match="sandbox denies reading") as raised:
        git_ssh_access(parse(project_with(agent, sandbox=hiding)), paths, {})
    assert raised.value.key == "lines.alpha.git_ssh"


def test_dromond_exits_on_an_inherited_agent_without_a_socket(
    files: Path,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from dromon import daemon

    paths = paths_for(files, short_runtime)
    (paths.state_dir / "dromon.toml").write_text(project_with(INHERIT))
    monkeypatch.delenv("SSH_AUTH_SOCK", raising=False)
    argv = ["--project", str(files), "--runtime-dir", str(short_runtime)]
    assert daemon.main(argv) == exits.FAILURE
    assert "has no SSH_AUTH_SOCK" in capsys.readouterr().err


def test_dromon_config_shows_and_checks_the_option(
    files: Path,
    agent: str,
    short_runtime: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = paths_for(files, short_runtime)
    file = paths.state_dir / "dromon.toml"
    argv = ["--project", str(files), "--runtime-dir", str(short_runtime), "config"]
    file.write_text("version = 1\n[lines.alpha]\nmodel = 'claude-haiku-5-5'\n")
    assert cli.main(argv) == exits.OK
    assert "git ssh: off" in capsys.readouterr().out
    file.write_text(project_with(INHERIT))
    monkeypatch.setenv("SSH_AUTH_SOCK", agent)
    assert cli.main(argv) == exits.OK
    out = capsys.readouterr().out
    assert f"git ssh: agent {INHERIT} at {agent}" in out
    assert "for alpha\n" in out
    assert out.count(", git ssh") == 1
    monkeypatch.delenv("SSH_AUTH_SOCK")
    assert cli.main(argv) == exits.FAILURE
    assert "has no SSH_AUTH_SOCK" in capsys.readouterr().err
