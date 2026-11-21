"""Hand-drawn single-line pictograms (emoji-like) that plot cleanly: a few strokes each, no filled areas.

Every icon fits a 100 x 100 box centred on the origin (y up). :func:`icon_strokes` returns the polylines.
"""
from __future__ import annotations

import math

import numpy as np

from .drawing import Stroke

Pts = list[tuple[float, float]]


def _arc(cx, cy, r, a0, a1, n=24, ry=None) -> Pts:
    ry = r if ry is None else ry
    return [(cx + r * math.cos(math.radians(a0 + (a1 - a0) * i / n)),
             cy + ry * math.sin(math.radians(a0 + (a1 - a0) * i / n))) for i in range(n + 1)]


def _circle(cx, cy, r, ry=None) -> Pts:
    return _arc(cx, cy, r, 0, 360, 36, ry)


def _bez(p0, p1, p2, p3, n=16) -> Pts:
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        out.append((u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                    u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]))
    return out


def _star(cx, cy, r, points=5, inner=0.4, rot=90) -> Pts:
    out = []
    for i in range(points * 2 + 1):
        a = math.radians(rot + i * 180 / points)
        rr = r if i % 2 == 0 else r * inner
        out.append((cx + rr * math.cos(a), cy + rr * math.sin(a)))
    return out


def _heart() -> list[Pts]:
    return [_bez((0, -42), (-62, 0), (-44, 44), (0, 20), 20) + _bez((0, 20), (44, 44), (62, 0), (0, -42), 20)[1:]]


def _smiley() -> list[Pts]:
    return [_circle(0, 0, 44), _circle(-15, 12, 3.5), _circle(15, 12, 3.5), _arc(0, 2, 26, 205, 335, 20)]


def _sad() -> list[Pts]:
    return [_circle(0, 0, 44), _circle(-15, 12, 3.5), _circle(15, 12, 3.5), _arc(0, -28, 24, 25, 155, 20)]


def _sun() -> list[Pts]:
    out = [_circle(0, 0, 18)]
    for i in range(12):
        a = math.radians(i * 30)
        out.append([(26 * math.cos(a), 26 * math.sin(a)), ((40 if i % 2 == 0 else 34) * math.cos(a), (40 if i % 2 == 0 else 34) * math.sin(a))])
    return out


def _simple_cloud() -> list[Pts]:
    pts = _arc(-26, -10, 18, 100, 270, 14) + [(26, -28)] + _arc(26, -10, 18, 270, 440, 14)[1:]
    pts += _arc(8, 8, 22, 40, 160, 14)[::-1][:0]
    pts += _arc(10, 6, 24, 20, 160, 16) + _arc(-12, 6, 0.01, 0, 0, 1)[:0]
    pts += [(-26 + 18 * math.cos(math.radians(100)), -10 + 18 * math.sin(math.radians(100)))]
    return [pts]


def _bolt() -> list[Pts]:
    return [[(12, 46), (-24, -4), (-2, -4), (-14, -46), (26, 8), (4, 8), (12, 46)]]


def _flower() -> list[Pts]:
    out = [_circle(0, 14, 7)]
    for i in range(6):
        a = math.radians(90 + i * 60)
        cx, cy = 20 * math.cos(a), 14 + 20 * math.sin(a)
        out.append(_circle(cx, cy, 9))
    out.append([(0, 6), (0, -44)])
    out.append(_bez((0, -26), (14, -22), (22, -30), (24, -38), 10))
    return out


def _tree() -> list[Pts]:
    return [_circle(0, 14, 30), [(-6, -15), (-6, -44), (6, -44), (6, -15)]]


def _house() -> list[Pts]:
    return [[(-38, -2), (0, 38), (38, -2)], [(-30, 4), (-30, -40), (30, -40), (30, 4)], [(-8, -40), (-8, -16), (8, -16), (8, -40)]]


def _check() -> list[Pts]:
    return [[(-38, 0), (-12, -28), (40, 34)]]


def _cross() -> list[Pts]:
    return [[(-34, 34), (34, -34)], [(-34, -34), (34, 34)]]


def _plus() -> list[Pts]:
    return [[(0, 38), (0, -38)], [(-38, 0), (38, 0)]]


def _arrow() -> list[Pts]:
    return [[(-44, 0), (44, 0)], [(20, 26), (46, 0), (20, -26)]]


def _note() -> list[Pts]:
    return [_circle(-14, -30, 12, 9), [(-2, -30), (-2, 40), (30, 26)], [(-2, 24), (30, 10)]]


def _envelope() -> list[Pts]:
    return [[(-44, 30), (44, 30), (44, -30), (-44, -30), (-44, 30)], [(-44, 30), (0, -6), (44, 30)]]


def _bulb() -> list[Pts]:
    return [_arc(0, 14, 28, 235, -55, 34), [(-12, -12), (-12, -24), (12, -24), (12, -12)], [(-10, -32), (10, -32)],
            [(-6, -40), (6, -40)], [(-6, -12), (-6, 8), (0, 14), (6, 8), (6, -12)]]


def _bubble() -> list[Pts]:
    return [[(-42, 34), (42, 34), (42, -22), (0, -22), (-22, -42), (-22, -22), (-42, -22), (-42, 34)]]


def _cup() -> list[Pts]:
    return [[(-30, 26), (-24, -30), (24, -30), (30, 26), (-30, 26)], _arc(30, 4, 14, 90, -90, 12),
            [(-12, 34), (-8, 44)], [(0, 34), (4, 44)], [(12, 34), (16, 44)]]


