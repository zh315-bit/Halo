"""Stateful scorer for the live feed (WS / POST /vitals). Must match score.score() exactly."""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

from .baseline import select_baseline
from .config import ScoringConfig
from .models import VITAL_METRICS, LatestReading, ScoreResult, VitalsReading
from .rules import MetricEval, evaluate_new, is_new_measurement, missing_eval
from .score import assemble, iso_z
from .trend import window_points


@dataclass
class _MetricState:
    pool: dict = field(default_factory=dict)          # activity -> eligible baseline values
    seen: bool = False
    prev_value: Optional[float] = None               # value on the previous row (carry-forward check)
    history: dict = field(default_factory=dict)      # activity -> [(ts, value)] of new measurements (trend)
    last_eval: Optional[MetricEval] = None
    last_new_ts: object = None


@dataclass
class _CrewState:
    frozen: bool = False
    last_ts: object = None
    metrics: dict = field(default_factory=lambda: {m: _MetricState() for m in VITAL_METRICS})
    latest: Optional[ScoreResult] = None
    last_reading: Optional[VitalsReading] = None
    received_wall: float = 0.0                       # time.monotonic() when last_reading was processed
    log: deque = field(default_factory=lambda: deque(maxlen=Engine.HISTORY_MAX))   # (reading, result)


log = logging.getLogger("halo.core.engine")


class Engine:
    name = "rules-v0"
    HISTORY_MAX = 6000            # (reading, result) pairs kept per crew for the daily report (~20 days at 5 min)

    def __init__(self, cfg: ScoringConfig, wall=time.monotonic):
        self.cfg = cfg
        self._wall = wall
        self._listeners: list[Callable[[ScoreResult], None]] = []
        self._crews: dict[str, _CrewState] = {}
        self._lock = threading.Lock()

    def add_listener(self, fn: Callable[[ScoreResult], None]) -> Callable[[], None]:
        """Call fn(score) after every update (outside the Engine lock, so fn may call latest() etc.).
        A listener that raises is logged and skipped; update() still returns its result. Returns a remover."""
        with self._lock:
            self._listeners.append(fn)

        def remove() -> None:
            with self._lock:
                if fn in self._listeners:
                    self._listeners.remove(fn)
        return remove

    def update(self, r: VitalsReading) -> ScoreResult:
        result = self._update(r)
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(result)
            except Exception:
                log.exception("Engine listener %r failed; score %s kept", fn, result.reading_id)
        return result

    def _update(self, r: VitalsReading) -> ScoreResult:
        with self._lock:
            st = self._crews.setdefault(r.crew_id, _CrewState())
            if st.last_ts is not None and r.timestamp < st.last_ts:
                raise ValueError(f"reading {r.reading_id} is older than the last one for {r.crew_id}")
            st.last_ts = r.timestamp
            if not r.in_baseline_phase:
                st.frozen = True                     # baseline never changes again (§7.1)
            per = {}
            for m in VITAL_METRICS:
                ms: _MetricState = st.metrics[m]
                v = r.value(m)
                prev_value, ms.prev_value = ms.prev_value, v
                if v is None:
                    per[m] = (missing_eval(m, self.cfg), None)
                    continue
                if is_new_measurement(r, m, ms.seen, prev_value, self.cfg):
                    baseline, quality = select_baseline(ms.pool, r.activity, m, self.cfg)
                    prev = ms.last_eval.alert_streak if ms.last_eval else 0
                    hist = ms.history.setdefault(r.activity, [])
                    hist.append((r.timestamp, v))
                    pts = window_points(hist, r.timestamp, self.cfg)
                    if len(hist) > 4 * len(pts) + 64:          # keep memory bounded on long feeds
                        del hist[:len(hist) - len(pts) - 32]
                    ms.last_eval = evaluate_new(r, m, baseline, quality, prev, pts, self.cfg)
                    ms.last_new_ts = r.timestamp
                    if not st.frozen:                # added AFTER scoring: no self-reference
                        ms.pool.setdefault(r.activity, []).append(v)
                ms.seen = True
                per[m] = (ms.last_eval, (r.timestamp - ms.last_new_ts).total_seconds() / 60.0)
            st.latest = assemble(r, per, self.cfg)
            st.last_reading, st.received_wall = r, self._wall()
            st.log.append((r, st.latest))
            return st.latest

    def reset(self, crew_id: Optional[str] = None) -> None:
        """Forget all state (or one crew's): baseline, history, latest score."""
        with self._lock:
            if crew_id is None:
                self._crews.clear()
            else:
                self._crews.pop(crew_id, None)

    def latest_reading(self, crew_id: str, now: Optional[datetime] = None) -> Optional[LatestReading]:
        """Last processed reading with per-vital measured_at / ages. `now` defaults to the feed clock:
        the reading's own timestamp plus the wall time elapsed since the Engine processed it (so ages
        grow between readings, also during an N× replay)."""
        with self._lock:
            st = self._crews.get(crew_id)
            if st is None or st.last_reading is None:
                return None
            r = st.last_reading
            if now is None:
                now = r.timestamp + timedelta(seconds=self._wall() - st.received_wall)
            measured, ages = {}, {}
            for m in VITAL_METRICS:
                ms = st.metrics[m]
                if r.value(m) is None or ms.last_new_ts is None:
                    measured[m], ages[m] = None, None
                else:
                    measured[m] = iso_z(ms.last_new_ts)
                    ages[m] = round((now - ms.last_new_ts).total_seconds(), 1)
            return LatestReading(**asdict(r), measured_at=measured, metric_age_seconds=ages,
                                 age_seconds=round((now - r.timestamp).total_seconds(), 1))

    def history(self, crew_id: str) -> list[tuple[VitalsReading, ScoreResult]]:
        """(reading, score) pairs this Engine produced for the crew, oldest first (bounded)."""
        with self._lock:
            st = self._crews.get(crew_id)
            return list(st.log) if st else []

    def crews(self) -> list[str]:
        with self._lock:
            return list(self._crews)

    def latest(self, crew_id: str) -> Optional[ScoreResult]:
        st = self._crews.get(crew_id)
        return st.latest if st else None
