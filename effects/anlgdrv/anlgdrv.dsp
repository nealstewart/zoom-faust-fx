// effects/faustrat/faustrat.dsp
//
// Full Analog-Physics ProCo RAT Distortion implemented in FAUST (`~` recursive
// state feedback + `'` 1-sample delays) and compiled to a Zoom MS Plus (.ZD2)
// package via:
//   python tools/faust2zd2.py effects/faustrat/faustrat.dsp --build
//
// Includes ALL FOUR stateful analog-circuit dynamics from `effects/ratdrive/ratdrive.c`:
//   1. Semiconductor Junction Thermal State (`junction_temp(pwr)` via `~` feedback):
//      Asymmetric heat attack (61 ms) and cool release (269 ms) driven by signal
//      power (`|x| * gain`) that dynamically shrinks silicon diode Vf rails
//      (`pos_scale`, `neg_scale`), increases 2nd-harmonic asymmetry (`asym_k`),
//      droops the LM308 op-amp slew ceiling (`max_slew`), and warms the Filter.
//   2. 1/f Brownian Thermal Flicker & Stereo Component Drift (`no.noise` + `no.lfnoise`):
//      Xorshift32 white noise integrated through a slow Brownian pole (`drift`)
//      modulating diode rails, slew rate, and bias (`n_bias`).
//   3. Interstage Coupling-Capacitor Charge / Blocking Distortion (`cap_clip` via `~`):
//      2-wire recursive feedback loop `(cap_step(x, ...) ~ _) : !, _` where the
//      capacitor charges on clipping overshoot `clamp(u - clip(u), -1.5, 1.5)`
//      and shifts the DC operating bias `u = x + n_bias - cap` dynamically.
//   4. 4x Oversampled (176.4 kHz) Sub-Stepped LM308 Slew Limiter + Hard-Knee 1N914
//      Silicon Diode Clipper (`slew4_ovs` via `~` + `[1, 3, 3, 1]/8` sinc^3 decimation)
//      plus dynamic diode-conductance modulation of the RAT Filter LPF (`dyn_rat_lpf`).

declare name "Anlg Drive";
declare zd2_filename "XXANLGDR";
declare zd2_id "07001170";
declare version "1.00";
declare zd2_display "ANLG DRV";
declare zd2_badge "AD";
declare description "Analog-modeled op-amp slew distortion with diode clipping and passive low-pass filter.";
declare zd2_dspload "21";

import("stdfaust.lib");

// ---- 5 Knobs (matching effects/ratdrive/manifest.json) ----
dist_knob  = hslider("Dist[1][tooltip:LM308 gain + silicon diode distortion amount (0..100).]", 77, 0, 100, 1) * 0.01 : si.smoo;
fltr_knob  = hslider("Fltr[2][tooltip:Classic RAT backwards Filter tone control (0 = bright, 100 = dark).]", 23, 0, 100, 1) * 0.01 : si.smoo;
tight_knob = hslider("Tight[3][tooltip:Pre-clip bass cut / Ruetz mod (0 = fat 60 Hz, 100 = tight 320 Hz).]", 50, 0, 100, 1) * 0.01 : si.smoo;
mix_knob   = hslider("Mix[4][tooltip:Parallel dry/wet blend (100 = full distortion, 60-85 great on vocals).]", 85, 0, 100, 1) * 0.01 : si.smoo;
level_knob = hslider("Level[5][curve:stock_vol][tooltip:Output level (80 is unity at nominal MS-70CDR+ bus level).]", 80, 0, 100, 1) : si.smoo;

// ---- 1. 1/f Brownian Thermal Flicker & Base Circuit Parameters ----
white      = no.noise;
drift      = no.lfnoise(0.0072);
tight_hz   = 60.0 + 260.0 * tight_knob;
base_gain  = 1.5 + 4.5 * dist_knob + 38.0 * dist_knob * dist_knob * dist_knob;
n_bias     = 0.067 * drift * dist_knob;
f_curve    = fltr_knob * (1.5 - 0.5 * fltr_knob);
makeup     = (0.56 - 0.10 * dist_knob) * level_knob;

