"""The ``dromon`` command, the umbrella client for the Dromon daemon.

Every subcommand is a placeholder in the seed. Each one parses, then reports that it
is not implemented and exits with ``EXIT_NOT_IMPLEMENTED``. The subcommand list is
provisional and is expected to change as the client protocol is designed.
"""

import argparse
import sys
from collections.abc import Sequence
from typing import Final

from dromon import __version__

EXIT_NOT_IMPLEMENTED: Final = 3
"""Exit status of a placeholder. Distinct from argparse's usage error status 2."""

SUBCOMMANDS: Final[dict[str, str]] = {
    "start": "start the daemon for this project",
    "stop": "stop the daemon",
    "status": "show the daemon and its session lines",
    "attach": "open the interactive client",
    "send": "send a message to a session line",
    "questions": "list open operator questions",
    "answer": "answer an operator question",
}
"""Planned subcommands and their one-line help, in display order."""


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``dromon``."""
    parser = argparse.ArgumentParser(
        prog="dromon",
        description="Client for the Dromon multi-agent harness daemon.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)
    for name, help_text in SUBCOMMANDS.items():
        commands.add_parser(name, help=help_text, description=help_text)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``dromon`` and return its exit status."""
    arguments = build_parser().parse_args(argv)
    command: str = arguments.command
    print(f"dromon {command}: not implemented in this version", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
