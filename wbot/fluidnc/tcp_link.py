"""TCP (telnet-style) :class:`Link` for FluidNC over WiFi (raw TCP, port 23 by default).

FluidNC's Telnet server speaks the same line protocol and the same single-byte realtime
commands (``? ! ~ 0x18 0x85``) as the serial port. Differences that matter here:

* connecting does NOT reset the board (no DTR), so no boot banner appears;
* a real ``telnet`` client may send IAC negotiation sequences; they are filtered from the data
  and answered with a polite refusal so the peer never waits for us;
* a WiFi link can stall for a moment: TCP_NODELAY (realtime bytes must not be coalesced) and
  keepalive (a powered-off board is noticed even when we are not writing) are set.

Every socket error becomes a :class:`LinkError`; ``close()`` is idempotent.
"""
from __future__ import annotations

import logging
import select
import socket
import sys
import threading

from .link import Link, LinkError

log = logging.getLogger(__name__)

__all__ = ["TcpLink", "IacFilter", "open_socket", "DEFAULT_TELNET_PORT"]

DEFAULT_TELNET_PORT = 23
WRITE_TIMEOUT_S = 2.0

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240


class IacFilter:
    """Stateful telnet IAC stripper (state survives chunk boundaries).

    ``feed(data) -> (clean, replies)``: ``clean`` is the payload without negotiation,
    ``replies`` the bytes to send back (``DONT x`` to ``WILL x``, ``WONT x`` to ``DO x``).
    ``IAC IAC`` is a literal 0xFF byte (RFC 854).
    """

    def __init__(self) -> None:
        self._state = 0          # 0 data, 1 after IAC, 2 after IAC+verb (needs option), 3 in SB, 4 SB+IAC
        self._verb = 0

    def feed(self, data: bytes) -> tuple[bytes, bytes]:
        if IAC not in data and self._state == 0:
            return data, b""
        out = bytearray()
        replies = bytearray()
        for b in data:
            st = self._state
            if st == 0:
                if b == IAC:
                    self._state = 1
                else:
                    out.append(b)
            elif st == 1:
                if b == IAC:
                    out.append(IAC)
                    self._state = 0
                elif b in (DO, DONT, WILL, WONT):
                    self._verb, self._state = b, 2
                elif b == SB:
                    self._state = 3
                else:                      # NOP, GA, AYT, ... : two byte command
                    self._state = 0
            elif st == 2:
                if self._verb == DO:
                    replies += bytes((IAC, WONT, b))
                elif self._verb == WILL:
                    replies += bytes((IAC, DONT, b))
                self._state = 0
            elif st == 3:
                if b == IAC:
                    self._state = 4
            else:                          # st == 4
                self._state = 0 if b == SE else (4 if b == IAC else 3)
        return bytes(out), bytes(replies)


def _resolve(host: str, port: int) -> list[tuple]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        hint = ""
        if host.lower().endswith(".local"):
            hint = (" mDNS-Namen (.local) werden nur aufgeloest, wenn der Rechner mDNS/Bonjour kann "
                    "und Board und PC im selben Netz haengen - sonst die IP-Adresse des Boards verwenden.")
        raise LinkError(f"Hostname '{host}' laesst sich nicht aufloesen ({exc}).{hint}") from exc
    infos.sort(key=lambda i: 0 if i[0] == socket.AF_INET else 1)    # the ESP32 is IPv4-only
    return infos


def _tune(sock: socket.socket) -> None:
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    try:
        if sys.platform == "win32":
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 5000, 2000))     # on, idle 5 s, interval 2 s
        else:
            for name, val in (("TCP_KEEPIDLE", 5), ("TCP_KEEPINTVL", 2), ("TCP_KEEPCNT", 3)):
                opt = getattr(socket, name, None)
                if opt is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, opt, val)
    except (OSError, AttributeError, ValueError) as exc:      # keepalive tuning is best effort
        log.debug("keepalive tuning failed: %s", exc)


def open_socket(host: str, port: int, connect_timeout: float) -> socket.socket:
    """Resolve + connect, tuned for FluidNC. Raises LinkError with a readable message."""
    last: Exception | None = None
    for family, stype, proto, _canon, addr in _resolve(host, port):
        sock = None
        try:
            sock = socket.socket(family, stype, proto)
            sock.settimeout(connect_timeout)
            sock.connect(addr)
            _tune(sock)
            sock.settimeout(WRITE_TIMEOUT_S)
            return sock
        except OSError as exc:
            last = exc
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
    raise LinkError(f"keine Verbindung zu {host}:{port} ({last}). Board eingeschaltet, im WLAN, "
                    f"Telnet/WebSocket in FluidNC aktiv?") from last


class TcpLink(Link):
    def __init__(self, host: str, port: int = DEFAULT_TELNET_PORT, connect_timeout: float = 5.0):
        self.host, self.port, self.connect_timeout = host, int(port), float(connect_timeout)
        self.name = f"tcp:{host}:{self.port}"
        self._sock: socket.socket | None = None
        self._wlock = threading.Lock()
        self._state_lock = threading.Lock()
        self._iac = IacFilter()

    # --- Link ---------------------------------------------------------------------
    def open(self) -> None:
        with self._state_lock:
            if self._sock is not None:
                return
            self._iac = IacFilter()
            self._sock = open_socket(self.host, self.port, self.connect_timeout)

    def close(self) -> None:
        with self._state_lock:
            sock, self._sock = self._sock, None
        if sock is None:
            return
        for fn in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
            try:
                fn()
            except OSError:
                pass

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    def _fail(self, sock: socket.socket, what: str, exc: BaseException | None) -> LinkError:
        with self._state_lock:
            if self._sock is sock:
                self._sock = None
        try:
            sock.close()
        except OSError:
            pass
        return LinkError(f"{what} on {self.name}" + (f": {exc}" if exc else ""))

    def write(self, data: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise LinkError(f"{self.name} is not open")
        with self._wlock:
            try:
                sock.sendall(data)
            except socket.timeout as exc:       # partial send: stream state unknown -> drop link
                raise self._fail(sock, "write timeout", exc) from exc
            except (OSError, ValueError) as exc:
                raise self._fail(sock, "write failed", exc) from exc

    def read(self, timeout: float = 0.05) -> bytes:
        sock = self._sock
        if sock is None:
            raise LinkError(f"{self.name} is not open")
        try:
            ready, _, _ = select.select([sock], [], [], max(timeout, 0.0))
            if not ready:
                return b""
            chunk = sock.recv(4096)
        except (OSError, ValueError) as exc:
            raise self._fail(sock, "read failed", exc) from exc
        if not chunk:
            raise self._fail(sock, "connection closed by the board", None)
        clean, replies = self._iac.feed(chunk)
        if replies:
            self.write(replies)
        return clean
