"""End to end over the network: Machine -> TcpLink/WsLink -> localhost -> FakeBoard (emulator).

Covers the "board on power only, controlled over WiFi" scenario: no reset on connect, reference
restore after reconnect, link loss = Not-Halt (job aborted, reference uncertain), reconnect.
Only localhost sockets, no serial port, no internet.
"""
from __future__ import annotations

import socket
import time

import pytest
from fastapi.testclient import TestClient

import wbot.machine as machine_mod
from wbot.api.app import create_app
from wbot.config import Config, load_config
from wbot.machine import Machine, MachineError
from wbot.sim.netserver import FakeBoard


@pytest.fixture()
def board():
    b = FakeBoard(load_config(), tcp_port=0, ws_port=0, speed=60).start()
    yield b
    b.stop()


@pytest.fixture()
def m(tmp_path, monkeypatch):
    for name in ("REFERENCE_FILE", "FAKE_REFERENCE_FILE"):
        monkeypatch.setattr(machine_mod, name, tmp_path / (name.lower() + ".json"))
    monkeypatch.setattr(machine_mod, "STATE_DIR", tmp_path)
    mach = Machine(load_config())
    yield mach
    mach.shutdown()


def wait(cond, timeout=30.0, what="condition"):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for {what}")


def wait_job(mach, timeout=90):
    wait(lambda: mach.job_info() and mach.job_info()["state"] not in ("running", "paused", "planning"),
         timeout, "job end")
    return mach.job_info()


def conn(m, board, protocol="telnet"):
    port = board.tcp_port if protocol == "telnet" else board.ws_port
    return m.connect("wifi", port, host="127.0.0.1", protocol=protocol)


@pytest.mark.parametrize("protocol", ["telnet", "websocket"])
def test_connect_reference_job_accuracy(m, board, protocol):
    st = conn(m, board, protocol)
    assert st["connection"]["status"] == "connected" and st["connection"]["transport"] == "wifi"
    assert st["connection"]["protocol"] == protocol
    port = board.tcp_port if protocol == "telnet" else board.ws_port
    assert st["connection"]["port"] == f"127.0.0.1:{port}" and st["connection"]["host"] == "127.0.0.1"
    assert st["machine"]["state"] == "idle"
    assert m.set_reference(0, 0)["reference"]["set"]
    m.goto(500, 400)
    assert m.state()["position"]["physical"] == pytest.approx([500, 400], abs=0.1)
    m.start_job({"pattern": {"id": "diagonals", "params": {}}})
    assert wait_job(m)["state"] == "done"
    err = m.jobs.trace_error(board.emu.trace)
    assert err and err["max_mm"] < 0.1                       # the point of the whole project, now over WiFi
    assert m.state()["pen"]["up"] is True


def test_connect_does_not_reset_board_and_reference_survives_reconnect(m, board):
    conn(m, board)
    m.set_reference(0, 0)
    m.goto(500, 400)
    fw_before = m.state()["position"]["firmware"]
    time.sleep(1.5)                                          # reference is persisted at <= 1 Hz
    m.disconnect()
    assert m.state()["reference"]["set"] is False
    st = conn(m, board)                                      # board stayed powered: no banner, no reset
    assert st["position"]["firmware"] == pytest.approx(fw_before, abs=1e-6)
    assert st["reference"]["set"] and st["reference"]["restored"]
    assert st["position"]["physical"] == pytest.approx([500, 400], abs=0.1)
    assert board.connections == 2


def test_loopback_reference_uses_fake_board_file(m, board, tmp_path):
    conn(m, board)
    m.set_reference(0, 0)
    assert (tmp_path / "fake_reference_file.json").exists()
    assert not (tmp_path / "reference_file.json").exists()   # the real board's reference is untouched


def test_link_drop_mid_job_is_not_halt_and_reconnect_works(m, board):
    conn(m, board)
    m.set_reference(0, 0)
    m.start_job({"pattern": {"id": "diagonals", "params": {}}})
    wait(lambda: (m.job_info() or {}).get("line", 0) > 3, 30, "job progress")
    board.drop_client()                                      # WiFi gone
    job = wait_job(m, 30)
    assert job["state"] == "error"          # not "cancelled": the cause is the lost link
    s = m.state()
    assert s["connection"]["status"] == "error" and s["machine"]["state"] == "disconnected"
    assert s["reference"]["uncertain"] is True
    with pytest.raises(MachineError) as ei:
        m.goto(100, 100)
    assert ei.value.code == "not_connected" or ei.value.code == "no_reference"
    # the board keeps executing what it already had; once it stood still we can reconnect
    wait(lambda: board.emu.firmware_position() is not None and not board.emu._active(), 60, "board idle")
    st = conn(m, board)                                      # stale client is cleaned up automatically
    assert st["connection"]["status"] == "connected"
    # position moved after the last persisted reference -> NOT restored (unsafe to trust it)
    assert st["reference"]["set"] is False
    assert m.set_reference(*board.emu.physical_position())["reference"]["set"]


def test_connection_errors_are_clear(m):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(MachineError) as ei:
        m.connect("wifi", port, host="127.0.0.1")
    assert ei.value.code == "connect_failed" and f"127.0.0.1:{port}" in ei.value.message
    assert m.state()["connection"]["status"] == "error"
    with pytest.raises(MachineError) as ei:
        m.connect("wifi", None, host="127.0.0.1", protocol="smoke")
    assert ei.value.code == "bad_wifi" and ei.value.status == 400


def test_second_client_replaces_first_like_a_single_channel_board(board):
    from wbot.fluidnc.tcp_link import TcpLink
    a, b = TcpLink("127.0.0.1", board.tcp_port), TcpLink("127.0.0.1", board.tcp_port)
    a.open()
    a.write(b"?")
    wait(lambda: board.has_client, 5, "client a")
    b.open()
    b.write(b"?")
    data = b""
    end = time.time() + 3
    while b"<" not in data and time.time() < end:
        data += b.read(0.05)
    assert b"<Idle" in data
    from wbot.fluidnc.link import LinkError
    with pytest.raises(LinkError):
        for _ in range(60):
            a.read(0.05)
    b.close()
    a.close()


def test_api_connect_wifi_body(board, tmp_path, monkeypatch):
    monkeypatch.setattr(machine_mod, "STATE_DIR", tmp_path)
    monkeypatch.setattr(machine_mod, "FAKE_REFERENCE_FILE", tmp_path / "f.json")
    mach = Machine(load_config())
    with TestClient(create_app(mach, serve_ui=False)) as c:
        r = c.post("/api/connect", json={"transport": "wifi", "host": "127.0.0.1",
                                         "protocol": "telnet", "port": board.tcp_port})
        assert r.status_code == 200, r.text
        assert c.get("/api/state").json()["connection"]["port"] == f"127.0.0.1:{board.tcp_port}"
        assert c.post("/api/disconnect").status_code == 200
        r = c.post("/api/connect", json={"transport": "wifi", "host": "127.0.0.1",
                                         "protocol": "websocket", "port": str(board.ws_port)})
        assert r.status_code == 200, r.text
        assert r.json()["connection"]["protocol"] == "websocket"
        mach.shutdown()


def test_wifi_config_defaults_match_yaml():
    a, b = Config().connection.wifi, load_config().connection.wifi
    assert a == b and a.host == "fluidnc.local" and a.protocol == "telnet" and a.port is None
    assert a.effective_port == 23
    assert a.model_copy(update={"protocol": "websocket"}).effective_port == 81
