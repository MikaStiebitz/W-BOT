"""Calibration: pattern generation, the least-squares solver, the wizard.

The solver is tested against *synthetic* measurements produced from known-true
parameters, so "did it work" has an exact answer.
"""

from __future__ import annotations

import numpy as np
import pytest

from wbbot.calib.patterns import build_pattern
from wbbot.calib.solve import (BeltReference, CalibrationMeasurement,
                               _make_kinematics, simulate_measurements,
                               solve_calibration)
from wbbot.calib.wizard import CalibrationWizard, ServoTuner, WizardStep
from wbbot.core.hershey import default_font
from wbbot.core.safearea import SafeAreaViolation

TRUTH = dict(ax_l=-104.0, ay_l=1347.0, ax_r=1603.5, ay_r=1352.5,
             k_left=1.004, k_right=0.997, c_left=3.5, c_right=-2.0,
             dx=1.5, dy=-2.5)


def belt_reference_for(cfg, truth, model, position=(750.0, 500.0)):
    """The tape-measured belt lengths a perfect operator would report."""
    p = np.array([truth["ax_l"], truth["ay_l"], truth["ax_r"], truth["ay_r"],
                  1, 1, 0, 0, truth["dx"], truth["dy"]])
    kin = _make_kinematics(model, p, cfg.geometry.pulley_pitch_radius_mm,
                           cfg.gondola.mass_kg)
    gondola = np.array(position) - np.array([truth["dx"], truth["dy"]])
    lengths = kin.inverse_many(gondola.reshape(1, 2))[0]
    return BeltReference(position=position, left_mm=float(lengths[0]),
                         right_mm=float(lengths[1]))


# ----------------------------------------------------------------------
# patterns
# ----------------------------------------------------------------------

def test_pattern_is_inside_the_safe_area(cfg, safe_area):
    pattern = build_pattern(cfg, safe_area)
    assert pattern.toolpath.validate(safe_area, raise_on_violation=False) == []
    assert np.all(safe_area.contains_many(pattern.toolpath.all_points()))


def test_pattern_target_count_and_numbering(cfg, safe_area):
    pattern = build_pattern(cfg, safe_area, n_x=4, n_y=3)
    assert len(pattern.targets) == 12
    assert [t.index for t in pattern.targets] == list(range(1, 13))
    assert all(t.label == str(t.index) for t in pattern.targets)


def test_pattern_targets_are_well_spread(cfg, safe_area):
    """Parameters only excited near the centre come out badly conditioned."""
    pattern = build_pattern(cfg, safe_area, n_x=3, n_y=3)
    pts = np.array([t.nominal for t in pattern.targets])
    assert pts[:, 0].max() - pts[:, 0].min() > 0.6 * cfg.board.width_mm
    assert pts[:, 1].max() - pts[:, 1].min() > 0.5 * cfg.board.height_mm


def test_pattern_includes_rectangle_and_diagonals(cfg, safe_area):
    with_rect = build_pattern(cfg, safe_area, draw_rectangle=True,
                              draw_diagonals=True)
    without = build_pattern(cfg, safe_area, draw_rectangle=False,
                            draw_diagonals=False)
    assert len(with_rect.toolpath.strokes) == len(without.toolpath.strokes) + 3


def test_pattern_labels_are_drawn(cfg, safe_area):
    """Numbers next to the crosses; measuring against the wrong target is the
    easiest way to ruin a calibration."""
    plain = build_pattern(cfg, safe_area, n_x=2, n_y=2, label_height_mm=14.0)
    font = default_font()
    expected_label_strokes = sum(
        len(font.render(str(i), 0, 0, 14.0)) for i in range(1, 5))
    assert expected_label_strokes > 0
    # 4 crosses (2 strokes each) + rectangle + 2 diagonals + labels
    assert len(plain.toolpath.strokes) == 4 * 2 + 3 + expected_label_strokes


def test_pattern_rejects_an_impossible_inset(cfg, safe_area):
    with pytest.raises(SafeAreaViolation):
        build_pattern(cfg, safe_area, inset_mm=2000.0)


def test_pattern_rejects_a_degenerate_grid(cfg, safe_area):
    with pytest.raises(ValueError):
        build_pattern(cfg, safe_area, n_x=1, n_y=3)


