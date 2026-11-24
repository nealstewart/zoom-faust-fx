"""Pixel-art icon for se50pitch (half-rack pitch shifter)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import INK, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    # Half-rack LCD unit with Up & Down pitch arrows
    d.rectangle([x0 + 1, y0 + 2, x1 - 1, y1 - 2], outline=INK, width=1)
    cx = (x0 + x1) // 2
    cy = (y0 + y1) // 2
    # Up arrow on left
    lx = cx - scale(9, sc)
    d.polygon([(lx, y0 + 6), (lx - 6, cy + 1), (lx + 6, cy + 1)], fill=INK)
    d.rectangle([lx - 2, cy + 1, lx + 2, y1 - 6], fill=INK)
    # Down arrow on right
    rx = cx + scale(9, sc)
    d.polygon([(rx, y1 - 6), (rx - 6, cy - 1), (rx + 6, cy - 1)], fill=INK)
    d.rectangle([rx - 2, y0 + 6, rx + 2, cy - 1], fill=INK)
