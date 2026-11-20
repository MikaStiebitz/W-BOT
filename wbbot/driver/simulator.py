"""A virtual FluidNC controller.

Not a convenience — a requirement.  The whole application must be developable
and testable without the plotter on the desk, including the parts that only
happen when things go wrong.  The simulator therefore models:

* the **G-code subset** the writer emits (G0/G1/G4/G21/G90/G91/G92/G28/M-codes),
* the **``ok`` / ``error:N`` handshake**, including holding back ``ok`` while
  the planner buffer is full — that is what makes character-counting flow
  control testable,
* **realtime bytes** ``?`` ``!`` ``~`` ``0x18`` ``0x85`` ``0x84``,
* **status reports** in FluidNC's ``<State|MPos:..|FS:..|Bf:..>`` format,
* **timed motion** with a trapezoidal profile, so progress and ETA behave,
* **fault injection**: alarms, hard limits, buffer overrun, link loss.

It records everything the pen draws, so a test can assert on the resulting
geometry instead of on the G-code text.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable, Sequence

import numpy as np

from ..config import MachineConfig
from ..core.motion import DEFAULT_JUNCTION_DEVIATION_MM, MotionLimits, junction_speed

__all__ = [
    "SimState",
    "SimBlock",
    "DrawnSegment",
    "FluidNCSimulator",
]

# Realtime command bytes (GRBL / FluidNC).
RT_STATUS = 0x3F        # '?'
RT_FEED_HOLD = 0x21     # '!'
RT_RESUME = 0x7E        # '~'
RT_SOFT_RESET = 0x18    # Ctrl-X
RT_SAFETY_DOOR = 0x84
RT_JOG_CANCEL = 0x85

#: Planner blocks the virtual controller can hold (FluidNC default is 16).
PLANNER_SIZE = 16
#: Serial RX buffer the virtual controller advertises.
RX_BUFFER = 128


class SimState(str, Enum):
    IDLE = "Idle"
    RUN = "Run"
    HOLD = "Hold"
    JOG = "Jog"
    ALARM = "Alarm"
    DOOR = "Door"
    HOME = "Home"
    CHECK = "Check"


@dataclass
class SimBlock:
    """One queued motion block."""

    start: np.ndarray            # 4-axis position [X, Y, Z, A]
    target: np.ndarray
    feed_mm_min: float
    is_jog: bool = False
    dwell_s: float = 0.0
    pen_down: bool = False
    line_no: int | None = None

    entry_speed: float = 0.0     # mm/s
    exit_speed: float = 0.0
    cruise_speed: float = 0.0
    duration: float = 0.0
    elapsed: float = 0.0

    @property
    def delta(self) -> np.ndarray:
        return self.target - self.start

    @property
    def xy_distance(self) -> float:
        return float(np.linalg.norm(self.delta[:2]))

    @property
    def distance(self) -> float:
        """Euclidean distance over all axes (servo axes included)."""
        return float(np.linalg.norm(self.delta))

    def position_at(self, t: float) -> np.ndarray:
        if self.duration <= 1e-12:
            return self.target.copy()
        frac = min(max(t / self.duration, 0.0), 1.0)
        return self.start + self.delta * frac


@dataclass(frozen=True)
class DrawnSegment:
    """A pen-down segment the virtual machine actually put on the board."""

    start: tuple[float, float]
    end: tuple[float, float]
    color_index: int
    t_start: float
    t_end: float


class FluidNCSimulator:
    """Virtual FluidNC. Speaks bytes in, bytes out."""

    def __init__(self, cfg: MachineConfig, *,
                 tick_hz: float = 200.0,
                 time_scale: float = 1.0,
                 planner_size: int = PLANNER_SIZE,
                 rx_buffer: int = RX_BUFFER):
        self.cfg = cfg
        self.tick_hz = float(tick_hz)
        #: >1 runs the virtual machine faster than real time (tests use ~200).
        self.time_scale = float(time_scale)
        self.planner_size = int(planner_size)
        self.rx_buffer = int(rx_buffer)

        self.output: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.link_up = True
        #: Accept bytes but answer nothing — models a wedged controller, which
        #: is what the host watchdog (not the transport) has to catch.
        self.mute = False

        # -- machine state --------------------------------------------------
        self.state = SimState.IDLE
        self.position = np.zeros(4, dtype=float)     # X, Y, Z, A
        self.work_offset = np.zeros(4, dtype=float)
        self.absolute_mode = True
        self.feed_mm_min = cfg.motion.travel_feed_mm_min
        self.alarm_code: int | None = None
        self.last_error: int | None = None

        # Start parked with the pen up and colour 1 selected, which is what a
        # freshly homed machine looks like after the header runs.
        self.position[0] = cfg.motion.park_position[0]
        self.position[1] = cfg.motion.park_position[1]
        self.position[2] = cfg.servo.pen.up_pos
        self.position[3] = cfg.revolver_position(0)

        self._blocks: list[SimBlock] = []
        self._deferred: list[SimBlock] = []
        self._dwell_remaining = 0.0
        self._pending_oks = 0
        self._rx_pending: list[str] = []

        # -- recording ------------------------------------------------------
        self.drawn: list[DrawnSegment] = []
        self.received_lines: list[str] = []
        self.sim_time = 0.0

        # -- fault injection ------------------------------------------------
        self.fail_after_lines: int | None = None
        self.alarm_after_lines: int | None = None
        self.alarm_code_to_raise: int = 1
        self.error_on_line_containing: str | None = None
        self.error_code_to_raise: int = 20
        self.reject_when_planner_full = False
        self._lines_seen = 0

        self._buffer = bytearray()
        self._task: asyncio.Task | None = None
        self._running = False
        self._on_disconnect: Callable[[str], None] | None = None
        self._limits = MotionLimits(
            acceleration_mm_s2=cfg.motion.acceleration_mm_s2,
            junction_deviation_mm=cfg.motion.junction_deviation_mm,
        )

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def set_disconnect_handler(self, handler: Callable[[str], None] | None) -> None:
        self._on_disconnect = handler

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.link_up = True
        self._task = asyncio.create_task(self._run(), name="fluidnc-sim")
        await self._emit("\r\nGrbl 3.7 [FluidNC v3.7.18 (wbbot-simulator)]")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        dt = 1.0 / self.tick_hz
        last = time.monotonic()
        try:
            while self._running:
                await asyncio.sleep(dt)
                now = time.monotonic()
                elapsed = (now - last) * self.time_scale
                last = now
                self._advance(elapsed)
                await self._flush_oks()
        except asyncio.CancelledError:  # pragma: no cover
            raise

    # ------------------------------------------------------------------
    # fault injection
    # ------------------------------------------------------------------

    def drop_link(self, reason: str = "simulated cable pull") -> None:
        """Kill the link the way a yanked USB cable would."""
        self.link_up = False
        self.output.put_nowait(None)
        if self._on_disconnect is not None:
            self._on_disconnect(reason)

    def raise_alarm(self, code: int = 1) -> None:
        """Force an ALARM, discarding queued motion (as the firmware does)."""
        self.alarm_code = code
        self.state = SimState.ALARM
        self._blocks.clear()
        self._deferred.clear()
        self._dwell_remaining = 0.0
        self.output.put_nowait(f"ALARM:{code}\r\n".encode())

    # ------------------------------------------------------------------
    # input
    # ------------------------------------------------------------------

    async def feed(self, data: bytes) -> None:
        """Consume bytes from the host."""
        if not self.link_up:
            return
        for byte in data:
            if byte in (RT_STATUS, RT_FEED_HOLD, RT_RESUME, RT_SOFT_RESET,
                        RT_JOG_CANCEL, RT_SAFETY_DOOR):
                await self._realtime(byte)
                continue
            if byte in (0x0A, 0x0D):
                line = self._buffer.decode("ascii", "replace")
                self._buffer.clear()
                if line.strip():
                    await self._accept_line(line.strip())
                continue
            self._buffer.append(byte)
            if len(self._buffer) > 512:
                self._buffer.clear()
                await self._emit("error:6")     # line too long

    async def _realtime(self, byte: int) -> None:
        if byte == RT_STATUS:
            await self._emit(self._status_report())
        elif byte == RT_FEED_HOLD:
            if self.state in (SimState.RUN, SimState.JOG):
                self.state = SimState.HOLD
        elif byte == RT_RESUME:
            if self.state == SimState.HOLD:
                self.state = SimState.RUN if self._blocks else SimState.IDLE
            elif self.state == SimState.DOOR:
                self.state = SimState.IDLE
        elif byte == RT_SOFT_RESET:
            self._blocks.clear()
            self._deferred.clear()
            self._dwell_remaining = 0.0
            self._pending_oks = 0
            self._rx_pending.clear()
            self.state = SimState.IDLE if self.alarm_code is None else SimState.ALARM
            await self._emit("\r\nGrbl 3.7 [FluidNC v3.7.18 (wbbot-simulator)]")
        elif byte == RT_JOG_CANCEL:
            # Cancel jog motion only; a running job is untouched. This is the
            # behaviour the live-drawing session depends on.
            if any(b.is_jog for b in self._blocks):
                self._blocks = [b for b in self._blocks if not b.is_jog]
                if not self._blocks:
                    self.state = SimState.IDLE
        elif byte == RT_SAFETY_DOOR:
            self.state = SimState.DOOR
            self._blocks.clear()

    async def _accept_line(self, line: str) -> None:
        self.received_lines.append(line)
        self._lines_seen += 1

        if self.fail_after_lines is not None and self._lines_seen > self.fail_after_lines:
            self.drop_link("simulated link failure after "
                           f"{self.fail_after_lines} lines")
            return
        if (self.alarm_after_lines is not None
                and self._lines_seen == self.alarm_after_lines):
            self.raise_alarm(self.alarm_code_to_raise)
            return
        if (self.error_on_line_containing
                and self.error_on_line_containing in line):
            self.last_error = self.error_code_to_raise
            await self._emit(f"error:{self.error_code_to_raise}")
            return

        stripped = line.split(";", 1)[0].strip()
        if not stripped:
            await self._emit("ok")
            return

        # -- system commands ------------------------------------------------
        if stripped.startswith("$"):
            await self._system_command(stripped)
            return

        if self.state == SimState.ALARM:
            await self._emit("error:9")     # locked out during alarm
            return

        try:
            block = self._parse_gcode(stripped)
        except ValueError as exc:
            self.last_error = int(str(exc)) if str(exc).isdigit() else 20
            await self._emit(f"error:{self.last_error}")
            return

        if block is None:
            await self._emit("ok")
            return

        # Anything already waiting must go first — if a new block could slip
        # into a free planner slot ahead of a deferred one, the program would
        # execute out of order (a pen lift overtaking the strokes before it).
        if self._deferred or len(self._blocks) >= self.planner_size:
            if self.reject_when_planner_full:
                await self._emit("error:8")
                return
            # GRBL semantics: withhold the ok until a slot frees up. This is
            # exactly what the character-counting streamer must cope with.
            self._rx_pending.append(stripped)
            self._queue_when_free(block)
            return

        self._enqueue(block)
        await self._emit("ok")

    def _queue_when_free(self, block: SimBlock) -> None:
        self._deferred.append(block)

    async def _system_command(self, cmd: str) -> None:
        upper = cmd.upper()
        if upper == "$X":
            self.alarm_code = None
            if self.state == SimState.ALARM:
                self.state = SimState.IDLE
            await self._emit("[MSG:Caution: Unlocked]")
            await self._emit("ok")
        elif upper == "$H":
            self.alarm_code = None
            self.state = SimState.IDLE
            self.position[0] = self.cfg.motion.park_position[0]
            self.position[1] = self.cfg.motion.park_position[1]
            await self._emit("ok")
        elif upper.startswith("$J="):
            if self.state == SimState.ALARM:
                await self._emit("error:9")
                return
            try:
                block = self._parse_gcode(cmd[3:], jog=True)
            except ValueError:
                await self._emit("error:20")
                return
            if block is None:
                await self._emit("ok")
                return
            if self._deferred or len(self._blocks) >= self.planner_size:
                self._rx_pending.append(cmd)
                self._queue_when_free(block)
                return
            self._enqueue(block)
            await self._emit("ok")
        elif upper in ("$$", "$I", "$G", "$#"):
            await self._emit("[MSG:simulator]")
            await self._emit("ok")
        else:
            await self._emit("ok")

    # ------------------------------------------------------------------
    # G-code parsing
    # ------------------------------------------------------------------

    _AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2, "A": 3}

    @property
    def _parser_pos(self) -> np.ndarray:
        """Modal position *as the parser sees it*.

        Real firmware keeps the G-code parser's idea of "where we are" ahead of
        the executed position — otherwise every word omitted from a line would
        be filled in from a stale, still-moving position.  Getting this wrong
        made queued ``G1 X.. Y..`` lines silently drag the servo axes back to
        wherever they happened to be when the line was parsed.

        Order matters: deferred blocks are queued *after* everything already in
        the planner, so they — not ``_blocks[-1]`` — hold the newest parser
        position whenever the planner has overflowed.
        """
        if self._deferred:
            return self._deferred[-1].target
        if self._blocks:
            return self._blocks[-1].target
        return self.position

    def _parse_gcode(self, line: str, jog: bool = False) -> SimBlock | None:
        words: list[tuple[str, float]] = []
        token = ""
        for ch in line.upper() + " ":
            if ch.isspace():
                if token:
                    words.append(self._parse_word(token))
                    token = ""
            else:
                token += ch

        base = self._parser_pos.copy()
        motion: int | None = None
        target = base.copy()
        feed = self.feed_mm_min
        dwell = 0.0
        saw_axis = False
        rapid = False

        for letter, value in words:
            if letter == "G":
                g = int(round(value))
                if g in (0, 1):
                    motion = g
                    rapid = (g == 0)
                elif g == 4:
                    motion = 4
                elif g == 20:
                    raise ValueError("2")       # inches unsupported here
                elif g == 21:
                    pass
                elif g == 90:
                    self.absolute_mode = True
                elif g == 91:
                    self.absolute_mode = False
                elif g in (93, 94):
                    pass
                elif g == 92:
                    motion = 92
                elif g == 28:
                    motion = 28
                elif g in (53, 54, 17, 40, 49, 80):
                    pass
                else:
                    raise ValueError("20")
            elif letter == "M":
                m = int(round(value))
                if m in (0, 1, 2, 30, 3, 4, 5, 8, 9, 18, 84):
                    pass
                else:
                    raise ValueError("20")
            elif letter == "F":
                if value <= 0:
                    raise ValueError("11")
                feed = value
            elif letter in self._AXIS_INDEX:
                saw_axis = True
                idx = self._AXIS_INDEX[letter]
                target[idx] = value if self.absolute_mode else base[idx] + value
            elif letter in ("P",):
                dwell = value
            elif letter in ("S", "N", "I", "J", "K", "R", "T"):
                pass
            else:
                raise ValueError("20")

        if motion == 4:
            return SimBlock(start=base.copy(), target=base.copy(),
                            feed_mm_min=feed, dwell_s=max(dwell, 0.0))
        if motion == 92:
            self.work_offset = target - base
            return None
        if motion == 28:
            target = base.copy()
            target[0] = self.cfg.motion.park_position[0]
            target[1] = self.cfg.motion.park_position[1]
            saw_axis = True
            rapid = True

        if not saw_axis:
            self.feed_mm_min = feed
            return None

        if rapid:
            feed = max(self.cfg.motion.travel_feed_mm_min, feed)
        else:
            self.feed_mm_min = feed

        return SimBlock(start=base.copy(), target=target,
                        feed_mm_min=feed, is_jog=jog,
                        pen_down=(self._pen_is_down(base[2])
                                  and self._pen_is_down(target[2])))

    @staticmethod
    def _parse_word(token: str) -> tuple[str, float]:
        letter = token[0]
        if not letter.isalpha():
            raise ValueError("20")
        try:
            return letter, float(token[1:])
        except ValueError as exc:
            raise ValueError("20") from exc

    def _pen_is_down(self, z: float) -> bool:
        pen = self.cfg.servo.pen
        return abs(z - pen.down_pos) < abs(pen.up_pos - pen.down_pos) * 0.5

    def _color_index(self, a: float) -> int:
        positions = self.cfg.servo.revolver.positions
        return int(np.argmin([abs(a - p) for p in positions]))

    # ------------------------------------------------------------------
    # motion
    # ------------------------------------------------------------------

    def _enqueue(self, block: SimBlock) -> None:
        block.start = (self._blocks[-1].target.copy() if self._blocks
                       else self.position.copy())
        # A dwell must never turn into a move, whatever the chain says.
        if block.dwell_s > 0.0:
            block.target = block.start.copy()
        # A Z change means the pen state changes *during* this block; the pen
        # counts as down only if it is down at the start and stays down.
        block.pen_down = (self._pen_is_down(block.start[2])
                          and self._pen_is_down(block.target[2]))
        self._plan_block(block)
        self._blocks.append(block)
        if self.state in (SimState.IDLE,):
            self.state = SimState.JOG if block.is_jog else SimState.RUN

    def _plan_block(self, block: SimBlock) -> None:
        if block.dwell_s > 0.0:
            block.duration = block.dwell_s
            block.cruise_speed = 0.0
            return
        dist = block.distance
        if dist <= 1e-12:
            block.duration = 0.0
            return
        v_max = max(block.feed_mm_min, 1e-6) / 60.0
        accel = self._limits.acceleration_mm_s2

        entry = 0.0
        if self._blocks:
            prev = self._blocks[-1]
            if (prev.dwell_s <= 0.0 and prev.distance > 1e-12
                    and np.allclose(prev.target, block.start, atol=1e-6)):
                u_prev = prev.delta / prev.distance
                u_next = block.delta / dist
                cap = min(prev.cruise_speed, v_max)
                entry = junction_speed(u_prev, u_next, self._limits, cap)
                prev.exit_speed = min(prev.exit_speed or entry, entry)
        block.entry_speed = min(entry, v_max)
        block.exit_speed = 0.0

        # Trapezoid from entry to a full stop (the simulator has a one-block
        # lookahead, which is conservative but never optimistic).
        d_accel = max((v_max ** 2 - block.entry_speed ** 2) / (2 * accel), 0.0)
        d_decel = v_max ** 2 / (2 * accel)
        if d_accel + d_decel <= dist:
            t = (v_max - block.entry_speed) / accel + v_max / accel
            t += (dist - d_accel - d_decel) / v_max
            block.cruise_speed = v_max
        else:
            peak = math.sqrt(max((2 * accel * dist + block.entry_speed ** 2) / 2.0, 0.0))
            peak = min(peak, v_max)
            peak = max(peak, block.entry_speed, 1e-6)
            t = (peak - block.entry_speed) / accel + peak / accel
            block.cruise_speed = peak
        block.duration = max(t, dist / max(v_max, 1e-9))

    def _advance(self, dt: float) -> None:
        if dt <= 0.0 or not self.link_up:
            return
        self.sim_time += dt
        if self.state in (SimState.HOLD, SimState.ALARM, SimState.DOOR):
            return

        remaining = dt
        guard = 0
        while remaining > 1e-12 and self._blocks and guard < 10000:
            guard += 1
            block = self._blocks[0]
            step = min(remaining, max(block.duration - block.elapsed, 0.0))
            t0 = block.elapsed
            block.elapsed += step
            remaining -= step
            new_pos = block.position_at(block.elapsed)

            if block.pen_down and block.xy_distance > 1e-9:
                p0 = block.position_at(t0)
                self.drawn.append(DrawnSegment(
                    start=(float(p0[0]), float(p0[1])),
                    end=(float(new_pos[0]), float(new_pos[1])),
                    color_index=self._color_index(float(new_pos[3])),
                    t_start=self.sim_time - remaining - step,
                    t_end=self.sim_time - remaining,
                ))
            self.position = new_pos

            if block.elapsed >= block.duration - 1e-12:
                self.position = block.target.copy()
                self._blocks.pop(0)
                self._promote_deferred()

        if not self._blocks:
            self.state = SimState.IDLE

    def _promote_deferred(self) -> None:
        if not self._deferred:
            return
        while self._deferred and len(self._blocks) < self.planner_size:
            self._enqueue(self._deferred.pop(0))
            self._pending_oks += 1
            if self._rx_pending:
                self._rx_pending.pop(0)

    async def _flush_oks(self) -> None:
        while self._pending_oks > 0:
            self._pending_oks -= 1
            await self._emit("ok")

    # ------------------------------------------------------------------
    # output
    # ------------------------------------------------------------------

    async def _emit(self, text: str) -> None:
        if not self.link_up or self.mute:
            return
        await self.output.put((text + "\r\n").encode("ascii", "replace"))

    def _status_report(self) -> str:
        state = self.state.value
        if self.state == SimState.ALARM and self.alarm_code is not None:
            state = "Alarm"
        mpos = self.position
        speed = 0.0
        if self._blocks and self._blocks[0].duration > 0:
            speed = self._blocks[0].cruise_speed * 60.0
        planner_free = max(self.planner_size - len(self._blocks), 0)
        rx_free = self.rx_buffer
        return (f"<{state}|MPos:{mpos[0]:.3f},{mpos[1]:.3f},"
                f"{mpos[2]:.3f},{mpos[3]:.3f}"
                f"|FS:{speed:.0f},0"
                f"|Bf:{planner_free},{rx_free}"
                f"|WCO:{self.work_offset[0]:.3f},{self.work_offset[1]:.3f},"
                f"{self.work_offset[2]:.3f}>")

    # ------------------------------------------------------------------
    # inspection helpers for tests
    # ------------------------------------------------------------------

    @property
    def xy(self) -> tuple[float, float]:
        return (float(self.position[0]), float(self.position[1]))

    @property
    def pen_down(self) -> bool:
        return self._pen_is_down(float(self.position[2]))

    @property
    def current_color(self) -> int:
        return self._color_index(float(self.position[3]))

    @property
    def queue_depth(self) -> int:
        return len(self._blocks) + len(self._deferred)

    def drawn_polylines(self, tolerance: float = 1e-6) -> list[list[tuple[float, float]]]:
        """Reassemble recorded segments into continuous polylines."""
        out: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        last_color: int | None = None
        for seg in self.drawn:
            if (current and last_color == seg.color_index
                    and math.dist(current[-1], seg.start) <= tolerance):
                current.append(seg.end)
            else:
                if len(current) >= 2:
                    out.append(current)
                current = [seg.start, seg.end]
            last_color = seg.color_index
        if len(current) >= 2:
            out.append(current)
        return out

    def drawn_bounds(self) -> tuple[float, float, float, float] | None:
        if not self.drawn:
            return None
        pts = np.array([[s.start[0], s.start[1]] for s in self.drawn]
                       + [[s.end[0], s.end[1]] for s in self.drawn])
        return (float(pts[:, 0].min()), float(pts[:, 1].min()),
                float(pts[:, 0].max()), float(pts[:, 1].max()))
