"""FluidNCClient: connect/send/query, e-stop, link loss (fake link, no serial port)."""
from __future__ import annotations

import threading
import time

import pytest

from wbot.fluidnc.client import (BootInfo, ClientError, CommandFailed, FluidNCClient, LinkEvent,
                                 StreamActiveError)
from wbot.fluidnc.link import LinkError

try:
    from fake_fluid import BANNER, FakeFluidLink, make, moves  # noqa: F401
except ImportError:  # pragma: no cover - when wbot_tests is a package
    from .fake_fluid import BANNER, FakeFluidLink, make, moves  # noqa: F401


# --- connect / send / query ----------------------------------------------------------------
def test_connect_reports_boot_info(make):
    fake, c, events = make()
    st = c.state
    assert st.link == "open" and st.machine_state == "Idle" and st.firmware == "FluidNC v3.9.9"
    assert st.mpos == (0.0, 0.0, 0.0)
    assert any(isinstance(e, LinkEvent) and e.state == "open" for e in events)
    boot = c.recent_lines()
    assert any("WallPlotter" in ln for ln in boot)


def test_connect_boot_info_object(make):
    fake, c, _ = make(connect=False)
    info = c.connect(timeout=3.0)
    assert isinstance(info, BootInfo)
    assert info.firmware == "FluidNC v3.9.9" and info.banner.startswith("Grbl 3.9")
    assert info.ver_line == "3.9 FluidNC v3.9.9" and info.alarm is None
    assert any("Kinematic system" in ln for ln in info.boot_lines)


def test_connect_timeout_when_controller_silent(make):
    fake = FakeFluidLink(boot_banner=False)
    fake.muted = True
    c = FluidNCClient(fake, status_hz=50.0)
    with pytest.raises(LinkError):
        c.connect(timeout=0.4)
    assert c.state.link == "closed"


def test_connect_in_alarm_reports_alarm(make):
    fake, c, _ = make(state="Alarm")
    assert c.state.machine_state == "Alarm" and c.state.alarm is not None


def test_send_ok_and_error(make):
    fake, c, _ = make()
    r = c.send("G0 X1 Y1")
    assert r.ok and r.status == "ok" and bool(r)
    r = c.send("G1 ERR")
    assert not r.ok and r.status == "error" and r.code == 20 and r.text
    assert "error:20" in (c.state.last_error or "")


def test_query_collects_lines(make):
    fake, c, _ = make()
    lines = c.query("$I")
    assert lines == ["[VER:3.9 FluidNC v3.9.9:]", "[OPT:PH,35,128]"]


def test_query_failure_raises(make):
    fake, c, _ = make()
    with pytest.raises(CommandFailed):
        c.query("G1 ERR")


def test_send_timeout_when_controller_never_answers(make):
    fake, c, _ = make(client_kw=dict(watchdog_s=0))
    fake.muted = True
    r = c.send("G0 X1", timeout=0.3)
    assert r.status == "timeout" and not r.ok


def test_line_too_long_rejected(make):
    fake, c, _ = make()
    with pytest.raises(ValueError):
        c.send("G1 " + "X" * 200)
    with pytest.raises(ValueError):
        c.send("G1\nG1")


# --- emergency stop / lost link ---------------------------------------------------------------
def test_emergency_stop_frees_blocked_threads_fast(make):
    fake, c, _ = make(move_time=0.5)
    results = {}
    t_stream = threading.Thread(target=lambda: results.setdefault("stream", c.stream(moves(100))))
    t_stream.start()
    time.sleep(0.5)
    t_wait = threading.Thread(target=lambda: results.setdefault("idle", c.wait_idle(30.0)))
    t_wait.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    assert c.emergency_stop() is True
    t_stream.join(2)
    t_wait.join(2)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5
    assert not t_stream.is_alive() and not t_wait.is_alive()
    assert results["stream"].status == "estop" and results["idle"] is False
    assert [b for b in fake.rt_log if b in (b"!", b"\x18")] == [b"!", b"\x18"]


def test_emergency_stop_blocked_send_is_freed(make):
    fake, c, _ = make()
    fake.muted = True
    out = []
    t = threading.Thread(target=lambda: out.append(c.send("G0 X1", timeout=20.0)))
    t.start()
    time.sleep(0.2)
    c.emergency_stop()
    t.join(2)
    assert out and out[0].status in ("estop", "reset") and not out[0].ok


def test_unlock_after_estop_during_motion(make):
    fake, c, _ = make(move_time=0.5)
    t = threading.Thread(target=lambda: c.stream(moves(40)))
    t.start()
    time.sleep(0.4)
    c.emergency_stop()
    assert c.wait_ready(5.0)
    assert c.state.machine_state == "Alarm" and c.state.alarm is not None
    t.join(3)
    assert c.unlock(10.0) is True
    assert c.state.machine_state == "Idle" and c.state.alarm is None
    assert c.send("G0 X1").ok


def test_unlock_verified_by_status_when_receipt_lost_to_banner(make):
    fake, c, _ = make(state="Alarm", banner_on_unlock=True)
    assert c.unlock(10.0) is True
    assert c.state.machine_state == "Idle"


