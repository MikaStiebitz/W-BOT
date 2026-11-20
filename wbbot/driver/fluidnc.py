"""FluidNC protocol driver: streaming, status, realtime commands.

Streaming uses the **character-counting** protocol, not "send a line and wait
for ok".  The controller acknowledges a line when it has been *parsed and
queued*, not when it has been executed, so send-and-wait would leave the
planner empty between every pair of lines and the machine would stutter its
way through a traced photo.  Instead we keep the controller's 128-byte serial
RX buffer as full as it can be and pop one entry off the pending queue for
every ``ok`` / ``error:`` that comes back.

Realtime commands (``?``, ``!``, ``~``, ``0x18``, ``0x85``) bypass the line
buffer entirely — that is what makes ``0x85`` (jog cancel) usable as the
steering primitive for live drawing.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Awaitable, Callable, Iterable, Sequence

from ..config import MachineConfig
from .transport import ConnectionLost, Transport, TransportError

log = logging.getLogger(__name__)

__all__ = [
    "FluidNCError",
    "GrblError",
    "GrblAlarm",
    "MachineStatus",
    "FluidNCDriver",
    "ERROR_TEXT",
    "ALARM_TEXT",
    "RT_STATUS",
    "RT_FEED_HOLD",
    "RT_RESUME",
    "RT_SOFT_RESET",
    "RT_JOG_CANCEL",
]

RT_STATUS = b"?"
RT_FEED_HOLD = b"!"
RT_RESUME = b"~"
RT_SOFT_RESET = b"\x18"
RT_SAFETY_DOOR = b"\x84"
RT_JOG_CANCEL = b"\x85"

#: GRBL / FluidNC error codes, in plain language for the UI.
ERROR_TEXT: dict[int, str] = {
    1: "G-code command letter not recognised",
    2: "Bad number format in the G-code",
    3: "Unsupported '$' system command",
    4: "Negative value where a positive one is required",
    5: "Homing is not enabled in the firmware config",
    6: "Step pulse time is too short",
    7: "EEPROM read failed, defaults restored",
    8: "'$' command only allowed while idle",
    9: "Locked out during alarm or homing — unlock with $X",
    10: "Soft limits need homing to be enabled",
    11: "Line length exceeded",
    12: "Requested feed exceeds the machine maximum",
    13: "A safety door input is active",
    14: "Build info / startup line exceeds the line limit",
    15: "Jog target exceeds the machine travel",
    16: "Invalid jog command",
    17: "Laser mode needs PWM output",
    20: "Unsupported or invalid G-code command",
    21: "Two G-code words from the same modal group",
    22: "Feed rate has not been set",
    23: "G-code command needs an integer value",
    24: "Two G-code commands need conflicting axis words",
    25: "A G-code word was repeated",
    26: "A G-code command is missing a required axis word",
    27: "Line number value is out of range",
    28: "G-code command is missing a required value word",
    29: "Work coordinate system not supported",
    30: "G53 only works with G0 and G1",
    31: "Axis words found but not used by this command",
    32: "G2/G3 arcs need at least one in-plane axis word",
    33: "Invalid motion target",
    34: "Arc radius is geometrically impossible",
    35: "G2/G3 arcs need in-plane offset words",
    36: "Unused value words present",
    37: "Tool length offset is only valid on the configured axis",
    38: "Tool number is greater than the maximum supported",
}

#: GRBL / FluidNC alarm codes.
ALARM_TEXT: dict[int, str] = {
    1: "Hard limit triggered — position is lost, re-home the machine",
    2: "Motion target exceeds the soft limit",
    3: "Reset while in motion — position is lost",
    4: "Probe failed: initial probe state was wrong",
    5: "Probe failed: probe did not contact the workpiece",
    6: "Homing failed: reset during homing",
    7: "Homing failed: a safety door opened",
    8: "Homing failed: limit switch not cleared on pull-off",
    9: "Homing failed: limit switch not found within the search distance",
    10: "Spindle control failure",
    11: "Control pin initially asserted",
}

_STATUS_RE = re.compile(r"^<([^|>]+)((?:\|[^|>]*)*)>$")
_ERROR_RE = re.compile(r"^error:\s*(\d+)", re.IGNORECASE)
_ALARM_RE = re.compile(r"^ALARM:\s*(\d+)", re.IGNORECASE)


class FluidNCError(RuntimeError):
    """Driver-level failure."""


class GrblError(FluidNCError):
    """The controller rejected a line with ``error:N``."""

    def __init__(self, code: int, line: str = ""):
        self.code = code
        self.line = line
        text = ERROR_TEXT.get(code, "unknown error")
        super().__init__(f"error:{code} — {text}" + (f" (line: {line!r})" if line else ""))


class GrblAlarm(FluidNCError):
    """The controller entered an ALARM state."""

    def __init__(self, code: int):
        self.code = code
        text = ALARM_TEXT.get(code, "unknown alarm")
        super().__init__(f"ALARM:{code} — {text}")


@dataclass(frozen=True)
class MachineStatus:
    """A parsed ``<...>`` status report."""

    state: str = "Unknown"
    mpos: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0)
    wco: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0)
    feed: float = 0.0
    spindle: float = 0.0
    planner_free: int | None = None
    rx_free: int | None = None
    line_number: int | None = None
    raw: str = ""
    timestamp: float = 0.0

    @property
    def x(self) -> float:
        return self.mpos[0] if len(self.mpos) > 0 else 0.0

    @property
    def y(self) -> float:
        return self.mpos[1] if len(self.mpos) > 1 else 0.0

    @property
    def z(self) -> float:
        return self.mpos[2] if len(self.mpos) > 2 else 0.0

    @property
    def a(self) -> float:
        return self.mpos[3] if len(self.mpos) > 3 else 0.0

    @property
    def xy(self) -> tuple[float, float]:
        return (self.x, self.y)

    @property
    def is_idle(self) -> bool:
        return self.state.lower().startswith("idle")

    @property
    def is_alarm(self) -> bool:
        return self.state.lower().startswith("alarm")

    @property
    def is_hold(self) -> bool:
        return self.state.lower().startswith("hold")

    @property
    def is_moving(self) -> bool:
        return self.state.lower().startswith(("run", "jog", "home"))

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "x": round(self.x, 3),
            "y": round(self.y, 3),
            "z": round(self.z, 3),
            "a": round(self.a, 3),
            "feed": round(self.feed, 1),
            "planner_free": self.planner_free,
            "rx_free": self.rx_free,
            "timestamp": self.timestamp,
        }

    @classmethod
    def parse(cls, text: str) -> "MachineStatus | None":
        m = _STATUS_RE.match(text.strip())
        if not m:
            return None
        state = m.group(1).strip()
        fields = [f for f in m.group(2).split("|") if f]
        mpos: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0)
        wpos: tuple[float, ...] | None = None
        wco: tuple[float, ...] = (0.0, 0.0, 0.0, 0.0)
        feed = spindle = 0.0
        planner_free = rx_free = None
        line_no = None

        def nums(s: str) -> tuple[float, ...]:
            out = []
            for part in s.split(","):
                part = part.strip()
                if not part:
                    continue
                try:
                    out.append(float(part))
                except ValueError:
                    out.append(0.0)
            return tuple(out)

        for field_text in fields:
            key, _, value = field_text.partition(":")
            key = key.strip()
            if key == "MPos":
                mpos = nums(value)
            elif key == "WPos":
                wpos = nums(value)
            elif key == "WCO":
                wco = nums(value)
            elif key == "FS":
                v = nums(value)
                feed = v[0] if v else 0.0
                spindle = v[1] if len(v) > 1 else 0.0
            elif key == "F":
                v = nums(value)
                feed = v[0] if v else 0.0
            elif key == "Bf":
                v = nums(value)
                if v:
                    planner_free = int(v[0])
                if len(v) > 1:
                    rx_free = int(v[1])
            elif key == "Ln":
                v = nums(value)
                line_no = int(v[0]) if v else None

        if wpos is not None and mpos == (0.0, 0.0, 0.0, 0.0):
            n = max(len(wpos), len(wco))
            padded_w = tuple(list(wpos) + [0.0] * (n - len(wpos)))
            padded_o = tuple(list(wco) + [0.0] * (n - len(wco)))
            mpos = tuple(w + o for w, o in zip(padded_w, padded_o))

        if len(mpos) < 4:
            mpos = tuple(list(mpos) + [0.0] * (4 - len(mpos)))
        if len(wco) < 4:
            wco = tuple(list(wco) + [0.0] * (4 - len(wco)))

        return cls(state=state, mpos=mpos, wco=wco, feed=feed, spindle=spindle,
                   planner_free=planner_free, rx_free=rx_free,
                   line_number=line_no, raw=text.strip(), timestamp=time.monotonic())


@dataclass
class _Pending:
    """One line that has been written but not yet acknowledged."""

    nbytes: int
    text: str
    future: asyncio.Future


class FluidNCDriver:
    """Protocol-level driver. Job orchestration lives in :mod:`machine`."""

    def __init__(self, cfg: MachineConfig, transport: Transport):
        self.cfg = cfg
        self.transport = transport
        self.rx_buffer = cfg.connection.serial.rx_buffer_bytes
        self.status: MachineStatus = MachineStatus()
        self.connected = False

        self._pending: deque[_Pending] = deque()
        self._pending_bytes = 0
        self._slot_free = asyncio.Event()
        self._slot_free.set()

        self._reader_task: asyncio.Task | None = None
        self._poller_task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._line_buffer = ""
        self._last_status_time = 0.0
        self._alarm: GrblAlarm | None = None
        self._stop = asyncio.Event()
        self._write_lock = asyncio.Lock()

        # -- observers ------------------------------------------------------
        self.on_status: Callable[[MachineStatus], None] | None = None
        self.on_message: Callable[[str], None] | None = None
        self.on_alarm: Callable[[GrblAlarm], None] | None = None
        self.on_disconnect: Callable[[str], None] | None = None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        self.transport.set_disconnect_handler(self._handle_transport_drop)
        await self.transport.open()
        self.connected = True
        self._stop.clear()
        self._alarm = None
        self._last_status_time = time.monotonic()
        self._reader_task = asyncio.create_task(self._reader_loop(), name="fluidnc-reader")
        self._poller_task = asyncio.create_task(self._poll_loop(), name="fluidnc-poller")
        self._watchdog_task = asyncio.create_task(
            self._watchdog_loop(), name="fluidnc-watchdog")
        # Wait for the first status so callers know the link really works.
        try:
            await self.wait_for_status(timeout=3.0)
        except asyncio.TimeoutError:
            log.warning("no status report within 3 s of connecting")

    async def disconnect(self) -> None:
        self._stop.set()
        self.connected = False
        for task in (self._poller_task, self._watchdog_task, self._reader_task):
            if task is not None:
                task.cancel()
        for task in (self._poller_task, self._watchdog_task, self._reader_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._poller_task = self._watchdog_task = self._reader_task = None
        self._fail_pending(ConnectionLost("driver disconnected"))
        await self.transport.close()

    def _handle_transport_drop(self, reason: str) -> None:
        # The transport and the reader loop both notice a dead link, so this
        # runs twice for one cable pull. Announcing it twice would let the
        # second notification downgrade an E-stop back to "just disconnected".
        if not self.connected:
            log.debug("transport drop reported again: %s", reason)
            return
        log.error("transport lost: %s", reason)
        self.connected = False
        self._fail_pending(ConnectionLost(reason))
        if self.on_disconnect is not None:
            try:
                self.on_disconnect(reason)
            except Exception:  # pragma: no cover
                log.exception("on_disconnect handler raised")

    @staticmethod
    def _mark_retrieved(future: asyncio.Future) -> None:
        """Consume a failed future's exception so asyncio stops complaining.

        When a stream aborts, the futures for lines that were already on the
        wire have nobody left to await them. Reading ``.exception()`` here
        marks them retrieved without hiding anything from a real awaiter.
        """
        if not future.cancelled():
            future.exception()

    def _fail_pending(self, exc: BaseException) -> None:
        while self._pending:
            item = self._pending.popleft()
            if not item.future.done():
                item.future.set_exception(exc)
                item.future.add_done_callback(self._mark_retrieved)
        self._pending_bytes = 0
        self._slot_free.set()

    # ------------------------------------------------------------------
    # reader
    # ------------------------------------------------------------------

    async def _reader_loop(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    chunk = await self.transport.read(timeout=0.5)
                except ConnectionLost as exc:
                    self._handle_transport_drop(str(exc))
                    return
                if not chunk:
                    continue
                self._line_buffer += chunk.decode("ascii", "replace")
                while True:
                    idx = min((i for i in (self._line_buffer.find("\n"),
                                           self._line_buffer.find("\r"))
                               if i >= 0), default=-1)
                    if idx < 0:
                        break
                    line = self._line_buffer[:idx]
                    self._line_buffer = self._line_buffer[idx + 1:]
                    if line.strip():
                        self._handle_line(line.strip())
                if len(self._line_buffer) > 4096:      # runaway garbage
                    self._line_buffer = self._line_buffer[-1024:]
        except asyncio.CancelledError:  # pragma: no cover
            raise

    def _handle_line(self, line: str) -> None:
        if line.startswith("<") and line.endswith(">"):
            status = MachineStatus.parse(line)
            if status is not None:
                self.status = status
                self._last_status_time = time.monotonic()
                if status.is_alarm and self._alarm is None:
                    self._raise_alarm(GrblAlarm(0))
                elif not status.is_alarm:
                    self._alarm = None
                if self.on_status is not None:
                    try:
                        self.on_status(status)
                    except Exception:  # pragma: no cover
                        log.exception("on_status handler raised")
            return

        low = line.lower()
        if low == "ok":
            self._resolve(None)
            return

        m = _ERROR_RE.match(line)
        if m:
            self._resolve(GrblError(int(m.group(1))))
            return

        m = _ALARM_RE.match(line)
        if m:
            self._raise_alarm(GrblAlarm(int(m.group(1))))
            return

        if line.startswith("Grbl") or line.startswith("[MSG:"):
            # A banner means the controller reset under us; nothing that is
            # still pending will ever be acknowledged.
            if line.startswith("Grbl") and self._pending:
                self._fail_pending(FluidNCError(
                    "controller reset while lines were in flight"))
            if self.on_message is not None:
                try:
                    self.on_message(line)
                except Exception:  # pragma: no cover
                    log.exception("on_message handler raised")
            return

        if self.on_message is not None:
            try:
                self.on_message(line)
            except Exception:  # pragma: no cover
                log.exception("on_message handler raised")

    def _raise_alarm(self, alarm: GrblAlarm) -> None:
        self._alarm = alarm
        self._fail_pending(alarm)
        if self.on_alarm is not None:
            try:
                self.on_alarm(alarm)
            except Exception:  # pragma: no cover
                log.exception("on_alarm handler raised")

    def _resolve(self, error: Exception | None) -> None:
        if not self._pending:
            return          # unsolicited ok (e.g. after a reset) — ignore
        item = self._pending.popleft()
        self._pending_bytes -= item.nbytes
        self._slot_free.set()
        if not item.future.done():
            if error is None:
                item.future.set_result(None)
            else:
                if isinstance(error, GrblError):
                    error = GrblError(error.code, item.text)
                item.future.set_exception(error)
                item.future.add_done_callback(self._mark_retrieved)

    # ------------------------------------------------------------------
    # status polling / watchdog
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        interval = 1.0 / max(self.cfg.connection.status_poll_hz, 0.5)
        try:
            while not self._stop.is_set():
                await asyncio.sleep(interval)
                if not self.connected:
                    continue
                try:
                    await self.send_realtime(RT_STATUS)
                except (ConnectionLost, TransportError):
                    return
        except asyncio.CancelledError:  # pragma: no cover
            raise

    async def _watchdog_loop(self) -> None:
        timeout = self.cfg.connection.watchdog_timeout_s
        try:
            while not self._stop.is_set():
                await asyncio.sleep(timeout / 3.0)
                if not self.connected:
                    continue
                silent = time.monotonic() - self._last_status_time
                if silent > timeout:
                    log.error("watchdog: no status for %.1f s — stopping", silent)
                    self.connected = False
                    self._fail_pending(ConnectionLost(
                        f"no status report for {silent:.1f} s"))
                    try:
                        await self.transport.send_bytes(RT_FEED_HOLD)
                    except Exception:
                        pass
                    if self.on_disconnect is not None:
                        try:
                            self.on_disconnect(
                                f"watchdog timeout after {silent:.1f} s")
                        except Exception:  # pragma: no cover
                            log.exception("on_disconnect handler raised")
                    return
        except asyncio.CancelledError:  # pragma: no cover
            raise

    async def wait_for_status(self, timeout: float = 3.0) -> MachineStatus:
        deadline = time.monotonic() + timeout
        await self.send_realtime(RT_STATUS)
        while time.monotonic() < deadline:
            if self.status.timestamp > 0:
                return self.status
            await asyncio.sleep(0.02)
        raise asyncio.TimeoutError("no status report received")

    # ------------------------------------------------------------------
    # sending
    # ------------------------------------------------------------------

    async def send_realtime(self, byte: bytes) -> None:
        """Send a realtime byte, bypassing the line queue."""
        if not self.transport.connected:
            raise ConnectionLost("transport is not connected")
        await self.transport.send_bytes(byte)

    async def send_line(self, line: str, *, timeout: float = 30.0) -> None:
        """Queue one line and await its ``ok``.

        Blocks while the controller's RX buffer has no room, which is exactly
        what character counting is for.
        """
        payload = line.strip()
        if not payload:
            return
        nbytes = len(payload.encode("ascii", "replace")) + 1
        if nbytes > self.rx_buffer:
            raise FluidNCError(
                f"line is {nbytes} bytes, longer than the {self.rx_buffer}-byte "
                f"RX buffer: {payload!r}")

        while True:
            if not self.connected:
                raise ConnectionLost("not connected")
            if self._alarm is not None:
                raise self._alarm
            if self._pending_bytes + nbytes <= self.rx_buffer:
                break
            self._slot_free.clear()
            try:
                await asyncio.wait_for(self._slot_free.wait(), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise FluidNCError(
                    f"timed out waiting for controller buffer space "
                    f"({self._pending_bytes}/{self.rx_buffer} bytes in flight)"
                ) from exc

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        item = _Pending(nbytes=nbytes, text=payload, future=future)
        async with self._write_lock:
            self._pending.append(item)
            self._pending_bytes += nbytes
            try:
                await self.transport.send_bytes(
                    (payload + "\n").encode("ascii", "replace"))
            except Exception:
                if item in self._pending:
                    self._pending.remove(item)
                    self._pending_bytes -= nbytes
                raise
        await asyncio.wait_for(future, timeout=timeout)

    async def send_line_nowait(self, line: str) -> asyncio.Future:
        """Queue a line and return its future without awaiting it.

        This is what the streamer uses: it keeps sending while earlier lines
        are still unacknowledged, which is the whole point of the protocol.
        """
        payload = line.strip()
        loop = asyncio.get_running_loop()
        if not payload:
            fut: asyncio.Future = loop.create_future()
            fut.set_result(None)
            return fut
        nbytes = len(payload.encode("ascii", "replace")) + 1
        if nbytes > self.rx_buffer:
            raise FluidNCError(
                f"line is {nbytes} bytes, longer than the {self.rx_buffer}-byte "
                f"RX buffer: {payload!r}")

        while True:
            if not self.connected:
                raise ConnectionLost("not connected")
            if self._alarm is not None:
                raise self._alarm
            if self._pending_bytes + nbytes <= self.rx_buffer:
                break
            self._slot_free.clear()
            await self._slot_free.wait()

        future = loop.create_future()
        item = _Pending(nbytes=nbytes, text=payload, future=future)
        async with self._write_lock:
            self._pending.append(item)
            self._pending_bytes += nbytes
            try:
                await self.transport.send_bytes(
                    (payload + "\n").encode("ascii", "replace"))
            except Exception:
                if item in self._pending:
                    self._pending.remove(item)
                    self._pending_bytes -= nbytes
                raise
        return future

    async def drain(self, timeout: float = 120.0) -> None:
        """Wait until every sent line has been acknowledged."""
        deadline = time.monotonic() + timeout
        while self._pending:
            if not self.connected:
                raise ConnectionLost("connection lost while draining")
            if time.monotonic() > deadline:
                raise FluidNCError(
                    f"timed out draining {len(self._pending)} unacknowledged line(s)")
            pending = list(self._pending)
            await asyncio.wait([p.future for p in pending],
                               timeout=0.5,
                               return_when=asyncio.ALL_COMPLETED)
            for p in pending:
                if p.future.done() and p.future.exception() is not None:
                    raise p.future.exception()

    async def wait_until_idle(self, timeout: float = 600.0,
                              poll: float = 0.05) -> None:
        """Wait for the controller to report Idle (motion actually finished)."""
        deadline = time.monotonic() + timeout
        # A status report may predate the last queued move; require two
        # consecutive Idle reports separated by a fresh poll.
        idle_streak = 0
        while time.monotonic() < deadline:
            if not self.connected:
                raise ConnectionLost("connection lost while waiting for idle")
            if self._alarm is not None:
                raise self._alarm
            await self.send_realtime(RT_STATUS)
            await asyncio.sleep(poll)
            if self.status.is_idle:
                idle_streak += 1
                if idle_streak >= 2:
                    return
            else:
                idle_streak = 0
        raise FluidNCError(f"machine did not become idle within {timeout} s")

    # ------------------------------------------------------------------
    # streaming
    # ------------------------------------------------------------------

    async def stream(self, lines: Sequence[str], *,
                     on_progress: Callable[[int, int], None] | None = None,
                     pause_event: asyncio.Event | None = None,
                     cancel_event: asyncio.Event | None = None,
                     line_timeout: float = 120.0) -> int:
        """Stream a program with character counting.

        Returns the number of lines that were acknowledged.  ``pause_event``,
        when set, holds off *new* lines (the controller keeps executing what
        it already has — combine with :meth:`feed_hold` for a real pause).
        ``cancel_event`` stops sending; the caller is responsible for flushing
        the controller.
        """
        executable = [ln.strip() for ln in lines
                      if ln.strip() and not ln.strip().startswith(";")]
        total = len(executable)
        futures: list[tuple[int, asyncio.Future]] = []
        sent = 0

        for i, line in enumerate(executable):
            if cancel_event is not None and cancel_event.is_set():
                break
            if pause_event is not None:
                while pause_event.is_set():
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    await asyncio.sleep(0.02)
                if cancel_event is not None and cancel_event.is_set():
                    break
            if self._alarm is not None:
                raise self._alarm
            if not self.connected:
                raise ConnectionLost("connection lost mid-stream")

            future = await self.send_line_nowait(line)
            futures.append((i, future))
            sent += 1

            # Report progress on acknowledged lines, not merely sent ones.
            while futures and futures[0][1].done():
                idx, fut = futures.pop(0)
                exc = fut.exception()
                if exc is not None:
                    raise exc
                if on_progress is not None:
                    on_progress(idx + 1, total)

        for idx, fut in futures:
            try:
                await asyncio.wait_for(fut, timeout=line_timeout)
            except asyncio.TimeoutError as exc:
                raise FluidNCError(
                    f"line {idx + 1}/{total} was never acknowledged") from exc
            if on_progress is not None:
                on_progress(idx + 1, total)
        return sent

    # ------------------------------------------------------------------
    # convenience realtime wrappers
    # ------------------------------------------------------------------

    async def feed_hold(self) -> None:
        await self.send_realtime(RT_FEED_HOLD)

    async def cycle_resume(self) -> None:
        await self.send_realtime(RT_RESUME)

    async def jog_cancel(self) -> None:
        await self.send_realtime(RT_JOG_CANCEL)

    async def soft_reset(self) -> None:
        """Ctrl-X: stop immediately and clear the planner."""
        await self.send_realtime(RT_SOFT_RESET)
        self._fail_pending(FluidNCError("soft reset issued"))
        self._line_buffer = ""
        await asyncio.sleep(0.05)

    async def unlock(self) -> None:
        """``$X`` — clear an alarm lock."""
        self._alarm = None
        await self.send_line("$X", timeout=5.0)

    async def home(self) -> None:
        await self.send_line("$H", timeout=120.0)

    async def jog_to(self, x: float, y: float, feed_mm_min: float) -> None:
        """Issue an absolute jog. Cancel it with :meth:`jog_cancel`."""
        await self.send_line_nowait(
            f"$J=G90 G21 X{x:.3f} Y{y:.3f} F{feed_mm_min:.0f}")

    @property
    def alarm(self) -> GrblAlarm | None:
        return self._alarm

    @property
    def pending_lines(self) -> int:
        return len(self._pending)

    @property
    def pending_bytes(self) -> int:
        return self._pending_bytes
