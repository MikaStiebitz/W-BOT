"""Rigid-body gondola on two belts: belt primitives and the static equilibrium (tilt) solver.

Gondola frame: origin = pen tip, y up, x right. The belts leave the gondola at two attach points
``e_left`` / ``e_right`` (about +-8 mm sideways and 176 mm above the pen tip). The gondola is a rigid
body that hangs from the two belts and gravity, so it rotates by a small angle ``theta``
(counter-clockwise positive) until three coplanar forces are in equilibrium: tension along belt 1,
tension along belt 2 and gravity through the centre of mass ``c = P + R(theta)(com_x, com_h)``.
Three forces balance only if their lines of action are *concurrent*: with ``X`` = intersection of the
two belt lines the condition is ``c_x == X_x`` (gravity is vertical).

Each belt runs from its attach point ``a`` tangentially to its pulley (free length ``sqrt(D^2 - r^2)``
plus wrap arc); for the left pulley the tangent point is ``T = C + r (cos phi, sin phi)`` with
``phi = atan2(v) + acos(r / |v|)``, ``v = a - C``. The right pulley is the mirror image.

Functions take a ``TrueModel`` (duck typed: anchors, ``pulley_r``, ``e_left``, ``e_right``,
``com_x``, ``com_height``) so this module does not import ``kinematics``.
"""
from __future__ import annotations

import math

import numpy as np


class KinematicsError(RuntimeError):
    pass


TILT_LIMIT = 0.35          # rad (20 deg): beyond this the equilibrium search gives up
_G_TOL = 1e-11             # mm, concurrency residual
_RES_TOL = 1e-9            # mm, forward-kinematics residual


# --------------------------------------------------------------------------- belt primitives
def left_len_grad(v: np.ndarray, r: float) -> tuple[np.ndarray, np.ndarray]:
    """Belt length (without constant) and gradient wrt v = attach - anchor for the *left* pulley."""
    vx, vy = v[:, 0], v[:, 1]
    d2 = vx * vx + vy * vy
    d = np.sqrt(d2)
    if np.any(d <= r * 1.001):
        raise KinematicsError("pen too close to a pulley")
    root = np.sqrt(d2 - r * r)
    alpha = np.arctan2(vy, vx)
    beta = np.arccos(r / d)
    length = root + r * (np.pi - alpha - beta)
    dd_dx, dd_dy = vx / d, vy / d
    da_dx, da_dy = -vy / d2, vx / d2
    gx = (root / d) * dd_dx - r * da_dx
    gy = (root / d) * dd_dy - r * da_dy
    return length, np.stack([gx, gy], axis=-1)


def _belt(vx: np.ndarray, vy: np.ndarray, r: float):
    """Left-pulley belt: length and unit direction (attach -> tangent point)."""
    d2 = vx * vx + vy * vy
    d = np.sqrt(d2)
    if np.any(d <= r * 1.001):
        raise KinematicsError("pen too close to a pulley")
    root = np.sqrt(d2 - r * r)
    phi = np.arctan2(vy, vx) + np.arccos(r / d)
    return (root + r * (np.pi - phi),
            (r * np.cos(phi) - vx) / root, (r * np.sin(phi) - vy) / root)


