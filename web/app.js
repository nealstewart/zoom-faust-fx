/**
 * web/app.js — Stomphacks ZD2 WebAssembly Auditioner UI & Web Audio Engine
 */

const MAX_CHAIN_SLOTS = 6;

const PRESETS = {
  xa_lofi: ["xaspu", "xarvb"],
  xa_dub: ["xaspu", "xadly", "xarvb"],
  xa_dream: ["xachr", "xadly", "xarvb"],
  fm_32bit: ["ym2612", "rf5cdly", "scsprvb"],
  brr_16bit: ["brrdsp", "firecho"],
  ebm_dirt: ["deskdrv", "slewdrv", "tzflng"],
  glitch_pitch: ["cdskip", "rackptch", "sophwg"],
  parallel_split: ["wfsplit", "swedsaw", "wfswap", "xarvb", "wfmerge"],
};

const KEYBOARD_NOTES = [
  { note: "C3", key: "a", freq: 130.81, black: false },
  { note: "C#3", key: "w", freq: 138.59, black: true },
  { note: "D3", key: "s", freq: 146.83, black: false },
  { note: "D#3", key: "e", freq: 155.56, black: true },
  { note: "E3", key: "d", freq: 164.81, black: false },
  { note: "F3", key: "f", freq: 174.61, black: false },
  { note: "F#3", key: "t", freq: 185.0, black: true },
  { note: "G3", key: "g", freq: 196.0, black: false },
  { note: "G#3", key: "y", freq: 207.65, black: true },
  { note: "A3", key: "h", freq: 220.0, black: false },
  { note: "A#3", key: "u", freq: 233.08, black: true },
  { note: "B3", key: "j", freq: 246.94, black: false },
  { note: "C4", key: "k", freq: 261.63, black: false },
];

const state = {
  catalog: null,
  effectsBySlug: new Map(),
  wasmCache: new Map(), // slug -> ArrayBuffer
  selectedCategory: "All",
  searchQuery: "",

  // Chain
  chain: [], // [{ id, slug, effect, values: Map<index, int>, bypassed: bool }]
  nextSlotId: 1,

  // Web Audio
  ctx: null,
  worklet: null,
  inputBus: null,
  analyser: null,
  audioReady: false,

  // Master
  globalBypass: false,
  inputGain: 1.0,
  outputGain: 0.9,
  masterMix: 1.0,

  // Sample player
  activeSourceTab: "synth",
  sampleBuffers: new Map(), // url -> AudioBuffer
  currentBuffer: null,
  currentBufferLabel: "",
  sampleNode: null,
  samplePlaying: false,
  sampleLoop: true,
  sampleOffset: 0,
  sampleStartTime: 0,

  // Synth
  autoRiffTimer: null,
  autoRiffStep: 0,

  // Mic
  micStream: null,
  micNode: null,

  // Scope
  scopeMode: "scope", // "scope" | "fft"

  // Drag & drop state
  dragPayload: null, // { kind: "new", slug } | { kind: "move", slotId }

  // Global Tempo / Tap Tempo for beat-synced effects
  bpm: 120,
  tapTimes: [],
};

const SYNC_BEATS = [0, 0.25, 1 / 3, 0.5, 2 / 3, 0.75, 1.0, 1.5];
const BEAT_SYNC_SLUGS = new Set(["xadly", "firecho"]);

function formatKnobValue(slotObj, param, val) {
  if (BEAT_SYNC_SLUGS.has(slotObj.slug) && param.name === "Time") {
    const syncParam = slotObj.effect.params.find((p) => p.name === "Sync");
    const syncIdx = syncParam ? slotObj.values.get(syncParam.index) : 0;
    const norm = param.max > 0 ? val / param.max : 0;
    if (syncIdx > 0) {
      const bpm = Math.round(60 + norm * 140);
      const ms = Math.min(700, Math.round((60000 / bpm) * SYNC_BEATS[syncIdx]));
      return `${bpm} BPM (${ms}ms)`;
    }
    const ms = Math.round(20 + norm * 680);
    return `${ms} ms`;
  }
  return String(val);
}

function applyBpmToSyncedSlots(bpm) {
  const clampedBpm = Math.max(60, Math.min(200, Math.round(bpm)));
  state.bpm = clampedBpm;
  const input = $("bpmInput");
  if (input) input.value = String(clampedBpm);

  let changed = false;
  for (const slotObj of state.chain) {
    if (!BEAT_SYNC_SLUGS.has(slotObj.slug)) continue;
    const syncParam = slotObj.effect.params.find((p) => p.name === "Sync");
    const timeParam = slotObj.effect.params.find((p) => p.name === "Time");
    if (!syncParam || !timeParam) continue;
    if (slotObj.values.get(syncParam.index) > 0) {
      const timeVal = Math.round(((clampedBpm - 60) / 140) * timeParam.max);
      setSlotParam(slotObj, timeParam, timeVal);
      changed = true;
    }
  }
  if (changed) renderPedalboard();
}

// ---- DOM refs ----
const $ = (id) => document.getElementById(id);

function paramToCoeff(param, intVal) {
  if (param.curve === "stock_vol") {
    return intVal <= 80 ? intVal / 80.0 : 1.0 + (intVal - 80.0) / 40.0;
  }
  return param.max > 0 ? intVal / param.max : 0.0;
}

