"""The ``[federation]`` and ``[peers]`` tables, the directories each peer adds to the
protected set, and the refusal of unconfined engines in a project with peers. The
design is ``docs/architecture/FEDERATION.md``."""

from dataclasses import replace
from pathlib import Path

import pytest

from dromon import federation
from dromon.daemon import check_sandbox_required, check_sandboxes, protected_dirs
from dromon.driver import ENGINE_OF, ENGINES
from dromon.federation import (
    DEFAULT_MAX_BODY_BYTES,
    MAX_PARTIES,
    MAX_PEERS,
    NO_FEDERATION,
    FederationError,
    PeerDefinition,
    parse_federation,
    peer_directories,
    unconfined_refusal,
)
from dromon.federation_tls import MAX_PEER_CERTIFICATES
from dromon.lineconfig import DEFAULT_CONFIG, ConfigError
from dromon.model import AgentAddress, LineAddress, OperatorAddress
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ConfigFileError, ProjectConfig, parse
from dromon.sandbox import SandboxError, parse_sandbox

SOCKET = "/srv/keleusma/run/peers.sock"
PI = next(model for model, engine in ENGINE_OF.items() if engine == "pi")
OPUS = "claude-opus-5-5"


def peer(**keys: object) -> dict[str, object]:
    return {"socket": SOCKET, **keys}


PIN = "0123456789abcdef" * 4


def tls(**keys: object) -> dict[str, object]:
    found: dict[str, object] = {
        "transport": "tls",
        "address": "studio.example.net:7411",
        "pin_sha256": PIN,
        "certificate": "peers/studio.pem",
    }
    found.update(keys)
    return {key: value for key, value in found.items() if value is not None}


def declared(**peers: dict[str, object]) -> federation.FederationConfig:
    return parse_federation(None, peers)


def test_no_tables_declare_nothing() -> None:
    assert parse_federation(None, None) == NO_FEDERATION
    assert NO_FEDERATION.peers == {} and not NO_FEDERATION.listen


def test_a_peer_with_only_its_socket_can_do_nothing() -> None:
    found = declared(keleusma=peer()).peers["keleusma"]
    assert found == PeerDefinition("keleusma", Path(SOCKET))
    assert not found.inbound and not found.outbound
    assert found.reach == found.senders == found.remote == ()
    assert found.max_body_bytes == DEFAULT_MAX_BODY_BYTES


def test_a_full_peer_reads_back_with_its_parties_sorted() -> None:
    config = parse_federation(
        {"listen": True, "accept_unconfined": True, "waiting_per_recipient": 3},
        {
            "keleusma": peer(
                project="/srv/keleusma",
                inbound=True,
                outbound=True,
                reach=["core", "@ana"],
                senders=["review", "&claude", "core"],
                remote=["kaizen"],
                priority_in=True,
                priority_out=True,
                max_body_bytes=10,
                expire_hours=2,
                turns_per_hour=4,
                waiting=9,
            )
        },
    )
    assert config.listen and config.accept_unconfined
    assert config.waiting_per_recipient == 3
    found = config.peers["keleusma"]
    assert found.project == Path("/srv/keleusma")
    assert found.reach == (OperatorAddress("ana"), LineAddress("core"))
    assert found.senders == (
        AgentAddress("claude"),
        LineAddress("core"),
        LineAddress("review"),
    )
    assert found.remote == (LineAddress("kaizen"),)
    assert (found.max_body_bytes, found.expire_hours) == (10, 2)
    assert (found.turns_per_hour, found.waiting) == (4, 9)
    assert found.inbound and found.outbound and found.priority_in and found.priority_out


