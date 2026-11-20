"""Camera: geometry, detection on synthetic frames, API, tracking and the automatic calibration (simulator only)."""
import math
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient

from wbot.api.app import create_app
from wbot.camera import detect
from wbot.camera.geometry import BoardCamera, CameraGeometryError, distort_px, undistort_px
from wbot.camera.simcam import SimCamera
from wbot.config import load_config
from wbot.machine import Machine

MARKERS = [{"id": 0, "x": 50.0, "y": 950.0}, {"id": 1, "x": 1450.0, "y": 950.0},
           {"id": 2, "x": 1450.0, "y": 50.0}, {"id": 3, "x": 50.0, "y": 50.0}]


class Seg:
    def __init__(self, x0, y0, x1, y1, down=True, slot=0):
        self.x0, self.y0, self.x1, self.y1, self.pen_down, self.color_slot = x0, y0, x1, y1, down, slot


def cross(x, y, a=20.0):
    return [Seg(x - a, y, x + a, y), Seg(x, y - a, x, y + a)]


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture(scope="module")
def simcam(cfg):
    cam = SimCamera(cfg, seed=3)
    cam.set_markers([{**m, "size_mm": 60.0} for m in MARKERS], {"id": 10, "size_mm": 50.0, "offset_mm": [0, 90], "height_mm": 36})
    return cam


# ------------------------------------------------------------------ geometry
def test_homography_roundtrip_and_parallax(simcam):
    brd = np.array([[0, 0], [1500, 0], [1500, 1000], [0, 1000], [750, 500]], float)
    img = simcam.project(brd)
    g = BoardCamera.fit(img, brd, simcam.size)
    assert g.rms_px < 1e-3
    pts = np.array([[123.0, 456.0], [1400.0, 80.0]])
    assert np.allclose(g.to_board(g.to_image(pts)), pts, atol=1e-4)
    # camera centre from the homography (focal estimated): close to the truth
    assert np.linalg.norm(g.center - simcam.C) < 0.06 * np.linalg.norm(simcam.C)
    # a point 40 mm in front of the board: parallax corrected to < 1 mm
    p = np.array([[300.0, 650.0]])
    seen = simcam.project(p, 40.0)
    assert np.linalg.norm(g.to_board(seen, z=40.0) - p) < 1.0
    assert np.linalg.norm(g.to_board(seen) - p) > 5.0          # without the correction it is far off


def test_bad_correspondences_rejected(simcam):
    with pytest.raises(CameraGeometryError):
        BoardCamera.fit([[0, 0], [1, 0], [1, 1]], [[0, 0], [1, 0], [1, 1]], (1920, 1080))
    brd = np.array([[0, 0], [1500, 0], [1500, 1000], [0, 1000]], float)
    with pytest.raises(CameraGeometryError):
        BoardCamera.fit(simcam.project(brd), brd[:, ::-1] * 0 + 5, simcam.size)


def test_lens_model_inverse():
    K = np.array([[1400, 0, 960], [0, 1400, 540], [0, 0, 1]], float)
    dist = np.array([-0.12, 0.03, 0.001, -0.0005, 0.0])
    px = np.array([[100.0, 80.0], [960, 540], [1800, 1000]])
    assert np.allclose(undistort_px(distort_px(px, K, dist), K, dist), px, atol=0.05)


# ------------------------------------------------------------------ detection
def test_markers_board_and_crosses_on_synthetic_frame(simcam):
    targets = [(300.0, 200.0), (750.0, 400.0), (1200.0, 650.0), (200.0, 700.0)]
    trace = [s for t in targets for s in cross(*t)]
    simcam._ink = None
    img = simcam.render(trace, (900.0, 300.0))
    found = detect.detect_markers(img)
    assert {0, 1, 2, 3, 10} <= set(found)
    quad = detect.detect_board(img)
    assert quad is not None
    from wbot.camera.service import CameraService
    m = Machine(load_config())
    svc = CameraService(m)
    svc.settings["markers"]["board"] = [{**mm, "size_mm": 60.0} for mm in MARKERS]
    svc.use_image(img)
    try:
        out = svc.calibrate_from_markers()
        assert out["rms_mm"] < 1.0 and not out["missing"]
        from wbot.camera.autocal import measure_crosses
        res = measure_crosses(svc, targets, 20.0, frames=1)
        errs = [math.dist(p["measured"], p["target"]) for p in res["points"]]
        assert max(errs) < 0.8, errs
        # a cross under the gondola is reported as hidden, not measured
        res2 = measure_crosses(svc, [(900.0, 330.0)], 20.0, frames=1, pen=(900.0, 300.0))
        assert res2["points"][0]["status"] == "verdeckt"
        # gondola marker tracking (offset 0/90 mm, 36 mm in front of the board)
        svc._track_once(0)
        assert svc.track["seen"] and math.dist(svc.track["pen"], (900.0, 300.0)) < 2.5
    finally:
        svc.shutdown()
        m.shutdown()


