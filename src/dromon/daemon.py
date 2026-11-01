"""The ``dromond`` command, the Dromon daemon for one project.

The daemon holds the project's event log as its single writer and serves the client
protocol of ``docs/protocol/CLIENT.md`` on a Unix domain socket in a private runtime
directory. It runs in the foreground. ``dromon start`` detaches it.

Lifecycle and failure modes.

- A non-blocking exclusive lock on ``dromond.lock`` admits one daemon per runtime
  directory. A second daemon exits with ``ALREADY_RUNNING`` and touches nothing.
- Holding the lock proves that no other daemon owns the socket path, so a socket file
  left by a crash is safely replaced.
- A ``stop`` request, SIGTERM or SIGINT ends the loop. Open connections are closed,
  the socket file is removed and the event log is closed. The lock file is left in
  place, because removing a lock file races with the next daemon taking it.
- A ``restart`` request drains first, as ``dromon.restart`` describes, then stops as
  ``stop`` does and starts a successor from the installed code once the lock is
  released, so the successor never finds it held.
- Each connection has an idle timeout and a line limit, so a stalled or hostile client
  cannot hold the daemon or exhaust its memory.

Concurrency. Everything runs on one asyncio event loop thread, including the event log's
blocking SQLite calls, which are short, and the scheduler's turns, which wait on their
engine processes without blocking the loop.

Authority. Whoever can connect to the socket runs as the operator's user, so a request
carries operator authority and names the operator it speaks for. Messages from lines
arrive through their ``send_message`` tool and never through the socket.

Porting note. ``fcntl.flock`` and Unix domain sockets are POSIX facilities, available on
every target of the Rust and Keleusma ports except Windows, which is not supported.
"""

import argparse
import asyncio
import json
import contextlib
import fcntl
import os
import shutil
import signal
import ssl
import sys
import tempfile
import time
import traceback
from datetime import UTC, datetime
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Protocol, cast

from claude_agent_sdk import SdkMcpTool
from OpenSSL.SSL import Context as OpenSSLContext

from dromon import __version__, exits, fleet, peercred, registry, restart, runs
from dromon import evidence
from dromon import proposals as proposal_store
from dromon.federation import (
    MAX_OPEN_REMOTE_PEERS,
    NO_FEDERATION,
    PEERS_DIR_NAME,
    PIN_PATTERN,
    SCOPE_LOCAL,
    SCOPE_REMOTE,
    FederationError,
    FederationConfig,
    check_token_store,
    ensure_identity,
    format_host_port,
    peer_directories,
    plan_peers,
    read_identity,
    store_token,
    stored_token,
    unconfined_refusal,
)
from dromon import client
from dromon.agents import (
    OFF as AGENTS_OFF,
    AgentError,
    AgentPolicy,
    Gateway,
    LineTokens,
    new_token,
    token_digest,
)
from dromon.disclosure import DisclosureError, load_terms
from dromon.engine import ENGINE_NAME as CLAUDE_CODE_ENGINE
from dromon.engine import EngineRefused, check_plugin, resolve_plugin
from dromon.teacher import Teacher, TeacherError
from dromon.driver import (
    DOCUMENTLESS_ENGINES,
    ENGINE_OF,
    MODELS,
    DriverError,
    Strategy,
)
from dromon.eventlog import EventLog, LogError, read_entries, snapshot, utc_now
from dromon.limits import DEFAULT_LIMITS, Limits
from dromon.mailbox import MAX_PAGE, listing, outcome, replied_ids, sent, summary
from dromon.lineconfig import (
    DEFAULT_CONFIG,
    ConfigError,
    LineConfig,
    check_instructions,
    config_from_document,
    decode_config,
)
from dromon.projectconfig import (
    ConfigFileError,
    ProjectConfig,
    config_path,
    declared_operators,
    load,
)
from dromon.sandbox import SandboxConfig, SandboxError, effective, merge
from dromon import gitssh
from dromon.gitssh import GitSshAccess, GitSshError, sandbox_layer, unreadable
from dromon.processes import environment_reading_works
from dromon.roles import RoleConflict, RoleWanted, plan_file_roles, seat_order
from dromon.relay import AsyncRelay, OperatorRelayer, RelayConfigError, build_routes
from dromon.workdir import git_write_guards, read_head_commit, resolve_workdir
from dromon.configview import (
    OVERRIDE,
    LineFacts,
    Overrides,
    line_entries,
    project_entries,
    project_layers,
    project_values,
)
from dromon.overrides import (
    Layer,
    apply_limits,
    OverrideError,
    Scope,
    Value,
    changed,
    check,
    spec_of,
)
from dromon.overrides import decode as decode_override
from dromon.overrides import encode as encode_override
from dromon.model import (
    ID_PATTERN,
    DEFAULT_HANDOFF_BELOW_PERCENT,
    DEFAULT_HANDOFF_SECONDS,
    FailoverPolicy,
    PeerKeyed,
    PeerOpened,
    ProjectOverridden,
    format_due,
    AgentAccess,
    AgentAddress,
    AgentRegistered,
    AgentRevoked,
    Correlation,
    EventRejected,
    InvalidField,
    LineAddress,
    LineOverridden,
    Message,
    OperatorAddress,
    QuestionStatus,
    RemoteAddress,
    ReturnCode,
    State,
    parse_any_address,
)
from dromon import launcher
from dromon.paths import PathError, ProjectPaths, ensure_private_dir, project_paths
from dromon.forwarder import (
    Connector,
    Forwarder,
    announced_name,
    orphaned,
    registered_connector,
    sent_times,
)
from dromon.federation_tls import TLS_HANDSHAKE_TIMEOUT_SECONDS
from dromon.open_tls import (
    OpenTlsStream,
    RawReader,
    RawWriter,
    open_server_context,
)
from dromon.tls_link import (
    peer_connector,
    pinned_peer,
    presented_pin,
    server_context_for,
)
from dromon.peering import (
    HELLO_KEYS,
    HELLO_TIMEOUT_SECONDS,
    MAX_HELLO_LINE_BYTES,
    MAX_PEER_CONNECTIONS,
    Receiver,
    choose_version,
    identify,
    outcome_result,
)
from dromon.protocol import (
    MAX_LINE_BYTES,
    MAX_PEER_LINE_BYTES,
    PROTOCOL_VERSION,
    ErrorCode,
    Operation,
    ProtocolError,
    Request,
    Response,
    decode_request,
    encode_response,
    request_id_of,
)
from dromon.scheduler import (
    CORRELATION_ARGUMENT,
    DEFAULT_OPERATOR,
    DEFAULT_TICK_SECONDS,
    MAX_TICK_SECONDS,
    Scheduler,
    engine_factory,
)

VIEW_LIMIT: Final = 50
"""The most messages one ``line_view`` returns."""

VIEW_PREVIEW_CHARACTERS: Final = 600

VIEW_BUDGET_BYTES: Final = 40_000
"""Room for messages in a ``line_view`` response, below the 64 KiB line limit."""

REPLIES_BUDGET_BYTES: Final = 40_000
"""The most bytes, as written in JSON, of the entries and the echoed identifier that
one ``replies`` answer carries, fields and bodies alike, so that it fits one protocol
line of 64 KiB with the response's own fields and counts to spare."""

IDLE_TIMEOUT_SECONDS: Final = 30.0
"""A connection that sends nothing for this long is closed."""

SHUTDOWN_TIMEOUT_SECONDS: Final = 5.0


class AlreadyRunning(Exception):
    """Another daemon holds the project's lock."""


def acquire_lock(path: Path) -> int:
    """Take the exclusive lock at ``path`` and return its descriptor."""
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise AlreadyRunning(f"another daemon holds {path}") from error
    except BaseException:
        os.close(descriptor)
        raise
    os.ftruncate(descriptor, 0)
    os.write(descriptor, f"{os.getpid()}\n".encode())
    return descriptor


