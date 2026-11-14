from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException

from ..config_loader import load_scoring_config
from ..core.models import VitalsReading as CoreReading
from ..core.score import score as score_batch
from .schemas import ScoreRequest, ScoreResult, VitalsReading

router = APIRouter(prefix="/api/v1", tags=["score"])


def _ts(s: str) -> datetime:
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, f"bad timestamp {s!r}")
    if t.tzinfo is None:
        raise HTTPException(422, f"timestamp {s!r} needs a timezone (use Z)")
    return t


def to_core(r: VitalsReading, baseline_until: datetime | None) -> CoreReading:
    ts = _ts(r.timestamp)
    return CoreReading(
        reading_id=r.reading_id, crew_id=r.crew_id, timestamp=ts, mission_day=r.mission_day, activity=r.activity,
        heart_rate_bpm=r.heart_rate_bpm, resp_rate_bpm=r.resp_rate_bpm, systolic_bp_mmhg=r.systolic_bp_mmhg,
        diastolic_bp_mmhg=r.diastolic_bp_mmhg, body_temp_c=r.body_temp_c, spo2_pct=r.spo2_pct,
        blink_rate_per_min=r.blink_rate_per_min, new_bp=r.new_reading.bp, new_temp=r.new_reading.temp,
        measurement_origin=r.measurement_origin, display_label=r.display_label,
        in_baseline_phase=baseline_until is None or ts <= baseline_until)


@router.post("/score", response_model=ScoreResult)
def score(req: ScoreRequest) -> dict:
    """Score the LAST reading of the window. Stateless: the baseline comes from this window only."""
    until = _ts(req.baseline_until) if req.baseline_until else None
    readings = [to_core(r, until) for r in req.readings]
    if len({r.crew_id for r in readings}) != 1:
        raise HTTPException(422, "a score window must contain one crew_id")
    try:
        return score_batch(readings, load_scoring_config())[-1].to_dict()
    except ValueError as e:          # unknown measurement_origin, out-of-order timestamps
        raise HTTPException(422, str(e))
