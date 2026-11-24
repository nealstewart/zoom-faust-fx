// effects/ps1chr/ps1chr.dsp
//
// Sony PlayStation 1 SPU Multi-Voice Gaussian Chorus & Ensemble ("PS1CHR")
// implemented in FAUST and compiled to a Zoom MS-70CDR+ (.ZD2 + .ZIC) package via:
//   python tools/faust2zd2.py effects/ps1chr/ps1chr.dsp --build
//
// DSP Architecture (Stereo-In -> Wide Stereo-Out, 2 x 2048-float SDRAM Ring Buffers):
//   1. Multi-Voice SPU Detune & Tri-Phase Modulation (0 / 120 / 240 deg) using
//      cubic-Lagrange fractional delay (`de.fdelay3(2048, ...)`).
//   2. 4 Hardware-Inspired PS1 SPU Modes:
//        - DUAL  : 2-voice classic PS1 stereo detune (L/R opposite phase)
//        - TRIO  : 3-voice lush tri-phase SPU ensemble (Chrono Cross / Ico pads)
//        - NEGV  : SPU negative-volume phase-inverted stereo widening
//        - VBLNK : 60 Hz VBlank-stepped pitch LFO + extra Gaussian/16-bit SPU haze
//   3. Sony SPU 4-Point Gaussian High-Frequency Roll-Off (`zm_ema` cascaded LPF)
//      + 16-bit signed accumulator warmth (`zm_sat16`) + 110 Hz wet HPF so bass
//      fundamentals stay solid.
//   4. Dry/Wet `Mix` (up to 100% wet vibrato/ensemble), `stock_vol` `Level`, and
//      trailing `ON/OFF` (`LATCH | UNLATCH`) footswitch.

declare name "XACHR";
declare zd2_filename "XXXACHR";
declare zd2_id "07001200";
declare version "1.00";
declare zd2_display "XA CHR";
declare zd2_badge "XA";
declare description "22.05 kHz 3-voice stereo modulation chorus with 16-bit saturation and warm low-pass rolloff.";
declare zd2_dspload "26";
declare zd2_footswitch "true";

import("stdfaust.lib");
import("zoom.lib");

// ---- 7 Knobs + Trailing Footswitch Toggle (2 Pages of 4 on Zoom MS-70CDR+) ----
mode_sel   = hslider("Mode[1][values:DUAL|TRIO|NEGV|VBLNK][tooltip:PS1 chorus voicing (DUAL = 2-voice stereo, TRIO = 3-voice ensemble, NEGV = SPU negative-volume wide stereo, VBLNK = 60Hz stepped pitch LFO).]", 1, 0, 3, 1);
rate_knob  = hslider("Rate[2][tooltip:Modulation speed (0.10..5.50 Hz).]", 24, 0, 100, 1) * 0.01 : si.smoo;
depth_knob = hslider("Depth[3][tooltip:Chorus pitch/delay sweep depth (0..100%).]", 55, 0, 100, 1) * 0.01 : si.smoo;
delay_knob = hslider("Delay[4][tooltip:Base voice delay offset (4..24 ms; tight doubler to wide ensemble).]", 35, 0, 100, 1) * 0.01 : si.smoo;
tone_knob  = hslider("Tone[5][tooltip:PS1 4-point Gaussian interpolation darkening (0 = bright, 100 = warm hazy 90s ROMpler).]", 45, 0, 100, 1) * 0.01 : si.smoo;
mix_knob   = hslider("Mix[6][tooltip:Dry/Wet blend (0 = dry, 50 = classic chorus, 100 = 100% wet vibrato/ensemble).]", 50, 0, 100, 1) * 0.01 : si.smoo;
level_knob = hslider("Level[7][curve:stock_vol][tooltip:Master output level (80 is unity).]", 80, 0, 100, 1) : si.smoo;

// ---- 1. Mode Tables (Hoisted to Block-Rate) ----
sel4(m, v0, v1, v2, v3) =
    ba.if(m < 0.5, v0,
    ba.if(m < 1.5, v1,
    ba.if(m < 2.5, v2, v3)));

