"""
tools/faust2zd2_pkg/__init__.py — Modular FAUST (.dsp / C) to Zoom MS Plus (.ZD2)
transpiler and C674x bare-metal kernel emitter.

STRICT STANDARD-FAUST POLICY:
  This package must remain strictly orthogonal to individual effects and implement
  ONLY standard Faust language constructs and official `stdfaust.lib` library
  primitives (`de.*`, `fi.*`, `si.*`, `os.*`, `no.*`, `an.*`, `co.*`, `ef.*`,
  `ba.*`, `ma.*`, `aa.*`, `sp.*`). Never add non-standard or custom helper
  functions to `faust2zd2_pkg`.
"""

from .allowlist import (
    ALLOWED_IMPORTS,
    ALLOWED_MODULES,
    ALLOWED_MODULE_FUNCTIONS,
    FaustAllowlistError,
    validate_expanded_faust,
    validate_faust_allowlist,
)
from .models import (
    MAX_DELAY_LINES,
    MAX_PATTERN_COUNT,
    MAX_SAFE_SDRAM_BYTES,
    MAX_SAMPLE_TEMP_BYTES,
    MAX_SAMPLE_TEMPS,
    MAX_STATE_BYTES,
    MAX_STATE_WORDS,
    FaustMemoryLimitError,
    FaustMemoryReport,
    FaustMeta,
    FaustParam,
    LfoSpec,
    parse_faust_dsp_metadata,
)
from .rewriter import (
    C674X_FAUST_HEADER,
    MATH_REDIRECTS,
    enforce_float32_literals,
    rewrite_c_divisions_and_math,
    strip_c_comments,
)
from .subset_compiler import FaustSubsetCompiler, analyze_faust_memory
from .c_backend import convert_faust_c_to_zd2_kernel
from .emitter import _emit_c674x_kernel_from_parts, build_manifest_dict

__all__ = [
    "ALLOWED_IMPORTS",
    "ALLOWED_MODULES",
    "ALLOWED_MODULE_FUNCTIONS",
    "FaustAllowlistError",
    "validate_expanded_faust",
    "validate_faust_allowlist",
    "MAX_STATE_BYTES",
    "MAX_STATE_WORDS",
    "MAX_SAFE_SDRAM_BYTES",
    "MAX_DELAY_LINES",
    "MAX_SAMPLE_TEMPS",
    "MAX_SAMPLE_TEMP_BYTES",
    "MAX_PATTERN_COUNT",
    "FaustMemoryLimitError",
    "FaustMemoryReport",
    "FaustMeta",
    "FaustParam",
    "LfoSpec",
    "parse_faust_dsp_metadata",
    "C674X_FAUST_HEADER",
    "MATH_REDIRECTS",
    "enforce_float32_literals",
    "rewrite_c_divisions_and_math",
    "strip_c_comments",
    "FaustSubsetCompiler",
    "analyze_faust_memory",
    "convert_faust_c_to_zd2_kernel",
    "_emit_c674x_kernel_from_parts",
    "build_manifest_dict",
]
