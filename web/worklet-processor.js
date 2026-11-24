/**
 * web/worklet-processor.js
 * Real-time AudioWorkletProcessor that executes compiled Stomphacks C kernels
 * (WebAssembly) in 16-sample Zoom MS Plus blocks (8 blocks per 128-sample quantum).
 */

const BLOCK_FRAMES = 16;
const BLOCKS_PER_QUANTUM = 8; // 8 * 16 = 128 samples

class StomphacksProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.slots = []; // [{ id, slug, exports, memory, eff, aux, gtrin, bypassed, ready }]
    this.globalBypass = false;
    this.inputGain = 1.0;
    this.outputGain = 1.0;
    this.masterMix = 1.0; // 0 = dry, 1 = wet

    // Scratch buffers for passing buses between chained slots
    this.mainL = new Float32Array(BLOCK_FRAMES);
    this.mainR = new Float32Array(BLOCK_FRAMES);
    this.auxL = new Float32Array(BLOCK_FRAMES);
    this.auxR = new Float32Array(BLOCK_FRAMES);
    this.gtrin = new Float32Array(BLOCK_FRAMES);
    this.dryL = new Float32Array(BLOCK_FRAMES);
    this.dryR = new Float32Array(BLOCK_FRAMES);

    // Metering
    this.meterCounter = 0;
    this.inPeakL = 0;
    this.inPeakR = 0;
    this.outPeakL = 0;
    this.outPeakR = 0;

    this.port.onmessage = (ev) => this.handleMessage(ev.data);
  }

  async handleMessage(msg) {
    if (!msg || !msg.type) return;

    switch (msg.type) {
      case "addSlot": {
        try {
          const { slotId, slug, wasmBytes, params, bypassed, insertIndex } = msg;
          const { instance } = await WebAssembly.instantiate(wasmBytes, {});
          const exp = instance.exports;
          exp.wasm_init();

          const mem = exp.memory;
          const effPtr = exp.wasm_get_eff_ptr();
          const auxPtr = exp.wasm_get_aux_ptr();
          const gtrinPtr = exp.wasm_get_gtrin_ptr();

          const slot = {
            id: slotId,
            slug,
            exports: exp,
            memory: mem,
            eff: new Float32Array(mem.buffer, effPtr, 32),
            aux: new Float32Array(mem.buffer, auxPtr, 32),
            gtrin: new Float32Array(mem.buffer, gtrinPtr, 16),
            bypassed: Boolean(bypassed),
            ready: true,
          };

          if (Array.isArray(params)) {
            for (const p of params) {
              exp.wasm_set_param(p.slot, p.coeff);
            }
          }
          exp.wasm_set_bypass(slot.bypassed ? 0.0 : 1.0);

          if (typeof insertIndex === "number" && insertIndex >= 0 && insertIndex <= this.slots.length) {
            this.slots.splice(insertIndex, 0, slot);
          } else {
            this.slots.push(slot);
          }
          this.port.postMessage({ type: "slotReady", slotId, slug });
        } catch (err) {
          this.port.postMessage({
            type: "slotError",
            slotId: msg.slotId,
            error: String(err && err.message ? err.message : err),
          });
        }
        break;
      }

      case "removeSlot": {
        this.slots = this.slots.filter((s) => s.id !== msg.slotId);
        break;
      }

      case "clearSlots": {
        this.slots = [];
        break;
      }

      case "reorderSlots": {
        const order = msg.order || [];
        const byId = new Map(this.slots.map((s) => [s.id, s]));
        const next = [];
        for (const id of order) {
          if (byId.has(id)) next.push(byId.get(id));
        }
        this.slots = next;
        break;
      }

      case "setParam": {
        const s = this.slots.find((x) => x.id === msg.slotId);
        if (s && s.ready) {
          s.exports.wasm_set_param(msg.coeffSlot, msg.coeff);
        }
        break;
      }

      case "setBypass": {
        const s = this.slots.find((x) => x.id === msg.slotId);
        if (s && s.ready) {
          s.bypassed = Boolean(msg.bypassed);
          s.exports.wasm_set_bypass(s.bypassed ? 0.0 : 1.0);
        }
        break;
      }

      case "resetAllAudio": {
        for (const s of this.slots) {
          if (s.ready && s.exports.wasm_reset_audio) {
            s.exports.wasm_reset_audio();
          }
        }
        break;
      }

      case "setMaster": {
        if (typeof msg.globalBypass === "boolean") this.globalBypass = msg.globalBypass;
        if (typeof msg.inputGain === "number") this.inputGain = msg.inputGain;
        if (typeof msg.outputGain === "number") this.outputGain = msg.outputGain;
        if (typeof msg.masterMix === "number") this.masterMix = msg.masterMix;
        break;
      }
    }
  }

  process(inputs, outputs) {
    const input = inputs[0];
    const output = outputs[0];
    if (!output || output.length === 0) return true;

    const outL = output[0];
    const outR = output.length > 1 ? output[1] : null;
    const frames = outL.length; // 128

    const inL = input && input.length > 0 ? input[0] : null;
    const inR = input && input.length > 1 ? input[1] : inL;

    const inGain = this.inputGain;
    const outGain = this.outputGain;
    const mix = this.masterMix;
    const numBlocks = Math.floor(frames / BLOCK_FRAMES);

    let pkInL = this.inPeakL;
    let pkInR = this.inPeakR;
    let pkOutL = this.outPeakL;
    let pkOutR = this.outPeakR;

    for (let b = 0; b < numBlocks; b++) {
      const base = b * BLOCK_FRAMES;

      // 1. Load 16-sample input block + input gain + pre-chain gtrin
      for (let i = 0; i < BLOCK_FRAMES; i++) {
        const sL = inL ? inL[base + i] * inGain : 0.0;
        const sR = inR ? inR[base + i] * inGain : 0.0;
        this.mainL[i] = sL;
        this.mainR[i] = sR;
        this.dryL[i] = sL;
        this.dryR[i] = sR;
        this.gtrin[i] = 0.5 * (sL + sR);
        this.auxL[i] = 0.0;
        this.auxR[i] = 0.0;

        const aL = sL < 0 ? -sL : sL;
        const aR = sR < 0 ? -sR : sR;
        if (aL > pkInL) pkInL = aL;
        if (aR > pkInR) pkInR = aR;
      }

      // 2. Run through WASM effect chain (unless globally bypassed)
      if (!this.globalBypass && this.slots.length > 0) {
        for (let k = 0; k < this.slots.length; k++) {
          const slot = this.slots[k];
          if (!slot.ready) continue;

          const eff = slot.eff;
          const aux = slot.aux;
          const gtr = slot.gtrin;

          for (let i = 0; i < BLOCK_FRAMES; i++) {
            eff[i] = this.mainL[i];
            eff[i + 16] = this.mainR[i];
            aux[i] = this.auxL[i];
            aux[i + 16] = this.auxR[i];
            gtr[i] = this.gtrin[i];
          }

          slot.exports.wasm_process_block();

          for (let i = 0; i < BLOCK_FRAMES; i++) {
            let yL = eff[i];
            let yR = eff[i + 16];
            let aL = aux[i];
            let aR = aux[i + 16];
            // NaN / Inf safety scrub between slots
            if (!Number.isFinite(yL)) yL = 0.0;
            if (!Number.isFinite(yR)) yR = 0.0;
            if (!Number.isFinite(aL)) aL = 0.0;
            if (!Number.isFinite(aR)) aR = 0.0;
            this.mainL[i] = yL;
            this.mainR[i] = yR;
            this.auxL[i] = aL;
            this.auxR[i] = aR;
          }
        }
      }

      // 3. Master Wet/Dry blend, output gain, safety soft-clipper, and write to Web Audio out
      for (let i = 0; i < BLOCK_FRAMES; i++) {
        let yL = (this.dryL[i] * (1.0 - mix) + this.mainL[i] * mix) * outGain;
        let yR = (this.dryR[i] * (1.0 - mix) + this.mainR[i] * mix) * outGain;

        // Hard safety ceiling at +/- 1.0 (gentle tanh knee above 0.92)
        if (yL > 0.92 || yL < -0.92) yL = Math.tanh(yL);
        if (yR > 0.92 || yR < -0.92) yR = Math.tanh(yR);

        outL[base + i] = yL;
        if (outR) outR[base + i] = yR;

        const aL = yL < 0 ? -yL : yL;
        const aR = yR < 0 ? -yR : yR;
        if (aL > pkOutL) pkOutL = aL;
        if (aR > pkOutR) pkOutR = aR;
      }
    }

    this.inPeakL = pkInL;
    this.inPeakR = pkInR;
    this.outPeakL = pkOutL;
    this.outPeakR = pkOutR;

    this.meterCounter++;
    if (this.meterCounter >= 18) {
      this.port.postMessage({
        type: "meters",
        inL: this.inPeakL,
        inR: this.inPeakR,
        outL: this.outPeakL,
        outR: this.outPeakR,
      });
      this.inPeakL = 0;
      this.inPeakR = 0;
      this.outPeakL = 0;
      this.outPeakR = 0;
      this.meterCounter = 0;
    }

    return true;
  }
}

registerProcessor("stomphacks-processor", StomphacksProcessor);
