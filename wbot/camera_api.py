"""REST endpoints of the camera and the tape-measure calibration (registered by ``api.app``).

    GET    /api/camera                      -> info (settings, source, geometry, coverage, track)
    PUT    /api/camera {patch}              -> info   (source, rotate, mirror, markers, tracking, view, line_mm, latency_ms)
    GET    /api/camera/devices              -> [{index, size, backend, thumb (data URL)}]   (slow: probes devices)
    GET    /api/camera/frame.jpg?view=raw|board&w=&q=&overlay=0|1   -> image/jpeg (X-Frame-Seq, X-Frame-T)
    GET    /api/camera/suggest-board        -> {image_points, board_points, image_size}  (not applied)
    POST   /api/camera/calibrate {image_points, board_points, method?} -> {rms_px, rms_mm, warnings, coverage}
    POST   /api/camera/calibrate/markers    -> same + {markers, missing}
    DELETE /api/camera/calibrate            -> info
    POST   /api/camera/lens/capture {cols, rows} | /lens/compute {square_mm} ; DELETE /api/camera/lens
    POST   /api/camera/track/learn-offset   -> {offset_mm} ;  POST /api/camera/track/latency -> {latency_ms}
    POST   /api/camera/measure {targets, arm_mm, frames?} -> {points:[{target, measured|null, status, ...}], ...}
    POST   /api/camera/autocal {n_x, n_y, inset_mm, draw, free} ; GET /api/camera/autocal ; POST /api/camera/autocal/cancel
    GET    /api/camera/markers.svg          -> printable sheet (print at 100 %)
    GET    /api/calibration/tape?cols&rows&arm_mm -> {points, pairs, pattern}
    POST   /api/calibration/tape/fit {pairs:[{a:[x,y], b:[x,y], measured_mm}], free?} ; .../tape/apply
"""
from __future__ import annotations

import asyncio
import base64
import math
from typing import Any

from fastapi import FastAPI
from fastapi.responses import Response

from .camera.autocal import AutoCalibration, measure_crosses
from .camera.service import CameraService
from .calibration import CalibrationError
from .errors import MachineError

MAX_MM = 1.0e5


def _pts(v: Any, what: str, n_min: int = 1) -> list[list[float]]:
    try:
        out = [[float(p[0]), float(p[1])] for p in v]
    except (TypeError, ValueError, IndexError, KeyError):
        raise MachineError(f"{what}: erwartet eine Liste von [x, y].", "bad_request", 400) from None
    if len(out) < n_min or not all(math.isfinite(a) and math.isfinite(b) and abs(a) < MAX_MM and abs(b) < MAX_MM for a, b in out):
        raise MachineError(f"{what}: zu wenige oder ungültige Punkte.", "bad_request", 400)
    return out


