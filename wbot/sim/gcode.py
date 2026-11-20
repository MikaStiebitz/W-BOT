"""Minimal Grbl/FluidNC G-code line parser for the emulator (words + error codes)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# FluidNC / Grbl error codes used by the emulator
ERR_EXPECTED_LETTER = 1
ERR_BAD_NUMBER = 2
ERR_INVALID_STATEMENT = 3      # unknown '$' command
ERR_NEGATIVE = 4
ERR_NO_HOMING = 5
ERR_NOT_IDLE = 8
ERR_LOCKED = 9                 # G-code locked out during alarm / jog
ERR_OVERFLOW = 11              # line too long
ERR_INVALID_JOG = 16
ERR_UNSUPPORTED = 20
ERR_MODAL_GROUP = 21
ERR_NO_FEED = 22
ERR_NOT_INTEGER = 23
ERR_WORD_REPEATED = 25
ERR_NO_AXIS_WORDS = 26
ERR_VALUE_MISSING = 28
ERR_INVALID_TARGET = 33

ERROR_TEXT = {
    1: "Expected command letter", 2: "Bad number format", 3: "Invalid statement",
    4: "Negative value", 5: "Setting disabled", 8: "Not idle", 9: "G-code locked out during alarm or jog",
    11: "Line overflow", 16: "Invalid jog command", 20: "Unsupported command", 21: "Modal group violation",
    22: "Undefined feed rate", 23: "Command value not integer", 25: "Word repeated",
    26: "No axis words", 28: "Value word missing", 33: "Invalid target",
}


class GcodeError(Exception):
    def __init__(self, code: int):
        super().__init__(f"error:{code}")
        self.code = code


_NUM = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)")

_G_GROUP = {0: "motion", 1: "motion", 4: "nonmodal", 17: "plane", 18: "plane", 19: "plane",
            20: "units", 21: "units", 54: "wcs", 55: "wcs", 56: "wcs", 57: "wcs", 58: "wcs", 59: "wcs",
            90: "distance", 91: "distance", 92: "nonmodal", 92.1: "nonmodal",
            94: "feedmode", 90.1: "arcdist", 91.1: "arcdist"}
_M_OK = {0, 1, 2, 3, 4, 5, 7, 8, 9, 30}
_WORDS = set("FPSTNXYZA")


def clean_line(raw: str) -> str:
    """Strip ``;`` and ``(...)`` comments, whitespace and control characters; upper-case."""
    out: list[str] = []
    depth = 0
    for ch in raw:
        if ch == ";" and depth == 0:
            break
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and not ch.isspace() and ch >= " ":
            out.append(ch)
    return "".join(out).upper()


@dataclass
class ParsedLine:
    g: list[float] = field(default_factory=list)
    m: list[int] = field(default_factory=list)
    words: dict[str, float] = field(default_factory=dict)


def parse_line(text: str, *, axes: str = "XYZ") -> ParsedLine:
    """Parse a cleaned line. Raises ``GcodeError`` with the FluidNC code."""
    out = ParsedLine()
    groups: set[str] = set()
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if not ("A" <= ch <= "Z"):
            raise GcodeError(ERR_EXPECTED_LETTER)
        m = _NUM.match(text, i + 1)
        if not m:
            raise GcodeError(ERR_BAD_NUMBER)
        val = float(m.group())
        i = m.end()
        if ch == "G":
            key = round(val, 1)
            if key not in _G_GROUP:
                raise GcodeError(ERR_UNSUPPORTED)
            grp = _G_GROUP[key]
            if grp in groups:
                raise GcodeError(ERR_MODAL_GROUP)
            groups.add(grp)
            out.g.append(key)
        elif ch == "M":
            if val != int(val):
                raise GcodeError(ERR_NOT_INTEGER)
            if int(val) not in _M_OK:
                raise GcodeError(ERR_UNSUPPORTED)
            out.m.append(int(val))
        elif ch in _WORDS:
            if ch in "XYZA" and ch not in axes:
                raise GcodeError(ERR_UNSUPPORTED)        # axis not configured in the firmware
            if ch in out.words:
                raise GcodeError(ERR_WORD_REPEATED)
            out.words[ch] = val
        else:
            raise GcodeError(ERR_UNSUPPORTED)
    return out
