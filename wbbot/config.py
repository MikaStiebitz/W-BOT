"""Machine configuration: load, validate, merge, save.

``config/machine.yaml`` holds the hardware truth from the build.
``config/calibration.yaml`` is written by the calibration wizard and is
deep-merged *on top of* the machine config at load time, so the wizard never
has to touch the hand-maintained file.

Everything is validated at construction time — an out-of-range value must fail
here, not three modules later when a belt length comes out negative.
"""

from __future__ import annotations

import copy
import dataclasses
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

import yaml

__all__ = [
    "ConfigError",
    "BoardConfig",
    "GeometryConfig",
    "DriveConfig",
    "GondolaConfig",
    "MotionConfig",
    "PenServoConfig",
    "RevolverServoConfig",
    "ServoConfig",
    "ColorConfig",
    "SafeAreaConfig",
    "SerialConfig",
    "WebsocketConfig",
    "ConnectionConfig",
    "PathsConfig",
    "ImagingConfig",
    "CameraConfig",
    "ApiConfig",
    "MachineConfig",
    "load_config",
    "save_calibration",
    "to_plain",
    "default_config_dir",
    "PACKAGE_ROOT",
    "PROJECT_ROOT",
]

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent


class ConfigError(ValueError):
    """Raised when the configuration is missing, malformed or out of range."""


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def default_config_dir() -> Path:
    """Directory holding machine.yaml / calibration.yaml.

    Overridable with ``WBBOT_CONFIG_DIR`` so tests and multiple machines can
    coexist without touching the repo copy.
    """
    env = os.environ.get("WBBOT_CONFIG_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return PROJECT_ROOT / "config"


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge ``overlay`` into a copy of ``base``.

    ``None`` values in the overlay are ignored (a calibration file that has
    not filled a field yet must not wipe the machine default).
    """
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _req(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise ConfigError(f"{where}: missing required key {key!r}")
    return d[key]


def _num(value: Any, where: str, *, minimum: float | None = None,
         maximum: float | None = None, allow_none: bool = False) -> float | None:
    if value is None:
        if allow_none:
            return None
        raise ConfigError(f"{where}: must be a number, got null")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}: must be a number, got {value!r}")
    v = float(value)
    if not math.isfinite(v):
        raise ConfigError(f"{where}: must be finite, got {v!r}")
    if minimum is not None and v < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {v}")
    if maximum is not None and v > maximum:
        raise ConfigError(f"{where}: must be <= {maximum}, got {v}")
    return v


def _int(value: Any, where: str, *, minimum: int | None = None,
         maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}: must be an integer, got {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"{where}: must be >= {minimum}, got {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{where}: must be <= {maximum}, got {value}")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: must be true/false, got {value!r}")
    return value


def _xy(value: Any, where: str, *, allow_none: bool = False
        ) -> tuple[float, float] | None:
    if value is None:
        if allow_none:
            return None
        raise ConfigError(f"{where}: must be a [x, y] pair, got null")
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise ConfigError(f"{where}: must be a [x, y] pair, got {value!r}")
    return (float(_num(value[0], f"{where}[0]")), float(_num(value[1], f"{where}[1]")))


# --------------------------------------------------------------------------
# sections
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class BoardConfig:
    width_mm: float
    height_mm: float

    @classmethod
    def from_dict(cls, d: dict) -> "BoardConfig":
        w = "board"
        return cls(
            width_mm=_num(_req(d, "width_mm", w), f"{w}.width_mm", minimum=1.0),
            height_mm=_num(_req(d, "height_mm", w), f"{w}.height_mm", minimum=1.0),
        )


@dataclass(frozen=True)
class GeometryConfig:
    anchor_left: tuple[float, float]
    anchor_right: tuple[float, float]
    pulley_pitch_radius_mm: float
    kinematics_model: str
    precompensate: bool
    precompensate_reference: tuple[float, float] | None

    @property
    def anchor_distance_mm(self) -> float:
        ax, ay = self.anchor_left
        bx, by = self.anchor_right
        return math.hypot(bx - ax, by - ay)

    @classmethod
    def from_dict(cls, d: dict) -> "GeometryConfig":
        w = "geometry"
        model = _req(d, "kinematics_model", w)
        if model not in ("naive", "tangent"):
            raise ConfigError(
                f"{w}.kinematics_model: must be 'naive' or 'tangent', got {model!r}")
        left = _xy(_req(d, "anchor_left", w), f"{w}.anchor_left")
        right = _xy(_req(d, "anchor_right", w), f"{w}.anchor_right")
        if right[0] <= left[0]:
            raise ConfigError(
                f"{w}: anchor_right.x ({right[0]}) must be right of "
                f"anchor_left.x ({left[0]})")
        radius = _num(_req(d, "pulley_pitch_radius_mm", w),
                      f"{w}.pulley_pitch_radius_mm", minimum=0.0, maximum=200.0)
        return cls(
            anchor_left=left,
            anchor_right=right,
            pulley_pitch_radius_mm=radius,
            kinematics_model=model,
            precompensate=_bool(d.get("precompensate", False), f"{w}.precompensate"),
            precompensate_reference=_xy(d.get("precompensate_reference"),
                                        f"{w}.precompensate_reference",
                                        allow_none=True),
        )


@dataclass(frozen=True)
class DriveConfig:
    motor_steps_per_rev: int
    microsteps: int
    pulley_teeth: int
    belt_pitch_mm: float
    steps_per_mm_left: float
    steps_per_mm_right: float
    invert_left: bool
    invert_right: bool
    belt_zero_left_mm: float | None
    belt_zero_right_mm: float | None

    @property
    def nominal_steps_per_mm(self) -> float:
        """steps_per_mm = (steps_per_rev * microsteps) / (teeth * pitch)."""
        return (self.motor_steps_per_rev * self.microsteps) / (
            self.pulley_teeth * self.belt_pitch_mm)

    @property
    def belt_travel_per_rev_mm(self) -> float:
        return self.pulley_teeth * self.belt_pitch_mm

    @classmethod
    def from_dict(cls, d: dict) -> "DriveConfig":
        w = "drive"
        steps_rev = _int(_req(d, "motor_steps_per_rev", w),
                         f"{w}.motor_steps_per_rev", minimum=1)
        micro = _int(_req(d, "microsteps", w), f"{w}.microsteps", minimum=1, maximum=256)
        teeth = _int(_req(d, "pulley_teeth", w), f"{w}.pulley_teeth", minimum=1)
        pitch = _num(_req(d, "belt_pitch_mm", w), f"{w}.belt_pitch_mm", minimum=0.1)
        nominal = (steps_rev * micro) / (teeth * pitch)
        left = _num(d.get("steps_per_mm_left"), f"{w}.steps_per_mm_left",
                    minimum=0.01, allow_none=True)
        right = _num(d.get("steps_per_mm_right"), f"{w}.steps_per_mm_right",
                     minimum=0.01, allow_none=True)
        return cls(
            motor_steps_per_rev=steps_rev,
            microsteps=micro,
            pulley_teeth=teeth,
            belt_pitch_mm=pitch,
            steps_per_mm_left=nominal if left is None else left,
            steps_per_mm_right=nominal if right is None else right,
            invert_left=_bool(d.get("invert_left", False), f"{w}.invert_left"),
            invert_right=_bool(d.get("invert_right", False), f"{w}.invert_right"),
            belt_zero_left_mm=_num(d.get("belt_zero_left_mm"),
                                   f"{w}.belt_zero_left_mm", allow_none=True),
            belt_zero_right_mm=_num(d.get("belt_zero_right_mm"),
                                    f"{w}.belt_zero_right_mm", allow_none=True),
        )


@dataclass(frozen=True)
class GondolaConfig:
    mass_kg: float
    pen_offset_mm: tuple[float, float]
    belt_attach_spacing_mm: float
    marker_line_width_mm: float
    marker_diameter_mm: float

    @classmethod
    def from_dict(cls, d: dict) -> "GondolaConfig":
        w = "gondola"
        return cls(
            mass_kg=_num(_req(d, "mass_kg", w), f"{w}.mass_kg", minimum=0.001, maximum=50.0),
            pen_offset_mm=_xy(d.get("pen_offset_mm", [0.0, 0.0]), f"{w}.pen_offset_mm"),
            belt_attach_spacing_mm=_num(d.get("belt_attach_spacing_mm", 0.0),
                                        f"{w}.belt_attach_spacing_mm", minimum=0.0),
            marker_line_width_mm=_num(d.get("marker_line_width_mm", 2.0),
                                      f"{w}.marker_line_width_mm", minimum=0.01),
            marker_diameter_mm=_num(d.get("marker_diameter_mm", 16.8),
                                    f"{w}.marker_diameter_mm", minimum=0.1),
        )


@dataclass(frozen=True)
class MotionConfig:
    travel_feed_mm_min: float
    draw_feed_mm_min: float
    acceleration_mm_s2: float
    jerk_mm_min: float
    junction_deviation_mm: float
    pen_down_dwell_s: float
    pen_up_dwell_s: float
    color_change_dwell_s: float
    park_position: tuple[float, float]

    @classmethod
    def from_dict(cls, d: dict) -> "MotionConfig":
        w = "motion"
        return cls(
            travel_feed_mm_min=_num(_req(d, "travel_feed_mm_min", w),
                                    f"{w}.travel_feed_mm_min", minimum=1.0, maximum=30000.0),
            draw_feed_mm_min=_num(_req(d, "draw_feed_mm_min", w),
                                  f"{w}.draw_feed_mm_min", minimum=1.0, maximum=30000.0),
            acceleration_mm_s2=_num(_req(d, "acceleration_mm_s2", w),
                                    f"{w}.acceleration_mm_s2", minimum=1.0, maximum=10000.0),
            jerk_mm_min=_num(d.get("jerk_mm_min", 0.0), f"{w}.jerk_mm_min", minimum=0.0),
            junction_deviation_mm=_num(d.get("junction_deviation_mm", 0.05),
                                       f"{w}.junction_deviation_mm",
                                       minimum=0.0, maximum=10.0),
            pen_down_dwell_s=_num(d.get("pen_down_dwell_s", 0.0),
                                  f"{w}.pen_down_dwell_s", minimum=0.0, maximum=10.0),
            pen_up_dwell_s=_num(d.get("pen_up_dwell_s", 0.0),
                                f"{w}.pen_up_dwell_s", minimum=0.0, maximum=10.0),
            color_change_dwell_s=_num(d.get("color_change_dwell_s", 0.5),
                                      f"{w}.color_change_dwell_s", minimum=0.0, maximum=30.0),
            park_position=_xy(_req(d, "park_position", w), f"{w}.park_position"),
        )


@dataclass(frozen=True)
class PenServoConfig:
    axis: str
    axis_max_travel: float
    down_pos: float
    up_pos: float
    pwm_hz: int
    min_pulse_us: int
    max_pulse_us: int
    output_pin: str

    @classmethod
    def from_dict(cls, d: dict) -> "PenServoConfig":
        w = "servo.pen"
        axis = str(_req(d, "axis", w)).upper()
        if axis not in ("X", "Y", "Z", "A", "B", "C"):
            raise ConfigError(f"{w}.axis: must be one of XYZABC, got {axis!r}")
        travel = _num(_req(d, "axis_max_travel", w), f"{w}.axis_max_travel", minimum=1.0)
        down = _num(_req(d, "down_pos", w), f"{w}.down_pos", minimum=0.0, maximum=travel)
        up = _num(_req(d, "up_pos", w), f"{w}.up_pos", minimum=0.0, maximum=travel)
        if abs(up - down) < 1e-9:
            raise ConfigError(f"{w}: up_pos and down_pos must differ")
        return cls(
            axis=axis, axis_max_travel=travel, down_pos=down, up_pos=up,
            pwm_hz=_int(d.get("pwm_hz", 50), f"{w}.pwm_hz", minimum=1, maximum=500),
            min_pulse_us=_int(d.get("min_pulse_us", 1000), f"{w}.min_pulse_us",
                              minimum=100, maximum=20000),
            max_pulse_us=_int(d.get("max_pulse_us", 2000), f"{w}.max_pulse_us",
                              minimum=100, maximum=20000),
            output_pin=str(d.get("output_pin", "NO_PIN")),
        )


@dataclass(frozen=True)
class RevolverServoConfig:
    axis: str
    axis_max_travel: float
    positions: tuple[float, ...]
    pwm_hz: int
    min_pulse_us: int
    max_pulse_us: int
    output_pin: str

    @classmethod
    def from_dict(cls, d: dict) -> "RevolverServoConfig":
        w = "servo.revolver"
        axis = str(_req(d, "axis", w)).upper()
        if axis not in ("X", "Y", "Z", "A", "B", "C"):
            raise ConfigError(f"{w}.axis: must be one of XYZABC, got {axis!r}")
        travel = _num(_req(d, "axis_max_travel", w), f"{w}.axis_max_travel", minimum=1.0)
        raw = _req(d, "positions", w)
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) == 0:
            raise ConfigError(f"{w}.positions: must be a non-empty list")
        positions = tuple(
            _num(v, f"{w}.positions[{i}]", minimum=0.0, maximum=travel)
            for i, v in enumerate(raw)
        )
        return cls(
            axis=axis, axis_max_travel=travel, positions=positions,
            pwm_hz=_int(d.get("pwm_hz", 50), f"{w}.pwm_hz", minimum=1, maximum=500),
            min_pulse_us=_int(d.get("min_pulse_us", 1000), f"{w}.min_pulse_us",
                              minimum=100, maximum=20000),
            max_pulse_us=_int(d.get("max_pulse_us", 2000), f"{w}.max_pulse_us",
                              minimum=100, maximum=20000),
            output_pin=str(d.get("output_pin", "NO_PIN")),
        )


@dataclass(frozen=True)
class ServoConfig:
    pen: PenServoConfig
    revolver: RevolverServoConfig
    feed_units_min: float
    idle_disable: bool
    idle_disable_after_s: float
    idle_disable_gcode: str

    @classmethod
    def from_dict(cls, d: dict) -> "ServoConfig":
        w = "servo"
        return cls(
            pen=PenServoConfig.from_dict(_req(d, "pen", w)),
            revolver=RevolverServoConfig.from_dict(_req(d, "revolver", w)),
            feed_units_min=_num(d.get("feed_units_min", 3000.0),
                                f"{w}.feed_units_min", minimum=1.0),
            idle_disable=_bool(d.get("idle_disable", True), f"{w}.idle_disable"),
            idle_disable_after_s=_num(d.get("idle_disable_after_s", 5.0),
                                      f"{w}.idle_disable_after_s", minimum=0.0),
            idle_disable_gcode=str(d.get("idle_disable_gcode", "") or ""),
        )


@dataclass(frozen=True)
class ColorConfig:
    slot: int
    name: str
    hex: str

    @classmethod
    def from_dict(cls, d: dict, index: int) -> "ColorConfig":
        w = f"colors[{index}]"
        raw_hex = str(d.get("hex", "#000000")).strip()
        if not raw_hex.startswith("#") or len(raw_hex) not in (4, 7):
            raise ConfigError(f"{w}.hex: must be '#rgb' or '#rrggbb', got {raw_hex!r}")
        try:
            int(raw_hex[1:], 16)
        except ValueError as exc:
            raise ConfigError(f"{w}.hex: not a hex colour: {raw_hex!r}") from exc
        return cls(
            slot=_int(_req(d, "slot", w), f"{w}.slot", minimum=1, maximum=16),
            name=str(d.get("name", f"slot{d.get('slot')}")),
            hex=raw_hex,
        )

    @property
    def rgb(self) -> tuple[int, int, int]:
        h = self.hex.lstrip("#")
        if len(h) == 3:
            h = "".join(c * 2 for c in h)
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


@dataclass(frozen=True)
class SafeAreaConfig:
    board_margin_mm: float
    min_tension_n: float
    max_error_amplification: float
    min_distance_to_anchor_line_mm: float
    grid_step_mm: float

    @classmethod
    def from_dict(cls, d: dict) -> "SafeAreaConfig":
        w = "safe_area"
        return cls(
            board_margin_mm=_num(d.get("board_margin_mm", 10.0),
                                 f"{w}.board_margin_mm", minimum=0.0),
            min_tension_n=_num(d.get("min_tension_n", 0.25),
                               f"{w}.min_tension_n", minimum=0.0),
            max_error_amplification=_num(d.get("max_error_amplification", 4.0),
                                         f"{w}.max_error_amplification", minimum=1.0),
            min_distance_to_anchor_line_mm=_num(
                d.get("min_distance_to_anchor_line_mm", 150.0),
                f"{w}.min_distance_to_anchor_line_mm", minimum=0.0),
            grid_step_mm=_num(d.get("grid_step_mm", 10.0),
                              f"{w}.grid_step_mm", minimum=0.5, maximum=200.0),
        )


@dataclass(frozen=True)
class SerialConfig:
    port: str | None
    baudrate: int
    rx_buffer_bytes: int


@dataclass(frozen=True)
class WebsocketConfig:
    host: str | None
    port: int


@dataclass(frozen=True)
class ConnectionConfig:
    transport: str
    serial: SerialConfig
    websocket: WebsocketConfig
    status_poll_hz: float
    watchdog_timeout_s: float

    @classmethod
    def from_dict(cls, d: dict) -> "ConnectionConfig":
        w = "connection"
        transport = str(d.get("transport", "simulator"))
        if transport not in ("simulator", "serial", "websocket"):
            raise ConfigError(
                f"{w}.transport: must be simulator|serial|websocket, got {transport!r}")
        ser = d.get("serial") or {}
        ws = d.get("websocket") or {}
        return cls(
            transport=transport,
            serial=SerialConfig(
                port=(str(ser["port"]) if ser.get("port") else None),
                baudrate=_int(ser.get("baudrate", 115200), f"{w}.serial.baudrate",
                              minimum=1200),
                rx_buffer_bytes=_int(ser.get("rx_buffer_bytes", 128),
                                     f"{w}.serial.rx_buffer_bytes",
                                     minimum=16, maximum=8192),
            ),
            websocket=WebsocketConfig(
                host=(str(ws["host"]) if ws.get("host") else None),
                port=_int(ws.get("port", 81), f"{w}.websocket.port",
                          minimum=1, maximum=65535),
            ),
            status_poll_hz=_num(d.get("status_poll_hz", 12.0), f"{w}.status_poll_hz",
                                minimum=0.5, maximum=100.0),
            watchdog_timeout_s=_num(d.get("watchdog_timeout_s", 3.0),
                                    f"{w}.watchdog_timeout_s", minimum=0.2),
        )


@dataclass(frozen=True)
class PathsConfig:
    simplify_tolerance_mm: float
    merge_tolerance_mm: float
    min_path_length_mm: float
    optimize_time_budget_s: float
    two_opt_max_iterations: int

    @classmethod
    def from_dict(cls, d: dict) -> "PathsConfig":
        w = "paths"
        return cls(
            simplify_tolerance_mm=_num(d.get("simplify_tolerance_mm", 0.5),
                                       f"{w}.simplify_tolerance_mm", minimum=0.0),
            merge_tolerance_mm=_num(d.get("merge_tolerance_mm", 1.0),
                                    f"{w}.merge_tolerance_mm", minimum=0.0),
            min_path_length_mm=_num(d.get("min_path_length_mm", 3.0),
                                    f"{w}.min_path_length_mm", minimum=0.0),
            optimize_time_budget_s=_num(d.get("optimize_time_budget_s", 5.0),
                                        f"{w}.optimize_time_budget_s", minimum=0.0),
            two_opt_max_iterations=_int(d.get("two_opt_max_iterations", 20000),
                                        f"{w}.two_opt_max_iterations", minimum=0),
        )


@dataclass(frozen=True)
class ImagingConfig:
    max_input_dimension_px: int
    bilateral_d: int
    bilateral_sigma_color: float
    bilateral_sigma_space: float
    canny_low: int
    canny_high: int
    min_contour_points: int

    @classmethod
    def from_dict(cls, d: dict) -> "ImagingConfig":
        w = "imaging"
        return cls(
            max_input_dimension_px=_int(d.get("max_input_dimension_px", 1600),
                                        f"{w}.max_input_dimension_px",
                                        minimum=64, maximum=8192),
            bilateral_d=_int(d.get("bilateral_d", 9), f"{w}.bilateral_d",
                             minimum=1, maximum=51),
            bilateral_sigma_color=_num(d.get("bilateral_sigma_color", 75.0),
                                       f"{w}.bilateral_sigma_color", minimum=0.0),
            bilateral_sigma_space=_num(d.get("bilateral_sigma_space", 75.0),
                                       f"{w}.bilateral_sigma_space", minimum=0.0),
            canny_low=_int(d.get("canny_low", 60), f"{w}.canny_low", minimum=0, maximum=255),
            canny_high=_int(d.get("canny_high", 160), f"{w}.canny_high",
                            minimum=0, maximum=255),
            min_contour_points=_int(d.get("min_contour_points", 4),
                                    f"{w}.min_contour_points", minimum=2),
        )


@dataclass(frozen=True)
class CameraConfig:
    enabled: bool
    device_index: int
    resolution: tuple[int, int]
    homography: tuple[tuple[float, ...], ...] | None

    @classmethod
    def from_dict(cls, d: dict) -> "CameraConfig":
        w = "camera"
        res = d.get("resolution", [1920, 1080])
        if not isinstance(res, Sequence) or len(res) != 2:
            raise ConfigError(f"{w}.resolution: must be [width, height]")
        homography = d.get("homography")
        h_out: tuple[tuple[float, ...], ...] | None = None
        if homography is not None:
            rows = list(homography)
            if len(rows) != 3 or any(len(r) != 3 for r in rows):
                raise ConfigError(f"{w}.homography: must be a 3x3 matrix")
            h_out = tuple(tuple(float(v) for v in r) for r in rows)
        return cls(
            enabled=_bool(d.get("enabled", False), f"{w}.enabled"),
            device_index=_int(d.get("device_index", 0), f"{w}.device_index", minimum=0),
            resolution=(_int(res[0], f"{w}.resolution[0]", minimum=16),
                        _int(res[1], f"{w}.resolution[1]", minimum=16)),
            homography=h_out,
        )


@dataclass(frozen=True)
class ApiConfig:
    host: str
    port: int

    @classmethod
    def from_dict(cls, d: dict) -> "ApiConfig":
        w = "api"
        return cls(
            host=str(d.get("host", "127.0.0.1")),
            port=_int(d.get("port", 8000), f"{w}.port", minimum=1, maximum=65535),
        )


# --------------------------------------------------------------------------
# root
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MachineConfig:
    """Fully validated machine configuration."""

    version: int
    board: BoardConfig
    geometry: GeometryConfig
    drive: DriveConfig
    gondola: GondolaConfig
    motion: MotionConfig
    servo: ServoConfig
    colors: tuple[ColorConfig, ...]
    safe_area: SafeAreaConfig
    connection: ConnectionConfig
    paths: PathsConfig
    imaging: ImagingConfig
    camera: CameraConfig
    api: ApiConfig
    raw: dict = field(default_factory=dict, repr=False, compare=False)

    # -- derived -----------------------------------------------------------

    @property
    def n_colors(self) -> int:
        return len(self.colors)

    def color(self, index: int) -> ColorConfig:
        """Colour by 0-based index."""
        if not 0 <= index < len(self.colors):
            raise ConfigError(
                f"colour index {index} out of range (0..{len(self.colors) - 1})")
        return self.colors[index]

    def revolver_position(self, index: int) -> float:
        """Revolver servo axis position for a 0-based colour index."""
        pos = self.servo.revolver.positions
        if not 0 <= index < len(pos):
            raise ConfigError(
                f"colour index {index} has no revolver position "
                f"(configured: {len(pos)})")
        return pos[index]

    # -- (de)serialisation -------------------------------------------------

    @classmethod
    def from_dict(cls, d: dict) -> "MachineConfig":
        if not isinstance(d, dict):
            raise ConfigError("configuration root must be a mapping")
        colors_raw = d.get("colors") or []
        if not colors_raw:
            raise ConfigError("colors: at least one marker must be configured")
        colors = tuple(ColorConfig.from_dict(c, i) for i, c in enumerate(colors_raw))
        slots = [c.slot for c in colors]
        if len(set(slots)) != len(slots):
            raise ConfigError(f"colors: duplicate slot numbers {slots}")

        cfg = cls(
            version=_int(d.get("version", 1), "version", minimum=1),
            board=BoardConfig.from_dict(_req(d, "board", "root")),
            geometry=GeometryConfig.from_dict(_req(d, "geometry", "root")),
            drive=DriveConfig.from_dict(_req(d, "drive", "root")),
            gondola=GondolaConfig.from_dict(_req(d, "gondola", "root")),
            motion=MotionConfig.from_dict(_req(d, "motion", "root")),
            servo=ServoConfig.from_dict(_req(d, "servo", "root")),
            colors=colors,
            safe_area=SafeAreaConfig.from_dict(d.get("safe_area") or {}),
            connection=ConnectionConfig.from_dict(d.get("connection") or {}),
            paths=PathsConfig.from_dict(d.get("paths") or {}),
            imaging=ImagingConfig.from_dict(d.get("imaging") or {}),
            camera=CameraConfig.from_dict(d.get("camera") or {}),
            api=ApiConfig.from_dict(d.get("api") or {}),
            raw=copy.deepcopy(d),
        )
        cfg._validate_cross_section()
        return cfg

    def _validate_cross_section(self) -> None:
        """Checks that need more than one section."""
        # Anchors must sit above the board, otherwise the gondola cannot hang.
        top = self.board.height_mm
        for name, (_, ay) in (("anchor_left", self.geometry.anchor_left),
                              ("anchor_right", self.geometry.anchor_right)):
            if ay <= top:
                raise ConfigError(
                    f"geometry.{name}: y={ay} must be above the board top edge "
                    f"({top}); the gondola hangs below the anchors")
        # Revolver must have a position for every configured colour.
        if len(self.servo.revolver.positions) < len(self.colors):
            raise ConfigError(
                f"servo.revolver.positions has {len(self.servo.revolver.positions)} "
                f"entries but {len(self.colors)} colours are configured")
        # Park position must be on the board.
        px, py = self.motion.park_position
        if not (0.0 <= px <= self.board.width_mm and 0.0 <= py <= self.board.height_mm):
            raise ConfigError(
                f"motion.park_position {self.motion.park_position} is outside the "
                f"board (0..{self.board.width_mm} x 0..{self.board.height_mm})")
        # Draw feed must not exceed travel feed (would be a config typo).
        if self.motion.draw_feed_mm_min > self.motion.travel_feed_mm_min:
            raise ConfigError(
                "motion.draw_feed_mm_min must not exceed motion.travel_feed_mm_min")
        # Pulley radius must be smaller than the shortest possible reach.
        r = self.geometry.pulley_pitch_radius_mm
        min_reach = min(
            math.hypot(px - ax, py - ay)
            for (ax, ay) in (self.geometry.anchor_left, self.geometry.anchor_right)
            for px in (0.0, self.board.width_mm)
            for py in (0.0, self.board.height_mm)
        )
        if r >= min_reach:
            raise ConfigError(
                f"geometry.pulley_pitch_radius_mm ({r}) is larger than the closest "
                f"board corner distance ({min_reach:.1f}); tangent kinematics undefined")

    def to_dict(self) -> dict:
        """Plain-Python view suitable for YAML/JSON."""
        def conv(value: Any) -> Any:
            if dataclasses.is_dataclass(value) and not isinstance(value, type):
                return {f.name: conv(getattr(value, f.name))
                        for f in dataclasses.fields(value) if f.name != "raw"}
            if isinstance(value, tuple):
                return [conv(v) for v in value]
            if isinstance(value, list):
                return [conv(v) for v in value]
            if isinstance(value, dict):
                return {k: conv(v) for k, v in value.items()}
            return value
        return conv(self)

    def replace(self, **overrides: Any) -> "MachineConfig":
        """Return a new config with a deep-merged overlay dict.

        ``cfg.replace(geometry={'precompensate': True})``
        """
        merged = _deep_merge(self.raw, overrides)
        return MachineConfig.from_dict(merged)


# --------------------------------------------------------------------------
# loading / saving
# --------------------------------------------------------------------------

def _read_yaml(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def load_config(machine_path: str | Path | None = None,
                calibration_path: str | Path | None = None,
                *,
                overrides: dict | None = None) -> MachineConfig:
    """Load machine.yaml, merge calibration.yaml and any explicit overrides.

    Precedence (lowest first): machine.yaml < calibration.yaml < overrides.
    A missing calibration file is not an error — it simply means the machine
    has not been calibrated yet.
    """
    cfg_dir = default_config_dir()
    m_path = Path(machine_path) if machine_path else cfg_dir / "machine.yaml"
    data = _read_yaml(m_path)

    c_path = Path(calibration_path) if calibration_path else cfg_dir / "calibration.yaml"
    if c_path.exists():
        calib = _read_yaml(c_path)
        # The calibration file may wrap its values in a "machine:" key alongside
        # its own report metadata; accept both shapes.
        payload = calib.get("machine") if isinstance(calib.get("machine"), dict) else calib
        payload = {k: v for k, v in payload.items()
                   if k not in ("report", "measurements", "solved_at", "residuals")}
        data = _deep_merge(data, payload)

    if overrides:
        data = _deep_merge(data, overrides)

    return MachineConfig.from_dict(data)


def to_plain(value: Any) -> Any:
    """Strip numpy scalars/arrays so a payload is YAML- and JSON-safe.

    numpy floats satisfy ``isinstance(x, float)`` nowhere useful and make
    ``yaml.safe_dump`` raise, which would only surface at the moment the user
    finally clicks "save calibration".
    """
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_plain(v) for v in value]
    if hasattr(value, "item") and hasattr(value, "dtype"):   # numpy scalar
        return to_plain(value.item())
    if hasattr(value, "tolist") and hasattr(value, "shape"):  # numpy array
        return to_plain(value.tolist())
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    return str(value)


def save_calibration(payload: dict, calibration_path: str | Path | None = None) -> Path:
    """Write the calibration overlay atomically.

    Returns the path written.
    """
    path = Path(calibration_path) if calibration_path \
        else default_config_dir() / "calibration.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = yaml.safe_dump(to_plain(payload), sort_keys=False, allow_unicode=True,
                          default_flow_style=False)
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path
