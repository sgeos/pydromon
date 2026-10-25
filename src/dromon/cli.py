"""The ``dromon`` command, the umbrella client for the Dromon daemon.

``start``, ``status`` and ``stop`` talk to the project's daemon over its socket. The
other subcommands are placeholders that report they are not implemented and exit with
``exits.NOT_IMPLEMENTED``. The subcommand list is expected to grow with the protocol.
"""

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from dromon import __version__, exits
from dromon.client import DaemonFailed, DaemonNotRunning, call, is_running, start, stop
from dromon.paths import PathError, ProjectPaths, project_paths
from dromon.driver import Driver, DriverError
from dromon.engine import ClaudeCodeLine, EngineRefused
from dromon.protocol import Operation, ProtocolError

PROBE_MODEL: Final = "claude-opus-5-5"
PROBE_PROMPT: Final = "Reply with the single word: ready"

SUBCOMMANDS: Final[dict[str, str]] = {
    "start": "start the daemon for this project",
    "stop": "stop the daemon",
    "status": "show the daemon and its session lines",
    "attach": "open the interactive client",
    "send": "send a message to a session line",
    "questions": "list open operator questions",
    "answer": "answer an operator question",
    "probe": "run one short turn on the engine and report its capabilities",
}
"""Planned subcommands and their one-line help, in display order."""


def _start(paths: ProjectPaths) -> int:
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


def _status(paths: ProjectPaths) -> int:
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
    print(f"project {result.get('project')}")
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


def _stop(paths: ProjectPaths) -> int:
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


def _probe(paths: ProjectPaths) -> int:
    """Run one turn with no tools on the subscription and report what the engine says.

    It uses a small amount of the operator's plan, and never a per-token credential.
    """
    try:
        driver = Driver.for_model(PROBE_MODEL)
        line = ClaudeCodeLine(driver, paths.root)
        report = asyncio.run(line.run_turn(PROBE_PROMPT))
    except (DriverError, EngineRefused) as error:
        print(f"dromon probe: {error}", file=sys.stderr)
        return exits.FAILURE
    settings = driver.settings
    print(f"driver {PROBE_MODEL}, wcet {driver.wcet_per_iteration} per decision")
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
            f"credential {found.api_key_source} (subscription),"
            f" tools {len(found.tools)}, permission mode {found.permission_mode}"
        )
        print("capabilities " + ", ".join(found.capabilities))
    print(f"turn {report.subtype} in {report.num_turns}, reply {report.text!r}")
    return exits.OK if report.subtype == "success" else exits.FAILURE


IMPLEMENTED: Final[dict[str, Callable[[ProjectPaths], int]]] = {
    "start": _start,
    "status": _status,
    "stop": _stop,
    "probe": _probe,
}


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
        commands.add_parser(name, help=help_text, description=help_text)
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
    return handler(paths)


if __name__ == "__main__":
    sys.exit(main())
