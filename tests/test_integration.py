"""End-to-end walks through the milestones of the brief.

Each test here is a whole workflow rather than a unit: photo in, ink on the
simulated board, with the numbers checked along the way.
"""

from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest

from wbbot.core.gcode import GCodeWriter
from wbbot.core.geometry import Drawing, Layer, Polyline, douglas_peucker
from wbbot.core.safearea import SafeArea
from wbbot.core.toolpath import Toolpath
from wbbot.driver.machine import MachineState


async def drain_job(machine, timeout: float = 60.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while machine.state in (MachineState.RUNNING, MachineState.PAUSED):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"job did not finish; state {machine.state}")
        await asyncio.sleep(0.05)


# ----------------------------------------------------------------------
# milestone 2 + 3: simulator draws what it was told, in the right colours
# ----------------------------------------------------------------------

async def test_milestone_3_multicolour_path_runs_in_the_simulator(
        machine, simulator, toolpath):
    await machine.run_toolpath(toolpath, title="milestone 3")
    await drain_job(machine)

    assert machine.state is MachineState.IDLE
    assert machine.progress.percent == pytest.approx(100.0)
    assert not simulator.pen_down

    polylines = simulator.drawn_polylines()
    assert len(polylines) == 3
    corners = [len(douglas_peucker(np.array(p), 0.5)) for p in polylines]
    assert corners == [5, 4, 2], "square, triangle and line were not drawn"

    order: list[int] = []
    for segment in simulator.drawn:
        if not order or order[-1] != segment.color_index:
            order.append(segment.color_index)
    assert order == [0, 1, 2], "colours were not drawn in contiguous blocks"


# ----------------------------------------------------------------------
# milestone 7 + 8: photo -> contours -> three colours -> ink
# ----------------------------------------------------------------------

async def test_milestone_8_photo_to_three_colour_drawing(
        machine, simulator, cfg, reference_image):
    pytest.importorskip("cv2")
    from wbbot.imaging.pipeline import PipelineParams, run_pipeline

    params = PipelineParams(trace_mode="contours", color_mode="palette")
    params.optimize_time_budget_s = 1.0
    result = run_pipeline(reference_image, cfg, params,
                          safe_area=machine.safe_area)

    assert len(result.toolpath.colors_used) == 3
    assert result.stats.n_color_changes == 2, "more than two revolver changes"
    assert np.all(machine.safe_area.contains_many(result.toolpath.all_points()))

    await machine.run_toolpath(result.toolpath, title="photo")
    await drain_job(machine, timeout=180.0)

    assert machine.state is MachineState.IDLE
    assert not simulator.pen_down
    assert sorted({s.color_index for s in simulator.drawn}) == [0, 1, 2]

    # The ink must land inside the fitted bounds, not somewhere else entirely.
    x0, y0, x1, y1 = result.toolpath.bounds
    drawn = simulator.drawn_bounds()
    assert drawn is not None
    assert drawn[0] >= x0 - 2 and drawn[1] >= y0 - 2
    assert drawn[2] <= x1 + 2 and drawn[3] <= y1 + 2


# ----------------------------------------------------------------------
# milestone 6: calibrate, apply, and confirm the machine improves
# ----------------------------------------------------------------------

def test_milestone_6_calibration_improves_a_misbuilt_machine(cfg):
    """A machine built slightly wrong is measured and corrected."""
    from wbbot.calib.patterns import build_pattern
    from wbbot.calib.solve import simulate_measurements, solve_calibration

    truth = dict(ax_l=-105.0, ay_l=1344.0, ax_r=1606.0, ay_r=1353.0,
                 k_left=1.005, k_right=0.996, c_left=4.0, c_right=-3.0,
                 dx=2.0, dy=-3.0)
    pattern = build_pattern(cfg, SafeArea(cfg), n_x=4, n_y=3)
    targets = [t.nominal for t in pattern.targets]
    measurements = simulate_measurements(cfg, targets, true_params=truth,
                                         model="tangent", noise_mm=0.4, seed=42)

    before = float(np.mean([m.error for m in measurements]))
    assert before > 2.0, "the synthetic machine was not actually misbuilt"

    result = solve_calibration(measurements, cfg, model="auto")
    assert result.model == "tangent", "the wrong belt model was chosen"
    assert result.max_error_mm < 2.0, result.report()
    assert result.mean_error_mm < before / 2

    # The fitted config must still describe a usable machine.
    calibrated = cfg.replace(**{k: v for k, v in
                                result.to_calibration_yaml(cfg).items()
                                if k != "report"})
    safe = SafeArea(calibrated)
    assert safe.polygon.area > 0.8 * cfg.board.width_mm * cfg.board.height_mm


# ----------------------------------------------------------------------
# milestone 10: an AI client plots a function and writes the answer
# ----------------------------------------------------------------------

async def test_milestone_10_plot_a_function_and_label_it(machine, simulator):
    from wbbot.mcp.service import PlotterService

    service = PlotterService(machine, default_wait=True, default_timeout_s=180.0)

    plotted = await service.plot_function("sin(x)/x", x_min=-12, x_max=12,
                                          box=[120, 380, 1100, 480])
    assert plotted["ok"], plotted.get("error")
    assert plotted["completed"]
    assert plotted["plot"]["axes"]["x_ticks"] > 3

    labelled = await service.write_text("y = sin(x)/x", 200, 200, height_mm=50)
    assert labelled["ok"], labelled.get("error")
    assert labelled["completed"]

    assert simulator.drawn
    assert not simulator.pen_down


