"""Find the whiteboard in a camera image by its frame: four long straight edges forming a convex quad.

A whiteboard on a white wall has almost no brightness contrast to its surroundings, but its aluminium
frame gives long, straight (if faint) edges. Line segments are found with LSD (sensitive to weak
edges), merged into lines, and every combination of two roughly horizontal and two roughly vertical
lines is scored by how much of each quad side is really covered by detected segments; larger quads
win ties, so the outer frame edge is preferred over the inner one. Belts, cables, writing and
furniture rarely produce four long, mutually closing edges.

Result: TL, TR, BR, BL corners in full-resolution pixels (a suggestion; the UI shows draggable corners).
"""
from __future__ import annotations

import itertools
import math

import cv2
import numpy as np

MIN_SCORE = 0.2           # mean coverage x sqrt(area fraction) x end penalty: below this it is not a board
MIN_COVER = 0.3           # every side must be covered by detected segments to at least this fraction


def _segments(gray: np.ndarray) -> np.ndarray:
    try:
        lsd = cv2.createLineSegmentDetector(cv2.LSD_REFINE_STD)
        segs = lsd.detect(gray)[0]
    except (cv2.error, AttributeError):
        segs = None
    if segs is None:                                       # fallback: Hough on Canny edges
        e = cv2.Canny(cv2.GaussianBlur(gray, (5, 5), 0), 20, 60)
        segs = cv2.HoughLinesP(e, 1, np.pi / 360, 30, minLineLength=15, maxLineGap=4)
    return np.zeros((0, 4)) if segs is None else segs.reshape(-1, 4).astype(float)


def _cluster(segs: np.ndarray, lens: np.ndarray, d_ang: float = 3.0, d_rho: float = 4.0, keep: int = 12):
    """Merge collinear segments into lines (point, unit direction, total length)."""
    order = np.argsort(-lens)
    clusters: list[dict] = []
    for i in order:
        x1, y1, x2, y2 = segs[i]
        th = math.atan2(y2 - y1, x2 - x1) % math.pi
        nrm = (-math.sin(th), math.cos(th))
        rho = x1 * nrm[0] + y1 * nrm[1]
        for c in clusters:
            dth = abs(th - c["th"])
            dth = min(dth, math.pi - dth)
            r = x1 * c["n"][0] + y1 * c["n"][1]
            r2 = x2 * c["n"][0] + y2 * c["n"][1]
            if math.degrees(dth) < d_ang and abs(r - c["rho"]) < d_rho and abs(r2 - c["rho"]) < d_rho:
                c["idx"].append(i)
                c["len"] += lens[i]
                break
        else:
            clusters.append({"th": th, "n": nrm, "rho": rho, "idx": [i], "len": float(lens[i])})
    clusters.sort(key=lambda c: -c["len"])
    lines = []
    for c in clusters[:keep]:
        pts = segs[c["idx"]].reshape(-1, 2)
        w = np.repeat(lens[c["idx"]], 2)
        ctr = (pts * w[:, None]).sum(0) / w.sum()
        cov = ((pts - ctr) * w[:, None]).T @ (pts - ctr)
        u = np.linalg.eigh(cov)[1][:, -1]
        lines.append((ctr, u / np.linalg.norm(u)))
    return lines


def _intersect(a, b):
    (p, r), (q, s) = a, b
    den = r[0] * s[1] - r[1] * s[0]
    if abs(den) < 1e-9:
        return None
    t = ((q - p)[0] * s[1] - (q - p)[1] * s[0]) / den
    return p + t * r


def _coverage(p: np.ndarray, q: np.ndarray, segs: np.ndarray, tol: float = 5.0) -> float:
    d = q - p
    ln = float(np.linalg.norm(d))
    if ln < 1:
        return 0.0
    u = d / ln
    nrm = np.array([-u[1], u[0]])
    a, b = segs[:, :2] - p, segs[:, 2:] - p
    on = (np.abs(a @ nrm) < tol) & (np.abs(b @ nrm) < tol)
    if not on.any():
        return 0.0
    ta, tb = a[on] @ u, b[on] @ u
    lo, hi = np.clip(np.minimum(ta, tb), 0, ln), np.clip(np.maximum(ta, tb), 0, ln)
    iv = sorted(zip(lo, hi))
    total, cur_lo, cur_hi = 0.0, None, None
    for s0, s1 in iv:                                      # union of the covered intervals
        if cur_hi is None or s0 > cur_hi:
            if cur_hi is not None:
                total += cur_hi - cur_lo
            cur_lo, cur_hi = s0, s1
        else:
            cur_hi = max(cur_hi, s1)
    if cur_hi is not None:
        total += cur_hi - cur_lo
    return total / ln


