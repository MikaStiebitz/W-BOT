"""Path optimisation: merge, order, simplify — and say what it cost.

A traced photo arrives as a few thousand short, arbitrarily ordered contours.
Drawn as-is, the gondola spends most of its time flying between them with the
pen up, and every one of those flights excites the pendulum.  This module:

1. **simplifies** each contour (anything finer than the 2 mm stroke width is
   wasted motion),
2. **merges** contours whose ends nearly touch into single strokes,
3. **orders** the strokes to minimise pen-up travel (greedy nearest neighbour,
   then 2-opt with orientation flipping, under a wall-clock budget),
4. **drops** contours too short to be worth the pen lift,
5. **reports** before/after numbers instead of quietly changing the drawing.

Colour grouping is never broken: each colour is optimised as its own block, so
the number of revolver changes stays at ``n_colours - 1``.

On vpype
--------
The brief suggests using ``vpype`` as the optimisation library.  This module
implements the same operations natively and uses vpype only if it is installed
*and* explicitly selected.  The reasons are recorded in ``DECISIONS.md``
(D-010): our optimiser has to stay colour-block-aware and has to report
safe-area violations rather than silently clipping, neither of which vpype's
``linesort``/``linemerge`` do — and vpype is an optional extra here rather than
a hard dependency.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from ..config import MachineConfig
from ..core.geometry import Polyline
from ..core.toolpath import Stroke, Toolpath

log = logging.getLogger(__name__)

__all__ = [
    "OptimizeReport",
    "merge_paths",
    "order_paths",
    "optimize_toolpath",
    "vpype_available",
]


def vpype_available() -> bool:
    try:
        import vpype  # noqa: F401,PLC0415
        return True
    except Exception:
        return False


@dataclass
class OptimizeReport:
    """What the optimiser did, in numbers the user can check."""

    paths_before: int = 0
    paths_after: int = 0
    points_before: int = 0
    points_after: int = 0
    draw_length_before_mm: float = 0.0
    draw_length_after_mm: float = 0.0
    travel_length_before_mm: float = 0.0
    travel_length_after_mm: float = 0.0
    duration_before_s: float = 0.0
    duration_after_s: float = 0.0
    merged: int = 0
    dropped_short: int = 0
    color_changes_before: int = 0
    color_changes_after: int = 0
    two_opt_improvements: int = 0
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def travel_saved_mm(self) -> float:
        return self.travel_length_before_mm - self.travel_length_after_mm

    @property
    def travel_saved_percent(self) -> float:
        if self.travel_length_before_mm <= 0:
            return 0.0
        return 100.0 * self.travel_saved_mm / self.travel_length_before_mm

    @property
    def time_saved_s(self) -> float:
        return self.duration_before_s - self.duration_after_s

    def to_dict(self) -> dict:
        return {
            "paths_before": self.paths_before,
            "paths_after": self.paths_after,
            "points_before": self.points_before,
            "points_after": self.points_after,
            "draw_length_before_mm": round(self.draw_length_before_mm, 1),
            "draw_length_after_mm": round(self.draw_length_after_mm, 1),
            "travel_length_before_mm": round(self.travel_length_before_mm, 1),
            "travel_length_after_mm": round(self.travel_length_after_mm, 1),
            "travel_saved_mm": round(self.travel_saved_mm, 1),
            "travel_saved_percent": round(self.travel_saved_percent, 1),
            "duration_before_s": round(self.duration_before_s, 1),
            "duration_after_s": round(self.duration_after_s, 1),
            "time_saved_s": round(self.time_saved_s, 1),
            "merged": self.merged,
            "dropped_short": self.dropped_short,
            "color_changes_before": self.color_changes_before,
            "color_changes_after": self.color_changes_after,
            "two_opt_improvements": self.two_opt_improvements,
            "elapsed_s": round(self.elapsed_s, 3),
            "notes": list(self.notes),
        }

    def summary_text(self) -> str:
        return (
            f"{self.paths_before} -> {self.paths_after} paths "
            f"({self.merged} merged, {self.dropped_short} dropped as too short); "
            f"travel {self.travel_length_before_mm:.0f} -> "
            f"{self.travel_length_after_mm:.0f} mm "
            f"({self.travel_saved_percent:+.0f} %); "
            f"estimated {self.duration_before_s / 60:.1f} -> "
            f"{self.duration_after_s / 60:.1f} min"
        )


# ==========================================================================
# merging
# ==========================================================================

def merge_paths(paths: Sequence[Polyline], tolerance: float) -> tuple[list[Polyline], int]:
    """Chain polylines whose endpoints lie within ``tolerance`` of each other.

    Returns ``(merged_paths, join_count)``.  Closed rings are never merged into
    anything — they already start where they end.
    """
    if tolerance <= 0.0:
        return list(paths), 0

    open_paths = [p for p in paths if not p.closed and len(p) >= 2]
    fixed = [p for p in paths if p.closed or len(p) < 2]
    if len(open_paths) < 2:
        return list(paths), 0

    remaining = {i: p for i, p in enumerate(open_paths)}
    merged: list[Polyline] = []
    joins = 0
    tol_sq = tolerance * tolerance

    # Endpoint index for fast lookup; rebuilt lazily as paths are consumed.
    while remaining:
        key = next(iter(remaining))
        current = remaining.pop(key)
        head = current.points[0]
        tail = current.points[-1]
        points = [current.points]

        extended = True
        while extended and remaining:
            extended = False
            best_key = None
            best_flip = False
            best_at_tail = True
            best_d = tol_sq

            for k, cand in remaining.items():
                c0, c1 = cand.points[0], cand.points[-1]
                for at_tail, anchor in ((True, tail), (False, head)):
                    for flip in (False, True):
                        # Appending after our tail needs the candidate's
                        # *start*; prepending before our head needs its *end*.
                        # Flipping the candidate swaps which endpoint that is.
                        if at_tail:
                            pt = c1 if flip else c0
                        else:
                            pt = c0 if flip else c1
                        d = float((anchor[0] - pt[0]) ** 2 + (anchor[1] - pt[1]) ** 2)
                        if d < best_d:
                            best_d = d
                            best_key = k
                            best_flip = flip
                            best_at_tail = at_tail
            if best_key is None:
                break

            cand = remaining.pop(best_key)
            pts = cand.points[::-1] if best_flip else cand.points
            coincident = best_d <= 1e-18
            if best_at_tail:
                points.append(pts[1:] if coincident and len(pts) > 1 else pts)
                tail = points[-1][-1]
            else:
                points.insert(0, pts[:-1] if coincident and len(pts) > 1 else pts)
                head = points[0][0]
            joins += 1
            extended = True

        combined = np.vstack(points)
        merged.append(Polyline(combined, closed=False).dedupe(1e-9))

    return merged + fixed, joins


# ==========================================================================
# ordering
# ==========================================================================

def _endpoints(paths: Sequence[Polyline]) -> tuple[np.ndarray, np.ndarray]:
    starts = np.array([p.points[0] for p in paths], dtype=float)
    ends = np.array([p.drawn_points[-1] for p in paths], dtype=float)
    return starts, ends


def _greedy_order(paths: Sequence[Polyline], start: np.ndarray
                  ) -> list[tuple[int, bool]]:
    """Nearest-neighbour ordering, choosing each path's direction as it goes."""
    n = len(paths)
    if n == 0:
        return []
    starts, ends = _endpoints(paths)
    reversible = np.array([not p.closed for p in paths])

    unvisited = np.ones(n, dtype=bool)
    order: list[tuple[int, bool]] = []
    current = np.asarray(start, dtype=float)

    try:
        from scipy.spatial import cKDTree      # noqa: PLC0415
        # Index both endpoints; entry 2i is path i forward, 2i+1 reversed.
        coords = np.empty((2 * n, 2), dtype=float)
        coords[0::2] = starts
        coords[1::2] = ends
        tree = cKDTree(coords)
        use_tree = n > 32
    except Exception:                          # pragma: no cover
        tree = None
        use_tree = False

    for _ in range(n):
        if use_tree and tree is not None:
            k = min(2 * n, 64)
            found = False
            while True:
                dists, idxs = tree.query(current, k=k)
                idxs = np.atleast_1d(idxs)
                dists = np.atleast_1d(dists)
                for d, idx in zip(dists, idxs):
                    if idx >= 2 * n:
                        continue
                    pi, flipped = divmod(int(idx), 2)
                    if not unvisited[pi]:
                        continue
                    if flipped and not reversible[pi]:
                        continue
                    order.append((pi, bool(flipped)))
                    unvisited[pi] = False
                    current = starts[pi] if flipped else ends[pi]
                    found = True
                    break
                if found or k >= 2 * n:
                    break
                k = min(2 * n, k * 4)
            if found:
                continue

        # Brute force fallback (also used for small inputs).
        idx_pool = np.flatnonzero(unvisited)
        d_fwd = np.linalg.norm(starts[idx_pool] - current, axis=1)
        d_rev = np.where(reversible[idx_pool],
                         np.linalg.norm(ends[idx_pool] - current, axis=1),
                         np.inf)
        best_fwd = int(np.argmin(d_fwd))
        best_rev = int(np.argmin(d_rev))
        if d_rev[best_rev] < d_fwd[best_fwd]:
            pi = int(idx_pool[best_rev])
            order.append((pi, True))
            current = starts[pi]
        else:
            pi = int(idx_pool[best_fwd])
            order.append((pi, False))
            current = ends[pi]
        unvisited[pi] = False
    return order


