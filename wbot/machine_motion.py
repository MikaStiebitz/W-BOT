"""Safe motion and servo commands for :class:`wbot.machine.Machine`."""
from __future__ import annotations

import logging

import numpy as np

from .errors import MachineError
from .servo import ServoLimitError, ServoModel

log = logging.getLogger(__name__)


class MotionMixin:
    # ------------------------------------------------------------------ motion
    def _fmt_feed(self, feed: float | None, default: float) -> float:
        f = float(feed or default)
        if not 10 <= f <= max(self.cfg.firmware.max_rate_mm_min, 1.0) * 1.0001:
            raise MachineError(f"Feed rate {f:.0f} mm/min outside 10..{self.cfg.firmware.max_rate_mm_min:.0f}.",
                               "bad_feed", 400)
        return f

    def _move_lines(self, points: np.ndarray, feed: float) -> list[str]:
        mp = self._need_reference()
        m = self.cfg.motion
        fw = mp.path_to_firmware(points, m.predistort_tolerance_mm, m.max_chord_mm, m.min_chord_mm)
        lines = [f"G1 X{p[0]:.3f} Y{p[1]:.3f} F{feed:.0f}" if i == 0 else f"G1 X{p[0]:.3f} Y{p[1]:.3f}"
                 for i, p in enumerate(fw[1:])]
        return ["G21", "G90"] + lines

    def _check_target(self, x: float, y: float) -> None:
        xmin, ymin, xmax, ymax = self.cfg.safe_rect()
        if not (xmin <= x <= xmax and ymin <= y <= ymax):
            raise MachineError(
                f"Target ({x:.1f}, {y:.1f}) mm is outside the permitted area "
                f"({xmin:.0f}..{xmax:.0f} x {ymin:.0f}..{ymax:.0f} mm).", "out_of_bounds", 422)

    def goto(self, x: float, y: float, feed: float | None = None, pen: str | None = "up") -> dict:
        with self._cmd():
            self._ready_for_motion()
            self._need_reference()
            self._check_target(x, y)
            cur = self.phys_xy()
            if cur is None:
                raise MachineError("Current position unknown.", "no_position")
            if pen != "keep" and (self.pen_up() is not True or not self.pen_known):
                self._servo_move_locked("pen", self.cfg.servos.pen.up_deg)
            f = self._fmt_feed(feed, self.cfg.motion.travel_feed_mm_min)
            self._send_lines(self._move_lines(np.array([cur, (x, y)]), f))
        return {"ok": True}

    def jog(self, dx: float, dy: float, feed: float | None = None, pen: str | None = None) -> dict:
        with self._cmd():
            self._ready_for_motion()
            if abs(dx) > 500 or abs(dy) > 500:
                raise MachineError("Jog step too large (max. 500 mm).", "bad_step", 400)
            f = self._fmt_feed(feed, self.cfg.motion.jog_feed_mm_min)
            if self.mapping is None:
                raise MachineError("Jog is locked without a reference - confirm the gondola position first.",
                                   "no_reference")
            cur = self.phys_xy()
            if cur is None:
                raise MachineError("Current position unknown.", "no_position")
            tx, ty = cur[0] + dx, cur[1] + dy
            xmin, ymin, xmax, ymax = self.cfg.safe_rect()
            want = (tx, ty)
            tx, ty = min(max(tx, xmin), xmax), min(max(ty, ymin), ymax)   # stop at the edge
            if np.hypot(tx - cur[0], ty - cur[1]) < 1e-6:
                raise MachineError("At the edge of the permitted area.", "out_of_bounds", 422)
            # Pen position unknown after power-up/reset (Z sits at 0 = "down"): lift first, never scribble.
            if pen == "up" or (pen is None and not self.pen_known):
                self._servo_move_locked("pen", self.cfg.servos.pen.up_deg)
            self._send_lines(self._move_lines(np.array([cur, (tx, ty)]), f))
        clamped = abs(want[0] - tx) > 1e-6 or abs(want[1] - ty) > 1e-6
        out = {"ok": True, "target": [tx, ty], "clamped": clamped}
        if clamped:
            out["warning"] = f"Step shortened at the edge of the permitted area (target {tx:.1f} | {ty:.1f} mm)."
        return out

    def park(self) -> dict:
        x, y = self.cfg.motion.park_position
        return self.goto(x, y, None, "up")

    # ------------------------------------------------------------------- servos
    def _servo_lines(self, which: str, angle: float) -> list[str]:
        spec = self.cfg.servos.pen if which == "pen" else self.cfg.servos.revolver
        model = ServoModel(spec)
        try:
            value = model.axis_value(angle)
        except ServoLimitError as exc:
            raise MachineError(str(exc), "servo_limit", 422) from exc
        prev = self.pen_angle() if which == "pen" else self.revolver_angle()
        wait = model.travel_time_s(prev, angle)
        return [f"G1 {spec.axis}{value:.3f} F{spec.speed_deg_s * 60:.0f}", f"G4 P{wait:.2f}"]

    def _servo_move_locked(self, which: str, angle: float) -> None:
        if which == "revolver":
            if not self.cfg.servos.revolver.enabled:
                raise MachineError("The revolver servo is disabled in the config.", "no_revolver", 422)
            if self.pen_up() is not True or not self.pen_known:  # pen first, never both at once
                self._send_lines(self._servo_lines("pen", self.cfg.servos.pen.up_deg))
                self.pen_known = True
        self._send_lines(self._servo_lines(which, angle))
        if which == "pen":
            self.pen_known = True

    def set_pen(self, state: str | None = None, angle: float | None = None) -> dict:
        pen = self.cfg.servos.pen
        if angle is None:
            if state not in ("up", "down"):
                raise MachineError("state must be 'up' or 'down'.", "bad_request", 400)
            angle = pen.up_deg if state == "up" else pen.down_deg
        with self._cmd():
            self._ready_for_motion()
            self._servo_move_locked("pen", float(angle))
        return self.state()

    def set_revolver(self, slot: int | None = None, angle: float | None = None) -> dict:
        slots = self.cfg.servos.revolver.slots_deg
        if angle is None:
            if slot is None or not 1 <= slot <= len(slots):
                raise MachineError(f"slot must be 1..{len(slots)}.", "bad_request", 400)
            angle = slots[slot - 1]
        with self._cmd():
            self._ready_for_motion()
            self._servo_move_locked("revolver", float(angle))
        return self.state()

    def set_servo_raw(self, servo: str, angle: float) -> dict:
        if servo not in ("pen", "revolver"):
            raise MachineError("servo must be 'pen' or 'revolver'.", "bad_request", 400)
        with self._cmd():
            self._ready_for_motion()
            self._servo_move_locked(servo, float(angle))
        return self.state()
