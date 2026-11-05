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
is not a quoted ``MAJOR.MINOR`` version, a duplicate and an unreadable file all fail
with a message.

**Versions added another way (round 15 review, V1).** A matrix can add a version with
an ``include`` entry such as ``- python: "3.13"``, which a reader of the one flow line
would miss, so the gate would check fewer versions than the workflow tests. Rather than
parse YAML, which the standard library cannot, this refuses any workflow with another
``python`` key beside the one matrix line, in block, entry or inline form, and any
``include`` or ``exclude`` key, which add or remove combinations this does not model.
An unquoted entry such as ``3.10`` is refused as well, because YAML reads it as the
number 3.1. A workflow in a shape this does not read fails and is not guessed at.
"""

import re
import sys
from pathlib import Path

WORKFLOW = Path(".github/workflows/ci.yml")
LINE = re.compile(r"^\s*python:\s*\[(?P<items>[^\]]*)\]\s*(?:#.*)?$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+$")
KEY = re.compile(r"""(?<![\w.$-])["']?python["']?\s*:""")
"""A ``python`` key anywhere on a line, in block form, as a list entry or inline in a
flow mapping. ``python-version:`` and ``matrix.python`` are not keys of that name."""
COMBINATIONS = re.compile(r"""(?<![\w.$-])["']?(?:include|exclude)["']?\s*:""")


class WorkflowError(Exception):
    """The workflow does not list its Python versions in the form this reads."""


def versions(text: str) -> list[str]:
    """The versions of the one ``python: [...]`` line of ``text``, in order."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    found = [match for line in lines if (match := LINE.match(line))]
    if len(found) != 1:
        raise WorkflowError(
            f"expected exactly one 'python: [...]' matrix line, found {len(found)}"
        )
    keys = sum(1 for line in lines if KEY.search(line.split(" #", 1)[0]))
    if keys != 1:
        raise WorkflowError(
            f"found {keys} 'python' keys, and only the one matrix line can be read."
            " Another, such as an include entry, would add a version this misses"
        )
    if any(COMBINATIONS.search(line.split(" #", 1)[0]) for line in lines):
        raise WorkflowError(
            "the workflow has an include or exclude key, which adds or removes"
            " combinations this does not read"
        )
    listed: list[str] = []
    for item in found[0].group("items").split(","):
        stripped = item.strip()
        if len(stripped) < 2 or stripped[0] not in "\"'" or stripped[-1] != stripped[0]:
            raise WorkflowError(
                f"{stripped!r} is not quoted, and YAML reads an unquoted 3.10 as 3.1"
            )
        version = stripped[1:-1]
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
