"""
tools/faust2zd2_pkg/rewriter.py — Bare-metal C674x call-free math header (`_rcpsp`, `_rsqrsp`,
Padé/parabolic approximations) and C expression rewriter (`a / b` -> `sh_fdiv(a, b)`,
32-bit float literal enforcement).
"""

from __future__ import annotations

import re
from .models import strip_c_comments

# ============================================================================
# Bare-Metal C674x Call-Free Math Header (0 CALLs, 0 B14, _rcpsp / _rsqrsp)
# ============================================================================

C674X_FAUST_HEADER = r"""/* ============================================================================
 * Bare-Metal TI TMS320C674x Call-Free Math & Reciprocal Intrinsics for FAUST
 * (Guarantees 0 CALL instructions and 0 B14 global references in cl6x output)
 * ============================================================================ */
#ifdef __TI_COMPILER_VERSION__
#pragma FUNC_ALWAYS_INLINE(sh_rcp)
#pragma FUNC_ALWAYS_INLINE(sh_rcp_pos)
#pragma FUNC_ALWAYS_INLINE(sh_fdiv)
#pragma FUNC_ALWAYS_INLINE(sh_fabsf)
#pragma FUNC_ALWAYS_INLINE(sh_floorf)
#pragma FUNC_ALWAYS_INLINE(sh_fmodf)
#pragma FUNC_ALWAYS_INLINE(sh_sqrtf)
#pragma FUNC_ALWAYS_INLINE(sh_sinf)
#pragma FUNC_ALWAYS_INLINE(sh_cosf)
#pragma FUNC_ALWAYS_INLINE(sh_tanf)
#pragma FUNC_ALWAYS_INLINE(sh_tanhf)
#pragma FUNC_ALWAYS_INLINE(sh_expf)
#pragma FUNC_ALWAYS_INLINE(sh_exp2f)
#pragma FUNC_ALWAYS_INLINE(sh_floor_log2f)
#pragma FUNC_ALWAYS_INLINE(sh_exp2_floor_log2f)
#pragma FUNC_ALWAYS_INLINE(sh_logf)
#pragma FUNC_ALWAYS_INLINE(sh_powf)
#pragma FUNC_ALWAYS_INLINE(sh_minf)
#pragma FUNC_ALWAYS_INLINE(sh_maxf)
#pragma FUNC_ALWAYS_INLINE(sh_clipf)
#pragma FUNC_ALWAYS_INLINE(sh_ceilf)
#pragma FUNC_ALWAYS_INLINE(sh_roundf)
#pragma FUNC_ALWAYS_INLINE(sh_signf)
#pragma FUNC_ALWAYS_INLINE(sh_atanf)
#pragma FUNC_ALWAYS_INLINE(sh_atan2f)
#pragma FUNC_ALWAYS_INLINE(sh_asinf)
#pragma FUNC_ALWAYS_INLINE(sh_acosf)
#pragma FUNC_ALWAYS_INLINE(sh_sinhf)
#pragma FUNC_ALWAYS_INLINE(sh_coshf)
#pragma FUNC_ALWAYS_INLINE(sh_log2f)
#pragma FUNC_ALWAYS_INLINE(sh_log10f)
#pragma FUNC_ALWAYS_INLINE(sh_dread_3)
#pragma FUNC_ALWAYS_INLINE(sh_dread_2)
#pragma FUNC_ALWAYS_INLINE(sh_dread_1)
#pragma FUNC_ALWAYS_INLINE(sh_dread_1_fast)
#pragma FUNC_ALWAYS_INLINE(sh_dread_i)
#endif

#ifndef SH_DELAY_LEN
#define SH_DELAY_LEN  1024u
#define SH_DELAY_MASK 1023u
#endif

/* Fractional 4-point (3rd-order) cubic Lagrange delay reader (`de.fdelay3` in stdfaust.lib/delays.lib) */
static inline float sh_dread_3(const float * restrict buf, uint32_t wpos, float d)
{
    float d_off = d - 0.99999f;
    if (d_off < 0.0f) d_off = 0.0f;
    else if (d_off > (float)(SH_DELAY_LEN - 4u)) d_off = (float)(SH_DELAY_LEN - 4u);
    int n = (int)d_off;
    float fd = 1.0f + d_off - (float)n;
    float fdm1 = fd - 1.0f;
    float fdm2 = fd - 2.0f;
    float fdm3 = fd - 3.0f;
    uint32_t i0 = (wpos - (uint32_t)n) & SH_DELAY_MASK;
    uint32_t i1 = (i0 - 1u) & SH_DELAY_MASK;
    uint32_t i2 = (i0 - 2u) & SH_DELAY_MASK;
    uint32_t i3 = (i0 - 3u) & SH_DELAY_MASK;
    float c0 = -fdm1 * fdm2 * fdm3 * 0.16666667f;
    float c1 =  fd   * fdm2 * fdm3 * 0.5f;
    float c2 = -fd   * fdm1 * fdm3 * 0.5f;
    float c3 =  fd   * fdm1 * fdm2 * 0.16666667f;
    return buf[i0] * c0 + buf[i1] * c1 + buf[i2] * c2 + buf[i3] * c3;
}

/* Fractional 3-point (2nd-order) quadratic Lagrange delay reader (`de.fdelay2` in stdfaust.lib/delays.lib) */
static inline float sh_dread_2(const float * restrict buf, uint32_t wpos, float d)
{
    float d_off = d - 0.49999f;
    if (d_off < 0.0f) d_off = 0.0f;
    else if (d_off > (float)(SH_DELAY_LEN - 4u)) d_off = (float)(SH_DELAY_LEN - 4u);
    int n = (int)d_off;
    float fd = 0.5f + d_off - (float)n;
    float fdm1 = fd - 1.0f;
    float fdm2 = fd - 2.0f;
    uint32_t i0 = (wpos - (uint32_t)n) & SH_DELAY_MASK;
    uint32_t i1 = (i0 - 1u) & SH_DELAY_MASK;
    uint32_t i2 = (i0 - 2u) & SH_DELAY_MASK;
    float c0 =  0.5f * fdm1 * fdm2;
    float c1 = -fd   * fdm2;
    float c2 =  0.5f * fd   * fdm1;
    return buf[i0] * c0 + buf[i1] * c1 + buf[i2] * c2;
}

/* Fractional 2-point (1st-order) linear Lagrange delay reader (`de.fdelay` / `de.fdelay1` in stdfaust.lib/delays.lib) */
static inline float sh_dread_1(const float * restrict buf, uint32_t wpos, float d)
{
    if (d < 0.0f) d = 0.0f;
    else if (d > (float)(SH_DELAY_LEN - 4u)) d = (float)(SH_DELAY_LEN - 4u);
    int id = (int)d;
    float f = d - (float)id;
    uint32_t i0 = (wpos - (uint32_t)id) & SH_DELAY_MASK;
    uint32_t i1 = (i0 - 1u) & SH_DELAY_MASK;
    float s0 = buf[i0], s1 = buf[i1];
    return s0 + f * (s1 - s0);
}

/* Statically-bounded 2-point linear delay reader (0 runtime clamps; memory-safe via SH_DELAY_MASK) */
static inline float sh_dread_1_fast(const float * restrict buf, uint32_t wpos, float d)
{
    int id = (int)d;
    float f = d - (float)id;
    uint32_t i0 = (wpos - (uint32_t)id) & SH_DELAY_MASK;
    uint32_t i1 = (i0 - 1u) & SH_DELAY_MASK;
    float s0 = buf[i0], s1 = buf[i1];
    return s0 + f * (s1 - s0);
}

/* Integer delay tap reader on a power-of-two ring buffer (`de.delay` in stdfaust.lib/delays.lib) */
static inline float sh_dread_i(const float * restrict buf, uint32_t wpos, float d)
{
    int id = (int)(d + 0.5f);
    if (id < 0) id = 0;
    else if (id > (int)SH_DELAY_MASK) id = (int)SH_DELAY_MASK;
    return buf[(wpos - (uint32_t)id) & SH_DELAY_MASK];
}

/* Single-cycle C674x RCPSP instruction + 1 Newton-Raphson step (~16-bit audio precision) */
static inline float sh_rcp(float x)
{
#if defined(__TI_COMPILER_VERSION__) && !defined(__clang__)
    float ax = _fabsf(x);
#else
    float ax = (x < 0.0f) ? -x : x;
#endif
    float s  = (x < 0.0f) ? -1.0f : 1.0f;
    if (ax < 1.0e-18f) ax = 1.0e-18f;
#ifdef __TI_COMPILER_VERSION__
    float r = _rcpsp(ax);
    r = r * (2.0f - ax * r);
    return s * r;
#else
    return (s * 1.0f) / ax;
#endif
}

/* Fast positive-denominator reciprocal (`den > 0`, e.g. `27 + 9*x^2` in tanh) */
static inline float sh_rcp_pos(float den)
{
    if (!(den > 1.0e-18f)) den = 1.0e-18f;
#ifdef __TI_COMPILER_VERSION__
    float r = _rcpsp(den);
    return r * (2.0f - den * r);
#else
    return 1.0f / den;
#endif
}

static inline float sh_fdiv(float num, float den)
{
    return num * sh_rcp(den);
}

static inline float sh_fabsf(float x)
{
#if defined(__TI_COMPILER_VERSION__) && !defined(__clang__)
    return _fabsf(x);
#else
    return (x < 0.0f) ? -x : x;
#endif
}

static inline float sh_minf(float a, float b)
{
    return (a < b) ? a : b;
}

static inline float sh_maxf(float a, float b)
{
    return (a > b) ? a : b;
}

static inline float sh_clipf(float x, float lo, float hi)
{
    float t = (x < lo) ? lo : x;
    return (t > hi) ? hi : t;
}

static inline float sh_floorf(float x)
{
    int i = (int)x;
    float fi = (float)i;
    return (fi > x) ? (fi - 1.0f) : fi;
}

static inline float sh_ceilf(float x)
{
    int i = (int)x;
    float fi = (float)i;
    return (fi < x) ? (fi + 1.0f) : fi;
}

static inline float sh_roundf(float x)
{
    return sh_floorf(x + 0.5f);
}

static inline float sh_signf(float x)
{
    return (x > 0.0f) ? 1.0f : ((x < 0.0f) ? -1.0f : 0.0f);
}

static inline float sh_fmodf(float x, float y)
{
    float q = x * sh_rcp(y);
    int iq = (int)q;
    return x - ((float)iq) * y;
}

static inline float sh_sqrtf(float x)
{
    if (!(x > 1.0e-18f)) return 0.0f;
#ifdef __TI_COMPILER_VERSION__
    float rs = _rsqrsp(x);
    rs = rs * (1.5f - 0.5f * x * rs * rs);
    rs = rs * (1.5f - 0.5f * x * rs * rs);
    return x * rs;
#else
    union { float f; uint32_t u; } v;
    v.f = x;
    v.u = 0x5f3759dfu - (v.u >> 1);
    float rs = v.f;
    rs = rs * (1.5f - 0.5f * x * rs * rs);
    rs = rs * (1.5f - 0.5f * x * rs * rs);
    return x * rs;
#endif
}

/* Parabolic sine approximation on radians x (<0.08% THD, 0 CALLs) */
static inline float sh_sinf(float x)
{
    float p = x * 0.318309886f;
    float half = (p + 1.0f) * 0.5f;
    int ih = (int)half;
    float fh = (float)ih;
    if (fh > half) fh -= 1.0f;
    float u = (half - fh) * 2.0f - 1.0f;
    float au = (u < 0.0f) ? -u : u;
    float y = 4.0f * u * (1.0f - au);
    float ay = (y < 0.0f) ? -y : y;
    return y + 0.225f * (y * ay - y);
}

static inline float sh_cosf(float x)
{
    return sh_sinf(x + 1.570796327f);
}

/* Padé [3/2] tangent approximation on [0, pi/2.2] for audio filter design */
static inline float sh_tanf(float x)
{
    float c = sh_clipf(x, -1.42f, 1.42f);
    float c2 = c * c;
    return c * (15.0f - c2) * sh_rcp(15.0f - 6.0f * c2);
}

/* Padé [3/2] rational tanh saturator (denominator 27 + 9*c2 >= 27.0f > 0) */
static inline float sh_tanhf(float x)
{
    float c = sh_clipf(x, -3.0f, 3.0f);
    float c2 = c * c;
    return c * (27.0f + c2) * sh_rcp_pos(27.0f + 9.0f * c2);
}

/* Fast IEEE-754 + cubic polynomial expf(x) */
static inline float sh_expf(float x)
{
    float c = sh_clipf(x, -20.0f, 20.0f);
    float t = c * 1.44269504f;
    int i = (int)t;
    float fi = (float)i;
    if (fi > t) { i -= 1; fi -= 1.0f; }
    float f = t - fi;
    float p = 1.0f + f * (0.6931472f + f * (0.2402265f + f * 0.0555041f));
    union { uint32_t u; float f; } scale;
    scale.u = (uint32_t)((i + 127) << 23);
    return p * scale.f;
}

/* Fast IEEE-754 + cubic polynomial exp2f(x) = 2^x (avoids sh_logf(2.0f) in pow(2.0f, x)) */
static inline float sh_exp2f(float x)
{
    float t = sh_clipf(x, -28.0f, 28.0f);
    int i = (int)t;
    float fi = (float)i;
    if (fi > t) { i -= 1; fi -= 1.0f; }
    float f = t - fi;
    float p = 1.0f + f * (0.6931472f + f * (0.2402265f + f * 0.0555041f));
    union { uint32_t u; float f; } scale;
    scale.u = (uint32_t)((i + 127) << 23);
    return p * scale.f;
}

/* Exact floor(log2(x)) for x > 0 via IEEE-754 biased exponent extraction (0 transcendentals) */
static inline float sh_floor_log2f(float x)
{
    if (!(x > 1.0e-18f)) return -60.0f;
    union { float f; uint32_t u; } v;
    v.f = x;
    int exp2 = (int)((v.u >> 23) & 0xFFu) - 127;
    return (float)exp2;
}

/* Exact 2^(floor(log2(x))) for x > 0 via IEEE-754 exponent bit-mask (1 bitwise AND, 0 calls) */
static inline float sh_exp2_floor_log2f(float x)
{
    if (!(x > 1.0e-18f)) return 1.0e-18f;
    union { float f; uint32_t u; } v;
    v.f = x;
    v.u &= 0x7F800000u;
    return v.f;
}

/* Fast IEEE-754 exponent extraction + Padé logf(x) */
static inline float sh_logf(float x)
{
    if (!(x > 1.0e-18f)) return -41.4f;
    union { float f; uint32_t u; } v;
    v.f = x;
    int exp2 = (int)((v.u >> 23) & 0xFFu) - 127;
    v.u = (v.u & 0x007FFFFFu) | 0x3F800000u;
    float m = v.f;
    float z = (m - 1.0f) * sh_rcp(m + 1.0f);
    float z2 = z * z;
    float ln_m = 2.0f * z * (1.0f + z2 * (0.3333333f + z2 * 0.2f));
    return ((float)exp2) * 0.69314718f + ln_m;
}

static inline float sh_powf(float base, float exponent)
{
    if (!(base > 1.0e-18f)) return 0.0f;
    return sh_expf(exponent * sh_logf(base));
}

static inline float sh_log2f(float x)
{
    return 1.44269504f * sh_logf(x);
}

static inline float sh_log10f(float x)
{
    return 0.43429448f * sh_logf(x);
}

/* Full-range rational arctangent approximation (< 0.0015 rad error, 0 CALLs) */
static inline float sh_atanf(float x)
{
    float s = (x < 0.0f) ? -1.0f : 1.0f;
    float a = (x < 0.0f) ? -x : x;
    int inv = (a > 1.0f) ? 1 : 0;
    float z = inv ? sh_rcp_pos(a) : a;
    float z2 = z * z;
    float r = z * (0.9998660f + z2 * (-0.3302995f + z2 * (0.1801410f + z2 * (-0.0851330f + 0.0208351f * z2))));
    float ang = inv ? (1.57079633f - r) : r;
    return s * ang;
}

static inline float sh_atan2f(float y, float x)
{
    if (x > 0.0f) return sh_atanf(y * sh_rcp_pos(x));
    if (x < 0.0f) {
        float r = sh_atanf(y * sh_rcp(x));
        return (y >= 0.0f) ? (r + 3.14159265f) : (r - 3.14159265f);
    }
    return (y > 0.0f) ? 1.57079633f : ((y < 0.0f) ? -1.57079633f : 0.0f);
}

static inline float sh_asinf(float x)
{
    float c = sh_clipf(x, -0.999999f, 0.999999f);
    return sh_atanf(c * sh_rcp_pos(sh_sqrtf(1.0f - c * c)));
}

static inline float sh_acosf(float x)
{
    return 1.57079633f - sh_asinf(x);
}

static inline float sh_sinhf(float x)
{
    float e = sh_expf(x);
    return 0.5f * (e - sh_rcp_pos(e));
}

static inline float sh_coshf(float x)
{
    float e = sh_expf(x);
    return 0.5f * (e + sh_rcp_pos(e));
}
"""


