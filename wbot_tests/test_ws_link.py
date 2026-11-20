"""WsLink semantics against a local websockets server (PING/CURRENT_ID/ACTIVE_ID frames, binary)."""
from __future__ import annotations

import threading
import time

import pytest
from websockets.sync.server import serve

from wbot.fluidnc.link import LinkError
from wbot.fluidnc.wifi import default_port, make_board_link, normalize_protocol
from wbot.fluidnc.ws_link import WsLink, is_housekeeping


class Server:
    def __init__(self, script):
        self.got: list = []
        self.script = script
        self.srv = serve(self._handler, "127.0.0.1", 0, compression=None, ping_interval=None)
        self.port = self.srv.socket.getsockname()[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def _handler(self, ws):
        self.ws = ws
        self.script(self, ws)

    def stop(self):
        self.srv.shutdown()


def read_until(link, n, timeout=3.0):
    out = b""
    end = time.time() + timeout
    while len(out) < n and time.time() < end:
        out += link.read(0.05)
    return out


def hold(srv, ws):
    try:
        for m in ws:
            srv.got.append(m)
    except Exception:  # noqa: BLE001
        pass


@pytest.fixture()
def make_server():
    servers = []

    def f(script):
        s = Server(script)
        servers.append(s)
        return s
    yield f
    for s in servers:
        s.stop()


def test_housekeeping_frames_are_filtered(make_server):
    def script(srv, ws):
        ws.send("CURRENT_ID:42")
        ws.send("ACTIVE_ID:42")
        ws.send("PING:0")
        ws.send(b"<Idle|MPos:1,2,0,0>\r\n")              # binary frame: controller output
        ws.send("ok\r\n")                                 # text frame: controller output
        ws.send("PING:0")
        hold(srv, ws)
    s = make_server(script)
    link = WsLink("127.0.0.1", s.port)
    link.open()
    try:
        assert read_until(link, 24) == b"<Idle|MPos:1,2,0,0>\r\nok\r\n"
    finally:
        link.close()


def test_write_text_for_ascii_binary_for_high_bytes(make_server):
    s = make_server(hold)
    link = WsLink("127.0.0.1", s.port)
    link.open()
    try:
        link.write(b"?")
        link.write(b"G0 X1\n")
        link.write(b"\x85")
        link.write(b"!\x18")
        end = time.time() + 2
        while len(s.got) < 4 and time.time() < end:
            time.sleep(0.01)
        assert s.got == ["?", "G0 X1\n", b"\x85", "!\x18"]
    finally:
        link.close()


def test_server_close_raises_linkerror_and_close_idempotent(make_server):
    def script(srv, ws):
        ws.send(b"ok\r\n")
        ws.close()
    s = make_server(script)
    link = WsLink("127.0.0.1", s.port)
    link.open()
    assert read_until(link, 4) == b"ok\r\n"
    with pytest.raises(LinkError):
        for _ in range(60):
            link.read(0.05)
    assert not link.is_open
    with pytest.raises(LinkError):
        link.write(b"?")
    link.close()
    link.close()


def test_timeout_returns_empty(make_server):
    s = make_server(hold)
    link = WsLink("127.0.0.1", s.port)
    link.open()
    try:
        assert link.read(0.05) == b""
    finally:
        link.close()


def test_refused_and_not_a_websocket(make_server):
    import socket
    sk = socket.socket()
    sk.bind(("127.0.0.1", 0))
    port = sk.getsockname()[1]
    sk.close()
    with pytest.raises(LinkError):
        WsLink("127.0.0.1", port, connect_timeout=1.0).open()
    # a plain TCP server that is no websocket: handshake fails -> LinkError, not a hang
    plain = socket.socket()
    plain.bind(("127.0.0.1", 0))
    plain.listen(1)
    threading.Thread(target=lambda: (plain.accept()[0].sendall(b"Grbl 3.9\r\n")), daemon=True).start()
    with pytest.raises(LinkError):
        WsLink("127.0.0.1", plain.getsockname()[1], connect_timeout=1.0).open()
    plain.close()


def test_is_housekeeping():
    for f in (b"PING:0", b"CURRENT_ID:12", b"ACTIVE_ID:1", b" ping: 5"):
        assert is_housekeeping(f)
    for f in (b"ok\r\n", b"<Idle|MPos:0,0>", b"[MSG:INFO: PING:x]", b"error:3"):
        assert not is_housekeeping(f)


def test_factory_defaults_and_errors():
    assert default_port("telnet") == 23 and default_port("websocket") == 81 and default_port(None) == 23
    assert normalize_protocol("WS") == "websocket"
    assert make_board_link("telnet", "x.example").name == "tcp:x.example:23"
    assert make_board_link("websocket", "x.example").name == "ws:x.example:81"
    assert make_board_link("websocket", "x.example", 8081).name == "ws:x.example:8081"
    for bad in (dict(protocol="smoke", host="h"), dict(protocol="telnet", host=" "),
                dict(protocol="telnet", host="h", port=70000)):
        with pytest.raises(LinkError):
            make_board_link(**bad)
