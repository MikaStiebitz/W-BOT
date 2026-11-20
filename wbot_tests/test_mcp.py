"""MCP server (AI control): tools end to end on the simulator, refusals, auth, stdio and HTTP sessions.

Everything runs against the simulator; no serial port is ever opened.
"""
import asyncio
import base64
import contextlib
import io
import json
import socket
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import Client
from PIL import Image

from wbot.api.app import create_app
from wbot.config import Mcp, deep_update, load_config
from wbot.machine import Machine
from wbot.mcp_server import build_server
from wbot.mcp_tools import Api, DrawOptions, McpToolError, load_ctx, plan_and_run
from wbot.drawing import Drawing

ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOLS = {"get_status", "get_board_info", "list_patterns", "preview_drawing", "draw_pattern",
                  "draw_strokes", "draw_text", "draw_shape", "draw_shapes", "draw_svg", "job_status", "pause_job",
                  "resume_job", "cancel_job", "emergency_stop", "pen_up", "move_to", "select_color",
                  "camera_snapshot"}
SQUARE = [[400, 300], [460, 300], [460, 360], [400, 360], [400, 300]]


# ------------------------------------------------------------------ fixtures / helpers
@pytest.fixture()
def plotter():
    """Machine on the simulator (speed 60), reference (0, 0), app without UI."""
    m = Machine(load_config())                 # no config_path: never writes the config
    app = create_app(m, serve_ui=False)
    m.connect("sim", None, None, {"speed": 60})
    m.set_reference(0, 0)
    yield m, app
    m.shutdown()


@contextlib.asynccontextmanager
async def mcp_client(app):
    """In-memory MCP client whose tools call the app's REST API in-process (like the /mcp mount)."""
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://wbot.test", timeout=60)
    api = Api(http, "test")
    async with Client(build_server(api)) as c:
        c.api = api
        yield c
    await http.aclose()


def text(res) -> str:
    return "\n".join(b.text for b in res.content if b.type == "text")


def images(res):
    return [b for b in res.content if b.type == "image"]


async def wait_job(c, timeout=90.0) -> str:
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = text(await c.call_tool("job_status", {}))
        if " done," in s or " error," in s or " cancelled," in s:
            return s
        await asyncio.sleep(0.25)
    raise AssertionError("job did not finish: " + s)


async def job_error_mm(c) -> float:
    return (await c.api.get("/api/simulator"))["job_error"]["max_mm"]


# ------------------------------------------------------------------ listing, info, preview
async def test_camera_snapshot(plotter):
    m, app = plotter
    async with mcp_client(app) as c:
        res = await c.call_tool("camera_snapshot", {})
        assert res.is_error and "No camera" in text(res)
        m.goto(750, 450)
        app.state.camera.configure({"source": {"kind": "sim"}})
        app.state.camera.frame(timeout=5.0)
        res = await c.call_tool("camera_snapshot", {})
        assert res.is_error and "not calibrated" in text(res)
        raw = await c.call_tool("camera_snapshot", {"view": "camera", "width": 640})
        assert not raw.is_error and images(raw)[0].mime_type == "image/jpeg"
        assert Image.open(io.BytesIO(base64.b64decode(images(raw)[0].data))).width == 640
        app.state.camera.calibrate_from_markers()
        res = await c.call_tool("camera_snapshot", {"view": "board"})
        assert not res.is_error and "image spans x" in text(res)
        im = Image.open(io.BytesIO(base64.b64decode(images(res)[0].data)))
        assert im.format == "JPEG" and im.width == 1280


async def test_tool_listing_resources_and_instructions(plotter):
    _, app = plotter
    async with mcp_client(app) as c:
        tools = (await c.list_tools()).tools
        names = {t.name for t in tools}
        assert names == EXPECTED_TOOLS
        assert all(t.description and len(t.description) > 30 for t in tools)
        # deliberately not exposed (need a human at the board)
        assert not {n for n in names if any(k in n for k in ("gcode", "connect", "reference", "unlock", "reset",
                                                             "config", "firmware", "servo"))}
        assert "usable" in c.instructions and "job_status" in c.instructions and "y UP" in c.instructions
        uris = {str(r.uri) for r in (await c.list_resources()).resources}
        assert uris == {"wbot://board", "wbot://patterns"}
        board = json.loads((await c.read_resource("wbot://board")).contents[0].text)
        assert board["usable_rect_mm"] == [120, 10, 1380, 750]
        assert [col["slot"] for col in board["colors"]] == [1, 2, 3]
        pats = json.loads((await c.read_resource("wbot://patterns")).contents[0].text)
        assert any(p["id"] == "square-frame" for p in pats)


