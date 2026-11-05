"""The part of ``check_disclosure`` that reads and matches, which runs off the loop.

**Round 15 review, D3, the operator's decision.** A check of 16 MiB held the daemon's
event loop for seconds, so every line, the client socket and the tick waited while
one line checked. The scheduler now plans a check on the loop, from its state, and
hands this module a ``CheckPlan`` of plain data, which a worker thread reads and
matches. The scheduler records the answer back on the loop. Nothing here reads or
changes the scheduler's state, so the thread shares no mutable object with the loop.

Every file is walked from the working directory's descriptor one component at a time
without following any link, and read only when it is a regular file of the daemon's
user with one name, outside every directory Dromon protects, as it was before this
module was split out. A file whose size would pass the plan's allowance is refused by
its size before it is read, so a refusal for size costs the daemon no reading.

**Porting note.** A port runs the same plan on a worker of its own and must, as here,
give the worker copies of the terms and paths rather than the scheduler's state.
"""

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from dromon import disclosure

MAX_CHECKED_TOTAL_BYTES: Final = 16 * 1024 * 1024
"""The most bytes one ``check_disclosure`` call reads, over all its files. The
scheduler exports it under the same name."""


class CheckRefused(ValueError):
    """The check cannot read or match what it was given. The message names the path
    or the bound, never a term."""


@dataclass(frozen=True, slots=True)
class CheckPlan:
    """What one check reads and matches, taken from the scheduler's state on the loop.

    ``text`` is set for a text check and ``files`` for a file check, each file as its
    path relative to ``root`` and the operator's accepted line digests for it.
    ``allowance`` is the bytes left under the hourly ceiling, and ``total`` the bytes
    one check may read in all. ``part`` is the size a file is matched in."""

    terms: tuple[str, ...]
    text: str | None
    root: Path | None
    protected: tuple[Path, ...]
    files: tuple[tuple[str, tuple[str, ...]], ...]
    allowance: int
    total: int
    part: int


def ceiling_refusal(allowance: int, ceiling: int, window: int) -> str:
    """The refusal of a check whose content would pass the hourly ceiling."""
    return (
        f"the checked content would pass the ceiling of {ceiling} bytes checked in"
        f" {window} seconds, with {max(allowance, 0)} bytes left, so it was refused"
        " by its size before it was read"
    )


def count(plan: CheckPlan, ceiling: int, window: int) -> tuple[int, int]:
    """How many lines of the plan's text or files hold a listed term, and how many
    bytes were read. ``CheckRefused`` for a file that cannot be read or matched, or
    for content past the allowance. Runs on a worker thread."""
    if plan.text is not None:
        size = len(plan.text.encode("utf-8"))
        if size > plan.allowance:
            raise CheckRefused(ceiling_refusal(plan.allowance, ceiling, window))
        return disclosure.matching_lines(plan.terms, plan.text), size
    if plan.root is None:
        raise CheckRefused("cannot resolve the working directory")
    matched = 0
    read = 0
    for relative, accepted in plan.files:
        data = read_file(
            plan.root,
            plan.protected,
            relative,
            plan.total - read,
            plan.allowance - read,
            (ceiling, window),
        )
        read += len(data)
        try:
            matched += disclosure.matching_lines_in_parts(
                plan.terms, data, plan.part, accepted
            )
        except disclosure.LineTooLong as error:
            raise CheckRefused(
                f"the supplied file has a line longer than {plan.part} bytes, which"
                " cannot be checked in parts without risking a term across a cut"
            ) from error
    return matched, read


def read_file(
    root: Path,
    protected: tuple[Path, ...],
    path: str,
    total: int,
    allowance: int,
    ceiling: tuple[int, int],
) -> bytes:
    """The contents of the file at ``path`` under ``root``, at most ``total`` bytes
    by the per-check bound and ``allowance`` by the hourly ceiling, whose limit and
    window ``ceiling`` gives for the refusal. ``CheckRefused`` otherwise. Bytes that
    are not UTF-8 are replaced by the matcher, which can only hide a term written in
    them."""
    if not path or "\x00" in path or path.startswith(("/", "~")):
        raise CheckRefused("path must be a relative path in your working directory")
    parts = Path(path).parts
    if ".." in parts or not parts:
        raise CheckRefused("path may not contain ..")
    candidate = root.joinpath(*parts)
    for guarded in protected:
        if candidate == guarded or guarded in candidate.parents:
            raise CheckRefused("the supplied file is in a directory Dromon protects")
    try:
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        raise CheckRefused(
            f"cannot open the working directory: {error.strerror}"
        ) from error
    try:
        for name in parts[:-1]:
            entered = os.open(
                name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent
            )
            os.close(parent)
            parent = entered
        descriptor = os.open(
            parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
    except OSError as error:
        raise CheckRefused(
            f"cannot read the supplied file: {error.strerror or type(error).__name__}"
        ) from error
    finally:
        os.close(parent)
    # Checked on the bare descriptor, since opening a directory as a file object
    # raises before any check could refuse it.
    facts = os.fstat(descriptor)
    if not stat.S_ISREG(facts.st_mode):
        os.close(descriptor)
        raise CheckRefused("the supplied file is not a regular file")
    if facts.st_nlink != 1 or facts.st_uid != os.getuid():
        os.close(descriptor)
        raise CheckRefused("the supplied file has more than one name or another owner")
    limit = min(total, allowance)
    raw = b""
    with os.fdopen(descriptor, "rb") as handle:
        # The size is read first, so a file past the limit is refused unread. The
        # read is still bounded, since a file may grow after the size is read.
        if facts.st_size <= limit:
            raw = handle.read(limit + 1)
    if facts.st_size > limit or len(raw) > limit:
        if allowance < total:
            raise CheckRefused(ceiling_refusal(allowance, *ceiling))
        # The words used before this module was split out, naming the bound and
        # not what is left of it.
        raise CheckRefused(
            f"the checked files are over {MAX_CHECKED_TOTAL_BYTES} bytes combined"
            if total < MAX_CHECKED_TOTAL_BYTES
            else f"the supplied file is over {MAX_CHECKED_TOTAL_BYTES} bytes"
        )
    return raw
