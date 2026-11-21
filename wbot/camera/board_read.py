"""Read parts of the board with the camera: a sharp top-down crop of an area (e.g. an answer box the user wrote
into) and free space for new content. Used by the quiz tools of the MCP server.

Both work on the board plane (z = 0), so the crops are rectified: x right, y up, a fixed number of pixels per mm.
Things that move on purpose (gondola, belts) never count as ink; objects in front of the board (a person) can be
replaced by the remembered background, and an area that is still covered is reported as blocked.
"""
from __future__ import annotations

import time

import cv2
import numpy as np

from ..errors import MachineError
from .detect import ink_map

INK_LEVEL = 0.35            # ink_map value that counts as a pen line
MAX_SIDE_PX = 2400          # longest side of a crop
MIN_BLOB_MM2 = 6.0          # smaller ink specks are noise
MOVING_TOL_MM = 30.0        # extra room around gondola and belts (the real gondola may hang off)
FRAME_MM = 12.0             # strip along the board edge: the frame, never ink


def _clip_rect(rect, outer) -> tuple[float, float, float, float]:
    try:
        x0, y0, x1, y1 = (float(v) for v in rect)
    except (TypeError, ValueError):
        raise MachineError("rect must be [x0, y0, x1, y1] in mm.", "bad_request", 400) from None
    x0, x1 = sorted((x0, x1))
    y0, y1 = sorted((y0, y1))
    x0, y0 = max(x0, outer[0]), max(y0, outer[1])
    x1, y1 = min(x1, outer[2]), min(y1, outer[3])
    if x1 - x0 < 5 or y1 - y0 < 5:
        raise MachineError("The area is outside the board or smaller than 5 mm.", "bad_request", 400)
    return x0, y0, x1, y1


def _settle(svc, timeout: float = 8.0) -> None:
    """Wait until the gondola stands still (a job's last move or the park move may still be under way): the
    gondola and belts are masked at the reported position, which must match the picture."""
    t_end = time.time() + timeout
    last = svc.m.phys_xy()
    still_since = time.time()
    while time.time() < t_end:
        time.sleep(0.15)
        p = svc.m.phys_xy()
        jobs = svc.m.jobs
        moving = bool(jobs and jobs.running and (jobs.job or {}).get("state") == "running")
        if moving or p is None or last is None or abs(p[0] - last[0]) + abs(p[1] - last[1]) > 0.2:
            still_since, last = time.time(), p
            if p is None and not moving:
                return
        elif time.time() - still_since > 0.4:
            return


def _grab(svc, frames: int) -> np.ndarray:
    _settle(svc)
    _, _, seq = svc.frame()
    try:
        svc.frame(timeout=2.0, after_seq=seq)             # a picture taken after the gondola stopped
    except MachineError:
        pass
    return svc.averaged(frames) if frames > 1 else svc.frame()[0]


def _poly_mask(shape, polys) -> np.ndarray:
    mask = np.zeros(shape[:2], np.uint8)
    for p in polys:
        cv2.fillPoly(mask, [np.round(np.asarray(p)).astype(np.int32)], 255)
    return mask


def _moving_polys(svc, extra_mm: float = MOVING_TOL_MM) -> list[np.ndarray]:
    """Image polygons of the gondola and its belts, generous: the real gondola may hang a few cm off."""
    g = svc.geom
    p = svc.m.phys_xy() if (g is not None and svc.m.mapping is not None) else None
    if p is None:
        return []
    x, y = p
    geo, e_ = svc.m.cfg.geometry, extra_mm
    out = [g.to_image([(x - 70 - e_, y - 70 - e_), (x + 70 + e_, y - 70 - e_), (x + 70 + e_, y + 240 + e_),
                       (x - 70 - e_, y + 240 + e_)], 36.0)]
    for a, sgn in ((geo.anchor_left, -1), (geo.anchor_right, 1)):
        end = np.array([x + sgn * geo.attach_half_spacing_mm, y + geo.attach_height_mm])
        d = end - np.asarray(a)
        n = np.array([-d[1], d[0]]) / max(np.linalg.norm(d), 1e-6) * (25.0 + e_)
        out.append(g.to_image([np.asarray(a) + n, end + n, end - n, np.asarray(a) - n], 30.0))
    return [o for o in out if np.all(np.isfinite(o))]


