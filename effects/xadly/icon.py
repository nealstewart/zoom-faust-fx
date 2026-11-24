"""Pixel-art icon for xadly (32-bit era BBD echo delay)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import INK, draw_32bit_ticks, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    # Cascading decaying BBD echo pulses
    base_y = y1 - scale(11, sc)
    d.line([x0, base_y, x1, base_y], fill=INK, width=1)
    bars = [1.0, 0.72, 0.50, 0.34, 0.20, 0.11]
    bw = max(3, (x1 - x0 - 4) // (len(bars) * 2))
    max_h = max(8, base_y - y0 - 4)
    for i, amp in enumerate(bars):
        bx = x0 + 2 + i * bw * 2
        bh = max(2, int(round(amp * max_h)))
        d.rectangle([bx, base_y - bh, bx + bw, base_y], fill=INK)
    draw_32bit_ticks(d, x0, y1 - scale(4, sc), x1, sc)