def test_unlock_when_not_locked_is_true(make):
    fake, c, _ = make()
    assert c.unlock(3.0) is True
    assert "$X" not in fake.processed


def test_soft_reset_and_wait_ready(make):
    fake, c, _ = make()
    assert c.reset(5.0) is True
    st = c.state
    assert st.machine_state == "Idle" and not st.resetting
    assert fake.rt_log.count(b"\x18") == 1


def test_link_loss_wakes_everything_and_notifies(make):
    fake, c, events = make(move_time=1.0)
    results = {}
    t1 = threading.Thread(target=lambda: results.setdefault("stream", c.stream(moves(50))))
    t1.start()
    time.sleep(0.3)
    t2 = threading.Thread(target=lambda: results.setdefault("idle", c.wait_idle(30.0)))
    t2.start()
    time.sleep(0.1)
    t0 = time.monotonic()
    fake.drop()
    t1.join(3)
    t2.join(3)
    assert time.monotonic() - t0 < 2.5
    assert not t1.is_alive() and not t2.is_alive()
    assert results["stream"].status == "lost" and results["idle"] is False
    assert c.state.link == "lost" and c.state.link_error
    lost = [e for e in events if isinstance(e, LinkEvent) and e.state == "lost"]
    assert len(lost) == 1
    r = c.send("G0 X1")
    assert r.status == "lost" and not r.ok
    with pytest.raises(LinkError):
        c.feed_hold()
    assert c.emergency_stop() is False                       # must not raise or hang


def test_link_loss_during_blocked_send(make):
    fake, c, _ = make()
    fake.muted = True
    out = []
    t = threading.Thread(target=lambda: out.append(c.send("G0 X1", timeout=20.0)))
    t.start()
    time.sleep(0.2)
    fake.drop()
    t.join(3)
    assert out and out[0].status == "lost"


def test_watchdog_declares_silent_controller_lost(make):
    fake, c, events = make(client_kw=dict(watchdog_s=0.4))
    fake.muted = True
    deadline = time.monotonic() + 3
    while c.state.link != "lost" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert c.state.link == "lost"
    assert any(isinstance(e, LinkEvent) and e.state == "lost" for e in events)


def test_close_frees_waiters_and_is_idempotent(make):
    fake, c, events = make()
    fake.muted = True
    c._watchdog_s = 0
    out = []
    t = threading.Thread(target=lambda: out.append(c.send("G0 X1", timeout=20.0)))
    t.start()
    time.sleep(0.2)
    c.close()
    t.join(3)
    assert out and out[0].status == "closed"
    c.close()
    assert c.state.link == "closed"
    assert sum(1 for e in events if isinstance(e, LinkEvent) and e.state == "closed") == 1


def test_reconnect_after_close(make):
    fake, c, _ = make()
    c.close()
    fake.boot_banner = False
    fake.muted = False
    c.connect(timeout=3.0)
    assert c.state.link == "open" and c.send("G0 X1").ok


def test_listener_exception_does_not_break_reader(make):
    calls = []

    def bad(ev):
        calls.append(ev)
        raise RuntimeError("boom")

    fake, c, _ = make(client_kw=dict(listener=bad))
    assert c.send("G0 X1").ok
    assert calls


# --- SerialLink with a monkeypatched pyserial (never opens a real port) ---------------------
def test_serial_link_maps_errors_and_serialises_writes(monkeypatch):
    import serial

    from wbot.fluidnc import serial_link

    class FakeSerial:
        def __init__(self, port, baud, **kw):
            self.kw, self.is_open, self.timeout = kw, True, kw["timeout"]
            self.in_waiting, self.busy, self.overlap, self.fail = 0, False, 0, None
            self.rx = bytearray(b"ok\r\n")

        def write(self, data):
            if self.fail:
                raise self.fail
            if self.busy:
                self.overlap += 1
            self.busy = True
            time.sleep(0.002)
            self.busy = False
            return len(data)

        def read(self, n=1):
            out, self.rx = bytes(self.rx[:n]), self.rx[n:]
            self.in_waiting = len(self.rx)
            return out

        def close(self):
            self.is_open = False

    monkeypatch.setattr(serial_link.serial, "Serial", FakeSerial)
    link = serial_link.SerialLink("COM99", 115200)
    with pytest.raises(LinkError):
        link.write(b"x")
    with pytest.raises(LinkError):
        link.read()
    link.open()
    assert link.is_open and link._ser.kw["rtscts"] is False and link._ser.kw["write_timeout"] > 0
    threads = [threading.Thread(target=lambda: [link.write(b"?") for _ in range(20)]) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert link._ser.overlap == 0
    assert link.read(0.05) == b"ok\r\n"
    link._ser.fail = serial.SerialTimeoutException("t")
    with pytest.raises(LinkError):
        link.write(b"x")
    link._ser.fail = serial.SerialException("gone")
    with pytest.raises(LinkError):
        link.write(b"x")
    link.close()
    assert not link.is_open


def test_list_ports_shape():
    from wbot.fluidnc.serial_link import list_ports
    for p in list_ports():
        assert {"device", "description", "hwid"} <= set(p)
