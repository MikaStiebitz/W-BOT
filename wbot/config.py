"""Machine configuration for W-BOT (single source of truth: ``config/wbot.yaml``).

Units: millimetres, degrees, seconds, microseconds. Board coordinates:
origin = lower-left corner of the drawing area, X right, Y up.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

XY = tuple[float, float]

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "wbot.yaml"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Board(_Model):
    width_mm: float = 1500.0
    height_mm: float = 1000.0
    margin_mm: float = 10.0          # keep the pen this far from the board edge ...
    # ... unless a side has its own margin (belts get flat near the anchor line and one belt
    # goes slack in the far bottom corners, see wbot/workspace.py)
    margin_left_mm: float | None = 120.0
    margin_right_mm: float | None = 120.0
    margin_bottom_mm: float | None = None
    margin_top_mm: float | None = 250.0     # gondola V2: belts attach 176 mm above the pen (see wbot/workspace.py)


class Geometry(_Model):
    """Physical belt geometry ("truth"). FluidNC knows only the naive part."""
    anchor_left: XY = (47.0, 1041.0)      # centre of the left pulley (motors clamped to the frame)
    anchor_right: XY = (1453.0, 1041.0)   # centre of the right pulley
    pulley_pitch_radius_mm: float = 12.732  # GT2 40T: 40*2/(2*pi)
    # "tangent": belt leaves the pulley tangentially (+ wrap arc)  -> accurate
    # "naive"  : belt runs to the pulley centre (what FluidNC computes) -> no pre-distortion
    model: Literal["tangent", "naive"] = "tangent"
    # Gondola V2: the belts leave the gondola at two attach points, x = -s (left belt) and +s (right belt)
    # relative to the pen axis and h above the pen tip; the rigid gondola tilts into static equilibrium
    # (wbot/gondola.py). Centre of mass (Fusion model): com_offset_x right of the pen axis, com_height above the
    # pen tip. Known simplification: the CoM sits ~30 mm farther from the board than the belts (out of plane) - ignored.
    attach_half_spacing_mm: float = Field(8.0, ge=0.0)
    attach_height_mm: float = Field(176.0, ge=0.0)
    com_height_mm: float = 33.8
    com_offset_x_mm: float = 1.2
    gondola_tilt: bool = True             # False = attach points keep their position relative to the pen
    gondola_mass_kg: float = Field(0.404, gt=0.0)   # statics only (wbot/workspace.py); 0.345..0.465 depending on fill
    # Calibration correction of the attach vector (pen tip minus attach point, as in the old point model):
    # (0, 0) = exactly the nominal geometry above.
    pen_offset_mm: XY = (0.0, 0.0)
    belt_scale_left: float = 1.0          # real belt mm per firmware belt mm
    belt_scale_right: float = 1.0


class Drive(_Model):
    motor_steps_per_rev: int = 200
    microsteps: int = 16
    pulley_teeth: int = 40
    belt_pitch_mm: float = 2.0
    invert_left: bool = False
    invert_right: bool = False

    @property
    def steps_per_mm(self) -> float:
        return self.motor_steps_per_rev * self.microsteps / (self.pulley_teeth * self.belt_pitch_mm)


class Firmware(_Model):
    """What the FluidNC config contains (the naive model it runs)."""
    # None -> same as geometry anchors.
    anchor_left: XY | None = None
    anchor_right: XY | None = None
    segment_length_mm: float = 1.0
    max_rate_mm_min: float = 3000.0
    # Position FluidNC reports right after boot, before the host told it anything.
    # Only used by the simulator and as a hint; the real value is read from `?`.
    boot_position: XY = (0.0, 0.0)
    # Where the pen must hang when the board is powered up. FluidNC's origin (0,0) IS this
    # point: unless anchor_left/right are set explicitly, the firmware anchors are the true
    # anchors minus boot_physical. (0,0) = board's lower-left corner (the active config).
    boot_physical: XY = (0.0, 0.0)


class Motion(_Model):
    travel_feed_mm_min: float = 3000.0
    draw_feed_mm_min: float = 1500.0
    jog_feed_mm_min: float = 3000.0
    acceleration_mm_s2: float = 250.0
    junction_deviation_mm: float = 0.05
    park_position: XY = (750.0, 700.0)
    # Pre-distortion: maximum deviation between the true straight line and the
    # chord FluidNC will actually follow between two pre-distorted vertices.
    predistort_tolerance_mm: float = 0.02
    max_chord_mm: float = 25.0
    min_chord_mm: float = 0.5


class ServoSpec(_Model):
    """One positional RC servo (MG90S) on a FluidNC servo axis.

    pulse_us(angle) = center_us + us_per_deg * angle   (angle = mechanical degrees)
    The firmware pulse window [fw_min_us, fw_max_us] is the hard limit.
    FluidNC maps its axis range [axis_min, axis_min + axis_travel] linearly
    onto that window (axis units are an implementation detail of servo.py).
    """
    axis: Literal["Z", "A"]
    pin: str
    center_us: float = 1500.0
    us_per_deg: float = 1000.0 / 180.0    # MG90S: 1000..2000 us == 180 deg
    min_deg: float
    max_deg: float
    axis_min: float                       # FluidNC axis value at fw_min_us
    axis_travel: float                    # FluidNC axis range (max_travel_mm)
    settle_s: float = 0.30                # wait after a move before anything else
    speed_deg_s: float = 500.0            # MG90S: ~0.1 s / 60 deg -> 600; be conservative

    @model_validator(mode="after")
    def _check(self) -> "ServoSpec":
        if self.min_deg >= self.max_deg:
            raise ValueError("min_deg must be < max_deg")
        if self.us_per_deg <= 0:
            raise ValueError("us_per_deg must be > 0")
        return self


class PenServo(ServoSpec):
    axis: Literal["Z", "A"] = "Z"
    pin: str = "gpio.4"
    min_deg: float = -44.0
    max_deg: float = 44.0
    axis_min: float = -44.0
    axis_travel: float = 88.0
    up_deg: float = 44.0
    down_deg: float = -44.0


class RevolverServo(ServoSpec):
    axis: Literal["Z", "A"] = "A"
    pin: str = "gpio.32"
    min_deg: float = -66.0
    max_deg: float = 66.0
    axis_min: float = 0.0
    axis_travel: float = 132.0
    slots_deg: list[float] = Field(default_factory=lambda: [-60.0, 0.0, 60.0])
    enabled: bool = True                   # False -> no A axis in the firmware config


class Servos(_Model):
    pen: PenServo = Field(default_factory=PenServo)
    revolver: RevolverServo = Field(default_factory=RevolverServo)


class Color(_Model):
    name: str
    hex: str = "#000000"


class Serial(_Model):
    port: str | None = "COM3"
    baudrate: int = 115200
    rx_buffer_bytes: int = 127


class Wifi(_Model):
    """FluidNC over WiFi. No authentication exists on these ports - trusted LAN only."""
    host: str = "fluidnc.local"
    port: int | None = None                # None: 23 (telnet) / 81 (websocket)
    protocol: Literal["telnet", "websocket"] = "telnet"

    @property
    def effective_port(self) -> int:
        return self.port or {"telnet": 23, "websocket": 81}[self.protocol]


class Connection(_Model):
    transport: Literal["serial", "sim", "wifi"] = "sim"
    serial: Serial = Field(default_factory=Serial)
    wifi: Wifi = Field(default_factory=Wifi)
    status_poll_hz: float = 10.0


class Api(_Model):
    host: str = "127.0.0.1"
    port: int = 8000


class Mcp(_Model):
    """AI control via MCP (wbot/mcp_server.py). Read live on every tool call."""
    enabled: bool = True                  # False: /mcp answers 503, the stdio server only offers emergency_stop
    allow_motion: bool = True             # False = read-only/preview-only kill switch (nothing moves)
    max_strokes: int = Field(2000, ge=1)
    max_points: int = Field(200000, ge=1)
    require_confirm_real_hardware: bool = False   # True: drawing on a serial/wifi board needs confirm=true


class Config(_Model):
    version: int = 2
    board: Board = Field(default_factory=Board)
    geometry: Geometry = Field(default_factory=Geometry)
    drive: Drive = Field(default_factory=Drive)
    firmware: Firmware = Field(default_factory=Firmware)
    motion: Motion = Field(default_factory=Motion)
    servos: Servos = Field(default_factory=Servos)
    colors: list[Color] = Field(default_factory=lambda: [
        Color(name="schwarz", hex="#111111"),
        Color(name="rot", hex="#d32f2f"),
        Color(name="blau", hex="#1565c0"),
    ])
    connection: Connection = Field(default_factory=Connection)
    api: Api = Field(default_factory=Api)
    mcp: Mcp = Field(default_factory=Mcp)

    @field_validator("colors")
    @classmethod
    def _three_colors(cls, v: list[Color]) -> list[Color]:
        if not 1 <= len(v) <= 3:
            raise ValueError("1..3 colours")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> "Config":
        pen, rev = self.servos.pen, self.servos.revolver
        if not (pen.min_deg <= pen.down_deg <= pen.max_deg and pen.min_deg <= pen.up_deg <= pen.max_deg):
            raise ValueError("pen up/down angle outside the pen servo limits")
        if pen.axis == rev.axis:
            raise ValueError("pen and revolver cannot share a firmware axis")
        for s in rev.slots_deg:
            if not rev.min_deg <= s <= rev.max_deg:
                raise ValueError("revolver slot outside servo limits")
        if len(rev.slots_deg) != len(self.colors):
            raise ValueError("number of revolver slots must equal number of colours")
        g = self.geometry
        if not (g.anchor_left[0] < g.anchor_right[0]):
            raise ValueError("anchor_left must be left of anchor_right")
        if g.gondola_tilt and g.attach_half_spacing_mm > 0 and g.com_height_mm >= g.attach_height_mm:
            raise ValueError("com_height_mm must be below attach_height_mm (otherwise the gondola is unstable)")
        return self

    # --- derived -----------------------------------------------------------
    @property
    def fw_anchor_left(self) -> XY:
        if self.firmware.anchor_left:
            return self.firmware.anchor_left
        a, b = self.geometry.anchor_left, self.firmware.boot_physical
        return (a[0] - b[0], a[1] - b[1])

    @property
    def fw_anchor_right(self) -> XY:
        if self.firmware.anchor_right:
            return self.firmware.anchor_right
        a, b = self.geometry.anchor_right, self.firmware.boot_physical
        return (a[0] - b[0], a[1] - b[1])

    @property
    def steps_per_mm(self) -> float:
        return self.drive.steps_per_mm

    def safe_rect(self) -> tuple[float, float, float, float]:
        """(xmin, ymin, xmax, ymax) the *pen* may reach."""
        l, b, r, t = self.board_margins()
        return (l, b, self.board.width_mm - r, self.board.height_mm - t)

    def board_margins(self) -> tuple[float, float, float, float]:
        """(left, bottom, right, top) margins in mm."""
        b = self.board
        pick = lambda v: b.margin_mm if v is None else v      # noqa: E731
        return (pick(b.margin_left_mm), pick(b.margin_bottom_mm),
                pick(b.margin_right_mm), pick(b.margin_top_mm))


def load_config(path: str | Path | None = None) -> Config:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.exists():
        return Config()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return Config.model_validate(data)


def dump_config(cfg: Config) -> str:
    return yaml.safe_dump(cfg.model_dump(mode="json"), sort_keys=False, allow_unicode=True)


def save_config(cfg: Config, path: str | Path | None = None) -> None:
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dump_config(cfg), encoding="utf-8")


def deep_update(cfg: Config, patch: dict) -> Config:
    """Return a new Config with ``patch`` (nested dict) merged in and re-validated."""
    def merge(a: dict, b: dict) -> dict:
        out = dict(a)
        for k, v in b.items():
            out[k] = merge(a[k], v) if isinstance(v, dict) and isinstance(a.get(k), dict) else v
        return out
    return Config.model_validate(merge(cfg.model_dump(mode="python"), patch))


__all__ = ["Config", "load_config", "save_config", "dump_config", "deep_update",
           "DEFAULT_CONFIG_PATH"]
