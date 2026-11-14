"""Latest vitals for the SOS "Display" step (AGENTS.md §8.2.3). Source of truth: the live Engine.

`from halo_agent.core import latest_reading` returns the last reading the Engine processed (a VitalsReading plus
`measured_at` / `metric_age_seconds` per vital and `age_seconds`), the same reading `score_latest` scored. The SOS
keeps no clock or CSV replay of its own. No reading (or no core support yet) -> every vital `missing`, flow continues.
Missing stays missing (§3.5): no defaults.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Optional

STALE_AFTER_S = 10 * 60   # team_derived: SOS display threshold
METRICS = ("heart_rate_bpm", "resp_rate_bpm", "systolic_bp_mmhg", "diastolic_bp_mmhg",
           "body_temp_c", "spo2_pct", "blink_rate_per_min")


def default_reading_fn(crew_id: str):
    """The only reading entry point (owned by core). Any failure -> None."""
    try:
        from halo_agent.core import latest_reading
    except ImportError:
        return None
    try:
        return latest_reading(crew_id)
    except Exception:
        return None


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _iso(ts) -> Optional[str]:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(ts)


def _origin(measurement_origin: Optional[str]) -> Optional[str]:
    if measurement_origin is None:
        return None
    return "demo_playback" if measurement_origin == "master_csv_playback_not_camera" else measurement_origin


def empty_snapshot() -> dict:
    return {"reading_id": None, "timestamp": None, "age_seconds": None, "display_label": None, "data_origin": None,
            "stale": True, "metrics": {m: {"value": None, "measured_at": None, "age_seconds": None,
                                           "status": "missing"} for m in METRICS}}


def snapshot(reading, stale_after_s: float = STALE_AFTER_S) -> dict:
    """core.LatestReading (or a dict with the same keys) -> the §6.4 vitals_snapshot."""
    if reading is None or not _get(reading, "reading_id"):
        return empty_snapshot()
    measured = _get(reading, "measured_at") or {}
    ages = _get(reading, "metric_age_seconds") or {}
    metrics = {}
    for m in METRICS:
        v = _get(reading, m)
        if v is None or (isinstance(v, float) and v != v):     # None or NaN
            metrics[m] = {"value": None, "measured_at": None, "age_seconds": None, "status": "missing"}
            continue
        age = ages.get(m)
        status = "stale" if age is None or age > stale_after_s else "ok"   # unknown age is never "ok"
        metrics[m] = {"value": float(v), "measured_at": _iso(measured.get(m)),
                      "age_seconds": None if age is None else round(float(age)), "status": status}
    age = _get(reading, "age_seconds")
    return {"reading_id": _get(reading, "reading_id"), "timestamp": _iso(_get(reading, "timestamp")),
            "age_seconds": None if age is None else round(float(age)),
            "display_label": _get(reading, "display_label") or None,
            "data_origin": _origin(_get(reading, "measurement_origin")),
            "stale": age is None or float(age) > stale_after_s, "metrics": metrics}


def as_dict(obj) -> Optional[dict]:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    return None
