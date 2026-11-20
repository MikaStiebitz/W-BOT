"""Driver + simulator: streaming, flow control, and every failure path.

The point of these tests is the *unhappy* cases. A plotter that streams a
clean job correctly but keeps going after the cable is pulled is not safe.
"""

from __future__ import annotations

import asyncio

import numpy as np
import pytest

from wbbot.core.gcode import GCodeWriter
from wbbot.core.geometry import Drawing, Layer, Polyline
from wbbot.core.toolpath import Toolpath
from wbbot.driver.fluidnc import (ConnectionLost, FluidNCDriver, FluidNCError,
                                  GrblAlarm, GrblError, MachineStatus)
from wbbot.driver.machine import Machine, MachineState
from wbbot.driver.simulator import FluidNCSimulator
from wbbot.driver.transport import SimulatorTransport


@pytest.fixture
async def driver(cfg, simulator):
    d = FluidNCDriver(cfg, SimulatorTransport(simulator))
    await d.connect()
    try:
        yield d
    finally:
        try:
            await d.disconnect()
        except Exception:
            pass


def program_lines(cfg, toolpath) -> list[str]:
    prog = GCodeWriter(cfg).render_toolpath(toolpath)
    return [text for _, text in prog.executable_lines()]


# ----------------------------------------------------------------------
# status parsing
# ----------------------------------------------------------------------

def test_parse_full_status():
    s = MachineStatus.parse(
        "<Run|MPos:12.500,-3.250,20.000,90.000|FS:1500,0|Bf:14,110|WCO:0.000,0.000,0.000>")
    assert s is not None
    assert s.state == "Run" and s.is_moving and not s.is_idle
    assert s.x == 12.5 and s.y == -3.25 and s.z == 20.0 and s.a == 90.0
    assert s.feed == 1500.0
    assert s.planner_free == 14 and s.rx_free == 110


def test_parse_wpos_status_applies_the_work_offset():
    s = MachineStatus.parse("<Idle|WPos:10.000,20.000,0.000|WCO:5.000,5.000,0.000>")
    assert s is not None
    assert (s.x, s.y) == (15.0, 25.0)


def test_parse_minimal_status():
    s = MachineStatus.parse("<Idle|MPos:0.000,0.000,0.000>")
    assert s is not None and s.is_idle and s.a == 0.0


def test_parse_rejects_garbage():
    assert MachineStatus.parse("not a status") is None
    assert MachineStatus.parse("ok") is None


def test_alarm_state_flags():
    s = MachineStatus.parse("<Alarm|MPos:0.000,0.000,0.000>")
    assert s is not None and s.is_alarm and not s.is_idle


# ----------------------------------------------------------------------
# basic protocol
# ----------------------------------------------------------------------

async def test_connect_gets_a_status(driver):
    assert driver.connected
    assert driver.status.timestamp > 0
    assert driver.status.is_idle


async def test_send_line_waits_for_ok(driver):
    await driver.send_line("G1 X400 Y400 F3000")
    assert driver.pending_lines == 0


async def test_error_response_raises_with_plain_language(driver, simulator):
    simulator.error_on_line_containing = "G99"
    simulator.error_code_to_raise = 20
    with pytest.raises(GrblError) as exc:
        await driver.send_line("G99 X1")
    assert exc.value.code == 20
    assert "Unsupported or invalid G-code" in str(exc.value)
    assert "G99 X1" in str(exc.value)


async def test_line_longer_than_the_rx_buffer_is_rejected(driver):
    with pytest.raises(FluidNCError, match="longer than"):
        await driver.send_line("G1 " + "X1 " * 100)


async def test_realtime_status_does_not_consume_a_buffer_slot(driver):
    before = driver.pending_bytes
    await driver.send_realtime(b"?")
    assert driver.pending_bytes == before


# ----------------------------------------------------------------------
# character counting
# ----------------------------------------------------------------------

async def test_character_counting_never_overruns_the_rx_buffer(cfg, simulator):
    """The invariant the whole streamer exists to maintain."""
    driver = FluidNCDriver(cfg, SimulatorTransport(simulator))
    await driver.connect()
    peak = 0
    lines = [f"G1 X{300 + i % 500} Y{300 + (i * 7) % 400} F1500" for i in range(400)]
    original = driver.send_line_nowait

    async def spy(line):
        nonlocal peak
        fut = await original(line)
        peak = max(peak, driver.pending_bytes)
        assert driver.pending_bytes <= driver.rx_buffer
        return fut

    driver.send_line_nowait = spy       # type: ignore[method-assign]
    await driver.stream(lines)
    await driver.drain()
    assert peak > cfg.connection.serial.rx_buffer_bytes * 0.5, \
        "the streamer is not actually filling the buffer"
    await driver.disconnect()


