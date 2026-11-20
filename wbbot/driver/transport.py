"""Byte transports to the controller: serial, websocket, simulator.

All three expose the same tiny async interface so nothing above this module
ever has to know whether it is talking to a real ESP32 over USB, to the same
ESP32 over WiFi, or to :mod:`wbbot.driver.simulator`.

The interface is deliberately byte-oriented rather than line-oriented: GRBL's
realtime commands (``?``, ``!``, ``~``, ``0x18``, ``0x85``) are single bytes
that must bypass any line buffering, and the character-counting streamer needs
to know exactly how many bytes it has put on the wire.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

__all__ = [
    "TransportError",
    "ConnectionLost",
    "Transport",
    "SerialTransport",
    "WebSocketTransport",
    "SimulatorTransport",
    "make_transport",
]


class TransportError(RuntimeError):
    """Transport could not be opened or used."""


class ConnectionLost(TransportError):
    """The link dropped while the machine may still have been moving."""


class Transport(ABC):
    """Async byte pipe to the controller."""

    def __init__(self) -> None:
        self._connected = False
        self._on_disconnect: Callable[[str], None] | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @abstractmethod
    async def open(self) -> None:
        """Establish the link. Raises :class:`TransportError` on failure."""

    @abstractmethod
    async def close(self) -> None:
        """Close the link. Must be safe to call twice."""

    # -- io ----------------------------------------------------------------

    @abstractmethod
    async def send_bytes(self, data: bytes) -> None:
        """Write raw bytes."""

    @abstractmethod
    async def read(self, timeout: float | None = None) -> bytes:
        """Read whatever has arrived.

        Returns ``b""`` on timeout.  Raises :class:`ConnectionLost` when the
        link is gone — never returns silently in that case, because a silent
        return would let a job keep streaming into the void.
        """

    async def send_line(self, line: str) -> int:
        """Send one command line. Returns the number of bytes written."""
        payload = (line.rstrip("\r\n") + "\n").encode("ascii", "replace")
        await self.send_bytes(payload)
        return len(payload)

    # -- notifications -----------------------------------------------------

    def set_disconnect_handler(self, handler: Callable[[str], None] | None) -> None:
        self._on_disconnect = handler

    def _notify_disconnect(self, reason: str) -> None:
        self._connected = False
        if self._on_disconnect is not None:
            try:
                self._on_disconnect(reason)
            except Exception:  # pragma: no cover - handler must not break us
                log.exception("disconnect handler raised")

    @property
    def description(self) -> str:
        return type(self).__name__


# ==========================================================================
# serial
# ==========================================================================

class SerialTransport(Transport):
    """USB serial link via pyserial, driven from a reader thread.

    pyserial is blocking, so a dedicated daemon thread pumps bytes into an
    asyncio queue; writes go through ``asyncio.to_thread``.
    """

    def __init__(self, port: str, baudrate: int = 115200,
                 rx_buffer_bytes: int = 128) -> None:
        super().__init__()
        if not port:
            raise TransportError("serial transport needs a port (e.g. COM5)")
        self.port = port
        self.baudrate = baudrate
        self.rx_buffer_bytes = rx_buffer_bytes
        self._serial = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def description(self) -> str:
        return f"serial {self.port} @ {self.baudrate}"

    async def open(self) -> None:
        try:
            import serial  # noqa: PLC0415 - optional at import time
        except ImportError as exc:  # pragma: no cover
            raise TransportError("pyserial is not installed") from exc
        try:
            self._serial = await asyncio.to_thread(
                serial.Serial, self.port, self.baudrate, timeout=0.05,
                write_timeout=2.0)
        except Exception as exc:
            raise TransportError(
                f"cannot open serial port {self.port}: {exc}") from exc
        self._loop = asyncio.get_running_loop()
        self._stop.clear()
        self._connected = True
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name="serial-reader")
        # FluidNC prints its banner on connect; give it a moment and flush.
        await asyncio.sleep(0.05)

    async def _reader_loop(self) -> None:
        assert self._serial is not None
        try:
            while not self._stop.is_set():
                try:
                    data = await asyncio.to_thread(self._read_available)
                except Exception as exc:
                    self._notify_disconnect(f"serial read failed: {exc}")
                    await self._queue.put(None)
                    return
                if data:
                    await self._queue.put(data)
                else:
                    await asyncio.sleep(0.005)
        except asyncio.CancelledError:  # pragma: no cover
            raise

    def _read_available(self) -> bytes:
        ser = self._serial
        if ser is None:
            return b""
        n = ser.in_waiting
        if n:
            return ser.read(n)
        return ser.read(1)          # blocks up to the 50 ms timeout

    async def close(self) -> None:
        self._stop.set()
        self._connected = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._serial is not None:
            try:
                await asyncio.to_thread(self._serial.close)
            except Exception:  # pragma: no cover
                pass
            self._serial = None

    async def send_bytes(self, data: bytes) -> None:
        if not self._connected or self._serial is None:
            raise ConnectionLost("serial port is not open")
        try:
            await asyncio.to_thread(self._serial.write, data)
        except Exception as exc:
            self._notify_disconnect(f"serial write failed: {exc}")
            raise ConnectionLost(f"serial write failed: {exc}") from exc

    async def read(self, timeout: float | None = None) -> bytes:
        try:
            if timeout is None:
                item = await self._queue.get()
            else:
                item = await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            if not self._connected:
                raise ConnectionLost("serial link closed") from None
            return b""
        if item is None:
            raise ConnectionLost("serial link closed")
        return item


# ==========================================================================
# websocket
# ==========================================================================

class WebSocketTransport(Transport):
    """FluidNC WiFi link (ESP3D websocket, default port 81).

    FluidNC mixes plain G-code responses with ESP3D ``[MSG:...]`` frames on
    this socket and may send either text or binary frames, so both are decoded
    the same way.  Verified against FluidNC 3.7; older builds differ — see
    ``DECISIONS.md``, D-009.
    """

    def __init__(self, host: str, port: int = 81, path: str = "/") -> None:
        super().__init__()
        if not host:
            raise TransportError("websocket transport needs a host")
        self.host = host
        self.port = port
        self.path = path
        self._ws = None
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._reader_task: asyncio.Task | None = None

    @property
    def uri(self) -> str:
        return f"ws://{self.host}:{self.port}{self.path}"

    @property
    def description(self) -> str:
        return f"websocket {self.uri}"

    async def open(self) -> None:
        try:
            import websockets  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise TransportError("the websockets package is not installed") from exc
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(self.uri, subprotocols=["arduino"],
                                   open_timeout=5, ping_interval=20),
                timeout=8.0)
        except Exception as exc:
            raise TransportError(f"cannot connect to {self.uri}: {exc}") from exc
        self._connected = True
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name="ws-reader")

    async def _reader_loop(self) -> None:
        try:
            async for message in self._ws:  # type: ignore[union-attr]
                data = message.encode("utf-8", "replace") \
                    if isinstance(message, str) else bytes(message)
                await self._queue.put(data)
        except asyncio.CancelledError:  # pragma: no cover
            raise
        except Exception as exc:
            self._notify_disconnect(f"websocket closed: {exc}")
        finally:
            await self._queue.put(None)

    async def close(self) -> None:
        self._connected = False
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # pragma: no cover
                pass
            self._ws = None

    async def send_bytes(self, data: bytes) -> None:
        if not self._connected or self._ws is None:
            raise ConnectionLost("websocket is not open")
        try:
            await self._ws.send(data)
        except Exception as exc:
            self._notify_disconnect(f"websocket write failed: {exc}")
            raise ConnectionLost(f"websocket write failed: {exc}") from exc

    async def read(self, timeout: float | None = None) -> bytes:
        try:
            if timeout is None:
                item = await self._queue.get()
            else:
                item = await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            if not self._connected:
                raise ConnectionLost("websocket closed") from None
            return b""
        if item is None:
            raise ConnectionLost("websocket closed")
        return item


# ==========================================================================
# simulator
# ==========================================================================

class SimulatorTransport(Transport):
    """In-process link to :class:`wbbot.driver.simulator.FluidNCSimulator`."""

    def __init__(self, simulator) -> None:
        super().__init__()
        self.simulator = simulator

    @property
    def description(self) -> str:
        return "simulator"

    async def open(self) -> None:
        await self.simulator.start()
        self.simulator.set_disconnect_handler(self._notify_disconnect)
        self._connected = True

    async def close(self) -> None:
        self._connected = False
        await self.simulator.stop()

    async def send_bytes(self, data: bytes) -> None:
        if not self._connected:
            raise ConnectionLost("simulator is not running")
        if not self.simulator.link_up:
            self._notify_disconnect("simulated link failure")
            raise ConnectionLost("simulated link failure")
        await self.simulator.feed(data)

    async def read(self, timeout: float | None = None) -> bytes:
        try:
            if timeout is None:
                item = await self.simulator.output.get()
            else:
                item = await asyncio.wait_for(self.simulator.output.get(), timeout)
        except asyncio.TimeoutError:
            if not self._connected:
                raise ConnectionLost("simulator stopped") from None
            return b""
        if item is None:
            raise ConnectionLost("simulated link failure")
        return item


# ==========================================================================

def make_transport(cfg, simulator=None) -> Transport:
    """Build the transport named by ``cfg.connection.transport``."""
    conn = cfg.connection
    if conn.transport == "simulator":
        if simulator is None:
            from .simulator import FluidNCSimulator  # noqa: PLC0415 - cycle
            simulator = FluidNCSimulator(cfg)
        return SimulatorTransport(simulator)
    if conn.transport == "serial":
        if not conn.serial.port:
            raise TransportError(
                "connection.transport is 'serial' but connection.serial.port "
                "is not set in machine.yaml")
        return SerialTransport(conn.serial.port, conn.serial.baudrate,
                               conn.serial.rx_buffer_bytes)
    if conn.transport == "websocket":
        if not conn.websocket.host:
            raise TransportError(
                "connection.transport is 'websocket' but "
                "connection.websocket.host is not set in machine.yaml")
        return WebSocketTransport(conn.websocket.host, conn.websocket.port)
    raise TransportError(f"unknown transport {conn.transport!r}")
