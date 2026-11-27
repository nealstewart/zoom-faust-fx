/**
 * web/webmidi.js — Chromium Web MIDI SysEx Relay & Safety UI State Helpers
 *
 * Acts strictly as a timeout-bounded SysEx transport between the Zoom MS Plus
 * pedal and the Podman FastAPI server (/ws/pedal). All safety decisions,
 * FLST_SEQ.ZT2 validation, and readback comparisons remain server-side.
 */

export const ZOOM_PORT_PREFIXES = ["ZOOM MS Plus Series", "ZOOM G"];

export function isZoomMidiPortName(name) {
  if (!name) return false;
  return ZOOM_PORT_PREFIXES.some((prefix) => name.includes(prefix));
}

export function stripSysExFraming(bytes) {
  const arr = Array.from(bytes);
  if (arr.length >= 2 && arr[0] === 0xf0 && arr[arr.length - 1] === 0xf7) {
    return arr.slice(1, -1);
  }
  return arr;
}

export function addSysExFraming(payload) {
  const arr = Array.from(payload);
  if (arr.length >= 2 && arr[0] === 0xf0 && arr[arr.length - 1] === 0xf7) {
    return new Uint8Array(arr);
  }
  return new Uint8Array([0xf0, ...arr, 0xf7]);
}

export function isChecklistComplete(checklist) {
  return Boolean(
    checklist &&
      checklist.autosave_off_confirmed &&
      checklist.backup_confirmed &&
      checklist.unsaved_patch_only_confirmed &&
      checklist.no_power_cycle_confirmed
  );
}

export class WebMidiRelayClient {
  constructor({ onLog, onStatus, onRescueLock } = {}) {
    this.onLog = onLog || (() => {});
    this.onStatus = onStatus || (() => {});
    this.onRescueLock = onRescueLock || (() => {});
    this.midiAccess = null;
    this.inputPort = null;
    this.outputPort = null;
    this.ws = null;
    this.transferActive = false;
    this._beforeUnloadHandler = (e) => {
      if (this.transferActive) {
        e.preventDefault();
        e.returnValue = "A pedal SysEx transfer is in progress. Closing now could wedge the pedal!";
        return e.returnValue;
      }
      return undefined;
    };
  }

  attachUnloadGuard() {
    if (typeof window !== "undefined") {
      window.addEventListener("beforeunload", this._beforeUnloadHandler);
    }
  }

  detachUnloadGuard() {
    if (typeof window !== "undefined") {
      window.removeEventListener("beforeunload", this._beforeUnloadHandler);
    }
  }

  setTransferActive(active) {
    this.transferActive = Boolean(active);
  }

  async openMidiPorts() {
    if (typeof navigator === "undefined" || !navigator.requestMIDIAccess) {
      throw new Error(
        "Web MIDI API is not available in this browser. Use Chrome or Edge on Windows/macOS/Linux."
      );
    }
    this.midiAccess = await navigator.requestMIDIAccess({ sysex: true });
    if (!this.midiAccess.sysexEnabled) {
      throw new Error("SysEx permission was denied. Grant MIDI SysEx access and retry.");
    }

    let inPort = null;
    for (const port of this.midiAccess.inputs.values()) {
      if (isZoomMidiPortName(port.name)) {
        inPort = port;
        break;
      }
    }
    let outPort = null;
    for (const port of this.midiAccess.outputs.values()) {
      if (isZoomMidiPortName(port.name)) {
        outPort = port;
        break;
      }
    }
    if (!inPort || !outPort) {
      const seen = Array.from(this.midiAccess.inputs.values()).map((p) => p.name);
      throw new Error(
        `No Zoom MS Plus MIDI port found. Ports seen: ${seen.join(", ") || "(none)"}`
      );
    }

    this.inputPort = inPort;
    this.outputPort = outPort;
    this.inputPort.onmidimessage = (event) => {
      const data = event.data;
      if (!data || data.length < 2) return;
      if (data[0] === 0xf0 && this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(
          JSON.stringify({
            type: "sysex_rx",
            data: stripSysExFraming(data),
          })
        );
      }
    };
    return { inputName: inPort.name, outputName: outPort.name };
  }

  connectWebSocket(wsUrl) {
    return new Promise((resolve, reject) => {
      const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
      const url = wsUrl || `${proto}//${window.location.host}/ws/pedal`;
      const ws = new WebSocket(url);
      this.ws = ws;

      ws.onopen = () => {
        if (this.inputPort && this.outputPort) {
          ws.send(JSON.stringify({ type: "webmidi_ready" }));
        }
        resolve(ws);
      };

      ws.onerror = (err) => {
        reject(new Error("WebSocket /ws/pedal connection error."));
      };

      ws.onmessage = (evt) => {
        let msg;
        try {
          msg = JSON.parse(evt.data);
        } catch {
          return;
        }
        if (msg.type === "sysex_tx" && Array.isArray(msg.data)) {
          if (this.outputPort) {
            this.outputPort.send(addSysExFraming(msg.data));
          }
        } else if (msg.type === "log") {
          this.onLog(msg);
          if (msg.message && msg.message.includes("DO NOT POWER CYCLE")) {
            this.onRescueLock(msg.message);
          }
        } else if (msg.type === "status" && msg.pedal) {
          this.onStatus(msg.pedal);
        }
      };

      ws.onclose = () => {
        this.onLog({ level: "warning", message: "WebSocket /ws/pedal disconnected." });
      };
    });
  }

  close() {
    this.setTransferActive(false);
    this.detachUnloadGuard();
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "webmidi_closed" }));
      this.ws.close();
    }
    if (this.inputPort) {
      this.inputPort.onmidimessage = null;
    }
    this.inputPort = null;
    this.outputPort = null;
  }
}
