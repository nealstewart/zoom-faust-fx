// effects/ym2612/ym2612.dsp
//
// Sega Genesis / Mega Drive YM2612 & Z80 PCM Hardware Artifact Emulator ("YM2612")
// implemented in FAUST and compiled to a Zoom MS-70CDR+ (.ZD2 + .ZIC) package via:
//   python tools/faust2zd2.py effects/ym2612/ym2612.dsp --build
//
// DSP Architecture (Stereo-In -> Stereo-Out, Zero SDRAM, Pure C674x L1D):
//   1. 4 Authentic Sega Genesis / Mega Drive Hardware Revision Models:
//        - M1VA2 : Model 1 VA2/VA3 ("High Definition Graphics") - full YM2612
//                  9-bit ladder DAC, warm ~3.8 kHz CXA1034P analog LPF, wide stereo.
//        - M1VA6 : Model 1 VA6/VA7 - brighter ~6.8 kHz analog filter, tighter ladder.
//        - M2VA1 : Model 2 VA0/VA1 - muffled ~2.1 kHz LPF, asymmetric LM324 op-amp
//                  clipping, narrowed stereo image.
//        - RAW9B : Raw YM2612 Direct Chip Tap - unfiltered 9-bit staircase & ladder.
//   2. Pre-DAC Drive into the YM2612 9-bit digital rail with auto-level compensation.
//   3. Z80 PCM Zero-Order Hold Sample Clock (4.0..44.1 kHz) with 68000/VDP DMA
//      Bus Contention Clock Jitter (`DMAJit`, 59.92 Hz VBlank + HBlank stutter).
//   4. Variable YM2612 9-bit / Z80 8-bit PCM DAC Quantizer (`Bits`, 15-bit -> 4.5-bit).
//   5. Signature YM2612 9-bit Resistor-Ladder Crossover Distortion ("Ladder Effect")
//      with zero-crossing discontinuity step and dead-silent zero floor.
//   6. Model-specific analog reconstruction filter, op-amp saturation, 28 Hz DC block,
//      `stock_vol` `Level`, and trailing `ON/OFF` (`LATCH | UNLATCH`) footswitch.

declare name "YM2612";
declare zd2_filename "XXYM2612";
declare zd2_id "07001210";
declare version "1.00";
declare zd2_display "YM2612";
declare zd2_badge "YM";
declare description "16-bit FM-era YM2612 9-bit ladder DAC, Z80 PCM rate/jitter, and analog output stage emulator.";
declare zd2_dspload "20";
declare zd2_footswitch "true";

import("stdfaust.lib");
import("zoom.lib");

// ---- 7 Knobs + Trailing Footswitch Toggle (2 Pages of 4 on Zoom MS-70CDR+) ----
model_sel   = hslider("Model[1][values:M1VA2|M1VA6|M2VA1|RAW9B][tooltip:Genesis hardware revision (M1VA2 = Model 1 VA2 warm, M1VA6 = Model 1 bright, M2VA1 = Model 2 muffled/clippy, RAW9B = unfiltered chip tap).]", 0, 0, 3, 1);
rate_knob   = hslider("Rate[2][tooltip:Z80 PCM / YM2612 sample-and-hold clock (0 = 4 kHz crusty speech PCM, 52 = 16 kHz Streets of Rage PCM, 100 = 44.1 kHz).]", 52, 0, 100, 1) * 0.01 : si.smoo;
bits_knob   = hslider("Bits[3][tooltip:DAC bit-depth quantization (0 = clean 15-bit, 55 = YM2612 9-bit DAC, 67 = Z80 8-bit PCM, 100 = 4.5-bit crunch).]", 55, 0, 100, 1) * 0.01 : si.smoo;
ladder_knob = hslider("Ladder[4][tooltip:YM2612 9-bit resistor-ladder zero-crossing discontinuity ('ladder effect' crossover buzz on quiet tails).]", 60, 0, 100, 1) * 0.01 : si.smoo;
dmajit_knob = hslider("DMAJit[5][tooltip:Z80 / 68000 DMA bus-contention sample clock jitter (59.92 Hz VBlank & scanline stutter).]", 35, 0, 100, 1) * 0.01 : si.smoo;
drive_knob  = hslider("Drive[6][tooltip:Pre-DAC 9-bit digital rail & Genesis LM324/CXA1034P op-amp overdrive (auto-level compensated).]", 35, 0, 100, 1) * 0.01 : si.smoo;
level_knob  = hslider("Level[7][curve:stock_vol][tooltip:Master output level (80 is unity at nominal MS-70CDR+ bus level).]", 80, 0, 100, 1) : si.smoo;

// ---- 1. Hardware Revision Parameter Tables (Hoisted to Block-Rate) ----
sel4(m, v0, v1, v2, v3) =
    ba.if(m < 0.5, v0,
    ba.if(m < 1.5, v1,
    ba.if(m < 2.5, v2, v3)));

