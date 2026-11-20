"""Calibration patterns the machine draws for you to measure.

The pattern has to be measurable with a tape measure by one person standing at
the board, which drives every design choice here:

* a **large rectangle** gives two long, straight references — a scale error
  shows up as a length mismatch, a skewed anchor as a non-square corner;
* **crosses** at known coordinates, each with its **number written next to it**
  so measurements cannot be transcribed against the wrong target;
* targets are placed **well inside the safe area** but spread as widely as
  possible, because parameters that are only excited near the middle of the
  board come out poorly conditioned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from ..config import MachineConfig
from ..core.geometry import Polyline
from ..core.hershey import default_font
from ..core.safearea import SafeArea, SafeAreaViolation
from ..core.toolpath import Stroke, Toolpath

__all__ = ["TargetPoint", "CalibrationPattern", "build_pattern"]


@dataclass(frozen=True)
class TargetPoint:
    """One cross the machine draws and you measure."""

    index: int
    nominal: tuple[float, float]      # commanded board coordinates, mm
    label: str

    def to_dict(self) -> dict:
        return {"index": self.index,
                "nominal_x": round(self.nominal[0], 2),
                "nominal_y": round(self.nominal[1], 2),
                "label": self.label}


@dataclass
class CalibrationPattern:
    toolpath: Toolpath
    targets: list[TargetPoint]
    rectangle: tuple[float, float, float, float]     # x0, y0, x1, y1
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "targets": [t.to_dict() for t in self.targets],
            "rectangle": [round(v, 2) for v in self.rectangle],
            "n_strokes": len(self.toolpath.strokes),
            "notes": list(self.notes),
        }

    def instructions(self) -> list[str]:
        x0, y0, x1, y1 = self.rectangle
        return [
            "1. Let the machine draw the pattern completely.",
            f"2. Measure the rectangle: it should be exactly "
            f"{x1 - x0:.0f} mm wide and {y1 - y0:.0f} mm high. "
            f"Measure both diagonals too — they must be equal.",
            "3. For every numbered cross, measure the position of its centre "
            "from the lower-left corner of the drawing area (the board origin) "
            "and enter X and Y in millimetres.",
            "4. Measure carefully: the fit can only be as good as the input. "
            "1 mm of measurement error becomes roughly 1 mm of residual.",
        ]


def _cross(x: float, y: float, size: float) -> list[Polyline]:
    h = size / 2.0
    return [
        Polyline(np.array([[x - h, y], [x + h, y]]), closed=False),
        Polyline(np.array([[x, y - h], [x, y + h]]), closed=False),
    ]


def build_pattern(cfg: MachineConfig,
                  safe_area: SafeArea | None = None, *,
                  n_x: int = 3,
                  n_y: int = 3,
                  inset_mm: float = 120.0,
                  cross_size_mm: float = 40.0,
                  label_height_mm: float = 14.0,
                  color_index: int = 0,
                  draw_rectangle: bool = True,
                  draw_diagonals: bool = True) -> CalibrationPattern:
    """Build the calibration pattern for this machine.

    ``inset_mm`` keeps the targets away from the safe-area boundary so a
    slightly-off machine still lands inside it while drawing.
    """
    sa = safe_area if safe_area is not None else SafeArea(cfg)
    if n_x < 2 or n_y < 2:
        raise ValueError("the pattern needs at least a 2 x 2 grid of targets")

    x0, y0, x1, y1 = sa.polygon.bounds
    x0 += inset_mm
    y0 += inset_mm
    x1 -= inset_mm
    y1 -= inset_mm
    if x1 <= x0 or y1 <= y0:
        raise SafeAreaViolation(
            f"an inset of {inset_mm} mm leaves no room for a calibration "
            f"pattern inside the safe area")

    notes: list[str] = []
    paths: list[Polyline] = []
    font = default_font()

    if draw_rectangle:
        rect = Polyline(np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]]),
                        closed=True)
        paths.append(rect)
    if draw_diagonals:
        paths.append(Polyline(np.array([[x0, y0], [x1, y1]]), closed=False))
        paths.append(Polyline(np.array([[x0, y1], [x1, y0]]), closed=False))

    targets: list[TargetPoint] = []
    xs = np.linspace(x0, x1, n_x)
    ys = np.linspace(y0, y1, n_y)
    index = 1
    for yy in ys:                       # bottom row first, left to right
        for xx in xs:
            if not sa.contains((xx, yy)):
                notes.append(
                    f"target at ({xx:.0f}, {yy:.0f}) mm is outside the safe "
                    f"area and was skipped")
                continue
            label = str(index)
            paths.extend(_cross(float(xx), float(yy), cross_size_mm))
            # Label sits up and to the right of the cross so it never
            # overlaps the lines you are trying to measure against.
            lx = float(xx) + cross_size_mm * 0.65
            ly = float(yy) + cross_size_mm * 0.25
            label_paths = font.render(label, lx, ly, label_height_mm)
            if all(sa.contains_many(p.points).all() for p in label_paths):
                paths.extend(label_paths)
            else:
                mirrored = font.render(
                    label, float(xx) - cross_size_mm * 0.65 -
                    font.measure(label, label_height_mm)[0],
                    ly, label_height_mm)
                if all(sa.contains_many(p.points).all() for p in mirrored):
                    paths.extend(mirrored)
                else:
                    notes.append(f"label for target {label} would leave the "
                                 f"safe area and was omitted")
            targets.append(TargetPoint(index=index,
                                       nominal=(float(xx), float(yy)),
                                       label=label))
            index += 1

    if len(targets) < 4:
        raise SafeAreaViolation(
            f"only {len(targets)} calibration targets fit inside the safe "
            f"area; at least 4 are needed for a meaningful fit")

    toolpath = Toolpath([Stroke(color_index, p) for p in paths],
                        metadata={"kind": "calibration_pattern"})
    toolpath.validate(sa)
    return CalibrationPattern(toolpath=toolpath, targets=targets,
                              rectangle=(x0, y0, x1, y1), notes=notes)