def test_pattern_instructions_mention_the_origin(cfg, safe_area):
    text = " ".join(build_pattern(cfg, safe_area).instructions()).lower()
    assert "lower-left" in text and "millimetre" in text


# ----------------------------------------------------------------------
# solver
# ----------------------------------------------------------------------

@pytest.mark.parametrize("model", ["naive", "tangent"])
def test_solver_recovers_a_noise_free_machine(cfg, safe_area, model):
    """Exact data in, exact positioning out."""
    targets = [t.nominal for t in build_pattern(cfg, safe_area, n_x=4, n_y=3).targets]
    measurements = simulate_measurements(cfg, targets, true_params=TRUTH,
                                         model=model)
    result = solve_calibration(measurements, cfg, model="auto",
                               reference=belt_reference_for(cfg, TRUTH, model))
    assert result.model == model, "the wrong belt model was selected"
    assert result.mean_error_mm < 0.01
    assert result.max_error_mm < 0.05
    assert result.converged


@pytest.mark.parametrize("model", ["naive", "tangent"])
def test_solver_meets_the_two_millimetre_target_with_realistic_noise(
        cfg, safe_area, model):
    """Milestone 6: residual under 2 mm with tape-measure-grade input."""
    targets = [t.nominal for t in build_pattern(cfg, safe_area, n_x=4, n_y=3).targets]
    measurements = simulate_measurements(cfg, targets, true_params=TRUTH,
                                         model=model, noise_mm=0.5, seed=1)
    result = solve_calibration(measurements, cfg, model="auto",
                               reference=belt_reference_for(cfg, TRUTH, model))
    assert result.mean_error_mm < 2.0
    assert result.max_error_mm < 2.0
    assert result.improvement_percent > 0


def test_solver_picks_the_model_that_actually_fits(cfg, safe_area):
    """The choice is made from the residual, not from theory."""
    targets = [t.nominal for t in build_pattern(cfg, safe_area, n_x=4, n_y=3).targets]
    for truth_model in ("naive", "tangent"):
        measurements = simulate_measurements(cfg, targets, true_params=TRUTH,
                                             model=truth_model)
        result = solve_calibration(
            measurements, cfg, model="auto",
            reference=belt_reference_for(cfg, TRUTH, truth_model))
        assert result.model == truth_model
        assert result.alternatives[truth_model] <= min(result.alternatives.values())


def test_solver_reports_the_worst_point(cfg, safe_area):
    targets = [t.nominal for t in build_pattern(cfg, safe_area, n_x=3, n_y=3).targets]
    measurements = simulate_measurements(cfg, targets, true_params=TRUTH,
                                         model="tangent", noise_mm=0.3, seed=2)
    # Spoil one measurement badly.
    bad = list(measurements)
    bad[4] = CalibrationMeasurement(
        commanded=bad[4].commanded,
        measured=(bad[4].measured[0] + 12.0, bad[4].measured[1] - 9.0),
        index=bad[4].index)
    result = solve_calibration(bad, cfg, model="tangent")
    assert result.worst_index == bad[4].index
    assert result.worst_point is not None


def test_solver_needs_enough_measurements(cfg):
    with pytest.raises(ValueError, match="at least 4"):
        solve_calibration([
            CalibrationMeasurement((100, 100), (101, 99), 1),
            CalibrationMeasurement((200, 200), (201, 199), 2),
        ], cfg)


def test_solver_warns_about_a_thin_dataset(cfg, safe_area):
    targets = [t.nominal for t in build_pattern(cfg, safe_area, n_x=2, n_y=2).targets]
    measurements = simulate_measurements(cfg, targets, true_params=TRUTH,
                                         model="naive")
    result = solve_calibration(measurements, cfg, model="naive")
    assert any("measurements for 10 parameters" in w for w in result.warnings)


def test_solver_warns_when_no_belt_reference_is_given(cfg, safe_area):
    targets = [t.nominal for t in build_pattern(cfg, safe_area).targets]
    measurements = simulate_measurements(cfg, targets, true_params=TRUTH,
                                         model="naive")
    result = solve_calibration(measurements, cfg, model="naive")
    assert any("belt reference" in w for w in result.warnings)


