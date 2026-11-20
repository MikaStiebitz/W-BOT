"""Path optimisation and safe-area fitting."""

from __future__ import annotations

import numpy as np
import pytest

from wbbot.core.geometry import Drawing, Layer, Polyline
from wbbot.core.safearea import SafeArea, SafeAreaViolation
from wbbot.core.toolpath import Stroke, Toolpath
from wbbot.paths.fitting import fit_drawing, fit_toolpath
from wbbot.paths.optimize import merge_paths, optimize_toolpath, order_paths


def scattered_toolpath(n=60, seed=0, color=0) -> Toolpath:
    """Short segments in deliberately terrible order."""
    rng = np.random.default_rng(seed)
    strokes = []
    for _ in range(n):
        p0 = rng.uniform([100, 100], [1400, 900])
        p1 = p0 + rng.uniform(-40, 40, size=2)
        strokes.append(Stroke(color, Polyline.from_points([p0, p1])))
    return Toolpath(strokes)


# ----------------------------------------------------------------------
# merging
# ----------------------------------------------------------------------

def test_merge_joins_touching_ends():
    a = Polyline.from_points([[0, 0], [10, 0]])
    b = Polyline.from_points([[10, 0], [20, 0]])
    merged, joins = merge_paths([a, b], tolerance=0.5)
    assert joins == 1 and len(merged) == 1
    assert merged[0].length == pytest.approx(20.0)


def test_merge_flips_a_path_when_that_is_what_connects():
    a = Polyline.from_points([[0, 0], [10, 0]])
    b = Polyline.from_points([[20, 0], [10, 0]])       # runs backwards
    merged, joins = merge_paths([a, b], tolerance=0.5)
    assert joins == 1 and len(merged) == 1
    assert merged[0].length == pytest.approx(20.0)


def test_merge_respects_the_tolerance():
    a = Polyline.from_points([[0, 0], [10, 0]])
    b = Polyline.from_points([[12, 0], [22, 0]])
    assert merge_paths([a, b], tolerance=0.5)[1] == 0
    assert merge_paths([a, b], tolerance=3.0)[1] == 1


def test_merge_leaves_closed_rings_alone(square):
    merged, joins = merge_paths([square, square.translate(1, 0)], tolerance=50.0)
    assert joins == 0 and len(merged) == 2
    assert all(p.closed for p in merged)


def test_merge_chains_many_segments():
    pieces = [Polyline.from_points([[i * 10, 0], [(i + 1) * 10, 0]])
              for i in range(10)]
    merged, joins = merge_paths(pieces, tolerance=0.5)
    assert len(merged) == 1 and joins == 9
    assert merged[0].length == pytest.approx(100.0)


def test_merge_with_zero_tolerance_is_a_no_op():
    pieces = [Polyline.from_points([[0, 0], [1, 0]]),
              Polyline.from_points([[1, 0], [2, 0]])]
    assert merge_paths(pieces, tolerance=0.0)[1] == 0


# ----------------------------------------------------------------------
# ordering
# ----------------------------------------------------------------------

def test_ordering_reduces_travel_a_lot():
    tp = scattered_toolpath(80, seed=1)
    paths = [s.polyline for s in tp.strokes]
    ordered, _, before, after = order_paths(paths, (750, 950), time_budget_s=2.0)
    assert len(ordered) == len(paths)
    assert after < before * 0.5, f"travel only fell from {before:.0f} to {after:.0f}"


def test_ordering_preserves_every_path():
    tp = scattered_toolpath(40, seed=2)
    paths = [s.polyline for s in tp.strokes]
    ordered, _, _, _ = order_paths(paths, (750, 950), time_budget_s=1.0)
    before = sorted(round(p.length, 6) for p in paths)
    after = sorted(round(p.length, 6) for p in ordered)
    assert before == after


def test_ordering_may_reverse_open_paths_but_not_rings(square):
    paths = [square,
             Polyline.from_points([[1400, 900], [1300, 800]]),
             Polyline.from_points([[200, 200], [300, 250]])]
    ordered, _, _, _ = order_paths(paths, (750, 950), time_budget_s=1.0)
    rings = [p for p in ordered if p.closed]
    assert len(rings) == 1
    assert np.allclose(rings[0].points, square.points)


