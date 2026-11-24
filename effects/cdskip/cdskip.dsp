// effects/cdskip/cdskip.dsp
//
// Authentic Scratched-CD / Failing Discman / Oval-Style Read-Error & Tracking-Skip
// Effect — written in standard FAUST and compiled to Zoom MS-70CDR+ (.ZD2 + .ZIC) via:
//   python tools/faust2zd2.py effects/cdskip/cdskip.dsp --build
//
// DSP Architecture (Mono-In -> Stereo-Out, 44.1 kHz, single 131,072-float SDRAM ring):
//   1. Footswitch-Linked Capture Mode (via trailing `ON/OFF` LATCH / UNLATCH):
//      - `UNLATCH` (default): Momentary RETRO grab — continuously records live audio
//        in the background; press & hold to immediately lock/skip the last `Slice` ms.
//      - `LATCH`: Hands-free AUTO forward-capture on vocal/instrument transients
//        (~2.5 ms attack, ~0.065 spike threshold to reject sub-ms mic/switch clicks).
//   2. Tracking Jump & Laser Slip (`Slice` + `Skip`):
//      - `Slice`: 13.3 ms (1 Red Book CD frame @ 75 Hz = 588 smps) .. 2,000 ms (88,200 smps).
//      - `Skip`: 0 = clean sector lock; higher values jump CD-frame subdivisions
//        (`1x`, `1/2x`, `1/4x`, `1/8x`) and laser-hunt (forward/backward read-head
//        slip across the preserved 2.95 s circulating buffer).
//   3. CIRC / Reed-Solomon Error Concealment Bursts (`Error`):
//      - Intermittent, bursty DAC failures randomly triggered per repeat / mid-slice:
//        * C2 Mute Dropouts (2..35 ms hard cutouts)
//        * Sample-Hold Freezes (3..40 ms metallic ZOH sample-freeze chirps)
//        * PCM Bit-Error Glitches (1..12 ms corrupted-word digital crackle/clicks)
//   4. Splice Hardness (`Click`) & Stereo Desync (`Width`):
//      - `Click`: 100 = authentic 0-sample CD sector jump; 0 = ~3.6 ms anti-click fade.
//      - `Width`: independent L/R micro-offset (0..27 ms) + single-channel dropouts.
//   5. Clean Undriven Filter & Unity Gain Staging:
//      - `LPF`: clean 2-pole Butterworth SVF (`500 Hz .. 16 kHz`, Q = 0.7071, no drive;
//        default `100` completely bypassed/open).
//      - `Level` (`stock_vol`, `80` = unity) + `Mix` (`x + mix * (wet - x)`).

declare name "CDSkip";
declare zd2_filename "XXCDSKIP";
declare zd2_id "070011e0";
declare version "1.00";
declare zd2_display "CD SKIP";
declare zd2_badge "CD";
declare description "Scratched-CD / failing Discman tracking-skip & CIRC read-error burst looper (UNLATCH=retro grab, LATCH=auto transient) with laser-hunt, stereo desync, and clean Butterworth LPF.";
declare zd2_dspload "27";
declare zd2_footswitch "unlatch";

import("stdfaust.lib");
import("zoom.lib");

// ---- 9 Pedal Knobs (+ trailing ON/OFF LATCH/UNLATCH defaulting to UNLATCH) ----
slice_knob = hslider("Slice[1][tooltip:Base CD sector slice length on an exponential curve (13.3 ms = 1 CD frame to 2,000 ms).]", 40, 0, 100, 1) * 0.01;
skip_knob  = hslider("Skip[2][tooltip:Tracking jump & laser-hunt depth (0 = locked sector, 100 = random 1x..1/8x subdivisions & read-head slip).]", 55, 0, 100, 1) * 0.01;
error_knob = hslider("Error[3][tooltip:CIRC / Reed-Solomon error concealment burst rate (C2 mute dropouts, ZOH sample-freeze chirps, PCM bit-error crackle).]", 50, 0, 100, 1) * 0.01;
rndmd_knob = hslider("RndMd[4][values:STOMP|SLICE|BOTH][tooltip:When tracking jumps, laser slip, and error bursts re-roll: on STOMP, every SLICE wrap, or BOTH.]", 2, 0, 2, 1);
click_knob = hslider("Click[5][tooltip:Sector splice hardness (100 = razor-sharp 0-sample CD jump, 0 = smooth 3.6 ms anti-click fade).]", 75, 0, 100, 1) * 0.01;
width_knob = hslider("Width[6][tooltip:Stereo desync (independent L/R micro-offset and occasional single-channel C2 dropouts).]", 40, 0, 100, 1) * 0.01;
lpf_knob   = hslider("LPF[7][tooltip:Clean undriven 2-pole Butterworth LPF cutoff (500 Hz to 16 kHz; 100 = completely open/bypassed).]", 100, 0, 100, 1) * 0.01;
level_knob = hslider("Level[8][curve:stock_vol][tooltip:Wet output level (80 is unity at nominal MS-70CDR+ bus level).]", 80, 0, 100, 1) : si.smoo;
mix_knob   = hslider("Mix[9][tooltip:Dry vs. wet crossfade when active (0..100%).]", 100, 0, 100, 1) * 0.01 : si.smoo;

