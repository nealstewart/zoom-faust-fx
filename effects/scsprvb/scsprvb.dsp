// effects/scsprvb/scsprvb.dsp
//
// Sega Saturn Yamaha YMF292 (SCSP) Programmable 128-Step DSP Reverb
// compiled to a Zoom MS-70CDR+ (.ZD2) package via:
//   python tools/faust2zd2.py effects/scsprvb/scsprvb.dsp --build
//
// Hardware Architecture Modeled:
//   1. Full 44.1 kHz Yamaha SCSP DSP rate (crisp, airy mid-90s rack ambience —
//      distinct from the PS1 SPU's 22.05 kHz half-rate reverb).
//   2. SCSP `PACK`/`UNPACK` 16-bit floating-point delay-RAM compression
//      (exponent + mantissa quantization whose step size tracks signal level,
//      yielding glassy Yamaha-DSP shimmer without harsh low-level truncation).
//   3. Dual input Schroeder all-pass diffusers feeding a 4-line orthogonal
//      Hadamard FDN tank in external SDRAM with gentle LFO tail modulation.
//   4. 5 Saturn reverb presets (`ROOM`, `HALL`, `CATH`, `GLSS`, `CAVE`) +
//      momentary footswitch throw (`ON/OFF = LATCH / UNLATCH`).

declare name "SCSP Rvb";
declare zd2_filename "XXSCSPRV";
declare zd2_id "07001220";
declare version "1.00";
declare zd2_display "SCSP RVB";
declare zd2_badge "SC";
declare description "YMF292 SCSP 44.1 kHz programmable DSP reverb with PACK/UNPACK floating-point delay RAM compression, Hadamard FDN tank, and 5 presets.";
declare zd2_dspload "30";
declare zd2_footswitch "true";

import("stdfaust.lib");
import("zoom.lib");

// ---- 7 Knobs + Trailing Footswitch Toggle ----
mode_sel   = hslider("Mode[1][values:ROOM|HALL|CATH|GLSS|CAVE][tooltip:Sega Saturn SCSP reverb space (Room, Hall, Cathedral, Glass, Cave).]", 1, 0, 4, 1);
decay_knob = hslider("Decay[2][tooltip:SCSP FDN reverb decay time (0..100).]", 68, 0, 100, 1) * 0.01 : si.smoo;
pred_ms    = hslider("PreD[3][unit:ms][tooltip:Pre-delay before the SCSP reverb tank (0..85 ms).]", 18, 0, 85, 1) : si.smoo;
damp_knob  = hslider("Damp[4][tooltip:High-frequency absorption inside the SCSP feedback matrix (0 = bright, 100 = dark).]", 42, 0, 100, 1) * 0.01 : si.smoo;
pack_knob  = hslider("Pack[5][tooltip:SCSP PACK/UNPACK floating-point delay RAM quantization grit (0 = clean 24-bit, 50 = authentic 11-bit mantissa, 100 = crushed 6-bit).]", 50, 0, 100, 1) * 0.01 : si.smoo;
mix_knob   = hslider("Mix[6][tooltip:Dry/Wet blend (0 = dry, 50 = balanced, 100 = 100% wet SCSP tank).]", 45, 0, 100, 1) * 0.01 : si.smoo;
level_knob = hslider("Level[7][curve:stock_vol][tooltip:Master output level (80 is unity gain).]", 80, 0, 100, 1) : si.smoo;

// ---- 1. Saturn SCSP Preset Tables (44.1 kHz Prime Delay Lengths, <= 4096) ----
sel5(m, v0, v1, v2, v3, v4) =
    ba.if(m < 0.5, v0,
    ba.if(m < 1.5, v1,
    ba.if(m < 2.5, v2,
    ba.if(m < 3.5, v3, v4))));

// 4 mutually-prime FDN tank delay lengths (samples @ 44.1 kHz)
dF0 = sel5(mode_sel,  887.0, 1789.0, 2749.0, 2111.0, 1327.0);
dF1 = sel5(mode_sel, 1123.0, 2153.0, 3251.0, 2557.0, 1693.0);
dF2 = sel5(mode_sel, 1429.0, 2609.0, 3671.0, 3037.0, 2081.0);
dF3 = sel5(mode_sel, 1699.0, 3083.0, 4001.0, 3541.0, 2447.0);

// Input diffuser all-pass lengths & preset brightness/diffusion bias
dApL     = sel5(mode_sel, 173.0, 269.0, 389.0, 461.0, 311.0);
dApR     = sel5(mode_sel, 211.0, 317.0, 443.0, 523.0, 359.0);
ap_gain  = sel5(mode_sel,  0.52,  0.60,  0.66,  0.72,  0.58);
damp_min = sel5(mode_sel,  0.22,  0.20,  0.16,  0.34,  0.12);
fb_ceil  = sel5(mode_sel,  0.88,  0.94,  0.975, 0.96,  0.93);

