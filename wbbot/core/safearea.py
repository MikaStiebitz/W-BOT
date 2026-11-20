"""Safe area: where the plotter may actually draw.

Three independent constraints, all derived from the physics in
:mod:`wbbot.core.kinematics`:

1. **Belt tension.**  Both belts must stay above ``min_tension_n``.  A belt
   that goes slack drops the gondola out of the kinematic model entirely.
   This constraint alone rules out everything outside the horizontal span of
   the anchors.
2. **Error amplification.**  ``||J^-1||`` must stay below
   ``max_error_amplification``.  It blows up where the belts become parallel
   (far below the anchors) or nearly collinear (right under the anchor line).
3. **Clearance from the anchor line** and from the board edge.

Two levels of answer
--------------------
:meth:`SafeArea.contains` evaluates the constraints *exactly* at a point and is
what every path validation uses.  :attr:`SafeArea.polygon` is a rasterised,
deliberately **conservative** approximation used for drawing the overlay in the
UI and for fitting artwork — it never claims a point is safe that
:meth:`contains` would reject.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import cached_property
from typing import Iterable, Sequence

import numpy as np
from shapely.geometry import Polygon, MultiPolygon, box
from shapely.ops import unary_union

from ..config import MachineConfig
from .kinematics import Kinematics, UnreachableError, make_kinematics

__all__ = [
    "SafeAreaViolation",
    "Violation",
    "HeatmapData",
    "SafeArea",
]


class SafeAreaViolation(ValueError):
    """Raised when geometry would leave the safe area."""

    def __init__(self, message: str, violations: "list[Violation] | None" = None):
        super().__init__(message)
        self.violations = violations or []


@dataclass(frozen=True)
class Violation:
    """A single point that failed the safe-area test."""

    point: tuple[float, float]
    reason: str
    #: Index of the offending path within the checked collection, if known.
    path_index: int | None = None
    #: Index of the offending vertex within that path, if known.
    vertex_index: int | None = None

    def __str__(self) -> str:
        loc = ""
        if self.path_index is not None:
            loc = f" [path {self.path_index}"
            if self.vertex_index is not None:
                loc += f", point {self.vertex_index}"
            loc += "]"
        return (f"({self.point[0]:.1f}, {self.point[1]:.1f}) mm: "
                f"{self.reason}{loc}")


@dataclass(frozen=True)
class HeatmapData:
    """Sampled quality fields over the board, for the UI overlay."""

    x: np.ndarray                 # (nx,)  grid x coordinates, mm
    y: np.ndarray                 # (ny,)  grid y coordinates, mm
    error_amplification: np.ndarray   # (ny, nx)
    tension_left: np.ndarray          # (ny, nx) newtons
    tension_right: np.ndarray         # (ny, nx) newtons
    min_tension: np.ndarray           # (ny, nx) newtons
    feasible: np.ndarray              # (ny, nx) bool

    def to_json(self) -> dict:
        """Compact, JSON-safe form for the websocket channel."""
        def clean(a: np.ndarray) -> list:
            return np.where(np.isfinite(a), a, -1.0).round(4).tolist()
        return {
            "x": self.x.round(3).tolist(),
            "y": self.y.round(3).tolist(),
            "error_amplification": clean(self.error_amplification),
            "tension_left": clean(self.tension_left),
            "tension_right": clean(self.tension_right),
            "min_tension": clean(self.min_tension),
            "feasible": self.feasible.tolist(),
        }


class SafeArea:
    """Feasible drawing region for one machine configuration."""

    def __init__(self, cfg: MachineConfig, kinematics: Kinematics | None = None):
        self.cfg = cfg
        self.kin = kinematics or make_kinematics(cfg)
        sa = cfg.safe_area
        self.margin = sa.board_margin_mm
        self.min_tension = sa.min_tension_n
        self.max_amplification = sa.max_error_amplification
        self.min_anchor_distance = sa.min_distance_to_anchor_line_mm
        self.grid_step = sa.grid_step_mm

        self.board_width = cfg.board.width_mm
        self.board_height = cfg.board.height_mm

        if 2 * self.margin >= min(self.board_width, self.board_height):
            raise ValueError(
                f"safe_area.board_margin_mm ({self.margin}) is too large for a "
                f"{self.board_width} x {self.board_height} mm board")

        # Anchor line: the straight line through both anchors.
        a = self.kin.anchor_left
        b = self.kin.anchor_right
        d = b - a
        n = float(np.linalg.norm(d))
        self._anchor_a = a
        # Unit normal of the anchor line, pointing *down* (away from the anchors).
        normal = np.array([-d[1], d[0]]) / n
        if normal[1] > 0:
            normal = -normal
        self._anchor_normal_down = normal

    # ------------------------------------------------------------------
    # exact point tests
    # ------------------------------------------------------------------

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """``(min_x, min_y, max_x, max_y)`` of the margin-inset board."""
        return (self.margin, self.margin,
                self.board_width - self.margin,
                self.board_height - self.margin)

    def _anchor_line_distance(self, p: np.ndarray) -> float:
        """Signed distance below the anchor line (positive = below)."""
        return float((p - self._anchor_a) @ self._anchor_normal_down)

    def reason(self, point: Sequence[float]) -> str | None:
        """Return why ``point`` is unsafe, or ``None`` if it is fine."""
        p = np.asarray(point, dtype=float).reshape(-1)
        if p.shape != (2,) or not np.all(np.isfinite(p)):
            return f"not a finite 2D point: {tuple(point)}"
        x0, y0, x1, y1 = self.bounds
        if not (x0 <= p[0] <= x1 and y0 <= p[1] <= y1):
            return (f"outside the board area "
                    f"({x0:.0f}..{x1:.0f} x {y0:.0f}..{y1:.0f} mm)")
        dist = self._anchor_line_distance(p)
        if dist < self.min_anchor_distance:
            return (f"only {dist:.0f} mm below the anchor line, "
                    f"minimum is {self.min_anchor_distance:.0f} mm")
        try:
            tens = self.kin.tensions(p)
        except UnreachableError as exc:
            return str(exc)
        if not math.isfinite(tens.left) or not math.isfinite(tens.right):
            return "belt geometry is degenerate here (belts collinear)"
        if tens.minimum < self.min_tension:
            side = "left" if tens.left < tens.right else "right"
            return (f"{side} belt tension would drop to {tens.minimum:.2f} N, "
                    f"minimum is {self.min_tension:.2f} N (belt goes slack)")
        try:
            amp = self.kin.error_amplification(p)
        except UnreachableError as exc:
            return str(exc)
        if not math.isfinite(amp) or amp > self.max_amplification:
            return (f"position error amplification {amp:.2f} exceeds the limit "
                    f"of {self.max_amplification:.2f} (belts nearly parallel)")
        return None

    def contains(self, point: Sequence[float]) -> bool:
        """Exact feasibility test for a single point."""
        return self.reason(point) is None

    def contains_many(self, points: Sequence[Sequence[float]] | np.ndarray) -> np.ndarray:
        """Vectorised feasibility test. Returns a bool array."""
        pts = np.asarray(points, dtype=float).reshape(-1, 2)
        if pts.size == 0:
            return np.zeros(0, dtype=bool)
        ok = np.all(np.isfinite(pts), axis=1)
        x0, y0, x1, y1 = self.bounds
        ok &= (pts[:, 0] >= x0) & (pts[:, 0] <= x1)
        ok &= (pts[:, 1] >= y0) & (pts[:, 1] <= y1)
        dist = (pts - self._anchor_a[None, :]) @ self._anchor_normal_down
        ok &= dist >= self.min_anchor_distance

        idx = np.flatnonzero(ok)
        if idx.size:
            sub = pts[idx]
            v1 = self.kin.anchor_left[None, :] - sub
            v2 = self.kin.anchor_right[None, :] - sub
            n1 = np.linalg.norm(v1, axis=1)
            n2 = np.linalg.norm(v2, axis=1)
            valid = (n1 > 1e-9) & (n2 > 1e-9)
            u1 = np.zeros_like(v1)
            u2 = np.zeros_like(v2)
            u1[valid] = v1[valid] / n1[valid, None]
            u2[valid] = v2[valid] / n2[valid, None]
            det = u1[:, 0] * u2[:, 1] - u2[:, 0] * u1[:, 1]
            weight = self.kin.gondola_mass_kg * 9.80665
            safe_det = np.where(np.abs(det) < 1e-12, np.nan, det)
            t1 = -weight * u2[:, 0] / safe_det
            t2 = weight * u1[:, 0] / safe_det
            tmin = np.minimum(t1, t2)
            good = valid & np.isfinite(tmin) & (tmin >= self.min_tension)

            # Error amplification from the angle between the belt directions.
            cos_gamma = np.abs(np.sum(u1 * u2, axis=1))
            sigma_min = np.sqrt(np.maximum(1.0 - np.minimum(cos_gamma, 1.0), 0.0))
            amp = np.where(sigma_min > 1e-12, 1.0 / np.maximum(sigma_min, 1e-12), np.inf)
            good &= np.isfinite(amp) & (amp <= self.max_amplification)

            ok[idx] = good
        return ok

    # ------------------------------------------------------------------
    # polygon (approximate, conservative)
    # ------------------------------------------------------------------

    @cached_property
    def polygon(self) -> Polygon | MultiPolygon:
        """Conservative polygon approximation of the feasible region.

        A grid cell is included only when **all four of its corners** pass the
        exact test, so the polygon is a subset of the true feasible region.
        Resolution is ``safe_area.grid_step_mm``.
        """
        x0, y0, x1, y1 = self.bounds
        step = self.grid_step
        nx = max(int(math.ceil((x1 - x0) / step)), 1)
        ny = max(int(math.ceil((y1 - y0) / step)), 1)
        xs = np.linspace(x0, x1, nx + 1)
        ys = np.linspace(y0, y1, ny + 1)
        gx, gy = np.meshgrid(xs, ys)
        corners = np.stack([gx.ravel(), gy.ravel()], axis=1)
        ok = self.contains_many(corners).reshape(ny + 1, nx + 1)
        # Cell (j, i) is good iff all four surrounding corners are good.
        cell_ok = ok[:-1, :-1] & ok[1:, :-1] & ok[:-1, 1:] & ok[1:, 1:]

        if not cell_ok.any():
            raise SafeAreaViolation(
                "the safe area is empty — check safe_area limits and anchor "
                "geometry in machine.yaml")

        # Merge horizontal runs into rectangles: a handful of boxes instead of
        # tens of thousands, which keeps the union fast and the result clean.
        boxes = []
        for j in range(ny):
            row = cell_ok[j]
            i = 0
            while i < nx:
                if not row[i]:
                    i += 1
                    continue
                start = i
                while i < nx and row[i]:
                    i += 1
                boxes.append(box(xs[start], ys[j], xs[i], ys[j + 1]))
        merged = unary_union(boxes)
        # Tiny buffer round-trip closes hairline seams between row rectangles
        # without inflating the outline.
        eps = step * 1e-6
        merged = merged.buffer(eps).buffer(-eps)
        simplified = merged.simplify(step * 0.25, preserve_topology=True)
        if simplified.is_empty or not simplified.is_valid:
            simplified = merged
        return simplified

    @cached_property
    def centroid(self) -> tuple[float, float]:
        """Representative interior point of the safe area."""
        pt = self.polygon.representative_point()
        c = self.polygon.centroid
        if self.contains((c.x, c.y)):
            return (float(c.x), float(c.y))
        return (float(pt.x), float(pt.y))

    @cached_property
    def area_mm2(self) -> float:
        return float(self.polygon.area)

    def polygon_coords(self) -> list[list[list[float]]]:
        """Exterior rings as nested lists, ready for JSON / canvas."""
        geoms = (self.polygon.geoms if isinstance(self.polygon, MultiPolygon)
                 else [self.polygon])
        out = []
        for g in geoms:
            out.append([[round(float(x), 3), round(float(y), 3)]
                        for x, y in g.exterior.coords])
        return out

    # ------------------------------------------------------------------
    # path validation
    # ------------------------------------------------------------------

    def check_points(self, points: Sequence[Sequence[float]] | np.ndarray,
                     *, path_index: int | None = None,
                     max_report: int = 10) -> list[Violation]:
        """Return violations for a point list (empty list == all safe)."""
        pts = np.asarray(points, dtype=float).reshape(-1, 2)
        if pts.size == 0:
            return []
        ok = self.contains_many(pts)
        bad = np.flatnonzero(~ok)
        out: list[Violation] = []
        for k in bad[:max_report]:
            reason = self.reason(pts[k]) or "outside the safe area"
            out.append(Violation(point=(float(pts[k][0]), float(pts[k][1])),
                                 reason=reason,
                                 path_index=path_index,
                                 vertex_index=int(k)))
        if len(bad) > max_report:
            out.append(Violation(
                point=(float(pts[bad[-1]][0]), float(pts[bad[-1]][1])),
                reason=f"... and {len(bad) - max_report} further points outside "
                       f"the safe area",
                path_index=path_index))
        return out

    def require_points(self, points: Sequence[Sequence[float]] | np.ndarray,
                       *, what: str = "geometry") -> None:
        """Raise :class:`SafeAreaViolation` if any point is unsafe."""
        violations = self.check_points(points)
        if violations:
            detail = "; ".join(str(v) for v in violations[:5])
            raise SafeAreaViolation(
                f"{what} leaves the safe area at {len(violations)} point(s): "
                f"{detail}", violations)

    def check_segment(self, start: Sequence[float], end: Sequence[float],
                      *, samples: int | None = None) -> list[Violation]:
        """Check a straight move, sampling along it.

        A straight line between two safe points can still cut through an
        unsafe pocket, so the interior is sampled at roughly ``grid_step``.
        """
        p0 = np.asarray(start, dtype=float)
        p1 = np.asarray(end, dtype=float)
        dist = float(np.linalg.norm(p1 - p0))
        n = samples if samples is not None else max(int(dist / self.grid_step) + 1, 2)
        n = min(max(n, 2), 2048)
        t = np.linspace(0.0, 1.0, n)[:, None]
        pts = p0[None, :] * (1 - t) + p1[None, :] * t
        return self.check_points(pts)

    def clamp(self, point: Sequence[float]) -> tuple[tuple[float, float], bool]:
        """Return ``(nearest_safe_point, was_clamped)``.

        Used by live drawing: when the mouse leaves the reachable region the
        gondola stops at the boundary rather than either refusing to move or
        being commanded somewhere it cannot go.  The caller is responsible for
        lifting the pen and saying so — clamping silently would draw a false
        line along the border.
        """
        p = np.asarray(point, dtype=float).reshape(-1)
        if p.shape != (2,) or not np.all(np.isfinite(p)):
            return (self.centroid, True)
        if self.contains(p):
            return ((float(p[0]), float(p[1])), False)

        from shapely.geometry import Point as _Point   # noqa: PLC0415
        from shapely.ops import nearest_points          # noqa: PLC0415
        target = _Point(float(p[0]), float(p[1]))
        try:
            nearest = nearest_points(self.polygon, target)[0]
            candidate = (float(nearest.x), float(nearest.y))
        except Exception:                                # pragma: no cover
            return (self.centroid, True)

        if self.contains(candidate):
            return (candidate, True)
        # The boundary point itself can fail the exact test by a hair; step a
        # little way towards the interior until it passes.
        centre = np.array(self.centroid, dtype=float)
        vec = np.array(candidate, dtype=float)
        for t in (0.001, 0.005, 0.02, 0.05, 0.15, 0.4, 1.0):
            trial = vec * (1 - t) + centre * t
            if self.contains(trial):
                return ((float(trial[0]), float(trial[1])), True)
        return (self.centroid, True)

    # ------------------------------------------------------------------
    # heatmap
    # ------------------------------------------------------------------

    def heatmap(self, step_mm: float | None = None) -> HeatmapData:
        """Sample amplification and tensions over the whole board.

        Sampled over the *physical* board (not the inset area) so the overlay
        also shows why the excluded strips are excluded.
        """
        step = step_mm or max(self.grid_step, 5.0)
        nx = max(int(self.board_width / step) + 1, 2)
        ny = max(int(self.board_height / step) + 1, 2)
        xs = np.linspace(0.0, self.board_width, nx)
        ys = np.linspace(0.0, self.board_height, ny)
        gx, gy = np.meshgrid(xs, ys)
        pts = np.stack([gx.ravel(), gy.ravel()], axis=1)

        v1 = self.kin.anchor_left[None, :] - pts
        v2 = self.kin.anchor_right[None, :] - pts
        n1 = np.linalg.norm(v1, axis=1)
        n2 = np.linalg.norm(v2, axis=1)
        n1 = np.maximum(n1, 1e-9)
        n2 = np.maximum(n2, 1e-9)
        u1 = v1 / n1[:, None]
        u2 = v2 / n2[:, None]

        cos_gamma = np.abs(np.sum(u1 * u2, axis=1))
        sigma_min = np.sqrt(np.maximum(1.0 - np.minimum(cos_gamma, 1.0), 0.0))
        with np.errstate(divide="ignore"):
            amp = np.where(sigma_min > 1e-9, 1.0 / np.maximum(sigma_min, 1e-12), np.inf)

        det = u1[:, 0] * u2[:, 1] - u2[:, 0] * u1[:, 1]
        weight = self.kin.gondola_mass_kg * 9.80665
        safe_det = np.where(np.abs(det) < 1e-12, np.nan, det)
        t1 = -weight * u2[:, 0] / safe_det
        t2 = weight * u1[:, 0] / safe_det

        feasible = self.contains_many(pts)

        shape = (ny, nx)
        return HeatmapData(
            x=xs, y=ys,
            error_amplification=amp.reshape(shape),
            tension_left=t1.reshape(shape),
            tension_right=t2.reshape(shape),
            min_tension=np.minimum(t1, t2).reshape(shape),
            feasible=feasible.reshape(shape),
        )

    # ------------------------------------------------------------------
    # reporting
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        """Human-readable summary, used by the UI and the MCP board info."""
        hm = self.heatmap()
        finite_amp = hm.error_amplification[np.isfinite(hm.error_amplification)]
        board_area = self.board_width * self.board_height
        return {
            "board_mm": [self.board_width, self.board_height],
            "margin_mm": self.margin,
            "area_mm2": round(self.area_mm2, 1),
            "coverage_percent": round(100.0 * self.area_mm2 / board_area, 2),
            "bounds": [round(v, 2) for v in self.polygon.bounds],
            "centroid": [round(v, 2) for v in self.centroid],
            "limits": {
                "min_tension_n": self.min_tension,
                "max_error_amplification": self.max_amplification,
                "min_distance_to_anchor_line_mm": self.min_anchor_distance,
            },
            "measured": {
                "error_amplification_min": round(float(finite_amp.min()), 3)
                if finite_amp.size else None,
                "error_amplification_max": round(float(finite_amp.max()), 3)
                if finite_amp.size else None,
                "min_tension_n_over_board": round(
                    float(np.nanmin(hm.min_tension)), 3),
                "max_tension_n_over_board": round(
                    float(np.nanmax(np.maximum(hm.tension_left, hm.tension_right))), 3),
            },
            "kinematics_model": self.kin.name,
        }
