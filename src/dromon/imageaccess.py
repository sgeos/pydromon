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
Dromon does not decode an image.
"""

import os
import stat
from pathlib import Path
from typing import Final

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

MAGIC_BYTES: Final[dict[str, tuple[bytes, ...]]] = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
}
"""The image types a line may view, by lowercase suffix, with the opening bytes each
must have. The content check stops a text file named ``x.png`` from being read through
this capability. ``.svg`` is excluded because it is text that can carry instructions.
For ``.webp`` the first four bytes only are compared and ``WEBP`` at byte 8 is checked
by ``image_refusal``."""

PROTECTED_NAMES: Final = frozenset({".dromon", ".git", "secret"})
"""Directory names under the working directory that this capability never reads from,
whatever the engine's own profile says, since they hold state, history and secrets."""

HEADER_BYTES: Final = 16


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
    suffix and the opening bytes of that type, and must be no larger than ``limit``."""
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
    magic = MAGIC_BYTES.get(suffix)
    if magic is None:
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
        header = os.read(descriptor, HEADER_BYTES)
    finally:
        os.close(descriptor)
    if not any(header.startswith(prefix) for prefix in magic):
        return f"the file {raw_path!r} does not begin as a {suffix[1:]} image"
    if suffix == ".webp" and header[8:12] != b"WEBP":
        return f"the file {raw_path!r} does not begin as a webp image"
    return None
