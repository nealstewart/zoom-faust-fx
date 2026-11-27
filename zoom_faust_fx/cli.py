"""Thin CLI entrypoint for zoom-faust-fx."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import __version__, benchmark, builder, find_stomphacks_dir, input_loader, safety


def _build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="zoom-faust-fx",
        description="Compile Faust DSP or C effects into Zoom MS Plus .ZD2/.ZIC modules.",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument(
        "--stomphacks-dir",
        type=Path,
        default=None,
        help="Path to checked-out stomphacks repository (overrides STOMPHACKS_DIR / auto-detect)",
    )

    sub = ap.add_subparsers(dest="command", required=True)

    # build
    p_build = sub.add_parser("build", help="Build a .dsp file or effect directory into .ZD2/.ZIC")
    p_build.add_argument("target", type=Path, help="Path to .dsp file or effect directory")
    p_build.add_argument("--out", type=Path, default=None, help="Override output directory")
    p_build.add_argument("--manifest", type=Path, default=None, help="Override or supply manifest.json")
    p_build.add_argument("--icon", type=Path, default=None, help="Optional PNG/ZIC icon override")
    p_build.add_argument("--no-strip", action="store_true", help="Preserve DWARF/symtab in ELF blob")
    p_build.add_argument("--no-benchmark", action="store_true", help="Skip C674x VLIW benchmark step")
    p_build.add_argument("--json", action="store_true", help="Emit machine-readable JSON summary")

    # check
    p_check = sub.add_parser("check", help="Run offline round-trip, envelope, catalog, and REPORT.md checks")
    p_check.add_argument("zd2", type=Path, help="Path to .ZD2 file")
    p_check.add_argument("--json", action="store_true", help="Emit machine-readable JSON summary")

    # benchmark
    p_bench = sub.add_parser("benchmark", help="Run C674x VLIW & host benchmark on .dsp, .dis, or dir")
    p_bench.add_argument("target", type=Path, help="Path to .dsp, .audio.dis, effect dir, or effects/ dir")
    p_bench.add_argument("--json", action="store_true", help="Emit machine-readable JSON summary")

    # install
    p_inst = sub.add_parser("install", help="Safely install a .ZD2 effect via tools-pedal/pedal_diy.py")
    p_inst.add_argument("zd2", type=Path, help="Path to .ZD2 file")
    p_inst.add_argument("--dry-run", action="store_true", help="Run pre-flight checks without touching pedal")
    p_inst.add_argument("--free-margin-kb", type=int, default=None, help="Override free space margin (KB)")

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = _build_parser()
    args = ap.parse_args(argv)

    if args.command == "build":
        spec = input_loader.load(
            args.target,
            stomphacks_dir=args.stomphacks_dir,
            out_dir=args.out,
            manifest_path=args.manifest,
            icon_path=args.icon,
            no_strip=args.no_strip,
        )
        result = None
        for ev in builder.build_steps(spec, run_benchmark=not args.no_benchmark):
            if not args.json:
                print(f"[{ev.stage}] {ev.status}: {ev.message}")
            if ev.result is not None:
                result = ev.result
        if result is None:
            return 1
        if args.json:
            print(json.dumps({
                "zd2": str(result.zd2_path) if result.zd2_path else None,
                "zic": str(result.zic_path) if result.zic_path else None,
                "report": str(result.report_path) if result.report_path else None,
                "manifest": str(result.manifest_path),
                "build_dir": str(result.build_dir),
                "report_passed": result.report_passed,
                "report_summary": result.report_summary,
                "dspload": result.dspload,
                "gain_staging": result.gain_staging.to_dict() if result.gain_staging else None,
            }, indent=2))
        return 0 if result.report_passed else 1

    if args.command == "check":
        res = safety.check_zd2(args.zd2, stomphacks_dir=args.stomphacks_dir)
        if args.json:
            print(json.dumps({
                "passed": res.passed,
                "summary": res.summary,
                "checks": res.checks,
                "errors": res.errors,
            }, indent=2))
        else:
            print(res.summary)
            for c in res.checks:
                print(f"  OK   {c}")
            for e in res.errors:
                print(f"  FAIL {e}")
        return 0 if res.passed else 1

    if args.command == "benchmark":
        results = []
        for ev in benchmark.analyze_steps(args.target, stomphacks_dir=args.stomphacks_dir):
            if not args.json and ev.status in ("running", "progress", "warning"):
                print(f"[{ev.stage}] {ev.message}")
            if ev.results:
                results = ev.results
        if args.json:
            print(json.dumps([r.to_dict() for r in results], indent=2))
        elif results and len(results) == 1:
            r = results[0]
            gs_part = ""
            if r.gain_staging is not None:
                g = r.gain_staging
                note_part = f" ({', '.join(g.notes)})" if g.notes else ""
                gs_part = (
                    f" | gain={g.gain_db:+.1f} dB pk={g.peak_out:.2f} "
                    f"hot_pk={g.hot_peak_out:.2f} dc={g.dc_offset:.4f} "
                    f"byp={g.bypass_gain_db:+.2f} dB [{g.status}]{note_part}"
                )
            print(
                f"Summary: {r.name} | {r.loop_type} | VLIW={r.vliw_cycles} cyc | "
                f"EstTotal={r.total_estimated_cycles} cyc | dspload={r.dspload_estimate:.2f} ({r.dspload_pct:.1f}%)"
                + (f" | host={r.host_ns_per_block:.1f} ns/blk" if r.host_ns_per_block is not None else "")
                + gs_part
            )
        elif results and len(results) > 1:
            gs_rows = [r for r in results if r.gain_staging is not None]
            if gs_rows:
                print("\nGain Staging Summary (0.24 RMS / -12.4 dBFS guitar bus, default knobs):")
                print(
                    f"  {'Effect':<12} {'Gain(dB)':>8} {'PkOut':>6} {'HotPk':>6} "
                    f"{'Clip%':>6} {'DC':>7} {'L/R(dB)':>8} {'Byp(dB)':>8} {'Status':<6} Notes"
                )
                for r in gs_rows:
                    g = r.gain_staging
                    assert g is not None
                    print(
                        f"  {r.name:<12} {g.gain_db:+8.2f} {g.peak_out:6.3f} {g.hot_peak_out:6.3f} "
                        f"{g.dac_clip_pct:6.2f} {g.dc_offset:7.4f} {g.lr_balance_db:+8.2f} "
                        f"{g.bypass_gain_db:+8.2f} {g.status:<6} {', '.join(g.notes)}"
                    )
        return 0

    if args.command == "install":
        sh_dir = find_stomphacks_dir(args.stomphacks_dir)
        pedal_diy = sh_dir / "tools-pedal" / "pedal_diy.py"
        cmd = [sys.executable, str(pedal_diy)]
        if args.free_margin_kb is not None:
            cmd.extend(["--free-margin-kb", str(args.free_margin_kb)])
        cmd.extend(["install", str(args.zd2.resolve())])
        if args.dry_run:
            cmd.append("--dry-run")
        proc = subprocess.run(cmd)
        return proc.returncode

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