def _blocked(svc, g, rect) -> bool:
    """Is an object in front of the board over this area right now?"""
    st = svc.presence.state or {}
    if not st.get("present") or not st.get("bbox"):
        return False
    q = g.to_image([(rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3])])
    if not np.all(np.isfinite(q)):
        return True
    bx0, by0, bx1, by1 = st["bbox"]
    return not (q[:, 0].max() < bx0 or q[:, 0].min() > bx1 or q[:, 1].max() < by0 or q[:, 1].min() > by1)


def _ink(view: np.ndarray, valid: np.ndarray, ppm: float) -> np.ndarray:
    ink = ink_map(view, bg_px=int(max(15, 10 * ppm))) >= INK_LEVEL
    ink &= valid
    n, lab, stats, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    small = np.where(stats[:, cv2.CC_STAT_AREA] < MIN_BLOB_MM2 * ppm * ppm)[0]
    if len(small) > 1:
        ink[np.isin(lab, small[small > 0])] = False
    return ink


def _drop_frame(ink: np.ndarray, band_px: float) -> np.ndarray:
    """Remove the drawn outline of a box: long ink lying mostly in a band along the crop's edges (the plotter and
    the camera are a few mm off, so the outline is not exactly on the edge)."""
    h, w = ink.shape
    b = max(1, int(round(band_px)))
    edge = np.zeros_like(ink)
    edge[:b], edge[-b:], edge[:, :b], edge[:, -b:] = True, True, True, True
    n, lab, stats, _ = cv2.connectedComponentsWithStats(ink.astype(np.uint8), connectivity=8)
    if n <= 1:
        return ink
    in_edge = np.bincount(lab[edge], minlength=n)
    for i in range(1, n):
        long = stats[i, cv2.CC_STAT_WIDTH] >= 0.3 * w or stats[i, cv2.CC_STAT_HEIGHT] >= 0.3 * h
        if long and in_edge[i] >= 0.8 * stats[i, cv2.CC_STAT_AREA]:
            ink = ink & (lab != i)
    return ink


def read_region(svc, rect, px_per_mm: float = 4.0, hide: bool = False, frames: int = 3,
                inset_mm: float = 0.0) -> dict:
    """Sharp top-down crop of ``rect`` (board mm) plus how much ink is in it.

    ``inset_mm`` leaves a border out of the ink measurement (the outline of an answer box)."""
    g = svc.geometry()
    rect = _clip_rect(rect, svc.view_rect())
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    ppm = float(min(max(px_per_mm, 1.0), 8.0, MAX_SIDE_PX / max(w, h)))
    img = _grab(svc, max(1, min(int(frames), 8)))
    blocked = _blocked(svc, g, rect)
    if hide and svc.presence.present and svc.presence.state.get("bbox"):
        img = svc.presence.filtered(img)
    view = g.rectify(img, rect, ppm)
    valid = g.rectify(np.full(img.shape[:2], 255, np.uint8), rect, ppm) > 127
    moving = g.rectify(_poly_mask(img.shape, _moving_polys(svc)), rect, ppm) > 127
    ink = _ink(view, valid & ~moving, ppm)
    inner = np.ones_like(ink)
    if inset_mm > 0:
        ink = _drop_frame(ink, inset_mm * ppm)
        k = int(round(2 * ppm))
        inner[:] = False
        inner[k:ink.shape[0] - k, k:ink.shape[1] - k] = True
        ink &= inner
    area = max(1, int((valid & inner).sum()))
    out = {"rect": [round(v, 1) for v in rect], "px_per_mm": round(ppm, 3), "size": [view.shape[1], view.shape[0]],
           "visible_pct": round(100.0 * valid.mean(), 1), "ink_pct": round(100.0 * ink.sum() / area, 2),
           "ink_bbox": None, "blocked": blocked, "hidden_objects": bool(hide and svc.presence.present)}
    ys, xs = np.nonzero(ink)
    if len(xs):
        out["ink_bbox"] = [round(rect[0] + xs.min() / ppm, 1), round(rect[3] - (ys.max() + 1) / ppm, 1),
                           round(rect[0] + (xs.max() + 1) / ppm, 1), round(rect[3] - ys.min() / ppm, 1)]
    ok, buf = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 90])
    if not ok:
        raise MachineError("Picture could not be encoded.", "internal", 500)
    out["jpeg"] = buf.tobytes()
    return out


