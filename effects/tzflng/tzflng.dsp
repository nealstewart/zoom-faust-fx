// effects/faustflg/faustflg.dsp
//
// Eventide-Style Vocal Flanger & Post-Punk / Industrial Comb-Filter (Killing Joke
// style) written in FAUST and compiled to a Zoom MS-70CDR+ (.ZD2 + .ZIC) package:
//   python tools/faust2zd2.py effects/faustflg/faustflg.dsp --build
//
// DSP Architecture (Mono-In -> Wide Stereo-Out, Designed to Sit After RAT Drive):
//   1. Dry Alignment Delay (Through-Zero Support):
//      Fixed ~2.5 ms delay on the dry path (120 samples at 48 kHz) via
//      `de.fdelay(1024, 120.0, x_in)` so the modulated wet tap can sweep across
//      and cross through zero relative to dry for authentic tape-flange phase
//      cancellation without nasal comb ringing.
//   2. Wet-Only Bandpass Filter:
//      1-pole HPF (~250 Hz) + 1-pole LPF (~4.5 kHz) applied strictly to the wet
//      input path before the delay line. Keeps the dry vocal fundamental solid
//      and prevents upstream RAT distortion harmonics from turning into harsh
//      metallic fizz.
//   3. Modulated Delay Line & Eventide "Wide" Stereo Output:
//      Power-of-two ring buffer with cubic Hermite (Catmull-Rom) fractional
//      interpolation across a 0.5 ms to 8.0 ms (24..384 samples) modulated range.
//      In Eventide "Wide" mode (`Wide = WIDE`), Left taps the full modulated
//      delay (`T_mod`) while Right taps the midpoint (`0.5 * T_mod`) with
//      inverted wet polarity (`dry + depth*wetL` on L, `dry - depth*wetR` on R)
//      so comb notches interlace across the stereo field instead of ringing in
//      the center.
//   4. Anti-Tonal Dispersed Feedback Loop:
//      Bipolar feedback (-65% to +65%; negative for hollow industrial comb,
//      positive for sharper metallic bite). Inside the feedback loop sit TWO
//      fixed 1st-order all-pass phase dispersers (`fi.tf1(g, 1.0, g)`) plus a
//      1-pole 4 kHz damping LPF and `ma.tanh` soft clipper so comb peaks are
//      dispersed out of integer harmonic alignment (lush/industrial rather than
//      a tuned metal pipe).
//   5. Non-Periodic Modulator (Zero Obvious LFO):
//      Combines a vocal envelope follower (`an.amp_follower_ud` with ~8 ms
//      attack, ~200 ms release) and a non-periodic dual-incommensurate drift
//      engine (`no.lfnoise` Brownian walk + coupled `0.11 Hz` and `0.29 Hz`
//      oscillators), mapped through an exponential (`m * m`) octave curve.
//      Setting `Env` = 0 and `Drift` = 0 freezes the delay into a static
//      tuned metallic comb filter.

declare name "TZ Flanger";
declare zd2_filename "XXTZFLNG";
declare zd2_id "07001190";
declare version "1.00";
declare zd2_display "TZ FLNG";
declare zd2_badge "TZ";
declare description "Through-zero stereo vocal & tape flanger with external SDRAM delay lines and all-pass feedback dispersion.";
declare zd2_dspload "29";

import("stdfaust.lib");
import("zoom.lib");

// ---- 6 Pedal Knobs (Mapped to Zoom MS-70CDR+ Parameter Pages) ----
manual_ms   = hslider("ManMs[1][unit:ms][tooltip:Base delay / static comb tuning (0.5..8.0 ms; 2.5 ms is through-zero center).]", 2.5, 0.5, 8.0, 0.1) : si.smoo;
env_depth   = hslider("Env[2][tooltip:Vocal envelope modulation depth and direction (-100..+100%).]", 35, -100, 100, 1) * 0.01 : si.smoo;
drift_depth = hslider("Drift[3][tooltip:Non-periodic micro-drift modulation depth (0..100%; 0 for frozen static comb).]", 30, 0, 100, 1) * 0.01 : si.smoo;
feedback    = hslider("Fdbk[4][tooltip:Bipolar all-pass dispersed feedback (-65..+65%; neg = hollow industrial, pos = metallic bite).]", -45, -65, 65, 1) * 0.01 : si.smoo;
depth_mix   = hslider("Depth[5][tooltip:Bipolar wet blend (-100..+100%; neg = through-zero phase cancel, pos = additive flange).]", -75, -100, 100, 1) * 0.01 : si.smoo;
stereo_mode = hslider("Wide[6][values:MONO|WIDE][tooltip:Stereo mode (MONO = dual T_mod, WIDE = Eventide T_mod Left / inverted 0.5*T_mod Right).]", 1, 0, 1, 1);

