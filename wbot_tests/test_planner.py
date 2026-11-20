import re

import numpy as np
import pytest

from wbot.config import load_config
from wbot.drawing import Drawing, Stroke
from wbot.kinematics import mapping_for
from wbot.patterns import build_pattern
from wbot.planner import MAX_LINE, PlanError, PlanOptions, plan
from wbot.servo import ServoLimitError, ServoModel

CFG = load_config()
RECT = CFG.safe_rect()
PEN = ServoModel(CFG.servos.pen)
REV = ServoModel(CFG.servos.revolver)
Z_UP, Z_DOWN = f"{PEN.axis_value(CFG.servos.pen.up_deg):.3f}", f"{PEN.axis_value(CFG.servos.pen.down_deg):.3f}"
XY_RE = re.compile(r"^G1 X(-?\d+\.\d{3}) Y(-?\d+\.\d{3})(?: F(\d+))?$")

MAPPINGS = {
    "centre-ref": lambda: mapping_for(CFG, (750, 500), (750, 500)),
    "offset-ref": lambda: mapping_for(CFG, (600, 300), (603.4, 296.9)),
}


def D(*strokes):
    return Drawing([Stroke(np.array(p, float), c) for p, c in strokes])


def multi_color_drawing():
    return D(([[250, 100], [350, 120]], 0), ([[450, 300], [550, 330]], 2), ([[300, 500], [400, 520]], 1),
             ([[270, 110], [370, 140]], 2), ([[650, 500], [750, 510]], 0), ([[850, 100], [950, 150]], 1))


def check_servo_order(pl):
    """Pen up + dwell before every revolver line; never mixed axes in a line."""
    pen_is_up = None
    for i, l in enumerate(pl.lines):
        t = l.text
        assert len(t) <= MAX_LINE
        assert not ("Z" in t and "A" in t.replace("G", "")) or t.startswith("G4")
        if t.startswith("G1 Z"):
            assert "X" not in t and "Y" not in t and "A" not in t
            pen_is_up = t.split()[1] == f"Z{Z_UP}"
            assert t.split()[1] in (f"Z{Z_UP}", f"Z{Z_DOWN}")
        elif t.startswith("G1 A"):
            assert pen_is_up is True, "revolver moved while the pen may be down"
            assert pl.lines[i - 1].text.startswith("G4"), "no dwell between pen up and revolver"
            assert float(pl.lines[i - 1].text.split("P")[1]) >= 0.3
            assert pl.lines[i + 1].text.startswith("G4"), "no dwell after revolver"
            assert not l.pen_down
            assert "X" not in t and "Z" not in t
        elif XY_RE.match(t) and l.kind == "draw" and l.pen_down:
            assert pen_is_up is False
    assert pen_is_up is True                                # never ends with the pen down


def replay(pl, mapping):
    """Replay G1 XY like FluidNC (1 mm segments in firmware space) -> physical polylines."""
    runs, cur_fw = [], None
    for l in pl.lines:
        m = XY_RE.match(l.text)
        if not m:
            continue
        fw = np.array([float(m.group(1)), float(m.group(2))])
        if cur_fw is not None:
            n = max(1, int(np.ceil(np.linalg.norm(fw - cur_fw) / 1.0)))
            seg = cur_fw + (fw - cur_fw) * np.linspace(0, 1, n + 1)[:, None]
            runs.append((mapping.to_physical(seg), l.kind == "draw" and bool(l.pen_down)))
        cur_fw = fw
    return runs


def drawn_geometry(runs):
    from shapely.geometry import LineString, MultiLineString
    return MultiLineString([LineString(r) for r, down in runs if down and len(r) >= 2])


def max_dev(pl, drawing, mapping):
    import shapely
    from shapely.geometry import LineString, MultiLineString, Point
    runs = replay(pl, mapping)
    got = drawn_geometry(runs)
    ref = MultiLineString([LineString(s.pts) for s in drawing.strokes])
    pts = np.concatenate([r for r, down in runs if down])
    d1 = shapely.distance(shapely.points(pts), ref).max()
    dense = []
    for s in drawing.strokes:
        L = s.length()
        t = np.linspace(0, L, max(2, int(L / 0.5)))
        cum = np.concatenate([[0], np.cumsum(np.hypot(*np.diff(s.pts, axis=0).T))])
        dense.append(np.column_stack([np.interp(t, cum, s.pts[:, 0]), np.interp(t, cum, s.pts[:, 1])]))
    d2 = shapely.distance(shapely.points(np.concatenate(dense)), got).max()
    return max(d1, d2)


