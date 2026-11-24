// effects/snesfir/snesfir.dsp
//
// Super Nintendo (SNES) Sony S-DSP 32 kHz 8-Tap FIR Echo
// compiled to a Zoom MS-70CDR+ (.ZD2) package via:
//   python tools/faust2zd2.py effects/snesfir/snesfir.dsp --build
//
// Hardware Architecture Modeled:
//   1. True 32 kHz Sony S-DSP echo sample rate (fractional phase-accumulator
//      ZOH stepping at 32,000 / 44,100 = 0.72562358).
//   2. Stereo external SDRAM echo ring buffers (up to 480 ms, covering the
//      authentic 16..240 ms SNES EDL range plus extended echo times).
//   3. 8-tap signed 1.7 fixed-point FIR filter (`c_k / 128.0`, oldest-to-newest)
//      inside the echo feedback loop, reading 8 consecutive 32 kHz-spaced taps
//      (`1.378125` samples apart at 44.1 kHz) from the shared SDRAM ring buffer.
//   4. 4 authentic SNES FIR presets:
//        RAW    : [127,   0,   0,   0,   0,   0,   0,   0]
//        CHRONO : [ 12,  33,  43,  43,  19,  -2, -13,  -7]
//        DKC    : [ 88, -65, -37, -16,  -2,   7,  12,  12]
//        CAVE   : [ 16,  16,  16,  32,  32,  16,  16,  16] (stability-normalized)
//   5. Selectable 16-bit fixed-point saturation vs. classic SNES S-DSP signed
//      16-bit integer overflow wraparound (`SatWrap`).

declare name "FIR Echo";
declare zd2_filename "XXFIRECH";
declare zd2_id "07001250";
declare version "1.00";
declare zd2_display "FIR ECHO";
declare zd2_badge "FE";
declare description "32 kHz stereo echo with 8-tap signed 1.7 fixed-point FIR filter (RAW, WARM, METAL, CAVE), beat-sync, and 16-bit sat/wrap.";
declare zd2_dspload "22";
declare zd2_footswitch "true";

import("stdfaust.lib");
import("zoom.lib");

// ---- 7 Knobs + Trailing Footswitch Toggle ----
fir_sel    = hslider("FIR[1][values:RAW|WARM|METAL|CAVE][tooltip:SNES S-DSP 8-tap signed 1.7 FIR preset (RAW, WARM, METAL, CAVE).]", 1, 0, 3, 1);
sync_sel   = hslider("Sync[2][values:FREE|1/16|1/8T|1/8|1/4T|1/8.|1/4|1/4.][tooltip:Beat-synced note subdivision (FREE = Time in 20..700 ms; 1/16..1/4. = beat-synced with Time as 60..200 BPM).]", 0, 0, 7, 1);
time_knob  = hslider("Time[3][tooltip:Echo time (20..700 ms when Sync=FREE) or Tempo (60..200 BPM when beat-synced; 43 = 120 BPM).]", 43, 0, 100, 1) * 0.01 : si.smoo;
fdbk_knob  = hslider("Fdbk[4][tooltip:S-DSP EFB echo feedback (0..95).]", 62, 0, 95, 1) * 0.01 : si.smoo;
wrap_knob  = hslider("SatWrap[5][tooltip:0 = clean 16-bit saturation, 100 = classic SNES S-DSP signed 16-bit overflow wraparound.]", 30, 0, 100, 1) * 0.01 : si.smoo;
mix_knob   = hslider("Mix[6][tooltip:Dry/Wet echo blend (EVOL, 0..100).]", 45, 0, 100, 1) * 0.01 : si.smoo;
level_knob = hslider("Level[7][curve:stock_vol][tooltip:Master output level (80 is unity gain).]", 80, 0, 100, 1) : si.smoo;

// ---- 1. SNES S-DSP 8-Tap Signed 1.7 FIR & Beat-Sync Tables (Hoisted to Block-Rate) ----
// Coefficients are c_k / 128.0 ordered oldest (c0) to newest (c7).
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

// Beat-sync multiplier (beats per repeat): FREE, 1/16, 1/8T, 1/8, 1/4T, 1/8., 1/4, 1/4.
sync_beats = sel8(sync_sel, 0.0, 0.25, 0.3333333, 0.5, 0.6666667, 0.75, 1.0, 1.5);
free_ms    = 20.0 + time_knob * 680.0;
sync_bpm   = 60.0 + time_knob * 140.0;
sync_ms    = min(700.0, (60000.0 / sync_bpm) * sync_beats);
time_ms    = ba.if(sync_sel < 0.5, free_ms, sync_ms);

// Stability normalization keeps peak passband gain <= 0.98 so EFB stays safe up to 95%:
//   RAW: 1.00 | CHRONO: 0.94 | DKC: 0.62 | CAVE: 0.78 (sum=160/128=1.25 -> 0.975)
fir_norm = sel4(fir_sel, 1.00, 0.94, 0.62, 0.78) * 0.0078125;  // * (1 / 128.0)

