"""
tests/test_faust_unit.py — Unit tests for the `tools/faust2zd2_pkg/` Faust
transpiler and C674x kernel emitter:

  1. `models.py`: `strip_c_comments`, `parse_faust_dsp_metadata` (declarations,
     UI controls `hslider`/`vslider`/`nentry`/`checkbox`/`button`, bracket tags,
     `stock_vol` heuristic, parameter ordering & deduplication).
  2. `rewriter.py`: `enforce_float32_literals` (zero `double` types/literals),
     `rewrite_c_divisions_and_math` (`/` and `/=` to `sh_fdiv`, `<math.h>`
     function redirects).
  3. `subset_compiler.py`: block-diagram algebra (`:`, `,`, `<:`, `:>`, `~`),
     pattern macros (`seq`, `par`, `sum`, `prod`), lambdas, `with { ... }`,
     infix `'`, `@`, `^`, `%`, `stdfaust.lib` primitives, `import("zoom.lib");`,
     common-delay-line SDRAM sharing, `+16`-float L1D cache-set skew stride,
     and compile-time state/SDRAM budget enforcement.
  4. `c_backend.py`: `convert_faust_c_to_zd2_kernel` (Mode 1 `faust -lang c`
     struct + `computemydsp` converter) + native C compilation and execution
     verification via `KernelHarness`.
  5. `emitter.py`: `build_manifest_dict` (linear, `log`, `exp`, `stock_vol`,
     discrete `values`, and trailing `footswitch` toggle generation).
"""

from __future__ import annotations

import math
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

from zoom_faust_fx.postprocess import (
    FaustMemoryLimitError,
    FaustMemoryReport,
    FaustMeta,
    FaustParam,
    FaustSubsetCompiler,
    analyze_faust_memory,
    build_manifest_dict,
    convert_faust_c_to_zd2_kernel,
    enforce_float32_literals,
    parse_faust_dsp_metadata,
    rewrite_c_divisions_and_math,
    strip_c_comments,
)
from tests.c_harness import KernelHarness, SH_FRAMES


class TestFaustModelsAndParser(unittest.TestCase):
    """Unit tests for `tools/faust2zd2_pkg/models.py`."""

    def test_strip_c_comments(self) -> None:
        src = """
        // single line comment
        declare name "TestFx"; /* inline block
        comment */
        process = _ * 0.5; // trailing
        """
        cleaned = strip_c_comments(src)
        self.assertNotIn("single line", cleaned)
        self.assertNotIn("inline block", cleaned)
        self.assertIn('declare name "TestFx";', cleaned)
        self.assertIn("process = _ * 0.5;", cleaned)

    def test_parse_faust_dsp_metadata_and_controls(self) -> None:
        dsp = """
        declare name "MySynthFx";
        declare zd2_filename "XXSYNTH";
        declare zd2_id "0x070011a0";
        declare version "1.20";
        declare zd2_display "SYNTH FX";
        declare zd2_badge "SY";
        declare description "Test synth effect.";
        declare zd2_dspload "19";
        declare author "waveformer";
        declare zd2_footswitch "true";

        // Out-of-order controls with bracket tags
        p_vol  = hslider("Level[4][tooltip:Master output level.]", 80, 0, 100, 1);
        p_freq = vslider("Freq[1][unit:Hz][scale:log][tooltip:Cutoff freq.]", 1000, 100, 10000, 1);
        p_mode = nentry("Mode[2][values:LP|BP|HP|NOTCH]", 1, 0, 3, 1);
        p_sw   = checkbox("Bright[3]");
        p_trig = button("Tap[5]");
        // Duplicate label should be deduplicated
        p_freq_dup = vslider("Freq[1][unit:Hz][scale:log]", 1000, 100, 10000, 1);
        process = _;
        """
        meta, params = parse_faust_dsp_metadata(dsp)
        self.assertEqual(meta.name, "MySynthFx")
        self.assertEqual(meta.filename, "XXSYNTH")
        self.assertEqual(meta.effect_id, "070011a0")
        self.assertEqual(meta.version, "1.20")
        self.assertEqual(meta.display, "SYNTH FX")
        self.assertEqual(meta.badge, "SY")
        self.assertEqual(meta.dspload, 19)
        self.assertEqual(meta.author, "waveformer")
        self.assertEqual(meta.footswitch, {})

        # 5 unique controls sorted by [1]..[5]
        self.assertEqual(len(params), 5)
        self.assertEqual([p.label for p in params], ["Freq", "Mode", "Bright", "Level", "Tap"])

        self.assertEqual(params[0].scale, "log")
        self.assertEqual(params[0].unit, "Hz")
        self.assertEqual(params[0].c_macro_name, "SH_PARAM_FREQ")

        self.assertEqual(params[1].values, ["LP", "BP", "HP", "NOTCH"])
        self.assertEqual(params[2].values, ["OFF", "ON"])
        # Level 0..100 init 80 auto-detects as stock_vol
        self.assertTrue(params[3].is_stock_vol)
        self.assertEqual(params[4].values, ["OFF", "TRIG"])

        # build_manifest_dict should append the trailing ON/OFF footswitch toggle
        manifest = build_manifest_dict(meta, params, "mysynth.c", 32)
        self.assertEqual(len(manifest["params"]), 6)
        self.assertEqual(manifest["params"][-1]["name"], "ON/OFF")
        self.assertTrue(manifest["params"][-1]["footswitch_toggle"])
        self.assertTrue(manifest["params"][-1]["trailing"])
        # Log-scaled Freq 100..10000 with init 1000 should land at click 50 of 100
        self.assertEqual(manifest["params"][0]["default"], 50)
        self.assertEqual(manifest["params"][3]["curve"], "stock_vol")


