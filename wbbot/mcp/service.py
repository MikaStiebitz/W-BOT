"""The plotter as a tool: every MCP capability, without the MCP plumbing.

Keeping this separate from :mod:`wbbot.mcp.server` means every tool is
exercisable from a test with nothing but the simulator — no MCP client, no
event loop games, no network.  ``server.py`` is a thin adapter over this class.

House rules every tool follows:

* **validate against the safe area first**, and say *why* in plain language
  when something does not fit;
* **return a duration estimate**, so a caller knows whether it just asked for
  four seconds or forty minutes of drawing;
* **support ``preview_only``**, so an AI can check its work before any ink
  goes on the board.
"""

from __future__ import annotations

import asyncio
import base64
import math
import time
from dataclasses import dataclass
from typing import Any, Literal, Sequence

import numpy as np

from ..calib.patterns import build_pattern
from ..calib.solve import CalibrationMeasurement, solve_calibration
from ..calib.wizard import CalibrationWizard
from ..config import MachineConfig
from ..core.geometry import Polyline
from ..core.hershey import default_font
from ..core.safearea import SafeArea, SafeAreaViolation
from ..core.toolpath import Stroke, Toolpath
from ..driver.machine import Machine, MachineState
from . import plotting as P
from .expressions import ExpressionError
from .plotting import PlotError, PlotWindow

__all__ = ["PlotterService", "ToolResult"]


def ok(**payload) -> dict:
    return {"ok": True, **payload}


def fail(message: str, **payload) -> dict:
    return {"ok": False, "error": message, **payload}


ToolResult = dict


