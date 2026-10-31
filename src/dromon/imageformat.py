"""Whether a file's whole content is one complete image container.

**Review of 1045994, I1.** Checking a file's first bytes lets a file named ``x.png``
begin as a png and carry anything after it. The capability exists so that a line can
look at a screenshot, so a file is accepted only when its entire content is a single
well-formed container of the type its name claims. The walk follows the container's own
structure and checks that nothing precedes or follows it.

* PNG. The signature, then chunks that each carry a correct checksum, the first an
  ``IHDR`` of 13 bytes, at least one ``IDAT``, and an empty ``IEND`` as the last byte.
* JPEG. ``SOI``, then marker segments with their lengths, at least one frame header
  and one scan, the scan's entropy-coded data up to the next marker, and ``EOI`` as the
  last byte.
* GIF. The header, the logical screen with its colour table, then extension and image
  blocks with their sub-blocks, at least one image, and the trailer as the last byte.
* WebP. A ``RIFF`` length equal to the file's length less 8, ``WEBP``, and chunks that
  fill the rest exactly, each padded to an even length, the first a ``VP8``, ``VP8L``
  or ``VP8X``.

**Limits.** This proves the container is complete, not that the bytes decode as an
image. Entropy-coded and compressed data, comments and text chunks can still hold
arbitrary bytes, which a decoder renders as noise or ignores. It is a bound on what a
view-only line can read through the capability, not a decoder.

**Porting note.** Each loop advances its position by at least one byte and ends at the
data's length, so it is bounded by the file size, at most ``MAX_IMAGE_BYTES``.
"""

import zlib
from typing import Final

PNG_SIGNATURE: Final = b"\x89PNG\r\n\x1a\n"
PNG_CHUNK_OVERHEAD: Final = 12
"""A PNG chunk's length, type and checksum, in bytes."""

JPEG_NO_LENGTH: Final = frozenset({0x01, *range(0xD0, 0xD8)})
"""Markers with no length: ``TEM`` and the restart markers."""

JPEG_FRAMES: Final = frozenset(set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC})
"""The start-of-frame markers, which exclude ``DHT``, ``JPG`` and ``DAC``."""

GIF_HEADERS: Final = (b"GIF87a", b"GIF89a")
WEBP_FIRST: Final = (b"VP8 ", b"VP8L", b"VP8X")


def _big(data: bytes, at: int, width: int) -> int:
    return int.from_bytes(data[at : at + width], "big")


def _little(data: bytes, at: int, width: int) -> int:
    return int.from_bytes(data[at : at + width], "little")


def _png(data: bytes) -> str | None:
    if not data.startswith(PNG_SIGNATURE):
        return "does not begin with the png signature"
    position = len(PNG_SIGNATURE)
    first = True
    seen_data = False
    while position < len(data):
        if position + PNG_CHUNK_OVERHEAD > len(data):
            return "ends inside a png chunk"
        size = _big(data, position, 4)
        kind = data[position + 4 : position + 8]
        end = position + 8 + size + 4
        if end > len(data):
            return "has a png chunk longer than the file"
        if not kind.isalpha() or not kind.isascii():
            return "has a png chunk with a malformed type"
        if first and (kind != b"IHDR" or size != 13):
            return "does not begin with a png IHDR of 13 bytes"
        first = False
        body = data[position + 4 : position + 8 + size]
        if zlib.crc32(body) != _big(data, end - 4, 4):
            return "has a png chunk with a wrong checksum"
        seen_data = seen_data or kind == b"IDAT"
        position = end
        if kind == b"IEND":
            if size != 0:
                return "has a png IEND with content"
            if position != len(data):
                return "has content after the png IEND"
            return None if seen_data else "has no png image data"
    return "ends without a png IEND"


