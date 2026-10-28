"""The peer table in the event log, the tokens peers present and store, and the
``peer_*`` operations, step 3 of federation phase 1. The design is
``docs/architecture/FEDERATION.md``."""

import asyncio
import json
import os
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import SdkMcpTool

from dromon import federation
from dromon.agents import token_digest
from dromon.configview import Overrides
from dromon.daemon import Daemon
from dromon.driver import Strategy
from dromon.eventlog import EventLog
from dromon.federation import (
    MAX_STORE_ENTRIES,
    PEERS_DIR_NAME,
    FederationConfig,
    FederationError,
    check_token_store,
    ensure_identity,
    parse_federation,
    read_identity,
    plan_peers,
    policy_text,
    store_token,
    stored_token,
    token_path,
)
from dromon.model import (
    MAX_PEER_POLICY_BYTES,
    EventRejected,
    InvalidField,
    Peer,
    PeerDeclared,
    PeerKeyed,
    PeerRemoved,
    State,
)
from dromon.paths import ProjectPaths, project_paths
from dromon.projectconfig import ProjectConfig
from dromon.protocol import Response
from dromon.scheduler import LineEngine, LineSpec, Scheduler

TOKEN = "A" * 43
"""A token of the form ``secrets.token_urlsafe(32)`` writes."""


def peers(**tables: dict[str, object]) -> FederationConfig:
    return parse_federation(None, tables)


def peer(name: str, **keys: object) -> dict[str, object]:
    return {"socket": f"/srv/{name}/run/peers.sock", **keys}


# The model.


def test_a_peer_is_declared_keyed_revoked_and_removed() -> None:
    state = State()
    state.apply(PeerDeclared("k", "unix", "{}"))
    assert state.peers["k"] == Peer("unix", "{}")
    state.apply(PeerKeyed("k", "a" * 64))
    state.apply(PeerDeclared("k", "unix", '{"inbound":true}'))
    assert state.peers["k"] == Peer("unix", '{"inbound":true}', "a" * 64)
    state.apply(PeerKeyed("k", ""))
    assert state.peers["k"].token_sha256 == ""
    state.apply(PeerKeyed("k", "b" * 64))
    state.apply(PeerRemoved("k"))
    assert state.peers["k"] == Peer("unix", '{"inbound":true}', "", removed=True)
    state.apply(PeerDeclared("k", "unix", '{"inbound":true}'))
    assert state.peers["k"] == Peer("unix", '{"inbound":true}')


def test_peer_events_that_do_not_apply_are_rejected() -> None:
    state = State()
    for event in (PeerRemoved("k"), PeerKeyed("k", "a" * 64)):
        with pytest.raises(EventRejected, match="no peer k"):
            state.check(event)
    state.apply(PeerDeclared("k", "unix", "{}"))
    with pytest.raises(EventRejected, match="declared as that"):
        state.check(PeerDeclared("k", "unix", "{}"))
    with pytest.raises(EventRejected, match="no token to revoke"):
        state.check(PeerKeyed("k", ""))
    state.apply(PeerRemoved("k"))
    for event in (PeerRemoved("k"), PeerKeyed("k", "a" * 64)):
        with pytest.raises(EventRejected, match="no peer k"):
            state.check(event)


MALFORMED: list[Callable[[], object]] = [
    lambda: PeerDeclared("K", "unix", "{}"),
    lambda: PeerDeclared("k", "smtp", "{}"),
    lambda: PeerDeclared("k", "unix", "x" * (MAX_PEER_POLICY_BYTES + 1)),
    lambda: PeerKeyed("k", "A" * 64),
    lambda: PeerKeyed("k", "a" * 63),
    lambda: PeerRemoved(""),
]


@pytest.mark.parametrize("make", MALFORMED)
def test_malformed_peer_events_are_invalid(make: Callable[[], object]) -> None:
    with pytest.raises(InvalidField):
        make()


# The plan, which records the project file's peers at start.


def test_the_plan_declares_new_peers_in_name_order() -> None:
    config = peers(b=peer("b"), a=peer("a", inbound=True))
    planned = plan_peers(State(), config)
    assert planned == (
        PeerDeclared("a", "unix", policy_text(config.peers["a"])),
        PeerDeclared("b", "unix", policy_text(config.peers["b"])),
    )


