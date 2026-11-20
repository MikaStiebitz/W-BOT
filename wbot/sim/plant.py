"""The physical side of the simulator: gondola on belts ("truth") plus servo horns.

The emulator (what the FluidNC firmware does) only knows *motor* belt lengths.
The plant turns those into where the pen really is, using ``TrueModel`` (tangent +
wrap, belts attached at two points of the tilting gondola, ``wbot/gondola.py``). ``true_overrides`` perturbs the truth (anchor offsets, pulley radius, belt
scales, pen offset) so tests can prove calibration works - or fails.

Motor belt length -> true belt length::

    L_true = L_true(physical_start) + (motor_L - motor_L0) * belt_scale

and ``TrueModel.position`` solves the forward kinematics. Every position change is
recorded as a ``TraceSeg`` in *physical* mm, together with pen state and colour.
"""
from __future__ import annotations

import logging
import math
import threading
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from ..config import Config
from .. import gondola
from ..kinematics import KinematicsError, TrueModel, true_model_from_config
from ..servo import ServoModel

log = logging.getLogger("wbot.sim")

XY = tuple[float, float]


@dataclass(slots=True)
class TraceSeg:
    """One piece of the recorded pen trace (physical mm). ``t`` = sim time at the segment start."""
    t: float
    x0: float
    y0: float
    x1: float
    y1: float
    pen_down: bool
    color_slot: int

    def as_list(self) -> list:
        return [self.x0, self.y0, self.x1, self.y1, self.pen_down, self.color_slot]


_OVERRIDE_KEYS = {
    "anchor_left", "anchor_right",                  # absolute (x, y)
    "anchor_left_dx", "anchor_left_dy", "anchor_right_dx", "anchor_right_dy",
    "anchor_dx", "anchor_dy",                       # alias: shift of the RIGHT anchor (span / height error)
    "radius", "scale_left", "scale_right",
    "pen_dx", "pen_dy", "pen_offset",
    "attach_half_spacing", "attach_height", "com_height", "com_x", "tilt",   # gondola V2 (rigid body on two belts)
}


def build_truth(cfg: Config, overrides: Mapping[str, object] | None) -> tuple[TrueModel, tuple[float, float]]:
    """Truth model + belt scales: ``cfg.geometry`` perturbed by ``overrides`` (None values are ignored)."""
    ov = {k: v for k, v in (overrides or {}).items() if v is not None}
    unknown = set(ov) - _OVERRIDE_KEYS
    if unknown:
        raise ValueError(f"unknown true_overrides: {sorted(unknown)}")
    g = cfg.geometry
    al = list(ov.get("anchor_left", g.anchor_left))
    ar = list(ov.get("anchor_right", g.anchor_right))
    al[0] += float(ov.get("anchor_left_dx", 0.0))
    al[1] += float(ov.get("anchor_left_dy", 0.0))
    ar[0] += float(ov.get("anchor_right_dx", 0.0)) + float(ov.get("anchor_dx", 0.0))
    ar[1] += float(ov.get("anchor_right_dy", 0.0)) + float(ov.get("anchor_dy", 0.0))
    pen = list(ov.get("pen_offset", g.pen_offset_mm))
    pen[0] += float(ov.get("pen_dx", 0.0))
    pen[1] += float(ov.get("pen_dy", 0.0))
    scale = (float(ov.get("scale_left", g.belt_scale_left)), float(ov.get("scale_right", g.belt_scale_right)))
    gond = {k: ov[k] for k in ("attach_half_spacing", "attach_height", "com_height", "com_x", "tilt") if k in ov}
    truth = true_model_from_config(cfg, anchor_left=(al[0], al[1]), anchor_right=(ar[0], ar[1]),
                                   radius=ov.get("radius"), pen_offset=(pen[0], pen[1]), **gond)
    return truth, scale


class Horn:
    """A servo horn: follows the commanded angle with a limited slew rate (no feedback to the host)."""

    __slots__ = ("model", "angle", "target", "speed")

    def __init__(self, model: ServoModel, angle: float, speed_deg_s: float):
        self.model = model
        self.angle = float(angle)
        self.target = float(angle)
        self.speed = float(speed_deg_s)

    def command_axis(self, axis_value: float) -> None:
        """FluidNC axis value -> pulse (clamped to the firmware window) -> target angle."""
        a = self.model.angle_for_axis(axis_value)
        self.target = min(max(a, self.model.min_deg), self.model.max_deg)

    def step(self, dt: float) -> bool:
        """Advance ``dt`` sim seconds; True while still moving."""
        d = self.target - self.angle
        if abs(d) <= 1e-9:
            return False
        m = self.speed * dt
        if abs(d) <= m:
            self.angle = self.target
            return False
        self.angle += math.copysign(m, d)
        return True

    @property
    def moving(self) -> bool:
        return abs(self.target - self.angle) > 1e-9


