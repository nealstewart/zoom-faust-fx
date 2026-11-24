"""Pixel-art icon for anlgrat (RAT-style distortion)."""

import math
from typing import Tuple

from PIL import ImageDraw
from pixel_icons import INK, draw_knob, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    cy = (y0 + y1) // 2 + scale(4, sc)
    # 3 classic RAT knobs along top
    r = max(3, scale(4.5, sc))
    ky = y0 + r + 2
    for f, ang in [(0.2, -120), (0.5, -60), (0.8, -30)]:
        draw_knob(d, x0 + int((x1 - x0) * f), ky, r, ang)
    # Hard-clipped square-ish RAT distortion wave below
    amp = max(4, scale(8, sc))
    pts = []
    for x in range(x0 + 1, x1):
        t = (x - x0) / max(1, (x1 - x0))
        s = math.sin(t * math.pi * 4.0) * 2.6
        c = max(-1.0, min(1.0, s))
        pts.append((x, cy - int(round(c * amp))))
    d.line(pts, fill=INK, width=2)
