"""The ``dromon`` command, the umbrella client for the Dromon daemon.

``start``, ``status`` and ``stop`` talk to the project's daemon over its socket. The
other subcommands are placeholders that report they are not implemented and exit with
``exits.NOT_IMPLEMENTED``. The subcommand list is expected to grow with the protocol.
"""

import argparse
import asyncio
import getpass
import json
import os
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, cast

from dromon import __version__, exits
from dromon.client import DaemonFailed, DaemonNotRunning, call, is_running, start, stop
from dromon.paths import PathError, ProjectPaths, project_paths
from dromon.driver import MODELS, Driver, DriverError, Strategy
from dromon.engine import EngineRefused
from dromon.agents import TOKEN_ENV
from dromon.lineconfig import EFFORTS
from dromon.model import AgentAccess
from dromon.protocol import Operation, ProtocolError
from dromon.scheduler import LineSpec, engine_factory
from dromon import settings as engine_settings
from dromon.sandbox import SandboxConfig, merge, sandbox_from_settings
from dromon.settings import SettingsConfig, split_settings_file

PROBE_MODEL: Final = "claude-haiku-5-5"
"""The least expensive subscription model, used for probes and tests by default."""
PROBE_PROMPT: Final = "Reply with the single word: ready"

SUBCOMMANDS: Final[dict[str, str]] = {
    "start": "start the daemon for this project",
    "create": "create a session line",
    "configure": "replace a session line's tools, MCP servers and tool policy",
    "stop": "stop the daemon",
    "status": "show the daemon and its session lines",
    "attach": "open an interactive client attached to one line",
    "send": "send a message to a session line or an operator",
    "questions": "list open operator questions",
    "answer": "answer an operator question",
    "mail": "look through an operator's mailbox",
    "read": "open a message in an operator's mailbox",
    "ack": "acknowledge a message without replying",
    "sent": "show what became of the mail an operator sent",
    "release": "resume a line the daemon held at a conversation limit",
    "config": "check the project file and show what it declares",
    "probe": "run one short turn on the engine and report its capabilities",
    "teacher": "bring the project's local teacher model up, report it, or release it",
    "agent": "register, list or revoke the external agents that may connect",
    "mcp": "serve an external agent over MCP on standard input and output",
}
"""Planned subcommands and their one-line help, in display order."""


