"""C6000 build, ZD2/ZIC packing, benchmark dspload integration, and REPORT.md gate."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from . import benchmark, faust_bridge, safety
from .input_loader import BuildSpec, _deep_merge_manifest


@dataclass(slots=True)
class BuildResult:
    """Final output of a zoom_faust_fx build."""

    zd2_path: Path | None
    zic_path: Path | None
    report_path: Path | None
    manifest_path: Path
    build_dir: Path
    report_passed: bool
    report_summary: str
    dspload: float | None = None
    audio_dis_path: Path | None = None


@dataclass(slots=True)
class BuildEvent:
    """Structured stage event yielded by build_steps()."""

    stage: str
    status: str  # running | completed | skipped | failed
    message: str
    result: BuildResult | None = None


def _run_zd2_make_effect(
    manifest_path: Path,
    stomphacks_dir: Path,
    *,
    effect_dir: Path | None = None,
    icon_path: Path | None = None,
    no_strip: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Invoke stomphacks tools/zd2_make_effect.py with zoom_faust_fx.pixel_icons."""
    env = dict(os.environ)
    env["STOMPHACKS_DIR"] = str(stomphacks_dir)
    pkg_root = Path(__file__).resolve().parent.parent
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(pkg_root) + (os.pathsep + existing_pp if existing_pp else "")

    if icon_path is None:
        cmd = [
            sys.executable,
            "-m",
            "zoom_faust_fx._zd2_runner",
            str(stomphacks_dir),
            str(manifest_path),
            str(effect_dir) if effect_dir is not None else "",
        ]
    else:
        cmd = [sys.executable, str(stomphacks_dir / "tools" / "zd2_make_effect.py"), str(manifest_path)]

    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def _sync_outputs(src_build: Path, dst_build: Path) -> None:
    if src_build.resolve() == dst_build.resolve():
        return
    dst_build.mkdir(parents=True, exist_ok=True)
    for item in src_build.iterdir():
        if item.is_file():
            shutil.copy2(item, dst_build / item.name)


