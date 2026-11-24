// effects/segacd/segacd.dsp
//
// Sega CD / Mega-CD Ricoh RF5C164 8-Bit Sign-Magnitude PCM Lo-Fi Processor
// compiled to a Zoom MS-70CDR+ (.ZD2) package via:
//   python tools/faust2zd2.py effects/segacd/segacd.dsp --build
//
// Hardware Architecture Modeled:
//   1. Ricoh RF5C164 8-bit sign-magnitude PCM encoding (1 sign bit + 7 magnitude
//      bits, 0..127) with authentic dual-zero crossover & zero idle buzz on silence.
//   2. Variable RF5C164 PCM clock (4.0 kHz .. 32.552 kHz) driven by a fractional
//      phase-accumulator ZOH sample-and-hold.
//   3. Random Sample & Hold (`ba.latch` + `no.noise`) clock jitter (`ClkJit`):
//      at 0 the clock is crystal-locked; turning ClkJit up modulates the RF5C164
//      sample-rate phase increment with stepped random S&H values clamped to the
//      valid clock range for organic clock-drift and jitter grit.
//   4. 4 hardware revision output stages (`M1CD`, `M2CD`, `CDX`, `RAW8B`) with
//      analog reconstruction filtering and pre-PCM overdrive.

declare name "RF5C FX";
declare zd2_filename "XXRF5CFX";
declare zd2_id "07001240";
declare version "1.00";
declare zd2_display "RF5C FX";
declare zd2_badge "RF";
declare description "Ricoh RF5C164 8-bit sign-magnitude PCM lo-fi processor with 4-32.55 kHz clock, random S&H clock jitter, and 4 output stages.";
declare zd2_dspload "14";

import("stdfaust.lib");
import("zoom.lib");

// ---- 7 Knobs ----
model_sel   = hslider("Model[1][values:MOD1|MOD2|PORT|RAW8B][tooltip:Sega CD hardware output stage (Model 1 stage, Model 2 dark stage, Portable crisp stage, Raw 8-bit RF5C164 tap).]", 0, 0, 3, 1);
rate_khz    = hslider("Rate[2][unit:kHz][tooltip:Ricoh RF5C164 PCM clock rate (4..33 kHz; 33 = max 32.55 kHz).]", 22, 4, 33, 1) : si.smoo;
bits_knob   = hslider("Bits[3][tooltip:8-bit sign-magnitude PCM grit (0 = clean 16-bit CD-DA, 55 = authentic 8-bit RF5C164, 100 = crunchy 4.5-bit).]", 55, 0, 100, 1) * 0.01 : si.smoo;
clkjit_knob = hslider("ClkJit[4][tooltip:Random S&H RF5C164 clock jitter & drift (0 = crystal-locked, 100 = unstable clock).]", 20, 0, 100, 1) * 0.01 : si.smoo;
tone_knob   = hslider("Tone[5][tooltip:Analog reconstruction low-pass filter trim (0 = dark, 100 = bright).]", 65, 0, 100, 1) * 0.01 : si.smoo;
drive_knob  = hslider("Drive[6][tooltip:Pre-PCM input overdrive (0..100).]", 15, 0, 100, 1) * 0.01 : si.smoo;
level_knob  = hslider("Level[7][curve:stock_vol][tooltip:Master output level (80 is unity gain).]", 80, 0, 100, 1) : si.smoo;

// ---- 1. Hardware Revision Tables (Hoisted to Block-Rate) ----
sel4(m, v0, v1, v2, v3) =
    ba.if(m < 0.5, v0,
    ba.if(m < 1.5, v1,
    ba.if(m < 2.5, v2, v3)));

// Model analog LPF ceiling (M1CD ~5.5k, M2CD ~3.8k, CDX ~7.8k, RAW8B open)
model_lpf  = sel4(model_sel, 0.56, 0.42, 0.72, 0.98);
st_width   = sel4(model_sel, 0.88, 0.72, 0.96, 1.00);
is_raw_tap = ba.if(model_sel > 2.5, 1.0, 0.0);

// ---- 2. Block-Rate DSP Coefficients ----
clk_hz        = min(32552.0, rate_khz * 1000.0);
clk_step_base = aa.clip(0.068, 0.73814, clk_hz / ma.SR);
jit_depth     = clkjit_knob * (0.28 + 0.42 * clkjit_knob);

// Combine model LPF ceiling with user Tone knob
tone_a = min(0.98, max(0.14, model_lpf * (0.28 + 0.72 * tone_knob)));

// 8-bit sign-magnitude magnitude levels:
// Bits=0 -> bypassed (clean), Bits=55 -> ~127 levels (authentic 8-bit sign+7-bit mag),
// Bits=100 -> 11 levels (crunchy 4.5-bit sign-magnitude)
mag_levels = ba.if(bits_knob < 0.01, 0.0,
    max(11.0, floor(11.0 + (1.0 - bits_knob) * (1.0 - bits_knob) * 570.0)));

// Pre-PCM overdrive & automatic loudness compensation
drv_gain = 1.0 + drive_knob * (2.2 + 4.0 * drive_knob);
drv_comp = 1.0 / (1.0 + drive_knob * 1.55);
out_gain = level_knob * drv_comp;

// ---- 3. Random Sample & Hold (`ba.latch` + `no.noise`) Clock Jitter ----
// Shared across L/R (single RF5C164 clock domain on hardware).
// At ClkJit=0, jit_depth=0 -> crystal-locked clock.
sh_rand  = ba.latch(ba.pulse(220.0), no.noise);
clk_step = aa.clip(0.068, 0.73814, clk_step_base * (1.0 + jit_depth * sh_rand));

// ---- 4. Ricoh RF5C164 ZOH Sample-and-Hold + Sign-Magnitude Quantizer ----
rf5c_sh_step(ph, hold, x_in) = next_ph, next_hold, next_hold
    with {
        ph_sum    = ph + clk_step;
        tk        = ba.if(ph_sum >= 1.0, 1.0, 0.0);
        next_ph   = ph_sum - tk;
        next_hold = ba.if(tk > 0.5, x_in, hold);
    };

rf5c_sh(x) = (rf5c_sh_step(x) ~ (_, _)) : !, !, _;

rf5c_quant(x) = ba.if(mag_levels < 1.0, x,
    ba.if(abs(x) < 1.0e-6, 0.0,
        sign_x * floor(min(1.0, abs(x)) * mag_levels) / mag_levels))
    with {
        sign_x = ba.if(x >= 0.0, 1.0, -1.0);
    };

rf5c_chan(x) = out
    with {
        driven = zm_soft_clip(x * drv_gain);
        held   = rf5c_sh(driven);
        quant  = rf5c_quant(held);
        // 2-pole analog reconstruction LPF (bypassed in RAW8B mode)
        filt   = zm_ema(tone_a, zm_ema(tone_a, quant));
        shaped = ba.if(is_raw_tap > 0.5, quant, filt);
        out    = shaped - zm_ema(0.004, shaped);
    };

// ---- 5. Stereo Output Stage ----
process(xL, xR) =
    (mid + side * st_width) * out_gain,
    (mid - side * st_width) * out_gain
    with {
        cL   = rf5c_chan(xL);
        cR   = rf5c_chan(xR);
        mid  = 0.5 * (cL + cR);
        side = 0.5 * (cL - cR);
    };
