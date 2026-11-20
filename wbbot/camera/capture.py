"""Camera access, kept behind an interface so tests never need a webcam."""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Sequence

import numpy as np

log = logging.getLogger(__name__)

__all__ = ["CameraError", "Camera", "OpenCVCamera", "StillCamera"]


class CameraError(RuntimeError):
    """The camera is unavailable or returned nothing usable."""


class Camera(ABC):
    """Minimal camera interface: open, grab a BGR frame, close."""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def capture(self) -> np.ndarray:
        """Return one BGR frame. Raises :class:`CameraError` on failure."""

    @property
    def is_open(self) -> bool:
        return False

    def __enter__(self) -> "Camera":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class OpenCVCamera(Camera):
    """A real camera via OpenCV's ``VideoCapture``.

    Captures are serialised behind a lock: two MCP tools asking for the board
    at the same moment must not interleave grabs on one device.
    """

    def __init__(self, device_index: int = 0,
                 resolution: Sequence[int] = (1920, 1080),
                 warmup_frames: int = 4):
        self.device_index = int(device_index)
        self.resolution = (int(resolution[0]), int(resolution[1]))
        self.warmup_frames = max(int(warmup_frames), 0)
        self._cap = None
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._cap is not None and bool(self._cap.isOpened())

    def open(self) -> None:
        try:
            import cv2  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise CameraError(
                "OpenCV is required for camera capture "
                "(pip install 'wbbot[imaging]')") from exc
        if self.is_open:
            return
        cap = cv2.VideoCapture(self.device_index)
        if not cap.isOpened():
            raise CameraError(
                f"cannot open camera {self.device_index} — is it connected and "
                f"not already in use by another application?")
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.resolution[0])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.resolution[1])
        self._cap = cap
        # Webcams need a few frames before auto-exposure settles; a photo of
        # the board taken immediately is usually too dark to threshold.
        for _ in range(self.warmup_frames):
            cap.read()
            time.sleep(0.05)

    def close(self) -> None:
        with self._lock:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:      # pragma: no cover
                    pass
                self._cap = None

    def capture(self) -> np.ndarray:
        with self._lock:
            if not self.is_open:
                raise CameraError("camera is not open")
            ok, frame = self._cap.read()
            if not ok or frame is None or frame.size == 0:
                raise CameraError(
                    f"camera {self.device_index} returned no frame")
            return frame


class StillCamera(Camera):
    """A camera that always returns the same image.

    Used by the tests and by anyone developing the read-back loop from a
    photograph instead of live hardware.
    """

    def __init__(self, image: np.ndarray | str | Path):
        if isinstance(image, (str, Path)):
            from ..imaging.preprocess import load_image  # noqa: PLC0415
            self._image = load_image(image)
        else:
            arr = np.asarray(image)
            if arr.ndim != 3 or arr.shape[2] != 3:
                raise CameraError("StillCamera needs a BGR image array")
            self._image = arr
        self._open = False
        self.captures = 0

    @property
    def is_open(self) -> bool:
        return self._open

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def capture(self) -> np.ndarray:
        if not self._open:
            raise CameraError("camera is not open")
        self.captures += 1
        return self._image.copy()


def make_camera(cfg) -> Camera:
    """Build the camera named by the configuration."""
    if not cfg.camera.enabled:
        raise CameraError(
            "the camera is disabled — set camera.enabled: true in machine.yaml")
    return OpenCVCamera(cfg.camera.device_index, cfg.camera.resolution)
