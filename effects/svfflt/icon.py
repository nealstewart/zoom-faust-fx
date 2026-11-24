"""Pixel-art icon for svfflt (resonant low-pass SVF)."""

import math
from typing import Tuple

from PIL import ImageDraw
from pixel_icons import INK


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    # Grid baseline
    base_y = y1 - 3
    d.line([x0, base_y, x1, base_y], fill=INK, width=1)
    # Resonant low-pass curve with sharp Q peak
    pts = []
    w = max(1, x1 - x0)
    h = max(1, y1 - y0 - 6)
    for x in range(x0, x1 + 1):
        u = (x - x0) / w
        if u < 0.45:
            val = 0.45
        elif u < 0.62:
            p = (u - 0.45) / 0.17
            val = 0.45 + 0.52 * math.sin(p * math.pi)
        else:
            p = (u - 0.62) / 0.38
            val = max(0.0, 0.45 * (1.0 - p) ** 1.7)
        pts.append((x, base_y - int(round(val * h))))
    d.line(pts, fill=INK, width=2)
    # Fill under curve with dithered vertical bars
    for i in range(0, len(pts), 3):
        px, py = pts[i]
        d.line([px, py + 2, px, base_y], fill=INK, width=1)
