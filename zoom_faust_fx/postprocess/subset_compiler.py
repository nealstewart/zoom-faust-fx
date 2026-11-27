"""
tools/faust2zd2_pkg/subset_compiler.py — Mode 2: Built-in clean-room FAUST `.dsp`
subset transpiler to bare-metal C674x Zoom MS Plus kernel parts.

STRICT STANDARD-FAUST POLICY:
  - `faust2zd2_pkg` only implements standard Faust language syntax (block-diagram
    algebra `:`, `,`, `<:`, `:>`, `~`, pattern macros `seq`, `par`, `sum`, `prod`,
    `\\(x).(body)` lambdas, `with { ... }`, `'`, `@`, `^`, `%`) and standard
    `stdfaust.lib` functions (`de.*`, `fi.*`, `si.*`, `os.*`, `no.*`, `an.*`,
    `co.*`, `ef.*`, `ba.*`, `ma.*`, `aa.*`, `sp.*`).
  - Never add custom or non-standard Faust library extensions to this package.
  - Multiple standard `de.delay` / `de.fdelay` / `de.fdelay1` / `de.fdelay3`
    taps that read from the same input signal expression automatically share a
    single underlying SDRAM circular buffer (standard Faust common-delay-line
    sharing), so multi-tap delays, pitch shifters, flangers, and reverbs can be
    written with standard `de.fdelay(max_len, d, sig)` / `de.delay(max_len, d, sig)`.
"""

from __future__ import annotations

import itertools
import math
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .emitter import _emit_c674x_kernel_from_parts
from .models import (
    MAX_DELAY_LINES,
    MAX_PATTERN_COUNT,
    MAX_SAFE_SDRAM_BYTES,
    MAX_SAMPLE_TEMPS,
    MAX_STATE_BYTES,
    FaustMemoryLimitError,
    FaustMemoryReport,
    FaustMeta,
    FaustParam,
    parse_faust_dsp_metadata,
    strip_c_comments,
)
from .rewriter import rewrite_c_divisions_and_math


class _TrackedLoopLines(list):
    """List subclass that tracks per-sample local temporary declarations per active function."""

    def __init__(self, compiler: "FaustSubsetCompiler"):
        super().__init__()
        self._compiler = compiler

    def append(self, line: str) -> None:
        super().append(line)
        self._compiler._on_loop_line_added(line)