class TestFaustRewriter(unittest.TestCase):
    """Unit tests for `tools/faust2zd2_pkg/rewriter.py`."""

    def test_enforce_float32_literals(self) -> None:
        code = "double x = 1.0 + 0.25 * 3.14159265e+00 - 1e-5 + .5 - 2.;"
        out = enforce_float32_literals(code)
        self.assertNotIn("double", out)
        self.assertIn("float x = 1.0f + 0.25f * 3.14159265e+00f - 1e-5f + .5f - 2.f;", out)

    def test_rewrite_c_divisions_and_math(self) -> None:
        code = """
        double a = sin(x) / cos(y);
        float b = (a + 1.0) / (2.0 * sqrt(z));
        b /= 3.5;
        float c = pow(10.0, db / 20.0) + tanh(u);
        """
        out = rewrite_c_divisions_and_math(code)
        self.assertNotIn("/", out)
        self.assertNotIn("double", out)
        self.assertIn("sh_fdiv(sh_sinf(x), sh_cosf(y))", out)
        self.assertIn("sh_fdiv((a + 1.0f), (2.0f * sh_sqrtf(z)))", out)
        self.assertIn("b = sh_fdiv(b, 3.5f);", out)
        self.assertIn("sh_powf(10.0f, sh_fdiv(db, 20.0f))", out)
        self.assertIn("sh_tanhf(u)", out)


