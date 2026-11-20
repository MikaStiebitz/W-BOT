"""Geometry calibration: fit anchors / belt scale / ... from ruler measurements of drawn crosses.

Model. The host aims at a physical target ``t`` and sends the firmware coordinate
``F_t = Mapping.from_config(cfg, reference, ref_fw).to_firmware(t)`` - computed with the
*believed* geometry. FluidNC turns ``F_t`` into belt-length changes ``dLfw = Lfw(F_t) - Lfw(F0)``
(its naive model with the flashed anchors, which this module never changes). Reality is a
``TrueModel`` with unknown parameters theta, so the pen really lands at

    P_true = TrueModel_theta.position( L_true_theta(reference + delta_ref) + dLfw * scale_theta )

``dLfw`` does not depend on theta, so it is computed once and the fit is a small non-linear
least-squares problem (``scipy.optimize.least_squares``, soft-L1 loss, bounds, weak prior).

Identifiability (honest limits):
* ``pen_offset`` is *exactly* degenerate with a common shift of both anchors (the reference is a
  pen position), so it is dropped whenever both anchors are free.
* ``reference`` (the believed reference was off) shifts the whole drawing rigidly; it competes with
  the anchors, so it is not part of the default set.
* ``radius`` correlates with the belt scales; use it only with many, well spread points.
* ``com_height`` / ``com_x`` (gondola V2 centre of mass, drives the tilt) are optional groups, see the
  gondola tests for how well crosses identify them.
* Points must be spread over the board; the Jacobian's condition number is reported and checked.

Targets: ``suggest_targets`` gives the grid; the ``crosshair-points`` pattern with
``cols=n_x, rows=n_y, arm_mm=inset_mm`` draws crosses with exactly these centres.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping as MappingT, Sequence

import numpy as np
from scipy.optimize import least_squares

from .config import Config, deep_update
from .kinematics import KinematicsError, Mapping, true_model_from_config

log = logging.getLogger(__name__)

XY = tuple[float, float]

#: nominal measurement noise (mm per axis) used to weight the weak prior
PRIOR_NOISE_MM = 0.5
MAX_POINTS = 500
COND_WARN = 150.0                 # condition number of the (mm-scaled) Jacobian
STD_WARN_MM = 1.5                 # parameter standard error (mm-equivalent) above which we warn
OUTLIER_MIN_MM = 3.0


class CalibrationError(ValueError):
    """Bad input for the calibration fit (message is German, shown to the user)."""


@dataclass(frozen=True)
class Measurement:
    """``target``: physical point the host aimed at. ``measured``: where the pen really was (board mm)."""
    target: tuple[float, float]
    measured: tuple[float, float]


# --------------------------------------------------------------------------- parameter table
@dataclass(frozen=True)
class _Comp:
    name: str            # flat parameter key
    group: str           # user facing group
    nominal: float
    lo: float
    hi: float
    k: float             # internal x = (value - nominal) * k   (scales use permille so 1 unit ~ 1 mm)
    sigma: float         # prior std (value units)


_GROUPS: dict[str, tuple[str, ...]] = {
    "anchor_left": ("al_x", "al_y"), "anchor_right": ("ar_x", "ar_y"),
    "scale": ("sl", "sr"), "scale_left": ("sl",), "scale_right": ("sr",),
    "pen_offset": ("pen_x", "pen_y"), "radius": ("r",), "reference": ("ref_x", "ref_y"),
    "com_height": ("hc",), "com_x": ("cx",),          # gondola V2 centre of mass (tilt); see module docstring
}
DEFAULT_FREE = ("anchor_left", "anchor_right", "scale")
_LABEL = {"al_x": "Anker links x", "al_y": "Anker links y", "ar_x": "Anker rechts x", "ar_y": "Anker rechts y",
          "sl": "Maßstab links", "sr": "Maßstab rechts", "pen_x": "Stiftversatz x", "pen_y": "Stiftversatz y",
          "r": "Riemenscheiben-Radius", "ref_x": "Referenz x", "ref_y": "Referenz y",
          "hc": "Schwerpunkt-Höhe", "cx": "Schwerpunkt x"}


def _nominal(cfg: Config) -> dict[str, float]:
    g = cfg.geometry
    return {"al_x": g.anchor_left[0], "al_y": g.anchor_left[1], "ar_x": g.anchor_right[0], "ar_y": g.anchor_right[1],
            "sl": g.belt_scale_left, "sr": g.belt_scale_right, "pen_x": g.pen_offset_mm[0], "pen_y": g.pen_offset_mm[1],
            "r": 0.0 if g.model == "naive" else g.pulley_pitch_radius_mm, "ref_x": 0.0, "ref_y": 0.0,
            "hc": g.com_height_mm, "cx": g.com_offset_x_mm}


def _comp(name: str, nom: dict[str, float]) -> _Comp:
    v = nom[name]
    if name[:2] in ("al", "ar"):
        return _Comp(name, "anchor", v, v - 30, v + 30, 1.0, 15.0)
    if name in ("sl", "sr"):
        return _Comp(name, "scale", v, min(0.97, v), max(1.03, v), 1000.0, 0.015)
    if name.startswith("pen"):
        return _Comp(name, "pen", v, v - 20, v + 20, 1.0, 10.0)
    if name == "r":
        return _Comp(name, "radius", v, max(v - 3.0, 0.5), v + 3.0, 1.0, 2.0)
    if name == "hc":
        return _Comp(name, "com", v, max(v - 25.0, 1.0), v + 40.0, 1.0, 15.0)
    if name == "cx":
        return _Comp(name, "com", v, v - 8.0, v + 8.0, 1.0, 4.0)
    return _Comp(name, "ref", v, v - 10, v + 10, 1.0, 5.0)


def _flatten(cfg: Config, tp: MappingT[str, object] | None) -> dict[str, float]:
    """Friendly ``true_params`` -> flat parameter dict (missing entries = the configured values)."""
    p = _nominal(cfg)
    for key, val in (tp or {}).items():
        if key == "anchor_left":
            p["al_x"], p["al_y"] = float(val[0]), float(val[1])
        elif key == "anchor_right":
            p["ar_x"], p["ar_y"] = float(val[0]), float(val[1])
        elif key in ("belt_scale_left", "scale_left"):
            p["sl"] = float(val)
        elif key in ("belt_scale_right", "scale_right"):
            p["sr"] = float(val)
        elif key in ("pen_offset", "pen_offset_mm"):
            p["pen_x"], p["pen_y"] = float(val[0]), float(val[1])
        elif key in ("radius", "pulley_pitch_radius_mm"):
            p["r"] = float(val)
        elif key in ("com_height", "com_height_mm"):
            p["hc"] = float(val)
        elif key in ("com_x", "com_offset_x_mm"):
            p["cx"] = float(val)
        elif key == "reference_correction":
            p["ref_x"], p["ref_y"] = float(val[0]), float(val[1])
        else:
            raise ValueError(f"unbekannter Geometrie-Parameter '{key}'")
    return p


class _Setup:
    """The theta-independent part: believed firmware coordinates and their belt-length changes."""

    def __init__(self, cfg: Config, reference: XY, ref_fw: XY | None, targets: np.ndarray):
        self.cfg = cfg
        self.ref = np.asarray(reference, float)
        self.t = np.asarray(targets, float).reshape(-1, 2)
        m0 = Mapping.from_config(cfg, (float(reference[0]), float(reference[1])),
                                 None if ref_fw is None else (float(ref_fw[0]), float(ref_fw[1])))
        try:
            fw_pts = m0.to_firmware(self.t)
        except KinematicsError as exc:
            raise CalibrationError(f"Ein Messpunkt ist mit dieser Referenz nicht anfahrbar: {exc}") from exc
        self.dl = m0.fw.lengths(fw_pts) - m0.fw.lengths(m0.ref_fw)

    def predict(self, p: MappingT[str, float]) -> np.ndarray:
        """Where the pen really lands for the targets under parameters ``p`` (flat dict)."""
        truth = true_model_from_config(self.cfg, anchor_left=(p["al_x"], p["al_y"]), anchor_right=(p["ar_x"], p["ar_y"]),
                                       radius=p["r"], pen_offset=(p["pen_x"], p["pen_y"]),
                                       com_height=p["hc"], com_x=p["cx"])
        l0 = truth.lengths(self.ref + (p["ref_x"], p["ref_y"]))
        lt = l0 + self.dl * np.array([p["sl"], p["sr"]])
        return truth.position(lt, seed=self.t)


def predicted_positions(cfg: Config, reference: XY, ref_fw: XY | None, targets: Iterable[XY],
                        true_params: MappingT[str, object] | None = None) -> np.ndarray:
    """Pen positions (N,2) the machine reaches for ``targets`` when the host used ``cfg`` but the
    real geometry is ``true_params`` (keys: anchor_left, anchor_right, belt_scale_left,
    belt_scale_right, pen_offset, radius, reference_correction; missing = as configured)."""
    t = np.asarray(list(targets), float).reshape(-1, 2)
    return _Setup(cfg, reference, ref_fw, t).predict(_flatten(cfg, true_params))


def suggest_targets(cfg: Config, n_x: int = 5, n_y: int = 4, inset_mm: float = 20.0) -> list[XY]:
    """Well spread grid inside ``cfg.safe_rect()`` (inset, corners included). Order: x outer, y inner -
    the same order as the ``crosshair-points`` pattern with ``cols=n_x, rows=n_y, arm_mm=inset_mm``."""
    if n_x < 2 or n_y < 2:
        raise CalibrationError("Mindestens 2 x 2 Messpunkte.")
    x0, y0, x1, y1 = cfg.safe_rect()
    xs = np.linspace(x0 + inset_mm, x1 - inset_mm, n_x)
    ys = np.linspace(y0 + inset_mm, y1 - inset_mm, n_y)
    return [(float(x), float(y)) for x in xs for y in ys]


# --------------------------------------------------------------------------- result
@dataclass
class CalibResult:
    patch: dict
    rms_before: float
    rms_after: float
    max_before: float
    max_after: float
    residuals: list[float]                      # |measured - model| after the fit, per input point (mm)
    warnings: list[str]
    free: list[str]
    n: int
    improved: bool = False
    reference_correction: tuple[float, float] = (0.0, 0.0)
    residual_vectors: list[list[float]] = field(default_factory=list)
    residuals_before: list[float] = field(default_factory=list)
    outliers: list[int] = field(default_factory=list)
    params: dict = field(default_factory=dict)  # fitted absolute values (also when no patch is advised)
    std: dict = field(default_factory=dict)     # standard errors of the free parameters (value units)
    condition_number: float | None = None

    def to_dict(self) -> dict:
        def f(v):
            return None if v is None else float(v)
        return {"patch": self.patch, "rms_before": f(self.rms_before), "rms_after": f(self.rms_after),
                "max_before": f(self.max_before), "max_after": f(self.max_after),
                "residuals": [float(v) for v in self.residuals],
                "residuals_before": [float(v) for v in self.residuals_before],
                "residual_vectors": [[float(a), float(b)] for a, b in self.residual_vectors],
                "warnings": list(self.warnings), "free": list(self.free), "n": int(self.n),
                "improved": bool(self.improved), "outliers": [int(i) for i in self.outliers],
                "reference_correction": [float(self.reference_correction[0]), float(self.reference_correction[1])],
                "params": self.params, "std": self.std, "condition_number": f(self.condition_number)}


def _rms(r: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.sum(r ** 2, axis=1)))) if len(r) else 0.0


def _max(r: np.ndarray) -> float:
    return float(np.max(np.linalg.norm(r, axis=1))) if len(r) else 0.0


def _resolve_free(free: Sequence[str] | None, n: int, warnings: list[str]) -> list[str]:
    if free is None:
        if n >= 6:
            free = DEFAULT_FREE
        elif n >= 3:
            free = ("scale",)
            warnings.append(f"Nur {n} Messpunkte: es wird nur der Riemen-Maßstab angepasst. "
                            "Für Anker-Korrektur mindestens 6, besser 12 bis 20 Punkte messen.")
        else:
            free = ()
    names = list(dict.fromkeys(free))
    for nm in names:
        if nm not in _GROUPS:
            raise CalibrationError(f"Unbekannter freier Parameter '{nm}' (erlaubt: {', '.join(_GROUPS)}).")
    if "scale_left" in names and "scale_right" in names:
        names = [x for x in names if x not in ("scale_left", "scale_right")] + ["scale"]
    if "scale" in names:
        names = [x for x in names if x not in ("scale_left", "scale_right")]
    if "pen_offset" in names and "anchor_left" in names and "anchor_right" in names:
        names.remove("pen_offset")
        warnings.append("Der Stiftversatz ist von einer gemeinsamen Verschiebung beider Anker nicht zu "
                        "unterscheiden und wird nicht separat angepasst (Anker sind frei).")
    return names


def _numjac(fun, x: np.ndarray, h: float = 1e-3) -> np.ndarray:
    cols = []
    for i in range(len(x)):
        d = np.zeros_like(x)
        d[i] = h
        cols.append((fun(x + d) - fun(x - d)) / (2 * h))
    return np.stack(cols, axis=1)


def _solve(setup: _Setup, meas: np.ndarray, mask: np.ndarray, comps: list[_Comp], nom: dict[str, float],
           loss_scale: float):
    def params(x: np.ndarray) -> dict[str, float]:
        p = dict(nom)
        for c, xi in zip(comps, x):
            p[c.name] = c.nominal + xi / c.k
        return p

    def data_res(x: np.ndarray) -> np.ndarray:
        try:
            pred = setup.predict(params(x))
        except KinematicsError:
            return np.full(int(mask.sum()) * 2, 1e3)
        return (meas - pred)[mask].ravel()

    prior_w = np.array([PRIOR_NOISE_MM / (c.sigma * c.k) for c in comps])

    def full_res(x: np.ndarray) -> np.ndarray:
        return np.concatenate([data_res(x), prior_w * x])

    x0 = np.zeros(len(comps))
    lo = np.array([(c.lo - c.nominal) * c.k for c in comps])
    hi = np.array([(c.hi - c.nominal) * c.k for c in comps])
    if len(comps):
        sol = least_squares(full_res, x0, bounds=(lo, hi), loss="soft_l1", f_scale=loss_scale,
                            diff_step=1e-3, max_nfev=300)
        x = sol.x
    else:
        x = x0
    return x, params(x), data_res, prior_w, lo, hi


def fit_geometry(cfg: Config, reference: XY, ref_fw: XY | None, measurements: Sequence[Measurement],
                 free: Sequence[str] | None = None, *, min_rms_mm: float = 0.6,
                 loss_scale_mm: float = 1.0) -> CalibResult:
    """Fit geometry parameters so the model reproduces the measured positions.

    ``free``: groups from ``anchor_left, anchor_right, scale, scale_left, scale_right, pen_offset,
    radius, reference`` (default: anchors + scale for >= 6 points, only scale for 3..5).
    ``min_rms_mm``: below this the measurement error is within ruler accuracy -> no patch."""
    meas_list = list(measurements)
    n = len(meas_list)
    if n > MAX_POINTS:
        raise CalibrationError(f"Zu viele Messpunkte (max. {MAX_POINTS}).")
    warnings: list[str] = []
    try:
        tgt = np.array([m.target for m in meas_list], float).reshape(-1, 2)
        mea = np.array([m.measured for m in meas_list], float).reshape(-1, 2)
    except (TypeError, ValueError) as exc:
        raise CalibrationError("Messpunkte ungültig (erwartet: Zielpunkt und gemessener Punkt je [x, y]).") from exc
    if not (np.all(np.isfinite(tgt)) and np.all(np.isfinite(mea))):
        raise CalibrationError("Messpunkte enthalten ungültige Zahlen.")
    names = _resolve_free(free, n, warnings)
    nom = _nominal(cfg)
    if "radius" in names and nom["r"] <= 0.0:
        names.remove("radius")
        warnings.append("Radius wird im Modus 'naive' nicht angepasst.")

    def empty(extra: str | None = None) -> CalibResult:
        if extra:
            warnings.append(extra)
        before = [] if n == 0 else list(np.linalg.norm(mea - tgt, axis=1))
        r0 = mea - tgt
        return CalibResult({}, _rms(r0), _rms(r0), _max(r0), _max(r0), before, warnings, names, n,
                           residuals_before=before)

    if n < 3 or not names:
        return empty(f"Zu wenige Messpunkte ({n}): mindestens 3, empfohlen 12 bis 20 über das ganze Board verteilt.")
    comps = [_comp(nm, nom) for g in names for nm in _GROUPS[g]]
    if len(comps) >= 2 * n:
        return empty(f"{len(comps)} freie Parameter bei nur {n} Messpunkten: unterbestimmt, keine Anpassung.")
    if len(comps) > 2 * n - 4:
        warnings.append(f"Wenige Messpunkte für {len(comps)} freie Parameter - das Ergebnis ist unsicher.")

    x0, y0, x1, y1 = cfg.safe_rect()
    if np.ptp(tgt[:, 0]) < 0.4 * (x1 - x0) or np.ptp(tgt[:, 1]) < 0.4 * (y1 - y0):
        warnings.append("Die Messpunkte decken weniger als 40 % der Boardbreite oder -höhe ab - "
                        "Messpunkte besser über das ganze Board verteilen.")

    setup = _Setup(cfg, reference, ref_fw, tgt)
    pred0 = setup.predict(nom)
    res_before = mea - pred0
    mask = np.ones(n, bool)
    x, p, data_res, prior_w, lo, hi = _solve(setup, mea, mask, comps, nom, loss_scale_mm)

    # --- robust outlier handling: flag gross errors (typos), refit without them
    r_all = np.linalg.norm(mea - setup.predict(p), axis=1)
    sigma = max(float(np.median(r_all)) / 1.1774, 0.2)
    bad = np.where(r_all > max(OUTLIER_MIN_MM, 5.0 * sigma))[0]
    if len(bad) and n - len(bad) >= max(3, len(comps) // 2 + 1) and len(bad) <= max(1, n // 4):
        mask[bad] = False
        x, p, data_res, prior_w, lo, hi = _solve(setup, mea, mask, comps, nom, loss_scale_mm)
        warnings.append("Messpunkt " + ", ".join(str(int(i) + 1) for i in bad) + " weicht stark ab "
                        f"({', '.join(f'{r_all[i]:.1f}' for i in bad)} mm) und wurde ignoriert - Tippfehler?")
    outliers = [int(i) for i in np.where(~mask)[0]]

    pred = setup.predict(p)
    res_after = mea - pred
    used = mask
    rms_b, rms_a = _rms(res_before[used]), _rms(res_after[used])
    max_b, max_a = _max(res_before[used]), _max(res_after[used])

    # --- conditioning / parameter uncertainty
    cond: float | None = None
    std: dict[str, float] = {}
    if len(comps):
        jac = _numjac(data_res, x)
        sv = np.linalg.svd(jac, compute_uv=False)
        cond = float(sv[0] / sv[-1]) if sv[-1] > 1e-12 else math.inf
        sig = max(rms_a / math.sqrt(2.0), 0.3)
        cov = np.linalg.pinv(jac.T @ jac + np.diag(prior_w ** 2)) * sig ** 2
        sd = np.sqrt(np.maximum(np.diag(cov), 0.0))
        for c, s_x in zip(comps, sd):
            std[c.name] = float(s_x / c.k)
            if s_x > STD_WARN_MM:
                warnings.append(f"{_LABEL[c.name]} ist nur auf etwa ±{s_x:.1f} mm (Wirkung am Stift) bestimmt (zu wenige oder "
                                "ungünstig verteilte Messpunkte).")
        if cond > COND_WARN or not math.isfinite(cond):
            warnings.append(f"Schlecht konditionierte Anpassung (Konditionszahl {cond:.0f}): die Messpunkte "
                            "bestimmen die Parameter nur ungenau. Mehr Punkte, besser verteilt, oder weniger "
                            "freie Parameter wählen.")
    at_bound = [c for c, xi, a, b in zip(comps, x, lo, hi) if min(xi - a, b - xi) < 1e-3 * (b - a)]
    if at_bound:
        warnings.append("Parameter am Grenzwert: " + ", ".join(_LABEL[c.name] for c in at_bound) +
                        ". Vermutlich ein Messfehler oder ein nicht modellierter Fehler.")

    # --- decision: never hand out a patch that does not clearly help
    improved = rms_b >= min_rms_mm and rms_a <= 0.85 * rms_b and rms_b - rms_a >= 0.2
    if not improved:
        if rms_b < min_rms_mm:
            warnings.append(f"Die Abweichung ({rms_b:.2f} mm rms) liegt im Bereich der Messgenauigkeit - "
                            "keine Änderung nötig.")
        else:
            warnings.append(f"Die Anpassung verbessert die Messwerte nicht nennenswert ({rms_b:.2f} → "
                            f"{rms_a:.2f} mm rms) - Konfiguration bleibt unverändert.")
    patch = _build_patch(cfg, names, p) if improved else {}
    if patch:
        try:
            deep_update(cfg, patch)
        except Exception as exc:  # noqa: BLE001 - pydantic validation error
            warnings.append(f"Das berechnete Ergebnis ist ungültig ({exc}) - keine Änderung.")
            patch, improved = {}, False

    full = {c.name: float(p[c.name]) for c in comps}
    ref_corr = (float(p["ref_x"]), float(p["ref_y"])) if "reference" in names else (0.0, 0.0)
    log.info("calibration fit n=%d free=%s rms %.3f -> %.3f mm improved=%s", n, names, rms_b, rms_a, improved)
    return CalibResult(patch, rms_b, rms_a, max_b, max_a, list(np.linalg.norm(res_after, axis=1)), warnings, names,
                       n, improved, ref_corr if improved else (0.0, 0.0), [list(map(float, v)) for v in res_after],
                       list(np.linalg.norm(res_before, axis=1)), outliers, full, std, cond)


def _build_patch(cfg: Config, names: list[str], p: dict[str, float]) -> dict:
    geo: dict = {}
    patch: dict = {"geometry": geo}
    if "anchor_left" in names:
        geo["anchor_left"] = [round(float(p["al_x"]), 3), round(float(p["al_y"]), 3)]
    if "anchor_right" in names:
        geo["anchor_right"] = [round(float(p["ar_x"]), 3), round(float(p["ar_y"]), 3)]
    if "anchor_left" in names or "anchor_right" in names:
        # the firmware anchors are derived from the geometry unless pinned: keep what is flashed
        patch["firmware"] = {"anchor_left": [float(v) for v in cfg.fw_anchor_left],
                             "anchor_right": [float(v) for v in cfg.fw_anchor_right]}
    if "scale" in names or "scale_left" in names:
        geo["belt_scale_left"] = round(float(p["sl"]), 6)
    if "scale" in names or "scale_right" in names:
        geo["belt_scale_right"] = round(float(p["sr"]), 6)
    if "pen_offset" in names:
        geo["pen_offset_mm"] = [round(float(p["pen_x"]), 3), round(float(p["pen_y"]), 3)]
    if "radius" in names:
        geo["pulley_pitch_radius_mm"] = round(float(p["r"]), 4)
    if "com_height" in names:
        geo["com_height_mm"] = round(float(p["hc"]), 3)
    if "com_x" in names:
        geo["com_offset_x_mm"] = round(float(p["cx"]), 3)
    if not geo:
        return {}
    return patch
