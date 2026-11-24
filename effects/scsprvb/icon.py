"""Pixel-art icon for scsprvb (Sega Saturn ringed planet + reverb waves)."""

import math
from typing import Tuple

from PIL import ImageDraw
from pixel_icons import BG, INK, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2 - scale(2, sc)

    # Planet sphere
    r = max(6, scale(9, sc))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=INK, outline=INK, width=1)
    # Subtle highlight crescent inside planet
    d.arc([cx - r + 2, cy - r + 2, cx + r - 2, cy + r - 2], 200, 310, fill=BG, width=1)

    # Tilted Saturn ring (drawn as angled ellipse points)
    rx = scale(20, sc)
    ry = scale(5.5, sc)
    tilt = -0.34
    cos_t, sin_t = math.cos(tilt), math.sin(tilt)
    ring_pts = []
    for i in range(37):
        ang = (i / 36.0) * 2.0 * math.pi
        lx = math.cos(ang) * rx
        ly = math.sin(ang) * ry
        px = cx + int(round(lx * cos_t - ly * sin_t))
        py = cy + int(round(lx * sin_t + ly * cos_t))
        ring_pts.append((px, py))
    d.line(ring_pts, fill=INK, width=2)

    # Expanding reverb reflection arcs underneath
    for rad in (scale(13, sc), scale(18, sc)):
        d.arc([cx - rad, y1 - rad - 1, cx + rad, y1 + rad - 1], 210, 330, fill=INK, width=1)
