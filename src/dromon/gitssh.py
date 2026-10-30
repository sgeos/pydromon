"""Git over the Secure Shell protocol for a line, an option only the operator enables.

**Required by the operator on 2026-10-29.** A line's engine starts with an
allowlisted environment and an isolated home, so the operator's ``SSH_AUTH_SOCK``,
``~/.ssh/config`` and ``known_hosts`` never reach its shell commands, and an enabled
sandbox denies the agent's socket. The project file's ``[git_ssh]`` table, with
``git_ssh = true`` on a line or a profile, gives that line's shell commands one
Secure Shell (SSH) agent socket and a ``GIT_SSH_COMMAND`` that pins the host keys.
``docs/protocol/PROJECT_CONFIG.md`` is normative.

What is forwarded. Only an agent socket, either the daemon's own ``SSH_AUTH_SOCK``
or a socket the operator names, such as that of an agent holding one deploy key. No
key file and never the operator's home directory. The public key the table names is
read by ``ssh`` only to choose which of the agent's keys to offer.

Threat model. A command that can reach the socket can ask the agent to sign with
every key it holds, for any host and repository those keys reach, and can run
``ssh`` with options of its own, so ``GIT_SSH_COMMAND`` narrows what ``git`` does and
not what a line can do. An agent that holds only a deploy key narrows the reach to
that repository. The option grants a capability and lifts no rule, so a project's
own rules that forbid a line to push still apply. A line whose sandbox is disabled
runs as the operator and could rewrite either file, so ``changed`` compares both with
the digests taken at load before each of its turns, which detects a change at a
turn's start and prevents none. The key checks prove a well-formed key is pinned,
never that it is GitHub's, so the operator compares its fingerprints with GitHub's
published ones.

Observed on 2026-10-29. Inside an enabled Claude Code sandbox ``ssh`` could not
resolve ``github.com``, and a tunnel through the sandbox's proxy was refused for ports
22 and 443, so the option is inferred to work only for a line whose sandbox is
disabled. ``docs/protocol/PROJECT_CONFIG.md`` records the test. No proxy route is
configured here. Whether Codex's and Grok's own sandboxes let a command connect to
the socket is not known.

Porting note. The checks use POSIX file modes and owners, and ``shlex`` quoting for
the POSIX shell that git runs ``GIT_SSH_COMMAND`` with.
"""

import base64
import binascii
import hashlib
import hmac
import os
import re
import shlex
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from dromon.sandbox import SandboxConfig, parse_sandbox

INHERIT: Final = "inherit"
"""The ``agent`` value that forwards the daemon's own ``SSH_AUTH_SOCK``."""
AGENT_VARIABLE: Final = "SSH_AUTH_SOCK"
COMMAND_VARIABLE: Final = "GIT_SSH_COMMAND"
HOST: Final = "github.com"
"""The host whose key the ``known_hosts`` file must pin, and the domain an enabled
sandbox allows."""
TABLE_KEYS: Final = frozenset({"agent", "known_hosts", "public_key"})
MAX_SOCKET_BYTES: Final = 103
"""The longest socket path macOS can connect to, one byte under its 104."""
MAX_KNOWN_HOSTS_BYTES: Final = 1024 * 1024
MAX_PUBLIC_KEY_BYTES: Final = 16 * 1024
UNSAFE: Final = re.compile(r"[\x00-\x1f\x7f\"'\\$%]")
"""Characters refused in every path. ``ssh`` expands ``%`` tokens and ``${NAME}``
in these options, and reads quotes and backslashes in them, so none can be written
safely, and a control character could end the option."""
KEY_TYPES: Final = frozenset(
    {
        "ssh-ed25519",
        "sk-ssh-ed25519@openssh.com",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ecdsa-sha2-nistp256@openssh.com",
        "ssh-rsa",
    }
)
"""The key types accepted in ``known_hosts`` and in the public key file. **Review
of 2026-10-29, M2 and L2.** Inference from the ``ssh_config`` manual page's default
``HostKeyAlgorithms``, without the certificate types, which pin a host only under a
``@cert-authority`` line, and without ``ssh-dss``, which OpenSSH no longer offers by
default. The signature names ``rsa-sha2-256`` and ``rsa-sha2-512`` use ``ssh-rsa``
keys."""
ECDSA_POINT_BYTES: Final = {"nistp256": 65, "nistp384": 97, "nistp521": 133}
"""The length of an uncompressed point on each curve, its ``0x04`` byte and both
coordinates."""
MAX_BLOB_FIELDS: Final = 8


