"""CameraService parts: motors / anchors measurement and the lens (chessboard) calibration."""
from __future__ import annotations

import numpy as np

from ..errors import MachineError
from . import detect
from .geometry import CameraGeometryError, calibrate_lens

CHESS_DEFAULT = (9, 6)


class ToolsMixin:
    # ------------------------------------------------------------------ motors / anchors
    def suggest_motors(self) -> dict:
        """Motor bodies found above the board (a suggestion; the user drags the points onto the pulley centres)."""
        g = self.geometry()
        img = self.averaged(3)
        b, z, s = self.m.cfg.board, float(self.settings["anchor_z_mm"]), 2.0
        rect = (-200.0, b.height_mm - 30.0, b.width_mm + 200.0, b.height_mm + 300.0)
        strip = g.rectify(img, rect, s, border=(255, 255, 255), z=z)
        pts = detect.find_motors(strip, s)
        if len(pts) < 2:
            raise MachineError("The motors were not found reliably. Please drag the two points onto the "
                               "pulley centres by hand.", "not_found", 422)
        brd = [[rect[0] + (x + 0.5) / s, rect[3] - (y + 0.5) / s] for x, y in pts]
        return {"image_points": g.to_image(np.asarray(brd), z).tolist(), "board_points": brd}

    def measure_anchors(self, left_px, right_px, z: float | None = None) -> dict:
        """Anchor (pulley centre) positions in board mm from their pixel positions, compared with the config."""
        g = self.geometry()
        z = float(self.settings["anchor_z_mm"] if z is None else z)
        L, R = g.to_board(np.array([left_px, right_px], float), z)
        if not (np.all(np.isfinite(L)) and np.all(np.isfinite(R))) or L[0] >= R[0]:
            raise MachineError("These points give no valid anchors (left must be left of right).", "bad_request", 400)
        geo, cfg = self.m.cfg.geometry, self.m.cfg
        res = max(g.mm_per_px(L), g.mm_per_px(R))
        out = {"anchor_left": [round(float(L[0]), 1), round(float(L[1]), 1)],
               "anchor_right": [round(float(R[0]), 1), round(float(R[1]), 1)],
               "config_left": list(geo.anchor_left), "config_right": list(geo.anchor_right),
               "distance_mm": round(float(np.linalg.norm(R - L)), 1),
               "config_distance_mm": round(float(np.linalg.norm(np.subtract(geo.anchor_right, geo.anchor_left))), 1),
               "uncertainty_mm": round(2.5 * res + 0.05 * z, 1), "z_mm": z, "warnings": []}
        dl = float(np.linalg.norm(L - np.asarray(geo.anchor_left)))
        dr = float(np.linalg.norm(R - np.asarray(geo.anchor_right)))
        out["deviation_mm"] = [round(dl, 1), round(dr, 1)]
        if max(dl, dr) > 30:
            out["warnings"].append("The measured anchors are far from the configuration. Either the motors really sit "
                                   "elsewhere (then apply and check the allowed drawing area) or the board alignment "
                                   "or the points are off.")
        out["patch"] = {"geometry": {"anchor_left": out["anchor_left"], "anchor_right": out["anchor_right"]},
                        "firmware": {"anchor_left": [float(v) for v in cfg.fw_anchor_left],
                                     "anchor_right": [float(v) for v in cfg.fw_anchor_right]}}
        # the allowed drawing area that fits these anchors (applied together with them)
        from ..config import deep_update
        from ..drawarea import suggest_area
        try:
            area = suggest_area(deep_update(cfg, {"geometry": out["patch"]["geometry"]}))
            out["area"] = area
            out["patch"]["board"] = area["margins"]
        except (ValueError, Exception) as exc:  # noqa: BLE001 - invalid geometry or nothing usable
            out["area"] = None
            out["warnings"].append(f"No usable drawing area found for these motor positions ({exc}).")
        return out

    def apply_anchors(self, left_px, right_px, z: float | None = None) -> dict:
        res = self.measure_anchors(left_px, right_px, z)
        res["config"] = self.m.update_config(res["patch"])
        self.m.log("info", f"Anchors applied from the camera: left {res['anchor_left']}, right {res['anchor_right']} mm")
        return res

    # ------------------------------------------------------------------ lens
    def lens_capture(self, cols: int = CHESS_DEFAULT[0], rows: int = CHESS_DEFAULT[1]) -> dict:
        img, _, _ = self.frame()
        c = detect.find_chessboard(img, (int(cols), int(rows)))
        if c is None:
            raise MachineError(f"No chessboard with {cols} x {rows} inner corners found in the picture.", "not_found", 422)
        with self._lock:
            self.lens_views.append(c)
            self._lens_pattern = (int(cols), int(rows), (img.shape[1], img.shape[0]))
        return {"views": len(self.lens_views)}

    def lens_compute(self, square_mm: float = 25.0) -> dict:
        with self._lock:
            pat = getattr(self, "_lens_pattern", None)
            if pat is None:
                raise MachineError("No chessboard shots yet.", "bad_request", 400)
            try:
                lens = calibrate_lens(self.lens_views, pat[:2], float(square_mm), pat[2])
            except CameraGeometryError as exc:
                raise MachineError(str(exc), "bad_calibration", 422) from exc
            self.settings["lens"] = lens
            self._save()
            self._rebuild_geometry()
        return {"lens": lens, "hint": "Align the board again if the picture no longer fits."}

    def lens_clear(self) -> dict:
        with self._lock:
            self.lens_views.clear()
            self.settings["lens"] = None
            self._save()
            self._rebuild_geometry()
        return self.info()

    # ------------------------------------------------------------------ one click
    def auto_setup(self, pick_camera: bool = True) -> dict:
        """One click: find the board and align to it, then find the motors and measure the anchors.
        The board alignment is applied (it only concerns the camera); the anchors are returned for
        confirmation, because they change the drawing geometry (apply with POST /api/camera/anchors)."""
        out: dict = {"board": None, "motors": None, "camera": None, "errors": []}
        if pick_camera:
            try:
                out["camera"] = self.pick_webcam()
            except MachineError as exc:
                out["errors"].append(exc.message)
                return out
        try:
            sug = self.suggest_board()
            res = self.set_correspondences(sug["image_points"], sug["board_points"], "auto", tuple(sug["image_size"]))
            out["board"] = {**res, "image_points": sug["image_points"]}
        except MachineError as exc:
            out["errors"].append(exc.message)
            return out
        try:
            mot = self.suggest_motors()
            meas = self.measure_anchors(mot["image_points"][0], mot["image_points"][1])
            out["motors"] = {**meas, "image_points": mot["image_points"]}
        except MachineError as exc:
            out["errors"].append(exc.message)
        return out

    # ------------------------------------------------------------------ gondola position from the belts
    def locate_gondola(self, apply: bool = False) -> dict:
        """Where the pen is, from the two belts in the picture (``gondola_find``). ``apply`` sets it as the
        machine's reference (the same as "gondola is here" in the UI)."""
        from .gondola_find import locate
        g = self.geometry()
        res = locate(self.m.cfg, g, self.averaged(3))
        if apply:
            if not res["ok"]:
                raise MachineError(res.get("error") or "Gondola not found.", "not_found", 422)
            res["state"] = self.m.set_reference(*res["pen"])
        return res
