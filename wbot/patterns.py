"""Test images for verifying accuracy and the hardware.

``REGISTRY`` maps a pattern id to a :class:`Pattern` (name, description and
parameter labels for the UI). Every pattern builds a :class:`Drawing` in board mm
that lies inside ``cfg.safe_rect()`` (the result is clipped as a last resort).
Use :func:`build_pattern` to fill defaults and validate parameters.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from .config import Config
from .drawing import Drawing, Stroke, clean, clip_to_rect
from .textfont import measure, text_strokes


# ------------------------------------------------------------------------------ infrastructure
@dataclass
class Param:
    id: str
    label: str
    kind: str                                   # float | int | bool | choice | str
    default: Any
    min: float | None = None
    max: float | None = None
    choices: list[tuple[str, str]] | None = None  # (value, label)
    unit: str | None = None

    def to_dict(self) -> dict:
        return {"id": self.id, "label": self.label, "kind": self.kind, "default": self.default,
                "min": self.min, "max": self.max, "unit": self.unit,
                "choices": None if self.choices is None else [{"value": v, "label": l} for v, l in self.choices]}

    def coerce(self, value: Any) -> Any:
        k, name = self.kind, self.label
        try:
            if k == "bool":
                if isinstance(value, str):
                    return value.strip().lower() in ("1", "true", "ja", "yes", "on")
                return bool(value)
            if k == "str":
                s = str(value)
                if self.max is not None and len(s) > self.max:
                    raise ValueError(f"'{name}' is too long (max. {int(self.max)} characters)")
                return s
            if k == "choice":
                if str(value) not in [v for v, _ in (self.choices or [])]:
                    raise ValueError(f"'{name}': invalid choice '{value}'")
                return str(value)
            if isinstance(value, bool) or value is None:
                raise TypeError
            v = int(value) if k == "int" else float(value)
            if k == "int" and float(value) != v:
                raise ValueError(f"'{name}' must be a whole number")
        except (TypeError, ValueError) as e:
            if isinstance(e, ValueError) and "'" in str(e):
                raise
            raise ValueError(f"'{name}': invalid value '{value}'") from None
        if not math.isfinite(v):
            raise ValueError(f"'{name}': invalid value")
        if (self.min is not None and v < self.min) or (self.max is not None and v > self.max):
            unit = f" {self.unit}" if self.unit else ""
            raise ValueError(f"'{name}' must be between {self.min:g} and {self.max:g}{unit} (is {v:g})")
        return v


@dataclass
class Pattern:
    id: str
    name: str
    description: str
    params: list[Param]
    build: Callable[[Config, dict], Drawing]

    def defaults(self) -> dict:
        return {p.id: p.default for p in self.params}

    def resolve(self, values: dict | None) -> dict:
        values = dict(values or {})
        out = self.defaults()
        known = {p.id: p for p in self.params}
        for k, v in values.items():
            if k not in known:
                raise ValueError(f"Unknown parameter '{k}' for test pattern '{self.name}'")
            out[k] = known[k].coerce(v)
        return out


REGISTRY: dict[str, Pattern] = {}


def _register(id: str, name: str, description: str, params: list[Param], colored: bool = True):
    ps = list(params) + ([_COLOR] if colored else [])

    def deco(fn: Callable[[Config, dict], list[Stroke]]):
        def build(cfg: Config, p: dict) -> Drawing:
            col = min(int(p.get("color", 0)), len(cfg.colors) - 1)
            strokes = fn(cfg, {**p, "color": col})
            return _finish(cfg, strokes, name)
        REGISTRY[id] = Pattern(id, name, description, ps, build)
        return fn
    return deco


def pattern_to_dict(p: Pattern) -> dict:
    return {"id": p.id, "name": p.name, "description": p.description,
            "params": [q.to_dict() for q in p.params]}


def build_pattern(cfg: Config, pattern_id: str, params: dict | None = None) -> Drawing:
    """Build a registered pattern with defaults filled in and ranges validated."""
    pat = REGISTRY.get(pattern_id)
    if pat is None:
        raise ValueError(f"Unknown test pattern '{pattern_id}'")
    return pat.build(cfg, pat.resolve(params))


def _finish(cfg: Config, strokes: list[Stroke], name: str) -> Drawing:
    d = Drawing(strokes, name)
    return clean(clip_to_rect(d, cfg.safe_rect()))


# ------------------------------------------------------------------------------ geometry helpers
def _P(id, label, kind, default, mn=None, mx=None, unit=None, choices=None) -> Param:
    return Param(id, label, kind, default, mn, mx, choices, unit)


_COLOR = _P("color", "Colour (slot)", "int", 0, 0, 2)


def _line(x0, y0, x1, y1, c=0) -> Stroke:
    return Stroke([[x0, y0], [x1, y1]], c)


def _rect(x0, y0, x1, y1, c=0) -> Stroke:
    return Stroke([[x0, y0], [x1, y0], [x1, y1], [x0, y1], [x0, y0]], c)


def _circle(cx, cy, r, c=0, tol=0.02) -> Stroke:
    n = max(48, int(math.ceil(math.pi / math.acos(max(1 - tol / max(r, 1e-6), 0.0)))))
    t = np.linspace(0, 2 * math.pi, n + 1)
    return Stroke(np.column_stack([cx + r * np.cos(t), cy + r * np.sin(t)]), c)


def _cross(x, y, arm, c=0) -> list[Stroke]:
    return [_line(x - arm, y, x + arm, y, c), _line(x, y - arm, x, y + arm, c)]


def _ctx(cfg: Config):
    x0, y0, x1, y1 = cfg.safe_rect()
    return x0, y0, x1, y1, (x0 + x1) / 2, (y0 + y1) / 2


def _fmt(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def _multiples(step: float, lo: float, hi: float) -> list[float]:
    k0, k1 = math.ceil(lo / step - 1e-9), math.floor(hi / step + 1e-9)
    return [k * step for k in range(int(k0), int(k1) + 1)]


# ------------------------------------------------------------------------------ patterns
@_register("calibration-grid", "Calibration grid",
           "Line grid at a fixed spacing with labels in mm. For measuring scale and squareness.",
           [_P("spacing_mm", "Line spacing", "float", 100.0, 10, 500, "mm"),
            _P("labels", "Labels", "bool", True),
            _P("label_size_mm", "Label height", "float", 8.0, 3, 20, "mm"),
            _P("frame", "Draw frame", "bool", True)])
def _grid(cfg, p):
    x0, y0, x1, y1, _, _ = _ctx(cfg)
    c, sp = p["color"], p["spacing_mm"]
    s: list[Stroke] = [_rect(x0, y0, x1, y1, c)] if p["frame"] else []
    xs = [x for x in _multiples(sp, x0, x1) if x0 + 1e-6 < x < x1 - 1e-6]
    ys = [y for y in _multiples(sp, y0, y1) if y0 + 1e-6 < y < y1 - 1e-6]
    s += [_line(x, y0, x, y1, c) for x in xs] + [_line(x0, y, x1, y, c) for y in ys]
    if p["labels"]:
        h = min(p["label_size_mm"], sp / 4)
        s += [t for x in xs for t in text_strokes(_fmt(x), h, x + 1.5, y0 + 2, color=c)]
        s += [t for y in ys for t in text_strokes(_fmt(y), h, x0 + 2, y + 1.5, color=c)]
    return s


@_register("crosshair-points", "Measurement crosses",
           "Small crosses on a known point grid (corners and centre included for odd counts). "
           "Measure them again with a ruler.",
           [_P("cols", "Columns", "int", 5, 2, 15), _P("rows", "Rows", "int", 3, 2, 11),
            _P("arm_mm", "Arm length", "float", 5.0, 2, 20, "mm"),
            _P("labels", "Label coordinates", "bool", False),
            _P("label_size_mm", "Label height", "float", 5.0, 3, 12, "mm")])
def _crosses(cfg, p):
    x0, y0, x1, y1, cx, cy = _ctx(cfg)
    a, c = p["arm_mm"], p["color"]
    xs = np.linspace(x0 + a, x1 - a, p["cols"])
    ys = np.linspace(y0 + a, y1 - a, p["rows"])
    s: list[Stroke] = []
    for x in xs:
        for y in ys:
            s += _cross(float(x), float(y), a, c)
            if p["labels"]:
                txt, h = f"{_fmt(x)},{_fmt(y)}", p["label_size_mm"]
                w, _ = measure(txt, h)
                tx = x + a + 1 if x < cx else x - a - 1 - w
                ty = y + a + 1 if y < cy else y - a - 1 - h
                s += text_strokes(txt, h, tx, ty, color=c)
    return s


@_register("square-frame", "Frame", "Outline of the permitted drawing area (optionally inset).",
           [_P("inset_mm", "Inset", "float", 0.0, 0, 400, "mm")])
def _frame(cfg, p):
    x0, y0, x1, y1, _, _ = _ctx(cfg)
    i = p["inset_mm"]
    return [_rect(x0 + i, y0 + i, x1 - i, y1 - i, p["color"])] if x1 - x0 > 2 * i and y1 - y0 > 2 * i else []


@_register("diagonals", "Diagonals", "Both diagonals of the drawing area. Checks the geometry across the whole surface.",
           [_P("inset_mm", "Inset", "float", 0.0, 0, 400, "mm"), _P("frame", "Draw frame", "bool", False)])
def _diag(cfg, p):
    x0, y0, x1, y1, _, _ = _ctx(cfg)
    i, c = p["inset_mm"], p["color"]
    a, b, e, f = x0 + i, y0 + i, x1 - i, y1 - i
    s = [_line(a, b, e, f, c), _line(a, f, e, b, c)]
    return s + ([_rect(a, b, e, f, c)] if p["frame"] else [])


@_register("circles", "Concentric circles", "Circles around the centre of the drawing area.",
           [_P("count", "Count", "int", 5, 1, 20), _P("outer_radius_mm", "Outer radius", "float", 360.0, 10, 490, "mm")])
def _circles(cfg, p):
    _, _, _, _, cx, cy = _ctx(cfg)
    return [_circle(cx, cy, p["outer_radius_mm"] * (i + 1) / p["count"], p["color"]) for i in range(p["count"])]


@_register("circle-accuracy", "Circle accuracy",
           "Circle with a cross of axes plus an inscribed and a circumscribed square. Measure the diameter and diagonals: "
           "deviations reveal scale and geometry errors.",
           [_P("diameter_mm", "Diameter", "float", 300.0, 20, 960, "mm"),
            _P("squares", "Squares", "choice", "both",
               choices=[("both", "Both"), ("inscribed", "Inscribed"),
                        ("circumscribed", "Circumscribed"), ("none", "None")]),
            _P("axes", "Axes cross", "bool", True)])
def _circle_acc(cfg, p):
    _, _, _, _, cx, cy = _ctx(cfg)
    r, c = p["diameter_mm"] / 2, p["color"]
    s = [_circle(cx, cy, r, c)]
    if p["squares"] in ("both", "inscribed"):
        h = r / math.sqrt(2)
        s.append(_rect(cx - h, cy - h, cx + h, cy + h, c))
    if p["squares"] in ("both", "circumscribed"):
        s.append(_rect(cx - r, cy - r, cx + r, cy + r, c))
    if p["axes"]:
        s += [_line(cx - r, cy, cx + r, cy, c), _line(cx, cy - r, cx, cy + r, c)]
    return s


@_register("spiral", "Spiral", "Archimedean spiral. Checks smooth curves and even feed.",
           [_P("turns", "Turns", "float", 6.0, 1, 30), _P("outer_radius_mm", "Outer radius", "float", 360.0, 10, 490, "mm"),
            _P("inner_radius_mm", "Inner radius", "float", 5.0, 0, 200, "mm")])
def _spiral(cfg, p):
    _, _, _, _, cx, cy = _ctx(cfg)
    r0, r1 = min(p["inner_radius_mm"], p["outer_radius_mm"]), p["outer_radius_mm"]
    dth = 2 * math.acos(max(1 - 0.02 / max(r1, 1.0), 0.0))
    n = int(min(max(p["turns"] * 2 * math.pi / max(dth, 1e-3), 64), 20000))
    th = np.linspace(0, p["turns"] * 2 * math.pi, n)
    r = r0 + (r1 - r0) * th / th[-1]
    return [Stroke(np.column_stack([cx + r * np.cos(th), cy + r * np.sin(th)]), p["color"])]


@_register("star", "Star", "Star polygon (pentagram etc.) with a circumscribed circle. Checks angles and long diagonals.",
           [_P("points", "Points", "int", 5, 3, 16), _P("skip", "Skip", "int", 2, 1, 7),
            _P("radius_mm", "Radius", "float", 350.0, 20, 490, "mm"), _P("circle", "Circumscribed circle", "bool", True)])
def _star(cfg, p):
    _, _, _, _, cx, cy = _ctx(cfg)
    n, k, r, c = p["points"], min(p["skip"], max((p["points"] - 1) // 2, 1)), p["radius_mm"], p["color"]
    ang = lambda i: math.pi / 2 + 2 * math.pi * i / n
    s: list[Stroke] = []
    seen: set[int] = set()
    for start in range(n):
        if start in seen:
            continue
        idx, i = [], start
        while i not in seen:
            seen.add(i)
            idx.append(i)
            i = (i + k) % n
        idx.append(start)
        s.append(Stroke([[cx + r * math.cos(ang(j)), cy + r * math.sin(ang(j))] for j in idx], c))
    return s + ([_circle(cx, cy, r, c)] if p["circle"] else [])


@_register("corner-marks", "Corner marks", "L-shaped marks in the four corners of the drawing area.",
           [_P("length_mm", "Leg length", "float", 50.0, 10, 200, "mm")])
def _corners(cfg, p):
    x0, y0, x1, y1, _, _ = _ctx(cfg)
    return _l_marks(x0, y0, x1, y1, p["length_mm"], p["color"])


def _l_marks(x0, y0, x1, y1, L, c) -> list[Stroke]:
    s = []
    for cx, dx in ((x0, 1), (x1, -1)):
        for cy, dy in ((y0, 1), (y1, -1)):
            s.append(Stroke([[cx + dx * L, cy], [cx, cy], [cx, cy + dy * L]], c))
    return s


@_register("ruler", "Rulers", "Rulers with a mm scale along the bottom and left edges, labelled every 100 mm.",
           [_P("axis", "Axes", "choice", "both", choices=[("both", "X and Y"), ("x", "X only"), ("y", "Y only")]),
            _P("tick_mm", "Tick spacing", "float", 10.0, 1, 50, "mm"),
            _P("labels", "Labels", "bool", True), _P("label_size_mm", "Label height", "float", 6.0, 3, 15, "mm")])
def _ruler(cfg, p):
    x0, y0, x1, y1, _, _ = _ctx(cfg)
    c, t = p["color"], p["tick_mm"]
    s: list[Stroke] = []

    def comb(lo, hi, base, horizontal):
        pts = [[lo, base]] if horizontal else [[base, lo]]
        for v in _multiples(t, lo, hi):
            k = 12.0 if abs(v / 100 - round(v / 100)) < 1e-9 else (8.0 if abs(v / 50 - round(v / 50)) < 1e-9 else 4.0)
            a, b = ([v, base], [v, base + k]) if horizontal else ([base, v], [base + k, v])
            pts += [a, b, a]
            if p["labels"] and k == 12.0:
                txt, h = _fmt(v), p["label_size_mm"]
                if horizontal:
                    w, _ = measure(txt, h)
                    s.extend(text_strokes(txt, h, min(v - 1, hi - w), base + 14, color=c))
                else:           # keep the label inside the (smaller) safe rect near its top/bottom edge
                    s.extend(text_strokes(txt, h, base + 14, min(max(v - h / 2, lo), hi - h), color=c))
        pts.append([hi, base] if horizontal else [base, hi])
        s.append(Stroke(pts, c))

    if p["axis"] in ("both", "x"):
        comb(x0, x1, y0, True)
    if p["axis"] in ("both", "y"):
        comb(y0, y1, x0, False)
    return s


@_register("text", "Text", "Any text in a single-stroke font (Hershey). Umlauts are supported.",
           [_P("text", "Text", "str", "W-BOT", None, 200),
            _P("size_mm", "Text height (capital letters)", "float", 40.0, 3, 300, "mm"),
            _P("align", "Alignment", "choice", "center",
               choices=[("left", "Left"), ("center", "Centre"), ("right", "Right")]),
            _P("fit", "Shrink to fit width", "bool", True)])
def _text(cfg, p):
    x0, y0, x1, y1, cx, cy = _ctx(cfg)
    size = p["size_mm"]
    w, h = measure(p["text"], size)
    if p["fit"] and w > 0 and (w > x1 - x0 or h > y1 - y0):
        size *= min((x1 - x0) / w, (y1 - y0) / h)
    x = {"left": x0, "center": cx, "right": x1}[p["align"]]
    return text_strokes(p["text"], size, x, cy, anchor=p["align"], valign="center", color=p["color"])


def _color_row(cfg: Config, cy: float, size: float, label: float) -> list[Stroke]:
    x0, _, x1, _, cx, _ = _ctx(cfg)
    n = len(cfg.colors)
    pitch = min(400.0, (x1 - x0) / n)
    s: list[Stroke] = []
    for i in range(n):
        x = cx + (i - (n - 1) / 2) * pitch
        h = size / 2
        zig, step, flip = [], max(size / 12, 1.0), False
        for yy in np.arange(cy - h, cy + h + 1e-9, step):
            zig += [[x - h, yy], [x + h, yy]][::-1] if flip else [[x - h, yy], [x + h, yy]]
            flip = not flip
        s += [Stroke(zig, i), _rect(x - h, cy - h, x + h, cy + h, i), _circle(x, cy + size * 1.2, h * 0.8, i)]
        s += [_line(x - h, cy - size * 1.4 - k * size / 6, x + h, cy - size * 1.4 - k * size / 6, i) for k in range(3)]
        s += text_strokes(cfg.colors[i].name, label, x, cy - size * 2.4 - label, anchor="center", color=i)
    return s


@_register("color-test", "Colour test",
           "For each colour a hatched square, a circle, three lines and the colour name. Tests the revolver "
           "and the colour change.",
           [_P("size_mm", "Shape size", "float", 40.0, 10, 100, "mm")], colored=False)
def _colors(cfg, p):
    _, _, _, _, _, cy = _ctx(cfg)
    return _color_row(cfg, cy, p["size_mm"], min(10.0, p["size_mm"] / 3))


@_register("pen-test", "Pen test",
           "Dots, strokes of different lengths, a dash-gap line and scribbles. Tests pen pressure, lifting and lowering.",
           [_P("dots", "Number of dots", "int", 10, 3, 40), _P("dot_spacing_mm", "Dot spacing", "float", 10.0, 3, 30, "mm")])
def _pen(cfg, p):
    _, _, _, _, cx, cy = _ctx(cfg)
    c, n, sp = p["color"], p["dots"], p["dot_spacing_mm"]
    s: list[Stroke] = []
    x_start = cx - (n - 1) * sp / 2
    s += [_line(x_start + i * sp, cy + 60, x_start + i * sp + 0.3, cy + 60, c) for i in range(n)]
    x = cx - 150
    for L in (1, 2, 3, 5, 10, 20):
        for _ in range(3):
            s.append(_line(x, cy + 35, x + L, cy + 35, c))
            x += L + 4
    s += [_line(cx - 100 + 10 * i, cy + 10, cx - 95 + 10 * i, cy + 10, c) for i in range(20)]
    s += [_line(cx - 100 + 4 * i, cy - 15, cx - 100 + 4 * i, cy - 5, c) for i in range(51)]
    s.append(_line(cx - 100, cy - 30, cx + 100, cy - 30, c))
    return s


@_register("speed-test", "Speed test",
           "Horizontal lines of different lengths, zigzag and square wave (sharp corners). "
           "Shows acceleration, corner rounding and slip.",
           [_P("max_length_mm", "Longest line", "float", 800.0, 100, 1400, "mm"), _P("steps", "Number of lines", "int", 5, 2, 8)])
def _speed(cfg, p):
    x0, y0, x1, y1, cx, cy = _ctx(cfg)
    c, s = p["color"], []
    L, ytop = p["max_length_mm"], y1 - 30
    for i in range(p["steps"]):
        ln, y = L / 2 ** i, ytop - 25 * i
        s.append(_line(cx - ln / 2, y, cx + ln / 2, y, c))
        s += text_strokes(f"{ln:.0f}", 6, cx + ln / 2 + 3, y - 3, color=c) if cx + ln / 2 + 40 < x1 else []
    zy = ytop - 25 * p["steps"] - 40
    zw = min(L, 600.0)
    k = np.arange(0, int(zw // 20) + 1)
    s.append(Stroke(np.column_stack([cx - zw / 2 + k * 20.0, zy + 15.0 * (k % 2)]), c))
    wave = [[cx - zw / 2, zy - 40]]
    for i in range(int(zw // 40)):
        a = cx - zw / 2 + i * 40
        wave += [[a, zy - 20], [a + 20, zy - 20], [a + 20, zy - 40], [a + 40, zy - 40]]
    s.append(Stroke(wave, c))
    return s


@_register("full-board-test", "Full-board test",
           "Everything compactly in one image: frame, grid, circle, measurement crosses, corner marks, text and colour test.",
           [_P("with_colors", "Include colour test", "bool", True)], colored=False)
def _full(cfg, p):
    x0, y0, x1, y1, cx, cy = _ctx(cfg)
    c = 0
    s: list[Stroke] = [_rect(x0, y0, x1, y1, c)]
    s += [_line(x, y0, x, y1, c) for x in _multiples(250.0, x0 + 1, x1 - 1)]
    s += [_line(x0, y, x1, y, c) for y in _multiples(250.0, y0 + 1, y1 - 1)]
    s += _l_marks(x0, y0, x1, y1, 30.0, c)
    s += [_line(x0, y0, x1, y1, c), _line(x0, y1, x1, y0, c)]
    s += _circle_acc(cfg, {"diameter_mm": 250.0, "squares": "both", "axes": True, "color": c})
    s += _crosses(cfg, {"cols": 5, "rows": 3, "arm_mm": 5.0, "labels": False, "label_size_mm": 5.0, "color": c})
    s += text_strokes("W-BOT", 20, cx, y1 - 45, anchor="center", color=c)
    if p["with_colors"]:
        s += _color_row(cfg, y0 + 75, 20.0, 6.0)
    return s


def tape_points(cfg: Config, cols: int = 3, rows: int = 3, inset_mm: float = 20.0) -> list[tuple[str, float, float]]:
    """Named points (A, B, C ... row by row from top-left) for the tape-measure calibration."""
    x0, y0, x1, y1, _, _ = _ctx(cfg)
    xs, ys = np.linspace(x0 + inset_mm, x1 - inset_mm, cols), np.linspace(y1 - inset_mm, y0 + inset_mm, rows)
    return [(chr(65 + r * cols + k), float(x), float(y)) for r, y in enumerate(ys) for k, x in enumerate(xs)]


@_register("tape-points", "Tape-measure points",
           "Labelled crosses (A, B, C ...) for calibration with a tape measure: only measure distances.",
           [_P("cols", "Columns", "int", 3, 2, 5), _P("rows", "Rows", "int", 3, 2, 4),
            _P("arm_mm", "Arm length", "float", 12.0, 4, 20, "mm"), _P("label_size_mm", "Label height", "float", 14.0, 6, 30, "mm")])
def _tape(cfg, p):
    c, a, hs = p["color"], p["arm_mm"], p["label_size_mm"]
    x0, y0, x1, y1, cx, cy = _ctx(cfg)
    s: list[Stroke] = []
    for name, x, y in tape_points(cfg, p["cols"], p["rows"], max(a, 20.0)):
        s += _cross(x, y, a, c)
        w, _ = measure(name, hs)
        s += text_strokes(name, hs, x + a + 3 if x < cx + 1 else x - a - 3 - w, y + 3 if y < cy else y - 3 - hs, color=c)
    return s


__all__ = ["Param", "Pattern", "REGISTRY", "build_pattern", "pattern_to_dict", "tape_points"]
