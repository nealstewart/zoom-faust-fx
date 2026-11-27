#!/usr/bin/env python3
"""
web/build_wasm.py — Compile all Stomphacks C effect kernels to WebAssembly (.wasm)
and build the catalog (`web/dist/effects.json`) + icons + dry audio samples for
the browser Web Audio auditioner.

Uses `.pixi`'s bundled Zig (`zig clang -target wasm32-freestanding`) so zero
external toolchains (like Emscripten) are required.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
STOMPHACKS_DIR = Path(
    os.environ.get("STOMPHACKS_DIR", ROOT / "vendor" / "stomphacks")
).resolve()
EFFECTS_DIR = ROOT / "effects"
STOMPHACKS_EFFECTS_DIR = STOMPHACKS_DIR / "effects"
SAMPLES_DIR = ROOT / "samples"
WEB_DIR = Path(__file__).resolve().parent
DIST_DIR = WEB_DIR / "dist"
WASM_DIR = DIST_DIR / "wasm"
ICONS_DIR = DIST_DIR / "icons"
DIST_SAMPLES_DIR = DIST_DIR / "samples"

FREE_COEFF_SLOTS = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14]

DRY_SAMPLES = [
    ("ebm_0_dry_synth.wav", "EBM Bass Synth (Dry)"),
    ("ps1_0_dry_detuned_pad.wav", "Detuned Pad (Dry)"),
    ("vocals_ps1_0_dry.wav", "Dry Vocals"),
    ("formant_blips_0_dry.wav", "Formant Blips (Dry)"),
    ("formant_rubs_0_dry.wav", "Formant Rubs (Dry)"),
]


def find_zig_exe() -> Path:
    candidates = [
        ROOT / ".pixi" / "envs" / "default" / "Library" / "bin" / "x86_64-w64-mingw32-zig.exe",
        ROOT / ".pixi" / "envs" / "default" / "Library" / "bin" / "zig.exe",
        ROOT / ".pixi" / "envs" / "default" / "bin" / "zig",
    ]
    for c in candidates:
        if c.is_file():
            return c
    w = shutil.which("zig") or shutil.which("x86_64-w64-mingw32-zig")
    if w:
        return Path(w)
    raise RuntimeError("Could not find zig executable in .pixi or on PATH.")


def param_default_coeff(p: dict) -> float:
    d = p.get("default", 0)
    if p.get("curve") == "stock_vol":
        return (d / 80.0) if d <= 80 else (1.0 + (d - 80.0) / 40.0)
    p_max = p.get("max", len(p["values"]) - 1 if "values" in p else 100)
    return (d / float(p_max)) if p_max > 0 else 0.0


def synthesize_sh_params_h(manifest: dict) -> tuple[str, list[dict]]:
    sym = re.sub(r"[^A-Za-z0-9]", "", manifest["name"])
    fn_name = f"Fx_SFX_{sym}"
    state_bytes = int(manifest.get("state_bytes", 16))
    guard_word = state_bytes // 4

    param_defines = []
    enriched_params = []
    slot_idx = 0
    for idx, p in enumerate(manifest.get("params", [])):
        p_name = p["name"]
        macro = "SH_PARAM_" + re.sub(r"[^A-Za-z0-9]+", "_", p_name.strip()).strip("_").upper()
        slot = p.get("slot", FREE_COEFF_SLOTS[slot_idx])
        slot_idx += 1
        param_defines.append(f"#define {macro} {slot}")

        values = p.get("values")
        p_max = p.get("max", (len(values) - 1) if values else 100)
        p_def = p.get("default", 0)
        enriched_params.append({
            "index": idx,
            "name": p_name,
            "slot": slot,
            "min": 0,
            "max": p_max,
            "default": p_def,
            "defaultCoeff": param_default_coeff(p),
            "curve": p.get("curve", "linear"),
            "values": values,
            "timeSync": p.get("time_sync"),
            "footswitchToggle": bool(p.get("footswitch_toggle", False)),
            "explanation": p.get("explanation", ""),
        })

    header = f"""#ifndef SH_PARAMS_H
#define SH_PARAMS_H

#define SH_AUDIO_FN {fn_name}
void {fn_name}(void **instance, void **ctx);

