"""M3 per-metric rules (AGENTS.md §7.2, §7.4): robust-z bands in the unfavourable direction only,
alert debounce, and absolute red flags. Reason text is templated here (never generated, never a diagnosis)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .baseline import POOLED
from .config import ScoringConfig
from .models import Baseline, Indicator, Reason, VitalsReading
from .trend import compute_trend

STATUS_RANK = {"insufficient": -1, "normal": 0, "caution": 1, "alert": 2}


@dataclass
class MetricEval:
    """Result of evaluating one NEW measurement of one metric. Carried-forward rows reuse it."""
    indicator: Indicator
    reasons: list[Reason] = field(default_factory=list)
    quality: Optional[str] = None      # ok | partial | insufficient; None when the value is missing
    alert_streak: int = 0              # consecutive new readings beyond the alert band, including this one
    building_n: Optional[int] = None   # baseline points available when there is no baseline yet


def is_new_measurement(reading: VitalsReading, metric: str, seen_before: bool, prev_value: Optional[float],
                       cfg: ScoringConfig) -> bool:
    """prev_value = this metric's value on the crew's previous row (None if missing or first row)."""
    v = reading.value(metric)
    if v is None:
        return False
    if not seen_before:                # first value for this metric: nothing to carry forward from
        return True
    if v != prev_value:                # a changed value cannot be a carry-forward, whatever the flags say
        return True
    flags = cfg.new_flags[metric]
    if flags == "always":
        return True
    return any({"bp": reading.new_bp, "temp": reading.new_temp}[f] for f in flags)


def _fmt(x: float) -> str:
    return f"{x:g}" if abs(x - round(x)) > 1e-9 else f"{x:.0f}"


def _unfavourable_z(z: float, metric: str, cfg: ScoringConfig) -> float:
    d = cfg.unfavourable[metric]
    return z if d == "high" else -z if d == "low" else abs(z)


def missing_eval(metric: str, cfg: ScoringConfig) -> MetricEval:
    label = cfg.metric_display.get(metric, {"label": metric})["label"]
    return MetricEval(
        indicator=Indicator(metric=metric, value=None, status="insufficient", deviation_mad=None,
                            baseline=None, trend=None, fresh_minutes=None),
        reasons=[Reason(metric=metric, value=None, baseline_median=None, deviation_mad=None,
                        direction="missing", status="insufficient", source="baseline", rule_id="missing",
                        text=f"No {label} value in this reading. Missing, not assumed normal.")],
    )


def evaluate_new(reading: VitalsReading, metric: str, baseline: Optional[Baseline], quality: str,
                 prev_alert_streak: int, trend_points: list, cfg: ScoringConfig) -> MetricEval:
    """trend_points: (ts, value) of this metric's new measurements in the trend window, same activity
    group, oldest -> newest, ending with this reading."""
    v = reading.value(metric)
    assert v is not None
    disp = cfg.metric_display.get(metric, {"label": metric, "unit": ""})
    label, unit = disp["label"], disp["unit"]
    status, z, reasons, streak = "insufficient", None, [], 0

    if baseline is not None:
        z = (v - baseline.median) / baseline.sigma
        zu = _unfavourable_z(z, metric, cfg)
        streak = prev_alert_streak + 1 if zu >= cfg.alert_z else 0
        if zu >= cfg.alert_z and streak >= cfg.alert_consecutive:
            status = "alert"
        elif zu >= cfg.caution_z:
            status = "caution"           # includes a first reading beyond the alert band (debounce cap)
        else:
            status = "normal"
        if status != "normal":
            where = "your all-activity baseline" if baseline.group == POOLED else "your baseline"
            group = "" if baseline.group == POOLED else baseline.group.replace("_", "-").capitalize() + " "
            lead = f"{group}{label}" if group else label[0].upper() + label[1:]
            text = (f"{lead} {_fmt(v)} {unit} is {abs(z):.1f} robust SD "
                    f"{'above' if z > 0 else 'below'} {where} ({_fmt(baseline.median)} {unit}).")
            if zu >= cfg.alert_z and status == "caution":
                text += f" Beyond the alert band once; alert needs {cfg.alert_consecutive} new readings in a row."
            reasons.append(Reason(metric=metric, value=v, baseline_median=baseline.median,
                                  deviation_mad=round(z, 2), direction="high" if z > 0 else "low",
                                  status=status, source="baseline", rule_id="z_band", text=text))

    trend, unfav = compute_trend(trend_points, v, baseline, metric, cfg)
    if unfav:
        # An unfavourable trend adds a caution even when the single reading is inside the band (§7.3).
        if STATUS_RANK[status] < STATUS_RANK["caution"]:
            status = "caution"
        span = f"{cfg.trend_window:g} {cfg.trend_unit}"
        group = "" if baseline.group == POOLED else baseline.group.replace("_", "-").capitalize() + " "
        lead = f"{group}{label}" if group else label[0].upper() + label[1:]
        text = (f"{lead} is {trend.direction} about {abs(trend.slope_per_day):g} {unit} per day "
                f"over the last {span}.")
        if trend.days_to_alert is not None:
            text += f" At this pace it reaches the alert band in about {trend.days_to_alert:g} days."
        reasons.append(Reason(metric=metric, value=v, baseline_median=baseline.median,
                              deviation_mad=round(z, 2), direction="trend_up" if trend.slope_per_day > 0
                              else "trend_down", status="caution", source="baseline", rule_id="trend",
                              text=text))

    for rf in cfg.red_flags:
        if rf.metric == metric and rf.fires(v):
            status = "alert"             # red flags override the baseline (§7.4); no debounce
            note = "" if rf.verified else " Source not yet verified."
            reasons.append(Reason(
                metric=metric, value=v, baseline_median=baseline.median if baseline else None,
                deviation_mad=round(z, 2) if z is not None else None,
                direction="low" if rf.op.startswith("<") else "high", status="alert", source=rf.source,
                rule_id=rf.rule_id,
                text=f"{label[0].upper() + label[1:]} {_fmt(v)} {unit} is {rf.op} the red-flag line "
                     f"{_fmt(rf.value)} {unit} (rule {rf.rule_id}, {rf.source}).{note}"))

    return MetricEval(
        indicator=Indicator(metric=metric, value=v, status=status,
                            deviation_mad=round(z, 2) if z is not None else None,
                            baseline=baseline, trend=trend, fresh_minutes=0.0),
        reasons=reasons, quality=quality, alert_streak=streak)
