"""M1 adapter: QR demo CSV row -> core VitalsReading (stdlib only). Used by the CLI replay and by M1 (Dhruv).

Column names verified against the real header (AGENTS.md §5). Mapping:
  demo_record_id                    -> reading_id
  mission_start + (mission_day − 1) days + clock_time -> timestamp   (⚑ the CSV has no calendar date)
  bp_new_reading / temp_new_reading -> new_reading.bp / new_reading.temp
  scenario in baseline_scenarios    -> in_baseline_phase
Rows already in §6.2 shape (reading_id, timestamp, new_reading) are accepted as well. Values may be CSV
strings or JSON numbers/bools. Empty / "nan" stays missing (§3.5).
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .config import ScoringConfig
from .models import OPTIONAL_METRICS, VITAL_METRICS, VitalsReading


def _num(x) -> Optional[float]:
    if x is None or isinstance(x, bool):
        return None
    if isinstance(x, (int, float)):
        return None if x != x else float(x)          # NaN -> missing
    s = str(x).strip()
    if s == "" or s.lower() in ("nan", "na", "null", "none"):
        return None
    return float(s)


def _bool(x) -> bool:
    if isinstance(x, bool):
        return x
    return str(x or "").strip().lower() in ("true", "1", "yes")


def _cfg(cfg: Optional[ScoringConfig]) -> ScoringConfig:
    if cfg is not None:
        return cfg
    from . import get_engine                         # process-wide engine installed at API startup
    eng = get_engine()
    if eng is None:
        raise RuntimeError("reading_from_csv_row needs cfg= when no core Engine is installed")
    return eng.cfg


def reading_from_csv_row(row: dict, cfg: Optional[ScoringConfig] = None) -> VitalsReading:
    """One CSV / dict row -> VitalsReading. Raises KeyError / ValueError on a row that can't be placed."""
    cfg = _cfg(cfg)
    if row.get("timestamp"):
        ts = datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            raise ValueError(f"timestamp {row['timestamp']!r} needs a timezone")
    else:
        hh, mm = str(row["clock_time"]).split(":")
        ts = cfg.mission_start + timedelta(days=int(row["mission_day"]) - 1, hours=int(hh), minutes=int(mm))
    nr = row.get("new_reading")
    if isinstance(nr, dict):
        new_bp, new_temp = _bool(nr.get("bp")), _bool(nr.get("temp"))
    else:
        new_bp, new_temp = _bool(row.get("bp_new_reading")), _bool(row.get("temp_new_reading"))
    return VitalsReading(
        reading_id=str(row.get("reading_id") or row["demo_record_id"]),
        crew_id=str(row["crew_id"]), timestamp=ts, mission_day=int(row["mission_day"]),
        activity=str(row["activity"]),
        **{m: _num(row.get(m)) for m in VITAL_METRICS + OPTIONAL_METRICS},
        new_bp=new_bp, new_temp=new_temp,
        measurement_origin=str(row["measurement_origin"]), display_label=str(row["display_label"]),
        in_baseline_phase=row.get("scenario", "baseline") in cfg.baseline_scenarios,
    )


def reading_to_wire(r: VitalsReading) -> dict:
    """Core reading -> §6.2 JSON (what POST /api/v1/score and the WS feed carry)."""
    from .score import iso_z
    return {
        "reading_id": r.reading_id, "crew_id": r.crew_id, "timestamp": iso_z(r.timestamp),
        "mission_day": r.mission_day, "activity": r.activity,
        **{m: r.value(m) for m in VITAL_METRICS + OPTIONAL_METRICS},
        "new_reading": {"bp": r.new_bp, "temp": r.new_temp},
        "measurement_origin": r.measurement_origin, "display_label": r.display_label,
    }


def load_csv(path: str | Path, cfg: Optional[ScoringConfig] = None) -> list[VitalsReading]:
    """Lines starting with '#' are comments (generated files carry a SIMULATED note there)."""
    with open(path, newline="", encoding="utf-8") as f:
        lines = (line for line in f if not line.startswith("#"))
        return [reading_from_csv_row(r, cfg) for r in csv.DictReader(lines)]
