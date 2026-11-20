"""Polyline IR, transforms, simplification, motion planning."""

from __future__ import annotations

import math

import numpy as np
import pytest

from wbbot.core.geometry import (Drawing, GeometryError, Layer, Polyline,
                                 douglas_peucker, rotation, scaling, translation)
from wbbot.core.motion import Move, MotionLimits, plan, total_duration


# ----------------------------------------------------------------------
# Polyline
# ----------------------------------------------------------------------

def test_length_open_and_closed():
    p = Polyline.from_points([[0, 0], [3, 0], [3, 4]])
    assert p.length == pytest.approx(7.0)
    closed = Polyline.from_points([[0, 0], [3, 0], [3, 4]], closed=True)
    assert closed.length == pytest.approx(12.0)     # + the 5 mm closing edge


def test_drawn_points_appends_the_closing_vertex():
    closed = Polyline.from_points([[0, 0], [1, 0], [1, 1]], closed=True)
    assert len(closed.drawn_points) == 4
    assert np.allclose(closed.drawn_points[-1], [0, 0])


def test_rejects_bad_shapes_and_nan():
    with pytest.raises(GeometryError):
        Polyline(np.zeros((3, 3)))
    with pytest.raises(GeometryError):
        Polyline(np.array([[0.0, float("nan")]]))


def test_transform_roundtrip():
    p = Polyline.from_points([[10, 20], [30, 40]])
    m = translation(5, -5) @ rotation(math.pi / 3, (10, 10)) @ scaling(2.0, 3.0)
    back = p.transform(m).transform(np.linalg.inv(m))
    assert np.allclose(back.points, p.points, atol=1e-9)


def test_reverse_preserves_length():
    p = Polyline.from_points([[0, 0], [5, 0], [5, 5]])
    assert p.reversed().length == pytest.approx(p.length)
    assert np.allclose(p.reversed().points[0], p.points[-1])


def test_dedupe_removes_consecutive_duplicates():
    p = Polyline.from_points([[0, 0], [0, 0], [1, 0], [1, 0], [2, 0]])
    assert len(p.dedupe()) == 3


def test_resample_is_uniform():
    p = Polyline.from_points([[0, 0], [100, 0]])
    r = p.resample(10.0)
    d = np.linalg.norm(np.diff(r.points, axis=0), axis=1)
    assert np.allclose(d, 10.0)
    assert r.length == pytest.approx(100.0)


def test_resample_keeps_closed_rings_closed():
    ring = Polyline.from_points([[0, 0], [10, 0], [10, 10], [0, 10]], closed=True)
    r = ring.resample(2.0)
    assert r.closed
    assert r.length == pytest.approx(40.0, rel=1e-6)


# ----------------------------------------------------------------------
# Douglas-Peucker
# ----------------------------------------------------------------------

def test_douglas_peucker_collapses_a_straight_line():
    pts = np.stack([np.linspace(0, 100, 200), np.zeros(200)], axis=1)
    assert len(douglas_peucker(pts, 0.5)) == 2


def test_douglas_peucker_keeps_real_corners():
    pts = np.array([[0, 0], [50, 0], [50, 50], [100, 50]], dtype=float)
    assert len(douglas_peucker(pts, 0.5)) == 4


def test_douglas_peucker_respects_tolerance():
    pts = np.array([[0, 0], [50, 1.0], [100, 0]], dtype=float)
    assert len(douglas_peucker(pts, 2.0)) == 2      # bump below tolerance
    assert len(douglas_peucker(pts, 0.5)) == 3


def test_douglas_peucker_handles_huge_inputs_without_recursion_limits():
    rng = np.random.default_rng(0)
    pts = np.cumsum(rng.normal(size=(60_000, 2)), axis=0)
    out = douglas_peucker(pts, 0.5)
    assert 2 <= len(out) <= len(pts)


def test_simplify_preserves_closed_ring_validity():
    ring = Polyline.from_points(
        [[0, 0], [10, 0.01], [20, 0], [20, 20], [0, 20]], closed=True)
    s = ring.simplify(0.5)
    assert s.closed and len(s) >= 3


# ----------------------------------------------------------------------
# Drawing / Layer
# ----------------------------------------------------------------------

