// effects/faustflt/faustflt.dsp
//
// Example FAUST DSP effect compiled to a Zoom MS Plus (.ZD2) package via
// `python tools/faust2zd2.py effects/faustflt/faustflt.dsp --build`
//
// Signal chain (calibrated for the Zoom MS Plus ~0.25 RMS / -18 dBFS bus):
//   1. Pre-drive high-pass filter (`fi.highpass(1, 95)`) to keep bass tight
//   2. Smooth tanh saturation (`ma.tanh(x * drive_gain)`) with division-free
//      reciprocal makeup gain calibrated so nominal pedal signals (~0.25 RMS)
//      maintain unity loudness across the entire Drive range (0..100)
//   3. Resonant 2-pole Chamberlin State Variable Filter (`fi.svf_lp(freq, res)`)
//   4. DC blocker (`fi.dcblocker`) + stock Zoom VOL output trim (`Level`)

declare name "SVFFlt";
declare zd2_filename "XXSVFFLT";
declare zd2_id "07001160";
declare version "1.00";
declare zd2_display "SVF FLT";
declare zd2_badge "SF";
declare description "Resonant 2-pole SVF synth filter + tanh saturator for Zoom MS Plus.";
declare zd2_dspload "8";

import("stdfaust.lib");

// ---- UI Controls (automatically mapped to Zoom MS Plus knobs & manifest.json) ----
drive_knob = hslider("Drive[1][tooltip:Pre-filter tanh saturation drive (0..100).]", 45, 0, 100, 1) : si.smoo;
freq_knob  = hslider("Freq[2][unit:Hz][tooltip:Resonant 2-pole SVF low-pass cutoff (180..7500 Hz).]", 1400, 180, 7500, 1) : si.smoo;
res_knob   = hslider("Res[3][tooltip:Filter resonance Q factor (1..10).]", 3.5, 0.7, 10.0, 0.1) : si.smoo;
level_knob = hslider("Level[4][curve:stock_vol][tooltip:Output level (80 is unity gain).]", 80, 0, 100, 1) : si.smoo;

// ---- Derived DSP signals (calibrated to Zoom -18 dBFS nominal bus level) ----
// Map Drive 0..100 -> 1.0x .. 13.0x pre-gain, with reciprocal makeup trim
// so small signals (~0.25 RMS) stay at unity loudness from Drive=0 to Drive=100
drive_gain  = 1.0 + drive_knob * 0.12;
makeup      = 0.25 / ma.tanh(0.25 * drive_gain);

saturate(x) = ma.tanh(x * drive_gain) * makeup;

// ---- Main Per-Channel Process ----
process(x) = x
    : fi.highpass(1, 95.0)
    : saturate
    : fi.svf_lp(freq_knob, res_knob)
    : fi.dcblocker
    : *(level_knob);
