"""Canvas-editor elements -> one drawing.

An *element* is a small JSON object the UI keeps and moves around: text, formula, function plot, symbol, image or
test pattern. ``x``/``y`` is the element's centre on the board (mm), ``rot`` the rotation in degrees, ``color`` the
pen slot. :func:`compose` renders them all into a single :class:`Drawing` (the same one the preview shows and the
job plots) and reports each element's bounding box and stroke range so the UI can select and drag them.
"""
from __future__ import annotations

import io
import logging
import math
import os
from functools import lru_cache

import numpy as np

from .config import Config
from .drawing import Drawing, Stroke
from .icons import EMOJI_ICON, ICONS, icon_list, icon_strokes
from .mathglyphs import special as math_special
from .mathlayout import MathError, layout_math
from .patterns import build_pattern
from .plotgraph import ExprError, render_plot
from .textfont import FONTS, font_list, measure, symbol_glyph, text_strokes

log = logging.getLogger(__name__)

TYPES = ("text", "math", "plot", "symbol", "image", "pattern")
MAX_ELEMENTS = 80
MAX_STROKES = 60000
MAX_TEXT = 4000


class ElementError(ValueError):
    """One element cannot be rendered (the message is shown next to it)."""


def _num(el: dict, key: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(el.get(key, default))
    except (TypeError, ValueError):
        raise ElementError(f"“{key}” is not a number.") from None
    if not math.isfinite(v):
        raise ElementError(f"“{key}” is not a number.")
    return min(max(v, lo), hi)


def _centered(strokes: list[Stroke]) -> list[Stroke]:
    pts = [s.pts for s in strokes if len(s.pts)]
    if not pts:
        return []
    a = np.concatenate(pts)
    c = (a.min(axis=0) + a.max(axis=0)) / 2
    return [Stroke(s.pts - c, s.color) for s in strokes]


def _scale_to(strokes: list[Stroke], size: float) -> list[Stroke]:
    """Centre on the origin and scale so the larger side of the bounding box is ``size`` mm."""
    strokes = _centered(strokes)
    pts = [s.pts for s in strokes]
    if not pts:
        return []
    a = np.concatenate(pts)
    span = float((a.max(axis=0) - a.min(axis=0)).max())
    k = size / span if span > 1e-9 else 1.0
    return [Stroke(s.pts * k, s.color) for s in strokes]


def _recolor(strokes: list[Stroke], color: int) -> list[Stroke]:
    return [Stroke(s.pts, color) for s in strokes]


# ------------------------------------------------------------------------------------------------ emoji
_EMOJI_FONTS = [r"C:\Windows\Fonts\seguiemj.ttf", "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
                "/usr/share/fonts/noto/NotoColorEmoji.ttf", "/System/Library/Fonts/Apple Color Emoji.ttc",
                "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf"]


@lru_cache(maxsize=256)
def _trace_emoji(char: str) -> tuple[tuple, ...]:
    from PIL import Image, ImageDraw, ImageFont

    from .imagetrace import ImageError, TraceOptions, trace_image
    path = next((p for p in _EMOJI_FONTS if os.path.exists(p)), None)
    if path is None:
        raise ElementError("No emoji font was found on this computer. Pick one of the drawn symbols instead.")
    try:
        font = ImageFont.truetype(path, 109 if "Noto" in path else 160)
        size = 260
        im = Image.new("RGBA", (size, size), (255, 255, 255, 255))
        ImageDraw.Draw(im).text((size // 2, size // 2), char, font=font, anchor="mm", embedded_color=True)
        buf = io.BytesIO()
        im.save(buf, "PNG")
        cfg = Config()
        res = trace_image(buf.getvalue(), cfg, TraceOptions(mode="photo", detail=60, fit_rect=(0, 0, 100, 100)))
    except (ImageError, OSError, ValueError) as exc:
        raise ElementError(f"This emoji cannot be drawn ({exc}).") from exc
    return tuple(tuple(map(tuple, s.pts.tolist())) for s in res.drawing.strokes)


def _emoji(char: str, size: float, color: int) -> list[Stroke]:
    icon = EMOJI_ICON.get(char) or EMOJI_ICON.get(char.replace("\ufe0f", ""))
    if icon:
        return icon_strokes(icon, size, color)
    raw = _trace_emoji(char.replace("\ufe0f", ""))
    return _scale_to([Stroke(np.array(p), color) for p in raw], size)


# ------------------------------------------------------------------------------------------------ renderers
def _r_text(el: dict, cfg: Config) -> list[Stroke]:
    text = str(el.get("text", ""))
    if len(text) > MAX_TEXT:
        raise ElementError("The text is too long.")
    if not text.strip():
        return []
    size = _num(el, "size", 20, 1.5, 600)
    font = el.get("font", "simple")
    if font not in FONTS:
        font = "simple"
    align = el.get("align", "center")
    align = align if align in ("left", "center", "right") else "center"
    kw = dict(font=font, mono=bool(el.get("mono")), tracking=_num(el, "tracking", 0, -3, 20),
              line_spacing=_num(el, "spacing", 1.0, 0.5, 3.0))
    w, _ = measure(text, size, **kw)
    x = {"left": -w / 2, "center": 0.0, "right": w / 2}[align]
    return text_strokes(text, size, x, 0, anchor=align, valign="center", color=int(el.get("color", 0)), **kw)


def _r_math(el: dict, cfg: Config) -> list[Stroke]:
    try:
        strokes, w, asc, desc = layout_math(str(el.get("latex", "")))
    except MathError as exc:
        raise ElementError(str(exc)) from exc
    k = _num(el, "size", 20, 2, 600) / 14.0
    col = int(el.get("color", 0))
    return _centered([Stroke(p * k, col) for p in strokes if len(p) >= 2])


def _r_plot(el: dict, cfg: Config) -> list[Stroke]:
    try:
        return render_plot({**el, "color": int(el.get("color", 0))})
    except ExprError as exc:
        raise ElementError(str(exc)) from exc


def _r_symbol(el: dict, cfg: Config) -> list[Stroke]:
    kind, sid = el.get("kind", "icon"), str(el.get("sym", ""))
    size, col = _num(el, "size", 40, 2, 1200), int(el.get("color", 0))
    if kind == "icon":
        if sid not in ICONS:
            raise ElementError("Unknown symbol.")
        return icon_strokes(sid, size, col)
    if kind == "emoji":
        if not sid or len(sid) > 8:
            raise ElementError("Type or paste one emoji.")
        return _emoji(sid, size, col)
    if kind == "glyph":
        if sid.startswith("special:"):
            sp = math_special(sid[8:])
            polys = sp[1] if sp else []
        else:
            sets, _, code = sid.partition(":")
            g = symbol_glyph(sets, int(code)) if code.isdigit() else None
            polys = g[1] if g else []
        if not polys:
            raise ElementError("Unknown symbol.")
        return _scale_to([Stroke(np.array(p, float), col) for p in polys if len(p) >= 2], size)
    raise ElementError("Unknown symbol.")


def _r_image(el: dict, cfg: Config) -> list[Stroke]:
    try:
        d = Drawing.from_json(el.get("drawing") or {})
    except ValueError as exc:
        raise ElementError(str(exc)) from exc
    size = el.get("size")
    return _scale_to(d.strokes, float(size)) if size else _centered(d.strokes)


def _r_pattern(el: dict, cfg: Config) -> list[Stroke]:
    try:
        d = build_pattern(cfg, el.get("pattern"), el.get("params") or {})
    except (ValueError, KeyError) as exc:
        raise ElementError(f"Invalid pattern: {exc}") from exc
    size = el.get("size")
    return _scale_to(d.strokes, float(size)) if size else _centered(d.strokes)


_RENDER = {"text": _r_text, "math": _r_math, "plot": _r_plot, "symbol": _r_symbol, "image": _r_image,
           "pattern": _r_pattern}


def render_element(el: dict, cfg: Config) -> Drawing:
    """The element placed on the board (rotated about its centre and moved to ``x``, ``y``)."""
    kind = el.get("type")
    if kind not in _RENDER:
        raise ElementError("Unknown element type.")
    strokes = _RENDER[kind](el, cfg)
    rot = math.radians(_num(el, "rot", 0, -3600, 3600))
    x, y = _num(el, "x", 0, -1e5, 1e5), _num(el, "y", 0, -1e5, 1e5)
    c, s = math.cos(rot), math.sin(rot)
    r = np.array([[c, -s], [s, c]])
    return Drawing([Stroke(p.pts @ r.T + [x, y], p.color) for p in strokes if len(p.pts) >= 2], kind)


def compose(elements: list[dict], cfg: Config) -> tuple[Drawing, list[dict]]:
    """All elements in one drawing plus per-element metadata (``id``, ``bbox``, ``range``, ``error``)."""
    if len(elements) > MAX_ELEMENTS:
        raise ElementError(f"At most {MAX_ELEMENTS} elements are supported.")
    out, meta = Drawing([], "elements"), []
    for i, el in enumerate(elements):
        info = {"id": el.get("id", i), "bbox": None, "range": [len(out.strokes), len(out.strokes)], "error": None}
        try:
            d = render_element(el, cfg)
            if len(out.strokes) + len(d.strokes) > MAX_STROKES:
                raise ElementError("Too many lines for one drawing.")
            out.extend(d)
            info["range"][1] = len(out.strokes)
            b = d.bounds()
            info["bbox"] = [round(v, 2) for v in b] if b else None
        except ElementError as exc:
            info["error"] = str(exc)
        meta.append(info)
    return out, meta


# ------------------------------------------------------------------------------------------------ catalog
GLYPH_GROUPS = [
    ("Math", [("mathupp", c) for c in (1, 3, 88, 6, 7, 31, 32, 63, 62, 66, 80, 27, 26, 77, 78, 72, 68, 69, 71, 86, 90, 92, 94, 64)]),
    ("Arrows", [("mathupp", 73), ("mathupp", 75), ("mathupp", 74), ("mathupp", 76)] + [("special", c) for c in "⇒⇐⇔↔⟶"]),
    ("Shapes", [("symbolic", c) for c in (40, 41, 42, 43, 44, 94, 45, 46, 47, 62, 64, 48, 49, 50)]),
    ("Fun", [("symbolic", c) for c in (77, 78, 79, 80, 65, 66, 67, 68, 72, 54, 56, 73, 74, 75, 55)]),
    ("Greek", [("greeks", ord(c) - 32) for c in "abcdefghijklmnopqrstuvwx"] + [("greeks", ord(c) - 32) for c in "HKNPRUWX"]),
]
ASCII_ART = [
    ("Cat", " /\\_/\\\n( o.o )\n > ^ <"),
    ("Heart", " ##   ##\n#### ####\n#########\n #######\n  #####\n   ###\n    #"),
    ("House", "   /\\\n  /  \\\n /____\\\n |    |\n |  []|\n |____|"),
    ("Robot", " [^_^]\n/|___|\\\n  | |\n _| |_"),
    ("Smile", "  ___\n /o o\\\n(  >  )\n \\ _ /"),
    ("Box", "+-------+\n|       |\n|  W-BOT|\n+-------+"),
]
PLOT_PRESETS = [
    ("Sine and cosine", {"xmin": -6.5, "xmax": 6.5, "ymin": -1.6, "ymax": 1.6,
                         "curves": [{"kind": "y", "expr": "sin(x)", "color": 0}, {"kind": "y", "expr": "cos(x)", "color": 1}]}),
    ("Parabola", {"xmin": -4, "xmax": 4, "ymin": -2, "ymax": 10, "curves": [{"kind": "y", "expr": "x^2", "color": 0}]}),
    ("Bell curve", {"xmin": -4, "xmax": 4, "ymin": -0.1, "ymax": 1.1, "curves": [{"kind": "y", "expr": "exp(-x^2/2)", "color": 0}]}),
    ("Circle", {"xmin": -2, "xmax": 2, "ymin": -2, "ymax": 2, "curves": [{"kind": "param", "expr": "cos(t)", "expr2": "sin(t)", "t0": 0, "t1": 6.2832}]}),
    ("Spiral", {"xmin": -8, "xmax": 8, "ymin": -8, "ymax": 8, "curves": [{"kind": "polar", "expr": "t/2", "t0": 0, "t1": 25}]}),
]


def _flat(strokes) -> list[list[float]]:
    return [[round(float(v), 2) for p in s for v in p] for s in strokes if len(s) >= 2]


@lru_cache(maxsize=1)
def catalog() -> dict:
    """Everything the UI palettes need (small thumbnails as flat stroke lists)."""
    fonts = []
    for f in font_list():
        st = text_strokes("Abc 123", 10, 0, 0, font=f["id"])
        fonts.append({**f, "sample": _flat(s.pts for s in st)})
    icons = [{**i, "strokes": _flat(s.pts for s in icon_strokes(i["id"], 100))} for i in icon_list()]
    groups = []
    for name, items in GLYPH_GROUPS:
        gl = []
        for sets, code in items:
            if sets == "special":
                sp = math_special(code)
                polys, sid = (sp[1] if sp else []), f"special:{code}"
            else:
                g = symbol_glyph(sets, code)
                polys, sid = (g[1] if g else []), f"{sets}:{code}"
            if polys:
                gl.append({"id": sid, "strokes": _flat([np.array(p) for p in polys])})
        groups.append({"name": name, "glyphs": gl})
    return {"fonts": fonts, "icons": icons, "glyph_groups": groups,
            "ascii_art": [{"name": n, "text": t} for n, t in ASCII_ART],
            "plot_presets": [{"name": n, "spec": s} for n, s in PLOT_PRESETS],
            "emoji": ["😀", "😎", "🤔", "😂", "🥳", "👍", "🎉", "🔥", "🌈", "🐱", "🐶", "🍕", "⚽", "🚗", "✈", "🎸"]}


__all__ = ["compose", "render_element", "catalog", "ElementError", "TYPES"]
