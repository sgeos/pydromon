"""Open federation, ``[federation] local`` and ``remote``, round 12.

``docs/architecture/FEDERATION.md`` is normative, under open federation. The tests
stand in for the process table with a fixed start time, since a sandbox denies
``ps``, and reach a second daemon in process, through its peer session, except for
the one test marked ``socket``, which a sandbox deselects.
"""

import asyncio
import json
import os
import socket
import stat
import threading
import time
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import client, peercred, registry
from dromon.configview import Overrides
from dromon.daemon import (
    Daemon,
    PeerSession,
    admitted_network,
    open_federation,
    open_registration,
    protected_dirs,
    registry_protected,
)
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.federation import (
    DEFAULT_REMOTE_MAX_BODY_BYTES,
    DEFAULT_REMOTE_TOTAL_DELIVERIES,
    DEFAULT_REMOTE_TOTAL_TURNS_PER_HOUR,
    DEFAULT_REMOTE_TOTAL_WAITING,
    DEFAULT_REMOTE_TURNS_PER_HOUR,
    DEFAULT_REMOTE_WAITING,
    DEFAULT_TURNS_PER_HOUR,
    DEFAULT_WAITING,
    MAX_OPEN_REMOTE_PEERS,
    MAX_PEERS,
    MAX_REMOTE_TOTAL_DELIVERIES,
    NO_FEDERATION,
    SCOPE_LOCAL,
    SCOPE_REMOTE,
    FederationConfig,
    FederationError,
    PeerDefinition,
    parse_federation,
    plan_peers,
    policy_text,
    unconfined_refusal,
)
from dromon.forwarder import Link, check_outbound
from dromon.lineconfig import ConfigError
from dromon.model import (
    EventRejected,
    LineAddress,
    OperatorAddress,
    PeerDeclared,
    PeerKeyed,
    PeerOpened,
    PeerRemoved,
    RemoteAddress,
    State,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.peering import (
    RATE_PER_PEER,
    RATE_WINDOW_SECONDS,
    Accepted,
    Busy,
    Outcome,
    Received,
    Receiver,
)
from dromon.projectconfig import ProjectConfig
from dromon.protocol import (
    Operation,
    Request,
    Response,
    decode_response,
    encode_request,
    encode_response,
)
from dromon.scheduler import LineEngine, LineSpec, Scheduler

AT = "2026-10-31T00:00:00Z"
STARTED = "Sat Oct 31 00:00:00 2026"
PIN = "a" * 64
OTHER_PIN = "b" * 64
CORE = LineAddress("core")
ALPHA = LineAddress("alpha")


LOOPBACK: list[object] = ["127.0.0.0/8", "::1/128"]
"""The ranges a remote open listener of these tests accepts, which the file must list,
finding F3 of the round 12 review."""


def fixed_start(*pids: int) -> registry.StartOf:
    """A process table in which ``pids`` started at ``STARTED`` and no other runs."""
    live = set(pids)
    return lambda pid: STARTED if pid in live else None


def unreadable(_pid: int) -> str | None:
    raise RuntimeError("the process table cannot be read")


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    found = tmp_path / "registry"
    found.mkdir(mode=0o700)
    found.chmod(0o700)
    return found


RUNTIME: list[Path] = []
"""The short runtime directory of the running test, since a socket path is bounded."""


@pytest.fixture(autouse=True)
def runtime_base(short_runtime: Path) -> Generator[None]:
    RUNTIME.append(short_runtime)
    yield
    RUNTIME.pop()


def entry(name: str, root: Path, pid: int | None = None) -> registry.Entry:
    return registry.Entry(
        name,
        RUNTIME[-1] / name / "peers.sock",
        root,
        os.getpid() if pid is None else pid,
        STARTED,
    )


def open_config(**keys: object) -> FederationConfig:
    table: dict[str, object] = {"local": "open", "listen": True, "name": "harbor"}
    table.update(keys)
    return parse_federation(table, None)


# The registry.


def test_an_entry_is_written_owner_only_and_reads_back(
    directory: Path, tmp_path: Path
) -> None:
    mine = entry("harbor", tmp_path / "harbor")
    registry.register(directory, mine, fixed_start(os.getpid()))
    path = registry.entry_path(directory, "harbor")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert registry.read_entry(directory, "harbor") == mine
    assert registry.entries(directory) == [mine]
    assert json.loads(path.read_text())["version"] == registry.REGISTRY_VERSION


def test_a_live_name_is_not_taken_and_a_stale_one_is_replaced(
    directory: Path, tmp_path: Path
) -> None:
    first = entry("harbor", tmp_path / "one", pid=4001)
    registry.register(directory, first, fixed_start(4001))
    second = entry("harbor", tmp_path / "two", pid=4002)
    with pytest.raises(registry.RegistryError, match="held by the live daemon"):
        registry.register(directory, second, fixed_start(4001, 4002))
    registry.register(directory, second, fixed_start(4002))
    assert registry.read_entry(directory, "harbor") == second


def test_a_holder_the_table_cannot_judge_keeps_its_name(
    directory: Path, tmp_path: Path
) -> None:
    registry.register(directory, entry("harbor", tmp_path, pid=4001), fixed_start(4001))
    with pytest.raises(registry.RegistryError, match="cannot tell whether"):
        registry.register(directory, entry("harbor", tmp_path, pid=4002), unreadable)


def test_a_daemon_removes_only_its_own_entry(directory: Path, tmp_path: Path) -> None:
    mine = entry("harbor", tmp_path, pid=4001)
    registry.register(directory, mine, fixed_start(4001))
    successor = entry("harbor", tmp_path, pid=4002)
    assert not registry.unregister(directory, successor)
    assert registry.unregister(directory, mine)
    assert registry.read_entry(directory, "harbor") is None


def test_a_registry_wider_than_its_owner_is_refused(
    directory: Path, tmp_path: Path
) -> None:
    directory.chmod(0o750)
    with pytest.raises(registry.RegistryError, match="mode 750"):
        registry.register(directory, entry("harbor", tmp_path), fixed_start())
    with pytest.raises(registry.RegistryError, match="mode 750"):
        registry.entries(directory)
    assert (
        registry.find(directory, "harbor", os.getpid(), STARTED, fixed_start()) is None
    )


def test_a_registry_that_is_a_link_is_refused(directory: Path, tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(directory)
    with pytest.raises(registry.RegistryError, match="not a directory"):
        registry.check_directory(link)


def test_an_entry_wider_than_its_owner_or_misnamed_is_ignored(
    directory: Path, tmp_path: Path
) -> None:
    mine = entry("harbor", tmp_path)
    registry.register(directory, mine, fixed_start(os.getpid()))
    path = registry.entry_path(directory, "harbor")
    path.chmod(0o644)
    assert registry.read_entry(directory, "harbor") is None
    path.chmod(0o600)
    os.rename(path, registry.entry_path(directory, "other"))
    assert registry.read_entry(directory, "other") is None


def test_an_entry_names_a_peers_sock_by_an_absolute_path(tmp_path: Path) -> None:
    with pytest.raises(registry.RegistryError, match="peers.sock"):
        registry.Entry("harbor", tmp_path / "other.sock", tmp_path, 1, STARTED)
    with pytest.raises(registry.RegistryError, match="absolute"):
        registry.Entry("harbor", Path("run/peers.sock"), tmp_path, 1, STARTED)
    with pytest.raises(registry.RegistryError, match="federation name"):
        registry.Entry("Not A Name", tmp_path / "peers.sock", tmp_path, 1, STARTED)


def test_find_needs_the_callers_pid_and_a_live_start_time(
    directory: Path, tmp_path: Path
) -> None:
    """A registry entry whose pid does not match the connecting process, and a stale
    one whose pid now has another start time, both find nothing."""
    mine = entry("harbor", tmp_path, pid=4001)
    registry.register(directory, mine, fixed_start(4001))
    assert registry.find(directory, "harbor", 4001, STARTED, fixed_start(4001)) == mine
    assert (
        registry.find(directory, "harbor", 4002, STARTED, fixed_start(4001, 4002))
        is None
    )

    def reused(_pid: int) -> str | None:
        return "Sun Nov  1 00:00:00 2026"

    assert registry.find(directory, "harbor", 4001, STARTED, reused) is None
    assert registry.find(directory, "harbor", 4001, STARTED, unreadable) is None
    assert registry.find(directory, "harbor", 4001, EARLIER, fixed_start(4001)) is None
    assert registry.live_entry(directory, "harbor", fixed_start()) is None


def test_the_default_registry_is_under_the_users_home_not_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "/nowhere")
    found = registry.default_directory()
    assert found.parts[-4:] == (".local", "state", "dromon", "registry")
    assert not str(found).startswith("/nowhere")
    assert registry.configured_directory({}) == found


def test_every_test_runs_against_a_registry_of_its_own(tmp_path: Path) -> None:
    """The guard of ``conftest``, so that no test reaches the user's real registry."""
    given = registry.configured_directory(os.environ)
    assert given != registry.default_directory()
    assert given.is_relative_to(tmp_path.parent)


@pytest.mark.parametrize("value", ["", "relative/registry", "/a/../registry", "/a/./b"])
def test_a_malformed_registry_override_is_refused_not_ignored(value: str) -> None:
    with pytest.raises(registry.RegistryError, match="DROMON_REGISTRY_DIR"):
        registry.configured_directory({registry.REGISTRY_DIR_ENV: value})


def test_an_override_is_used_and_the_default_is_denied_as_well(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    moved = tmp_path / "elsewhere"
    assert registry.configured_directory({registry.REGISTRY_DIR_ENV: str(moved)}) == (
        moved
    )
    (tmp_path / "harbor").mkdir()
    paths = project_paths(tmp_path / "harbor", RUNTIME[-1] / "harbor")
    denied = protected_dirs(paths, open_config(), moved)
    assert Path(os.path.realpath(moved)) in denied
    assert Path(os.path.realpath(registry.default_directory())) in denied
    monkeypatch.setenv(registry.REGISTRY_DIR_ENV, "relative")
    with pytest.raises(ConfigError, match="DROMON_REGISTRY_DIR"):
        protected_dirs(paths, open_config())


# The peer's pid.


def test_the_peer_pid_of_a_socket_pair_is_this_process() -> None:
    left, right = socket.socketpair()
    with left, right:
        assert peercred.peer_pid(left) == os.getpid()


def test_a_peer_pid_that_cannot_be_read_is_refused() -> None:
    class Broken:
        def getsockopt(self, level: int, optname: int, buflen: int, /) -> bytes:
            raise OSError(9, "Bad file descriptor")

    with pytest.raises(peercred.PeerCredentialError, match="Bad file descriptor"):
        peercred.peer_pid(Broken())


# The project file.


def test_allowlist_is_the_default_of_both_modes_and_changes_nothing() -> None:
    parsed = parse_federation(None, None)
    assert parsed == NO_FEDERATION
    assert (parsed.local, parsed.remote, parsed.any_open) == (
        "allowlist",
        "allowlist",
        False,
    )
    declared = parse_federation(
        {"listen": True},
        {"keleusma": {"socket": "/srv/k/run/peers.sock", "inbound": True}},
    )
    peer = declared.peers["keleusma"]
    assert peer.open_scope == ""
    assert "open" not in policy_text(peer)


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"local": "open", "listen": True}, "federation.name must give"),
        ({"local": "open", "name": "harbor"}, "listen must be true"),
        ({"local": "wide"}, "must be allowlist or open"),
        ({"local": "open", "listen": True, "name": PIN}, "64 hexadecimal"),
        ({"remote": "open"}, "tls_listen must name"),
        (
            {"tls_listen": "127.0.0.1:7411", "remote": "open"},
            "remote is open, so federation.allowed_networks must list",
        ),
        (
            {"tls_listen": "127.0.0.1:7411", "remote": "open", "allowed_networks": []},
            "1 to 64 address ranges",
        ),
        ({"allowed_networks": ["10.0.0.0/8"]}, "tls_listen must be set"),
        (
            {
                "tls_listen": "127.0.0.1:7411",
                "remote": "open",
                "allowed_networks": ["10.0.0.1/8"],
            },
            "not an address range",
        ),
        ({"open": {"remote": ["*:*"]}}, "party is not an address"),
        ({"open": {"remote": ["core"]}}, "not NAME:PARTY"),
        ({"open": {"wide": True}}, "not a known key"),
    ],
)
def test_a_malformed_open_table_is_refused(
    table: dict[str, object], message: str
) -> None:
    with pytest.raises(FederationError, match=message):
        parse_federation(table, None)