function formatTime(sec) {
  if (!Number.isFinite(sec) || sec < 0) sec = 0;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

// ---- Web Audio Engine ----
async function ensureAudio() {
  if (state.audioReady) {
    if (state.ctx.state === "suspended") {
      await state.ctx.resume();
    }
    updateAudioStatus();
    return;
  }

  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  const ctx = new AudioCtx({ sampleRate: 44100 });
  await ctx.audioWorklet.addModule("worklet-processor.js");

  const inputBus = ctx.createGain();
  const worklet = new AudioWorkletNode(ctx, "stomphacks-processor", {
    numberOfInputs: 1,
    numberOfOutputs: 1,
    outputChannelCount: [2],
  });
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 1024;
  analyser.smoothingTimeConstant = 0.78;

  inputBus.connect(worklet);
  worklet.connect(analyser);
  analyser.connect(ctx.destination);

  worklet.port.onmessage = (ev) => {
    const msg = ev.data;
    if (!msg) return;
    if (msg.type === "meters") {
      updateMeters(msg.inL, msg.inR, msg.outL, msg.outR);
    } else if (msg.type === "slotError") {
      console.error("WASM slot error:", msg);
      $("audioCtxState").textContent = `WASM Error: ${msg.error}`;
    }
  };

  state.ctx = ctx;
  state.inputBus = inputBus;
  state.worklet = worklet;
  state.analyser = analyser;
  state.audioReady = true;

  // Push current master settings
  syncMasterToWorklet();

  // Instantiate any slots already placed on the board before audio started
  for (let i = 0; i < state.chain.length; i++) {
    await pushSlotToWorklet(state.chain[i], i);
  }

  if (ctx.state === "suspended") {
    await ctx.resume();
  }
  updateAudioStatus();
}

function updateAudioStatus() {
  const pill = $("audioCtxState");
  if (!state.ctx) {
    pill.textContent = "Audio: Idle";
    pill.classList.remove("running");
    return;
  }
  const sr = state.ctx.sampleRate;
  if (state.ctx.state === "running") {
    pill.textContent = `Audio: Running @ ${(sr / 1000).toFixed(1)} kHz`;
    pill.classList.add("running");
  } else {
    pill.textContent = `Audio: ${state.ctx.state}`;
    pill.classList.remove("running");
  }
}

function syncMasterToWorklet() {
  if (!state.worklet) return;
  state.worklet.port.postMessage({
    type: "setMaster",
    globalBypass: state.globalBypass,
    inputGain: state.inputGain,
    outputGain: state.outputGain,
    masterMix: state.masterMix,
  });
}

async function fetchWasmBytes(slug) {
  if (state.wasmCache.has(slug)) {
    return state.wasmCache.get(slug);
  }
  const fx = state.effectsBySlug.get(slug);
  const res = await fetch(fx.wasmUrl);
  if (!res.ok) throw new Error(`Failed to fetch ${fx.wasmUrl}: ${res.status}`);
  const buf = await res.arrayBuffer();
  state.wasmCache.set(slug, buf);
  return buf;
}

async function pushSlotToWorklet(slotObj, insertIndex) {
  if (!state.worklet) return;
  const wasmBytes = await fetchWasmBytes(slotObj.slug);
  const params = slotObj.effect.params.map((p) => ({
    slot: p.slot,
    coeff: paramToCoeff(p, slotObj.values.get(p.index)),
  }));
  state.worklet.port.postMessage({
    type: "addSlot",
    slotId: slotObj.id,
    slug: slotObj.slug,
    wasmBytes: wasmBytes.slice(0),
    params,
    bypassed: slotObj.bypassed,
    insertIndex,
  });
}

// ---- Chain Management ----
async function addEffectToChain(slug, replaceAll = false, insertIndex = null) {
  const fx = state.effectsBySlug.get(slug);
  if (!fx) return;

  if (replaceAll) {
    clearChain();
  }
  if (state.chain.length >= MAX_CHAIN_SLOTS) {
    alert(`Maximum ${MAX_CHAIN_SLOTS} effects in the chain.`);
    return;
  }

  const values = new Map();
  for (const p of fx.params) {
    values.set(p.index, p.default);
  }

  const slotObj = {
    id: state.nextSlotId++,
    slug,
    effect: fx,
    values,
    bypassed: false,
  };

  const idx =
    typeof insertIndex === "number" && insertIndex >= 0 && insertIndex <= state.chain.length
      ? insertIndex
      : state.chain.length;
  state.chain.splice(idx, 0, slotObj);
  renderPedalboard();

  if (state.audioReady) {
    await pushSlotToWorklet(slotObj, idx);
  }
}

function moveSlotToIndex(slotId, insertIndex) {
  const fromIdx = state.chain.findIndex((s) => s.id === slotId);
  if (fromIdx < 0) return;
  let toIdx = Math.max(0, Math.min(state.chain.length, insertIndex));
  if (toIdx > fromIdx) toIdx--;
  if (toIdx === fromIdx) return;

  const [item] = state.chain.splice(fromIdx, 1);
  state.chain.splice(toIdx, 0, item);
  if (state.worklet) {
    state.worklet.port.postMessage({
      type: "reorderSlots",
      order: state.chain.map((s) => s.id),
    });
  }
  renderPedalboard();
}

function removeSlot(slotId) {
  state.chain = state.chain.filter((s) => s.id !== slotId);
  if (state.worklet) {
    state.worklet.port.postMessage({ type: "removeSlot", slotId });
  }
  renderPedalboard();
}

function moveSlot(slotId, delta) {
  const idx = state.chain.findIndex((s) => s.id === slotId);
  if (idx < 0) return;
  const target = idx + delta;
  if (target < 0 || target >= state.chain.length) return;
  const [item] = state.chain.splice(idx, 1);
  state.chain.splice(target, 0, item);
  if (state.worklet) {
    state.worklet.port.postMessage({
      type: "reorderSlots",
      order: state.chain.map((s) => s.id),
    });
  }
  renderPedalboard();
}

function clearChain() {
  state.chain = [];
  if (state.worklet) {
    state.worklet.port.postMessage({ type: "clearSlots" });
  }
  renderPedalboard();
}

async function loadPreset(presetKey) {
  const slugs = PRESETS[presetKey];
  if (!slugs) return;
  clearChain();
  for (const slug of slugs) {
    if (state.effectsBySlug.has(slug)) {
      await addEffectToChain(slug, false);
    }
  }
}

function setSlotParam(slotObj, param, intVal) {
  const clamped = Math.max(param.min, Math.min(param.max, Math.round(intVal)));
  slotObj.values.set(param.index, clamped);
  if (state.worklet) {
    state.worklet.port.postMessage({
      type: "setParam",
      slotId: slotObj.id,
      coeffSlot: param.slot,
      coeff: paramToCoeff(param, clamped),
    });
  }
}

function setSlotBypass(slotObj, bypassed) {
  slotObj.bypassed = Boolean(bypassed);
  if (state.worklet) {
    state.worklet.port.postMessage({
      type: "setBypass",
      slotId: slotObj.id,
      bypassed: slotObj.bypassed,
    });
  }
}

function isSlotMomentary(slotObj) {
  const toggleParam = slotObj.effect.params.find((p) => p.footswitchToggle);
  if (!toggleParam) return false;
  return slotObj.values.get(toggleParam.index) === 1; // 1 = UNLATCH
}

// ---- Sample Player ----
async function loadSampleUrl(url, label) {
  await ensureAudio();
  if (state.sampleBuffers.has(url)) {
    state.currentBuffer = state.sampleBuffers.get(url);
    state.currentBufferLabel = label;
    state.sampleOffset = 0;
    updateTimeDisplay();
    return;
  }
  const res = await fetch(url);
  const arr = await res.arrayBuffer();
  const audioBuf = await state.ctx.decodeAudioData(arr);
  state.sampleBuffers.set(url, audioBuf);
  state.currentBuffer = audioBuf;
  state.currentBufferLabel = label;
  state.sampleOffset = 0;
  updateTimeDisplay();
}

async function loadLocalAudioFile(file) {
  await ensureAudio();
  const arr = await file.arrayBuffer();
  const audioBuf = await state.ctx.decodeAudioData(arr);
  state.currentBuffer = audioBuf;
  state.currentBufferLabel = file.name;
  state.sampleOffset = 0;

  // Add option to dropdown
  const sel = $("sampleSelect");
  const opt = document.createElement("option");
  opt.value = "__custom__";
  opt.textContent = `📁 ${file.name}`;
  const existing = sel.querySelector('option[value="__custom__"]');
  if (existing) existing.remove();
  sel.appendChild(opt);
  sel.value = "__custom__";

  switchSourceTab("sample");
  playSample();
}

function stopSampleNodeOnly() {
  if (state.sampleNode) {
    try {
      state.sampleNode.onended = null;
      state.sampleNode.stop();
      state.sampleNode.disconnect();
    } catch (_) {}
    state.sampleNode = null;
  }
}

function getSamplePlayhead() {
  if (!state.currentBuffer) return 0;
  const dur = state.currentBuffer.duration;
  if (!state.samplePlaying || !state.ctx) return state.sampleOffset;
  const elapsed = state.ctx.currentTime - state.sampleStartTime;
  const pos = state.sampleOffset + elapsed;
  return state.sampleLoop && dur > 0 ? pos % dur : Math.min(pos, dur);
}

async function playSample() {
  await ensureAudio();
  if (!state.currentBuffer) {
    const sel = $("sampleSelect");
    const opt = sel.options[sel.selectedIndex];
    if (opt && opt.value && opt.value !== "__custom__") {
      await loadSampleUrl(opt.value, opt.textContent);
    }
  }
  if (!state.currentBuffer) return;

  stopSampleNodeOnly();
  const src = state.ctx.createBufferSource();
  src.buffer = state.currentBuffer;
  src.loop = state.sampleLoop;
  src.connect(state.inputBus);

  const dur = state.currentBuffer.duration;
  const offset = state.sampleOffset >= dur - 0.02 ? 0 : state.sampleOffset;
  state.sampleOffset = offset;
  state.sampleStartTime = state.ctx.currentTime;
  state.samplePlaying = true;
  state.sampleNode = src;

  src.onended = () => {
    if (state.sampleNode === src && !state.sampleLoop) {
      state.samplePlaying = false;
      state.sampleOffset = 0;
      $("playSampleBtn").textContent = "▶ Play";
    }
  };

  src.start(0, offset);
  $("playSampleBtn").textContent = "⏸ Pause";
}

function pauseSample() {
  if (!state.samplePlaying) return;
  state.sampleOffset = getSamplePlayhead();
  state.samplePlaying = false;
  stopSampleNodeOnly();
  $("playSampleBtn").textContent = "▶ Play";
}

function updateTimeDisplay() {
  if (!state.currentBuffer) {
    $("timeDisplay").textContent = "0:00 / 0:00";
    $("scrubSlider").value = 0;
    return;
  }
  const dur = state.currentBuffer.duration;
  const pos = getSamplePlayhead();
  $("timeDisplay").textContent = `${formatTime(pos)} / ${formatTime(dur)}`;
  if (dur > 0) {
    $("scrubSlider").value = Math.round((pos / dur) * 1000);
  }
}

// ---- Test Synth (TB-303 Acid Bassline Engine & 16-Step Sequencer) ----
function midiToFreq(m) {
  return 440 * Math.pow(2, (m - 69) / 12);
}

// Each step: { n: midiNote (or null for rest), a: accent, s: slide }
const ACID_PATTERNS = {
  acid1: [
    { n: 36, a: 1, s: 0 }, { n: 36, a: 0, s: 0 }, { n: 48, a: 1, s: 1 }, { n: 36, a: 0, s: 0 },
    { n: 39, a: 0, s: 0 }, { n: 36, a: 0, s: 1 }, { n: 43, a: 1, s: 0 }, { n: null, a: 0, s: 0 },
    { n: 36, a: 0, s: 0 }, { n: 46, a: 1, s: 1 }, { n: 48, a: 1, s: 0 }, { n: 39, a: 0, s: 0 },
    { n: 43, a: 1, s: 1 }, { n: 41, a: 0, s: 0 }, { n: 39, a: 1, s: 0 }, { n: 36, a: 0, s: 0 },
  ],
  acid2: [
    { n: 33, a: 1, s: 0 }, { n: null, a: 0, s: 0 }, { n: 45, a: 1, s: 1 }, { n: 43, a: 0, s: 0 },
    { n: 33, a: 0, s: 0 }, { n: 36, a: 1, s: 0 }, { n: 33, a: 0, s: 1 }, { n: 40, a: 1, s: 0 },
    { n: 33, a: 0, s: 0 }, { n: 45, a: 1, s: 0 }, { n: null, a: 0, s: 0 }, { n: 48, a: 1, s: 1 },
    { n: 45, a: 0, s: 0 }, { n: 40, a: 1, s: 1 }, { n: 36, a: 0, s: 0 }, { n: 38, a: 1, s: 0 },
  ],
  ebm: [
    { n: 36, a: 1, s: 0 }, { n: 36, a: 0, s: 0 }, { n: 48, a: 1, s: 0 }, { n: 36, a: 0, s: 0 },
    { n: 36, a: 1, s: 0 }, { n: 36, a: 0, s: 0 }, { n: 46, a: 1, s: 0 }, { n: 48, a: 0, s: 0 },
    { n: 36, a: 1, s: 0 }, { n: 36, a: 0, s: 0 }, { n: 48, a: 1, s: 1 }, { n: 43, a: 0, s: 0 },
    { n: 39, a: 1, s: 0 }, { n: 39, a: 0, s: 0 }, { n: 41, a: 1, s: 0 }, { n: 43, a: 1, s: 0 },
  ],
  minimal: [
    { n: 36, a: 1, s: 0 }, { n: null, a: 0, s: 0 }, { n: null, a: 0, s: 0 }, { n: 36, a: 0, s: 1 },
    { n: 37, a: 1, s: 0 }, { n: null, a: 0, s: 0 }, { n: 48, a: 1, s: 1 }, { n: 36, a: 0, s: 0 },
    { n: null, a: 0, s: 0 }, { n: 36, a: 0, s: 0 }, { n: 43, a: 1, s: 0 }, { n: null, a: 0, s: 0 },
    { n: 37, a: 0, s: 1 }, { n: 36, a: 1, s: 0 }, { n: null, a: 0, s: 0 }, { n: 46, a: 1, s: 0 },
  ],
};

let currentAcidPattern = ACID_PATTERNS.acid1.map((s) => ({ ...s }));
let acidVoice = null; // persistent mono 303 voice { osc, f1, f2, shaper, vca, lastFreq }
let acidNextStepTime = 0;

function makeSoftClipCurve(amount = 2.2) {
  const n = 256;
  const curve = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const x = (i * 2) / (n - 1) - 1;
    curve[i] = Math.tanh(x * amount) / Math.tanh(amount);
  }
  return curve;
}

