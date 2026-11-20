"""Load and condition an image before tracing.

Bilateral filtering rather than a Gaussian blur: it kills sensor noise while
leaving edges sharp, and edges are the entire product here.  A Gaussian would
round exactly the corners the plotter should draw.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

__all__ = [
    "ImagingError",
    "PreprocessParams",
    "load_image",
    "decode_image",
    "preprocess",
    "to_gray",
    "correct_perspective",
    "auto_levels",
]


class ImagingError(RuntimeError):
    """Image could not be read or processed."""


def _cv2():
    try:
        import cv2  # noqa: PLC0415
        return cv2
    except ImportError as exc:  # pragma: no cover
        raise ImagingError(
            "OpenCV is required for the image pipeline "
            "(pip install 'wbbot[imaging]')") from exc


@dataclass
class PreprocessParams:
    """Everything the user can turn in the UI before tracing."""

    max_dimension_px: int = 1600
    denoise: bool = True
    bilateral_d: int = 9
    bilateral_sigma_color: float = 75.0
    bilateral_sigma_space: float = 75.0
    #: Standard convention (``out = in ** (1/gamma)``, as in PIL and
    #: ImageMagick): >1 brightens the mid-tones, <1 darkens them.
    gamma: float = 1.0
    contrast: float = 1.0
    brightness: float = 0.0
    auto_levels: bool = False
    invert: bool = False
    #: Optional 4-point perspective quad (camera input), in source pixels.
    perspective_quad: Sequence[Sequence[float]] | None = None

    def validate(self) -> None:
        if not 64 <= self.max_dimension_px <= 8192:
            raise ImagingError(
                f"max_dimension_px must be 64..8192, got {self.max_dimension_px}")
        if self.bilateral_d < 1 or self.bilateral_d > 51:
            raise ImagingError("bilateral_d must be 1..51")
        if not 0.05 <= self.gamma <= 10.0:
            raise ImagingError(f"gamma must be 0.05..10, got {self.gamma}")
        if not 0.0 < self.contrast <= 10.0:
            raise ImagingError(f"contrast must be >0..10, got {self.contrast}")
        if self.perspective_quad is not None:
            quad = np.asarray(self.perspective_quad, dtype=float)
            if quad.shape != (4, 2):
                raise ImagingError(
                    "perspective_quad must be four [x, y] points, got shape "
                    f"{quad.shape}")


# --------------------------------------------------------------------------

def load_image(path: str | Path) -> np.ndarray:
    """Read an image file as BGR uint8."""
    cv2 = _cv2()
    p = Path(path)
    if not p.exists():
        raise ImagingError(f"image not found: {p}")
    # np.fromfile handles non-ASCII paths, which cv2.imread does not on Windows.
    data = np.fromfile(str(p), dtype=np.uint8)
    img = cv2.imdecode(data, cv2.IMREAD_COLOR)
    if img is None:
        raise ImagingError(f"cannot decode {p} — unsupported or corrupt image")
    return img


def decode_image(data: bytes) -> np.ndarray:
    """Decode image bytes (an upload) as BGR uint8."""
    cv2 = _cv2()
    if not data:
        raise ImagingError("empty image upload")
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ImagingError("cannot decode the uploaded data as an image")
    return img


def to_gray(img: np.ndarray) -> np.ndarray:
    cv2 = _cv2()
    if img.ndim == 2:
        return img
    return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def auto_levels(gray: np.ndarray, low_percentile: float = 1.0,
                high_percentile: float = 99.0) -> np.ndarray:
    """Stretch the histogram between two percentiles."""
    lo = float(np.percentile(gray, low_percentile))
    hi = float(np.percentile(gray, high_percentile))
    if hi - lo < 1e-6:
        return gray.copy()
    out = (gray.astype(np.float32) - lo) * (255.0 / (hi - lo))
    return np.clip(out, 0, 255).astype(np.uint8)


def correct_perspective(img: np.ndarray, quad: Sequence[Sequence[float]],
                        out_size: tuple[int, int] | None = None) -> np.ndarray:
    """Rectify a quadrilateral to a rectangle.

    ``quad`` is ordered top-left, top-right, bottom-right, bottom-left.  When
    ``out_size`` is omitted, the output keeps the quad's average side lengths,
    so a photographed board comes back roughly at its true aspect ratio.
    """
    cv2 = _cv2()
    src = np.asarray(quad, dtype=np.float32)
    if src.shape != (4, 2):
        raise ImagingError("perspective quad must be four [x, y] points")
    if out_size is None:
        width = int(round(0.5 * (np.linalg.norm(src[1] - src[0])
                                 + np.linalg.norm(src[2] - src[3]))))
        height = int(round(0.5 * (np.linalg.norm(src[3] - src[0])
                                  + np.linalg.norm(src[2] - src[1]))))
        out_size = (max(width, 16), max(height, 16))
    w, h = out_size
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]],
                   dtype=np.float32)
    matrix = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img, matrix, (w, h), flags=cv2.INTER_LINEAR)


def _resize_to_fit(img: np.ndarray, max_dim: int) -> np.ndarray:
    cv2 = _cv2()
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= max_dim:
        return img
    scale = max_dim / longest
    return cv2.resize(img, (max(int(round(w * scale)), 1),
                            max(int(round(h * scale)), 1)),
                      interpolation=cv2.INTER_AREA)


def preprocess(img: np.ndarray, params: PreprocessParams) -> np.ndarray:
    """Apply the whole conditioning chain. Returns BGR uint8."""
    params.validate()
    cv2 = _cv2()
    if img is None or img.size == 0:
        raise ImagingError("empty image")
    out = img
    if out.ndim == 2:
        out = cv2.cvtColor(out, cv2.COLOR_GRAY2BGR)

    if params.perspective_quad is not None:
        out = correct_perspective(out, params.perspective_quad)

    out = _resize_to_fit(out, params.max_dimension_px)

    if params.denoise:
        d = params.bilateral_d
        if d % 2 == 0:
            d += 1
        out = cv2.bilateralFilter(out, d, params.bilateral_sigma_color,
                                  params.bilateral_sigma_space)

    if params.contrast != 1.0 or params.brightness != 0.0:
        out = cv2.convertScaleAbs(out, alpha=params.contrast,
                                  beta=params.brightness)

    if params.gamma != 1.0:
        inv = 1.0 / params.gamma
        table = np.array([((i / 255.0) ** inv) * 255 for i in range(256)],
                         dtype=np.uint8)
        out = cv2.LUT(out, table)

    if params.auto_levels:
        channels = [auto_levels(out[:, :, c]) for c in range(out.shape[2])]
        out = np.stack(channels, axis=2)

    if params.invert:
        out = cv2.bitwise_not(out)
    return out
