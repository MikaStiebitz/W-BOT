"""Emulator behaviour tests (firmware side): boot, status, parser/errors, motion, planner, realtime commands.

Run together with ``wbot_tests/test_sim.py`` (plant, key pre-distortion test, link semantics).
"""
from __future__ import annotations

import pytest
import yaml

from wbot.config import Config, deep_update
from wbot.servo import ServoModel
from wbot.sim import FluidNCEmulator

try:                                    # package layout (wbot_tests/__init__.py) or rootdir-less layout
    from .sim_helpers import Host, g1_lines, make_emu
except ImportError:                     # pragma: no cover
    from sim_helpers import Host, g1_lines, make_emu

CFG = Config()
PEN = ServoModel(CFG.servos.pen)
REV = ServoModel(CFG.servos.revolver)
# Reported MPos is derived from whole motor steps (1/steps_per_mm belt resolution). Near the firmware origin the
# belts of the new anchor layout have an amplification of ~1.6 (<= 2 assumed): worst-case position quantisation.
QUANT = 2.0 * (0.5 / CFG.steps_per_mm) * 2 ** 0.5


def host(**kw) -> Host:
    return Host(make_emu(**kw))


# --------------------------------------------------------------------------------------- boot / status
def test_boot_banner_and_axes():
    emu = FluidNCEmulator(CFG, realtime=False)
    emu.open()
    text = emu.read(0).decode()
    assert "Grbl 3.9 [FluidNC v3.9.9" in text
    assert "[MSG:INFO: Kinematic system: WallPlotter]" in text
    assert "[MSG:INFO: Axis count 4]" in text
    assert text.index("Kinematic system") < text.index("Grbl 3.9")
    emu.close()


def test_status_idle_and_axes_count():
    h = host()
    st = h.status()
    assert st["state"] == "Idle" and st["MPos"] == [0.0, 0.0, 0.0, 0.0] and st["FS"] == [0, 0]
    assert "WCO" in st                                     # first report carries WCO like real FluidNC
    assert "WCO" not in h.status()
    cfg3 = deep_update(CFG, {"servos": {"revolver": {"enabled": False}}, "colors": [{"name": "a"}, {"name": "b"}, {"name": "c"}]})
    h3 = host(cfg=cfg3)
    assert len(h3.status()["MPos"]) == 3
    assert h3.cmd("G1 A10 F100") == ["error:20"]
    emu = FluidNCEmulator(cfg3, realtime=False)
    emu.open()
    assert "Axis count 3" in emu.read(0).decode()


def test_boot_state_firmware_position_and_servos():
    emu = make_emu(physical_start=(750.0, 500.0))
    assert emu.firmware_position() == pytest.approx(CFG.firmware.boot_position, abs=1e-6)
    assert emu.physical_position() == (750.0, 500.0)
    ang = emu.servo_angles()
    assert ang["pen"] == pytest.approx(0.0, abs=1e-6)                       # Z axis 0 = centre = 0 deg
    assert ang["revolver"] == pytest.approx(REV.min_deg, abs=1e-6)         # A axis 0 = fw_min_us = min_deg
    assert not emu.plant.pen_down(ang["pen"])                               # centre is equidistant from up/down: counted as lifted


def test_info_gcode_modes_and_dump():
    h = host()
    info = h.cmd("$I")
    assert info[-1] == "ok" and info[0].startswith("[VER:3.9 FluidNC v3.9.9")
    assert h.cmd("$G")[0].startswith("[GC:G0 G54") and h.cmd("G91 G20")[-1] == "ok"
    assert "G91" in h.cmd("$G")[0] and "G20" in h.cmd("$G")[0]
    dump = h.cmd("$CD")
    assert dump[-1] == "ok"
    cfgd = yaml.safe_load("\n".join(dump[:-1]))
    wp = cfgd["kinematics"]["WallPlotter"]
    assert wp["left_anchor_x"] == CFG.fw_anchor_left[0] and wp["right_anchor_y"] == CFG.fw_anchor_right[1]
    assert wp["left_anchor_y"] == CFG.fw_anchor_left[1] and wp["right_anchor_x"] == CFG.fw_anchor_right[0]
    assert wp["segment_length"] == 1.0
    z = cfgd["axes"]["z"]["motor0"]["rc_servo"]
    assert (z["min_pulse_us"], z["max_pulse_us"]) == (PEN.fw_min_us, PEN.fw_max_us)
    assert cfgd["axes"]["a"]["motor0"]["rc_servo"]["output_pin"] == "gpio.32"
    assert cfgd["stepping"]["engine"] == "I2S_STATIC"


