"""Isolated per-job build manager wrapping zoom_faust_fx.builder and safety."""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import builder, find_stomphacks_dir, find_ti_cgt_dir, input_loader, safety


@dataclass
class BuildJob:
    """State and outputs for a single isolated build job."""

    build_id: str
    work_dir: Path
    status: str = "queued"  # queued | running | completed | failed
    stages: list[dict[str, Any]] = field(default_factory=list)
    report_passed: bool = False
    installable: bool = False
    report_summary: str = ""
    report_text: str = ""
    dspload: float | None = None
    zd2_path: Path | None = None
    zic_path: Path | None = None
    report_path: Path | None = None
    manifest_path: Path | None = None
    safety_checks: list[str] = field(default_factory=list)
    safety_errors: list[str] = field(default_factory=list)
    error: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        artifacts: list[str] = []
        for p in (self.zd2_path, self.zic_path, self.report_path, self.manifest_path):
            if p is not None and p.is_file():
                artifacts.append(p.name)
        return {
            "build_id": self.build_id,
            "status": self.status,
            "stages": list(self.stages),
            "report_passed": self.report_passed,
            "installable": self.installable,
            "report_summary": self.report_summary,
            "report_text": self.report_text,
            "dspload": self.dspload,
            "zd2_filename": self.zd2_path.name if self.zd2_path and self.zd2_path.is_file() else None,
            "zic_filename": self.zic_path.name if self.zic_path and self.zic_path.is_file() else None,
            "artifacts": artifacts,
            "safety_checks": list(self.safety_checks),
            "safety_errors": list(self.safety_errors),
            "error": self.error,
            "created_at": self.created_at,
        }


def _safe_stem(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip()).strip("_")
    return cleaned or "custom_fx"


def _write_icon_asset(input_dir: Path, icon_png_base64: str | None, icon_bitmap: list[list[int]] | None) -> Path | None:
    """Persist an optional PNG base64 or 1-bit bitmap matrix into input_dir/icon.png."""
    if icon_png_base64:
        raw = icon_png_base64.split(",", 1)[-1]
        data = base64.b64decode(raw)
        icon_path = input_dir / "icon.png"
        icon_path.write_bytes(data)
        return icon_path

    if icon_bitmap:
        try:
            from PIL import Image
        except ImportError:
            return None
        height = len(icon_bitmap)
        width = len(icon_bitmap[0]) if height > 0 else 0
        if width <= 0 or height <= 0:
            return None
        img = Image.new("1", (width, height), 0)
        for y, row in enumerate(icon_bitmap):
            for x, val in enumerate(row[:width]):
                img.putpixel((x, y), 1 if val else 0)
        icon_path = input_dir / "icon.png"
        img.save(icon_path)
        return icon_path

    return None


