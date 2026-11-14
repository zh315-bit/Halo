"""M1 adapter for the team's camera scan page: one scan record (JSON, `scans` table) -> core VitalsReading.

Stdlib only. Mapping (AGENTS.md §5):
  session_id                 -> reading_id
  created_at                 -> timestamp (must carry a timezone)
  heart_bpm                  -> heart_rate_bpm, only if heart_quality >= min_heart_quality (else missing)
  breathing_brpm             -> resp_rate_bpm, only if breathing_quality >= min_breathing_quality (else missing)
  blink_rate_per_min         -> blink_rate_per_min (reported, never scored)
  SpO2, BP, body temperature -> missing: the camera does not measure them, never assumed normal
  test scores, self-report, the page's own verdict / readiness_score / flags -> scan_context (never scored)
Every value in a scan is a fresh measurement, so both new-reading flags are set.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .config import ScoringConfig
from .csv_source import _num
from .models import VitalsReading


@dataclass(frozen=True)
class ScanConfig:
    live_crew_id: str
    min_heart_quality: float
    min_breathing_quality: float
    activity: str
    measurement_origin: str
    display_label: str
    context_fields: tuple[str, ...]

    @classmethod
    def from_dict(cls, d: dict) -> "ScanConfig":
        return cls(live_crew_id=str(d["live_crew_id"]), min_heart_quality=float(d["min_heart_quality"]),
                   min_breathing_quality=float(d["min_breathing_quality"]),
                   activity=str(d["activity"]), measurement_origin=str(d["measurement_origin"]),
                   display_label=str(d["display_label"]), context_fields=tuple(d["context_fields"]))


def _ts(x) -> datetime:
    if not x:
        raise ValueError("scan record needs created_at")
    ts = datetime.fromisoformat(str(x).replace("Z", "+00:00"))
    if ts.tzinfo is None:
        raise ValueError(f"created_at {x!r} needs a timezone")
    return ts


def reading_from_scan(record: dict, crew_id: Optional[str], cfg: ScanConfig,
                      scoring: ScoringConfig) -> VitalsReading:
    """Raises ValueError on a record that can't be placed (no session_id / created_at, bad numbers)."""
    if not isinstance(record, dict):
        raise ValueError("scan record must be a JSON object")
    sid = record.get("session_id") or record.get("id")
    if not sid:
        raise ValueError("scan record needs session_id")
    ts = _ts(record.get("created_at"))
    excluded = {}

    def gated(value_key: str, quality_key: str, minimum: float, metric: str) -> Optional[float]:
        v, q = _num(record.get(value_key)), _num(record.get(quality_key))
        if v is not None and (q is None or q < minimum):
            excluded[metric] = f"{quality_key} {q:g} < {minimum:g}" if q is not None else f"{quality_key} missing"
            return None
        return v

    quality = _num(record.get("heart_quality"))
    hr = gated("heart_bpm", "heart_quality", cfg.min_heart_quality, "heart_rate_bpm")
    resp = gated("breathing_brpm", "breathing_quality", cfg.min_breathing_quality, "resp_rate_bpm")
    context = {k: record.get(k) for k in cfg.context_fields}
    context.update(source="scan_app", scan_session_id=str(sid), app_version=record.get("app_version"),
                   heart_quality=quality, breathing_quality=_num(record.get("breathing_quality")),
                   excluded=excluded)
    return VitalsReading(
        reading_id=str(sid), crew_id=crew_id or cfg.live_crew_id, timestamp=ts,
        mission_day=(ts.date() - scoring.mission_start.date()).days + 1, activity=cfg.activity,
        heart_rate_bpm=hr, resp_rate_bpm=resp,
        systolic_bp_mmhg=None, diastolic_bp_mmhg=None, body_temp_c=None, spo2_pct=None,
        blink_rate_per_min=_num(record.get("blink_rate_per_min")),
        new_bp=True, new_temp=True,
        measurement_origin=cfg.measurement_origin, display_label=cfg.display_label,
        in_baseline_phase=True, scan_context=context, raw_scan=copy.deepcopy(record))