def markers_svg(svc: CameraService) -> str:
    """A4 portrait sheet with the board markers and the gondola marker at real size."""
    from .camera.detect import marker_image
    mk = svc.settings["markers"]
    items = [(int(b["id"]), float(b.get("size_mm", mk["size_mm"])),
              f"Tafel-Marker {b['id']}: Mitte bei x {b['x']:g} | y {b['y']:g} mm, Oberkante nach oben") for b in mk["board"]]
    g = mk.get("gondola") or {}
    if g.get("enabled", True):
        items.append((int(g.get("id", 10)), float(g.get("size_mm", 50.0)), f"Gondel-Marker {g.get('id', 10)}: vorn auf die Gondel"))
    bits = {"4X4_50": 4, "5X5_100": 5, "6X6_250": 6}[mk["dict"]] + 2
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" viewBox="0 0 210 297">',
             '<rect width="210" height="297" fill="#fff"/>',
             '<text x="105" y="12" font-size="5" text-anchor="middle" font-family="sans-serif">W-BOT Kamera-Marker - '
             'in Originalgröße drucken (100 %, nicht anpassen)</text>']
    for k, (mid, size, label) in enumerate(items[:6]):
        cx, cy = 55 + (k % 2) * 100, 55 + (k // 2) * 88
        img = marker_image(mid, bits * 10, mk["dict"])
        cell = size / bits
        x0, y0 = cx - size / 2, cy - size / 2
        parts.append(f'<rect x="{x0 - cell:.2f}" y="{y0 - cell:.2f}" width="{size + 2 * cell:.2f}" height="{size + 2 * cell:.2f}" '
                     'fill="none" stroke="#bbb" stroke-width="0.2" stroke-dasharray="1,1"/>')
        for r in range(bits):
            for c in range(bits):
                if img[r * 10 + 5, c * 10 + 5] < 128:
                    parts.append(f'<rect x="{x0 + c * cell:.3f}" y="{y0 + r * cell:.3f}" width="{cell + 0.02:.3f}" '
                                 f'height="{cell + 0.02:.3f}" fill="#000"/>')
        parts.append(f'<text x="{cx}" y="{cy + size / 2 + cell + 6}" font-size="3.4" text-anchor="middle" '
                     f'font-family="sans-serif">{label} ({size:g} mm)</text>')
    parts.append("</svg>")
    return "".join(parts)


def register_camera_routes(app: FastAPI, m, svc: CameraService) -> None:
    auto = AutoCalibration(svc)
    app.state.autocal = auto

    async def run(fn, *a, **kw):
        return await asyncio.to_thread(fn, *a, **kw)

    @app.get("/api/camera")
    async def cam_info():
        return svc.info()

    @app.put("/api/camera")
    async def cam_put(patch: dict[str, Any]):
        return await run(svc.configure, patch)

    @app.get("/api/camera/devices")
    async def cam_devices(max_index: int = 6):
        from .camera.sources import probe_devices
        devs = await run(probe_devices, max(1, min(int(max_index), 10)))
        return [{**{k: v for k, v in d.items() if k != "thumb"},
                 "thumb": "data:image/jpeg;base64," + base64.b64encode(d["thumb"]).decode()} for d in devs]

    @app.get("/api/camera/frame.jpg")
    async def cam_frame(view: str = "raw", w: int | None = None, q: int = 75, overlay: int = 0):
        data, meta = await run(svc.jpeg, view, w, q, bool(overlay))
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Frame-Seq": str(meta["seq"]),
                                                                  "X-Frame-T": f"{meta['t']:.3f}"})

    @app.get("/api/camera/suggest-board")
    async def cam_suggest():
        return await run(svc.suggest_board)

    @app.post("/api/camera/calibrate")
    async def cam_calibrate(body: dict[str, Any]):
        ip = _pts(body.get("image_points"), "image_points", 4)
        bp = _pts(body.get("board_points"), "board_points", 4)
        return await run(svc.set_correspondences, ip, bp, str(body.get("method") or "corners"))

    @app.post("/api/camera/calibrate/markers")
    async def cam_calibrate_markers():
        return await run(svc.calibrate_from_markers)

    @app.delete("/api/camera/calibrate")
    async def cam_calibrate_clear():
        return await run(svc.clear_calibration)

    @app.post("/api/camera/lens/capture")
    async def cam_lens_capture(body: dict[str, Any] | None = None):
        b = body or {}
        return await run(svc.lens_capture, int(b.get("cols", 9)), int(b.get("rows", 6)))

    @app.post("/api/camera/lens/compute")
    async def cam_lens_compute(body: dict[str, Any] | None = None):
        return await run(svc.lens_compute, float((body or {}).get("square_mm", 25.0)))

    @app.delete("/api/camera/lens")
    async def cam_lens_clear():
        return await run(svc.lens_clear)

    @app.post("/api/camera/track/learn-offset")
    async def cam_learn():
        return await run(svc.learn_offset)

    @app.post("/api/camera/track/latency")
    async def cam_latency():
        return await run(svc.estimate_latency)

    @app.post("/api/camera/measure")
    async def cam_measure(body: dict[str, Any]):
        tg = _pts(body.get("targets"), "targets", 1)
        try:
            arm = float(body.get("arm_mm", 20.0))
        except (TypeError, ValueError):
            raise MachineError("arm_mm muss eine Zahl sein.", "bad_request", 400) from None
        if not 2.0 <= arm <= 40.0:
            raise MachineError("arm_mm muss zwischen 2 und 40 mm liegen.", "bad_request", 400)
        return await run(measure_crosses, svc, tg, arm, frames=int(body.get("frames", 6)))

    @app.post("/api/camera/autocal")
    async def cam_autocal(body: dict[str, Any] | None = None):
        return await run(auto.start, body or {})

    @app.get("/api/camera/autocal")
    async def cam_autocal_state():
        return auto.state

    @app.post("/api/camera/autocal/cancel")
    async def cam_autocal_cancel():
        return await run(auto.cancel)

    @app.get("/api/camera/markers.svg")
    async def cam_markers():
        return Response(markers_svg(svc), media_type="image/svg+xml")

    # ------------------------------------------------------------------ tape measure
    @app.get("/api/calibration/tape")
    async def tape(cols: int = 3, rows: int = 3, arm_mm: float = 12.0):
        from .calibration_tape import tape_plan
        try:
            return tape_plan(m.cfg, cols, rows, arm_mm)
        except CalibrationError as exc:
            raise MachineError(str(exc), "bad_request", 400) from exc

    def _tape_fit(body: dict) -> dict:
        from .calibration_api import _reference
        from .calibration_tape import fit_distances
        raw = body.get("pairs")
        if not isinstance(raw, list) or not raw:
            raise MachineError("Keine Abstände übergeben ('pairs').", "bad_request", 400)
        pairs = []
        for i, p in enumerate(raw, 1):
            if not isinstance(p, dict):
                raise MachineError(f"Abstand {i}: Objekt mit a, b, measured_mm erwartet.", "bad_request", 400)
            a, b = _pts([p.get("a")], f"Abstand {i} a")[0], _pts([p.get("b")], f"Abstand {i} b")[0]
            try:
                d = float(p.get("measured_mm"))
            except (TypeError, ValueError):
                raise MachineError(f"Abstand {i}: measured_mm fehlt.", "bad_request", 400) from None
            pairs.append((tuple(a), tuple(b), d))
        free = body.get("free")
        if free is not None and (not isinstance(free, list) or not all(isinstance(f, str) for f in free)):
            raise MachineError("'free' muss eine Liste von Parameternamen sein.", "bad_request", 400)
        ref, ref_fw = _reference(m)
        try:
            return fit_distances(m.cfg, ref, ref_fw, pairs, free)
        except CalibrationError as exc:
            raise MachineError(str(exc), "bad_request", 400) from exc

    @app.post("/api/calibration/tape/fit")
    async def tape_fit(body: dict[str, Any]):
        return await run(_tape_fit, body)

    @app.post("/api/calibration/tape/apply")
    async def tape_apply(body: dict[str, Any]):
        def go():
            res = _tape_fit(body)
            if not res["patch"]:
                return {"applied": False, "result": res, "config": m.config_json()}
            return {"applied": True, "result": res, "config": m.update_config(res["patch"])}
        return await run(go)