def attach_points(m, p: np.ndarray, th: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Absolute attach points (N,2) each for pen positions ``p`` (N,2) and tilt ``th`` (N,)."""
    c, s = np.cos(th), np.sin(th)
    out = []
    for lx, ly in (m.e_left, m.e_right):
        out.append(np.stack([p[:, 0] + c * lx - s * ly, p[:, 1] + s * lx + c * ly], axis=-1))
    return out[0], out[1]


def pose(m, p: np.ndarray, th: np.ndarray, with_dirs: bool = False):
    """Belt lengths (N,2) and concurrency residual ``g = c_x - X_x`` (N,) at pen ``p``, tilt ``th``.
    With ``with_dirs`` also the unit belt directions (N,2,2) [left, right] and the attach points."""
    al, ar = attach_points(m, p, th)
    r = m.pulley_r
    cl, cr = m.anchor_left, m.anchor_right
    ll, dlx, dly = _belt(al[:, 0] - cl[0], al[:, 1] - cl[1], r)
    lr, dmx, dry = _belt(-(ar[:, 0] - cr[0]), ar[:, 1] - cr[1], r)
    drx = -dmx
    wx, wy = ar[:, 0] - al[:, 0], ar[:, 1] - al[:, 1]
    den = dlx * dry - dly * drx
    if np.any(np.abs(den) < 1e-9):
        raise KinematicsError("belts parallel")
    t = (wx * dry - wy * drx) / den
    xx = al[:, 0] + t * dlx
    cxp = p[:, 0] + np.cos(th) * m.com_x - np.sin(th) * m.com_height
    lens = np.stack([ll, lr], axis=-1)
    if not with_dirs:
        return lens, cxp - xx
    dirs = np.stack([np.stack([dlx, dly], -1), np.stack([drx, dry], -1)], axis=1)
    return lens, cxp - xx, dirs, (al, ar)


def solve_tilt(m, p: np.ndarray, th0: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Equilibrium tilt (N,) and belt lengths (N,2) for pen positions ``p`` (secant iteration)."""
    n = len(p)
    th_prev = np.zeros(n) if th0 is None else np.asarray(th0, float).copy()
    l_prev, g_prev = pose(m, p, th_prev)
    slope0 = max(0.5 * (m.e_left[1] + m.e_right[1]) - m.com_height, 1.0)   # dg/dtheta ~ h - hc
    th = th_prev - np.clip(g_prev / slope0, -0.2, 0.2)
    lens, g = pose(m, p, th)
    for _ in range(40):
        act = np.abs(g) > _G_TOL
        if not act.any():
            break
        dth, dg = th - th_prev, g - g_prev
        ok = np.abs(dg) > 1e-18
        slope = np.where(ok, dg / np.where(dth == 0, 1.0, dth), slope0)
        slope = np.where(np.abs(slope) < 1e-3, slope0, slope)
        step = np.where(act, np.clip(g / slope, -0.1, 0.1), 0.0)
        th_prev, g_prev = th, g
        th = th - step
        if np.max(np.abs(th)) > TILT_LIMIT:
            raise KinematicsError("gondola tilt out of range (no stable equilibrium)")
        lens, g = pose(m, p, th)
    else:
        raise KinematicsError("gondola equilibrium did not converge")
    return th, lens


def refine_position(m, lt: np.ndarray, p0: np.ndarray, th0: np.ndarray | None = None,
                    max_iter: int = 40) -> np.ndarray:
    """Pen positions (N,2) for absolute belt lengths ``lt`` (N,2): Newton on the full model with
    a numerical Jacobian, seeded with ``p0`` (e.g. the tilt-free solution)."""
    p = p0.copy()
    th = solve_tilt(m, p)[0] if th0 is None else np.asarray(th0, float).copy()
    hp, ht = 1e-5, 1e-7
    for _ in range(max_iter):
        lens, g = pose(m, p, th)
        res = np.concatenate([lens - lt, g[:, None]], axis=1)                 # (N,3)
        if np.max(np.abs(res)) < _RES_TOL:
            return p
        f0 = np.concatenate([lens, g[:, None]], axis=1)
        cols = []
        for dp, dt, h in ((np.array([hp, 0.0]), 0.0, hp), (np.array([0.0, hp]), 0.0, hp),
                          (np.zeros(2), ht, ht)):
            l2, g2 = pose(m, p + dp, th + dt)
            cols.append((np.concatenate([l2, g2[:, None]], axis=1) - f0) / h)
        jac = np.stack(cols, axis=2)                                           # (N,3,3)
        try:
            step = np.linalg.solve(jac, res[..., None])[..., 0]
        except np.linalg.LinAlgError as exc:
            raise KinematicsError("singular gondola geometry") from exc
        n = np.linalg.norm(step[:, :2], axis=-1, keepdims=True)
        scale = np.where(n > 50.0, 50.0 / np.maximum(n, 1e-12), 1.0)
        step = step * scale
        step[:, 2] = np.clip(step[:, 2], -0.1, 0.1)
        p = p - step[:, :2]
        th = th - step[:, 2]
        if np.max(np.abs(th)) > TILT_LIMIT:
            raise KinematicsError("gondola tilt out of range (no stable equilibrium)")
    raise KinematicsError("true-model forward kinematics did not converge")


# --------------------------------------------------------------------------- scalar (plant) solver
def _belt_s(vx: float, vy: float, r: float):
    d2 = vx * vx + vy * vy
    d = math.sqrt(d2)
    if d <= r * 1.001:
        raise KinematicsError("pen too close to a pulley")
    root = math.sqrt(d2 - r * r)
    phi = math.atan2(vy, vx) + math.acos(r / d)
    return (root + r * (math.pi - phi),
            (r * math.cos(phi) - vx) / root, (r * math.sin(phi) - vy) / root)


def pose_scalar(m, px: float, py: float, th: float) -> tuple[float, float, float]:
    """Scalar twin of :func:`pose` (belt lengths and concurrency residual)."""
    c, s = math.cos(th), math.sin(th)
    (llx, lly), (lrx, lry) = m.e_left, m.e_right
    alx, aly = px + c * llx - s * lly, py + s * llx + c * lly
    arx, ary = px + c * lrx - s * lry, py + s * lrx + c * lry
    r = m.pulley_r
    (clx, cly), (crx, cry) = m.anchor_left, m.anchor_right
    ll, dlx, dly = _belt_s(alx - clx, aly - cly, r)
    lr, dmx, dry = _belt_s(-(arx - crx), ary - cry, r)
    drx = -dmx
    den = dlx * dry - dly * drx
    if abs(den) < 1e-9:
        raise KinematicsError("belts parallel")
    t = ((arx - alx) * dry - (ary - aly) * drx) / den
    return ll, lr, (px + c * m.com_x - s * m.com_height) - (alx + t * dlx)


def solve_scalar(m, ll: float, lr: float, px: float, py: float, th: float,
                 max_iter: int = 40) -> tuple[float, float, float]:
    """Newton on (px, py, theta) for absolute belt lengths (``ll``, ``lr``); warm-start friendly."""
    hp, ht = 1e-5, 1e-7
    for _ in range(max_iter):
        l1, l2, g = pose_scalar(m, px, py, th)
        r1, r2 = l1 - ll, l2 - lr
        if abs(r1) < _RES_TOL and abs(r2) < _RES_TOL and abs(g) < _RES_TOL:
            return px, py, th
        a1, a2, a3 = pose_scalar(m, px + hp, py, th)
        b1, b2, b3 = pose_scalar(m, px, py + hp, th)
        c1, c2, c3 = pose_scalar(m, px, py, th + ht)
        # rows = equations (L1, L2, g), columns = (px, py, th)
        j = ((a1 - l1) / hp, (b1 - l1) / hp, (c1 - l1) / ht,
             (a2 - l2) / hp, (b2 - l2) / hp, (c2 - l2) / ht,
             (a3 - g) / hp, (b3 - g) / hp, (c3 - g) / ht)
        det = (j[0] * (j[4] * j[8] - j[5] * j[7]) - j[1] * (j[3] * j[8] - j[5] * j[6])
               + j[2] * (j[3] * j[7] - j[4] * j[6]))
        if abs(det) < 1e-14:
            raise KinematicsError("singular belt geometry")
        dx = (r1 * (j[4] * j[8] - j[5] * j[7]) - j[1] * (r2 * j[8] - j[5] * g)
              + j[2] * (r2 * j[7] - j[4] * g)) / det
        dy = (j[0] * (r2 * j[8] - j[5] * g) - r1 * (j[3] * j[8] - j[5] * j[6])
              + j[2] * (j[3] * g - r2 * j[6])) / det
        dt = (j[0] * (j[4] * g - r2 * j[7]) - j[1] * (j[3] * g - r2 * j[6])
              + r1 * (j[3] * j[7] - j[4] * j[6])) / det
        n = math.hypot(dx, dy)
        if n > 50.0:
            dx, dy = dx * 50.0 / n, dy * 50.0 / n
        dt = max(-0.1, min(0.1, dt))
        px, py, th = px - dx, py - dy, th - dt
        if abs(th) > TILT_LIMIT:
            raise KinematicsError("gondola tilt out of range (no stable equilibrium)")
    raise KinematicsError("true-model forward kinematics did not converge")
