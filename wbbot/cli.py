"""Command line interface.

    wbbot serve                 start the web UI + API
    wbbot mcp                   start the MCP server on stdio
    wbbot info                  machine summary and safe-area report
    wbbot fluidnc-config        generate the firmware configuration
    wbbot draw photo.jpg        trace an image and plot it
    wbbot preview photo.jpg     trace an image and write an SVG
    wbbot calibrate             run the calibration wizard in the terminal
    wbbot selftest              draw a test pattern through the simulator
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

from .config import ConfigError, load_config

log = logging.getLogger(__name__)

__all__ = ["main"]


def _load(args: argparse.Namespace):
    overrides: dict = {}
    if getattr(args, "port", None):
        overrides.setdefault("connection", {}).setdefault("serial", {})["port"] = args.port
        overrides["connection"]["transport"] = "serial"
    if getattr(args, "host", None):
        overrides.setdefault("connection", {}).setdefault("websocket", {})["host"] = args.host
        overrides["connection"]["transport"] = "websocket"
    if getattr(args, "simulator", False):
        overrides.setdefault("connection", {})["transport"] = "simulator"
    return load_config(getattr(args, "config", None), overrides=overrides or None)


async def _connected_machine(cfg):
    from .driver.machine import Machine
    from .driver.simulator import FluidNCSimulator
    from .driver.transport import SimulatorTransport, make_transport

    simulator = (FluidNCSimulator(cfg)
                 if cfg.connection.transport == "simulator" else None)
    transport = (SimulatorTransport(simulator) if simulator is not None
                 else make_transport(cfg))
    machine = Machine(cfg, transport=transport, simulator=simulator)
    await machine.connect()
    return machine


# ==========================================================================
# commands
# ==========================================================================

def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn
    cfg = _load(args)
    host = args.bind or cfg.api.host
    port = args.http_port or cfg.api.port
    ui = Path(__file__).resolve().parents[1] / "web" / "dist"
    print(f"wbbot on http://{host}:{port}   transport: {cfg.connection.transport}")
    if not ui.is_dir():
        print("note: the web UI is not built yet — run 'cd web && npm install "
              "&& npm run build'", file=sys.stderr)
    from .api.app import create_app
    uvicorn.run(create_app(cfg), host=host, port=port,
                log_level=args.log_level.lower())
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp.server import main as mcp_main
    argv: list[str] = []
    if args.config:
        argv += ["--config", args.config]
    if args.port:
        argv += ["--port", args.port]
    if args.host:
        argv += ["--host", args.host]
    if args.simulator:
        argv += ["--transport", "simulator"]
    return mcp_main(argv)


def cmd_info(args: argparse.Namespace) -> int:
    from .core.kinematics import make_kinematics
    from .core.safearea import SafeArea

    cfg = _load(args)
    kin = make_kinematics(cfg)
    safe = SafeArea(cfg, kin)
    summary = safe.summary()

    if args.json:
        print(json.dumps({"config": cfg.to_dict(), "safe_area": summary},
                         indent=2, default=str))
        return 0

    print(f"Board            {cfg.board.width_mm:.0f} x {cfg.board.height_mm:.0f} mm")
    print(f"Anchors          L {cfg.geometry.anchor_left}  "
          f"R {cfg.geometry.anchor_right}  span "
          f"{cfg.geometry.anchor_distance_mm:.0f} mm")
    print(f"Pulley radius    {cfg.geometry.pulley_pitch_radius_mm} mm "
          f"({cfg.drive.pulley_teeth} teeth, "
          f"{cfg.drive.belt_travel_per_rev_mm:.0f} mm/rev)")
    print(f"Steps per mm     {cfg.drive.nominal_steps_per_mm:.3f} "
          f"(1/{cfg.drive.microsteps} microstepping)")
    print(f"Kinematics       {kin.name}"
          f"{'  + precompensation' if cfg.geometry.precompensate else ''}")
    print(f"Transport        {cfg.connection.transport}")
    print()
    print(f"Safe area        {summary['area_mm2'] / 1e6:.3f} m^2 "
          f"({summary['coverage_percent']:.1f} % of the board)")
    print(f"  bounds         {summary['bounds']}")
    print(f"  error gain     {summary['measured']['error_amplification_min']} .. "
          f"{summary['measured']['error_amplification_max']} "
          f"(limit {summary['limits']['max_error_amplification']})")
    print(f"  belt tension   {summary['measured']['min_tension_n_over_board']} .. "
          f"{summary['measured']['max_tension_n_over_board']} N "
          f"(minimum {summary['limits']['min_tension_n']})")
    print()
    print("Markers          " + ", ".join(
        f"{i + 1}:{c.name} ({c.hex})" for i, c in enumerate(cfg.colors)))
    return 0


def cmd_fluidnc_config(args: argparse.Namespace) -> int:
    from .driver.fluidnc_config import (SCHEMAS, generate_fluidnc_config,
                                        write_fluidnc_config)
    cfg = _load(args)
    if args.schema not in SCHEMAS:
        print(f"unknown schema {args.schema!r}; available: "
              f"{', '.join(sorted(SCHEMAS))}", file=sys.stderr)
        return 2
    if args.output == "-":
        print(generate_fluidnc_config(cfg, args.schema))
    else:
        path = write_fluidnc_config(cfg, args.output, args.schema)
        print(f"written to {path}")
        print("Verify the marked keys against $CD on the controller before "
              "flashing.")
    return 0


def cmd_preview(args: argparse.Namespace) -> int:
    from .imaging.pipeline import PipelineParams, run_pipeline
    cfg = _load(args)
    params = PipelineParams(trace_mode=args.mode, color_mode=args.colors)
    result = run_pipeline(args.image, cfg, params)
    print(result.stats.to_dict()["total_duration_human"], "estimated")
    for line in _describe(result):
        print(line)
    out = Path(args.output or "preview.svg")
    out.write_text(result.preview_svg(cfg), encoding="utf-8")
    print(f"preview written to {out}")
    return 0


def _describe(result) -> list[str]:
    stats = result.stats
    lines = [
        f"  paths       {stats.n_strokes}",
        f"  points      {stats.n_points}",
        f"  draw        {stats.draw_length_mm / 1000:.2f} m",
        f"  travel      {stats.travel_length_mm / 1000:.2f} m "
        f"({stats.travel_ratio * 100:.0f} %)",
        f"  colours     {list(stats.colors_used)} "
        f"({stats.n_color_changes} changes)",
    ]
    lines += [f"  ! {w}" for w in result.warnings]
    return lines


async def _draw(args: argparse.Namespace) -> int:
    from .imaging.pipeline import PipelineParams, run_pipeline
    cfg = _load(args)
    params = PipelineParams(trace_mode=args.mode, color_mode=args.colors)
    result = run_pipeline(args.image, cfg, params)
    for line in _describe(result):
        print(line)
    if result.toolpath.is_empty:
        print("nothing to draw", file=sys.stderr)
        return 1

    if not args.yes:
        answer = input(f"Draw this? It will take about "
                       f"{result.stats.to_dict()['total_duration_human']}. [y/N] ")
        if answer.strip().lower() not in ("y", "yes", "j", "ja"):
            print("cancelled")
            return 0

    machine = await _connected_machine(cfg)
    from .driver.machine import MachineState
    try:
        await machine.run_toolpath(result.toolpath, title=Path(args.image).name)
        last = -1
        while machine.state in (MachineState.RUNNING, MachineState.PAUSED):
            await asyncio.sleep(0.5)
            percent = int(machine.progress.percent)
            if percent != last:
                last = percent
                print(f"\r  {percent:3d} %  "
                      f"eta {machine.progress.eta_s / 60:5.1f} min", end="",
                      flush=True)
        print()
        if machine.state is MachineState.IDLE:
            print("done")
            return 0
        print(f"stopped in state {machine.state.value}: "
              f"{machine.last_error}", file=sys.stderr)
        return 1
    finally:
        await machine.disconnect()


def cmd_draw(args: argparse.Namespace) -> int:
    return asyncio.run(_draw(args))


async def _selftest(args: argparse.Namespace) -> int:
    """Draw a known pattern through the simulator and verify the result."""
    import numpy as np
    from .core.geometry import Drawing, Layer, Polyline, douglas_peucker
    from .core.gcode import GCodeWriter
    from .core.toolpath import Toolpath

    cfg = _load(args)
    machine = await _connected_machine(cfg)
    simulator = machine.simulator
    try:
        square = Polyline.from_points(
            [[300, 300], [700, 300], [700, 700], [300, 700]], closed=True)
        triangle = Polyline.from_points(
            [[900, 300], [1300, 300], [1100, 700]], closed=True)
        toolpath = Toolpath.from_drawing(
            Drawing([Layer(0, [square]), Layer(1, [triangle])]))
        program = GCodeWriter(cfg).render_toolpath(toolpath, title="selftest")
        print(f"program: {len(program.lines)} lines, "
              f"{program.stats.to_dict()['total_duration_human']} estimated")

        from .driver.machine import MachineState
        await machine.run_toolpath(toolpath, title="selftest")
        last = -1
        while machine.state in (MachineState.RUNNING, MachineState.PAUSED):
            await asyncio.sleep(0.2)
            percent = int(machine.progress.percent)
            if percent != last:
                last = percent
                print(f"\r  {percent:3d} %", end="", flush=True)
        print()

        if simulator is None:
            print("connected to real hardware; skipping geometry verification")
            return 0
        polylines = simulator.drawn_polylines()
        print(f"simulator drew {len(polylines)} polyline(s)")
        ok = len(polylines) == 2 and not simulator.pen_down
        for pts in polylines:
            corners = douglas_peucker(np.array(pts), 0.5)
            print(f"  {len(pts):4d} points -> {len(corners)} corners")
        print("PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        await machine.disconnect()


def cmd_selftest(args: argparse.Namespace) -> int:
    return asyncio.run(_selftest(args))


async def _calibrate(args: argparse.Namespace) -> int:
    from .calib.wizard import CalibrationWizard
    from .driver.machine import MachineState

    cfg = _load(args)
    machine = await _connected_machine(cfg)
    wizard = CalibrationWizard(cfg, machine.safe_area)
    try:
        wizard.begin()
        print("Step 1 — belt reference (optional but recommended).")
        answer = input("  Measure both belt lengths with a tape? [y/N] ")
        if answer.strip().lower() in ("y", "yes", "j", "ja"):
            x = float(input("  gondola X (mm): "))
            y = float(input("  gondola Y (mm): "))
            await machine.move_to(x, y)
            left = float(input("  measured left belt (mm): "))
            right = float(input("  measured right belt (mm): "))
            wizard.set_reference((x, y), left, right)
        else:
            wizard.skip_reference()

        pattern = wizard.build_pattern(n_x=args.grid, n_y=args.grid)
        print(f"\nStep 2 — drawing {len(pattern.targets)} targets.")
        await machine.run_toolpath(wizard.toolpath, title="calibration")
        last = -1
        while machine.state in (MachineState.RUNNING, MachineState.PAUSED):
            await asyncio.sleep(0.5)
            percent = int(machine.progress.percent)
            if percent != last:
                last = percent
                print(f"\r  {percent:3d} %", end="", flush=True)
        print("\n")
        for line in pattern.instructions():
            print(line)

        print("\nStep 3 — enter the measured positions.")
        for target in pattern.targets:
            print(f"  target {target.index} "
                  f"(nominal {target.nominal[0]:.0f}, {target.nominal[1]:.0f} mm)")
            mx = input("    measured X (blank to skip): ").strip()
            if not mx:
                continue
            my = input("    measured Y: ").strip()
            wizard.record(target.index, float(mx), float(my))

        print("\nStep 4 — fitting.")
        result = wizard.solve()
        print(result.report())
        answer = input("\nSave to calibration.yaml? [y/N] ")
        if answer.strip().lower() in ("y", "yes", "j", "ja"):
            print(f"written to {wizard.save()}")
        return 0
    finally:
        await machine.disconnect()


def cmd_calibrate(args: argparse.Namespace) -> int:
    return asyncio.run(_calibrate(args))


# ==========================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wbbot",
        description="Control software for a V-shape whiteboard plotter")
    parser.add_argument("--config", help="path to machine.yaml")
    parser.add_argument("--port", help="serial port, e.g. COM5")
    parser.add_argument("--host", help="controller host for the WiFi link")
    parser.add_argument("--simulator", action="store_true",
                        help="force the built-in simulator")
    parser.add_argument("--log-level", default="WARNING")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="start the web UI and API")
    serve.add_argument("--bind", help="interface to bind (default from config)")
    serve.add_argument("--http-port", type=int, help="HTTP port")
    serve.set_defaults(func=cmd_serve)

    mcp = sub.add_parser("mcp", help="start the MCP server on stdio")
    mcp.set_defaults(func=cmd_mcp)

    info = sub.add_parser("info", help="machine and safe-area summary")
    info.add_argument("--json", action="store_true")
    info.set_defaults(func=cmd_info)

    fluid = sub.add_parser("fluidnc-config", help="generate the FluidNC config")
    fluid.add_argument("--schema", default="3.7")
    fluid.add_argument("-o", "--output", default=None,
                       help="output path, or '-' for stdout")
    fluid.set_defaults(func=cmd_fluidnc_config)

    preview = sub.add_parser("preview", help="trace an image to an SVG")
    preview.add_argument("image")
    preview.add_argument("--mode", default="contours")
    preview.add_argument("--colors", default="single")
    preview.add_argument("-o", "--output")
    preview.set_defaults(func=cmd_preview)

    draw = sub.add_parser("draw", help="trace an image and plot it")
    draw.add_argument("image")
    draw.add_argument("--mode", default="contours")
    draw.add_argument("--colors", default="single")
    draw.add_argument("-y", "--yes", action="store_true",
                      help="do not ask before drawing")
    draw.set_defaults(func=cmd_draw)

    calib = sub.add_parser("calibrate", help="run the calibration wizard")
    calib.add_argument("--grid", type=int, default=3)
    calib.set_defaults(func=cmd_calibrate)

    selftest = sub.add_parser("selftest", help="draw a test pattern and check it")
    selftest.set_defaults(func=cmd_selftest)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s")
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as exc:
        if args.log_level.upper() == "DEBUG":
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
