// effects/segacddly/segacddly.dsp
//
// Sega CD / Mega-CD Ricoh RF5C164 8-Bit Sign-Magnitude PCM Ping-Pong Delay
// compiled to a Zoom MS-70CDR+ (.ZD2) package via:
//   python tools/faust2zd2.py effects/segacddly/segacddly.dsp --build
//
// Hardware Architecture Modeled:
//   1. Ricoh RF5C164 8-bit sign-magnitude PCM encoding (1 sign bit + 7 magnitude
//      bits, 0..127 — authentic dual-zero crossover distinct from 2's complement).
//   2. Variable RF5C164 PCM clock (4.0 kHz .. 32.55 kHz) with random Sample &
//      Hold (`ba.latch` + `no.noise`) clock jitter (`ClkJit`) modulating both
//      the PCM phase increment and subtle delay-tap clock drift, followed by a
//      Sega CD analog output reconstruction LPF.
//   3. Dual 32,768-sample external SDRAM stereo delay buffers (~512 KB, up to
//      740 ms per side) with 4 stereo routing modes (`PING`, `DUAL`, `MONO`, `WIDE`).
//   4. Momentary footswitch support (`ON/OFF = LATCH / UNLATCH`).

declare name "RF5CDly";
declare zd2_filename "XXRF5CDL";
declare zd2_id "07001230";
declare version "1.00";
declare zd2_display "RF5C DLY";
declare zd2_badge "RF";
declare description "Ricoh RF5C164 8-bit sign-magnitude PCM stereo ping-pong delay (up to 740 ms) with 4-32.55 kHz clock and random S&H clock jitter.";
declare zd2_dspload "20";
declare zd2_footswitch "true";

import("stdfaust.lib");
import("zoom.lib");

// ---- 8 Knobs + Trailing Footswitch Toggle ----
mode_sel    = hslider("Mode[1][values:PING|DUAL|MONO|WIDE][tooltip:RF5C164 stereo echo routing (Ping-Pong, Dual 1:0.75, Mono, Wide Inverted).]", 0, 0, 3, 1);
time_ms     = hslider("Time[2][unit:ms][tooltip:Delay time (20..740 ms).]", 320, 20, 740, 1) : si.smoo;
fdbk_knob   = hslider("Fdbk[3][tooltip:Echo feedback repeats (0..95).]", 52, 0, 95, 1) * 0.01 : si.smoo;
rate_khz    = hslider("Rate[4][unit:kHz][tooltip:Ricoh RF5C164 PCM clock rate (4..33 kHz; 33 = max 32.55 kHz).]", 22, 4, 33, 1) : si.smoo;
bits_knob   = hslider("Bits[5][tooltip:8-bit sign-magnitude PCM grit (0 = clean 16-bit CD-DA, 55 = authentic 8-bit RF5C164, 100 = crunchy 5-bit).]", 55, 0, 100, 1) * 0.01 : si.smoo;
clkjit_knob = hslider("ClkJit[6][tooltip:Random S&H RF5C164 clock jitter & delay-tap drift (0 = crystal-locked, 100 = unstable clock).]", 20, 0, 100, 1) * 0.01 : si.smoo;
tone_knob   = hslider("Tone[7][tooltip:Sega CD analog output low-pass filter (0 = dark, 100 = bright).]", 62, 0, 100, 1) * 0.01 : si.smoo;
mix_knob    = hslider("Mix[8][tooltip:Dry/Wet delay mix (0..100).]", 42, 0, 100, 1) * 0.01 : si.smoo;

// ---- 1. Block-Rate Parameters ----
d_base = max(44.0, min(32550.0, ba.ms2samp(time_ms)));

// Feedback cap at 0.94 to stay musical & bounded
fb     = min(0.94, fdbk_knob);

// RF5C164 PCM clock phase increment (4,000 Hz .. 32,552 Hz)
clk_hz        = min(32552.0, rate_khz * 1000.0);
clk_step_base = aa.clip(0.068, 0.73814, clk_hz / ma.SR);
jit_depth     = clkjit_knob * (0.28 + 0.42 * clkjit_knob);
tap_jit_amt   = clkjit_knob * clkjit_knob * 18.0;

// 1-pole analog reconstruction LPF cutoff (1.2 kHz .. 11.5 kHz)
lpf_hz = 1200.0 + tone_knob * tone_knob * 10300.0;
lpf_a  = min(0.96, max(0.12, 1.0 - exp(-6.2831853 * lpf_hz / ma.SR)));

// 8-bit sign-magnitude magnitude levels:
// Bits=0 -> bypassed (clean), Bits=55 -> 127 levels (authentic 8-bit sign+7-bit mag),
// Bits=100 -> 15 levels (5-bit sign+4-bit mag)
mag_levels = ba.if(bits_knob < 0.01, 0.0,
    max(15.0, floor(15.0 + (1.0 - bits_knob) * (1.0 - bits_knob) * 550.0)));

// True Ricoh RF5C164 sign-magnitude quantizer:
// Truncates magnitude toward zero (creating the chip's characteristic dual-zero deadzone)
// while guaranteeing exact 0.0 -> 0.0 on silence.
rf5c_quant(x) = ba.if(mag_levels < 1.0, x,
    ba.if(abs(x) < 1.0e-6, 0.0,
        sign_x * floor(min(1.0, abs(x)) * mag_levels) / mag_levels))
    with {
        sign_x = ba.if(x >= 0.0, 1.0, -1.0);
    };

