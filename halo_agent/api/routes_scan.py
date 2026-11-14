"""Camera scan ingest: POST /api/v1/scan, POST /api/v1/scan/upload (file fallback), GET /api/v1/crews.

Scan records come from the team's scan page. Each one becomes a core VitalsReading (core.reading_from_scan)
and goes through the live Engine.update, the same path as every other feed, so SOS, score and the daily
report all see it. A live crew has its own baseline and never mixes with demo/simulated data (409).

Persistence: every accepted (non-duplicate) scan is appended to data/live_scans.jsonl (env HALO_LIVE_SCANS_FILE)
as {"crew_id", "received_at", "record"}; at startup the file is fed back in order, so live baselines survive a
restart. Replay resets never touch it; DELETE /api/v1/admin/live-scans clears it."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile

from .. import core
from ..config_loader import REPO_ROOT, load_scan_dict, load_scoring_config
from ..core.csv_source import reading_to_wire
from ..core.scan_source import ScanConfig, reading_from_scan
from .replay import _engine
from .schemas import CrewList, LiveScansCleared, ScanIngestResult, ScanUploadResponse

router = APIRouter(prefix="/api/v1", tags=["scan"])
MAX_UPLOAD_BYTES = 2_000_000
log = logging.getLogger("halo.scan")
_file_lock = threading.Lock()


def scans_file() -> Path:
    return Path(os.environ.get("HALO_LIVE_SCANS_FILE") or REPO_ROOT / "data" / "live_scans.jsonl")


def _append(crew_id: str, record: dict) -> None:
    line = json.dumps({"crew_id": crew_id, "received_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                       "record": record}, ensure_ascii=False)
    f = scans_file()
    with _file_lock:
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


def _read_lines() -> list[dict]:
    f = scans_file()
    if not f.is_file():
        return []
    out = []
    for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("%s line %d is not JSON; skipped", f.name, n)
    return out


def load_persisted() -> int:
    """Startup: feed data/live_scans.jsonl back into the live Engine, oldest line first. Bad lines are logged."""
    n = 0
    for row in _read_lines():
        try:
            if not ingest(row["record"], row.get("crew_id"), persist=False)["duplicate"]:
                n += 1
        except (HTTPException, KeyError, TypeError) as e:
            log.warning("persisted scan skipped: %s", getattr(e, "detail", e))
    if n:
        log.info("live scans: %d restored from %s", n, scans_file())
    return n


def scan_config() -> ScanConfig:
    return ScanConfig.from_dict(load_scan_dict())


def ingest(record: dict, crew_id: Optional[str], persist: bool = True) -> dict:
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
    if persist:
        _append(r.crew_id, record)
    return {"accepted": True, "duplicate": False, "crew_id": r.crew_id, "reading_id": r.reading_id,
            "reading": reading_to_wire(r), "score": res.to_dict()}


@router.post("/scan", response_model=ScanIngestResult)
def post_scan(body: dict = Body(..., examples=[{"crew_id": "LIVE_01", "record": {"session_id": "...",
                                                                               "created_at": "..."}}])) -> dict:
    """Body `{"crew_id"?, "record": {...}}`, or the scan record itself with an optional `crew_id` key."""
    if isinstance(body.get("record"), dict):
        record, crew_id = body["record"], body.get("crew_id")
    else:
        record = {k: v for k, v in body.items() if k != "crew_id"}
        crew_id = body.get("crew_id")
    if crew_id is not None and not isinstance(crew_id, str):
        raise HTTPException(422, "crew_id must be a string")
    return ingest(record, crew_id)


@router.delete("/admin/live-scans", response_model=LiveScansCleared)
def delete_live_scans(crew_id: Optional[str] = Query(None, description="one live crew; omit = every live crew")) -> dict:
    """Remove persisted scans (data/live_scans.jsonl) and forget those live crews in the Engine."""
    eng = _engine()
    if crew_id is not None and eng.is_live(crew_id) is False:
        raise HTTPException(409, f"{crew_id} is not a live crew")
    with _file_lock:
        rows = _read_lines()
        keep = [x for x in rows if crew_id is not None and x.get("crew_id") != crew_id]
        f = scans_file()
        if f.is_file():
            tmp = f.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in keep), encoding="utf-8")
            tmp.replace(f)
    crews = [crew_id] if crew_id is not None else [c for c in eng.crews() if eng.is_live(c)]
    for c in crews:
        eng.reset(c)
    return {"cleared_crews": sorted(set(crews)), "removed_records": len(rows) - len(keep), "file": str(scans_file())}


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
