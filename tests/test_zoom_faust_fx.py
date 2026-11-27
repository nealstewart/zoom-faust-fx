"""Unit and integration tests for the zoom_faust_fx package and CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from zoom_faust_fx import benchmark, builder, faust_bridge, find_stomphacks_dir, input_loader, safety


def _has_stomphacks() -> bool:
    try:
        find_stomphacks_dir()
        return True
    except FileNotFoundError:
        return False


class TestInputLoader(unittest.TestCase):
    def test_load_dsp_and_manifest_override(self) -> None:
        if not _has_stomphacks():
            self.skipTest("stomphacks checkout not found")
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            dsp = tmp_p / "my_gain.dsp"
            dsp.write_text(
                'declare name "MyGain";\nprocess = _, _;\n',
                encoding="utf-8",
            )
            override = tmp_p / "override.json"
            override.write_text(
                json.dumps({"name": "OverGain", "filename": "XXOVERGN"}),
                encoding="utf-8",
            )
            spec = input_loader.load(
                dsp,
                out_dir=tmp_p / "out",
                manifest_path=override,
            )
            self.assertEqual(spec.source_type, "dsp")
            self.assertEqual(spec.dsp_path, dsp.resolve())
            self.assertEqual(spec.manifest_override["filename"], "XXOVERGN")

    def test_load_effect_directory(self) -> None:
        if not _has_stomphacks():
            self.skipTest("stomphacks checkout not found")
        spec = input_loader.load(ROOT / "effects" / "svfflt")
        self.assertEqual(spec.source_type, "directory")
        self.assertIsNotNone(spec.dsp_path)
        self.assertEqual(spec.output_dir, (ROOT / "effects" / "svfflt" / "build").resolve())

    def test_missing_target_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            input_loader.load(ROOT / "effects" / "does_not_exist_12345")


class TestBenchmarkParser(unittest.TestCase):
    def test_vliw_sploop_and_stall_parser(self) -> None:
        sample_dis = """
00007800            Fx_SFX_Sample:
00007800   02904264           LDW.D1T1      *+A4[2],A5
00007804   00a398aa           MVK.S2        0x4731,B1
00007808       4c6e           NOP           3
0000780a   50148120    [!B1]  BNOP.S1       $C$L1 (PC+40 = 0x00007828),4
00007810       0c67           SPLOOPD       1
00007814       0c04           STW.D1T1      A0,*A4++[1]
00007816       1c05 ||        STW.D2T2      B0,*B4++[1]
00007818   08034000           SPKERNEL      1,0
0000781c            $C$L1:
0000781c   0084b362           BNOP.S2X      A1,5
"""
        with tempfile.TemporaryDirectory() as tmp:
            dis_p = Path(tmp) / "sample.audio.dis"
            dis_p.write_text(sample_dis, encoding="utf-8")
            m = benchmark.analyze_dis_file(dis_p, eff_name="Sample")
            self.assertEqual(m.loop_type, "SPLOOP(II=1)")
            self.assertEqual(m.trip_count, 16)
            self.assertGreater(m.vliw_cycles, 0)
            self.assertGreaterEqual(m.dspload_estimate, 2.0)

    def test_sdram_detection_increases_stall_cycles(self) -> None:
        sdram_dis = """
Fx_SFX_Delay:
    LDW.D1T1 *+A4[3],A5
    NOP 4
    SPLOOP 2
    LDW.D1T1 *A5++,A6
    SPKERNEL 1,0
"""
        with tempfile.TemporaryDirectory() as tmp:
            dis_p = Path(tmp) / "delay.audio.dis"
            dis_p.write_text(sdram_dis, encoding="utf-8")
            m = benchmark.analyze_dis_file(dis_p, eff_name="Delay")
            self.assertTrue(m.uses_sdram)
            self.assertGreater(m.sdram_stall_cycles, 0)


class TestCLISmoke(unittest.TestCase):
    def test_cli_help(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "zoom_faust_fx.cli", "--help"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("zoom-faust-fx", proc.stdout)
        self.assertIn("build", proc.stdout)
        self.assertIn("benchmark", proc.stdout)
        self.assertIn("check", proc.stdout)


if __name__ == "__main__":
    unittest.main()
