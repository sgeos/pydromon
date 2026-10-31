"""Viewing a local image, as an operator-granted capability named ``ViewImage``.

**Round 11.** A line that must look at a screenshot needs the engine to deliver the
image to the model. Each engine can, but not in the same way, and none of them was
ever allowed to. A line is given the capability by naming ``ViewImage`` among its
``builtin_tools``, as it names ``Read``, and without it no line can view an image.
Image generation and editing are not part of it and stay refused on every engine.

How each engine delivers it, and what Dromon enforces itself.

* Claude Code. ``Read`` returns an image to the model. ``ViewImage`` offers ``Read``
  and the gate then allows it only for a path that ``image_refusal`` accepts. A line
  that has ``Read`` already has more than ``ViewImage``, and is not narrowed by it.
* Codex. ``view_image`` is a Codex feature that Dromon keeps disabled unless the line
  has ``ViewImage``. The permission profile bounds what it can read, and Dromon checks
  the path of each ``imageView`` item against ``image_refusal`` when the item starts.
* Grok. Its ``read_file`` tool is the only one that shows a local image, and it also
  reads text. A line with ``ViewImage`` and not ``Read`` may make ``read_file`` calls
  only for a path that ``image_refusal`` accepts. Grok's image generation and editing
  tools are not permitted.

**Trust.** The path comes from the model, so it is untrusted data, and the check is
made on the real path. **Failure mode.** The check and the engine's read are two steps,
so a file replaced between them is not caught, and a symbolic link swapped in is
caught only at the check. The size limit is on the file's bytes, which bounds the
transfer and not the model's cost, since that follows the image's dimensions, and
Dromon does not decode an image, only checks that the whole file is one complete
container of the type its name claims.
"""

import os
import stat
from pathlib import Path
from typing import Final

from dromon.imageformat import CHECKERS, container_refusal

VIEW_IMAGE: Final = "ViewImage"
"""The capability's name among a line's ``builtin_tools``."""

MAX_IMAGE_BYTES: Final = 5 * 1024 * 1024
"""The largest image file a line may view, 5 MiB. **Rationale, an inference from
recollection and not verified here.** Anthropic's API is understood to refuse an image
over 5 MB, which is the lowest limit among the three providers' documented ones that
the author knows, so one bound keeps every engine within the lowest. A screenshot of a
user interface is well under it. A larger file is refused with a message that names the
limit, so the line can save a smaller capture."""

MAX_PATH_CHARACTERS: Final = 4096
"""The longest path text checked. A longer one is refused unread."""

PROTECTED_NAMES: Final = frozenset({".dromon", ".git", "secret"})
"""Directory names under the working directory that this capability never reads from,
whatever the engine's own profile says, since they hold state, history and secrets."""


def claude_tools(builtin_tools: tuple[str, ...]) -> list[str]:
    """The built-in tool names Claude Code is started with for ``builtin_tools``. Its
    ``ViewImage`` is a Dromon name, offered to the engine as ``Read``."""
    names = {"Read" if name == VIEW_IMAGE else name for name in builtin_tools}
    return sorted(names)


def view_only(builtin_tools: tuple[str, ...]) -> bool:
    """Whether the tools give ``ViewImage`` without ``Read``, so reading is limited to
    images that ``image_refusal`` accepts."""
    return VIEW_IMAGE in builtin_tools and "Read" not in builtin_tools


def image_refusal(
    raw_path: object, workdir: Path, limit: int = MAX_IMAGE_BYTES
) -> str | None:
    """Why a line may not view ``raw_path``, or ``None`` if it may. A relative path is
    taken from ``workdir``. The real path must stay inside the working directory's
    real path and outside ``PROTECTED_NAMES``, must be a regular file with an image
    suffix, no larger than ``limit``, and wholly one complete container of that type,
    as ``dromon.imageformat`` checks. **Review of 1045994, I1.**"""
    if not isinstance(raw_path, str) or not raw_path:
        return "the image path is missing or is not text"
    if len(raw_path) > MAX_PATH_CHARACTERS or "\x00" in raw_path:
        return "the image path is too long or holds a null character"
    root = Path(os.path.realpath(workdir))
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        real = Path(os.path.realpath(candidate, strict=True))
    except (OSError, RuntimeError):
        return f"the image {raw_path!r} does not exist or cannot be resolved"
    if real != root and root not in real.parents:
        return f"the image {raw_path!r} is outside the line's working directory"
    if PROTECTED_NAMES & set(real.relative_to(root).parts):
        return f"the image {raw_path!r} is in a directory this capability never reads"
    suffix = real.suffix.lower()
    if suffix not in CHECKERS:
        return (
            f"the file {raw_path!r} is not a png, jpg, jpeg, gif or webp image by its"
            " name"
        )
    # A path is classified before it is opened, and opened without blocking, since
    # opening a named pipe waits for a writer and would stall the daemon.
    try:
        if not stat.S_ISREG(os.stat(real).st_mode):
            return f"the image {raw_path!r} is not a regular file"
        flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        descriptor = os.open(real, flags)
    except OSError as error:
        return f"the image {raw_path!r} cannot be opened: {error.strerror}"
    try:
        found = os.fstat(descriptor)
        if not stat.S_ISREG(found.st_mode):
            return f"the image {raw_path!r} is not a regular file"
        if found.st_size > limit:
            return (
                f"the image {raw_path!r} is {found.st_size} bytes, over the limit"
                f" of {limit}"
            )
        # The whole file is read, one byte past the limit so a file that grew since
        # the size was taken is caught, and checked as one complete container.
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            data = handle.read(limit + 1)
    except OSError as error:
        return f"the image {raw_path!r} cannot be read: {error.strerror}"
    finally:
        os.close(descriptor)
    if len(data) > limit:
        return (
            f"the image {raw_path!r} grew past the limit of {limit} while it was read"
        )
    why = container_refusal(suffix, data)
    if why is not None:
        return f"the file {raw_path!r} {why}"
    return None