def _gear() -> list[Pts]:
    out = []
    n = 8
    pts = []
    for i in range(n):
        a = i * 360 / n
        for da, r in ((-14, 30), (-9, 42), (9, 42), (14, 30)):
            pts.append((r * math.cos(math.radians(a + da)), r * math.sin(math.radians(a + da))))
    pts.append(pts[0])
    return [pts, _circle(0, 0, 13)]


def _rocket() -> list[Pts]:
    return [_bez((0, 46), (22, 22), (22, -14), (14, -26), 14) + [(-14, -26)] + _bez((-14, -26), (-22, -14), (-22, 22), (0, 46), 14)[:],
            _circle(0, 8, 8), [(14, -14), (30, -32), (14, -26)], [(-14, -14), (-30, -32), (-14, -26)],
            [(-6, -34), (0, -46), (6, -34)]]


def _crown() -> list[Pts]:
    return [[(-40, -26), (-44, 24), (-20, 0), (0, 32), (20, 0), (44, 24), (40, -26), (-40, -26)], [(-40, -36), (40, -36)]]


def _diamond() -> list[Pts]:
    return [[(-26, 30), (26, 30), (44, 6), (0, -42), (-44, 6), (-26, 30)], [(-44, 6), (44, 6)], [(-26, 30), (-12, 6), (0, -42), (12, 6), (26, 30)]]


def _pin() -> list[Pts]:
    return [_bez((0, -44), (-12, -24), (-30, -8), (-30, 12)) + _arc(0, 12, 30, 180, 0, 24)[1:] +
            _bez((30, 12), (30, -8), (12, -24), (0, -44))[1:], _circle(0, 12, 10)]


def _butterfly() -> list[Pts]:
    return [_bez((0, 4), (-20, 44), (-50, 36), (-40, 8), 12) + _bez((-40, 8), (-34, -10), (-14, -8), (0, 0), 10)[1:],
            _bez((0, 0), (-16, -12), (-34, -44), (-22, -34), 10)[::-1][:0] + _bez((0, -2), (-24, -10), (-30, -44), (-14, -36), 10) + [(0, -6)],
            _bez((0, 4), (20, 44), (50, 36), (40, 8), 12) + _bez((40, 8), (34, -10), (14, -8), (0, 0), 10)[1:],
            _bez((0, -2), (24, -10), (30, -44), (14, -36), 10) + [(0, -6)], [(0, 14), (0, -30)],
            [(0, 14), (-8, 30)], [(0, 14), (8, 30)]]


ICONS: dict[str, tuple[str, callable]] = {
    "smiley": ("Smiley", _smiley), "sad": ("Sad face", _sad), "heart": ("Heart", _heart),
    "star": ("Star", lambda: [_star(0, 0, 46)]), "sun": ("Sun", _sun), "cloud": ("Cloud", _simple_cloud),
    "bolt": ("Lightning", _bolt), "flower": ("Flower", _flower), "tree": ("Tree", _tree), "house": ("House", _house),
    "check": ("Check mark", _check), "cross": ("Cross", _cross), "plus": ("Plus", _plus), "arrow": ("Arrow", _arrow),
    "note": ("Music note", _note), "envelope": ("Envelope", _envelope), "bulb": ("Light bulb", _bulb),
    "bubble": ("Speech bubble", _bubble), "cup": ("Coffee", _cup), "gear": ("Gear", _gear), "rocket": ("Rocket", _rocket),
    "crown": ("Crown", _crown), "diamond": ("Diamond", _diamond), "pin": ("Pin", _pin), "butterfly": ("Butterfly", _butterfly),
}
# emoji that map to a hand-drawn icon instead of being traced from the system emoji font
EMOJI_ICON = {"😀": "smiley", "😊": "smiley", "🙂": "smiley", "☺": "smiley", "😃": "smiley", "😄": "smiley",
              "☹": "sad", "🙁": "sad", "😢": "sad", "❤": "heart", "♥": "heart", "💖": "heart", "💗": "heart",
              "⭐": "star", "★": "star", "☆": "star", "☀": "sun", "🌞": "sun", "☁": "cloud", "⚡": "bolt",
              "🌸": "flower", "🌼": "flower", "🌳": "tree", "🌲": "tree", "🏠": "house", "✓": "check", "✔": "check",
              "✗": "cross", "✘": "cross", "❌": "cross", "➕": "plus", "➡": "arrow", "→": "arrow", "🎵": "note",
              "♪": "note", "✉": "envelope", "📧": "envelope", "💡": "bulb", "💬": "bubble", "☕": "cup", "⚙": "gear",
              "🚀": "rocket", "👑": "crown", "💎": "diamond", "📍": "pin", "🦋": "butterfly"}


def icon_strokes(icon_id: str, size_mm: float = 40.0, color: int = 0) -> list[Stroke]:
    """Strokes of an icon scaled so its larger side is ``size_mm``; centred on the origin."""
    if icon_id not in ICONS:
        raise KeyError(icon_id)
    polys = [np.array(p, float) for p in ICONS[icon_id][1]() if len(p) >= 2]
    allp = np.concatenate(polys)
    c = (allp.min(axis=0) + allp.max(axis=0)) / 2
    span = float((allp.max(axis=0) - allp.min(axis=0)).max()) or 1.0
    k = size_mm / span
    return [Stroke((p - c) * k, color) for p in polys]


def icon_list() -> list[dict]:
    return [{"id": i, "label": v[0]} for i, v in ICONS.items()]


__all__ = ["ICONS", "EMOJI_ICON", "icon_strokes", "icon_list"]
