import { WebMidiRelayClient, isChecklistComplete } from "./webmidi.js";

const DEFAULT_DSP = `import("stdfaust.lib");

gain = hslider("Gain", 50, 0, 100, 1) : /(50.0);
process = *(gain), *(gain);
`;

const ICON_SIZE = 32;
const ICON_SCALE = 10;
const iconBitmap = Array.from({ length: ICON_SIZE }, () => Array(ICON_SIZE).fill(0));
// Seed a simple diagonal/border default icon
for (let i = 4; i < 28; i++) {
  iconBitmap[4][i] = 1;
  iconBitmap[27][i] = 1;
  iconBitmap[i][4] = 1;
  iconBitmap[i][27] = 1;
}

let iconTool = "draw";
let isDrawingIcon = false;
let currentBuild = null;
let pedalState = {
  connected: false,
  autosave_off_confirmed: false,
  backup_completed: false,
  rescue_locked: false,
};

const els = {
  capBadge: document.getElementById("capBadge"),
  dspEditor: document.getElementById("dspEditor"),
  wasmPreviewBtn: document.getElementById("wasmPreviewBtn"),
  wasmStopBtn: document.getElementById("wasmStopBtn"),
  wasmStatus: document.getElementById("wasmStatus"),
  fxName: document.getElementById("fxName"),
  fxFilename: document.getElementById("fxFilename"),
  fxId: document.getElementById("fxId"),
  fxVersion: document.getElementById("fxVersion"),
  fxDesc: document.getElementById("fxDesc"),
  iconCanvas: document.getElementById("iconCanvas"),
  iconDrawBtn: document.getElementById("iconDrawBtn"),
  iconEraseBtn: document.getElementById("iconEraseBtn"),
  iconFillBtn: document.getElementById("iconFillBtn"),
  iconSquareBtn: document.getElementById("iconSquareBtn"),
  iconCircleBtn: document.getElementById("iconCircleBtn"),
  iconSineBtn: document.getElementById("iconSineBtn"),
  iconSqWaveBtn: document.getElementById("iconSqWaveBtn"),
  iconInvertBtn: document.getElementById("iconInvertBtn"),
  iconClearBtn: document.getElementById("iconClearBtn"),
  buildBtn: document.getElementById("buildBtn"),
  buildGateBadge: document.getElementById("buildGateBadge"),
  stageList: document.getElementById("stageList"),
  artifactLinks: document.getElementById("artifactLinks"),
  reportViewer: document.getElementById("reportViewer"),
  autosaveBadge: document.getElementById("autosaveBadge"),
  webMidiBtn: document.getElementById("webMidiBtn"),
  safeConnectBtn: document.getElementById("safeConnectBtn"),
  backupBtn: document.getElementById("backupBtn"),
  installBtn: document.getElementById("installBtn"),
  uninstallBtn: document.getElementById("uninstallBtn"),
  rescueBtn: document.getElementById("rescueBtn"),
  pedalLog: document.getElementById("pedalLog"),
  rescueBanner: document.getElementById("rescueBanner"),
  rescueBannerReason: document.getElementById("rescueBannerReason"),
  bannerRescueBtn: document.getElementById("bannerRescueBtn"),
  confirmModal: document.getElementById("confirmModal"),
  modalTitle: document.getElementById("modalTitle"),
  modalDetails: document.getElementById("modalDetails"),
  modalCancelBtn: document.getElementById("modalCancelBtn"),
  modalConfirmBtn: document.getElementById("modalConfirmBtn"),
  chkAutosave: document.getElementById("chkAutosave"),
  chkBackup: document.getElementById("chkBackup"),
  chkUnsaved: document.getElementById("chkUnsaved"),
  chkNoPower: document.getElementById("chkNoPower"),
};

els.dspEditor.value = DEFAULT_DSP;

function appendPedalLog(msg, level = "info") {
  const line = document.createElement("div");
  line.textContent = `[${level}] ${msg}`;
  if (level === "error") line.style.color = "#ff94a0";
  if (level === "warning") line.style.color = "#f6c453";
  els.pedalLog.appendChild(line);
  els.pedalLog.scrollTop = els.pedalLog.scrollHeight;
}

function showRescueLock(reason) {
  els.rescueBannerReason.textContent =
    reason || "Transfer failed verification. Keep the pedal powered and run Rescue.";
  els.rescueBanner.classList.remove("hidden");
  updatePedalButtons();
}

function clearRescueLock() {
  els.rescueBanner.classList.add("hidden");
}

function updatePedalButtons() {
  const ready = pedalState.connected && pedalState.autosave_off_confirmed;
  const locked = pedalState.rescue_locked;
  els.backupBtn.disabled = !ready || locked;
  els.installBtn.disabled = !(
    ready &&
    pedalState.backup_completed &&
    !locked &&
    currentBuild &&
    currentBuild.installable
  );
  els.uninstallBtn.disabled = !(ready && pedalState.backup_completed && !locked && currentBuild);

  if (pedalState.autosave_off_confirmed) {
    els.autosaveBadge.textContent = "Autosave: OFF (ACK)";
    els.autosaveBadge.className = "status-pill ok";
  } else {
    els.autosaveBadge.textContent = "Autosave: Unconfirmed";
    els.autosaveBadge.className = "status-pill";
  }
}

const relay = new WebMidiRelayClient({
  onLog: (m) => appendPedalLog(m.message, m.level || "info"),
  onStatus: (st) => {
    pedalState = st;
    if (st.rescue_locked) showRescueLock(st.rescue_reason);
    else clearRescueLock();
    updatePedalButtons();
  },
  onRescueLock: (reason) => showRescueLock(reason),
});

// ---- 1-Bit Icon Canvas ----
let dragStart = null;
let dragSnapshot = null;

function cloneBitmap(src) {
  return src.map((row) => row.slice());
}

function setBitmapPixel(bitmap, x, y, val = 1) {
  if (x >= 0 && x < ICON_SIZE && y >= 0 && y < ICON_SIZE) {
    bitmap[y][x] = val ? 1 : 0;
  }
}

function drawLineOnBitmap(bitmap, x0, y0, x1, y1, val = 1) {
  let dx = Math.abs(x1 - x0);
  let dy = Math.abs(y1 - y0);
  const sx = x0 < x1 ? 1 : -1;
  const sy = y0 < y1 ? 1 : -1;
  let err = dx - dy;
  let cx = x0;
  let cy = y0;
  while (true) {
    setBitmapPixel(bitmap, cx, cy, val);
    if (cx === x1 && cy === y1) break;
    const e2 = 2 * err;
    if (e2 > -dy) {
      err -= dy;
      cx += sx;
    }
    if (e2 < dx) {
      err += dx;
      cy += sy;
    }
  }
}

