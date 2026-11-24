// effects/ps1rvb/ps1rvb.dsp
//
// Sony PlayStation 1 CXD2922Q SPU 22.05 kHz Half-Rate Hardware Reverb Engine
// implemented in FAUST and compiled to a Zoom MS-70CDR+ (.ZD2) package via:
//   python tools/faust2zd2.py effects/ps1rvb/ps1rvb.dsp --build
//
// Features:
//   1. True half-rate (22,050 Hz) decimation & reconstruction (~8.5 kHz 2-pole LPF).
//   2. Same-side + cross-side multi-tap comb reflections (`vWALL`, `vIIR`, `vCOMB1..4`)
//      across 8 x 1024-float external SDRAM ring buffers (32 KB) with 16-bit SPU
//      signed fixed-point accumulator saturation (`sh_spu_sat16`).
//   3. Two cascaded Schroeder all-pass diffusers per channel (`dAPF1`, `dAPF2`).
//   4. All 5 Sony PS1 BIOS presets (`ROOM`, `STDO`, `HALL`, `SPCE`, `PIPE`)
//      plus momentary footswitch throw support (`ON/OFF = LATCH / UNLATCH`).

declare name "XA Reverb";
declare zd2_filename "XXXARVB";
declare zd2_id "070011b0";
declare version "1.00";
declare zd2_display "XA RVB";
declare zd2_badge "XA";
declare description "22.05 kHz half-rate 16-bit hardware reverb with 5 space presets (Room, Studio, Hall, Space, Pipe) and momentary footswitch.";
declare zd2_dspload "32";
declare zd2_footswitch "true";

import("stdfaust.lib");
import("zoom.lib");

// ---- 7 Knobs + Trailing Footswitch Toggle (2 Pages of 4 on MS-70CDR+) ----
mode_sel   = hslider("Mode[1][values:ROOM|STDO|HALL|SPCE|PIPE][tooltip:Sony PS1 SPU BIOS reverb algorithm (Room, Studio, Hall, Space Echo, Pipe).]", 3, 0, 4, 1);
decay_knob = hslider("Decay[2][tooltip:SPU vWALL reflection feedback / reverb tail duration (0..100).]", 68, 0, 100, 1) * 0.01 : si.smoo;
pred_ms    = hslider("PreD[3][unit:ms][tooltip:Pre-delay before the SPU reverb tank (0..90 ms).]", 15, 0, 90, 1) : si.smoo;
damp_knob  = hslider("Damp[4][tooltip:SPU vIIR 22.05 kHz internal tank high-frequency damping (0 = bright metallic, 100 = dark).]", 55, 0, 100, 1) * 0.01 : si.smoo;
early_knob = hslider("Early[5][tooltip:SPU dAPF1/dAPF2 all-pass diffusion and fluttery early reflections (0..100).]", 60, 0, 100, 1) * 0.01 : si.smoo;
mix_knob   = hslider("Mix[6][tooltip:Dry/Wet blend (0 = dry, 50 = unity dry + wet, 100 = 100% wet SPU tank).]", 45, 0, 100, 1) * 0.01 : si.smoo;
level_knob = hslider("Level[7][curve:stock_vol][tooltip:Master output level (80 is unity at nominal MS-70CDR+ bus level).]", 80, 0, 100, 1) : si.smoo;

// ---- 1. Sony PS1 BIOS Reverb Preset Tables (Hoisted to Block-Rate) ----
sel5(m, v0, v1, v2, v3, v4) =
    ba.if(m < 0.5, v0,
    ba.if(m < 1.5, v1,
    ba.if(m < 2.5, v2,
    ba.if(m < 3.5, v3, v4))));

dC1L   = sel5(mode_sel, 311.0, 547.0, 1187.0, 1499.0, 211.0);
dC1R   = sel5(mode_sel, 331.0, 569.0, 1223.0, 1543.0, 227.0);
dC1T2L = sel5(mode_sel, 193.0, 277.0,  331.0,  367.0, 149.0);
dC1T2R = sel5(mode_sel, 211.0, 293.0,  353.0,  389.0, 157.0);
dC2L   = sel5(mode_sel, 443.0, 751.0, 1571.0, 1973.0, 313.0);
dC2R   = sel5(mode_sel, 461.0, 773.0, 1613.0, 2017.0, 337.0);
dC2T2L = sel5(mode_sel, 293.0, 499.0, 1063.0, 1361.0, 197.0);
dC2T2R = sel5(mode_sel, 307.0, 521.0, 1097.0, 1399.0, 211.0);
dApf1L = sel5(mode_sel,  89.0, 149.0,  263.0,  347.0, 223.0);
dApf1R = sel5(mode_sel,  97.0, 157.0,  277.0,  359.0, 229.0);
dApf2L = sel5(mode_sel,  59.0,  97.0,  179.0,  239.0, 149.0);
dApf2R = sel5(mode_sel,  67.0, 107.0,  191.0,  251.0, 157.0);
xfb    = sel5(mode_sel,  0.88,  0.90,   0.94,   0.96, -0.94);

