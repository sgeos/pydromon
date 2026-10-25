"""The ``dromon`` command, the umbrella client for the Dromon daemon.

``start``, ``status`` and ``stop`` talk to the project's daemon over its socket. The
other subcommands are placeholders that report they are not implemented and exit with
``exits.NOT_IMPLEMENTED``. The subcommand list is expected to grow with the protocol.
"""

import argparse
import asyncio
import getpass
import json
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final, cast

from dromon import __version__, exits
from dromon.client import DaemonFailed, DaemonNotRunning, call, is_running, start, stop
from dromon.paths import PathError, ProjectPaths, project_paths
from dromon.driver import MODELS, Driver, DriverError, Strategy
from dromon.engine import ClaudeCodeLine, EngineRefused
from dromon.protocol import Operation, ProtocolError

PROBE_MODEL: Final = "claude-haiku-5-5"
"""The least expensive subscription model, used for probes and tests by default."""
PROBE_PROMPT: Final = "Reply with the single word: ready"

SUBCOMMANDS: Final[dict[str, str]] = {
    "start": "start the daemon for this project",
    "create": "create a session line",
    "configure": "replace a session line's tools, MCP servers and tool policy",
    "stop": "stop the daemon",
    "status": "show the daemon and its session lines",
    "attach": "open the interactive client",
    "send": "send a message to a session line or an operator",
    "questions": "list open operator questions",
    "answer": "answer an operator question",
    "mail": "look through an operator's mailbox",
    "read": "open a message in an operator's mailbox",
    "probe": "run one short turn on the engine and report its capabilities",
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
        line = ClaudeCodeLine(driver, paths.root)
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
    """
    tools: list[str] = arguments.tool or []
    allowed: list[str] = arguments.allow_tool or []
    mcp_file: Path | None = arguments.mcp_config
    allow_all: bool = arguments.allow_all_tools
    inherit: bool = arguments.inherit_account_mcp
    if not (tools or allowed or mcp_file or allow_all or inherit):
        return None
    document: dict[str, object] = {
        "builtin_tools": tools,
        "allowed_tools": allowed,
        "tool_policy": "allow_all" if allow_all else "allowlist",
        "inherit_account_mcp": inherit,
    }
    if mcp_file is not None:
        document["mcp_servers"] = json.loads(mcp_file.read_text(encoding="utf-8"))
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
            f"dromon create: cannot read the MCP configuration: {error}",
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
            f"dromon configure: cannot read the MCP configuration: {error}",
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
        if entry.get("answered") is True:
            parts.append("answered")
        print(", ".join(parts))
    return exits.OK


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
        if name in ("create", "configure"):
            command.add_argument("line_id", help="the line's identifier")
            _add_tool_options(command)
        if name in ("create", "probe"):
            _add_driver_options(command)
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
        if name in ("mail", "read"):
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
        if name == "read":
            command.add_argument("message_id", help="the message to open")
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
