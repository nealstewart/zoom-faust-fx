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
  expandedSlotId: null, // Only one effect expanded at a time on mobile accordion

  // Web Audio
  ctx: null,
  worklet: null,
  inputBus: null,
  analyser: null,
  audioReady: false,
  audioInitPromise: null,

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

const NOTE_LABELS = ["1/16", "1/8T", "1/8", "1/4T", "1/8.", "1/4", "1/4."];
const NOTE_BEATS = [0.25, 1 / 3, 0.5, 2 / 3, 0.75, 1.0, 1.5];
const BEAT_SYNC_SLUGS = new Set(["xadly", "firecho", "wtdelay", "rf5cdly"]);

function formatKnobValue(slotObj, param, val) {
  if (param.timeSync) {
    const minMs = param.timeSync.min_ms;
    const maxMs = param.timeSync.max_ms;
    const notes = param.timeSync.notes || NOTE_LABELS;
    const msMaxClick = maxMs - minMs;
    if (val > msMaxClick) {
      const noteIdx = Math.min(notes.length - 1, Math.max(0, val - msMaxClick - 1));
      const ms = Math.min(maxMs, Math.round((60000 / state.bpm) * NOTE_BEATS[noteIdx]));
      return `${notes[noteIdx]} (${ms}ms)`;
    }
    return `${minMs + val} ms`;
  }
  return String(val);
}

function sendBpmCoeff(slotObj, bpm) {
  if (!state.worklet) return;
  const qtrMs = 60000.0 / Math.max(40, Math.min(300, bpm));
  state.worklet.port.postMessage({
    type: "setParam",
    slotId: slotObj.id,
    coeffSlot: 15,
    coeff: qtrMs,
  });
}

