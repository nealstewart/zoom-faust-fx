// effects/sophwg/sophwg.dsp
//
// SOPHIE-Style 4-Material Vocal-Triggered Pitch-Envelope + Karplus-Strong + Comb Synthesizer
// (0.00 Rubber -> 0.33 Bubble -> 0.67 Plastic -> 1.00 Metal)
// compiled to a Zoom MS-70CDR+ (.ZD2 + .ZIC) package via:
//   python tools/faust2zd2.py effects/sophwg/sophwg.dsp --build
//
// 4-Split Material Morphing Architecture (Mono-In -> Stereo-Out):
//   1. 0.00 RUBBER (Thudding, Short Bouncy):
//      - Pitch Mul: 0.55x | P.Env: +2.40 oct | P.Env Decay: 180 ms
//      - KS Feedback: 0.89 | KS LPF: 580 Hz  | Comb: 0.00 | Crash: 0.35
//   2. 0.33 BUBBLE (User-tuned liquid resonant bubble):
//      - Pitch Mul: 2.75x | P.Env: +1.15 oct | P.Env Decay: 600 ms
//      - KS Feedback: 0.98 | KS LPF: 1500 Hz | Comb: -0.25 | Crash: 0.00
//   3. 0.67 PLASTIC (High-pitched frictional balloon/toy squeak):
//      - Pitch Mul: 5.50x (~800 Hz base -> ~2.6 kHz chirp peak)
//      - P.Env: +2.40 oct with a slower ~20 ms rising chirp attack + 75 ms decay
//        so every transient sweeps UP and DOWN ("WEE-irp!") like rubbed vinyl/plastic
//      - KS Feedback: 0.945 (high Q over short ~18-sample delay line = pure tonal whistle,
//        yet still decays to silence in ~70 ms!)
//      - KS LPF: 11,500 Hz | Comb: -0.35 (at 0.50 * T0) | Crash: 0.45
//   4. 1.00 METAL (Crashing Inharmonic Bell/Plate):
//      - Pitch Mul: 1.25x | P.Env: +0.35 oct | P.Env Decay: 35 ms
//      - KS Feedback: 0.992 | KS LPF: 14,500 Hz | Comb: +0.65 (at 0.4142 * T0) | Crash: 3.20

declare name "Soph WG";
declare zd2_filename "XXSOPHWG";
declare zd2_id "070011c0";
declare version "1.00";
declare zd2_display "SOPH WG";
declare zd2_badge "SW";
declare description "SOPHIE-style 4-point morphing Karplus-Strong + comb + triggered pitch-envelope vocal synthesizer (Rubber, Bubble, Squeaky Plastic, Inharmonic Metal).";
declare zd2_dspload "32";

import("stdfaust.lib");
import("zoom.lib");

// ---- 6 Pedal Knobs (Mapped to Zoom MS-70CDR+ Parameter Pages) ----
material   = hslider("Matrl[1][tooltip:4-point material morph (0 = Rubber, 33 = Bubble, 67 = Squeaky Plastic, 100 = Metal).]", 33, 0, 100, 1) * 0.01 : si.smoo;
pitch_tune = hslider("Pitch[2][tooltip:Base Karplus-Strong pitch (-24..+24 semitones).]", 0, -24, 24, 1) : si.smoo;
pitch_env  = hslider("P.Env[3][tooltip:Onset-triggered pitch envelope sweep depth (-100..+100%).]", 88, -100, 100, 1) * 0.01 : si.smoo;
decay      = hslider("Decay[4][tooltip:Karplus-Strong feedback decay scaling (0..100%).]", 55, 0, 100, 1) * 0.01 : si.smoo;
bite_disp  = hslider("Bite[5][tooltip:Comb filter depth and transient bite (0..100%).]", 80, 0, 100, 1) * 0.01 : si.smoo;
mix        = hslider("Mix[6][tooltip:Dry vocal vs. wet blend (0..100%).]", 95, 0, 100, 1) * 0.01 : si.smoo;

// ---- 4-Point Partition-of-Unity Triangular Weights (0.00, 0.333, 0.667, 1.00) ----
m3    = material * 3.0;
w_rub = max(0.0, 1.0 - m3);
w_bub = max(0.0, 1.0 - abs(m3 - 1.0));
w_pls = max(0.0, 1.0 - abs(m3 - 2.0));
w_met = max(0.0, m3 - 2.0);

// ---- 1. Base Pitch Multiplier across the 4 Materials ----
mat_freq_mul = 0.55 * w_rub + 2.75 * w_bub + 5.50 * w_pls + 1.25 * w_met;
base_hz      = 145.0 * ba.semi2ratio(pitch_tune) * mat_freq_mul;
base_del     = aa.clip(6.0, 960.0, ma.SR / base_hz);

// ---- 2. Triggered Pitch-Envelope Attack, Decay & Sweep Depth ----
// Bubble uses the long 600 ms pitch envelope decay (pole = 0.999904) with +1.15 oct;
// Plastic uses a ~18 ms rising chirp attack (`0.0085`) + 75 ms decay with +2.40 oct
// so it makes a vocal upward-then-downward "WEE-irp!" balloon/toy squeak!
penv_att_k      = 0.1800 * w_rub + 0.1200 * w_bub + 0.0085 * w_pls + 0.2200 * w_met;
penv_decay_pole = 0.999680 * w_rub + 0.999904 * w_bub + 0.999250 * w_pls + 0.998350 * w_met;
mat_penv_oct    = 2.40 * w_rub + 1.15 * w_bub + 2.40 * w_pls + 0.35 * w_met;

