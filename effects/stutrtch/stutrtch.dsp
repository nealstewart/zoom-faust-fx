// effects/stutrtch/stutrtch.dsp
//
// Momentary/Auto 3s SDRAM Stutter & Randomized Ratchet Looper with Independent
// Random SRR, BRR, Driven Analog 2-Pole LPF, and Clean 2-Pole HPF — written in
// standard FAUST and compiled to Zoom MS-70CDR+ (.ZD2 + .ZIC) via:
//   python tools/faust2zd2.py effects/stutrtch/stutrtch.dsp --build
//
// DSP Architecture (Mono-In -> Stereo-Out, 44.1 kHz, 131,072-float SDRAM ring):
//   1. Footswitch-Linked Capture Mode (via trailing `ON/OFF` LATCH / UNLATCH):
//      - `UNLATCH` (default): Momentary RETRO grab — continuously records in the
//        background; press & hold to instantly replay/stutter what just happened.
//      - `LATCH`: Hands-free AUTO mode — while latched ON, watches for vocal or
//        instrument transients (`an.amp_follower_ud`), forward-captures `Slice` ms,
//        and stutters 4 repeats before re-arming.
//   2. Independent Random Sources (`ba.latch` on decorrelated noise streams):
//      - `Ratch`: random subdivisions (`1/2x`, `1x`, `2x`, `3x`, `4x`, `6x`, `8x`)
//                 + peak-normalized stereo scatter (`0` = off).
//      - `SRR`:   random ZOH sample-rate reduction down to ~1.5 kHz (`0` = off).
//      - `BRR`:   random bit-depth quantization down to ~2.5-bit (`0` = off).
//      - `RndMd`: re-roll on `STOMP`, every `SLICE` wrap, or `BOTH`.
//   3. Dual Filter Section (Driven Analog LPF -> Clean HPF):
//      - `LPF`: pedal-level (-18 dBFS / 0.25 RMS) unity-calibrated OTA soft
//        saturation (`zm_soft_clip(x * 2.5) * 0.40`) into a 2-pole resonant SVF
//        (`350 Hz .. 16 kHz`, Q = 0.87; defaults to `100` open).
//      - `HPF`: clean, undriven 2-pole Butterworth HPF (`40 Hz .. 3,500 Hz`,
//        Q = 0.7071) placed after the LPF (defaults to `0` = bypassed).
//   4. Unity-Calibrated Gain Staging:
//      - `Level` (`stock_vol`, `80` = unity) + `Mix` (`x + mix * (wet - x)`).

declare name "Stut Rtch";
declare zd2_filename "XXSTUTRT";
declare zd2_id "070011d0";
declare version "2.00";
declare zd2_display "STUT RTCH";
declare zd2_badge "SR";
declare description "FAUST 3s SDRAM stutter & random ratchet looper (UNLATCH=retro grab, LATCH=auto transient) with random SRR/BRR, driven analog LPF, and clean HPF.";
declare zd2_dspload "27";
declare zd2_footswitch "unlatch";

import("stdfaust.lib");
import("zoom.lib");

// ---- 9 Pedal Knobs (+ trailing ON/OFF LATCH/UNLATCH defaulting to UNLATCH) ----
slice_knob = hslider("Slice[1][tooltip:Base stutter slice length on an exponential curve (25 ms to 2,000 ms).]", 45, 0, 100, 1) * 0.01;
ratch_knob = hslider("Ratch[2][tooltip:Random ratchet subdivision depth (0 = steady 1x loop, 100 = wide random 1/2x..8x jumps & scatter).]", 35, 0, 100, 1) * 0.01;
rndmd_knob = hslider("RndMd[3][values:STOMP|SLICE|BOTH][tooltip:When random sources re-roll Ratchet, SRR, and BRR: on STOMP, every SLICE wrap, or BOTH.]", 2, 0, 2, 1);
srr_knob   = hslider("SRR[4][tooltip:Random Sample Rate Reduction ceiling (0 = off / 44.1 kHz, 100 = random ZOH down to ~1.5 kHz).]", 40, 0, 100, 1) * 0.01;
brr_knob   = hslider("BRR[5][tooltip:Random Bit Rate Reduction ceiling (0 = off, 100 = random bitcrushing down to ~2.5-bit).]", 35, 0, 100, 1) * 0.01;
lpf_knob   = hslider("LPF[6][tooltip:Driven emulated analog 2-pole resonant LPF cutoff (350 Hz warm dub to 16 kHz open).]", 100, 0, 100, 1) * 0.01;
hpf_knob   = hslider("HPF[7][tooltip:Clean undriven 2-pole Butterworth HPF cutoff after LPF (0 = off, 1..100 = 40 Hz to 3.5 kHz).]", 0, 0, 100, 1) * 0.01;
level_knob = hslider("Level[8][curve:stock_vol][tooltip:Stutter wet output level (80 is unity at nominal MS-70CDR+ bus level).]", 80, 0, 100, 1) : si.smoo;
mix_knob   = hslider("Mix[9][tooltip:Dry vs. stutter wet crossfade when active (0..100%).]", 100, 0, 100, 1) * 0.01 : si.smoo;