def _start(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    del arguments
    if is_running(paths):
        print(f"dromon start: already running for {paths.root}")
        return exits.OK
    try:
        pid = start(paths)
    except (DaemonFailed, PathError) as error:
        print(f"dromon start: {error}", file=sys.stderr)
        return exits.FAILURE
    print(f"dromon start: started, pid {pid}")
    return exits.OK


def _status(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    del arguments
    try:
        response = call(paths.socket, Operation.STATUS)
    except DaemonNotRunning:
        print(f"dromon status: not running for {paths.root}")
        return exits.NOT_RUNNING
    except (ProtocolError, TimeoutError, OSError) as error:
        print(f"dromon status: {error}", file=sys.stderr)
        return exits.FAILURE
    if not response.ok or response.result is None:
        print(f"dromon status: {response.error_message}", file=sys.stderr)
        return exits.FAILURE
    result = response.result
    print(f"running, pid {result.get('pid')}, since {result.get('started_at')}")
    print(f"project {result.get('project')}, tick {result.get('tick')}")
    print(
        f"daemon {result.get('daemon_version')},"
        f" protocol {result.get('protocol_version')}"
    )
    log = result.get("event_log")
    if isinstance(log, dict):
        counts: dict[str, object] = log  # pyright: ignore[reportUnknownVariableType]
        print(
            f"events {counts.get('last_seq')}, lines {counts.get('lines')},"
            f" operators {counts.get('operators')},"
            f" open questions {counts.get('open_questions')}"
        )
    return exits.OK


def _stop(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    del arguments
    try:
        stop(paths)
    except DaemonNotRunning:
        print(f"dromon stop: not running for {paths.root}")
        return exits.NOT_RUNNING
    except (DaemonFailed, ProtocolError, TimeoutError, OSError) as error:
        print(f"dromon stop: {error}", file=sys.stderr)
        return exits.FAILURE
    print("dromon stop: stopped")
    return exits.OK


def _probe(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Run one short turn with no tools and report what the engine says.

    The driver's strategy decides which credential is acceptable, so the default
    subscription driver can never start usage billed per token.
    """
    try:
        model: str = arguments.model
        strategy = Strategy(arguments.strategy)
        driver = Driver.for_model(model, strategy)
        line = engine_factory(str(paths.root))(LineSpec("probe", model, strategy), [])
        report = asyncio.run(line.run_turn(PROBE_PROMPT))
    except (DriverError, EngineRefused) as error:
        print(f"dromon probe: {error}", file=sys.stderr)
        return exits.FAILURE
    settings = driver.settings
    print(
        f"driver {settings.model} {settings.strategy},"
        f" wcet {driver.wcet_per_iteration} per decision"
    )
    print(
        f"settings effort {settings.effort}, max turns {settings.max_turns},"
        f" tool budget {settings.tool_budget},"
        f" context window {settings.context_window}"
    )
    print(f"engine {settings.engine}, credential {report.credential}")
    found = report.capabilities
    if found is not None:
        print(
            f"engine Claude Code {found.claude_code_version},"
            f" Agent SDK {found.sdk_version}, model {found.model}"
        )
        print(
            f"credential {report.credential} from {found.api_key_source!r},"
            f" tools {len(found.tools)}, permission mode {found.permission_mode}"
        )
        print("capabilities " + ", ".join(found.capabilities))
    print(f"turn {report.subtype} in {report.num_turns}, reply {report.text!r}")
    return exits.OK if report.subtype == "success" else exits.FAILURE


def default_operator() -> str:
    """The operator identifier for this user, from the login name when it is valid."""
    try:
        name = getpass.getuser().lower()
    except OSError:
        return "operator"
    cleaned = re.sub(r"[^a-z0-9._-]", "-", name).strip("-._")[:64]
    return cleaned or "operator"


def _request(
    paths: ProjectPaths, command: str, operation: Operation, args: dict[str, object]
) -> int:
    try:
        response = call(paths.socket, operation, args=args)
    except DaemonNotRunning:
        print(f"dromon {command}: not running for {paths.root}", file=sys.stderr)
        return exits.NOT_RUNNING
    except (ProtocolError, TimeoutError, OSError) as error:
        print(f"dromon {command}: {error}", file=sys.stderr)
        return exits.FAILURE
    if not response.ok or response.result is None:
        print(f"dromon {command}: {response.error_message}", file=sys.stderr)
        return exits.FAILURE
    for key, value in sorted(response.result.items()):
        print(f"{key} {value}")
    return exits.OK


def _config_document(arguments: argparse.Namespace) -> dict[str, object] | None:
    """The configuration the tool options describe, or ``None`` if none was given.

    An ``--mcp-config`` file is read here, by the operator's client, and its contents
    travel to the daemon, which stores them in the event log, credentials included.
    A ``--sandbox-config`` file holds a Claude Code sandbox object, or a settings file
    whose ``sandbox`` key is taken. It is validated here with operator authority. A
    ``--settings-config`` file is a Claude Code settings file whose supported keys are
    taken, its ``sandbox`` key included, and any other key is an error.
    """
    tools: list[str] = arguments.tool or []
    allowed: list[str] = arguments.allow_tool or []
    mcp_file: Path | None = arguments.mcp_config
    sandbox_file: Path | None = arguments.sandbox_config
    settings_file: Path | None = arguments.settings_config
    effort: str | None = arguments.effort
    allow_all: bool = arguments.allow_all_tools
    inherit: bool = arguments.inherit_account_mcp
    instructions: list[str] = arguments.instructions or []
    prompt: bool = arguments.claude_code_prompt
    given = (
        tools,
        allowed,
        mcp_file,
        allow_all,
        inherit,
        sandbox_file,
        settings_file,
        instructions,
        prompt,
    )
    if not any(given) and effort is None:
        return None
    document: dict[str, object] = {
        "builtin_tools": tools,
        "allowed_tools": allowed,
        "tool_policy": "allow_all" if allow_all else "allowlist",
        "inherit_account_mcp": inherit,
    }
    if mcp_file is not None:
        document["mcp_servers"] = json.loads(mcp_file.read_text(encoding="utf-8"))
    if sandbox_file is not None:
        loaded: object = json.loads(sandbox_file.read_text(encoding="utf-8"))
        document["sandbox"] = sandbox_from_settings(loaded).document()
    if settings_file is not None:
        loaded = json.loads(settings_file.read_text(encoding="utf-8"))
        settings, sandbox = split_settings_file(loaded)
        if settings is not None:
            document["settings"] = settings.document()
        if sandbox is not None:
            if "sandbox" in document:
                raise ValueError(
                    "the settings file has a sandbox, and so does --sandbox-config"
                )
            document["sandbox"] = sandbox_from_settings(sandbox).document()
    if effort is not None:
        document["effort"] = effort
    if instructions:
        document["instructions"] = instructions
    if prompt:
        document["claude_code_prompt"] = True
    return document


def _create(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "line_id": arguments.line_id,
        "model": arguments.model,
        "strategy": arguments.strategy,
    }
    try:
        config = _config_document(arguments)
    except (OSError, ValueError) as error:
        print(
            f"dromon create: cannot read the line configuration: {error}",
            file=sys.stderr,
        )
        return exits.FAILURE
    if config is not None:
        args["config"] = config
    return _request(paths, "create", Operation.CREATE_LINE, args)


def _configure(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    try:
        config = _config_document(arguments) or {}
    except (OSError, ValueError) as error:
        print(
            f"dromon configure: cannot read the line configuration: {error}",
            file=sys.stderr,
        )
        return exits.FAILURE
    args: dict[str, object] = {"line_id": arguments.line_id, "config": config}
    return _request(paths, "configure", Operation.CONFIGURE_LINE, args)


def _send(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "recipient": arguments.recipient,
        "body": arguments.body,
        "operator": arguments.operator,
    }
    if arguments.reply_to is not None:
        args["reply_to"] = arguments.reply_to
    return _request(paths, "send", Operation.SEND, args)


def _questions(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {} if arguments.all else {"operator": arguments.operator}
    try:
        response = call(paths.socket, Operation.QUESTIONS, args=args)
    except DaemonNotRunning:
        print(f"dromon questions: not running for {paths.root}", file=sys.stderr)
        return exits.NOT_RUNNING
    except (ProtocolError, TimeoutError, OSError) as error:
        print(f"dromon questions: {error}", file=sys.stderr)
        return exits.FAILURE
    if not response.ok or response.result is None:
        print(f"dromon questions: {response.error_message}", file=sys.stderr)
        return exits.FAILURE
    found = response.result.get("questions")
    entries = cast(list[dict[str, object]], found) if isinstance(found, list) else []
    if not entries:
        print("no open questions")
    for entry in entries:
        print(
            f"{entry.get('question_id')} from {entry.get('line')}"
            f" to {entry.get('operator')}"
        )
        for text_line in str(entry.get("text", "")).splitlines() or [""]:
            print(f"    {text_line}")
        choices = entry.get("choices")
        if isinstance(choices, list) and choices:
            print(
                "    choices: "
                + " | ".join(str(c) for c in cast(list[object], choices))
            )
    return exits.OK


def _mail(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "operator": arguments.operator,
        "unread_only": arguments.unread,
        "offset": arguments.offset,
        "limit": arguments.limit,
    }
    if arguments.sender is not None:
        args["sender"] = arguments.sender
    response = _call(paths, "mail", Operation.MAIL, args)
    if isinstance(response, int):
        return response
    result = response
    entries = cast(list[dict[str, object]], result.get("messages", []))
    print(
        f"{result.get('total')} matching, {result.get('unread')} unread,"
        f" {result.get('unanswered')} unanswered"
    )
    for entry in entries:
        parts = [f"{entry.get('message_id')} from {entry.get('sender')}"]
        parts.append(str(entry.get("kind")))
        if entry.get("reply_to") is not None:
            parts.append(f"replying to {entry.get('reply_to')}")
        parts.append("read" if entry.get("read") is True else "unread")
        if entry.get("outcome") != "unanswered":
            parts.append(str(entry.get("outcome")))
        print(", ".join(parts))
    return exits.OK


def _ack(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "operator": arguments.operator,
        "message_id": arguments.message_id,
    }
    return _request(paths, "ack", Operation.ACKNOWLEDGE, args)


def _sent(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "operator": arguments.operator,
        "unanswered_only": arguments.unanswered,
        "offset": arguments.offset,
        "limit": arguments.limit,
    }
    response = _call(paths, "sent", Operation.SENT, args)
    if isinstance(response, int):
        return response
    entries = cast(list[dict[str, object]], response.get("messages", []))
    print(f"{response.get('total')} sent")
    for entry in entries:
        delivery = (
            "read"
            if entry.get("read") is True
            else "delivered"
            if entry.get("delivered") is True
            else "waiting"
        )
        print(
            f"{entry.get('message_id')} to {entry.get('recipient')},"
            f" {entry.get('kind')}, {delivery}, {entry.get('outcome')}"
        )
    return exits.OK


def _attach(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    from dromon.attach import Attachment

    attachment = Attachment(paths, arguments.line_id, arguments.operator)
    if arguments.gui:
        from dromon import gui

        return gui.run(attachment, round(arguments.poll_seconds * 1000))
    from dromon import tui

    return tui.run(attachment, arguments.poll_seconds)


def _config(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Validate ``.dromon/dromon.toml`` without a daemon and summarize it."""
    del arguments
    from dromon.projectconfig import ConfigFileError, config_path, load

    file = config_path(paths.state_dir)
    if not file.exists():
        print(f"no project file at {file}. Built-in defaults apply.")
        return exits.OK
    try:
        config = load(file)
    except ConfigFileError as error:
        print(f"dromon config: {error}", file=sys.stderr)
        return exits.FAILURE
    limits = config.limits
    print(f"{file} is valid")
    print(
        f"limits: {limits.max_autonomous_turns} autonomous turns,"
        f" ${limits.max_cost_usd} estimated cost, {limits.cooldown_seconds}s cooldown"
    )
    print(f"wake every {config.wake_every_ticks} ticks")
    print(
        f"project: {_sandbox_summary(config.sandbox)},"
        f" {_settings_summary(config.settings)}"
    )
    if config.teacher is not None:
        teacher = config.teacher
        print(
            f"teacher: {teacher.model} at {teacher.endpoint},"
            f" context {teacher.context},"
            f" keep alive {teacher.keep_alive},"
            + (" autostart" if teacher.autostart else " started with dromon teacher up")
        )
    for line_id, line in sorted(config.lines.items()):
        line_settings = engine_settings.merge([config.settings, line.config.settings])
        tools = ", ".join(line.config.builtin_tools) or "no built-in tools"
        servers = ", ".join(line.config.server_names()) or "no MCP servers"
        print(
            f"line {line_id}: {line.model} {line.strategy},"
            f" {line.config.tool_policy}, {tools}, {servers},"
            f" {_sandbox_summary(merge([config.sandbox, line.config.sandbox]))},"
            f" {_settings_summary(line_settings)}"
            + (f", effort {line.config.effort}" if line.config.effort else "")
        )
    return exits.OK


def _agent(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Register, list or revoke external agents, through the daemon."""
    action: str = arguments.action
    name: str | None = arguments.name
    if action != "list" and name is None:
        print(f"dromon agent {action}: name the agent", file=sys.stderr)
        return exits.USAGE
    if action == "list":
        operation, args = Operation.AGENT_LIST, {}
    elif action == "add":
        operation = Operation.AGENT_ADD
        args = {"agent": name, "access": arguments.access}
    else:
        operation, args = Operation.AGENT_REVOKE, {"agent": name}
    try:
        response = call(paths.socket, operation, args=cast(dict[str, object], args))
    except DaemonNotRunning:
        print(f"dromon agent: no daemon is running for {paths.root}", file=sys.stderr)
        return exits.NOT_RUNNING
    except (ProtocolError, TimeoutError, OSError) as error:
        print(f"dromon agent: {error}", file=sys.stderr)
        return exits.FAILURE
    if not response.ok or response.result is None:
        print(f"dromon agent: {response.error_message}", file=sys.stderr)
        return exits.FAILURE
    result = response.result
    if action == "list":
        policy = cast(dict[str, object], result["policy"])
        listening = "listening" if policy["listen"] else "not listening"
        print(
            f"agents {listening}, anonymous {policy['anonymous']},"
            f" at most {policy['max_access']}"
        )
        for entry in cast(list[dict[str, object]], result["agents"]):
            how = "token" if entry["token"] else "anonymous"
            revoked = ", revoked" if entry["revoked"] else ""
            print(f"{entry['agent']} {entry['access']}, {how}{revoked}")
        return exits.OK
    if action == "revoke":
        print(f"revoked {result['agent']}")
        return exits.OK
    print(
        f"registered {result['agent']} with {result['access']} access,"
        f" effective {result['effective_access']}"
    )
    if not result["listening"]:
        print("the project file does not set [agents] listen, so it cannot connect yet")
    print(f"token {result['token']}")
    print("The token is shown once. An MCP configuration for the agent:")
    server = {
        "command": _dromon_command(),
        "args": ["--project", str(paths.root), "mcp", "--agent", name],
        "env": {TOKEN_ENV: result["token"]},
    }
    print(json.dumps({"mcpServers": {"dromon": server}}, indent=2))
    return exits.OK


def _dromon_command() -> str:
    """The path of this ``dromon``, so an MCP configuration starts the same one."""
    launched = Path(sys.argv[0])
    return str(launched.resolve()) if launched.is_file() else "dromon"


def _mcp(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Serve one external agent over MCP on standard input and output."""
    from dromon.mcp import Bridge, serve

    agent: str = arguments.agent
    token = os.environ.get(TOKEN_ENV) or None
    return serve(Bridge(paths.agent_socket, agent, token), sys.stdin, sys.stdout)


def _teacher(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    """Bring the project's teacher up, report it, or release it, without a daemon."""
    from dromon.projectconfig import ConfigFileError, config_path, load
    from dromon.teacher import Teacher, TeacherError

    file = config_path(paths.state_dir)
    try:
        config = load(file) if file.exists() else None
    except ConfigFileError as error:
        print(f"dromon teacher: {error}", file=sys.stderr)
        return exits.FAILURE
    if config is None or config.teacher is None:
        print(f"dromon teacher: {file} declares no [teacher]", file=sys.stderr)
        return exits.FAILURE
    teacher = Teacher(config.teacher, paths.runtime_dir)
    action: str = arguments.action
    try:
        if action == "up":
            print(f"dromon teacher: loading {config.teacher.model}")
            found = teacher.up(force=arguments.force)
        elif action == "down":
            found = teacher.down()
        else:
            found = teacher.status()
    except TeacherError as error:
        print(f"dromon teacher: {error}", file=sys.stderr)
        return exits.FAILURE
    print(
        f"teacher {config.teacher.model} at {config.teacher.endpoint}:"
        f" {found.summary()}"
    )
    if action == "status":
        return exits.OK
    return exits.OK if found.loaded == (action == "up") else exits.FAILURE


def _settings_summary(settings: SettingsConfig | None) -> str:
    return "no engine settings" if settings is None else settings.summary()


def _sandbox_summary(sandbox: SandboxConfig | None) -> str:
    if sandbox is None:
        return "no sandbox settings"
    return "sandboxed" if sandbox.enabled else "not sandboxed"


def _release(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {"line_id": arguments.line_id}
    return _request(paths, "release", Operation.RELEASE, args)


def _read(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "operator": arguments.operator,
        "message_id": arguments.message_id,
    }
    response = _call(paths, "read", Operation.READ, args)
    if isinstance(response, int):
        return response
    reply = response.get("reply_to")
    suffix = f", replying to {reply}" if reply is not None else ""
    print(f"{response.get('message_id')} from {response.get('sender')}{suffix}")
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        print("choices: " + " | ".join(str(c) for c in cast(list[object], choices)))
    print()
    print(response.get("body"))
    return exits.OK


def _call(
    paths: ProjectPaths, command: str, operation: Operation, args: dict[str, object]
) -> dict[str, object] | int:
    """The result of one request, or the exit status that reports its failure."""
    try:
        response = call(paths.socket, operation, args=args)
    except DaemonNotRunning:
        print(f"dromon {command}: not running for {paths.root}", file=sys.stderr)
        return exits.NOT_RUNNING
    except (ProtocolError, TimeoutError, OSError) as error:
        print(f"dromon {command}: {error}", file=sys.stderr)
        return exits.FAILURE
    if not response.ok or response.result is None:
        print(f"dromon {command}: {response.error_message}", file=sys.stderr)
        return exits.FAILURE
    return response.result


def _answer(paths: ProjectPaths, arguments: argparse.Namespace) -> int:
    args: dict[str, object] = {
        "question_id": arguments.question_id,
        "body": arguments.body,
        "operator": arguments.operator,
    }
    return _request(paths, "answer", Operation.ANSWER, args)


IMPLEMENTED: Final[dict[str, Callable[[ProjectPaths, argparse.Namespace], int]]] = {
    "start": _start,
    "status": _status,
    "stop": _stop,
    "probe": _probe,
    "create": _create,
    "configure": _configure,
    "send": _send,
    "questions": _questions,
    "answer": _answer,
    "mail": _mail,
    "read": _read,
    "ack": _ack,
    "sent": _sent,
    "release": _release,
    "attach": _attach,
    "config": _config,
    "teacher": _teacher,
    "agent": _agent,
    "mcp": _mcp,
}


def _add_driver_options(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--model", choices=MODELS, default=PROBE_MODEL, help="default %(default)s"
    )
    command.add_argument(
        "--strategy",
        choices=[strategy.value for strategy in Strategy],
        default=Strategy.SUBSCRIPTION.value,
        help="the driver's credential strategy, default %(default)s",
    )


def _add_tool_options(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--tool", action="append", help="a built-in tool such as Read, repeatable"
    )
    command.add_argument(
        "--mcp-config",
        type=Path,
        default=None,
        help="a JSON file of MCP servers, in the mcpServers shape",
    )
    command.add_argument(
        "--sandbox-config",
        type=Path,
        default=None,
        help="a JSON file of Claude Code sandbox settings for the line's commands",
    )
    command.add_argument(
        "--settings-config",
        type=Path,
        default=None,
        help="a Claude Code settings file of permissions, hooks, env and attribution",
    )
    command.add_argument(
        "--instructions",
        action="append",
        help="a file appended to the system prompt, relative to the project root,"
        " repeatable",
    )
    command.add_argument(
        "--claude-code-prompt",
        action="store_true",
        help="run on Claude Code's own system prompt instead of an empty one",
    )
    command.add_argument(
        "--effort",
        choices=EFFORTS,
        default=None,
        help="the reasoning effort, instead of the one the driver declares",
    )
    command.add_argument(
        "--allow-tool",
        action="append",
        help="an allowed tool name or mcp__server__*, repeatable",
    )
    command.add_argument(
        "--allow-all-tools",
        action="store_true",
        help="allow every tool the engine offers, instead of an allowlist",
    )
    command.add_argument(
        "--inherit-account-mcp",
        action="store_true",
        help="also load the account's MCP servers and claude.ai connectors",
    )


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``dromon``."""
    parser = argparse.ArgumentParser(
        prog="dromon",
        description="Client for the Dromon multi-agent harness daemon.",
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
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    for name, help_text in SUBCOMMANDS.items():
        command = commands.add_parser(name, help=help_text, description=help_text)
        if name == "release":
            command.add_argument("line_id", help="the held line")
        if name == "attach":
            command.add_argument("line_id", help="the line to attach to")
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="who the client speaks for, default %(default)s",
            )
            command.add_argument(
                "--gui", action="store_true", help="open a graphical window instead"
            )
            command.add_argument(
                "--poll-seconds",
                type=float,
                default=1.0,
                help="how often to refresh, default %(default)s",
            )
        if name in ("create", "configure"):
            command.add_argument("line_id", help="the line's identifier")
            _add_tool_options(command)
        if name in ("create", "probe"):
            _add_driver_options(command)
        if name == "agent":
            command.add_argument("action", choices=["add", "list", "revoke"])
            command.add_argument(
                "name", nargs="?", help="the agent, for add and revoke"
            )
            command.add_argument(
                "--access",
                choices=[level.value for level in AgentAccess],
                default=AgentAccess.MESSAGE.value,
                help="what the agent may do, for add, default %(default)s",
            )
        if name == "mcp":
            command.add_argument(
                "--agent",
                required=True,
                help="the agent's name. Its token is read from DROMON_AGENT_TOKEN",
            )
        if name == "teacher":
            command.add_argument(
                "action",
                choices=["up", "status", "down"],
                nargs="?",
                default="status",
                help="default %(default)s",
            )
            command.add_argument(
                "--force",
                action="store_true",
                help="load the model even when too little memory is available",
            )
        if name == "questions":
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="whose questions to list, default %(default)s",
            )
            command.add_argument(
                "--all", action="store_true", help="list every operator's questions"
            )
        if name == "answer":
            command.add_argument("question_id", help="the question answered")
            command.add_argument("body", help="the answer")
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="the operator answering, default %(default)s",
            )
        if name in ("mail", "read", "ack", "sent"):
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="whose mailbox, default %(default)s",
            )
        if name == "mail":
            command.add_argument("--unread", action="store_true", help="unread only")
            command.add_argument(
                "--from", dest="sender", default=None, help="one sender"
            )
            command.add_argument(
                "--offset", type=int, default=0, help="matches to skip"
            )
            command.add_argument(
                "--limit", type=int, default=20, help="at most this many, default 20"
            )
        if name in ("read", "ack"):
            command.add_argument("message_id", help="the message")
        if name == "sent":
            command.add_argument(
                "--unanswered", action="store_true", help="only mail awaiting an answer"
            )
            command.add_argument(
                "--offset", type=int, default=0, help="messages to skip"
            )
            command.add_argument(
                "--limit", type=int, default=20, help="at most this many, default 20"
            )
        if name == "send":
            command.add_argument("recipient", help="a line, or an operator as @name")
            command.add_argument("body", help="the message text")
            command.add_argument(
                "--operator",
                default=default_operator(),
                help="the operator sending it, default %(default)s",
            )
            command.add_argument(
                "--reply-to", default=None, help="the message answered"
            )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``dromon`` and return its exit status."""
    arguments = build_parser().parse_args(argv)
    command: str = arguments.command
    handler = IMPLEMENTED.get(command)
    if handler is None:
        print(f"dromon {command}: not implemented in this version", file=sys.stderr)
        return exits.NOT_IMPLEMENTED
    project: Path = arguments.project
    runtime_dir: Path | None = arguments.runtime_dir
    try:
        paths = project_paths(project, runtime_dir)
    except PathError as error:
        print(f"dromon {command}: {error}", file=sys.stderr)
        return exits.FAILURE
    return handler(paths, arguments)


if __name__ == "__main__":
    sys.exit(main())
