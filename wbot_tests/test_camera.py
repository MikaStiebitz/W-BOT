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
from wbot.config import Config
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
    return Config()


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
    from wbot.camera.boardfind import find_board
    assert find_board(img) is not None
    from wbot.camera.service import CameraService
    m = Machine(Config())
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
        assert res2["points"][0]["status"] == "hidden"
        # gondola marker tracking (offset 0/90 mm, 36 mm in front of the board)
        svc._track_once(0)
        assert svc.track["seen"] and math.dist(svc.track["pen"], (900.0, 300.0)) < 2.5
    finally:
        svc.shutdown()
        m.shutdown()


# ------------------------------------------------------------------ API + auto calibration on the simulator
@pytest.fixture()
def client():
    m = Machine(Config())
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
    assert svg.status_code == 200 and svg.text.startswith("<svg") and "Gondola marker" in svg.text
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
    m_cfg = Config()
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


def test_motors_found_and_anchors_measured():
    """The camera finds the motors above the board and measures where the anchors really are."""
    from wbot.camera.service import CameraService
    from wbot.config import deep_update
    real = deep_update(Config(), {"geometry": {"anchor_left": [260.0, 1078.0], "anchor_right": [825.0, 1086.0]}})
    cam = SimCamera(real, seed=5)
    cam.set_markers([{**mm, "size_mm": 60.0} for mm in MARKERS], None)
    img = cam.render((), (600.0, 400.0))
    m = Machine(Config())                 # config still believes 47 / 1453
    svc = CameraService(m)
    svc.settings["markers"]["board"] = [{**mm, "size_mm": 60.0} for mm in MARKERS]
    svc.use_image(img)
    try:
        svc.calibrate_from_markers()
        sug = svc.suggest_motors()
        for got, want in zip(sug["board_points"], ([260.0, 1078.0], [825.0, 1086.0])):
            assert abs(got[0] - want[0]) < 12 and abs(got[1] - want[1]) < 30, sug
        # the user drags the points onto the pulley centres (here: the true projection)
        px = cam.project([(260.0, 1078.0), (825.0, 1086.0)], 41.0)
        res = svc.measure_anchors(px[0], px[1])
        assert math.dist(res["anchor_left"], (260, 1078)) < 3 and math.dist(res["anchor_right"], (825, 1086)) < 3, res
        assert res["deviation_mm"][0] > 200 and res["warnings"]
        assert res["patch"]["firmware"]["anchor_left"] == list(m.cfg.fw_anchor_left)
        # the drawing area that fits these motors is proposed (and applied) together with them
        x0, y0, x1, y1 = res["area"]["rect"]
        assert 260 < x0 < 400 and 690 < x1 < 830 and res["patch"]["board"]["margin_left_mm"] == round(x0, 1)
    finally:
        svc.shutdown()
        m.shutdown()


def test_board_found_by_its_frame(simcam):
    """Frame-edge detector: finds the board outline even though belts and the gondola cross it."""
    from wbot.camera.boardfind import find_board
    simcam._ink = None
    img = simcam.render(cross(400.0, 300.0), (700.0, 450.0))
    found = find_board(img)
    assert found is not None
    corners, score = found
    # the outer frame is 28 mm outside the board in the sim: corners within ~45 px of the board corners
    true = simcam.project([(0, 1000), (1500, 1000), (1500, 0), (0, 0)])
    assert np.max(np.linalg.norm(corners - true, axis=1)) < 45, (corners, true)
    # a picture without a whole board gives no suggestion instead of a wrong one
    assert find_board(np.full((1080, 1920, 3), 200, np.uint8)) is None


def _guard_rig(cam_cfg=None):
    from wbot.camera.service import CameraService
    m = Machine(Config())
    svc = CameraService(m)
    svc.settings["markers"]["board"] = [{**mm, "size_mm": 60.0} for mm in MARKERS]
    cam = SimCamera(cam_cfg or Config(), seed=11)
    cam.set_markers([{**mm, "size_mm": 60.0} for mm in MARKERS], None)
    return m, svc, cam


def test_person_detected_with_skeleton_and_filtered_out():
    m, svc, cam = _guard_rig()
    try:
        empty = [cam.render((), None) for _ in range(18)]
        for im in empty:
            st = svc.presence.update(im)
        assert not st["present"]
        cam.person = (700.0, 400.0)
        for _ in range(4):
            st = svc.presence.update(cam.render((), None))
        assert st["present"] and len(st["outline"]) >= 4 and len(st["skeleton"]) > 20
        img = cam.render((), None)
        x0, y0, x1, y1 = st["bbox"]
        diff_raw = np.abs(img[y0:y1, x0:x1].astype(float) - empty[0][y0:y1, x0:x1]).mean()
        diff_flt = np.abs(svc.presence.filtered(img)[y0:y1, x0:x1].astype(float) - empty[0][y0:y1, x0:x1]).mean()
        assert diff_flt < 0.35 * diff_raw, (diff_raw, diff_flt)
    finally:
        svc.shutdown()
        m.shutdown()


