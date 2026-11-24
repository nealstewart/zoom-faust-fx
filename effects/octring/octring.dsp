// effects/octring/octring.dsp
//
// Hellektro Octave / Ring-Mod Gated Velcro Fuzz implemented in FAUST and
// compiled to a Zoom MS-70CDR+ (.ZD2 + .ZIC) package via:
//   python tools/faust2zd2.py effects/octring/octring.dsp --build
//
// DSP Architecture:
//   1. Pre-clip ~120 Hz HPF (`fi.highpass1(120)`) + starved-BJT thermal
//      noise-gate envelope (`an.amp_follower_ud`).
//   2. Full-Wave Rectifier Octave (`|x| - DC`, blended via `Oct`).
//   3. Zero-Trig Parabolic Sine Carrier Ring Modulator (`os.osc(freq_hz)`
//      blended via `Ring`, sweeping 40..1200 Hz).
//   4. 4x Oversampled (176.4 kHz) Asymmetrical Gated Velcro Fuzz Clipper
//      (`ef.velcro_clip`) with coupling-capacitor blocking distortion
//      (`coupled_velcro ~ _`) and dynamic ~5.5 kHz fizz tamer.

declare name "Oct Ring";
declare zd2_filename "XXOCTRG";
declare zd2_id "07001150";
declare version "1.00";
declare zd2_display "OCT RING";
declare zd2_badge "SH";
declare description "Clean-room FAUST Hellektro Octave/Ring-Mod Fuzz: full-wave rectifier octave gnarl, internal carrier ring modulator, and 4x oversampled gated velcro fuzz.";
declare zd2_dspload "26";

import("stdfaust.lib");

// ---- 6 Knobs (matching effects/octring/manifest.json) ----
fuzz_knob  = hslider("Fuzz[1][tooltip:Gated velcro fuzz drive (0..100).]", 60, 0, 100, 1) * 0.01 : si.smoo;
oct_knob   = hslider("Oct[2][tooltip:Octavia/Superfuzz full-wave rectifier upper-octave gnarl (0..100).]", 55, 0, 100, 1) * 0.01 : si.smoo;
ring_knob  = hslider("Ring[3][tooltip:Internal sine-carrier Ring Modulator depth (0 = pure octave fuzz, 100 = full robotic ring).]", 35, 0, 100, 1) * 0.01 : si.smoo;
freq_knob  = hslider("Freq[4][tooltip:Ring Mod carrier frequency (40 Hz demonic growl -> 1200 Hz metallic shriek).]", 35, 0, 100, 1) * 0.01 : si.smoo;
mix_knob   = hslider("Mix[5][tooltip:Parallel dry/wet blend (60-85 keeps vocals intelligible while ripping).]", 80, 0, 100, 1) * 0.01 : si.smoo;
level_knob = hslider("Level[6][curve:stock_vol][tooltip:Output level (80 is unity at nominal MS-70CDR+ bus level).]", 80, 0, 100, 1) : si.smoo;

// ---- 1. Thermal & Carrier Setup ----
white     = no.noise;
drift     = no.lfnoise(0.0072);
base_gain = 2.2 + 40.0 * fuzz_knob * fuzz_knob;
n_bias    = 0.067 * drift * fuzz_knob;
freq_hz   = 40.0 + 1160.0 * freq_knob * freq_knob;
dry_ring  = 1.0 - ring_knob;
makeup    = (0.40 + 0.42 * (1.0 - fuzz_knob) * (1.0 - fuzz_knob)) * level_knob;

// ---- 2. Starved-BJT Junction Temperature Follower (`~` state) ----
temp_step(prev_temp, pwr) = prev_temp + ba.if(pwr > prev_temp, 0.000372, 0.000084) * (pwr - prev_temp);
junction_temp(pwr) = (temp_step(pwr) ~ _);

// ---- 3. Full-Wave Rectifier Octave Stage (`|x| - DC`) ----
fullwave_oct(x) = x + oct_knob * (fi.highpass(1, 30.0, abs(x)) * 1.8 - x);

// ---- 4. 4x Oversampled Coupling-Cap + Velcro Fuzz Clipper (`~` state) ----
velcro_clip(x, asym, pos_lim, neg_lim) =
    aa.clip(neg_lim, pos_lim, (x + asym * x * abs(x) + 0.05) * 1.45);

velcro_cap_step(cap, p_in, t_in, asym_k, pos_lim, neg_lim) = next_cap, wet4
    with {
        stp      = 0.25 * (t_in - p_in);
        b        = n_bias - cap;
        c0       = velcro_clip(p_in + stp + b,       asym_k, pos_lim, neg_lim);
        c1       = velcro_clip(p_in + 2.0 * stp + b, asym_k, pos_lim, neg_lim);
        c2       = velcro_clip(t_in - stp + b,       asym_k, pos_lim, neg_lim);
        u3       = t_in + b;
        c3       = velcro_clip(u3,                   asym_k, pos_lim, neg_lim);
        wet4     = 0.125 * (c0 + 3.0 * (c1 + c2) + c3);
        i_cap    = aa.clip(-1.5, 1.5, u3 - c3);
        next_cap = aa.clip(-0.42, 0.42, 0.9986 * cap + 0.0042 * i_cap);
    };
coupled_velcro(x_mod, asym_k, pos_lim, neg_lim) =
    (velcro_cap_step(x_mod', x_mod, asym_k, pos_lim, neg_lim) ~ _) : !, _;

// ---- 5. Dynamic Diode-Conductance ~5.5 kHz Fizz Tamer (`~` state) ----
fizz_step(prev_y, x_dc, fk) = prev_y + fk * (x_dc - prev_y);
dyn_fizz_lpf(x_dc, fk) = (fizz_step(x_dc, fk) ~ _);

// ---- Complete OctRing Channel ----
octring_channel(x_hp) =
    dyn_fizz_lpf(
        fi.dcblocker(
            coupled_velcro(x_oct * ring_mod * gain_t, asym_t, pos_lim_t, neg_lim_t) * g_open
        ),
        fizz_k_t
    ) * makeup
    with {
        gate_env  = an.amp_follower_ud(0.04, 0.0007, x_hp);
        inst_pwr  = min(1.0, 0.338 * abs(x_hp) * base_gain);
        temp      = junction_temp(inst_pwr);
        g_thresh  = 0.002 + 0.0014 * temp;
        g_open    = aa.clip(0.0, 1.0, (gate_env - g_thresh) * 100.0);
        x_oct     = fullwave_oct(x_hp);
        ring_mod  = dry_ring + ring_knob * os.osc(freq_hz);
        gain_t    = base_gain * (1.0 - 0.173 * temp + 0.171 * drift + 0.019 * white);
        asym_t    = (0.74 * temp + 0.18 * drift) * fuzz_knob;
        pos_lim_t = max(0.25, 0.90 * (1.0 - 0.58 * temp + 0.07 * drift));
        neg_lim_t = min(-0.25, -0.96 * (1.0 - 0.203 * temp - 0.07 * drift));
        fizz_k_t  = 0.44 * (1.0 - 0.14 * inst_pwr);
    };

process(x) = x + mix_knob * (octring_channel(fi.highpass(1, 120.0, x)) - x);
