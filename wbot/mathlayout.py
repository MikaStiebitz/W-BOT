r"""LaTeX-style formulas -> single-stroke pen paths.

A small subset of LaTeX maths is understood: ``a^2 + b_i``, ``\frac{a}{b}``, ``\sqrt[n]{x}``, ``\sum_{i=1}^{n}``,
``\int_a^b``, ``\lim_{x \to 0}``, Greek letters, relations, ``\left( ... \right)``, ``\text{...}``, accents and
matrices (``\begin{pmatrix} a & b \\ c & d \end{pmatrix}``). Unicode symbols can be typed directly.

Layout works in font units (cap height 14, baseline y = 0); the caller scales to millimetres.
:func:`layout_math` returns the strokes and the box ``(width, ascent, descent)``.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import mathglyphs as mg
from .mathparse import (ACCENTS, AXIS, BIN, BOLD_FONT, CLOSERS, DIGIT_FONT, FUNCS, LETTER_FONT, LIMITS, MATRICES,
                        MED, MIN_SCALE, REL, SCRIPT_FONT, SPACES, THICK, THIN, MathError, _Parser)
from .textfont import glyph, symbol_glyph


@dataclass
class Box:
    strokes: list = field(default_factory=list)      # list of (n, 2) arrays, origin at the left end of the baseline
    w: float = 0.0
    asc: float = 0.0
    desc: float = 0.0
    cls: str = "ord"
    limits: bool = False                             # big operator whose scripts go above / below

    def moved(self, dx: float, dy: float) -> list:
        return [p + (dx, dy) for p in self.strokes]


def _arr(strokes, s: float = 1.0) -> list:
    return [np.asarray(p, float) * s for p in strokes if len(p) >= 2]


def _arr(strokes, s: float = 1.0) -> list:
    return [np.asarray(p, float) * s for p in strokes if len(p) >= 2]


# ------------------------------------------------------------------------------------------------ layout
def _glyph_box(ch: str, font_id: str, s: float, cls: str = "ord") -> Box:
    sp = mg.special(ch)
    if sp is not None:
        adv, strokes = sp
    else:
        adv, strokes = glyph(ch, font_id)
    arrs = _arr(strokes, s)
    ys = np.concatenate([a[:, 1] for a in arrs]) if arrs else np.array([0.0])
    return Box(arrs, adv * s, max(float(ys.max()), 0.0), max(-float(ys.min()), 0.0), cls)


def _char_cls(ch: str) -> str:
    if ch in REL:
        return "rel"
    if ch in BIN:
        return "bin"
    if ch in "([{⟨":
        return "open"
    if ch in ")]}⟩":
        return "close"
    if ch in ",;":
        return "punct"
    return "ord"


def _text_box(txt: str, font_id: str, s: float, cls: str = "ord") -> Box:
    pen, strokes, asc, desc = 0.0, [], 0.0, 0.0
    for ch in txt:
        if ch == " ":
            pen += 4.0 * s
            continue
        b = _glyph_box(ch, font_id, s)
        strokes += b.moved(pen, 0)
        pen += b.w + 0.2 * s
        asc, desc = max(asc, b.asc), max(desc, b.desc)
    return Box(strokes, max(pen - 0.2 * s, 0.0), asc, desc, cls)


class _Layout:
    def lay(self, node, s: float, font_id: str | None = None) -> Box:
        return getattr(self, "n_" + node[0])(node, s, font_id)

    # --- atoms
    def n_char(self, n, s, font_id):
        ch = n[1]
        if ch == "-":
            ch = "−"
        if font_id is None:
            fid = LETTER_FONT if ch.isalpha() and ch.isascii() else DIGIT_FONT
        else:
            fid = font_id
        return _glyph_box(ch, fid, s, _char_cls(ch))

    def n_sym(self, n, s, font_id):
        ch = n[1]
        if len(ch) > 1:
            return _text_box(ch, DIGIT_FONT, s)
        b = _glyph_box(ch, DIGIT_FONT, s, _char_cls(ch))
        if ch in "∑∏":
            b.cls = "op"
        return b

    def n_space(self, n, s, font_id):
        return Box([], n[1] * s)

    def n_text(self, n, s, font_id):
        return _text_box(n[1], n[2], s, "op" if n[3] else "ord")

    def n_func(self, n, s, font_id):
        b = _text_box(n[1], DIGIT_FONT, s, "op")
        b.limits = n[1] in LIMITS
        return b

    def n_style(self, n, s, font_id):
        return self.lay(n[2], s, n[1])

    def n_row(self, n, s, font_id):
        boxes = [self.lay(c, s, font_id) for c in n[1]]
        prev = None
        for b in boxes:                                   # unary + / - at the start or after an operator
            if b.cls == "bin" and (prev is None or prev in ("bin", "rel", "open", "punct", "op")):
                b.cls = "ord"
            prev = b.cls if b.strokes or b.w else prev
        return self.hbox(boxes, s)

    def hbox(self, boxes: list[Box], s: float) -> Box:
        out, x, asc, desc, prev = [], 0.0, 0.0, 0.0, None
        small = s < 0.8
        for i, b in enumerate(boxes):
            if prev is not None and (b.strokes or b.w):
                gap = 0.0
                if "rel" in (prev, b.cls):
                    gap = 0.0 if small else THICK
                elif "bin" in (prev, b.cls):
                    gap = 0.0 if small else MED
                elif prev == "punct":
                    gap = THIN
                elif prev == "op" and b.cls != "close" or (b.cls == "op" and prev in ("ord", "close")):
                    gap = THIN
                x += gap * s
            out += b.moved(x, 0)
            x += b.w
            asc, desc = max(asc, b.asc), max(desc, b.desc)
            if b.strokes or b.w:
                prev = b.cls
        return Box(out, x, asc, desc, "ord")

    # --- structures
    def n_frac(self, n, s, font_id):
        fs = max(s * 0.9 if s >= 0.99 else s * 0.8, MIN_SCALE)
        num, den = self.lay(n[1], fs, font_id), self.lay(n[2], fs, font_id)
        bar = len(n) < 4 or n[3]
        g = 2.4 * s
        a = AXIS * s
        w = max(num.w, den.w) + 3.0 * s
        nb, db = a + g + num.desc, a - g - den.asc
        strokes = num.moved((w - num.w) / 2, nb) + den.moved((w - den.w) / 2, db)
        if bar:
            strokes.append(np.array([[0.4 * s, a], [w - 0.4 * s, a]]))
        return Box(strokes, w, nb + num.asc, -(db - den.desc))

    def n_sqrt(self, n, s, font_id):
        body = self.lay(n[1], s, font_id)
        top, bottom = body.asc + 2.4 * s, -body.desc - 0.8 * s
        sign_w, rad = mg.radical(body.w, top, bottom, s)
        idx = self.lay(n[2], max(s * 0.55, 0.4), font_id) if n[2] is not None else None
        off = max(0.0, idx.w - 0.45 * sign_w) if idx else 0.0
        strokes = [p + (off, 0) for p in _arr(rad)] + body.moved(off + sign_w + 0.8 * s, 0)
        if idx:
            strokes += idx.moved(off + 0.45 * sign_w - idx.w, bottom + 0.55 * (top - bottom) + 1.0)
        return Box(strokes, off + sign_w + body.w + 2.3 * s, top, -bottom)

    def n_delim(self, n, s, font_id):
        body = self.lay(n[2], s, font_id)
        return self.wrap(n[1], body, n[3], s)

    def wrap(self, left: str, body: Box, right: str, s: float, *, stretch: bool | None = None) -> Box:
        a = AXIS * s
        half = max(body.asc - a, body.desc + a) + 1.2 * s
        tall = (body.asc + body.desc) > 16.0 * s if stretch is None else stretch
        parts, x, asc, desc = [], 0.0, 0.0, 0.0

        def piece(kind: str) -> Box:
            if not kind:
                return Box([], 1.0 * s)
            if not tall and kind in "()[]|":
                return _glyph_box(kind, DIGIT_FONT, s, "open" if kind in "([" else "close")
            w, st = mg.delimiter(kind, a + half, a - half)
            return Box(_arr(st), w, a + half, half - a)

        for b in (piece(left), body, piece(right) if right else Box()):
            parts += b.moved(x, 0); x += b.w
            asc, desc = max(asc, b.asc), max(desc, b.desc)
        return Box(parts, x, asc, desc)

    def n_acc(self, n, s, font_id):
        body = self.lay(n[2], s, font_id)
        y = max(body.asc, 9.5 * s) + 0.8 * s
        return Box(body.strokes + _arr(mg.accent(n[1], body.w, y)), body.w, y + 3.4, body.desc)

    def n_under(self, n, s, font_id):
        body = self.lay(n[1], s, font_id)
        y = -body.desc - 1.4 * s
        return Box(body.strokes + [np.array([[0.0, y], [body.w, y]])], body.w, body.asc, -y)

    def n_bigop(self, n, s, font_id):
        sym, limits, name = n[1], n[2], n[3]
        sc = (1.55 if limits else 1.0) * s if s >= 0.99 else s
        if sym == "∫":
            g = symbol_glyph("mathupp", 81)
            adv, strokes = g
            arrs = _arr(strokes, 0.62 * s)
            ys = np.concatenate([a[:, 1] for a in arrs])
            shift = AXIS * s - (ys.min() + ys.max()) / 2
            arrs = [p + (0, shift) for p in arrs]
            b = Box(arrs, adv * 0.62 * s, float(ys.max() + shift), -float(ys.min() + shift), "op")
            if name in ("iint", "iiint"):
                count = 2 if name == "iint" else 3
                single = b
                strokes2, x = [], 0.0
                for _ in range(count):
                    strokes2 += single.moved(x, 0); x += single.w * 0.55
                b = Box(strokes2, x + single.w * 0.45, single.asc, single.desc, "op")
            elif name == "oint":
                b.strokes = b.strokes + _arr([mg.circle(b.w / 2, AXIS * s, 3.0 * s)])
            return b
        g = symbol_glyph("mathupp", 27 if sym == "∑" else 26)
        adv, strokes = g
        arrs = _arr(strokes, sc)
        ys = np.concatenate([a[:, 1] for a in arrs])
        shift = AXIS * s - (ys.min() + ys.max()) / 2
        arrs = [p + (0, shift) for p in arrs]
        b = Box(arrs, adv * sc, float(ys.max() + shift), -float(ys.min() + shift), "op")
        b.limits = limits
        return b

    def n_script(self, n, s, font_id):
        base = self.lay(n[1], s, font_id)
        ss = max(s * 0.7, MIN_SCALE) if s > MIN_SCALE else s
        sup = self.lay(n[2], ss, font_id) if n[2] else None
        sub = self.lay(n[3], ss, font_id) if n[3] else None
        if base.limits and s >= 0.99:
            return self.limits(base, sup, sub, s)
        sup_base = max(7.2 * s, base.asc - 4.2 * s) if sup else 0.0
        sub_base = -max(3.4 * s, base.desc + 0.6 * s) if sub else 0.0
        if base.cls == "op" and base.asc > 16 * s:           # integral: scripts hug the top and the bottom
            sup_base = base.asc - 8.0 * s
            sub_base = -base.desc + 1.0 * s + (sub.asc if sub else 0)
        if sup and sub and (sup_base - sup.desc) - (sub_base + sub.asc) < 2.0 * s:
            sub_base = (sup_base - sup.desc) - 2.0 * s - sub.asc
        kern = 0.6 * s
        strokes, w = list(base.strokes), base.w
        extra = 0.0
        if sup:
            strokes += sup.moved(base.w + kern, sup_base); extra = max(extra, sup.w)
        if sub:
            strokes += sub.moved(base.w + kern * (0 if base.cls == "op" else 1), sub_base); extra = max(extra, sub.w)
        asc = max(base.asc, sup_base + sup.asc if sup else 0.0)
        desc = max(base.desc, -(sub_base - sub.desc) if sub else 0.0)
        return Box(strokes, w + kern + extra, asc, desc, base.cls)

    def limits(self, base: Box, sup, sub, s) -> Box:
        w = max(base.w, sup.w if sup else 0.0, sub.w if sub else 0.0)
        strokes = base.moved((w - base.w) / 2, 0)
        asc, desc = base.asc, base.desc
        if sup:
            y = base.asc + 1.8 * s + sup.desc
            strokes += sup.moved((w - sup.w) / 2, y)
            asc = y + sup.asc
        if sub:
            y = -base.desc - 1.8 * s - sub.asc
            strokes += sub.moved((w - sub.w) / 2, y)
            desc = -(y - sub.desc)
        return Box(strokes, w, asc, desc, "op")

    def n_mat(self, n, s, font_id):
        name, rows = n[1], n[2]
        ms = s if name not in ("matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix") else s * 0.95
        cells = [[self.lay(c, ms, font_id) for c in r] for r in rows]
        ncol = max((len(r) for r in cells), default=0)
        colw = [max((r[j].w for r in cells if j < len(r)), default=0.0) for j in range(ncol)]
        align = name in ("aligned", "align", "align*", "split", "gathered", "cases")
        gap = 0.0 if name in ("aligned", "align", "align*", "split") else 7.0 * s
        rowh = [(max((c.asc for c in r), default=0.0), max((c.desc for c in r), default=0.0)) for r in cells]
        total = sum(a + d for a, d in rowh) + 4.5 * s * (len(rows) - 1)
        y = total / 2 + AXIS * s
        strokes, w = [], 0.0
        ys = []
        for (asc, desc) in rowh:
            y -= asc; ys.append(y); y -= desc + 4.5 * s
        for r, base in zip(cells, ys):
            x = 0.0
            for j, c in enumerate(r):
                off = 0.0
                if name in ("aligned", "align", "align*", "split"):
                    off = colw[j] - c.w if j % 2 == 0 else 0.0
                elif name in ("cases", "gathered") or align:
                    off = 0.0
                else:
                    off = (colw[j] - c.w) / 2
                strokes += c.moved(x + off, base)
                x += colw[j] + gap
            w = max(w, x - gap)
        asc, desc = total / 2 + AXIS * s, total / 2 - AXIS * s
        box = Box(strokes, w, asc, desc)
        left, right = MATRICES[name]
        if left or right:
            return self.wrap(left, box, right, s, stretch=True)
        return box


def layout_math(src: str, *, max_len: int = 600) -> tuple[list[np.ndarray], float, float, float]:
    """Formula -> (strokes in font units, width, ascent, descent). Raises :class:`MathError`."""
    src = (src or "").strip()
    if not src:
        raise MathError("Type a formula.")
    if len(src) > max_len:
        raise MathError(f"The formula is too long (maximum {max_len} characters).")
    p = _Parser(src)
    lines, cur = [], []
    while True:
        r = p.row()
        cur.append(r)
        k, v = p.peek()
        if k is None:
            break
        if k == "nl":
            p.next(); lines.append(("row", [c for c in cur])); cur = []
            continue
        if k == "}":
            raise MathError("There is a } without a matching {.")
        if k == "&":
            p.next(); continue
        if k == "cmd" and v in ("right", "end"):
            raise MathError(f"\\{v} without a matching \\{'left' if v == 'right' else 'begin'}.")
    lines.append(("row", cur))
    lay = _Layout()
    boxes = [lay.lay(("row", [c for r in ln[1] for c in r[1]]), 1.0) for ln in lines]
    if len(boxes) == 1:
        b = boxes[0]
        return b.strokes, b.w, b.asc, b.desc
    strokes, y, w = [], 0.0, 0.0
    for i, b in enumerate(boxes):
        if i:
            y -= boxes[i - 1].desc + 5.0 + b.asc
        strokes += b.moved(0, y)
        w = max(w, b.w)
    return strokes, w, boxes[0].asc, -(y - boxes[-1].desc)


__all__ = ["layout_math", "MathError"]
