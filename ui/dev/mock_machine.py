"""Erfundene, aber plausible Maschine fuer den UI-Mock-Server (keine echte Hardware!)."""
from __future__ import annotations

import asyncio
import copy
import math
import os
import time
from collections import deque

import mock_patterns as pat

SPEEDUP = float(os.environ.get("MOCK_SPEED", "3"))   # Bewegungen laufen so viel schneller
TICK = 0.05


class ApiErr(Exception):
    def __init__(self, status: int, msg: str, code: str):
        super().__init__(msg)
        self.status, self.msg, self.code = status, msg, code


def deep_merge(a, b):
    out = copy.deepcopy(a)
    for k, v in b.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class MockMachine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.listeners: set[asyncio.Queue] = set()
        self.logs: deque = deque(maxlen=300)
        self.job = None
        self._job_seq = 0
        self._task: asyncio.Task | None = None
        self.paused = False
        self._reset_runtime()

    # ------------------------------------------------------------------ basics
    def _reset_runtime(self):
        self.status, self.transport, self.port, self.firmware, self.error = "disconnected", None, None, None, None
        self.alarm = None
        self.busy_kind = None            # None | "run" | "jog"
        self.fw = [0.0, 0.0]
        self.ref = None
        self.truth = [0.0, 0.0]
        self.pen = 0.0                   # Boot: Servo mittig -> Stift "unten"!
        self.rev = 0.0
        self.perturb: dict = {}
        self.trace: list = []
        self.err_n, self.err_sum, self.err_max = 0, 0.0, 0.0
        self.paused = False

    def emit(self, msg):
        for q in list(self.listeners):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def log(self, level, text):
        rec = {"type": "log", "level": level, "text": text, "t": time.time()}
        self.logs.append(rec)
        self.emit(rec)

    @property
    def sv(self):
        return self.cfg["servos"]

    def safe_rect(self):
        b = self.cfg["board"]
        m = b["margin_mm"]
        return [m, m, b["width_mm"] - m, b["height_mm"] - m]

    def belief(self):
        if not self.ref:
            return None
        r = self.ref
        return [self.fw[0] - r["firmware"][0] + r["physical"][0], self.fw[1] - r["firmware"][1] + r["physical"][1]]

    def _err(self, p):
        q = self.perturb
        if not q:
            return 0.0, 0.0
        ex = q.get("pen_dx", 0) + 0.5 * q.get("anchor_dx", 0) + (q.get("scale_left", 1) - 1) * (p[0] - 750) * 0.6
        ey = (q.get("pen_dy", 0) + 0.7 * q.get("anchor_dy", 0) + (q.get("scale_right", 1) - 1) * (p[1] - 500) * 0.6
              + (q.get("radius", 12.732) - 12.732) * 0.4)
        return ex, ey

    def _pen_down(self):
        return self.pen < (self.sv["pen"]["up_deg"] + self.sv["pen"]["down_deg"]) / 2

    def _slot_index(self):
        for i, s in enumerate(self.sv["revolver"]["slots_deg"]):
            if abs(self.rev - s) < 1.0:
                return i
        return None

    def mstate(self):
        if self.status != "connected":
            return "disconnected"
        if self.alarm:
            return "alarm"
        if self.paused and self.busy_kind:
            return "hold"
        return self.busy_kind or "idle"

    # ------------------------------------------------------------------ snapshots
    def snapshot(self):
        b = self.belief()
        si = self._slot_index()
        return {
            "connection": {"status": self.status, "transport": self.transport, "port": self.port,
                           "firmware": self.firmware, "error": self.error},
            "machine": {"state": self.mstate(), "alarm": self.alarm, "busy": self.busy_kind is not None},
            "position": {"firmware": [round(self.fw[0], 3), round(self.fw[1], 3)],
                         "physical": [round(b[0], 3), round(b[1], 3)] if b else None,
                         "axes": {"Z": round(self.pen, 2), "A": round(90 + self.rev, 2)}},
            "reference": {"set": self.ref is not None,
                          "physical": self.ref["physical"] if self.ref else None,
                          "firmware": self.ref["firmware"] if self.ref else None,
                          "restored": False, "uncertain": False},
            "pen": {"angle": round(self.pen, 2), "up": not self._pen_down()},
            "revolver": {"angle": round(self.rev, 2), "slot": si + 1 if si is not None else None},
            "limits": {"safe_rect": self.safe_rect(), "pen": [-87.8, 87.8], "revolver": [-90, 90]},
            "job": self.job_info(), "warnings": [],
        }

    def job_info(self):
        if not self.job:
            return None
        j = dict(self.job)
        if j["state"] in ("running", "paused", "planning"):
            j["elapsed_s"] = round(time.time() - j["_t0"], 1)
        j.pop("_t0", None)
        return j

    def sim_info(self):
        if self.status != "connected" or self.transport != "sim":
            return None
        b = self.belief()
        err = math.dist(self.truth, b) if b else None
        return {"physical": [round(v, 3) for v in self.truth], "intended": [round(v, 3) for v in b] if b else None,
                "error_mm": round(err, 3) if err is not None else None,
                "error_max_mm": round(self.err_max, 3),
                "error_mean_mm": round(self.err_sum / self.err_n, 3) if self.err_n else None,
                "perturb": self.perturb, "trace_segments": len(self.trace), "trace": self.trace[-4000:]}

    # ------------------------------------------------------------------ connection
    async def connect(self, body):
        if self.status in ("connected", "connecting"):
            raise ApiErr(409, "Bereits verbunden. Erst trennen.", "already_connected")
        transport = body.get("transport") or "sim"
        port = body.get("port")
        if transport == "serial" and not port:
            raise ApiErr(400, "Kein serieller Port gewählt.", "no_port")
        self._reset_runtime()
        self.status = "connecting"
        self.emit({"type": "state", "data": self.snapshot()})
        await asyncio.sleep(0.7)
        if transport == "serial" and port == "COM7":
            self.status, self.error = "error", "Port COM7 konnte nicht geöffnet werden (Zugriff verweigert)."
            raise ApiErr(502, self.error, "port_busy")
        self.status, self.transport, self.port = "connected", transport, port if transport == "serial" else None
        self.firmware = "Grbl 3.9 [FluidNC v3.9.9 (MOCK)]"
        sim = body.get("sim") or {}
        self.perturb = dict(sim.get("perturb") or {})
        start = sim.get("physical_start") or [0.0, 0.0]
        self.truth = list(map(float, start))
        self.emit({"type": "trace", "data": {"clear": True}})
        for t in ("Grbl 3.9 [FluidNC v3.9.9 (MOCK) '$' for help]", "[MSG:INFO: Kinematic system: WallPlotter]",
                  "[MSG:INFO: Axis Z/A: Servo-Achsen aktiv]"):
            self.log("rx", t)
        self.log("info", f"Verbunden ({transport}{' ' + port if port else ''}). Keine Referenz – Position unbekannt.")
        return self.snapshot()

    async def disconnect(self):
        self._cancel_task()
        keep = self.logs
        self._reset_runtime()
        self.job = None
        self.logs = keep
        self.log("info", "Getrennt.")
        self.emit({"type": "trace", "data": {"clear": True}})
        return self.snapshot()

    def require_connected(self):
        if self.status != "connected":
            raise ApiErr(409, "Nicht verbunden. Zuerst oben verbinden.", "not_connected")
        if self.alarm:
            raise ApiErr(409, "Alarm aktiv. Erst entsperren ($X) oder zurücksetzen.", "alarm")

    def require_idle(self):
        self.require_connected()
        if self.busy_kind or (self._task and not self._task.done()):
            raise ApiErr(409, "Die Maschine ist beschäftigt. Warten, pausieren oder abbrechen.", "busy")

    async def set_reference(self, x, y):
        self.require_idle()
        self.ref = {"physical": [float(x), float(y)], "firmware": list(self.fw)}
        self.truth = [x + self._err([x, y])[0], y + self._err([x, y])[1]]
        self.log("info", f"Referenz gesetzt: physisch ({x:.1f} | {y:.1f}) = Firmware ({self.fw[0]:.2f} | {self.fw[1]:.2f}).")
        return self.snapshot()

    # ------------------------------------------------------------------ motion
    def _cancel_task(self):
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self.busy_kind = None
        self.paused = False

    async def _checkpoint(self):
        while self.paused:
            await asyncio.sleep(0.05)

    def _advance(self, new_fw):
        old_t = list(self.truth)
        self.fw = new_fw
        base = self.belief() or new_fw
        ex, ey = self._err(base)
        self.truth = [base[0] + ex, base[1] + ey]
        if self.ref:
            e = math.dist(self.truth, base)
            self.err_n += 1
            self.err_sum += e
            self.err_max = max(self.err_max, e)
        si = self._slot_index()
        seg = [round(old_t[0], 2), round(old_t[1], 2), round(self.truth[0], 2), round(self.truth[1], 2),
               self._pen_down(), si if si is not None else 1]
        self.trace.append(seg)
        if len(self.trace) > 30000:
            del self.trace[:5000]
        self.emit({"type": "trace", "data": {"seg": seg}})

    async def _move_fw(self, target, feed):
        x0, y0 = self.fw
        dx, dy = target[0] - x0, target[1] - y0
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return
        v = max(feed, 60.0) / 60.0 * SPEEDUP
        n = max(1, int(dist / (v * TICK)))
        for i in range(1, n + 1):
            await self._checkpoint()
            self._advance([x0 + dx * i / n, y0 + dy * i / n])
            await asyncio.sleep(TICK)

    def _phys_to_fw(self, p):
        r = self.ref
        return [p[0] - r["physical"][0] + r["firmware"][0], p[1] - r["physical"][1] + r["firmware"][1]]

    async def _servo(self, which, angle, extra=0.0):
        key = "pen" if which == "pen" else "revolver"
        cur = self.pen if which == "pen" else self.rev
        wait = abs(angle - cur) / self.sv[key]["speed_deg_s"] + self.sv[key]["settle_s"] + extra
        await asyncio.sleep(wait / min(SPEEDUP, 3))
        if which == "pen":
            self.pen = angle
        else:
            self.rev = angle
        self.log("tx", f"G1 {'Z' if which == 'pen' else 'A'}{(angle if which == 'pen' else 90 + angle):.1f} F{self.sv[key]['speed_deg_s'] * 60:.0f}")
        self.log("rx", "ok")

    async def _pen_up(self):
        if self._pen_down():
            await self._servo("pen", self.sv["pen"]["up_deg"])

    def _spawn(self, coro, kind):
        self.busy_kind = kind

        async def wrap():
            try:
                await coro
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log("error", f"Mock-Fehler: {e}")
            finally:
                if asyncio.current_task() is self._task:
                    self.busy_kind = None
                    self.paused = False
        self._task = asyncio.create_task(wrap())

    def _check_target(self, x, y):
        r = self.safe_rect()
        if not (r[0] <= x <= r[2] and r[1] <= y <= r[3]):
            raise ApiErr(400, f"Ziel ({x:.1f} | {y:.1f}) liegt außerhalb des erlaubten Bereichs "
                              f"(X {r[0]:g}…{r[2]:g}, Y {r[1]:g}…{r[3]:g} mm).", "out_of_range")

    async def jog(self, dx, dy, feed):
        self.require_idle()
        feed = feed or self.cfg["motion"]["jog_feed_mm_min"]
        b = self.belief()
        if b:
            self._check_target(b[0] + dx, b[1] + dy)
        else:
            self.log("warn", "Jog ohne Referenz: Fahrt in Firmware-Koordinaten.")
        tgt = [self.fw[0] + dx, self.fw[1] + dy]
        self.log("tx", f"$J=G91 X{dx:g} Y{dy:g} F{feed:g}")
        self._spawn(self._move_fw(tgt, feed), "jog")
        return {"ok": True}

    async def goto(self, x, y, feed, pen):
        self.require_idle()
        if not self.ref:
            raise ApiErr(409, "Keine Referenz gesetzt. Gondel auf eine bekannte Position stellen und Referenz setzen.", "no_reference")
        self._check_target(x, y)
        feed = feed or self.cfg["motion"]["travel_feed_mm_min"]

        async def go():
            if pen != "keep":
                await self._pen_up()
            self.log("tx", f"G1 X{x:.2f} Y{y:.2f} F{feed:g}")
            await self._move_fw(self._phys_to_fw([x, y]), feed)
            self.log("rx", "ok")
        self._spawn(go(), "run")
        return {"ok": True}

    async def park(self):
        px, py = self.cfg["motion"]["park_position"]
        return await self.goto(px, py, None, "up")

    async def set_pen(self, state, angle):
        self.require_connected()
        lim = 87.8
        if state in ("up", "down"):
            angle = self.sv["pen"]["up_deg" if state == "up" else "down_deg"]
        if angle is None:
            raise ApiErr(400, "Weder Zustand noch Winkel angegeben.", "bad_request")
        if abs(angle) > lim:
            raise ApiErr(400, f"Winkel {angle:g}° außerhalb der Servo-Grenzen (±{lim:g}°).", "out_of_range")
        await self._servo("pen", float(angle))
        return self.snapshot()

    async def set_revolver(self, slot, angle):
        self.require_connected()
        slots = self.sv["revolver"]["slots_deg"]
        if slot is not None:
            if not 1 <= int(slot) <= len(slots):
                raise ApiErr(400, f"Slot {slot} existiert nicht (1…{len(slots)}).", "bad_slot")
            angle = slots[int(slot) - 1]
        if angle is None:
            raise ApiErr(400, "Weder Slot noch Winkel angegeben.", "bad_request")
        if abs(angle) > 90:
            raise ApiErr(400, "Revolver-Winkel außerhalb ±90°.", "out_of_range")
        await self._pen_up()
        await self._servo("revolver", float(angle))
        return self.snapshot()

    async def set_servo_raw(self, servo, angle):
        if servo == "pen":
            return await self.set_pen(None, angle)
        if servo == "revolver":
            return await self.set_revolver(None, angle)
        raise ApiErr(400, "Unbekannter Servo (pen|revolver).", "bad_request")

    # ------------------------------------------------------------------ control
    async def hold(self):
        self.require_connected()
        if self.busy_kind:
            self.paused = True
            if self.job and self.job["state"] == "running":
                self.job["state"] = "paused"
            self.log("tx", "!")
        return self.snapshot()

    async def resume(self):
        self.require_connected()
        self.paused = False
        if self.job and self.job["state"] == "paused":
            self.job["state"] = "running"
        self.log("tx", "~")
        return self.snapshot()

    async def cancel(self):
        self.require_connected()
        self._cancel_task()
        if self.job and self.job["state"] in ("planning", "running", "paused"):
            self.job.update(state="cancelled", message="Vom Nutzer abgebrochen.")
        self.log("warn", "Abgebrochen.")
        return self.snapshot()

    async def estop(self):
        self._cancel_task()
        if self.status == "connected":
            self.alarm = {"code": 3, "text": "Reset während der Bewegung (Not-Halt). Position kann verloren sein."}
            self.ref = None
            if self.job and self.job["state"] in ("planning", "running", "paused"):
                self.job.update(state="cancelled", message="Not-Halt.")
            self.log("error", "NOT-HALT ausgelöst. ALARM:3 – Referenz verworfen, Maschine entsperren und neu referenzieren.")
        return self.snapshot()

    async def unlock(self):
        self.require_connected_only()
        self.alarm = None
        self.log("tx", "$X")
        self.log("rx", "[MSG:Caution: Unlocked]")
        return self.snapshot()

    async def reset(self):
        self.require_connected_only()
        self._cancel_task()
        self.alarm = None
        self.ref = None
        self.log("tx", "0x18 (Soft-Reset)")
        self.log("rx", "Grbl 3.9 [FluidNC v3.9.9 (MOCK) '$' for help]")
        return self.snapshot()

    def require_connected_only(self):
        if self.status != "connected":
            raise ApiErr(409, "Nicht verbunden.", "not_connected")

    async def gcode(self, line):
        self.require_connected_only()
        line = (line or "").strip()
        if not line:
            raise ApiErr(400, "Leere Zeile.", "bad_request")
        self.log("tx", line)
        if line == "?":
            r = [f"<{self.mstate().capitalize()}|MPos:{self.fw[0]:.3f},{self.fw[1]:.3f},{self.pen:.3f},{90 + self.rev:.3f}|FS:0,0>"]
        elif line == "$I":
            r = ["[VER:3.9 FluidNC v3.9.9 (MOCK):]", "[OPT:MOCK]", "ok"]
        elif line == "$G":
            r = ["[GC:G0 G54 G17 G21 G90 G94 M5 M9 T0 F0 S0]", "ok"]
        elif self.alarm and line.upper() not in ("$X",):
            r = ["error:9"]
        else:
            r = ["ok"]
        for x in r:
            self.log("rx", x)
        return r

    # ------------------------------------------------------------------ drawings / jobs
    def plan(self, body):
        if body.get("pattern"):
            d = pat.build(body["pattern"].get("id"), body["pattern"].get("params"))
            if d is None:
                raise ApiErr(404, "Unbekanntes Testbild.", "unknown_pattern")
        elif body.get("drawing"):
            d = body["drawing"]
        else:
            raise ApiErr(400, "Weder Testbild noch Zeichnung angegeben.", "bad_request")
        d = pat.apply_layout(d, body.get("layout"), self.safe_rect())
        m = self.cfg["motion"]
        info = pat.analyse(d, self.safe_rect(), m["draw_feed_mm_min"], m["travel_feed_mm_min"])
        return {"drawing": d, **info}

    async def start_job(self, body):
        self.require_idle()
        if not self.ref:
            raise ApiErr(409, "Keine Referenz gesetzt. Ohne Referenz kann nicht gezeichnet werden.", "no_reference")
        p = self.plan(body)
        self._job_seq += 1
        total = max(p["stats"]["lines"], 1)
        self.job = {"id": self._job_seq, "name": p["drawing"]["name"], "state": "planning",
                    "dry_run": bool(body.get("dry_run")), "progress": 0.0, "line": 0, "total": total,
                    "elapsed_s": 0.0, "eta_s": None, "message": "Plane Fahrten …", "_t0": time.time()}
        self._spawn(self._run_job(p["drawing"], self.job, float(body.get("speed_scale") or 1.0)), "run")
        return self.job_info()

    async def _run_job(self, drawing, job, scale):
        m = self.cfg["motion"]
        try:
            await asyncio.sleep(0.8)
            job.update(state="running", message="")
            await self._pen_up()
            for s in drawing["strokes"]:
                pts = s["points"]
                if self._slot_index() != s["color"]:
                    job["message"] = f"Farbwechsel: {self.cfg['colors'][s['color']]['name']}"
                    await self._pen_up()
                    await self._servo("revolver", self.sv["revolver"]["slots_deg"][s["color"]])
                    job["message"] = ""
                await self._move_fw(self._phys_to_fw(pts[0]), m["travel_feed_mm_min"] * scale)
                job["line"] += 2
                if not job["dry_run"]:
                    await self._servo("pen", self.sv["pen"]["down_deg"])
                for p in pts[1:]:
                    await self._move_fw(self._phys_to_fw(p), m["draw_feed_mm_min"] * scale)
                    job["line"] += 1
                    self._job_progress(job)
                await self._pen_up()
                job["line"] += 2
                self._job_progress(job)
            px, py = m["park_position"]
            await self._move_fw(self._phys_to_fw([px, py]), m["travel_feed_mm_min"])
            job.update(state="done", line=job["total"], progress=1.0, eta_s=0.0, message="Fertig.")
        except asyncio.CancelledError:
            if job["state"] not in ("cancelled",):
                job.update(state="cancelled")
            raise

    def _job_progress(self, job):
        job["line"] = min(job["line"], job["total"])
        job["progress"] = round(job["line"] / job["total"], 4)
        el = time.time() - job["_t0"]
        job["eta_s"] = round(el * (job["total"] - job["line"]) / job["line"], 1) if job["line"] else None

    # ------------------------------------------------------------------ config
    def update_config(self, patch):
        new = deep_merge(self.cfg, patch)
        b = new["board"]
        if b["width_mm"] <= 0 or b["height_mm"] <= 0 or b["margin_mm"] < 0:
            raise ApiErr(422, "Brettmaße müssen positiv sein.", "invalid_config")
        if len(new["servos"]["revolver"]["slots_deg"]) != len(new["colors"]):
            raise ApiErr(422, "Anzahl der Revolver-Slots muss der Anzahl der Farben entsprechen.", "invalid_config")
        self.cfg = new
        return self.cfg