def _board_ink(svc) -> tuple[np.ndarray, np.ndarray, tuple[float, float, float, float]]:
    """(ink, visible) masks of the whole board at 1 px per mm (row 0 = top edge), cached per camera frame."""
    g = svc.geometry()
    b = svc.m.cfg.board
    rect = (0.0, 0.0, float(b.width_mm), float(b.height_mm))
    _, _, seq = svc.frame()
    hit = getattr(svc, "_board_ink_cache", None)
    if hit and hit[0] == seq and hit[1] is g and hit[2] == rect:
        return hit[3], hit[4], rect
    img = _grab(svc, 3)
    view = g.rectify(img, rect, 1.0)
    # objects in front of the board (a person, but also a "ghost" where the gondola stood) are unknown, not ink
    seen = np.full(img.shape[:2], 255, np.uint8)
    if svc.presence.present:
        seen[_poly_mask(img.shape, svc.presence.state.get("outlines") or []) > 0] = 0
    valid = g.rectify(seen, rect, 1.0) > 127
    moving = g.rectify(_poly_mask(img.shape, _moving_polys(svc)), rect, 1.0) > 127
    ink = _ink(view, valid & ~moving, 1.0)
    f = int(FRAME_MM)
    ink[:f], ink[-f:], ink[:, :f], ink[:, -f:] = False, False, False, False
    svc._board_ink_cache = (seq, g, rect, ink, valid)
    return ink, valid, rect


def _crop(mask: np.ndarray, board_h: float, r) -> np.ndarray:
    """Part of a board mask (1 px/mm, row 0 = top) inside rect r = (x0, y0, x1, y1) mm."""
    return mask[int(round(board_h - r[3])):int(round(board_h - r[1])), int(round(r[0])):int(round(r[2]))]


def _dilate(mask: np.ndarray, mm: float) -> np.ndarray:
    m = int(round(max(0.0, mm)))
    if not m:
        return mask
    return cv2.dilate(mask.astype(np.uint8), cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * m + 1, 2 * m + 1))) > 0


def free_space(svc, width_mm: float, height_mm: float, margin_mm: float = 15.0, count: int = 3,
               near: tuple[float, float] | None = None) -> dict:
    """Empty places of ``width_mm`` x ``height_mm`` inside the usable area: emptiest first, then closest to
    ``near`` (default: the middle of the usable area). Unseen parts count as occupied; gondola and belts do not."""
    area = svc.m.cfg.safe_rect()
    W, H = float(width_mm), float(height_mm)
    if W < 10 or H < 10:
        raise MachineError("width_mm and height_mm must be at least 10 mm.", "bad_request", 400)
    if W > area[2] - area[0] or H > area[3] - area[1]:
        raise MachineError(f"{W:.0f} x {H:.0f} mm does not fit into the usable area "
                           f"({area[2] - area[0]:.0f} x {area[3] - area[1]:.0f} mm).", "bad_request", 400)
    ink, valid, brect = _board_ink(svc)
    occ = _crop(_dilate(ink, margin_mm) | ~valid, brect[3], area)
    ii = cv2.integral(occ.astype(np.uint8))
    rh, rw = occ.shape
    wi, hi = int(round(W)), int(round(H))
    cx, cy = near if near else ((area[0] + area[2]) / 2, (area[1] + area[3]) / 2)
    cand = []
    for r in range(0, rh - hi + 1, 10):
        for c in range(0, rw - wi + 1, 10):
            s = int(ii[r + hi, c + wi] - ii[r, c + wi] - ii[r + hi, c] + ii[r, c])
            x0, y1 = area[0] + c, area[3] - r
            cand.append((s / (wi * hi), float(np.hypot(x0 + W / 2 - cx, y1 - H / 2 - cy)), (x0, y1 - H, x0 + W, y1)))
    cand.sort(key=lambda t: (round(t[0], 3), t[1]))
    picked: list[tuple] = []
    for frac, _, r in cand:
        if any(not (r[2] <= p[0] or r[0] >= p[2] or r[3] <= p[1] or r[1] >= p[3]) for _, p in picked):
            continue
        picked.append((frac, r))
        if len(picked) >= max(1, count):
            break
    return {"area": [round(v, 1) for v in area],
            "places": [{"rect": [round(v, 1) for v in r], "occupied_pct": round(100 * f, 1)} for f, r in picked]}


