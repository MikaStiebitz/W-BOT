"""Streaming part of the FluidNC client (character counting, cancel, drain)."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Iterable

from . import protocol as proto
from .client_core import ClientBase, _Aborted, _Item, _StreamState
from .client_types import StreamActiveError, StreamResult, Traffic
from .link import LinkError

log = logging.getLogger(__name__)


class StreamMixin(ClientBase):
    # --------------------------------------------------------------- stream ------
    def stream(self, lines: Iterable[str], *, on_progress: Callable[[int, int], None] | None = None,
               cancel: threading.Event | None = None, stop_on_error: bool = True,
               ack_timeout: float | None = None) -> StreamResult:
        """Stream lines with character counting. Blank lines are skipped. ``on_progress(acked,
        total)`` is called from the calling thread whenever acknowledgements advanced.
        ``cancel`` set -> feed hold, wait for Hold, soft reset (planner flush), wait for the
        banner; result status ``"cancelled"``. Other stop reasons: ``"error"`` (line rejected,
        ``stop_on_error``), ``"alarm"``, ``"lost"``, ``"reset"``, ``"estop"``, ``"timeout"``."""
        prepared = [self._prepare(ln, self._rx) for ln in lines]
        prepared = [p for p in prepared if p[0]]
        total = len(prepared)
        if total == 0:
            return StreamResult("done", 0, 0, 0, final_state=self._machine_state)
        if not self._acquire_busy("stream", time.monotonic() + 5.0):
            raise StreamActiveError("another command is in progress")
        try:
            with self._cv:
                st = _StreamState(total, self._abort_gen, self._reset_gen, self._alarm_seq)
                self._stream = st
            return self._run_stream(prepared, st, on_progress, cancel, stop_on_error,
                                    ack_timeout if ack_timeout is not None else self._ack_timeout_s)
        finally:
            with self._cv:
                st.ending = True
                self._stream = None
                for item in self._pending:        # unanswered leftovers: keep counting, drop link to stream
                    if item.stream is st:
                        item.stream = None
                        item.abandoned = True
                self._busy = None
                self._cv.notify_all()

    def _stop_reason_locked(self, st: _StreamState) -> str | None:
        if self._link_state != "open":
            return "lost"
        if self._abort_gen != st.abort0:
            return "estop"
        if self._reset_gen != st.reset0:
            return "reset"
        if self._alarm_seq != st.alarm0 or self._machine_state == "Alarm":
            return "alarm"
        return None

    def _run_stream(self, prepared: list[tuple[str, int]], st: _StreamState, on_progress,
                    cancel, stop_on_error: bool, ack_timeout: float) -> StreamResult:
        total = st.total
        sent = 0
        reported = 0
        status = "done"
        message = ""
        last_move = time.monotonic()
        last_acked = 0
        while True:
            if cancel is not None and cancel.is_set():
                status = "cancelled"
                break
            batch: list[_Item] = []
            with self._cv:
                reason = self._stop_reason_locked(st)
                if reason:
                    status = reason
                    break
                if st.errors and stop_on_error:
                    status = "error"
                    break
                if st.acked >= total:
                    break
                now = time.monotonic()
                if st.acked != last_acked or self._machine_state in ("Hold", "Door"):
                    last_acked, last_move = st.acked, now
                elif now - last_move > ack_timeout and self._pending:
                    status, message = "timeout", f"no acknowledgement for {ack_timeout:.0f}s"
                    break
                free = self._rx - self._inflight
                idx = sent
                while idx < total and prepared[idx][1] <= free:
                    text, n = prepared[idx]
                    item = _Item(text, n, "stream", stream=st)
                    self._pending.append(item)
                    self._inflight += n
                    free -= n
                    batch.append(item)
                    idx += 1
                if not batch and st.acked == reported:
                    self._cv.wait(0.05)
                acked_now = st.acked
            for item in batch:
                try:
                    self._write((item.text + "\n").encode("utf-8"), reset_gen=st.reset0)
                except _Aborted:
                    break                          # next loop iteration reports estop/reset
                except LinkError:
                    break                          # reader/_lost state is set; loop reports "lost"
                sent += 1
                self._emit(Traffic("tx", item.text))
            if acked_now != reported and on_progress is not None:
                reported = acked_now
                try:
                    on_progress(reported, total)
                except Exception:  # noqa: BLE001
                    log.exception("on_progress raised")
            reported = acked_now

        with self._cv:
            st.ending = True
        if status == "cancelled":
            self._cancel_sequence()
        elif status in ("error", "alarm"):
            self._drain(st, sent, 2.0)
        with self._cv:
            acked = st.acked
            errors = tuple(st.errors)
            final = self._machine_state
        if on_progress is not None and acked != reported:
            try:
                on_progress(acked, total)
            except Exception:  # noqa: BLE001
                log.exception("on_progress raised")
        if status == "done" and errors:
            status = "error"
        if status == "error" and not message and errors:
            e = errors[0]
            message = f"line {e.index + 1}: error:{e.code} {e.text} ({e.line})"
        elif status == "cancelled":
            message = f"cancelled; {acked}/{total} lines were acknowledged, planner flushed"
        elif status == "alarm":
            alarm = self.state.alarm
            message = f"ALARM:{alarm[0]} {alarm[1]}" if alarm else "alarm"
        return StreamResult(status, total, sent, acked, errors, message, final)

    def _drain(self, st: _StreamState, sent: int, timeout: float) -> None:
        """After a stop without reset: give the already-sent lines a moment to be acknowledged."""
        end = time.monotonic() + timeout
        with self._cv:
            while st.acked < sent and self._link_state == "open" and time.monotonic() < end:
                self._cv.wait(0.05)

    def _cancel_sequence(self, hold_timeout: float = 5.0, reset_timeout: float = 5.0) -> None:
        """Feed hold -> wait for Hold:0 -> soft reset (flushes the planner) -> wait for banner."""
        t0 = time.monotonic()
        try:
            self._rt(proto.RT_FEED_HOLD)
        except (LinkError, _Aborted):
            return
        end = t0 + hold_timeout
        with self._cv:
            while time.monotonic() < end and self._link_state == "open":
                s = self._machine_state
                if self._status_t > t0 and ((s == "Hold" and self._status and self._status.substate == 0)
                                            or s in ("Idle", "Alarm")):
                    break
                self._cv.release()
                try:
                    self._rt(proto.RT_STATUS)
                except (LinkError, _Aborted):
                    return
                finally:
                    self._cv.acquire()
                self._cv.wait(0.05)
            if self._link_state != "open":
                return
            self._mark_reset_locked()
        try:
            self._rt(proto.RT_SOFT_RESET)
        except (LinkError, _Aborted):
            return
        self.wait_ready(reset_timeout)
