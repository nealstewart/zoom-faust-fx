"""Resolves a standalone .dsp file or an effect directory into a normalized BuildSpec."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import find_stomphacks_dir
from .faust_bridge import extract_metadata


@dataclass(slots=True)
class BuildSpec:
    """Normalized build specification consumed by builder.build_steps()."""

    source_type: str  # "dsp" | "directory"
    source_path: Path
    stomphacks_dir: Path
    output_dir: Path
    dsp_path: Path | None = None
    c_path: Path | None = None
    manifest_path: Path | None = None
    manifest_override: dict[str, Any] = field(default_factory=dict)
    icon_path: Path | None = None
    effect_dir: Path | None = None
    no_strip: bool = False
    stem: str = ""


def _deep_merge_manifest(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for k, v in override.items():
        if k == "params" and isinstance(v, list) and isinstance(merged.get("params"), list):
            # Merge per-param overrides by index or name if lengths match, else replace
            if len(v) == len(merged["params"]):
                merged_params = []
                for bp, op in zip(merged["params"], v):
                    p = dict(bp)
                    p.update(op)
                    merged_params.append(p)
                merged["params"] = merged_params
            else:
                merged["params"] = v
        else:
            merged[k] = v
    return merged


def load(
    target: Path | str,
    *,
    stomphacks_dir: Path | str | None = None,
    out_dir: Path | str | None = None,
    manifest_path: Path | str | None = None,
    icon_path: Path | str | None = None,
    no_strip: bool = False,
) -> BuildSpec:
    """Resolve a `.dsp` file or effect directory into a normalized BuildSpec."""
    tpath = Path(target).expanduser().resolve()
    if not tpath.exists():
        raise FileNotFoundError(f"Target not found: {tpath}")

    sh_dir = find_stomphacks_dir(stomphacks_dir)
    icon = Path(icon_path).expanduser().resolve() if icon_path else None
    if icon is not None and not icon.is_file():
        raise FileNotFoundError(f"Icon file not found: {icon}")

    override_dict: dict[str, Any] = {}
    explicit_manifest: Path | None = None
    if manifest_path is not None:
        explicit_manifest = Path(manifest_path).expanduser().resolve()
        if not explicit_manifest.is_file():
            raise FileNotFoundError(f"Manifest file not found: {explicit_manifest}")
        override_dict = json.loads(explicit_manifest.read_text(encoding="utf-8-sig"))

    if tpath.is_file():
        if tpath.suffix.lower() != ".dsp":
            raise ValueError(f"Expected a .dsp file or effect directory, got: {tpath}")
        # Validate metadata can be parsed early
        extract_metadata(tpath)
        resolved_out = (
            Path(out_dir).expanduser().resolve()
            if out_dir is not None
            else (Path.cwd() / "build" / tpath.stem).resolve()
        )
        return BuildSpec(
            source_type="dsp",
            source_path=tpath,
            stomphacks_dir=sh_dir,
            output_dir=resolved_out,
            dsp_path=tpath,
            manifest_path=explicit_manifest,
            manifest_override=override_dict,
            icon_path=icon,
            effect_dir=tpath.parent,
            no_strip=no_strip,
            stem=tpath.stem,
        )

    if not tpath.is_dir():
        raise ValueError(f"Target must be a .dsp file or directory: {tpath}")

    # Directory target: look for manifest.json (at root or gen/) and/or .dsp / .c
    resolved_out = (
        Path(out_dir).expanduser().resolve()
        if out_dir is not None
        else (tpath / "build").resolve()
    )

    dir_manifest: Path | None = None
    if explicit_manifest is not None:
        dir_manifest = explicit_manifest
    elif (tpath / "manifest.json").is_file():
        dir_manifest = tpath / "manifest.json"
    elif (tpath / "gen" / "manifest.json").is_file():
        dir_manifest = tpath / "gen" / "manifest.json"

    dsp_files = sorted(tpath.glob("*.dsp"))
    dsp_file = dsp_files[0] if dsp_files else None

    c_file: Path | None = None
    if dir_manifest is not None:
        m_data = json.loads(dir_manifest.read_text(encoding="utf-8-sig"))
        if not override_dict:
            override_dict = m_data
        kernel_name = m_data.get("kernel")
        if kernel_name:
            cand_c = dir_manifest.parent / kernel_name
            if cand_c.is_file():
                c_file = cand_c
    if c_file is None:
        c_files = sorted(tpath.glob("*.c"))
        if c_files:
            c_file = c_files[0]

    if dir_manifest is None and dsp_file is None:
        raise FileNotFoundError(
            f"Effect directory '{tpath}' must contain manifest.json or a .dsp file"
        )
    if dsp_file is None and c_file is None:
        raise FileNotFoundError(
            f"Effect directory '{tpath}' has manifest.json but no .dsp or .c kernel"
        )

    # Prefer .dsp if present so Faust codegen runs unless only .c is present
    if icon is None:
        for cand_icon in (tpath / "icon.png", tpath / "icon_0.png"):
            if cand_icon.is_file():
                icon = cand_icon
                break

    return BuildSpec(
        source_type="directory",
        source_path=tpath,
        stomphacks_dir=sh_dir,
        output_dir=resolved_out,
        dsp_path=dsp_file,
        c_path=c_file,
        manifest_path=dir_manifest,
        manifest_override=override_dict,
        icon_path=icon,
        effect_dir=tpath,
        no_strip=no_strip,
        stem=tpath.name,
    )
