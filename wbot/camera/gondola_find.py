"""Find the gondola in the camera picture from its two belts - no marker needed.

The belts are dark straight lines from the pulleys down to the gondola. In a top-down view of the
plane the belts run in (about 30 mm in front of the board), the direction of each belt is found by
scanning rays from its pulley: the ray along the belt is the darkest over its first ~200 mm. The
gondola hangs where both belts end: the left belt reaches the left attach point (pen + (-s, h)), the
right one the right attach point (pen + (s, h)). Both conditions are linear in the pen position, so
two belt directions give the pen directly. Tilt of the gondola is ignored (a few mm).
"""
from __future__ import annotations

import math

import cv2
import numpy as np

from ..config import Config
from .geometry import BoardCamera

BELT_Z = 30.0
ANCHOR_Z = 41.0
PX_PER_MM = 1.0


def _darkness(view: np.ndarray) -> np.ndarray:
    m = view.min(axis=2).astype(np.float32) if view.ndim == 3 else view.astype(np.float32)
    bg = cv2.blur(cv2.dilate(m, np.ones((25, 25), np.uint8)), (9, 9))
    return np.clip(bg - m, 0, 255)


def _ray_score(dark: np.ndarray, p0: np.ndarray, ang: float, r0: float, r1: float) -> float:
    d = np.array([math.sin(ang), -math.cos(ang)])          # image coords: y grows downwards; ang 0 = straight down
    d[1] = -d[1]
    rs = np.arange(r0, r1, 2.0)
    pts = p0[None] + rs[:, None] * d[None]
    xi, yi = np.round(pts[:, 0]).astype(int), np.round(pts[:, 1]).astype(int)
    h, w = dark.shape
    ok = (xi >= 0) & (xi < w) & (yi >= 0) & (yi < h)
    if ok.sum() < 0.8 * len(rs):
        return 0.0
    return float(np.median(dark[yi[ok], xi[ok]]))


def _belt_angle(dark: np.ndarray, p0: np.ndarray, side: int, r0: float = 70.0, r1: float = 260.0):
    """Best belt angle (radians from straight down, + = towards the board centre) and its contrast."""
    angs = np.radians(np.arange(-15.0, 85.0, 0.4)) * side
    sc = np.array([_ray_score(dark, p0, a, r0, r1) for a in angs])
    i = int(np.argmax(sc))
    best = angs[i]
    if 0 < i < len(sc) - 1:                                   # parabolic refinement
        a, b, c = sc[i - 1], sc[i], sc[i + 1]
        den = a - 2 * b + c
        if den < 0:
            best += 0.5 * (a - c) / den * (angs[1] - angs[0])
    contrast = float(sc[i] - np.median(sc))
    return float(best), contrast


def locate(cfg: Config, geom: BoardCamera, img: np.ndarray) -> dict:
    """{pen: [x, y], belts: [[ax, ay, ex, ey], ...], contrast, ok} in board mm."""
    g, b = cfg.geometry, cfg.board
    top = max(g.anchor_left[1], g.anchor_right[1]) + 60
    rect = (-150.0, -80.0, b.width_mm + 150.0, top)
    view = geom.rectify(img, rect, PX_PER_MM, border=(255, 255, 255), z=BELT_Z)
    dark = _darkness(view)
    to_px = lambda x, y: np.array([(x - rect[0]) * PX_PER_MM, (rect[3] - y) * PX_PER_MM])   # noqa: E731
    # the pulleys sit ~41 mm in front of the board, the belts' lower ends ~30 mm: express the pulley
    # centres in the belt plane so both ends of each belt are seen consistently
    anchors = [geom.to_board(geom.to_image([a], ANCHOR_Z), BELT_Z)[0] for a in (g.anchor_left, g.anchor_right)]
    out_dirs, contrasts = [], []
    for anchor, side in ((anchors[0], 1), (anchors[1], -1)):
        ang, con = _belt_angle(dark, to_px(*anchor), side)
        out_dirs.append(np.array([math.sin(ang), -math.cos(ang)]))     # board coords: y up, belt goes down
        contrasts.append(con)
    s, h = g.attach_half_spacing_mm, g.attach_height_mm
    # pen p: (p + (-s, h) - A_L) x d_L = 0 and (p + (s, h) - A_R) x d_R = 0   (2-D cross products)
    rows, rhs = [], []
    for anchor, d, off in ((anchors[0], out_dirs[0], (-s, h)), (anchors[1], out_dirs[1], (s, h))):
        a = np.asarray(anchor, float) - np.asarray(off, float)
        rows.append([d[1], -d[0]])
        rhs.append(a[0] * d[1] - a[1] * d[0])
    A = np.array(rows)
    res: dict = {"contrast": [round(c, 1) for c in contrasts], "ok": False}
    if abs(np.linalg.det(A)) < 1e-3:
        res["error"] = "The belts run parallel: no gondola hanging on them?"
        return res
    pen = np.linalg.solve(A, np.array(rhs))
    res["pen"] = [round(float(pen[0]), 1), round(float(pen[1]), 1)]
    res["belts"] = [[*map(float, anchors[0]), float(pen[0] - s), float(pen[1] + h)],
                    [*map(float, anchors[1]), float(pen[0] + s), float(pen[1] + h)]]
    inside = -50 <= pen[0] <= b.width_mm + 50 and -50 <= pen[1] <= b.height_mm
    strong = min(contrasts) >= 12.0
    res["ok"] = bool(inside and strong)
    if not inside:
        res["error"] = "The belts meet outside the board: the gondola was not found."
    elif not strong:
        res["error"] = "The belts are hard to see in the picture (too little contrast)."
    return res