// ---- Block-Rate Parameter Scaling ----
// Exponential Slice: 25 ms (1,102 smps) .. 2,000 ms (88,200 smps @ 44.1 kHz)
slice_ms      = 25.0 * (1.0 + 4.0 * slice_knob + 15.0 * slice_knob * slice_knob + 60.0 * slice_knob * slice_knob * slice_knob);
base_win_smps = aa.clip(1102.0, 88200.0, slice_ms * 44.1);

// Exponential Driven Analog LPF cutoff: 350 Hz .. 16,000 Hz
lpf_hz = aa.clip(350.0, 16000.0, 350.0 * (1.0 + 3.0 * lpf_knob + 11.7 * lpf_knob * lpf_knob + 30.0 * lpf_knob * lpf_knob * lpf_knob));

// Exponential Clean HPF cutoff: 40 Hz .. 3,500 Hz (bypassed when hpf_knob <= 0.005)
hpf_hz = aa.clip(40.0, 3500.0, 40.0 * (1.0 + 4.0 * hpf_knob + 17.5 * hpf_knob * hpf_knob + 65.0 * hpf_knob * hpf_knob * hpf_knob));

// RndMd flags
roll_on_stomp = ba.if(rndmd_knob < 0.5, 1.0, ba.if(rndmd_knob > 1.5, 1.0, 0.0));
roll_on_slice = ba.if(rndmd_knob > 0.5, 1.0, 0.0);

// ---- 1. Footswitch-Linked Capture State Machine (UNLATCH=RETRO, LATCH=AUTO) ----
cap_state_step(prev_cnt, fsw, is_unlatch, trans_trig) = next_cnt
    with {
        auto_end   = base_win_smps * 5.0;
        // UNLATCH -> RETRO: freeze immediately while footswitch is held
        retro_next = ba.if(fsw > 0.5, base_win_smps + 2.0, 0.0);
        // LATCH -> AUTO: forward-capture on transient, repeat 4x, then re-arm
        auto_armed = ba.if(prev_cnt > 0.5,
                           ba.if(prev_cnt >= auto_end, 0.0, prev_cnt + 1.0),
                           ba.if(trans_trig > 0.5, 1.0, 0.0));
        auto_next  = ba.if(fsw > 0.5, auto_armed, 0.0);
        next_cnt   = ba.if(is_unlatch > 0.5, retro_next, auto_next);
    };

// ---- 2. ZOH Sample-Rate Reduction Phase Accumulator ----
zoh_phase_step(prev_z, step_inc) =
    ba.if(prev_z + step_inc >= 1.0, prev_z + step_inc - 1.0, prev_z + step_inc);