def test_the_plan_declares_changes_removes_dropped_peers_and_is_idempotent() -> None:
    state = State()
    first = peers(a=peer("a"), b=peer("b"))
    for event in plan_peers(state, first):
        state.apply(event)
    assert plan_peers(state, first) == ()
    state.apply(PeerKeyed("a", "a" * 64))
    second = peers(a=peer("a", outbound=True))
    planned = plan_peers(state, second)
    assert planned == (
        PeerDeclared("a", "unix", policy_text(second.peers["a"])),
        PeerRemoved("b"),
    )
    for event in planned:
        state.apply(event)
    assert state.peers["a"].token_sha256 == "a" * 64
    assert plan_peers(state, peers()) == (PeerRemoved("a"),)
    state.apply(PeerRemoved("a"))
    again = plan_peers(state, peers(b=peer("b")))
    assert again == (PeerDeclared("b", "unix", policy_text(first.peers["b"])),)


def test_the_policy_is_canonical_and_holds_every_key_but_the_transport() -> None:
    config = peers(
        k=peer(
            "k",
            project="/srv/k",
            inbound=True,
            reach=["core", "@ana"],
            senders=["review"],
            remote=["kaizen"],
        )
    )
    text = policy_text(config.peers["k"])
    document = json.loads(text)
    # A unix peer's document holds every key a unix peer may set, the one phase 1
    # recorded, and no key of the tls transport.
    assert set(document) == federation.PEER_KEYS - {"transport"} - (
        federation.TLS_ONLY_KEYS
    )
    assert text == json.dumps(document, sort_keys=True, separators=(",", ":"))
    assert document["reach"] == ["@ana", "core"]
    assert document["project"] == "/srv/k"
    assert json.loads(policy_text(peers(k=peer("k")).peers["k"]))["project"] is None


# The daemon's own TLS identity, phase 2 step 2.


def test_the_identity_is_made_once_and_its_pin_kept(tmp_path: Path) -> None:
    store = tmp_path / PEERS_DIR_NAME
    assert read_identity(store) is None
    made, fresh = ensure_identity(store)
    assert fresh and len(made.pin_sha256) == 64
    assert os.stat(store).st_mode & 0o777 == 0o700
    for path in (made.private_key, made.certificate):
        assert path.parent == store and os.stat(path).st_mode & 0o777 == 0o600
    again, fresh = ensure_identity(store)
    assert not fresh and again == made
    assert read_identity(store) == made
    check_token_store(store)


def test_a_damaged_identity_is_refused_not_replaced(tmp_path: Path) -> None:
    store = tmp_path / PEERS_DIR_NAME
    made, _ = ensure_identity(store)
    made.certificate.chmod(0o644)
    with pytest.raises(FederationError, match="wider than 0600"):
        ensure_identity(store)
    made.certificate.chmod(0o600)
    made.certificate.write_bytes(b"not a certificate")
    with pytest.raises(FederationError, match="does not hold a certificate"):
        ensure_identity(store)
    made.certificate.unlink()
    with pytest.raises(FederationError, match="holds only one of"):
        ensure_identity(store)
    assert made.private_key.exists()
    made.certificate.symlink_to(made.private_key)
    with pytest.raises(FederationError, match="could not be read"):
        ensure_identity(store)


def test_peer_key_answers_the_pin_and_never_the_key(paths: ProjectPaths) -> None:
    with attached(paths, peers(k=peer("k"))) as daemon:
        first = ok(daemon, "peer_key")
        assert first["made"] is True
        second = ok(daemon, "peer_key")
        assert second["made"] is False
        assert second["pin_sha256"] == first["pin_sha256"]
        store = paths.state_dir / PEERS_DIR_NAME
        assert first["certificate"] == str(store / "identity-certificate.pem")
        key = (store / "identity-key.pem").read_text()
        assert "PRIVATE KEY" in key and key not in json.dumps(first)
        refused = ask(daemon, "peer_key", {"peer": "k"})
        assert not refused.ok


