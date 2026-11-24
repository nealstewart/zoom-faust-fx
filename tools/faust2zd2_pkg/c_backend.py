"""
tools/faust2zd2_pkg/c_backend.py — Mode 1: Parse external `faust -lang c` output
into a bare-metal C674x Zoom MS Plus kernel.
"""

from __future__ import annotations

import re
from typing import List, Tuple

from .emitter import _emit_c674x_kernel_from_parts
from .models import FaustMeta, FaustParam, strip_c_comments


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

    struct_m = re.search(r"typedef\s+struct\s*\{([^}]+)\}\s*(\w+)\s*;", src, re.S)
    if not struct_m:
        raise ValueError("Could not find FAUST DSP struct (`typedef struct { ... } mydsp;`) in C file.")
    struct_body = struct_m.group(1)

    state_vars: List[Tuple[str, str, int]] = []
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
        r'\s*"([^"]+)"\s*,\s*&dsp->(\w+)\s*,\s*([^,]+)\s*,\s*([^,]+)\s*,\s*([^,]+)\s*,\s*([^\)]+)\)'
    )
    c_params: List[FaustParam] = []
    for idx, m in enumerate(ui_re.finditer(src)):
        label = m.group(1).strip()
        vname = m.group(2).strip()
        init_v = float(m.group(3).replace("f", "").strip())
        min_v = float(m.group(4).replace("f", "").strip())
        max_v = float(m.group(5).replace("f", "").strip())
        step_v = float(m.group(6).replace("f", "").strip())

        matched = next((p for p in dsp_params if p.label == label[:12]), None)
        c_params.append(
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
                order=matched.order if matched else idx,
            )
        )
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

    loop_m = re.search(
        r"for\s*\(\s*(?:int\s+)?i0\s*=\s*0\s*;\s*i0\s*<\s*count\s*;\s*i0\s*=\s*i0\s*\+\s*1\s*\)\s*\{(.*)\}",
        comp_body,
        re.S,
    )
    if not loop_m:
        raise ValueError("Could not locate `for (int i0 = 0; i0 < count; i0 = i0 + 1)` inside computemydsp.")

    pre_loop = comp_body[: loop_m.start()].strip()
    loop_inner = loop_m.group(1).strip()
    pre_loop = re.sub(r"FAUSTFLOAT\*\s+(?:input|output)\d+\s*=\s*(?:inputs|outputs)\[\d+\]\s*;", "", pre_loop)

    return _emit_c674x_kernel_from_parts(meta, params, state_vars, [], pre_loop, loop_inner)
