// effects/mackdrv/mackdrv.dsp
//
// 1990s Mackie 1202 / CR-1604 Console Channel-Strip Overdrive implemented in
// FAUST and compiled to a Zoom MS-70CDR+ (.ZD2 + .ZIC) package via:
//   python tools/faust2zd2.py effects/mackdrv/mackdrv.dsp --build
//
// DSP Architecture:
//   1. Gentle console input ~40 Hz sub-rumble HPF (`fi.highpass1(40)`).
//   2. Shared Console Power-Rail Sag Envelope (`an.amp_follower_ud`) + Dual Op-Amp
//      Die Thermal Follower (`junction_temp ~ _`) + 1/f Brownian Drift (`no.lfnoise`),
//      matching a real Mackie 1202/CR-1604 shared ±15V PSU & NJM4580D dual op-amp.
//   3. 4x Oversampled (176.4 kHz) Dual-Stage Mackie Preamp Saturation
//      (`mack_clip` cubic pre-warmth + asymmetric rail crush) with
//      `[1, 3, 3, 1]/8` sinc^3 decimation and coupling-capacitor bias shift.
//   4. Post-Preamp 3-Band Mackie Channel-Strip EQ:
//      - Low shelf (~110 Hz): `fi.lowpass1(110)` trend boost/cut around 50
//      - Mid bell  (~2.5 kHz): `fi.svf_peak(2500, 1.11, g_mid)`
//      - High shelf (~6.5 kHz): `fi.highpass1(6500)` residual boost/cut around 50

declare name "Desk Drive";
declare zd2_filename "XXDESKDR";
declare zd2_id "07001130";
declare version "1.00";
declare zd2_display "DESK DRV";
declare zd2_badge "DK";
declare description "Overdriven compact analog console channel strip preamp with soft clipping and 3-band EQ.";
declare zd2_dspload "30";

import("stdfaust.lib");

// ---- 6 Knobs (matching effects/mackdrv/manifest.json) ----
gain_knob  = hslider("Gain[1][tooltip:Console mic-preamp Trim overload (0 = warm glue, 100 = crushed gabber/EBM).]", 55, 0, 100, 1) * 0.01 : si.smoo;
low_knob   = hslider("Low[2][tooltip:100 Hz channel-strip Low shelf (50 = flat, 0 = cut, 100 = +10 dB boost).]", 50, 0, 100, 1) * 0.01 : si.smoo;
mid_knob   = hslider("Mid[3][tooltip:2.5 kHz channel-strip Mid bell (50 = flat, 0 = scoop, 100 = vocal bite).]", 60, 0, 100, 1) * 0.01 : si.smoo;
high_knob  = hslider("High[4][tooltip:6.5 kHz channel-strip High shelf (50 = flat, 0 = dark, 100 = crisp air).]", 45, 0, 100, 1) * 0.01 : si.smoo;
mix_knob   = hslider("Mix[5][tooltip:Parallel dry/wet blend (100 = full console strip).]", 100, 0, 100, 1) * 0.01 : si.smoo;
level_knob = hslider("Level[6][curve:stock_vol][tooltip:Channel fader output level (80 is unity at nominal MS-70CDR+ bus level).]", 80, 0, 100, 1) : si.smoo;

// ---- 1. Console Thermal & 3-Band EQ Parameters ----
white     = no.noise;
drift     = no.lfnoise(0.0072);
base_gain = 1.2 + 4.0 * gain_knob + 28.0 * gain_knob * gain_knob * gain_knob;
bias_base = (0.06 + 0.067 * drift) * gain_knob;

g_low     = (low_knob  - 0.5) * ba.if(low_knob  >= 0.5, 3.0, 1.4);
g_mid     = (mid_knob  - 0.5) * ba.if(mid_knob  >= 0.5, 2.6, 1.3);
g_high    = (high_knob - 0.5) * ba.if(high_knob >= 0.5, 2.4, 1.4);
makeup    = (0.46 - 0.06 * gain_knob) * level_knob;

