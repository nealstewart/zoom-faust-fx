// effects/wtdelay/wtdelay.dsp
//
// Live Wavetable-Capture Delay ("WT DELAY") — written in standard FAUST
// and compiled to a Zoom MS-70CDR+ (.ZD2 + .ZIC) package via:
//   zoom-faust-fx build effects/wtdelay --out effects/wtdelay/gen
//
// DSP Architecture (Stereo-In -> Stereo-Out, Mix=100% for pure stereo send):
//   1. Live Wavetable Capture (64-Frame Circular FIFO in SDRAM):
//      - Continuously slices gated incoming audio into 256-sample single-cycle
//        frames (~5.8 ms) stored in a 64-frame (16,384-sample) circular FIFO
//        (`rwtable`, 64 KB SDRAM shared by both stereo readers).
//      - Auto-calibrated input amplitude gate (-42 dBFS) prevents silence from
//        overwriting captured frames; the active write slot is staged outside
//        the 63-frame playback window so committed timbres are never corrupted.
//      - Tracks captured frame count so echoes never read empty zero frames,
//        even on the very first note.
//   2. ZOH Wavetable Playback & Rhythmic History Modulation:
//      - Dual stereo wavetable readers loop historical frames at controllable
//        `Pitch` (-24..+24 st) using Zero-Order Hold (integer phase readout)
//        to preserve raw, gritty early-digital wavetable aliasing.
//      - `Sync` + `Time` set the delay time and rhythmic history scan (either
//        FREE 40..1200 ms, or beat-synced 1/16..1/4. at 60..200 BPM).
//      - Frame-index changes are latched strictly on each oscillator's phase
//        reset (zero-crossing of the phasor) for click-free frame jumps.
//   3. Brief Modulated Wavetable Note -> Stereo Audio Feedback Delay:
//      - Each incoming note's snappy amplitude envelope gates the modulated ZOH
//        wavetable into a brief, morphing stereo note (`wt_note_l/r`), shaped
//        by `Tone` (2-pole LPF).
//      - That brief modulated wavetable note is pushed directly into a real
//        stereo audio feedback delay (`2 x de.delay(32768)`, 256 KB SDRAM,
//        up to 700 ms; 1.0x L / 0.75x R with warm feedback damping & cross-bleed).
//      - `Decay` sets delay feedback repeats; `Mix` blends dry/wet (100% = send).

declare name "WT Delay";
declare zd2_filename "XXWTDLY";
declare zd2_id "07001270";
declare version "1.00";
declare zd2_display "WT DELAY";
declare zd2_badge "WT";
declare description "Live wavetable-capture stereo delay with gated 64-frame FIFO, ZOH playback, BPM-synced delayed echo trails, and dry/wet mix.";
declare zd2_dspload "28";

import("stdfaust.lib");
import("zoom.lib");

// ---- 9 Pedal Knobs ----
rate_knob  = hslider("Rate[1][tooltip:Frame capture rate into the 64-frame FIFO (0 = slow ~370 ms snapshots, 100 = continuous 5.8 ms slices).]", 72, 0, 100, 1) * 0.01;
pitch_st   = hslider("Pitch[2][unit:st][tooltip:Wavetable playback pitch in semitones (-24 to +24 st; 0 = native 1x speed).]", 0, -24, 24, 0.5);
time_click = hslider("Time[3][time_sync:40..700][tooltip:Delay time: 40..700 ms (clicks 0..660), then 1/16..1/4. synced to pedal BPM (665 = 1/8.).]", 665, 0, 667, 1);
depth_knob = hslider("Depth[4][tooltip:Wavetable frame-scan drift around each echo's sampled frame (0 = exact sampled frame, 100 = wide history scan).]", 25, 0, 100, 1) * 0.01;
mode_sel   = hslider("Mode[5][values:SAW|STEP|TRI|RND][tooltip:History scan shape (SAW = backward trail sweep, STEP = 4-step rhythmic, TRI = ping-pong drift, RND = random scatter).]", 0, 0, 3, 1);
decay_knob = hslider("Decay[6][tooltip:Echo repeat feedback / decay (0 = single brief echo, 100 = long fading wavetable delay trails).]", 48, 0, 100, 1) * 0.01;
tone_knob  = hslider("Tone[7][tooltip:Wet wavetable low-pass filter cutoff (warm dark pad to raw ZOH aliasing).]", 65, 0, 100, 1) * 0.01;
mix_knob   = hslider("Mix[8][tooltip:Dry/Wet delay mix (0 = dry, 50 = balanced blend, 100 = 100% wet stereo send).]", 55, 0, 100, 1) * 0.01;
level_knob = hslider("Level[9][curve:stock_vol][tooltip:Master stereo output level (80 is unity).]", 80, 0, 100, 1) : zm_ema(0.005);

