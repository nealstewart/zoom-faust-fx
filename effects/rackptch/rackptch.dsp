// effects/se50pitch/se50pitch.dsp
//
// Boss SE-50 Alg.12 Inspired 3-Voice Dual-Tap Pitch Shifter with 32 kHz / 12-Bit
// Rack DAC, Stereo Spread/Detune, and Feedback implemented in FAUST and compiled
// to a Zoom MS-70CDR+ (.ZD2 + .ZIC) package via:
//   python tools/faust2zd2.py effects/se50pitch/se50pitch.dsp --build
//
// DSP Architecture (Stereo In/Out — Pair After FaustVFE Vocal Front-End):
//   1. Feedback Injection + ~135 Hz Feedback Anti-Mud HPF (`fi.highpass(1, 135)`),
//      plus zero-state [0.25, 0.50, 0.25] binomial Nyquist damper stored in
//      shared 4096-sample external SDRAM ring buffers (`de.fdelay1(4096, d, sig)`).
//   2. 3 Parallel Dual-Tap Pitch Voices (-24..+24 semitones):
//      - V1 (Left-leaning,   -8 cent micro-detune,  0 ms pre-delay, phase 0.000)
//      - V2 (Center,          0 cent detune,        7 ms pre-delay, phase 0.333)
//      - V3 (Right-leaning,  +8 cent micro-detune, 14 ms pre-delay, phase 0.667)
//      - 34.0 ms (1500-sample) grain window with 20% smoothstep Tukey Hermite
//        crossfading (`3t^2 - 2t^3`) and +-1 cent 1/f Brownian chorus humanize.
//   3. Vintage 1990 Boss SE-50 32 kHz / 12-Bit Rack DAC:
//      - Anti-aliasing pre-filter (`si.smooth(0.38)`) -> 32 kHz zero-order-hold
//        clock (`SE50_DAC_STEP = 32000 / 44100 = 0.72562358`, `ba.latch`) with
//        cubic soft clipping and 12-bit signed PCM quantization -> reconstruction
//        filter (`si.smooth(0.42)`), tapped for the soft-limited pitch feedback loop.

declare name "RackPtch";
declare zd2_filename "XXRCKPTC";
declare zd2_id "07001110";
declare version "1.00";
declare zd2_display "RACK PTC";
declare zd2_badge "RP";
declare description "3-voice dual-tap stereo micro-pitch shifter with 32 kHz / 12-bit rack DAC character and feedback.";
declare zd2_dspload "22";

import("stdfaust.lib");
import("zoom.lib");