@dataclass(slots=True)
class Daemon:
    """The request handling of one daemon, separate from its sockets for testing."""

    paths: ProjectPaths
    log: EventLog
    started_at: str
    scheduler: Scheduler
    stop_requested: asyncio.Event = field(default_factory=asyncio.Event)
    agents: AgentPolicy = AGENTS_OFF
    """Who may connect to the agent socket, and with what access."""
    line_tokens: LineTokens = field(default_factory=LineTokens)
    """The tokens of lines whose engines reach their tools over the line socket."""
    project: ProjectConfig = field(default_factory=ProjectConfig)
    """The project file as last loaded, at start or by ``reload_project``."""
    command_line: Overrides = field(default_factory=Overrides)
    project_file_present: bool = False
    line_secrets: dict[str, tuple[str, ...]] = field(
        default_factory=dict[str, tuple[str, ...]], repr=False
    )
    hold_wakes: bool = False
    """Whether the command line held wakes, which a restart's successor keeps."""
    receiver: Receiver = field(default_factory=lambda: Receiver(time.monotonic))
    """The judge of peer deliveries, with each peer's rate limit in memory."""
    connect: Connector | None = None
    """How the forwarder reaches a peer, by default ``peer_connector``, which reaches
    each peer over its own transport. Tests give their own."""
    registry_dir: Path = field(
        default_factory=lambda: registry.configured_directory(os.environ)
    )
    """The per-user registry of open local federation, ``DROMON_REGISTRY_DIR`` when
    it is set. ``run`` gives the one it resolved at start, and tests give their own."""
    start_of: registry.StartOf = runs.process_start
    """When a pid started, by the process table, which tells a live registry entry
    from a stale one. Tests give their own."""
    successor: list[str] | None = field(default=None, init=False)
    """The options a successor starts with, once a restart is ready to stop."""
    relayer: OperatorRelayer | None = field(default=None, init=False)
    """The operators' relayer, once ``serve`` has made it."""
    _watches: set["asyncio.Task[None]"] = field(
        default_factory=lambda: set[asyncio.Task[None]](), init=False, repr=False
    )
    """The watch of a draining restart, kept so that it is not collected."""

    # Settings, at the project's scope and each line's.

    def project_layers(
        self, overrides: Mapping[str, Value] | None = None
    ) -> list[Layer]:
        """The project's layers, with the stored project overrides unless others
        are given."""
        if overrides is None:
            overrides, skipped = decode_override(
                Scope.PROJECT, self.log.state.project_overrides
            )
            for reason in skipped:
                print(
                    f"dromond: a project override is skipped: {reason}", file=sys.stderr
                )
        return project_layers(
            self.project, self.command_line, overrides, self.project_file_present
        )

    def apply_project_values(self) -> None:
        """Give the scheduler the project-wide values the layers resolve to. Stored
        overrides that no longer resolve are skipped, so they cannot stop a start.
        The scheduler is given the file too, for ``role_view``."""
        self.scheduler.set_project_file(self.project)
        try:
            values = project_values(self.project_layers())
        except (ValueError, InvalidField) as error:
            print(
                f"dromond: the project overrides are skipped: {error}", file=sys.stderr
            )
            values = project_values(self.project_layers({}))
        self.scheduler.set_project(
            values.limits, values.operator, values.tick_seconds, values.wake_every_ticks
        )

    def line_facts(self, line_id: str) -> LineFacts:
        scheduler = self.scheduler
        line = self.log.state.lines[line_id]
        own_limits, own_wake = scheduler.line_policy(line_id)
        return LineFacts(
            line_id=line_id,
            created_model=line.engine,
            model=scheduler.model_of(line_id),
            model_choices=scheduler.model_choices(line_id),
            strategy=line.strategy,
            session=line.engine_session,
            config=scheduler.effective_config(line_id),
            overrides=scheduler.line_overrides(line_id),
            limits=scheduler.limits_for(line_id),
            own_limits=own_limits,
            wake_every_ticks=scheduler.wake_every_for(line_id),
            own_wake=own_wake,
            rebound=line.rebound,
        )

    # The gateway's view of the daemon.

    @property
    def state(self) -> State:
        return self.log.state

    def append(self, event: AgentRegistered) -> None:
        self.log.append(event)

    def discloses(self, *texts: str) -> bool:
        return self.scheduler.discloses(*texts)

    def mail_tools(self, owner: AgentAddress) -> list[SdkMcpTool[Any]]:
        return [
            self.scheduler.send_tool(owner),
            self.scheduler.list_mail_tool(owner),
            self.scheduler.read_mail_tool(owner),
            self.scheduler.acknowledge_mail_tool(owner),
            self.scheduler.list_sent_tool(owner),
        ]

    def respond(self, line: bytes) -> Response:
        """Answer one request line. Never raises for anything a client can send."""
        try:
            request = decode_request(line)
        except ProtocolError as error:
            return Response(
                request_id_of(line),
                ok=False,
                error_code=error.code,
                error_message=str(error),
            )
        return self.execute(request)

    def line_tools(self, line_id: str) -> list[SdkMcpTool[Any]]:
        """A line's own tools, the same ones an in-process engine receives."""
        return self.scheduler.own_tools(line_id)

    async def respond_line(self, line: bytes) -> Response:
        """Answer one line on the line socket, for an engine reaching its line's own
        tools. Only the token the daemon issued for that line is accepted."""
        try:
            request = decode_request(line)
        except ProtocolError as error:
            return Response(
                request_id_of(line),
                ok=False,
                error_code=error.code,
                error_message=str(error),
            )
        if request.operation is Operation.PING:
            return Response(request.request_id, ok=True, result={})
        if request.operation not in (Operation.AGENT_TOOLS, Operation.AGENT_CALL):
            return Response(
                request.request_id,
                ok=False,
                error_code=ErrorCode.UNKNOWN_OPERATION,
                error_message="the line socket serves only agent_tools and agent_call",
            )
        args = request.args
        try:
            line_id = self.line_tokens.check(args.get("line"), args.get("token"))
        except AgentError as error:
            return Response(
                request.request_id,
                ok=False,
                error_code=ErrorCode.REJECTED,
                error_message=str(error),
            )
        if line_id not in self.log.state.lines:
            return Response(
                request.request_id,
                ok=False,
                error_code=ErrorCode.REJECTED,
                error_message=f"no line {line_id}",
            )
        tools = self.line_tools(line_id)
        if request.operation is Operation.AGENT_TOOLS:
            listed: list[dict[str, object]] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                }
                for tool in tools
            ]
            return Response(
                request.request_id,
                ok=True,
                result={"line": line_id, "tools": cast(object, listed)},
            )
        name = args.get("tool")
        arguments = args.get("arguments", {})
        tool = next((t for t in tools if t.name == name), None)
        if tool is None or not isinstance(arguments, dict):
            result: dict[str, object] = {
                "content": [{"type": "text", "text": f"no tool {name}"}],
                "is_error": True,
            }
        else:
            found = await tool.handler(cast(dict[str, Any], arguments))
            result = {
                "content": found.get("content", []),
                "is_error": bool(found.get("is_error", False)),
            }
        return Response(request.request_id, ok=True, result=_bounded(result))

    async def respond_agent(self, line: bytes) -> Response:
        """Answer one line on the agent socket, which serves only agent operations.
        Never raises for anything an agent can send."""
        try:
            request = decode_request(line)
        except ProtocolError as error:
            return Response(
                request_id_of(line),
                ok=False,
                error_code=error.code,
                error_message=str(error),
            )
        if request.operation is Operation.PING:
            return Response(request.request_id, ok=True, result={})
        if request.operation not in (Operation.AGENT_TOOLS, Operation.AGENT_CALL):
            return Response(
                request.request_id,
                ok=False,
                error_code=ErrorCode.UNKNOWN_OPERATION,
                error_message="the agent socket serves only agent_tools and agent_call",
            )
        gateway = Gateway(self.agents, self)
        args = request.args
        try:
            access = gateway.authenticate(args.get("agent"), args.get("token"))
        except (AgentError, InvalidField, EventRejected) as error:
            return Response(
                request.request_id,
                ok=False,
                error_code=ErrorCode.REJECTED,
                error_message=str(error),
            )
        agent_id = cast(str, args["agent"])
        if request.operation is Operation.AGENT_TOOLS:
            return Response(
                request.request_id,
                ok=True,
                result={
                    "agent": str(AgentAddress(agent_id)),
                    "access": access.value,
                    "tools": cast(object, gateway.tools(access)),
                },
            )
        result = await gateway.call(
            agent_id, access, args.get("tool"), args.get("arguments", {})
        )
        return Response(request.request_id, ok=True, result=_bounded(result))

    def respond_peer(self, session: "PeerSession", line: bytes) -> Response:
        """Answer one line on the peer socket, which serves only ``hello`` and
        ``deliver``. Never raises for anything a peer can send."""
        try:
            request = decode_request(line, MAX_PEER_LINE_BYTES)
        except ProtocolError as error:
            return _refusal(
                request_id_of(line, MAX_PEER_LINE_BYTES), error.code, str(error)
            )
        match request.operation:
            case Operation.PING:
                return Response(request.request_id, ok=True, result={})
            case Operation.HELLO:
                return self._hello(session, request)
            case Operation.DELIVER:
                return self._deliver(session, request)
            case _:
                return _refusal(
                    request.request_id,
                    ErrorCode.UNKNOWN_OPERATION,
                    "the peer socket serves only hello and deliver",
                )

    def _hello(self, session: "PeerSession", request: Request) -> Response:
        """Name the connection's peer by the token it presents on the socket, or over
        TLS by the pin of the key its handshake presented, when ``hello`` takes no
        token. The name it gives itself is advisory and never names it. An undeclared
        peer and a wrong token are refused alike."""
        args = request.args
        if session.peer_id is not None:
            return _refusal(
                request.request_id,
                ErrorCode.REJECTED,
                "hello was already given on this connection",
            )
        federation = self.project.federation
        keys = HELLO_KEYS - {"token"} if session.pin_sha256 else HELLO_KEYS
        # On the socket of an open daemon, a hello without a token names its peer by
        # the registry entry of the announced name, whose pid must be the caller's.
        registered = (
            not session.pin_sha256
            and federation.local_open
            and frozenset(args) == HELLO_KEYS - {"token"}
        )
        if registered:
            keys = HELLO_KEYS - {"token"}
        if frozenset(args) != keys:
            return _refusal(
                request.request_id,
                ErrorCode.BAD_REQUEST,
                f"hello takes exactly {sorted(keys)}",
            )
        name = args["name"]
        if not isinstance(name, str) or ID_PATTERN.fullmatch(name) is None:
            return _refusal(
                request.request_id, ErrorCode.BAD_REQUEST, "name is not an identifier"
            )
        version = choose_version(args["versions"])
        if version is None:
            return _refusal(
                request.request_id,
                ErrorCode.UNSUPPORTED_VERSION,
                "no federation version in common",
            )
        if session.pin_sha256:
            peer_id = self._pinned(session.pin_sha256, record=True)
        elif registered:
            peer_id = self._registered(session, name)
        else:
            peer_id = identify(self.log.state, args["token"])
        if peer_id is None:
            print(
                f"dromond: refused a peer's hello, announced as {name}",
                file=sys.stderr,
            )
            return _refusal(request.request_id, ErrorCode.REJECTED, PEER_REFUSED)
        session.peer_id = peer_id
        session.registered = registered
        session.token_sha256 = (
            "" if registered else self.log.state.peers[peer_id].token_sha256
        )
        return Response(request.request_id, ok=True, result={"version": version})

    def _pinned(self, pin: str, *, record: bool) -> str | None:
        """The peer the key of ``pin`` names. An inbound declared ``tls`` peer's
        pin names it. Otherwise, while ``remote`` is open, the pin names an open peer
        of its own, first recorded with ``peer_opened`` when ``record`` is true, unless
        a declared peer pins that key or bears that name, since a declared table always
        governs its own peer. A new key is refused once ``MAX_OPEN_REMOTE_PEERS``
        undeclared keys are recorded."""
        federation = self.project.federation
        state = self.log.state
        declared = pinned_peer(federation, state, pin)
        if declared is not None or not federation.remote_open:
            return declared
        if PIN_PATTERN.fullmatch(pin) is None or pin in federation.peers:
            return None
        if any(peer.pin_sha256 == pin for peer in federation.peers.values()):
            return None
        known = state.peers.get(pin)
        if known is not None and not known.removed:
            return pin if known.scope == SCOPE_REMOTE else None
        if not record:
            return None
        if len(federation.open_remote(state)) >= MAX_OPEN_REMOTE_PEERS:
            # Refused before anything is recorded, finding F1 of the round 12 review.
            print(
                f"dromond: refused a new remote key, since {MAX_OPEN_REMOTE_PEERS}"
                " undeclared keys are recorded already",
                file=sys.stderr,
            )
            return None
        self.log.append(PeerOpened(pin, SCOPE_REMOTE, format_due(datetime.now(UTC))))
        return pin

    def _registered(self, session: "PeerSession", name: str) -> str | None:
        """The peer that a hello without a token names, by the registry entry
        ``session.entry`` that ``PeerSession.respond`` found for the announced
        ``name`` and the caller's pid. An entry whose socket is a declared ``unix``
        peer's names that peer, whose own table governs. Any other entry names the
        open peer of its federation name, first recorded with ``peer_opened``, unless
        a declared peer has that name or the entry is this daemon's own."""
        entry = session.entry
        federation = self.project.federation
        state = self.log.state
        if entry is None or entry.name != name:
            return None
        if _same_path(entry.socket, self.paths.peer_socket):
            return None
        for peer_id, peer in sorted(federation.peers.items()):
            if peer.socket is not None and _same_path(peer.socket, entry.socket):
                known = state.peers.get(peer_id)
                return None if known is None or known.removed else peer_id
        if name in federation.peers or name == federation.name:
            return None
        known = state.peers.get(name)
        if known is not None and not known.removed:
            return name if known.scope == SCOPE_LOCAL else None
        self.log.append(PeerOpened(name, SCOPE_LOCAL, format_due(datetime.now(UTC))))
        return name

    def _deliver(self, session: "PeerSession", request: Request) -> Response:
        """Judge one delivery from the connection's peer. The peer's token, or over
        TLS its pin, is checked again, so a token revoked, a pin changed or a peer
        removed while connected delivers nothing more."""
        peer_id = session.peer_id
        if peer_id is None:
            return _refusal(
                request.request_id, ErrorCode.REJECTED, "deliver needs a hello first"
            )
        peer = self.log.state.peers.get(peer_id)
        if session.pin_sha256:
            named = self._pinned(session.pin_sha256, record=False)
            lost = named != peer_id
        elif session.registered:
            # The registry named the caller at hello. Its peer is lost when it is
            # removed, or when no policy governs it any more.
            lost = (
                peer is None
                or peer.removed
                or self.project.federation.definition(self.log.state, peer_id) is None
            )
        else:
            lost = (
                peer is None
                or peer.removed
                or peer.token_sha256 != session.token_sha256
            )
        if lost:
            session.peer_id = None
            session.token_sha256 = ""
            return _refusal(request.request_id, ErrorCode.REJECTED, PEER_REFUSED)
        try:
            outcome = self.receiver.deliver(
                self.log.state,
                self.project.federation,
                declared_operators(self.project),
                peer_id,
                request.args,
                self.scheduler.receive,
            )
        except EventRejected as error:
            # The receiver's checks run before the append, so this is a fault here,
            # reported as transient rather than as a refusal the peer would keep.
            print(
                f"dromond: a delivery from peer {peer_id} was not recorded: {error}",
                file=sys.stderr,
            )
            return _refusal(
                request.request_id,
                ErrorCode.INTERNAL,
                "the daemon could not record the message",
            )
        return Response(request.request_id, ok=True, result=outcome_result(outcome))

    def forwarder(self) -> Forwarder:
        """The forwarder for the project's peers, made once the log holds the peers
        of this start. Mail waiting for a peer no longer declared is returned with
        ``unknown_peer`` first. The send times of waiting mail are read from the log,
        so expiry survives a restart, and the log is read only when mail waits."""
        federation = self.project.federation
        for message_id in orphaned(self.log.state, federation):
            self.scheduler.return_mail(message_id, ReturnCode.UNKNOWN_PEER)
        waiting = {m for queue in self.log.state.outbox.values() for m in queue}
        times = (
            sent_times(read_entries(self.paths.event_log), waiting) if waiting else {}
        )
        store = self.paths.state_dir / PEERS_DIR_NAME

        async def locate(name: str) -> Path | None:
            entry = await asyncio.to_thread(
                registry.live_entry, self.registry_dir, name, self.start_of
            )
            return None if entry is None else entry.socket

        forwarder = Forwarder(
            self.scheduler,
            federation,
            registered_connector(
                (
                    peer_connector(self.paths.state_dir, store)
                    if self.connect is None
                    else self.connect
                ),
                locate,
            ),
            lambda peer_id: _outbound_token(store, peer_id),
            announced_name(self.paths.root, self.scheduler.discloses),
            time.monotonic,
            lambda: datetime.now(UTC),
            times,
        )
        self.scheduler.on_outbox = forwarder.notify
        self.scheduler.peer_reports = forwarder.reports
        return forwarder

    def relay_routes(self, config: ProjectConfig) -> dict[str, AsyncRelay]:
        """The relay of each operator ``config`` routes. Raises ``ProjectMismatch``
        for a relay refused now, including a notice file outside the daemon's
        protected state or runtime directories, so a start or reload names it and
        changes nothing."""
        protected_dirs(self.paths, config.federation, self.registry_dir)
        root = self.paths.root
        state_runtime_dirs = (self.paths.state_dir, self.paths.runtime_dir)

        try:
            return build_routes(config.relays, root, state_runtime_dirs)
        except RelayConfigError as error:
            raise ProjectMismatch(f"the project file is refused: {error}") from error

    def operator_relayer(self) -> OperatorRelayer:
        """The relayer for the project's operators, which the scheduler tells of each
        message to an operator. Made with no routes as well, so ``dromon status``
        reports the unrouted mail."""
        relayer = OperatorRelayer(
            self.scheduler, self.relay_routes(self.project), time.monotonic
        )
        self.scheduler.on_operator_mail = relayer.notify
        self.relayer = relayer
        return relayer

    def execute(self, request: Request) -> Response:
        """Answer one decoded request with operator authority."""
        match request.operation:
            case Operation.PING:
                return Response(request.request_id, ok=True, result={})
            case Operation.STATUS:
                key = request.args.get(CORRELATION_ARGUMENT)
                if key is None:
                    return Response(request.request_id, ok=True, result=self.status())
                if not isinstance(key, str) or ID_PATTERN.fullmatch(key) is None:
                    return _refusal(
                        request.request_id,
                        ErrorCode.BAD_REQUEST,
                        "correlation is not an identifier",
                    )
                return Response(
                    request.request_id,
                    ok=True,
                    result={
                        **self.status(),
                        "correlation": correlation_totals(self.log.state, key),
                    },
                )
            case Operation.STOP:
                lost = self.tasks_lost(sorted(self.log.state.lines))
                self.stop_requested.set()
                return Response(
                    request.request_id, ok=True, result={"stopping": True, **lost}
                )
            case Operation.AGENT_TOOLS | Operation.AGENT_CALL:
                return Response(
                    request.request_id,
                    ok=False,
                    error_code=ErrorCode.UNKNOWN_OPERATION,
                    error_message=f"{request.operation} is served on the agent socket",
                )
            case Operation.HELLO | Operation.DELIVER:
                return Response(
                    request.request_id,
                    ok=False,
                    error_code=ErrorCode.UNKNOWN_OPERATION,
                    error_message=f"{request.operation} is served on the peer socket",
                )
            case (
                Operation.CREATE_LINE
                | Operation.CONFIGURE_LINE
                | Operation.SEND
                | Operation.QUESTIONS
                | Operation.ANSWER
                | Operation.MAIL
                | Operation.REPLIES
                | Operation.READ
                | Operation.ACKNOWLEDGE
                | Operation.SENT
                | Operation.RELEASE
                | Operation.LINE_VIEW
                | Operation.LINE_ACTIVITY
                | Operation.INTERRUPT
                | Operation.HIBERNATE
                | Operation.AWAKEN
                | Operation.RESTART
                | Operation.SETTINGS
                | Operation.SET_SETTING
                | Operation.CLEAR_SESSION
                | Operation.REBIND_LINE
                | Operation.RELOAD_PROJECT
                | Operation.ROLES
                | Operation.ROLE_ENTRY
                | Operation.ROLE_TAKE
                | Operation.ROLE_VACATE
                | Operation.ROLE_HANDOFF
                | Operation.ROLE_CREATE
                | Operation.ROLE_UPDATE
                | Operation.ROLE_DELETE
                | Operation.RUN_CLEAR
                | Operation.BUDGETS
                | Operation.BUDGET_SET
                | Operation.AGENT_ADD
                | Operation.AGENT_REVOKE
                | Operation.AGENT_LIST
                | Operation.PEER_TOKEN
                | Operation.PEER_REVOKE
                | Operation.PEER_STORE
                | Operation.PEER_LIST
                | Operation.PEER_KEY
            ):
                return self._command(request)

    def _command(self, request: Request) -> Response:
        """Run a request that changes state, turning each refusal into an error."""
        try:
            match request.operation:
                case Operation.CREATE_LINE:
                    result = self._create_line(request.args)
                case Operation.CONFIGURE_LINE:
                    result = self._configure_line(request.args)
                case Operation.QUESTIONS:
                    result = self._questions(request.args)
                case Operation.ANSWER:
                    result = self._answer(request.args)
                case Operation.MAIL:
                    result = self._mail(request.args)
                case Operation.REPLIES:
                    result = self._replies(request.args)
                case Operation.READ:
                    result = self._read(request.args)
                case Operation.ACKNOWLEDGE:
                    result = self._acknowledge(request.args)
                case Operation.SENT:
                    result = self._sent(request.args)
                case Operation.RELEASE:
                    result = self._release(request.args)
                case Operation.LINE_VIEW:
                    result = self._line_view(request.args)
                case Operation.LINE_ACTIVITY:
                    result = self._line_activity(request.args)
                case Operation.INTERRUPT:
                    (line_id,) = _text_args(request.args, ("line_id",))
                    interrupted = self.scheduler.interrupt(line_id)
                    result = {"line_id": line_id, "interrupted": interrupted}
                case Operation.HIBERNATE:
                    (line_id,) = _text_args(request.args, ("line_id",))
                    changed = self.scheduler.hibernate(line_id)
                    result = {
                        "line_id": line_id,
                        "hibernated": True,
                        "changed": changed,
                        "running": self.scheduler.condition(line_id) == "running",
                    }
                case Operation.AWAKEN:
                    (line_id,) = _text_args(request.args, ("line_id",))
                    changed = self.scheduler.awaken(line_id)
                    result = {
                        "line_id": line_id,
                        "hibernated": False,
                        "changed": changed,
                    }
                case Operation.RESTART:
                    result = self._restart(request.args)
                case Operation.SETTINGS:
                    result = self._settings(request.args)
                case Operation.SET_SETTING:
                    result = self._set_setting(request.args)
                case Operation.CLEAR_SESSION:
                    (line_id,) = _text_args(request.args, ("line_id",))
                    lost = self.tasks_lost([line_id])
                    self.scheduler.clear_session(line_id)
                    result = {"line_id": line_id, **lost}
                case Operation.REBIND_LINE:
                    result = self._rebind_line(request.args)
                case Operation.RELOAD_PROJECT:
                    result = self._reload_project(request.args)
                case Operation.ROLES:
                    names = ("role_id",) if "role_id" in request.args else ()
                    values = _text_args(request.args, names)
                    roles = self.scheduler.roles_view(values[0] if values else None)
                    result = {"roles": cast(object, roles)}
                    if not values:
                        retired = self.scheduler.retired_roles_view()
                        result["retired"] = cast(object, retired)
                case Operation.ROLE_ENTRY:
                    (entry_id,) = _text_args(request.args, ("entry_id",))
                    result = self.scheduler.role_entry(entry_id)
                case Operation.ROLE_CREATE:
                    result = self._role_set(request.args, create=True)
                case Operation.ROLE_UPDATE:
                    result = self._role_set(request.args, create=False)
                case Operation.ROLE_DELETE:
                    role_id, operator = _text_args(
                        request.args, ("role_id", "operator")
                    )
                    result = self._role_delete(role_id, operator)
                case Operation.ROLE_TAKE:
                    role_id, seat, operator = _text_args(
                        request.args, ("role_id", "seat", "operator")
                    )
                    result = self._give(role_id, seat, operator)
                case Operation.ROLE_VACATE:
                    role_id, operator = _text_args(
                        request.args, ("role_id", "operator")
                    )
                    result = self._give(role_id, None, operator)
                case Operation.ROLE_HANDOFF:
                    role_id, to, operator = _text_args(
                        request.args, ("role_id", "to", "operator")
                    )
                    owner, message_id = self.scheduler.request_handoff(
                        OperatorAddress(operator), role_id, to
                    )
                    result = {
                        "role_id": role_id,
                        "owner": owner,
                        "to": to,
                        "message_id": message_id,
                        "owner_hibernated": self.log.state.lines[owner].hibernated,
                    }
                case Operation.RUN_CLEAR:
                    run_id, operator = _text_args(request.args, ("run_id", "operator"))
                    cleared = self.scheduler.clear_run(
                        OperatorAddress(operator), run_id
                    )
                    result = {
                        "run_id": run_id,
                        "operator": operator,
                        "cleared_at": cleared.cleared_at,
                    }
                case Operation.BUDGETS:
                    _text_args(request.args, ())
                    result = self.scheduler.budgets_view()
                case Operation.BUDGET_SET:
                    result = self._budget_set(request.args)
                case Operation.AGENT_ADD:
                    result = self._agent_add(request.args)
                case Operation.AGENT_REVOKE:
                    (agent_id,) = _text_args(request.args, ("agent",))
                    self.log.append(AgentRevoked(agent_id))
                    result: dict[str, object] = {"agent": str(AgentAddress(agent_id))}
                case Operation.AGENT_LIST:
                    result = self._agent_list(request.args)
                case Operation.PEER_TOKEN:
                    (peer_id,) = _text_args(request.args, ("peer",))
                    result = self._peer_token(peer_id)
                case Operation.PEER_REVOKE:
                    (peer_id,) = _text_args(request.args, ("peer",))
                    self.log.append(PeerKeyed(peer_id, ""))
                    result = {"peer": peer_id, "revoked": True}
                case Operation.PEER_STORE:
                    peer_id, token = _text_args(request.args, ("peer", "token"))
                    result = self._peer_store(peer_id, token)
                case Operation.PEER_LIST:
                    _text_args(request.args, ())
                    result = self._peer_list()
                case Operation.PEER_KEY:
                    _text_args(request.args, ())
                    result = self._peer_key()
                case _:
                    result = self._send(request.args)
        except ProtocolError as error:
            return Response(
                request.request_id,
                False,
                error_code=error.code,
                error_message=str(error),
            )
        except (
            InvalidField,
            EventRejected,
            restart.RestartRefused,
            DriverError,
            ConfigError,
            OverrideError,
            ConfigFileError,
            ProjectMismatch,
            SandboxError,
            DisclosureError,
            FederationError,
        ) as error:
            return Response(
                request.request_id,
                False,
                error_code=ErrorCode.REJECTED,
                error_message=str(error),
            )
        return Response(request.request_id, True, result=result)

    def _create_line(self, args: dict[str, object]) -> dict[str, object]:
        texts = {
            key: value for key, value in args.items() if key not in ("config", "limits")
        }
        names = ("line_id", "model", "strategy")
        if "engine_session" in texts:
            names += ("engine_session",)
        values = _text_args(texts, names)
        line_id, model, strategy = values[:3]
        session = values[3] if len(values) > 3 else None
        try:
            chosen = Strategy(strategy)
        except ValueError as error:
            raise DriverError(f"unknown strategy {strategy!r}") from error
        config = _config_arg(args) if "config" in args else DEFAULT_CONFIG
        self._check_workdir(config)
        check_sandbox_required(self.project, line_id, model, config)
        raw_limits = args.get("limits", {})
        if not isinstance(raw_limits, dict):
            raise OverrideError("limits must be an object")
        limits_document: dict[str, Value] = {}
        for key, value in cast(dict[str, object], raw_limits).items():
            if key not in ("max_autonomous_turns", "max_cost_usd", "cooldown_seconds"):
                raise OverrideError("unknown line limit key")
            full_key = "limits." + key
            limits_document[full_key] = check(spec_of(Scope.LINE, full_key), value)
        try:
            apply_limits(self.scheduler.limits, limits_document)
        except ValueError as error:
            raise OverrideError(str(error)) from error
        # Limits cannot change the engine or placement. Validate and encode the
        # entire override before creation, so no later override validation can
        # refuse a request whose line has already been recorded.
        initial_override = (
            LineOverridden(line_id, encode_override(limits_document))
            if limits_document
            else None
        )
        self.scheduler.create_line(line_id, model, chosen, config, session)
        if initial_override is not None:
            self.log.append(initial_override)
        return {"line_id": line_id}

    def _configure_line(self, args: dict[str, object]) -> dict[str, object]:
        texts = {key: value for key, value in args.items() if key != "config"}
        (line_id,) = _text_args(texts, ("line_id",))
        if "config" not in args:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "configure_line needs a config")
        if line_id not in self.log.state.lines:
            raise EventRejected(f"no line {line_id}")
        config = _config_arg(args)
        self._check_workdir(config)
        check_sandbox_required(
            self.project, line_id, self.scheduler.model_of(line_id), config
        )
        lost = self.tasks_lost([line_id])
        self.scheduler.configure_line(line_id, config)
        return {"line_id": line_id, **lost}

    def _rebind_line(self, args: dict[str, object]) -> dict[str, object]:
        """Move a line to another model and strategy, as only an operator may.

        Besides the scheduler's own checks, a line moving onto an engine other than
        Claude Code is refused while the project's own sandbox or settings would
        reach it, and a
        line moving onto Claude Code is refused when the project requires a sandbox
        it lacks, as at ``create_line``.
        """
        line_id, model, strategy = _text_args(args, ("line_id", "model", "strategy"))
        try:
            chosen = Strategy(strategy)
        except ValueError as error:
            raise DriverError(f"unknown strategy {strategy!r}") from error
        if line_id not in self.log.state.lines:
            raise EventRejected(f"no line {line_id}")
        engine = ENGINE_OF.get(model)
        own = self.project.sandbox is not None or self.project.settings is not None
        if engine in DOCUMENTLESS_ENGINES and own:
            raise ConfigError(
                f"the project's sandbox or settings would apply to line {line_id},"
                f" which on {model} runs on an engine that takes neither"
            )
        config = self.scheduler.effective_config(line_id)
        check_sandbox_required(self.project, line_id, model, config)
        lost = self.tasks_lost([line_id])
        self.scheduler.rebind_line(line_id, model, chosen)
        return {
            "line_id": line_id,
            "model": model,
            "strategy": chosen.value,
            **lost,
        }

    def _give(self, role_id: str, seat: str | None, operator: str) -> dict[str, object]:
        """An operator's take of ``role_id`` for ``seat``, or its vacating. The
        response warns of the background tasks the former owner's kept engine
        connection closes with, read before the change, as ``tasks_lost`` says."""
        role = self.log.state.roles.get(role_id)
        owner = None if role is None else role.owner
        lost = self.tasks_lost([owner]) if owner is not None else {}
        plan = self.scheduler.give_ownership(OperatorAddress(operator), role_id, seat)
        return {
            "role_id": role_id,
            "owner": plan.owner,
            "previous": plan.previous,
            **lost,
        }

    def _role_set(self, args: dict[str, object], *, create: bool) -> dict[str, object]:
        """An operator's ``role_create`` or ``role_update``, as ``role_wanted``
        reads its arguments. The response is the role as ``roles`` shows it, with the
        lines made seats and the seats unbound, and warns of the background tasks
        their kept engine connections close with, read before the change."""
        role_id, operator = _text_args(
            {key: args.get(key) for key in ("role_id", "operator")},
            ("role_id", "operator"),
        )
        wanted = role_wanted(self.log.state, role_id, args, create=create)
        current = {
            line_id
            for line_id, seat in self.log.state.seats.items()
            if seat.role_id == role_id
        }
        moving = sorted(current ^ {line_id for line_id, _ in wanted.seats})
        lost = self.tasks_lost(moving)
        change = self.scheduler.set_role(
            OperatorAddress(operator), role_id, wanted, create=create
        )
        return {
            "role": self.scheduler.role_view(role_id),
            "bound": list(change.bound),
            "unbound": list(change.unbound),
            **lost,
        }

    def _role_delete(self, role_id: str, operator: str) -> dict[str, object]:
        """An operator's ``role_delete``. The response names the seats unbound, which
        stay lines, and warns of the background tasks their kept engine connections
        close with, read before the change."""
        seats = sorted(
            line_id
            for line_id, seat in self.log.state.seats.items()
            if seat.role_id == role_id
        )
        lost = self.tasks_lost(seats)
        change = self.scheduler.retire_role(OperatorAddress(operator), role_id)
        retired = self.log.state.retired_roles[role_id]
        return {
            "role_id": role_id,
            "unbound": list(change.unbound),
            "retired_at": retired.retired_at,
            **lost,
        }

    def _budget_set(self, args: dict[str, object]) -> dict[str, object]:
        """An operator's statement of a provider's budget, with ``provider`` and
        optionally ``remaining_percent``, a whole number, ``resets_at``, a time in
        the model's due-time format, and ``note``, at least one of the first two."""
        allowed = {"provider", "remaining_percent", "resets_at", "note"}
        if "provider" not in args or not set(args) <= allowed:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST,
                f"budget_set takes {sorted(allowed)}, with provider",
            )
        texts = {
            key: args[key] for key in ("provider", "resets_at", "note") if key in args
        }
        names = tuple(sorted(texts))
        values = dict(zip(names, _text_args(texts, names), strict=True))
        percent = args.get("remaining_percent")
        if percent is not None and type(percent) is not int:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, "remaining_percent is not a whole number"
            )
        return self.scheduler.state_budget(
            values["provider"], percent, values.get("resets_at"), values.get("note", "")
        )

    def tasks_lost(self, line_ids: Sequence[str]) -> dict[str, object]:
        """What a response says of the background tasks an action closes with the
        lines' kept engine connections, nothing when there are none. The action is
        not blocked, only reported.
        Read before the action, and a task can end in between, so it is an upper
        bound."""
        lost = sum(self.scheduler.kept_tasks(line_id) for line_id in line_ids)
        if not lost:
            return {}
        return {
            "background_tasks_lost": lost,
            "warning": (
                f"{lost} background tasks were running in kept engine sessions and"
                " are stopped with them"
            ),
        }

    def _check_workdir(self, config: LineConfig) -> None:
        """Refuse a working directory that does not exist or is not allowed, a
        plugin that would not load, and instructions ``check_instructions`` refuses,
        before anything is recorded. Raises ``ConfigError``."""
        resolve_workdir(
            config.workdir,
            self.paths.root,
            protected_dirs(self.paths, self.project.federation, self.registry_dir),
        )
        check_plugins(config)
        check_instructions(config.instructions, self.paths.root)

    def _questions(self, args: dict[str, object]) -> dict[str, object]:
        """Open questions for ``operator``, or for every operator when it is omitted."""
        names = ("operator",) if "operator" in args else ()
        values = _text_args(args, names)
        operator = OperatorAddress(values[0]) if values else None
        found = self.scheduler.questions(operator)
        return {"questions": [_question_summary(i, m) for i, m in found]}

    def _answer(self, args: dict[str, object]) -> dict[str, object]:
        question_id, body, operator = _text_args(
            args, ("question_id", "body", "operator")
        )
        message_id = self.scheduler.answer(OperatorAddress(operator), question_id, body)
        return {"message_id": message_id}

    def _mail(self, args: dict[str, object]) -> dict[str, object]:
        """A page of an operator's mailbox, with its summary. Listing delivers.
        issues_only lists the open issues lines raised. Round 11, item 3."""
        allowed = {
            "operator",
            "unread_only",
            "sender",
            "issues_only",
            "offset",
            "limit",
        }
        if "operator" not in args or not set(args) <= allowed:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"mail takes {sorted(allowed)}, with operator"
            )
        (operator_text,) = _text_args({"operator": args["operator"]}, ("operator",))
        unread_only = args.get("unread_only", False)
        offset = args.get("offset", 0)
        limit = args.get("limit", MAX_PAGE)
        sender = args.get("sender")
        issues_only = args.get("issues_only", False)
        if type(unread_only) is not bool or type(issues_only) is not bool:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, "unread_only and issues_only are booleans"
            )
        if type(offset) is not int or type(limit) is not int:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "offset and limit are integers")
        if sender is not None and not isinstance(sender, str):
            raise ProtocolError(ErrorCode.BAD_REQUEST, "sender is not a string")
        owner = OperatorAddress(operator_text)
        self.scheduler.collect(owner)
        try:
            page, total = listing(
                self.log.state,
                owner,
                unread_only=unread_only,
                sender=None if sender is None else parse_any_address(sender),
                issues_only=issues_only,
                offset=offset,
                limit=limit,
            )
        except ValueError as error:
            raise InvalidField(str(error)) from error
        replied = replied_ids(self.log.state, owner)
        counts = summary(self.log.state, owner, 0)
        return {
            "total": total,
            **counts.counts(),
            "messages": [
                {
                    "message_id": message_id,
                    "sender": str(message.sender),
                    "kind": message.kind.value,
                    "reply_to": message.reply_to,
                    "read": message.read,
                    "outcome": outcome(message_id, message, replied),
                }
                for message_id, message in page
            ],
        }

    def _replies(self, args: dict[str, object]) -> dict[str, object]:
        """The replies in an operator's mailbox to one message, with their bodies,
        oldest first, marking none of them read.

        **Round 9, item 5.** ``read`` marks a message read, so a wait loop that
        counted unread replies could not also read them without changing its own
        count. This query answers both. It is not read-only in the log. It delivers
        the operator's undelivered mail first, as ``mail`` and ``collect`` do, since
        an operator's client showing it is its delivery, and so it appends
        ``message_delivered`` events. Delivery is not reading, and no message is
        marked read. Round 9 review, F11.

        **Round 9 review, F13.** Each entry as written, its fields as well as its
        body, and the echoed identifier are counted against
        ``REPLIES_BUDGET_BYTES``, so the answer fits one protocol line whatever its
        fields hold. A body past what is left is withheld, with its size, for
        ``read`` to open, and an entry whose fields alone do not fit is left out with
        every entry after it, counted in ``left_out``, for a later page to give."""
        allowed = {
            "operator",
            "message_id",
            "sender",
            "unread_only",
            "offset",
            "limit",
        }
        if not {"operator", "message_id"} <= set(args) or not set(args) <= allowed:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST,
                f"replies takes {sorted(allowed)}, with operator and message_id",
            )
        operator_text, message_id = _text_args(
            {"operator": args["operator"], "message_id": args["message_id"]},
            ("operator", "message_id"),
        )
        unread_only = args.get("unread_only", False)
        offset = args.get("offset", 0)
        limit = args.get("limit", MAX_PAGE)
        if type(unread_only) is not bool:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "unread_only is not a boolean")
        if type(offset) is not int or type(limit) is not int:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "offset and limit are integers")
        if offset < 0 or not 1 <= limit <= MAX_PAGE:
            raise InvalidField(
                f"offset must be at least 0 and limit in [1, {MAX_PAGE}]"
            )
        if message_id not in self.log.state.messages:
            # Round 9 review, F12, accepted by the operator. This tells any operator
            # who holds the socket whether an identifier exists. Identifiers are
            # random, so the signal is low, and the socket is the operator's own.
            raise EventRejected(f"no message {message_id}")
        owner = OperatorAddress(operator_text)
        sender_text = args.get("sender")
        sender: LineAddress | None = None
        if sender_text is not None:
            if not isinstance(sender_text, str):
                raise ProtocolError(ErrorCode.BAD_REQUEST, "sender is not text")
            address = parse_any_address(sender_text)
            if not isinstance(address, LineAddress):
                raise ProtocolError(ErrorCode.BAD_REQUEST, "sender must name a line")
            sender = address
        self.scheduler.collect(owner)
        found = [
            (reply_id, message)
            for reply_id, message in self.log.state.messages.items()
            if message.recipient == owner
            and message.reply_to == message_id
            and (sender is None or message.sender == sender)
        ]
        unread = sum(not message.read for _, message in found)
        matching = [item for item in found if not unread_only or not item[1].read]
        replied = replied_ids(self.log.state, owner)
        entries: list[dict[str, object]] = []
        left_out = 0
        budget = REPLIES_BUDGET_BYTES - _written(message_id)
        for reply_id, message in matching[offset : offset + limit]:
            entry: dict[str, object] = {
                "message_id": reply_id,
                "sender": str(message.sender),
                "kind": message.kind.value,
                "read": message.read,
                "outcome": outcome(reply_id, message, replied),
                "body": None,
                "body_bytes": len(message.body.encode("utf-8")),
            }
            # The comma that separates it from the entry before.
            fields = _written(entry) + 1
            if left_out or fields > budget:
                # Entries stay in order, so once one is left out so is every later
                # one, and the next page begins at the first left out.
                left_out += 1
                continue
            budget -= fields
            # Measured as written, since escaping can enlarge a body sixfold, and in
            # place of the ``null`` already counted.
            body = _written(message.body) - _written(None)
            if body <= budget:
                entry["body"] = message.body
                budget -= body
            entries.append(entry)
        return {
            "message_id": message_id,
            "total": len(matching),
            "replies": len(found),
            "unread": unread,
            "left_out": left_out,
            "messages": entries,
        }

    def _line_view(self, args: dict[str, object]) -> dict[str, object]:
        """What an attached client shows: the line, its recent mail and questions.

        Bodies are previews, and the newest messages are kept within a byte budget,
        so the response always fits one protocol line however large the mail is.
        """
        allowed = {"line_id", "limit"}
        if "line_id" not in args or not set(args) <= allowed:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"line_view takes {sorted(allowed)}"
            )
        (line_id,) = _text_args({"line_id": args["line_id"]}, ("line_id",))
        limit = args.get("limit", VIEW_LIMIT)
        if type(limit) is not int or not 1 <= limit <= VIEW_LIMIT:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"limit must be in [1, {VIEW_LIMIT}]"
            )
        state = self.log.state
        if line_id not in state.lines:
            raise EventRejected(f"no line {line_id}")
        address = LineAddress(line_id)
        involved = [
            (message_id, message)
            for message_id, message in state.messages.items()
            if address in (message.sender, message.recipient)
        ]
        summaries = {
            str(entry["line_id"]): entry for entry in self.scheduler.line_summaries()
        }
        messages: list[dict[str, object]] = []
        budget = VIEW_BUDGET_BYTES
        for message_id, message in reversed(involved[-limit:]):
            preview = message.body[:VIEW_PREVIEW_CHARACTERS]
            entry: dict[str, object] = {
                "message_id": message_id,
                "sender": str(message.sender),
                "recipient": str(message.recipient),
                "kind": message.kind.value,
                "reply_to": message.reply_to,
                "status": None if message.status is None else message.status.value,
                "delivered": message.delivered,
                "read": message.read,
                "body": preview,
                "truncated": len(preview) < len(message.body),
            }
            budget -= len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))
            if budget < 0:
                break
            messages.append(entry)
        messages.reverse()
        questions = [
            _question_summary(message_id, message)
            for message_id, message in involved
            if message.status is QuestionStatus.OPEN and message.sender == address
        ]
        return {
            "line": dict(summaries[line_id]),
            "tick": self.scheduler.tick,
            "messages": messages,
            "total": len(involved),
            "open_questions": questions[-VIEW_LIMIT:],
        }

    def _line_activity(self, args: dict[str, object]) -> dict[str, object]:
        """The entries of a line's activity changed after revision ``after``.

        The newest entries are kept within a byte budget, so the response fits one
        protocol line. ``complete`` is false when older changed entries were left
        out, which a client shows rather than fetching them, because they may be
        large and are already past.
        """
        allowed = {"line_id", "after"}
        if "line_id" not in args or not set(args) <= allowed:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"line_activity takes {sorted(allowed)}"
            )
        (line_id,) = _text_args({"line_id": args["line_id"]}, ("line_id",))
        after = args.get("after", 0)
        if type(after) is not int or after < 0:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, "after is a non-negative integer"
            )
        if line_id not in self.log.state.lines:
            raise EventRejected(f"no line {line_id}")
        changed = self.scheduler.activity.after(line_id, after)
        kept: list[dict[str, object]] = []
        budget = VIEW_BUDGET_BYTES
        for entry in reversed(changed):
            document = entry.to_json()
            budget -= len(json.dumps(document, ensure_ascii=False).encode("utf-8"))
            if budget < 0:
                break
            kept.append(document)
        kept.reverse()
        return {
            "line_id": line_id,
            "seq": self.scheduler.activity.seq,
            "entries": kept,
            "complete": len(kept) == len(changed),
        }

    def _settings(self, args: dict[str, object]) -> dict[str, object]:
        """The project's settings, and a line's when ``line_id`` is given, each with
        the layer its value comes from."""
        allowed = {"line_id"}
        if not set(args) <= allowed:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "settings takes only line_id")
        layers = self.project_layers()
        file = str(config_path(self.paths.state_dir))
        result: dict[str, object] = {
            "project": project_entries(
                layers, self.project, file, self.project_file_present
            ),
            "models": list(MODELS),
        }
        if "line_id" in args:
            (line_id,) = _text_args(args, ("line_id",))
            if line_id not in self.log.state.lines:
                raise EventRejected(f"no line {line_id}")
            result["line_id"] = line_id
            result["line"] = line_entries(self.line_facts(line_id), layers)
        return result

    def _set_setting(self, args: dict[str, object]) -> dict[str, object]:
        """Set one setting at the project's scope or a line's, or return it to the
        layers below when ``value`` is omitted. Returns where the value now comes
        from, which is not the override when the command line shadows it."""
        scope_text = args.get("scope")
        allowed = {"scope", "key", "value", "line_id"}
        if not set(args) <= allowed or not isinstance(args.get("key"), str):
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"set_setting takes {sorted(allowed)}, with key"
            )
        key = cast(str, args["key"])
        value = args.get("value")
        if scope_text == Scope.LINE.value:
            (line_id,) = _text_args({"line_id": args.get("line_id")}, ("line_id",))
            lost = self.tasks_lost([line_id])
            self.scheduler.override_line(line_id, key, value)
            found = next(
                e
                for e in line_entries(self.line_facts(line_id), self.project_layers())
                if e["key"] == key
            )
            landed = _landed(found, value is not None)
            return {"scope": "line", "line_id": line_id, **landed, **lost}
        if scope_text != Scope.PROJECT.value or "line_id" in args:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, "scope is line, with line_id, or project"
            )
        spec = spec_of(Scope.PROJECT, key)
        stored, _ = decode_override(Scope.PROJECT, self.log.state.project_overrides)
        document = changed(stored, key, None if value is None else check(spec, value))
        try:
            project_values(self.project_layers(document))
        except (ValueError, InvalidField) as error:
            raise OverrideError(f"{key}: {error}") from error
        self.log.append(ProjectOverridden(encode_override(document)))
        self.apply_project_values()
        found = next(
            e
            for e in project_entries(
                self.project_layers(), self.project, "", self.project_file_present
            )
            if e["key"] == key
        )
        return {"scope": "project", **_landed(found, value is not None)}

    def _reload_project(self, args: dict[str, object]) -> dict[str, object]:
        """Read the project file again and apply its lines, limits and wake policy.

        A change to the sandbox, the engine settings, the teacher, the external
        agent policy, the federation, the ``[git_ssh]`` table or the lines it
        reaches is refused, because the running engine factory, teacher and agent
        socket were built from them, and the daemon must restart to apply it.
        A refused reload changes nothing.
        """
        if args:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "reload_project takes nothing")
        file = config_path(self.paths.state_dir)
        present = file.exists()
        config = load(file, self.paths.root) if present else ProjectConfig()
        check_sandboxes(config, self.paths)
        try:
            check_engines(config)
        except ValueError as error:
            raise OverrideError(str(error)) from error
        fixed = ("sandbox", "settings", "teacher", "agents", "federation", "git_ssh")
        moved = [
            name
            for name in fixed
            if getattr(config, name) != getattr(self.project, name)
        ]
        if git_ssh_lines(config) != git_ssh_lines(self.project):
            moved.append("the lines given git_ssh")
        if moved:
            raise OverrideError(
                "the project file changes "
                + ", ".join(moved)
                + ", which apply only when the daemon restarts"
            )
        try:
            project_values(project_layers(config, self.command_line, {}, present))
        except (ValueError, InvalidField) as error:
            raise OverrideError(str(error)) from error
        check_project(self.log, config)
        check_roles(self.scheduler, self.log, config)
        routes = self.relay_routes(config)
        terms = project_terms(config, self.paths)
        check_lines_sandboxed(config, self.log, self.scheduler)
        for line_id in sorted(set(self.project.lines) - set(config.lines)):
            if line_id in self.log.state.lines:
                self.scheduler.set_line_policy(line_id, None, None)
        apply_project(self.scheduler, self.log, config)
        self.line_secrets.clear()
        self.line_secrets.update(
            {
                line_id: definition.secret_names
                for line_id, definition in config.lines.items()
            }
        )
        self.scheduler.set_disclosure_terms(terms)
        self.scheduler.set_disclosure_accepted(config.disclosure_accepted)
        self.scheduler.set_leases(config.lease_names, config.lease_ttl_seconds)
        self.scheduler.set_runs(config.run_commands)
        self.project = config
        self.project_file_present = present
        self.apply_project_values()
        notices = apply_roles(self.scheduler, self.log, config)
        if self.relayer is not None:
            self.relayer.set_routes(routes)
        return {
            "file": str(file),
            "present": present,
            "lines": sorted(config.lines),
            "roles": sorted(config.roles),
            "role_notices": list(notices),
        }

    def _agent_add(self, args: dict[str, object]) -> dict[str, object]:
        """Register an agent, or replace its access and token, and return the token.
        The token is returned once and only its digest is stored."""
        agent_id, access_text = _text_args(args, ("agent", "access"))
        try:
            access = AgentAccess(access_text)
        except ValueError as error:
            raise InvalidField(f"unknown access {access_text!r}") from error
        token = new_token()
        self.log.append(AgentRegistered(agent_id, access, token_digest(token)))
        effective = (
            access
            if access.rank <= self.agents.max_access.rank
            else (self.agents.max_access)
        )
        return {
            "agent": str(AgentAddress(agent_id)),
            "access": access.value,
            "effective_access": effective.value,
            "token": token,
            "listening": self.agents.listen,
        }

    def _peer_token(self, peer_id: str) -> dict[str, object]:
        """Issue the token the peer presents to this daemon, replacing any earlier one,
        and return it. The token is returned once and only its digest is stored, as for
        an agent. The peer must be declared in the project file."""
        token = new_token()
        self.log.append(PeerKeyed(peer_id, token_digest(token)))
        return {"peer": peer_id, "token": token}

    def _peer_store(self, peer_id: str, token: str) -> dict[str, object]:
        """Store the token the peer issued for this daemon under ``.dromon/peers``.
        The peer must be declared in the log, so a mistyped name stores nothing."""
        known = self.log.state.peers.get(peer_id)
        if known is None or known.removed:
            raise FederationError(f"no peer {peer_id}")
        path = store_token(self.paths.state_dir / PEERS_DIR_NAME, peer_id, token)
        return {"peer": peer_id, "stored": str(path)}

    def _peer_key(self) -> dict[str, object]:
        """This daemon's TLS identity, made once under ``.dromon/peers``, with its pin
        and the path of its public certificate, which the operator gives the peer's
        operator with the pin. The private key never leaves the store."""
        store = self.paths.state_dir / PEERS_DIR_NAME
        identity, made = ensure_identity(store)
        return {
            "pin_sha256": identity.pin_sha256,
            "certificate": str(identity.certificate),
            "made": made,
        }

    def _peer_list(self) -> dict[str, object]:
        """The declared peers, with each one's directions and whether a token is issued
        for it and stored from it. Never a token or a digest."""
        store = self.paths.state_dir / PEERS_DIR_NAME
        peers: list[object] = []
        for peer_id, definition in sorted(self.project.federation.peers.items()):
            known = self.log.state.peers.get(peer_id)
            try:
                stored: object = stored_token(store, peer_id) is not None
            except FederationError as error:
                stored = str(error)
            peers.append(
                {
                    "peer": peer_id,
                    "transport": definition.transport,
                    "socket": None
                    if definition.socket is None
                    else str(definition.socket),
                    "address": None
                    if definition.address is None
                    else format_host_port(definition.address),
                    "inbound": definition.inbound,
                    "outbound": definition.outbound,
                    "issued": known is not None and bool(known.token_sha256),
                    "stored": stored,
                }
            )
        return {"listen": self.project.federation.listen, "peers": peers}

    def _agent_list(self, args: dict[str, object]) -> dict[str, object]:
        _text_args(args, ())
        return {
            "policy": {
                "listen": self.agents.listen,
                "anonymous": self.agents.anonymous.value,
                "max_access": self.agents.max_access.value,
            },
            "agents": [
                {
                    "agent": str(AgentAddress(agent_id)),
                    "access": agent.access.value,
                    "token": bool(agent.token_sha256),
                    "revoked": agent.revoked,
                }
                for agent_id, agent in sorted(self.log.state.agents.items())
            ],
        }

    def _release(self, args: dict[str, object]) -> dict[str, object]:
        (line_id,) = _text_args(args, ("line_id",))
        self.scheduler.release(line_id)
        return {"line_id": line_id}

    def _acknowledge(self, args: dict[str, object]) -> dict[str, object]:
        operator, message_id = _text_args(args, ("operator", "message_id"))
        self.scheduler.acknowledge(OperatorAddress(operator), message_id)
        return {"message_id": message_id}

    def _sent(self, args: dict[str, object]) -> dict[str, object]:
        """A page of the mail an operator has sent, with each message's fate."""
        allowed = {"operator", "unanswered_only", "offset", "limit"}
        if "operator" not in args or not set(args) <= allowed:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"sent takes {sorted(allowed)}, with operator"
            )
        (operator_text,) = _text_args({"operator": args["operator"]}, ("operator",))
        unanswered_only = args.get("unanswered_only", False)
        offset = args.get("offset", 0)
        limit = args.get("limit", MAX_PAGE)
        if type(unanswered_only) is not bool:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, "unanswered_only is not a boolean"
            )
        if type(offset) is not int or type(limit) is not int:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "offset and limit are integers")
        try:
            page, total = sent(
                self.log.state,
                OperatorAddress(operator_text),
                unanswered_only=unanswered_only,
                offset=offset,
                limit=limit,
            )
        except ValueError as error:
            raise InvalidField(str(error)) from error
        return {
            "total": total,
            "messages": [
                {
                    "message_id": status.message_id,
                    "recipient": status.recipient,
                    "kind": status.kind,
                    "delivered": status.delivered,
                    "read": status.read,
                    "outcome": status.outcome,
                    "delivery": status.delivery,
                }
                for status in page
            ],
        }

    def _read(self, args: dict[str, object]) -> dict[str, object]:
        operator, message_id = _text_args(args, ("operator", "message_id"))
        message = self.scheduler.read(OperatorAddress(operator), message_id)
        return {
            "message_id": message_id,
            "sender": str(message.sender),
            "recipient": str(message.recipient),
            "kind": message.kind.value,
            "reply_to": message.reply_to,
            "choices": list(message.choices),
            "body": message.body,
        }

    def _send(self, args: dict[str, object]) -> dict[str, object]:
        """Record an operator's note. ``priority``, optional and default false, makes
        it priority mail, which only a line may receive, and which for an operator
        is unlimited."""
        urgent = args.get("priority", False)
        if not isinstance(urgent, bool):
            raise ProtocolError(ErrorCode.BAD_REQUEST, "priority is not a boolean")
        texts = {key: value for key, value in args.items() if key != "priority"}
        optional = ("reply_to",) if "reply_to" in texts else ()
        values = _text_args(texts, ("recipient", "body", "operator", *optional))
        recipient, body, operator = values[:3]
        reply_to = values[3] if optional else None
        sender = OperatorAddress(operator)
        address = parse_any_address(recipient)
        if urgent and not isinstance(address, RemoteAddress):
            # Checked before the operator is registered, so a refusal records nothing.
            self.scheduler.check_priority(sender, address)
        self.scheduler.ensure_operator(sender.operator_id)
        message_id = self.scheduler.send(
            sender, address, body, reply_to, priority=urgent
        )
        return {"message_id": message_id, "priority": urgent}

    def _restart(self, args: dict[str, object]) -> dict[str, object]:
        """Begin a restart for ``operator``. ``mode`` is ``wait``, the default, or
        ``interrupt``, ``wait_seconds`` bounds the drain, and ``end_tasks``, default
        false, lets it stop background tasks of kept sessions. Needs the daemon's
        event loop, on which the watch runs."""
        unknown = sorted(set(args) - {"operator", "mode", "wait_seconds", "end_tasks"})
        if unknown:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"restart takes no {', '.join(unknown)}"
            )
        texts = {key: args[key] for key in ("operator", "mode") if key in args}
        names = ("operator", "mode") if "mode" in texts else ("operator",)
        values = _text_args(texts, names)
        operator = OperatorAddress(values[0]).operator_id
        try:
            mode = restart.Mode(values[1] if len(values) > 1 else restart.Mode.WAIT)
        except ValueError as error:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, "mode is not wait or interrupt"
            ) from error
        end_tasks = args.get("end_tasks", False)
        if not isinstance(end_tasks, bool):
            raise ProtocolError(ErrorCode.BAD_REQUEST, "end_tasks is not a boolean")
        try:
            wait = restart.check_wait(
                args.get("wait_seconds", restart.DEFAULT_WAIT_SECONDS)
            )
        except restart.RestartRefused as error:
            raise ProtocolError(ErrorCode.BAD_REQUEST, str(error)) from error
        loop = asyncio.get_running_loop()
        lost = self.tasks_lost(sorted(self.log.state.lines)) if end_tasks else {}
        running = self.scheduler.begin_restart(
            mode, operator, wait, end_tasks=end_tasks
        )
        watch = loop.create_task(self._watch_restart(wait))
        self._watches.add(watch)
        watch.add_done_callback(self._watches.discard)
        return {
            "restarting": True,
            "mode": mode.value,
            "wait_seconds": wait,
            "running": cast(object, running),
            "pid": os.getpid(),
            "commit": self.scheduler.daemon_status()["commit"],
            **lost,
        }

    async def _watch_restart(self, wait_seconds: int) -> None:
        """Stop the daemon for its successor once the draining restart is ready, or
        return once it is abandoned. Bounded by ``restart.polls``."""
        for _ in range(restart.polls(wait_seconds)):
            outcome = self.scheduler.restart_progress()
            if outcome is restart.Outcome.READY:
                self.successor = successor_args(self.command_line, self.hold_wakes)
                print("dromond: stopping for a restart", file=sys.stderr)
                self.stop_requested.set()
                return
            if outcome is restart.Outcome.ABANDONED:
                return
            await asyncio.sleep(restart.POLL_SECONDS)
        self.scheduler.abandon_restart(
            "its watch made every poll its wait allows without passing the deadline"
        )

    def status(self) -> dict[str, object]:
        state = self.log.state
        open_questions = sum(
            1 for m in state.messages.values() if m.status is QuestionStatus.OPEN
        )
        return {
            "daemon_version": __version__,
            "protocol_version": PROTOCOL_VERSION,
            "pid": os.getpid(),
            "project": str(self.paths.root),
            "started_at": self.started_at,
            "options": successor_args(self.command_line, self.hold_wakes),
            "event_log": {
                "last_seq": self.log.last_seq,
                "lines": len(state.lines),
                "operators": len(state.operators),
                "open_questions": open_questions,
                **self.scheduler.event_log_status(),
            },
            "tick": self.scheduler.tick,
            "daemon": self.scheduler.daemon_status(),
            "restart": self.scheduler.restart_status(),
            "lines": self.scheduler.line_summaries(),
            "scheduler": self.scheduler.fault_status(),
            "peers": [
                report.wire(detail=True) for report in self.scheduler.peer_reports()
            ],
            "relays": None if self.relayer is None else self.relayer.report(),
            "agents": {
                "listen": self.agents.listen,
                "anonymous": self.agents.anonymous.value,
                "max_access": self.agents.max_access.value,
                "registered": sum(
                    1 for agent in state.agents.values() if not agent.revoked
                ),
            },
        }


