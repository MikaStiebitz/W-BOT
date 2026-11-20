import time

import pytest
from fastapi.testclient import TestClient

from wbot.api.app import create_app
from wbot.config import load_config
from wbot.machine import Machine


@pytest.fixture()
def client():
    m = Machine(load_config())          # no config_path -> never writes config in tests
    app = create_app(m, serve_ui=False)
    with TestClient(app) as c:
        yield c
        m.shutdown()


def wait_job(c, timeout=90):
    t = time.time()
    while time.time() - t < timeout:
        j = c.get("/api/job").json()
        if j and j["state"] not in ("running", "paused", "planning"):
            return j
        time.sleep(0.2)
    raise AssertionError("job timeout")


def connect(c, **sim):
    r = c.post("/api/connect", json={"transport": "sim", "sim": {"speed": 60, **sim}})
    assert r.status_code == 200, r.text
    return r.json()


def test_motion_blocked_without_connection_and_reference(client):
    r = client.post("/api/goto", json={"x": 100, "y": 100})
    assert r.status_code == 409 and r.json()["code"] == "not_connected"
    connect(client)
    r = client.post("/api/goto", json={"x": 100, "y": 100})
    assert r.json()["code"] == "no_reference"
    r = client.post("/api/jog", json={"dx": 10, "dy": 0})
    assert r.json()["code"] == "no_reference"


def test_full_flow_jog_servos_job_accuracy(client):
    st = connect(client)
    assert st["machine"]["state"] == "idle" and st["pen"]["known"] is False
    assert client.post("/api/reference", json={"x": 0, "y": 0}).json()["reference"]["set"]
    assert client.post("/api/goto", json={"x": 500, "y": 400}).status_code == 200
    s = client.get("/api/state").json()
    assert s["position"]["physical"] == pytest.approx([500, 400], abs=0.1)
    assert s["pen"]["up"] is True
    assert client.post("/api/jog", json={"dx": 25, "dy": -10}).status_code == 200
    # out of bounds refused
    r = client.post("/api/goto", json={"x": 5000, "y": 400})
    assert r.status_code == 422 and r.json()["code"] == "out_of_bounds"
    # servo limits
    assert client.post("/api/servo", json={"servo": "pen", "angle": 120}).status_code == 422
    assert client.post("/api/revolver", json={"slot": 3}).json()["revolver"]["slot"] == 3
    assert client.post("/api/pen", json={"state": "down"}).json()["pen"]["up"] is False
    assert client.post("/api/revolver", json={"slot": 2}).json()["revolver"]["slot"] == 2
    assert client.post("/api/pen", json={"state": "up"}).json()["pen"]["up"] is True

    pats = client.get("/api/patterns").json()
    assert any(p["id"] == "square-frame" for p in pats)
    pv = client.post("/api/preview", json={"pattern": {"id": "square-frame", "params": {}}}).json()
    assert pv["estimated_s"] > 0 and pv["drawing"]["strokes"]
    job = client.post("/api/jobs", json={"pattern": {"id": "diagonals", "params": {}}}).json()
    assert job["state"] == "running"
    final = wait_job(client)
    assert final["state"] == "done"
    sim = client.get("/api/simulator").json()
    assert sim["job_error"]["max_mm"] < 0.1          # the point of the whole project
    assert client.get("/api/state").json()["pen"]["up"] is True


def test_perturbed_machine_is_visibly_worse(client):
    connect(client, perturb={"scale_left": 1.01})
    client.post("/api/reference", json={"x": 0, "y": 0})
    client.post("/api/jobs", json={"pattern": {"id": "diagonals", "params": {}}})
    wait_job(client)
    assert client.get("/api/simulator").json()["job_error"]["max_mm"] > 1.0


def test_unreachable_reference_explained(client):
    connect(client)
    r = client.post("/api/reference", json={"x": 750, "y": 950})
    assert r.status_code == 422 and r.json()["code"] == "reference_unreachable"


def test_validation_error_shape(client):
    r = client.post("/api/goto", json={"x": "abc"})
    assert r.status_code == 422 and "error" in r.json()


def test_estop_and_unlock(client):
    connect(client)
    client.post("/api/reference", json={"x": 0, "y": 0})
    client.post("/api/jobs", json={"pattern": {"id": "diagonals", "params": {}}})
    time.sleep(0.5)
    assert client.post("/api/estop").status_code == 200
    time.sleep(0.8)
    s = client.get("/api/state").json()
    assert s["reference"]["uncertain"] is True
    if s["machine"]["state"] == "alarm":
        assert client.post("/api/unlock").status_code == 200
    assert client.get("/api/state").json()["machine"]["state"] == "idle"


def test_gcode_console_is_validated(client):
    connect(client)
    client.post("/api/reference", json={"x": 0, "y": 0})
    assert client.post("/api/gcode", json={"line": "$I"}).status_code == 200
    r = client.post("/api/gcode", json={"line": "G1 X9000 Y10 F1000"})
    assert r.status_code == 422


def test_config_roundtrip_and_firmware_config(client):
    c = client.get("/api/config").json()
    assert c["geometry"]["anchor_left"] == list(load_config().geometry.anchor_left)
    assert c["geometry"]["anchor_left"] == [47, 1041]               # motors on the whiteboard frame
    assert "WallPlotter" in client.get("/api/firmware-config").text


def test_shifted_boot_position_is_equally_accurate():
    """Hanging the gondola at the park position at power-up works just as well."""
    from wbot.config import deep_update
    cfg = deep_update(load_config(), {"firmware": {"boot_physical": [750, 700]}})
    m = Machine(cfg)
    with TestClient(create_app(m, serve_ui=False)) as c:
        connect(c)
        assert c.post("/api/reference", json={"x": 750, "y": 700}).status_code == 200
        c.post("/api/jobs", json={"pattern": {"id": "square-frame", "params": {}}})
        assert wait_job(c)["state"] == "done"
        assert c.get("/api/simulator").json()["job_error"]["max_mm"] < 0.1
        m.shutdown()


def test_commands_are_refused_while_a_job_runs(client):
    connect(client, speed=1)
    client.post("/api/reference", json={"x": 0, "y": 0})
    assert client.post("/api/jobs", json={"pattern": {"id": "diagonals", "params": {}}}).status_code == 200
    r = client.post("/api/goto", json={"x": 500, "y": 400})
    assert r.status_code == 409 and r.json()["code"] == "job_running"
    r = client.post("/api/jobs", json={"pattern": {"id": "diagonals", "params": {}}})
    assert r.status_code == 409 and r.json()["code"] == "job_running"
    client.post("/api/estop")
