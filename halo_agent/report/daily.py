"""M5 daily report, template version (AGENTS.md §9 steps 1–4). Deterministic, offline, no LLM.

Inputs are what the live Engine already produced (reading, ScoreResult) pairs, so the report, SOS and the
score endpoints all share one source. Missing stays missing: a field with no data is null, never 0.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from ..core.config import ScoringConfig
from ..core.models import VITAL_METRICS, ScoreResult, VitalsReading
from ..core.rules import STATUS_RANK
from .templates import _LABEL, summary_text

DISCLAIMER = "Decision support only. Not a diagnosis."
UTC = timezone.utc


@dataclass(frozen=True)
class ReportConfig:
    night_start_hour_prev_day: int
    night_end_hour: int
    min_coverage: float
    min_nights: int
    min_mornings: int
    duty_map: dict
    morning_window_hours: float
    duty_consecutive: int
    rec: dict

    @classmethod
    def from_dict(cls, d: dict) -> "ReportConfig":
        return cls(night_start_hour_prev_day=int(d["night_window"]["start_hour_prev_day"]),
                   night_end_hour=int(d["night_window"]["end_hour"]),
                   min_coverage=float(d["night_window"]["min_coverage"]),
                   min_nights=int(d["min_nights_for_baseline"]), min_mornings=int(d["min_mornings_for_baseline"]),
                   duty_map=dict(d["duty_status"]), morning_window_hours=float(d["morning_window_hours"]),
                   duty_consecutive=int(d["duty_consecutive"]),
                   rec=dict(d["recommendations"]))


def mission_day_of(d: date, scfg: ScoringConfig) -> int:
    return (d - scfg.mission_start.date()).days + 1


def night_window(d: date, rcfg: ReportConfig) -> tuple[datetime, datetime]:
    start = datetime.combine(d - timedelta(days=1), time(rcfg.night_start_hour_prev_day), UTC)
    return start, datetime.combine(d, time(rcfg.night_end_hour), UTC)


def _step_minutes(readings: list[VitalsReading]) -> Optional[float]:
    gaps = [(b.timestamp - a.timestamp).total_seconds() / 60 for a, b in zip(readings, readings[1:])]
    gaps = [g for g in gaps if g > 0]
    return statistics.median(gaps) if gaps else None


def in_bed_minutes(readings: list[VitalsReading], d: date, rcfg: ReportConfig,
                   cutoff: Optional[datetime] = None) -> tuple[Optional[float], list[float]]:
    """Sleep is ESTIMATED from activity = in_bed (no sleep stages in the data, AGENTS.md §5).
    Returns (minutes, in-bed HR values). Minutes are None unless the readings cover at least
    min_coverage of the night window up to the cutoff: an uncovered night is unknown, not 0 minutes."""
    lo, hi = night_window(d, rcfg)
    hi = min(hi, cutoff) if cutoff else hi
    night = [r for r in readings if lo <= r.timestamp < hi]
    step = _step_minutes(readings)
    if not night or step is None:
        return None, []
    if len(night) * step < rcfg.min_coverage * (hi - lo).total_seconds() / 60:
        return None, []
    in_bed = [r for r in night if r.activity == "in_bed"]
    return len(in_bed) * step, [r.heart_rate_bpm for r in in_bed if r.heart_rate_bpm is not None]


def debounced_status(resting: list[tuple[VitalsReading, ScoreResult]], all_rows: list[tuple[VitalsReading, ScoreResult]],
                     n: int) -> tuple[str, set]:
    """Duty level for the morning window and the (metric, rule_id) findings behind it.

    * Personal-baseline z bands (resting readings only): a metric's level counts only if its last `n`
      consecutive NEW measurements (fresh_minutes == 0) all reach it, so one noisy reading cannot set duty.
    * Unfavourable trends (resting readings): count at once, they already span days.
    * Red flags (source != baseline): count at once from ANY activity — an absolute line just crossed
      must not be hidden because the reading was taken while active.
    """
    worst, found = 0, set()
    for metric in VITAL_METRICS:
        levels = []
        for _, sc in resting:
            ind = next(i for i in sc.indicators if i.metric == metric)
            if ind.fresh_minutes != 0:
                continue                                    # carried-forward row: same measurement as before
            levels.append(max(STATUS_RANK[ind.status], 0) if any(
                x.metric == metric and x.rule_id == "z_band" for x in sc.reasons) else 0)
            if len(levels) >= n and min(levels[-n:]) > 0:
                lv = min(levels[-n:])
                worst = max(worst, lv)
                found.add((metric, "z_band"))
            for x in sc.reasons:
                if x.metric == metric and x.rule_id == "trend":
                    worst = max(worst, STATUS_RANK[x.status])
                    found.add((metric, "trend"))
    for _, sc in all_rows:
        for x in sc.reasons:
            if x.source != "baseline" and x.status in ("caution", "alert"):
                worst = max(worst, STATUS_RANK[x.status])
                found.add((x.metric, x.rule_id))
    return {0: "normal", 1: "caution", 2: "alert"}[worst], found


def _pct(x: float, ref: float) -> float:
    return round((x - ref) / ref * 100, 1)


def build_daily_report(crew_id: str, d: date, history: list[tuple[VitalsReading, ScoreResult]],
                       scfg: ScoringConfig, rcfg: ReportConfig, environment: Optional[dict] = None,
                       mornings: Optional[dict] = None) -> dict:
    """history: Engine (reading, score) pairs, oldest first. mornings: date -> {"completed_at": datetime|None,
    "completion_seconds": float|None} from POST /activity/morning. environment: radiation reference dict."""
    mornings = mornings or {}
    readings = [r for r, _ in history]
    lo, hi = night_window(d, rcfg)
    m = mornings.get(d) or {}
    cutoff = min(hi, m["completed_at"]) if m.get("completed_at") else hi

    # 2. collect ---------------------------------------------------------------------------------------
    minutes, bed_hr = in_bed_minutes(readings, d, rcfg, cutoff)
    earlier = [x for x in (in_bed_minutes(readings, d - timedelta(days=k), rcfg)[0] for k in range(1, 15))
               if x is not None]
    sleep_vs = round(minutes - statistics.median(earlier), 1) if minutes is not None and len(earlier) >= rcfg.min_nights else None

    done_s = m.get("completion_seconds")
    prev_done = [v["completion_seconds"] for k, v in mornings.items() if k < d and v.get("completion_seconds")]
    morning_vs = (_pct(done_s, statistics.median(prev_done))
                  if done_s is not None and len(prev_done) >= rcfg.min_mornings else None)

    window = [(r, s) for r, s in history if lo <= r.timestamp <= cutoff]
    # 3. score: M3 already ran on every reading (alert is debounced there). Duty looks at the RESTING readings
    #    of the morning window and takes the worst status, so one late reading can neither hide nor create it.
    m_lo = cutoff - timedelta(hours=rcfg.morning_window_hours)
    morning_all = [(r, s) for r, s in window if r.timestamp >= m_lo]
    morning = [(r, s) for r, s in morning_all if r.activity == "resting"]
    latest_rest = morning[-1] if morning else None

    days = int(scfg.trend_window) if scfg.trend_unit == "days" else 0
    vitals_trend = []
    for metric in VITAL_METRICS:
        ind = next((i for i in latest_rest[1].indicators if i.metric == metric), None) if latest_rest else None
        vitals_trend.append({"metric": metric, "days": days,
                             "direction": ind.trend.direction if ind is not None and ind.trend else "insufficient"})

    # 4. template ---------------------------------------------------------------------------------------
    if morning:
        duty_level, found = debounced_status(morning, morning_all, rcfg.duty_consecutive)
    else:                                           # no resting reading: unknown, never "go" (red flags still show)
        duty_level, found = None, debounced_status([], morning_all, rcfg.duty_consecutive)[1]
    duty = rcfg.duty_map[duty_level] if duty_level else rcfg.duty_map["no_data"]
    if duty_level is None and any(STATUS_RANK[x.status] == 2 for _, sc in morning_all for x in sc.reasons
                                  if (x.metric, x.rule_id) in found):
        duty = rcfg.duty_map["alert"]
    watch: list[str] = []
    if not morning:
        watch.append("No resting vitals in the morning window. Missing, not assumed normal.")
    latest_text = {}
    for _, sc in morning_all:                       # most recent wording of each finding that counted
        for x in sc.reasons:
            if (x.metric, x.rule_id) in found:
                latest_text[(x.metric, x.rule_id)] = (STATUS_RANK[x.status], x.text)
    watch.extend(t for _, t in sorted(latest_text.values(), key=lambda v: -v[0]))
    night_alerts = [s for r, s in window if s.status == "alert"]
    if night_alerts:
        rules = sorted({x.rule_id for s in night_alerts for x in s.reasons if x.status == "alert"})
        watch.append(f"{len(night_alerts)} alert reading(s) since {lo:%H:%M} the previous day ({', '.join(rules)}).")

    # recommendations: fixed team_policy sentences only (no medical content)
    rec: list[str] = []
    for t in vitals_trend:
        bad = scfg.unfavourable[t["metric"]]
        if (t["direction"] == "rising" and bad in ("high", "both")) or (t["direction"] == "falling" and bad in ("low", "both")):
            rec.append(rcfg.rec["unfavourable_trend"].format(metric=_LABEL[t["metric"]]))
    if not morning or any(latest_rest[0].value(m) is None for m in VITAL_METRICS):
        rec.append(rcfg.rec["missing_data"])
    if duty == "rest":
        rec.append(rcfg.rec["rest"])
    recommendations = list(dict.fromkeys(rec))

    origins = sorted({s.data_origin for _, s in window})
    data_sources = origins + (["NASA historical reference"] if environment else [])
    report = {
        "crew_id": crew_id,
        "date": d.isoformat(),
        "duty_status": duty,
        "sleep": {"in_bed_minutes": minutes, "vs_baseline": sleep_vs,
                  "in_bed_hr_median": statistics.median(bed_hr) if bed_hr else None},
        "morning_activity": {"completion_seconds": done_s, "vs_baseline_pct": morning_vs},
        "vitals_trend": vitals_trend,
        "environment_context": environment or {"label": "Historical ISS radiation - May 2024",
                                               "is_personal_dose": False},
        "watch_items": watch,
        "recommendations": recommendations,
        "recommendations_source": rcfg.rec["source"],
        "summary_text": "",
        "summary_generated_by": "template",
        "data_sources": data_sources,
        "disclaimer": DISCLAIMER,
    }
    report["summary_text"] = summary_text(report, duty_level, len(morning), rcfg.morning_window_hours,
                                          rcfg.duty_consecutive)
    return report