// ---- Block-Rate Parameter Scaling ----
// Exponential Slice: 13.333 ms (588 smps = 1 Red Book CD frame @ 75 Hz) .. 2,000 ms (88,200 smps)
slice_ms      = 13.333333 * (1.0 + 4.0 * slice_knob + 25.0 * slice_knob * slice_knob + 120.0 * slice_knob * slice_knob * slice_knob);
base_win_smps = aa.clip(588.0, 88200.0, slice_ms * 44.1);

// Exponential Clean Butterworth LPF cutoff: 500 Hz .. 16,000 Hz (bypassed when LPF = 100)
lpf_hz = aa.clip(500.0, 16000.0, 500.0 * (1.0 + 3.0 * lpf_knob + 10.0 * lpf_knob * lpf_knob + 18.0 * lpf_knob * lpf_knob * lpf_knob));

// Splice anti-click ramp: 0 smps at Click=100, 160 smps (~3.63 ms) at Click=0
fade_smps = (1.0 - click_knob) * 160.0;
inv_fade  = 1.0 / max(1.0, fade_smps);

// RndMd flags
roll_on_stomp = ba.if(rndmd_knob < 0.5, 1.0, ba.if(rndmd_knob > 1.5, 1.0, 0.0));
roll_on_slice = ba.if(rndmd_knob > 0.5, 1.0, 0.0);

// ---- 1. Footswitch-Linked Capture State Machine (UNLATCH=RETRO, LATCH=AUTO) ----
cap_state_step(prev_cnt, fsw, is_unlatch, trans_trig) = next_cnt
    with {
        auto_end   = base_win_smps * 5.0;
        // UNLATCH -> RETRO: freeze immediately while footswitch is held
        retro_next = ba.if(fsw > 0.5, base_win_smps + 2.0, 0.0);
        // LATCH -> AUTO: forward-capture on transient, repeat ~4x, then re-arm
        auto_armed = ba.if(prev_cnt > 0.5,
                           ba.if(prev_cnt >= auto_end, 0.0, prev_cnt + 1.0),
                           ba.if(trans_trig > 0.5, 1.0, 0.0));
        auto_next  = ba.if(fsw > 0.5, auto_armed, 0.0);
        next_cnt   = ba.if(is_unlatch > 0.5, retro_next, auto_next);
    };

