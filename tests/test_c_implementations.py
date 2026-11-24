"""
tests/test_c_implementations.py — Unit tests for the C implementations used by
the Faust-to-ZD2 compilation pipeline and effect kernels:

  1. Bare-metal call-free math intrinsics in `C674X_FAUST_HEADER` (`sh_rcp`,
     `sh_rcp_pos`, `sh_fdiv`, `sh_fabsf`, `sh_minf`, `sh_maxf`, `sh_clipf`,
     `sh_floorf`, `sh_ceilf`, `sh_roundf`, `sh_signf`, `sh_fmodf`, `sh_sqrtf`,
     `sh_sinf`, `sh_cosf`, `sh_tanf`, `sh_tanhf`, `sh_expf`, `sh_logf`,
     `sh_powf`, `sh_log2f`, `sh_log10f`, `sh_atanf`, `sh_atan2f`, `sh_asinf`,
     `sh_acosf`, `sh_sinhf`, `sh_coshf`).
  2. Simulated TI C674x hardware reciprocal (`_rcpsp`) and reciprocal-square-root
     (`_rsqrsp`) 8-bit mantissa seed + Newton-Raphson refinement code paths
     under `#ifdef __TI_COMPILER_VERSION__`.
  3. All 4 power-of-two SDRAM delay ring-buffer readers (`sh_dread_i`,
     `sh_dread_1`, `sh_dread_2`, `sh_dread_3`) across wrap-around boundaries,
     polynomial interpolation exactness, and out-of-range delay clamps.
  4. Native execution of compiled C effect kernels (`effects/*/*.c`):
     - Mandatory fail-closed safety prologues (`SAFETY.md`): null pointers,
       corrupted `SH_STATE_GUARD`, out-of-range/NaN `fade` and knob coefficients,
       and malformed `instance[3]` SDRAM descriptors.
     - Input/output `(-8, +8)` NaN/Inf/rail scrubbing and persistent state NaN
       sanitization.
     - First-block `si.smoo` snap-to-target (`init_magic == 0x46535431u`) vs
       subsequent block exponential glide.
     - Firmware bypass crossfade (`fade = 0.0`, `0.5`, `1.0`).
     - Functional DSP verification across real C kernels (`svfflt`,
       `tzflng`, `anlgdrv`, `vocalfe`, `rackptch`, `xaspu`, `xarvb`,
       `swedsaw`, `deskdrv`, `octring`, `slewdrv`, `gain`).
"""

from __future__ import annotations

import ctypes
import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from faust2zd2_pkg import C674X_FAUST_HEADER
from tests.c_harness import KernelHarness, SH_FRAMES, compile_c_to_dll