@pytest.mark.parametrize("mname", sorted(MAPPINGS))
@pytest.mark.parametrize("pid,params", [
    ("circle-accuracy", {}), ("star", {}), ("spiral", {"turns": 3}), ("crosshair-points", {}),
    ("text", {"text": "Test 123", "size_mm": 30}), ("square-frame", {}), ("diagonals", {}),
    ("calibration-grid", {"spacing_mm": 250, "labels": False}), ("pen-test", {}),
])
def test_physical_path_deviation_below_0_05mm(pid, params, mname):
    m = MAPPINGS[mname]()
    d = build_pattern(CFG, pid, params)
    pl = plan(d, CFG, m)
    assert not pl.warnings
    assert max_dev(pl, d, m) < 0.05


def test_pen_up_before_every_revolver_line_all_patterns():
    m = MAPPINGS["centre-ref"]()
    for pid in ("color-test", "full-board-test"):
        pl = plan(build_pattern(CFG, pid), CFG, m)
        check_servo_order(pl)
        assert pl.stats["color_changes"] >= 2
    check_servo_order(plan(multi_color_drawing(), CFG, m, PlanOptions(optimize=False)))
    check_servo_order(plan(multi_color_drawing(), CFG, m, PlanOptions(dry_run=True)))


def test_unoptimised_order_changes_color_more_often():
    m = MAPPINGS["centre-ref"]()
    a = plan(multi_color_drawing(), CFG, m)
    b = plan(multi_color_drawing(), CFG, m, PlanOptions(optimize=False))
    assert a.stats["color_changes"] == 2
    assert b.stats["color_changes"] > a.stats["color_changes"]
    assert sum(l.kind == "color" for l in a.lines) == 3        # initial select + 2 changes
    slots = [l.color for l in a.lines if l.kind == "color"]
    assert slots == [0, 1, 2]


def test_no_point_outside_safe_rect():
    m = MAPPINGS["offset-ref"]()
    for pid in ("full-board-test", "ruler", "calibration-grid"):
        pl = plan(build_pattern(CFG, pid), CFG, m)
        for l in pl.lines:
            if l.phys is not None:
                assert RECT[0] - 1e-6 <= l.phys[0] <= RECT[2] + 1e-6 and RECT[1] - 1e-6 <= l.phys[1] <= RECT[3] + 1e-6
        for pts, _ in replay(pl, m):
            assert pts[:, 0].min() >= RECT[0] - 0.05 and pts[:, 0].max() <= RECT[2] + 0.05
            assert pts[:, 1].min() >= RECT[1] - 0.05 and pts[:, 1].max() <= RECT[3] + 0.05


def test_clip_warning_and_strict_mode():
    m = MAPPINGS["centre-ref"]()
    d = D(([[-50, 500], [750, 500], [1600, 500]], 0))
    pl = plan(d, CFG, m)
    assert pl.warnings and "clipped" in pl.warnings[0]
    for l in pl.lines:
        if l.phys:
            assert RECT[0] - 1e-6 <= l.phys[0] <= RECT[2] + 1e-6
    with pytest.raises(PlanError):
        plan(d, CFG, m, PlanOptions(clip=False))
    plan(D(([[RECT[0], RECT[1]], [RECT[2], RECT[3]]], 0)), CFG, m, PlanOptions(clip=False))   # exactly on the edge is fine


def test_deterministic_and_line_format():
    m = MAPPINGS["centre-ref"]()
    d = build_pattern(CFG, "full-board-test")
    a, b = plan(d, CFG, m), plan(d, CFG, m)
    assert a.gcode() == b.gcode() and a.estimated_s == b.estimated_s
    assert a.lines[0].text == "G21" and a.lines[1].text == "G90"
    assert a.lines[2].text.startswith("G1 Z") and a.lines[3].text.startswith("G4 P")
    pat = re.compile(r"^(G21|G90|G4 P\d+\.\d{3}|G1 [ZA]-?\d+\.\d{3} F\d+|G1 X-?\d+\.\d{3} Y-?\d+\.\d{3}( F\d+)?)$")
    prev_resets_f = True
    for l in a.lines:
        assert pat.match(l.text), l.text
        mm = XY_RE.match(l.text)
        if mm:
            if prev_resets_f:
                assert mm.group(3), "F must be restated after servo/dwell: " + l.text
            prev_resets_f = False
        elif l.text.startswith(("G4", "G1 Z", "G1 A")):
            prev_resets_f = True
    assert a.stats["lines"] == len(a.lines) and a.stats["bytes"] == sum(len(l.text) + 1 for l in a.lines)
    assert a.stats["color_changes"] == 2 and a.stats["strokes"] == len(a.lines and
                                                                       [1 for l in a.lines if l.kind == "pen" and Z_DOWN in l.text])


