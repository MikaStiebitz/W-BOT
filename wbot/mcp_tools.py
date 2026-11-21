"""Building blocks of the MCP server: REST client, validation, the draw pipeline, text formatting.

Everything here talks to the plotter only through the wbot REST API (``Api``), so every
safety rule of the Machine layer applies unchanged.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field

import httpx
from mcp_types import ImageContent, TextContent

from .config import Config, Mcp
from .drawing import Drawing, Stroke, clean, clip_to_rect, count_outside
from .mcp_render import render_png

Content = list[TextContent | ImageContent]

# What an AI should tell the user when the API refuses for a reason only a human can fix.
HUMAN_HINTS = {
    "not_connected": "A human must connect the plotter in the web UI (connect/disconnect is not available to the AI).",
    "no_reference": "A human must confirm the gondola position in the web UI (set reference); the AI cannot do this.",
    "alarm": "The controller is in alarm; a human must unlock it in the web UI after checking the machine.",
    "hold": "The machine is paused: use resume_job or cancel_job.",
    "job_running": "A job is running: wait for it (job_status), or pause_job / cancel_job.",
}


class McpToolError(Exception):
    """Reported to the AI as a tool error (``is_error``); the message is shown verbatim."""

    def __init__(self, message: str, code: str = "error"):
        super().__init__(message)
        self.message, self.code = message, code


class Api:
    """Thin async wrapper around the wbot REST API (in-process ASGI or remote HTTP)."""

    def __init__(self, client: httpx.AsyncClient, label: str):
        self.client, self.label = client, label
        self.job_lock = asyncio.Lock()      # serialises "check no job runs" + "start job" (see refuse_if_job_running)
        logging.getLogger("httpx").setLevel(logging.WARNING)     # one INFO line per request is noise

    async def request(self, method: str, path: str, **kw) -> dict | list | str | bytes:
        try:
            r = await self.client.request(method, path, **kw)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError, httpx.RemoteProtocolError, OSError) as exc:
            raise McpToolError(
                f"The W-BOT web server is not reachable ({self.label}): {exc.__class__.__name__}. "
                "Please start `python -m wbot serve` first (or give the right address with --api).",
                "server_down") from None
        except httpx.TimeoutException:
            raise McpToolError("Timed out waiting for the W-BOT web server.", "timeout") from None
        if r.status_code >= 400:
            try:
                body = r.json()
                msg, code = str(body.get("error") or r.text), str(body.get("code") or "error")
            except ValueError:
                msg, code = r.text[:300] or f"HTTP {r.status_code}", "error"
            hint = HUMAN_HINTS.get(code)
            raise McpToolError(f"{msg}" + (f" [{hint}]" if hint else ""), code)
        ct = r.headers.get("content-type", "")
        if ct.startswith("image/"):
            return r.content
        return r.json() if "json" in ct else r.text

    async def get(self, path: str):
        return await self.request("GET", path)

    async def post(self, path: str, body: dict | None = None):
        return await self.request("POST", path, json=body if body is not None else {})


@dataclass
class Ctx:
    """Config + state snapshot for one tool call."""
    cfg: Config
    state: dict
    mcp: Mcp = field(init=False)

    def __post_init__(self) -> None:
        self.mcp = self.cfg.mcp


async def load_ctx(api: Api) -> Ctx:
    cfg_json = await api.get("/api/config")
    state = await api.get("/api/state")
    try:
        cfg = Config.model_validate(cfg_json)
    except Exception as exc:  # noqa: BLE001
        raise McpToolError(f"The server configuration is not readable: {exc}", "bad_config") from exc
    return Ctx(cfg, state)


async def refuse_if_job_running(api: Api, what: str = "") -> None:
    """The REST layer would BLOCK (not refuse) a motion request while a job runs, until the job ended and then
    run it. An AI must get an immediate refusal instead, so check first."""
    job = await api.get("/api/job")
    if job and job["state"] in ("planning", "running", "paused"):
        raise McpToolError(f"A job is already running: {job_text(job)}. "
                           + (what or "Wait for it (job_status), or use pause_job / cancel_job."), "job_running")


# --------------------------------------------------------------------------- colours / limits
def resolve_color(value: int | str, cfg: Config) -> int:
    """1-based slot number or colour name -> 0-based slot used by Drawing/Stroke."""
    n = len(cfg.colors)
    names = ", ".join(f"{i + 1}={c.name}" for i, c in enumerate(cfg.colors))
    if isinstance(value, bool):
        raise McpToolError(f"Invalid colour {value!r}. Use a slot number 1..{n} or a name ({names}).")
    if isinstance(value, str):
        v = value.strip()
        if v.isdigit():
            value = int(v)
        else:
            for i, c in enumerate(cfg.colors):
                if c.name.lower() == v.lower():
                    return i
            raise McpToolError(f"Unknown colour name {value!r}. Available: {names}.")
    if not 1 <= int(value) <= n:
        raise McpToolError(f"Colour slot {value} does not exist. Use 1..{n} ({names}).")
    return int(value) - 1


def check_enabled(ctx: Ctx) -> None:
    if not ctx.mcp.enabled:
        raise McpToolError("MCP control is disabled in the configuration (mcp.enabled).", "mcp_disabled")


def check_motion_allowed(ctx: Ctx) -> None:
    check_enabled(ctx)
    if not ctx.mcp.allow_motion:
        raise McpToolError("Motion is blocked by the configuration (mcp.allow_motion = false): only status, "
                           "preview and stop are allowed. A human can change this in config/wbot.yaml.",
                           "motion_disabled")


def strokes_from_input(raw: list, ctx: Ctx) -> Drawing:
    """Validate AI-supplied strokes ([{color, points}]) against the configured limits."""
    mc = ctx.mcp
    if not raw:
        raise McpToolError("'strokes' is empty.")
    if len(raw) > mc.max_strokes:
        raise McpToolError(f"Too many strokes ({len(raw)} > limit {mc.max_strokes}). Simplify or split the drawing.")
    total = sum(len(s.points) for s in raw)
    if total > mc.max_points:
        raise McpToolError(f"Too many points ({total} > limit {mc.max_points}). Simplify the drawing.")
    out: list[Stroke] = []
    for i, s in enumerate(raw):
        if len(s.points) < 2:
            raise McpToolError(f"Stroke {i} has fewer than 2 points.")
        if any(len(p) != 2 for p in s.points):
            raise McpToolError(f"Stroke {i}: every point must be an [x, y] pair.")
        try:
            out.append(Stroke(s.points, resolve_color(s.color, ctx.cfg)))
        except ValueError as exc:
            raise McpToolError(f"Stroke {i}: {exc}") from None
    return Drawing(out)


def check_limits(d: Drawing, ctx: Ctx) -> None:
    mc = ctx.mcp
    if len(d.strokes) > mc.max_strokes:
        raise McpToolError(f"Too many strokes ({len(d.strokes)} > limit {mc.max_strokes}). Simplify or split.")
    if d.point_count() > mc.max_points:
        raise McpToolError(f"Too many points ({d.point_count()} > limit {mc.max_points}). Simplify.")


# --------------------------------------------------------------------------- formatting
def fmt_time(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s} s" if s < 90 else f"{s // 60} min {s % 60:02d} s"


def rect_text(r) -> str:
    return f"x {r[0]:g}..{r[2]:g}, y {r[1]:g}..{r[3]:g} mm"


def colors_text(cfg: Config) -> str:
    return ", ".join(f"{i + 1}={c.name} ({c.hex})" for i, c in enumerate(cfg.colors))


def readiness(state: dict, mcp: Mcp) -> list[str]:
    """Reasons why drawing is currently impossible (empty list = ready)."""
    why = []
    if not mcp.enabled:
        why.append("MCP disabled in config (mcp.enabled=false)")
    if not mcp.allow_motion:
        why.append("motion disabled in config (mcp.allow_motion=false)")
    if state["connection"]["status"] != "connected":
        why.append("not connected (a human must connect in the web UI)")
    elif not state["reference"]["set"]:
        why.append("no reference: a human must confirm the gondola position in the web UI")
    ms = state["machine"]["state"]
    if ms in ("alarm", "hold", "door"):
        why.append(f"machine state is '{ms}'" + (" (a human must unlock it)" if ms == "alarm" else ""))
    job = state.get("job")
    if job and job["state"] in ("planning", "running", "paused"):
        why.append(f"job {job['id']} is {job['state']}")
    return why


def status_text(state: dict, cfg: Config) -> str:
    c, m, pos, ref = state["connection"], state["machine"], state["position"], state["reference"]
    lines = [f"connection: {c['status']}" + (f" ({c['transport']}, {c['port']})" if c["transport"] else "")
             + (f", error: {c['error']}" if c.get("error") else "")]
    mach = m["state"] + (f", alarm {m['alarm']['code']}: {m['alarm']['text']}" if m.get("alarm") else "")
    lines.append(f"machine: {mach}" + (" (busy)" if m.get("busy") else ""))
    ph = pos.get("physical")
    lines.append(f"pen position: x={ph[0] + 0.0:.1f} y={ph[1] + 0.0:.1f} mm" if ph else "pen position: unknown (no reference)")
    lines.append("reference: set" + (" (restored from last session, verify!)" if ref.get("restored") else "")
                 + (" - UNCERTAIN after stop/reset, a human should re-reference" if ref.get("uncertain") else "")
                 if ref["set"] else "reference: NOT set (a human must confirm the gondola position in the web UI)")
    pen = state["pen"]
    lines.append("pen: " + ("unknown" if not pen.get("known") and pen.get("up") is None
                            else "up" if pen.get("up") else "down"))
    slot = state["revolver"]["slot"]
    lines.append("colour: " + (f"{slot} ({cfg.colors[slot - 1].name})" if slot and slot <= len(cfg.colors)
                               else "unknown/between slots"))
    lines.append("job: " + job_text(state.get("job")))
    for w in state.get("warnings") or []:
        lines.append(f"warning: {w}")
    why = readiness(state, cfg.mcp)
    lines.append("ready_to_draw: " + ("yes" if not why else "NO - " + "; ".join(why)))
    return "\n".join(lines)


def job_text(job: dict | None) -> str:
    if not job:
        return "none since the server started"
    t = (f"{job['id']} '{job['name']}' {job['state']}, {job['progress'] * 100:.0f}% "
         f"({job['line']}/{job['total']} lines), elapsed {fmt_time(job['elapsed_s'])}")
    if job["state"] in ("running", "paused"):
        t += f", ~{fmt_time(job['eta_s'])} left"
    if job.get("dry_run"):
        t += " [dry run, pen stays up]"
    if job.get("message"):
        t += f" - {job['message']}"
    return t


def board_info(ctx: Ctx) -> dict:
    cfg, st = ctx.cfg, ctx.state
    return {
        "board_mm": [cfg.board.width_mm, cfg.board.height_mm],
        "usable_rect_mm": list(st["limits"]["safe_rect"]),
        "coordinates": "origin lower-left corner, x right, y up, all values in millimetres",
        "colors": [{"slot": i + 1, "name": c.name, "hex": c.hex} for i, c in enumerate(cfg.colors)],
        "park_position_mm": list(cfg.motion.park_position),
        "draw_feed_mm_min": cfg.motion.draw_feed_mm_min,
        "travel_feed_mm_min": cfg.motion.travel_feed_mm_min,
        "limits": {"max_strokes": ctx.mcp.max_strokes, "max_points": ctx.mcp.max_points},
        "allow_motion": ctx.mcp.allow_motion,
        "require_confirm_real_hardware": ctx.mcp.require_confirm_real_hardware,
    }


def board_text(info: dict) -> str:
    r = info["usable_rect_mm"]
    return "\n".join([
        f"board: {info['board_mm'][0]:g} x {info['board_mm'][1]:g} mm; {info['coordinates']}",
        f"usable area for the pen: {rect_text(r)} (anything outside is clipped with a warning)",
        "pen colours (use the slot number or the name): "
        + ", ".join(f"{c['slot']}={c['name']} ({c['hex']})" for c in info["colors"]),
        f"park position: {info['park_position_mm'][0]:g}, {info['park_position_mm'][1]:g} mm; "
        f"draw speed {info['draw_feed_mm_min']:g} mm/min, travel {info['travel_feed_mm_min']:g} mm/min",
        f"limits per drawing: {info['limits']['max_strokes']} strokes, {info['limits']['max_points']} points",
        f"motion allowed: {'yes' if info['allow_motion'] else 'NO (preview only)'}",
    ])


def patterns_text(patterns: list[dict], ncolors: int) -> str:
    out = []
    for p in patterns:
        ps = []
        for q in p["params"]:
            if q["id"] == "color":
                ps.append(f"color (colour slot 1..{ncolors}, default 1)")
                continue
            d = f"{q['id']} ({q['kind']}, default {q['default']!r}"
            if q.get("min") is not None or q.get("max") is not None:
                d += f", {q.get('min')}..{q.get('max')}"
            if q.get("unit"):
                d += f" {q['unit']}"
            if q.get("choices"):
                d += ", one of " + "/".join(c["value"] for c in q["choices"])
            ps.append(d + ")")
        out.append(f"- {p['id']}: {p['name']} - {p['description']}\n    params: " + ("; ".join(ps) or "none"))
    return "\n".join(out)


# --------------------------------------------------------------------------- the draw pipeline
@dataclass
class DrawOptions:
    name: str = "AI drawing"
    dry_run: bool = False
    preview_only: bool = False
    confirm: bool = False
    allow_overlap: bool = False                              # draw over existing writing (camera check)
    show_travel: bool = True
    notes: list[str] = field(default_factory=list)          # informational, shown as "Note:"
    warnings: list[str] = field(default_factory=list)       # shown as "Warnings:"


OVERLAP_PCT = 0.5            # share of a drawing's lines touching existing ink that counts as "overlaps"


async def overlap_note(api: Api, d: Drawing) -> str | None:
    """With a calibrated camera: a message if the drawing would run over what is already on the board."""
    try:
        info = await api.get("/api/camera")
        if not (info.get("source") and info.get("calibrated")):
            return None
        ov = await api.post("/api/camera/overlap", {"drawing": d.to_json()})
    except McpToolError:
        return None
    if ov["overlap_pct"] < OVERLAP_PCT:
        return None
    msg = f"The drawing runs over writing that is already on the board ({ov['overlap_pct']:.0f} % of its lines touch ink"
    if ov["boxes"]:
        msg += "; occupied: " + ", ".join(rect_text(b) for b in ov["boxes"][:3])
    msg += ")."
    sg = ov.get("suggestion")
    if sg:
        msg += (f" Empty place of the same size: {rect_text(sg['rect'])}, i.e. shift everything by "
                f"dx={sg['dx']:+.0f} mm, dy={sg['dy']:+.0f} mm.")
    else:
        msg += " No empty place of that size was found: the board may need wiping."
    return msg


def _png_block(png: bytes) -> ImageContent:
    import base64
    return ImageContent(type="image", data=base64.b64encode(png).decode(), mime_type="image/png")


async def plan_and_run(api: Api, ctx: Ctx, drawing: Drawing, opt: DrawOptions, *,
                       with_image: bool = True) -> Content:
    """Clip -> preview (plan, estimate) -> render PNG -> (unless preview_only) start the job."""
    check_enabled(ctx)
    if not opt.preview_only:                      # refuse before doing any work
        check_motion_allowed(ctx)
        serial = ctx.state["connection"].get("transport") not in (None, "sim")
        if ctx.mcp.require_confirm_real_hardware and serial and not opt.dry_run and not opt.confirm:
            raise McpToolError("This drawing would run on REAL hardware and the operator requires an explicit "
                               "confirmation (mcp.require_confirm_real_hardware). Ask the user, then call again with "
                               "confirm=true (or use dry_run=true first).", "confirm_required")
        await refuse_if_job_running(api)
    check_limits(drawing, ctx)
    rect = ctx.cfg.safe_rect()
    warnings = list(opt.warnings)
    outside = count_outside(drawing, rect)
    d = clean(clip_to_rect(drawing, rect))
    if outside:
        warnings.append(f"{outside} point(s) lay outside the usable area ({rect_text(rect)}) and were clipped.")
    if not d.strokes:
        raise McpToolError(f"Nothing to draw: no stroke remains inside the usable area ({rect_text(rect)}).")
    d.name = opt.name
    note = await overlap_note(api, d)
    if note:
        if opt.preview_only or opt.dry_run or opt.allow_overlap:
            warnings.append(note)
        else:
            raise McpToolError(note + " Ask the user whether to draw over it (then call again with allow_overlap=true) "
                               "or move the drawing there.", "overlap")
    body = {"drawing": d.to_json(), "dry_run": bool(opt.dry_run and not opt.preview_only), "name": opt.name}
    pv = await api.post("/api/preview", body)
    for w in pv.get("warnings", []):
        if not (outside and "outside the permitted area" in w):
            warnings.append(w)
    pen_xy = tuple(ctx.state["position"]["physical"]) if ctx.state["position"].get("physical") else None
    content: Content = []
    stats = pv.get("stats", {})
    head = (f"{len(d.strokes)} strokes, {stats.get('draw_mm', d.length()):.0f} mm of lines, "
            f"{stats.get('travel_mm', 0):.0f} mm pen-up travel, {stats.get('color_changes', 0)} colour change(s), "
            f"estimated {fmt_time(pv['estimated_s'])}. Bounds: {rect_text(pv['bounds'])}. "
            f"Colours used: {', '.join(f'{c + 1} ({ctx.cfg.colors[c].name})' for c in d.colors())}.")
    if opt.preview_only:
        text = "PREVIEW ONLY (nothing moves). " + head
    else:
        async with api.job_lock:
            await refuse_if_job_running(api)
            job = await api.post("/api/jobs", body)
        text = (f"Job {job['id']} started{' (DRY RUN: pen stays up)' if job['dry_run'] else ''}: "
                f"'{opt.name}'. " + head +
                " It runs in the background: poll job_status every 10-30 s and do not start another drawing "
                "until it is done (cancel_job / pause_job / emergency_stop are available).")
    if warnings:
        text += "\nWarnings: " + " | ".join(dict.fromkeys(warnings))
    content.append(TextContent(type="text", text=text))
    if with_image:
        png = await asyncio.to_thread(render_png, d, ctx.cfg, show_travel=opt.show_travel, pen_xy=pen_xy)
        content.append(_png_block(png))
    return content


def to_json_text(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


__all__ = ["Api", "McpToolError", "Ctx", "load_ctx", "resolve_color", "strokes_from_input", "plan_and_run",
           "DrawOptions", "status_text", "job_text", "board_info", "board_text", "patterns_text", "readiness",
           "check_motion_allowed", "check_enabled", "to_json_text", "fmt_time", "refuse_if_job_running"]