// ---- 9 Knobs (matching effects/se50pitch/manifest.json) ----
v1_st = hslider("V1 St[1][values:-24|-23|-22|-21|-20|-19|-18|-17|-16|-15|-14|-13|-12|-11|-10|-9|-8|-7|-6|-5|-4|-3|-2|-1|0|+1|+2|+3|+4|+5|+6|+7|+8|+9|+10|+11|+12|+13|+14|+15|+16|+17|+18|+19|+20|+21|+22|+23|+24][tooltip:Voice 1 pitch shift in semitones (-24 to +24; default -2).]", 22, 0, 48, 1);
v1_lv = hslider("V1 Lv[2][tooltip:Voice 1 mix level (0..100, scaled for unity 3-voice sum).]", 65, 0, 100, 1) * 0.01 : si.smoo;
v2_st = hslider("V2 St[3][values:-24|-23|-22|-21|-20|-19|-18|-17|-16|-15|-14|-13|-12|-11|-10|-9|-8|-7|-6|-5|-4|-3|-2|-1|0|+1|+2|+3|+4|+5|+6|+7|+8|+9|+10|+11|+12|+13|+14|+15|+16|+17|+18|+19|+20|+21|+22|+23|+24][tooltip:Voice 2 pitch shift in semitones (-24 to +24; default -5).]", 19, 0, 48, 1);
v2_lv = hslider("V2 Lv[4][tooltip:Voice 2 mix level (0..100, scaled for unity 3-voice sum).]", 50, 0, 100, 1) * 0.01 : si.smoo;
v3_st = hslider("V3 St[5][values:-24|-23|-22|-21|-20|-19|-18|-17|-16|-15|-14|-13|-12|-11|-10|-9|-8|-7|-6|-5|-4|-3|-2|-1|0|+1|+2|+3|+4|+5|+6|+7|+8|+9|+10|+11|+12|+13|+14|+15|+16|+17|+18|+19|+20|+21|+22|+23|+24][tooltip:Voice 3 pitch shift in semitones (-24 to +24; default -12).]", 12, 0, 48, 1);
v3_lv = hslider("V3 Lv[6][tooltip:Voice 3 mix level (0..100, scaled for unity 3-voice sum).]", 55, 0, 100, 1) * 0.01 : si.smoo;
dry_k = hslider("Dry[7][tooltip:Dry vocal level fed with pitch swarm into Drive (100 is unity).]", 75, 0, 100, 1) * 0.01 : si.smoo;
fdbk_k = hslider("Fdbk[8][tooltip:SE-50 pitch feedback (10-35 metallic halo, 50-90 cascading spirals).]", 10, 0, 100, 1) * 0.01 : si.smoo;
sprd_k = hslider("Sprd[9][tooltip:SE-50 stereo pan spread, +-8 cent micro-detune, and 0/7/14 ms pre-delay.]", 75, 0, 100, 1) * 0.01 : si.smoo;

// ---- 1. Block-Rate Pitch Ratios, Stereo Spread, Pre-Delays & Brownian Wander ----
white = no.noise;
drift = no.lfnoise(0.0072);

st1 = floor(v1_st + 0.5) - 24.0;
st2 = floor(v2_st + 0.5) - 24.0;
st3 = floor(v3_st + 0.5) - 24.0;

det    = 0.00463 * sprd_k;
wander = 0.0006  * drift * sprd_k;

r1 = ba.semi2ratio(st1) * (1.0 - det) * (1.0 - wander);
r2 = ba.semi2ratio(st2);
r3 = ba.semi2ratio(st3) * (1.0 + det) * (1.0 + wander);

// Phase velocities for 1500-sample (~34.0 ms) SE-50 grain window (1/1500 = 0.0006666667)
dp1 = (1.0 - r1) * 0.0006666667;
dp2 = (1.0 - r2) * 0.0006666667;
dp3 = (1.0 - r3) * 0.0006666667;

// Haas stereo pre-delays (0 ms / 7 ms / 14 ms at Sprd = 100)
base1 = 132.0;
base2 = 132.0 + 308.0 * sprd_k;
base3 = 132.0 + 616.0 * sprd_k;

// Stereo panorama gains (V1 Left, V2 Center, V3 Right; 0.55x 3-voice unity scaling)
side_atten = 1.0 - 0.72 * sprd_k;
g1L = v1_lv * 0.55;              g1R = v1_lv * 0.55 * side_atten;
g2L = v2_lv * 0.55;              g2R = v2_lv * 0.55;
g3L = v3_lv * 0.55 * side_atten; g3R = v3_lv * 0.55;

fdbk_gain = fdbk_k * 0.78;
dith      = white * 0.00017;

// ---- 2. 20% Smoothstep Tukey Window & Dual-Tap Grain Phase Accumulator ----
// Since p in [0, 1) and |dp| < 0.002, branchless/predicated wrap avoids sh_floorf().
ph_step(p, dp) = ba.if(p_sum >= 1.0, p_sum - 1.0, ba.if(p_sum < 0.0, p_sum + 1.0, p_sum))
    with {
        p_sum = p + dp;
    };

p1_raw = (ph_step(dp1) ~ _);
p2_raw = (ph_step(dp2) ~ _);
p3_raw = (ph_step(dp3) ~ _);

