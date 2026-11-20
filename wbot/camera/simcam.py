"""Synthetic camera: films the simulator's *real* (plant) trace like a badly placed webcam would.

A pinhole camera off to the side and below the board centre, slightly rolled, optional barrel
distortion, blur and sensor noise. It shows the aluminium frame, the board, ArUco markers glued to
the board, the ink the pen really left (``FluidNCEmulator.trace`` is physical mm), the belts and the
gondola (with its marker). This makes overlay, board detection, tracking and the camera
auto-calibration testable end to end without hardware. ``true_H`` is the ground truth.
"""
from __future__ import annotations

import math

import cv2
import numpy as np

from ..config import Config
from .detect import marker_image

GONDOLA_W, GONDOLA_BELOW, GONDOLA_ABOVE, GONDOLA_Z = 76.0, 34.0, 190.0, 36.0
BELT_Z, ANCHOR_Z = 30.0, 41.0


def _hex_bgr(hx: str) -> tuple[int, int, int]:
    hx = hx.lstrip("#")
    r, g, b = int(hx[0:2], 16), int(hx[2:4], 16), int(hx[4:6], 16)
    return (b, g, r)


class SimCamera:
    def __init__(self, cfg: Config, *, size=(1920, 1080), focal: float = 1380.0,
                 center=(980.0, 330.0, 2250.0), look_at=(760.0, 520.0, 0.0), roll_deg: float = 2.5,
                 k1: float = 0.0, noise: float = 2.5, blur: float = 0.6, line_mm: float = 2.2, seed: int = 0):
        self.cfg, self.size = cfg, (int(size[0]), int(size[1]))
        w, h = self.size
        self.K = np.array([[focal, 0, w / 2], [0, focal, h / 2], [0, 0, 1]], float)
        self.dist = np.array([k1, 0, 0, 0, 0], float)
        C = np.asarray(center, float)
        f = np.asarray(look_at, float) - C
        f /= np.linalg.norm(f)
        x = np.cross(f, (0.0, 1.0, 0.0))
        x /= np.linalg.norm(x)
        y = np.cross(f, x)
        R = np.stack([x, y, f])
        c, s = math.cos(math.radians(roll_deg)), math.sin(math.radians(roll_deg))
        R = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]]) @ R
        self.R, self.t, self.C = R, -R @ C, C
        self.true_H = self.K @ np.column_stack([R[:, 0], R[:, 1], self.t])
        self.true_H /= self.true_H[2, 2]
        self.noise, self.blur, self.line_mm = float(noise), float(blur), float(line_mm)
        self.rng = np.random.default_rng(seed)
        self.person: tuple[float, float] | None = None     # (x, y) board mm of a person's chest, standing in front
        self.board_markers: list[dict] = []
        self.gondola_marker: dict | None = None
        self.dict_name = "4X4_50"
        self._static: np.ndarray | None = None
        self._ink: np.ndarray | None = None
        self._ink_n = 0
        self._dmap = None
        if k1:
            gx, gy = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
            pts = np.column_stack([gx.ravel(), gy.ravel()]).reshape(-1, 1, 2)
            und = cv2.undistortPoints(pts, self.K, self.dist, P=self.K).reshape(h, w, 2)
            self._dmap = (und[:, :, 0].copy(), und[:, :, 1].copy())

    # ------------------------------------------------------------------ scene setup
    def set_markers(self, board: list[dict] | None, gondola: dict | None, dict_name: str = "4X4_50") -> None:
        self.board_markers, self.gondola_marker, self.dict_name = list(board or []), gondola, dict_name
        self._static = None

    def project(self, pts, z=0.0) -> np.ndarray:
        """Board points (N,2) at height z (scalar or (N,)) -> ideal (undistorted) pixels."""
        p = np.asarray(pts, float).reshape(-1, 2)
        zz = np.broadcast_to(np.asarray(z, float), (len(p),))
        X = np.column_stack([p, zz]) @ self.R.T + self.t
        return X[:, :2] / X[:, 2:3] * np.array([self.K[0, 0], self.K[1, 1]]) + self.K[:2, 2]

    def scale_at(self, xy) -> float:
        q = self.project([xy, (xy[0] + 1.0, xy[1])])
        return float(np.linalg.norm(q[1] - q[0]))

    def _poly(self, img, pts, color, z=0.0):
        q = self.project(pts, z)
        cv2.fillPoly(img, [np.round(q * 16).astype(np.int32)], color, cv2.LINE_AA, shift=4)

    def _marker(self, img, m: dict, z: float = 0.0, offset=(0.0, 0.0)):
        side, mid = float(m.get("size_mm", 60.0)), int(m["id"])
        cx, cy = float(m["x"]) + offset[0], float(m["y"]) + offset[1]
        pad = side * 0.2
        px = 240
        mk = marker_image(mid, px, self.dict_name)
        pp = int(px * pad / side)
        tile = cv2.copyMakeBorder(mk, pp, pp, pp, pp, cv2.BORDER_CONSTANT, value=255)
        tile = cv2.cvtColor(tile, cv2.COLOR_GRAY2BGR)
        hs = side / 2 + pad
        quad = self.project([(cx - hs, cy + hs), (cx + hs, cy + hs), (cx + hs, cy - hs), (cx - hs, cy - hs)], z)
        n = tile.shape[0]
        src = np.float32([[0, 0], [n, 0], [n, n], [0, n]])
        x0, y0 = np.floor(quad.min(axis=0)).astype(int)
        x1, y1 = np.ceil(quad.max(axis=0)).astype(int)
        H, W = img.shape[:2]
        x0, y0, x1, y1 = max(0, x0), max(0, y0), min(W, x1 + 1), min(H, y1 + 1)
        if x1 <= x0 or y1 <= y0:
            return
        M = cv2.getPerspectiveTransform(src, np.float32(quad - (x0, y0)))
        warped = cv2.warpPerspective(tile, M, (x1 - x0, y1 - y0), flags=cv2.INTER_AREA)
        mask = cv2.warpPerspective(np.full((n, n), 255, np.uint8), M, (x1 - x0, y1 - y0))
        roi = img[y0:y1, x0:x1]
        roi[mask > 127] = (warped[mask > 127] * 0.92).astype(np.uint8)

    def _static_layer(self) -> np.ndarray:
        if self._static is not None:
            return self._static
        w, h = self.size
        img = np.empty((h, w, 3), np.uint8)
        img[:] = (206, 208, 211)          # a white wall, like the real test setup
        b = self.cfg.board
        fr = 28.0
        self._poly(img, [(-fr, -fr), (b.width_mm + fr, -fr), (b.width_mm + fr, b.height_mm + fr), (-fr, b.height_mm + fr)],
                   (196, 198, 202), 6.0)
        self._poly(img, [(0, 0), (b.width_mm, 0), (b.width_mm, b.height_mm), (0, b.height_mm)], (236, 239, 241))
        g = self.cfg.geometry
        for ax, ay in (g.anchor_left, g.anchor_right):              # motor clamp on the frame, motor body, pulley
            self._poly(img, [(ax - 45, b.height_mm - 5), (ax + 45, b.height_mm - 5), (ax + 45, b.height_mm + 14),
                             (ax - 45, b.height_mm + 14)], (30, 30, 34), 12.0)
            self._poly(img, [(ax - 21, ay - 21), (ax + 21, ay - 21), (ax + 21, ay + 21), (ax - 21, ay + 21)], (70, 72, 78), ANCHOR_Z)
        for m in self.board_markers:
            self._marker(img, m)
        gx, gy = np.meshgrid(np.linspace(-1, 1, w), np.linspace(-1, 1, h))
        light = (1.0 - 0.10 * (gx * gx + gy * gy) + 0.04 * gx).astype(np.float32)
        self._static = np.clip(img * light[:, :, None], 0, 255).astype(np.uint8)
        return self._static

    def _ink_layer(self, trace) -> np.ndarray:
        """Ink drawn so far (incremental; restarts when the trace shrinks)."""
        n = len(trace)
        if self._ink is None or n < self._ink_n:
            w, h = self.size
            self._ink = np.zeros((h, w, 4), np.uint8)
            self._ink_n = 0
        segs = [s for s in trace[self._ink_n:n] if s.pen_down]
        self._ink_n = n
        if segs:
            cols = self.cfg.colors
            by_col: dict[int, list] = {}
            for s in segs:
                by_col.setdefault(int(s.color_slot or 0), []).append(s)
            mid = (self.cfg.board.width_mm / 2, self.cfg.board.height_mm / 2)
            thick = max(1, int(round(self.line_mm * self.scale_at(mid))))
            for slot, ss in by_col.items():
                a = self.project([(s.x0, s.y0) for s in ss])
                bb = self.project([(s.x1, s.y1) for s in ss])
                bgr = _hex_bgr(cols[min(slot, len(cols) - 1)].hex)
                for p, q in zip(np.round(a * 16).astype(np.int32), np.round(bb * 16).astype(np.int32)):
                    cv2.line(self._ink, tuple(p), tuple(q), (*bgr, 255), thick, cv2.LINE_AA, shift=4)
        return self._ink

    # ------------------------------------------------------------------ frame
    def render(self, trace=(), pen_xy=None) -> np.ndarray:
        img = self._static_layer().copy()
        ink = self._ink_layer(list(trace))
        a = ink[:, :, 3:4].astype(np.float32) / 255.0
        if a.any():
            img = (img * (1 - a * 0.9) + ink[:, :, :3] * (a * 0.9)).astype(np.uint8)
        if pen_xy is not None:
            self._draw_gondola(img, (float(pen_xy[0]), float(pen_xy[1])))
        if self.person is not None:
            self._draw_person(img, *self.person)
        if self._dmap is not None:
            img = cv2.remap(img, self._dmap[0], self._dmap[1], cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        if self.blur > 0:
            img = cv2.GaussianBlur(img, (0, 0), self.blur)
        if self.noise > 0:
            n = self.rng.normal(0.0, self.noise, img.shape[:2]).astype(np.float32)[:, :, None]
            img = np.clip(img + n, 0, 255).astype(np.uint8)
        return img

    def _draw_person(self, img, x, y, z=450.0):
        col = (88, 76, 120)
        self._poly(img, [(x - 210, y - 900), (x + 210, y - 900), (x + 230, y + 250), (x - 230, y + 250)], col, z)
        head = self.project([(x, y + 420), (x + 110, y + 420)], z)
        cv2.circle(img, tuple(np.round(head[0]).astype(int)), int(np.linalg.norm(head[1] - head[0])), (70, 90, 150), -1, cv2.LINE_AA)
        arm = self.project([(x + 200, y + 200), (x + 520, y + 420)], z)
        cv2.line(img, tuple(np.round(arm[0]).astype(int)), tuple(np.round(arm[1]).astype(int)), col,
                 max(2, int(0.25 * self.scale_at((x, y)) * 110)), cv2.LINE_AA)

    def _draw_gondola(self, img, p):
        g = self.cfg.geometry
        s, ah = g.attach_half_spacing_mm, g.attach_height_mm
        for anchor, ax in ((g.anchor_left, -s), (g.anchor_right, s)):
            q = self.project([anchor, (p[0] + ax, p[1] + ah)], [ANCHOR_Z, BELT_Z])
            th = max(1, int(round(6.0 * self.scale_at(p))))
            cv2.line(img, tuple(np.round(q[0]).astype(int)), tuple(np.round(q[1]).astype(int)), (40, 40, 44), th, cv2.LINE_AA)
        x, y = p
        self._poly(img, [(x - GONDOLA_W / 2, y - GONDOLA_BELOW), (x + GONDOLA_W / 2, y - GONDOLA_BELOW),
                         (x + GONDOLA_W / 2, y + GONDOLA_ABOVE), (x - GONDOLA_W / 2, y + GONDOLA_ABOVE)], (58, 60, 66), GONDOLA_Z)
        gm = self.gondola_marker
        if gm:
            off = gm.get("offset_mm", (0.0, 90.0))
            self._marker(img, {"id": gm["id"], "size_mm": gm.get("size_mm", 50.0), "x": x, "y": y},
                         float(gm.get("height_mm", GONDOLA_Z)) + 0.5, (float(off[0]), float(off[1])))