MAX_AGENT_TEXT: Final = 48 * 1024
"""The most text one agent result carries, so it fits one protocol line."""


PEER_REFUSED: Final = "the peer was refused"
"""The one refusal of an unknown peer, a wrong token and a revoked one, so a caller
cannot tell which peer names are declared."""


@dataclass(slots=True)
class PeerSession:
    """One connection on the peer socket, and the peer its ``hello`` named."""

    daemon: Daemon
    peer_id: str | None = None
    token_sha256: str = ""
    """The digest the peer's token matched at ``hello``, checked again at each
    ``deliver``."""
    pin_sha256: str = ""
    """Over TLS, the pin of the key the peer presented in its handshake, which names
    it at ``hello`` and again at each ``deliver``. Empty on the socket."""
    peer_pid: int | None = None
    """On the socket, the pid of the connecting process, which open local federation
    matches against the registry, or ``None`` when it could not be read."""
    peer_started: str | None = None
    """The start time of ``peer_pid`` by the process table, read once at accept by
    ``read_peer_start``, or ``None`` when it was not or could not be read, which
    refuses a hello without a token."""
    entry: registry.Entry | None = None
    """The live registry entry of the name a hello without a token announced, whose
    pid is ``peer_pid``, found before ``hello`` is judged."""
    registered: bool = False
    """Whether ``hello`` named the peer by the registry rather than a token or pin."""

    async def respond(self, line: bytes) -> Response:
        if self.peer_id is None and self.peer_pid is not None:
            await self._find_entry(line)
        return self.daemon.respond_peer(self, line)

    async def read_peer_start(self) -> None:
        """Read the start time of ``peer_pid`` at accept, in a worker thread, since it
        reads the process table, and only while ``local`` is open. A table that
        cannot be read leaves it unread, so the registry names no caller."""
        self.peer_started = None
        pid = self.peer_pid
        if pid is None or not self.daemon.project.federation.local_open:
            return
        try:
            self.peer_started = await asyncio.to_thread(self.daemon.start_of, pid)
        except Exception as error:  # noqa: BLE001, any failure refuses the caller
            print(
                f"dromond: the start time of pid {pid} could not be read: {error}",
                file=sys.stderr,
            )

    async def _find_entry(self, line: bytes) -> None:
        """Find the registry entry a hello without a token claims, in a worker
        thread, since telling a live entry from a stale one reads the process table.
        Only an open daemon looks, and only for a well-formed name."""
        self.entry = None
        if not self.daemon.project.federation.local_open:
            return
        try:
            document = json.loads(line)
        except ValueError:
            return
        if not isinstance(document, dict):
            return
        args = cast(dict[str, object], document).get("args")
        if not isinstance(args, dict):
            return
        name = cast(dict[str, object], args).get("name")
        if not isinstance(name, str) or ID_PATTERN.fullmatch(name) is None:
            return
        if "token" in cast(dict[str, object], args):
            return
        pid = self.peer_pid
        started = self.peer_started
        if pid is None or started is None:
            return
        self.entry = await asyncio.to_thread(
            registry.find,
            self.daemon.registry_dir,
            name,
            pid,
            started,
            self.daemon.start_of,
        )


