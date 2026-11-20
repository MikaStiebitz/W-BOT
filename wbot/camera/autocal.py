"""Camera measurement of drawn crosses and the automatic calibration run.

``measure_crosses``: for each target, a small top-down crop (4 px/mm) around it is cut out of the
(averaged) camera image, the cross is found (template + line fit of both arms, see
``detect.find_cross``) and converted to board mm. Crosses hidden by the gondola or its belts are
reported as such instead of being measured badly.

``AutoCalibration``: draw the cross grid -> move the gondola to two places in turn (left, right) and
measure each cross from the view where it is least occluded -> the existing geometry fit
(``calibration.fit_geometry``). It never applies anything: the UI shows the result and the user
applies it with the normal "Korrektur übernehmen" (POST /api/calibration/apply).
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import TYPE_CHECKING, Any

import numpy as np

from ..errors import MachineError
from . import detect
from .simcam import GONDOLA_ABOVE, GONDOLA_BELOW, GONDOLA_W

if TYPE_CHECKING:
    from .service import CameraService

log = logging.getLogger(__name__)
CROP_PX_PER_MM = 4.0


def _seg_dist(p, a, b) -> float:
    p, a, b = (np.asarray(v, float) for v in (p, a, b))
    ab = b - a
    t = float(np.clip(np.dot(p - a, ab) / max(float(np.dot(ab, ab)), 1e-9), 0.0, 1.0))
    return float(np.linalg.norm(p - (a + t * ab)))


def clearance(cfg, target, pen) -> float:
    """Distance (mm) of a target to the gondola body and its belts; negative = inside the body."""
    if pen is None:
        return 1e6
    x, y = target
    px, py = pen
    dx = abs(x - px) - (GONDOLA_W / 2 + 12)
    dy = max((py - GONDOLA_BELOW - 12) - y, y - (py + GONDOLA_ABOVE + 12))
    body = max(dx, dy) if (dx > 0 or dy > 0) else min(dx, dy)
    g = cfg.geometry
    belts = min(_seg_dist(target, g.anchor_left, (px - g.attach_half_spacing_mm, py + g.attach_height_mm)),
                _seg_dist(target, g.anchor_right, (px + g.attach_half_spacing_mm, py + g.attach_height_mm)))
    return min(body, belts - 6.0)


def measure_crosses(svc: "CameraService", targets, arm_mm: float, *, frames: int = 6, search_mm: float | None = None,
                    image: np.ndarray | None = None, pen=None) -> dict:
    g = svc.geometry()
    img = image if image is not None else svc.averaged(frames)
    if pen is None and svc.m.mapping is not None:
        pen = svc.m.phys_xy()
    line_mm = float(svc.settings.get("line_mm", 2.0))
    arm = float(arm_mm)
    search = float(search_mm) if search_mm else max(10.0, 0.8 * arm)
    s = CROP_PX_PER_MM
    half = arm * 1.3 + search + line_mm
    from shapely.geometry import Point, Polygon
    cov = g.coverage((-50.0, -50.0, svc.m.cfg.board.width_mm + 50, svc.m.cfg.board.height_mm + 50))
    area = Polygon(cov) if len(cov) >= 3 else None
    points, worst_res = [], 0.0
    for t in targets:
        tx, ty = float(t[0]), float(t[1])
        rec: dict[str, Any] = {"target": [tx, ty], "measured": None, "status": "ok"}
        mmpx = g.mm_per_px((tx, ty))
        rec["mm_per_px"] = round(mmpx, 3)
        worst_res = max(worst_res, mmpx)
        clr = clearance(svc.m.cfg, (tx, ty), pen)
        rec["clearance_mm"] = round(clr, 1) if clr < 1e5 else None
        if area is None or not area.buffer(-half).contains(Point(tx, ty)):
            rec["status"] = "außerhalb"
        elif clr < arm:
            rec["status"] = "verdeckt"
        else:
            r = (tx - half, ty - half, tx + half, ty + half)
            crop = g.rectify(img, r, s, border=(255, 255, 255))
            hit = detect.find_cross(crop, (half * s, half * s), arm * s, line_mm * s, search * s)
            if hit is None:
                rec["status"] = "nicht gefunden"
            else:
                mx, my = r[0] + (hit.x + 0.5) / s, r[3] - (hit.y + 0.5) / s
                rec.update(measured=[round(mx, 3), round(my, 3)], score=round(hit.score, 3),
                           coverage=round(hit.coverage, 2), angle_deg=round(hit.angle_deg, 2))
        points.append(rec)
    found = sum(1 for p in points if p["measured"])
    warnings = []
    if worst_res > 2.5:
        warnings.append(f"Die Kamera löst an manchen Punkten nur {worst_res:.1f} mm pro Pixel auf: näher heran oder "
                        "höhere Auflösung, sonst ist die Messung ungenau.")
    miss = [i + 1 for i, p in enumerate(points) if p["status"] in ("nicht gefunden",)]
    if miss:
        warnings.append("Nicht gefunden: Kreuz " + ", ".join(map(str, miss)) + " (gezeichnet? Belichtung? Liniendicke?).")
    return {"points": points, "found": found, "total": len(points), "warnings": warnings,
            "rms_px": g.rms_px, "homography_rms_mm": g.rms_mm}


def merge_views(views: list[dict]) -> list[dict]:
    """Per target: the measurement from the view with the most clearance (mean if both are clear)."""
    out = []
    for recs in zip(*[v["points"] for v in views]):
        good = [r for r in recs if r["measured"]]
        if not good:
            out.append(max(recs, key=lambda r: r.get("clearance_mm") or 0.0))
            continue
        clear = [r for r in good if (r.get("clearance_mm") or 1e6) > 120.0]
        use = clear or [max(good, key=lambda r: r.get("clearance_mm") or 1e6)]
        m = np.mean([r["measured"] for r in use], axis=0)
        best = dict(use[0])
        best["measured"] = [round(float(m[0]), 3), round(float(m[1]), 3)]
        best["views"] = len(use)
        out.append(best)
    return out


class AutoCalibration:
    """Background run; state is published as WS messages {"type": "autocal", "data": state}."""

    def __init__(self, svc: "CameraService"):
        self.svc, self.m = svc, svc.m
        self.state: dict = {"state": "idle"}
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _set(self, **kw) -> None:
        self.state = {**self.state, **kw, "t": time.time()}
        self.m._emit({"type": "autocal", "data": self.state})

    def start(self, body: dict) -> dict:
        if self.running:
            raise MachineError("Die Kamera-Kalibrierung läuft schon.", "busy", 409)
        self.svc.geometry()
        if self.m.mapping is None:
            raise MachineError("Erst die Referenz setzen.", "no_reference")
        from ..calibration_api import calibration_targets
        nx, ny, inset = int(body.get("n_x", 5)), int(body.get("n_y", 4)), float(body.get("inset_mm", 20.0))
        tg = calibration_targets(self.m, nx, ny, inset)
        if "pattern" not in tg:
            raise MachineError("Mit diesen Einstellungen lassen sich keine Messkreuze zeichnen (Armlänge 2-20 mm).", "bad_request", 400)
        self._cancel.clear()
        self.state = {"state": "starting", "targets": tg["targets"], "arm_mm": inset, "step": "Start", "progress": 0.0}
        args = (tg, bool(body.get("draw", True)), body.get("free"))
        self._thread = threading.Thread(target=self._run, args=args, name="camera-autocal", daemon=True)
        self._thread.start()
        return self.state

    def cancel(self) -> dict:
        self._cancel.set()
        if self.m.jobs and self.m.jobs.running:
            self.m.cancel()
        return self.state

    def _wait_job(self) -> None:
        while True:
            if self._cancel.is_set():
                raise MachineError("Abgebrochen.", "cancelled", 409)
            job = self.m.jobs.job if self.m.jobs else None
            if job and job["state"] not in ("planning", "running", "paused"):
                if job["state"] != "done":
                    raise MachineError(f"Zeichnen nicht fertig geworden ({job['state']}).", "job_failed", 409)
                return
            if job:
                self._set(progress=0.05 + 0.6 * float(job.get("progress") or 0.0))
            time.sleep(0.2)

    def _wait_idle(self, timeout: float = 120.0) -> None:
        t_end = time.time() + timeout
        while time.time() < t_end:
            ms, _ = self.m.machine_state()
            if ms == "idle" and not self.m._cmd_lock.locked():
                return
            if self._cancel.is_set():
                raise MachineError("Abgebrochen.", "cancelled", 409)
            time.sleep(0.1)
        raise MachineError("Die Maschine wird nicht fertig.", "timeout", 504)

    def _run(self, tg: dict, draw: bool, free) -> None:
        targets, arm = tg["targets"], float(tg["pattern"]["params"]["arm_mm"])
        try:
            if draw:
                self._set(state="drawing", step="Messkreuze zeichnen", progress=0.05)
                self.m.start_job({"pattern": tg["pattern"], "name": "Messkreuze (Kamera)"})
                time.sleep(0.3)
                self._wait_job()
            x0, y0, x1, y1 = self.m.cfg.safe_rect()
            spots = [(x0 + 0.12 * (x1 - x0), y1), (x0 + 0.88 * (x1 - x0), y1)]
            views = []
            for i, (sx, sy) in enumerate(spots):
                self._set(state="measuring", step=f"Gondel zur Seite fahren ({i + 1}/2)", progress=0.7 + 0.1 * i)
                self._wait_idle()
                self.m.goto(sx, sy, None, "up")
                self._wait_idle()
                time.sleep(0.6)                       # gondola swings out, camera latency
                self._set(step=f"Kreuze messen ({i + 1}/2)")
                views.append(measure_crosses(self.svc, targets, arm, frames=8, pen=(sx, sy)))
            points = merge_views(views)
            meas = [{"target": p["target"], "measured": p["measured"]} for p in points if p["measured"]]
            warnings = sorted({w for v in views for w in v["warnings"]})
            self._set(step="Geometrie anpassen", progress=0.9, points=points, warnings=warnings)
            if len(meas) < 3:
                raise MachineError(f"Nur {len(meas)} Kreuze erkannt - zu wenig für eine Auswertung.", "not_found", 422)
            from ..calibration_api import calibration_fit
            result = calibration_fit(self.m, {"measurements": meas, "free": free})
            self._set(state="done", step="Fertig", progress=1.0, result=result, measurements=meas)
        except MachineError as exc:
            self._set(state="error" if exc.code != "cancelled" else "cancelled", step=exc.message, error=exc.message)
        except Exception as exc:  # noqa: BLE001
            log.exception("auto calibration failed")
            self._set(state="error", step=f"Interner Fehler: {exc}", error=str(exc))