async def test_streaming_keeps_the_planner_busy(cfg, simulator):
    """Send-and-wait would leave the planner empty; character counting must not."""
    driver = FluidNCDriver(cfg, SimulatorTransport(simulator))
    await driver.connect()
    depths = []
    lines = [f"G1 X{300 + i % 400} Y{300 + (i * 13) % 300} F1500" for i in range(200)]

    async def watcher():
        for _ in range(300):
            await asyncio.sleep(0.002)
            depths.append(len(simulator._blocks))

    task = asyncio.create_task(watcher())
    await driver.stream(lines)
    await driver.drain()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert max(depths) >= 2, f"planner never held more than {max(depths)} block(s)"
    await driver.disconnect()


async def test_stream_reports_progress_monotonically(driver, cfg, toolpath):
    seen: list[int] = []
    await driver.stream(program_lines(cfg, toolpath),
                        on_progress=lambda d, t: seen.append(d))
    assert seen == sorted(seen)
    assert seen[-1] == len(program_lines(cfg, toolpath))


# ----------------------------------------------------------------------
# drawing correctness
# ----------------------------------------------------------------------

async def test_square_is_drawn_exactly(driver, cfg, simulator, square):
    tp = Toolpath.from_drawing(Drawing([Layer(0, [square])]))
    await driver.stream(program_lines(cfg, tp))
    await driver.drain()
    await driver.wait_until_idle(timeout=60)

    polys = simulator.drawn_polylines()
    assert len(polys) == 1
    from wbbot.core.geometry import douglas_peucker
    corners = douglas_peucker(np.array(polys[0]), 0.5)
    assert len(corners) == 5                      # 4 corners + closing point
    expected = [(300, 300), (700, 300), (700, 700), (300, 700), (300, 300)]
    assert np.allclose(corners, expected, atol=0.5)
    assert not simulator.pen_down


async def test_three_colour_job_uses_all_three_and_two_changes(
        driver, cfg, simulator, toolpath):
    await driver.stream(program_lines(cfg, toolpath))
    await driver.drain()
    await driver.wait_until_idle(timeout=120)
    used = sorted({s.color_index for s in simulator.drawn})
    assert used == [0, 1, 2]
    # Colour must change exactly twice, in order.
    order = []
    for seg in simulator.drawn:
        if not order or order[-1] != seg.color_index:
            order.append(seg.color_index)
    assert order == [0, 1, 2]


async def test_nothing_is_drawn_while_the_pen_is_up(driver, cfg, simulator, toolpath):
    await driver.stream(program_lines(cfg, toolpath))
    await driver.drain()
    await driver.wait_until_idle(timeout=120)
    # Every recorded segment must lie on one of the three shapes' bounding
    # boxes; a travel drawn by mistake would escape them.
    for seg in simulator.drawn:
        x, y = seg.end
        assert 190 <= x <= 1310 and 290 <= y <= 860, f"stray ink at {seg.end}"


@pytest.mark.parametrize("planner_size,time_scale", [(16, 300.0), (4, 300.0),
                                                     (2, 150.0), (16, 1000.0)])
async def test_execution_order_holds_under_buffer_pressure(
        cfg, toolpath, planner_size, time_scale):
    """A small planner forces the deferred path; order must still hold."""
    sim = FluidNCSimulator(cfg, time_scale=time_scale, tick_hz=400,
                           planner_size=planner_size)
    driver = FluidNCDriver(cfg, SimulatorTransport(sim))
    await driver.connect()

    chain_ok = True
    original = sim._enqueue
    prev: list = [None]

    def spy(block):
        original(block)
        if prev[0] is not None and not np.allclose(prev[0], block.start, atol=1e-6):
            nonlocal chain_ok
            chain_ok = False
        prev[0] = block.target.copy()

    sim._enqueue = spy      # type: ignore[method-assign]
    await driver.stream(program_lines(cfg, toolpath))
    await driver.drain()
    await driver.wait_until_idle(timeout=180)
    assert chain_ok, "blocks executed out of order"
    assert not sim.pen_down
    assert len(sim.drawn_polylines()) == 3
    await driver.disconnect()