MALFORMED: list[tuple[tuple[object, object], str]] = [
    (({"bogus": 1}, None), "federation.bogus is not a known key"),
    (({"listen": 1}, None), "federation.listen must be a boolean"),
    (({"waiting_per_recipient": 0}, None), "must be a whole number from 1"),
    (([], None), "federation must be a table"),
    ((None, []), "peers must be a table"),
    ((None, {"Bad": peer()}), "peers.Bad is not a valid peer identifier"),
    ((None, {"k": 1}), "peers.k must be a table"),
    ((None, {"k": peer(bogus=1)}), "peers.k.bogus is not a known key"),
    ((None, {"k": peer(transport="smtp")}), "peers.k.transport must be unix or tls"),
    ((None, {"k": peer(address="h:1")}), "peers.k.address does not apply to a unix"),
    ((None, {"k": tls(socket=SOCKET)}), "peers.k.socket does not apply to a tls"),
    ((None, {"k": tls(project="/srv/k")}), "peers.k.project does not apply to a tls"),
    ((None, {"k": tls(address=None)}), "peers.k.address must be a host:port"),
    ((None, {"k": tls(address="host")}), "peers.k.address must end in :PORT"),
    ((None, {"k": tls(address="host:0")}), "port outside 1 to 65535"),
    ((None, {"k": tls(address="host:65536")}), "port outside 1 to 65535"),
    ((None, {"k": tls(address="host:+1")}), "must end in :PORT"),
    ((None, {"k": tls(address="a b:1")}), "peers.k.address has a malformed host"),
    ((None, {"k": tls(address="h/x:1")}), "has a malformed host"),
    ((None, {"k": tls(address=":1")}), "has a malformed host"),
    ((None, {"k": tls(address="-h:1")}), "has a malformed host"),
    ((None, {"k": tls(address="ü.example:1")}), "has a malformed host"),
    ((None, {"k": tls(address="[::g]:1")}), "has a malformed address"),
    ((None, {"k": tls(pin_sha256=None)}), "pin_sha256 must be 64 lowercase"),
    ((None, {"k": tls(pin_sha256=PIN.upper())}), "pin_sha256 must be 64 lowercase"),
    ((None, {"k": tls(pin_sha256=PIN[:-1])}), "pin_sha256 must be 64 lowercase"),
    ((None, {"k": tls(certificate=None)}), "must be a path relative to .dromon"),
    ((None, {"k": tls(certificate="/k.pem")}), "must be a path relative to .dromon"),
    ((None, {"k": tls(certificate="~/k.pem")}), "must be a path relative"),
    ((None, {"k": tls(certificate="peers/../k.pem")}), "has an empty, . or .."),
    ((None, {"k": tls(certificate="peers//k.pem")}), "has an empty, . or .."),
    ((None, {"k": tls(certificate="a\x00")}), "holds a zero byte"),
    ((None, {"k": tls(certificate="a" * 1025)}), "at most 1024 bytes"),
    ((None, {"k": tls(inbound=True)}), "so federation.tls_listen must name"),
    (({"tls_listen": "0.0.0.0:7411"}, {"k": tls()}), "no tls peer is inbound"),
    (({"tls_listen": "0.0.0.0:7411"}, None), "no tls peer is inbound"),
    (({"tls_listen": 7411}, None), "federation.tls_listen must be a host:port"),
    ((None, {"a": tls(), "b": tls()}), "peers.b and peers.a share one pin"),
    ((None, {"k": {}}), "peers.k.socket must be an absolute path"),
    ((None, {"k": peer(socket="run/peers.sock")}), "must be an absolute path"),
    ((None, {"k": peer(socket="/srv/k/run/x.sock")}), "must name a file peers"),
    ((None, {"k": peer(socket="/srv/../run/peers.sock")}), "has a . or .. step"),
    ((None, {"k": peer(socket="/" + "a" * 100 + "/peers.sock")}), "at most 103"),
    ((None, {"k": peer(socket="/srv/\x00/peers.sock")}), "holds a zero byte"),
    ((None, {"k": peer(project="srv/k")}), "peers.k.project must be an absolute"),
    ((None, {"k": peer(inbound="yes")}), "peers.k.inbound must be a boolean"),
    ((None, {"k": peer(reach="core")}), "peers.k.reach must be a list"),
    ((None, {"k": peer(reach=[1])}), "holds 1, not an address"),
    ((None, {"k": peer(reach=["other:core"])}), "which is not an address"),
    ((None, {"k": peer(reach=["core\n"])}), "which is not an address"),
    ((None, {"k": peer(senders=["@"])}), "which is not an address"),
    ((None, {"k": peer(remote=["a", "a"])}), "names 'a' twice"),
    ((None, {"k": peer(max_body_bytes=0)}), "max_body_bytes must be a whole"),
    ((None, {"k": peer(max_body_bytes=True)}), "max_body_bytes must be a whole"),
    ((None, {"k": peer(expire_hours=24 * 91)}), "from 1 to 2160"),
    ((None, {"k": peer(turns_per_hour=61)}), "from 1 to 60"),
    ((None, {"k": peer(waiting=-1)}), "peers.k.waiting must be a whole"),
]


