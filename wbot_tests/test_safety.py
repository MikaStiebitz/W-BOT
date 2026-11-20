from wbot.config import load_config
from wbot.kinematics import Mapping
from wbot.safety import validate_gcode

from wbot.servo import ServoModel

CFG = load_config()
M = Mapping.from_config(CFG, (750, 500))
PEN, REV = ServoModel(CFG.servos.pen), ServoModel(CFG.servos.revolver)
Z_UP, Z_DOWN = PEN.axis_value(CFG.servos.pen.up_deg), PEN.axis_value(CFG.servos.pen.down_deg)
A0 = REV.axis_value(CFG.servos.revolver.slots_deg[0])


def test_good_sequence_passes():
    lines = ["G21", "G90", f"G1 Z{Z_UP:.3f} F30000", "G4 P0.5", f"G1 A{A0:.3f} F30000", "G4 P0.5", "G1 X750 Y500 F3000"]
    assert validate_gcode(lines, CFG, M, z_axis=Z_DOWN) == []


def test_revolver_with_pen_down_refused():
    p = validate_gcode([f"G1 A{A0:.3f} F100"], CFG, M, z_axis=Z_DOWN)
    assert p and "pen" in p[0].message


def test_revolver_and_motion_in_one_line_refused():
    p = validate_gcode([f"G1 A{A0:.3f} X5 Y5"], CFG, M, z_axis=Z_UP)
    assert any("same line" in x.message for x in p)


def test_out_of_bounds_refused():
    assert validate_gcode(["G1 X5000 Y10"], CFG, M)
    assert validate_gcode(["G1 X750 Y5000"], CFG, M)


def test_servo_limit_refused():
    assert validate_gcode(["G1 Z95"], CFG, M)


def test_relative_xy_refused_and_unreferenced_refused():
    assert validate_gcode(["G91", "G1 X5 Y5"], CFG, M)
    assert validate_gcode(["G1 X5 Y5"], CFG, None)


def test_way_in_from_outside_is_allowed_but_only_once():
    lines = ["G1 X1 Y1 F3000", "G1 X750 Y500", "G1 X1 Y1"]      # firmware coords; ref (750,500)
    m = Mapping.from_config(CFG, (0, 0), (0, 0))
    lines = ["G1 X5 Y5 F3000", "G1 X300 Y300", "G1 X5 Y5"]
    assert len(validate_gcode(lines, CFG, m)) >= 2
    p = validate_gcode(lines, CFG, m, start_outside=True)
    assert [x.index for x in p] == [2]                           # leaving again is refused


def test_revolver_needs_settle_time_after_lifting_the_pen():
    """The steel pen hits the ring edge if the revolver turns before the pen is really up."""
    too_early = [f"G1 Z{Z_UP:.3f} F30000", f"G1 A{A0:.3f} F30000"]
    assert any("too soon" in p.message for p in validate_gcode(too_early, CFG, M, z_axis=Z_DOWN))
    short = [f"G1 Z{Z_UP:.3f} F30000", "G4 P0.05", f"G1 A{A0:.3f} F30000"]
    assert any("too soon" in p.message for p in validate_gcode(short, CFG, M, z_axis=Z_DOWN))
    ok = [f"G1 Z{Z_UP:.3f} F30000", f"G4 P{CFG.servos.pen.settle_s:.2f}", f"G1 A{A0:.3f} F30000"]
    assert validate_gcode(ok, CFG, M, z_axis=Z_DOWN) == []
    # pen already confirmed up (no Z move in the batch): no extra wait needed
    assert validate_gcode([f"G1 A{A0:.3f} F30000"], CFG, M, z_axis=Z_UP) == []
