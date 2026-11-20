r"""Least-squares fit of the machine parameters from measured crosses.

The model
---------
We command a cartesian point ``P_cmd``.  FluidNC converts it to belt lengths
with *its configured* (nominal) anchors and steps/mm:

.. math::  L_{fw} = \mathrm{naive\_inverse}(P_{cmd};\ A_0, B_0)

The motors then move ``(L_fw - L_fw^{zero}) \cdot spm_{nominal}`` steps.  The
belt actually pays out those steps divided by the *true* steps/mm, so the real
belt length is an affine function of what the firmware thought it was:

.. math::  L_{real} = c + k \cdot L_{fw}

with one ``(c, k)`` pair per side.  ``k`` absorbs the pulley-diameter error
from 3D-printing tolerance; ``c`` absorbs the unknown belt zero.

Finally the physical pen lands where the *true* geometry says it does:

.. math::  L_{real} = \mathrm{model\_inverse}(P_{measured} - (dx, dy);\ A, B, r)

so the residual for each measured cross is

.. math::  r_j = \mathrm{model\_inverse}(P_j - \delta) - (c + k \odot L_{fw,j})

— two residuals per cross, for ten parameters:
``ax_l, ay_l, ax_r, ay_r, k_L, k_R, c_L, c_R, dx, dy``.

Identifiability
---------------
``c`` and the anchor positions are partly degenerate: a constant belt offset
can trade against moving an anchor.  Two things keep the fit honest:

* targets spread widely over the board (the pattern's job), which makes the
  two effects vary differently;
* the optional **tape-measure reference** from step 1 of the wizard, which
  pins the absolute belt length at one known point and is weighted in.

The solver runs the fit for *both* belt models and returns the one with the
lower residual, rather than assuming the theory applies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal, Sequence

import numpy as np
from scipy.optimize import least_squares

from ..config import MachineConfig
from ..core.kinematics import (Kinematics, NaiveKinematics, TangentKinematics,
                               UnreachableError)

__all__ = [
    "CalibrationMeasurement",
    "BeltReference",
    "CalibrationResult",
    "solve_calibration",
    "simulate_measurements",
]

_PARAM_NAMES = ("ax_l", "ay_l", "ax_r", "ay_r",
                "k_left", "k_right", "c_left", "c_right", "dx", "dy")


@dataclass(frozen=True)
class CalibrationMeasurement:
    """One commanded target and where the pen actually put it."""

    commanded: tuple[float, float]
    measured: tuple[float, float]
    index: int = 0

    def __post_init__(self) -> None:
        for name, p in (("commanded", self.commanded), ("measured", self.measured)):
            if len(p) != 2 or not all(math.isfinite(float(v)) for v in p):
                raise ValueError(f"{name} must be a finite [x, y] pair, got {p!r}")
        # Normalise to plain Python floats at the boundary. numpy scalars pass
        # every check above and then blow up in yaml.safe_dump three steps
        # later, when the calibration is being written to disk.
        object.__setattr__(self, "commanded",
                           (float(self.commanded[0]), float(self.commanded[1])))
        object.__setattr__(self, "measured",
                           (float(self.measured[0]), float(self.measured[1])))
        object.__setattr__(self, "index", int(self.index))

    @property
    def error(self) -> float:
        return math.dist(self.commanded, self.measured)


@dataclass(frozen=True)
class BeltReference:
    """Tape-measured absolute belt lengths at one known gondola position."""

    position: tuple[float, float]
    left_mm: float
    right_mm: float
    #: Relative weight against the cross residuals.
    weight: float = 2.0


@dataclass
class CalibrationResult:
    """The fitted parameters plus an honest account of how well they fit."""

    model: Literal["naive", "tangent"]
    params: dict[str, float]
    converged: bool
    n_measurements: int
    residual_mm: np.ndarray = field(default_factory=lambda: np.zeros(0))
    mean_error_mm: float = 0.0
    max_error_mm: float = 0.0
    rms_error_mm: float = 0.0
    worst_index: int | None = None
    worst_point: tuple[float, float] | None = None
    initial_mean_error_mm: float = 0.0
    initial_max_error_mm: float = 0.0
    alternatives: dict[str, float] = field(default_factory=dict)
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def improvement_percent(self) -> float:
        if self.initial_mean_error_mm <= 1e-9:
            return 0.0
        return 100.0 * (1.0 - self.mean_error_mm / self.initial_mean_error_mm)

    def report(self) -> str:
        lines = [
            f"Calibration fit using the '{self.model}' belt model "
            f"({self.n_measurements} measurements).",
            f"  mean deviation {self.mean_error_mm:.2f} mm, "
            f"max {self.max_error_mm:.2f} mm, RMS {self.rms_error_mm:.2f} mm",
        ]
        if self.worst_point is not None:
            lines.append(
                f"  worst point: target {self.worst_index} at "
                f"({self.worst_point[0]:.0f}, {self.worst_point[1]:.0f}) mm")
        lines.append(
            f"  before calibration: mean {self.initial_mean_error_mm:.2f} mm, "
            f"max {self.initial_max_error_mm:.2f} mm "
            f"({self.improvement_percent:+.0f} %)")
        if self.alternatives:
            alts = ", ".join(f"{k}: {v:.2f} mm" for k, v in self.alternatives.items())
            lines.append(f"  model comparison (mean deviation) -> {alts}")
        lines.append("  fitted parameters:")
        for name in _PARAM_NAMES:
            if name in self.params:
                lines.append(f"    {name:<9} = {self.params[name]:.4f}")
        for w in self.warnings:
            lines.append(f"  ! {w}")
        if not self.converged:
            lines.append("  ! the optimiser did NOT converge — treat with suspicion")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "params": {k: round(v, 6) for k, v in self.params.items()},
            "converged": self.converged,
            "n_measurements": self.n_measurements,
            "mean_error_mm": round(self.mean_error_mm, 4),
            "max_error_mm": round(self.max_error_mm, 4),
            "rms_error_mm": round(self.rms_error_mm, 4),
            "worst_index": self.worst_index,
            "worst_point": ([round(v, 2) for v in self.worst_point]
                            if self.worst_point else None),
            "initial_mean_error_mm": round(self.initial_mean_error_mm, 4),
            "initial_max_error_mm": round(self.initial_max_error_mm, 4),
            "improvement_percent": round(self.improvement_percent, 1),
            "alternatives": {k: round(v, 4) for k, v in self.alternatives.items()},
            "message": self.message,
            "warnings": list(self.warnings),
            "report": self.report(),
        }

    def to_calibration_yaml(self, cfg: MachineConfig) -> dict:
        """Overlay dict for ``config/calibration.yaml``."""
        from ..config import to_plain
        p = self.params
        nominal = cfg.drive.nominal_steps_per_mm
        return {
            "geometry": {
                "anchor_left": [round(p["ax_l"], 3), round(p["ay_l"], 3)],
                "anchor_right": [round(p["ax_r"], 3), round(p["ay_r"], 3)],
                "kinematics_model": self.model,
            },
            "drive": {
                # k = spm_nominal / spm_true  =>  spm_true = spm_nominal / k
                "steps_per_mm_left": round(nominal / p["k_left"], 5),
                "steps_per_mm_right": round(nominal / p["k_right"], 5),
                "belt_zero_left_mm": round(p["c_left"], 4),
                "belt_zero_right_mm": round(p["c_right"], 4),
            },
            "gondola": {
                "pen_offset_mm": [round(p["dx"], 3), round(p["dy"], 3)],
            },
            "report": to_plain(self.to_dict()),
        }


# --------------------------------------------------------------------------

def _make_kinematics(model: str, params: np.ndarray, radius: float,
                     mass: float) -> Kinematics:
    cls = TangentKinematics if model == "tangent" else NaiveKinematics
    return cls((params[0], params[1]), (params[2], params[3]), radius, mass)


def _residuals(params: np.ndarray, model: str,
               commanded_lengths: np.ndarray,
               measured: np.ndarray,
               radius: float, mass: float,
               reference: BeltReference | None,
               nominal_naive: NaiveKinematics) -> np.ndarray:
    ax_l, ay_l, ax_r, ay_r, k_l, k_r, c_l, c_r, dx, dy = params
    if ax_r - ax_l < 1.0:
        return np.full(len(measured) * 2 + (2 if reference else 0), 1e6)
    try:
        kin = _make_kinematics(model, params, radius, mass)
        gondola = measured - np.array([dx, dy])
        predicted = kin.inverse_many(gondola)
    except (UnreachableError, ValueError):
        return np.full(len(measured) * 2 + (2 if reference else 0), 1e6)

    expected = np.stack([c_l + k_l * commanded_lengths[:, 0],
                         c_r + k_r * commanded_lengths[:, 1]], axis=1)
    res = (predicted - expected).ravel()

    if reference is not None:
        try:
            ref_pos = np.array(reference.position, dtype=float) - np.array([dx, dy])
            ref_pred = kin.inverse_many(ref_pos.reshape(1, 2))[0]
        except (UnreachableError, ValueError):
            return np.concatenate([res, np.full(2, 1e6)])
        w = math.sqrt(max(reference.weight, 0.0))
        res = np.concatenate([
            res,
            w * np.array([ref_pred[0] - reference.left_mm,
                          ref_pred[1] - reference.right_mm]),
        ])
    return res


def _position_errors(params: np.ndarray, model: str,
                     measurements: Sequence[CalibrationMeasurement],
                     commanded_lengths: np.ndarray,
                     radius: float, mass: float) -> np.ndarray:
    """Where would the pen land if we applied these parameters?

    This is the number the user cares about — millimetres on the board, not
    millimetres of belt.
    """
    ax_l, ay_l, ax_r, ay_r, k_l, k_r, c_l, c_r, dx, dy = params
    kin = _make_kinematics(model, params, radius, mass)
    expected = np.stack([c_l + k_l * commanded_lengths[:, 0],
                         c_r + k_r * commanded_lengths[:, 1]], axis=1)
    try:
        predicted_gondola = kin.forward_many(expected)
    except UnreachableError:
        return np.full(len(measurements), np.inf)
    predicted_pen = predicted_gondola + np.array([dx, dy])
    measured = np.array([m.measured for m in measurements], dtype=float)
    return np.linalg.norm(predicted_pen - measured, axis=1)


def solve_calibration(measurements: Sequence[CalibrationMeasurement],
                      cfg: MachineConfig, *,
                      model: Literal["naive", "tangent", "auto"] = "auto",
                      reference: BeltReference | None = None,
                      fit_pen_offset: bool = True,
                      fit_scale: bool = True,
                      fit_belt_zero: bool = True,
                      max_anchor_shift_mm: float = 150.0) -> CalibrationResult:
    """Fit the machine parameters. Returns the better of the two belt models."""
    measurements = list(measurements)
    if len(measurements) < 4:
        raise ValueError(
            f"at least 4 measurements are needed to fit the machine "
            f"parameters, got {len(measurements)}")

    a0 = np.array(cfg.geometry.anchor_left, dtype=float)
    b0 = np.array(cfg.geometry.anchor_right, dtype=float)
    radius = cfg.geometry.pulley_pitch_radius_mm
    mass = cfg.gondola.mass_kg

    nominal_naive = NaiveKinematics(a0, b0, 0.0, mass)
    commanded = np.array([m.commanded for m in measurements], dtype=float)
    measured = np.array([m.measured for m in measurements], dtype=float)
    commanded_lengths = nominal_naive.inverse_many(commanded)

    initial_err = np.linalg.norm(measured - commanded, axis=1)

    x0 = np.array([a0[0], a0[1], b0[0], b0[1], 1.0, 1.0, 0.0, 0.0, 0.0, 0.0])
    lo = np.array([a0[0] - max_anchor_shift_mm, a0[1] - max_anchor_shift_mm,
                   b0[0] - max_anchor_shift_mm, b0[1] - max_anchor_shift_mm,
                   0.90, 0.90, -400.0, -400.0, -60.0, -60.0])
    hi = np.array([a0[0] + max_anchor_shift_mm, a0[1] + max_anchor_shift_mm,
                   b0[0] + max_anchor_shift_mm, b0[1] + max_anchor_shift_mm,
                   1.10, 1.10, 400.0, 400.0, 60.0, 60.0])
    if not fit_scale:
        lo[4] = lo[5] = 1.0 - 1e-9
        hi[4] = hi[5] = 1.0 + 1e-9
    if not fit_belt_zero:
        # The corner method has too few points to separate the belt zero from
        # the anchor positions; freezing it keeps the fit well-determined.
        lo[6] = lo[7] = -1e-9
        hi[6] = hi[7] = 1e-9
    if not fit_pen_offset:
        lo[8] = lo[9] = -1e-9
        hi[8] = hi[9] = 1e-9

    candidates = ("naive", "tangent") if model == "auto" else (model,)
    results: dict[str, tuple[CalibrationResult, float]] = {}

    for candidate in candidates:
        try:
            sol = least_squares(
                _residuals, x0, bounds=(lo, hi),
                args=(candidate, commanded_lengths, measured, radius, mass,
                      reference, nominal_naive),
                method="trf", loss="soft_l1", f_scale=1.0,
                max_nfev=4000, xtol=1e-12, ftol=1e-12, gtol=1e-12)
        except Exception as exc:                       # pragma: no cover
            results[candidate] = (CalibrationResult(
                model=candidate, params={}, converged=False,
                n_measurements=len(measurements),
                message=f"optimiser failed: {exc}"), math.inf)
            continue

        errors = _position_errors(sol.x, candidate, measurements,
                                  commanded_lengths, radius, mass)
        finite = errors[np.isfinite(errors)]
        mean_err = float(finite.mean()) if finite.size else math.inf
        worst = int(np.argmax(errors)) if errors.size else None

        warnings: list[str] = []
        if abs(sol.x[4] - 1.0) > 0.05 or abs(sol.x[5] - 1.0) > 0.05:
            warnings.append(
                f"steps/mm correction is large (left {sol.x[4]:.4f}, right "
                f"{sol.x[5]:.4f}); check the pulley tooth count and microstepping")
        shift_l = float(np.linalg.norm(sol.x[0:2] - a0))
        shift_r = float(np.linalg.norm(sol.x[2:4] - b0))
        if shift_l > 50.0 or shift_r > 50.0:
            warnings.append(
                f"anchors moved a long way from their nominal positions "
                f"(left {shift_l:.0f} mm, right {shift_r:.0f} mm); "
                f"re-check the measurements before trusting this")
        if reference is None:
            warnings.append(
                "no tape-measure belt reference was supplied; the belt zero "
                "and the anchor positions are only weakly separated")
        if len(measurements) < 9:
            warnings.append(
                f"only {len(measurements)} measurements for 10 parameters — "
                f"a 3x3 pattern or larger is strongly recommended")

        result = CalibrationResult(
            model=candidate,
            params={name: float(v) for name, v in zip(_PARAM_NAMES, sol.x)},
            converged=bool(sol.success),
            n_measurements=len(measurements),
            residual_mm=errors,
            mean_error_mm=mean_err,
            max_error_mm=float(finite.max()) if finite.size else math.inf,
            rms_error_mm=float(np.sqrt((finite ** 2).mean())) if finite.size
            else math.inf,
            worst_index=(measurements[worst].index if worst is not None else None),
            worst_point=(measurements[worst].measured if worst is not None else None),
            initial_mean_error_mm=float(initial_err.mean()),
            initial_max_error_mm=float(initial_err.max()),
            message=str(sol.message),
            warnings=warnings,
        )
        results[candidate] = (result, mean_err)

    best_name = min(results, key=lambda k: results[k][1])
    best = results[best_name][0]
    best.alternatives = {k: v for k, (_, v) in results.items()}

    if len(results) > 1:
        other = [v for k, v in best.alternatives.items() if k != best_name]
        if other and abs(other[0] - best.mean_error_mm) < 0.05:
            best.warnings.append(
                f"the two belt models fit equally well "
                f"({best.mean_error_mm:.2f} vs {other[0]:.2f} mm); the pulley "
                f"wrap is not resolvable from this data — keep the simpler "
                f"'naive' model unless a later measurement disagrees")
    if best.mean_error_mm > 2.0:
        best.warnings.append(
            f"mean deviation {best.mean_error_mm:.2f} mm exceeds the 2 mm "
            f"target — re-measure, or check for a mechanical problem")
    return best


# --------------------------------------------------------------------------

def simulate_measurements(cfg: MachineConfig,
                          targets: Sequence[Sequence[float]],
                          *,
                          true_params: dict[str, float],
                          model: Literal["naive", "tangent"] = "tangent",
                          noise_mm: float = 0.0,
                          seed: int = 0) -> list[CalibrationMeasurement]:
    """Generate synthetic measurements from known-true parameters.

    Used by the tests to prove the solver recovers what it should, and by the
    UI to preview what a given error would look like.
    """
    a0 = np.array(cfg.geometry.anchor_left, dtype=float)
    b0 = np.array(cfg.geometry.anchor_right, dtype=float)
    radius = cfg.geometry.pulley_pitch_radius_mm
    mass = cfg.gondola.mass_kg

    p = np.array([
        true_params.get("ax_l", a0[0]), true_params.get("ay_l", a0[1]),
        true_params.get("ax_r", b0[0]), true_params.get("ay_r", b0[1]),
        true_params.get("k_left", 1.0), true_params.get("k_right", 1.0),
        true_params.get("c_left", 0.0), true_params.get("c_right", 0.0),
        true_params.get("dx", 0.0), true_params.get("dy", 0.0),
    ], dtype=float)

    nominal_naive = NaiveKinematics(a0, b0, 0.0, mass)
    commanded = np.array(targets, dtype=float)
    cmd_lengths = nominal_naive.inverse_many(commanded)
    real_lengths = np.stack([p[6] + p[4] * cmd_lengths[:, 0],
                             p[7] + p[5] * cmd_lengths[:, 1]], axis=1)

    kin = _make_kinematics(model, p, radius, mass)
    gondola = kin.forward_many(real_lengths)
    pen = gondola + np.array([p[8], p[9]])

    if noise_mm > 0:
        rng = np.random.default_rng(seed)
        pen = pen + rng.normal(0.0, noise_mm, size=pen.shape)

    return [CalibrationMeasurement(commanded=tuple(c), measured=tuple(m),
                                   index=i + 1)
            for i, (c, m) in enumerate(zip(commanded, pen))]