async def test_poles_become_gaps_on_the_board(machine, simulator):
    """tan(x) must not leave a vertical stripe across the whiteboard."""
    from wbbot.mcp.service import PlotterService

    service = PlotterService(machine, default_wait=True, default_timeout_s=180.0)
    result = await service.plot_function(
        "tan(x)", x_min=-6.3, x_max=6.3, y_min=-4, y_max=4,
        box=[200, 300, 1000, 400], axes=False)
    assert result["ok"], result.get("error")
    assert result["plot"]["segments"] >= 4

    # No single pen-down segment may span most of the plot height.
    for segment in simulator.drawn:
        rise = abs(segment.end[1] - segment.start[1])
        assert rise < 400 * 0.9, "a pole was drawn through"


# ----------------------------------------------------------------------
# milestone 9: live drawing puts real ink on the board
# ----------------------------------------------------------------------

async def test_milestone_9_live_session_draws_and_reports_latency(
        machine, cfg, simulator):
    from wbbot.live.session import LiveConfig, LiveSession

    session = LiveSession(machine, cfg,
                          LiveConfig(update_hz=50.0, max_pending_blocks=3),
                          machine.safe_area)
    await session.start()
    try:
        session.set_cursor(400, 400)
        await asyncio.sleep(0.2)
        await session.set_drawing(True)
        for i in range(25):
            session.set_cursor(400 + i * 20, 400 + math.sin(i / 3) * 80)
            await asyncio.sleep(0.03)
        await session.set_drawing(False)
        await asyncio.sleep(0.4)

        assert session.strokes, "the live session recorded no stroke"
        assert session.strokes[0].length > 100
        assert session.measured_latency_ms < 150.0
        assert simulator.drawn, "nothing reached the board"
    finally:
        await session.stop()
    assert not simulator.pen_down


# ----------------------------------------------------------------------
# safety, end to end
# ----------------------------------------------------------------------

async def test_a_job_that_leaves_the_board_is_refused_before_anything_moves(
        machine, simulator):
    from wbbot.core.safearea import SafeAreaViolation

    bad = Toolpath.from_polylines(
        [Polyline.from_points([[200, 200], [4000, 200]])])
    with pytest.raises(SafeAreaViolation):
        await machine.run_toolpath(bad)
    assert simulator.drawn == []
    assert machine.state is MachineState.IDLE


async def test_alarm_mid_job_stops_the_machine_and_explains_itself(
        machine, simulator, toolpath):
    await machine.run_toolpath(toolpath)
    await asyncio.sleep(0.05)
    simulator.raise_alarm(1)

    for _ in range(200):
        await asyncio.sleep(0.02)
        if machine.state is MachineState.ALARM:
            break
    assert machine.state is MachineState.ALARM
    assert "hard limit" in (machine.last_error or "").lower()

    from wbbot.driver.fluidnc import FluidNCError
    with pytest.raises(FluidNCError):
        await machine.run_toolpath(toolpath)

    await machine.clear_alarm()
    assert machine.state is MachineState.IDLE
    assert machine.last_error is None


async def test_resume_after_cancel_continues_in_the_right_colour(
        machine, cfg, simulator):
    """A long three-colour job is cancelled part way and picked up again."""
    strokes = []
    for colour in range(3):
        for i in range(6):
            y = 200 + colour * 250
            strokes.append(Polyline.from_points(
                [[200 + i * 180, y], [340 + i * 180, y]]))
    drawing = Drawing([Layer(c, strokes[c * 6:(c + 1) * 6]) for c in range(3)])
    toolpath = Toolpath.from_drawing(drawing)

    await machine.run_toolpath(toolpath, title="long job")
    await asyncio.sleep(0.6)
    await machine.cancel()
    assert machine.state is MachineState.IDLE
    assert machine.last_error is None

    stroke = machine.snapshot()["resume_from_stroke"]
    assert stroke > 0, "the job was cancelled before it started"

    program = machine.prepare(toolpath, resume_from_stroke=stroke)
    expected_colour = toolpath.strokes[stroke].color_index
    assert program.stats.colors_used[0] == expected_colour
    # The resumed program must re-select the colour before drawing.
    revolver_axis = cfg.servo.revolver.axis
    first_move = next(i for i, l in enumerate(program.lines)
                      if l.startswith("G1 X"))
    first_colour = next(i for i, l in enumerate(program.lines)
                        if l.startswith(f"G1 {revolver_axis}"))
    assert first_colour < first_move

    await machine.resume_cancelled_job()
    await drain_job(machine, timeout=120.0)
    assert machine.state is MachineState.IDLE


async def test_gcode_never_contains_a_point_outside_the_safe_area(
        machine, cfg, reference_image):
    """The last line of defence, exercised on real traced output."""
    pytest.importorskip("cv2")
    from wbbot.imaging.pipeline import PipelineParams, run_pipeline

    result = run_pipeline(reference_image, cfg,
                          PipelineParams(trace_mode="contours"),
                          safe_area=machine.safe_area)
    program = GCodeWriter(cfg, machine.safe_area).render_toolpath(result.toolpath)

    targets = [m.target for m in program.meta if m.target is not None]
    assert targets
    assert np.all(machine.safe_area.contains_many(np.array(targets)))
    assert program.text.isascii()
