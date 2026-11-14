"""Camera scan ingest: POST /api/v1/scan, POST /api/v1/scan/upload (file fallback), GET /api/v1/crews.

Scan records come from the team's scan page. Each one becomes a core VitalsReading (core.reading_from_scan)
and goes through the live Engine.update, the same path as every other feed, so SOS, score and the daily
report all see it. A live crew has its own baseline and never mixes with demo/simulated data (409)."""

from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from .. import core
from ..config_loader import load_scan_dict, load_scoring_config
from ..core.csv_source import reading_to_wire
from ..core.scan_source import ScanConfig, reading_from_scan
from .replay import _engine
from .schemas import CrewList, ScanIngestRequest, ScanIngestResult, ScanUploadResponse

router = APIRouter(prefix="/api/v1", tags=["scan"])
MAX_UPLOAD_BYTES = 2_000_000


def scan_config() -> ScanConfig:
    return ScanConfig.from_dict(load_scan_dict())


def ingest(record: dict, crew_id: Optional[str]) -> dict:
    eng = _engine()
    try:
        r = reading_from_scan(record, crew_id, scan_config(), eng.cfg)
    except (ValueError, TypeError) as e:
        raise HTTPException(422, f"bad scan record: {e}")
    for old, sc in reversed(eng.history(r.crew_id)):
        if old.reading_id == r.reading_id:          # the page may retry: idempotent, nothing re-scored
            return {"accepted": True, "duplicate": True, "crew_id": r.crew_id, "reading_id": r.reading_id,
                    "reading": reading_to_wire(old), "score": sc.to_dict()}
    try:
        res = eng.update(r)
    except ValueError as e:                         # older than the crew's last reading, or a synthetic crew_id
        raise HTTPException(409, str(e))
    return {"accepted": True, "duplicate": False, "crew_id": r.crew_id, "reading_id": r.reading_id,
            "reading": reading_to_wire(r), "score": res.to_dict()}


@router.post("/scan", response_model=ScanIngestResult)
def post_scan(req: ScanIngestRequest) -> dict:
    return ingest(req.record, req.crew_id)


@router.post("/scan/upload", response_model=ScanUploadResponse)
async def upload_scan(file: UploadFile = File(...), crew_id: Optional[str] = Form(None)) -> dict:
    """A downloaded scan JSON: one record, {"crew_id", "record"}, or a list of records (ingested oldest first)."""
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "scan file too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(422, f"not a JSON file: {e}")
    if isinstance(data, dict) and isinstance(data.get("record"), dict):
        crew_id, records = crew_id or data.get("crew_id"), [data["record"]]
    elif isinstance(data, dict):
        records = [data]
    elif isinstance(data, list) and all(isinstance(x, dict) for x in data) and data:
        records = sorted(data, key=lambda x: str(x.get("created_at") or ""))
    else:
        raise HTTPException(422, "expected a scan record, {crew_id, record} or a list of records")
    results = [ingest(rec, crew_id) for rec in records]
    return {"accepted": len(results), "results": results}


@router.get("/crews", response_model=CrewList)
def crews() -> dict:
    """Every crew the live Engine knows, labelled synthetic (demo playback / SIMULATED) or live (camera scans).
    The configured live crew is always listed, with 0 readings until its first scan."""
    eng = core.get_engine()
    out, cfg = [], load_scoring_config()
    for cid in (eng.crews() if eng else []):
        hist = eng.history(cid)
        last_r, last_s = hist[-1] if hist else (None, None)
        out.append({"crew_id": cid, "source": "live" if eng.is_live(cid) else "synthetic",
                    "data_origin": last_s.data_origin if last_s else None, "readings": len(hist),
                    "latest_reading_id": last_s.reading_id if last_s else None,
                    "latest_as_of": last_s.as_of if last_s else None,
                    "latest_status": last_s.status if last_s else None,
                    "display_label": last_r.display_label if last_r else None,
                    "baseline_building": last_s.baseline_building if last_s else None})
    live_id = scan_config().live_crew_id
    if all(c["crew_id"] != live_id for c in out):
        origin = scan_config().measurement_origin
        out.append({"crew_id": live_id, "source": "live", "data_origin": cfg.data_origin.get(origin),
                    "readings": 0, "latest_reading_id": None, "latest_as_of": None, "latest_status": None,
                    "display_label": scan_config().display_label, "baseline_building": None})
    return {"crews": out}
