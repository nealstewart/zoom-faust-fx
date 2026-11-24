"""Pixel-art icon for stutrtch (accelerating stutter ratchet)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import INK, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    # Chopped accelerating ratchet slices
    cy = (y0 + y1) // 2
    h = (y1 - y0) // 2 - 3
    x = x0 + 1
    w = max(6, scale(9, sc))
    while x < x1 - 1:
        x2 = min(x1 - 1, x + w)
        d.rectangle([x, cy - h, x2, cy + h], fill=INK)
        x = x2 + 2
        w = max(2, int(w * 0.72))
