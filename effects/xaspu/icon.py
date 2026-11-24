"""Pixel-art icon for xaspu (32-bit era DIP microchip + staircase wave)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import BG, INK, draw_32bit_ticks, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    # Retro DIP microchip with legs + staircase quantized wave inside
    mx0, my0 = x0 + scale(6, sc), y0 + scale(5, sc)
    mx1, my1 = x1 - scale(6, sc), y1 - scale(10, sc)
    d.rectangle([mx0, my0, mx1, my1], fill=INK)
    # IC pins on left & right
    for py in range(my0 + 3, my1 - 1, 5):
        d.line([mx0 - 4, py, mx0, py], fill=INK, width=2)
        d.line([mx1, py, mx1 + 4, py], fill=INK, width=2)
    # White staircase wave etched inside chip
    cy = (my0 + my1) // 2
    step = max(3, (mx1 - mx0 - 6) // 6)
    pts = [
        (mx0 + 3, cy + 4), (mx0 + 3 + step, cy + 4),
        (mx0 + 3 + step, cy - 3), (mx0 + 3 + step * 2, cy - 3),
        (mx0 + 3 + step * 2, cy - 6), (mx0 + 3 + step * 3, cy - 6),
        (mx0 + 3 + step * 3, cy + 2), (mx0 + 3 + step * 4, cy + 2),
        (mx0 + 3 + step * 4, cy + 5), (mx1 - 3, cy + 5),
    ]
    d.line(pts, fill=BG, width=1)
    draw_32bit_ticks(d, x0, y1 - scale(4, sc), x1, sc)