# ----------------------------------------------------------------------
# failure paths
# ----------------------------------------------------------------------

async def test_alarm_mid_job_surfaces_and_stops(cfg, simulator, toolpath):
    simulator.alarm_after_lines = 8
    simulator.alarm_code_to_raise = 2
    driver = FluidNCDriver(cfg, SimulatorTransport(simulator))
    await driver.connect()
    with pytest.raises(GrblAlarm) as exc:
        await driver.stream(program_lines(cfg, toolpath))
    assert exc.value.code == 2
    assert "soft limit" in str(exc.value)
    assert driver.alarm is not None
    await driver.disconnect()


async def test_connection_loss_mid_job_raises_not_hangs(cfg, simulator, toolpath):
    simulator.fail_after_lines = 6
    driver = FluidNCDriver(cfg, SimulatorTransport(simulator))
    await driver.connect()
    with pytest.raises(ConnectionLost):
        await asyncio.wait_for(
            driver.stream(program_lines(cfg, toolpath)), timeout=10.0)
    await driver.disconnect()


async def test_planner_full_rejection_is_reported(cfg, simulator, toolpath):
    simulator.reject_when_planner_full = True
    simulator.planner_size = 2
    driver = FluidNCDriver(cfg, SimulatorTransport(simulator))
    await driver.connect()
    with pytest.raises(GrblError) as exc:
        await driver.stream(program_lines(cfg, toolpath))
    assert exc.value.code == 8
    await driver.disconnect()


async def test_unlock_clears_an_alarm(driver, simulator):
    simulator.raise_alarm(1)
    await asyncio.sleep(0.1)
    assert driver.alarm is not None
    await driver.unlock()
    assert driver.alarm is None
    await driver.send_line("G1 X500 Y500 F3000")


async def test_lines_are_refused_while_alarmed(driver, simulator):
    simulator.raise_alarm(1)
    await asyncio.sleep(0.1)
    with pytest.raises(GrblAlarm):
        await driver.send_line("G1 X500 Y500 F3000")


async def test_soft_reset_flushes_the_queue(driver, simulator):
    for i in range(30):
        await driver.send_line_nowait(f"G1 X{400 + i} Y400 F1500")
    await driver.soft_reset()
    await asyncio.sleep(0.1)
    assert simulator.queue_depth == 0


async def test_jog_cancel_only_kills_jogs(driver, simulator):
    await driver.send_line_nowait("G1 X400 Y400 F600")
    await driver.jog_to(1200, 800, 3000)
    await asyncio.sleep(0.01)
    await driver.jog_cancel()
    await asyncio.sleep(0.05)
    assert not any(b.is_jog for b in simulator._blocks)


async def test_watchdog_trips_when_status_stops(cfg):
    """A silent link must stop the machine, not stall quietly."""
    fast = cfg.replace(connection={"watchdog_timeout_s": 0.4})
    sim = FluidNCSimulator(fast, time_scale=50.0, tick_hz=200)
    driver = FluidNCDriver(fast, SimulatorTransport(sim))
    reasons: list[str] = []
    driver.on_disconnect = reasons.append
    await driver.connect()
    # Still accepting bytes, just never answering — a wedged controller. The
    # transport cannot notice this; only the host-side watchdog can.
    sim.mute = True
    await asyncio.sleep(1.2)
    assert reasons and "watchdog" in reasons[0].lower()
    assert not driver.connected
    await driver.disconnect()


# ----------------------------------------------------------------------
# machine state machine
# ----------------------------------------------------------------------

async def test_machine_runs_a_job_to_completion(machine, toolpath, simulator):
    events: list[str] = []
    machine.add_listener(lambda e: events.append(e.kind))
    await machine.run_toolpath(toolpath, title="test")
    assert machine.state is MachineState.RUNNING
    for _ in range(600):
        await asyncio.sleep(0.02)
        if machine.state is MachineState.IDLE:
            break
    assert machine.state is MachineState.IDLE
    assert "job_finished" in events
    assert machine.progress.percent == pytest.approx(100.0)
    assert len(simulator.drawn_polylines()) == 3