function drawSquareOnBitmap(bitmap, x0, y0, x1, y1, val = 1) {
  const side = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0));
  const sx = x1 >= x0 ? 1 : -1;
  const sy = y1 >= y0 ? 1 : -1;
  const ex = x0 + sx * side;
  const ey = y0 + sy * side;
  const minX = Math.min(x0, ex);
  const maxX = Math.max(x0, ex);
  const minY = Math.min(y0, ey);
  const maxY = Math.max(y0, ey);
  for (let x = minX; x <= maxX; x++) {
    setBitmapPixel(bitmap, x, minY, val);
    setBitmapPixel(bitmap, x, maxY, val);
  }
  for (let y = minY; y <= maxY; y++) {
    setBitmapPixel(bitmap, minX, y, val);
    setBitmapPixel(bitmap, maxX, y, val);
  }
}

function drawCircleOnBitmap(bitmap, cx, cy, x1, y1, val = 1) {
  const r = Math.round(Math.hypot(x1 - cx, y1 - cy));
  if (r <= 0) {
    setBitmapPixel(bitmap, cx, cy, val);
    return;
  }
  let x = r;
  let y = 0;
  let err = 1 - x;
  while (x >= y) {
    setBitmapPixel(bitmap, cx + x, cy + y, val);
    setBitmapPixel(bitmap, cx + y, cy + x, val);
    setBitmapPixel(bitmap, cx - y, cy + x, val);
    setBitmapPixel(bitmap, cx - x, cy + y, val);
    setBitmapPixel(bitmap, cx - x, cy - y, val);
    setBitmapPixel(bitmap, cx - y, cy - x, val);
    setBitmapPixel(bitmap, cx + y, cy - x, val);
    setBitmapPixel(bitmap, cx + x, cy - y, val);
    y++;
    if (err < 0) {
      err += 2 * y + 1;
    } else {
      x--;
      err += 2 * (y - x) + 1;
    }
  }
}

function drawSineWaveOnBitmap(bitmap, x0, y0, x1, y1, val = 1) {
  const minX = Math.min(x0, x1);
  const maxX = Math.max(x0, x1);
  const span = Math.max(1, maxX - minX);
  const amp = Math.max(2, Math.abs(y1 - y0));
  const cycles = Math.max(1, Math.round(span / 12));
  let prevY = null;
  for (let x = minX; x <= maxX; x++) {
    const t = (x - minX) / span;
    const y = Math.round(y0 - Math.sin(t * cycles * Math.PI * 2) * amp);
    if (prevY !== null && Math.abs(y - prevY) > 1) {
      drawLineOnBitmap(bitmap, x - 1, prevY, x, y, val);
    } else {
      setBitmapPixel(bitmap, x, y, val);
    }
    prevY = y;
  }
}

function drawSquareWaveOnBitmap(bitmap, x0, y0, x1, y1, val = 1) {
  const minX = Math.min(x0, x1);
  const maxX = Math.max(x0, x1);
  const span = Math.max(1, maxX - minX);
  const amp = Math.max(2, Math.abs(y1 - y0));
  const cycles = Math.max(1, Math.round(span / 12));
  let prevY = null;
  for (let x = minX; x <= maxX; x++) {
    const t = (x - minX) / span;
    const phase = (t * cycles) % 1;
    const y = phase < 0.5 ? y0 - amp : y0 + amp;
    if (prevY !== null && prevY !== y) {
      drawLineOnBitmap(bitmap, x, prevY, x, y, val);
    }
    setBitmapPixel(bitmap, x, y, val);
    prevY = y;
  }
}

function floodFillBitmap(bitmap, startX, startY) {
  if (startX < 0 || startX >= ICON_SIZE || startY < 0 || startY >= ICON_SIZE) return;
  const target = bitmap[startY][startX];
  const replacement = target ? 0 : 1;
  const queue = [[startX, startY]];
  const visited = new Set([`${startX},${startY}`]);
  while (queue.length > 0) {
    const [x, y] = queue.shift();
    bitmap[y][x] = replacement;
    for (const [nx, ny] of [
      [x + 1, y],
      [x - 1, y],
      [x, y + 1],
      [x, y - 1],
    ]) {
      if (nx < 0 || nx >= ICON_SIZE || ny < 0 || ny >= ICON_SIZE) continue;
      const key = `${nx},${ny}`;
      if (!visited.has(key) && bitmap[ny][nx] === target) {
        visited.add(key);
        queue.push([nx, ny]);
      }
    }
  }
}

function drawIconCanvas(bitmap = iconBitmap) {
  const ctx = els.iconCanvas.getContext("2d");
  ctx.fillStyle = "#080a0f";
  ctx.fillRect(0, 0, ICON_SIZE * ICON_SCALE, ICON_SIZE * ICON_SCALE);
  for (let y = 0; y < ICON_SIZE; y++) {
    for (let x = 0; x < ICON_SIZE; x++) {
      ctx.fillStyle = bitmap[y][x] ? "#7ee2a8" : "#111622";
      ctx.fillRect(x * ICON_SCALE, y * ICON_SCALE, ICON_SCALE - 1, ICON_SCALE - 1);
    }
  }
}

function canvasCoords(evt) {
  const rect = els.iconCanvas.getBoundingClientRect();
  return {
    x: Math.max(0, Math.min(ICON_SIZE - 1, Math.floor(((evt.clientX - rect.left) / rect.width) * ICON_SIZE))),
    y: Math.max(0, Math.min(ICON_SIZE - 1, Math.floor(((evt.clientY - rect.top) / rect.height) * ICON_SIZE))),
  };
}

function applyShapeToBitmap(bitmap, start, end) {
  if (iconTool === "square") {
    drawSquareOnBitmap(bitmap, start.x, start.y, end.x, end.y, 1);
  } else if (iconTool === "circle") {
    drawCircleOnBitmap(bitmap, start.x, start.y, end.x, end.y, 1);
  } else if (iconTool === "sine") {
    drawSineWaveOnBitmap(bitmap, start.x, start.y, end.x, end.y, 1);
  } else if (iconTool === "sqwave") {
    drawSquareWaveOnBitmap(bitmap, start.x, start.y, end.x, end.y, 1);
  }
}

function setActiveIconTool(tool) {
  iconTool = tool;
  const toolButtons = [
    els.iconDrawBtn,
    els.iconEraseBtn,
    els.iconFillBtn,
    els.iconSquareBtn,
    els.iconCircleBtn,
    els.iconSineBtn,
    els.iconSqWaveBtn,
  ];
  for (const btn of toolButtons) {
    if (btn) btn.classList.toggle("active", btn.dataset.tool === tool);
  }
}

