"""FastAPI application: REST for actions, WebSocket for everything live.

The API is a thin skin over :class:`~wbbot.driver.machine.Machine`,
:class:`~wbbot.live.session.LiveSession` and the image pipeline.  It holds no
machine logic of its own — the safe-area rules and state transitions live one
layer down, so the MCP server and the CLI get identical behaviour for free.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Literal, Sequence

from fastapi import (BackgroundTasks, Depends, FastAPI, File, HTTPException,
                     UploadFile, WebSocket, WebSocketDisconnect)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..ams import Ams, AmsError
from ..calib.wizard import CalibrationWizard
from ..config import MachineConfig, load_config, save_calibration
from ..core.gcode import GCodeWriter
from ..core.geometry import Polyline
from ..core.safearea import SafeArea, SafeAreaViolation
from ..core.toolpath import Stroke, Toolpath
from ..driver.machine import Machine, MachineEvent, MachineState
from ..driver.simulator import FluidNCSimulator
from ..driver.transport import SimulatorTransport, make_transport
from ..live.session import LiveConfig, LiveSession
from ..live.sketch import SketchSession
from ..mcp.service import PlotterService
from .ws import Hub

log = logging.getLogger(__name__)

__all__ = ["create_app", "AppState"]


# ==========================================================================
# request models
# ==========================================================================

class MoveRequest(BaseModel):
    x: float
    y: float
    draw: bool = False


class ColorRequest(BaseModel):
    index: int = Field(ge=0)


class PenRequest(BaseModel):
    down: bool


class CursorRequest(BaseModel):
    x: float
    y: float


class DrawingRequest(BaseModel):
    drawing: bool


class PipelineRequest(BaseModel):
    trace_mode: str = "contours"
    color_mode: str = "single"
    color_order: list[int] | None = None
    fit_mode: str = "contain"
    fit_margin_mm: float = 15.0
    optimize: bool = True
    max_paths: int = 20000
    canny_low: int | None = None
    canny_high: int | None = None
    threshold: int | None = None
    min_path_length_mm: float | None = None
    simplify_tolerance_mm: float | None = None
    stipple_points: int | None = None
    hatch_spacing_min_px: float | None = None
    hatch_spacing_max_px: float | None = None
    gamma: float = 1.0
    contrast: float = 1.0
    invert: bool = False
    marker_colors: list[list[int]] | None = None


class CalibrationReferenceRequest(BaseModel):
    x: float
    y: float
    left_mm: float
    right_mm: float


class CalibrationPatternRequest(BaseModel):
    n_x: int = 3
    n_y: int = 3
    inset_mm: float = 120.0


class MeasurementRequest(BaseModel):
    index: int
    x: float
    y: float


class ServoRequest(BaseModel):
    pen_up: float | None = None
    pen_down: float | None = None
    revolver: list[float] | None = None
    test: bool = False


class AmsSlotRequest(BaseModel):
    index: int = Field(ge=0)
    name: str | None = None
    hex: str | None = None
    enabled: bool | None = None


class CornerMoveRequest(BaseModel):
    x: float
    y: float


class CornerRecordRequest(BaseModel):
    index: int = Field(ge=0, le=3)


# ==========================================================================
# application state
# ==========================================================================

class AppState:
    """Everything the endpoints share."""

    def __init__(self, cfg: MachineConfig, machine: Machine | None = None):
        self.cfg = cfg
        self.simulator: FluidNCSimulator | None = None
        if machine is None:
            if cfg.connection.transport == "simulator":
                # WBBOT_SIM_TIMESCALE speeds the virtual machine up for
                # development (e.g. 50 = fifty times faster than real time).
                # It has no effect on a real transport.
                import os                                   # noqa: PLC0415
                try:
                    time_scale = float(os.environ.get("WBBOT_SIM_TIMESCALE", "1"))
                except ValueError:
                    time_scale = 1.0
                self.simulator = FluidNCSimulator(
                    cfg, time_scale=max(time_scale, 0.01))
                transport = SimulatorTransport(self.simulator)
            else:
                transport = make_transport(cfg)
            machine = Machine(cfg, transport=transport, simulator=self.simulator)
        else:
            self.simulator = machine.simulator
        self.machine = machine
        self.safe_area: SafeArea = machine.safe_area
        self.ams = Ams(cfg)
        self.hub = Hub()
        self.live: LiveSession | None = None
        self.sketch: SketchSession | None = None
        self.wizard = CalibrationWizard(cfg, self.safe_area)
        # Corner calibration jogs to the physical board corners, which sit
        # inside the margin the normal safe area excludes.
        self.corner_safe_area = SafeArea(cfg.replace(
            safe_area={"board_margin_mm": 0.0}))
        self.service = PlotterService(machine, default_wait=False, ams=self.ams)
        self.last_pipeline_result = None
        self.last_toolpath: Toolpath | None = None
        self.machine.add_listener(self._on_machine_event)

    def _on_machine_event(self, event: MachineEvent) -> None:
        self.hub.broadcast(event.kind, event.payload)

    def _on_live_event(self, event: dict) -> None:
        kind = event.pop("kind", "live")
        self.hub.broadcast(kind, event)

    def _on_sketch_event(self, event: dict) -> None:
        kind = event.pop("kind", "sketch")
        self.hub.broadcast(kind, event)

    async def shutdown(self) -> None:
        if self.live is not None:
            with contextlib.suppress(Exception):
                await self.live.stop()
        if self.sketch is not None:
            with contextlib.suppress(Exception):
                await self.sketch.stop()
        with contextlib.suppress(Exception):
            await self.machine.disconnect()


# ==========================================================================
# app factory
# ==========================================================================

def create_app(cfg: MachineConfig | None = None,
               machine: Machine | None = None,
               *, serve_ui: bool = True) -> FastAPI:
    cfg = cfg or load_config()
    state = AppState(cfg, machine)

    app = FastAPI(title="wbbot", version="0.11.0",
                  description="Control software for a V-shape whiteboard plotter")
    app.state.wbbot = state

    app.add_middleware(
        CORSMiddleware,
        # The UI is served from the same process in production; the permissive
        # rule exists so `vite dev` on :5173 can talk to :8000.
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def machine_or_503() -> Machine:
        if state.machine.state is MachineState.DISCONNECTED:
            raise HTTPException(503, "the plotter is not connected")
        return state.machine

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @app.on_event("startup")
    async def _startup() -> None:
        try:
            await state.machine.connect()
        except Exception as exc:
            log.error("could not connect at startup: %s", exc)

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await state.shutdown()

    # ------------------------------------------------------------------
    # machine info and state
    # ------------------------------------------------------------------

    @app.get("/api/config")
    async def get_config() -> dict:
        return {
            "board": {"width_mm": cfg.board.width_mm,
                      "height_mm": cfg.board.height_mm},
            "geometry": {
                "anchor_left": list(cfg.geometry.anchor_left),
                "anchor_right": list(cfg.geometry.anchor_right),
                "pulley_radius_mm": cfg.geometry.pulley_pitch_radius_mm,
                "kinematics_model": cfg.geometry.kinematics_model,
                "precompensate": cfg.geometry.precompensate,
            },
            "colors": [{"index": i, "slot": c.slot, "name": c.name, "hex": c.hex}
                       for i, c in enumerate(cfg.colors)],
            "motion": {
                "travel_feed_mm_min": cfg.motion.travel_feed_mm_min,
                "draw_feed_mm_min": cfg.motion.draw_feed_mm_min,
                "park_position": list(cfg.motion.park_position),
            },
            "marker_line_width_mm": cfg.gondola.marker_line_width_mm,
            "transport": state.machine.transport.description,
        }

    @app.get("/api/status")
    async def get_status() -> dict:
        return state.machine.snapshot()

    @app.get("/api/safe-area")
    async def get_safe_area() -> dict:
        return {"polygon": state.safe_area.polygon_coords(),
                "bounds": list(state.safe_area.bounds),
                "summary": state.safe_area.summary()}

    @app.get("/api/heatmap")
    async def get_heatmap(step_mm: float = 25.0) -> dict:
        if not 2.0 <= step_mm <= 200.0:
            raise HTTPException(400, "step_mm must be between 2 and 200")
        return state.safe_area.heatmap(step_mm).to_json()

    # ------------------------------------------------------------------
    # connection and control
    # ------------------------------------------------------------------

    @app.post("/api/connect")
    async def connect() -> dict:
        try:
            await state.machine.connect()
        except Exception as exc:
            raise HTTPException(503, f"could not connect: {exc}") from exc
        return state.machine.snapshot()

    @app.post("/api/disconnect")
    async def disconnect() -> dict:
        await state.machine.disconnect()
        return state.machine.snapshot()

    @app.post("/api/estop")
    async def estop() -> dict:
        await state.machine.emergency_stop()
        return state.machine.snapshot()

    @app.post("/api/clear-alarm")
    async def clear_alarm(m: Machine = Depends(machine_or_503)) -> dict:
        try:
            await m.clear_alarm()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        return m.snapshot()

    @app.post("/api/home")
    async def home(m: Machine = Depends(machine_or_503)) -> dict:
        try:
            await m.home()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        return m.snapshot()

    @app.post("/api/pause")
    async def pause(m: Machine = Depends(machine_or_503)) -> dict:
        try:
            await m.pause()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        return m.snapshot()

    @app.post("/api/resume")
    async def resume(m: Machine = Depends(machine_or_503)) -> dict:
        try:
            await m.resume()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        return m.snapshot()

    @app.post("/api/cancel")
    async def cancel(m: Machine = Depends(machine_or_503)) -> dict:
        try:
            await m.cancel()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        return m.snapshot()

    @app.post("/api/resume-job")
    async def resume_job(m: Machine = Depends(machine_or_503)) -> dict:
        try:
            await m.resume_cancelled_job()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        return m.snapshot()

    # ------------------------------------------------------------------
    # manual motion
    # ------------------------------------------------------------------

    @app.post("/api/move")
    async def move(request: MoveRequest,
                   m: Machine = Depends(machine_or_503)) -> dict:
        try:
            await m.move_to(request.x, request.y, draw=request.draw)
        except SafeAreaViolation as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        return m.snapshot()

    @app.post("/api/pen")
    async def pen(request: PenRequest,
                  m: Machine = Depends(machine_or_503)) -> dict:
        try:
            await m.set_pen(request.down)
        except SafeAreaViolation as exc:
            raise HTTPException(400, str(exc)) from exc
        return m.snapshot()

    @app.post("/api/color")
    async def color(request: ColorRequest,
                    m: Machine = Depends(machine_or_503)) -> dict:
        if request.index >= cfg.n_colors:
            raise HTTPException(400, f"colour {request.index} does not exist")
        await m.set_color(request.index)
        return m.snapshot()

    # ------------------------------------------------------------------
    # marker magazine (AMS)
    # ------------------------------------------------------------------

    @app.get("/api/ams")
    async def ams_get() -> dict:
        return {**state.ams.to_dict(), "active": state.machine.active_color}

    @app.put("/api/ams/slot")
    async def ams_set_slot(request: AmsSlotRequest) -> dict:
        try:
            state.ams.set_slot(request.index, name=request.name,
                               hex=request.hex, enabled=request.enabled)
        except AmsError as exc:
            raise HTTPException(400, str(exc)) from exc
        payload = {**state.ams.to_dict(), "active": state.machine.active_color}
        state.hub.broadcast("ams", payload)
        return payload

    @app.post("/api/ams/select")
    async def ams_select(request: ColorRequest,
                         m: Machine = Depends(machine_or_503)) -> dict:
        try:
            state.ams.require_enabled(request.index)
        except AmsError as exc:
            raise HTTPException(400, str(exc)) from exc
        await m.set_color(request.index)
        payload = {**state.ams.to_dict(), "active": m.active_color}
        state.hub.broadcast("ams", payload)
        return payload

    # ------------------------------------------------------------------
    # image pipeline
    # ------------------------------------------------------------------

    def _build_params(request: PipelineRequest):
        from ..imaging.pipeline import PipelineParams
        params = PipelineParams(
            trace_mode=request.trace_mode,        # type: ignore[arg-type]
            color_mode=request.color_mode,        # type: ignore[arg-type]
            color_order=request.color_order,
            fit_mode=request.fit_mode,            # type: ignore[arg-type]
            fit_margin_mm=request.fit_margin_mm,
            optimize=request.optimize,
            max_paths=request.max_paths,
            min_path_length_mm=request.min_path_length_mm,
            simplify_tolerance_mm=request.simplify_tolerance_mm,
        )
        params.preprocess.gamma = request.gamma
        params.preprocess.contrast = request.contrast
        params.preprocess.invert = request.invert
        if request.canny_low is not None:
            params.contours.canny_low = request.canny_low
        if request.canny_high is not None:
            params.contours.canny_high = request.canny_high
        if request.threshold is not None:
            params.bitmap.threshold = request.threshold
            params.centerline.threshold = request.threshold
        if request.stipple_points is not None:
            params.stipple.n_points = request.stipple_points
        if request.hatch_spacing_min_px is not None:
            params.hatch.min_spacing_px = request.hatch_spacing_min_px
        if request.hatch_spacing_max_px is not None:
            params.hatch.max_spacing_px = request.hatch_spacing_max_px
        if request.marker_colors:
            params.colorsep.marker_colors = [tuple(c) for c in request.marker_colors]
            params.colorsep.slot_indices = list(range(len(request.marker_colors)))
        else:
            # The AMS is the truth about what is loaded: separate onto the
            # enabled slots only, tagged with their real revolver indices.
            enabled = state.ams.enabled_slots()
            params.colorsep.marker_colors = [s.rgb for s in enabled]
            params.colorsep.slot_indices = [s.index for s in enabled]
            params.colorsep.chroma_weight = 1.4
        return params

    @app.post("/api/image/preview")
    async def image_preview(file: UploadFile = File(...),
                            trace_mode: str = "contours",
                            color_mode: str = "single") -> dict:
        try:
            from ..imaging.pipeline import run_pipeline
        except ImportError as exc:
            raise HTTPException(501, f"the image pipeline needs OpenCV: {exc}")
        data = await file.read()
        if not data:
            raise HTTPException(400, "the uploaded file is empty")
        request = PipelineRequest(trace_mode=trace_mode, color_mode=color_mode)
        params = _build_params(request)
        try:
            result = await asyncio.to_thread(run_pipeline, data, cfg, params,
                                             safe_area=state.safe_area)
        except Exception as exc:
            raise HTTPException(400, f"the image could not be processed: {exc}")
        state.last_pipeline_result = result
        state.last_toolpath = result.toolpath
        return {**result.to_dict(), "preview_svg": result.preview_svg(cfg, colors=state.ams.hex_list()),
                "paths": _toolpath_json(result.toolpath)}

    @app.post("/api/image/process")
    async def image_process(request: PipelineRequest,
                            file: UploadFile = File(...)) -> dict:
        try:
            from ..imaging.pipeline import run_pipeline
        except ImportError as exc:
            raise HTTPException(501, f"the image pipeline needs OpenCV: {exc}")
        data = await file.read()
        params = _build_params(request)
        try:
            result = await asyncio.to_thread(run_pipeline, data, cfg, params,
                                             safe_area=state.safe_area)
        except Exception as exc:
            raise HTTPException(400, f"the image could not be processed: {exc}")
        state.last_pipeline_result = result
        state.last_toolpath = result.toolpath
        return {**result.to_dict(), "preview_svg": result.preview_svg(cfg, colors=state.ams.hex_list()),
                "paths": _toolpath_json(result.toolpath)}

    @app.post("/api/draw")
    async def draw(m: Machine = Depends(machine_or_503)) -> dict:
        if state.last_toolpath is None or state.last_toolpath.is_empty:
            raise HTTPException(400, "there is no prepared drawing to send")
        try:
            await m.run_toolpath(state.last_toolpath, title="image")
        except SafeAreaViolation as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        return m.snapshot()

    @app.get("/api/gcode")
    async def get_gcode() -> Response:
        if state.last_toolpath is None or state.last_toolpath.is_empty:
            raise HTTPException(400, "there is no prepared drawing")
        program = GCodeWriter(cfg, state.safe_area).render_toolpath(
            state.last_toolpath, title="wbbot job")
        return Response(program.text, media_type="text/plain",
                        headers={"Content-Disposition":
                                 'attachment; filename="wbbot.nc"'})

    # ------------------------------------------------------------------
    # live drawing
    # ------------------------------------------------------------------

    @app.post("/api/live/start")
    async def live_start(m: Machine = Depends(machine_or_503)) -> dict:
        if state.sketch is not None and state.sketch.active:
            raise HTTPException(409, "a sketch session is running — stop it first")
        if state.live is not None:
            await state.live.stop()
        session = LiveSession(m, cfg, LiveConfig(), state.safe_area)
        session.add_listener(state._on_live_event)
        try:
            await session.start()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        state.live = session
        return session.snapshot()

    @app.post("/api/live/stop")
    async def live_stop() -> dict:
        if state.live is None:
            raise HTTPException(400, "no live session is running")
        await state.live.stop()
        snapshot = state.live.snapshot()
        state.live = None
        return snapshot

    @app.get("/api/live/status")
    async def live_status() -> dict:
        if state.live is None:
            return {"state": "stopped", "undo_supported": False}
        return state.live.snapshot()

    @app.get("/api/live/strokes")
    async def live_strokes() -> dict:
        if state.live is None:
            return {"strokes": []}
        return {"strokes": state.live.strokes_to_json()}

    # ------------------------------------------------------------------
    # sketch queue (asynchronous live drawing)
    # ------------------------------------------------------------------

    @app.post("/api/sketch/start")
    async def sketch_start(m: Machine = Depends(machine_or_503)) -> dict:
        if state.live is not None:
            raise HTTPException(409, "the cursor live session is running — "
                                     "stop it first")
        if state.sketch is not None:
            await state.sketch.stop()
        session = SketchSession(m, cfg, state.safe_area)
        session.add_listener(state._on_sketch_event)
        try:
            await session.start()
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        state.sketch = session
        return session.snapshot()

    @app.post("/api/sketch/stop")
    async def sketch_stop() -> dict:
        if state.sketch is None:
            raise HTTPException(400, "no sketch session is running")
        await state.sketch.stop()
        snapshot = state.sketch.snapshot()
        state.sketch = None
        return snapshot

    @app.get("/api/sketch/status")
    async def sketch_status(include_points: bool = False) -> dict:
        if state.sketch is None:
            return {"active": False, "state": "stopped", "queued": 0,
                    "done": 0, "failed": 0, "current_id": None, "total": 0,
                    "strokes": [], "last_error": None, "undo_supported": False}
        return state.sketch.snapshot(include_points=include_points)

    @app.post("/api/sketch/clear")
    async def sketch_clear() -> dict:
        if state.sketch is None:
            raise HTTPException(400, "no sketch session is running")
        removed = state.sketch.clear()
        return {"removed": removed, **state.sketch.snapshot()}

    # ------------------------------------------------------------------
    # corner calibration
    # ------------------------------------------------------------------

    @app.post("/api/calibration/corners/begin")
    async def corners_begin() -> dict:
        return state.wizard.begin_corners()

    @app.post("/api/calibration/corners/move")
    async def corners_move(request: CornerMoveRequest,
                           m: Machine = Depends(machine_or_503)) -> dict:
        reason = state.corner_safe_area.reason((request.x, request.y))
        if reason is not None:
            raise HTTPException(400, f"({request.x:.0f}, {request.y:.0f}) mm "
                                     f"is not reachable: {reason}")
        px, py = m.writer.map_point((request.x, request.y))
        await m.driver.send_line(
            f"G1 X{px:.3f} Y{py:.3f} F{cfg.motion.travel_feed_mm_min:.0f}")
        return {"target": [request.x, request.y]}

    @app.post("/api/calibration/corners/record")
    async def corners_record(request: CornerRecordRequest,
                             m: Machine = Depends(machine_or_503)) -> dict:
        # The status report lags the commanded position while moving; wait for
        # the controller to settle so the recorded position is the real one.
        try:
            await m.driver.wait_until_idle(timeout=20.0)
        except Exception as exc:
            raise HTTPException(409, f"the machine did not settle: {exc}") from exc
        x, y = m.driver.status.xy
        try:
            return state.wizard.record_corner(request.index, x, y)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/calibration/corners/solve")
    async def corners_solve() -> dict:
        try:
            result = state.wizard.solve_corners()
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"result": result.to_dict(), "report": result.report(),
                "meets_target": result.max_error_mm < 2.0}

    # ------------------------------------------------------------------
    # calibration
    # ------------------------------------------------------------------

    @app.post("/api/calibration/begin")
    async def calib_begin() -> dict:
        return state.wizard.begin()

    @app.post("/api/calibration/reference")
    async def calib_reference(request: CalibrationReferenceRequest) -> dict:
        try:
            return state.wizard.set_reference((request.x, request.y),
                                              request.left_mm, request.right_mm)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/calibration/skip-reference")
    async def calib_skip_reference() -> dict:
        return state.wizard.skip_reference()

    @app.post("/api/calibration/pattern")
    async def calib_pattern(request: CalibrationPatternRequest) -> dict:
        try:
            pattern = state.wizard.build_pattern(n_x=request.n_x, n_y=request.n_y,
                                                 inset_mm=request.inset_mm)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
        state.last_toolpath = pattern.toolpath
        return {**state.wizard.state(), "paths": _toolpath_json(pattern.toolpath)}

    @app.post("/api/calibration/draw")
    async def calib_draw(m: Machine = Depends(machine_or_503)) -> dict:
        if state.wizard.pattern is None:
            raise HTTPException(400, "build the calibration pattern first")
        try:
            await m.run_toolpath(state.wizard.toolpath, title="calibration")
        except Exception as exc:
            raise HTTPException(409, str(exc)) from exc
        return m.snapshot()

    @app.post("/api/calibration/measure")
    async def calib_measure(measurements: list[MeasurementRequest]) -> dict:
        try:
            return state.wizard.record_many(
                [{"index": m.index, "x": m.x, "y": m.y} for m in measurements])
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.post("/api/calibration/solve")
    async def calib_solve(model: str = "auto") -> dict:
        try:
            result = state.wizard.solve(model=model)  # type: ignore[arg-type]
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"result": result.to_dict(), "report": result.report(),
                "meets_target": result.max_error_mm < 2.0}

    @app.post("/api/calibration/save")
    async def calib_save() -> dict:
        try:
            path = state.wizard.save()
        except RuntimeError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"saved_to": str(path),
                "note": "restart the server to load the new calibration"}

    @app.post("/api/calibration/servo")
    async def calib_servo(request: ServoRequest,
                          m: Machine = Depends(machine_or_503)) -> dict:
        tuner = state.wizard.servo
        if request.pen_up is not None:
            tuner.set_pen_up(request.pen_up)
        if request.pen_down is not None:
            tuner.set_pen_down(request.pen_down)
        if request.revolver is not None:
            for i, value in enumerate(request.revolver):
                if i < len(tuner.revolver):
                    tuner.set_revolver(i, value)
        if request.test:
            axis = cfg.servo.pen.axis
            feed = cfg.servo.feed_units_min
            for value in (tuner.pen_down, tuner.pen_up):
                await m.driver.send_line(f"G1 {axis}{value:.2f} F{feed:.0f}")
                await m.driver.send_line("G4 P0.5")
        return tuner.to_dict()

    # ------------------------------------------------------------------
    # fluidnc config
    # ------------------------------------------------------------------

    @app.get("/api/fluidnc-config")
    async def fluidnc_config(schema: str = "3.7") -> Response:
        from ..driver.fluidnc_config import SCHEMAS, generate_fluidnc_config
        if schema not in SCHEMAS:
            raise HTTPException(400, f"unknown schema {schema!r}; available: "
                                     f"{', '.join(sorted(SCHEMAS))}")
        return Response(generate_fluidnc_config(cfg, schema),
                        media_type="text/yaml",
                        headers={"Content-Disposition":
                                 'attachment; filename="config.yaml"'})

    # ------------------------------------------------------------------
    # websocket
    # ------------------------------------------------------------------

    @app.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.accept()
        client = await state.hub.add(websocket)
        pump = asyncio.create_task(state.hub.pump(client))
        try:
            await websocket.send_json({
                "kind": "hello",
                "payload": {"status": state.machine.snapshot(),
                            "safe_area": state.safe_area.polygon_coords(),
                            "board": [cfg.board.width_mm, cfg.board.height_mm],
                            "colors": [c.hex for c in cfg.colors]},
                "t": time.time()})
            while True:
                message = await websocket.receive_json()
                await _handle_ws_message(state, websocket, message)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            log.debug("websocket closed: %s", exc)
        finally:
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump
            await state.hub.remove(client)

    # ------------------------------------------------------------------
    # static UI
    # ------------------------------------------------------------------

    if serve_ui:
        ui_dir = Path(__file__).resolve().parents[2] / "web" / "dist"
        if ui_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=ui_dir / "assets"),
                      name="assets")

            @app.get("/")
            async def index() -> FileResponse:
                return FileResponse(ui_dir / "index.html")
        else:
            @app.get("/")
            async def index_placeholder() -> HTMLResponse:
                return HTMLResponse(
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>wbbot</title>"
                    "<body style='font-family:system-ui;padding:3rem;"
                    "max-width:40rem;margin:auto'>"
                    "<h1>wbbot</h1>"
                    "<p>The API is running, but the web UI has not been built "
                    "yet.</p><pre>cd web &amp;&amp; npm install &amp;&amp; "
                    "npm run build</pre>"
                    "<p>API docs: <a href='/docs'>/docs</a></p></body>")

    return app


# ==========================================================================
# helpers
# ==========================================================================

def _toolpath_json(toolpath: Toolpath) -> list[dict]:
    """Paths in a shape the canvas can draw directly."""
    return [{"color": s.color_index,
             "closed": bool(s.polyline.closed),
             "points": s.polyline.to_list(2)}
            for s in toolpath.strokes]


async def _handle_ws_message(state: AppState, websocket: WebSocket,
                             message: dict) -> None:
    """Handle a client frame. Live cursor updates arrive here at ~60 Hz."""
    kind = message.get("kind")
    payload = message.get("payload") or {}

    if kind == "cursor":
        if state.live is not None:
            state.live.set_cursor(float(payload.get("x", 0.0)),
                                  float(payload.get("y", 0.0)))
        return
    if kind == "drawing":
        if state.live is not None:
            await state.live.set_drawing(bool(payload.get("drawing", False)))
        return
    if kind == "color":
        if state.live is not None:
            try:
                await state.live.set_color(int(payload.get("index", 0)))
            except ValueError as exc:
                await websocket.send_json({"kind": "error",
                                           "payload": {"message": str(exc)}})
        return
    if kind == "stroke":
        # A finished sketch stroke: points in board mm plus a marker slot.
        if state.sketch is None or not state.sketch.active:
            await websocket.send_json({
                "kind": "sketch_ack",
                "payload": {"ok": False,
                            "error": "no sketch session is running"}})
            return
        color = int(payload.get("color", state.machine.active_color))
        try:
            state.ams.require_enabled(color)
        except AmsError as exc:
            await websocket.send_json({"kind": "sketch_ack",
                                       "payload": {"ok": False,
                                                   "error": str(exc)}})
            return
        result = await state.sketch.enqueue(payload.get("points") or [], color)
        await websocket.send_json({"kind": "sketch_ack", "payload": result})
        return
    if kind == "sketch_clear":
        if state.sketch is not None:
            state.sketch.clear()
        return
    if kind == "sketch_snapshot":
        if state.sketch is not None:
            await websocket.send_json({
                "kind": "sketch_state",
                "payload": state.sketch.snapshot()})
        return
    if kind == "estop":
        # Reachable even mid-stream, which is the whole point of a space-bar
        # e-stop: it must not queue behind anything.
        if state.sketch is not None:
            state.sketch.clear()
        if state.live is not None:
            await state.live.emergency_stop()
        else:
            await state.machine.emergency_stop()
        return
    if kind == "ping":
        await websocket.send_json({"kind": "pong",
                                   "payload": {"t": payload.get("t")}})
        return
    if kind == "live_snapshot":
        if state.live is not None:
            await websocket.send_json({"kind": "live_status",
                                       "payload": state.live.snapshot()})
        return
    await websocket.send_json({"kind": "error",
                               "payload": {"message": f"unknown frame {kind!r}"}})
