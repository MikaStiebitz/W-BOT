"""REST + WebSocket API against the simulator."""

from __future__ import annotations

import asyncio
import io
import json

import pytest
from fastapi.testclient import TestClient

from wbbot.api.app import create_app
from wbbot.driver.machine import Machine
from wbbot.driver.transport import SimulatorTransport


@pytest.fixture
def client(cfg, simulator):
    machine = Machine(cfg, transport=SimulatorTransport(simulator),
                      simulator=simulator)
    app = create_app(cfg, machine, serve_ui=False)
    with TestClient(app) as test_client:
        yield test_client


# ----------------------------------------------------------------------
# info
# ----------------------------------------------------------------------

def test_config_endpoint(client):
    data = client.get("/api/config").json()
    assert data["board"]["width_mm"] == 1500.0
    assert len(data["colors"]) == 3
    assert data["geometry"]["anchor_left"] == [-100.0, 1350.0]


def test_status_endpoint(client):
    data = client.get("/api/status").json()
    assert data["state"] in ("idle", "disconnected")
    assert "position" in data and "progress" in data


def test_safe_area_endpoint(client):
    data = client.get("/api/safe-area").json()
    assert data["polygon"] and len(data["polygon"][0]) >= 4
    assert data["summary"]["coverage_percent"] > 90


def test_heatmap_endpoint(client):
    data = client.get("/api/heatmap?step_mm=50").json()
    assert len(data["x"]) > 5 and len(data["y"]) > 3
    assert len(data["error_amplification"]) == len(data["y"])
    assert len(data["feasible"][0]) == len(data["x"])


def test_heatmap_rejects_a_silly_step(client):
    assert client.get("/api/heatmap?step_mm=0.1").status_code == 400


def test_openapi_schema_is_valid(client):
    schema = client.get("/openapi.json").json()
    assert schema["info"]["title"] == "wbbot"
    assert "/api/status" in schema["paths"]


def test_placeholder_index_explains_how_to_build_the_ui(cfg, simulator):
    machine = Machine(cfg, transport=SimulatorTransport(simulator),
                      simulator=simulator)
    app = create_app(cfg, machine, serve_ui=True)
    with TestClient(app) as c:
        body = c.get("/").text
        assert "npm run build" in body or "<div id=" in body


# ----------------------------------------------------------------------
# control
# ----------------------------------------------------------------------

def test_move_and_pen(client):
    assert client.post("/api/move", json={"x": 600, "y": 500}).status_code == 200
    assert client.post("/api/pen", json={"down": True}).status_code == 200
    assert client.post("/api/pen", json={"down": False}).status_code == 200


def test_move_outside_the_safe_area_is_a_400(client):
    response = client.post("/api/move", json={"x": 9000, "y": 500})
    assert response.status_code == 400
    assert "not reachable" in response.json()["detail"]


def test_colour_change(client):
    assert client.post("/api/color", json={"index": 2}).status_code == 200
    assert client.post("/api/color", json={"index": 7}).status_code == 400


def test_estop_and_clear(client):
    assert client.post("/api/estop").json()["state"] == "estop"
    assert client.post("/api/clear-alarm").json()["state"] == "idle"


def test_draw_without_a_prepared_job_is_refused(client):
    response = client.post("/api/draw")
    assert response.status_code == 400
    assert "no prepared drawing" in response.json()["detail"]


def test_gcode_download_needs_a_job(client):
    assert client.get("/api/gcode").status_code == 400


def test_pause_with_no_job_is_a_409(client):
    assert client.post("/api/pause").status_code == 409


# ----------------------------------------------------------------------
# image pipeline
# ----------------------------------------------------------------------

def test_image_preview_and_draw_and_gcode(client, reference_image):
    cv2 = pytest.importorskip("cv2")
    ok, buf = cv2.imencode(".png", reference_image)
    assert ok
    files = {"file": ("test.png", io.BytesIO(buf.tobytes()), "image/png")}
    response = client.post("/api/image/preview?trace_mode=contours", files=files)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["preview_svg"].startswith("<svg")
    assert data["paths"] and data["stats"]["draw_length_mm"] > 0

    assert client.post("/api/draw").status_code == 200
    gcode = client.get("/api/gcode")
    assert gcode.status_code == 200
    assert gcode.text.startswith("; wbbot job") or "G21" in gcode.text


