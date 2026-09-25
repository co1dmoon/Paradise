"""Draws the bot's 500×500 PNG avatar (§1, P1): a white gift box with a golden bow on red.

    cd santa-bot && .venv/bin/python -m tools.make_avatar [avatar.png]

Standard library only: shapes are signed-distance functions in unit coordinates,
so edges are smooth at any size, and the PNG is written with zlib. The picture
is deterministic and contains no MAX logo (§11).
"""

from __future__ import annotations

import math
import struct
import sys
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

SIZE = 500
RGB = tuple[float, float, float]
Distance = Callable[[float, float], float]


def _hex(color: str) -> RGB:
    return (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16))


def _rect(cx: float, cy: float, half_w: float, half_h: float) -> Distance:
    def distance(x: float, y: float) -> float:
        qx, qy = abs(x - cx) - half_w, abs(y - cy) - half_h
        return math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0)

    return distance


def _ellipse(cx: float, cy: float, rx: float, ry: float) -> Distance:
    def distance(x: float, y: float) -> float:
        return (math.hypot((x - cx) / rx, (y - cy) / ry) - 1.0) * min(rx, ry)

    return distance


@dataclass(frozen=True, slots=True)
class Shape:
    distance: Distance
    color: RGB
    opacity: float = 1.0


_GOLD, _DARK_GOLD, _WHITE, _LID = _hex("#FFC107"), _hex("#FF8F00"), _hex("#FFFFFF"), _hex("#F1F1F1")
_SNOW = ((0.13, 0.16), (0.84, 0.12), (0.22, 0.83), (0.9, 0.62), (0.08, 0.52), (0.72, 0.9), (0.62, 0.08))
SHAPES: tuple[Shape, ...] = (
    Shape(_ellipse(0.5, 0.815, 0.27, 0.035), (0, 0, 0), 0.25),  # shadow
    *(Shape(_ellipse(x, y, 0.018, 0.018), _WHITE, 0.55) for x, y in _SNOW),
    Shape(_rect(0.5, 0.625, 0.2, 0.175), _WHITE),  # box
    Shape(_rect(0.5, 0.435, 0.235, 0.05), _LID),  # lid
    Shape(_rect(0.5, 0.5925, 0.035, 0.2075), _GOLD),  # ribbon, from the lid top to the bottom
    Shape(_ellipse(0.425, 0.345, 0.08, 0.047), _GOLD),  # bow, left loop
    Shape(_ellipse(0.575, 0.345, 0.08, 0.047), _GOLD),  # bow, right loop
    Shape(_ellipse(0.5, 0.36, 0.033, 0.033), _DARK_GOLD),  # knot
)


def _background(y: float) -> RGB:
    top, bottom = _hex("#D32F2F"), _hex("#8E1B1B")
    return (top[0] + (bottom[0] - top[0]) * y, top[1] + (bottom[1] - top[1]) * y, top[2] + (bottom[2] - top[2]) * y)


def pixels(size: int = SIZE) -> bytes:
    """RGB rows, each prefixed with PNG filter byte 0."""
    rows = bytearray()
    for row in range(size):
        y = (row + 0.5) / size
        base = _background(y)
        rows.append(0)
        for column in range(size):
            x = (column + 0.5) / size
            r, g, b = base
            for shape in SHAPES:
                coverage = min(1.0, max(0.0, 0.5 - shape.distance(x, y) * size)) * shape.opacity
                if coverage:
                    r += (shape.color[0] - r) * coverage
                    g += (shape.color[1] - g) * coverage
                    b += (shape.color[2] - b) * coverage
            rows += bytes((round(r), round(g), round(b)))
    return bytes(rows)


def png(size: int = SIZE) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)  # 8-bit RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(pixels(size), 9)) + chunk(
        b"IEND", b"")


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    path = Path(args[0] if args else "avatar.png")
    path.write_bytes(png())
    print(f"Готово: {path} (500×500, PNG). Загрузите его как логотип бота на платформе MAX.")


if __name__ == "__main__":
    main()