class FaustSubsetCompiler:
    """
    Compiles a structured FAUST `.dsp` file directly into C674x kernel parts:
      - Deduplicates and hoists parameter smoothers (`si.smoo`) to control-rate
        (with first-block snap-to-target initialization so init/chain-edits never
        sweep from 0 Hz / 0 volume).
      - Hoists control-dependent filter coefficients (`sh_sinf`, `sh_rcp`, `sh_sqrtf`)
        out of the 16-sample loop so the sample loop contains only fast register math.
      - Supports external SDRAM delay lines (`de.fdelay`, `de.fdelay1`, `de.fdelay3`,
        `de.delay`, `de.sdelay`, `fi.allpass_comb`, `fi.fb_fcomb`) with automatic
        common-delay-line sharing when multiple taps read from the same signal.
      - Resolves standard Faust `import("file.lib");` statements against the effect
        directory and `effects/` (while `import("stdfaust.lib");` provides the standard
        library primitives).
      - Uses scalar variables (`in0`, `in1`, `out0`, `out1`) with zero stack-array
        pointer aliasing.
    """

    def __init__(
        self,
        dsp_text: str,
        meta: FaustMeta,
        params: List[FaustParam],
        search_dirs: Optional[List[Path]] = None,
    ):
        self.search_dirs = list(search_dirs or [])
        self.dsp_text = self._resolve_imports(strip_c_comments(dsp_text))
        self.meta = meta
        self.params = params
        self.state_vars: List[Tuple[str, str, int]] = []
        self.smoo_bindings: List[Tuple[str, str]] = []  # (state_var_name, target_expr)
        self.pre_lines: List[str] = []
        self._call_stack: List[str] = ["process"]
        self.fn_invocations: Dict[str, int] = {"process": 1}
        self.fn_state_words: Dict[str, int] = {}
        self.fn_sdram_lines: Dict[str, int] = {}
        self.fn_sdram_bytes: Dict[str, int] = {}
        self.fn_temp_vars: Dict[str, int] = {}
        self.memory_report: Optional[FaustMemoryReport] = None
        self.loop_lines: List[str] = _TrackedLoopLines(self)
        self._state_counter = 0
        self._smoo_cache: Dict[str, str] = {}
        self._pre_cache: Dict[str, str] = {}
        self._pre_var_to_expr: Dict[str, str] = {}
        self._sig_cache: Dict[str, str] = {}
        self._noise_cache: Dict[str, str] = {}
        self.uses_sdram_delay: bool = False
        self.delay_wpos_var: Optional[str] = None
        self.delay_line_count: int = 0
        self.delay_line_len: int = 1024
        self.delay_lines_map: Dict[str, int] = {}
        self.delay_line_requested_lens: Dict[int, int] = {}
        self.delay_line_owners: Dict[int, str] = {}
        self.delay_written_signals: Dict[str, int] = {}
        self.delay_step_expr: Optional[str] = None
        self.logical_sdram_floats: int = 0

    @property
    def current_fn(self) -> str:
        return self._call_stack[-1] if self._call_stack else "process"

    def _push_fn(self, fn_name: str) -> None:
        if self._call_stack.count(fn_name) > 8:
            chain = " -> ".join(self._call_stack + [fn_name])
            raise FaustMemoryLimitError(
                f"Static memory analysis failed: unbounded recursive function invocation cycle detected ({chain})."
            )
        self._call_stack.append(fn_name)
        self.fn_invocations[fn_name] = self.fn_invocations.get(fn_name, 0) + 1

    def _pop_fn(self) -> None:
        if len(self._call_stack) > 1:
            self._call_stack.pop()

    def _on_loop_line_added(self, line: str) -> None:
        if re.match(r"^\s*(?:float|int|uint32_t|FAUSTFLOAT)\s+[A-Za-z_][A-Za-z0-9_]*\s*=", line):
            fn = self.current_fn
            self.fn_temp_vars[fn] = self.fn_temp_vars.get(fn, 0) + 1
            total_temps = sum(self.fn_temp_vars.values())
            if total_temps > MAX_SAMPLE_TEMPS:
                report = self.build_memory_report()
                raise FaustMemoryLimitError(
                    f"Static memory analysis failed: FAUST script emits {total_temps} per-sample local "
                    f"temporary variables ({total_temps * 4} B) across function invocations (currently in "
                    f"`{fn}`), exceeding the {MAX_SAMPLE_TEMPS}-variable ({MAX_SAMPLE_TEMPS * 4} B) "
                    f"C674x stack/register spill limit.\n{report.format_breakdown()}"
                )

    def build_memory_report(self) -> FaustMemoryReport:
        total_state_words = sum(alen for _, _, alen in self.state_vars) + 1  # +1 for init_magic
        state_bytes = max(16, ((total_state_words * 4 + 7) // 8) * 8)
        if total_state_words * 4 <= MAX_STATE_BYTES:
            state_bytes = min(MAX_STATE_BYTES, state_bytes)
        else:
            state_bytes = total_state_words * 4

        p2_len = 0
        phys_sdram = 0
        if self.delay_line_count > 0:
            p2_len = 1024
            while p2_len < self.delay_line_len:
                p2_len *= 2
            phys_sdram = self.delay_line_count * (p2_len + 16) * 4

        logical_sdram = sum(self.delay_line_requested_lens.values()) * 4
        temp_count = sum(
            1
            for l in self.loop_lines
            if re.match(r"^\s*(?:float|int|uint32_t|FAUSTFLOAT)\s+[A-Za-z_][A-Za-z0-9_]*\s*=", l)
        )
        return FaustMemoryReport(
            state_words=total_state_words,
            state_bytes=state_bytes,
            delay_line_count=self.delay_line_count,
            delay_line_len=p2_len,
            logical_sdram_bytes=logical_sdram,
            physical_sdram_bytes=phys_sdram,
            sample_temp_count=temp_count,
            sample_temp_bytes=temp_count * 4,
            function_invocations=dict(self.fn_invocations),
            function_state_words=dict(self.fn_state_words),
            function_sdram_lines=dict(self.fn_sdram_lines),
            function_sdram_bytes=dict(self.fn_sdram_bytes),
            function_temp_vars=dict(self.fn_temp_vars),
        )

    def _validate_memory_budgets(self, context_msg: str = "") -> FaustMemoryReport:
        report = self.build_memory_report()
        self.memory_report = report
        ctx = f" ({context_msg})" if context_msg else ""

        if report.state_words * 4 > MAX_STATE_BYTES:
            raise FaustMemoryLimitError(
                f"Static memory analysis failed{ctx}: FAUST DSP requires {report.state_words * 4} bytes "
                f"({report.state_words} words) of persistent state across function invocations, which "
                f"exceeds the Zoom MS Plus {MAX_STATE_BYTES}-byte instance[2] limit.\n"
                f"{report.format_breakdown()}"
            )
        if report.delay_line_count > MAX_DELAY_LINES:
            raise FaustMemoryLimitError(
                f"Static memory analysis failed{ctx}: FAUST DSP allocates {report.delay_line_count} "
                f"parallel SDRAM delay lines across function invocations, exceeding the "
                f"{MAX_DELAY_LINES}-line C6745 L1D/L2 cache-set thrashing limit.\n"
                f"{report.format_breakdown()}"
            )
        if report.logical_sdram_bytes > MAX_SAFE_SDRAM_BYTES:
            raise FaustMemoryLimitError(
                f"Static memory analysis failed{ctx}: cumulative requested delay memory across function "
                f"invocations is {report.logical_sdram_bytes} bytes, exceeding the {MAX_SAFE_SDRAM_BYTES:,}-byte "
                f"(3.0 s @ 44.1 kHz) instance[3] SDRAM safety cap.\n"
                f"{report.format_breakdown()}"
            )
        if report.physical_sdram_bytes > MAX_SAFE_SDRAM_BYTES:
            raise FaustMemoryLimitError(
                f"Static memory analysis failed{ctx}: FAUST DSP requires {report.physical_sdram_bytes} bytes "
                f"({report.delay_line_count} x {report.delay_line_len + 16}-float strided lines) of "
                f"instance[3] SDRAM delay memory across function invocations, which exceeds the "
                f"529,200-byte (3.0 s @ 44.1 kHz) safety cap.\n"
                f"{report.format_breakdown()}"
            )
        if report.sample_temp_count > MAX_SAMPLE_TEMPS:
            raise FaustMemoryLimitError(
                f"Static memory analysis failed{ctx}: FAUST DSP emits {report.sample_temp_count} per-sample "
                f"local temporary variables ({report.sample_temp_bytes} bytes) across function invocations, "
                f"exceeding the {MAX_SAMPLE_TEMPS}-variable ({MAX_SAMPLE_TEMPS * 4} B) stack/register spill limit.\n"
                f"{report.format_breakdown()}"
            )
        return report

    def _resolve_imports(self, text: str) -> str:
        """Resolve standard Faust `import("...");` directives (except built-in `stdfaust.lib`)."""
        seen = set()
        imp_re = re.compile(r'import\s*\(\s*"([^"]+)"\s*\)\s*;')
        for _ in range(16):
            changed = False
            for m in list(imp_re.finditer(text)):
                fname = m.group(1).strip()
                if fname == "stdfaust.lib" or fname.endswith("stdfaust.lib"):
                    continue
                if fname in seen:
                    text = text[: m.start()] + text[m.end() :]
                    changed = True
                    break
                found_path: Optional[Path] = None
                for sdir in self.search_dirs:
                    cand = (sdir / fname).resolve()
                    if cand.is_file():
                        found_path = cand
                        break
                if found_path is None:
                    raise ValueError(
                        f"Could not resolve Faust import(\"{fname}\") in search directories: {self.search_dirs}"
                    )
                seen.add(fname)
                lib_text = strip_c_comments(found_path.read_text(encoding="utf-8"))
                lib_text = re.sub(r"\bzm_(?:fsw_active|is_unlatch|bpm)\s*=\s*[^;]+;", "", lib_text)
                text = text[: m.start()] + "\n" + lib_text + "\n" + text[m.end() :]
                changed = True
                break
            if not changed:
                break
        return text

    def ensure_delay_wpos(self) -> str:
        if self.delay_wpos_var is None:
            self.uses_sdram_delay = True
            self.delay_wpos_var = self.alloc_state("iWpos", 1, ctype="uint32_t")
        return self.delay_wpos_var

    def alloc_delay_line(self, tag: str, min_len: int = 1024) -> int:
        self.ensure_delay_wpos()
        if min_len <= 0:
            raise FaustMemoryLimitError(
                f"Static memory analysis failed in `{self.current_fn}`: delay line length must be positive (got {min_len})."
            )
        if min_len > getattr(self, "delay_line_len", 1024):
            # Round up to next power of two (e.g., 4096 for multi-voice pitch shifters)
            p2 = 1024
            while p2 < min_len:
                p2 *= 2
            self.delay_line_len = p2
        fn = self.current_fn
        if tag not in self.delay_lines_map:
            idx = self.delay_line_count
            self.delay_line_count += 1
            self.delay_lines_map[tag] = idx
            self.delay_line_requested_lens[idx] = min_len
            self.delay_line_owners[idx] = fn
            self.fn_sdram_lines[fn] = self.fn_sdram_lines.get(fn, 0) + 1
            self.fn_sdram_bytes[fn] = self.fn_sdram_bytes.get(fn, 0) + min_len * 4
        else:
            idx = self.delay_lines_map[tag]
            old_len = self.delay_line_requested_lens.get(idx, 1024)
            if min_len > old_len:
                owner = self.delay_line_owners.get(idx, fn)
                self.delay_line_requested_lens[idx] = min_len
                self.fn_sdram_bytes[owner] = self.fn_sdram_bytes.get(owner, 0) + (min_len - old_len) * 4
        self._validate_memory_budgets(f"in `{fn}` allocating SDRAM delay line")
        return self.delay_lines_map[tag]

    def _update_shared_delay_line_len(self, line_idx: int, max_l: int) -> None:
        if max_l > self.delay_line_len:
            p2 = 1024
            while p2 < max_l:
                p2 *= 2
            self.delay_line_len = p2
        old_len = self.delay_line_requested_lens.get(line_idx, 1024)
        if max_l > old_len:
            owner = self.delay_line_owners.get(line_idx, self.current_fn)
            self.delay_line_requested_lens[line_idx] = max_l
            self.fn_sdram_bytes[owner] = self.fn_sdram_bytes.get(owner, 0) + (max_l - old_len) * 4
        self._validate_memory_budgets(f"in `{self.current_fn}` expanding shared SDRAM delay line")

    def alloc_state(self, prefix: str, length: int = 1, ctype: str = "float") -> str:
        name = f"{prefix}_{self._state_counter}"
        self._state_counter += 1
        self.state_vars.append((ctype, name, length))
        fn = self.current_fn
        self.fn_state_words[fn] = self.fn_state_words.get(fn, 0) + length
        self._validate_memory_budgets(f"in `{fn}` allocating persistent state `{prefix}`")
        return name

    def hoist_block_expr(self, prefix: str, c_expr: str) -> str:
        """Hoist a block-invariant (control-rate) expression before the 16-sample loop,
        or constant-fold pure-literal expressions directly to float literals (0 registers)."""
        c_expr = c_expr.strip()
        if (
            self._is_control_rate(c_expr)
            and not self._has_control_var(c_expr)
            and not re.search(r"\b(?:k_white_noise|k_pink_noise)\b|dsp->", c_expr)
        ):
            lo, hi = self._estimate_expr_bounds(c_expr)
            if lo is not None and hi is not None and lo == hi and math.isfinite(lo):
                lit = f"{lo:.8g}"
                if "." not in lit and "e" not in lit and "E" not in lit:
                    lit += ".0"
                lit += "f"
                return f"({lit})" if lo < 0.0 else lit
        if c_expr in self._pre_cache:
            return self._pre_cache[c_expr]
        vname = f"{prefix}_{len(self._pre_cache)}"
        self._pre_cache[c_expr] = vname
        self._pre_var_to_expr[vname] = c_expr
        self.pre_lines.append(f"float {vname} = {c_expr};")
        return vname

    def compile(self) -> Tuple[str, int, List[FaustParam]]:
        body = self.dsp_text
        body = re.sub(r'declare\s+[A-Za-z0-9_]+\s+"[^"]*"\s*;', "", body)
        body = re.sub(r'import\s*\(\s*"[^"]*"\s*\)\s*;', "", body)

        ui_re = re.compile(
            r'\b(?:(?:hslider|vslider|nentry)\s*\(\s*"([^"]+)"\s*,'
            r'\s*[^,]+\s*,\s*[^,]+\s*,\s*[^,]+\s*,\s*[^\)]+\)'
            r'|(?:checkbox|button)\s*\(\s*"([^"]+)"\s*\))'
        )

        def repl_slider(m: re.Match) -> str:
            raw_label = m.group(1) or m.group(2) or ""
            clean_label = re.sub(r"\[[^\]]*\]", "", raw_label).strip()[:12]
            for p in self.params:
                if p.label == clean_label:
                    return p.var_name
            return "0.0f"

        body = ui_re.sub(repl_slider, body)
        body = self._expand_pattern_macros(body)

        defs: Dict[str, Tuple[List[str], str]] = {}
        body = self._extract_lambdas(body, defs)
        self.with_map: Dict[str, List[Tuple[str, List[str], str]]] = {}
        for stmt in self._split_top_statements(body):
            stmt = stmt.strip()
            if not stmt or "=" not in stmt:
                continue
            lhs, rhs = stmt.split("=", 1)
            lhs = lhs.strip()
            rhs = rhs.strip()

            m_with = re.search(r"\bwith\s*\{(.*)\}\s*$", rhs, re.S)
            with_locals: List[Tuple[str, List[str], str]] = []
            if m_with:
                with_body = m_with.group(1)
                rhs = rhs[: m_with.start()].strip()
                for wstmt in self._split_top_statements(with_body):
                    wstmt = wstmt.strip()
                    if not wstmt or "=" not in wstmt:
                        continue
                    wlhs, wrhs = wstmt.split("=", 1)
                    wlhs, wrhs = wlhs.strip(), wrhs.strip()
                    wm_fn = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)$", wlhs)
                    if wm_fn:
                        wname = wm_fn.group(1)
                        wargs = [a.strip() for a in wm_fn.group(2).split(",") if a.strip()]
                    else:
                        wname, wargs = wlhs, []
                    with_locals.append((wname, wargs, wrhs))

            m_fn = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)$", lhs)
            if m_fn:
                fname = m_fn.group(1)
                args = [a.strip() for a in m_fn.group(2).split(",") if a.strip()]
            else:
                fname, args = lhs, []

            if with_locals:
                self.with_map[fname] = with_locals
                for wname, wargs, wrhs in with_locals:
                    if wargs:
                        defs[wname] = (wargs, wrhs)

            if not args and rhs in defs and defs[rhs][0]:
                defs[fname] = defs[rhs]
            else:
                defs[fname] = (args, rhs)

        if "process" not in defs:
            raise ValueError("FAUST `.dsp` file must define `process = ...;`.")

        self._compile_definitions(defs)
        self._validate_memory_budgets("final static memory check")

        return _emit_c674x_kernel_from_parts(
            self.meta,
            self.params,
            self.state_vars,
            self.smoo_bindings,
            "\n    ".join(self.pre_lines),
            "\n        ".join(self.loop_lines),
            delay_wpos_var=self.delay_wpos_var,
            delay_line_count=self.delay_line_count,
            delay_step_expr=self.delay_step_expr,
            delay_line_len=self.delay_line_len,
            bpm_var_name="host_bpm" if self.meta.reads_bpm else None,
        )

    def _expand_pattern_macros(self, text: str) -> str:
        """Expand compile-time `seq(i, N, expr)`, `par(i, N, expr)`, `sum(i, N, expr)`, `prod(i, N, expr)`."""
        joiners = {"seq": " : ", "par": ", ", "sum": " + ", "prod": " * "}
        for _ in range(32):
            changed = False
            for macro, sep in joiners.items():
                m = self._find_call(text, macro)
                if not m:
                    continue
                start, end, args = m
                if len(args) != 3:
                    continue
                ivar = args[0].strip()
                count = int(round(float(eval(args[1], {"__builtins__": {}}))))
                if count > MAX_PATTERN_COUNT:
                    raise FaustMemoryLimitError(
                        f"Static memory analysis failed: `{macro}({ivar}, {count}, ...)` requests {count} "
                        f"unrolled stage invocations, exceeding the {MAX_PATTERN_COUNT}-stage compile-time "
                        f"expansion ceiling."
                    )
                pat_body = args[2].strip()
                items = []
                for k in range(max(1, count)):
                    sub_b = re.sub(rf"\b{re.escape(ivar)}\b", str(k), pat_body)
                    items.append(f"({sub_b})")
                expanded = "(" + sep.join(items) + ")"
                text = text[:start] + expanded + text[end:]
                changed = True
                break
            if not changed:
                break
        return text

    def _extract_lambdas(self, text: str, defs: Dict[str, Tuple[List[str], str]]) -> str:
        """Desugar Faust anonymous lambdas `\\(x, ...).(body)` into synthetic top-level functions."""
        lam_idx = 0
        lam_re = re.compile(r"\\\(\s*([A-Za-z_][A-Za-z0-9_,\s]*)\)\s*\.\s*\(")
        while True:
            m = lam_re.search(text)
            if not m:
                break
            args = [a.strip() for a in m.group(1).split(",") if a.strip()]
            p_open = m.end() - 1
            depth = 1
            i = p_open + 1
            while i < len(text) and depth > 0:
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                i += 1
            lbody = text[p_open + 1 : i - 1].strip()
            lname = f"__lambda_{lam_idx}"
            lam_idx += 1
            defs[lname] = (args, lbody)
            text = text[: m.start()] + lname + text[i:]
        return text

    @staticmethod
    def _split_top_statements(text: str) -> List[str]:
        stmts = []
        cur = []
        depth = 0
        for ch in text:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            if ch == ";" and depth == 0:
                stmts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        if "".join(cur).strip():
            stmts.append("".join(cur))
        return stmts

    @staticmethod
    def _is_control_rate(c_expr: str) -> bool:
        """True if `c_expr` depends only on sliders/smoothed parameters and constants (not audio samples)."""
        return not bool(
            re.search(
                r"\b(in0|in1|fTmp_|fWith_|fSig_|fSplit_|fSvf|fEnv|fRms|fComp|fGate|fLag|fTf|fComb|fPipe|fSlew|fOvs|fAp|fDel|fLatch|fRec_|fRecNext_|fRecOut_|fNoi_)\w*\b|dsp->(?!fSmoo_|fDrift_|fPink)\w+",
                c_expr,
            )
        )

    @staticmethod
    def _has_control_var(c_expr: str) -> bool:
        return bool(re.search(r"\b(?:k_|fHslider|fVslider|fEntry|fCheckbox|fButton)\w*\b|dsp->(?:fSmoo_|fDrift_|fPink)\w*", c_expr))

    def _hoist_additive_group(self, s: str) -> str:
        """Group control-rate additive terms in a top-level `+`/`-` sum into one hoisted block constant."""
        s_str = s.strip()
        if not s_str or "?" in s_str or ":" in s_str or "," in s_str:
            return s
        # Split s_str into signed additive terms at top-level (depth == 0)
        terms: List[Tuple[str, str]] = []
        depth = 0
        cur_sign = "+"
        cur_chars: List[str] = []
        i = 0
        n = len(s_str)
        while i < n:
            ch = s_str[i]
            if ch in "([":
                depth += 1
                cur_chars.append(ch)
            elif ch in ")]":
                depth = max(0, depth - 1)
                cur_chars.append(ch)
            elif depth == 0 and ch == "-" and i + 1 < n and s_str[i + 1] == ">":
                cur_chars.append("->")
                i += 2
                continue
            elif depth == 0 and ch in "+-":
                # Check if unary sign at start or after operator/scientific notation
                prev_nonspace = ""
                for k in range(len(cur_chars) - 1, -1, -1):
                    if not cur_chars[k].isspace():
                        prev_nonspace = cur_chars[k]
                        break
                is_sci = prev_nonspace in "eE" and len(cur_chars) >= 2 and (cur_chars[-2].isdigit() or cur_chars[-2] == ".")
                if not prev_nonspace:
                    if ch == "-":
                        cur_sign = "-" if cur_sign == "+" else "+"
                elif is_sci or prev_nonspace in "*/><=&|!":
                    cur_chars.append(ch)
                else:
                    term_body = "".join(cur_chars).strip()
                    if term_body:
                        terms.append((cur_sign, term_body))
                    cur_sign = ch
                    cur_chars = []
            else:
                cur_chars.append(ch)
            i += 1
        last_body = "".join(cur_chars).strip()
        if last_body:
            terms.append((cur_sign, last_body))

        if len(terms) < 2:
            return s

        ctrl_terms: List[Tuple[str, str]] = []
        audio_terms: List[Tuple[str, str]] = []
        for sgn, tbody in terms:
            if self._is_control_rate(tbody):
                ctrl_terms.append((sgn, tbody))
            else:
                audio_terms.append((sgn, tbody))

        if not audio_terms or not ctrl_terms:
            return s

        # Hoist if >=2 control terms combine with a control var, or 1 control term has arithmetic with a control var
        ctrl_combined_raw = " ".join(f"{sgn} {tb}" for sgn, tb in ctrl_terms).lstrip("+ ").strip()
        if not self._has_control_var(ctrl_combined_raw):
            return s
        if len(ctrl_terms) == 1 and not re.search(r"[*/+\-]", ctrl_terms[0][1]):
            return s

        hoisted_k = self.hoist_block_expr("k_add", ctrl_combined_raw)
        rebuilt = [hoisted_k]
        for sgn, tb in audio_terms:
            rebuilt.append(f"{sgn} {tb}")
        return " ".join(rebuilt)

    def _hoist_control_subexprs(self, c_expr: str) -> str:
        """Hoist control-rate parenthesized subexpressions and affine additive terms out of audio-rate expressions."""
        if self._is_control_rate(c_expr):
            return c_expr

        # 1. Fold constant * audio * control_var products: e.g. `0.338 * sh_fabsf(...) * k_base_gain_4`
        m_prod = re.match(
            r"^\s*(\d+(?:\.\d*)?(?:[eE][+\-]?\d+)?)f?\s*\*\s*(sh_fabsf\s*\([^()]+\)|\([^()]+\))\s*\*\s*(k_[A-Za-z0-9_]+)\s*$",
            c_expr,
        )
        if m_prod:
            c_lit, mid_audio, k_var = m_prod.group(1), m_prod.group(2), m_prod.group(3)
            hk = self.hoist_block_expr("k_mul", f"{c_lit} * {k_var}")
            return f"{hk} * {mid_audio}"

        # 2. Bottom-up scan of parenthesized groups
        work = c_expr
        for _ in range(8):
            changed = False
            # Find innermost or clean parenthesized groups
            i = 0
            while i < len(work):
                if work[i] == "(":
                    # Check if preceded by function identifier
                    prev_i = i - 1
                    while prev_i >= 0 and work[prev_i].isspace():
                        prev_i -= 1
                    is_fn_call = prev_i >= 0 and (work[prev_i].isalnum() or work[prev_i] == "_")
                    depth = 1
                    j = i + 1
                    while j < len(work) and depth > 0:
                        if work[j] == "(":
                            depth += 1
                        elif work[j] == ")":
                            depth -= 1
                        j += 1
                    if depth == 0:
                        inner = work[i + 1 : j - 1].strip()
                        if not is_fn_call and inner and "?" not in inner and ":" not in inner and "," not in inner:
                            if (
                                self._is_control_rate(inner)
                                and self._has_control_var(inner)
                                and re.search(r"[+\-*/]", inner)
                                and not re.match(r"^[+\-]?\s*[A-Za-z0-9_.]+\s*$", inner)
                                and not re.search(r"[<>!=]=", inner)
                                and not re.search(r"[<>]", inner)
                            ):
                                hk = self.hoist_block_expr("k_sub", inner)
                                work = work[:i] + hk + work[j:]
                                changed = True
                                break
                            else:
                                new_inner = self._hoist_additive_group(inner)
                                if new_inner != inner:
                                    work = work[: i + 1] + new_inner + work[j - 1 :]
                                    changed = True
                                    break
                i += 1
            if not changed:
                break

        work = self._hoist_additive_group(work)
        return work

    @classmethod
    def _c_ternary_to_py(cls, s: str) -> str:
        """Recursively convert C `cond ? t_expr : f_expr` into `_tern(cond, t_expr, f_expr)`."""
        s = s.strip()
        if "?" not in s:
            return s

        def convert_level(txt: str) -> str:
            txt = txt.strip()
            if "?" not in txt:
                return txt
            out = []
            i = 0
            n = len(txt)
            while i < n:
                if txt[i] == "(":
                    depth = 1
                    j = i + 1
                    while j < n and depth > 0:
                        if txt[j] == "(":
                            depth += 1
                        elif txt[j] == ")":
                            depth -= 1
                        j += 1
                    inner = convert_level(txt[i + 1 : j - 1])
                    out.append(f"({inner})")
                    i = j
                else:
                    out.append(txt[i])
                    i += 1
            cur_txt = "".join(out)
            depth = 0
            q_idx = -1
            c_idx = -1
            q_nesting = 0
            for idx, ch in enumerate(cur_txt):
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif depth == 0:
                    if ch == "?":
                        if q_idx < 0:
                            q_idx = idx
                        else:
                            q_nesting += 1
                    elif ch == ":" and q_idx >= 0:
                        if q_nesting > 0:
                            q_nesting -= 1
                        else:
                            c_idx = idx
                            break
            if q_idx >= 0 and c_idx > q_idx:
                cond_s = cur_txt[:q_idx].strip()
                t_s = convert_level(cur_txt[q_idx + 1 : c_idx])
                f_s = convert_level(cur_txt[c_idx + 1 :])
                return f"_tern({cond_s}, {t_s}, {f_s})"
            return cur_txt

        return convert_level(s)

    def _estimate_expr_bounds(self, c_expr: str) -> Tuple[Optional[float], Optional[float]]:
        """
        Statically estimate the `[min, max]` interval of a C expression across all
        slider ranges `[p.min_val, p.max_val]` and bounded audio primitives
        (`sh_clipf`, `ma.frac`, `os.osc`, `os.phasor`, `an.amp_follower`, noise).
        """
        var_map: Dict[str, str] = {}
        for k, v in self._pre_var_to_expr.items():
            var_map[k] = re.sub(r"\bdsp->(fSmoo_\w+)\b", r"\1", v)
        for svar, texpr in self.smoo_bindings:
            var_map[svar] = re.sub(r"\bdsp->(fSmoo_\w+)\b", r"\1", texpr)
        for line in self.loop_lines:
            m_decl = re.match(
                r"^\s*(?:float|int|uint32_t|FAUSTFLOAT)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+);\s*$",
                line,
            )
            if m_decl:
                var_map[m_decl.group(1)] = re.sub(r"\bdsp->(fSmoo_\w+)\b", r"\1", m_decl.group(2))

        synth_domains: Dict[str, List[float]] = {
            "__sb_unit": [0.0, 0.5, 1.0],
            "__sb_bipolar": [-1.0, 0.0, 1.0],
        }
        # Pre-scan var_map for `X - sh_floorf(X)` (`ma.frac`) or `[0, 1)` phase wrap ternaries
        for k, rhs_v in list(var_map.items()):
            rhs_clean = self._strip_outer_parens(rhs_v)
            m_frac = re.match(r"^(.+?)\s*-\s*sh_floorf\s*\(\s*\1\s*\)$", rhs_clean)
            if m_frac:
                var_map[k] = "__sb_unit"
            elif re.search(r">=\s*1\.0f?\s*\)\s*\?\s*\([^?]+-\s*1\.0f?\s*\)", rhs_clean):
                var_map[k] = "__sb_unit"

        work = c_expr.strip()
        for _ in range(24):
            prev = work
            work = re.sub(r"\bdsp->(fSmoo_\w+)\b", r"\1", work)
            work = re.sub(r"\bdsp->(?:fDrift_\w+|fLfn0_h\w+)\b", "__sb_bipolar", work)
            work = re.sub(r"\b(?:k_white_noise|k_pink_noise)\b", "__sb_bipolar", work)
            work = re.sub(r"\bdsp->(?:fPhasor\w+|fEnv\w+|fRms\w+|fGate_g\w+)\b", "__sb_unit", work)
            work = re.sub(
                r"\bsh_(?:sinf|cosf)\s*\(\s*3\.14159265f?\s*\*\s*dsp->fOsc_ph\w+\s*\)",
                "__sb_bipolar",
                work,
            )
            # Collapse inline `(X) - sh_floorf(X)` to `__sb_unit`
            work = re.sub(
                r"\(\s*([A-Za-z0-9_.\->+\-*/\s]+?)\s*\)\s*-\s*sh_floorf\s*\(\s*\1\s*\)",
                "__sb_unit",
                work,
            )
            for vname, vrhs in var_map.items():
                if re.search(rf"\b{re.escape(vname)}\b", work):
                    work = re.sub(rf"\b{re.escape(vname)}\b", f"({vrhs})", work)
            if work == prev:
                break

        # If any `sh_clipf(expr, lo_lit, hi_lit)` has remaining unresolved state vars inside `expr`,
        # replace it with a synthetic bounded variable `[lo_lit, hi_lit]`.
        clip_idx = 0
        while True:
            m_clip = self._find_call(work, "sh_clipf")
            if not m_clip:
                break
            c_start, c_end, c_args = m_clip
            if len(c_args) == 3 and re.search(r"\bdsp->\w+\b", c_args[0]):
                try:
                    lo_v = float(c_args[1].replace("f", "").strip())
                    hi_v = float(c_args[2].replace("f", "").strip())
                    sb_name = f"__sb_clip_{clip_idx}"
                    clip_idx += 1
                    synth_domains[sb_name] = sorted(set([lo_v, 0.5 * (lo_v + hi_v), hi_v]))
                    work = work[:c_start] + sb_name + work[c_end:]
                    continue
                except Exception:
                    pass
            break

        # Replace any remaining `dsp->fRec*` feedback state reference that couldn't be statically unrolled
        if re.search(r"\bdsp->\w+\b", work):
            return None, None

        py_expr = rewrite_c_divisions_and_math(work)
        py_expr = re.sub(r"\(\s*float\s*\)\s*\(\s*\(\s*int\s*\)", "float(int(", py_expr)
        py_expr = re.sub(r"\(\s*(?:float|int|uint32_t|FAUSTFLOAT)\s*\)", "", py_expr)
        py_expr = re.sub(
            r"(?<![A-Za-z0-9_])((?:\d+\.\d*|\.\d+)(?:[eE][+\-]?\d+)?|\d+[eE][+\-]?\d+)f\b",
            r"\1",
            py_expr,
        )
        py_expr = self._c_ternary_to_py(py_expr)

        def _rcp(x: float) -> float:
            ax = abs(float(x))
            s = -1.0 if float(x) < 0.0 else 1.0
            return s / max(ax, 1.0e-18)

        def _rcp_pos(x: float) -> float:
            return 1.0 / max(float(x), 1.0e-18)

        eval_ns = {
            "__builtins__": {},
            "float": float,
            "int": int,
            "_tern": lambda c, t, f: float(t) if bool(c) else float(f),
            "sh_rcp": _rcp,
            "sh_rcp_pos": _rcp_pos,
            "sh_fdiv": lambda a, b: float(a) * _rcp(b),
            "sh_fabsf": lambda x: abs(float(x)),
            "sh_minf": lambda a, b: min(float(a), float(b)),
            "sh_maxf": lambda a, b: max(float(a), float(b)),
            "sh_clipf": lambda x, lo, hi: max(float(lo), min(float(hi), float(x))),
            "sh_floorf": lambda x: float(math.floor(float(x))),
            "sh_ceilf": lambda x: float(math.ceil(float(x))),
            "sh_roundf": lambda x: float(math.floor(float(x) + 0.5)),
            "sh_signf": lambda x: (1.0 if float(x) > 0.0 else (-1.0 if float(x) < 0.0 else 0.0)),
            "sh_fmodf": lambda x, y: float(math.fmod(float(x), float(y))) if float(y) != 0.0 else 0.0,
            "sh_sqrtf": lambda x: math.sqrt(float(x)) if float(x) > 1.0e-18 else 0.0,
            "sh_sinf": lambda x: math.sin(float(x)),
            "sh_cosf": lambda x: math.cos(float(x)),
            "sh_tanf": lambda x: math.tan(max(-1.42, min(1.42, float(x)))),
            "sh_tanhf": lambda x: math.tanh(max(-3.0, min(3.0, float(x)))),
            "sh_expf": lambda x: math.exp(max(-20.0, min(20.0, float(x)))),
            "sh_logf": lambda x: math.log(float(x)) if float(x) > 1.0e-18 else -41.4,
            "sh_powf": lambda a, b: math.pow(float(a), float(b)) if float(a) > 1.0e-18 else 0.0,
            "sh_log2f": lambda x: math.log2(float(x)) if float(x) > 1.0e-18 else -59.7,
            "sh_log10f": lambda x: math.log10(float(x)) if float(x) > 1.0e-18 else -18.0,
            "sh_atanf": lambda x: math.atan(float(x)),
            "sh_atan2f": lambda y, x: math.atan2(float(y), float(x)),
            "sh_asinf": lambda x: math.asin(max(-0.999999, min(0.999999, float(x)))),
            "sh_acosf": lambda x: math.acos(max(-0.999999, min(0.999999, float(x)))),
            "sh_sinhf": lambda x: math.sinh(max(-20.0, min(20.0, float(x)))),
            "sh_coshf": lambda x: math.cosh(max(-20.0, min(20.0, float(x)))),
        }

        var_domains: List[Tuple[str, List[float]]] = []
        for p in self.params:
            if re.search(rf"\b{re.escape(p.var_name)}\b", py_expr):
                if p.is_stock_vol:
                    pts = [0.0, 1.0, 1.5]
                elif p.values and 0 < (p.max_val - p.min_val) <= 16:
                    span_i = int(round(p.max_val - p.min_val))
                    pts = [float(p.min_val + k) for k in range(span_i + 1)]
                else:
                    pts = sorted(set([float(p.min_val), float(p.init), float(p.max_val)]))
                var_domains.append((p.var_name, pts))

        for sb_name, sb_pts in synth_domains.items():
            if re.search(rf"\b{re.escape(sb_name)}\b", py_expr):
                var_domains.append((sb_name, sb_pts))

        for audio_in in ("in0", "in1"):
            if re.search(rf"\b{audio_in}\b", py_expr):
                var_domains.append((audio_in, [-1.0, 0.0, 1.0]))

        if len(var_domains) > 6:
            # Trim to endpoints (min, max) so cartesian product stays <= 2^N
            var_domains = [(vn, [pts[0], pts[-1]]) for vn, pts in var_domains[:8]]

        results: List[float] = []
        try:
            if not var_domains:
                val = float(eval(py_expr, eval_ns))
                if math.isfinite(val):
                    results.append(val)
            else:
                vnames = [vd[0] for vd in var_domains]
                vlists = [vd[1] for vd in var_domains]
                for combo in itertools.islice(itertools.product(*vlists), 128):
                    local_ns = dict(eval_ns)
                    for vn, vv in zip(vnames, combo):
                        local_ns[vn] = vv
                    val = float(eval(py_expr, local_ns))
                    if math.isfinite(val):
                        results.append(val)
        except Exception:
            return None, None

        if not results:
            return None, None
        return min(results), max(results)

    def _resolve_and_check_delay_bounds(
        self,
        prim_name: str,
        max_l_arg: str,
        d_arg: str,
        env: Dict[str, str],
        ch_suffix: str,
        expand_fn,
    ) -> Tuple[int, str]:
        """
        Statically evaluate and validate both the buffer capacity `max_l` and
        worst-case delay tap distance `d` for any SDRAM delay primitive.
        """
        max_l: Optional[int] = None
        try:
            max_l = int(round(float(eval(max_l_arg, {"__builtins__": {}}))))
        except Exception:
            max_l_c = expand_fn(max_l_arg, env, ch_suffix)
            _, hi_max = self._estimate_expr_bounds(max_l_c)
            if hi_max is not None and math.isfinite(hi_max):
                max_l = int(round(hi_max))

        if max_l is None or max_l <= 0:
            raise FaustMemoryLimitError(
                f"Static memory analysis failed in `{prim_name}` (invoked in `{self.current_fn}`): "
                f"delay buffer size `{max_l_arg}` is non-positive or cannot be statically bounded."
            )
        if max_l * 4 > MAX_SAFE_SDRAM_BYTES:
            report = self.build_memory_report()
            raise FaustMemoryLimitError(
                f"Static memory analysis failed in `{prim_name}` (invoked in `{self.current_fn}`): "
                f"requested delay buffer capacity of {max_l} samples ({max_l * 4} bytes) exceeds the "
                f"529,200-byte (3.0 s @ 44.1 kHz) instance[3] SDRAM safety cap.\n"
                f"{report.format_breakdown()}"
            )

        d_c = expand_fn(d_arg, env, ch_suffix)
        _, d_hi = self._estimate_expr_bounds(d_c)
        if d_hi is not None and math.isfinite(d_hi) and d_hi > max_l:
            report = self.build_memory_report()
            raise FaustMemoryLimitError(
                f"Static memory analysis failed in `{prim_name}` (invoked in `{self.current_fn}`): "
                f"delay tap `{d_arg}` can reach {d_hi:.1f} samples across knob ranges, overflowing "
                f"the allocated {max_l}-sample delay buffer.\n"
                f"{report.format_breakdown()}"
            )
        return max_l, d_c

    def _compile_definitions(self, defs: Dict[str, Tuple[List[str], str]]) -> None:
        const_map = {
            "ma.SR": "44100.0f",
            "ma.PI": "3.14159265f",
            "ma.E": "2.71828183f",
            "ma.EPSILON": "1.0e-18f",
            "ma.FTZ": "0.0f",
        }

        def expand_expr(expr: str, env: Dict[str, str], ch_suffix: str) -> str:
            expr = expr.strip()

            while True:
                replaced = False
                for m_cand in re.finditer(r"\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(", expr):
                    outer_start = m_cand.start()
                    fn_name = m_cand.group(1)
                    call_res = self._find_call(expr[outer_start + 1 :], fn_name)
                    if not call_res:
                        continue
                    _, call_end_rel, call_args = call_res
                    after_call = outer_start + 1 + call_end_rel
                    m_tail = re.match(
                        r"\s*~\s*(?:_|\(\s*_(?:\s*,\s*_)*\s*\))\s*\)\s*(?::\s*!(?:\s*,\s*!)*\s*,\s*_(?:\s*,\s*_)*\s*)?",
                        expr[after_call:],
                    )
                    if m_tail:
                        total_end = after_call + m_tail.end()
                        joined_args = ", ".join(call_args)
                        repl = f"ba.rec({fn_name}, {joined_args})" if joined_args else f"ba.rec({fn_name})"
                        expr = expr[:outer_start] + repl + expr[total_end:]
                        replaced = True
                        break
                if not replaced:
                    break
            expr = re.sub(
                r"\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*~\s*(?:_|\(\s*_(?:\s*,\s*_)*\s*\))\s*\)\s*(?::\s*!(?:\s*,\s*!)*\s*,\s*_(?:\s*,\s*_)*\s*)?",
                r"ba.rec(\1)",
                expr,
            )

            expr = self._eval_pipeline(expr, env, ch_suffix, expand_expr)
            expr = self._rewrite_infix_operators(expr)

            while True:
                m = self._find_call(expr, "ba.rec")
                if not m:
                    break
                start, end, args = m
                fn_name = args[0].strip()
                if fn_name not in defs:
                    raise ValueError(f"Unknown recursive feedback function in `~`: {fn_name}")
                uargs, urhs = defs[fn_name]
                in_actuals = args[1:]
                k_fb = len(uargs) - len(in_actuals)
                if k_fb < 1:
                    raise ValueError(
                        f"Recursive function `{fn_name}` expects at least 1 feedback wire "
                        f"({len(uargs)} formals vs {len(in_actuals)} inputs)."
                    )
                fb_formals = uargs[:k_fb]
                in_formals = uargs[k_fb:]

                self._push_fn(fn_name)
                try:
                    fb_states = [
                        self.alloc_state(
                            f"fRec_fb_{fn_name}{ch_suffix}" if k_fb == 1 else f"fRec_fb_{fn_name}_{idx}{ch_suffix}",
                            1,
                        )
                        for idx in range(k_fb)
                    ]
                    sub_env = {}
                    for fb_formal, fb_state in zip(fb_formals, fb_states):
                        sub_env[fb_formal] = f"dsp->{fb_state}"
                    for formal, actual in zip(in_formals, in_actuals):
                        actual_c = self._strip_outer_parens(expand_expr(actual, env, ch_suffix))
                        if self._is_control_rate(actual_c) or re.match(r"^(?:dsp->)?[A-Za-z0-9_]+$", actual_c):
                            sub_env[formal] = actual_c
                        else:
                            uid = self._state_counter
                            self._state_counter += 1
                            arg_tmp = f"fTmp_fbarg_{fn_name}_{formal}_{uid}"
                            self.loop_lines.append(f"float {arg_tmp} = {actual_c};")
                            sub_env[formal] = arg_tmp

                    if fn_name in self.with_map:
                        for wname, _, _ in self.with_map[fn_name]:
                            sub_env.pop(wname, None)
                        for wname, wargs, wrhs in self.with_map[fn_name]:
                            if wargs:
                                continue
                            w_c = self._hoist_control_subexprs(expand_expr(wrhs, sub_env, ch_suffix))
                            w_clean = self._strip_outer_parens(w_c)
                            if self._is_control_rate(w_c):
                                sub_env[wname] = self.hoist_block_expr(f"k_{wname}", w_c)
                            elif re.match(r"^(?:fTmp_|fWith_|fRecNext_|fRecOut_)[A-Za-z0-9_]*$", w_clean):
                                sub_env[wname] = w_clean
                            else:
                                uid = self._state_counter
                                self._state_counter += 1
                                w_tmp = f"fWith_{fn_name}_{wname}_{uid}"
                                self.loop_lines.append(f"float {w_tmp} = {w_c};")
                                sub_env[wname] = w_tmp

                    parts = self._split_top_comma(urhs)
                    if len(parts) < k_fb:
                        raise ValueError(
                            f"Recursive function `{fn_name}` with {k_fb} feedback wires must return at least {k_fb} outputs."
                        )
                    t_next_vars = []
                    for idx in range(k_fb):
                        uid = self._state_counter
                        self._state_counter += 1
                        t_next = f"fRecNext_{fn_name}_{uid}"
                        next_c = self._hoist_control_subexprs(expand_expr(parts[idx], sub_env, ch_suffix))
                        self.loop_lines.append(f"float {t_next} = {next_c};")
                        t_next_vars.append(t_next)

                    if len(parts) == k_fb:
                        t_out = t_next_vars[0]
                    elif len(parts) == k_fb + 1:
                        uid = self._state_counter
                        self._state_counter += 1
                        t_out = f"fRecOut_{fn_name}_{uid}"
                        out_c = self._hoist_control_subexprs(expand_expr(parts[k_fb], sub_env, ch_suffix))
                        self.loop_lines.append(f"float {t_out} = {out_c};")
                    else:
                        t_out_vars = []
                        for out_idx in range(k_fb, len(parts)):
                            uid = self._state_counter
                            self._state_counter += 1
                            t_ov = f"fRecOut_{fn_name}_{out_idx - k_fb}_{uid}"
                            out_c = self._hoist_control_subexprs(expand_expr(parts[out_idx], sub_env, ch_suffix))
                            self.loop_lines.append(f"float {t_ov} = {out_c};")
                            t_out_vars.append(t_ov)
                        t_out = ", ".join(t_out_vars)

                    for fb_state, t_next in zip(fb_states, t_next_vars):
                        self.loop_lines.append(f"dsp->{fb_state} = {t_next};")
                finally:
                    self._pop_fn()
                expr = expr[:start] + t_out + expr[end:]
                pipe_parts = self._split_top_colon(expr)
                if len(pipe_parts) > 1:
                    acc = pipe_parts[0].strip()
                    for st in pipe_parts[1:]:
                        acc = self._apply_pipe_stage(st.strip(), acc)
                    expr = acc

            for k, v in const_map.items():
                expr = re.sub(rf"\b{re.escape(k)}\b", v, expr)

            for k, v in env.items():
                expr = re.sub(rf"\b{re.escape(k)}\b", f"({v})", expr)

            for _pass in range(16):
                changed_in_pass = False
                for uname, (uargs, urhs) in defs.items():
                    if uname == "process" or not uargs:
                        continue
                    if "~" not in urhs and len(self._split_top_comma(urhs)) > 1:
                        continue
                    while True:
                        m = self._find_call(expr, uname)
                        if not m:
                            break
                        start, end, call_args = m
                        if len(call_args) != len(uargs):
                            break
                        changed_in_pass = True
                        expanded_actuals = [
                            self._strip_outer_parens(expand_expr(actual, env, ch_suffix))
                            for actual in call_args
                        ]
                        self._push_fn(uname)
                        try:
                            sub_env = dict(env)
                            with_defs = self.with_map.get(uname, [])
                            body_sources = [urhs] + [wrhs for _, wargs, wrhs in with_defs if not wargs]
                            for formal, actual_c in zip(uargs, expanded_actuals):
                                formal_refs = sum(
                                    len(re.findall(rf"\b{re.escape(formal)}\b", src))
                                    for src in body_sources
                                )
                                if (
                                    formal_refs >= 2
                                    and not self._is_control_rate(actual_c)
                                    and not re.match(r"^(?:dsp->)?[A-Za-z0-9_]+$", actual_c)
                                ):
                                    uid = self._state_counter
                                    self._state_counter += 1
                                    arg_tmp = f"fTmp_arg_{uname}_{formal}_{uid}"
                                    self.loop_lines.append(f"float {arg_tmp} = {actual_c};")
                                    sub_env[formal] = arg_tmp
                                else:
                                    sub_env[formal] = actual_c
                            if uname in self.with_map:
                                for wname, _, _ in self.with_map[uname]:
                                    sub_env.pop(wname, None)
                                for wname, wargs, wrhs in self.with_map[uname]:
                                    if wargs:
                                        continue
                                    w_c = self._hoist_control_subexprs(expand_expr(wrhs, sub_env, ch_suffix))
                                    if self._is_control_rate(w_c):
                                        sub_env[wname] = self.hoist_block_expr(f"k_{wname}", w_c)
                                    else:
                                        uid = self._state_counter
                                        self._state_counter += 1
                                        w_tmp = f"fWith_{uname}_{wname}_{uid}"
                                        self.loop_lines.append(f"float {w_tmp} = {w_c};")
                                        sub_env[wname] = w_tmp
                            expanded_rhs = self._hoist_control_subexprs(expand_expr(urhs, sub_env, ch_suffix))
                            expr = expr[:start] + f"({expanded_rhs})" + expr[end:]
                        finally:
                            self._pop_fn()

                for uname, (uargs, urhs) in defs.items():
                    if uname == "process" or uargs or len(self._split_top_comma(urhs)) > 1:
                        continue
                    if re.search(rf"\b{re.escape(uname)}\b", expr):
                        changed_in_pass = True
                        if uname in self._sig_cache:
                            expr = re.sub(rf"\b{re.escape(uname)}\b", self._sig_cache[uname], expr)
                            continue
                        self._push_fn(uname)
                        try:
                            sub_env = dict(env)
                            if uname in self.with_map:
                                for wname, _, _ in self.with_map[uname]:
                                    sub_env.pop(wname, None)
                                for wname, wargs, wrhs in self.with_map[uname]:
                                    if wargs:
                                        continue
                                    w_c = expand_expr(wrhs, sub_env, ch_suffix)
                                    if self._is_control_rate(w_c):
                                        sub_env[wname] = self.hoist_block_expr(f"k_{wname}", w_c)
                                    else:
                                        uid = self._state_counter
                                        self._state_counter += 1
                                        w_tmp = f"fWith_{uname}_{wname}_{uid}"
                                        self.loop_lines.append(f"float {w_tmp} = {w_c};")
                                        sub_env[wname] = w_tmp
                            expanded_rhs = expand_expr(urhs, sub_env, ch_suffix)
                            if self._is_control_rate(expanded_rhs):
                                hoisted = self.hoist_block_expr(f"k_{uname}", expanded_rhs)
                                self._sig_cache[uname] = hoisted
                                expr = re.sub(rf"\b{re.escape(uname)}\b", hoisted, expr)
                            elif not re.search(r"\b(in0|in1|_)\b", expanded_rhs):
                                uid = self._state_counter
                                self._state_counter += 1
                                sig_var = f"fSig_{uname}_{uid}"
                                self.loop_lines.append(f"float {sig_var} = {expanded_rhs};")
                                self._sig_cache[uname] = sig_var
                                expr = re.sub(rf"\b{re.escape(uname)}\b", sig_var, expr)
                            else:
                                expr = re.sub(
                                    rf"\b{re.escape(uname)}\b",
                                    f"({expanded_rhs})",
                                    expr,
                                )
                        finally:
                            self._pop_fn()
                if not changed_in_pass:
                    break

            while True:
                m = self._find_call(expr, "si.smoo")
                if not m:
                    break
                start, end, args = m
                target_c = expand_expr(args[0], env, ch_suffix).strip()
                if target_c in self._smoo_cache:
                    svar = self._smoo_cache[target_c]
                else:
                    svar = self.alloc_state("fSmoo", 1)
                    self._smoo_cache[target_c] = svar
                    self.smoo_bindings.append((svar, target_c))
                expr = expr[:start] + f"dsp->{svar}" + expr[end:]

            while True:
                m = self._find_call(expr, "fi.dcblocker")
                if not m:
                    break
                start, end, args = m
                x_c = expand_expr(args[0], env, ch_suffix)
                vec = self.alloc_state(f"fVec_dc{ch_suffix}", 1)
                rec = self.alloc_state(f"fRec_dc{ch_suffix}", 1)
                uid = self._state_counter
                t_in = f"fTmp_dcIn_{uid}"
                t_out = f"fTmp_dcOut_{uid}"
                self.loop_lines.append(f"float {t_in} = {x_c};")
                self.loop_lines.append(
                    f"float {t_out} = {t_in} - dsp->{vec} + 0.995f * dsp->{rec};"
                )
                self.loop_lines.append(f"dsp->{vec} = {t_in};")
                self.loop_lines.append(f"dsp->{rec} = {t_out};")
                expr = expr[:start] + t_out + expr[end:]

            while True:
                m = self._find_call(expr, "fi.lowpass")
                if not m:
                    break
                start, end, args = m
                ord_val = int(round(float(eval(args[0], {"__builtins__": {}}))))
                fc_arg, x_arg = args[1], args[2]
                if ord_val <= 1:
                    fc_c = expand_expr(fc_arg, env, ch_suffix)
                    x_c = expand_expr(x_arg, env, ch_suffix)
                    rec = self.alloc_state(f"fRec_lp1{ch_suffix}", 1)
                    raw_alpha = f"sh_clipf(({fc_c}) * 0.00014247585f, 0.0005f, 0.99f)"
                    alpha_c = (
                        self.hoist_block_expr("k_lp1_a", raw_alpha)
                        if self._is_control_rate(fc_c)
                        else raw_alpha
                    )
                    self.loop_lines.append(
                        f"dsp->{rec} += {alpha_c} * (({x_c}) - dsp->{rec});"
                    )
                    expr = expr[:start] + f"dsp->{rec}" + expr[end:]
                elif ord_val == 2:
                    repl = f"fi.svf_lp({fc_arg}, 0.70710678f, {x_arg})"
                    expr = expr[:start] + expand_expr(repl, env, ch_suffix) + expr[end:]
                elif ord_val == 3:
                    repl = f"fi.lowpass(1, {fc_arg}, fi.svf_lp({fc_arg}, 0.70710678f, {x_arg}))"
                    expr = expr[:start] + expand_expr(repl, env, ch_suffix) + expr[end:]
                else:
                    repl = f"fi.svf_lp({fc_arg}, 0.54119610f, fi.svf_lp({fc_arg}, 1.3065630f, {x_arg}))"
                    expr = expr[:start] + expand_expr(repl, env, ch_suffix) + expr[end:]

            while True:
                m = self._find_call(expr, "fi.highpass")
                if not m:
                    break
                start, end, args = m
                ord_val = int(round(float(eval(args[0], {"__builtins__": {}}))))
                fc_arg, x_arg = args[1], args[2]
                if ord_val <= 1:
                    fc_c = expand_expr(fc_arg, env, ch_suffix)
                    x_c = expand_expr(x_arg, env, ch_suffix)
                    rec = self.alloc_state(f"fRec_hp1{ch_suffix}", 1)
                    uid = self._state_counter
                    self._state_counter += 1
                    t_in = f"fTmp_hpIn_{uid}"
                    raw_alpha = f"sh_clipf(({fc_c}) * 0.00014247585f, 0.0005f, 0.99f)"
                    alpha_c = (
                        self.hoist_block_expr("k_hp1_a", raw_alpha)
                        if self._is_control_rate(fc_c)
                        else raw_alpha
                    )
                    self.loop_lines.append(f"float {t_in} = {x_c};")
                    self.loop_lines.append(
                        f"dsp->{rec} += {alpha_c} * ({t_in} - dsp->{rec});"
                    )
                    expr = expr[:start] + f"({t_in} - dsp->{rec})" + expr[end:]
                elif ord_val == 2:
                    repl = f"fi.svf_hp({fc_arg}, 0.70710678f, {x_arg})"
                    expr = expr[:start] + expand_expr(repl, env, ch_suffix) + expr[end:]
                elif ord_val == 3:
                    repl = f"fi.highpass(1, {fc_arg}, fi.svf_hp({fc_arg}, 0.70710678f, {x_arg}))"
                    expr = expr[:start] + expand_expr(repl, env, ch_suffix) + expr[end:]
                else:
                    repl = f"fi.svf_hp({fc_arg}, 0.54119610f, fi.svf_hp({fc_arg}, 1.3065630f, {x_arg}))"
                    expr = expr[:start] + expand_expr(repl, env, ch_suffix) + expr[end:]

            while True:
                m = self._find_call(expr, "fi.lowpass0_highpass1")
                if not m:
                    break
                start, end, args = m
                if len(args) < 3:
                    break
                x_c = expand_expr(args[2], env, ch_suffix)
                expr = expr[:start] + f"({x_c})" + expr[end:]

            while True:
                m = self._find_call(expr, "fi.allpass_comb")
                if not m:
                    break
                start, end, args = m
                max_l_arg, d_arg, g_arg, x_arg = args[0], args[1], args[2], args[3]
                max_l, d_c = self._resolve_and_check_delay_bounds(
                    "fi.allpass_comb", max_l_arg, d_arg, env, ch_suffix, expand_expr
                )
                g_c = expand_expr(g_arg, env, ch_suffix)
                x_c = expand_expr(x_arg, env, ch_suffix)
                uid = self._state_counter
                self._state_counter += 1
                line_idx = self.alloc_delay_line(f"apcomb_{uid}{ch_suffix}", min_len=max_l)
                wpos_var = self.ensure_delay_wpos()
                g_var = self.hoist_block_expr("k_apc_g", g_c) if self._is_control_rate(g_c) else g_c
                t_del = f"fApcDel_{uid}"
                t_acc = f"fApcAcc_{uid}"
                t_out = f"fApcOut_{uid}"
                self.loop_lines.append(
                    f"float {t_del} = sh_dread_i(dline_{line_idx}, dsp->{wpos_var}, {d_c});"
                )
                self.loop_lines.append(
                    f"float {t_acc} = ({x_c}) - ({g_var}) * {t_del};"
                )
                self.loop_lines.append(f"dline_{line_idx}[dsp->{wpos_var}] = {t_acc};")
                self.loop_lines.append(
                    f"float {t_out} = {t_del} + ({g_var}) * {t_acc};"
                )
                expr = expr[:start] + t_out + expr[end:]

            while True:
                m = self._find_call(expr, "si.smooth")
                if not m:
                    break
                start, end, args = m
                s_c = expand_expr(args[0], env, ch_suffix)
                x_c = expand_expr(args[1], env, ch_suffix)
                rec = self.alloc_state(f"fRec_smoo{ch_suffix}", 1)
                raw_alpha = f"(1.0f - sh_clipf({s_c}, 0.0f, 0.99995f))"
                alpha_c = (
                    self.hoist_block_expr("k_smoo_a", raw_alpha)
                    if self._is_control_rate(s_c)
                    else raw_alpha
                )
                self.loop_lines.append(
                    f"dsp->{rec} += ({alpha_c}) * (({x_c}) - dsp->{rec});"
                )
                expr = expr[:start] + f"dsp->{rec}" + expr[end:]

            while True:
                m = self._find_call(expr, "si.lag_ud")
                if not m:
                    break
                start, end, args = m
                up_c = expand_expr(args[0], env, ch_suffix)
                dn_c = expand_expr(args[1], env, ch_suffix)
                x_c = expand_expr(args[2], env, ch_suffix)
                rec = self.alloc_state(f"fLag{ch_suffix}", 1)
                uid = self._state_counter
                raw_ku = f"sh_clipf(sh_rcp(sh_maxf(1.0f, 44100.0f * ({up_c}))), 0.00001f, 1.0f)"
                raw_kd = f"sh_clipf(sh_rcp(sh_maxf(1.0f, 44100.0f * ({dn_c}))), 0.00001f, 1.0f)"
                ku_c = self.hoist_block_expr("k_lag_u", raw_ku) if self._is_control_rate(up_c) else raw_ku
                kd_c = self.hoist_block_expr("k_lag_d", raw_kd) if self._is_control_rate(dn_c) else raw_kd
                self.loop_lines.append(f"float fLagIn_{uid} = {x_c};")
                self.loop_lines.append(
                    f"float fLagK_{uid} = (fLagIn_{uid} > dsp->{rec}) ? ({ku_c}) : ({kd_c});"
                )
                self.loop_lines.append(
                    f"dsp->{rec} += fLagK_{uid} * (fLagIn_{uid} - dsp->{rec});"
                )
                expr = expr[:start] + f"dsp->{rec}" + expr[end:]

            while True:
                m = self._find_call(expr, "fi.bandpass")
                if not m:
                    break
                start, end, args = m
                f1_c = expand_expr(args[1], env, ch_suffix)
                f2_c = expand_expr(args[2], env, ch_suffix)
                x_arg = args[3]
                fc_bp = f"sh_sqrtf(sh_maxf(20.0f, ({f1_c}) * ({f2_c})))"
                q_bp = f"sh_clipf(({fc_bp}) * sh_rcp(sh_maxf(5.0f, sh_fabsf(({f2_c}) - ({f1_c})))), 0.25f, 20.0f)"
                repl = f"fi.svf_bp({fc_bp}, {q_bp}, {x_arg})"
                expr = expr[:start] + expand_expr(repl, env, ch_suffix) + expr[end:]

            for res_fn, svf_target in (
                ("fi.resonlp", "fi.svf_lp"),
                ("fi.resonhp", "fi.svf_hp"),
                ("fi.resonbp", "fi.svf_bp"),
            ):
                while True:
                    m = self._find_call(expr, res_fn)
                    if not m:
                        break
                    start, end, args = m
                    fc_a, q_a, g_a, x_a = args[0], args[1], args[2], args[3]
                    repl = f"(({g_a}) * {svf_target}({fc_a}, {q_a}, {x_a}))"
                    expr = expr[:start] + expand_expr(repl, env, ch_suffix) + expr[end:]

            while True:
                m = self._find_call(expr, "fi.notchw")
                if not m:
                    break
                start, end, args = m
                bw_c = expand_expr(args[0], env, ch_suffix)
                fc_c = expand_expr(args[1], env, ch_suffix)
                q_n = f"sh_clipf(({fc_c}) * sh_rcp(sh_maxf(1.0f, {bw_c})), 0.25f, 25.0f)"
                repl = f"fi.svf_notch({fc_c}, {q_n}, {args[2]})"
                expr = expr[:start] + expand_expr(repl, env, ch_suffix) + expr[end:]

            while True:
                m = self._find_call(expr, "fi.low_shelf")
                if not m:
                    break
                start, end, args = m
                db_c = expand_expr(args[0], env, ch_suffix)
                fc_c = expand_expr(args[1], env, ch_suffix)
                x_c = expand_expr(args[2], env, ch_suffix)
                uid = self._state_counter
                self._state_counter += 1
                t_in = f"fTmp_lsIn_{uid}"
                g_expr = f"(sh_powf(10.0f, 0.05f * ({db_c})) - 1.0f)"
                g_var = self.hoist_block_expr("k_ls_g", g_expr) if self._is_control_rate(db_c) else g_expr
                self.loop_lines.append(f"float {t_in} = {x_c};")
                lp_c = expand_expr(f"fi.lowpass(1, {fc_c}, {t_in})", env, ch_suffix)
                expr = expr[:start] + f"({t_in} + ({g_var}) * ({lp_c}))" + expr[end:]

            while True:
                m = self._find_call(expr, "fi.high_shelf")
                if not m:
                    break
                start, end, args = m
                db_c = expand_expr(args[0], env, ch_suffix)
                fc_c = expand_expr(args[1], env, ch_suffix)
                x_c = expand_expr(args[2], env, ch_suffix)
                uid = self._state_counter
                self._state_counter += 1
                t_in = f"fTmp_hsIn_{uid}"
                g_expr = f"(sh_powf(10.0f, 0.05f * ({db_c})) - 1.0f)"
                g_var = self.hoist_block_expr("k_hs_g", g_expr) if self._is_control_rate(db_c) else g_expr
                self.loop_lines.append(f"float {t_in} = {x_c};")
                hp_c = expand_expr(f"fi.highpass(1, {fc_c}, {t_in})", env, ch_suffix)
                expr = expr[:start] + f"({t_in} + ({g_var}) * ({hp_c}))" + expr[end:]

            for pk_fn, is_bw in (("fi.peak_eq", True), ("fi.peak_eq_cq", False)):
                while True:
                    m = self._find_call(expr, pk_fn)
                    if not m:
                        break
                    start, end, args = m
                    db_c = expand_expr(args[0], env, ch_suffix)
                    fc_c = expand_expr(args[1], env, ch_suffix)
                    p3_c = expand_expr(args[2], env, ch_suffix)
                    q_expr = f"sh_clipf(({fc_c}) * sh_rcp(sh_maxf(1.0f, {p3_c})), 0.2f, 20.0f)" if is_bw else p3_c
                    g_expr = f"(sh_powf(10.0f, 0.05f * ({db_c})) - 1.0f) * sh_rcp(sh_maxf(0.2f, {q_expr}))"
                    repl = f"fi.svf_peak({fc_c}, {q_expr}, {g_expr}, {args[3]})"
                    expr = expr[:start] + expand_expr(repl, env, ch_suffix) + expr[end:]

            while True:
                m = self._find_call(expr, "fi.tf1")
                if not m:
                    break
                start, end, args = m
                b0_c = expand_expr(args[0], env, ch_suffix)
                b1_c = expand_expr(args[1], env, ch_suffix)
                a1_c = expand_expr(args[2], env, ch_suffix)
                x_c = expand_expr(args[3], env, ch_suffix)
                st_z1 = self.alloc_state(f"fTf1_z1{ch_suffix}", 1)
                uid = self._state_counter
                t_in = f"fTf1In_{uid}"
                t_out = f"fTf1Out_{uid}"
                self.loop_lines.append(f"float {t_in} = {x_c};")
                self.loop_lines.append(f"float {t_out} = ({b0_c}) * {t_in} + dsp->{st_z1};")
                self.loop_lines.append(
                    f"dsp->{st_z1} = sh_clipf(({b1_c}) * {t_in} - ({a1_c}) * {t_out}, -16.0f, 16.0f);"
                )
                expr = expr[:start] + t_out + expr[end:]

            while True:
                m = self._find_call(expr, "fi.tf2")
                if not m:
                    break
                start, end, args = m
                b0_c = expand_expr(args[0], env, ch_suffix)
                b1_c = expand_expr(args[1], env, ch_suffix)
                b2_c = expand_expr(args[2], env, ch_suffix)
                a1_c = expand_expr(args[3], env, ch_suffix)
                a2_c = expand_expr(args[4], env, ch_suffix)
                x_c = expand_expr(args[5], env, ch_suffix)
                st_z1 = self.alloc_state(f"fTf2_z1{ch_suffix}", 1)
                st_z2 = self.alloc_state(f"fTf2_z2{ch_suffix}", 1)
                uid = self._state_counter
                t_in = f"fTf2In_{uid}"
                t_out = f"fTf2Out_{uid}"
                self.loop_lines.append(f"float {t_in} = {x_c};")
                self.loop_lines.append(f"float {t_out} = ({b0_c}) * {t_in} + dsp->{st_z1};")
                self.loop_lines.append(
                    f"dsp->{st_z1} = sh_clipf(({b1_c}) * {t_in} - ({a1_c}) * {t_out} + dsp->{st_z2}, -16.0f, 16.0f);"
                )
                self.loop_lines.append(
                    f"dsp->{st_z2} = sh_clipf(({b2_c}) * {t_in} - ({a2_c}) * {t_out}, -16.0f, 16.0f);"
                )
                expr = expr[:start] + t_out + expr[end:]

            while True:
                m = self._find_call(expr, "fi.fb_fcomb")
                if not m:
                    break
                start, end, args = m
                max_l, del_c = self._resolve_and_check_delay_bounds(
                    "fi.fb_fcomb", args[0], args[1], env, ch_suffix, expand_expr
                )
                b0_c = expand_expr(args[2], env, ch_suffix)
                an_c = expand_expr(args[3], env, ch_suffix)
                x_c = expand_expr(args[4], env, ch_suffix)
                uid = self._state_counter
                self._state_counter += 1
                line_idx = self.alloc_delay_line(f"fcomb_{uid}{ch_suffix}", min_len=max_l)
                wpos_var = self.ensure_delay_wpos()
                t_del = f"fCombDel_{uid}"
                t_out = f"fCombOut_{uid}"
                self.loop_lines.append(
                    f"float {t_del} = sh_dread_1(dline_{line_idx}, dsp->{wpos_var}, {del_c});"
                )
                self.loop_lines.append(
                    f"float {t_out} = ({b0_c}) * ({x_c}) - ({an_c}) * {t_del};"
                )
                self.loop_lines.append(f"dline_{line_idx}[dsp->{wpos_var}] = {t_out};")
                expr = expr[:start] + t_out + expr[end:]

            for svf_kind in ("fi.svf_lp", "fi.svf_bp", "fi.svf_hp", "fi.svf_notch", "fi.svf_peak"):
                while True:
                    m = self._find_call(expr, svf_kind)
                    if not m:
                        break
                    start, end, args = m
                    fc_c = expand_expr(args[0], env, ch_suffix)
                    q_c = expand_expr(args[1], env, ch_suffix)
                    if svf_kind == "fi.svf_peak":
                        g_c = expand_expr(args[2], env, ch_suffix)
                        x_c = expand_expr(args[3], env, ch_suffix)
                    else:
                        g_c = "1.0f"
                        x_c = expand_expr(args[2], env, ch_suffix)
                    rec_lp = self.alloc_state(f"fSvf_lp{ch_suffix}", 1)
                    rec_bp = self.alloc_state(f"fSvf_bp{ch_suffix}", 1)
                    uid = self._state_counter

                    raw_f = f"sh_clipf(2.0f * sh_sinf(3.14159265f * ({fc_c}) * 2.2675737e-05f), 0.001f, 0.82f)"
                    raw_d = f"sh_clipf(sh_rcp({q_c}), 0.05f, 2.0f)"
                    f_var = (
                        self.hoist_block_expr("k_svf_f", raw_f)
                        if self._is_control_rate(fc_c)
                        else raw_f
                    )
                    d_var = (
                        self.hoist_block_expr("k_svf_d", raw_d)
                        if self._is_control_rate(q_c)
                        else raw_d
                    )

                    self.loop_lines.append(f"float fSvfIn_{uid} = {x_c};")
                    self.loop_lines.append(
                        f"float fSvfHp_{uid} = fSvfIn_{uid} - dsp->{rec_lp} - ({d_var}) * dsp->{rec_bp};"
                    )
                    self.loop_lines.append(
                        f"dsp->{rec_bp} += ({f_var}) * fSvfHp_{uid};"
                    )
                    self.loop_lines.append(
                        f"dsp->{rec_lp} += ({f_var}) * dsp->{rec_bp};"
                    )
                    if svf_kind == "fi.svf_lp":
                        out_expr = f"dsp->{rec_lp}"
                    elif svf_kind == "fi.svf_bp":
                        out_expr = f"dsp->{rec_bp}"
                    elif svf_kind == "fi.svf_hp":
                        out_expr = f"fSvfHp_{uid}"
                    elif svf_kind == "fi.svf_notch":
                        out_expr = f"(fSvfIn_{uid} - ({d_var}) * dsp->{rec_bp})"
                    else:
                        out_expr = f"(fSvfIn_{uid} + ({g_c}) * dsp->{rec_bp})"
                    expr = expr[:start] + out_expr + expr[end:]

            for osc_fn, is_cos in (("os.osc", False), ("os.oscsin", False), ("os.osccos", True)):
                while True:
                    m = self._find_call(expr, osc_fn)
                    if not m:
                        break
                    start, end, args = m
                    freq_c = expand_expr(args[0], env, ch_suffix)
                    rec = self.alloc_state(f"fOsc_ph{ch_suffix}", 1)
                    raw_inc = f"({freq_c}) * 4.5351474e-05f"
                    inc_c = (
                        self.hoist_block_expr("k_osc_inc", raw_inc)
                        if self._is_control_rate(freq_c)
                        else raw_inc
                    )
                    self.loop_lines.append(f"dsp->{rec} += {inc_c};")
                    self.loop_lines.append(f"if (dsp->{rec} >= 1.0f) dsp->{rec} -= 2.0f;")
                    self.loop_lines.append(f"if (dsp->{rec} < -1.0f) dsp->{rec} += 2.0f;")
                    trig_fn = "sh_cosf" if is_cos else "sh_sinf"
                    expr = expr[:start] + f"{trig_fn}(3.14159265f * dsp->{rec})" + expr[end:]

            for ph_fn in ("os.phasor", "os.lf_sawpos", "os.sawtooth", "os.lf_saw", "os.triangle", "os.lf_triangle", "os.square", "os.lf_squarewave", "os.pulsetrain", "os.lf_pulsetrain"):
                while True:
                    m = self._find_call(expr, ph_fn)
                    if not m:
                        break
                    start, end, args = m
                    if ph_fn == "os.phasor":
                        ts_c = expand_expr(args[0], env, ch_suffix)
                        freq_c = expand_expr(args[1], env, ch_suffix)
                        duty_c = "0.5f"
                    elif ph_fn in ("os.pulsetrain", "os.lf_pulsetrain"):
                        ts_c = "1.0f"
                        freq_c = expand_expr(args[0], env, ch_suffix)
                        duty_c = expand_expr(args[1], env, ch_suffix)
                    else:
                        ts_c = "1.0f"
                        freq_c = expand_expr(args[0], env, ch_suffix)
                        duty_c = "0.5f"
                    rec = self.alloc_state(f"fPhasor{ch_suffix}", 1)
                    raw_inc = f"({freq_c}) * 2.2675737e-05f"
                    inc_c = (
                        self.hoist_block_expr("k_ph_inc", raw_inc)
                        if self._is_control_rate(freq_c)
                        else raw_inc
                    )
                    self.loop_lines.append(f"dsp->{rec} += {inc_c};")
                    self.loop_lines.append(f"dsp->{rec} -= sh_floorf(dsp->{rec});")
                    if ph_fn == "os.phasor":
                        out_e = f"(dsp->{rec} * ({ts_c}))"
                    elif ph_fn == "os.lf_sawpos":
                        out_e = f"dsp->{rec}"
                    elif ph_fn in ("os.sawtooth", "os.lf_saw"):
                        out_e = f"(2.0f * dsp->{rec} - 1.0f)"
                    elif ph_fn in ("os.triangle", "os.lf_triangle"):
                        out_e = f"(2.0f * sh_fabsf(2.0f * dsp->{rec} - 1.0f) - 1.0f)"
                    else:
                        out_e = f"((dsp->{rec} < ({duty_c})) ? 1.0f : -1.0f)"
                    expr = expr[:start] + out_e + expr[end:]

            for amp_fn in ("an.amp_follower_ud", "an.amp_follower_ar", "an.amp_follower"):
                while True:
                    m = self._find_call(expr, amp_fn)
                    if not m:
                        break
                    start, end, args = m
                    if amp_fn == "an.amp_follower":
                        att_c = "1.0f"
                        r_arg = expand_expr(args[0], env, ch_suffix)
                        rel_c = f"sh_clipf(sh_rcp(sh_maxf(1.0f, 44100.0f * ({r_arg}))), 0.00001f, 1.0f)"
                        x_c = expand_expr(args[1], env, ch_suffix)
                    elif amp_fn == "an.amp_follower_ar":
                        a_arg = expand_expr(args[0], env, ch_suffix)
                        r_arg = expand_expr(args[1], env, ch_suffix)
                        att_c = f"sh_clipf(sh_rcp(sh_maxf(1.0f, 44100.0f * ({a_arg}))), 0.00001f, 1.0f)"
                        rel_c = f"sh_clipf(sh_rcp(sh_maxf(1.0f, 44100.0f * ({r_arg}))), 0.00001f, 1.0f)"
                        x_c = expand_expr(args[2], env, ch_suffix)
                    else:
                        att_c = expand_expr(args[0], env, ch_suffix)
                        rel_c = expand_expr(args[1], env, ch_suffix)
                        x_c = expand_expr(args[2], env, ch_suffix)
                    if self._is_control_rate(att_c) and att_c != "1.0f":
                        att_c = self.hoist_block_expr("k_env_a", att_c)
                    if self._is_control_rate(rel_c):
                        rel_c = self.hoist_block_expr("k_env_r", rel_c)
                    rec = self.alloc_state(f"fEnv{ch_suffix}", 1)
                    uid = self._state_counter
                    self.loop_lines.append(f"float fEnvIn_{uid} = sh_fabsf({x_c});")
                    self.loop_lines.append(
                        f"float fEnvK_{uid} = (fEnvIn_{uid} > dsp->{rec}) ? ({att_c}) : ({rel_c});"
                    )
                    self.loop_lines.append(
                        f"dsp->{rec} += fEnvK_{uid} * (fEnvIn_{uid} - dsp->{rec});"
                    )
                    expr = expr[:start] + f"dsp->{rec}" + expr[end:]

            while True:
                m = self._find_call(expr, "an.rms_envelope_tau")
                if not m:
                    break
                start, end, args = m
                tau_c = expand_expr(args[0], env, ch_suffix)
                x_c = expand_expr(args[1], env, ch_suffix)
                rec = self.alloc_state(f"fRms{ch_suffix}", 1)
                uid = self._state_counter
                raw_k = f"sh_clipf(sh_rcp(sh_maxf(1.0f, 44100.0f * ({tau_c}))), 0.00001f, 1.0f)"
                k_c = self.hoist_block_expr("k_rms_a", raw_k) if self._is_control_rate(tau_c) else raw_k
                self.loop_lines.append(f"float fRmsIn_{uid} = {x_c};")
                self.loop_lines.append(
                    f"dsp->{rec} += ({k_c}) * (fRmsIn_{uid} * fRmsIn_{uid} - dsp->{rec});"
                )
                expr = expr[:start] + f"sh_sqrtf(dsp->{rec})" + expr[end:]

            while True:
                m = self._find_call(expr, "co.compressor_mono")
                if not m:
                    break
                start, end, args = m
                rat_c = expand_expr(args[0], env, ch_suffix)
                thr_c = expand_expr(args[1], env, ch_suffix)
                att_c = expand_expr(args[2], env, ch_suffix)
                rel_c = expand_expr(args[3], env, ch_suffix)
                x_c = expand_expr(args[4], env, ch_suffix)
                uid = self._state_counter
                self._state_counter += 1
                t_in = f"fCompIn_{uid}"
                self.loop_lines.append(f"float {t_in} = {x_c};")
                env_c = expand_expr(f"an.amp_follower_ar({att_c}, {rel_c}, {t_in})", env, ch_suffix)
                slope_e = f"(1.0f - sh_rcp(sh_maxf(1.0f, {rat_c})))"
                if self._is_control_rate(rat_c):
                    slope_e = self.hoist_block_expr("k_comp_sl", slope_e)
                thr_lin = f"sh_powf(10.0f, 0.05f * ({thr_c}))"
                if self._is_control_rate(thr_c):
                    thr_lin = self.hoist_block_expr("k_comp_th", thr_lin)
                t_g = f"fCompG_{uid}"
                self.loop_lines.append(
                    f"float {t_g} = ({env_c} > ({thr_lin})) ? sh_powf(({env_c}) * sh_rcp_pos({thr_lin}), -({slope_e})) : 1.0f;"
                )
                expr = expr[:start] + f"({t_in} * {t_g})" + expr[end:]

            while True:
                m = self._find_call(expr, "co.limiter_1176_R4_mono")
                if not m:
                    break
                start, end, args = m
                repl = f"co.compressor_mono(4.0f, -6.0f, 0.0008f, 0.050f, {args[0]})"
                expr = expr[:start] + expand_expr(repl, env, ch_suffix) + expr[end:]

            while True:
                m = self._find_call(expr, "ef.gate_mono")
                if not m:
                    break
                start, end, args = m
                thr_c = expand_expr(args[0], env, ch_suffix)
                att_c = expand_expr(args[1], env, ch_suffix)
                hold_c = expand_expr(args[2], env, ch_suffix)
                rel_c = expand_expr(args[3], env, ch_suffix)
                x_c = expand_expr(args[4], env, ch_suffix)
                st_env = self.alloc_state(f"fGate_env{ch_suffix}", 1)
                st_cnt = self.alloc_state(f"fGate_cnt{ch_suffix}", 1)
                st_g = self.alloc_state(f"fGate_g{ch_suffix}", 1)
                uid = self._state_counter
                thr_lin = f"sh_powf(10.0f, 0.05f * ({thr_c}))"
                if self._is_control_rate(thr_c):
                    thr_lin = self.hoist_block_expr("k_gt_th", thr_lin)
                ka_e = f"sh_clipf(sh_rcp(sh_maxf(1.0f, 44100.0f * ({att_c}))), 0.0001f, 1.0f)"
                kr_e = f"sh_clipf(sh_rcp(sh_maxf(1.0f, 44100.0f * ({rel_c}))), 0.00001f, 1.0f)"
                h_smps = f"sh_maxf(1.0f, 44100.0f * ({hold_c}))"
                if self._is_control_rate(att_c):
                    ka_e = self.hoist_block_expr("k_gt_a", ka_e)
                if self._is_control_rate(rel_c):
                    kr_e = self.hoist_block_expr("k_gt_r", kr_e)
                if self._is_control_rate(hold_c):
                    h_smps = self.hoist_block_expr("k_gt_h", h_smps)
                t_in = f"fGateIn_{uid}"
                self.loop_lines.append(f"float {t_in} = {x_c};")
                self.loop_lines.append(
                    f"float fGateAbs_{uid} = sh_fabsf({t_in});"
                )
                self.loop_lines.append(
                    f"dsp->{st_env} += ((fGateAbs_{uid} > dsp->{st_env}) ? 0.05f : 0.001f) * (fGateAbs_{uid} - dsp->{st_env});"
                )
                self.loop_lines.append(
                    f"if (dsp->{st_env} >= ({thr_lin})) dsp->{st_cnt} = ({h_smps}); else if (dsp->{st_cnt} > 0.0f) dsp->{st_cnt} -= 1.0f;"
                )
                self.loop_lines.append(
                    f"float fGateTgt_{uid} = (dsp->{st_cnt} > 0.0f) ? 1.0f : 0.0f;"
                )
                self.loop_lines.append(
                    f"dsp->{st_g} += ((fGateTgt_{uid} > dsp->{st_g}) ? ({ka_e}) : ({kr_e})) * (fGateTgt_{uid} - dsp->{st_g});"
                )
                expr = expr[:start] + f"({t_in} * dsp->{st_g})" + expr[end:]

            while True:
                m = self._find_call(expr, "ef.slew")
                if not m:
                    break
                start, end, args = m
                rate_c = expand_expr(args[0], env, ch_suffix)
                x_c = expand_expr(args[1], env, ch_suffix)
                rec = self.alloc_state(f"fSlew{ch_suffix}", 1)
                uid = self._state_counter
                r_var = (
                    self.hoist_block_expr("k_slew", rate_c)
                    if self._is_control_rate(rate_c)
                    else rate_c
                )
                self.loop_lines.append(f"float fSlewD_{uid} = ({x_c}) - dsp->{rec};")
                self.loop_lines.append(
                    f"dsp->{rec} += sh_clipf(fSlewD_{uid}, -({r_var}), ({r_var}));"
                )
                expr = expr[:start] + f"dsp->{rec}" + expr[end:]

            if re.search(r"\bzm_fsw_active\b", expr):
                expr = re.sub(r"\bzm_fsw_active\b", "((fade >= 0.05f) ? 1.0f : 0.0f)", expr)

            if re.search(r"\bzm_is_unlatch\b", expr):
                expr = re.sub(r"\bzm_is_unlatch\b", "((fsw_unlatch >= 0.5f) ? 1.0f : 0.0f)", expr)

            if re.search(r"\bzm_bpm\b", expr):
                self.meta.reads_bpm = True
                expr = re.sub(r"\bzm_bpm\b", "host_bpm", expr)

            if re.search(r"\bno\.noise\b", expr):
                if "no.noise" in self._noise_cache:
                    n_var = self._noise_cache["no.noise"]
                else:
                    rng_state = self.alloc_state("iRng", 1, ctype="uint32_t")
                    n_var = "k_white_noise"
                    self._noise_cache["no.noise"] = n_var
                    self.pre_lines.append(f"if (dsp->{rng_state} == 0u) dsp->{rng_state} = 0x52415431u;")
                    self.pre_lines.append(f"dsp->{rng_state} ^= dsp->{rng_state} << 13;")
                    self.pre_lines.append(f"dsp->{rng_state} ^= dsp->{rng_state} >> 17;")
                    self.pre_lines.append(f"dsp->{rng_state} ^= dsp->{rng_state} << 5;")
                    self.pre_lines.append(
                        f"float {n_var} = (float)((int32_t)dsp->{rng_state}) * 4.6566129e-10f;"
                    )
                expr = re.sub(r"\bno\.noise\b", n_var, expr)

            while True:
                m = self._find_call(expr, "no.lfnoise")
                if not m:
                    break
                start, end, args = m
                alpha_c = expand_expr(args[0], env, ch_suffix)
                cache_key = f"lfnoise_{alpha_c}"
                if cache_key in self._noise_cache:
                    d_var = self._noise_cache[cache_key]
                else:
                    if "no.noise" not in self._noise_cache:
                        rng_state = self.alloc_state("iRng", 1, ctype="uint32_t")
                        self._noise_cache["no.noise"] = "k_white_noise"
                        self.pre_lines.append(f"if (dsp->{rng_state} == 0u) dsp->{rng_state} = 0x52415431u;")
                        self.pre_lines.append(f"dsp->{rng_state} ^= dsp->{rng_state} << 13;")
                        self.pre_lines.append(f"dsp->{rng_state} ^= dsp->{rng_state} >> 17;")
                        self.pre_lines.append(f"dsp->{rng_state} ^= dsp->{rng_state} << 5;")
                        self.pre_lines.append(
                            f"float k_white_noise = (float)((int32_t)dsp->{rng_state}) * 4.6566129e-10f;"
                        )
                    drift_state = self.alloc_state("fDrift", 1)
                    self.pre_lines.append(
                        f"dsp->{drift_state} += ({alpha_c}) * (k_white_noise - dsp->{drift_state});"
                    )
                    d_var = f"dsp->{drift_state}"
                    self._noise_cache[cache_key] = d_var
                expr = expr[:start] + d_var + expr[end:]

            if re.search(r"\bno\.pink_noise\b", expr):
                if "no.noise" not in self._noise_cache:
                    rng_state = self.alloc_state("iRng", 1, ctype="uint32_t")
                    self._noise_cache["no.noise"] = "k_white_noise"
                    self.pre_lines.append(f"if (dsp->{rng_state} == 0u) dsp->{rng_state} = 0x52415431u;")
                    self.pre_lines.append(f"dsp->{rng_state} ^= dsp->{rng_state} << 13;")
                    self.pre_lines.append(f"dsp->{rng_state} ^= dsp->{rng_state} >> 17;")
                    self.pre_lines.append(f"dsp->{rng_state} ^= dsp->{rng_state} << 5;")
                    self.pre_lines.append(
                        f"float k_white_noise = (float)((int32_t)dsp->{rng_state}) * 4.6566129e-10f;"
                    )
                if "no.pink_noise" in self._noise_cache:
                    p_var = self._noise_cache["no.pink_noise"]
                else:
                    pk0 = self.alloc_state("fPink0", 1)
                    pk1 = self.alloc_state("fPink1", 1)
                    pk2 = self.alloc_state("fPink2", 1)
                    p_var = "k_pink_noise"
                    self._noise_cache["no.pink_noise"] = p_var
                    self.pre_lines.append(f"dsp->{pk0} = 0.99765f * dsp->{pk0} + k_white_noise * 0.0990460f;")
                    self.pre_lines.append(f"dsp->{pk1} = 0.96300f * dsp->{pk1} + k_white_noise * 0.2965164f;")
                    self.pre_lines.append(f"dsp->{pk2} = 0.57000f * dsp->{pk2} + k_white_noise * 1.0526913f;")
                    self.pre_lines.append(
                        f"float {p_var} = 0.25f * (dsp->{pk0} + dsp->{pk1} + dsp->{pk2} + k_white_noise * 0.1848f);"
                    )
                expr = re.sub(r"\bno\.pink_noise\b", p_var, expr)

            while True:
                m = self._find_call(expr, "no.lfnoise0")
                if not m:
                    break
                start, end, args = m
                freq_c = expand_expr(args[0], env, ch_suffix)
                if "no.noise" not in self._noise_cache:
                    rng_state = self.alloc_state("iRng", 1, ctype="uint32_t")
                    self._noise_cache["no.noise"] = "k_white_noise"
                    self.pre_lines.append(f"if (dsp->{rng_state} == 0u) dsp->{rng_state} = 0x52415431u;")
                    self.pre_lines.append(f"dsp->{rng_state} ^= dsp->{rng_state} << 13;")
                    self.pre_lines.append(f"dsp->{rng_state} ^= dsp->{rng_state} >> 17;")
                    self.pre_lines.append(f"dsp->{rng_state} ^= dsp->{rng_state} << 5;")
                    self.pre_lines.append(
                        f"float k_white_noise = (float)((int32_t)dsp->{rng_state}) * 4.6566129e-10f;"
                    )
                ph_st = self.alloc_state(f"fLfn0_ph{ch_suffix}", 1)
                hold_st = self.alloc_state(f"fLfn0_h{ch_suffix}", 1)
                raw_inc = f"({freq_c}) * 2.2675737e-05f"
                inc_c = self.hoist_block_expr("k_lfn0_inc", raw_inc) if self._is_control_rate(freq_c) else raw_inc
                self.loop_lines.append(f"dsp->{ph_st} += {inc_c};")
                self.loop_lines.append(f"if (dsp->{ph_st} >= 1.0f) {{ dsp->{ph_st} -= 1.0f; dsp->{hold_st} = k_white_noise; }}")
                expr = expr[:start] + f"dsp->{hold_st}" + expr[end:]

            for latch_fn in ("ba.latch", "ba.sAndH"):
                while True:
                    m = self._find_call(expr, latch_fn)
                    if not m:
                        break
                    start, end, args = m
                    trig_c = expand_expr(args[0], env, ch_suffix)
                    x_c = expand_expr(args[1], env, ch_suffix)
                    rec = self.alloc_state(f"fLatch{ch_suffix}", 1)
                    self.loop_lines.append(
                        f"dsp->{rec} = (({trig_c}) > 0.5f) ? ({x_c}) : dsp->{rec};"
                    )
                    expr = expr[:start] + f"dsp->{rec}" + expr[end:]

            while True:
                m = self._find_call(expr, "ba.downSample")
                if not m:
                    break
                start, end, args = m
                freq_c = expand_expr(args[0], env, ch_suffix)
                x_c = expand_expr(args[1], env, ch_suffix)
                ph_st = self.alloc_state(f"fDs_ph{ch_suffix}", 1)
                hold_st = self.alloc_state(f"fDs_h{ch_suffix}", 1)
                raw_step = f"sh_clipf(({freq_c}) * 2.2675737e-05f, 0.001f, 1.0f)"
                step_c = self.hoist_block_expr("k_ds_stp", raw_step) if self._is_control_rate(freq_c) else raw_step
                self.loop_lines.append(f"dsp->{ph_st} += {step_c};")
                self.loop_lines.append(
                    f"if (dsp->{ph_st} >= 1.0f) {{ dsp->{ph_st} -= 1.0f; dsp->{hold_st} = ({x_c}); }}"
                )
                expr = expr[:start] + f"dsp->{hold_st}" + expr[end:]

            for pulse_fn, has_width in (("ba.pulse", False), ("ba.pulsen", True)):
                while True:
                    m = self._find_call(expr, pulse_fn)
                    if not m:
                        break
                    start, end, args = m
                    w_c = expand_expr(args[0], env, ch_suffix) if has_width else "1.0f"
                    p_c = expand_expr(args[1] if has_width else args[0], env, ch_suffix)
                    cnt_st = self.alloc_state(f"fPulse{ch_suffix}", 1)
                    self.loop_lines.append(f"dsp->{cnt_st} += 1.0f;")
                    self.loop_lines.append(f"if (dsp->{cnt_st} >= ({p_c})) dsp->{cnt_st} = 0.0f;")
                    expr = expr[:start] + f"((dsp->{cnt_st} < ({w_c})) ? 1.0f : 0.0f)" + expr[end:]

            while True:
                m = self._find_call(expr, "ba.selectn")
                if not m:
                    break
                start, end, args = m
                idx_c = expand_expr(args[1], env, ch_suffix)
                choices = [expand_expr(a, env, ch_suffix) for a in args[2:]]
                if not choices:
                    expr = expr[:start] + "0.0f" + expr[end:]
                    continue
                acc_sel = choices[-1]
                for c_idx in range(len(choices) - 2, -1, -1):
                    acc_sel = f"(({idx_c} < {c_idx + 0.5:.1f}f) ? ({choices[c_idx]}) : ({acc_sel}))"
                if self._is_control_rate(idx_c) and all(self._is_control_rate(c) for c in choices):
                    acc_sel = self.hoist_block_expr("k_seln", acc_sel)
                expr = expr[:start] + acc_sel + expr[end:]

            for del_fn, reader_c in (
                ("de.fdelay3", "sh_dread_3"),
                ("de.fdelay2", "sh_dread_2"),
                ("de.fdelay1", "sh_dread_1"),
                ("de.fdelay", "sh_dread_1"),
                ("de.delay", "sh_dread_i"),
            ):
                while True:
                    m = self._find_call(expr, del_fn)
                    if not m:
                        break
                    start, end, args = m
                    max_l, d_c = self._resolve_and_check_delay_bounds(
                        del_fn, args[0], args[1], env, ch_suffix, expand_expr
                    )
                    x_c = expand_expr(args[2], env, ch_suffix)
                    uid = self._state_counter
                    self._state_counter += 1
                    wpos_var = self.ensure_delay_wpos()
                    sig_key = f"{ch_suffix}:{self._strip_outer_parens(x_c)}"
                    if sig_key in self.delay_written_signals:
                        line_idx = self.delay_written_signals[sig_key]
                        self._update_shared_delay_line_len(line_idx, max_l)
                    else:
                        line_idx = self.alloc_delay_line(f"sig_{sig_key}", min_len=max_l)
                        self.delay_written_signals[sig_key] = line_idx
                        t_w = f"fDelIn_{uid}"
                        self.loop_lines.append(f"float {t_w} = {x_c};")
                        self.loop_lines.append(f"dline_{line_idx}[dsp->{wpos_var}] = {t_w};")
                    t_r = f"fDelOut_{uid}"
                    actual_reader = reader_c
                    if reader_c == "sh_dread_1":
                        d_lo, d_hi = self._estimate_expr_bounds(d_c)
                        if (
                            d_lo is not None
                            and d_hi is not None
                            and d_lo >= 0.0
                            and d_hi <= (max_l - 4)
                        ):
                            actual_reader = "sh_dread_1_fast"
                    self.loop_lines.append(
                        f"float {t_r} = {actual_reader}(dline_{line_idx}, dsp->{wpos_var}, {d_c});"
                    )
                    expr = expr[:start] + t_r + expr[end:]

            while True:
                m = self._find_call(expr, "de.sdelay")
                if not m:
                    break
                start, end, args = m
                max_l, d_c = self._resolve_and_check_delay_bounds(
                    "de.sdelay", args[0], args[2], env, ch_suffix, expand_expr
                )
                itp_c = expand_expr(args[1], env, ch_suffix)
                x_c = expand_expr(args[3], env, ch_suffix)
                d_smoo = self.alloc_state(f"fSdel_d{ch_suffix}", 1)
                raw_k = f"sh_clipf(sh_rcp(sh_maxf(1.0f, {itp_c})), 0.0001f, 1.0f)"
                k_c = self.hoist_block_expr("k_sdel_k", raw_k) if self._is_control_rate(itp_c) else raw_k
                self.loop_lines.append(f"dsp->{d_smoo} += ({k_c}) * (({d_c}) - dsp->{d_smoo});")
                uid = self._state_counter
                self._state_counter += 1
                wpos_var = self.ensure_delay_wpos()
                sig_key = f"{ch_suffix}:{self._strip_outer_parens(x_c)}"
                if sig_key in self.delay_written_signals:
                    line_idx = self.delay_written_signals[sig_key]
                    self._update_shared_delay_line_len(line_idx, max_l)
                else:
                    line_idx = self.alloc_delay_line(f"sdel_{sig_key}", min_len=max_l)
                    self.delay_written_signals[sig_key] = line_idx
                    t_w = f"fDelIn_{uid}"
                    self.loop_lines.append(f"float {t_w} = {x_c};")
                    self.loop_lines.append(f"dline_{line_idx}[dsp->{wpos_var}] = {t_w};")
                t_r = f"fDelOut_{uid}"
                self.loop_lines.append(
                    f"float {t_r} = sh_dread_i(dline_{line_idx}, dsp->{wpos_var}, dsp->{d_smoo});"
                )
                expr = expr[:start] + t_r + expr[end:]

            while True:
                m = self._find_call(expr, "mem")
                if not m:
                    break
                start, end, args = m
                x_c = expand_expr(args[0], env, ch_suffix)
                prev_state = self.alloc_state(f"fPrev{ch_suffix}", 1)
                uid = self._state_counter
                t_cur = f"fTmp_cur_{uid}"
                t_old = f"fTmp_prev_{uid}"
                self.loop_lines.append(f"float {t_cur} = {x_c};")
                self.loop_lines.append(f"float {t_old} = dsp->{prev_state};")
                self.loop_lines.append(f"dsp->{prev_state} = {t_cur};")
                expr = expr[:start] + t_old + expr[end:]

            stateless_fns = {
                "ba.db2linear": lambda a: f"sh_powf(10.0f, 0.05f * ({a[0]}))",
                "ba.linear2db": lambda a: f"(8.6858896f * sh_logf({a[0]}))",
                "ba.if": lambda a: f"(({a[0]}) ? ({a[1]}) : ({a[2]}))",
                "ba.select2": lambda a: f"((({a[0]}) > 0.5f) ? ({a[2]}) : ({a[1]}))",
                "ba.select3": lambda a: f"((({a[0]}) < 0.5f) ? ({a[1]}) : ((({a[0]}) < 1.5f) ? ({a[2]}) : ({a[3]})))",
                "ba.sec2samp": lambda a: f"(44100.0f * ({a[0]}))",
                "ba.samp2sec": lambda a: f"(2.2675737e-05f * ({a[0]}))",
                "ba.ms2samp": lambda a: f"(44.1f * ({a[0]}))",
                "ba.tau2pole": lambda a: f"sh_expf(-sh_rcp(sh_maxf(1.0e-5f, 44100.0f * ({a[0]}))))",
                "ba.pole2tau": lambda a: f"(-sh_rcp(44100.0f * sh_logf(sh_clipf({a[0]}, 0.0001f, 0.99999f))))",
                "ba.midikey2hz": lambda a: f"(440.0f * sh_powf(2.0f, (({a[0]}) - 69.0f) * 0.083333336f))",
                "ba.hz2midikey": lambda a: f"(69.0f + 17.3123405f * sh_logf(({a[0]}) * 0.0022727273f))",
                "ba.semi2ratio": lambda a: f"sh_powf(2.0f, ({a[0]}) * 0.083333336f)",
                "ba.cent2ratio": lambda a: f"sh_powf(2.0f, ({a[0]}) * 0.00083333335f)",
                "ma.tanh": lambda a: f"sh_tanhf({a[0]})",
                "ma.fabs": lambda a: f"sh_fabsf({a[0]})",
                "abs": lambda a: f"sh_fabsf({a[0]})",
                "min": lambda a: f"sh_minf({a[0]}, {a[1]})",
                "max": lambda a: f"sh_maxf({a[0]}, {a[1]})",
                "aa.clip": lambda a: f"sh_clipf({a[2]}, {a[0]}, {a[1]})",
                "int": lambda a: f"((float)((int)({a[0]})))",
                "floor": lambda a: f"sh_floorf({a[0]})",
                "ma.floor": lambda a: f"sh_floorf({a[0]})",
                "ceil": lambda a: f"sh_ceilf({a[0]})",
                "ma.ceil": lambda a: f"sh_ceilf({a[0]})",
                "round": lambda a: f"sh_roundf({a[0]})",
                "rint": lambda a: f"sh_roundf({a[0]})",
                "ma.rint": lambda a: f"sh_roundf({a[0]})",
                "ma.frac": lambda a: f"(({a[0]}) - sh_floorf({a[0]}))",
                "ma.signum": lambda a: f"sh_signf({a[0]})",
                "fmod": lambda a: f"sh_fmodf({a[0]}, {a[1]})",
                "ma.fmod": lambda a: f"sh_fmodf({a[0]}, {a[1]})",
                "exp": lambda a: f"sh_expf({a[0]})",
                "ma.exp": lambda a: f"sh_expf({a[0]})",
                "log": lambda a: f"sh_logf({a[0]})",
                "ma.log": lambda a: f"sh_logf({a[0]})",
                "log2": lambda a: f"sh_log2f({a[0]})",
                "ma.log2": lambda a: f"sh_log2f({a[0]})",
                "log10": lambda a: f"sh_log10f({a[0]})",
                "ma.log10": lambda a: f"sh_log10f({a[0]})",
                "pow": lambda a: f"sh_powf({a[0]}, {a[1]})",
                "ma.pow": lambda a: f"sh_powf({a[0]}, {a[1]})",
                "sqrt": lambda a: f"sh_sqrtf({a[0]})",
                "ma.sqrt": lambda a: f"sh_sqrtf({a[0]})",
                "sin": lambda a: f"sh_sinf({a[0]})",
                "ma.sin": lambda a: f"sh_sinf({a[0]})",
                "cos": lambda a: f"sh_cosf({a[0]})",
                "ma.cos": lambda a: f"sh_cosf({a[0]})",
                "tan": lambda a: f"sh_tanf({a[0]})",
                "ma.tan": lambda a: f"sh_tanf({a[0]})",
                "atan": lambda a: f"sh_atanf({a[0]})",
                "ma.atan": lambda a: f"sh_atanf({a[0]})",
                "atan2": lambda a: f"sh_atan2f({a[0]}, {a[1]})",
                "ma.atan2": lambda a: f"sh_atan2f({a[0]}, {a[1]})",
                "asin": lambda a: f"sh_asinf({a[0]})",
                "ma.asin": lambda a: f"sh_asinf({a[0]})",
                "acos": lambda a: f"sh_acosf({a[0]})",
                "ma.acos": lambda a: f"sh_acosf({a[0]})",
                "sinh": lambda a: f"sh_sinhf({a[0]})",
                "ma.sinh": lambda a: f"sh_sinhf({a[0]})",
                "cosh": lambda a: f"sh_coshf({a[0]})",
                "ma.cosh": lambda a: f"sh_coshf({a[0]})",
                "ef.mix": lambda a: f"(({a[1]}) + ({a[0]}) * (({a[2]}) - ({a[1]})))",
                "ef.cubicnl": lambda a: (
                    f"sh_tanhf((({a[2]}) + ({a[1]})) * (1.0f + 3.0f * ({a[0]})))"
                    if len(a) >= 3
                    else (
                        f"sh_tanhf(({a[0]}) * (1.0f + ({a[1]})))"
                        if len(a) == 2
                        else f"sh_tanhf({a[0]})"
                    )
                ),
            }
            for fn_name, builder in stateless_fns.items():
                while True:
                    m = self._find_call(expr, fn_name)
                    if not m:
                        break
                    start, end, args = m
                    exp_args = [expand_expr(a, env, ch_suffix) for a in args]
                    built = builder(exp_args)
                    if all(self._is_control_rate(a) for a in exp_args):
                        built = self.hoist_block_expr("k_fn", built)
                    expr = expr[:start] + built + expr[end:]

            while "'" in expr:
                q_idx = expr.find("'")
                i = q_idx - 1
                while i >= 0 and expr[i].isspace():
                    i -= 1
                if i < 0:
                    break
                if expr[i] == ")":
                    depth = 1
                    i -= 1
                    while i >= 0 and depth > 0:
                        if expr[i] == ")":
                            depth += 1
                        elif expr[i] == "(":
                            depth -= 1
                        i -= 1
                    while i >= 0 and (expr[i].isalnum() or expr[i] in "_."):
                        i -= 1
                    atom_start = i + 1
                else:
                    while i >= 0 and (expr[i].isalnum() or expr[i] in "_."):
                        i -= 1
                    atom_start = i + 1
                atom_expr = expr[atom_start:q_idx].strip()
                prev_state = self.alloc_state(f"fPrev{ch_suffix}", 1)
                uid = self._state_counter
                t_cur = f"fTmp_cur_{uid}"
                t_old = f"fTmp_prev_{uid}"
                self.loop_lines.append(f"float {t_cur} = {atom_expr};")
                self.loop_lines.append(f"float {t_old} = dsp->{prev_state};")
                self.loop_lines.append(f"dsp->{prev_state} = {t_cur};")
                expr = expr[:atom_start] + t_old + expr[q_idx + 1 :]

            return expr

        proc_args, proc_rhs = defs["process"]
        if len(proc_args) == 2:
            env = {proc_args[0]: "in0", proc_args[1]: "in1"}
            if "process" in self.with_map:
                for wname, wargs, wrhs in self.with_map["process"]:
                    if wargs:
                        continue
                    w_c = expand_expr(wrhs, env, "_M")
                    if self._is_control_rate(w_c):
                        env[wname] = self.hoist_block_expr(f"k_{wname}", w_c)
                    else:
                        uid = self._state_counter
                        self._state_counter += 1
                        w_tmp = f"fWith_process_{wname}_{uid}"
                        self.loop_lines.append(f"float {w_tmp} = {w_c};")
                        env[wname] = w_tmp
            eval_rhs = self._eval_pipeline(proc_rhs)
            parts = self._split_top_comma(eval_rhs)
            if len(parts) == 2:
                out_l = expand_expr(parts[0], env, "_L")
                out_r = expand_expr(parts[1], env, "_R")
                self.loop_lines.append(f"out0 = {out_l};")
                self.loop_lines.append(f"out1 = {out_r};")
            else:
                out_m = expand_expr(eval_rhs, env, "_M")
                out_parts = self._split_top_comma(out_m)
                if len(out_parts) == 2:
                    self.loop_lines.append(f"out0 = {out_parts[0].strip()};")
                    self.loop_lines.append(f"out1 = {out_parts[1].strip()};")
                else:
                    self.loop_lines.append(f"float fMonoOut = {out_m};")
                    self.loop_lines.append("out0 = fMonoOut;")
                    self.loop_lines.append("out1 = fMonoOut;")
        elif len(proc_args) == 1:
            x_param = proc_args[0]
            eval_rhs = self._eval_pipeline(proc_rhs)
            parts = self._split_top_comma(eval_rhs)
            if len(parts) == 2:
                out_l = expand_expr(parts[0], {x_param: "in0"}, "_L")
                out_r = expand_expr(parts[1], {x_param: "in0"}, "_R")
                self.loop_lines.append(f"out0 = {out_l};")
                self.loop_lines.append(f"out1 = {out_r};")
            else:
                for in_var, out_var, sfx in (
                    ("in0", "out0", "_L"),
                    ("in1", "out1", "_R"),
                ):
                    val_c = expand_expr(eval_rhs, {x_param: in_var}, sfx)
                    self.loop_lines.append(f"{out_var} = {val_c};")
        else:
            m_pan = self._find_call(proc_rhs, "sp.panner")
            if m_pan and m_pan[0] == 0 and m_pan[1] == len(proc_rhs.strip()):
                pan_c = expand_expr(m_pan[2][0], {}, "_M")
                x_c = expand_expr(m_pan[2][1], {}, "_M") if len(m_pan[2]) > 1 else "in0"
                self.loop_lines.append(f"float fPanIn = {x_c};")
                self.loop_lines.append(f"float fPanP = sh_clipf({pan_c}, 0.0f, 1.0f);")
                self.loop_lines.append("out0 = fPanIn * sh_sqrtf(1.0f - fPanP);")
                self.loop_lines.append("out1 = fPanIn * sh_sqrtf(fPanP);")
                return
            eval_rhs = self._eval_pipeline(proc_rhs)
            parts = self._split_top_comma(eval_rhs)
            if len(parts) == 2:
                for idx_ch, (in_var, out_var, sfx) in enumerate((
                    ("in0", "out0", "_L"),
                    ("in1", "out1", "_R"),
                )):
                    br = parts[idx_ch].strip()
                    if re.search(r"\b_\b", br):
                        call_e = re.sub(r"\b_\b", in_var, br)
                    else:
                        call_e = self._apply_pipe_stage(br, in_var)
                    val_c = expand_expr(call_e, {}, sfx)
                    self.loop_lines.append(f"{out_var} = {val_c};")
            elif ":" in proc_rhs and "<:" not in proc_rhs and ":>" not in proc_rhs:
                stages = self._split_top_colon(proc_rhs)
                for in_var, out_var, sfx in (
                    ("in0", "out0", "_L"),
                    ("in1", "out1", "_R"),
                ):
                    cur_sig = in_var
                    for st_idx, st in enumerate(stages):
                        call_expr = self._apply_pipe_stage(st.strip(), cur_sig)
                        val_c = expand_expr(call_expr, {}, sfx)
                        tmp_var = f"fPipe{sfx}_{st_idx}"
                        self.loop_lines.append(f"float {tmp_var} = {val_c};")
                        cur_sig = tmp_var
                    self.loop_lines.append(f"{out_var} = {cur_sig};")
            else:
                for in_var, out_var, sfx in (
                    ("in0", "out0", "_L"),
                    ("in1", "out1", "_R"),
                ):
                    rhs_sub = re.sub(r"\b_\b", in_var, eval_rhs)
                    val_c = expand_expr(rhs_sub, {}, sfx)
                    self.loop_lines.append(f"{out_var} = {val_c};")

    def _eval_pipeline(
        self,
        expr: str,
        env: Optional[Dict[str, str]] = None,
        ch_suffix: str = "",
        expand_fn=None,
    ) -> str:
        """
        Evaluate Faust block-diagram composition operators (`:`, `<:` split, `:>` merge)
        at the top level of `expr` (and inside parenthesized sub-pipelines).
        """
        expr = expr.strip()
        tokens = self._split_pipeline_ops(expr)
        if len(tokens) <= 1:
            return expr
        first_stage = tokens[0][1].strip()
        wires = [w.strip() for w in self._split_top_comma(first_stage)]
        for op, stage in tokens[1:]:
            stage = stage.strip()
            targets = [t.strip() for t in self._split_top_comma(stage)]
            if op == "<:":
                if expand_fn is not None and len(targets) > len(wires):
                    bound_wires = []
                    for w in wires:
                        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", w):
                            bound_wires.append(w)
                        else:
                            w_c = expand_fn(w, env or {}, ch_suffix)
                            uid = self._state_counter
                            self._state_counter += 1
                            tmp_w = f"fSplit_{uid}"
                            self.loop_lines.append(f"float {tmp_w} = {w_c};")
                            bound_wires.append(tmp_w)
                    wires = bound_wires
                wires = [
                    self._apply_pipe_stage(b, wires[idx % len(wires)], env, ch_suffix, expand_fn)
                    for idx, b in enumerate(targets)
                ]
            elif op == ":>":
                nt = max(1, len(targets))
                merged = [
                    " + ".join(f"({wires[i]})" for i in range(k, len(wires), nt))
                    for k in range(nt)
                ]
                wires = [self._apply_pipe_stage(t, m, env, ch_suffix, expand_fn) for t, m in zip(targets, merged)]
            else:
                if len(wires) == len(targets):
                    wires = [self._apply_pipe_stage(t, w, env, ch_suffix, expand_fn) for t, w in zip(targets, wires)]
                elif len(wires) == 2 and stage in ("+", "-", "*", "/"):
                    wires = [f"(({wires[0]}) {stage} ({wires[1]}))"]
                elif len(wires) == 2 and stage in ("min", "max"):
                    wires = [f"{stage}({wires[0]}, {wires[1]})"]
                elif len(targets) == 1 and len(wires) > 1:
                    wires = [self._apply_multi_pipe_stage(targets[0], wires)]
                else:
                    wires = [self._apply_pipe_stage(stage, ", ".join(wires), env, ch_suffix, expand_fn)]
        return ", ".join(wires)

    @staticmethod
    def _split_pipeline_ops(expr: str) -> List[Tuple[str, str]]:
        """Split `expr` at top-level `<:`, `:>`, and `:` into `[(op, stage), ...]`."""
        expr = expr.strip()
        while expr.startswith("(") and expr.endswith(")"):
            depth = 0
            matched = False
            for idx, ch in enumerate(expr):
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                if depth == 0 and idx < len(expr) - 1:
                    matched = False
                    break
                elif depth == 0 and idx == len(expr) - 1:
                    matched = True
            if matched:
                expr = expr[1:-1].strip()
            else:
                break
        res: List[Tuple[str, str]] = []
        cur: List[str] = []
        cur_op = ""
        depth = 0
        q_depth = 0
        i = 0
        n = len(expr)
        while i < n:
            ch = expr[i]
            if ch in "([{":
                depth += 1
                cur.append(ch)
                i += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
                cur.append(ch)
                i += 1
            elif depth == 0 and ch == "?":
                q_depth += 1
                cur.append(ch)
                i += 1
            elif depth == 0 and expr[i : i + 2] in ("<:", ":>"):
                res.append((cur_op, "".join(cur)))
                cur_op = expr[i : i + 2]
                cur = []
                i += 2
            elif depth == 0 and ch == ":":
                if q_depth > 0:
                    q_depth -= 1
                    cur.append(ch)
                    i += 1
                else:
                    res.append((cur_op, "".join(cur)))
                    cur_op = ":"
                    cur = []
                    i += 1
            else:
                cur.append(ch)
                i += 1
        if cur:
            res.append((cur_op, "".join(cur)))
        return res

    def _rewrite_infix_operators(self, expr: str) -> str:
        """
        Rewrites Faust infix operators that differ from C:
          - `a @ d` -> N-sample delay (`'` chain for small const 1..4, else `de.fdelay(1024, d, a)`)
          - `a ^ b` -> `pow(a, b)`
          - `a % b` -> `ma.fmod(a, b)`
        """
        for op_ch, target_fn in (("^", "pow"), ("%", "ma.fmod"), ("@", "@delay")):
            guard = 0
            while op_ch in expr and guard < 128:
                guard += 1
                idx = expr.find(op_ch)
                if idx < 0:
                    break
                l_start = self._extract_atom_lhs(expr, idx)
                r_end = self._extract_atom_rhs(expr, idx)
                lhs = expr[l_start:idx].strip()
                rhs = expr[idx + 1 : r_end].strip()
                if not lhs or not rhs:
                    break
                if target_fn == "@delay":
                    if re.match(r"^\d+$", rhs):
                        d_int = int(rhs)
                        if d_int <= 0:
                            repl = f"({lhs})"
                        elif d_int <= 4:
                            repl = f"({lhs})" + ("'" * d_int)
                        else:
                            repl = f"de.fdelay({d_int}, {d_int}.0f, {lhs})"
                    else:
                        repl = f"de.fdelay({rhs}, {rhs}, {lhs})"
                else:
                    repl = f"{target_fn}({lhs}, {rhs})"
                expr = expr[:l_start] + repl + expr[r_end:]
        return expr

    @staticmethod
    def _extract_atom_lhs(s: str, op_idx: int) -> int:
        i = op_idx - 1
        while i >= 0 and s[i].isspace():
            i -= 1
        if i < 0:
            return op_idx
        if s[i] == ")":
            depth = 1
            i -= 1
            while i >= 0 and depth > 0:
                if s[i] == ")":
                    depth += 1
                elif s[i] == "(":
                    depth -= 1
                i -= 1
            while i >= 0 and (s[i].isalnum() or s[i] in "_."):
                i -= 1
            return i + 1
        while i >= 0 and (s[i].isalnum() or s[i] in "_.'"):
            i -= 1
        return i + 1

    @staticmethod
    def _extract_atom_rhs(s: str, op_idx: int) -> int:
        n = len(s)
        i = op_idx + 1
        while i < n and s[i].isspace():
            i += 1
        while i < n and s[i] in "+-":
            i += 1
            while i < n and s[i].isspace():
                i += 1
        if i < n and s[i] == "(":
            depth = 1
            i += 1
            while i < n and depth > 0:
                if s[i] == "(":
                    depth += 1
                elif s[i] == ")":
                    depth -= 1
                i += 1
            return i
        while i < n and (s[i].isalnum() or s[i] in "_."):
            i += 1
        if i < n and s[i] == "(":
            depth = 1
            i += 1
            while i < n and depth > 0:
                if s[i] == "(":
                    depth += 1
                elif s[i] == ")":
                    depth -= 1
                i += 1
        return i

    @staticmethod
    def _apply_multi_pipe_stage(stage: str, sigs: List[str]) -> str:
        stage = stage.strip()
        while stage.startswith("(") and stage.endswith(")"):
            stage = stage[1:-1].strip()
        joined = ", ".join(sigs)
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*\((.*)\)$", stage, re.S)
        if m:
            fn = m.group(1)
            inner = m.group(2).strip()
            if inner:
                return f"{fn}({inner}, {joined})"
            return f"{fn}({joined})"
        return f"{stage}({joined})"

    @staticmethod
    def _strip_outer_parens(expr: str) -> str:
        expr = expr.strip()
        while expr.startswith("(") and expr.endswith(")"):
            depth = 0
            matched = False
            for idx, ch in enumerate(expr):
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                if depth == 0 and idx < len(expr) - 1:
                    matched = False
                    break
                elif depth == 0 and idx == len(expr) - 1:
                    matched = True
            if matched:
                expr = expr[1:-1].strip()
            else:
                break
        return expr

    def _apply_pipe_stage(
        self,
        stage: str,
        sig: str,
        env: Optional[Dict[str, str]] = None,
        ch_suffix: str = "",
        expand_fn=None,
    ) -> str:
        stage = self._strip_outer_parens(stage)
        if len(self._split_pipeline_ops(stage)) > 1:
            return self._eval_pipeline(f"({sig}) : {stage}", env, ch_suffix, expand_fn)
        m_op = re.match(r"^([\*\+\-/])\s*\((.*)\)$", stage, re.S)
        if m_op:
            op, arg = m_op.group(1), m_op.group(2).strip()
            return f"(({sig}) {op} ({arg}))"
        if re.search(r"\b_\b", stage):
            return re.sub(r"\b_\b", f"({sig})", stage)
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*)\s*\((.*)\)$", stage, re.S)
        if m:
            fn = m.group(1)
            inner = m.group(2).strip()
            if inner:
                return f"{fn}({inner}, {sig})"
            return f"{fn}({sig})"
        return f"{stage}({sig})"

    @staticmethod
    def _find_call(expr: str, fn_name: str) -> Optional[Tuple[int, int, List[str]]]:
        m = re.search(rf"\b{re.escape(fn_name)}\s*\(", expr)
        if not m:
            return None
        start = m.start()
        p_open = expr.find("(", m.end() - 1)
        depth = 1
        i = p_open + 1
        args = []
        cur = []
        while i < len(expr) and depth > 0:
            ch = expr[i]
            if ch == "(":
                depth += 1
                cur.append(ch)
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    args.append("".join(cur).strip())
                    break
                cur.append(ch)
            elif ch == "," and depth == 1:
                args.append("".join(cur).strip())
                cur = []
            else:
                cur.append(ch)
            i += 1
        return start, i + 1, [a for a in args if a != ""]

    @staticmethod
    def _split_top_colon(expr: str) -> List[str]:
        parts = []
        cur = []
        depth = 0
        for ch in expr:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            if ch == ":" and depth == 0:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        if cur:
            parts.append("".join(cur))
        return parts

    @staticmethod
    def _split_top_comma(expr: str) -> List[str]:
        expr = expr.strip()
        while expr.startswith("(") and expr.endswith(")"):
            depth = 0
            matched_end = False
            for idx, ch in enumerate(expr):
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth -= 1
                if depth == 0 and idx < len(expr) - 1:
                    matched_end = False
                    break
                elif depth == 0 and idx == len(expr) - 1:
                    matched_end = True
            if matched_end:
                expr = expr[1:-1].strip()
            else:
                break
        parts = []
        cur = []
        depth = 0
        for ch in expr:
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                depth = max(0, depth - 1)
            if ch == "," and depth == 0:
                parts.append("".join(cur))
                cur = []
            else:
                cur.append(ch)
        if cur:
            parts.append("".join(cur))
        return parts


def analyze_faust_memory(
    dsp_text: str,
    search_dirs: Optional[List[Path]] = None,
) -> FaustMemoryReport:
    """
    Perform static memory analysis across all function invocations in a FAUST
    `.dsp` script without writing files, returning a `FaustMemoryReport` or
    raising `FaustMemoryLimitError` if any hardware memory budget (`instance[2]`
    188-byte state, `instance[3]` 529,200-byte SDRAM delay cap, 32-line L1D/L2
    cache-set limit, delay tap capacity bounds, or 512-variable per-sample stack
    temporary limit) is exceeded.
    """
    meta, params = parse_faust_dsp_metadata(dsp_text)
    compiler = FaustSubsetCompiler(dsp_text, meta, params, search_dirs=search_dirs)
    compiler.compile()
    assert compiler.memory_report is not None
    return compiler.memory_report