class BuildManager:
    """Manages isolated per-job build directories and executes zoom_faust_fx builds."""

    def __init__(
        self,
        work_root: Path | str | None = None,
        *,
        stomphacks_dir: Path | str | None = None,
    ) -> None:
        default_root = os.environ.get("STOMPHACKS_WORK_DIR") or str(Path.cwd() / "build" / "jobs")
        self.work_root = Path(work_root or default_root).expanduser().resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        self._explicit_stomphacks_dir = Path(stomphacks_dir).resolve() if stomphacks_dir else None
        self._jobs: dict[str, BuildJob] = {}
        self._lock = threading.Lock()
        self._listeners: dict[str, list[Callable[[dict[str, Any]], None]]] = {}

    def _resolve_stomphacks(self) -> Path:
        return find_stomphacks_dir(self._explicit_stomphacks_dir)

    def get_job(self, build_id: str) -> BuildJob | None:
        with self._lock:
            return self._jobs.get(build_id)

    def subscribe(self, build_id: str, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            self._listeners.setdefault(build_id, []).append(callback)

    def unsubscribe(self, build_id: str, callback: Callable[[dict[str, Any]], None]) -> None:
        with self._lock:
            lst = self._listeners.get(build_id, [])
            if callback in lst:
                lst.remove(callback)

    def _emit(self, build_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._listeners.get(build_id, []))
        for cb in listeners:
            try:
                cb(event)
            except Exception:
                pass

    def get_artifact_path(self, build_id: str, filename: str) -> Path | None:
        job = self.get_job(build_id)
        if job is None:
            return None
        safe_name = Path(filename).name
        if safe_name != filename or safe_name in ("", ".", ".."):
            return None
        for p in (job.zd2_path, job.zic_path, job.report_path, job.manifest_path):
            if p is not None and p.name == safe_name and p.is_file():
                return p
        return None

    def create_job(
        self,
        *,
        dsp_code: str | None = None,
        c_code: str | None = None,
        manifest: dict[str, Any] | None = None,
        name: str = "custom_fx",
        icon_png_base64: str | None = None,
        icon_bitmap: list[list[int]] | None = None,
    ) -> BuildJob:
        if not dsp_code and not c_code:
            raise ValueError("Either dsp_code or c_code must be provided.")

        build_id = uuid.uuid4().hex[:12]
        job_dir = (self.work_root / build_id).resolve()
        input_dir = job_dir / "input"
        out_dir = job_dir / "out"
        input_dir.mkdir(parents=True, exist_ok=True)
        out_dir.mkdir(parents=True, exist_ok=True)

        stem = _safe_stem((manifest or {}).get("name") or name)
        manifest_path: Path | None = None
        if manifest:
            manifest = dict(manifest)
            if isinstance(manifest.get("id"), str) and manifest["id"].lower().startswith("0x"):
                manifest["id"] = manifest["id"][2:]
            manifest.setdefault("author", "waveformer")
            manifest_path = input_dir / "manifest.json"
            manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        _write_icon_asset(input_dir, icon_png_base64, icon_bitmap)

        if dsp_code:
            dsp_path = input_dir / f"{stem}.dsp"
            dsp_path.write_text(dsp_code, encoding="utf-8")
        else:
            assert c_code is not None
            c_name = (manifest or {}).get("kernel") or f"{stem}.c"
            c_path = input_dir / c_name
            c_path.write_text(c_code, encoding="utf-8")
            if manifest_path is None:
                raise ValueError("manifest is required when building from raw c_code.")

        job = BuildJob(build_id=build_id, work_dir=job_dir, status="queued")
        with self._lock:
            self._jobs[build_id] = job
        return job

    def run_job(
        self,
        build_id: str,
        *,
        run_benchmark: bool = True,
        no_strip: bool = False,
    ) -> BuildJob:
        job = self.get_job(build_id)
        if job is None:
            raise KeyError(f"Unknown build_id: {build_id}")

        job.status = "running"
        input_dir = job.work_dir / "input"
        out_dir = job.work_dir / "out"
        sh_dir = self._resolve_stomphacks()

        dsp_files = list(input_dir.glob("*.dsp"))
        manifest_file = input_dir / "manifest.json"
        icon_file = input_dir / "icon.png"

        try:
            if dsp_files:
                spec = input_loader.load(
                    dsp_files[0],
                    stomphacks_dir=sh_dir,
                    out_dir=out_dir,
                    manifest_path=manifest_file if manifest_file.is_file() else None,
                    icon_path=icon_file if icon_file.is_file() else None,
                    no_strip=no_strip,
                )
            else:
                spec = input_loader.load(
                    input_dir,
                    stomphacks_dir=sh_dir,
                    out_dir=out_dir,
                    icon_path=icon_file if icon_file.is_file() else None,
                    no_strip=no_strip,
                )

            result: builder.BuildResult | None = None
            for ev in builder.build_steps(spec, run_benchmark=run_benchmark):
                ev_dict = {
                    "type": "stage",
                    "build_id": build_id,
                    "stage": ev.stage,
                    "status": ev.status,
                    "message": ev.message,
                }
                job.stages.append(ev_dict)
                self._emit(build_id, ev_dict)
                if ev.result is not None:
                    result = ev.result

            if result is None:
                raise RuntimeError("Build pipeline finished without producing a BuildResult.")

            job.zd2_path = result.zd2_path
            job.zic_path = result.zic_path
            job.report_path = result.report_path
            job.manifest_path = result.manifest_path
            job.dspload = result.dspload
            job.report_passed = result.report_passed
            job.report_summary = result.report_summary
            if result.report_path and result.report_path.is_file():
                job.report_text = result.report_path.read_text(encoding="utf-8", errors="replace")

            # Hard gate: both REPORT.md and offline safety.check_zd2 must pass.
            if result.report_passed and result.zd2_path and result.zd2_path.is_file():
                sres = safety.check_zd2(
                    result.zd2_path,
                    stomphacks_dir=sh_dir,
                    report_path=result.report_path,
                )
                job.safety_checks = list(sres.checks)
                job.safety_errors = list(sres.errors)
                job.installable = bool(sres.passed)
            else:
                job.installable = False
                if not result.report_passed:
                    job.safety_errors.append(result.report_summary or "REPORT.md gate failed")

            job.status = "completed" if job.report_passed else "failed"
        except Exception as exc:
            job.status = "failed"
            job.installable = False
            job.error = str(exc)
            job.safety_errors.append(str(exc))
            self._emit(
                build_id,
                {
                    "type": "error",
                    "build_id": build_id,
                    "stage": "build",
                    "status": "failed",
                    "message": str(exc),
                },
            )

        self._emit(build_id, {"type": "done", "build_id": build_id, "job": job.to_dict()})
        return job

    def check_capabilities(self) -> dict[str, Any]:
        sh_dir: Path | None = None
        ti_cgt: Path | None = None
        try:
            sh_dir = self._resolve_stomphacks()
            ti_cgt = find_ti_cgt_dir(sh_dir)
        except FileNotFoundError:
            pass
        return {
            "profile": os.environ.get("STOMPHACKS_PROFILE", "podman"),
            "stomphacks_dir": str(sh_dir) if sh_dir else None,
            "ti_cgt_available": ti_cgt is not None,
            "ti_cgt_dir": str(ti_cgt) if ti_cgt else None,
            "linux_midi_available": os.environ.get("STOMPHACKS_DIRECT_MIDI") == "1",
        }
