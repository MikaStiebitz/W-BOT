"""FluidNC config generation, $CD verification and boot-log checks."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from wbot.config import deep_update, load_config
from wbot.fluidnc_config import (ConfigGenError, check_boot_log, expected_config,
                                 generate_firmware_config, has_errors, servo_axes, verify_dump)
from wbot.servo import ServoModel

ROOT = Path(__file__).resolve().parent.parent
LIVE = ROOT / "backup" / "config-live-2026-11-20.yaml"

CFG = load_config()                                                  # revolver enabled
CFG_NO_REV = deep_update(CFG, {"servos": {"revolver": {"enabled": False}}})


def _parse(text: str) -> dict:
    return yaml.safe_load(text)


def _flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        p = f"{prefix}{k}"
        out.update(_flatten(v, p + ".") if isinstance(v, dict) else {p: v})
    return out


def _live() -> dict:
    lines = [ln for ln in LIVE.read_text(encoding="utf-8").splitlines() if ln.strip() != "ok"]
    return yaml.safe_load("\n".join(lines))


# ------------------------------------------------------------------ generator
def test_output_is_ascii_valid_yaml_with_trailing_newline():
    for cfg in (CFG, CFG_NO_REV):
        text = generate_firmware_config(cfg)
        text.encode("ascii")                                         # no umlauts, no surprises
        assert text.endswith("\n") and not text.endswith("\n\n")
        assert _parse(text) == expected_config(cfg)


def test_matches_live_backup_except_intended_differences():
    """Revolver disabled == the live file, apart from the listed, deliberate changes."""
    new = _flatten(_parse(generate_firmware_config(CFG_NO_REV)))
    old = _flatten(_live())
    # (key -> (live value, generated value))
    intended = {
        "meta": (old["meta"], new["meta"]),                          # generator provenance text
        "axes.z.max_travel_mm": (180.0, 88.0),                       # axis -44..44 instead of 0..180
        "axes.z.homing.mpos_mm": (-90.0, -44.0),
        "axes.z.motor0.rc_servo.min_pulse_us": (1250, 1256),         # 5.556 us/deg, +-44 deg
        "axes.z.motor0.rc_servo.max_pulse_us": (1750, 1744),
        "axes.z.motor0.rc_servo.output_pin": ("gpio.22", "gpio.4"),  # IO22/Probe smears the signal -> SCL
    }
    # the motors now sit clamped on top of the whiteboard frame: the kinematics anchors (the one thing the
    # live board was NOT flashed with) are intended differences, derived from the configured geometry
    fw_l, fw_r = CFG_NO_REV.fw_anchor_left, CFG_NO_REV.fw_anchor_right
    for key, new_v in (("left_anchor_x", fw_l[0]), ("left_anchor_y", fw_l[1]),
                       ("right_anchor_x", fw_r[0]), ("right_anchor_y", fw_r[1])):
        k = f"kinematics.WallPlotter.{key}"
        intended[k] = (old[k], float(new_v))
    diff = {k: (old.get(k), new.get(k)) for k in old.keys() | new.keys() if old.get(k) != new.get(k)}
    assert diff == intended


def test_core_facts():
    d = _parse(generate_firmware_config(CFG))
    assert list(d["kinematics"]) == ["WallPlotter"]                  # NOT wall_plotter (D-029)
    k = d["kinematics"]["WallPlotter"]
    assert (k["left_anchor_x"], k["left_anchor_y"]) == (47.0, 1041.0)       # motors on the whiteboard frame
    assert (k["right_anchor_x"], k["right_anchor_y"]) == (1453.0, 1041.0)
    assert (k["left_axis"], k["right_axis"]) == (0, 1)
    assert d["stepping"]["engine"] == "I2S_STATIC" and d["stepping"]["idle_ms"] == 255   # D-033
    assert d["i2so"] == {"bck_pin": "gpio.16", "data_pin": "gpio.21", "ws_pin": "gpio.17"}
    assert d["axes"]["shared_stepper_disable_pin"] == "I2SO.0"
    for ax in ("x", "y"):
        a = d["axes"][ax]
        assert a["steps_per_mm"] == 40.0 and a["max_travel_mm"] == 2700.0
        assert a["soft_limits"] is False and a["homing"] == {"cycle": 0}
        assert a["max_rate_mm_per_min"] == 3000.0 and a["acceleration_mm_per_sec2"] == 250.0
    assert d["axes"]["x"]["motor0"]["stepstick"] == {"step_pin": "I2SO.1", "direction_pin": "I2SO.2"}
    assert d["axes"]["y"]["motor0"]["stepstick"] == {"step_pin": "I2SO.5", "direction_pin": "I2SO.6"}
    assert d["start"]["must_home"] is False


def test_firmware_anchors_and_numbers_follow_config():
    cfg = deep_update(CFG, {"firmware": {"anchor_left": [-90, 1300], "segment_length_mm": 2.0,
                                          "max_rate_mm_min": 2400},
                            "motion": {"acceleration_mm_s2": 180}})
    d = _parse(generate_firmware_config(cfg))
    k = d["kinematics"]["WallPlotter"]
    assert (k["left_anchor_x"], k["left_anchor_y"], k["segment_length"]) == (-90.0, 1300.0, 2.0)
    assert d["axes"]["x"]["max_rate_mm_per_min"] == 2400.0
    assert d["axes"]["y"]["acceleration_mm_per_sec2"] == 180.0


@pytest.mark.parametrize("left,right", [(False, False), (True, False), (False, True), (True, True)])
def test_direction_inversion(left, right):
    cfg = deep_update(CFG, {"drive": {"invert_left": left, "invert_right": right}})
    ax = _parse(generate_firmware_config(cfg))["axes"]
    assert ax["x"]["motor0"]["stepstick"]["direction_pin"] == ("I2SO.2:low" if left else "I2SO.2")
    assert ax["y"]["motor0"]["stepstick"]["direction_pin"] == ("I2SO.6:low" if right else "I2SO.6")


def test_servo_pulse_windows_equal_servomodel():
    d = _parse(generate_firmware_config(CFG))
    for letter, spec in servo_axes(CFG):
        m = ServoModel(spec)
        rc = d["axes"][letter]["motor0"]["rc_servo"]
        assert (rc["min_pulse_us"], rc["max_pulse_us"]) == (m.fw_min_us, m.fw_max_us)
        assert rc["pwm_hz"] == 50 and rc["output_pin"] == spec.pin
        a = d["axes"][letter]
        assert a["homing"]["positive_direction"] is False and a["steps_per_mm"] == 1.0
        assert a["max_rate_mm_per_min"] == 600000.0 and a["soft_limits"] is False
        assert (a["homing"]["mpos_mm"], a["max_travel_mm"]) == (spec.axis_min, spec.axis_travel)
    pen, rev = d["axes"]["z"], d["axes"]["a"]
    assert (pen["motor0"]["rc_servo"]["min_pulse_us"], pen["motor0"]["rc_servo"]["max_pulse_us"]) == (1256, 1744)
    assert (pen["homing"]["mpos_mm"], pen["max_travel_mm"]) == (-44.0, 88.0)
    assert (rev["motor0"]["rc_servo"]["min_pulse_us"], rev["motor0"]["rc_servo"]["max_pulse_us"]) == (1134, 1866)
    assert (rev["homing"]["mpos_mm"], rev["max_travel_mm"]) == (0.0, 132.0)
    assert (pen["motor0"]["rc_servo"]["output_pin"], rev["motor0"]["rc_servo"]["output_pin"]) == ("gpio.4", "gpio.32")


def _fluidnc_pulse(axis: dict, pos: float) -> float:
    """FluidNC RcServo::set_location with positive_direction false."""
    rc = axis["motor0"]["rc_servo"]
    amin = axis["homing"]["mpos_mm"]
    amax = amin + axis["max_travel_mm"]
    pos = min(max(pos, amin), amax)                                  # mapConstrain clamps
    return rc["min_pulse_us"] + (pos - amin) / (amax - amin) * (rc["max_pulse_us"] - rc["min_pulse_us"])


@pytest.mark.parametrize("letter,angles", [("z", [-44, -30, -10, 0, 20, 30, 44]),
                                           ("a", [-66, -60, 0, 60, 66])])
def test_axis_value_maps_back_to_the_right_pulse(letter, angles):
    d = _parse(generate_firmware_config(CFG))
    spec = dict(servo_axes(CFG))[letter]
    m = ServoModel(spec)
    for ang in angles:
        pos = m.axis_value(ang)
        assert _fluidnc_pulse(d["axes"][letter], pos) == pytest.approx(m.pulse_us(m.clamp(ang)), abs=1e-6)
        assert m.angle_for_axis(pos) == pytest.approx(m.clamp(ang), abs=1e-6)


def test_pen_axis_zero_is_servo_centre_and_window_is_hard_limit():
    d = _parse(generate_firmware_config(CFG))
    z = d["axes"]["z"]
    assert _fluidnc_pulse(z, 0.0) == pytest.approx(1500.0, abs=0.6)   # power-up position = centre
    assert _fluidnc_pulse(z, 500.0) == 1744 and _fluidnc_pulse(z, -500.0) == 1256


def test_revolver_disabled_omits_axis_with_explanation():
    text = generate_firmware_config(CFG_NO_REV)
    d = _parse(text)
    assert "a" not in d["axes"] and "z" in d["axes"]
    assert "NO_PIN" not in yaml.safe_dump(d)                         # never a pinless servo
    assert "ENTFERNT" in text and "error:9" in text
    assert "gpio.32" not in yaml.safe_dump(d)


def test_servo_axes_must_be_contiguous():
    cfg = deep_update(CFG, {"servos": {"pen": {"axis": "A"}, "revolver": {"axis": "Z", "enabled": False}}})
    with pytest.raises(ConfigGenError):
        generate_firmware_config(cfg)


def test_generation_is_deterministic():
    assert generate_firmware_config(CFG) == generate_firmware_config(CFG)


# ------------------------------------------------------------------ verify_dump
def _dump_of(cfg=CFG, mutate=None, trailer="ok\n") -> str:
    d = _parse(generate_firmware_config(cfg))
    if mutate:
        mutate(d)
    return yaml.safe_dump(d, sort_keys=False) + trailer


def test_verify_clean_dump():
    assert verify_dump(CFG, _dump_of()) == []
    assert verify_dump(CFG_NO_REV, _dump_of(CFG_NO_REV)) == []


def test_verify_tolerates_chatter_extras_and_three_decimal_numbers():
    d = _parse(generate_firmware_config(CFG))
    d["axes"]["x"]["homing"]["feed_mm_per_min"] = 100.0              # firmware prints defaults too
    d["axes"]["x"]["steps_per_mm"] = "40.000"
    d["kinematics"]["WallPlotter"]["left_anchor_x"] = "47.000"
    text = "[MSG:INFO: something]\r\n" + yaml.safe_dump(d, sort_keys=False).replace("\n", "\r\n") + "ok\r\n"
    assert verify_dump(CFG, text) == []


def _errors(issues):
    return {i.key for i in issues if i.severity == "error"}


def test_verify_wrong_kinematics_name_is_flagged():
    def mut(d):
        d["kinematics"] = {"wall_plotter": d["kinematics"]["WallPlotter"]}
    issues = verify_dump(CFG, _dump_of(mutate=mut))
    assert "kinematics" in _errors(issues)
    assert "kartesisch" in next(i for i in issues if i.key == "kinematics").message_de
    assert verify_dump(CFG, _dump_of(mutate=lambda d: d.pop("kinematics")))[0].key == "kinematics"


def test_verify_detects_each_dangerous_deviation():
    cases = {
        "stepping.idle_ms": lambda d: d["stepping"].update(idle_ms=254),
        "kinematics.WallPlotter.left_anchor_y": lambda d: d["kinematics"]["WallPlotter"].update(left_anchor_y=1300.0),
        "axes.x.steps_per_mm": lambda d: d["axes"]["x"].update(steps_per_mm=80.0),
        "axes.y.motor0.stepstick.direction_pin": lambda d: d["axes"]["y"]["motor0"]["stepstick"].update(direction_pin="I2SO.6:low"),
        "axes.x.soft_limits": lambda d: d["axes"]["x"].update(soft_limits=True),
        "axes.y.homing.cycle": lambda d: d["axes"]["y"]["homing"].update(cycle=1),
        "axes.z.motor0.rc_servo.min_pulse_us": lambda d: d["axes"]["z"]["motor0"]["rc_servo"].update(min_pulse_us=1250),
        "axes.z.motor0.rc_servo.max_pulse_us": lambda d: d["axes"]["z"]["motor0"]["rc_servo"].update(max_pulse_us=1750),
        "axes.z.homing.positive_direction": lambda d: d["axes"]["z"]["homing"].update(positive_direction=True),
        "axes.z.homing.mpos_mm": lambda d: d["axes"]["z"]["homing"].update(mpos_mm=-90.0),
        "axes.z.max_travel_mm": lambda d: d["axes"]["z"].update(max_travel_mm=180.0),
        "axes.a.motor0.rc_servo.output_pin": lambda d: d["axes"]["a"]["motor0"]["rc_servo"].update(output_pin="NO_PIN"),
        "i2so.bck_pin": lambda d: d["i2so"].update(bck_pin="gpio.18"),
        "stepping.engine": lambda d: d["stepping"].update(engine="RMT"),
        "start.must_home": lambda d: d["start"].update(must_home=True),
        "axes.shared_stepper_disable_pin": lambda d: d["axes"].update(shared_stepper_disable_pin="I2SO.3"),
        "axes.a": lambda d: d["axes"].pop("a"),
    }
    for key, mut in cases.items():
        issues = verify_dump(CFG, _dump_of(mutate=mut))
        assert key in _errors(issues), key
        bad = next(i for i in issues if i.key == key)
        assert bad.message_de and bad.severity == "error"


def test_verify_warns_about_unexpected_revolver_axis():
    issues = verify_dump(CFG_NO_REV, _dump_of(CFG))                  # firmware still has axis A
    assert [(i.severity, i.key) for i in issues] == [("warn", "axes.a")]


def test_verify_garbage_dump():
    for text in ("", "ok\n", "not: [valid", "- a\n- b\n"):
        issues = verify_dump(CFG, text)
        assert len(issues) == 1 and issues[0].key == "dump" and issues[0].severity == "error"


def test_verify_old_live_file_reports_exactly_the_known_gaps():
    issues = verify_dump(CFG_NO_REV, LIVE.read_text(encoding="utf-8"))
    # the live file was flashed with the OLD anchors (-100/1350, 1600/1350): the new geometry is a known gap, too
    assert _errors(issues) == {"axes.z.max_travel_mm", "axes.z.homing.mpos_mm",
                               "axes.z.motor0.rc_servo.min_pulse_us", "axes.z.motor0.rc_servo.max_pulse_us",
                               "axes.z.motor0.rc_servo.output_pin",
                               "kinematics.WallPlotter.left_anchor_x", "kinematics.WallPlotter.left_anchor_y",
                               "kinematics.WallPlotter.right_anchor_x", "kinematics.WallPlotter.right_anchor_y"}
    assert has_errors(issues)


def test_verify_inverted_drive_expectation():
    cfg = deep_update(CFG, {"drive": {"invert_left": True}})
    assert "axes.x.motor0.stepstick.direction_pin" in _errors(verify_dump(cfg, _dump_of(CFG)))
    assert verify_dump(cfg, _dump_of(cfg)) == []


# ------------------------------------------------------------------ boot log
GOOD_LOG = """\
Grbl 3.9 [FluidNC v3.9.9 (wifi) '$' for help]
[MSG:INFO: FluidNC v3.9.9]
[MSG:INFO: Machine wbbot V-plotter]
[MSG:INFO: Board MKS DLC32 V2.1]
[MSG:INFO: Axis count 4]
[MSG:INFO: Axis X (-2700.000,0.000)]
[MSG:INFO: Axis Y (-2700.000,0.000)]
[MSG:INFO: Axis Z (-44.000,44.000)]
[MSG:INFO: Axis A (0.000,132.000)]
[MSG:INFO: Kinematic system: WallPlotter]
[MSG:INFO: Init Done]
"""


def test_boot_log_good():
    assert check_boot_log(GOOD_LOG, CFG) == []
    assert check_boot_log(GOOD_LOG) == []
    no_rev = GOOD_LOG.replace("Axis count 4", "Axis count 3").replace("[MSG:INFO: Axis A (0.000,132.000)]\n", "")
    assert check_boot_log(no_rev, CFG_NO_REV) == []


def test_boot_log_ignored_key_cartesian_is_an_error():
    log = GOOD_LOG.replace("WallPlotter", "Cartesian").replace(
        "[MSG:INFO: Axis count 4]", "[MSG:ERR: Ignored key wall_plotter]\n[MSG:INFO: Axis count 4]")
    issues = check_boot_log(log, CFG)
    assert {i.key for i in issues} == {"kinematics", "ignored_key.wall_plotter"}
    assert all(i.severity == "error" for i in issues)


def test_boot_log_ignored_key_alone_is_flagged_even_with_wallplotter():
    log = GOOD_LOG + "[MSG:ERR: Ignored key segmentlength]\n"
    issues = check_boot_log(log, CFG)
    assert [i.key for i in issues] == ["ignored_key.segmentlength"]


def test_boot_log_errors_and_missing_kinematics():
    issues = check_boot_log("[MSG:ERR: RC Servo disabled: No output pin]\n[MSG:INFO: Axis count 4]\n", CFG)
    keys = {i.key for i in issues}
    assert "kinematics" in keys and "boot_error" in keys


def test_boot_log_axis_count_and_ranges():
    wrong_count = check_boot_log(GOOD_LOG.replace("Axis count 4", "Axis count 3"), CFG)
    assert [i.key for i in wrong_count] == ["axis_count"]
    old_range = check_boot_log(GOOD_LOG.replace("(-44.000,44.000)", "(0.000,180.000)"), CFG)
    assert [i.key for i in old_range] == ["axis_range.Z"]
    neg = check_boot_log(GOOD_LOG.replace("(0.000,132.000)", "(-132.000,0.000)"), CFG)    # D-030 failure
    assert [i.key for i in neg] == ["axis_range.A"]
    unexpected_a = check_boot_log(GOOD_LOG.replace("Axis count 4", "Axis count 3"), CFG_NO_REV)
    assert {i.key for i in unexpected_a} == {"axis_range.A"}
    no_count = check_boot_log(GOOD_LOG.replace("[MSG:INFO: Axis count 4]\n", ""), CFG)
    assert [(i.severity, i.key) for i in no_count] == [("warn", "axis_count")]