class TestFaustSubsetCompiler(unittest.TestCase):
    """Unit tests for `tools/faust2zd2_pkg/subset_compiler.py`."""

    def _compile_dsp(self, dsp_text: str):
        meta, params = parse_faust_dsp_metadata(dsp_text)
        compiler = FaustSubsetCompiler(
            dsp_text,
            meta,
            params,
            search_dirs=[ROOT / "effects", ROOT],
        )
        kernel_c, state_bytes, params = compiler.compile()
        manifest = build_manifest_dict(meta, params, "test.c", state_bytes)
        return kernel_c, state_bytes, params, manifest, compiler

    def test_block_diagram_algebra_and_pattern_macros(self) -> None:
        """
        Test `:`, `,`, `<:`, `:>`, `~`, `seq`, `par`, `sum`, `prod`, lambdas,
        `'` 1-sample delay, `@` small register delay, `^`, `%`, and execute
        the resulting C kernel natively.
        """
        dsp = r"""
        declare name "AlgTest";
        declare zd2_filename "XXALGT";
        declare zd2_id "070011b0";
        import("stdfaust.lib");

        gain = hslider("Gain[1]", 50, 0, 100, 1) * 0.01 : si.smoo;

        // Compile-time sum & prod folds
        k_sum  = sum(i, 4, i + 1);   // 1 + 2 + 3 + 4 = 10
        k_prod = prod(i, 3, i + 1);  // 1 * 2 * 3 = 6
        norm_k = k_prod / k_sum;     // 0.6

        // Anonymous lambda + 1-sample delay + small @ delay
        fir2 = \(x).(0.5 * x + 0.25 * (x') + 0.25 * (x @ 2));

        // 1-wire recursive feedback loop (~)
         pole_step(y_prev, x_in) = 0.5 * x_in + 0.5 * y_prev;
        one_pole(x) = (pole_step(x) ~ _);

        // Split (<:) and Merge (:>) with seq()
        branch(x) = x <: (fir2, one_pole) :> *(0.5 * norm_k * gain);

        process = par(i, 2, seq(j, 2, branch));
        """
        kernel_c, state_bytes, params, manifest, _ = self._compile_dsp(dsp)
        self.assertLessEqual(state_bytes, 188)
        self.assertNotIn("double", kernel_c)

        with KernelHarness(kernel_c, manifest) as h:
            impulse = [0.5] + [0.0] * (SH_FRAMES - 1)
            out_l, out_r = h.run_block(impulse, impulse)
            # Symmetric par(i, 2, ...) must produce identical L and R on identical inputs
            self.assertEqual(out_l, out_r)
            self.assertGreater(out_l[0], 0.0)
            self.assertGreater(out_l[1], 0.0)
            self.assertGreater(out_l[2], 0.0)

    def test_common_delay_line_sharing_and_l1d_cache_skew(self) -> None:
        """
        Verify multiple `de.fdelay*` taps on the same signal share a single
        SDRAM delay line per channel and offset consecutive lines by `SH_DELAY_LEN + 16`.
        """
        dsp = """
        declare name "MultiTap";
        declare zd2_filename "XXMTAP";
        declare zd2_id "070011c0";
        import("stdfaust.lib");
        import("zoom.lib");

        // 3 taps reading from the exact same input signal `x` per channel
        multi_tap(x) = 0.5 * zm_delay(10.0, x)
                     + 0.3 * zm_fdelay1(20.5, x)
                     + 0.2 * zm_fdelay3(30.25, x);

        process(xL, xR) = multi_tap(xL), multi_tap(xR);
        """
        kernel_c, state_bytes, _, manifest, compiler = self._compile_dsp(dsp)
        # 3 taps on xL share dline_0, 3 taps on xR share dline_1 -> total 2 delay lines!
        self.assertEqual(compiler.delay_line_count, 2)
        self.assertEqual(compiler.delay_line_len, 4096)
        # Verify +16 float (+64 B) L1D cache-set skew stride (4096 + 16 = 4112)
        self.assertIn("dline_0 = ((float *)dbaseAddr) + 0u;", kernel_c)
        self.assertIn("dline_1 = ((float *)dbaseAddr) + 4112u;", kernel_c)

        with KernelHarness(kernel_c, manifest) as h:
            impulse = [0.8] + [0.0] * (SH_FRAMES - 1)
            zeros = [0.0] * SH_FRAMES
            out0_l, _ = h.run_block(impulse, impulse)
            out1_l, _ = h.run_block(zeros, zeros)
            all_l = out0_l + out1_l
            # Tap 1 at d=10 samples should be 0.5 * 0.8 = 0.4
            self.assertAlmostEqual(all_l[10], 0.4, places=4)
            # Tap 2 at d=20.5 samples splits across samples 20 and 21 (0.3 * 0.8 * 0.5 = 0.12 each)
            self.assertAlmostEqual(all_l[20], 0.12, places=3)
            self.assertAlmostEqual(all_l[21], 0.12, places=3)

    def test_state_budget_and_sdram_budget_enforcement(self) -> None:
        """Verify exceeding the 188-byte instance[2] limit or 529,200-byte SDRAM cap raises ValueError."""
        # 1. Exceed 188-byte (47-word) instance[2] state limit with a 25-stage stereo SVF cascade
        too_big_state_dsp = """
        declare name "TooBigSt";
        declare zd2_filename "XXBIGST";
        declare zd2_id "070011d0";
        import("stdfaust.lib");
        process(x) = x : seq(i, 25, fi.svf_lp(1000.0, 0.707));
        """
        with self.assertRaises(FaustMemoryLimitError) as ctx_st:
            self._compile_dsp(too_big_state_dsp)
        self.assertIn("188-byte", str(ctx_st.exception))

        # 2. Exceed 529,200-byte SDRAM safety cap
        too_big_sdram_dsp = """
        declare name "TooBigSd";
        declare zd2_filename "XXBIGSD";
        declare zd2_id "070011d1";
        import("stdfaust.lib");
        process(x) = de.delay(262144, 100.0, x);
        """
        with self.assertRaises(FaustMemoryLimitError) as ctx_sd:
            self._compile_dsp(too_big_sdram_dsp)
        self.assertIn("529,200-byte", str(ctx_sd.exception))

    def test_static_memory_analysis_across_all_repo_dsp_effects(self) -> None:
        """
        Run `analyze_faust_memory` across every `.dsp` effect in `effects/` and
        verify all memory budgets and per-function invocation metrics.
        """
        dsp_files = sorted((ROOT / "effects").glob("*/*.dsp"))
        self.assertGreaterEqual(len(dsp_files), 10)
        for dsp_path in dsp_files:
            with self.subTest(dsp=dsp_path.name):
                report = analyze_faust_memory(
                    dsp_path.read_text(encoding="utf-8"),
                    search_dirs=[dsp_path.parent, ROOT / "effects", ROOT],
                )
                self.assertIsInstance(report, FaustMemoryReport)
                self.assertLessEqual(report.state_bytes, 188)
                self.assertLessEqual(report.physical_sdram_bytes, 529200)
                self.assertLessEqual(report.logical_sdram_bytes, 529200)
                self.assertLessEqual(report.delay_line_count, 32)
                self.assertLessEqual(report.sample_temp_count, 512)
                self.assertIn("process", report.function_invocations)

    def test_static_analysis_breaks_on_state_overflow_across_function_invocations(self) -> None:
        """
        Verify static analysis breaks when a small stateful function (`voice_filt`,
        4 state words = 16 B per call) is invoked enough times across nested
        functions (`bank` x 2 stereo channels = 14 invocations = 224 B > 188 B).
        """
        dsp = """
        declare name "StateOvf";
        declare zd2_filename "XXSTOVF";
        declare zd2_id "070011f0";
        import("stdfaust.lib");

        voice_filt(x) = fi.svf_lp(1200.0, 0.707, fi.svf_hp(120.0, 0.707, x));
        bank(x) = x : seq(i, 7, voice_filt);
        process(xL, xR) = bank(xL), bank(xR);
        """
        with self.assertRaises(FaustMemoryLimitError) as ctx:
            analyze_faust_memory(dsp, search_dirs=[ROOT / "effects"])
        msg = str(ctx.exception)
        self.assertIn("188-byte", msg)
        self.assertIn("voice_filt", msg)
        self.assertIn("per-function invocation memory breakdown", msg)

    def test_static_analysis_breaks_on_cumulative_sdram_across_function_invocations(self) -> None:
        """
        Verify static analysis breaks when multiple function invocations each
        request a valid delay buffer (16,384 floats = 64 KB each) that
        cumulatively exceeds the 529,200-byte SDRAM cap across invocations,
        as well as symbolic `max_len`, `fi.fb_fcomb`, and `@` large delays.
        """
        # 1. Cumulative SDRAM overflow across 10 invocations of a 16,384-sample delay function
        dsp_multi_fn = """
        declare name "SdramOvf";
        declare zd2_filename "XXSDOVF";
        declare zd2_id "070011f1";
        import("stdfaust.lib");

        tap_stage(i, x) = de.fdelay(16384, 100.0 + i * 10.0, x * 0.9 + i * 0.001);
        half_chain(x) = x : seq(i, 5, tap_stage(i));
        process(xL, xR) = half_chain(xL), half_chain(xR);
        """
        with self.assertRaises(FaustMemoryLimitError) as ctx1:
            analyze_faust_memory(dsp_multi_fn, search_dirs=[ROOT / "effects"])
        self.assertIn("529,200-byte", str(ctx1.exception))
        self.assertIn("tap_stage", str(ctx1.exception))

        # 2. Symbolic max_len passed via function argument (`ba.sec2samp(4.0)` = 176,400 samples = 705,600 B > 529,200 B)
        dsp_symbolic_maxlen = """
        declare name "SymMaxL";
        declare zd2_filename "XXSYML";
        declare zd2_id "070011f2";
        import("stdfaust.lib");

        MAX_S = ba.sec2samp(4.0);
        my_del(sz, x) = de.delay(sz, 100.0, x);
        process(x) = my_del(MAX_S, x);
        """
        with self.assertRaises(FaustMemoryLimitError) as ctx2:
            analyze_faust_memory(dsp_symbolic_maxlen, search_dirs=[ROOT / "effects"])
        self.assertIn("529,200-byte", str(ctx2.exception))

        # 3. `fi.fb_fcomb` and `@` operator with excessive delay length
        dsp_fcomb_ovf = """
        declare name "FcombOvf";
        declare zd2_filename "XXFCOVF";
        declare zd2_id "070011f3";
        import("stdfaust.lib");
        process(x) = fi.fb_fcomb(200000, 100.0, 1.0, 0.5, x);
        """
        with self.assertRaises(FaustMemoryLimitError):
            analyze_faust_memory(dsp_fcomb_ovf, search_dirs=[ROOT / "effects"])

        dsp_at_ovf = """
        declare name "AtOvf";
        declare zd2_filename "XXATOVF";
        declare zd2_id "070011f4";
        import("stdfaust.lib");
        process(x) = x @ 150000;
        """
        with self.assertRaises(FaustMemoryLimitError):
            analyze_faust_memory(dsp_at_ovf, search_dirs=[ROOT / "effects"])

    def test_static_analysis_breaks_when_delay_tap_exceeds_buffer_capacity(self) -> None:
        """
        Verify static interval analysis catches delay taps `d` that exceed the
        declared `max_len` buffer capacity — both for constant taps and for
        knob-driven taps whose default value fits but whose maximum knob position
        overflows the buffer.
        """
        # 1. Constant tap (5000 samples) exceeding `zoom.lib` `zm_delay` (4096-sample buffer)
        dsp_const_overflow = """
        declare name "TapOvf1";
        declare zd2_filename "XXTPOV1";
        declare zd2_id "070011f5";
        import("stdfaust.lib");
        import("zoom.lib");
        process(x) = zm_delay(5000.0, x);
        """
        with self.assertRaises(FaustMemoryLimitError) as ctx1:
            analyze_faust_memory(dsp_const_overflow, search_dirs=[ROOT / "effects"])
        self.assertIn("overflowing the allocated 4096-sample delay buffer", str(ctx1.exception))

        # 2. Knob-driven tap: default 20 ms (882 samples <= 4096) fits, but max 150 ms (6615 samples > 4096) overflows!
        dsp_knob_overflow = """
        declare name "TapOvf2";
        declare zd2_filename "XXTPOV2";
        declare zd2_id "070011f6";
        import("stdfaust.lib");
        import("zoom.lib");

        time_ms = hslider("Time[1]", 20, 1, 150, 1) : si.smoo;
        echo_voice(x) = zm_fdelay1(ba.ms2samp(time_ms), x);
        process(x) = echo_voice(x);
        """
        with self.assertRaises(FaustMemoryLimitError) as ctx2:
            analyze_faust_memory(dsp_knob_overflow, search_dirs=[ROOT / "effects"])
        self.assertIn("6615.0 samples", str(ctx2.exception))
        self.assertIn("4096-sample", str(ctx2.exception))

    def test_static_analysis_breaks_on_excessive_delay_lines_or_stack_temps_or_recursion(self) -> None:
        """
        Verify static analysis breaks when function invocations exceed:
          1. The 32-line parallel SDRAM delay line L1D/L2 cache-set limit
          2. The 512-variable per-sample local stack temporary limit (even with 0 state)
          3. Unbounded cyclic function recursion (`a(x) = b(x); b(x) = a(x);`)
        """
        # 1. 34 distinct 1024-sample delay lines (only ~141 KB total SDRAM, but > 32 parallel cache lines)
        dsp_too_many_lines = """
        declare name "LineOvf";
        declare zd2_filename "XXLNOVF";
        declare zd2_id "070011f7";
        import("stdfaust.lib");

        d_tap(i, x) = de.delay(1024, 10.0 + i, x * (1.0 + i * 0.01));
        process(x) = sum(i, 18, d_tap(i, x));
        """
        with self.assertRaises(FaustMemoryLimitError) as ctx_lines:
            analyze_faust_memory(dsp_too_many_lines, search_dirs=[ROOT / "effects"])
        self.assertIn("32-line", str(ctx_lines.exception))

        # 2. Stateless function tree generating > 512 per-sample local stack temporaries (0 state words, 0 SDRAM!)
        dsp_temp_overflow = """
        declare name "TempOvf";
        declare zd2_filename "XXTMPOVF";
        declare zd2_id "070011f8";
        import("stdfaust.lib");

        nl_step(x) = ma.tanh(x * 1.4 + 0.05) - 0.1 * ma.sin(x * 2.5)
            with {
                a = x * 0.9 + 0.01;
                b = a * a - 0.02;
                c = b * 0.5 + a;
            };
        macro_stage(x) = x : seq(i, 12, nl_step);
        process(x) = x : seq(j, 10, macro_stage);
        """
        with self.assertRaises(FaustMemoryLimitError) as ctx_temps:
            analyze_faust_memory(dsp_temp_overflow, search_dirs=[ROOT / "effects"])
        self.assertIn("512-variable", str(ctx_temps.exception))
        self.assertIn("nl_step", str(ctx_temps.exception))

        # 3. Direct cyclic function call recursion without `~` feedback operator
        dsp_cycle = """
        declare name "CycleOvf";
        declare zd2_filename "XXCYCOVF";
        declare zd2_id "070011f9";
        import("stdfaust.lib");

        ping(x) = pong(x * 0.5);
        pong(x) = ping(x + 0.1);
        process(x) = ping(x);
        """
        with self.assertRaises(FaustMemoryLimitError) as ctx_cyc:
            analyze_faust_memory(dsp_cycle, search_dirs=[ROOT / "effects"])
        self.assertIn("cycle", str(ctx_cyc.exception).lower())

    def test_error_on_missing_process_or_missing_import(self) -> None:
        with self.assertRaises(ValueError):
            self._compile_dsp('declare name "NoProc"; x = 1.0;')
        with self.assertRaises(ValueError):
            self._compile_dsp('import("nonexistent_library_12345.lib"); process = _;')

    def test_stdfaust_library_primitives_compile_and_execute(self) -> None:
        """
        Verify a broad set of `stdfaust.lib` filters, oscillators, noises,
        analyzers, dynamics, and basic primitives compile to valid C and run
        cleanly in the native harness.
        """
        dsp_filters_and_dynamics = """
        declare name "StdPrims1";
        declare zd2_filename "XXSTDP1";
        declare zd2_id "070011d2";
        import("stdfaust.lib");

        sel = hslider("Sel[1][values:A|B|C|D]", 1, 0, 3, 1);

        chain(x) = x
            : fi.lowpass(2, 2500.0)
            : fi.highpass(2, 80.0)
            : fi.low_shelf(3.0, 200.0)
            : fi.high_shelf(-2.0, 4000.0)
            : fi.peak_eq(2.0, 1000.0, 300.0)
            : fi.tf1(0.5, 0.2, -0.1)
            : fi.tf2(0.8, 0.1, 0.05, -0.2, 0.05)
            : co.limiter_1176_R4_mono
            : ef.gate_mono(-45.0, 0.002, 0.02, 0.05)
            : ef.slew(0.2)
            : ef.cubicnl(0.3, 0.0)
            : *(ba.selectn(4, sel, 0.5, 0.8, 1.0, 1.2));

        process(xL, xR) = chain(xL), chain(xR);
        """
        kernel_c, state_bytes, _, manifest, _ = self._compile_dsp(dsp_filters_and_dynamics)
        self.assertLessEqual(state_bytes, 188)
        with KernelHarness(kernel_c, manifest) as h:
            sig = [0.25 * math.sin(2.0 * math.pi * i / 16.0) for i in range(SH_FRAMES)]
            out_l, out_r = h.run_block(sig, sig)
            for s in out_l + out_r:
                self.assertTrue(math.isfinite(s))
            self.assertGreater(sum(s * s for s in out_l), 1e-4)

        dsp_osc_noise_delays = """
        declare name "StdPrims2";
        declare zd2_filename "XXSTDP2";
        declare zd2_id "070011d3";
        import("stdfaust.lib");

        lfo = 0.2 * os.osc(2.0) + 0.2 * os.osccos(3.0) + 0.2 * os.triangle(1.5)
            + 0.2 * os.sawtooth(1.0) + 0.2 * os.square(4.0);
        n_sig = 0.01 * (no.noise + no.pink_noise + no.lfnoise0(10.0));
        comb_stage(x) = fi.allpass_comb(1024, 32.0, 0.4, fi.fb_fcomb(1024, 48.0, 0.8, 0.3, x));

        process(x) = comb_stage(ba.downSample(22050.0, x) + n_sig * (1.0 + lfo));
        """
        kernel_c2, state_bytes2, _, manifest2, _ = self._compile_dsp(dsp_osc_noise_delays)
        self.assertLessEqual(state_bytes2, 188)
        with KernelHarness(kernel_c2, manifest2) as h:
            sig = [0.2 * math.cos(2.0 * math.pi * i / 16.0) for i in range(SH_FRAMES)]
            out_l, out_r = h.run_block(sig, sig)
            for s in out_l + out_r:
                self.assertTrue(math.isfinite(s))
            self.assertGreater(sum(s * s for s in out_l), 1e-4)


