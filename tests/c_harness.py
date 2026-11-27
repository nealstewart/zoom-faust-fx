"""
tests/c_harness.py — Native C compilation and execution harness for testing
`C674X_FAUST_HEADER` math/delay intrinsics and generated Zoom MS Plus C kernels
(`SH_AUDIO_FN(void **instance, void **ctx)`) on the host CPU via `ctypes`.

Uses `x86_64-w64-mingw32-zig.exe clang` + `lld-link /DLL /NOENTRY` (from the
repo's `.pixi` environment) or standard `clang`/`gcc` to compile freestanding
call-free C kernels into native shared libraries with zero external CRT/libm
dependencies, and allocates SDRAM delay buffers (`instance[3]`) in the low
32-bit virtual address space (< 4 GB) so the 32-bit `uint32_t dd[3]` SDRAM
descriptor works unmodified on 64-bit hosts.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
TOOLS_DIR = ROOT / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

SH_FRAMES = 16
SH_CH_B_OFFSET = 16
SH_STATE_GUARD = 0x57464731


def find_zig_exe() -> Optional[Path]:
    """Locate the Zig binary in `.pixi` or on PATH."""
    pixi_roots = [ROOT, ROOT.parent / "stomphacks", ROOT / "vendor" / "stomphacks"]
    for base in pixi_roots:
        for rel in (
            Path(".pixi/envs/default/Library/bin/x86_64-w64-mingw32-zig.exe"),
            Path(".pixi/envs/default/Library/bin/zig.exe"),
            Path(".pixi/envs/default/bin/zig"),
        ):
            c = base / rel
            if c.is_file():
                return c
    w = shutil.which("zig") or shutil.which("x86_64-w64-mingw32-zig")
    return Path(w) if w else None


class NativeLib:
    """Wrapper around a loaded `ctypes.CDLL` that cleanly unloads on close."""

    def __init__(self, dll_path: Path, temp_dir: Path):
        self.dll_path = dll_path
        self.temp_dir = temp_dir
        self.lib = ctypes.CDLL(str(dll_path))
        self._closed = False

    def close(self) -> None:
        if not self._closed and self.lib is not None:
            self._closed = True
            if os.name == "nt":
                ctypes.windll.kernel32.FreeLibrary(ctypes.c_void_p(self.lib._handle))
            self.lib = None
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def __enter__(self) -> "NativeLib":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


_DLL_CACHE_DIR: Optional[Path] = None
_LOADED_LIBS: Dict[str, ctypes.CDLL] = {}


def compile_c_to_dll(
    c_source: str,
    include_dirs: Optional[List[Path]] = None,
    extra_defines: Optional[List[str]] = None,
) -> ctypes.CDLL:
    """
    Compile freestanding C source code into a native shared library (`.dll`/`.so`)
    and return the loaded `ctypes.CDLL`. Cached by content hash across the test run.
    """
    global _DLL_CACHE_DIR
    inc_Paths = [str(p.resolve()) for p in (include_dirs or [])]
    defs = list(extra_defines or [])
    key_str = c_source + "\n||INC:" + "|".join(inc_Paths) + "\n||DEF:" + "|".join(defs)
    digest = hashlib.sha256(key_str.encode("utf-8")).hexdigest()[:16]

    if digest in _LOADED_LIBS:
        return _LOADED_LIBS[digest]

    if _DLL_CACHE_DIR is None:
        _DLL_CACHE_DIR = Path(tempfile.mkdtemp(prefix="stomphacks_c_test_"))

    work_dir = _DLL_CACHE_DIR / digest
    work_dir.mkdir(parents=True, exist_ok=True)

    # Prepend `int _fltused = 0;` for freestanding MSVC target compatibility
    full_source = "int _fltused = 0;\n" + c_source
    c_file = work_dir / "module.c"
    c_file.write_text(full_source, encoding="utf-8")

    zig_exe = find_zig_exe()
    if os.name == "nt" and zig_exe is not None:
        obj_file = work_dir / "module.obj"
        dll_file = work_dir / "module.dll"
        zig_inc = zig_exe.parent.parent / "lib" / "zig" / "include"
        cmd_compile = [
            str(zig_exe),
            "clang",
            "-target",
            "x86_64-pc-windows-msvc",
            "-ffreestanding",
            "-c",
            "-O2",
            "-Wno-unused-value",
            "-Wno-parentheses-equality",
        ]
        if zig_inc.is_dir():
            cmd_compile.extend(["-isystem", str(zig_inc)])
        for inc in inc_Paths:
            cmd_compile.extend(["-I", inc])
        for d in defs:
            cmd_compile.append(f"-D{d}")
        cmd_compile.extend([str(c_file), "-o", str(obj_file)])
        res_c = subprocess.run(cmd_compile, capture_output=True, text=True)
        if res_c.returncode != 0:
            raise RuntimeError(f"zig clang failed:\n{res_c.stderr}\n{res_c.stdout}")

        cmd_link = [
            str(zig_exe),
            "lld-link",
            "/DLL",
            "/NOENTRY",
            str(obj_file),
            f"/OUT:{dll_file}",
        ]
        res_l = subprocess.run(cmd_link, capture_output=True, text=True)
        if res_l.returncode != 0:
            raise RuntimeError(f"zig lld-link failed:\n{res_l.stderr}\n{res_l.stdout}")
    else:
        ext = ".dll" if os.name == "nt" else ".so"
        dll_file = work_dir / f"module{ext}"
        cc = shutil.which("clang") or shutil.which("gcc") or shutil.which("cc")
        if not cc:
            raise RuntimeError("No C compiler (zig, clang, gcc) found for C unit tests.")
        cmd = [cc, "-shared", "-fPIC", "-O2"]
        for inc in inc_Paths:
            cmd.extend(["-I", inc])
        for d in defs:
            cmd.append(f"-D{d}")
        cmd.extend([str(c_file), "-o", str(dll_file)])
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    lib = ctypes.CDLL(str(dll_file))
    _LOADED_LIBS[digest] = lib
    return lib


class Low4GBBuffer:
    """
    Allocates a raw memory block in the low 32-bit virtual address space (< 4 GB)
    so 64-bit host tests can populate the Zoom MS Plus 32-bit SDRAM descriptor
    `uint32_t dd[3] = { base_addr, end_addr, size_bytes }` and let unmodified C
    kernels cast `(uintptr_t)dd[0]` back to a valid `float *` pointer.
    """

    def __init__(self, size_bytes: int = 131072):
        # Round up to 64 KB allocation granularity
        self.size_bytes = ((size_bytes + 65535) // 65536) * 65536
        self.base_addr: int = 0
        self._buf_fallback = None

        if os.name == "nt":
            k32 = ctypes.windll.kernel32
            k32.VirtualAlloc.argtypes = [
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.c_uint32,
                ctypes.c_uint32,
            ]
            k32.VirtualAlloc.restype = ctypes.c_void_p
            MEM_COMMIT_RESERVE = 0x1000 | 0x2000
            PAGE_READWRITE = 0x04
            for hint in range(0x20000000, 0x70000000, 0x01000000):
                ptr = k32.VirtualAlloc(hint, self.size_bytes, MEM_COMMIT_RESERVE, PAGE_READWRITE)
                if ptr and ptr < 0x100000000:
                    self.base_addr = int(ptr)
                    break
            if not self.base_addr:
                raise RuntimeError("VirtualAlloc failed to allocate buffer in low 4 GB address space.")
        else:
            # On POSIX, try mmap with MAP_32BIT (0x40 on Linux x86_64) or low hint address
            self._buf_fallback = (ctypes.c_uint8 * self.size_bytes)()
            addr = ctypes.addressof(self._buf_fallback)
            if addr >= 0x100000000:
                raise RuntimeError("Fallback buffer address exceeds 32-bit range on non-Windows host.")
            self.base_addr = addr

    def zero(self) -> None:
        if self.base_addr:
            ctypes.memset(self.base_addr, 0, self.size_bytes)

    def read_floats(self, offset_floats: int, count: int) -> List[float]:
        arr_type = ctypes.c_float * count
        arr = arr_type.from_address(self.base_addr + offset_floats * 4)
        return list(arr)

    def close(self) -> None:
        if os.name == "nt" and self.base_addr:
            k32 = ctypes.windll.kernel32
            k32.VirtualFree.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
            k32.VirtualFree.restype = ctypes.c_int
            k32.VirtualFree(self.base_addr, 0, 0x8000)
            self.base_addr = 0

    def __enter__(self) -> "Low4GBBuffer":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()


def synthesize_sh_params_h(manifest: dict) -> str:
    """
    Generate a standalone `sh_params.h` from a `manifest.json` dict (matching
    `tools/zd2_make_effect.py`'s header layout) so any kernel C file can be
    compiled without needing a prior `build/` directory.
    """
    sym = re.sub(r"[^A-Za-z0-9]", "", manifest["name"])
    fn_name = f"Fx_SFX_{sym}"
    state_bytes = int(manifest.get("state_bytes", 16))
    guard_word = state_bytes // 4

    free_slots = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
    param_defines = []
    edit_c_defines = []
    slot_idx = 0
    for idx, p in enumerate(manifest.get("params", [])):
        p_name = p["name"]
        macro = "SH_PARAM_" + re.sub(r"[^A-Za-z0-9]+", "_", p_name.strip()).strip("_").upper()
        slot = p.get("slot", free_slots[slot_idx])
        slot_idx += 1
        param_defines.append(f"#define {macro} {slot}")
        k = idx + 2
        emacro = "SH_EDIT_" + re.sub(r"[^A-Za-z0-9]", "_", p_name).upper()
        edit_c_defines.append(f"#define {emacro} Aw_P{k}_edit_c")
        edit_c_defines.append(f"void Aw_P{k}_edit_c(int click, float *coeff, void **ctx, int slot);")

    return f"""#ifndef SH_PARAMS_H
#define SH_PARAMS_H
#include <stdint.h>

#define SH_AUDIO_FN {fn_name}
void {fn_name}(void **instance, void **ctx);

#define SH_FRAMES        16
#define SH_CH_B_OFFSET   16
#define SH_COEFF_BYPASS  0
#define SH_CTX_GTRIN     0
#define SH_CTX_EFF       1
#define SH_CTX_OUT       2
#define SH_CTX_SHUTTLE   7
#define SH_CTX_LEGACY_IN 8
#define SH_SAMPLE_RATE   44100.0f
#define SH_STATE_BYTES   {state_bytes}
#define SH_STATE_GUARD       0x57464731u
#define SH_STATE_GUARD_WORD  {guard_word}
#define SH_NUM_PARAMS    {len(param_defines)}
{chr(10).join(param_defines)}

typedef int (*sh_note_to_ms_fn)(int note_idx, int max_ms);

static inline int sh_note_to_ms(void **ctx, int note_idx, int max_ms) {{
    sh_note_to_ms_fn fn = ctx ? (sh_note_to_ms_fn)ctx[34] : (sh_note_to_ms_fn)0;
    if (!fn) return 0;
    int ms = fn(note_idx, max_ms);
    return (ms > 0 && ms <= max_ms) ? ms : 0;
}}

typedef void (*sh_ramp_f32_fn)(float *dest, float target, float rate);
typedef void (*sh_ramp_i32_fn)(int32_t *dest, int32_t target, int32_t rate);

static inline void sh_ramp_f32(void **ctx, float *dest, float target, float rate) {{
    sh_ramp_f32_fn fn = ctx ? (sh_ramp_f32_fn)ctx[3] : (sh_ramp_f32_fn)0;
    if (fn) fn(dest, target, rate); else *dest = target;
}}

static inline void sh_smooth_f32(void **ctx, float *dest, float target, float rate) {{
    sh_ramp_f32_fn fn = ctx ? (sh_ramp_f32_fn)ctx[4] : (sh_ramp_f32_fn)0;
    if (fn) fn(dest, target, rate); else *dest = target;
}}

static inline void sh_ramp_i32(void **ctx, int32_t *dest, int32_t target, int32_t rate) {{
    sh_ramp_i32_fn fn = ctx ? (sh_ramp_i32_fn)ctx[5] : (sh_ramp_i32_fn)0;
    if (fn) fn(dest, target, rate); else *dest = target;
}}

static inline void sh_smooth_i32(void **ctx, int32_t *dest, int32_t target, int32_t rate) {{
    sh_ramp_i32_fn fn = ctx ? (sh_ramp_i32_fn)ctx[6] : (sh_ramp_i32_fn)0;
    if (fn) fn(dest, target, rate); else *dest = target;
}}

#define SH_BIND_EDIT(target, fn) \\
    void target(int click, float *coeff, void **ctx, int slot) {{ \\
        fn(click, coeff, ctx, slot); \\
    }}

{chr(10).join(edit_c_defines)}

#endif
"""


class KernelHarness:
    """
    Compiles a Zoom MS Plus C kernel (`<effect>.c` + `manifest.json`) into a
    native DLL and provides an in-memory pedal execution harness (`instance[0..3]`,
    `ctx[0..8]`, `coef[0..15]`, `state` with `SH_STATE_GUARD`, `eff[32]`, and
    low-4GB SDRAM `dd[3]`).
    """

    def __init__(
        self,
        kernel_c_source: str,
        manifest: dict,
        sdram_size_bytes: int = 705600,
    ):
        self.manifest = manifest
        self.sym = re.sub(r"[^A-Za-z0-9]", "", manifest["name"])
        self.fn_name = f"Fx_SFX_{self.sym}"
        self.state_bytes = int(manifest.get("state_bytes", 16))
        self.guard_word = self.state_bytes // 4

        sh_params_h = synthesize_sh_params_h(manifest)
        # Inline sh_params.h directly and export the audio entry point
        patched_c = kernel_c_source.replace('#include "sh_params.h"', sh_params_h)
        patched_c += f"\n__declspec(dllexport) void run_kernel(void **instance, void **ctx) {{ {self.fn_name}(instance, ctx); }}\n"

        self.lib = compile_c_to_dll(patched_c)
        self._run_fn = self.lib.run_kernel
        self._run_fn.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self._run_fn.restype = None

        # Allocate ABI buffers
        self.coef = (ctypes.c_float * 16)()
        # State buffer: state_bytes + 4 bytes for SH_STATE_GUARD + 16 bytes padding
        self.state_words_len = self.guard_word + 8
        self.state_words = (ctypes.c_uint32 * self.state_words_len)()
        self.eff = (ctypes.c_float * 32)()
        self.gtrin = (ctypes.c_float * 16)()

        self.sdram = Low4GBBuffer(sdram_size_bytes)
        self.dd = (ctypes.c_uint32 * 3)()

        self.instance = (ctypes.c_void_p * 4)()
        self.ctx = (ctypes.c_void_p * 45)()
        self.lfo_ramp_sink = ctypes.c_int32(0)
        self.lfo_ramp_dst = ctypes.c_void_p(ctypes.addressof(self.lfo_ramp_sink))
        self.lfo_ramp_buf = (ctypes.c_int32 * 16)()

        self.param_slots: Dict[str, int] = {}
        self.reset()

    def reset(self) -> None:
        """Reset state, SDRAM, and initialize coefficients to manifest defaults."""
        ctypes.memset(self.state_words, 0, ctypes.sizeof(self.state_words))
        self.state_words[self.guard_word] = SH_STATE_GUARD

        ctypes.memset(self.coef, 0, ctypes.sizeof(self.coef))
        self.coef[0] = 1.0  # SH_COEFF_BYPASS = 1.0 (100% active)
        self.coef[15] = 500.0  # SH_COEFF_BPM = 500.0 ms (120 BPM)

        free_slots = [5, 6, 7, 8, 9, 10, 11, 12, 13, 14]
        slot_idx = 0
        icoef_view = ctypes.cast(self.coef, ctypes.POINTER(ctypes.c_int32))
        for p in self.manifest.get("params", []):
            slot = p.get("slot", free_slots[slot_idx])
            slot_idx += 1
            self.param_slots[p["name"]] = slot
            p_max = p.get("max", len(p["values"]) - 1 if "values" in p else 100)
            p_def = p.get("default", 0)
            if p.get("q31_default") is not None:
                icoef_view[slot] = int(p["q31_default"])
            elif p.get("lfo_sync"):
                ls = p["lfo_sync"]
                min_dhz = int(ls["min_dhz"])
                max_dhz = int(ls["max_dhz"])
                step_dhz = int(ls.get("step_dhz", 1))
                hz_max_click = (max_dhz - min_dhz) // step_dhz
                hz = (min_dhz + p_def * step_dhz) * 0.1 if p_def <= hz_max_click else 2.0
                icoef_view[slot] = int(round(hz * 97391.548))
            elif p.get("curve") == "stock_vol":
                # Stock VOL curve: v/80 for v <= 80, 1 + (v-80)/40 above
                val = (p_def / 80.0) if p_def <= 80 else (1.0 + (p_def - 80.0) / 40.0)
                self.coef[slot] = val
            else:
                val = (p_def / float(p_max)) if p_max > 0 else 0.0
                self.coef[slot] = val

        self.sdram.zero()
        self.dd[0] = self.sdram.base_addr
        self.dd[1] = self.sdram.base_addr + self.sdram.size_bytes
        self.dd[2] = self.sdram.size_bytes

        self.instance[0] = 0
        self.instance[1] = ctypes.cast(self.coef, ctypes.c_void_p)
        self.instance[2] = ctypes.cast(self.state_words, ctypes.c_void_p)
        self.instance[3] = ctypes.cast(self.dd, ctypes.c_void_p)

        ctypes.memset(self.ctx, 0, ctypes.sizeof(self.ctx))
        ctypes.memset(self.lfo_ramp_buf, 0, ctypes.sizeof(self.lfo_ramp_buf))
        self.ctx[0] = ctypes.cast(self.gtrin, ctypes.c_void_p)
        self.ctx[1] = ctypes.cast(self.eff, ctypes.c_void_p)
        self.ctx[9] = ctypes.cast(ctypes.byref(self.lfo_ramp_dst), ctypes.c_void_p)
        self.ctx[10] = ctypes.cast(self.lfo_ramp_buf, ctypes.c_void_p)

    def set_param_norm(self, name: str, norm_val: float) -> None:
        self.coef[self.param_slots[name]] = norm_val

    def set_bypass_fade(self, fade: float) -> None:
        self.coef[0] = fade

    def set_bpm(self, bpm: float) -> None:
        self.coef[15] = 60000.0 / max(30.0, min(400.0, float(bpm)))

    def run_block(
        self,
        in_l: Optional[List[float]] = None,
        in_r: Optional[List[float]] = None,
    ) -> Tuple[List[float], List[float]]:
        """Run one 16-frame stereo block in-place through `SH_AUDIO_FN`."""
        if in_l is not None:
            for i in range(SH_FRAMES):
                self.eff[i] = in_l[i] if i < len(in_l) else 0.0
        if in_r is not None:
            for i in range(SH_FRAMES):
                self.eff[i + SH_CH_B_OFFSET] = in_r[i] if i < len(in_r) else 0.0
        elif in_l is not None:
            for i in range(SH_FRAMES):
                self.eff[i + SH_CH_B_OFFSET] = self.eff[i]

        self._run_fn(self.instance, self.ctx)
        out_l = [float(self.eff[i]) for i in range(SH_FRAMES)]
        out_r = [float(self.eff[i + SH_CH_B_OFFSET]) for i in range(SH_FRAMES)]
        return out_l, out_r

    def close(self) -> None:
        self.sdram.close()

    def __enter__(self) -> "KernelHarness":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()