// ---- Block-Rate Control Scaling ----
// Normalize manual_ms (0.5..8.0 ms) to unipolar [0..1] on a square-root scale
// so squaring the combined modulator (`m * m`) yields perceptually uniform
// octave sweeps across the entire 0.5..8.0 ms (24..384 sample) range.
man_norm  = (manual_ms - 0.5) * 0.13333333;
man_root  = man_norm * (0.55 + 0.45 * man_norm);
r_pol     = ba.if(stereo_mode > 0.5, -1.0, 1.0);
r_tap_mul = ba.if(stereo_mode > 0.5,  0.5, 1.0);
out_trim  = 1.0 / (1.0 + 0.25 * abs(depth_mix) + 0.28 * abs(depth_mix) * abs(feedback));

// ---- 1. Non-Periodic Modulator (Vocal Envelope + Incommensurate Micro-Drift) ----
// Envelope follower: ~8 ms attack (k=0.00260), ~200 ms release (k=0.000104)
// Micro-drift: 1/f Brownian random walk (`no.lfnoise`) blended with two
// non-harmonic slow oscillators (0.11 Hz + 0.29 Hz) so the ear never locks
// onto a periodic guitar-pedal LFO cycle.
non_periodic_drift = 0.50 * no.lfnoise(0.0035) + 0.30 * zm_osc(0.11) + 0.20 * zm_osc(0.29);

mod_delay_smps(x) = 24.0 + 360.0 * (m_clamped * m_clamped)
    with {
        vox_env   = an.amp_follower_ud(0.00260, 0.000104, x);
        env_push  = min(1.0, vox_env * 2.8) * env_depth;
        drift_val = non_periodic_drift * drift_depth * 0.45;
        m_clamped = aa.clip(0.0, 1.0, man_root + env_push + drift_val);
    };

// ---- 2. Wet-Only Bandpass Filter (250 Hz HPF -> 4.5 kHz LPF) ----
wet_bandpass(x) = fi.lowpass(1, 4500.0, fi.highpass(1, 250.0, x));

// ---- 3. Anti-Tonal Dispersed Feedback Loop + Modulated Delay Line ----
// Recursive feedback loop (`flanger_tank_step ~ _`):
//   - Soft-clips and damps the feedback return through a 4 kHz LPF and TWO
//     cascaded 1st-order all-pass filters (`fi.tf1(g, 1.0, g)` with `g1 = -0.42`,
//     `g2 = 0.37`) to push the comb peaks out of integer harmonic alignment.
//   - Returns `tank_in = wet_in + fb_sig` as the single state wire fed into
//     `de.fdelay(1024, t_mod, tank_in)` (Left + feedback) and
//     `de.fdelay(1024, t_mod * r_tap_mul, tank_in)` (Right midpoint tap).
//     Because both `de.fdelay` calls read from the same signal `tank_in`, they
//     automatically share a single 1024-sample SDRAM ring buffer (`dline_1`)!
flanger_tank_step(prev_tank_in, wet_in, t_mod) = wet_in + fb_sig
    with {
        fb_prev   = de.fdelay3(1024, t_mod, prev_tank_in);
        fb_damped = fi.lowpass(1, 4000.0, fb_prev);
        fb_ap1    = zm_allpass1(-0.42, fb_damped);
        fb_ap2    = zm_allpass1(0.37, fb_ap1);
        fb_sig    = ma.tanh(fb_ap2 * feedback);
    };
flanger_tank_sig(wet_in, t_mod) = (flanger_tank_step(wet_in, t_mod) ~ _);

// ---- 4. Complete Mono-In / Wide Stereo-Out Process ----
// Dry path is delayed by a fixed 120 samples (2.5 ms @ 48 kHz) in delay line 0
// (`de.delay(1024, 120.0, xL)`) so `T_mod` sweeps cleanly through zero at 2.5 ms.
process(xL, xR) = (dry_aligned + depth_mix * wet_l) * out_trim,
                  (dry_aligned + depth_mix * r_pol * wet_r) * out_trim
    with {
        dry_aligned = de.delay(1024, 120.0, xL);
        t_mod       = mod_delay_smps(xL);
        wet_in      = wet_bandpass(xL);
        tank_sig    = flanger_tank_sig(wet_in, t_mod);
        wet_l       = de.fdelay3(1024, t_mod, tank_sig);
        wet_r       = de.fdelay3(1024, t_mod * r_tap_mul, tank_sig);
    };