def _travel_cost(order: Sequence[tuple[int, bool]], starts: np.ndarray,
                 ends: np.ndarray, origin: np.ndarray) -> float:
    if not order:
        return 0.0
    total = 0.0
    prev = origin
    for pi, flipped in order:
        s = ends[pi] if flipped else starts[pi]
        e = starts[pi] if flipped else ends[pi]
        total += float(np.linalg.norm(s - prev))
        prev = e
    return total


def _two_opt(order: list[tuple[int, bool]], paths: Sequence[Polyline],
             origin: np.ndarray, *, time_budget_s: float,
             max_iterations: int) -> tuple[list[tuple[int, bool]], int]:
    """2-opt with orientation flipping, bounded by wall clock.

    Reversing a run of strokes also reverses each stroke, which is free for an
    open polyline and forbidden for a closed ring — those keep their direction
    and simply move as a block.
    """
    n = len(order)
    if n < 4 or time_budget_s <= 0.0:
        return order, 0
    starts, ends = _endpoints(paths)
    reversible = np.array([not p.closed for p in paths])

    def node_start(item: tuple[int, bool]) -> np.ndarray:
        pi, fl = item
        return ends[pi] if fl else starts[pi]

    def node_end(item: tuple[int, bool]) -> np.ndarray:
        pi, fl = item
        return starts[pi] if fl else ends[pi]

    deadline = time.monotonic() + time_budget_s
    improvements = 0
    iterations = 0
    improved = True
    while improved:
        improved = False
        for i in range(n):
            if time.monotonic() > deadline or iterations >= max_iterations:
                return order, improvements
            prev_end = origin if i == 0 else node_end(order[i - 1])
            for j in range(i + 1, n):
                iterations += 1
                if iterations % 512 == 0 and time.monotonic() > deadline:
                    return order, improvements
                # The whole run [i..j] must be reversible to flip it.
                if not all(reversible[order[k][0]] for k in range(i, j + 1)):
                    continue
                next_start = (node_start(order[j + 1]) if j + 1 < n else None)
                before = float(np.linalg.norm(node_start(order[i]) - prev_end))
                after = float(np.linalg.norm(node_end(order[j]) - prev_end))
                if next_start is not None:
                    before += float(np.linalg.norm(next_start - node_end(order[j])))
                    after += float(np.linalg.norm(next_start - node_start(order[i])))
                if after < before - 1e-9:
                    segment = [(pi, not fl) for pi, fl in order[i:j + 1]][::-1]
                    order[i:j + 1] = segment
                    improvements += 1
                    improved = True
                    prev_end = origin if i == 0 else node_end(order[i - 1])
    return order, improvements


