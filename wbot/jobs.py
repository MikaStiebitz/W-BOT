"""Drawing resolution, preview and the job runner (streams a Plan to the controller)."""
from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import TYPE_CHECKING

import numpy as np

from .drawing import Drawing
from .estimate import JobHistory, eta_s
from .kinematics import Mapping
from .patterns import REGISTRY, build_pattern, pattern_to_dict
from .planner import PlanOptions, plan as make_plan
from .safety import validate_gcode

if TYPE_CHECKING:
    from .machine import Machine

log = logging.getLogger(__name__)


class JobManager:
    def __init__(self, machine: "Machine"):
        self.m = machine
        self.job: dict | None = None
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._intended: Drawing | None = None
        self._t0 = 0.0
        self._plan = None
        self._trace_from = 0
        self._est: dict = {}
        self._live = False
        from .machine import STATE_DIR
        self.history = JobHistory(STATE_DIR / "job_history.json" if machine.persist else None)   # learns from finished jobs

    # ----------------------------------------------------------------- helpers
    @property
    def running(self) -> bool:
        return bool(self.job and self.job["state"] in ("planning", "running", "paused")
                    and self._thread is not None and self._thread.is_alive())

    def patterns(self) -> list[dict]:
        return [pattern_to_dict(p) for p in REGISTRY.values()]

    def import_svg(self, data: bytes, name: str) -> dict:
        from .svgimport import svg_to_drawing
        from .machine import MachineError
        try:
            d = svg_to_drawing(data, self.m.cfg, name=name)
        except Exception as exc:  # noqa: BLE001
            raise MachineError(f"The SVG could not be read: {exc}", "bad_svg", 400) from exc
        return {"drawing": d.to_json()}

    def import_image(self, data: bytes, name: str, options: dict | None = None) -> dict:
        """Trace a raster image into pen paths. Options: mode, detail, invert, use_pen_colors, shade, pen, threshold."""
        from .imagetrace import ImageError, TraceOptions, trace_image
        from .machine import MachineError
        o = options or {}
        try:
            t = TraceOptions(mode=str(o.get("mode", "auto")), detail=int(o.get("detail", 50)),
                             invert=bool(o.get("invert", False)), use_pen_colors=bool(o.get("use_pen_colors", False)),
                             shade=bool(o.get("shade", False)),
                             pen=int(o.get("pen", 0)), threshold=None if o.get("threshold") is None else int(o["threshold"]),
                             pen_widths_mm=[c.width_mm for c in self.m.cfg.colors])
            res = trace_image(data, self.m.cfg, t)
        except (ImageError, ValueError) as exc:
            raise MachineError(str(exc), "bad_image", 400) from exc
        res.drawing.name = name
        return {"drawing": res.drawing.to_json(), "info": res.info, "warnings": res.warnings}

    def resolve(self, body: dict, meta: list | None = None, allow_empty: bool = False) -> Drawing:
        """Drawing of a request body: canvas elements, a traced drawing or a test pattern, then the layout.
        ``meta`` (a list) receives the per-element boxes and stroke ranges when elements are used."""
        from .machine import MachineError
        cfg = self.m.cfg
        try:
            if body.get("elements") is not None:
                from .elements import ElementError, compose
                try:
                    d, info = compose(list(body["elements"]), cfg)
                except ElementError as exc:
                    raise MachineError(str(exc), "bad_request", 400) from exc
                if meta is not None:
                    meta.extend(info)
                if not d.strokes and not allow_empty:
                    raise MachineError("Add something to draw first.", "bad_request", 400)
            elif body.get("drawing"):
                d = Drawing.from_json(body["drawing"])
            elif body.get("pattern"):
                pat = body["pattern"]
                d = build_pattern(cfg, pat.get("id"), pat.get("params") or {})
            else:
                raise MachineError("Neither a test pattern nor a drawing was specified.", "bad_request", 400)
        except MachineError:
            raise
        except (ValueError, KeyError) as exc:
            raise MachineError(f"Invalid input: {exc}", "bad_request", 400) from exc
        lay = body.get("layout") or {}
        if lay:
            if lay.get("fit"):
                xmin, ymin, xmax, ymax = cfg.safe_rect()
                d = d.fit_to((xmin, ymin, xmax, ymax), keep_aspect=True)
            d = d.transformed(scale=float(lay.get("scale", 1.0)), rotate_deg=float(lay.get("rotate", 0.0)),
                              dx=float(lay.get("dx", 0.0)), dy=float(lay.get("dy", 0.0)))
        return d

    def _mapping(self) -> tuple[Mapping, bool]:
        if self.m.mapping is not None:
            return self.m.mapping, True
        c = (self.m.cfg.board.width_mm / 2, self.m.cfg.board.height_mm / 2)
        return Mapping.from_config(self.m.cfg, c, c), False

    def compose(self, body: dict) -> dict:
        """Fast preview of canvas elements: the drawing and each element's box, without planning the pen path."""
        meta: list = []
        d = self.resolve(body, meta, allow_empty=True)
        b = d.bounds()
        return {"drawing": d.to_json(), "bounds": None if b is None else [float(v) for v in b], "elements": meta}

    def preview(self, body: dict) -> dict:
        meta: list = []
        d = self.resolve(body, meta, allow_empty=True)
        if body.get("elements") is not None and not d.strokes:      # nothing drawable yet: still report each element's error
            return {"drawing": {"name": "", "strokes": []}, "bounds": None, "estimated_s": 0.0, "warnings": [],
                    "stats": {"strokes": 0, "draw_mm": 0.0}, "elements": meta}
        mp, real = self._mapping()
        live = bool(body.get("live"))
        opts = PlanOptions(dry_run=bool(body.get("dry_run")), speed_scale=float(body.get("speed_scale") or 1.0),
                           optimize=not live, park_at_end=not live)
        p = make_plan(d, self.m.cfg, mp, opts)
        warnings = list(p.warnings)
        if not real:
            warnings.append("No reference set - time estimate uses an assumed reference.")
        est = self._estimate(p.estimated_s)
        out = {"drawing": d.to_json(), "bounds": [float(v) for v in d.bounds()],
               "estimated_s": est["estimated_s"], "estimate": est, "warnings": warnings, "stats": p.stats}
        if body.get("elements") is not None:
            out["elements"] = meta
        return out

    # ---------------------------------------------------------------- estimates
    def _transport(self) -> str:
        return self.m.transport or self.m.cfg.connection.transport

    def _sim_speed(self) -> float:
        return max(float(getattr(self.m, "sim_speed", 1.0) or 1.0), 1e-6) if self._transport() == "sim" else 1.0

    def _estimate(self, modelled_s: float) -> dict:
        """Modelled time corrected by what past jobs on this kind of connection really took."""
        e = self.history.estimate(modelled_s, self._transport())
        sp = self._sim_speed()
        if sp != 1.0:                                   # the simulator may run faster than real time
            e["sim_speed"] = sp
            e["estimated_s"] = round(e["estimated_s"] / sp, 1)
        return e

    # --------------------------------------------------------------------- job
    def start(self, body: dict) -> dict:
        from .machine import MachineError
        m = self.m
        with m._cmd():
            m._ready_for_motion()
            mp = m._need_reference()
            ms, _ = m.machine_state()
            if ms != "idle":
                raise MachineError("The machine is not ready (Idle required).", "not_idle")
            d = self.resolve(body)
            slot = m.revolver_slot()
            live = bool(body.get("live"))
            opts = PlanOptions(dry_run=bool(body.get("dry_run")),
                               speed_scale=float(body.get("speed_scale") or 1.0),
                               optimize=not live, park_at_end=not live,
                               start_phys=m.phys_xy(),
                               current_slot=None if slot is None else slot - 1)
            plan = make_plan(d, m.cfg, mp, opts)
            lines = [ln.text for ln in plan.lines]
            problems = validate_gcode(lines, m.cfg, mp, z_axis=m.axis(m.cfg.servos.pen.axis),
                                      start_outside=m.outside_safe_rect())
            if problems:
                p = problems[0]
                raise MachineError(f"Safety check of the drawing: {p.message} (line {p.index + 1})",
                                   "unsafe", 422)
        self._plan, self._intended = plan, d
        emu = m._emulator()
        self._trace_from = len(emu.trace) if emu is not None else 0
        self._cancel.clear()
        self._est = self._estimate(plan.estimated_s)
        self._live = live
        self.job = {"id": uuid.uuid4().hex[:8], "name": body.get("name") or d.name or "Job",
                    "state": "running", "dry_run": opts.dry_run, "progress": 0.0, "line": 0,
                    "total": len(lines), "elapsed_s": 0.0, "eta_s": self._est["estimated_s"],
                    "estimated_s": self._est["estimated_s"], "message": ""}
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._run, args=(lines,), name="job", daemon=True)
        self._thread.start()
        m.log("info", f"Job '{self.job['name']}' started ({len(lines)} lines, ~{plan.estimated_s:.0f} s)"
                      f"{' [dry run]' if opts.dry_run else ''}.")
        self._publish()
        return dict(self.job)

    def _publish(self) -> None:
        if self.job:
            self.job["elapsed_s"] = round(time.monotonic() - self._t0, 1) if self._t0 else 0.0
            self.m._emit({"type": "job", "data": dict(self.job)})

    def _run(self, lines: list[str]) -> None:
        m, job = self.m, self.job
        total = len(lines)
        plan, est = self._plan, self._est
        sp, factor = est.get("sim_speed", 1.0), est.get("factor", 1.0)

        def progress(done: int, _total: int) -> None:
            frac = done / total if total else 1.0
            job["line"], job["progress"] = done, round(frac, 4)
            # remaining time from the modelled time of the lines done so far, blended with this job's real pace
            t_done = plan.line_end[min(done, len(plan.line_end)) - 1] if done and plan.line_end else frac * plan.estimated_s
            job["eta_s"] = round(eta_s(plan.estimated_s, t_done, (time.monotonic() - self._t0) * sp, factor) / sp, 1)
            self._publish()

        try:
            with m._cmd_lock:
                res = m.client.stream(lines, on_progress=progress, cancel=self._cancel, stop_on_error=True)
                if self._cancel.is_set():
                    self._finish_cancel()
                    return
                if not getattr(res, "ok", True):
                    raise RuntimeError(f"Controller error: {getattr(res, 'message', '') or getattr(res, 'status', 'unknown')}")
                if not m.client.wait_idle(timeout=600.0, cancel=self._cancel):
                    if self._cancel.is_set():
                        self._finish_cancel()
                        return
                    raise RuntimeError("Timed out waiting for the job to finish.")
            job["state"], job["progress"], job["eta_s"] = "done", 1.0, 0.0
            elapsed = time.monotonic() - self._t0
            if not job.get("dry_run") and not self._live:            # learn: modelled vs. real duration
                self.history.record(transport=self._transport(), est_s=plan.estimated_s, actual_s=elapsed * sp,
                                    draw_mm=float(plan.stats.get("draw_mm", 0.0)), travel_mm=float(plan.stats.get("travel_mm", 0.0)),
                                    strokes=int(plan.stats.get("strokes", 0)))
            m.log("info", f"Job '{job['name']}' finished in {elapsed:.0f} s (estimated {job.get('estimated_s', 0):.0f} s).")
        except Exception as exc:  # noqa: BLE001
            if job["state"] not in ("cancelled",):
                job["state"], job["message"] = "error", str(exc)
                m.log("error", f"Job aborted: {exc}")
        finally:
            self._publish()

    def _finish_cancel(self) -> None:
        m, job = self.m, self.job
        if job["state"] == "error":                  # link_lost() already reported the real cause
            return
        job["state"], job["message"] = "cancelled", "Cancelled"
        m.log("warn", "Job cancelled - lifting the pen.")
        try:
            st, _ = m.machine_state()
            if st == "alarm":
                m.client.unlock()
            m._send_lines(m._servo_lines("pen", m.cfg.servos.pen.up_deg))
        except Exception as exc:  # noqa: BLE001
            m.log("error", f"Lifting the pen after the cancel failed: {exc}")

    # ---------------------------------------------------------------- controls
    def cancel(self) -> None:
        if self.running:
            self._cancel.set()

    def on_hold(self) -> None:
        if self.job and self.job["state"] == "running":
            self.job["state"] = "paused"
            self._publish()

    def on_resume(self) -> None:
        if self.job and self.job["state"] == "paused":
            self.job["state"] = "running"
            self._publish()

    def on_estop(self) -> None:
        if self.running:
            self._cancel.set()
            self.job["state"], self.job["message"] = "cancelled", "Emergency stop"
            self._publish()

    def link_lost(self) -> None:
        if self.running:
            self._cancel.set()
            self.job["state"], self.job["message"] = "error", "Connection lost"
            self._publish()

    def abort_on_disconnect(self) -> None:
        self._cancel.set()

    def info(self) -> dict | None:
        if self.job:
            self.job["elapsed_s"] = round(time.monotonic() - self._t0, 1) if self.running else self.job["elapsed_s"]
        return dict(self.job) if self.job else None

    # --------------------------------------------------------------- simulator
    def trace_error(self, trace) -> dict | None:
        """Deviation of the physically drawn trace from the intended strokes (mm)."""
        if self._intended is None or not self._intended.strokes:
            return None
        try:
            from shapely.geometry import LineString, MultiLineString, Point
        except ImportError:
            return None
        lines = [LineString(s.pts) for s in self._intended.strokes if len(s.pts) >= 2]
        if not lines:
            return None
        target = MultiLineString(lines)
        pts = []
        for seg in trace[self._trace_from:]:
            if seg.pen_down:
                n = max(2, int(np.hypot(seg.x1 - seg.x0, seg.y1 - seg.y0) / 2.0) + 1)
                for t in np.linspace(0, 1, n):
                    pts.append((seg.x0 + t * (seg.x1 - seg.x0), seg.y0 + t * (seg.y1 - seg.y0)))
        if not pts:
            return None
        dev = np.array([target.distance(Point(p)) for p in pts[:: max(1, len(pts) // 4000)]])
        return {"max_mm": float(dev.max()), "mean_mm": float(dev.mean()),
                "p95_mm": float(np.percentile(dev, 95)), "samples": int(len(dev))}
