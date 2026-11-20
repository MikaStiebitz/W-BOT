"""Control surface of the emulator: realtime commands, fault injection, observers, status report.

Mixin of ``FluidNCEmulator`` (uses its attributes: planner, plant, horns, _lock, ...).
"""
from __future__ import annotations

import logging
import time

import numpy as np

from ..kinematics import KinematicsError
from .plant import TraceSeg
from .syscmd import BANNER

log = logging.getLogger("wbot.sim")


def _f3(v: float) -> str:
    """Three decimals, never "-0.000"."""
    return f"{round(v, 3) + 0.0:.3f}"


class ControlMixin:
    # ------------------------------------------------------------------ fault injection
    def inject_alarm(self, code: int = 1) -> None:
        """Hard alarm (e.g. limit): motion stops dead, ``ALARM:code``, locked until ``$X``."""
        with self._lock:
            self._tick()
            self.planner.flush()
            self._pending = None
            self._hold, self._jog_cancelling = None, False
            self._resync_pos()
            self._alarm = int(code)
            self._emit(f"ALARM:{int(code)}")
        self._wake.set()

    def inject_error(self, code: int, count: int = 1) -> None:
        """The next ``count`` non-empty lines are answered with ``error:code`` instead of running."""
        with self._lock:
            self._inject.extend([int(code)] * count)

    def drop_link(self) -> None:
        """Simulate USB loss: read/write raise LinkError. The machine keeps running."""
        with self._lock:
            self._dropped = True
        with self._out_cv:
            self._out_cv.notify_all()

    def restore_link(self) -> None:
        with self._lock:
            self._dropped = False

    def pause_output(self) -> None:
        with self._out_cv:
            self._paused = True

    def resume_output(self) -> None:
        with self._out_cv:
            self._paused = False
            self._out_cv.notify_all()

    # ------------------------------------------------------------------ observers
    @property
    def trace(self) -> list[TraceSeg]:
        """Live pen trace (physical mm). Append-only until ``reset_trace()``."""
        return self.plant.trace

    def reset_trace(self) -> None:
        self.plant.reset_trace()

    def physical_position(self) -> tuple[float, float]:
        return self.plant.pos

    def firmware_position(self) -> tuple[float, float]:
        with self._lock:
            return self._fw_xy()

    def servo_angles(self) -> dict[str, float]:
        """Physical horn angles (deg) - after the slew, not the commanded value."""
        with self._lock:
            return {"pen": self._pen_horn.angle, "revolver": self._rev_horn.angle}

    def axis_values(self) -> dict[str, float]:
        """Firmware axis values X, Y (firmware coords) and the servo axes."""
        with self._lock:
            x, y = self._fw_xy()
            return {"X": x, "Y": y, "Z": self._axes[0], "A": self._axes[1]}

    @property
    def machine_state(self) -> str:
        with self._lock:
            return self._state_str()

    @property
    def sim_time(self) -> float:
        return self._t

    @property
    def speed(self) -> float:
        return self._speed

    @speed.setter
    def speed(self, value: float) -> None:
        with self._lock:
            self._tick()
            self._speed = float(value)

    @property
    def rx_overflows(self) -> int:
        return self.stats["rx_overflows"]

    def snapshot(self) -> dict:
        with self._lock:
            fx, fy = self._fw_xy()
            return {"sim_time": self._t, "state": self._state_str(), "alarm": self._alarm,
                    "firmware": (fx, fy), "physical": self.plant.pos, "axes": {"Z": self._axes[0], "A": self._axes[1]},
                    "servo_deg": {"pen": self._pen_horn.angle, "revolver": self._rev_horn.angle},
                    "pen_down": self.plant.pen_down(self._pen_horn.angle),
                    "planner_free": self.planner.free, "trace_len": len(self.plant.trace),
                    "stats": dict(self.stats)}

    def settled(self) -> bool:
        with self._lock:
            return (not self.planner.q and self._pending is None and not self._ring
                    and not self._pen_horn.moving and not self._rev_horn.moving)

    def advance(self, dt: float) -> None:
        """Manual mode: advance the simulation by ``dt`` sim seconds."""
        with self._lock:
            self._advance(dt)

    def run_until_idle(self, max_sim_s: float = 3600.0, chunk: float = 0.05) -> float:
        """Manual mode: advance until everything is done; returns the sim seconds used."""
        used = 0.0
        while used < max_sim_s and not self.settled():
            self.advance(chunk)
            used += chunk
        return used

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """Real-time mode: block (real seconds) until settled; manual mode: run_until_idle."""
        if self._manual:
            return self.run_until_idle(timeout) < timeout
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.settled():
                return True
            time.sleep(0.002)
        return False

    # ------------------------------------------------------------------ realtime commands
    def _realtime(self, b: int) -> None:
        if b == 0x3F:
            self._emit(self._status_line())
        elif b == 0x18:
            self._soft_reset()
        elif b == 0x21:
            self._feed_hold()
        elif b == 0x7E:
            if self._hold == 0:
                self._hold = None
                self.planner.stopping = False
                self.planner.replan()
        elif b == 0x85:
            self._jog_cancel()

    def _feed_hold(self) -> None:
        pl = self.planner
        if self._alarm is not None or self._hold is not None:
            return
        if pl.q and pl.q[0].jog:
            self._jog_cancel()
            return
        pl.stopping = True
        self._hold = 1 if (pl.q and pl.v > 1e-12) else 0

    def _jog_cancel(self) -> None:
        pl = self.planner
        if self._alarm is not None or not (pl.q and pl.q[0].jog):
            return
        pl.stopping = True
        self._jog_cancelling = True
        if pl.v <= 1e-12:
            pl.flush()
            self._jog_cancelling = False
            self._resync_pos()

    def _soft_reset(self) -> None:
        st = self._state_str()
        pl = self.planner
        moving = st in ("Run", "Jog") or (st.startswith("Hold") and self.ALARM_ON_RESET_IN_HOLD and bool(pl.q))
        pl.flush()
        self._pending = None
        self._ring.clear()
        self._linebuf.clear()
        self._last_cr = self._line_over = False
        self._hold, self._jog_cancelling = None, False
        self._resync_pos()
        self._reset_modal()
        self._wco_count = 0
        if moving:
            self._alarm = 3
            self._emit("ALARM:3")
        self._emit(BANNER)
        if self._alarm is not None:
            self._emit("[MSG:'$H'|'$X' to unlock]")

    # ------------------------------------------------------------------ state / status
    def _state_str(self) -> str:
        if self._alarm is not None:
            return "Alarm"
        if self._hold is not None:
            return f"Hold:{self._hold}"
        if self.planner.q:
            return "Jog" if self.planner.q[0].jog else "Run"
        return "Idle"

    def _fw_xy(self) -> tuple[float, float]:
        key = self._motor_L
        if self._xy_cache is None or self._xy_cache[0] != key:
            try:
                p = self.fw.position(np.asarray(key))
                self._xy_cache = (key, (float(p[0]), float(p[1])))
            except KinematicsError:
                if self._xy_cache is None:
                    return self._boot_xy
        return self._xy_cache[1]

    def _resync_pos(self) -> None:
        x, y = self._fw_xy()
        self._pos = [x, y, self._axes[0], self._axes[1]]

    def _status_line(self) -> str:
        x, y = self._fw_xy()
        mpos = [x, y, self._axes[0], self._axes[1]][: self.n_axes]
        state = self._state_str()
        parts = [state]
        if self._status_mask & 1:
            parts.append("MPos:" + ",".join(_f3(v) for v in mpos))
        else:
            parts.append("WPos:" + ",".join(_f3(v - o) for v, o in zip(mpos, self._offset)))
        feed = self.planner.v * 60.0 if self.planner.q else 0.0
        parts.append(f"FS:{int(round(feed))},0")
        if self._status_mask & 2:
            parts.append(f"Bf:{self.planner.free},{max(self._rx_cap - 1 - len(self._ring), 0)}")
        if self._report_wco:
            if self._wco_count <= 0:
                parts.append("WCO:" + ",".join(_f3(v) for v in self._offset[: self.n_axes]))
                self._wco_count = 10 if state in ("Run", "Jog") or state.startswith("Hold") else 30
            self._wco_count -= 1
        return "<" + "|".join(parts) + ">"
