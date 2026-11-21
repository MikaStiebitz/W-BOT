"""Tokenizer and parser for the LaTeX subset that :mod:`wbot.mathlayout` lays out (fractions, scripts, matrices, ...)."""
from __future__ import annotations


AXIS = 5.2                       # height of the fraction bar / minus sign above the baseline (font units)
THIN, MED, THICK = 2.6, 4.0, 5.5
MIN_SCALE = 0.5
LETTER_FONT, DIGIT_FONT, BOLD_FONT, SCRIPT_FONT = "italic", "serif", "serif-bold", "script"

GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ", "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ",
    "omicron": "ο", "pi": "π", "varpi": "π", "rho": "ρ", "varrho": "ρ", "sigma": "σ", "varsigma": "ς", "tau": "τ",
    "upsilon": "υ", "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ",
    "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ", "Phi": "Φ", "Psi": "Ψ",
    "Omega": "Ω"}
SYM = {
    "pm": "±", "mp": "∓", "times": "×", "cdot": "·", "div": "÷", "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥",
    "neq": "≠", "ne": "≠", "approx": "≈", "equiv": "≡", "propto": "∝", "infty": "∞", "partial": "∂", "nabla": "∇",
    "in": "∈", "notin": "∉", "subset": "⊂", "supset": "⊃", "subseteq": "⊆", "supseteq": "⊇", "cup": "∪", "cap": "∩",
    "to": "→", "rightarrow": "→", "leftarrow": "←", "gets": "←", "leftrightarrow": "↔", "Rightarrow": "⇒",
    "Leftarrow": "⇐", "Leftrightarrow": "⇔", "implies": "⟹", "iff": "⟺", "uparrow": "↑", "downarrow": "↓",
    "forall": "∀", "exists": "∃", "emptyset": "∅", "varnothing": "∅", "degree": "°", "circ": "°", "perp": "⊥",
    "parallel": "∥", "angle": "∠", "therefore": "∴", "sim": "∼", "prime": "′", "aleph": "ℵ", "ldots": "...",
    "dots": "...", "cdots": "⋯", "ast": "*", "star": "*", "bullet": "·", "qed": "∎", "square": "□",
    "lbrace": "{", "rbrace": "}", "langle": "⟨", "rangle": "⟩", "vert": "|", "mid": "|", "Vert": "‖", "lt": "<", "gt": ">"}
REL = set("=<>≤≥≠≈≡∝∈∉⊂⊃⊆⊇→←↔⇒⇐⇔⟹⟸⟺∼⊥∥⟶")
BIN = set("+−×÷·±∓∪∩*")
FUNCS = {"sin", "cos", "tan", "cot", "sec", "csc", "arcsin", "arccos", "arctan", "sinh", "cosh", "tanh", "log", "ln",
         "lg", "exp", "det", "gcd", "deg", "dim", "ker", "max", "min", "sup", "inf", "lim", "limsup", "liminf", "arg",
         "Pr", "mod"}
LIMITS = {"lim", "limsup", "liminf", "max", "min", "sup", "inf"}
SPACES = {",": 2.6, ";": 4.0, ":": 3.5, " ": 4.0, "!": -2.0, "quad": 14.0, "qquad": 28.0}
ACCENTS = {"hat": "hat", "widehat": "hat", "bar": "bar", "overline": "bar", "vec": "vec", "dot": "dot",
           "ddot": "ddot", "tilde": "tilde", "widetilde": "tilde"}
STYLES = {"mathrm": DIGIT_FONT, "text": DIGIT_FONT, "mathbf": BOLD_FONT, "boldsymbol": BOLD_FONT, "mathbb": BOLD_FONT,
          "mathit": LETTER_FONT, "mathcal": SCRIPT_FONT, "mathscr": SCRIPT_FONT, "textbf": BOLD_FONT,
          "operatorname": DIGIT_FONT}
