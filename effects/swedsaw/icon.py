"""Pixel-art icon for hm2saw (Swedish chainsaw distortion)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import INK, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    # Jagged Swedish chainsaw teeth
    mid_y = (y0 + y1) // 2
    tooth_w = max(5, scale(7, sc))
    amp = max(6, scale(10, sc))
    pts = []
    x = x0
    while x <= x1:
        pts.append((x, mid_y + amp))
        pts.append((min(x1, x + tooth_w // 2), mid_y - amp))
        x += tooth_w
    d.line(pts, fill=INK, width=2)
    # Heavy solid chainsaw bar underneath
    d.rectangle([x0, mid_y + amp - 1, x1, y1 - 2], fill=INK)