class GitSshError(ValueError):
    """A ``[git_ssh]`` table that is malformed, or that names a socket or a file the
    daemon refuses. ``key`` is the dotted key the refusal is about."""

    def __init__(self, message: str, key: str = "git_ssh") -> None:
        super().__init__(message)
        self.key = key


@dataclass(frozen=True, slots=True)
class GitSshConfig:
    """The ``[git_ssh]`` table as written, checked without the file system."""

    agent: str
    """``INHERIT``, or the absolute path of an agent socket."""
    known_hosts: str
    """An absolute path, or one relative to the project root."""
    public_key: str
    """The public key ``ssh`` offers from the agent, as ``known_hosts`` is written."""


@dataclass(frozen=True, slots=True)
class GitSshAccess:
    """A ``GitSshConfig`` resolved and checked at load, as real absolute paths, with
    the SHA-256 digest of each file's contents as they were checked."""

    socket: str
    known_hosts: str
    public_key: str
    known_hosts_sha256: str
    public_key_sha256: str


def _path_text(value: object, key: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise GitSshError(f"{key} must be a path of 1 to 4096 characters", key)
    if UNSAFE.search(value) is not None:
        raise GitSshError(
            f"{key} must not hold a control character, a quote, a backslash, $ or %,"
            " which ssh would read as part of its option",
            key,
        )
    if value.startswith("~"):
        raise GitSshError(f"{key} must be absolute or relative to the project", key)
    return value


def parse_git_ssh(value: object) -> GitSshConfig:
    """Validate a ``[git_ssh]`` table, or raise ``GitSshError``."""
    if not isinstance(value, dict):
        raise GitSshError("git_ssh must be a table")
    table = cast(dict[str, object], value)
    unknown = sorted(set(table) - TABLE_KEYS)
    if unknown:
        raise GitSshError(
            f"git_ssh.{unknown[0]} is not a known key. Known keys are"
            f" {sorted(TABLE_KEYS)}",
            f"git_ssh.{unknown[0]}",
        )
    for name in sorted(TABLE_KEYS):
        if name not in table:
            raise GitSshError(f"git_ssh.{name} is required", f"git_ssh.{name}")
    agent = _path_text(table["agent"], "git_ssh.agent")
    if agent != INHERIT and not agent.startswith("/"):
        raise GitSshError(
            f"git_ssh.agent must be {INHERIT!r} or the absolute path of a socket",
            "git_ssh.agent",
        )
    return GitSshConfig(
        agent=agent,
        known_hosts=_path_text(table["known_hosts"], "git_ssh.known_hosts"),
        public_key=_path_text(table["public_key"], "git_ssh.public_key"),
    )


def _inside(path: str, directories: Sequence[Path]) -> Path | None:
    for directory in directories:
        guarded = os.path.realpath(directory)
        if os.path.commonpath([path, guarded]) == guarded:
            return Path(directory)
    return None


def _socket(config: GitSshConfig, environment: Mapping[str, str]) -> str:
    key = "git_ssh.agent"
    given = config.agent
    if given == INHERIT:
        inherited = environment.get(AGENT_VARIABLE, "")
        if not inherited:
            raise GitSshError(
                f"git_ssh.agent is {INHERIT!r}, but the daemon's environment has no"
                f" {AGENT_VARIABLE}. Start an agent first, or name its socket",
                key,
            )
        given = _path_text(inherited, f"{AGENT_VARIABLE} for git_ssh.agent")
        if not given.startswith("/"):
            raise GitSshError(f"{AGENT_VARIABLE} {given!r} is not absolute", key)
    real = os.path.realpath(given)
    _path_text(real, key)
    if len(real.encode("utf-8")) > MAX_SOCKET_BYTES:
        raise GitSshError(
            f"the agent socket {real!r} is longer than {MAX_SOCKET_BYTES} bytes", key
        )
    try:
        found = os.stat(real)
    except OSError as error:
        raise GitSshError(
            f"the agent socket {given!r} does not exist or cannot be read: {error}",
            key,
        ) from error
    if not stat.S_ISSOCK(found.st_mode):
        raise GitSshError(f"the agent socket {given!r} is not a socket", key)
    if found.st_uid != os.getuid():
        raise GitSshError(
            f"the agent socket {given!r} belongs to another user, not the operator",
            key,
        )
    _socket_directory(real, given, key)
    return real


def _socket_directory(real: str, given: str, key: str) -> None:
    """Refuse a socket whose directory another user could change. **Review of
    2026-10-29, L5.** Another user who may write the directory could remove the
    socket and bind one of their own in its place after the check, and the line
    would then ask that user's agent to sign. A directory of the operator's or of
    root, writable by neither its group nor others, rules that out. A sticky
    directory such as ``/tmp`` is refused too, though the sticky bit would keep
    others from removing the operator's socket, so that the rule stays one rule.
    ``ssh-agent`` and macOS's launchd both make a private directory for theirs."""
    directory = os.path.dirname(real)
    try:
        found = os.stat(directory)
    except OSError as error:
        raise GitSshError(
            f"the directory of the agent socket {given!r} cannot be read: {error}",
            key,
        ) from error
    if found.st_uid not in (os.getuid(), 0) or found.st_mode & (
        stat.S_IWGRP | stat.S_IWOTH
    ):
        raise GitSshError(
            f"the agent socket {given!r} is in {directory!r}, which another user"
            " may write. Name a socket in a directory that only the operator may"
            " write",
            key,
        )


def _operator_file(path: str, key: str, limit: int) -> tuple[str, bytes]:
    """The real path and contents of a regular file of the daemon's user, at most
    ``limit`` bytes, that neither its group nor others may write."""
    real = os.path.realpath(path)
    _path_text(real, key)
    try:
        found = os.stat(real)
    except OSError as error:
        raise GitSshError(f"{key} {path!r} does not exist: {error}", key) from error
    if not stat.S_ISREG(found.st_mode):
        raise GitSshError(f"{key} {path!r} is not a regular file", key)
    if found.st_uid != os.getuid() or found.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise GitSshError(
            f"{key} {path!r} must belong to the operator and be writable by no one"
            " else",
            key,
        )
    if found.st_size > limit:
        raise GitSshError(f"{key} {path!r} is larger than {limit} bytes", key)
    try:
        with open(real, "rb") as file:
            return real, file.read(limit + 1)[:limit]
    except OSError as error:
        raise GitSshError(f"{key} {path!r} cannot be read: {error}", key) from error


def _hashed_match(pattern: str, host: str) -> bool:
    """Whether a hashed ``known_hosts`` name, ``|1|SALT|HASH``, is ``host``."""
    parts = pattern.split("|")
    if len(parts) != 4 or parts[0] or parts[1] != "1":
        return False
    try:
        salt = base64.b64decode(parts[2], validate=True)
        expected = base64.b64decode(parts[3], validate=True)
    except binascii.Error:
        return False
    digest = hmac.new(salt, host.encode("ascii"), hashlib.sha1).digest()
    return hmac.compare_digest(digest, expected)


def _blob_fields(blob: bytes) -> list[bytes] | None:
    """The length-prefixed strings a key blob is made of, or ``None`` when it is not
    a whole number of them, or holds more than ``MAX_BLOB_FIELDS``."""
    fields: list[bytes] = []
    at = 0
    for _ in range(MAX_BLOB_FIELDS):
        if at == len(blob):
            return fields
        if at + 4 > len(blob):
            return None
        size = int.from_bytes(blob[at : at + 4], "big")
        at += 4
        if size > len(blob) - at:
            return None
        fields.append(blob[at : at + size])
        at += size
    return fields if at == len(blob) else None


def well_formed(key_type: str, encoded: str) -> bool:
    """Whether ``encoded`` is the base64 of a public key blob of ``key_type``, one of
    ``KEY_TYPES``, whose own type string is ``key_type`` and whose key data has the
    shape that type gives it. **Review of 2026-10-29, M2.** This proves a
    well-formed key, never that it is GitHub's."""
    if key_type not in KEY_TYPES:
        return False
    try:
        blob = base64.b64decode(encoded, validate=True)
    except binascii.Error:
        return False
    fields = _blob_fields(blob)
    if not fields or fields[0] != key_type.encode("ascii"):
        return False
    data = fields[1:]
    match key_type:
        case "ssh-ed25519":
            return len(data) == 1 and len(data[0]) == 32
        case "sk-ssh-ed25519@openssh.com":
            return len(data) == 2 and len(data[0]) == 32 and len(data[1]) > 0
        case "ssh-rsa":
            return len(data) == 2 and all(len(part) > 0 for part in data)
        case _:
            curve = key_type.removeprefix("sk-").split("@")[0].rsplit("-", 1)[1]
            count = 3 if key_type.startswith("sk-") else 2
            return (
                len(data) == count
                and data[0] == curve.encode("ascii")
                and len(data[1]) == ECDSA_POINT_BYTES[curve]
                and data[1][0] == 4
                and (count == 2 or len(data[2]) > 0)
            )


def pins(text: str, host: str = HOST) -> bool:
    """Whether ``known_hosts`` text holds a plain entry with a well-formed key for
    ``host``. A marked line, such as ``@revoked`` or ``@cert-authority``, and a
    wildcard pattern pin nothing, so they do not count. **Review of 2026-10-29,
    L1.** Nor does a line whose host list holds any negated pattern, which agrees
    with ``ssh-keygen -F`` as the review observed it for ``!github.com,github.com``,
    and errs toward refusing."""
    for line in text.splitlines()[:65536]:
        fields = line.split()
        if len(fields) < 3 or fields[0].startswith(("#", "@")):
            continue
        if not well_formed(fields[1], fields[2]):
            continue
        patterns = fields[0].split(",")
        if any(pattern.startswith("!") for pattern in patterns):
            continue
        for pattern in patterns:
            if pattern.lower() == host or _hashed_match(pattern, host):
                return True
    return False


def _public_key(contents: bytes, key: str) -> None:
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GitSshError(f"{key} is not UTF-8 text", key) from error
    if "PRIVATE KEY" in text:
        raise GitSshError(
            f"{key} holds a private key. Name the public key, whose private half"
            " the agent holds",
            key,
        )
    lines = [line for line in text.splitlines() if line.strip()]
    fields = lines[0].split() if len(lines) == 1 else []
    if len(fields) < 2 or not well_formed(fields[0], fields[1]):
        raise GitSshError(
            f"{key} must hold exactly one well-formed public key line, of a type in"
            f" {sorted(KEY_TYPES)}",
            key,
        )


def resolve(
    config: GitSshConfig,
    root: Path,
    environment: Mapping[str, str],
    protected: Sequence[Path] = (),
) -> GitSshAccess:
    """``config`` checked against the file system and the daemon's ``environment``,
    with relative files resolved against the project ``root``. Raises
    ``GitSshError`` for an inherited agent with no ``SSH_AUTH_SOCK``, a socket that
    does not exist, is not a socket or is not the operator's, a ``known_hosts``
    that is missing or pins no key for ``github.com``, a public key file that holds
    anything but one public key, and any of them inside a ``protected`` directory,
    which every enabled sandbox denies."""
    socket = _socket(config, environment)
    known_hosts, hosts = _operator_file(
        str(root / config.known_hosts), "git_ssh.known_hosts", MAX_KNOWN_HOSTS_BYTES
    )
    try:
        hosts_text = hosts.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GitSshError(
            "git_ssh.known_hosts is not UTF-8 text", "git_ssh.known_hosts"
        ) from error
    if not pins(hosts_text):
        raise GitSshError(
            f"git_ssh.known_hosts {config.known_hosts!r} holds no key for {HOST}."
            " Add GitHub's published host keys to it first",
            "git_ssh.known_hosts",
        )
    public_key, contents = _operator_file(
        str(root / config.public_key), "git_ssh.public_key", MAX_PUBLIC_KEY_BYTES
    )
    _public_key(contents, "git_ssh.public_key")
    for key, path in (
        ("git_ssh.agent", socket),
        ("git_ssh.known_hosts", known_hosts),
        ("git_ssh.public_key", public_key),
    ):
        directory = _inside(path, protected)
        if directory is not None:
            raise GitSshError(
                f"{key} {path!r} is inside {directory}, which Dromon protects", key
            )
    return GitSshAccess(
        socket=socket,
        known_hosts=known_hosts,
        public_key=public_key,
        known_hosts_sha256=hashlib.sha256(hosts).hexdigest(),
        public_key_sha256=hashlib.sha256(contents).hexdigest(),
    )


def changed(access: GitSshAccess) -> str | None:
    """The first of the two files whose contents differ from those checked at load,
    or that is now missing, unreadable, not the operator's or writable by another,
    or ``None`` when both are as they were. **Review of 2026-10-29, M1.** A line
    whose sandbox is disabled runs as the operator and could rewrite either file,
    pinning ``github.com`` to another key for every line given the option. The
    scheduler calls this before each turn of such a line. It detects a change at a
    turn's start and prevents none, since a line can change a file and restore it
    between two checks."""
    for key, path, digest, limit in (
        (
            "git_ssh.known_hosts",
            access.known_hosts,
            access.known_hosts_sha256,
            MAX_KNOWN_HOSTS_BYTES,
        ),
        (
            "git_ssh.public_key",
            access.public_key,
            access.public_key_sha256,
            MAX_PUBLIC_KEY_BYTES,
        ),
    ):
        try:
            _, contents = _operator_file(path, key, limit)
        except GitSshError:
            return path
        if not hmac.compare_digest(hashlib.sha256(contents).hexdigest(), digest):
            return path
    return None


def _option(name: str, path: str) -> str:
    """One ``-o`` value, with the path double quoted for ssh's own parser, so that a
    space in it is safe. ``UNSAFE`` keeps every character ssh would read there out."""
    return f'{name}="{path}"'


def ssh_command(access: GitSshAccess) -> str:
    """The ``GIT_SSH_COMMAND`` for ``access``, quoted for the shell git runs it with.

    ``-F /dev/null`` ignores every configuration file, and ``IdentitiesOnly`` with
    the public key offers only that key of the agent's. **Inference from the
    ssh_config manual page, not tested here.** Without an ``IdentityFile``,
    ``IdentitiesOnly`` would offer no key of the agent's at all, since the default
    files are absent from a line's home. ``StrictHostKeyChecking`` refuses a host the
    named ``known_hosts`` does not pin, ``GlobalKnownHostsFile`` keeps the system's
    file out, ``UpdateHostKeys`` keeps ssh from writing it, and ``BatchMode`` refuses
    any prompt, which no one would answer."""
    arguments = [
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        _option("IdentityAgent", access.socket),
        "-o",
        _option("IdentityFile", access.public_key),
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        _option("UserKnownHostsFile", access.known_hosts),
        "-o",
        "GlobalKnownHostsFile=/dev/null",
        "-o",
        "UpdateHostKeys=no",
        "-o",
        "BatchMode=yes",
    ]
    return " ".join(shlex.quote(argument) for argument in arguments)


def environment(access: GitSshAccess | None) -> dict[str, str]:
    """The variables every adapter adds for a line's shell commands, the one source
    of them, and none for a line without the option."""
    if access is None:
        return {}
    return {COMMAND_VARIABLE: ssh_command(access)}


def sandbox_layer(access: GitSshAccess) -> SandboxConfig:
    """The layer merged into an enabled sandbox of a line with the option, over every
    other layer. It allows the socket and ``github.com``, and denies writing the two
    files, so a command cannot add a host key. It lifts nothing, since
    ``dromon.sandbox.effective`` adds the protected directories after it and refuses
    a socket inside one."""
    return parse_sandbox(
        {
            "network": {
                "allowUnixSockets": [access.socket],
                "allowedDomains": [HOST],
            },
            "filesystem": {"denyWrite": [access.known_hosts, access.public_key]},
        },
        operator=True,
    )


def unreadable(access: GitSshAccess, document: Mapping[str, object]) -> str | None:
    """The first of the two files an effective sandbox ``document`` denies reading,
    which ssh inside it could then not read, or ``None``."""
    filesystem = document.get("filesystem")
    if not isinstance(filesystem, dict):
        return None
    denied = cast(dict[str, object], filesystem).get("denyRead", [])
    if not isinstance(denied, list):
        return None
    directories = [
        Path(os.path.expanduser(entry))
        for entry in cast(list[object], denied)
        if isinstance(entry, str)
    ]
    for path in (access.known_hosts, access.public_key):
        if _inside(path, [d for d in directories if d.is_absolute()]) is not None:
            return path
    return None
