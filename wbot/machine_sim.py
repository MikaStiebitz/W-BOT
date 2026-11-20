"""Simulator observation helpers for :class:`wbot.machine.Machine`."""
from __future__ import annotations

import logging

import numpy as np


log = logging.getLogger(__name__)


class SimMixin:
    # ---------------------------------------------------------------- simulator
    def _emulator(self):
        link = self.link
        return link if (self.transport == "sim" and hasattr(link, "trace")) else None

    def _pump_sim_trace(self) -> None:
        emu = self._emulator()
        if emu is None:
            return
        trace = emu.trace
        n = len(trace)
        if n < self._sim_trace_sent:
            self._sim_trace_sent = 0
        for seg in trace[self._sim_trace_sent:n]:
            self._emit({"type": "trace", "data": {"seg": [round(seg.x0, 3), round(seg.y0, 3),
                                                          round(seg.x1, 3), round(seg.y1, 3),
                                                          bool(seg.pen_down), seg.color_slot]}})
        self._sim_trace_sent = n

    def simulator_info(self) -> dict | None:
        emu = self._emulator()
        if emu is None:
            return None
        phys = [float(v) for v in emu.physical_position()]
        intended = self.phys_xy()
        err = None if intended is None else float(np.hypot(phys[0] - intended[0], phys[1] - intended[1]))
        info: dict = {"physical": phys, "intended": list(intended) if intended else None,
                      "firmware": [float(v) for v in emu.firmware_position()],
                      "error_mm": err, "perturb": dict(self.sim_opts.get("perturb") or {}),
                      "trace_segments": len(emu.trace),
                      "trace": [[round(g.x0, 3), round(g.y0, 3), round(g.x1, 3), round(g.y1, 3),
                                 bool(g.pen_down), g.color_slot] for g in emu.trace[-6000:]]}
        je = self.jobs.trace_error(emu.trace) if self.jobs else None
        if je:
            info["job_error"] = je
            info["error_max_mm"], info["error_mean_mm"] = je["max_mm"], je["mean_mm"]
        return info
