"""zoom_faust_fx: Shared Python library and CLI for Zoom MS Plus .ZD2/.ZIC effects."""

from __future__ import annotations

import os
from pathlib import Path

__version__ = "0.1.0"


def is_stomphacks_root(path: Path) -> bool:
    """Return True if path looks like a checked-out stomphacks repository."""
    return (
        (path / "tools").is_dir()
        and (path / "tools-pedal").is_dir()
        and (path / "zoom-zt2").is_dir()
    )


def find_stomphacks_dir(explicit: Path | str | None = None) -> Path:
    """Resolve the stomphacks repository root.

    Resolution order:
    1. Explicit ``--stomphacks-dir`` argument.
    2. ``STOMPHACKS_DIR`` environment variable.
    3. Current working directory or any of its parent directories.
    4. Sibling ``../stomphacks`` directory (useful during local development).
    """
    if explicit is not None:
        p = Path(explicit).expanduser().resolve()
        if not is_stomphacks_root(p):
            raise FileNotFoundError(
                f"Invalid --stomphacks-dir '{p}': expected tools/, tools-pedal/, and zoom-zt2/"
            )
        return p

    env_dir = os.environ.get("STOMPHACKS_DIR")
    if env_dir:
        p = Path(env_dir).expanduser().resolve()
        if not is_stomphacks_root(p):
            raise FileNotFoundError(
                f"Invalid STOMPHACKS_DIR '{p}': expected tools/, tools-pedal/, and zoom-zt2/"
            )
        return p

    cwd = Path.cwd().resolve()
    for cand in [cwd, *cwd.parents]:
        if is_stomphacks_root(cand):
            return cand
        sibling = cand / "stomphacks"
        if is_stomphacks_root(sibling):
            return sibling

    raise FileNotFoundError(
        "Could not locate a stomphacks checkout. Set STOMPHACKS_DIR, pass "
        "--stomphacks-dir, or run from inside a stomphacks directory."
    )


def find_ti_cgt_dir(stomphacks_dir: Path) -> Path | None:
    """Discover the TI C6000 CGT 8.5.0 LTS installation directory."""
    env_cgt = os.environ.get("ZOOM_TI_CGT")
    if env_cgt:
        p = Path(env_cgt).expanduser().resolve()
        if (p / "bin").is_dir():
            return p

    for cand in (
        stomphacks_dir / ".tools" / "ti-cgt-c6000_8.5.0.LTS",
        Path("/Applications/ti/ti-cgt-c6000_8.5.0.LTS"),
        Path("/opt/ti/c6000"),
    ):
        if (cand / "bin").is_dir():
            return cand

    return None


__all__ = [
    "__version__",
    "find_stomphacks_dir",
    "find_ti_cgt_dir",
    "is_stomphacks_root",
]