def find_board(img: np.ndarray, work_w: int = 960) -> tuple[np.ndarray, float] | None:
    """(corners TL, TR, BR, BL in px, score 0..1) or None."""
    h0, w0 = img.shape[:2]
    k = min(1.0, work_w / w0)
    small = cv2.resize(img, (int(w0 * k), int(h0 * k)), interpolation=cv2.INTER_AREA) if k < 1 else img
    g = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
    H, W = g.shape
    segs = _segments(g)
    lens = np.hypot(segs[:, 2] - segs[:, 0], segs[:, 3] - segs[:, 1])
    keep = lens > 0.015 * W
    segs, lens = segs[keep], lens[keep]
    if len(segs) < 4:
        return None
    ang = np.degrees(np.arctan2(segs[:, 3] - segs[:, 1], segs[:, 2] - segs[:, 0])) % 180
    hor_m, ver_m = (ang < 20) | (ang > 160), (ang > 70) & (ang < 110)
    hs, vs = _cluster(segs[hor_m], lens[hor_m]), _cluster(segs[ver_m], lens[ver_m])
    hseg, vseg = segs[hor_m], segs[ver_m]
    hs.sort(key=lambda l: l[0][1])
    vs.sort(key=lambda l: l[0][0])
    best, bscore = None, 0.0
    for t, b in itertools.combinations(hs, 2):
        if b[0][1] - t[0][1] < 0.2 * H:
            continue
        for l, r in itertools.combinations(vs, 2):
            if r[0][0] - l[0][0] < 0.2 * W:
                continue
            c = [_intersect(t, l), _intersect(t, r), _intersect(b, r), _intersect(b, l)]
            if any(v is None for v in c):
                continue
            q = np.array(c)
            m = 0.01 * W
            if np.any(q[:, 0] < m) or np.any(q[:, 0] > W - m) or np.any(q[:, 1] < m) or np.any(q[:, 1] > H - m):
                continue
            area = cv2.contourArea(q.astype(np.float32)) / (W * H)
            if area < 0.06 or area > 0.9 or not cv2.isContourConvex(q.astype(np.float32).reshape(-1, 1, 2)):
                continue
            cs = (_coverage(q[0], q[1], hseg), _coverage(q[3], q[2], hseg),
                  _coverage(q[0], q[3], vseg), _coverage(q[1], q[2], vseg))
            if min(cs) < MIN_COVER:
                continue
            # frame edges end at the corners; belts, door frames and table edges run on past them
            ext = []
            for (p0, p1, sg) in ((q[0], q[1], hseg), (q[3], q[2], hseg), (q[0], q[3], vseg), (q[1], q[2], vseg)):
                d = 0.15 * (p1 - p0)
                ext.append(max(_coverage(p1 + 0.02 * (p1 - p0), p1 + d, sg), _coverage(p0 - 0.02 * (p1 - p0), p0 - d, sg)))
            score = (sum(cs) / 4) * math.sqrt(area) * (1.0 - 0.7 * max(ext))
            if score > bscore:
                best, bscore = q, score
    if best is None or bscore < MIN_SCORE:
        return None
    return snap_to_caps(img, best / k), float(bscore)


def snap_to_caps(img: np.ndarray, corners: np.ndarray, radius_frac: float = 0.018) -> np.ndarray:
    """Most whiteboards have dark plastic corner caps. Where one sits near a found corner, use its centre
    (the same point a person clicks); otherwise keep the line intersection."""
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    H, W = g.shape
    r = max(8, int(radius_frac * W))
    out = corners.astype(float).copy()
    for i, (cx, cy) in enumerate(corners):
        x0, y0, x1, y1 = int(cx) - r, int(cy) - r, int(cx) + r, int(cy) + r
        if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
            continue
        win = g[y0:y1, x0:x1]
        thr = 0.45 * float(np.median(win))
        n, lab, st, cen = cv2.connectedComponentsWithStats((win < thr).astype(np.uint8), 8)
        best, bd = None, math.inf
        for j in range(1, n):
            a, w, h = st[j][4], st[j][2], st[j][3]
            # a cap is a solid dark block (an ArUco marker has white cells: lower fill)
            if not ((0.003 * W) ** 2 <= a <= (0.02 * W) ** 2) or max(w, h) > 2.5 * min(w, h) or a < 0.55 * w * h:
                continue
            d = math.hypot(cen[j][0] - r, cen[j][1] - r)
            if d < bd:
                best, bd = cen[j], d
        if best is not None:
            out[i] = (x0 + best[0], y0 + best[1])
    return out
