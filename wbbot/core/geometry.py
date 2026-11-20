"""Polyline intermediate representation and geometric helpers.

Everything downstream of the image pipeline speaks :class:`Polyline`: a plain
``(N, 2)`` float array of board millimetres plus a ``closed`` flag.  Keeping
the IR this dumb is deliberate — the optimiser, the G-code writer, the
simulator and the web preview all consume the same thing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Iterable, Iterator, Sequence

import numpy as np

__all__ = [
    "GeometryError",
    "Polyline",
    "Layer",
    "Drawing",
    "affine",
    "translation",
    "scaling",
    "rotation",
    "douglas_peucker",
    "bounds_of",
]


class GeometryError(ValueError):
    """Malformed geometry."""


# --------------------------------------------------------------------------
# affine helpers (3x3 homogeneous, row-vector convention: p' = p @ M.T)
# --------------------------------------------------------------------------

def affine(a: float = 1.0, b: float = 0.0, c: float = 0.0,
           d: float = 0.0, e: float = 1.0, f: float = 0.0) -> np.ndarray:
    """Build ``[[a, b, c], [d, e, f], [0, 0, 1]]``."""
    return np.array([[a, b, c], [d, e, f], [0.0, 0.0, 1.0]], dtype=float)


def translation(dx: float, dy: float) -> np.ndarray:
    return affine(1.0, 0.0, dx, 0.0, 1.0, dy)


def scaling(sx: float, sy: float | None = None,
            origin: Sequence[float] = (0.0, 0.0)) -> np.ndarray:
    sy = sx if sy is None else sy
    ox, oy = float(origin[0]), float(origin[1])
    return translation(ox, oy) @ affine(sx, 0.0, 0.0, 0.0, sy, 0.0) @ translation(-ox, -oy)


def rotation(angle_rad: float, origin: Sequence[float] = (0.0, 0.0)) -> np.ndarray:
    c, s = math.cos(angle_rad), math.sin(angle_rad)
    ox, oy = float(origin[0]), float(origin[1])
    return translation(ox, oy) @ affine(c, -s, 0.0, s, c, 0.0) @ translation(-ox, -oy)


def _apply(matrix: np.ndarray, points: np.ndarray) -> np.ndarray:
    if matrix.shape != (3, 3):
        raise GeometryError(f"transform must be 3x3, got {matrix.shape}")
    homo = np.concatenate([points, np.ones((len(points), 1))], axis=1)
    out = homo @ matrix.T
    w = out[:, 2:3]
    if np.any(np.abs(w) < 1e-12):
        raise GeometryError("transform maps a point to infinity")
    return out[:, :2] / w


# --------------------------------------------------------------------------
# Douglas-Peucker
# --------------------------------------------------------------------------

def douglas_peucker(points: np.ndarray, tolerance: float) -> np.ndarray:
    """Iterative Douglas-Peucker simplification.

    Iterative rather than recursive: a 20 000-point contour from a photo would
    blow the Python recursion limit.
    """
    pts = np.asarray(points, dtype=float)
    n = len(pts)
    if n <= 2 or tolerance <= 0.0:
        return pts.copy()

    keep = np.zeros(n, dtype=bool)
    keep[0] = keep[-1] = True
    stack: list[tuple[int, int]] = [(0, n - 1)]
    tol_sq = tolerance * tolerance

    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        a = pts[i]
        b = pts[j]
        seg = b - a
        seg_len_sq = float(seg @ seg)
        rel = pts[i + 1:j] - a
        if seg_len_sq < 1e-18:
            dist_sq = np.einsum("ij,ij->i", rel, rel)
        else:
            t = np.clip((rel @ seg) / seg_len_sq, 0.0, 1.0)
            proj = t[:, None] * seg[None, :]
            diff = rel - proj
            dist_sq = np.einsum("ij,ij->i", diff, diff)
        k = int(np.argmax(dist_sq))
        if dist_sq[k] > tol_sq:
            split = i + 1 + k
            keep[split] = True
            stack.append((i, split))
            stack.append((split, j))
    return pts[keep]


def bounds_of(items: Iterable["Polyline"]) -> tuple[float, float, float, float] | None:
    """Combined ``(min_x, min_y, max_x, max_y)`` or ``None`` when empty."""
    mins = None
    maxs = None
    for pl in items:
        if len(pl.points) == 0:
            continue
        lo = pl.points.min(axis=0)
        hi = pl.points.max(axis=0)
        mins = lo if mins is None else np.minimum(mins, lo)
        maxs = hi if maxs is None else np.maximum(maxs, hi)
    if mins is None:
        return None
    return (float(mins[0]), float(mins[1]), float(maxs[0]), float(maxs[1]))


# --------------------------------------------------------------------------
# Polyline
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Polyline:
    """An open or closed chain of points in board millimetres."""

    points: np.ndarray
    closed: bool = False

    def __post_init__(self) -> None:
        pts = np.asarray(self.points, dtype=float)
        if pts.ndim != 2 or pts.shape[1] != 2:
            raise GeometryError(
                f"Polyline needs an (N, 2) array, got shape {pts.shape}")
        if len(pts) and not np.all(np.isfinite(pts)):
            raise GeometryError("Polyline contains non-finite coordinates")
        object.__setattr__(self, "points", pts)

    # -- construction ------------------------------------------------------

    @classmethod
    def from_points(cls, points: Sequence[Sequence[float]],
                    closed: bool = False) -> "Polyline":
        arr = np.asarray(points, dtype=float)
        if arr.size == 0:
            arr = np.zeros((0, 2), dtype=float)
        return cls(arr.reshape(-1, 2), closed)

    # -- basics ------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.points)

    def __iter__(self) -> Iterator[np.ndarray]:
        return iter(self.points)

    @property
    def is_empty(self) -> bool:
        return len(self.points) < 2

    @property
    def start(self) -> np.ndarray:
        if len(self.points) == 0:
            raise GeometryError("empty polyline has no start point")
        return self.points[0]

    @property
    def end(self) -> np.ndarray:
        if len(self.points) == 0:
            raise GeometryError("empty polyline has no end point")
        return self.points[-1] if not self.closed else self.points[0]

    @property
    def drawn_points(self) -> np.ndarray:
        """Points including the closing vertex for closed rings."""
        if self.closed and len(self.points) >= 2 and not np.allclose(
                self.points[0], self.points[-1]):
            return np.vstack([self.points, self.points[0]])
        return self.points

    @property
    def length(self) -> float:
        pts = self.drawn_points
        if len(pts) < 2:
            return 0.0
        return float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1)))

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        if len(self.points) == 0:
            raise GeometryError("empty polyline has no bounds")
        lo = self.points.min(axis=0)
        hi = self.points.max(axis=0)
        return (float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1]))

    # -- transforms --------------------------------------------------------

    def reversed(self) -> "Polyline":
        return replace(self, points=self.points[::-1].copy())

    def transform(self, matrix: np.ndarray) -> "Polyline":
        if len(self.points) == 0:
            return self
        return replace(self, points=_apply(np.asarray(matrix, dtype=float), self.points))

    def translate(self, dx: float, dy: float) -> "Polyline":
        if len(self.points) == 0:
            return self
        return replace(self, points=self.points + np.array([dx, dy], dtype=float))

    def scale(self, sx: float, sy: float | None = None,
              origin: Sequence[float] = (0.0, 0.0)) -> "Polyline":
        return self.transform(scaling(sx, sy, origin))

    def rotate(self, angle_rad: float,
               origin: Sequence[float] = (0.0, 0.0)) -> "Polyline":
        return self.transform(rotation(angle_rad, origin))

    # -- resampling / cleaning --------------------------------------------

    def simplify(self, tolerance: float) -> "Polyline":
        """Douglas-Peucker, preserving the closed flag."""
        if len(self.points) <= 2 or tolerance <= 0.0:
            return self
        if self.closed:
            ring = self.drawn_points
            simplified = douglas_peucker(ring, tolerance)
            if len(simplified) >= 4 and np.allclose(simplified[0], simplified[-1]):
                simplified = simplified[:-1]
            if len(simplified) < 3:
                return replace(self, points=self.points[:1], closed=False)
            return replace(self, points=simplified)
        return replace(self, points=douglas_peucker(self.points, tolerance))

    def dedupe(self, epsilon: float = 1e-9) -> "Polyline":
        """Drop consecutive duplicate vertices."""
        pts = self.points
        if len(pts) < 2:
            return self
        d = np.linalg.norm(np.diff(pts, axis=0), axis=1)
        keep = np.concatenate([[True], d > epsilon])
        out = pts[keep]
        if len(out) < 1:
            out = pts[:1]
        return replace(self, points=out)

    def resample(self, step_mm: float) -> "Polyline":
        """Uniformly resample along arc length (keeps both endpoints)."""
        if step_mm <= 0.0:
            raise GeometryError("resample step must be > 0")
        pts = self.drawn_points
        if len(pts) < 2:
            return self
        seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        total = float(cum[-1])
        if total <= 0.0:
            return self
        n = max(int(math.ceil(total / step_mm)), 1)
        targets = np.linspace(0.0, total, n + 1)
        x = np.interp(targets, cum, pts[:, 0])
        y = np.interp(targets, cum, pts[:, 1])
        out = np.stack([x, y], axis=1)
        if self.closed and len(out) >= 4:
            out = out[:-1]
        return replace(self, points=out)

    def split_at(self, indices: Sequence[int]) -> list["Polyline"]:
        """Split an open polyline at the given vertex indices."""
        if self.closed:
            raise GeometryError("cannot split a closed polyline by index")
        cuts = sorted({int(i) for i in indices if 0 < int(i) < len(self.points) - 1})
        if not cuts:
            return [self]
        out = []
        prev = 0
        for c in cuts + [len(self.points) - 1]:
            chunk = self.points[prev:c + 1]
            if len(chunk) >= 2:
                out.append(Polyline(chunk.copy(), closed=False))
            prev = c
        return out

    # -- serialisation -----------------------------------------------------

    def to_list(self, ndigits: int = 3) -> list[list[float]]:
        return [[round(float(x), ndigits), round(float(y), ndigits)]
                for x, y in self.points]


# --------------------------------------------------------------------------
# Layer / Drawing
# --------------------------------------------------------------------------

@dataclass
class Layer:
    """All polylines that share one marker colour."""

    color_index: int
    paths: list[Polyline] = field(default_factory=list)
    name: str = ""

    def __post_init__(self) -> None:
        if self.color_index < 0:
            raise GeometryError(f"color_index must be >= 0, got {self.color_index}")
        if not self.name:
            self.name = f"layer{self.color_index + 1}"

    @property
    def length(self) -> float:
        return sum(p.length for p in self.paths)

    @property
    def n_points(self) -> int:
        return sum(len(p) for p in self.paths)

    @property
    def bounds(self) -> tuple[float, float, float, float] | None:
        return bounds_of(self.paths)

    def transform(self, matrix: np.ndarray) -> "Layer":
        return Layer(self.color_index, [p.transform(matrix) for p in self.paths], self.name)

    def filtered(self, min_length_mm: float) -> "Layer":
        return Layer(self.color_index,
                     [p for p in self.paths if p.length >= min_length_mm],
                     self.name)


@dataclass
class Drawing:
    """A complete multi-colour drawing, still in arbitrary units."""

    layers: list[Layer] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_paths(cls, paths: Sequence[Polyline], color_index: int = 0) -> "Drawing":
        return cls(layers=[Layer(color_index, list(paths))])

    @property
    def all_paths(self) -> list[Polyline]:
        return [p for layer in self.layers for p in layer.paths]

    @property
    def n_paths(self) -> int:
        return sum(len(layer.paths) for layer in self.layers)

    @property
    def n_points(self) -> int:
        return sum(layer.n_points for layer in self.layers)

    @property
    def length(self) -> float:
        return sum(layer.length for layer in self.layers)

    @property
    def bounds(self) -> tuple[float, float, float, float] | None:
        return bounds_of(self.all_paths)

    def layer_for(self, color_index: int) -> Layer:
        for layer in self.layers:
            if layer.color_index == color_index:
                return layer
        layer = Layer(color_index)
        self.layers.append(layer)
        return layer

    def transform(self, matrix: np.ndarray) -> "Drawing":
        return Drawing([layer.transform(matrix) for layer in self.layers],
                       dict(self.metadata))

    def simplified(self, tolerance: float) -> "Drawing":
        return Drawing(
            [Layer(l.color_index,
                   [p.simplify(tolerance).dedupe() for p in l.paths], l.name)
             for l in self.layers],
            dict(self.metadata))

    def filtered(self, min_length_mm: float) -> "Drawing":
        return Drawing([l.filtered(min_length_mm) for l in self.layers],
                       dict(self.metadata))

    def non_empty(self) -> "Drawing":
        """Drop empty paths and empty layers."""
        layers = []
        for layer in self.layers:
            paths = [p for p in layer.paths if not p.is_empty]
            if paths:
                layers.append(Layer(layer.color_index, paths, layer.name))
        return Drawing(layers, dict(self.metadata))

    def to_svg(self, width_mm: float, height_mm: float,
               colors: Sequence[str] | None = None,
               stroke_width_mm: float = 1.0) -> str:
        """Render a preview SVG in board coordinates (Y flipped for screen)."""
        parts = [
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{width_mm}mm" height="{height_mm}mm" '
            f'viewBox="0 0 {width_mm} {height_mm}">',
            f'<rect width="{width_mm}" height="{height_mm}" fill="#ffffff"/>',
        ]
        for layer in self.layers:
            if colors and layer.color_index < len(colors):
                stroke = colors[layer.color_index]
            else:
                stroke = "#000000"
            parts.append(f'<g fill="none" stroke="{stroke}" '
                         f'stroke-width="{stroke_width_mm}" '
                         f'stroke-linecap="round" stroke-linejoin="round">')
            for path in layer.paths:
                pts = path.drawn_points
                if len(pts) < 2:
                    continue
                d = " ".join(
                    f"{'M' if i == 0 else 'L'}{x:.2f},{height_mm - y:.2f}"
                    for i, (x, y) in enumerate(pts))
                parts.append(f'<path d="{d}"/>')
            parts.append("</g>")
        parts.append("</svg>")
        return "\n".join(parts)