async def test_status_board_info_and_patterns(plotter):
    _, app = plotter
    async with mcp_client(app) as c:
        s = text(await c.call_tool("get_status", {}))
        assert "connection: connected (sim" in s and "reference: set" in s and "ready_to_draw: yes" in s
        b = text(await c.call_tool("get_board_info", {}))
        assert "x 120..1380, y 10..750" in b and "1=black" in b and "3=blue" in b and "lower-left" in b
        p = text(await c.call_tool("list_patterns", {}))
        assert "square-frame" in p and "colour slot 1..3" in p


async def test_preview_returns_valid_png_and_moves_nothing(plotter):
    m, app = plotter
    async with mcp_client(app) as c:
        strokes = [{"color": 1, "points": SQUARE}, {"color": "blue", "points": [[600, 300], [900, 500]]}]
        res = await c.call_tool("preview_drawing", {"strokes": strokes})
        assert not res.is_error
        assert "PREVIEW ONLY" in text(res) and "estimated" in text(res)
        png = images(res)[0]
        assert png.mime_type == "image/png"
        im = Image.open(io.BytesIO(base64.b64decode(png.data)))
        assert im.format == "PNG" and im.width >= 800 and im.height >= 500
        px = im.convert("RGB").getcolors(maxcolors=1 << 20)
        assert len(px) > 5                                     # not blank
        assert any(abs(r - 21) < 25 and abs(g - 101) < 25 and abs(b - 192) < 25 for _, (r, g, b) in px)  # blue pen
        assert m.job_info() is None and m.simulator_info()["trace_segments"] == 0


async def test_preview_via_pattern_and_validation(plotter):
    _, app = plotter
    async with mcp_client(app) as c:
        ok = await c.call_tool("preview_drawing", {"pattern": "star", "params": {"color": "red"}})
        assert not ok.is_error and "2 (red)" in text(ok) and images(ok)
        for args in ({}, {"strokes": [{"points": SQUARE}], "pattern": "star"}):
            assert (await c.call_tool("preview_drawing", args)).is_error
        bad = await c.call_tool("preview_drawing", {"pattern": "gibt-es-nicht"})
        assert bad.is_error and "Unknown test pattern" in text(bad)
        bad = await c.call_tool("preview_drawing", {"strokes": [{"color": 4, "points": SQUARE}]})
        assert bad.is_error and "1..3" in text(bad)
        bad = await c.call_tool("preview_drawing", {"strokes": [{"points": [[1, 1]]}]})
        assert bad.is_error and "fewer than 2" in text(bad)


# ------------------------------------------------------------------ drawing end to end
async def test_draw_text_end_to_end_accuracy(plotter):
    m, app = plotter
    async with mcp_client(app) as c:
        res = await c.call_tool("draw_text", {"text": "HI", "x": 750, "y": 400, "height_mm": 30, "color": 1})
        assert not res.is_error and "Job " in text(res) and "poll job_status" in text(res) and images(res)
        assert m.job_info()["state"] in ("running", "done")      # returned immediately, job runs on its own
        assert " done," in await wait_job(c)
        assert await job_error_mm(c) < 0.1
        assert (await c.api.get("/api/state"))["pen"]["up"] is True


async def test_draw_shape_and_shapes_and_strokes_end_to_end(plotter):
    _, app = plotter
    async with mcp_client(app) as c:
        r = await c.call_tool("draw_shape", {"kind": "circle", "x": 700, "y": 400, "radius": 25,
                                             "fill": True, "hatch_spacing_mm": 6, "color": "red"})
        assert not r.is_error and "2 (red)" in text(r)
        assert " done," in await wait_job(c)
        assert await job_error_mm(c) < 0.1
        r = await c.call_tool("draw_shapes", {"shapes": [
            {"kind": "rect", "x": 500, "y": 300, "width": 60, "height": 40},
            {"kind": "polygon", "x": 530, "y": 370, "radius": 35, "sides": 3, "color": 3},
            {"kind": "line", "points": [[500, 300], [560, 340]], "color": 2}]})
        assert not r.is_error and "3 strokes" in text(r) or "strokes" in text(r)
        assert " done," in await wait_job(c)
        assert await job_error_mm(c) < 0.1
        r = await c.call_tool("draw_strokes", {"strokes": [{"color": 1, "points": SQUARE},
                                                           {"color": 3, "points": [[420, 320], [440, 340]]}]})
        assert not r.is_error
        assert " done," in await wait_job(c)
        assert await job_error_mm(c) < 0.1


