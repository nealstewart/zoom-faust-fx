"""Fail-closed allowlist of safe Faust imports, modules, and directives for Zoom MS Plus."""

from __future__ import annotations

import re

from .models import strip_c_comments

ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        "stdfaust.lib",
        "zoom.lib",
    }
)

# Per-module function allowlist:
#   - None => all functions in that module are allowed
#   - frozenset[str] => only those verified functions in that module are allowed
ALLOWED_MODULE_FUNCTIONS: dict[str, frozenset[str] | None] = {
    "aa": None,
    "an": None,
    "ba": None,
    "co": frozenset(
        {
            "compressor_mono",
            "compressor_stereo",
            "limiter_1176_R4_mono",
            "limiter_1176_R4_stereo",
        }
    ),
    "de": None,
    "ef": frozenset(
        {
            "cubicnl",
            "cubicnl_nodc",
            "gate_mono",
            "gate_stereo",
            "speakerbp",
            "stereo_width",
            "ms_enc",
            "ms_dec",
            "dryWetMixer",
            "dryWetMixerConstantPower",
            "echo",
            "transpose",
            "softclipQuadratic",
            "wavefold",
        }
    ),
    "en": None,
    "fi": None,
    "ma": None,
    "no": None,
    "os": None,
    "re": frozenset({"dattorro_rev", "jcrev"}),
    "ro": None,
    "si": None,
    "sp": None,
    "ve": frozenset(
        {
            "moog_vcf",
            "moogLadder",
            "korg35LPF",
            "oberheimLPF",
            "crybaby",
            "autowah",
            "wah4",
        }
    ),
}

ALLOWED_MODULES: frozenset[str] = frozenset(ALLOWED_MODULE_FUNCTIONS.keys())

FORBIDDEN_DIRECTIVES: tuple[str, ...] = ("ffunction", "fvariable", "fconstant")
UNSUPPORTED_UI_ELEMENTS: tuple[str, ...] = ("hbargraph", "vbargraph")

ALTERNATIVE_SUGGESTIONS: dict[str, str] = {
    "re.stereo_freeverb": "Use zm_freeverb(room, damp, width) from zoom.lib, or re.dattorro_rev / re.jcrev.",
    "re.mono_freeverb": "Use zm_freeverb(room, damp, width) from zoom.lib, or re.dattorro_rev / re.jcrev.",
    "re.zita_rev1_stereo": "Use re.dattorro_rev, re.jcrev, or zm_freeverb from zoom.lib.",
    "re.jpverb": "Use re.dattorro_rev, re.jcrev, or zm_freeverb from zoom.lib.",
    "re.greyhole": "Use re.dattorro_rev, re.jcrev, or zm_freeverb from zoom.lib.",
    "co.RMS_FBFFcompressor_N_chan": "Use co.compressor_stereo, co.compressor_mono, or co.limiter_1176_R4_stereo.",
    "co.RMS_compressor_N_chan": "Use co.compressor_stereo or co.compressor_mono.",
    "ef.echo1s": "Use ef.echo(0.5, duration, feedback) or zm_delay from zoom.lib.",
    "ef.echo2s": "Use ef.echo(0.5, duration, feedback) or zm_delay from zoom.lib.",
    "ef.echo5s": "Use ef.echo(0.5, duration, feedback) or zm_delay from zoom.lib.",
    "ef.echo10s": "Use ef.echo(0.5, duration, feedback) or zm_delay from zoom.lib.",
    "ef.echo21s": "Use ef.echo(0.5, duration, feedback) or zm_delay from zoom.lib.",
    "ef.echo43s": "Use ef.echo(0.5, duration, feedback) or zm_delay from zoom.lib.",
    "ve": "Use fi.lowpass, fi.highpass, or zm_svf_lp / zm_svf_hp / zm_svf_peak from zoom.lib.",
}


class FaustAllowlistError(ValueError):
    """Raised when a Faust .dsp uses a disallowed import, module, or FFI directive."""