// ---- 2. Block-Rate Tank Coefficients ----
d_inv    = 1.0 - decay_knob;
fb_gain  = min(fb_ceil, 0.42 + (fb_ceil - 0.42) * (1.0 - d_inv * d_inv));
// Orthogonal 2x2 Hadamard normalization = 0.70710678
h_fb     = fb_gain * 0.70710678;
// 1-pole LP damping coefficient inside the FDN loop
damp_a   = max(damp_min, 0.92 - damp_knob * (0.92 - damp_min));
pre_samp = ba.ms2samp(pred_ms);
dry_g    = sqrt(max(0.0, 1.0 - mix_knob));
wet_g    = sqrt(max(0.0, mix_knob));

// SCSP PACK/UNPACK mantissa resolution:
// Pack=0 -> bypassed (clean), Pack=50 -> ~1024 levels/octave (11-bit signed mantissa),
// Pack=100 -> ~24 levels/octave (coarse 6-bit Saturn float-pack shimmer)
pack_steps     = ba.if(pack_knob < 0.01, 0.0, 24.0 + (1.0 - pack_knob) * (1.0 - pack_knob) * 2024.0);
inv_pack_steps = ba.if(pack_steps < 1.0, 0.0, 1.0 / pack_steps);

// Emulate Yamaha SCSP floating-point delay-RAM PACK/UNPACK:
// Step size is proportional to the coarse envelope magnitude (preserving exact 0 -> 0)
scsp_pack(x) = ba.if(pack_steps < 1.0, x, int(x * inv_q) * q_step)
    with {
        // Piecewise-octave coarse scale (4-tier binary/linear without log2/pow or fdiv)
        ax      = abs(x);
        oct     = ba.if(ax > 0.25, ba.if(ax > 0.5, 1.0, 0.5), ba.if(ax > 0.125, 0.25, 0.0625));
        inv_oct = ba.if(ax > 0.25, ba.if(ax > 0.5, 1.0, 2.0), ba.if(ax > 0.125, 4.0, 16.0));
        q_step  = oct * inv_pack_steps;
        inv_q   = pack_steps * inv_oct;
    };

// Gentle SCSP chorus/shimmer LFO modulation for the FDN read taps
lfo1 = zm_osc(0.43) * 5.5;
lfo2 = zm_osc(0.61) * 5.5;

// ---- 3. Stereo Orthogonal Hadamard FDN Tanks in External SDRAM ----
// 4 x 4096-sample SDRAM ring buffers (~65 KB) + pre-delay + all-pass diffusers
scsp_fdn_l_step(w0, w1, inL, inR) = nw0, nw1, outL
    with {
        t0 = de.fdelay1(4096, dF0 + lfo1, w0);
        t1 = de.fdelay1(4096, dF2 - lfo1, w1);

        // 2x2 unitary Hadamard feedback rotation + SCSP PACK/UNPACK quantization
        m0  = t0 + t1;
        m1  = t0 - t1;
        nw0 = zm_sat16(scsp_pack(zm_ema(damp_a, inL + h_fb * m0)));
        nw1 = zm_sat16(scsp_pack(zm_ema(damp_a, inR + h_fb * m1)));

        rawL = 0.62 * t0 - 0.52 * t1;
        outL = rawL - zm_ema(0.005, rawL);
    };

scsp_fdn_r_step(w2, w3, inR, inL) = nw2, nw3, outR
    with {
        t2 = de.fdelay1(4096, dF1 + lfo2, w2);
        t3 = de.fdelay1(4096, dF3 - lfo2, w3);

        m2  = t2 + t3;
        m3  = t2 - t3;
        nw2 = zm_sat16(scsp_pack(zm_ema(damp_a, inR + h_fb * m2)));
        nw3 = zm_sat16(scsp_pack(zm_ema(damp_a, inL + h_fb * m3)));

        rawR = 0.62 * t2 + 0.52 * t3;
        outR = rawR - zm_ema(0.005, rawR);
    };

scsp_tank_l(inL, inR) = (scsp_fdn_l_step(inL, inR) ~ (_, _)) : !, !, _;
scsp_tank_r(inR, inL) = (scsp_fdn_r_step(inR, inL) ~ (_, _)) : !, !, _;

// ---- 4. Full Stereo Signal Chain ----
process(xL, xR) =
    zm_soft_clip(xL * dry_g + wetL * wet_g * 1.15) * level_knob,
    zm_soft_clip(xR * dry_g + wetR * wet_g * 1.15) * level_knob
    with {
        // Pre-delay (4096 samples = up to 92 ms @ 44.1 kHz)
        preL = de.delay(4096, pre_samp, xL);
        preR = de.delay(4096, pre_samp, xR);

        // Input Schroeder all-pass diffusion (2 x 1024 samples)
        difL = fi.allpass_comb(1024, dApL, 0.0 - ap_gain, preL * 0.58);
        difR = fi.allpass_comb(1024, dApR, 0.0 - ap_gain, preR * 0.58);

        wetL = scsp_tank_l(difL, difR);
        wetR = scsp_tank_r(difR, difL);
    };