def _jpeg_scan(data: bytes, position: int) -> int | None:
    """The position of the marker that ends entropy-coded data starting at
    ``position``, or ``None`` if there is none."""
    while True:
        found = data.find(b"\xff", position)
        if found < 0 or found + 1 >= len(data):
            return None
        following = data[found + 1]
        if following == 0x00 or 0xD0 <= following <= 0xD7:
            position = found + 2
            continue
        return found


def _jpeg(data: bytes) -> str | None:
    if not data.startswith(b"\xff\xd8"):
        return "does not begin with the jpeg start marker"
    position = 2
    frame = scan = False
    while position < len(data):
        if data[position] != 0xFF:
            return "has jpeg bytes where a marker belongs"
        while position + 1 < len(data) and data[position + 1] == 0xFF:
            position += 1
        if position + 1 >= len(data):
            return "ends inside a jpeg marker"
        marker = data[position + 1]
        position += 2
        if marker == 0xD9:
            if position != len(data):
                return "has content after the jpeg end marker"
            return None if frame and scan else "has no jpeg frame and scan"
        if marker == 0xD8:
            return "has a second jpeg start marker"
        if marker in JPEG_NO_LENGTH:
            continue
        if position + 2 > len(data):
            return "ends inside a jpeg segment length"
        length = _big(data, position, 2)
        if length < 2 or position + length > len(data):
            return "has a jpeg segment longer than the file"
        position += length
        frame = frame or marker in JPEG_FRAMES
        if marker == 0xDA:
            scan = True
            ended = _jpeg_scan(data, position)
            if ended is None:
                return "has jpeg scan data that never ends"
            position = ended
    return "ends without the jpeg end marker"


def _skip_sub_blocks(data: bytes, position: int) -> int | None:
    """The position after a GIF's sub-blocks from ``position``, or ``None``."""
    while position < len(data):
        size = data[position]
        position += 1 + size
        if size == 0:
            return position if position <= len(data) else None
    return None


def _gif(data: bytes) -> str | None:
    if not data.startswith(GIF_HEADERS) or len(data) < 13:
        return "does not begin with a gif header and screen"
    position = 13
    if data[10] & 0x80:
        position += 3 * 2 ** ((data[10] & 7) + 1)
    images = 0
    while position < len(data):
        block = data[position]
        if block == 0x3B:
            if position + 1 != len(data):
                return "has content after the gif trailer"
            return None if images else "has no gif image"
        if block == 0x21:
            skipped = _skip_sub_blocks(data, position + 2)
        elif block == 0x2C and position + 10 <= len(data):
            images += 1
            packed = data[position + 9]
            position += 10
            if packed & 0x80:
                position += 3 * 2 ** ((packed & 7) + 1)
            skipped = _skip_sub_blocks(data, position + 1)
        else:
            return "has an unknown gif block"
        if skipped is None:
            return "has a gif block that runs past the file"
        position = skipped
    return "ends without the gif trailer"


def _webp(data: bytes) -> str | None:
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return "does not begin as a riff webp"
    if _little(data, 4, 4) != len(data) - 8:
        return "has a riff length that is not the file's length"
    position = 12
    if data[position : position + 4] not in WEBP_FIRST:
        return "does not begin with a webp image chunk"
    while position < len(data):
        if position + 8 > len(data):
            return "ends inside a webp chunk"
        size = _little(data, position + 4, 4)
        position += 8 + size + (size & 1)
        if position > len(data):
            return "has a webp chunk longer than the file"
    return None


CHECKERS: Final = {
    ".png": _png,
    ".jpg": _jpeg,
    ".jpeg": _jpeg,
    ".gif": _gif,
    ".webp": _webp,
}
"""The container check for each suffix a line may view."""


def container_refusal(suffix: str, data: bytes) -> str | None:
    """Why ``data`` is not one complete image of the type ``suffix`` names, as a phrase
    after the file's name, or ``None`` when it is."""
    checker = CHECKERS.get(suffix)
    if checker is None:
        return "is not a type that can be viewed"
    return checker(data)
