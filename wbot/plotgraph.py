"""Function graphs with axes for the canvas editor: ``y = f(x)``, parametric and polar curves, data points.

Expressions are parsed with a whitelist (``ast``), never evaluated with ``eval`` on raw input:
``sin(x)``, ``x^2 - 3x + 1``, ``exp(-x^2)``, ``sqrt(abs(x))``, ``2pi``. The result is a list of strokes in
millimetres, centred on the middle of the plot frame.
"""
from __future__ import annotations

import ast
import math
import re

import numpy as np

from .drawing import Stroke, clip_polyline
from .textfont import text_strokes

FUNCS = {"sin": np.sin, "cos": np.cos, "tan": np.tan, "asin": np.arcsin, "acos": np.arccos, "atan": np.arctan,
         "arcsin": np.arcsin, "arccos": np.arccos, "arctan": np.arctan, "sinh": np.sinh, "cosh": np.cosh,
         "tanh": np.tanh, "exp": np.exp, "ln": np.log, "log": np.log10, "log10": np.log10, "log2": np.log2,
         "sqrt": np.sqrt, "abs": np.abs, "sign": np.sign, "floor": np.floor, "ceil": np.ceil, "round": np.round,
         "sec": lambda v: 1 / np.cos(v), "csc": lambda v: 1 / np.sin(v), "cot": lambda v: 1 / np.tan(v)}
FUNCS2 = {"min": np.minimum, "max": np.maximum, "atan2": np.arctan2, "mod": np.mod, "pow": np.power}
CONSTS = {"pi": math.pi, "e": math.e, "tau": 2 * math.pi}
MAX_EXPR = 200
N_SAMPLES = 900


class ExprError(ValueError):
    """The expression cannot be read (the message is shown to the user)."""


def _prep(src: str, variable: str) -> str:
    s = src.strip().replace("^", "**").replace("π", "pi").replace("·", "*").replace("×", "*").replace("−", "-")
    s = re.sub(r"^\s*(y|r|x)\s*=\s*", "", s)
    s = re.sub(r"(?<![A-Za-z_\d.])(\d+(?:\.\d+)?)\s*(?=[A-Za-z(])", r"\1*", s)     # 2x -> 2*x, 3(x+1)
    s = re.sub(r"\)\s*(?=[A-Za-z0-9(])", ")*", s)                                  # (x+1)(x-1)
    s = re.sub(rf"\b({variable}|pi|e)\s*(?=\()", r"\1*", s)                        # x(x+1)
    return s


class _Floaty(ast.NodeTransformer):
    """Numbers become floats, so a huge integer power such as 9**9**9 cannot hang the server."""

    def visit_Constant(self, n):
        return ast.copy_location(ast.Constant(float(n.value)), n)


def compile_expr(src: str, variable: str = "x"):
    """Returns ``f(values) -> ndarray``. Raises :class:`ExprError` with a readable message."""
    if not src or not src.strip():
        raise ExprError("Type a function, for example sin(x).")
    if len(src) > MAX_EXPR:
        raise ExprError("The expression is too long.")
    try:
        tree = ast.parse(_prep(src, variable), mode="eval")
    except SyntaxError:
        raise ExprError(f"“{src}” cannot be read. Use for example 2x^2 - 3 or sin(x)/x.") from None

    def check(n):
        if isinstance(n, ast.Expression):
            return check(n.body)
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow, ast.Mod)):
            return check(n.left), check(n.right)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
            return check(n.operand)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)) and not isinstance(n.value, bool):
            return
        if isinstance(n, ast.Name):
            if n.id != variable and n.id not in CONSTS:
                raise ExprError(f"Unknown name “{n.id}”. The variable is {variable}.")
            return
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and not n.keywords:
            ok = (n.func.id in FUNCS and len(n.args) == 1) or (n.func.id in FUNCS2 and len(n.args) == 2)
            if not ok:
                raise ExprError(f"Unknown function “{n.func.id}”.")
            return [check(a) for a in n.args]
        raise ExprError(f"“{src}” uses something that is not allowed.")

    check(tree)
    tree = ast.fix_missing_locations(_Floaty().visit(tree))
    code = compile(tree, "<expr>", "eval")

    def f(v: np.ndarray) -> np.ndarray:
        env = {"__builtins__": {}, **CONSTS, **FUNCS, **FUNCS2, variable: v}
        try:
            with np.errstate(all="ignore"):
                r = eval(code, env)           # noqa: S307  (tree was whitelisted above)
        except (OverflowError, ValueError, ZeroDivisionError, TypeError):
            raise ExprError(f"“{src}” cannot be calculated.") from None
        r = np.asarray(r, float)
        return np.broadcast_to(r, np.shape(v)).copy() if r.ndim == 0 else r

    return f


def _nice_step(span: float, target: int = 6) -> float:
    raw = span / max(target, 1)
    p = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 2.5, 5, 10):
        if raw <= m * p:
            return m * p
    return 10 * p


