"""Homography between camera pixels and board millimetres.

Calibrated from the four board corners: put a marker on each corner of the
drawing area, click them in the camera image, and this maps every pixel to a
board coordinate and back.  That is what lets the AI say "there is free space
at (400, 700) mm" instead of "there is free space at pixel (812, 233)".
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

import numpy as np

__all__ = ["BoardMapError", "BoardMap"]


class BoardMapError(ValueError):
    """The camera-to-board mapping is missing or degenerate."""


def _cv2():
    try:
        import cv2  # noqa: PLC0415
        return cv2
    except ImportError as exc:  # pragma: no cover
        raise BoardMapError(
            "OpenCV is required for camera support "
            "(pip install 'wbbot[imaging]')") from exc


@dataclass
class BoardMap:
    """Camera pixels <-> board millimetres."""

    matrix: np.ndarray            # 3x3, pixel -> mm
    board_width_mm: float
    board_height_mm: float

    def __post_init__(self) -> None:
        m = np.asarray(self.matrix, dtype=float)
        if m.shape != (3, 3):
            raise BoardMapError(f"homography must be 3x3, got {m.shape}")
        if not np.all(np.isfinite(m)):
            raise BoardMapError("homography contains non-finite values")
        if abs(float(np.linalg.det(m))) < 1e-12:
            raise BoardMapError("homography is singular — the four corners are "
                                "probably collinear or duplicated")
        object.__setattr__(self, "matrix", m)

    # -- construction ---------------------------------------------------

    @classmethod
    def from_corners(cls, corners_px: Sequence[Sequence[float]],
                     board_width_mm: float, board_height_mm: float
                     ) -> "BoardMap":
        """Build from the four drawing-area corners seen in the camera image.

        ``corners_px`` is ordered **bottom-left, bottom-right, top-right,
        top-left** — matching the board's own coordinate system, whose origin
        is the lower-left corner.
        """
        cv2 = _cv2()
        src = np.asarray(corners_px, dtype=np.float32)
        if src.shape != (4, 2):
            raise BoardMapError(
                f"need exactly four corner points, got shape {src.shape}")
        if not np.all(np.isfinite(src)):
            raise BoardMapError("corner points contain non-finite values")
        dst = np.array([
            [0.0, 0.0],
            [board_width_mm, 0.0],
            [board_width_mm, board_height_mm],
            [0.0, board_height_mm],
        ], dtype=np.float32)
        matrix = cv2.getPerspectiveTransform(src, dst)
        return cls(matrix=np.asarray(matrix, dtype=float),
                   board_width_mm=float(board_width_mm),
                   board_height_mm=float(board_height_mm))

    @classmethod
    def from_config(cls, cfg) -> "BoardMap | None":
        if cfg.camera.homography is None:
            return None
        return cls(matrix=np.array(cfg.camera.homography, dtype=float),
                   board_width_mm=cfg.board.width_mm,
                   board_height_mm=cfg.board.height_mm)

    # -- mapping --------------------------------------------------------

    @property
    def inverse_matrix(self) -> np.ndarray:
        return np.linalg.inv(self.matrix)

    def _apply(self, matrix: np.ndarray, points: Sequence[Sequence[float]]
               ) -> np.ndarray:
        pts = np.asarray(points, dtype=float).reshape(-1, 2)
        homo = np.concatenate([pts, np.ones((len(pts), 1))], axis=1)
        out = homo @ matrix.T
        w = out[:, 2:3]
        if np.any(np.abs(w) < 1e-12):
            raise BoardMapError("a point maps to infinity under this homography")
        return out[:, :2] / w

    def pixel_to_board(self, points: Sequence[Sequence[float]]) -> np.ndarray:
        """Camera pixels -> board millimetres."""
        return self._apply(self.matrix, points)

    def board_to_pixel(self, points: Sequence[Sequence[float]]) -> np.ndarray:
        """Board millimetres -> camera pixels."""
        return self._apply(self.inverse_matrix, points)

    # -- rectification --------------------------------------------------

    def rectify(self, image: np.ndarray, px_per_mm: float = 1.0) -> np.ndarray:
        """Warp a camera frame into a straight-on view of the board.

        The result is board-aligned with ``y`` **flipped back to image order**
        (row 0 = top of the board), which is what every image routine expects.
        """
        cv2 = _cv2()
        if px_per_mm <= 0:
            raise BoardMapError("px_per_mm must be > 0")
        w = max(int(round(self.board_width_mm * px_per_mm)), 8)
        h = max(int(round(self.board_height_mm * px_per_mm)), 8)
        # mm -> output pixels, flipping Y so the board top ends up at row 0.
        scale = np.array([[px_per_mm, 0.0, 0.0],
                          [0.0, -px_per_mm, h],
                          [0.0, 0.0, 1.0]], dtype=float)
        full = scale @ self.matrix
        return cv2.warpPerspective(image, full, (w, h), flags=cv2.INTER_LINEAR)

    def rectified_pixel_to_board(self, points: Sequence[Sequence[float]],
                                 image_height_px: int,
                                 px_per_mm: float = 1.0) -> np.ndarray:
        """Map a point in a rectified image back to board millimetres."""
        pts = np.asarray(points, dtype=float).reshape(-1, 2)
        out = np.empty_like(pts)
        out[:, 0] = pts[:, 0] / px_per_mm
        out[:, 1] = (image_height_px - pts[:, 1]) / px_per_mm
        return out

    # -- quality --------------------------------------------------------

    def reprojection_error(self, corners_px: Sequence[Sequence[float]]) -> float:
        """Max reprojection error in millimetres for the calibration corners."""
        got = self.pixel_to_board(corners_px)
        want = np.array([[0.0, 0.0],
                         [self.board_width_mm, 0.0],
                         [self.board_width_mm, self.board_height_mm],
                         [0.0, self.board_height_mm]])
        return float(np.max(np.linalg.norm(got - want, axis=1)))

    # -- persistence ----------------------------------------------------

    def to_list(self) -> list[list[float]]:
        return [[float(v) for v in row] for row in self.matrix]

    def to_overlay(self) -> dict:
        return {"camera": {"homography": self.to_list(), "enabled": True}}

    def __repr__(self) -> str:      # pragma: no cover
        return (f"BoardMap({self.board_width_mm:.0f} x "
                f"{self.board_height_mm:.0f} mm)")
