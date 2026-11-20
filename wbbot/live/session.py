"""Live drawing: the mouse steers the gondola, the pen follows.

Two modes, and they need genuinely different plumbing.

**Cursor follow (button up).**  The target changes constantly, so queued moves
are worthless the moment they are queued.  Jog commands (``$J=``) are the right
primitive: ``0x85`` (jog cancel) flushes *only* jog motion and empties the
planner immediately, so every cursor update becomes "cancel, re-aim".  Doing
this with ``G1`` would build a queue of stale destinations that the gondola
would then dutifully visit one by one.

**Drawing (button down).**  Now every point matters — the line has to pass
through all of them.  Short ``G1`` segments are streamed with a deliberately
*shallow* queue (2-4 blocks): a deep planner buffer would give smoother motion
but put the pen a second behind the mouse.

Honesty about lag
-----------------
The plotter is much slower than a mouse.  The session therefore tracks three
separate things so the UI can show all of them:

* ``actual`` — where the gondola really is, from the status reports;
* ``pending`` — target points sent but not yet reached;
* ``drawn`` — segments the machine has actually completed.

Undo is physically impossible.  The session exposes no undo, and the UI is
expected to say so rather than offer a button that only edits the preview.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Sequence

import numpy as np

from ..config import MachineConfig
from ..core.safearea import SafeArea
from ..driver.fluidnc import ConnectionLost, FluidNCError
from ..driver.machine import Machine, MachineState

log = logging.getLogger(__name__)

__all__ = ["LiveConfig", "LiveState", "LiveSession", "LiveStroke"]


@dataclass(frozen=True)
class LiveConfig:
    """Tuning for the live loop."""

    #: How often the sender acts on the newest cursor position.
    update_hz: float = 20.0
    #: Feed for cursor-follow jogs.
    jog_feed_mm_min: float = 4000.0
    #: Feed while drawing.
    draw_feed_mm_min: float = 1800.0
    #: Ignore cursor moves smaller than this (noise, and wasted jogs).
    min_move_mm: float = 1.5
    #: Ignore drawn points closer than this to the previous one.
    min_segment_mm: float = 1.0
    #: Split anything longer than this so a single move cannot run away.
    max_segment_mm: float = 40.0
    #: Keep at most this many un-acknowledged blocks while drawing. This is
    #: the single most important latency knob.
    max_pending_blocks: int = 3
    #: Warn above this measured lag.
    latency_target_ms: float = 150.0

    def __post_init__(self) -> None:
        if not 1.0 <= self.update_hz <= 100.0:
            raise ValueError("update_hz must be 1..100")
        if self.max_pending_blocks < 1:
            raise ValueError("max_pending_blocks must be >= 1")
        if self.min_segment_mm <= 0 or self.max_segment_mm <= self.min_segment_mm:
            raise ValueError("segment limits must satisfy 0 < min < max")


class LiveState(str, Enum):
    STOPPED = "stopped"
    FOLLOWING = "following"     # pen up, chasing the cursor
    DRAWING = "drawing"         # pen down, tracing the cursor
    BLOCKED = "blocked"         # cursor is outside the safe area
    ERROR = "error"


@dataclass
class LiveStroke:
    """One pen-down stroke recorded during the session."""

    color_index: int
    points: list[tuple[float, float]] = field(default_factory=list)

    @property
    def length(self) -> float:
        if len(self.points) < 2:
            return 0.0
        arr = np.array(self.points)
        return float(np.sum(np.linalg.norm(np.diff(arr, axis=0), axis=1)))


class LiveSession:
    """Owns the live-drawing loop for one machine."""

    def __init__(self, machine: Machine, cfg: MachineConfig | None = None,
                 live_cfg: LiveConfig | None = None,
                 safe_area: SafeArea | None = None):
        self.machine = machine
        self.cfg = cfg or machine.cfg
        self.live = live_cfg or LiveConfig(
            draw_feed_mm_min=machine.cfg.motion.draw_feed_mm_min,
            jog_feed_mm_min=machine.cfg.motion.travel_feed_mm_min,
        )
        self.safe_area = safe_area or machine.safe_area

        self.state = LiveState.STOPPED
        self.color_index = machine.active_color
        self.pen_down = False

        self._cursor: tuple[float, float] | None = None
        self._cursor_stamp: float = 0.0
        self._want_drawing = False
        self._last_sent: tuple[float, float] | None = None
        self._last_jog_target: tuple[float, float] | None = None

        self.pending: list[tuple[float, float]] = []
        self.strokes: list[LiveStroke] = []
        self._current: LiveStroke | None = None

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._listeners: list[Callable[[dict], None]] = []
        self.blocked_reason: str | None = None
        self.last_error: str | None = None

        # -- diagnostics ----------------------------------------------------
        self.measured_latency_ms: float = 0.0
        self._latency_samples: list[float] = []
        self._sends = 0
        self._jog_cancels = 0

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
                log.exception("live listener raised")

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._task is not None:
            return
        if self.machine.state not in (MachineState.IDLE, MachineState.JOGGING):
            raise FluidNCError(
                f"cannot start live drawing while the machine is "
                f"{self.machine.state.value}")
        self._stop.clear()
        self.state = LiveState.FOLLOWING
        self.pen_down = False
        self.blocked_reason = None
        self.last_error = None
        await self.machine.set_pen(False)
        self._task = asyncio.create_task(self._loop(), name="live-session")
        self._emit("live_started", **self.snapshot())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._end_stroke()
        try:
            if self.machine.driver.connected:
                await self.machine.driver.jog_cancel()
                await self.machine.set_pen(False)
        except Exception:
            log.debug("could not park the pen when stopping", exc_info=True)
        self.pen_down = False
        self.state = LiveState.STOPPED
        self._emit("live_stopped", **self.snapshot())

    # ------------------------------------------------------------------
    # input
    # ------------------------------------------------------------------

    def set_cursor(self, x: float, y: float) -> None:
        """Newest cursor position, in board millimetres.

        Called at whatever rate the browser produces mouse events; only the
        most recent value is ever used, so a fast mouse cannot flood the link.
        """
        if not (math.isfinite(x) and math.isfinite(y)):
            return
        self._cursor = (float(x), float(y))
        self._cursor_stamp = time.monotonic()

    async def set_drawing(self, drawing: bool) -> None:
        """Mouse button down/up."""
        self._want_drawing = bool(drawing)
        if not drawing:
            await self._end_stroke()

    async def set_color(self, index: int) -> None:
        """Change marker mid-session (keys 1/2/3)."""
        if index == self.color_index:
            return
        if not 0 <= index < self.cfg.n_colors:
            raise ValueError(
                f"colour {index} out of range (0..{self.cfg.n_colors - 1})")
        was_drawing = self._want_drawing
        self._want_drawing = False
        await self._end_stroke()
        if self.pen_down:
            await self.machine.set_pen(False)
            self.pen_down = False
        await self.machine.set_color(index)
        self.color_index = index
        self._want_drawing = was_drawing
        self._emit("live_color", color=index)

    async def emergency_stop(self) -> None:
        self._want_drawing = False
        self._stop.set()
        await self.machine.emergency_stop()
        self.state = LiveState.ERROR
        self.last_error = "emergency stop"
        await self._end_stroke()
        self._emit("live_estop", **self.snapshot())

    # ------------------------------------------------------------------
    # the loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        interval = 1.0 / self.live.update_hz
        try:
            while not self._stop.is_set():
                await asyncio.sleep(interval)
                try:
                    await self._tick()
                except (ConnectionLost, FluidNCError) as exc:
                    self.last_error = str(exc)
                    self.state = LiveState.ERROR
                    self._emit("live_error", message=str(exc))
                    return
        except asyncio.CancelledError:      # pragma: no cover
            raise

    async def _tick(self) -> None:
        cursor = self._cursor
        if cursor is None or not self.machine.driver.connected:
            return

        target, clamped = self.safe_area.clamp(cursor)
        if clamped:
            reason = self.safe_area.reason(cursor) or "outside the safe area"
            if self.blocked_reason != reason:
                self.blocked_reason = reason
                self._emit("live_blocked", reason=reason,
                           cursor=list(cursor), clamped_to=list(target))
            # Never draw a fake line along the boundary.
            if self.pen_down:
                await self.machine.set_pen(False)
                self.pen_down = False
                await self._end_stroke()
            self.state = LiveState.BLOCKED
        else:
            if self.blocked_reason is not None:
                self.blocked_reason = None
                self._emit("live_unblocked")

        if self._want_drawing and not clamped:
            await self._tick_drawing(target)
        else:
            await self._tick_following(target, clamped)

    async def _tick_following(self, target: tuple[float, float],
                              clamped: bool) -> None:
        if self.pen_down:
            await self.machine.set_pen(False)
            self.pen_down = False
            await self._end_stroke()
        if not clamped:
            self.state = LiveState.FOLLOWING

        if (self._last_jog_target is not None
                and math.dist(target, self._last_jog_target) < self.live.min_move_mm):
            return

        # Cancel first: a jog already in flight is aimed at a stale point.
        await self.machine.driver.jog_cancel()
        self._jog_cancels += 1
        await self.machine.driver.jog_to(target[0], target[1],
                                         self.live.jog_feed_mm_min)
        self._last_jog_target = target
        self._sends += 1

    async def _tick_drawing(self, target: tuple[float, float]) -> None:
        driver = self.machine.driver
        if not self.pen_down:
            # Stop chasing before putting the pen down, or the first stroke
            # gets dragged from wherever the jog happened to be.
            await driver.jog_cancel()
            self._last_jog_target = None
            await self._goto_before_drawing(target)
            await self.machine.set_pen(True)
            self.pen_down = True
            self.state = LiveState.DRAWING
            self._current = LiveStroke(color_index=self.color_index,
                                       points=[target])
            self._last_sent = target
            self.pending = []
            return

        self.state = LiveState.DRAWING
        if self._last_sent is not None:
            distance = math.dist(target, self._last_sent)
            if distance < self.live.min_segment_mm:
                return
        # Keep the queue shallow — this is the latency budget.
        if driver.pending_lines >= self.live.max_pending_blocks:
            return

        segments = self._split(self._last_sent or target, target)
        for point in segments:
            if driver.pending_lines >= self.live.max_pending_blocks:
                break
            px, py = self.machine.writer.map_point(point)
            t0 = time.monotonic()
            await driver.send_line_nowait(
                f"G1 X{px:.3f} Y{py:.3f} F{self.live.draw_feed_mm_min:.0f}")
            self._record_latency(t0)
            self._last_sent = point
            self.pending.append(point)
            self._sends += 1
            if self._current is not None:
                self._current.points.append(point)

    def _split(self, start: tuple[float, float],
               end: tuple[float, float]) -> list[tuple[float, float]]:
        """Break a long jump into bounded segments."""
        distance = math.dist(start, end)
        if distance <= self.live.max_segment_mm:
            return [end]
        n = int(math.ceil(distance / self.live.max_segment_mm))
        return [(start[0] + (end[0] - start[0]) * i / n,
                 start[1] + (end[1] - start[1]) * i / n)
                for i in range(1, n + 1)]

    async def _goto_before_drawing(self, target: tuple[float, float]) -> None:
        """Move to the stroke start with the pen up, and wait for it."""
        current = self.machine.driver.status.xy
        if math.dist(current, target) < self.live.min_segment_mm:
            return
        violations = self.safe_area.check_segment(current, target)
        if violations:
            # Approach could cut a corner outside the safe area; jog instead
            # and let the firmware's own limits apply.
            await self.machine.driver.jog_to(target[0], target[1],
                                             self.live.jog_feed_mm_min)
            return
        px, py = self.machine.writer.map_point(target)
        await self.machine.driver.send_line(
            f"G1 X{px:.3f} Y{py:.3f} F{self.live.jog_feed_mm_min:.0f}",
            timeout=30.0)

    async def _end_stroke(self) -> None:
        if self._current is not None:
            if len(self._current.points) >= 2:
                self.strokes.append(self._current)
                self._emit("live_stroke_finished",
                           points=len(self._current.points),
                           length_mm=round(self._current.length, 1),
                           color=self._current.color_index)
            self._current = None
        self.pending = []
        self._last_sent = None

    def _record_latency(self, t0: float) -> None:
        ms = (time.monotonic() - t0) * 1000.0
        self._latency_samples.append(ms)
        if len(self._latency_samples) > 64:
            self._latency_samples.pop(0)
        self.measured_latency_ms = float(np.median(self._latency_samples))

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def prune_pending(self) -> None:
        """Drop pending points the gondola has already reached."""
        if not self.pending:
            return
        actual = self.machine.driver.status.xy
        while self.pending and math.dist(self.pending[0], actual) < 3.0:
            self.pending.pop(0)

    @property
    def drawn_strokes(self) -> list[LiveStroke]:
        out = list(self.strokes)
        if self._current is not None and len(self._current.points) >= 2:
            out.append(self._current)
        return out

    def snapshot(self) -> dict:
        self.prune_pending()
        status = self.machine.driver.status
        return {
            "state": self.state.value,
            "pen_down": self.pen_down,
            "color": self.color_index,
            "cursor": list(self._cursor) if self._cursor else None,
            "actual": [round(status.x, 2), round(status.y, 2)],
            "pending": [[round(x, 2), round(y, 2)] for x, y in self.pending],
            "n_strokes": len(self.drawn_strokes),
            "blocked_reason": self.blocked_reason,
            "latency_ms": round(self.measured_latency_ms, 1),
            "latency_ok": self.measured_latency_ms <= self.live.latency_target_ms,
            "queue_depth": self.machine.driver.pending_lines,
            "sends": self._sends,
            "jog_cancels": self._jog_cancels,
            "last_error": self.last_error,
            # Stated explicitly so the UI does not invent an undo button:
            # ink on a whiteboard cannot be taken back by software.
            "undo_supported": False,
        }

    def strokes_to_json(self) -> list[dict]:
        return [{"color": s.color_index,
                 "points": [[round(x, 2), round(y, 2)] for x, y in s.points],
                 "length_mm": round(s.length, 1)}
                for s in self.drawn_strokes]
