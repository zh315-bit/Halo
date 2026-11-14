"""Stateful scorer for the live feed (WS / POST /vitals). Must match score.score() exactly."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from .baseline import select_baseline
from .config import ScoringConfig
from .models import VITAL_METRICS, ScoreResult, VitalsReading
from .rules import MetricEval, evaluate_new, is_new_measurement, missing_eval
from .score import assemble


@dataclass
class _MetricState:
    pool: dict = field(default_factory=dict)          # activity -> eligible baseline values
    seen: bool = False
    prev_value: Optional[float] = None               # value on the previous row (carry-forward check)
    last_eval: Optional[MetricEval] = None
    last_new_ts: object = None


@dataclass
class _CrewState:
    frozen: bool = False
    last_ts: object = None
    metrics: dict = field(default_factory=lambda: {m: _MetricState() for m in VITAL_METRICS})
    latest: Optional[ScoreResult] = None


class Engine:
    name = "rules-v0"

    def __init__(self, cfg: ScoringConfig):
        self.cfg = cfg
        self._crews: dict[str, _CrewState] = {}
        self._lock = threading.Lock()

    def update(self, r: VitalsReading) -> ScoreResult:
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
                    ms.last_eval = evaluate_new(r, m, baseline, quality, prev, self.cfg)
                    ms.last_new_ts = r.timestamp
                    if not st.frozen:                # added AFTER scoring: no self-reference
                        ms.pool.setdefault(r.activity, []).append(v)
                ms.seen = True
                per[m] = (ms.last_eval, (r.timestamp - ms.last_new_ts).total_seconds() / 60.0)
            st.latest = assemble(r, per, self.cfg)
            return st.latest

    def latest(self, crew_id: str) -> Optional[ScoreResult]:
        st = self._crews.get(crew_id)
        return st.latest if st else None