def build_steps(
    spec: BuildSpec,
    *,
    run_benchmark: bool = True,
) -> Iterator[BuildEvent]:
    """Execute the full build pipeline, yielding structured stage events."""
    sh_dir = spec.stomphacks_dir
    out_dir = spec.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # Determine working directory where manifest.json and .c live
    if spec.source_type == "dsp":
        work_dir = out_dir / "_work"
        work_dir.mkdir(parents=True, exist_ok=True)
    elif (spec.source_path / "gen").is_dir() and spec.dsp_path is not None:
        work_dir = spec.source_path / "gen"
        work_dir.mkdir(parents=True, exist_ok=True)
    elif spec.manifest_path is not None:
        work_dir = spec.manifest_path.parent
    else:
        work_dir = spec.source_path

    # 1. faust_codegen & 2. postprocess
    manifest_data: dict[str, Any] = {}
    if spec.dsp_path is not None:
        yield BuildEvent("faust_codegen", "running", f"Compiling Faust DSP: {spec.dsp_path.name}")
        try:
            faust_out = faust_bridge.compile_dsp_to_kernel(
                spec.dsp_path,
                search_dirs=[spec.dsp_path.parent, spec.dsp_path.parent.parent],
            )
        except Exception as exc:
            yield BuildEvent("faust_codegen", "failed", f"Faust compilation failed: {exc}")
            raise
        yield BuildEvent(
            "faust_codegen",
            "completed",
            f"Generated kernel via {faust_out.mode_used}",
        )

        yield BuildEvent("postprocess", "running", "Generating C674x kernel & manifest.json")
        manifest_data = dict(faust_out.manifest)
        if spec.manifest_override:
            manifest_data = _deep_merge_manifest(manifest_data, spec.manifest_override)
        if spec.no_strip:
            manifest_data["strip"] = False

        c_name = manifest_data.get("kernel", f"{spec.dsp_path.stem}.c")
        manifest_data["kernel"] = c_name
        c_path = work_dir / c_name
        c_path.write_text(faust_out.kernel_c, encoding="utf-8")
        manifest_path = work_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")
        yield BuildEvent(
            "postprocess",
            "completed",
            f"Wrote {c_path.name} ({faust_out.state_bytes} B state) and manifest.json",
        )
    else:
        yield BuildEvent("faust_codegen", "skipped", "C-kernel target; skipping Faust codegen")
        yield BuildEvent("postprocess", "skipped", "C-kernel target; skipping Faust postprocess")
        assert spec.manifest_path is not None
        manifest_path = spec.manifest_path
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        if spec.no_strip:
            manifest_data["strip"] = False
            manifest_path = work_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")

    # 3. compile, 4. assemble, 5. link (via zd2_make_effect.py)
    yield BuildEvent("compile", "running", f"Compiling {manifest_data.get('kernel')} with TI cl6x")
    proc = _run_zd2_make_effect(
        manifest_path,
        sh_dir,
        effect_dir=spec.effect_dir,
        icon_path=spec.icon_path,
        no_strip=spec.no_strip,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        yield BuildEvent("compile", "failed", f"zd2_make_effect.py failed:\n{err}")
        raise RuntimeError(f"zd2_make_effect.py failed (exit {proc.returncode}): {err}")

    yield BuildEvent("compile", "completed", "Compiled C kernel with TI cl6x")
    yield BuildEvent("assemble", "completed", "Assembled clean-room scaffold with asm6x")
    yield BuildEvent("link", "completed", "Linked dynamic shared ELF with lnk6x")

    work_build = work_dir / "build"
    fname = manifest_data["filename"]
    audio_dis = work_build / f"{fname}.audio.dis"

    # 6. benchmark
    dspload_val: float | None = manifest_data.get("dspload")
    if run_benchmark and audio_dis.is_file():
        yield BuildEvent("benchmark", "running", f"Analyzing C674x VLIW & memory hierarchy ({audio_dis.name})")
        metrics = benchmark.analyze_dis_file(audio_dis, eff_name=manifest_data.get("name", fname))
        dspload_val = metrics.dspload_estimate
        yield BuildEvent(
            "benchmark",
            "completed",
            f"{metrics.loop_type}: {metrics.vliw_cycles} VLIW cyc, dspload={dspload_val:.2f} ({metrics.dspload_pct:.1f}%)",
        )
        # Populate dspload into manifest.json and re-pack so .ZD2 INFO.dspload & REPORT.md match
        if manifest_data.get("dspload") != dspload_val:
            manifest_data["dspload"] = dspload_val
            manifest_path.write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")
            proc2 = _run_zd2_make_effect(
                manifest_path,
                sh_dir,
                effect_dir=spec.effect_dir,
                icon_path=spec.icon_path,
                no_strip=spec.no_strip,
            )
            if proc2.returncode != 0:
                err2 = (proc2.stderr or proc2.stdout or "").strip()
                yield BuildEvent("pack", "failed", f"Re-pack with dspload failed: {err2}")
                raise RuntimeError(f"Re-pack failed: {err2}")
    else:
        yield BuildEvent(
            "benchmark",
            "skipped",
            "Benchmark skipped (--no-benchmark or missing .audio.dis)",
        )

    # 7. pack
    yield BuildEvent("pack", "running", f"Packaging {fname}.ZD2 and {fname}.ZIC")
    _sync_outputs(work_build, out_dir)
    zd2_path = out_dir / f"{fname}.ZD2"
    zic_path = out_dir / f"{fname}.ZIC"
    report_path = out_dir / "REPORT.md"
    final_dis = out_dir / f"{fname}.audio.dis"
    yield BuildEvent("pack", "completed", f"Packaged {zd2_path.name} ({zd2_path.stat().st_size} bytes)")

    # 8. verify
    yield BuildEvent("verify", "running", f"Verifying {zd2_path.name}, CRC32, envelope, and REPORT.md")
    chk = safety.check_zd2(zd2_path, stomphacks_dir=sh_dir, report_path=report_path)
    result = BuildResult(
        zd2_path=zd2_path if zd2_path.is_file() else None,
        zic_path=zic_path if zic_path.is_file() else None,
        report_path=report_path if report_path.is_file() else None,
        manifest_path=manifest_path,
        build_dir=out_dir,
        report_passed=chk.passed,
        report_summary=chk.summary,
        dspload=dspload_val,
        audio_dis_path=final_dis if final_dis.is_file() else None,
    )
    yield BuildEvent(
        "verify",
        "completed" if chk.passed else "failed",
        chk.summary,
        result=result,
    )


def build(
    spec: BuildSpec,
    *,
    run_benchmark: bool = True,
) -> BuildResult:
    """Convenience wrapper executing build_steps() to completion."""
    result: BuildResult | None = None
    for ev in build_steps(spec, run_benchmark=run_benchmark):
        if ev.result is not None:
            result = ev.result
    if result is None:
        raise RuntimeError("Build completed without producing a BuildResult")
    return result