# The token store.


def test_a_missing_store_passes_and_a_stored_token_reads_back(tmp_path: Path) -> None:
    store = tmp_path / PEERS_DIR_NAME
    check_token_store(store)
    assert stored_token(store, "k") is None
    path = store_token(store, "k", TOKEN)
    assert path == token_path(store, "k")
    assert os.stat(store).st_mode & 0o777 == 0o700
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert stored_token(store, "k") == TOKEN
    store_token(store, "k", "B" * 43)
    assert stored_token(store, "k") == "B" * 43
    assert sorted(entry.name for entry in store.iterdir()) == ["k.token"]
    check_token_store(store)


def test_a_store_wider_than_its_owner_is_refused(tmp_path: Path) -> None:
    store = tmp_path / PEERS_DIR_NAME
    path = store_token(store, "k", TOKEN)
    path.chmod(0o640)
    with pytest.raises(FederationError, match="mode 0640, wider than 0600"):
        check_token_store(store)
    with pytest.raises(FederationError, match="wider than 0600"):
        stored_token(store, "k")
    path.chmod(0o600)
    store.chmod(0o750)
    with pytest.raises(FederationError, match="mode 0750, wider than 0700"):
        check_token_store(store)
    with pytest.raises(FederationError, match="wider than 0700"):
        store_token(store, "j", TOKEN)
    store.chmod(0o700)


def test_a_store_holding_a_link_a_directory_or_too_much_is_refused(
    tmp_path: Path,
) -> None:
    store = tmp_path / PEERS_DIR_NAME
    store.mkdir(mode=0o700)
    (store / "k.token").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(FederationError, match="must be a regular file"):
        check_token_store(store)
    (store / "k.token").unlink()
    (store / "sub").mkdir(mode=0o700)
    with pytest.raises(FederationError, match="must be a regular file"):
        check_token_store(store)
    (store / "sub").rmdir()
    for number in range(MAX_STORE_ENTRIES + 1):
        (store / f"p{number}.token").touch(mode=0o600)
    with pytest.raises(FederationError, match=f"more than {MAX_STORE_ENTRIES}"):
        check_token_store(store)
    linked = tmp_path / "linked"
    linked.symlink_to(store)
    with pytest.raises(FederationError, match="must be a directory"):
        check_token_store(linked)


def test_a_malformed_token_is_refused_without_quoting_it(tmp_path: Path) -> None:
    store = tmp_path / PEERS_DIR_NAME
    for bad in ("short", "has space " + TOKEN, TOKEN + "\n", "é" * 20):
        with pytest.raises(FederationError) as caught:
            store_token(store, "k", bad)
        assert bad not in str(caught.value)
    with pytest.raises(FederationError, match="valid peer identifier"):
        store_token(store, "../k", TOKEN)
    store.mkdir(mode=0o700, exist_ok=True)
    junk = "not a token, but a secret"
    (store / "k.token").write_text(junk, encoding="ascii")
    (store / "k.token").chmod(0o600)
    with pytest.raises(FederationError, match="does not hold a token") as caught:
        stored_token(store, "k")
    assert junk not in str(caught.value)


# The operations, driven in process without sockets.


@pytest.fixture
def paths(tmp_path: Path, short_runtime: Path) -> ProjectPaths:
    found = project_paths(tmp_path, short_runtime)
    found.state_dir.mkdir(mode=0o700)
    return found


def no_engine(spec: LineSpec, tools: Sequence[SdkMcpTool[Any]]) -> LineEngine:
    raise AssertionError(f"no engine may start in this test, but {spec.line_id} did")


@contextmanager
def attached(paths: ProjectPaths, config: FederationConfig) -> Generator[Daemon]:
    with EventLog(paths.event_log) as log:
        scheduler = Scheduler(log, no_engine, hold_wakes=True)
        scheduler.create_line("alpha", "claude-haiku-5-5", Strategy.SUBSCRIPTION)
        for event in plan_peers(log.state, config):
            log.append(event)
        yield Daemon(
            paths,
            log,
            "2026-10-28T00:00:00Z",
            scheduler,
            project=ProjectConfig(federation=config),
            command_line=Overrides(),
            project_file_present=False,
        )


