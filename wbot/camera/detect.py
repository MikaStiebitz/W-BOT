"""Image analysis: ArUco markers, the whiteboard outline, drawn measuring crosses, chessboards.

All functions take numpy images (BGR uint8 or grey) and return pixel coordinates of the image
they were given; mapping to board mm is the caller's job (``geometry.BoardCamera``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

ARUCO_DICTS = {"4X4_50": cv2.aruco.DICT_4X4_50, "5X5_100": cv2.aruco.DICT_5X5_100, "6X6_250": cv2.aruco.DICT_6X6_250}


def gray(img: np.ndarray) -> np.ndarray:
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


# ---------------------------------------------------------------------- ArUco
def _dictionary(name: str):
    if name not in ARUCO_DICTS:
        raise ValueError(f"unknown ArUco dictionary {name!r}")
    return cv2.aruco.getPredefinedDictionary(ARUCO_DICTS[name])


_DETECTORS: dict[str, object] = {}


def detect_markers(img: np.ndarray, dict_name: str = "4X4_50") -> dict[int, np.ndarray]:
    """{id: corners (4,2) px} in the marker's own order (top-left, top-right, bottom-right, bottom-left
    of the printed marker). Duplicate ids: the larger one wins."""
    det = _DETECTORS.get(dict_name)
    if det is None:
        p = cv2.aruco.DetectorParameters()
        p.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        p.adaptiveThreshWinSizeMax = 53
        det = cv2.aruco.ArucoDetector(_dictionary(dict_name), p)
        _DETECTORS[dict_name] = det
    corners, ids, _ = det.detectMarkers(gray(img))
    out: dict[int, np.ndarray] = {}
    if ids is None:
        return out
    for c, i in zip(corners, ids.ravel()):
        c = np.asarray(c, float).reshape(4, 2)
        i = int(i)
        if i not in out or cv2.contourArea(c.astype(np.float32)) > cv2.contourArea(out[i].astype(np.float32)):
            out[i] = c
    return out


def marker_image(marker_id: int, side_px: int, dict_name: str = "4X4_50", border_bits: int = 1) -> np.ndarray:
    return cv2.aruco.generateImageMarker(_dictionary(dict_name), int(marker_id), int(side_px), borderBits=border_bits)


# ---------------------------------------------------------------------- board outline
def order_quad(pts) -> np.ndarray:
    """Four points -> top-left, top-right, bottom-right, bottom-left (image coordinates, y down)."""
    p = np.asarray(pts, float).reshape(4, 2)
    s, d = p.sum(axis=1), p[:, 0] - p[:, 1]
    return np.array([p[np.argmin(s)], p[np.argmax(d)], p[np.argmax(s)], p[np.argmin(d)]])


def detect_board(img: np.ndarray, min_area_frac: float = 0.08) -> np.ndarray | None:
    """Largest bright quadrilateral (the whiteboard) -> 4 corners TL, TR, BR, BL in px, or None.
    A suggestion only: the user checks and drags the corners."""
    h0, w0 = img.shape[:2]
    k = 800.0 / max(w0, h0)
    small = cv2.resize(img, (int(w0 * k), int(h0 * k)), interpolation=cv2.INTER_AREA) if k < 1 else img.copy()
    k = min(k, 1.0)
    if small.ndim == 3:
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV)
        light = cv2.subtract(hsv[:, :, 2], cv2.multiply(hsv[:, :, 1], 0.6))     # bright and unsaturated
    else:
        light = small
    light = cv2.GaussianBlur(light, (5, 5), 0)
    _, th = cv2.threshold(light, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))       # ink, belts, gondola
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
    cnts, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best, best_area = None, min_area_frac * small.shape[0] * small.shape[1]
    for c in cnts:
        hull = cv2.convexHull(c)
        area = cv2.contourArea(hull)
        if area < best_area:
            continue
        peri = cv2.arcLength(hull, True)
        for eps in (0.01, 0.02, 0.03, 0.05, 0.08):
            ap = cv2.approxPolyDP(hull, eps * peri, True)
            if len(ap) == 4:
                if cv2.isContourConvex(ap):
                    best, best_area = ap.reshape(4, 2).astype(float), area
                break
    if best is None:
        return None
    return order_quad(best / k)


# ---------------------------------------------------------------------- ink and crosses
def ink_map(img: np.ndarray, bg_px: int = 31) -> np.ndarray:
    """Ink strength 0..1 (dark or coloured marker on a light board), robust to uneven lighting."""
    m = img.min(axis=2) if img.ndim == 3 else img
    m = m.astype(np.float32)
    k = max(3, bg_px | 1)
    bg = cv2.dilate(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    bg = cv2.blur(bg, (k, k))
    ink = (bg - m) / np.maximum(bg, 30.0)
    return np.clip(ink * 2.0, 0.0, 1.0)


def cross_template(arm_px: float, line_px: float) -> np.ndarray:
    n = int(math.ceil(arm_px * 1.25)) * 2 + 1
    t = np.zeros((n, n), np.float32)
    c, w = n // 2, max(1, int(round(line_px / 2)))
    a = int(round(arm_px))
    t[c - w:c + w + 1, c - a:c + a + 1] = 1.0
    t[c - a:c + a + 1, c - w:c + w + 1] = 1.0
    return cv2.GaussianBlur(t, (0, 0), max(0.6, line_px / 4))


@dataclass
class CrossHit:
    x: float              # px in the image given to find_cross
    y: float
    score: float          # template correlation 0..1
    coverage: float       # fraction of the expected arm length found with ink (both arms, min)
    angle_deg: float      # angle between the two fitted arms (90 = perfect)
    resid_px: float       # rms distance of the arm centre points to their fitted lines


def _arm_points(ink: np.ndarray, cx: float, cy: float, arm: float, gap: float, band: float, horizontal: bool):
    """Ink-weighted centre line of one arm: for each column (row), the centroid across the band."""
    img = ink if horizontal else ink.T
    ccx, ccy = (cx, cy) if horizontal else (cy, cx)
    h, w = img.shape
    y0, y1 = max(0, int(ccy - band)), min(h, int(ccy + band) + 1)
    pts, wts = [], []
    if y1 - y0 < 3:
        return np.zeros((0, 2)), np.zeros(0)
    ys = np.arange(y0, y1, dtype=float)
    for x in range(max(0, int(ccx - arm * 1.3)), min(w, int(ccx + arm * 1.3) + 1)):
        if abs(x - ccx) < gap:
            continue
        col = img[y0:y1, x]
        s = float(col.sum())
        if s < 0.8:                      # less than ~one pixel of full ink
            continue
        pts.append((float(x), float((col * ys).sum() / s)))
        wts.append(s)
    p, wt = np.asarray(pts, float).reshape(-1, 2), np.asarray(wts, float)
    if not horizontal and len(p):
        p = p[:, ::-1]
    return p, wt


def _fit_line(p: np.ndarray, wt: np.ndarray, horizontal: bool, iters: int = 3):
    """Weighted robust fit; horizontal: y = a + b x, vertical: x = a + b y. Returns (a, b, rms, n)."""
    u, v = (p[:, 0], p[:, 1]) if horizontal else (p[:, 1], p[:, 0])
    keep = np.ones(len(u), bool)
    a = b = 0.0
    rms = math.inf
    for _ in range(iters):
        if keep.sum() < 4:
            return None
        A = np.column_stack([np.ones(keep.sum()), u[keep]])
        sw = np.sqrt(wt[keep])
        sol, *_ = np.linalg.lstsq(A * sw[:, None], v[keep] * sw, rcond=None)
        a, b = float(sol[0]), float(sol[1])
        r = v - (a + b * u)
        rms = float(np.sqrt(np.average(r[keep] ** 2, weights=wt[keep])))
        keep = np.abs(r) <= max(2.5 * rms, 0.75)
    return a, b, rms, int(keep.sum())


def find_cross(img_or_ink: np.ndarray, guess: tuple[float, float], arm_px: float, line_px: float,
               search_px: float, *, is_ink: bool = False, min_score: float = 0.3,
               min_coverage: float = 0.45) -> CrossHit | None:
    """Centre of a drawn '+' near ``guess`` (px) with arm length ``arm_px`` (centre to tip)."""
    ink = img_or_ink.astype(np.float32) if is_ink else ink_map(img_or_ink, int(max(15, line_px * 6)))
    tmpl = cross_template(arm_px, line_px)
    th, tw = tmpl.shape
    gx, gy = guess
    r = int(math.ceil(search_px))
    x0, y0 = int(gx) - r - tw // 2, int(gy) - r - th // 2
    x1, y1 = int(gx) + r + tw // 2 + 1, int(gy) + r + th // 2 + 1
    H, W = ink.shape
    if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
        xa, ya, xb, yb = max(0, x0), max(0, y0), min(W, x1), min(H, y1)
        if xb - xa <= tw or yb - ya <= th:
            return None
        x0, y0, x1, y1 = xa, ya, xb, yb
    roi = ink[y0:y1, x0:x1]
    if float(roi.max(initial=0.0)) < 0.15:
        return None
    res = cv2.matchTemplate(roi, tmpl, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if not math.isfinite(score) or score < min_score:
        return None
    cx, cy = x0 + loc[0] + tw // 2, y0 + loc[1] + th // 2
    gap, band = max(2.0 * line_px, 2.0), max(2.5 * line_px, 0.22 * arm_px, 3.0)
    fit_h = fit_v = None
    for _ in range(3):                                   # alternate: arms -> intersection -> arms
        ph, wh = _arm_points(ink, cx, cy, arm_px, gap, band, True)
        pv, wv = _arm_points(ink, cx, cy, arm_px, gap, band, False)
        fit_h = _fit_line(ph, wh, True) if len(ph) >= 4 else None
        fit_v = _fit_line(pv, wv, False) if len(pv) >= 4 else None
        if fit_h is None or fit_v is None:
            return None
        a1, b1 = fit_h[0], fit_h[1]                      # y = a1 + b1 x
        a2, b2 = fit_v[0], fit_v[1]                      # x = a2 + b2 y
        den = 1.0 - b1 * b2
        if abs(den) < 1e-6:
            return None
        nx = (a2 + b2 * a1) / den
        ny = a1 + b1 * nx
        moved = math.hypot(nx - cx, ny - cy)
        cx, cy = nx, ny
        if moved < 0.05:
            break
    expected = 2.0 * (arm_px * 1.0 - gap)
    cov = min(fit_h[3], fit_v[3]) / max(expected, 1.0)
    angle = 90.0 - math.degrees(math.atan(fit_h[1]) + math.atan(fit_v[1]))     # between the two arms
    if cov < min_coverage or abs(angle - 90.0) > 15.0 or math.hypot(cx - gx, cy - gy) > search_px * 1.2:
        return None
    return CrossHit(float(cx), float(cy), float(score), float(min(cov, 1.0)), float(angle),
                    float(math.hypot(fit_h[2], fit_v[2])))


# ---------------------------------------------------------------------- chessboard
def find_chessboard(img: np.ndarray, pattern: tuple[int, int]) -> np.ndarray | None:
    """Inner corners (cols x rows) of a printed chessboard, sub-pixel, or None."""
    g = gray(img)
    ok, corners = cv2.findChessboardCornersSB(g, pattern, flags=cv2.CALIB_CB_NORMALIZE_IMAGE | cv2.CALIB_CB_EXHAUSTIVE)
    if not ok or corners is None:
        return None
    return corners.reshape(-1, 2).astype(float)
