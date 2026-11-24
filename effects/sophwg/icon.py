"""Pixel-art icon for sophwg (waveguide resonator)."""

import math
from typing import Tuple

from PIL import ImageDraw
from pixel_icons import INK


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    cy = (y0 + y1) // 2
    # Vibrating waveguide string harmonic envelope
    amp = (y1 - y0) * 0.36
    for mult in (1.0, -1.0, 0.45, -0.45):
        pts = []
        for x in range(x0, x1 + 1):
            t = (x - x0) / max(1, (x1 - x0))
            y = cy - int(round(math.sin(t * math.pi) * amp * mult))
            pts.append((x, y))
        d.line(pts, fill=INK, width=2 if abs(mult) > 0.8 else 1)
