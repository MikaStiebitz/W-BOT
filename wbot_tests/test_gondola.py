"""Gondola V2: belts attach 176 mm above the pen at two points and the rigid gondola tilts (wbot/gondola.py)."""
from __future__ import annotations

import math

import numpy as np
import pytest

from wbot import gondola
from wbot.calibration import Measurement, fit_geometry, predicted_positions, suggest_targets
from wbot.config import deep_update, load_config
from wbot.kinematics import KinematicsError, Mapping, TrueModel, true_model_from_config
from wbot.workspace import amplification, tensions

try:                                    # package layout or rootdir-less layout (same pattern as test_sim.py)
    from .sim_helpers import deviation_to_polylines, drawn_points, make_emu
    from .test_sim import SHAPES, _draw
except ImportError:                     # pragma: no cover
    from sim_helpers import deviation_to_polylines, drawn_points, make_emu
    from test_sim import SHAPES, _draw

CFG = load_config()
X0, Y0, X1, Y1 = CFG.safe_rect()
GRID = np.array([[x, y] for x in np.linspace(X0, X1, 6) for y in np.linspace(Y0, Y1, 5)])   # corners included
FINE = np.array([[x, y] for x in np.linspace(X0, X1, 22) for y in np.linspace(Y0, Y1, 13)])
AL, AR = CFG.geometry.anchor_left, CFG.geometry.anchor_right
R = CFG.geometry.pulley_pitch_radius_mm


# --------------------------------------------------------------------------------------- (1) point model
def _old_point_lengths(al, ar, r, off, pts):
    """The pre-V2 single-point model, re-implemented independently (belt end = pen - pen_offset)."""
    out = []
    for x, y in pts:
        gx, gy = x - off[0], y - off[1]
        row = []
        for (ax, ay), sgn in ((al, 1.0), (ar, -1.0)):             # right pulley = mirror image
            vx, vy = sgn * (gx - ax), gy - ay
            d = math.hypot(vx, vy)
            row.append(math.sqrt(d * d - r * r) + r * (math.pi - math.atan2(vy, vx) - math.acos(r / d)))
        out.append(row)
    return np.array(out)


def test_zero_spacing_and_height_reproduce_the_old_point_model():
    pts = np.vstack([GRID, [[750, 500], [5, 5]]])
    off = (3.0, -2.0)
    want = _old_point_lengths(AL, AR, R, off, pts)
    direct = TrueModel(AL, AR, R, off)                                         # defaults: s = h = 0
    via_cfg = true_model_from_config(CFG, attach_half_spacing=0.0, attach_height=0.0, pen_offset=off)
    for m in (direct, via_cfg):
        assert not m.has_tilt
        assert np.allclose(m.lengths(pts), want, atol=1e-9)
        assert np.allclose(m.position(m.lengths(pts)), pts, atol=1e-7)          # forward without a seed
    # a pure height (s = 0) is the point model with the offset shifted by -h
    h = true_model_from_config(CFG, attach_half_spacing=0.0, attach_height=176.0, pen_offset=off)
    assert np.allclose(h.lengths(pts), _old_point_lengths(AL, AR, R, (off[0], off[1] - 176.0), pts), atol=1e-9)


def test_tilt_free_gondola_uses_the_two_attach_points():
    m = true_model_from_config(CFG, tilt=False)
    l = m.lengths(GRID)
    for k, ((ax, ay), sgn, (ex, ey)) in enumerate(((AL, 1.0, (-8.0, 176.0)), (AR, -1.0, (8.0, 176.0)))):
        v = np.stack([sgn * (GRID[:, 0] + ex - ax), GRID[:, 1] + ey - ay], axis=1)
        d = np.hypot(v[:, 0], v[:, 1])
        want = np.sqrt(d * d - R * R) + R * (math.pi - np.arctan2(v[:, 1], v[:, 0]) - np.arccos(R / d))
        assert np.allclose(l[:, k], want, atol=1e-9)
    assert np.allclose(m.tilt_angle(GRID), 0.0)
    _, jac = m.lengths_and_jac(GRID)
    eps = 1e-5
    for k in range(2):
        d = np.zeros(2)
        d[k] = eps
        assert np.allclose(jac[:, :, k], (m.lengths(GRID + d) - m.lengths(GRID - d)) / (2 * eps), atol=1e-6)


def test_factory_overrides_and_naive_mode():
    with pytest.raises(ValueError):
        true_model_from_config(CFG, nonsense=1)
    m = true_model_from_config(CFG, com_height=50.0, attach_height=150.0, radius=None)
    assert (m.com_height, m.attach_height, m.pulley_r) == (50.0, 150.0, R)
    naive = deep_update(CFG, {"geometry": {"model": "naive"}})                  # no pre-distortion at all
    assert np.allclose(Mapping.from_config(naive, (750, 500)).to_firmware(GRID), GRID, atol=1e-9)
    with pytest.raises(Exception):
        deep_update(CFG, {"geometry": {"com_height_mm": 200.0}})                # CoM above the attach points


