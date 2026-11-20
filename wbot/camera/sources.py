"""Frame sources. Each runs one grabber thread and keeps only the newest frame (never a queue).

* ``DeviceSource``  local camera via OpenCV. On Windows DirectShow first (OBS Virtual Camera is a
  DirectShow device), then Media Foundation. Asks for MJPG at the wanted resolution (1080p webcams
  only reach 30 fps compressed). Reopens itself after a loss (unplugged, OBS stopped).
* ``UrlSource``     network stream OpenCV can read (MJPEG over http, rtsp, IP-camera apps).
* ``SimSource``     renders ``SimCamera`` frames from the connected simulator.
* ``StillSource``   a fixed image (tests, debugging a saved snapshot).
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable

import cv2
import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Frame:
    image: np.ndarray       # BGR uint8
    t: float                # time.time() when grabbed
    seq: int


class Source:
    kind = "none"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._seq = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.error: str | None = None
        self.fps = 0.0
        self._times: list[float] = []

    # --- lifecycle --------------------------------------------------------------
    def start(self) -> "Source":
        self._thread = threading.Thread(target=self._run, name=f"camera-{self.kind}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)
        self._release()

    def _release(self) -> None:
        pass

    def _run(self) -> None:          # pragma: no cover - overridden
        raise NotImplementedError

    # --- frames -------------------------------------------------------------------
    def _publish(self, img: np.ndarray) -> None:
        now = time.time()
        with self._lock:
            self._seq += 1
            self._frame = Frame(img, now, self._seq)
            self._times.append(now)
            self._times = self._times[-30:]
            if len(self._times) >= 2:
                self.fps = (len(self._times) - 1) / max(self._times[-1] - self._times[0], 1e-6)
        self.error = None

    def latest(self) -> Frame | None:
        with self._lock:
            return self._frame

    def wait_frame(self, after_seq: int = 0, timeout: float = 3.0) -> Frame | None:
        """Newest frame with ``seq > after_seq`` (blocks up to ``timeout``)."""
        t_end = time.time() + timeout
        while time.time() < t_end and not self._stop.is_set():
            f = self.latest()
            if f is not None and f.seq > after_seq:
                return f
            time.sleep(0.01)
        return None

    def info(self) -> dict:
        f = self.latest()
        return {"kind": self.kind, "fps": round(self.fps, 1), "error": self.error,
                "size": None if f is None else [int(f.image.shape[1]), int(f.image.shape[0])],
                "age_s": None if f is None else round(time.time() - f.t, 2)}


class _CaptureSource(Source):
    """Common loop for OpenCV ``VideoCapture`` sources."""
    reopen_s = 2.0

    def __init__(self) -> None:
        super().__init__()
        self.cap = None

    def _open(self):                 # pragma: no cover - overridden
        raise NotImplementedError

    def _release(self) -> None:
        cap, self.cap = self.cap, None
        if cap is not None:
            try:
                cap.release()
            except Exception:  # noqa: BLE001
                pass

    def _run(self) -> None:
        fails = 0
        while not self._stop.is_set():
            if self.cap is None:
                try:
                    self.cap = self._open()
                except Exception as exc:  # noqa: BLE001
                    self.cap, self.error = None, f"Kamera nicht verfügbar: {exc}"
                if self.cap is None:
                    self._stop.wait(self.reopen_s)
                    continue
            ok, img = self.cap.read()
            if not ok or img is None:
                fails += 1
                if fails >= 15:
                    self.error = "Kamera liefert keine Bilder mehr (abgesteckt, OBS beendet?). Neuer Versuch ..."
                    self._release()
                    fails = 0
                    self._stop.wait(self.reopen_s)
                else:
                    self._stop.wait(0.03)
                continue
            fails = 0
            if img.ndim == 2:
                img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
            self._publish(img)
        self._release()


def _backends() -> list[int]:
    if sys.platform == "win32":
        return [cv2.CAP_DSHOW, cv2.CAP_MSMF]
    if sys.platform == "darwin":
        return [cv2.CAP_AVFOUNDATION]
    return [cv2.CAP_V4L2, cv2.CAP_ANY]


class DeviceSource(_CaptureSource):
    kind = "device"

    def __init__(self, index: int, width: int = 1920, height: int = 1080, fps: int = 30) -> None:
        super().__init__()
        self.index, self.want = int(index), (int(width), int(height), int(fps))

    def _open(self):
        last = "unbekannt"
        for be in _backends():
            cap = cv2.VideoCapture(self.index, be)
            if not cap.isOpened():
                cap.release()
                continue
            w, h, fps = self.want
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
            cap.set(cv2.CAP_PROP_FPS, fps)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            ok, img = cap.read()
            if ok and img is not None:
                log.info("camera %d opened (backend %s) %dx%d", self.index, cap.getBackendName(), img.shape[1], img.shape[0])
                self._publish(img)
                return cap
            last = f"Gerät {self.index} öffnet, liefert aber kein Bild"
            cap.release()
        self.error = f"Kamera {self.index} nicht verfügbar ({last})."
        return None


class UrlSource(_CaptureSource):
    kind = "url"

    def __init__(self, url: str) -> None:
        super().__init__()
        self.url = url

    def _open(self):
        cap = cv2.VideoCapture(self.url)
        if not cap.isOpened():
            cap.release()
            self.error = "Stream nicht erreichbar (Adresse prüfen)."
            return None
        return cap


class SimSource(Source):
    kind = "sim"

    def __init__(self, render: Callable[[], np.ndarray | None], fps: float = 8.0) -> None:
        super().__init__()
        self.render, self.period = render, 1.0 / max(fps, 0.5)

    def _run(self) -> None:
        while not self._stop.is_set():
            t0 = time.time()
            try:
                img = self.render()
                if img is not None:
                    self._publish(img)
            except Exception as exc:  # noqa: BLE001
                self.error = f"Simulator-Kamera: {exc}"
                log.exception("sim camera failed")
            self._stop.wait(max(0.0, self.period - (time.time() - t0)))


class StillSource(Source):
    kind = "still"

    def __init__(self, image: np.ndarray) -> None:
        super().__init__()
        self.image = image

    def _run(self) -> None:
        while not self._stop.is_set():
            self._publish(self.image)
            self._stop.wait(0.2)


def probe_devices(max_index: int = 6, thumb_w: int = 240) -> list[dict]:
    """Try indices 0..max_index-1. Returns [{index, size, thumb (JPEG bytes)}] of devices that deliver
    a picture (the thumbnail lets the user recognise e.g. the OBS Virtual Camera)."""
    out = []
    for i in range(max_index):
        for be in _backends():
            cap = cv2.VideoCapture(i, be)
            try:
                if not cap.isOpened():
                    continue
                ok, img = cap.read()
                if not ok or img is None:
                    continue
                k = thumb_w / img.shape[1]
                th = cv2.resize(img, (thumb_w, max(1, int(img.shape[0] * k))), interpolation=cv2.INTER_AREA)
                ok2, jpg = cv2.imencode(".jpg", th, [cv2.IMWRITE_JPEG_QUALITY, 70])
                out.append({"index": i, "size": [int(img.shape[1]), int(img.shape[0])],
                            "backend": cap.getBackendName(), "thumb": jpg.tobytes() if ok2 else b""})
                break
            finally:
                cap.release()
    return out