function ensureAcidVoice() {
  if (acidVoice) return acidVoice;
  const ctx = state.ctx;
  const osc = ctx.createOscillator();
  osc.type = "sawtooth";
  osc.frequency.value = 65.41;

  // Cascaded 24dB/oct-style resonant lowpass for proper 303 squelch
  const f1 = ctx.createBiquadFilter();
  f1.type = "lowpass";
  f1.frequency.value = 320;
  f1.Q.value = 8.0;

  const f2 = ctx.createBiquadFilter();
  f2.type = "lowpass";
  f2.frequency.value = 320;
  f2.Q.value = 4.5;

  // Subtle diode-ladder analog saturation
  const shaper = ctx.createWaveShaper();
  shaper.curve = makeSoftClipCurve(2.4);
  shaper.oversample = "2x";

  const vca = ctx.createGain();
  vca.gain.value = 0.0;

  osc.connect(f1);
  f1.connect(f2);
  f2.connect(shaper);
  shaper.connect(vca);
  vca.connect(state.inputBus);
  osc.start();

  acidVoice = { osc, f1, f2, shaper, vca, lastFreq: 65.41 };
  return acidVoice;
}

function scheduleAcidStep(freq, when, stepDur, accent, slide) {
  const v = ensureAcidVoice();
  const voiceMode = $("synthVoiceSelect").value;
  v.osc.type = voiceMode === "acid_sqr" ? "square" : "sawtooth";

  const cutNorm = Number($("acidCutoffSlider")?.value ?? 38) / 100;
  const resNorm = Number($("acidResSlider")?.value ?? 72) / 100;

  // Exponential cutoff 90 Hz .. 2800 Hz
  const baseCut = 90 * Math.pow(31, cutNorm);
  const peakCut = Math.min(11000, baseCut * (accent ? 7.8 : 4.2));
  const q1 = 1.0 + resNorm * 14.5 + (accent ? 2.5 : 0);
  const q2 = 0.8 + resNorm * 7.5;
  const dec = accent ? 0.14 : 0.21;
  const gate = slide ? stepDur * 1.05 : stepDur * 0.62;

  v.f1.Q.setValueAtTime(q1, when);
  v.f2.Q.setValueAtTime(q2, when);

  if (slide) {
    v.osc.frequency.cancelScheduledValues(when);
    v.osc.frequency.setValueAtTime(v.lastFreq, when);
    v.osc.frequency.exponentialRampToValueAtTime(freq, when + Math.min(0.065, stepDur * 0.5));
  } else {
    v.osc.frequency.cancelScheduledValues(when);
    v.osc.frequency.setValueAtTime(freq, when);
  }
  v.lastFreq = freq;

  // Filter envelope
  v.f1.frequency.cancelScheduledValues(when);
  v.f2.frequency.cancelScheduledValues(when);
  if (!slide) {
    v.f1.frequency.setValueAtTime(baseCut, when);
    v.f1.frequency.linearRampToValueAtTime(peakCut, when + 0.004);
    v.f1.frequency.exponentialRampToValueAtTime(Math.max(70, baseCut), when + dec);

    v.f2.frequency.setValueAtTime(baseCut * 1.15, when);
    v.f2.frequency.linearRampToValueAtTime(Math.min(12000, peakCut * 1.15), when + 0.004);
    v.f2.frequency.exponentialRampToValueAtTime(Math.max(80, baseCut * 1.15), when + dec);
  } else {
    // Slid notes keep filter gliding smoothly
    v.f1.frequency.exponentialRampToValueAtTime(Math.max(70, baseCut * (accent ? 2.6 : 1.4)), when + dec);
    v.f2.frequency.exponentialRampToValueAtTime(Math.max(80, baseCut * (accent ? 2.8 : 1.5)), when + dec);
  }

  // VCA envelope
  const peakAmp = accent ? 0.56 : 0.38;
  v.vca.gain.cancelScheduledValues(when);
  if (!slide) {
    v.vca.gain.setValueAtTime(0.001, when);
    v.vca.gain.linearRampToValueAtTime(peakAmp, when + 0.004);
    v.vca.gain.setTargetAtTime(0.0001, when + gate, 0.018);
  } else {
    v.vca.gain.linearRampToValueAtTime(peakAmp, when + 0.015);
    v.vca.gain.setTargetAtTime(0.0001, when + gate, 0.025);
  }
}