def test_fixed_scale_and_offset_can_be_disabled(cfg, safe_area):
    targets = [t.nominal for t in build_pattern(cfg, safe_area).targets]
    measurements = simulate_measurements(cfg, targets, true_params=TRUTH,
                                         model="naive")
    result = solve_calibration(measurements, cfg, model="naive",
                               fit_scale=False, fit_pen_offset=False)
    assert result.params["k_left"] == pytest.approx(1.0, abs=1e-6)
    assert result.params["dx"] == pytest.approx(0.0, abs=1e-6)


def test_result_report_and_yaml(cfg, safe_area):
    import yaml
    targets = [t.nominal for t in build_pattern(cfg, safe_area).targets]
    measurements = simulate_measurements(cfg, targets, true_params=TRUTH,
                                         model="tangent", noise_mm=0.3, seed=3)
    result = solve_calibration(measurements, cfg, model="auto")
    text = result.report()
    assert "mean deviation" in text and "fitted parameters" in text
    overlay = result.to_calibration_yaml(cfg)
    yaml.safe_dump(overlay)
    assert "geometry" in overlay and "drive" in overlay and "gondola" in overlay
    assert overlay["drive"]["steps_per_mm_left"] > 0


def test_calibration_overlay_reloads_into_a_valid_config(cfg, safe_area, tmp_path):
    from wbbot.config import load_config, save_calibration
    targets = [t.nominal for t in build_pattern(cfg, safe_area, n_x=4, n_y=3).targets]
    measurements = simulate_measurements(cfg, targets, true_params=TRUTH,
                                         model="tangent", noise_mm=0.2, seed=4)
    result = solve_calibration(measurements, cfg, model="auto")
    path = tmp_path / "calibration.yaml"
    save_calibration(result.to_calibration_yaml(cfg), path)

    from pathlib import Path
    reloaded = load_config(Path(__file__).parents[1] / "config" / "machine.yaml",
                           calibration_path=path)
    assert reloaded.geometry.kinematics_model == result.model
    assert reloaded.drive.steps_per_mm_left > 0
    # The fitted anchors must have actually taken effect.
    assert reloaded.geometry.anchor_left != cfg.geometry.anchor_left


# ----------------------------------------------------------------------
# servo tuner
# ----------------------------------------------------------------------

def test_servo_tuner_starts_from_the_config(cfg):
    tuner = ServoTuner(cfg)
    assert tuner.pen_up == cfg.servo.pen.up_pos
    assert tuner.revolver == list(cfg.servo.revolver.positions)
    assert tuner.validate() == []


def test_servo_tuner_clamps_to_the_axis_range(cfg):
    tuner = ServoTuner(cfg)
    assert tuner.set_pen_up(9999) == cfg.servo.pen.axis_max_travel
    assert tuner.set_pen_down(-50) == 0.0


def test_servo_tuner_flags_an_unusable_pen_stroke(cfg):
    tuner = ServoTuner(cfg)
    tuner.set_pen_up(10.0)
    tuner.set_pen_down(10.2)
    assert any("never lift clear" in p for p in tuner.validate())


def test_servo_tuner_flags_colliding_revolver_positions(cfg):
    tuner = ServoTuner(cfg)
    tuner.set_revolver(1, tuner.revolver[0] + 1.0)
    assert any("wrong marker" in p for p in tuner.validate())


def test_servo_tuner_rejects_a_bad_slot(cfg):
    with pytest.raises(ValueError):
        ServoTuner(cfg).set_revolver(9, 45.0)


# ----------------------------------------------------------------------
# wizard
# ----------------------------------------------------------------------

def test_wizard_happy_path(cfg, safe_area, tmp_path):
    wizard = CalibrationWizard(cfg, safe_area,
                               calibration_path=tmp_path / "calibration.yaml")
    assert wizard.begin()["step"] == WizardStep.REFERENCE.value

    ref = belt_reference_for(cfg, TRUTH, "tangent")
    state = wizard.set_reference(ref.position, ref.left_mm, ref.right_mm)
    assert state["step"] == WizardStep.PATTERN.value and state["has_reference"]

    pattern = wizard.build_pattern(n_x=4, n_y=3)
    assert wizard.step is WizardStep.MEASURE
    assert len(wizard.missing_targets) == len(pattern.targets)

    truth_measurements = simulate_measurements(
        cfg, [t.nominal for t in pattern.targets], true_params=TRUTH,
        model="tangent", noise_mm=0.4, seed=5)
    for target, m in zip(pattern.targets, truth_measurements):
        wizard.record(target.index, m.measured[0], m.measured[1])
    assert wizard.missing_targets == []

    result = wizard.solve()
    assert wizard.step is WizardStep.REVIEW
    assert result.mean_error_mm < 2.0

    path = wizard.save()
    assert path.exists() and wizard.step is WizardStep.SAVED
    import yaml
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["geometry"]["kinematics_model"] in ("naive", "tangent")
    assert len(saved["measurements"]) == len(pattern.targets)
    assert "belt_reference" in saved