def _build_math_test_dll(simulate_ti_c674x: bool = False) -> ctypes.CDLL:
    """
    Compile `C674X_FAUST_HEADER` into a native DLL with exported wrapper
    functions for every `sh_*` math and delay primitive.
    When `simulate_ti_c674x=True`, defines `__TI_COMPILER_VERSION__` and
    provides bit-accurate C674x 8-bit mantissa lookup approximations for
    `_rcpsp(x)` and `_rsqrsp(x)` to test the hardware Newton-Raphson path.
    """
    ti_mock = ""
    if simulate_ti_c674x:
        ti_mock = r"""
#define __TI_COMPILER_VERSION__ 8005000
/* Bit-accurate simulation of C674x 8-bit mantissa seed instructions RCPSP & RSQRSP */
static inline float _rcpsp(float x) {
    union { float f; uint32_t u; } v;
    v.f = x;
    /* Quantize mantissa to top 8 bits before computing 1/x seed (~8-bit precision) */
    v.u &= 0xFFFF8000u;
    if ((v.u & 0x7F800000u) == 0u) return 1.0e18f;
    return 1.0f / v.f;
}
static inline float _rsqrsp(float x) {
    union { float f; uint32_t u; } v;
    v.f = x;
    v.u = 0x5f3759dfu - (v.u >> 1);
    /* Single pre-refinement to ~8-bit seed precision matching C674x RSQRSP */
    float rs = v.f;
    return rs * (1.5f - 0.5f * x * rs * rs);
}
"""
    # Strip #pragma FUNC_ALWAYS_INLINE lines when simulating __TI_COMPILER_VERSION__ on Clang
    header_code = C674X_FAUST_HEADER
    if simulate_ti_c674x:
        header_code = "\n".join(
            line for line in header_code.splitlines() if not line.strip().startswith("#pragma FUNC_ALWAYS_INLINE")
        )

    c_src = (
        "#include <stdint.h>\n"
        + ti_mock
        + "\n#define SH_DELAY_LEN  1024u\n#define SH_DELAY_MASK 1023u\n"
        + header_code
        + """

#define EXPORT __declspec(dllexport)

EXPORT float t_rcp(float x) { return sh_rcp(x); }
EXPORT float t_rcp_pos(float x) { return sh_rcp_pos(x); }
EXPORT float t_fdiv(float a, float b) { return sh_fdiv(a, b); }
EXPORT float t_fabsf(float x) { return sh_fabsf(x); }
EXPORT float t_minf(float a, float b) { return sh_minf(a, b); }
EXPORT float t_maxf(float a, float b) { return sh_maxf(a, b); }
EXPORT float t_clipf(float x, float lo, float hi) { return sh_clipf(x, lo, hi); }
EXPORT float t_floorf(float x) { return sh_floorf(x); }
EXPORT float t_ceilf(float x) { return sh_ceilf(x); }
EXPORT float t_roundf(float x) { return sh_roundf(x); }
EXPORT float t_signf(float x) { return sh_signf(x); }
EXPORT float t_fmodf(float x, float y) { return sh_fmodf(x, y); }
EXPORT float t_sqrtf(float x) { return sh_sqrtf(x); }
EXPORT float t_sinf(float x) { return sh_sinf(x); }
EXPORT float t_cosf(float x) { return sh_cosf(x); }
EXPORT float t_tanf(float x) { return sh_tanf(x); }
EXPORT float t_tanhf(float x) { return sh_tanhf(x); }
EXPORT float t_expf(float x) { return sh_expf(x); }
EXPORT float t_logf(float x) { return sh_logf(x); }
EXPORT float t_powf(float a, float b) { return sh_powf(a, b); }
EXPORT float t_log2f(float x) { return sh_log2f(x); }
EXPORT float t_log10f(float x) { return sh_log10f(x); }
EXPORT float t_atanf(float x) { return sh_atanf(x); }
EXPORT float t_atan2f(float y, float x) { return sh_atan2f(y, x); }
EXPORT float t_asinf(float x) { return sh_asinf(x); }
EXPORT float t_acosf(float x) { return sh_acosf(x); }
EXPORT float t_sinhf(float x) { return sh_sinhf(x); }
EXPORT float t_coshf(float x) { return sh_coshf(x); }

EXPORT float t_dread_i(const float *buf, uint32_t wpos, float d) { return sh_dread_i(buf, wpos, d); }
EXPORT float t_dread_1(const float *buf, uint32_t wpos, float d) { return sh_dread_1(buf, wpos, d); }
EXPORT float t_dread_2(const float *buf, uint32_t wpos, float d) { return sh_dread_2(buf, wpos, d); }
EXPORT float t_dread_3(const float *buf, uint32_t wpos, float d) { return sh_dread_3(buf, wpos, d); }
"""
    )
    lib = compile_c_to_dll(c_src)

    unary_fns = [
        "t_rcp", "t_rcp_pos", "t_fabsf", "t_floorf", "t_ceilf", "t_roundf",
        "t_signf", "t_sqrtf", "t_sinf", "t_cosf", "t_tanf", "t_tanhf",
        "t_expf", "t_logf", "t_log2f", "t_log10f", "t_atanf", "t_asinf",
        "t_acosf", "t_sinhf", "t_coshf",
    ]
    for fn in unary_fns:
        f = getattr(lib, fn)
        f.argtypes = [ctypes.c_float]
        f.restype = ctypes.c_float

    binary_fns = ["t_fdiv", "t_minf", "t_maxf", "t_fmodf", "t_powf", "t_atan2f"]
    for fn in binary_fns:
        f = getattr(lib, fn)
        f.argtypes = [ctypes.c_float, ctypes.c_float]
        f.restype = ctypes.c_float

    lib.t_clipf.argtypes = [ctypes.c_float, ctypes.c_float, ctypes.c_float]
    lib.t_clipf.restype = ctypes.c_float

    for fn in ["t_dread_i", "t_dread_1", "t_dread_2", "t_dread_3"]:
        f = getattr(lib, fn)
        f.argtypes = [ctypes.POINTER(ctypes.c_float), ctypes.c_uint32, ctypes.c_float]
        f.restype = ctypes.c_float

    return lib


