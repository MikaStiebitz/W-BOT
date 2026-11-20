"""The generated firmware config — the one artefact that touches real hardware."""

from __future__ import annotations

from collections import Counter

import pytest
import yaml

from wbbot.driver.fluidnc_config import (DEFAULT_SCHEMA, SCHEMAS, VERIFY_KEYS,
                                         generate_fluidnc_config,
                                         verify_against_controller,
                                         write_fluidnc_config)


def collect_pins(node, out=None) -> list[str]:
    """Every pin name mentioned anywhere in the config tree."""
    out = [] if out is None else out
    if isinstance(node, dict):
        for value in node.values():
            if isinstance(value, (dict, list)):
                collect_pins(value, out)
            elif isinstance(value, str) and (value.startswith("I2SO.")
                                             or value.startswith("gpio.")):
                out.append(value.split(":")[0])
    elif isinstance(node, list):
        for value in node:
            collect_pins(value, out)
    return out


@pytest.mark.parametrize("schema", sorted(SCHEMAS))
def test_no_pin_is_declared_twice(cfg, schema):
    """FluidNC rejects a whole config file if one pin appears twice.

    The failure is nasty: the board reports the error, then falls back to its
    built-in defaults — so it looks like a config loaded, but the machine moves
    with the wrong kinematics. The DLC32 shares one enable line between both
    drivers, which is exactly the shape that invites this mistake.
    """
    pins = collect_pins(SCHEMAS[schema](cfg))
    duplicates = [p for p, n in Counter(pins).items() if n > 1]
    assert duplicates == [], f"schema {schema} declares {duplicates} twice"
    assert len(pins) >= 6, "the config lost its pin assignments"


@pytest.mark.parametrize("schema", sorted(SCHEMAS))
def test_config_is_valid_yaml_and_ascii(cfg, schema):
    text = generate_fluidnc_config(cfg, schema)
    assert text.isascii()
    body = yaml.safe_load(text)
    assert body["axes"]["x"]["steps_per_mm"] == cfg.drive.steps_per_mm_left
    assert body["axes"]["shared_stepper_disable_pin"]
    # The shared enable line must not be repeated inside a motor block.
    for axis in ("x", "y"):
        assert "disable_pin" not in body["axes"][axis]["motor0"]["stepstick"]


def test_servo_pins_match_the_machine_config(cfg):
    body = yaml.safe_load(generate_fluidnc_config(cfg))
    assert body["axes"]["z"]["motor0"]["rc_servo"]["output_pin"] == \
        cfg.servo.pen.output_pin
    assert body["axes"]["a"]["motor0"]["rc_servo"]["output_pin"] == \
        cfg.servo.revolver.output_pin


def test_servo_axes_use_esp32_output_capable_pins(cfg):
    """GPIO 34-39 on an ESP32 are input-only; a servo on one is silently dead."""
    body = yaml.safe_load(generate_fluidnc_config(cfg))
    for axis in ("z", "a"):
        pin = body["axes"][axis]["motor0"]["rc_servo"]["output_pin"]
        number = int(pin.split(".")[1].split(":")[0])
        assert not 34 <= number <= 39, f"{pin} is input-only on the ESP32"


def test_anchor_geometry_reaches_the_firmware(cfg):
    body = yaml.safe_load(generate_fluidnc_config(cfg))
    wall = body["kinematics"]["wall_plotter"]
    assert wall["left_anchor_x"] == cfg.geometry.anchor_left[0]
    assert wall["right_anchor_y"] == cfg.geometry.anchor_right[1]
    assert wall["left_axis"] == 0 and wall["right_axis"] == 1


def test_homing_and_soft_limits_stay_off(cfg):
    """No limit switches: the host owns the safe area (DECISIONS D-008)."""
    body = yaml.safe_load(generate_fluidnc_config(cfg))
    assert body["start"]["must_home"] is False
    for axis in ("x", "y", "z", "a"):
        assert body["axes"][axis]["soft_limits"] is False


def test_header_tells_the_operator_to_verify(cfg):
    text = generate_fluidnc_config(cfg)
    assert "VERIFY BEFORE FLASHING" in text
    assert "$CD" in text
    for key in VERIFY_KEYS:
        assert key in text


def test_schema_36_uses_the_older_kinematics_key(cfg):
    assert "wallplotter" in yaml.safe_load(generate_fluidnc_config(cfg, "3.6"))["kinematics"]
    assert "wall_plotter" in yaml.safe_load(generate_fluidnc_config(cfg, "3.7"))["kinematics"]


def test_unknown_schema_is_rejected(cfg):
    with pytest.raises(ValueError, match="unknown FluidNC schema"):
        generate_fluidnc_config(cfg, "1.0")


def test_verify_against_controller_accepts_our_own_output(cfg):
    dump = generate_fluidnc_config(cfg, DEFAULT_SCHEMA)
    report = verify_against_controller(cfg, dump, DEFAULT_SCHEMA)
    assert report["ok"], report


def test_verify_against_controller_reports_a_renamed_key(cfg):
    """The exact drift the brief warned about."""
    dump = generate_fluidnc_config(cfg, "3.6")          # older key spelling
    report = verify_against_controller(cfg, dump, "3.7")
    assert not report["ok"]
    assert any("wall_plotter" in k for k in report["missing_keys"])
    assert report["notes"]


def test_verify_against_controller_reports_a_changed_value(cfg):
    dump = generate_fluidnc_config(cfg).replace("left_anchor_x: -100.0",
                                                "left_anchor_x: -95.0")
    report = verify_against_controller(cfg, dump)
    assert not report["ok"]
    assert any(m["key"].endswith("left_anchor_x") for m in report["mismatched"])


def test_verify_against_controller_survives_garbage(cfg):
    assert not verify_against_controller(cfg, "{{{not yaml")["ok"]
    assert not verify_against_controller(cfg, "- a\n- b\n")["ok"]


def test_write_fluidnc_config(cfg, tmp_path):
    path = write_fluidnc_config(cfg, tmp_path / "config.yaml")
    assert path.exists()
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["board"]