async function triggerSynthNote(freq, duration = 0.45) {
  await ensureAudio();
  const ctx = state.ctx;
  const now = ctx.currentTime;
  const voice = $("synthVoiceSelect").value;

  if (voice === "acid_saw" || voice === "acid_sqr") {
    // Play one octave down on keyboard so C3..C4 hits classic 303 bass register
    scheduleAcidStep(freq * 0.5, now, duration, true, false);
    return;
  }

  const env = ctx.createGain();
  env.connect(state.inputBus);
  const osc1 = ctx.createOscillator();
  const osc2 = ctx.createOscillator();
  const filt = ctx.createBiquadFilter();
  filt.type = "lowpass";

  if (voice === "pluck") {
    osc1.type = "sawtooth";
    osc2.type = "triangle";
    osc1.frequency.setValueAtTime(freq, now);
    osc2.frequency.setValueAtTime(freq * 2.002, now);
    filt.frequency.setValueAtTime(3400, now);
    filt.frequency.exponentialRampToValueAtTime(380, now + 0.35);
    env.gain.setValueAtTime(0.001, now);
    env.gain.linearRampToValueAtTime(0.42, now + 0.006);
    env.gain.exponentialRampToValueAtTime(0.001, now + duration);
  } else {
    // Warm Stereo Pad
    osc1.type = "sawtooth";
    osc2.type = "sawtooth";
    osc1.frequency.setValueAtTime(freq, now);
    osc2.frequency.setValueAtTime(freq * 1.007, now);
    filt.frequency.setValueAtTime(1400, now);
    env.gain.setValueAtTime(0.001, now);
    env.gain.linearRampToValueAtTime(0.28, now + 0.16);
    env.gain.exponentialRampToValueAtTime(0.001, now + duration * 1.9);
    duration *= 1.9;
  }

  osc1.connect(filt);
  osc2.connect(filt);
  filt.connect(env);
  osc1.start(now);
  osc2.start(now);
  osc1.stop(now + duration + 0.05);
  osc2.stop(now + duration + 0.05);
}

function randomizeAcidPattern() {
  // Minor / Phrygian acid scale around C2 (MIDI 36)
  const scale = [36, 36, 36, 37, 39, 41, 43, 46, 48, 48, 51];
  currentAcidPattern = Array.from({ length: 16 }, (_, i) => {
    const rest = i !== 0 && Math.random() < 0.22;
    const n = rest ? null : i === 0 ? 36 : scale[Math.floor(Math.random() * scale.length)];
    return {
      n,
      a: !rest && (i % 4 === 0 || Math.random() < 0.35) ? 1 : 0,
      s: !rest && i > 0 && Math.random() < 0.28 ? 1 : 0,
    };
  });
}

async function toggleAutoRiff() {
  const btn = $("autoRiffBtn");
  if (state.autoRiffTimer) {
    clearInterval(state.autoRiffTimer);
    state.autoRiffTimer = null;
    if (acidVoice && state.ctx) {
      acidVoice.vca.gain.setTargetAtTime(0.0, state.ctx.currentTime, 0.015);
    }
    btn.textContent = "▶ 303 Loop";
    btn.classList.remove("btn-toggle", "active");
    btn.classList.add("btn-primary");
    return;
  }

  await ensureAudio();
  state.autoRiffStep = 0;
  acidNextStepTime = state.ctx.currentTime + 0.04;
  btn.textContent = "■ Stop 303";
  btn.classList.remove("btn-primary");
  btn.classList.add("btn-toggle", "active");

  // Lookahead 16th-note scheduler locked to state.bpm
  state.autoRiffTimer = setInterval(() => {
    if (!state.ctx) return;
    const stepDur = 60.0 / state.bpm / 4.0; // 16th note at current toolbar BPM
    while (acidNextStepTime < state.ctx.currentTime + 0.12) {
      const idx = state.autoRiffStep % currentAcidPattern.length;
      const step = currentAcidPattern[idx];
      const prevStep = currentAcidPattern[(idx + 15) % 16];
      if (step && step.n !== null) {
        const freq = midiToFreq(step.n);
        const voice = $("synthVoiceSelect").value;
        if (voice === "acid_saw" || voice === "acid_sqr") {
          scheduleAcidStep(freq, acidNextStepTime, stepDur, Boolean(step.a), Boolean(prevStep && prevStep.s));
        } else {
          triggerSynthNote(freq * 2, stepDur * 1.6);
        }
      }
      acidNextStepTime += stepDur;
      state.autoRiffStep++;
    }
  }, 25);
}

