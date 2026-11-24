"""Pixel-art icon for segacd (Ricoh RF5C164 DIP chip + Sega CD disc)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import BG, INK, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    cx = (x0 + x1) // 2

    # Upper half: RF5C164 IC package with pins
    ic_x0 = x0 + scale(9, sc)
    ic_x1 = x1 - scale(9, sc)
    ic_y0 = y0 + scale(3, sc)
    ic_y1 = y0 + scale(21, sc)
    d.rectangle([ic_x0, ic_y0, ic_x1, ic_y1], fill=INK)
    pin_len = max(2, scale(4, sc))
    for i in range(5):
        py = ic_y0 + scale(3 + i * 3.2, sc)
        d.line([ic_x0 - pin_len, py, ic_x0, py], fill=INK, width=1)
        d.line([ic_x1, py, ic_x1 + pin_len, py], fill=INK, width=1)
    d.text((cx - scale(6, sc), ic_y0 + scale(4, sc)), "CD", fill=BG)

    # Lower half: jittered stepped 8-bit PCM waveform
    wy_mid = y1 - scale(9, sc)
    amp = max(4, scale(6.5, sc))
    d.line([x0 + 2, wy_mid, x1 - 2, wy_mid], fill=INK, width=1)
    # Uneven step widths visualize RF5C164 random S&H clock jitter
    segs = [
        (0.00, 0.10,  0.45),
        (0.10, 0.28,  0.90),
        (0.28, 0.37,  0.30),
        (0.37, 0.52, -0.35),
        (0.52, 0.72, -0.90),
        (0.72, 0.84, -0.45),
        (0.84, 1.00,  0.25),
    ]
    span = (x1 - 3) - (x0 + 3)
    prev_y = None
    for f0, f1, val in segs:
        sx0 = x0 + 3 + int(span * f0)
        sx1 = x0 + 3 + int(span * f1)
        sy = wy_mid - int(round(val * amp))
        if prev_y is not None:
            d.line([sx0, prev_y, sx0, sy], fill=INK, width=1)
        d.line([sx0, sy, sx1, sy], fill=INK, width=2)
        prev_y = sy