// Random Sample & Hold (`ba.latch` + `no.noise`) for RF5C164 clock jitter:
// Stepped at ~200 Hz (220 samples @ 44.1 kHz); at ClkJit=0, clk_step is crystal-locked.
sh_rand  = ba.latch(ba.pulse(220.0), no.noise);
clk_step = aa.clip(0.068, 0.73814, clk_step_base * (1.0 + jit_depth * sh_rand));
tap_jit  = zm_ema(0.015, sh_rand) * tap_jit_amt;

// Delay tap positions with subtle RF5C164 clock-drift modulation
d_l = aa.clip(44.0, 32600.0, d_base + tap_jit);
d_r = aa.clip(44.0, 32600.0,
    ba.if(mode_sel > 0.5 & mode_sel < 1.5, d_base * 0.75, d_base) - tap_jit);

// Fractional-clock ZOH sample-and-hold driven by jittered RF5C164 phase increment
rf5c_sh_step(ph, hold, x_in) = next_ph, next_hold, next_hold
    with {
        ph_sum    = ph + clk_step;
        tk        = ba.if(ph_sum >= 1.0, 1.0, 0.0);
        next_ph   = ph_sum - tk;
        next_hold = ba.if(tk > 0.5, x_in, hold);
    };

rf5c_sh(x) = (rf5c_sh_step(x) ~ (_, _)) : !, !, _;

// ---- 2. Equal-Power Dry/Wet Mix & Dual 32,768-Sample Stereo SDRAM Delay Tank (~512 KB) ----
// Mode 0 (PING): L feeds R, R feeds L (classic stepped-pan ping-pong)
// Mode 1 (DUAL): independent L/R with 1:0.75 time ratio
// Mode 2 (MONO): summed mono PCM delay
// Mode 3 (WIDE): cross-fed inverted-polarity stereo echo
dry_g   = sqrt(max(0.0, 1.0 - mix_knob));
wet_g   = sqrt(max(0.0, mix_knob));
is_ping = (mode_sel < 0.5);
is_mono = (mode_sel > 1.5) & (mode_sel < 2.5);
is_wide = (mode_sel > 2.5);

segacd_l_step(wL, wR, inL, inR) = nwL, nwR, wetL
    with {
        tapL = de.fdelay1(32768, d_l, wL);
        tapR = de.fdelay1(32768, d_r, wR);

        // RF5C164 jittered sample-and-hold + sign-magnitude PCM quantization + analog LPF
        pcmL = zm_ema(lpf_a, rf5c_sh(rf5c_quant(tapL)));
        pcmR = zm_ema(lpf_a, rf5c_sh(rf5c_quant(tapR)));

        // Subtle DC blocker inside feedback loop
        fltL = pcmL - zm_ema(0.004, pcmL);
        fltR = pcmR - zm_ema(0.004, pcmR);

        // Stereo feedback routing by mode
        mono_in = 0.5 * (inL + inR);
        injL = ba.if(is_ping, mono_in, ba.if(is_mono, mono_in, inL));
        injR = ba.if(is_ping, 0.0,     ba.if(is_mono, mono_in, inR));

        fbL = ba.if(is_ping, fltR, ba.if(is_wide, 0.0 - fltR, fltL));
        fbR = ba.if(is_ping, fltL, ba.if(is_wide, fltL,       fltR));

        nwL = zm_sat16(injL + fb * fbL);
        nwR = zm_sat16(injR + fb * fbR);

        wetL = fltL;
    };

segacd_r_step(wL, wR, inL, inR) = nwL, nwR, wetR
    with {
        tapL = de.fdelay1(32768, d_l, wL);
        tapR = de.fdelay1(32768, d_r, wR);

        pcmL = zm_ema(lpf_a, rf5c_sh(rf5c_quant(tapL)));
        pcmR = zm_ema(lpf_a, rf5c_sh(rf5c_quant(tapR)));

        fltL = pcmL - zm_ema(0.004, pcmL);
        fltR = pcmR - zm_ema(0.004, pcmR);

        mono_in = 0.5 * (inL + inR);
        injL = ba.if(is_ping, mono_in, ba.if(is_mono, mono_in, inL));
        injR = ba.if(is_ping, 0.0,     ba.if(is_mono, mono_in, inR));

        fbL = ba.if(is_ping, fltR, ba.if(is_wide, 0.0 - fltR, fltL));
        fbR = ba.if(is_ping, fltL, ba.if(is_wide, fltL,       fltR));

        nwL = zm_sat16(injL + fb * fbL);
        nwR = zm_sat16(injR + fb * fbR);

        wetR = ba.if(is_wide, 0.0 - fltR, fltR);
    };

segacd_tank_l(inL, inR) = (segacd_l_step(inL, inR) ~ (_, _)) : !, !, _;
segacd_tank_r(inL, inR) = (segacd_r_step(inL, inR) ~ (_, _)) : !, !, _;

process(xL, xR) =
    xL * dry_g + wetL * wet_g,
    xR * dry_g + wetR * wet_g
    with {
        wetL = segacd_tank_l(xL, xR);
        wetR = segacd_tank_r(xL, xR);
    };
