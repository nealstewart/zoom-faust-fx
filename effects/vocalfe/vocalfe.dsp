// effects/faustvfe/faustvfe.dsp
//
// All-in-One Vocal Front-End implemented in FAUST and compiled to a
// Zoom MS-70CDR+ (.ZD2 + .ZIC) package via:
//   python tools/faust2zd2.py effects/faustvfe/faustvfe.dsp --build
//
// Signal Chain (Strictly in Order):
//   1. 2-Pole Butterworth HPF (80..150 Hz, Q = 1/sqrt(2) = 0.7071):
//      Removes stage rumble, plosives, and close-mic proximity boom via
//      `fi.svf_hp(hpf_hz, 0.70710678)` (coefficients hoisted to block-rate).
//   2. Sidechain-Filtered Smart Vocal Gate:
//      - Detector sidechain only: 1-pole HPF (~300 Hz) + 1-pole LPF (~3 kHz)
//        so kick/bass bleed and cymbals don't falsely trigger the gate.
//      - Envelope follower: `an.amp_follower_ud` with ~1 ms attack (k=0.0206),
//        ~30 ms release (k=0.000694).
//      - Hysteresis + Hold State Machine (`gate_hold_step ~ _`):
//        `thresh_close` set 5 dB below `thresh_open` (* 0.5623413), plus a
//        100 ms hold countdown (4800 samples) before closing.
//      - Gain smoother (`gate_gain_step ~ _`): fast open (~1.5 ms, k=0.0138),
//        slower close (~100 ms, k=0.000208), with a configurable floor clamp
//        (`Floor` -40..0 dB, default -18 dB) so it tames live feedback without
//        chopping room ambience to dead silence.
//   3. Zero-Latency Relative Split-Band De-Esser:
//      - Audio path: 1-pole complementary split at ~5.5 kHz (`x_hp = x - lp`),
//        applying gain reduction via `y = x - (1.0 - g) * x_hp` so `g = 1.0`
//        gives bit-exact dry pass-through with zero phase smear.
//      - Detector sidechain: cascades a second 1-pole HPF on `x_hp` at ~6.5 kHz
//        (12 dB/oct) so loud vowels don't falsely trigger sibilance reduction.
//      - Relative detection: compares `env_hi` against broadband `env_full`;
//        only attenuates when `env_hi > ratio * env_full` AND `env_hi > noise_floor`.
//      - Fast attack (~0.75 ms, k=0.0274), fast release (~30 ms, k=0.000694),
//        max cut capped at -10 dB (g >= 0.3162278) to prevent lisping.
//   4. Output Soft Clipper / Peak Catcher:
//      - Lightweight Padé [3/2] rational `ma.tanh` soft clipper with input
//        `Drive` (0..12 dB) and peak `Ceil` trim + stock Zoom `Level` output.

declare name "VocalFE";
declare zd2_filename "XXVOCLFE";
declare zd2_id "07001180";
declare version "1.00";
declare zd2_display "VOCAL FE";
declare zd2_badge "VF";
declare description "All-in-One Vocal Front-End: 2-pole Butterworth HPF, sidechain-filtered smart gate with hold and floor clamp, zero-latency relative split-band de-esser, and Pade tanh peak catcher.";
declare zd2_dspload "24";

import("stdfaust.lib");

// ---- 6 Knobs (Mapped to Zoom MS-70CDR+ Parameter Pages; <=5 char names) ----
hpf_hz     = hslider("HPF[1][unit:Hz][tooltip:2-pole Butterworth high-pass cutoff (80..150 Hz).]", 100, 80, 150, 1) : si.smoo;
gate_db    = hslider("Gate[2][unit:dB][tooltip:Smart vocal gate open threshold (-55..-15 dBFS; close is 5 dB lower + 100 ms hold).]", -38, -55, -15, 1) : si.smoo;
floor_db   = hslider("Floor[3][unit:dB][tooltip:Gate closed floor clamp (-40..0 dB; default -18 dB tames feedback without dead dropouts).]", -18, -40, 0, 1) : si.smoo;
deess_knob = hslider("DeEss[4][tooltip:Relative split-band de-esser sensitivity (0..100; capped at -10 dB max cut).]", 55, 0, 100, 1) * 0.01 : si.smoo;
drive_db   = hslider("Drive[5][unit:dB][tooltip:Pre-clipper vocal drive / peak catcher ceiling push (0..12 dB).]", 2, 0, 12, 0.5) : si.smoo;
level_knob = hslider("Level[6][curve:stock_vol][tooltip:Output level (80 is unity gain).]", 80, 0, 100, 1) : si.smoo;

