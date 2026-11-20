"""CameraService part: camera-movement follow-up and people at the board (guard, queue, filter).

Settings (``camera.json``):
  watch:  {enabled}                         re-align the board mapping when the camera was moved
  people: {enabled, guard, filter, resume}  guard: "off" | "warn" | "protect"
          protect = no job starts while someone stands at the board, a running job pauses
          (and resumes 3 s after they left when ``resume``); a job can be queued to start once it is free.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np

from ..errors import MachineError
from .presence import PresenceWatch
from .watch import CameraWatch, carry_points

log = logging.getLogger(__name__)
CLEAR_S = 3.0              # nobody seen for this long = free
WATCH_EVERY_S = 2.0
MAX_QUEUED = 50           # waiting jobs (live drawing sends many small ones)


class GuardMixin:
    def _init_guard(self) -> None:
        self.watch = CameraWatch(self.path.parent / "camera_ref.png" if self.persist else None)
        self.presence = PresenceWatch()
        self.watch_state: dict = {"state": "none"}
        self._watch_t = 0.0
        self._paused_by_person = False
        self._queued: list[dict] = []          # jobs waiting for a free board, started one after another
        self._people_warn_t = 0.0
        c = self.settings.get("calib")
        if c and self.watch.load(tuple(c["image_size"])):
            self.watch_state = {"state": "check", "t": time.time()}       # verify on the first frame after start

    # ------------------------------------------------------------------ camera moved?
    def _set_reference(self, img: np.ndarray) -> None:
        self.watch.set_reference(img)
        self.presence.reset()
        self.watch_state = {"state": "ok", "t": time.time()}

    def _watch_once(self, img: np.ndarray) -> None:
        if not self.settings["watch"]["enabled"] or self.geom is None or time.time() - self._watch_t < WATCH_EVERY_S:
            return
        self._watch_t = time.time()
        if self.presence.present:                    # someone in front: wait, the picture is not representative
            return
        res = self.watch.check(img)
        if res is None:
            return
        if not res["moved"]:
            if self.watch_state.get("state") != "ok":
                self.watch_state = {"state": "ok", "t": time.time()}
            return
        c = self.settings["calib"]
        if res["ok"]:
            pts = carry_points(res["H"], c["image_points"])
            try:
                self.set_correspondences(pts, c["board_points"], c.get("method", "corners"), tuple(c["image_size"]), image=img)
            except MachineError as exc:
                self.watch_state = {"state": "lost", "t": time.time(), "error": exc.message}
                return
            self.watch_state = {"state": "followed", "t": time.time(), "shift_px": res["shift_px"]}
            self.m.log("info", f"Camera was moved ({res['shift_px']:.0f} px): board alignment followed automatically.")
        elif self.watch_state.get("state") != "lost":
            self.watch_state = {"state": "lost", "t": time.time()}
            self.m.log("warn", "The camera moved a lot or shows something else: please align the board again.")
        self.m._emit({"type": "camera_watch", "data": self.watch_state})

    # ------------------------------------------------------------------ people
    def _exclude_polys(self) -> list[np.ndarray]:
        """Image polygons of things that move on purpose: the gondola and its belts."""
        g = self.geom
        p = self.m.phys_xy() if (g is not None and self.m.mapping is not None) else None
        if p is None:
            return []
        x, y = p
        geo = self.m.cfg.geometry
        out = [g.to_image([(x - 70, y - 70), (x + 70, y - 70), (x + 70, y + 240), (x - 70, y + 240)], 36.0)]
        for a, s in ((geo.anchor_left, -1), (geo.anchor_right, 1)):
            e = np.array([x + s * geo.attach_half_spacing_mm, y + geo.attach_height_mm])
            d = e - np.asarray(a)
            n = np.array([-d[1], d[0]]) / max(np.linalg.norm(d), 1e-6) * 25.0
            out.append(g.to_image([np.asarray(a) + n, e + n, e - n, np.asarray(a) - n], 30.0))
        return [o for o in out if np.all(np.isfinite(o))]

    def _watch_area(self) -> np.ndarray | None:
        """The board and a strip around it (image px): only objects there matter."""
        g = self.geom
        if g is None:
            return None
        b = self.m.cfg.board
        n = 24
        xs, ys = np.linspace(-150, b.width_mm + 150, n), np.linspace(-150, b.height_mm + 250, n)
        ring = ([(x, ys[0]) for x in xs] + [(xs[-1], y) for y in ys] + [(x, ys[-1]) for x in xs[::-1]]
                + [(xs[0], y) for y in ys[::-1]])
        q = g.to_image(np.array(ring))
        return q if np.all(np.isfinite(q)) else None

    def _presence_once(self, img: np.ndarray) -> None:
        ppl = self.settings["people"]
        if not ppl["enabled"]:
            return
        st = self.presence.update(img, self._exclude_polys(), roi=self._watch_area())
        st["guard"] = ppl["guard"]
        st["queued"] = bool(self._queued)
        st["queued_count"] = len(self._queued)
        st["paused_by_person"] = self._paused_by_person
        self.m._emit({"type": "presence", "data": st})
        jobs = self.m.jobs
        running = bool(jobs and jobs.running)
        job_state = (jobs.job or {}).get("state") if jobs else None
        now = time.time()
        if self.presence.present:
            if ppl["guard"] == "protect" and running and job_state == "running":
                try:
                    self.m.hold()
                    self._paused_by_person = True
                    self.m.log("warn", "Something is in front of the board: job paused.")
                except MachineError as exc:
                    log.warning("pause for person failed: %s", exc.message)
            elif ppl["guard"] in ("warn", "protect") and running and now - self._people_warn_t > 30:
                self._people_warn_t = now
                self.m.log("warn", "Careful: something is in front of the board while the machine draws.")
            return
        if now - self.presence.last_seen < CLEAR_S:
            return
        if self._paused_by_person:
            self._paused_by_person = False
            if ppl["resume"] and job_state == "paused":
                try:
                    self.m.resume()
                    self.m.log("info", "Board free again: job continues.")
                except MachineError as exc:
                    log.warning("resume after person failed: %s", exc.message)
        if self._queued and not running:
            body = self._queued.pop(0)
            try:
                self.m.start_job(body)
                self.m.log("info", "Board free: waiting job started.")
            except MachineError as exc:
                self.m.log("warn", f"Waiting job could not start: {exc.message}")

    def person_present(self) -> bool:
        s = self.source
        return bool(self.settings["people"]["enabled"] and s is not None and s.latest() is not None
                    and self.presence.present)

    def check_clear(self, body: dict | None = None) -> None:
        """Called before a job starts. ``protect`` refuses while someone stands at the board."""
        if self.settings["people"]["guard"] == "protect" and self.person_present() and not (body or {}).get("force"):
            raise MachineError("Something is in front of the board. The job does not start while the board is blocked "
                               "(start it when free, or turn the guard off in the Camera tab).", "person_present", 409)

    def queue_job(self, body: dict) -> dict[str, Any]:
        if not isinstance(body, dict) or not (body.get("pattern") or body.get("drawing")):
            raise MachineError("No job given.", "bad_request", 400)
        self.m._jobs().resolve(body)                 # validate now, not when the board is free
        if len(self._queued) >= MAX_QUEUED:
            raise MachineError("Too many jobs are already waiting for the board.", "queue_full", 409)
        self._queued.append(dict(body))
        if len(self._queued) == 1:
            self.m.log("info", "Job waits until the board is free.")
        return {"queued": True, "count": len(self._queued)}

    def cancel_queue(self) -> dict[str, Any]:
        self._queued = []
        return {"queued": False}
