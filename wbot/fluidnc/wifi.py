"""Choose the WiFi link for a board: telnet (raw TCP) or websocket."""
from __future__ import annotations

from .link import Link, LinkError

PROTOCOLS = ("telnet", "websocket")
DEFAULT_PORTS = {"telnet": 23, "websocket": 81}


def normalize_protocol(protocol: str | None) -> str:
    p = (protocol or "telnet").strip().lower()
    if p in ("ws", "websocket"):
        return "websocket"
    if p in ("tcp", "telnet"):
        return "telnet"
    raise LinkError(f"unknown WiFi protocol '{protocol}' (telnet or websocket)")


def default_port(protocol: str | None) -> int:
    return DEFAULT_PORTS[normalize_protocol(protocol)]


def make_board_link(protocol: str | None, host: str, port: int | None = None,
                    connect_timeout: float = 5.0) -> Link:
    """Unopened link to a FluidNC board over WiFi. ``port=None`` picks 23 (telnet) / 81 (websocket)."""
    proto = normalize_protocol(protocol)
    host = (host or "").strip()
    if not host:
        raise LinkError("no host specified for the WiFi connection")
    p = int(port) if port else DEFAULT_PORTS[proto]
    if not 1 <= p <= 65535:
        raise LinkError(f"invalid port {p}")
    if proto == "websocket":
        from .ws_link import WsLink
        return WsLink(host, p, connect_timeout)
    from .tcp_link import TcpLink
    return TcpLink(host, p, connect_timeout)