els.iconCanvas.addEventListener("mousedown", (e) => {
  const pt = canvasCoords(e);
  if (iconTool === "fill") {
    floodFillBitmap(iconBitmap, pt.x, pt.y);
    drawIconCanvas();
    return;
  }
  isDrawingIcon = true;
  dragStart = pt;
  if (iconTool === "draw" || iconTool === "erase") {
    setBitmapPixel(iconBitmap, pt.x, pt.y, iconTool === "draw" ? 1 : 0);
    drawIconCanvas();
  } else {
    dragSnapshot = cloneBitmap(iconBitmap);
    const preview = cloneBitmap(dragSnapshot);
    applyShapeToBitmap(preview, dragStart, pt);
    drawIconCanvas(preview);
  }
});

window.addEventListener("mouseup", (e) => {
  if (!isDrawingIcon) return;
  if (dragSnapshot && dragStart) {
    const pt = canvasCoords(e);
    applyShapeToBitmap(iconBitmap, dragStart, pt);
    drawIconCanvas();
  }
  isDrawingIcon = false;
  dragStart = null;
  dragSnapshot = null;
});

els.iconCanvas.addEventListener("mousemove", (e) => {
  if (!isDrawingIcon) return;
  const pt = canvasCoords(e);
  if (iconTool === "draw" || iconTool === "erase") {
    setBitmapPixel(iconBitmap, pt.x, pt.y, iconTool === "draw" ? 1 : 0);
    drawIconCanvas();
  } else if (dragSnapshot && dragStart) {
    const preview = cloneBitmap(dragSnapshot);
    applyShapeToBitmap(preview, dragStart, pt);
    drawIconCanvas(preview);
  }
});

[
  els.iconDrawBtn,
  els.iconEraseBtn,
  els.iconFillBtn,
  els.iconSquareBtn,
  els.iconCircleBtn,
  els.iconSineBtn,
  els.iconSqWaveBtn,
].forEach((btn) => {
  if (btn) {
    btn.addEventListener("click", () => setActiveIconTool(btn.dataset.tool));
  }
});
els.iconInvertBtn.addEventListener("click", () => {
  for (let y = 0; y < ICON_SIZE; y++) {
    for (let x = 0; x < ICON_SIZE; x++) {
      iconBitmap[y][x] = iconBitmap[y][x] ? 0 : 1;
    }
  }
  drawIconCanvas();
});
els.iconClearBtn.addEventListener("click", () => {
  for (let y = 0; y < ICON_SIZE; y++) iconBitmap[y].fill(0);
  drawIconCanvas();
});
drawIconCanvas();

// ---- Live Faust WASM Preview (@grame/faustwasm) & Test Synth ----
let wasmAudioCtx = null;
let wasmInputBus = null;
let wasmNode = null;

function ensureStudioAudio() {
  if (!wasmAudioCtx) {
    wasmAudioCtx = new AudioContext({ sampleRate: 44100 });
    wasmInputBus = wasmAudioCtx.createGain();
    wasmInputBus.gain.value = 1.0;
    wasmInputBus.connect(wasmAudioCtx.destination);
  }
  if (wasmAudioCtx.state === "suspended") {
    return wasmAudioCtx.resume();
  }
  return Promise.resolve();
}

function connectWasmPreviewNode(node) {
  if (wasmNode) {
    try {
      wasmInputBus.disconnect(wasmNode);
      wasmNode.disconnect();
    } catch {}
  } else {
    try {
      wasmInputBus.disconnect(wasmAudioCtx.destination);
    } catch {}
  }
  wasmNode = node;
  wasmInputBus.connect(wasmNode);
  wasmNode.connect(wasmAudioCtx.destination);
}

function disconnectWasmPreviewNode() {
  if (wasmNode) {
    try {
      wasmInputBus.disconnect(wasmNode);
      wasmNode.disconnect();
    } catch {}
    wasmNode = null;
    if (wasmInputBus && wasmAudioCtx) {
      wasmInputBus.connect(wasmAudioCtx.destination);
    }
  }
}

const faustAllowlist = {
  allowedImports: new Set(["stdfaust.lib", "zoom.lib"]),
  allowedModules: new Set(["aa", "an", "ba", "co", "de", "ef", "en", "fi", "ma", "no", "os", "re", "ro", "si", "sp", "ve"]),
  allowedModuleFunctions: {
    co: new Set(["compressor_mono", "compressor_stereo", "limiter_1176_R4_mono", "limiter_1176_R4_stereo"]),
    ef: new Set(["cubicnl", "cubicnl_nodc", "gate_mono", "gate_stereo", "speakerbp", "stereo_width", "ms_enc", "ms_dec", "dryWetMixer", "dryWetMixerConstantPower", "echo", "transpose", "softclipQuadratic", "wavefold"]),
    re: new Set(["dattorro_rev", "jcrev"]),
    ve: new Set(["moog_vcf", "moogLadder", "korg35LPF", "oberheimLPF", "crybaby", "autowah", "wah4"]),
  },
  forbiddenDirectives: ["ffunction", "fvariable", "fconstant"],
  unsupportedUiElements: ["hbargraph", "vbargraph"],
  alternativeSuggestions: {
    "re.stereo_freeverb": "Use zm_freeverb(room, damp, width) from zoom.lib, or re.dattorro_rev / re.jcrev.",
    "re.mono_freeverb": "Use zm_freeverb(room, damp, width) from zoom.lib, or re.dattorro_rev / re.jcrev.",
    "co.RMS_FBFFcompressor_N_chan": "Use co.compressor_stereo, co.compressor_mono, or co.limiter_1176_R4_stereo.",
  },
};

