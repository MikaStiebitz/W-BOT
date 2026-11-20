"""PNG preview of a planned drawing, so an AI can *see* what the plotter will draw.

Pure Pillow, no hardware: board outline, usable rectangle, 100 mm grid, strokes in the
configured pen colours and (optionally) the pen-up travel between strokes as thin grey
lines. Board coordinates: origin lower left, x right, y up, millimetres. Rendered at 2x
and downsampled for anti-aliasing.
"""
from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from .config import Config
from .drawing import Drawing, optimize_order

MARGIN_PX = 34          # space around the board for axis labels
LEGEND_PX = 40          # strip below the board
SS = 2                  # supersampling factor


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                                   # very old Pillow
        return ImageFont.load_default()


def _rgb(hexstr: str) -> tuple[int, int, int]:
    h = hexstr.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return 17, 17, 17


def render_png(drawing: Drawing, cfg: Config, *, width_px: int = 1000, show_travel: bool = True,
               pen_xy: tuple[float, float] | None = None, grid_mm: float = 100.0) -> bytes:
    """Render ``drawing`` (already clipped, board mm) and return PNG bytes.

    ``pen_xy`` = current physical pen position (marked with a ring) and the start of the
    first travel move; default is the park position. Strokes are ordered like the planner
    orders them, so the grey travel lines match the real job.
    """
    bw, bh = cfg.board.width_mm, cfg.board.height_mm
    k = width_px * SS / bw                               # pixels per mm (supersampled)
    m, lg = MARGIN_PX * SS, LEGEND_PX * SS
    W, H = int(bw * k) + 2 * m, int(bh * k) + 2 * m + lg
    img = Image.new("RGB", (W, H), (250, 250, 247))
    d = ImageDraw.Draw(img)

    def P(x: float, y: float) -> tuple[float, float]:
        return m + x * k, m + (bh - y) * k

    # board surface + grid
    d.rectangle([P(0, bh), P(bw, 0)], fill=(255, 255, 255), outline=(60, 60, 60), width=2 * SS)
    small = _font(11 * SS)
    g = 0.0
    while g <= bw + 1e-6:
        d.line([P(g, 0), P(g, bh)], fill=(228, 228, 228), width=SS)
        d.text((P(g, 0)[0], P(g, 0)[1] + 3 * SS), f"{g:g}", fill=(120, 120, 120), font=small, anchor="mt")
        g += grid_mm
    g = 0.0
    while g <= bh + 1e-6:
        d.line([P(0, g), P(bw, g)], fill=(228, 228, 228), width=SS)
        d.text((P(0, g)[0] - 4 * SS, P(0, g)[1]), f"{g:g}", fill=(120, 120, 120), font=small, anchor="rm")
        g += grid_mm

    # usable rectangle (where the pen can reach)
    x0, y0, x1, y1 = cfg.safe_rect()
    d.rectangle([P(x0, y1), P(x1, y0)], outline=(46, 125, 50), width=2 * SS)
    d.text((P(x0, y1)[0] + 4 * SS, P(x0, y1)[1] + 3 * SS), "usable area", fill=(46, 125, 50), font=small)

    # order like the planner, then travel lines, then strokes
    start = pen_xy or tuple(cfg.motion.park_position)
    ordered = optimize_order(drawing, start)
    colors = [_rgb(c.hex) for c in cfg.colors]
    if show_travel and ordered.strokes:
        cur = start
        for s in ordered.strokes:
            d.line([P(*cur), P(*s.start)], fill=(176, 176, 176), width=SS)
            cur = tuple(s.end)
    lw = max(2 * SS, int(round(2.0 * k)))                # ~2 mm marker line
    for s in ordered.strokes:
        col = colors[min(max(s.color, 0), len(colors) - 1)]
        pts = [P(float(x), float(y)) for x, y in s.pts]
        if len(pts) >= 2:
            d.line(pts, fill=col, width=lw, joint="curve")
            r = lw / 2
            for p in (pts[0], pts[-1]):                  # round caps
                d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=col)

    if pen_xy is not None:                               # current pen position
        px, py = P(*pen_xy)
        r = 7 * SS
        d.ellipse([px - r, py - r, px + r, py + r], outline=(211, 47, 47), width=2 * SS)
        d.line([(px - r, py), (px + r, py)], fill=(211, 47, 47), width=SS)
        d.line([(px, py - r), (px, py + r)], fill=(211, 47, 47), width=SS)

    # legend
    font = _font(12 * SS)
    ly = m + int(bh * k) + 26 * SS
    lx = m
    d.text((lx, ly), "pen colours:", fill=(60, 60, 60), font=font, anchor="lm")
    lx += 92 * SS
    for i, c in enumerate(cfg.colors):
        d.rectangle([lx, ly - 6 * SS, lx + 14 * SS, ly + 6 * SS], fill=_rgb(c.hex))
        label = f"{i + 1} {c.name}"
        d.text((lx + 20 * SS, ly), label, fill=(60, 60, 60), font=font, anchor="lm")
        lx += (20 + 9 * len(label) + 16) * SS
    st = ordered
    info = f"{len(st.strokes)} strokes, {st.length():.0f} mm drawn"
    d.text((W - m, ly), info, fill=(60, 60, 60), font=font, anchor="rm")

    out = img.resize((W // SS, H // SS), Image.LANCZOS)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


__all__ = ["render_png"]
