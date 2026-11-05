#!/usr/bin/env python3
"""The Python versions the continuous integration workflow tests, one per line.

**Why.** The gate once type-checked only the interpreter it ran on, and the workflow's
other interpreter found Python 3.12 errors it had missed. The gate now type-checks every
version the workflow lists, and reads the list from the workflow so that the two cannot
drift apart. Standard library only, so the gate can run it with ``python3 -I``.

**What it reads.** The matrix line ``python: ["3.12", "3.15"]`` of ``ci.yml``, which
must stay on one line in flow form. A workflow that lists its versions otherwise, or
none, is an error and not an empty list, because a gate that reads no versions would
check none and say it passed. **Failure modes.** More than one such line, an entry that
is not a ``MAJOR.MINOR`` version, a duplicate and an unreadable file all fail with a
message.
"""

import re
import sys
from pathlib import Path

WORKFLOW = Path(".github/workflows/ci.yml")
LINE = re.compile(r"^\s*python:\s*\[(?P<items>[^\]]*)\]\s*(?:#.*)?$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+$")


class WorkflowError(Exception):
    """The workflow does not list its Python versions in the form this reads."""


def versions(text: str) -> list[str]:
    """The versions of the one ``python: [...]`` line of ``text``, in order."""
    found = [match for line in text.splitlines() if (match := LINE.match(line))]
    if len(found) != 1:
        raise WorkflowError(
            f"expected exactly one 'python: [...]' matrix line, found {len(found)}"
        )
    listed: list[str] = []
    for item in found[0].group("items").split(","):
        version = item.strip().strip("\"'")
        if not VERSION.match(version):
            raise WorkflowError(f"{item.strip()!r} is not a MAJOR.MINOR version")
        if version in listed:
            raise WorkflowError(f"version {version} is listed twice")
        listed.append(version)
    return listed


def main(arguments: list[str]) -> int:
    path = Path(arguments[0]) if arguments else WORKFLOW
    try:
        listed = versions(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, WorkflowError) as error:
        print(f"ci_pythons.py: {path}: {error}", file=sys.stderr)
        return 1
    print("\n".join(listed))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