function validateDspAllowlist(dspCode) {
  let clean = dspCode.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
  const impRe = /import\s*\(\s*"([^"]+)"\s*\)/g;
  let m;
  while ((m = impRe.exec(clean)) !== null) {
    const lib = m[1].trim();
    if (!faustAllowlist.allowedImports.has(lib)) {
      const allowed = Array.from(faustAllowlist.allowedImports).sort().join(", ");
      return `Disallowed Faust import '${lib}'. Allowed: ${allowed}.`;
    }
  }
  const noStrings = clean.replace(/"(?:\\.|[^"\\])*"/g, '""');
  for (const dir of faustAllowlist.forbiddenDirectives) {
    if (new RegExp(`\\b${dir}\\s*\\(`).test(noStrings)) {
      return `Disallowed Faust FFI directive '${dir}(...)'.`;
    }
  }
  for (const ui of faustAllowlist.unsupportedUiElements) {
    if (new RegExp(`\\b${ui}\\s*\\(`).test(noStrings)) {
      return `Unsupported Faust UI element '${ui}(...)'. Zoom MS Plus hardware does not support output meters/bargraphs.`;
    }
  }
  const modRe = /\b([a-z]{2})\.([A-Za-z_][A-Za-z0-9_]*)\b/g;
  const badMods = new Set();
  while ((m = modRe.exec(noStrings)) !== null) {
    const mod = m[1];
    const sym = m[2];
    const full = `${mod}.${sym}`;
    const alt = faustAllowlist.alternativeSuggestions[full] || faustAllowlist.alternativeSuggestions[mod];
    const altSuffix = alt ? ` Alternative: ${alt}` : "";
    if (!faustAllowlist.allowedModules.has(mod)) {
      badMods.add(`${full} (module '${mod}' not allowed).${altSuffix}`);
    } else {
      const fnSet = faustAllowlist.allowedModuleFunctions[mod];
      if (fnSet && !fnSet.has(sym)) {
        badMods.add(`${full} (allowed in ${mod}.*: ${Array.from(fnSet).sort().join(", ")}).${altSuffix}`);
      }
    }
  }
  if (badMods.size > 0) {
    return `Disallowed Faust symbol(s): ${Array.from(badMods).sort().join("; ")}.`;
  }
  const delRe = /\bde\.(?:delay|fdelay[1-3]?|sdelay)\s*\(\s*(\d+)\s*,/g;
  while ((m = delRe.exec(noStrings)) !== null) {
    const n = parseInt(m[1], 10);
    if (n <= 0 || (n & (n - 1)) !== 0) {
      let nextP2 = 1;
      while (nextP2 < Math.max(1, n)) nextP2 *= 2;
      return `Non-power-of-two delay buffer size ${n} in '${m[0]} ...)' is not supported on Zoom MS Plus (emits '%' modulo / CALLP __c6xabi_remi). Use a power of two instead (e.g. ${nextP2}).`;
    }
  }
  return null;
}

els.wasmPreviewBtn.addEventListener("click", async () => {
  const allowErr = validateDspAllowlist(els.dspEditor.value);
  if (allowErr) {
    els.wasmStatus.textContent = `Blocked by allowlist: ${allowErr}`;
    return;
  }
  els.wasmStatus.textContent = "WASM Preview: Compiling DSP via @grame/faustwasm…";
  try {
    await ensureStudioAudio();
    const faustModule = await import("https://cdn.jsdelivr.net/npm/@grame/faustwasm/+esm");
    const {
      instantiateFaustModule,
      instantiateFaustModuleFromFile,
      LibFaust,
      FaustCompiler,
      FaustMonoDspGenerator,
    } = faustModule;

    const module = instantiateFaustModule
      ? await instantiateFaustModule()
      : await instantiateFaustModuleFromFile(
          "https://cdn.jsdelivr.net/npm/@grame/faustwasm/libfaust-wasm/libfaust-wasm.js"
        );
    const libFaust = new LibFaust(module);
    const compiler = new FaustCompiler(libFaust);
    const generator = new FaustMonoDspGenerator();
    let previewDsp = els.dspEditor.value;
    if (/import\s*\(\s*"zoom\.lib"\s*\)\s*;/.test(previewDsp)) {
      try {
        const zResp = await fetch("/api/faust/zoom.lib");
        if (zResp.ok) {
          const zLib = await zResp.text();
          previewDsp = previewDsp.replace(
            /import\s*\(\s*"zoom\.lib"\s*\)\s*;/g,
            `\n// ---- inlined zoom.lib ----\n${zLib}\n`
          );
        }
      } catch {}
    }
    if (typeof compiler.expandDsp === "function") {
      const expanded = compiler.expandDsp(previewDsp, "");
      if (expanded) {
        const noStrings = expanded.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "").replace(/"(?:\\.|[^"\\])*"/g, '""');
        for (const ui of faustAllowlist.unsupportedUiElements) {
          if (new RegExp(`\\b${ui}\\s*\\(`).test(noStrings)) {
            els.wasmStatus.textContent = `Blocked by allowlist: Transitive '${ui}(...)' detected after DSP expansion.`;
            return;
          }
        }
      }
    }
    await generator.compile(compiler, "StomphacksPreview", previewDsp, "");
    const node = await generator.createNode(wasmAudioCtx);
    connectWasmPreviewNode(node);
    applyAllKnobValuesToWasmNode();
    els.wasmPreviewBtn.disabled = true;
    els.wasmStopBtn.disabled = false;
    els.wasmStatus.textContent =
      "WASM Preview: Active! Play the Test Synth (303 Loop or A-K keys) to audition.";
  } catch (err) {
    els.wasmStatus.textContent = `WASM Preview unavailable (${err.message}). Server build still works.`;
  }
});

els.wasmStopBtn.addEventListener("click", () => {
  disconnectWasmPreviewNode();
  els.wasmPreviewBtn.disabled = false;
  els.wasmStopBtn.disabled = true;
  els.wasmStatus.textContent = "WASM Preview: Stopped (Test Synth bypassed directly to output).";
});

// ---- Faust UI Knob Parser & Live Parameter Binding ----
const faustKnobsContainer = document.getElementById("faustKnobsContainer");
const faustKnobCount = document.getElementById("faustKnobCount");
const faustParamValues = new Map();

function parseFaustUiControls(dspCode) {
  const controls = [];
  const sliderRe = /\b(hslider|vslider|nentry)\s*\(\s*"([^"]+)"\s*,\s*([^,]+)\s*,\s*([^,]+)\s*,\s*([^,]+)\s*,\s*([^)]+)\)/g;
  const toggleRe = /\b(checkbox|button)\s*\(\s*"([^"]+)"\s*\)/g;
  let m;
  while ((m = sliderRe.exec(dspCode)) !== null) {
    const label = m[2].replace(/\[[^\]]*\]/g, "").trim() || m[2];
    const init = parseFloat(m[3]);
    const min = parseFloat(m[4]);
    const max = parseFloat(m[5]);
    const step = parseFloat(m[6]);
    if (!Number.isNaN(init) && !Number.isNaN(min) && !Number.isNaN(max)) {
      controls.push({
        kind: m[1],
        rawLabel: m[2],
        name: label,
        init,
        min,
        max,
        step: Number.isNaN(step) || step <= 0 ? (max - min) / 100 : step,
        index: m.index,
      });
    }
  }
  while ((m = toggleRe.exec(dspCode)) !== null) {
    const label = m[2].replace(/\[[^\]]*\]/g, "").trim() || m[2];
    controls.push({
      kind: m[1],
      rawLabel: m[2],
      name: label,
      init: 0,
      min: 0,
      max: 1,
      step: 1,
      index: m.index,
    });
  }
  controls.sort((a, b) => a.index - b.index);
  return controls;
}

