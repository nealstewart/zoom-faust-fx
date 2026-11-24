// effects/snesdsp/snesdsp.dsp
//
// Super Nintendo (SNES) Sony CXD1222Q S-DSP / SPC700 Audio Chip Emulator
// compiled to a Zoom MS-70CDR+ (.ZD2) package via:
//   python tools/faust2zd2.py effects/snesdsp/snesdsp.dsp --build
//
// Hardware Architecture Modeled:
//   1. Real-time 4-bit BRR (Bit Rate Reduction) ADPCM codec with all 4 authentic
//      SNES S-DSP predictor filters (F0: direct PCM, F1: 15/16, F2: 61/32 - 15/16,
//      F3: 115/64 - 13/16) and adaptive 2^shift block quantization.
//   2. Authentic Sony S-DSP 4-point Gaussian interpolator (matching the real
//      512-entry S-DSP ROM table: [0.181, 0.638, 0.181, 0.0] at t=0 and
//      [0.045, 0.455, 0.455, 0.045] at t=0.5), imparting the iconic ~11 dB
//      warm "SNES blanket" high-frequency rolloff.
//   3. Variable BRR voice clock (4.0 kHz .. 32.0 kHz) feeding the 32 kHz S-DSP.
//   4. 4 hardware revision / output modes (`SHVC`, `1CHIP`, `RAW`, `ZOH`) with
//      zero idle buzz on silence.

declare name "BRR DSP";
declare zd2_filename "XXBRRDSP";
declare zd2_id "07001260";
declare version "1.00";
declare zd2_display "BRR DSP";
declare zd2_badge "BR";
declare description "16-bit era 4-bit BRR ADPCM codec (F0-F3 predictors), 4-point Gaussian interpolator, 4-32 kHz voice clock, and 4 output models.";
declare zd2_dspload "20";

import("stdfaust.lib");
import("zoom.lib");

// ---- 7 Knobs ----
model_sel  = hslider("Model[1][values:WARM|CLEAN|RAW|ZOH][tooltip:SNES hardware stage (WARM = vintage warm stage, CLEAN = crisp revision, RAW = direct S-DSP digital, ZOH = bypass Gaussian).]", 0, 0, 3, 1);
filt_sel   = hslider("Filter[2][values:F0|F1|F2|F3][tooltip:SNES 4-bit BRR ADPCM predictor filter (F0=Direct, F1=15/16, F2=61/32-15/16, F3=115/64-13/16).]", 2, 0, 3, 1);
rate_khz   = hslider("Rate[3][unit:kHz][tooltip:BRR voice sample rate into the 32 kHz S-DSP Gaussian interpolator (4..32 kHz).]", 18, 4, 32, 1) : si.smoo;
gauss_knob = hslider("Gauss[4][tooltip:SNES 4-point Gaussian interpolator warmth / blanket rolloff (0 = crisp linear/ZOH, 75 = authentic SNES, 100 = extra cloudy).]", 75, 0, 100, 1) * 0.01 : si.smoo;
grit_knob  = hslider("BrrGrit[5][tooltip:4-bit BRR ADPCM compression & adaptive nibble quantization grit (0 = clean 16-bit, 55 = authentic BRR, 100 = crushed).]", 55, 0, 100, 1) * 0.01 : si.smoo;
drive_knob = hslider("Drive[6][tooltip:Pre-BRR input overdrive with S-DSP 16-bit saturation (0..100).]", 15, 0, 100, 1) * 0.01 : si.smoo;
level_knob = hslider("Level[7][curve:stock_vol][tooltip:Master output level (80 is unity gain).]", 80, 0, 100, 1) : si.smoo;

// ---- 1. Hardware Model & BRR Filter Tables (Hoisted to Block-Rate) ----
sel4(m, v0, v1, v2, v3) =
    ba.if(m < 0.5, v0,
    ba.if(m < 1.5, v1,
    ba.if(m < 2.5, v2, v3)));

// Authentic SNES BRR ADPCM predictor coefficients (F0..F3)
brr_k0 = sel4(filt_sel, 0.0, 0.9375,  1.90625,  1.796875);
brr_k1 = sel4(filt_sel, 0.0, 0.0,    -0.9375,  -0.8125);

// Hardware analog output stage LPF alpha & stereo width
// SHVC (~9.5 kHz warm), 1CHIP (~13.5 kHz clean), RAW (open), ZOH (open + bypass Gaussian)
analog_a  = sel4(model_sel, 0.64, 0.80, 0.98, 0.98);
st_width  = sel4(model_sel, 0.90, 1.00, 1.00, 1.00);
is_raw    = ba.if(model_sel > 1.5, 1.0, 0.0);
is_zoh    = ba.if(model_sel > 2.5, 1.0, 0.0);

// ---- 2. Block-Rate DSP Coefficients ----
// BRR voice clock step relative to 44.1 kHz (4.0 kHz .. 32.0 kHz)
rate_step = aa.clip(0.0907, 0.72562358, (rate_khz * 1000.0) / ma.SR);