def test_the_name_may_not_be_a_declared_peers() -> None:
    with pytest.raises(FederationError, match="declared peer's name"):
        parse_federation(
            {"local": "open", "listen": True, "name": "keleusma"},
            {"keleusma": {"socket": "/srv/k/run/peers.sock"}},
        )


def test_the_open_table_gives_each_open_peer_its_policy() -> None:
    parsed = open_config(
        open={
            "reach": ["core"],
            "senders": ["alpha"],
            "remote": ["tidepool:kaizen", "*:@ana", "reef:core"],
            "turns_per_hour": 3,
            "remote_reach": ["@ana"],
        }
    )
    local = parsed.open.local_definition("tidepool")
    assert (local.open_scope, local.inbound, local.outbound) == (
        SCOPE_LOCAL,
        True,
        True,
    )
    assert [str(party) for party in local.remote] == ["@ana", "kaizen"]
    assert local.turns_per_hour == 3
    remote = parsed.open.remote_definition(PIN)
    assert (remote.open_scope, remote.outbound, remote.pin_sha256) == (
        SCOPE_REMOTE,
        False,
        PIN,
    )
    assert remote.reach == (OperatorAddress("ana"),)
    assert (remote.max_body_bytes, remote.turns_per_hour, remote.waiting) == (
        DEFAULT_REMOTE_MAX_BODY_BYTES,
        DEFAULT_REMOTE_TURNS_PER_HOUR,
        DEFAULT_REMOTE_WAITING,
    )