def validate_faust_allowlist(dsp_text: str) -> None:
    """Validate that dsp_text only uses allowlisted Faust imports, modules, and directives."""
    clean = strip_c_comments(dsp_text)
    # Strip string literals for directive/module scanning (keep imports checked separately)
    for m in re.finditer(r'import\s*\(\s*"([^"]+)"\s*\)', clean):
        lib = m.group(1).strip()
        if lib not in ALLOWED_IMPORTS:
            allowed = ", ".join(sorted(ALLOWED_IMPORTS))
            raise FaustAllowlistError(
                f"Disallowed Faust import '{lib}'. Only allowlisted libraries are permitted: {allowed}."
            )

    no_strings = re.sub(r'"(?:\\.|[^"\\])*"', '""', clean)

    for directive in FORBIDDEN_DIRECTIVES:
        if re.search(rf"\b{directive}\s*\(", no_strings):
            raise FaustAllowlistError(
                f"Disallowed Faust FFI directive '{directive}(...)'. "
                "User DSPs cannot inject raw C foreign functions/variables."
            )

    for ui_elem in UNSUPPORTED_UI_ELEMENTS:
        if re.search(rf"\b{ui_elem}\s*\(", no_strings):
            raise FaustAllowlistError(
                f"Unsupported Faust UI element '{ui_elem}(...)'. "
                "Zoom MS Plus hardware does not support output meters/bargraphs."
            )

    disallowed_mods: list[str] = []
    for m in re.finditer(r"\b([a-z]{2})\.([A-Za-z_][A-Za-z0-9_]*)\b", no_strings):
        mod, sym = m.group(1), m.group(2)
        full = f"{mod}.{sym}"
        alt = ALTERNATIVE_SUGGESTIONS.get(full) or ALTERNATIVE_SUGGESTIONS.get(mod)
        alt_suffix = f" Alternative: {alt}" if alt else ""
        if mod not in ALLOWED_MODULE_FUNCTIONS:
            disallowed_mods.append(f"{full} (module '{mod}' not allowed).{alt_suffix}")
        else:
            allowed_syms = ALLOWED_MODULE_FUNCTIONS[mod]
            if allowed_syms is not None and sym not in allowed_syms:
                opts = ", ".join(sorted(allowed_syms))
                disallowed_mods.append(f"{full} (allowed in {mod}.*: {opts}).{alt_suffix}")

    if disallowed_mods:
        mods_str = "; ".join(sorted(set(disallowed_mods)))
        raise FaustAllowlistError(f"Disallowed Faust symbol(s): {mods_str}.")

    for m in re.finditer(r"\bde\.(?:delay|fdelay[1-3]?|sdelay)\s*\(\s*(\d+)\s*,", no_strings):
        n = int(m.group(1))
        if n <= 0 or (n & (n - 1)) != 0:
            next_p2 = 1
            while next_p2 < max(1, n):
                next_p2 *= 2
            raise FaustAllowlistError(
                f"Non-power-of-two delay buffer size {n} in '{m.group(0)} ...)' is not supported "
                f"on Zoom MS Plus (emits '%' modulo / CALLP __c6xabi_remi). "
                f"Use a power of two instead (e.g. {next_p2})."
            )


def validate_expanded_faust(expanded_dsp: str) -> None:
    """Validate macro-expanded Faust DSP (`faust -e`) for transitive banned constructs."""
    clean = strip_c_comments(expanded_dsp)
    no_strings = re.sub(r'"(?:\\.|[^"\\])*"', '""', clean)

    for ui_elem in UNSUPPORTED_UI_ELEMENTS:
        if re.search(rf"\b{ui_elem}\s*\(", no_strings):
            raise FaustAllowlistError(
                f"Transitive unsupported Faust UI element '{ui_elem}(...)' detected after expansion. "
                "Zoom MS Plus hardware does not support output meters/bargraphs."
            )

    for m in re.finditer(r"\bffunction\s*\([^,]+,\s*<([^>]+)>", no_strings):
        hdr = m.group(1).strip()
        if hdr not in ("math.h", "stdlib.h"):
            raise FaustAllowlistError(
                f"Transitive disallowed Faust FFI header '<{hdr}>' detected after expansion."
            )
