"""MCP server: lets an AI assistant or any MCP client draw on and operate the whiteboard plotter.

One implementation, two ways to run it (see ``wbot/mcp_mount.py`` for the HTTP mount):

* mounted in the FastAPI app at ``/mcp`` (streamable HTTP, calls the REST API in-process), or
* standalone ``python -m wbot mcp [--api http://127.0.0.1:8000]`` (stdio), proxying to a running
  ``python -m wbot serve`` so both share the one machine connection and state.

Safety design. All motion goes through the existing REST/Machine layer, so its rules stay in
force: pen is lifted before the revolver turns, every point is clipped to the usable rectangle,
a reference is required, and every G-code line passes ``wbot.safety``. The tools are deliberately
HIGH-LEVEL. NOT exposed, because each of them needs a human at the board:

* raw G-code / console (could bypass the planner's pen/revolver ordering and geometry),
* connect / disconnect and choosing the serial port (opening a port can reset the board),
* set_reference (only a human can see where the gondola really hangs),
* unlock / reset after an alarm (the cause must be inspected first),
* config changes and firmware upload (would change limits or the machine's behaviour),
* direct servo angles (mechanical end stops).

``mcp.allow_motion: false`` in config/wbot.yaml is a kill switch (status, preview and stop only).
Tool errors carry the German message of the API; tools use 1-based colour slots only.
"""
from __future__ import annotations

import asyncio
import functools
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp_types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict, ValidationError

from .drawing import Drawing
from .mcp_shapes import ShapeError, ShapeSpec, build_shape, build_text
from .mcp_tools import (Api, DrawOptions, McpToolError, board_info, board_text, check_enabled,
                        check_motion_allowed, job_text, load_ctx, patterns_text, plan_and_run,
                        refuse_if_job_running, resolve_color, status_text, strokes_from_input, to_json_text)
from .patterns import build_pattern

INSTRUCTIONS = """\
W-BOT is a wall-mounted whiteboard plotter (V-plotter: two belts, a pen servo and a 3-colour pen revolver). \
You draw on it with these tools.

COORDINATES: millimetres, origin = lower-left corner of the board, x to the right, y UP. The pen can only reach a \
usable rectangle inside the board (see get_board_info); anything outside is clipped with a warning, so design \
inside it. Pen colours are the 1-based slots 1..3 (or their names); use get_board_info to see them.

WORKFLOW
1. get_status: the plotter must be connected and have a reference. A human does that in the web UI; you cannot \
(connect, set reference, unlock and raw G-code are deliberately not available). If ready_to_draw is NO, tell the \
user what is missing.
2. get_board_info / list_patterns to learn the board and ready-made test patterns.
3. Look before you draw: preview_drawing, or any draw_* tool with preview_only=true, returns a PNG of exactly what \
would be drawn (usable area, grid in mm, colours, grey pen-up travel) plus estimates. Check composition, size and \
position, fix, then draw. dry_run=true runs the whole job with the pen up.
4. Drawing is slow (minutes). draw_* tools return immediately with a job id; poll job_status every 10-30 s. Do not \
start a second drawing while one is running (it is refused). pause_job / resume_job / cancel_job (lifts the pen) \
and emergency_stop exist; after an emergency stop a human must unlock and re-reference.
5. Colour changes cost time (the revolver turns): group things by colour.
6. If a camera is set up, camera_snapshot shows what is really on the board (with the plan as overlay): look before drawing next to existing content, and after a drawing to check it.

TIPS: one draw call = one job, so put a whole composition into one call (draw_strokes, draw_shapes). Close a \
polygon by repeating its first point. Single-stroke font: text height is the cap height; below ~15 mm text gets \
hard to read with a marker. Filled areas are hatched with parallel lines (hatch_spacing_mm >= 1; 3-5 mm looks \
solid enough with a marker). Errors are German messages from the machine; relay anything that needs a human.
"""

Color = int | str
_RO = ToolAnnotations(read_only_hint=True, destructive_hint=False)
_DRAW = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
_STOP = ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True)


class StrokeIn(BaseModel):
    """One pen-down polyline: colour slot (1-based) or name, and [x, y] points in mm."""
    model_config = ConfigDict(extra="forbid")
    color: int | str = 1
    points: list[list[float]]


def _err(msg: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=msg)], is_error=True)


