"""Image -> polylines. Optional: needs OpenCV (``pip install wbbot[imaging]``)."""

from __future__ import annotations


def opencv_available() -> bool:
    try:
        import cv2  # noqa: F401,PLC0415
        return True
    except Exception:
        return False


__all__ = ["opencv_available"]