def test_a_declared_table_governs_its_peer_and_an_unopened_name_has_none() -> None:
    parsed = parse_federation(
        {"local": "open", "listen": True, "name": "harbor"},
        {"tidepool": {"socket": "/srv/t/run/peers.sock", "reach": ["alpha"]}},
    )
    state = State()
    state.apply(PeerOpened("reef", SCOPE_LOCAL, AT))
    for event in plan_peers(state, parsed):
        state.apply(event)
    declared = parsed.definition(state, "tidepool")
    assert declared is not None and declared.open_scope == ""
    opened = parsed.definition(state, "reef")
    assert opened is not None and opened.open_scope == SCOPE_LOCAL
    assert parsed.definition(state, "lagoon") is None
    closed = parse_federation(None, None)
    assert closed.definition(state, "reef") is None


def test_open_federation_takes_the_unconfined_engine_rule() -> None:
    assert unconfined_refusal(NO_FEDERATION, "alpha", "pi", False) is None
    refusal = unconfined_refusal(open_config(), "alpha", "pi", False)
    assert refusal is not None and "open federation" in refusal
    accepted = open_config(accept_unconfined=True)
    assert unconfined_refusal(accepted, "alpha", "pi", False) is None


def test_allowed_networks_admit_only_their_ranges() -> None:
    parsed = parse_federation(
        {
            "tls_listen": "127.0.0.1:7411",
            "remote": "open",
            "allowed_networks": ["192.0.2.0/24", "2001:db8::/32"],
        },
        None,
    )
    assert parsed.admits("192.0.2.7")
    assert parsed.admits("::ffff:192.0.2.7")
    assert parsed.admits("2001:db8::1")
    assert not parsed.admits("198.51.100.1")
    assert not parsed.admits("not an address")
    assert NO_FEDERATION.admits("198.51.100.1")


def test_a_world_open_listener_is_written_down() -> None:
    """Decided by the operator on 2026-10-31, finding F3 of the round 12 review. With
    ``remote`` open, leaving ``allowed_networks`` out is refused, so a listener that
    admits every address is one whose file lists both whole ranges. Only then does
    it admit them all."""
    table: dict[str, object] = {"tls_listen": "[::]:7411", "remote": "open"}
    with pytest.raises(FederationError, match='"0.0.0.0/0" and "::/0"'):
        parse_federation(table, None)
    world = parse_federation({**table, "allowed_networks": ["0.0.0.0/0", "::/0"]}, None)
    for host in ("198.51.100.1", "::ffff:203.0.113.5", "2001:db8::1", "::1"):
        assert world.admits(host)
    only_v4 = parse_federation({**table, "allowed_networks": ["0.0.0.0/0"]}, None)
    assert only_v4.admits("198.51.100.1")
    assert not only_v4.admits("2001:db8::1")


@dataclass
class FakeWriter:
    peer: object
    closed: bool = False

    def get_extra_info(self, name: str) -> object:
        return self.peer if name == "peername" else None

    def close(self) -> None:
        self.closed = True


def test_a_tls_connection_from_outside_allowed_networks_is_refused() -> None:
    parsed = parse_federation(
        {
            "tls_listen": "127.0.0.1:7411",
            "remote": "open",
            "allowed_networks": ["192.0.2.0/24"],
        },
        None,
    )
    inside = cast(asyncio.StreamWriter, FakeWriter(("192.0.2.9", 50000)))
    outside = cast(asyncio.StreamWriter, FakeWriter(("198.51.100.1", 50000)))
    unknown = cast(asyncio.StreamWriter, FakeWriter(None))
    assert admitted_network(parsed, inside)
    assert not admitted_network(parsed, outside)
    assert not admitted_network(parsed, unknown)
    assert admitted_network(NO_FEDERATION, outside)


# The model.


def test_an_open_peer_is_recorded_once_and_a_declared_table_replaces_it() -> None:
    state = State()
    state.apply(PeerOpened("reef", SCOPE_LOCAL, AT))
    assert state.peers["reef"].scope == SCOPE_LOCAL
    with pytest.raises(EventRejected, match="open already"):
        state.check(PeerOpened("reef", SCOPE_LOCAL, AT))
    with pytest.raises(EventRejected, match="no token"):
        state.check(PeerKeyed("reef", "c" * 64))
    state.apply(PeerDeclared("reef", "unix", "{}"))
    assert state.peers["reef"].scope == ""
    with pytest.raises(EventRejected, match="declared"):
        state.check(PeerOpened("reef", SCOPE_LOCAL, AT))
    state.apply(PeerRemoved("reef"))
    state.apply(PeerOpened("reef", SCOPE_LOCAL, AT))
    assert state.peers["reef"].scope == SCOPE_LOCAL


def test_the_project_file_never_removes_an_open_peer() -> None:
    state = State()
    state.apply(PeerOpened("reef", SCOPE_LOCAL, AT))
    assert plan_peers(state, open_config()) == ()
    assert plan_peers(state, NO_FEDERATION) == ()


