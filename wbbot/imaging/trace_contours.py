"""Canny edge tracing — the workhorse for photographs.

Canny produces one-pixel-wide edges, so the strokes are followed with the
graph walker rather than ``findContours`` (see :mod:`wbbot.imaging.tracing`
for why that distinction matters).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..core.geometry import Polyline
from .preprocess import ImagingError, _cv2, to_gray
from .tracing import trace_thin_mask

__all__ = ["ContourParams", "trace_contours"]


@dataclass
class ContourParams:
    canny_low: int = 60
    canny_high: int = 160
    #: Gaussian pre-blur in pixels; 0 disables it.
    blur_px: int = 3
    #: Drop strokes shorter than this, in pixels.
    min_length_px: float = 8.0
    min_points: int = 3
    #: Close 1-pixel gaps in the edge map before tracing.
    close_gaps: bool = True
    #: Douglas-Peucker tolerance in pixels applied right after tracing.
    simplify_px: float = 0.75
    #: Auto-pick the thresholds from the image median (Otsu-style heuristic).
    auto_threshold: bool = False

    def validate(self) -> None:
        if not 0 <= self.canny_low <= 255 or not 0 <= self.canny_high <= 255:
            raise ImagingError("Canny thresholds must be 0..255")
        if self.canny_low > self.canny_high:
            raise ImagingError(
                f"canny_low ({self.canny_low}) must not exceed canny_high "
                f"({self.canny_high})")
        if self.blur_px < 0 or self.blur_px > 51:
            raise ImagingError("blur_px must be 0..51")


def trace_contours(image: np.ndarray,
                   params: ContourParams | None = None) -> list[Polyline]:
    """Trace edges of a BGR or grayscale image. Returns pixel-space polylines."""
    cv2 = _cv2()
    params = params or ContourParams()
    params.validate()

    gray = to_gray(image)
    if params.blur_px > 0:
        k = params.blur_px if params.blur_px % 2 == 1 else params.blur_px + 1
        gray = cv2.GaussianBlur(gray, (k, k), 0)

    low, high = params.canny_low, params.canny_high
    if params.auto_threshold:
        median = float(np.median(gray))
        low = int(max(0, 0.66 * median))
        high = int(min(255, 1.33 * median))
        if low >= high:
            low, high = max(high - 30, 0), min(high + 30, 255)

    edges = cv2.Canny(gray, low, high, L2gradient=True)

    if params.close_gaps:
        kernel = np.ones((3, 3), np.uint8)
        edges = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)
        # Closing thickens the edges, so thin them back to one pixel —
        # otherwise the graph walker sees blobs instead of lines.
        from .tracing import thin_mask   # noqa: PLC0415 - optional dependency
        edges = thin_mask(edges)

    raw = trace_thin_mask(edges, min_points=params.min_points,
                          min_length_px=params.min_length_px)

    out: list[Polyline] = []
    for pts in raw:
        pl = Polyline(pts, closed=False)
        if params.simplify_px > 0:
            pl = pl.simplify(params.simplify_px)
        pl = pl.dedupe()
        if len(pl) >= 2:
            out.append(pl)
    return out
