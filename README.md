# zoom-faust-fx

> **[Try the Live WebAssembly Browser Auditioner →](https://nealstewart.github.io/zoom-faust-fx/)**

> [!WARNING]
> **Experimental software — use at your own risk.** This project compiles and loads unofficial bare-metal DSP binaries onto physical Zoom MultiStomp Plus pedals. Not all generated effects or compiler paths have been exhaustively validated on hardware, and bugs could crash, freeze, or otherwise affect your pedal. Always back up your pedal first (see `vendor/stomphacks/SAFETY.md`) and use this project strictly at your own risk.

First and foremost, a huge thank you to **thammer** for all the hard work that went into building [stomphacks](https://github.com/thammer/stomphacks). Reverse-engineering the Zoom MultiStomp Plus binary format, documenting the bare-metal TI TMS320C674x ABI and safety invariants, and creating the end-to-end `.ZD2` build, verification, and pedal-backup toolchain laid the entire foundation that makes custom effects on these pedals possible. This project consumes `stomphacks` directly as a Git submodule (`vendor/stomphacks`) and builds on top of that foundation.

The goal of **zoom-faust-fx** is to let you write custom audio effects for the Zoom **MS-70CDR+** (and compatible MS Plus pedals) in **[Faust](https://faust.grame.fr/)** (`.dsp`) instead of hand-writing bare-metal C674x C. It provides a Faust-to-ZD2 compiler (`tools/faust2zd2.py`) that enforces the pedal's real-time, call-free, and memory-budget constraints (including external SDRAM delay lines at `instance[3]`), pairs each effect with a custom 1-bit pixel-art pedal icon (`tools/pixel_icons.py`), and includes a local C test harness, C674x cycle/memory benchmarks, and a WebAssembly browser auditioner (`web/`) so you can design and test effects before ever loading them onto hardware.

---

## Defining a Zoom Faust Effect

A Zoom Faust effect is a single `.dsp` file (typically placed in `effects/<slug>/<slug>.dsp`) made up of three parts:

1. **`declare` metadata headers** — describe the `.ZD2` container, unique 32-bit effect ID, pedal display label, 2-character badge, and estimated DSP load.
2. **Faust UI controls (`hslider`, `nentry`, `checkbox`)** — automatically map to pedal knobs and `manifest.json` entries. Bracket tags control the knob order (`[1]`, `[2]`, …), display units (`[unit:Hz]`), built-in curves (`[curve:stock_vol]`), and help text (`[tooltip:...]`). Pipe knobs through `si.smoo` for click-free smoothing.
3. **The `process` DSP expression** — standard `stdfaust.lib` primitives (plus optional Zoom-tuned helpers from `import("zoom.lib");`), either mono (`process(x) = ...`, auto-expanded to stereo) or stereo (`process(l, r) = ...`).

### Minimal Example (`effects/warmflt/warmflt.dsp`)

```faust
declare name "WarmFlt";
declare zd2_filename "XXWRMFLT";
declare zd2_id "07001990";
declare version "1.00";
declare zd2_display "WARM FLT";
declare zd2_badge "WF";
declare description "Warm tanh saturator and resonant low-pass filter.";
declare zd2_dspload "8";

import("stdfaust.lib");

// Pedal knobs (mapped in order [1]..[4] to manifest.json)
drive = hslider("Drive[1][tooltip:Pre-filter saturation (0..100).]", 40, 0, 100, 1) : si.smoo;
freq  = hslider("Freq[2][unit:Hz][tooltip:Low-pass cutoff (180..7500 Hz).]", 1400, 180, 7500, 1) : si.smoo;
res   = hslider("Res[3][tooltip:Filter resonance Q (0.7..10).]", 3.0, 0.7, 10.0, 0.1) : si.smoo;
level = hslider("Level[4][curve:stock_vol][tooltip:Output level (80 is unity).]", 80, 0, 100, 1) : si.smoo;

// Unity-calibrated saturation for the MS Plus nominal bus (~0.25 RMS)
drive_gain  = 1.0 + drive * 0.12;
makeup      = 0.25 / ma.tanh(0.25 * drive_gain);
saturate(x) = ma.tanh(x * drive_gain) * makeup;

process(x) = x
    : fi.highpass(1, 95.0)
    : saturate
    : fi.svf_lp(freq, res)
    : fi.dcblocker
    : *(level);
```

### Optional Custom Pedal Icon (Image or `icon.py`)

Zoom MS Plus `.ZIC` icon files contain **two 1-bit monochrome frames** (black ink on white LCD):

| Frame | Size | Where it is shown |
|---|---|---|
| Browser tile | **`72 × 97` px** | Pedal effect browser, `.ZD2` container `ICON` section, and WebAssembly auditioner |
| Edit view | **`102 × 128` px** | Pedal parameter edit screen |

You can provide a custom icon beside your `.dsp` file in any of three ways (checked in priority order):

1. **Size-specific images** — `icon_72x97.png` and/or `icon_102x128.png` (also `.bmp`, `.gif`, `.webp`, `.jpg`) for pixel-exact art at each resolution.
2. **Single image** — `icon.png` (recommended `102 × 128` px or `72 × 97` px; transparent pixels composite onto white, and any other size is automatically resized and thresholded to 1-bit monochrome).
3. **Programmatic `icon.py`** — define `draw_art(d, box, sc)` to draw inside the stompbox window (with enclosure, footswitch, and label drawn automatically), or `render_icon(manifest, size)` for full canvas control:

```python
from pixel_icons import INK, draw_knob, scale

def draw_art(d, box, sc):
    x0, y0, x1, y1 = box
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    draw_knob(d, cx, cy, min(x1 - x0, y1 - y0) // 3, angle_deg=-45)
```

If none of these are present, a default stompbox icon is generated automatically.

---

## Compiling & Testing (`zoom-faust-fx` CLI & Library)

`zoom-faust-fx` is packaged as a standard Python package (`pyproject.toml`) providing both the `zoom-faust-fx` CLI and the reusable `zoom_faust_fx` library, connecting to a checked-out `stomphacks` repository via `--stomphacks-dir`, `STOMPHACKS_DIR`, or automatic current-directory/sibling detection.

### 1. Build a `.dsp` file or effect directory into `.ZD2` + `.ZIC`

```bash
export STOMPHACKS_DIR=/path/to/stomphacks

# Build a standalone .dsp file (runs C674x VLIW benchmark to populate manifest.json dspload)
zoom-faust-fx build effects/svfflt/svfflt.dsp

# Build an effect directory (outputs to effects/svfflt/build/ or --out <dir>)
zoom-faust-fx build effects/svfflt

# Skip the C674x VLIW benchmark step
zoom-faust-fx build effects/svfflt --no-benchmark
```

### 2. Benchmark, Verify Offline, and Install Safely

```bash
# Run standalone C674x VLIW & memory-hierarchy benchmark (supports --json)
zoom-faust-fx benchmark effects/svfflt/svfflt.dsp
zoom-faust-fx benchmark effects/ --json

# Verify CRC32, round-trip, container envelope, stock catalog, ZIC, and REPORT.md offline
zoom-faust-fx check effects/svfflt/build/XXSVFFLT.ZD2

# Install safely via stomphacks/tools-pedal/pedal_diy.py
zoom-faust-fx install effects/svfflt/build/XXSVFFLT.ZD2 --dry-run
```

### 3. Python Library API

```python
from pathlib import Path
from zoom_faust_fx import benchmark, builder, input_loader

spec = input_loader.load(Path("effects/svfflt/svfflt.dsp"), stomphacks_dir=Path("/path/to/stomphacks"))
for event in builder.build_steps(spec, run_benchmark=True):
    print(f"[{event.stage}] {event.status}: {event.message}")
    if event.result is not None:
        result = event.result

assert result.report_passed, result.report_summary
```

### 4. Tests & Browser Auditioner

```bash
# Run unit and integration tests
pixi run test

# Compile all effects to WebAssembly and launch the browser auditioner
pixi run web-build
pixi run web
```

---

## Setup

### Dependencies

- **Python 3.12+** (`pip install -e ".[server]"` to include FastAPI & Uvicorn for **Zoom Faust FX Studio**)
- **[Faust](https://faust.grame.fr/) (`faust` CLI)** — required for full `stdfaust.lib` compilation (`FAUST_REQUIRE_CLI=1` in Podman):
  - **Linux (Debian/Ubuntu):** `sudo apt-get install faust`
  - **macOS (Homebrew):** `brew install faust`
  - **Windows (PowerShell — downloads & runs the latest official `grame-cncm/faust` `win64.exe` installer):**
    ```powershell
    $asset = (Invoke-RestMethod "https://api.github.com/repos/grame-cncm/faust/releases/latest").assets | Where-Object { $_.name -match "win64\.exe$" } | Select-Object -First 1
    Invoke-WebRequest $asset.browser_download_url -OutFile "$env:TEMP\$($asset.name)"
    Start-Process "$env:TEMP\$($asset.name)" -Wait
    ```
    Then add `C:\Program Files\Faust\bin` to your `PATH` and verify with `faust --version`.
- **`stomphacks`** checkout (with `zoom-zt2` initialized) — set `STOMPHACKS_DIR=/path/to/stomphacks` (or pass `--stomphacks-dir`).
- **TI C6000 CGT `8.5.0.LTS`** — set `ZOOM_TI_CGT` to its install directory (or place it at `<stomphacks_dir>/.tools/ti-cgt-c6000_8.5.0.LTS`).
- **Podman** (optional, for running the containerized `linux/amd64` Studio server via `podman compose up`).

```bash
# Install zoom-faust-fx in editable mode (or via pipx)
pip install -e /path/to/zoom-faust-fx
# or directly from GitHub:
pip install "git+https://github.com/nealstewart/zoom-faust-fx.git"
```

---

## Trademarks & Affiliation

All product, hardware, and chip designations mentioned in documentation are the property of their respective owners. This project is an independent, clean-room DSP implementation with no affiliation with, sponsorship by, or endorsement from Zoom Corporation, GRAME, or any audio/hardware manufacturer.