@pytest.mark.parametrize(("tables", "message"), MALFORMED)
def test_malformed_tables_are_refused_with_their_key(
    tables: tuple[object, object], message: str
) -> None:
    with pytest.raises(FederationError, match=message.replace(".", r"\.")):
        parse_federation(*tables)


def test_lists_and_peers_are_bounded() -> None:
    many = [f"line{n}" for n in range(MAX_PARTIES + 1)]
    with pytest.raises(FederationError, match="at most 256 addresses"):
        declared(k=peer(reach=many))
    peers = {f"p{n}": peer(socket=f"/srv/p{n}/peers.sock") for n in range(MAX_PEERS)}
    assert len(parse_federation(None, peers).peers) == MAX_PEERS
    peers["extra"] = peer(socket="/srv/extra/peers.sock")
    with pytest.raises(FederationError, match="more than 64 peers"):
        parse_federation(None, peers)


def test_a_tls_peer_reads_back_with_its_key_fields() -> None:
    config = parse_federation(
        {"tls_listen": "[2001:DB8::1]:7411"},
        {
            "studio": tls(inbound=True, outbound=True),
            "local": tls(address="127.0.0.1:1", pin_sha256="f" * 64),
        },
    )
    listen = config.tls_listen
    assert listen is not None and listen == ("2001:db8::1", 7411)
    assert federation.format_host_port(listen) == "[2001:db8::1]:7411"
    found = config.peers["studio"]
    assert found.transport == "tls" and found.socket is None and found.project is None
    assert found.address == ("studio.example.net", 7411)
    assert found.pin_sha256 == PIN
    assert found.certificate == Path("peers/studio.pem")
    assert found.directories() == ()
    assert config.peers["local"].address == ("127.0.0.1", 1)
    policy = federation.policy_text(found)
    assert '"address":"studio.example.net:7411"' in policy
    assert f'"pin_sha256":"{PIN}"' in policy
    assert '"certificate":"peers/studio.pem"' in policy
    assert '"socket"' not in policy and '"project"' not in policy


def test_a_unix_peers_policy_text_is_what_phase_1_recorded() -> None:
    found = declared(k=peer(project="/srv/keleusma")).peers["k"]
    policy = federation.policy_text(found)
    assert f'"socket":"{SOCKET}"' in policy
    assert '"project":"/srv/keleusma"' in policy
    assert '"address"' not in policy and '"pin_sha256"' not in policy


def test_a_peer_denies_its_runtime_directory_and_its_named_state() -> None:
    bare = PeerDefinition("k", Path(SOCKET))
    assert bare.directories() == (Path(SOCKET).parent,)
    named = PeerDefinition("k", Path(SOCKET), project=Path("/srv/keleusma"))
    assert named.directories() == (Path(SOCKET).parent, Path("/srv/keleusma/.dromon"))


def test_peer_directories_are_resolved_and_refuse_overlap(short_runtime: Path) -> None:
    # Pytest's own temporary paths can exceed the bound on a socket path.
    tmp_path = short_runtime.parent
    real = tmp_path / "real"
    (real / "run").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real)
    config = declared(
        a=peer(socket=str(tmp_path / "link/run/peers.sock"), project=str(real)),
        b=peer(socket=str(tmp_path / "b/run/peers.sock")),
    )
    own = (tmp_path / "own/.dromon", tmp_path / "own/run")
    found = peer_directories(config, own)
    assert found == (
        Path(real.resolve() / "run"),
        Path(real.resolve() / ".dromon"),
        Path((tmp_path / "b/run").resolve()),
    )
    itself = declared(a=peer(socket=str(tmp_path / "own/run/peers.sock")))
    with pytest.raises(FederationError, match="this project's own directory"):
        peer_directories(itself, own)
    shared = declared(
        a=peer(socket=str(tmp_path / "link/run/peers.sock")),
        b=peer(socket=str(real / "run/peers.sock")),
    )
    with pytest.raises(FederationError, match="peers.b and peers.a share"):
        peer_directories(shared, own)