#define SH_FRAMES        16
#define SH_CH_B_OFFSET   16
#define SH_COEFF_BYPASS  0
#define SH_COEFF_BPM     15
#define SH_CTX_GTRIN     0
#define SH_CTX_EFF       1
#define SH_CTX_OUT       2
#define SH_CTX_SHUTTLE   7
#define SH_CTX_LEGACY_IN 8
#define SH_SAMPLE_RATE   44100.0f
#define SH_STATE_BYTES   {state_bytes}
#define SH_STATE_GUARD       0x57464731u
#define SH_STATE_GUARD_WORD  {guard_word}
#define SH_NUM_PARAMS    {len(param_defines)}
{chr(10).join(param_defines)}

#endif
"""
    return header, enriched_params


def make_wasm_shim(fn_name: str, enriched_params: list[dict], needs_sdram: bool) -> str:
    sdram_bytes = 1048576 if needs_sdram else 4096
    default_unlatch = any(
        p.get("footswitchToggle") and p.get("default", 0) == 1
        for p in enriched_params
    )
    init_bypass = "0.0f" if default_unlatch else "1.0f"
    init_coefs = "\n".join(
        f"    g_coef[{p['slot']}] = {float(p['defaultCoeff']):.9f}f;"
        for p in enriched_params
    )
    return f"""
/* ==== WebAssembly AudioWorklet Shim ==== */
#include <stddef.h>
#include <stdint.h>

void *memset(void *dst, int c, size_t n) {{
    uint8_t *p = (uint8_t *)dst;
    uint8_t v = (uint8_t)c;
    for (size_t i = 0; i < n; i++) p[i] = v;
    return dst;
}}

void *memcpy(void *dst, const void *src, size_t n) {{
    uint8_t *d = (uint8_t *)dst;
    const uint8_t *s = (const uint8_t *)src;
    for (size_t i = 0; i < n; i++) d[i] = s[i];
    return dst;
}}

#define WASM_SDRAM_BYTES {sdram_bytes}u
#define WASM_STATE_WORDS 64u

static float    g_coef[16];
static uint32_t g_state[WASM_STATE_WORDS];
static float    g_eff[32];
static float    g_aux[32];
static float    g_gtrin[16];
static float    g_shuttle[32];
static float    g_legacy[32];
static uint32_t g_dd[3];
static void    *g_instance[4];
static void    *g_ctx[9];
static uint8_t  g_sdram[WASM_SDRAM_BYTES] __attribute__((aligned(16)));

static float    g_bypass_target = {init_bypass};
static float    g_bypass_cur    = {init_bypass};

__attribute__((export_name("wasm_init")))
void wasm_init(void) {{
    for (uint32_t i = 0; i < 16u; i++) g_coef[i] = 0.0f;
    g_coef[15] = 500.0f;  /* default 120 BPM quarter-note ms */
    for (uint32_t i = 0; i < WASM_STATE_WORDS; i++) g_state[i] = 0u;
    for (uint32_t i = 0; i < 32u; i++) {{
        g_eff[i] = 0.0f;
        g_aux[i] = 0.0f;
        g_shuttle[i] = 0.0f;
        g_legacy[i] = 0.0f;
    }}
    for (uint32_t i = 0; i < 16u; i++) g_gtrin[i] = 0.0f;
    for (uint32_t i = 0; i < WASM_SDRAM_BYTES; i++) g_sdram[i] = 0u;

    g_state[SH_STATE_GUARD_WORD] = SH_STATE_GUARD;

    g_bypass_target = {init_bypass};
    g_bypass_cur    = {init_bypass};
    g_coef[0]       = {init_bypass};
{init_coefs}

    uint32_t base = (uint32_t)(uintptr_t)&g_sdram[0];
    g_dd[0] = base;
    g_dd[1] = base + WASM_SDRAM_BYTES;
    g_dd[2] = WASM_SDRAM_BYTES;

    g_instance[0] = (void *)0;
    g_instance[1] = (void *)g_coef;
    g_instance[2] = (void *)g_state;
    g_instance[3] = (void *)g_dd;

    for (uint32_t i = 0; i < 9u; i++) g_ctx[i] = (void *)0;
    g_ctx[SH_CTX_GTRIN]     = (void *)g_gtrin;
    g_ctx[SH_CTX_EFF]       = (void *)g_eff;
    g_ctx[SH_CTX_OUT]       = (void *)g_aux;
    g_ctx[SH_CTX_SHUTTLE]   = (void *)g_shuttle;
    g_ctx[SH_CTX_LEGACY_IN] = (void *)g_legacy;
}}