// ---- Constants (Power-of-Two Sizes for Mask-Only Indexing) ----
FRAME_LEN   = 256;
FRAME_MASK  = 255;
NUM_FRAMES  = 64;
FRAMES_MASK = 63;
TABLE_LEN   = 16384;   // 64 frames * 256 samples = 16,384 floats (64 KB SDRAM)
TABLE_MASK  = 16383;
MAX_HIST    = 62.0;    // 63 committed frames (0..62); 1 staging frame reserved for live writes

// ---- Helpers: 4-Way & 8-Way Selectors ----
sel4(m, v0, v1, v2, v3) =
    ba.if(m < 0.5, v0,
    ba.if(m < 1.5, v1,
    ba.if(m < 2.5, v2, v3)));

sel8(m, v0, v1, v2, v3, v4, v5, v6, v7) =
    ba.if(m < 0.5, v0,
    ba.if(m < 1.5, v1,
    ba.if(m < 2.5, v2,
    ba.if(m < 3.5, v3,
    ba.if(m < 4.5, v4,
    ba.if(m < 5.5, v5,
    ba.if(m < 6.5, v6, v7)))))));

// ---- Block-Rate Parameter Scaling ----
// Capture wait period between frames: 0 samples (back-to-back) up to 16,384 samples (~371 ms)
inv_rate   = 1.0 - rate_knob;
cap_period = inv_rate * inv_rate * 16384.0;

// Playback phase increment in samples/sample (0.25x .. 4.0x)
phase_inc_l = pow(2.0, pitch_st * 0.083333333);
phase_inc_r = phase_inc_l * 1.004;   // subtle stereo micro-detune

// Single-knob Zoom-style Time: 0..660 = 40..700 ms, 661..667 = 1/16..1/4. @ zm_bpm
delay_ms   = zm_time_sync_ms(time_click, 40.0, 700.0, zm_bpm);
delay_smps = int(aa.clip(1764.0, 30870.0, delay_ms * 44.1));

// History scan LFO (modulates each brief wavetable note across captured frames)
scan_hz  = 1000.0 / max(40.0, delay_ms);
scan_inc = scan_hz * 0.000022675737;

// History scan modulation depth across captured frames (0 .. 32 frames)
depth_frames = depth_knob * 32.0;

// Echo envelope feedback gain (0.15 = 1-2 short echoes .. 0.88 = long fading trails)
fb_gain = 0.15 + decay_knob * 0.73;

// Auto-calibrated amplitude gate threshold (-42 dBFS on Zoom 0.25 RMS nominal bus)
gate_thresh = 0.008;

// 2-pole LPF alpha range (0.045 dark ~320 Hz .. 0.88 bright ~14 kHz)
tone_alpha_base = 0.045 + tone_knob * tone_knob * 0.835;

// Equal-power dry/wet blend (Mix = 100% -> pure stereo send)
dry_gain = sqrt(max(0.0, 1.0 - mix_knob));
wet_gain = sqrt(max(0.0, mix_knob));

