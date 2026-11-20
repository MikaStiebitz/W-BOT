"""G-code interpretation of the emulated firmware (modal state, blocks, dwell, jog)."""
from __future__ import annotations

import math

from .gcode import (ERR_INVALID_JOG, ERR_INVALID_TARGET, ERR_LOCKED, ERR_NEGATIVE, ERR_NO_AXIS_WORDS,
                    ERR_NO_FEED, ERR_NOT_IDLE, ERR_VALUE_MISSING, GcodeError, ParsedLine, clean_line,
                    parse_line)
from .planner import Block, make_block

AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2, "A": 3}
SERVO_MAX_RATE = 600000.0      # mm/min, as in the firmware config of the servo axes
SERVO_ACCEL = 1_000_000.0      # mm/s^2
_LIMIT = 1e6


class Pending:
    """A line that has been parsed but whose ``ok`` is still withheld (planner full / dwell)."""
    __slots__ = ("kind", "blk", "seconds", "end", "stage")

    def __init__(self, kind: str, blk: Block | None = None, seconds: float = 0.0):
        self.kind, self.blk, self.seconds, self.end, self.stage = kind, blk, seconds, 0.0, 0


class GcodeMixin:
    """Needs: planner, fw, cfg, _pos, _offset, _m_* modal fields, _pending, _t, _emit/_ok/_error."""

    # --- entry ---------------------------------------------------------------
    def _gcode(self, text: str) -> None:
        if self._state_str() in ("Alarm", "Jog"):
            self._error(ERR_LOCKED)
            return
        try:
            self._run_line(parse_line(text, axes=self._axes_letters))
        except GcodeError as exc:
            self._error(exc.code)

    def _run_line(self, ln: ParsedLine) -> None:
        g, w = ln.g, ln.words
        scale, absmode, motion, feed = self._m_scale, self._m_abs, self._m_motion, self._m_feed
        if 20 in g:
            scale = 25.4
        if 21 in g:
            scale = 1.0
        if 90 in g:
            absmode = True
        if 91 in g:
            absmode = False
        if 0 in g:
            motion = 0
        if 1 in g:
            motion = 1
        if "F" in w:
            if w["F"] < 0:
                raise GcodeError(ERR_NEGATIVE)
            feed = w["F"] * scale
        axis_words = [a for a in "XYZA" if a in w]
        pos = self._pos
        new_offset = None
        if 92 in g or 92.1 in g:
            new_offset = [0.0] * 4
            if 92 in g:
                if not axis_words:
                    raise GcodeError(ERR_NO_AXIS_WORDS)
                new_offset = list(self._offset)
                for a in axis_words:
                    i = AXIS_INDEX[a]
                    new_offset[i] = pos[i] - w[a] * scale
        dwell = blk = target = None
        if 4 in g:
            if "P" not in w:
                raise GcodeError(ERR_VALUE_MISSING)
            if w["P"] < 0:
                raise GcodeError(ERR_NEGATIVE)
            dwell = w["P"]
        elif axis_words and new_offset is None:
            target = self._target(pos, w, axis_words, scale, absmode)
            if motion == 1 and feed <= 0:
                raise GcodeError(ERR_NO_FEED)
            blk = self._block(pos, target, None if motion == 0 else feed)
        # commit (nothing above changed state)
        self._m_scale, self._m_abs, self._m_motion, self._m_feed = scale, absmode, motion, feed
        if new_offset is not None:
            self._offset = new_offset
        if target is not None:
            self._pos = target
        if dwell:
            self._pending = Pending("dwell", seconds=dwell)
        elif blk is not None:
            self._pending = Pending("block", blk=blk)
        else:
            self._ok()

    def _target(self, pos, words, axis_words, scale: float, absmode: bool) -> list[float]:
        target = list(pos)
        for a in axis_words:
            i = AXIS_INDEX[a]
            v = words[a] * scale
            target[i] = v + self._offset[i] if absmode else pos[i] + v
            if not math.isfinite(target[i]) or abs(target[i]) > _LIMIT:
                raise GcodeError(ERR_INVALID_TARGET)
        return target

    def _block(self, pos, target, feed_mm_min: float | None, jog: bool = False) -> Block | None:
        """Block for a move; ``feed_mm_min`` None = rapid (G0)."""
        xy = math.hypot(target[0] - pos[0], target[1] - pos[1]) > 1e-9
        cap = self.cfg.firmware.max_rate_mm_min if xy else SERVO_MAX_RATE
        rate = cap if feed_mm_min is None else min(feed_mm_min, cap)
        accel = self.cfg.motion.acceleration_mm_s2 if xy else SERVO_ACCEL
        return make_block(pos, target, rate / 60.0, accel, self.fw, self.cfg.firmware.segment_length_mm, jog)

    # --- jog -------------------------------------------------------------------
    def _do_jog(self, body: str) -> None:
        state = self._state_str()
        if state == "Alarm":
            self._error(ERR_LOCKED)
            return
        if state not in ("Idle", "Jog"):
            self._error(ERR_NOT_IDLE)
            return
        try:
            ln = parse_line(clean_line(body), axes=self._axes_letters)
            if ln.m or any(x not in (20, 21, 90, 91, 94) for x in ln.g):
                raise GcodeError(ERR_INVALID_JOG)
            axis_words = [a for a in "XYZA" if a in ln.words]
            if not axis_words:
                raise GcodeError(ERR_INVALID_JOG)
            scale = 25.4 if 20 in ln.g else 1.0
            absmode = self._m_abs if not (90 in ln.g or 91 in ln.g) else (90 in ln.g)
            if ln.words.get("F", 0.0) <= 0:
                raise GcodeError(ERR_NO_FEED)
            target = self._target(self._pos, ln.words, axis_words, scale, absmode)
            blk = self._block(self._pos, target, ln.words["F"] * scale, jog=True)
        except GcodeError as exc:
            self._error(exc.code)
            return
        self._pos = target            # chained relative jogs accumulate; resynced on cancel/reset
        if blk is None:
            self._ok()
        else:
            self._pending = Pending("block", blk=blk)

    # --- withheld ok -------------------------------------------------------------
    def _resolve_pending(self) -> bool:
        """Try to finish the pending line; True when it is done (ok sent)."""
        p = self._pending
        if p.kind == "block":
            if self.planner.full:
                return False
            self.planner.add(p.blk)
        else:                                          # dwell: wait for the planner to drain, then time
            if p.stage == 0:
                if self.planner.q:
                    return False
                p.end, p.stage = self._t + p.seconds, 1
            if self._t < p.end - 1e-9:
                return False
        self._pending = None
        self._ok()
        return True
