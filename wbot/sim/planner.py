"""Motion planner of the emulated firmware: blocks, junction speeds, trapezoid execution.

A ``Block`` is one straight move in firmware coordinates (X, Y = Cartesian pen position
as the firmware sees it, Z, A = servo axes). The WallPlotter kinematics split the XY part
into segments of ``segment_length`` mm; between segment end points the motors (belt
lengths) interpolate linearly. Speed is planned along the block with ``accel`` and the
Grbl junction-deviation rule, so streamed polylines do not stop at every vertex.
"""
from __future__ import annotations

import math
from collections import deque

import numpy as np

from ..kinematics import FirmwareModel

_EPS = 1e-9


class Block:
    __slots__ = ("start", "end", "length", "lxy", "feed", "accel", "unit", "jog",
                 "seg_d", "seg_l0", "seg_l1", "max_entry", "entry", "exit", "s")

    def state_at(self, s: float) -> tuple[float, float, float, float]:
        """(belt_left, belt_right, z, a) after travelling ``s`` mm along the block."""
        u = min(max(s / self.length, 0.0), 1.0)
        d = u * self.lxy
        z = self.start[2] + (self.end[2] - self.start[2]) * u
        a = self.start[3] + (self.end[3] - self.start[3]) * u
        return (float(np.interp(d, self.seg_d, self.seg_l0)),
                float(np.interp(d, self.seg_d, self.seg_l1)), float(z), float(a))


def make_block(start, end, feed_mm_s: float, accel: float, fw: FirmwareModel,
               segment_mm: float, jog: bool = False) -> Block | None:
    """Build a block from ``start`` to ``end`` ([x, y, z, a]); None for a zero-length move."""
    start = np.asarray(start, float)
    end = np.asarray(end, float)
    d = end - start
    lxy = math.hypot(d[0], d[1])
    length = math.sqrt(float(d @ d))
    if length < 1e-9:
        return None
    b = Block()
    b.start, b.end, b.length, b.lxy = start, end, length, lxy
    b.feed, b.accel, b.jog = max(feed_mm_s, 1e-3), max(accel, 1e-3), jog
    b.unit = d / length
    if lxy > 1e-9:
        n = max(1, math.ceil(lxy / segment_mm - 1e-9))
        seg_d = np.minimum(np.arange(n + 1) * segment_mm, lxy)
        seg_d[-1] = lxy
        pts = start[:2] + (seg_d / lxy)[:, None] * d[:2]
        lens = fw.lengths(pts)
    else:
        seg_d = np.array([0.0, 1.0])
        lens = np.vstack([fw.lengths(start[:2])] * 2)
    b.seg_d, b.seg_l0, b.seg_l1 = seg_d, np.ascontiguousarray(lens[:, 0]), np.ascontiguousarray(lens[:, 1])
    b.max_entry = b.entry = b.exit = 0.0
    b.s = 0.0
    return b


def junction_speed(prev: Block, cur: Block, junction_deviation: float) -> float:
    """Grbl junction-deviation limit between two consecutive blocks (mm/s)."""
    cos_t = -float(prev.unit @ cur.unit)
    if cos_t > 0.999999:
        return 0.0                                   # full reversal
    if cos_t < -0.999999:
        return min(prev.feed, cur.feed)              # straight on
    sin_half = math.sqrt(0.5 * (1.0 - cos_t))
    acc = min(prev.accel, cur.accel)
    v = math.sqrt(acc * junction_deviation * sin_half / (1.0 - sin_half))
    return min(v, prev.feed, cur.feed)