def test_ordering_respects_the_time_budget():
    import time
    tp = scattered_toolpath(400, seed=3)
    paths = [s.polyline for s in tp.strokes]
    t0 = time.monotonic()
    order_paths(paths, (750, 950), time_budget_s=0.3)
    assert time.monotonic() - t0 < 5.0


def test_ordering_empty_input():
    assert order_paths([], (0, 0)) == ([], 0, 0.0, 0.0)


# ----------------------------------------------------------------------
# full optimisation
# ----------------------------------------------------------------------

def test_optimize_reduces_travel_and_duration(cfg):
    tp = scattered_toolpath(80, seed=4)
    out, report = optimize_toolpath(tp, cfg, time_budget_s=2.0)
    assert report.travel_length_after_mm < report.travel_length_before_mm
    assert report.duration_after_s < report.duration_before_s
    assert report.travel_saved_percent > 20
    assert "paths" in report.summary_text()


def test_optimize_keeps_colour_blocks_contiguous(cfg):
    strokes = []
    rng = np.random.default_rng(9)
    for i in range(45):
        p0 = rng.uniform([200, 200], [1300, 800])
        strokes.append(Stroke(i % 3, Polyline.from_points([p0, p0 + [30, 20]])))
    tp = Toolpath(strokes)
    assert tp.color_change_count > 3
    out, report = optimize_toolpath(tp, cfg, time_budget_s=1.0)
    assert report.color_changes_after == 2, "must be exactly n_colours - 1"
    seen = [s.color_index for s in out.strokes]
    for colour in set(seen):
        idx = [i for i, c in enumerate(seen) if c == colour]
        assert idx == list(range(idx[0], idx[-1] + 1)), "colour block was split"


def test_optimize_honours_an_explicit_colour_order(cfg):
    strokes = [Stroke(c, Polyline.from_points([[200 + c * 100, 300],
                                               [260 + c * 100, 360]]))
               for c in (0, 1, 2)]
    out, _ = optimize_toolpath(Toolpath(strokes), cfg, color_order=[2, 0, 1])
    assert [s.color_index for s in out.strokes] == [2, 0, 1]


def test_optimize_drops_short_paths_and_says_so(cfg):
    strokes = [Stroke(0, Polyline.from_points([[300, 300], [300.5, 300]])),
               Stroke(0, Polyline.from_points([[400, 400], [700, 400]]))]
    out, report = optimize_toolpath(Toolpath(strokes), cfg,
                                    min_path_length_mm=3.0)
    assert report.dropped_short == 1
    assert len(out.strokes) == 1
    assert any("dropped" in n for n in report.notes)


def test_optimize_simplifies_dense_polylines(cfg):
    dense = Polyline.from_points(
        np.stack([np.linspace(300, 1200, 4000), np.full(4000, 500.0)], axis=1))
    out, report = optimize_toolpath(Toolpath([Stroke(0, dense)]), cfg)
    assert report.points_after < 10
    assert report.draw_length_after_mm == pytest.approx(900.0, rel=1e-3)


def test_optimize_merges_split_contours(cfg):
    pieces = [Stroke(0, Polyline.from_points([[300 + i * 50, 400],
                                              [300 + (i + 1) * 50, 400]]))
              for i in range(12)]
    out, report = optimize_toolpath(Toolpath(pieces), cfg, merge_tolerance_mm=1.0)
    assert report.merged == 11
    assert len(out.strokes) == 1


def test_optimize_of_an_empty_toolpath(cfg):
    out, report = optimize_toolpath(Toolpath([]), cfg)
    assert out.is_empty and report.paths_after == 0


def test_report_serialises(cfg):
    _, report = optimize_toolpath(scattered_toolpath(20, seed=5), cfg,
                                  time_budget_s=0.5)
    d = report.to_dict()
    import json
    json.dumps(d)
    assert d["paths_before"] == 20