async def test_draw_pattern_and_svg_dry_run(plotter):
    m, app = plotter
    async with mcp_client(app) as c:
        svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="60mm" height="40mm" viewBox="0 0 60 40">'
               '<rect x="2" y="2" width="56" height="36" fill="none" stroke="#000"/><text x="5" y="20">x</text></svg>')
        r = await c.call_tool("draw_svg", {"svg_text": svg, "fit": False, "x": 500, "y": 300, "dry_run": True})
        assert not r.is_error and "DRY RUN" in text(r) and "text elements are not imported" in text(r)
        assert " done," in await wait_job(c)
        assert all(not s.pen_down for s in m._emulator().trace)  # dry run: pen stayed up
        r = await c.call_tool("draw_pattern", {"id": "corner-marks", "params": {"length_mm": 20}})
        assert not r.is_error
        assert " done," in await wait_job(c)
        assert await job_error_mm(c) < 0.1
        bad = await c.call_tool("draw_svg", {"svg_text": "<html/>"})
        assert bad.is_error and "SVG" in text(bad)


async def test_clipping_warns_and_empty_after_clip_is_refused(plotter):
    m, app = plotter
    async with mcp_client(app) as c:
        r = await c.call_tool("draw_strokes", {"strokes": [{"points": [[-300, 400], [700, 400]]}],
                                               "preview_only": True})
        assert not r.is_error and "clipped" in text(r) and "x 120" in text(r)
        r = await c.call_tool("draw_strokes", {"strokes": [{"points": [[-300, -400], [-100, -100]]}]})
        assert r.is_error and "Nothing to draw" in text(r)
        assert m.job_info() is None


# ------------------------------------------------------------------ job control
async def test_pause_resume_cancel_lifts_pen(plotter):
    m, app = plotter
    async with mcp_client(app) as c:
        long = {"strokes": [{"points": [[200, 200], [1200, 200], [1200, 600], [200, 600], [200, 200]]}]}
        assert not (await c.call_tool("draw_strokes", long)).is_error
        # a second drawing while one runs is refused
        r = await c.call_tool("draw_text", {"text": "X", "x": 700, "y": 400})
        assert r.is_error and "already running" in text(r)
        r = await c.call_tool("move_to", {"x": 500, "y": 400})
        assert r.is_error and "already running" in text(r)
        await asyncio.sleep(0.5)
        assert "Hold sent" in text(await c.call_tool("pause_job", {}))
        await asyncio.sleep(0.3)
        assert "paused" in text(await c.call_tool("job_status", {}))
        assert "Resumed" in text(await c.call_tool("resume_job", {}))
        r = await c.call_tool("cancel_job", {})
        assert not r.is_error and "cancelled" in text(r)
        assert (await c.api.get("/api/state"))["pen"]["up"] is True
        assert (await c.call_tool("cancel_job", {})).is_error          # nothing left to cancel


async def test_emergency_stop_works_even_with_motion_disabled(plotter):
    m, app = plotter
    async with mcp_client(app) as c:
        long = {"strokes": [{"points": [[200, 200], [1200, 200], [1200, 600], [200, 600]]}]}
        assert not (await c.call_tool("draw_strokes", long)).is_error
        m.cfg = deep_update(m.cfg, {"mcp": {"allow_motion": False}})    # update_config refuses during a job
        await asyncio.sleep(0.4)
        r = await c.call_tool("emergency_stop", {})
        assert not r.is_error and "EMERGENCY STOP" in text(r) and "human" in text(r)
        await asyncio.sleep(1.0)
        s = await c.api.get("/api/state")
        assert s["job"]["state"] in ("cancelled", "error") and s["reference"]["uncertain"] is True


# ------------------------------------------------------------------ refusals
async def test_refusals_not_connected_and_no_reference():
    m = Machine(load_config())
    app = create_app(m, serve_ui=False)
    try:
        async with mcp_client(app) as c:
            assert "ready_to_draw: NO" in text(await c.call_tool("get_status", {}))
            r = await c.call_tool("draw_text", {"text": "A", "x": 700, "y": 400})
            assert r.is_error and "Not connected" in text(r) and "human" in text(r)
            assert not (await c.call_tool("draw_text", {"text": "A", "x": 700, "y": 400, "preview_only": True})).is_error
            m.connect("sim", None, None, {"speed": 60})
            r = await c.call_tool("draw_text", {"text": "A", "x": 700, "y": 400})
            assert r.is_error and "gondola position" in text(r)
            assert (await c.call_tool("move_to", {"x": 500, "y": 400})).is_error
    finally:
        m.shutdown()


