"""CameraService: one per app. Owns the frame source, the board mapping and the gondola tracking.

Settings live in ``config/state/camera.json`` (written only when ``persist``), never in wbot.yaml:
they describe where this camera hangs today, not the machine. Errors are ``MachineError``, like the
rest of the API.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import cv2
import numpy as np

from ..errors import MachineError
from . import detect
from .geometry import BoardCamera, CameraGeometryError
from .simcam import SimCamera
from .service_tools import ToolsMixin
from .sources import DeviceSource, SimSource, Source, StillSource, UrlSource
from .tracking import TrackingMixin
from .guard import GuardMixin

if TYPE_CHECKING:
    from ..machine import Machine

log = logging.getLogger(__name__)
# Camera state lives next to the machine state; WBOT_STATE_DIR moves it (side servers, tests).
_STATE_DIR = Path(os.environ.get("WBOT_STATE_DIR") or Path(__file__).resolve().parent.parent.parent / "config" / "state")
CAMERA_FILE = _STATE_DIR / "camera.json"

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
    "view": {"px_per_mm": 1.0, "margin_mm": 30.0, "fps": 10},     # fps: how often the browser fetches a picture
    "line_mm": 2.0,              # marker line width (cross detection)
    "latency_ms": 150.0,
    "anchor_z_mm": 41.0,
    "watch": {"enabled": True},  # follow the board mapping when the camera was moved
    "people": {"enabled": True, "guard": "warn", "filter": False, "resume": True},         # pulley centres in front of the board (motors clamped to the frame)
}


def _merge(a: dict, b: dict) -> dict:
    out = copy.deepcopy(a)
    for k, v in (b or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


class CameraService(ToolsMixin, TrackingMixin, GuardMixin):
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
        self._init_guard()
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
            raise MachineError("Expected an object with camera settings.", "bad_request", 400)
        allowed = set(DEFAULTS) - {"calib", "lens"}
        bad = [k for k in patch if k not in allowed]
        if bad:
            raise MachineError(f"Unknown camera setting: {', '.join(bad)}", "bad_request", 400)
        src = patch.get("source")
        if src is not None:
            kind = src.get("kind", self.settings["source"]["kind"])
            if kind not in ("none", "device", "url", "sim"):
                raise MachineError("Camera source must be none, device, url or sim.", "bad_request", 400)
            if kind == "url" and not str(src.get("url", self.settings["source"]["url"])).strip():
                raise MachineError("Please give a stream address.", "bad_request", 400)
        if "rotate" in patch and patch["rotate"] not in (0, 90, 180, 270):
            raise MachineError("Rotation must be 0, 90, 180 or 270 degrees.", "bad_request", 400)
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

    def devices_in_use(self) -> dict[int, np.ndarray]:
        """index -> latest frame of the open webcam (a probe must not open it a second time)."""
        src = self.source
        f = src.latest() if isinstance(src, DeviceSource) else None
        return {src.index: f.image} if f is not None else {}

    def pick_webcam(self, timeout: float = 20.0) -> dict:
        """Use the open camera if it delivers a real picture, else the first webcam that does (virtual
        cameras without a source deliver black and are skipped)."""
        from .sources import looks_blank, probe_devices
        src = self.source
        f = src.latest() if src is not None else None
        if f is not None and not looks_blank(f.image):
            return {"index": getattr(src, "index", None), "kind": src.kind, "changed": False}
        devs = [d for d in probe_devices(6, 120, self.devices_in_use()) if not d["blank"]]
        if not devs:
            raise MachineError("No webcam delivers a picture. Plugged in? Lens cap off?", "no_camera", 409)
        idx = devs[0]["index"]
        self.configure({"source": {"kind": "device", "index": idx}})
        t_end = time.time() + timeout
        while time.time() < t_end:
            s2 = self.source
            if s2 is not None and s2.latest() is not None:
                return {"index": idx, "kind": "device", "changed": True}
            time.sleep(0.2)
        raise MachineError(f"Webcam {idx} does not deliver a picture.", "no_frame", 503)

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
            raise MachineError("No camera selected (Camera tab).", "no_camera", 409)
        f = src.wait_frame(after_seq, timeout) if after_seq or src.latest() is None else src.latest()
        if f is None:
            raise MachineError(src.error or "The camera delivers no picture yet.", "no_frame", 503)
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
            raise MachineError("The camera is not aligned to the board yet (detect the board or set the corners).",
                               "camera_uncalibrated", 409)
        if self.source is not None and self.source.latest() is not None:
            img, _, _ = self.frame()
            if (img.shape[1], img.shape[0]) != tuple(g.image_size):
                raise MachineError(f"The camera resolution changed ({img.shape[1]}x{img.shape[0]} instead of "
                                   f"{g.image_size[0]}x{g.image_size[1]}): please align the board again.", "camera_uncalibrated", 409)
        return g

    def set_correspondences(self, image_points, board_points, method: str = "corners", image_size=None,
                            image: np.ndarray | None = None) -> dict:
        if image is None and self.source is not None and self.source.latest() is not None:
            image, _, _ = self.frame()
        if image_size is None:
            if image is None:
                image, _, _ = self.frame()
            image_size = (image.shape[1], image.shape[0])
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
            if image is not None and (image.shape[1], image.shape[0]) == tuple(image_size):
                self._set_reference(image)          # for noticing later camera movement
        cov = g.coverage((0.0, 0.0, self.m.cfg.board.width_mm, self.m.cfg.board.height_mm))
        warnings = []
        if g.rms_mm > 3.0:
            warnings.append(f"The points do not fit well together ({g.rms_mm:.1f} mm residual). Check their positions.")
        return {"ok": True, "rms_px": g.rms_px, "rms_mm": g.rms_mm, "warnings": warnings, "coverage": cov}

    def clear_calibration(self) -> dict:
        with self._lock:
            self.settings["calib"] = None
            self._save()
            self._rebuild_geometry()
        return self.info()

    def suggest_board(self) -> dict:
        """Whiteboard outline found in the image (not applied; the UI shows draggable corners)."""
        img = self.averaged(3)
        from .boardfind import find_board
        found = find_board(img)                     # frame edges (+ corner caps): works on a white wall
        q = found[0] if found is not None else detect.detect_board(img)
        b = self.m.cfg.board
        if q is None:
            raise MachineError(_board_hint(img), "not_found", 422)
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
        if not used:
            raise MachineError("No board markers in the picture. Print and stick the markers first (marker sheet), or set the "
                               "corners by hand.", "not_found", 422)
        if len(used) < 2:
            raise MachineError(f"Only {len(used)} board markers found (at least 2 needed, 4 is better). "
                               f"Missing: {', '.join(map(str, missing)) or '-'}.", "not_found", 422)
        out = self.set_correspondences(img_pts, brd_pts, "markers", (img.shape[1], img.shape[0]), image=img)
        out["markers"], out["missing"] = used, missing
        if missing:
            out["warnings"].append(f"Marker {', '.join(map(str, missing))} not found - all four are more accurate.")
        return out

    # ------------------------------------------------------------------ images
    def jpeg(self, view: str = "raw", width: int | None = None, quality: int = 80, overlay: bool = False,
             hide_people: bool | None = None) -> tuple[bytes, dict]:
        img, t, seq = self.frame()
        if hide_people is None:
            hide_people = bool(self.settings["people"]["filter"])
        key = (seq, view, width, quality, overlay, hide_people)
        meta = {"seq": seq, "t": t}
        hit = self._jpeg_cache.get(key)
        if hit is not None:
            return hit, meta
        if hide_people and self.presence.present:
            img = self.presence.filtered(img)       # "see-through" person: remembered background instead
        if view == "board":
            g = self.geometry()
            img = g.rectify(img, self.view_rect(), float(self.settings["view"]["px_per_mm"]))
        elif view != "raw":
            raise MachineError("View must be raw or board.", "bad_request", 400)
        if overlay:
            from .overlay import draw_overlay
            img = draw_overlay(self, img, view)
        if width and 64 <= int(width) < img.shape[1]:
            k = int(width) / img.shape[1]
            img = cv2.resize(img, (int(width), max(1, int(img.shape[0] * k))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, int(max(30, min(95, quality)))])
        if not ok:
            raise MachineError("Picture could not be encoded.", "internal", 500)
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
               "watch": self.watch_state, "presence": self.presence.state, "queued": bool(self._queued), "queued_count": len(self._queued),
               "coverage": g.coverage((0.0, 0.0, self.m.cfg.board.width_mm, self.m.cfg.board.height_mm)) if g else []}
        return out


def _board_hint(img: np.ndarray) -> str:
    """Why the board was not found, in words a user can act on."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mean, sat = float(g.mean()), float((g > 250).mean())
    if mean < 8 or float(g.std()) < 3:
        why = "the picture is black or empty (wrong camera? lens cap?)"
    elif mean < 45:
        why = "the picture is too dark"
    elif sat > 0.25:
        why = "the picture is overexposed (bright light or a window behind the board?)"
    else:
        why = "no closed board frame was found: all four edges and corners must be in view and not covered"
    return f"Board not found: {why}. You can also mark the four corners by hand."

