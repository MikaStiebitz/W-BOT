"""Independent last line of defence: validate G-code *before* it reaches the controller.

The planner already respects the safe area and the servo order; this module
re-derives both from the final G-code text, so a bug in any generator (planner,
jog, console) cannot move the machine out of bounds or turn the revolver with
the pen down.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import numpy as np

from .config import Config
from .kinematics import KinematicsError, Mapping
from .servo import ServoModel

_WORD = re.compile(r"([A-Za-z])\s*(-?\d*\.?\d+)")
TOL_MM = 0.25            # tolerance for the physical bounds check
PEN_TOL_DEG = 3.0


@dataclass(frozen=True)
class Problem:
    index: int
    line: str
    message: str


def parse_words(line: str) -> dict[str, float]:
    line = line.split(";")[0]
    line = re.sub(r"\([^)]*\)", "", line)
    return {m.group(1).upper(): float(m.group(2)) for m in _WORD.finditer(line)}


def pen_state_after(axis_z: float | None, cfg: Config) -> str:
    """'up' | 'down' | 'unknown' for a Z-axis value."""
    if axis_z is None:
        return "unknown"
    pen = ServoModel(cfg.servos.pen)
    ang = pen.angle_for_axis(axis_z)
    if abs(ang - cfg.servos.pen.up_deg) <= PEN_TOL_DEG:
        return "up"
    if abs(ang - cfg.servos.pen.down_deg) <= PEN_TOL_DEG:
        return "down"
    return "unknown"


def validate_gcode(lines: list[str], cfg: Config, mapping: Mapping | None, *,
                   z_axis: float | None = None, allow_unreferenced_xy: bool = False,
                   start_outside: bool = False) -> list[Problem]:
    """Return a list of problems (empty = safe to send).

    ``start_outside``: the pen currently sits outside the safe rect (e.g. in the board corner
    after power-up); targets are tolerated until the first one inside the rect.

    Checks: every XY target maps to a physical point inside the safe rect (needs
    ``mapping``), servo targets are inside their limits, an A (revolver) move only
    happens while the pen is known to be up and no XY/Z motion is queued in the
    same line.
    """
    problems: list[Problem] = []
    pen, rev = ServoModel(cfg.servos.pen), ServoModel(cfg.servos.revolver)
    xmin, ymin, xmax, ymax = cfg.safe_rect()
    absolute = True
    pos_xy: list[float] | None = None   # last absolute XY target (modal)
    pen_moved = False                   # a Z move happened inside this batch ...
    dwell_s = 0.0                       # ... and this much G4 dwell followed it
    for i, raw in enumerate(lines):
        text = raw.strip()
        if not text or text.startswith(";"):
            continue
        up = text.upper()
        if up.startswith("$J="):
            text = text[3:]
        elif up.startswith("$") or text.startswith("?"):
            continue
        w = parse_words(text)
        if "G" in w:
            g = w["G"]
            if g == 90:
                absolute = True
            elif g == 91:
                absolute = False
        has_xy = "X" in w or "Y" in w
        has_z = "Z" in w
        has_a = "A" in w
        if w.get("G") == 4 and "P" in w:
            dwell_s += w["P"]
        if (has_xy or has_z) and has_a:
            problems.append(Problem(i, raw, "Revolver und Bewegung/Stift in derselben Zeile"))
        if has_z:
            try:
                z_angle = pen.angle_for_axis(w["Z"] if absolute else (z_axis or 0.0) + w["Z"])
                if not pen.min_deg - 0.3 <= z_angle <= pen.max_deg + 0.3:
                    problems.append(Problem(i, raw, f"Stift-Winkel {z_angle:.1f}° außerhalb der Grenzen"))
            except Exception as exc:  # noqa: BLE001
                problems.append(Problem(i, raw, f"Stift-Wert ungültig: {exc}"))
            z_axis = w["Z"] if absolute else (z_axis or 0.0) + w["Z"]
            pen_moved, dwell_s = True, 0.0
        if has_a:
            if pen_moved and dwell_s + 1e-9 < cfg.servos.pen.settle_s:
                problems.append(Problem(i, raw, "Revolver dreht zu früh nach dem Heben: erst warten, bis der Stift oben ist"))
            if pen_state_after(z_axis, cfg) != "up":
                problems.append(Problem(i, raw, "Revolver darf nur drehen, wenn der Stift sicher oben ist"))
            a_ang = rev.angle_for_axis(w["A"])
            if not rev.min_deg - 0.3 <= a_ang <= rev.max_deg + 0.3:
                problems.append(Problem(i, raw, f"Revolver-Winkel {a_ang:.1f}° außerhalb der Grenzen"))
        if has_xy:
            if not absolute:
                problems.append(Problem(i, raw, "Relative XY-Fahrten (G91) sind gesperrt"))
                continue
            x = w.get("X", pos_xy[0] if pos_xy else None)
            y = w.get("Y", pos_xy[1] if pos_xy else None)
            if x is None or y is None:
                problems.append(Problem(i, raw, "XY-Fahrt ohne vollstaendige Zielposition"))
                continue
            pos_xy = [x, y]
            if mapping is None:
                if not allow_unreferenced_xy:
                    problems.append(Problem(i, raw, "Keine Referenz: absolute Fahrten gesperrt"))
                continue
            try:
                p = mapping.to_physical([x, y])
            except KinematicsError as exc:
                problems.append(Problem(i, raw, f"Position nicht erreichbar: {exc}"))
                continue
            inside = xmin - TOL_MM <= p[0] <= xmax + TOL_MM and ymin - TOL_MM <= p[1] <= ymax + TOL_MM
            if inside:
                start_outside = False             # entered the safe area: strict from here on
            elif not start_outside:               # (the way in from outside, e.g. the boot corner, is allowed)
                problems.append(Problem(
                    i, raw, f"Ziel ({p[0]:.1f}, {p[1]:.1f}) mm außerhalb des erlaubten Bereichs"))
    return problems


def physical_in_bounds(cfg: Config, pts) -> bool:
    xmin, ymin, xmax, ymax = cfg.safe_rect()
    a = np.asarray(pts, float).reshape(-1, 2)
    return bool(np.all((a[:, 0] >= xmin - 1e-9) & (a[:, 0] <= xmax + 1e-9)
                       & (a[:, 1] >= ymin - 1e-9) & (a[:, 1] <= ymax + 1e-9)))
