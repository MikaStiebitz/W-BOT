"""In-process FluidNC v3.9.9 emulator (WallPlotter kinematics, serial protocol, planner, servos).

``FluidNCEmulator`` implements ``Link``: the host writes raw bytes and reads raw bytes, exactly as
with a serial port. Time is simulated with its own clock (``speed`` x real time), advanced by a
background thread (``realtime=True``) or explicitly via ``advance()`` (``realtime=False``,
deterministic, used by most tests).

What it models (see docs/ARCHITECTURE.md, "Schnittstelle Simulator"): boot banner, ``?`` status,
Idle/Run/Hold:0/Hold:1/Jog/Alarm, ok/error:N (ok only once a line is accepted into the 16-block
planner), a 128 byte RX buffer (excess bytes are dropped with ``[MSG:ERR: RX overflow]``), G-code
subset, ``$J``, ``$X``, ``$I``, ``$G``, ``$CD``, ``!`` ``~`` Ctrl-X 0x85, G4, servo axes with the
pulse-window clamp. The physical consequences live in ``Plant`` (``emulator.plant``).
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from typing import Mapping, Sequence

import numpy as np

from ..config import Config
from ..fluidnc.link import Link, LinkError
from ..kinematics import FirmwareModel
from ..servo import ServoModel
from .gcode import ERROR_TEXT, ERR_OVERFLOW, clean_line
from .control import ControlMixin
from .gexec import GcodeMixin
from .planner import Planner
from .plant import Horn, Plant
from .syscmd import BANNER, SysCmdMixin

log = logging.getLogger("wbot.sim")

_RT = frozenset((0x3F, 0x21, 0x7E, 0x18, 0x85))      # ? ! ~ Ctrl-X jog-cancel
DT_MAX = 0.02            # largest sim step while moving (s)
DT_MIN = 0.001
PLANT_RES_MM = 0.5       # plant is evaluated at least every this many mm of path
LINE_MAX = 255


class FluidNCEmulator(ControlMixin, GcodeMixin, SysCmdMixin, Link):
    name = "sim"
    #: real FluidNC raises ALARM:3 on Ctrl-X while a feed hold with queued motion is active
    ALARM_ON_RESET_IN_HOLD = True

    def __init__(self, cfg: Config, *, speed: float = 1.0, physical_start: Sequence[float] | None = None,
                 true_overrides: Mapping[str, object] | None = None,
                 boot_servo_deg: Mapping[str, float] | None = None, realtime: bool = True,
                 rx_buffer_bytes: int = 128, planner_blocks: int = 16, quantize_steps: bool = True,
                 report_wco: bool = True, servo_speed_factor: float = 1.0, verbose_errors: bool = False,
                 read_chunk: int | None = None):
        self.cfg = cfg
        self._speed = float(speed)
        self._manual = not realtime
        self._rx_cap = int(rx_buffer_bytes)
        self._planner_blocks = int(planner_blocks)
        self._quantize = quantize_steps
        self._report_wco = report_wco
        self._verbose = verbose_errors
        self._read_chunk = read_chunk
        self.fw = FirmwareModel(cfg.fw_anchor_left, cfg.fw_anchor_right)
        self._spm = float(cfg.steps_per_mm)
        bx, by = cfg.firmware.boot_position
        self._boot_xy = (float(bx), float(by))
        self._L0 = tuple(float(v) for v in self.fw.lengths(np.asarray(self._boot_xy)))
        start = tuple(float(v) for v in physical_start) if physical_start is not None else self._boot_xy
        self.plant = Plant(cfg, physical_start=start, motor_L0=self._L0, true_overrides=true_overrides)
        pen, rev = cfg.servos.pen, cfg.servos.revolver
        self._pen_axis, self._rev_axis = pen.axis, rev.axis
        servo_axes = {pen.axis} | ({rev.axis} if rev.enabled else set())
        self._axes_letters = "XY" + "".join(sorted(servo_axes))
        self.n_axes = 4 if "A" in servo_axes else 3
        boot = dict(boot_servo_deg or {})
        pm, rm = ServoModel(pen), ServoModel(rev)
        self._pen_horn = Horn(pm, boot.get("pen", pm.clamp(pm.angle_for_axis(0.0))), pen.speed_deg_s * servo_speed_factor)
        self._rev_horn = Horn(rm, boot.get("revolver", rm.clamp(rm.angle_for_axis(0.0))), rev.speed_deg_s * servo_speed_factor)
        # threading
        self._lock = threading.RLock()
        self._out = bytearray()
        self._out_cv = threading.Condition()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._t = 0.0
        self._last_real = time.monotonic()
        self._open = False
        self._dropped = False
        self._paused = False
        self._booted = False
        self._inject: deque[int] = deque()
        self.stats = {"ok": 0, "errors": 0, "rx_overflows": 0, "max_planner_depth": 0}
        self._ring: deque[int] = deque()
        self._linebuf = bytearray()
        self._init_runtime()

    # ------------------------------------------------------------------ boot / reset
    def _init_runtime(self) -> None:
        self.planner = Planner((self._L0[0], self._L0[1], 0.0, 0.0), self._planner_blocks,
                               self.cfg.motion.junction_deviation_mm)
        self._pos = [self._boot_xy[0], self._boot_xy[1], 0.0, 0.0]
        self._reset_modal()
        self._pending = None
        self._ring.clear()
        self._linebuf.clear()
        self._last_cr = False
        self._line_over = False
        self._alarm: int | None = None
        self._hold: int | None = None
        self._jog_cancelling = False
        self._wco_count = 0
        self._status_mask = 1
        self._motor_L = self._L0
        self._axes = [0.0, 0.0]
        self._xy_cache: tuple | None = None
        self._last_sig: tuple | None = None
        self._pen_horn.command_axis(0.0)
        self._rev_horn.command_axis(0.0)

    def _reset_modal(self) -> None:
        self._m_motion, self._m_abs, self._m_scale, self._m_feed = 0, True, 1.0, 0.0
        self._offset = [0.0] * 4

    def _boot_messages(self) -> None:
        self._emit("[MSG:INFO: FluidNC v3.9.9 (simulated)]", "[MSG:INFO: Machine wbot simulator]",
                   "[MSG:INFO: Kinematic system: WallPlotter]", f"[MSG:INFO: Axis count {self.n_axes}]", BANNER)

    # ------------------------------------------------------------------ Link
    def open(self) -> None:
        with self._lock:
            if self._open and not self._dropped:
                return
            if self._booted:          # re-open = DTR reset: firmware reboots, the gondola stays
                self.planner.flush()
                self._init_runtime()
                self.plant.rebase(self.plant.pos, self._L0)
            self._booted = True
            self._open, self._dropped, self._paused = True, False, False
            with self._out_cv:
                self._out.clear()
            self._last_real = time.monotonic()
            self._boot_messages()
        if not self._manual and (self._thread is None or not self._thread.is_alive()):
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="fluidnc-sim", daemon=True)
            self._thread.start()

    def close(self) -> None:
        with self._lock:
            self._open = False
        self._stop.set()
        self._wake.set()
        th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(2.0)
        self._thread = None
        with self._out_cv:
            self._out_cv.notify_all()

    @property
    def is_open(self) -> bool:
        return self._open and not self._dropped

    def __enter__(self) -> "FluidNCEmulator":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _check_io(self) -> None:
        if not self._open:
            raise LinkError("sim link is closed")
        if self._dropped:
            raise LinkError("sim link lost")

    def write(self, data: bytes) -> None:
        if isinstance(data, str):
            data = data.encode("latin-1")
        with self._lock:
            self._check_io()
            self._tick()
            overflow = 0
            for b in data:
                if b in _RT:
                    self._pump()
                    self._realtime(b)
                elif b >= 0x80:
                    continue
                elif len(self._ring) >= self._rx_cap:
                    overflow += 1
                else:
                    self._ring.append(b)
                    self._pump()
            if overflow:
                self.stats["rx_overflows"] += overflow
                log.warning("sim: RX overflow, %d bytes dropped", overflow)
                self._emit("[MSG:ERR: RX overflow]")
        self._wake.set()

    def read(self, timeout: float = 0.05) -> bytes:
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._out_cv:
            while True:
                self._check_io()
                if self._out and not self._paused:
                    n = len(self._out) if not self._read_chunk else self._read_chunk
                    data = bytes(self._out[:n])
                    del self._out[:n]
                    return data
                left = deadline - time.monotonic()
                if self._manual or left <= 0:
                    return b""
                self._out_cv.wait(left)

    # ------------------------------------------------------------------ clock
    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                try:
                    self._tick()
                except Exception:                          # keep the machine alive, show the bug
                    log.exception("sim: step failed")
                active = self._active()
            if active:
                time.sleep(0.001)
            else:
                self._wake.wait(0.02)
                self._wake.clear()

    def _tick(self) -> None:
        if self._manual:
            return
        now = time.monotonic()
        dt = min(now - self._last_real, 0.5) * self._speed
        self._last_real = now
        if dt > 0:
            self._advance(dt)

    def _active(self) -> bool:
        p = self._pending
        if p is not None and p.kind == "dwell" and p.stage == 1:
            return True
        if self._pen_horn.moving or self._rev_horn.moving:
            return True
        q = self.planner
        return bool(q.q) and not (q.stopping and q.v <= 1e-12)

    def _advance(self, dt: float) -> None:
        remaining = dt
        while remaining > 1e-12:
            step = remaining
            if self._active():
                q = self.planner.q
                step = min(step, min(DT_MAX, max(DT_MIN, PLANT_RES_MM / q[0].feed)) if q else DT_MAX)
            p = self._pending
            if p is not None and p.kind == "dwell" and p.stage == 1:
                step = min(step, max(p.end - self._t, 1e-9))
            step = max(step, 1e-9)
            self._t += step
            self._step(step)
            remaining -= step

    def _step(self, dt: float) -> None:
        pl = self.planner
        if pl.q:
            pl.advance(dt)
        if pl.stopping and pl.v <= 1e-12:
            if self._jog_cancelling:
                pl.flush()
                self._jog_cancelling = False
                self._resync_pos()
            elif self._hold == 1:
                self._hold = 0
        self._sync_outputs(dt)
        if self._pending is not None or self._ring:
            self._pump()

    def _sync_outputs(self, dt: float) -> None:
        lL, lR, z, a = self.planner.cur
        if self._quantize:
            sl = math.floor((lL - self._L0[0]) * self._spm + 0.5)
            sr = math.floor((lR - self._L0[1]) * self._spm + 0.5)
            lL, lR = self._L0[0] + sl / self._spm, self._L0[1] + sr / self._spm
        self._motor_L, self._axes = (lL, lR), [z, a]
        pen_ax, rev_ax = (z if self._pen_axis == "Z" else a), (z if self._rev_axis == "Z" else a)
        self._pen_horn.command_axis(pen_ax)
        if self.cfg.servos.revolver.enabled:
            self._rev_horn.command_axis(rev_ax)
        self._pen_horn.step(dt)
        self._rev_horn.step(dt)
        sig = (lL, lR, self._pen_horn.angle, self._rev_horn.angle)
        if sig != self._last_sig:
            self._last_sig = sig
            self.plant.update(self._t, self._motor_L, self._pen_horn.angle, self._rev_horn.angle)

    # ------------------------------------------------------------------ serial side
    def _emit(self, *lines: str) -> None:
        data = "".join(f"{ln}\r\n" for ln in lines).encode("latin-1", "replace")
        with self._out_cv:
            self._out.extend(data)
            self._out_cv.notify_all()

    def _ok(self) -> None:
        self.stats["ok"] += 1
        self._emit("ok")

    def _error(self, code: int) -> None:
        self.stats["errors"] += 1
        self._emit(f"error:{code} ({ERROR_TEXT.get(code, 'Error')})" if self._verbose else f"error:{code}")

    def _pump(self) -> None:
        """Consume RX bytes into lines and execute them until something blocks."""
        while True:
            if self._pending is not None and not self._resolve_pending():
                return
            if not self._take_line():
                return

    def _take_line(self) -> bool:
        ring = self._ring
        while ring:
            b = ring.popleft()
            if b == 10 and self._last_cr:
                self._last_cr = False
                continue
            self._last_cr = b == 13
            if b in (10, 13):
                line = self._linebuf.decode("latin-1")
                self._linebuf.clear()
                over, self._line_over = self._line_over, False
                self._exec_line(line, over)
                return True
            if len(self._linebuf) < LINE_MAX:
                self._linebuf.append(b)
            else:
                self._line_over = True
        return False

    def _exec_line(self, line: str, over: bool) -> None:
        if over:
            self._error(ERR_OVERFLOW)
            return
        s = line.strip()
        if s.startswith("$"):
            if self._inject:
                self._error(self._inject.popleft())
            else:
                self._sys_command(s)
            return
        text = clean_line(s)
        if text in ("", "%"):
            self._ok()
            return
        if self._inject:
            self._error(self._inject.popleft())
            return
        self._gcode(text)
        self.stats["max_planner_depth"] = max(self.stats["max_planner_depth"], len(self.planner.q))
