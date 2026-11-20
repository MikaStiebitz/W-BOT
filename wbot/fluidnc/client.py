"""Streaming client for FluidNC over any :class:`~wbot.fluidnc.link.Link`.

Threading model
---------------
* ONE reader thread per connection: reads bytes, splits lines, updates the state, wakes
  waiters, polls ``?`` at ``status_hz`` and runs the watchdog.
* ONE write lock (``_wlock``): every byte that goes out (lines and realtime bytes) passes it,
  because pyserial on Windows is not thread-safe (DECISIONS D-037).
* ONE condition variable (``_cv``) guards all mutable state. Nothing blocks while holding it
  except ``cv.wait``; listener callbacks and link I/O happen outside of it.
* Every blocking call has a timeout. Link loss / reset / emergency stop fail *all* waiters.

Character counting: the sum of ``len(line)+1`` over sent-but-unacknowledged lines never
exceeds ``rx_buffer_bytes``; each ``ok``/``error:N`` acknowledges the oldest line.
Realtime bytes (``? ! ~ 0x18 0x85``) are never part of that accounting.
"""
from __future__ import annotations

import logging
import threading
import time

from . import protocol as proto
from .client_core import _Aborted, _Item
from .client_stream import StreamMixin
from .client_types import (BootInfo, ClientError, ClientState, CommandFailed, LineError, LinkEvent,
                           Reply, StreamActiveError, StreamResult, Traffic)
from .link import LinkError

log = logging.getLogger(__name__)

__all__ = ["FluidNCClient", "BootInfo", "ClientError", "ClientState", "CommandFailed", "LineError",
           "LinkEvent", "Reply", "StreamActiveError", "StreamResult", "Traffic"]