def correlation_totals(state: State, key: str) -> dict[str, object]:
    """The totals of one correlation key as ``status`` reports them. The cost is a
    decimal text of whole millionths of a dollar, so no float rounds it. A key with
    no recorded turn, or one dropped as least recently used, reports ``recorded``
    false and zero totals."""
    total = state.correlations.get(key)
    found = total if total is not None else Correlation()
    micro = found.cost_micro_usd
    return {
        "key": key,
        "recorded": total is not None,
        "turns": found.turns,
        "shared_turns": found.shared_turns,
        "cost_usd": f"{micro // 1_000_000}.{micro % 1_000_000:06d}",
        "unknown_cost_turns": found.unknown_cost_turns,
    }


def _refusal(request_id: str, code: ErrorCode, message: str) -> Response:
    return Response(request_id, ok=False, error_code=code, error_message=message)


def _bounded(result: dict[str, object]) -> dict[str, object]:
    """``result`` with each text item cut to ``MAX_AGENT_TEXT`` characters, noting the
    cut, since a message body may be larger than a protocol line."""
    content = cast(list[dict[str, object]], result.get("content", []))
    bounded: list[dict[str, object]] = []
    for item in content:
        text = item.get("text")
        if isinstance(text, str) and len(text) > MAX_AGENT_TEXT:
            item = {**item, "text": text[:MAX_AGENT_TEXT] + "\n[truncated by Dromon]"}
        bounded.append(item)
    return {**result, "content": cast(object, bounded)}