// Stagger voice phases by 0.000, 0.333, 0.667 so crossfade splices never collide
p1A = p1_raw;
p1B = ba.if(p1A >= 0.500, p1A - 0.500, p1A + 0.500);
w1A = zm_tukey20(p1A);
d1A = p1A * 1500.0 + base1;
d1B = p1B * 1500.0 + base1;

p2A = ba.if(p2_raw >= 0.6666667, p2_raw - 0.6666667, p2_raw + 0.3333333);
p2B = ba.if(p2A >= 0.500, p2A - 0.500, p2A + 0.500);
w2A = zm_tukey20(p2A);
d2A = p2A * 1500.0 + base2;
d2B = p2B * 1500.0 + base2;

p3A = ba.if(p3_raw >= 0.3333333, p3_raw - 0.3333333, p3_raw + 0.6666667);
p3B = ba.if(p3A >= 0.500, p3A - 0.500, p3A + 0.500);
w3A = zm_tukey20(p3A);
d3A = p3A * 1500.0 + base3;
d3B = p3B * 1500.0 + base3;

// ---- 3. 32 kHz / 12-Bit Boss SE-50 Rack DAC Clock & Quantizer ----
dac_clk_step(ph) = ba.if(ph_sum >= 1.0, ph_sum - 1.0, ph_sum)
    with {
        ph_sum = ph + 0.72562358;
    };
dac_ph   = (dac_clk_step ~ _);
dac_trig = ba.if(dac_ph < 0.72562358, 1.0, 0.0);

// ---- 4. Mono-In / Stereo-Out Pitch Tank with 32 kHz DAC Feedback (`~`) ----
// Authentic Boss SE-50 Alg.12 topology (pairs directly after mono-in FaustVFE):
//   - Single shared 4096-sample SDRAM ring buffer (`dline_0`, 16 KB, fits in L1D/L2)
//   - 3 dual-tap pitch voices (6 `zm_fdelay1` reads total, lerp crossfade `tB + wA*(tA-tB)`)
//   - Full stereo output via 0/7/14 ms Haas pre-delays, L/C/R pan spread, and stereo DAC
//   - True-stereo dry pass-through (`inL * dry_k`, `inR * dry_k`)
se50_tank(fb, inL, inR) = fb_next, outL, outR
    with {
        hp       = fi.highpass(1, 135.0, inL + zm_soft_clip(fb * fdbk_gain));
        aa_sig   = 0.50 * hp + 0.50 * (hp');
        t1A      = zm_fdelay1(d1A, aa_sig);
        t1B      = zm_fdelay1(d1B, aa_sig);
        v1       = t1B + w1A * (t1A - t1B);
        t2A      = zm_fdelay1(d2A, aa_sig);
        t2B      = zm_fdelay1(d2B, aa_sig);
        v2       = t2B + w2A * (t2A - t2B);
        t3A      = zm_fdelay1(d3A, aa_sig);
        t3B      = zm_fdelay1(d3B, aa_sig);
        v3       = t3B + w3A * (t3A - t3B);
        wetL     = v1 * g1L + v2 * g2L + v3 * g3L;
        wetR     = v1 * g1R + v2 * g2R + v3 * g3R;
        preL     = zm_ema(0.62, wetL);
        preR     = zm_ema(0.62, wetR);
        holdL    = ba.latch(dac_trig, zm_quant12(zm_soft_clip(preL) + dith));
        holdR    = ba.latch(dac_trig, zm_quant12(zm_soft_clip(preR) - dith));
        dacPostL = zm_ema(0.58, holdL);
        dacPostR = zm_ema(0.58, holdR);
        fb_next  = 0.5 * (dacPostL + dacPostR);
        outL     = inL * dry_k + dacPostL;
        outR     = inR * dry_k + dacPostR;
    };

process(xL, xR) = (se50_tank(xL, xR) ~ _);