// ---- Live Mic / Guitar ----
async function toggleMicInput() {
  const btn = $("micEnableBtn");
  const status = $("micStatus");
  const devSel = $("micDeviceSelect");

  if (state.micStream) {
    state.micStream.getTracks().forEach((t) => t.stop());
    if (state.micNode) {
      state.micNode.disconnect();
      state.micNode = null;
    }
    state.micStream = null;
    btn.textContent = "🎙 Enable Live Input";
    status.textContent = "Stopped";
    status.classList.remove("running");
    return;
  }

  try {
    await ensureAudio();
    const deviceId = devSel.value || undefined;
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false,
      },
    });
    const node = state.ctx.createMediaStreamSource(stream);
    node.connect(state.inputBus);
    state.micStream = stream;
    state.micNode = node;

    btn.textContent = "■ Stop Live Input";
    status.textContent = "Live Input Active";
    status.classList.add("running");

    // Populate audio input device list now that permission is granted
    const devices = await navigator.mediaDevices.enumerateDevices();
    const inputs = devices.filter((d) => d.kind === "audioinput");
    devSel.innerHTML = "";
    for (const d of inputs) {
      const opt = document.createElement("option");
      opt.value = d.deviceId;
      opt.textContent = d.label || `Audio Input (${d.deviceId.slice(0, 6)})`;
      devSel.appendChild(opt);
    }
    devSel.disabled = false;
  } catch (err) {
    status.textContent = `Mic error: ${err.message || err}`;
  }
}

// ---- UI Rendering ----
function renderCategories() {
  const cats = ["All", ...new Set(state.catalog.effects.map((e) => e.category))];
  const wrap = $("categoryPills");
  wrap.innerHTML = "";
  for (const cat of cats) {
    const b = document.createElement("button");
    b.className = "cat-pill" + (cat === state.selectedCategory ? " active" : "");
    b.textContent = cat;
    b.onclick = () => {
      state.selectedCategory = cat;
      renderCategories();
      renderSidebarEffects();
    };
    wrap.appendChild(b);
  }
}

function renderSidebarEffects() {
  const list = $("effectList");
  const q = state.searchQuery.trim().toLowerCase();
  const filtered = state.catalog.effects.filter((fx) => {
    if (state.selectedCategory !== "All" && fx.category !== state.selectedCategory) return false;
    if (!q) return true;
    return (
      fx.name.toLowerCase().includes(q) ||
      fx.display.toLowerCase().includes(q) ||
      fx.slug.toLowerCase().includes(q) ||
      fx.description.toLowerCase().includes(q) ||
      (fx.zd2 && fx.zd2.toLowerCase().includes(q))
    );
  });

  $("effectCount").textContent = String(filtered.length);
  list.innerHTML = "";

  for (const fx of filtered) {
    const item = document.createElement("div");
    item.className = "effect-item";
    item.title = `${fx.description}\n\nTip: Drag onto the pedalboard to insert into the chain.`;
    item.draggable = true;

    item.addEventListener("dragstart", (e) => {
      state.dragPayload = { kind: "new", slug: fx.slug };
      e.dataTransfer.effectAllowed = "copy";
      e.dataTransfer.setData("application/x-stomphacks-effect", fx.slug);
      item.classList.add("dragging");
    });

    item.addEventListener("dragend", () => {
      item.classList.remove("dragging");
      clearDropIndicators();
      state.dragPayload = null;
    });

    const grip = document.createElement("span");
    grip.className = "drag-grip";
    grip.textContent = "⋮⋮";

    const icon = document.createElement("div");
    icon.className = "effect-icon";
    if (fx.iconUrl) {
      const img = document.createElement("img");
      img.src = fx.iconUrl;
      img.alt = fx.badge;
      img.draggable = false;
      icon.appendChild(img);
    } else {
      icon.textContent = fx.badge;
    }

    const meta = document.createElement("div");
    meta.className = "effect-meta";
    meta.innerHTML = `
      <div class="effect-title">${fx.name}</div>
      <div class="effect-sub">
        <span>${fx.zd2 || fx.filename + ".ZD2"}</span>
        <span>${fx.params.length}p</span>
        <span>${(fx.wasmBytes / 1024).toFixed(1)}KB</span>
      </div>
    `;

    const actions = document.createElement("div");
    actions.className = "effect-actions";

    const loadBtn = document.createElement("button");
    loadBtn.className = "btn btn-sm btn-primary";
    loadBtn.textContent = "Load";
    loadBtn.title = "Audition this effect alone";
    loadBtn.onclick = () => addEffectToChain(fx.slug, true);

    const addBtn = document.createElement("button");
    addBtn.className = "btn btn-sm btn-ghost";
    addBtn.textContent = "+ Chain";
    addBtn.title = "Append to signal chain";
    addBtn.onclick = () => addEffectToChain(fx.slug, false);

    actions.append(loadBtn, addBtn);
    item.append(grip, icon, meta, actions);
    list.appendChild(item);
  }
}

function createKnobControl(slotObj, param) {
  const cell = document.createElement("div");
  cell.className = "param-cell";
  cell.title = param.explanation || param.name;

  const label = document.createElement("div");
  label.className = "param-label";
  label.textContent = param.name;

  const wrap = document.createElement("div");
  wrap.className = "knob-wrap";

  const valEl = document.createElement("div");
  valEl.className = "param-val";

  // SVG rotary knob (-135 deg to +135 deg)
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 60 60");
  svg.classList.add("knob-svg");

  const r = 22;
  const cx = 30;
  const cy = 30;
  const startAngle = -225; // degrees
  const sweep = 270;

  function polar(deg) {
    const rad = (deg * Math.PI) / 180;
    return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
  }

  function arcPath(a0, a1) {
    const [x0, y0] = polar(a0);
    const [x1, y1] = polar(a1);
    const large = Math.abs(a1 - a0) > 180 ? 1 : 0;
    return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
  }

  const defs = document.createElementNS(svgNS, "defs");
  defs.innerHTML = `<linearGradient id="knobGrad" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0%" stop-color="#3b475e"/><stop offset="100%" stop-color="#181f2c"/>
  </linearGradient>`;

  const track = document.createElementNS(svgNS, "path");
  track.setAttribute("class", "knob-track");
  track.setAttribute("d", arcPath(startAngle, startAngle + sweep));

  const arc = document.createElementNS(svgNS, "path");
  arc.setAttribute("class", "knob-arc");

  const cap = document.createElementNS(svgNS, "circle");
  cap.setAttribute("class", "knob-cap");
  cap.setAttribute("cx", cx);
  cap.setAttribute("cy", cy);
  cap.setAttribute("r", 15);

  const pointer = document.createElementNS(svgNS, "line");
  pointer.setAttribute("class", "knob-pointer");

  svg.append(defs, track, arc, cap, pointer);
  wrap.appendChild(svg);

  function updateVisual(val) {
    const norm = param.max > 0 ? val / param.max : 0;
    const deg = startAngle + norm * sweep;
    if (norm <= 0.001) {
      arc.setAttribute("d", "");
    } else {
      arc.setAttribute("d", arcPath(startAngle, deg));
    }
    const rad = (deg * Math.PI) / 180;
    pointer.setAttribute("x1", (cx + 4 * Math.cos(rad)).toFixed(2));
    pointer.setAttribute("y1", (cy + 4 * Math.sin(rad)).toFixed(2));
    pointer.setAttribute("x2", (cx + 13 * Math.cos(rad)).toFixed(2));
    pointer.setAttribute("y2", (cy + 13 * Math.sin(rad)).toFixed(2));
    valEl.textContent = formatKnobValue(slotObj, param, val);
  }

  updateVisual(slotObj.values.get(param.index));

  // Vertical drag interaction
  let dragging = false;
  let startY = 0;
  let startVal = 0;

  wrap.addEventListener("pointerdown", (e) => {
    dragging = true;
    startY = e.clientY;
    startVal = slotObj.values.get(param.index);
    wrap.setPointerCapture(e.pointerId);
  });

  wrap.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const dy = startY - e.clientY;
    const range = param.max - param.min;
    const sensitivity = e.shiftKey ? 450 : 150;
    const nextVal = Math.round(startVal + (dy / sensitivity) * range);
    const clamped = Math.max(param.min, Math.min(param.max, nextVal));
    if (clamped !== slotObj.values.get(param.index)) {
      setSlotParam(slotObj, param, clamped);
      updateVisual(clamped);
    }
  });

  wrap.addEventListener("pointerup", () => {
    dragging = false;
  });

  wrap.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      const step = Math.max(1, Math.round((param.max - param.min) / 40));
      const cur = slotObj.values.get(param.index);
      const next = cur + (e.deltaY < 0 ? step : -step);
      const clamped = Math.max(param.min, Math.min(param.max, next));
      setSlotParam(slotObj, param, clamped);
      updateVisual(clamped);
    },
    { passive: false }
  );

  wrap.addEventListener("dblclick", () => {
    setSlotParam(slotObj, param, param.default);
    updateVisual(param.default);
  });

  cell.append(label, wrap, valEl);
  return cell;
}