def _landed(found: Mapping[str, object], was_set: bool) -> dict[str, object]:
    """The parts of a settings entry a ``set_setting`` result reports. A value that
    was set but does not come from the override is shadowed by a higher layer."""
    return {
        "key": found["key"],
        "value": found["value"],
        "source": found["source"],
        "shadowed": was_set and found["source"] != OVERRIDE,
    }


def _written(value: object) -> int:
    """The bytes ``value`` takes in a protocol line, which is written compactly and
    without escaping beyond JSON's own, as ``dromon.protocol`` writes it."""
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _question_summary(message_id: str, message: Message) -> dict[str, object]:
    return {
        "question_id": message_id,
        "line": str(message.sender),
        "operator": str(message.recipient),
        "text": message.body,
        "choices": list(message.choices),
    }


def _config_arg(args: dict[str, object]) -> LineConfig:
    value = args["config"]
    if not isinstance(value, dict):
        raise ProtocolError(ErrorCode.BAD_REQUEST, "config is not an object")
    return config_from_document(cast(dict[str, object], value))


ROLE_CREATE_ARGS: Final = frozenset(
    {
        "role_id",
        "operator",
        "workdir",
        "seats",
        "no_own",
        "handoff_seconds",
        "failover",
        "handoff_below_percent",
    }
)
"""What ``role_create`` takes. ``role_id``, ``operator``, ``workdir`` and ``seats``
are required."""

ROLE_UPDATE_ARGS: Final = ROLE_CREATE_ARGS | {"may_own"}
"""What ``role_update`` takes. Only ``role_id`` and ``operator`` are required, and
any other argument given changes that term."""


def _names_arg(args: dict[str, object], name: str) -> tuple[str, ...]:
    """The array of strings ``name`` of ``args``, empty when absent, or
    ``ProtocolError``. Bounded by the request's line limit."""
    value = args.get(name, [])
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in cast(list[object], value)
    ):
        raise ProtocolError(ErrorCode.BAD_REQUEST, f"{name} is not a list of strings")
    return tuple(cast(list[str], value))


def role_wanted(
    state: State, role_id: str, args: dict[str, object], *, create: bool
) -> RoleWanted:
    """The terms ``role_create`` or ``role_update`` asks for, or ``ProtocolError``.

    A create gives the worktree and the seats in failover order, and may give the
    other terms ``role_declared`` holds, which default as the project file's do. An
    update starts from the role as it stands. ``seats``, when given, replaces the
    seats and the order, so it adds, removes and reorders them, and a seat it keeps
    keeps its ``may_own``. ``no_own`` and ``may_own`` name seats that may not, or may,
    own the role. Whether the change applies is ``roles.plan_role_set``'s."""
    allowed = ROLE_CREATE_ARGS if create else ROLE_UPDATE_ARGS
    required = {"role_id", "operator"}
    if create:
        required |= {"workdir", "seats"}
    if not required <= set(args) <= allowed:
        operation = "role_create" if create else "role_update"
        raise ProtocolError(
            ErrorCode.BAD_REQUEST,
            f"{operation} takes {sorted(allowed)}, with {sorted(required)}",
        )
    role = state.roles.get(role_id)
    if not create and role is None:
        retired = state.retired_roles.get(role_id)
        detail = "" if retired is None else f", which was retired by {retired.by}"
        raise EventRejected(f"no role {role_id}{detail}")
    for name in ("handoff_seconds", "handoff_below_percent"):
        if name in args and type(args[name]) is not int:
            raise ProtocolError(ErrorCode.BAD_REQUEST, f"{name} is not a whole number")
    for name in ("workdir", "failover"):
        if name in args and not isinstance(args[name], str):
            raise ProtocolError(ErrorCode.BAD_REQUEST, f"{name} is not a string")
    failover_text = args.get("failover")
    failover = None
    if failover_text is not None:
        try:
            failover = FailoverPolicy(cast(str, failover_text))
        except ValueError as error:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST,
                "failover is one of " + ", ".join(p.value for p in FailoverPolicy),
            ) from error
    no_own = set(_names_arg(args, "no_own"))
    may_own = set(_names_arg(args, "may_own"))
    if no_own & may_own:
        raise ProtocolError(
            ErrorCode.BAD_REQUEST,
            "no_own and may_own both name " + ", ".join(sorted(no_own & may_own)),
        )
    current: dict[str, bool] = {}
    if role is not None:
        current = {
            line_id: state.seats[line_id].may_own
            for line_id in seat_order(state, role_id)
        }
    if "seats" in args:
        order = _names_arg(args, "seats")
        if len(set(order)) != len(order):
            raise ProtocolError(ErrorCode.BAD_REQUEST, "seats names a line twice")
        names: tuple[str, ...] = order
    else:
        order = () if role is None else role.seats
        names = tuple(current)
    unknown = sorted((no_own | may_own) - set(names))
    if unknown:
        raise ProtocolError(
            ErrorCode.BAD_REQUEST,
            "no_own and may_own name only seats of the role, not " + ", ".join(unknown),
        )
    seats = tuple(
        (
            line_id,
            line_id not in no_own
            and (line_id in may_own or current.get(line_id, True)),
        )
        for line_id in names
    )
    return RoleWanted(
        workdir=cast(str, args.get("workdir", "" if role is None else role.workdir)),
        handoff_seconds=cast(
            int,
            args.get(
                "handoff_seconds",
                DEFAULT_HANDOFF_SECONDS if role is None else role.handoff_seconds,
            ),
        ),
        failover=failover
        or (FailoverPolicy.AUTOMATIC if role is None else role.failover),
        handoff_below_percent=cast(
            int,
            args.get(
                "handoff_below_percent",
                DEFAULT_HANDOFF_BELOW_PERCENT
                if role is None
                else role.handoff_below_percent,
            ),
        ),
        order=order,
        seats=seats,
    )


def _text_args(args: dict[str, object], names: tuple[str, ...]) -> tuple[str, ...]:
    """Return exactly the named text arguments, or raise ``ProtocolError``."""
    if set(args) != set(names):
        raise ProtocolError(
            ErrorCode.BAD_REQUEST,
            f"the arguments are {sorted(args)}, expected {sorted(names)}",
        )
    values: list[str] = []
    for name in names:
        value = args[name]
        if not isinstance(value, str):
            raise ProtocolError(ErrorCode.BAD_REQUEST, f"{name} is not a string")
        values.append(value)
    return tuple(values)


class PeerReader(Protocol):
    """What a peer connection reads from, an ``asyncio`` stream or the open
    listener's ``OpenTlsStream``."""

    async def readline(self) -> bytes: ...

    async def read(self, n: int = -1) -> bytes: ...


class PeerWriter(Protocol):
    """What a connection writes answers to, an ``asyncio`` stream or the open
    listener's ``OpenTlsStream``."""

    def write(self, data: bytes) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...

    async def wait_closed(self) -> None: ...


async def _write(writer: PeerWriter, response: Response) -> None:
    """Write ``response``, or, when it cannot be encoded, such as a result over the
    line limit or one holding a float, an internal error that names the request, so
    the client learns why instead of finding the connection closed."""
    try:
        encoded = encode_response(response)
    except ProtocolError as error:
        print(f"dromond: a response could not be encoded: {error}", file=sys.stderr)
        encoded = encode_response(
            Response(
                response.request_id,
                ok=False,
                error_code=ErrorCode.INTERNAL,
                error_message=f"the daemon could not encode its response: {error}",
            )
        )
    writer.write(encoded)
    await writer.drain()


type Responder = Callable[[bytes], Awaitable[Response]]


async def _connection(
    daemon: Daemon,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    responder: Responder,
    limit: int = MAX_LINE_BYTES,
) -> None:
    """Serve requests on one connection until EOF, timeout, error or stop."""
    try:
        while not daemon.stop_requested.is_set():
            try:
                line = await asyncio.wait_for(reader.readline(), IDLE_TIMEOUT_SECONDS)
            except TimeoutError:
                break
            except ValueError:
                # The stream reader raises this when a line exceeds its limit. The rest
                # of the line is still buffered, so the connection cannot continue.
                await _write(
                    writer,
                    Response(
                        "",
                        ok=False,
                        error_code=ErrorCode.TOO_LONG,
                        error_message=f"the line exceeds {limit} bytes",
                    ),
                )
                break
            if not line:
                break
            if not line.endswith(b"\n"):
                await _write(
                    writer,
                    Response(
                        request_id_of(line, limit),
                        ok=False,
                        error_code=ErrorCode.BAD_REQUEST,
                        error_message="the connection closed inside a line",
                    ),
                )
                break
            await _write(writer, await responder(line))
    except (ConnectionError, BrokenPipeError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, BrokenPipeError):
            await writer.wait_closed()


class LineReader:
    """Reads lines of at most ``limit`` bytes in chunks, so that the stream's own
    limit can stay small. The peer socket's stream is limited to the ``hello`` line,
    and an identified peer's larger lines are read through this."""

    def __init__(self, reader: PeerReader, limit: int) -> None:
        self._reader = reader
        self._limit = limit
        self._buffer = bytearray()

    async def readline(self) -> bytes:
        """The next line with its newline, or what remains at end of file, which is
        empty or lacks the newline. Raises ``ValueError`` past the limit. Each pass
        returns, raises, or grows the buffer toward the limit, so it is bounded."""
        while True:
            end = self._buffer.find(b"\n")
            if end >= 0:
                if end + 1 > self._limit:
                    raise ValueError(f"the line exceeds {self._limit} bytes")
                line = bytes(self._buffer[: end + 1])
                del self._buffer[: end + 1]
                return line
            if len(self._buffer) >= self._limit:
                raise ValueError(f"the line exceeds {self._limit} bytes")
            chunk = await self._reader.read(MAX_HELLO_LINE_BYTES)
            if not chunk:
                rest = bytes(self._buffer)
                self._buffer.clear()
                return rest
            self._buffer += chunk


