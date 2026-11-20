"""Reference handling (physical <-> firmware pairing) for :class:`wbot.machine.Machine`."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import numpy as np

from .errors import MachineError
from .kinematics import KinematicsError, Mapping

log = logging.getLogger(__name__)

def _m():
    """The machine module owns the paths/constants (tests patch them there)."""
    from . import machine
    return machine


class ReferenceMixin:
    # ---------------------------------------------------------------- reference
    def set_reference(self, x: float, y: float) -> dict:
        self._require_conn()
        if self.jobs and self.jobs.running:
            raise MachineError("Not possible while a job is running.", "job_running")
        ms, _ = self.machine_state()
        if ms in ("run", "jog"):
            raise MachineError("The machine is moving - stop it first.", "moving")
        fw = self.fw_xy()
        if fw is None:
            raise MachineError("No position report from the controller.", "no_status")
        b = self.cfg.board
        if not (-50 <= x <= b.width_mm + 50 and -50 <= y <= b.height_mm + 50):
            raise MachineError("The reference position is far outside the board.", "bad_reference", 400)
        try:
            self.mapping = Mapping.from_config(self.cfg, (x, y), fw)
        except KinematicsError as exc:
            raise MachineError(f"Reference not possible: {exc}", "bad_reference", 400) from exc
        bad = self.mapping.unreachable_points(self.cfg.safe_rect())
        if bad:
            self.mapping = None
            boot = self.cfg.firmware.boot_physical
            if abs(x - (fw[0] + boot[0])) < 1.0 and abs(y - (fw[1] + boot[1])) < 1.0:
                # the position agrees with what the firmware reports: the geometry or the drawing area is the problem
                msg = (f"With the configured motor positions, {len(bad)} points of the drawing area cannot be reached "
                       f"from ({x:.0f}, {y:.0f}) mm. Check the motor positions (Setup) or shrink the drawing area (More, Settings).")
            else:
                msg = (f"The firmware believes the gondola is at ({fw[0]:.0f}, {fw[1]:.0f}) mm, but you specified "
                       f"({x:.0f}, {y:.0f}) mm. {len(bad)} points of the board cannot be reached that way. "
                       f"Before powering on, hang the gondola where the firmware expects it "
                       f"(boot position in the config: {boot[0]:.0f}, {boot[1]:.0f} mm), and restart the board.")
            raise MachineError(msg, "reference_unreachable", 422)
        self.reference = {"physical": [x, y], "firmware": list(fw), "restored": False, "uncertain": False}
        self._save_reference()
        self.log("info", f"Reference set: pen at ({x:.1f}, {y:.1f}) mm, firmware ({fw[0]:.3f}, {fw[1]:.3f}).")
        return self.state()

    def _reference_file(self) -> Path:
        """Real boards (serial and WiFi, same hardware) share one reference file; a loopback
        host means the fake board of ``wbot sim-server`` and must never overwrite it."""
        if self.transport == "wifi" and (self.host or "").lower() in _m().LOOPBACK_HOSTS:
            return _m().FAKE_REFERENCE_FILE
        return _m().REFERENCE_FILE

    def _save_reference(self) -> None:
        ph, fw = self.phys_xy(), self.fw_xy()
        if ph is None or fw is None or self.transport in (None, "sim"):
            return
        try:
            f = self._reference_file()
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"physical": ph, "firmware": fw, "t": time.time()}))
        except OSError:
            pass

    def _try_restore_reference(self) -> None:
        if self.transport in (None, "sim") or not self._reference_file().exists():
            return
        try:
            saved = json.loads(self._reference_file().read_text())
            fw = self.fw_xy()
            if fw and np.hypot(fw[0] - saved["firmware"][0], fw[1] - saved["firmware"][1]) <= _m().FW_MATCH_MM:
                self.set_reference(*saved["physical"])
                self.reference["restored"] = True
                self._warn("Reference restored from the last session - please check that the "
                           "gondola really hangs there (re-reference after a reset/power loss if necessary).")
        except Exception:  # noqa: BLE001
            log.exception("could not restore reference")

    def _need_reference(self) -> Mapping:
        if self.mapping is None:
            raise MachineError("Confirm the gondola position first (set the reference).", "no_reference")
        return self.mapping