def test_drawing_aggregates(three_color_drawing):
    d = three_color_drawing
    assert d.n_paths == 3
    assert d.length > 0
    assert d.bounds is not None
    assert len(d.layers) == 3


def test_filter_drops_short_paths():
    d = Drawing([Layer(0, [Polyline.from_points([[0, 0], [0.5, 0]]),
                           Polyline.from_points([[0, 0], [100, 0]])])])
    assert d.filtered(min_length_mm=1.0).n_paths == 1


def test_non_empty_drops_degenerate_paths():
    d = Drawing([Layer(0, [Polyline.from_points([[1, 1]]),
                           Polyline.from_points([[0, 0], [10, 0]])]),
                 Layer(1, [Polyline.from_points([])])])
    cleaned = d.non_empty()
    assert cleaned.n_paths == 1 and len(cleaned.layers) == 1


def test_svg_export_is_wellformed(three_color_drawing):
    svg = three_color_drawing.to_svg(1500, 1000, colors=["#111", "#d33", "#15c"])
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert svg.count("<path") == 3
    assert "#d33" in svg


# ----------------------------------------------------------------------
# motion planning
# ----------------------------------------------------------------------

@pytest.fixture
def limits():
    return MotionLimits(acceleration_mm_s2=250.0, junction_deviation_mm=0.05)


def test_single_move_is_slower_than_the_naive_estimate(limits):
    m = Move((0, 0), (100, 0), feed_mm_min=1500.0)
    naive = 100.0 / (1500.0 / 60.0)
    assert total_duration([m], limits) > naive


def test_long_move_approaches_the_cruise_estimate(limits):
    m = Move((0, 0), (5000, 0), feed_mm_min=1500.0)
    naive = 5000.0 / (1500.0 / 60.0)
    t = total_duration([m], limits)
    assert naive < t < naive * 1.05


def test_collinear_moves_do_not_stop_between_them(limits):
    chain = [Move((0, 0), (100, 0), 1500.0), Move((100, 0), (200, 0), 1500.0)]
    single = [Move((0, 0), (200, 0), 1500.0)]
    assert total_duration(chain, limits) == pytest.approx(
        total_duration(single, limits), rel=1e-6)


def test_a_sharp_reversal_forces_a_stop(limits):
    planned = plan([Move((0, 0), (100, 0), 1500.0),
                    Move((100, 0), (0, 0), 1500.0)], limits)
    assert planned[0].exit_speed == pytest.approx(0.0, abs=1e-6)


def test_a_right_angle_slows_but_does_not_stop(limits):
    planned = plan([Move((0, 0), (100, 0), 1500.0),
                    Move((100, 0), (100, 100), 1500.0)], limits)
    assert 0.0 < planned[0].exit_speed < planned[0].cruise_speed


def test_discontinuity_forces_a_full_stop(limits):
    planned = plan([Move((0, 0), (100, 0), 1500.0),
                    Move((500, 500), (600, 500), 1500.0)], limits)
    assert planned[0].exit_speed == pytest.approx(0.0, abs=1e-9)
    assert planned[1].entry_speed == pytest.approx(0.0, abs=1e-9)


def test_zero_length_moves_are_dropped(limits):
    assert plan([Move((5, 5), (5, 5), 1500.0)], limits) == []


def test_speeds_never_exceed_the_feed_limit(limits):
    rng = np.random.default_rng(5)
    pts = rng.uniform(0, 500, size=(60, 2))
    moves = [Move(tuple(a), tuple(b), 1500.0)
             for a, b in zip(pts, pts[1:])]
    for pm in plan(moves, limits):
        assert pm.cruise_speed <= 1500.0 / 60.0 + 1e-9
        assert pm.entry_speed <= pm.cruise_speed + 1e-9
        assert pm.exit_speed <= 1500.0 / 60.0 + 1e-9
        assert pm.duration > 0


def test_zero_junction_deviation_stops_at_every_corner():
    strict = MotionLimits(acceleration_mm_s2=250.0, junction_deviation_mm=0.0)
    planned = plan([Move((0, 0), (100, 0), 1500.0),
                    Move((100, 0), (100, 100), 1500.0)], strict)
    assert planned[0].exit_speed == pytest.approx(0.0, abs=1e-9)