// ---- 1. Compact Gated 64-Frame Circular FIFO Capture State Machine ----
// Uses only 2 state variables (`prev_pos` and `prev_head`):
//   - `prev_pos <= 0.0`: IDLE / waiting (`wait = -prev_pos`)
//   - `prev_pos in [1.0, 256.0]`: CAPTURING sample `wsmp = prev_pos - 1.0`
//   - `prev_head` counts 0..63 on first fill, then wraps inside 64..127 so
//     `avail_hist = min(62, max(0, prev_head - 1))` prevents reading empty frames.
cap_fifo_step(gate_open, prev_pos, prev_head) =
    next_pos, next_head, widx, edge_win, committed_head, avail_hist
    with {
        is_cap    = prev_pos > 0.5;
        wait_cnt  = -prev_pos;
        start_now = (is_cap == 0) & (gate_open > 0.5) & (wait_cnt >= cap_period);
        at_end    = is_cap & (prev_pos >= 256.0);

        cur_wsmp  = ba.if(is_cap, prev_pos - 1.0, 0.0);
        widx      = ((int(prev_head) & FRAMES_MASK) * FRAME_LEN + (int(cur_wsmp) & FRAME_MASK)) & TABLE_MASK;

        // 4-sample edge micro-taper so sample 0 and 255 meet cleanly at 0.0
        edge_win  = min(1.0, cur_wsmp * 0.25) * min(1.0, (255.0 - cur_wsmp) * 0.25);

        // Newest fully committed frame is immediately behind the staging slot
        committed_head = (int(prev_head) + 63) & FRAMES_MASK;
        avail_hist     = min(MAX_HIST, max(0.0, prev_head - 1.0));

        next_pos  = ba.if(is_cap,
                          ba.if(at_end, 0.0, prev_pos + 1.0),
                          ba.if(start_now, 1.0, 0.0 - min(cap_period, wait_cnt + 1.0)));
        head_inc  = prev_head + 1.0;
        next_head = ba.if(at_end,
                          ba.if(head_inc >= 128.0, 64.0, head_inc),
                          prev_head);
    };

// ---- 2. Wavetable Playback Phasor (0 .. 256) with Zero-Crossing Reset ----
wt_phasor(inc) = ph, phase_reset
    with {
        step(prev)  = ba.if(prev + inc >= 256.0, prev + inc - 256.0, prev + inc);
        ph          = step ~ _;
        phase_reset = ph < inc;
    };

// ---- 3. Frame-History Scan Modulator (0.0 .. 1.0) ----
scan_mod(ph, rnd_src) = sel4(mode_sel, saw_m, step_m, tri_m, rnd_m)
    with {
        saw_m     = ph;
        step_m    = float(int(ph * 4.0)) * 0.25;
        tri_m     = 1.0 - abs(2.0 * ph - 1.0);
        step_trig = ph < scan_inc;
        rnd_m     = ba.latch(step_trig, rnd_src);
    };

// ---- 4. Single-Channel ZOH Wavetable Reader (Phase-Reset-Synced Frame Index) ----
wt_voice(widx, wsig, committed_head, avail_hist, inc, mod_norm) =
    rwtable(TABLE_LEN, 0.0, widx, wsig, ridx)
    with {
        ph_pair       = wt_phasor(inc);
        ph            = ph_pair : _, !;
        phase_reset   = ph_pair : !, _;

        // Target historical frame index clamped to actually captured frames
        offset_frames = int(aa.clip(0.0, avail_hist, mod_norm * depth_frames));
        target_frame  = (committed_head + NUM_FRAMES - offset_frames) & FRAMES_MASK;

        // Sync frame-index changes strictly to the phasor's zero-crossing reset
        latched_frame = int(ba.latch(phase_reset, float(target_frame))) & FRAMES_MASK;

        // Zero-Order Hold (integer phase readout) for raw, gritty wavetable aliasing
        zoh_idx       = int(ph) & FRAME_MASK;
        ridx          = (latched_frame * FRAME_LEN + zoh_idx) & TABLE_MASK;
    };

// ---- 5. Peak-Attack / Exponential-Release Envelope Follower (1 State Variable) ----
env_follow(rel, x) = (step ~ _)
    with {
        step(prev) = max(abs(x), prev * rel);
    };