class PlotterService:
    """Every board capability, as plain async methods returning dicts."""

    def __init__(self, machine: Machine, *,
                 default_wait: bool = True,
                 default_timeout_s: float = 300.0,
                 ams=None):
        self.machine = machine
        self.cfg: MachineConfig = machine.cfg
        self.safe_area: SafeArea = machine.safe_area
        self.default_wait = default_wait
        self.default_timeout_s = default_timeout_s
        self.wizard = CalibrationWizard(self.cfg, self.safe_area)
        if ams is None:
            try:
                from ..ams import Ams          # noqa: PLC0415
                ams = Ams(self.cfg)
            except Exception:                  # pragma: no cover - defensive
                ams = None
        self.ams = ams
        self._camera = None
        self._board_map = None

    def _slot_hex(self, index: int) -> str:
        if self.ams is not None and 0 <= index < len(self.ams.slots):
            return self.ams.slots[index].hex
        return self.cfg.color(index).hex

    def _slot_name(self, index: int) -> str:
        if self.ams is not None and 0 <= index < len(self.ams.slots):
            return self.ams.slots[index].name
        return self.cfg.color(index).name

    # ------------------------------------------------------------------
    # shared submission path
    # ------------------------------------------------------------------

    async def _submit(self, polylines: Sequence[Polyline], color: int, *,
                      preview_only: bool = False,
                      title: str = "mcp",
                      wait: bool | None = None,
                      timeout_s: float | None = None,
                      extra: dict | None = None) -> ToolResult:
        """Validate, estimate and (unless previewing) draw."""
        extra = extra or {}
        polylines = [p for p in polylines if not p.is_empty]
        if not polylines:
            return fail("nothing to draw: the request produced no geometry",
                        **extra)
        if not 0 <= color < self.cfg.n_colors:
            return fail(
                f"colour {color} does not exist; this machine has "
                f"{self.cfg.n_colors} markers (0..{self.cfg.n_colors - 1})")

        toolpath = Toolpath([Stroke(color, p) for p in polylines])
        violations = toolpath.validate(self.safe_area, raise_on_violation=False)
        if violations:
            bounds = toolpath.bounds
            return fail(
                f"the geometry leaves the drawable area at "
                f"{len(violations)} point(s): {violations[0]}",
                violations=[str(v) for v in violations[:5]],
                geometry_bounds=[round(v, 1) for v in bounds] if bounds else None,
                safe_area_bounds=[round(v, 1) for v in self.safe_area.bounds],
                **extra)

        stats = toolpath.stats(self.cfg)
        payload = {
            "stats": stats.to_dict(),
            "estimated_duration_s": round(stats.total_duration_s, 1),
            "estimated_duration_human": stats.to_dict()["total_duration_human"],
            "n_paths": len(polylines),
            "color": color,
            "color_name": self.cfg.color(color).name,
            "bounds": [round(v, 1) for v in (toolpath.bounds or (0, 0, 0, 0))],
            **extra,
        }

        if preview_only:
            payload["preview_only"] = True
            payload["preview_svg"] = self._preview_svg(toolpath)
            return ok(**payload)

        result = await self._run(toolpath, title=title, wait=wait,
                                 timeout_s=timeout_s)
        payload.update(result)
        return ok(**payload) if result.get("started") else fail(
            result.get("error", "the job could not be started"), **payload)

    async def _run(self, toolpath: Toolpath, *, title: str,
                   wait: bool | None, timeout_s: float | None) -> dict:
        wait = self.default_wait if wait is None else wait
        timeout_s = self.default_timeout_s if timeout_s is None else timeout_s
        if self.machine.state is MachineState.DISCONNECTED:
            return {"started": False, "error": "the plotter is not connected"}
        if self.machine.state in (MachineState.ALARM, MachineState.ESTOP):
            return {"started": False,
                    "error": f"the plotter is in {self.machine.state.value} state "
                             f"({self.machine.last_error or 'unknown cause'}); "
                             f"call clear_alarm first"}
        if self.machine.is_busy or self.machine.state is MachineState.PAUSED:
            return {"started": False,
                    "error": f"the plotter is busy ({self.machine.state.value}); "
                             f"wait for it or call cancel"}
        try:
            await self.machine.run_toolpath(toolpath, title=title)
        except SafeAreaViolation as exc:
            return {"started": False, "error": str(exc)}
        except Exception as exc:
            return {"started": False, "error": f"could not start: {exc}"}

        if not wait:
            return {"started": True, "completed": False,
                    "note": "drawing has started; poll get_status for progress"}

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            await asyncio.sleep(0.05)
            if self.machine.state is MachineState.IDLE:
                return {"started": True, "completed": True}
            if self.machine.state in (MachineState.ALARM, MachineState.ESTOP):
                return {"started": True, "completed": False,
                        "error": self.machine.last_error or "the machine stopped"}
        return {"started": True, "completed": False,
                "note": f"still drawing after {timeout_s:.0f} s; "
                        f"poll get_status for progress"}

    def _preview_svg(self, toolpath: Toolpath) -> str:
        from ..core.geometry import Drawing, Layer
        layers = [Layer(c, [s.polyline for s in toolpath.strokes
                            if s.color_index == c])
                  for c in toolpath.colors_used]
        drawing = Drawing(layers)
        return drawing.to_svg(self.cfg.board.width_mm, self.cfg.board.height_mm,
                              colors=[c.hex for c in self.cfg.colors],
                              stroke_width_mm=self.cfg.gondola.marker_line_width_mm)

    def _default_box(self, box: Sequence[float] | None) -> tuple[float, float, float, float]:
        """A sensible plot rectangle when the caller does not give one."""
        if box is not None:
            if len(box) != 4:
                raise PlotError("box must be [x, y, width, height] in mm")
            return tuple(float(v) for v in box)          # type: ignore[return-value]
        x0, y0, x1, y1 = self.safe_area.polygon.bounds
        margin = 40.0
        return (x0 + margin, y0 + margin,
                (x1 - x0) - 2 * margin, (y1 - y0) - 2 * margin)

    # ==================================================================
    # state and control
    # ==================================================================

    async def get_status(self) -> ToolResult:
        snap = self.machine.snapshot()
        return ok(**snap,
                  pen_state="down" if snap["pen_down"] else "up",
                  queue_length=snap["pending_lines"])

    async def get_board_info(self) -> ToolResult:
        summary = self.safe_area.summary()
        return ok(
            board_width_mm=self.cfg.board.width_mm,
            board_height_mm=self.cfg.board.height_mm,
            origin="lower-left corner of the drawing area; x right, y up",
            safe_area=summary,
            safe_area_polygon=self.safe_area.polygon_coords(),
            colors=[{"index": i, "slot": c.slot,
                     "name": self._slot_name(i), "hex": self._slot_hex(i),
                     "enabled": (self.ams.slots[i].enabled
                                 if self.ams is not None
                                 and i < len(self.ams.slots) else True)}
                    for i, c in enumerate(self.cfg.colors)],
            marker_line_width_mm=self.cfg.gondola.marker_line_width_mm,
            travel_feed_mm_min=self.cfg.motion.travel_feed_mm_min,
            draw_feed_mm_min=self.cfg.motion.draw_feed_mm_min,
            kinematics_model=self.cfg.geometry.kinematics_model,
            notes=[
                "All coordinates are millimetres on the board.",
                "Anything outside the safe area is refused, not clipped.",
                f"A stroke is {self.cfg.gondola.marker_line_width_mm} mm wide; "
                f"detail finer than that will not be visible.",
            ])

    async def home(self) -> ToolResult:
        try:
            await self.machine.home()
        except Exception as exc:
            return fail(f"homing failed: {exc}")
        return ok(position=self.machine.snapshot()["position"])

    async def pause(self) -> ToolResult:
        try:
            await self.machine.pause()
        except Exception as exc:
            return fail(str(exc))
        return ok(state=self.machine.state.value)

    async def resume(self) -> ToolResult:
        try:
            await self.machine.resume()
        except Exception as exc:
            return fail(str(exc))
        return ok(state=self.machine.state.value)

    async def cancel(self) -> ToolResult:
        try:
            await self.machine.cancel()
        except Exception as exc:
            return fail(str(exc))
        return ok(state=self.machine.state.value,
                  resume_from_stroke=self.machine.snapshot()["resume_from_stroke"])

    async def emergency_stop(self) -> ToolResult:
        await self.machine.emergency_stop()
        return ok(state=self.machine.state.value,
                  note="motion stopped; position may be lost. Call clear_alarm, "
                       "then re-home before drawing again.")

    async def clear_alarm(self) -> ToolResult:
        try:
            await self.machine.clear_alarm()
        except Exception as exc:
            return fail(str(exc))
        return ok(state=self.machine.state.value)

    # ==================================================================
    # elementary drawing
    # ==================================================================

    async def move_to(self, x: float, y: float) -> ToolResult:
        try:
            await self.machine.move_to(x, y)
        except SafeAreaViolation as exc:
            return fail(str(exc))
        except Exception as exc:
            return fail(f"move failed: {exc}")
        return ok(position={"x": x, "y": y})

    async def pen_up(self) -> ToolResult:
        try:
            await self.machine.set_pen(False)
        except Exception as exc:
            return fail(str(exc))
        return ok(pen_state="up")

    async def pen_down(self) -> ToolResult:
        try:
            await self.machine.set_pen(True)
        except SafeAreaViolation as exc:
            return fail(str(exc))
        except Exception as exc:
            return fail(str(exc))
        return ok(pen_state="down")

    async def set_color(self, index: int) -> ToolResult:
        if not 0 <= index < self.cfg.n_colors:
            return fail(f"colour {index} does not exist; valid range is "
                        f"0..{self.cfg.n_colors - 1}")
        try:
            await self.machine.set_color(index)
        except Exception as exc:
            return fail(str(exc))
        return ok(color=index, color_name=self.cfg.color(index).name)

    async def draw_polyline(self, points: Sequence[Sequence[float]],
                            color: int = 0, closed: bool = False,
                            **kw) -> ToolResult:
        try:
            line = P.polygon(points, closed=closed)
        except PlotError as exc:
            return fail(str(exc))
        return await self._submit([line], color, title="polyline", **kw)

    async def draw_line(self, x1: float, y1: float, x2: float, y2: float,
                        color: int = 0, **kw) -> ToolResult:
        if math.dist((x1, y1), (x2, y2)) < 1e-6:
            return fail("start and end are the same point")
        return await self._submit(
            [Polyline(np.array([[x1, y1], [x2, y2]]), closed=False)],
            color, title="line", **kw)

    # ==================================================================
    # geometric drawing
    # ==================================================================

    async def draw_circle(self, cx: float, cy: float, radius: float,
                          color: int = 0, **kw) -> ToolResult:
        try:
            shape = P.circle(cx, cy, radius)
        except PlotError as exc:
            return fail(str(exc))
        return await self._submit([shape], color, title="circle", **kw)

    async def draw_arc(self, cx: float, cy: float, radius: float,
                       start_deg: float, end_deg: float,
                       color: int = 0, **kw) -> ToolResult:
        try:
            shape = P.arc(cx, cy, radius, start_deg, end_deg)
        except PlotError as exc:
            return fail(str(exc))
        return await self._submit([shape], color, title="arc", **kw)

    async def draw_rect(self, x: float, y: float, width: float, height: float,
                        color: int = 0, corner_radius: float = 0.0,
                        **kw) -> ToolResult:
        try:
            shape = P.rectangle(x, y, width, height, corner_radius=corner_radius)
        except PlotError as exc:
            return fail(str(exc))
        return await self._submit([shape], color, title="rectangle", **kw)

    async def draw_polygon(self, points: Sequence[Sequence[float]],
                           color: int = 0, **kw) -> ToolResult:
        try:
            shape = P.polygon(points, closed=True)
        except PlotError as exc:
            return fail(str(exc))
        return await self._submit([shape], color, title="polygon", **kw)

    async def draw_bezier(self, control_points: Sequence[Sequence[float]],
                          color: int = 0, **kw) -> ToolResult:
        try:
            shape = P.bezier(control_points)
        except PlotError as exc:
            return fail(str(exc))
        return await self._submit([shape], color, title="bezier", **kw)

    # ==================================================================
    # text and mathematics
    # ==================================================================

    async def write_text(self, text: str, x: float, y: float,
                         height_mm: float = 30.0, color: int = 0,
                         align: str = "left", angle_deg: float = 0.0,
                         **kw) -> ToolResult:
        if not text.strip():
            return fail("the text is empty")
        font = default_font()
        missing = font.unsupported(text)
        try:
            paths = P.text(text, x, y, height_mm, align=align,  # type: ignore[arg-type]
                           angle_deg=angle_deg)
        except PlotError as exc:
            return fail(str(exc))
        width, height = font.measure(text, height_mm)
        extra: dict[str, Any] = {"text_width_mm": round(width, 1),
                                 "text_height_mm": round(height, 1)}
        if missing:
            extra["unsupported_characters"] = missing
            extra["note"] = (f"these characters have no glyph and were drawn as "
                             f"'?': {' '.join(missing)}")
        return await self._submit(paths, color, title="text", extra=extra, **kw)

    async def write_equation(self, latex: str, x: float, y: float,
                             height_mm: float = 40.0, color: int = 0,
                             mode: str = "outline", align: str = "left",
                             **kw) -> ToolResult:
        if mode not in ("outline", "stroke", "skeleton"):
            return fail(f"unknown mode {mode!r}; use outline, stroke or skeleton")
        try:
            paths, info = P.write_equation(latex, x, y, height_mm,
                                           mode=mode,          # type: ignore[arg-type]
                                           align=align)        # type: ignore[arg-type]
        except PlotError as exc:
            return fail(str(exc))
        return await self._submit(paths, color, title="equation",
                                  extra={"equation": info}, **kw)

    async def plot_function(self, expression: str,
                            x_min: float = -10.0, x_max: float = 10.0,
                            y_min: float | None = None, y_max: float | None = None,
                            box: Sequence[float] | None = None,
                            color: int = 0, axes: bool = True,
                            axes_color: int | None = None,
                            samples: int = 1200,
                            equal_aspect: bool = False,
                            **kw) -> ToolResult:
        try:
            plot_box = self._default_box(box)
            paths, window, info = P.plot_function(
                expression, x_min=x_min, x_max=x_max, y_min=y_min, y_max=y_max,
                box=plot_box, samples=samples, equal_aspect=equal_aspect)
        except (PlotError, ExpressionError) as exc:
            return fail(str(exc))
        if not paths:
            return fail(
                f"'{expression}' produced nothing drawable on "
                f"[{x_min}, {x_max}]", plot=info)

        all_paths = list(paths)
        if axes:
            axis_paths, axis_info = P.draw_axes(
                window, x_range=(info["x_range"][0], info["x_range"][1]),
                y_range=(info["y_range"][0], info["y_range"][1]))
            info["axes"] = axis_info
            all_paths = axis_paths + all_paths
        return await self._submit(all_paths, color, title=f"plot {expression}",
                                  extra={"plot": info}, **kw)

    async def plot_parametric(self, x_expression: str, y_expression: str,
                              t_min: float = 0.0, t_max: float = 6.283185,
                              box: Sequence[float] | None = None,
                              color: int = 0, samples: int = 1500,
                              **kw) -> ToolResult:
        try:
            paths, _, info = P.plot_parametric(
                x_expression, y_expression, t_min=t_min, t_max=t_max,
                box=self._default_box(box), samples=samples)
        except (PlotError, ExpressionError) as exc:
            return fail(str(exc))
        return await self._submit(paths, color, title="parametric",
                                  extra={"plot": info}, **kw)

    async def plot_polar(self, r_expression: str,
                         theta_min: float = 0.0,
                         theta_max: float = 6.283185,
                         box: Sequence[float] | None = None,
                         color: int = 0, samples: int = 1500,
                         **kw) -> ToolResult:
        try:
            paths, _, info = P.plot_polar(
                r_expression, theta_min=theta_min, theta_max=theta_max,
                box=self._default_box(box), samples=samples)
        except (PlotError, ExpressionError) as exc:
            return fail(str(exc))
        return await self._submit(paths, color, title="polar",
                                  extra={"plot": info}, **kw)

    async def draw_axes(self, x_range: Sequence[float] = (-10, 10),
                        y_range: Sequence[float] = (-10, 10),
                        box: Sequence[float] | None = None,
                        tick_spacing: Sequence[float] | None = None,
                        labels: bool = True, color: int = 0,
                        axis_labels: Sequence[str] = ("x", "y"),
                        **kw) -> ToolResult:
        try:
            plot_box = self._default_box(box)
            window = PlotWindow.fit((x_range[0], x_range[1]),
                                    (y_range[0], y_range[1]), plot_box)
            paths, info = P.draw_axes(
                window, x_range=(x_range[0], x_range[1]),
                y_range=(y_range[0], y_range[1]),
                tick_spacing=(tuple(tick_spacing) if tick_spacing else None),
                labels=labels, axis_labels=(axis_labels[0], axis_labels[1]))
        except PlotError as exc:
            return fail(str(exc))
        return await self._submit(paths, color, title="axes",
                                  extra={"axes": info}, **kw)

    async def plot_data(self, points: Sequence[Sequence[float]],
                        style: str = "line",
                        box: Sequence[float] | None = None,
                        x_range: Sequence[float] | None = None,
                        y_range: Sequence[float] | None = None,
                        axes: bool = True, color: int = 0,
                        **kw) -> ToolResult:
        pts = np.asarray(points, dtype=float).reshape(-1, 2)
        if len(pts) == 0:
            return fail("no data points were given")
        if not np.all(np.isfinite(pts)):
            return fail("the data contains non-finite values")
        try:
            plot_box = self._default_box(box)
            xr = (tuple(x_range) if x_range else
                  P._pad_range((float(pts[:, 0].min()), float(pts[:, 0].max()))))
            yr = (tuple(y_range) if y_range else
                  P._pad_range((float(pts[:, 1].min()), float(pts[:, 1].max()))))
            window = PlotWindow.fit(xr, yr, plot_box)
            paths = P.plot_data(pts, window, style=style)  # type: ignore[arg-type]
            info: dict[str, Any] = {"style": style, "n_points": len(pts),
                                    "x_range": list(xr), "y_range": list(yr)}
            if axes:
                axis_paths, axis_info = P.draw_axes(window, x_range=xr, y_range=yr)
                info["axes"] = axis_info
                paths = axis_paths + paths
        except PlotError as exc:
            return fail(str(exc))
        return await self._submit(paths, color, title="data plot",
                                  extra={"plot": info}, **kw)

    # ==================================================================
    # images
    # ==================================================================

    async def draw_image(self, path_or_data: str,
                         mode: str = "contours",
                         colors: int = 1,
                         fit: str = "contain",
                         margin_mm: float = 20.0,
                         color_mode: str | None = None,
                         **kw) -> ToolResult:
        try:
            from ..imaging.pipeline import PipelineParams, run_pipeline
        except ImportError as exc:
            return fail(f"the image pipeline needs OpenCV: {exc}")

        source: Any = path_or_data
        if isinstance(path_or_data, str) and path_or_data.startswith("data:"):
            try:
                source = base64.b64decode(path_or_data.split(",", 1)[1])
            except Exception as exc:
                return fail(f"could not decode the inline image data: {exc}")
        elif isinstance(path_or_data, str) and path_or_data.startswith(("http://",
                                                                        "https://")):
            return fail(
                "remote URLs are not fetched by the plotter. Download the "
                "image and pass a local path or inline data: URI instead.")

        if color_mode is None:
            color_mode = "single" if colors <= 1 else "palette"
        params = PipelineParams(trace_mode=mode,          # type: ignore[arg-type]
                                color_mode=color_mode,    # type: ignore[arg-type]
                                fit_mode=fit,             # type: ignore[arg-type]
                                fit_margin_mm=margin_mm)
        if self.ams is not None:
            enabled = self.ams.enabled_slots()
            params.colorsep.marker_colors = [s.rgb for s in enabled]
            params.colorsep.slot_indices = [s.index for s in enabled]
            params.colorsep.chroma_weight = 1.4
        try:
            result = run_pipeline(source, self.cfg, params,
                                  safe_area=self.safe_area)
        except Exception as exc:
            return fail(f"the image could not be processed: {exc}")

        if result.toolpath.is_empty:
            return fail("the image produced no drawable paths",
                        warnings=result.warnings, pipeline=result.to_dict())

        preview_only = kw.pop("preview_only", False)
        payload = {"pipeline": result.to_dict(),
                   "stats": result.stats.to_dict(),
                   "estimated_duration_s": round(result.stats.total_duration_s, 1),
                   "estimated_duration_human":
                       result.stats.to_dict()["total_duration_human"],
                   "warnings": result.warnings}
        if preview_only:
            payload["preview_only"] = True
            payload["preview_svg"] = result.preview_svg(self.cfg)
            return ok(**payload)
        run = await self._run(result.toolpath, title="image", **kw)
        payload.update(run)
        return ok(**payload) if run.get("started") else fail(
            run.get("error", "could not start"), **payload)

    async def preview_image(self, path_or_data: str, mode: str = "contours",
                            colors: int = 1, fit: str = "contain",
                            margin_mm: float = 20.0,
                            color_mode: str | None = None) -> ToolResult:
        return await self.draw_image(path_or_data, mode=mode, colors=colors,
                                     fit=fit, margin_mm=margin_mm,
                                     color_mode=color_mode, preview_only=True)

    # ==================================================================
    # calibration
    # ==================================================================

    async def run_calibration(self, n_x: int = 3, n_y: int = 3,
                              preview_only: bool = False,
                              **kw) -> ToolResult:
        try:
            self.wizard.begin()
            self.wizard.skip_reference()
            pattern = self.wizard.build_pattern(n_x=n_x, n_y=n_y)
        except Exception as exc:
            return fail(f"could not build the calibration pattern: {exc}")
        result = await self._submit(
            [s.polyline for s in pattern.toolpath.strokes], 0,
            preview_only=preview_only, title="calibration pattern",
            extra={"pattern": pattern.to_dict(),
                   "instructions": pattern.instructions()},
            **kw)
        return result

    async def submit_calibration(self,
                                 measurements: Sequence[dict]) -> ToolResult:
        try:
            self.wizard.record_many(measurements)
            result = self.wizard.solve()
        except Exception as exc:
            return fail(str(exc))
        return ok(result=result.to_dict(), report=result.report(),
                  meets_target=result.max_error_mm < 2.0)

    # ==================================================================
    # perception
    # ==================================================================

    def _ensure_camera(self):
        if self._camera is not None:
            return self._camera
        from ..camera.capture import CameraError, make_camera
        try:
            camera = make_camera(self.cfg)
            camera.open()
        except CameraError as exc:
            raise RuntimeError(str(exc)) from exc
        self._camera = camera
        return camera

    def attach_camera(self, camera, board_map=None) -> None:
        """Inject a camera (used by tests and by the API layer)."""
        self._camera = camera
        if board_map is not None:
            self._board_map = board_map

    def _ensure_board_map(self):
        if self._board_map is not None:
            return self._board_map
        from ..camera.boardmap import BoardMap
        board_map = BoardMap.from_config(self.cfg)
        if board_map is None:
            raise RuntimeError(
                "the camera is not calibrated: no homography in machine.yaml. "
                "Run the camera calibration (four board corners) first.")
        self._board_map = board_map
        return board_map

    async def capture_board(self, include_image: bool = True,
                            px_per_mm: float = 1.0) -> ToolResult:
        try:
            camera = self._ensure_camera()
            board_map = self._ensure_board_map()
            from ..camera.readback import encode_png
            frame = await asyncio.to_thread(camera.capture)
            rectified = board_map.rectify(frame, px_per_mm=px_per_mm)
        except Exception as exc:
            return fail(str(exc))
        payload: dict[str, Any] = {
            "width_px": int(rectified.shape[1]),
            "height_px": int(rectified.shape[0]),
            "px_per_mm": px_per_mm,
            "board_mm": [self.cfg.board.width_mm, self.cfg.board.height_mm],
        }
        if include_image:
            payload["image_png_base64"] = encode_png(rectified)
        return ok(**payload)

    async def read_board(self, include_image: bool = False,
                         px_per_mm: float = 1.0) -> ToolResult:
        try:
            camera = self._ensure_camera()
            board_map = self._ensure_board_map()
            from ..camera.readback import read_board as do_read
            frame = await asyncio.to_thread(camera.capture)
            reading = do_read(frame, board_map, px_per_mm=px_per_mm)
        except Exception as exc:
            return fail(str(exc))
        return ok(**reading.to_dict(include_image=include_image))

    async def find_free_space(self, width_mm: float, height_mm: float,
                              margin_mm: float = 20.0,
                              max_results: int = 5,
                              px_per_mm: float = 1.0) -> ToolResult:
        if width_mm <= 0 or height_mm <= 0:
            return fail("width_mm and height_mm must be positive")
        try:
            camera = self._ensure_camera()
            board_map = self._ensure_board_map()
            from ..camera.readback import find_free_space as do_find
            from ..camera.readback import read_board as do_read
            frame = await asyncio.to_thread(camera.capture)
            reading = do_read(frame, board_map, px_per_mm=px_per_mm)
            spots = do_find(reading, width_mm, height_mm,
                            safe_area=self.safe_area, margin_mm=margin_mm,
                            max_results=max_results)
        except Exception as exc:
            return fail(str(exc))
        if not spots:
            return fail(
                f"no free {width_mm:.0f} x {height_mm:.0f} mm area was found. "
                f"The board is {reading.coverage_percent:.0f} % covered — "
                f"try a smaller size or erase something.",
                coverage_percent=round(reading.coverage_percent, 1),
                occupied_regions=[r.to_dict() for r in reading.regions[:5]])
        return ok(spaces=[s.to_dict() for s in spots],
                  best=spots[0].to_dict(),
                  coverage_percent=round(reading.coverage_percent, 1),
                  occupied_regions=[r.to_dict() for r in reading.regions[:8]])