// ---- 3. Karplus-Strong Loop Feedback (Decay) ----
// Bubble = 0.98 at default Decay=55%; Plastic = 0.945 (high Q for pure tonal squeak
// at 800..2500 Hz, while still decaying to silence in ~70 ms because T0 is only ~20 samples!)
fb_base = 0.890 * w_rub + 0.980 * w_bub + 0.945 * w_pls + 0.991 * w_met;
ks_fb   = aa.clip(0.40, 0.996, fb_base + (decay - 0.55) * (0.10 * w_rub + 0.03 * w_bub + 0.06 * w_pls + 0.012 * w_met));

// ---- 4. Karplus-Strong Loop Damping LPF ----
// Rubber = 580 Hz, Bubble = 1500 Hz, Plastic = 11500 Hz, Metal = 14500 Hz
ks_lpf_hz = 580.0 * w_rub + 1500.0 * w_bub + 11500.0 * w_pls + 14500.0 * w_met;

// ---- 5. Comb Filter Tap Ratio & Gain ----
// Bubble = -0.25, Plastic = -0.35 (at 0.50 * T0), Metal = +0.65 (at 0.4142 * T0)
comb_ratio = 0.50 * w_pls + 0.4142 * (w_rub + w_bub + w_met);
comb_raw   = 0.00 * w_rub - 0.25 * w_bub - 0.35 * w_pls + 0.65 * w_met;
comb_g     = comb_raw * (0.35 + 0.65 * bite_disp);

// ---- 6. Exciter Transient Crash vs. Body ----
// Bubble has Crash = 0.00 (pure liquid body excitation); Metal has Crash = 3.20
crash_gain = (0.35 * w_rub + 0.00 * w_bub + 0.45 * w_pls + 3.20 * w_met) * (0.4 + 0.6 * bite_disp);
body_gain  = 2.60 * w_rub + 2.40 * w_bub + 2.20 * w_pls + 0.90 * w_met;

dry_gain = 1.0 - mix;
wet_gain = mix * 1.35;

// ---- 1. Onset Detector & Triggered Pitch-Envelope Generator (`~ _`) ----
vox_hpf(x)  = fi.highpass(2, 95.0, x);
env_fast(x) = an.amp_follower_ud(0.06, 0.0015, x);
env_slow(x) = an.amp_follower_ud(0.0015, 0.0003, x);

trig_penv_step(prev_env, x) = next_env
    with {
        ef          = env_fast(x);
        es          = env_slow(x);
        flux        = ef - es;
        is_onset    = ba.if(flux > 0.006, 1.0, 0.0);
        target_peak = min(1.6, flux * 16.0 + ef * 2.5);
        decayed     = prev_env * penv_decay_pole;
        attacked    = prev_env + penv_att_k * (target_peak - prev_env);
        next_env    = ba.if((is_onset > 0.5) & (target_peak > decayed), attacked, decayed);
    };

triggered_pitch_env(x) = (trig_penv_step(x) ~ _);

ks_delay_smps(x) = aa.clip(6.0, 980.0, base_del * mod_ratio)
    with {
        penv_sig  = triggered_pitch_env(x);
        bend      = penv_sig * pitch_env * mat_penv_oct;
        mod_ratio = aa.clip(0.14, 3.2, (1.0 - 0.38 * bend) / max(0.14, 1.0 + 0.55 * bend));
    };

ks_exciter(x) = (body_exc * body_gain + crash_exc * crash_gain) * 0.70
    with {
        xh         = vox_hpf(x);
        trans      = max(0.0, env_fast(xh) - env_slow(xh));
        burst_gate = min(3.2, trans * 12.0);
        body_exc   = fi.lowpass(1, 1200.0 + 5500.0 * (1.0 - w_rub), xh);
        crash_exc  = fi.highpass(1, 1800.0, xh) * burst_gate;
    };

// ---- 2. Unified Karplus-Strong + Comb Loop ----
ks_comb_step(prev_sig, exc, d_smps) = exc + fb_sig
    with {
        ks_tap   = de.fdelay3(1024, d_smps, prev_sig);
        comb_tap = de.fdelay1(1024, max(3.0, d_smps * comb_ratio), prev_sig);

        norm_g   = 1.0 / (1.0 + abs(comb_g) * 0.96);
        raw_loop = (ks_tap + comb_g * comb_tap) * norm_g;

        damped   = fi.dcblocker(fi.lowpass(1, ks_lpf_hz, raw_loop));
        fb_sig   = zm_soft_clip(damped) * ks_fb;
    };

ks_synth(exc, d_smps) = (ks_comb_step(exc, d_smps) ~ _);

// ---- 3. Post-Loop Stereo Comb Spatializer ----
process(xL, xR) = out_l, out_r
    with {
        d_smps = ks_delay_smps(xL);
        exc    = ks_exciter(xL);
        loop_s = ks_synth(exc, d_smps);

        wet_l  = de.fdelay3(1024, d_smps, loop_s);
        wet_r  = de.fdelay3(1024, min(1018.0, d_smps * (1.0 + 0.012 * w_met) + 4.0), loop_s);

        out_l  = xL * dry_gain + wet_l * wet_gain;
        out_r  = xL * dry_gain + wet_r * wet_gain;
    };
