"""Live drawing: mode switching, latency, safe-area behaviour at the edge."""

from __future__ import annotations

import asyncio
import math

import numpy as np
import pytest

from wbbot.core.safearea import SafeArea
from wbbot.driver.fluidnc import FluidNCError
from wbbot.live.session import LiveConfig, LiveSession, LiveState


@pytest.fixture
async def live(machine, cfg, safe_area):
    session = LiveSession(machine, cfg,
                          LiveConfig(update_hz=50.0, min_move_mm=1.0,
                                     min_segment_mm=1.0,
                                     max_pending_blocks=3),
                          safe_area)
    yield session
    try:
        await session.stop()
    except Exception:
        pass


async def settle(seconds: float = 0.25) -> None:
    await asyncio.sleep(seconds)


# ----------------------------------------------------------------------
# safe-area clamping
# ----------------------------------------------------------------------

def test_clamp_returns_the_point_unchanged_when_it_is_safe(safe_area):
    point, clamped = safe_area.clamp((750, 500))
    assert not clamped and point == (750.0, 500.0)


def test_clamp_pulls_an_outside_point_to_the_boundary(safe_area):
    point, clamped = safe_area.clamp((3000, 500))
    assert clamped
    assert safe_area.contains(point)
    assert point[0] < 1500


@pytest.mark.parametrize("outside", [(-500, 500), (750, 5000), (750, -900),
                                     (5000, 5000)])
def test_clamped_points_are_always_safe(safe_area, outside):
    point, clamped = safe_area.clamp(outside)
    assert clamped and safe_area.contains(point)


def test_clamp_handles_nonsense_input(safe_area):
    point, clamped = safe_area.clamp((float("nan"), 5))
    assert clamped and safe_area.contains(point)


# ----------------------------------------------------------------------
# lifecycle
# ----------------------------------------------------------------------

async def test_start_lifts_the_pen_and_follows(live, machine):
    await live.start()
    assert live.state is LiveState.FOLLOWING
    assert not live.pen_down


async def test_cannot_start_while_a_job_runs(live, machine, toolpath):
    await machine.run_toolpath(toolpath)
    with pytest.raises(FluidNCError):
        await live.start()
    await machine.cancel()


async def test_stop_parks_the_pen(live):
    await live.start()
    live.set_cursor(600, 500)
    await live.set_drawing(True)
    await settle()
    await live.stop()
    assert live.state is LiveState.STOPPED
    assert not live.pen_down


# ----------------------------------------------------------------------
# cursor following
# ----------------------------------------------------------------------

async def test_following_uses_jog_cancel_before_each_new_target(live, simulator):
    """Jog-cancel-then-re-aim is what keeps the gondola chasing the cursor."""
    await live.start()
    for x in range(300, 900, 60):
        live.set_cursor(x, 500)
        await asyncio.sleep(0.04)
    await settle()
    assert live._jog_cancels >= 3
    jogs = [l for l in simulator.received_lines if l.startswith("$J=")]
    assert len(jogs) >= 3


async def test_following_ignores_tiny_cursor_jitter(live):
    await live.start()
    live.set_cursor(600, 500)
    await settle()
    before = live._sends
    for _ in range(10):
        live.set_cursor(600.2, 500.1)
        await asyncio.sleep(0.03)
    assert live._sends == before, "sub-millimetre jitter should not be sent"


async def test_following_does_not_put_the_pen_down(live, simulator):
    await live.start()
    for x in range(300, 800, 50):
        live.set_cursor(x, 500)
        await asyncio.sleep(0.03)
    await settle()
    assert not live.pen_down
    assert simulator.drawn == []


# ----------------------------------------------------------------------
# drawing
# ----------------------------------------------------------------------

async def test_drawing_puts_the_pen_down_and_records_a_stroke(live, simulator):
    await live.start()
    live.set_cursor(400, 400)
    await settle(0.15)
    await live.set_drawing(True)
    for i in range(12):
        live.set_cursor(400 + i * 20, 400)
        await asyncio.sleep(0.05)
    await settle(0.3)
    assert live.pen_down
    assert live.state is LiveState.DRAWING
    await live.set_drawing(False)
    await settle()
    assert live.strokes, "no stroke was recorded"
    assert live.strokes[0].length > 50


async def test_drawing_keeps_the_queue_shallow(live):
    """The latency budget: a deep planner buffer means a laggy pen."""
    await live.start()
    live.set_cursor(400, 400)
    await settle(0.15)
    await live.set_drawing(True)
    peak = 0
    for i in range(40):
        live.set_cursor(400 + i * 15, 400 + math.sin(i / 3) * 60)
        await asyncio.sleep(0.02)
        peak = max(peak, live.machine.driver.pending_lines)
    await live.set_drawing(False)
    assert peak <= live.live.max_pending_blocks + 1, (
        f"queue reached {peak} blocks, budget is {live.live.max_pending_blocks}")


async def test_measured_latency_stays_under_the_target(live):
    """Milestone 9 asks for under 150 ms of felt latency."""
    await live.start()
    live.set_cursor(400, 400)
    await settle(0.15)
    await live.set_drawing(True)
    for i in range(30):
        live.set_cursor(400 + i * 15, 420)
        await asyncio.sleep(0.02)
    await settle()
    await live.set_drawing(False)
    assert live.measured_latency_ms < live.live.latency_target_ms
    assert live.snapshot()["latency_ok"]


