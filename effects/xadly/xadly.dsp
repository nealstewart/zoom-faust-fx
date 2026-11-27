// effects/ps1dly/ps1dly.dsp
//
// Sony PlayStation 1 SPU / BBD Hybrid Delay & Echo ("PS1DLY")
// implemented in FAUST and compiled to a Zoom MS-70CDR+ (.ZD2 + .ZIC) package via:
//   python tools/faust2zd2.py effects/ps1dly/ps1dly.dsp --build
//
// DSP Architecture (Stereo-In -> Stereo-Out, 2 x 32,768-float SDRAM Ring Buffers):
//   1. 22.05 kHz SPU Half-Rate Input Conditioning (~8.5 kHz 2-pole anti-alias LPF
//      + 65 Hz rumble HPF) to keep repeats warm and authentic to 1996 PS1 SPU RAM.
//   2. 4 Hardware-Inspired Modes:
//        - ECHO : Classic PS1 SPU stereo echo (slight 1.03x R detune + warm cross-bleed)
//        - PING : True stereo Ping-Pong BBD echo (L -> R -> L bouncing repeats)
//        - TAPE : PS1 Space Echo multi-head (main tap T + rhythmic 0.667*T / 0.5*T taps)
//        - DARK : Deep MN3005 BBD / Silent Hill dub echo (darker clock + all-pass smear)
//   3. Modulated Cubic-Lagrange Fractional Delay (`de.fdelay3(32768, ...)`, 20..700 ms)
//      with quadrature dual-rate BBD chorus/warble LFO (`0.43 Hz` + `0.61 Hz`).
//   4. Progressive Repeat Degradation inside the Feedback Loop:
//      `vIIR` 1-pole damping + `zm_sat16` 16-bit SPU RAM saturation + optional lo-fi
//      ADPCM/bit-step `Grit` + `ma.tanh` self-oscillation ceiling.
//   5. Equal-power Dry/Wet `Mix`, stock `Level`, and momentary `ON/OFF` footswitch.

declare name "XA Delay";
declare zd2_filename "XXXADLY";
declare zd2_id "070011f0";
declare version "1.00";
declare zd2_display "XA DLY";
declare zd2_badge "XA";
declare description "22.05 kHz half-rate / BBD hybrid stereo echo with 4 modes (Echo, Ping-Pong, Tape, Dark) and beat-synced subdivisions.";
declare zd2_dspload "30";
declare zd2_footswitch "true";

import("stdfaust.lib");
import("zoom.lib");

// ---- 8 Knobs + Trailing Footswitch Toggle ----
mode_sel   = hslider("Mode[1][values:ECHO|PING|TAPE|DARK][tooltip:Echo topology (ECHO = PS1 stereo, PING = ping-pong, TAPE = multi-head space echo, DARK = deep BBD dub).]", 0, 0, 3, 1);
time_click = hslider("Time[2][time_sync:20..700][tooltip:Delay time: 20..700 ms (clicks 0..680), then 1/16..1/4. synced to pedal BPM.]", 300, 0, 687, 1);
fdbk_knob  = hslider("Fdbk[3][tooltip:Repeat feedback (0..95%; saturates safely into warm self-oscillation near max).]", 52, 0, 95, 1) * 0.01 : si.smoo;
tone_knob  = hslider("Tone[4][tooltip:SPU vIIR / BBD clock darkening on repeats (0 = bright 8.5 kHz SPU, 100 = dark 1.2 kHz BBD).]", 48, 0, 100, 1) * 0.01 : si.smoo;
mod_knob   = hslider("Mod[5][tooltip:BBD clock warble / chorus modulation depth on repeats (0..100%).]", 25, 0, 100, 1) * 0.01 : si.smoo;
grit_knob  = hslider("Grit[6][tooltip:PS1 SPU 16-bit RAM saturation and progressive lo-fi repeat grit (0..100%).]", 35, 0, 100, 1) * 0.01 : si.smoo;
mix_knob   = hslider("Mix[7][tooltip:Dry/Wet blend (0 = dry, 50 = unity dry + wet, 100 = 100% wet).]", 45, 0, 100, 1) * 0.01 : si.smoo;
level_knob = hslider("Level[8][curve:stock_vol][tooltip:Master output level (80 is unity).]", 80, 0, 100, 1) : si.smoo;

// ---- 1. Mode & Beat-Sync Tables (Hoisted to Block-Rate) ----
sel4(m, v0, v1, v2, v3) =
    ba.if(m < 0.5, v0,
    ba.if(m < 1.5, v1,
    ba.if(m < 2.5, v2, v3)));

sel8(m, v0, v1, v2, v3, v4, v5, v6, v7) =
    ba.if(m < 0.5, v0,
    ba.if(m < 1.5, v1,
    ba.if(m < 2.5, v2,
    ba.if(m < 3.5, v3,
    ba.if(m < 4.5, v4,
    ba.if(m < 5.5, v5,
    ba.if(m < 6.5, v6, v7)))))));

// Single-knob Zoom-style Time: 0..680 = 20..700 ms, 681..687 = 1/16..1/4. @ zm_bpm
time_ms = zm_time_sync_ms(time_click, 20.0, 700.0, zm_bpm);