def ask(daemon: Daemon, op: str, args: dict[str, object] | None = None) -> Response:
    line = {"id": "r", "op": op, "v": 1, "args": args or {}}
    return daemon.respond((json.dumps(line) + "\n").encode())


def ok(
    daemon: Daemon, op: str, args: dict[str, object] | None = None
) -> dict[str, object]:
    response = ask(daemon, op, args)
    assert response.ok and response.result is not None, response.error_message
    return response.result


def test_a_token_is_issued_once_and_only_its_digest_is_kept(
    paths: ProjectPaths,
) -> None:
    with attached(paths, peers(k=peer("k", inbound=True))) as daemon:
        result = ok(daemon, "peer_token", {"peer": "k"})
        token = cast(str, result["token"])
        assert daemon.log.state.peers["k"].token_sha256 == token_digest(token)
        assert token.encode("ascii") not in paths.event_log.read_bytes()
        second = cast(str, ok(daemon, "peer_token", {"peer": "k"})["token"])
        assert second != token
        assert daemon.log.state.peers["k"].token_sha256 == token_digest(second)
        assert ok(daemon, "peer_revoke", {"peer": "k"})["revoked"] is True
        assert daemon.log.state.peers["k"].token_sha256 == ""
        refused = ask(daemon, "peer_revoke", {"peer": "k"})
        assert not refused.ok and "no token to revoke" in str(refused.error_message)
        unknown = ask(daemon, "peer_token", {"peer": "nobody"})
        assert not unknown.ok and "no peer nobody" in str(unknown.error_message)


def test_a_stored_token_is_kept_under_the_state_directory(paths: ProjectPaths) -> None:
    with attached(paths, peers(k=peer("k", outbound=True))) as daemon:
        unknown = ask(daemon, "peer_store", {"peer": "nobody", "token": TOKEN})
        assert not unknown.ok and "no peer nobody" in str(unknown.error_message)
        bad = ask(daemon, "peer_store", {"peer": "k", "token": "short"})
        assert not bad.ok and "short" not in str(bad.error_message)
        ok(daemon, "peer_store", {"peer": "k", "token": TOKEN})
        store = paths.state_dir / PEERS_DIR_NAME
        assert stored_token(store, "k") == TOKEN


def test_the_list_shows_each_peer_and_never_a_token(paths: ProjectPaths) -> None:
    config = parse_federation(
        {"listen": True}, {"k": peer("k", inbound=True), "j": peer("j")}
    )
    with attached(paths, config) as daemon:
        issued = cast(str, ok(daemon, "peer_token", {"peer": "k"})["token"])
        ok(daemon, "peer_store", {"peer": "k", "token": TOKEN})
        result = ok(daemon, "peer_list")
        text = json.dumps(result)
        assert issued not in text and TOKEN not in text
        assert token_digest(issued) not in text
        assert result["listen"] is True
        entries = cast(list[dict[str, object]], result["peers"])
        assert [entry["peer"] for entry in entries] == ["j", "k"]
        j, k = entries
        assert (j["inbound"], j["outbound"], j["issued"], j["stored"]) == (
            False,
            False,
            False,
            False,
        )
        assert (k["inbound"], k["issued"], k["stored"]) == (True, True, True)
        (paths.state_dir / PEERS_DIR_NAME / "k.token").chmod(0o644)
        listed = cast(list[dict[str, object]], ok(daemon, "peer_list")["peers"])
        widened = listed[1]["stored"]
        assert isinstance(widened, str) and "wider than 0600" in widened


def test_peer_operations_are_not_served_on_the_agent_socket(
    paths: ProjectPaths,
) -> None:
    with attached(paths, peers(k=peer("k"))) as daemon:
        for op in ("peer_token", "peer_list", "peer_key"):
            line = json.dumps({"id": "r", "op": op, "v": 1, "args": {"peer": "k"}})
            response = asyncio.run(daemon.respond_agent((line + "\n").encode()))
            assert not response.ok
        assert daemon.log.state.peers["k"].token_sha256 == ""
