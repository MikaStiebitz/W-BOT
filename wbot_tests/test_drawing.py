import json

import numpy as np
import pytest

from wbot.config import load_config
from wbot.drawing import (Drawing, Stroke, clean, clip_polyline, clip_to_rect, count_outside,
                          optimize_order, travel_length)
from wbot.svgimport import SvgError, SvgOptions, import_svg, svg_to_drawing

RECT = (0.0, 0.0, 100.0, 50.0)


def D(*strokes, name="t"):
    return Drawing([Stroke(p, c) for p, c in strokes], name)


def test_json_roundtrip_contract():
    d = D(([[0, 0], [1.23456, 2.0]], 1), ([[5, 5], [6, 7], [8, 1]], 0), name="x")
    j = d.to_json()
    assert set(j) == {"name", "strokes"}
    assert set(j["strokes"][0]) == {"color", "points"}
    assert j["strokes"][0]["points"][1] == [1.235, 2.0]       # rounded to 3 decimals
    assert j["strokes"][0]["color"] == 1
    back = Drawing.from_json(json.dumps(j))
    assert back.name == "x" and len(back) == 2 and back.strokes[0].color == 1
    assert np.allclose(back.strokes[1].pts, d.strokes[1].pts)
    assert Drawing.from_json(j).bounds() == [0.0, 0.0, 8.0, 7.0]


def test_json_rejects_garbage():
    for bad in ({}, {"strokes": 5}, {"strokes": [{"points": [[0, float("nan")]]}]}, {"strokes": [{"color": 1}]}):
        with pytest.raises(ValueError):
            Drawing.from_json(bad)


def test_bounds_is_list():
    d = D(([[1, 2], [5, 9]], 0))
    assert d.bounds() == [1.0, 2.0, 5.0, 9.0]
    assert Drawing().bounds() is None


def test_transform_and_fit():
    d = D(([[0, 0], [10, 0], [10, 5]], 0))
    t = d.transformed(scale=2, dx=3, dy=1)                    # about the bounds centre (5, 2.5)
    assert np.allclose(t.bounds(), [-5 + 3, -2.5 + 1, 15 + 3, 7.5 + 1])
    r = d.transformed(rotate_deg=90)
    assert np.isclose(r.bounds()[2] - r.bounds()[0], 5) and np.isclose(r.bounds()[3] - r.bounds()[1], 10)
    f = d.fit_to((0, 0, 100, 100), margin=10)
    assert np.allclose(f.bounds(), [10, 30, 90, 70])
    g = d.fit_to((0, 0, 100, 100), keep_aspect=False)
    assert np.allclose(g.bounds(), [0, 0, 100, 100])


def test_clip_polyline_splits():
    pts = np.array([[-10, 10], [50, 10], [50, 80], [60, 80], [60, 10], [110, 10]], float)
    pieces = clip_polyline(pts, RECT)
    assert len(pieces) == 2
    assert np.allclose(pieces[0], [[0, 10], [50, 10], [50, 50]])
    assert np.allclose(pieces[1], [[60, 50], [60, 10], [100, 10]])


def test_clip_to_rect_and_boundary_points_kept():
    d = D(([[0, 0], [100, 0], [100, 50], [0, 50], [0, 0]], 2), ([[200, 200], [300, 300]], 0))
    c = clip_to_rect(d, RECT)
    assert len(c) == 1 and c.strokes[0].color == 2 and len(c.strokes[0].pts) == 5
    assert count_outside(d, RECT) == 2 and count_outside(c, RECT) == 0


def test_clean_drops_zero_length():
    d = D(([[1, 1], [1, 1]], 0), ([[1, 1]], 0), ([[0, 0], [0, 0], [5, 0]], 0))
    c = clean(d)
    assert len(c) == 1 and len(c.strokes[0].pts) == 2


def test_optimize_groups_colors_and_reduces_travel():
    rng = np.random.default_rng(3)
    strokes = []
    for i in range(60):
        a = rng.uniform(0, 500, 2)
        strokes.append((np.array([a, a + rng.uniform(-20, 20, 2) + [5, 5]]), i % 3))
    d = D(*strokes)
    o = optimize_order(d, (0, 0), merge_tol_mm=0)
    cols = [s.color for s in o.strokes]
    assert cols == sorted(cols)                                    # each colour once, contiguous
    assert len(o) == len(d)
    assert np.isclose(o.length(), d.length())                      # strokes only reordered/flipped
    assert travel_length(o.strokes, (0, 0)) < travel_length(d.strokes, (0, 0)) * 0.5
    again = optimize_order(d, (0, 0), merge_tol_mm=0)
    assert [s.pts.tolist() for s in o.strokes] == [s.pts.tolist() for s in again.strokes]   # deterministic


