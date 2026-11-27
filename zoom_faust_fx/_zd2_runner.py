"""Internal runner invoking stomphacks zd2_make_effect.py with pixel_icons hook."""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

from . import pixel_icons


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    stomphacks_dir = Path(args[0])
    manifest_path = Path(args[1])
    effect_dir = Path(args[2]) if len(args) > 2 and args[2] else None

    sys.path.insert(0, str(stomphacks_dir / "tools"))
    import zd2_from_scratch  # noqa: E402
    import zd2_make_effect  # noqa: E402

    def _make_pixel_icon(m: dict, builddir: Path, report: list) -> Path:
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
        eff_dir = effect_dir if effect_dir is not None else builddir.parent
        for p in frames:
            w, h = Image.open(p).size
            pixel_icons.render_pixel_icon(m, (w, h), effect_dir=eff_dir).save(p)
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

    zd2_make_effect.make_icon = _make_pixel_icon
    sys.argv = ["zd2_make_effect.py", str(manifest_path)]
    zd2_make_effect.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