function createSelectorControl(slotObj, param, onChangeExtra) {
  const cell = document.createElement("div");
  cell.className = "param-cell wide";
  cell.title = param.explanation || param.name;

  const label = document.createElement("div");
  label.className = "param-label";
  label.textContent = param.name;

  const group = document.createElement("div");
  group.className = "seg-group";

  const cur = slotObj.values.get(param.index);
  param.values.forEach((vLabel, idx) => {
    const b = document.createElement("button");
    b.className = "seg-btn" + (idx === cur ? " active" : "");
    b.textContent = vLabel;
    b.onclick = () => {
      setSlotParam(slotObj, param, idx);
      group.querySelectorAll(".seg-btn").forEach((el, j) => {
        el.classList.toggle("active", j === idx);
      });
      if (onChangeExtra) onChangeExtra();
    };
    group.appendChild(b);
  });

  cell.append(label, group);
  return cell;
}

function clearDropIndicators() {
  const board = $("pedalboard");
  if (!board) return;
  board.classList.remove("drag-over");
  board.querySelectorAll(".pedal-card.drop-before, .pedal-card.drop-after").forEach((el) => {
    el.classList.remove("drop-before", "drop-after");
  });
}

function getDropInsertIndex(board, clientX) {
  const cards = Array.from(board.querySelectorAll(".pedal-card"));
  for (let i = 0; i < cards.length; i++) {
    const rect = cards[i].getBoundingClientRect();
    const midX = rect.left + rect.width / 2;
    if (clientX < midX) return i;
  }
  return cards.length;
}

function showDropIndicator(board, insertIndex) {
  clearDropIndicators();
  board.classList.add("drag-over");
  const cards = Array.from(board.querySelectorAll(".pedal-card"));
  if (cards.length === 0) return;
  if (insertIndex < cards.length) {
    cards[insertIndex].classList.add("drop-before");
  } else {
    cards[cards.length - 1].classList.add("drop-after");
  }
}

function renderPedalboard() {
  const board = $("pedalboard");
  board.innerHTML = "";

  const summary = $("chainSummary");
  if (state.chain.length === 0) {
    summary.textContent = "IN → (bypass) → OUT";
    board.innerHTML = `
      <div class="empty-board">
        <h3>Pedalboard Empty</h3>
        <p><strong>Drag &amp; drop</strong> any ZD2 effect from the left onto this board, click <strong>Load</strong> / <strong>+ Chain</strong>, or pick a preset above.</p>
      </div>
    `;
    return;
  }

  summary.textContent = "IN → " + state.chain.map((s) => s.effect.display).join(" → ") + " → OUT";

  state.chain.forEach((slotObj, idx) => {
    const fx = slotObj.effect;
    const card = document.createElement("div");
    card.className = "pedal-card" + (slotObj.bypassed ? " bypassed" : "");
    card.dataset.slotId = String(slotObj.id);

    // Header (draggable handle to reorder chain without interfering with knobs)
    const head = document.createElement("div");
    head.className = "pedal-head";
    head.draggable = true;
    head.title = "Drag to reorder in signal chain";

    head.addEventListener("dragstart", (e) => {
      if (e.target.closest("button")) {
        e.preventDefault();
        return;
      }
      state.dragPayload = { kind: "move", slotId: slotObj.id };
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("application/x-stomphacks-slot", String(slotObj.id));
      if (e.dataTransfer.setDragImage) {
        e.dataTransfer.setDragImage(card, 40, 20);
      }
      card.classList.add("dragging");
    });

    head.addEventListener("dragend", () => {
      card.classList.remove("dragging");
      clearDropIndicators();
      state.dragPayload = null;
    });

    const titleWrap = document.createElement("div");
    titleWrap.className = "pedal-title-wrap";
    const grip = document.createElement("span");
    grip.className = "drag-grip";
    grip.textContent = "⋮⋮";

    const titles = document.createElement("div");
    titles.style.minWidth = "0";
    titles.innerHTML = `
      <div class="pedal-name">${fx.display}</div>
      <div class="pedal-zd2">${fx.zd2 || fx.filename + ".ZD2"} · ${fx.dspload}% DSP</div>
    `;
    titleWrap.append(grip, titles);

    const tools = document.createElement("div");
    tools.className = "pedal-tools";

    const leftBtn = document.createElement("button");
    leftBtn.className = "icon-btn";
    leftBtn.textContent = "←";
    leftBtn.title = "Move earlier in chain";
    leftBtn.disabled = idx === 0;
    leftBtn.onclick = () => moveSlot(slotObj.id, -1);

    const rightBtn = document.createElement("button");
    rightBtn.className = "icon-btn";
    rightBtn.textContent = "→";
    rightBtn.title = "Move later in chain";
    rightBtn.disabled = idx === state.chain.length - 1;
    rightBtn.onclick = () => moveSlot(slotObj.id, 1);

    const resetBtn = document.createElement("button");
    resetBtn.className = "icon-btn";
    resetBtn.textContent = "↺";
    resetBtn.title = "Reset parameters to defaults";
    resetBtn.onclick = () => {
      for (const p of fx.params) setSlotParam(slotObj, p, p.default);
      renderPedalboard();
    };

    const rmBtn = document.createElement("button");
    rmBtn.className = "icon-btn danger";
    rmBtn.textContent = "×";
    rmBtn.title = "Remove from chain";
    rmBtn.onclick = () => removeSlot(slotObj.id);

    tools.append(leftBtn, rightBtn, resetBtn, rmBtn);
    head.append(titleWrap, tools);

    // Prominent pixel-art LCD hero + description
    const hero = document.createElement("div");
    hero.className = "pedal-hero";

    const artFrame = document.createElement("div");
    artFrame.className = "pedal-art";
    if (fx.iconUrl) {
      const img = document.createElement("img");
      img.src = fx.iconUrl;
      img.alt = fx.display;
      img.draggable = false;
      artFrame.appendChild(img);
    } else {
      artFrame.textContent = fx.badge;
    }

    const desc = document.createElement("div");
    desc.className = "pedal-desc";
    desc.textContent = fx.description;
    desc.title = fx.description;

    hero.append(artFrame, desc);

    // Footswitch footer (created early so ON/OFF selector can update label)
    const footer = document.createElement("div");
    footer.className = "pedal-footer";
    const fswBtn = document.createElement("button");
    fswBtn.className = "footswitch-btn";

    function refreshFootswitchUI() {
      const mom = isSlotMomentary(slotObj);
      card.classList.toggle("bypassed", slotObj.bypassed);
      const modeTag = mom ? "HOLD (UNLATCH)" : slotObj.bypassed ? "BYPASSED" : "ENGAGED";
      fswBtn.innerHTML = `<span class="footswitch-led"></span> ${modeTag}`;
    }

    // Params
    const paramsGrid = document.createElement("div");
    paramsGrid.className = "pedal-params";
    for (const p of fx.params) {
      if (Array.isArray(p.values) && p.values.length > 0) {
        paramsGrid.appendChild(
          createSelectorControl(slotObj, p, () => {
            if (p.footswitchToggle) {
              // When switching to UNLATCH, disengage until held
              if (isSlotMomentary(slotObj)) setSlotBypass(slotObj, true);
              refreshFootswitchUI();
            } else if (BEAT_SYNC_SLUGS.has(slotObj.slug) && p.name === "Sync") {
              // Sync Time knob to global BPM when switching into a beat division
              if (slotObj.values.get(p.index) > 0) {
                applyBpmToSyncedSlots(state.bpm);
              } else {
                renderPedalboard();
              }
            }
          })
        );
      } else {
        paramsGrid.appendChild(createKnobControl(slotObj, p));
      }
    }

    // Footswitch interaction (supports both LATCH and momentary UNLATCH)
    fswBtn.addEventListener("pointerdown", () => {
      if (isSlotMomentary(slotObj)) {
        setSlotBypass(slotObj, false);
        refreshFootswitchUI();
      } else {
        setSlotBypass(slotObj, !slotObj.bypassed);
        refreshFootswitchUI();
      }
    });

    const releaseMomentary = () => {
      if (isSlotMomentary(slotObj) && !slotObj.bypassed) {
        setSlotBypass(slotObj, true);
        refreshFootswitchUI();
      }
    };
    fswBtn.addEventListener("pointerup", releaseMomentary);
    fswBtn.addEventListener("pointerleave", releaseMomentary);

    refreshFootswitchUI();
    footer.appendChild(fswBtn);

    card.append(head, hero, paramsGrid, footer);
    board.appendChild(card);
  });
}

