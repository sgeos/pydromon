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
module was split out. Every file of a check is opened and its size read before any
is read, so a check whose files would pass the plan's allowance is refused by their
size before anything is read, and a refusal for size costs the daemon no reading.

**Round 15, item 4, merged with the review's D1.** A file whose exact bytes already
passed the list is answered from the memory of passes, with no match and no budget
spent. The plan carries that memory as digests and sizes. Only when every file's size
equals the size of a remembered pass are the files read and their digests compared,
so content that cannot be remembered is still refused unread once the budget is
spent or would be passed. Content read for that comparison is never matched unless
the budget allows the check.

**Porting note.** A port runs the same plan on a worker of its own and must, as here,
give the worker copies of the terms, paths and remembered digests rather than the
scheduler's state.
"""

import contextlib
import hashlib
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
    one check may read in all. ``part`` is the size a file is matched in.

    ``remembered`` holds the passes the line may be answered from, as ``(path,
    SHA-256)`` with an empty path for bytes that passed as a text or as written by
    ``write_scratch``, and ``remembered_sizes`` their sizes as ``(path, size)``.
    ``refusal`` is set when the budget is spent, and is then the answer to anything
    the memory does not cover. A text check is never answered from memory."""

    terms: tuple[str, ...]
    text: str | None
    root: Path | None
    protected: tuple[Path, ...]
    files: tuple[tuple[str, tuple[str, ...]], ...]
    allowance: int
    total: int
    part: int
    remembered: frozenset[tuple[str, str]] = frozenset()
    remembered_sizes: frozenset[tuple[str, int]] = frozenset()
    refusal: str | None = None


@dataclass(frozen=True, slots=True)
class CheckResult:
    """What a check found. ``matched`` counts the lines holding a term and ``read``
    the bytes matched. ``passed`` gives each file checked as ``(path, SHA-256,
    size)``, or the text as one entry with an empty path, so that a pass can be
    remembered. ``cached`` is true when every file was answered from memory, and
    then nothing was matched and ``read`` is zero."""

    matched: int
    read: int
    passed: tuple[tuple[str, str, int], ...]
    cached: bool = False


def ceiling_refusal(allowance: int, ceiling: int, window: int) -> str:
    """The refusal of a check whose content would pass the hourly ceiling."""
    return (
        f"the checked content would pass the ceiling of {ceiling} bytes checked in"
        f" {window} seconds, with {max(allowance, 0)} bytes left, so it was refused"
        " by its size before it was read"
    )


def count(plan: CheckPlan, ceiling: int, window: int) -> CheckResult:
    """What the plan's text or files hold. ``CheckRefused`` for a file that cannot
    be read or matched, for content past the allowance, and with the plan's refusal
    for content the memory does not cover once the budget is spent. Runs on a
    worker thread."""
    if plan.text is not None:
        if plan.refusal is not None:
            raise CheckRefused(plan.refusal)
        data = plan.text.encode("utf-8")
        if len(data) > plan.allowance:
            raise CheckRefused(ceiling_refusal(plan.allowance, ceiling, window))
        passed = (("", hashlib.sha256(data).hexdigest(), len(data)),)
        return CheckResult(
            disclosure.matching_lines(plan.terms, plan.text), len(data), passed
        )
    if plan.root is None:
        raise CheckRefused("cannot resolve the working directory")
    opened: list[tuple[str, tuple[str, ...], int, int]] = []
    try:
        for relative, accepted in plan.files:
            descriptor, size = open_file(plan.root, plan.protected, relative)
            opened.append((relative, accepted, descriptor, size))
        return _count_opened(plan, opened, (ceiling, window))
    finally:
        for _, _, descriptor, _ in opened:
            _close_quietly(descriptor)


def _count_opened(
    plan: CheckPlan,
    opened: list[tuple[str, tuple[str, ...], int, int]],
    ceiling: tuple[int, int],
) -> CheckResult:
    """The steps of ``count`` once every file is open and its size known. Each
    descriptor is closed by the caller, and reading one leaves it open."""
    combined = sum(size for _, _, _, size in opened)
    if combined > plan.total:
        if plan.allowance < plan.total:
            raise CheckRefused(ceiling_refusal(plan.allowance, *ceiling))
        raise CheckRefused(
            f"the checked files are over {plan.total} bytes combined"
            if len(opened) > 1
            else f"the supplied file is over {plan.total} bytes"
        )
    contents: list[bytes | None] = [None] * len(opened)
    if all(_size_remembered(plan, name, size) for name, _, _, size in opened):
        cached = True
        for index, (name, _, descriptor, size) in enumerate(opened):
            data = _read(descriptor, size)
            contents[index] = data
            digest = hashlib.sha256(data).hexdigest()
            if ("", digest) not in plan.remembered and (
                name,
                digest,
            ) not in plan.remembered:
                # The rest is read only if the budget allows the check.
                cached = False
                break
        if cached:
            return CheckResult(0, 0, (), cached=True)
    if plan.refusal is not None:
        raise CheckRefused(plan.refusal)
    if combined > plan.allowance:
        raise CheckRefused(ceiling_refusal(plan.allowance, *ceiling))
    limit = min(plan.total, plan.allowance)
    matched = 0
    read = 0
    passed: list[tuple[str, str, int]] = []
    for index, (relative, accepted, descriptor, _) in enumerate(opened):
        data = contents[index]
        if data is None:
            data = _read(descriptor, limit - read)
        read += len(data)
        if read > limit:
            raise CheckRefused(
                "the supplied file grew while it was checked, so check it again"
            )
        try:
            matched += disclosure.matching_lines_in_parts(
                plan.terms, data, plan.part, accepted
            )
        except disclosure.LineTooLong as error:
            raise CheckRefused(
                f"the supplied file has a line longer than {plan.part} bytes, which"
                " cannot be checked in parts without risking a term across a cut"
            ) from error
        passed.append((relative, hashlib.sha256(data).hexdigest(), len(data)))
    return CheckResult(matched, read, tuple(passed))


def _size_remembered(plan: CheckPlan, path: str, size: int) -> bool:
    return (path, size) in plan.remembered_sizes or ("", size) in plan.remembered_sizes


def _read(descriptor: int, limit: int) -> bytes:
    """At most ``limit`` and one more bytes of the open file, so a caller sees that
    a file grew past ``limit``. Each file is read once, from its start. The
    descriptor stays open for the caller to close."""
    with os.fdopen(descriptor, "rb", closefd=False) as handle:
        return handle.read(max(limit, 0) + 1)


def _close_quietly(descriptor: int) -> None:
    with contextlib.suppress(OSError):
        os.close(descriptor)


def open_file(root: Path, protected: tuple[Path, ...], path: str) -> tuple[int, int]:
    """An open descriptor of the file at ``path`` under ``root`` and its size, or
    ``CheckRefused``. The caller closes the descriptor. Bytes that are not UTF-8 are
    replaced by the matcher, which can only hide a term written in them."""
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
    facts = os.fstat(descriptor)
    if not stat.S_ISREG(facts.st_mode):
        os.close(descriptor)
        raise CheckRefused("the supplied file is not a regular file")
    if facts.st_nlink != 1 or facts.st_uid != os.getuid():
        os.close(descriptor)
        raise CheckRefused("the supplied file has more than one name or another owner")
    return descriptor, facts.st_size
