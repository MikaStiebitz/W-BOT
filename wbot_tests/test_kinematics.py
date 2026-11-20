import numpy as np
import pytest

from wbot.config import Config, deep_update, load_config
from wbot.kinematics import (FirmwareModel, KinematicsError, Mapping, TrueModel,
                             uncompensated_error)
from wbot.servo import ServoLimitError, ServoModel

CFG = load_config()
_X0, _Y0, _X1, _Y1 = CFG.safe_rect()
# corners / centre / edges of the configured safe area (the motors sit only 41 mm above the board, so points
# above it or outside the side margins are not expressible in FluidNC's coordinates - that is the point of safe_rect)
PTS = np.array([[_X0, _Y0], [750, 500], [_X1, _Y1], [_X0 + 10, _Y1 - 20], [_X1 - 20, _Y0 + 40], [_X0 + 5, _Y1]], float)


def test_firmware_roundtrip():
    fw = FirmwareModel(CFG.fw_anchor_left, CFG.fw_anchor_right)
    assert np.allclose(fw.position(fw.lengths(PTS)), PTS, atol=1e-9)


def test_true_model_jacobian_matches_finite_difference():
    t = TrueModel(CFG.geometry.anchor_left, CFG.geometry.anchor_right, 12.732, (3.0, -2.0))
    _, jac = t.lengths_and_jac(PTS)
    eps = 1e-6
    for k in range(2):
        d = np.zeros(2)
        d[k] = eps
        num = (t.lengths(PTS + d) - t.lengths(PTS - d)) / (2 * eps)
        assert np.allclose(jac[:, :, k], num, atol=1e-6)


def test_true_forward_roundtrip():
    t = TrueModel(CFG.geometry.anchor_left, CFG.geometry.anchor_right, 12.732)
    assert np.allclose(t.position(t.lengths(PTS)), PTS, atol=1e-7)


@pytest.mark.parametrize("ref", [(750, 500), (400, 300), (5, 5)])
def test_mapping_roundtrip(ref):
    m = Mapping.from_config(CFG, ref, ref)
    assert np.allclose(m.to_physical(m.to_firmware(PTS)), PTS, atol=1e-7)


def test_reference_point_maps_to_ref_firmware():
    m = Mapping.from_config(CFG, (750, 500), (740, 510))
    assert np.allclose(m.to_firmware((750, 500)), (740, 510), atol=1e-9)
    assert np.allclose(m.to_physical((740, 510)), (750, 500), atol=1e-7)


def test_naive_model_is_identity():
    cfg = deep_update(CFG, {"geometry": {"model": "naive"}})
    m = Mapping.from_config(cfg, (750, 500))
    assert np.allclose(m.to_firmware(PTS), PTS, atol=1e-9)


def test_wrap_term_matters():
    """Without pre-distortion the pen is several mm off (D-002: up to ~13 mm)."""
    err = uncompensated_error(CFG, PTS)
    assert err.max() > 5.0
    assert err.min() < 1e-6          # reference point itself is exact


def test_path_chord_error_small():
    m = Mapping.from_config(CFG, (750, 500))
    line = np.array([[_X0, _Y0], [_X1, _Y1]], float)
    fw = m.path_to_firmware(line, tolerance=0.02)
    dense = np.vstack([a + np.linspace(0, 1, 60)[:, None] * (b - a) for a, b in zip(fw[:-1], fw[1:])])
    phys = m.to_physical(dense)
    d = line[1] - line[0]
    dev = np.abs(d[0] * (phys[:, 1] - line[0][1]) - d[1] * (phys[:, 0] - line[0][0])) / np.hypot(*d)
    assert dev.max() < 0.03


def test_belt_scale_error_is_compensated_in_mapping():
    cfg = deep_update(CFG, {"geometry": {"belt_scale_left": 1.01}})
    m = Mapping.from_config(cfg, (750, 500))
    assert np.allclose(m.to_physical(m.to_firmware(PTS)), PTS, atol=1e-7)


def test_pen_too_close_to_pulley_raises():
    t = TrueModel(CFG.geometry.anchor_left, CFG.geometry.anchor_right, 12.732)
    with pytest.raises(KinematicsError):
        ax, ay = CFG.geometry.anchor_left
        t.lengths((ax + 3.0, ay - 4.0))             # 5 mm from the pulley centre (< pulley radius)


def test_servo_model_windows():
    pen = ServoModel(CFG.servos.pen)
    assert (pen.fw_min_us, pen.fw_max_us) == (1256, 1744)
    assert pen.axis_value(0.0) == pytest.approx(0.0, abs=1e-9)
    assert pen.pulse_us(60) == pytest.approx(1500 + 60 * 1000 / 180)
    rev = ServoModel(CFG.servos.revolver)
    assert (rev.fw_min_us, rev.fw_max_us) == (1134, 1866)
    assert [rev.axis_value(a) for a in CFG.servos.revolver.slots_deg] == pytest.approx([5.89, 66.0, 126.11], abs=0.02)
    with pytest.raises(ServoLimitError):
        pen.axis_value(95)
    with pytest.raises(ServoLimitError):
        pen.axis_value(-120)


def test_config_roundtrip_and_validation():
    assert CFG.steps_per_mm == 40.0
    with pytest.raises(Exception):
        deep_update(CFG, {"servos": {"pen": {"up_deg": 120}}})
    assert isinstance(Config.model_validate(CFG.model_dump(mode="json")), Config)


def test_default_workspace_is_statically_sound():
    """The configured safe area keeps the belts steep enough and both belts loaded."""
    from wbot.workspace import analyze
    rep = analyze(CFG)
    assert rep.amp_max <= 5.0, rep
    assert rep.tension_min_n >= 0.25, rep
    assert CFG.safe_rect() == (120.0, 10.0, 1380.0, 750.0)
