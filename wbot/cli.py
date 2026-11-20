"""Command line: ``python -m wbot serve [--sim | --port COM3]``."""
from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="wbot", description="W-BOT Wandplotter")
    ap.add_argument("--config", help="path to wbot.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the web UI and the API")
    s.add_argument("--host", default=None)
    s.add_argument("--http-port", type=int, default=None, help="HTTP port (default 8000)")
    s.add_argument("--connect", choices=["sim", "serial", "wifi"], help="connect immediately")
    s.add_argument("--port", help="COM port for --connect serial (never open COM3 unintentionally!)")
    _wifi_args(s)
    s.add_argument("-v", "--verbose", action="store_true")

    pr = sub.add_parser("probe", help="READ ONLY: query the controller (version, state, axes, $CD comparison), no motion")
    pr.add_argument("--port", help="COM port, e.g. COM3 (must be free!)")
    _wifi_args(pr)
    ss = sub.add_parser("sim-server", help="fake board on the network: emulator behind Telnet/WebSocket (test without hardware)")
    ss.add_argument("--bind", default="127.0.0.1", help="bind address (default 127.0.0.1; use 0.0.0.0 only on purpose)")
    ss.add_argument("--tcp-port", type=int, default=2323)
    ss.add_argument("--ws-port", type=int, default=2324, help="0 = no WebSocket")
    ss.add_argument("--speed", type=float, default=1.0, help="emulator time-lapse factor")
    ss.add_argument("--physical-start", type=float, nargs=2, metavar=("X", "Y"), help="where the gondola physically hangs")
    wc = sub.add_parser("wifi-commands", help="OUTPUT ONLY: FluidNC console commands to bring the board onto WiFi")
    wc.add_argument("--ssid", required=True)
    wc.add_argument("--hostname", default="wbot")
    sub.add_parser("ports", help="list serial ports")
    sub.add_parser("firmware-config", help="generate and print the FluidNC config.yaml")
    mc = sub.add_parser("mcp", help="MCP server (stdio) for AI control; forwards to a running 'wbot serve'")
    mc.add_argument("--api", default=None, help="address of the running server (default http://127.0.0.1:8000)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config(args.config)

    if args.cmd == "ports":
        from .fluidnc.serial_link import list_ports
        for p in list_ports():
            print(f"{p['device']}\t{p.get('description', '')}")
        return 0
    if args.cmd == "wifi-commands":
        from .wifi_setup import wifi_commands
        try:
            sys.stdout.write(wifi_commands(args.ssid, args.hostname))
        except ValueError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
        return 0
    if args.cmd == "mcp":                     # stdio: stdout carries only MCP frames, logs go to stderr
        from .mcp_server import run_stdio
        h = "127.0.0.1" if cfg.api.host in ("0.0.0.0", "::", "") else cfg.api.host
        run_stdio(args.api or f"http://{h}:{cfg.api.port}")
        return 0
    if args.cmd == "sim-server":
        return _sim_server(cfg, args)
    if args.cmd == "probe":
        if bool(args.port) == bool(args.board_host):
            print("Error: specify exactly one of --port (serial) or --board-host (WiFi).", file=sys.stderr)
            return 2
        return _probe(cfg, args.port, args.board_host, args.board_port, args.protocol)
    if args.cmd == "firmware-config":
        from .fluidnc_config import generate_firmware_config
        sys.stdout.write(generate_firmware_config(cfg))
        return 0

    import uvicorn
    from .api.app import create_app
    from .machine import Machine
    machine = Machine(cfg, config_path=args.config, persist=True)
    app = create_app(machine)
    if args.connect:
        import threading
        if args.connect == "wifi":
            threading.Timer(1.0, lambda: machine.connect("wifi", args.board_port, host=args.board_host,
                                                         protocol=args.protocol)).start()
        else:
            threading.Timer(1.0, lambda: machine.connect(args.connect, args.port)).start()
    host = args.host or cfg.api.host
    port = args.http_port or cfg.api.port
    print(f"W-BOT running at http://{host}:{port}/  (the simulator is the default - real hardware only with --connect serial --port COMx)")
    uvicorn.run(app, host=host, port=port, log_level="warning")
    return 0


def _wifi_args(p) -> None:
    """Board-side WiFi options. (``--host`` already means the HTTP bind address of ``serve``.)"""
    p.add_argument("--board-host", help="WiFi: host/IP of the board, e.g. fluidnc.local (never a real board unintentionally)")
    p.add_argument("--board-port", type=int, help="WiFi: TCP port (default 23 Telnet / 81 WebSocket)")
    p.add_argument("--protocol", choices=["telnet", "websocket"], help="WiFi protocol (default telnet)")


def _sim_server(cfg, args) -> int:
    import time
    from .sim.netserver import FakeBoard
    board = FakeBoard(cfg, bind=args.bind, tcp_port=args.tcp_port, ws_port=args.ws_port or None,
                      speed=args.speed, physical_start=args.physical_start)
    board.start()
    ws = f", WebSocket {args.bind}:{board.ws_port}" if board.ws_port else ""
    print(f"Fake board running: Telnet {args.bind}:{board.tcp_port}{ws}  (Ctrl+C to stop)")
    print(f"  UI/Server:  python -m wbot serve --connect wifi --board-host {args.bind} --board-port {board.tcp_port}")
    if args.bind not in ("127.0.0.1", "localhost", "::1"):
        print("  WARNING: reachable from the network, WITHOUT authentication.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        board.stop()
    return 0


def _probe(cfg, port: str | None, board_host: str | None = None, board_port: int | None = None,
           protocol: str | None = None) -> int:
    """Read-only smoke test of the real controller. Sends only $I, $CD and realtime '?'."""
    from .fluidnc.client import FluidNCClient
    if board_host:
        from .fluidnc.wifi import make_board_link
        w = cfg.connection.wifi
        link = make_board_link(protocol or w.protocol, board_host, board_port)
    else:
        from .fluidnc.serial_link import SerialLink
        link = SerialLink(port, cfg.connection.serial.baudrate)
    from .fluidnc_config import has_errors, verify_dump
    client = FluidNCClient(link, status_hz=2.0, watchdog_s=8.0 if board_host else 5.0)
    info = client.connect(timeout=8.0)
    print(f"Firmware : {info.firmware}")
    print(f"Banner   : {info.banner}")
    print(f"State    : {info.machine_state}")
    print(f"Position : {info.mpos}  ({len(info.mpos or ())} axes)")
    for ln in client.query("$I", timeout=5.0):
        print("  $I>", ln)
    dump = "\n".join(client.query("$CD", timeout=10.0))
    issues = verify_dump(cfg, dump)
    for i in issues:
        print(f"[{i.severity}] {i.key}: {i.message_de}")
    print("Config comparison:", "ERROR" if has_errors(issues) else "ok")
    client.close()
    return 1 if has_errors(issues) else 0


if __name__ == "__main__":
    raise SystemExit(main())