// ---- 2. Semiconductor Junction Temperature Follower (`~` recursive state) ----
// 61 ms heat attack (k=0.000372/sample) and 269 ms cool release (k=0.000084/sample)
temp_step(prev_temp, pwr) = prev_temp + ba.if(pwr > prev_temp, 0.000372, 0.000084) * (pwr - prev_temp);
junction_temp(pwr) = (temp_step(pwr) ~ _);

// ---- 3. 4x Oversampled (176.4 kHz) Sub-Stepped LM308 Slew Limiter (`~` state) ----
// Advances a single `s_prev` LM308 output voltage state across 4 linear sub-steps
// between `p = x'` and `t = x`, returning `[1, 3, 3, 1]/8` binomial-filtered slew
slew1(s, target, ms) = s + aa.clip(-ms, ms, target - s);
slew4_step(s_prev, p, t, ms) = s3, (0.125 * (s0 + 3.0 * (s1 + s2) + s3))
    with {
        stp = 0.25 * (t - p);
        s0  = slew1(s_prev, p + stp, ms);
        s1  = slew1(s0,     p + 2.0 * stp, ms);
        s2  = slew1(s1,     t - stp, ms);
        s3  = slew1(s2,     t, ms);
    };
slew4_ovs(x, ms) = (slew4_step(x', x, ms) ~ _) : !, _;

// ---- 4. Coupling-Capacitor Dynamic DC Bias Shift & Hard-Knee 1N914 Diode Clipper (`~` state) ----
// 2-wire feedback loop: wire 1 is `next_cap` (fed back to `cap`), wire 2 is `clipped_audio`
rat_clip(x, pos_sc, neg_sc, asym_k) =
    ba.if(xw >= 0.0, s * pos_sc, 0.0 - s * neg_sc)
    with {
        xw = x + asym_k * x * abs(x);
        a  = abs(xw);
        q  = min(1.0, (a - 0.45) * 1.1111111);
        s  = ba.if(a > 0.45, 0.45 + 0.40 * q * (2.0 - q), a);
    };

cap_step(cap, s_in, pos_sc, neg_sc, asym_k) = next_cap, c
    with {
        u        = s_in + n_bias - cap;
        c        = rat_clip(u, pos_sc, neg_sc, asym_k);
        i_cap    = aa.clip(-1.5, 1.5, u - c);
        next_cap = aa.clip(-0.38, 0.38, 0.9988 * cap + 0.0035 * i_cap);
    };
coupled_diode(s_in, pos_sc, neg_sc, asym_k) = (cap_step(s_in, pos_sc, neg_sc, asym_k) ~ _) : !, _;

// ---- 5. Dynamic Diode-Conductance RAT Post-Filter (`~` state) ----
rat_lpf_step(prev_y, ac, lp_k) = prev_y + lp_k * (ac - prev_y);
dyn_rat_lpf(ac, lp_k) = (rat_lpf_step(ac, lp_k) ~ _);

// ---- Complete Thermally Coupled RAT Chain ----
rat_channel(x_hp) =
    dyn_rat_lpf(
        fi.dcblocker(
            coupled_diode(
                slew4_ovs(x_hp * gain_t, max_slew_t),
                pos_scale_t,
                neg_scale_t,
                asym_t
            )
        ),
        lp_k_t
    ) * makeup
    with {
        inst_pwr    = min(1.0, 0.338 * abs(x_hp) * base_gain);
        temp        = junction_temp(inst_pwr);
        gain_t      = base_gain * (1.0 - 0.173 * temp + 0.171 * drift + 0.019 * white);
        pos_scale_t = max(0.25, 1.0 - 0.58 * temp + 0.07 * drift);
        neg_scale_t = max(0.25, 1.0 - 0.203 * temp - 0.07 * drift);
        asym_t      = (0.74 * temp + 0.18 * drift) * dist_knob;
        max_slew_t  = max(0.015, (0.18 - 0.11 * dist_knob) * (1.0 - 0.483 * temp + 0.07 * drift));
        lp_base_t   = (0.78 - 0.64 * f_curve) * (1.0 - 0.31 * temp + 0.07 * drift);
        lp_k_t      = max(0.08, lp_base_t * (1.0 - 0.12 * inst_pwr));
    };

process(x) = x + mix_knob * (rat_channel(fi.highpass(1, tight_hz, x)) - x);
