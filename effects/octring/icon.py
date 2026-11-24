"""Pixel-art icon for octring (octave ring modulator)."""

from typing import Tuple

from PIL import ImageDraw
from pixel_icons import INK


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    r = min(x1 - x0, y1 - y0) // 3
    off = r // 2
    # Two interlocking ring-modulator rings
    d.ellipse([cx - r - off, cy - r, cx + r - off, cy + r], outline=INK, width=2)
    d.ellipse([cx - r + off, cy - r, cx + r + off, cy + r], outline=INK, width=2)
    d.rectangle([cx - 2, cy - 2, cx + 2, cy + 2], fill=INK)