// ---- 2. Unified 4-Wire Recursive CD-Skip Engine ----
// Circulates the full 2.95 s (130,000-sample) audio history inside a single
// 131,072-float SDRAM ring buffer (`dline_0`), allowing true forward/backward
// laser-hunting and stereo micro-offset reads without overwriting captured audio.
cdskip_engine_step(prev_ph, prev_win, prev_rd, prev_loop, xL, xR, mono_in, is_frozen, stomp_edge, r1, r2, r3, r4, r5) =
    next_ph, next_win, next_rd, next_loop, fin_L, fin_R
    with {
        cur_w   = ba.if(prev_win < 73.0, base_win_smps, prev_win);
        wrapped = (is_frozen > 0.5) & ((prev_ph + 1.0) >= cur_w);
        next_ph = ba.if(is_frozen > 0.5, ba.if(wrapped, 0.0, prev_ph + 1.0), 0.0);
        do_roll = ((stomp_edge * roll_on_stomp) + (wrapped * roll_on_slice)) > 0.5;

        // Random Source 1 (r1): CD-frame subdivision (1x, 1/2x, 1/4x, 1/8x)
        sel_s    = r1 * (1.0 + skip_knob * 3.2);
        subdiv_s = ba.if(sel_s < 1.0, 1.0,
                   ba.if(sel_s < 2.0, 2.0,
                   ba.if(sel_s < 3.0, 4.0, 8.0)));
        rolled_w = ba.if(skip_knob > 0.01,
                         aa.clip(73.5, 88200.0, base_win_smps / subdiv_s),
                         base_win_smps);
        next_win = ba.if(is_frozen < 0.5,
                         base_win_smps,
                         ba.if(do_roll, rolled_w, cur_w));

        // Random Source 2 (r2): Laser-hunt read-head slip (forward/backward across buffer)
        slip_prob = skip_knob * 0.75;
        slip_u    = r2 / max(0.01, slip_prob);
        slip_dir  = slip_u * 2.4 - 1.5; // [-1.5..+0.9]: biased forward with backward jumps
        slip_smps = ba.if((skip_knob > 0.01) & (r2 < slip_prob),
                          slip_dir * cur_w * (0.35 + 0.65 * skip_knob),
                          0.0);
        raw_rd    = prev_rd + cur_w + ba.if(do_roll, slip_smps, 0.0);
        rd_hi     = ba.if(raw_rd >= 130001.0, raw_rd - 130001.0, raw_rd);
        rd_wrap   = ba.if(rd_hi < 1.0, rd_hi + 130000.0, rd_hi);
        next_rd   = ba.if(is_frozen < 0.5,
                          base_win_smps,
                          ba.if(stomp_edge > 0.5,
                                next_win,
                                ba.if(wrapped, rd_wrap, prev_rd)));

        // Random Source 4 (r4): Stereo desync micro-offset (0..27 ms) & single-channel drop
        stereo_off = ba.latch(do_roll, r4) * 1200.0 * width_knob;
        rd_r_raw   = next_rd + stereo_off;
        rd_r       = ba.if(rd_r_raw >= 130001.0, rd_r_raw - 130001.0, rd_r_raw);

        // Single 131,072-float SDRAM ring buffer (3 integer reads share dline_0)
        recirc    = de.delay(131072, 130000.0, prev_loop);
        raw_L     = de.delay(131072, next_rd, prev_loop);
        raw_R     = de.delay(131072, rd_r, prev_loop);
        next_loop = ba.if(is_frozen > 0.5, recirc, mono_in);

        // Random Source 3 & 5 (r3, r5): CIRC / Reed-Solomon Error Concealment Bursts
        // Mode 0 = clean, 1 = C2 mute dropout (2..35 ms),
        // Mode 2 = ZOH sample-hold freeze (3..40 ms), 3 = PCM bit-error crackle (1..12 ms)
        err_gate  = error_knob * 0.85;
        err_u     = r3 / max(0.01, err_gate);
        rolled_em = ba.if((error_knob > 0.01) & (r3 < err_gate),
                          ba.if(err_u < 0.34, 1.0,
                          ba.if(err_u < 0.67, 2.0, 3.0)),
                          0.0);
        err_mode  = ba.latch(do_roll, rolled_em);

        // Burst duration & start offset inside slice
        dur_scale = 0.35 + 0.65 * error_knob;
        rolled_du = ba.if(rolled_em < 1.5, 88.0 + r4 * 1455.0 * dur_scale,
                    ba.if(rolled_em < 2.5, 132.0 + r4 * 1632.0 * dur_scale,
                                           44.0 + r4 * 485.0 * dur_scale));
        max_st    = max(0.0, next_win - rolled_du);
        rolled_st = r5 * max_st;
        err_st    = ba.latch(do_roll, rolled_st);
        err_en    = ba.latch(do_roll, rolled_st + rolled_du);
        in_burst  = (err_mode > 0.5) & (next_ph >= err_st) & (next_ph < err_en);

        // Mode 1: C2 Mute Dropout (with Width-controlled single-channel dropouts)
        drop_sel  = ba.latch(do_roll, r2);
        is_c2     = (in_burst > 0.5) & (err_mode < 1.5);
        mute_L    = ba.if(is_c2 > 0.5,
                          ba.if((width_knob > 0.05) & (drop_sel < width_knob * 0.35), 1.0, 0.0),
                          1.0);
        mute_R    = ba.if(is_c2 > 0.5,
                          ba.if((width_knob > 0.05) & (drop_sel >= width_knob * 0.35) & (drop_sel < width_knob * 0.70), 1.0, 0.0),
                          1.0);

        // Mode 2: Sample-Hold Freeze (metallic ZOH chirp, 920 Hz..4.4 kHz hold rate)
        rolled_zr = 1.0 / (10.0 + r5 * 38.0);
        zoh_rate  = ba.latch(do_roll, rolled_zr);
        is_zoh    = (in_burst > 0.5) & (err_mode > 1.5) & (err_mode < 2.5);
        zoh_trig  = (is_zoh < 0.5) | (ma.frac(next_ph * zoh_rate) < zoh_rate);
        sig_z_L   = ba.latch(zoh_trig, raw_L);
        sig_z_R   = ba.latch(zoh_trig, raw_R);

        // Mode 3: PCM Bit-Error Glitch (corrupted 2's-complement MSB sign flips & word drops, <= unity)
        is_pcm    = (in_burst > 0.5) & (err_mode > 2.5);
        bit_h     = ma.frac(next_ph * 0.38196601 + r5);
        pcm_L     = ba.if(bit_h > 0.78, -sig_z_L,
                    ba.if(bit_h > 0.52, int(sig_z_L * 4.0) * 0.25,
                    ba.if(bit_h < 0.14, 0.0, sig_z_L)));
        pcm_R     = ba.if(bit_h > 0.76, -sig_z_R,
                    ba.if(bit_h > 0.50, int(sig_z_R * 4.0) * 0.25,
                    ba.if(bit_h < 0.14, 0.0, sig_z_R)));

        // Apply error concealment & mute dropouts
        err_L     = ba.if(is_pcm > 0.5, pcm_L, sig_z_L) * mute_L;
        err_R     = ba.if(is_pcm > 0.5, pcm_R, sig_z_R) * mute_R;

        // Splice hardness envelope (Click=100 -> 0-sample hard jump; Click=0 -> 3.6 ms fade)
        env_atk   = aa.clip(0.0, 1.0, (next_ph + 1.0) * inv_fade);
        env_tail  = aa.clip(0.0, 1.0, (next_win - next_ph) * inv_fade);
        env_sp    = ba.if(click_knob > 0.995, 1.0, env_atk * env_tail);
        sp_L      = err_L * env_sp;
        sp_R      = err_R * env_sp;

        // Clean, undriven 2-pole Butterworth LPF (Q = 0.70710678; bypassed at LPF = 100)
        lpf_L     = fi.svf_lp(lpf_hz, 0.70710678, sp_L);
        lpf_R     = fi.svf_lp(lpf_hz, 0.70710678, sp_R);
        filt_L    = ba.if(lpf_knob < 0.995, lpf_L, sp_L);
        filt_R    = ba.if(lpf_knob < 0.995, lpf_R, sp_R);

        out_L     = filt_L * level_knob;
        out_R     = filt_R * level_knob;

        // Smooth footswitch engage/release crossfade (~2.5 ms) & dry/wet Mix
        xfade_env = zm_ema(0.009, ba.if(is_frozen > 0.5, 1.0, 0.0));
        wetL      = xL + xfade_env * (out_L - xL);
        wetR      = xR + xfade_env * (out_R - xR);
        fin_L     = xL + mix_knob * (wetL - xL);
        fin_R     = xR + mix_knob * (wetR - xR);
    };