async def test_allow_motion_false_is_preview_only(plotter):
    m, app = plotter
    m.update_config({"mcp": {"allow_motion": False}})
    async with mcp_client(app) as c:
        assert "motion disabled" in text(await c.call_tool("get_status", {}))
        for name, args in (("draw_text", {"text": "A", "x": 700, "y": 400}),
                           ("draw_pattern", {"id": "square-frame"}), ("move_to", {"x": 500, "y": 400}),
                           ("pen_up", {}), ("select_color", {"color": 2}),
                           ("draw_strokes", {"strokes": [{"points": SQUARE}], "dry_run": True})):
            r = await c.call_tool(name, args)
            assert r.is_error and "mcp.allow_motion" in text(r), name
        assert not (await c.call_tool("draw_text", {"text": "A", "x": 700, "y": 400, "preview_only": True})).is_error
        assert m.job_info() is None and m.simulator_info()["trace_segments"] == 0


async def test_input_limits_from_config(plotter):
    m, app = plotter
    m.update_config({"mcp": {"max_strokes": 3, "max_points": 12}})
    async with mcp_client(app) as c:
        r = await c.call_tool("draw_strokes", {"strokes": [{"points": SQUARE[:2]}] * 4})
        assert r.is_error and "Too many strokes" in text(r)
        r = await c.call_tool("draw_strokes", {"strokes": [{"points": SQUARE}] * 3})
        assert r.is_error and "Too many points" in text(r)
        r = await c.call_tool("draw_shape", {"kind": "circle", "x": 700, "y": 400, "radius": 50, "fill": True,
                                             "hatch_spacing_mm": 2})
        assert r.is_error and "Too many" in text(r)
        r = await c.call_tool("draw_shape", {"kind": "circle", "x": 700, "y": 400})
        assert r.is_error and "needs" in text(r)
        r = await c.call_tool("draw_text", {"text": "x" * 600, "x": 700, "y": 400})
        assert r.is_error and "too long" in text(r)
        assert m.job_info() is None


async def test_simple_moves_and_color(plotter):
    _, app = plotter
    async with mcp_client(app) as c:
        assert "moved to x=500.0 y=400.0" in text(await c.call_tool("move_to", {"x": 500, "y": 400}))
        r = await c.call_tool("move_to", {"x": 5000, "y": 400})
        assert r.is_error and "outside the permitted area" in text(r)
        assert "colour 2 (red)" in text(await c.call_tool("select_color", {"color": "red"}))
        assert "colour 3" in text(await c.call_tool("select_color", {"color": 3}))
        assert (await c.call_tool("select_color", {"color": 9})).is_error
        assert "pen up" in text(await c.call_tool("pen_up", {}))
        assert "colour: 3 (blue)" in text(await c.call_tool("get_status", {}))


async def test_confirm_required_for_real_hardware(plotter):
    """require_confirm_real_hardware: a non-sim transport needs confirm=true (state is faked, nothing is opened)."""
    _, app = plotter
    async with mcp_client(app) as c:
        ctx = await load_ctx(c.api)
        ctx.state = {**ctx.state, "connection": {**ctx.state["connection"], "transport": "serial"}}
        ctx.mcp = Mcp(require_confirm_real_hardware=True)
        d = Drawing.from_json({"strokes": [{"color": 0, "points": SQUARE}]})
        with pytest.raises(McpToolError) as e:
            await plan_and_run(c.api, ctx, d, DrawOptions())
        assert e.value.code == "confirm_required"
        out = await plan_and_run(c.api, ctx, d, DrawOptions(confirm=True, dry_run=True))
        assert "DRY RUN" in out[0].text
        await wait_job(c)


async def test_disabled_in_config_only_emergency_stop(plotter):
    m, app = plotter
    m.update_config({"mcp": {"enabled": False}})
    async with mcp_client(app) as c:
        for name, args in (("get_status", {}), ("draw_text", {"text": "A", "x": 700, "y": 400}),
                           ("preview_drawing", {"pattern": "star"})):
            r = await c.call_tool(name, args)
            assert r.is_error and "mcp.enabled" in text(r)
        assert not (await c.call_tool("emergency_stop", {})).is_error