class Plant:
    """True gondola position from motor belt lengths, with a pen trace."""

    def __init__(self, cfg: Config, *, physical_start: XY, motor_L0: Sequence[float],
                 true_overrides: Mapping[str, object] | None = None, max_trace: int = 1_000_000):
        self.cfg = cfg
        self.truth, self.scale = build_truth(cfg, true_overrides)
        self._scale = np.asarray(self.scale, float)
        self.pen_model = ServoModel(cfg.servos.pen)
        self._slots = list(cfg.servos.revolver.slots_deg)
        self.max_trace = max_trace
        self.lock = threading.RLock()
        self.trace: list[TraceSeg] = []
        self.fault: str | None = None
        self._was_down: bool | None = None     # None until the first update (no dot at boot)
        self._full_warned = False
        self.rebase(physical_start, motor_L0)

    # --- reference ---------------------------------------------------------
    def rebase(self, physical: XY, motor_L0: Sequence[float]) -> None:
        """The motors now read ``motor_L0`` while the gondola physically is at ``physical``
        (firmware reboot: position lost, gondola did not move)."""
        with self.lock:
            self._lt0 = np.asarray(self.truth.lengths(np.asarray(physical, float)), float)
            self._ml0 = np.asarray(motor_L0, float)
            self.pos: XY = (float(physical[0]), float(physical[1]))
            self._th = float(self.truth.tilt_angle(np.asarray(physical, float)))   # warm start of the tilt solve
            self._last_L: tuple | None = None

    # --- model -------------------------------------------------------------
    def motor_to_physical(self, motor_L: Sequence[float]) -> XY:
        """Physical pen position for firmware motor belt lengths (left, right)."""
        lt = self._lt0 + (np.asarray(motor_L, float) - self._ml0) * self._scale
        try:
            return self._solve(float(lt[0]), float(lt[1]), self.pos)
        except KinematicsError:
            p = self.truth.position(lt, seed=np.asarray(self.pos))     # slow, robust fallback
            return float(p[0]), float(p[1])

    def _solve(self, ll: float, lr: float, seed: XY) -> XY:
        """Scalar Newton solve of ``TrueModel`` (same maths, ~20x faster than the numpy version)."""
        t = self.truth
        if t.has_tilt:                                        # rigid gondola: (x, y, theta), warm-started
            px, py, self._th = gondola.solve_scalar(t, ll, lr, seed[0], seed[1], self._th)
            return px, py
        r = t.pulley_r
        alx, aly = t.anchor_left
        arx, ary = t.anchor_right
        (elx, ely), (erx, ery) = t.e_left, t.e_right          # attach points relative to the pen
        px, py = seed[0], seed[1]
        for _ in range(40):
            vx, vy = px + elx - alx, py + ely - aly           # left pulley
            d2 = vx * vx + vy * vy
            d = math.sqrt(d2)
            if d <= r * 1.001:
                raise KinematicsError("pen too close to a pulley")
            root = math.sqrt(d2 - r * r)
            l1 = root + r * (math.pi - math.atan2(vy, vx) - math.acos(r / d))
            a1x, a1y = (root / d) * (vx / d) + r * vy / d2, (root / d) * (vy / d) - r * vx / d2
            wx, wy = -(px + erx - arx), py + ery - ary        # right pulley = mirror image
            e2 = wx * wx + wy * wy
            e = math.sqrt(e2)
            if e <= r * 1.001:
                raise KinematicsError("pen too close to a pulley")
            root2 = math.sqrt(e2 - r * r)
            l2 = root2 + r * (math.pi - math.atan2(wy, wx) - math.acos(r / e))
            a2x, a2y = -((root2 / e) * (wx / e) + r * wy / e2), (root2 / e) * (wy / e) - r * wx / e2
            r1, r2 = l1 - ll, l2 - lr
            if abs(r1) < 1e-9 and abs(r2) < 1e-9:
                return px, py
            det = a1x * a2y - a1y * a2x
            if abs(det) < 1e-12:
                raise KinematicsError("singular belt geometry")
            dx, dy = (r1 * a2y - a1y * r2) / det, (a1x * r2 - a2x * r1) / det
            n = math.hypot(dx, dy)
            if n > 50.0:
                dx, dy = dx * 50.0 / n, dy * 50.0 / n
            px, py = px - dx, py - dy
        raise KinematicsError("true-model forward kinematics did not converge")

    def pen_down(self, angle_deg: float) -> bool:
        s = self.cfg.servos.pen
        # strictly closer to down; the neutral centre (power-up, equidistant) counts as lifted
        return abs(angle_deg - s.down_deg) < abs(angle_deg - s.up_deg) - 1e-9

    def color_slot(self, angle_deg: float) -> int:
        if not self._slots:
            return 0
        return min(range(len(self._slots)), key=lambda i: abs(self._slots[i] - angle_deg))

    # --- time stepping -------------------------------------------------------
    def update(self, t: float, motor_L: Sequence[float], pen_deg: float, rev_deg: float) -> None:
        key = tuple(motor_L)
        try:
            nx, ny = self.pos if key == self._last_L else self.motor_to_physical(key)
            self._last_L = key
        except KinematicsError as exc:        # pen at a pulley: physically impossible, hold position
            if self.fault is None:
                self.fault = str(exc)
                log.warning("plant: %s - gondola held at %s", exc, self.pos)
            return
        down = self.pen_down(pen_deg)
        slot = self.color_slot(rev_deg)
        with self.lock:
            x0, y0 = self.pos
            moved = math.hypot(nx - x0, ny - y0) > 1e-7
            if moved or (down and self._was_down is False):      # a dot when the pen touches down
                if len(self.trace) < self.max_trace:
                    self.trace.append(TraceSeg(t, x0, y0, nx, ny, down, slot))
                elif not self._full_warned:
                    self._full_warned = True
                    log.warning("plant: trace full (%d segments), recording stopped", self.max_trace)
            self.pos = (nx, ny)
            self._was_down = down

    # --- trace ---------------------------------------------------------------
    def reset_trace(self) -> None:
        with self.lock:
            self.trace.clear()
            self._full_warned = False

    def trace_since(self, index: int = 0) -> list[TraceSeg]:
        with self.lock:
            return self.trace[index:]
