"""Pixel-art icon for xachr (32-bit era 3-voice chorus)."""

import math
from typing import Tuple

from PIL import ImageDraw
from pixel_icons import INK, draw_32bit_ticks, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    # 3 phase-shifted overlapping chorus sine waves + 32-bit ticks
    cy = (y0 + y1 - scale(9, sc)) // 2
    amp = max(5, (y1 - y0 - scale(12, sc)) * 0.36)
    w = max(1, x1 - x0)
    for phase, lw in [(0.0, 2), (2.094, 1), (4.188, 1)]:
        pts = []
        for x in range(x0, x1 + 1):
            t = (x - x0) / w
            y = cy - int(round(math.sin(t * math.pi * 2.5 + phase) * amp))
            pts.append((x, y))
        d.line(pts, fill=INK, width=lw)
    draw_32bit_ticks(d, x0, y1 - scale(4, sc), x1, sc)
