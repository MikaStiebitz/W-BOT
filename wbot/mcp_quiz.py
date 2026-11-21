"""Quiz / tutor tools for the MCP server: the assistant writes a task with an empty answer box on the board, the
user writes the answer by hand and says so in the chat, and the assistant reads the box with the camera.

    ask_on_board      task text + answer box in one job, placed in empty space (camera) or where asked
    read_board_area   sharp top-down camera crop of an area (default: the last answer box) + how much is written
    find_free_space   empty places of a given size, seen by the camera

The plotter cannot erase: a used area stays written until a human wipes it.
"""
from __future__ import annotations

from typing import Any

from mcp_types import ImageContent, TextContent

from .drawing import Drawing
from .mcp_shapes import ShapeSpec, build_shape, build_text
from .mcp_tools import McpToolError, resolve_color

BOX_INSET_MM = 15.0         # band along the box edges where its own outline may lie (not part of the answer)
WRITTEN_PCT = 0.4           # ink share of the box that counts as "something is written"

QUIZ_PROMPT = """\
Play a whiteboard quiz with the user{topic}. The plotter writes, the user answers by hand, the camera checks.

1. get_status: the plotter must be ready to draw; the camera must be set up (camera_snapshot works).
2. Think of ONE short task (a calculation, a word to translate, a question with a short answer{level}).
3. ask_on_board(question=...): it writes the task and draws an empty answer box below it in a free part of the \
board. Wait for the job to finish (job_status), then tell the user to write the answer into the box and to say \
when they are done.
4. When the user says they are done: read_board_area() (no arguments = the last answer box). Read the handwriting \
in the image yourself. If it says the area is blocked, ask the user to step aside and read again. If nothing is \
written, say so.
5. Tell the user whether the answer is right, explain briefly, keep score, and ask_on_board the next task. The \
plotter cannot erase: when no free space is left, ask the user to wipe the board.
"""