# ------------------------------------------------------------------ API + auto calibration on the simulator
@pytest.fixture()
def client():
    m = Machine(load_config())
    app = create_app(m, serve_ui=False)
    with TestClient(app) as c:
        yield c
        m.shutdown()


def _setup_sim(c, perturb=None):
    r = c.post("/api/connect", json={"transport": "sim", "sim": {"speed": 200, "perturb": perturb or {}}})
    assert r.status_code == 200, r.text
    st = r.json()
    if not st["reference"]["set"]:              # the simulated gondola boots at the board origin
        assert c.post("/api/reference", json={"x": 0, "y": 0}).status_code == 200
    assert c.post("/api/goto", json={"x": 750, "y": 450}).status_code == 200     # off the corner marker
    r = c.put("/api/camera", json={"source": {"kind": "sim"}})
    assert r.status_code == 200, r.text
    for _ in range(50):
        if c.get("/api/camera").json()["source"]["size"]:
            break
        time.sleep(0.1)


def test_camera_api_frames_and_marker_calibration(client):
    c = client
    assert c.get("/api/camera/frame.jpg").status_code == 409            # no camera yet
    _setup_sim(c)
    r = c.get("/api/camera/frame.jpg?w=640")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
    assert c.get("/api/camera/frame.jpg?view=board").status_code == 409  # not calibrated
    r = c.post("/api/camera/calibrate/markers")
    assert r.status_code == 200, r.text
    assert r.json()["rms_mm"] < 1.0
    info = c.get("/api/camera").json()
    assert info["calibrated"] and len(info["coverage"]) >= 4
    r = c.get("/api/camera/frame.jpg?view=board&overlay=1")
    assert r.status_code == 200 and len(r.content) > 10000
    sug = c.get("/api/camera/suggest-board")
    assert sug.status_code == 200 and len(sug.json()["image_points"]) == 4
    assert c.post("/api/camera/calibrate", json={"image_points": [[0, 0]], "board_points": [[0, 0]]}).status_code == 400
    svg = c.get("/api/camera/markers.svg")
    assert svg.status_code == 200 and svg.text.startswith("<svg") and "Gondel-Marker" in svg.text
    assert c.put("/api/camera", json={"bogus": 1}).status_code == 400


def test_auto_calibration_recovers_belt_scale(client):
    c = client
    _setup_sim(c, perturb={"scale_left": 1.006})
    assert c.post("/api/camera/calibrate/markers").status_code == 200
    r = c.post("/api/camera/autocal", json={"n_x": 4, "n_y": 3, "inset_mm": 15})
    assert r.status_code == 200, r.text
    t_end = time.time() + 240
    st = {}
    while time.time() < t_end:
        st = c.get("/api/camera/autocal").json()
        if st["state"] in ("done", "error", "cancelled"):
            break
        time.sleep(0.3)
    assert st["state"] == "done", st
    found = [p for p in st["points"] if p["measured"]]
    assert len(found) >= 10
    res = st["result"]
    assert res["improved"] and res["rms_after"] < 0.5 * res["rms_before"]
    assert abs(res["patch"]["geometry"]["belt_scale_left"] - 1.006) < 0.0015


def test_tape_calibration_api(client):
    c = client
    _setup_sim(c)
    plan = c.get("/api/calibration/tape").json()
    assert len(plan["points"]) == 9 and len(plan["pairs"]) == 12 and plan["pattern"]["id"] == "tape-points"
    assert c.post("/api/preview", json={"pattern": plan["pattern"]}).status_code == 200
    by = {p["name"]: p["xy"] for p in plan["points"]}
    # fake a 0.5 % long left belt: distances grow along the left side (from the model)
    from wbot.calibration import predicted_positions
    st = c.get("/api/state").json()
    m_cfg = load_config()
    real = predicted_positions(m_cfg, st["reference"]["physical"], st["reference"]["firmware"], list(by.values()),
                               {"belt_scale_left": 1.005})
    rp = dict(zip(by, real))
    pairs = [{"a": by[q["a"]], "b": by[q["b"]], "measured_mm": math.dist(rp[q["a"]], rp[q["b"]])} for q in plan["pairs"]]
    r = c.post("/api/calibration/tape/fit", json={"pairs": pairs})
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["improved"] and res["rms_after"] < 0.3 and abs(res["patch"]["geometry"]["belt_scale_left"] - 1.005) < 0.002
    bad = [dict(pairs[0], measured_mm=pairs[0]["measured_mm"] * 1.3)] + pairs[1:]
    assert c.post("/api/calibration/tape/fit", json={"pairs": bad}).status_code == 400
