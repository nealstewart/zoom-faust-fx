"""Single-flight MIDI mutex, Web MIDI SysEx relay, and SAFETY.md state machine."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import queue
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import find_stomphacks_dir, safety


class PedalBusyError(RuntimeError):
    """Raised when a pedal operation is already in flight (409 Conflict)."""


class SafetyGateError(ValueError):
    """Raised when a SAFETY.md pre-flight or checklist gate refuses an operation."""


class RescueLockError(RuntimeError):
    """Raised when the pedal is locked in DO-NOT-POWER-CYCLE state until rescue."""


class PedalTransferError(RuntimeError):
    """Raised when a MIDI transfer fails (may or may not trigger rescue lock)."""

    def __init__(self, message: str, *, rescue_required: bool = False) -> None:
        super().__init__(message)
        self.rescue_required = rescue_required


@dataclass(slots=True)
class ChecklistAck:
    """Mandatory 4-item anti-bricking checklist from SAFETY.md."""

    autosave_off_confirmed: bool = False
    backup_confirmed: bool = False
    unsaved_patch_only_confirmed: bool = False
    no_power_cycle_confirmed: bool = False

    def validate(self) -> None:
        missing = []
        if not self.autosave_off_confirmed:
            missing.append("autosave_off_confirmed")
        if not self.backup_confirmed:
            missing.append("backup_confirmed")
        if not self.unsaved_patch_only_confirmed:
            missing.append("unsaved_patch_only_confirmed")
        if not self.no_power_cycle_confirmed:
            missing.append("no_power_cycle_confirmed")
        if missing:
            raise SafetyGateError(
                "Anti-bricking checklist incomplete; must confirm: " + ", ".join(missing)
            )


class _DummySysExMsg:
    """Lightweight mido.Message('sysex', ...) stand-in when mido is absent or mocked."""

    def __init__(self, data: list[int] | bytes) -> None:
        self.type = "sysex"
        self.data = list(data)


class _AdapterOutport:
    def __init__(self, send_fn: Callable[[list[int]], None]) -> None:
        self._send = send_fn

    def send(self, msg: Any) -> None:
        self._send(list(msg.data))


class _AdapterInport:
    def __init__(self, recv_fn: Callable[[float], list[int] | bytes | None]) -> None:
        self._recv = recv_fn

    def poll(self) -> _DummySysExMsg | None:
        data = self._recv(0.0)
        if data is None:
            return None
        return _DummySysExMsg(list(data))


class TransportAdapter:
    """Wraps a .send(payload)/.recv(timeout) transport to look like a zoomzt2 pedal."""

    def __init__(self, transport: Any) -> None:
        self.transport = transport
        self.outport = _AdapterOutport(transport.send)
        self.inport = _AdapterInport(transport.recv)

    def send(self, payload: list[int] | bytes) -> None:
        self.transport.send(list(payload))

    def recv(self, timeout: float = 5.0) -> list[int] | bytes | None:
        return self.transport.recv(timeout)

    def disconnect(self) -> None:
        close_fn = getattr(self.transport, "close", None)
        if callable(close_fn):
            close_fn()


class DirectMidiTransport:
    """Linux ALSA / mido transport implementing .send(payload) and .recv(timeout)."""

    def __init__(self, stomphacks_dir: Path) -> None:
        zt2_dir = str(stomphacks_dir / "zoom-zt2")
        if zt2_dir not in sys.path:
            sys.path.insert(0, zt2_dir)
        import mido
        import zoomzt2

        self._mido = mido
        self._pedal = zoomzt2.zoomzt2()
        if not self._pedal.connect():
            raise SafetyGateError(
                "No USB-MIDI pedal matching 'ZOOM MS Plus Series' found. "
                f"Ports seen: {mido.get_input_names()}"
            )

    def send(self, payload: list[int] | bytes) -> None:
        self._pedal.outport.send(self._mido.Message("sysex", data=list(payload)))

    def recv(self, timeout: float = 5.0) -> list[int] | None:
        deadline = time.time() + max(0.0, timeout)
        while True:
            msg = self._pedal.inport.poll()
            if msg is not None:
                return list(msg.data)
            if time.time() >= deadline:
                return None
            time.sleep(0.005)

    def close(self) -> None:
        try:
            self._pedal.disconnect()
        except Exception:
            pass


class WebMidiTransport:
    """Relay transport bridging file_session's sync send/recv to WS /ws/pedal."""

    def __init__(self, loop: asyncio.AbstractEventLoop, send_ws_json: Callable[[dict[str, Any]], Any]) -> None:
        self._loop = loop
        self._send_ws_json = send_ws_json
        self._rx_queue: queue.Queue[list[int] | None] = queue.Queue()
        self._closed = False

    def send(self, payload: list[int] | bytes) -> None:
        if self._closed:
            raise PedalTransferError("Web MIDI client disconnected.")
        msg = {"type": "sysex_tx", "data": list(payload)}
        fut = asyncio.run_coroutine_threadsafe(self._send_ws_json(msg), self._loop)
        fut.result(timeout=5.0)

    def recv(self, timeout: float = 5.0) -> list[int] | None:
        if self._closed:
            return None
        try:
            item = self._rx_queue.get(timeout=max(0.001, timeout))
            return item
        except queue.Empty:
            return None

    def push_rx(self, data: list[int]) -> None:
        if not self._closed:
            self._rx_queue.put(list(data))

    def close(self) -> None:
        self._closed = True
        self._rx_queue.put(None)