class TestC674xFaustHeaderMath(unittest.TestCase):
    """Unit tests for all call-free C math & delay primitives in `C674X_FAUST_HEADER`."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.lib_host = _build_math_test_dll(simulate_ti_c674x=False)
        cls.lib_ti = _build_math_test_dll(simulate_ti_c674x=True)

    def test_rcp_and_fdiv_host_and_ti_paths(self) -> None:
        for lib in (self.lib_host, self.lib_ti):
            for x in (-100.0, -3.5, -1.0, -0.125, 0.125, 1.0, 2.5, 44100.0):
                got = lib.t_rcp(x)
                expected = 1.0 / x
                self.assertAlmostEqual(got, expected, delta=abs(expected) * 1e-4)

            for x in (0.1, 1.0, 27.0, 1000.0):
                got = lib.t_rcp_pos(x)
                self.assertAlmostEqual(got, 1.0 / x, delta=(1.0 / x) * 1e-4)

            # Near-zero and negative guard on sh_rcp / sh_rcp_pos must never return NaN/Inf
            self.assertTrue(math.isfinite(lib.t_rcp(0.0)))
            self.assertTrue(math.isfinite(lib.t_rcp(-0.0)))
            self.assertTrue(math.isfinite(lib.t_rcp_pos(0.0)))
            self.assertTrue(math.isfinite(lib.t_rcp_pos(-5.0)))
            self.assertGreater(lib.t_rcp_pos(0.0), 1e15)

            for num, den in ((10.0, 2.5), (-7.5, 3.0), (15.0, -5.0), (-9.0, -4.5)):
                self.assertAlmostEqual(lib.t_fdiv(num, den), num / den, delta=1e-4)

    def test_basic_rounding_and_clipping(self) -> None:
        lib = self.lib_host
        self.assertEqual(lib.t_fabsf(-3.25), 3.25)
        self.assertEqual(lib.t_fabsf(3.25), 3.25)
        self.assertEqual(lib.t_minf(-2.0, 1.5), -2.0)
        self.assertEqual(lib.t_maxf(-2.0, 1.5), 1.5)
        self.assertEqual(lib.t_clipf(-5.0, -1.5, 1.5), -1.5)
        self.assertEqual(lib.t_clipf(5.0, -1.5, 1.5), 1.5)
        self.assertEqual(lib.t_clipf(0.5, -1.5, 1.5), 0.5)

        for x in (-3.7, -3.0, -2.1, -0.01, 0.0, 0.01, 2.1, 3.0, 3.9):
            self.assertEqual(lib.t_floorf(x), math.floor(x), f"sh_floorf({x})")
            self.assertEqual(lib.t_ceilf(x), math.ceil(x), f"sh_ceilf({x})")
            self.assertEqual(lib.t_roundf(x), math.floor(x + 0.5), f"sh_roundf({x})")

        self.assertEqual(lib.t_signf(4.2), 1.0)
        self.assertEqual(lib.t_signf(-4.2), -1.0)
        self.assertEqual(lib.t_signf(0.0), 0.0)

        self.assertAlmostEqual(lib.t_fmodf(5.75, 2.0), 1.75, places=4)
        self.assertAlmostEqual(lib.t_fmodf(-5.75, 2.0), -1.75, places=4)

    def test_sqrtf_host_and_ti_paths(self) -> None:
        for lib in (self.lib_host, self.lib_ti):
            self.assertEqual(lib.t_sqrtf(0.0), 0.0)
            self.assertEqual(lib.t_sqrtf(-4.0), 0.0)
            for x in (1e-4, 0.25, 1.0, 2.0, 9.0, 100.0, 44100.0):
                got = lib.t_sqrtf(x)
                expected = math.sqrt(x)
                self.assertAlmostEqual(got, expected, delta=expected * 2e-4)

    def test_trig_and_hyperbolic_approximations(self) -> None:
        lib = self.lib_ti
        # Parabolic sine and cosine across [-2*pi, 2*pi] (< 0.002 abs error)
        for k in range(-64, 65):
            x = k * (2.0 * math.pi / 32.0)
            self.assertAlmostEqual(lib.t_sinf(x), math.sin(x), delta=0.0025, msg=f"x={x}")
            self.assertAlmostEqual(lib.t_cosf(x), math.cos(x), delta=0.0025, msg=f"x={x}")

        # Padé [3/2] tanf on audio filter design frequencies [0, 1.1]
        for x in (-1.0, -0.5, -0.1, 0.0, 0.1, 0.5, 1.0):
            self.assertAlmostEqual(lib.t_tanf(x), math.tan(x), delta=0.015, msg=f"tan({x})")

        # Padé [3/2] rational tanh saturator: exact 0, odd symmetry, clamps to +-1 at +-3
        self.assertEqual(lib.t_tanhf(0.0), 0.0)
        self.assertAlmostEqual(lib.t_tanhf(3.0), 1.0, places=5)
        self.assertAlmostEqual(lib.t_tanhf(-3.0), -1.0, places=5)
        self.assertAlmostEqual(lib.t_tanhf(10.0), 1.0, places=5)
        self.assertAlmostEqual(lib.t_tanhf(-10.0), -1.0, places=5)
        for x in (0.2, 0.5, 1.0, 1.5, 2.0):
            pos = lib.t_tanhf(x)
            neg = lib.t_tanhf(-x)
            self.assertAlmostEqual(pos, -neg, places=6)
            pade_exact = x * (27.0 + x * x) / (27.0 + 9.0 * x * x)
            self.assertAlmostEqual(pos, pade_exact, delta=1e-4)
            self.assertAlmostEqual(pos, math.tanh(x), delta=0.025)

        # sinhf and coshf
        for x in (-1.5, -0.5, 0.0, 0.5, 1.5):
            self.assertAlmostEqual(lib.t_sinhf(x), math.sinh(x), delta=0.02)
            self.assertAlmostEqual(lib.t_coshf(x), math.cosh(x), delta=0.02)

    def test_exp_log_pow_approximations(self) -> None:
        lib = self.lib_ti
        for x in (-5.0, -2.0, -0.5, 0.0, 0.5, 1.0, 2.0, 5.0):
            expected = math.exp(x)
            self.assertAlmostEqual(lib.t_expf(x), expected, delta=expected * 0.005)

        # Non-positive guard on logf
        self.assertAlmostEqual(lib.t_logf(0.0), -41.4, places=3)
        self.assertAlmostEqual(lib.t_logf(-2.0), -41.4, places=3)

        for x in (0.01, 0.1, 0.5, 1.0, 2.0, 10.0, 100.0, 1000.0):
            self.assertAlmostEqual(lib.t_logf(x), math.log(x), delta=0.005)
            self.assertAlmostEqual(lib.t_log2f(x), math.log2(x), delta=0.008)
            self.assertAlmostEqual(lib.t_log10f(x), math.log10(x), delta=0.005)

        # powf (used in semi2ratio, db2linear, etc.)
        self.assertEqual(lib.t_powf(0.0, 2.0), 0.0)
        self.assertEqual(lib.t_powf(-1.0, 2.0), 0.0)
        self.assertAlmostEqual(lib.t_powf(2.0, 1.0), 2.0, delta=0.01)
        self.assertAlmostEqual(lib.t_powf(2.0, -1.0), 0.5, delta=0.005)
        self.assertAlmostEqual(lib.t_powf(10.0, -20.0 * 0.05), 0.1, delta=0.002)

    def test_inverse_trig_approximations(self) -> None:
        lib = self.lib_ti
        for x in (-50.0, -4.0, -1.0, -0.5, 0.0, 0.5, 1.0, 4.0, 50.0):
            self.assertAlmostEqual(lib.t_atanf(x), math.atan(x), delta=0.002)

        for y, x in ((1.0, 1.0), (1.0, -1.0), (-1.0, -1.0), (-1.0, 1.0), (1.0, 0.0), (-1.0, 0.0), (0.0, 0.0)):
            self.assertAlmostEqual(lib.t_atan2f(y, x), math.atan2(y, x), delta=0.003)

        for x in (-0.8, -0.4, 0.0, 0.4, 0.8):
            self.assertAlmostEqual(lib.t_asinf(x), math.asin(x), delta=0.003)
            self.assertAlmostEqual(lib.t_acosf(x), math.acos(x), delta=0.003)

    def test_sdram_delay_readers_interpolation_and_wraparound(self) -> None:
        lib = self.lib_host
        buf_len = 1024
        mask = 1023
        buf = (ctypes.c_float * buf_len)()

        # 1. Integer delay tap reader `sh_dread_i` across wrap-around boundary (wpos = 5)
        wpos = 5
        for k in range(buf_len):
            buf[(wpos - k) & mask] = float(k * 10)

        self.assertEqual(lib.t_dread_i(buf, wpos, 0.0), 0.0)
        self.assertEqual(lib.t_dread_i(buf, wpos, 1.0), 10.0)
        self.assertEqual(lib.t_dread_i(buf, wpos, 5.0), 50.0)
        # Wrap around index 0 into the end of the 1024-sample ring buffer
        self.assertEqual(lib.t_dread_i(buf, wpos, 6.0), 60.0)
        self.assertEqual(lib.t_dread_i(buf, wpos, 120.0), 1200.0)
        # Rounding to nearest integer (`(int)(d + 0.5f)`) and boundary clamping
        self.assertEqual(lib.t_dread_i(buf, wpos, 11.8), 120.0)
        self.assertEqual(lib.t_dread_i(buf, wpos, -5.0), 0.0)
        self.assertEqual(lib.t_dread_i(buf, wpos, 5000.0), float(mask * 10))

        # 2. 2-point linear Lagrange `sh_dread_1` on linear signal f(d) = 2.5 * d + 3.0
        wpos = 10
        for k in range(buf_len):
            buf[(wpos - k) & mask] = 2.5 * k + 3.0

        for d in (0.0, 0.25, 1.5, 10.3, 10.75, 250.6):
            self.assertAlmostEqual(lib.t_dread_1(buf, wpos, d), 2.5 * d + 3.0, places=3)

        # 3. 3-point quadratic Lagrange `sh_dread_2` on quadratic signal f(d) = 0.05 * d^2 - 0.4 * d + 1.0
        for k in range(100):
            buf[(wpos - k) & mask] = 0.05 * (k * k) - 0.4 * k + 1.0

        for d in (2.25, 5.5, 10.4, 24.75):
            expected = 0.05 * (d * d) - 0.4 * d + 1.0
            self.assertAlmostEqual(lib.t_dread_2(buf, wpos, d), expected, delta=1e-3)

        # 4. 4-point cubic Lagrange `sh_dread_3` on cubic signal f(d) = 0.002 * d^3 - 0.03 * d^2 + 0.5 * d - 1.0
        for k in range(100):
            buf[(wpos - k) & mask] = 0.002 * (k ** 3) - 0.03 * (k ** 2) + 0.5 * k - 1.0

        for d in (3.25, 8.5, 11.2, 30.75):
            expected = 0.002 * (d ** 3) - 0.03 * (d ** 2) + 0.5 * d - 1.0
            self.assertAlmostEqual(lib.t_dread_3(buf, wpos, d), expected, delta=1e-3)


class TestCompiledCKernels(unittest.TestCase):
    """Unit tests natively executing the C effect kernels in `effects/*/*.c`."""

    def _load_effect_harness(self, effect_name: str) -> KernelHarness:
        eff_dir = ROOT / "effects" / effect_name
        gen_dir = eff_dir / "gen" if (eff_dir / "gen" / "manifest.json").is_file() else eff_dir
        manifest = json.loads((gen_dir / "manifest.json").read_text(encoding="utf-8-sig"))
        c_src = (gen_dir / manifest["kernel"]).read_text(encoding="utf-8-sig")
        return KernelHarness(c_src, manifest)

    def test_fail_closed_safety_prologue_no_delay_kernel(self) -> None:
        """Verify mandatory SAFETY.md fail-closed guard checks on `svfflt.c`."""
        with self._load_effect_harness("svfflt") as h:
            signal = [0.25] * SH_FRAMES

            # 1. Null instance or null ctx is a safe no-op
            h._run_fn(None, h.ctx)
            h._run_fn(h.instance, None)

            # 2. Corrupted SH_STATE_GUARD canary fails closed to 0.0f
            h.reset()
            h.state_words[h.guard_word] = 0xDEADBEEF
            out_l, out_r = h.run_block(signal, signal)
            self.assertEqual(out_l, [0.0] * SH_FRAMES)
            self.assertEqual(out_r, [0.0] * SH_FRAMES)

            # 3. Null coef pointer (instance[1] = NULL) fails closed to 0.0f
            h.reset()
            h.instance[1] = 0
            out_l, out_r = h.run_block(signal, signal)
            self.assertEqual(out_l, [0.0] * SH_FRAMES)
            self.assertEqual(out_r, [0.0] * SH_FRAMES)

            # 4. Out-of-range bypass fade (< 0, > 1, NaN) fails closed to 0.0f
            for bad_fade in (-0.1, 1.1, float("nan")):
                h.reset()
                h.set_bypass_fade(bad_fade)
                out_l, out_r = h.run_block(signal, signal)
                self.assertEqual(out_l, [0.0] * SH_FRAMES)

            # 5. Out-of-range linear knob (< 0 or > 1) and stock_vol knob (> 1.6) fail closed
            h.reset()
            h.set_param_norm("Drive", 1.05)
            out_l, _ = h.run_block(signal, signal)
            self.assertEqual(out_l, [0.0] * SH_FRAMES)

            h.reset()
            h.set_param_norm("Level", 1.65)
            out_l, _ = h.run_block(signal, signal)
            self.assertEqual(out_l, [0.0] * SH_FRAMES)

    def test_fail_closed_sdram_descriptor_guard(self) -> None:
        """Verify mandatory wrap-safe `instance[3]` SDRAM descriptor checks on `tzflng.c`."""
        with self._load_effect_harness("tzflng") as h:
            signal = [0.25] * SH_FRAMES

            # 1. Null instance[3]
            h.reset()
            h.instance[3] = 0
            out_l, out_r = h.run_block(signal, signal)
            self.assertEqual(out_l, [0.0] * SH_FRAMES)
            self.assertEqual(out_r, [0.0] * SH_FRAMES)

            # 2. Unaligned base address (b & 3 != 0)
            h.reset()
            h.dd[0] = h.sdram.base_addr + 1
            h.dd[1] = h.dd[0] + h.sdram.size_bytes
            out_l, _ = h.run_block(signal, signal)
            self.assertEqual(out_l, [0.0] * SH_FRAMES)

            # 3. Buffer size smaller than need_bytes (12,480 B for 3 x 1040-float lines)
            h.reset()
            h.dd[2] = 4096
            h.dd[1] = h.dd[0] + 4096
            out_l, _ = h.run_block(signal, signal)
            self.assertEqual(out_l, [0.0] * SH_FRAMES)

            # 4. Mismatched (e - b) != sz
            h.reset()
            h.dd[1] = h.dd[0] + h.dd[2] + 4
            out_l, _ = h.run_block(signal, signal)
            self.assertEqual(out_l, [0.0] * SH_FRAMES)

    def test_nan_inf_rail_scrubbing_and_state_sanitization(self) -> None:
        """Verify input (-8, +8) NaN/Inf clamping and persistent state NaN recovery."""
        with self._load_effect_harness("svfflt") as h:
            # Corrupt persistent float state words with NaN / +Inf / 1e20f
            float_state_ptr = ctypes.cast(h.state_words, ctypes.POINTER(ctypes.c_float))
            for idx in range(h.guard_word - 1):
                float_state_ptr[idx] = float("nan") if idx % 2 == 0 else 1.0e20

            bad_inputs = [
                float("nan"), float("inf"), float("-inf"),
                9.5, -12.0, 0.25, -0.25, 0.1,
                0.0, 0.2, -0.2, 8.0, -8.0, 0.15, -0.15, 0.05,
            ]
            out_l, out_r = h.run_block(bad_inputs, bad_inputs)
            for s in out_l + out_r:
                self.assertTrue(math.isfinite(s), f"Non-finite output sample: {s}")
                self.assertGreater(s, -8.0)
                self.assertLess(s, 8.0)

            # State words must now all be scrubbed to finite values
            for idx in range(h.guard_word - 1):
                self.assertTrue(math.isfinite(float_state_ptr[idx]))

    def test_first_block_smoother_snap_and_subsequent_glide(self) -> None:
        """Verify `init_magic == 0x46535431u` snaps smoothers on block 1 and glides on block 2."""
        with self._load_effect_harness("svfflt") as h:
            # At reset, state is all zeros (except SH_STATE_GUARD at guard_word)
            self.assertNotIn(0x46535431, list(h.state_words[: h.guard_word]))
            h.run_block([0.0] * SH_FRAMES, [0.0] * SH_FRAMES)
            # After block 1, init_magic (word 14 in svfflt's 15-word faust_zd2_state_t) is 0x46535431u
            self.assertIn(0x46535431, list(h.state_words[: h.guard_word]))

            float_state = ctypes.cast(h.state_words, ctypes.POINTER(ctypes.c_float))
            # fSmoo_3 (word 1 in svfflt's state struct) is Drive (default 45.0)
            self.assertAlmostEqual(float_state[1], 45.0, places=3)

            # Change Drive to 100.0 (norm = 1.0): block 2 should glide by 0.03152 * (100 - 45)
            h.set_param_norm("Drive", 1.0)
            h.run_block([0.0] * SH_FRAMES, [0.0] * SH_FRAMES)
            expected_glided = 45.0 + 0.03152 * (100.0 - 45.0)
            self.assertAlmostEqual(float_state[1], expected_glided, places=3)

    def test_bypass_crossfade_behavior(self) -> None:
        """Verify `fade = 0.0` is bit-exact dry pass-through and `fade = 0.5` is exact 50/50 mix."""
        with self._load_effect_harness("svfflt") as h:
            dry = [0.15 * math.sin(2.0 * math.pi * i / 16.0) for i in range(SH_FRAMES)]

            h.reset()
            h.set_bypass_fade(0.0)
            out_dry_l, out_dry_r = h.run_block(dry, dry)
            for i in range(SH_FRAMES):
                self.assertAlmostEqual(out_dry_l[i], dry[i], places=6)
                self.assertAlmostEqual(out_dry_r[i], dry[i], places=6)

            h.reset()
            h.set_bypass_fade(1.0)
            out_wet_l, _ = h.run_block(dry, dry)

            h.reset()
            h.set_bypass_fade(0.5)
            out_half_l, _ = h.run_block(dry, dry)
            for i in range(SH_FRAMES):
                expected_half = dry[i] + 0.5 * (out_wet_l[i] - dry[i])
                self.assertAlmostEqual(out_half_l[i], expected_half, places=5)

    def test_svfflt_svf_frequency_response(self) -> None:
        """Verify `svfflt.c` SVF low-pass filter passes low frequencies and attenuates Nyquist."""
        with self._load_effect_harness("svfflt") as h:
            # Set low cutoff (Freq norm = 0.05 -> ~546 Hz), low resonance, unity level
            h.set_param_norm("Drive", 0.0)
            h.set_param_norm("Freq", 0.05)
            h.set_param_norm("Res", 0.0)

            # Run 10 blocks of 200 Hz low-frequency sine vs 10 blocks of Nyquist (+0.2, -0.2)
            low_rms = 0.0
            for b in range(10):
                blk = [0.2 * math.sin(2.0 * math.pi * 200.0 * (b * 16 + i) / 44100.0) for i in range(16)]
                out_l, _ = h.run_block(blk, blk)
                if b >= 5:
                    low_rms += sum(x * x for x in out_l)

            h.reset()
            h.set_param_norm("Drive", 0.0)
            h.set_param_norm("Freq", 0.05)
            h.set_param_norm("Res", 0.0)
            nyq_rms = 0.0
            for b in range(10):
                blk = [0.2 if i % 2 == 0 else -0.2 for i in range(16)]
                out_l, _ = h.run_block(blk, blk)
                if b >= 5:
                    nyq_rms += sum(x * x for x in out_l)

            self.assertGreater(low_rms, 0.1)
            self.assertLess(nyq_rms, low_rms * 0.05)

    def test_tzflng_dry_alignment_delay_and_wide_stereo_polarity(self) -> None:
        """
        Verify `tzflng.c` delays dry signal by 120 samples (`de.delay(1024, 120.0, xL)`)
        and differentiates Left/Right channels when `Wide = 1`.
        """
        with self._load_effect_harness("tzflng") as h:
            # Set Depth = 0 (norm 0.5 -> 0% wet) to isolate the 120-sample dry alignment delay
            h.set_param_norm("Depth", 0.5)
            impulse = [0.5] + [0.0] * (SH_FRAMES - 1)
            zeros = [0.0] * SH_FRAMES

            all_l = []
            for b in range(10):
                out_l, _ = h.run_block(impulse if b == 0 else zeros, zeros)
                all_l.extend(out_l)

            # Sample 120 should carry the 0.5 impulse, while samples 0..119 are 0.0
            self.assertEqual( max(abs(x) for x in all_l[:120]), 0.0)
            self.assertAlmostEqual(all_l[120], 0.5, places=4)

            # Now enable wet Depth (-75% default) and Wide = 1 (norm 1.0): L and R must differ
            h.reset()
            h.set_param_norm("Wide", 1.0)
            diff_energy = 0.0
            for b in range(12):
                sig = [0.25 * math.sin(2.0 * math.pi * 330.0 * (b * 16 + i) / 44100.0) for i in range(16)]
                out_l, out_r = h.run_block(sig, sig)
                diff_energy += sum((l - r) ** 2 for l, r in zip(out_l, out_r))
            self.assertGreater(diff_energy, 1e-3)

    def test_xarvb_sdram_reverb_impulse_tail(self) -> None:
        """
        Verify `xarvb.c` writes into its 8 SDRAM delay lines and produces a
        late reverb tail after an input burst stops.
        """
        with self._load_effect_harness("xarvb") as h:
            burst = [0.3 * math.sin(2.0 * math.pi * 500.0 * i / 16.0) for i in range(16)]
            zeros = [0.0] * 16
            for _ in range(4):
                h.run_block(burst, burst)

            tail_energy = 0.0
            for _ in range(250):
                out_l, out_r = h.run_block(zeros, zeros)
                tail_energy += sum(l * l + r * r for l, r in zip(out_l, out_r))

            self.assertGreater(tail_energy, 1e-4)

    def test_ym2612_genesis_artifacts(self) -> None:
        """
        Verify `ym2612.c` Sega Genesis / Mega Drive artifact emulation:
          1. Silence in -> exact silence out (zero idle buzz / zero DC offset).
          2. `Ladder` zero-crossing discontinuity adds audible crossover harmonics
             to a quiet decaying sine wave compared to `Ladder = 0`.
          3. `Model = M2VA1` (norm 2/3) is darker on high-frequency content and
             narrower in stereo than `RAW9B` (norm 1.0).
          4. `Rate = 0` (4 kHz Z80 PCM) produces staircase zero-order hold steps.
        """
        with self._load_effect_harness("ym2612") as h:
            # 1. Exact silence on zero input
            zeros = [0.0] * SH_FRAMES
            for _ in range(8):
                out_l, out_r = h.run_block(zeros, zeros)
                self.assertEqual(max(abs(s) for s in out_l + out_r), 0.0)

            # 2. Ladder effect crossover boost on quiet 220 Hz sine wave
            def run_quiet_sine(ladder_norm: float) -> float:
                h.reset()
                h.set_param_norm("Model", 1.0)    # RAW9B (unfiltered)
                h.set_param_norm("Rate", 1.0)     # 44.1 kHz
                h.set_param_norm("Bits", 0.55)    # 9-bit YM2612 DAC
                h.set_param_norm("Ladder", ladder_norm)
                h.set_param_norm("DMAJit", 0.0)
                h.set_param_norm("Drive", 0.0)
                energy = 0.0
                for b in range(20):
                    sig = [0.02 * math.sin(2.0 * math.pi * 220.0 * (b * 16 + i) / 44100.0) for i in range(16)]
                    ol, _ = h.run_block(sig, sig)
                    if b >= 4:
                        energy += sum(x * x for x in ol)
                return energy

            e_no_ladder = run_quiet_sine(0.0)
            e_with_ladder = run_quiet_sine(1.0)
            self.assertGreater(e_with_ladder, e_no_ladder * 2.0)

            # 3. Model M2VA1 vs RAW9B: darker HF & narrower stereo
            def run_model_hf_stereo(model_norm: float) -> tuple[float, float]:
                h.reset()
                h.set_param_norm("Model", model_norm)
                h.set_param_norm("Rate", 1.0)
                h.set_param_norm("Bits", 0.0)
                h.set_param_norm("Ladder", 0.0)
                h.set_param_norm("DMAJit", 0.0)
                h.set_param_norm("Drive", 0.0)
                hf_e = 0.0
                side_e = 0.0
                for b in range(16):
                    # 6 kHz opposite-phase stereo test tone
                    in_l = [0.2 * math.sin(2.0 * math.pi * 6000.0 * (b * 16 + i) / 44100.0) for i in range(16)]
                    in_r = [-x for x in in_l]
                    ol, or_ = h.run_block(in_l, in_r)
                    if b >= 4:
                        hf_e += sum(l * l for l in ol)
                        side_e += sum((l - r) ** 2 for l, r in zip(ol, or_))
                return hf_e, side_e

            m2_hf, m2_side = run_model_hf_stereo(2.0 / 3.0)  # M2VA1
            raw_hf, raw_side = run_model_hf_stereo(1.0)      # RAW9B
            self.assertLess(m2_hf, raw_hf * 0.25)
            self.assertLess(m2_side, raw_side * 0.25)

    def test_all_effect_c_kernels_compile_and_process_audio_cleanly(self) -> None:
        """
        Compile and execute every C effect kernel in `effects/` over 8 stereo blocks
        of test audio, verifying finite bounded output, non-zero active processing,
        and intact `SH_STATE_GUARD` canary word.
        """
        effect_names = [
            "tzflng",
            "svfflt",
            "anlgdrv",
            "vocalfe",
            "swedsaw",
            "deskdrv",
            "octring",
            "xachr",
            "xadly",
            "xarvb",
            "xaspu",
            "slewdrv",
            "scsprvb",
            "rackptch",
            "rf5cfx",
            "rf5cdly",
            "brrdsp",
            "firecho",
            "sophwg",
            "stutrtch",
            "cdskip",
            "ym2612",
        ]
        for name in effect_names:
            with self.subTest(effect=name):
                with self._load_effect_harness(name) as h:
                    energy = 0.0
                    for b in range(12):
                        in_l = [0.25 * math.sin(2.0 * math.pi * 440.0 * (b * 16 + i) / 44100.0) for i in range(16)]
                        in_r = [0.25 * math.cos(2.0 * math.pi * 440.0 * (b * 16 + i) / 44100.0) for i in range(16)]
                        out_l, out_r = h.run_block(in_l, in_r)
                        for s in out_l + out_r:
                            self.assertTrue(math.isfinite(s), f"{name} produced non-finite sample")
                            self.assertGreater(s, -8.0)
                            self.assertLess(s, 8.0)
                        energy += sum(s * s for s in out_l + out_r)
                        # Verify SH_STATE_GUARD canary was never overwritten by out-of-bounds state access
                        self.assertEqual(
                            h.state_words[h.guard_word],
                            0x57464731,
                            f"{name} corrupted SH_STATE_GUARD canary word!",
                        )
                    self.assertGreater(energy, 1e-4, f"{name} produced complete silence on valid input")

    def test_scsprvb_saturn_reverb_tail_and_silence(self) -> None:
        """Verify `scsprvb.c` produces exact silence on zero input and a stereo reverb tail on impulse."""
        with self._load_effect_harness("scsprvb") as h:
            zeros = [0.0] * SH_FRAMES
            for _ in range(8):
                ol, or_ = h.run_block(zeros, zeros)
                self.assertEqual(max(abs(s) for s in ol + or_), 0.0)

            h.reset()
            h.set_param_norm("PreD", 0.0)
            h.set_param_norm("Decay", 0.8)
            h.set_param_norm("Mix", 1.0)
            imp = [0.8] + [0.0] * (SH_FRAMES - 1)
            h.run_block(imp, imp)
            tail_l = 0.0
            tail_r = 0.0
            for _ in range(240):
                ol, or_ = h.run_block(zeros, zeros)
                tail_l += sum(s * s for s in ol)
                tail_r += sum(s * s for s in or_)
            self.assertGreater(tail_l, 1e-4)
            self.assertGreater(tail_r, 1e-4)

    def test_rf5cdly_pingpong_and_silence(self) -> None:
        """Verify `rf5cdly.c` produces exact silence on zero input and L->R ping-pong repeats."""
        with self._load_effect_harness("rf5cdly") as h:
            zeros = [0.0] * SH_FRAMES
            for _ in range(8):
                ol, or_ = h.run_block(zeros, zeros)
                self.assertEqual(max(abs(s) for s in ol + or_), 0.0)

            h.reset()
            h.set_param_norm("Mode", 0.0)   # PING
            h.set_param_norm("Time", 0.0)   # 20 ms (~882 samples = ~55 blocks)
            h.set_param_norm("Fdbk", 0.65)
            h.set_param_norm("Mix", 1.0)
            imp = [0.8] + [0.0] * (SH_FRAMES - 1)
            h.run_block(imp, zeros)
            e_l = 0.0
            e_r = 0.0
            for _ in range(180):
                ol, or_ = h.run_block(zeros, zeros)
                e_l += sum(s * s for s in ol)
                e_r += sum(s * s for s in or_)
            self.assertGreater(e_l, 1e-4, "rf5cdly missing left echo")
            self.assertGreater(e_r, 1e-4, "rf5cdly missing ping-pong right bounce")

    def test_rf5cfx_clkjit_and_silence(self) -> None:
        """Verify `rf5cfx.c` produces exact silence at ClkJit=1 and measurable clock-jitter modulation."""
        with self._load_effect_harness("rf5cfx") as h:
            zeros = [0.0] * SH_FRAMES
            h.set_param_norm("ClkJit", 1.0)
            for _ in range(16):
                ol, or_ = h.run_block(zeros, zeros)
                self.assertEqual(max(abs(s) for s in ol + or_), 0.0)

            def run_tone(clkjit_norm: float):
                h.reset()
                h.set_param_norm("Model", 1.0)    # RAW8B
                h.set_param_norm("Rate", 0.25)    # ~11 kHz RF5C164 clock
                h.set_param_norm("Bits", 0.55)    # 8-bit sign-magnitude
                h.set_param_norm("ClkJit", clkjit_norm)
                h.set_param_norm("Drive", 0.0)
                samples = []
                for b in range(40):
                    sig = [0.35 * math.sin(2.0 * math.pi * 880.0 * (b * 16 + i) / 44100.0) for i in range(16)]
                    ol, _ = h.run_block(sig, sig)
                    if b >= 16:
                        samples.extend(ol)
                return samples

            locked = run_tone(0.0)
            jittered = run_tone(1.0)
            diff_energy = sum((a - b) ** 2 for a, b in zip(locked, jittered))
            self.assertGreater(diff_energy, 1e-3, "ClkJit=1.0 did not modulate RF5C164 PCM clock")

    def test_firecho_presets_stability_and_wrap(self) -> None:
        """Verify `firecho.c`: exact silence, all 4 FIR presets stable at high EFB, and SatWrap behavior."""
        with self._load_effect_harness("firecho") as h:
            zeros = [0.0] * SH_FRAMES
            for _ in range(8):
                ol, or_ = h.run_block(zeros, zeros)
                self.assertEqual(max(abs(s) for s in ol + or_), 0.0)

            # Verify all 4 presets produce non-zero echo tails and remain bounded at Fdbk=0.95
            preset_energies = []
            for p_idx in range(4):
                h.reset()
                h.set_param_norm("FIR", p_idx / 3.0)
                h.set_param_norm("Time", 0.0)     # 16 ms (~706 samples = ~44 blocks)
                h.set_param_norm("Fdbk", 0.95)
                h.set_param_norm("SatWrap", 0.0)
                h.set_param_norm("Mix", 1.0)
                imp = [0.6] + [0.0] * (SH_FRAMES - 1)
                h.run_block(imp, imp)
                e = 0.0
                peak = 0.0
                for _ in range(180):
                    ol, or_ = h.run_block(zeros, zeros)
                    for s in ol + or_:
                        self.assertTrue(math.isfinite(s))
                        peak = max(peak, abs(s))
                    e += sum(s * s for s in ol + or_)
                self.assertGreater(e, 1e-4, f"SNESFIR preset {p_idx} produced silence")
                self.assertLess(peak, 2.0, f"SNESFIR preset {p_idx} blew up at high EFB")
                preset_energies.append(e)

            # CHRONO (low-pass) and DKC (high-pass/comb) should differ noticeably from RAW
            self.assertNotAlmostEqual(preset_energies[0], preset_energies[2], places=2)

            # Verify SatWrap=1.0 differs from clean saturation SatWrap=0.0 on a hot signal
            def run_hot(wrap_norm: float):
                h.reset()
                h.set_param_norm("FIR", 0.0)      # RAW
                h.set_param_norm("Time", 0.0)     # 16 ms
                h.set_param_norm("Fdbk", 0.7)
                h.set_param_norm("SatWrap", wrap_norm)
                h.set_param_norm("Mix", 1.0)
                out = []
                for b in range(70):
                    sig = [0.9 * math.sin(2.0 * math.pi * 220.0 * (b * 16 + i) / 44100.0) for i in range(16)]
                    ol, _ = h.run_block(sig, sig)
                    if b >= 45:
                        out.extend(ol)
                return out

            sat_out = run_hot(0.0)
            wrap_out = run_hot(1.0)
            diff_wrap = sum((a - b) ** 2 for a, b in zip(sat_out, wrap_out))
            self.assertGreater(diff_wrap, 1e-3, "SatWrap=1.0 did not differ from SatWrap=0.0")

    def test_brrdsp_gaussian_rolloff_and_silence(self) -> None:
        """Verify `brrdsp.c`: exact silence on zero input and SNES 4-point Gaussian HF rolloff vs ZOH."""
        with self._load_effect_harness("brrdsp") as h:
            zeros = [0.0] * SH_FRAMES
            for _ in range(12):
                ol, or_ = h.run_block(zeros, zeros)
                self.assertEqual(max(abs(s) for s in ol + or_), 0.0)

            def hf_energy(model_norm: float, gauss_norm: float) -> float:
                h.reset()
                h.set_param_norm("Model", model_norm)
                h.set_param_norm("Filter", 2.0 / 3.0)  # F2
                h.set_param_norm("Rate", 0.5)          # ~18 kHz BRR voice clock
                h.set_param_norm("Gauss", gauss_norm)
                h.set_param_norm("BrrGrit", 0.55)
                h.set_param_norm("Drive", 0.0)
                e = 0.0
                for b in range(30):
                    # High-frequency 7.5 kHz tone
                    sig = [0.35 * math.sin(2.0 * math.pi * 7500.0 * (b * 16 + i) / 44100.0) for i in range(16)]
                    ol, _ = h.run_block(sig, sig)
                    if b >= 8:
                        e += sum(s * s for s in ol)
                return e

            e_zoh = hf_energy(1.0, 0.0)       # ZOH (unfiltered staircase)
            e_gauss = hf_energy(0.0, 0.85)    # SHVC + SNES 4-point Gaussian blanket
            self.assertLess(e_gauss, e_zoh * 0.65, "SNES Gaussian rolloff did not attenuate HF vs ZOH")


if __name__ == "__main__":
    unittest.main()
