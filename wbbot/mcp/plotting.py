"""Geometry generators: shapes, text, function plots, axes, equations.

Everything here returns plain :class:`~wbbot.core.geometry.Polyline` lists in
board millimetres, so it is fully testable without an MCP client, a machine, or
a network.  :mod:`wbbot.mcp.server` is a thin wrapper that exposes these as
tools and adds safe-area validation.

Pole handling
-------------
Plotting ``tan(x)`` or ``1/x`` naively draws a vertical line across the whole
board at every asymptote.  :func:`plot_function` splits the curve wherever the
value leaves the requested window, becomes non-finite, or jumps discontinuously
— so a pole becomes a gap, which is what a person would draw.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Literal, Sequence

import numpy as np

from ..core.geometry import Polyline
from ..core.hershey import CAP_HEIGHT, default_font
from .expressions import CompiledExpression, ExpressionError, compile_expression

__all__ = [
    "PlotError",
    "circle", "ellipse", "arc", "rectangle", "polygon", "bezier",
    "text", "plot_function", "plot_parametric", "plot_polar",
    "draw_axes", "plot_data", "write_equation",
    "PlotWindow",
]


class PlotError(ValueError):
    """The requested geometry cannot be produced."""


def _segments_for(radius: float, sweep_rad: float, chord_tolerance: float = 0.2
                  ) -> int:
    """Segment count so the chord error stays under ``chord_tolerance`` mm.

    Well below the 2 mm stroke width, so circles look like circles without
    generating thousands of pointless vertices.
    """
    if radius <= 0:
        return 2
    ratio = max(1.0 - chord_tolerance / radius, -1.0)
    max_step = 2.0 * math.acos(min(ratio, 1.0)) if ratio < 1.0 else math.pi
    if max_step <= 1e-6:
        return 512
    return int(min(max(math.ceil(abs(sweep_rad) / max_step), 8), 2048))


# ==========================================================================
# primitives
# ==========================================================================

def ellipse(cx: float, cy: float, rx: float, ry: float, *,
            rotation_deg: float = 0.0, segments: int | None = None) -> Polyline:
    if rx <= 0 or ry <= 0:
        raise PlotError(f"ellipse radii must be positive, got rx={rx}, ry={ry}")
    n = segments or _segments_for(max(rx, ry), 2 * math.pi)
    t = np.linspace(0.0, 2 * math.pi, n, endpoint=False)
    pts = np.stack([rx * np.cos(t), ry * np.sin(t)], axis=1)
    if rotation_deg:
        a = math.radians(rotation_deg)
        rot = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
        pts = pts @ rot.T
    return Polyline(pts + np.array([cx, cy]), closed=True)


def circle(cx: float, cy: float, radius: float, *,
           segments: int | None = None) -> Polyline:
    if radius <= 0:
        raise PlotError(f"circle radius must be positive, got {radius}")
    return ellipse(cx, cy, radius, radius, segments=segments)


def arc(cx: float, cy: float, radius: float,
        start_deg: float, end_deg: float, *,
        segments: int | None = None) -> Polyline:
    if radius <= 0:
        raise PlotError(f"arc radius must be positive, got {radius}")
    sweep = math.radians(end_deg - start_deg)
    if abs(sweep) < 1e-9:
        raise PlotError("arc sweep is zero")
    n = segments or _segments_for(radius, sweep)
    t = np.linspace(math.radians(start_deg), math.radians(end_deg), n + 1)
    pts = np.stack([cx + radius * np.cos(t), cy + radius * np.sin(t)], axis=1)
    return Polyline(pts, closed=False)


def rectangle(x: float, y: float, width: float, height: float, *,
              corner_radius: float = 0.0) -> Polyline:
    if width <= 0 or height <= 0:
        raise PlotError(
            f"rectangle needs positive dimensions, got {width} x {height}")
    if corner_radius <= 0:
        pts = np.array([[x, y], [x + width, y],
                        [x + width, y + height], [x, y + height]])
        return Polyline(pts, closed=True)

    r = min(corner_radius, width / 2.0, height / 2.0)
    parts: list[np.ndarray] = []
    for cx, cy, a0, a1 in ((x + width - r, y + r, -90, 0),
                           (x + width - r, y + height - r, 0, 90),
                           (x + r, y + height - r, 90, 180),
                           (x + r, y + r, 180, 270)):
        n = _segments_for(r, math.pi / 2)
        t = np.linspace(math.radians(a0), math.radians(a1), n + 1)
        parts.append(np.stack([cx + r * np.cos(t), cy + r * np.sin(t)], axis=1))
    return Polyline(np.vstack(parts), closed=True)


def polygon(points: Sequence[Sequence[float]], *, closed: bool = True) -> Polyline:
    pts = np.asarray(points, dtype=float).reshape(-1, 2)
    if len(pts) < 2:
        raise PlotError(f"a polygon needs at least 2 points, got {len(pts)}")
    if not np.all(np.isfinite(pts)):
        raise PlotError("polygon contains non-finite coordinates")
    return Polyline(pts, closed=bool(closed) and len(pts) >= 3)


def bezier(control_points: Sequence[Sequence[float]], *,
           segments: int | None = None) -> Polyline:
    """Bezier of any degree, via de Casteljau.

    Three points give a quadratic, four a cubic, and so on — the AI does not
    have to know which call to make.
    """
    ctrl = np.asarray(control_points, dtype=float).reshape(-1, 2)
    if len(ctrl) < 2:
        raise PlotError(
            f"a Bezier needs at least 2 control points, got {len(ctrl)}")
    if not np.all(np.isfinite(ctrl)):
        raise PlotError("control points contain non-finite coordinates")
    if len(ctrl) > 32:
        raise PlotError(
            f"{len(ctrl)} control points is beyond what a single Bezier should "
            f"carry; split it into several curves")

    span = float(np.sum(np.linalg.norm(np.diff(ctrl, axis=0), axis=1)))
    n = segments or int(min(max(span / 1.5, 16), 800))
    t = np.linspace(0.0, 1.0, n + 1)[:, None]
    pts = ctrl[None, :, :].repeat(len(t), axis=0)
    k = len(ctrl)
    for _ in range(k - 1):
        pts = pts[:, :-1, :] * (1 - t[:, :, None]) + pts[:, 1:, :] * t[:, :, None]
    return Polyline(pts[:, 0, :], closed=False)


def text(content: str, x: float, y: float, height_mm: float, *,
         align: Literal["left", "center", "right"] = "left",
         baseline: Literal["baseline", "bottom", "middle", "top"] = "baseline",
         angle_deg: float = 0.0) -> list[Polyline]:
    """Single-stroke text (see :mod:`wbbot.core.hershey`)."""
    if not content:
        return []
    if height_mm <= 0:
        raise PlotError(f"text height must be positive, got {height_mm}")
    return default_font().render(content, x, y, height_mm, align=align,
                                 baseline=baseline, angle_deg=angle_deg)


# ==========================================================================
# plot window
# ==========================================================================

@dataclass(frozen=True)
class PlotWindow:
    """Maps maths coordinates to board millimetres."""

    origin: tuple[float, float]          # board mm of maths (0, 0)
    scale_x: float                       # mm per maths unit
    scale_y: float

    @classmethod
    def fit(cls, x_range: tuple[float, float], y_range: tuple[float, float],
            box: tuple[float, float, float, float], *,
            equal_aspect: bool = False) -> "PlotWindow":
        """Fit maths ranges into a board rectangle ``(x, y, width, height)``."""
        x0, y0, w, h = box
        if w <= 0 or h <= 0:
            raise PlotError(f"plot box must have positive size, got {w} x {h}")
        dx = x_range[1] - x_range[0]
        dy = y_range[1] - y_range[0]
        if abs(dx) < 1e-12 or abs(dy) < 1e-12:
            raise PlotError(
                f"plot ranges must be non-degenerate, got x={x_range}, y={y_range}")
        sx = w / dx
        sy = h / dy
        if equal_aspect:
            s = min(abs(sx), abs(sy))
            sx = math.copysign(s, sx)
            sy = math.copysign(s, sy)
        origin = (x0 - x_range[0] * sx + (w - dx * sx) / 2.0,
                  y0 - y_range[0] * sy + (h - dy * sy) / 2.0)
        return cls(origin=origin, scale_x=sx, scale_y=sy)

    def to_board(self, xs: np.ndarray, ys: np.ndarray) -> np.ndarray:
        return np.stack([self.origin[0] + np.asarray(xs) * self.scale_x,
                         self.origin[1] + np.asarray(ys) * self.scale_y], axis=1)

    def to_board_point(self, x: float, y: float) -> tuple[float, float]:
        return (self.origin[0] + x * self.scale_x,
                self.origin[1] + y * self.scale_y)


# ==========================================================================
# function plotting
# ==========================================================================

def _split_curve(xs: np.ndarray, ys: np.ndarray,
                 y_min: float, y_max: float,
                 jump_factor: float = 12.0) -> list[tuple[np.ndarray, np.ndarray]]:
    """Break a sampled curve at poles, gaps and window exits.

    A pole shows up as a huge step between neighbouring samples, usually with
    a sign change.  Comparing each step against the *median* step makes the
    test scale-free, so it works for ``tan(x)`` and for ``1e6/x`` alike.
    """
    valid = np.isfinite(ys) & (ys >= y_min) & (ys <= y_max)
    if not valid.any():
        return []

    dy = np.abs(np.diff(ys, prepend=ys[0]))
    finite_dy = dy[np.isfinite(dy) & (dy > 0)]
    if finite_dy.size:
        median_step = float(np.median(finite_dy))
        threshold = max(median_step * jump_factor,
                        (y_max - y_min) * 0.25)
    else:
        threshold = math.inf
    breaks = np.zeros(len(ys), dtype=bool)
    breaks[1:] = (np.abs(np.diff(ys)) > threshold)

    runs: list[tuple[np.ndarray, np.ndarray]] = []
    start: int | None = None
    for i in range(len(ys)):
        if valid[i] and not (breaks[i] and start is not None):
            if start is None:
                start = i
            continue
        if start is not None and i - start >= 2:
            runs.append((xs[start:i], ys[start:i]))
        start = i if valid[i] else None
    if start is not None and len(ys) - start >= 2:
        runs.append((xs[start:], ys[start:]))
    return runs


def plot_function(expression: str, *,
                  x_min: float, x_max: float,
                  y_min: float | None = None, y_max: float | None = None,
                  window: PlotWindow | None = None,
                  box: tuple[float, float, float, float] | None = None,
                  samples: int = 1200,
                  equal_aspect: bool = False,
                  variable: str = "x") -> tuple[list[Polyline], PlotWindow, dict]:
    """Plot ``y = f(x)``. Returns ``(polylines, window, info)``."""
    if x_max <= x_min:
        raise PlotError(f"x_max ({x_max}) must be greater than x_min ({x_min})")
    if not 16 <= samples <= 200_000:
        raise PlotError(f"samples must be 16..200000, got {samples}")

    compiled = compile_expression(expression, (variable,))
    xs = np.linspace(x_min, x_max, samples)
    ys = compiled(xs)
    ys = np.asarray(ys, dtype=float)
    if ys.shape != xs.shape:
        ys = np.broadcast_to(ys, xs.shape).astype(float)

    finite = ys[np.isfinite(ys)]
    if finite.size == 0:
        raise PlotError(
            f"'{expression}' has no finite values on [{x_min}, {x_max}] — "
            f"check the expression and the range")

    if y_min is None or y_max is None:
        # Percentiles, not min/max: one pole would otherwise squash the entire
        # interesting part of the curve into a single pixel row.
        lo = float(np.percentile(finite, 1))
        hi = float(np.percentile(finite, 99))
        if hi - lo < 1e-9:
            lo, hi = lo - 1.0, hi + 1.0
        pad = 0.08 * (hi - lo)
        y_min = lo - pad if y_min is None else y_min
        y_max = hi + pad if y_max is None else y_max
    if y_max <= y_min:
        raise PlotError(f"y_max ({y_max}) must be greater than y_min ({y_min})")

    if window is None:
        if box is None:
            raise PlotError("plot_function needs either a window or a box")
        window = PlotWindow.fit((x_min, x_max), (y_min, y_max), box,
                                equal_aspect=equal_aspect)

    runs = _split_curve(xs, ys, y_min, y_max)
    polylines = [Polyline(window.to_board(rx, ry), closed=False)
                 for rx, ry in runs if len(rx) >= 2]

    clipped = int(np.sum(np.isfinite(ys) & ((ys < y_min) | (ys > y_max))))
    info = {
        "expression": expression,
        "x_range": [x_min, x_max],
        "y_range": [y_min, y_max],
        "segments": len(polylines),
        "samples": samples,
        "points_clipped": clipped,
        "non_finite": int(np.sum(~np.isfinite(ys))),
        "notes": [],
    }
    if len(polylines) > 1:
        info["notes"].append(
            f"the curve was split into {len(polylines)} pieces at "
            f"discontinuities or poles")
    if clipped:
        info["notes"].append(
            f"{clipped} sample(s) fell outside the y range and were clipped")
    if not polylines:
        info["notes"].append("nothing was drawable inside the given window")
    return polylines, window, info


def plot_parametric(x_expression: str, y_expression: str, *,
                    t_min: float, t_max: float,
                    window: PlotWindow | None = None,
                    box: tuple[float, float, float, float] | None = None,
                    x_range: tuple[float, float] | None = None,
                    y_range: tuple[float, float] | None = None,
                    samples: int = 1500,
                    equal_aspect: bool = True,
                    variable: str = "t") -> tuple[list[Polyline], PlotWindow, dict]:
    """Plot ``(x(t), y(t))``."""
    if t_max <= t_min:
        raise PlotError(f"t_max ({t_max}) must be greater than t_min ({t_min})")
    fx = compile_expression(x_expression, (variable,))
    fy = compile_expression(y_expression, (variable,))
    ts = np.linspace(t_min, t_max, samples)
    xs = np.broadcast_to(np.asarray(fx(ts), dtype=float), ts.shape)
    ys = np.broadcast_to(np.asarray(fy(ts), dtype=float), ts.shape)

    good = np.isfinite(xs) & np.isfinite(ys)
    if not good.any():
        raise PlotError(
            f"the parametric curve has no finite points on "
            f"[{t_min}, {t_max}]")

    if x_range is None:
        x_range = (float(np.min(xs[good])), float(np.max(xs[good])))
    if y_range is None:
        y_range = (float(np.min(ys[good])), float(np.max(ys[good])))
    x_range = _pad_range(x_range)
    y_range = _pad_range(y_range)

    if window is None:
        if box is None:
            raise PlotError("plot_parametric needs either a window or a box")
        window = PlotWindow.fit(x_range, y_range, box, equal_aspect=equal_aspect)

    board = window.to_board(xs, ys)
    polylines: list[Polyline] = []
    start: int | None = None
    for i in range(len(ts)):
        if good[i]:
            if start is None:
                start = i
        else:
            if start is not None and i - start >= 2:
                polylines.append(Polyline(board[start:i], closed=False))
            start = None
    if start is not None and len(ts) - start >= 2:
        polylines.append(Polyline(board[start:], closed=False))

    info = {"x_expression": x_expression, "y_expression": y_expression,
            "t_range": [t_min, t_max], "segments": len(polylines),
            "x_range": list(x_range), "y_range": list(y_range), "notes": []}
    return polylines, window, info


def plot_polar(r_expression: str, *,
               theta_min: float = 0.0, theta_max: float = 2 * math.pi,
               window: PlotWindow | None = None,
               box: tuple[float, float, float, float] | None = None,
               samples: int = 1500,
               variable: str = "theta",
               allow_negative_r: bool = True
               ) -> tuple[list[Polyline], PlotWindow, dict]:
    """Plot ``r = f(theta)``."""
    if theta_max <= theta_min:
        raise PlotError("theta_max must be greater than theta_min")
    fr = compile_expression(r_expression, (variable,))
    th = np.linspace(theta_min, theta_max, samples)
    r = np.broadcast_to(np.asarray(fr(th), dtype=float), th.shape)
    good = np.isfinite(r)
    if not allow_negative_r:
        good = good & (r >= 0)
    if not good.any():
        raise PlotError(
            f"'{r_expression}' has no usable values on "
            f"[{theta_min}, {theta_max}]")

    xs = r * np.cos(th)
    ys = r * np.sin(th)
    limit = float(np.max(np.abs(r[good])))
    limit = limit if limit > 1e-9 else 1.0
    rng = (-limit * 1.05, limit * 1.05)
    if window is None:
        if box is None:
            raise PlotError("plot_polar needs either a window or a box")
        window = PlotWindow.fit(rng, rng, box, equal_aspect=True)

    board = window.to_board(xs, ys)
    polylines: list[Polyline] = []
    start: int | None = None
    for i in range(len(th)):
        if good[i]:
            if start is None:
                start = i
        else:
            if start is not None and i - start >= 2:
                polylines.append(Polyline(board[start:i], closed=False))
            start = None
    if start is not None and len(th) - start >= 2:
        polylines.append(Polyline(board[start:], closed=False))

    info = {"r_expression": r_expression,
            "theta_range": [theta_min, theta_max],
            "segments": len(polylines), "max_r": limit, "notes": []}
    return polylines, window, info


def _pad_range(rng: tuple[float, float], fraction: float = 0.05
               ) -> tuple[float, float]:
    lo, hi = float(rng[0]), float(rng[1])
    if hi - lo < 1e-9:
        return (lo - 1.0, hi + 1.0)
    pad = (hi - lo) * fraction
    return (lo - pad, hi + pad)


# ==========================================================================
# axes and data
# ==========================================================================

def _nice_tick(span: float, target_count: int = 8) -> float:
    """A round tick spacing (1, 2, 5 x 10^n) close to ``span / target``."""
    if span <= 0:
        return 1.0
    raw = span / max(target_count, 1)
    magnitude = 10.0 ** math.floor(math.log10(raw))
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        if raw <= magnitude * mult:
            return magnitude * mult
    return magnitude * 10.0


def _format_tick(value: float, step: float) -> str:
    if abs(value) < step * 1e-6:
        return "0"
    decimals = max(0, -int(math.floor(math.log10(step))) if step < 1 else 0)
    text_value = f"{value:.{decimals}f}"
    if "." in text_value:
        text_value = text_value.rstrip("0").rstrip(".")
    return text_value or "0"


#: Smallest cap height that stays readable with a 2 mm marker. Below roughly
#: six stroke widths the counters close up and digits turn into blobs.
MIN_READABLE_TEXT_MM = 12.0


def draw_axes(window: PlotWindow, *,
              x_range: tuple[float, float],
              y_range: tuple[float, float],
              tick_spacing: tuple[float, float] | None = None,
              labels: bool = True,
              label_height_mm: float = MIN_READABLE_TEXT_MM,
              tick_length_mm: float = 6.0,
              axis_labels: tuple[str, str] = ("x", "y"),
              arrows: bool = True) -> tuple[list[Polyline], dict]:
    """Draw labelled axes for a plot window.

    ``label_height_mm`` defaults to 12 mm rather than something typographically
    small: a whiteboard marker lays down a 2 mm line, so a 6 mm digit is three
    stroke widths tall and reads as a smudge from across the room.
    """
    polylines: list[Polyline] = []

    x0b, _ = window.to_board_point(x_range[0], 0.0)
    x1b, _ = window.to_board_point(x_range[1], 0.0)
    _, y0b = window.to_board_point(0.0, y_range[0])
    _, y1b = window.to_board_point(0.0, y_range[1])

    # Axes are pinned to zero when zero is inside the window, otherwise to the
    # nearest edge — an axis floating off-screen helps nobody.
    axis_y_math = min(max(0.0, y_range[0]), y_range[1])
    axis_x_math = min(max(0.0, x_range[0]), x_range[1])
    _, axis_y = window.to_board_point(0.0, axis_y_math)
    axis_x, _ = window.to_board_point(axis_x_math, 0.0)

    polylines.append(Polyline(np.array([[x0b, axis_y], [x1b, axis_y]]), closed=False))
    polylines.append(Polyline(np.array([[axis_x, y0b], [axis_x, y1b]]), closed=False))

    if arrows:
        a = max(tick_length_mm * 1.4, 2.0)
        polylines.append(Polyline(np.array(
            [[x1b - a, axis_y + a * 0.6], [x1b, axis_y], [x1b - a, axis_y - a * 0.6]]),
            closed=False))
        polylines.append(Polyline(np.array(
            [[axis_x - a * 0.6, y1b - a], [axis_x, y1b], [axis_x + a * 0.6, y1b - a]]),
            closed=False))

    if tick_spacing is None:
        tick_spacing = (_nice_tick(x_range[1] - x_range[0]),
                        _nice_tick(y_range[1] - y_range[0]))
    step_x, step_y = tick_spacing
    if step_x <= 0 or step_y <= 0:
        raise PlotError(f"tick spacing must be positive, got {tick_spacing}")

    n_x = int((x_range[1] - x_range[0]) / step_x) + 2
    n_y = int((y_range[1] - y_range[0]) / step_y) + 2
    if n_x > 200 or n_y > 200:
        raise PlotError(
            f"tick spacing {tick_spacing} would produce {n_x} x {n_y} ticks; "
            f"use a larger spacing")

    font = default_font()
    tick_values_x = []
    start_x = math.ceil(x_range[0] / step_x) * step_x
    v = start_x
    while v <= x_range[1] + 1e-9:
        tick_values_x.append(v)
        v += step_x
    tick_values_y = []
    start_y = math.ceil(y_range[0] / step_y) * step_y
    v = start_y
    while v <= y_range[1] + 1e-9:
        tick_values_y.append(v)
        v += step_y

    for value in tick_values_x:
        bx, _ = window.to_board_point(value, 0.0)
        polylines.append(Polyline(
            np.array([[bx, axis_y - tick_length_mm],
                      [bx, axis_y + tick_length_mm]]), closed=False))
        if labels and abs(value) > step_x * 1e-6:
            label = _format_tick(value, step_x)
            polylines.extend(font.render(
                label, bx, axis_y - tick_length_mm - label_height_mm * 0.6,
                label_height_mm, align="center", baseline="top"))
    for value in tick_values_y:
        _, by = window.to_board_point(0.0, value)
        polylines.append(Polyline(
            np.array([[axis_x - tick_length_mm, by],
                      [axis_x + tick_length_mm, by]]), closed=False))
        if labels and abs(value) > step_y * 1e-6:
            label = _format_tick(value, step_y)
            polylines.extend(font.render(
                label, axis_x - tick_length_mm - label_height_mm * 0.35, by,
                label_height_mm, align="right", baseline="middle"))

    if labels and axis_labels:
        if axis_labels[0]:
            polylines.extend(font.render(
                axis_labels[0], x1b - label_height_mm * 0.4,
                axis_y + tick_length_mm + label_height_mm * 0.5,
                label_height_mm, align="right"))
        if axis_labels[1]:
            polylines.extend(font.render(
                axis_labels[1], axis_x + tick_length_mm + label_height_mm * 0.4,
                y1b - label_height_mm * 1.2, label_height_mm))

    info: dict = {"tick_spacing": [step_x, step_y],
                  "x_ticks": len(tick_values_x), "y_ticks": len(tick_values_y),
                  "label_height_mm": label_height_mm, "notes": []}
    if labels and label_height_mm < MIN_READABLE_TEXT_MM:
        info["notes"].append(
            f"tick labels are {label_height_mm:g} mm tall; below "
            f"{MIN_READABLE_TEXT_MM:g} mm a 2 mm marker fills them in. "
            f"Use a larger label_height_mm or labels=False.")
    return polylines, info


def plot_data(points: Sequence[Sequence[float]], window: PlotWindow, *,
              style: Literal["line", "scatter", "bar", "step"] = "line",
              marker_size_mm: float = 3.0) -> list[Polyline]:
    """Scatter, line, step or bar chart from data in maths coordinates."""
    pts = np.asarray(points, dtype=float).reshape(-1, 2)
    if len(pts) == 0:
        return []
    if not np.all(np.isfinite(pts)):
        raise PlotError("data contains non-finite values")
    board = window.to_board(pts[:, 0], pts[:, 1])

    if style == "line":
        return [Polyline(board, closed=False)] if len(board) >= 2 else []
    if style == "step":
        out = [board[0]]
        for prev, nxt in zip(board, board[1:]):
            out.append([nxt[0], prev[1]])
            out.append(nxt)
        return [Polyline(np.array(out), closed=False)]
    if style == "scatter":
        r = marker_size_mm / 2.0
        return [circle(float(x), float(y), r) for x, y in board]
    if style == "bar":
        _, baseline = window.to_board_point(0.0, 0.0)
        width = marker_size_mm * 2.0
        if len(board) > 1:
            width = min(width,
                        float(np.min(np.abs(np.diff(board[:, 0]))) * 0.7))
        width = max(width, 0.5)
        out = []
        for x, y in board:
            lo, hi = min(baseline, y), max(baseline, y)
            if hi - lo < 0.2:
                continue
            out.append(Polyline(np.array([
                [x - width / 2, lo], [x + width / 2, lo],
                [x + width / 2, hi], [x - width / 2, hi]]), closed=True))
        return out
    raise PlotError(f"unknown data style {style!r}")


# ==========================================================================
# equations
# ==========================================================================

def write_equation(latex: str, x: float, y: float, height_mm: float, *,
                   mode: Literal["outline", "stroke", "skeleton"] = "outline",
                   align: Literal["left", "center", "right"] = "left",
                   ) -> tuple[list[Polyline], dict]:
    r"""Render a LaTeX equation as drawable paths.

    Three honest options, because there is a real trade-off:

    ``outline`` (default)
        matplotlib's mathtext engine gives true glyph **outlines**.  At
        whiteboard sizes (>= 25 mm) that looks correct and deliberate; below
        about 15 mm the counters start filling in with a 2 mm marker.
    ``stroke``
        Convert the LaTeX to a linear expression and draw it with the
        single-stroke font.  Not typeset — no real fractions or stacked
        limits — but genuinely single-stroke, so it is legible at any size and
        far quicker to plot.
    ``skeleton``
        Render the outline, then skeletonise it back to centrelines.  Keeps
        real typesetting at small sizes at the cost of slightly ragged strokes.
    """
    if not latex.strip():
        return [], {"mode": mode, "note": "empty equation"}
    if height_mm <= 0:
        raise PlotError(f"equation height must be positive, got {height_mm}")

    if mode == "stroke":
        plain = _latex_to_plain(latex)
        polys = text(plain, x, y, height_mm, align=align)
        return polys, {"mode": "stroke", "rendered_text": plain,
                       "note": "single-stroke rendering; fractions are shown "
                               "linearly as (a)/(b)"}

    paths, info = _render_mathtext(latex, x, y, height_mm, align=align)
    if mode == "skeleton":
        paths, extra = _skeletonise(paths, height_mm)
        info.update(extra)
        info["mode"] = "skeleton"
    else:
        info["mode"] = "outline"
        if height_mm < 15.0:
            info.setdefault("notes", []).append(
                f"outline text at {height_mm:g} mm with a "
                f"2 mm marker will fill in; use mode='stroke' or a larger size")
    return paths, info


def _render_mathtext(latex: str, x: float, y: float, height_mm: float, *,
                     align: str = "left") -> tuple[list[Polyline], dict]:
    try:
        import matplotlib                                   # noqa: PLC0415
        matplotlib.use("Agg")
        from matplotlib.textpath import TextPath            # noqa: PLC0415
        from matplotlib.font_manager import FontProperties  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise PlotError(
            "matplotlib is required for LaTeX equation rendering "
            "(pip install matplotlib), or use mode='stroke'") from exc

    expression = latex.strip()
    if not (expression.startswith("$") and expression.endswith("$")):
        expression = f"${expression}$"

    try:
        path = TextPath((0, 0), expression, size=100,
                        prop=FontProperties(family="DejaVu Sans"))
    except Exception as exc:
        raise PlotError(
            f"cannot render the LaTeX {latex!r}: {exc}. Check the syntax, or "
            f"use mode='stroke' for a plain-text rendering") from exc

    polys = path.to_polygons(closed_only=False)
    if not polys:
        return [], {"note": "the equation rendered to nothing"}

    stacked = np.vstack([np.asarray(p, dtype=float) for p in polys])
    y_min = float(stacked[:, 1].min())
    y_max = float(stacked[:, 1].max())
    x_min = float(stacked[:, 0].min())
    x_max = float(stacked[:, 0].max())
    span = max(y_max - y_min, 1e-9)
    # Scale by cap height so `height_mm` means the same thing as it does for
    # single-stroke text.
    scale = height_mm / span

    width_mm = (x_max - x_min) * scale
    if align == "center":
        dx = x - width_mm / 2.0
    elif align == "right":
        dx = x - width_mm
    else:
        dx = x

    out: list[Polyline] = []
    for poly in polys:
        pts = np.asarray(poly, dtype=float)
        if len(pts) < 2:
            continue
        pts = (pts - np.array([x_min, y_min])) * scale + np.array([dx, y])
        closed = bool(np.allclose(pts[0], pts[-1], atol=1e-9))
        if closed and len(pts) > 2:
            pts = pts[:-1]
        if len(pts) >= 2:
            out.append(Polyline(pts, closed=closed))

    return out, {"latex": latex, "n_paths": len(out),
                 "width_mm": round(width_mm, 2),
                 "height_mm": round(height_mm, 2),
                 "notes": []}


def _skeletonise(paths: Sequence[Polyline], height_mm: float,
                 px_per_mm: float = 6.0) -> tuple[list[Polyline], dict]:
    """Rasterise filled outlines and trace their centrelines back out."""
    try:
        import cv2                                          # noqa: PLC0415
        from ..imaging.tracing import thin_mask, trace_thin_mask  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise PlotError(
            "OpenCV and scikit-image are required for mode='skeleton' "
            "(pip install 'wbbot[imaging]')") from exc

    if not paths:
        return [], {}
    stacked = np.vstack([p.drawn_points for p in paths])
    x0, y0 = stacked.min(axis=0)
    x1, y1 = stacked.max(axis=0)
    pad = 2.0
    w = int(math.ceil((x1 - x0 + 2 * pad) * px_per_mm))
    h = int(math.ceil((y1 - y0 + 2 * pad) * px_per_mm))
    if w < 4 or h < 4 or w * h > 40_000_000:
        raise PlotError("the equation is too small or too large to skeletonise")

    canvas = np.zeros((h, w), np.uint8)
    contours = []
    for p in paths:
        pts = (p.drawn_points - np.array([x0 - pad, y0 - pad])) * px_per_mm
        pts[:, 1] = h - pts[:, 1]
        contours.append(np.round(pts).astype(np.int32))
    # Even-odd fill so counters (the hole in an "e") stay open.
    cv2.fillPoly(canvas, contours, 255, lineType=cv2.LINE_8)
    for c in contours:
        cv2.polylines(canvas, [c], True, 255, 1, cv2.LINE_8)

    skeleton = thin_mask(canvas)
    traced = trace_thin_mask(skeleton, min_points=3, min_length_px=px_per_mm)

    out: list[Polyline] = []
    for pts in traced:
        board = pts.astype(float)
        board[:, 1] = h - board[:, 1]
        board = board / px_per_mm + np.array([x0 - pad, y0 - pad])
        pl = Polyline(board, closed=False).simplify(0.25).dedupe()
        if len(pl) >= 2:
            out.append(pl)
    return out, {"skeleton_paths": len(out),
                 "notes": ["skeletonised from the outline; strokes may be "
                           "slightly ragged at small sizes"]}


_LATEX_REPLACEMENTS = [
    (r"\left", ""), (r"\right", ""), (r"\,", " "), (r"\;", " "), (r"\!", ""),
    (r"\cdot", "*"), (r"\times", "\u00d7"), (r"\div", "\u00f7"),
    (r"\pm", "\u00b1"), (r"\leq", "\u2264"), (r"\le", "\u2264"),
    (r"\geq", "\u2265"), (r"\ge", "\u2265"), (r"\neq", "\u2260"),
    (r"\ne", "\u2260"), (r"\approx", "\u2248"), (r"\infty", "\u221e"),
    (r"\pi", "\u03c0"), (r"\alpha", "\u03b1"), (r"\beta", "\u03b2"),
    (r"\theta", "\u03b8"), (r"\lambda", "\u03bb"), (r"\omega", "\u03c9"),
    (r"\Delta", "\u0394"), (r"\sum", "\u2211"), (r"\int", "\u222b"),
    (r"\rightarrow", "\u2192"), (r"\to", "\u2192"), (r"\leftarrow", "\u2190"),
    (r"\degree", "\u00b0"), (r"\%", "%"), (r"\$", "$"),
]


def _match_group(text_in: str, open_index: int) -> tuple[str, int]:
    """Return ``(contents, index_after)`` for the ``{...}`` at ``open_index``.

    Brace-counting rather than a regex: ``\frac{\sqrt{x}}{2}`` nests, and
    ``[^{}]*`` silently mismatches it.
    """
    if open_index >= len(text_in) or text_in[open_index] != "{":
        return "", open_index
    depth = 0
    for i in range(open_index, len(text_in)):
        if text_in[i] == "{":
            depth += 1
        elif text_in[i] == "}":
            depth -= 1
            if depth == 0:
                return text_in[open_index + 1:i], i + 1
    return text_in[open_index + 1:], len(text_in)


def _expand_commands(text_in: str) -> str:
    r"""Rewrite the brace-taking commands, innermost first."""
    specs = {
        r"\frac": 2, r"\dfrac": 2, r"\tfrac": 2, r"\binom": 2,
        r"\sqrt": 1, r"\mathrm": 1, r"\mathbf": 1, r"\mathit": 1,
        r"\text": 1, r"\operatorname": 1, r"\hat": 1, r"\bar": 1,
        r"\vec": 1,
    }
    for _ in range(24):          # bounded: each pass removes one nesting level
        best = -1
        best_cmd = ""
        for cmd in specs:
            idx = text_in.rfind(cmd)
            if idx > best:
                # Avoid matching \frac inside \dfrac by preferring the longest.
                best, best_cmd = idx, cmd
        if best < 0:
            break
        cmd = best_cmd
        # Prefer the longest command name starting here.
        for other in specs:
            if text_in.startswith(other, best) and len(other) > len(cmd):
                cmd = other
        pos = best + len(cmd)
        while pos < len(text_in) and text_in[pos] == " ":
            pos += 1
        args: list[str] = []
        for _arg in range(specs[cmd]):
            if pos < len(text_in) and text_in[pos] == "{":
                content, pos = _match_group(text_in, pos)
                args.append(content)
            elif pos < len(text_in):
                args.append(text_in[pos])
                pos += 1
            else:
                args.append("")
        if cmd in (r"\frac", r"\dfrac", r"\tfrac"):
            replacement = f"({args[0]})/({args[1]})"
        elif cmd == r"\binom":
            replacement = f"C({args[0]},{args[1]})"
        elif cmd == r"\sqrt":
            replacement = f"√({args[0]})"
        else:
            replacement = args[0]
        text_in = text_in[:best] + replacement + text_in[pos:]
    return text_in


def _latex_to_plain(latex: str) -> str:
    r"""Flatten LaTeX to a single line the stroke font can draw.

    Fractions become ``(a)/(b)`` and roots become ``√(a)`` — linear rather
    than typeset, but unambiguous and drawable in one stroke width.
    """
    import re                                               # noqa: PLC0415

    out = latex.strip().strip("$").strip()
    out = _expand_commands(out)
    out = re.sub(r"\\begin\{[^}]*\}|\\end\{[^}]*\}", "", out)
    for src, dst in _LATEX_REPLACEMENTS:
        out = out.replace(src, dst)
    out = re.sub(r"\^\{([^{}]*)\}", r"^\1", out)
    out = re.sub(r"_\{([^{}]*)\}", r"_\1", out)
    out = out.replace("{", "").replace("}", "")
    out = re.sub(r"\\[a-zA-Z]+", "", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out