// ---- Meters & Oscilloscope ----
function updateMeters(inL, inR, outL, outR) {
  const pct = (v) => Math.min(100, Math.round(Math.pow(Math.min(v, 1.2), 0.65) * 90));
  $("meterInL").style.width = `${pct(inL)}%`;
  $("meterInR").style.width = `${pct(inR)}%`;
  $("meterOutL").style.width = `${pct(outL)}%`;
  $("meterOutR").style.width = `${pct(outR)}%`;
}

function startAnimationLoop() {
  const canvas = $("scopeCanvas");
  const g = canvas.getContext("2d");
  const timeBuf = new Float32Array(512);
  const freqBuf = new Uint8Array(512);

  function tick() {
    requestAnimationFrame(tick);

    if (state.samplePlaying) {
      updateTimeDisplay();
    }

    const w = canvas.width;
    const h = canvas.height;
    g.fillStyle = "#0a141c";
    g.fillRect(0, 0, w, h);

    // Subtle grid center line
    g.strokeStyle = "rgba(56, 189, 248, 0.14)";
    g.lineWidth = 1;
    g.beginPath();
    g.moveTo(0, h / 2);
    g.lineTo(w, h / 2);
    g.stroke();

    if (!state.analyser) return;

    if (state.scopeMode === "scope") {
      state.analyser.getFloatTimeDomainData(timeBuf);
      g.strokeStyle = "#38bdf8";
      g.lineWidth = 1.5;
      g.beginPath();
      const step = w / timeBuf.length;
      for (let i = 0; i < timeBuf.length; i++) {
        const y = (0.5 - timeBuf[i] * 0.45) * h;
        if (i === 0) g.moveTo(0, y);
        else g.lineTo(i * step, y);
      }
      g.stroke();
    } else {
      state.analyser.getByteFrequencyData(freqBuf);
      const bins = 64;
      const barW = w / bins;
      g.fillStyle = "#38bdf8";
      for (let i = 0; i < bins; i++) {
        const v = freqBuf[i * 2] / 255;
        const bh = v * (h - 4);
        g.fillRect(i * barW, h - bh, barW - 1, bh);
      }
    }
  }
  tick();
}

function switchSourceTab(tab) {
  state.activeSourceTab = tab;
  document.querySelectorAll(".tab-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.source === tab);
  });
  document.querySelectorAll(".source-panel").forEach((p) => {
    p.classList.toggle("active", p.id === `panel-${tab}`);
  });
}

function buildMiniKeyboard() {
  const kb = $("miniKeyboard");
  kb.innerHTML = "";
  for (const n of KEYBOARD_NOTES) {
    const b = document.createElement("button");
    b.className = "key-btn" + (n.black ? " black" : "");
    b.textContent = n.key.toUpperCase();
    b.dataset.key = n.key;
    b.title = `${n.note} (${n.key.toUpperCase()})`;
    b.addEventListener("pointerdown", () => {
      b.classList.add("pressed");
      triggerSynthNote(n.freq);
    });
    const up = () => b.classList.remove("pressed");
    b.addEventListener("pointerup", up);
    b.addEventListener("pointerleave", up);
    kb.appendChild(b);
  }
}