def test_wizard_apply_returns_a_usable_config(cfg, safe_area):
    wizard = CalibrationWizard(cfg, safe_area)
    wizard.begin()
    wizard.skip_reference()
    pattern = wizard.build_pattern()
    ms = simulate_measurements(cfg, [t.nominal for t in pattern.targets],
                               true_params=TRUTH, model="naive")
    for t, m in zip(pattern.targets, ms):
        wizard.record(t.index, *m.measured)
    wizard.solve()
    applied = wizard.apply()
    assert applied.geometry.anchor_left != cfg.geometry.anchor_left
    from wbbot.core.safearea import SafeArea
    assert SafeArea(applied).polygon.area > 0


def test_wizard_rejects_an_unknown_target(cfg, safe_area):
    wizard = CalibrationWizard(cfg, safe_area)
    wizard.begin()
    wizard.skip_reference()
    wizard.build_pattern(n_x=2, n_y=2)
    with pytest.raises(ValueError, match="no target numbered"):
        wizard.record(99, 100.0, 100.0)


def test_wizard_rejects_an_off_board_measurement(cfg, safe_area):
    wizard = CalibrationWizard(cfg, safe_area)
    wizard.begin()
    wizard.skip_reference()
    wizard.build_pattern(n_x=2, n_y=2)
    with pytest.raises(ValueError, match="nowhere near the board"):
        wizard.record(1, 99999.0, 100.0)


def test_wizard_rejects_an_implausible_belt_reference(cfg, safe_area):
    wizard = CalibrationWizard(cfg, safe_area)
    wizard.begin()
    with pytest.raises(ValueError, match="tape measure"):
        wizard.set_reference((750, 500), left_mm=50.0, right_mm=50.0)


def test_wizard_rejects_a_reference_outside_the_safe_area(cfg, safe_area):
    wizard = CalibrationWizard(cfg, safe_area)
    wizard.begin()
    with pytest.raises(ValueError, match="not usable"):
        wizard.set_reference((-500, 500), 1000.0, 1000.0)


def test_wizard_flags_a_wildly_off_measurement(cfg, safe_area):
    wizard = CalibrationWizard(cfg, safe_area)
    wizard.begin()
    wizard.skip_reference()
    pattern = wizard.build_pattern(n_x=2, n_y=2)
    t = pattern.targets[0]
    wizard.record(t.index, t.nominal[0] + 200, t.nominal[1])
    assert any("that is a lot" in e for e in wizard.errors)


def test_wizard_re_recording_replaces_not_duplicates(cfg, safe_area):
    wizard = CalibrationWizard(cfg, safe_area)
    wizard.begin()
    wizard.skip_reference()
    wizard.build_pattern(n_x=2, n_y=2)
    wizard.record(1, 200.0, 200.0)
    wizard.record(1, 205.0, 203.0)
    assert len(wizard.measurements) == 1
    assert wizard.measurements[0].measured == (205.0, 203.0)


def test_wizard_needs_measurements_before_solving(cfg, safe_area):
    wizard = CalibrationWizard(cfg, safe_area)
    wizard.begin()
    wizard.skip_reference()
    wizard.build_pattern(n_x=2, n_y=2)
    with pytest.raises(ValueError, match="at least 4"):
        wizard.solve()


def test_wizard_state_is_json_serialisable(cfg, safe_area):
    import json
    wizard = CalibrationWizard(cfg, safe_area)
    wizard.begin()
    wizard.skip_reference()
    wizard.build_pattern(n_x=2, n_y=2)
    json.dumps(wizard.state())


# ----------------------------------------------------------------------
# corner calibration
# ----------------------------------------------------------------------