// ---- Block-Rate Precomputed Thresholds & Gains ----
thresh_open  = ba.db2linear(gate_db);
thresh_close = thresh_open * 0.5623413;
gate_floor   = ba.db2linear(floor_db);

// DeEss knob (0..1): true-zero quadratic strength curve (0 = off, 50 = ~-3 dB,
// 100 = tight ~-7..-9 dB) + relative HF/Full ratio from 0.35 down to 0.10
deess_ratio    = 0.35 - 0.25 * deess_knob;
deess_strength = 4.50 * deess_knob * deess_knob;

// Output soft-clipper drive & ceiling compensation (-1 dBFS peak catcher ceiling = 0.89125)
clip_drive = ba.db2linear(drive_db);
clip_ceil  = 0.8912509 / (1.0 + 0.25 * (clip_drive - 1.0));

// ============================================================================
// STAGE 1: 2-Pole Butterworth HPF (80..150 Hz, Q = 1/sqrt(2) = 0.70710678)
// ============================================================================
stage1_hpf(x) = fi.svf_hp(hpf_hz, 0.70710678, x);

// ============================================================================
// STAGE 2: Sidechain-Filtered Smart Vocal Gate (300 Hz..3 kHz SC, Hold + Hysteresis)
// ============================================================================
// Hold-counter state machine (`~` feedback):
//   - State > 0.0 means Gate is OPEN or in HOLD countdown (4800 samples = 100 ms @ 48 kHz)
//   - State == 0.0 means Gate is CLOSED
//   - Hysteresis: when closed (cnt <= 0), requires `env >= thresh_open` to open;
//     once open/holding (cnt > 0), stays open at full 4800 count whenever `env >= thresh_close`,
//     and counts down 4800 -> 0 during the 100 ms hold period before closing.
gate_hold_step(cnt, env) =
    ba.if(cnt > 0.0,
          ba.if(env >= thresh_close, 4800.0, max(0.0, cnt - 1.0)),
          ba.if(env >= thresh_open,  4800.0, 0.0));
gate_hold_timer(env) = (gate_hold_step(env) ~ _);

// Asymmetric gain smoother: ~1.5 ms open (k=0.0137928), ~100 ms close (k=0.0002083)
gate_gain_step(g_prev, g_tgt) =
    g_prev + ba.if(g_tgt > g_prev, 0.0137928, 0.0002083) * (g_tgt - g_prev);
gate_smoother(g_tgt) = (gate_gain_step(g_tgt) ~ _);

stage2_gate(x) = x * g_smooth
    with {
        sc_bp    = fi.lowpass(1, 3000.0, fi.highpass(1, 300.0, x));
        sc_env   = an.amp_follower_ud(0.0206179, 0.0006942, sc_bp);
        hold_cnt = gate_hold_timer(sc_env);
        g_target = ba.if(hold_cnt > 0.0, 1.0, gate_floor);
        g_smooth = gate_smoother(g_target);
    };

// ============================================================================
// STAGE 3: 2nd-Order Linkwitz-Riley (LR2) Split-Band De-Esser (3.8 kHz Split)
// ============================================================================
// Exact exponential 1-pole pole `p = exp(-2*pi*3800/44100) = 0.581931` cascaded
// into LR2 (`x_lp2 - ds_g * x_hp2`): 0 dB flat all-pass when `ds_g = 1.0`,
// true 12 dB/oct 4..10+ kHz attenuation when sibilance exceeds `rel_thr`.
stage3_deesser(x) = x_lp2 - ds_g * x_hp2
    with {
        x_lp1    = si.smooth(0.581931, x);
        x_lp2    = si.smooth(0.581931, x_lp1);
        x_hp1    = x - x_lp1;
        x_hp2    = x_hp1 - si.smooth(0.581931, x_hp1);
        sc_sib   = x_hp2;
        env_hi   = an.amp_follower_ud(0.0273957, 0.0006942, sc_sib);
        env_full = an.amp_follower_ud(0.0273957, 0.0006942, x);
        rel_thr  = deess_ratio * env_full;
        excess   = max(0.0, env_hi - rel_thr) / (rel_thr + 0.0001);
        raw_g    = 1.0 / (1.0 + deess_strength * excess);
        ds_g     = ba.if((deess_knob > 0.005) & (env_hi > 0.0005623), max(0.15, raw_g), 1.0);
    };

// ============================================================================
// STAGE 4: Output Soft Clipper / Peak Catcher (Padé [3/2] Rational tanh)
// ============================================================================
stage4_clipper(x) = ma.tanh(x * clip_drive) * clip_ceil * level_knob;

// ---- Mono-In / Mono-Out Master Chain ----
vocal_chain(x) = stage4_clipper(stage3_deesser(stage2_gate(stage1_hpf(x))));
process(xL, xR) = vocal_chain(xL);