c0 = sel4(fir_sel, 127.0,  12.0,  88.0, 16.0) * fir_norm;
c1 = sel4(fir_sel,   0.0,  33.0, -65.0, 16.0) * fir_norm;
c2 = sel4(fir_sel,   0.0,  43.0, -37.0, 16.0) * fir_norm;
c3 = sel4(fir_sel,   0.0,  43.0, -16.0, 32.0) * fir_norm;
c4 = sel4(fir_sel,   0.0,  19.0,  -2.0, 32.0) * fir_norm;
c5 = sel4(fir_sel,   0.0,  -2.0,   7.0, 16.0) * fir_norm;
c6 = sel4(fir_sel,   0.0, -13.0,  12.0, 16.0) * fir_norm;
c7 = sel4(fir_sel,   0.0,  -7.0,  12.0, 16.0) * fir_norm;

// ---- 2. Block-Rate S-DSP Parameters ----
// Delay in 44.1 kHz samples (room for 7 extra 32 kHz taps = ~9.65 samples)
d_base   = aa.clip(32.0, 32600.0, ba.ms2samp(time_ms));
// Slight stereo offset (~2.2 ms on right) for authentic SNES stereo echo spread
d_l      = d_base;
d_r      = aa.clip(32.0, 32600.0, d_base + 96.0);
// Spacing of one 32 kHz S-DSP sample in 44.1 kHz frames: 44100 / 32000 = 1.378125
t32      = 1.378125;

// EFB feedback gain (capped safely at 0.94)
efb      = min(0.94, fdbk_knob);
dry_g    = sqrt(max(0.0, 1.0 - mix_knob));
wet_g    = sqrt(max(0.0, mix_knob));

// Wraparound threshold: at higher SatWrap, lower wrap rail slightly so normal
// pedal signals (~0.25-0.6 peak) can kiss classic S-DSP 16-bit integer wraparound
wrap_thr = max(0.32, 0.98 - wrap_knob * 0.62);
wrap_inv = 1.0 / (2.0 * wrap_thr);

// ---- 3. 32 kHz S-DSP ZOH Clock & 16-Bit Saturation / Wraparound ----
snes_32k_step(ph, hold, x_in) = next_ph, next_hold, next_hold
    with {
        ph_sum    = ph + 0.72562358;  // 32,000 / 44,100 Hz
        tk        = ba.if(ph_sum >= 1.0, 1.0, 0.0);
        next_ph   = ph_sum - tk;
        next_hold = ba.if(tk > 0.5, x_in, hold);
    };

snes_32k_sh(x) = (snes_32k_step(x) ~ (_, _)) : !, !, _;

// Classic S-DSP signed 16-bit wraparound vs. smooth 16-bit saturation
snes_sat_wrap(x) = ba.if(abs(x) < 1.0e-6, 0.0,
    sat_x * (1.0 - wrap_knob) + wrap_x * wrap_knob)
    with {
        sat_x  = zm_sat16(x);
        // Signed two's-complement-style modular wrap into [-wrap_thr, +wrap_thr)
        wrap_x = aa.clip(-1.0, 1.0, x - (2.0 * wrap_thr) * floor((x + wrap_thr) * wrap_inv));
    };

// ---- 4. Per-Channel 8-Tap Signed 1.7 FIR Echo Loop in External SDRAM ----
// All 8 taps share ONE 32,768-sample SDRAM ring buffer per channel (256 KB total).
snes_echo_step(w, d_smps, xin) = nw, wet_out
    with {
        // 8 consecutive 32 kHz-spaced taps, oldest (s0 -> c0) to newest (s7 -> c7)
        s0 = de.fdelay1(32768, d_smps + 7.0 * t32, w);
        s1 = de.fdelay1(32768, d_smps + 6.0 * t32, w);
        s2 = de.fdelay1(32768, d_smps + 5.0 * t32, w);
        s3 = de.fdelay1(32768, d_smps + 4.0 * t32, w);
        s4 = de.fdelay1(32768, d_smps + 3.0 * t32, w);
        s5 = de.fdelay1(32768, d_smps + 2.0 * t32, w);
        s6 = de.fdelay1(32768, d_smps + 1.0 * t32, w);
        s7 = de.fdelay1(32768, d_smps,             w);

        // 8-tap S-DSP FIR accumulator + 16-bit sat/wrap
        fir_sum = c0*s0 + c1*s1 + c2*s2 + c3*s3 + c4*s4 + c5*s5 + c6*s6 + c7*s7;
        fir_out = snes_sat_wrap(fir_sum);

        // 32 kHz S-DSP sample-and-hold + gentle DC removal
        zoh_out = snes_32k_sh(fir_out);
        wet_out = zoh_out - zm_ema(0.004, zoh_out);

        // Write input + EFB * FIR output back into echo RAM
        nw      = snes_sat_wrap(xin + efb * wet_out);
    };

snes_echo_chan(d_smps, x_in) = (snes_echo_step(d_smps, x_in) ~ _) : !, _;

// ---- 5. Full Stereo Process ----
process(xL, xR) =
    (xL * dry_g + wetL * wet_g) * level_knob,
    (xR * dry_g + wetR * wet_g) * level_knob
    with {
        wetL = snes_echo_chan(d_l, xL);
        wetR = snes_echo_chan(d_r, xR);
    };
