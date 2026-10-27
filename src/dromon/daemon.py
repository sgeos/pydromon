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
import signal
import sys
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, cast

from claude_agent_sdk import SdkMcpTool

from dromon import __version__, exits, restart
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
from dromon.eventlog import EventLog, LogError, utc_now
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
from dromon.projectconfig import ConfigFileError, ProjectConfig, config_path, load
from dromon.sandbox import SandboxConfig, SandboxError, effective, merge
from dromon.processes import environment_reading_works
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
    ProjectOverridden,
    AgentAccess,
    AgentAddress,
    AgentRegistered,
    AgentRevoked,
    EventRejected,
    InvalidField,
    LineAddress,
    Message,
    OperatorAddress,
    QuestionStatus,
    State,
    parse_address,
)
from dromon import launcher
from dromon.paths import PathError, ProjectPaths, ensure_private_dir, project_paths
from dromon.protocol import (
    MAX_LINE_BYTES,
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
    hold_wakes: bool = False
    """Whether the command line held wakes, which a restart's successor keeps."""
    successor: list[str] | None = field(default=None, init=False)
    """The options a successor starts with, once a restart is ready to stop."""
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
        overrides that no longer resolve are skipped, so they cannot stop a start."""
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

    def execute(self, request: Request) -> Response:
        """Answer one decoded request with operator authority."""
        match request.operation:
            case Operation.PING:
                return Response(request.request_id, ok=True, result={})
            case Operation.STATUS:
                return Response(request.request_id, ok=True, result=self.status())
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
            case (
                Operation.CREATE_LINE
                | Operation.CONFIGURE_LINE
                | Operation.SEND
                | Operation.QUESTIONS
                | Operation.ANSWER
                | Operation.MAIL
                | Operation.READ
                | Operation.ACKNOWLEDGE
                | Operation.SENT
                | Operation.RELEASE
                | Operation.LINE_VIEW
                | Operation.LINE_ACTIVITY
                | Operation.INTERRUPT
                | Operation.MUTE
                | Operation.UNMUTE
                | Operation.RESTART
                | Operation.SETTINGS
                | Operation.SET_SETTING
                | Operation.CLEAR_SESSION
                | Operation.REBIND_LINE
                | Operation.RELOAD_PROJECT
                | Operation.AGENT_ADD
                | Operation.AGENT_REVOKE
                | Operation.AGENT_LIST
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
                case Operation.MUTE:
                    (line_id,) = _text_args(request.args, ("line_id",))
                    changed = self.scheduler.mute(line_id)
                    result = {
                        "line_id": line_id,
                        "muted": True,
                        "changed": changed,
                        "running": self.scheduler.condition(line_id) == "running",
                    }
                case Operation.UNMUTE:
                    (line_id,) = _text_args(request.args, ("line_id",))
                    changed = self.scheduler.unmute(line_id)
                    result = {"line_id": line_id, "muted": False, "changed": changed}
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
                case Operation.AGENT_ADD:
                    result = self._agent_add(request.args)
                case Operation.AGENT_REVOKE:
                    (agent_id,) = _text_args(request.args, ("agent",))
                    self.log.append(AgentRevoked(agent_id))
                    result: dict[str, object] = {"agent": str(AgentAddress(agent_id))}
                case Operation.AGENT_LIST:
                    result = self._agent_list(request.args)
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
        ) as error:
            return Response(
                request.request_id,
                False,
                error_code=ErrorCode.REJECTED,
                error_message=str(error),
            )
        return Response(request.request_id, True, result=result)

    def _create_line(self, args: dict[str, object]) -> dict[str, object]:
        texts = {key: value for key, value in args.items() if key != "config"}
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
        self.scheduler.create_line(line_id, model, chosen, config, session)
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
        resolve_workdir(config.workdir, self.paths.root, protected_dirs(self.paths))
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
        """A page of an operator's mailbox, with its summary. Listing delivers."""
        allowed = {"operator", "unread_only", "sender", "offset", "limit"}
        if "operator" not in args or not set(args) <= allowed:
            raise ProtocolError(
                ErrorCode.BAD_REQUEST, f"mail takes {sorted(allowed)}, with operator"
            )
        (operator_text,) = _text_args({"operator": args["operator"]}, ("operator",))
        unread_only = args.get("unread_only", False)
        offset = args.get("offset", 0)
        limit = args.get("limit", MAX_PAGE)
        sender = args.get("sender")
        if type(unread_only) is not bool:
            raise ProtocolError(ErrorCode.BAD_REQUEST, "unread_only is not a boolean")
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
                sender=None if sender is None else parse_address(sender),
                offset=offset,
                limit=limit,
            )
        except ValueError as error:
            raise InvalidField(str(error)) from error
        replied = replied_ids(self.log.state, owner)
        counts = summary(self.log.state, owner, 0)
        return {
            "total": total,
            "unread": counts.unread,
            "unanswered": counts.unanswered,
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

        A change to the sandbox, the engine settings, the teacher or the external
        agent policy is refused, because the running engine factory, teacher and
        agent socket were built from them, and the daemon must restart to apply it.
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
        fixed = ("sandbox", "settings", "teacher", "agents")
        moved = [
            name
            for name in fixed
            if getattr(config, name) != getattr(self.project, name)
        ]
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
        terms = project_terms(config, self.paths)
        check_lines_sandboxed(config, self.log, self.scheduler)
        for line_id in sorted(set(self.project.lines) - set(config.lines)):
            if line_id in self.log.state.lines:
                self.scheduler.set_line_policy(line_id, None, None)
        apply_project(self.scheduler, self.log, config)
        self.scheduler.set_disclosure_terms(terms)
        self.scheduler.set_leases(config.lease_names, config.lease_ttl_seconds)
        self.project = config
        self.project_file_present = present
        self.apply_project_values()
        return {"file": str(file), "present": present, "lines": sorted(config.lines)}

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
        address = parse_address(recipient)
        if urgent:
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


async def _write(writer: asyncio.StreamWriter, response: Response) -> None:
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
                        error_message=f"the line exceeds {MAX_LINE_BYTES} bytes",
                    ),
                )
                break
            if not line:
                break
            if not line.endswith(b"\n"):
                await _write(
                    writer,
                    Response(
                        request_id_of(line),
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


async def serve(daemon: Daemon) -> None:
    """Listen on the project's socket until a stop is requested."""
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, daemon.stop_requested.set)
    writers: set[asyncio.StreamWriter] = set()

    async def operator_responder(line: bytes) -> Response:
        return daemon.respond(line)

    def accepting(
        responder: Responder,
    ) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]:
        async def accept(
            reader: asyncio.StreamReader, writer: asyncio.StreamWriter
        ) -> None:
            writers.add(writer)
            try:
                await _connection(daemon, reader, writer, responder)
            finally:
                writers.discard(writer)

        return accept

    socket_path = daemon.paths.socket
    server = await asyncio.start_unix_server(
        accepting(operator_responder), path=str(socket_path), limit=MAX_LINE_BYTES
    )
    # The line socket serves only lines whose engine was issued a token, so it is
    # always open, and it never accepts an agent or operator request.
    line_path = daemon.paths.line_socket
    with contextlib.suppress(FileNotFoundError):
        line_path.unlink()
    servers = [
        server,
        await asyncio.start_unix_server(
            accepting(daemon.respond_line), path=str(line_path), limit=MAX_LINE_BYTES
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
                accepting(daemon.respond_agent),
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
    daemon.scheduler.recover()
    scheduling = asyncio.create_task(daemon.scheduler.run(daemon.stop_requested))
    try:
        # The umask already gives 0600, and this states it rather than relying on it.
        socket_path.chmod(0o600)
        print(f"dromond: serving {daemon.paths.root} on {socket_path}", file=sys.stderr)
        await daemon.stop_requested.wait()
    finally:
        daemon.stop_requested.set()
        await scheduling
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


class ProjectMismatch(Exception):
    """The project file and the event log disagree on something that cannot change."""


def protected_dirs(paths: ProjectPaths) -> tuple[Path, ...]:
    """The directories every enabled sandbox denies, which hold the event log, the
    project file and the daemon's socket."""
    return (paths.state_dir.resolve(), paths.runtime_dir.resolve())


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
    protected = protected_dirs(paths)
    effective(config.sandbox, paths.root, protected)
    for line in config.lines.values():
        check_plugins(line.config)
        workdir = resolve_workdir(line.config.workdir, paths.root, protected)
        merged = merge([config.sandbox, line.config.sandbox])
        enabled = merged is not None and merged.enabled
        guarded = git_write_guards(workdir) if enabled else ()
        effective(merged, workdir, protected, guarded)


def check_sandbox_required(
    project: ProjectConfig, line_id: str, model: str, config: LineConfig
) -> None:
    """Refuse a Claude Code line whose merged sandbox is not enabled, when the
    project requires one. Codex and Grok lines run under their own permission
    profiles, Pi lines take no built-in tool that could run a command or touch a
    file, and Cursor and Antigravity lines are refused, so they satisfy the
    requirement. Raises ``SandboxError``."""
    if not project.require_sandbox or ENGINE_OF.get(model) != CLAUDE_CODE_ENGINE:
        return
    merged: SandboxConfig | None = merge([project.sandbox, config.sandbox])
    if merged is None or not merged.enabled:
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
            )
        else:
            stored = (
                DEFAULT_CONFIG
                if existing.config is None
                else decode_config(existing.config)
            )
            if stored != definition.config:
                scheduler.configure_line(line_id, definition.config)
        scheduler.set_line_policy(
            line_id, definition.limits, definition.wake_every_ticks
        )


def run(
    paths: ProjectPaths,
    *,
    overrides: Overrides | None = None,
    hold_wakes: bool = False,
) -> int:
    """Run the daemon for ``paths`` in the foreground and return its exit status.

    The project file ``.dromon/dromon.toml``, when present, supplies lines, limits and
    the wake policy. ``overrides`` from the command line take precedence over it.
    """
    os.umask(0o077)
    given = overrides or Overrides()
    try:
        ensure_private_dir(paths.state_dir)
        ensure_private_dir(paths.runtime_dir)
        ensure_private_dir(paths.left_reports)
        file = config_path(paths.state_dir)
        config = load(file, paths.root) if file.exists() else ProjectConfig()
        check_sandboxes(config, paths)
        check_engines(config)
        terms = project_terms(config, paths)
        limits, operator, tick = resolve(config, given)
        lock = acquire_lock(paths.lock)
    except AlreadyRunning as error:
        print(f"dromond: {error}", file=sys.stderr)
        return exits.ALREADY_RUNNING
    except (PathError, ConfigFileError, ValueError, InvalidField) as error:
        print(f"dromond: {error}", file=sys.stderr)
        return exits.FAILURE
    line_tokens = LineTokens()
    launcher.configure(paths.left_reports)
    successor: list[str] | None = None
    try:
        with EventLog(paths.event_log) as log:
            scheduler = Scheduler(
                log,
                engine_factory(
                    str(paths.root),
                    project_sandbox=config.sandbox,
                    project_settings=config.settings,
                    protected=protected_dirs(paths),
                    line_tokens=line_tokens,
                    line_socket=paths.line_socket,
                ),
                limits=limits,
                default_operator=operator,
                hold_wakes=hold_wakes,
                tick_seconds=tick,
                wake_every_ticks=config.wake_every_ticks,
                root=paths.root,
                protected=protected_dirs(paths),
                daemon_commit=read_head_commit(Path(__file__).parent),
                daemon_checkout=Path(__file__).parent,
                processes_identifiable=environment_reading_works(),
                line_tokens=line_tokens,
                report_dir=paths.left_reports,
                walk_check=launcher.walk_unavailable,
            )
            scheduler.record_walk_gap(launcher.walk_gap())
            scheduler.set_disclosure_terms(terms)
            scheduler.set_leases(config.lease_names, config.lease_ttl_seconds)
            check_lines_sandboxed(config, log, scheduler)
            apply_project(scheduler, log, config)
            teacher = start_teacher(config, paths)
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
                    hold_wakes=hold_wakes,
                )
                daemon.apply_project_values()
                asyncio.run(serve(daemon))
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
    ) as error:
        print(f"dromond: {error}", file=sys.stderr)
        return exits.FAILURE
    finally:
        for stale in (paths.socket, paths.agent_socket, paths.line_socket):
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
    return run(paths, overrides=overrides, hold_wakes=hold)


if __name__ == "__main__":
    sys.exit(main())