MATRICES = {"matrix": ("", ""), "pmatrix": ("(", ")"), "bmatrix": ("[", "]"), "Bmatrix": ("{", "}"),
            "vmatrix": ("|", "|"), "Vmatrix": ("‖", "‖"), "cases": ("{", ""), "aligned": ("", ""),
            "align": ("", ""), "align*": ("", ""), "array": ("", ""), "split": ("", ""), "gathered": ("", "")}
CLOSERS = {"(": ")", "[": "]", "{": "}", "⟨": "⟩", "|": "|", "‖": "‖", "⌊": "⌋", "⌈": "⌉"}


class MathError(ValueError):
    """The formula cannot be read (the message is shown to the user)."""


# ------------------------------------------------------------------------------------------------ parsing
def _tokens(src: str) -> list[tuple[str, str]]:
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            j = i + 1
            if j < n and src[j].isalpha():
                while j < n and src[j].isalpha():
                    j += 1
                out.append(("cmd", src[i + 1:j]))
            elif j < n and src[j] == "\\":
                out.append(("nl", "\\\\")); j += 1
            else:
                out.append(("cmd", src[j:j + 1])); j += 1
            i = j
        elif c in "{}^_&":
            out.append((c, c)); i += 1
        elif c.isspace():
            out.append(("sp", " ")); i += 1
        elif c == "$":
            i += 1
        else:
            out.append(("char", c)); i += 1
    return out


_SUP = dict(zip("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻ⁿ", "0123456789+-n"))
_SUB = dict(zip("₀₁₂₃₄₅₆₇₈₉₊₋", "0123456789+-"))


