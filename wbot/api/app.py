"""FastAPI application: REST + WebSocket on top of :class:`wbot.machine.Machine`.

Contract: docs/ARCHITECTURE.md ("HTTP/WebSocket-API"). The UI in ``ui/`` is served at ``/``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from ..machine import Machine, MachineError

log = logging.getLogger(__name__)
mimetypes.add_type("application/javascript", ".js")   # Windows registry may say text/plain
mimetypes.add_type("text/css", ".css")
UI_DIR = Path(__file__).resolve().parent.parent.parent / "ui"


class _Body(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ConnectBody(_Body):
    transport: str | None = None
    port: str | int | None = None          # COM port (serial) or TCP port (wifi)
    baudrate: int | None = None
    sim: dict | None = None
    host: str | None = None                # wifi: board host / IP
    protocol: str | None = None            # wifi: "telnet" | "websocket"


class XYBody(_Body):
    x: float
    y: float
    feed: float | None = None
    pen: str | None = None


class JogBody(_Body):
    dx: float = 0.0
    dy: float = 0.0
    feed: float | None = None
    pen: str | None = None


class PenBody(_Body):
    state: str | None = None
    angle: float | None = None


class RevolverBody(_Body):
    slot: int | None = None
    angle: float | None = None


class ServoBody(_Body):
    servo: str
    angle: float


class GcodeBody(_Body):
    line: str


class PlanBody(_Body):
    pattern: dict | None = None
    drawing: dict | None = None
    layout: dict | None = None
    dry_run: bool = False
    speed_scale: float = 1.0
    name: str | None = None


class Hub:
    """Fan-out of machine events to WebSocket clients (thread-safe)."""

    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[asyncio.Queue] = set()

    def publish(self, msg: dict) -> None:
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._put, msg)
        except RuntimeError:
            pass

    def _put(self, msg: dict) -> None:
        for q in list(self.clients):
            if q.full():
                # slow client: drop non-state messages first, never block the machine
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass


def create_app(machine: Machine | None = None, *, serve_ui: bool = True) -> FastAPI:
    hub = Hub()
    m = machine or Machine(listener=hub.publish)
    if machine is not None:
        m.listener = hub.publish
    app = FastAPI(title="W-BOT", version="2.0")
    app.state.machine, app.state.hub = m, hub

    @app.on_event("startup")
    async def _startup() -> None:  # noqa: D401
        hub.loop = asyncio.get_running_loop()

    from ..camera.service import CameraService
    from ..camera_api import register_camera_routes
    cam = CameraService(m, persist=m.persist)
    m.camera = cam
    app.state.camera = cam

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        cam.shutdown()
        m.shutdown()

    @app.exception_handler(MachineError)
    async def _machine_error(_: Request, exc: MachineError) -> JSONResponse:
        return JSONResponse({"error": exc.message, "code": exc.code}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ())[1:])
        return JSONResponse({"error": f"Ungueltige Eingabe ({where}): {first.get('msg', '')}",
                             "code": "bad_request"}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error")
        return JSONResponse({"error": f"Interner Fehler: {exc}", "code": "internal"}, status_code=500)

    async def run(fn, *a, **kw):
        """Machine calls block on hardware: keep the event loop free."""
        return await asyncio.to_thread(fn, *a, **kw)

    # --- state / connection ----------------------------------------------------
    @app.get("/api/state")
    async def state():
        return m.state()

    @app.get("/api/ports")
    async def ports():
        return await run(m.list_ports)

    @app.post("/api/connect")
    async def connect(b: ConnectBody):
        return await run(m.connect, b.transport, b.port, b.baudrate, b.sim, b.host, b.protocol)

    @app.post("/api/disconnect")
    async def disconnect():
        return await run(m.disconnect)

    @app.post("/api/reference")
    async def reference(b: XYBody):
        return await run(m.set_reference, b.x, b.y)

    # --- motion ------------------------------------------------------------------
    @app.post("/api/jog")
    async def jog(b: JogBody):
        return await run(m.jog, b.dx, b.dy, b.feed, b.pen)

    @app.post("/api/goto")
    async def goto(b: XYBody):
        return await run(m.goto, b.x, b.y, b.feed, b.pen)

    @app.post("/api/park")
    async def park():
        return await run(m.park)

    @app.post("/api/pen")
    async def pen(b: PenBody):
        return await run(m.set_pen, b.state, b.angle)

    @app.post("/api/revolver")
    async def revolver(b: RevolverBody):
        return await run(m.set_revolver, b.slot, b.angle)

    @app.post("/api/servo")
    async def servo(b: ServoBody):
        return await run(m.set_servo_raw, b.servo, b.angle)

    for name in ("hold", "resume", "cancel", "estop", "unlock", "reset"):
        def _make(n: str):
            async def handler():
                return await run(getattr(m, n))
            return handler
        app.post(f"/api/{name}")(_make(name))

    @app.post("/api/gcode")
    async def gcode(b: GcodeBody):
        return {"replies": await run(m.send_raw, b.line)}

    # --- drawings / jobs ---------------------------------------------------------
    @app.get("/api/patterns")
    async def patterns():
        return m.patterns()

    @app.post("/api/preview")
    async def preview(b: PlanBody):
        return await run(m.preview, b.model_dump())

    @app.post("/api/jobs")
    async def jobs(b: PlanBody):
        return await run(m.start_job, b.model_dump())

    @app.get("/api/job")
    async def job():
        return m.job_info()

    @app.post("/api/svg")
    async def svg(file: UploadFile = File(...)):
        data = await file.read()
        return await run(m.import_svg, data, file.filename or "svg")

    # --- config ------------------------------------------------------------------
    @app.get("/api/config")
    async def get_config():
        return m.config_json()

    @app.put("/api/config")
    async def put_config(patch: dict[str, Any]):
        return await run(m.update_config, patch)

    @app.get("/api/firmware-config", response_class=PlainTextResponse)
    async def firmware_config():
        return m.firmware_config_text()

    @app.get("/api/calibration/targets")
    async def calib_targets(n_x: int = 5, n_y: int = 4, inset_mm: float = 20.0):
        from ..calibration_api import calibration_targets
        return await run(calibration_targets, m, n_x, n_y, inset_mm)

    @app.post("/api/calibration/fit")
    async def calib_fit(body: dict[str, Any]):
        from ..calibration_api import calibration_fit
        return await run(calibration_fit, m, body)

    @app.post("/api/calibration/apply")
    async def calib_apply(body: dict[str, Any]):
        from ..calibration_api import calibration_apply
        return await run(calibration_apply, m, body)

    @app.get("/api/simulator")
    async def simulator():
        return m.simulator_info()

    # --- websocket ---------------------------------------------------------------
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        hub.clients.add(q)
        try:
            await sock.send_text(json.dumps({"type": "state", "data": m.state()}))
            if m.job_info():
                await sock.send_text(json.dumps({"type": "job", "data": m.job_info()}))
            last_state = 0.0
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    msg = None
                now = time.monotonic()
                if msg is not None:
                    await sock.send_text(json.dumps(msg, default=_json_default))
                # periodic state push (<=10 Hz) so positions stay live
                if now - last_state >= 0.1:
                    last_state = now
                    await sock.send_text(json.dumps({"type": "state", "data": m.state()},
                                                    default=_json_default))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            hub.clients.discard(q)

    register_camera_routes(app, m, cam)

    from ..mcp_mount import attach_mcp       # AI control at /mcp (lazy: the mcp package loads on first use)
    attach_mcp(app, m)

    if serve_ui and UI_DIR.exists():
        @app.middleware("http")
        async def _hide_dev(request: Request, call_next):
            if request.url.path.startswith("/dev/"):       # mock backend sources: never served
                return JSONResponse({"error": "Nicht gefunden", "code": "not_found"}, status_code=404)
            return await call_next(request)

        app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")
    return app


def _json_default(o: Any) -> Any:
    try:
        import numpy as np
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, np.generic):
            return o.item()
    except Exception:  # noqa: BLE001
        pass
    return str(o)
