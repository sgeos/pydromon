"""A check of one sandbox rule for one path inside a line's own scratch directory.

**Round 15, item 6.** A reviewer line inferred what its sandbox allowed from the
sandbox document and from one failed command. This answers ``allowed`` or ``denied``
for a read or a write of a named path, with the rule that decided it, so a rule is
tested by asking and not by inferring.

**Round 15, review of 3344ca6.** A write answers from the document alone. The effective
document does not list the line's working directory, which Claude Code may grant by
default, so a path that only the working directory would cover is answered ``unknown``
with the directory named, and never ``allowed`` on an assumption the document does not
hold. A read of a path no ``denyRead`` entry covers is ``allowed`` for the same reason
the other way, since reads are open unless denied.

**What it is, stated plainly.** It evaluates the effective sandbox document the
engine was given, the same document ``line_status`` shows, against the path. It is a
**check of configuration, not a probe by the operating system**. The daemon cannot run
a process under Claude Code's own confinement, so a rule the engine's sandbox applies
differently from this reading, or a restriction the document does not state, is not
seen. It says so in every answer, in ``basis`` and ``operating_system_probe``.

**It never widens what a line may do.** It performs no read and no write of the named
path. It resolves links with ``os.path.realpath``, which reads link targets and no file
contents, and only for a path that lies inside the line's scratch directory by its
normalised text. A path outside the scratch directory is refused without being looked
at, a ``..`` that leaves it included. Nothing it returns names a file's content.

Failure modes. An unknown operation, an empty or oversize path, a path that leaves
the scratch directory and a line with no sandbox document are refused with the reason.
A document whose fields are not the expected shapes is read as having no entries for
them, which fails toward ``denied`` or ``unknown`` for a write and toward ``allowed``
for a read, so
the answer is stated with its rule and should be checked against ``line_status`` by a
reviewer who doubts it.

Porting note. Plain path arithmetic over lists of strings, which a port carries across.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final, cast

OPERATIONS: Final = ("read", "write")
MAX_PATH_CHARACTERS: Final = 1024


class ProbeRefused(ValueError):
    """The request cannot be answered, for the reason given."""


def _entries(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [entry for entry in cast("list[object]", value) if isinstance(entry, str)]


def _covered(target: Path, roots: list[str]) -> str | None:
    """The first root that is ``target`` or encloses it, or ``None``."""
    resolved = Path(os.path.realpath(target))
    for root in roots:
        base = Path(os.path.realpath(os.path.expanduser(root)))
        if resolved == base or base in resolved.parents:
            return root
    return None


def inside(scratch: Path, text: str) -> Path:
    """``text`` as a path inside ``scratch``, relative text taken from it. Raises
    ``ProbeRefused`` for text that is empty, too long, holds a NUL, or leaves
    ``scratch`` once normalised."""
    if not text or len(text) > MAX_PATH_CHARACTERS or "\x00" in text:
        raise ProbeRefused("path must be 1 to 1024 characters without a NUL")
    joined = os.path.normpath(os.path.join(scratch, text))
    base = os.path.normpath(scratch)
    if joined != base and not joined.startswith(base + os.sep):
        raise ProbeRefused("path must lie inside your own scratch directory")
    return Path(joined)


def probe(
    document: Mapping[str, object] | None,
    cwd: Path,
    scratch: Path,
    operation: str,
    text: str,
) -> dict[str, object]:
    """Whether ``operation`` on ``text`` is allowed by ``document``, with its rule.
    Raises ``ProbeRefused``."""
    if operation not in OPERATIONS:
        raise ProbeRefused(f"operation must be one of {', '.join(OPERATIONS)}")
    target = inside(scratch, text)
    if document is None or document.get("enabled") is not True:
        raise ProbeRefused("this line has no enabled sandbox document to check")
    filesystem_value = document.get("filesystem")
    filesystem = (
        cast("dict[str, object]", filesystem_value)
        if isinstance(filesystem_value, dict)
        else {}
    )
    answer: dict[str, object] = {
        "operation": operation,
        "path": str(target),
        "basis": "the effective sandbox document the engine was given",
        "operating_system_probe": False,
    }
    if filesystem.get("disabled") is True:
        answer.update(result="allowed", rule="the filesystem sandbox is disabled")
        return answer
    if operation == "write":
        denied = _covered(target, _entries(filesystem.get("denyWrite")))
        if denied is not None:
            answer.update(result="denied", rule=f"denyWrite {denied}")
            return answer
        granted = _covered(target, _entries(filesystem.get("allowWrite")))
        if granted is not None:
            answer.update(result="allowed", rule=f"allowWrite {granted}")
        elif _covered(target, [str(cwd)]) is not None:
            # Review of 3344ca6. The document does not list the working directory,
            # so a grant from the engine's own default is not shown by it and is not
            # claimed here.
            answer.update(
                result="unknown",
                rule=(
                    "no allowWrite entry of the document covers it, and the document"
                    " does not say whether the engine grants the working directory"
                ),
                working_directory=str(cwd),
            )
        else:
            answer.update(result="denied", rule="no allowWrite entry covers it")
        return answer
    blocked = _covered(target, _entries(filesystem.get("denyRead")))
    lifted = _covered(target, _entries(filesystem.get("allowRead")))
    if blocked is not None and lifted is None:
        answer.update(result="denied", rule=f"denyRead {blocked}")
    elif blocked is not None and lifted is not None:
        answer.update(
            result="allowed", rule=f"allowRead {lifted} lifts denyRead {blocked}"
        )
    else:
        answer.update(result="allowed", rule="no denyRead entry covers it")
    return answer
