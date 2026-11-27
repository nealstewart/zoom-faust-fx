#!/usr/bin/env python3
"""
tools/pixel_icons.py — Programmatic 1-bit primitive pixel-art icon loader &
helper library for Zoom MS Plus (.ZIC / .ZD2) and the WebAssembly Auditioner.

Each effect folder may optionally provide its own icon beside its `.dsp` file
in any of these forms (checked in priority order):

1. Size-specific static image: `icon_72x97.png` and/or `icon_102x128.png`
   (also `.bmp`, `.gif`, `.webp`, `.jpg`, `.jpeg`).
2. Single static image: `icon.png` (auto-resized to `72x97` and `102x128` and
   converted to 1-bit monochrome).
3. Programmatic `icon.py` defining either:

    def draw_art(d, box, sc):
        # Draw 1-bit artwork inside `box = (x0, y0, x1, y1)` on Pillow ImageDraw `d`
        # using `pixel_icons` helpers (`INK`, `BG`, `scale`, `draw_knob`, ...).

   or, for full control over the entire icon canvas:

    def render_icon(manifest, size):
        # Return a Pillow Image of `size` (converted to 1-bit mode '1').

If an effect folder provides none of the above, a clean default stompbox icon
is rendered automatically.
"""

from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageDraw

sys.modules.setdefault("pixel_icons", sys.modules[__name__])

# 1-bit monochrome palette matching Zoom MS-70CDR+ LCD (0 = black ink, 255 = white LCD)
INK = 0
BG = 255


def scale(val: float, sc: float) -> int:
    """Scale a coordinate from the 72x97 base canvas to the active icon size."""
    return int(round(val * sc))


# Backwards-compatible alias
_s = scale


def draw_chassis(d: ImageDraw.ImageDraw, w: int, h: int) -> Tuple[int, int, int, int]:
    """Draw a stompbox enclosure with corner screws, top LCD/art window, and footswitch."""
    sc = w / 72.0
    pad = scale(2, sc)
    x0, y0, x1, y1 = pad, pad, w - 1 - pad, h - 1 - pad

    # Outer beveled pedal border
    d.rectangle([x0 + 1, y0, x1 - 1, y1], fill=BG, outline=INK, width=1)
    d.rectangle([x0, y0 + 1, x1, y1 - 1], outline=INK, width=1)
    # Inner crisp frame
    d.rectangle([x0 + 2, y0 + 2, x1 - 2, y1 - 2], outline=INK, width=1)

    # 4 corner screw pixels
    for sx in (x0 + 5, x1 - 5):
        for sy in (y0 + 5, y1 - 5):
            d.rectangle([sx - 1, sy - 1, sx + 1, sy + 1], fill=INK)

    # Main artwork box (upper ~58% of pedal)
    ax0 = x0 + scale(6, sc)
    ay0 = y0 + scale(7, sc)
    ax1 = x1 - scale(6, sc)
    ay1 = y0 + scale(56, sc)
    d.rectangle([ax0, ay0, ax1, ay1], fill=BG, outline=INK, width=1)

    # Small footswitch circle near bottom
    fw_r = scale(5, sc)
    cx = w // 2
    cy = y1 - scale(10, sc)
    d.ellipse([cx - fw_r, cy - fw_r, cx + fw_r, cy + fw_r], fill=BG, outline=INK, width=1)
    d.ellipse([cx - fw_r + 2, cy - fw_r + 2, cx + fw_r - 2, cy + fw_r - 2], fill=INK)

    # Tiny LED indicator to the left of footswitch
    led_x = cx - scale(16, sc)
    d.rectangle([led_x - 1, cy - 1, led_x + 1, cy + 1], fill=INK)

    return ax0 + 2, ay0 + 2, ax1 - 2, ay1 - 2


def draw_knob(d: ImageDraw.ImageDraw, cx: int, cy: int, r: int, angle_deg: float = -45.0) -> None:
    """Draw a rotary potentiometer knob with pointer line."""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BG, outline=INK, width=1)
    rad = math.radians(angle_deg)
    px = cx + int(round((r - 1) * math.cos(rad)))
    py = cy + int(round((r - 1) * math.sin(rad)))
    d.line([cx, cy, px, py], fill=INK, width=1)