# ----------------------------------------------------------------------
# fitting
# ----------------------------------------------------------------------

def test_fit_contains_a_small_drawing(safe_area):
    d = Drawing([Layer(0, [Polyline.from_points([[0, 0], [10, 0], [10, 10]],
                                                closed=True)])])
    fitted, result = fit_drawing(d, safe_area)
    assert result.scale > 1.0
    pts = np.vstack([p.drawn_points for p in fitted.all_paths])
    assert np.all(safe_area.contains_many(pts))


def test_fit_shrinks_an_oversized_drawing(safe_area):
    d = Drawing([Layer(0, [Polyline.from_points(
        [[-5000, -5000], [5000, -5000], [5000, 5000], [-5000, 5000]],
        closed=True)])])
    fitted, result = fit_drawing(d, safe_area)
    assert result.scale < 1.0
    pts = np.vstack([p.drawn_points for p in fitted.all_paths])
    assert np.all(safe_area.contains_many(pts))


def test_fit_preserves_the_aspect_ratio_in_contain_mode(safe_area):
    d = Drawing([Layer(0, [Polyline.from_points(
        [[0, 0], [200, 0], [200, 50], [0, 50]], closed=True)])])
    fitted, _ = fit_drawing(d, safe_area)
    x0, y0, x1, y1 = fitted.bounds
    assert (x1 - x0) / (y1 - y0) == pytest.approx(4.0, rel=1e-6)


def test_fit_alignment_moves_the_drawing(safe_area):
    d = Drawing([Layer(0, [Polyline.from_points([[0, 0], [100, 0], [100, 100]],
                                                closed=True)])])
    left, _ = fit_drawing(d, safe_area, mode="none", align=("left", "bottom"))
    right, _ = fit_drawing(d, safe_area, mode="none", align=("right", "top"))
    assert left.bounds[0] < right.bounds[0]
    assert left.bounds[1] < right.bounds[1]


def test_fit_verifies_against_the_polygon_not_the_bounding_box(cfg):
    """With a bitten-into safe area, a bbox fit would leak into the corners."""
    strict = cfg.replace(safe_area={"min_tension_n": 0.5, "board_margin_mm": 0.0})
    sa = SafeArea(strict)
    assert sa.polygon.area < cfg.board.width_mm * cfg.board.height_mm
    d = Drawing([Layer(0, [Polyline.from_points(
        [[0, 0], [100, 0], [100, 100], [0, 100]], closed=True)])])
    fitted, result = fit_drawing(d, sa, margin_mm=1.0)
    pts = np.vstack([p.drawn_points for p in fitted.all_paths])
    assert np.all(sa.contains_many(pts))


def test_fit_toolpath_matches_fit_drawing(safe_area, three_color_drawing):
    tp = Toolpath.from_drawing(three_color_drawing)
    fitted, result = fit_toolpath(tp, safe_area)
    assert np.all(safe_area.contains_many(fitted.all_points()))
    assert result.scale > 0


def test_fit_of_nothing_raises(safe_area):
    with pytest.raises(SafeAreaViolation, match="no points"):
        fit_drawing(Drawing([]), safe_area)


def test_fit_with_an_absurd_margin_raises(safe_area, three_color_drawing):
    with pytest.raises(SafeAreaViolation, match="no room"):
        fit_drawing(three_color_drawing, safe_area, margin_mm=5000.0)


def test_fit_result_serialises(safe_area, three_color_drawing):
    import json
    _, result = fit_drawing(three_color_drawing, safe_area)
    json.dumps(result.to_dict())


def test_optimized_and_fitted_drawing_generates_valid_gcode(cfg, safe_area):
    """The whole path stage, end to end."""
    from wbbot.core.gcode import GCodeWriter
    tp = scattered_toolpath(50, seed=6)
    fitted, _ = fit_toolpath(tp, safe_area)
    optimised, report = optimize_toolpath(fitted, cfg, time_budget_s=1.0)
    prog = GCodeWriter(cfg).render_toolpath(optimised)
    assert prog.text.isascii()
    assert report.paths_after > 0
