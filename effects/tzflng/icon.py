"""Pixel-art icon for evntflng (through-zero flanger)."""

import math
from typing import Tuple

from PIL import ImageDraw
from pixel_icons import INK


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    cy = (y0 + y1) // 2
    # Through-zero center dashed line
    for x in range(x0, x1, 4):
        d.line([x, cy, x + 1, cy], fill=INK)
    # Two opposing swept comb sine waves crossing through zero
    pts1, pts2 = [], []
    amp = (y1 - y0) * 0.38
    for x in range(x0, x1 + 1):
        t = (x - x0) / max(1, (x1 - x0))
        y_a = cy - int(round(math.sin(t * math.pi * 3.0) * amp))
        y_b = cy + int(round(math.sin(t * math.pi * 3.0 + 0.9) * amp * 0.72))
        pts1.append((x, y_a))
        pts2.append((x, y_b))
    d.line(pts1, fill=INK, width=2)
    d.line(pts2, fill=INK, width=1)
