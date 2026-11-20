"""'$' system commands of the emulated FluidNC ($I, $G, $CD, $X, $10=, ...)."""
from __future__ import annotations

import yaml

from ..servo import ServoModel
from .gcode import ERR_INVALID_STATEMENT, ERR_NO_HOMING, ERR_NOT_IDLE

VERSION = "3.9.9"
BANNER = "Grbl 3.9 [FluidNC v3.9.9 (wifi) '$' for help]"


def config_dump(emu) -> str:
    """Minimal ``$CD`` output in the key layout of the real FluidNC config."""
    cfg = emu.cfg
    spm = float(cfg.steps_per_mm)

    def xy_axis(step: str, direction: str) -> dict:
        return {"steps_per_mm": spm, "max_rate_mm_per_min": float(cfg.firmware.max_rate_mm_min),
                "acceleration_mm_per_sec2": float(cfg.motion.acceleration_mm_s2), "max_travel_mm": 2700.0,
                "soft_limits": False, "homing": {"cycle": 0},
                "motor0": {"stepstick": {"step_pin": step, "direction_pin": direction}}}

    def servo_axis(spec) -> dict:
        m = ServoModel(spec)
        return {"steps_per_mm": 1.0, "max_rate_mm_per_min": 600000.0, "acceleration_mm_per_sec2": 1000000.0,
                "max_travel_mm": float(spec.axis_travel), "soft_limits": False,
                "homing": {"cycle": 0, "positive_direction": False, "mpos_mm": float(spec.axis_min)},
                "motor0": {"rc_servo": {"output_pin": spec.pin, "pwm_hz": 50,
                                        "min_pulse_us": m.fw_min_us, "max_pulse_us": m.fw_max_us}}}

    al, ar = cfg.fw_anchor_left, cfg.fw_anchor_right
    axes: dict = {"shared_stepper_disable_pin": "I2SO.0",
                  "x": xy_axis("I2SO.1", "I2SO.2"), "y": xy_axis("I2SO.5", "I2SO.6")}
    axes[cfg.servos.pen.axis.lower()] = servo_axis(cfg.servos.pen)
    if cfg.servos.revolver.enabled:
        axes[cfg.servos.revolver.axis.lower()] = servo_axis(cfg.servos.revolver)
    dump = {
        "name": "wbot simulator", "board": "MKS DLC32 V2.1 (simulated)",
        "stepping": {"engine": "I2S_STATIC", "idle_ms": 255, "pulse_us": 4, "dir_delay_us": 1, "disable_delay_us": 0},
        "kinematics": {"WallPlotter": {
            "left_axis": 0, "left_anchor_x": float(al[0]), "left_anchor_y": float(al[1]),
            "right_axis": 1, "right_anchor_x": float(ar[0]), "right_anchor_y": float(ar[1]),
            "segment_length": float(cfg.firmware.segment_length_mm)}},
        "axes": axes,
        "start": {"must_home": False, "deactivate_parking": True},
        "arc_tolerance_mm": 0.002, "junction_deviation_mm": float(cfg.motion.junction_deviation_mm),
        "verbose_errors": True, "report_inches": False, "use_line_numbers": False,
    }
    return yaml.safe_dump(dump, sort_keys=False, default_flow_style=False)


class SysCmdMixin:
    """Needs: ``_emit``, ``_ok``, ``_error``, ``_state_str``, ``_alarm``, ``_hold``, ``_do_jog``, modal fields."""

    def _sys_command(self, s: str) -> None:
        body = s[1:].strip()
        low = body.lower()
        state = self._state_str()
        if low[:2] == "j=":
            self._do_jog(body[2:])
        elif low == "":
            self._emit("[HLP:$$ $+ $# $S $L $G $I $N $x=val $Nx=line $J=line $SLP $C $X $H ~ ! ? ctrl-x]")
            self._ok()
        elif low == "x":
            if state not in ("Idle", "Alarm"):
                self._error(ERR_NOT_IDLE)
                return
            if self._alarm is not None:
                self._alarm = None
                self._emit("[MSG:Caution: Unlocked]")
            self._ok()
        elif low == "i":
            self._emit(f"[VER:3.9 FluidNC v{VERSION}:]", f"[OPT:PH,{self.planner.capacity},{self._rx_cap}]")
            self._ok()
        elif low == "g":
            self._emit(self._gc_modes())
            self._ok()
        elif low in ("cd", "config/dump"):
            self._emit(*config_dump(self).rstrip("\n").split("\n"))
            self._ok()
        elif low.startswith("10="):
            if state not in ("Idle", "Alarm"):
                self._error(ERR_NOT_IDLE)
                return
            try:
                self._status_mask = int(float(low[3:]))
            except ValueError:
                self._error(2)
                return
            self._ok()
        elif low == "h":
            self._error(ERR_NO_HOMING)
        else:
            self._error(ERR_INVALID_STATEMENT)

    def _gc_modes(self) -> str:
        motion = "G1" if self._m_motion == 1 else "G0"
        units = "G20" if self._m_scale != 1.0 else "G21"
        dist = "G90" if self._m_abs else "G91"
        return f"[GC:{motion} G54 G17 {units} {dist} G94 M5 M9 T0 F{self._m_feed:g} S0]"
