// effects/ps1spu/ps1spu.dsp
//
// Sony PlayStation 1 CXD2922Q SPU 4-Bit XA ADPCM Voice Codec & 4-Point
// Gaussian DAC implemented in FAUST and compiled to a Zoom MS-70CDR+ (.ZD2)
// package via:
//   python tools/faust2zd2.py effects/ps1spu/ps1spu.dsp --build

declare name "XA SPU";
declare zd2_filename "XXXASPU";
declare zd2_id "070011a0";
declare version "1.00";
declare zd2_display "XA SPU";
declare zd2_badge "XA";
declare description "32-bit era 4-bit XA ADPCM codec and 4-point Gaussian DAC interpolator lo-fi processor.";
declare zd2_dspload "23";

import("stdfaust.lib");
import("zoom.lib");

// ---- 6 Knobs (matching effects/ps1spu/manifest.json) ----
rate_knob   = hslider("Rate[1][tooltip:SPU voice sample clock (0 = 6 kHz dark loop, 50 = 19.6 kHz VAG, 100 = 44.1 kHz).]", 50, 0, 100, 1) * 0.01 : si.smoo;
adpcm_knob  = hslider("ADPCM[2][tooltip:4-bit XA ADPCM intensity and predictor drive (0 = 16-bit PCM, 50 = 4-bit XA, 100 = slope-overload crunch).]", 55, 0, 100, 1) * 0.01 : si.smoo;
pred_sel    = hslider("Pred[3][values:FLAT|XA 1|XA 2|XA 3|XA 4][tooltip:Sony SPU ADPCM differential IIR predictor filter (FLAT, XA 1..4).]", 2, 0, 4, 1);
gauss_knob  = hslider("Gauss[4][tooltip:DAC reconstruction (0 = ZOH steps, 35 = linear, 75 = PS1 4-point Gaussian, 100 = warm pillow).]", 75, 0, 100, 1) * 0.01 : si.smoo;
width_knob  = hslider("Width[5][tooltip:Stereo sample width (0 = mono 512KB RAM-saver voice, 100 = full stereo).]", 40, 0, 100, 1) * 0.01 : si.smoo;
level_knob  = hslider("Level[6][curve:stock_vol][tooltip:Master output level (80 is unity at nominal MS-70CDR+ bus level).]", 80, 0, 100, 1) : si.smoo;

// ---- 1. Sony CXD2922Q Hardware IIR Predictor Coefficients (Exact 64ths) ----
xa_k0 = ba.if(pred_sel < 0.5, 0.0,
        ba.if(pred_sel < 1.5, 0.9375,
        ba.if(pred_sel < 2.5, 1.796875,
        ba.if(pred_sel < 3.5, 1.53125, 1.90625))));

xa_k1 = ba.if(pred_sel < 1.5, 0.0,
        ba.if(pred_sel < 2.5, -0.8125,
        ba.if(pred_sel < 3.5, -0.859375, -0.9375)));

// ---- 2. Clock Rate, Anti-Alias Pre-Filter & ADPCM Slope-Overload Drive ----
rate_step   = min(1.0, 0.1360544 + rate_knob * (0.370 + 0.4939456 * rate_knob));
k_pre       = aa.clip(0.14, 0.94, rate_step * 0.88);
adpcm_blend = min(1.0, adpcm_knob * 2.0);
drv_pre     = 1.0 + max(0.0, adpcm_knob - 0.5) * 8.0;
drv_post    = 1.0 / drv_pre;

// ---- 3. 4-Point Gaussian / Linear / ZOH & Post-Pillow Coefficients ----
zoh_to_lin   = min(1.0, gauss_knob * 2.85714286);
lin_to_gauss = aa.clip(0.0, 1.0, (gauss_knob - 0.35) * 2.5);
k_post       = aa.clip(0.30, 1.0, 1.0 - max(0.0, gauss_knob - 0.75) * 2.72);
out_gain     = 0.5 * level_knob;