async def peer_connection(
    daemon: Daemon,
    reader: PeerReader,
    writer: PeerWriter,
    session: "PeerSession",
) -> None:
    """Serve one connection on the peer socket. Its first line must arrive within
    ``HELLO_TIMEOUT_SECONDS`` and hold at most ``MAX_HELLO_LINE_BYTES``, the stream's
    own limit, and anything but an accepted ``hello`` closes the connection after its
    answer. Later lines may be as large as ``MAX_PEER_LINE_BYTES``, and the connection
    closes when the session loses its peer, as a revoked token makes it."""
    try:
        try:
            first = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT_SECONDS)
        except TimeoutError:
            return
        except ValueError:
            await _write(writer, _too_long(MAX_HELLO_LINE_BYTES))
            return
        if not first.endswith(b"\n"):
            return
        await _write(writer, await session.respond(first))
        lines = LineReader(reader, MAX_PEER_LINE_BYTES)
        while session.peer_id is not None and not daemon.stop_requested.is_set():
            try:
                line = await asyncio.wait_for(lines.readline(), IDLE_TIMEOUT_SECONDS)
            except TimeoutError:
                break
            except ValueError:
                await _write(writer, _too_long(MAX_PEER_LINE_BYTES))
                break
            if not line.endswith(b"\n"):
                break
            await _write(writer, await session.respond(line))
    except (ConnectionError, BrokenPipeError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, BrokenPipeError):
            await writer.wait_closed()


async def tls_peer_connection(
    daemon: Daemon,
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    context: ssl.SSLContext,
) -> None:
    """Serve one connection on the TLS listener. The handshake must finish within
    ``TLS_HANDSHAKE_TIMEOUT_SECONDS`` and present the key of an inbound ``tls`` peer
    by its pin, before any frame is read, and the connection is then served as one on
    the peer socket, with the peer named by that pin in place of a token. A refusal is
    said on standard error and answered with nothing, since a client that failed the
    handshake cannot read an answer."""
    try:
        await writer.start_tls(
            context, ssl_handshake_timeout=TLS_HANDSHAKE_TIMEOUT_SECONDS
        )
    except (OSError, TimeoutError) as error:
        # ``ssl.SSLError`` and ``ConnectionError`` are ``OSError``.
        print(
            f"dromond: refused a tls connection, its handshake failed: {error!r}",
            file=sys.stderr,
        )
        await _closed(writer)
        return
    pin = presented_pin(writer.get_extra_info("ssl_object"))
    if pin is None or (
        pinned_peer(daemon.project.federation, daemon.log.state, pin) is None
    ):
        print(
            "dromond: refused a tls connection, its key matches no inbound tls peer",
            file=sys.stderr,
        )
        await _closed(writer)
        return
    await peer_connection(daemon, reader, writer, PeerSession(daemon, pin_sha256=pin))


async def open_tls_peer_connection(
    daemon: Daemon,
    reader: RawReader,
    writer: RawWriter,
    context: OpenSSLContext,
) -> None:
    """Serve one connection on the open TLS listener, which ``remote = "open"``
    makes. The handshake must finish within ``TLS_HANDSHAKE_TIMEOUT_SECONDS`` and
    present a self-signed Ed25519 key, which ``dromon.open_tls`` judges, before any
    frame is read. The key's pin then names the peer, a declared one whose pin it is
    or else an open peer of its own, as ``Daemon._pinned`` decides at ``hello``."""
    stream = OpenTlsStream(context, reader, writer, MAX_HELLO_LINE_BYTES)
    try:
        pin = await asyncio.wait_for(stream.handshake(), TLS_HANDSHAKE_TIMEOUT_SECONDS)
    except (OSError, TimeoutError) as error:
        # ``OpenTlsError`` is a ``ConnectionError``, so an ``OSError``.
        print(
            f"dromond: refused an open tls connection, its handshake failed: {error!r}",
            file=sys.stderr,
        )
        await _closed(writer)
        return
    await peer_connection(daemon, stream, stream, PeerSession(daemon, pin_sha256=pin))


