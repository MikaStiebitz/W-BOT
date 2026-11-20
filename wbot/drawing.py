"""Drawing model: polylines in board millimetres, grouped by colour slot.

A :class:`Drawing` is what patterns and the SVG importer produce and what the
planner consumes. Everything here is pure numpy geometry: JSON round-trip,
affine layout (scale/rotate/translate/fit), clipping to a rectangle
(Liang-Barsky per segment, strokes are split where they leave the rectangle)
and stroke-order optimisation (colour groups, nearest neighbour with direction
flip, a light 2-opt pass, merging of touching ends).

Coordinates: board mm, origin lower left, X right, Y up.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field

import numpy as np

log = logging.getLogger(__name__)

Rect = tuple[float, float, float, float]          # xmin, ymin, xmax, ymax
XY = tuple[float, float]

MIN_STROKE_LEN_MM = 1e-3
_MAX_NN = 15000          # above this the nearest-neighbour search is skipped (O(n^2))
_MAX_2OPT = 1200         # 2-opt only for groups up to this many strokes


def _as_pts(p) -> np.ndarray:
    a = np.array(p, dtype=float)               # copy
    if a.size == 0:
        return a.reshape(0, 2)
    a = a.reshape(-1, 2)
    if not np.all(np.isfinite(a)):
        raise ValueError("Stroke contains invalid (non-finite) coordinates")
    return a


@dataclass
class Stroke:
    """One pen-down polyline. ``color`` is a 0-based revolver/colour slot."""
    pts: np.ndarray
    color: int = 0

    def __post_init__(self) -> None:
        self.pts = _as_pts(self.pts)
        self.color = int(self.color)

    def __len__(self) -> int:
        return len(self.pts)

    @property
    def start(self) -> np.ndarray:
        return self.pts[0]

    @property
    def end(self) -> np.ndarray:
        return self.pts[-1]

    def length(self) -> float:
        if len(self.pts) < 2:
            return 0.0
        return float(np.hypot(*np.diff(self.pts, axis=0).T).sum())

    def is_closed(self, tol: float = 1e-6) -> bool:
        return len(self.pts) > 2 and float(np.hypot(*(self.pts[0] - self.pts[-1]))) <= tol

    def reversed(self) -> "Stroke":
        return Stroke(self.pts[::-1], self.color)


@dataclass
class Drawing:
    strokes: list[Stroke] = field(default_factory=list)
    name: str = ""

    # --- queries ---------------------------------------------------------------
    def __len__(self) -> int:
        return len(self.strokes)

    def bounds(self) -> list[float] | None:
        """``[xmin, ymin, xmax, ymax]`` or None for an empty drawing."""
        pts = [s.pts for s in self.strokes if len(s.pts)]
        if not pts:
            return None
        a = np.concatenate(pts)
        return [float(a[:, 0].min()), float(a[:, 1].min()), float(a[:, 0].max()), float(a[:, 1].max())]

    def length(self) -> float:
        return sum(s.length() for s in self.strokes)

    def point_count(self) -> int:
        return sum(len(s.pts) for s in self.strokes)

    def colors(self) -> list[int]:
        return sorted({s.color for s in self.strokes})

    def copy(self) -> "Drawing":
        return Drawing([Stroke(s.pts, s.color) for s in self.strokes], self.name)

    def extend(self, other: "Drawing") -> None:
        self.strokes.extend(Stroke(s.pts, s.color) for s in other.strokes)

    # --- layout ----------------------------------------------------------------
    def _map(self, fn) -> "Drawing":
        return Drawing([Stroke(fn(s.pts), s.color) for s in self.strokes], self.name)

    def transformed(self, scale: float = 1.0, rotate_deg: float = 0.0,
                    dx: float = 0.0, dy: float = 0.0, about: XY | None = None) -> "Drawing":
        """Scale and rotate (counter-clockwise) about ``about`` (default: centre of
        the bounds), then translate by ``(dx, dy)``."""
        b = self.bounds()
        if b is None:
            return self.copy()
        c = np.array(about if about is not None else ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2), float)
        t = math.radians(rotate_deg)
        rot = np.array([[math.cos(t), -math.sin(t)], [math.sin(t), math.cos(t)]])
        off = np.array([dx, dy], float)
        return self._map(lambda p: (p - c) @ rot.T * scale + c + off)

    def fit_to(self, rect: Rect, keep_aspect: bool = True, margin: float = 0.0) -> "Drawing":
        """Scale (and centre) into ``rect`` shrunk by ``margin`` on every side."""
        b = self.bounds()
        if b is None:
            return self.copy()
        x0, y0, x1, y1 = rect[0] + margin, rect[1] + margin, rect[2] - margin, rect[3] - margin
        if x1 <= x0 or y1 <= y0:
            raise ValueError("Target area is empty after subtracting the margin")
        w, h = b[2] - b[0], b[3] - b[1]
        sx = (x1 - x0) / w if w > 1e-9 else None
        sy = (y1 - y0) / h if h > 1e-9 else None
        if sx is None and sy is None:
            sx = sy = 1.0
        elif sx is None:
            sx = sy
        elif sy is None:
            sy = sx
        if keep_aspect:
            sx = sy = min(sx, sy)
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        tx, ty = (x0 + x1) / 2, (y0 + y1) / 2
        k = np.array([sx, sy])
        return self._map(lambda p: (p - [cx, cy]) * k + [tx, ty])

    # --- JSON ------------------------------------------------------------------
    def to_json(self) -> dict:
        return {"name": self.name,
                "strokes": [{"color": s.color,
                             "points": np.round(s.pts, 3).tolist()} for s in self.strokes]}

    @classmethod
    def from_json(cls, data: dict | str | bytes) -> "Drawing":
        if isinstance(data, (str, bytes)):
            data = json.loads(data)
        if not isinstance(data, dict) or not isinstance(data.get("strokes"), list):
            raise ValueError("Invalid drawing: 'strokes' is missing")
        strokes = []
        for i, s in enumerate(data["strokes"]):
            try:
                strokes.append(Stroke(s["points"] if "points" in s else s["pts"], int(s.get("color", 0))))
            except (KeyError, TypeError, ValueError) as e:
                raise ValueError(f"Invalid stroke no. {i}: {e}") from e
        return cls(strokes, str(data.get("name", "")))


# ------------------------------------------------------------------------------ cleaning
def clean(drawing: Drawing, min_length: float = MIN_STROKE_LEN_MM, dedupe: bool = True) -> Drawing:
    """Drop consecutive duplicate points and strokes shorter than ``min_length``."""
    out: list[Stroke] = []
    for s in drawing.strokes:
        p = s.pts
        if dedupe and len(p) > 1:
            keep = np.concatenate([[True], np.hypot(*np.diff(p, axis=0).T) > 1e-9])
            p = p[keep]
        st = Stroke(p, s.color)
        if len(p) >= 2 and st.length() >= min_length:
            out.append(st)
    return Drawing(out, drawing.name)


# ------------------------------------------------------------------------------ clipping
def count_outside(drawing: Drawing, rect: Rect, eps: float = 1e-6) -> int:
    """Number of points outside ``rect`` (with tolerance ``eps``)."""
    n = 0
    for s in drawing.strokes:
        p = s.pts
        n += int(np.count_nonzero((p[:, 0] < rect[0] - eps) | (p[:, 0] > rect[2] + eps)
                                  | (p[:, 1] < rect[1] - eps) | (p[:, 1] > rect[3] + eps)))
    return n


def clip_polyline(pts: np.ndarray, rect: Rect) -> list[np.ndarray]:
    """Liang-Barsky clip of a polyline; returns the visible pieces (each >= 2 points)."""
    p = _as_pts(pts)
    if len(p) < 2:
        return []
    xmin, ymin, xmax, ymax = rect
    a, b = p[:-1], p[1:]
    d = b - a
    t0 = np.zeros(len(a))
    t1 = np.ones(len(a))
    ok = np.ones(len(a), bool)
    for pp, qq in ((-d[:, 0], a[:, 0] - xmin), (d[:, 0], xmax - a[:, 0]),
                   (-d[:, 1], a[:, 1] - ymin), (d[:, 1], ymax - a[:, 1])):
        par = np.abs(pp) < 1e-15
        ok &= ~(par & (qq < 0))
        with np.errstate(divide="ignore", invalid="ignore"):
            r = np.where(par, 0.0, qq / np.where(par, 1.0, pp))
        t0 = np.where((pp < 0) & ~par, np.maximum(t0, r), t0)
        t1 = np.where((pp > 0) & ~par, np.minimum(t1, r), t1)
    ok &= t0 <= t1
    pieces: list[list[np.ndarray]] = []
    cur: list[np.ndarray] | None = None
    for i in range(len(a)):
        if not ok[i]:
            cur = None
            continue
        s = a[i] + t0[i] * d[i]
        e = a[i] + t1[i] * d[i]
        if cur is None or t0[i] > 1e-12:
            cur = [s]
            pieces.append(cur)
        cur.append(e)
        if t1[i] < 1.0 - 1e-12:
            cur = None
    out = []
    for c in pieces:
        arr = np.clip(np.array(c), [xmin, ymin], [xmax, ymax])
        if len(arr) >= 2 and float(np.hypot(*np.diff(arr, axis=0).T).sum()) > 1e-9:
            out.append(arr)
    return out


def clip_to_rect(drawing: Drawing, rect: Rect) -> Drawing:
    """Clip every stroke to ``rect``; strokes leaving and re-entering are split."""
    strokes = []
    for s in drawing.strokes:
        for piece in clip_polyline(s.pts, rect):
            strokes.append(Stroke(piece, s.color))
    return Drawing(strokes, drawing.name)


# ------------------------------------------------------------------------------ ordering
def _dist(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return np.hypot(a[..., 0] - b[..., 0], a[..., 1] - b[..., 1])


def _nearest_neighbour(S: np.ndarray, E: np.ndarray, start: np.ndarray) -> tuple[list[int], list[bool]]:
    n = len(S)
    used = np.zeros(n, bool)
    order: list[int] = []
    flips: list[bool] = []
    cur = start
    big = np.inf
    for _ in range(n):
        ds = np.where(used, big, _dist(S, cur))
        de = np.where(used, big, _dist(E, cur))
        i, j = int(np.argmin(ds)), int(np.argmin(de))
        if ds[i] <= de[j]:
            k, fl = i, False
        else:
            k, fl = j, True
        used[k] = True
        order.append(k)
        flips.append(fl)
        cur = E[k] if not fl else S[k]
    return order, flips


def _two_opt(S: np.ndarray, E: np.ndarray, start: np.ndarray,
             order: list[int], flips: list[bool], passes: int = 4) -> tuple[list[int], list[bool]]:
    """Reverse sub-sequences (flipping their strokes) while that shortens the travel."""
    n = len(order)
    ordr = np.array(order)
    fl = np.array(flips)
    Sc = np.where(fl[:, None], E[ordr], S[ordr])       # current start of each position
    Ec = np.where(fl[:, None], S[ordr], E[ordr])
    for _ in range(passes):
        improved = False
        for a in range(n):
            prev_e = start if a == 0 else Ec[a - 1]
            b = np.arange(a, n)
            eb = Ec[b]
            sa = Sc[a]
            has_next = b + 1 < n
            nxt = Sc[np.minimum(b + 1, n - 1)]
            old = _dist(prev_e, sa) + np.where(has_next, _dist(eb, nxt), 0.0)
            new = _dist(prev_e, eb) + np.where(has_next, _dist(sa, nxt), 0.0)
            delta = new - old
            j = int(np.argmin(delta))
            if delta[j] < -1e-9:
                bb = a + j
                ordr[a:bb + 1] = ordr[a:bb + 1][::-1].copy()
                fl[a:bb + 1] = ~fl[a:bb + 1][::-1]
                s_new, e_new = Ec[a:bb + 1][::-1].copy(), Sc[a:bb + 1][::-1].copy()
                Sc[a:bb + 1], Ec[a:bb + 1] = s_new, e_new
                improved = True
        if not improved:
            break
    return ordr.tolist(), fl.tolist()


def _merge_touching(strokes: list[Stroke], tol: float) -> list[Stroke]:
    out: list[Stroke] = []
    for s in strokes:
        if out and out[-1].color == s.color and float(_dist(out[-1].end, s.start)) <= tol:
            prev = out[-1]
            out[-1] = Stroke(np.concatenate([prev.pts, s.pts[1:]]), prev.color)
        else:
            out.append(s)
    return out


def travel_length(strokes: list[Stroke], start: XY | None = None) -> float:
    """Sum of pen-up distances between consecutive strokes (from ``start`` if given)."""
    cur = None if start is None else np.asarray(start, float)
    total = 0.0
    for s in strokes:
        if cur is not None:
            total += float(_dist(cur, s.start))
        cur = s.end
    return total


def optimize_order(drawing: Drawing, start: XY | None = None, *, merge_tol_mm: float = 0.05,
                   color_order: list[int] | None = None, two_opt: bool = True) -> Drawing:
    """Reorder (and flip) strokes to minimise pen-up travel.

    Strokes are grouped by colour (every colour appears exactly once, ascending
    unless ``color_order`` is given), ordered by nearest neighbour with direction
    flip and refined with a bounded 2-opt pass. Strokes whose ends touch within
    ``merge_tol_mm`` are joined (0 disables). Zero-length strokes are dropped.
    Deterministic.
    """
    d = clean(drawing)
    cur = np.asarray(start if start is not None else (0.0, 0.0), float)
    colors = color_order if color_order is not None else sorted({s.color for s in d.strokes})
    out: list[Stroke] = []
    for col in colors:
        grp = [s for s in d.strokes if s.color == col]
        if not grp:
            continue
        S = np.array([s.start for s in grp])
        E = np.array([s.end for s in grp])
        if len(grp) > _MAX_NN:
            log.warning("optimize_order: %d strokes, order left unchanged", len(grp))
            order, flips = list(range(len(grp))), [False] * len(grp)
        else:
            order, flips = _nearest_neighbour(S, E, cur)
            if two_opt and 3 <= len(grp) <= _MAX_2OPT:
                order, flips = _two_opt(S, E, cur, order, flips)
        seq = [grp[i].reversed() if f else grp[i] for i, f in zip(order, flips)]
        if merge_tol_mm > 0:
            seq = _merge_touching(seq, merge_tol_mm)
        out.extend(seq)
        cur = out[-1].end
    return Drawing(out, d.name)


__all__ = ["Stroke", "Drawing", "Rect", "clean", "count_outside", "clip_polyline", "clip_to_rect",
           "optimize_order", "travel_length", "MIN_STROKE_LEN_MM"]
