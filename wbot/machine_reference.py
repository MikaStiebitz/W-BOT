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
            raise MachineError("Während eines Jobs nicht möglich.", "job_running")
        ms, _ = self.machine_state()
        if ms in ("run", "jog"):
            raise MachineError("Maschine bewegt sich - erst stoppen.", "moving")
        fw = self.fw_xy()
        if fw is None:
            raise MachineError("Keine Positionsmeldung vom Controller.", "no_status")
        b = self.cfg.board
        if not (-50 <= x <= b.width_mm + 50 and -50 <= y <= b.height_mm + 50):
            raise MachineError("Referenzposition liegt weit außerhalb des Boards.", "bad_reference", 400)
        try:
            self.mapping = Mapping.from_config(self.cfg, (x, y), fw)
        except KinematicsError as exc:
            raise MachineError(f"Referenz nicht möglich: {exc}", "bad_reference", 400) from exc
        bad = self.mapping.unreachable_points(self.cfg.safe_rect())
        if bad:
            self.mapping = None
            raise MachineError(
                f"Die Firmware glaubt, die Gondel stehe bei ({fw[0]:.0f}, {fw[1]:.0f}) mm, du gibst aber "
                f"({x:.0f}, {y:.0f}) mm an. So lassen sich {len(bad)} Punkte des Boards nicht anfahren. "
                f"Hänge die Gondel vor dem Einschalten dorthin, wo die Firmware sie vermutet "
                f"(Boot-Position der Config: {self.cfg.firmware.boot_physical[0]:.0f}, "
                f"{self.cfg.firmware.boot_physical[1]:.0f} mm), und starte das Board neu.",
                "reference_unreachable", 422)
        self.reference = {"physical": [x, y], "firmware": list(fw), "restored": False, "uncertain": False}
        self._save_reference()
        self.log("info", f"Referenz gesetzt: Stift bei ({x:.1f}, {y:.1f}) mm, Firmware ({fw[0]:.3f}, {fw[1]:.3f}).")
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
                self._warn("Referenz aus der letzten Sitzung wiederhergestellt - bitte prüfen, ob die "
                           "Gondel wirklich dort hängt (nach Reset/Stromausfall ggf. neu referenzieren).")
        except Exception:  # noqa: BLE001
            log.exception("could not restore reference")

    def _need_reference(self) -> Mapping:
        if self.mapping is None:
            raise MachineError("Erst die Gondelposition bestätigen (Referenz setzen).", "no_reference")
        return self.mapping