# --------------------------------------------------------------------------------------- parser / errors
def test_error_codes():
    h = host()
    assert h.cmd("$FOO") == ["error:3"]
    assert h.cmd("G2 X10 Y10 I5 F100") == ["error:20"]
    assert h.cmd("G1 B5 F100") == ["error:20"]
    assert h.cmd("M99") == ["error:20"]
    assert h.cmd("G1 X F100") == ["error:2"]
    assert h.cmd("G1 X10") == ["error:22"]                       # no feed rate defined yet
    assert h.cmd("G0 G1 X10") == ["error:21"]
    assert h.cmd("G1 X1 X2 F100") == ["error:25"]
    assert h.cmd("G1 X99999999 F100") == ["error:33"]
    out = h.cmd("?X")                                             # '?' inside a line is realtime, the rest 'X' is bad
    assert out[0].startswith("<Idle|") and out[1] == "error:2"
    assert h.cmd("%") == ["ok"] and h.cmd("") == ["ok"] and h.cmd("; just a comment") == ["ok"]
    assert h.cmd("G1" + "X1" * 200 + " F10")[-1] == "error:11"    # line too long
    assert h.cmd("G92") == ["error:26"]
    assert h.cmd("G4") == ["error:28"]


def test_comments_case_and_line_endings():
    h = host()
    assert h.cmd("g1 x10 (move) y0 f600 ; tail") == ["ok"]
    h.send("G91\r\n")
    h.send("G90\r")
    h.send("G90\n")
    assert h.pump() == ["ok", "ok", "ok"]                         # CRLF counts as one terminator
    assert h.cmd("G21 G90 G94") == ["ok"]


def test_verbose_errors_and_inject_error():
    h = host(verbose_errors=True)
    assert h.cmd("$FOO")[0].startswith("error:3 (")
    emu = h.emu
    emu.inject_error(20, count=2)
    assert h.cmd("G90") == ["error:20 (Unsupported command)"] and h.cmd("G90")[0].startswith("error:20")
    assert h.cmd("G90") == ["ok"]


def test_relative_units_and_work_offset():
    h = host()
    assert h.cmd("G91 G1 X10 Y0 F6000") == ["ok"]
    h.run()
    assert h.status()["MPos"][0] == pytest.approx(10.0, abs=0.02)
    h.cmd("G92 X0 Y0")                                            # work zero here (machine 10,0)
    st = h.status()
    assert st.get("WCO", [None])[0] is None or st["WCO"][0] == pytest.approx(10.0, abs=0.02)
    h.cmd("G90 G0 X5")                                            # work X5 = machine 15
    h.run()
    assert h.status()["MPos"][0] == pytest.approx(15.0, abs=0.02)
    h.cmd("G20 G0 X1")                                            # 1 inch in work coordinates -> machine 35.4
    h.run()
    assert h.status()["MPos"][0] == pytest.approx(35.4, abs=0.02)
    assert h.cmd("$10=0") == ["ok"]
    assert "WPos" in h.status()


# --------------------------------------------------------------------------------------- motion & timing
def test_trapezoid_timing():
    h = host()
    h.cmd("G1 X100 Y0 F1500")
    used = h.run()
    # L/v + v/a = 100/25 + 25/250 (naive firmware belt kinematics: motors are not Cartesian, firmware path is)
    assert used == pytest.approx(100 / 25 + 25 / 250, abs=0.1)
    assert h.status()["MPos"][0] == pytest.approx(100.0, abs=0.02)


