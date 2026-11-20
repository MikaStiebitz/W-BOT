"""Server-side overlay on a camera image (used by the MCP snapshot; the web UI draws its own).

Draws the board outline and usable area, the planned drawing of the current/last job (the
"skeleton"), the pen position the software believes (cross) and the one the camera tracks (circle).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import cv2
import numpy as np

if TYPE_CHECKING:
    from .service import CameraService


def _hex_bgr(hx: str) -> tuple[int, int, int]:
    hx = hx.lstrip("#")
    return (int(hx[4:6], 16), int(hx[2:4], 16), int(hx[0:2], 16))


def _mapper(svc: "CameraService", view: str):
    if view == "board":
        x0, y0, x1, y1 = svc.view_rect()
        s = float(svc.settings["view"]["px_per_mm"])
        return lambda pts: np.column_stack([(np.asarray(pts, float)[:, 0] - x0) * s,
                                            (y1 - np.asarray(pts, float)[:, 1]) * s])
    g = svc.geom
    return None if g is None else (lambda pts: g.to_image(np.asarray(pts, float)))


def _poly(img, f, pts, color, thick, closed=False, dense=40.0):
    p = np.asarray(pts, float)
    if closed:
        p = np.vstack([p, p[:1]])
    out = [p[0]]
    for a, b in zip(p[:-1], p[1:]):                   # densify: straight board lines bend under lens distortion
        n = max(1, int(np.linalg.norm(b - a) / dense))
        out += [a + (b - a) * k / n for k in range(1, n + 1)]
    q = f(np.asarray(out))
    q = q[np.all(np.isfinite(q), axis=1)]
    if len(q) >= 2:
        cv2.polylines(img, [np.round(q * 8).astype(np.int32)], False, color, thick, cv2.LINE_AA, shift=3)


def draw_overlay(svc: "CameraService", img: np.ndarray, view: str) -> np.ndarray:
    f = _mapper(svc, view)
    if f is None:
        return img
    out = img.copy()
    cfg = svc.m.cfg
    b = cfg.board
    lw = max(1, int(round(img.shape[1] / 900)))
    _poly(out, f, [(0, 0), (b.width_mm, 0), (b.width_mm, b.height_mm), (0, b.height_mm)], (0, 140, 255), lw, True)
    x0, y0, x1, y1 = cfg.safe_rect()
    _poly(out, f, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], (0, 200, 255), lw, True, dense=20)
    jobs = svc.m.jobs
    d = getattr(jobs, "_intended", None) if jobs else None
    if d is not None:
        for s in d.strokes[:4000]:
            if len(s.pts) >= 2:
                col = _hex_bgr(cfg.colors[min(int(s.color), len(cfg.colors) - 1)].hex)
                bright = tuple(int(c * 0.35 + 255 * 0.65) for c in col) if sum(col) < 150 else col
                _poly(out, f, s.pts, (255, 0, 255) if sum(col) < 150 else bright, lw, dense=15)
    soll = svc.m.phys_xy() if svc.m.mapping is not None else None
    if soll is not None:
        q = f(np.array([soll]))[0]
        if np.all(np.isfinite(q)):
            c, r = tuple(int(v) for v in np.round(q)), 14 * lw
            cv2.line(out, (c[0] - r, c[1]), (c[0] + r, c[1]), (0, 200, 0), 2 * lw, cv2.LINE_AA)
            cv2.line(out, (c[0], c[1] - r), (c[0], c[1] + r), (0, 200, 0), 2 * lw, cv2.LINE_AA)
    tr = svc.track
    if tr and tr.get("pen"):
        q = f(np.array([tr["pen"]]))[0]
        if np.all(np.isfinite(q)):
            cv2.circle(out, tuple(int(v) for v in np.round(q)), 11 * lw, (255, 80, 0), 2 * lw, cv2.LINE_AA)
    return out