def test_unconfined_engines_are_known_engines() -> None:
    assert federation.UNCONFINED_ENGINES.issubset(ENGINES)


def test_unconfined_engines_are_refused_only_with_peers_and_without_consent() -> None:
    peers = declared(k=peer())
    assert unconfined_refusal(NO_FEDERATION, "a", "pi", False) is None
    assert unconfined_refusal(peers, "a", "codex", False) is None
    for engine in sorted(federation.UNCONFINED_ENGINES):
        for sandboxed in (False, True):
            refusal = unconfined_refusal(peers, "a", engine, sandboxed)
            assert refusal is not None and f"line a runs on {engine}" in refusal
    accepted = parse_federation({"accept_unconfined": True}, {"k": peer()})
    assert unconfined_refusal(accepted, "a", "pi", False) is None


def test_claude_code_is_unconfined_beside_peers_only_without_its_sandbox() -> None:
    peers = declared(k=peer())
    assert federation.SANDBOXED_ENGINES.issubset(ENGINES)
    for engine in sorted(federation.SANDBOXED_ENGINES):
        assert unconfined_refusal(peers, "a", engine, True) is None
        refusal = unconfined_refusal(peers, "a", engine, False)
        assert refusal is not None and "without an enabled sandbox" in refusal
        assert unconfined_refusal(NO_FEDERATION, "a", engine, False) is None
        accepted = parse_federation({"accept_unconfined": True}, {"k": peer()})
        assert unconfined_refusal(accepted, "a", engine, False) is None


def project_with_peers(paths: ProjectPaths, extra: str = "") -> ProjectConfig:
    return parse(
        "version = 1\n"
        f"[federation]\n{extra}\n"
        "[peers.keleusma]\n"
        f'socket = "{paths.root.parent}/keleusma/run/peers.sock"\n'
        f'project = "{paths.root.parent}/keleusma"\n'
    )


