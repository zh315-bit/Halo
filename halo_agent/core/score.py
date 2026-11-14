"""M3 scoring: batch `score(readings)` and the shared aggregation used by engine.Engine.

Batch and Engine are written independently (batch recomputes from the prefix, Engine keeps running
state) and must agree exactly — tests/test_scoring.py checks it. Both follow the same rules:

* No look-ahead (§3.7): a reading's baseline uses only eligible readings strictly before it.
* Frozen baseline (§7.1): only readings before the crew's first non-baseline-phase reading are eligible.
  In the QR demo every row is baseline phase, so the baseline grows causally through the whole replay.
* New measurements only: a carried-forward value reuses its last new measurement's evaluation.
"""

from __future__ import annotations

from datetime import timezone
from typing import Iterable, Optional

from .baseline import QUALITY_RANK, select_baseline
from .config import ScoringConfig
from .models import OPTIONAL_METRICS, VITAL_METRICS, Baseline, Indicator, ScoreResult, VitalsReading
from .rules import STATUS_RANK, MetricEval, evaluate_new, is_new_measurement, missing_eval
from .trend import window_points


def iso_z(ts) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def data_origin_of(reading: VitalsReading, cfg: ScoringConfig) -> str:
    try:
        return cfg.data_origin[reading.measurement_origin]
    except KeyError:
        # §3.6: never guess a label for data we can't place.
        raise ValueError(f"unknown measurement_origin {reading.measurement_origin!r}; add it to scoring.yaml")


def check_one_kind(live: Optional[bool], reading: VitalsReading, origin: str, cfg: ScoringConfig) -> bool:
    """A crew's readings are either all real camera data or all demo/simulated (AGENTS.md §3.6, §5): mixing
    them would put real scans on a synthetic baseline. Returns the crew's kind; raises on a mix."""
    kind = cfg.is_live(origin)
    if live is not None and live != kind:
        raise ValueError(f"{reading.crew_id} already holds {'live' if live else 'synthetic'} data; reading "
                         f"{reading.reading_id} is {origin}. Use a separate crew_id.")
    return kind


def _freshness(minutes: float, cfg: ScoringConfig) -> float:
    if minutes <= cfg.fresh_full_minutes:
        return 1.0
    if minutes >= cfg.fresh_zero_minutes:
        return 0.0
    return 1.0 - (minutes - cfg.fresh_full_minutes) / (cfg.fresh_zero_minutes - cfg.fresh_full_minutes)


def assemble(reading: VitalsReading, per_metric: dict[str, tuple[MetricEval, Optional[float]]],
             cfg: ScoringConfig) -> ScoreResult:
    """per_metric: vital -> (evaluation to show, fresh_minutes or None if missing)."""
    indicators, reasons = [], []
    for m in VITAL_METRICS:
        ev, fresh = per_metric[m]
        ind = ev.indicator
        b = ind.baseline
        shown = Baseline(median=round(b.median, 3), sigma=round(b.sigma, 3), n=b.n, group=b.group) if b else None
        indicators.append(Indicator(metric=m, value=reading.value(m), status=ind.status,
                                    deviation_mad=ind.deviation_mad, baseline=shown, trend=ind.trend,
                                    fresh_minutes=None if fresh is None else round(fresh, 1)))
        reasons.extend(ev.reasons)
    for m in OPTIONAL_METRICS:      # reported, never scored, not counted in quality/confidence
        indicators.append(Indicator(metric=m, value=reading.value(m), status="insufficient", deviation_mad=None,
                                    baseline=None, trend=None, fresh_minutes=None))

    vit = indicators[:len(VITAL_METRICS)]
    worst = max((STATUS_RANK[i.status] for i in vit), default=-1)
    status = {2: "alert", 1: "caution"}.get(worst, "normal")
    n_caution = sum(i.status == "caution" for i in vit)
    has_bad_trend = any(r.rule_id == "trend" for r in reasons)   # trend lands after Phase 1
    severity = 3 if status == "alert" else 2 if (n_caution >= 2 or has_bad_trend) else 1 if n_caution else 0

    present = [m for m in VITAL_METRICS if reading.value(m) is not None]
    quals = [per_metric[m][0].quality or "insufficient" for m in present]
    baseline_quality = max(quals, key=QUALITY_RANK.__getitem__) if quals else "insufficient"

    # confidence = baseline_term × freshness_term × completeness_term   (AGENTS.md §7.5)
    #   M = the vitals this data_origin can measure (scoring.yaml measurable_metrics; default all 6)
    #   baseline_term     = mean over M of min(1, n / baseline_full_n[origin]); n = 0 without a baseline
    #   freshness_term    = mean over present vitals of f(minutes since that vital's last NEW measurement),
    #                       f = 1 up to fresh_full_minutes, linear down to 0 at fresh_zero_minutes
    #   completeness_term = present vitals in M / |M|
    # With no assessable vital the product is 0, so a "normal" status with confidence 0 means "unknown".
    origin = data_origin_of(reading, cfg)
    measurable = cfg.measurable_for(origin)
    full_n = cfg.baseline_full_n_for(origin)
    by_metric = {i.metric: i for i in vit}
    b_term = sum(min(1.0, (by_metric[m].baseline.n if by_metric[m].baseline else 0) / full_n)
                 for m in measurable) / len(measurable)
    f_term = (sum(_freshness(per_metric[m][1], cfg) for m in present) / len(present)) if present else 0.0
    c_term = sum(m in present for m in measurable) / len(measurable)
    confidence = round(b_term * f_term * c_term, 3)
    building = [per_metric[m][0].building_n for m in present if per_metric[m][0].building_n is not None]
    need = cfg.min_points_for(origin)
    baseline_building = ({"n": min(building), "needed": need, "label": f"Baseline building ({min(building)}/{need})"}
                         if building else None)

    return ScoreResult(crew_id=reading.crew_id, reading_id=reading.reading_id, as_of=iso_z(reading.timestamp),
                       status=status, severity=severity, confidence=confidence, baseline_quality=baseline_quality,
                       engine=cfg.engine, data_origin=origin,
                       indicators=indicators, reasons=reasons, baseline_building=baseline_building)


