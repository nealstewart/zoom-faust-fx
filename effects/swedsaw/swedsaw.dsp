// effects/hm2saw/hm2saw.dsp
//
// Boss HM-2 Heavy Metal ("Chainsaw") Distortion implemented in FAUST and
// compiled to a Zoom MS-70CDR+ (.ZD2 + .ZIC) package via:
//   python tools/faust2zd2.py effects/hm2saw/hm2saw.dsp --build
//
// DSP Architecture:
//   1. Pre-clip ~160 Hz bass-tightening HPF (`fi.highpass1(160)`).
//   2. Semiconductor junction thermal follower (`junction_temp ~ _`) and 1/f
//      Brownian thermal flicker (`no.lfnoise(0.0072)`).
//   3. 4x Oversampled (176.4 kHz) dual-stage HM-2 cascaded silicon/transistor
//      clipper (`ef.hm2_clip`) with `[1, 3, 3, 1]/8` sinc^3 anti-aliasing
//      decimation and interstage coupling-capacitor bias shift (`coupled_hm2 ~ _`).
//   4. Active Dual-Gyrator Peaking EQ (3 Chamberlin SVFs):
//      - Color L: ~90 Hz sub-punch (`fi.svf_peak(90, 1.18, g_lo)`)
//      - Color H: ~1.15 kHz nasal chainsaw buzz (`fi.svf_peak(1150, 1.39, g_h1)`)
//               + ~3.0 kHz upper razor grit (`fi.svf_peak(3000, 1.05, g_h2)`)
//   5. Dynamic diode-conductance ~6.0 kHz cab/fizz tamer (`dyn_fizz_lpf ~ _`)
//      and unity-calibrated makeup gain.

declare name "Swed Saw";
declare zd2_filename "XXSWDSAW";
declare zd2_id "07001140";
declare version "1.00";
declare zd2_display "SWED SAW";
declare zd2_badge "SW";
declare description "High-gain Swedish chainsaw distortion with dual-band resonant mid/high gyrator EQ.";
declare zd2_dspload "27";

import("stdfaust.lib");

// ---- 5 Knobs (matching effects/hm2saw/manifest.json) ----
dist_knob = hslider("Dist[1][tooltip:Cascaded dual-stage chainsaw clipping gain (0..100; dime it for Swedish buzz!).]", 75, 0, 100, 1) * 0.01 : si.smoo;
coll_knob = hslider("Col L[2][tooltip:HM-2 ~90 Hz active gyrator low punch (50 = neutral, 100 = dimed wall).]", 75, 0, 100, 1) * 0.01 : si.smoo;
colh_knob = hslider("Col H[3][tooltip:HM-2 ~1.15 kHz + ~3.0 kHz active gyrator chainsaw snarl (50 = neutral, 100 = full buzz).]", 85, 0, 100, 1) * 0.01 : si.smoo;
mix_knob  = hslider("Mix[4][tooltip:Parallel dry/wet blend (100 = pure chainsaw, 65-85 keeps vocals razor-clear).]", 85, 0, 100, 1) * 0.01 : si.smoo;
level_knob = hslider("Level[5][curve:stock_vol][tooltip:Output level (80 is unity at nominal MS-70CDR+ bus level).]", 80, 0, 100, 1) : si.smoo;

// ---- 1. Thermal & Gyrator Control Parameters ----
white     = no.noise;
drift     = no.lfnoise(0.0072);
base_gain = 1.8 + 6.2 * dist_knob + 50.0 * dist_knob * dist_knob * dist_knob;
n_bias    = 0.067 * drift * dist_knob;

g_lo      = (coll_knob - 0.5) * ba.if(coll_knob >= 0.5, 3.8, 1.3);
g_h1      = (colh_knob - 0.5) * ba.if(colh_knob >= 0.5, 4.2, 1.4);
g_h2      = (colh_knob - 0.5) * ba.if(colh_knob >= 0.5, 2.4, 1.2);
eq_comp   = 1.0 - 0.30 * colh_knob;
makeup    = (0.26 + 0.04 * dist_knob) * eq_comp * level_knob;

// ---- 2. Semiconductor Junction Temperature Follower (`~` state) ----
temp_step(prev_temp, pwr) = prev_temp + ba.if(pwr > prev_temp, 0.000372, 0.000084) * (pwr - prev_temp);
junction_temp(pwr) = (temp_step(pwr) ~ _);

// ---- 3. 4x Oversampled Coupling-Cap + Dual-Stage HM-2 Clipper (`~` state) ----
hm2_clip(x, asym, pos_lim, neg_lim) =
    aa.clip(neg_lim, pos_lim, xc * (2.1375 - 0.7125 * xc * xc) + 0.08)
    with {
        xw = x + asym * x * abs(x);
        xc = aa.clip(-1.0, 1.0, xw);
    };

hm2_cap_step(cap, p_in, t_in, asym_k, pos_lim, neg_lim) = next_cap, wet4
    with {
        stp      = 0.25 * (t_in - p_in);
        b        = n_bias - cap;
        c0       = hm2_clip(p_in + stp + b,       asym_k, pos_lim, neg_lim);
        c1       = hm2_clip(p_in + 2.0 * stp + b, asym_k, pos_lim, neg_lim);
        c2       = hm2_clip(t_in - stp + b,       asym_k, pos_lim, neg_lim);
        u3       = t_in + b;
        c3       = hm2_clip(u3,                   asym_k, pos_lim, neg_lim);
        wet4     = 0.125 * (c0 + 3.0 * (c1 + c2) + c3);
        i_cap    = aa.clip(-1.5, 1.5, u3 - c3);
        next_cap = aa.clip(-0.38, 0.38, 0.9988 * cap + 0.0035 * i_cap);
    };
coupled_hm2(x_scaled, asym_k, pos_lim, neg_lim) =
    (hm2_cap_step(x_scaled', x_scaled, asym_k, pos_lim, neg_lim) ~ _) : !, _;

// ---- 4. Dynamic Diode-Conductance ~6 kHz Fizz Tamer (`~` state) ----
fizz_step(prev_y, x_eq, fk) = prev_y + fk * (x_eq - prev_y);
dyn_fizz_lpf(x_eq, fk) = (fizz_step(x_eq, fk) ~ _);

// ---- Complete HM-2 Chainsaw Channel ----
hm2_channel(x_hp) =
    dyn_fizz_lpf(
        fi.svf_peak(3000.0, 1.05, g_h2,
            fi.svf_peak(1150.0, 1.39, g_h1,
                fi.svf_peak(90.0, 1.18, g_lo,
                    fi.dcblocker(
                        coupled_hm2(x_hp * gain_t, asym_t, pos_lim_t, neg_lim_t)
                    )
                )
            )
        ),
        fizz_k_t
    ) * makeup
    with {
        inst_pwr  = min(1.0, 0.29 * abs(x_hp) * base_gain);
        temp      = junction_temp(inst_pwr);
        gain_t    = base_gain * (1.0 - 0.173 * temp + 0.171 * drift + 0.019 * white);
        asym_t    = (0.74 * temp + 0.18 * drift) * dist_knob;
        pos_lim_t = max(0.25, 0.88 * (1.0 - 0.58 * temp + 0.07 * drift));
        neg_lim_t = min(-0.25, -0.96 * (1.0 - 0.203 * temp - 0.07 * drift));
        fizz_k_t  = 0.46 * (1.0 - 0.14 * inst_pwr);
    };

process(x) = x + mix_knob * (hm2_channel(fi.highpass(1, 160.0, x)) - x);