@pytest.fixture
def paths(short_runtime: Path) -> ProjectPaths:
    root = short_runtime.parent / "here"
    root.mkdir()
    found = project_paths(root, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


def test_the_project_file_reads_both_tables(paths: ProjectPaths) -> None:
    config = project_with_peers(paths, "listen = true")
    assert config.federation.listen
    assert set(config.federation.peers) == {"keleusma"}
    assert ProjectConfig().federation == NO_FEDERATION


def test_a_bad_peer_names_its_line_in_the_project_file() -> None:
    text = 'version = 1\n\n[peers.k]\nsocket = "relative/peers.sock"\n'
    with pytest.raises(ConfigFileError, match=r"dromon\.toml:3: peers\.k\.socket"):
        parse(text)


def test_protected_directories_add_each_peer(paths: ProjectPaths) -> None:
    config = project_with_peers(paths)
    own = protected_dirs(paths)
    assert own == (paths.state_dir.resolve(), paths.runtime_dir.resolve())
    keleusma = (paths.root.parent / "keleusma").resolve()
    assert protected_dirs(paths, config.federation) == (
        *own,
        keleusma / "run",
        keleusma / ".dromon",
    )


def test_a_peer_that_is_this_project_is_refused(paths: ProjectPaths) -> None:
    config = parse(
        f'version = 1\n[peers.self]\nsocket = "{paths.runtime_dir}/peers.sock"\n'
    )
    with pytest.raises(ConfigError, match="this project's own directory"):
        protected_dirs(paths, config.federation)
    with pytest.raises(ConfigError, match="this project's own directory"):
        check_sandboxes(config, paths)


def test_an_enabled_sandbox_denies_the_peers_directories(paths: ProjectPaths) -> None:
    config = parse(
        "version = 1\n[sandbox]\nenabled = true\n"
        f'[peers.keleusma]\nsocket = "{paths.root.parent}/keleusma/run/peers.sock"\n'
        f"[sandbox.filesystem]\n"
        f'allowRead = ["{paths.root.parent}/keleusma/run/x"]\n'
    )
    with pytest.raises(SandboxError, match="inside a directory Dromon protects"):
        check_sandboxes(config, paths)


def test_a_workdir_inside_a_peer_is_refused(paths: ProjectPaths) -> None:
    nested = paths.root / "vendor" / "other"
    (nested / "run").mkdir(parents=True)
    config = parse(
        "version = 1\n"
        f'[peers.other]\nsocket = "{nested}/run/peers.sock"\n'
        '[lines.a]\nmodel = "claude-opus-5-5"\nworkdir = "vendor/other/run"\n'
    )
    with pytest.raises(ConfigError, match="protect"):
        check_sandboxes(config, paths)


def test_a_line_on_an_unconfined_engine_is_refused_beside_peers(
    paths: ProjectPaths,
) -> None:
    config = project_with_peers(paths)
    with pytest.raises(SandboxError, match="which Dromon cannot confine"):
        check_sandbox_required(config, "a", PI, DEFAULT_CONFIG)
    check_sandbox_required(ProjectConfig(), "a", PI, DEFAULT_CONFIG)
    accepted = project_with_peers(paths, "accept_unconfined = true")
    check_sandbox_required(accepted, "a", PI, DEFAULT_CONFIG)


def test_a_claude_code_line_needs_its_sandbox_beside_peers(
    paths: ProjectPaths,
) -> None:
    config = project_with_peers(paths)
    with pytest.raises(SandboxError, match="without an enabled sandbox"):
        check_sandbox_required(config, "a", OPUS, DEFAULT_CONFIG)
    sandboxed = replace(
        DEFAULT_CONFIG, sandbox=parse_sandbox({"enabled": True}, operator=True)
    )
    check_sandbox_required(config, "a", OPUS, sandboxed)
    disabled = replace(
        DEFAULT_CONFIG, sandbox=parse_sandbox({"enabled": False}, operator=True)
    )
    with pytest.raises(SandboxError, match="without an enabled sandbox"):
        check_sandbox_required(config, "a", OPUS, disabled)
    check_sandbox_required(ProjectConfig(), "a", OPUS, DEFAULT_CONFIG)
    accepted = project_with_peers(paths, "accept_unconfined = true")
    check_sandbox_required(accepted, "a", OPUS, DEFAULT_CONFIG)


# Several inbound tls peers, once each certificate names its own key.


def inbound_peers(count: int) -> dict[str, dict[str, object]]:
    return {
        f"p{index:02d}": tls(inbound=True, reach=["alpha"], pin_sha256=f"{index:064x}")
        for index in range(count)
    }


def test_every_declared_peer_may_be_inbound_over_tls() -> None:
    """The inbound bound is the transport's bound on the certificates one listener
    trusts, which the bound on declared peers does not exceed, so a daemon may
    declare every peer it may have as an inbound tls peer."""
    assert federation.MAX_INBOUND_TLS == MAX_PEER_CERTIFICATES >= MAX_PEERS
    config = parse_federation({"tls_listen": "0.0.0.0:7411"}, inbound_peers(MAX_PEERS))
    assert sum(peer.inbound for peer in config.peers.values()) == MAX_PEERS


def test_inbound_tls_peers_past_the_bound_are_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The check guards the transport's bound should the bound on declared peers ever
    exceed it, so it is reached here with a smaller bound."""
    monkeypatch.setattr(federation, "MAX_INBOUND_TLS", 2)
    parse_federation({"tls_listen": "0.0.0.0:7411"}, inbound_peers(2))
    with pytest.raises(
        FederationError, match="3 tls peers are inbound, from peers.p00, and at most 2"
    ):
        parse_federation({"tls_listen": "0.0.0.0:7411"}, inbound_peers(3))