__attribute__((export_name("wasm_reset_audio")))
void wasm_reset_audio(void) {{
    for (uint32_t i = 0; i < WASM_STATE_WORDS; i++) g_state[i] = 0u;
    for (uint32_t i = 0; i < WASM_SDRAM_BYTES; i++) g_sdram[i] = 0u;
    g_state[SH_STATE_GUARD_WORD] = SH_STATE_GUARD;
}}

__attribute__((export_name("wasm_set_param")))
void wasm_set_param(int slot, float value) {{
    if (slot >= 0 && slot < 16) {{
        g_coef[slot] = value;
    }}
}}

__attribute__((export_name("wasm_set_bypass")))
void wasm_set_bypass(float target) {{
    if (target < 0.0f) target = 0.0f;
    if (target > 1.0f) target = 1.0f;
    g_bypass_target = target;
}}

__attribute__((export_name("wasm_get_eff_ptr")))
float *wasm_get_eff_ptr(void) {{ return g_eff; }}

__attribute__((export_name("wasm_get_aux_ptr")))
float *wasm_get_aux_ptr(void) {{ return g_aux; }}

__attribute__((export_name("wasm_get_gtrin_ptr")))
float *wasm_get_gtrin_ptr(void) {{ return g_gtrin; }}

__attribute__((export_name("wasm_process_block")))
void wasm_process_block(void) {{
    /* Smooth bypass fade over ~16 blocks (~5.8 ms at 44.1 kHz), matching firmware */
    float diff = g_bypass_target - g_bypass_cur;
    if (diff > 0.0625f) g_bypass_cur += 0.0625f;
    else if (diff < -0.0625f) g_bypass_cur -= 0.0625f;
    else g_bypass_cur = g_bypass_target;
    g_coef[0] = g_bypass_cur;

    {fn_name}(g_instance, g_ctx);
}}
"""


def compile_effect(zig_exe: Path, effect_dir: Path, tmp_dir: Path) -> Optional[dict]:
    gen_dir = effect_dir / "gen" if (effect_dir / "gen" / "manifest.json").is_file() else effect_dir
    manifest_path = gen_dir / "manifest.json"
    if not manifest_path.is_file():
        return None

    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    kernel_name = manifest.get("kernel")
    if not kernel_name:
        return None
    kernel_path = gen_dir / kernel_name
    if not kernel_path.is_file():
        return None

    slug = effect_dir.name
    sym = re.sub(r"[^A-Za-z0-9]", "", manifest["name"])
    fn_name = f"Fx_SFX_{sym}"

    sh_params_h, enriched_params = synthesize_sh_params_h(manifest)
    kernel_src = kernel_path.read_text(encoding="utf-8")
    needs_sdram = ("instance[3]" in kernel_src) or ("dd[0]" in kernel_src)

    patched_src = kernel_src.replace('#include "sh_params.h"', sh_params_h)
    patched_src += make_wasm_shim(fn_name, enriched_params, needs_sdram)

    c_file = tmp_dir / f"{slug}.c"
    obj_file = tmp_dir / f"{slug}.o"
    wasm_file = WASM_DIR / f"{slug}.wasm"
    c_file.write_text(patched_src, encoding="utf-8")

    zig_real = zig_exe.resolve()
    zig_inc_candidates = [
        zig_real.parent / "lib" / "include",
        zig_real.parent / "lib" / "zig" / "include",
        zig_real.parent.parent / "lib" / "zig" / "include",
        zig_exe.parent.parent / "lib" / "zig" / "include",
    ]
    zig_inc = next((p for p in zig_inc_candidates if p.is_dir()), None)
    cmd_cc = [
        str(zig_exe),
        "clang",
        "-target", "wasm32-freestanding",
        "-ffreestanding",
        "-fno-builtin",
        "-O2",
        "-c",
        "-Wno-unused-value",
        "-Wno-parentheses-equality",
        "-Wno-unused-variable",
    ]
    if zig_inc is not None:
        cmd_cc.extend(["-isystem", str(zig_inc)])
    cmd_cc.extend([str(c_file), "-o", str(obj_file)])

    res_cc = subprocess.run(cmd_cc, capture_output=True, text=True)
    if res_cc.returncode != 0:
        raise RuntimeError(f"[{slug}] clang failed:\n{res_cc.stderr}\n{res_cc.stdout}")

    cmd_ld = [
        str(zig_exe),
        "wasm-ld",
        "--no-entry",
        "--export-dynamic",
        "--strip-all",
        str(obj_file),
        "-o", str(wasm_file),
    ]
    res_ld = subprocess.run(cmd_ld, capture_output=True, text=True)
    if res_ld.returncode != 0:
        raise RuntimeError(f"[{slug}] wasm-ld failed:\n{res_ld.stderr}\n{res_ld.stdout}")

    # Render programmatic 1-bit pixel-art icon for every effect (matches Zoom ZIC)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from zoom_faust_fx import pixel_icons
    icon_dst = ICONS_DIR / f"{slug}.png"
    pixel_icons.render_pixel_icon(manifest, (72, 97), effect_dir=effect_dir).save(icon_dst)
    icon_rel = f"dist/icons/{slug}.png"

    # Check if a built ZD2 exists
    zd2_name = None
    build_dir = gen_dir / "build"
    if build_dir.is_dir():
        for f in build_dir.glob("*.ZD2"):
            if "_template" not in f.name and "_rebuilt" not in f.name:
                zd2_name = f.name
                break

    # Categorize for UI filtering
    if slug in ("wfsplit", "wfswap", "wfmerge", "gain"):
        category = "Utility / Routing"
    elif slug in ("xarvb", "xaspu", "xadly", "scsprvb", "rf5cfx", "rf5cdly", "brrdsp", "firecho", "sophwg"):
        category = "Reverb / Space"
    elif slug in ("slewdrv", "anlgdrv", "swedsaw", "deskdrv", "ym2612"):
        category = "Drive / Dirt"
    elif slug in ("cdskip", "stutrtch", "rackptch"):
        category = "Glitch / Pitch"
    else:
        category = "Mod / Filter"

    return {
        "slug": slug,
        "name": manifest.get("name", slug),
        "display": manifest.get("display", manifest.get("name", slug)),
        "badge": manifest.get("badge", "SH"),
        "id": manifest.get("id", ""),
        "filename": manifest.get("filename", ""),
        "zd2": zd2_name,
        "version": manifest.get("version", "1.00"),
        "author": manifest.get("author", "stomphacks"),
        "description": manifest.get("description", ""),
        "dspload": manifest.get("dspload", 0),
        "category": category,
        "usesSdram": needs_sdram,
        "wasmUrl": f"dist/wasm/{slug}.wasm",
        "wasmBytes": wasm_file.stat().st_size,
        "iconUrl": icon_rel,
        "hasFootswitchToggle": any(p["footswitchToggle"] for p in enriched_params),
        "params": enriched_params,
    }


def main() -> int:
    zig_exe = find_zig_exe()
    WASM_DIR.mkdir(parents=True, exist_ok=True)
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    DIST_SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Using Zig: {zig_exe}")
    catalog: List[dict] = []

    with tempfile.TemporaryDirectory(prefix="stomphacks_wasm_") as tmp:
        tmp_dir = Path(tmp)
        effect_dirs: Dict[str, Path] = {}
        if STOMPHACKS_EFFECTS_DIR.is_dir():
            for d in sorted(STOMPHACKS_EFFECTS_DIR.iterdir()):
                if d.is_dir():
                    effect_dirs[d.name] = d
        for d in sorted(EFFECTS_DIR.iterdir()):
            if d.is_dir():
                effect_dirs[d.name] = d
        for slug in sorted(effect_dirs):
            info = compile_effect(zig_exe, effect_dirs[slug], tmp_dir)
            if info:
                catalog.append(info)
                print(
                    f"  [OK] {info['slug']:10s} -> {info['wasmBytes']:6d} bytes "
                    f"({len(info['params'])} params, sdram={'1MB' if info['usesSdram'] else '4KB'})"
                )

    samples_meta = []
    for fname, label in DRY_SAMPLES:
        src = SAMPLES_DIR / fname
        if src.is_file():
            dst = DIST_SAMPLES_DIR / fname
            shutil.copy2(src, dst)
            samples_meta.append({
                "id": fname,
                "label": label,
                "url": f"dist/samples/{fname}",
                "bytes": dst.stat().st_size,
            })

    out_json = DIST_DIR / "effects.json"
    payload = {
        "sampleRate": 44100,
        "blockFrames": 16,
        "effects": catalog,
        "samples": samples_meta,
    }
    out_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nBuilt {len(catalog)} WASM effects + {len(samples_meta)} dry samples -> {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