function applyBpmToSyncedSlots(bpm) {
  const clampedBpm = Math.max(60, Math.min(200, Math.round(bpm)));
  state.bpm = clampedBpm;
  const input = $("bpmInput");
  if (input) input.value = String(clampedBpm);

  let changed = false;
  for (const slotObj of state.chain) {
    // Push live global BPM quarter-note ms into coef[15] (SH_COEFF_BPM) for all effects
    sendBpmCoeff(slotObj, clampedBpm);
    if (BEAT_SYNC_SLUGS.has(slotObj.slug)) {
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
function setAudioSessionType(type) {
  try {
    if (navigator.audioSession && "type" in navigator.audioSession) {
      navigator.audioSession.type = type;
    }
  } catch (_) {}
}

function unlockAudioContextSync(ctx) {
  // iOS Safari requires resume() + a brief silent buffer synchronously inside the user gesture,
  // before any await (like audioWorklet.addModule or fetch) breaks user activation.
  try {
    const p = ctx.resume();
    if (p && typeof p.catch === "function") p.catch(() => {});
  } catch (_) {}
  try {
    const buf = ctx.createBuffer(1, 1, ctx.sampleRate || 44100);
    const src = ctx.createBufferSource();
    src.buffer = buf;
    src.connect(ctx.destination);
    src.start(0);
  } catch (_) {}
}

async function ensureAudio() {
  if (state.audioReady && state.ctx) {
    if (state.ctx.state !== "running") {
      try {
        await state.ctx.resume();
      } catch (_) {}
    }
    updateAudioStatus();
    return;
  }

  // If initialization is already in flight, still try to resume synchronously on this gesture
  if (state.audioInitPromise) {
    if (state.ctx && state.ctx.state !== "running") {
      unlockAudioContextSync(state.ctx);
    }
    return state.audioInitPromise;
  }

  // iOS 17+ Safari: play Web Audio even when the hardware Silent/Ring switch is muted
  setAudioSessionType("playback");

  const AudioCtx = window.AudioContext || window.webkitAudioContext;
  if (!AudioCtx) {
    $("audioCtxState").textContent = "Audio Error: Web Audio not supported";
    throw new Error("Web Audio API is not supported in this browser.");
  }

  let ctx;
  try {
    ctx = new AudioCtx({ sampleRate: 44100 });
  } catch (_) {
    // Fallback for mobile devices / Bluetooth outputs that reject a forced 44.1 kHz rate
    ctx = new AudioCtx();
  }
  state.ctx = ctx;
  ctx.addEventListener("statechange", updateAudioStatus);

  // Unlock synchronously inside the user gesture BEFORE any await!
  unlockAudioContextSync(ctx);

  state.audioInitPromise = (async () => {
    try {
      if (!ctx.audioWorklet) {
        const msg = window.isSecureContext
          ? "Audio Error: AudioWorklet not supported"
          : "Audio Error: HTTPS required for AudioWorklet";
        $("audioCtxState").textContent = msg;
        throw new Error(msg);
      }

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

      if (ctx.state !== "running") {
        try {
          await ctx.resume();
        } catch (_) {}
      }
      updateAudioStatus();
    } catch (err) {
      state.audioInitPromise = null;
      if (!state.audioReady) {
        try {
          ctx.close();
        } catch (_) {}
        state.ctx = null;
      }
      console.error("Audio init failed:", err);
      $("audioCtxState").textContent = `Audio Error: ${err?.message || err}`;
      throw err;
    }
  })();

  return state.audioInitPromise;
}

function updateAudioStatus() {
  const pill = $("audioCtxState");
  if (!state.ctx) {
    pill.textContent = "Audio: Tap to Start";
    pill.classList.remove("running", "needs-attention");
    return;
  }
  const sr = state.ctx.sampleRate;
  if (state.ctx.state === "running") {
    pill.textContent = `Audio: ${(sr / 1000).toFixed(1)} kHz`;
    pill.classList.add("running");
    pill.classList.remove("needs-attention");
  } else {
    pill.textContent = `Audio: ${state.ctx.state} (tap to resume)`;
    pill.classList.remove("running");
    pill.classList.add("needs-attention");
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
  sendBpmCoeff(slotObj, state.bpm);
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
  // Default-unlatched momentary effects (e.g. STUT RTCH) start bypassed until held
  slotObj.bypassed = isSlotMomentary(slotObj);

  const idx =
    typeof insertIndex === "number" && insertIndex >= 0 && insertIndex <= state.chain.length
      ? insertIndex
      : state.chain.length;
  state.chain.splice(idx, 0, slotObj);
  state.expandedSlotId = slotObj.id;
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
  if (state.expandedSlotId === slotId) {
    state.expandedSlotId = state.chain[0]?.id ?? null;
  }
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
  state.expandedSlotId = null;
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
  if (state.chain.length > 0) {
    state.expandedSlotId = state.chain[0].id;
    renderPedalboard();
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

const DREAMY_PAD_CHORDS = [
  [48, 55, 58, 62], // Cm9
  [44, 51, 55, 60], // Abmaj7
  [41, 48, 51, 55], // Fm9
  [46, 53, 55, 62], // Bb6/9
  [48, 55, 60, 63], // Cm (open)
  [44, 51, 56, 62], // Abmaj9#11
  [41, 48, 53, 58], // Fm11
  [43, 50, 53, 60], // G7sus4
];

let currentAcidPattern = ACID_PATTERNS.acid1.map((s) => ({ ...s }));
let currentPadChords = DREAMY_PAD_CHORDS.map((c) => [...c]);
let acidVoice = null; // persistent mono 303 voice { osc, f1, f2, shaper, vca, lastFreq }
let acidNextStepTime = 0;
const activePadEnvs = new Set(); // active pad gain envelopes for smooth stop fade-out

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
  const patVal = $("acidPatternSelect")?.value ?? "acid1";
  const isAcidLine = patVal === "acid1" || patVal === "acid2";
  const fltEnvNorm = isAcidLine ? Number($("acidFltEnvSlider")?.value ?? 55) / 100 : 0.55;
  const ampEnvNorm = isAcidLine ? Number($("acidAmpEnvSlider")?.value ?? 55) / 100 : 0.55;

  // Exponential cutoff 90 Hz .. 2800 Hz
  const baseCut = 90 * Math.pow(31, cutNorm);
  const envMult = 1.2 + fltEnvNorm * (accent ? 12.0 : 5.5);
  const peakCut = Math.min(11500, baseCut * envMult);
  const q1 = 1.0 + resNorm * 14.5 + (accent ? 2.5 : 0);
  const q2 = 0.8 + resNorm * 7.5;
  const dec = (0.04 + fltEnvNorm * fltEnvNorm * 0.42) * (accent ? 0.72 : 1.0);
  const gateFrac = 0.16 + ampEnvNorm * 0.82;
  const gate = slide ? stepDur * 1.05 : stepDur * gateFrac;
  const ampRel = 0.005 + ampEnvNorm * ampEnvNorm * 0.075;

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
    v.vca.gain.setTargetAtTime(0.0001, when + gate, ampRel);
  } else {
    v.vca.gain.linearRampToValueAtTime(peakAmp, when + 0.015);
    v.vca.gain.setTargetAtTime(0.0001, when + gate, ampRel * 1.35);
  }
}

function schedulePadNote(freq, when, holdDur = 2.4, peakAmp = 0.14, pan = 0) {
  const ctx = state.ctx;
  if (!ctx || !state.inputBus) return;

  const attack = 1.35; // > 1s lush slow swell
  const release = 2.4; // > 1s velvety tail
  const releaseStart = when + attack + holdDur;
  const endTime = releaseStart + release;

  const cutNorm = Number($("acidCutoffSlider")?.value ?? 38) / 100;
  const resNorm = Number($("acidResSlider")?.value ?? 72) / 100;

  // Base cutoff 180 Hz .. 1800 Hz, Q gently resonant for singing harmonics
  const baseCut = 180 * Math.pow(10, cutNorm);
  const peak1 = Math.min(8200, baseCut * 4.4);
  const midDip = Math.min(4800, baseCut * 2.0);
  const peak2 = Math.min(7200, baseCut * 3.5);
  const tailCut = Math.max(95, baseCut * 0.6);
  const q1 = 1.2 + resNorm * 5.2;
  const q2 = 0.9 + resNorm * 2.6;

  // Oscillators: detuned saw pair + warm sub-octave triangle
  const osc1 = ctx.createOscillator();
  const osc2 = ctx.createOscillator();
  const oscSub = ctx.createOscillator();
  osc1.type = "sawtooth";
  osc2.type = "sawtooth";
  oscSub.type = "triangle";
  osc1.frequency.setValueAtTime(freq, when);
  osc2.frequency.setValueAtTime(freq, when);
  oscSub.frequency.setValueAtTime(freq * 0.5, when);
  osc1.detune.setValueAtTime(-7, when);
  osc2.detune.setValueAtTime(7, when);

  // Subtle slow analog pitch drift LFO
  const driftLfo = ctx.createOscillator();
  const driftGain = ctx.createGain();
  driftLfo.type = "sine";
  driftLfo.frequency.setValueAtTime(0.24 + Math.random() * 0.14, when);
  driftGain.gain.setValueAtTime(4.5, when);
  driftLfo.connect(driftGain);
  driftGain.connect(osc1.detune);
  driftGain.connect(osc2.detune);

  const subGain = ctx.createGain();
  subGain.gain.setValueAtTime(0.32, when);

  // Cascaded dual lowpass with complex 4-stage filter envelope + slow shimmer LFO
  const f1 = ctx.createBiquadFilter();
  f1.type = "lowpass";
  f1.Q.setValueAtTime(q1, when);

  const f2 = ctx.createBiquadFilter();
  f2.type = "lowpass";
  f2.Q.setValueAtTime(q2, when);

  // Stage 1: initial slow bloom -> Stage 2: breathing dip -> Stage 3: secondary swell -> Stage 4: dark release
  const tPeak1 = when + attack * 0.85;
  const tDip = when + attack + holdDur * 0.42;
  const tPeak2 = releaseStart;

  f1.frequency.setValueAtTime(baseCut, when);
  f1.frequency.exponentialRampToValueAtTime(peak1, tPeak1);
  f1.frequency.exponentialRampToValueAtTime(midDip, tDip);
  f1.frequency.exponentialRampToValueAtTime(peak2, tPeak2);
  f1.frequency.exponentialRampToValueAtTime(tailCut, endTime);

  f2.frequency.setValueAtTime(baseCut * 1.25, when);
  f2.frequency.exponentialRampToValueAtTime(Math.min(9500, peak1 * 1.2), tPeak1 + 0.18);
  f2.frequency.exponentialRampToValueAtTime(midDip * 1.15, tDip + 0.18);
  f2.frequency.exponentialRampToValueAtTime(Math.min(9000, peak2 * 1.15), tPeak2);
  f2.frequency.exponentialRampToValueAtTime(tailCut * 1.1, endTime);

  // Filter shimmer LFO (slow undulation during sustain)
  const filtLfo = ctx.createOscillator();
  const filtLfoGain = ctx.createGain();
  filtLfo.type = "sine";
  filtLfo.frequency.setValueAtTime(0.38 + Math.random() * 0.2, when);
  filtLfoGain.gain.setValueAtTime(baseCut * 0.32, when);
  filtLfo.connect(filtLfoGain);
  filtLfoGain.connect(f1.frequency);

  // Long >1s attack & >1s release VCA envelope
  const env = ctx.createGain();
  env.gain.setValueAtTime(0.0001, when);
  env.gain.linearRampToValueAtTime(peakAmp, when + attack);
  env.gain.setValueAtTime(peakAmp * 0.92, releaseStart);
  env.gain.exponentialRampToValueAtTime(0.0001, endTime);

  osc1.connect(f1);
  osc2.connect(f1);
  oscSub.connect(subGain);
  subGain.connect(f1);
  f1.connect(f2);
  f2.connect(env);

  if (typeof ctx.createStereoPanner === "function") {
    const panner = ctx.createStereoPanner();
    panner.pan.setValueAtTime(pan, when);
    env.connect(panner);
    panner.connect(state.inputBus);
  } else {
    env.connect(state.inputBus);
  }

  activePadEnvs.add(env);

  osc1.start(when);
  osc2.start(when);
  oscSub.start(when);
  driftLfo.start(when);
  filtLfo.start(when);

  const stopAt = endTime + 0.08;
  osc1.stop(stopAt);
  osc2.stop(stopAt);
  oscSub.stop(stopAt);
  driftLfo.stop(stopAt);
  filtLfo.stop(stopAt);

  osc1.onended = () => {
    activePadEnvs.delete(env);
    try {
      env.disconnect();
    } catch (_) {}
  };
}

function fadeOutActivePads() {
  if (!state.ctx) return;
  const now = state.ctx.currentTime;
  for (const env of activePadEnvs) {
    try {
      env.gain.cancelScheduledValues(now);
      env.gain.setTargetAtTime(0.0001, now, 0.14);
    } catch (_) {}
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

  if (voice === "pad") {
    schedulePadNote(freq, now, Math.max(1.2, duration), 0.24, 0);
    return;
  }

  const env = ctx.createGain();
  env.connect(state.inputBus);
  const osc1 = ctx.createOscillator();
  const osc2 = ctx.createOscillator();
  const filt = ctx.createBiquadFilter();
  filt.type = "lowpass";

  // Pluck
  osc1.type = "sawtooth";
  osc2.type = "triangle";
  osc1.frequency.setValueAtTime(freq, now);
  osc2.frequency.setValueAtTime(freq * 2.002, now);
  filt.frequency.setValueAtTime(3400, now);
  filt.frequency.exponentialRampToValueAtTime(380, now + 0.35);
  env.gain.setValueAtTime(0.001, now);
  env.gain.linearRampToValueAtTime(0.42, now + 0.006);
  env.gain.exponentialRampToValueAtTime(0.001, now + duration);

  osc1.connect(filt);
  osc2.connect(filt);
  filt.connect(env);
  osc1.start(now);
  osc2.start(now);
  osc1.stop(now + duration + 0.05);
  osc2.stop(now + duration + 0.05);
}

function randomizeAcidPattern() {
  if ($("acidPatternSelect")?.value === "dreamy_pad") {
    // Generate an 8-chord lush dreamy ambient progression in C minor / Dorian
    const pool = [
      [48, 55, 58, 62], // Cm9
      [44, 51, 55, 60], // Abmaj7
      [41, 48, 51, 55], // Fm9
      [46, 53, 55, 62], // Bb6/9
      [43, 50, 53, 58], // Gm7
      [44, 51, 56, 62], // Abmaj9#11
      [39, 46, 50, 55], // Ebmaj7
      [41, 48, 53, 58], // Fm11
      [48, 55, 60, 65], // Csus4
      [43, 50, 53, 60], // G7sus4
    ];
    currentPadChords = Array.from({ length: 8 }, (_, i) => {
      const ch = i === 0 ? pool[0] : pool[Math.floor(Math.random() * pool.length)];
      return [...ch];
    });
    return;
  }

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

function updateSeqButtonLabel() {
  const btn = $("autoRiffBtn");
  if (!btn) return;
  const isPadSeq = $("acidPatternSelect")?.value === "dreamy_pad";
  if (state.autoRiffTimer) {
    btn.textContent = isPadSeq ? "■ Stop Pad" : "■ Stop 303";
  } else {
    btn.textContent = isPadSeq ? "▶ Pad Loop" : "▶ 303 Loop";
  }
}

async function toggleAutoRiff() {
  const btn = $("autoRiffBtn");
  if (state.autoRiffTimer) {
    clearInterval(state.autoRiffTimer);
    state.autoRiffTimer = null;
    if (acidVoice && state.ctx) {
      acidVoice.vca.gain.setTargetAtTime(0.0, state.ctx.currentTime, 0.015);
    }
    fadeOutActivePads();
    btn.classList.remove("btn-toggle", "active");
    btn.classList.add("btn-primary");
    updateSeqButtonLabel();
    return;
  }

  await ensureAudio();
  state.autoRiffStep = 0;
  acidNextStepTime = state.ctx.currentTime + 0.04;
  btn.classList.remove("btn-primary");
  btn.classList.add("btn-toggle", "active");

  // Lookahead scheduler locked to state.bpm (16th-note acid or 2-bar dreamy pad chords)
  state.autoRiffTimer = setInterval(() => {
    if (!state.ctx) return;
    const isPadSeq = $("acidPatternSelect")?.value === "dreamy_pad";
    if (isPadSeq) {
      // Each dreamy chord spans 2 full bars (8 beats), overlapping with >1s attack & release
      const chordDur = (60.0 / state.bpm) * 8.0;
      const holdDur = Math.max(1.8, chordDur - 0.8);
      const pans = [-0.45, -0.15, 0.15, 0.45];
      while (acidNextStepTime < state.ctx.currentTime + 0.25) {
        const chord = currentPadChords[state.autoRiffStep % currentPadChords.length];
        chord.forEach((midi, i) => {
          schedulePadNote(
            midiToFreq(midi),
            acidNextStepTime + i * 0.06,
            holdDur,
            0.125,
            pans[i % pans.length]
          );
        });
        acidNextStepTime += chordDur;
        state.autoRiffStep++;
      }
      return;
    }

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
        } else if (voice === "pad") {
          schedulePadNote(freq * 2, acidNextStepTime, Math.max(1.2, stepDur * 4), 0.14, 0);
        } else {
          triggerSynthNote(freq * 2, stepDur * 1.6);
        }
      }
      acidNextStepTime += stepDur;
      state.autoRiffStep++;
    }
  }, 25);

  updateSeqButtonLabel();
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
    setAudioSessionType("playback");
    btn.textContent = "🎙 Enable Live Input";
    status.textContent = "Stopped";
    status.classList.remove("running");
    return;
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    status.textContent = "Live input requires HTTPS & mic support";
    return;
  }

  try {
    setAudioSessionType("play-and-record");
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

// ---- Mobile Sidebar Drawer ----
function setSidebarOpen(open) {
  const sidebar = $("sidebar");
  const backdrop = $("sidebarBackdrop");
  if (!sidebar || !backdrop) return;
  sidebar.classList.toggle("open", open);
  backdrop.classList.toggle("visible", open);
  document.body.classList.toggle("drawer-open", open);
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
    const descEl = document.createElement("div");
    descEl.className = "effect-desc mobile-only";
    descEl.textContent = fx.description;
    meta.appendChild(descEl);

    const actions = document.createElement("div");
    actions.className = "effect-actions";

    const loadBtn = document.createElement("button");
    loadBtn.className = "btn btn-sm btn-primary";
    loadBtn.textContent = "Load";
    loadBtn.title = "Audition this effect alone";
    loadBtn.onclick = () => {
      addEffectToChain(fx.slug, true);
      if (window.innerWidth <= 860) setSidebarOpen(false);
    };

    const addBtn = document.createElement("button");
    addBtn.className = "btn btn-sm btn-ghost";
    addBtn.textContent = "+ Chain";
    addBtn.title = "Append to signal chain";
    addBtn.onclick = () => {
      addEffectToChain(fx.slug, false);
      if (window.innerWidth <= 860) {
        addBtn.textContent = "Added ✓";
        setTimeout(() => {
          addBtn.textContent = "+ Chain";
        }, 700);
      }
    };

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

  svg.append(track, arc, cap, pointer);
  wrap.appendChild(svg);

  function valToNorm(val) {
    if (param.timeSync) {
      const msMax = param.timeSync.max_ms - param.timeSync.min_ms;
      const nNotes = Math.max(1, param.max - msMax);
      if (val <= msMax) return (val / Math.max(1, msMax)) * 0.72;
      return 0.72 + ((val - msMax) / nNotes) * 0.28;
    }
    return param.max > 0 ? val / param.max : 0;
  }

  function normToVal(norm) {
    const n = Math.max(0, Math.min(1, norm));
    if (param.timeSync) {
      const msMax = param.timeSync.max_ms - param.timeSync.min_ms;
      const nNotes = Math.max(1, param.max - msMax);
      if (n <= 0.72) return Math.round((n / 0.72) * msMax);
      const noteStep = Math.max(1, Math.min(nNotes, Math.ceil(((n - 0.72) / 0.28) * nNotes)));
      return msMax + noteStep;
    }
    return Math.round(param.min + n * (param.max - param.min));
  }

  function updateVisual(val) {
    const norm = valToNorm(val);
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

  // Vertical drag interaction (mouse + touch) + double-tap to reset
  let dragging = false;
  let moved = false;
  let startY = 0;
  let startNorm = 0;
  let lastTapTime = 0;

  wrap.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    dragging = true;
    moved = false;
    startY = e.clientY;
    startNorm = valToNorm(slotObj.values.get(param.index));
    try {
      wrap.setPointerCapture(e.pointerId);
    } catch (_) {}
  });

  wrap.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const dy = startY - e.clientY;
    if (Math.abs(dy) > 4) moved = true;
    const sensitivity = e.shiftKey ? 450 : 150;
    const nextVal = normToVal(startNorm + dy / sensitivity);
    const clamped = Math.max(param.min, Math.min(param.max, nextVal));
    if (clamped !== slotObj.values.get(param.index)) {
      setSlotParam(slotObj, param, clamped);
      updateVisual(clamped);
    }
  });

  const endDrag = (e) => {
    if (!dragging) return;
    dragging = false;
    if (e.type === "pointerup" && !moved && e.pointerType === "touch") {
      const now = performance.now();
      if (now - lastTapTime < 320) {
        setSlotParam(slotObj, param, param.default);
        updateVisual(param.default);
        lastTapTime = 0;
      } else {
        lastTapTime = now;
      }
    }
  };
  wrap.addEventListener("pointerup", endDrag);
  wrap.addEventListener("pointercancel", endDrag);
  wrap.addEventListener("lostpointercapture", endDrag);

  wrap.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      const cur = slotObj.values.get(param.index);
      let step = Math.max(1, Math.round((param.max - param.min) / 40));
      if (param.timeSync) {
        const msMax = param.timeSync.max_ms - param.timeSync.min_ms;
        step = (cur > msMax || (cur === msMax && e.deltaY < 0)) ? 1 : 15;
      }
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
    const isExpanded = state.expandedSlotId === slotObj.id;
    const card = document.createElement("div");
    card.className =
      "pedal-card" +
      (slotObj.bypassed ? " bypassed" : "") +
      (isExpanded ? " expanded" : " collapsed");
    card.dataset.slotId = String(slotObj.id);

    // Header (draggable on desktop; tap to expand/collapse accordion on mobile)
    const head = document.createElement("div");
    head.className = "pedal-head";
    head.draggable = true;
    head.title = "Drag to reorder · Tap to expand/collapse";
    head.setAttribute("aria-expanded", String(isExpanded));

    head.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      state.expandedSlotId = isExpanded ? null : slotObj.id;
      renderPedalboard();
    });

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

    const headLedBtn = document.createElement("button");
    headLedBtn.type = "button";
    headLedBtn.className = "pedal-head-led mobile-only";
    headLedBtn.title = "Toggle effect bypass (hold for momentary UNLATCH)";
    headLedBtn.setAttribute("aria-label", `Toggle ${fx.display} bypass`);
    headLedBtn.innerHTML = `<span class="footswitch-led"></span>`;
    const syncLedUI = () => {
      card.classList.toggle("bypassed", slotObj.bypassed);
      const fsw = card.querySelector(".footswitch-btn");
      if (fsw) {
        const mom = isSlotMomentary(slotObj);
        const modeTag = mom ? "HOLD (UNLATCH)" : slotObj.bypassed ? "BYPASSED" : "ENGAGED";
        fsw.innerHTML = `<span class="footswitch-led"></span> ${modeTag}`;
      }
    };
    headLedBtn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (isSlotMomentary(slotObj)) {
        try {
          headLedBtn.setPointerCapture(e.pointerId);
        } catch (_) {}
        setSlotBypass(slotObj, false);
        syncLedUI();
      } else {
        setSlotBypass(slotObj, !slotObj.bypassed);
        syncLedUI();
      }
    });
    const releaseHeadMomentary = () => {
      if (isSlotMomentary(slotObj) && !slotObj.bypassed) {
        setSlotBypass(slotObj, true);
        syncLedUI();
      }
    };
    headLedBtn.addEventListener("pointerup", releaseHeadMomentary);
    headLedBtn.addEventListener("pointercancel", releaseHeadMomentary);
    headLedBtn.addEventListener("pointerleave", releaseHeadMomentary);
    headLedBtn.addEventListener("lostpointercapture", releaseHeadMomentary);
    headLedBtn.addEventListener("click", (e) => e.stopPropagation());

    const headIcon = document.createElement("div");
    headIcon.className = "pedal-head-icon mobile-only";
    if (fx.iconUrl) {
      const hImg = document.createElement("img");
      hImg.src = fx.iconUrl;
      hImg.alt = fx.display;
      hImg.draggable = false;
      headIcon.appendChild(hImg);
    } else {
      headIcon.textContent = fx.badge;
    }

    const titles = document.createElement("div");
    titles.style.minWidth = "0";
    titles.innerHTML = `
      <div class="pedal-name">${fx.display}</div>
      <div class="pedal-zd2">${fx.zd2 || fx.filename + ".ZD2"} · ${fx.dspload}% DSP</div>
    `;

    const chevron = document.createElement("span");
    chevron.className = "pedal-chevron mobile-only";
    chevron.textContent = isExpanded ? "▾" : "▸";

    titleWrap.append(grip, headLedBtn, headIcon, titles, chevron);

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
      if (isSlotMomentary(slotObj)) setSlotBypass(slotObj, true);
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
    fswBtn.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      if (isSlotMomentary(slotObj)) {
        try {
          fswBtn.setPointerCapture(e.pointerId);
        } catch (_) {}
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
    fswBtn.addEventListener("pointercancel", releaseMomentary);
    fswBtn.addEventListener("pointerleave", releaseMomentary);
    fswBtn.addEventListener("lostpointercapture", releaseMomentary);

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
    g.fillStyle = "#12f094";
    g.fillRect(0, 0, w, h);

    // Subtle LCD grid center line
    g.strokeStyle = "rgba(4, 59, 36, 0.24)";
    g.lineWidth = 1;
    g.beginPath();
    g.moveTo(0, h / 2);
    g.lineTo(w, h / 2);
    g.stroke();

    if (!state.analyser) return;

    if (state.scopeMode === "scope") {
      state.analyser.getFloatTimeDomainData(timeBuf);
      g.strokeStyle = "#043b24";
      g.lineWidth = 1.75;
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
      g.fillStyle = "#043b24";
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
    b.type = "button";
    b.className = "key-btn" + (n.black ? " black" : "");
    b.textContent = n.key.toUpperCase();
    b.dataset.key = n.key;
    b.title = `${n.note} (${n.key.toUpperCase()})`;
    b.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      b.classList.add("pressed");
      triggerSynthNote(n.freq);
    });
    const up = () => b.classList.remove("pressed");
    b.addEventListener("pointerup", up);
    b.addEventListener("pointercancel", up);
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

  // Synth (TB-303 Acid & Dreamy Pad Sequencer)
  $("autoRiffBtn").addEventListener("click", toggleAutoRiff);
  $("acidPatternSelect").addEventListener("change", (e) => {
    const val = e.target.value;
    if (val === "dreamy_pad") {
      $("synthVoiceSelect").value = "pad";
      currentPadChords = DREAMY_PAD_CHORDS.map((c) => [...c]);
      if (acidVoice && state.ctx) {
        acidVoice.vca.gain.setTargetAtTime(0.0, state.ctx.currentTime, 0.02);
      }
    } else {
      const p = ACID_PATTERNS[val];
      if (p) currentAcidPattern = p.map((s) => ({ ...s }));
      if ($("synthVoiceSelect").value === "pad") {
        $("synthVoiceSelect").value = "acid_saw";
      }
      fadeOutActivePads();
    }
    if (state.autoRiffTimer && state.ctx) {
      state.autoRiffStep = 0;
      acidNextStepTime = state.ctx.currentTime + 0.04;
    }
    updateSeqButtonLabel();
    updateAcidEnvVisibility();
  });
  function initMiniSynthKnobs() {
    const svgNS = "http://www.w3.org/2000/svg";
    const r = 22, cx = 30, cy = 30, startAngle = -225, sweep = 270;
    const polar = (deg) => {
      const rad = (deg * Math.PI) / 180;
      return [cx + r * Math.cos(rad), cy + r * Math.sin(rad)];
    };
    const arcPath = (a0, a1) => {
      const [x0, y0] = polar(a0);
      const [x1, y1] = polar(a1);
      const large = a1 - a0 > 180 ? 1 : 0;
      return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
    };

    document.querySelectorAll(".mini-knob-slot").forEach((slot) => {
      const input = $(slot.dataset.input);
      if (!input) return;
      const defVal = Number(slot.dataset.default ?? input.value ?? 50);

      const lbl = document.createElement("div");
      lbl.className = "mini-knob-label";
      lbl.textContent = slot.dataset.label || "";

      const wrap = document.createElement("div");
      wrap.className = "knob-wrap mini-knob-wrap";

      const svg = document.createElementNS(svgNS, "svg");
      svg.setAttribute("viewBox", "0 0 60 60");
      svg.classList.add("knob-svg", "mini-knob-svg");

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

      svg.append(track, arc, cap, pointer);
      wrap.appendChild(svg);

      const valEl = document.createElement("div");
      valEl.className = "mini-knob-val mono";

      slot.append(lbl, wrap, valEl);

      function setVal(v) {
        const clamped = Math.max(0, Math.min(100, Math.round(v)));
        input.value = String(clamped);
        const norm = clamped / 100;
        const deg = startAngle + norm * sweep;
        arc.setAttribute("d", norm <= 0.001 ? "" : arcPath(startAngle, deg));
        const rad = (deg * Math.PI) / 180;
        pointer.setAttribute("x1", (cx + 4 * Math.cos(rad)).toFixed(2));
        pointer.setAttribute("y1", (cy + 4 * Math.sin(rad)).toFixed(2));
        pointer.setAttribute("x2", (cx + 13 * Math.cos(rad)).toFixed(2));
        pointer.setAttribute("y2", (cy + 13 * Math.sin(rad)).toFixed(2));
        valEl.textContent = String(clamped);
      }

      setVal(Number(input.value));

      let dragging = false, startY = 0, startVal = 0;
      wrap.addEventListener("pointerdown", (e) => {
        e.preventDefault();
        dragging = true;
        startY = e.clientY;
        startVal = Number(input.value);
        try { wrap.setPointerCapture(e.pointerId); } catch (_) {}
      });
      wrap.addEventListener("pointermove", (e) => {
        if (!dragging) return;
        const dy = startY - e.clientY;
        const sens = e.shiftKey ? 320 : 120;
        setVal(startVal + (dy / sens) * 100);
      });
      const stopDrag = () => { dragging = false; };
      wrap.addEventListener("pointerup", stopDrag);
      wrap.addEventListener("pointercancel", stopDrag);
      wrap.addEventListener("lostpointercapture", stopDrag);
      wrap.addEventListener("dblclick", () => setVal(defVal));
      wrap.addEventListener("wheel", (e) => {
        e.preventDefault();
        setVal(Number(input.value) + (e.deltaY < 0 ? 3 : -3));
      }, { passive: false });
    });
  }
  initMiniSynthKnobs();

  function updateAcidEnvVisibility() {
    const grp = $("acidEnvGroup");
    if (!grp) return;
    const pat = $("acidPatternSelect")?.value;
    grp.hidden = !(pat === "acid1" || pat === "acid2");
  }
  updateAcidEnvVisibility();
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

  const toggleGlobalBypass = () => {
    state.globalBypass = !state.globalBypass;
    const btn = $("globalBypassBtn");
    btn.classList.toggle("bypassed", state.globalBypass);
    btn.innerHTML = `<span class="led"></span> ${state.globalBypass ? "BYPASSED" : "ACTIVE"}`;
    const mBtn = $("mobileBypassBtn");
    if (mBtn) {
      mBtn.classList.toggle("bypassed", state.globalBypass);
      mBtn.innerHTML = `<span class="led"></span> ${state.globalBypass ? "BYPASS" : "ON"}`;
    }
    syncMasterToWorklet();
  };
  $("globalBypassBtn").addEventListener("click", toggleGlobalBypass);
  $("mobileBypassBtn")?.addEventListener("click", toggleGlobalBypass);

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

  // Mobile sidebar drawer, collapsible panels & audio status button
  $("openSidebarBtn")?.addEventListener("click", () => setSidebarOpen(true));
  $("closeSidebarBtn")?.addEventListener("click", () => setSidebarOpen(false));
  $("sidebarBackdrop")?.addEventListener("click", () => setSidebarOpen(false));

  $("toggleMasterBtn")?.addEventListener("click", () => {
    const topbar = document.querySelector(".topbar");
    const btn = $("toggleMasterBtn");
    if (!topbar || !btn) return;
    const isOpen = topbar.classList.toggle("master-open");
    btn.setAttribute("aria-expanded", String(isOpen));
    btn.textContent = isOpen ? "Mix ▴" : "Mix ▾";
    btn.classList.toggle("btn-toggle", isOpen);
    btn.classList.toggle("active", isOpen);
  });

  $("toggleSynthExtrasBtn")?.addEventListener("click", () => {
    const extras = $("synthExtras");
    const btn = $("toggleSynthExtrasBtn");
    if (!extras || !btn) return;
    const isOpen = extras.classList.toggle("open");
    btn.setAttribute("aria-expanded", String(isOpen));
    btn.textContent = isOpen ? "Tone ▴" : "Tone ▾";
    btn.classList.toggle("btn-toggle", isOpen);
    btn.classList.toggle("active", isOpen);
  });

  $("audioCtxState")?.addEventListener("click", () => {
    ensureAudio().catch(() => {});
  });

  // Automatically resume suspended/interrupted mobile AudioContext on next user gesture
  document.addEventListener(
    "pointerdown",
    () => {
      if (state.ctx && state.ctx.state !== "running") {
        unlockAudioContextSync(state.ctx);
      }
    },
    { passive: true }
  );

  // Computer keyboard shortcuts (Space = global bypass, A-K = synth notes when on Synth tab)
  window.addEventListener("keydown", (e) => {
    if (e.repeat) return;
    if (e.key === "Escape") {
      setSidebarOpen(false);
      return;
    }
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
