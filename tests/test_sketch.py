"""Async sketch queue: draw ahead, the plotter catches up."""

from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest

from wbbot.driver.machine import MachineState
from wbbot.live.sketch import SketchSession, SketchState


@pytest.fixture
async def sketch(machine, cfg, safe_area):
    session = SketchSession(machine, cfg, safe_area)
    await session.start()
    try:
        yield session
    finally:
        try:
            await session.stop()
        except Exception:
            pass


async def wait_for_drain(session: SketchSession, expected_done: int,
                         timeout: float = 30.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.05)
        if session.done_count >= expected_done and session.queued_count == 0 \
                and session.state is not SketchState.DRAWING:
            return
    raise AssertionError(
        f"queue did not drain: done={session.done_count}/{expected_done}, "
        f"queued={session.queued_count}, state={session.state}")


def line(x0, y0, x1, y1, n=6):
    return [[x0 + (x1 - x0) * i / (n - 1), y0 + (y1 - y0) * i / (n - 1)]
            for i in range(n)]


# ----------------------------------------------------------------------
# lifecycle
# ----------------------------------------------------------------------

async def test_start_requires_an_idle_machine(machine, cfg, safe_area, toolpath):
    from wbbot.driver.fluidnc import FluidNCError
    await machine.run_toolpath(toolpath)
    session = SketchSession(machine, cfg, safe_area)
    with pytest.raises(FluidNCError):
        await session.start()
    await machine.cancel()


async def test_stop_lifts_the_pen(sketch, machine, simulator):
    await sketch.enqueue(line(300, 300, 600, 300), 0)
    await asyncio.sleep(0.1)
    await sketch.stop()
    await asyncio.sleep(0.2)
    assert sketch.state is SketchState.STOPPED
    assert not simulator.pen_down


# ----------------------------------------------------------------------
# queueing while drawing — the whole point
# ----------------------------------------------------------------------

async def test_strokes_can_queue_up_while_the_plotter_draws(sketch):
    """Draw ahead as fast as you like; the queue absorbs it."""
    for i in range(6):
        result = await sketch.enqueue(line(200, 200 + i * 80, 1200, 200 + i * 80), 0)
        assert result["ok"], result
    # All were accepted immediately, well before the plotter finished any.
    assert sketch.queued_count + (1 if sketch.current_id else 0) >= 5
    await wait_for_drain(sketch, expected_done=6)
    assert sketch.done_count == 6
    assert all(s.status == "done" for s in sketch.strokes)


async def test_strokes_are_drawn_in_fifo_order_with_their_colours(
        sketch, simulator):
    await sketch.enqueue(line(200, 300, 700, 300), 0)
    await sketch.enqueue(line(200, 450, 700, 450), 2)
    await sketch.enqueue(line(200, 600, 700, 600), 1)
    await wait_for_drain(sketch, expected_done=3)

    # Ink order on the (virtual) board must match queue order.
    order: list[int] = []
    for segment in simulator.drawn:
        if not order or order[-1] != segment.color_index:
            order.append(segment.color_index)
    assert order == [0, 2, 1]
    assert not simulator.pen_down


async def test_the_pen_travels_up_between_strokes(sketch, simulator):
    await sketch.enqueue(line(200, 300, 500, 300), 0)
    await sketch.enqueue(line(1000, 800, 1300, 800), 0)
    await wait_for_drain(sketch, expected_done=2)
    # Nothing may be inked along the long diagonal between the two strokes.
    for segment in simulator.drawn:
        y = segment.end[1]
        assert y < 350 or y > 750, f"stray ink at {segment.end} between strokes"


async def test_events_tell_the_story(sketch):
    events: list[str] = []
    sketch.add_listener(lambda e: events.append(e["kind"]))
    await sketch.enqueue(line(300, 300, 800, 300), 0)
    await wait_for_drain(sketch, expected_done=1)
    assert "sketch_queued" in events
    assert "sketch_stroke_started" in events
    assert "sketch_stroke_done" in events


# ----------------------------------------------------------------------
# validation
# ----------------------------------------------------------------------

async def test_a_stroke_outside_the_safe_area_is_refused(sketch, simulator):
    result = await sketch.enqueue(line(200, 500, 4000, 500), 0)
    assert not result["ok"]
    assert "drawable area" in result["error"]
    assert simulator.drawn == []


async def test_bad_input_is_refused_not_crashed(sketch):
    assert not (await sketch.enqueue([], 0))["ok"]
    assert not (await sketch.enqueue([[100, 100]], 0))["ok"]
    assert not (await sketch.enqueue([[float("nan"), 1], [2, 2]], 0))["ok"]
    assert not (await sketch.enqueue(line(300, 300, 400, 300), 9))["ok"]
    assert not (await sketch.enqueue("nonsense", 0))["ok"]


async def test_strokes_are_simplified_before_queueing(sketch):
    dense = [[300 + i * 0.5, 400.0] for i in range(1000)]
    result = await sketch.enqueue(dense, 0)
    assert result["ok"]
    assert result["n_points"] < 50, "the straight line was not simplified"


