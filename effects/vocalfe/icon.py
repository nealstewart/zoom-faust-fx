"""Pixel-art icon for vocalfe (studio condenser mic)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import INK, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    cx = (x0 + x1) // 2 - scale(5, sc)
    cy = (y0 + y1) // 2 - scale(3, sc)
    # Studio condenser mic capsule
    mw = scale(7, sc)
    mh = scale(11, sc)
    d.rounded_rectangle([cx - mw, cy - mh, cx + mw, cy + mh], radius=mw, outline=INK, width=2)
    # Grille lines
    for dy in range(-mh + 4, mh - 2, 3):
        d.line([cx - mw + 2, cy + dy, cx + mw - 2, cy + dy], fill=INK, width=1)
    # Mic stand cradle
    d.arc([cx - mw - 3, cy - 2, cx + mw + 3, cy + mh + 4], 0, 180, fill=INK, width=1)
    d.line([cx, cy + mh + 4, cx, y1 - 2], fill=INK, width=2)
    d.line([cx - scale(6, sc), y1 - 2, cx + scale(6, sc), y1 - 2], fill=INK, width=2)
    # Soundwave arcs on the right
    for rad in (scale(6, sc), scale(11, sc), scale(16, sc)):
        d.arc([cx + mw + 2 - rad, cy - rad, cx + mw + 2 + rad, cy + rad], -50, 50, fill=INK, width=1)