// ---- 3. Unified 3-Wire Recursive Stutter + Random SRR/BRR + Driven LPF + Clean HPF ----
stut_engine_step(prev_ph, prev_win, prev_loop, mono_in, is_frozen, stomp_edge, n1, n2, n3) =
    next_ph, next_win, next_loop, stut_sig
    with {
        cur_w    = ba.if(prev_win < 441.0, base_win_smps, prev_win);
        wrapped  = (is_frozen > 0.5) & ((prev_ph + 1.0) >= cur_w);
        next_ph  = ba.if(is_frozen > 0.5, ba.if(wrapped, 0.0, prev_ph + 1.0), 0.0);
        do_roll  = ((stomp_edge * roll_on_stomp) + (wrapped * roll_on_slice)) > 0.5;

        // Random Source 1: Ratchet subdivision
        sel_r    = n1 * (1.0 + ratch_knob * 5.5);
        subdiv_r = ba.if(sel_r < 1.0, 1.0,
                   ba.if(sel_r < 2.0, 2.0,
                   ba.if(sel_r < 3.0, 3.0,
                   ba.if(sel_r < 4.0, 4.0,
                   ba.if(sel_r < 5.0, 6.0,
                   ba.if(sel_r < 5.8, 8.0, 0.5))))));
        rolled_w = ba.if(ratch_knob > 0.01,
                         aa.clip(441.0, 128000.0, base_win_smps / subdiv_r),
                         base_win_smps);
        next_win = ba.if(is_frozen < 0.5,
                         base_win_smps,
                         ba.if(do_roll, rolled_w, cur_w));

        // 2.97s Float32 SDRAM Feedback Looper (131,072 samples)
        next_loop = ba.if(is_frozen > 0.5, de.fdelay1(131072, next_win, prev_loop), mono_in);

        // Random Source 2: Sample Rate Reduction (ZOH decimation)
        u_srr    = ba.latch(do_roll, n2);
        srr_div  = 1.0 + (u_srr * u_srr) * (srr_knob * srr_knob) * 27.0;
        srr_step = ba.if(srr_knob > 0.01, 1.0 / srr_div, 1.0);
        z_ph     = (zoh_phase_step(srr_step) ~ _);
        z_trig   = (z_ph < srr_step) | (srr_step >= 0.995);
        s_srr    = ba.if(srr_knob > 0.01, ba.latch(z_trig, next_loop), next_loop);

        // Random Source 3: Bit Rate Reduction (bit-depth quantization)
        u_brr     = ba.latch(do_roll, n3);
        crush_amt = u_brr * brr_knob;
        inv_c     = 1.0 - crush_amt;
        brr_lvls  = 3.0 + (inv_c * inv_c * inv_c) * 125.0;
        s_brr     = ba.if((brr_knob > 0.01) & (crush_amt >= 0.04),
                          int(s_srr * brr_lvls) / brr_lvls,
                          s_srr);

        // Driven Emulated Analog 2-Pole Resonant LPF (Q = 0.87) with pedal-level
        // (-18 dBFS / 0.25 RMS) unity-calibrated OTA soft saturation
        s_ota = zm_soft_clip(s_brr * 2.5) * 0.40;
        s_lpf = fi.svf_lp(lpf_hz, 0.87, s_ota);

        // Clean, Undriven 2-Pole Butterworth HPF (Q = 0.7071, bypassed when HPF = 0)
        s_hpf  = fi.svf_hp(hpf_hz, 0.70710678, s_lpf);
        s_filt = ba.if(hpf_knob > 0.005, s_hpf, s_lpf);

        // Anti-click 24-sample (~0.54 ms) attack & tail fade (full legato sustain)
        env_atk  = aa.clip(0.0, 1.0, next_ph * 0.0416667);
        env_tail = aa.clip(0.0, 1.0, (next_win - next_ph) * 0.0416667);
        stut_sig = s_filt * (env_atk * env_tail) * level_knob;
    };

// ---- 4. Full Stereo Process Graph ----
process(xL, xR) = (xL + mix_knob * (wetL - xL)),
                  (xR + mix_knob * (wetR - xR))
    with {
        mono_in    = 0.5 * (xL + xR);
        fsw        = zm_fsw_active;
        is_unlatch = zm_is_unlatch;

        // Transient detector for LATCH -> AUTO mode
        env_fast   = an.amp_follower_ud(0.030, 0.0005, mono_in);
        env_slow   = an.amp_follower_ud(0.0012, 0.0002, mono_in);
        trans_trig = (env_fast - env_slow) > 0.045;

        // Capture & freeze state (UNLATCH = RETRO, LATCH = AUTO)
        cap_cnt    = (cap_state_step(fsw, is_unlatch, trans_trig) ~ _);
        is_frozen  = cap_cnt > (base_win_smps + 0.5);
        stomp_edge = (is_frozen > 0.5) & ((is_frozen') < 0.5);

        // Three decorrelated noise streams in [0, 1)
        n1 = abs(no.noise);
        n2 = ma.frac(abs(no.noise * 17.31 + zm_osc(3.71)));
        n3 = ma.frac(abs(no.noise * 43.17 + zm_osc(7.13)));

        // Run unified 3-wire recursive stutter engine -> 4th output is stut_sig
        stut_sig = (stut_engine_step(mono_in, is_frozen, stomp_edge, n1, n2, n3) ~ (_, _, _)) : !, !, !, _;

        // Peak-normalized stereo scatter (never exceeds unity) & click-free freeze crossfade
        pan_rnd   = ba.latch(stomp_edge, n1);
        scatter   = (pan_rnd * 2.0 - 1.0) * 0.28 * ratch_knob;
        pan_norm  = 1.0 / (1.0 + abs(scatter));
        pan_l     = (1.0 + scatter) * pan_norm;
        pan_r     = (1.0 - scatter) * pan_norm;
        xfade_env = zm_ema(0.009, ba.if(is_frozen > 0.5, 1.0, 0.0));
        wetL      = xL + xfade_env * (stut_sig * pan_l - xL);
        wetR      = xR + xfade_env * (stut_sig * pan_r - xR);
    };
