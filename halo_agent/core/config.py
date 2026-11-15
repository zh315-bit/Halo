"""Typed scoring config. Built from plain dicts so core stays stdlib-only (YAML is parsed in
halo_agent/config_loader.py). No numeric defaults live here: config/*.yaml is the single source."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .models import VITAL_METRICS

_OPS = {
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
}


@dataclass(frozen=True)
class RedFlag:
    rule_id: str
    metric: str
    op: str
    value: float
    source: str
    source_note: str
    verified: bool

    def fires(self, x: Optional[float]) -> bool:
        return x is not None and _OPS[self.op](x, self.value)


def _deviation_words(rows: list) -> tuple:
    out = tuple((None if r.get("below") is None else float(r["below"]), str(r.get("word") or "")) for r in rows)
    limits = [b for b, _ in out if b is not None]
    if limits != sorted(limits) or (out and out[-1][0] is not None) or any(b is None for b, _ in out[:-1]):
        raise ValueError("scoring.yaml deviation_words: ascending `below` limits, last band without a limit")
    return out


@dataclass(frozen=True)
class ScoringConfig:
    engine: str
    mission_start: datetime
    baseline_scenarios: tuple[str, ...]
    min_points: int
    min_points_by_origin: dict    # data_origin -> min_points override (live camera scans: 3)
    live_data_origins: frozenset  # data_origins that are real measurements ("live" crews)
    mad_scale: float
    no_pool_metrics: frozenset
    mad_floor: dict
    caution_z: float
    alert_z: float
    alert_consecutive: int
    unfavourable: dict
    new_flags: dict           # metric -> "always" | tuple of flag names
    baseline_full_n: float
    baseline_full_n_by_origin: dict
    measurable_metrics: dict      # data_origin -> tuple of vitals it can measure (default: all)
    fresh_full_minutes: float
    fresh_zero_minutes: float
    trend_window: float
    trend_unit: str
    trend_min_steps: int
    trend_min_points_per_step: int
    trend_slope_sigma_per_day: float
    trend_min_slope_t: Optional[float]
    days_to_alert_max: float
    data_origin: dict
    metric_display: dict
    deviation_words: tuple        # ((below |z| or None, word), ...) for reason text, ascending
    red_flags: tuple[RedFlag, ...]

    def min_points_for(self, data_origin: str) -> int:
        return self.min_points_by_origin.get(data_origin, self.min_points)

    def measurable_for(self, data_origin: str) -> tuple:
        return self.measurable_metrics.get(data_origin, VITAL_METRICS)

    def baseline_full_n_for(self, data_origin: str) -> float:
        return self.baseline_full_n_by_origin.get(data_origin, self.baseline_full_n)

    def is_live(self, data_origin: str) -> bool:
        return data_origin in self.live_data_origins

    @classmethod
    def from_dicts(cls, scoring: dict, red_flags: dict) -> "ScoringConfig":
        b, z, c, t = scoring["baseline"], scoring["z_bands"], scoring["confidence"], scoring["trend"]
        if t["unit"] not in ("days", "hours", "readings"):
            raise ValueError("trend.unit must be days | hours | readings")
        for m in VITAL_METRICS:
            for section in ("mad_floor",):
                if m not in b[section]:
                    raise ValueError(f"scoring.yaml baseline.{section} missing {m}")
            if m not in scoring["unfavourable"] or m not in scoring["new_measurement_flags"]:
                raise ValueError(f"scoring.yaml missing unfavourable/new_measurement_flags for {m}")
        for origin, ms in (scoring.get("measurable_metrics") or {}).items():
            if not ms or any(m not in VITAL_METRICS for m in ms):
                raise ValueError(f"scoring.yaml measurable_metrics.{origin}: unknown or empty vitals")
        flags = {}
        for m, v in scoring["new_measurement_flags"].items():
            flags[m] = "always" if v == "always" else tuple(v)
        rules = []
        for r in (red_flags or {}).get("rules", []):
            # AGENTS.md §3.1: a rule without a source never runs, whatever `enabled` says.
            if not r.get("enabled", False) or not r.get("source"):
                continue
            if r["op"] not in _OPS or r["metric"] not in VITAL_METRICS:
                raise ValueError(f"bad red flag {r.get('rule_id')}")
            rules.append(RedFlag(rule_id=r["rule_id"], metric=r["metric"], op=r["op"], value=float(r["value"]),
                                 source=r["source"], source_note=r.get("source_note", ""),
                                 verified=bool(r.get("verified", False))))
        return cls(
            engine=scoring["engine"],
            mission_start=datetime.fromisoformat(scoring["mission_start"].replace("Z", "+00:00")),
            baseline_scenarios=tuple(scoring["baseline_scenarios"]),
            min_points=int(b["min_points"]),
            min_points_by_origin={k: int(v) for k, v in (b.get("min_points_by_origin") or {}).items()},
            live_data_origins=frozenset(scoring.get("live_data_origins") or ()),
            mad_scale=float(b["mad_scale"]),
            no_pool_metrics=frozenset(b.get("no_pool_metrics") or ()),
            mad_floor={k: float(v) for k, v in b["mad_floor"].items()},
            caution_z=float(z["caution"]),
            alert_z=float(z["alert"]),
            alert_consecutive=int(z["alert_consecutive"]),
            unfavourable=dict(scoring["unfavourable"]),
            new_flags=flags,
            baseline_full_n=float(c["baseline_full_n"]),
            baseline_full_n_by_origin={k: float(v) for k, v in (c.get("baseline_full_n_by_origin") or {}).items()},
            measurable_metrics={k: tuple(v) for k, v in (scoring.get("measurable_metrics") or {}).items()},
            fresh_full_minutes=float(c["fresh_full_minutes"]),
            fresh_zero_minutes=float(c["fresh_zero_minutes"]),
            trend_window=float(t["window"]),
            trend_unit=t["unit"],
            trend_min_steps=int(t["min_steps"]),
            trend_min_points_per_step=int(t["min_points_per_step"]),
            trend_slope_sigma_per_day=float(t["slope_sigma_per_day"]),
            trend_min_slope_t=None if t.get("min_slope_t") is None else float(t["min_slope_t"]),
            days_to_alert_max=float(t["days_to_alert_max"]),
            data_origin=dict(scoring["data_origin"]),
            metric_display=dict(scoring["metrics"]),
            deviation_words=_deviation_words(scoring.get("deviation_words") or []),
            red_flags=tuple(rules),
        )
