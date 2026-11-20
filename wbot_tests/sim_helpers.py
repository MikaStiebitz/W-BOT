"""Test helpers for the simulator: a raw-byte "host" on top of a manual-time emulator."""
from __future__ import annotations

import re
from collections import deque

import numpy as np

from wbot.config import Config
from wbot.servo import ServoModel
from wbot.sim import FluidNCEmulator

STATUS_RE = re.compile(r"^<([^|>]+)((?:\|[^|>]*)*)>$")


def make_emu(cfg: Config | None = None, **kw) -> FluidNCEmulator:
    """Opened emulator with manual time (deterministic); boot messages already consumed."""
    kw.setdefault("realtime", False)
    emu = FluidNCEmulator(cfg or Config(), **kw)
    emu.open()
    emu.read(0)
    return emu


def parse_status(line: str) -> dict:
    m = STATUS_RE.match(line)
    assert m, f"not a status line: {line!r}"
    out: dict = {"state": m.group(1), "raw": line}
    for field in m.group(2).split("|")[1:]:
        k, _, v = field.partition(":")
        out[k] = [float(x) for x in v.split(",")] if k in ("MPos", "WPos", "WCO", "FS", "Bf") else v
    return out


class Host:
    """Minimal serial host: sends bytes, advances sim time, collects lines."""

    def __init__(self, emu: FluidNCEmulator):
        self.emu = emu
        self.lines: list[str] = []         # everything received so far (not consumed by cmd/status)
        self._buf = ""

    # --- low level ----------------------------------------------------------
    def send(self, text: str | bytes) -> None:
        self.emu.write(text.encode("latin-1") if isinstance(text, str) else text)

    def pump(self, dt: float = 0.0) -> list[str]:
        if dt:
            self.emu.advance(dt)
        data = self.emu.read(0).decode("latin-1")
        self._buf += data
        out = []
        while "\n" in self._buf:
            ln, self._buf = self._buf.split("\n", 1)
            ln = ln.strip("\r")
            if ln:
                out.append(ln)
        self.lines.extend(out)
        return out

    def take(self) -> list[str]:
        out, self.lines = self.lines, []
        return out

    # --- convenience ----------------------------------------------------------
    def cmd(self, line: str, dt: float = 0.0) -> list[str]:
        """Send one line, advance ``dt`` sim seconds, return the lines received (consumed)."""
        self.take()
        self.send(line + "\n")
        self.pump(dt)
        return self.take()

    def status(self, dt: float = 0.0) -> dict:
        self.take()
        if dt:
            self.emu.advance(dt)
        self.send("?")
        self.pump()
        sts = [ln for ln in self.take() if ln.startswith("<")]
        assert sts, "no status reply"
        return parse_status(sts[-1])

    def run(self, max_s: float = 600.0) -> float:
        """Advance until the machine is settled; keeps the received lines."""
        used = self.emu.run_until_idle(max_s)
        self.pump()
        return used

    def stream(self, lines: list[str], rx_limit: int = 127, step: float = 0.02, max_sim: float = 3600.0) -> list[str]:
        """Character-counting streamer. Returns the ok/error replies in order (other lines in self.lines)."""
        sent: deque[int] = deque()
        outstanding, i = 0, 0
        replies: list[str] = []
        t_end = self.emu.sim_time + max_sim
        while i < len(lines) or sent:
            while i < len(lines) and outstanding + len(lines[i]) + 1 <= rx_limit:
                self.send(lines[i] + "\n")
                sent.append(len(lines[i]) + 1)
                outstanding += len(lines[i]) + 1
                i += 1
            self.emu.advance(step)
            for ln in self.pump():
                if ln == "ok" or ln.startswith("error"):
                    outstanding -= sent.popleft()
                    replies.append(ln)
                else:
                    self.lines.append(ln)
            assert self.emu.sim_time < t_end, "stream did not finish"
        return replies


# --- G-code builders ------------------------------------------------------------
def servo_z(cfg: Config, deg: float) -> float:
    return ServoModel(cfg.servos.pen).axis_value(deg)


def servo_a(cfg: Config, deg: float) -> float:
    return ServoModel(cfg.servos.revolver).axis_value(deg)


def g1_lines(pts, feed: float, prec: int = 4) -> list[str]:
    return [f"G1 X{x:.{prec}f} Y{y:.{prec}f} F{feed:g}" for x, y in pts]


def deviation_to_polylines(trace_points: np.ndarray, polylines: list[np.ndarray]) -> np.ndarray:
    """Distance of each point to the nearest segment of any polyline."""
    best = np.full(len(trace_points), np.inf)
    for pl in polylines:
        for a, b in zip(pl[:-1], pl[1:]):
            ab = b - a
            t = np.clip(((trace_points - a) @ ab) / (ab @ ab), 0.0, 1.0)
            d = np.linalg.norm(trace_points - (a + t[:, None] * ab), axis=1)
            best = np.minimum(best, d)
    return best


def drawn_points(emu: FluidNCEmulator) -> np.ndarray:
    """Endpoints of all pen-down trace segments (physical mm)."""
    pts = []
    for s in emu.trace:
        if s.pen_down:
            pts.append((s.x0, s.y0))
            pts.append((s.x1, s.y1))
    return np.asarray(pts, float).reshape(-1, 2)