def register_quiz_tools(server, tool, ro, draw_ann, api, ctx, draw) -> None:
    state: dict[str, Any] = {"answer": None, "question": None}

    async def _camera_ready() -> dict | None:
        info = await api.get("/api/camera")
        return info if info.get("source") and info.get("calibrated") else None

    @tool(draw_ann)
    async def ask_on_board(question: str, answer_width_mm: float | None = None, answer_height_mm: float = 120.0,
                           text_height_mm: float = 30.0, x: float | None = None, y: float | None = None,
                           color: int | str = 1, box_color: int | str | None = None, gap_mm: float = 15.0,
                           dry_run: bool = False, preview_only: bool = False, confirm: bool = False, allow_overlap: bool = False) -> Any:
        """Write a task or question on the board and draw an empty answer box below it, for the user to answer by \
hand (quiz, tutoring, vocabulary, maths ...). Without x/y it is placed in empty space found by the camera (or the \
middle of the usable area without a camera); with x/y, (x, y) is the TOP-LEFT corner of the whole block in mm. Use \
\\n for several lines. The answer box is remembered: after the job and the user's "done", call read_board_area() \
to see what they wrote. Returns the job, the answer box in mm and a PNG preview."""
        c = await ctx()
        col = resolve_color(color, c.cfg)
        bcol = col if box_color is None else resolve_color(box_color, c.cfg)
        _, (tw, th) = build_text(question, 0.0, 0.0, text_height_mm, col, "left", "top", 1.0)
        bw = float(answer_width_mm) if answer_width_mm else max(tw, 250.0)
        bh = float(answer_height_mm)
        if bw < 40 or bh < 30:
            raise McpToolError("The answer box must be at least 40 x 30 mm.")
        W, H = max(tw, bw), th + gap_mm + bh
        notes = []
        if x is None or y is None:
            left, top = None, None
            if await _camera_ready():
                try:
                    fs = await api.get(f"/api/camera/free-space?w={W:.0f}&h={H:.0f}&margin=20&count=1")
                    p = (fs.get("places") or [None])[0]
                    if p and p["occupied_pct"] < 1.0:
                        left, top = p["rect"][0], p["rect"][3]
                        notes.append("placed in empty space seen by the camera")
                    else:
                        notes.append("the camera found no empty space that big: placed in the middle. Ask the user "
                                     "to wipe the board if it is full")
                except McpToolError as exc:
                    notes.append(f"camera free-space search failed ({exc.message}): placed in the middle")
            else:
                notes.append("no calibrated camera: placed in the middle of the usable area, check it is empty")
            if left is None:
                r = c.cfg.safe_rect()
                left, top = (r[0] + r[2] - W) / 2, (r[1] + r[3] + H) / 2
        else:
            left, top = float(x), float(y)
        strokes, _ = build_text(question, left, top, text_height_mm, col, "left", "top", 1.0)
        box = (left, top - H, left + bw, top - H + bh)
        strokes += build_shape(ShapeSpec(kind="rect", x=box[0], y=box[1], width=bw, height=bh, color=bcol), bcol,
                               c.cfg.safe_rect())
        res = await draw(c, Drawing(strokes), "Quiz " + " ".join(question.split())[:20], dry_run, preview_only,
                         confirm, allow_overlap=allow_overlap, notes=notes)
        if not preview_only:
            state["answer"], state["question"] = [round(v, 1) for v in box], question
        res.insert(1, TextContent(type="text", text=(
            f"Answer box: x {box[0]:.0f}..{box[2]:.0f} mm, y {box[1]:.0f}..{box[3]:.0f} mm"
            + (" (remembered for read_board_area)" if not preview_only else " (preview: not remembered)")
            + ". " + "; ".join(notes))))
        return res

    @tool(ro)
    async def read_board_area(x0: float | None = None, y0: float | None = None, x1: float | None = None,
                              y1: float | None = None, px_per_mm: float = 4.0, hide_objects: bool = False) -> Any:
        """Look closely at one area of the board with the camera: a sharp, straightened top-down crop (x right, \
y up) plus how much is written in it. Without coordinates it reads the last answer box from ask_on_board. Use it \
after the user says they wrote their answer, then read the handwriting in the image yourself. If something is in \
front of the area (blocked), ask the user to step aside and try again (hide_objects=true replaces it by the \
remembered background, which may also hide fresh writing). Never moves anything."""
        await ctx()
        if None in (x0, y0, x1, y1):
            if any(v is not None for v in (x0, y0, x1, y1)):
                raise McpToolError("Give all of x0, y0, x1, y1, or none for the last answer box.")
            if not state["answer"]:
                raise McpToolError("No answer box yet: use ask_on_board first, or give x0, y0, x1, y1 in mm.")
            rect, inset = state["answer"], BOX_INSET_MM
        else:
            rect, inset = [x0, y0, x1, y1], 0.0
        if not await _camera_ready():
            raise McpToolError("No calibrated camera. A human selects and aligns it in the web UI (Camera tab).",
                               "no_camera")
        r = await api.post("/api/camera/region", {"rect": rect, "px_per_mm": px_per_mm, "hide": hide_objects,
                                                  "frames": 3, "inset_mm": inset})
        x0_, y0_, x1_, y1_ = r["rect"]
        lines = [f"board area x {x0_:g}..{x1_:g} mm, y {y0_:g}..{y1_:g} mm, {r['px_per_mm']:g} px per mm "
                 f"(image top = y {y1_:g})"]
        if state["answer"] and rect == state["answer"] and state["question"]:
            lines.append(f"this is the answer box for the task: {state['question']!r}")
        written = r["ink_pct"] >= WRITTEN_PCT
        lines.append(f"ink covers {r['ink_pct']:.1f} % of the area: "
                     + (f"something is written (x {r['ink_bbox'][0]:g}..{r['ink_bbox'][2]:g}, "
                        f"y {r['ink_bbox'][1]:g}..{r['ink_bbox'][3]:g} mm)" if written and r["ink_bbox"]
                        else "looks EMPTY (nothing written yet?)"))
        if r["blocked"]:
            lines.append("WARNING: something is in front of the board over this area"
                         + (" (replaced by the remembered background, so fresh writing may be missing)"
                            if r["hidden_objects"] else "") + ": ask the user to step aside and read again")
        if r["visible_pct"] < 95:
            lines.append(f"only {r['visible_pct']:.0f} % of the area is in the camera picture (black = not seen)")
        img = r["image"].split(",", 1)[1]
        return [TextContent(type="text", text="\n".join(lines)),
                ImageContent(type="image", data=img, mime_type="image/jpeg")]

    @tool(ro)
    async def find_free_space(width_mm: float, height_mm: float, margin_mm: float = 20.0) -> str:
        """Empty places of width_mm x height_mm in the usable area, seen by the camera (existing writing plus \
margin_mm around it counts as occupied). Emptiest and most central first; rects are [x0, y0, x1, y1] mm."""
        await ctx()
        if not await _camera_ready():
            raise McpToolError("No calibrated camera. A human selects and aligns it in the web UI (Camera tab).",
                               "no_camera")
        fs = await api.get(f"/api/camera/free-space?w={width_mm:.0f}&h={height_mm:.0f}&margin={margin_mm:.0f}&count=3")
        if not fs["places"]:
            return "No place of that size found."
        rows = [f"x {p['rect'][0]:g}..{p['rect'][2]:g}, y {p['rect'][1]:g}..{p['rect'][3]:g} mm: "
                f"{'empty' if p['occupied_pct'] == 0 else str(p['occupied_pct']) + ' % occupied'}" for p in fs["places"]]
        return "Free places (best first):\n" + "\n".join(rows)

    @server.prompt(name="board_quiz", title="Whiteboard quiz",
                   description="Quiz or tutor game: tasks on the board, handwritten answers checked by camera")
    def board_quiz(topic: str = "", level: str = "") -> str:
        return QUIZ_PROMPT.format(topic=f" about {topic}" if topic else "",
                                  level=f", level: {level}" if level else "")


__all__ = ["register_quiz_tools", "QUIZ_PROMPT"]
