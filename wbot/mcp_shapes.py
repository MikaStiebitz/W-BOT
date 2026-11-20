"""Geometry builders for the MCP drawing tools: shapes (with optional hatch fill) and text.

Everything returns :class:`wbot.drawing.Stroke` lists in board millimetres (origin lower
left, x right, y up). Colours are already 0-based slots here. Errors are
:class:`ShapeError` with a plain English message that the AI can act on.
"""
from __future__ import annotations

import math
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

from .drawing import Stroke
from .textfont import measure, text_strokes

KINDS = ("line", "polyline", "rect", "circle", "ellipse", "polygon", "arc")
MIN_HATCH_MM = 1.0
MAX_TEXT_CHARS = 500
LIMIT_MM = 1.0e5          # coordinates beyond this are certainly a mistake


class ShapeError(ValueError):
    """Invalid shape description."""


class ShapeSpec(BaseModel):
    """One shape. Which fields are needed depends on ``kind`` (see the draw_shape tool)."""
    model_config = ConfigDict(extra="forbid")
    kind: Literal["line", "polyline", "rect", "circle", "ellipse", "polygon", "arc"]
    points: list[list[float]] | None = None
    x: float | None = None
    y: float | None = None
    width: float | None = None
    height: float | None = None
    radius: float | None = None
    rx: float | None = None
    ry: float | None = None
    start_deg: float | None = None
    end_deg: float | None = None
    sides: int | None = None
    rotation_deg: float = 0.0
    fill: bool = False
    hatch_spacing_mm: float = 3.0
    hatch_angle_deg: float = 45.0
    color: int | str = 1


def _need(spec: ShapeSpec, *names: str) -> list[float]:
    vals = [getattr(spec, n) for n in names]
    if any(v is None for v in vals):
        raise ShapeError(f"{spec.kind} needs: {', '.join(names)}")
    for n, v in zip(names, vals):
        if not math.isfinite(v) or abs(v) > LIMIT_MM:
            raise ShapeError(f"{n}={v} is not a sensible coordinate/size in mm")
    return vals


def _pts(spec: ShapeSpec, minimum: int) -> np.ndarray:
    if not spec.points or len(spec.points) < minimum:
        raise ShapeError(f"{spec.kind} needs 'points' with at least {minimum} [x, y] pairs")
    try:
        a = np.array(spec.points, dtype=float)
    except ValueError:
        raise ShapeError("points must be a list of [x, y] pairs") from None
    if a.ndim != 2 or a.shape[1] != 2 or not np.all(np.isfinite(a)) or np.abs(a).max() > LIMIT_MM:
        raise ShapeError("points must be finite [x, y] pairs in mm")
    return a


def _rot(p: np.ndarray, deg: float, about: tuple[float, float]) -> np.ndarray:
    if not deg:
        return p
    t = math.radians(deg)
    r = np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])
    c = np.asarray(about, float)
    return (p - c) @ r.T + c


def _arc_points(cx: float, cy: float, rx: float, ry: float, a0: float, a1: float,
                rot: float = 0.0, tol: float = 0.05) -> np.ndarray:
    r = max(rx, ry)
    step = 2 * math.acos(max(0.0, 1 - tol / r)) if r > tol else math.pi / 4
    n = int(min(1440, max(8, math.ceil(abs(a1 - a0) / max(step, 1e-3)))))
    t = np.linspace(a0, a1, n + 1)
    pts = np.column_stack([cx + rx * np.cos(t), cy + ry * np.sin(t)])
    return _rot(pts, rot, (cx, cy))


def shape_outline(spec: ShapeSpec) -> tuple[np.ndarray, bool]:
    """(outline polyline, closed?) of the shape."""
    k = spec.kind
    if k == "line":
        return _pts(spec, 2)[:2], False
    if k == "polyline":
        return _pts(spec, 2), False
    if k == "rect":
        x, y, w, h = _need(spec, "x", "y", "width", "height")
        if w <= 0 or h <= 0:
            raise ShapeError("rect width and height must be > 0")
        p = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h], [x, y]])
        return _rot(p, spec.rotation_deg, (x + w / 2, y + h / 2)), True
    if k == "circle":
        x, y, r = _need(spec, "x", "y", "radius")
        if r <= 0:
            raise ShapeError("circle radius must be > 0")
        return _arc_points(x, y, r, r, 0.0, 2 * math.pi), True
    if k == "ellipse":
        x, y, rx, ry = _need(spec, "x", "y", "rx", "ry")
        if rx <= 0 or ry <= 0:
            raise ShapeError("ellipse rx and ry must be > 0")
        return _arc_points(x, y, rx, ry, 0.0, 2 * math.pi, spec.rotation_deg), True
    if k == "arc":
        x, y, r, a0, a1 = _need(spec, "x", "y", "radius", "start_deg", "end_deg")
        if r <= 0:
            raise ShapeError("arc radius must be > 0")
        sweep = a1 - a0
        if sweep == 0 or abs(sweep) > 360:
            raise ShapeError("arc needs 0 < |end_deg - start_deg| <= 360")
        return _arc_points(x, y, r, r, math.radians(a0), math.radians(a1)), abs(sweep) >= 360
    # polygon: explicit points or regular n-gon
    if spec.points:
        p = _pts(spec, 3)
    else:
        x, y, r = _need(spec, "x", "y", "radius")
        n = spec.sides or 0
        if n < 3 or n > 200 or r <= 0:
            raise ShapeError("polygon needs either 'points' (>= 3) or x, y, radius and sides (3..200)")
        ang = math.pi / 2 + 2 * math.pi * np.arange(n) / n          # first vertex points up
        p = np.column_stack([x + r * np.cos(ang), y + r * np.sin(ang)])
        p = _rot(p, spec.rotation_deg, (x, y))
    if not np.allclose(p[0], p[-1]):
        p = np.vstack([p, p[:1]])
    return p, True


