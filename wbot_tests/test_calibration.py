"""Calibration: fit geometry from measured crosses (synthetic, ill-conditioned, end-to-end on the simulator)."""
from __future__ import annotations

import time

import numpy as np
import pytest

from wbot.calibration import (CalibrationError, Measurement, fit_geometry, predicted_positions,
                              suggest_targets)
from wbot.calibration_api import calibration_apply, calibration_fit, calibration_targets
from wbot.config import Config, deep_update, load_config
from wbot.machine import Machine, MachineError
from wbot.sim.plant import build_truth

CFG = load_config()                     # the machine as configured (anchors on the frame, per-side margins)
REF = (0.0, 0.0)                        # reference pair of the default boot position: pen at (0,0), firmware (0,0)
PERTURB = {"anchor_right_dx": 3.0, "anchor_left_dy": -2.0, "scale_left": 1.004, "scale_right": 0.997}


def truth_params(cfg: Config, perturb: dict) -> dict:
    """Simulator-style overrides -> absolute true parameters for ``predicted_positions``."""
    truth, sc = build_truth(cfg, perturb)
    return {"anchor_left": truth.anchor_left, "anchor_right": truth.anchor_right,
            "belt_scale_left": sc[0], "belt_scale_right": sc[1], "pen_offset": truth.pen_offset,
            "radius": truth.pulley_r}


def real_error(cfg: Config, tp: dict, targets, ref=REF) -> np.ndarray:
    """Distance (mm) between aim and landing point if the host pre-distorts with ``cfg``."""
    t = np.asarray(list(targets), float)
    return np.linalg.norm(predicted_positions(cfg, ref, ref, t, tp) - t, axis=1)


def measure(cfg: Config, tp: dict, targets, noise: float, seed: int, ref=REF) -> list[Measurement]:
    rng = np.random.default_rng(seed)
    pos = predicted_positions(cfg, ref, ref, targets, tp)
    pos = pos + rng.normal(0.0, noise, pos.shape)
    return [Measurement(tuple(map(float, t)), tuple(map(float, m))) for t, m in zip(targets, pos)]


def test_targets_grid_covers_corners_and_matches_pattern_layout():
    tg = suggest_targets(CFG)
    assert len(tg) == 20
    x0, y0, x1, y1 = CFG.safe_rect()
    assert (x0 + 20, y0 + 20) in tg and (x1 - 20, y1 - 20) in tg and (x0 + 20, y1 - 20) in tg
    from wbot.patterns import build_pattern
    d = build_pattern(CFG, "crosshair-points", {"cols": 5, "rows": 4, "arm_mm": 20})
    centres = sorted((round(float(s.pts[:, 0].mean()), 3), round(float(s.pts[:, 1].mean()), 3)) for s in d.strokes)
    assert sorted(set(centres)) == sorted((round(x, 3), round(y, 3)) for x, y in tg)
    with pytest.raises(CalibrationError):
        suggest_targets(CFG, 1, 4)


# --------------------------------------------------------------------------------------- (1) synthetic
def test_synthetic_fit_recovers_geometry_and_improves_independent_points():
    tp = truth_params(CFG, PERTURB)
    tg = suggest_targets(CFG)
    ms = measure(CFG, tp, tg, noise=0.3, seed=11)
    res = fit_geometry(CFG, REF, REF, ms)
    assert res.free == ["anchor_left", "anchor_right", "scale"] and res.n == 20
    assert res.improved and res.rms_before > 2.5 and res.rms_after < 0.5
    assert res.max_after < res.max_before and len(res.residuals) == 20
    assert res.condition_number < 100 and not res.warnings
    # the patch is a valid config patch that keeps the flashed (firmware) anchors
    new = deep_update(CFG, res.patch)
    assert new.fw_anchor_left == pytest.approx(CFG.fw_anchor_left) and new.fw_anchor_right == pytest.approx(CFG.fw_anchor_right)
    assert new.geometry.belt_scale_left == pytest.approx(1.004, abs=0.0006)
    assert new.geometry.belt_scale_right == pytest.approx(0.997, abs=0.0006)
    # independent test set: real landing error with the patched pre-distortion
    rng = np.random.default_rng(5)
    x0, y0, x1, y1 = CFG.safe_rect()
    test = np.column_stack([rng.uniform(x0, x1, 80), rng.uniform(y0, y1, 80)])
    before, after = real_error(CFG, tp, test), real_error(new, tp, test)
    assert before.mean() > 2.0 and before.max() > 4.0
    assert np.sqrt(np.mean(after ** 2)) < 0.3 and after.max() < 0.5
    d = res.to_dict()                                           # JSON-able
    import json
    json.dumps(d)
    assert set(d) >= {"patch", "rms_before", "rms_after", "max_before", "max_after", "residuals", "warnings", "free", "n"}


