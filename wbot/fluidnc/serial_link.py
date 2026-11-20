"""pyserial-backed :class:`Link` for the MKS DLC32 (CH340, 115200 8N1)."""
from __future__ import annotations

import logging
import threading

from .link import Link, LinkError

try:  # pragma: no cover - import guard only
    import serial
    from serial.tools import list_ports as _list_ports
except ImportError:  # pragma: no cover
    serial = None  # type: ignore[assignment]
    _list_ports = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

__all__ = ["SerialLink", "list_ports"]


def list_ports() -> list[dict]:
    """Available serial ports as ``{device, description, hwid, vid, pid}`` (sorted by device)."""
    if _list_ports is None:  # pragma: no cover
        return []
    out = []
    for p in _list_ports.comports():
        out.append({"device": p.device, "description": p.description or "",
                    "hwid": p.hwid or "", "vid": p.vid, "pid": p.pid})
    out.sort(key=lambda d: d["device"])
    return out


class SerialLink(Link):
    """Serial port with exactly one write lock.

    pyserial's Windows backend keeps a single OVERLAPPED structure for writes, so two
    concurrent ``write()`` calls corrupt each other and surface as a bogus "Write timeout"
    (DECISIONS D-037). Every write therefore goes through ``_wlock``. Reads happen from the
    single reader thread of the client and use their own OVERLAPPED structure.
    """

    WRITE_TIMEOUT_S = 2.0

    def __init__(self, port: str, baudrate: int = 115200, *, write_timeout: float = WRITE_TIMEOUT_S):
        self.port = port
        self.baudrate = baudrate
        self.write_timeout = write_timeout
        self.name = f"serial:{port}@{baudrate}"
        self._ser = None
        self._wlock = threading.Lock()
        self._state_lock = threading.Lock()
        self._read_timeout = 0.05

    # --- Link ---------------------------------------------------------------------
    def open(self) -> None:
        if serial is None:  # pragma: no cover
            raise LinkError("pyserial is not installed")
        with self._state_lock:
            if self._ser is not None and self._ser.is_open:
                return
            try:
                # No flow control, stated explicitly: on the DLC32 the modem lines drive
                # the ESP32 reset circuit, not a UART handshake (DECISIONS D-037 era notes).
                ser = serial.Serial(self.port, self.baudrate, timeout=self._read_timeout,
                                    write_timeout=self.write_timeout,
                                    rtscts=False, dsrdtr=False, xonxoff=False)
            except (serial.SerialException, OSError, ValueError) as exc:
                raise LinkError(f"cannot open {self.port}: {exc}") from exc
            self._ser = ser

    def close(self) -> None:
        with self._state_lock:
            ser, self._ser = self._ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception as exc:  # noqa: BLE001 - closing must never raise
                log.debug("closing %s: %s", self.port, exc)

    @property
    def is_open(self) -> bool:
        ser = self._ser
        return ser is not None and bool(ser.is_open)

    def write(self, data: bytes) -> None:
        ser = self._ser
        if ser is None:
            raise LinkError(f"{self.port} is not open")
        with self._wlock:
            try:
                n = ser.write(data)
            except serial.SerialTimeoutException as exc:
                raise LinkError(f"write timeout on {self.port}") from exc
            except (serial.SerialException, OSError, ValueError, AttributeError) as exc:
                raise LinkError(f"write failed on {self.port}: {exc}") from exc
        if n is not None and n != len(data):
            raise LinkError(f"short write on {self.port}: {n}/{len(data)} bytes")

    def read(self, timeout: float = 0.05) -> bytes:
        ser = self._ser
        if ser is None:
            raise LinkError(f"{self.port} is not open")
        try:
            if timeout != self._read_timeout:
                self._read_timeout = timeout
                ser.timeout = timeout
            data = ser.read(1)
            if data:
                waiting = ser.in_waiting
                if waiting:
                    data += ser.read(waiting)
            return data
        except (serial.SerialException, OSError, ValueError, TypeError, AttributeError) as exc:
            raise LinkError(f"read failed on {self.port}: {exc}") from exc