class FluidNCClient(StreamMixin):
    # ---------------------------------------------------------------- send -------
    def send(self, line: str, timeout: float = 10.0, *, _collect: bool = False) -> Reply:
        """Send one line and block until ``ok``/``error:N``. Never raises for controller or link
        failures (see ``Reply.status``); raises ``StreamActiveError`` during a stream and
        ``ValueError`` for an unsendable line."""
        text, n = self._prepare(line, self._rx)
        if not text:
            return Reply(True, "ok", "")
        deadline = time.monotonic() + timeout
        if not self._acquire_busy("send", deadline):
            return Reply(False, "timeout", text, text="busy: another command is in progress")
        try:
            item = _Item(text, n, "send", collect=_collect, wait=True)
            with self._cv:
                while True:
                    if self._link_state != "open":
                        return Reply(False, "lost" if self._link_state == "lost" else "closed", text,
                                     text=self._link_error or "link not open")
                    if self._inflight + n <= self._rx:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return Reply(False, "timeout", text, text="RX buffer still occupied")
                    self._cv.wait(min(remaining, 0.05))
                rg = self._reset_gen
                item.t_sent = time.monotonic()
                self._pending.append(item)
                self._inflight += n
            try:
                self._write((text + "\n").encode("utf-8"), reset_gen=rg)
            except _Aborted:
                return Reply(False, "estop", text, text="reset/e-stop while sending")
            except LinkError as exc:
                return Reply(False, "lost", text, text=str(exc))
            self._emit(Traffic("tx", text))
            if not item.event.wait(max(0.0, deadline - time.monotonic())):
                with self._cv:
                    if item.status is None:
                        item.abandoned = True
                        return Reply(False, "timeout", text, text="no reply from the controller",
                                     lines=tuple(item.lines))
            return item.reply()
        finally:
            self._release_busy()

    def query(self, line: str, timeout: float = 5.0) -> list[str]:
        """Send a ``$`` query and return the payload lines received before ``ok``.
        Raises :class:`CommandFailed` on error/timeout/link loss."""
        rep = self.query_reply(line, timeout)
        if not rep.ok:
            raise CommandFailed(rep)
        return list(rep.lines)

    def query_reply(self, line: str, timeout: float = 5.0) -> Reply:
        return self.send(line, timeout, _collect=True)

    # -------------------------------------------------------------- realtime -----
    def _rt(self, data: bytes) -> None:
        self._write(data, lock_timeout=1.0)

    def feed_hold(self) -> None:
        self._rt(proto.RT_FEED_HOLD)

    def cycle_start(self) -> None:
        self._rt(proto.RT_CYCLE_START)

    def jog_cancel(self) -> None:
        self._rt(proto.RT_JOG_CANCEL)

    def _mark_reset_locked(self) -> None:
        self._reset_gen += 1
        self._resetting = True
        self._reset_banner_target = self._banner_count + 1
        self._reset_sent_t = time.monotonic()
        self._fail_all_locked("reset", "soft reset")
        self._cv.notify_all()

    def soft_reset(self) -> None:
        """Ctrl-X. Fails everything in flight immediately; the banner follows in ~1.5 s
        (use :meth:`wait_ready`)."""
        with self._cv:
            self._mark_reset_locked()
        self._rt(proto.RT_SOFT_RESET)

    def reset(self, timeout: float = 5.0) -> bool:
        self.soft_reset()
        return self.wait_ready(timeout)

    def emergency_stop(self) -> bool:
        """Feed hold + soft reset, immediately. Frees every thread blocked in ``stream()``,
        ``send()`` or ``wait_idle()`` and returns without waiting for the reboot. Returns
        whether the stop bytes could be written."""
        with self._cv:
            self._abort_gen += 1
            self._mark_reset_locked()
            self._cv.notify_all()
        try:
            self._write(proto.RT_FEED_HOLD + proto.RT_SOFT_RESET, lock_timeout=1.0)
        except (LinkError, _Aborted) as exc:
            log.error("emergency stop could not be written: %s", exc)
            return False
        return True

    def request_status(self, timeout: float = 1.0) -> proto.Status | None:
        """Ask for a status report and wait for one newer than the request."""
        t0 = time.monotonic()
        try:
            self._rt(proto.RT_STATUS)
        except (LinkError, _Aborted):
            return None
        deadline = t0 + timeout
        with self._cv:
            while self._status_t <= t0:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._link_state != "open":
                    return None
                self._cv.wait(min(remaining, 0.05))
            return self._status

    def wait_ready(self, timeout: float = 5.0) -> bool:
        """After a reset: wait for the boot banner and the first status report after it."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._resetting or self._machine_state == "Unknown":
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._link_state != "open":
                    return False
                self._cv.wait(min(remaining, 0.05))
            return self._link_state == "open"

    def unlock(self, timeout: float = 10.0) -> bool:
        """``$X`` and verify by status (the receipt can be lost to the boot banner race,
        DECISIONS D-046). True when the machine is no longer in Alarm."""
        deadline = time.monotonic() + timeout
        for _ in range(3):
            if time.monotonic() >= deadline or self._link_state != "open":
                break
            if not self.wait_ready(min(5.0, max(0.1, deadline - time.monotonic()))):
                continue
            st = self.request_status(1.0)
            if st is None:
                continue
            if st.state != "Alarm":
                return True
            try:
                self.send("$X", timeout=min(3.0, max(0.5, deadline - time.monotonic())))
            except ClientError:
                return False
            end = time.monotonic() + 2.0
            while time.monotonic() < end:
                st = self.request_status(0.5)
                if st is not None and st.state != "Alarm":
                    return True
                if self._link_state != "open":
                    return False
        return self._machine_state not in ("Alarm", "Unknown") and self._link_state == "open"

    # ------------------------------------------------------------- wait_idle -----
    def wait_idle(self, timeout: float = 60.0, *, cancel: threading.Event | None = None) -> bool:
        """True when idle AND every line is acknowledged AND a status newer than the last ack
        says Idle. False on timeout, cancel, alarm, reset, e-stop or link loss."""
        deadline = time.monotonic() + timeout
        with self._cv:
            abort0, reset0 = self._abort_gen, self._reset_gen
            last_poll = 0.0
            while True:
                if self._link_state != "open" or self._abort_gen != abort0 or self._reset_gen != reset0:
                    return False
                if cancel is not None and cancel.is_set():
                    return False
                if self._machine_state == "Alarm":
                    return False
                if (not self._pending and self._machine_state == "Idle"
                        and self._status_t > self._last_ack_t):
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                now = time.monotonic()
                if not self.polling and now - last_poll > 0.1:
                    last_poll = now
                    self._cv.release()
                    try:
                        self._rt(proto.RT_STATUS)
                    except (LinkError, _Aborted):
                        pass
                    finally:
                        self._cv.acquire()
                    continue
                self._cv.wait(min(remaining, 0.05))
