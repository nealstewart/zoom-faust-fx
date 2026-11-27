"""
tools/faust2zd2_pkg/c_backend.py — Mode 1: Parse external `faust -lang c` output
into a bare-metal C674x Zoom MS Plus kernel.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from .emitter import _emit_c674x_kernel_from_parts
from .models import FaustMeta, FaustParam, strip_c_comments


def _parse_c_float(expr: str) -> float:
    cleaned = re.sub(r"^\(\s*(?:FAUSTFLOAT|float|double)\s*\)\s*", "", expr.strip())
    cleaned = cleaned.rstrip("fF").strip()
    return float(cleaned)


def convert_faust_c_to_zd2_kernel(
    faust_c_source: str,
    meta: FaustMeta,
    dsp_params: List[FaustParam],
) -> Tuple[str, int, List[FaustParam]]:
    """
    Takes the C source produced by `faust -lang c` and transforms it into a
    self-contained, 0-CALL, 0-B14, NaN-scrubbed Zoom MS Plus C674x kernel.
    """
    src = strip_c_comments(faust_c_source)

    struct_m = re.search(r"typedef\s+struct\s*\{([^}]+)\}\s*(mydsp)\s*;", src, re.S)
    if not struct_m:
        struct_m = re.search(r"typedef\s+struct\s*\{([^}]+)\}\s*(\w+)\s*;", src, re.S)
    if not struct_m:
        raise ValueError("Could not find FAUST DSP struct (`typedef struct { ... } mydsp;`) in C file.")
    struct_body = struct_m.group(1)

    state_vars: List[Tuple[str, str, int]] = []
    sdram_arrays: List[Tuple[str, str, int]] = []
    slider_vars: List[str] = []

    for line in struct_body.splitlines():
        line = line.strip().rstrip(";")
        if not line:
            continue
        m_arr = re.match(r"(FAUSTFLOAT|float|int|uint32_t)\s+(\w+)\s*\[\s*(\d+)\s*\]", line)
        m_sca = re.match(r"(FAUSTFLOAT|float|int|uint32_t)\s+(\w+)$", line)
        if m_arr:
            ctype, vname, alen = m_arr.group(1), m_arr.group(2), int(m_arr.group(3))
            ctype = "float" if ctype == "FAUSTFLOAT" else ctype
            if alen >= 16:
                sdram_arrays.append((ctype, vname, alen))
            else:
                state_vars.append((ctype, vname, alen))
        elif m_sca:
            ctype, vname = m_sca.group(1), m_sca.group(2)
            ctype = "float" if ctype == "FAUSTFLOAT" else ctype
            if vname == "fSampleRate":
                continue
            if re.match(r"f(Hslider|Vslider|Entry|Button|Checkbox)\d+", vname):
                slider_vars.append(vname)
            else:
                state_vars.append((ctype, vname, 1))

    ui_re = re.compile(
        r'add(?:HorizontalSlider|VerticalSlider|NumEntry)\s*\(\s*ui_interface->uiInterface\s*,'
        r'\s*"([^"]+)"\s*,\s*&dsp->(\w+)\s*,\s*([^,]+)\s*,\s*([^,]+)\s*,\s*([^,]+)\s*,\s*((?:\([^)]*\)\s*)?[^,;\)]+)\)'
    )
    btn_re = re.compile(
        r'add(?:Button|CheckButton)\s*\(\s*ui_interface->uiInterface\s*,'
        r'\s*"([^"]+)"\s*,\s*&dsp->(\w+)\s*\)'
    )
    c_params_with_pos: list[tuple[int, FaustParam]] = []
    bpm_var_name: str | None = None
    for m in ui_re.finditer(src):
        label = m.group(1).strip()
        vname = m.group(2).strip()
        if label == "__zm_bpm__":
            bpm_var_name = vname
            meta.reads_bpm = True
            continue
        init_v = _parse_c_float(m.group(3))
        min_v = _parse_c_float(m.group(4))
        max_v = _parse_c_float(m.group(5))
        step_v = _parse_c_float(m.group(6))
        matched = next((p for p in dsp_params if p.label == label[:12]), None)
        c_params_with_pos.append((
            m.start(),
            FaustParam(
                label=label[:12],
                var_name=vname,
                init=init_v,
                min_val=min_v,
                max_val=max_v,
                step=step_v,
                curve=matched.curve if matched else None,
                explanation=matched.explanation if matched else None,
                unit=matched.unit if matched else "",
                values=matched.values if matched else None,
                time_sync=matched.time_sync if matched else None,
                order=matched.order if matched else len(c_params_with_pos),
            ),
        ))
    for m in btn_re.finditer(src):
        label = m.group(1).strip()
        vname = m.group(2).strip()
        matched = next((p for p in dsp_params if p.label == label[:12]), None)
        c_params_with_pos.append((
            m.start(),
            FaustParam(
                label=label[:12],
                var_name=vname,
                init=0.0,
                min_val=0.0,
                max_val=1.0,
                step=1.0,
                curve=matched.curve if matched else None,
                explanation=matched.explanation if matched else None,
                unit=matched.unit if matched else "",
                values=matched.values if matched else None,
                time_sync=matched.time_sync if matched else None,
                order=matched.order if matched else len(c_params_with_pos),
            ),
        ))
    c_params = [p for _, p in sorted(c_params_with_pos, key=lambda item: item[0])]
    if c_params:
        c_params.sort(key=lambda p: p.order)
        params = c_params
    else:
        params = dsp_params

    comp_hdr = re.search(r"void\s+compute\w+\s*\([^)]*\)\s*\{", src)
    if not comp_hdr:
        raise ValueError("Could not find `computemydsp` function in FAUST C output.")
    b_open = comp_hdr.end() - 1
    depth = 1
    idx_ch = b_open + 1
    while idx_ch < len(src) and depth > 0:
        if src[idx_ch] == "{":
            depth += 1
        elif src[idx_ch] == "}":
            depth -= 1
        idx_ch += 1
    comp_body = src[b_open + 1 : idx_ch - 1]

    loop_hdr = re.search(
        r"for\s*\(\s*(?:int\s+)?i0\s*=\s*0\s*;\s*i0\s*<\s*count\s*;\s*"
        r"(?:i0\s*=\s*i0\s*\+\s*1|i0\s*=\s*faust_wrap_add\s*\(\s*i0\s*,\s*1\s*\)|i0\+\+|\+\+i0)\s*\)\s*\{",
        comp_body,
    )
    if not loop_hdr:
        raise ValueError("Could not locate `for (int i0 = 0; i0 < count; ...)` inside computemydsp.")

    l_open = loop_hdr.end() - 1
    ldepth = 1
    lidx = l_open + 1
    while lidx < len(comp_body) and ldepth > 0:
        if comp_body[lidx] == "{":
            ldepth += 1
        elif comp_body[lidx] == "}":
            ldepth -= 1
        lidx += 1
    loop_inner = comp_body[l_open + 1 : lidx - 1].strip()

    pre_loop = comp_body[: loop_hdr.start()].strip()
    pre_loop = re.sub(r"FAUSTFLOAT\*\s+(?:input|output)\d+\s*=\s*(?:inputs|outputs)\[\d+\]\s*;", "", pre_loop)
    pre_loop = re.sub(
        r"(?:/\*\s*C99 loop\s*\*/\s*)?\{\s*int\s+i0\s*;\s*$",
        "",
        pre_loop,
        flags=re.S,
    ).strip()

    # Faust 2.88+ wraps integer index arithmetic in (possibly nested) faust_wrap_add/sub/mul
    def _unwrap_faust_int_ops(text: str) -> str:
        ops = {"faust_wrap_add": "+", "faust_wrap_sub": "-", "faust_wrap_mul": "*"}
        pat = re.compile(r"\b(faust_wrap_(?:add|sub|mul))\s*\(")
        while True:
            m = pat.search(text)
            if not m:
                return text
            op = ops[m.group(1)]
            i = m.end()
            depth = 1
            comma_pos = -1
            while i < len(text) and depth > 0:
                ch = text[i]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == "," and depth == 1 and comma_pos == -1:
                    comma_pos = i
                i += 1
            if depth != 0 or comma_pos == -1:
                return text
            lhs = text[m.end() : comma_pos].strip()
            rhs = text[comma_pos + 1 : i - 1].strip()
            text = text[: m.start()] + f"(({lhs}) {op} ({rhs}))" + text[i:]

    pre_loop = _unwrap_faust_int_ops(pre_loop)
    loop_inner = _unwrap_faust_int_ops(loop_inner)

    # Unroll Faust 4+-tap shift loops: for (j0 = N; j0 > 0; j0 = j0 - 1) { dsp->X[j0] = dsp->X[j0 - 1]; }
    shift_re = re.compile(
        r"for\s*\(\s*(?:int\s+)?(\w+)\s*=\s*(\d+)\s*;\s*\1\s*>\s*0\s*;\s*\1\s*=\s*"
        r"(?:\1\s*-\s*1|faust_wrap_sub\s*\(\s*\1\s*,\s*1\s*\)|\(\(\1\)\s*-\s*\(1\)\))\s*\)\s*\{\s*"
        r"dsp->(\w+)\s*\[\s*\1\s*\]\s*=\s*dsp->\3\s*\[\s*(?:faust_wrap_sub\s*\(\s*\1\s*,\s*1\s*\)|\(\(\1\)\s*-\s*\(1\)\)|\1\s*-\s*1)\s*\]\s*;\s*\}",
        re.S,
    )

    def _unroll_shift(m: re.Match[str]) -> str:
        n = int(m.group(2))
        arr = m.group(3)
        return " ".join(f"dsp->{arr}[{k}] = dsp->{arr}[{k - 1}];" for k in range(n, 0, -1))

    loop_inner = shift_re.sub(_unroll_shift, loop_inner)
    # Strip any leftover empty `{ int j0; ... }` wrapper if Faust emitted C99 loop block
    loop_inner = re.sub(r"\{\s*int\s+\w+\s*;\s*(dsp->\w+\[\d+\]\s*=\s*dsp->\w+\[\d+\]\s*;\s*)+\}", lambda m: " ".join(re.findall(r"dsp->\w+\[\d+\]\s*=\s*dsp->\w+\[\d+\];", m.group(0))), loop_inner)

    # Extract sample-rate constant initialization from instanceConstantsmydsp(...)
    init_const_lines: List[str] = []
    const_hdr = re.search(r"void\s+instanceConstants\w*\s*\([^)]*\)\s*\{", src)
    if const_hdr:
        c_open = const_hdr.end() - 1
        cdepth = 1
        cidx = c_open + 1
        while cidx < len(src) and cdepth > 0:
            if src[cidx] == "{":
                cdepth += 1
            elif src[cidx] == "}":
                cdepth -= 1
            cidx += 1
        const_body = src[c_open + 1 : cidx - 1]
        for raw_line in const_body.splitlines():
            cline = raw_line.strip()
            if not cline:
                continue
            if re.match(r"dsp->fSampleRate\s*=", cline):
                continue
            # Zero-initialized rwtable SDRAM buffers are already zeroed by the pedal;
            # skip Faust's heap-allocated mydspSIG* zero-fill helper calls.
            if re.search(r"\b(?:mydspSIG\d+|(?:new|delete|instanceInit|fill)mydspSIG\d+)\b", cline):
                continue
            cline = re.sub(r"\bdsp->fSampleRate\b|\bsample_rate\b", "SH_SAMPLE_RATE", cline)
            init_const_lines.append(cline)

    return _emit_c674x_kernel_from_parts(
        meta,
        params,
        state_vars,
        [],
        pre_loop,
        loop_inner,
        sdram_arrays=sdram_arrays,
        init_const_lines=init_const_lines,
        bpm_var_name=bpm_var_name,
    )
