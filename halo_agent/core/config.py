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


@dataclass(frozen=True)
class ScoringConfig:
    engine: str
    mission_start: datetime
    baseline_scenarios: tuple[str, ...]
    min_points: int
    mad_scale: float
    no_pool_metrics: frozenset
    mad_floor: dict
    caution_z: float
    alert_z: float
    alert_consecutive: int
    unfavourable: dict
    new_flags: dict           # metric -> "always" | tuple of flag names
    baseline_full_n: float
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
    red_flags: tuple[RedFlag, ...]

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
            mad_scale=float(b["mad_scale"]),
            no_pool_metrics=frozenset(b.get("no_pool_metrics") or ()),
            mad_floor={k: float(v) for k, v in b["mad_floor"].items()},
            caution_z=float(z["caution"]),
            alert_z=float(z["alert"]),
            alert_consecutive=int(z["alert_consecutive"]),
            unfavourable=dict(scoring["unfavourable"]),
            new_flags=flags,
            baseline_full_n=float(c["baseline_full_n"]),
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
            red_flags=tuple(rules),
        )