// ---- 4. Pure-FAUST 4-Point Gaussian / Linear / ZOH Interpolator ----
spu_interp(h0, h1, h2, h3, t) = out_lin + lin_to_gauss * (gauss - out_lin)
    with {
        lin     = h1 + t * (h2 - h1);
        t2      = t * t;
        t3      = t2 * t;
        omt     = 1.0 - t;
        w0      = 0.16666667 * omt * omt * omt;
        w1      = 0.16666667 * (3.0 * t3 - 6.0 * t2 + 4.0);
        w2      = 0.16666667 * (0.0 - 3.0 * t3 + 3.0 * t2 + 3.0 * t + 1.0);
        w3      = 0.16666667 * t3;
        gauss   = w0 * h0 + w1 * h1 + w2 * h2 + w3 * h3;
        out_lin = h1 + zoh_to_lin * (lin - h1);
    };

// ---- 5. Real-Time Sony SPU 4-Bit XA ADPCM Voice State Machine (`~` 8-wire state) ----
spu_voice_step(ph, p1, p2, env, h0, h1, h2, h3, r_step, x_in) =
    next_ph, next_p1, next_p2, next_env, next_h0, next_h1, next_h2, next_h3, interp_out
    with {
        ph_sum     = ph + r_step;
        tk         = ba.if(ph_sum >= 1.0, 1.0, 0.0);
        next_ph    = ph_sum - tk;

        x_c        = aa.clip(-1.25, 1.25, x_in);
        pred       = aa.clip(-1.35, 1.35, xa_k0 * p1 + xa_k1 * p2);
        err        = x_c - pred;
        abs_err    = abs(err);
        e_init     = ba.if(env < 0.00024414, 0.015625, env);
        e_raw      = ba.if(abs_err > e_init, e_init + 0.45 * (abs_err - e_init), e_init * 0.945);
        e_upd      = aa.clip(0.00024414, 0.5, e_raw);
        q_step     = pow(2.0, floor(log(e_upd) * 1.44269504) - 2.0);
        scaled     = (err / q_step) * drv_pre;
        nib        = aa.clip(-8.0, 7.0, floor(scaled + 0.5));
        q_out      = pred + nib * q_step * drv_post;
        pcm16      = int(x_c * 4096.0) * 0.000244140625;
        dec_upd    = aa.clip(-1.20, 1.20, pcm16 + adpcm_blend * (q_out - pcm16));

        next_p1    = ba.if(tk > 0.5, dec_upd, p1);
        next_p2    = ba.if(tk > 0.5, p1,      p2);
        next_env   = ba.if(tk > 0.5, e_upd,   env);
        next_h0    = ba.if(tk > 0.5, h1,      h0);
        next_h1    = ba.if(tk > 0.5, h2,      h1);
        next_h2    = ba.if(tk > 0.5, h3,      h2);
        next_h3    = ba.if(tk > 0.5, dec_upd, h3);

        interp_out = spu_interp(next_h0, next_h1, next_h2, next_h3, next_ph);
    };

spu_voice(r_step, x) = (spu_voice_step(r_step, x) ~ (_, _, _, _, _, _, _, _)) : !, !, !, !, !, !, !, !, _;

// ---- Per-Channel SPU Voice Pipeline ----
spu_channel(r_step, x_st) =
    zm_ema(k_post,
        spu_voice(
            r_step,
            zm_ema(k_pre, zm_ema(k_pre, x_st))
        )
    ) * out_gain;

// ---- 512 KB RAM-Budget Mid/Side Stereo Width + Dual-Voice Pitch-Register Spread ----
// At Width=0: collapses cleanly to mono with identical L/R voice clocks.
// At Width>0: preserves Mid/Side stereo AND applies subtle opposing L/R SPU clock
// micro-spread via Loop 1 Q31 LFO so even mono guitar inputs gain dual-voice stereo width.
width_mod_amt = rate_step * width_knob * 0.008;
rate_base     = min(1.0 - width_mod_amt, rate_step);
width_delta   = zm_osc(0.31) * width_mod_amt;
rate_l        = rate_base + width_delta;
rate_r        = rate_base - width_delta;

process(xL, xR) = spu_channel(rate_l, st_l), spu_channel(rate_r, st_r)
    with {
        mid_s  = 0.5 * (xL + xR);
        side_s = 0.5 * (xL - xR) * width_knob;
        st_l   = (mid_s + side_s) * 2.0;
        st_r   = (mid_s - side_s) * 2.0;
    };
