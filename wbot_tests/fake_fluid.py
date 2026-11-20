"""In-memory fake FluidNC for client tests: 128-byte RX buffer, planner back-pressure,
realtime bytes, boot banner, alarm/unlock behaviour, link drop and silence injection."""
from __future__ import annotations

import queue
import threading
import time

import pytest

from wbot.fluidnc.client import FluidNCClient
from wbot.fluidnc.link import Link, LinkError

BANNER = "Grbl 3.9 [FluidNC v3.9.9 (wifi) '$' for help]"


class FakeFluidLink(Link):
    """Tiny FluidNC stand-in: 128-byte RX buffer, planner with back-pressure, realtime bytes."""
    name = "fake"
    RX_SIZE = 128

    def __init__(self, *, line_delay=0.0, move_time=0.0, planner_blocks=4, state="Idle",
                 banner_delay=0.05, boot_banner=True, alarm_after=None, banner_on_unlock=False):
        self.out: queue.Queue = queue.Queue()
        self._lines: queue.Queue = queue.Queue()
        self.line_delay, self.move_time, self.planner_blocks = line_delay, move_time, planner_blocks
        self.banner_delay, self.boot_banner = banner_delay, boot_banner
        self.alarm_after, self.banner_on_unlock = alarm_after, banner_on_unlock
        self.rx_used = self.max_rx_used = 0
        self.overflow = False
        self.processed: list[str] = []
        self.rt_log: list[bytes] = []
        self.alarm = state == "Alarm"
        self.held = False
        self.sub = 0
        self.busy_until = 0.0
        self.silent_until = 0.0
        self.dead = False
        self.muted = False
        self.concurrent_writes = 0
        self._in_write = False
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._open = False
        self._stop = False
        self._epoch = 0

    # --- Link --------------------------------------------------------------------
    def open(self):
        self._open = True
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def close(self):
        self._open = False
        self._stop = True

    @property
    def is_open(self):
        return self._open and not self.dead

    def drop(self):
        self.dead = True
        self.out.put(None)

    def write(self, data: bytes):
        if self.dead or not self._open:
            raise LinkError("fake link dead")
        if self._in_write:
            self.concurrent_writes += 1
        self._in_write = True
        try:
            time.sleep(0.0003)                      # widen the race window for lock bugs
            for b in data:
                ch = bytes([b])
                if ch in (b"?", b"!", b"~", b"\x18", b"\x85"):
                    self._realtime(ch)
                elif ch == b"\n":
                    self._complete_line(self._buf.decode())
                    self._buf.clear()
                else:
                    self._buf.extend(ch)
        finally:
            self._in_write = False

    def read(self, timeout=0.05):
        if self.dead:
            raise LinkError("fake link dead")
        try:
            item = self.out.get(timeout=timeout)
        except queue.Empty:
            return b""
        if item is None:
            raise LinkError("fake link dropped")
        return item

    # --- controller behaviour ------------------------------------------------------
    def emit(self, text: str, *, force=False):
        if self.dead or (self.muted and not force):
            return
        self.out.put((text + "\r\n").encode())

    def _state_name(self):
        if self.alarm:
            return "Alarm"
        if self.held:
            return f"Hold:{self.sub}"
        if time.monotonic() < self.busy_until or not self._lines.empty():
            return "Run"
        return "Idle"

    def _realtime(self, ch: bytes):
        self.rt_log.append(ch)
        now = time.monotonic()
        if ch == b"?":
            if now >= self.silent_until:
                self.emit(f"<{self._state_name()}|MPos:0.000,0.000,0.000|FS:0,0>")
        elif ch == b"!":
            if self._state_name() == "Run":
                self.held, self.sub = True, 1
                threading.Timer(0.05, lambda: setattr(self, "sub", 0)).start()
        elif ch == b"~":
            self.held = False
        elif ch == b"\x18":
            with self._lock:
                was_run = self._state_name() in ("Run", "Hold:1")   # reset while still moving
                self._epoch += 1
                while not self._lines.empty():
                    self._lines.get_nowait()
                self.rx_used = 0
                self.busy_until = 0.0
                self.held = False
                self.alarm = was_run
                self.silent_until = now + self.banner_delay
            threading.Timer(self.banner_delay, self._after_reset).start()

    def _after_reset(self):
        self.emit(BANNER)
        if self.alarm:
            self.emit("ALARM:3")

    def _complete_line(self, text: str):
        with self._lock:
            self.rx_used += len(text) + 1
            self.max_rx_used = max(self.max_rx_used, self.rx_used)
            if self.rx_used > self.RX_SIZE:
                self.overflow = True
        self._lines.put(text)

    def _run(self):
        if self.boot_banner:
            self.silent_until = time.monotonic() + self.banner_delay
            time.sleep(self.banner_delay)
            self.emit(BANNER)
            self.emit("[MSG:INFO: Kinematic system: WallPlotter]")
        while not self._stop and not self.dead:
            if self.held:
                time.sleep(0.005)
                continue
            try:
                text = self._lines.get(timeout=0.01)
            except queue.Empty:
                continue
            epoch = self._epoch
            time.sleep(self.line_delay)
            if text.startswith("G1") or text.startswith("G0"):
                # planner back-pressure: no ok until a planner slot is free
                while (self.busy_until - time.monotonic() > (self.planner_blocks - 1) * self.move_time
                       and not self.held and not self._stop and epoch == self._epoch):
                    time.sleep(0.002)
            if epoch != self._epoch:
                continue                                  # flushed by a reset while in hand
            self._process(text)

    def _process(self, text: str):
        with self._lock:
            self.rx_used = max(0, self.rx_used - (len(text) + 1))
        self.processed.append(text)
        if text == "$I":
            self.emit("[VER:3.9 FluidNC v3.9.9:]")
            self.emit("[OPT:PH,35,128]")
            self.emit("ok")
        elif text == "$X":
            self.alarm = False
            self.emit("[MSG:INFO: Caution: Unlocked]")
            if self.banner_on_unlock:
                self.emit(BANNER)
            else:
                self.emit("ok")
        elif self.alarm:
            self.emit("error:9")
        elif "ERR" in text:
            self.emit("error:20")
        else:
            if text.startswith(("G1", "G0")):
                self.busy_until = max(time.monotonic(), self.busy_until) + self.move_time
            self.emit("ok")
            if self.alarm_after is not None and len(self.processed) >= self.alarm_after:
                self.alarm = True
                self.emit("ALARM:1")


# --- helpers ---------------------------------------------------------------------------
@pytest.fixture
def make():
    made: list[FluidNCClient] = []

    def factory(*, connect=True, client_kw=None, **fake_kw):
        events: list = []
        fake = FakeFluidLink(**fake_kw)
        kw = dict(status_hz=50.0, listener=events.append)
        kw.update(client_kw or {})
        client = FluidNCClient(fake, **kw)
        made.append(client)
        if connect:
            client.connect(timeout=3.0)
        return fake, client, events

    yield factory
    for c in made:
        c.close()


def moves(n, base=0):
    return [f"G1 X{base + i}.5 Y{i}.25 F1500" for i in range(n)]
