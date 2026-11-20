"""MCP server: exposes the whiteboard plotter as a set of tools.

Thin adapter over :class:`wbbot.mcp.service.PlotterService` — the tools carry
the schemas and descriptions, the service carries the behaviour.

Run it with::

    wbbot-mcp                    # simulator, safe to try anywhere
    wbbot-mcp --transport serial --port COM5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Any, Sequence

from ..config import load_config
from ..driver.machine import Machine
from ..driver.simulator import FluidNCSimulator
from ..driver.transport import SimulatorTransport, make_transport
from .service import PlotterService

log = logging.getLogger(__name__)

__all__ = ["build_server", "main", "TOOL_SPECS"]


def _num(desc: str, default: float | None = None) -> dict:
    schema: dict[str, Any] = {"type": "number", "description": desc}
    if default is not None:
        schema["default"] = default
    return schema


def _points(desc: str) -> dict:
    return {
        "type": "array",
        "description": desc,
        "items": {"type": "array", "items": {"type": "number"},
                  "minItems": 2, "maxItems": 2},
        "minItems": 1,
    }


_COLOR = {"type": "integer", "default": 0,
          "description": "Marker index (0-based revolver slot)."}
_PREVIEW = {"type": "boolean", "default": False,
            "description": "Validate and estimate only; draw nothing."}
_BOX = {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4,
        "description": "Plot rectangle [x, y, width, height] in board mm. "
                       "Defaults to a large centred area."}


#: ``(tool_name, description, json_schema_properties, required)``
TOOL_SPECS: list[tuple[str, str, dict, list[str]]] = [
    # -- state -------------------------------------------------------------
    ("get_status",
     "Current plotter state: position, pen, colour, job progress, errors.",
     {}, []),
    ("get_board_info",
     "Board dimensions, the safe drawing area, available marker colours and "
     "the stroke width. Call this before planning any layout.",
     {}, []),
    ("home", "Home the machine and return to the park position.", {}, []),
    ("pause", "Feed-hold the running job. It can be resumed exactly.", {}, []),
    ("resume", "Resume a paused job.", {}, []),
    ("cancel",
     "Stop the running job cleanly, remembering where to resume from.", {}, []),
    ("emergency_stop",
     "Stop all motion immediately. Position may be lost; re-home afterwards.",
     {}, []),
    ("clear_alarm", "Clear an alarm or e-stop and return the machine to idle.",
     {}, []),

    # -- elementary --------------------------------------------------------
    ("move_to", "Move the gondola with the pen up.",
     {"x": _num("X in board mm"), "y": _num("Y in board mm")}, ["x", "y"]),
    ("pen_up", "Lift the pen.", {}, []),
    ("pen_down",
     "Lower the pen. Refused if the current position is outside the safe area.",
     {}, []),
    ("set_color", "Rotate the revolver to a marker.",
     {"index": {"type": "integer", "description": "0-based marker index"}},
     ["index"]),
    ("draw_line", "Draw a straight line.",
     {"x1": _num("start X"), "y1": _num("start Y"),
      "x2": _num("end X"), "y2": _num("end Y"),
      "color": _COLOR, "preview_only": _PREVIEW},
     ["x1", "y1", "x2", "y2"]),
    ("draw_polyline", "Draw a connected sequence of points.",
     {"points": _points("Vertices [[x, y], ...] in board mm"),
      "closed": {"type": "boolean", "default": False},
      "color": _COLOR, "preview_only": _PREVIEW},
     ["points"]),

    # -- geometry ----------------------------------------------------------
    ("draw_circle", "Draw a circle.",
     {"cx": _num("centre X"), "cy": _num("centre Y"), "radius": _num("radius mm"),
      "color": _COLOR, "preview_only": _PREVIEW}, ["cx", "cy", "radius"]),
    ("draw_arc", "Draw a circular arc (angles in degrees, counter-clockwise).",
     {"cx": _num("centre X"), "cy": _num("centre Y"), "radius": _num("radius mm"),
      "start_deg": _num("start angle"), "end_deg": _num("end angle"),
      "color": _COLOR, "preview_only": _PREVIEW},
     ["cx", "cy", "radius", "start_deg", "end_deg"]),
    ("draw_rect", "Draw a rectangle, optionally with rounded corners.",
     {"x": _num("lower-left X"), "y": _num("lower-left Y"),
      "width": _num("width mm"), "height": _num("height mm"),
      "corner_radius": _num("corner radius mm", 0.0),
      "color": _COLOR, "preview_only": _PREVIEW},
     ["x", "y", "width", "height"]),
    ("draw_polygon", "Draw a closed polygon.",
     {"points": _points("Vertices [[x, y], ...]"),
      "color": _COLOR, "preview_only": _PREVIEW}, ["points"]),
    ("draw_bezier",
     "Draw a Bezier curve of any degree (3 control points = quadratic, "
     "4 = cubic).",
     {"control_points": _points("Control points [[x, y], ...]"),
      "color": _COLOR, "preview_only": _PREVIEW}, ["control_points"]),

    # -- text and maths ----------------------------------------------------
    ("write_text",
     "Write text in a single-stroke font. height_mm is the capital-letter "
     "height. Use \\n for line breaks.",
     {"text": {"type": "string"},
      "x": _num("anchor X"), "y": _num("baseline Y"),
      "height_mm": _num("capital height in mm", 30.0),
      "align": {"type": "string", "enum": ["left", "center", "right"],
                "default": "left"},
      "angle_deg": _num("rotation in degrees", 0.0),
      "color": _COLOR, "preview_only": _PREVIEW},
     ["text", "x", "y"]),
    ("write_equation",
     "Render a LaTeX equation. mode='outline' gives true typesetting (best at "
     ">= 25 mm), mode='stroke' gives a linear single-stroke rendering that "
     "stays legible at any size, mode='skeleton' gives typeset centrelines.",
     {"latex": {"type": "string", "description": "LaTeX, e.g. \\frac{a}{b}"},
      "x": _num("anchor X"), "y": _num("anchor Y"),
      "height_mm": _num("total block height in mm", 40.0),
      "mode": {"type": "string", "enum": ["outline", "stroke", "skeleton"],
               "default": "outline"},
      "align": {"type": "string", "enum": ["left", "center", "right"],
                "default": "left"},
      "color": _COLOR, "preview_only": _PREVIEW},
     ["latex", "x", "y"]),
    ("plot_function",
     "Plot y = f(x). The expression is parsed safely (no eval). Poles and "
     "discontinuities become gaps, not vertical lines.",
     {"expression": {"type": "string", "description": "e.g. 'sin(x)/x'"},
      "x_min": _num("lower x", -10.0), "x_max": _num("upper x", 10.0),
      "y_min": _num("lower y (auto if omitted)"),
      "y_max": _num("upper y (auto if omitted)"),
      "box": _BOX, "axes": {"type": "boolean", "default": True},
      "samples": {"type": "integer", "default": 1200},
      "equal_aspect": {"type": "boolean", "default": False},
      "color": _COLOR, "preview_only": _PREVIEW},
     ["expression"]),
    ("plot_parametric", "Plot a parametric curve (x(t), y(t)).",
     {"x_expression": {"type": "string"}, "y_expression": {"type": "string"},
      "t_min": _num("lower t", 0.0), "t_max": _num("upper t", 6.283185),
      "box": _BOX, "samples": {"type": "integer", "default": 1500},
      "color": _COLOR, "preview_only": _PREVIEW},
     ["x_expression", "y_expression"]),
    ("plot_polar", "Plot r = f(theta) in polar coordinates.",
     {"r_expression": {"type": "string", "description": "e.g. '2*cos(3*theta)'"},
      "theta_min": _num("lower theta", 0.0),
      "theta_max": _num("upper theta", 6.283185),
      "box": _BOX, "samples": {"type": "integer", "default": 1500},
      "color": _COLOR, "preview_only": _PREVIEW},
     ["r_expression"]),
    ("draw_axes", "Draw labelled coordinate axes with ticks.",
     {"x_range": {"type": "array", "items": {"type": "number"},
                  "minItems": 2, "maxItems": 2, "default": [-10, 10]},
      "y_range": {"type": "array", "items": {"type": "number"},
                  "minItems": 2, "maxItems": 2, "default": [-10, 10]},
      "tick_spacing": {"type": "array", "items": {"type": "number"},
                       "minItems": 2, "maxItems": 2},
      "labels": {"type": "boolean", "default": True},
      "axis_labels": {"type": "array", "items": {"type": "string"},
                      "default": ["x", "y"]},
      "box": _BOX, "color": _COLOR, "preview_only": _PREVIEW}, []),
    ("plot_data", "Plot data points as a line, scatter, step or bar chart.",
     {"points": _points("Data [[x, y], ...] in data units"),
      "style": {"type": "string", "enum": ["line", "scatter", "bar", "step"],
                "default": "line"},
      "box": _BOX, "axes": {"type": "boolean", "default": True},
      "color": _COLOR, "preview_only": _PREVIEW}, ["points"]),

    # -- images ------------------------------------------------------------
    ("draw_image",
     "Trace an image and draw it. Accepts a local file path or a data: URI.",
     {"path_or_data": {"type": "string",
                       "description": "Local path or 'data:image/png;base64,...'"},
      "mode": {"type": "string",
               "enum": ["contours", "bitmap", "centerline", "halftone",
                        "stipple", "tsp"],
               "default": "contours"},
      "colors": {"type": "integer", "default": 1,
                 "description": "1 = single marker, 3 = full colour separation"},
      "fit": {"type": "string", "enum": ["contain", "fill", "none"],
              "default": "contain"},
      "margin_mm": _num("margin from the safe-area edge", 20.0),
      "preview_only": _PREVIEW},
     ["path_or_data"]),
    ("preview_image",
     "Trace an image and return an SVG preview plus timing, without drawing.",
     {"path_or_data": {"type": "string"},
      "mode": {"type": "string",
               "enum": ["contours", "bitmap", "centerline", "halftone",
                        "stipple", "tsp"], "default": "contours"},
      "colors": {"type": "integer", "default": 1},
      "fit": {"type": "string", "enum": ["contain", "fill", "none"],
              "default": "contain"},
      "margin_mm": _num("margin", 20.0)},
     ["path_or_data"]),

    # -- calibration -------------------------------------------------------
    ("run_calibration",
     "Draw the numbered calibration pattern. Returns the nominal target "
     "coordinates and measuring instructions for a human.",
     {"n_x": {"type": "integer", "default": 3},
      "n_y": {"type": "integer", "default": 3},
      "preview_only": _PREVIEW}, []),
    ("submit_calibration",
     "Submit measured cross positions and fit the machine parameters.",
     {"measurements": {
         "type": "array",
         "items": {"type": "object",
                   "properties": {"index": {"type": "integer"},
                                  "x": {"type": "number"},
                                  "y": {"type": "number"}},
                   "required": ["index", "x", "y"]}}},
     ["measurements"]),

    # -- perception --------------------------------------------------------
    ("capture_board",
     "Take a perspective-corrected photo of the board (needs a calibrated "
     "camera). Returns a PNG as base64.",
     {"include_image": {"type": "boolean", "default": True},
      "px_per_mm": _num("resolution of the rectified image", 1.0)}, []),
    ("read_board",
     "Look at the board: where the ink is, how much of the board is covered, "
     "and a description you can reason about.",
     {"include_image": {"type": "boolean", "default": False},
      "px_per_mm": _num("resolution", 1.0)}, []),
    ("find_free_space",
     "Find empty places to put new content of a given size, in board mm. "
     "Results are ranked by how much clear space surrounds them.",
     {"width_mm": _num("required width"), "height_mm": _num("required height"),
      "margin_mm": _num("clearance to keep from existing ink", 20.0),
      "max_results": {"type": "integer", "default": 5}},
     ["width_mm", "height_mm"]),
]

SERVER_INSTRUCTIONS = """\
This server controls a physical V-plotter that draws on a 1500 x 1000 mm
whiteboard with three coloured markers.