// ---- 3. Full Stereo Process Graph ----
process(xL, xR) = (cdskip_engine_step(xL, xR, mono_in, is_frozen, stomp_edge, r1, r2, r3, r4, r5) ~ (_, _, _, _))
    with {
        mono_in    = 0.5 * (xL + xR);
        fsw        = zm_fsw_active;
        is_unlatch = zm_is_unlatch;

        // Transient detector for LATCH -> AUTO mode (~2.5 ms attack, ~0.065 threshold)
        env_fast   = an.amp_follower_ud(0.009, 0.0005, mono_in);
        env_slow   = an.amp_follower_ud(0.0012, 0.0002, mono_in);
        trans_trig = (env_fast - env_slow) > 0.065;

        // Capture & freeze state (UNLATCH = RETRO, LATCH = AUTO)
        cap_cnt    = (cap_state_step(fsw, is_unlatch, trans_trig) ~ _);
        is_frozen  = cap_cnt > (base_win_smps + 0.5);
        stomp_edge = (is_frozen > 0.5) & ((is_frozen') < 0.5);

        // Five decorrelated random streams in [0, 1) (hoisted to block rate)
        u0 = abs(no.noise);
        r1 = u0;
        r2 = ma.frac(u0 * 17.317 + 0.137);
        r3 = ma.frac(u0 * 43.193 + 0.419);
        r4 = ma.frac(u0 * 79.731 + 0.713);
        r5 = ma.frac(u0 * 131.37 + 0.293);
    };