def test_empty_upload_is_rejected(client):
    files = {"file": ("empty.png", io.BytesIO(b""), "image/png")}
    assert client.post("/api/image/preview", files=files).status_code == 400


def test_garbage_upload_is_rejected(client):
    files = {"file": ("x.png", io.BytesIO(b"not an image"), "image/png")}
    assert client.post("/api/image/preview", files=files).status_code == 400


# ----------------------------------------------------------------------
# calibration
# ----------------------------------------------------------------------

def test_calibration_flow(client, cfg):
    from wbbot.calib.solve import simulate_measurements

    assert client.post("/api/calibration/begin").json()["step"] == "reference"
    assert client.post("/api/calibration/skip-reference").json()["step"] == "pattern"

    pattern = client.post("/api/calibration/pattern",
                          json={"n_x": 3, "n_y": 3}).json()
    targets = pattern["pattern"]["targets"]
    assert len(targets) == 9 and pattern["paths"]

    truth = dict(ax_l=-102.0, ay_l=1349.0, ax_r=1601.0, ay_r=1351.0,
                 k_left=1.002, k_right=0.999, c_left=1.0, c_right=-1.0,
                 dx=1.0, dy=-1.0)
    nominal = [(t["nominal_x"], t["nominal_y"]) for t in targets]
    measured = simulate_measurements(cfg, nominal, true_params=truth,
                                     model="tangent", noise_mm=0.3, seed=9)
    payload = [{"index": t["index"], "x": m.measured[0], "y": m.measured[1]}
               for t, m in zip(targets, measured)]
    assert client.post("/api/calibration/measure", json=payload).status_code == 200

    solved = client.post("/api/calibration/solve").json()
    assert solved["result"]["mean_error_mm"] < 2.0
    assert "mean deviation" in solved["report"]


def test_calibration_rejects_a_bad_reference(client):
    client.post("/api/calibration/begin")
    response = client.post("/api/calibration/reference",
                           json={"x": 750, "y": 500,
                                 "left_mm": 10, "right_mm": 10})
    assert response.status_code == 400


def test_calibration_solve_without_measurements_is_a_400(client):
    client.post("/api/calibration/begin")
    client.post("/api/calibration/skip-reference")
    client.post("/api/calibration/pattern", json={"n_x": 2, "n_y": 2})
    assert client.post("/api/calibration/solve").status_code == 400


def test_servo_tuner_endpoint(client):
    response = client.post("/api/calibration/servo",
                           json={"pen_up": 22.0, "pen_down": 1.0})
    assert response.status_code == 200
    data = response.json()
    assert data["pen_up"] == 22.0 and data["pen_down"] == 1.0
    assert data["problems"] == []


# ----------------------------------------------------------------------
# fluidnc config
# ----------------------------------------------------------------------

def test_fluidnc_config_download(client):
    response = client.get("/api/fluidnc-config?schema=3.7")
    assert response.status_code == 200
    text = response.text
    assert "VERIFY BEFORE FLASHING" in text
    assert "wall_plotter" in text
    assert "rc_servo" in text
    assert "gpio.25" in text and "gpio.32" in text


def test_fluidnc_config_older_schema(client):
    text = client.get("/api/fluidnc-config?schema=3.6").text
    assert "wallplotter" in text


def test_fluidnc_config_unknown_schema(client):
    assert client.get("/api/fluidnc-config?schema=9.9").status_code == 400


# ----------------------------------------------------------------------
# live drawing
# ----------------------------------------------------------------------

def test_live_start_stop(client):
    started = client.post("/api/live/start")
    assert started.status_code == 200, started.text
    assert started.json()["state"] in ("following", "drawing")
    assert client.get("/api/live/status").json()["undo_supported"] is False
    assert client.post("/api/live/stop").status_code == 200
    assert client.get("/api/live/status").json()["state"] == "stopped"


def test_live_stop_without_a_session_is_a_400(client):
    assert client.post("/api/live/stop").status_code == 400


# ----------------------------------------------------------------------
# websocket
# ----------------------------------------------------------------------

def test_websocket_hello_carries_the_board(client):
    with client.websocket_connect("/ws") as ws:
        message = ws.receive_json()
        assert message["kind"] == "hello"
        assert message["payload"]["board"] == [1500.0, 1000.0]
        assert message["payload"]["safe_area"]
        assert len(message["payload"]["colors"]) == 3


