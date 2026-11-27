"""Faust -> raw C codegen & UI metadata extraction bridge."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .postprocess import (
    FaustAllowlistError,
    FaustMemoryReport,
    FaustMeta,
    FaustParam,
    FaustSubsetCompiler,
    build_manifest_dict,
    convert_faust_c_to_zd2_kernel,
    parse_faust_dsp_metadata,
    validate_expanded_faust,
    validate_faust_allowlist,
)


@dataclass(slots=True)
class FaustCompileOutput:
    """Result of compiling/lowering a Faust .dsp file into a C674x kernel + manifest."""

    kernel_c: str
    state_bytes: int
    params: list[FaustParam]
    meta: FaustMeta
    manifest: dict
    mode_used: str
    memory_report: Optional[FaustMemoryReport] = None


def extract_metadata(dsp_path: Path) -> tuple[FaustMeta, list[FaustParam]]:
    """Parse Faust `declare` metadata and UI controls from a .dsp file."""
    dsp_text = dsp_path.read_text(encoding="utf-8")
    return parse_faust_dsp_metadata(dsp_text)


def _rewrite_wavetable_oscs(dsp_text: str) -> str:
    """Rewrite os.osc(f)/os.osccos(f) to wavetable-free sin/cos(2*ma.PI*os.phasor(1.0, f)) for faust CLI."""
    replacements = (("os.osccos", "cos"), ("os.osc", "sin"))
    out = dsp_text
    for fn_name, trig in replacements:
        pat = re.compile(rf"\b{re.escape(fn_name)}\s*\(")
        pos = 0
        chunks: list[str] = []
        while True:
            m = pat.search(out, pos)
            if not m:
                chunks.append(out[pos:])
                break
            chunks.append(out[pos : m.start()])
            i = m.end()
            depth = 1
            while i < len(out) and depth > 0:
                if out[i] == "(":
                    depth += 1
                elif out[i] == ")":
                    depth -= 1
                i += 1
            if depth != 0:
                chunks.append(out[m.start() :])
                pos = len(out)
                break
            arg = out[m.end() : i - 1].strip()
            chunks.append(f"({trig}(2.0 * ma.PI * os.phasor(1.0, ({arg}))))")
            pos = i
        out = "".join(chunks)
    if re.search(r"\bzm_bpm_ms\b", out):
        out = re.sub(
            r"\bzm_bpm_ms\s*\(\s*([^,]+?)\s*,\s*([^)]+?)\s*\)",
            r"(min((\2), (60000.0 / zm_bpm) * (\1)))",
            out,
        )
    if re.search(r"\bzm_bpm\b", out):
        out = re.sub(r"\bzm_bpm\b", 'hslider("__zm_bpm__", 120.0, 30.0, 300.0, 0.1)', out)
    # Redirect zoom.lib Q31 LFO primitives to audio-rate ffunction markers for c_backend Loop 1
    if re.search(
        r"\bzm_(?:lf_sawpos|lf_sawpos_phase|lf_saw|lf_trianglepos|lf_triangle"
        r"|lf_squarewavepos|lf_squarewave|osc|osccos|oscp|quadosc|triosc|lf_sh|lfo_sync_hz)\b",
        out,
    ):
        lfo_overrides = """
