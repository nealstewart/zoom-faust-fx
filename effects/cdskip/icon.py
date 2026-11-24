"""Pixel-art icon for cdskip (glitchy skipping CD)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import BG, INK, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    r_out = min(x1 - x0, y1 - y0) // 2 - 1
    r_in = max(3, r_out // 3)
    d.ellipse([cx - r_out, cy - r_out, cx + r_out, cy + r_out], outline=INK, width=2)
    d.ellipse([cx - r_in, cy - r_in, cx + r_in, cy + r_in], outline=INK, width=1)
    # Glitchy horizontal skip slices across the CD
    for dy, dx0, dx1 in [(-5, -r_out - 2, 4), (2, -2, r_out + 2), (7, -r_out, 2)]:
        yy = cy + scale(dy, sc)
        d.line([cx + dx0, yy, cx + dx1, yy], fill=BG, width=2)
        d.line([cx + dx0 + 3, yy, cx + dx1, yy], fill=INK, width=1)
