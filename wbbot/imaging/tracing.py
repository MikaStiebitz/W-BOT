"""Binary-image tracing primitives shared by every tracer.

Why not just ``cv2.findContours`` everywhere
--------------------------------------------
``findContours`` traces the *boundary of a filled region*.  Given a Canny edge
map — which is one pixel wide — it walks up one side of every edge and back
down the other, so every line comes out drawn twice.  On a plotter that means
double the time and a visibly fattened stroke.

So this module provides two primitives and the tracers pick the right one:

* :func:`trace_filled_regions` — ``findContours`` on a *filled* mask, correct
  for silhouettes and logos;
* :func:`trace_thin_mask` — an 8-connected graph walk over a *one-pixel-wide*
  mask (Canny output, skeletons), which yields each stroke exactly once and
  splits cleanly at junctions.
"""

from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np

from .preprocess import ImagingError, _cv2

__all__ = [
    "trace_thin_mask",
    "trace_filled_regions",
    "thin_mask",
]

# 8-neighbourhood, ordered so straight continuations are tried before diagonals.
_NEIGHBOURS = ((0, 1), (0, -1), (1, 0), (-1, 0),
               (1, 1), (1, -1), (-1, 1), (-1, -1))


def thin_mask(mask: np.ndarray) -> np.ndarray:
    """Reduce a filled binary mask to a one-pixel skeleton."""
    try:
        from skimage.morphology import skeletonize  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise ImagingError(
            "scikit-image is required for centreline tracing "
            "(pip install 'wbbot[imaging]')") from exc
    binary = mask > 0
    return (skeletonize(binary).astype(np.uint8) * 255)


def _build_adjacency(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return ``(coords, neighbours, degree)`` for the foreground pixels.

    ``coords`` is ``(N, 2)`` in ``(row, col)``; ``neighbours`` is ``(N, 8)``
    holding indices into ``coords`` or ``-1``.
    """
    h, w = mask.shape
    fg = mask > 0
    index = np.full((h, w), -1, dtype=np.int64)
    rows, cols = np.nonzero(fg)
    n = len(rows)
    if n == 0:
        return (np.zeros((0, 2), dtype=np.int64),
                np.zeros((0, 8), dtype=np.int64),
                np.zeros(0, dtype=np.int8))
    index[rows, cols] = np.arange(n)
    coords = np.stack([rows, cols], axis=1)

    neighbours = np.full((n, 8), -1, dtype=np.int64)
    for k, (dr, dc) in enumerate(_NEIGHBOURS):
        r = rows + dr
        c = cols + dc
        valid = (r >= 0) & (r < h) & (c >= 0) & (c < w)
        found = np.full(n, -1, dtype=np.int64)
        found[valid] = index[r[valid], c[valid]]
        neighbours[:, k] = found
    degree = (neighbours >= 0).sum(axis=1).astype(np.int8)
    return coords, neighbours, degree


def trace_thin_mask(mask: np.ndarray, *,
                    min_points: int = 2,
                    min_length_px: float = 0.0) -> list[np.ndarray]:
    """Follow a one-pixel-wide mask into polylines, one per stroke.

    Walks start at endpoints (degree 1) so open strokes come out whole, then
    at junctions, then anywhere left over (closed loops).  Each edge pixel is
    consumed once, so nothing is drawn twice.

    Returns arrays of ``(x, y)`` in pixel coordinates.
    """
    if mask.ndim != 2:
        raise ImagingError(f"trace_thin_mask needs a 2D mask, got {mask.shape}")
    coords, neighbours, degree = _build_adjacency(mask)
    n = len(coords)
    if n == 0:
        return []

    used_edges: set[tuple[int, int]] = set()
    visited_node = np.zeros(n, dtype=bool)
    paths: list[np.ndarray] = []

    def edge_key(a: int, b: int) -> tuple[int, int]:
        return (a, b) if a < b else (b, a)

    def walk(start: int, first: int) -> list[int]:
        chain = [start, first]
        used_edges.add(edge_key(start, first))
        visited_node[start] = True
        visited_node[first] = True
        current = first
        previous = start
        while True:
            if degree[current] != 2:
                break                       # endpoint or junction: stop here
            nxt = -1
            for cand in neighbours[current]:
                if cand < 0 or cand == previous:
                    continue
                if edge_key(current, cand) in used_edges:
                    continue
                nxt = int(cand)
                break
            if nxt < 0:
                break
            used_edges.add(edge_key(current, nxt))
            visited_node[nxt] = True
            chain.append(nxt)
            previous, current = current, nxt
        return chain

    def emit(chain: Sequence[int]) -> None:
        if len(chain) < max(min_points, 2):
            return
        pts = coords[list(chain)]
        xy = np.stack([pts[:, 1], pts[:, 0]], axis=1).astype(float)
        if min_length_px > 0.0:
            length = float(np.sum(np.linalg.norm(np.diff(xy, axis=0), axis=1)))
            if length < min_length_px:
                return
        paths.append(xy)

    # 1. open strokes, from their endpoints
    for node in np.flatnonzero(degree == 1):
        for cand in neighbours[node]:
            if cand < 0 or edge_key(int(node), int(cand)) in used_edges:
                continue
            emit(walk(int(node), int(cand)))

    # 2. strokes radiating from junctions
    for node in np.flatnonzero(degree >= 3):
        for cand in neighbours[node]:
            if cand < 0 or edge_key(int(node), int(cand)) in used_edges:
                continue
            emit(walk(int(node), int(cand)))

    # 3. whatever is left is a closed loop
    for node in range(n):
        if degree[node] != 2:
            continue
        for cand in neighbours[node]:
            if cand < 0 or edge_key(node, int(cand)) in used_edges:
                continue
            chain = walk(node, int(cand))
            # Close the ring if the walk came back around.
            if len(chain) > 2 and edge_key(chain[-1], chain[0]) not in used_edges:
                for c in neighbours[chain[-1]]:
                    if c == chain[0]:
                        chain.append(chain[0])
                        used_edges.add(edge_key(chain[-2], chain[0]))
                        break
            emit(chain)
    return paths


def trace_filled_regions(mask: np.ndarray, *,
                         min_points: int = 4,
                         min_area_px: float = 0.0,
                         include_holes: bool = True,
                         approx_epsilon_px: float = 0.0
                         ) -> list[tuple[np.ndarray, bool]]:
    """Outline filled regions with ``findContours``.

    Returns ``(points_xy, is_closed)`` pairs.  Region outlines are always
    closed; holes are included as separate rings when ``include_holes`` is set,
    which is what makes a letter "O" come out with both circles.
    """
    cv2 = _cv2()
    if mask.ndim != 2:
        raise ImagingError(f"trace_filled_regions needs a 2D mask, got {mask.shape}")
    binary = (mask > 0).astype(np.uint8) * 255
    mode = cv2.RETR_CCOMP if include_holes else cv2.RETR_EXTERNAL
    found = cv2.findContours(binary, mode, cv2.CHAIN_APPROX_NONE)
    contours = found[-2]          # 3-tuple on OpenCV 3, 2-tuple on 4/5

    out: list[tuple[np.ndarray, bool]] = []
    for contour in contours:
        pts = contour.reshape(-1, 2).astype(float)
        if len(pts) < min_points:
            continue
        if min_area_px > 0.0 and abs(cv2.contourArea(contour)) < min_area_px:
            continue
        if approx_epsilon_px > 0.0:
            approx = cv2.approxPolyDP(contour, approx_epsilon_px, True)
            pts = approx.reshape(-1, 2).astype(float)
            if len(pts) < 3:
                continue
        out.append((pts, True))
    return out