@pytest.mark.parametrize(
    ("peer_id", "scope", "at"),
    [("Reef", SCOPE_LOCAL, AT), ("reef", "nearby", AT), ("reef", SCOPE_LOCAL, "now")],
)
def test_a_malformed_peer_opened_is_invalid(peer_id: str, scope: str, at: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011, InvalidField is a ValueError
        PeerOpened(peer_id, scope, at)


# Two daemons in process.


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


@contextmanager
def daemon_for(
    root: Path,
    federation: FederationConfig,
    directory: Path,
    start_of: registry.StartOf,
    lines: Sequence[str] = ("core",),
) -> Generator[Daemon]:
    root.mkdir(parents=True, exist_ok=True)
    paths = project_paths(root, RUNTIME[-1] / root.name)
    paths.state_dir.mkdir(mode=0o700, exist_ok=True)
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        for line_id in lines:
            scheduler.create_line(line_id, "claude-haiku-5-5", Strategy.SUBSCRIPTION)
        scheduler.ensure_operator("ana")
        for event in plan_peers(log.state, federation):
            log.append(event)
        scheduler.federation = federation
        open_federation(scheduler, log, federation, directory)
        daemon = Daemon(
            paths,
            log,
            AT,
            scheduler,
            # The file declares ``ana``, whom a peer may reach, decision 25.
            project=ProjectConfig(operator="ana", federation=federation),
            command_line=Overrides(),
            project_file_present=False,
            registry_dir=directory,
            start_of=start_of,
        )
        yield daemon


def register_daemon(daemon: Daemon, pid: int | None = None) -> registry.Entry:
    federation = daemon.project.federation
    assert federation.name is not None
    found = registry.Entry(
        federation.name,
        daemon.paths.peer_socket,
        daemon.paths.root.resolve(),
        os.getpid() if pid is None else pid,
        STARTED,
    )
    registry.register(daemon.registry_dir, found, fixed_start(found.pid))
    return found


def frame(op: str, args: dict[str, object]) -> bytes:
    return (json.dumps({"id": "r", "op": op, "v": 1, "args": args}) + "\n").encode()


def open_hello(name: str) -> bytes:
    return frame("hello", {"versions": [1], "name": name})


def note(recipient: str = "core", **keys: object) -> dict[str, object]:
    args: dict[str, object] = {
        "origin_id": "o1",
        "sender": "alpha",
        "recipient": recipient,
        "kind": "note",
        "body": "hello across projects",
        "reply_to": None,
        "priority": False,
    }
    args.update(keys)
    return args


def ask(session: PeerSession, line: bytes) -> Response:
    return asyncio.run(session.respond(line))


RECEIVER_OPEN = {"reach": ["core"], "senders": ["core"], "remote": ["*:alpha"]}
SENDER_OPEN = {"senders": ["alpha"], "remote": ["tidepool:core"]}


def test_an_open_daemon_names_a_registered_caller_by_its_entry(
    directory: Path, tmp_path: Path
) -> None:
    receiver = open_config(name="tidepool", open=RECEIVER_OPEN)
    with daemon_for(
        tmp_path / "tidepool", receiver, directory, fixed_start(os.getpid())
    ) as daemon:
        caller = entry("harbor", tmp_path / "harbor")
        registry.register(directory, caller, fixed_start(os.getpid()))
        session = PeerSession(daemon, peer_pid=os.getpid(), peer_started=STARTED)
        greeted = ask(session, open_hello("harbor"))
        assert greeted.ok, greeted.error_message
        assert session.registered and session.peer_id == "harbor"
        assert daemon.log.state.peers["harbor"].scope == SCOPE_LOCAL
        delivered = ask(session, frame("deliver", note()))
        assert delivered.result is not None
        assert delivered.result["outcome"] == "accepted"
        message = daemon.log.state.messages[cast(str, delivered.result["message_id"])]
        assert message.sender == RemoteAddress("harbor", ALPHA)


def test_an_open_peer_sending_outside_its_reach_is_refused(
    directory: Path, tmp_path: Path
) -> None:
    receiver = open_config(name="tidepool", open=RECEIVER_OPEN)
    with daemon_for(
        tmp_path / "tidepool",
        receiver,
        directory,
        fixed_start(os.getpid()),
        lines=("core", "review"),
    ) as daemon:
        registry.register(
            directory, entry("harbor", tmp_path / "harbor"), fixed_start(os.getpid())
        )
        session = PeerSession(daemon, peer_pid=os.getpid(), peer_started=STARTED)
        assert ask(session, open_hello("harbor")).ok
        refused = ask(session, frame("deliver", note("review")))
        assert refused.result is not None
        assert (refused.result["outcome"], refused.result["code"]) == (
            "refused",
            "refused",
        )
        assert not [
            m for m in daemon.log.state.messages.values() if m.recipient == "review"
        ]


@pytest.mark.parametrize(
    "case", ["no_entry", "other_pid", "stale", "wrong_name", "own", "token_closed"]
)
def test_each_refusal_of_a_hello_without_a_token(
    case: str, directory: Path, tmp_path: Path
) -> None:
    """A registry entry whose pid is not the caller's, a stale entry, an entry of
    another name, this daemon's own entry, and a hello without a token to a daemon
    that is not open are each refused, and record no open peer."""
    receiver = open_config(name="tidepool", open=RECEIVER_OPEN)
    if case == "token_closed":
        receiver = parse_federation({"listen": True}, None)
    pid = os.getpid()
    start_of = fixed_start() if case == "stale" else fixed_start(pid, pid + 1)
    with daemon_for(tmp_path / "tidepool", receiver, directory, start_of) as daemon:
        name = "harbor"
        if case == "own":
            name = "tidepool"
            register_daemon(daemon)
        elif case != "no_entry":
            caller = entry("harbor", tmp_path / "harbor", pid=pid)
            registry.register(directory, caller, fixed_start(pid))
        session = PeerSession(
            daemon,
            peer_pid=pid + 1 if case == "other_pid" else pid,
            peer_started=STARTED,
        )
        greeted = ask(session, open_hello("reef" if case == "wrong_name" else name))
        assert not greeted.ok
        assert session.peer_id is None
        assert not any(peer.scope for peer in daemon.log.state.peers.values())


EARLIER = "Fri Oct 30 23:59:59 2026"
"""A start time other than ``STARTED``, that of a process whose pid was reused."""


def test_a_pid_reused_between_accept_and_hello_is_refused(
    directory: Path, tmp_path: Path
) -> None:
    """Decided by the operator on 2026-10-31, finding F2 of the round 12 review. The
    caller's pid started at ``EARLIER`` when its connection was accepted. By the
    hello, a daemon of the announced name holds that pid with ``STARTED`` and has
    registered it. The pid and the entry agree at the hello, and the start time read
    at accept does not, so the hello is refused and records no open peer."""
    pid = os.getpid()
    answers = iter([EARLIER, STARTED, STARTED])
    receiver = open_config(name="tidepool", open=RECEIVER_OPEN)
    with daemon_for(
        tmp_path / "tidepool", receiver, directory, fixed_start(pid)
    ) as daemon:
        registry.register(
            directory, entry("harbor", tmp_path / "harbor", pid=pid), fixed_start(pid)
        )
        daemon.start_of = lambda _pid: next(answers)
        session = PeerSession(daemon, peer_pid=pid)
        asyncio.run(session.read_peer_start())
        assert session.peer_started == EARLIER
        greeted = ask(session, open_hello("harbor"))
        assert not greeted.ok
        assert session.peer_id is None
        assert "harbor" not in daemon.log.state.peers


def test_the_start_time_is_read_once_off_the_event_loop(
    directory: Path, tmp_path: Path
) -> None:
    """The start time is read in a worker thread, never on the event loop's, and
    a caller whose start time matches at accept and at the hello is named."""
    pid = os.getpid()
    threads: list[int] = []

    def table(found: int) -> str | None:
        threads.append(threading.get_ident())
        return STARTED if found == pid else None

    receiver = open_config(name="tidepool", open=RECEIVER_OPEN)
    with daemon_for(
        tmp_path / "tidepool", receiver, directory, fixed_start(pid)
    ) as daemon:
        registry.register(
            directory, entry("harbor", tmp_path / "harbor", pid=pid), fixed_start(pid)
        )
        daemon.start_of = table
        session = PeerSession(daemon, peer_pid=pid)

        async def accept_and_greet() -> tuple[int, Response]:
            await session.read_peer_start()
            return threading.get_ident(), await session.respond(open_hello("harbor"))

        loop_thread, greeted = asyncio.run(accept_and_greet())
        assert greeted.ok, greeted.error_message
        assert session.peer_id == "harbor"
        assert len(threads) == 2
        assert loop_thread not in threads


@pytest.mark.parametrize("table", ["unreadable", "gone"])
def test_a_start_time_that_cannot_be_read_at_accept_is_refused(
    table: str, directory: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A process table that cannot answer, and a caller gone before its start time
    is read, both leave the start time unread, and the hello is refused."""
    pid = os.getpid()
    receiver = open_config(name="tidepool", open=RECEIVER_OPEN)
    with daemon_for(
        tmp_path / "tidepool", receiver, directory, fixed_start(pid)
    ) as daemon:
        registry.register(
            directory, entry("harbor", tmp_path / "harbor", pid=pid), fixed_start(pid)
        )
        daemon.start_of = unreadable if table == "unreadable" else fixed_start()
        session = PeerSession(daemon, peer_pid=pid)
        asyncio.run(session.read_peer_start())
        assert session.peer_started is None
        daemon.start_of = fixed_start(pid)
        assert not ask(session, open_hello("harbor")).ok
        assert "harbor" not in daemon.log.state.peers
    if table == "unreadable":
        assert "could not be read" in capsys.readouterr().err


def test_a_registered_declared_peer_is_governed_by_its_own_table(
    directory: Path, tmp_path: Path
) -> None:
    caller = entry("harbor", tmp_path / "harbor")
    receiver = parse_federation(
        {"local": "open", "listen": True, "name": "tidepool", "open": RECEIVER_OPEN},
        {
            "kin": {
                "socket": str(caller.socket),
                "inbound": True,
                "reach": [],
            }
        },
    )
    with daemon_for(
        tmp_path / "tidepool", receiver, directory, fixed_start(os.getpid())
    ) as daemon:
        registry.register(directory, caller, fixed_start(os.getpid()))
        session = PeerSession(daemon, peer_pid=os.getpid(), peer_started=STARTED)
        assert ask(session, open_hello("harbor")).ok
        assert session.peer_id == "kin"
        refused = ask(session, frame("deliver", note()))
        assert refused.result is not None and refused.result["code"] == "refused"
        assert "harbor" not in daemon.log.state.peers


class SessionLink:
    """A link into another daemon's peer session, in process."""

    def __init__(self, session: PeerSession) -> None:
        self.session = session

    async def exchange(self, request: Request) -> Response:
        answer = await self.session.respond(encode_request(request, 1 << 20))
        return decode_response(encode_response(answer, 1 << 20), 1 << 20)

    async def close(self) -> None:
        return None


@dataclass
class Bridge:
    """A connector that reaches a receiving daemon in process, recording the socket
    the registry gave for each link."""

    receiver: Daemon
    sockets: list[Path | None] = field(default_factory=list[Path | None])

    async def connect(self, peer_id: str, definition: PeerDefinition) -> Link:
        self.sockets.append(definition.socket)
        return SessionLink(
            PeerSession(self.receiver, peer_pid=os.getpid(), peer_started=STARTED)
        )


def deliver_all(sender: Daemon) -> None:
    async def run() -> None:
        forwarder = sender.forwarder()
        for _ in range(10):
            for peer in list(forwarder.peers.values()):
                await peer.step()
            if not sender.log.state.outbox.get("tidepool"):
                return

    asyncio.run(run())


def test_two_open_daemons_exchange_a_note_through_the_registry(
    directory: Path, tmp_path: Path
) -> None:
    start_of = fixed_start(os.getpid())
    with (
        daemon_for(
            tmp_path / "tidepool",
            open_config(name="tidepool", open=RECEIVER_OPEN),
            directory,
            start_of,
        ) as receiver,
        daemon_for(
            tmp_path / "harbor",
            open_config(name="harbor", open=SENDER_OPEN),
            directory,
            start_of,
            lines=("alpha",),
        ) as sender,
    ):
        receiving = register_daemon(receiver)
        register_daemon(sender)
        bridge = Bridge(receiver)
        sender.connect = bridge.connect
        message_id = sender.scheduler.send(
            ALPHA, RemoteAddress("tidepool", CORE), "hello across projects"
        )
        assert sender.log.state.peers["tidepool"].scope == SCOPE_LOCAL
        deliver_all(sender)
        assert bridge.sockets == [receiving.socket]
        assert message_id not in sender.log.state.outbox.get("tidepool", {})
        received = [
            m
            for m in receiver.log.state.messages.values()
            if m.sender == RemoteAddress("harbor", ALPHA)
        ]
        assert [m.body for m in received] == ["hello across projects"]


def test_a_declared_peer_with_no_token_is_reached_through_the_registry(
    directory: Path, tmp_path: Path
) -> None:
    """The sender declares the receiver but holds no token for it, and the receiver
    does not declare the sender. Both are open, so the sender's hello names it by its
    registry entry, and the receiver takes it as an open peer."""
    start_of = fixed_start(os.getpid())
    with daemon_for(
        tmp_path / "tidepool",
        open_config(name="tidepool", open=RECEIVER_OPEN),
        directory,
        start_of,
    ) as receiver:
        receiving = register_daemon(receiver)
        declared = parse_federation(
            {"local": "open", "listen": True, "name": "harbor"},
            {
                "tidepool": {
                    "socket": str(receiving.socket),
                    "outbound": True,
                    "senders": ["alpha"],
                    "remote": ["core"],
                }
            },
        )
        with daemon_for(
            tmp_path / "harbor", declared, directory, start_of, lines=("alpha",)
        ) as sender:
            register_daemon(sender)
            sender.connect = Bridge(receiver).connect
            sender.scheduler.send(ALPHA, RemoteAddress("tidepool", CORE), "declared")
            assert sender.log.state.peers["tidepool"].scope == ""
            deliver_all(sender)
            assert receiver.log.state.peers["harbor"].scope == SCOPE_LOCAL
            bodies = [m.body for m in receiver.log.state.messages.values()]
            assert "declared" in bodies


def test_mail_to_an_unregistered_or_unpermitted_project_records_nothing(
    directory: Path, tmp_path: Path
) -> None:
    with daemon_for(
        tmp_path / "harbor",
        open_config(name="harbor", open=SENDER_OPEN),
        directory,
        fixed_start(os.getpid()),
        lines=("alpha", "beta"),
    ) as sender:
        before = len(sender.log.state.messages)
        with pytest.raises(EventRejected, match="no peer"):
            sender.scheduler.send(ALPHA, RemoteAddress("tidepool", CORE), "hi")
        registry.register(
            directory, entry("tidepool", tmp_path / "tidepool"), fixed_start()
        )
        with pytest.raises(EventRejected, match="may not send"):
            sender.scheduler.send(
                LineAddress("beta"), RemoteAddress("tidepool", CORE), "hi"
            )
        with pytest.raises(EventRejected, match="not a party"):
            sender.scheduler.send(ALPHA, RemoteAddress("tidepool", ALPHA), "hi")
        assert "tidepool" not in sender.log.state.peers
        assert len(sender.log.state.messages) == before


def test_an_open_peer_without_a_live_entry_backs_off(
    directory: Path, tmp_path: Path
) -> None:
    with daemon_for(
        tmp_path / "harbor",
        open_config(name="harbor", open=SENDER_OPEN),
        directory,
        fixed_start(),
        lines=("alpha",),
    ) as sender:
        registry.register(
            directory, entry("tidepool", tmp_path / "tidepool"), fixed_start()
        )
        sender.connect = Bridge(sender).connect
        sender.scheduler.send(ALPHA, RemoteAddress("tidepool", CORE), "hi")

        async def run() -> str | None:
            forwarder = sender.forwarder()
            await forwarder.peers["tidepool"].step()
            return forwarder.peers["tidepool"].report().phase.value

        assert asyncio.run(run()) == "backing_off"
        assert sender.log.state.outbox["tidepool"]


def test_mail_waiting_for_an_open_peer_returns_when_the_mode_closes(
    directory: Path, tmp_path: Path
) -> None:
    with daemon_for(
        tmp_path / "harbor",
        open_config(name="harbor", open=SENDER_OPEN),
        directory,
        fixed_start(),
        lines=("alpha",),
    ) as sender:
        registry.register(
            directory, entry("tidepool", tmp_path / "tidepool"), fixed_start()
        )
        message_id = sender.scheduler.send(ALPHA, RemoteAddress("tidepool", CORE), "hi")
        sender.project = ProjectConfig(federation=NO_FEDERATION)
        sender.forwarder()
        assert message_id not in sender.log.state.outbox.get("tidepool", {})


# Remote open, judged in process. The listener's acceptance of an undeclared key is
# refused at start for now, so these drive the session as the listener would.


def test_an_unknown_remote_key_reaches_only_remote_reach(
    directory: Path, tmp_path: Path
) -> None:
    remote = parse_federation(
        {
            "tls_listen": "127.0.0.1:7411",
            "remote": "open",
            "allowed_networks": LOOPBACK,
            "open": {"reach": ["core"], "remote_reach": ["@ana"]},
        },
        None,
    )
    with daemon_for(tmp_path / "harbor", remote, directory, fixed_start()) as daemon:
        session = PeerSession(daemon, pin_sha256=PIN)
        greeted = ask(session, frame("hello", {"versions": [1], "name": "far"}))
        assert greeted.ok, greeted.error_message
        assert session.peer_id == PIN
        assert daemon.log.state.peers[PIN].scope == SCOPE_REMOTE
        refused = ask(session, frame("deliver", note("core")))
        assert refused.result is not None and refused.result["code"] == "refused"
        accepted = ask(session, frame("deliver", note("@ana", origin_id="o2")))
        assert accepted.result is not None
        assert accepted.result["outcome"] == "accepted"
        big = "x" * (DEFAULT_REMOTE_MAX_BODY_BYTES + 1)
        too_large = ask(
            session, frame("deliver", note("@ana", origin_id="o3", body=big))
        )
        assert too_large.result is not None
        assert too_large.result["code"] == "too_large"


def remote_open(**open_keys: object) -> FederationConfig:
    return parse_federation(
        {
            "tls_listen": "127.0.0.1:7411",
            "remote": "open",
            "allowed_networks": LOOPBACK,
            "open": {"remote_reach": ["@ana"], **open_keys},
        },
        None,
    )


def minted(index: int) -> str:
    """The pin of the ``index``th key a host mints, distinct from ``PIN``."""
    return f"{index + 1:064x}"


def test_a_host_that_mints_keys_past_the_cap_is_refused_before_any_event(
    directory: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Decided by the operator on 2026-10-31, finding F1 of the round 12 review. One
    host presents a fresh key at each handshake. Each key up to the cap is recorded
    as an open peer, the next is refused with nothing appended to the log, and a key
    recorded already still names its peer."""
    assert MAX_OPEN_REMOTE_PEERS == MAX_PEERS
    hello = frame("hello", {"versions": [1], "name": "far"})
    with daemon_for(
        tmp_path / "harbor", remote_open(), directory, fixed_start()
    ) as daemon:
        for index in range(MAX_OPEN_REMOTE_PEERS):
            assert ask(PeerSession(daemon, pin_sha256=minted(index)), hello).ok
        opened = daemon.log.state.peers
        assert len([p for p in opened.values() if p.scope == SCOPE_REMOTE]) == (
            MAX_OPEN_REMOTE_PEERS
        )
        before = daemon.log.last_seq
        past = minted(MAX_OPEN_REMOTE_PEERS)
        refused = ask(PeerSession(daemon, pin_sha256=past), hello)
        assert not refused.ok
        assert daemon.log.last_seq == before
        assert past not in daemon.log.state.peers
        assert ask(PeerSession(daemon, pin_sha256=minted(0)), hello).ok
        assert daemon.log.last_seq == before
    assert "undeclared keys are recorded already" in capsys.readouterr().err


def test_every_undeclared_key_shares_one_bound_on_waiting_mail(
    directory: Path, tmp_path: Path
) -> None:
    """Each key may have 10 notes waiting, and all of them together 3. The first key
    fills the shared bound, so the second key's first note is refused as full, and a
    declared limit of its own would not have stopped it."""
    federation = remote_open(remote_waiting=10, remote_total_waiting=3)
    hello = frame("hello", {"versions": [1], "name": "far"})
    with daemon_for(
        tmp_path / "harbor", federation, directory, fixed_start()
    ) as daemon:
        first = PeerSession(daemon, pin_sha256=minted(0))
        second = PeerSession(daemon, pin_sha256=minted(1))
        assert ask(first, hello).ok and ask(second, hello).ok
        for index in range(3):
            sent = ask(first, frame("deliver", note("@ana", origin_id=f"o{index}")))
            assert sent.result is not None and sent.result["outcome"] == "accepted"
        full = ask(second, frame("deliver", note("@ana", origin_id="p0")))
        assert full.result is not None and full.result["code"] == "full"
        assert daemon.log.state.waiting_from.get(minted(1), 0) == 0


def test_the_scheduler_is_given_the_allowance_the_undeclared_keys_share(
    directory: Path, tmp_path: Path
) -> None:
    federation = remote_open(remote_total_turns_per_hour=4)
    with daemon_for(
        tmp_path / "harbor", federation, directory, fixed_start()
    ) as daemon:
        daemon.log.append(PeerOpened(PIN, SCOPE_REMOTE, AT))
        turns = daemon.scheduler.peer_turns
        assert turns.shared(PIN)
        assert not turns.shared(OTHER_PIN)
        assert turns.shared_allowance() == 4


def test_the_shared_bounds_default_to_one_declared_peers_and_are_checked() -> None:
    parsed = remote_open().open
    assert (parsed.remote_total_turns_per_hour, parsed.remote_total_waiting) == (
        DEFAULT_REMOTE_TOTAL_TURNS_PER_HOUR,
        DEFAULT_REMOTE_TOTAL_WAITING,
    )
    assert (DEFAULT_REMOTE_TOTAL_TURNS_PER_HOUR, DEFAULT_REMOTE_TOTAL_WAITING) == (
        DEFAULT_TURNS_PER_HOUR,
        DEFAULT_WAITING,
    )
    for key in ("remote_total_turns_per_hour", "remote_total_waiting"):
        with pytest.raises(FederationError, match=f"open.{key} must be"):
            remote_open(**{key: 0})


def stub_receive(*_args: object) -> Received:
    """Records nothing, so no delivery is a resend and none fills a waiting bound."""
    return Received("m1", False)


@pytest.mark.parametrize("allowance", [None, 5])
def test_every_undeclared_key_shares_one_rate_of_deliveries(
    allowance: int | None,
) -> None:
    """Decided by the operator on 2026-10-31, finding R1 of the round 12 re-review.
    The most undeclared keys there may be deliver in turn, each far within its own
    rate, and together they make no more deliveries in one window than the shared
    rate, by default one declared peer's. A declared peer is not slowed by them, and
    the shared rate comes back when the window passes."""
    chosen = {} if allowance is None else {"remote_total_deliveries": allowance}
    federation = parse_federation(
        {
            "tls_listen": "127.0.0.1:7411",
            "remote": "open",
            "allowed_networks": LOOPBACK,
            "open": {"remote_reach": ["@ana"], **chosen},
        },
        {"kin": {"socket": "/srv/k/run/peers.sock", "inbound": True}},
    )
    expected = RATE_PER_PEER if allowance is None else allowance
    now = [0.0]
    receiver = Receiver(lambda: now[0])
    state = State()
    keys = [minted(index) for index in range(MAX_OPEN_REMOTE_PEERS)]
    for key in keys:
        state.apply(PeerOpened(key, SCOPE_REMOTE, AT))
    for event in plan_peers(state, federation):
        state.apply(event)
    operators = frozenset({"ana"})

    def deliver(peer_id: str, origin_id: str) -> Outcome:
        args = note("@ana", origin_id=origin_id)
        return receiver.deliver(
            state, federation, operators, peer_id, args, stub_receive
        )

    rounds = 2
    assert rounds < RATE_PER_PEER < rounds * MAX_OPEN_REMOTE_PEERS
    answers = [deliver(key, f"o{round_}") for round_ in range(rounds) for key in keys]
    passed = [answer for answer in answers if not isinstance(answer, Busy)]
    assert len(passed) == expected
    assert all(isinstance(answer, Accepted) for answer in passed)
    assert answers[expected] == Busy(int(RATE_WINDOW_SECONDS))
    assert not isinstance(deliver("kin", "k0"), Busy)
    now[0] += RATE_WINDOW_SECONDS
    assert isinstance(deliver(keys[-1], "late"), Accepted)


def test_the_shared_rate_defaults_to_one_declared_peers_and_never_loosens() -> None:
    """The default is one declared peer's rate, which ``federation`` writes as a
    number, and the largest setting is what the cap's keys could make without it."""
    assert DEFAULT_REMOTE_TOTAL_DELIVERIES == RATE_PER_PEER
    assert remote_open().open.remote_total_deliveries == RATE_PER_PEER
    assert MAX_REMOTE_TOTAL_DELIVERIES == MAX_OPEN_REMOTE_PEERS * RATE_PER_PEER
    widest = remote_open(remote_total_deliveries=MAX_REMOTE_TOTAL_DELIVERIES)
    assert widest.open.remote_total_deliveries == MAX_REMOTE_TOTAL_DELIVERIES
    for value in (0, MAX_REMOTE_TOTAL_DELIVERIES + 1, "60"):
        with pytest.raises(
            FederationError, match="open.remote_total_deliveries must be"
        ):
            remote_open(remote_total_deliveries=value)


def test_only_an_undeclared_remote_key_is_an_open_remote_peer() -> None:
    federation = parse_federation(
        {
            "tls_listen": "127.0.0.1:7411",
            "local": "open",
            "listen": True,
            "name": "harbor",
            "remote": "open",
            "allowed_networks": LOOPBACK,
        },
        {"kin": {"socket": "/srv/k/run/peers.sock"}},
    )
    state = State()
    state.apply(PeerOpened(PIN, SCOPE_REMOTE, AT))
    state.apply(PeerOpened(OTHER_PIN, SCOPE_REMOTE, AT))
    state.apply(PeerRemoved(OTHER_PIN))
    state.apply(PeerOpened("tidepool", SCOPE_LOCAL, AT))
    for event in plan_peers(state, federation):
        state.apply(event)
    assert federation.open_remote(state) == [PIN]
    assert federation.is_open_remote(state, PIN)
    for other in (OTHER_PIN, "tidepool", "kin", "nobody"):
        assert not federation.is_open_remote(state, other)


def test_a_key_a_declared_peer_pins_is_never_an_open_peer(
    directory: Path, tmp_path: Path
) -> None:
    remote = parse_federation(
        {
            "tls_listen": "127.0.0.1:7411",
            "remote": "open",
            "allowed_networks": LOOPBACK,
            "open": {"remote_reach": ["@ana"]},
        },
        {
            "far": {
                "transport": "tls",
                "address": "far.example.net:7411",
                "pin_sha256": OTHER_PIN,
                "certificate": "peers/far.pem",
            }
        },
    )
    with daemon_for(tmp_path / "harbor", remote, directory, fixed_start()) as daemon:
        session = PeerSession(daemon, pin_sha256=OTHER_PIN)
        assert not ask(session, frame("hello", {"versions": [1], "name": "far"})).ok
        assert OTHER_PIN not in daemon.log.state.peers


def test_an_unknown_key_is_refused_while_remote_is_an_allowlist(
    directory: Path, tmp_path: Path
) -> None:
    with daemon_for(
        tmp_path / "harbor", parse_federation(None, None), directory, fixed_start()
    ) as daemon:
        session = PeerSession(daemon, pin_sha256=PIN)
        assert not ask(session, frame("hello", {"versions": [1], "name": "far"})).ok
        assert PIN not in daemon.log.state.peers


def test_an_outbound_check_never_lets_mail_reach_an_undeclared_key() -> None:
    remote = parse_federation(
        {
            "tls_listen": "127.0.0.1:7411",
            "remote": "open",
            "allowed_networks": LOOPBACK,
        },
        None,
    )
    state = State()
    state.apply(PeerOpened(PIN, SCOPE_REMOTE, AT))
    with pytest.raises(EventRejected, match="is off"):
        check_outbound(
            state, remote, ALPHA, RemoteAddress(PIN, CORE), "hi", priority=False
        )


# The protected directories.


def test_every_registered_project_is_denied_from_the_next_build(
    directory: Path, tmp_path: Path
) -> None:
    (tmp_path / "harbor").mkdir()
    paths = project_paths(tmp_path / "harbor", RUNTIME[-1] / "harbor")
    federation = open_config()
    assert Path(os.path.realpath(directory)) in protected_dirs(
        paths, federation, directory
    )
    assert Path(os.path.realpath(directory)) not in protected_dirs(paths, NO_FEDERATION)
    read = registry_protected(paths, federation, directory)
    assert read is not None
    assert read() == ()
    other = entry("tidepool", tmp_path / "tidepool")
    registry.register(directory, other, fixed_start())
    assert set(read()) == {Path(os.path.realpath(each)) for each in other.directories()}
    assert registry_protected(paths, NO_FEDERATION, directory) is None


def test_an_open_daemon_registers_and_refuses_a_disclosed_or_held_name(
    directory: Path, tmp_path: Path
) -> None:
    (tmp_path / "harbor").mkdir()
    paths: ProjectPaths = project_paths(tmp_path / "harbor", RUNTIME[-1] / "harbor")
    federation = open_config()
    start_of = fixed_start(os.getpid())
    with pytest.raises(FederationError, match="disclosure list"):
        open_registration(paths, federation, directory, lambda _t: True, start_of)
    found = open_registration(paths, federation, directory, lambda _t: False, start_of)
    assert found is not None and found.name == "harbor"
    with pytest.raises(FederationError, match="held by the live daemon"):
        open_registration(paths, federation, directory, lambda _t: False, start_of)
    assert registry.unregister(directory, found)
    assert (
        open_registration(paths, NO_FEDERATION, directory, lambda _t: False, start_of)
        is None
    )
    inside = paths.root / "registry"
    inside.mkdir(parents=True, mode=0o700)
    with pytest.raises(FederationError, match="inside the project"):
        open_registration(paths, federation, inside, lambda _t: False, start_of)


# Over the sockets, which a sandbox denies.


@pytest.mark.socket
def test_two_open_daemons_exchange_a_note_over_their_sockets(
    tmp_path: Path, short_runtime: Path
) -> None:
    """Two daemons started as processes, each open and registered in the test's own
    registry, which they inherit through ``DROMON_REGISTRY_DIR``, exchange one
    operator's note through their peer sockets, named by their entries and their pids.

    **Found on 2026-10-31.** The first run sent to a receiver that had never seen
    ``@ana``, which refused the note at once, since a daemon held an operator only once
    that operator had sent through it. Decision 25 of ``FEDERATION.md`` lets an operator
    the project file declares receive first, so the receiver declares ``@ana`` by
    ``[daemon] operator`` and nobody sends through it."""
    registry_dir = Path(os.environ[registry.REGISTRY_DIR_ENV])
    names = ("tidepool", "harbor")
    started: list[tuple[ProjectPaths, int]] = []
    try:
        for index, name in enumerate(names):
            root = tmp_path / name
            root.mkdir()
            paths = project_paths(root, short_runtime / str(index))
            paths.state_dir.mkdir(mode=0o700)
            other = names[1 - index]
            (paths.state_dir / "dromon.toml").write_text(
                'version = 1\n[daemon]\noperator = "ana"\n'
                '[federation]\nlisten = true\nlocal = "open"\n'
                f'name = "{name}"\n'
                "[federation.open]\n"
                'reach = ["@ana"]\nsenders = ["@ana"]\n'
                f'remote = ["{other}:@ana"]\n'
            )
            started.append((paths, client.start(paths, ["--hold-wakes"])))
        receiving, _ = started[0]
        sending, _ = started[1]
        assert {each.name for each in registry.entries(registry_dir)} == set(names)
        sent = client.call(
            sending.socket,
            Operation.SEND,
            args={"operator": "ana", "recipient": f"{names[0]}:@ana", "body": "hi"},
        )
        assert sent.ok, sent.error_message
        deadline = time.monotonic() + 30
        found = False
        listed = None
        while time.monotonic() < deadline and not found:
            listed = client.call(
                receiving.socket, Operation.MAIL, args={"operator": "ana"}
            )
            found = listed.ok and f"{names[1]}:@ana" in json.dumps(listed.result)
            time.sleep(0.2)
        # The runtime directories, which hold each daemon's output, are removed
        # after the test, so a failure carries them.
        outputs = {
            paths.root.name: paths.daemon_output.read_text(errors="replace")
            for paths, _ in started
            if paths.daemon_output.exists()
        }
        assert found, (listed, outputs)
    finally:
        for paths, pid in started:
            if client.is_running(paths):
                client.stop(paths)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                finished, _ = os.waitpid(pid, os.WNOHANG)
                if finished:
                    break
                time.sleep(0.05)
