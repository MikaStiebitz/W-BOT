"""Usable drawing area from belt statics: error amplification and belt tension.

A V-plotter gets worse where the belts get flat (near the anchor line: tiny belt-length
errors become large position errors, ``amplification``) and where one belt carries almost no
load (bottom corners far from one anchor: the belt sags, ``tension``). Both are evaluated
here from the *configured* anchors, so a new anchor layout can be judged before drawing.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .config import Config
from .kinematics import KinematicsError, TrueModel, true_model_from_config

G = 9.81


def _model(cfg: Config) -> TrueModel:
    """Statics use the tilt-free gondola (attach points rigidly offset from the pen): consistent with the
    belt directions below, and the tilt only moves the attach points by a few mm."""
    return true_model_from_config(cfg, tilt=False, radius=cfg.geometry.pulley_pitch_radius_mm)


def amplification(cfg: Config, pts) -> np.ndarray:
    """Largest singular value of J^-1: mm of pen error per mm of belt-length error."""
    p = np.asarray(pts, float).reshape(-1, 2)
    model = _model(cfg)

    def one(q) -> float:
        try:
            _, jac = model.lengths_and_jac(q.reshape(1, 2))
            return float(np.linalg.norm(np.linalg.inv(jac[0]), 2))
        except (KinematicsError, np.linalg.LinAlgError):
            return float("inf")                      # unreachable / singular: unusable

    try:
        _, jac = model.lengths_and_jac(p)
        return np.array([np.linalg.norm(np.linalg.inv(m), 2) for m in jac])
    except (KinematicsError, np.linalg.LinAlgError):
        return np.array([one(q) for q in p])


def tensions(cfg: Config, pts, mass_kg: float | None = None) -> np.ndarray:
    """(N,2) belt tensions in N (left, right) of the hanging gondola. The belt lines of action run from
    the two attach points tangentially to the pulleys. ``mass_kg`` default: ``geometry.gondola_mass_kg``.
    A negative value means that belt would have to push, i.e. the pen is outside the span."""
    p = np.asarray(pts, float).reshape(-1, 2)
    m = cfg.geometry.gondola_mass_kg if mass_kg is None else mass_kg
    model = _model(cfg)

    def solve(q: np.ndarray) -> np.ndarray:
        d = model.belt_dirs(q)                                     # (N,2,2): [left, right] x (dx, dy)
        a = np.stack([d[:, :, 0], d[:, :, 1]], axis=1)             # rows: x, y ; columns: left, right
        rhs = np.tile(np.array([0.0, m * G]), (len(q), 1))
        return np.linalg.solve(a, rhs[..., None])[..., 0]

    try:
        return solve(p)
    except (KinematicsError, np.linalg.LinAlgError):
        out = np.full((len(p), 2), -np.inf)                         # unreachable: no valid tension
        for i, q in enumerate(p):
            try:
                out[i] = solve(q.reshape(1, 2))[0]
            except (KinematicsError, np.linalg.LinAlgError):
                pass
        return out


@dataclass(frozen=True)
class WorkspaceReport:
    rect: tuple[float, float, float, float]
    amp_max: float
    amp_max_at: tuple[float, float]
    tension_min_n: float
    tension_min_at: tuple[float, float]
    tension_max_n: float

    def ok(self, max_amp: float = 5.0, min_tension_n: float = 0.2) -> bool:
        return self.amp_max <= max_amp and self.tension_min_n >= min_tension_n


def analyze(cfg: Config, rect=None, step: float = 10.0) -> WorkspaceReport:
    xmin, ymin, xmax, ymax = rect or cfg.safe_rect()
    xs, ys = np.arange(xmin, xmax + 1e-9, step), np.arange(ymin, ymax + 1e-9, step)
    p = np.array([[x, y] for x in xs for y in ys])
    amp, ten = amplification(cfg, p), tensions(cfg, p)
    tmin = ten.min(axis=1)
    ia, it = int(amp.argmax()), int(tmin.argmin())
    return WorkspaceReport((xmin, ymin, xmax, ymax), float(amp[ia]), tuple(p[ia]), float(tmin[it]),
                           tuple(p[it]), float(ten.max()))


def suggest_rect(cfg: Config, max_amp: float = 5.0, min_tension_n: float = 0.2,
                 step: float = 10.0) -> tuple[float, float, float, float]:
    """Largest board-centred rectangle (equal side margins, bottom = board margin) in which
    amplification <= max_amp and both belts carry >= min_tension_n."""
    b = cfg.board
    xs, ys = np.arange(0, b.width_mm + 1e-9, step), np.arange(0, b.height_mm + 1e-9, step)
    p = np.array([[x, y] for x in xs for y in ys])
    ok = (amplification(cfg, p) <= max_amp) & (tensions(cfg, p).min(axis=1) >= min_tension_n)
    best, ybot = None, cfg.board_margins()[2]
    for xm in np.arange(b.margin_mm, b.width_mm / 2, step):
        for ytop in np.arange(b.height_mm - b.margin_mm, b.height_mm / 2, -step):
            sel = (p[:, 0] >= xm) & (p[:, 0] <= b.width_mm - xm) & (p[:, 1] >= ybot) & (p[:, 1] <= ytop)
            if sel.any() and ok[sel].all():
                area = (b.width_mm - 2 * xm) * (ytop - ybot)
                if best is None or area > best[0]:
                    best = (area, xm, ytop)
                break               # lower ytop only shrinks the area for this xm
    if best is None:
        raise ValueError("no usable area with these limits")
    return (float(best[1]), float(ybot), float(b.width_mm - best[1]), float(best[2]))
