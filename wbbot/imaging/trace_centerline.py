"""Skeleton tracing — for line art that is already strokes, not regions.

Outlining a hand-drawn line with ``findContours`` gives you its *outline*: a
long thin loop that the plotter draws twice, once down each side.  Skeletonising
first collapses the stroke to its centreline, which is what was actually drawn.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..core.geometry import Polyline
from .preprocess import ImagingError, _cv2, to_gray
from .tracing import thin_mask, trace_thin_mask

__all__ = ["CenterlineParams", "trace_centerline"]


@dataclass
class CenterlineParams:
    threshold: int | None = None          # None -> Otsu
    dark_is_ink: bool = True
    #: Adaptive thresholding copes with uneven lighting (phone photos of paper).
    adaptive: bool = False
    adaptive_block_px: int = 31
    adaptive_c: float = 8.0
    cleanup_px: int = 1
    min_length_px: float = 10.0
    min_points: int = 3
    simplify_px: float = 0.75
    #: Prune skeleton spurs shorter than this many pixels.
    prune_px: float = 4.0

    def validate(self) -> None:
        if self.adaptive_block_px < 3 or self.adaptive_block_px % 2 == 0:
            raise ImagingError("adaptive_block_px must be an odd number >= 3")
        if self.threshold is not None and not 0 <= self.threshold <= 255:
            raise ImagingError("threshold must be 0..255 or None for Otsu")


def trace_centerline(image: np.ndarray,
                     params: CenterlineParams | None = None) -> list[Polyline]:
    """Skeletonise the ink and follow it. Returns pixel-space polylines."""
    cv2 = _cv2()
    params = params or CenterlineParams()
    params.validate()
    gray = to_gray(image)

    if params.adaptive:
        mode = cv2.THRESH_BINARY_INV if params.dark_is_ink else cv2.THRESH_BINARY
        mask = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                     mode, params.adaptive_block_px,
                                     params.adaptive_c)
    elif params.threshold is None:
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
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    skeleton = thin_mask(mask)
    raw = trace_thin_mask(skeleton, min_points=params.min_points,
                          min_length_px=params.min_length_px)

    out: list[Polyline] = []
    for pts in raw:
        length = float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1)))
        # Skeletonisation sprouts short spurs at every bump in the outline;
        # they are artefacts, not strokes.
        if length < params.prune_px:
            continue
        pl = Polyline(pts, closed=False)
        if params.simplify_px > 0:
            pl = pl.simplify(params.simplify_px)
        pl = pl.dedupe()
        if len(pl) >= 2:
            out.append(pl)
    return out