// Voice 3 ensemble blend (0 in DUAL, strong in TRIO/VBLNK, medium in NEGV)
v3_gain   = sel4(mode_sel, 0.00, 0.62, 0.40, 0.58);
// Right-channel polarity (-1 in NEGV SPU negative-volume mode, +1 otherwise)
r_pol     = sel4(mode_sel, 1.00, 1.00, -1.00, 1.00);
// Extra Gaussian darkening in VBLNK mode
tone_bias = sel4(mode_sel, 0.00, 0.05, 0.05, 0.22);
// 60 Hz VBlank staircase quantization amount (active in VBLNK mode)
is_vblnk  = ba.if(mode_sel > 2.5, 1.0, 0.0);

// ---- 2. Block-Rate Coefficients ----
// Perceptual exponential LFO rate: 0.12 Hz .. 5.2 Hz
lfo_hz    = 0.12 + rate_knob * rate_knob * 5.08;
// Base voice delay in samples (4..24 ms -> 176..1058 samples)
base_smps = 176.0 + delay_knob * 882.0;
// Sweep depth in samples (up to ~240 samples = ~5.4 ms)
mod_smps  = depth_knob * (0.35 + 0.65 * depth_knob) * 240.0;
// Gaussian interpolation LPF alpha (0.85 bright -> 0.16 warm hazy)
tone_eff  = aa.clip(0.0, 1.0, tone_knob + tone_bias);
g_alpha   = max(0.15, 0.85 - tone_eff * 0.68);
// Output gain compensation when 3 voices are active
wet_norm  = 1.0 / (1.0 + 0.52 * v3_gain);
dry_gain  = sqrt(max(0.0, 1.0 - mix_knob));
wet_gain  = sqrt(max(0.0, mix_knob));

// ---- 3. Tri-Phase LFOs (0, 120, 240 deg) + Optional 60 Hz VBlank Stepped Quantization ----
// Construct 120-degree and 240-degree phase-shifted sines from sin/cos pair
s0 = os.osc(lfo_hz);
c0 = os.osccos(lfo_hz);
lfo1_raw = s0;
lfo2_raw = -0.5 * s0 + 0.8660254 * c0;
lfo3_raw = -0.5 * s0 - 0.8660254 * c0;

// In VBLNK mode, quantize LFO to 16 discrete steps (~60 Hz pitch-register feel)
step_q(u) = ba.if(is_vblnk > 0.5, int(u * 8.0) * 0.125, u);
lfo1 = step_q(lfo1_raw);
lfo2 = step_q(lfo2_raw);
lfo3 = step_q(lfo3_raw);

// ---- 4. PS1 SPU Gaussian Voice Conditioning (110 Hz HPF + 2-Stage Gaussian LPF + 16-Bit Sat) ----
spu_gauss_wet(x) = zm_sat16(zm_ema(g_alpha, zm_ema(g_alpha, fi.highpass(1, 110.0, x))));

// ---- 5. Complete Stereo 3-Voice PS1 Chorus ----
// Multiple `de.fdelay3(2048, ...)` reads on `inL` / `inR` share 2 x 2048-float SDRAM buffers (16 KB)
process(xL, xR) = (xL * dry_gain + wetL * wet_gain) * level_knob,
                  (xR * dry_gain + wetR * wet_gain) * level_knob
    with {
        inL = spu_gauss_wet(xL);
        inR = spu_gauss_wet(xR);

        d1 = aa.clip(16.0, 2000.0, base_smps * 0.85 + mod_smps * lfo1);
        d2 = aa.clip(16.0, 2000.0, base_smps * 1.15 + mod_smps * lfo2);
        d3 = aa.clip(16.0, 2000.0, base_smps * 1.45 + mod_smps * lfo3);

        // Voice 1 (L), Voice 2 (R), Voice 3 (cross-channel ensemble)
        v1L = de.fdelay3(2048, d1, inL);
        v3L = de.fdelay3(2048, d3, inL);
        v2R = de.fdelay3(2048, d2, inR);
        v3R = de.fdelay3(2048, d3, inR);

        wetL = (v1L + v3_gain * v3R) * wet_norm;
        wetR = (r_pol * v2R + v3_gain * v3L) * wet_norm;
    };