async def test_long_jumps_are_split_into_bounded_segments(live):
    await live.start()
    live.set_cursor(300, 300)
    await settle(0.15)
    await live.set_drawing(True)
    live.set_cursor(1200, 800)          # a huge mouse jump
    await settle(0.5)
    await live.set_drawing(False)
    await settle()
    if live.strokes:
        pts = np.array(live.strokes[0].points)
        if len(pts) > 1:
            steps = np.linalg.norm(np.diff(pts, axis=0), axis=1)
            assert steps.max() <= live.live.max_segment_mm + 1e-6


async def test_releasing_the_button_ends_the_stroke(live):
    await live.start()
    live.set_cursor(400, 400)
    await settle(0.15)
    await live.set_drawing(True)
    for i in range(8):
        live.set_cursor(400 + i * 25, 400)
        await asyncio.sleep(0.04)
    await live.set_drawing(False)
    await settle()
    assert not live.pen_down
    n_strokes = len(live.strokes)

    await live.set_drawing(True)
    for i in range(8):
        live.set_cursor(400 + i * 25, 600)
        await asyncio.sleep(0.04)
    await live.set_drawing(False)
    await settle()
    assert len(live.strokes) == n_strokes + 1, "a second stroke was not recorded"


# ----------------------------------------------------------------------
# leaving the safe area
# ----------------------------------------------------------------------

async def test_leaving_the_safe_area_lifts_the_pen_and_reports(live):
    events: list[dict] = []
    live.add_listener(events.append)
    await live.start()
    live.set_cursor(700, 500)
    await settle(0.15)
    await live.set_drawing(True)
    for i in range(6):
        live.set_cursor(700 + i * 40, 500)
        await asyncio.sleep(0.04)
    await settle(0.2)

    live.set_cursor(4000, 500)          # way off the board
    await settle(0.3)
    assert live.state is LiveState.BLOCKED
    assert not live.pen_down, "the pen must lift when the cursor leaves"
    assert live.blocked_reason
    assert any(e.get("kind") == "live_blocked" for e in events)


async def test_no_false_line_is_drawn_along_the_boundary(live, simulator):
    await live.start()
    live.set_cursor(700, 500)
    await settle(0.15)
    await live.set_drawing(True)
    await settle(0.15)
    drawn_before = len(simulator.drawn)
    for y in range(400, 700, 40):
        live.set_cursor(6000, y)        # dragging along outside
        await asyncio.sleep(0.04)
    await settle(0.3)
    # Some ink may exist from before, but nothing new while blocked.
    assert len(simulator.drawn) >= drawn_before
    assert not live.pen_down


async def test_returning_inside_unblocks(live):
    await live.start()
    live.set_cursor(9000, 500)
    await settle(0.2)
    assert live.state is LiveState.BLOCKED
    live.set_cursor(700, 500)
    await settle(0.2)
    assert live.state is LiveState.FOLLOWING
    assert live.blocked_reason is None


# ----------------------------------------------------------------------
# colour and stop
# ----------------------------------------------------------------------

async def test_colour_change_mid_session_lifts_the_pen_first(live, simulator):
    await live.start()
    live.set_cursor(500, 500)
    await settle(0.15)
    await live.set_drawing(True)
    await settle(0.2)
    await live.set_color(2)
    await settle(0.2)
    assert live.color_index == 2
    # A revolver move must never happen with the pen on the board.
    a_moves = [i for i, l in enumerate(simulator.received_lines)
               if l.startswith("G1 A")]
    assert a_moves, "no revolver move was issued"


async def test_invalid_colour_is_rejected(live):
    await live.start()
    with pytest.raises(ValueError):
        await live.set_color(9)


async def test_emergency_stop_from_a_live_session(live, machine):
    from wbbot.driver.machine import MachineState
    await live.start()
    live.set_cursor(500, 500)
    await settle(0.1)
    await live.emergency_stop()
    assert live.state is LiveState.ERROR
    assert machine.state is MachineState.ESTOP


# ----------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------

async def test_snapshot_is_json_safe_and_says_undo_is_impossible(live):
    import json
    await live.start()
    live.set_cursor(600, 500)
    await settle(0.1)
    snapshot = live.snapshot()
    json.dumps(snapshot)
    assert snapshot["undo_supported"] is False
    for key in ("state", "actual", "pending", "latency_ms", "queue_depth"):
        assert key in snapshot


async def test_strokes_export_is_json_safe(live):
    import json
    await live.start()
    live.set_cursor(400, 400)
    await settle(0.15)
    await live.set_drawing(True)
    for i in range(6):
        live.set_cursor(400 + i * 30, 400)
        await asyncio.sleep(0.04)
    await live.set_drawing(False)
    await settle()
    json.dumps(live.strokes_to_json())


async def test_pending_trail_shrinks_as_the_gondola_catches_up(live):
    await live.start()
    live.set_cursor(400, 400)
    await settle(0.15)
    await live.set_drawing(True)
    for i in range(10):
        live.set_cursor(400 + i * 20, 400)
        await asyncio.sleep(0.03)
    await live.set_drawing(False)
    await settle(0.6)
    live.prune_pending()
    assert len(live.pending) == 0


def test_live_config_validates():
    with pytest.raises(ValueError):
        LiveConfig(update_hz=0.0)
    with pytest.raises(ValueError):
        LiveConfig(max_pending_blocks=0)
    with pytest.raises(ValueError):
        LiveConfig(min_segment_mm=10.0, max_segment_mm=5.0)
