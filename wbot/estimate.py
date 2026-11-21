"""Drawing time estimates that learn from past jobs.

The planner models the time of a job from path length, feed rates, acceleration, servo moves and
dwells. Reality differs by machine and by how a controller buffers moves, so every finished job
records (modelled time, real time). The median real/modelled ratio of recent jobs on the same kind
of connection corrects the next estimate. The history lives in ``config/state`` (never committed).
"""
from __future__ import annotations

import json
import logging
import statistics
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

MAX_RECORDS = 200
WINDOW = 15                 # most recent jobs that count
MIN_SAMPLES = 3             # below this the model is used as is
FACTOR_RANGE = (0.5, 3.0)
MIN_JOB_S = 8.0             # shorter jobs are dominated by start-up latency and say little


class JobHistory:
    """Past jobs: [{t, transport, est_s, actual_s, draw_mm, travel_mm, strokes, speed_scale}]."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._lock = threading.Lock()
        self.records: list[dict] = []
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    self.records = [r for r in data if isinstance(r, dict) and r.get("est_s") and r.get("actual_s")][-MAX_RECORDS:]
            except (OSError, ValueError) as exc:
                log.warning("job history unreadable (%s): starting empty", exc)

    # ---------------------------------------------------------------- learning
    def record(self, *, transport: str, est_s: float, actual_s: float, **extra) -> bool:
        """Remember one finished job (modelled and real duration in seconds). False if it is too short to learn from."""
        if not (est_s > 0 and actual_s >= MIN_JOB_S):
            return False
        rec = {"t": round(time.time()), "transport": transport, "est_s": round(float(est_s), 1), "actual_s": round(float(actual_s), 1)}
        rec.update({k: (round(v, 2) if isinstance(v, float) else v) for k, v in extra.items()})
        with self._lock:
            self.records.append(rec)
            del self.records[:-MAX_RECORDS]
            self._save()
        return True

    def _save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.records, indent=1), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.warning("job history not saved: %s", exc)

    # --------------------------------------------------------------- correcting
    def factor(self, transport: str) -> tuple[float, int]:
        """(real/modelled ratio, number of jobs it is based on) for this kind of connection; (1.0, n) until enough jobs exist."""
        with self._lock:
            ratios = [r["actual_s"] / r["est_s"] for r in self.records if r.get("transport") == transport][-WINDOW:]
        if len(ratios) < MIN_SAMPLES:
            return 1.0, len(ratios)
        lo, hi = FACTOR_RANGE
        return min(max(statistics.median(ratios), lo), hi), len(ratios)

    def estimate(self, raw_s: float, transport: str) -> dict:
        f, n = self.factor(transport)
        return {"raw_s": round(raw_s, 1), "factor": round(f, 3), "samples": n, "estimated_s": round(raw_s * f, 1)}


def eta_s(plan_total_s: float, plan_done_s: float, elapsed_s: float, factor: float) -> float:
    """Remaining time of a running job.

    ``plan_done_s`` is the modelled time of the lines already handled. Early on the learned factor is
    trusted; as the job advances the measured pace of this very job takes over.
    """
    remaining = max(plan_total_s - plan_done_s, 0.0)
    if plan_total_s <= 0:
        return 0.0
    frac = min(max(plan_done_s / plan_total_s, 0.0), 1.0)
    pace = (elapsed_s / plan_done_s) if plan_done_s >= 5.0 and elapsed_s > 0 else factor
    k = (1 - frac) * factor + frac * pace
    return remaining * k