def _score_crew(seq: list[VitalsReading], cfg: ScoringConfig) -> list[ScoreResult]:
    n = len(seq)
    live = None
    for r in seq:
        live = check_one_kind(live, r, data_origin_of(r, cfg), cfg)
    need = {k: cfg.min_points_for(data_origin_of(r, cfg)) for k, r in enumerate(seq)}
    freeze_at = next((k for k, r in enumerate(seq) if not r.in_baseline_phase), n)
    first_seen = {m: next((k for k, r in enumerate(seq) if r.value(m) is not None), n) for m in VITAL_METRICS}
    new = {m: [is_new_measurement(seq[k], m, k > first_seen[m], seq[k - 1].value(m) if k else None, cfg)
               for k in range(n)] for m in VITAL_METRICS}
    memo: dict[tuple[str, int], MetricEval] = {}

    def eval_at(m: str, j: int) -> MetricEval:
        """Evaluation of the new measurement of m at index j, from seq[:j] only."""
        if (m, j) in memo:
            return memo[(m, j)]
        pool: dict[str, list[float]] = {}
        for k in range(min(j, freeze_at)):
            if new[m][k]:
                pool.setdefault(seq[k].activity, []).append(seq[k].value(m))
        baseline, quality, avail = select_baseline(pool, seq[j].activity, m, cfg, need[j])
        prev = next((k for k in range(j - 1, -1, -1) if new[m][k]), None)
        prev_streak = eval_at(m, prev).alert_streak if prev is not None else 0
        hist = [(seq[k].timestamp, seq[k].value(m)) for k in range(j + 1)
                if new[m][k] and seq[k].activity == seq[j].activity]
        pts = window_points(hist, seq[j].timestamp, cfg)
        memo[(m, j)] = ev = evaluate_new(seq[j], m, baseline, quality, prev_streak, pts, cfg)
        ev.building_n = None if baseline else avail
        return ev

    out = []
    for i, r in enumerate(seq):
        per = {}
        for m in VITAL_METRICS:
            if r.value(m) is None:
                per[m] = (missing_eval(m, cfg), None)
                continue
            j = next(k for k in range(i, -1, -1) if new[m][k])   # exists: first value is always new
            per[m] = (eval_at(m, j), (r.timestamp - seq[j].timestamp).total_seconds() / 60.0)
        out.append(assemble(r, per, cfg))
    return out


def score(readings: Iterable[VitalsReading], cfg: ScoringConfig) -> list[ScoreResult]:
    """Score every reading (input order kept). Readings must be in time order within each crew."""
    readings = list(readings)
    by_crew: dict[str, list[int]] = {}
    for idx, r in enumerate(readings):
        by_crew.setdefault(r.crew_id, []).append(idx)
    out: list[Optional[ScoreResult]] = [None] * len(readings)
    for idxs in by_crew.values():
        seq = [readings[i] for i in idxs]
        for a, b in zip(seq, seq[1:]):
            if b.timestamp < a.timestamp:
                raise ValueError(f"readings out of time order: {a.reading_id} -> {b.reading_id}")
        for i, res in zip(idxs, _score_crew(seq, cfg)):
            out[i] = res
    return out  # type: ignore[return-value]
