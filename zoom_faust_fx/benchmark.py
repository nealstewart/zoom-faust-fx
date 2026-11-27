"""C674x VLIW cycle/memory-hierarchy benchmark, host timing & stock calibration."""

from __future__ import annotations

import ctypes
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from . import find_stomphacks_dir, find_ti_cgt_dir
from .faust_bridge import compile_dsp_to_kernel

# Budget calibration: 270.0 raw = 100% MS-70CDR+ budget (~110,000-135,000 C674x cycles/block
# across 6 slots + firmware overhead; 100% single-effect slot budget ~ 65,000 cycles/block).
RAW_BUDGET = 270.0
CYCLES_PER_RAW_UNIT = 240.0  # ~64,800 cycles == 270.0 raw (calibrated against stock/DIY corpus)


@dataclass(slots=True)
class BenchmarkMetrics:
    """C674x VLIW, memory-hierarchy, and optional host timing metrics for an effect."""

    name: str
    loop_type: str
    trip_count: int
    prologue_cyc: int
    iter_cyc: int
    epilogue_cyc: int
    vliw_cycles: int
    loop_insts: int
    loop_spills_per_iter: int
    total_spills_per_block: int
    sdram_stall_cycles: int
    l1d_l2_stall_cycles: int
    total_estimated_cycles: int
    dspload_estimate: float
    dspload_pct: float
    host_ns_per_block: float | None = None
    uses_sdram: bool = False
    dis_path: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class BenchmarkEvent:
    """Structured event yielded by analyze_steps()."""

    stage: str
    status: str
    message: str
    metrics: BenchmarkMetrics | None = None
    results: list[BenchmarkMetrics] = field(default_factory=list)


def clean_dis6x_line(line: str) -> str:
    """Strip address, 16/32-bit hex opcode columns, and PC annotations from dis6x output."""
    s = re.sub(r"^[0-9a-fA-F]{8}\s+[0-9a-fA-F]{4,8}\s+", "", line).strip()
    s = re.sub(r"^[0-9a-fA-F]{4,8}\s+", "", s).strip()
    s = re.sub(r"\s*\(PC[+-]\d+\s*=\s*0x[0-9a-fA-F]+\)", "", s)
    return s


def count_vliw_cycles_in_lines(lines: list[str]) -> tuple[int, int, int]:
    """Count VLIW execute packets (cycles), instructions, and stack ops (*+B15 / *B15)."""
    cycles = 0
    insts = 0
    stack_ops = 0

    for raw in lines:
        line = clean_dis6x_line(raw)
        if not line or line.startswith(";") or line.startswith(".fphead"):
            continue
        if re.match(r"^[A-Za-z0-9_$]+:$", line):
            continue

        is_parallel = line.startswith("||")
        line_body = re.sub(r"^\|\|\^?\s*", "", line) if is_parallel else line

        if "B15" in line_body and ("LD" in line_body or "ST" in line_body):
            stack_ops += 1

        insts += 1

        m_nop = re.match(r"^NOP\s+(\d+)\b", line_body)
        if m_nop:
            if not is_parallel:
                cycles += int(m_nop.group(1))
            continue

        m_bnop = re.search(r"\bBNOP\.[A-Za-z0-9]+\s+[^,]+,\s*(\d+)", line_body)
        extra_bnop = int(m_bnop.group(1)) if m_bnop else 0

        if not is_parallel:
            cycles += 1 + extra_bnop
        else:
            cycles += extra_bnop

    return cycles, insts, stack_ops


