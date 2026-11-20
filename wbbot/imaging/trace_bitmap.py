"""Threshold + region outlining — for logos, silhouettes and flat artwork.

This is the case ``findContours`` is actually designed for: filled regions
with an inside and an outside.  Holes are traced too, so a letter "O" keeps
both of its circles.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..core.geometry import Polyline
from .preprocess import ImagingError, _cv2, to_gray
from .tracing import trace_filled_regions

__all__ = ["BitmapParams", "trace_bitmap"]


@dataclass
class BitmapParams:
    #: Fixed threshold 0..255, or ``None`` for Otsu.
    threshold: int | None = None
    #: Trace dark regions (True) or bright ones (False).
    dark_is_ink: bool = True
    #: Ignore blobs smaller than this, in pixels squared.
    min_area_px: float = 40.0
    include_holes: bool = True
    #: Morphological cleanup radius in pixels; 0 disables it.
    cleanup_px: int = 1
    simplify_px: float = 0.75
    min_points: int = 4

    def validate(self) -> None:
        if self.threshold is not None and not 0 <= self.threshold <= 255:
            raise ImagingError("threshold must be 0..255 or None for Otsu")
        if self.cleanup_px < 0 or self.cleanup_px > 25:
            raise ImagingError("cleanup_px must be 0..25")


def binarize(image: np.ndarray, params: BitmapParams) -> np.ndarray:
    """Produce the ink mask (255 = draw here)."""
    cv2 = _cv2()
    params.validate()
    gray = to_gray(image)
    if params.threshold is None:
        _, mask = cv2.threshold(
            gray, 0, 255,
            (cv2.THRESH_BINARY_INV if params.dark_is_ink else cv2.THRESH_BINARY)
            + cv2.THRESH_OTSU)
    else:
        mode = cv2.THRESH_BINARY_INV if params.dark_is_ink else cv2.THRESH_BINARY
        _, mask = cv2.threshold(gray, params.threshold, 255, mode)

    if params.cleanup_px > 0:
        k = 2 * params.cleanup_px + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    return mask


def trace_bitmap(image: np.ndarray,
                 params: BitmapParams | None = None) -> list[Polyline]:
    """Outline the thresholded regions. Returns pixel-space closed polylines."""
    params = params or BitmapParams()
    mask = binarize(image, params)
    rings = trace_filled_regions(mask,
                                 min_points=params.min_points,
                                 min_area_px=params.min_area_px,
                                 include_holes=params.include_holes)
    out: list[Polyline] = []
    for pts, closed in rings:
        pl = Polyline(pts, closed=closed)
        if params.simplify_px > 0:
            pl = pl.simplify(params.simplify_px)
        pl = pl.dedupe()
        if len(pl) >= 3:
            out.append(pl)
    return out
