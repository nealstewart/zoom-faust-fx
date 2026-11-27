"""Pixel-art icon for wtdelay (Live Wavetable-Capture Delay)."""

import math
from typing import Tuple

from PIL import ImageDraw
from pixel_icons import INK, draw_32bit_ticks, scale


def draw_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float) -> None:
    x0, y0, x1, y1 = box
    w = max(12, x1 - x0)
    h = max(16, y1 - y0 - scale(8, sc))

    # Stacked 3D-perspective wavetable frames receding into history (ZOH stepped)
    rows = 4
    row_step = max(4, h // (rows + 1))
    amp_max = max(3, row_step // 2)

    for r in range(rows):
        # Back-to-front: older frames higher & narrower, newest frame lower & full width
        frac = r / max(1, rows - 1)
        cy = y0 + scale(5, sc) + r * row_step
        inset = int(round((1.0 - frac) * w * 0.16))
        rx0 = x0 + 1 + inset
        rx1 = x1 - 1 - inset
        rw = max(8, rx1 - rx0)
        steps = 8
        prev_pt = None
        for s in range(steps + 1):
            px = rx0 + int(round((s / steps) * rw))
            # Evolving harmonic wavetable shape across frames
            phase = (s / steps) * 2.0 * math.pi
            val = math.sin(phase) * (0.65 + 0.35 * frac) + 0.45 * math.sin((2 + r) * phase)
            py = cy - int(round(val * amp_max * (0.55 + 0.45 * frac)))
            if prev_pt is not None:
                # Zero-order-hold staircase horizontal then vertical segment
                d.line([prev_pt[0], prev_pt[1], px, prev_pt[1]], fill=INK, width=1)
                d.line([px, prev_pt[1], px, py], fill=INK, width=1)
            prev_pt = (px, py)

    draw_32bit_ticks(d, x0, y1 - scale(4, sc), x1, sc)
