"""The ``dromond`` command, the Dromon daemon.

The seed parses its arguments, reports that the daemon is not implemented, and exits
with ``EXIT_NOT_IMPLEMENTED``. It opens no socket and starts no session line.
"""

import argparse
import sys
from collections.abc import Sequence

from dromon import __version__
from dromon.cli import EXIT_NOT_IMPLEMENTED


def build_parser() -> argparse.ArgumentParser:
    """Return the argument parser for ``dromond``."""
    parser = argparse.ArgumentParser(
        prog="dromond",
        description="The Dromon multi-agent harness daemon.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``dromond`` and return its exit status."""
    build_parser().parse_args(argv)
    print("dromond: not implemented in this version", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


if __name__ == "__main__":
    sys.exit(main())