# ------------------------------------------------------------------ mounted app: auth, real HTTP and stdio
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}}
HDR = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_mount_token_host_and_disabled(monkeypatch):
    monkeypatch.delenv("WBOT_MCP_TOKEN", raising=False)
    m = Machine(load_config())
    with TestClient(create_app(m, serve_ui=False), base_url="http://127.0.0.1:8000") as c:
        assert c.post("/mcp", json=INIT, headers=HDR).status_code == 200          # loopback, no token needed
        r = c.post("/mcp", json=INIT, headers={**HDR, "Host": "evil.example:8000"})
        assert r.status_code == 421                                                # DNS rebinding protection
        r = c.post("/mcp", json=INIT, headers={**HDR, "Origin": "http://evil.example"})
        assert r.status_code == 403
        monkeypatch.setenv("WBOT_MCP_TOKEN", "s3cret")
        assert c.post("/mcp", json=INIT, headers=HDR).status_code == 401
        assert c.post("/mcp", json=INIT, headers={**HDR, "Authorization": "Bearer nope"}).status_code == 401
        ok = c.post("/mcp", json=INIT, headers={**HDR, "Authorization": "Bearer s3cret"})
        assert ok.status_code == 200 and "wbot" in ok.text
        assert c.get("/api/state").status_code == 200                              # REST API itself is not gated
        monkeypatch.delenv("WBOT_MCP_TOKEN")
        m.update_config({"mcp": {"enabled": False}})
        assert c.post("/mcp", json=INIT, headers=HDR).status_code == 503
    m.shutdown()


class LiveServer:
    """The real app on a real localhost port (uvicorn thread) for HTTP and stdio sessions."""

    def __init__(self, app):
        import uvicorn
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        t0 = time.time()
        while not self.server.started and time.time() - t0 < 15:
            time.sleep(0.05)
        assert self.server.started
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(15)


async def test_real_http_session_with_token_and_shared_state(plotter, monkeypatch):
    import httpx2
    from mcp.client.streamable_http import streamable_http_client
    m, app = plotter
    monkeypatch.setenv("WBOT_MCP_TOKEN", "tok123")
    with LiveServer(app) as srv:
        assert httpx.post(srv.url + "/mcp", json=INIT, headers=HDR).status_code == 401
        hc = httpx2.AsyncClient(headers={"Authorization": "Bearer tok123"}, timeout=60)
        async with hc, Client(streamable_http_client(srv.url + "/mcp", http_client=hc)) as c:
            assert {t.name for t in (await c.list_tools()).tools} == EXPECTED_TOOLS
            s = text(await c.call_tool("get_status", {}))
            assert "connection: connected (sim" in s                  # same Machine as the web UI
            r = await c.call_tool("draw_shape", {"kind": "rect", "x": 600, "y": 400, "width": 30, "height": 20})
            assert not r.is_error
            for _ in range(200):
                if m.job_info() and m.job_info()["state"] == "done":
                    break
                await asyncio.sleep(0.2)
            assert m.job_info()["state"] == "done"
        assert httpx.get(srv.url + "/api/job").json()["state"] == "done"   # the web UI sees the AI's job


async def test_stdio_server_proxies_and_reports_missing_webserver(plotter):
    from mcp import StdioServerParameters, stdio_client

    async def session(api_url):
        params = StdioServerParameters(command=sys.executable, args=["-m", "wbot", "mcp", "--api", api_url],
                                       cwd=str(ROOT))
        async with Client(stdio_client(params)) as c:
            assert {t.name for t in (await c.list_tools()).tools} == EXPECTED_TOOLS
            return text(await c.call_tool("get_status", {})), await c.call_tool("get_board_info", {})

    _, app = plotter
    with LiveServer(app) as srv:
        status, board = await session(srv.url)
        assert "connection: connected (sim" in status and "x 120..1380" in text(board)
    with socket.socket() as s:                                       # a port where nothing listens
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"
    params = StdioServerParameters(command=sys.executable, args=["-m", "wbot", "mcp", "--api", dead], cwd=str(ROOT))
    async with Client(stdio_client(params)) as c:
        r = await c.call_tool("get_status", {})
        assert r.is_error and "not reachable" in text(r) and "python -m wbot serve" in text(r)
        r = await c.call_tool("emergency_stop", {})
        assert r.is_error and "not reachable" in text(r)
