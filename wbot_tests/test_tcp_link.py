"""TcpLink semantics against a local socket server (no board, no internet)."""
from __future__ import annotations

import socket
import threading
import time

import pytest

from wbot.fluidnc.link import LinkError
from wbot.fluidnc.tcp_link import IAC, IacFilter, TcpLink


class Peer:
    """Local TCP server accepting one connection; records what the link sends."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.conn: socket.socket | None = None
        self.received = bytearray()
        self._lock = threading.Lock()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        try:
            self.conn, _ = self.srv.accept()
        except OSError:
            return
        self.conn.settimeout(0.05)
        while True:
            try:
                d = self.conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            if not d:
                return
            with self._lock:
                self.received += d

    def wait_conn(self):
        for _ in range(200):
            if self.conn is not None:
                return self.conn
            time.sleep(0.01)
        raise AssertionError("no client connected")

    def data(self) -> bytes:
        with self._lock:
            return bytes(self.received)

    def wait_data(self, n: int, timeout=2.0) -> bytes:
        end = time.time() + timeout
        while time.time() < end and len(self.data()) < n:
            time.sleep(0.01)
        return self.data()

    def close(self):
        for s in (self.conn, self.srv):
            try:
                s and s.close()
            except OSError:
                pass


@pytest.fixture()
def peer():
    p = Peer()
    yield p
    p.close()


def read_until(link, n, timeout=2.0):
    out = b""
    end = time.time() + timeout
    while len(out) < n and time.time() < end:
        out += link.read(0.05)
    return out


def test_roundtrip_and_socket_options(peer):
    link = TcpLink("127.0.0.1", peer.port)
    link.open()
    try:
        assert link.is_open and link.name == f"tcp:127.0.0.1:{peer.port}"
        s = link._sock
        assert s.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
        assert s.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        link.write(b"?")
        assert peer.wait_data(1) == b"?"
        peer.wait_conn().sendall(b"<Idle|MPos:0,0>\r\nok\r\n")
        assert read_until(link, 22) == b"<Idle|MPos:0,0>\r\nok\r\n"
        assert link.read(0.02) == b""                       # timeout -> b"", not an error
    finally:
        link.close()


def test_iac_sequences_are_filtered_and_refused(peer):
    link = TcpLink("127.0.0.1", peer.port)
    link.open()
    try:
        conn = peer.wait_conn()
        conn.sendall(bytes([IAC, 251, 1]) + b"ok" + bytes([IAC, 253, 3]) + b"\r\n"
                     + bytes([IAC, 250, 24, 1, IAC, 240]) + b"x")
        assert read_until(link, 5) == b"ok\r\nx"
        reply = peer.wait_data(6)
        assert reply == bytes([IAC, 254, 1, IAC, 252, 3])        # DONT 1, WONT 3
    finally:
        link.close()


def test_iac_split_across_chunks_and_literal_ff():
    f = IacFilter()
    out = b""
    for chunk in (b"a" + bytes([IAC]), bytes([251]), bytes([1]) + b"b", bytes([IAC, IAC]) + b"c",
                  bytes([IAC, 250, 31]), bytes([IAC]), bytes([240]) + b"d"):
        clean, _ = f.feed(chunk)
        out += clean
    assert out == b"ab\xffcd"


def test_peer_close_raises_linkerror_and_close_is_idempotent(peer):
    link = TcpLink("127.0.0.1", peer.port)
    link.open()
    peer.wait_conn().close()
    with pytest.raises(LinkError):
        for _ in range(40):
            link.read(0.05)
    assert not link.is_open
    with pytest.raises(LinkError):
        link.write(b"?")
    link.close()
    link.close()


def test_use_before_open_and_reopen(peer):
    link = TcpLink("127.0.0.1", peer.port)
    with pytest.raises(LinkError):
        link.write(b"x")
    with pytest.raises(LinkError):
        link.read(0.01)
    link.close()                                                # never opened: still fine
    link.open()
    link.open()                                                 # second open is a no-op
    link.close()
    assert not link.is_open


def test_connection_refused_is_linkerror_with_hint():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                                   # nobody listens there now
    link = TcpLink("127.0.0.1", port, connect_timeout=1.0)
    with pytest.raises(LinkError) as ei:
        link.open()
    assert f"127.0.0.1:{port}" in str(ei.value) and not link.is_open


def test_unresolvable_host_gives_clear_error_with_mdns_hint(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror(11001, "getaddrinfo failed")
    monkeypatch.setattr(socket, "getaddrinfo", boom)            # no real DNS/mDNS query in tests
    with pytest.raises(LinkError) as ei:
        TcpLink("fluidnc.local", 23).open()
    msg = str(ei.value)
    assert "fluidnc.local" in msg and "mDNS" in msg and "IP" in msg


def test_concurrent_writes_do_not_interleave(peer):
    link = TcpLink("127.0.0.1", peer.port)
    link.open()
    try:
        lines = [(f"G1 X{i} Y{j}\n").encode() * 1 for i in range(4) for j in range(50)]
        chunks = [lines[i::4] for i in range(4)]

        def work(c):
            for ln in c:
                link.write(ln)
        ts = [threading.Thread(target=work, args=(c,)) for c in chunks]
        [t.start() for t in ts]
        [t.join() for t in ts]
        total = sum(len(x) for x in lines)
        got = peer.wait_data(total)
        assert sorted(got.split(b"\n")[:-1]) == sorted(x.strip() for x in lines)
    finally:
        link.close()


def test_reconnect_after_drop(peer):
    link = TcpLink("127.0.0.1", peer.port)
    link.open()
    peer.wait_conn().close()
    with pytest.raises(LinkError):
        for _ in range(40):
            link.read(0.05)
    peer2 = Peer()
    try:
        link2 = TcpLink("127.0.0.1", peer2.port)
        link2.open()
        link2.write(b"$I\n")
        assert peer2.wait_data(3) == b"$I\n"
        link2.close()
    finally:
        peer2.close()