async def test_queue_limit_is_enforced(machine, cfg, safe_area):
    session = SketchSession(machine, cfg, safe_area)
    session.MAX_QUEUE = 5
    await session.start()
    try:
        # Occupy the worker with one long stroke, then fill the queue.
        # (The sleep lets the worker pick it up — enqueue() itself never
        # yields, so without it the first stroke still counts as queued.)
        await session.enqueue(line(100, 100, 1400, 900, n=40), 0)
        await asyncio.sleep(0.15)
        for i in range(5):
            result = await session.enqueue(line(200, 200 + i * 50, 400,
                                                200 + i * 50), 0)
            assert result["ok"]
        overflow = await session.enqueue(line(200, 800, 400, 800), 0)
        assert not overflow["ok"] and "full" in overflow["error"]
    finally:
        await session.stop()


# ----------------------------------------------------------------------
# clearing
# ----------------------------------------------------------------------

async def test_clear_drops_queued_but_not_the_current_stroke(sketch):
    await sketch.enqueue(line(100, 100, 1400, 900, n=60), 0)   # long, current
    await asyncio.sleep(0.15)
    for i in range(3):
        await sketch.enqueue(line(200, 300 + i * 100, 500, 300 + i * 100), 0)
    removed = sketch.clear()
    assert len(removed) == 3
    assert sketch.queued_count == 0
    await wait_for_drain(sketch, expected_done=1)
    assert sketch.done_count == 1, "the in-flight stroke should still finish"


# ----------------------------------------------------------------------
# reporting
# ----------------------------------------------------------------------

async def test_snapshot_is_json_safe_and_honest(sketch):
    await sketch.enqueue(line(300, 300, 700, 300), 1)
    snap = sketch.snapshot(include_points=True)
    json.dumps(snap)
    assert snap["undo_supported"] is False
    assert snap["strokes"][0]["color"] == 1
    assert snap["strokes"][0]["points"]


async def test_alarm_mid_stroke_stops_the_queue(sketch, simulator, machine):
    await sketch.enqueue(line(100, 100, 1400, 900, n=60), 0)
    for i in range(3):
        await sketch.enqueue(line(200, 300 + i * 100, 600, 300 + i * 100), 0)
    await asyncio.sleep(0.2)
    simulator.raise_alarm(1)
    for _ in range(100):
        await asyncio.sleep(0.05)
        if sketch.state is SketchState.ERROR:
            break
    assert sketch.state is SketchState.ERROR
    assert sketch.last_error and "hard limit" in sketch.last_error.lower()
    assert sketch.failed_count >= 1


# ----------------------------------------------------------------------
# API + websocket
# ----------------------------------------------------------------------

@pytest.fixture
def client(cfg, simulator, tmp_path, monkeypatch):
    monkeypatch.setenv("WBBOT_CONFIG_DIR", str(tmp_path))
    from fastapi.testclient import TestClient
    from wbbot.api.app import create_app
    from wbbot.driver.machine import Machine
    from wbbot.driver.transport import SimulatorTransport

    machine = Machine(cfg, transport=SimulatorTransport(simulator),
                      simulator=simulator)
    app = create_app(cfg, machine, serve_ui=False)
    with TestClient(app) as test_client:
        yield test_client


def test_sketch_rest_lifecycle(client):
    assert client.get("/api/sketch/status").json()["active"] is False
    started = client.post("/api/sketch/start").json()
    assert started["state"] == "ready"
    assert client.get("/api/sketch/status").json()["active"] is True
    assert client.post("/api/sketch/clear").status_code == 200
    stopped = client.post("/api/sketch/stop").json()
    assert stopped["state"] == "stopped"
    assert client.post("/api/sketch/stop").status_code == 400


def test_stroke_over_websocket_ends_up_as_ink(client, simulator):
    import time
    client.post("/api/sketch/start")
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()                        # hello
        ws.send_json({"kind": "stroke",
                      "payload": {"points": line(400, 400, 900, 600),
                                  "color": 1}})
        for _ in range(20):
            frame = ws.receive_json()
            if frame["kind"] == "sketch_ack":
                assert frame["payload"]["ok"], frame["payload"]
                break
        else:
            pytest.fail("no sketch_ack received")

    deadline = time.time() + 15
    while time.time() < deadline:
        status = client.get("/api/sketch/status").json()
        if status["done"] >= 1:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"stroke never finished: {status}")
    assert any(s.color_index == 1 for s in simulator.drawn)
    client.post("/api/sketch/stop")


def test_stroke_to_a_disabled_slot_is_refused(client):
    client.post("/api/sketch/start")
    client.put("/api/ams/slot", json={"index": 2, "enabled": False})
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"kind": "stroke",
                      "payload": {"points": line(400, 400, 600, 400),
                                  "color": 2}})
        for _ in range(20):
            frame = ws.receive_json()
            if frame["kind"] == "sketch_ack":
                assert not frame["payload"]["ok"]
                assert "disabled" in frame["payload"]["error"]
                break
        else:
            pytest.fail("no sketch_ack received")
    client.post("/api/sketch/stop")


def test_sketch_and_cursor_live_are_mutually_exclusive(client):
    client.post("/api/sketch/start")
    assert client.post("/api/live/start").status_code == 409
    client.post("/api/sketch/stop")
    assert client.post("/api/live/start").status_code == 200
    assert client.post("/api/sketch/start").status_code == 409
    client.post("/api/live/stop")
