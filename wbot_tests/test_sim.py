"""Simulator tests: FluidNC emulator behaviour and the physical plant (pre-distortion KEY test)."""
from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from wbot.config import Config, deep_update, load_config
from wbot.fluidnc.link import LinkError
from wbot.kinematics import Mapping
from wbot.servo import ServoModel
from wbot.sim import FluidNCEmulator, make_sim_link

try:                                    # package layout (wbot_tests/__init__.py) or rootdir-less layout
    from .legacy_geometry import legacy_cfg
    from .sim_helpers import Host, deviation_to_polylines, drawn_points, g1_lines, make_emu, servo_a, servo_z
except ImportError:                     # pragma: no cover
    from legacy_geometry import legacy_cfg
    from sim_helpers import Host, deviation_to_polylines, drawn_points, g1_lines, make_emu, servo_a, servo_z

CFG = Config()
RECT = load_config().safe_rect()      # the pen's usable area on the default machine (120, 10, 1380, 900)
PEN = ServoModel(CFG.servos.pen)
REV = ServoModel(CFG.servos.revolver)


def host(**kw) -> Host:
    return Host(make_emu(**kw))


# --------------------------------------------------------------------------------------- link semantics
def test_link_errors_and_fault_injection_realtime():
    emu = FluidNCEmulator(CFG, speed=100.0)
    with pytest.raises(LinkError):
        emu.write(b"?")                                           # not open yet
    emu.open()
    assert emu.is_open and emu.read(1.0).startswith(b"[MSG")      # boot text arrives without a poll
    t0 = time.monotonic()
    assert emu.read(0.05) == b"" and time.monotonic() - t0 < 1.0   # timeout -> b""
    emu.write(b"?")
    assert b"<Idle|" in emu.read(1.0)
    emu.pause_output()
    emu.write(b"?")
    assert emu.read(0.1) == b""                                   # withheld
    emu.resume_output()
    assert b"<Idle|" in emu.read(1.0)
    emu.drop_link()
    with pytest.raises(LinkError):
        emu.read(0.1)
    with pytest.raises(LinkError):
        emu.write(b"?")
    assert not emu.is_open
    emu.close()
    emu = FluidNCEmulator(CFG)
    emu.open()
    emu.close()
    with pytest.raises(LinkError):
        emu.read(0.01)


def test_blocked_reader_is_released_by_close():
    emu = FluidNCEmulator(CFG)
    emu.open()
    emu.read(1.0)
    errs = []

    def reader():
        try:
            emu.read(5.0)
        except LinkError as exc:
            errs.append(exc)

    th = threading.Thread(target=reader)
    th.start()
    time.sleep(0.1)
    emu.close()
    th.join(2.0)
    assert errs and not th.is_alive()


def test_realtime_thread_runs_motion_and_is_thread_safe():
    emu = FluidNCEmulator(CFG, speed=200.0)
    emu.open()
    emu.write(b"G1 X100 Y50 F3000\n")
    stop = threading.Event()
    polled = []

    def poller():
        while not stop.is_set():
            emu.write(b"?")
            time.sleep(0.002)

    th = threading.Thread(target=poller)
    th.start()
    t0 = time.monotonic()
    assert emu.wait_idle(10.0)
    stop.set()
    th.join()
    wall = time.monotonic() - t0
    text = b""
    while True:
        chunk = emu.read(0.05)
        if not chunk:
            break
        text += chunk
    assert b"\r\nok\r\n" in b"\r\n" + text and b"<Idle|" in text        # polled reports came back
    assert wall < 3.0 and emu.sim_time > 3.0                      # ~4 s of machine time in well under 3 s
    assert emu.firmware_position() == pytest.approx((100.0, 50.0), abs=0.02)
    emu.close()