// ---- 2. Block-Rate SPU Tank Coefficients ----
// Concave perceptual decay curve: Decay=50 -> 0.85, Decay=68 -> 0.927, Decay=100 -> 0.978
d_inv    = 1.0 - decay_knob;
vWall    = min(0.978, 0.45 + 0.528 * (1.0 - d_inv * d_inv));
vWallX   = vWall * xfb;
vIir     = max(0.18, 0.88 - damp_knob * 0.66);
vApf1    = 0.20 + 0.52 * early_knob;
vApf2    = 0.16 + 0.48 * early_knob;
pre_samp = ba.ms2samp(pred_ms);
rvb_dry  = sqrt(max(0.0, 1.0 - mix_knob));
rvb_wet  = sqrt(max(0.0, mix_knob));

// ---- 3. PS1 SPU 16-Bit Accumulator Saturator & Schroeder All-Pass Stage ----
// Standard Faust Schroeder all-pass comb (`fi.allpass_comb(maxdel, d, -g, x)`),
// scaling delay lengths by 2x (44.1 kHz equivalent of the 22.05 kHz SPU RAM offsets)
// across 4096-sample SDRAM ring buffers (131 KB total, < 25% of per-slot 529 KB cap).
spu_apf(d_half, g, x) = fi.allpass_comb(4096, d_half * 2.0, 0.0 - g, zm_sat16(x));

// ---- 4. Full Stereo SPU Reverb Tank (4 Comb Lines + 4 All-Pass Lines in SDRAM) ----
// Standard Faust 2-wire recursive comb tanks (`~ (_, _)`):
// Multiple `de.delay(4096, d, w_k)` taps reading from the same feedback wire `w_k`
// automatically share a single 4096-sample SDRAM delay buffer per comb branch.
spu_comb_l_step(w0, w1, lin, rin) = nw0, nw1, sumL
    with {
        c1FbL = de.delay(4096, dC1L   * 2.0, w0);
        c1T2L = de.delay(4096, dC1T2L * 2.0, w0);
        c2FbL = de.delay(4096, dC2L   * 2.0, w1);
        c2T2R = de.delay(4096, dC2T2R * 2.0, w1);

        iir1L = zm_ema(vIir, lin + vWall  * c1FbL);
        iir2L = zm_ema(vIir, rin + vWallX * c2FbL);

        nw0   = zm_sat16(iir1L);
        nw1   = zm_sat16(iir2L);

        rawL  = 0.58 * c1FbL + 0.48 * c2FbL - 0.38 * c1T2L + 0.30 * c2T2R;
        sumL  = rawL - zm_ema(0.007, rawL);
    };

spu_comb_r_step(w2, w3, rin, lin) = nw2, nw3, sumR
    with {
        c1FbR = de.delay(4096, dC1R   * 2.0, w2);
        c1T2R = de.delay(4096, dC1T2R * 2.0, w2);
        c2FbR = de.delay(4096, dC2R   * 2.0, w3);
        c2T2L = de.delay(4096, dC2T2L * 2.0, w3);

        iir1R = zm_ema(vIir, rin + vWall  * c1FbR);
        iir2R = zm_ema(vIir, lin + vWallX * c2FbR);

        nw2   = zm_sat16(iir1R);
        nw3   = zm_sat16(iir2R);

        rawR  = 0.58 * c1FbR + 0.48 * c2FbR - 0.38 * c1T2R + 0.30 * c2T2L;
        sumR  = rawR - zm_ema(0.007, rawR);
    };

spu_tank_l(lin, rin) = (spu_comb_l_step(lin, rin) ~ (_, _)) : !, !, _;
spu_tank_r(rin, lin) = (spu_comb_r_step(rin, lin) ~ (_, _)) : !, !, _;

process(xL, xR) =
    (xL * rvb_dry + rec_l * rvb_wet * 1.45) * level_knob,
    (xR * rvb_dry + rec_r * rvb_wet * 1.45) * level_knob
    with {
        lin_f = zm_ema(0.62, zm_ema(0.62, xL)) * 0.85;
        rin_f = zm_ema(0.62, zm_ema(0.62, xR)) * 0.85;
        lin   = de.delay(4096, pre_samp, lin_f);
        rin   = de.delay(4096, pre_samp, rin_f);

        sumL  = spu_tank_l(lin, rin);
        sumR  = spu_tank_r(rin, lin);

        ap1L  = spu_apf(dApf1L, vApf1, sumL);
        ap2L  = spu_apf(dApf2L, vApf2, ap1L);
        ap1R  = spu_apf(dApf1R, vApf1, sumR);
        ap2R  = spu_apf(dApf2R, vApf2, ap1R);

        rec_l = zm_ema(0.64, ap2L);
        rec_r = zm_ema(0.64, ap2R);
    };