async def test_machine_refuses_unsafe_geometry(machine):
    bad = Toolpath.from_polylines([Polyline.from_points([[100, 100], [3000, 100]])])
    from wbbot.core.safearea import SafeAreaViolation
    with pytest.raises(SafeAreaViolation):
        await machine.run_toolpath(bad)
    assert machine.state is MachineState.IDLE


async def test_pause_resume_round_trip(machine, toolpath):
    await machine.run_toolpath(toolpath)
    await asyncio.sleep(0.05)
    await machine.pause()
    assert machine.state is MachineState.PAUSED
    done_at_pause = machine.progress.lines_done
    await asyncio.sleep(0.1)
    await machine.resume()
    assert machine.state is MachineState.RUNNING
    for _ in range(600):
        await asyncio.sleep(0.02)
        if machine.state is MachineState.IDLE:
            break
    assert machine.state is MachineState.IDLE
    assert machine.progress.lines_done > done_at_pause


async def test_cancel_remembers_the_stroke_and_resumes_in_colour(machine, toolpath):
    await machine.run_toolpath(toolpath)
    await asyncio.sleep(0.08)
    await machine.cancel()
    assert machine.state is MachineState.IDLE
    stroke = machine.snapshot()["resume_from_stroke"]
    assert stroke >= 0
    if stroke > 0:
        prog = machine.prepare(toolpath, resume_from_stroke=stroke)
        assert prog.stats.colors_used[0] == toolpath.strokes[stroke].color_index


async def test_cancelling_is_not_reported_as_an_error(machine, toolpath):
    """Flushing the planner makes the streamer raise; that is expected."""
    events: list[tuple[str, dict]] = []
    machine.add_listener(lambda e: events.append((e.kind, e.payload)))
    await machine.run_toolpath(toolpath)
    await asyncio.sleep(0.08)
    await machine.cancel()
    await asyncio.sleep(0.3)
    assert machine.last_error is None, (
        f"cancel surfaced an internal error: {machine.last_error}")
    assert machine.snapshot()["last_error"] is None
    assert not [e for e in events if e[0] == "job_failed"]
    assert [e for e in events if e[0] == "job_cancelled"]


async def test_cancel_leaves_the_pen_up(machine, toolpath, simulator):
    await machine.run_toolpath(toolpath)
    await asyncio.sleep(0.12)
    await machine.cancel()
    await asyncio.sleep(0.3)
    assert not simulator.pen_down, "the pen was left on the board after cancel"


async def test_emergency_stop_from_running(machine, toolpath):
    await machine.run_toolpath(toolpath)
    await asyncio.sleep(0.05)
    await machine.emergency_stop()
    assert machine.state is MachineState.ESTOP
    with pytest.raises(FluidNCError, match="estop"):
        await machine.run_toolpath(toolpath)
    await machine.clear_alarm()
    assert machine.state is MachineState.IDLE


async def test_alarm_during_a_job_moves_the_machine_to_alarm(machine, toolpath,
                                                             simulator):
    await machine.run_toolpath(toolpath)
    await asyncio.sleep(0.03)
    simulator.raise_alarm(1)
    for _ in range(200):
        await asyncio.sleep(0.02)
        if machine.state is MachineState.ALARM:
            break
    assert machine.state is MachineState.ALARM
    assert "hard limit" in (machine.last_error or "").lower()


async def test_connection_loss_becomes_an_estop(machine, toolpath, simulator):
    await machine.run_toolpath(toolpath)
    await asyncio.sleep(0.05)
    simulator.drop_link()
    for _ in range(200):
        await asyncio.sleep(0.02)
        if machine.state is MachineState.ESTOP:
            break
    assert machine.state is MachineState.ESTOP


async def test_move_to_validates_the_target(machine):
    from wbbot.core.safearea import SafeAreaViolation
    with pytest.raises(SafeAreaViolation):
        await machine.move_to(5000, 500)
    await machine.move_to(600, 600)


async def test_pen_down_outside_the_safe_area_is_refused(machine, simulator):
    from wbbot.core.safearea import SafeAreaViolation
    simulator.position[0] = -500.0
    simulator.position[1] = 500.0
    await asyncio.sleep(0.15)
    with pytest.raises(SafeAreaViolation):
        await machine.set_pen(True)


async def test_snapshot_shape(machine):
    snap = machine.snapshot()
    for key in ("state", "connected", "position", "pen_down", "color",
                "progress", "transport"):
        assert key in snap
