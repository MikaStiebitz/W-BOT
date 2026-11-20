"""Asynchronous sketch queue: draw ahead as fast as you like.

The original live mode ties the pen to the cursor, so you can only ever draw
as fast as the plotter moves.  The sketch queue decouples the two ends:

* strokes drawn in the browser are captured at full mouse speed and **queued**,
  each with its own marker colour;
* a worker drains the queue and draws stroke after stroke — travelling with
  the pen up, switching the revolver between strokes when the colour changes —
  while new strokes keep arriving on top.

You can sketch a whole picture ahead of the machine; the plotter catches up on
its own time.  The UI shows three truths: what is queued (faint), what is
being drawn right now (highlighted), and what is already ink (solid).

Undo still does not exist — a queued stroke can be removed, ink cannot.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Sequence

import numpy as np

from ..config import MachineConfig
from ..core.geometry import Polyline
from ..core.safearea import SafeArea
from ..driver.fluidnc import ConnectionLost, FluidNCError, GrblAlarm
from ..driver.machine import Machine, MachineState

log = logging.getLogger(__name__)

__all__ = ["SketchState", "SketchStroke", "SketchSession"]


class SketchState(str, Enum):
    STOPPED = "stopped"
    READY = "ready"          # waiting for strokes
    DRAWING = "drawing"      # working through the queue
    ERROR = "error"


@dataclass
class SketchStroke:
    """One stroke in the queue."""

    id: int
    color: int
    points: np.ndarray
    status: str = "queued"       # queued | drawing | done | failed
    error: str | None = None

    @property
    def length_mm(self) -> float:
        if len(self.points) < 2:
            return 0.0
        return float(np.sum(np.linalg.norm(np.diff(self.points, axis=0), axis=1)))

    def summary(self, include_points: bool = False) -> dict:
        out = {
            "id": self.id,
            "color": self.color,
            "status": self.status,
            "n_points": int(len(self.points)),
            "length_mm": round(self.length_mm, 1),
            "error": self.error,
        }
        if include_points:
            out["points"] = [[round(float(x), 2), round(float(y), 2)]
                             for x, y in self.points]
        return out


class SketchSession:
    """Owns the stroke queue and the worker that draws it."""

    #: Hard ceiling so a runaway client cannot queue unbounded work.
    MAX_QUEUE = 500
    #: Completed strokes kept for the UI before the oldest are dropped.
    MAX_HISTORY = 300

    def __init__(self, machine: Machine, cfg: MachineConfig | None = None,
                 safe_area: SafeArea | None = None):
        self.machine = machine
        self.cfg = cfg or machine.cfg
        self.safe_area = safe_area or machine.safe_area
        self.state = SketchState.STOPPED
        self.strokes: list[SketchStroke] = []
        self.current_id: int | None = None
        self.done_count = 0
        self.failed_count = 0
        self.last_error: str | None = None

        self._next_id = 1
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._listeners: list[Callable[[dict], None]] = []

    # ------------------------------------------------------------------

    def add_listener(self, callback: Callable[[dict], None]) -> None:
        self._listeners.append(callback)

    def _emit(self, kind: str, **payload) -> None:
        event = {"kind": kind, **payload}
        for cb in list(self._listeners):
            try:
                result = cb(event)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:      # pragma: no cover
                log.exception("sketch listener raised")

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.active:
            return
        if self.machine.state not in (MachineState.IDLE, MachineState.JOGGING):
            raise FluidNCError(
                f"cannot start sketching while the machine is "
                f"{self.machine.state.value}")
        await self.machine.set_pen(False)
        self.state = SketchState.READY
        self.last_error = None
        self._task = asyncio.create_task(self._worker(), name="sketch-worker")
        self._emit("sketch_started_session", **self.snapshot())

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            if self.machine.driver.connected:
                await self.machine.set_pen(False)
        except Exception:
            log.debug("could not lift the pen when stopping the sketch",
                      exc_info=True)
        self.state = SketchState.STOPPED
        self.current_id = None
        self._emit("sketch_stopped", **self.snapshot())

    # ------------------------------------------------------------------
    # queue
    # ------------------------------------------------------------------

    @property
    def queued_count(self) -> int:
        return sum(1 for s in self.strokes if s.status == "queued")

    async def enqueue(self, points: Sequence[Sequence[float]],
                      color: int) -> dict:
        """Validate and queue one stroke. Never raises for bad input."""
        if not self.active:
            return {"ok": False, "error": "the sketch session is not running"}
        if not 0 <= color < self.cfg.n_colors:
            return {"ok": False,
                    "error": f"colour {color} does not exist "
                             f"(0..{self.cfg.n_colors - 1})"}
        if self.queued_count >= self.MAX_QUEUE:
            return {"ok": False,
                    "error": f"the queue is full ({self.MAX_QUEUE} strokes) — "
                             f"let the plotter catch up first"}
        try:
            pts = np.asarray(points, dtype=float).reshape(-1, 2)
        except (TypeError, ValueError):
            return {"ok": False, "error": "points must be [[x, y], ...] in mm"}
        if len(pts) < 2 or not np.all(np.isfinite(pts)):
            return {"ok": False, "error": "a stroke needs at least two finite points"}

        violations = self.safe_area.check_points(pts)
        if violations:
            return {"ok": False,
                    "error": f"the stroke leaves the drawable area: "
                             f"{violations[0]}",
                    "violations": [str(v) for v in violations[:3]]}

        simplified = Polyline(pts).simplify(
            self.cfg.paths.simplify_tolerance_mm).dedupe()
        if len(simplified) < 2:
            return {"ok": False, "error": "the stroke is too short to draw"}

        stroke = SketchStroke(id=self._next_id, color=int(color),
                              points=simplified.points)
        self._next_id += 1
        self.strokes.append(stroke)
        self._trim_history()
        self._wake.set()
        self._emit("sketch_queued", stroke=stroke.summary(include_points=True),
                   queued=self.queued_count)
        return {"ok": True, "id": stroke.id,
                "n_points": int(len(stroke.points)),
                "queued": self.queued_count}

    def clear(self) -> list[int]:
        """Drop everything still queued. The stroke being drawn continues."""
        removed = [s.id for s in self.strokes if s.status == "queued"]
        self.strokes = [s for s in self.strokes if s.status != "queued"]
        if removed:
            self._emit("sketch_cleared", ids=removed, queued=0)
        return removed

    def _trim_history(self) -> None:
        finished = [s for s in self.strokes if s.status in ("done", "failed")]
        overflow = len(finished) - self.MAX_HISTORY
        if overflow > 0:
            drop = {s.id for s in finished[:overflow]}
            self.strokes = [s for s in self.strokes if s.id not in drop]

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------

    async def _worker(self) -> None:
        try:
            while True:
                stroke = next((s for s in self.strokes
                               if s.status == "queued"), None)
                if stroke is None:
                    if self.state is not SketchState.ERROR:
                        self.state = SketchState.READY
                        self.current_id = None
                    self._wake.clear()
                    await self._wake.wait()
                    continue

                self.state = SketchState.DRAWING
                self.current_id = stroke.id
                stroke.status = "drawing"
                self._emit("sketch_stroke_started", id=stroke.id,
                           color=stroke.color, queued=self.queued_count)
                try:
                    await self._draw(stroke)
                except asyncio.CancelledError:
                    stroke.status = "queued"
                    raise
                except (GrblAlarm, ConnectionLost) as exc:
                    stroke.status = "failed"
                    stroke.error = str(exc)
                    self.failed_count += 1
                    self.last_error = str(exc)
                    self.state = SketchState.ERROR
                    self._emit("sketch_stroke_failed", id=stroke.id,
                               error=str(exc))
                    self._emit("sketch_error", error=str(exc))
                    return
                except Exception as exc:
                    stroke.status = "failed"
                    stroke.error = str(exc)
                    self.failed_count += 1
                    self.last_error = str(exc)
                    self._emit("sketch_stroke_failed", id=stroke.id,
                               error=str(exc))
                    continue
                stroke.status = "done"
                self.done_count += 1
                self._emit("sketch_stroke_done", id=stroke.id,
                           queued=self.queued_count)
        except asyncio.CancelledError:      # pragma: no cover
            raise

    async def _draw(self, stroke: SketchStroke) -> None:
        machine = self.machine
        driver = machine.driver
        if stroke.color != machine.active_color:
            await machine.set_color(stroke.color)

        travel = self.cfg.motion.travel_feed_mm_min
        draw = self.cfg.motion.draw_feed_mm_min
        x, y = machine.writer.map_point(
            (float(stroke.points[0][0]), float(stroke.points[0][1])))
        await driver.send_line(f"G1 X{x:.3f} Y{y:.3f} F{travel:.0f}")
        await machine.set_pen(True)
        lines = []
        for p in stroke.points[1:]:
            px, py = machine.writer.map_point((float(p[0]), float(p[1])))
            lines.append(f"G1 X{px:.3f} Y{py:.3f} F{draw:.0f}")
        await driver.stream(lines)
        await machine.set_pen(False)
        # stream() returns when the controller has *acknowledged* the lines —
        # they sit in the planner, the gondola is still moving. "done" in the
        # UI means "this is ink now", so wait for the motion to actually
        # finish. It also means an alarm mid-stroke surfaces here, attributed
        # to the stroke that caused it, instead of poisoning the next one.
        await driver.wait_until_idle(timeout=600.0)

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def snapshot(self, include_points: bool = False,
                 max_strokes: int = 40) -> dict:
        recent = self.strokes[-max_strokes:]
        return {
            "active": self.active,
            "state": self.state.value,
            "queued": self.queued_count,
            "done": self.done_count,
            "failed": self.failed_count,
            "current_id": self.current_id,
            "total": len(self.strokes),
            "strokes": [s.summary(include_points=include_points)
                        for s in recent],
            "last_error": self.last_error,
            "undo_supported": False,
        }