def test_guard_blocks_job_and_starts_queued_job_when_free():
    m, svc, cam = _guard_rig()
    try:
        m.connect("sim", None, None, {"speed": 200})
        m.set_reference(0, 0)
        m.camera = svc
        svc.settings["people"]["guard"] = "protect"
        svc.use_image(cam.render((), None))
        svc._track_stop.set()                                # drive the detector by hand, not from the loop
        for _ in range(18):
            svc.presence.update(cam.render((), None))
        cam.person = (700.0, 400.0)
        for _ in range(4):
            svc.presence.update(cam.render((), None))
        assert svc.person_present()
        body = {"pattern": {"id": "square-frame", "params": {}}, "dry_run": True}
        from wbot.errors import MachineError
        with pytest.raises(MachineError) as ei:
            m.start_job(body)
        assert ei.value.code == "person_present"
        svc.queue_job(body)
        assert svc.queue_job(body)["count"] == 2       # live drawing queues many small jobs: none may be lost
        cam.person = None
        for _ in range(8):
            svc._presence_once(cam.render((), None))
            svc.presence.last_seen -= 10            # pretend 10 s passed
        assert m.jobs and m.jobs.job and m.jobs.job["state"] in ("planning", "running", "done")
        assert len(svc._queued) <= 1
        m.cancel()
    finally:
        svc.shutdown()
        m.shutdown()


def test_camera_moved_is_followed():
    m, svc, cam = _guard_rig()
    try:
        img_a = cam.render((), None)
        svc.use_image(img_a)
        svc.calibrate_from_markers()
        assert svc.watch.has_reference
        moved = SimCamera(Config(), seed=12, center=(1010.0, 300.0, 2230.0), look_at=(780.0, 500.0, 0.0), roll_deg=3.5)
        moved.set_markers([{**mm, "size_mm": 60.0} for mm in MARKERS], None)
        img_b = moved.render((), None)
        svc._watch_t = 0.0
        svc._watch_once(img_b)
        assert svc.watch_state["state"] == "followed", svc.watch_state
        corners = [(0, 0), (1500, 0), (1500, 1000), (0, 1000), (750, 500)]
        err = np.linalg.norm(svc.geom.to_image(corners) - moved.project(corners), axis=1)
        assert err.max() < 4.0, err
        # unchanged picture: nothing to do
        svc._watch_t = 0.0
        svc._watch_once(img_b)
        assert svc.watch_state["state"] == "ok"
    finally:
        svc.shutdown()
        m.shutdown()


def test_one_click_auto_setup():
    """Board found and aligned, motors found and measured (not applied) in one call."""
    m, svc, cam = _guard_rig()
    try:
        svc.use_image(cam.render((), (600.0, 400.0)))
        res = svc.auto_setup()
        assert not res["errors"], res["errors"]
        assert svc.geom is not None and res["board"]["rms_mm"] < 1.0
        # auto alignment uses the outer frame (28 mm outside the board in the sim) and the motor body centre:
        # a few cm off, which is what the drag-to-refine step is for
        assert math.dist(res["motors"]["anchor_left"], m.cfg.geometry.anchor_left) < 70
        assert m.cfg.geometry.anchor_left == Config().geometry.anchor_left       # nothing applied
    finally:
        svc.shutdown()
        m.shutdown()


def test_drawing_area_follows_the_motors():
    from wbot.config import deep_update
    from wbot.drawarea import suggest_area
    x0, y0, x1, y1 = suggest_area(Config())["rect"]
    assert x0 < 150 and x1 > 1350 and y1 > 700                 # motors at the corners: nearly the whole board
    off = deep_update(Config(), {"geometry": {"anchor_left": [270.0, 1087.0], "anchor_right": [828.0, 1083.0]}})
    x0, y0, x1, y1 = suggest_area(off)["rect"]
    assert 270 <= x0 and x1 <= 828 and abs((x0 + x1) / 2 - 549) < 40   # between the motors


def test_exposure_change_is_not_an_object_and_roi_limits_detection():
    m, svc, cam = _guard_rig()
    try:
        empty = cam.render((), None)
        for _ in range(10):
            svc.presence.update(cam.render((), None))
        for _ in range(6):                                   # webcam re-exposes: whole picture brighter
            st = svc.presence.update(np.clip(cam.render((), None) * 1.15, 0, 255).astype(np.uint8))
        assert not st["present"]
        # something outside the watched area (here: the left third of the picture) is ignored
        roi = np.array([[700, 0], [1920, 0], [1920, 1080], [700, 1080]], float)
        cam.person = (100.0, 400.0)                          # in front of the left board edge -> left in the image
        for _ in range(4):
            st = svc.presence.update(cam.render((), None), roi=roi)
        assert not st["present"], st.get("bbox")
        del empty
    finally:
        svc.shutdown()
        m.shutdown()


def test_ghost_object_fades_out():
    """Something that was in the picture at the start and left (or a lighting jump) must not stick."""
    m, svc, cam = _guard_rig()
    try:
        cam.person = (700.0, 400.0)
        for _ in range(8):                                   # person already there when watching starts
            svc.presence.update(cam.render((), None))
        cam.person = None                                    # ... and leaves: the revealed board looks "new"
        states = [svc.presence.update(cam.render((), None)) for _ in range(40)]
        assert not states[-1]["present"], states[-1].get("bbox")
    finally:
        svc.shutdown()
        m.shutdown()


def test_gondola_found_from_its_belts():
    from wbot.camera.gondola_find import locate
    from wbot.camera.geometry import BoardCamera
    cfg = Config()
    cam = SimCamera(cfg, seed=3)
    brd = np.array([[0, 0], [1500, 0], [1500, 1000], [0, 1000]], float)
    g = BoardCamera.fit(cam.project(brd), brd, cam.size)
    for pen in [(600.0, 400.0), (300.0, 200.0), (1100.0, 650.0)]:
        r = locate(cfg, g, cam.render((), pen))
        assert r["ok"] and math.dist(r["pen"], pen) < 15, (pen, r)
    r = locate(cfg, g, cam.render((), None))                  # no gondola: nothing hangs on the belts
    assert not r["ok"]
