"""REST endpoints of the camera and the tape-measure calibration (registered by ``api.app``).

    GET    /api/camera                      -> info (settings, source, geometry, coverage, track)
    PUT    /api/camera {patch}              -> info   (source, rotate, mirror, markers, tracking, view, line_mm, latency_ms)
    GET    /api/camera/devices              -> [{index, size, backend, thumb (data URL)}]   (slow: probes devices)
    GET    /api/camera/frame.jpg?view=raw|board&w=&q=&overlay=0|1&hide=0|1 -> image/jpeg (hide: person filtered out)
    POST   /api/camera/region {rect, px_per_mm?, hide?, frames?, inset_mm?} -> sharp top-down crop + ink in it
    GET    /api/camera/free-space?w=&h=&margin=&count= -> empty places of that size in the usable area
    GET    /api/camera/occupancy?cell=10 -> what is already written: cell grid (0 free, 1 ink, 2 unseen) + boxes
    POST   /api/camera/overlap {strokes | drawing, margin_mm?} -> overlap_pct, hit boxes, free place to move to
    GET    /api/camera/suggest-board        -> {image_points, board_points, image_size}  (not applied)
    POST   /api/camera/calibrate {image_points, board_points, method?} -> {rms_px, rms_mm, warnings, coverage}
    POST   /api/camera/calibrate/markers    -> same + {markers, missing}
    DELETE /api/camera/calibrate            -> info
    POST   /api/camera/auto-setup?pick_camera=1 -> {camera, board (applied), motors (measured, not applied), errors}
    GET    /api/camera/motors/suggest       -> {image_points, board_points}  (motor bodies, not applied)
    POST   /api/camera/gondola/locate {apply?} -> {pen, belts, contrast, ok, error?} (+ state when applied as reference)
    GET    /api/drawing-area/suggest        -> {rect, margins} largest usable area for the configured anchors
    POST   /api/camera/anchors {left:[u,v], right:[u,v], z_mm?, apply?} -> measured anchors vs config (+ config)
    POST   /api/camera/queue-job {job body}  -> {queued}  (starts once nobody stands at the board) ; DELETE cancels
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
        raise MachineError(f"{what}: expected a list of [x, y].", "bad_request", 400) from None
    if len(out) < n_min or not all(math.isfinite(a) and math.isfinite(b) and abs(a) < MAX_MM and abs(b) < MAX_MM for a, b in out):
        raise MachineError(f"{what}: too few or invalid points.", "bad_request", 400)
    return out


def markers_svg(svc: CameraService) -> str:
    """A4 portrait sheet with the board markers and the gondola marker at real size."""
    from .camera.detect import marker_image
    mk = svc.settings["markers"]
    items = [(int(b["id"]), float(b.get("size_mm", mk["size_mm"])),
              f"Board marker {b['id']}: centre at x {b['x']:g} | y {b['y']:g} mm, top edge up") for b in mk["board"]]
    g = mk.get("gondola") or {}
    if g.get("enabled", True):
        items.append((int(g.get("id", 10)), float(g.get("size_mm", 50.0)), f"Gondola marker {g.get('id', 10)}: on the front of the gondola"))
    bits = {"4X4_50": 4, "5X5_100": 5, "6X6_250": 6}[mk["dict"]] + 2
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" viewBox="0 0 210 297">',
             '<rect width="210" height="297" fill="#fff"/>',
             '<text x="105" y="12" font-size="5" text-anchor="middle" font-family="sans-serif">W-BOT camera markers - '
             'print at original size (100 %, no scaling)</text>']
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
        devs = await run(probe_devices, max(1, min(int(max_index), 10)), 240, svc.devices_in_use())
        return [{**{k: v for k, v in d.items() if k != "thumb"},
                 "thumb": "data:image/jpeg;base64," + base64.b64encode(d["thumb"]).decode()} for d in devs]

    @app.get("/api/camera/frame.jpg")
    async def cam_frame(view: str = "raw", w: int | None = None, q: int = 75, overlay: int = 0, hide: int | None = None):
        data, meta = await run(svc.jpeg, view, w, q, bool(overlay), None if hide is None else bool(hide))
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Frame-Seq": str(meta["seq"]),
                                                                  "X-Frame-T": f"{meta['t']:.3f}"})

    @app.post("/api/camera/region")
    async def cam_region(body: dict[str, Any]):
        from .camera.board_read import read_region
        r = await run(read_region, svc, body.get("rect"), float(body.get("px_per_mm") or 4.0),
                      bool(body.get("hide", False)), int(body.get("frames") or 3), float(body.get("inset_mm") or 0.0))
        r["image"] = "data:image/jpeg;base64," + base64.b64encode(r.pop("jpeg")).decode()
        return r

    @app.get("/api/camera/free-space")
    async def cam_free_space(w: float, h: float, margin: float = 15.0, count: int = 3):
        from .camera.board_read import free_space
        return await run(free_space, svc, w, h, margin, max(1, min(int(count), 8)))

    @app.get("/api/camera/occupancy")
    async def cam_occupancy(cell: float = 10.0):
        from .camera.board_read import occupancy
        return await run(occupancy, svc, cell)

    @app.post("/api/camera/overlap")
    async def cam_overlap(body: dict[str, Any]):
        from .camera.board_read import overlap
        strokes = body.get("strokes")
        if strokes is None and isinstance(body.get("drawing"), dict):
            strokes = body["drawing"].get("strokes")
        return await run(overlap, svc, strokes, float(body.get("margin_mm", 5.0)))

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

    @app.post("/api/camera/auto-setup")
    async def cam_auto_setup(pick_camera: int = 1):
        return await run(svc.auto_setup, bool(pick_camera))

    @app.get("/api/drawing-area/suggest")
    async def drawing_area():
        from .drawarea import suggest_area

        def go():
            try:
                return suggest_area(m.cfg)
            except ValueError as exc:
                raise MachineError(str(exc), "not_found", 422) from exc
        return await run(go)

    @app.post("/api/camera/gondola/locate")
    async def cam_gondola(body: dict[str, Any] | None = None):
        return await run(svc.locate_gondola, bool((body or {}).get("apply")))

    @app.get("/api/camera/motors/suggest")
    async def cam_motors():
        return await run(svc.suggest_motors)

    @app.post("/api/camera/anchors")
    async def cam_anchors(body: dict[str, Any]):
        pts = _pts([body.get("left"), body.get("right")], "left/right")
        z = body.get("z_mm")
        fn = svc.apply_anchors if body.get("apply") else svc.measure_anchors
        return await run(fn, pts[0], pts[1], None if z is None else float(z))

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
            raise MachineError("arm_mm must be a number.", "bad_request", 400) from None
        if not 2.0 <= arm <= 40.0:
            raise MachineError("arm_mm must be between 2 and 40 mm.", "bad_request", 400)
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

    @app.post("/api/camera/queue-job")
    async def cam_queue(body: dict[str, Any]):
        return await run(svc.queue_job, body)

    @app.delete("/api/camera/queue-job")
    async def cam_unqueue():
        return await run(svc.cancel_queue)

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
            raise MachineError("No distances given ('pairs').", "bad_request", 400)
        pairs = []
        for i, p in enumerate(raw, 1):
            if not isinstance(p, dict):
                raise MachineError(f"Distance {i}: object with a, b, measured_mm expected.", "bad_request", 400)
            a, b = _pts([p.get("a")], f"Distance {i} a")[0], _pts([p.get("b")], f"Distance {i} b")[0]
            try:
                d = float(p.get("measured_mm"))
            except (TypeError, ValueError):
                raise MachineError(f"Distance {i}: measured_mm missing.", "bad_request", 400) from None
            pairs.append((tuple(a), tuple(b), d))
        free = body.get("free")
        if free is not None and (not isinstance(free, list) or not all(isinstance(f, str) for f in free)):
            raise MachineError("'free' must be a list of parameter names.", "bad_request", 400)
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
