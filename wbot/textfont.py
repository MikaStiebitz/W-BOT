"""Single-stroke (Hershey) text for plotting.

Glyph data lives in ``wbot/data/hershey_simplex.json`` (the default font "simple") and
``wbot/data/hershey_fonts.json`` (the other fonts and the symbol sets). Glyphs are in font
units: baseline y = 0, cap height 14, every glyph carries its advance width.
``size_mm`` always means the *cap height*. Characters a font does not have fall back to
Greek letters and maths symbols (so ``α``, ``≤`` or ``→`` work in every font), then to
composed German umlauts, then to ``?``.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from .drawing import Stroke

DATA = Path(__file__).resolve().parent / "data"
FONT_PATH = DATA / "hershey_simplex.json"          # kept for older imports
EXTRA_PATH = DATA / "hershey_fonts.json"
_UMLAUT = {"ä": "a", "ö": "o", "ü": "u", "Ä": "A", "Ö": "O", "Ü": "U"}

# id -> (label, source font in hershey_fonts.json (None = the simplex file), letter gap in font units)
FONTS: dict[str, tuple[str, str | None, float]] = {
    "simple": ("Simple", None, 1.5),
    "sans": ("Sans", "futuram", 1.5),
    "serif": ("Serif", "timesr", 1.5),
    "serif-bold": ("Serif bold", "timesrb", 1.5),
    "italic": ("Italic", "timesi", 1.0),
    "italic-bold": ("Italic bold", "timesib", 1.0),
    "script": ("Handwriting", "scripts", 0.0),
    "script-bold": ("Handwriting bold", "scriptc", 0.0),
    "cursive": ("Cursive", "cursive", 0.0),
    "title": ("Title", "rowmant", 1.5),
    "gothic": ("Old English", "gothiceng", 1.0),
}
DEFAULT_FONT = "simple"

_GREEK_LOW = dict(zip("αβγδεζηθικλμνξοπρστυφχψω", "abcdefghijklmnopqrstuvwx"))
_GREEK_LOW["ς"] = "r"
_GREEK_UP = dict(zip("ΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ", "ABCDEFGHIJKLMNOPQRSTUVWX"))
# Unicode -> (symbol set, glyph index); the sets are "mathupp" and "symbolic" of hershey_fonts.json
SYMBOLS: dict[str, tuple[str, int]] = {
    "±": ("mathupp", 1), "∓": ("mathupp", 2), "×": ("mathupp", 3), "·": ("mathupp", 4), "≤": ("mathupp", 6),
    "≥": ("mathupp", 7), "∏": ("mathupp", 26), "∑": ("mathupp", 27), "≠": ("mathupp", 31), "≡": ("mathupp", 32),
    "∝": ("mathupp", 62), "∞": ("mathupp", 63), "°": ("mathupp", 64), "⊂": ("mathupp", 68), "∪": ("mathupp", 69),
    "⊃": ("mathupp", 70), "∩": ("mathupp", 71), "∈": ("mathupp", 72), "→": ("mathupp", 73), "↑": ("mathupp", 74),
    "←": ("mathupp", 75), "↓": ("mathupp", 76), "∂": ("mathupp", 77), "∇": ("mathupp", 78), "√": ("mathupp", 66),
    "∫": ("mathupp", 80), "∃": ("mathupp", 86), "ℵ": ("mathupp", 87), "÷": ("mathupp", 88), "∥": ("mathupp", 89),
    "⊥": ("mathupp", 90), "∠": ("mathupp", 92), "∴": ("mathupp", 94), "∼": ("mathupp", 95), "−": ("mathupp", 13),
    "♠": ("symbolic", 77), "♥": ("symbolic", 78), "♦": ("symbolic", 79), "♣": ("symbolic", 80), "§": ("symbolic", 73),
    "†": ("symbolic", 74), "‡": ("symbolic", 75), "☆": ("symbolic", 44), "★": ("symbolic", 94), "○": ("symbolic", 40),
    "□": ("symbolic", 41), "△": ("symbolic", 42), "◇": ("symbolic", 43), "✝": ("symbolic", 75),
}
_ALIASES = {"–": "-", "—": "-", "“": '"', "”": '"', "‘": "'", "’": "'", "•": ".", "…": "...", " ": " ", "\t": "  "}


@dataclass
class Font:
    id: str
    label: str
    glyphs: dict[str, tuple[float, list[list[tuple[float, float]]]]]
    line_height: float
    gap: float


@lru_cache(maxsize=1)
def _simple() -> dict:
    return json.loads(FONT_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def _extra() -> dict:
    return json.loads(EXTRA_PATH.read_text(encoding="utf-8"))["fonts"]


def _pairs(flat: list[float]) -> list[tuple[float, float]]:
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat) - 1, 2)]


@lru_cache(maxsize=None)
def get_font(font_id: str = DEFAULT_FONT) -> Font:
    label, src, gap = FONTS.get(font_id, FONTS[DEFAULT_FONT])
    if src is None:
        d = _simple()
        return Font("simple", label, {c: (float(g["advance"]), [[tuple(p) for p in s] for s in g["strokes"]])
                                      for c, g in d["glyphs"].items()}, float(d["line_height"]), float(d["letter_spacing"]))
    d = _extra()[src]
    return Font(font_id, label, {c: (float(g[0]), [_pairs(s) for s in g[1]]) for c, g in d["glyphs"].items()},
                float(d.get("line_height", 22.0)), gap)


@lru_cache(maxsize=None)
def symbol_glyph(sym_set: str, code: int) -> tuple[float, list[list[tuple[float, float]]]] | None:
    """(advance, strokes) of one glyph of a symbol set ("mathupp", "symbolic", "greeks"), or None."""
    g = _extra().get(sym_set, {}).get("glyphs", {}).get(str(code))
    return None if g is None else (float(g[0]), [_pairs(s) for s in g[1]])


def _glyph(f: Font, ch: str) -> tuple[float, list[list[tuple[float, float]]]]:
    """(advance, strokes) in font units for one character, with the fallbacks described above."""
    if ch in f.glyphs:
        return f.glyphs[ch]
    ch = _ALIASES.get(ch, ch)
    if len(ch) > 1:                                       # e.g. "…" -> "..."
        return _concat(f, ch)
    if ch in f.glyphs:
        return f.glyphs[ch]
    if ch in _GREEK_LOW or ch in _GREEK_UP:
        g = symbol_glyph("greeks", ord(_GREEK_LOW.get(ch) or _GREEK_UP[ch]) - 32)
        if g:
            return g
    if ch in SYMBOLS:
        g = symbol_glyph(*SYMBOLS[ch])
        if g:
            return g
    if ch in _UMLAUT:
        adv, strokes = _glyph(f, _UMLAUT[ch])
        y = 16.8 if ch.isupper() else 12.2
        return adv, strokes + [[(adv * k, y), (adv * k, y + 0.6)] for k in (0.3, 0.7)]
    if ch == "ß":
        return _concat(f, "ss")
    return f.glyphs.get("?", (7.0, []))


def _concat(f: Font, s: str) -> tuple[float, list[list[tuple[float, float]]]]:
    pen, out = 0.0, []
    for c in s:
        adv, strokes = _glyph(f, c)
        out += [[(x + pen, y) for x, y in st] for st in strokes]
        pen += adv + f.gap
    return pen - f.gap, out


def glyph(char: str, font_id: str = DEFAULT_FONT) -> tuple[float, list[list[tuple[float, float]]]]:
    """Public glyph lookup: (advance, strokes) in font units (cap height 14)."""
    return _glyph(get_font(font_id), char)


def _lines(text: str) -> list[str]:
    return text.replace("\\n", "\n").split("\n")


MONO_ADVANCE = 11.0     # fixed pitch (font units) of the monospaced layout, for ASCII art


def _line_width_units(f: Font, line: str, mono: bool, tracking: float) -> float:
    if mono:
        return MONO_ADVANCE * len(line)
    return sum(_glyph(f, c)[0] for c in line) + (f.gap + tracking) * max(len(line) - 1, 0)


def measure(text: str, size_mm: float, line_spacing: float = 1.0, *, font: str = DEFAULT_FONT,
            mono: bool = False, tracking: float = 0.0) -> tuple[float, float]:
    """(width, height) in mm of the block; height = cap height + line pitch * (lines - 1)."""
    f = get_font(font) if isinstance(font, str) else font
    k = size_mm / 14.0
    lines = _lines(text)
    w = max((_line_width_units(f, ln, mono, tracking) for ln in lines), default=0.0) * k
    pitch = f.line_height * k * line_spacing
    return w, size_mm + pitch * (len(lines) - 1)


def text_strokes(text: str, size_mm: float, x: float, y: float, *, anchor: str = "left",
                 valign: str = "baseline", line_spacing: float = 1.0, color: int = 0,
                 font: str = DEFAULT_FONT, mono: bool = False, tracking: float = 0.0) -> list[Stroke]:
    """Strokes for ``text``. ``(x, y)`` is the anchor: ``anchor`` left/center/right is
    the horizontal reference of every line, ``valign`` baseline/center/top the vertical
    reference of the whole block (first line's baseline / block middle / top of caps).
    ``mono`` lays the text out on a fixed grid (ASCII art); ``tracking`` adds letter space (font units)."""
    f = get_font(font) if isinstance(font, str) else font
    k = size_mm / 14.0
    lines = _lines(text)
    pitch = f.line_height * k * line_spacing
    _, block_h = measure(text, size_mm, line_spacing, font=f, mono=mono, tracking=tracking)
    y0 = {"baseline": y, "center": y + block_h / 2 - size_mm, "top": y - size_mm}[valign]
    out: list[Stroke] = []
    for i, line in enumerate(lines):
        width = _line_width_units(f, line, mono, tracking) * k
        x0 = {"left": x, "center": x - width / 2, "right": x - width}[anchor]
        base = y0 - i * pitch
        pen = 0.0
        for ch in line:
            adv, strokes = _glyph(f, ch)
            off = (MONO_ADVANCE - adv) / 2 if mono else 0.0
            for s in strokes:
                if len(s) >= 2:
                    pts = np.array(s, float) * k + [x0 + (pen + off) * k, base]
                    out.append(Stroke(pts, color))
            pen += MONO_ADVANCE if mono else adv + f.gap + tracking
    return out


def font_list() -> list[dict]:
    return [{"id": i, "label": v[0]} for i, v in FONTS.items()]


__all__ = ["measure", "text_strokes", "glyph", "symbol_glyph", "font_list", "FONTS", "SYMBOLS", "FONT_PATH",
           "DEFAULT_FONT", "MONO_ADVANCE"]