def analyze_dis_file(dis_path: Path, eff_name: str = "", *, strict: bool = True) -> BenchmarkMetrics:
    """Analyze a `.audio.dis` disassembly to compute VLIW, memory, and dspload metrics."""
    raw_lines = [
        clean_dis6x_line(l)
        for l in dis_path.read_text(encoding="utf-8", errors="replace").splitlines()
    ]
    joined_dis = "\n".join(raw_lines)
    if strict and eff_name and not eff_name.startswith("STOCK:"):
        if re.search(r"\bCALLP?\b", joined_dis):
            raise RuntimeError(f"Effect {eff_name} emitted a forbidden CALL instruction in {dis_path}")
        if re.search(r"\bB14\b", joined_dis):
            raise RuntimeError(f"Effect {eff_name} emitted a forbidden B14 DP reference in {dis_path}")

    labels: dict[str, int] = {}
    for idx, s in enumerate(raw_lines):
        m = re.match(r"^(\$C\$L\d+):$", s)
        if m:
            labels[m.group(1)] = idx

    backward_loops: list[tuple[str, int, int]] = []
    for idx, s in enumerate(raw_lines):
        m = re.search(r"\bB(?:NOP)?\.S[12]\s+(\$C\$L\d+)\b", s)
        if m:
            lbl = m.group(1)
            if lbl in labels and labels[lbl] < idx:
                end_idx = idx + 1
                pkt_after = 0
                m_bnop = re.search(r"\bBNOP\.S[12]\s+\$C\$L\d+,\s*(\d+)", s)
                bnop_n = int(m_bnop.group(1)) if m_bnop else 0
                needed_pkts = max(0, 5 - bnop_n)
                while end_idx < len(raw_lines) and pkt_after < needed_pkts:
                    nxt = raw_lines[end_idx]
                    if re.match(r"^\$C\$L\d+:$", nxt):
                        break
                    if nxt and not nxt.startswith(".fphead") and not nxt.startswith("||"):
                        m_n = re.match(r"^NOP\s+(\d+)", nxt)
                        pkt_after += int(m_n.group(1)) if m_n else 1
                    end_idx += 1
                backward_loops.append((lbl, labels[lbl], end_idx))

    sploops: list[tuple[int, int, int]] = []
    for idx, s in enumerate(raw_lines):
        s_clean = re.sub(r"^\|\|\^?\s*", "", s)
        m = re.match(r"^SPLOOP(?:D|W)?\s+(\d+)", s_clean)
        if m:
            ii = int(m.group(1))
            k_idx = idx + 1
            while k_idx < len(raw_lines) and "SPKERNEL" not in raw_lines[k_idx]:
                k_idx += 1
            sploops.append((ii, idx, min(len(raw_lines), k_idx + 2)))

    if backward_loops:
        backward_loops.sort(key=lambda x: x[2] - x[1], reverse=True)
        _, loop_start, loop_end = backward_loops[0]
        loop_lines = raw_lines[loop_start:loop_end]
        loop_cyc, loop_inst, loop_spills = count_vliw_cycles_in_lines(loop_lines)

        skipped_cyc = 0
        i = 0
        while i < len(loop_lines):
            s = loop_lines[i]
            if not s.startswith("[") and not re.match(r"^\|\|\s*\[", s):
                m_fwd = re.search(r"\bB(?:NOP)?\.S[12]\s+(\$C\$L\d+)", s)
                if m_fwd:
                    target_lbl = m_fwd.group(1)
                    for j in range(i + 1, len(loop_lines)):
                        if loop_lines[j] == f"{target_lbl}:":
                            alt_cyc, _, _ = count_vliw_cycles_in_lines(loop_lines[i + 2 : j])
                            skipped_cyc += alt_cyc
                            break
            i += 1

        effective_iter_cyc = max(1, loop_cyc - skipped_cyc)
        pre_lines = raw_lines[:loop_start]
        trip_count = 16
        is_sw_pipelined = False
        for pl in pre_lines[-40:]:
            if re.search(r"\bSUB\.[A-Za-z0-9]+\s+[AB]\d+,\s*0x4,", pl):
                trip_count = 13
                is_sw_pipelined = True
                break

        prologue_cyc, _, prologue_spills = count_vliw_cycles_in_lines(pre_lines)
        epilogue_end = loop_end
        for idx in range(loop_end, len(raw_lines)):
            if re.search(r"\bB(?:NOP)?\.S2\s+B3\b", raw_lines[idx]):
                epilogue_end = min(len(raw_lines), idx + 6)
                break
        epilogue_cyc, _, epilogue_spills = count_vliw_cycles_in_lines(raw_lines[loop_end:epilogue_end])
        total_vliw_cyc = prologue_cyc + effective_iter_cyc * trip_count + epilogue_cyc
        loop_type = "SW-Pipelined" if is_sw_pipelined else "Scalar-VLIW"
        total_spills = prologue_spills + loop_spills * trip_count + epilogue_spills
    elif sploops:
        sploops.sort(key=lambda x: x[0], reverse=True)
        ii, sp_start, sp_end = sploops[0]
        prologue_cyc, _, prologue_spills = count_vliw_cycles_in_lines(raw_lines[: min(56, sp_start)])
        body_cyc, loop_inst, loop_spills = count_vliw_cycles_in_lines(raw_lines[sp_start:sp_end])
        loop_total = 16 * ii + body_cyc
        epilogue_cyc = 12
        total_vliw_cyc = prologue_cyc + loop_total + epilogue_cyc
        loop_type = f"SPLOOP(II={ii})"
        trip_count = 16
        effective_iter_cyc = ii
        total_spills = prologue_spills + loop_spills * 16
    else:
        cyc, loop_inst, total_spills = count_vliw_cycles_in_lines(raw_lines)
        loop_type = "Flat"
        trip_count = 1
        prologue_cyc = cyc
        effective_iter_cyc = 0
        epilogue_cyc = 0
        total_vliw_cyc = cyc
        loop_spills = 0

    # Memory hierarchy model (docs/c6745-memory-hierarchy.md):
    # - L1D miss -> L2 hit: ~10 cycles
    # - Stack spills stay in L1D (0 miss if warm, ~1 cycle contention)
    # - SDRAM access via instance[3]: detect descriptor load *+A4[3] / *+B4[3] or many non-stack LDW
    uses_sdram = bool(re.search(r"\*[+]?[AB]\d+\[3\]", joined_dis))
    mem_loads = len(re.findall(r"\bLD(?:W|NDW|DW)\b", joined_dis))
    non_stack_loads = max(0, mem_loads - total_spills)
    l1d_l2_stall = min(non_stack_loads, 8) * 4 if not uses_sdram else non_stack_loads * 6
    sdram_stall = (max(1, non_stack_loads // 4) * 75) if uses_sdram else 0

    total_est = total_vliw_cyc + l1d_l2_stall + sdram_stall
    raw_est = max(2.0, min(RAW_BUDGET, round(total_est / CYCLES_PER_RAW_UNIT, 2)))
    return BenchmarkMetrics(
        name=eff_name or dis_path.stem,
        loop_type=loop_type,
        trip_count=trip_count,
        prologue_cyc=prologue_cyc,
        iter_cyc=effective_iter_cyc,
        epilogue_cyc=epilogue_cyc,
        vliw_cycles=total_vliw_cyc,
        loop_insts=loop_inst,
        loop_spills_per_iter=loop_spills,
        total_spills_per_block=total_spills,
        sdram_stall_cycles=sdram_stall,
        l1d_l2_stall_cycles=l1d_l2_stall,
        total_estimated_cycles=total_est,
        dspload_estimate=raw_est,
        dspload_pct=round(raw_est / 2.7, 1),
        uses_sdram=uses_sdram,
        dis_path=str(dis_path),
    )


def _synthesize_sh_params_h(manifest: dict) -> str:
    params = manifest.get("params", [])
    sym = re.sub(r"[^A-Za-z0-9]", "", manifest["name"])
    lines = [
        "#ifndef SH_PARAMS_H",
        "#define SH_PARAMS_H",
        f"#define SH_AUDIO_FN Fx_SFX_{sym}",
        "#define SH_FRAMES 16",
        "#define SH_CH_B_OFFSET 16",
        "#define SH_CTX_EFF 1",
        "#define SH_CTX_AUX 2",
        "#define SH_COEFF_BYPASS 0",
        "#define SH_COEFF_BPM 15",
    ]
    state_bytes = int(manifest.get("state_bytes", 16))
    lines.append(f"#define SH_STATE_GUARD_WORD {state_bytes // 4}")
    lines.append("#define SH_STATE_GUARD 0x57464731u")
    slots = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    for idx, p in enumerate(params):
        slot = p.get("slot", slots[idx] if idx < len(slots) else idx + 5)
        pname = re.sub(r"[^A-Za-z0-9]+", "_", p["name"].upper()).strip("_") or f"PARAM_{idx}"
        lines.append(f"#define SH_PARAM_{pname} {slot}")
    lines.append("#endif")
    return "\n".join(lines) + "\n"


def compile_c_to_dis(
    c_path: Path,
    manifest: dict,
    stomphacks_dir: Path,
    tmp_dir: Path,
) -> Path:
    """Compile a C kernel with cl6x and disassemble its audio function with dis6x."""
    ti_dir = find_ti_cgt_dir(stomphacks_dir)
    if ti_dir is None:
        raise FileNotFoundError("TI C6000 CGT not found; set ZOOM_TI_CGT")
    ti_bin = ti_dir / "bin"
    ti_inc = ti_dir / "include"
    cl6x = ti_bin / ("cl6x.exe" if sys.platform == "win32" else "cl6x")
    dis6x = ti_bin / ("dis6x.exe" if sys.platform == "win32" else "dis6x")

    (tmp_dir / "sh_params.h").write_text(_synthesize_sh_params_h(manifest), encoding="utf-8")
    obj_path = tmp_dir / f"{c_path.stem}.obj"

    ofs = manifest.get("opt_for_space", 3)
    olvl = manifest.get("opt_level", 2)
    extra_cflags = manifest.get("kernel_cflags", [])
    cmd = (
        [
            str(cl6x),
            "--c99",
            f"--opt_level={olvl}",
            "--silicon_version=6740",
            "--abi=eabi",
            "--endian=little",
            "--object_format=elf",
            "--symdebug:none",
            "--mem_model:const=data",
            "--mem_model:data=far_aggregates",
        ]
        + ([f"--opt_for_space={ofs}"] if ofs is not None else [])
        + list(extra_cflags)
        + [
            f"--include_path={ti_inc}",
            f"--include_path={tmp_dir}",
            f"--include_path={c_path.parent}",
            f"--include_path={stomphacks_dir / 'effects'}",
            "-c",
            str(c_path),
            f"--output_file={obj_path}",
        ]
    )
    subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=str(tmp_dir))
    res = subprocess.run(
        [str(dis6x), str(obj_path)],
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
    )
    sym = re.sub(r"[^A-Za-z0-9]", "", manifest["name"])
    fn_name = f"Fx_SFX_{sym}"
    out_lines: list[str] = []
    in_fn = False
    for line in res.stdout.splitlines():
        s = line.strip()
        m_lbl = re.match(r"^(?:[0-9a-fA-F]{8}\s+)?([A-Za-z_][A-Za-z0-9_$]*):$", s)
        if m_lbl:
            lbl = m_lbl.group(1)
            if lbl == fn_name:
                in_fn = True
                out_lines.append(f"{fn_name}:")
                continue
            if in_fn and not lbl.startswith("$C$"):
                break
        if in_fn:
            if s.startswith("TEXT Section") or s.startswith("DATA Section"):
                break
            cleaned = clean_dis6x_line(line)
            if cleaned:
                out_lines.append(cleaned)
    out_dis = tmp_dir / f"{c_path.stem}.audio.dis"
    out_dis.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return out_dis


def try_host_timing(c_path: Path, manifest: dict, n_blocks: int = 50000) -> float | None:
    """Optionally measure native host ns/block via tests/c_harness.py if a host C compiler exists."""
    try:
        repo_root = Path(__file__).resolve().parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from tests.c_harness import KernelHarness
    except Exception:
        return None

    try:
        c_src = c_path.read_text(encoding="utf-8-sig", errors="replace")
        sym = re.sub(r"[^A-Za-z0-9]", "", manifest["name"])
        fn_name = f"Fx_SFX_{sym}"
        export_attr = "__declspec(dllexport) " if sys.platform == "win32" else ""
        bench_c_addon = f"""
{export_attr}void run_kernel_bench(void **instance, void **ctx, int n_blocks) {{
    float *eff = (float *)ctx[1];
    for (int b = 0; b < n_blocks; b++) {{
        for (int i = 0; i < 16; i++) {{
            float s = 0.25f * ((float)((i + b) & 15) * 0.125f - 1.0f);
            eff[i] = s;
            eff[i + 16] = -s;
        }}
        {fn_name}(instance, ctx);
    }}
}}
"""
        with KernelHarness(c_src + bench_c_addon, manifest) as h:
            aux_buf = (ctypes.c_float * 32)()
            h.ctx[2] = ctypes.cast(aux_buf, ctypes.c_void_p)
            bench_fn = h.lib.run_kernel_bench
            bench_fn.argtypes = [
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_int,
            ]
            bench_fn.restype = None
            bench_fn(h.instance, h.ctx, 1000)
            t0 = time.perf_counter_ns()
            bench_fn(h.instance, h.ctx, n_blocks)
            t1 = time.perf_counter_ns()
        return round((t1 - t0) / n_blocks, 2)
    except Exception:
        return None


def _analyze_single_target(target: Path, stomphacks_dir: Path) -> BenchmarkMetrics:
    if target.is_file() and target.name.endswith(".dis"):
        return analyze_dis_file(target, eff_name=target.stem.split(".")[0], strict=False)

    if target.is_file() and target.suffix.lower() == ".dsp":
        out = compile_dsp_to_kernel(target)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            c_path = tmp_p / f"{target.stem}.c"
            c_path.write_text(out.kernel_c, encoding="utf-8")
            dis_path = compile_c_to_dis(c_path, out.manifest, stomphacks_dir, tmp_p)
            metrics = analyze_dis_file(dis_path, eff_name=out.meta.name)
            metrics.host_ns_per_block = try_host_timing(c_path, out.manifest)
            return metrics

    if target.is_dir():
        # Check for existing .audio.dis in build/ or gen/build/
        for bdir in (target / "build", target / "gen" / "build"):
            dis_list = sorted(bdir.glob("*.audio.dis")) if bdir.is_dir() else []
            if dis_list:
                metrics = analyze_dis_file(dis_list[0], eff_name=target.name, strict=False)
                # Optionally attach host timing if C source exists
                for src_dir in (target / "gen", target):
                    mpath = src_dir / "manifest.json"
                    if mpath.is_file():
                        m = json.loads(mpath.read_text(encoding="utf-8-sig"))
                        cpath = src_dir / m.get("kernel", f"{target.name}.c")
                        if cpath.is_file():
                            metrics.host_ns_per_block = try_host_timing(cpath, m)
                        break
                return metrics

        # No .audio.dis yet: look for manifest.json + .c or .dsp
        for src_dir in (target / "gen", target):
            mpath = src_dir / "manifest.json"
            if mpath.is_file():
                m = json.loads(mpath.read_text(encoding="utf-8-sig"))
                cpath = src_dir / m.get("kernel", f"{target.name}.c")
                if cpath.is_file():
                    with tempfile.TemporaryDirectory() as tmp:
                        dis_path = compile_c_to_dis(cpath, m, stomphacks_dir, Path(tmp))
                        metrics = analyze_dis_file(dis_path, eff_name=m.get("name", target.name))
                        metrics.host_ns_per_block = try_host_timing(cpath, m)
                        return metrics

        dsp_files = sorted(target.glob("*.dsp"))
        if dsp_files:
            return _analyze_single_target(dsp_files[0], stomphacks_dir)

    raise FileNotFoundError(f"Cannot benchmark target: {target}")


def analyze_steps(
    target: Path | str,
    *,
    stomphacks_dir: Path | str | None = None,
) -> Iterator[BenchmarkEvent]:
    """Generator yielding benchmark stages for a .dsp, .audio.dis, effect dir, or effects/ collection."""
    tpath = Path(target).expanduser().resolve()
    if not tpath.exists():
        raise FileNotFoundError(f"Benchmark target not found: {tpath}")
    sh_dir = find_stomphacks_dir(stomphacks_dir)

    # Check if tpath is a collection directory (contains multiple effect subdirs)
    if (
        tpath.is_dir()
        and not (tpath / "manifest.json").is_file()
        and not (tpath / "gen" / "manifest.json").is_file()
        and not list(tpath.glob("*.dsp"))
        and not (tpath / "build").is_dir()
    ):
        subdirs = sorted(p for p in tpath.iterdir() if p.is_dir() and not p.name.startswith("."))
        results: list[BenchmarkMetrics] = []
        yield BenchmarkEvent(
            stage="benchmark",
            status="running",
            message=f"Benchmarking {len(subdirs)} effects in {tpath.name}/",
        )
        for sub in subdirs:
            try:
                m = _analyze_single_target(sub, sh_dir)
                results.append(m)
                yield BenchmarkEvent(
                    stage="benchmark",
                    status="progress",
                    message=f"{sub.name}: {m.vliw_cycles} VLIW cyc, dspload~{m.dspload_estimate:.1f} ({m.dspload_pct:.1f}%)",
                    metrics=m,
                )
            except Exception as exc:
                yield BenchmarkEvent(
                    stage="benchmark",
                    status="warning",
                    message=f"{sub.name}: skipped ({exc})",
                )
        yield BenchmarkEvent(
            stage="benchmark",
            status="completed",
            message=f"Benchmarked {len(results)} effects",
            results=results,
        )
        return

    yield BenchmarkEvent(
        stage="benchmark",
        status="running",
        message=f"Analyzing {tpath.name}",
    )
    metrics = _analyze_single_target(tpath, sh_dir)
    yield BenchmarkEvent(
        stage="benchmark",
        status="completed",
        message=(
            f"{metrics.name}: {metrics.loop_type}, {metrics.vliw_cycles} VLIW cyc, "
            f"dspload~{metrics.dspload_estimate:.1f} ({metrics.dspload_pct:.1f}%)"
        ),
        metrics=metrics,
        results=[metrics],
    )


def analyze(
    target: Path | str,
    *,
    stomphacks_dir: Path | str | None = None,
) -> BenchmarkMetrics:
    """Convenience wrapper returning BenchmarkMetrics for a single target."""
    last: BenchmarkMetrics | None = None
    for ev in analyze_steps(target, stomphacks_dir=stomphacks_dir):
        if ev.metrics is not None:
            last = ev.metrics
    if last is None:
        raise RuntimeError(f"No benchmark metrics produced for {target}")
    return last
