"""WebSocket :class:`Link` for FluidNC's WebUI socket (port 81 on v3.x, UNVERIFIED for v3.9.9).

The socket carries the same text as the serial port. Besides that, the WebUI server may send
housekeeping text frames (``PING:...``, ``CURRENT_ID:...``, ``ACTIVE_ID:...``) which are filtered
out here and never reach the line parser. Host -> board: pure ASCII goes out as a text frame,
anything with bytes >= 0x80 (e.g. the 0x85 jog-cancel realtime byte) as a binary frame, so the
byte value arrives unchanged.

Uses the sync client of the ``websockets`` package (v12+; developed against 17.0).
"""
from __future__ import annotations

import logging
import re
import threading

from .link import Link, LinkError
from .tcp_link import open_socket

try:  # pragma: no cover - import guard only
    from websockets.exceptions import ConnectionClosed, WebSocketException
    from websockets.sync.client import connect as _ws_connect
except ImportError:  # pragma: no cover
    _ws_connect = None  # type: ignore[assignment]
    ConnectionClosed = WebSocketException = Exception  # type: ignore[assignment,misc]

log = logging.getLogger(__name__)

__all__ = ["WsLink", "is_housekeeping", "DEFAULT_WS_PORT"]

DEFAULT_WS_PORT = 81
_HOUSEKEEPING = re.compile(rb"^\s*(PING|CURRENT_ID|ACTIVE_ID|currentID|activeID)\s*:", re.IGNORECASE)


def is_housekeeping(frame: bytes) -> bool:
    """True for WebUI bookkeeping frames that are not controller output."""
    return _HOUSEKEEPING.match(frame) is not None


class WsLink(Link):
    def __init__(self, host: str, port: int = DEFAULT_WS_PORT, connect_timeout: float = 5.0,
                 subprotocols: list[str] | None = None):
        self.host, self.port, self.connect_timeout = host, int(port), float(connect_timeout)
        self.subprotocols = subprotocols
        self.name = f"ws:{host}:{self.port}"
        self._ws = None
        self._wlock = threading.Lock()
        self._state_lock = threading.Lock()

    def open(self) -> None:
        if _ws_connect is None:  # pragma: no cover
            raise LinkError("the 'websockets' package is not installed")
        with self._state_lock:
            if self._ws is not None:
                return
            sock = open_socket(self.host, self.port, self.connect_timeout)
            try:
                sock.settimeout(None)
                host = f"[{self.host}]" if ":" in self.host else self.host
                self._ws = _ws_connect(
                    f"ws://{host}:{self.port}/", sock=sock, open_timeout=self.connect_timeout,
                    subprotocols=self.subprotocols, compression=None, proxy=None,
                    ping_interval=None, close_timeout=1.0, max_size=1 << 20)
            except (OSError, WebSocketException, TimeoutError, ValueError) as exc:
                try:
                    sock.close()
                except OSError:
                    pass
                raise LinkError(f"WebSocket handshake with {self.host}:{self.port} failed: {exc}") from exc

    def close(self) -> None:
        with self._state_lock:
            ws, self._ws = self._ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception as exc:  # noqa: BLE001 - closing must never raise
                log.debug("closing %s: %s", self.name, exc)

    @property
    def is_open(self) -> bool:
        return self._ws is not None

    def _fail(self, ws, what: str, exc: BaseException | None) -> LinkError:
        with self._state_lock:
            if self._ws is ws:
                self._ws = None
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
        return LinkError(f"{what} on {self.name}" + (f": {exc}" if exc else ""))

    def write(self, data: bytes) -> None:
        ws = self._ws
        if ws is None:
            raise LinkError(f"{self.name} is not open")
        with self._wlock:
            try:
                ws.send(data.decode("ascii") if data.isascii() else bytes(data))
            except (ConnectionClosed, OSError, WebSocketException, ValueError) as exc:
                raise self._fail(ws, "write failed", exc) from exc

    def _take(self, msg) -> bytes:
        data = msg.encode("utf-8") if isinstance(msg, str) else bytes(msg)
        return b"" if is_housekeeping(data) else data

    def read(self, timeout: float = 0.05) -> bytes:
        ws = self._ws
        if ws is None:
            raise LinkError(f"{self.name} is not open")
        out = bytearray()
        try:
            try:
                out += self._take(ws.recv(timeout=max(timeout, 0.0)))
            except TimeoutError:
                return b""
            while len(out) < 65536:            # drain what is already queued
                try:
                    out += self._take(ws.recv(timeout=0))
                except TimeoutError:
                    break
        except (ConnectionClosed, OSError, WebSocketException, ValueError) as exc:
            if out:                            # deliver what we have; the next read reports the loss
                return bytes(out)
            raise self._fail(ws, "connection closed by the board", exc) from exc
        return bytes(out)