function setWasmNodeParam(ctrlName, value) {
  if (!wasmNode) return;
  try {
    const params = typeof wasmNode.getParams === "function" ? wasmNode.getParams() : [];
    const target = params.find((p) => p.endsWith(`/${ctrlName}`) || p === ctrlName);
    if (target && typeof wasmNode.setParamValue === "function") {
      wasmNode.setParamValue(target, value);
    }
  } catch {}
}

function applyAllKnobValuesToWasmNode() {
  for (const [name, val] of faustParamValues.entries()) {
    setWasmNodeParam(name, val);
  }
}

function renderFaustKnobs() {
  if (!faustKnobsContainer) return;
  const controls = parseFaustUiControls(els.dspEditor.value);
  faustKnobsContainer.innerHTML = "";
  if (faustKnobCount) {
    faustKnobCount.textContent = `${controls.length} ${controls.length === 1 ? "knob" : "knobs"}`;
  }
  if (controls.length === 0) {
    const empty = document.createElement("div");
    empty.className = "muted";
    empty.style.fontSize = "12px";
    empty.textContent = "No hslider/vslider/nentry/checkbox controls detected in DSP.";
    faustKnobsContainer.appendChild(empty);
    return;
  }

  for (const ctrl of controls) {
    const currentVal = faustParamValues.has(ctrl.name) ? faustParamValues.get(ctrl.name) : ctrl.init;
    faustParamValues.set(ctrl.name, currentVal);

    const card = document.createElement("div");
    card.className = "faust-knob-card";

    const top = document.createElement("div");
    top.className = "faust-knob-top";
    const nameEl = document.createElement("span");
    nameEl.className = "faust-knob-name";
    nameEl.title = ctrl.name;
    nameEl.textContent = ctrl.name;
    const valEl = document.createElement("span");
    valEl.className = "faust-knob-val mono";
    valEl.textContent = Number.isInteger(ctrl.step) ? String(currentVal) : Number(currentVal).toFixed(2);
    top.appendChild(nameEl);
    top.appendChild(valEl);
    card.appendChild(top);

    if (ctrl.kind === "checkbox" || ctrl.kind === "button") {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `btn btn-sm ${currentVal ? "btn-primary" : "btn-ghost"}`;
      btn.textContent = currentVal ? "ON" : "OFF";
      btn.addEventListener("click", () => {
        const next = faustParamValues.get(ctrl.name) ? 0 : 1;
        faustParamValues.set(ctrl.name, next);
        btn.className = `btn btn-sm ${next ? "btn-primary" : "btn-ghost"}`;
        btn.textContent = next ? "ON" : "OFF";
        valEl.textContent = String(next);
        setWasmNodeParam(ctrl.name, next);
      });
      card.appendChild(btn);
    } else {
      const slider = document.createElement("input");
      slider.type = "range";
      slider.min = String(ctrl.min);
      slider.max = String(ctrl.max);
      slider.step = String(ctrl.step);
      slider.value = String(currentVal);
      slider.addEventListener("input", () => {
        const v = parseFloat(slider.value);
        faustParamValues.set(ctrl.name, v);
        valEl.textContent = Number.isInteger(ctrl.step) ? String(v) : v.toFixed(2);
        setWasmNodeParam(ctrl.name, v);
      });
      card.appendChild(slider);
    }
    faustKnobsContainer.appendChild(card);
  }
}

els.dspEditor.addEventListener("input", () => {
  renderFaustKnobs();
  const allowErr = validateDspAllowlist(els.dspEditor.value);
  if (allowErr && !wasmNode) {
    els.wasmStatus.textContent = `Allowlist warning: ${allowErr}`;
  } else if (!allowErr && !wasmNode) {
    els.wasmStatus.textContent = "WASM Preview: Ready (uses @grame/faustwasm in-browser AudioWorklet).";
  }
});
renderFaustKnobs();

// ---- TB-303 Acid Sequencer & Keyboard ----
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

const ACID_PATTERNS = {
  acid1: [
    { n: 36, a: 1, s: 0 }, { n: 36, a: 0, s: 0 }, { n: 48, a: 0, s: 1 }, { n: 36, a: 0, s: 0 },
    { n: 39, a: 1, s: 0 }, { n: 43, a: 0, s: 1 }, { n: 36, a: 0, s: 0 }, { n: null, a: 0, s: 0 },
    { n: 36, a: 1, s: 0 }, { n: 46, a: 0, s: 1 }, { n: 48, a: 1, s: 0 }, { n: 43, a: 0, s: 0 },
    { n: 39, a: 0, s: 1 }, { n: 36, a: 0, s: 0 }, { n: 41, a: 1, s: 0 }, { n: 43, a: 0, s: 1 },
  ],
  acid2: [
    { n: 36, a: 1, s: 0 }, { n: null, a: 0, s: 0 }, { n: 36, a: 0, s: 0 }, { n: 48, a: 1, s: 1 },
    { n: 43, a: 0, s: 0 }, { n: 36, a: 0, s: 0 }, { n: 37, a: 1, s: 1 }, { n: 36, a: 0, s: 0 },
    { n: 39, a: 0, s: 0 }, { n: 43, a: 1, s: 0 }, { n: 48, a: 0, s: 1 }, { n: 51, a: 1, s: 0 },
    { n: 48, a: 0, s: 1 }, { n: 43, a: 0, s: 0 }, { n: 39, a: 1, s: 0 }, { n: 36, a: 0, s: 0 },
  ],
  ebm: [
    { n: 36, a: 1, s: 0 }, { n: 48, a: 0, s: 0 }, { n: 36, a: 0, s: 0 }, { n: 48, a: 1, s: 0 },
    { n: 36, a: 0, s: 0 }, { n: 48, a: 0, s: 0 }, { n: 39, a: 1, s: 0 }, { n: 51, a: 0, s: 0 },
    { n: 36, a: 1, s: 0 }, { n: 48, a: 0, s: 0 }, { n: 36, a: 0, s: 0 }, { n: 48, a: 1, s: 0 },
    { n: 43, a: 1, s: 0 }, { n: 41, a: 0, s: 0 }, { n: 39, a: 1, s: 1 }, { n: 36, a: 0, s: 0 },
  ],
  minimal: [
    { n: 36, a: 1, s: 0 }, { n: null, a: 0, s: 0 }, { n: null, a: 0, s: 0 }, { n: 36, a: 0, s: 0 },
    { n: null, a: 0, s: 0 }, { n: 43, a: 0, s: 1 }, { n: 36, a: 0, s: 0 }, { n: null, a: 0, s: 0 },
    { n: 37, a: 1, s: 0 }, { n: null, a: 0, s: 0 }, { n: 36, a: 0, s: 0 }, { n: null, a: 0, s: 0 },
    { n: 48, a: 1, s: 1 }, { n: 43, a: 0, s: 0 }, { n: null, a: 0, s: 0 }, { n: 39, a: 0, s: 0 },
  ],
};

