"""G-code generation: :class:`Action` list -> FluidNC-ready program.

Design decisions worth knowing (all recorded in ``DECISIONS.md``):

* **Travels use ``G1`` with an explicit ``F``, not ``G0``.**  ``G0`` runs at
  whatever ``max_rate_mm_per_min`` the firmware config happens to hold, which
  would silently invalidate every duration estimate and make the pendulum
  behaviour unpredictable.  An explicit feed keeps the plan and the machine in
  agreement.
* **Servos are ordinary axes.**  FluidNC exposes an RC servo as a motor on a
  virtual axis, so pen lift is ``G1 Z…`` and the colour revolver is ``G1 A…``.
* **Every emitted XY is checked against the safe area first.**  Nothing leaves
  this module that the machine is not allowed to reach.
* **Pre-compensation happens here and only here.**  Cartesian board
  coordinates go in, possibly pre-distorted coordinates go out, so the rest of
  the software never has to think about it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np

from ..config import MachineConfig
from .kinematics import Kinematics, make_kinematics
from .safearea import SafeArea, SafeAreaViolation
from .toolpath import Action, ActionKind, Toolpath, ToolpathStats, estimate

__all__ = [
    "GCodeError",
    "LineMeta",
    "GCodeProgram",
    "GCodeWriter",
    "render",
]


class GCodeError(ValueError):
    """G-code could not be produced."""


_ASCII_FOLD = {
    "—": "-", "–": "-", "‘": "'", "’": "'",
    "“": '"', "”": '"', "ä": "ae", "ö": "oe",
    "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue",
    "ß": "ss", "°": "deg",
}


def _ascii(text: str) -> str:
    """Fold text to plain ASCII and strip characters that break G-code."""
    out = "".join(_ASCII_FOLD.get(ch, ch) for ch in str(text))
    out = out.encode("ascii", "replace").decode("ascii")
    # ';' would start a nested comment, '(' ')' are comment delimiters.
    return out.replace(";", ",").replace("(", "[").replace(")", "]").strip()


def _fmt(value: float, digits: int = 3) -> str:
    """Format a coordinate without trailing zeros or '-0'."""
    if not math.isfinite(value):
        raise GCodeError(f"non-finite coordinate: {value}")
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-", "-0") else text


@dataclass(frozen=True)
class LineMeta:
    """Where a G-code line came from — drives progress and resume."""

    index: int
    kind: ActionKind | None
    stroke_index: int | None
    #: Cartesian target *before* pre-compensation (what the user asked for).
    target: tuple[float, float] | None = None


@dataclass
class GCodeProgram:
    """A complete program plus everything the UI needs to reason about it."""

    lines: list[str] = field(default_factory=list)
    meta: list[LineMeta] = field(default_factory=list)
    stats: ToolpathStats | None = None
    warnings: list[str] = field(default_factory=list)
    #: Planned execution time of each line, in seconds (comments are 0).
    line_durations: list[float] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.lines)

    def __iter__(self):
        return iter(self.lines)

    @property
    def text(self) -> str:
        return "\n".join(self.lines) + "\n"

    def executable_lines(self) -> list[tuple[int, str]]:
        """``(index, line)`` for lines that are not pure comments/blank."""
        out = []
        for i, line in enumerate(self.lines):
            stripped = line.strip()
            if not stripped or stripped.startswith(";") or stripped.startswith("("):
                continue
            out.append((i, stripped))
        return out

    def line_index_for_stroke(self, stroke_index: int) -> int | None:
        """First line belonging to ``stroke_index`` (for resume)."""
        for m in self.meta:
            if m.stroke_index == stroke_index:
                return m.index
        return None

    def progress_map(self) -> list[int | None]:
        """Per-line stroke index, so a streamed ack maps to UI progress."""
        return [m.stroke_index for m in self.meta]

    def executable_durations(self) -> list[float]:
        """Planned seconds per *executable* line, aligned with
        :meth:`executable_lines`. Lets the UI show a real ETA instead of a
        line-count percentage, which is meaningless when strokes differ in
        length by two orders of magnitude."""
        keep = {i for i, _ in self.executable_lines()}
        return [d for i, d in enumerate(self.line_durations) if i in keep]


class GCodeWriter:
    """Turns actions into G-code for one specific machine configuration."""

    def __init__(self, cfg: MachineConfig,
                 safe_area: SafeArea | None = None,
                 kinematics: Kinematics | None = None):
        self.cfg = cfg
        self.kin = kinematics or make_kinematics(cfg)
        self.safe_area = safe_area if safe_area is not None else SafeArea(cfg, self.kin)
        geo = cfg.geometry
        self._precompensate = geo.precompensate
        ref = geo.precompensate_reference
        self._reference = np.array(ref, dtype=float) if ref is not None \
            else np.array(self.safe_area.centroid, dtype=float)

    # ------------------------------------------------------------------

    def map_point(self, point: Sequence[float]) -> tuple[float, float]:
        """Cartesian board mm -> the XY actually sent to FluidNC."""
        if not self._precompensate:
            return (float(point[0]), float(point[1]))
        out = self.kin.precompensate(point, self._reference)
        return (float(out[0]), float(out[1]))

    def map_points(self, points: np.ndarray) -> np.ndarray:
        if not self._precompensate:
            return np.asarray(points, dtype=float)
        return self.kin.precompensate_many(points, self._reference)

    # ------------------------------------------------------------------

    def render(self, actions: Sequence[Action], *,
               start_position: Sequence[float] | None = None,
               check_safe_area: bool = True,
               title: str = "wbbot job",
               include_stats: bool = True,
               stats: ToolpathStats | None = None) -> GCodeProgram:
        cfg = self.cfg
        prog = GCodeProgram()
        servo_feed = _fmt(cfg.servo.feed_units_min, 1)
        pen_axis = cfg.servo.pen.axis
        rev_axis = cfg.servo.revolver.axis

        if stats is None:
            stats = estimate(actions, cfg, start_position=start_position)
        prog.stats = stats

        def emit(line: str, kind: ActionKind | None = None,
                 stroke_index: int | None = None,
                 target: tuple[float, float] | None = None) -> None:
            prog.meta.append(LineMeta(index=len(prog.lines), kind=kind,
                                      stroke_index=stroke_index, target=target))
            prog.lines.append(line)

        # -- header --------------------------------------------------------
        # ASCII only: the program travels over a serial link to an ESP32 that
        # has no business decoding UTF-8 in comments.
        emit(f"; {_ascii(title)}")
        emit(f"; generated by wbbot - board {_fmt(cfg.board.width_mm, 1)} x "
             f"{_fmt(cfg.board.height_mm, 1)} mm")
        emit(f"; kinematics: {self.kin.name}"
             f"{' + precompensation' if self._precompensate else ''}")
        if include_stats:
            d = stats.to_dict()
            emit(f"; strokes {d['n_strokes']}, points {d['n_points']}, "
                 f"colour changes {d['n_color_changes']}")
            emit(f"; draw {d['draw_length_mm']} mm, travel "
                 f"{d['travel_length_mm']} mm")
            emit(f"; estimated duration {d['total_duration_human']} "
                 f"({d['total_duration_s']} s)")
        emit("G21", None)          # millimetres
        emit("G90", None)          # absolute
        emit("G94", None)          # units per minute

        # -- validate every XY up front -----------------------------------
        if check_safe_area:
            targets = [a.target for a in actions
                       if a.kind in (ActionKind.TRAVEL, ActionKind.DRAW)
                       and a.target is not None]
            if targets:
                violations = self.safe_area.check_points(np.asarray(targets, dtype=float))
                if violations:
                    detail = "; ".join(str(v) for v in violations[:5])
                    raise SafeAreaViolation(
                        f"refusing to generate G-code: {len(violations)} point(s) "
                        f"outside the safe area: {detail}", violations)

        # -- body ----------------------------------------------------------
        last_feed: float | None = None
        pen_is_down = False
        position: tuple[float, float] | None = (
            tuple(float(v) for v in start_position) if start_position is not None
            else tuple(float(v) for v in cfg.motion.park_position))

        for act in actions:
            si = act.stroke_index
            if act.kind is ActionKind.PEN_UP:
                emit(f"G1 {pen_axis}{_fmt(cfg.servo.pen.up_pos, 2)} F{servo_feed}",
                     act.kind, si)
                last_feed = None
                pen_is_down = False
            elif act.kind is ActionKind.PEN_DOWN:
                if position is not None and not self.safe_area.contains(position):
                    raise SafeAreaViolation(
                        f"refusing to lower the pen at "
                        f"({position[0]:.1f}, {position[1]:.1f}) mm — outside "
                        f"the safe area")
                emit(f"G1 {pen_axis}{_fmt(cfg.servo.pen.down_pos, 2)} F{servo_feed}",
                     act.kind, si)
                last_feed = None
                pen_is_down = True
            elif act.kind is ActionKind.SET_COLOR:
                if act.color_index is None:
                    raise GCodeError("set_color action without a colour index")
                if pen_is_down:
                    raise GCodeError(
                        "colour change requested while the pen is down — the "
                        "revolver would smear the board")
                pos = cfg.revolver_position(act.color_index)
                emit(f"; colour {act.color_index + 1} "
                     f"[{_ascii(cfg.color(act.color_index).name)}]", act.kind, si)
                emit(f"G1 {rev_axis}{_fmt(pos, 2)} F{servo_feed}", act.kind, si)
                last_feed = None
            elif act.kind is ActionKind.DWELL:
                seconds = float(act.seconds or 0.0)
                if seconds > 0:
                    emit(f"G4 P{_fmt(seconds, 3)}", act.kind, si)
            elif act.kind in (ActionKind.TRAVEL, ActionKind.DRAW):
                if act.target is None:
                    raise GCodeError(f"{act.kind.value} action without a target")
                feed = act.feed_mm_min or (
                    cfg.motion.draw_feed_mm_min if act.kind is ActionKind.DRAW
                    else cfg.motion.travel_feed_mm_min)
                x, y = self.map_point(act.target)
                parts = ["G1", f"X{_fmt(x)}", f"Y{_fmt(y)}"]
                if last_feed is None or abs(feed - last_feed) > 1e-9:
                    parts.append(f"F{_fmt(feed, 1)}")
                    last_feed = feed
                emit(" ".join(parts), act.kind, si, target=act.target)
                position = act.target
            else:  # pragma: no cover - enum is closed
                raise GCodeError(f"unhandled action kind {act.kind!r}")

        # -- footer ---------------------------------------------------------
        if pen_is_down:
            emit(f"G1 {pen_axis}{_fmt(cfg.servo.pen.up_pos, 2)} F{servo_feed}",
                 ActionKind.PEN_UP)
            prog.warnings.append(
                "action list ended with the pen down; an explicit lift was appended")
        if cfg.servo.idle_disable and cfg.servo.idle_disable_gcode.strip():
            emit(cfg.servo.idle_disable_gcode.strip())
        emit("; end of job")
        self._attach_durations(prog, start_position)
        return prog

    def _attach_durations(self, prog: "GCodeProgram",
                          start_position: Sequence[float] | None) -> None:
        """Plan the emitted moves once and hand each line its own duration."""
        from .motion import Move, MotionLimits, plan  # local: avoids a cycle

        cfg = self.cfg
        limits = MotionLimits(
            acceleration_mm_s2=cfg.motion.acceleration_mm_s2,
            junction_deviation_mm=cfg.motion.junction_deviation_mm,
        )
        durations = [0.0] * len(prog.lines)
        pos = tuple(float(v) for v in (start_position or cfg.motion.park_position))
        moves: list[Move] = []
        move_lines: list[int] = []
        for m in prog.meta:
            if m.kind is ActionKind.DWELL:
                text = prog.lines[m.index]
                if " P" in text:
                    try:
                        durations[m.index] = float(text.split(" P", 1)[1].split()[0])
                    except (ValueError, IndexError):
                        pass
            elif m.kind in (ActionKind.TRAVEL, ActionKind.DRAW) and m.target is not None:
                feed = (cfg.motion.draw_feed_mm_min if m.kind is ActionKind.DRAW
                        else cfg.motion.travel_feed_mm_min)
                moves.append(Move(start=pos, end=m.target, feed_mm_min=feed,
                                  tag=m.kind.value))
                move_lines.append(m.index)
                pos = m.target
        # plan() drops zero-length moves, so match results back by identity.
        planned = {id(pm.move): pm.duration for pm in plan(moves, limits)}
        for mv, line_idx in zip(moves, move_lines):
            durations[line_idx] = planned.get(id(mv), 0.0)
        prog.line_durations = durations

    # ------------------------------------------------------------------

    def render_toolpath(self, toolpath: Toolpath, *,
                        start_position: Sequence[float] | None = None,
                        resume_from_stroke: int = 0,
                        check_safe_area: bool = True,
                        title: str = "wbbot job") -> GCodeProgram:
        actions = toolpath.expand(self.cfg,
                                  start_position=start_position,
                                  resume_from_stroke=resume_from_stroke)
        # Count strokes/points from the toolpath itself — the action list has
        # already lost the notion of "one stroke".
        remaining = toolpath.strokes[resume_from_stroke:]
        stats = estimate(
            actions, self.cfg,
            start_position=start_position or self.cfg.motion.park_position,
            n_strokes=len(remaining),
            n_points=sum(len(s.polyline.drawn_points) for s in remaining),
            colors_used=tuple(dict.fromkeys(s.color_index for s in remaining)),
        )
        return self.render(actions, start_position=start_position,
                           check_safe_area=check_safe_area, title=title,
                           stats=stats)


def render(toolpath: Toolpath, cfg: MachineConfig, **kwargs) -> GCodeProgram:
    """Convenience wrapper around :class:`GCodeWriter`."""
    return GCodeWriter(cfg).render_toolpath(toolpath, **kwargs)
