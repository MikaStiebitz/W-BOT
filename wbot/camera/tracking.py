"""CameraService part: tracking the gondola marker (live pen position seen by the camera)."""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Any

import numpy as np

from ..errors import MachineError
from . import detect

log = logging.getLogger(__name__)


class TrackingMixin:
    # ------------------------------------------------------------------ tracking (gondola marker)
    def _start_tracking(self) -> None:
        self._track_stop = threading.Event()
        self._track_thread = threading.Thread(target=self._track_loop, args=(self._track_stop,), name="camera-track", daemon=True)
        self._track_thread.start()

    def _track_loop(self, stop: threading.Event) -> None:
        seq = 0
        last_seen_seq, tick = 0, 0
        while not stop.wait(0.2):
            tick += 1
            try:
                seq = self._track_once(seq)
            except MachineError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("camera tracking failed")
            src = self.source
            f = src.latest() if src is not None else None
            if f is None or f.seq == last_seen_seq or tick % 2:
                continue
            last_seen_seq = f.seq
            img = self._orient(f.image)
            for step in (self._presence_once, self._watch_once):     # people first: watch skips while someone stands there
                try:
                    step(img)
                except MachineError:
                    pass
                except Exception:  # noqa: BLE001
                    log.exception("camera %s failed", step.__name__)

    def _track_once(self, seq: int) -> int:
        g, src = self.geom, self.source
        gm = self.settings["markers"].get("gondola") or {}
        if g is None or src is None or not gm.get("enabled"):
            return seq
        f = src.latest()
        if f is None or f.seq == seq:
            return seq
        img = self._orient(f.image)
        if (img.shape[1], img.shape[0]) != tuple(g.image_size):
            return f.seq
        found = detect.detect_markers(img, self.settings["markers"]["dict"]).get(int(gm["id"]))
        soll = self.m.phys_xy() if self.m.mapping is not None else None
        ms, _ = self.m.machine_state()
        rec: dict[str, Any] = {"t": f.t, "seen": found is not None, "moving": ms != "idle",
                               "soll": None if soll is None else [round(soll[0], 2), round(soll[1], 2)]}
        if found is not None:
            mxy = g.to_board([found.mean(axis=0)], z=float(gm.get("height_mm", 36.0)))[0]
            off = gm.get("offset_mm", [0.0, 90.0])
            pen = (float(mxy[0] - off[0]), float(mxy[1] - off[1]))
            rec.update(marker=[round(float(mxy[0]), 2), round(float(mxy[1]), 2)], pen=[round(pen[0], 2), round(pen[1], 2)],
                       corners=found.tolist())
            if soll is not None:
                dev = math.hypot(pen[0] - soll[0], pen[1] - soll[1])
                rec["dev_mm"] = round(dev, 2)
                self.history.append((f.t, pen[0], pen[1], soll[0], soll[1]))
                self.history = self.history[-600:]
                self._maybe_warn(dev)
        self.track = rec
        self.m._emit({"type": "camera", "data": rec})
        return f.seq

    def _maybe_warn(self, dev: float) -> None:
        warn = float(self.settings["tracking"]["warn_mm"])
        ms, _ = self.m.machine_state()
        if dev > warn and ms == "idle" and time.time() - self._last_warn > 30.0:
            self._last_warn = time.time()
            self.m.log("warn", f"Camera: the gondola is {dev:.1f} mm away from where it should be "
                               "(belt skipped? wrong reference? learn the marker offset?).")

    def learn_offset(self) -> dict:
        """Marker-to-pen offset from the current pose (machine idle, reference set)."""
        if self.m.mapping is None:
            raise MachineError("Set the reference first.", "no_reference")
        tr, soll = self.track, self.m.phys_xy()
        if not tr or not tr.get("marker") or time.time() - tr["t"] > 2.0:
            raise MachineError("The gondola marker is not visible right now.", "not_found", 422)
        off = [round(tr["marker"][0] - soll[0], 2), round(tr["marker"][1] - soll[1], 2)]
        with self._lock:
            self.settings["markers"]["gondola"]["offset_mm"] = off
            self._save()
            self.history.clear()
        return {"offset_mm": off}

    def estimate_latency(self) -> dict:
        """Camera delay: shift of the software trajectory that best matches the tracked one."""
        h = np.asarray(self.history[-300:], float)
        if len(h) < 20 or np.ptp(h[:, 3]) + np.ptp(h[:, 4]) < 50:
            raise MachineError("Too little movement recorded: move the gondola around a few times first.", "bad_request", 400)
        t = h[:, 0]
        best = (math.inf, 0.0)
        for lag in np.arange(0.0, 1.01, 0.02):
            sx = np.interp(t - lag, t, h[:, 3])
            sy = np.interp(t - lag, t, h[:, 4])
            ok = t - lag >= t[0]
            e = float(np.median(np.hypot(h[ok, 1] - sx[ok], h[ok, 2] - sy[ok]))) if ok.sum() > 10 else math.inf
            best = min(best, (e, float(lag)))
        with self._lock:
            self.settings["latency_ms"] = round(best[1] * 1000.0)
            self._save()
        return {"latency_ms": self.settings["latency_ms"], "median_mm": round(best[0], 2)}
