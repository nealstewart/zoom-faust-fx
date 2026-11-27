"""
tools/faust2zd2_pkg/models.py — Data structures and metadata/UI parser for FAUST `.dsp` files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ============================================================================
# Zoom MS Plus / TMS320C6745 Static Memory Budget Constants
# ============================================================================

# Persistent per-instance state budget in `instance[2]` (bytes 0..187; byte 188..191 is SH_STATE_GUARD)
MAX_STATE_BYTES = 188
MAX_STATE_WORDS = MAX_STATE_BYTES // 4  # 47 words total (46 DSP state words + 1 init_magic word)

# External SDRAM delay buffer safety cap in `instance[3]` (3.0 s @ 44.1 kHz = 132,300 floats,
# leaving a 1.0 s / 176,400 B safety margin inside the 705,600 B per-slot firmware grant)
MAX_SAFE_SDRAM_BYTES = 529200

# Maximum distinct parallel SDRAM delay lines before 2-way L1D / 4-way L2 cache-set thrashing
MAX_DELAY_LINES = 32

# Maximum per-sample local temporary variables inside the 16-frame audio loop
# (caps C674x B15 stack-frame spills and .text code bloat across inlined function invocations)
MAX_SAMPLE_TEMPS = 512
MAX_SAMPLE_TEMP_BYTES = MAX_SAMPLE_TEMPS * 4  # 2,048 B

# Maximum compile-time count in pattern macros `seq(i, N, ...)`, `par(i, N, ...)`, `sum`, `prod`
MAX_PATTERN_COUNT = 256


class FaustMemoryLimitError(ValueError):
    """
    Raised by static memory analysis when a FAUST `.dsp` script or generated C
    kernel attempts to allocate excessive memory across function invocations
    (persistent `instance[2]` state, `instance[3]` SDRAM delay buffers, delay tap
    bounds exceeding buffer capacity, parallel delay-line cache sets, or
    per-sample stack/register temporaries).
    """


@dataclass
class FaustMemoryReport:
    """Static memory analysis summary across all function invocations in a FAUST script."""

    state_words: int = 0
    state_bytes: int = 16
    delay_line_count: int = 0
    delay_line_len: int = 0
    logical_sdram_bytes: int = 0
    physical_sdram_bytes: int = 0
    sample_temp_count: int = 0
    sample_temp_bytes: int = 0
    function_invocations: Dict[str, int] = field(default_factory=dict)
    function_state_words: Dict[str, int] = field(default_factory=dict)
    function_sdram_lines: Dict[str, int] = field(default_factory=dict)
    function_sdram_bytes: Dict[str, int] = field(default_factory=dict)
    function_temp_vars: Dict[str, int] = field(default_factory=dict)

    def format_breakdown(self) -> str:
        lines = [
            f"  state_bytes          : {self.state_bytes} B ({self.state_words} words, limit {MAX_STATE_BYTES} B / {MAX_STATE_WORDS} words)",
            f"  physical_sdram_bytes : {self.physical_sdram_bytes} B ({self.delay_line_count} lines x {self.delay_line_len} floats, limit {MAX_SAFE_SDRAM_BYTES} B)",
            f"  logical_sdram_bytes  : {self.logical_sdram_bytes} B (sum of requested delay capacities, limit {MAX_SAFE_SDRAM_BYTES} B)",
            f"  sample_temp_bytes    : {self.sample_temp_bytes} B ({self.sample_temp_count} locals, limit {MAX_SAMPLE_TEMP_BYTES} B / {MAX_SAMPLE_TEMPS} locals)",
        ]
        if self.function_invocations:
            lines.append("  per-function invocation memory breakdown:")
            for fn, count in sorted(
                self.function_invocations.items(),
                key=lambda kv: (
                    self.function_sdram_bytes.get(kv[0], 0),
                    self.function_state_words.get(kv[0], 0),
                    self.function_temp_vars.get(kv[0], 0),
                    kv[1],
                ),
                reverse=True,
            ):
                st_w = self.function_state_words.get(fn, 0)
                sd_l = self.function_sdram_lines.get(fn, 0)
                sd_b = self.function_sdram_bytes.get(fn, 0)
                tmp_v = self.function_temp_vars.get(fn, 0)
                lines.append(
                    f"    - {fn}: {count}x invoked | state={st_w * 4} B ({st_w}w) | "
                    f"sdram={sd_b} B ({sd_l} lines) | sample_temps={tmp_v * 4} B ({tmp_v} vars)"
                )
        return "\n".join(lines)


DEFAULT_LFO_SYNC_NOTES: List[str] = [
    "1/32", "1/16", "1/8T", "1/16.", "1/8",
    "1/4T", "1/8.", "1/4", "1/4.", "1/2",
]


@dataclass
class LfoSpec:
    """One stock-style Q31 LFO output buffer generated in Loop 1 (`ctx[9..10]`)."""

    lfo_id: int
    phase_group_id: int
    shape: str  # sawpos, saw, trianglepos, triangle, squarewavepos, squarewave, sin, cos, sh
    buf_name: str
    rate_param_idx: Optional[int] = None
    rate_expr: Optional[str] = None
    phase_offset_u32: int = 0
    phase_expr: Optional[str] = None
    phase_is_radians: bool = False


@dataclass
class FaustParam:
    label: str
    var_name: str
    init: float
    min_val: float
    max_val: float
    step: float
    curve: Optional[str] = None
    explanation: Optional[str] = None
    unit: str = ""
    values: Optional[List[str]] = None
    time_sync: Optional[dict] = None
    lfo_sync: Optional[dict] = None
    is_q31_lfo_rate: bool = False
    scale: str = "linear"
    order: int = 999

    @property
    def c_macro_name(self) -> str:
        s = re.sub(r"[^A-Za-z0-9]+", "_", self.label.strip()).strip("_").upper()
        return f"SH_PARAM_{s}"

    @property
    def c_edit_macro_name(self) -> str:
        s = re.sub(r"[^A-Za-z0-9]", "_", self.label[:12]).upper()
        return f"SH_EDIT_{s}"

    @property
    def is_stock_vol(self) -> bool:
        if self.curve == "stock_vol":
            return True
        if self.curve == "linear":
            return False
        return (
            self.label.strip().lower() in ("level", "vol", "volume", "out", "output")
            and self.min_val == 0.0
            and self.max_val == 100.0
            and self.init == 80.0
        )


@dataclass
class FaustMeta:
    name: str = "FaustFx"
    filename: str = "XXFAUST"
    effect_id: str = "07001160"
    version: str = "1.00"
    display: str = "FAUST FX"
    badge: str = "FS"
    description: str = "Compiled from FAUST DSP to bare-metal Zoom MS Plus C674x."
    dspload: int = 12
    author: Optional[str] = None
    footswitch: Optional[dict] = None
    footswitch_default: int = 0
    reads_bpm: bool = False


def strip_c_comments(code: str) -> str:
    """Remove /* ... */ and // ... comments from C/FAUST source."""
    code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    code = re.sub(r"//[^\n]*", "", code)
    return code


