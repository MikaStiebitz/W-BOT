"""Plain functions behind the calibration endpoints (the API layer only registers them).

``body`` format (fit and apply)::

    {"measurements": [{"target": [x, y], "measured": [x, y]}, ...],   # board mm, origin lower-left
     "free": ["anchor_left", "anchor_right", "scale"] | null}          # see wbot.calibration.fit_geometry

Errors are raised as ``MachineError`` (``code`` "bad_request" / "no_reference"), like the rest of the API.
"""
from __future__ import annotations

import logging
import math
from typing import Any

from .calibration import CalibrationError, CalibResult, Measurement, fit_geometry, suggest_targets
from .kinematics import KinematicsError, Mapping
from .machine import Machine, MachineError

log = logging.getLogger(__name__)

MAX_COORD = 1.0e5


def calibration_targets(machine: Machine, n_x: int = 5, n_y: int = 4, inset_mm: float = 20.0) -> dict:
    """Grid of crosses to draw and measure. ``pattern`` says how to draw exactly these centres."""
    try:
        pts = suggest_targets(machine.cfg, int(n_x), int(n_y), float(inset_mm))
    except (CalibrationError, TypeError, ValueError) as exc:
        raise MachineError(str(exc), "bad_request", 400) from exc
    arm = float(inset_mm)
    out: dict[str, Any] = {"targets": [[round(x, 3), round(y, 3)] for x, y in pts]}
    if 2.0 <= arm <= 20.0 and 2 <= n_x <= 15 and 2 <= n_y <= 11:
        out["pattern"] = {"id": "crosshair-points", "params": {"cols": int(n_x), "rows": int(n_y), "arm_mm": arm}}
    return out


def _xy(v: Any, what: str) -> tuple[float, float]:
    try:
        x, y = float(v[0]), float(v[1])
        if len(v) != 2:
            raise ValueError
    except (TypeError, ValueError, IndexError, KeyError):
        raise MachineError(f"{what}: erwartet [x, y] in mm.", "bad_request", 400) from None
    if not (math.isfinite(x) and math.isfinite(y) and abs(x) < MAX_COORD and abs(y) < MAX_COORD):
        raise MachineError(f"{what}: ungültige Zahl.", "bad_request", 400)
    return x, y


def _parse(body: dict | None) -> tuple[list[Measurement], list[str] | None]:
    body = body or {}
    raw = body.get("measurements")
    if not isinstance(raw, list) or not raw:
        raise MachineError("Keine Messwerte übergeben ('measurements').", "bad_request", 400)
    meas = []
    for i, m in enumerate(raw, 1):
        if not isinstance(m, dict):
            raise MachineError(f"Messpunkt {i}: Objekt mit 'target' und 'measured' erwartet.", "bad_request", 400)
        meas.append(Measurement(_xy(m.get("target"), f"Messpunkt {i} target"),
                                _xy(m.get("measured"), f"Messpunkt {i} measured")))
    free = body.get("free")
    if free is not None and (not isinstance(free, list) or not all(isinstance(f, str) for f in free)):
        raise MachineError("'free' muss eine Liste von Parameternamen sein.", "bad_request", 400)
    return meas, free


def _reference(machine: Machine) -> tuple[tuple[float, float], tuple[float, float]]:
    with machine._lock:
        ref = machine.reference
        if ref is None or machine.mapping is None:
            raise MachineError("Erst die Gondelposition bestätigen (Referenz setzen).", "no_reference")
        return (float(ref["physical"][0]), float(ref["physical"][1])), (float(ref["firmware"][0]), float(ref["firmware"][1]))


def _fit(machine: Machine, body: dict | None) -> CalibResult:
    meas, free = _parse(body)
    ref, ref_fw = _reference(machine)
    try:
        return fit_geometry(machine.cfg, ref, ref_fw, meas, free)
    except CalibrationError as exc:
        raise MachineError(str(exc), "bad_request", 400) from exc


def calibration_fit(machine: Machine, body: dict | None) -> dict:
    """Fit only; the configuration is not changed. Returns ``CalibResult.to_dict()``."""
    return _fit(machine, body).to_dict()


def calibration_apply(machine: Machine, body: dict | None) -> dict:
    """Fit and apply the patch via ``machine.update_config`` (saved like any config change).

    A reference correction (only when ``free`` contains "reference") moves the machine's reference
    position and rebuilds its mapping. Returns ``{"applied": bool, "result": ..., "config": ...}``;
    ``applied`` is False (config untouched) when the fit does not clearly improve the measurements."""
    res = _fit(machine, body)
    if not res.patch and res.reference_correction == (0.0, 0.0):
        return {"applied": False, "result": res.to_dict(), "config": machine.config_json()}
    cfg_json = machine.update_config(res.patch) if res.patch else machine.config_json()
    dx, dy = res.reference_correction
    if (dx, dy) != (0.0, 0.0):
        with machine._lock:
            ref = machine.reference
            if ref is not None:
                new_ref = (float(ref["physical"][0]) + dx, float(ref["physical"][1]) + dy)
                try:
                    mapping = Mapping.from_config(machine.cfg, new_ref, tuple(ref["firmware"]))
                except KinematicsError as exc:
                    raise MachineError(f"Referenzkorrektur nicht möglich: {exc}", "bad_reference", 422) from exc
                ref["physical"] = [new_ref[0], new_ref[1]]
                machine.mapping = mapping
    log.info("calibration applied: patch=%s ref_corr=%s", res.patch, res.reference_correction)
    return {"applied": True, "result": res.to_dict(), "config": cfg_json}
