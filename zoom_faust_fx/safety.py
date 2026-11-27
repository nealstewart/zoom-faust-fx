"""Shared pre-flight safety checks wrapping stomphacks tools and tools-pedal."""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import find_stomphacks_dir


@dataclass(slots=True)
class SafetyCheckResult:
    """Result of offline safety / container / FLST validation."""

    passed: bool
    summary: str
    checks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _load_module(name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(name, str(file_path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module {name} from {file_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ensure_stomphacks_sys_path(stomphacks_dir: Path) -> None:
    for sub in ("zoom-zt2", "tools", "tools-pedal"):
        p = str(stomphacks_dir / sub)
        if p not in sys.path:
            sys.path.insert(0, p)


def check_zd2(
    zd2_path: Path | str,
    *,
    stomphacks_dir: Path | str | None = None,
    report_path: Path | str | None = None,
) -> SafetyCheckResult:
    """Run offline zd2_roundtrip, envelope, stock_catalog/classify, ZIC, and REPORT.md checks."""
    zpath = Path(zd2_path).expanduser().resolve()
    if not zpath.is_file():
        return SafetyCheckResult(
            passed=False,
            summary=f"ZD2 file not found: {zpath}",
            errors=[f"File not found: {zpath}"],
        )

    sh_dir = find_stomphacks_dir(stomphacks_dir)
    _ensure_stomphacks_sys_path(sh_dir)

    checks: list[str] = []
    errors: list[str] = []

    # 1. zd2_roundtrip + CRC32
    rt_mod = _load_module("sh_zd2_roundtrip", sh_dir / "tools" / "zd2_roundtrip.py")
    data = zpath.read_bytes()
    if not rt_mod.check_crc(data):
        errors.append("CRC32 check failed")
    else:
        checks.append("CRC32 valid")

    rt_line = rt_mod.check_file(zpath)
    if rt_line.startswith("OK"):
        checks.append(rt_line.strip())
    else:
        errors.append(f"Round-trip failed: {rt_line}")

    # 2. pedal_diy envelope, ZIC, and stock_catalog collision checks
    pdiy = _load_module("sh_pedal_diy", sh_dir / "tools-pedal" / "pedal_diy.py")
    env_problems = pdiy.check_envelope(data)
    if env_problems:
        for prob in env_problems:
            errors.append(f"Envelope invariant failed: {prob}")
    else:
        checks.append("Container envelope invariants OK")

    zic_path = zpath.with_suffix(".ZIC")
    if zic_path.is_file():
        zic_problems = pdiy.check_zic(zic_path.read_bytes())
        if zic_problems:
            for prob in zic_problems:
                errors.append(f"ZIC icon invariant failed: {prob}")
        else:
            checks.append(f"Companion ZIC valid ({zic_path.name})")

    try:
        res_cls = pdiy.classify(zpath)
        ok_cls, eid, name, reason = res_cls[0], res_cls[1], res_cls[2], res_cls[3]
        if ok_cls is True or ok_cls == "diy":
            eid_i = int(eid) if isinstance(eid, int) else 0
            checks.append(f"DIY classification OK (id=0x{eid_i:08x}, name={name!r})")
        else:
            errors.append(f"Classification rejected ({ok_cls}): {reason}")
    except SystemExit as exc:
        errors.append(f"Classification error: {exc}")

    # 3. REPORT.md check if present
    rep = Path(report_path).resolve() if report_path else (zpath.parent / "REPORT.md")
    if rep.is_file():
        rep_text = rep.read_text(encoding="utf-8", errors="replace")
        if "envelope invariants OK" in rep_text and (
            "SAFE-DSP ok" in rep_text or "SAFE-DSP relaxed:" in rep_text
        ):
            checks.append(f"REPORT.md verified ({rep.name})")
        else:
            errors.append(f"REPORT.md missing expected OK markers ({rep})")

    passed = len(errors) == 0
    summary = (
        f"PASS ({len(checks)} checks): {zpath.name}"
        if passed
        else f"FAIL ({len(errors)} errors): " + "; ".join(errors)
    )
    return SafetyCheckResult(passed=passed, summary=summary, checks=checks, errors=errors)


def check_flst(
    flst_path: Path | str,
    *,
    stomphacks_dir: Path | str | None = None,
) -> SafetyCheckResult:
    """Validate an offline FLST_SEQ.ZT2 file via tools/flst_check.py."""
    fpath = Path(flst_path).expanduser().resolve()
    if not fpath.is_file():
        return SafetyCheckResult(
            passed=False,
            summary=f"FLST file not found: {fpath}",
            errors=[f"File not found: {fpath}"],
        )

    sh_dir = find_stomphacks_dir(stomphacks_dir)
    _ensure_stomphacks_sys_path(sh_dir)
    flst_mod = _load_module("sh_flst_check", sh_dir / "tools" / "flst_check.py")

    ok, problems, entries = flst_mod.validate_flst(fpath.read_bytes())
    if ok:
        return SafetyCheckResult(
            passed=True,
            summary=f"PASS: FLST_SEQ.ZT2 valid ({len(entries)} entries)",
            checks=[f"FLST round-trip & group checks OK ({len(entries)} entries)"],
        )
    return SafetyCheckResult(
        passed=False,
        summary="FAIL: " + "; ".join(problems),
        errors=list(problems),
    )