def test_junctions_do_not_stop_at_every_vertex():
    h = host()
    pts = [(i * 1.0, 0.0) for i in range(1, 101)]
    assert h.stream(g1_lines(pts, 1500)) == ["ok"] * 100
    h.run()
    straight = h.emu.sim_time
    assert straight < 100 / 25 + 1.0                              # stop-and-go would need ~100 * 0.2 s
    h2 = host()
    zig = [(i * 1.0, (i % 2) * 1.0) for i in range(1, 41)]        # sharp zigzag corners slow down
    h2.stream(g1_lines(zig, 1500))
    h2.run()
    assert h2.emu.sim_time > straight * 0.5 + 1.0


def test_servo_move_takes_feed_time_and_horn_lags():
    h = host()
    assert h.cmd("G1 Z20 F3000") == ["ok"]
    assert h.status()["state"] == "Run"
    used = h.run()
    assert used == pytest.approx(20 / 50, abs=0.05)               # 20 mm at 50 mm/s = 0.4 s
    angle = PEN.angle_for_axis(20.0)
    assert h.emu.servo_angles()["pen"] == pytest.approx(angle, abs=0.01)
    assert h.status()["MPos"][2] == pytest.approx(20.0)
    # horn slews with speed_deg_s: a 60 deg step needs >= 0.2 s even though the axis value arrives sooner
    h.cmd("G1 Z-40 F600000")
    h.emu.advance(0.05)
    a = h.emu.servo_angles()["pen"]
    assert a > PEN.angle_for_axis(-40.0) + 5                      # still travelling
    h.run()
    assert h.emu.servo_angles()["pen"] == pytest.approx(PEN.angle_for_axis(-40.0), abs=0.01)


def test_servo_clamp_pulse_window():
    h = host()
    assert h.cmd("G1 Z200 F60000") == ["ok"]                      # outside the axis range: parser accepts
    h.run()
    assert h.status()["MPos"][2] == pytest.approx(200.0)
    assert h.emu.servo_angles()["pen"] == pytest.approx(PEN.max_deg, abs=1e-6)   # horn does not go further
    h.cmd("G1 Z-500 F60000")
    h.run()
    assert h.emu.servo_angles()["pen"] == pytest.approx(PEN.min_deg, abs=1e-6)
    h.cmd("G1 A500 F60000")
    h.run()
    assert h.emu.servo_angles()["revolver"] == pytest.approx(REV.max_deg, abs=1e-6)
    h.cmd("G1 A90 F60000")                                        # 90 = axis value of 0 deg
    h.run()
    assert h.emu.servo_angles()["revolver"] == pytest.approx(REV.angle_for_axis(90.0), abs=1e-3)


def test_dwell_timing_and_state():
    h = host()
    h.cmd("G4 P0.5")
    assert h.take() == []                                         # ok withheld while dwelling
    t0 = h.emu.sim_time
    assert h.status(0.2)["state"] == "Idle"                       # status still answered
    h.pump(0.5)
    assert h.take() == ["ok"]
    assert h.emu.sim_time - t0 == pytest.approx(0.7, abs=0.01)
    assert h.cmd("G4 P0") == ["ok"]
    # dwell waits for the motion to finish first
    h.cmd("G1 X25 F1500")
    h.cmd("G4 P1.0")
    t_start = h.emu.sim_time
    h.run()
    assert "ok" in h.take()
    assert h.emu.sim_time - t_start == pytest.approx(25 / 25 + 0.1 + 1.0, abs=0.05)


def test_planner_backpressure():
    h = host()
    h.cmd("$10=3")
    h.take()
    oks = 0
    for i in range(1, 21):                                        # time frozen: nothing completes
        oks += h.cmd(f"G1 X{i * 3} F300").count("ok")
    assert oks == 16                                              # 17th line is held: ok only when accepted
    assert h.emu.stats["max_planner_depth"] == 16
    st = h.status()
    assert st["state"] == "Run" and st["Bf"][0] == 0              # planner full
    assert st["Bf"][1] < 127                                      # held bytes sit in the RX buffer
    assert h.emu.rx_overflows == 0
    h.pump(4.0)                                                   # first block (3 mm at 5 mm/s) completes
    assert h.take().count("ok") >= 1


