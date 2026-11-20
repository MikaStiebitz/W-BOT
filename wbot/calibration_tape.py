"""Calibration without a camera and without a coordinate system: measure *distances* with a tape.

The machine draws labelled crosses A, B, C ... (pattern ``tape-points``). The user measures a short
list of centre-to-centre distances (long ones: board width, height, diagonals) - a tape measure
gets those to about +-1 mm over 1.3 m, much easier than absolute x/y positions from a board corner.
Distances are invariant to where the board origin is, so the reference only has to be roughly right.

Model: the same as ``calibration.fit_geometry`` (``_Setup.predict`` = where the pen really lands
for each aimed-at point); the residual of pair k is |P_a - P_b| - d_k. A weak prior keeps the
parameters that distances cannot see (a common shift of both anchors) at their configured values.
"""
from __future__ import annotations

import logging
import math
from typing import Sequence

import numpy as np
from scipy.optimize import least_squares

from .calibration import (PRIOR_NOISE_MM, CalibrationError, _GROUPS, _LABEL, _Setup, _build_patch, _comp,
                          _nominal)
from .config import Config, deep_update
from .kinematics import KinematicsError
from .patterns import tape_points

log = logging.getLogger(__name__)
XY = tuple[float, float]
MAX_PAIRS = 200


def tape_plan(cfg: Config, cols: int = 3, rows: int = 3, arm_mm: float = 12.0) -> dict:
    """Points to draw and the distances to measure (outer edges, diagonals, centre lines, spokes)."""
    if not (2 <= cols <= 5 and 2 <= rows <= 4):
        raise CalibrationError("Spalten 2-5, Zeilen 2-4.")
    pts = tape_points(cfg, cols, rows, max(float(arm_mm), 20.0))
    by = {n: (x, y) for n, x, y in pts}
    grid = [[pts[r * cols + k][0] for k in range(cols)] for r in range(rows)]
    pairs: list[tuple[str, str]] = []

    def add(a, b):
        if a != b and (a, b) not in pairs and (b, a) not in pairs:
            pairs.append((a, b))
    tl, tr, bl, br = grid[0][0], grid[0][-1], grid[-1][0], grid[-1][-1]
    for a, b in ((tl, tr), (bl, br), (tl, bl), (tr, br), (tl, br), (tr, bl)):
        add(a, b)
    for r in range(1, rows - 1):
        add(grid[r][0], grid[r][-1])
    for k in range(1, cols - 1):
        add(grid[0][k], grid[-1][k])
    if rows >= 3 and cols >= 3:
        c = grid[rows // 2][cols // 2]
        for corner in (tl, tr, bl, br):
            add(c, corner)
    else:
        for r in range(rows - 1):
            for k in range(cols - 1):
                add(grid[r][k], grid[r + 1][k + 1])
    out = [{"a": a, "b": b, "nominal_mm": round(math.dist(by[a], by[b]), 1)} for a, b in pairs]
    return {"points": [{"name": n, "xy": [round(x, 2), round(y, 2)]} for n, x, y in pts], "pairs": out,
            "pattern": {"id": "tape-points", "params": {"cols": cols, "rows": rows, "arm_mm": float(arm_mm)}}}


def fit_distances(cfg: Config, reference: XY, ref_fw: XY | None, pairs: Sequence[tuple[XY, XY, float]],
                  free: Sequence[str] | None = None, *, min_rms_mm: float = 0.8) -> dict:
    """``pairs``: (aimed-at point a, aimed-at point b, measured distance mm)."""
    n = len(pairs)
    if n > MAX_PAIRS:
        raise CalibrationError(f"Zu viele Abstände (max. {MAX_PAIRS}).")
    if n < 3:
        raise CalibrationError("Mindestens 3 Abstände messen (empfohlen: alle aus der Liste).")
    warnings: list[str] = []
    try:
        A = np.array([p[0] for p in pairs], float).reshape(-1, 2)
        B = np.array([p[1] for p in pairs], float).reshape(-1, 2)
        d = np.array([p[2] for p in pairs], float)
    except (TypeError, ValueError) as exc:
        raise CalibrationError("Abstände ungültig (erwartet: Punkt a, Punkt b, gemessene mm).") from exc
    if not (np.all(np.isfinite(A)) and np.all(np.isfinite(B)) and np.all(np.isfinite(d))) or np.any(d <= 0):
        raise CalibrationError("Abstände enthalten ungültige Zahlen.")
    nominal = np.linalg.norm(A - B, axis=1)
    if np.any(np.abs(d - nominal) > 0.05 * nominal + 20.0):
        bad = [i + 1 for i in np.where(np.abs(d - nominal) > 0.05 * nominal + 20.0)[0]]
        raise CalibrationError(f"Abstand {', '.join(map(str, bad))} weicht mehr als 5 % vom Soll ab - vertippt oder "
                               "falsches Punktpaar?")
    if free is None:
        free = ("scale", "anchor_left", "anchor_right") if n >= 8 else ("scale",)
        if n < 8:
            warnings.append(f"Nur {n} Abstände: es wird nur der Riemen-Maßstab angepasst.")
    names = [f for f in dict.fromkeys(free)]
    for nm in names:
        if nm not in _GROUPS or nm in ("reference", "pen_offset"):
            raise CalibrationError(f"'{nm}' lässt sich aus Abständen nicht bestimmen.")
    uniq, inv = np.unique(np.vstack([A, B]).round(6), axis=0, return_inverse=True)
    ia, ib = inv[:n], inv[n:]
    setup = _Setup(cfg, reference, ref_fw, uniq)
    nom = _nominal(cfg)
    comps = [_comp(nm, nom) for g in names for nm in _GROUPS[g]]
    if len(comps) > n:
        raise CalibrationError(f"{len(comps)} freie Parameter bei {n} Abständen: zu wenige Messungen.")

    def params(x):
        p = dict(nom)
        for c, xi in zip(comps, x):
            p[c.name] = c.nominal + xi / c.k
        return p

    def res(x):
        try:
            P = setup.predict(params(x))
        except KinematicsError:
            return np.full(n, 1e3)
        return np.linalg.norm(P[ia] - P[ib], axis=1) - d

    prior_w = np.array([PRIOR_NOISE_MM / (c.sigma * c.k) for c in comps])
    x0 = np.zeros(len(comps))
    lo = np.array([(c.lo - c.nominal) * c.k for c in comps])
    hi = np.array([(c.hi - c.nominal) * c.k for c in comps])
    sol = least_squares(lambda x: np.concatenate([res(x), prior_w * x]), x0, bounds=(lo, hi), loss="soft_l1",
                        f_scale=1.0, diff_step=1e-3, max_nfev=300)
    r0, r1 = res(x0), res(sol.x)
    rms_b, rms_a = float(np.sqrt(np.mean(r0 ** 2))), float(np.sqrt(np.mean(r1 ** 2)))
    p = params(sol.x)
    improved = rms_b >= min_rms_mm and rms_a <= 0.85 * rms_b and rms_b - rms_a >= 0.3
    if not improved:
        warnings.append(f"Abstände {rms_b:.2f} → {rms_a:.2f} mm rms: " +
                        ("liegt im Bereich der Messgenauigkeit, keine Änderung nötig." if rms_b < min_rms_mm
                         else "keine klare Verbesserung, Konfiguration bleibt."))
    patch = _build_patch(cfg, names, p) if improved else {}
    if patch:
        try:
            deep_update(cfg, patch)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"Ergebnis ungültig ({exc}) - keine Änderung.")
            patch, improved = {}, False
    bad = [i + 1 for i in np.where(np.abs(r1) > max(3.0, 4 * rms_a))[0]] if n >= 6 else []
    if bad:
        warnings.append(f"Abstand {', '.join(map(str, bad))} passt nicht zu den anderen - bitte nachmessen.")
    log.info("tape fit n=%d free=%s rms %.3f -> %.3f improved=%s", n, names, rms_b, rms_a, improved)
    return {"patch": patch, "improved": improved, "rms_before": rms_b, "rms_after": rms_a,
            "max_before": float(np.max(np.abs(r0))), "max_after": float(np.max(np.abs(r1))),
            "residuals_before": [float(v) for v in r0], "residuals": [float(v) for v in r1],
            "free": names, "n": n, "warnings": warnings, "suspicious": bad,
            "params": {c.name: float(p[c.name]) for c in comps},
            "labels": {c.name: _LABEL[c.name] for c in comps}}

