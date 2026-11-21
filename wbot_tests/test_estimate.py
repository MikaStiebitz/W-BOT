import time

import pytest
from fastapi.testclient import TestClient

import wbot.estimate as est
from wbot.api.app import create_app
from wbot.config import Config, load_config
from wbot.estimate import JobHistory, eta_s
from wbot.kinematics import Mapping
from wbot.machine import Machine
from wbot.patterns import build_pattern
from wbot.planner import PlanOptions, plan


def _hist(tmp_path=None, n=5, ratio=1.2, transport="serial", est_s=100.0):
    h = JobHistory(None if tmp_path is None else tmp_path / "h.json")
    for _ in range(n):
        assert h.record(transport=transport, est_s=est_s, actual_s=est_s * ratio)
    return h


def test_factor_needs_a_few_jobs_and_is_per_connection():
    h = JobHistory(None)
    assert h.factor("serial") == (1.0, 0)
    for _ in range(2):
        h.record(transport="serial", est_s=100, actual_s=130)
    assert h.factor("serial") == (1.0, 2)                    # not enough yet: the model is used as is
    h.record(transport="serial", est_s=100, actual_s=130)
    assert h.factor("serial") == (pytest.approx(1.3), 3)
    assert h.factor("sim") == (1.0, 0)                        # another kind of connection learns separately
    assert h.estimate(200, "serial")["estimated_s"] == pytest.approx(260)


def test_factor_is_robust_and_bounded():
    h = _hist(n=6, ratio=1.1)
    h.record(transport="serial", est_s=100, actual_s=900)    # one freak job (paused for an hour) does not move the median much
    assert h.factor("serial")[0] == pytest.approx(1.1)
    wild = _hist(n=5, ratio=10.0)
    assert wild.factor("serial")[0] == est.FACTOR_RANGE[1]


def test_short_jobs_are_not_learned_from():
    h = JobHistory(None)
    assert not h.record(transport="sim", est_s=3, actual_s=2)
    assert h.records == []


def test_history_survives_a_restart(tmp_path):
    h = _hist(tmp_path, n=4, ratio=1.5)
    again = JobHistory(tmp_path / "h.json")
    assert again.factor("serial") == (pytest.approx(1.5), 4)
    (tmp_path / "h.json").write_text("not json", encoding="utf-8")
    assert JobHistory(tmp_path / "h.json").records == []     # a damaged file never breaks the server


def test_eta_trusts_the_factor_first_and_the_real_pace_later():
    assert eta_s(100, 0, 0, 1.2) == pytest.approx(120)       # nothing done yet: learned factor
    # half done (50 s modelled) after 100 s real: this job runs at 2x, so ~100 s remain, not 60
    assert eta_s(100, 50, 100, 1.2) == pytest.approx(50 * ((0.5) * 1.2 + 0.5 * 2.0))
    assert eta_s(100, 100, 200, 1.2) == 0
    assert eta_s(0, 0, 0, 1.0) == 0


def test_plan_gives_a_monotonic_time_for_every_line():
    cfg = Config()
    m = Mapping.from_config(cfg, (750, 500), (750, 500))
    p = plan(build_pattern(cfg, "square-frame", {}), cfg, m, PlanOptions())
    assert len(p.line_end) == len(p.lines) > 10
    assert all(b >= a for a, b in zip(p.line_end, p.line_end[1:]))
    assert p.line_end[-1] == pytest.approx(p.estimated_s)


@pytest.fixture()
def client():
    m = Machine(load_config())
    with TestClient(create_app(m, serve_ui=False)) as c:
        yield c, m
        m.shutdown()


def _wait(c, timeout=90):
    t = time.time()
    while time.time() - t < timeout:
        j = c.get("/api/job").json()
        if j and j["state"] not in ("running", "paused", "planning"):
            return j
        time.sleep(0.1)
    raise AssertionError("job timeout")


def test_preview_and_job_carry_the_estimate_and_the_job_teaches_the_history(client, monkeypatch):
    c, m = client
    monkeypatch.setattr(est, "MIN_JOB_S", 0.0)               # a fast simulator job still counts here
    body = {"pattern": {"id": "square-frame", "params": {}}}
    pv = c.post("/api/preview", json=body).json()
    assert pv["estimate"]["raw_s"] > 0 and pv["estimated_s"] == pytest.approx(pv["estimate"]["raw_s"])   # nothing learned yet
    c.post("/api/connect", json={"transport": "sim", "sim": {"speed": 60}})
    c.post("/api/reference", json={"x": 0, "y": 0})
    pv = c.post("/api/preview", json=body).json()
    assert pv["estimate"]["sim_speed"] == 60 and pv["estimated_s"] == pytest.approx(pv["estimate"]["raw_s"] / 60, rel=0.01)
    job = c.post("/api/jobs", json=body).json()
    assert job["estimated_s"] == pytest.approx(pv["estimated_s"], rel=0.01)
    done = _wait(c)
    assert done["state"] == "done" and done["eta_s"] == 0
    rec = m.jobs.history.records[-1]
    assert rec["transport"] == "sim" and rec["est_s"] > 0 and rec["actual_s"] > 0
    # real-time equivalent of the run: the simulator ran 60x faster, so this is in the order of the model, not 1/60 of it
    assert 0.2 < rec["actual_s"] / rec["est_s"] < 5
