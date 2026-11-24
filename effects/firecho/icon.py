"""Pixel-art icon for snesfir (SNES ABXY button diamond + 8-tap FIR impulse)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import BG, INK, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    cx = (x0 + x1) // 2

    # Upper half: iconic SNES tilted ABXY 4-button diamond
    dy_top = y0 + scale(11, sc)
    r = max(2, scale(3.2, sc))
    off_x = scale(7, sc)
    off_y = scale(5, sc)
    # X (top) & B (bottom) filled; Y (left) & A (right) hollow
    d.ellipse([cx - r, dy_top - off_y - r, cx + r, dy_top - off_y + r], fill=INK, outline=INK)
    d.ellipse([cx - r, dy_top + off_y - r, cx + r, dy_top + off_y + r], fill=INK, outline=INK)
    d.ellipse([cx - off_x - r, dy_top - r, cx - off_x + r, dy_top + r], fill=BG, outline=INK, width=1)
    d.ellipse([cx + off_x - r, dy_top - r, cx + off_x + r, dy_top + r], fill=BG, outline=INK, width=1)

    # Lower half: 8-tap bipolar FIR impulse response bars (CHRONO-like curve)
    mid_y = y1 - scale(8, sc)
    d.line([x0 + 2, mid_y, x1 - 2, mid_y], fill=INK, width=1)
    taps = [0.28, 0.72, 0.96, 0.96, 0.44, -0.10, -0.35, -0.20]
    span = (x1 - 4) - (x0 + 4)
    bw = max(2, span // 11)
    amp = max(5, scale(7.5, sc))
    for i, t in enumerate(taps):
        bx = x0 + 3 + int(span * i / 8)
        by = mid_y - int(round(t * amp))
        y_lo, y_hi = min(mid_y, by), max(mid_y, by)
        d.rectangle([bx, y_lo, bx + bw, y_hi], fill=INK)
