"""Pixel-art icon for segacddly (Sega CD disc + stepped 8-bit PCM echo bars)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import BG, INK, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    cx = (x0 + x1) // 2

    # Upper half: Sega CD disc silhouette
    disc_y = y0 + scale(14, sc)
    r_out = max(7, scale(11, sc))
    r_in = max(2, r_out // 3)
    d.ellipse([cx - r_out, disc_y - r_out, cx + r_out, disc_y + r_out], fill=BG, outline=INK, width=2)
    d.ellipse([cx - r_in, disc_y - r_in, cx + r_in, disc_y + r_in], fill=BG, outline=INK, width=1)
    # Reflective sheen line on disc
    d.line([cx - r_out + 3, disc_y - 3, cx - r_in - 1, disc_y - 1], fill=INK, width=1)
    d.line([cx + r_in + 1, disc_y + 1, cx + r_out - 3, disc_y + 3], fill=INK, width=1)

    # Lower half: stepped 8-bit PCM ping-pong echo bars
    base_y = y1 - scale(3, sc)
    d.line([x0 + 1, base_y, x1 - 1, base_y], fill=INK, width=1)
    bars = [1.0, 0.72, 0.50, 0.34, 0.20]
    span = (x1 - 2) - (x0 + 2)
    bw = max(3, span // (len(bars) * 2))
    max_h = max(6, scale(11, sc))
    for i, amp in enumerate(bars):
        bx = x0 + 3 + i * (bw + max(3, scale(3, sc)))
        bh = max(2, int(round(amp * max_h)))
        d.rectangle([bx, base_y - bh, bx + bw, base_y], fill=INK)