def test_rx_overflow_detected():
    h = host()
    for i in range(1, 18):                                        # fill planner, 17th line blocks
        h.cmd(f"G1 X{i * 3} F300")
    blob = "".join(f"G1 X{100 + i} F300\n" for i in range(20))    # 300+ bytes while blocked
    h.send(blob)
    out = h.pump()
    assert any("RX overflow" in ln for ln in out)
    assert h.emu.rx_overflows > 100                               # a 128 byte buffer cannot take 320 bytes


def test_character_counting_streaming_never_overflows():
    h = host()
    pts = [(i * 2.0, 0.0) for i in range(1, 80)]
    replies = h.stream(g1_lines(pts, 3000), rx_limit=127)
    assert replies == ["ok"] * len(pts)
    assert h.emu.rx_overflows == 0 and not [ln for ln in h.lines if "ERR" in ln]
    h.run()
    assert h.status()["MPos"][0] == pytest.approx(158.0, abs=QUANT)


# --------------------------------------------------------------------------------------- realtime commands
def test_feed_hold_and_resume():
    h = host()
    h.cmd("G1 X100 F1500")
    h.emu.advance(1.0)
    h.send("!")
    st = h.status()
    assert st["state"] == "Hold:1"
    h.emu.advance(0.3)
    st = h.status()
    assert st["state"] == "Hold:0" and st["FS"][0] == 0
    x = st["MPos"][0]
    assert 10 < x < 100
    h.emu.advance(2.0)
    assert h.status()["MPos"][0] == x                             # really standing still
    assert h.cmd("G4 P0.1") == []                                 # new lines are accepted but nothing moves
    h.send("~")
    assert h.status()["state"] == "Run"
    h.run()
    assert h.status()["MPos"][0] == pytest.approx(100.0, abs=0.02)
    h.send("!")
    assert h.status()["state"] == "Hold:0"                        # hold while idle
    h.send("~")
    assert h.status()["state"] == "Idle"
    h.send("~")
    assert h.status()["state"] == "Idle"


def test_resume_ignored_while_still_decelerating():
    h = host()
    h.cmd("G1 X100 F3000")
    h.emu.advance(1.0)
    h.send("!")
    h.send("~")                                                   # Hold:1 -> resume is ignored
    assert h.status()["state"] == "Hold:1"


def test_soft_reset_while_idle_keeps_everything():
    h = host()
    h.cmd("G1 Z20 F3000")
    h.cmd("G1 X20 F6000")
    h.run()
    h.take()
    before = (h.emu.firmware_position(), h.emu.physical_position(), h.emu.servo_angles())
    h.send("\x18")
    out = h.pump()
    assert out == ["Grbl 3.9 [FluidNC v3.9.9 (wifi) '$' for help]"]   # no ALARM: it was not moving
    assert h.status()["state"] == "Idle"
    assert (h.emu.firmware_position(), h.emu.physical_position(), h.emu.servo_angles()) == before
    assert h.cmd("G1 X30") == ["error:22"]                        # modal state (feed) was reset


def test_soft_reset_while_moving_raises_alarm3():
    h = host()
    h.cmd("G1 X200 F3000")
    h.emu.advance(1.0)
    h.send("\x18")
    out = h.pump()
    assert out[0] == "ALARM:3" and out[1].startswith("Grbl 3.9")
    st = h.status()
    assert st["state"] == "Alarm"
    x = st["MPos"][0]
    assert 5 < x < 200
    h.emu.advance(5.0)
    assert h.status()["MPos"][0] == x                             # flushed, motors stopped
    assert h.cmd("G1 X10 F100") == ["error:9"]
    assert h.cmd("$X") == ["[MSG:Caution: Unlocked]", "ok"]
    assert h.status()["state"] == "Idle"
    assert h.cmd("G1 X10 F6000") == ["ok"]