let studioAcidVoice = null;
let studioAutoRiffTimer = null;
let studioAutoRiffStep = 0;
let studioAcidNextStepTime = 0;
let studioCurrentAcidPattern = [...ACID_PATTERNS.acid1];

const studioAutoRiffBtn = document.getElementById("autoRiffBtn");
const studioAcidRandBtn = document.getElementById("acidRandBtn");
const studioVoiceSelect = document.getElementById("synthVoiceSelect");
const studioPatternSelect = document.getElementById("acidPatternSelect");
const studioCutoffSlider = document.getElementById("acidCutoffSlider");
const studioResSlider = document.getElementById("acidResSlider");
const studioMiniKeyboard = document.getElementById("miniKeyboard");

function midiToHz(midi) {
  return 440 * Math.pow(2, (midi - 69) / 12);
}

function ensureStudioAcidVoice() {
  if (studioAcidVoice) return studioAcidVoice;
  const osc = wasmAudioCtx.createOscillator();
  osc.type = "sawtooth";
  const f1 = wasmAudioCtx.createBiquadFilter();
  const f2 = wasmAudioCtx.createBiquadFilter();
  f1.type = "lowpass";
  f2.type = "lowpass";
  const vca = wasmAudioCtx.createGain();
  vca.gain.value = 0.0;
  osc.connect(f1);
  f1.connect(f2);
  f2.connect(vca);
  vca.connect(wasmInputBus);
  osc.start();
  studioAcidVoice = { osc, f1, f2, vca, lastFreq: 65.41 };
  return studioAcidVoice;
}

