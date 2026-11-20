"""Toolpath IR: the single canonical description of "what the machine does".

A :class:`Toolpath` is an ordered list of :class:`Stroke` objects (one polyline
plus its marker colour).  :meth:`Toolpath.expand` turns that into a flat list of
:class:`Action` objects — travels, draws, pen lifts, colour changes, dwells.

Everything downstream consumes ``Action`` lists, never the strokes directly:

* :mod:`wbbot.core.gcode` renders them to G-code,
* :func:`estimate` times them with the real trapezoidal planner,
* the simulator replays them,
* the web UI previews them.

That keeps "pen up before travelling" a single decision made in one place.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Iterator, Sequence

import numpy as np

from ..config import MachineConfig
from .geometry import Drawing, Polyline
from .motion import Move, MotionLimits, plan
from .safearea import SafeArea, SafeAreaViolation

__all__ = [
    "ActionKind",
    "Action",
    "Stroke",
    "ToolpathStats",
    "Toolpath",
    "estimate",
]


class ActionKind(str, Enum):
    SET_COLOR = "set_color"
    PEN_UP = "pen_up"
    PEN_DOWN = "pen_down"
    TRAVEL = "travel"      # rapid, pen up
    DRAW = "draw"          # feed, pen down
    DWELL = "dwell"


@dataclass(frozen=True)
class Action:
    """One elementary machine action."""

    kind: ActionKind
    target: tuple[float, float] | None = None
    feed_mm_min: float | None = None
    color_index: int | None = None
    seconds: float | None = None
    #: Index of the stroke this action belongs to; enables exact job resume.
    stroke_index: int | None = None

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        if self.target is not None:
            return (f"{self.kind.value}({self.target[0]:.2f}, "
                    f"{self.target[1]:.2f})")
        if self.color_index is not None:
            return f"{self.kind.value}({self.color_index})"
        if self.seconds is not None:
            return f"{self.kind.value}({self.seconds:.3f}s)"
        return self.kind.value


@dataclass(frozen=True)
class Stroke:
    """One continuous pen-down path in one colour."""

    color_index: int
    polyline: Polyline

    def __post_init__(self) -> None:
        if self.color_index < 0:
            raise ValueError(f"color_index must be >= 0, got {self.color_index}")

    @property
    def length(self) -> float:
        return self.polyline.length

    @property
    def start(self) -> np.ndarray:
        return self.polyline.points[0]

    @property
    def end(self) -> np.ndarray:
        pts = self.polyline.drawn_points
        return pts[-1]

    def reversed(self) -> "Stroke":
        if self.polyline.closed:
            return self
        return Stroke(self.color_index, self.polyline.reversed())


@dataclass(frozen=True)
class ToolpathStats:
    """Numbers the UI shows before anything is sent to the machine."""

    n_strokes: int
    n_points: int
    n_color_changes: int
    draw_length_mm: float
    travel_length_mm: float
    draw_duration_s: float
    travel_duration_s: float
    dwell_duration_s: float
    colors_used: tuple[int, ...]

    @property
    def total_length_mm(self) -> float:
        return self.draw_length_mm + self.travel_length_mm

    @property
    def total_duration_s(self) -> float:
        return self.draw_duration_s + self.travel_duration_s + self.dwell_duration_s

    @property
    def travel_ratio(self) -> float:
        total = self.total_length_mm
        return (self.travel_length_mm / total) if total > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "n_strokes": self.n_strokes,
            "n_points": self.n_points,
            "n_color_changes": self.n_color_changes,
            "draw_length_mm": round(self.draw_length_mm, 2),
            "travel_length_mm": round(self.travel_length_mm, 2),
            "total_length_mm": round(self.total_length_mm, 2),
            "travel_ratio": round(self.travel_ratio, 4),
            "draw_duration_s": round(self.draw_duration_s, 2),
            "travel_duration_s": round(self.travel_duration_s, 2),
            "dwell_duration_s": round(self.dwell_duration_s, 2),
            "total_duration_s": round(self.total_duration_s, 2),
            "total_duration_human": _human_duration(self.total_duration_s),
            "colors_used": list(self.colors_used),
        }


def _human_duration(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


@dataclass
class Toolpath:
    """An ordered, colour-grouped drawing job in board millimetres."""

    strokes: list[Stroke] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    @classmethod
    def from_drawing(cls, drawing: Drawing,
                     color_order: Sequence[int] | None = None) -> "Toolpath":
        """Flatten a :class:`Drawing`, grouping strokes by colour.

        Colour grouping is mandatory, not an optimisation: a revolver change
        costs time *and* repeatability, so all paths of one colour are drawn
        before the next colour is selected.  That caps the number of changes
        at ``n_colours - 1``.
        """
        by_color: dict[int, list[Polyline]] = {}
        for layer in drawing.layers:
            bucket = by_color.setdefault(layer.color_index, [])
            bucket.extend(p for p in layer.paths if not p.is_empty)

        if color_order is not None:
            order = [c for c in color_order if c in by_color]
            order += [c for c in sorted(by_color) if c not in order]
        else:
            order = sorted(by_color)

        strokes: list[Stroke] = []
        for color in order:
            for path in by_color[color]:
                strokes.append(Stroke(color, path))
        return cls(strokes=strokes, metadata=dict(drawing.metadata))

    @classmethod
    def from_polylines(cls, paths: Sequence[Polyline],
                       color_index: int = 0) -> "Toolpath":
        return cls(strokes=[Stroke(color_index, p) for p in paths if not p.is_empty])

    # ------------------------------------------------------------------
    # basics
    # ------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.strokes)

    def __iter__(self) -> Iterator[Stroke]:
        return iter(self.strokes)

    @property
    def is_empty(self) -> bool:
        return not self.strokes

    @property
    def colors_used(self) -> tuple[int, ...]:
        seen: list[int] = []
        for s in self.strokes:
            if s.color_index not in seen:
                seen.append(s.color_index)
        return tuple(seen)

    @property
    def color_change_count(self) -> int:
        changes = 0
        current: int | None = None
        for s in self.strokes:
            if s.color_index != current:
                if current is not None:
                    changes += 1
                current = s.color_index
        return changes

    @property
    def bounds(self) -> tuple[float, float, float, float] | None:
        pts = [s.polyline.points for s in self.strokes if len(s.polyline)]
        if not pts:
            return None
        allp = np.vstack(pts)
        return (float(allp[:, 0].min()), float(allp[:, 1].min()),
                float(allp[:, 0].max()), float(allp[:, 1].max()))

    def all_points(self) -> np.ndarray:
        arrays = [s.polyline.drawn_points for s in self.strokes if len(s.polyline)]
        if not arrays:
            return np.zeros((0, 2), dtype=float)
        return np.vstack(arrays)

    def transform(self, matrix: np.ndarray) -> "Toolpath":
        return Toolpath([Stroke(s.color_index, s.polyline.transform(matrix))
                         for s in self.strokes], dict(self.metadata))

    def grouped_by_color(self, color_order: Sequence[int] | None = None) -> "Toolpath":
        """Re-sort so every colour is drawn in one contiguous block."""
        by_color: dict[int, list[Stroke]] = {}
        for s in self.strokes:
            by_color.setdefault(s.color_index, []).append(s)
        if color_order is not None:
            order = [c for c in color_order if c in by_color]
            order += [c for c in sorted(by_color) if c not in order]
        else:
            order = sorted(by_color)
        out: list[Stroke] = []
        for c in order:
            out.extend(by_color[c])
        return Toolpath(out, dict(self.metadata))

    # ------------------------------------------------------------------
    # validation
    # ------------------------------------------------------------------

    def validate(self, safe_area: SafeArea, *, raise_on_violation: bool = True) -> list:
        """Check every drawn point against the safe area.

        Travel moves are checked too — the pen is up, but the gondola still
        has to physically get there.
        """
        violations = []
        for i, stroke in enumerate(self.strokes):
            violations.extend(
                safe_area.check_points(stroke.polyline.drawn_points, path_index=i))
        if violations and raise_on_violation:
            detail = "; ".join(str(v) for v in violations[:5])
            raise SafeAreaViolation(
                f"toolpath leaves the safe area at {len(violations)} point(s): "
                f"{detail}", violations)
        return violations

    # ------------------------------------------------------------------
    # expansion
    # ------------------------------------------------------------------

    def expand(self, cfg: MachineConfig, *,
               start_position: Sequence[float] | None = None,
               resume_from_stroke: int = 0,
               park_at_end: bool = True) -> list[Action]:
        """Flatten into machine actions.

        ``resume_from_stroke`` re-emits the colour selection and travels to
        the resume point, so a paused job continues in the right colour at
        the right place instead of silently drawing the rest in whatever
        colour happened to be loaded.
        """
        if resume_from_stroke < 0 or (self.strokes and
                                      resume_from_stroke > len(self.strokes)):
            raise ValueError(
                f"resume_from_stroke {resume_from_stroke} out of range "
                f"(0..{len(self.strokes)})")

        m = cfg.motion
        park = tuple(float(v) for v in (start_position or m.park_position))
        actions: list[Action] = [Action(ActionKind.PEN_UP)]
        if m.pen_up_dwell_s > 0:
            actions.append(Action(ActionKind.DWELL, seconds=m.pen_up_dwell_s))

        current_color: int | None = None
        position = park
        pen_down = False

        for i, stroke in enumerate(self.strokes[resume_from_stroke:],
                                   start=resume_from_stroke):
            pts = stroke.polyline.drawn_points
            if len(pts) < 2:
                continue

            if stroke.color_index != current_color:
                if pen_down:
                    actions.append(Action(ActionKind.PEN_UP, stroke_index=i))
                    pen_down = False
                    if m.pen_up_dwell_s > 0:
                        actions.append(Action(ActionKind.DWELL,
                                              seconds=m.pen_up_dwell_s,
                                              stroke_index=i))
                actions.append(Action(ActionKind.SET_COLOR,
                                      color_index=stroke.color_index,
                                      stroke_index=i))
                if m.color_change_dwell_s > 0:
                    actions.append(Action(ActionKind.DWELL,
                                          seconds=m.color_change_dwell_s,
                                          stroke_index=i))
                current_color = stroke.color_index

            target = (float(pts[0][0]), float(pts[0][1]))
            if not _close(position, target):
                if pen_down:
                    actions.append(Action(ActionKind.PEN_UP, stroke_index=i))
                    pen_down = False
                    if m.pen_up_dwell_s > 0:
                        actions.append(Action(ActionKind.DWELL,
                                              seconds=m.pen_up_dwell_s,
                                              stroke_index=i))
                actions.append(Action(ActionKind.TRAVEL, target=target,
                                      feed_mm_min=m.travel_feed_mm_min,
                                      stroke_index=i))
                position = target

            if not pen_down:
                actions.append(Action(ActionKind.PEN_DOWN, stroke_index=i))
                pen_down = True
                if m.pen_down_dwell_s > 0:
                    actions.append(Action(ActionKind.DWELL,
                                          seconds=m.pen_down_dwell_s,
                                          stroke_index=i))

            for p in pts[1:]:
                target = (float(p[0]), float(p[1]))
                actions.append(Action(ActionKind.DRAW, target=target,
                                      feed_mm_min=m.draw_feed_mm_min,
                                      stroke_index=i))
                position = target

        if pen_down:
            actions.append(Action(ActionKind.PEN_UP))
            if m.pen_up_dwell_s > 0:
                actions.append(Action(ActionKind.DWELL, seconds=m.pen_up_dwell_s))
        if park_at_end and not _close(position, park):
            actions.append(Action(ActionKind.TRAVEL, target=park,
                                  feed_mm_min=m.travel_feed_mm_min))
        return actions

    # ------------------------------------------------------------------
    # statistics
    # ------------------------------------------------------------------

    def stats(self, cfg: MachineConfig,
              start_position: Sequence[float] | None = None) -> ToolpathStats:
        actions = self.expand(cfg, start_position=start_position)
        return estimate(actions, cfg,
                        start_position=start_position or cfg.motion.park_position,
                        n_strokes=len(self.strokes),
                        n_points=sum(len(s.polyline.drawn_points)
                                     for s in self.strokes),
                        colors_used=self.colors_used)


def _close(a: Sequence[float], b: Sequence[float], tol: float = 1e-6) -> bool:
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def estimate(actions: Sequence[Action], cfg: MachineConfig,
             start_position: Sequence[float] | None = None,
             *,
             n_strokes: int | None = None,
             n_points: int | None = None,
             colors_used: Sequence[int] | None = None) -> ToolpathStats:
    """Time and measure an action list with the real trapezoidal planner."""
    limits = MotionLimits(
        acceleration_mm_s2=cfg.motion.acceleration_mm_s2,
        junction_deviation_mm=cfg.motion.junction_deviation_mm,
    )
    pos = tuple(float(v) for v in (start_position or cfg.motion.park_position))

    moves: list[Move] = []
    dwell = 0.0
    draw_len = 0.0
    travel_len = 0.0
    color_changes = 0
    current_color: int | None = None
    seen_colors: list[int] = []

    for act in actions:
        if act.kind is ActionKind.DWELL:
            dwell += float(act.seconds or 0.0)
        elif act.kind is ActionKind.SET_COLOR:
            if act.color_index is not None:
                if current_color is not None and act.color_index != current_color:
                    color_changes += 1
                current_color = act.color_index
                if act.color_index not in seen_colors:
                    seen_colors.append(act.color_index)
        elif act.kind in (ActionKind.TRAVEL, ActionKind.DRAW):
            if act.target is None:
                raise ValueError(f"{act.kind.value} action without a target")
            feed = act.feed_mm_min or (
                cfg.motion.draw_feed_mm_min if act.kind is ActionKind.DRAW
                else cfg.motion.travel_feed_mm_min)
            tag = "draw" if act.kind is ActionKind.DRAW else "travel"
            move = Move(start=pos, end=act.target, feed_mm_min=feed, tag=tag)
            d = move.distance
            if tag == "draw":
                draw_len += d
            else:
                travel_len += d
            moves.append(move)
            pos = act.target
        # PEN_UP / PEN_DOWN cost time only through their dwells.

    planned = plan(moves, limits)
    draw_time = sum(pm.duration for pm in planned if pm.tag == "draw")
    travel_time = sum(pm.duration for pm in planned if pm.tag == "travel")

    return ToolpathStats(
        n_strokes=n_strokes if n_strokes is not None else 0,
        n_points=n_points if n_points is not None else len(moves) + 1,
        n_color_changes=color_changes,
        draw_length_mm=draw_len,
        travel_length_mm=travel_len,
        draw_duration_s=draw_time,
        travel_duration_s=travel_time,
        dwell_duration_s=dwell,
        colors_used=tuple(colors_used) if colors_used is not None
        else tuple(seen_colors),
    )