def build_server(api: Api) -> MCPServer:
    server = MCPServer("wbot", title="W-BOT whiteboard plotter", instructions=INSTRUCTIONS, version="2.0")

    def tool(annotations: ToolAnnotations):
        def deco(fn):
            @functools.wraps(fn)
            async def wrapper(*a, **kw):
                try:
                    return await fn(*a, **kw)
                except McpToolError as exc:
                    return _err(exc.message)
                except (ShapeError, ValueError, ValidationError) as exc:
                    return _err(f"Invalid input: {exc}")
            server.add_tool(wrapper, name=fn.__name__, annotations=annotations, structured_output=False)
            return fn
        return deco

    async def ctx():
        c = await load_ctx(api)
        check_enabled(c)
        return c

    async def draw(c, d: Drawing, name: str, dry_run: bool, preview_only: bool, confirm: bool,
                   notes: list[str] | None = None, warnings: list[str] | None = None, show_travel: bool = True):
        return await plan_and_run(api, c, d, DrawOptions(name=name, dry_run=dry_run, preview_only=preview_only,
                                                         confirm=confirm, show_travel=show_travel,
                                                         notes=notes or [], warnings=warnings or []))

    # ------------------------------------------------------------------ information
    @tool(_RO)
    async def get_status() -> str:
        """Current state of the plotter: connection, machine state, pen position (mm), reference, pen up/down, \
selected colour, running job, warnings, and whether drawing is possible right now (ready_to_draw)."""
        c = await ctx()
        return status_text(c.state, c.cfg)

    @tool(_RO)
    async def get_board_info() -> str:
        """Board size, the usable drawing rectangle, coordinate convention (origin lower-left, x right, y up, mm), \
pen colours with their 1-based slot numbers and names, speeds and the per-drawing limits."""
        return board_text(board_info(await ctx()))

    @tool(_RO)
    async def list_patterns() -> str:
        """Ready-made test patterns (grid, frame, circles, star, text, ...) with their parameters, for \
draw_pattern. Colour parameters use the 1-based slots."""
        c = await ctx()
        return patterns_text(await api.get("/api/patterns"), len(c.cfg.colors))

    @tool(_RO)
    async def job_status() -> str:
        """State of the current/last drawing job (progress, elapsed, time left) plus the machine state. Poll this \
every 10-30 s after starting a drawing."""
        c = await ctx()
        job = await api.get("/api/job")
        return f"job: {job_text(job)}\nmachine: {c.state['machine']['state']}"

    @tool(_RO)
    async def camera_snapshot(view: Literal["board", "camera"] = "board", overlay: bool = True, width: int = 1280) -> Any:
        """Live photo of the whiteboard from the camera: what is REALLY on the board. view="board" (default) is the \
rectified top-down view in board orientation (x right, y up, covering the board plus a margin; the text gives the mm \
range of the image), view="camera" the raw camera picture. With overlay=true it shows the board outline (orange), the \
usable area (yellow, dashed), the planned drawing of the current/last job (magenta or pen colour), where the software \
believes the pen is (green cross) and where the camera sees the gondola marker (blue circle). Use it to check a result, \
find free space or compare plan and reality. Never moves anything."""
        await ctx()
        info = await api.get("/api/camera")
        if not info.get("source"):
            raise McpToolError("No camera is selected. A human chooses one in the web UI (tab Kamera).", "no_camera")
        v = "board" if view == "board" else "raw"
        if v == "board" and not info.get("calibrated"):
            raise McpToolError("The camera is not calibrated to the board yet (web UI, tab Kamera, step 3). "
                               "view='camera' gives the raw picture.", "camera_uncalibrated")
        w = max(320, min(int(width), 1920))
        jpg = await api.request("GET", f"/api/camera/frame.jpg?view={v}&w={w}&q=80&overlay={int(bool(overlay))}")
        if not isinstance(jpg, bytes):
            raise McpToolError("The camera did not return an image.")
        lines = [f"camera snapshot, view={view}"]
        if v == "board":
            x0, y0, x1, y1 = info["view_rect"]
            lines.append(f"image spans x {x0:g}..{x1:g} mm (left to right) and y {y1:g}..{y0:g} mm (top to bottom), "
                         f"about {w / (x1 - x0):.2f} px per mm; parts the camera cannot see are black")
        tr = info.get("track") or {}
        if tr.get("pen"):
            lines.append(f"gondola marker: pen seen at x={tr['pen'][0]:.1f} y={tr['pen'][1]:.1f} mm"
                         + (f", {tr['dev_mm']:.1f} mm from the software position" if tr.get("dev_mm") is not None else ""))
        import base64
        return [TextContent(type="text", text="\n".join(lines)),
                ImageContent(type="image", data=base64.b64encode(jpg).decode(), mime_type="image/jpeg")]

    # ------------------------------------------------------------------ preview
    @tool(_RO)
    async def preview_drawing(strokes: list[StrokeIn] | None = None, pattern: str | None = None,
                              params: dict[str, Any] | None = None, show_travel: bool = True,
                              name: str = "AI drawing") -> Any:
        """Render what would be drawn as a PNG image (board outline, usable area, 100 mm grid, strokes in the pen \
colours, grey pen-up travel) plus estimated time and warnings. Never moves anything. Give EITHER `strokes` \
([{color, points:[[x,y],...]}], mm) OR `pattern` (id from list_patterns) with optional `params`. (Text, shapes and \
SVG have their own tools with preview_only=true.)"""
        c = await ctx()
        if (strokes is None) == (pattern is None):
            raise McpToolError("Give exactly one of 'strokes' or 'pattern'.")
        d = strokes_from_input(strokes, c) if strokes is not None else _pattern(c, pattern, params)
        return await draw(c, d, name, False, True, False, show_travel=show_travel)

    # ------------------------------------------------------------------ drawing
    def _pattern(c, pid: str, params: dict | None) -> Drawing:
        p = dict(params or {})
        if "color" in p:
            p["color"] = resolve_color(p["color"], c.cfg)
        try:
            return build_pattern(c.cfg, pid, p)
        except ValueError as exc:
            raise McpToolError(f"{exc}. Available patterns: use list_patterns.") from None

    @tool(_DRAW)
    async def draw_pattern(id: str, params: dict[str, Any] | None = None, dry_run: bool = False,
                           preview_only: bool = False, confirm: bool = False) -> Any:
        """Draw a ready-made pattern (id and parameters from list_patterns), e.g. 'square-frame', 'circles', \
'star', 'text'. Starts a background job and returns its id, an estimate and a PNG preview. preview_only=true only \
renders; dry_run=true runs with the pen up; confirm=true only when the operator requires confirmation for real \
hardware."""
        c = await ctx()
        return await draw(c, _pattern(c, id, params), f"Pattern {id}", dry_run, preview_only, confirm)

    @tool(_DRAW)
    async def draw_strokes(strokes: list[StrokeIn], name: str = "AI drawing", dry_run: bool = False,
                           preview_only: bool = False, confirm: bool = False) -> Any:
        """Draw freely: a list of strokes, each {color: slot 1..3 or name, points: [[x, y], ...]} in board mm \
(origin lower-left, y up). Points outside the usable area are clipped with a warning. Limits: see get_board_info. \
Starts a background job (poll job_status) and returns a PNG preview. preview_only=true only renders; dry_run=true \
runs with the pen up."""
        c = await ctx()
        return await draw(c, strokes_from_input(strokes, c), name, dry_run, preview_only, confirm)

    @tool(_DRAW)
    async def draw_text(text: str, x: float, y: float, height_mm: float = 40.0, color: Color = 1,
                        align: Literal["left", "center", "right"] = "center",
                        valign: Literal["baseline", "center", "top"] = "center", line_spacing: float = 1.0,
                        dry_run: bool = False, preview_only: bool = False, confirm: bool = False) -> Any:
        """Write text with a single-stroke (Hershey) font, e.g. to write HALLO in the middle of the board. (x, y) \
is the anchor in board mm: `align` = horizontal reference (left/center/right edge of every line), `valign` = vertical \
reference of the whole block (center = middle of the capitals, baseline, top). height_mm = capital height. Use \\n \
for several lines. Umlauts work. Returns a PNG preview; text sticking out of the usable area is clipped (warning)."""
        c = await ctx()
        strokes, (w, h) = build_text(text, x, y, height_mm, resolve_color(color, c.cfg), align, valign, line_spacing)
        return await draw(c, Drawing(strokes), "Text " + " ".join(text.split())[:20], dry_run, preview_only, confirm,
                          notes=[f"text block is {w:.0f} x {h:.0f} mm"])

    @tool(_DRAW)
    async def draw_shape(kind: Literal["line", "polyline", "rect", "circle", "ellipse", "polygon", "arc"],
                         points: list[list[float]] | None = None, x: float | None = None, y: float | None = None,
                         width: float | None = None, height: float | None = None, radius: float | None = None,
                         rx: float | None = None, ry: float | None = None, start_deg: float | None = None,
                         end_deg: float | None = None, sides: int | None = None, rotation_deg: float = 0.0,
                         fill: bool = False, hatch_spacing_mm: float = 3.0, hatch_angle_deg: float = 45.0,
                         color: Color = 1, dry_run: bool = False, preview_only: bool = False,
                         confirm: bool = False) -> Any:
        """Draw ONE geometric shape (use draw_shapes for a composition in one job). Needed fields per kind: \
line: points=[[x0,y0],[x1,y1]]; polyline: points (>=2); rect: x, y = lower-left corner, width, height (optional \
rotation_deg about the centre); circle: x, y = centre, radius; ellipse: x, y, rx, ry (optional rotation_deg); \
polygon: points (>=3, closed automatically) OR x, y, radius, sides (regular, first vertex up); arc: x, y = centre, \
radius, start_deg, end_deg (counter-clockwise from the +x axis). fill=true hatches closed shapes with parallel lines \
(hatch_spacing_mm >= 1, hatch_angle_deg); a filled arc is a pie sector."""
        c = await ctx()
        spec = ShapeSpec(kind=kind, points=points, x=x, y=y, width=width, height=height, radius=radius, rx=rx,
                         ry=ry, start_deg=start_deg, end_deg=end_deg, sides=sides, rotation_deg=rotation_deg,
                         fill=fill, hatch_spacing_mm=hatch_spacing_mm, hatch_angle_deg=hatch_angle_deg, color=color)
        d = Drawing(build_shape(spec, resolve_color(color, c.cfg), c.cfg.safe_rect()))
        return await draw(c, d, f"Shape {kind}", dry_run, preview_only, confirm)

    @tool(_DRAW)
    async def draw_shapes(shapes: list[ShapeSpec], name: str = "AI shapes", dry_run: bool = False,
                          preview_only: bool = False, confirm: bool = False) -> Any:
        """Draw several shapes as ONE job (e.g. a house = rect + polygon roof + door). Each shape has the fields \
of draw_shape (kind, points, x, y, width, height, radius, rx, ry, start_deg, end_deg, sides, rotation_deg, fill, \
hatch_spacing_mm, hatch_angle_deg, color). Returns a PNG preview."""
        c = await ctx()
        if not shapes:
            raise McpToolError("'shapes' is empty.")
        strokes = []
        for i, s in enumerate(shapes):
            try:
                strokes += build_shape(s, resolve_color(s.color, c.cfg), c.cfg.safe_rect())
            except ShapeError as exc:
                raise McpToolError(f"Shape {i} ({s.kind}): {exc}") from None
        return await draw(c, Drawing(strokes), name, dry_run, preview_only, confirm)

    @tool(_DRAW)
    async def draw_svg(svg_text: str, fit: bool = True, margin_mm: float = 10.0, x: float | None = None,
                       y: float | None = None, name: str = "AI svg", dry_run: bool = False,
                       preview_only: bool = False, confirm: bool = False) -> Any:
        """Draw an SVG document given as text. Only outlines are drawn (paths, lines, rects, circles, polygons; \
no text/images/fills; the nearest pen colour is chosen per stroke colour). fit=true scales it to the usable area \
(minus margin_mm); fit=false keeps its real size (SVG width/height in mm) and puts its lower-left corner at (x, y) \
(default: lower-left of the usable area). Returns a PNG preview."""
        from .svgimport import SvgError, SvgOptions, import_svg
        c = await ctx()
        try:
            imp = import_svg(svg_text, c.cfg, SvgOptions(max_points=c.mcp.max_points, max_bytes=2_000_000), name)
        except SvgError as exc:
            raise McpToolError(f"SVG konnte nicht gelesen werden: {exc}") from None
        d, rect = imp.drawing, c.cfg.safe_rect()
        if not d.strokes:
            raise McpToolError("The SVG contains nothing drawable. " + " ".join(imp.warnings))
        if fit:
            d = d.fit_to(rect, keep_aspect=True, margin=margin_mm)
        else:
            b = d.bounds()
            d = d.transformed(dx=(rect[0] if x is None else x) - b[0], dy=(rect[1] if y is None else y) - b[1])
        return await draw(c, d, name, dry_run, preview_only, confirm, warnings=imp.warnings)

    # ------------------------------------------------------------------ job control
    async def _running_job() -> dict | None:
        job = await api.get("/api/job")
        return job if job and job["state"] in ("planning", "running", "paused") else None

    @tool(_STOP)
    async def pause_job() -> str:
        """Pause the running job (feed hold; the pen stays where it is). Continue with resume_job."""
        await ctx()
        if not await _running_job():
            raise McpToolError("No job is running.")
        await api.post("/api/hold")
        return "Hold sent: " + job_text(await api.get("/api/job"))

    @tool(_DRAW)
    async def resume_job() -> str:
        """Continue a paused job where it stopped (only when motion is allowed in the config)."""
        check_motion_allowed(await ctx())
        job = await _running_job()
        if not job or job["state"] != "paused":
            raise McpToolError("No paused job to resume.")
        await api.post("/api/resume")
        return "Resumed: " + job_text(await api.get("/api/job"))

    @tool(_STOP)
    async def cancel_job() -> str:
        """Cancel the running or paused job and lift the pen."""
        await ctx()
        if not await _running_job():
            raise McpToolError("No job is running.")
        await api.post("/api/cancel")
        st: dict = {}
        for _ in range(50):                          # the runner lifts the pen after stopping: wait for that
            st = await api.get("/api/state")
            job = st.get("job")
            if (not job or job["state"] not in ("planning", "running", "paused")) and not st["machine"]["busy"]:
                break
            await asyncio.sleep(0.2)
        return f"Cancel requested: {job_text(st.get('job'))}; pen {'up' if st['pen']['up'] else 'NOT confirmed up'}"

    @tool(_STOP)
    async def emergency_stop() -> str:
        """Immediate stop of all motion (also stops a running job). Always allowed. Afterwards the controller is \
in alarm and the position is uncertain: a human must check the machine, unlock it and re-confirm the reference."""
        await api.post("/api/estop")
        return ("EMERGENCY STOP sent. The machine is stopped; the controller is in alarm and the position is "
                "uncertain. Tell the user: a human must inspect the plotter, unlock it and re-confirm the "
                "reference in the web UI before drawing again.")

    # ------------------------------------------------------------------ simple moves
    @tool(_DRAW)
    async def pen_up() -> str:
        """Lift the pen (safe; nothing moves in x/y)."""
        check_motion_allowed(await ctx())
        await refuse_if_job_running(api, "The pen is under the job's control.")
        s = await api.post("/api/pen", {"state": "up"})
        return "pen up" if s["pen"]["up"] else "pen command sent"

    @tool(_DRAW)
    async def move_to(x: float, y: float) -> str:
        """Lift the pen and move to (x, y) in board mm (inside the usable area; needs a reference). Blocks until \
the gondola arrived (up to a few tens of seconds)."""
        check_motion_allowed(await ctx())
        await refuse_if_job_running(api)
        await api.post("/api/goto", {"x": x, "y": y, "pen": "up"})
        s = await api.get("/api/state")
        ph = s["position"]["physical"]
        return f"moved to x={ph[0]:.1f} y={ph[1]:.1f} mm, pen up" if ph else "moved"

    @tool(_DRAW)
    async def select_color(color: Color) -> str:
        """Turn the revolver to a pen colour (slot 1..3 or name). The pen is lifted automatically first."""
        c = await ctx()
        check_motion_allowed(c)
        await refuse_if_job_running(api)
        slot = resolve_color(color, c.cfg)
        s = await api.post("/api/revolver", {"slot": slot + 1})
        return f"colour {slot + 1} ({c.cfg.colors[slot].name}) selected; pen up" if s["revolver"]["slot"] == slot + 1 \
            else "revolver command sent"

    # ------------------------------------------------------------------ resources
    @server.resource("wbot://board", name="board", mime_type="application/json",
                     description="Board size, usable rectangle, colours and limits (JSON)")
    async def board_resource() -> str:
        return to_json_text(board_info(await ctx()))

    @server.resource("wbot://patterns", name="patterns", mime_type="application/json",
                     description="Available test patterns and their parameters (JSON)")
    async def patterns_resource() -> str:
        await ctx()
        return to_json_text(await api.get("/api/patterns"))

    return server


# ---------------------------------------------------------------------- transports
def api_for_url(url: str) -> Api:
    base = url.rstrip("/")
    return Api(httpx.AsyncClient(base_url=base, timeout=httpx.Timeout(300.0, connect=5.0)), base)


def run_stdio(api_url: str) -> None:
    """Standalone stdio server proxying to a running ``wbot serve`` (stdout carries only MCP frames)."""
    build_server(api_for_url(api_url)).run("stdio")


__all__ = ["build_server", "api_for_url", "run_stdio", "INSTRUCTIONS", "Api"]