class Planner:
    def __init__(self, cur: tuple[float, float, float, float], capacity: int = 16,
                 junction_deviation: float = 0.05):
        self.q: deque[Block] = deque()
        self.capacity = capacity
        self.jd = junction_deviation
        self.v = 0.0                 # current speed along the running block (mm/s)
        self.stopping = False        # decelerate to standstill (feed hold / jog cancel)
        self.cur = cur               # motor state (belt_l, belt_r, z, a), continuous
        self._last_end = cur

    # --- queue ---------------------------------------------------------------
    @property
    def free(self) -> int:
        return self.capacity - len(self.q)

    @property
    def full(self) -> bool:
        return len(self.q) >= self.capacity

    def add(self, blk: Block) -> None:
        blk.max_entry = junction_speed(self.q[-1], blk, self.jd) if self.q else 0.0
        self.q.append(blk)
        self.replan()

    def flush(self) -> None:
        """Abort everything immediately (soft reset / alarm): motors stop where they are."""
        self.q.clear()
        self.v = 0.0
        self.stopping = False

    def replan(self) -> None:
        q = list(self.q)
        n = len(q)
        if not n:
            return
        nxt = 0.0
        for i in range(n - 1, 0, -1):               # reverse pass: entry speeds that allow stopping
            b = q[i]
            b.exit = nxt
            b.entry = min(b.max_entry, math.sqrt(nxt * nxt + 2.0 * b.accel * b.length))
            nxt = b.entry
        q[0].exit = nxt
        q[0].entry = self.v
        for i in range(n - 1):                      # forward pass: speeds that can be reached
            b = q[i]
            rem = b.length - b.s if i == 0 else b.length
            reach = math.sqrt(b.entry * b.entry + 2.0 * b.accel * rem)
            if q[i + 1].entry > reach:
                q[i + 1].entry = reach
            b.exit = q[i + 1].entry
        q[-1].exit = 0.0

    # --- execution -------------------------------------------------------------
    def advance(self, dt: float) -> int:
        """Run the motion for ``dt`` sim seconds. Returns the number of finished blocks."""
        if not self.q:
            self.v = 0.0
            return 0
        done = self._advance_stopping(dt) if self.stopping else self._advance_run(dt)
        if done:
            self.replan()
        b = self.q[0] if self.q else None
        self.cur = b.state_at(b.s) if b else self._last_end
        return done

    def _pop(self) -> None:
        b = self.q.popleft()
        self._last_end = b.state_at(b.length)

    def _advance_run(self, dt: float) -> int:
        done = 0
        for _ in range(10000):
            if dt <= 1e-12 or not self.q:
                break
            b = self.q[0]
            a, vn, v = b.accel, b.feed, min(self.v, b.feed)
            rem = b.length - b.s
            if rem <= _EPS:
                self._pop()
                done += 1
                self.v = v
                continue
            ve = min(b.exit, vn, math.sqrt(v * v + 2.0 * a * rem))
            ddec = (v * v - ve * ve) / (2.0 * a) if v > ve else 0.0
            if v > ve + 1e-12 and rem <= ddec + 1e-7:           # braking towards the exit speed
                vend = math.sqrt(max(v * v - 2.0 * a * rem, 0.0))
                t_end = (v - vend) / a
                if t_end <= dt:
                    b.s = b.length
                    dt -= t_end
                    self.v = vend
                else:
                    b.s += v * dt - 0.5 * a * dt * dt
                    self.v = v - a * dt
                    dt = 0.0
            elif v < vn - 1e-12:                                  # accelerating
                vt = min(vn, math.sqrt((2.0 * a * rem + v * v + ve * ve) / 2.0))
                if vt <= v + 1e-12:
                    vt = v + 1e-9                                 # at the peak: next pass brakes
                t_acc = (vt - v) / a
                if t_acc <= dt:
                    b.s += 0.5 * (v + vt) * t_acc
                    self.v = vt
                    dt -= t_acc
                else:
                    b.s += v * dt + 0.5 * a * dt * dt
                    self.v = v + a * dt
                    dt = 0.0
            else:                                                 # cruising
                self.v = v
                dist = rem - ddec
                t_c = dist / v if v > 1e-12 else dt
                if t_c <= dt:
                    b.s += max(dist, 0.0)
                    dt -= t_c
                    if dist <= 1e-12:
                        b.s = b.length
                else:
                    b.s += v * dt
                    dt = 0.0
        if not self.q:
            self.v = 0.0
        return done

    def _advance_stopping(self, dt: float) -> int:
        done = 0
        for _ in range(10000):
            if dt <= 1e-12 or not self.q or self.v <= 1e-12:
                break
            b = self.q[0]
            a, v = b.accel, self.v
            rem = b.length - b.s
            if v * v / (2.0 * a) <= rem + _EPS:                   # stops inside this block
                t = min(dt, v / a)
                b.s += v * t - 0.5 * a * t * t
                self.v = v - a * t if t < v / a - 1e-15 else 0.0
                dt -= t
            else:                                                 # crosses into the next block
                vend = math.sqrt(max(v * v - 2.0 * a * rem, 0.0))
                t_end = (v - vend) / a
                if t_end <= dt:
                    b.s = b.length
                    self._pop()
                    done += 1
                    self.v = vend
                    dt -= t_end
                else:
                    b.s += v * dt - 0.5 * a * dt * dt
                    self.v = v - a * dt
                    dt = 0.0
        if not self.q or self.v <= 1e-12:
            self.v = 0.0
        return done