async def _closed(writer: PeerWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


def _too_long(limit: int) -> Response:
    return Response(
        "",
        ok=False,
        error_code=ErrorCode.TOO_LONG,
        error_message=f"the line exceeds {limit} bytes",
    )


class ConnectionCap:
    """Serves at most ``limit`` connections at once, and answers any further one with
    one rejected response before closing it. The count includes connections still
    being checked by ``peercred``, so a flood is turned away before any work for it.

    Inference, not verified under load: one event loop runs every accept, so the count
    needs no lock. A port with threads needs an atomic counter here."""

    def __init__(self, limit: int, what: str) -> None:
        if limit < 1:
            raise ValueError(f"a connection cap must be at least 1, not {limit}")
        self.limit = limit
        self.what = what
        self.active = 0

    async def admit(
        self,
        writer: asyncio.StreamWriter,
        serving: Callable[[], Awaitable[None]],
        *,
        answer: bool = True,
    ) -> None:
        """Serve the connection, or refuse it when the cap is reached. ``answer``
        false closes a refused one without a response, as on the TLS listener, where
        a plain line would only break the client's handshake."""
        if self.active >= self.limit:
            print(
                f"dromond: refused a connection, {self.limit} {self.what} are open",
                file=sys.stderr,
            )
            if not answer:
                await _closed(writer)
                return
            try:
                await _write(
                    writer,
                    Response(
                        "",
                        ok=False,
                        error_code=ErrorCode.REJECTED,
                        error_message=(
                            f"the daemon serves at most {self.limit} {self.what}"
                        ),
                    ),
                )
            except (ConnectionError, BrokenPipeError):
                pass
            finally:
                writer.close()
                with contextlib.suppress(ConnectionError, BrokenPipeError):
                    await writer.wait_closed()
            return
        self.active += 1
        try:
            await serving()
        finally:
            self.active -= 1


def _outbound_token(store: Path, peer_id: str) -> str | None:
    """The token stored for ``peer_id``, or ``None`` when none is stored or the store
    is unsafe, which is said on standard error and retried at the next connection."""
    try:
        return stored_token(store, peer_id)
    except FederationError as error:
        print(
            f"dromond: the token for peer {peer_id} is unusable: {error}",
            file=sys.stderr,
        )
        return None


async def _guarded(
    writer: asyncio.StreamWriter,
    own_uid: int,
    serving: Callable[[], Awaitable[None]],
) -> None:
    """Serve a connection only when its peer runs as ``own_uid``, read by ``peercred``.

    A refused connection receives one rejected response and is closed, and the refusal
    is written to standard error, so an operator can see who was turned away.
    """
    reason = peercred.refusal(
        cast(peercred.CredentialSocket, writer.get_extra_info("socket")), own_uid
    )
    if reason is None:
        await serving()
        return
    print(f"dromond: refused a connection, {reason}", file=sys.stderr)
    try:
        await _write(
            writer,
            Response(
                "",
                ok=False,
                error_code=ErrorCode.REJECTED,
                error_message=f"the daemon refused the connection, {reason}",
            ),
        )
    except (ConnectionError, BrokenPipeError):
        pass
    finally:
        writer.close()
        with contextlib.suppress(ConnectionError, BrokenPipeError):
            await writer.wait_closed()


def watch_task(
    what: str, stop: asyncio.Event
) -> Callable[["asyncio.Task[None]"], None]:
    """A done callback that says on standard error, when it happens, that the task
    for ``what`` ended with an exception or before ``stop`` was set. Without it, an
    exception in a task the daemon awaits only at shutdown is seen only then.
    ``PeerForwarder.run`` and ``Scheduler.run`` already survive what a step raises, so
    this is the outer watch, for what escapes those."""

    def ended(task: "asyncio.Task[None]") -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            trace = "".join(traceback.format_exception(error)).rstrip()
            print(
                f"dromond: the {what} ended with an unexpected error, and its work"
                f" waits until the daemon restarts\n{trace}",
                file=sys.stderr,
            )
        elif not stop.is_set():
            print(
                f"dromond: the {what} ended before the daemon stopped, and its work"
                " waits until the daemon restarts",
                file=sys.stderr,
            )

    return ended


async def serve(daemon: Daemon) -> None:
    """Listen on the project's socket until a stop is requested."""
    loop = asyncio.get_running_loop()
    own_uid = os.getuid()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, daemon.stop_requested.set)
    writers: set[asyncio.StreamWriter] = set()

    async def operator_responder(line: bytes) -> Response:
        return daemon.respond(line)

    def accepting(
        responder: Callable[[], Responder], limit: int = MAX_LINE_BYTES
    ) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
        """Accept connections, each served by a responder made for it, so that the
        peer socket can keep the peer its ``hello`` named for one connection."""

        async def accept(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            writers.add(writer)
            try:
                # Every socket goes through here, the operator's, the lines', the
                # agents' and the peers', so none of them serves a process of another
                # user.
                await _guarded(
                    writer,
                    own_uid,
                    lambda: _connection(daemon, reader, writer, responder(), limit),
                )
            finally:
                writers.discard(writer)

        return accept

    socket_path = daemon.paths.socket
    server = await asyncio.start_unix_server(
        accepting(lambda: operator_responder),
        path=str(socket_path),
        limit=MAX_LINE_BYTES,
    )
    # The line socket serves only lines whose engine was issued a token, so it is
    # always open, and it never accepts an agent or operator request.
    line_path = daemon.paths.line_socket
    with contextlib.suppress(FileNotFoundError):
        line_path.unlink()
    servers = [
        server,
        await asyncio.start_unix_server(
            accepting(lambda: daemon.respond_line),
            path=str(line_path),
            limit=MAX_LINE_BYTES,
        ),
    ]
    line_path.chmod(0o600)
    if daemon.agents.listen:
        # A separate socket, so that nothing an agent sends reaches the operator
        # dispatch, and so that turning agents off leaves nothing to connect to.
        agent_path = daemon.paths.agent_socket
        with contextlib.suppress(FileNotFoundError):
            agent_path.unlink()
        servers.append(
            await asyncio.start_unix_server(
                accepting(lambda: daemon.respond_agent),
                path=str(agent_path),
                limit=MAX_LINE_BYTES,
            )
        )
        agent_path.chmod(0o600)
        print(
            f"dromond: agents may connect on {agent_path}, anonymous"
            f" {daemon.agents.anonymous}, at most {daemon.agents.max_access}",
            file=sys.stderr,
        )
    # One cap counts the peer socket's connections and the TLS listener's together,
    # each from its accept, so a TLS connection counts before its handshake.
    peer_cap = ConnectionCap(MAX_PEER_CONNECTIONS, "peer connections")
    if daemon.project.federation.listen:
        # A separate socket that serves only hello and deliver, so that nothing a
        # peer sends reaches the operator's or an agent's dispatch.
        peer_path = daemon.paths.peer_socket
        with contextlib.suppress(FileNotFoundError):
            peer_path.unlink()

        async def peer_serve(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            # The caller's start time is read at accept, before any line, so a pid
            # reused before the hello does not match the entry, finding F2.
            session = PeerSession(daemon, peer_pid=_peer_pid(writer))
            await session.read_peer_start()
            await peer_connection(daemon, reader, writer, session)

        async def peer_accept(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            writers.add(writer)
            try:
                await peer_cap.admit(
                    writer,
                    lambda: _guarded(
                        writer, own_uid, lambda: peer_serve(reader, writer)
                    ),
                )
            finally:
                writers.discard(writer)

        # The stream's limit is the hello line's, which bounds what a connection
        # that has not named its peer can make the daemon buffer.
        servers.append(
            await asyncio.start_unix_server(
                peer_accept, path=str(peer_path), limit=MAX_HELLO_LINE_BYTES
            )
        )
        peer_path.chmod(0o600)
        print(f"dromond: peers may connect on {peer_path}", file=sys.stderr)
    tls_listener = await _listen_tls(daemon, peer_cap, writers)
    if tls_listener is not None:
        servers.append(tls_listener)
    # Made before any task starts, so that a log it cannot read stops the start cleanly.
    forwarder = daemon.forwarder()
    relayer = daemon.operator_relayer()
    daemon.scheduler.recover()
    scheduling = asyncio.create_task(daemon.scheduler.run(daemon.stop_requested))
    forwarding = asyncio.create_task(forwarder.run(daemon.stop_requested))
    relaying = asyncio.create_task(relayer.run(daemon.stop_requested))
    scheduling.add_done_callback(watch_task("scheduler", daemon.stop_requested))
    forwarding.add_done_callback(watch_task("forwarder", daemon.stop_requested))
    relaying.add_done_callback(watch_task("relayer", daemon.stop_requested))
    try:
        # The umask already gives 0600, and this states it rather than relying on it.
        socket_path.chmod(0o600)
        print(f"dromond: serving {daemon.paths.root} on {socket_path}", file=sys.stderr)
        await daemon.stop_requested.wait()
    finally:
        daemon.stop_requested.set()
        await scheduling
        # Its watch has said how it ended, and an error here must not keep the
        # sockets open.
        with contextlib.suppress(Exception):
            await forwarding
        # A relay command still running is killed with its process group, and its
        # notice stays undelivered for the next start.
        relaying.cancel()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await relaying
        for listening in servers:
            listening.close()
        for writer in list(writers):
            writer.close()
        for listening in servers:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    listening.wait_closed(), SHUTDOWN_TIMEOUT_SECONDS
                )
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)


def admitted_network(
    federation: FederationConfig, writer: asyncio.StreamWriter
) -> bool:
    """Whether a connection to the TLS listener comes from ``allowed_networks``,
    judged by its address before it counts under the cap and before its handshake,
    so a refused one costs neither a slot nor a handshake. A connection whose address
    cannot be read is refused while the list is set."""
    if not federation.allowed_networks:
        return True
    peer: object = writer.get_extra_info("peername")
    host: object = (
        cast(tuple[object, ...], peer)[0] if isinstance(peer, tuple) and peer else None
    )
    if isinstance(host, str) and federation.admits(host):
        return True
    print(
        f"dromond: refused a tls connection from {host}, outside allowed_networks",
        file=sys.stderr,
    )
    return False


async def _listen_tls(
    daemon: Daemon, cap: ConnectionCap, writers: set[asyncio.StreamWriter]
) -> asyncio.Server | None:
    """The TLS listener on ``tls_listen``, when the project names one. A context or
    address that fails is said on standard error and nothing listens over TLS, so
    the rest of the daemon, the socket's peers included, still serves. **Decided by
    core, for review.** Refusing the start would make the failure louder, at the
    cost of every other service."""
    federation = daemon.project.federation
    if federation.tls_listen is None:
        return None
    where = format_host_port(federation.tls_listen)
    try:
        serving = tls_serving(daemon)
    except (FederationError, OSError) as error:
        print(f"dromond: nothing listens over tls on {where}: {error}", file=sys.stderr)
        return None

    async def accept(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        if not admitted_network(federation, writer):
            writer.close()
            return
        writers.add(writer)
        try:
            await cap.admit(writer, lambda: serving(reader, writer), answer=False)
        finally:
            writers.discard(writer)

    host, port = federation.tls_listen
    try:
        # The stream's limit is the hello line's, as on the peer socket.
        server = await asyncio.start_server(
            accept, host, port, limit=MAX_HELLO_LINE_BYTES
        )
    except OSError as error:
        print(f"dromond: nothing listens over tls on {where}: {error}", file=sys.stderr)
        return None
    print(f"dromond: peers may connect over tls on {where}", file=sys.stderr)
    return server


type TlsServing = Callable[
    [asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]
]
"""Serves one connection the TLS listener has admitted, from its handshake on."""


def tls_serving(daemon: Daemon) -> TlsServing:
    """How the TLS listener serves each connection. With ``remote = "open"`` it is
    ``open_tls_peer_connection`` over pyOpenSSL, for declared and undeclared keys
    alike, since one port cannot hold two TLS stacks. Otherwise it is the standard
    library's allowlist listener, unchanged. Raises ``FederationError`` or ``OSError``
    when the context cannot be built, such as for a missing identity."""
    federation = daemon.project.federation
    store = daemon.paths.state_dir / PEERS_DIR_NAME
    if federation.remote_open:
        identity = read_identity(store)
        if identity is None:
            raise FederationError(
                "this daemon has no identity to present over tls, which dromon peer"
                " key makes"
            )
        opened = open_server_context(identity)

        def open_serving(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> Awaitable[None]:
            return open_tls_peer_connection(daemon, reader, writer, opened)

        return open_serving
    context = server_context_for(daemon.paths.state_dir, store, federation)

    def pinned_serving(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> Awaitable[None]:
        return tls_peer_connection(daemon, reader, writer, context)

    return pinned_serving


def successor_args(overrides: Overrides, hold_wakes: bool) -> list[str]:
    """The ``dromond`` options a restart's successor starts with, so that it keeps
    what the command line gave. ``dromon.client.start`` adds the project and the
    runtime directory. Floats are written with ``repr``, which reads back exactly."""
    args: list[str] = []
    if overrides.operator is not None:
        args += ["--operator", overrides.operator]
    if overrides.tick_seconds is not None:
        args += ["--tick-seconds", repr(overrides.tick_seconds)]
    if overrides.max_autonomous_turns is not None:
        args += ["--max-autonomous-turns", str(overrides.max_autonomous_turns)]
    if overrides.max_cost_usd is not None:
        args += ["--max-cost-usd", repr(overrides.max_cost_usd)]
    if overrides.cooldown_seconds is not None:
        args += ["--cooldown-seconds", repr(overrides.cooldown_seconds)]
    if hold_wakes:
        args.append("--hold-wakes")
    return args


def start_successor(paths: ProjectPaths, args: Sequence[str]) -> int:
    """Start a restart's successor once this daemon has released its lock and its
    sockets, and return this daemon's exit status. A successor that does not start
    leaves no daemon running, which is said, since the code it would run is the code
    installed now, which the operator must fix or start by hand."""
    try:
        pid = client.start(paths, args)
    except (client.DaemonFailed, PathError, ValueError, OSError) as error:
        print(
            f"dromond: the restarted daemon did not start: {error}. No daemon runs"
            " for the project until dromon start succeeds",
            file=sys.stderr,
        )
        return exits.FAILURE
    print(
        f"dromond: stopped for a restart, and the successor is pid {pid}",
        file=sys.stderr,
    )
    return exits.OK


def _peer_pid(writer: asyncio.StreamWriter) -> int | None:
    """The pid of the process that connected to the peer socket, or ``None``."""
    try:
        return peercred.peer_pid(
            cast(peercred.CredentialSocket, writer.get_extra_info("socket"))
        )
    except peercred.PeerCredentialError:
        return None


def _same_path(left: Path, right: Path) -> bool:
    """Whether two paths name one file, compared by their resolved spelling, since a
    socket that is not yet made or is gone cannot be compared by its inode."""
    return os.path.realpath(left) == os.path.realpath(right)


class ProjectMismatch(Exception):
    """The project file and the event log disagree on something that cannot change."""


def protected_dirs(
    paths: ProjectPaths,
    federation: FederationConfig = NO_FEDERATION,
    registry_dir: Path | None = None,
) -> tuple[Path, ...]:
    """The directories every enabled sandbox denies, which hold the event log, the
    project file and the daemon's socket, followed by each declared peer's, which hold
    the same for another project, and then the fleet index. Raises ``ConfigError``
    when a peer names this project's own directories, or two peers share one."""
    own = (paths.state_dir.resolve(), paths.runtime_dir.resolve())
    try:
        found = own + peer_directories(federation, own)
    except FederationError as error:
        raise ConfigError(str(error)) from error
    try:
        registry_in_use = registry_dir or registry.configured_directory(os.environ)
    except registry.RegistryError as error:
        raise ConfigError(str(error)) from error
    # The fleet index, whatever the federation mode, so that no line can write an
    # entry that sends the operator's commands to a socket of its choosing. Round 13,
    # item 1. A directory that does not exist yet is denied as well, by its path.
    found = (
        *found,
        *(
            index
            for index in fleet.denied_indexes(registry_in_use)
            if index not in found
        ),
    )
    # The registry of open local federation, which a line that could write it could
    # use to pass for a daemon. Each registered project's own directories are read
    # at each engine build, by ``registry_protected``. The default registry is denied
    # too when ``DROMON_REGISTRY_DIR`` moves this daemon's, so that its lines cannot
    # write the one other daemons read.
    if federation.local_open:
        reals = dict.fromkeys(
            os.path.realpath(each)
            for each in (registry_in_use, registry.default_directory())
        )
        found = (*found, *(Path(real) for real in reals))
    return found


def open_federation(
    scheduler: Scheduler,
    log: EventLog,
    federation: FederationConfig,
    directory: Path | None = None,
) -> None:
    """Give the scheduler what open federation needs at each use, the open peers'
    turns per hour from ``[federation.open]``, the allowance the undeclared remote
    keys share, and, while ``local`` is open, whether a name is registered."""

    def turns(peer_id: str) -> int:
        definition = federation.definition(log.state, peer_id)
        return 0 if definition is None else definition.turns_per_hour

    scheduler.peer_turns.open_allowance = turns
    scheduler.peer_turns.shared = lambda peer_id: federation.is_open_remote(
        log.state, peer_id
    )
    scheduler.peer_turns.shared_allowance = lambda: (
        federation.open.remote_total_turns_per_hour
    )
    if not federation.local_open:
        return
    where = directory or registry.configured_directory(os.environ)

    def registered(name: str) -> bool:
        try:
            registry.check_directory(where)
        except registry.RegistryError:
            return False
        return registry.read_entry(where, name) is not None

    scheduler.registered = registered


def registry_protected(
    paths: ProjectPaths, federation: FederationConfig, directory: Path
) -> Callable[[], tuple[Path, ...]] | None:
    """While ``local`` is open, what reads every registered project's runtime and
    state directories, live or stale, other than this project's own, for each engine
    build. **Decided by core, for the operator's review.** They are read at each
    build rather than at start, so a project registered after this daemon started is
    denied from each line's next turn. A turn already running keeps the sandbox it
    started with. A registry that cannot be read, or holds more entries than it may,
    raises ``ConfigError``, which ends the turn before anything runs."""
    if not federation.local_open:
        return None
    own = {
        os.path.realpath(paths.state_dir),
        os.path.realpath(paths.runtime_dir),
    }

    def read() -> tuple[Path, ...]:
        try:
            found = registry.entries(directory)
        except registry.RegistryError as error:
            raise ConfigError(f"the open-federation registry: {error}") from error
        dirs: dict[str, Path] = {}
        for entry in found:
            for each in entry.directories():
                real = os.path.realpath(each)
                if real not in own:
                    dirs[real] = Path(real)
        return tuple(dirs[key] for key in sorted(dirs))

    return read


def open_registration(
    paths: ProjectPaths,
    federation: FederationConfig,
    directory: Path,
    discloses: Callable[[str], bool],
    start_of: registry.StartOf,
) -> registry.Entry | None:
    """Register this daemon in the open-federation registry while ``local`` is open,
    with the pid and start time the process table gives it, and say the entry, or
    ``None`` when the project is not open. Raises ``FederationError`` when the
    registry is refused, the name is held by a live daemon, the name is on the
    disclosure list, which it would leave the project through, or the registry lies
    in the project, where a line could reach it."""
    if not federation.local_open or federation.name is None:
        return None
    name = federation.name
    if discloses(name):
        raise FederationError(
            "federation.name is on the disclosure list, and an open daemon gives it"
            " to every registered project"
        )
    root = paths.root.resolve()
    real = Path(os.path.realpath(directory))
    if real == root or root in real.parents:
        raise FederationError(
            f"the open-federation registry {real} lies inside the project {root}"
        )
    pid = os.getpid()
    try:
        started = start_of(pid)
    except Exception as error:
        raise FederationError(
            f"the process table cannot give this daemon's start time: {error}"
        ) from error
    if started is None:
        raise FederationError("the process table does not list this daemon")
    try:
        entry = registry.Entry(name, paths.peer_socket, root, pid, started)
        registry.register(directory, entry, start_of)
    except registry.RegistryError as error:
        raise FederationError(str(error)) from error
    print(
        f"dromond: registered for open local federation as {name} in {directory}",
        file=sys.stderr,
    )
    return entry


def make_index(registry_dir: Path) -> bool:
    """Make the fleet index before any engine is built, so that every line's sandbox
    denies a directory that exists, and say whether it is there. Review of 7ff033f,
    G5. **Decided by core, for the operator's review.** A failure is written to
    standard error and the daemon starts, as one that cannot be listed does. A
    directory refused for a wider mode exists, so it is denied. **Inference, not
    verified.** A directory the daemon cannot make, its lines, which run as the same
    user with less access, cannot make either, unless the cause passes."""
    try:
        fleet.ensure_index(registry_dir)
    except fleet.FleetError as error:
        print(f"dromond: {error}", file=sys.stderr)
        return False
    return True


def fleet_registration(
    paths: ProjectPaths,
    registry_dir: Path,
    start_of: registry.StartOf,
    commit: str | None,
) -> fleet.IndexEntry | None:
    """Write this daemon's entry in the fleet index, whatever its federation mode, and
    say it, or ``None`` when it cannot be written. Round 13, item 1.

    **Decided by core, for the operator's review.** A failure is written to standard
    error and the daemon starts unlisted, rather than refusing to start. The index
    grants nothing, since a client reaches a daemon only through its socket, so a
    daemon missing from it loses only the fleet commands' sight of it, while a refusal
    would stop a project's daemon for a convenience. A project root whose last step is
    not a valid name, such as one with a control character, is also left out."""
    root = paths.root.resolve()
    directory = fleet.index_directory(registry_dir)
    try:
        started = start_of(os.getpid())
        if started is None:
            raise fleet.FleetError("the process table does not list this daemon")
        entry = fleet.IndexEntry(
            root.name, root, paths.socket, os.getpid(), started, commit
        )
        fleet.write_index_entry(directory, entry)
    except Exception as error:  # noqa: BLE001, any failure leaves the daemon unlisted
        print(
            f"dromond: not listed in the fleet index {directory}: {error}",
            file=sys.stderr,
        )
        return None
    return entry


def check_plugins(config: LineConfig) -> None:
    """Refuse a configuration whose plugins would not load, as the engine would at its
    next turn. Raises ``ConfigError``."""
    for path in config.plugins:
        try:
            check_plugin(resolve_plugin(path))
        except EngineRefused as error:
            raise ConfigError(str(error)) from error


def check_sandboxes(config: ProjectConfig, paths: ProjectPaths) -> None:
    """Refuse a project file whose working directories are not allowed, or whose
    sandboxes cannot be made safe in them, before anything starts, rather than at
    each line's first turn. Raises ``ConfigError`` or ``SandboxError``."""
    protected = protected_dirs(paths, config.federation)
    effective(config.sandbox, paths.root, protected)
    for line in config.lines.values():
        check_plugins(line.config)
        workdir = resolve_workdir(line.config.workdir, paths.root, protected)
        merged = merge([config.sandbox, line.config.sandbox])
        enabled = merged is not None and merged.enabled
        guarded = git_write_guards(workdir) if enabled else ()
        effective(merged, workdir, protected, guarded)
    for role_id, role in sorted(config.roles.items()):
        worktree = resolve_workdir(role.workdir, paths.root, protected)
        for line_id in role.seats:
            seat = config.lines[line_id].config.workdir
            if resolve_workdir(seat, paths.root, protected) != worktree:
                raise ConfigError(
                    f"line {line_id} is a seat of role {role_id}, so it runs in the"
                    f" role's worktree {role.workdir}, not in {seat}"
                )


def git_ssh_access(
    config: ProjectConfig, paths: ProjectPaths, environment: Mapping[str, str]
) -> dict[str, GitSshAccess]:
    """The resolved ``[git_ssh]`` agent of each line the project file gives it, and
    none when the file has no such table. The table is checked whenever it is
    present, so a socket or a file it names is refused before anything starts even
    when no line uses it yet. Raises ``GitSshError``, which is a ``ValueError``,
    also for a line whose enabled sandbox denies reading a file ssh needs.

    ``environment`` is the daemon's own, read for ``SSH_AUTH_SOCK`` when the agent is
    inherited. **Assumption.** The socket is resolved once, at start, so an agent
    restarted on another socket needs a daemon restart too."""
    if config.git_ssh is None:
        return {}
    protected = protected_dirs(paths, config.federation)
    access = gitssh.resolve(config.git_ssh, paths.root, environment, protected)
    given: dict[str, GitSshAccess] = {}
    for line_id, line in sorted(config.lines.items()):
        if not line.git_ssh:
            continue
        merged = merge([config.sandbox, line.config.sandbox])
        if merged is not None and merged.enabled:
            workdir = resolve_workdir(line.config.workdir, paths.root, protected)
            document = effective(
                merge([merged, sandbox_layer(access)]),
                workdir,
                protected,
                git_write_guards(workdir),
            )
            denied = None if document is None else unreadable(access, document)
            if denied is not None:
                raise GitSshError(
                    f"line {line_id}'s sandbox denies reading {denied}, which ssh"
                    " needs for the [git_ssh] table",
                    f"lines.{line_id}.git_ssh",
                )
        given[line_id] = access
    return given


def git_ssh_lines(config: ProjectConfig) -> tuple[str, ...]:
    """The lines the project file gives its ``[git_ssh]`` agent."""
    return tuple(sorted(i for i, line in config.lines.items() if line.git_ssh))


def check_sandbox_required(
    project: ProjectConfig, line_id: str, model: str, config: LineConfig
) -> None:
    """Refuse a Claude Code line whose merged sandbox is not enabled, when the
    project requires one. Codex and Grok lines run under their own permission
    profiles, Pi lines take no built-in tool that could run a command or touch a
    file, and Cursor and Antigravity lines are refused, so they satisfy the
    requirement. Also refuse a line on an engine Dromon cannot confine while the
    project declares peers and does not accept it, by decision 9 of the federation
    design, which counts a Claude Code line without an enabled sandbox, by the
    operator's decision of 2026-10-28. Raises ``SandboxError``."""
    merged: SandboxConfig | None = merge([project.sandbox, config.sandbox])
    sandboxed = merged is not None and merged.enabled
    refusal = unconfined_refusal(
        project.federation, line_id, ENGINE_OF.get(model), sandboxed
    )
    if refusal is not None:
        raise SandboxError(refusal)
    if not project.require_sandbox or ENGINE_OF.get(model) != CLAUDE_CODE_ENGINE:
        return
    if not sandboxed:
        raise SandboxError(
            f"line {line_id} would run on Claude Code without an enabled sandbox,"
            " which the project's daemon.require_sandbox requires"
        )


def check_lines_sandboxed(
    config: ProjectConfig, log: EventLog, scheduler: Scheduler
) -> None:
    """Refuse a project that requires sandboxes while a line it declares, or a line
    created through a client, would run without one. Raises ``SandboxError``."""
    for line_id, definition in sorted(config.lines.items()):
        check_sandbox_required(config, line_id, definition.model, definition.config)
    for line_id in sorted(set(log.state.lines) - set(config.lines)):
        check_sandbox_required(
            config,
            line_id,
            scheduler.model_of(line_id),
            scheduler.effective_config(line_id),
        )


def project_terms(config: ProjectConfig, paths: ProjectPaths) -> tuple[str, ...]:
    """The project's disclosure terms, or none when it names no list. Raises
    ``DisclosureError`` for a list that cannot be read, naming the file only."""
    if config.disclosure_terms is None:
        return ()
    return load_terms(paths.root / config.disclosure_terms)


def check_engines(config: ProjectConfig) -> None:
    """Refuse a project file that gives a Codex line a sandbox or settings through the
    project's own tables, which the line's engine cannot honour. Raises ``ValueError``.
    A line's own tables are checked when the line is created or configured."""
    if config.sandbox is None and config.settings is None:
        return
    outside = sorted(
        line_id
        for line_id, line in config.lines.items()
        if ENGINE_OF[line.model] in DOCUMENTLESS_ENGINES
    )
    if outside:
        raise ValueError(
            "the project's sandbox or settings would apply to the lines "
            + ", ".join(outside)
            + ", which run on engines other than Claude Code and take neither"
        )


def start_teacher(config: ProjectConfig, paths: ProjectPaths) -> Teacher | None:
    """Bring the project's teacher up when its table sets ``autostart``, before any
    line runs, and return it so the daemon can release it at exit. Raises
    ``TeacherError``."""
    if config.teacher is None or not config.teacher.autostart:
        return None
    teacher = Teacher(config.teacher, paths.runtime_dir)
    print(f"dromond: loading the teacher {config.teacher.model}", file=sys.stderr)
    found = teacher.up()
    print(f"dromond: teacher {found.summary()}", file=sys.stderr)
    return teacher


def release_teacher(teacher: Teacher) -> None:
    """Release a teacher the daemon brought up. A failure is reported, not raised,
    because the daemon is already stopping."""
    try:
        teacher.down()
    except TeacherError as error:
        print(f"dromond: could not release the teacher: {error}", file=sys.stderr)


def resolve(config: ProjectConfig, overrides: Overrides) -> tuple[Limits, str, float]:
    """The project-wide limits, default operator and tick cadence to run with."""
    base = config.limits
    limits = Limits(
        overrides.max_autonomous_turns
        if overrides.max_autonomous_turns is not None
        else base.max_autonomous_turns,
        overrides.max_cost_usd
        if overrides.max_cost_usd is not None
        else base.max_cost_usd,
        overrides.cooldown_seconds
        if overrides.cooldown_seconds is not None
        else base.cooldown_seconds,
    )
    operator = overrides.operator or config.operator or DEFAULT_OPERATOR
    tick = overrides.tick_seconds
    if tick is None:
        tick = (
            config.tick_seconds
            if config.tick_seconds is not None
            else DEFAULT_TICK_SECONDS
        )
    return limits, OperatorAddress(operator).operator_id, tick


def check_project(log: EventLog, config: ProjectConfig) -> None:
    """Raise ``ProjectMismatch`` if the file changes a line's model or strategy, so
    that nothing is applied from such a file. The comparison is with the line's
    current binding, which ``rebind_line`` may have moved since the line was created,
    so a file updated after a rebind matches. A client may still switch a line's model
    within its engine, as an override."""
    for line_id, definition in sorted(config.lines.items()):
        existing = log.state.lines.get(line_id)
        if existing is not None and (existing.engine, existing.strategy) != (
            definition.model,
            definition.strategy.value,
        ):
            raise ProjectMismatch(
                f"line {line_id} runs {existing.engine} with the"
                f" {existing.strategy} strategy, but the project file says"
                f" {definition.model} with"
                f" {definition.strategy}. Change the file to the line's current"
                " binding, rebind the line, or create a new line instead."
            )


def check_roles(scheduler: Scheduler, log: EventLog, config: ProjectConfig) -> None:
    """Raise ``ProjectMismatch`` if the file's roles cannot be recorded, so that
    nothing is applied from such a file, as ``check_project`` does for lines."""
    try:
        plan_file_roles(
            log.state, config, scheduler.default_operator, scheduler.wall_due()
        )
        # Round 10 review, F2, F3 and F4. The one-writer rule, on the state the file
        # would make, so a file that breaks it is refused whole.
        scheduler.check_project_file(config)
    except (RoleConflict, EventRejected) as error:
        raise ProjectMismatch(f"the project file is refused: {error}") from error


def apply_roles(
    scheduler: Scheduler, log: EventLog, config: ProjectConfig
) -> tuple[str, ...]:
    """Record the file's roles, seats and first owners, as
    ``roles.plan_file_roles`` describes, and return its notices of the roles the file
    leaves as they are, which a reload answers with and a start prints. It runs
    after ``apply_project``, which creates the seats' lines, and after the project's
    operator is set, who is recorded as declaring an owner."""
    try:
        plan = plan_file_roles(
            log.state, config, scheduler.default_operator, scheduler.wall_due()
        )
    except RoleConflict as error:
        raise ProjectMismatch(str(error)) from error
    for event in plan.events:
        log.append(event)
    return plan.notices


def apply_project(scheduler: Scheduler, log: EventLog, config: ProjectConfig) -> None:
    """Bring the log's lines into line with the file's definitions.

    A declared line that does not exist is created, adopting the file's ``session``
    if it gives one. One whose tools changed is reconfigured, under any overrides an
    operator set from a client, which starts a new engine session when its working
    directory moved. A ``session`` for a line that exists is not used. A file that
    changes a line's model or strategy is refused before anything is applied. Lines
    created through the client and absent from the file are left as they are.
    """
    check_project(log, config)
    for line_id, definition in sorted(config.lines.items()):
        existing = log.state.lines.get(line_id)
        if existing is None:
            scheduler.create_line(
                line_id,
                definition.model,
                definition.strategy,
                definition.config,
                definition.session,
                placement_checked=True,
            )
        else:
            stored = (
                DEFAULT_CONFIG
                if existing.config is None
                else decode_config(existing.config)
            )
            if stored != definition.config:
                scheduler.configure_line(
                    line_id, definition.config, placement_checked=True
                )
        scheduler.set_line_policy(
            line_id, definition.limits, definition.wake_every_ticks
        )


def private_dirs(paths: ProjectPaths) -> tuple[Path, ...]:
    """Every private directory Dromon owns under the project, which a start creates
    or checks. The peers' token store is checked by ``check_token_store``, and a
    teacher's runtime directory when the teacher starts."""
    return (
        paths.state_dir,
        paths.runtime_dir,
        paths.left_reports,
        paths.state_dir / proposal_store.PROPOSALS_DIRECTORY,
        paths.state_dir / evidence.PROBES_DIRECTORY,
    )


def check_private_dirs(paths: ProjectPaths) -> None:
    """Create every private directory with mode 0700, or check the one there, and
    refuse naming every one that is not private, not only the first. Raises
    ``PathError``.

    **Round 14, item 8.** A supervised run in another project failed late, on a
    private directory made before with a wide mode. The start now makes or checks
    them all before any line's turn, a role's first turn included, so such a
    project refuses at once with every path to fix. An existing directory is never
    tightened silently, as ``ensure_private_dir`` decides. **Residual.** A mode
    widened after the start is found at the next start, or where the directory is
    next used."""
    refused: list[str] = []
    for directory in private_dirs(paths):
        try:
            ensure_private_dir(directory)
        except PathError as error:
            refused.append(str(error))
    if refused:
        raise PathError(
            "private directories Dromon owns are not private: " + " ".join(refused)
        )


CHECK_PASSED: Final = "dromond: the start checks pass, and nothing was started"
"""What ``dromond --check`` says when a start would get past its checks."""


def check_scratch(paths: ProjectPaths) -> Path:
    """A new private directory under the runtime directory for a check's copy of
    the event log, on the same file system and as private as the log, after
    sweeping the directories killed checks left, as ``restart.sweep_checks``
    describes. Raises ``LogError``."""
    restart.sweep_checks(paths.runtime_dir, restart.CHECK_SECONDS)
    try:
        return Path(
            tempfile.mkdtemp(prefix=restart.CHECK_PREFIX, dir=paths.runtime_dir)
        )
    except OSError as error:
        raise LogError(
            f"cannot make a directory for a copy of the log in {paths.runtime_dir}"
        ) from error


def run(
    paths: ProjectPaths,
    *,
    overrides: Overrides | None = None,
    hold_wakes: bool = False,
    check_only: bool = False,
) -> int:
    """Run the daemon for ``paths`` in the foreground and return its exit status.

    The project file ``.dromon/dromon.toml``, when present, supplies lines, limits and
    the wake policy. ``overrides`` from the command line take precedence over it.

    With ``check_only``, the start checks run and nothing starts, which is what
    ``dromon restart`` asks of the installed code before it stops the running
    daemon. The checks are this function's own, in its own order, so a check added
    to a start is a check of the preflight too. They read the project file and a
    copy of the event log made by ``eventlog.snapshot`` in a private directory under
    the runtime directory, which is removed afterwards, so whatever a start would
    append is appended to the copy. The lock, which the running daemon holds, is not
    taken, the teacher is not brought up, the registry is not written, no socket is
    made and none is removed, the process table is not probed, and recovery, which
    runs in ``serve``, does not run. ``docs/protocol/RESTART.md`` says what the
    check therefore cannot find.
    """
    os.umask(0o077)
    given = overrides or Overrides()
    try:
        # Resolved first, so that a malformed override refuses the start before any
        # check reads the registry, and every later use reads this one.
        registry_dir = registry.configured_directory(os.environ)
        peercred.check_supported()
        check_private_dirs(paths)
        check_token_store(paths.state_dir / PEERS_DIR_NAME)
        file = config_path(paths.state_dir)
        config = load(file, paths.root) if file.exists() else ProjectConfig()
        check_sandboxes(config, paths)
        line_git_ssh = git_ssh_access(config, paths, os.environ)
        check_engines(config)
        terms = project_terms(config, paths)
        limits, operator, tick = resolve(config, given)
        lock = None if check_only else acquire_lock(paths.lock)
    except AlreadyRunning as error:
        print(f"dromond: {error}", file=sys.stderr)
        return exits.ALREADY_RUNNING
    except (
        PathError,
        ConfigFileError,
        ValueError,
        InvalidField,
        peercred.PeerCredentialError,
        registry.RegistryError,
    ) as error:
        print(f"dromond: {error}", file=sys.stderr)
        return exits.FAILURE
    line_tokens = LineTokens()
    line_secrets = {
        line_id: definition.secret_names for line_id, definition in config.lines.items()
    }
    launcher.configure(paths.left_reports)
    if lock is not None:
        # A start holds the lock, so no check of its own runs, and any preflight
        # directory left is a killed check's copy of the log.
        restart.sweep_checks(paths.runtime_dir, None)
    if not check_only:
        # A check writes nothing of the operator's, the fleet index included. It
        # builds no engine, so no sandbox needs the index to exist, and it returns
        # before ``fleet_registration``, so it writes no entry either.
        make_index(registry_dir)
    successor: list[str] | None = None
    scheduler: Scheduler | None = None
    scratch: Path | None = None
    daemon_commit = read_head_commit(Path(__file__).parent)
    try:
        log_path = paths.event_log
        if check_only:
            scratch = check_scratch(paths)
            log_path = scratch / paths.event_log.name
            snapshot(paths.event_log, log_path)
        with EventLog(log_path) as log:
            scheduler = Scheduler(
                log,
                engine_factory(
                    str(paths.root),
                    project_sandbox=config.sandbox,
                    project_settings=config.settings,
                    protected=protected_dirs(paths, config.federation, registry_dir),
                    line_tokens=line_tokens,
                    line_socket=paths.line_socket,
                    line_secrets=line_secrets,
                    line_git_ssh=line_git_ssh,
                    read_only_paths=config.read_only_paths,
                    extra_protected=registry_protected(
                        paths, config.federation, registry_dir
                    ),
                ),
                limits=limits,
                default_operator=operator,
                hold_wakes=hold_wakes,
                tick_seconds=tick,
                wake_every_ticks=config.wake_every_ticks,
                root=paths.root,
                protected=protected_dirs(paths, config.federation, registry_dir),
                read_only_paths=config.read_only_paths,
                daemon_commit=daemon_commit,
                daemon_checkout=Path(__file__).parent,
                processes_identifiable=(
                    False if check_only else environment_reading_works()
                ),
                line_tokens=line_tokens,
                report_dir=paths.left_reports,
                walk_check=launcher.walk_unavailable,
            )
            scheduler.record_walk_gap(launcher.walk_gap())
            scheduler.set_disclosure_terms(terms)
            scheduler.set_disclosure_accepted(config.disclosure_accepted)
            scheduler.set_git_ssh(line_git_ssh)
            scheduler.set_leases(config.lease_names, config.lease_ttl_seconds)
            scheduler.set_runs(config.run_commands)
            check_lines_sandboxed(config, log, scheduler)
            check_roles(scheduler, log, config)
            apply_project(scheduler, log, config)
            for event in plan_peers(log.state, config.federation):
                log.append(event)
            scheduler.federation = config.federation
            scheduler.peer_turns.set_allowances(
                {
                    peer_id: peer.turns_per_hour
                    for peer_id, peer in config.federation.peers.items()
                }
            )
            open_federation(scheduler, log, config.federation, registry_dir)
            teacher = None if check_only else start_teacher(config, paths)
            try:
                daemon = Daemon(
                    paths,
                    log,
                    utc_now(),
                    scheduler,
                    agents=config.agents,
                    line_tokens=line_tokens,
                    project=config,
                    command_line=given,
                    project_file_present=file.exists(),
                    line_secrets=line_secrets,
                    hold_wakes=hold_wakes,
                    registry_dir=registry_dir,
                )
                daemon.apply_project_values()
                for notice in apply_roles(scheduler, log, config):
                    print(f"dromond: {notice}", file=sys.stderr)
                # Checked here as well as in ``serve``, so a refused relay stops the
                # start before any socket is made.
                daemon.relay_routes(config)
                if check_only:
                    print(CHECK_PASSED, file=sys.stderr)
                    return exits.OK
                entry = open_registration(
                    paths,
                    config.federation,
                    daemon.registry_dir,
                    scheduler.discloses,
                    daemon.start_of,
                )
                indexed = fleet_registration(
                    paths,
                    daemon.registry_dir,
                    daemon.start_of,
                    daemon_commit,
                )
                try:
                    asyncio.run(serve(daemon))
                finally:
                    if indexed is not None:
                        fleet.remove_index_entry(
                            fleet.index_directory(daemon.registry_dir), indexed
                        )
                    if entry is not None:
                        registry.unregister(daemon.registry_dir, entry)
                successor = daemon.successor
            finally:
                if teacher is not None:
                    release_teacher(teacher)
    except (
        LogError,
        ProjectMismatch,
        DriverError,
        EventRejected,
        TeacherError,
        SandboxError,
        FederationError,
    ) as error:
        print(f"dromond: {error}", file=sys.stderr)
        return exits.FAILURE
    finally:
        if scheduler is not None:
            scheduler.stop_workers()
        if scratch is not None:
            shutil.rmtree(scratch, ignore_errors=True)
        # A check holds no lock, and the sockets are the running daemon's.
        if lock is not None:
            for stale in (
                paths.socket,
                paths.agent_socket,
                paths.line_socket,
                paths.peer_socket,
            ):
                with contextlib.suppress(FileNotFoundError):
                    stale.unlink()
            os.close(lock)
    if successor is not None:
        return start_successor(paths, successor)
    print("dromond: stopped", file=sys.stderr)
    return exits.OK


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``dromond``."""
    parser = argparse.ArgumentParser(
        prog="dromond",
        description="The Dromon multi-agent harness daemon, run in the foreground.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--project", type=Path, default=Path(), help="project root, default ."
    )
    parser.add_argument(
        "--runtime-dir",
        type=Path,
        default=None,
        help="directory for the socket and lock, default .dromon/run",
    )
    parser.add_argument(
        "--max-autonomous-turns",
        type=int,
        default=None,
        help="turns a line may take without operator mail before it is held,"
        f" default from the project file, else {DEFAULT_LIMITS.max_autonomous_turns}",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=None,
        help="estimated cost a line may run up before it is held, default from the"
        f" project file, else {DEFAULT_LIMITS.max_cost_usd}",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=float,
        default=None,
        help="the least time between a line's turns, default from the project file,"
        f" else {DEFAULT_LIMITS.cooldown_seconds}",
    )
    parser.add_argument(
        "--operator",
        default=None,
        help="who is asked about a held line no operator has messaged, default from"
        f" the project file, else {DEFAULT_OPERATOR}",
    )
    parser.add_argument(
        "--hold-wakes",
        action="store_true",
        help="record messages but wake no line, for maintenance and tests",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="run the start checks against the project file and a copy of the event"
        " log, start nothing and exit, as dromon restart does before it stops the"
        " running daemon",
    )
    parser.add_argument(
        "--tick-seconds",
        type=float,
        default=None,
        help="the time between ticks while work is in flight, default from the"
        f" project file, else {DEFAULT_TICK_SECONDS}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``dromond`` and return its exit status."""
    arguments = build_parser().parse_args(argv)
    project: Path = arguments.project
    runtime_dir: Path | None = arguments.runtime_dir
    try:
        paths = project_paths(project, runtime_dir)
    except PathError as error:
        print(f"dromond: {error}", file=sys.stderr)
        return exits.FAILURE
    tick: float | None = arguments.tick_seconds
    if tick is not None and not 0 <= tick <= MAX_TICK_SECONDS:
        print(
            f"dromond: --tick-seconds must be in [0, {MAX_TICK_SECONDS}]",
            file=sys.stderr,
        )
        return exits.USAGE
    overrides = Overrides(
        operator=arguments.operator,
        tick_seconds=tick,
        max_autonomous_turns=arguments.max_autonomous_turns,
        max_cost_usd=arguments.max_cost_usd,
        cooldown_seconds=arguments.cooldown_seconds,
    )
    try:
        resolve(ProjectConfig(), overrides)
    except (ValueError, InvalidField) as error:
        print(f"dromond: {error}", file=sys.stderr)
        return exits.USAGE
    hold: bool = arguments.hold_wakes
    check: bool = arguments.check
    return run(paths, overrides=overrides, hold_wakes=hold, check_only=check)


if __name__ == "__main__":
    sys.exit(main())