def test_alarm_injection_and_unlock():
    h = host()
    h.cmd("G1 X100 F1500")
    h.emu.advance(1.0)
    h.emu.inject_alarm(1)
    assert "ALARM:1" in h.pump()
    assert h.status()["state"] == "Alarm"
    assert h.cmd("G1 X0 F100") == ["error:9"]
    assert h.cmd("$J=G91 X1 F100") == ["error:9"]
    assert h.cmd("$I")[-1] == "ok"                                # information commands still work
    assert h.cmd("$X") == ["[MSG:Caution: Unlocked]", "ok"]
    assert h.status()["state"] == "Idle"


def test_jog_and_jog_cancel():
    h = host()
    assert h.cmd("$J=G91 X50 F600") == ["ok"]
    assert h.status()["state"] == "Jog"
    assert h.cmd("G1 X0 F100") == ["error:9"]                     # G-code is locked out while jogging
    h.emu.advance(1.0)
    h.send("\x85")
    assert h.status()["state"] == "Jog"                           # still decelerating
    h.emu.advance(0.5)
    st = h.status()
    assert st["state"] == "Idle"
    x = st["MPos"][0]
    assert 5 < x < 25
    assert h.cmd("$J=G91 X10 F600") == ["ok"]                     # relative to where it really stopped
    h.run()
    assert h.status()["MPos"][0] == pytest.approx(x + 10, abs=2 * QUANT)
    h.cmd("G1 X300 F600")
    assert h.cmd("$J=G91 X1 F600") == ["error:8"]                 # $J only when idle
    h.send("\x18")
    h.take()
    h.cmd("$X")
    assert h.cmd("$J=G91 F600") == ["error:16"]                   # no axis words
    assert h.cmd("$J=G91 X1") == ["error:22"]
    assert h.cmd("$J=G91 G1 X1 F10") == ["error:16"]


def test_jog_runs_to_completion_and_chains():
    h = host()
    assert h.cmd("$J=G91 X20 F3000") == ["ok"] and h.cmd("$J=G91 X20 Y10 F3000") == ["ok"]
    h.run()
    st = h.status()
    assert st["state"] == "Idle" and st["MPos"][:2] == pytest.approx([40.0, 10.0], abs=0.03)


def test_reset_during_hold_alarms_only_with_queued_motion():
    h = host()
    h.cmd("G1 X100 F1500")
    h.emu.advance(1.0)
    h.send("!")
    h.emu.advance(1.0)
    assert h.status()["state"] == "Hold:0"
    h.send("\x18")
    assert h.pump()[0] == "ALARM:3"                               # like real FluidNC: motion was suspended
    h2 = host()
    h2.send("!")
    assert h2.status()["state"] == "Hold:0"
    h2.send("\x18")
    assert h2.pump() == ["Grbl 3.9 [FluidNC v3.9.9 (wifi) '$' for help]"] and h2.status()["state"] == "Idle"


def test_read_chunking_reassembles_lines():
    emu = make_emu(read_chunk=3)
    emu.write(b"$I\n")
    data = b""
    for _ in range(100):
        chunk = emu.read(0)
        assert len(chunk) <= 3
        data += chunk
    assert b"[VER:3.9 FluidNC" in data and b"Grbl 3.9" in data and data.endswith(b"ok\r\n")


def test_servo_boot_angle_override_and_slew_speed_factor():
    emu = make_emu(boot_servo_deg={"pen": 60.0, "revolver": 0.0})
    assert emu.servo_angles() == {"pen": 60.0, "revolver": 0.0}
    emu.advance(0.05)
    assert emu.servo_angles()["pen"] < 60.0                       # horn travels from 60 deg to the boot pulse (0 deg)
    emu.advance(1.0)
    assert emu.servo_angles()["pen"] == pytest.approx(0.0, abs=1e-6)
    slow = make_emu(servo_speed_factor=0.1)
    Host(slow).cmd("G1 Z60 F600000")
    slow.advance(0.1)
    assert slow.servo_angles()["pen"] < 10.0                      # a sluggish servo: 50 deg/s