def hatch_lines(poly_pts: np.ndarray, rect: tuple[float, float, float, float],
                spacing: float, angle_deg: float) -> list[np.ndarray]:
    """Parallel hatch segments filling a closed polygon, clipped to ``rect`` (serpentine order)."""
    from shapely import affinity
    from shapely.geometry import LineString, Polygon, box
    if spacing < MIN_HATCH_MM:
        raise ShapeError(f"hatch_spacing_mm must be >= {MIN_HATCH_MM:g} mm")
    poly = Polygon(poly_pts).buffer(0).intersection(box(*rect))
    if poly.is_empty:
        return []
    c = poly.centroid
    rot = affinity.rotate(poly, -angle_deg, origin=(c.x, c.y))
    xmin, ymin, xmax, ymax = rot.bounds
    out: list[np.ndarray] = []
    y, flip = ymin + spacing / 2, False
    while y < ymax:
        inter = rot.intersection(LineString([(xmin - 1, y), (xmax + 1, y)]))
        geoms = [g for g in (inter.geoms if hasattr(inter, "geoms") else [inter])
                 if g.geom_type == "LineString" and g.length > 1e-6]
        geoms.sort(key=lambda g: g.bounds[0], reverse=flip)     # serpentine: alternate direction
        for g in geoms:
            a = np.array(affinity.rotate(g, angle_deg, origin=(c.x, c.y)).coords, float)
            out.append(a[::-1] if flip else a)
        flip, y = not flip, y + spacing
    return out


def build_shape(spec: ShapeSpec, color: int, rect: tuple[float, float, float, float]) -> list[Stroke]:
    """Strokes for one shape: outline, plus hatch lines when ``fill`` is set."""
    outline, closed = shape_outline(spec)
    strokes = [Stroke(outline, color)]
    if spec.fill:
        if not closed and spec.kind != "arc":
            raise ShapeError(f"{spec.kind} cannot be filled (only closed shapes and arcs)")
        poly = outline
        if spec.kind == "arc" and not closed:               # pie sector
            x, y = spec.x, spec.y
            poly = np.vstack([[x, y], outline, [x, y]])
            strokes = [Stroke(poly, color)]
        strokes += [Stroke(h, color) for h in hatch_lines(poly, rect, spec.hatch_spacing_mm, spec.hatch_angle_deg)]
    return strokes


def build_text(text: str, x: float, y: float, height_mm: float, color: int, align: str, valign: str,
               line_spacing: float) -> tuple[list[Stroke], tuple[float, float]]:
    """Hershey single-stroke text; returns (strokes, (width_mm, height_mm) of the block)."""
    if not text.strip():
        raise ShapeError("text is empty")
    if len(text) > MAX_TEXT_CHARS:
        raise ShapeError(f"text too long ({len(text)} > {MAX_TEXT_CHARS} characters)")
    if not 4.0 <= height_mm <= 400.0:
        raise ShapeError("height_mm (cap height) must be between 4 and 400 mm")
    if align not in ("left", "center", "right"):
        raise ShapeError("align must be left, center or right")
    if valign not in ("baseline", "center", "top"):
        raise ShapeError("valign must be baseline, center or top")
    if not 0.5 <= line_spacing <= 3.0:
        raise ShapeError("line_spacing must be between 0.5 and 3")
    for v in (x, y):
        if not math.isfinite(v) or abs(v) > LIMIT_MM:
            raise ShapeError("x/y are not sensible coordinates in mm")
    strokes = text_strokes(text, height_mm, x, y, anchor=align, valign=valign,
                           line_spacing=line_spacing, color=color)
    return strokes, measure(text, height_mm, line_spacing)


__all__ = ["ShapeSpec", "ShapeError", "KINDS", "build_shape", "build_text", "hatch_lines", "shape_outline"]