def test_dry_run_same_xy_pen_never_down():
    m = MAPPINGS["offset-ref"]()
    d = build_pattern(CFG, "circle-accuracy")
    real, dry = plan(d, CFG, m), plan(d, CFG, m, PlanOptions(dry_run=True))
    xy = lambda p: [(float(XY_RE.match(l.text).group(1)), float(XY_RE.match(l.text).group(2)))
                    for l in p.lines if XY_RE.match(l.text)]
    assert xy(real) == xy(dry)
    assert not any(l.pen_down for l in dry.lines)
    assert not any(l.text == f"G1 Z{Z_DOWN} F30000" for l in dry.lines)
    assert [l.kind for l in dry.lines].count("draw") == [l.kind for l in real.lines].count("draw")


def test_start_and_end_pen_up_and_park():
    m = MAPPINGS["centre-ref"]()
    pl = plan(D(([[200, 100], [300, 100]], 0)), CFG, m)
    zlines = [l for l in pl.lines if l.text.startswith("G1 Z")]
    assert zlines[0].text.startswith(f"G1 Z{Z_UP}") and zlines[-1].text.startswith(f"G1 Z{Z_UP}")
    last = pl.lines[-1]
    assert last.kind == "move" and last.phys == pytest.approx(CFG.motion.park_position)
    assert not last.pen_down
    nopark = plan(D(([[200, 100], [300, 100]], 0)), CFG, m, PlanOptions(park_at_end=False))
    assert nopark.lines[-1].kind != "move"


def test_current_slot_skips_first_revolver_move():
    m = MAPPINGS["centre-ref"]()
    d = D(([[200, 100], [300, 100]], 1))
    assert any(l.kind == "color" for l in plan(d, CFG, m).lines)
    assert not any(l.kind == "color" for l in plan(d, CFG, m, PlanOptions(current_slot=1)).lines)


def test_color_map_and_bad_slot():
    m = MAPPINGS["centre-ref"]()
    d = D(([[200, 100], [300, 100]], 0))
    pl = plan(d, CFG, m, PlanOptions(color_map={0: 2}))
    assert [l.color for l in pl.lines if l.kind == "color"] == [2]
    assert f"A{REV.axis_value(60.0):.3f}" in [l.text.split()[1] for l in pl.lines if l.kind == "color"]
    with pytest.raises(PlanError):
        plan(d, CFG, m, PlanOptions(color_map={0: 7}))


def test_servo_limit_error_propagates(monkeypatch):
    m = MAPPINGS["centre-ref"]()

    def boom(self, a):
        raise ServoLimitError("x")
    monkeypatch.setattr(ServoModel, "axis_value", boom)
    with pytest.raises(ServoLimitError):
        plan(D(([[200, 100], [300, 100]], 0)), CFG, m)


def test_estimated_time_plausible():
    m = MAPPINGS["centre-ref"]()
    pl = plan(D(([[200, 500], [300, 500]], 0)), CFG, m,
              PlanOptions(start_phys=(200, 500), park_at_end=False))
    v, a = CFG.motion.draw_feed_mm_min / 60, CFG.motion.acceleration_mm_s2
    move = 100 / v + v / a                                   # trapezoid: cruise + accel/decel
    dwells = sum(float(l.text.split("P")[1]) for l in pl.lines if l.kind == "dwell")
    assert pl.estimated_s >= move + dwells
    assert pl.estimated_s <= move + dwells + 3.0
    slow = plan(D(([[200, 500], [300, 500]], 0)), CFG, m, PlanOptions(start_phys=(200, 500), speed_scale=0.5,
                                                                      park_at_end=False))
    assert slow.estimated_s > pl.estimated_s
    assert "F750" in "\n".join(slow.gcode())


def test_corners_slow_the_estimate():
    m = MAPPINGS["centre-ref"]()
    straight = plan(D(([[200, 500], [600, 500]], 0)), CFG, m, PlanOptions(park_at_end=False))
    zig = plan(D(([[200, 500], [300, 510], [400, 490], [500, 510], [600, 500]], 0)), CFG, m,
               PlanOptions(park_at_end=False))
    assert zig.estimated_s > straight.estimated_s


def test_empty_drawing_is_safe():
    pl = plan(Drawing(), CFG, MAPPINGS["centre-ref"]())
    assert pl.warnings and pl.stats["strokes"] == 0
    assert not any(l.text.startswith("G1 Z") and Z_DOWN in l.text for l in pl.lines)


def test_stats_lengths():
    m = MAPPINGS["centre-ref"]()
    pl = plan(D(([[200, 100], [300, 100]], 0), ([[400, 100], [400, 200]], 0)), CFG, m, PlanOptions(start_phys=(200, 100)))
    assert pl.stats["strokes"] == 2
    assert pl.stats["draw_mm"] == pytest.approx(200.0, abs=0.1)
    assert pl.stats["travel_mm"] > 100