def occupancy(svc, cell_mm: float = 10.0, group_mm: float = 15.0) -> dict:
    """What is already written on the board, for an overlay: a coarse cell grid and grouped boxes (mm).

    ``cells``: one string per row from the TOP edge down, one char per cell from the left: 0 free, 1 ink,
    2 not seen by the camera. ``boxes``: bounding boxes of ink grouped within ``group_mm``, largest first."""
    ink, valid, (bx0, by0, bx1, by1) = _board_ink(svc)
    c = int(min(max(cell_mm, 5.0), 50.0))
    rows, cols = int(np.ceil(ink.shape[0] / c)), int(np.ceil(ink.shape[1] / c))

    def pooled(a: np.ndarray) -> np.ndarray:
        a = np.pad(a.astype(np.int32), ((0, rows * c - a.shape[0]), (0, cols * c - a.shape[1])))
        return a.reshape(rows, c, cols, c).sum(axis=(1, 3))

    ink_n, vis_n = pooled(ink), pooled(valid)
    code = np.where(ink_n >= 3, 1, np.where(vis_n < 0.5 * c * c, 2, 0))
    half = group_mm / 2
    n, _, stats, _ = cv2.connectedComponentsWithStats(_dilate(ink, half).astype(np.uint8), connectivity=8)
    boxes = []
    for i in range(1, n):
        x, y, w, h = (int(v) for v in stats[i, :4])
        if w * h >= 100:
            boxes.append([max(bx0, x + half), max(by0, by1 - (y + h) + half),
                          min(bx1, x + w - half), min(by1, by1 - y - half)])
    boxes.sort(key=lambda r: -(r[2] - r[0]) * (r[3] - r[1]))
    return {"rect": [bx0, by0, bx1, by1], "cell_mm": c, "cols": cols, "rows": rows,
            "cells": ["".join(map(str, row)) for row in code.tolist()],
            "boxes": [[round(v, 1) for v in b] for b in boxes[:60]],
            "ink_pct": round(100.0 * ink.sum() / max(1, valid.sum()), 2)}


def overlap(svc, strokes: list, margin_mm: float = 5.0) -> dict:
    """Does a planned drawing run over existing ink? ``strokes``: point lists [[x, y], ...] mm or {points: ...}.

    Returns the share of the drawing's path that touches ink, the ink boxes it hits and, if it overlaps, an empty
    place of the drawing's size near where it was meant to go (with the shift dx, dy)."""
    ink, valid, (_, _, _, bh) = _board_ink(svc)
    path = np.zeros(ink.shape, np.uint8)
    pts_all = []
    for s in strokes or []:
        pts = s.get("points") if isinstance(s, dict) else s
        q = np.asarray(pts or [], float).reshape(-1, 2)
        if not len(q):
            continue
        pts_all.append(q)
        px = np.column_stack([q[:, 0], bh - q[:, 1]]).round().astype(np.int32)
        cv2.polylines(path, [px], False, 255, 1 + 2 * int(round(max(0.0, margin_mm))))
    if not pts_all:
        raise MachineError("No strokes given.", "bad_request", 400)
    allp = np.vstack(pts_all)
    b = [float(allp[:, 0].min()), float(allp[:, 1].min()), float(allp[:, 0].max()), float(allp[:, 1].max())]
    on = path > 0
    hit = on & ink
    total = max(1, int(on.sum()))
    out: dict = {"overlap_pct": round(100.0 * hit.sum() / total, 2), "bounds": [round(v, 1) for v in b],
                 "unseen_pct": round(100.0 * (on & ~valid).sum() / total, 1), "boxes": [], "suggestion": None}
    if out["overlap_pct"] < 0.05:
        return out
    m = margin_mm
    out["boxes"] = [r for r in occupancy(svc)["boxes"]
                    if not (r[2] < b[0] - m or r[0] > b[2] + m or r[3] < b[1] - m or r[1] > b[3] + m)]
    try:
        fs = free_space(svc, max(10.0, b[2] - b[0]), max(10.0, b[3] - b[1]), margin_mm=10.0, count=1,
                        near=((b[0] + b[2]) / 2, (b[1] + b[3]) / 2))
        p = fs["places"][0] if fs["places"] else None
        if p and p["occupied_pct"] < 0.5:
            out["suggestion"] = {"rect": p["rect"], "dx": round(p["rect"][0] - b[0], 1),
                                 "dy": round(p["rect"][1] - b[1], 1)}
    except MachineError:
        pass
    return out