MATH_REDIRECTS = {
    "fabsf": "sh_fabsf",
    "fabs": "sh_fabsf",
    "floorf": "sh_floorf",
    "floor": "sh_floorf",
    "ceilf": "sh_ceilf",
    "ceil": "sh_ceilf",
    "roundf": "sh_roundf",
    "round": "sh_roundf",
    "rintf": "sh_roundf",
    "rint": "sh_roundf",
    "fmodf": "sh_fmodf",
    "fmod": "sh_fmodf",
    "sqrtf": "sh_sqrtf",
    "sqrt": "sh_sqrtf",
    "sinf": "sh_sinf",
    "sin": "sh_sinf",
    "cosf": "sh_cosf",
    "cos": "sh_cosf",
    "tanf": "sh_tanf",
    "tan": "sh_tanf",
    "tanhf": "sh_tanhf",
    "tanh": "sh_tanhf",
    "atanf": "sh_atanf",
    "atan": "sh_atanf",
    "atan2f": "sh_atan2f",
    "atan2": "sh_atan2f",
    "asinf": "sh_asinf",
    "asin": "sh_asinf",
    "acosf": "sh_acosf",
    "acos": "sh_acosf",
    "sinhf": "sh_sinhf",
    "sinh": "sh_sinhf",
    "coshf": "sh_coshf",
    "cosh": "sh_coshf",
    "expf": "sh_expf",
    "exp": "sh_expf",
    "logf": "sh_logf",
    "log": "sh_logf",
    "log2f": "sh_log2f",
    "log2": "sh_log2f",
    "log10f": "sh_log10f",
    "log10": "sh_log10f",
    "powf": "sh_powf",
    "pow": "sh_powf",
    "fminf": "sh_minf",
    "fmaxf": "sh_maxf",
    "min": "sh_minf",
    "max": "sh_maxf",
}