Coordinates are millimetres. The origin (0, 0) is the LOWER-LEFT corner of the
drawing area, x increases to the right and y increases upwards.

Before laying anything out, call get_board_info: it returns the board size, the
safe drawing area (the reachable region, which is slightly smaller than the
board) and the marker colours currently loaded.

Things worth knowing:
  * A marker stroke is about 2 mm wide. Detail finer than that will not show.
  * Requests outside the safe area are REFUSED, never silently clipped.
  * Every drawing tool accepts preview_only=true to validate and get a time
    estimate without drawing.
  * Drawing is slow. A full-board image can take half an hour; the returned
    estimated_duration_human tells you before you commit.
  * Changing colour costs time, so group work by colour where you can.
  * Ink cannot be undone. There is no erase tool.
"""


async def dispatch(service: PlotterService, name: str,
                   arguments: dict | None) -> dict:
    """Run one tool by name and always return a JSON-safe payload.

    A tool must never take the server down: bad arguments, safe-area refusals
    and hardware faults all come back as ``{"ok": false, "error": ...}``.
    """
    arguments = arguments or {}
    if name not in {spec[0] for spec in TOOL_SPECS}:
        return {"ok": False, "error": f"unknown tool {name!r}"}
    handler = getattr(service, name, None)
    if handler is None:      # pragma: no cover - guarded by the test suite
        return {"ok": False, "error": f"tool {name!r} has no implementation"}
    try:
        return await handler(**arguments)
    except TypeError as exc:
        return {"ok": False, "error": f"bad arguments for {name}: {exc}"}
    except Exception as exc:
        log.exception("tool %s failed", name)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _tool_payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str, indent=1)


def build_server(service: PlotterService, name: str = "wbbot"):
    """Create the MCP server object bound to ``service``.

    Supports both SDK generations: 2.x passes ``on_list_tools`` /
    ``on_call_tool`` to the constructor, while 1.x used ``@server.list_tools()``
    decorators.  The same version-drift discipline the brief asks for around
    FluidNC applies here — the shape of this API has already changed once.
    """
    try:
        from mcp.server import Server                       # noqa: PLC0415
        from mcp import types                               # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "the MCP SDK is not installed (pip install 'wbbot[mcp]')") from exc

    def build_tools() -> list:
        tools = []
        for tool_name, description, properties, required in TOOL_SPECS:
            schema = {"type": "object", "properties": properties,
                      "required": required}
            try:                       # SDK 2.x
                tools.append(types.Tool(name=tool_name, description=description,
                                        input_schema=schema))
            except Exception:          # SDK 1.x used the camelCase alias
                tools.append(types.Tool(name=tool_name, description=description,
                                        inputSchema=schema))
        return tools

    # ---- SDK 2.x: handlers are constructor arguments ---------------------
    if "on_call_tool" in __import__("inspect").signature(Server.__init__).parameters:
        async def on_list_tools(ctx, params=None):
            return types.ListToolsResult(tools=build_tools())

        async def on_call_tool(ctx, params):
            payload = await dispatch(service, params.name, params.arguments)
            return types.CallToolResult(
                content=[types.TextContent(type="text",
                                           text=_tool_payload(payload))],
                is_error=not payload.get("ok", False),
            )

        return Server(name, instructions=SERVER_INSTRUCTIONS,
                      on_list_tools=on_list_tools, on_call_tool=on_call_tool)

    # ---- SDK 1.x: decorator registration ---------------------------------
    server = Server(name, instructions=SERVER_INSTRUCTIONS)  # pragma: no cover

    @server.list_tools()                                     # pragma: no cover
    async def list_tools():
        return build_tools()

    @server.call_tool()                                      # pragma: no cover
    async def call_tool(tool_name: str, arguments: dict | None):
        payload = await dispatch(service, tool_name, arguments)
        return [types.TextContent(type="text", text=_tool_payload(payload))]

    return server


async def run_stdio(service: PlotterService) -> None:  # pragma: no cover
    from mcp.server.stdio import stdio_server            # noqa: PLC0415

    server = build_server(service)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="wbbot-mcp",
        description="MCP server exposing the whiteboard plotter as tools")
    parser.add_argument("--transport",
                        choices=["simulator", "serial", "websocket"],
                        help="override connection.transport from machine.yaml")
    parser.add_argument("--port", help="serial port, e.g. COM5")
    parser.add_argument("--host", help="controller host for the websocket link")
    parser.add_argument("--config", help="path to machine.yaml")
    parser.add_argument("--no-wait", action="store_true",
                        help="return from drawing tools immediately instead of "
                             "waiting for the job to finish")
    parser.add_argument("--log-level", default="WARNING")
    return parser.parse_args(argv)


async def _amain(args: argparse.Namespace) -> int:  # pragma: no cover
    overrides: dict = {}
    if args.transport:
        overrides.setdefault("connection", {})["transport"] = args.transport
    if args.port:
        overrides.setdefault("connection", {}).setdefault("serial", {})["port"] = args.port
        overrides["connection"].setdefault("transport", "serial")
    if args.host:
        overrides.setdefault("connection", {}).setdefault(
            "websocket", {})["host"] = args.host
        overrides["connection"].setdefault("transport", "websocket")

    cfg = load_config(args.config, overrides=overrides or None)
    simulator = (FluidNCSimulator(cfg)
                 if cfg.connection.transport == "simulator" else None)
    transport = (SimulatorTransport(simulator) if simulator is not None
                 else make_transport(cfg))
    machine = Machine(cfg, transport=transport, simulator=simulator)
    await machine.connect()
    service = PlotterService(machine, default_wait=not args.no_wait)
    log.info("wbbot MCP server ready on %s", transport.description)
    try:
        await run_stdio(service)
    finally:
        await machine.disconnect()
    return 0


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover
    args = _parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.WARNING),
                        stream=sys.stderr)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
