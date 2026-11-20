"""Fit artwork into the safe area — scale, centre, and *verify*.

Scaling to the safe area's bounding box is not enough: the safe area is a
polygon, not a rectangle, and the excluded parts are exactly the corners where
artwork tends to end up.  So every fit ends with an exact check of all points
and, if anything is still outside, a bisection on the scale factor.

Nothing is ever silently clipped.  If artwork cannot be made to fit, the caller
gets an exception with the reason, not a quietly truncated drawing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Sequence

import numpy as np

from ..core.geometry import Drawing, Polyline, scaling, translation
from ..core.safearea import SafeArea, SafeAreaViolation
from ..core.toolpath import Stroke, Toolpath

__all__ = ["FitResult", "fit_drawing", "fit_toolpath", "FitMode"]

FitMode = Literal["contain", "fill", "none"]

_ALIGN_X = {"left": 0.0, "center": 0.5, "centre": 0.5, "right": 1.0}
_ALIGN_Y = {"bottom": 0.0, "center": 0.5, "centre": 0.5, "top": 1.0}


@dataclass(frozen=True)
class FitResult:
    """The transform that was applied, plus how it worked out."""

    matrix: np.ndarray
    scale: float
    source_bounds: tuple[float, float, float, float]
    target_bounds: tuple[float, float, float, float]
    shrink_steps: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "scale": round(self.scale, 6),
            "source_bounds": [round(v, 2) for v in self.source_bounds],
            "target_bounds": [round(v, 2) for v in self.target_bounds],
            "shrink_steps": self.shrink_steps,
            "note": self.note,
        }


def _collect_points(paths: Sequence[Polyline]) -> np.ndarray:
    arrays = [p.drawn_points for p in paths if len(p)]
    if not arrays:
        return np.zeros((0, 2), dtype=float)
    return np.vstack(arrays)


def _build_transform(source: tuple[float, float, float, float],
                     scale: float,
                     target_center: tuple[float, float]) -> np.ndarray:
    sx0, sy0, sx1, sy1 = source
    cx = 0.5 * (sx0 + sx1)
    cy = 0.5 * (sy0 + sy1)
    return (translation(target_center[0], target_center[1])
            @ scaling(scale, scale)
            @ translation(-cx, -cy))


def _fit_points(points: np.ndarray, safe_area: SafeArea, *,
                mode: FitMode,
                margin_mm: float,
                align: tuple[str, str],
                max_scale: float | None,
                shrink_iterations: int = 40) -> FitResult:
    if len(points) == 0:
        raise SafeAreaViolation("nothing to fit: the drawing has no points")

    src = (float(points[:, 0].min()), float(points[:, 1].min()),
           float(points[:, 0].max()), float(points[:, 1].max()))
    src_w = max(src[2] - src[0], 1e-9)
    src_h = max(src[3] - src[1], 1e-9)

    poly = safe_area.polygon
    px0, py0, px1, py1 = poly.bounds
    px0 += margin_mm
    py0 += margin_mm
    px1 -= margin_mm
    py1 -= margin_mm
    if px1 <= px0 or py1 <= py0:
        raise SafeAreaViolation(
            f"a margin of {margin_mm} mm leaves no room inside the safe area")
    dst_w = px1 - px0
    dst_h = py1 - py0

    if mode == "none":
        scale = 1.0
    elif mode == "fill":
        scale = max(dst_w / src_w, dst_h / src_h)
    else:                       # contain
        scale = min(dst_w / src_w, dst_h / src_h)
    if max_scale is not None:
        scale = min(scale, max_scale)

    ax = _ALIGN_X.get(align[0], 0.5)
    ay = _ALIGN_Y.get(align[1], 0.5)

    def centre_for(s: float) -> tuple[float, float]:
        w = src_w * s
        h = src_h * s
        cx = px0 + ax * dst_w + (0.5 - ax) * w
        cy = py0 + ay * dst_h + (0.5 - ay) * h
        # Keep the box inside the target rectangle whatever the alignment.
        cx = min(max(cx, px0 + w / 2), max(px1 - w / 2, px0 + w / 2))
        cy = min(max(cy, py0 + h / 2), max(py1 - h / 2, py0 + h / 2))
        return (cx, cy)

    def transform_for(s: float) -> np.ndarray:
        return _build_transform(src, s, centre_for(s))

    def all_inside(s: float) -> bool:
        m = transform_for(s)
        homo = np.concatenate([points, np.ones((len(points), 1))], axis=1)
        moved = (homo @ m.T)[:, :2]
        return bool(np.all(safe_area.contains_many(moved)))

    steps = 0
    note = ""
    if not all_inside(scale):
        # Bisect on the scale factor: the safe area is a polygon, so fitting
        # its bounding box does not guarantee the corners are reachable.
        lo, hi = 0.0, scale
        best = None
        for _ in range(shrink_iterations):
            steps += 1
            mid = 0.5 * (lo + hi)
            if mid <= 1e-9:
                break
            if all_inside(mid):
                best = mid
                lo = mid
            else:
                hi = mid
            if hi - lo < 1e-6 * max(scale, 1.0):
                break
        if best is None:
            raise SafeAreaViolation(
                "the drawing cannot be placed inside the safe area at any "
                "scale — check the safe-area limits in machine.yaml")
        note = (f"scaled down from {scale:.4f} to {best:.4f} so every point "
                f"stays inside the safe-area polygon")
        scale = best

    matrix = transform_for(scale)
    homo = np.concatenate([points, np.ones((len(points), 1))], axis=1)
    moved = (homo @ matrix.T)[:, :2]
    dst = (float(moved[:, 0].min()), float(moved[:, 1].min()),
           float(moved[:, 0].max()), float(moved[:, 1].max()))
    return FitResult(matrix=matrix, scale=scale, source_bounds=src,
                     target_bounds=dst, shrink_steps=steps, note=note)


def fit_drawing(drawing: Drawing, safe_area: SafeArea, *,
                mode: FitMode = "contain",
                margin_mm: float = 5.0,
                align: tuple[str, str] = ("center", "center"),
                max_scale: float | None = None) -> tuple[Drawing, FitResult]:
    """Scale and centre a drawing so every point is provably reachable."""
    points = _collect_points(drawing.all_paths)
    result = _fit_points(points, safe_area, mode=mode, margin_mm=margin_mm,
                         align=align, max_scale=max_scale)
    fitted = drawing.transform(result.matrix)
    # Belt and braces: the transform is exact, but verifying costs nothing
    # compared to drawing something off the board.
    moved = _collect_points(fitted.all_paths)
    if len(moved) and not bool(np.all(safe_area.contains_many(moved))):
        raise SafeAreaViolation(
            "internal error: the fitted drawing still leaves the safe area")
    return fitted, result


def fit_toolpath(toolpath: Toolpath, safe_area: SafeArea, *,
                 mode: FitMode = "contain",
                 margin_mm: float = 5.0,
                 align: tuple[str, str] = ("center", "center"),
                 max_scale: float | None = None) -> tuple[Toolpath, FitResult]:
    """:func:`fit_drawing` for a :class:`~wbbot.core.toolpath.Toolpath`."""
    points = toolpath.all_points()
    result = _fit_points(points, safe_area, mode=mode, margin_mm=margin_mm,
                         align=align, max_scale=max_scale)
    fitted = toolpath.transform(result.matrix)
    moved = fitted.all_points()
    if len(moved) and not bool(np.all(safe_area.contains_many(moved))):
        raise SafeAreaViolation(
            "internal error: the fitted toolpath still leaves the safe area")
    return fitted, result