def test_reference_and_pen_offset_are_fittable_when_identifiable():
    # believed reference is 1.5 / -2 mm off: the pen really started at (1.5, -2)
    tp = {**truth_params(CFG, {"anchor_dx": 2.0, "scale_left": 1.003}), "reference_correction": (1.5, -2.0)}
    tg = suggest_targets(CFG)
    res = fit_geometry(CFG, REF, REF, measure(CFG, tp, tg, 0.2, 3), ("anchor_left", "anchor_right", "scale", "reference"))
    assert res.improved and res.rms_after < 0.4
    assert res.reference_correction == pytest.approx((1.5, -2.0), abs=0.5)
    # pen offset (with anchors fixed) is a distinct, recoverable parameter ...
    tp2 = truth_params(CFG, {"pen_dx": 2.0, "pen_dy": -1.5})
    res2 = fit_geometry(CFG, REF, REF, measure(CFG, tp2, tg, 0.1, 4), ("pen_offset",))
    assert res2.improved and res2.rms_after < 0.3
    assert res2.patch["geometry"]["pen_offset_mm"] == pytest.approx([2.0, -1.5], abs=0.3)
    # ... but indistinguishable from a common anchor shift, so it is dropped when both anchors are free
    res3 = fit_geometry(CFG, REF, REF, measure(CFG, tp2, tg, 0.1, 4), ("anchor_left", "anchor_right", "pen_offset"))
    assert "pen_offset" not in res3.free and any("Stiftversatz" in w for w in res3.warnings)
    assert "pen_offset_mm" not in res3.patch.get("geometry", {})


# --------------------------------------------------------------------------------------- (3) honesty
def test_too_few_or_clustered_points_warn_and_restrict():
    tp = truth_params(CFG, PERTURB)
    tg = suggest_targets(CFG)
    r2 = fit_geometry(CFG, REF, REF, measure(CFG, tp, tg[:2], 0.1, 1))
    assert r2.patch == {} and not r2.improved and any("wenige" in w for w in r2.warnings)
    r4 = fit_geometry(CFG, REF, REF, measure(CFG, tp, [tg[0], tg[7], tg[12], tg[19]], 0.1, 1))
    assert r4.free == ["scale"] and any("Nur 4 Messpunkte" in w for w in r4.warnings)
    assert set(r4.patch["geometry"]) == {"belt_scale_left", "belt_scale_right"} and "firmware" not in r4.patch
    # explicit request for more parameters than the data can carry
    r5 = fit_geometry(CFG, REF, REF, measure(CFG, tp, tg[:3], 0.1, 1),
                      ("anchor_left", "anchor_right", "scale", "reference"))
    assert r5.patch == {} and any("unterbestimmt" in w for w in r5.warnings)
    # all crosses in one corner patch: ill-conditioned -> must say so
    near = [(100.0 + 25.0 * i, 100.0 + 20.0 * j) for i in range(4) for j in range(3)]
    rc = fit_geometry(CFG, REF, REF, measure(CFG, tp, near, 0.2, 2))
    assert any("Konditionszahl" in w or "bestimmt" in w for w in rc.warnings)
    assert any("40 %" in w for w in rc.warnings)
    # too many free parameters (incl. radius) on a modest set is flagged as badly conditioned
    rr = fit_geometry(CFG, REF, REF, measure(CFG, tp, tg, 0.3, 2),
                      ("anchor_left", "anchor_right", "scale", "reference", "radius"))
    assert rr.condition_number > 150 and any("Konditionszahl" in w for w in rr.warnings)
    with pytest.raises(CalibrationError, match="Unbekannter"):
        fit_geometry(CFG, REF, REF, measure(CFG, tp, tg, 0.1, 1), ("bogus",))


def test_noise_only_on_nominal_machine_claims_no_improvement():
    tg = suggest_targets(CFG)
    for seed in range(5):
        res = fit_geometry(CFG, REF, REF, measure(CFG, {}, tg, noise=0.3, seed=seed))
        assert res.rms_before < 0.6 and not res.improved
        assert res.patch == {} and any("Messgenauigkeit" in w for w in res.warnings)
        assert res.max_before < 2.0                    # sanity: pure noise, nothing systematic
        assert all(abs(v) < 2.0 for v in (res.params["al_x"] - CFG.geometry.anchor_left[0],
                                          res.params["al_y"] - CFG.geometry.anchor_left[1],
                                          res.params["ar_x"] - CFG.geometry.anchor_right[0],
                                          res.params["ar_y"] - CFG.geometry.anchor_right[1]))
        assert abs(res.params["sl"] - 1.0) < 0.0015 and abs(res.params["sr"] - 1.0) < 0.0015


def test_outlier_of_15mm_does_not_wreck_the_fit():
    tp = truth_params(CFG, PERTURB)
    tg = suggest_targets(CFG)
    ms = measure(CFG, tp, tg, 0.3, 21)
    bad = ms[8]
    ms[8] = Measurement(bad.target, (bad.measured[0] + 15.0, bad.measured[1] - 4.0))
    res = fit_geometry(CFG, REF, REF, ms)
    assert res.improved and res.outliers == [8] and any("Messpunkt 9" in w for w in res.warnings)
    assert res.rms_after < 0.5
    new = deep_update(CFG, res.patch)
    rng = np.random.default_rng(8)
    x0, y0, x1, y1 = CFG.safe_rect()
    test = np.column_stack([rng.uniform(x0, x1, 60), rng.uniform(y0, y1, 60)])
    assert np.sqrt(np.mean(real_error(new, tp, test) ** 2)) < 0.6
    # with two further wrong values robust loss alone still keeps the result usable
    ms[3] = Measurement(ms[3].target, (ms[3].measured[0] - 9.0, ms[3].measured[1]))
    ms[15] = Measurement(ms[15].target, (ms[15].measured[0], ms[15].measured[1] + 12.0))
    res2 = fit_geometry(CFG, REF, REF, ms)
    assert res2.improved
    assert np.sqrt(np.mean(real_error(deep_update(CFG, res2.patch), tp, test) ** 2)) < 1.5