def test_optimize_flips_and_merges():
    d = D(([[0, 0], [10, 0]], 0), ([[20, 0], [10.02, 0]], 0), ([[100, 0], [30, 0]], 0))
    o = optimize_order(d, (0, 0), merge_tol_mm=0.05)
    assert len(o) == 2
    assert np.allclose(o.strokes[0].pts[0], [0, 0]) and np.allclose(o.strokes[0].pts[-1], [20, 0])
    assert np.allclose(o.strokes[1].pts[0], [30, 0])
    assert len(optimize_order(d, (0, 0), merge_tol_mm=0)) == 3


# ----------------------------------------------------------------------------- SVG
SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="100mm" height="50mm" viewBox="0 0 100 50">'
       '<g transform="translate(10,5)">'
       '<path d="M0 0 L 10 0 h5 v5 z" stroke="#d32f2f" fill="none"/>'
       '<circle cx="50" cy="25" r="10" stroke="blue" fill="none"/>'
       '<rect x="1" y="1" width="20" height="10" rx="3" stroke="black" fill="none"/>'
       '<line x1="0" y1="0" x2="5" y2="5" stroke="white"/>'
       '</g></svg>')


def test_svg_basic_units_colors_flip():
    r = import_svg(SVG, load_config())
    assert (r.width_mm, r.height_mm) == (100.0, 50.0)
    assert len(r.drawing) == 3                                     # white line skipped
    assert sorted(s.color for s in r.drawing.strokes) == [0, 1, 2]
    b = r.drawing.bounds()
    assert b[3] == pytest.approx(45.0) and b[0] == pytest.approx(10.0)     # y flipped to board orientation


def test_svg_units_without_viewbox():
    d = svg_to_drawing('<svg xmlns="http://www.w3.org/2000/svg" width="2in" height="1in">'
                       '<line x1="0" y1="0" x2="1" y2="0" stroke="black"/></svg>')
    assert d.length() == pytest.approx(25.4)


def test_svg_circle_is_round_within_tolerance():
    d = svg_to_drawing('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100mm" height="100mm">'
                       '<circle cx="50" cy="50" r="40" stroke="black" fill="none"/></svg>')
    p = d.strokes[0].pts
    assert np.all(np.abs(np.hypot(p[:, 0] - 50, p[:, 1] - 50) - 40) < 0.1)
    mid = (p[:-1] + p[1:]) / 2
    assert np.all(40 - np.hypot(mid[:, 0] - 50, mid[:, 1] - 50) < 0.1)     # chord sag within tolerance


def test_svg_path_commands_all():
    path = ("M10 10 H50 V30 h-10 v-5 C60 30 70 40 80 30 S100 20 110 30 Q120 10 130 30 T150 30 "
            "A10 10 0 0 1 170 30 a5 5 0 1 0 10 0 Z")
    d = svg_to_drawing('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 100" width="200mm" height="100mm">'
                       f'<path d="{path}" stroke="black" fill="none"/></svg>')
    assert len(d) == 1 and d.strokes[0].is_closed()
    assert d.bounds()[2] == pytest.approx(180, abs=0.2)


def test_svg_compact_arc_flags_and_transforms():
    d = svg_to_drawing('<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
                       '<g transform="scale(2) rotate(90)"><path d="M0 0a5 5 0 00.5.5" stroke="black"/></g></svg>')
    assert len(d) == 1
    d2 = svg_to_drawing('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" width="10mm" height="10mm">'
                        '<polyline points="0,0 10,0 10,10" transform="matrix(1 0 0 1 0 0) translate(0 0)" '
                        'stroke="#000"/></svg>')
    assert np.allclose(d2.strokes[0].pts[0], [0, 10]) and np.allclose(d2.strokes[0].pts[-1], [10, 0])


def test_svg_color_map_and_rejects():
    s = SVG.replace('stroke="blue"', 'stroke="#123456"')
    r = import_svg(s, None, SvgOptions(color_map={"#123456": 2}))
    assert 2 in {x.color for x in r.drawing.strokes}
    with pytest.raises(SvgError):
        import_svg("<svg", None)
    with pytest.raises(SvgError):
        import_svg(b"x" * 100, None, SvgOptions(max_bytes=10))
    with pytest.raises(SvgError):
        import_svg('<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "b">]><svg xmlns="http://www.w3.org/2000/svg"/>')
    with pytest.raises(SvgError):
        import_svg(SVG, None, SvgOptions(max_points=5))
    with pytest.raises(SvgError):
        import_svg('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><path d="M0 0 L"/></svg>')