function scheduleStudioAcidStep(freq, when, stepDur, accent, slide) {
  const v = ensureStudioAcidVoice();
  v.osc.type = studioVoiceSelect?.value === "acid_sqr" ? "square" : "sawtooth";
  const cutNorm = Number(studioCutoffSlider?.value ?? 38) / 100;
  const resNorm = Number(studioResSlider?.value ?? 72) / 100;
  const baseCut = 90 * Math.pow(31, cutNorm);
  const peakCut = Math.min(11000, baseCut * (accent ? 7.8 : 4.2));
  const q1 = 1.0 + resNorm * 14.5 + (accent ? 2.5 : 0);
  const q2 = 0.8 + resNorm * 7.5;
  const dec = accent ? 0.14 : 0.21;
  const gate = slide ? stepDur * 1.05 : stepDur * 0.62;

  v.f1.Q.setValueAtTime(q1, when);
  v.f2.Q.setValueAtTime(q2, when);
  v.osc.frequency.cancelScheduledValues(when);
  if (slide) {
    v.osc.frequency.setValueAtTime(v.lastFreq, when);
    v.osc.frequency.exponentialRampToValueAtTime(freq, when + Math.min(0.065, stepDur * 0.5));
  } else {
    v.osc.frequency.setValueAtTime(freq, when);
  }
  v.lastFreq = freq;

  v.f1.frequency.cancelScheduledValues(when);
  v.f2.frequency.cancelScheduledValues(when);
  if (!slide) {
    v.f1.frequency.setValueAtTime(baseCut, when);
    v.f1.frequency.linearRampToValueAtTime(peakCut, when + 0.004);
    v.f1.frequency.exponentialRampToValueAtTime(Math.max(70, baseCut), when + dec);
    v.f2.frequency.setValueAtTime(baseCut * 1.15, when);
    v.f2.frequency.linearRampToValueAtTime(Math.min(12000, peakCut * 1.15), when + 0.004);
    v.f2.frequency.exponentialRampToValueAtTime(Math.max(80, baseCut * 1.15), when + dec);
  }

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

async function playStudioNote(freq) {
  await ensureStudioAudio();
  scheduleStudioAcidStep(freq, wasmAudioCtx.currentTime, 0.25, true, false);
}

if (studioAutoRiffBtn) {
  studioAutoRiffBtn.addEventListener("click", async () => {
    if (studioAutoRiffTimer) {
      clearInterval(studioAutoRiffTimer);
      studioAutoRiffTimer = null;
      if (studioAcidVoice && wasmAudioCtx) {
        studioAcidVoice.vca.gain.setTargetAtTime(0.0, wasmAudioCtx.currentTime, 0.015);
      }
      studioAutoRiffBtn.textContent = "▶ 303 Loop";
      studioAutoRiffBtn.classList.remove("btn-toggle", "active");
      studioAutoRiffBtn.classList.add("btn-primary");
      return;
    }
    await ensureStudioAudio();
    studioAutoRiffStep = 0;
    studioAcidNextStepTime = wasmAudioCtx.currentTime + 0.04;
    studioAutoRiffBtn.textContent = "■ Stop 303";
    studioAutoRiffBtn.classList.remove("btn-primary");
    studioAutoRiffBtn.classList.add("btn-toggle", "active");
    studioAutoRiffTimer = setInterval(() => {
      const stepDur = 60 / 120 / 4;
      while (studioAcidNextStepTime < wasmAudioCtx.currentTime + 0.1) {
        const st = studioCurrentAcidPattern[studioAutoRiffStep % studioCurrentAcidPattern.length];
        if (st.n !== null) {
          scheduleStudioAcidStep(midiToHz(st.n), studioAcidNextStepTime, stepDur, !!st.a, !!st.s);
        }
        studioAutoRiffStep++;
        studioAcidNextStepTime += stepDur;
      }
    }, 25);
  });
}

if (studioPatternSelect) {
  studioPatternSelect.addEventListener("change", () => {
    const key = studioPatternSelect.value;
    studioCurrentAcidPattern = [...(ACID_PATTERNS[key] || ACID_PATTERNS.acid1)];
  });
}

if (studioAcidRandBtn) {
  studioAcidRandBtn.addEventListener("click", () => {
    const scale = [36, 36, 37, 39, 41, 43, 46, 48, 51];
    studioCurrentAcidPattern = Array.from({ length: 16 }, (_, i) => {
      const rest = i !== 0 && Math.random() < 0.22;
      const n = rest ? null : i === 0 ? 36 : scale[Math.floor(Math.random() * scale.length)];
      return {
        n,
        a: !rest && (i % 4 === 0 || Math.random() < 0.35) ? 1 : 0,
        s: !rest && i > 0 && Math.random() < 0.28 ? 1 : 0,
      };
    });
  });
}

if (studioMiniKeyboard) {
  for (const k of KEYBOARD_NOTES) {
    const el = document.createElement("button");
    el.type = "button";
    el.className = `key-btn ${k.black ? "black" : "white"}`;
    el.dataset.key = k.key;
    el.textContent = k.key.toUpperCase();
    el.title = `${k.note} (${k.key.toUpperCase()})`;
    el.addEventListener("mousedown", () => playStudioNote(k.freq));
    studioMiniKeyboard.appendChild(el);
  }
  window.addEventListener("keydown", (e) => {
    if (e.repeat) return;
    if (e.target && (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT" || e.target.tagName === "SELECT")) {
      return;
    }
    const found = KEYBOARD_NOTES.find((k) => k.key === e.key.toLowerCase());
    if (found) playStudioNote(found.freq);
  });
}

// ---- Server Build & REPORT.md Gate ----
function renderBuildState(job) {
  currentBuild = job;
  els.stageList.innerHTML = "";
  for (const st of job.stages || []) {
    const row = document.createElement("div");
    row.textContent = `[${st.stage}] ${st.status}: ${st.message}`;
    els.stageList.appendChild(row);
  }
  els.artifactLinks.innerHTML = "";
  for (const fname of job.artifacts || []) {
    const a = document.createElement("a");
    a.href = `/api/build/${job.build_id}/artifacts/${encodeURIComponent(fname)}`;
    a.className = "btn btn-ghost btn-sm";
    a.textContent = `⬇ ${fname}`;
    a.download = fname;
    els.artifactLinks.appendChild(a);
  }
  els.reportViewer.textContent = job.report_text || job.report_summary || job.error || "";
  if (job.installable) {
    els.buildGateBadge.textContent = `PASS (installable · dspload ${job.dspload ?? "?"}%)`;
    els.buildGateBadge.className = "status-pill ok";
  } else if (job.status === "running" || job.status === "queued") {
    els.buildGateBadge.textContent = "Building…";
    els.buildGateBadge.className = "status-pill";
  } else {
    els.buildGateBadge.textContent = "FAIL (non-installable)";
    els.buildGateBadge.className = "status-pill fail";
  }
  updatePedalButtons();
}

const nextIdBtn = document.getElementById("nextIdBtn");

async function allocateNextFreeId({ fromDevice = true } = {}) {
  try {
    const resp = await fetch(`/api/pedal/next-id?group=7&from_device=${fromDevice ? "true" : "false"}`);
    if (!resp.ok) return;
    const data = await resp.json();
    if (data.id) {
      els.fxId.value = data.id;
      appendPedalLog(`Assigned next free DIY ID ${data.id} (checked ${data.source}).`);
    }
  } catch {}
}

if (nextIdBtn) {
  nextIdBtn.addEventListener("click", () => allocateNextFreeId({ fromDevice: pedalState.connected }));
}

els.buildBtn.addEventListener("click", async () => {
  const allowErr = validateDspAllowlist(els.dspEditor.value);
  if (allowErr) {
    els.buildGateBadge.textContent = "Allowlist Blocked";
    els.buildGateBadge.className = "status-pill fail";
    els.reportViewer.textContent = allowErr;
    return;
  }
  let fxIdVal = (els.fxId.value.trim() || "07001000").replace(/^0x/i, "");
  if (!/^[0-9a-fA-F]{8}$/.test(fxIdVal)) {
    els.buildGateBadge.textContent = "Invalid ID";
    els.buildGateBadge.className = "status-pill fail";
    els.reportViewer.textContent = `DIY Effect ID '${els.fxId.value.trim()}' must be 8 hex digits (e.g. 07001000). Click "Next Free" to auto-assign.`;
    return;
  }
  els.buildBtn.disabled = true;
  els.stageList.innerHTML = "<div>Starting isolated build…</div>";
  try {
    const resp = await fetch("/api/build", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        dsp_code: els.dspEditor.value,
        name: els.fxName.value.trim() || "MyGain",
        manifest: {
          name: els.fxName.value.trim() || "MyGain",
          filename: els.fxFilename.value.trim() || "XXMYGAIN",
          id: fxIdVal,
          author: "waveformer",
          version: els.fxVersion.value.trim() || "1.00",
          description: els.fxDesc.value.trim() || "",
        },
        icon_bitmap: iconBitmap,
        run_benchmark: true,
        async_mode: true,
      }),
    });
    const job = await resp.json();
    if (!resp.ok) throw new Error(job.detail || "Build start failed");
    renderBuildState(job);

    const es = new EventSource(`/api/build/${job.build_id}/events`);
    es.onmessage = (evt) => {
      const payload = JSON.parse(evt.data);
      if (payload.type === "done" && payload.job) {
        renderBuildState(payload.job);
        es.close();
        els.buildBtn.disabled = false;
      } else if (payload.stage) {
        const row = document.createElement("div");
        row.textContent = `[${payload.stage}] ${payload.status}: ${payload.message}`;
        els.stageList.appendChild(row);
        els.stageList.scrollTop = els.stageList.scrollHeight;
      }
    };
    es.onerror = () => {
      es.close();
      els.buildBtn.disabled = false;
    };
  } catch (err) {
    els.buildGateBadge.textContent = "Error";
    els.buildGateBadge.className = "status-pill fail";
    els.reportViewer.textContent = String(err.message || err);
    els.buildBtn.disabled = false;
  }
});

// ---- Pedal Web MIDI + Safety Modal ----
async function openConfirmationModal({ action, title, detailsText, onConfirm }) {
  const tokenResp = await fetch("/api/pedal/confirm-token", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, target: detailsText }),
  });
  const tokenData = await tokenResp.json();
  const token = tokenData.confirmation_token;

  els.modalTitle.textContent = title;
  els.modalDetails.textContent = detailsText;
  for (const chk of [els.chkAutosave, els.chkBackup, els.chkUnsaved, els.chkNoPower]) {
    chk.checked = false;
  }
  els.modalConfirmBtn.disabled = true;
  els.confirmModal.classList.remove("hidden");

  const getChecklist = () => ({
    autosave_off_confirmed: els.chkAutosave.checked,
    backup_confirmed: els.chkBackup.checked,
    unsaved_patch_only_confirmed: els.chkUnsaved.checked,
    no_power_cycle_confirmed: els.chkNoPower.checked,
  });

  const onCheckChange = () => {
    els.modalConfirmBtn.disabled = !isChecklistComplete(getChecklist());
  };
  const boxes = [els.chkAutosave, els.chkBackup, els.chkUnsaved, els.chkNoPower];
  boxes.forEach((b) => b.addEventListener("change", onCheckChange));

  const cleanup = () => {
    boxes.forEach((b) => b.removeEventListener("change", onCheckChange));
    els.confirmModal.classList.add("hidden");
    els.modalCancelBtn.onclick = null;
    els.modalConfirmBtn.onclick = null;
  };

  els.modalCancelBtn.onclick = cleanup;
  els.modalConfirmBtn.onclick = async () => {
    const checklist = getChecklist();
    cleanup();
    await onConfirm({ checklist, confirmation_token: token });
  };
}