def parse_faust_dsp_metadata(dsp_text: str) -> Tuple[FaustMeta, List[FaustParam]]:
    """Extract `declare ...` headers and UI control definitions from a `.dsp` file."""
    clean = strip_c_comments(dsp_text)
    meta = FaustMeta()

    for m in re.finditer(r'declare\s+([A-Za-z0-9_]+)\s+"([^"]*)"\s*;', clean):
        key, val = m.group(1).lower(), m.group(2)
        if key in ("name", "zd2_name"):
            meta.name = val[:10]
        elif key == "zd2_filename":
            meta.filename = val[:8].upper()
        elif key == "zd2_id":
            meta.effect_id = val.lower().replace("0x", "")
        elif key in ("version", "zd2_version"):
            meta.version = val[:4]
        elif key == "zd2_display":
            meta.display = val[:10]
        elif key == "zd2_badge":
            meta.badge = val[:2].upper()
        elif key in ("description", "zd2_description"):
            meta.description = val
        elif key == "zd2_dspload":
            meta.dspload = int(float(val))
        elif key in ("author", "zd2_author"):
            meta.author = val
        elif key == "zd2_footswitch":
            vlow = val.strip().lower()
            meta.footswitch = {} if vlow in ("true", "1", "{}", "unlatch", "latch") else {"sub_word": 256, "slot": 0}
            meta.footswitch_default = 1 if vlow == "unlatch" else 0

    params: List[FaustParam] = []
    ui_re = re.compile(
        r'\b(?:(?P<sl>hslider|vslider|nentry)\s*\(\s*"(?P<slabel>[^"]+)"\s*,'
        r'\s*(?P<init>[^,]+)\s*,\s*(?P<min>[^,]+)\s*,\s*(?P<max>[^,]+)\s*,\s*(?P<step>[^\)]+)\)'
        r'|(?P<cb>checkbox|button)\s*\(\s*"(?P<clabel>[^"]+)"\s*\))'
    )
    seen_labels: Dict[str, FaustParam] = {}
    for idx, m in enumerate(ui_re.finditer(clean)):
        if m.group("sl"):
            raw_label = m.group("slabel")
            init_v = float(eval(m.group("init"), {"__builtins__": {}}))
            min_v = float(eval(m.group("min"), {"__builtins__": {}}))
            max_v = float(eval(m.group("max"), {"__builtins__": {}}))
            step_v = float(eval(m.group("step"), {"__builtins__": {}}))
            default_values = None
        else:
            raw_label = m.group("clabel")
            init_v, min_v, max_v, step_v = 0.0, 0.0, 1.0, 1.0
            default_values = ["OFF", "ON"] if m.group("cb") == "checkbox" else ["OFF", "TRIG"]

        order = idx
        curve = None
        explanation = None
        unit = ""
        values = default_values
        time_sync = None
        lfo_sync = None
        scale = "linear"
        for tag_m in re.finditer(r"\[([^\]]+)\]", raw_label):
            t = tag_m.group(1).strip()
            if t.isdigit():
                order = int(t)
            elif ":" in t:
                tk, tv = [x.strip() for x in t.split(":", 1)]
                tkl = tk.lower()
                if tkl == "curve":
                    curve = tv
                elif tkl == "scale":
                    scale = tv.lower()
                elif tkl in ("tooltip", "explanation", "desc"):
                    explanation = tv
                elif tkl == "unit":
                    unit = tv
                elif tkl == "values":
                    values = [x.strip()[:7] for x in tv.split("|") if x.strip()]
                elif tkl == "time_sync":
                    parts = tv.split("..")
                    if len(parts) == 2:
                        time_sync = {
                            "min_ms": int(float(parts[0])),
                            "max_ms": int(float(parts[1])),
                            "notes": ["1/16", "1/8T", "1/8", "1/4T", "1/8.", "1/4", "1/4."],
                        }
                elif tkl == "lfo_sync" or (tkl == "zoom" and tv.lower() == "lfo_sync"):
                    if tkl == "zoom":
                        min_dhz = max(0, int(round(min_v * 10.0)))
                        max_dhz = min(999, int(round(max_v * 10.0)))
                        step_dhz = max(1, int(round(step_v * 10.0))) if step_v > 0 else 1
                    else:
                        step_part = None
                        rng_part = tv
                        if ":" in tv:
                            rng_part, step_part = tv.split(":", 1)
                        parts = rng_part.split("..")
                        if len(parts) == 2:
                            min_dhz = max(0, int(round(float(parts[0]) * 10.0)))
                            max_dhz = min(999, int(round(float(parts[1]) * 10.0)))
                        else:
                            min_dhz = max(0, int(round(min_v * 10.0)))
                            max_dhz = min(999, int(round(max_v * 10.0)))
                        step_dhz = (
                            max(1, int(round(float(step_part) * 10.0)))
                            if step_part is not None
                            else max(1, int(round(step_v * 10.0))) if step_v > 0 else 1
                        )
                    if max_dhz >= min_dhz and (max_dhz - min_dhz) % step_dhz != 0:
                        max_dhz = min_dhz + ((max_dhz - min_dhz) // step_dhz) * step_dhz
                    lfo_sync = {
                        "min_dhz": min_dhz,
                        "max_dhz": max_dhz,
                        "step_dhz": step_dhz,
                        "notes": list(DEFAULT_LFO_SYNC_NOTES),
                    }
            elif t.lower() == "lfo_sync":
                min_dhz = max(0, int(round(min_v * 10.0)))
                max_dhz = min(999, int(round(max_v * 10.0)))
                step_dhz = max(1, int(round(step_v * 10.0))) if step_v > 0 else 1
                if max_dhz >= min_dhz and (max_dhz - min_dhz) % step_dhz != 0:
                    max_dhz = min_dhz + ((max_dhz - min_dhz) // step_dhz) * step_dhz
                lfo_sync = {
                    "min_dhz": min_dhz,
                    "max_dhz": max_dhz,
                    "step_dhz": step_dhz,
                    "notes": list(DEFAULT_LFO_SYNC_NOTES),
                }

        clean_label = re.sub(r"\[[^\]]*\]", "", raw_label).strip()
        if clean_label in seen_labels:
            continue
        var_name = f"fHslider{len(seen_labels)}"
        p = FaustParam(
            label=clean_label[:12],
            var_name=var_name,
            init=init_v,
            min_val=min_v,
            max_val=max_v,
            step=step_v,
            curve=curve,
            explanation=explanation,
            unit=unit,
            values=values,
            time_sync=time_sync,
            lfo_sync=lfo_sync,
            is_q31_lfo_rate=bool(lfo_sync),
            scale=scale,
            order=order,
        )
        seen_labels[clean_label] = p
        params.append(p)

    params.sort(key=lambda p: (p.order, p.var_name))
    return meta, params
