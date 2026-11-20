"""Single-stroke (Hershey simplex) text for plotting.

Glyph data lives in ``wbot/data/hershey_simplex.json``: baseline y = 0, cap
height 14 font units, every glyph carries its advance width. ``size_mm`` always
means the *cap height*. German umlauts are composed from the base letter plus
two dots (the font has none); ``ß`` is drawn as ``ss``.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import numpy as np

from .drawing import Stroke

FONT_PATH = Path(__file__).resolve().parent / "data" / "hershey_simplex.json"
_UMLAUT = {"ä": "a", "ö": "o", "ü": "u", "Ä": "A", "Ö": "O", "Ü": "U"}


@lru_cache(maxsize=1)
def _font() -> dict:
    return json.loads(FONT_PATH.read_text(encoding="utf-8"))


def _glyph(ch: str) -> tuple[float, list[list[tuple[float, float]]]]:
    """(advance, strokes) in font units for one character (composed if needed)."""
    g = _font()["glyphs"]
    if ch in g:
        return float(g[ch]["advance"]), [[tuple(p) for p in s] for s in g[ch]["strokes"]]
    if ch in _UMLAUT:
        adv, strokes = _glyph(_UMLAUT[ch])
        y = 16.8 if ch.isupper() else 12.2
        strokes = strokes + [[(adv * f, y), (adv * f, y + 0.6)] for f in (0.3, 0.7)]
        return adv, strokes
    if ch == "ß":
        a1, s1 = _glyph("s")
        return 2 * a1 + _font()["letter_spacing"], s1 + [[(x + a1 + _font()["letter_spacing"], y) for x, y in s]
                                                           for s in s1]
    return _glyph("?") if ch != "?" else (7.0, [])


def _line_width_units(line: str) -> float:
    sp = _font()["letter_spacing"]
    return sum(_glyph(c)[0] for c in line) + sp * max(len(line) - 1, 0)


def _lines(text: str) -> list[str]:
    return text.replace("\\n", "\n").split("\n")


def measure(text: str, size_mm: float, line_spacing: float = 1.0) -> tuple[float, float]:
    """(width, height) in mm of the block; height = cap height + line pitch * (lines - 1)."""
    k = size_mm / _font()["cap_height"]
    lines = _lines(text)
    w = max((_line_width_units(l) for l in lines), default=0.0) * k
    pitch = _font()["line_height"] * k * line_spacing
    return w, size_mm + pitch * (len(lines) - 1)


def text_strokes(text: str, size_mm: float, x: float, y: float, *, anchor: str = "left",
                 valign: str = "baseline", line_spacing: float = 1.0, color: int = 0) -> list[Stroke]:
    """Strokes for ``text``. ``(x, y)`` is the anchor: ``anchor`` left/center/right is
    the horizontal reference of every line, ``valign`` baseline/center/top the vertical
    reference of the whole block (first line's baseline / block middle / top of caps)."""
    k = size_mm / _font()["cap_height"]
    sp = _font()["letter_spacing"]
    lines = _lines(text)
    pitch = _font()["line_height"] * k * line_spacing
    _, block_h = measure(text, size_mm, line_spacing)
    y0 = {"baseline": y, "center": y + block_h / 2 - size_mm, "top": y - size_mm}[valign]
    out: list[Stroke] = []
    for i, line in enumerate(lines):
        width = _line_width_units(line) * k
        x0 = {"left": x, "center": x - width / 2, "right": x - width}[anchor]
        base = y0 - i * pitch
        pen = 0.0
        for ch in line:
            adv, strokes = _glyph(ch)
            for s in strokes:
                if len(s) >= 2:
                    pts = np.array(s, float) * k + [x0 + pen * k, base]
                    out.append(Stroke(pts, color))
            pen += adv + sp
    return out


__all__ = ["measure", "text_strokes", "FONT_PATH"]
