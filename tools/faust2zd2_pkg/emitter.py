"""
tools/faust2zd2_pkg/emitter.py — Shared C674x Zoom MS Plus kernel & `manifest.json` emitter.
"""

from __future__ import annotations

import math
import re
from typing import List, Optional, Tuple

from .models import (
    MAX_DELAY_LINES,
    MAX_SAFE_SDRAM_BYTES,
    MAX_SAMPLE_TEMPS,
    MAX_STATE_BYTES,
    FaustMemoryLimitError,
    FaustMeta,
    FaustParam,
)
from .rewriter import C674X_FAUST_HEADER, rewrite_c_divisions_and_math


def _emit_c674x_kernel_from_parts(
    meta: FaustMeta,
    params: List[FaustParam],
    state_vars: List[Tuple[str, str, int]],
    smoo_bindings: List[Tuple[str, str]],
    pre_loop: str,
    loop_inner: str,
    delay_wpos_var: Optional[str] = None,
    delay_line_count: int = 0,
    delay_step_expr: Optional[str] = None,
    delay_line_len: int = 1024,
) -> Tuple[str, int, List[FaustParam]]:
    """
    Assembles the complete, self-contained C674x C kernel source file.
    Operates directly on `instance[2]` with zero stack-struct duplication,
    zero 64-bit `double` constants, first-block smoother snap, and full NaN guards.
    """
    all_state_vars = list(state_vars) + [("uint32_t", "init_magic", 1)]
    total_words = sum(alen for _, _, alen in all_state_vars)

    if total_words * 4 > MAX_STATE_BYTES:
        raise FaustMemoryLimitError(
            f"FAUST DSP struct requires {total_words * 4} bytes ({total_words} words) of state, "
            f"which exceeds the Zoom MS Plus {MAX_STATE_BYTES}-byte instance[2] limit."
        )
    state_bytes = min(MAX_STATE_BYTES, ((total_words * 4 + 7) // 8) * 8)
    if state_bytes < 16:
        state_bytes = 16

    sample_temp_decls = len(
        re.findall(r"\b(?:float|int|uint32_t|FAUSTFLOAT)\s+[A-Za-z_][A-Za-z0-9_]*\s*=", loop_inner)
    )
    if sample_temp_decls > MAX_SAMPLE_TEMPS:
        raise FaustMemoryLimitError(
            f"FAUST DSP audio loop emits {sample_temp_decls} per-sample local temporary variables "
            f"({sample_temp_decls * 4} bytes), exceeding the {MAX_SAMPLE_TEMPS}-variable "
            f"({MAX_SAMPLE_TEMPS * 4}-byte) C674x stack/register spill limit across function invocations."
        )

    loop_inner = re.sub(r"\binput0\s*\[\s*i0\s*\]", "in0", loop_inner)
    loop_inner = re.sub(r"\binput1\s*\[\s*i0\s*\]", "in1", loop_inner)
    loop_inner = re.sub(r"\boutput0\s*\[\s*i0\s*\]", "out0", loop_inner)
    loop_inner = re.sub(r"\boutput1\s*\[\s*i0\s*\]", "out1", loop_inner)

    for p in params:
        pre_loop = re.sub(rf"\bdsp->{re.escape(p.var_name)}\b", p.var_name, pre_loop)
        loop_inner = re.sub(rf"\bdsp->{re.escape(p.var_name)}\b", p.var_name, loop_inner)

    pre_loop_clean = rewrite_c_divisions_and_math(pre_loop)
    loop_inner_clean = rewrite_c_divisions_and_math(loop_inner)

    uses_out1 = bool(re.search(r"\bout1\b", loop_inner_clean))
    mono_tail = "\n        out1 = out0;" if not uses_out1 else ""

    def _build_state_io_lines(
        svars: List[Tuple[str, str, int]], indent: str = "    "
    ) -> Tuple[List[str], List[str], List[str]]:
        u_lines: List[str] = []
        s_lines: List[str] = []
        c_lines: List[str] = []
        for ctype, vname, alen in svars:
            if alen == 1:
                u_lines.append(f"{indent}{ctype} st_{vname} = pstate->{vname};")
                if ctype == "float":
                    s_lines.append(
                        f"{indent}if (!(st_{vname} > -1.0e12f && st_{vname} < 1.0e12f)) st_{vname} = 0.0f;"
                    )
                c_lines.append(f"{indent}pstate->{vname} = st_{vname};")
            else:
                for k in range(alen):
                    u_lines.append(f"{indent}{ctype} st_{vname}_{k} = pstate->{vname}[{k}];")
                    if ctype == "float":
                        s_lines.append(
                            f"{indent}if (!(st_{vname}_{k} > -1.0e12f && st_{vname}_{k} < 1.0e12f)) st_{vname}_{k} = 0.0f;"
                        )
                    c_lines.append(f"{indent}pstate->{vname}[{k}] = st_{vname}_{k};")
        return u_lines, s_lines, c_lines

    unpack_lines, scrub_lines, commit_lines = _build_state_io_lines(state_vars, "    ")

    def replace_dsp_refs(code_str: str) -> str:
        code_str = re.sub(r"\bdsp->(\w+)\[(\d+)\]", r"st_\1_\2", code_str)
        code_str = re.sub(r"\bdsp->(\w+)\b", r"st_\1", code_str)
        return code_str

    pre_loop_clean = replace_dsp_refs(pre_loop_clean)
    loop_inner_clean = replace_dsp_refs(loop_inner_clean)

    coef_load_lines = ["    float fade = coef[SH_COEFF_BYPASS];"]
    coef_check_lines = ["    if (!(fade >= 0.0f && fade <= 1.0f)) ok = 0u;"]
    slider_map_lines = []

    for idx, p in enumerate(params):
        cvar = f"raw_p{idx}"
        coef_load_lines.append(f"    float {cvar} = coef[{p.c_macro_name}];")
        if p.is_stock_vol:
            coef_check_lines.append(f"    if (!({cvar} >= 0.0f && {cvar} <= 1.6f)) ok = 0u;")
            slider_map_lines.append(f"    float {p.var_name} = {cvar};")
        else:
            coef_check_lines.append(f"    if (!({cvar} >= 0.0f && {cvar} <= 1.0f)) ok = 0u;")
            span = p.max_val - p.min_val
            if p.scale == "log" and p.min_val > 0.0 and p.max_val > p.min_val:
                ln_ratio = math.log(p.max_val / p.min_val)
                slider_map_lines.append(
                    f"    float {p.var_name} = {p.min_val:.6f}f * sh_expf({cvar} * {ln_ratio:.6f}f);"
                )
            elif p.scale == "exp":
                slider_map_lines.append(
                    f"    float {p.var_name} = {p.min_val:.6f}f + ({cvar} * {cvar}) * {span:.6f}f;"
                )
            else:
                slider_map_lines.append(
                    f"    float {p.var_name} = {p.min_val:.6f}f + {cvar} * {span:.6f}f;"
                )

    smoo_lines = []
    if smoo_bindings:
        smoo_lines.append("    if (pstate->init_magic != 0x46535431u) {")
        for svar, texpr in smoo_bindings:
            texpr_clean = replace_dsp_refs(rewrite_c_divisions_and_math(texpr))
            smoo_lines.append(f"        st_{svar} = {texpr_clean};")
        smoo_lines.append("        pstate->init_magic = 0x46535431u;")
        smoo_lines.append("    } else {")
        for svar, texpr in smoo_bindings:
            texpr_clean = replace_dsp_refs(rewrite_c_divisions_and_math(texpr))
            smoo_lines.append(
                f"        st_{svar} += 0.03152f * (({texpr_clean}) - st_{svar});"
            )
        smoo_lines.append("    }")

    sdram_guard_lines = []
    sdram_ptr_lines = []
    wpos_step_tail = ""
    delay_defines = ""
    if delay_line_count > 0 and delay_wpos_var:
        if delay_line_count > MAX_DELAY_LINES:
            raise FaustMemoryLimitError(
                f"FAUST DSP allocates {delay_line_count} parallel SDRAM delay lines across function "
                f"invocations, exceeding the {MAX_DELAY_LINES}-line C6745 L1D/L2 cache-set thrashing limit."
            )
        p2_len = 1024
        while p2_len < delay_line_len:
            p2_len *= 2
        p2_mask = p2_len - 1
        delay_defines = f"#define SH_DELAY_LEN  {p2_len}u\n#define SH_DELAY_MASK {p2_mask}u\n"
        # Pad each power-of-two ring buffer by +16 floats (+64 B = 1 L1D cache line)
        # so multi-line accesses at the same `wpos` skew across distinct L1D cache sets
        # and never thrash the C6745's 2-way set-associative L1D cache.
        line_stride_floats = p2_len + 16
        need_bytes = delay_line_count * line_stride_floats * 4
        if need_bytes > MAX_SAFE_SDRAM_BYTES:
            raise FaustMemoryLimitError(
                f"FAUST DSP requires {need_bytes} bytes ({delay_line_count} x {line_stride_floats}-float lines) of "
                f"instance[3] SDRAM delay memory, which exceeds the 529,200-byte "
                f"(3.0 s @ 44.1 kHz) safety cap (1.0 s guard margin below the 705,600-byte slot limit)."
            )
        sdram_guard_lines = [
            "    const uint32_t * restrict dd = (const uint32_t * restrict)instance[3];",
            "    uintptr_t dbaseAddr = 0u;",
            "    if (!dd) {",
            "        ok = 0u;",
            "    } else {",
            "        uint32_t b = dd[0], e = dd[1], sz = dd[2];",
            f"        if (b == 0u || (b & 3u) != 0u || sz < {need_bytes}u || e <= b || (uint32_t)(e - b) != sz) {{",
            "            ok = 0u;",
            "        } else {",
            "            dbaseAddr = (uintptr_t)b;",
            "        }",
            "    }",
        ]
        for idx in range(delay_line_count):
            sdram_ptr_lines.append(
                f"    float * restrict dline_{idx} = ((float *)dbaseAddr) + {idx * line_stride_floats}u;"
            )
        sdram_ptr_lines.append(f"    st_{delay_wpos_var} &= {p2_mask}u;")
        if delay_step_expr:
            step_clean = replace_dsp_refs(rewrite_c_divisions_and_math(delay_step_expr))
            wpos_step_tail = f"\n        st_{delay_wpos_var} = (st_{delay_wpos_var} + ((({step_clean}) > 0.5f) ? 1u : 0u)) & {p2_mask}u;"
        else:
            wpos_step_tail = f"\n        st_{delay_wpos_var} = (st_{delay_wpos_var} + 1u) & {p2_mask}u;"

    loop_section_c = f"""    /* ---- 5. FAUST sample-rate loop (16 frames, stereo in-place) -------- */
#ifdef __TI_COMPILER_VERSION__
#pragma MUST_ITERATE(SH_FRAMES, SH_FRAMES)
#pragma UNROLL(1)
#endif
    for (i0 = 0; i0 < SH_FRAMES; i0++) {{
        float dryL = eff[i0];
        float dryR = eff[i0 + SH_CH_B_OFFSET];
        if (!(dryL > -8.0f && dryL < 8.0f)) dryL = 0.0f;
        if (!(dryR > -8.0f && dryR < 8.0f)) dryR = 0.0f;

        float in0  = dryL;
        float in1  = dryR;
        float out0 = 0.0f;
        float out1 = 0.0f;
        (void)in1;

        {loop_inner_clean}{mono_tail}

        /* Firmware bypass crossfade & mandatory (-8, +8) output NaN clamp */
        float outL = dryL + fade * (out0 - dryL);
        float outR = dryR + fade * (out1 - dryR);
        if (!(outL > -8.0f && outL < 8.0f)) outL = 0.0f;
        if (!(outR > -8.0f && outR < 8.0f)) outR = 0.0f;

        eff[i0]                  = outL;
        eff[i0 + SH_CH_B_OFFSET] = outR;{wpos_step_tail}
    }}"""

    fsw_decl = "    float fsw_unlatch = 0.0f;\n" if meta.footswitch is not None else ""
    fsw_load = "        fsw_unlatch = coef[SH_PARAM_ON_OFF];\n" if meta.footswitch is not None else ""
    if meta.footswitch is not None:
        coef_check_lines.append("    if (!(fsw_unlatch >= 0.0f && fsw_unlatch <= 1.0f)) ok = 0u;")

    kernel_c = f"""/* Auto-generated by tools/faust2zd2.py from FAUST DSP specification.
 * Effect: {meta.name} ({meta.filename}.ZD2, ID 0x{meta.effect_id})
 * Target: Zoom MS Plus (TI TMS320C674x bare-metal DSP, 44.1 kHz, 0 CALL, 0 B14)
 * Persistent state: {total_words} words ({total_words * 4} B allocated inside state_bytes={state_bytes})
 */

#include <stdint.h>
#include "sh_params.h"

typedef float FAUSTFLOAT;
{delay_defines}
{C674X_FAUST_HEADER}

/* Persistent FAUST DSP state mapped directly onto instance[2] (zero stack copy!) */
typedef struct {{
{chr(10).join(f"    {ctype} {vname}" + (f"[{alen}];" if alen > 1 else ";") for ctype, vname, alen in all_state_vars)}
}} faust_zd2_state_t;

void SH_AUDIO_FN(void **instance, void **ctx)
{{
    int i0;
    if (!instance || !ctx) return;
    float * restrict eff = (float * restrict)ctx[1];
    if (!eff) return;

    const float * restrict coef = (const float * restrict)instance[1];
    faust_zd2_state_t * restrict pstate = (faust_zd2_state_t * restrict)instance[2];

    /* ---- 1. Mandatory fail-closed guard prologue (SAFETY.md) ----------- */
    unsigned ok = (coef != 0 && pstate != 0) ? 1u : 0u;
#ifdef SH_STATE_GUARD
    if (ok && ((const uint32_t *)pstate)[SH_STATE_GUARD_WORD] != SH_STATE_GUARD)
        ok = 0u;
#endif
    float fade = 0.0f;
{fsw_decl}{chr(10).join(f"    float raw_p{idx} = 0.0f;" for idx in range(len(params)))}
    if (ok) {{
        fade = coef[SH_COEFF_BYPASS];
{fsw_load}{chr(10).join(f"        raw_p{idx} = coef[{p.c_macro_name}];" for idx, p in enumerate(params))}
{chr(10).join("    " + line for line in coef_check_lines)}
    }}
{chr(10).join(sdram_guard_lines)}

    if (!ok) {{
        for (i0 = 0; i0 < SH_FRAMES; i0++) {{
            eff[i0]                  = 0.0f;
            eff[i0 + SH_CH_B_OFFSET] = 0.0f;
        }}
        return;
    }}

    /* ---- 2. Unpack persistent state to registers & scrub NaN / Inf ----- */
{chr(10).join(unpack_lines)}
{chr(10).join(scrub_lines)}
{chr(10).join(sdram_ptr_lines)}

    /* ---- 3. Map normalized Zoom coefficients [0..1] to FAUST sliders --- */
{chr(10).join(slider_map_lines)}

    /* ---- 4. Block-rate parameter smoothing & control setup ------------- */
{chr(10).join(smoo_lines)}
    {pre_loop_clean}

{loop_section_c}

    /* ---- 6. Commit register state back to instance[2] ------------------ */
{chr(10).join(commit_lines)}
}}
"""
    return kernel_c, state_bytes, params


def build_manifest_dict(
    meta: FaustMeta,
    params: List[FaustParam],
    kernel_filename: str,
    state_bytes: int,
) -> dict:
    """Create the `manifest.json` structure for `tools/zd2_make_effect.py`."""
    manifest_params = []
    for p in params:
        if p.is_stock_vol and not p.values:
            steps = 100
        else:
            steps = int(round((p.max_val - p.min_val) / p.step)) if p.step > 0 else 100
            if steps < 1 or steps > 255:
                steps = 100
        if p.scale == "log" and p.min_val > 0.0 and p.max_val > p.min_val and p.init >= p.min_val:
            norm_init = math.log(p.init / p.min_val) / math.log(p.max_val / p.min_val)
        elif p.scale == "exp" and p.max_val > p.min_val and p.init >= p.min_val:
            norm_init = math.sqrt((p.init - p.min_val) / (p.max_val - p.min_val))
        else:
            norm_init = (p.init - p.min_val) / (p.max_val - p.min_val) if p.max_val > p.min_val else 0.0
        default_click = int(round(norm_init * steps))
        default_click = max(0, min(steps, default_click))

        unit_str = f" {p.unit}" if p.unit else ""
        desc = p.explanation or (
            f"{p.label}: {p.min_val:g}..{p.max_val:g}{unit_str} (default {p.init:g}{unit_str})."
        )
        if p.values:
            entry = {
                "name": p.label[:12],
                "values": p.values,
                "default": max(0, min(len(p.values) - 1, default_click)),
                "explanation": desc,
            }
        else:
            entry = {
                "name": p.label[:12],
                "max": steps,
                "default": default_click,
                "explanation": desc,
            }
            if p.is_stock_vol:
                entry["curve"] = "stock_vol"
        manifest_params.append(entry)

    if meta.footswitch is not None:
        manifest_params.append(
            {
                "name": "ON/OFF",
                "values": ["LATCH", "UNLATCH"],
                "default": meta.footswitch_default,
                "footswitch_toggle": True,
                "trailing": True,
                "explanation": "LATCH: normal toggle. UNLATCH: momentary while held.",
            }
        )

    m = {
        "name": meta.name,
        "filename": meta.filename,
        "id": meta.effect_id,
        "version": meta.version,
        "display": meta.display,
        "badge": meta.badge,
        "description": meta.description,
        "dspload": meta.dspload,
        "kernel": kernel_filename,
        "kernel_section": "text",
        "allow_stack": True,
        "opt_for_space": None,
        "state_bytes": state_bytes,
        "scaffold": "cleanroom",
        "params": manifest_params,
    }
    if meta.footswitch is not None:
        m["footswitch"] = meta.footswitch
    if meta.author:
        m["author"] = meta.author
    return m