def test_websocket_ping_pong(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"kind": "ping", "payload": {"t": 42}})
        for _ in range(20):
            message = ws.receive_json()
            if message["kind"] == "pong":
                assert message["payload"]["t"] == 42
                return
        pytest.fail("no pong received")


def test_websocket_rejects_an_unknown_frame(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"kind": "nonsense"})
        for _ in range(20):
            message = ws.receive_json()
            if message["kind"] == "error":
                assert "nonsense" in message["payload"]["message"]
                return
        pytest.fail("no error frame received")


def test_websocket_receives_machine_events(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()                      # hello
        client.post("/api/color", json={"index": 1})
        kinds = []
        for _ in range(40):
            kinds.append(ws.receive_json()["kind"])
            if "status" in kinds:
                break
        assert "status" in kinds


def test_websocket_estop_frame_stops_the_machine(client):
    with client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.send_json({"kind": "estop"})
        for _ in range(40):
            message = ws.receive_json()
            if message["kind"] == "state" and message["payload"]["state"] == "estop":
                break
        assert client.get("/api/status").json()["state"] == "estop"


# ----------------------------------------------------------------------
# hub behaviour
# ----------------------------------------------------------------------

async def test_hub_drops_frames_for_a_slow_client_instead_of_blocking():
    """A stalled browser tab must never stall the machine's event loop."""
    from wbbot.api.ws import CLIENT_QUEUE_SIZE, Hub

    hub = Hub()

    class DeadSocket:
        async def send_text(self, message): ...

    client = await hub.add(DeadSocket())
    for i in range(CLIENT_QUEUE_SIZE * 3):
        hub.broadcast("status", {"i": i})
    assert client.queue.qsize() <= CLIENT_QUEUE_SIZE
    assert client.dropped > 0
    # The newest frame must survive; stale status is what gets thrown away.
    frames = []
    while not client.queue.empty():
        frames.append(json.loads(client.queue.get_nowait()))
    assert frames[-1]["payload"]["i"] == CLIENT_QUEUE_SIZE * 3 - 1


async def test_hub_broadcast_with_no_clients_is_a_noop():
    from wbbot.api.ws import Hub
    Hub().broadcast("status", {"x": 1})


# ----------------------------------------------------------------------
# corner calibration
# ----------------------------------------------------------------------

def test_corner_calibration_flow_over_the_api(client):
    state = client.post("/api/calibration/corners/begin").json()
    assert state["step"] == "corners"
    targets = state["corners"]["targets"]
    assert len(targets) == 4
    assert state["corners"]["labels"] == [
        "oben links", "oben rechts", "unten rechts", "unten links"]

    for index, (x, y) in enumerate(targets):
        move = client.post("/api/calibration/corners/move",
                           json={"x": x, "y": y})
        assert move.status_code == 200, move.text
        record = client.post("/api/calibration/corners/record",
                             json={"index": index})
        assert record.status_code == 200, record.text
    assert record.json()["corners"]["missing"] == []

    solved = client.post("/api/calibration/corners/solve").json()
    assert solved["meets_target"]
    # A perfect (simulated) machine must come back essentially unchanged.
    assert solved["result"]["mean_error_mm"] < 0.05
    assert abs(solved["result"]["params"]["ax_l"] - (-100.0)) < 0.5


def test_corner_move_can_reach_the_physical_corners(client):
    """(0,0) sits inside the normal safe-area margin; corner mode allows it."""
    client.post("/api/calibration/corners/begin")
    assert client.post("/api/calibration/corners/move",
                       json={"x": 0, "y": 0}).status_code == 200
    assert client.post("/api/calibration/corners/move",
                       json={"x": 1500, "y": 1000}).status_code == 200


def test_corner_move_still_refuses_the_truly_unreachable(client):
    client.post("/api/calibration/corners/begin")
    response = client.post("/api/calibration/corners/move",
                           json={"x": -400, "y": 500})
    assert response.status_code == 400
    assert "not reachable" in response.json()["detail"]


def test_corner_solve_without_records_is_a_400(client):
    client.post("/api/calibration/corners/begin")
    assert client.post("/api/calibration/corners/solve").status_code == 400