def test_reopen_reboots_firmware_but_not_the_gondola():
    emu = make_emu()
    h = Host(emu)
    h.cmd("G1 X100 Y50 F6000")
    h.run()
    phys = emu.physical_position()
    assert emu.firmware_position() == pytest.approx((100.0, 50.0), abs=0.02)
    emu.close()
    emu.open()
    assert "Grbl 3.9" in emu.read(0).decode()
    assert emu.firmware_position() == pytest.approx(CFG.firmware.boot_position, abs=1e-6)
    assert emu.physical_position() == pytest.approx(phys, abs=1e-6)
    # relative motion from the new firmware origin is still physically consistent (no jump)
    h = Host(emu)
    h.cmd("G91 G1 X10 F6000")
    h.run()
    assert np.hypot(*(np.array(emu.physical_position()) - phys)) < 12.0


# --------------------------------------------------------------------------------------- trace
def test_trace_pen_state_and_colour():
    h = host()
    h.cmd(f"G1 Z{servo_z(CFG, CFG.servos.pen.down_deg):.4f} F6000")   # pen down first (boot position is neutral)
    h.cmd("G4 P0.3")
    h.cmd("G1 X30 F6000")
    h.run()
    assert h.emu.trace and all(s.pen_down for s in h.emu.trace)
    h.emu.reset_trace()
    assert h.emu.trace == []
    h.cmd(f"G1 Z{servo_z(CFG, CFG.servos.pen.up_deg):.4f} F6000")
    h.cmd("G4 P0.3")
    h.cmd("G1 X60 F6000")
    h.cmd(f"G1 A{servo_a(CFG, 60):.4f} F6000")
    h.cmd("G4 P0.3")
    h.cmd(f"G1 Z{servo_z(CFG, CFG.servos.pen.down_deg):.4f} F6000")
    h.cmd("G4 P0.3")
    h.cmd("G1 X90 F6000")
    h.run()
    tr = h.emu.trace
    assert tr and tr[0].t >= 0
    moves = [s for s in tr if np.hypot(s.x1 - s.x0, s.y1 - s.y0) > 0]
    up = [s for s in moves if not s.pen_down]
    down = [s for s in moves if s.pen_down]
    assert up and down
    assert all(s.color_slot == 0 for s in up if s.x1 <= 60.01)    # first stroke: revolver at -90 deg -> slot 0
    assert all(s.color_slot == 2 for s in down)                   # +60 deg slot while drawing the last stroke
    assert max(s.x1 for s in up) < max(s.x1 for s in down)
    assert [s.t for s in tr] == sorted(s.t for s in tr)


def test_pen_servo_order_matters_in_trace():
    """Moving while the pen is down is visible in the trace (the host must lift first)."""
    h = host()
    h.cmd(f"G1 Z{servo_z(CFG, CFG.servos.pen.up_deg):.4f} F6000")
    h.run()
    h.cmd(f"G1 Z{servo_z(CFG, CFG.servos.pen.down_deg):.4f} F6000")
    h.cmd("G1 X20 F6000")                                         # no wait: horn lags only a few ms
    h.run()
    assert drawn_points(h.emu).size > 0


# --------------------------------------------------------------------------------------- KEY test
def _square(c, w):
    x, y = c
    return np.array([(x - w, y - w), (x + w, y - w), (x + w, y + w), (x - w, y + w), (x - w, y - w)], float)


def _circle(c, r, n=96):
    t = np.linspace(0, 2 * np.pi, n + 1)
    return np.stack([c[0] + r * np.cos(t), c[1] + r * np.sin(t)], axis=1)


# Shapes stay in the lower/middle board: the emulator quantises belt lengths to whole steps (1/40 mm) and the
# belts get flat towards the top (gondola V2: attach points 176 mm above the pen, usable area ends at y=750), so the
# pen error floor rises there (~0.05 mm at y=700, amplification 2.9).
SHAPES = {
    "square": [_square((750, 350), 300)],
    "circle": [_circle((750, 350), 300)],
    "diagonal": [np.array([(RECT[0] + 10.0, RECT[1] + 10.0), (RECT[2] - 10.0, RECT[3] - 10.0)])],
}


