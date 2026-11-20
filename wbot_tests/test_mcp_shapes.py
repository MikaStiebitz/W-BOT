"""Geometry builders and PNG renderer behind the MCP tools (pure functions, no machine)."""
import io

import numpy as np
import pytest
from PIL import Image

from wbot.config import load_config
from wbot.drawing import Drawing, Stroke
from wbot.mcp_render import render_png
from wbot.mcp_shapes import ShapeError, ShapeSpec, build_shape, build_text, hatch_lines

CFG = load_config()
RECT = CFG.safe_rect()


def spec(**kw) -> ShapeSpec:
    return ShapeSpec(**kw)


def pts(strokes):
    return np.concatenate([s.pts for s in strokes])


def test_rect_circle_ellipse_arc_geometry():
    r = build_shape(spec(kind="rect", x=200, y=100, width=50, height=30), 0, RECT)[0]
    assert r.is_closed() and r.pts.min(0).tolist() == [200, 100] and r.pts.max(0).tolist() == [250, 130]
    c = build_shape(spec(kind="circle", x=500, y=400, radius=80), 1, RECT)[0]
    assert c.color == 1 and c.is_closed(1e-6)
    d = np.hypot(*(c.pts - [500, 400]).T)
    assert d.min() == pytest.approx(80, abs=1e-6) and d.max() == pytest.approx(80, abs=1e-6)
    e = build_shape(spec(kind="ellipse", x=500, y=400, rx=100, ry=40), 0, RECT)[0]
    assert np.ptp(e.pts[:, 0]) == pytest.approx(200, abs=0.1) and np.ptp(e.pts[:, 1]) == pytest.approx(80, abs=0.1)
    e = build_shape(spec(kind="ellipse", x=500, y=400, rx=100, ry=40, rotation_deg=90), 0, RECT)[0]
    assert np.ptp(e.pts[:, 0]) == pytest.approx(80, abs=0.1)
    a = build_shape(spec(kind="arc", x=500, y=400, radius=100, start_deg=0, end_deg=90), 0, RECT)[0]
    assert a.pts[0] == pytest.approx([600, 400]) and a.pts[-1] == pytest.approx([500, 500]) and not a.is_closed()


def test_polygon_regular_line_polyline():
    tri = build_shape(spec(kind="polygon", x=500, y=400, radius=50, sides=3), 0, RECT)[0]
    assert len(tri.pts) == 4 and tri.is_closed() and tri.pts[0] == pytest.approx([500, 450])   # apex up
    closed = build_shape(spec(kind="polygon", points=[[0, 0], [10, 0], [10, 10]]), 0, RECT)[0]
    assert closed.is_closed()
    ln = build_shape(spec(kind="line", points=[[100, 100], [300, 200], [999, 999]]), 0, RECT)[0]
    assert len(ln.pts) == 2
    assert len(build_shape(spec(kind="polyline", points=[[1, 1], [2, 2], [3, 1]]), 0, RECT)[0].pts) == 3


def test_fill_hatches_inside_shape_and_serpentine():
    out = build_shape(spec(kind="rect", x=300, y=300, width=100, height=50, fill=True, hatch_spacing_mm=5,
                           hatch_angle_deg=0), 0, RECT)
    hatch = out[1:]
    assert len(hatch) == 10                                            # 50 mm / 5 mm
    p = pts(hatch)
    assert p[:, 0].min() >= 300 - 1e-6 and p[:, 0].max() <= 400 + 1e-6 and p[:, 1].min() >= 300
    assert hatch[0].start[0] < hatch[0].end[0] and hatch[1].start[0] > hatch[1].end[0]   # alternating direction
    circ = build_shape(spec(kind="circle", x=600, y=400, radius=40, fill=True, hatch_spacing_mm=4), 0, RECT)[1:]
    assert circ and np.hypot(*(pts(circ) - [600, 400]).T).max() <= 40.01
    pie = build_shape(spec(kind="arc", x=600, y=400, radius=50, start_deg=0, end_deg=90, fill=True), 0, RECT)
    assert len(pie) > 3 and pts(pie).min(0)[0] >= 600 - 1e-6


def test_hatch_is_clipped_to_the_usable_rect_and_cheap():
    big = np.array([[0, 0], [5000, 0], [5000, 5000], [0, 5000], [0, 0]], float)
    lines = hatch_lines(big, RECT, 5.0, 45.0)
    p = np.concatenate(lines)
    assert p[:, 0].min() >= RECT[0] - 1e-6 and p[:, 1].max() <= RECT[3] + 1e-6
    assert 100 < len(lines) < 600


@pytest.mark.parametrize("kw,msg", [
    (dict(kind="circle", x=1, y=1), "needs"),
    (dict(kind="circle", x=1, y=1, radius=-5), "radius"),
    (dict(kind="rect", x=1, y=1, width=0, height=4), "> 0"),
    (dict(kind="polygon", x=1, y=1, radius=5, sides=2), "polygon needs"),
    (dict(kind="line", points=[[1, 1]]), "at least 2"),
    (dict(kind="arc", x=1, y=1, radius=5, start_deg=0, end_deg=0), "arc needs"),
    (dict(kind="circle", x=1e9, y=1, radius=5), "sensible"),
    (dict(kind="line", points=[[1, 1], [2]]), "pairs"),
    (dict(kind="line", points=[[0, 0], [1, 1]], fill=True), "cannot be filled"),
    (dict(kind="rect", x=1, y=1, width=5, height=5, fill=True, hatch_spacing_mm=0.2), "hatch_spacing_mm"),
])
def test_invalid_shapes_are_rejected_with_a_reason(kw, msg):
    with pytest.raises(ShapeError, match=msg):
        build_shape(spec(**kw), 0, RECT)


def test_text_anchor_and_validation():
    strokes, (w, h) = build_text("HALLO", 700, 400, 40, 2, "center", "center", 1.0)
    p = pts(strokes)
    assert all(s.color == 2 for s in strokes) and h == 40
    assert (p[:, 0].min() + p[:, 0].max()) / 2 == pytest.approx(700, abs=1.0)
    assert (p[:, 1].min() + p[:, 1].max()) / 2 == pytest.approx(400, abs=1.0)
    s2, (_, h2) = build_text("A\nB", 100, 100, 20, 0, "left", "baseline", 1.0)
    assert h2 > 20 and pts(s2)[:, 0].min() >= 100 - 1e-6
    for bad in (dict(text="  "), dict(text="x", height_mm=1), dict(text="x", align="middle"),
                dict(text="x" * 501)):
        kw = dict(text="x", x=0, y=0, height_mm=20, color=0, align="left", valign="top", line_spacing=1.0) | bad
        with pytest.raises(ShapeError):
            build_text(**kw)


def test_render_png_content_and_colours():
    d = Drawing([Stroke([[300, 300], [900, 600]], 0), Stroke([[300, 600], [900, 300]], 1),
                 Stroke([[300, 450], [900, 450]], 2)])
    png = render_png(d, CFG, pen_xy=(200, 200))
    im = Image.open(io.BytesIO(png)).convert("RGB")
    assert 900 <= im.width <= 1200 and im.height > 600
    cols = {c for _, c in im.getcolors(1 << 20)}
    assert any(r > 190 and g < 80 and b < 80 for r, g, b in cols)             # red stroke
    assert any(b > 150 and r < 60 for r, g, b in cols)                        # blue stroke
    empty = render_png(Drawing(), CFG)
    assert Image.open(io.BytesIO(empty)).size == im.size and len(empty) < len(png)
