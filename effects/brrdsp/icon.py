"""Pixel-art icon for snesdsp (Sony S-DSP chip + smooth 4-point Gaussian bell curve)."""

import math
from typing import Tuple

from PIL import ImageDraw
from pixel_icons import BG, INK, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    cx = (x0 + x1) // 2

    # Upper half: Sony S-DSP QFP square chip with pins on top/bottom/sides
    ic_w = scale(14, sc)
    ic_y0 = y0 + scale(4, sc)
    ic_y1 = y0 + scale(21, sc)
    d.rectangle([cx - ic_w, ic_y0, cx + ic_w, ic_y1], fill=INK)
    pin = max(2, scale(3, sc))
    for i in range(4):
        py = ic_y0 + scale(3 + i * 3.8, sc)
        d.line([cx - ic_w - pin, py, cx - ic_w, py], fill=INK, width=1)
        d.line([cx + ic_w, py, cx + ic_w + pin, py], fill=INK, width=1)
    d.text((cx - scale(9, sc), ic_y0 + scale(4, sc)), "SPC", fill=BG)

    # Lower half: smooth SNES 4-point Gaussian interpolation bell curve
    base_y = y1 - scale(3, sc)
    d.line([x0 + 2, base_y, x1 - 2, base_y], fill=INK, width=1)
    amp = max(6, scale(11, sc))
    w = max(1, (x1 - 3) - (x0 + 3))
    pts = []
    for x in range(x0 + 3, x1 - 2):
        u = ((x - (x0 + 3)) / w - 0.5) * 4.4
        val = math.exp(-0.5 * u * u)
        pts.append((x, base_y - int(round(val * amp))))
    d.line(pts, fill=INK, width=2)
