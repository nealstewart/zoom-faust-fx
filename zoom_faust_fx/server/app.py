"""FastAPI HTTP & WebSocket server for Stomphacks + zoom-faust-fx."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .build_manager import BuildManager
from .midi_relay import (
    ChecklistAck,
    PedalBusyError,
    PedalSessionManager,
    PedalTransferError,
    RescueLockError,
    SafetyGateError,
    WebMidiTransport,
)


class BuildRequest(BaseModel):
    dsp_code: str | None = None
    c_code: str | None = None
    manifest: dict[str, Any] | None = None
    name: str = "custom_fx"
    icon_png_base64: str | None = None
    icon_bitmap: list[list[int]] | None = None
    run_benchmark: bool = True
    no_strip: bool = False
    async_mode: bool = False


class ConfirmTokenRequest(BaseModel):
    action: str = Field(..., pattern="^(install|uninstall|rescue)$")
    target: str = ""


class ChecklistModel(BaseModel):
    autosave_off_confirmed: bool = False
    backup_confirmed: bool = False
    unsaved_patch_only_confirmed: bool = False
    no_power_cycle_confirmed: bool = False

    def to_ack(self) -> ChecklistAck:
        return ChecklistAck(
            autosave_off_confirmed=self.autosave_off_confirmed,
            backup_confirmed=self.backup_confirmed,
            unsaved_patch_only_confirmed=self.unsaved_patch_only_confirmed,
            no_power_cycle_confirmed=self.no_power_cycle_confirmed,
        )


class PedalInstallRequest(BaseModel):
    build_id: str
    confirmation_token: str
    checklist: ChecklistModel
    free_margin_kb: int = 100


class PedalUninstallRequest(BaseModel):
    build_id: str | None = None
    zd2_path: str | None = None
    confirmation_token: str
    checklist: ChecklistModel


class PedalRescueRequest(BaseModel):
    write_back: bool = False
    flst_backup_path: str | None = None
    confirmation_token: str | None = None
    checklist: ChecklistModel | None = None


def _raise_pedal_http(exc: Exception) -> None:
    if isinstance(exc, PedalBusyError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, RescueLockError):
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    if isinstance(exc, SafetyGateError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, PedalTransferError):
        status = 423 if exc.rescue_required else 502
        raise HTTPException(
            status_code=status,
            detail={"message": str(exc), "rescue_required": exc.rescue_required},
        ) from exc
    raise HTTPException(status_code=500, detail=str(exc)) from exc


def create_app(
    *,
    build_manager: BuildManager | None = None,
    pedal_manager: PedalSessionManager | None = None,
    web_dir: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="Stomphacks + zoom-faust-fx Server", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    bm = build_manager or BuildManager()
    pm = pedal_manager or PedalSessionManager()
    app.state.build_manager = bm
    app.state.pedal_manager = pm

    @app.get("/api/capabilities")
    async def api_capabilities() -> dict[str, Any]:
        caps = bm.check_capabilities()
        caps["pedal"] = pm.status_dict()
        return caps

    @app.get("/api/faust/zoom.lib")
    async def api_faust_zoom_lib() -> FileResponse:
        zoom_lib = Path(__file__).resolve().parent.parent.parent / "effects" / "zoom.lib"
        if not zoom_lib.is_file():
            raise HTTPException(status_code=404, detail="effects/zoom.lib not found")
        return FileResponse(zoom_lib, media_type="text/plain")

    @app.get("/api/faust/allowlist")
    async def api_faust_allowlist() -> dict[str, Any]:
        from ..postprocess.allowlist import (
            ALLOWED_IMPORTS,
            ALLOWED_MODULES,
            ALLOWED_MODULE_FUNCTIONS,
            ALTERNATIVE_SUGGESTIONS,
            FORBIDDEN_DIRECTIVES,
            UNSUPPORTED_UI_ELEMENTS,
        )

        return {
            "allowed_imports": sorted(ALLOWED_IMPORTS),
            "allowed_modules": sorted(ALLOWED_MODULES),
            "allowed_module_functions": {
                k: sorted(v) if v is not None else None
                for k, v in ALLOWED_MODULE_FUNCTIONS.items()
            },
            "alternative_suggestions": dict(ALTERNATIVE_SUGGESTIONS),
            "forbidden_directives": list(FORBIDDEN_DIRECTIVES),
            "unsupported_ui_elements": list(UNSUPPORTED_UI_ELEMENTS),
        }

    @app.post("/api/build")
    async def api_build(req: BuildRequest) -> dict[str, Any]:
        try:
            job = bm.create_job(
                dsp_code=req.dsp_code,
                c_code=req.c_code,
                manifest=req.manifest,
                name=req.name,
                icon_png_base64=req.icon_png_base64,
                icon_bitmap=req.icon_bitmap,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if req.async_mode:
            asyncio.create_task(
                asyncio.to_thread(
                    bm.run_job,
                    job.build_id,
                    run_benchmark=req.run_benchmark,
                    no_strip=req.no_strip,
                )
            )
            return job.to_dict()

        completed = await asyncio.to_thread(
            bm.run_job,
            job.build_id,
            run_benchmark=req.run_benchmark,
            no_strip=req.no_strip,
        )
        return completed.to_dict()

    @app.get("/api/build/{build_id}")
    async def api_build_status(build_id: str) -> dict[str, Any]:
        job = bm.get_job(build_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown build_id: {build_id}")
        return job.to_dict()

    @app.get("/api/build/{build_id}/events")
    async def api_build_events(build_id: str) -> StreamingResponse:
        job = bm.get_job(build_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown build_id: {build_id}")

        loop = asyncio.get_running_loop()
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        def on_event(ev: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(q.put_nowait, ev)

        bm.subscribe(build_id, on_event)

        async def event_stream():
            try:
                for existing in list(job.stages):
                    yield f"data: {json.dumps(existing)}\n\n"
                if job.status in ("completed", "failed"):
                    yield f"data: {json.dumps({'type': 'done', 'build_id': build_id, 'job': job.to_dict()})}\n\n"
                    return
                while True:
                    ev = await q.get()
                    yield f"data: {json.dumps(ev)}\n\n"
                    if ev.get("type") == "done":
                        break
            finally:
                bm.unsubscribe(build_id, on_event)

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    @app.get("/api/build/{build_id}/artifacts")
    async def api_build_artifacts(build_id: str) -> dict[str, Any]:
        job = bm.get_job(build_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown build_id: {build_id}")
        data = job.to_dict()
        return {
            "build_id": build_id,
            "installable": job.installable,
            "report_passed": job.report_passed,
            "artifacts": [
                {
                    "filename": name,
                    "url": f"/api/build/{build_id}/artifacts/{name}",
                }
                for name in data["artifacts"]
            ],
        }

    @app.get("/api/build/{build_id}/artifacts/{filename}")
    async def api_build_artifact_download(build_id: str, filename: str) -> FileResponse:
        path = bm.get_artifact_path(build_id, filename)
        if path is None:
            raise HTTPException(status_code=404, detail=f"Artifact not found: {filename}")
        return FileResponse(path, filename=path.name)

    @app.get("/api/pedal/status")
    async def api_pedal_status() -> dict[str, Any]:
        return pm.status_dict()

    @app.get("/api/pedal/next-id")
    async def api_pedal_next_id(group: int = 0x07, from_device: bool = True) -> dict[str, Any]:
        try:
            return await asyncio.to_thread(pm.allocate_diy_id, group=group, from_device=from_device)
        except Exception as exc:
            _raise_pedal_http(exc)

    @app.post("/api/pedal/confirm-token")
    async def api_pedal_confirm_token(req: ConfirmTokenRequest) -> dict[str, str]:
        token = pm.issue_confirmation_token(req.action, req.target)
        return {"confirmation_token": token, "action": req.action}

    @app.post("/api/pedal/connect")
    async def api_pedal_connect() -> dict[str, Any]:
        try:
            return await asyncio.to_thread(pm.connect)
        except Exception as exc:
            _raise_pedal_http(exc)

    @app.post("/api/pedal/backup")
    async def api_pedal_backup() -> dict[str, Any]:
        try:
            return await asyncio.to_thread(pm.backup)
        except Exception as exc:
            _raise_pedal_http(exc)

    @app.post("/api/pedal/install")
    async def api_pedal_install(req: PedalInstallRequest) -> dict[str, Any]:
        job = bm.get_job(req.build_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Unknown build_id: {req.build_id}")
        if not job.installable or job.zd2_path is None:
            raise HTTPException(
                status_code=400,
                detail="Build failed REPORT.md or safety gate and is marked non-installable.",
            )
        try:
            return await asyncio.to_thread(
                pm.install,
                zd2_path=job.zd2_path,
                report_path=job.report_path,
                checklist=req.checklist.to_ack(),
                confirmation_token=req.confirmation_token,
                free_margin_kb=req.free_margin_kb,
            )
        except Exception as exc:
            _raise_pedal_http(exc)

    @app.post("/api/pedal/uninstall")
    async def api_pedal_uninstall(req: PedalUninstallRequest) -> dict[str, Any]:
        zd2: Path | None = None
        if req.build_id:
            job = bm.get_job(req.build_id)
            if job is None or job.zd2_path is None:
                raise HTTPException(status_code=404, detail=f"Build/ZD2 not found: {req.build_id}")
            zd2 = job.zd2_path
        elif req.zd2_path:
            zd2 = Path(req.zd2_path).expanduser().resolve()
        if zd2 is None or not zd2.is_file():
            raise HTTPException(status_code=400, detail="Valid build_id or zd2_path is required.")
        try:
            return await asyncio.to_thread(
                pm.uninstall,
                zd2_path=zd2,
                checklist=req.checklist.to_ack(),
                confirmation_token=req.confirmation_token,
            )
        except Exception as exc:
            _raise_pedal_http(exc)

    @app.post("/api/pedal/rescue")
    async def api_pedal_rescue(req: PedalRescueRequest) -> dict[str, Any]:
        flst_path = Path(req.flst_backup_path).expanduser().resolve() if req.flst_backup_path else None
        if flst_path is None and req.write_back:
            default_flst = pm.backup_dir / "FLST_SEQ.ZT2"
            if default_flst.is_file():
                flst_path = default_flst
        try:
            return await asyncio.to_thread(
                pm.rescue,
                write_back=req.write_back,
                flst_backup_path=flst_path,
                checklist=req.checklist.to_ack() if req.checklist else None,
                confirmation_token=req.confirmation_token,
            )
        except Exception as exc:
            _raise_pedal_http(exc)

    @app.websocket("/ws/pedal")
    async def ws_pedal(websocket: WebSocket) -> None:
        await websocket.accept()
        loop = asyncio.get_running_loop()
        send_lock = asyncio.Lock()

        async def send_ws_json(payload: dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_json(payload)

        def on_log(payload: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(asyncio.create_task, send_ws_json(payload))

        transport = WebMidiTransport(loop, send_ws_json)
        pm.register_ws_log(on_log)
        try:
            await send_ws_json({"type": "status", "pedal": pm.status_dict()})
            while True:
                msg = await websocket.receive_json()
                mtype = msg.get("type")
                if mtype == "webmidi_ready":
                    pm.set_webmidi_transport(transport)
                    await send_ws_json({"type": "status", "pedal": pm.status_dict()})
                elif mtype == "webmidi_closed":
                    pm.set_webmidi_transport(None)
                    await send_ws_json({"type": "status", "pedal": pm.status_dict()})
                elif mtype == "sysex_rx":
                    data = msg.get("data") or []
                    transport.push_rx([int(b) for b in data])
                elif mtype == "ping":
                    await send_ws_json({"type": "pong"})
        except WebSocketDisconnect:
            pass
        finally:
            pm.unregister_ws_log(on_log)
            if pm._active_webmidi is transport:
                pm.set_webmidi_transport(None)
            transport.close()

    resolved_web = web_dir or (Path(__file__).resolve().parent.parent.parent / "web")
    if os.environ.get("SERVE_STATIC", "1") != "0" and resolved_web.is_dir():
        app.mount("/", StaticFiles(directory=str(resolved_web), html=True), name="web")

    return app


app = create_app()


def main() -> int:
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("zoom_faust_fx.server.app:app", host=host, port=port, reload=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