def _load_tool(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load {name} from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _printable_strings(blob: bytes, minlen: int = 4) -> list[str]:
    out: list[str] = []
    run: list[str] = []
    for b in blob:
        if 32 <= b < 127:
            run.append(chr(b))
        else:
            if len(run) >= minlen:
                out.append("".join(run))
            run = []
    if len(run) >= minlen:
        out.append("".join(run))
    return out


class PedalSessionManager:
    """Single-flight, fail-closed pedal session state machine enforcing SAFETY.md."""

    IDENTITY_REQUEST = [0x7E, 0x7F, 0x06, 0x01]
    AUTOSAVE_OFF = [0x52, 0x00, 0x6E, 0x64, 0x20, 0x00, 0x64, 0x0F, 0x00, 0x00, 0x00, 0x00, 0x00]
    EDITOR_ON = [0x52, 0x00, 0x6E, 0x50]
    EDITOR_OFF = [0x52, 0x00, 0x6E, 0x51]
    CURRENT_PATCH_REQ = [0x52, 0x00, 0x6E, 0x64, 0x13]

    def __init__(
        self,
        *,
        stomphacks_dir: Path | str | None = None,
        session_dir: Path | str | None = None,
        backup_dir: Path | str | None = None,
    ) -> None:
        self._explicit_sh = Path(stomphacks_dir).resolve() if stomphacks_dir else None
        self.session_dir = Path(
            session_dir or os.environ.get("STOMPHACKS_SESSION_DIR") or (Path.cwd() / "session")
        ).expanduser().resolve()
        self.backup_dir = Path(
            backup_dir or os.environ.get("STOMPHACKS_BACKUP_DIR") or (Path.cwd() / "backup")
        ).expanduser().resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

        self._midi_lock = threading.Lock()
        self._busy_op: str | None = None

        self.connected = False
        self.autosave_off_confirmed = False
        self.identity_hex = ""
        self.identity_printable = ""
        self.current_patch_strings: list[str] = []
        self.backup_completed = False
        self.rescue_locked = False
        self.rescue_reason = ""

        self._ws_log_callbacks: list[Callable[[dict[str, Any]], None]] = []
        self._active_webmidi: WebMidiTransport | None = None
        self._issued_tokens: dict[str, dict[str, Any]] = {}

    def _sh_dir(self) -> Path:
        return find_stomphacks_dir(self._explicit_sh)

    def _ensure_paths(self, sh_dir: Path) -> None:
        for sub in ("zoom-zt2", "tools", "tools-pedal"):
            p = str(sh_dir / sub)
            if p not in sys.path:
                sys.path.insert(0, p)

    def register_ws_log(self, cb: Callable[[dict[str, Any]], None]) -> None:
        self._ws_log_callbacks.append(cb)

    def unregister_ws_log(self, cb: Callable[[dict[str, Any]], None]) -> None:
        if cb in self._ws_log_callbacks:
            self._ws_log_callbacks.remove(cb)

    def set_webmidi_transport(self, transport: WebMidiTransport | None) -> None:
        self._active_webmidi = transport

    def _emit_log(self, message: str, *, level: str = "info") -> None:
        payload = {"type": "log", "level": level, "message": message}
        for cb in list(self._ws_log_callbacks):
            try:
                cb(payload)
            except Exception:
                pass

    def issue_confirmation_token(self, action: str, target: str) -> str:
        token = secrets.token_urlsafe(16)
        self._issued_tokens[token] = {"action": action, "target": target, "ts": time.time()}
        return token

    def _consume_confirmation_token(self, token: str | None, action: str) -> None:
        if not token or not token.strip():
            raise SafetyGateError("Explicit confirmation_token is required for pedal mutations.")
        meta = self._issued_tokens.pop(token, None)
        if meta is not None and meta["action"] != action:
            raise SafetyGateError(f"Confirmation token was issued for '{meta['action']}', not '{action}'.")

    def status_dict(self) -> dict[str, Any]:
        return {
            "busy": self._busy_op is not None,
            "busy_op": self._busy_op,
            "connected": self.connected,
            "autosave_off_confirmed": self.autosave_off_confirmed,
            "identity_printable": self.identity_printable,
            "current_patch_strings": list(self.current_patch_strings),
            "backup_completed": self.backup_completed,
            "rescue_locked": self.rescue_locked,
            "rescue_reason": self.rescue_reason,
            "webmidi_connected": self._active_webmidi is not None,
        }

    def _acquire_mutex(self, op: str) -> None:
        if not self._midi_lock.acquire(blocking=False):
            raise PedalBusyError(f"Pedal MIDI mutex is held by '{self._busy_op}'.")
        self._busy_op = op

    def _release_mutex(self) -> None:
        self._busy_op = None
        if self._midi_lock.locked():
            self._midi_lock.release()

    def _open_transport(self, transport_override: Any | None) -> tuple[Any, bool]:
        """Return (transport, should_close)."""
        if transport_override is not None:
            return transport_override, False
        if self._active_webmidi is not None:
            return self._active_webmidi, False
        if os.environ.get("STOMPHACKS_DIRECT_MIDI") == "1" or sys.platform.startswith("linux"):
            return DirectMidiTransport(self._sh_dir()), True
        raise SafetyGateError(
            "No pedal MIDI transport connected. Connect via Web MIDI (/ws/pedal) in Chromium "
            "or run with direct Linux ALSA MIDI."
        )

    def _send_expect(self, transport: Any, payload: list[int], what: str, timeout: float = 3.0) -> list[int]:
        transport.send(payload)
        deadline = time.time() + timeout
        while True:
            remaining = max(0.0, deadline - time.time())
            msg = transport.recv(remaining)
            if msg is not None:
                return list(msg)
            if time.time() >= deadline:
                raise PedalTransferError(
                    f"FAIL-CLOSED: no reply to {what} within {timeout:.1f}s (SAFETY.md)."
                )

    def _verify_autosave_off(self, transport: Any, sh_dir: Path, timeout: float = 2.5) -> None:
        self._ensure_paths(sh_dir)
        pedal_common = _load_tool("sh_pedal_common", sh_dir / "tools-pedal" / "pedal_common.py")

        transport.send(self.EDITOR_ON)
        time.sleep(0.02)
        while transport.recv(0.0) is not None:
            pass

        transport.send(self.AUTOSAVE_OFF)
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = transport.recv(max(0.01, deadline - time.time()))
            if msg is not None:
                applied = pedal_common.parse_autosave_ack(bytes(msg))
                if applied is not None:
                    if applied == 0:
                        self.autosave_off_confirmed = True
                        return
                    self.autosave_off_confirmed = False
                    raise SafetyGateError(
                        f"REFUSED: Pedal reports AUTOSAVE={applied} (ON). Turn autosave OFF on pedal."
                    )
        self.autosave_off_confirmed = False
        raise SafetyGateError(
            "REFUSED: Could not confirm AUTOSAVE=OFF (no valid 64 20 01 ACK within timeout)."
        )

    def connect(self, *, transport: Any | None = None) -> dict[str, Any]:
        self._acquire_mutex("connect")
        sh_dir = self._sh_dir()
        opened, should_close = self._open_transport(transport)
        try:
            self._emit_log("safe_connect: requesting pedal identity...")
            opened.send(self.IDENTITY_REQUEST)
            ident_msg = opened.recv(1.5)
            if ident_msg is not None:
                ident = bytes(ident_msg)
                ascii_tail = "".join(chr(b) for b in ident if 32 <= b < 127)
                self.identity_hex = ident.hex()
                self.identity_printable = ascii_tail
            else:
                self.identity_hex = ""
                self.identity_printable = ""
            (self.session_dir / "identity.txt").write_text(
                f"identity_reply_hex: {self.identity_hex}\nidentity_printable: {self.identity_printable}\n",
                encoding="utf-8",
            )

            self._emit_log("safe_connect: forcing Autosave OFF and verifying ACK...")
            self._verify_autosave_off(opened, sh_dir)

            self._emit_log("safe_connect: inspecting current patch (read-only)...")
            opened.send(self.CURRENT_PATCH_REQ)
            patch_reply = opened.recv(2.0)
            if patch_reply is not None:
                patch_bytes = bytes(patch_reply)
                (self.session_dir / "CURRENT.ZPTC").write_bytes(patch_bytes)
                self.current_patch_strings = _printable_strings(patch_bytes)
            else:
                self.current_patch_strings = []

            opened.send(self.EDITOR_OFF)
            self.connected = True
            self._emit_log(
                f"safe_connect: OK ({self.identity_printable or 'MockPedal'}, Autosave OFF confirmed)"
            )
            return {
                "connected": True,
                "autosave_off_confirmed": self.autosave_off_confirmed,
                "identity_hex": self.identity_hex,
                "identity_printable": self.identity_printable,
                "current_patch_strings": self.current_patch_strings,
            }
        finally:
            if should_close and hasattr(opened, "close"):
                opened.close()
            self._release_mutex()

    def backup(self, *, transport: Any | None = None) -> dict[str, Any]:
        if self.rescue_locked:
            raise RescueLockError(self.rescue_reason)
        if not self.connected or not self.autosave_off_confirmed:
            raise SafetyGateError("Run safe_connect first (Autosave OFF must be confirmed).")

        self._acquire_mutex("backup")
        sh_dir = self._sh_dir()
        self._ensure_paths(sh_dir)
        fs = _load_tool("sh_file_session", sh_dir / "tools-pedal" / "file_session.py")
        flst_check = _load_tool("sh_flst_check", sh_dir / "tools" / "flst_check.py")

        opened, should_close = self._open_transport(transport)
        try:
            sess = fs.FileSession(opened, log=self._emit_log)
            sess.pcmode_on()
            try:
                flst_bytes = sess.download(fs.FLST_NAME)
                sess.close()
                files_dir = self.backup_dir / "files"
                files_dir.mkdir(parents=True, exist_ok=True)
                (self.backup_dir / fs.FLST_NAME).write_bytes(flst_bytes)

                ok, _probs, entries = flst_check.validate_flst(flst_bytes)
                backed_up: list[str] = [fs.FLST_NAME]
                if ok:
                    for entry in entries:
                        if isinstance(entry, (tuple, list)) and len(entry) >= 2:
                            fname = entry[1]
                        else:
                            fname = getattr(entry, "filename", None) or (
                                entry.get("filename") if isinstance(entry, dict) else None
                            )
                        if not fname:
                            continue
                        dst = files_dir / fname
                        if dst.is_file() and dst.stat().st_size > 0:
                            backed_up.append(fname)
                            continue
                        if sess.file_check(fname):
                            sess.close()
                            blob = sess.download(fname)
                            sess.close()
                            dst.write_bytes(blob)
                            backed_up.append(fname)
                        else:
                            sess.close()
                self.backup_completed = True
                self._emit_log(f"Backup complete: {len(backed_up)} file(s) saved.")
                return {
                    "backup_completed": True,
                    "backup_dir": str(self.backup_dir),
                    "files_count": len(backed_up),
                    "flst_valid": ok,
                }
            finally:
                try:
                    sess.pcmode_off()
                except Exception:
                    pass
        finally:
            if should_close and hasattr(opened, "close"):
                opened.close()
            self._release_mutex()

    def install(
        self,
        *,
        zd2_path: Path,
        report_path: Path | None,
        checklist: ChecklistAck,
        confirmation_token: str,
        free_margin_kb: int = 100,
        transport: Any | None = None,
    ) -> dict[str, Any]:
        if self.rescue_locked:
            raise RescueLockError(self.rescue_reason)
        if not self.connected or not self.autosave_off_confirmed:
            raise SafetyGateError("Run safe_connect first (Autosave OFF must be confirmed).")
        if not self.backup_completed:
            raise SafetyGateError("Backup Gate: complete a session backup before the first write.")

        checklist.validate()
        self._consume_confirmation_token(confirmation_token, "install")

        sh_dir = self._sh_dir()
        self._ensure_paths(sh_dir)
        fs = _load_tool("sh_file_session", sh_dir / "tools-pedal" / "file_session.py")
        pdiy = _load_tool("sh_pedal_diy", sh_dir / "tools-pedal" / "pedal_diy.py")
        flst_check = _load_tool("sh_flst_check", sh_dir / "tools" / "flst_check.py")
        import zoomzt2

        margin_bytes = int(free_margin_kb) * 1024
        if margin_bytes < fs.UPLOAD_MARGIN_FLOOR:
            raise SafetyGateError(
                f"free_margin_kb={free_margin_kb} is below the 32 KB safety floor."
            )

        sres = safety.check_zd2(zd2_path, stomphacks_dir=sh_dir, report_path=report_path)
        if not sres.passed:
            raise SafetyGateError("Pre-flight safety check failed: " + "; ".join(sres.errors))

        zic_path = zd2_path.with_suffix(".ZIC")
        if not zic_path.is_file():
            raise SafetyGateError(f"Companion ZIC icon not found: {zic_path.name}")

        ok_cls, eid, name, reason = pdiy.classify(str(zd2_path))
        if not (ok_cls is True or ok_cls == "diy"):
            raise SafetyGateError(f"DIY classification refused: {reason}")

        self._acquire_mutex("install")
        opened, should_close = self._open_transport(transport)
        try:
            self._verify_autosave_off(opened, sh_dir)
            sess = fs.FileSession(opened, log=self._emit_log)
            sess.pcmode_on()
            try:
                flst_old = sess.download(fs.FLST_NAME)
                sess.close()
                ok, probs, entries = flst_check.validate_flst(flst_old)
                if not ok:
                    raise SafetyGateError("On-pedal FLST_SEQ.ZT2 invalid: " + "; ".join(probs))

                base = zd2_path.name
                collisions = pdiy.check_resident_collisions(entries, base, eid)
                if collisions:
                    raise SafetyGateError("Resident effect collision: " + "; ".join(collisions))

                zt2_helper = zoomzt2.zoomzt2()
                canon = bytes(flst_old[: flst_check.FLST_SIZE])
                flst_new = flst_check.pad_to_native(
                    zt2_helper.add_effect_from_filename(canon, str(zd2_path)), len(flst_old)
                )
                good, probs = flst_check.expect_single_add(flst_old, flst_new, base)
                if not good:
                    raise SafetyGateError("Proposed FLST_SEQ.ZT2 failed validation: " + "; ".join(probs))

                files = [(zic_path.name, zic_path.read_bytes()), (base, zd2_path.read_bytes())]
                res = fs.install_effect(
                    sess, files, flst_old, flst_new, log=self._emit_log, margin=margin_bytes
                )
                self._emit_log(f"Install complete & readback-verified: {base}")
                return {
                    "installed": True,
                    "filename": base,
                    "effect_id": f"0x{int(eid):08x}",
                    "name": name,
                    "flst_retries": res.get("flst_retries", 0),
                }
            except fs.PedalUnresolved as exc:
                self.rescue_locked = True
                self.rescue_reason = f"DO NOT POWER CYCLE: {exc}"
                self._emit_log(self.rescue_reason, level="error")
                raise PedalTransferError(self.rescue_reason, rescue_required=True) from exc
            except fs.TransferAborted as exc:
                self._emit_log(f"Transfer aborted safely (pedal verified unchanged): {exc}", level="warning")
                raise PedalTransferError(str(exc), rescue_required=False) from exc
            except fs.TransferError as exc:
                try:
                    sess.abort()
                except Exception:
                    pass
                raise PedalTransferError(str(exc), rescue_required=False) from exc
            finally:
                try:
                    sess.pcmode_off()
                except Exception:
                    pass
        finally:
            if should_close and hasattr(opened, "close"):
                opened.close()
            self._release_mutex()

    def uninstall(
        self,
        *,
        zd2_path: Path,
        checklist: ChecklistAck,
        confirmation_token: str,
        transport: Any | None = None,
    ) -> dict[str, Any]:
        if self.rescue_locked:
            raise RescueLockError(self.rescue_reason)
        if not self.connected or not self.autosave_off_confirmed:
            raise SafetyGateError("Run safe_connect first (Autosave OFF must be confirmed).")
        if not self.backup_completed:
            raise SafetyGateError("Backup Gate: complete a session backup before the first write.")

        checklist.validate()
        self._consume_confirmation_token(confirmation_token, "uninstall")

        sh_dir = self._sh_dir()
        self._ensure_paths(sh_dir)
        fs = _load_tool("sh_file_session", sh_dir / "tools-pedal" / "file_session.py")
        pdiy = _load_tool("sh_pedal_diy", sh_dir / "tools-pedal" / "pedal_diy.py")
        flst_check = _load_tool("sh_flst_check", sh_dir / "tools" / "flst_check.py")
        import zoomzt2

        ok_cls, eid, name, reason = pdiy.classify(str(zd2_path))
        if not (ok_cls is True or ok_cls == "diy"):
            raise SafetyGateError(f"Refusing to uninstall non-DIY/stock effect: {reason}")

        self._acquire_mutex("uninstall")
        opened, should_close = self._open_transport(transport)
        try:
            self._verify_autosave_off(opened, sh_dir)
            sess = fs.FileSession(opened, log=self._emit_log)
            sess.pcmode_on()
            try:
                flst_old = sess.download(fs.FLST_NAME)
                sess.close()
                ok, probs, _entries = flst_check.validate_flst(flst_old)
                if not ok:
                    raise SafetyGateError("On-pedal FLST_SEQ.ZT2 invalid: " + "; ".join(probs))

                base = zd2_path.name
                zic_name = zd2_path.with_suffix(".ZIC").name
                zt2_helper = zoomzt2.zoomzt2()
                canon = bytes(flst_old[: flst_check.FLST_SIZE])
                flst_new = flst_check.pad_to_native(
                    zt2_helper.remove_effect(canon, base), len(flst_old)
                )
                good, probs = flst_check.expect_single_remove(flst_old, flst_new, base)
                if not good:
                    raise SafetyGateError("Proposed FLST_SEQ.ZT2 failed validation: " + "; ".join(probs))

                res = fs.uninstall_effect(
                    sess, [base, zic_name], flst_old, flst_new, log=self._emit_log
                )
                self._emit_log(f"Uninstall complete & verified: {base}")
                return {
                    "uninstalled": True,
                    "filename": base,
                    "flst_retries": res.get("flst_retries", 0),
                    "orphans": res.get("orphans", []),
                }
            except fs.PedalUnresolved as exc:
                self.rescue_locked = True
                self.rescue_reason = f"DO NOT POWER CYCLE: {exc}"
                self._emit_log(self.rescue_reason, level="error")
                raise PedalTransferError(self.rescue_reason, rescue_required=True) from exc
            except fs.TransferAborted as exc:
                raise PedalTransferError(str(exc), rescue_required=False) from exc
            finally:
                try:
                    sess.pcmode_off()
                except Exception:
                    pass
        finally:
            if should_close and hasattr(opened, "close"):
                opened.close()
            self._release_mutex()

    def rescue(
        self,
        *,
        write_back: bool = False,
        flst_backup_path: Path | None = None,
        checklist: ChecklistAck | None = None,
        confirmation_token: str | None = None,
        transport: Any | None = None,
    ) -> dict[str, Any]:
        if write_back:
            if checklist is None:
                raise SafetyGateError("Checklist confirmation required for rescue --write-back.")
            checklist.validate()
            self._consume_confirmation_token(confirmation_token, "rescue")
            if flst_backup_path is None or not flst_backup_path.is_file():
                raise SafetyGateError("A known-good FLST_SEQ.ZT2 backup file is required for write_back.")

        sh_dir = self._sh_dir()
        self._ensure_paths(sh_dir)
        fs = _load_tool("sh_file_session", sh_dir / "tools-pedal" / "file_session.py")
        flst_check = _load_tool("sh_flst_check", sh_dir / "tools" / "flst_check.py")

        self._acquire_mutex("rescue")
        opened, should_close = self._open_transport(transport)
        try:
            sess = fs.FileSession(opened, log=self._emit_log)
            # Close any wedged file session first without assuming PC mode
            try:
                sess.close()
            except Exception:
                pass
            sess.pcmode_on()
            try:
                flst_cur = sess.download(fs.FLST_NAME)
                sess.close()
                ok, probs, _entries = flst_check.validate_flst(flst_cur)
                if write_back and flst_backup_path is not None:
                    good_bytes = flst_backup_path.read_bytes()
                    g_ok, g_probs, _ = flst_check.validate_flst(good_bytes)
                    if not g_ok:
                        raise SafetyGateError("Backup FLST_SEQ.ZT2 is invalid: " + "; ".join(g_probs))
                    fs.replace_flst(sess, flst_cur, good_bytes, log=self._emit_log)
                    ok = True
                    probs = []
                if ok:
                    self.rescue_locked = False
                    self.rescue_reason = ""
                    self._emit_log("Rescue verified FLST_SEQ.ZT2 intact; lock cleared.")
                    return {"rescued": True, "flst_valid": True, "rescue_locked": False}
                raise PedalTransferError(
                    "FLST_SEQ.ZT2 on pedal failed validation after rescue: " + "; ".join(probs),
                    rescue_required=True,
                )
            finally:
                try:
                    sess.pcmode_off()
                except Exception:
                    pass
        finally:
            if should_close and hasattr(opened, "close"):
                opened.close()
            self._release_mutex()

    def allocate_diy_id(
        self,
        *,
        group: int = 0x07,
        from_device: bool = True,
        transport: Any | None = None,
    ) -> dict[str, Any]:
        """Find the next free 8-hex-digit DIY ID not used by stock_catalog or the device."""
        if not (0x01 <= group <= 0x1D):
            raise SafetyGateError(f"Invalid effect group 0x{group:02x}; must be 0x01..0x1d.")

        sh_dir = self._sh_dir()
        self._ensure_paths(sh_dir)
        pdiy = _load_tool("sh_pedal_diy", sh_dir / "tools-pedal" / "pedal_diy.py")
        flst_check = _load_tool("sh_flst_check", sh_dir / "tools" / "flst_check.py")
        fs = _load_tool("sh_file_session", sh_dir / "tools-pedal" / "file_session.py")

        stock_ids, _stock_names, _stock_files = pdiy.stock_index()
        used_ids: set[int] = set(stock_ids)
        source = "stock_catalog"
        resident_count = 0

        flst_bytes: bytes | None = None
        if from_device and (transport is not None or self._active_webmidi is not None):
            self._acquire_mutex("next_id")
            opened, should_close = self._open_transport(transport)
            try:
                sess = fs.FileSession(opened, log=self._emit_log)
                sess.pcmode_on()
                try:
                    flst_bytes = sess.download(fs.FLST_NAME)
                    sess.close()
                    source = "device"
                finally:
                    try:
                        sess.pcmode_off()
                    except Exception:
                        pass
            finally:
                if should_close and hasattr(opened, "close"):
                    opened.close()
                self._release_mutex()

        if flst_bytes is None:
            backup_flst = self.backup_dir / fs.FLST_NAME
            if backup_flst.is_file():
                flst_bytes = backup_flst.read_bytes()
                source = "backup"

        if flst_bytes is not None:
            ok, _probs, entries = flst_check.validate_flst(flst_bytes)
            if ok:
                resident_count = len(entries)
                for entry in entries:
                    if isinstance(entry, (tuple, list)) and len(entry) >= 4:
                        eid = entry[3]
                    else:
                        eid = getattr(entry, "id", None)
                        if eid is None and isinstance(entry, dict):
                            eid = entry.get("id")
                    if eid is not None:
                        used_ids.add(int(eid))

        base = (group << 24) | 0x1000
        for offset in range(0x1000):
            cand = base + offset
            if cand not in used_ids and pdiy.is_diy_id(cand):
                return {
                    "id": f"{cand:08x}",
                    "group": group,
                    "source": source,
                    "resident_count": resident_count,
                }
        raise SafetyGateError(f"No free DIY IDs remaining in playground range for group 0x{group:02x}.")
