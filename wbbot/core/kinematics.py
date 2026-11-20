r"""V-plotter kinematics: naive and tangent belt models.

Coordinate system
-----------------
Board millimetres, origin ``(0, 0)`` at the lower-left corner of the drawing
area, ``X`` right, ``Y`` up.  Anchors ``A`` (left) and ``B`` (right) are the
*centres* of the two belt pulleys and sit above and outboard of the board.

Two models
----------
``NaiveKinematics``
    ``L = |P - A|``.  This is what FluidNC's ``wallplotter`` kinematics
    computes internally, so it is the model that actually drives the machine.

``TangentKinematics``
    The physically correct model: the belt leaves the pulley *tangentially*
    and part of it is wrapped around the pulley.

Derivation of the tangent model
-------------------------------
Let ``r`` be the pulley pitch radius, ``A`` the pulley centre and ``P`` the
belt attachment point on the gondola.  Write

    v = P - A,   D = |v|,   alpha = atan2(v_y, v_x)

The belt touches the pulley at a tangent point ``T`` with ``AT ⟂ TP``.  In the
right triangle ``A-T-P`` (right angle at ``T``) the leg ``|AT| = r`` and the
hypotenuse ``|AP| = D``, hence

    free belt length   l_free = sqrt(D^2 - r^2)
    half-angle at A    beta   = arccos(r / D)

and the tangent point lies at polar angle ``alpha + beta`` on the pulley (for
the left pulley, whose slack side hangs down on the *outboard* side and which
therefore wraps over the top).

The belt is one continuous strip: counterweight -> down the outboard side ->
over the pulley -> off at ``T`` -> gondola.  The outboard run leaves the
pulley at the fixed polar angle ``theta0 = pi`` (leftmost point, tangent
vertical).  Going clockwise from ``theta0`` to the tangent point, the wrapped
arc is

    l_wrap = r * (pi - alpha - beta)

so the total belt length paid out towards the gondola is

    L(P) = sqrt(D^2 - r^2) + r * (pi - alpha - beta)   + const

The additive constant (outboard run + counterweight drop) is absorbed by the
machine zero and cancels in every difference, so it is dropped here.

The right pulley is the mirror image of the left one about a vertical axis.
Mirroring maps ``alpha -> pi - alpha``, so the same expression is used after
substituting ``alpha_mirrored = wrap_to_pi(pi - alpha)``.

Why it matters
--------------
The pure ``sqrt(D^2 - r^2)`` term is negligible: at ``D = 1000 mm`` and
``r = 12.732 mm`` it differs from ``D`` by 0.08 mm.  The *wrap* term is not:
``alpha`` sweeps roughly 74 degrees across the board, which is
``r * 1.29 rad ~ 16 mm`` of belt — a systematic, position-dependent error that
naive kinematics silently absorbs into the shape of the drawing.

Architecture note
-----------------
FluidNC computes the naive model on the controller.  We therefore keep working
in cartesian millimetres and, if the calibration shows the tangent model fits
better, *pre-distort* the target coordinates with :func:`Kinematics.precompensate`
so that FluidNC's naive arithmetic produces the belt lengths the tangent model
demands.  We never command belt lengths directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Literal, Sequence

import numpy as np

from ..config import MachineConfig

__all__ = [
    "KinematicsError",
    "UnreachableError",
    "BeltLengths",
    "Tensions",
    "Kinematics",
    "NaiveKinematics",
    "TangentKinematics",
    "make_kinematics",
    "GRAVITY_M_S2",
]

GRAVITY_M_S2 = 9.80665

ArrayLike = Sequence[float] | np.ndarray


class KinematicsError(ValueError):
    """Base class for kinematics failures."""


class UnreachableError(KinematicsError):
    """The requested position or belt-length pair has no valid solution."""


@dataclass(frozen=True)
class BeltLengths:
    """Belt length pair in millimetres (left, right)."""

    left: float
    right: float

    def as_array(self) -> np.ndarray:
        return np.array([self.left, self.right], dtype=float)

    def __iter__(self):
        yield self.left
        yield self.right


@dataclass(frozen=True)
class Tensions:
    """Static belt tensions in newtons."""

    left: float
    right: float

    @property
    def minimum(self) -> float:
        return min(self.left, self.right)

    def __iter__(self):
        yield self.left
        yield self.right


def _as_xy(point: ArrayLike) -> np.ndarray:
    arr = np.asarray(point, dtype=float)
    if arr.shape[-1] != 2:
        raise KinematicsError(f"expected a 2D point or (...,2) array, got shape {arr.shape}")
    if not np.all(np.isfinite(arr)):
        raise KinematicsError(f"point contains non-finite values: {point!r}")
    return arr


def _wrap_to_pi(angle: np.ndarray | float) -> np.ndarray | float:
    """Normalise angles into (-pi, pi]."""
    wrapped = (np.asarray(angle, dtype=float) + np.pi) % (2.0 * np.pi) - np.pi
    # (-pi, pi]: numpy's modulo yields -pi for the boundary; flip it to +pi.
    wrapped = np.where(np.isclose(wrapped, -np.pi), np.pi, wrapped)
    return wrapped if isinstance(angle, np.ndarray) else float(wrapped)


# ==========================================================================
# base
# ==========================================================================

class Kinematics:
    """Common interface for both belt models.

    Subclasses implement :meth:`inverse` (and its vectorised sibling) plus the
    model-specific pieces of :meth:`forward`.  Everything that does not depend
    on the belt model — Jacobian, condition number, tensions — lives here.
    """

    name: str = "base"

    def __init__(self,
                 anchor_left: ArrayLike,
                 anchor_right: ArrayLike,
                 pulley_radius_mm: float = 0.0,
                 gondola_mass_kg: float = 0.360) -> None:
        self.anchor_left = _as_xy(anchor_left).astype(float)
        self.anchor_right = _as_xy(anchor_right).astype(float)
        if self.anchor_left.shape != (2,) or self.anchor_right.shape != (2,):
            raise KinematicsError("anchors must be single 2D points")
        if self.anchor_right[0] <= self.anchor_left[0]:
            raise KinematicsError("anchor_right must lie to the right of anchor_left")
        if pulley_radius_mm < 0.0:
            raise KinematicsError("pulley_radius_mm must be >= 0")
        if gondola_mass_kg <= 0.0:
            raise KinematicsError("gondola_mass_kg must be > 0")
        self.pulley_radius_mm = float(pulley_radius_mm)
        self.gondola_mass_kg = float(gondola_mass_kg)
        self._anchor_span = float(np.linalg.norm(self.anchor_right - self.anchor_left))
        if self._anchor_span <= 0.0:
            raise KinematicsError("anchors must not coincide")

    # -- construction ------------------------------------------------------

    @classmethod
    def from_config(cls, cfg: MachineConfig) -> "Kinematics":
        return cls(cfg.geometry.anchor_left,
                   cfg.geometry.anchor_right,
                   cfg.geometry.pulley_pitch_radius_mm,
                   cfg.gondola.mass_kg)

    # -- to be provided by subclasses -------------------------------------

    def inverse_many(self, points: ArrayLike) -> np.ndarray:
        """Vectorised inverse kinematics.

        Parameters
        ----------
        points : array (..., 2)

        Returns
        -------
        array (..., 2) of ``[L_left, L_right]``
        """
        raise NotImplementedError

    def forward_many(self, lengths: ArrayLike) -> np.ndarray:
        """Vectorised forward kinematics. Inverse of :meth:`inverse_many`."""
        raise NotImplementedError

    # -- scalar convenience wrappers --------------------------------------

    def inverse(self, point: ArrayLike) -> BeltLengths:
        """Position -> belt lengths."""
        res = self.inverse_many(_as_xy(point).reshape(1, 2))[0]
        return BeltLengths(float(res[0]), float(res[1]))

    def forward(self, lengths: ArrayLike | BeltLengths) -> np.ndarray:
        """Belt lengths -> position ``[x, y]``."""
        if isinstance(lengths, BeltLengths):
            arr = lengths.as_array()
        else:
            arr = np.asarray(lengths, dtype=float)
        return self.forward_many(arr.reshape(1, 2))[0]

    # -- model independent -------------------------------------------------

    def jacobian(self, point: ArrayLike) -> np.ndarray:
        r"""Jacobian of the *naive* inverse kinematics at ``point``.

        .. math::
            J = \begin{bmatrix}
              (x-a_{xl})/L_1 & (y-a_{yl})/L_1 \\
              (x-a_{xr})/L_2 & (y-a_{yr})/L_2
            \end{bmatrix}

        Both rows are unit vectors pointing from the anchor to the gondola, so
        the conditioning is governed purely by the angle between the belts.
        The tangent correction changes this by O(r/D) < 1.3 % and is
        deliberately ignored here: this matrix is used for *sensitivity*
        analysis, not for control.
        """
        p = _as_xy(point).reshape(2)
        d1 = p - self.anchor_left
        d2 = p - self.anchor_right
        n1 = float(np.linalg.norm(d1))
        n2 = float(np.linalg.norm(d2))
        if n1 <= 1e-9 or n2 <= 1e-9:
            raise UnreachableError(f"point {tuple(p)} coincides with an anchor")
        return np.array([d1 / n1, d2 / n2], dtype=float)

    def singular_values(self, point: ArrayLike) -> tuple[float, float]:
        """``(sigma_max, sigma_min)`` of the Jacobian."""
        sv = np.linalg.svd(self.jacobian(point), compute_uv=False)
        return float(sv[0]), float(sv[-1])

    def error_amplification(self, point: ArrayLike) -> float:
        r"""``||J^{-1}||_2`` — how a belt-length error maps to a position error.

        A value of 2.0 means: 0.1 mm of belt error can become 0.2 mm of
        position error.  Explodes where the two belts become parallel (far
        below the anchors) or nearly collinear (just under the anchor line).
        """
        _, s_min = self.singular_values(point)
        if s_min <= 1e-12:
            return math.inf
        return 1.0 / s_min

    def condition_number(self, point: ArrayLike) -> float:
        """``sigma_max / sigma_min`` of the Jacobian (standard definition).

        See :meth:`error_amplification` for the absolute error gain, which is
        what the safe-area check actually uses.
        """
        s_max, s_min = self.singular_values(point)
        if s_min <= 1e-12:
            return math.inf
        return s_max / s_min

    def tensions(self, point: ArrayLike) -> Tensions:
        r"""Static belt tensions of the hanging gondola.

        Force balance with unit vectors ``u_i`` pointing from the gondola
        towards anchor ``i``:

        .. math::
            T_1 u_1 + T_2 u_2 = (0,\; m g)

        Both tensions must stay positive — a belt cannot push.  A negative
        result means the gondola cannot be held statically at that point,
        which happens outside the horizontal span of the anchors.
        """
        p = _as_xy(point).reshape(2)
        v1 = self.anchor_left - p
        v2 = self.anchor_right - p
        n1 = float(np.linalg.norm(v1))
        n2 = float(np.linalg.norm(v2))
        if n1 <= 1e-9 or n2 <= 1e-9:
            raise UnreachableError(f"point {tuple(p)} coincides with an anchor")
        u1 = v1 / n1
        u2 = v2 / n2
        det = u1[0] * u2[1] - u2[0] * u1[1]
        if abs(det) < 1e-12:
            # Belts collinear: the vertical load cannot be carried.
            return Tensions(-math.inf, -math.inf)
        weight = self.gondola_mass_kg * GRAVITY_M_S2
        t1 = -weight * u2[0] / det
        t2 = weight * u1[0] / det
        return Tensions(float(t1), float(t2))

    def belt_angles(self, point: ArrayLike) -> tuple[float, float]:
        """Elevation angles (rad) of both belts above horizontal at the gondola."""
        p = _as_xy(point).reshape(2)
        v1 = self.anchor_left - p
        v2 = self.anchor_right - p
        return (math.atan2(v1[1], abs(v1[0]) if v1[0] != 0 else 1e-12),
                math.atan2(v2[1], abs(v2[0]) if v2[0] != 0 else 1e-12))

    # -- pre-compensation --------------------------------------------------

    def precompensate(self, point: ArrayLike,
                      reference: ArrayLike | None = None) -> np.ndarray:
        """Return ``P'`` such that FluidNC's naive kinematics yields the belt
        lengths *this* model demands for ``point``.

        FluidNC computes ``L = |P' - A|``.  We want the machine to produce
        ``L_model(point)``.  Because the absolute belt length carries an
        arbitrary zero offset, only *differences* are meaningful, so the
        correction is anchored at a reference point where it is exactly zero
        (default: the midpoint between the anchors, projected onto the board
        height of ``point``; callers normally pass the safe-area centroid).

        For :class:`NaiveKinematics` this is the identity.
        """
        p = _as_xy(point).reshape(2)
        ref = self._default_reference() if reference is None else _as_xy(reference).reshape(2)
        naive = NaiveKinematics(self.anchor_left, self.anchor_right,
                                0.0, self.gondola_mass_kg)
        l_model = self.inverse_many(p.reshape(1, 2))[0]
        l_model_ref = self.inverse_many(ref.reshape(1, 2))[0]
        l_naive_ref = naive.inverse_many(ref.reshape(1, 2))[0]
        l_cmd = l_naive_ref + (l_model - l_model_ref)
        return naive.forward_many(l_cmd.reshape(1, 2))[0]

    def precompensate_many(self, points: ArrayLike,
                           reference: ArrayLike | None = None) -> np.ndarray:
        """Vectorised :meth:`precompensate`."""
        pts = _as_xy(points)
        flat = pts.reshape(-1, 2)
        ref = self._default_reference() if reference is None else _as_xy(reference).reshape(2)
        naive = NaiveKinematics(self.anchor_left, self.anchor_right,
                                0.0, self.gondola_mass_kg)
        l_model = self.inverse_many(flat)
        l_model_ref = self.inverse_many(ref.reshape(1, 2))[0]
        l_naive_ref = naive.inverse_many(ref.reshape(1, 2))[0]
        l_cmd = l_naive_ref + (l_model - l_model_ref)
        return naive.forward_many(l_cmd).reshape(pts.shape)

    def _default_reference(self) -> np.ndarray:
        mid = 0.5 * (self.anchor_left + self.anchor_right)
        # A point well inside a typical workspace: midway between the anchors,
        # one anchor-span/2 below them.
        return np.array([mid[0], mid[1] - 0.5 * self._anchor_span], dtype=float)

    # -- misc --------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"{type(self).__name__}(A={tuple(self.anchor_left)}, "
                f"B={tuple(self.anchor_right)}, r={self.pulley_radius_mm})")


# ==========================================================================
# naive
# ==========================================================================

class NaiveKinematics(Kinematics):
    """Belt runs to the pulley *centre* — the model FluidNC implements."""

    name = "naive"

    def inverse_many(self, points: ArrayLike) -> np.ndarray:
        pts = _as_xy(points)
        flat = pts.reshape(-1, 2)
        l1 = np.linalg.norm(flat - self.anchor_left, axis=1)
        l2 = np.linalg.norm(flat - self.anchor_right, axis=1)
        return np.stack([l1, l2], axis=1).reshape(pts.shape)

    def forward_many(self, lengths: ArrayLike) -> np.ndarray:
        r"""Circle-circle intersection.

        .. code::

            d    = |B - A|
            a    = (L1^2 - L2^2 + d^2) / (2 d)
            h    = sqrt(L1^2 - a^2)
            Pmid = A + a (B - A)/d
            P    = Pmid +- h * perp((B - A)/d)

        Of the two mirror solutions the one *below* the anchor line is taken —
        the gondola hangs, it never floats above the belts.
        """
        arr = np.asarray(lengths, dtype=float)
        if arr.shape[-1] != 2:
            raise KinematicsError(
                f"expected (...,2) belt lengths, got shape {arr.shape}")
        if not np.all(np.isfinite(arr)):
            raise UnreachableError("belt lengths contain non-finite values")
        flat = arr.reshape(-1, 2)
        l1 = flat[:, 0]
        l2 = flat[:, 1]
        if np.any(l1 < 0.0) or np.any(l2 < 0.0):
            raise UnreachableError("belt lengths must be non-negative")

        ab = self.anchor_right - self.anchor_left
        d = self._anchor_span
        u = ab / d

        a = (l1 ** 2 - l2 ** 2 + d ** 2) / (2.0 * d)
        h_sq = l1 ** 2 - a ** 2
        bad = h_sq < -1e-6
        if np.any(bad):
            idx = int(np.argmax(bad))
            raise UnreachableError(
                f"belt lengths ({l1[idx]:.3f}, {l2[idx]:.3f}) do not intersect: "
                f"the circles are too small or too far apart for an anchor "
                f"span of {d:.3f} mm")
        h = np.sqrt(np.maximum(h_sq, 0.0))

        p_mid = self.anchor_left[None, :] + a[:, None] * u[None, :]
        # Two perpendicular offsets; pick the one on the far side from +Y
        # relative to the anchor line, i.e. the lower solution.
        perp = np.array([u[1], -u[0]], dtype=float)      # rotate -90 deg
        cand_a = p_mid + h[:, None] * perp[None, :]
        cand_b = p_mid - h[:, None] * perp[None, :]
        # Signed distance along the *upward* normal of the anchor line.
        up = np.array([-u[1], u[0]], dtype=float)
        if up[1] < 0.0:
            up = -up
        s_a = (cand_a - self.anchor_left[None, :]) @ up
        s_b = (cand_b - self.anchor_left[None, :]) @ up
        pick_a = (s_a <= s_b)[:, None]
        out = np.where(pick_a, cand_a, cand_b)
        return out.reshape(arr.shape)

    def precompensate(self, point: ArrayLike,
                      reference: ArrayLike | None = None) -> np.ndarray:
        return _as_xy(point).reshape(2).copy()

    def precompensate_many(self, points: ArrayLike,
                           reference: ArrayLike | None = None) -> np.ndarray:
        return _as_xy(points).copy()


# ==========================================================================
# tangent
# ==========================================================================

class TangentKinematics(Kinematics):
    """Physically correct model: tangential belt departure plus pulley wrap.

    See the module docstring for the derivation.  With ``pulley_radius_mm = 0``
    this degenerates exactly into :class:`NaiveKinematics`.
    """

    name = "tangent"

    #: Newton tolerance for :meth:`forward_many`, in millimetres of belt.
    forward_tolerance_mm = 1e-10
    forward_max_iterations = 60

    # -- inverse -----------------------------------------------------------

    def _belt_terms(self, flat: np.ndarray) -> tuple[np.ndarray, ...]:
        """Return ``(v1, d1, alpha1, beta1, v2, d2, alpha2m, beta2)``.

        ``alpha1`` is the raw polar angle at the left pulley; ``alpha2m`` is
        the *mirrored* polar angle at the right pulley, so both sides share
        the same wrap formula.
        """
        r = self.pulley_radius_mm
        v1 = flat - self.anchor_left
        v2 = flat - self.anchor_right
        d1 = np.linalg.norm(v1, axis=1)
        d2 = np.linalg.norm(v2, axis=1)
        if np.any(d1 <= r) or np.any(d2 <= r):
            i = int(np.argmax((d1 <= r) | (d2 <= r)))
            raise UnreachableError(
                f"point {tuple(flat[i])} is inside a pulley "
                f"(distance {min(d1[i], d2[i]):.3f} mm <= radius {r} mm)")
        alpha1 = np.arctan2(v1[:, 1], v1[:, 0])
        alpha2 = np.arctan2(v2[:, 1], v2[:, 0])
        alpha2m = _wrap_to_pi(np.pi - alpha2)
        beta1 = np.arccos(np.clip(r / d1, -1.0, 1.0))
        beta2 = np.arccos(np.clip(r / d2, -1.0, 1.0))
        return v1, d1, alpha1, beta1, v2, d2, alpha2m, beta2

    def inverse_many(self, points: ArrayLike) -> np.ndarray:
        pts = _as_xy(points)
        flat = pts.reshape(-1, 2)
        r = self.pulley_radius_mm
        v1, d1, a1, b1, v2, d2, a2m, b2 = self._belt_terms(flat)
        free1 = np.sqrt(np.maximum(d1 ** 2 - r ** 2, 0.0))
        free2 = np.sqrt(np.maximum(d2 ** 2 - r ** 2, 0.0))
        wrap1 = r * (np.pi - a1 - b1)
        wrap2 = r * (np.pi - a2m - b2)
        return np.stack([free1 + wrap1, free2 + wrap2], axis=1).reshape(pts.shape)

    def belt_jacobian(self, points: ArrayLike) -> np.ndarray:
        r"""Exact ``d(L1,L2)/d(x,y)`` of the tangent model.

        With ``v = P - A``, ``D = |v|``:

        .. math::
            \nabla \ell_{free} &= v / \sqrt{D^2 - r^2} \\
            \nabla \alpha      &= (-v_y, v_x) / D^2 \\
            \nabla \beta       &= r\,v / (D^2 \sqrt{D^2 - r^2}) \\
            \nabla \ell_{wrap} &= -r (\nabla\alpha + \nabla\beta)

        Returns an array of shape ``(N, 2, 2)``; row ``i`` is
        ``[dL_i/dx, dL_i/dy]``.
        """
        flat = _as_xy(points).reshape(-1, 2)
        r = self.pulley_radius_mm
        v1, d1, _, _, v2, d2, _, _ = self._belt_terms(flat)

        def grad(v: np.ndarray, d: np.ndarray, mirrored: bool) -> np.ndarray:
            s = np.sqrt(np.maximum(d ** 2 - r ** 2, 1e-18))
            g_free = v / s[:, None]
            g_alpha = np.stack([-v[:, 1], v[:, 0]], axis=1) / (d ** 2)[:, None]
            if mirrored:
                g_alpha = -g_alpha
            g_beta = (r * v) / ((d ** 2) * s)[:, None]
            return g_free - r * (g_alpha + g_beta)

        j1 = grad(v1, d1, mirrored=False)
        j2 = grad(v2, d2, mirrored=True)
        return np.stack([j1, j2], axis=1)

    # -- forward -----------------------------------------------------------

    def forward_many(self, lengths: ArrayLike) -> np.ndarray:
        """Newton solve, seeded from the naive circle-circle solution.

        The naive seed is within a few millimetres of the true solution over
        the whole workspace, so Newton converges in 2-4 iterations.  If it
        stalls (it should not) the routine falls back to a damped
        Gauss-Newton with backtracking, and raises rather than returning a
        silently wrong point.
        """
        arr = np.asarray(lengths, dtype=float)
        if arr.shape[-1] != 2:
            raise KinematicsError(
                f"expected (...,2) belt lengths, got shape {arr.shape}")
        if not np.all(np.isfinite(arr)):
            raise UnreachableError("belt lengths contain non-finite values")
        flat = arr.reshape(-1, 2)

        naive = NaiveKinematics(self.anchor_left, self.anchor_right,
                                0.0, self.gondola_mass_kg)
        r = self.pulley_radius_mm
        # Seed: strip the (roughly constant) wrap term so the naive solve gets
        # a length that is already close to a centre-to-centre distance.
        seed_lengths = np.maximum(flat - r * (np.pi / 2.0), 1e-6)
        try:
            p = naive.forward_many(seed_lengths).copy()
        except UnreachableError:
            p = naive.forward_many(np.maximum(flat, 1e-6)).copy()

        active = np.ones(len(flat), dtype=bool)
        for _ in range(self.forward_max_iterations):
            if not np.any(active):
                break
            idx = np.flatnonzero(active)
            try:
                resid = self.inverse_many(p[idx]) - flat[idx]
            except UnreachableError:
                # A Newton step landed inside a pulley; pull it back towards
                # the seed and retry.
                p[idx] = 0.5 * (p[idx] + naive.forward_many(seed_lengths[idx]))
                continue
            done = np.max(np.abs(resid), axis=1) < self.forward_tolerance_mm
            if np.any(done):
                active[idx[done]] = False
            todo = idx[~done]
            if todo.size == 0:
                break
            jac = self.belt_jacobian(p[todo])
            det = jac[:, 0, 0] * jac[:, 1, 1] - jac[:, 0, 1] * jac[:, 1, 0]
            if np.any(np.abs(det) < 1e-12):
                bad = todo[np.abs(det) < 1e-12]
                raise UnreachableError(
                    f"tangent forward kinematics is singular near "
                    f"{tuple(p[bad[0]])} — belts are parallel there")
            rr = resid[~done]
            inv = np.empty_like(jac)
            inv[:, 0, 0] = jac[:, 1, 1] / det
            inv[:, 0, 1] = -jac[:, 0, 1] / det
            inv[:, 1, 0] = -jac[:, 1, 0] / det
            inv[:, 1, 1] = jac[:, 0, 0] / det
            step = np.einsum("nij,nj->ni", inv, rr)
            # Trust region: never move more than 5 % of the anchor span at once.
            max_step = 0.05 * self._anchor_span
            norms = np.linalg.norm(step, axis=1)
            scale = np.where(norms > max_step, max_step / np.maximum(norms, 1e-12), 1.0)
            p[todo] = p[todo] - step * scale[:, None]

        if np.any(active):
            resid = self.inverse_many(p[active]) - flat[active]
            worst = float(np.max(np.abs(resid)))
            raise UnreachableError(
                f"tangent forward kinematics did not converge for "
                f"{int(active.sum())} point(s); worst belt residual "
                f"{worst:.6f} mm after {self.forward_max_iterations} iterations")
        return p.reshape(arr.shape)


# ==========================================================================
# factory
# ==========================================================================

def make_kinematics(cfg: MachineConfig,
                    model: Literal["naive", "tangent"] | None = None) -> Kinematics:
    """Build the kinematics selected by the configuration.

    ``model`` overrides ``cfg.geometry.kinematics_model`` (used by the
    calibration solver, which fits both and compares residuals).
    """
    chosen = model or cfg.geometry.kinematics_model
    if chosen == "naive":
        return NaiveKinematics(cfg.geometry.anchor_left,
                               cfg.geometry.anchor_right,
                               cfg.geometry.pulley_pitch_radius_mm,
                               cfg.gondola.mass_kg)
    if chosen == "tangent":
        return TangentKinematics(cfg.geometry.anchor_left,
                                 cfg.geometry.anchor_right,
                                 cfg.geometry.pulley_pitch_radius_mm,
                                 cfg.gondola.mass_kg)
    raise KinematicsError(f"unknown kinematics model {chosen!r}")
