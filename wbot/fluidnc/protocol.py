"""FluidNC / Grbl line protocol: pure parsing functions, no I/O, no threads.

Everything the controller sends is one of:

* ``ok`` / ``error:N``           -- acknowledgement of exactly one sent line
* ``ALARM:N``                    -- alarm raised (asynchronous)
* ``<Idle|MPos:..|FS:..>``       -- status report (answer to the realtime ``?``)
* ``[MSG:INFO: ...]``            -- informational/error message (asynchronous)
* ``Grbl 3.9 [FluidNC v3.9.9 ...]`` -- boot banner (the controller restarted)
* anything else                  -- payload lines of a query (``$I``, ``$G``, ``$CD`` ...)
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --- realtime bytes (never part of a line, never counted in the RX buffer) -----------
RT_STATUS = b"?"
RT_FEED_HOLD = b"!"
RT_CYCLE_START = b"~"
RT_SOFT_RESET = b"\x18"
RT_JOG_CANCEL = b"\x85"

#: Grbl/FluidNC error codes (text kept short; the UI translates if it wants to).
ERROR_TEXT: dict[int, str] = {
    1: "G-code word not recognised (expected command letter)",
    2: "Bad number format",
    3: "Invalid '$' statement",
    4: "Negative value where a positive one is required",
    5: "Setting is disabled",
    6: "Step pulse time too short",
    7: "Setting read failed",
    8: "Command only allowed while idle",
    9: "Locked: alarm or lock active (unlock with $X)",
    10: "Soft limit error",
    11: "Line overflow",
    12: "Max step rate exceeded",
    13: "Check door",
    14: "Line length exceeded",
    15: "Travel exceeded",
    16: "Invalid jog command",
    17: "Setting disabled (laser mode needs PWM)",
    18: "No homing cycles configured",
    19: "Single-axis homing not allowed",
    20: "Unsupported or invalid G-code command",
    21: "Two G-code words from the same modal group",
    22: "Feed rate not set",
    23: "G-code command needs an integer value",
    24: "Two G-code commands need conflicting axis words",
    25: "G-code word repeated",
    26: "G-code command is missing its axis words",
    27: "Invalid line number",
    28: "G-code command is missing a required value word",
    29: "Unsupported coordinate system",
    30: "G53 only valid with G0 and G1",
    31: "Axis words present but not used",
    32: "Arc needs at least one in-plane axis word",
    33: "Invalid motion target",
    34: "Arc radius error",
    35: "Arc needs in-plane offset words",
    36: "Unused value words present",
    37: "G43.1 dynamic tool length offset on wrong axis",
    38: "Maximum value exceeded",
    39: "P parameter maximum exceeded",
    60: "SD card mount failed",
    61: "SD card read failed",
    62: "SD card directory open failed",
    63: "SD card directory not found",
    64: "SD card file empty",
    65: "SD card file not found",
    66: "SD card file open failed",
    67: "SD card busy",
    68: "SD card delete failed",
    69: "SD card file create failed",
    70: "Bluetooth start failed",
    71: "WiFi start failed",
    80: "Number out of range",
    81: "Invalid value",
    90: "Message send failed",
    100: "NVS set failed",
    101: "NVS stats failed",
    110: "Authentication failed",
    111: "End of line / unexpected EOL",
    120: "Another interface is busy",
    130: "Jog cancelled",
}

ALARM_TEXT: dict[int, str] = {
    1: "Hard limit triggered, position may be lost",
    2: "Soft limit: motion target exceeds machine travel",
    3: "Reset while in motion, position may be lost",
    4: "Probe fail: probe not in expected initial state",
    5: "Probe fail: probe did not contact the workpiece",
    6: "Homing fail: reset during homing cycle",
    7: "Homing fail: safety door opened",
    8: "Homing fail: limit switch not cleared on pull-off",
    9: "Homing fail: limit switch not found",
    10: "Spindle control failure",
    11: "Control pin initially asserted",
    12: "Homing fail: ambiguous limit switch",
    13: "Hard stop",
    14: "Unhomed",
    15: "Init failure",
}

STATES = ("Idle", "Run", "Hold", "Jog", "Alarm", "Door", "Check", "Home", "Sleep")


def error_text(code: int) -> str:
    return ERROR_TEXT.get(code, f"unknown error {code}")


def alarm_text(code: int) -> str:
    return ALARM_TEXT.get(code, f"unknown alarm {code}")


# --- events -------------------------------------------------------------------------
@dataclass(frozen=True)
class Status:
    """Parsed ``<...>`` report. Positions are tuples with one value per configured axis."""
    state: str                              # "Idle", "Run", "Hold", "Jog", "Alarm", "Door", ...
    substate: int | None = None             # the N of "Hold:N" / "Door:N"
    mpos: tuple[float, ...] | None = None
    wpos: tuple[float, ...] | None = None
    wco: tuple[float, ...] | None = None
    feed: float | None = None
    spindle: float | None = None
    planner_free: int | None = None         # Bf:<planner>,<rx>
    rx_free: int | None = None
    raw: str = ""
    ov: tuple[float, ...] | None = None     # Ov:feed,rapid,spindle (percent)
    pins: str | None = None                 # Pn:...


@dataclass(frozen=True)
class Ok:
    pass


@dataclass(frozen=True)
class Error:
    code: int
    text: str


@dataclass(frozen=True)
class Alarm:
    code: int
    text: str


@dataclass(frozen=True)
class Message:
    kind: str       # "INFO", "ERR", "WARN", "DBG" ... or "" when the tag has no kind
    text: str
    raw: str = ""


@dataclass(frozen=True)
class Banner:
    text: str
    version: str | None = None      # e.g. "FluidNC v3.9.9"


@dataclass(frozen=True)
class Reply:
    text: str


Event = Ok | Error | Alarm | Status | Message | Banner | Reply

_STATUS_RE = re.compile(r"^<([^<>]*)>$")
_ERROR_RE = re.compile(r"^error:\s*(-?\d+)\s*$", re.IGNORECASE)
_ALARM_RE = re.compile(r"^ALARM:\s*(-?\d+)\s*$", re.IGNORECASE)
_MSG_RE = re.compile(r"^\[MSG:\s*(?:([A-Za-z]+):\s?)?(.*)\]$")
_BANNER_RE = re.compile(r"^Grbl\s+\S+")
_VERSION_RE = re.compile(r"(FluidNC\s+v?[\w.\-+]+)")
_VER_LINE_RE = re.compile(r"^\[VER:([^\]]*)\]")
_KNOWN_MSG_KINDS = {"INFO", "ERR", "WARN", "DBG", "ERROR", "WARNING", "DEBUG"}


def _floats(text: str) -> tuple[float, ...]:
    out: list[float] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(float(part))
        except ValueError:
            out.append(float("nan"))
    return tuple(out)


def parse_status(line: str) -> Status | None:
    """Parse ``<Idle|MPos:0.000,0.000|FS:0,0|...>``; ``None`` when ``line`` is not a report."""
    m = _STATUS_RE.match(line.strip())
    if not m:
        return None
    fields = m.group(1).split("|")
    head = fields[0].strip()
    if not head:
        return None
    state, _, sub = head.partition(":")
    substate: int | None = None
    if sub.strip():
        try:
            substate = int(sub)
        except ValueError:
            substate = None
    mpos = wpos = wco = ov = None
    feed = spindle = None
    planner_free = rx_free = None
    pins = None
    for field in fields[1:]:
        key, _, value = field.partition(":")
        key = key.strip()
        if key == "MPos":
            mpos = _floats(value)
        elif key == "WPos":
            wpos = _floats(value)
        elif key == "WCO":
            wco = _floats(value)
        elif key == "FS":
            v = _floats(value)
            feed = v[0] if v else None
            spindle = v[1] if len(v) > 1 else None
        elif key == "F":
            v = _floats(value)
            feed = v[0] if v else None
        elif key == "Bf":
            v = _floats(value)
            if v:
                planner_free = int(v[0])
            if len(v) > 1:
                rx_free = int(v[1])
        elif key == "Ov":
            ov = _floats(value)
        elif key == "Pn":
            pins = value
    return Status(state=state.strip(), substate=substate, mpos=mpos, wpos=wpos, wco=wco,
                  feed=feed, spindle=spindle, planner_free=planner_free, rx_free=rx_free,
                  raw=line.strip(), ov=ov, pins=pins)


def banner_version(text: str) -> str | None:
    """``"Grbl 3.9 [FluidNC v3.9.9 (wifi) ...]"`` -> ``"FluidNC v3.9.9"``."""
    m = _VERSION_RE.search(text)
    return m.group(1) if m else None


def parse_ver_line(line: str) -> str | None:
    """``[VER:3.9 FluidNC v3.9.9:]`` (answer to ``$I``) -> ``"3.9 FluidNC v3.9.9"``."""
    m = _VER_LINE_RE.match(line.strip())
    if not m:
        return None
    return m.group(1).strip().rstrip(":").strip()


def parse_line(line: str) -> Event | None:
    """Classify one received line (without line ending). ``None`` for an empty line."""
    s = line.strip()
    if not s:
        return None
    if s.startswith("<"):
        st = parse_status(s)
        if st is not None:
            return st
    low = s.lower()
    if low == "ok":
        return Ok()
    m = _ERROR_RE.match(s)
    if m:
        code = int(m.group(1))
        return Error(code, error_text(code))
    m = _ALARM_RE.match(s)
    if m:
        code = int(m.group(1))
        return Alarm(code, alarm_text(code))
    if s.startswith("[MSG:"):
        mm = _MSG_RE.match(s)
        if mm:
            kind = (mm.group(1) or "").upper()
            text = mm.group(2)
            if kind and kind not in _KNOWN_MSG_KINDS:
                # "[MSG:Reset ...]" style: the "kind" was really the first word
                return Message("", s[5:-1].strip() if s.endswith("]") else s[5:], s)
            return Message(kind, text.strip(), s)
        return Message("", s[5:].rstrip("]").strip(), s)
    if _BANNER_RE.match(s):
        return Banner(s, banner_version(s))
    return Reply(s)
