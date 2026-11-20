"""Connection core of the FluidNC client: state, reader thread, write lock, line handling.

See ``client.py`` for the threading model and the public API."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable

from . import protocol as proto
from .client_types import (BootInfo, ClientError, ClientState, LineError, LinkEvent, Reply,
                           StreamActiveError, Traffic)
from .link import Link, LinkError

log = logging.getLogger(__name__)

_ABANDON_TTL_S = 60.0       # an unanswered, given-up-on line stops being counted after this


class _Aborted(Exception):
    """Internal: a reset/e-stop happened between reserving and writing a line."""


class _Item:
    """One sent-but-unacknowledged line."""
    __slots__ = ("text", "nbytes", "kind", "event", "status", "code", "err", "lines",
                 "collect", "abandoned", "t_sent", "stream")

    def __init__(self, text: str, nbytes: int, kind: str, *, collect: bool = False,
                 stream: "_StreamState | None" = None, wait: bool = False):
        self.text = text
        self.nbytes = nbytes
        self.kind = kind                      # "send" | "stream"
        self.event = threading.Event() if wait else None
        self.status: str | None = None
        self.code: int | None = None
        self.err = ""
        self.lines: list[str] = []
        self.collect = collect
        self.abandoned = False
        self.t_sent = time.monotonic()
        self.stream = stream

    def finish(self, status: str, code: int | None = None, err: str = "") -> None:
        self.status, self.code, self.err = status, code, err
        if self.event is not None:
            self.event.set()

    def reply(self) -> Reply:
        return Reply(ok=self.status == "ok", status=self.status or "timeout", line=self.text,
                     code=self.code, text=self.err, lines=tuple(self.lines))


class _StreamState:
    def __init__(self, total: int, abort0: int, reset0: int, alarm0: int):
        self.total = total
        self.acked = 0
        self.errors: list[LineError] = []
        self.abort0, self.reset0, self.alarm0 = abort0, reset0, alarm0
        self.ending = False
        self.lines: list[str] = []

class ClientBase:
    def __init__(self, link: Link, *, rx_buffer_bytes: int = 127, status_hz: float = 10.0,
                 listener: Callable[[object], None] | None = None,
                 watchdog_s: float = 5.0, ack_timeout_s: float = 120.0):
        self._link = link
        self._rx = int(rx_buffer_bytes)
        self._poll_period = 1.0 / status_hz if status_hz > 0 else 0.0
        self._listener = listener
        self._watchdog_s = watchdog_s
        self._ack_timeout_s = ack_timeout_s
        self.polling = True               # False: no '?' polling and no watchdog (e.g. XMODEM)

        self._cv = threading.Condition(threading.Lock())
        self._wlock = threading.Lock()
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._gen = 0                     # connection generation
        self._closing = False

        self._pending: deque[_Item] = deque()
        self._inflight = 0
        self._busy: str | None = None     # None | "send" | "stream"
        self._stream: _StreamState | None = None
        self._abort_gen = 0               # bumped by emergency_stop()
        self._reset_gen = 0               # bumped by every reset (ours or unexpected)

        self._reset_state()

    # ------------------------------------------------------------------ state ----
    def _reset_state(self) -> None:
        self._link_state = "closed"
        self._link_error: str | None = None
        self._machine_state = "Unknown"
        self._status: proto.Status | None = None
        self._status_t = 0.0
        self._alarm: tuple[int, str] | None = None
        self._alarm_seq = 0
        self._last_error: str | None = None
        self._firmware: str | None = None
        self._banner: str | None = None
        self._banner_count = 0
        self._last_banner_t = 0.0
        self._resetting = False
        self._connecting = False
        self._reset_banner_target = 0
        self._reset_sent_t = 0.0
        self._last_ack_t = 0.0
        self._last_rx_t = 0.0
        self._recent: deque[str] = deque(maxlen=500)

    @property
    def link(self) -> Link:
        return self._link

    @property
    def state(self) -> ClientState:
        with self._cv:
            st = self._status
            alarm = self._alarm
            if alarm is None and self._machine_state == "Alarm":
                alarm = (0, "Alarm active (code unknown); unlock with $X")
            return ClientState(
                link=self._link_state, machine_state=self._machine_state,
                substate=st.substate if st else None, mpos=st.mpos if st else None,
                wpos=st.wpos if st else None, wco=st.wco if st else None,
                feed=st.feed if st else None, alarm=alarm, last_error=self._last_error,
                last_status_age_s=(time.monotonic() - self._status_t) if self._status_t else None,
                planner_free=st.planner_free if st else None, rx_free=st.rx_free if st else None,
                firmware=self._firmware, resetting=self._resetting,
                streaming=self._busy == "stream", in_flight_bytes=self._inflight,
                link_error=self._link_error)

    def recent_lines(self, n: int = 100) -> list[str]:
        """Last non-status lines received since the connection was opened (boot log etc.)."""
        with self._cv:
            return list(self._recent)[-n:]

    def _emit(self, event: object) -> None:
        cb = self._listener
        if cb is None:
            return
        try:
            cb(event)
        except Exception:  # noqa: BLE001 - a listener must never break the reader
            log.exception("listener raised")

    # ------------------------------------------------------------- connection ----
    def connect(self, timeout: float = 5.0) -> BootInfo:
        with self._cv:
            if self._link_state == "open":
                raise ClientError("already connected")
            self._reset_state()
            self._pending.clear()
            self._inflight = 0
            self._closing = False
            self._gen += 1
            gen = self._gen
        self._connecting = True                # banners during connect are expected (DTR reset)
        self._link.open()                      # LinkError propagates
        t0 = time.monotonic()
        with self._cv:
            self._link_state = "open"
            self._last_rx_t = t0
        self._stop = threading.Event()
        self._reader = threading.Thread(target=self._reader_main, args=(gen, self._stop),
                                        name=f"fluidnc-reader-{self._link.name}", daemon=True)
        self._reader.start()
        self._emit(LinkEvent("open"))

        deadline = t0 + timeout
        ready = False
        with self._cv:
            while time.monotonic() < deadline and self._link_state == "open":
                if self._status_t > t0 and self._status_t >= self._last_banner_t:
                    # A late banner (DTR-triggered reset) can still follow the first report.
                    banner_n = self._banner_count
                    self._cv.wait(0.3)
                    if self._banner_count == banner_n and self._status_t >= self._last_banner_t:
                        ready = True
                        break
                    continue
                self._cv.wait(0.05)
        if not ready:
            with self._cv:
                self._connecting = False
                reason = self._link_error
            self.close()
            raise LinkError(reason or f"no status reply from the controller within {timeout:.1f}s "
                                      "(wrong port/baud rate, or not FluidNC?)")
        ver_line = None
        rep = self.query_reply("$I", timeout=2.0)
        for ln in rep.lines:
            v = proto.parse_ver_line(ln)
            if v:
                ver_line = v
        with self._cv:
            self._connecting = False
            if self._firmware is None and ver_line:
                self._firmware = ver_line
            alarm = self._alarm
            if alarm is None and self._machine_state == "Alarm":
                alarm = (0, "Alarm active (code unknown); unlock with $X")
            return BootInfo(firmware=self._firmware, banner=self._banner,
                            machine_state=self._machine_state, alarm=alarm,
                            mpos=self._status.mpos if self._status else None, ver_line=ver_line,
                            boot_lines=tuple(self._recent))

    def close(self) -> None:
        with self._cv:
            was = self._link_state
            self._closing = True
            self._link_state = "closed"
            self._fail_all_locked("closed", "client closed")
            self._cv.notify_all()
        self._stop.set()
        try:
            self._link.close()
        except Exception:  # noqa: BLE001
            log.debug("link close failed", exc_info=True)
        t = self._reader
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)
        if was != "closed":
            self._emit(LinkEvent("closed"))

    def _lost(self, reason: str, gen: int) -> None:
        with self._cv:
            if gen != self._gen or self._link_state != "open":
                return
            self._link_state = "lost"
            self._link_error = reason
            self._last_error = f"Link lost: {reason}"
            self._fail_all_locked("lost", reason)
            self._cv.notify_all()
        log.warning("link lost: %s", reason)
        self._stop.set()
        try:
            self._link.close()
        except Exception:  # noqa: BLE001
            log.debug("link close after loss failed", exc_info=True)
        self._emit(LinkEvent("lost", reason))

    def _fail_all_locked(self, status: str, reason: str) -> None:
        for item in self._pending:
            if item.status is None:
                item.finish(status, None, reason)
        self._pending.clear()
        self._inflight = 0

    # ------------------------------------------------------------------ write ----
    def _write(self, data: bytes, *, reset_gen: int | None = None, lock_timeout: float = 2.0) -> None:
        """Write under the single write lock. ``reset_gen``: refuse (``_Aborted``) when a reset
        or e-stop happened since the caller reserved the line."""
        if self._link_state != "open":
            raise LinkError(f"link is {self._link_state}")
        gen = self._gen
        if not self._wlock.acquire(timeout=lock_timeout):
            err = LinkError("write lock timeout (link stuck)")
            self._lost(str(err), gen)
            raise err
        failure: LinkError | None = None
        try:
            if reset_gen is not None and reset_gen != self._reset_gen:
                raise _Aborted()
            self._link.write(data)
        except LinkError as exc:
            failure = exc
        finally:
            self._wlock.release()
        if failure is not None:
            self._lost(str(failure), gen)
            raise failure

    # ----------------------------------------------------------------- reader ----
    def _reader_main(self, gen: int, stop: threading.Event) -> None:
        buf = bytearray()
        next_poll = 0.0
        while not stop.is_set():
            if self.polling and self._poll_period and time.monotonic() >= next_poll:
                next_poll = time.monotonic() + self._poll_period
                try:
                    self._write(proto.RT_STATUS, lock_timeout=0.5)
                except (LinkError, _Aborted):
                    return
            try:
                data = self._link.read(0.05)
            except LinkError as exc:
                if not stop.is_set() and not self._closing:
                    self._lost(str(exc), gen)
                return
            except Exception as exc:  # noqa: BLE001
                if not stop.is_set() and not self._closing:
                    self._lost(f"read failed: {exc}", gen)
                return
            now = time.monotonic()
            if data:
                self._last_rx_t = now
                buf.extend(data)
                while True:
                    i = buf.find(b"\n")
                    if i < 0:
                        break
                    raw = bytes(buf[:i])
                    del buf[:i + 1]
                    self._handle_line(raw.decode("utf-8", errors="replace").strip("\r \t"))
                if len(buf) > 4096:           # no newline in 4 KiB: garbage, keep memory bounded
                    del buf[:]
            if self.polling and self._watchdog_s and now - self._last_rx_t > self._watchdog_s:
                self._lost(f"no data from the controller for {self._watchdog_s:.0f}s", gen)
                return
            self._expire_abandoned(now)

    def _expire_abandoned(self, now: float) -> None:
        with self._cv:
            if not self._pending:
                return
            head = self._pending[0]
            if head.abandoned and now - head.t_sent > _ABANDON_TTL_S:
                self._pending.popleft()
                self._inflight -= head.nbytes
                log.warning("dropping unanswered line %r", head.text)
                self._cv.notify_all()

    def _handle_line(self, line: str) -> None:
        ev = proto.parse_line(line)
        if ev is None:
            return
        emit: list[object] = []
        now = time.monotonic()
        with self._cv:
            if isinstance(ev, proto.Status):
                self._on_status(ev, now)
                emit.append(ev)
            elif isinstance(ev, (proto.Ok, proto.Error)):
                self._on_ack(ev, now)
                if isinstance(ev, proto.Error):
                    emit.append(Traffic("rx", line))
            elif isinstance(ev, proto.Alarm):
                self._alarm = (ev.code, ev.text)
                self._alarm_seq += 1
                self._machine_state = "Alarm"
                self._last_error = f"ALARM:{ev.code} {ev.text}"
                self._recent.append(line)
                emit += [ev, Traffic("rx", line)]
            elif isinstance(ev, proto.Banner):
                self._on_banner(ev, now)
                self._recent.append(line)
                emit += [ev, Traffic("rx", line)]
            else:                              # Message / Reply
                self._recent.append(line)
                if self._pending and self._pending[0].collect:
                    self._pending[0].lines.append(line)
                if isinstance(ev, proto.Message):
                    if ev.kind in ("ERR", "ERROR"):
                        self._last_error = ev.text
                    emit.append(ev)
                emit.append(Traffic("rx", line))
            self._cv.notify_all()
        for e in emit:
            self._emit(e)

    def _on_status(self, st: proto.Status, now: float) -> None:
        self._status, self._status_t = st, now
        self._machine_state = st.state
        if st.state != "Alarm":
            self._alarm = None
        if self._resetting and (self._banner_count >= self._reset_banner_target
                                or now - self._reset_sent_t > 10.0):
            self._resetting = False

    def _on_ack(self, ev: proto.Ok | proto.Error, now: float) -> None:
        if not self._pending:
            log.debug("unsolicited %s ignored", type(ev).__name__)
            return
        item = self._pending.popleft()
        self._inflight -= item.nbytes
        self._last_ack_t = now
        if isinstance(ev, proto.Ok):
            item.finish("ok")
        else:
            item.finish("error", ev.code, ev.text)
            if not item.abandoned:
                self._last_error = f"error:{ev.code} {ev.text} (line: {item.text})"
        st = item.stream
        if st is not None:
            st.acked += 1
            if isinstance(ev, proto.Error):
                idx = st.acked - 1
                st.errors.append(LineError(idx, item.text, ev.code, ev.text))

    def _on_banner(self, ev: proto.Banner, now: float) -> None:
        self._banner = ev.text
        if ev.version:
            self._firmware = ev.version
        self._banner_count += 1
        self._last_banner_t = now
        if not self._resetting and not self._connecting:
            self._reset_gen += 1
            self._last_error = "controller restarted unexpectedly"
        self._fail_all_locked("reset", "controller restarted")
        self._machine_state = "Unknown"       # until the next status report
        self._alarm = None

    # --------------------------------------------------------- busy gate (cv) ----
    def _stream_ending_locked(self) -> bool:
        st = self._stream
        return (st is not None and (st.ending or st.abort0 != self._abort_gen
                                    or st.reset0 != self._reset_gen or self._link_state != "open"))

    def _acquire_busy(self, kind: str, deadline: float) -> bool:
        with self._cv:
            while True:
                if self._busy is None:
                    self._busy = kind
                    return True
                if self._busy == "stream" and not self._stream_ending_locked():
                    raise StreamActiveError("a stream is running; use the realtime commands or cancel it")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cv.wait(min(remaining, 0.05))

    def _release_busy(self) -> None:
        with self._cv:
            self._busy = None
            self._cv.notify_all()

    @staticmethod
    def _prepare(line: str, rx: int) -> tuple[str, int]:
        text = line.strip()
        if "\n" in text or "\r" in text:
            raise ValueError("a G-code line must not contain a line break")
        n = len(text.encode("utf-8")) + 1
        if n > rx:
            raise ValueError(f"line too long for the RX buffer ({n} > {rx} bytes): {text[:40]!r}...")
        return text, n