def _fmt(v: float) -> str:
    return "0" if abs(v) < 1e-9 else f"{v:.6g}"


def _rdp(p: np.ndarray, tol: float) -> np.ndarray:
    if len(p) < 3:
        return p
    keep = np.zeros(len(p), bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(p) - 1)]
    while stack:
        a, b = stack.pop()
        if b <= a + 1:
            continue
        d = p[b] - p[a]
        n = math.hypot(*d)
        seg = p[a + 1:b] - p[a]
        dist = np.hypot(*seg.T) if n < 1e-12 else np.abs(d[0] * seg[:, 1] - d[1] * seg[:, 0]) / n
        i = int(np.argmax(dist))
        if dist[i] > tol:
            keep[a + 1 + i] = True
            stack += [(a, a + 1 + i), (a + 1 + i, b)]
    return p[keep]


def _curve_pieces(x: np.ndarray, y: np.ndarray, rect, jump: float) -> list[np.ndarray]:
    """Cut a sampled curve at NaN / infinite values and big jumps (asymptotes), clip to the frame."""
    ok = np.isfinite(x) & np.isfinite(y)
    dy = np.abs(np.diff(y))
    brk = ~ok[:-1] | ~ok[1:] | (dy > jump)
    pts = np.column_stack([x, y])
    out, start = [], 0
    for i in np.flatnonzero(brk).tolist() + [len(x) - 1]:
        seg = pts[start:i + 1]
        seg = seg[np.isfinite(seg).all(axis=1)]
        start = i + 1
        if len(seg) >= 2:
            out += clip_polyline(seg, rect)
    return out