# --------------------------------------------------------------------------------------- (2) equilibrium
def _rot(th, v):
    c, s = np.cos(th), np.sin(th)
    return np.array([c * v[0] - s * v[1], s * v[0] + c * v[1]])


def test_equilibrium_lines_concurrent_forces_and_torque_balance():
    m = true_model_from_config(CFG)
    th = m.tilt_angle(GRID)
    al, ar = m.attach_points(GRID)
    dirs = m.belt_dirs(GRID)
    mass_g = CFG.geometry.gondola_mass_kg * 9.81
    for i, p in enumerate(GRID):
        dl, dr = dirs[i]
        # belts are tangent to their pulleys (distance centre -> belt line == r) and head towards them
        for a, d, c in ((al[i], dl, AL), (ar[i], dr, AR)):
            assert abs((c[0] - a[0]) * d[1] - (c[1] - a[1]) * d[0]) == pytest.approx(R, abs=1e-9)
        assert dl[0] < 0 < dr[0]
        # lines of action concurrent with gravity through the centre of mass
        t, _ = np.linalg.solve(np.array([dl, -dr]).T, ar[i] - al[i])
        x = al[i] + t * dl
        c = p + _rot(th[i], (CFG.geometry.com_offset_x_mm, CFG.geometry.com_height_mm))
        assert abs(c[0] - x[0]) < 1e-9
        # statics: tensions from the force balance are positive and the torque about the CoM vanishes
        ten = np.linalg.solve(np.array([dl, dr]).T, [0.0, mass_g])
        assert np.all(ten > 0)
        torque = sum((a - c)[0] * (T * d)[1] - (a - c)[1] * (T * d)[0] for a, d, T in ((al[i], dl, ten[0]), (ar[i], dr, ten[1])))
        assert abs(torque) < 1e-6


def test_tilt_is_antisymmetric_about_the_centre_column_and_reported(capsys):
    # symmetric machine (anchors 47 / 1453) with the CoM on the pen axis: theta(-dx) == -theta(+dx), 0 in the middle
    sym = true_model_from_config(CFG, com_x=0.0)
    mid = (AL[0] + AR[0]) / 2
    ys = np.linspace(Y0, Y1, 5)
    assert np.allclose(sym.tilt_angle(np.stack([np.full(5, mid), ys], axis=1)), 0.0, atol=1e-9)
    for dx in (100.0, 400.0, 630.0):
        tl = sym.tilt_angle(np.stack([np.full(5, mid - dx), ys], axis=1))
        tr = sym.tilt_angle(np.stack([np.full(5, mid + dx), ys], axis=1))
        assert np.all(tl * tr < 0) and np.allclose(tl, -tr, atol=1e-9)
    # nominal gondola (CoM 1.2 mm right of the pen axis): sign still flips between the sides
    m = true_model_from_config(CFG)
    th = np.degrees(m.tilt_angle(GRID))
    assert np.all(th[GRID[:, 0] == X0] * th[GRID[:, 0] == X1] < 0)
    # what it costs to ignore the tilt
    free = true_model_from_config(CFG, tilt=False)
    pen_err = np.linalg.norm(free.position(m.lengths(GRID)) - GRID, axis=1)       # same belt lengths, no rotation
    ref = (0.0, 0.0)
    mt, mf = Mapping.from_config(CFG, ref, ref), Mapping.from_config(deep_update(CFG, {"geometry": {"gondola_tilt": False}}), ref, ref)
    drawn_err = np.linalg.norm(mt.to_physical(mf.to_firmware(FINE)) - FINE, axis=1)   # host ignores tilt, machine tilts
    with capsys.disabled():
        print(f"\n[gondola] |theta| over the safe rect: mean {np.abs(th).mean():.2f} deg, max {np.abs(th).max():.2f} deg; "
              f"tilt ignored at equal belt lengths: pen error mean {pen_err.mean():.1f} / max {pen_err.max():.1f} mm; "
              f"host ignores tilt (ref at 0,0): drawing error mean {drawn_err.mean():.1f} / max {drawn_err.max():.1f} mm")
    assert pen_err.max() > 0.5 and drawn_err.max() > 0.5


