"""Live camera-scan data in the SOS (AGENTS.md §8.6). Experimental, and always labelled as such.

Interface expected from core (main, `POST /api/v1/scan`):
- `latest_reading(crew_id)` for a live crew (e.g. LIVE_01) returns a reading whose `data_origin` (or
  `measurement_origin`) is `camera_scan`; vitals the camera cannot measure are None.
- `latest_scan(crew_id)` returns the crew's last scan record as posted by the scan page (field names as in
  tests/fixtures/scan_record_sample.json), or None. If core has no `latest_scan`, a `scan_context` attribute on the
  reading is used instead. Anything missing -> no scan context; the SOS flow never depends on it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

CAMERA_ORIGIN = "camera_scan"
LIVE_LABEL = "Live camera measurement (experimental)"
NOT_BY_CAMERA_TEXT = "Not measured by camera scan"
NOT_BY_CAMERA = ("spo2_pct", "systolic_bp_mmhg", "diastolic_bp_mmhg", "body_temp_c")
OTHER_LABEL = "Replayed demo or simulated data (not a live measurement)"


def default_scan_fn(crew_id: str):
    try:
        from halo_agent.core import latest_scan
    except ImportError:
        return None
    try:
        return latest_scan(crew_id)
    except Exception:
        return None


def _get(obj, key, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def is_camera(reading) -> bool:
    return CAMERA_ORIGIN in (_get(reading, "data_origin"), _get(reading, "measurement_origin"))


def summarize(record, now_iso: str) -> Optional[dict]:
    """Raw scan record (or an already-summarised dict) -> the scan_context shown in the SOS."""
    if not record or not isinstance(record, dict):
        return None
    if "heart_rate_bpm" in record and "label" in record:          # already a summary
        return dict(record)
    scanned, now = _parse(record.get("created_at")), _parse(now_iso)
    age = round((now - scanned).total_seconds()) if scanned and now else None
    return {
        "label": LIVE_LABEL,
        "data_origin": CAMERA_ORIGIN,
        "scan_id": record.get("session_id"),
        "scanned_at": record.get("created_at"),
        "age_seconds": age,
        "heart_rate_bpm": record.get("heart_bpm"),          # None when the scan found it unreliable
        "heart_quality": record.get("heart_quality"),       # signal quality 0..1
        "breathing_brpm": record.get("breathing_brpm"),
        "breathing_quality": record.get("breathing_quality"),
        "reaction_median_ms": record.get("reaction_median_ms"),
        "verdict": record.get("verdict"),
        "scan_grade": record.get("scan_grade"),
        "notes": list(record.get("notes") or []),
    }