def render_plot(spec: dict, palette_size: int = 8) -> list[Stroke]:
    """Strokes (mm) for a plot spec, centred on the frame. Raises :class:`ExprError`.

    spec: ``w``, ``h`` (mm), ``xmin xmax ymin ymax`` (``ymin``/``ymax`` None = automatic), ``grid``, ``ticks``,
    ``axes`` ("cross" | "box" | "none"), ``xlabel``, ``ylabel``, ``color``, ``curves``: list of
    ``{"kind": "y"|"param"|"polar"|"points", "expr", "expr2", "t0", "t1", "points", "color"}``.
    """
    w, h = float(spec.get("w", 160)), float(spec.get("h", 110))
    if not (10 <= w <= 3000 and 10 <= h <= 3000):
        raise ExprError("The plot size is out of range.")
    x0, x1 = float(spec.get("xmin", -5)), float(spec.get("xmax", 5))
    if not x1 > x0:
        raise ExprError("x max must be larger than x min.")
    color = int(spec.get("color", 0))
    label_h = float(np.clip(0.035 * max(w, h), 3.0, 8.0))
    curves = []
    for c in spec.get("curves") or []:
        kind = c.get("kind", "y")
        if kind == "y":
            f = compile_expr(c.get("expr", ""), "x")
            xs = np.linspace(x0, x1, N_SAMPLES)
            curves.append((xs, f(xs), c))
        elif kind in ("param", "polar"):
            t0, t1 = float(c.get("t0", 0)), float(c.get("t1", 2 * math.pi))
            ts = np.linspace(t0, t1, N_SAMPLES * 2)
            if kind == "param":
                fx, fy = compile_expr(c.get("expr", ""), "t"), compile_expr(c.get("expr2", ""), "t")
                curves.append((fx(ts), fy(ts), c))
            else:
                r = compile_expr(c.get("expr", ""), "t")(ts)
                curves.append((r * np.cos(ts), r * np.sin(ts), c))
        elif kind == "points":
            pts = np.asarray(c.get("points") or [], float).reshape(-1, 2)
            if len(pts):
                curves.append((pts[:, 0], pts[:, 1], c))
    ymin, ymax = spec.get("ymin"), spec.get("ymax")
    if ymin is None or ymax is None:
        vals = np.concatenate([cy[np.isfinite(cy) & (np.isfinite(cx) & (cx >= x0) & (cx <= x1))] for cx, cy, _ in curves]
                              or [np.array([-1.0, 1.0])])
        if len(vals) == 0:
            vals = np.array([-1.0, 1.0])
        lo, hi = (np.percentile(vals, 1), np.percentile(vals, 99)) if len(vals) > 20 else (vals.min(), vals.max())
        pad = max((hi - lo) * 0.08, 1e-6)
        ymin = lo - pad if ymin is None else float(ymin)
        ymax = hi + pad if ymax is None else float(ymax)
        if ymax - ymin < 1e-9:
            ymin, ymax = ymin - 1, ymax + 1
    ymin, ymax = float(ymin), float(ymax)
    if not ymax > ymin:
        raise ExprError("y max must be larger than y min.")
    sx, sy = w / (x1 - x0), h / (ymax - ymin)
    out: list[Stroke] = []
    frame = (0.0, 0.0, w, h)
    to_mm = lambda p: np.column_stack([(p[:, 0] - x0) * sx, (p[:, 1] - ymin) * sy])

    # grid + ticks + labels
    stepx, stepy = _nice_step(x1 - x0, 6 if w > 120 else 4), _nice_step(ymax - ymin, 5 if h > 90 else 3)
    xt = np.arange(math.ceil(x0 / stepx - 1e-9) * stepx, x1 + 1e-9, stepx)
    yt = np.arange(math.ceil(ymin / stepy - 1e-9) * stepy, ymax + 1e-9, stepy)
    axes = spec.get("axes", "cross")
    cross_x = float(np.clip(0.0, x0, x1))              # where the y axis runs
    cross_y = float(np.clip(0.0, ymin, ymax))
    ax_x = (cross_x - x0) * sx if axes == "cross" else 0.0
    ax_y = (cross_y - ymin) * sy if axes == "cross" else 0.0
    tick = 1.4
    if spec.get("grid", True):
        dash, gap = 1.6, 2.4
        for v in xt:
            gx = (v - x0) * sx
            y = 0.0
            while y < h:
                out.append(Stroke([[gx, y], [gx, min(y + dash, h)]], color)); y += dash + gap
        for v in yt:
            gy = (v - ymin) * sy
            xx = 0.0
            while xx < w:
                out.append(Stroke([[xx, gy], [min(xx + dash, w), gy]], color)); xx += dash + gap
    if axes == "box":
        out.append(Stroke([[0, 0], [w, 0], [w, h], [0, h], [0, 0]], color))
    elif axes == "cross":
        arrow = min(2.6, 0.03 * max(w, h) + 1)
        out.append(Stroke([[0, ax_y], [w, ax_y]], color))
        out.append(Stroke([[w - arrow * 1.6, ax_y + arrow * 0.7], [w, ax_y], [w - arrow * 1.6, ax_y - arrow * 0.7]], color))
        out.append(Stroke([[ax_x, 0], [ax_x, h]], color))
        out.append(Stroke([[ax_x - arrow * 0.7, h - arrow * 1.6], [ax_x, h], [ax_x + arrow * 0.7, h - arrow * 1.6]], color))
    if spec.get("ticks", True) and axes != "none":
        for v in xt:
            gx = (v - x0) * sx
            if axes == "cross" and abs(gx - ax_x) < 1e-6 and ax_x > 0:
                continue
            if axes == "cross" and gx > w - 3:
                continue
            out.append(Stroke([[gx, ax_y - tick], [gx, ax_y + tick]], color))
            if spec.get("labels", True):
                out += text_strokes(_fmt(v), label_h * 0.8, gx, ax_y - tick - label_h * 1.1, anchor="center", color=color)
        for v in yt:
            gy = (v - ymin) * sy
            if axes == "cross" and abs(gy - ax_y) < 1e-6 and ax_y > 0:
                continue
            if axes == "cross" and gy > h - 3:
                continue
            out.append(Stroke([[ax_x - tick, gy], [ax_x + tick, gy]], color))
            if spec.get("labels", True):
                out += text_strokes(_fmt(v), label_h * 0.8, ax_x - tick - 1.2, gy - label_h * 0.4, anchor="right", color=color)
    for key, px, py, anc in (("xlabel", w, ax_y - label_h * 2.4, "right"), ("ylabel", ax_x + 3, h - label_h * 0.5, "left")):
        txt = str(spec.get(key) or "")
        if txt:
            out += text_strokes(txt, label_h, px if key == "xlabel" else px, py, anchor=anc, color=color)
    title = str(spec.get("title") or "")
    if title:
        out += text_strokes(title, label_h * 1.2, w / 2, h + label_h * 1.2, anchor="center", color=color)
    # curves
    for cx, cy, c in curves:
        col = int(c.get("color", color))
        if c.get("kind") == "points":
            p = to_mm(np.column_stack([cx, cy]))
            if c.get("line", True) and len(p) > 1:
                for piece in clip_polyline(p, frame):
                    out.append(Stroke(piece, col))
            for q in p:
                if 0 <= q[0] <= w and 0 <= q[1] <= h:
                    r = 0.9
                    out.append(Stroke([[q[0] - r, q[1]], [q[0] + r, q[1]]], col))
                    out.append(Stroke([[q[0], q[1] - r], [q[0], q[1] + r]], col))
            continue
        p = to_mm(np.column_stack([cx, cy]))
        for piece in _curve_pieces(p[:, 0], p[:, 1], frame, jump=0.6 * h):
            out.append(Stroke(_rdp(piece, 0.04), col))
    # centre on the frame, leave room for labels only through the caller's bounding box
    return [Stroke(s.pts - [w / 2, h / 2], s.color) for s in out]


__all__ = ["compile_expr", "render_plot", "ExprError"]