class TestFaustCBackendConverter(unittest.TestCase):
    """Unit tests for `tools/faust2zd2_pkg/c_backend.py` (Mode 1 `faust -lang c` converter)."""

    def test_convert_faust_c_to_zd2_kernel_and_execute_natively(self) -> None:
        dsp_text = """
        declare name "FaustCMod";
        declare zd2_filename "XXFSTC1";
        declare zd2_id "070011e0";
        import("stdfaust.lib");
        drive = hslider("Drive[1]", 50, 0, 100, 1);
        level = hslider("Level[2][curve:stock_vol]", 80, 0, 100, 1);
        process = _;
        """
        # Authentic `faust -lang c -cn mydsp` output structure
        faust_c_output = """
        typedef struct {
            int fSampleRate;
            FAUSTFLOAT fHslider0;
            FAUSTFLOAT fHslider1;
            float fRec0[2];
        } mydsp;

        void buildUserInterfacemydsp(mydsp* dsp, UIGlue* ui_interface) {
            ui_interface->addHorizontalSlider(ui_interface->uiInterface, "Drive", &dsp->fHslider0, 50.0f, 0.0f, 100.0f, 1.0f);
            ui_interface->addHorizontalSlider(ui_interface->uiInterface, "Level", &dsp->fHslider1, 80.0f, 0.0f, 100.0f, 1.0f);
        }

        void computemydsp(mydsp* dsp, int count, FAUSTFLOAT** inputs, FAUSTFLOAT** outputs) {
            FAUSTFLOAT* input0 = inputs[0];
            FAUSTFLOAT* input1 = inputs[1];
            FAUSTFLOAT* output0 = outputs[0];
            FAUSTFLOAT* output1 = outputs[1];
            double fSlow0 = (1.0 + 0.04 * (double)dsp->fHslider0);
            double fSlow1 = (double)dsp->fHslider1 / (1.0 + 0.2 * fSlow0);
            for (int i0 = 0; i0 < count; i0 = i0 + 1) {
                double fTemp0 = (double)input0[i0];
                dsp->fRec0[0] = 0.5 * dsp->fRec0[1] + 0.5 * tanh(fTemp0 * fSlow0);
                output0[i0] = (FAUSTFLOAT)(dsp->fRec0[0] * fSlow1);
                output1[i0] = (FAUSTFLOAT)(dsp->fRec0[0] * fSlow1);
                dsp->fRec0[1] = dsp->fRec0[0];
            }
        }
        """
        meta, dsp_params = parse_faust_dsp_metadata(dsp_text)
        kernel_c, state_bytes, params = convert_faust_c_to_zd2_kernel(
            faust_c_output, meta, dsp_params
        )

        # Verify all double types, "/" divisions, tanh(), and array states were rewritten
        self.assertNotIn("double", kernel_c)
        self.assertIn("sh_fdiv(", kernel_c)
        self.assertIn("sh_tanhf(", kernel_c)
        self.assertIn("st_fRec0_0", kernel_c)
        self.assertIn("st_fRec0_1", kernel_c)
        self.assertEqual(state_bytes, 16)

        manifest = build_manifest_dict(meta, params, "faustcmod.c", state_bytes)
        with KernelHarness(kernel_c, manifest) as h:
            sig = [0.25 * math.sin(2.0 * math.pi * i / 16.0) for i in range(SH_FRAMES)]
            out_l, out_r = h.run_block(sig, sig)
            self.assertEqual(out_l, out_r)
            for s in out_l:
                self.assertTrue(math.isfinite(s))
            self.assertGreater(sum(s * s for s in out_l), 1e-3)


if __name__ == "__main__":
    unittest.main()