def order_paths(paths: Sequence[Polyline], start: Sequence[float], *,
                time_budget_s: float = 5.0,
                two_opt_max_iterations: int = 20000
                ) -> tuple[list[Polyline], int, float, float]:
    """Order and orient paths to minimise pen-up travel.

    Returns ``(ordered_paths, two_opt_improvements, travel_before, travel_after)``.
    """
    if not paths:
        return [], 0, 0.0, 0.0
    origin = np.asarray(start, dtype=float)
    starts, ends = _endpoints(paths)
    identity = [(i, False) for i in range(len(paths))]
    travel_before = _travel_cost(identity, starts, ends, origin)

    order = _greedy_order(paths, origin)
    order, improvements = _two_opt(order, paths, origin,
                                   time_budget_s=time_budget_s,
                                   max_iterations=two_opt_max_iterations)
    travel_after = _travel_cost(order, starts, ends, origin)

    out = []
    for pi, flipped in order:
        p = paths[pi]
        out.append(p.reversed() if (flipped and not p.closed) else p)
    return out, improvements, travel_before, travel_after


# ==========================================================================
# top level
# ==========================================================================

def optimize_toolpath(toolpath: Toolpath, cfg: MachineConfig, *,
                      start: Sequence[float] | None = None,
                      simplify_tolerance_mm: float | None = None,
                      merge_tolerance_mm: float | None = None,
                      min_path_length_mm: float | None = None,
                      time_budget_s: float | None = None,
                      color_order: Sequence[int] | None = None
                      ) -> tuple[Toolpath, OptimizeReport]:
    """Full optimisation pass. Never reorders across colours."""
    t0 = time.monotonic()
    p = cfg.paths
    simplify_tol = p.simplify_tolerance_mm if simplify_tolerance_mm is None \
        else simplify_tolerance_mm
    merge_tol = p.merge_tolerance_mm if merge_tolerance_mm is None \
        else merge_tolerance_mm
    min_len = p.min_path_length_mm if min_path_length_mm is None \
        else min_path_length_mm
    budget = p.optimize_time_budget_s if time_budget_s is None else time_budget_s

    report = OptimizeReport()
    report.paths_before = len(toolpath.strokes)
    report.points_before = sum(len(s.polyline) for s in toolpath.strokes)
    report.color_changes_before = toolpath.color_change_count
    before_stats = toolpath.stats(cfg, start_position=start)
    report.draw_length_before_mm = before_stats.draw_length_mm
    report.travel_length_before_mm = before_stats.travel_length_mm
    report.duration_before_s = before_stats.total_duration_s

    # Group by colour, preserving the requested colour sequence.
    by_color: dict[int, list[Polyline]] = {}
    for stroke in toolpath.strokes:
        by_color.setdefault(stroke.color_index, []).append(stroke.polyline)
    if color_order is not None:
        order = [c for c in color_order if c in by_color]
        order += [c for c in sorted(by_color) if c not in order]
    else:
        order = sorted(by_color)

    cursor = np.asarray(start if start is not None else cfg.motion.park_position,
                        dtype=float)
    per_color_budget = budget / max(len(order), 1)
    out_strokes: list[Stroke] = []

    for color in order:
        paths = by_color[color]

        if simplify_tol > 0:
            paths = [p_.simplify(simplify_tol).dedupe() for p_ in paths]
        paths = [p_ for p_ in paths if not p_.is_empty]

        paths, joins = merge_paths(paths, merge_tol)
        report.merged += joins

        if min_len > 0:
            kept = [p_ for p_ in paths if p_.length >= min_len]
            report.dropped_short += len(paths) - len(kept)
            paths = kept

        if not paths:
            continue

        ordered, improvements, _, _ = order_paths(
            paths, cursor, time_budget_s=per_color_budget,
            two_opt_max_iterations=p.two_opt_max_iterations)
        report.two_opt_improvements += improvements

        for path in ordered:
            out_strokes.append(Stroke(color, path))
        cursor = ordered[-1].drawn_points[-1]

    result = Toolpath(out_strokes, dict(toolpath.metadata))
    report.paths_after = len(result.strokes)
    report.points_after = sum(len(s.polyline) for s in result.strokes)
    report.color_changes_after = result.color_change_count
    after_stats = result.stats(cfg, start_position=start)
    report.draw_length_after_mm = after_stats.draw_length_mm
    report.travel_length_after_mm = after_stats.travel_length_mm
    report.duration_after_s = after_stats.total_duration_s
    report.elapsed_s = time.monotonic() - t0

    if report.dropped_short:
        report.notes.append(
            f"{report.dropped_short} path(s) shorter than {min_len:g} mm were "
            f"dropped — raise paths.min_path_length_mm to keep them")
    if report.color_changes_after > max(len(order) - 1, 0):
        report.notes.append(
            f"colour changes ({report.color_changes_after}) exceed the "
            f"expected {max(len(order) - 1, 0)}")
    if report.travel_saved_mm < 0:
        report.notes.append(
            "ordering did not reduce travel; the input was already efficient")
    return result, report