zm_lf_sawpos(f)          = ffunction(float __sh_lfo_sawpos(float, int), "", "")(f, ba.time);
zm_lf_sawpos_phase(f, p) = ffunction(float __sh_lfo_sawpos_phase(float, float, int), "", "")(f, p, ba.time);
zm_lf_saw(f)             = ffunction(float __sh_lfo_saw(float, int), "", "")(f, ba.time);
zm_lf_trianglepos(f)     = ffunction(float __sh_lfo_trianglepos(float, int), "", "")(f, ba.time);
zm_lf_triangle(f)        = ffunction(float __sh_lfo_triangle(float, int), "", "")(f, ba.time);
zm_lf_squarewavepos(f)   = ffunction(float __sh_lfo_squarewavepos(float, int), "", "")(f, ba.time);
zm_lf_squarewave(f)      = ffunction(float __sh_lfo_squarewave(float, int), "", "")(f, ba.time);
zm_osc(f)                = ffunction(float __sh_lfo_osc(float, int), "", "")(f, ba.time);
zm_osccos(f)             = ffunction(float __sh_lfo_osccos(float, int), "", "")(f, ba.time);
zm_oscp(f, p)            = ffunction(float __sh_lfo_oscp(float, float, int), "", "")(f, p, ba.time);
zm_lf_sh(f)              = ffunction(float __sh_lfo_sh(float, int), "", "")(f, ba.time);
zm_lfo_sync_hz(c, a, b, s, bpm) = c;
"""
        out = out + "\n" + lfo_overrides
    return out


def compile_dsp_to_kernel(
    dsp_path: Path,
    *,
    from_c_path: Path | None = None,
    faust_bin: str | None = None,
    search_dirs: list[Path] | None = None,
) -> FaustCompileOutput:
    """Compile a Faust `.dsp` file (or pre-generated C) into a Stomphacks C674x kernel."""
    dsp_path = dsp_path.resolve()
    dsp_text = dsp_path.read_text(encoding="utf-8")
    validate_faust_allowlist(dsp_text)
    meta, params = parse_faust_dsp_metadata(dsp_text)

    faust_c_code: Optional[str] = None
    mode_used = ""
    inc_dirs = list(search_dirs or [])
    if dsp_path.parent not in inc_dirs:
        inc_dirs.insert(0, dsp_path.parent)
    if dsp_path.parent.parent not in inc_dirs:
        inc_dirs.append(dsp_path.parent.parent)

    mem_report: Optional[FaustMemoryReport] = None
    kernel_c: Optional[str] = None
    state_bytes = 0

    if from_c_path is not None:
        faust_c_code = from_c_path.resolve().read_text(encoding="utf-8")
        mode_used = f"pre-generated FAUST C ({from_c_path.name})"
        kernel_c, state_bytes, params = convert_faust_c_to_zd2_kernel(
            faust_c_code, meta, params
        )
    else:
        exe_name = faust_bin or os.environ.get("FAUST_BIN", "faust")
        faust_exe = shutil.which(exe_name)
        require_cli = os.environ.get("FAUST_REQUIRE_CLI") == "1"
        if faust_exe:
            inc_args: list[str] = []
            for d in inc_dirs:
                if d.is_dir():
                    inc_args.extend(["-I", str(d)])
            rewritten_dsp = _rewrite_wavetable_oscs(dsp_text)
            try:
                with tempfile.NamedTemporaryFile(
                    suffix=".dsp", mode="w", encoding="utf-8", delete=False
                ) as src_tf, tempfile.NamedTemporaryFile(suffix=".dsp", delete=False) as exp_tf:
                    cli_dsp_path = Path(src_tf.name)
                    src_tf.write(rewritten_dsp)
                    exp_path = Path(exp_tf.name)
                try:
                    exp_cmd = [faust_exe, "-e", "-o", str(exp_path), *inc_args, str(cli_dsp_path)]
                    subprocess.run(exp_cmd, capture_output=True, text=True, check=True)
                    validate_expanded_faust(exp_path.read_text(encoding="utf-8"))
                    cmd = [faust_exe, "-lang", "c", "-cn", "mydsp", *inc_args, str(cli_dsp_path)]
                    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
                finally:
                    cli_dsp_path.unlink(missing_ok=True)
                    exp_path.unlink(missing_ok=True)
                faust_c_code = res.stdout
                kernel_c, state_bytes, params = convert_faust_c_to_zd2_kernel(
                    faust_c_code, meta, params
                )
                mode_used = f"external faust CLI ({faust_exe})"
            except FaustAllowlistError:
                raise
            except Exception:
                if require_cli:
                    raise
                kernel_c = None
        elif require_cli:
            raise FileNotFoundError(
                f"Required Faust CLI '{exe_name}' was not found on PATH (FAUST_REQUIRE_CLI=1)."
            )

    if kernel_c is None:
        mode_used = "built-in clean-room FAUST subset compiler"
        compiler = FaustSubsetCompiler(
            dsp_text,
            meta,
            params,
            search_dirs=inc_dirs,
        )
        kernel_c, state_bytes, params = compiler.compile()
        mem_report = compiler.memory_report

    kernel_filename = f"{dsp_path.stem}.c"
    manifest_dict = build_manifest_dict(meta, params, kernel_filename, state_bytes)
    return FaustCompileOutput(
        kernel_c=kernel_c,
        state_bytes=state_bytes,
        params=params,
        meta=meta,
        manifest=manifest_dict,
        mode_used=mode_used,
        memory_report=mem_report,
    )
