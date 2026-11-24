"""Pixel-art icon for ym2612 (Sega Genesis / Mega Drive YM2612 DIP-24 + 9-bit ladder)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import BG, INK, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    cx = (x0 + x1) // 2
    # DIP-24 IC package outline in upper portion
    ic_x0 = x0 + scale(10, sc)
    ic_x1 = x1 - scale(10, sc)
    ic_y0 = y0 + scale(3, sc)
    ic_y1 = y0 + scale(22, sc)
    d.rectangle([ic_x0, ic_y0, ic_x1, ic_y1], fill=BG, outline=INK, width=1)
    # Pin-1 notch on top center
    notch_r = max(2, scale(2.5, sc))
    d.arc([cx - notch_r, ic_y0 - notch_r, cx + notch_r, ic_y0 + notch_r], 0, 180, fill=INK, width=1)
    # Left & right DIP legs (6 pins per side)
    pin_len = max(2, scale(4, sc))
    for i in range(6):
        py = ic_y0 + scale(3 + i * 2.8, sc)
        d.line([ic_x0 - pin_len, py, ic_x0, py], fill=INK, width=1)
        d.line([ic_x1, py, ic_x1 + pin_len, py], fill=INK, width=1)
    # "16" pixel badge inside chip
    d.text((cx - scale(6, sc), ic_y0 + scale(5, sc)), "16", fill=INK)

    # Lower portion: 9-bit DAC staircase with iconic zero-crossing ladder jump
    wy_mid = y1 - scale(9, sc)
    amp = max(4, scale(6.5, sc))
    d.line([x0 + 2, wy_mid, x1 - 2, wy_mid], fill=INK, width=1)
    steps = [0.0, 0.55, 0.95, 0.55, 0.25, -0.25, -0.55, -0.95, -0.55, 0.0]
    span = (x1 - 3) - (x0 + 3)
    n = len(steps)
    for i in range(n):
        sx0 = x0 + 3 + int(span * i / n)
        sx1 = x0 + 3 + int(span * (i + 1) / n)
        sy = wy_mid - int(round(steps[i] * amp))
        d.line([sx0, sy, sx1, sy], fill=INK, width=2)
        if i + 1 < n:
            sy_next = wy_mid - int(round(steps[i + 1] * amp))
            d.line([sx1, sy, sx1, sy_next], fill=INK, width=1)
