"""tools/make_avatar.py (P1): a valid, deterministic PNG drawn with the standard library only."""

from __future__ import annotations

import struct
import zlib

from tools import make_avatar


def decode(data: bytes) -> tuple[int, int, bytes]:
    """Width, height and the raw RGB rows (with filter bytes) of an 8-bit RGB PNG."""
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    position, chunks = 8, {}
    while position < len(data):
        (length,) = struct.unpack(">I", data[position:position + 4])
        kind = data[position + 4:position + 8]
        body = data[position + 8:position + 8 + length]
        (crc,) = struct.unpack(">I", data[position + 8 + length:position + 12 + length])
        assert crc == zlib.crc32(kind + body)
        chunks[kind] = body
        position += 12 + length
    width, height, depth, color_type = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, color_type) == (8, 2) and b"IEND" in chunks
    return width, height, zlib.decompress(chunks[b"IDAT"])


def test_avatar_is_a_gift_on_red() -> None:
    size = 80
    width, height, rows = decode(make_avatar.png(size))
    assert (width, height) == (size, size) and len(rows) == size * (1 + 3 * size)

    def pixel(x: int, y: int) -> tuple[int, ...]:
        start = y * (1 + 3 * size) + 1 + 3 * x
        return tuple(rows[start:start + 3])

    assert pixel(2, 2)[0] > 180 and pixel(2, 2)[1] < 80, "red background"
    assert pixel(size * 35 // 100, size * 65 // 100) == (255, 255, 255), "white box"
    assert pixel(size // 2, size * 65 // 100)[:2] == (255, 193), "golden ribbon"
    assert make_avatar.png(size) == make_avatar.png(size)
