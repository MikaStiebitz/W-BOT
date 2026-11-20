"""The guided calibration flow.

A small explicit state machine rather than a script, because the UI drives it
one step at a time and the machine has to keep answering in between.

    idle
      -> reference     jog the gondola somewhere, tape-measure both belts
      -> pattern       the machine draws the numbered pattern
      -> measure       you type in where each cross actually landed
      -> solve         least-squares fit over all parameters at once
      -> review        residual report; accept or go back and re-measure
      -> saved         written to config/calibration.yaml

The reference step is optional but recommended: without it the belt zero and
the anchor positions are only weakly separable (see :mod:`wbbot.calib.solve`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal, Sequence

import numpy as np

from ..config import MachineConfig, save_calibration
from ..core.safearea import SafeArea
from ..core.toolpath import Toolpath
from .patterns import CalibrationPattern, TargetPoint, build_pattern
from .solve import (BeltReference, CalibrationMeasurement, CalibrationResult,
                    solve_calibration)

log = logging.getLogger(__name__)

__all__ = ["WizardStep", "ServoTuner", "CalibrationWizard"]


class WizardStep(str, Enum):
    IDLE = "idle"
    REFERENCE = "reference"
    PATTERN = "pattern"
    MEASURE = "measure"
    CORNERS = "corners"
    SOLVE = "solve"
    REVIEW = "review"
    SAVED = "saved"


#: Order of the corner walk, matching :attr:`CalibrationWizard.corner_targets`.
CORNER_LABELS = ("oben links", "oben rechts", "unten rechts", "unten links")


@dataclass
class ServoTuner:
    """Live servo tuning: move a servo, look at the board, keep the number.

    The pen must touch firmly enough to draw but not press — too much and the
    marker tip splays and the gondola is pushed off the board plane.  That is
    a judgement call made by eye, so this just exposes the values safely.
    """

    cfg: MachineConfig
    pen_up: float = 0.0
    pen_down: float = 0.0
    revolver: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.pen_up = self.cfg.servo.pen.up_pos
        self.pen_down = self.cfg.servo.pen.down_pos
        if not self.revolver:
            self.revolver = list(self.cfg.servo.revolver.positions)

    def clamp_pen(self, value: float) -> float:
        return float(min(max(value, 0.0), self.cfg.servo.pen.axis_max_travel))

    def clamp_revolver(self, value: float) -> float:
        return float(min(max(value, 0.0), self.cfg.servo.revolver.axis_max_travel))

    def set_pen_up(self, value: float) -> float:
        self.pen_up = self.clamp_pen(value)
        return self.pen_up

    def set_pen_down(self, value: float) -> float:
        self.pen_down = self.clamp_pen(value)
        return self.pen_down

    def set_revolver(self, index: int, value: float) -> float:
        if not 0 <= index < len(self.revolver):
            raise ValueError(
                f"revolver slot {index} out of range (0..{len(self.revolver) - 1})")
        self.revolver[index] = self.clamp_revolver(value)
        return self.revolver[index]

    def validate(self) -> list[str]:
        problems = []
        if abs(self.pen_up - self.pen_down) < 1.0:
            problems.append(
                f"pen up ({self.pen_up:.1f}) and down ({self.pen_down:.1f}) are "
                f"nearly identical — the pen would never lift clear")
        for i, a in enumerate(self.revolver):
            for j, b in enumerate(self.revolver[i + 1:], start=i + 1):
                if abs(a - b) < 5.0:
                    problems.append(
                        f"revolver positions {i + 1} and {j + 1} are only "
                        f"{abs(a - b):.1f} apart — the wrong marker may engage")
        return problems

    def to_overlay(self) -> dict:
        return {"servo": {
            "pen": {"up_pos": round(self.pen_up, 2),
                    "down_pos": round(self.pen_down, 2)},
            "revolver": {"positions": [round(v, 2) for v in self.revolver]},
        }}

    def to_dict(self) -> dict:
        return {"pen_up": self.pen_up, "pen_down": self.pen_down,
                "revolver": list(self.revolver),
                "problems": self.validate()}


class CalibrationWizard:
    """Stateful driver for the calibration flow.

    Deliberately has no knowledge of transports: the caller (REST endpoint,
    MCP tool, CLI) does the actual drawing and feeds results back in.
    """

    def __init__(self, cfg: MachineConfig, safe_area: SafeArea | None = None,
                 *, calibration_path: str | Path | None = None):
        self.cfg = cfg
        self.safe_area = safe_area if safe_area is not None else SafeArea(cfg)
        self.calibration_path = calibration_path
        self.step: WizardStep = WizardStep.IDLE
        self.pattern: CalibrationPattern | None = None
        self.reference: BeltReference | None = None
        self.measurements: list[CalibrationMeasurement] = []
        self.result: CalibrationResult | None = None
        self.servo = ServoTuner(cfg)
        self.errors: list[str] = []
        # -- corner method ---------------------------------------------------
        self.corner_targets: list[tuple[float, float]] = []
        self.corner_records: dict[int, CalibrationMeasurement] = {}

    # -- step 1 ---------------------------------------------------------

    def begin(self) -> dict:
        self.step = WizardStep.REFERENCE
        self.pattern = None
        self.reference = None
        self.measurements = []
        self.result = None
        self.errors = []
        return self.state()

    def set_reference(self, position: Sequence[float],
                      left_mm: float, right_mm: float,
                      weight: float = 2.0) -> dict:
        """Record the tape-measured belt lengths at a known gondola position."""
        pos = (float(position[0]), float(position[1]))
        reason = self.safe_area.reason(pos)
        if reason is not None:
            raise ValueError(f"reference position {pos} is not usable: {reason}")
        if left_mm <= 0 or right_mm <= 0:
            raise ValueError("measured belt lengths must be positive")
        # Sanity-check against the nominal geometry: a transposed or mistyped
        # number here would quietly poison the whole fit.
        from ..core.kinematics import make_kinematics
        expected = make_kinematics(self.cfg).inverse(pos)
        for name, got, want in (("left", left_mm, expected.left),
                                ("right", right_mm, expected.right)):
            if abs(got - want) > 300.0:
                raise ValueError(
                    f"the measured {name} belt length ({got:.0f} mm) differs "
                    f"from the expected {want:.0f} mm by more than 300 mm — "
                    f"check the tape measure and the reference position")
        self.reference = BeltReference(position=pos, left_mm=float(left_mm),
                                       right_mm=float(right_mm), weight=weight)
        self.step = WizardStep.PATTERN
        return self.state()

    def skip_reference(self) -> dict:
        self.reference = None
        self.step = WizardStep.PATTERN
        return self.state()

    # -- step 2 ---------------------------------------------------------

    def build_pattern(self, *, n_x: int = 3, n_y: int = 3,
                      inset_mm: float = 120.0,
                      color_index: int = 0) -> CalibrationPattern:
        self.pattern = build_pattern(self.cfg, self.safe_area, n_x=n_x, n_y=n_y,
                                     inset_mm=inset_mm, color_index=color_index)
        self.step = WizardStep.MEASURE
        return self.pattern

    @property
    def toolpath(self) -> Toolpath:
        if self.pattern is None:
            raise RuntimeError("build the calibration pattern first")
        return self.pattern.toolpath

    # -- step 3 ---------------------------------------------------------

    def record(self, index: int, measured_x: float, measured_y: float) -> dict:
        """Record where cross ``index`` actually landed."""
        if self.pattern is None:
            raise RuntimeError("build the calibration pattern first")
        target = next((t for t in self.pattern.targets if t.index == index), None)
        if target is None:
            valid = ", ".join(str(t.index) for t in self.pattern.targets)
            raise ValueError(f"no target numbered {index}; valid targets: {valid}")
        board = self.cfg.board
        if not (-200 <= measured_x <= board.width_mm + 200
                and -200 <= measured_y <= board.height_mm + 200):
            raise ValueError(
                f"measured position ({measured_x}, {measured_y}) mm is nowhere "
                f"near the board — check the units and the origin corner")
        deviation = float(np.hypot(measured_x - target.nominal[0],
                                   measured_y - target.nominal[1]))
        if deviation > 150.0:
            self.errors.append(
                f"target {index} is {deviation:.0f} mm from its nominal "
                f"position — that is a lot; is the number right?")
        self.measurements = [m for m in self.measurements if m.index != index]
        self.measurements.append(CalibrationMeasurement(
            commanded=target.nominal,
            measured=(float(measured_x), float(measured_y)),
            index=index))
        self.measurements.sort(key=lambda m: m.index)
        return self.state()

    def record_many(self, entries: Sequence[dict]) -> dict:
        for entry in entries:
            self.record(int(entry["index"]), float(entry["x"]), float(entry["y"]))
        return self.state()

    @property
    def missing_targets(self) -> list[int]:
        if self.pattern is None:
            return []
        done = {m.index for m in self.measurements}
        return [t.index for t in self.pattern.targets if t.index not in done]

    # -- corner method ---------------------------------------------------
    #
    # The quick alternative to the measured pattern: jog the gondola until the
    # pen tip sits exactly on each board corner, record the position the
    # machine *believes* it is at, and fit from the difference. Four corners
    # give eight residuals; only the anchor positions are fitted (the belt
    # zero and pen offset are frozen — with four points they are degenerate
    # with the anchors and would soak up the signal).

    def begin_corners(self) -> dict:
        board = self.cfg.board
        self.step = WizardStep.CORNERS
        self.corner_targets = [
            (0.0, board.height_mm),                 # oben links
            (board.width_mm, board.height_mm),      # oben rechts
            (board.width_mm, 0.0),                  # unten rechts
            (0.0, 0.0),                             # unten links
        ]
        self.corner_records = {}
        self.result = None
        self.errors = []
        return self.state()

    def record_corner(self, index: int, machine_x: float,
                      machine_y: float) -> dict:
        """Record where the machine believes it is while the pen sits on
        corner ``index`` (0 = top-left, clockwise)."""
        if self.step is not WizardStep.CORNERS:
            raise RuntimeError("start the corner calibration first")
        if not 0 <= index < len(self.corner_targets):
            raise ValueError(
                f"corner index {index} out of range (0..{len(self.corner_targets) - 1})")
        target = self.corner_targets[index]
        deviation = float(np.hypot(machine_x - target[0], machine_y - target[1]))
        if deviation > 250.0:
            raise ValueError(
                f"the machine reports ({machine_x:.0f}, {machine_y:.0f}) mm but "
                f"the corner '{CORNER_LABELS[index]}' is at "
                f"({target[0]:.0f}, {target[1]:.0f}) — {deviation:.0f} mm apart. "
                f"That is too far to be a calibration error; is the right "
                f"corner selected?")
        self.corner_records[index] = CalibrationMeasurement(
            commanded=(float(machine_x), float(machine_y)),
            measured=target, index=index + 1)
        return self.state()

    @property
    def missing_corners(self) -> list[int]:
        return [i for i in range(len(self.corner_targets))
                if i not in self.corner_records]

    def solve_corners(self, *, model: Literal["naive", "tangent"] = "naive"
                      ) -> CalibrationResult:
        if self.missing_corners:
            names = ", ".join(CORNER_LABELS[i] for i in self.missing_corners)
            raise ValueError(f"corners still missing: {names}")
        self.step = WizardStep.SOLVE
        measurements = [self.corner_records[i]
                        for i in sorted(self.corner_records)]
        self.result = solve_calibration(
            measurements, self.cfg, model=model,
            fit_scale=False, fit_pen_offset=False, fit_belt_zero=False)
        self.result.warnings.append(
            "corner calibration fits the anchor positions only; steps/mm, "
            "belt zero and pen offset keep their configured values. Run the "
            "measured pattern for the full fit.")
        self.step = WizardStep.REVIEW
        return self.result

    # -- step 4 ---------------------------------------------------------

    def solve(self, *, model: Literal["naive", "tangent", "auto"] = "auto",
              fit_pen_offset: bool = True,
              fit_scale: bool = True) -> CalibrationResult:
        if len(self.measurements) < 4:
            raise ValueError(
                f"at least 4 measured targets are needed, "
                f"have {len(self.measurements)}")
        self.step = WizardStep.SOLVE
        self.result = solve_calibration(
            self.measurements, self.cfg, model=model, reference=self.reference,
            fit_pen_offset=fit_pen_offset, fit_scale=fit_scale)
        self.step = WizardStep.REVIEW
        return self.result

    # -- step 5 ---------------------------------------------------------

    def save(self, *, include_servo: bool = True) -> Path:
        if self.result is None:
            raise RuntimeError("run solve() before saving")
        payload = self.result.to_calibration_yaml(self.cfg)
        if include_servo:
            overlay = self.servo.to_overlay()
            payload.setdefault("servo", {}).update(overlay["servo"])
        payload["measurements"] = [
            {"index": m.index,
             "commanded": [round(m.commanded[0], 2), round(m.commanded[1], 2)],
             "measured": [round(m.measured[0], 2), round(m.measured[1], 2)],
             "error_mm": round(m.error, 2)}
            for m in self.measurements
        ]
        if self.reference is not None:
            payload["belt_reference"] = {
                "position": list(self.reference.position),
                "left_mm": self.reference.left_mm,
                "right_mm": self.reference.right_mm,
            }
        if self.corner_records:
            payload["corner_records"] = [
                {"corner": CORNER_LABELS[i],
                 "machine": [round(m.commanded[0], 2), round(m.commanded[1], 2)],
                 "true": [round(m.measured[0], 2), round(m.measured[1], 2)],
                 "error_mm": round(m.error, 2)}
                for i, m in sorted(self.corner_records.items())
            ]
        path = save_calibration(payload, self.calibration_path)
        self.step = WizardStep.SAVED
        log.info("calibration written to %s", path)
        return path

    def apply(self) -> MachineConfig:
        """Return a config with the fit applied, without writing anything."""
        if self.result is None:
            raise RuntimeError("run solve() before applying")
        overlay = self.result.to_calibration_yaml(self.cfg)
        overlay.pop("report", None)
        return self.cfg.replace(**overlay)

    # -- introspection ---------------------------------------------------

    def state(self) -> dict:
        return {
            "step": self.step.value,
            "corners": {
                "targets": [[x, y] for x, y in self.corner_targets],
                "labels": list(CORNER_LABELS[:len(self.corner_targets)]),
                "recorded": sorted(self.corner_records),
                "missing": self.missing_corners,
            },
            "has_reference": self.reference is not None,
            "reference": ({"position": list(self.reference.position),
                           "left_mm": self.reference.left_mm,
                           "right_mm": self.reference.right_mm}
                          if self.reference else None),
            "pattern": self.pattern.to_dict() if self.pattern else None,
            "instructions": self.pattern.instructions() if self.pattern else [],
            "measurements": [
                {"index": m.index, "commanded": list(m.commanded),
                 "measured": list(m.measured), "error_mm": round(m.error, 2)}
                for m in self.measurements],
            "missing_targets": self.missing_targets,
            "result": self.result.to_dict() if self.result else None,
            "servo": self.servo.to_dict(),
            "errors": list(self.errors),
        }
