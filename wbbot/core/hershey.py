"""Single-stroke ("engraving") vector font for plotting text.

Why not a normal font
---------------------
A TrueType glyph is an *outline*: drawing it with a 2 mm marker gives you the
silhouette of the letter, and at small sizes the counters fill in and it turns
into a blob.  A single-stroke font stores the *skeleton* — the path a hand
would take — so the pen simply follows it.  This is the Hershey principle, and
it is the only thing that reads well on a whiteboard.

Coordinate system (font units)
------------------------------
* baseline ``y = 0``
* cap height ``y = 14``
* x-height ``y = 9``
* descender ``y = -5``
* glyphs advance left to right; each carries its own advance width

Text is scaled so that ``height_mm`` maps the cap height, which is the
dimension a human means by "2 cm tall letters".

Curves are generated from arc helpers rather than typed as point lists, so the
round letters actually look round and the data stays reviewable.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Literal, Sequence

import numpy as np

from .geometry import Polyline

__all__ = [
    "CAP_HEIGHT",
    "X_HEIGHT",
    "DESCENDER",
    "StrokeFont",
    "default_font",
    "text_to_polylines",
    "measure_text",
]

CAP_HEIGHT = 14.0
X_HEIGHT = 9.0
DESCENDER = -5.0
LINE_HEIGHT = 22.0          # baseline-to-baseline in font units

Stroke = list[tuple[float, float]]
Glyph = tuple[float, list[Stroke]]      # (advance, strokes)


# --------------------------------------------------------------------------
# construction helpers
# --------------------------------------------------------------------------

def _arc(cx: float, cy: float, rx: float, ry: float,
         a0: float, a1: float, segments: int | None = None) -> Stroke:
    """Elliptical arc from ``a0`` to ``a1`` degrees (counter-clockwise)."""
    sweep = abs(a1 - a0)
    if segments is None:
        segments = max(int(sweep / 15.0), 4)
    out: Stroke = []
    for i in range(segments + 1):
        t = math.radians(a0 + (a1 - a0) * i / segments)
        out.append((cx + rx * math.cos(t), cy + ry * math.sin(t)))
    return out


def _line(*points: tuple[float, float]) -> Stroke:
    return list(points)


# --------------------------------------------------------------------------
# glyph table
# --------------------------------------------------------------------------

def _build_glyphs() -> dict[str, Glyph]:
    g: dict[str, Glyph] = {}

    # -- space and basic punctuation ------------------------------------
    g[" "] = (7.0, [])
    g["."] = (5.0, [_line((2, 0), (2.6, 0), (2.6, 0.6), (2, 0.6), (2, 0))])
    g[","] = (5.0, [_line((2.6, 1), (2.6, 0), (1.6, -2.5))])
    g[":"] = (5.0, [_line((2, 9), (2.6, 9), (2.6, 8.4), (2, 8.4), (2, 9)),
                    _line((2, 0.6), (2.6, 0.6), (2.6, 0), (2, 0), (2, 0.6))])
    g[";"] = (5.0, [_line((2, 9), (2.6, 9), (2.6, 8.4), (2, 8.4), (2, 9)),
                    _line((2.6, 1), (2.6, 0), (1.6, -2.5))])
    g["!"] = (5.0, [_line((2.3, 14), (2.3, 4)),
                    _line((2, 0.6), (2.6, 0.6), (2.6, 0), (2, 0), (2, 0.6))])
    g["?"] = (10.0, [_arc(5, 11, 4, 3, 180, -20) + [(5, 5), (5, 4)],
                     _line((4.7, 0.6), (5.3, 0.6), (5.3, 0), (4.7, 0), (4.7, 0.6))])
    g["'"] = (4.0, [_line((2, 14), (2, 10))])
    g['"'] = (7.0, [_line((2, 14), (2, 10)), _line((5, 14), (5, 10))])
    g["`"] = (5.0, [_line((1.5, 14), (3.5, 11))])
    g["-"] = (9.0, [_line((1.5, 7), (7.5, 7))])
    g["_"] = (10.0, [_line((0, -3), (10, -3))])
    g["+"] = (12.0, [_line((6, 2), (6, 12)), _line((1, 7), (11, 7))])
    g["="] = (12.0, [_line((1, 9), (11, 9)), _line((1, 5), (11, 5))])
    g["*"] = (10.0, [_line((5, 13), (5, 5)), _line((1.5, 11), (8.5, 7)),
                     _line((8.5, 11), (1.5, 7))])
    g["/"] = (9.0, [_line((0.5, -2), (8.5, 15))])
    g["\\"] = (9.0, [_line((0.5, 15), (8.5, -2))])
    g["|"] = (5.0, [_line((2.5, -3), (2.5, 15))])
    # Brackets are sized from three fixed points — (5, 15), (1.5, 6), (5, -3) —
    # so they actually enclose a full-height expression instead of curling up
    # into comma-sized nubs.
    g["("] = (6.0, [_arc(8.5, 6, 7.0, 10.39, 120, 240)])
    g[")"] = (6.0, [_arc(-2.5, 6, 7.0, 10.39, 60, -60)])
    g["["] = (6.0, [_line((4.5, -3), (1.5, -3), (1.5, 15), (4.5, 15))])
    g["]"] = (6.0, [_line((1.5, -3), (4.5, -3), (4.5, 15), (1.5, 15))])
    g["{"] = (7.0, [_line((5, 15), (3.5, 15), (3.5, 7.8), (2, 6),
                          (3.5, 4.2), (3.5, -3), (5, -3))])
    g["}"] = (7.0, [_line((2, 15), (3.5, 15), (3.5, 7.8), (5, 6),
                          (3.5, 4.2), (3.5, -3), (2, -3))])
    g["<"] = (11.0, [_line((9, 12), (2, 7), (9, 2))])
    g[">"] = (11.0, [_line((2, 12), (9, 7), (2, 2))])
    g["#"] = (12.0, [_line((3.5, 14), (2.5, 0)), _line((8.5, 14), (7.5, 0)),
                     _line((1, 10), (10.5, 10)), _line((0.7, 4.5), (10.2, 4.5))])
    g["%"] = (14.0, [_line((1, 0), (13, 14)),
                     _arc(3.5, 11, 2.5, 2.5, 0, 360),
                     _arc(10.5, 3, 2.5, 2.5, 0, 360)])
    g["&"] = (14.0, [[(13.5, 0), (5.5, 9.5)] +
                     _arc(5.5, 11.5, 2.6, 2.5, -90, 90) +
                     [(4.0, 12.6), (2.0, 10.0), (2.6, 7.0), (9.0, 2.0)] +
                     _arc(6.0, 3.6, 3.6, 3.6, -24, -150) +
                     [(1.5, 6.0), (3.4, 9.0), (10.0, 9.0)]])
    g["@"] = (16.0, [_arc(9.5, 7, 3, 3, 0, 360) +
                     [(12.5, 4), (13.5, 5.5)],
                     _arc(8, 7, 7.5, 7, -30, 260)])
    g["$"] = (12.0, [_line((6, 15), (6, -1)),
                     _arc(6, 10.5, 4, 3, 20, 180) +
                     [(2, 9), (2.5, 7.6), (6, 7),
                      (9.5, 6.4), (10, 4.5)] +
                     _arc(6, 4, 4, 3, 0, -180)])
    g["^"] = (10.0, [_line((1.5, 10), (5, 14), (8.5, 10))])
    g["~"] = (12.0, [_line((1, 7), (3.5, 9), (6, 7), (8.5, 5), (11, 7))])

    # -- digits ---------------------------------------------------------
    g["0"] = (12.0, [_arc(6, 7, 4.5, 7, 0, 360), _line((3.5, 3), (8.5, 11))])
    g["1"] = (12.0, [_line((2.5, 11), (6, 14), (6, 0)), _line((3, 0), (9, 0))])
    g["2"] = (12.0, [_arc(6, 10.5, 4.2, 3.5, 170, -35) +
                     [(1.5, 0), (10.5, 0)]])
    g["3"] = (12.0, [_arc(6, 10.7, 4, 3.3, 165, -60),
                     _line((5, 7.5), (6.3, 7.5)),
                     _arc(6, 3.7, 4.4, 3.7, 60, -170)])
    g["4"] = (12.0, [_line((8, 0), (8, 14), (1, 4), (11, 4))])
    g["5"] = (12.0, [_line((10, 14), (3, 14), (2.2, 8)),
                     _arc(6.2, 4.6, 4.4, 4.6, 75, -170)])
    g["6"] = (12.0, [_arc(6, 4.5, 4.5, 4.5, -60, 250),
                     _line((2.2, 6.5), (3.6, 11), (7, 14))])
    g["7"] = (12.0, [_line((1.5, 14), (10.5, 14), (4.5, 0))])
    g["8"] = (12.0, [_arc(6, 10.7, 3.9, 3.3, 0, 360),
                     _arc(6, 4, 4.6, 4, 0, 360)])
    g["9"] = (12.0, [_arc(6, 9.5, 4.5, 4.5, 120, -180),
                     _line((9.8, 7.5), (8.4, 3), (5, 0))])

    # -- uppercase ------------------------------------------------------
    g["A"] = (13.0, [_line((0.5, 0), (6.5, 14), (12.5, 0)), _line((2.7, 5), (10.3, 5))])
    g["B"] = (13.0, [_line((1.5, 0), (1.5, 14), (8, 14)),
                     _arc(8, 10.9, 3.5, 3.1, 90, -90),
                     _line((1.5, 7.8), (8.5, 7.8)),
                     _arc(8.5, 3.9, 4, 3.9, 90, -90),
                     _line((8.5, 0), (1.5, 0))])
    g["C"] = (13.0, [_arc(7, 7, 5.5, 7, 55, 305)])
    g["D"] = (13.0, [_line((1.5, 0), (1.5, 14), (6, 14)),
                     _arc(6, 7, 5.5, 7, 90, -90),
                     _line((6, 0), (1.5, 0))])
    g["E"] = (12.0, [_line((10.5, 14), (1.5, 14), (1.5, 0), (10.5, 0)),
                     _line((1.5, 7.5), (8.5, 7.5))])
    g["F"] = (12.0, [_line((10.5, 14), (1.5, 14), (1.5, 0)),
                     _line((1.5, 7.5), (8.5, 7.5))])
    g["G"] = (14.0, [_arc(7, 7, 5.5, 7, 55, 300) + [(12.5, 0.5), (12.5, 5.5), (8, 5.5)]])
    g["H"] = (13.0, [_line((1.5, 0), (1.5, 14)), _line((11.5, 0), (11.5, 14)),
                     _line((1.5, 7.5), (11.5, 7.5))])
    g["I"] = (7.0, [_line((3.5, 0), (3.5, 14)), _line((1, 14), (6, 14)),
                    _line((1, 0), (6, 0))])
    g["J"] = (11.0, [_line((8, 14), (8, 4)), _arc(4.5, 4, 3.5, 4, 0, -180)])
    g["K"] = (13.0, [_line((1.5, 0), (1.5, 14)), _line((11.5, 14), (1.5, 6)),
                     _line((5, 8.8), (12, 0))])
    g["L"] = (11.0, [_line((1.5, 14), (1.5, 0), (10, 0))])
    g["M"] = (15.0, [_line((1.5, 0), (1.5, 14), (7.5, 3), (13.5, 14), (13.5, 0))])
    g["N"] = (13.0, [_line((1.5, 0), (1.5, 14), (11.5, 0), (11.5, 14))])
    g["O"] = (14.0, [_arc(7, 7, 5.5, 7, 0, 360)])
    g["P"] = (12.0, [_line((1.5, 0), (1.5, 14), (7, 14)),
                     _arc(7, 10.7, 3.8, 3.3, 90, -90),
                     _line((7, 7.4), (1.5, 7.4))])
    g["Q"] = (14.0, [_arc(7, 7, 5.5, 7, 0, 360), _line((8.5, 3.5), (13, -2))])
    g["R"] = (13.0, [_line((1.5, 0), (1.5, 14), (7, 14)),
                     _arc(7, 10.7, 3.8, 3.3, 90, -90),
                     _line((7, 7.4), (1.5, 7.4)),
                     _line((6, 7.4), (12, 0))])
    g["S"] = (12.0, [_arc(6.2, 10.6, 4.2, 3.4, 20, 180) +
                     [(2, 9.2), (2.6, 8), (6, 7.2),
                      (9.6, 6.4), (10.3, 5)] +
                     _arc(6.2, 3.7, 4.3, 3.7, 0, -190)])
    g["T"] = (12.0, [_line((0.5, 14), (11.5, 14)), _line((6, 14), (6, 0))])
    g["U"] = (13.0, [_line((1.5, 14), (1.5, 4)), _arc(6.5, 4, 5, 4, 180, 360),
                     _line((11.5, 4), (11.5, 14))])
    g["V"] = (13.0, [_line((0.5, 14), (6.5, 0), (12.5, 14))])
    g["W"] = (17.0, [_line((0.5, 14), (4.5, 0), (8.5, 10), (12.5, 0), (16.5, 14))])
    g["X"] = (13.0, [_line((1, 14), (12, 0)), _line((12, 14), (1, 0))])
    g["Y"] = (13.0, [_line((0.5, 14), (6.5, 7), (12.5, 14)), _line((6.5, 7), (6.5, 0))])
    g["Z"] = (12.0, [_line((1, 14), (11, 14), (1, 0), (11, 0))])

    # -- lowercase ------------------------------------------------------
    g["a"] = (11.0, [_arc(5.5, 4.5, 3.8, 4.5, 60, 350),
                     _line((9.3, 9), (9.3, 0)),
                     _line((9.3, 1.5), (10.5, 0))])
    g["b"] = (11.0, [_line((1.5, 14), (1.5, 0)),
                     _arc(5.5, 4.5, 4, 4.5, 250, 470)])
    g["c"] = (10.0, [_arc(5.5, 4.5, 4, 4.5, 55, 305)])
    g["d"] = (11.0, [_line((9.5, 14), (9.5, 0)),
                     _arc(5.5, 4.5, 4, 4.5, -70, 250)])
    g["e"] = (10.0, [_line((1.5, 4.7), (9.5, 4.7)),
                     _arc(5.5, 4.5, 4, 4.5, 0, 290)])
    g["f"] = (7.0, [_line((6.5, 14), (4, 14), (3, 12), (3, 0)),
                    _line((0.5, 9), (6, 9))])
    g["g"] = (11.0, [_arc(5.5, 4.5, 4, 4.5, -70, 250),
                     _line((9.5, 9), (9.5, -1)),
                     _arc(5.5, -1, 4, 4, 0, -180)])
    g["h"] = (11.0, [_line((1.5, 14), (1.5, 0)),
                     _arc(5.5, 5.2, 4, 3.8, 180, 0),
                     _line((9.5, 5.2), (9.5, 0))])
    g["i"] = (5.0, [_line((2.5, 9), (2.5, 0)),
                    _line((2.2, 12.6), (2.8, 12.6), (2.8, 12), (2.2, 12), (2.2, 12.6))])
    g["j"] = (5.0, [_line((2.5, 9), (2.5, -1)), _arc(-0.5, -1, 3, 3, 0, -90),
                    _line((2.2, 12.6), (2.8, 12.6), (2.8, 12), (2.2, 12), (2.2, 12.6))])
    g["k"] = (10.0, [_line((1.5, 14), (1.5, 0)), _line((9, 9), (1.5, 3.5)),
                     _line((4, 5.3), (9.5, 0))])
    g["l"] = (5.0, [_line((1, 14), (2.5, 14), (2.5, 1.4), (4, 0))])
    g["m"] = (16.0, [_line((1.5, 9), (1.5, 0)),
                     _arc(5, 5.4, 3.5, 3.6, 180, 0), _line((8.5, 5.4), (8.5, 0)),
                     _arc(12, 5.4, 3.5, 3.6, 180, 0), _line((15, 5.4), (15, 0))])
    g["n"] = (11.0, [_line((1.5, 9), (1.5, 0)),
                     _arc(5.5, 5.2, 4, 3.8, 180, 0), _line((9.5, 5.2), (9.5, 0))])
    g["o"] = (11.0, [_arc(5.5, 4.5, 4, 4.5, 0, 360)])
    g["p"] = (11.0, [_line((1.5, 9), (1.5, -5)),
                     _arc(5.5, 4.5, 4, 4.5, 250, 470)])
    g["q"] = (11.0, [_line((9.5, 9), (9.5, -5)),
                     _arc(5.5, 4.5, 4, 4.5, -70, 250)])
    g["r"] = (8.0, [_line((1.5, 9), (1.5, 0)),
                    _arc(5, 5.4, 3.5, 3.6, 180, 60)])
    g["s"] = (10.0, [_arc(5, 6.9, 3.4, 2.2, 20, 180) +
                     [(1.6, 6), (2.2, 5.2), (5, 4.6), (7.8, 4.0), (8.4, 3.0)] +
                     _arc(5, 2.4, 3.4, 2.4, 0, -190)])
    g["t"] = (7.0, [_line((3, 14), (3, 1.6), (5.5, 0)), _line((0.5, 9), (6, 9))])
    g["u"] = (11.0, [_line((1.5, 9), (1.5, 3.8)),
                     _arc(5.5, 3.8, 4, 3.8, 180, 360),
                     _line((9.5, 9), (9.5, 0))])
    g["v"] = (10.0, [_line((0.5, 9), (5, 0), (9.5, 9))])
    g["w"] = (14.0, [_line((0.5, 9), (3.5, 0), (7, 6.5), (10.5, 0), (13.5, 9))])
    g["x"] = (10.0, [_line((1, 9), (9, 0)), _line((9, 9), (1, 0))])
    g["y"] = (10.0, [_line((0.5, 9), (5, 0)), _line((9.5, 9), (3, -5))])
    g["z"] = (10.0, [_line((1, 9), (9, 9), (1, 0), (9, 0))])

    # -- a few symbols the maths tools need -----------------------------
    g["×"] = (12.0, [_line((2.5, 10.5), (9.5, 3.5)),      # multiplication
                          _line((9.5, 10.5), (2.5, 3.5))])
    g["÷"] = (12.0, [_line((1.5, 7), (10.5, 7)),          # division
                          _line((5.7, 10), (6.3, 10), (6.3, 9.4), (5.7, 9.4), (5.7, 10)),
                          _line((5.7, 4.6), (6.3, 4.6), (6.3, 4), (5.7, 4), (5.7, 4.6))])
    g["±"] = (12.0, [_line((6, 3.5), (6, 12)), _line((1, 8), (11, 8)),
                          _line((1, 1), (11, 1))])
    g["≤"] = (12.0, [_line((10, 12), (2, 7.5), (10, 3)), _line((2, 0.5), (10, 0.5))])
    g["≥"] = (12.0, [_line((2, 12), (10, 7.5), (2, 3)), _line((2, 0.5), (10, 0.5))])
    g["≠"] = (12.0, [_line((1, 9), (11, 9)), _line((1, 5), (11, 5)),
                          _line((3.5, 2), (8.5, 12))])
    g["≈"] = (12.0, [_line((1, 9), (3.5, 10.6), (6, 9), (8.5, 7.4), (11, 9)),
                          _line((1, 5), (3.5, 6.6), (6, 5), (8.5, 3.4), (11, 5))])
    g["π"] = (12.0, [_line((1, 9), (11, 9)), _line((4, 9), (3.4, 0)),
                          _line((8.5, 9), (8.5, 1.5), (10.5, 0.6))])
    g["√"] = (14.0, [_line((0.5, 7), (3, 7), (6, -1), (9, 15), (14, 15))])
    g["∑"] = (13.0, [_line((11.5, 14), (1.5, 14), (7, 7), (1.5, 0), (11.5, 0))])
    g["∫"] = (9.0, [_arc(6, 12.5, 2.5, 2.5, 90, 270)[:6] +
                         [(4.5, 10), (4.5, 4)] +
                         _arc(3, 1.5, 2.5, 2.5, -90, 90)[:6]])
    g["∞"] = (16.0, [_arc(4.5, 6, 4, 4, 0, 360) + _arc(11.5, 6, 4, 4, 180, -180)])
    g["°"] = (7.0, [_arc(3.5, 11.5, 2.2, 2.2, 0, 360)])
    g["µ"] = (11.0, [_line((1.5, 9), (1.5, -5)),
                          _line((1.5, 3.8), (1.5, 3.8)),
                          _arc(5.5, 3.8, 4, 3.8, 180, 360),
                          _line((9.5, 9), (9.5, 0))])
    g["→"] = (14.0, [_line((1, 7), (13, 7)), _line((9, 10.5), (13, 7), (9, 3.5))])
    g["←"] = (14.0, [_line((1, 7), (13, 7)), _line((5, 10.5), (1, 7), (5, 3.5))])
    g["Δ"] = (13.0, [_line((0.5, 0), (6.5, 14), (12.5, 0), (0.5, 0))])
    g["α"] = (12.0, [_arc(5.2, 4.5, 4.0, 4.5, 35, 325) + [(10.5, 0)],
                          _line((8.2, 8.4), (10.5, 0))])
    g["β"] = (11.0, [_line((1.5, -5), (1.5, 10)),
                          _arc(5, 10.5, 3.5, 3.5, 180, 0),
                          _arc(5.5, 5, 3, 3, 90, -110),
                          _arc(5, 2, 3.5, 3.5, 90, -180)])
    g["θ"] = (11.0, [_arc(5.5, 7, 4, 7, 0, 360), _line((1.5, 7), (9.5, 7))])
    g["λ"] = (12.0, [_line((0.5, 0), (7, 12)), _line((3.5, 14), (5, 14), (11, 0))])
    g["ω"] = (14.0, [_arc(4, 4, 3.5, 4.5, 180, 380)[:14] + [(7, 8)] +
                          _arc(10, 4, 3.5, 4.5, 160, 360)])
    return g


def _clean(glyphs: dict[str, Glyph]) -> dict[str, Glyph]:
    """Drop degenerate points so no glyph carries a zero-length segment."""
    out: dict[str, Glyph] = {}
    for ch, (advance, strokes) in glyphs.items():
        cleaned: list[Stroke] = []
        for stroke in strokes:
            pts = [(float(x), float(y)) for x, y in stroke]
            dedup: Stroke = []
            for pt in pts:
                if not dedup or math.dist(pt, dedup[-1]) > 1e-9:
                    dedup.append(pt)
            if len(dedup) >= 2:
                cleaned.append(dedup)
        out[ch] = (float(advance), cleaned)
    return out


GLYPHS: dict[str, Glyph] = _clean(_build_glyphs())

#: Characters that have no glyph fall back to this.
FALLBACK = "?"


# --------------------------------------------------------------------------
# font object
# --------------------------------------------------------------------------

@dataclass
class StrokeFont:
    """Renders text into plotter polylines."""

    glyphs: dict[str, Glyph]
    #: Extra advance between glyphs, in font units.
    letter_spacing: float = 1.5
    line_height: float = LINE_HEIGHT

    # -- metrics --------------------------------------------------------

    def glyph(self, char: str) -> Glyph:
        if char in self.glyphs:
            return self.glyphs[char]
        # Common substitutions before giving up.
        for alt in (char.upper(), char.lower()):
            if alt in self.glyphs:
                return self.glyphs[alt]
        return self.glyphs.get(FALLBACK, (7.0, []))

    def supports(self, char: str) -> bool:
        return char in self.glyphs

    def unsupported(self, text: str) -> list[str]:
        """Characters that would render as the fallback glyph."""
        out = []
        for ch in text:
            if ch in ("\n", "\t", "\r"):
                continue
            if ch not in self.glyphs and ch.upper() not in self.glyphs \
                    and ch.lower() not in self.glyphs:
                if ch not in out:
                    out.append(ch)
        return out

    def line_width_units(self, line: str) -> float:
        total = 0.0
        for i, ch in enumerate(line):
            advance, _ = self.glyph(ch)
            total += advance
            if i < len(line) - 1:
                total += self.letter_spacing
        return total

    def measure(self, text: str, height_mm: float) -> tuple[float, float]:
        """``(width_mm, height_mm)`` of the rendered block."""
        if height_mm <= 0:
            raise ValueError("height_mm must be > 0")
        scale = height_mm / CAP_HEIGHT
        lines = text.split("\n")
        width = max((self.line_width_units(l) for l in lines), default=0.0)
        height = CAP_HEIGHT + (len(lines) - 1) * self.line_height
        return (width * scale, height * scale)

    # -- rendering ------------------------------------------------------

    def render(self, text: str, x: float, y: float, height_mm: float, *,
               align: Literal["left", "center", "right"] = "left",
               baseline: Literal["baseline", "bottom", "middle", "top"] = "baseline",
               angle_deg: float = 0.0,
               ) -> list[Polyline]:
        """Render ``text`` with its anchor at ``(x, y)`` in board millimetres.

        ``height_mm`` is the **cap height** — the height of an upper-case
        letter, which is what a person means by "2 cm tall".
        """
        if height_mm <= 0:
            raise ValueError("height_mm must be > 0")
        scale = height_mm / CAP_HEIGHT
        lines = text.split("\n")
        widths = [self.line_width_units(l) for l in lines]
        block_w = max(widths, default=0.0)
        block_h = CAP_HEIGHT + (len(lines) - 1) * self.line_height

        if baseline == "baseline":
            y_offset = 0.0
        elif baseline == "bottom":
            y_offset = -DESCENDER
        elif baseline == "middle":
            y_offset = block_h / 2.0 - CAP_HEIGHT / 2.0
        else:                                    # top
            y_offset = -CAP_HEIGHT
        # First baseline sits at the top of the block.
        y_offset += (len(lines) - 1) * self.line_height

        cos_a = math.cos(math.radians(angle_deg))
        sin_a = math.sin(math.radians(angle_deg))

        # The alignment does two things: it positions the whole block relative
        # to the anchor, and it aligns the lines within the block. Doing only
        # the second (the easy mistake) makes `align` a no-op for single-line
        # text, because one line is always exactly as wide as its block.
        if align == "center":
            block_offset = -block_w / 2.0
        elif align == "right":
            block_offset = -block_w
        else:
            block_offset = 0.0

        out: list[Polyline] = []
        for row, line in enumerate(lines):
            if align == "center":
                pen_x = block_offset + (block_w - widths[row]) / 2.0
            elif align == "right":
                pen_x = block_offset + (block_w - widths[row])
            else:
                pen_x = block_offset
            pen_y = y_offset - row * self.line_height

            for i, ch in enumerate(line):
                advance, strokes = self.glyph(ch)
                for stroke in strokes:
                    pts = np.asarray(stroke, dtype=float)
                    pts = pts + np.array([pen_x, pen_y])
                    pts = pts * scale
                    if angle_deg:
                        rot = np.array([[cos_a, -sin_a], [sin_a, cos_a]])
                        pts = pts @ rot.T
                    pts = pts + np.array([x, y])
                    out.append(Polyline(pts, closed=False))
                pen_x += advance
                if i < len(line) - 1:
                    pen_x += self.letter_spacing
        return out

    # -- persistence ----------------------------------------------------

    def to_json(self) -> str:
        return json.dumps(
            {"cap_height": CAP_HEIGHT, "x_height": X_HEIGHT,
             "descender": DESCENDER, "line_height": self.line_height,
             "letter_spacing": self.letter_spacing,
             "glyphs": {ch: {"advance": adv,
                             "strokes": [[[round(x, 3), round(y, 3)]
                                          for x, y in s] for s in strokes]}
                        for ch, (adv, strokes) in sorted(self.glyphs.items())}},
            ensure_ascii=False, indent=1)

    @classmethod
    def from_json(cls, text: str) -> "StrokeFont":
        data = json.loads(text)
        glyphs = {
            ch: (float(entry["advance"]),
                 [[(float(p[0]), float(p[1])) for p in s]
                  for s in entry["strokes"]])
            for ch, entry in data["glyphs"].items()
        }
        return cls(glyphs=glyphs,
                   letter_spacing=float(data.get("letter_spacing", 1.5)),
                   line_height=float(data.get("line_height", LINE_HEIGHT)))


@lru_cache(maxsize=1)
def default_font() -> StrokeFont:
    """The bundled single-stroke font.

    Loads ``wbbot/mcp/fonts/hershey_simplex.json`` when present so the font can
    be swapped without touching code, and falls back to the built-in table.
    """
    path = Path(__file__).resolve().parent.parent / "mcp" / "fonts" / \
        "hershey_simplex.json"
    if path.exists():
        try:
            return StrokeFont.from_json(path.read_text(encoding="utf-8"))
        except Exception:      # pragma: no cover - corrupt file, use built-in
            pass
    return StrokeFont(glyphs=GLYPHS)


def text_to_polylines(text: str, x: float, y: float, height_mm: float,
                      **kwargs) -> list[Polyline]:
    return default_font().render(text, x, y, height_mm, **kwargs)


def measure_text(text: str, height_mm: float) -> tuple[float, float]:
    return default_font().measure(text, height_mm)
