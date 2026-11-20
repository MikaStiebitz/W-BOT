"""Minimal, dependency-free SVG -> :class:`Drawing` import.

Supported: ``path`` (M L H V C S Q T A Z, absolute and relative), ``polyline``,
``polygon``, ``line``, ``rect`` (incl. rounded), ``circle``, ``ellipse``, nested
``g``/``svg``, transforms (translate scale rotate matrix skewX skewY), ``viewBox`` /
``width`` / ``height`` units -> mm. Only outlines are drawn (fills are not hatched).
Stroke (else fill) colour is mapped to the nearest configured pen colour.
Curves are flattened to ``tolerance_mm``. The result lies in ``[0,W] x [0,H]`` mm with
Y pointing up (SVG's Y axis is flipped); place it with ``Drawing.fit_to``.
Ignored (reported as warnings): text, images, ``use``, CSS ``<style>``, gradients.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import numpy as np

from .config import Config
from .drawing import Drawing, Stroke, clean


class SvgError(ValueError):
    """The SVG cannot be imported (message is shown to the user)."""


@dataclass
class SvgOptions:
    tolerance_mm: float = 0.1
    color_map: dict[str, int] | None = None    # "#rrggbb" or colour name -> slot (overrides nearest)
    skip_white: bool = True                    # white lines are invisible on a whiteboard
    max_bytes: int = 20_000_000
    max_points: int = 1_000_000
    default_color: int = 0


@dataclass
class SvgImport:
    drawing: Drawing
    width_mm: float
    height_mm: float
    warnings: list[str] = field(default_factory=list)


_UNITS = {"": 25.4 / 96, "px": 25.4 / 96, "mm": 1.0, "cm": 10.0, "in": 25.4, "pt": 25.4 / 72, "pc": 25.4 / 6}
_NAMED = {"black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#008000", "lime": "#00ff00",
          "blue": "#0000ff", "yellow": "#ffff00", "orange": "#ffa500", "purple": "#800080", "gray": "#808080",
          "grey": "#808080", "cyan": "#00ffff", "aqua": "#00ffff", "magenta": "#ff00ff", "fuchsia": "#ff00ff",
          "navy": "#000080", "maroon": "#800000", "brown": "#a52a2a", "pink": "#ffc0cb", "teal": "#008080",
          "olive": "#808000", "silver": "#c0c0c0", "darkblue": "#00008b", "darkred": "#8b0000",
          "darkgreen": "#006400", "gold": "#ffd700", "indigo": "#4b0082", "violet": "#ee82ee"}
_NUM = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_SKIP = {"defs", "clipPath", "mask", "symbol", "metadata", "title", "desc", "style", "marker", "pattern",
         "linearGradient", "radialGradient", "filter"}
_WARN_TAGS = {"text": "text elements", "image": "images", "use": "'use' references", "style": "CSS styles"}
_CMD_ARGS = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


# ------------------------------------------------------------------------------ small parsers
def _tag(e: ET.Element) -> str:
    return e.tag.rsplit("}", 1)[-1]


def _length(v: str | None, default: float | None = None) -> float | None:
    """Length string -> mm (None for percentages / missing)."""
    if v is None:
        return default
    m = re.fullmatch(r"\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-z%]*)\s*", v)
    if not m or m.group(2) == "%" or m.group(2) not in _UNITS:
        return default
    return float(m.group(1)) * _UNITS[m.group(2)]


def _num(e: ET.Element, name: str, default: float = 0.0) -> float:
    m = _NUM.match((e.get(name) or "").strip())
    return float(m.group(0)) if m else default


def _parse_color(v: str | None) -> tuple[int, int, int] | None:
    if v is None:
        return None
    v = v.strip().lower()
    if v in ("", "none", "transparent"):
        return None
    v = _NAMED.get(v, v)
    if v.startswith("#"):
        h = v[1:]
        if len(h) in (3, 4):
            h = "".join(c * 2 for c in h[:3])
        if len(h) >= 6:
            try:
                return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
            except ValueError:
                return (0, 0, 0)
    m = re.fullmatch(r"rgba?\(([^)]*)\)", v)
    if m:
        parts = [x.strip() for x in m.group(1).replace("/", ",").split(",")][:3]
        try:
            return tuple(int(round(float(x[:-1]) * 2.55)) if x.endswith("%") else int(float(x)) for x in parts)  # type: ignore
        except ValueError:
            return (0, 0, 0)
    return (0, 0, 0)          # currentColor, url(#gradient), unknown -> black


def _matrix(s: str) -> np.ndarray:
    m = np.eye(3)
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", s or ""):
        a = [float(x) for x in _NUM.findall(args)]
        t = np.eye(3)
        if name == "translate" and a:
            t[0, 2], t[1, 2] = a[0], (a[1] if len(a) > 1 else 0.0)
        elif name == "scale" and a:
            t[0, 0], t[1, 1] = a[0], (a[1] if len(a) > 1 else a[0])
        elif name == "rotate" and a:
            r = math.radians(a[0])
            t[:2, :2] = [[math.cos(r), -math.sin(r)], [math.sin(r), math.cos(r)]]
            if len(a) >= 3:
                t = np.array([[1, 0, a[1]], [0, 1, a[2]], [0, 0, 1.0]]) @ t @ np.array(
                    [[1, 0, -a[1]], [0, 1, -a[2]], [0, 0, 1.0]])
        elif name == "matrix" and len(a) == 6:
            t = np.array([[a[0], a[2], a[4]], [a[1], a[3], a[5]], [0, 0, 1.0]])
        elif name == "skewx" and a:
            t[0, 1] = math.tan(math.radians(a[0]))
        elif name == "skewy" and a:
            t[1, 0] = math.tan(math.radians(a[0]))
        m = m @ t
    return m


def _apply(m: np.ndarray, p: np.ndarray) -> np.ndarray:
    p = np.asarray(p, float).reshape(-1, 2)
    return p @ m[:2, :2].T + m[:2, 2]


# ------------------------------------------------------------------------------ path geometry
def _tokens(d: str):
    """Yield command letters and numbers; arc flags are handled by the caller."""
    i, n = 0, len(d)
    while i < n:
        c = d[i]
        if c.isalpha() and c not in "eE":
            yield c
            i += 1
        elif c in " ,\t\r\n":
            i += 1
        else:
            m = _NUM.match(d, i)
            if not m:
                raise SvgError(f"Invalid path data at character {i}")
            yield m.group(0)
            i = m.end()


def _arc_to_cubics(p0, rx, ry, phi, fa, fs, p1) -> list[tuple]:
    """SVG endpoint arc -> cubic Béziers (each <= 90 deg)."""
    rx, ry = abs(rx), abs(ry)
    if rx < 1e-12 or ry < 1e-12 or np.allclose(p0, p1):
        return [(p0, p0 + (p1 - p0) / 3, p0 + 2 * (p1 - p0) / 3, p1)] if not np.allclose(p0, p1) else []
    cp, sp = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1, y1 = cp * dx + sp * dy, -sp * dx + cp * dy
    lam = x1 * x1 / rx ** 2 + y1 * y1 / ry ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1 ** 2 - ry ** 2 * x1 ** 2
    den = rx ** 2 * y1 ** 2 + ry ** 2 * x1 ** 2
    co = math.sqrt(max(num / den, 0.0)) * (-1 if fa == fs else 1)
    cxp, cyp = co * rx * y1 / ry, -co * ry * x1 / rx
    cx, cy = cp * cxp - sp * cyp + (p0[0] + p1[0]) / 2, sp * cxp + cp * cyp + (p0[1] + p1[1]) / 2
    ang = lambda ux, uy, vx, vy: math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
    th1 = ang(1, 0, (x1 - cxp) / rx, (y1 - cyp) / ry)
    dth = ang((x1 - cxp) / rx, (y1 - cyp) / ry, (-x1 - cxp) / rx, (-y1 - cyp) / ry)
    if not fs and dth > 0:
        dth -= 2 * math.pi
    elif fs and dth < 0:
        dth += 2 * math.pi
    n = max(1, int(math.ceil(abs(dth) / (math.pi / 4) - 1e-9)))
    step = dth / n
    k = 4 / 3 * math.tan(step / 4)
    out = []
    for i in range(n):
        a0, a1 = th1 + i * step, th1 + (i + 1) * step

        def pt(a, dxr=0.0, dyr=0.0):
            ex, ey = rx * (math.cos(a) + dxr), ry * (math.sin(a) + dyr)
            return np.array([cp * ex - sp * ey + cx, sp * ex + cp * ey + cy])
        q0, q3 = pt(a0), pt(a1)
        q1 = pt(a0, -k * math.sin(a0), k * math.cos(a0))
        q2 = pt(a1, k * math.sin(a1), -k * math.cos(a1))
        out.append((q0, q1, q2, q3))
    if out:
        out[0] = (p0,) + out[0][1:]
        out[-1] = out[-1][:3] + (p1,)
    return out


def _parse_path(d: str) -> list[tuple[list, bool]]:
    """Path data -> list of (segments, closed); segment = ('L', p0, p1) | ('C', p0, c1, c2, p1)
    | ('Q', p0, c, p1) in user units."""
    toks = list(_tokens(d))
    subs: list[tuple[list, bool]] = []
    segs: list = []
    cur = start = np.zeros(2)
    last_c = last_q = None
    i, cmd = 0, None

    def flush(closed: bool):
        nonlocal segs
        if segs or closed:
            subs.append((segs, closed))
        segs = []

    while i < len(toks):
        t = toks[i]
        if isinstance(t, str) and t.isalpha():
            cmd, i = t, i + 1
            if cmd in "Zz":
                if segs:
                    if not np.allclose(cur, start):
                        segs.append(("L", cur, start))
                    flush(True)
                cur = start
                last_c = last_q = None
                cmd = None
                continue
        elif cmd is None:
            raise SvgError("Path data starts without a command")
        up, rel = cmd.upper(), cmd.islower()
        if up not in _CMD_ARGS:
            raise SvgError(f"Unknown path command '{cmd}'")
        nargs = _CMD_ARGS[up]
        if up == "A":
            vals = []
            for k in range(7):
                if i >= len(toks):
                    raise SvgError("Path data incomplete")
                s = toks[i]
                if k in (3, 4) and len(s) > 1 and s[0] in "01":
                    vals.append(float(s[0]))        # compact flags: "a1 1 0 00.5.5"
                    toks[i] = s[1:]
                else:
                    vals.append(float(s))
                    i += 1
        else:
            if i + nargs > len(toks):
                raise SvgError("Path data incomplete")
            try:
                vals = [float(x) for x in toks[i:i + nargs]]
            except ValueError:
                raise SvgError("Invalid number in path data") from None
            i += nargs
        ox, oy = (cur[0], cur[1]) if rel else (0.0, 0.0)
        if up == "M":
            flush(False)
            cur = start = np.array([vals[0] + ox, vals[1] + oy])
            cmd = "l" if rel else "L"
            last_c = last_q = None
            continue
        if up == "L":
            nxt = np.array([vals[0] + ox, vals[1] + oy])
            segs.append(("L", cur, nxt))
        elif up == "H":
            nxt = np.array([vals[0] + ox, cur[1]])
            segs.append(("L", cur, nxt))
        elif up == "V":
            nxt = np.array([cur[0], vals[0] + oy])
            segs.append(("L", cur, nxt))
        elif up in "CS":
            if up == "C":
                c1 = np.array([vals[0] + ox, vals[1] + oy])
                c2, nxt = np.array([vals[2] + ox, vals[3] + oy]), np.array([vals[4] + ox, vals[5] + oy])
            else:
                c1 = 2 * cur - last_c if last_c is not None else cur
                c2, nxt = np.array([vals[0] + ox, vals[1] + oy]), np.array([vals[2] + ox, vals[3] + oy])
            segs.append(("C", cur, c1, c2, nxt))
            last_c, last_q = c2, None
            cur = nxt
            continue
        elif up in "QT":
            if up == "Q":
                c = np.array([vals[0] + ox, vals[1] + oy])
                nxt = np.array([vals[2] + ox, vals[3] + oy])
            else:
                c = 2 * cur - last_q if last_q is not None else cur
                nxt = np.array([vals[0] + ox, vals[1] + oy])
            segs.append(("Q", cur, c, nxt))
            last_q, last_c = c, None
            cur = nxt
            continue
        else:   # A
            nxt = np.array([vals[5] + ox, vals[6] + oy])
            for q in _arc_to_cubics(cur, vals[0], vals[1], math.radians(vals[2]), vals[3] > 0.5, vals[4] > 0.5, nxt):
                segs.append(("C",) + q)
        cur = nxt
        last_c = last_q = None
    flush(False)
    return subs


def _flatten(subs, m: np.ndarray, tol: float) -> list[tuple[np.ndarray, bool]]:
    """Apply ``m`` to control points, flatten to ``tol`` (mm)."""
    out = []
    for segs, closed in subs:
        pts: list[np.ndarray] = []
        for s in segs:
            q = _apply(m, np.array(s[1:]))
            if s[0] == "L":
                a = q
            elif s[0] == "C":
                mm = max(np.hypot(*(q[0] - 2 * q[1] + q[2])), np.hypot(*(q[1] - 2 * q[2] + q[3])))
                n = int(min(max(math.ceil(math.sqrt(0.75 * mm / tol)), 1), 400))
                t = np.linspace(0, 1, n + 1)[:, None]
                a = (1 - t) ** 3 * q[0] + 3 * (1 - t) ** 2 * t * q[1] + 3 * (1 - t) * t ** 2 * q[2] + t ** 3 * q[3]
            else:
                mm = np.hypot(*(q[0] - 2 * q[1] + q[2]))
                n = int(min(max(math.ceil(math.sqrt(mm / (4 * tol))), 1), 400))
                t = np.linspace(0, 1, n + 1)[:, None]
                a = (1 - t) ** 2 * q[0] + 2 * (1 - t) * t * q[1] + t ** 2 * q[2]
            pts.extend(a if not pts else a[1:])
        if len(pts) >= 2:
            out.append((np.array(pts), closed))
    return out


def _shape_path(e: ET.Element, tag: str) -> list[tuple[list, bool]] | None:
    """Convert basic shapes to the path representation (user units)."""
    P = lambda x, y: np.array([x, y], float)
    if tag == "path":
        return _parse_path(e.get("d") or "")
    if tag in ("polyline", "polygon"):
        v = [float(x) for x in _NUM.findall(e.get("points") or "")]
        pts = [P(v[i], v[i + 1]) for i in range(0, len(v) - 1, 2)]
        if len(pts) < 2:
            return []
        segs = [("L", a, b) for a, b in zip(pts[:-1], pts[1:])]
        if tag == "polygon":
            segs.append(("L", pts[-1], pts[0]))
        return [(segs, tag == "polygon")]
    if tag == "line":
        return [([("L", P(_num(e, "x1"), _num(e, "y1")), P(_num(e, "x2"), _num(e, "y2")))], False)]
    if tag == "rect":
        x, y, w, h = _num(e, "x"), _num(e, "y"), _num(e, "width"), _num(e, "height")
        if w <= 0 or h <= 0:
            return []
        rx, ry = _num(e, "rx", -1), _num(e, "ry", -1)
        rx, ry = (ry if rx < 0 else rx), (rx if ry < 0 else ry)
        rx, ry = min(max(rx, 0), w / 2), min(max(ry, 0), h / 2)
        if rx <= 0 or ry <= 0:
            c = [P(x, y), P(x + w, y), P(x + w, y + h), P(x, y + h)]
            return [([("L", c[i], c[(i + 1) % 4]) for i in range(4)], True)]
        d = (f"M{x + rx},{y} H{x + w - rx} A{rx},{ry} 0 0 1 {x + w},{y + ry} V{y + h - ry} "
             f"A{rx},{ry} 0 0 1 {x + w - rx},{y + h} H{x + rx} A{rx},{ry} 0 0 1 {x},{y + h - ry} "
             f"V{y + ry} A{rx},{ry} 0 0 1 {x + rx},{y} Z")
        return _parse_path(d)
    if tag in ("circle", "ellipse"):
        cx, cy = _num(e, "cx"), _num(e, "cy")
        rx, ry = (_num(e, "r"),) * 2 if tag == "circle" else (_num(e, "rx"), _num(e, "ry"))
        if rx <= 0 or ry <= 0:
            return []
        d = f"M{cx + rx},{cy} A{rx},{ry} 0 1 1 {cx - rx},{cy} A{rx},{ry} 0 1 1 {cx + rx},{cy} Z"
        return _parse_path(d)
    return None


# ------------------------------------------------------------------------------ import
def _style(e: ET.Element, parent: dict) -> dict:
    st = dict(parent)
    for k in ("stroke", "fill", "display", "visibility"):
        if e.get(k) is not None:
            st[k] = e.get(k).strip()
    for decl in (e.get("style") or "").split(";"):
        if ":" in decl:
            k, v = decl.split(":", 1)
            if k.strip() in ("stroke", "fill", "display", "visibility"):
                st[k.strip()] = v.strip()
    return st


def import_svg(data: bytes | str, cfg: Config | None = None, opts: SvgOptions | None = None,
               name: str = "") -> SvgImport:
    opts = opts or SvgOptions()
    raw = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    if len(raw) > opts.max_bytes:
        raise SvgError(f"SVG file too large ({len(raw) / 1e6:.1f} MB, allowed {opts.max_bytes / 1e6:.0f} MB)")
    if re.search(rb"<!ENTITY|<!DOCTYPE[^>]*\[", raw, re.I):
        raise SvgError("SVG with DOCTYPE entities is rejected for security reasons")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise SvgError(f"Not a valid SVG file: {e}") from None
    if _tag(root) != "svg":
        raise SvgError("Not an SVG file (root element is not <svg>)")
    warnings: list[str] = []

    vb = [float(x) for x in _NUM.findall(root.get("viewBox") or "")]
    w_mm, h_mm = _length(root.get("width")), _length(root.get("height"))
    if len(vb) == 4 and vb[2] > 0 and vb[3] > 0:
        w_mm = w_mm if w_mm else vb[2] * _UNITS["px"]
        h_mm = h_mm if h_mm else vb[3] * _UNITS["px"]
        s = min(w_mm / vb[2], h_mm / vb[3])
        base = np.array([[s, 0, (w_mm - s * vb[2]) / 2 - s * vb[0]],
                         [0, s, (h_mm - s * vb[3]) / 2 - s * vb[1]], [0, 0, 1.0]])
    else:
        if not w_mm or not h_mm:
            raise SvgError("An SVG without a viewBox and without width/height cannot be scaled")
        # no viewBox: one user unit == the unit of the width attribute (px if none)
        m_unit = re.search(r"([a-z]+)\s*$", (root.get("width") or "").strip())
        f = _UNITS.get(m_unit.group(1), _UNITS["px"]) if m_unit else _UNITS["px"]
        base = np.diag([f, f, 1.0])
    flip = np.array([[1, 0, 0], [0, -1, h_mm], [0, 0, 1.0]])
    root_m = flip @ base

    lut = None
    if cfg is not None and opts.color_map is None:
        lut = [(_parse_color(c.hex) or (0, 0, 0)) for c in cfg.colors]
    ncol = len(cfg.colors) if cfg else 1

    def slot(rgb: tuple[int, int, int]) -> int:
        if opts.color_map:
            for k, v in opts.color_map.items():
                if (_parse_color(k) or None) == rgb:
                    return int(v)
            return opts.default_color
        if lut is None:
            return opts.default_color
        return int(np.argmin([sum((a - b) ** 2 for a, b in zip(rgb, c)) for c in lut]))

    strokes: list[Stroke] = []
    npts = 0
    warned: set[str] = set()

    def walk(e: ET.Element, m: np.ndarray, parent: dict) -> None:
        nonlocal npts
        tag = _tag(e)
        if tag in _SKIP:
            if tag in _WARN_TAGS and tag not in warned:
                warned.add(tag)
            return
        st = _style(e, parent)
        if st.get("display") == "none" or st.get("visibility") in ("hidden", "collapse"):
            return
        mm = m @ _matrix(e.get("transform") or "") if tag != "svg" or e is not root else m
        if tag in ("svg", "g", "a", "switch"):
            for ch in e:
                walk(ch, mm, st)
            return
        if tag in _WARN_TAGS:
            if tag not in warned:
                warned.add(tag)
                warnings.append(f"{_WARN_TAGS[tag]} are not imported")
            return
        subs = _shape_path(e, tag)
        if subs is None:
            return
        stroke_c = _parse_color(st.get("stroke"))
        fill_c = _parse_color(st.get("fill", "black"))
        rgb = stroke_c if stroke_c is not None else fill_c
        if rgb is None or (opts.skip_white and min(rgb) >= 250):
            return
        col = min(slot(rgb), max(ncol - 1, 0)) if cfg else slot(rgb)
        for pts, closed in _flatten(subs, mm, opts.tolerance_mm):
            npts += len(pts)
            if npts > opts.max_points:
                raise SvgError(f"SVG has too many points (more than {opts.max_points})")
            strokes.append(Stroke(pts, col))

    walk(root, root_m, {})
    if not strokes:
        warnings.append("The SVG file contains no drawable lines")
    return SvgImport(clean(Drawing(strokes, name)), float(w_mm), float(h_mm), warnings)


def svg_to_drawing(data: bytes | str, cfg: Config | None = None, opts: SvgOptions | None = None,
                   name: str = "") -> Drawing:
    return import_svg(data, cfg, opts, name).drawing


__all__ = ["SvgError", "SvgOptions", "SvgImport", "import_svg", "svg_to_drawing"]
