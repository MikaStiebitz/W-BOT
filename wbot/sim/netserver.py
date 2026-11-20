"""A FAKE FluidNC board on the LAN: one :class:`FluidNCEmulator` behind a raw-TCP (telnet-like)
server and a WebSocket server, so the complete WiFi path (UI -> Machine -> TcpLink/WsLink ->
network -> emulator) can be tested without hardware.

Behaviour that mirrors a powered ESP32 on WiFi:

* the emulator is booted ONCE when the server starts; a client connecting does NOT reset it (no
  DTR) and sees no boot banner, so the host-side position reference survives reconnects;
* one client at a time, the newest connection wins (a dead old connection never locks you out);
* when the client goes away the board keeps executing what it already has queued.

Binds ``127.0.0.1`` by default; ``bind="0.0.0.0"`` must be explicit. No authentication, like
FluidNC itself.
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Mapping, Sequence

from ..config import Config, load_config
from ..fluidnc.link import LinkError
from .emulator import FluidNCEmulator

log = logging.getLogger("wbot.sim.net")

__all__ = ["FakeBoard"]

_WS_HOUSEKEEPING = (b"CURRENT_ID:0", b"ACTIVE_ID:0")


class _Endpoint:
    """One connected client: ``recv`` returns b"" on timeout and raises EOFError when gone."""

    def recv(self, timeout: float) -> bytes: raise NotImplementedError
    def send(self, data: bytes) -> None: raise NotImplementedError
    def close(self) -> None: raise NotImplementedError
    keepalive_frame: bytes | None = None


class _TcpEndpoint(_Endpoint):
    def __init__(self, conn: socket.socket):
        self.conn = conn
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.settimeout(2.0)

    def recv(self, timeout: float) -> bytes:
        try:
            self.conn.settimeout(timeout)
            data = self.conn.recv(4096)
        except socket.timeout:
            return b""
        except OSError as exc:
            raise EOFError(str(exc)) from exc
        if not data:
            raise EOFError("closed")
        return data

    def send(self, data: bytes) -> None:
        try:
            self.conn.settimeout(2.0)
            self.conn.sendall(data)
        except OSError as exc:
            raise EOFError(str(exc)) from exc

    def close(self) -> None:
        for fn in (lambda: self.conn.shutdown(socket.SHUT_RDWR), self.conn.close):
            try:
                fn()
            except OSError:
                pass


class _WsEndpoint(_Endpoint):
    def __init__(self, ws, binary: bool):
        self.ws, self.binary = ws, binary

    def recv(self, timeout: float) -> bytes:
        from websockets.exceptions import ConnectionClosed
        try:
            msg = self.ws.recv(timeout=timeout)
        except TimeoutError:
            return b""
        except (ConnectionClosed, OSError) as exc:
            raise EOFError(str(exc)) from exc
        return msg.encode("utf-8") if isinstance(msg, str) else bytes(msg)

    def send(self, data: bytes) -> None:
        from websockets.exceptions import ConnectionClosed
        try:
            self.ws.send(data if self.binary else data.decode("latin-1"))
        except (ConnectionClosed, OSError) as exc:
            raise EOFError(str(exc)) from exc

    def send_housekeeping(self, frame: bytes) -> None:
        from websockets.exceptions import ConnectionClosed
        try:
            self.ws.send(frame.decode("ascii"))                 # always a TEXT frame
        except (ConnectionClosed, OSError) as exc:
            raise EOFError(str(exc)) from exc

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


class _Session:
    def __init__(self, ep: _Endpoint):
        self.ep = ep
        self.closed = threading.Event()
        self.done = threading.Event()

    def close(self) -> None:
        self.closed.set()
        self.ep.close()


class FakeBoard:
    def __init__(self, cfg: Config | None = None, *, bind: str = "127.0.0.1", tcp_port: int = 2323,
                 ws_port: int | None = 2324, speed: float = 1.0,
                 physical_start: Sequence[float] | None = None,
                 perturb: Mapping[str, object] | None = None, ws_binary: bool = True,
                 ws_housekeeping: bool = True):
        self.cfg = cfg or load_config()
        self.bind, self.tcp_port, self.ws_port = bind, tcp_port, ws_port
        self.ws_binary, self.ws_housekeeping = ws_binary, ws_housekeeping
        start = tuple(physical_start) if physical_start else tuple(self.cfg.firmware.boot_physical)
        self.emu = FluidNCEmulator(self.cfg, speed=speed, physical_start=start,
                                   true_overrides=dict(perturb) if perturb else None)
        self._lock = threading.Lock()
        self._current: _Session | None = None
        self._stop = threading.Event()
        self._tcp_srv: socket.socket | None = None
        self._ws_srv = None
        self._threads: list[threading.Thread] = []
        self.connections = 0

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> "FakeBoard":
        self.emu.open()                                     # the board boots exactly once
        self.emu.read(0)                                    # nobody listens to the boot banner
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.bind, self.tcp_port))
        srv.listen(2)
        srv.settimeout(0.2)
        self.tcp_port = srv.getsockname()[1]
        self._tcp_srv = srv
        self._spawn(self._accept_tcp, "fakeboard-tcp")
        if self.ws_port is not None:
            from websockets.sync.server import serve
            self._ws_srv = serve(self._ws_handler, self.bind, self.ws_port, compression=None,
                                 ping_interval=None, open_timeout=5.0)
            self.ws_port = self._ws_srv.socket.getsockname()[1]
            self._spawn(self._ws_srv.serve_forever, "fakeboard-ws")
        log.info("fake FluidNC board: telnet %s:%s%s", self.bind, self.tcp_port,
                 f", websocket {self.bind}:{self.ws_port}" if self._ws_srv else "")
        return self

    def stop(self) -> None:
        self._stop.set()
        self.drop_client()
        if self._tcp_srv is not None:
            try:
                self._tcp_srv.close()
            except OSError:
                pass
        if self._ws_srv is not None:
            try:
                self._ws_srv.shutdown()
            except Exception:  # noqa: BLE001
                pass
        for t in self._threads:
            t.join(2.0)
        self.emu.close()

    def __enter__(self) -> "FakeBoard":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def _spawn(self, fn, name: str, *args) -> threading.Thread:
        t = threading.Thread(target=fn, args=args, name=name, daemon=True)
        t.start()
        self._threads.append(t)
        return t

    # ------------------------------------------------------------------ fault injection
    @property
    def has_client(self) -> bool:
        return self._current is not None

    def drop_client(self) -> None:
        """Cut the current connection like a WiFi loss; the board itself keeps running."""
        with self._lock:
            sess = self._current
        if sess is not None:
            sess.close()
            sess.done.wait(2.0)

    # ------------------------------------------------------------------ servers
    def _accept_tcp(self) -> None:
        srv = self._tcp_srv
        while not self._stop.is_set():
            try:
                conn, _addr = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._spawn(self._run_session, "fakeboard-tcp-client", _TcpEndpoint(conn))

    def _ws_handler(self, ws) -> None:
        self._run_session(_WsEndpoint(ws, self.ws_binary))

    # ------------------------------------------------------------------ session pump
    def _run_session(self, ep: _Endpoint) -> None:
        sess = _Session(ep)
        with self._lock:
            old, self._current = self._current, sess
            self.connections += 1
        if old is not None:                                 # newest connection wins
            old.close()
            old.done.wait(2.0)
        while self.emu.read(0):                             # stale output belongs to nobody
            pass
        sender = threading.Thread(target=self._pump_out, args=(sess,), name="fakeboard-out", daemon=True)
        sender.start()
        try:
            while not sess.closed.is_set() and not self._stop.is_set():
                data = ep.recv(0.1)
                if data:
                    try:
                        self.emu.write(data)
                    except LinkError:
                        break
        except EOFError:
            pass
        finally:
            sess.closed.set()
            ep.close()
            sender.join(1.0)
            with self._lock:
                if self._current is sess:
                    self._current = None
            sess.done.set()

    def _pump_out(self, sess: _Session) -> None:
        ep = sess.ep
        last_hk = 0.0
        sent_ids = False
        try:
            while not sess.closed.is_set() and not self._stop.is_set():
                now = time.monotonic()
                if self.ws_housekeeping and isinstance(ep, _WsEndpoint):
                    if not sent_ids:
                        sent_ids = True
                        for fr in _WS_HOUSEKEEPING:
                            ep.send_housekeeping(fr)
                    if now - last_hk > 1.0:
                        last_hk = now
                        ep.send_housekeeping(b"PING:0")
                try:
                    data = self.emu.read(0.05)
                except LinkError:
                    return
                if data:
                    ep.send(data)
        except EOFError:
            pass
        finally:
            sess.closed.set()