def draw_32bit_ticks(d: ImageDraw.ImageDraw, x0: int, y: int, x1: int, sc: float) -> None:
    """Draw a clean 4-bar digital clock / sample-tick accent strip (generic 32-bit motif)."""
    span = x1 - x0
    bar_w = max(3, scale(4, sc))
    bar_h = max(2, scale(2.5, sc))
    for i, f in enumerate((0.14, 0.38, 0.62, 0.86)):
        cx = x0 + int(span * f)
        h = bar_h if (i % 2 == 0) else max(1, bar_h - 1)
        d.rectangle([cx - bar_w // 2, y - h, cx + bar_w // 2, y + h], fill=INK)


def _draw_default_art(d: ImageDraw.ImageDraw, box: Tuple[int, int, int, int], sc: float, m: dict) -> None:
    """Fallback artwork when an effect folder does not define `icon.py`."""
    joined = " ".join([
        str(m.get("kernel", "")).replace(".c", "").lower(),
        str(m.get("name", "")).lower(),
        str(m.get("filename", "")).lower(),
    ])
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2

    # Built-in routing symbols for upstream stomphacks utility effects
    if any(k in joined for k in ("wfsplit", "wfmerge", "wfswap")):
        top_y = y0 + scale(8, sc)
        bot_y = y1 - scale(8, sc)
        if "wfsplit" in joined:
            d.line([x0 + 2, cy, cx - 2, cy], fill=INK, width=2)
            d.line([cx - 2, cy, x1 - 4, top_y], fill=INK, width=2)
            d.line([cx - 2, cy, x1 - 4, bot_y], fill=INK, width=2)
            d.polygon([(x1 - 1, top_y), (x1 - 6, top_y - 3), (x1 - 6, top_y + 3)], fill=INK)
            d.polygon([(x1 - 1, bot_y), (x1 - 6, bot_y - 3), (x1 - 6, bot_y + 3)], fill=INK)
        elif "wfmerge" in joined:
            d.line([x0 + 2, top_y, cx + 2, cy], fill=INK, width=2)
            d.line([x0 + 2, bot_y, cx + 2, cy], fill=INK, width=2)
            d.line([cx + 2, cy, x1 - 4, cy], fill=INK, width=2)
            d.polygon([(x1 - 1, cy), (x1 - 6, cy - 4), (x1 - 6, cy + 4)], fill=INK)
        else:  # wfswap
            d.line([x0 + 2, top_y, x1 - 4, bot_y], fill=INK, width=2)
            d.line([x0 + 2, bot_y, x1 - 4, top_y], fill=INK, width=2)
            d.polygon([(x1 - 1, top_y), (x1 - 6, top_y - 3), (x1 - 6, top_y + 3)], fill=INK)
            d.polygon([(x1 - 1, bot_y), (x1 - 6, bot_y - 3), (x1 - 6, bot_y + 3)], fill=INK)
        return

    # Generic stompbox knob + boost '+'
    r = min(x1 - x0, y1 - y0) // 3
    draw_knob(d, cx, cy, r, -45)
    px, py = x1 - scale(6, sc), y0 + scale(6, sc)
    d.line([px - 3, py, px + 3, py], fill=INK, width=2)
    d.line([px, py - 3, px, py + 3], fill=INK, width=2)


IMAGE_EXTS = (".png", ".bmp", ".gif", ".webp", ".jpg", ".jpeg")


def _resolve_effect_root(effect_dir: Optional[Path]) -> Optional[Path]:
    if effect_dir is None:
        return None
    base = Path(effect_dir)
    if base.name == "gen" and not (base / "icon.py").is_file():
        return base.parent
    return base


def _load_static_icon_image(effect_dir: Optional[Path], size: Tuple[int, int]) -> Optional[Image.Image]:
    """Load `icon_{w}x{h}.<ext>` or `icon.<ext>` from the effect folder if present."""
    root = _resolve_effect_root(effect_dir)
    if root is None:
        return None

    w, h = size
    candidates = [root / f"icon_{w}x{h}{ext}" for ext in IMAGE_EXTS]
    candidates.extend(root / f"icon{ext}" for ext in IMAGE_EXTS)

    img_path = next((p for p in candidates if p.is_file()), None)
    if img_path is None:
        return None

    with Image.open(img_path) as raw:
        was_1bit = raw.mode == "1"
        if raw.mode in ("RGBA", "LA") or (raw.mode == "P" and "transparency" in raw.info):
            rgba = raw.convert("RGBA")
            bg = Image.new("RGBA", rgba.size, (BG, BG, BG, 255))
            img = Image.alpha_composite(bg, rgba).convert("L")
        else:
            img = raw.convert("L")

        if img.size != size:
            resample = Image.Resampling.NEAREST if was_1bit else Image.Resampling.LANCZOS
            img = img.resize(size, resample)

        return img.convert("1", dither=Image.Dither.NONE)


def _load_icon_module(effect_dir: Optional[Path]):
    """Load `effect_dir / 'icon.py'` (or parent if `effect_dir` is `gen/`), else None."""
    if effect_dir is None:
        return None
    base = Path(effect_dir)
    icon_py = base / "icon.py"
    if not icon_py.is_file() and base.name == "gen":
        icon_py = base.parent / "icon.py"
    if not icon_py.is_file():
        return None

    # Ensure `import pixel_icons` works inside effect icon.py files
    tools_dir = str(Path(__file__).resolve().parent)
    if tools_dir not in sys.path:
        sys.path.insert(0, tools_dir)

    mod_name = f"_zoom_fx_icon_{icon_py.parent.name}"
    spec = importlib.util.spec_from_file_location(mod_name, icon_py)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def render_pixel_icon(
    m: dict,
    size: Tuple[int, int],
    effect_dir: Optional[Path] = None,
) -> Image.Image:
    """Render a 1-bit ('1') monochrome stompbox pixel-art icon for `m` at `size`.

    Lookup priority inside `effect_dir` (falling back to parent if `gen/`):
      1. `icon_{w}x{h}.png` (or `.bmp`/`.gif`/`.webp`/`.jpg`)
      2. `icon.png` (auto-resized and converted to 1-bit monochrome)
      3. `icon.py` (`render_icon(m, size)` or `draw_art(d, box, sc)`)
      4. Default stompbox icon
    """
    static_img = _load_static_icon_image(effect_dir, size)
    if static_img is not None:
        return static_img

    mod = _load_icon_module(effect_dir)
    if mod is not None and callable(getattr(mod, "render_icon", None)):
        return mod.render_icon(m, size).convert("1")

    w, h = size
    sc = w / 72.0
    img = Image.new("L", (w, h), BG)
    d = ImageDraw.Draw(img)

    art_box = draw_chassis(d, w, h)
    if mod is not None and callable(getattr(mod, "draw_art", None)):
        mod.draw_art(d, art_box, sc)
    else:
        _draw_default_art(d, art_box, sc, m)

    # Crisp centered display label between artwork window and footswitch
    label = str(m.get("display", m.get("name", "")))[:9]
    bb = d.textbbox((0, 0), label)
    tw = bb[2] - bb[0]
    label_y = art_box[3] + scale(6, sc)
    d.text(((w - tw) // 2, label_y), label, fill=INK)

    return img.convert("1")


if __name__ == "__main__":
    # Quick preview / regenerate all web icons directly if run standalone
    import json

    root = Path(__file__).resolve().parent.parent
    out_dir = root / "web" / "dist" / "icons"
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    for d_path in sorted((root / "effects").iterdir()):
        mp = d_path / "gen" / "manifest.json"
        if not mp.is_file():
            mp = d_path / "manifest.json"
        if not mp.is_file():
            continue
        m = json.loads(mp.read_text(encoding="utf-8-sig"))
        render_pixel_icon(m, (72, 97), effect_dir=d_path).save(out_dir / f"{d_path.name}.png")
        count += 1
    print(f"Rendered {count} pixel-art icons -> {out_dir}")
