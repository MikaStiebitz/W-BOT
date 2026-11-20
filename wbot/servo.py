"""Servo angle <-> pulse <-> FluidNC axis value.

Servos are positional MG90S units without feedback, so everything here is an
absolute angle. There is no timing logic for continuous rotation anywhere.

The FluidNC firmware window ``[fw_min_us, fw_max_us]`` is the hard mechanical
limit: FluidNC clamps its position-to-pulse mapping to it. We pick it as the
integer-rounded-inward pulse range of ``[min_deg, max_deg]``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .config import ServoSpec


class ServoLimitError(ValueError):
    """Requested angle is outside the servo's allowed range."""


@dataclass(frozen=True)
class ServoModel:
    spec: ServoSpec

    # --- firmware window ---------------------------------------------------
    @property
    def fw_min_us(self) -> int:
        return math.ceil(self.spec.center_us + self.spec.us_per_deg * self.spec.min_deg)

    @property
    def fw_max_us(self) -> int:
        return math.floor(self.spec.center_us + self.spec.us_per_deg * self.spec.max_deg)

    # --- angle limits actually reachable (pulse window may be a bit narrower) --
    @property
    def min_deg(self) -> float:
        return max(self.spec.min_deg, (self.fw_min_us - self.spec.center_us) / self.spec.us_per_deg)

    @property
    def max_deg(self) -> float:
        return min(self.spec.max_deg, (self.fw_max_us - self.spec.center_us) / self.spec.us_per_deg)

    # --- conversions -------------------------------------------------------
    def pulse_us(self, angle_deg: float) -> float:
        return self.spec.center_us + self.spec.us_per_deg * angle_deg

    def angle_for_pulse(self, pulse_us: float) -> float:
        return (pulse_us - self.spec.center_us) / self.spec.us_per_deg

    def check(self, angle_deg: float) -> float:
        """Return ``angle_deg`` if allowed, else raise (never silently clamp)."""
        # 0.25 deg tolerance: integer pulse rounding makes the pulse window a hair
        # narrower than the nominal +-44 deg; values within it are clamped, not refused.
        if not (self.min_deg - 0.25 <= angle_deg <= self.max_deg + 0.25):
            raise ServoLimitError(
                f"{angle_deg:.1f} deg outside the allowed {self.min_deg:.1f}..{self.max_deg:.1f} deg "
                f"of servo on axis {self.spec.axis}")
        return min(max(angle_deg, self.min_deg), self.max_deg)

    def clamp(self, angle_deg: float) -> float:
        return min(max(angle_deg, self.min_deg), self.max_deg)

    def axis_value(self, angle_deg: float) -> float:
        """FluidNC axis coordinate that yields ``angle_deg`` (checked)."""
        a = self.check(angle_deg)
        frac = (self.pulse_us(a) - self.fw_min_us) / (self.fw_max_us - self.fw_min_us)
        return self.spec.axis_min + frac * self.spec.axis_travel

    def angle_for_axis(self, axis_value: float) -> float:
        frac = (axis_value - self.spec.axis_min) / self.spec.axis_travel
        pulse = self.fw_min_us + frac * (self.fw_max_us - self.fw_min_us)
        return self.angle_for_pulse(pulse)

    def travel_time_s(self, from_deg: float | None, to_deg: float) -> float:
        """Time to wait for the horn to arrive (unknown start -> full range)."""
        span = (self.max_deg - self.min_deg) if from_deg is None else abs(to_deg - from_deg)
        return span / self.spec.speed_deg_s + self.spec.settle_s