def _draw(emu, cfg, polylines, to_fw, feed=1500.0):
    """Pen up -> travel -> pen down -> draw -> pen up, exactly in the order the planner uses."""
    h = Host(emu)
    up, dn = servo_z(cfg, cfg.servos.pen.up_deg), servo_z(cfg, cfg.servos.pen.down_deg)
    lines = [f"G90 G1 Z{up:.4f} F6000", "G4 P0.3"]
    for pl in polylines:
        s = to_fw(pl[:1])[0]
        lines += [f"G1 X{s[0]:.4f} Y{s[1]:.4f} F3000", f"G1 Z{dn:.4f} F6000", "G4 P0.3"]
        lines += g1_lines(to_fw(pl), feed)
        lines += [f"G1 Z{up:.4f} F6000", "G4 P0.3"]
    replies = h.stream(lines)
    assert all(r == "ok" for r in replies), replies
    h.run()
    return h


@pytest.mark.parametrize("shape", list(SHAPES))
# None = standard power-up (pen hangs at (0,0) = firmware origin); (300, 200) = a non-default pairing.
# A reference far from the firmware origin (e.g. board centre) leaves parts of the board unreachable
# (FluidNC's belt circles no longer intersect, see Mapping.unreachable_points) and is not a valid pairing here.
@pytest.mark.parametrize("start", [None, (300.0, 200.0)])
def test_predistortion_is_exact_on_the_true_plant(shape, start):
    cfg = CFG
    emu = make_emu(cfg, physical_start=start)
    P0 = emu.physical_position()
    F0 = emu.firmware_position()
    m = Mapping.from_config(cfg, P0, F0)
    pls = SHAPES[shape]
    tol, mc, mn = 0.01, cfg.motion.max_chord_mm, cfg.motion.min_chord_mm
    _draw(emu, cfg, pls, lambda p: m.path_to_firmware(p, tol, mc, mn))
    pts = drawn_points(emu)
    assert len(pts) > 20
    dev = deviation_to_polylines(pts, pls)
    assert dev.max() < 0.06, f"{shape}: max deviation {dev.max():.4f} mm"      # step quantisation floor ~0.05 (V2 gondola)
    # the whole stroke was really drawn
    drawn = sum(np.hypot(s.x1 - s.x0, s.y1 - s.y0) for s in emu.trace if s.pen_down)
    want = sum(np.linalg.norm(np.diff(p, axis=0), axis=1).sum() for p in pls)
    assert drawn == pytest.approx(want, rel=0.01)
    assert emu.rx_overflows == 0


@pytest.mark.parametrize("shape", ["square", "circle"])
def test_predistortion_is_exact_on_the_legacy_geometry(shape):
    cfg = legacy_cfg()
    emu = make_emu(cfg)
    m = Mapping.from_config(cfg, emu.physical_position(), emu.firmware_position())
    pls = SHAPES[shape]
    _draw(emu, cfg, pls, lambda p: m.path_to_firmware(p, 0.01, cfg.motion.max_chord_mm, cfg.motion.min_chord_mm))
    assert deviation_to_polylines(drawn_points(emu), pls).max() < 0.05


@pytest.mark.parametrize("shape", ["square", "diagonal"])
def test_without_predistortion_the_drawing_is_off_by_millimetres(shape):
    emu = make_emu(CFG)
    pls = SHAPES[shape]
    _draw(emu, CFG, pls, lambda p: np.asarray(p, float))          # naive: send the physical coordinates
    dev = deviation_to_polylines(drawn_points(emu), pls)
    assert dev.max() > 2.0, f"{shape}: only {dev.max():.2f} mm off without pre-distortion"


