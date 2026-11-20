"""Machine state machine: the one place that decides what the plotter does.

Everything above this layer (REST, websocket, MCP, live drawing) asks the
:class:`Machine` for state changes and gets events back.  Nothing else talks to
:class:`~wbbot.driver.fluidnc.FluidNCDriver` directly while a job is running.

Safety rules encoded here, not left to callers:

* a lost connection immediately becomes an **E-stop**, never a silent stall;
* **cancel** decelerates first (``!``) and only then flushes (``0x18``), so the
  controller does not alarm out with a lost position;
* a paused job **remembers its stroke**, so resuming continues in the right
  colour at the right place instead of restarting;
* the pen is lifted on every abnormal exit.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Awaitable, Sequence

from ..config import MachineConfig
from ..core.gcode import GCodeProgram, GCodeWriter
from ..core.safearea import SafeArea, SafeAreaViolation
from ..core.toolpath import Toolpath
from .fluidnc import (ConnectionLost, FluidNCDriver, FluidNCError, GrblAlarm,
                      GrblError, MachineStatus)
from .simulator import FluidNCSimulator
from .transport import Transport, TransportError, make_transport

log = logging.getLogger(__name__)

__all__ = [
    "MachineState",
    "JobProgress",
    "MachineEvent",
    "Machine",
]


class MachineState(str, Enum):
    DISCONNECTED = "disconnected"
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    ALARM = "alarm"
    ESTOP = "estop"
    HOMING = "homing"
    JOGGING = "jogging"


#: State transitions that are allowed. Anything else is a bug, and is logged.
_ALLOWED: dict[MachineState, set[MachineState]] = {
    MachineState.DISCONNECTED: {MachineState.IDLE, MachineState.ALARM,
                                MachineState.DISCONNECTED},
    MachineState.IDLE: {MachineState.RUNNING, MachineState.HOMING,
                        MachineState.JOGGING, MachineState.ALARM,
                        MachineState.ESTOP, MachineState.DISCONNECTED,
                        MachineState.IDLE},
    MachineState.RUNNING: {MachineState.PAUSED, MachineState.IDLE,
                           MachineState.ALARM, MachineState.ESTOP,
                           MachineState.DISCONNECTED},
    MachineState.PAUSED: {MachineState.RUNNING, MachineState.IDLE,
                          MachineState.ALARM, MachineState.ESTOP,
                          MachineState.DISCONNECTED},
    MachineState.ALARM: {MachineState.IDLE, MachineState.ESTOP,
                         MachineState.DISCONNECTED, MachineState.ALARM},
    MachineState.ESTOP: {MachineState.IDLE, MachineState.DISCONNECTED,
                         MachineState.ALARM, MachineState.ESTOP},
    MachineState.HOMING: {MachineState.IDLE, MachineState.ALARM,
                          MachineState.ESTOP, MachineState.DISCONNECTED},
    MachineState.JOGGING: {MachineState.IDLE, MachineState.ALARM,
                           MachineState.ESTOP, MachineState.DISCONNECTED,
                           MachineState.JOGGING},
}


@dataclass
class JobProgress:
    """Live progress of the running job."""

    lines_done: int = 0
    lines_total: int = 0
    stroke_index: int | None = None
    strokes_total: int = 0
    seconds_done: float = 0.0
    seconds_total: float = 0.0
    started_at: float | None = None
    title: str = ""

    @property
    def percent(self) -> float:
        """Progress by *planned time*, not by line count.

        A traced photo has strokes that differ in length by two orders of
        magnitude; a line-count percentage would sit at 90 % for half the job.
        """
        if self.seconds_total > 0:
            return min(100.0, 100.0 * self.seconds_done / self.seconds_total)
        if self.lines_total > 0:
            return min(100.0, 100.0 * self.lines_done / self.lines_total)
        return 0.0

    @property
    def eta_s(self) -> float:
        return max(0.0, self.seconds_total - self.seconds_done)

    @property
    def elapsed_s(self) -> float:
        return 0.0 if self.started_at is None else time.monotonic() - self.started_at

    def to_dict(self) -> dict:
        return {
            "lines_done": self.lines_done,
            "lines_total": self.lines_total,
            "stroke_index": self.stroke_index,
            "strokes_total": self.strokes_total,
            "percent": round(self.percent, 2),
            "seconds_done": round(self.seconds_done, 1),
            "seconds_total": round(self.seconds_total, 1),
            "eta_s": round(self.eta_s, 1),
            "elapsed_s": round(self.elapsed_s, 1),
            "title": self.title,
        }


@dataclass(frozen=True)
class MachineEvent:
    """Something the UI should know about."""

    kind: str
    payload: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "payload": self.payload,
                "timestamp": self.timestamp}


class Machine:
    """Owns the driver, the current job and the machine state."""

    def __init__(self, cfg: MachineConfig, *,
                 transport: Transport | None = None,
                 simulator: FluidNCSimulator | None = None):
        self.cfg = cfg
        self.simulator = simulator
        if transport is None:
            if cfg.connection.transport == "simulator" and simulator is None:
                self.simulator = FluidNCSimulator(cfg)
            transport = make_transport(cfg, self.simulator)
        self.transport = transport
        self.driver = FluidNCDriver(cfg, transport)
        self.safe_area = SafeArea(cfg)
        self.writer = GCodeWriter(cfg, self.safe_area)

        self._state = MachineState.DISCONNECTED
        self.progress = JobProgress()
        self.last_error: str | None = None
        self.active_color: int = 0
        self.pen_down: bool = False

        self._toolpath: Toolpath | None = None
        self._program: GCodeProgram | None = None
        self._job_task: asyncio.Task | None = None
        self._pause_event = asyncio.Event()
        self._cancel_event = asyncio.Event()
        self._resume_stroke = 0
        self._listeners: list[Callable[[MachineEvent], Any]] = []
        self._lock = asyncio.Lock()

        self.driver.on_status = self._on_status
        self.driver.on_alarm = self._on_alarm
        self.driver.on_disconnect = self._on_disconnect
        self.driver.on_message = self._on_message

    # ------------------------------------------------------------------
    # events
    # ------------------------------------------------------------------

    def add_listener(self, callback: Callable[[MachineEvent], Any]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[MachineEvent], Any]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def _emit(self, kind: str, **payload) -> None:
        event = MachineEvent(kind=kind, payload=payload)
        for cb in list(self._listeners):
            try:
                result = cb(event)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:  # pragma: no cover
                log.exception("machine event listener raised")

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    @property
    def state(self) -> MachineState:
        return self._state

    def _set_state(self, new: MachineState, reason: str = "") -> None:
        if new == self._state:
            return
        if new not in _ALLOWED.get(self._state, set()):
            log.warning("unexpected state transition %s -> %s (%s)",
                        self._state.value, new.value, reason)
        old = self._state
        self._state = new
        log.info("state %s -> %s%s", old.value, new.value,
                 f" ({reason})" if reason else "")
        self._emit("state", state=new.value, previous=old.value, reason=reason)

    @property
    def is_busy(self) -> bool:
        return self._state in (MachineState.RUNNING, MachineState.HOMING,
                               MachineState.JOGGING)

    # ------------------------------------------------------------------
    # driver callbacks
    # ------------------------------------------------------------------

    def _on_status(self, status: MachineStatus) -> None:
        pen = self.cfg.servo.pen
        span = abs(pen.up_pos - pen.down_pos)
        self.pen_down = abs(status.z - pen.down_pos) < span * 0.5 if span else False
        positions = self.cfg.servo.revolver.positions
        if positions:
            self.active_color = min(range(len(positions)),
                                    key=lambda i: abs(status.a - positions[i]))
        self._emit("status", **status.to_dict(),
                   pen_down=self.pen_down, color=self.active_color)

    def _on_alarm(self, alarm: GrblAlarm) -> None:
        self.last_error = str(alarm)
        self._cancel_event.set()
        self._set_state(MachineState.ALARM, str(alarm))
        self._emit("alarm", code=alarm.code, message=str(alarm))

    def _on_disconnect(self, reason: str) -> None:
        self.last_error = reason
        self._cancel_event.set()
        # A dropped link during motion is not recoverable by waiting; the only
        # correct response is to treat it as an emergency stop. Never downgrade
        # an already-declared E-stop or alarm to a plain "disconnected".
        if self._state in (MachineState.ESTOP, MachineState.ALARM):
            pass
        elif self.is_busy:
            self._set_state(MachineState.ESTOP, reason)
        else:
            self._set_state(MachineState.DISCONNECTED, reason)
        self._emit("disconnected", reason=reason)

    def _on_message(self, message: str) -> None:
        self._emit("message", text=message)

    # ------------------------------------------------------------------
    # connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        if self._state != MachineState.DISCONNECTED:
            return
        try:
            await self.driver.connect()
        except (TransportError, ConnectionLost) as exc:
            self.last_error = str(exc)
            self._emit("error", message=str(exc))
            raise
        self.last_error = None
        if self.driver.status.is_alarm:
            self._set_state(MachineState.ALARM, "controller reports alarm")
        else:
            self._set_state(MachineState.IDLE, "connected")
        self._emit("connected", transport=self.transport.description)

    async def disconnect(self) -> None:
        await self._stop_job_task()
        await self.driver.disconnect()
        self._set_state(MachineState.DISCONNECTED, "closed by user")

    # ------------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------------

    def prepare(self, toolpath: Toolpath, *, title: str = "job",
                resume_from_stroke: int = 0) -> GCodeProgram:
        """Validate and render a toolpath without sending anything."""
        toolpath.validate(self.safe_area)
        program = self.writer.render_toolpath(
            toolpath, resume_from_stroke=resume_from_stroke, title=title)
        return program

    async def run_toolpath(self, toolpath: Toolpath, *, title: str = "job",
                           resume_from_stroke: int = 0) -> None:
        """Render, validate and start streaming. Returns once the job starts."""
        async with self._lock:
            if self._state == MachineState.DISCONNECTED:
                raise FluidNCError("not connected")
            if self._state in (MachineState.ALARM, MachineState.ESTOP):
                raise FluidNCError(
                    f"cannot start a job while in {self._state.value} state — "
                    f"clear it first ({self.last_error or 'unknown cause'})")
            if self.is_busy:
                raise FluidNCError(f"machine is {self._state.value}")

            program = self.prepare(toolpath, title=title,
                                   resume_from_stroke=resume_from_stroke)
            self._toolpath = toolpath
            self._program = program
            self._resume_stroke = resume_from_stroke
            self._pause_event.clear()
            self._cancel_event.clear()

            executable = program.executable_lines()
            durations = program.executable_durations()
            self.progress = JobProgress(
                lines_done=0,
                lines_total=len(executable),
                stroke_index=resume_from_stroke if toolpath.strokes else None,
                strokes_total=len(toolpath.strokes),
                seconds_total=sum(durations),
                started_at=time.monotonic(),
                title=title,
            )
            self._set_state(MachineState.RUNNING, title)
            self._emit("job_started", **self.progress.to_dict(),
                       stats=program.stats.to_dict() if program.stats else {})
            self._job_task = asyncio.create_task(
                self._run_job(program), name="wbbot-job")

    async def _run_job(self, program: GCodeProgram) -> None:
        executable = program.executable_lines()
        lines = [text for _, text in executable]
        line_indices = [idx for idx, _ in executable]
        durations = program.executable_durations()
        stroke_of_line = program.progress_map()
        cumulative = 0.0
        acc: list[float] = []
        for d in durations:
            cumulative += d
            acc.append(cumulative)

        def on_progress(done: int, total: int) -> None:
            self.progress.lines_done = done
            self.progress.seconds_done = acc[done - 1] if 0 < done <= len(acc) else 0.0
            if done - 1 < len(line_indices):
                si = stroke_of_line[line_indices[done - 1]]
                if si is not None:
                    self.progress.stroke_index = si
            self._emit("progress", **self.progress.to_dict())

        try:
            await self.driver.stream(lines, on_progress=on_progress,
                                     pause_event=self._pause_event,
                                     cancel_event=self._cancel_event)
            if self._cancel_event.is_set():
                self._emit("job_cancelled", **self.progress.to_dict())
                return
            await self.driver.drain()
            await self.driver.wait_until_idle(timeout=3600.0)
            self.progress.lines_done = self.progress.lines_total
            self.progress.seconds_done = self.progress.seconds_total
            self._set_state(MachineState.IDLE, "job finished")
            self._emit("job_finished", **self.progress.to_dict())
        except asyncio.CancelledError:
            raise
        except GrblAlarm as exc:
            self.last_error = str(exc)
            self._set_state(MachineState.ALARM, str(exc))
            self._emit("job_failed", message=str(exc), **self.progress.to_dict())
        except (ConnectionLost, TransportError) as exc:
            if self._cancel_event.is_set():
                # The link was torn down by our own cancel; not a failure.
                self._emit("job_cancelled", **self.progress.to_dict())
                return
            self.last_error = str(exc)
            self._set_state(MachineState.ESTOP, str(exc))
            self._emit("job_failed", message=str(exc), **self.progress.to_dict())
        except (GrblError, FluidNCError) as exc:
            if self._cancel_event.is_set():
                # Cancelling flushes the planner, so the lines still in flight
                # will never be acknowledged and the streamer raises. That is
                # the expected outcome of a deliberate stop, not an error to
                # put in front of the user.
                self._emit("job_cancelled", **self.progress.to_dict())
                return
            self.last_error = str(exc)
            await self._safe_stop()
            self._set_state(MachineState.IDLE, "job aborted")
            self._emit("job_failed", message=str(exc), **self.progress.to_dict())
        finally:
            self._job_task = None

    async def _stop_job_task(self) -> None:
        self._cancel_event.set()
        task = self._job_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._job_task = None

    async def _safe_stop(self) -> None:
        """Lift the pen without assuming the link still works."""
        try:
            pen = self.cfg.servo.pen
            await self.driver.send_line(
                f"G1 {pen.axis}{pen.up_pos:.2f} F{self.cfg.servo.feed_units_min:.0f}",
                timeout=5.0)
        except Exception:
            log.debug("could not lift the pen during safe stop", exc_info=True)

    # ------------------------------------------------------------------
    # control
    # ------------------------------------------------------------------

    async def pause(self) -> None:
        """Feed hold; the controller finishes decelerating and waits."""
        if self._state != MachineState.RUNNING:
            raise FluidNCError(f"nothing to pause (state: {self._state.value})")
        self._pause_event.set()
        await self.driver.feed_hold()
        self._set_state(MachineState.PAUSED, "paused by user")
        self._emit("paused", **self.progress.to_dict())

    async def resume(self) -> None:
        if self._state != MachineState.PAUSED:
            raise FluidNCError(f"nothing to resume (state: {self._state.value})")
        await self.driver.cycle_resume()
        self._pause_event.clear()
        self._set_state(MachineState.RUNNING, "resumed by user")
        self._emit("resumed", **self.progress.to_dict())

    async def cancel(self) -> None:
        """Stop the job cleanly and remember where to pick it up again.

        Decelerate with a feed hold *first*, then flush the planner.  Resetting
        while the machine is still moving is what makes FluidNC raise
        ``ALARM:3`` and forget its position.
        """
        if self._state not in (MachineState.RUNNING, MachineState.PAUSED):
            raise FluidNCError(f"no job to cancel (state: {self._state.value})")
        stroke = self.progress.stroke_index or 0
        self._cancel_event.set()
        self._pause_event.set()
        try:
            await self.driver.feed_hold()
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                if self.driver.status.is_hold or self.driver.status.is_idle:
                    break
            await self.driver.soft_reset()
            await asyncio.sleep(0.2)
            await self._safe_stop()
        except (ConnectionLost, TransportError) as exc:
            self.last_error = str(exc)
            self._set_state(MachineState.ESTOP, str(exc))
            return
        await self._stop_job_task()
        self._resume_stroke = stroke
        self._pause_event.clear()
        # A user-requested stop leaves nothing to report; clearing this stops
        # the internal "soft reset issued" note reaching the status bar.
        self.last_error = None
        self._set_state(MachineState.IDLE, "cancelled by user")
        self._emit("job_cancelled", resume_from_stroke=stroke,
                   **self.progress.to_dict())

    async def resume_cancelled_job(self) -> None:
        """Restart the last job from the stroke it was interrupted at."""
        if self._toolpath is None:
            raise FluidNCError("there is no previous job to resume")
        await self.run_toolpath(self._toolpath,
                                title=f"{self.progress.title} (resumed)",
                                resume_from_stroke=self._resume_stroke)

    async def emergency_stop(self) -> None:
        """Stop now. Correctness of position is explicitly not a goal here."""
        self._cancel_event.set()
        self._pause_event.set()
        try:
            await self.driver.feed_hold()
            await self.driver.soft_reset()
        except Exception:
            log.warning("e-stop could not reach the controller", exc_info=True)
        await self._stop_job_task()
        if self.progress.stroke_index is not None:
            self._resume_stroke = self.progress.stroke_index
        self._set_state(MachineState.ESTOP, "emergency stop")
        self._emit("estop")

    async def clear_alarm(self) -> None:
        """``$X`` unlock, then back to idle."""
        if self._state not in (MachineState.ALARM, MachineState.ESTOP):
            return
        if not self.driver.connected:
            raise FluidNCError("not connected")
        await self.driver.unlock()
        await self._safe_stop()
        self.last_error = None
        self._set_state(MachineState.IDLE, "alarm cleared")
        self._emit("alarm_cleared")

    async def home(self) -> None:
        if self.is_busy:
            raise FluidNCError(f"machine is {self._state.value}")
        self._set_state(MachineState.HOMING, "homing")
        try:
            await self.driver.home()
            await self.driver.wait_until_idle(timeout=180.0)
        except Exception as exc:
            self.last_error = str(exc)
            self._set_state(MachineState.ALARM, str(exc))
            raise
        self._set_state(MachineState.IDLE, "homed")

    # ------------------------------------------------------------------
    # direct motion (manual mode / MCP primitives)
    # ------------------------------------------------------------------

    async def move_to(self, x: float, y: float, *, feed: float | None = None,
                      draw: bool = False) -> None:
        """Single validated move. Refuses anything outside the safe area."""
        if self._state not in (MachineState.IDLE, MachineState.JOGGING):
            raise FluidNCError(f"machine is {self._state.value}")
        reason = self.safe_area.reason((x, y))
        if reason is not None:
            raise SafeAreaViolation(f"({x:.1f}, {y:.1f}) mm is not reachable: {reason}")
        start = self.driver.status.xy
        violations = self.safe_area.check_segment(start, (x, y))
        if violations:
            raise SafeAreaViolation(
                f"the path from ({start[0]:.0f}, {start[1]:.0f}) to "
                f"({x:.0f}, {y:.0f}) leaves the safe area", violations)
        feed = feed or (self.cfg.motion.draw_feed_mm_min if draw
                        else self.cfg.motion.travel_feed_mm_min)
        px, py = self.writer.map_point((x, y))
        await self.driver.send_line(f"G1 X{px:.3f} Y{py:.3f} F{feed:.0f}")

    async def set_pen(self, down: bool) -> None:
        pen = self.cfg.servo.pen
        if down:
            pos = self.driver.status.xy
            reason = self.safe_area.reason(pos)
            if reason is not None:
                raise SafeAreaViolation(
                    f"refusing to lower the pen at ({pos[0]:.1f}, {pos[1]:.1f}) "
                    f"mm: {reason}")
        target = pen.down_pos if down else pen.up_pos
        await self.driver.send_line(
            f"G1 {pen.axis}{target:.2f} F{self.cfg.servo.feed_units_min:.0f}")
        self.pen_down = down

    async def set_color(self, index: int) -> None:
        if self.pen_down:
            await self.set_pen(False)
        pos = self.cfg.revolver_position(index)
        rev = self.cfg.servo.revolver
        await self.driver.send_line(
            f"G1 {rev.axis}{pos:.2f} F{self.cfg.servo.feed_units_min:.0f}")
        if self.cfg.motion.color_change_dwell_s > 0:
            await self.driver.send_line(
                f"G4 P{self.cfg.motion.color_change_dwell_s:.3f}")
        self.active_color = index

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        """Everything the UI needs in one object."""
        status = self.driver.status
        return {
            "state": self._state.value,
            "connected": self.driver.connected,
            "transport": self.transport.description,
            "position": {"x": round(status.x, 2), "y": round(status.y, 2)},
            "controller_state": status.state,
            "pen_down": self.pen_down,
            "color": self.active_color,
            "color_name": (self.cfg.color(self.active_color).name
                           if self.active_color < self.cfg.n_colors else "?"),
            "feed": round(status.feed, 1),
            "progress": self.progress.to_dict(),
            "last_error": self.last_error,
            "can_resume": self._toolpath is not None and self._resume_stroke > 0,
            "resume_from_stroke": self._resume_stroke,
            "pending_lines": self.driver.pending_lines,
        }