def test_scalar_solver_matches_vectorised_model():
    m = true_model_from_config(CFG, com_height=40.0, com_x=-1.0)
    lens = m.lengths(GRID)
    th = m.tilt_angle(GRID)
    for p, l, t in zip(GRID, lens, th):
        ll, lr, g = gondola.pose_scalar(m, float(p[0]), float(p[1]), float(t))
        assert (ll, lr) == pytest.approx(tuple(l), abs=1e-9) and abs(g) < 1e-9
        got = gondola.solve_scalar(m, float(l[0]), float(l[1]), p[0] + 3.0, p[1] - 3.0, 0.0)
        assert got == pytest.approx((p[0], p[1], t), abs=1e-7)


def test_forward_kinematics_roundtrip_with_and_without_seed():
    m = true_model_from_config(CFG)
    lens = m.lengths(GRID)
    assert np.allclose(m.position(lens, seed=GRID + 6.0), GRID, atol=1e-7)
    assert np.allclose(m.position(lens), GRID, atol=1e-7)
    with pytest.raises(KinematicsError):
        m.lengths((AL[0] + 8.0 + 3.0, AL[1] - 176.0 - 4.0))                       # left attach point 5 mm from the pulley centre


# --------------------------------------------------------------------------------------- (3)(4) Mapping
@pytest.mark.parametrize("ref", [(0.0, 0.0), (300.0, 200.0), (750.0, 500.0)])
def test_mapping_roundtrip_over_the_whole_safe_rect(ref):
    m = Mapping.from_config(CFG, ref, ref)
    assert np.allclose(m.to_physical(m.to_firmware(FINE)), FINE, atol=1e-6)
    assert np.allclose(m.to_physical(m.to_firmware(GRID)), GRID, atol=1e-6)
    fw = m.path_to_firmware(GRID[:2])                                         # warm-started (memo) forward solve
    ph = m.to_physical(fw)
    assert np.allclose(ph[[0, -1]], GRID[:2], atol=1e-6) and np.allclose(ph, m.to_physical(fw.copy() + 0.0), atol=1e-9)


@pytest.mark.parametrize("ref", [(0.0, 0.0), (750.0, 500.0)])
def test_path_chord_error_on_a_long_diagonal(ref):
    m = Mapping.from_config(CFG, ref, ref)
    line = np.array([[X0, Y0], [X1, Y1]], float)
    fw = m.path_to_firmware(line, tolerance=0.02)
    dense = np.vstack([a + np.linspace(0, 1, 60)[:, None] * (b - a) for a, b in zip(fw[:-1], fw[1:])])
    phys = m.to_physical(dense)
    d = line[1] - line[0]
    dev = np.abs(d[0] * (phys[:, 1] - line[0][1]) - d[1] * (phys[:, 0] - line[0][0])) / np.hypot(*d)
    assert dev.max() < 0.03


# --------------------------------------------------------------------------------------- (5) end to end
_VARIANTS = {
    "nominal": {},
    "other-gondola": {"geometry": {"attach_half_spacing_mm": 11.0, "attach_height_mm": 150.0,
                                   "com_height_mm": 50.0, "com_offset_x_mm": -2.0}},
    "no-tilt": {"geometry": {"gondola_tilt": False}},
}


@pytest.mark.parametrize("shape", list(SHAPES))
@pytest.mark.parametrize("variant", list(_VARIANTS))
def test_drawing_through_the_simulator_with_the_same_gondola_model(shape, variant):
    cfg = deep_update(CFG, _VARIANTS[variant])
    emu = make_emu(cfg)
    m = Mapping.from_config(cfg, emu.physical_position(), emu.firmware_position())
    _draw(emu, cfg, SHAPES[shape], lambda p: m.path_to_firmware(p, 0.01, cfg.motion.max_chord_mm, cfg.motion.min_chord_mm))
    dev = deviation_to_polylines(drawn_points(emu), SHAPES[shape])
    assert dev.size > 20 and dev.max() < 0.06, f"{variant}/{shape}: {dev.max():.4f} mm"


def test_plant_overrides_for_the_gondola_and_ignoring_the_tilt_is_visible():
    gond = {"attach_half_spacing": 8.0, "attach_height": 176.0, "com_height": 33.8, "com_x": 1.2, "tilt": True}
    emu = make_emu(CFG, true_overrides=gond)                                   # explicit == configured: exact
    m = Mapping.from_config(CFG, emu.physical_position(), emu.firmware_position())
    pls = SHAPES["diagonal"]
    _draw(emu, CFG, pls, lambda p: m.path_to_firmware(p, 0.01))
    assert deviation_to_polylines(drawn_points(emu), pls).max() < 0.06
    emu2 = make_emu(CFG, true_overrides=gond)                                  # host that ignores the tilt
    cfg_free = deep_update(CFG, {"geometry": {"gondola_tilt": False}})
    m2 = Mapping.from_config(cfg_free, emu2.physical_position(), emu2.firmware_position())
    _draw(emu2, CFG, pls, lambda p: m2.path_to_firmware(p, 0.01))
    assert deviation_to_polylines(drawn_points(emu2), pls).max() > 2.0
    with pytest.raises(ValueError):
        make_emu(CFG, true_overrides={"nonsense": 1})


