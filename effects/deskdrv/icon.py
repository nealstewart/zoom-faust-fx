"""Pixel-art icon for mackdrv (console channel strip overdrive)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import BG, INK, draw_knob, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    # Console channel strip: 2 knobs on left, vertical fader on right
    r = max(3, scale(4.5, sc))
    lx = x0 + scale(11, sc)
    draw_knob(d, lx, y0 + scale(9, sc), r, -135)
    draw_knob(d, lx, y0 + scale(23, sc), r, 35)
    # Vertical fader slot on right
    fx = x1 - scale(11, sc)
    d.line([fx, y0 + 3, fx, y1 - 3], fill=INK, width=2)
    # Tick marks
    for ty in range(y0 + 5, y1 - 4, 5):
        d.line([fx - 5, ty, fx - 3, ty], fill=INK)
        d.line([fx + 3, ty, fx + 5, ty], fill=INK)
    # Fader cap pushed near top (driven!)
    fy = y0 + scale(10, sc)
    d.rectangle([fx - 5, fy - 3, fx + 5, fy + 3], fill=INK)
    d.line([fx - 4, fy, fx + 4, fy], fill=BG)
