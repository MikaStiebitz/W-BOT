"""CameraService: one per app. Owns the frame source, the board mapping and the gondola tracking.

Settings live in ``config/state/camera.json`` (written only when ``persist``), never in wbot.yaml:
they describe where this camera hangs today, not the machine. Errors are ``MachineError`` with
German messages, like the rest of the API.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from ..errors import MachineError
from . import detect
from .geometry import BoardCamera, CameraGeometryError, calibrate_lens
from .simcam import SimCamera
from .sources import DeviceSource, SimSource, Source, StillSource, UrlSource

if TYPE_CHECKING:
    from ..machine import Machine

log = logging.getLogger(__name__)
CAMERA_FILE = Path(__file__).resolve().parent.parent.parent / "config" / "state" / "camera.json"

DEFAULTS: dict[str, Any] = {
    "source": {"kind": "none", "index": 0, "url": "", "width": 1920, "height": 1080, "fps": 30},
    "rotate": 0,                 # 0 / 90 / 180 / 270 degrees clockwise
    "mirror": False,
    "calib": None,               # {method, image_points, board_points, image_size}
    "lens": None,                # {K, dist, rms_px, views, image_size}
    "markers": {"dict": "4X4_50", "size_mm": 60.0,
                "board": [{"id": 0, "x": 50.0, "y": 950.0}, {"id": 1, "x": 1450.0, "y": 950.0},
                          {"id": 2, "x": 1450.0, "y": 50.0}, {"id": 3, "x": 50.0, "y": 50.0}],
                "gondola": {"enabled": True, "id": 10, "size_mm": 50.0, "height_mm": 36.0, "offset_mm": [0.0, 90.0]}},
    "tracking": {"warn_mm": 8.0},
    "view": {"px_per_mm": 1.0, "margin_mm": 30.0},
    "line_mm": 2.0,              # marker line width (cross detection)
    "latency_ms": 150.0,
}
CHESS_DEFAULT = (9, 6)


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


class CameraService:
    def __init__(self, machine: "Machine", *, persist: bool = False, path: Path | None = None):
        self.m, self.persist, self.path = machine, persist, Path(path or CAMERA_FILE)
        self._lock = threading.RLock()
        self.settings = _merge(DEFAULTS, self._load() if persist else {})
        self.source: Source | None = None
        self.geom: BoardCamera | None = None
        self.geom_error: str | None = None
        self.sim: SimCamera | None = None
        self.lens_views: list[np.ndarray] = []
        self.track: dict | None = None
        self.history: list[tuple[float, float, float, float, float]] = []    # t, cam_x, cam_y, soll_x, soll_y
        self._last_warn = 0.0
        self._track_stop = threading.Event()
        self._track_thread: threading.Thread | None = None
        self._jpeg_cache: dict[tuple, bytes] = {}
        self._rebuild_geometry()
        if self.settings["source"]["kind"] != "none":
            try:
                self._open_source()
            except MachineError as exc:
                log.warning("camera not opened at start: %s", exc.message)

    # ------------------------------------------------------------------ persistence
    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.warning("camera settings unreadable (%s), using defaults", exc)
            return {}

    def _save(self) -> None:
        if not self.persist:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.settings, indent=1), encoding="utf-8")
        except OSError as exc:
            log.warning("camera settings not saved: %s", exc)

    # ------------------------------------------------------------------ source
    def configure(self, patch: dict) -> dict:
        """Change settings (partial). Reopens the source when it changed."""
        if not isinstance(patch, dict):
            raise MachineError("Erwartet ein Objekt mit Kamera-Einstellungen.", "bad_request", 400)
        allowed = set(DEFAULTS) - {"calib", "lens"}
        bad = [k for k in patch if k not in allowed]
        if bad:
            raise MachineError(f"Unbekannte Kamera-Einstellung: {', '.join(bad)}", "bad_request", 400)
        src = patch.get("source")
        if src is not None:
            kind = src.get("kind", self.settings["source"]["kind"])
            if kind not in ("none", "device", "url", "sim"):
                raise MachineError("Kameraquelle muss none, device, url oder sim sein.", "bad_request", 400)
            if kind == "url" and not str(src.get("url", self.settings["source"]["url"])).strip():
                raise MachineError("Bitte eine Stream-Adresse angeben.", "bad_request", 400)
        if "rotate" in patch and patch["rotate"] not in (0, 90, 180, 270):
            raise MachineError("Drehung muss 0, 90, 180 oder 270 Grad sein.", "bad_request", 400)
        with self._lock:
            old_src = dict(self.settings["source"])
            old_orient = (self.settings["rotate"], self.settings["mirror"])
            self.settings = _merge(self.settings, patch)
            if (self.settings["rotate"], self.settings["mirror"]) != old_orient:
                self.settings["calib"], self.settings["lens"] = None, None
                self.lens_views.clear()
            self._save()
            if src is not None and self.settings["source"] != old_src or (src is not None and self.source is None):
                self._open_source()
            if "markers" in patch and self.sim is not None:
                self._sim_markers()
            self._rebuild_geometry()
        return self.info()

    def _open_source(self) -> None:
        self._close_source()
        s = self.settings["source"]
        kind = s["kind"]
        if kind == "none":
            return
        if kind == "device":
            self.source = DeviceSource(int(s.get("index", 0)), int(s.get("width", 1920)), int(s.get("height", 1080)),
                                       int(s.get("fps", 30)))
        elif kind == "url":
            self.source = UrlSource(str(s["url"]).strip())
        elif kind == "sim":
            self.sim = SimCamera(self.m.cfg)
            self._sim_markers()
            self.source = SimSource(self._sim_frame)
        self.source.start()
        self._start_tracking()

    def _sim_markers(self) -> None:
        mk = self.settings["markers"]
        board = [{"id": b["id"], "x": b["x"], "y": b["y"], "size_mm": b.get("size_mm", mk["size_mm"])} for b in mk["board"]]
        g = mk.get("gondola") or {}
        self.sim.set_markers(board, g if g.get("enabled") else None, mk["dict"])

    def _sim_frame(self) -> np.ndarray:
        emu = self.m._emulator()
        if emu is None:
            return self.sim.render((), None)
        return self.sim.render(list(emu.trace), tuple(emu.physical_position()))

    def use_image(self, image: np.ndarray) -> None:
        """Feed a fixed image (tests / offline analysis of a saved snapshot)."""
        with self._lock:
            self._close_source()
            self.source = StillSource(image).start()
            self._start_tracking()

    def _close_source(self) -> None:
        self._track_stop.set()
        if self._track_thread and self._track_thread is not threading.current_thread():
            self._track_thread.join(timeout=2.0)
        src, self.source, self.sim = self.source, None, None
        if src is not None:
            src.stop()
        self._jpeg_cache.clear()

    def shutdown(self) -> None:
        with self._lock:
            self._close_source()

    # ------------------------------------------------------------------ frames
    def _orient(self, img: np.ndarray) -> np.ndarray:
        r = self.settings["rotate"]
        if r == 90:
            img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
        elif r == 180:
            img = cv2.rotate(img, cv2.ROTATE_180)
        elif r == 270:
            img = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
        return cv2.flip(img, 1) if self.settings["mirror"] else img

    def frame(self, timeout: float = 3.0, after_seq: int = 0):
        """(image, t, seq) of the newest oriented frame. Raises when no camera delivers."""
        src = self.source
        if src is None:
            raise MachineError("Keine Kamera gewählt (Reiter Kamera).", "no_camera", 409)
        f = src.wait_frame(after_seq, timeout) if after_seq or src.latest() is None else src.latest()
        if f is None:
            raise MachineError(src.error or "Die Kamera liefert noch kein Bild.", "no_frame", 503)
        return self._orient(f.image), f.t, f.seq

    def averaged(self, n: int = 6, timeout: float = 6.0) -> np.ndarray:
        """Mean of the next ``n`` distinct frames (less sensor noise for measuring)."""
        img, _, seq = self.frame()
        acc = img.astype(np.float32)
        k, t_end = 1, time.time() + timeout
        while k < n and time.time() < t_end:
            try:
                img, _, seq = self.frame(timeout=max(0.1, t_end - time.time()), after_seq=seq)
            except MachineError:
                break
            if img.shape != acc.shape:
                break
            acc += img
            k += 1
        return np.clip(acc / k, 0, 255).astype(np.uint8)

    def view_rect(self) -> tuple[float, float, float, float]:
        b, mg = self.m.cfg.board, float(self.settings["view"]["margin_mm"])
        return (-mg, -mg, b.width_mm + mg, b.height_mm + mg)

    # ------------------------------------------------------------------ board mapping
    def _rebuild_geometry(self) -> None:
        c, lens = self.settings.get("calib"), self.settings.get("lens")
        self.geom, self.geom_error = None, None
        if not c:
            return
        try:
            size = tuple(c["image_size"])
            K = dist = None
            if lens and tuple(lens["image_size"]) == size:
                K, dist = np.asarray(lens["K"], float), np.asarray(lens["dist"], float)
            self.geom = BoardCamera.fit(c["image_points"], c["board_points"], size, K, dist)
        except (CameraGeometryError, KeyError, TypeError, ValueError) as exc:
            self.geom_error = str(exc)
        self._jpeg_cache.clear()

    def geometry(self) -> BoardCamera:
        g = self.geom
        if g is None:
            raise MachineError("Die Kamera ist noch nicht auf die Tafel eingemessen (Ecken setzen oder Marker erkennen).",
                               "camera_uncalibrated", 409)
        if self.source is not None and self.source.latest() is not None:
            img, _, _ = self.frame()
            if (img.shape[1], img.shape[0]) != tuple(g.image_size):
                raise MachineError(f"Die Kamera-Auflösung hat sich geändert ({img.shape[1]}x{img.shape[0]} statt "
                                   f"{g.image_size[0]}x{g.image_size[1]}): bitte neu einmessen.", "camera_uncalibrated", 409)
        return g

    def set_correspondences(self, image_points, board_points, method: str = "corners", image_size=None) -> dict:
        if image_size is None:
            img, _, _ = self.frame()
            image_size = (img.shape[1], img.shape[0])
        with self._lock:
            lens = self.settings.get("lens")
            K = dist = None
            if lens and tuple(lens["image_size"]) == tuple(image_size):
                K, dist = lens["K"], lens["dist"]
            try:
                g = BoardCamera.fit(image_points, board_points, image_size, K, dist)
            except CameraGeometryError as exc:
                raise MachineError(str(exc), "bad_calibration", 422) from exc
            self.settings["calib"] = {"method": method, "image_points": np.asarray(image_points, float).tolist(),
                                      "board_points": np.asarray(board_points, float).tolist(),
                                      "image_size": [int(image_size[0]), int(image_size[1])], "t": time.time()}
            self._save()
            self.geom, self.geom_error = g, None
            self._jpeg_cache.clear()
        cov = g.coverage((0.0, 0.0, self.m.cfg.board.width_mm, self.m.cfg.board.height_mm))
        warnings = []
        if g.rms_mm > 3.0:
            warnings.append(f"Die Punkte passen schlecht zusammen ({g.rms_mm:.1f} mm Restfehler). Positionen prüfen.")
        return {"ok": True, "rms_px": g.rms_px, "rms_mm": g.rms_mm, "warnings": warnings, "coverage": cov}

    def clear_calibration(self) -> dict:
        with self._lock:
            self.settings["calib"] = None
            self._save()
            self._rebuild_geometry()
        return self.info()

    def suggest_board(self) -> dict:
        """Whiteboard outline found in the image (not applied; the UI shows draggable corners)."""
        img, _, _ = self.frame()
        q = detect.detect_board(img)
        b = self.m.cfg.board
        if q is None:
            raise MachineError("Die Tafel wurde im Bild nicht gefunden. Bitte die vier Ecken von Hand setzen.", "not_found", 422)
        return {"image_points": q.tolist(),
                "board_points": [[0.0, b.height_mm], [b.width_mm, b.height_mm], [b.width_mm, 0.0], [0.0, 0.0]],
                "image_size": [int(img.shape[1]), int(img.shape[0])]}

    def calibrate_from_markers(self) -> dict:
        """Homography from the ArUco markers glued to the board (all four corners of each marker)."""
        mk = self.settings["markers"]
        img = self.averaged(3)
        found = detect.detect_markers(img, mk["dict"])
        img_pts, brd_pts, used = [], [], []
        for b in mk["board"]:
            c = found.get(int(b["id"]))
            if c is None:
                continue
            s = float(b.get("size_mm", mk["size_mm"])) / 2
            x, y = float(b["x"]), float(b["y"])
            img_pts += c.tolist()
            brd_pts += [[x - s, y + s], [x + s, y + s], [x + s, y - s], [x - s, y - s]]
            used.append(int(b["id"]))
        missing = [int(b["id"]) for b in mk["board"] if int(b["id"]) not in used]
        if len(used) < 2:
            raise MachineError(f"Nur {len(used)} Tafel-Marker erkannt (nötig: mindestens 2, besser 4). "
                               f"Fehlend: {', '.join(map(str, missing)) or '-'}.", "not_found", 422)
        out = self.set_correspondences(img_pts, brd_pts, "markers", (img.shape[1], img.shape[0]))
        out["markers"], out["missing"] = used, missing
        if missing:
            out["warnings"].append(f"Marker {', '.join(map(str, missing))} nicht gefunden - mit allen vier ist es genauer.")
        return out

    # ------------------------------------------------------------------ lens
    def lens_capture(self, cols: int = CHESS_DEFAULT[0], rows: int = CHESS_DEFAULT[1]) -> dict:
        img, _, _ = self.frame()
        c = detect.find_chessboard(img, (int(cols), int(rows)))
        if c is None:
            raise MachineError(f"Kein Schachbrett mit {cols} x {rows} inneren Ecken im Bild gefunden.", "not_found", 422)
        with self._lock:
            self.lens_views.append(c)
            self._lens_pattern = (int(cols), int(rows), (img.shape[1], img.shape[0]))
        return {"views": len(self.lens_views)}

    def lens_compute(self, square_mm: float = 25.0) -> dict:
        with self._lock:
            pat = getattr(self, "_lens_pattern", None)
            if pat is None:
                raise MachineError("Noch keine Schachbrett-Aufnahmen.", "bad_request", 400)
            try:
                lens = calibrate_lens(self.lens_views, pat[:2], float(square_mm), pat[2])
            except CameraGeometryError as exc:
                raise MachineError(str(exc), "bad_calibration", 422) from exc
            self.settings["lens"] = lens
            self._save()
            self._rebuild_geometry()
        return {"lens": lens, "hint": "Danach die Tafel neu einmessen, falls die Entzerrung nicht mehr passt."}

    def lens_clear(self) -> dict:
        with self._lock:
            self.lens_views.clear()
            self.settings["lens"] = None
            self._save()
            self._rebuild_geometry()
        return self.info()

    # ------------------------------------------------------------------ tracking (gondola marker)
    def _start_tracking(self) -> None:
        self._track_stop = threading.Event()
        self._track_thread = threading.Thread(target=self._track_loop, args=(self._track_stop,), name="camera-track", daemon=True)
        self._track_thread.start()

    def _track_loop(self, stop: threading.Event) -> None:
        seq = 0
        while not stop.wait(0.2):
            try:
                seq = self._track_once(seq)
            except MachineError:
                pass
            except Exception:  # noqa: BLE001
                log.exception("camera tracking failed")

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
            self.m.log("warn", f"Kamera: die Gondel steht {dev:.1f} mm neben der Soll-Position "
                               "(Riemen übersprungen? Referenz falsch? Marker-Versatz einlernen?).")

    def learn_offset(self) -> dict:
        """Marker-to-pen offset from the current pose (machine idle, reference set)."""
        if self.m.mapping is None:
            raise MachineError("Erst die Referenz setzen.", "no_reference")
        tr, soll = self.track, self.m.phys_xy()
        if not tr or not tr.get("marker") or time.time() - tr["t"] > 2.0:
            raise MachineError("Der Gondel-Marker ist gerade nicht zu sehen.", "not_found", 422)
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
            raise MachineError("Zu wenig Bewegung aufgezeichnet: erst die Gondel ein paar Mal fahren lassen.", "bad_request", 400)
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

    # ------------------------------------------------------------------ images
    def jpeg(self, view: str = "raw", width: int | None = None, quality: int = 80, overlay: bool = False) -> tuple[bytes, dict]:
        img, t, seq = self.frame()
        key = (seq, view, width, quality, overlay)
        meta = {"seq": seq, "t": t}
        hit = self._jpeg_cache.get(key)
        if hit is not None:
            return hit, meta
        if view == "board":
            g = self.geometry()
            img = g.rectify(img, self.view_rect(), float(self.settings["view"]["px_per_mm"]))
        elif view != "raw":
            raise MachineError("Ansicht muss raw oder board sein.", "bad_request", 400)
        if overlay:
            from .overlay import draw_overlay
            img = draw_overlay(self, img, view)
        if width and 64 <= int(width) < img.shape[1]:
            k = int(width) / img.shape[1]
            img = cv2.resize(img, (int(width), max(1, int(img.shape[0] * k))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(max(30, min(95, quality)))])
        if not ok:
            raise MachineError("Bild konnte nicht kodiert werden.", "internal", 500)
        data = buf.tobytes()
        if len(self._jpeg_cache) > 12:
            self._jpeg_cache.clear()
        self._jpeg_cache[key] = data
        return data, meta

    # ------------------------------------------------------------------ state
    def info(self) -> dict:
        src, g = self.source, self.geom
        rect = self.view_rect()
        out = {"settings": copy.deepcopy(self.settings), "source": src.info() if src else None,
               "calibrated": g is not None, "geometry": g.to_json() if g else None, "geometry_error": self.geom_error,
               "view_rect": list(rect), "lens_views": len(self.lens_views), "track": self.track,
               "coverage": g.coverage((0.0, 0.0, self.m.cfg.board.width_mm, self.m.cfg.board.height_mm)) if g else []}
        return out
