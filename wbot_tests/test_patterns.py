import json
import math

import numpy as np
import pytest

from wbot.config import deep_update, load_config
from wbot.drawing import count_outside
from wbot.patterns import REGISTRY, build_pattern, pattern_to_dict
from wbot.textfont import measure, text_strokes

CFG = load_config()
RECT = CFG.safe_rect()


def test_registry_has_at_least_twelve_documented_patterns():
    assert len(REGISTRY) >= 12
    for pid in ("calibration-grid", "crosshair-points", "square-frame", "diagonals", "circles", "circle-accuracy",
                "spiral", "star", "corner-marks", "ruler", "text", "color-test", "pen-test", "speed-test",
                "full-board-test"):
        assert pid in REGISTRY
    for p in REGISTRY.values():
        assert p.name and p.description and p.id in REGISTRY
        ids = [q.id for q in p.params]
        assert len(ids) == len(set(ids))


@pytest.mark.parametrize("pid", sorted(REGISTRY))
def test_default_build_inside_safe_rect_and_nonempty(pid):
    d = build_pattern(CFG, pid)
    assert len(d) > 0 and d.length() > 0
    assert count_outside(d, RECT, eps=1e-9) == 0
    assert all(len(s.pts) >= 2 for s in d.strokes)
    assert all(0 <= s.color < len(CFG.colors) for s in d.strokes)
    json.dumps(d.to_json())
    json.dumps(pattern_to_dict(REGISTRY[pid]))


@pytest.mark.parametrize("pid", sorted(REGISTRY))
def test_defaults_fit_without_clipping(pid, monkeypatch):
    """The defaults must be designed for the safe rect, not rescued by the last-resort clip."""
    monkeypatch.setattr("wbot.patterns.clip_to_rect", lambda d, r: d)
    assert count_outside(build_pattern(CFG, pid), RECT, eps=1e-9) == 0


@pytest.mark.parametrize("pid", sorted(REGISTRY))
def test_param_extremes_stay_inside_safe_rect(pid):
    pat = REGISTRY[pid]
    for pick in ("min", "max"):
        vals = {q.id: getattr(q, pick) for q in pat.params
                if q.kind in ("float", "int") and getattr(q, pick) is not None}
        d = build_pattern(CFG, pid, vals)
        assert count_outside(d, RECT, eps=1e-9) == 0


def test_pattern_to_dict_shape():
    d = pattern_to_dict(REGISTRY["circle-accuracy"])
    assert set(d) == {"id", "name", "description", "params"}
    assert set(d["params"][0]) == {"id", "label", "kind", "default", "min", "max", "choices", "unit"}
    ch = [p for p in d["params"] if p["kind"] == "choice"][0]
    assert ch["choices"][0].keys() == {"value", "label"}


def test_param_validation_errors():
    with pytest.raises(ValueError, match="must be between"):
        build_pattern(CFG, "circles", {"count": 99})
    with pytest.raises(ValueError, match="Unknown parameter"):
        build_pattern(CFG, "circles", {"nope": 1})
    with pytest.raises(ValueError, match="Unknown test pattern"):
        build_pattern(CFG, "nope")
    with pytest.raises(ValueError):
        build_pattern(CFG, "circles", {"count": "abc"})
    with pytest.raises(ValueError):
        build_pattern(CFG, "circle-accuracy", {"squares": "triangle"})
    with pytest.raises(ValueError):
        build_pattern(CFG, "text", {"text": "x" * 500})
    assert len(build_pattern(CFG, "circles", {"count": "3"})) == 3          # strings coerce (HTTP forms)


def test_circle_accuracy_geometry():
    d = build_pattern(CFG, "circle-accuracy", {"diameter_mm": 400, "axes": False})
    cx, cy = (RECT[0] + RECT[2]) / 2, (RECT[1] + RECT[3]) / 2          # patterns centre on the safe rect
    circle = max(d.strokes, key=lambda s: len(s.pts))
    r = np.hypot(circle.pts[:, 0] - cx, circle.pts[:, 1] - cy)
    assert np.allclose(r, 200, atol=1e-6)
    assert len(d) == 3


def test_crosshair_contains_corners_and_centre():
    d = build_pattern(CFG, "crosshair-points", {"cols": 5, "rows": 3, "arm_mm": 5})
    centres = {(round(float(s.pts[:, 0].mean()), 3), round(float(s.pts[:, 1].mean()), 3)) for s in d.strokes}
    x0, y0, x1, y1 = RECT
    for c in [(x0 + 5, y0 + 5), (x1 - 5, y1 - 5), (x0 + 5, y1 - 5), (x1 - 5, y0 + 5), ((x0 + x1) / 2, (y0 + y1) / 2)]:
        assert c in centres


def test_calibration_grid_lines_at_multiples():
    d = build_pattern(CFG, "calibration-grid", {"spacing_mm": 250, "labels": False, "frame": False})
    xs = sorted({round(float(s.pts[0, 0]), 3) for s in d.strokes if abs(s.pts[0, 0] - s.pts[-1, 0]) < 1e-9})
    assert xs == [250, 500, 750, 1000, 1250]


def test_color_test_uses_every_color():
    d = build_pattern(CFG, "color-test")
    assert d.colors() == [0, 1, 2]


def test_text_umlauts_and_fit():
    d = build_pattern(CFG, "text", {"text": "Größe äöü ÄÖÜ ß", "size_mm": 40})
    assert len(d) > 10 and count_outside(d, RECT, eps=1e-9) == 0
    long = build_pattern(CFG, "text", {"text": "W" * 100, "size_mm": 100})        # shrinks to fit width
    b = long.bounds()
    assert b[0] >= RECT[0] - 1e-6 and b[2] <= RECT[2] + 1e-6
    w, h = measure("HELLO", 20)
    s = text_strokes("HELLO", 20, 0, 0)
    pts = np.concatenate([x.pts for x in s])
    assert pts[:, 1].max() == pytest.approx(20.0, abs=0.01) and pts[:, 0].max() <= w + 1e-6


def test_other_board_size_still_inside_safe_rect():
    cfg = deep_update(CFG, {"board": {"width_mm": 600, "height_mm": 400}})
    for pid in REGISTRY:
        d = build_pattern(cfg, pid)
        assert count_outside(d, cfg.safe_rect(), eps=1e-9) == 0
