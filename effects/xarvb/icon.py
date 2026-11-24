"""Pixel-art icon for xarvb (32-bit era isometric reverb chamber)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import INK, draw_32bit_ticks, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    # Isometric reverberant chamber / tunnel perspective lines
    ry1 = y1 - scale(10, sc)
    inset_x = scale(11, sc)
    inset_y = scale(8, sc)
    d.rectangle([x0 + 1, y0 + 1, x1 - 1, ry1], outline=INK, width=1)
    d.rectangle([x0 + inset_x, y0 + inset_y, x1 - inset_x, ry1 - inset_y], outline=INK, width=2)
    d.line([x0 + 1, y0 + 1, x0 + inset_x, y0 + inset_y], fill=INK)
    d.line([x1 - 1, y0 + 1, x1 - inset_x, y0 + inset_y], fill=INK)
    d.line([x0 + 1, ry1, x0 + inset_x, ry1 - inset_y], fill=INK)
    d.line([x1 - 1, ry1, x1 - inset_x, ry1 - inset_y], fill=INK)
    draw_32bit_ticks(d, x0, y1 - scale(4, sc), x1, sc)