els.webMidiBtn.addEventListener("click", async () => {
  try {
    const ports = await relay.openMidiPorts();
    await relay.connectWebSocket();
    appendPedalLog(`Web MIDI ready: ${ports.inputName}`);
  } catch (err) {
    appendPedalLog(err.message, "error");
  }
});

els.safeConnectBtn.addEventListener("click", async () => {
  try {
    if (!relay.ws || relay.ws.readyState !== WebSocket.OPEN) {
      await relay.connectWebSocket();
    }
    const resp = await fetch("/api/pedal/connect", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "Connect failed");
    pedalState.connected = data.connected;
    pedalState.autosave_off_confirmed = data.autosave_off_confirmed;
    appendPedalLog(`Connected: ${data.identity_printable || "Zoom MS+"} (Autosave OFF verified)`);
    updatePedalButtons();
    await allocateNextFreeId({ fromDevice: true });
  } catch (err) {
    appendPedalLog(err.message, "error");
  }
});

els.backupBtn.addEventListener("click", async () => {
  relay.attachUnloadGuard();
  relay.setTransferActive(true);
  try {
    const resp = await fetch("/api/pedal/backup", { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || "Backup failed");
    pedalState.backup_completed = true;
    appendPedalLog(`Backup complete (${data.files_count} files).`);
    updatePedalButtons();
  } catch (err) {
    appendPedalLog(err.message, "error");
  } finally {
    relay.setTransferActive(false);
    relay.detachUnloadGuard();
  }
});

els.installBtn.addEventListener("click", async () => {
  if (!currentBuild || !currentBuild.installable) return;
  await openConfirmationModal({
    action: "install",
    title: "Confirm Safe .ZD2 Install",
    detailsText:
      `Build ID: ${currentBuild.build_id}\n` +
      `Effect File: ${currentBuild.zd2_filename}\n` +
      `Icon File: ${currentBuild.zic_filename}\n` +
      `Required Free Margin: >= 100 KB (never < 32 KB)`,
    onConfirm: async ({ checklist, confirmation_token }) => {
      relay.attachUnloadGuard();
      relay.setTransferActive(true);
      try {
        const resp = await fetch("/api/pedal/install", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            build_id: currentBuild.build_id,
            confirmation_token,
            checklist,
          }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          if (resp.status === 423) showRescueLock(data.detail?.message || data.detail);
          throw new Error(data.detail?.message || data.detail || "Install failed");
        }
        appendPedalLog(`Installed ${data.filename} (${data.effect_id}) & verified!`);
      } catch (err) {
        appendPedalLog(err.message, "error");
      } finally {
        relay.setTransferActive(false);
        relay.detachUnloadGuard();
      }
    },
  });
});

els.uninstallBtn.addEventListener("click", async () => {
  if (!currentBuild) return;
  await openConfirmationModal({
    action: "uninstall",
    title: "Confirm Safe .ZD2 Uninstall",
    detailsText: `Build ID: ${currentBuild.build_id}\nEffect File: ${currentBuild.zd2_filename}`,
    onConfirm: async ({ checklist, confirmation_token }) => {
      relay.attachUnloadGuard();
      relay.setTransferActive(true);
      try {
        const resp = await fetch("/api/pedal/uninstall", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            build_id: currentBuild.build_id,
            confirmation_token,
            checklist,
          }),
        });
        const data = await resp.json();
        if (!resp.ok) {
          if (resp.status === 423) showRescueLock(data.detail?.message || data.detail);
          throw new Error(data.detail?.message || data.detail || "Uninstall failed");
        }
        appendPedalLog(`Uninstalled ${data.filename} & verified!`);
      } catch (err) {
        appendPedalLog(err.message, "error");
      } finally {
        relay.setTransferActive(false);
        relay.detachUnloadGuard();
      }
    },
  });
});

async function runRescue() {
  relay.attachUnloadGuard();
  relay.setTransferActive(true);
  try {
    const resp = await fetch("/api/pedal/rescue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ write_back: false }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail?.message || data.detail || "Rescue failed");
    pedalState.rescue_locked = false;
    clearRescueLock();
    appendPedalLog("Rescue succeeded: FLST_SEQ.ZT2 verified intact.");
    updatePedalButtons();
  } catch (err) {
    appendPedalLog(err.message, "error");
  } finally {
    relay.setTransferActive(false);
    relay.detachUnloadGuard();
  }
}

els.rescueBtn.addEventListener("click", runRescue);
els.bannerRescueBtn.addEventListener("click", runRescue);

// ---- Init Capabilities ----
(async () => {
  try {
    const resp = await fetch("/api/capabilities");
    const caps = await resp.json();
    const cgtLabel = caps.ti_cgt_available ? "TI CGT OK" : "TI CGT Missing";
    const midiLabel = caps.linux_midi_available ? "Linux MIDI" : "Web MIDI";
    els.capBadge.textContent = `${caps.profile.toUpperCase()} · ${cgtLabel} · ${midiLabel}`;
    els.capBadge.className = caps.ti_cgt_available ? "status-pill ok" : "status-pill";
    if (caps.pedal) {
      pedalState = caps.pedal;
      if (caps.pedal.rescue_locked) showRescueLock(caps.pedal.rescue_reason);
      updatePedalButtons();
    }
    await allocateNextFreeId({ fromDevice: false });
    try {
      const aResp = await fetch("/api/faust/allowlist");
      if (aResp.ok) {
        const aData = await aResp.json();
        faustAllowlist.allowedImports = new Set(aData.allowed_imports || []);
        faustAllowlist.allowedModules = new Set(aData.allowed_modules || []);
        faustAllowlist.allowedModuleFunctions = {};
        if (aData.allowed_module_functions) {
          for (const [mod, fns] of Object.entries(aData.allowed_module_functions)) {
            if (Array.isArray(fns)) faustAllowlist.allowedModuleFunctions[mod] = new Set(fns);
          }
        }
        faustAllowlist.forbiddenDirectives = aData.forbidden_directives || [];
        faustAllowlist.unsupportedUiElements = aData.unsupported_ui_elements || [];
        if (aData.alternative_suggestions) {
          faustAllowlist.alternativeSuggestions = aData.alternative_suggestions;
        }
      }
    } catch {}
  } catch {
    els.capBadge.textContent = "Server Offline";
    els.capBadge.className = "status-pill fail";
  }
})();
