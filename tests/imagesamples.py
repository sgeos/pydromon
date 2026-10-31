"""Small synthetic images that are complete containers, built here and not checked in.

The pixel data is not a real picture's. ``dromon.imageformat`` checks structure and not
decoding, so each builder makes the smallest well-formed container, and ``size`` pads it
to an exact length with an ancillary or comment part, so a test can sit on the size
limit.
"""

import struct
import zlib


def _chunk(kind: bytes, body: bytes) -> bytes:
    return (
        struct.pack(">I", len(body))
        + kind
        + body
        + struct.pack(">I", zlib.crc32(kind + body))
    )


def png(size: int | None = None) -> bytes:
    """A 1 by 1 png, padded with a private ancillary chunk to ``size`` bytes."""
    header = _chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
    data = _chunk(b"IDAT", zlib.compress(b"\x00\x00"))
    end = _chunk(b"IEND", b"")
    base = b"\x89PNG\r\n\x1a\n" + header + data + end
    if size is None:
        return base
    pad = size - len(base) - 12
    assert pad >= 0
    return b"\x89PNG\r\n\x1a\n" + header + _chunk(b"prVt", b"\x00" * pad) + data + end


def jpeg(size: int | None = None) -> bytes:
    """A jpeg of one frame and one scan, padded with a comment to ``size`` bytes."""
    frame = b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, 1, 1, 1) + b"\x01\x11\x00"
    scan = (
        b"\xff\xda" + struct.pack(">HB", 8, 1) + b"\x01\x00\x00\x3f\x00" + b"\x12\x34"
    )
    base = b"\xff\xd8" + frame + scan + b"\xff\xd9"
    if size is None:
        return base
    pad = size - len(base) - 4
    assert 0 <= pad <= 65533
    comment = b"\xff\xfe" + struct.pack(">H", pad + 2) + b"\x00" * pad
    return b"\xff\xd8" + comment + frame + scan + b"\xff\xd9"


def gif(size: int | None = None) -> bytes:
    """A 1 by 1 gif, padded with a comment extension to ``size`` bytes."""
    screen = (
        b"GIF89a"
        + struct.pack("<HHBBB", 1, 1, 0x80, 0, 0)
        + b"\x00\x00\x00\xff\xff\xff"
    )
    image = b"\x2c" + struct.pack("<HHHHB", 0, 0, 1, 1, 0) + b"\x02\x02\x44\x01\x00"
    base = screen + image + b"\x3b"
    if size is None:
        return base
    pad = size - len(base) - 4
    assert 0 <= pad <= 255
    comment = b"\x21\xfe" + bytes([pad]) + b"\x00" * pad + b"\x00"
    return screen + comment + image + b"\x3b"


def webp(size: int | None = None) -> bytes:
    """A webp of one ``VP8L`` chunk, padded to ``size`` bytes, which must be even."""
    body = b"\x2f\x00\x00\x00\x00\x00"
    if size is not None:
        assert size % 2 == 0
        body = b"\x2f" + b"\x00" * (size - 12 - 8 - 1)
    chunk = b"VP8L" + struct.pack("<I", len(body)) + body + b"\x00" * (len(body) & 1)
    return b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk
