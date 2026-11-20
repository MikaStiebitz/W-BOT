import time

import pytest
from fastapi.testclient import TestClient

from wbot.api.app import create_app
from wbot.config import load_config
from wbot.machine import Machine


@pytest.fixture()
def client():
    m = Machine(load_config())
    with TestClient(create_app(m, serve_ui=False)) as c:
        yield c
        m.shutdown()


def _wait(c, timeout=60):
    t = time.time()
    while time.time() - t < timeout:
        j = c.get("/api/job").json()
        if j and j["state"] not in ("running", "paused", "planning"):
            return j
        time.sleep(0.1)
    raise AssertionError("job timeout")


def _stroke(x0, y0, x1, y1, color=0):
    return {"color": color, "points": [[x0, y0], [x1, y1]]}


def test_live_job_keeps_stroke_order_and_does_not_park(client):
    client.post("/api/connect", json={"transport": "sim", "sim": {"speed": 60}})
    assert client.post("/api/reference", json={"x": 0, "y": 0}).json()["reference"]["set"]
    # the second stroke is nearer to the first one's start: an optimiser would swap them
    strokes = [_stroke(700, 400, 800, 400), _stroke(200, 300, 260, 300)]
    job = client.post("/api/jobs", json={"drawing": {"strokes": strokes}, "live": True, "speed_scale": 3.0}).json()
    assert job["state"] == "running"
    assert _wait(client)["state"] == "done"
    sim = client.get("/api/simulator").json()
    down = [s for s in sim["trace"] if s[4]]
    # drawn in the given order: first the stroke at x=700..800, then the one at x=200..260
    assert down[0][0] > 600 and down[-1][2] < 300
    # no parking move after a live job: the pen stays where the last stroke ended
    pos = client.get("/api/state").json()["position"]["physical"]
    assert pos == pytest.approx([260, 300], abs=1.0)


def test_normal_job_still_parks(client):
    client.post("/api/connect", json={"transport": "sim", "sim": {"speed": 60}})
    client.post("/api/reference", json={"x": 0, "y": 0})
    client.post("/api/jobs", json={"drawing": {"strokes": [_stroke(200, 300, 260, 300)]}})
    assert _wait(client)["state"] == "done"
    pos = client.get("/api/state").json()["position"]["physical"]
    assert pos != pytest.approx([260, 300], abs=1.0)