def test_true_overrides_break_or_validate_calibration():
    pls = SHAPES["square"]
    perturb = {"radius": 15.5, "pen_dx": 2.0, "pen_dy": -1.5, "scale_left": 1.002, "scale_right": 0.9985,
               "anchor_dx": 3.0}
    # host believes the nominal machine -> visible error
    emu = make_emu(CFG, true_overrides=perturb)
    m = Mapping.from_config(CFG, emu.physical_position(), emu.firmware_position())
    _draw(emu, CFG, pls, lambda p: m.path_to_firmware(p, 0.01))
    dev = deviation_to_polylines(drawn_points(emu), pls)
    assert dev.max() > 0.5
    # host knows the truth -> exact again
    truth_cfg = deep_update(CFG, {"geometry": {"pulley_pitch_radius_mm": 15.5, "pen_offset_mm": [2.0, -1.5],
                                               "belt_scale_left": 1.002, "belt_scale_right": 0.9985,
                                               "anchor_right": [CFG.geometry.anchor_right[0] + 3.0,      # anchor_dx
                                                                CFG.geometry.anchor_right[1]]},
                                    "firmware": {"anchor_left": list(CFG.fw_anchor_left),
                                                 "anchor_right": list(CFG.fw_anchor_right)}})
    emu2 = make_emu(CFG, true_overrides=perturb)
    # reference pen position equals the physical start; model built from the true parameters
    m2 = Mapping.from_config(truth_cfg, emu2.physical_position(), emu2.firmware_position())
    _draw(emu2, CFG, pls, lambda p: m2.path_to_firmware(p, 0.01))
    dev2 = deviation_to_polylines(drawn_points(emu2), pls)
    assert dev2.max() < 0.06
    with pytest.raises(ValueError):
        make_emu(CFG, true_overrides={"nonsense": 1})


def test_plant_fast_solver_matches_true_model():
    emu = make_emu(CFG, true_overrides={"radius": 14.0, "pen_dx": 3.0, "pen_dy": -2.0, "anchor_dx": 4.0})
    pl = emu.plant
    rng = np.random.default_rng(3)
    for _ in range(40):
        p = np.array([rng.uniform(30, 1470), rng.uniform(30, 970)])
        lt = pl.truth.lengths(p)
        want = pl.truth.position(lt, seed=p + rng.uniform(-4, 4, 2))
        got = pl._solve(float(lt[0]), float(lt[1]), tuple(p + rng.uniform(-4, 4, 2)))
        assert got == pytest.approx(tuple(want), abs=1e-6) and got == pytest.approx(tuple(p), abs=1e-6)


def test_make_sim_link_returns_unopened_link():
    link = make_sim_link(CFG, realtime=False)
    assert isinstance(link, FluidNCEmulator) and not link.is_open and link.name == "sim"
    link.open()
    assert link.is_open
    link.close()


def test_realtime_character_counting_stream():
    emu = FluidNCEmulator(CFG, speed=100.0)
    emu.open()
    emu.read(0.2)
    pts = [(i * 3.0, (i % 7) * 2.0) for i in range(1, 60)]
    lines = g1_lines(pts, 3000)
    sent, outstanding, i, oks, buf = [], 0, 0, 0, ""
    deadline = time.monotonic() + 20
    while oks < len(lines) and time.monotonic() < deadline:
        while i < len(lines) and outstanding + len(lines[i]) + 1 <= 127:
            emu.write((lines[i] + "\n").encode())
            sent.append(len(lines[i]) + 1)
            outstanding += sent[-1]
            i += 1
        buf += emu.read(0.005).decode("latin-1")
        while "\n" in buf:
            ln, buf = buf.split("\n", 1)
            if ln.strip() == "ok":
                outstanding -= sent.pop(0)
                oks += 1
            assert "ERR" not in ln and not ln.startswith("error")
    assert oks == len(lines) and emu.rx_overflows == 0
    assert emu.wait_idle(10.0)
    assert emu.firmware_position() == pytest.approx(pts[-1], abs=0.02)
    emu.close()