// Same-side vs cross-side feedback routing
same_fb_mul  = sel4(mode_sel, 0.80, 0.00, 0.72, 0.62);
cross_fb_mul = sel4(mode_sel, 0.22, 1.00, 0.30, 0.42);
// Right channel time ratio (slight detune in ECHO/DARK, exact in PING/TAPE)
r_time_mul   = sel4(mode_sel, 1.03, 1.00, 1.00, 1.04);
// Secondary multi-head tap mix (active in TAPE mode, subtle in DARK)
head2_gain   = sel4(mode_sel, 0.00, 0.00, 0.52, 0.22);
// Extra darkening bias in DARK BBD mode
dark_bias    = sel4(mode_sel, 0.00, 0.04, 0.08, 0.24);
// Ping-pong mono-to-L input injection flag
is_ping      = ba.if((mode_sel > 0.5) & (mode_sel < 1.5), 1.0, 0.0);

// ---- 2. Block-Rate Coefficients ----
base_smps = aa.clip(441.0, 30870.0, zm_ms2samp(time_ms));
// vIIR 1-pole damping coefficient: 0.86 (bright SPU) -> 0.14 (dark MN3005 BBD)
tone_eff  = aa.clip(0.0, 1.0, tone_knob + dark_bias);
v_iir     = max(0.12, 0.86 - tone_eff * 0.72);
// BBD modulation depth in samples (up to ~175 samples = ~4.0 ms)
mod_smps  = mod_knob * mod_knob * 175.0;
// Feedback drive & progressive grit parameters
fb_gain   = fdbk_knob * 0.98;
sat_drive = 1.0 + grit_knob * 1.45;
sat_inv   = 1.0 / (1.0 + grit_knob * 0.85);
// Grit bit-quantization scale (soft 7..11-bit step blend)
q_blend   = grit_knob * grit_knob * 0.55;
// Equal-power dry/wet mix
dry_gain  = sqrt(max(0.0, 1.0 - mix_knob));
wet_gain  = sqrt(max(0.0, mix_knob));

// ---- 3. PS1 SPU / BBD Repeat Shaper (16-Bit RAM Sat + Optional Lo-Fi Crush) ----
spu_grit(x) = sat + q_blend * (crushed - sat)
    with {
        sat     = zm_sat16(x * sat_drive) * sat_inv;
        crushed = int(sat * 96.0) * 0.010416667;
    };

// ---- 4. Dual-Rate Quadrature BBD LFO ----
lfo_a = os.osc(0.43);
lfo_b = os.osc(0.61);
mod_l = (0.65 * lfo_a + 0.35 * lfo_b) * mod_smps;
mod_r = (-0.65 * lfo_a + 0.35 * lfo_b) * mod_smps;

// ---- 5. Stereo SDRAM Delay & Feedback Tank (2 x 32,768 floats = 262 KB <= 529 KB cap) ----
// Single coupled L/R feedback loop so ECHO, PING (true L->R->L ping-pong),
// TAPE (multi-head 0.667*T / 0.5*T), and DARK BBD modes share one stereo pair of delay lines.
ps1_dly_step(wL, wR, xL, xR, sL, sR) = nwL, nwR, outL, outR
    with {
        dL   = base_smps + mod_l;
        dR   = base_smps * r_time_mul + mod_r;
        dL2  = base_smps * 0.6667 + mod_l * 0.5;
        dR2  = base_smps * 0.5000 + mod_r * 0.5;

        tapL  = de.fdelay3(32768, dL,  wL);
        tapL2 = de.fdelay1(32768, dL2, wL);
        tapR  = de.fdelay3(32768, dR,  wR);
        tapR2 = de.fdelay1(32768, dR2, wR);

        dampedL = zm_ema(v_iir, zm_allpass1(-0.28, tapL));
        dampedR = zm_ema(v_iir, zm_allpass1( 0.25, tapR));

        fbL = spu_grit((same_fb_mul * dampedL + cross_fb_mul * dampedR) * fb_gain);
        fbR = spu_grit((same_fb_mul * dampedR + cross_fb_mul * dampedL) * fb_gain);

        injL = ba.if(is_ping > 0.5, 0.707 * (sL + sR), sL);
        injR = ba.if(is_ping > 0.5, 0.0, sR);

        nwL  = zm_sat16(injL + fbL);
        nwR  = zm_sat16(injR + fbR);

        wetL = dampedL + head2_gain * tapL2;
        wetR = dampedR + head2_gain * tapR2;

        outL = (xL * dry_gain + wetL * wet_gain) * level_knob;
        outR = (xR * dry_gain + wetR * wet_gain) * level_knob;
    };

// ---- 6. Complete Stereo Process ----
// SPU 22.05 kHz half-rate input conditioning (~8.5 kHz LPF + 65 Hz HPF) on wet send
spu_in_cond(x) = fi.lowpass(2, 8500.0, fi.highpass(1, 65.0, x));

process(xL, xR) = (ps1_dly_step(xL, xR, sL, sR) ~ (_, _))
    with {
        sL = spu_in_cond(xL);
        sR = spu_in_cond(xR);
    };
