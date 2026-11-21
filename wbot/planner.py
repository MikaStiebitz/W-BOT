"""Job planner: :class:`Drawing` -> G-code lines for FluidNC.

All geometry is physical board mm; every XY target goes through
``mapping.path_to_firmware`` (exact pre-distortion). The servo order rule is
hard: pen up -> wait -> revolver -> wait -> (later) pen down. A revolver line is
never emitted without a preceding pen-up + dwell and never together with
another axis. The result is deterministic.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

import numpy as np

from .config import Config, ServoSpec
from .drawing import Drawing, Stroke, clean, clip_to_rect, count_outside, optimize_order
from .kinematics import Mapping
from .servo import ServoModel

log = logging.getLogger(__name__)

MAX_LINE = 60


class PlanError(ValueError):
    """The job cannot be planned safely."""


@dataclass
class PlanOptions:
    dry_run: bool = False            # pen stays up, XY moves identical
    clip: bool = True                # clip to the safe rect (with warning); False -> PlanError
    speed_scale: float = 1.0         # scales travel and draw feed (capped by firmware max rate)
    park_at_end: bool = True
    color_map: dict[int, int] | None = None   # drawing colour -> revolver slot
    optimize: bool = True            # reorder strokes (colour groups, nearest neighbour)
    start_phys: tuple[float, float] | None = None   # where the pen is now (None: unknown)
    current_slot: int | None = None  # revolver slot known to be active (skips the first move)
    pen_down_dwell_s: float = 0.15   # wait after lowering the pen
    pre_lift_dwell_s: float = 0.05   # wait before lifting the pen
    pen_up_dwell_s: float = 0.05     # wait after lifting (normal stroke change)


@dataclass
class PlanLine:
    text: str
    kind: str                        # move | travel | draw | pen | color | dwell | setup
    phys: tuple[float, float] | None = None
    color: int | None = None         # active slot after this line
    pen_down: bool | None = None     # pen state after this line
    t: float = 0.0                   # rough duration of this line in seconds (weights for the progress estimate)


@dataclass
class Plan:
    lines: list[PlanLine] = field(default_factory=list)
    estimated_s: float = 0.0
    line_end: list[float] = field(default_factory=list)   # estimated time (s) at which each line has finished
    stats: dict = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def gcode(self) -> list[str]:
        return [l.text for l in self.lines]


# ------------------------------------------------------------------------------ G-code helpers
def _n(v: float) -> str:
    s = f"{v:.3f}"
    return "0.000" if s == "-0.000" else s


def servo_gcode(spec: ServoSpec, angle_deg: float, *, feed: float | None = None) -> str:
    """``G1 <axis><value> F<deg/min>`` for an absolute servo angle (raises ServoLimitError)."""
    model = ServoModel(spec)
    f = spec.speed_deg_s * 60.0 if feed is None else feed
    return f"G1 {spec.axis}{_n(model.axis_value(angle_deg))} F{int(round(f))}"


def dwell_gcode(seconds: float) -> str:
    return f"G4 P{max(seconds, 0.0):.3f}"


# ------------------------------------------------------------------------------ time estimate
def _run_time(pts: list[tuple[float, float]], feeds: list[float], acc: float, jd: float) -> float:
    """Seconds for a continuous run of XY moves (rest at both ends, junction deviation between).
    ``feeds`` in mm/s, one per segment."""
    p = np.asarray(pts, float)
    seg = np.diff(p, axis=0)
    ln = np.hypot(seg[:, 0], seg[:, 1])
    keep = ln > 1e-9
    seg, ln, f = seg[keep], ln[keep], np.asarray(feeds, float)[keep]
    n = len(ln)
    if n == 0:
        return 0.0
    u = seg / ln[:, None]
    vj = np.zeros(n + 1)
    cosang = -np.einsum("ij,ij->i", u[:-1], u[1:])
    sin_half = np.sqrt(np.clip(0.5 * (1 - cosang), 0, 1))
    with np.errstate(divide="ignore", invalid="ignore"):
        v2 = acc * jd * sin_half / np.maximum(1 - sin_half, 1e-9)
    vj[1:-1] = np.where(cosang < -0.999999, np.inf, np.sqrt(v2))
    vj[1:-1] = np.minimum(vj[1:-1], np.minimum(f[:-1], f[1:]))
    vj[1:-1] = np.where(cosang > 0.999999, 0.0, vj[1:-1])
    v = vj.copy()
    for i in range(n):                                   # forward pass
        v[i + 1] = min(v[i + 1], math.sqrt(v[i] ** 2 + 2 * acc * ln[i]))
    for i in range(n - 1, -1, -1):                       # backward pass
        v[i] = min(v[i], math.sqrt(v[i + 1] ** 2 + 2 * acc * ln[i]))
    t = 0.0
    for i in range(n):
        a, b, L, fm = v[i], v[i + 1], ln[i], f[i]
        vp2 = (2 * acc * L + a * a + b * b) / 2          # peak speed^2 of the triangular profile
        if vp2 <= fm * fm:
            vp = math.sqrt(max(vp2, 0.0))
            t += (vp - a) / acc + (vp - b) / acc
        else:
            t += (fm - a) / acc + (fm - b) / acc + (L - (fm * fm - a * a) / (2 * acc) - (fm * fm - b * b) / (2 * acc)) / fm
    return t


# ------------------------------------------------------------------------------ planner
class _Emitter:
    def __init__(self, cfg: Config, mapping: Mapping, opts: PlanOptions):
        self.cfg, self.map, self.o = cfg, mapping, opts
        self.lines: list[PlanLine] = []
        self.pen = ServoModel(cfg.servos.pen)
        self.rev = ServoModel(cfg.servos.revolver)
        self.slot: int | None = None
        self.pen_down = False
        self.pen_deg: float | None = None
        self.rev_deg: float | None = None
        self.f_cur: int | None = None
        self.run: list[tuple[float, float]] = []
        self.run_feeds: list[float] = []
        self.time = 0.0
        mx = cfg.firmware.max_rate_mm_min
        sc = max(opts.speed_scale, 1e-3)
        self.f_travel = int(round(min(max(cfg.motion.travel_feed_mm_min * sc, 1.0), mx)))
        self.f_draw = int(round(min(max(cfg.motion.draw_feed_mm_min * sc, 1.0), mx)))
        self.fw_pos: tuple[float, float] | None = None
        self.phys: tuple[float, float] | None = opts.start_phys
        self.travel_mm = 0.0

    # -- raw line output
    def add(self, text: str, kind: str, phys=None) -> None:
        if len(text) > MAX_LINE:
            raise PlanError(f"G-code line too long: {text}")
        self.lines.append(PlanLine(text, kind, phys, self.slot, self.pen_down))

    def _flush_run(self) -> None:
        if len(self.run) > 1:
            self.time += _run_time(self.run, self.run_feeds, self.cfg.motion.acceleration_mm_s2,
                                   self.cfg.motion.junction_deviation_mm)
        self.run, self.run_feeds = [], []

    def _break(self) -> None:
        self._flush_run()
        self.f_cur = None                      # first XY line after a servo/dwell restates F

    def dwell(self, s: float) -> None:
        if s <= 0:
            return
        self._break()
        self.time += s
        self.add(dwell_gcode(s), "dwell", self.phys)
        self.lines[-1].t = s

    def servo(self, which: str, deg: float) -> None:
        spec = self.cfg.servos.pen if which == "pen" else self.cfg.servos.revolver
        text = servo_gcode(spec, deg)                          # raises ServoLimitError
        self._break()
        prev = self.pen_deg if which == "pen" else self.rev_deg
        span = (ServoModel(spec).max_deg - ServoModel(spec).min_deg) if prev is None else abs(deg - prev)
        self.time += span / spec.speed_deg_s
        if which == "pen":
            self.pen_deg = deg
            self.pen_down = deg == self.cfg.servos.pen.down_deg and not self.o.dry_run
        else:
            self.rev_deg = deg
        self.add(text, "pen" if which == "pen" else "color", self.phys)
        self.lines[-1].t = span / spec.speed_deg_s

    def xy(self, fw: tuple[float, float], phys: tuple[float, float], feed: int, kind: str) -> None:
        x, y = _n(fw[0]), _n(fw[1])
        if self.fw_pos is not None and (x, y) == (_n(self.fw_pos[0]), _n(self.fw_pos[1])):
            self.phys = phys
            return
        text = f"G1 X{x} Y{y}" + (f" F{feed}" if self.f_cur != feed else "")
        self.f_cur = feed
        fx, fy = float(x), float(y)
        if not self.run:
            self.run = [self.fw_pos or (fx, fy)]
        self.run.append((fx, fy))
        self.run_feeds.append(feed / 60.0)
        dist = math.hypot(fx - self.run[-2][0], fy - self.run[-2][1]) if len(self.run) > 1 else 0.0
        self.fw_pos = (fx, fy)
        self.phys = phys
        self.add(text, kind, phys)
        self.lines[-1].t = dist / max(feed / 60.0, 1e-6)

    # -- composite moves
    def path(self, pts: np.ndarray, feed: int, kind: str, include_first: bool) -> None:
        o = self.cfg.motion
        fw = self.map.path_to_firmware(pts, o.predistort_tolerance_mm, o.max_chord_mm, o.min_chord_mm)
        ph = self.map.to_physical(fw)
        ph[-1] = pts[-1]
        for i in range(0 if include_first else 1, len(fw)):
            self.xy((float(fw[i, 0]), float(fw[i, 1])), (float(ph[i, 0]), float(ph[i, 1])), feed, kind)

    def travel_to(self, target: np.ndarray, kind: str = "travel") -> None:
        t = (float(target[0]), float(target[1]))
        if self.phys is None:
            self.path(np.array([t]), self.f_travel, kind, include_first=True)
        else:
            self.travel_mm += math.hypot(t[0] - self.phys[0], t[1] - self.phys[1])
            self.path(np.array([self.phys, t]), self.f_travel, kind, include_first=False)

    def lift(self, wait_s: float) -> None:
        if not self.o.dry_run and self.pen_down:
            self.dwell(self.o.pre_lift_dwell_s)
        if self.pen_deg != self.cfg.servos.pen.up_deg:
            self.servo("pen", self.cfg.servos.pen.up_deg)
        self.dwell(wait_s)

    def lower(self) -> None:
        self.servo("pen", self.cfg.servos.pen.down_deg)
        self.dwell(self.o.pen_down_dwell_s)

    def select_slot(self, slot: int) -> None:
        """Rotate the revolver. Caller has lifted the pen and waited (asserted)."""
        if self.pen_down or self.pen_deg != self.cfg.servos.pen.up_deg:
            raise PlanError("internal error: revolver move without the pen raised")
        if self.o.current_slot is not None and self.slot is None and slot == self.o.current_slot:
            self.slot = slot
            self.rev_deg = self.cfg.servos.revolver.slots_deg[slot]
            return
        deg = self.cfg.servos.revolver.slots_deg[slot]
        wait = self.rev.travel_time_s(self.rev_deg, deg)
        self.servo("color", deg)
        self.slot = slot
        self.lines[-1].color = slot
        self.dwell(wait)


def plan(drawing: Drawing, cfg: Config, mapping: Mapping, opts: PlanOptions | None = None) -> Plan:
    o = opts or PlanOptions()
    warnings: list[str] = []
    rect = cfg.safe_rect()
    rev_on = cfg.servos.revolver.enabled
    nslots = len(cfg.servos.revolver.slots_deg)

    # ---- safety: everything inside the safe rect
    d = drawing
    outside = count_outside(d, rect)
    if outside:
        if not o.clip:
            raise PlanError(f"{outside} points lie outside the permitted area {tuple(round(v, 1) for v in rect)}")
        d = clip_to_rect(d, rect)
        warnings.append(f"{outside} points outside the permitted area were clipped")
    d = clean(d)

    # ---- colour -> slot
    cmap = o.color_map or {}
    strokes = []
    for s in d.strokes:
        slot = int(cmap.get(s.color, s.color))
        if not 0 <= slot < nslots:
            raise PlanError(f"Colour {s.color} has no revolver slot (slot {slot}, available: {nslots})")
        strokes.append(Stroke(s.pts, slot))
    d = Drawing(strokes, d.name)
    if not rev_on and len({s.color for s in d.strokes}) > 1:
        warnings.append("The revolver is disabled: several colours will be drawn with the current pen")

    em = _Emitter(cfg, mapping, o)
    start = o.start_phys or tuple(float(v) for v in mapping.ref_phys)
    if o.optimize:
        d = optimize_order(d, start)

    # validate all servo targets up-front (ServoLimitError propagates)
    pen_spec = cfg.servos.pen
    em.pen.axis_value(pen_spec.up_deg)
    em.pen.axis_value(pen_spec.down_deg)
    if rev_on:
        for a in cfg.servos.revolver.slots_deg:
            em.rev.axis_value(a)

    if not d.strokes:
        warnings.append("The drawing contains no strokes")
    em.add("G21", "setup")
    em.add("G90", "setup")
    em.lift(em.pen.travel_time_s(None, pen_spec.up_deg))      # unknown pen angle -> full range

    n_draw, draw_mm, color_changes = 0, 0.0, 0
    for idx, s in enumerate(d.strokes):
        if rev_on and em.slot != s.color:
            if em.slot is not None:
                if o.dry_run:        # real runs already lifted + waited after the previous stroke
                    em.lift(em.pen.travel_time_s(pen_spec.down_deg, pen_spec.up_deg))
                color_changes += 1
            em.select_slot(s.color)
        em.travel_to(s.start)
        if not o.dry_run:
            em.lower()
        em.path(s.pts, em.f_draw, "draw", include_first=False)
        n_draw += 1
        draw_mm += s.length()
        nxt = d.strokes[idx + 1] if idx + 1 < len(d.strokes) else None
        if not o.dry_run:
            change = rev_on and nxt is not None and nxt.color != s.color
            em.lift(em.pen.travel_time_s(pen_spec.down_deg, pen_spec.up_deg) if change else o.pen_up_dwell_s)

    if o.park_at_end:
        px, py = cfg.motion.park_position
        qx = min(max(px, rect[0]), rect[2])
        qy = min(max(py, rect[1]), rect[3])
        if (qx, qy) != (px, py):
            warnings.append("The park position is outside the permitted area and was limited")
        em.travel_to(np.array([qx, qy]), "move")
    em._flush_run()

    # final safety: never leave the pen down
    if em.pen_down:
        em.lift(o.pen_up_dwell_s)

    lines = em.lines
    plan_ = Plan(lines=lines, estimated_s=em.time, warnings=warnings)
    raw = np.cumsum([ln.t for ln in lines]) if lines else np.zeros(0)
    if len(raw) and raw[-1] > 0:                          # spread the modelled total (with acceleration) over the lines
        plan_.line_end = (raw * (em.time / raw[-1])).tolist()
    plan_.stats = {
        "strokes": n_draw,
        "draw_mm": round(draw_mm, 1),
        "travel_mm": round(em.travel_mm, 1),
        "color_changes": color_changes,
        "lines": len(lines),
        "bytes": sum(len(l.text) + 1 for l in lines),
    }
    return plan_


__all__ = ["Plan", "PlanLine", "PlanOptions", "PlanError", "plan", "servo_gcode", "dwell_gcode"]
