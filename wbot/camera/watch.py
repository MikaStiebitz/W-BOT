"""Notice when the camera was moved (bumped, re-aimed, after a restart) and follow it.

At every board calibration the current image is stored as the *reference* (``camera_ref.png`` next
to ``camera.json``). Later images are compared with it: ORB features, ratio-test matches and a RANSAC
homography from the reference to the current image. Writing on the board, the gondola or a person
change only part of the picture; the wall, the frame and the room around keep enough features for
RANSAC. If the picture moved by more than a few pixels and the match is trustworthy, the stored
image points of the calibration are carried over with that homography - the board stays aligned
without a new calibration. If the view changed too much to match, the caller is told to re-calibrate.
"""
from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)
WORK_W = 960
MIN_INLIERS = 40
MIN_INLIER_RATIO = 0.3
MOVED_PX = 3.0            # full-resolution pixels


class CameraWatch:
    def __init__(self, path: Path | None = None):
        self.path = path
        self.orb = cv2.ORB_create(nfeatures=3000, fastThreshold=12)
        self.ref_kp = None
        self.ref_des = None
        self.ref_size: tuple[int, int] | None = None     # full-resolution (w, h) of the reference
        self.k = 1.0

    @staticmethod
    def _small(img: np.ndarray) -> tuple[np.ndarray, float]:
        h, w = img.shape[:2]
        k = min(1.0, WORK_W / w)
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
        if k < 1.0:
            g = cv2.resize(g, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
        return cv2.equalizeHist(g), k

    @property
    def has_reference(self) -> bool:
        return self.ref_des is not None and len(self.ref_des) >= MIN_INLIERS

    def set_reference(self, img: np.ndarray, save: bool = True) -> None:
        g, k = self._small(img)
        self.ref_kp, self.ref_des = self.orb.detectAndCompute(g, None)
        self.ref_size, self.k = (int(img.shape[1]), int(img.shape[0])), k
        if save and self.path is not None:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(self.path), g)
            except OSError as exc:
                log.warning("camera reference image not saved: %s", exc)

    def load(self, size: tuple[int, int]) -> bool:
        """Reference image from disk (after a restart). ``size``: full-resolution size it belongs to."""
        if self.path is None or not self.path.exists():
            return False
        g = cv2.imread(str(self.path), cv2.IMREAD_GRAYSCALE)
        if g is None:
            return False
        self.ref_kp, self.ref_des = self.orb.detectAndCompute(g, None)
        self.ref_size, self.k = (int(size[0]), int(size[1])), g.shape[1] / float(size[0])
        return self.has_reference

    def check(self, img: np.ndarray) -> dict | None:
        """Compare with the reference. Returns {moved, ok, shift_px, inliers, H (full-res ref -> current)}."""
        if not self.has_reference or (img.shape[1], img.shape[0]) != self.ref_size:
            return None
        g, k = self._small(img)
        kp, des = self.orb.detectAndCompute(g, None)
        if des is None or len(des) < MIN_INLIERS:
            return {"moved": True, "ok": False, "shift_px": None, "inliers": 0, "H": None}
        pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(self.ref_des, des, k=2)
        good = [m for m, *n in pairs if n and m.distance < 0.75 * n[0].distance]
        if len(good) < MIN_INLIERS:
            return {"moved": True, "ok": False, "shift_px": None, "inliers": len(good), "H": None}
        src = np.float32([self.ref_kp[m.queryIdx].pt for m in good])
        dst = np.float32([kp[m.trainIdx].pt for m in good])
        Hs, inl = cv2.findHomography(src, dst, cv2.RANSAC, 2.5)
        n_in = int(inl.sum()) if inl is not None else 0
        ok = Hs is not None and n_in >= MIN_INLIERS and n_in >= MIN_INLIER_RATIO * len(good)
        if not ok:
            return {"moved": True, "ok": False, "shift_px": None, "inliers": n_in, "H": None}
        H = np.diag([1 / k, 1 / k, 1.0]) @ Hs @ np.diag([self.k, self.k, 1.0])   # full-res reference -> current
        w, h = self.ref_size
        grid = np.array([[x, y, 1.0] for x in np.linspace(0, w, 7) for y in np.linspace(0, h, 5)])
        q = grid @ H.T
        shift = float(np.median(np.linalg.norm(q[:, :2] / q[:, 2:3] - grid[:, :2], axis=1)))
        return {"moved": shift > MOVED_PX, "ok": True, "shift_px": round(shift, 2), "inliers": n_in, "H": H}


def carry_points(H: np.ndarray, pts) -> list[list[float]]:
    p = np.column_stack([np.asarray(pts, float).reshape(-1, 2), np.ones(len(pts))]) @ np.asarray(H).T
    return (p[:, :2] / p[:, 2:3]).tolist()