def test_plant_fast_solver_matches_true_model_with_tilt():
    emu = make_emu(CFG, true_overrides={"com_height": 45.0, "com_x": 0.5, "pen_dx": 1.0, "anchor_dx": 2.0})
    pl = emu.plant
    assert pl.truth.has_tilt
    rng = np.random.default_rng(5)
    for _ in range(30):
        p = np.array([rng.uniform(X0, X1), rng.uniform(Y0, Y1)])
        lt = pl.truth.lengths(p)
        got = pl._solve(float(lt[0]), float(lt[1]), tuple(p + rng.uniform(-4, 4, 2)))
        assert got == pytest.approx(tuple(p), abs=1e-6)


# --------------------------------------------------------------------------------------- (6) sensitivity
SENS = {
    "com_height 20": {"com_height_mm": 20.0}, "com_height 60": {"com_height_mm": 60.0},
    "com_x -3": {"com_offset_x_mm": -3.0}, "com_x +5": {"com_offset_x_mm": 5.0},
    "spacing +2": {"attach_half_spacing_mm": 10.0}, "spacing -2": {"attach_half_spacing_mm": 6.0},
    "height +2": {"attach_height_mm": 178.0}, "height -2": {"attach_height_mm": 174.0},
}


def _mismatch_error(host_patch: dict, ref=(0.0, 0.0)) -> np.ndarray:
    """Drawing error (mm) over the safe rect: the machine is the nominal gondola, the host assumes ``host_patch``."""
    host = deep_update(CFG, {"geometry": host_patch})
    truth_m, host_m = Mapping.from_config(CFG, ref, ref), Mapping.from_config(host, ref, ref)
    return np.linalg.norm(truth_m.to_physical(host_m.to_firmware(FINE)) - FINE, axis=1)


def test_sensitivity_to_wrong_gondola_parameters_is_documented(capsys):
    res = {k: _mismatch_error(v) for k, v in SENS.items()}
    with capsys.disabled():
        print("\n[gondola] max / mean drawing error (mm) when the host assumes a wrong gondola (reference at 0,0):")
        for k, e in res.items():
            print(f"  {k:14s} {e.max():6.2f} / {e.mean():5.2f}")
    # loose bounds: they only document the order of magnitude (a 1 mm CoM error costs about 1 mm in the drawing)
    for k, e in res.items():
        assert 0.3 < e.max() < 25.0, k
    assert max(res["com_height 20"].max(), res["com_height 60"].max()) < 12.0


# --------------------------------------------------------------------------------------- workspace / calibration
def test_workspace_statics_use_the_attach_points_and_configured_mass():
    pts = GRID
    assert np.allclose(tensions(CFG, pts), tensions(CFG, pts, 0.404))
    assert np.allclose(tensions(CFG, pts, 0.8), 2 * tensions(CFG, pts, 0.4))
    heavy = deep_update(CFG, {"geometry": {"gondola_mass_kg": 0.808}})
    assert np.allclose(tensions(heavy, pts), 2 * tensions(CFG, pts))
    assert np.all(tensions(CFG, pts) > 0.2)
    # attach points 176 mm above the pen: the belts become horizontal (amplification explodes) near y = 1041 - 176
    assert amplification(CFG, [[750, 860.0]])[0] > 20 > amplification(CFG, [[750, Y1]])[0]


def test_com_height_in_the_calibration_is_only_weakly_identifiable():
    """Crosses constrain the CoM height only to a few mm (see ``std``): optional, not a default parameter."""
    tg = suggest_targets(CFG, 6, 4)
    truth = {"com_height": 45.0, "com_x": 0.0, "anchor_right": (AR[0] + 2.0, AR[1])}
    pos = predicted_positions(CFG, (0.0, 0.0), (0.0, 0.0), tg, truth)
    ms = [Measurement(tuple(map(float, t)), tuple(map(float, p))) for t, p in zip(tg, pos)]
    base = fit_geometry(CFG, (0.0, 0.0), (0.0, 0.0), ms, ("anchor_left", "anchor_right", "scale"))
    withc = fit_geometry(CFG, (0.0, 0.0), (0.0, 0.0), ms, ("anchor_left", "anchor_right", "scale", "com_height"))
    assert withc.rms_after < base.rms_after
    assert withc.params["hc"] == pytest.approx(45.0, abs=8.0) and withc.std["hc"] > 1.0
    assert withc.patch["geometry"]["com_height_mm"] == pytest.approx(withc.params["hc"], abs=1e-2)
