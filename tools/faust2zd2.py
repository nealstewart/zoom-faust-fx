#!/usr/bin/env python3
"""
tools/faust2zd2.py — Compile FAUST (.dsp) or FAUST-generated C into a Zoom MS Plus
effect directory (manifest.json + bare-metal C674x kernel) and optionally build
the installable .ZD2 + .ZIC pair via tools/zd2_make_effect.py.

Implementation is split into the modular `tools/faust2zd2_pkg/` package:
  - `models.py`          : FaustParam, FaustMeta, and `.dsp` metadata/slider parser
  - `rewriter.py`        : Bare-metal C674x call-free math header (`_rcpsp`, `_rsqrsp`)
                           and C division/float32 expression rewriter
  - `subset_compiler.py` : Clean-room FAUST `.dsp` subset transpiler (with block-rate
                           hoisting, `~` recursive feedback, and SDRAM delay lines)
  - `c_backend.py`       : External `faust -lang c` output converter
  - `emitter.py`         : C674x kernel and `manifest.json` code generator

Usage:
  python tools/faust2zd2.py effects/faustflg/faustflg.dsp --build
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from faust2zd2_pkg import (
    C674X_FAUST_HEADER,
    MATH_REDIRECTS,
    FaustMemoryLimitError,
    FaustMemoryReport,
    FaustMeta,
    FaustParam,
    FaustSubsetCompiler,
    _emit_c674x_kernel_from_parts,
    analyze_faust_memory,
    build_manifest_dict,
    convert_faust_c_to_zd2_kernel,
    enforce_float32_literals,
    parse_faust_dsp_metadata,
    rewrite_c_divisions_and_math,
    strip_c_comments,
)

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Compile a FAUST (.dsp) effect into a Zoom MS Plus C674x kernel + ZD2 package."
    )
    ap.add_argument("dsp", type=Path, help="Input FAUST .dsp file")
    ap.add_argument(
        "--from-c",
        type=Path,
        default=None,
        help="Optional pre-generated `faust -lang c` source file (skips invoking `faust` CLI)",
    )
    ap.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Target effect directory (defaults to parent directory of the .dsp file)",
    )
    ap.add_argument(
        "--faust-bin",
        type=str,
        default=os.environ.get("FAUST_BIN", "faust"),
        help="Path to `faust` executable if installed",
    )
    ap.add_argument(
        "--build",
        action="store_true",
        help="Immediately run `tools/zd2_make_effect.py` to compile and verify the .ZD2 package",
    )
    args = ap.parse_args()

    dsp_path = args.dsp.resolve()
    if not dsp_path.exists():
        sys.exit(f"faust2zd2: ERROR: DSP file not found: {dsp_path}")

    out_dir = (args.out_dir or (dsp_path.parent / "gen")).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    dsp_text = dsp_path.read_text(encoding="utf-8")
    meta, params = parse_faust_dsp_metadata(dsp_text)

    faust_c_code: Optional[str] = None
    mode_used = ""

    if args.from_c:
        faust_c_code = args.from_c.resolve().read_text(encoding="utf-8")
        mode_used = f"pre-generated FAUST C ({args.from_c.name})"
    else:
        faust_exe = shutil.which(args.faust_bin)
        if faust_exe:
            print(f"== Running external FAUST compiler: {faust_exe} -lang c ==")
            res = subprocess.run(
                [
                    faust_exe,
                    "-lang",
                    "c",
                    "-cn",
                    "mydsp",
                    "-I",
                    str(dsp_path.parent),
                    "-I",
                    str(ROOT / "effects"),
                    str(dsp_path),
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            faust_c_code = res.stdout
            mode_used = f"external faust CLI ({faust_exe})"

    mem_report: Optional[FaustMemoryReport] = None
    try:
        if faust_c_code is not None:
            kernel_c, state_bytes, params = convert_faust_c_to_zd2_kernel(
                faust_c_code, meta, params
            )
        else:
            mode_used = "built-in clean-room FAUST subset compiler"
            compiler = FaustSubsetCompiler(
                dsp_text,
                meta,
                params,
                search_dirs=[dsp_path.parent, ROOT / "effects", ROOT],
            )
            kernel_c, state_bytes, params = compiler.compile()
            mem_report = compiler.memory_report
    except FaustMemoryLimitError as exc:
        sys.exit(f"faust2zd2: ERROR: {exc}")

    stem = dsp_path.stem
    kernel_filename = f"{stem}.c"
    kernel_path = out_dir / kernel_filename
    manifest_path = out_dir / "manifest.json"

    kernel_path.write_text(kernel_c, encoding="utf-8")
    manifest_dict = build_manifest_dict(meta, params, kernel_filename, state_bytes)
    manifest_path.write_text(json.dumps(manifest_dict, indent=2) + "\n", encoding="utf-8")

    print(f"== faust2zd2: {meta.name} ({meta.filename}.ZD2, id 0x{meta.effect_id}) ==")
    print(f"   compiler mode : {mode_used}")
    print(f"   state_bytes   : {state_bytes} B (<= 188 B budget)")
    if mem_report is not None:
        print(
            f"   sdram_bytes   : {mem_report.physical_sdram_bytes} B "
            f"({mem_report.delay_line_count} lines, <= 529,200 B budget)"
        )
        print(
            f"   sample_temps  : {mem_report.sample_temp_bytes} B "
            f"({mem_report.sample_temp_count} locals, <= 2,048 B budget)"
        )
    print(f"   parameters    : {len(params)} ({', '.join(p.label for p in params)})")
    print(f"   wrote kernel  : {kernel_path}")
    print(f"   wrote manifest: {manifest_path}")

    if args.build:
        print("== Invoking tools/build_effect.py ==")
        subprocess.run(
            [sys.executable, str(ROOT / "tools" / "build_effect.py"), str(manifest_path)],
            check=True,
        )


if __name__ == "__main__":
    main()