def enforce_float32_literals(code: str) -> str:
    """
    Ensures every floating-point literal in C code (e.g. `1.0`, `0.06`, `1e-5`,
    `3.14159265e+00`) carries a trailing `f` suffix so TI's `cl6x` compiler
    never promotes expressions to 64-bit `double` (`MPYSPDP`/`ADDDP`/`DPSP`)
    and never emits `.const` relocation loads for 64-bit float constants.
    """
    code = re.sub(r"\bdouble\b", "float", code)

    float_re = re.compile(
        r"(?<![A-Za-z0-9_\.])"
        r"(?:"
        r"(?:\d+\.\d*|\.\d+)(?:[eE][+\-]?\d+)?"
        r"|"
        r"\d+[eE][+\-]?\d+"
        r")"
        r"(?![fFdD0-9A-Za-z_\.])"
    )
    return float_re.sub(lambda m: f"{m.group(0)}f", code)


def rewrite_c_divisions_and_math(code: str) -> str:
    """
    Rewrites all C floating-point division operators `lhs / rhs` into
    `sh_fdiv(lhs, rhs)`, redirects `<math.h>` function identifiers to
    call-free `sh_*` intrinsics, and suffixes all float literals with `f`.
    """
    for orig, repl in MATH_REDIRECTS.items():
        code = re.sub(rf"\b{orig}\s*\(", f"{repl}(", code)

    code = re.sub(
        r"([A-Za-z_][A-Za-z0-9_.\->\[\]]*)\s*/=\s*([^;]+);",
        r"\1 = sh_fdiv(\1, \2);",
        code,
    )

    def extract_lhs(s: str, slash_idx: int) -> int:
        i = slash_idx - 1
        while i >= 0 and s[i].isspace():
            i -= 1
        if i < 0:
            return slash_idx
        if s[i] in ")]":
            close_ch = s[i]
            open_ch = "(" if close_ch == ")" else "["
            depth = 1
            i -= 1
            while i >= 0 and depth > 0:
                if s[i] == close_ch:
                    depth += 1
                elif s[i] == open_ch:
                    depth -= 1
                i -= 1
            while i >= 0 and (s[i].isalnum() or s[i] in "_->."):
                i -= 1
                if i >= 0 and s[i] == "]" and open_ch == "(":
                    break
            return i + 1
        else:
            while i >= 0 and (s[i].isalnum() or s[i] in "_.+-"):
                if s[i] in "+-" and not (i >= 1 and s[i - 1] in "eE"):
                    break
                i -= 1
            return i + 1

    def extract_rhs(s: str, slash_idx: int) -> int:
        n = len(s)
        i = slash_idx + 1
        while i < n and s[i].isspace():
            i += 1
        while i < n and s[i] in "+-":
            i += 1
            while i < n and s[i].isspace():
                i += 1
        if i < n and s[i] == "(":
            depth = 1
            i += 1
            while i < n and depth > 0:
                if s[i] == "(":
                    depth += 1
                elif s[i] == ")":
                    depth -= 1
                i += 1
            after = i
            while after < n and s[after].isspace():
                after += 1
            if after < n and (s[after].isalnum() or s[after] in "_("):
                inner = s[slash_idx + 1 : i].strip()[1:-1].strip()
                if inner in ("float", "int", "uint32_t", "double", "FAUSTFLOAT"):
                    return extract_rhs(s, i - 1)
            return i
        else:
            while i < n and (s[i].isalnum() or s[i] in "_."):
                if s[i] in "eE" and i + 1 < n and s[i + 1] in "+-":
                    i += 2
                    continue
                i += 1
            while i < n and s[i] in "([":
                open_ch = s[i]
                close_ch = ")" if open_ch == "(" else "]"
                depth = 1
                i += 1
                while i < n and depth > 0:
                    if s[i] == open_ch:
                        depth += 1
                    elif s[i] == close_ch:
                        depth -= 1
                    i += 1
            return i

    guard = 0
    while "/" in code and guard < 500:
        guard += 1
        m = re.search(r"/", code)
        if not m:
            break
        idx = m.start()
        l_start = extract_lhs(code, idx)
        r_end = extract_rhs(code, idx)
        lhs = code[l_start:idx].strip()
        rhs = code[idx + 1 : r_end].strip()
        if not lhs or not rhs:
            break
        code = code[:l_start] + f"sh_fdiv({lhs}, {rhs})" + code[r_end:]

    code = enforce_float32_literals(code)

    # IEEE-754 exponent extraction peephole optimizations:
    # 1. floor(log(x) * 1.44269504f) or floor(log2(x)) -> sh_floor_log2f(x)
    code = re.sub(
        r"\bsh_floorf\s*\(\s*sh_logf\s*\(\s*(\([^()]+\)|[A-Za-z0-9_]+)\s*\)\s*\*\s*1\.442695\d*f\s*\)",
        r"sh_floor_log2f(\1)",
        code,
    )
    code = re.sub(
        r"\bsh_floorf\s*\(\s*1\.442695\d*f\s*\*\s*sh_logf\s*\(\s*(\([^()]+\)|[A-Za-z0-9_]+)\s*\)\s*\)",
        r"sh_floor_log2f(\1)",
        code,
    )
    code = re.sub(
        r"\bsh_floorf\s*\(\s*sh_log2f\s*\(\s*(\([^()]+\)|[A-Za-z0-9_]+)\s*\)\s*\)",
        r"sh_floor_log2f(\1)",
        code,
    )
    # 2. pow(2.0f, sh_floor_log2f(x) +/- k) -> (2^(+/-k) * sh_exp2_floor_log2f(x))
    def _replace_pow2_floor_log2(m: re.Match) -> str:
        arg = m.group(1)
        op = m.group(2)
        val_str = m.group(3)
        if op and val_str:
            shift = float(val_str) if op == "+" else -float(val_str)
            scale = 2.0 ** shift
            return f"({scale:.8g}f * sh_exp2_floor_log2f({arg}))"
        return f"sh_exp2_floor_log2f({arg})"

    code = re.sub(
        r"\bsh_powf\s*\(\s*2\.0+f\s*,\s*sh_floor_log2f\s*\(\s*(\([^()]+\)|[A-Za-z0-9_]+)\s*\)(?:\s*([+\-])\s*(\d+(?:\.\d*)?)f)?\s*\)",
        _replace_pow2_floor_log2,
        code,
    )
    # 3. Remaining pow(2.0f, y) -> sh_exp2f(y) (avoids runtime sh_logf(2.0f))
    code = re.sub(
        r"\bsh_powf\s*\(\s*2\.0+f\s*,",
        "sh_exp2f(",
        code,
    )
    return code
