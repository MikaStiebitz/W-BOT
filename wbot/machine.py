"""Machine controller: connection, reference, safe motion, servos, jobs, e-stop.

Everything that moves the machine passes through :meth:`Machine._send_lines`, which
runs the independent G-code validator (``wbot.safety``) first.
All public methods are thread-safe and return plain JSON-able data.
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable


from .errors import MachineError
from .machine_motion import MotionMixin
from .machine_reference import ReferenceMixin
from .machine_sim import SimMixin
from .config import Config, deep_update, dump_config, load_config, save_config
from .kinematics import KinematicsError, Mapping
from .safety import PEN_TOL_DEG, pen_state_after, validate_gcode
from .servo import ServoModel

log = logging.getLogger(__name__)
STATE_DIR = Path(__file__).resolve().parent.parent / "config" / "state"
REFERENCE_FILE = STATE_DIR / "reference.json"
FAKE_REFERENCE_FILE = STATE_DIR / "reference-fakeboard.json"   # loopback boards (sim-server) never touch the real one
LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
FW_MATCH_MM = 0.05
WIFI_WATCHDOG_S = 8.0      # WiFi stalls longer than a cable; link loss still aborts jobs (emergency stop)


class Machine(ReferenceMixin, MotionMixin, SimMixin):
    def __init__(self, cfg: Config | None = None, listener: Callable[[dict], None] | None = None,
                 config_path: str | Path | None = None, persist: bool = False):
        self.persist = persist                  # write config changes to disk (CLI/server only)
        self.config_path = Path(config_path) if config_path else None
        self.cfg = cfg or load_config(self.config_path)
        self.listener = listener
        self._lock = threading.RLock()          # protects connection/reference fields
        self._cmd_lock = threading.Lock()       # one motion command at a time
        self.client = None
        self.link = None
        self.transport: str | None = None
        self.port: str | None = None
        self.host: str | None = None            # WiFi board host (None for sim/serial)
        self.protocol: str | None = None        # "telnet" | "websocket" (WiFi only)
        self.conn_status = "disconnected"
        self.conn_error: str | None = None
        self.firmware_name: str | None = None
        self.mapping: Mapping | None = None
        self.reference: dict | None = None      # {physical, firmware, restored, uncertain}
        self.warnings: list[str] = []
        self.jobs = None                        # JobManager (set lazily, see jobs.py)
        self._sim_trace_sent = 0
        self._last_ref_save = 0.0
        self.pen_known = False                  # pen position commanded since connect/reset
        self.sim_opts: dict = {}
        self.camera = None                      # CameraService (set by the web app, see wbot/camera)

    @contextlib.contextmanager
    def _cmd(self):
        """One motion command at a time. Refuses (instead of silently queueing behind a running
        job) so that a UI click or an AI tool call gets an immediate, clear answer."""
        if self.jobs and self.jobs.running:
            raise MachineError("A job is running - pause or cancel it first.", "job_running")
        if not self._cmd_lock.acquire(blocking=False):
            raise MachineError("The machine is busy - please wait a moment.", "busy")
        try:
            yield
        finally:
            self._cmd_lock.release()

    # ------------------------------------------------------------------ events
    def _emit(self, msg: dict) -> None:
        cb = self.listener
        if cb:
            try:
                cb(msg)
            except Exception:  # noqa: BLE001
                log.exception("listener failed")

    def log(self, level: str, text: str) -> None:
        self._emit({"type": "log", "level": level, "text": text, "t": time.time()})
        getattr(log, "warning" if level == "warn" else "info" if level != "error" else "error")(text)

    def _warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)
        self.log("warn", text)

    # -------------------------------------------------------------- connection
    @staticmethod
    def list_ports() -> list[dict]:
        from .fluidnc.serial_link import list_ports
        return [{"device": p["device"], "description": p.get("description", "")} for p in list_ports()]

    def connect(self, transport: str | None = None, port: str | None = None,
                baudrate: int | None = None, sim: dict | None = None,
                host: str | None = None, protocol: str | None = None) -> dict:
        with self._lock:
            if self.client is not None and self.client.state.link != "open":
                self.disconnect()                # link was lost: clean up, then reconnect
            if self.client is not None:
                raise MachineError("Already connected - disconnect first.", "already_connected")
            transport = transport or self.cfg.connection.transport
            self.conn_status, self.conn_error = "connecting", None
        try:
            if transport == "sim":
                from .sim import make_sim_link
                link = make_sim_link(self.cfg, **self._sim_kwargs(sim or {}))
                self.port = "Simulator"
            elif transport == "serial":
                from .fluidnc.serial_link import SerialLink
                port = port or self.cfg.connection.serial.port
                if not port:
                    raise MachineError("No COM port selected.", "no_port", 400)
                link = SerialLink(port, baudrate or self.cfg.connection.serial.baudrate)
                self.port = port
            elif transport == "wifi":
                link = self._wifi_link(host, protocol, port)
            else:
                raise MachineError(f"Unknown transport '{transport}'.", "bad_transport", 400)
            from .fluidnc.client import FluidNCClient
            wifi = transport == "wifi"
            client = FluidNCClient(link, rx_buffer_bytes=self.cfg.connection.serial.rx_buffer_bytes,
                                   status_hz=self.cfg.connection.status_poll_hz,
                                   listener=self._on_client_event,
                                   watchdog_s=WIFI_WATCHDOG_S if wifi else 5.0)
            info = client.connect(timeout=10.0 if wifi else 8.0)
        except MachineError:
            self.conn_status = "disconnected"
            self.host = self.protocol = None
            raise
        except Exception as exc:  # noqa: BLE001
            self.conn_status, self.conn_error = "error", str(exc)
            self.host = self.protocol = None
            self.log("error", f"Connection failed: {exc}")
            raise MachineError(f"Connection failed: {exc}", "connect_failed", 502) from exc
        with self._lock:
            self.client, self.link, self.transport = client, link, transport
            self.conn_status = "connected"
            self.firmware_name = getattr(info, "firmware", None) or getattr(info, "version", None)
            self.mapping, self.reference = None, None
            self._sim_trace_sent = 0
            self.pen_known = False
            self.sim_opts = dict(sim or {})
        self._emit({"type": "trace", "data": {"clear": True}})
        label = f"{transport}/{self.protocol}" if transport == "wifi" else transport
        self.log("info", f"Connected via {label} ({self.port}).")
        self._check_axes()
        self._try_restore_reference()
        return self.state()

    def _wifi_link(self, host: str | None, protocol: str | None, port: Any):
        """Link to a FluidNC board over WiFi; sets ``self.port`` to ``host:port`` for the state."""
        from .fluidnc.link import LinkError
        from .fluidnc.wifi import default_port, make_board_link, normalize_protocol
        w = self.cfg.connection.wifi
        host = (host or w.host or "").strip()
        if not host:
            raise MachineError("No host specified for the WiFi connection.", "no_host", 400)
        try:
            proto = normalize_protocol(protocol or w.protocol)
            if port in (None, "", 0):
                p = w.effective_port if proto == w.protocol else default_port(proto)
            else:
                p = int(port)
            link = make_board_link(proto, host, p)
        except (LinkError, ValueError) as exc:
            raise MachineError(f"WiFi connection not possible: {exc}", "bad_wifi", 400) from exc
        self.host, self.protocol = host, proto
        self.port = f"{host}:{p}"
        return link

    def _sim_kwargs(self, sim: dict) -> dict:
        kw: dict[str, Any] = {}
        self.sim_speed = float(sim.get("speed") or 1.0)          # real-time factor of the simulator (estimates divide by it)
        if "speed" in sim and sim["speed"]:
            kw["speed"] = float(sim["speed"])
        kw["physical_start"] = tuple(sim.get("physical_start") or self.cfg.firmware.boot_physical)
        if sim.get("perturb"):
            kw["true_overrides"] = dict(sim["perturb"])
        return kw

    def _check_axes(self) -> None:
        st = self.client.state
        n = len(st.mpos or ())
        want = 4 if self.cfg.servos.revolver.enabled else 3
        if n and n < want:
            self._warn(f"Firmware reports only {n} axes, expected {want} "
                       "(revolver servo missing from the FluidNC config?).")

    def disconnect(self) -> dict:
        with self._lock:
            client, self.client = self.client, None
            self.link = None
        if self.jobs:
            self.jobs.abort_on_disconnect()
        if client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                log.exception("close failed")
        with self._lock:
            self.conn_status, self.transport, self.port = "disconnected", None, None
            self.host = self.protocol = None
            self.mapping, self.reference = None, None
        self._emit({"type": "trace", "data": {"clear": True}})
        self.log("info", "Disconnected.")
        return self.state()

    def shutdown(self) -> None:
        try:
            self.disconnect()
        except Exception:  # noqa: BLE001
            pass

    def _on_client_event(self, ev: Any) -> None:
        name = type(ev).__name__
        if name == "Traffic":
            self.log(ev.direction, str(ev.text))
        elif name == "Message":
            kind = str(getattr(ev, "kind", "")).upper()
            self.log("error" if kind == "ERR" else "info", str(ev.text))
        elif name == "Alarm":
            self.log("warn", f"ALARM:{ev.code} {ev.text}")
        elif name == "Banner":
            self.log("info", str(ev.text))
        elif name == "LinkEvent":
            if ev.state == "lost":
                self.conn_status, self.conn_error = "error", "Connection lost"
                self.log("error", f"Connection to the controller lost ({getattr(ev, 'reason', '')}) - emergency stop.")
                if self.reference:
                    self.reference["uncertain"] = True
                if self.jobs:
                    self.jobs.link_lost()
        elif name == "Status":
            self._on_status()

    def _on_status(self) -> None:
        self._pump_sim_trace()
        now = time.monotonic()
        if self.mapping is not None and now - self._last_ref_save > 1.0:
            self._last_ref_save = now
            self._save_reference()

    # -------------------------------------------------------------------- state
    def _require_conn(self) -> None:
        if self.client is None or self.conn_status != "connected":
            raise MachineError("Not connected.", "not_connected")

    def fw_xy(self) -> tuple[float, float] | None:
        if self.client is None:
            return None
        mpos = self.client.state.mpos
        return (float(mpos[0]), float(mpos[1])) if mpos and len(mpos) >= 2 else None

    def phys_xy(self) -> tuple[float, float] | None:
        fw = self.fw_xy()
        if fw is None or self.mapping is None:
            return None
        try:
            p = self.mapping.to_physical(fw)
            return float(p[0]), float(p[1])
        except KinematicsError:
            return None

    def outside_safe_rect(self) -> bool:
        ph = self.phys_xy()
        if ph is None:
            return False
        xmin, ymin, xmax, ymax = self.cfg.safe_rect()
        return not (xmin - 0.25 <= ph[0] <= xmax + 0.25 and ymin - 0.25 <= ph[1] <= ymax + 0.25)

    def axis(self, name: str) -> float | None:
        if self.client is None:
            return None
        mpos = self.client.state.mpos or ()
        idx = {"Z": 2, "A": 3}[name]
        return float(mpos[idx]) if len(mpos) > idx else None

    def pen_angle(self) -> float | None:
        z = self.axis(self.cfg.servos.pen.axis)
        return None if z is None else ServoModel(self.cfg.servos.pen).angle_for_axis(z)

    def revolver_angle(self) -> float | None:
        if not self.cfg.servos.revolver.enabled:
            return None
        a = self.axis(self.cfg.servos.revolver.axis)
        return None if a is None else ServoModel(self.cfg.servos.revolver).angle_for_axis(a)

    def pen_up(self) -> bool | None:
        s = pen_state_after(self.axis(self.cfg.servos.pen.axis), self.cfg)
        return {"up": True, "down": False}.get(s)

    def revolver_slot(self) -> int | None:
        ang = self.revolver_angle()
        if ang is None:
            return None
        for i, s in enumerate(self.cfg.servos.revolver.slots_deg):
            if abs(ang - s) <= PEN_TOL_DEG:
                return i + 1
        return None

    def machine_state(self) -> tuple[str, dict | None]:
        if self.client is None:
            return "disconnected", None
        st = self.client.state
        if st.link == "lost":
            return "disconnected", None
        name = (st.machine_state or "Idle").lower().split(":")[0]
        name = {"hold": "hold", "jog": "jog", "alarm": "alarm", "door": "door", "check": "check",
                "run": "run", "idle": "idle", "home": "run", "sleep": "idle"}.get(name, "idle")
        alarm = None
        if st.alarm:
            alarm = {"code": st.alarm[0], "text": st.alarm[1]}
        return name, alarm

    def state(self) -> dict:
        ms, alarm = self.machine_state()
        fw, ph = self.fw_xy(), self.phys_xy()
        pen_a, rev_a = self.pen_angle(), self.revolver_angle()
        pen, rev = ServoModel(self.cfg.servos.pen), ServoModel(self.cfg.servos.revolver)
        axes = {}
        for ax in ("Z", "A"):
            v = self.axis(ax)
            if v is not None:
                axes[ax] = round(v, 3)
        ref = self.reference
        return {
            "connection": {"status": self.conn_status, "transport": self.transport,
                           "port": self.port, "host": self.host, "protocol": self.protocol, "firmware": self.firmware_name, "error": self.conn_error},
            "machine": {"state": ms, "alarm": alarm,
                        "busy": bool(self.jobs and self.jobs.running) or self._cmd_lock.locked()},
            "position": {"firmware": list(fw) if fw else None,
                         "physical": [round(ph[0], 3), round(ph[1], 3)] if ph else None, "axes": axes},
            "reference": {"set": self.mapping is not None,
                          "physical": ref["physical"] if ref else None,
                          "firmware": ref["firmware"] if ref else None,
                          "boot_physical": list(self.cfg.firmware.boot_physical),
                          "restored": bool(ref and ref.get("restored")),
                          "uncertain": bool(ref and ref.get("uncertain"))},
            "pen": {"angle": None if pen_a is None else round(pen_a, 2), "up": self.pen_up(),
                    "known": self.pen_known},
            "revolver": {"angle": None if rev_a is None else round(rev_a, 2),
                         "slot": self.revolver_slot()},
            "limits": {"safe_rect": list(self.cfg.safe_rect()),
                       "pen": [round(pen.min_deg, 2), round(pen.max_deg, 2)],
                       "revolver": [round(rev.min_deg, 2), round(rev.max_deg, 2)]},
            "job": self.jobs.info() if self.jobs else None,
            "warnings": list(self.warnings),
        }

    # ----------------------------------------------------------------- guards
    def _ready_for_motion(self, *, allow_busy_job: bool = False) -> None:
        self._require_conn()
        if self.jobs and self.jobs.running and not allow_busy_job:
            raise MachineError("A job is running - pause or cancel it first.", "job_running")
        ms, alarm = self.machine_state()
        if ms == "alarm":
            raise MachineError("Controller in alarm - unlock it first.", "alarm")
        if ms == "hold":
            raise MachineError("Machine paused (Hold) - resume or cancel.", "hold")

    def _send_lines(self, lines: list[str], *, wait: bool = True, timeout: float = 120.0) -> None:
        """Validate, stream, wait for completion. The single gate to the hardware."""
        problems = validate_gcode(lines, self.cfg, self.mapping,
                                  z_axis=self.axis(self.cfg.servos.pen.axis),
                                  allow_unreferenced_xy=False, start_outside=self.outside_safe_rect())
        if problems:
            p = problems[0]
            self.log("error", f"G-code rejected: {p.message} ({p.line!r})")
            raise MachineError(f"Safety check: {p.message}", "unsafe", 422)
        res = self.client.stream(lines, stop_on_error=True)
        if not getattr(res, "ok", True):
            raise MachineError(f"Controller reports an error: {getattr(res, 'message', '') or getattr(res, 'status', 'unknown')}", "controller_error")
        if wait and not self.client.wait_idle(timeout=timeout):
            raise MachineError("Timed out waiting for the motion to finish.", "timeout", 504)

    # ------------------------------------------------------- control / e-stop
    def hold(self) -> dict:
        self._require_conn()
        self.client.feed_hold()
        if self.jobs:
            self.jobs.on_hold()
        return self.state()

    def resume(self) -> dict:
        self._require_conn()
        self.client.cycle_start()
        if self.jobs:
            self.jobs.on_resume()
        return self.state()

    def cancel(self) -> dict:
        self._require_conn()
        if self.jobs and self.jobs.running:
            self.jobs.cancel()
        else:
            self.client.feed_hold()
        return self.state()

    def estop(self) -> dict:
        """Immediate stop. Does not take the command lock: must always work."""
        if self.client is not None:
            self.client.emergency_stop()
        if self.jobs:
            self.jobs.on_estop()
        if self.reference:
            self.reference["uncertain"] = True
        self._warn("EMERGENCY STOP triggered - check the position after the stop, re-reference if necessary.")
        return self.state()

    def unlock(self) -> dict:
        self._require_conn()
        if not self.client.unlock():
            raise MachineError("Unlock failed - the controller stays in alarm.", "unlock_failed")
        self.warnings = [w for w in self.warnings if "EMERGENCY STOP" not in w]
        return self.state()

    def reset(self) -> dict:
        self._require_conn()
        self.client.soft_reset()
        return self.state()

    def send_raw(self, line: str) -> list[str]:
        self._require_conn()
        line = line.strip()
        if not line:
            raise MachineError("Empty line.", "bad_request", 400)
        up = line.upper()
        if line.startswith("$") and not up.startswith(("$J=", "$X", "$H")):
            return self.client.query(line)        # $I, $G, $CD ... read-only queries / settings
        with self._cmd():
            self._ready_for_motion()
            problems = validate_gcode([line], self.cfg, self.mapping,
                                      z_axis=self.axis(self.cfg.servos.pen.axis),
                                      allow_unreferenced_xy=False)
            if problems:
                raise MachineError(f"Safety check: {problems[0].message}", "unsafe", 422)
            rep = self.client.send(line, timeout=30.0)
            return [getattr(rep, "text", None) or ("ok" if getattr(rep, "ok", False) else "error")]

    # ------------------------------------------------------------------- config
    def config_json(self) -> dict:
        return json.loads(self.cfg.model_dump_json())

    def update_config(self, patch: dict) -> dict:
        if self.client is not None and (self.jobs and self.jobs.running):
            raise MachineError("Not possible while a job is running.", "job_running")
        try:
            new = deep_update(self.cfg, patch)
        except Exception as exc:  # noqa: BLE001
            raise MachineError(f"Invalid configuration: {exc}", "bad_config", 422) from exc
        self.cfg = new
        if self.mapping is not None and self.reference:        # geometry may have changed
            self.mapping = Mapping.from_config(new, tuple(self.reference["physical"]),
                                               tuple(self.reference["firmware"]))
        if self.persist:
            try:
                save_config(new, self.config_path)
            except OSError as exc:
                self._warn(f"The configuration could not be saved: {exc}")
        return self.config_json()

    def firmware_config_text(self) -> str:
        from .fluidnc_config import generate_firmware_config
        return generate_firmware_config(self.cfg)

    # ---------------------------------------------------- drawings and jobs
    def _jobs(self):
        if self.jobs is None:
            from .jobs import JobManager
            self.jobs = JobManager(self)
        return self.jobs

    def patterns(self) -> list[dict]:
        return self._jobs().patterns()

    def preview(self, body: dict) -> dict:
        return self._jobs().preview(body)

    def compose(self, body: dict) -> dict:
        return self._jobs().compose(body)

    def start_job(self, body: dict) -> dict:
        if self.camera is not None:
            self.camera.check_clear(body)          # nobody standing at the board (if the camera guard is on)
        return self._jobs().start(body)

    def job_info(self) -> dict | None:
        return self.jobs.info() if self.jobs else None

    def import_svg(self, data: bytes, name: str) -> dict:
        return self._jobs().import_svg(data, name)

    def import_file(self, data: bytes, name: str, options: dict | None = None) -> dict:
        """SVG or raster image -> {drawing, info?, warnings?} (the kind is detected from the content)."""
        head = data[:2048].lstrip().lower()
        if head.startswith(b"<svg") or head.startswith(b"<?xml") or b"<svg" in head:
            return self._jobs().import_svg(data, name)
        return self._jobs().import_image(data, name, options)

    @property
    def dump(self) -> str:
        return dump_config(self.cfg)
