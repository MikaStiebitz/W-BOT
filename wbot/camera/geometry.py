"""Board <-> image mapping for one camera.

Board frame: millimetres, origin lower-left, x right, y up, z out of the board towards the camera
(right-handed). The plane z = 0 maps to *undistorted* pixels by a homography ``H`` (8 DOF, fitted
from >= 4 correspondences, exact for a pinhole camera). An optional lens model ``K, dist``
(OpenCV radial-tangential, from a chessboard calibration) maps undistorted <-> raw pixels.

For points above the board (a marker on the gondola, ~30-60 mm in front of the board) the camera
centre ``C`` is needed: a point at height z seen at pixel u lies on the ray from ``C`` through the
board point H^-1(u). ``C`` comes from decomposing ``H`` with ``K`` (known, or a focal length
estimated from ``H`` itself with the principal point at the image centre). An error of 10 % in ``C``
moves a 40 mm high point by roughly 10 % of its parallax, i.e. a few tenths of a mm to ~2 mm.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import cv2
import numpy as np

DEFAULT_FOCAL_FRAC = 0.75          # f / image width of a typical 70-78 deg webcam


class CameraGeometryError(ValueError):
    """Bad correspondences (message is shown to the user)."""


def _h(points) -> np.ndarray:
    a = np.asarray(points, float).reshape(-1, 2)
    return np.column_stack([a, np.ones(len(a))])


def apply_h(H: np.ndarray, pts) -> np.ndarray:
    """Apply a 3x3 homography to (N,2) points. Points at/behind the horizon become NaN."""
    q = _h(pts) @ np.asarray(H, float).T
    w = q[:, 2:3]
    with np.errstate(divide="ignore", invalid="ignore"):
        out = q[:, :2] / w
    out[(np.abs(w[:, 0]) < 1e-12)] = np.nan
    return out


def estimate_focal(H: np.ndarray, image_size: tuple[int, int]) -> float | None:
    """Focal length (px) from one plane homography, principal point at the image centre, square pixels
    (Zhang's two constraints). None when the view is too frontal to tell."""
    w, hgt = image_size
    T = np.array([[1, 0, -w / 2], [0, 1, -hgt / 2], [0, 0, 1]], float)
    M = T @ np.asarray(H, float)
    (a1, a2), (b1, b2), (c1, c2) = M[0, :2], M[1, :2], M[2, :2]
    cands = []
    den1 = c1 * c2
    if abs(den1) > 1e-12:
        cands.append((-(a1 * a2 + b1 * b2) / den1, abs(den1)))
    den2 = c1 * c1 - c2 * c2
    if abs(den2) > 1e-12:
        cands.append((-(a1 * a1 + b1 * b1 - a2 * a2 - b2 * b2) / den2, abs(den2)))
    good = [(f2, wt) for f2, wt in cands if f2 > 0]
    if not good:
        return None
    f = math.sqrt(sum(f2 * wt for f2, wt in good) / sum(wt for _, wt in good))
    return f if 0.3 * w <= f <= 4.0 * w else None


def default_K(image_size: tuple[int, int], f: float | None = None) -> np.ndarray:
    w, hgt = image_size
    f = f or DEFAULT_FOCAL_FRAC * w
    return np.array([[f, 0, w / 2], [0, f, hgt / 2], [0, 0, 1]], float)


def pose_from_h(H: np.ndarray, K: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(R, t) with x_cam = R X_board + t, from a board->undistorted-pixel homography."""
    M = np.linalg.inv(K) @ np.asarray(H, float)
    lam = 2.0 / (np.linalg.norm(M[:, 0]) + np.linalg.norm(M[:, 1]))
    if M[2, 2] * lam < 0:                     # board must be in front of the camera
        lam = -lam
    r1, r2, t = M[:, 0] * lam, M[:, 1] * lam, M[:, 2] * lam
    R = np.column_stack([r1, r2, np.cross(r1, r2)])
    u, _, vt = np.linalg.svd(R)
    R = u @ vt
    if np.linalg.det(R) < 0:
        R = u @ np.diag([1, 1, -1]) @ vt
    return R, t


@dataclass
class BoardCamera:
    """Mapping between board mm and raw image pixels (see module docstring)."""
    H: np.ndarray                                   # board z=0 -> undistorted px
    image_size: tuple[int, int]                     # (w, h) px
    K: np.ndarray | None = None                     # lens model (None = ideal pinhole, no distortion)
    dist: np.ndarray | None = None
    rms_px: float = 0.0                             # fit residual of the correspondences
    rms_mm: float = 0.0
    focal_px: float = 0.0
    focal_estimated: bool = True
    center: np.ndarray = field(default_factory=lambda: np.zeros(3))    # camera centre in board mm
    _maps: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.H = np.asarray(self.H, float) / float(self.H[2, 2])
        self.Hi = np.linalg.inv(self.H)
        if self.K is not None:
            K = np.asarray(self.K, float)
            self.focal_px, self.focal_estimated = float(K[0, 0]), False
        else:
            f = estimate_focal(self.H, self.image_size)
            self.focal_px, self.focal_estimated = (f or DEFAULT_FOCAL_FRAC * self.image_size[0]), True
            K = default_K(self.image_size, self.focal_px)
        R, t = pose_from_h(self.H, K)
        self.center = -R.T @ t
        if self.center[2] < 0:                      # mirrored image (e.g. a flipped virtual camera)
            self.center = self.center * np.array([1, 1, -1])

    # ------------------------------------------------------------------ construction
    @classmethod
    def fit(cls, image_pts, board_pts, image_size, K=None, dist=None) -> "BoardCamera":
        img = np.asarray(image_pts, float).reshape(-1, 2)
        brd = np.asarray(board_pts, float).reshape(-1, 2)
        if len(img) != len(brd) or len(img) < 4:
            raise CameraGeometryError("At least 4 point pairs (image and board) are needed.")
        if not (np.all(np.isfinite(img)) and np.all(np.isfinite(brd))):
            raise CameraGeometryError("Invalid coordinates.")
        und = undistort_px(img, K, dist)
        area = cv2.contourArea(cv2.convexHull(brd.astype(np.float32)))
        if area < 100.0 * 100.0:
            raise CameraGeometryError("The board points are too close together (span at least 10 x 10 cm).")
        if cv2.contourArea(cv2.convexHull(und.astype(np.float32))) < 400.0:
            raise CameraGeometryError("The image points are too close together.")
        H, _ = cv2.findHomography(brd, und, 0)
        if H is None or not np.all(np.isfinite(H)):
            raise CameraGeometryError("No mapping can be computed from these points (check their order).")
        cam = cls(H, (int(image_size[0]), int(image_size[1])), None if K is None else np.asarray(K, float),
                  None if dist is None else np.asarray(dist, float).ravel())
        proj = cam.to_image(brd)
        back = cam.to_board(img)
        cam.rms_px = float(np.sqrt(np.nanmean(np.sum((proj - img) ** 2, axis=1))))
        cam.rms_mm = float(np.sqrt(np.nanmean(np.sum((back - brd) ** 2, axis=1))))
        if not cam._orientation_ok(brd):
            raise CameraGeometryError("The points are swapped (check the order of the corners).")
        return cam

    def _orientation_ok(self, brd: np.ndarray) -> bool:
        """All correspondences in front of the camera (no point beyond the horizon)."""
        q = _h(brd) @ self.H.T
        return bool(np.all(q[:, 2] > 0) or np.all(q[:, 2] < 0))

    # ------------------------------------------------------------------ mapping
    def to_image(self, board_xy, z: float = 0.0) -> np.ndarray:
        """Board points (N,2) at height z (mm in front of the board) -> raw image px."""
        b = np.asarray(board_xy, float).reshape(-1, 2)
        if z:
            c = self.center
            k = c[2] / (c[2] - z)
            b = c[:2] + (b - c[:2]) * k
        return distort_px(apply_h(self.H, b), self.K, self.dist)

    def to_board(self, image_px, z: float = 0.0) -> np.ndarray:
        """Raw image px (N,2) -> board mm of points at height z."""
        b = apply_h(self.Hi, undistort_px(np.asarray(image_px, float).reshape(-1, 2), self.K, self.dist))
        if z:
            c = self.center
            s = (c[2] - z) / c[2]
            b = c[:2] + (b - c[:2]) * s
        return b

    def mm_per_px(self, board_xy) -> float:
        """Local board resolution at a board point (mm per image pixel, geometric mean)."""
        p = np.asarray(board_xy, float).reshape(2)
        q = self.to_image(np.array([p, p + (1, 0), p + (0, 1)]))
        a, b = np.linalg.norm(q[1] - q[0]), np.linalg.norm(q[2] - q[0])
        return float(1.0 / math.sqrt(max(a * b, 1e-12)))

    # ------------------------------------------------------------------ rectification
    def rectify_maps(self, rect: tuple[float, float, float, float], px_per_mm: float, z: float = 0.0):
        """cv2.remap maps that turn the raw image into a top-down board view of ``rect``
        (x0, y0, x1, y1 mm; output row 0 = y1). Cached."""
        key = (tuple(round(v, 3) for v in rect), round(px_per_mm, 4), round(z, 2))
        if key in self._maps:
            return self._maps[key]
        x0, y0, x1, y1 = rect
        w, hgt = max(1, int(round((x1 - x0) * px_per_mm))), max(1, int(round((y1 - y0) * px_per_mm)))
        xs = x0 + (np.arange(w) + 0.5) / px_per_mm
        ys = y1 - (np.arange(hgt) + 0.5) / px_per_mm
        gx, gy = np.meshgrid(xs, ys)
        src = self.to_image(np.column_stack([gx.ravel(), gy.ravel()]), z)
        src = np.nan_to_num(src, nan=-1e6)
        mx = src[:, 0].reshape(hgt, w).astype(np.float32)
        my = src[:, 1].reshape(hgt, w).astype(np.float32)
        if len(self._maps) > 6:
            self._maps.clear()
        self._maps[key] = (mx, my)
        return mx, my

    def rectify(self, frame: np.ndarray, rect, px_per_mm: float, border=(0, 0, 0), z: float = 0.0) -> np.ndarray:
        """Top-down view of ``rect`` on the plane ``z`` mm in front of the board."""
        mx, my = self.rectify_maps(rect, px_per_mm, z)
        return cv2.remap(frame, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=border)

    def coverage(self, rect: tuple[float, float, float, float]) -> list[list[float]]:
        """Part of ``rect`` (board mm) the camera sees, as a polygon [[x, y], ...] (may be empty)."""
        from shapely.geometry import Polygon, box
        w, hgt = self.image_size
        n = 24
        border = np.concatenate([np.column_stack([np.linspace(0, w, n), np.zeros(n)]),
                                 np.column_stack([np.full(n, w), np.linspace(0, hgt, n)]),
                                 np.column_stack([np.linspace(w, 0, n), np.full(n, hgt)]),
                                 np.column_stack([np.zeros(n), np.linspace(hgt, 0, n)])])
        und = undistort_px(border, self.K, self.dist)
        q = _h(und) @ self.Hi.T
        sign = np.sign(np.median(_h(self.to_image([[(rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2]])) @ self.Hi.T, axis=0)[2])
        ok = q[:, 2] * sign > 1e-9
        if ok.sum() < 3:
            return []
        pts = q[ok, :2] / q[ok, 2:3]
        try:
            poly = Polygon(pts).buffer(0).intersection(box(*rect))
        except Exception:  # noqa: BLE001 - degenerate geometry
            return []
        if poly.is_empty:
            return []
        if poly.geom_type != "Polygon":
            poly = max(poly.geoms, key=lambda g: g.area)
        return [[round(float(x), 2), round(float(y), 2)] for x, y in poly.exterior.coords[:-1]]

    # ------------------------------------------------------------------ serialisation
    def to_json(self) -> dict:
        return {"H": self.H.tolist(), "H_inv": self.Hi.tolist(), "image_size": list(self.image_size),
                "K": None if self.K is None else np.asarray(self.K).tolist(),
                "dist": None if self.dist is None else np.asarray(self.dist).ravel().tolist(),
                "rms_px": round(self.rms_px, 3), "rms_mm": round(self.rms_mm, 3),
                "focal_px": round(self.focal_px, 1), "focal_estimated": self.focal_estimated,
                "center": [round(float(v), 1) for v in self.center]}


# ---------------------------------------------------------------------- lens distortion
def undistort_px(px, K, dist) -> np.ndarray:
    p = np.asarray(px, float).reshape(-1, 2)
    if K is None or dist is None or not np.any(np.asarray(dist)):
        return p.copy()
    out = cv2.undistortPoints(p.reshape(-1, 1, 2), np.asarray(K, float), np.asarray(dist, float), P=np.asarray(K, float))
    return out.reshape(-1, 2)


def distort_px(px, K, dist) -> np.ndarray:
    """Inverse of ``undistort_px`` (OpenCV model k1, k2, p1, p2, k3)."""
    p = np.asarray(px, float).reshape(-1, 2)
    if K is None or dist is None or not np.any(np.asarray(dist)):
        return p.copy()
    K = np.asarray(K, float)
    d = np.zeros(5)
    dd = np.asarray(dist, float).ravel()[:5]
    d[:len(dd)] = dd
    k1, k2, p1, p2, k3 = d
    x = (p[:, 0] - K[0, 2]) / K[0, 0]
    y = (p[:, 1] - K[1, 2]) / K[1, 1]
    r2 = x * x + y * y
    rad = 1 + k1 * r2 + k2 * r2 * r2 + k3 * r2 ** 3
    xd = x * rad + 2 * p1 * x * y + p2 * (r2 + 2 * x * x)
    yd = y * rad + p1 * (r2 + 2 * y * y) + 2 * p2 * x * y
    return np.column_stack([xd * K[0, 0] + K[0, 2], yd * K[1, 1] + K[1, 2]])


def calibrate_lens(corner_sets: list[np.ndarray], pattern: tuple[int, int], square_mm: float,
                   image_size: tuple[int, int]) -> dict:
    """Intrinsics from several chessboard views. Returns {K, dist, rms_px, views}."""
    if len(corner_sets) < 4:
        raise CameraGeometryError("At least 4 chessboard shots from different angles are needed (10-15 is better).")
    cols, rows = pattern
    obj = np.zeros((cols * rows, 3), np.float32)
    obj[:, :2] = np.mgrid[0:cols, 0:rows].T.reshape(-1, 2) * float(square_mm)
    objs = [obj] * len(corner_sets)
    imgs = [np.asarray(c, np.float32).reshape(-1, 1, 2) for c in corner_sets]
    flags = cv2.CALIB_FIX_K3 if len(corner_sets) < 10 else 0
    rms, K, dist, _, _ = cv2.calibrateCamera(objs, imgs, (int(image_size[0]), int(image_size[1])), None, None, flags=flags)
    if not math.isfinite(rms) or rms > 3.0:
        raise CameraGeometryError(f"Lens calibration unusable (residual {rms:.2f} px). Keep the chessboard flat, use more angles.")
    return {"K": K.tolist(), "dist": dist.ravel().tolist(), "rms_px": float(rms), "views": len(corner_sets),
            "image_size": [int(image_size[0]), int(image_size[1])]}
