"""
tests/test_faust_e2e.py — End-to-end local integration tests for the complete
FAUST (`.dsp` and `--from-c`) to Zoom MS Plus (`.ZD2` + `.ZIC`) compilation
pipeline (`tools/faust2zd2.py --build` -> `tools/zd2_make_effect.py` -> TI C6000
`cl6x`/`asm6x`/`lnk6x`/`dis6x`/`ofd6x` -> `zoom-zt2` container & CRC verification
-> native audio execution).
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))
if str(ROOT / "zoom-zt2") not in sys.path:
    sys.path.insert(0, str(ROOT / "zoom-zt2"))

import zoomzt2
import zd2_roundtrip
from tests.c_harness import KernelHarness, SH_FRAMES


def _extract_fn_disassembly(dis_text: str, fn_name: str) -> str:
    """Extract the disassembly block for `fn_name` from a TI `dis6x` listing."""
    m = re.search(rf"^\s*{re.escape(fn_name)}:\s*$", dis_text, re.M)
    if not m:
        return ""
    start = m.end()
    # Read until the next top-level symbol label or section header
    next_m = re.search(r"^(?:[A-Za-z_][A-Za-z0-9_]*:|TEXT Section)", dis_text[start:], re.M)
    end = (start + next_m.start()) if next_m else len(dis_text)
    return dis_text[start:end]


class TestFaustEndToEndIntegration(unittest.TestCase):
    """End-to-end local integration tests running `tools/faust2zd2.py --build`."""

    def _assert_zd2_and_report_valid(
        self,
        out_dir: Path,
        expected_filename: str,
        expected_id_hex: str,
        expected_name: str,
    ) -> dict:
        manifest_path = out_dir / "manifest.json"
        self.assertTrue(manifest_path.is_file(), "manifest.json was not generated")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        build_dir = out_dir / "build"
        zd2_path = build_dir / f"{expected_filename}.ZD2"
        zic_path = build_dir / f"{expected_filename}.ZIC"
        report_path = build_dir / "REPORT.md"
        sh_params_path = build_dir / "sh_params.h"
        dis_path = build_dir / f"{expected_filename}.text.dis"

        self.assertTrue(zd2_path.is_file(), f"Missing {zd2_path}")
        self.assertTrue(zic_path.is_file(), f"Missing {zic_path}")
        self.assertTrue(report_path.is_file(), f"Missing {report_path}")
        self.assertTrue(sh_params_path.is_file(), f"Missing {sh_params_path}")
        self.assertTrue(dis_path.is_file(), f"Missing {dis_path}")

        # 1. Verify REPORT.md has zero failed checks and all expected verification lines
        report_text = report_path.read_text(encoding="utf-8")
        self.assertNotIn("FAIL", report_text, f"REPORT.md contains a failure:\n{report_text}")
        self.assertIn("all ABS32/ABS_L16/ABS_H16", report_text)
        self.assertIn("structure invariants OK", report_text)
        self.assertIn("envelope invariants OK", report_text)

        # 2. Audit TI C674x disassembly of the audio kernel (`Fx_SFX_<Sym>`)
        sym = re.sub(r"[^A-Za-z0-9]", "", expected_name)
        fn_name = f"Fx_SFX_{sym}"
        dis_text = dis_path.read_text(encoding="utf-8")
        fn_dis = _extract_fn_disassembly(dis_text, fn_name)
        self.assertTrue(fn_dis.strip(), f"Could not find {fn_name} in {dis_path}")
        self.assertIsNone(
            re.search(r"\bCALLP?\b", fn_dis),
            f"Audio kernel {fn_name} emitted a forbidden CALL instruction:\n{fn_dis}",
        )
        self.assertIsNone(
            re.search(r"\bB14\b", fn_dis),
            f"Audio kernel {fn_name} emitted a forbidden B14 DP reference:\n{fn_dis}",
        )
        self.assertIsNone(
            re.search(r"\b(?:MPYSPDP|ADDDP|SUBDP|DPSP|SPDP)\b", fn_dis),
            f"Audio kernel {fn_name} emitted a 64-bit double-precision instruction:\n{fn_dis}",
        )

        # 3. Parse the synthesized .ZD2 container with zoom-zt2 and verify identity, roundtrip & CRC32
        raw_zd2 = zd2_path.read_bytes()
        self.assertTrue(zd2_roundtrip.check_crc(raw_zd2), "ZD2 container CRC32 check failed")
        parsed = zoomzt2.ZD2.parse(raw_zd2)
        self.assertEqual(parsed.id, int(expected_id_hex, 16))
        self.assertEqual(parsed.name.strip("\x00"), expected_name)
        self.assertEqual(zoomzt2.ZD2.build(parsed), raw_zd2, "ZD2 container roundtrip mismatch")

        return manifest

    def test_e2e_subset_compiler_filter_saturator_footswitch(self) -> None:
        """
        E2E Test 1: Compile a filter + tanh saturator `.dsp` effect (with `log`
        scale, `stock_vol` curve, discrete selector `values`, and momentary
        `zd2_footswitch`) through `faust2zd2.py --build`, verify the `.ZD2`
        binary and C674x disassembly, and execute the generated C kernel natively.
        """
        dsp_code = """
        declare name "E2EFlt";
        declare zd2_filename "XXE2EFL";
        declare zd2_id "07001210";
        declare version "1.00";
        declare zd2_display "E2E FLT";
        declare zd2_badge "EF";
        declare description "End-to-end test SVF filter + tanh saturator.";
        declare zd2_dspload "12";
        declare zd2_footswitch "true";

        import("stdfaust.lib");

        drive = hslider("Drive[1]", 40, 0, 100, 1) * 0.01 : si.smoo;
        freq  = hslider("Freq[2][unit:Hz][scale:log]", 1200, 120, 12000, 1) : si.smoo;
        mode  = hslider("Mode[3][values:LP|HP]", 0, 0, 1, 1);
        level = hslider("Level[4][curve:stock_vol]", 80, 0, 100, 1) : si.smoo;

        pre_gain = 1.0 + drive * 8.0;
        trim     = 0.25 / ma.tanh(0.25 * pre_gain);

        sat(x) = ma.tanh(x * pre_gain) * trim;
        filt(x) = ba.if(mode < 0.5, fi.svf_lp(freq, 0.7071, x), fi.svf_hp(freq, 0.7071, x));

        process(x) = x : sat : filt : fi.dcblocker : *(level);
        """
        with tempfile.TemporaryDirectory(prefix="stomphacks_e2e_flt_") as td:
            td_path = Path(td)
            dsp_path = td_path / "e2eflt.dsp"
            dsp_path.write_text(dsp_code, encoding="utf-8")

            res = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "faust2zd2.py"),
                    str(dsp_path),
                    "--out-dir",
                    str(td_path),
                    "--build",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, f"faust2zd2 --build failed:\n{res.stderr}\n{res.stdout}")

            manifest = self._assert_zd2_and_report_valid(
                td_path,
                expected_filename="XXE2EFL",
                expected_id_hex="07001210",
                expected_name="E2EFlt",
            )

            kernel_c = (td_path / "e2eflt.c").read_text(encoding="utf-8")
            with KernelHarness(kernel_c, manifest) as h:
                sig = [0.25 * math.sin(2.0 * math.pi * 400.0 * i / 44100.0) for i in range(SH_FRAMES)]
                out_l, out_r = h.run_block(sig, sig)
                self.assertEqual(out_l, out_r)
                self.assertGreater(sum(s * s for s in out_l), 1e-4)

    def test_e2e_subset_compiler_sdram_multitap_delay_and_zoom_lib(self) -> None:
        """
        E2E Test 2: Compile a stereo multi-tap SDRAM delay + recursive feedback
        effect importing `zoom.lib` through `faust2zd2.py --build`, verify
        common-delay-line sharing, C674x disassembly, `.ZD2` container CRC, and
        exact sample delay timing in native C execution.
        """
        dsp_code = """
        declare name "E2EDly";
        declare zd2_filename "XXE2EDL";
        declare zd2_id "07001220";
        declare version "1.00";
        declare zd2_display "E2E DLY";
        declare zd2_badge "ED";
        declare description "End-to-end test SDRAM multi-tap delay + feedback.";
        declare zd2_dspload "15";

        import("stdfaust.lib");
        import("zoom.lib");

        fdbk  = hslider("Fdbk[1]", 25, 0, 80, 1) * 0.01 : si.smoo;
        mix   = hslider("Mix[2]", 50, 0, 100, 1) * 0.01 : si.smoo;
        level = hslider("Level[3][curve:stock_vol]", 80, 0, 100, 1) : si.smoo;

        tank_step(prev_w, x_in) = x_in + zm_soft_clip(zm_allpass1(-0.35, fb_tap) * fdbk)
            with {
                fb_tap = zm_delay(16.0, prev_w);
            };
        tank(x) = (tank_step(x) ~ _);

        chan(x) = (x + mix * (0.6 * tap1 + 0.4 * tap2)) * level
            with {
                w    = tank(x);
                tap1 = zm_delay(8.0, w);
                tap2 = zm_fdelay1(12.0, w);
            };

        process(xL, xR) = chan(xL), chan(xR);
        """
        with tempfile.TemporaryDirectory(prefix="stomphacks_e2e_dly_") as td:
            td_path = Path(td)
            dsp_path = td_path / "e2edly.dsp"
            dsp_path.write_text(dsp_code, encoding="utf-8")

            res = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "faust2zd2.py"),
                    str(dsp_path),
                    "--out-dir",
                    str(td_path),
                    "--build",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, f"faust2zd2 --build failed:\n{res.stderr}\n{res.stdout}")

            manifest = self._assert_zd2_and_report_valid(
                td_path,
                expected_filename="XXE2EDL",
                expected_id_hex="07001220",
                expected_name="E2EDly",
            )

            kernel_c = (td_path / "e2edly.c").read_text(encoding="utf-8")
            # tap1 and tap2 on w share a single delay line per channel
            self.assertIn("dline_0 = ((float *)dbaseAddr) + 0u;", kernel_c)
            self.assertIn("dline_1 = ((float *)dbaseAddr) + 4112u;", kernel_c)

            with KernelHarness(kernel_c, manifest) as h:
                h.set_param_norm("Fdbk", 0.0)
                h.set_param_norm("Mix", 1.0)
                impulse = [0.5] + [0.0] * (SH_FRAMES - 1)
                out_l, _ = h.run_block(impulse, impulse)
                # Sample 0 has dry = 0.5; sample 8 has tap1 = 0.6 * 0.5 = 0.3; sample 12 has tap2 = 0.4 * 0.5 = 0.2
                self.assertAlmostEqual(out_l[0], 0.5, places=4)
                self.assertAlmostEqual(out_l[8], 0.3, places=4)
                self.assertAlmostEqual(out_l[12], 0.2, places=4)

    def test_e2e_from_c_external_faust_backend_build(self) -> None:
        """
        E2E Test 3: Run `tools/faust2zd2.py --from-c <faust_c_file> --build`
        end-to-end through `c_backend.py` and `zd2_make_effect.py`, verifying
        that external `faust -lang c` output compiles to a verified `.ZD2` with
        0 CALLs and executes audio properly.
        """
        dsp_code = """
        declare name "E2ECBk";
        declare zd2_filename "XXE2ECB";
        declare zd2_id "07001230";
        declare version "1.00";
        declare zd2_display "E2E CBK";
        declare zd2_badge "EC";
        declare description "End-to-end test from pre-generated faust -lang c.";
        declare zd2_dspload "10";

        import("stdfaust.lib");
        drive = hslider("Drive[1]", 50, 0, 100, 1);
        level = hslider("Level[2][curve:stock_vol]", 80, 0, 100, 1);
        process = _;
        """
        faust_c_code = """
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
            double fSlow0 = (1.0 + 0.05 * (double)dsp->fHslider0);
            double fSlow1 = (double)dsp->fHslider1 / (1.0 + 0.1 * fSlow0);
            for (int i0 = 0; i0 < count; i0 = i0 + 1) {
                double fTemp0 = (double)input0[i0];
                dsp->fRec0[0] = 0.25 * dsp->fRec0[1] + 0.75 * tanh(fTemp0 * fSlow0);
                output0[i0] = (FAUSTFLOAT)(dsp->fRec0[0] * fSlow1);
                output1[i0] = (FAUSTFLOAT)(dsp->fRec0[0] * fSlow1);
                dsp->fRec0[1] = dsp->fRec0[0];
            }
        }
        """
        with tempfile.TemporaryDirectory(prefix="stomphacks_e2e_cbk_") as td:
            td_path = Path(td)
            dsp_path = td_path / "e2ecbk.dsp"
            raw_c_path = td_path / "raw_faust.c"
            dsp_path.write_text(dsp_code, encoding="utf-8")
            raw_c_path.write_text(faust_c_code, encoding="utf-8")

            res = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "faust2zd2.py"),
                    str(dsp_path),
                    "--from-c",
                    str(raw_c_path),
                    "--out-dir",
                    str(td_path),
                    "--build",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(res.returncode, 0, f"faust2zd2 --from-c --build failed:\n{res.stderr}\n{res.stdout}")

            manifest = self._assert_zd2_and_report_valid(
                td_path,
                expected_filename="XXE2ECB",
                expected_id_hex="07001230",
                expected_name="E2ECBk",
            )

            kernel_c = (td_path / "e2ecbk.c").read_text(encoding="utf-8")
            with KernelHarness(kernel_c, manifest) as h:
                sig = [0.25 * math.sin(2.0 * math.pi * i / 16.0) for i in range(SH_FRAMES)]
                out_l, out_r = h.run_block(sig, sig)
                self.assertEqual(out_l, out_r)
                self.assertGreater(sum(s * s for s in out_l), 1e-3)

    def test_e2e_cli_breaks_when_script_exceeds_memory_across_function_invocations(self) -> None:
        """
        E2E Test 4: Verify `tools/faust2zd2.py` CLI performs static memory
        analysis and aborts with a non-zero exit code (writing no `.c` or
        `manifest.json`) when a `.dsp` script attempts to allocate excessive
        memory across its function invocations.
        """
        bad_dsp = """
        declare name "E2EBig";
        declare zd2_filename "XXE2EBG";
        declare zd2_id "07001240";
        import("stdfaust.lib");

        // Each invocation of `comb_unit` allocates a 16,384-float (64 KB) SDRAM line;
        // 6 stages x 2 stereo channels = 12 invocations = 786,432 B (> 529,200 B cap).
        comb_unit(i, x) = de.fdelay(16384, 250.0 + i * 25.0, x * 0.8 + i * 0.001);
        multi_comb(x) = x : seq(i, 6, comb_unit(i));
        process(xL, xR) = multi_comb(xL), multi_comb(xR);
        """
        with tempfile.TemporaryDirectory(prefix="stomphacks_e2e_memfail_") as td:
            td_path = Path(td)
            dsp_path = td_path / "e2ebig.dsp"
            dsp_path.write_text(bad_dsp, encoding="utf-8")

            res = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools" / "faust2zd2.py"),
                    str(dsp_path),
                    "--out-dir",
                    str(td_path),
                ],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(res.returncode, 0, "Expected faust2zd2.py to fail on memory limit breach")
            combined_out = res.stderr + "\n" + res.stdout
            self.assertIn("Static memory analysis failed", combined_out)
            self.assertIn("comb_unit", combined_out)
            self.assertFalse((td_path / "e2ebig.c").exists(), "Kernel .c should not be written on memory failure")
            self.assertFalse((td_path / "manifest.json").exists(), "manifest.json should not be written on failure")


if __name__ == "__main__":
    unittest.main()
