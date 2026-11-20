"""Tone rendering: hatching, stippling and TSP art.

A whiteboard marker lays down one uniform 2 mm line — there are no grey values,
so tone has to come from line density.  Three strategies:

``hatch``
    Parallel lines whose *spacing* follows the local tone.  Cheap, fast,
    reads well from across a room, and can be layered at two angles for
    cross-hatching.
``stipple``
    Weighted point sampling with Lloyd relaxation; each point becomes a dot.
``tsp``
    The stipple points joined into a single continuous line — the classic
    "one unbroken stroke" style, and the fastest of the three to plot because
    the pen never lifts.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

import numpy as np

from ..core.geometry import Polyline
from .preprocess import ImagingError, _cv2, to_gray

__all__ = [
    "HatchParams",
    "StippleParams",
    "hatch",
    "stipple",
    "tsp_path",
    "stipple_dots",
]


# ==========================================================================
# hatching
# ==========================================================================

@dataclass
class HatchParams:
    angle_deg: float = 45.0
    #: Line pitch for the *darkest* tone, in pixels.
    min_spacing_px: float = 3.0
    #: Line pitch for the *lightest* drawn tone, in pixels.
    max_spacing_px: float = 24.0
    #: Tones brighter than this are left blank.
    white_threshold: int = 235
    #: Tones darker than this all get the minimum spacing.
    black_threshold: int = 25
    #: Optional second pass at +90 deg for cross-hatching.
    cross_hatch: bool = False
    min_run_px: float = 4.0
    #: Restrict hatching to this mask (255 = allowed), e.g. one colour layer.
    levels: int = 6

    def validate(self) -> None:
        if self.min_spacing_px <= 0 or self.max_spacing_px <= 0:
            raise ImagingError("hatch spacing must be > 0")
        if self.min_spacing_px > self.max_spacing_px:
            raise ImagingError("min_spacing_px must not exceed max_spacing_px")
        if self.levels < 1:
            raise ImagingError("levels must be >= 1")


def _hatch_single(gray: np.ndarray, mask: np.ndarray | None,
                  angle_deg: float, params: HatchParams) -> list[Polyline]:
    """One hatching pass at one angle.

    Works in a rotated frame so the scan lines are simple image rows, then
    maps the runs back.  Quantising into a handful of tone bands keeps the
    number of distinct line pitches small, which looks deliberate rather than
    noisy.
    """
    cv2 = _cv2()
    h, w = gray.shape
    diag = int(math.ceil(math.hypot(h, w))) + 2
    centre = (w / 2.0, h / 2.0)
    rot = cv2.getRotationMatrix2D(centre, angle_deg, 1.0)
    rot[0, 2] += diag / 2.0 - centre[0]
    rot[1, 2] += diag / 2.0 - centre[1]

    rotated = cv2.warpAffine(gray, rot, (diag, diag), flags=cv2.INTER_LINEAR,
                             borderValue=255)
    if mask is not None:
        rot_mask = cv2.warpAffine(mask, rot, (diag, diag), flags=cv2.INTER_NEAREST,
                                  borderValue=0)
    else:
        rot_mask = None

    inverse = cv2.invertAffineTransform(rot)

    lo = float(params.black_threshold)
    hi = float(params.white_threshold)
    span = max(hi - lo, 1e-6)

    segments: list[np.ndarray] = []
    # Walk bands of rows; within a band the tone decides the line pitch.
    band = max(int(params.max_spacing_px), 1)
    y = 0
    while y < diag:
        row_slice = rotated[y:min(y + band, diag)]
        if row_slice.size == 0:
            break
        if rot_mask is not None:
            m_slice = rot_mask[y:min(y + band, diag)]
            valid = m_slice > 0
            tone = float(np.mean(row_slice[valid])) if valid.any() else 255.0
        else:
            tone = float(np.mean(row_slice))
        if tone >= hi:
            y += band
            continue
        # Darker tone -> smaller pitch.
        t = 1.0 - min(max((tone - lo) / span, 0.0), 1.0)
        level = round(t * (params.levels - 1)) / max(params.levels - 1, 1)
        spacing = params.max_spacing_px + (
            params.min_spacing_px - params.max_spacing_px) * level
        spacing = max(spacing, 1.0)

        yy = y
        while yy < min(y + band, diag):
            row = rotated[yy]
            drawable = row < hi
            if rot_mask is not None:
                drawable &= rot_mask[yy] > 0
            # Extract runs of drawable pixels.
            if drawable.any():
                idx = np.flatnonzero(
                    np.diff(np.concatenate([[0], drawable.view(np.int8), [0]])))
                for a, b in zip(idx[0::2], idx[1::2]):
                    if b - a >= params.min_run_px:
                        segments.append(np.array([[a, yy], [b - 1, yy]],
                                                 dtype=float))
            yy += int(round(spacing))
        y += band

    if not segments:
        return []

    out: list[Polyline] = []
    for seg in segments:
        homo = np.concatenate([seg, np.ones((2, 1))], axis=1)
        back = homo @ inverse.T
        # Discard anything the rotation pushed outside the original frame.
        if np.any(back[:, 0] < -1) or np.any(back[:, 0] > w + 1):
            continue
        if np.any(back[:, 1] < -1) or np.any(back[:, 1] > h + 1):
            continue
        out.append(Polyline(back[:, :2], closed=False))
    return out


def hatch(image: np.ndarray, params: HatchParams | None = None, *,
          mask: np.ndarray | None = None) -> list[Polyline]:
    """Hatch an image by tone. Returns pixel-space polylines."""
    params = params or HatchParams()
    params.validate()
    gray = to_gray(image)
    lines = _hatch_single(gray, mask, params.angle_deg, params)
    if params.cross_hatch:
        lines += _hatch_single(gray, mask, params.angle_deg + 90.0, params)
    return lines


# ==========================================================================
# stippling
# ==========================================================================

@dataclass
class StippleParams:
    n_points: int = 2000
    #: Lloyd relaxation rounds; more = more even, less = more organic.
    relaxation_rounds: int = 12
    white_threshold: int = 240
    min_distance_px: float = 2.0
    seed: int = 0
    #: Radius of the drawn dot in pixels (0 draws a degenerate 2-point mark).
    dot_radius_px: float = 1.0

    def validate(self) -> None:
        if self.n_points < 1:
            raise ImagingError("n_points must be >= 1")
        if self.relaxation_rounds < 0:
            raise ImagingError("relaxation_rounds must be >= 0")


def stipple(image: np.ndarray, params: StippleParams | None = None) -> np.ndarray:
    """Weighted point sampling. Returns an ``(N, 2)`` array of pixel coords.

    Rejection sampling seeds the points by darkness, then Lloyd relaxation
    (weighted centroids of a Voronoi partition) spreads them out so they read
    as even tone instead of clumps.
    """
    params = params or StippleParams()
    params.validate()
    gray = to_gray(image).astype(np.float32)
    h, w = gray.shape

    weight = np.clip(params.white_threshold - gray, 0, None)
    total = float(weight.sum())
    if total <= 0:
        return np.zeros((0, 2), dtype=float)

    rng = np.random.default_rng(params.seed)
    flat = weight.ravel() / total
    picks = rng.choice(flat.size, size=params.n_points, p=flat, replace=True)
    ys, xs = np.unravel_index(picks, (h, w))
    pts = np.stack([xs, ys], axis=1).astype(float)
    pts += rng.uniform(-0.5, 0.5, size=pts.shape)

    if params.relaxation_rounds > 0:
        try:
            from scipy.spatial import cKDTree      # noqa: PLC0415
        except ImportError:                        # pragma: no cover
            return pts
        # Sample the density field once; relaxing against every pixel would be
        # needlessly expensive for the visual difference it makes.
        step = max(1, int(math.sqrt(h * w / 120_000.0)))
        gy, gx = np.mgrid[0:h:step, 0:w:step]
        samples = np.stack([gx.ravel(), gy.ravel()], axis=1).astype(float)
        sample_w = weight[gy.ravel(), gx.ravel()].astype(float)
        keep = sample_w > 0
        samples = samples[keep]
        sample_w = sample_w[keep]
        if len(samples) == 0:
            return pts

        for _ in range(params.relaxation_rounds):
            tree = cKDTree(pts)
            _, owner = tree.query(samples, k=1)
            sums = np.zeros((len(pts), 2), dtype=float)
            weights = np.zeros(len(pts), dtype=float)
            np.add.at(sums, owner, samples * sample_w[:, None])
            np.add.at(weights, owner, sample_w)
            moved = weights > 0
            pts[moved] = sums[moved] / weights[moved, None]

    pts[:, 0] = np.clip(pts[:, 0], 0, w - 1)
    pts[:, 1] = np.clip(pts[:, 1], 0, h - 1)
    return pts


def stipple_dots(points: np.ndarray, radius_px: float = 1.0) -> list[Polyline]:
    """Turn stipple points into drawable marks.

    A whiteboard marker makes a 2 mm blob from a single touch, so a tiny
    circle is usually overkill — but a zero-length move draws nothing at all,
    so every dot becomes at least a short tick.
    """
    out: list[Polyline] = []
    if radius_px <= 0.05:
        for x, y in points:
            out.append(Polyline(np.array([[x, y], [x + 0.5, y]]), closed=False))
        return out
    angles = np.linspace(0, 2 * math.pi, 9)
    unit = np.stack([np.cos(angles), np.sin(angles)], axis=1) * radius_px
    for x, y in points:
        out.append(Polyline(unit + np.array([x, y]), closed=True))
    return out


# ==========================================================================
# TSP art
# ==========================================================================

def tsp_path(points: np.ndarray, *, time_budget_s: float = 5.0,
             two_opt: bool = True) -> Polyline:
    """Join stipple points into one continuous stroke.

    Greedy nearest neighbour, then 2-opt under a wall-clock budget. The result
    is a single pen-down path, which is by far the fastest way to render tone.
    """
    import time                                    # noqa: PLC0415

    pts = np.asarray(points, dtype=float).reshape(-1, 2)
    n = len(pts)
    if n == 0:
        return Polyline(np.zeros((0, 2)), closed=False)
    if n <= 2:
        return Polyline(pts, closed=False)

    try:
        from scipy.spatial import cKDTree          # noqa: PLC0415
        tree = cKDTree(pts)
    except ImportError:                            # pragma: no cover
        tree = None

    visited = np.zeros(n, dtype=bool)
    order = [0]
    visited[0] = True
    current = 0
    for _ in range(n - 1):
        if tree is not None:
            k = 8
            nxt = -1
            while nxt < 0 and k <= n:
                _, idxs = tree.query(pts[current], k=min(k, n))
                for idx in np.atleast_1d(idxs):
                    if not visited[int(idx)]:
                        nxt = int(idx)
                        break
                k *= 4
            if nxt < 0:
                remaining = np.flatnonzero(~visited)
                d = np.linalg.norm(pts[remaining] - pts[current], axis=1)
                nxt = int(remaining[int(np.argmin(d))])
        else:                                      # pragma: no cover
            remaining = np.flatnonzero(~visited)
            d = np.linalg.norm(pts[remaining] - pts[current], axis=1)
            nxt = int(remaining[int(np.argmin(d))])
        order.append(nxt)
        visited[nxt] = True
        current = nxt

    if two_opt and time_budget_s > 0 and n >= 5:
        deadline = time.monotonic() + time_budget_s
        arr = np.array(order)
        improved = True
        while improved and time.monotonic() < deadline:
            improved = False
            for i in range(1, n - 2):
                if time.monotonic() > deadline:
                    break
                a, b = pts[arr[i - 1]], pts[arr[i]]
                # Only consider a local window; a full sweep is O(n^2).
                for j in range(i + 1, min(i + 60, n - 1)):
                    c, d = pts[arr[j]], pts[arr[j + 1]]
                    before = np.linalg.norm(a - b) + np.linalg.norm(c - d)
                    after = np.linalg.norm(a - c) + np.linalg.norm(b - d)
                    if after < before - 1e-9:
                        arr[i:j + 1] = arr[i:j + 1][::-1]
                        improved = True
                        b = pts[arr[i]]
        order = arr.tolist()

    return Polyline(pts[order], closed=False)