def corner_belief(cfg, true_left, true_right, corner):
    """Where the machine *believes* it is when the pen sits on ``corner``.

    Closed form: the physical belt lengths at the corner come from the true
    anchors; feeding those lengths through the firmware's nominal geometry
    gives the position the controller reports.
    """
    from wbbot.core.kinematics import NaiveKinematics
    nominal = NaiveKinematics(cfg.geometry.anchor_left,
                              cfg.geometry.anchor_right, 0.0)
    true_kin = NaiveKinematics(true_left, true_right, 0.0)
    return nominal.forward(true_kin.inverse(corner))


def test_corner_walk_recovers_shifted_anchors(cfg, safe_area):
    """Jog to the four corners of a misbuilt machine; the fit finds it."""
    true_left = (-104.0, 1346.0)
    true_right = (1603.0, 1352.5)

    wizard = CalibrationWizard(cfg, safe_area)
    state = wizard.begin_corners()
    assert state["step"] == "corners"
    assert len(state["corners"]["targets"]) == 4
    assert state["corners"]["labels"][0] == "oben links"

    for i, corner in enumerate(wizard.corner_targets):
        believed = corner_belief(cfg, true_left, true_right, corner)
        wizard.record_corner(i, float(believed[0]), float(believed[1]))
    assert wizard.missing_corners == []

    result = wizard.solve_corners()
    assert result.mean_error_mm < 0.05, result.report()
    p = result.params
    assert p["ax_l"] == pytest.approx(true_left[0], abs=0.5)
    assert p["ay_l"] == pytest.approx(true_left[1], abs=0.5)
    assert p["ax_r"] == pytest.approx(true_right[0], abs=0.5)
    assert p["ay_r"] == pytest.approx(true_right[1], abs=0.5)
    # The frozen parameters must not have moved.
    assert p["k_left"] == pytest.approx(1.0, abs=1e-6)
    assert p["c_left"] == pytest.approx(0.0, abs=1e-6)
    assert p["dx"] == pytest.approx(0.0, abs=1e-6)
    assert any("anchor positions only" in w for w in result.warnings)


def test_corner_walk_on_a_perfect_machine_changes_nothing(cfg, safe_area):
    wizard = CalibrationWizard(cfg, safe_area)
    wizard.begin_corners()
    for i, corner in enumerate(wizard.corner_targets):
        wizard.record_corner(i, corner[0], corner[1])
    result = wizard.solve_corners()
    assert result.mean_error_mm < 0.01
    assert result.params["ax_l"] == pytest.approx(
        cfg.geometry.anchor_left[0], abs=0.2)


def test_corner_solve_requires_all_four(cfg, safe_area):
    wizard = CalibrationWizard(cfg, safe_area)
    wizard.begin_corners()
    wizard.record_corner(0, 2.0, 998.0)
    with pytest.raises(ValueError, match="still missing"):
        wizard.solve_corners()


def test_recording_a_wildly_wrong_corner_is_refused(cfg, safe_area):
    wizard = CalibrationWizard(cfg, safe_area)
    wizard.begin_corners()
    with pytest.raises(ValueError, match="too far"):
        wizard.record_corner(0, 700.0, 300.0)     # nowhere near top-left
    with pytest.raises(ValueError, match="out of range"):
        wizard.record_corner(7, 0.0, 1000.0)


def test_recording_before_begin_is_refused(cfg, safe_area):
    wizard = CalibrationWizard(cfg, safe_area)
    with pytest.raises(RuntimeError, match="start the corner"):
        wizard.record_corner(0, 0.0, 1000.0)


def test_corner_result_saves_like_any_other(cfg, safe_area, tmp_path):
    import yaml as yaml_mod
    wizard = CalibrationWizard(cfg, safe_area,
                               calibration_path=tmp_path / "calibration.yaml")
    wizard.begin_corners()
    for i, corner in enumerate(wizard.corner_targets):
        believed = corner_belief(cfg, (-103.0, 1348.0), (1602.0, 1351.0), corner)
        wizard.record_corner(i, float(believed[0]), float(believed[1]))
    wizard.solve_corners()
    path = wizard.save()
    saved = yaml_mod.safe_load(path.read_text(encoding="utf-8"))
    assert "corner_records" in saved
    assert len(saved["corner_records"]) == 4
    assert saved["geometry"]["anchor_left"][0] == pytest.approx(-103.0, abs=0.5)
