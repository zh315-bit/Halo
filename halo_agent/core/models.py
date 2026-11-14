"""Core data types (stdlib dataclasses, AGENTS.md §3.9). Field names mirror §6.2 / §6.3 exactly;
halo_agent/api/schemas.py holds the pydantic twins used at the HTTP boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional

VITAL_METRICS = ("heart_rate_bpm", "resp_rate_bpm", "systolic_bp_mmhg",
                 "diastolic_bp_mmhg", "body_temp_c", "spo2_pct")
OPTIONAL_METRICS = ("blink_rate_per_min",)  # no column in the demo data yet (⚑); reported, never scored


@dataclass(frozen=True)
class VitalsReading:
    reading_id: str
    crew_id: str
    timestamp: datetime            # timezone-aware
    mission_day: int
    activity: str
    heart_rate_bpm: Optional[float] = None
    resp_rate_bpm: Optional[float] = None
    systolic_bp_mmhg: Optional[float] = None
    diastolic_bp_mmhg: Optional[float] = None
    body_temp_c: Optional[float] = None
    spo2_pct: Optional[float] = None
    blink_rate_per_min: Optional[float] = None
    new_bp: bool = False
    new_temp: bool = False
    measurement_origin: str = ""
    display_label: str = ""
    # Not part of the §6.2 wire contract: True while the crew is in its baseline phase (§7.1).
    in_baseline_phase: bool = True
    # Camera-scan extras kept verbatim (test scores, self-report, the scan page's own verdict). Never scored.
    scan_context: Optional[dict] = field(default=None, compare=False, hash=False)

    def value(self, metric: str) -> Optional[float]:
        return getattr(self, metric)


@dataclass(frozen=True)
class LatestReading(VitalsReading):
    """The last reading the Engine processed for a crew, plus when each vital was last NEWLY measured.
    It is a VitalsReading, so callers that only need the values can ignore the extra fields."""
    measured_at: dict = field(default_factory=dict)         # vital -> ISO8601 of its last new measurement
    metric_age_seconds: dict = field(default_factory=dict)  # vital -> seconds since that measurement (None = missing)
    age_seconds: float = 0.0                                # seconds since this reading's timestamp


@dataclass
class Baseline:
    median: float
    sigma: float
    n: int
    group: str                     # activity name, or "pooled" for the cross-activity fallback


@dataclass
class Trend:
    direction: str                 # rising | falling | stable
    slope_per_day: float
    days_to_alert: Optional[float] = None


@dataclass
class Indicator:
    metric: str
    value: Optional[float]
    status: str                    # normal | caution | alert | insufficient
    deviation_mad: Optional[float]
    baseline: Optional[Baseline]
    trend: Optional[Trend]
    fresh_minutes: Optional[float]


@dataclass
class Reason:
    metric: str
    value: Optional[float]
    baseline_median: Optional[float]
    deviation_mad: Optional[float]
    direction: str                 # high | low | trend_up | trend_down | missing
    status: str
    source: str                    # nasa | guideline | team_derived | baseline
    rule_id: str
    text: str


@dataclass
class ScoreResult:
    crew_id: str
    reading_id: str
    as_of: str
    status: str
    severity: int
    confidence: float
    baseline_quality: str
    engine: str
    data_origin: str
    indicators: list[Indicator] = field(default_factory=list)
    reasons: list[Reason] = field(default_factory=list)
    # {"n", "needed", "label": "Baseline building (n/needed)"} while a present vital has too few baseline points.
    baseline_building: Optional[dict] = None

    def to_dict(self) -> dict:
        return asdict(self)