// Analog reconstruction 2-pole LPF alpha: M1VA2 (~3.8k), M1VA6 (~6.8k), M2VA1 (~2.1k), RAW9B (open)
lpf_alpha   = sel4(model_sel, 0.46, 0.63, 0.31, 0.96);
// Stereo image width (Model 2 has narrower stereo mix)
st_width    = sel4(model_sel, 1.00, 1.00, 0.55, 1.00);
// YM2612 vs YM3438 ladder intensity scaling
ladder_scl  = sel4(model_sel, 1.00, 0.72, 0.85, 1.15);
// Op-amp asymmetry (Model 2 VA0/VA1 LM324 asymmetric clipping)
opamp_asym  = sel4(model_sel, 0.08, 0.03, 0.34, 0.00);
is_raw_tap  = ba.if(model_sel > 2.5, 1.0, 0.0);

// ---- 2. Block-Rate DSP Coefficients ----
// Z80 PCM / YM2612 clock step: 4.0 kHz (0.0907) -> ~16 kHz (0.36) -> 44.1 kHz (1.0)
rate_step   = min(1.0, 0.09070295 + rate_knob * (0.28 + 0.62929705 * rate_knob));
eff_rate    = min(1.0 - dmajit_knob * 0.22, rate_step);
jit_depth   = dmajit_knob * (0.35 + 0.50 * dmajit_knob);

// Bit-depth quantizer levels: 2^(14 - 10.5*b) => 16384 (b=0), ~300 (b=0.55, 9-bit), 128 (b=0.67, 8-bit), ~11.3 (b=1)
q_levels    = pow(2.0, 14.0 - bits_knob * 10.5);
q_inv       = 1.0 / q_levels;

// YM2612 9-bit resistor-ladder zero-crossing step (0 at Ladder=0, ~6 LSB at 60%, ~16 LSB at 100%)
ladder_step = ladder_knob * ladder_knob * 0.065 * ladder_scl;

// Pre-DAC overdrive & automatic loudness compensation
drv_gain    = 1.0 + drive_knob * (2.5 + 4.5 * drive_knob);
drv_comp    = 1.0 / (1.0 + drive_knob * 1.65);
asym_norm   = 1.0 / (1.0 + 0.25 * opamp_asym);
out_gain    = 0.5 * level_knob * drv_comp;

// ---- 3. Z80 / 68000 DMA Bus Contention Clock Jitter Modulator ----
// Shared across L/R (single YM2612 + Z80 clock domain on real hardware!)
dma_jit_sig = 0.45 * zm_osc(59.92) + 0.30 * zm_osc(217.3) + 0.25 * zm_ema(0.18, no.noise);
step_jit    = aa.clip(0.045, 1.0, eff_rate * (1.0 + jit_depth * dma_jit_sig));

// ---- 4. YM2612 ZOH Sample-and-Hold + 9-Bit Quantizer + Ladder Effect ----
ym_sh_step(ph, hold, x_in) = next_ph, next_hold, next_hold
    with {
        ph_sum    = ph + step_jit;
        tk        = ba.if(ph_sum >= 1.0, 1.0, 0.0);
        next_ph   = ph_sum - tk;
        next_hold = ba.if(tk > 0.5, x_in, hold);
    };

ym_sh(x) = (ym_sh_step(x) ~ (_, _)) : !, !, _;

ym_dac_ladder(x_held) = aa.clip(-1.15, 1.15, q_sig + sgn_q * ladder_step)
    with {
        q_sig = floor(x_held * q_levels + 0.5) * q_inv;
        sgn_q = ba.if(q_sig > 0.00015, 1.0,
                ba.if(q_sig < -0.00015, -1.0, 0.0));
    };

// ---- 5. Genesis Analog Output Stage (Op-Amp Saturation + 2-Pole Reconstruction LPF + DC Block) ----
genesis_amp(u) = ba.if(is_raw_tap > 0.5,
                       aa.clip(-1.15, 1.15, u),
                       zm_soft_clip(u + opamp_asym * u * u) * asym_norm);

ym_channel(x_st) =
    fi.highpass(1, 28.0,
        zm_ema(lpf_alpha,
            zm_ema(lpf_alpha,
                genesis_amp(
                    ym_dac_ladder(
                        ym_sh(
                            aa.clip(-1.0, 1.0, x_st * drv_gain)
                        )
                    )
                )
            )
        )
    ) * out_gain;

// ---- 6. Stereo Processing with Model-Specific Stereo Width ----
process(xL, xR) = ym_channel(st_l), ym_channel(st_r)
    with {
        mid_s  = 0.5 * (xL + xR);
        side_s = 0.5 * (xL - xR) * st_width;
        st_l   = (mid_s + side_s) * 2.0;
        st_r   = (mid_s - side_s) * 2.0;
    };