// ---- 2. Op-Amp Die Temperature Follower (`~` state) ----
temp_step(prev_temp, pwr) = prev_temp + ba.if(pwr > prev_temp, 0.000372, 0.000084) * (pwr - prev_temp);
junction_temp(pwr) = (temp_step(pwr) ~ _);

// ---- 3. 4x Oversampled Coupling-Cap + Dual-Stage Mackie Clipper (`~` state) ----
mack_clip(x, asym, pos_lim, neg_lim) =
    aa.clip(neg_lim, pos_lim, xc * (1.35 - 0.20 * xc * xc))
    with {
        xw = x + asym * x * abs(x);
        xc = aa.clip(-1.5, 1.5, xw);
    };

mack_cap_step(cap, p_in, t_in, asym_k, pos_lim, neg_lim) = next_cap, wet4
    with {
        stp      = 0.25 * (t_in - p_in);
        b        = bias_base - cap;
        c0       = mack_clip(p_in + stp + b,       asym_k, pos_lim, neg_lim);
        c1       = mack_clip(p_in + 2.0 * stp + b, asym_k, pos_lim, neg_lim);
        c2       = mack_clip(t_in - stp + b,       asym_k, pos_lim, neg_lim);
        u3       = t_in + b;
        c3       = mack_clip(u3,                   asym_k, pos_lim, neg_lim);
        wet4     = 0.125 * (c0 + 3.0 * (c1 + c2) + c3);
        i_cap    = aa.clip(-1.5, 1.5, u3 - c3);
        next_cap = aa.clip(-0.38, 0.38, 0.9988 * cap + 0.0035 * i_cap);
    };
coupled_mack(x_scaled, asym_k, pos_lim, neg_lim) =
    (mack_cap_step(x_scaled', x_scaled, asym_k, pos_lim, neg_lim) ~ _) : !, _;

// ---- 4. 3-Band Mackie Channel-Strip EQ ----
mack_low_shelf(y)  = y + g_low  * fi.lowpass(1, 110.0, y);
mack_mid_bell(y)   = fi.svf_peak(2500.0, 1.11, g_mid, y);
mack_high_shelf(y) = y + g_high * fi.highpass(1, 6500.0, y);

// ---- Complete Mackie 1202 Channel (shared stereo PSU sag & dual op-amp thermal state) ----
mack_strip(x_hp, gain_t, asym_t, pos_lim_t, neg_lim_t) =
    mack_high_shelf(
        mack_mid_bell(
            mack_low_shelf(
                fi.dcblocker(
                    coupled_mack(x_hp * gain_t, asym_t, pos_lim_t, neg_lim_t)
                )
            )
        )
    ) * makeup;

process(xL, xR) = (xL + mix_knob * (wetL - xL)),
                  (xR + mix_knob * (wetR - xR))
    with {
        hpL       = fi.highpass(1, 40.0, xL);
        hpR       = fi.highpass(1, 40.0, xR);
        bus_peak  = max(abs(hpL), abs(hpR));
        sag_env   = an.amp_follower_ud(0.0030, 0.00040, bus_peak);
        inst_pwr  = min(1.0, 0.34 * bus_peak * base_gain);
        temp      = junction_temp(inst_pwr);
        gain_t    = base_gain * (1.0 - 0.173 * temp + 0.171 * drift + 0.019 * white);
        asym_t    = (0.74 * temp + 0.18 * drift) * gain_knob;
        pos_lim_t = max(0.25, 0.92 * (1.0 - 0.25 * sag_env - 0.45 * temp + 0.07 * drift));
        neg_lim_t = min(-0.25, -0.84 * (1.0 - 0.20 * sag_env - 0.20 * temp - 0.07 * drift));
        wetL      = mack_strip(hpL, gain_t, asym_t, pos_lim_t, neg_lim_t);
        wetR      = mack_strip(hpR, gain_t, asym_t, pos_lim_t, neg_lim_t);
    };
