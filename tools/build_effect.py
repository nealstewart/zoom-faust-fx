#!/usr/bin/env python3
"""Wrapper around vendor/stomphacks/tools/zd2_make_effect.py that plugs in
zoom-faust-fx's custom pixel-art icon renderer (tools/pixel_icons.py).

Usage:
    python tools/build_effect.py effects/<name>/manifest.json [zd2_make_effect args...]
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STOMPHACKS_DIR = Path(
    os.environ.get("STOMPHACKS_DIR", ROOT / "vendor" / "stomphacks")
).resolve()

sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(STOMPHACKS_DIR / "tools"))

import pixel_icons  # noqa: E402
import zd2_make_effect  # noqa: E402


def _make_pixel_icon(m: dict, builddir: Path, report: list) -> Path:
    from PIL import Image
    import zd2_from_scratch

    zic = builddir / f"{m['filename']}.ZIC"
    zic.write_bytes(zd2_from_scratch.blank_zic())
    for old in builddir.glob("icon_*.png"):
        old.unlink()
    zd2_make_effect.run(
        [
            zd2_make_effect.PY,
            zd2_make_effect.ROOT / "zoom-zt2" / "convert_zic.py",
            "-p",
            "icon",
            zic.name,
        ],
        cwd=builddir,
    )

    frames = sorted(builddir.glob("icon_*.png"))
    assert frames, "convert_zic produced no frames"
    effect_dir = builddir.parent
    for p in frames:
        w, h = Image.open(p).size
        img = pixel_icons.render_pixel_icon(m, (w, h), effect_dir=effect_dir)
        img.save(p)

    zd2_make_effect.run(
        [
            zd2_make_effect.PY,
            zd2_make_effect.ROOT / "zoom-zt2" / "convert_zic.py",
            "-r",
            "-p",
            "icon",
            zic.name,
        ],
        cwd=builddir,
    )
    zd2_from_scratch.verify_zic(zic.read_bytes())
    report.append(
        f"icon: {len(frames)} fresh pixel-art frames, "
        f"'{m['display']}' + {m['badge']} badge, structure invariants OK"
    )
    return zic


def main() -> None:
    zd2_make_effect.make_icon = _make_pixel_icon
    zd2_make_effect.main()


if __name__ == "__main__":
    main()