class _Parser:
    def __init__(self, src: str):
        self.t = _tokens(src)
        self.i = 0
        self.raw = False                 # inside \text{...} spaces are kept

    def peek(self):
        while not self.raw and self.i < len(self.t) and self.t[self.i][0] == "sp":
            self.i += 1
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def next(self):
        tok = self.peek()
        self.i += 1
        return tok

    def row(self):
        items = []
        while True:
            k, v = self.peek()
            if k is None or k in ("}", "&", "nl") or (k == "cmd" and v in ("right", "end")):
                break
            items.append(self.item())
        return ("row", items)

    def group(self):
        k, _ = self.next()
        if k != "{":
            raise MathError("A { is missing.")
        r = self.row()
        if self.next()[0] != "}":
            raise MathError("A { is not closed.")
        return r

    def arg(self):
        k, v = self.peek()
        if k == "{":
            return self.group()
        if k is None or k == "}":
            raise MathError("An argument is missing.")
        if k in ("^", "_", "&", "nl"):
            raise MathError(f"Unexpected {v}.")
        return ("row", [self.atom()])

    def item(self):
        base = self.atom()
        sup = sub = None
        while True:
            k, v = self.peek()
            if k == "^":
                self.next(); a = self.arg(); sup = a if sup is None else ("row", [sup, a])
            elif k == "_":
                self.next(); a = self.arg(); sub = a if sub is None else ("row", [sub, a])
            elif k == "char" and v == "'":
                self.next(); a = ("row", [("char", "′")]); sup = a if sup is None else ("row", [sup, a])
            elif k == "char" and v in _SUP:
                self.next(); a = ("row", [("char", _SUP[v])]); sup = a if sup is None else ("row", [sup, a])
            elif k == "char" and v in _SUB:
                self.next(); a = ("row", [("char", _SUB[v])]); sub = a if sub is None else ("row", [sub, a])
            else:
                break
        return ("script", base, sup, sub) if sup or sub else base

    def delim_token(self):
        k, v = self.next()
        if k == "char" and v == ".":
            return ""
        if k == "char" and v in "()[]|<>/":
            return {"<": "⟨", ">": "⟩"}.get(v, v)
        if k == "cmd":
            m = {"{": "{", "}": "}", "lbrace": "{", "rbrace": "}", "langle": "⟨", "rangle": "⟩", "|": "‖", "vert": "|",
                 "Vert": "‖", "lfloor": "⌊", "rfloor": "⌋", "lceil": "⌈", "rceil": "⌉"}
            if v in m:
                return m[v]
        raise MathError("This bracket after \\left / \\right is not supported.")

    def atom(self):
        k, v = self.next()
        if k == "{":
            self.i -= 1
            return self.group()
        if k == "char":
            return ("char", v)
        if k != "cmd":
            raise MathError(f"Unexpected {v!r}.")
        if v in GREEK:
            return ("char", GREEK[v])
        if v in SYM:
            return ("sym", SYM[v])
        if v in SPACES:
            return ("space", SPACES[v])
        if v in ("{", "}", "%", "$", "#", "&", "_"):
            return ("char", v)
        if v in ("frac", "dfrac", "tfrac", "cfrac"):
            return ("frac", self.arg(), self.arg())
        if v == "binom":
            return ("delim", "(", ("frac", self.arg(), self.arg(), False), ")")
        if v == "sqrt":
            idx = None
            if self.peek()[0] == "char" and self.peek()[1] == "[":
                self.next()
                idx = self.row() if False else self._until_bracket()
            return ("sqrt", self.arg(), idx)
        if v in ("sum", "prod", "int", "oint", "iint", "iiint", "coprod"):
            return ("bigop", {"sum": "∑", "prod": "∏", "coprod": "∏"}.get(v, "∫"), v in ("sum", "prod", "coprod"), v)
        if v in FUNCS:
            return ("func", v)
        if v in STYLES:
            if v in ("text", "mathrm", "operatorname", "textbf"):
                return ("text", self._raw_group(), STYLES[v], v == "operatorname")
            return ("style", STYLES[v], self.arg())
        if v in ACCENTS:
            return ("acc", ACCENTS[v], self.arg())
        if v == "underline":
            return ("under", self.arg())
        if v == "left":
            left = self.delim_token()
            body = self.row()
            if not (self.peek() == ("cmd", "right")):
                raise MathError("\\left needs a matching \\right.")
            self.next()
            return ("delim", left, body, self.delim_token())
        if v == "right":
            raise MathError("\\right without \\left.")
        if v == "begin":
            return self.environment()
        if v == "end":
            raise MathError("\\end without \\begin.")
        if v in ("big", "Big", "bigg", "Bigg", "displaystyle", "textstyle", "limits", "nolimits", "mathstrut"):
            return ("space", 0.0)
        raise MathError(f"Unknown command \\{v}.")

    def _until_bracket(self):
        items = []
        while True:
            k, v = self.peek()
            if k is None:
                raise MathError("A [ is not closed.")
            if k == "char" and v == "]":
                self.next(); return ("row", items)
            items.append(self.item())

    def _raw_group(self) -> str:
        k, _ = self.next()
        if k != "{":
            raise MathError("A { is missing.")
        self.raw = True
        try:
            depth, out = 1, []
            while True:
                k, v = self.next()
                if k is None:
                    raise MathError("A { is not closed.")
                if k == "{":
                    depth += 1
                elif k == "}":
                    depth -= 1
                    if depth == 0:
                        return "".join(out)
                elif k in ("char", "sp"):
                    out.append(v)
                elif k == "cmd" and v in SPACES:
                    out.append(" ")
        finally:
            self.raw = False

    def environment(self):
        name = self._raw_group()
        if name not in MATRICES:
            raise MathError(f"The environment {name!r} is not supported.")
        if name == "array" and self.peek()[0] == "{":
            self._raw_group()
        rows, cur, cell = [], [], []
        while True:
            r = self.row()
            k, v = self.peek()
            cell = r
            if k == "&":
                self.next(); cur.append(cell); continue
            if k == "nl":
                self.next(); cur.append(cell); rows.append(cur); cur = []; continue
            if k == "cmd" and v == "end":
                self.next()
                if self._raw_group() != name:
                    raise MathError("\\end does not match \\begin.")
                if cur or cell[1]:
                    cur.append(cell); rows.append(cur)
                return ("mat", name, rows)
            raise MathError("An environment is not closed with \\end.")


__all__ = ["MathError", "_Parser"]