// ---- Wire Events & Boot ----
async function init() {
  const res = await fetch("dist/effects.json");
  if (!res.ok) {
    document.body.innerHTML = `<div style="padding:24px;color:#f87171">Failed to load dist/effects.json. Run <code>python web/build_wasm.py</code> first.</div>`;
    return;
  }
  state.catalog = await res.json();
  for (const fx of state.catalog.effects) {
    state.effectsBySlug.set(fx.slug, fx);
  }

  // Populate dry samples dropdown
  const sel = $("sampleSelect");
  sel.innerHTML = "";
  if (state.catalog.samples.length === 0) {
    const opt = document.createElement("option");
    opt.value = "__custom__";
    opt.textContent = "No bundled samples — click + Load Audio";
    sel.appendChild(opt);
  } else {
    for (const s of state.catalog.samples) {
      const opt = document.createElement("option");
      opt.value = s.url;
      opt.textContent = s.label;
      sel.appendChild(opt);
    }
  }

  renderCategories();
  renderSidebarEffects();
  buildMiniKeyboard();

  // Default starter effect: XARVB (or first effect)
  const starter = state.effectsBySlug.has("xarvb")
    ? "xarvb"
    : state.catalog.effects[0]?.slug;
  if (starter) {
    await addEffectToChain(starter, true);
  }

  // Search
  $("searchInput").addEventListener("input", (e) => {
    state.searchQuery = e.target.value;
    renderSidebarEffects();
  });

  // Source tabs
  document.querySelectorAll(".tab-btn").forEach((b) => {
    b.addEventListener("click", () => switchSourceTab(b.dataset.source));
  });

  // Sample controls
  sel.addEventListener("change", async () => {
    const wasPlaying = state.samplePlaying;
    pauseSample();
    const opt = sel.options[sel.selectedIndex];
    if (opt && opt.value !== "__custom__") {
      await loadSampleUrl(opt.value, opt.textContent);
      if (wasPlaying) playSample();
    }
  });

  $("playSampleBtn").addEventListener("click", () => {
    if (state.samplePlaying) pauseSample();
    else playSample();
  });

  $("loopSampleBtn").addEventListener("click", () => {
    state.sampleLoop = !state.sampleLoop;
    $("loopSampleBtn").classList.toggle("active", state.sampleLoop);
    if (state.sampleNode) state.sampleNode.loop = state.sampleLoop;
  });

  $("scrubSlider").addEventListener("input", (e) => {
    if (!state.currentBuffer) return;
    const frac = Number(e.target.value) / 1000;
    state.sampleOffset = frac * state.currentBuffer.duration;
    if (state.samplePlaying) {
      playSample();
    } else {
      updateTimeDisplay();
    }
  });

  $("fileInput").addEventListener("change", (e) => {
    const file = e.target.files?.[0];
    if (file) loadLocalAudioFile(file);
  });

  // Synth (TB-303 Acid Sequencer)
  $("autoRiffBtn").addEventListener("click", toggleAutoRiff);
  $("acidPatternSelect").addEventListener("change", (e) => {
    const p = ACID_PATTERNS[e.target.value];
    if (p) currentAcidPattern = p.map((s) => ({ ...s }));
  });
  $("acidRandBtn").addEventListener("click", () => {
    randomizeAcidPattern();
  });

  // Mic
  $("micEnableBtn").addEventListener("click", toggleMicInput);

  // Master controls
  $("inGainSlider").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    state.inputGain = v / 100;
    $("inGainVal").textContent = `${v}%`;
    syncMasterToWorklet();
  });

  $("masterMixSlider").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    state.masterMix = v / 100;
    $("masterMixVal").textContent = `${v}%`;
    syncMasterToWorklet();
  });

  $("outGainSlider").addEventListener("input", (e) => {
    const v = Number(e.target.value);
    state.outputGain = v / 100;
    $("outGainVal").textContent = `${v}%`;
    syncMasterToWorklet();
  });

  $("globalBypassBtn").addEventListener("click", () => {
    state.globalBypass = !state.globalBypass;
    const btn = $("globalBypassBtn");
    btn.classList.toggle("bypassed", state.globalBypass);
    btn.innerHTML = `<span class="led"></span> ${state.globalBypass ? "BYPASSED" : "ACTIVE"}`;
    syncMasterToWorklet();
  });

  $("panicBtn").addEventListener("click", () => {
    if (state.worklet) {
      state.worklet.port.postMessage({ type: "resetAllAudio" });
    }
  });

  // Global BPM & Tap Tempo
  $("bpmInput").addEventListener("change", (e) => {
    const val = Number(e.target.value);
    if (Number.isFinite(val)) applyBpmToSyncedSlots(val);
  });

  $("tapTempoBtn").addEventListener("click", () => {
    const now = performance.now();
    if (state.tapTimes.length > 0 && now - state.tapTimes[state.tapTimes.length - 1] > 2000) {
      state.tapTimes = [];
    }
    state.tapTimes.push(now);
    if (state.tapTimes.length > 5) state.tapTimes.shift();
    if (state.tapTimes.length >= 2) {
      let sum = 0;
      for (let i = 1; i < state.tapTimes.length; i++) {
        sum += state.tapTimes[i] - state.tapTimes[i - 1];
      }
      const avgMs = sum / (state.tapTimes.length - 1);
      if (avgMs > 150) {
        applyBpmToSyncedSlots(60000 / avgMs);
      }
    }
  });

  // Presets & clear
  $("presetSelect").addEventListener("change", (e) => {
    if (e.target.value) {
      loadPreset(e.target.value);
      e.target.value = "";
    }
  });
  $("clearChainBtn").addEventListener("click", clearChain);

  // Scope mode toggle
  $("scopeCanvas").addEventListener("click", () => {
    state.scopeMode = state.scopeMode === "scope" ? "fft" : "scope";
    $("scopeModeLabel").textContent = state.scopeMode.toUpperCase();
  });

  // Pedalboard drag & drop (insert new effects from sidebar or reorder existing pedals)
  const boardEl = $("pedalboard");
  boardEl.addEventListener("dragover", (e) => {
    if (!state.dragPayload) return;
    e.preventDefault();
    e.stopPropagation();
    e.dataTransfer.dropEffect = state.dragPayload.kind === "new" ? "copy" : "move";
    const idx = getDropInsertIndex(boardEl, e.clientX);
    showDropIndicator(boardEl, idx);
  });

  boardEl.addEventListener("dragleave", (e) => {
    if (!state.dragPayload) return;
    if (!boardEl.contains(e.relatedTarget)) {
      clearDropIndicators();
    }
  });

  boardEl.addEventListener("drop", async (e) => {
    if (!state.dragPayload) return;
    e.preventDefault();
    e.stopPropagation();
    const payload = state.dragPayload;
    const insertIdx = getDropInsertIndex(boardEl, e.clientX);
    clearDropIndicators();
    state.dragPayload = null;

    if (payload.kind === "new") {
      await addEffectToChain(payload.slug, false, insertIdx);
    } else if (payload.kind === "move") {
      moveSlotToIndex(payload.slotId, insertIdx);
    }
  });

  // Drag & drop external audio files anywhere (ignored when dragging pedals)
  window.addEventListener("dragover", (e) => {
    if (state.dragPayload) return;
    const types = e.dataTransfer?.types ? Array.from(e.dataTransfer.types) : [];
    if (!types.includes("Files")) return;
    e.preventDefault();
    $("dropOverlay").classList.remove("hidden");
  });
  window.addEventListener("dragleave", (e) => {
    if (e.relatedTarget === null) $("dropOverlay").classList.add("hidden");
  });
  window.addEventListener("drop", (e) => {
    if (state.dragPayload) return;
    $("dropOverlay").classList.add("hidden");
    const file = e.dataTransfer?.files?.[0];
    if (file && file.type.startsWith("audio/")) {
      e.preventDefault();
      loadLocalAudioFile(file);
    }
  });

  // Computer keyboard shortcuts (Space = global bypass, A-K = synth notes when on Synth tab)
  window.addEventListener("keydown", (e) => {
    if (e.repeat) return;
    if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
    if (e.code === "Space") {
      e.preventDefault();
      $("globalBypassBtn").click();
      return;
    }
    if (state.activeSourceTab === "synth") {
      const k = e.key.toLowerCase();
      const note = KEYBOARD_NOTES.find((n) => n.key === k);
      if (note) {
        const btn = document.querySelector(`.key-btn[data-key="${k}"]`);
        if (btn) btn.classList.add("pressed");
        triggerSynthNote(note.freq);
      }
    }
  });

  window.addEventListener("keyup", (e) => {
    const k = e.key.toLowerCase();
    const btn = document.querySelector(`.key-btn[data-key="${k}"]`);
    if (btn) btn.classList.remove("pressed");
  });

  startAnimationLoop();
}

init();