// ---- 6. Full Stereo Process (Mix = 100% for pure stereo send) ----
process(xL, xR) = (xL * dry_gain + wetL * wet_gain) * level_knob,
                  (xR * dry_gain + wetR * wet_gain) * level_knob
    with {
        // Mono input sum with 1-pole DC blocker (centers single-cycle frames)
        mono_raw = 0.5 * (xL + xR);
        mono_in  = mono_raw - zm_ema(0.0015, mono_raw);

        // Ripple-free transient / note-onset detector: fires ONCE per new note attack
        // (zero re-triggers on steady held tones), launching a brief ~75 ms wavetable pluck
        in_env    = zm_ema(0.004, abs(mono_in));          // ~5.6 ms smooth envelope
        env_slow  = zm_ema(0.0004, in_env);               // ~56 ms baseline
        gate_open = in_env > gate_thresh;
        onset     = (in_env - env_slow) > 0.018;

        // One-shot brief percussive burst (~55 ms solid, then sharply gated off to 0.0)
        pluck_step(trig, prev) = ba.if((trig > 0.5) & (prev < 0.15), 1.0, prev * 0.9993);
        brief_env  = pluck_step(onset) ~ _;
        burst_gate = aa.clip(0.0, 1.0, (brief_env - 0.35) * 10.0);

        // Run gated 64-frame FIFO capture engine
        fifo_out       = (cap_fifo_step(gate_open) ~ (_, _)) : !, !, _, _, _, _;
        widx           = fifo_out : _, !, !, !;
        edge_win       = fifo_out : !, _, !, !;
        committed_head = fifo_out : !, !, _, !;
        avail_hist     = fifo_out : !, !, !, _;

        // Soft-clipped, edge-tapered write sample
        wsig = zm_soft_clip(mono_in) * edge_win;

        // Wavetable history modulation (shared random latch + stereo offset saves state)
        scan_step(p) = ba.if(p + scan_inc >= 1.0, p + scan_inc - 1.0, p + scan_inc);
        scan_ph_l    = scan_step ~ _;
        rnd_l        = abs(no.noise);
        mod_l        = scan_mod(scan_ph_l, rnd_l);
        mod_r        = ma.frac(mod_l + 0.25);

        // Dual stereo ZOH wavetable readers scanning backward from live captured frames
        raw_l = wt_voice(widx, wsig, committed_head, avail_hist, phase_inc_l, mod_l);
        raw_r = wt_voice(widx, wsig, committed_head, avail_hist, phase_inc_r, mod_r);

        // Crisp, gated wavetable note burst pushed into the stereo audio delay
        wt_note_l = zm_soft_clip((raw_l : zm_ema(tone_alpha_base)) * burst_gate * 1.45);
        wt_note_r = zm_soft_clip((raw_r : zm_ema(tone_alpha_base)) * burst_gate * 1.45);

        // Normal stereo audio feedback delay (2 x de.delay(32768) = 256 KB SDRAM)
        damp_a = aa.clip(0.18, 0.90, 0.25 + tone_knob * 0.65);
        dly_step(inL, inR, fbL, fbR) = wL, wR, dL, dR
            with {
                dL = de.delay(32768, delay_smps, fbL) : zm_ema(damp_a);
                dR = de.delay(32768, delay_smps, fbR) : zm_ema(damp_a);
                wL = zm_soft_clip(inL + dL * fb_gain);
                wR = zm_soft_clip(inR + dR * fb_gain);
            };
        dly_out = (dly_step(wt_note_l, wt_note_r) ~ (_, _)) : !, !, _, _;
        dly_l   = dly_out : _, !;
        dly_r   = dly_out : !, _;

        // Snappy output gate on the delayed repeats: cuts off quiet tails cleanly
        // so echoes stay tight, staccato, and rhythmic
        wet_env  = zm_ema(0.008, max(abs(dly_l), abs(dly_r)));
        out_gate = aa.clip(0.0, 1.0, (wet_env - 0.025) * 40.0);
        wetL     = dly_l * out_gate;
        wetR     = dly_r * out_gate;
    };