# --------------------------------------------------------------------------------------- (2) end to end
def _wait_job(m: Machine, timeout: float = 90.0) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = m.job_info()
        if j and j["state"] not in ("planning", "running", "paused"):
            return j
        time.sleep(0.1)
    raise AssertionError("job timeout")


def _job_error(m: Machine) -> float:
    m.start_job({"pattern": {"id": "diagonals", "params": {}}})
    assert _wait_job(m)["state"] == "done"
    return float(m.simulator_info()["job_error"]["max_mm"])


@pytest.fixture()
def sim_machine(tmp_path):
    m = Machine(CFG, config_path=tmp_path / "wbot.yaml")        # never touches config/wbot.yaml
    m.connect("sim", sim={"speed": 300, "perturb": PERTURB})
    m.set_reference(0.0, 0.0)
    yield m
    m.shutdown()


def test_end_to_end_on_the_simulator(sim_machine):
    m = sim_machine
    err_before = _job_error(m)
    assert err_before > 2.5                                     # perturbed machine, nominal config

    # draw-and-measure: move the pen to every target and read where the plant really put it
    tg = calibration_targets(m)["targets"]
    emu = m._emulator()
    rng = np.random.default_rng(2)
    meas = []
    for x, y in tg:
        m.goto(x, y)
        px, py = emu.physical_position()
        meas.append({"target": [x, y], "measured": [px + rng.normal(0, 0.2), py + rng.normal(0, 0.2)]})
    body = {"measurements": meas}

    fit = calibration_fit(m, body)
    assert fit["improved"] and fit["rms_before"] > 2.5 and fit["rms_after"] < 0.5
    assert m.cfg.geometry.anchor_right == CFG.geometry.anchor_right        # fit alone changes nothing

    out = calibration_apply(m, body)
    assert out["applied"] and out["config"]["geometry"]["belt_scale_left"] == pytest.approx(1.004, abs=0.001)
    assert m.cfg.geometry.anchor_right != CFG.geometry.anchor_right
    assert m.cfg.fw_anchor_right == pytest.approx(CFG.fw_anchor_right)     # flashed anchors stay pinned

    err_after = _job_error(m)                                    # same machine, same perturbation, patched host
    assert err_after < 0.5 and err_after * 5 < err_before, (err_before, err_after)


def test_end_to_end_fresh_sim_with_same_physical_truth(sim_machine, tmp_path):
    """Reconnecting a new sim: ``perturb`` is relative to the *configured* geometry, so the (unchanged)
    physical truth has to be passed as absolute values once the geometry has been patched."""
    m = sim_machine
    tg = calibration_targets(m)["targets"]
    meas = []
    emu = m._emulator()
    for x, y in tg:
        m.goto(x, y)
        meas.append({"target": [x, y], "measured": [float(v) for v in emu.physical_position()]})
    res = calibration_apply(m, {"measurements": meas})
    assert res["applied"] and res["result"]["rms_after"] < 0.1       # noise free
    truth, sc = build_truth(CFG, PERTURB)
    absolute = {"anchor_left": truth.anchor_left, "anchor_right": truth.anchor_right,
                "scale_left": sc[0], "scale_right": sc[1]}
    m.disconnect()
    m.connect("sim", sim={"speed": 300, "perturb": absolute})
    m.set_reference(0.0, 0.0)
    assert _job_error(m) < 0.3


# --------------------------------------------------------------------------------------- API glue
def test_api_functions_validate_and_never_change_config_on_bad_input(tmp_path):
    m = Machine(CFG, config_path=tmp_path / "c.yaml")
    t = calibration_targets(m)
    assert len(t["targets"]) == 20 and t["pattern"]["id"] == "crosshair-points"
    assert t["pattern"]["params"] == {"cols": 5, "rows": 4, "arm_mm": 20.0}
    good = {"measurements": [{"target": [100, 100], "measured": [101, 100]}] * 8}
    with pytest.raises(MachineError) as e:
        calibration_fit(m, good)
    assert e.value.code == "no_reference"
    for bad in (None, {}, {"measurements": []}, {"measurements": [{"target": [1], "measured": [1, 2]}]},
                {"measurements": [{"target": [1, 2], "measured": [float("nan"), 2]}]},
                {"measurements": [1, 2]}, {**good, "free": "scale"}):
        with pytest.raises(MachineError) as e:
            calibration_apply(m, bad)
        assert e.value.code == "bad_request"
    assert m.cfg == CFG
    with pytest.raises(MachineError):
        calibration_targets(m, 1, 4)
