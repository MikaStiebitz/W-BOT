"""Trapezoidal motion planning — used for time estimates *and* the simulator.

The plotter is a pendulum on two belts, so realistic timing matters: a naive
``distance / feedrate`` estimate is wrong by a factor of two or more on the
short segments a traced photo consists of, and the simulator would replay
motion that the real machine could never follow.

The planner is the standard GRBL/FluidNC scheme:

1. Every junction gets a maximum cornering speed from the "junction deviation"
   model — how far the tool is allowed to deviate from the exact corner.
2. A backward pass caps each entry speed so the move can still decelerate to
   its exit speed.
3. A forward pass caps each exit speed so it is reachable from the entry speed.
4. Each move is then a trapezoid (or a triangle, when the cruise speed is
   never reached).

All internal units are mm and mm/s.  Feedrates coming in from G-code / config
are mm/min.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

__all__ = [
    "Move",
    "PlannedMove",
    "MotionLimits",
    "plan",
    "total_duration",
    "junction_speed",
    "DEFAULT_JUNCTION_DEVIATION_MM",
]

DEFAULT_JUNCTION_DEVIATION_MM = 0.05


@dataclass(frozen=True)
class MotionLimits:
    """Machine limits in planner units."""

    acceleration_mm_s2: float
    junction_deviation_mm: float = DEFAULT_JUNCTION_DEVIATION_MM
    min_junction_speed_mm_s: float = 0.0

    def __post_init__(self) -> None:
        if self.acceleration_mm_s2 <= 0.0:
            raise ValueError("acceleration must be > 0")
        if self.junction_deviation_mm < 0.0:
            raise ValueError("junction_deviation must be >= 0")


@dataclass(frozen=True)
class Move:
    """A straight linear move request."""

    start: tuple[float, float]
    end: tuple[float, float]
    feed_mm_min: float
    #: Free-form tag carried through to the planned result (e.g. "draw").
    tag: str = ""

    @property
    def delta(self) -> np.ndarray:
        return np.array(self.end, dtype=float) - np.array(self.start, dtype=float)

    @property
    def distance(self) -> float:
        return float(np.linalg.norm(self.delta))


@dataclass(frozen=True)
class PlannedMove:
    """A move with its resolved velocity profile."""

    move: Move
    distance: float
    entry_speed: float      # mm/s
    cruise_speed: float     # mm/s
    exit_speed: float       # mm/s
    duration: float         # s

    @property
    def tag(self) -> str:
        return self.move.tag


def _junction_speed(prev_unit: np.ndarray, next_unit: np.ndarray,
                    limits: MotionLimits, v_cap: float) -> float:
    """Maximum speed through a corner (GRBL junction-deviation model).

    ``cos_theta`` is the cosine of the angle between the *reversed* incoming
    direction and the outgoing direction: ``-1`` for a straight line,
    ``+1`` for a full reversal.
    """
    if limits.junction_deviation_mm <= 0.0:
        return 0.0
    cos_theta = float(-np.dot(prev_unit, next_unit))
    cos_theta = min(max(cos_theta, -1.0), 1.0)
    if cos_theta <= -0.999999:          # collinear, no corner at all
        return v_cap
    if cos_theta >= 0.999999:           # 180 deg reversal, must stop
        return limits.min_junction_speed_mm_s
    sin_half = math.sqrt(0.5 * (1.0 - cos_theta))
    if sin_half >= 1.0 - 1e-12:
        return limits.min_junction_speed_mm_s
    radius = limits.junction_deviation_mm * sin_half / (1.0 - sin_half)
    v = math.sqrt(limits.acceleration_mm_s2 * radius)
    return min(max(v, limits.min_junction_speed_mm_s), v_cap)


#: Public alias — the simulator needs the same cornering rule as the planner.
junction_speed = _junction_speed


def _profile_duration(distance: float, v_entry: float, v_cruise: float,
                      v_exit: float, accel: float) -> tuple[float, float]:
    """Return ``(duration, achieved_peak_speed)`` for one trapezoid."""
    if distance <= 1e-12:
        return 0.0, v_entry
    v_entry = max(v_entry, 0.0)
    v_exit = max(v_exit, 0.0)
    v_cruise = max(v_cruise, 1e-9)

    d_accel = max((v_cruise ** 2 - v_entry ** 2) / (2.0 * accel), 0.0)
    d_decel = max((v_cruise ** 2 - v_exit ** 2) / (2.0 * accel), 0.0)

    if d_accel + d_decel <= distance:
        t = (v_cruise - v_entry) / accel
        t += (v_cruise - v_exit) / accel
        t += (distance - d_accel - d_decel) / v_cruise
        return t, v_cruise

    # Triangular profile: solve for the peak speed actually reached.
    peak_sq = (2.0 * accel * distance + v_entry ** 2 + v_exit ** 2) / 2.0
    peak = math.sqrt(max(peak_sq, 0.0))
    peak = min(peak, v_cruise)
    peak = max(peak, max(v_entry, v_exit))
    t = (peak - v_entry) / accel + (peak - v_exit) / accel
    if peak > 1e-9:
        covered = ((peak ** 2 - v_entry ** 2) + (peak ** 2 - v_exit ** 2)) / (2.0 * accel)
        leftover = distance - covered
        if leftover > 1e-9:
            t += leftover / peak
    return t, peak


def plan(moves: Sequence[Move], limits: MotionLimits) -> list[PlannedMove]:
    """Resolve a velocity profile for a chain of moves.

    Moves that are not geometrically continuous (the next move does not start
    where the previous one ended) force a full stop at the discontinuity —
    that is what happens on the machine too, because something else (a pen
    lift, a dwell) sits in between.
    """
    items = [m for m in moves if m.distance > 1e-9]
    n = len(items)
    if n == 0:
        return []

    accel = limits.acceleration_mm_s2
    dist = np.array([m.distance for m in items], dtype=float)
    v_max = np.array([max(m.feed_mm_min, 1e-6) / 60.0 for m in items], dtype=float)
    units = np.array([m.delta / m.distance for m in items], dtype=float)

    # -- junction limits ---------------------------------------------------
    v_junction = np.zeros(n + 1, dtype=float)      # speed *between* moves
    for i in range(1, n):
        prev_end = np.array(items[i - 1].end, dtype=float)
        next_start = np.array(items[i].start, dtype=float)
        if not np.allclose(prev_end, next_start, atol=1e-6):
            v_junction[i] = 0.0                     # discontinuity -> stop
            continue
        cap = min(v_max[i - 1], v_max[i])
        v_junction[i] = _junction_speed(units[i - 1], units[i], limits, cap)
    v_junction[0] = 0.0
    v_junction[n] = 0.0

    # -- backward pass: can we still stop in time? -------------------------
    for i in range(n - 1, -1, -1):
        reachable = math.sqrt(v_junction[i + 1] ** 2 + 2.0 * accel * dist[i])
        v_junction[i] = min(v_junction[i], reachable, v_max[i])

    # -- forward pass: can we actually get up to speed? --------------------
    for i in range(n):
        reachable = math.sqrt(v_junction[i] ** 2 + 2.0 * accel * dist[i])
        v_junction[i + 1] = min(v_junction[i + 1], reachable, v_max[i])

    out: list[PlannedMove] = []
    for i, m in enumerate(items):
        duration, peak = _profile_duration(
            float(dist[i]), float(v_junction[i]), float(v_max[i]),
            float(v_junction[i + 1]), accel)
        out.append(PlannedMove(
            move=m,
            distance=float(dist[i]),
            entry_speed=float(v_junction[i]),
            cruise_speed=float(peak),
            exit_speed=float(v_junction[i + 1]),
            duration=float(duration),
        ))
    return out


def total_duration(moves: Sequence[Move], limits: MotionLimits) -> float:
    """Total travel time in seconds (dwells not included)."""
    return sum(pm.duration for pm in plan(moves, limits))
