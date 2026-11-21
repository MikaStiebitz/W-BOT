"""Procedural shapes for the formula layout: stretchy delimiters, radicals, arrows, accents.

Everything is in font units (cap height 14, y up, origin at the left end of the baseline) and returned as
``(width, strokes)`` where strokes are lists of ``(x, y)`` points.
"""
from __future__ import annotations

import math

Pts = list[tuple[float, float]]


def _bez(p0, p1, p2, n: int = 14) -> Pts:
    """Quadratic Bezier through three control points."""
    out = []
    for i in range(n + 1):
        t = i / n
        out.append(((1 - t) ** 2 * p0[0] + 2 * t * (1 - t) * p1[0] + t * t * p2[0],
                    (1 - t) ** 2 * p0[1] + 2 * t * (1 - t) * p1[1] + t * t * p2[1]))
    return out


def circle(cx: float, cy: float, r: float, n: int = 28) -> Pts:
    return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n + 1)]


def delimiter(kind: str, top: float, bottom: float) -> tuple[float, list[Pts]]:
    """Stretched delimiter spanning ``bottom`` .. ``top`` (y). ``kind``: ( ) [ ] { } | ‖ ⟨ ⟩ ⌊ ⌋ ⌈ ⌉ ."""
    h = top - bottom
    mid = (top + bottom) / 2
    w = min(1.6 + 0.07 * h, 4.2)                      # horizontal extent of the curve
    pad = 1.0
    if kind in "()":
        sgn = 1 if kind == "(" else -1
        x0, x1 = (w if sgn > 0 else 0.0), (0.0 if sgn > 0 else w)
        # a ( opens to the right: ends at x0 (right), belly at the left
        a, b = (w, 0.0) if kind == "(" else (0.0, w)
        pts = _bez((a, top), (b - (a - b) * 0.55, mid), (a, bottom), 22)
        return w + 2 * pad, [[(x + pad, y) for x, y in pts]]
    if kind in "[]":
        a, b = (w, 0.0) if kind == "[" else (0.0, w)
        return w + 2 * pad, [[(a + pad, top), (b + pad, top), (b + pad, bottom), (a + pad, bottom)]]
    if kind in "{}":
        a, b = (w, 0.0) if kind == "{" else (0.0, w)       # a: ends, b: the cusp side
        c = (a + b) / 2
        q1, q2 = top - h * 0.25, bottom + h * 0.25
        up = _bez((a, top), (c, top), (c, q1), 8) + _bez((c, q1), (c, mid), (b, mid), 8)[1:]
        dn = _bez((b, mid), (c, mid), (c, q2), 8) + _bez((c, q2), (c, bottom), (a, bottom), 8)[1:]
        return w + 2 * pad, [[(x + pad, y) for x, y in up + dn[1:]]]
    if kind == "|":
        return 2 * pad + 0.5, [[(pad, top), (pad, bottom)]]
    if kind == "‖":
        return 2 * pad + 3.0, [[(pad, top), (pad, bottom)], [(pad + 3, top), (pad + 3, bottom)]]
    if kind in "⟨<":
        return w + 2 * pad, [[(w + pad, top), (pad, mid), (w + pad, bottom)]]
    if kind in "⟩>":
        return w + 2 * pad, [[(pad, top), (w + pad, mid), (pad, bottom)]]
    if kind == "⌊":
        return w + 2 * pad, [[(pad, top), (pad, bottom), (w + pad, bottom)]]
    if kind == "⌋":
        return w + 2 * pad, [[(w + pad, top), (w + pad, bottom), (pad, bottom)]]
    if kind == "⌈":
        return w + 2 * pad, [[(w + pad, top), (pad, top), (pad, bottom)]]
    if kind == "⌉":
        return w + 2 * pad, [[(pad, top), (w + pad, top), (w + pad, bottom)]]
    return 2 * pad, []


def radical(body_w: float, top: float, bottom: float, s: float = 1.0) -> tuple[float, list[Pts]]:
    """Square-root sign: returns (width of the sign, strokes); the bar runs on to ``sign_w + body_w``."""
    h = top - bottom
    sign_w = 6.5 * s + 0.12 * h
    pts = [(0.0, bottom + 0.42 * h), (sign_w * 0.22, bottom + 0.48 * h), (sign_w * 0.55, bottom),
           (sign_w, top), (sign_w + body_w + 1.5 * s, top)]
    return sign_w, [pts]