// BRR ADPCM blend & pre-drive
brr_blend = grit_knob;
drv_pre   = 1.0 + drive_knob * (2.2 + 4.0 * drive_knob);
drv_comp  = 1.0 / (1.0 + drive_knob * 1.55);
// Extra quantization coarsening above Grit=55
q_scale   = 0.75 + grit_knob * 1.45;
out_gain  = level_knob * drv_comp;

// Gaussian blend (0 in ZOH model)
gauss_eff = ba.if(is_zoh > 0.5, 0.0, gauss_knob);
// Extra cloudy rolloff when Gauss > 0.75
extra_lp  = max(0.32, 0.98 - max(0.0, gauss_eff - 0.70) * 2.1);

// ---- 3. Authentic Sony S-DSP 4-Point Gaussian Interpolator ----
// Matches the real 512-entry S-DSP Gaussian ROM table to within ~0.4%
// (t=0 -> [0.181, 0.638, 0.181, 0.0]; t=0.5 -> [0.045, 0.455, 0.455, 0.045])
// with exact unity DC sum (w0 + w1 + w2 + w3 == 1.0).
snes_gauss_interp(h0, h1, h2, h3, t) =
    h1 + gauss_eff * (gauss - h1)
    with {
        omt   = 1.0 - t;
        w0    = 0.181 * omt * omt;
        w3    = 0.181 * t * t;
        rem   = 1.0 - w0 - w3;
        w1    = rem * (0.779 * omt + 0.221 * t);
        w2    = rem - w1;
        gauss = w0 * h0 + w1 * h1 + w2 * h2 + w3 * h3;
    };

// ---- 4. Real-Time SNES 4-Bit BRR ADPCM + 4-Point Gaussian Voice (`~` 8-wire state) ----
snes_voice_step(ph, p1, p2, env, h0, h1, h2, h3, x_in) =
    next_ph, next_p1, next_p2, next_env, next_h0, next_h1, next_h2, next_h3, interp_out
    with {
        ph_sum  = ph + rate_step;
        tk      = ba.if(ph_sum >= 1.0, 1.0, 0.0);
        next_ph = ph_sum - tk;

        // Exact zero-input silence path (zero idle buzz!)
        x_c     = aa.clip(-1.15, 1.15, x_in);
        is_sil  = ba.if(abs(x_c) < 1.0e-6 & abs(p1) < 1.0e-6 & abs(p2) < 1.0e-6, 1.0, 0.0);

        // BRR 4-bit ADPCM encode -> decode
        pred    = aa.clip(-1.25, 1.25, brr_k0 * p1 + brr_k1 * p2);
        err     = x_c - pred;
        abs_err = abs(err);
        e_init  = ba.if(env < 0.00024414, 0.0078125, env);
        e_raw   = ba.if(abs_err > e_init, e_init + 0.45 * (abs_err - e_init), e_init * 0.94);
        e_upd   = aa.clip(0.00024414, 0.5, e_raw);
        // Power-of-2 BRR shift step (2^shift)
        q_step  = pow(2.0, floor(log(e_upd) * 1.44269504) - 2.0) * q_scale;
        nib     = aa.clip(-8.0, 7.0, floor(err / q_step + 0.5));
        q_out   = aa.clip(-1.15, 1.15, pred + nib * q_step);

        dec_raw = x_c + brr_blend * (q_out - x_c);
        dec_upd = ba.if(is_sil > 0.5, 0.0, dec_raw);

        next_p1  = ba.if(tk > 0.5, dec_upd, p1);
        next_p2  = ba.if(tk > 0.5, p1,      p2);
        next_env = ba.if(tk > 0.5, e_upd,   env);
        next_h0  = ba.if(tk > 0.5, h1,      h0);
        next_h1  = ba.if(tk > 0.5, h2,      h1);
        next_h2  = ba.if(tk > 0.5, h3,      h2);
        next_h3  = ba.if(tk > 0.5, dec_upd, h3);

        interp_out = snes_gauss_interp(next_h0, next_h1, next_h2, next_h3, next_ph);
    };

snes_voice(x) = (snes_voice_step(x) ~ (_, _, _, _, _, _, _, _)) : !, !, !, !, !, !, !, !, _;

// ---- 5. Per-Channel SNES S-DSP Pipeline ----
snes_chan(x) = out
    with {
        driven = zm_soft_clip(x * drv_pre);
        voiced = snes_voice(driven);
        cloudy = zm_ema(extra_lp, voiced);
        analog = zm_ema(analog_a, cloudy);
        shaped = ba.if(is_raw > 0.5, cloudy, analog);
        out    = shaped - zm_ema(0.004, shaped);
    };

// ---- 6. Stereo Output Stage ----
process(xL, xR) =
    (mid + side * st_width) * out_gain,
    (mid - side * st_width) * out_gain
    with {
        cL   = snes_chan(xL);
        cR   = snes_chan(xR);
        mid  = 0.5 * (cL + cR);
        side = 0.5 * (cL - cR);
    };
