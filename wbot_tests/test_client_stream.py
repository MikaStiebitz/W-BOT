"""FluidNCClient streaming: character counting, errors, cancel, alarm (fake link)."""
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


# --- streaming ---------------------------------------------------------------------------------
def test_stream_character_counting_never_overflows(make):
    fake, c, _ = make(line_delay=0.002)
    lines = moves(200)
    progress = []
    res = c.stream(lines, on_progress=lambda a, t: progress.append((a, t)))
    assert res.ok and res.status == "done" and res.acked == res.sent == res.total == 200
    assert not fake.overflow and fake.max_rx_used <= 127
    assert fake.processed[-200:] == lines                    # order preserved
    assert progress and progress[-1] == (200, 200)
    assert [a for a, _ in progress] == sorted(a for a, _ in progress)
    assert c.state.in_flight_bytes == 0 and not c.state.streaming


def test_stream_uses_the_buffer_not_one_line_at_a_time(make):
    fake, c, _ = make(line_delay=0.01)
    c.stream(moves(40))
    assert fake.max_rx_used > 60                             # several lines in flight


def test_stream_stop_on_error(make):
    fake, c, _ = make(line_delay=0.001)
    lines = moves(10) + ["G1 ERR"] + moves(10, 50)
    res = c.stream(lines)
    assert res.status == "error" and not res.ok
    assert res.errors and res.errors[0].index == 10 and res.errors[0].code == 20
    assert "error:20" in res.message
    assert res.acked < len(lines) or res.sent < len(lines)


def test_stream_continue_on_error(make):
    fake, c, _ = make()
    lines = moves(5) + ["G1 ERR"] + moves(5, 20)
    res = c.stream(lines, stop_on_error=False)
    assert res.status == "error" and res.acked == 11 and len(res.errors) == 1


def test_stream_skips_blank_lines(make):
    fake, c, _ = make()
    res = c.stream(["", "G0 X1", "  ", "G0 X2"])
    assert res.ok and res.total == 2


def test_send_during_stream_raises(make):
    fake, c, _ = make(line_delay=0.02)
    out = []
    t = threading.Thread(target=lambda: out.append(c.stream(moves(30))))
    t.start()
    time.sleep(0.15)
    with pytest.raises(StreamActiveError):
        c.send("G0 X1")
    with pytest.raises(StreamActiveError):
        c.stream(["G0 X1"])
    c.feed_hold()                                            # realtime stays allowed
    c.cycle_start()
    t.join(5)
    assert out and out[0].ok
    assert c.send("G0 X2").ok                                 # free again afterwards


def test_realtime_bytes_not_counted(make):
    fake, c, _ = make(line_delay=0.05)
    t = threading.Thread(target=lambda: c.stream(moves(20)))
    t.start()
    time.sleep(0.2)
    before = c.state.in_flight_bytes
    for _ in range(5):
        c.cycle_start()
        c.jog_cancel()
    assert c.state.in_flight_bytes <= 127 and before <= 127
    t.join(10)
    assert not fake.overflow
    assert b"~" in fake.rt_log and b"\x85" in fake.rt_log
    assert all("~" not in ln and "\x85" not in ln for ln in fake.processed)


def test_status_polling_continues_while_streaming(make):
    fake, c, _ = make(line_delay=0.01)
    n0 = fake.rt_log.count(b"?")
    c.stream(moves(60))
    assert fake.rt_log.count(b"?") - n0 >= 5


def test_no_concurrent_writes(make):
    fake, c, _ = make(line_delay=0.001)
    stop = threading.Event()

    def spam():
        while not stop.is_set():
            try:
                c.cycle_start()
            except LinkError:
                return
            time.sleep(0.001)

    t = threading.Thread(target=spam)
    t.start()
    res = c.stream(moves(80))
    stop.set()
    t.join(2)
    assert res.ok and fake.concurrent_writes == 0


def test_stream_cancel_uses_hold_then_reset(make):
    fake, c, _ = make(move_time=0.2)
    cancel = threading.Event()
    out = []
    t = threading.Thread(target=lambda: out.append(c.stream(moves(60), cancel=cancel)))
    t.start()
    time.sleep(0.7)
    cancel.set()
    t.join(10)
    assert out, "stream did not return"
    res = out[0]
    assert res.status == "cancelled" and 0 < res.acked < 60
    rt = [b for b in fake.rt_log if b in (b"!", b"\x18")]
    assert rt == [b"!", b"\x18"]
    assert res.final_state == "Idle"                         # reset out of Hold:0 -> no alarm
    assert c.state.in_flight_bytes == 0 and not c.state.resetting
    assert c.send("G0 X1").ok


def test_wait_idle_waits_for_motion(make):
    fake, c, _ = make(move_time=0.2)
    assert c.stream(moves(3)).ok
    t0 = time.monotonic()
    assert c.wait_idle(5.0)
    assert time.monotonic() - t0 >= 0.3
    assert c.state.machine_state == "Idle"


def test_wait_idle_timeout_and_cancel(make):
    fake, c, _ = make(move_time=1.0)
    c.stream(moves(3))
    assert c.wait_idle(0.2) is False
    ev = threading.Event()
    threading.Timer(0.1, ev.set).start()
    t0 = time.monotonic()
    assert c.wait_idle(5.0, cancel=ev) is False
    assert time.monotonic() - t0 < 1.0


def test_stream_aborts_on_alarm(make):
    fake, c, _ = make(alarm_after=8)
    res = c.stream(moves(100))
    assert res.status == "alarm" and res.sent < 100
    assert c.state.alarm and c.state.alarm[0] == 1
    assert c.state.machine_state == "Alarm"


def test_stream_aborts_on_unexpected_reset(make):
    fake, c, _ = make(move_time=0.2)
    out = []
    t = threading.Thread(target=lambda: out.append(c.stream(moves(60))))
    t.start()
    time.sleep(0.4)
    fake.emit(BANNER)
    t.join(5)
    assert out and out[0].status == "reset"