def arrow_h(width: float, y: float, *, double: bool = False, both: bool = False, left: bool = False,
            head: float = 3.2) -> list[Pts]:
    """Horizontal arrow of ``width`` at height ``y`` (single or double line, one or both heads)."""
    out: list[Pts] = []
    gap = 1.7 if double else 0.0
    ys = [y - gap, y + gap] if double else [y]
    x0, x1 = 0.0, width
    for yy in ys:
        out.append([(x0 + (head * 0.6 if (left or both) and double else 0), yy),
                    (x1 - (head * 0.6 if not left and double else 0), yy)] if double else [(x0, yy), (x1, yy)])
    def tip(x: float, direction: int) -> Pts:
        return [(x - direction * head, y + head * 0.8), (x, y), (x - direction * head, y - head * 0.8)]
    if both or not left:
        out.append(tip(x1, 1))
    if both or left:
        out.append(tip(x0, -1))
    return out


def accent(kind: str, width: float, y: float) -> list[Pts]:
    """Accent glyph centred over ``width`` with its lowest point at ``y``."""
    c = width / 2
    if kind == "hat":
        return [[(c - 2.8, y), (c, y + 2.6), (c + 2.8, y)]]
    if kind == "bar":
        return [[(1.0, y + 1.2), (width - 1.0, y + 1.2)]]
    if kind == "vec":
        return [[(c - 3.5, y + 1.2), (c + 3.5, y + 1.2)], [(c + 1.6, y + 2.8), (c + 3.5, y + 1.2), (c + 1.6, y - 0.4)]]
    if kind == "dot":
        return [[(c, y + 0.6), (c + 0.2, y + 0.8)]]
    if kind == "ddot":
        return [[(c - 1.8, y + 0.6), (c - 1.6, y + 0.8)], [(c + 1.8, y + 0.6), (c + 2.0, y + 0.8)]]
    if kind == "tilde":
        return [[(c - 3.2, y + 0.6), (c - 1.6, y + 2.2), (c, y + 1.2), (c + 1.6, y + 0.2), (c + 3.2, y + 1.8)]]
    return []


def special(ch: str, h: float = 14.0) -> tuple[float, list[Pts]] | None:
    """Glyphs the Hershey sets lack: ≈ ⇒ ⇐ ⇔ ↔ ∀ ∅ ′ ⋯ ∎ ⊆ ⊇ ∉ ∼ ⟂ ⌀."""
    ax = 5.2                                            # axis height
    if ch == "≈":
        wave = lambda y: [(0.0, y), (2.2, y + 1.7), (4.4, y), (6.6, y - 1.7), (8.8, y)]
        return 10.0, [wave(ax + 2.0), wave(ax - 2.0)]
    if ch in "⇒⟹":
        return 13.0, arrow_h(13.0, ax, double=True)
    if ch in "⇐⟸":
        return 13.0, arrow_h(13.0, ax, double=True, left=True)
    if ch in "⇔⟺":
        return 14.0, arrow_h(14.0, ax, double=True, both=True)
    if ch == "↔":
        return 13.0, arrow_h(13.0, ax, both=True)
    if ch == "⟶":
        return 16.0, arrow_h(16.0, ax)
    if ch == "∀":
        return 9.0, [[(0.0, 14.0), (4.5, 0.0), (9.0, 14.0)], [(2.0, 7.4), (7.0, 7.4)]]
    if ch == "∅":
        return 11.0, [circle(5.5, 7.0, 5.4), [(1.2, 0.5), (9.8, 13.5)]]
    if ch == "′":
        return 2.6, [[(1.9, 14.0), (0.9, 9.4)]]
    if ch == "⋯":
        return 14.0, [[(1.0, ax), (1.3, ax)], [(6.5, ax), (6.8, ax)], [(12.0, ax), (12.3, ax)]]
    if ch == "⊆":
        return 9.0, [[(9.0, 12.0), (0.0, 12.0), (0.0, 2.0), (9.0, 2.0)], [(0.0, -1.0), (9.0, -1.0)]]
    if ch == "⊇":
        return 9.0, [[(0.0, 12.0), (9.0, 12.0), (9.0, 2.0), (0.0, 2.0)], [(0.0, -1.0), (9.0, -1.0)]]
    if ch == "∉":
        return 9.0, [[(9.0, 12.0), (0.0, 12.0), (0.0, 2.0), (9.0, 2.0)], [(0.0, 7.0), (7.0, 7.0)], [(7.5, 13.0), (1.5, 1.0)]]
    if ch == "∎":
        return 9.0, [[(0.0, 0.0), (9.0, 0.0), (9.0, 9.0), (0.0, 9.0), (0.0, 0.0)]]
    return None


__all__ = ["delimiter", "radical", "arrow_h", "accent", "special", "circle"]
