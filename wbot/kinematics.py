"""Belt kinematics and the pre-distortion that makes FluidNC's naive model exact.

Two models live here:

``TrueModel``      what the machine physically does. The belt leaves the pulley
                   *tangentially*, wraps the pulley and hangs down on the outer
                   side, so the belt length (measured to the motor) is

                       L(P) = sqrt(D^2 - r^2) + r * (pi - alpha - beta)  [+ const]
                       D = |P - A|,  alpha = atan2(v_y, v_x),  beta = acos(r / D)

                   (mirrored for the right pulley). With r = 0 this degenerates
                   to the naive model. The belts do not end at the pen tip but at two
                   attach points on the gondola (gondola V2: +-8 mm, 176 mm above the pen);
                   the rigid gondola also tilts into static equilibrium (``gondola.py``:
                   the two belt lines and gravity through the centre of mass are concurrent).
                   ``true_model_from_config`` is the one factory that builds it.

``FirmwareModel``  what FluidNC's WallPlotter runs: belt length = distance from
                   the anchor *centre* to the pen, no wrap term. The wrap term
                   alone moves the pen by ~13 mm across this board.

Everything the motors do depends only on *belt-length changes*. So the exact
trick is not "correct a point" but "make the firmware's belt-length change equal
the true machine's belt-length change":

    L_fw(F) = L_fw(F0) + (L_true(P) - L_true(P0)) / scale         (per belt)

``(P0, F0)`` is the *reference*: the physical pen position P0 that corresponds
to the position F0 the firmware believes it is at. Solving the two circle
equations for F gives the pre-distorted firmware coordinate for any physical
target P (``Mapping.to_firmware``) and, with a Newton solve on the true model,
the physical position for any firmware coordinate (``Mapping.to_physical``).
This also absorbs an arbitrary power-up offset: it does not matter where the
firmware thinks it starts, only that (P0, F0) is a correct pair.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import numpy as np

from . import gondola as _gd
from .config import Config
from .gondola import KinematicsError, left_len_grad as _left_len_grad

ArrayLike = np.ndarray | list | tuple


def _as_pts(p: ArrayLike) -> tuple[np.ndarray, bool]:
    a = np.asarray(p, dtype=float)
    single = a.ndim == 1
    return (a.reshape(1, 2) if single else a.reshape(-1, 2)), single


@dataclass(frozen=True)
class TrueModel:
    """Belts + gondola. The belts end at two attach points, not at the pen tip.

    Gondola frame: origin = pen tip. Attach vectors ``e = (+-attach_half_spacing, attach_height) -
    pen_offset`` (``pen_offset`` is a small calibration correction, sign as in the old point model:
    attach = pen - pen_offset when spacing = height = 0).

    * ``attach_half_spacing == 0`` or ``tilt == False``: the attach points keep their position
      relative to the pen (no rotation); every belt is an independent analytic function of the pen.
      With spacing = height = 0 this is exactly the old single-point model.
    * otherwise the rigid gondola rotates by ``theta(P)`` into static equilibrium (see ``gondola.py``;
      ``com_height`` / ``com_x`` = centre of mass above / right of the pen tip).
    """
    anchor_left: tuple[float, float]
    anchor_right: tuple[float, float]
    pulley_r: float
    pen_offset: tuple[float, float] = (0.0, 0.0)
    attach_half_spacing: float = 0.0
    attach_height: float = 0.0
    com_height: float = 33.8
    com_x: float = 1.2
    tilt: bool = True

    # --- geometry of the gondola ---------------------------------------------
    @property
    def e_left(self) -> tuple[float, float]:
        return (-self.attach_half_spacing - self.pen_offset[0], self.attach_height - self.pen_offset[1])

    @property
    def e_right(self) -> tuple[float, float]:
        return (self.attach_half_spacing - self.pen_offset[0], self.attach_height - self.pen_offset[1])

    @cached_property
    def has_tilt(self) -> bool:
        return bool(self.tilt and self.attach_half_spacing > 0.0)

    def _free_lengths_and_jac(self, p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Tilt-free model (attach points rigidly offset from the pen): analytic lengths + Jacobian."""
        vl = p + np.asarray(self.e_left) - np.asarray(self.anchor_left)
        vr = p + np.asarray(self.e_right) - np.asarray(self.anchor_right)
        ll, gl = _left_len_grad(vl, self.pulley_r)
        # right pulley = mirror image about the vertical axis
        vr_m = np.stack([-vr[:, 0], vr[:, 1]], axis=-1)
        lr, gr_m = _left_len_grad(vr_m, self.pulley_r)
        gr = np.stack([-gr_m[:, 0], gr_m[:, 1]], axis=-1)
        return np.stack([ll, lr], axis=-1), np.stack([gl, gr], axis=1)

    def _solve_pose(self, p: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
        """(theta (N,), lengths (N,2) or None when the analytic path computes them)."""
        if self.has_tilt:
            return _gd.solve_tilt(self, p)
        return np.zeros(len(p)), None

    # --- inverse kinematics --------------------------------------------------
    def tilt_angle(self, pen: ArrayLike) -> np.ndarray:
        """Gondola tilt theta (rad, counter-clockwise positive) at pen positions (0 without tilt)."""
        p, single = _as_pts(pen)
        th = self._solve_pose(p)[0]
        return th[0] if single else th

    def lengths_and_jac(self, pen: ArrayLike) -> tuple[np.ndarray, np.ndarray]:
        """Absolute true belt lengths (N,2) and d(L)/d(pen) (N,2,2)."""
        p, _ = _as_pts(pen)
        if not self.has_tilt:
            return self._free_lengths_and_jac(p)
        h = 1e-3                                           # central differences (theta re-solved each time)
        lens = _gd.solve_tilt(self, p)[1]
        jac = np.empty((len(p), 2, 2))
        for k in range(2):
            d = np.zeros(2)
            d[k] = h
            jac[:, :, k] = (_gd.solve_tilt(self, p + d)[1] - _gd.solve_tilt(self, p - d)[1]) / (2 * h)
        return lens, jac

    def lengths(self, pen: ArrayLike) -> np.ndarray:
        p, single = _as_pts(pen)
        l = _gd.solve_tilt(self, p)[1] if self.has_tilt else self._free_lengths_and_jac(p)[0]
        return l[0] if single else l

    def lengths_theta(self, p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Belt lengths (N,2) and tilt (N,) for pen positions ``p`` (N,2) (no input reshaping)."""
        if self.has_tilt:
            th, l = _gd.solve_tilt(self, p)
            return l, th
        return self._free_lengths_and_jac(p)[0], np.zeros(len(p))

    def attach_points(self, pen: ArrayLike) -> tuple[np.ndarray, np.ndarray]:
        """Absolute (left, right) belt attach points at the equilibrium pose."""
        p, single = _as_pts(pen)
        al, ar = _gd.attach_points(self, p, self._solve_pose(p)[0])
        return (al[0], ar[0]) if single else (al, ar)

    def belt_dirs(self, pen: ArrayLike) -> np.ndarray:
        """Unit directions (N,2,2) [left, right] from the attach point towards the pulley (line of
        action of the belt tension) at the equilibrium pose."""
        p, single = _as_pts(pen)
        d = _gd.pose(self, p, self._solve_pose(p)[0], with_dirs=True)[2]
        return d[0] if single else d

    # --- forward kinematics --------------------------------------------------
    def position(self, lengths: ArrayLike, seed: ArrayLike | None = None,
                 tol: float = 1e-9, max_iter: int = 60, theta0: ArrayLike | None = None) -> np.ndarray:
        """Pen position for absolute true belt lengths (Newton). ``seed`` (+ ``theta0``) = a pen
        position / tilt already known to be close (skips the tilt-free pre-solve).

        Stage 1 solves the tilt-free model analytically; with tilt, stage 2 refines on the full
        rigid-body model (numerical Jacobian over pen position and tilt)."""
        lt, single = _as_pts(lengths)
        if seed is None:
            fw = FirmwareModel(self.anchor_left, self.anchor_right)
            x = fw.position(lt) - 0.5 * (np.asarray(self.e_left) + np.asarray(self.e_right))
        else:
            x, _ = _as_pts(seed)
            x = x.copy()
        th0 = None if theta0 is None else np.asarray(theta0, float).reshape(-1)
        if not (self.has_tilt and th0 is not None):          # (a given tilt means: seed is already precise)
            stage_tol = 1e-4 if self.has_tilt else tol
            for _ in range(max_iter):
                cur, jac = self._free_lengths_and_jac(x)
                res = cur - lt
                if np.max(np.abs(res)) < stage_tol:
                    break
                step = np.linalg.solve(jac, res[..., None])[..., 0]
                # trust region: never jump more than 50 mm per iteration
                n = np.linalg.norm(step, axis=-1, keepdims=True)
                step = np.where(n > 50.0, step * 50.0 / np.maximum(n, 1e-12), step)
                x = x - step
            else:
                raise KinematicsError("true-model forward kinematics did not converge")
        if self.has_tilt:
            x = _gd.refine_position(self, lt, x, th0)
        return x[0] if single else x


_TRUE_KEYS = {"anchor_left", "anchor_right", "radius", "pen_offset", "attach_half_spacing",
              "attach_height", "com_height", "com_x", "tilt"}


def true_model_from_config(cfg: Config, **overrides) -> TrueModel:
    """THE place that turns ``cfg.geometry`` into a ``TrueModel`` (host mapping, simulator plant,
    calibration, workspace statics all use it). ``overrides`` (``None`` ignored): anchor_left,
    anchor_right, radius, pen_offset, attach_half_spacing, attach_height, com_height, com_x, tilt.
    ``model: naive`` = no pre-distortion: no pulley wrap and a point gondola (no attach points)."""
    ov = {k: v for k, v in overrides.items() if v is not None}
    bad = set(ov) - _TRUE_KEYS
    if bad:
        raise ValueError(f"unknown true-model parameters: {sorted(bad)}")
    g = cfg.geometry
    naive = g.model == "naive"
    d = {
        "anchor_left": g.anchor_left, "anchor_right": g.anchor_right,
        "radius": 0.0 if naive else g.pulley_pitch_radius_mm,
        "pen_offset": g.pen_offset_mm,
        "attach_half_spacing": 0.0 if naive else g.attach_half_spacing_mm,
        "attach_height": 0.0 if naive else g.attach_height_mm,
        "com_height": g.com_height_mm, "com_x": g.com_offset_x_mm, "tilt": g.gondola_tilt,
    }
    d.update(ov)
    pair = lambda v: (float(v[0]), float(v[1]))        # noqa: E731
    return TrueModel(pair(d["anchor_left"]), pair(d["anchor_right"]), float(d["radius"]), pair(d["pen_offset"]),
                     float(d["attach_half_spacing"]), float(d["attach_height"]),
                     float(d["com_height"]), float(d["com_x"]), bool(d["tilt"]))


@dataclass(frozen=True)
class FirmwareModel:
    """FluidNC WallPlotter: belt length = distance anchor-centre -> point."""
    anchor_left: tuple[float, float]
    anchor_right: tuple[float, float]

    def lengths(self, pos: ArrayLike) -> np.ndarray:
        p, single = _as_pts(pos)
        l = np.stack([np.hypot(*(p - np.asarray(self.anchor_left)).T),
                      np.hypot(*(p - np.asarray(self.anchor_right)).T)], axis=-1)
        return l[0] if single else l

    def position(self, lengths: ArrayLike) -> np.ndarray:
        """Circle-circle intersection; the solution *below* the anchors."""
        l, single = _as_pts(lengths)
        al, ar = np.asarray(self.anchor_left, float), np.asarray(self.anchor_right, float)
        base = ar - al
        d = float(np.hypot(*base))
        u = base / d
        n = np.array([u[1], -u[0]])           # points "down" for left->right anchors
        a = (l[:, 0] ** 2 - l[:, 1] ** 2 + d * d) / (2 * d)
        h2 = l[:, 0] ** 2 - a ** 2
        if np.any(h2 < -1e-9):
            raise KinematicsError("belt lengths cannot meet (too short)")
        h = np.sqrt(np.maximum(h2, 0.0))
        out = al + a[:, None] * u + h[:, None] * n
        return out[0] if single else out


class Mapping:
    """Physical pen position <-> firmware (FluidNC) coordinate, given a reference."""

    def __init__(self, true: TrueModel, fw: FirmwareModel,
                 ref_phys: tuple[float, float], ref_fw: tuple[float, float],
                 scale: tuple[float, float] = (1.0, 1.0)):
        self.true, self.fw = true, fw
        self.scale = np.asarray(scale, float)
        self.ref_phys = np.asarray(ref_phys, float)
        self.ref_fw = np.asarray(ref_fw, float)
        self._lt0 = true.lengths(self.ref_phys)
        self._lf0 = fw.lengths(self.ref_fw)
        self._memo: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None   # (fw, phys, theta) of the last path

    @classmethod
    def from_config(cls, cfg: Config, ref_phys: tuple[float, float],
                    ref_fw: tuple[float, float] | None = None) -> "Mapping":
        g = cfg.geometry
        true = true_model_from_config(cfg)
        fw = FirmwareModel(cfg.fw_anchor_left, cfg.fw_anchor_right)
        return cls(true, fw, ref_phys, ref_fw if ref_fw is not None else ref_phys,
                   (g.belt_scale_left, g.belt_scale_right))

    # --- physical -> firmware ------------------------------------------------
    def to_firmware(self, phys: ArrayLike) -> np.ndarray:
        p, single = _as_pts(phys)
        out = self._fw(p)[0]
        return out[0] if single else out

    def _fw(self, p: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        lt, th = self.true.lengths_theta(p)
        return self.fw.position(self._lf0 + (lt - self._lt0) / self.scale), th

    # --- firmware -> physical ------------------------------------------------
    def to_physical(self, fw_pos: ArrayLike) -> np.ndarray:
        f, single = _as_pts(fw_pos)
        lt = self._lt0 + (self.fw.lengths(f) - self._lf0) * self.scale
        memo = self._memo                 # exact seed when ``f`` is what path_to_firmware just returned
        if memo is not None and memo[0].shape == f.shape and np.array_equal(memo[0], f):
            out = self.true.position(lt, seed=memo[1], theta0=memo[2])
        else:
            out = self.true.position(lt, seed=f)
        return out[0] if single else out

    # --- paths -----------------------------------------------------------------
    def path_to_firmware(self, points: ArrayLike, tolerance: float = 0.02,
                         max_chord: float = 25.0, min_chord: float = 0.5) -> np.ndarray:
        """Pre-distort a physical polyline, adding vertices where FluidNC's straight
        chord between two pre-distorted vertices would stray more than ``tolerance``
        from the intended straight physical line."""
        pts, _ = _as_pts(points)
        if len(pts) < 2:
            return self.to_firmware(pts)
        # start: split every segment so none is longer than max_chord
        verts = [pts[0]]
        for a, b in zip(pts[:-1], pts[1:]):
            k = max(1, int(np.ceil(np.linalg.norm(b - a) / max_chord)))
            ts = np.linspace(0.0, 1.0, k + 1)[1:]
            verts.extend(a + t * (b - a) for t in ts)
        v = np.asarray(verts)
        fv, thv = self._fw(v)                    # pre-distorted vertices (+ tilt), kept in step with ``v``
        for _ in range(14):                      # iterative midpoint refinement (vectorised)
            a, b = v[:-1], v[1:]
            mid = (a + b) / 2
            fm, thm = self._fw(mid)
            err = np.linalg.norm(fm - (fv[:-1] + fv[1:]) / 2, axis=1)
            length = np.linalg.norm(b - a, axis=1)
            split = np.where((err > tolerance) & (length > 2 * min_chord))[0]
            if len(split) == 0:
                break
            v = np.insert(v, split + 1, mid[split], axis=0)
            fv = np.insert(fv, split + 1, fm[split], axis=0)
            thv = np.insert(thv, split + 1, thm[split])
        self._memo = (fv.copy(), v, thv)
        return fv

    def unreachable_points(self, rect: tuple[float, float, float, float], n: int = 9) -> list[tuple[float, float]]:
        """Points of ``rect`` (xmin, ymin, xmax, ymax) the firmware cannot be told to go to.

        Happens when the reference pair (P0, F0) is far apart: the firmware's absolute belt
        lengths then differ so much from the real ones that FluidNC's two circles no longer
        intersect for part of the board."""
        xmin, ymin, xmax, ymax = rect
        bad: list[tuple[float, float]] = []
        for x in np.linspace(xmin, xmax, n):
            for y in np.linspace(ymin, ymax, max(3, n * 2 // 3)):
                try:
                    self.to_firmware((x, y))
                except KinematicsError:
                    bad.append((float(x), float(y)))
        return bad

    def distortion_at(self, phys: ArrayLike) -> np.ndarray:
        """Firmware coordinate minus physical coordinate (how much we pre-distort)."""
        p, single = _as_pts(phys)
        d = self.to_firmware(p) - p
        return d[0] if single else d


def mapping_for(cfg: Config, ref_phys: tuple[float, float],
                ref_fw: tuple[float, float] | None = None) -> Mapping:
    return Mapping.from_config(cfg, ref_phys, ref_fw)


def uncompensated_error(cfg: Config, points: ArrayLike,
                        ref: tuple[float, float] | None = None) -> np.ndarray:
    """How far (mm) the pen would land from the target *without* pre-distortion,
    if the machine were referenced at ``ref`` (default: board centre)."""
    ref = ref or (cfg.board.width_mm / 2, cfg.board.height_mm / 2)
    m = Mapping.from_config(cfg, ref)
    p, _ = _as_pts(points)
    return np.linalg.norm(m.to_physical(p) - p, axis=1)
