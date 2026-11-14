"""Latest vitals for the SOS "Display" step (AGENTS.md §8.2.2), from the QR demo CSV playback.

Until M1 (Dhruv) exposes a latest-reading interface, the SOS reads `data/qr_scan_demo_clean.csv` itself.

- Timestamp: mission_start + (mission_day − 1) days + clock_time (AGENTS.md §5, ⚑).
- Replay clock: "now" starts at the last CSV row and advances with the wall clock, so ages grow during a
  live demo and readings go stale after STALE_AFTER_S. Tests inject `now` directly.
- Per-metric age uses the metric's LAST NEW MEASUREMENT, not the carried-forward row:
  HR every row; SBP/DBP/resp/SpO2 when bp_new_reading or temp_new_reading; temp when temp_new_reading.
- Missing stays missing (value None, status `missing`). No defaults (§3.5).
"""

from __future__ import annotations

import csv
import math
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

DATA = Path(__file__).resolve().parents[2] / "data" / "qr_scan_demo_clean.csv"
STALE_AFTER_S = 10 * 60   # team_derived: SOS display threshold
METRICS = ("heart_rate_bpm", "resp_rate_bpm", "systolic_bp_mmhg", "diastolic_bp_mmhg",
           "body_temp_c", "spo2_pct", "blink_rate_per_min")
_BP_GROUP = ("systolic_bp_mmhg", "diastolic_bp_mmhg", "resp_rate_bpm", "spo2_pct")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _num(v) -> Optional[float]:
    if v is None or str(v).strip() == "":
        return None
    try:
        f = float(v)
    except ValueError:
        return None
    return None if math.isnan(f) else f


def _flag(v) -> bool:
    return str(v).strip().lower() == "true"


def mission_start() -> datetime:
    return parse_iso(os.environ.get("HALO_MISSION_START", "2026-11-15T00:00:00Z"))


def load_rows(path: Path = DATA, start: Optional[datetime] = None) -> list[dict]:
    """CSV -> rows with a parsed `ts`, sorted by time. Values parsed with _num (blank/NaN -> None)."""
    start = start or mission_start()
    out = []
    with open(path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            hh, mm = (int(x) for x in r["clock_time"].split(":"))
            ts = start + timedelta(days=int(r["mission_day"]) - 1, hours=hh, minutes=mm)
            row = {"reading_id": r["demo_record_id"], "crew_id": r["crew_id"], "ts": ts,
                   "activity": r.get("activity"), "measurement_origin": r.get("measurement_origin"),
                   "display_label": r.get("display_label"),
                   "bp_new": _flag(r.get("bp_new_reading")), "temp_new": _flag(r.get("temp_new_reading"))}
            for m in METRICS:
                row[m] = _num(r.get(m))
            out.append(row)
    out.sort(key=lambda x: x["ts"])
    return out


class ReplayClock:
    """Replay time = anchor + (wall now − wall at creation)."""

    def __init__(self, anchor: datetime, wall: Callable[[], float] = time.time):
        self.anchor, self._wall, self._t0 = anchor, wall, wall()

    def __call__(self) -> datetime:
        return self.anchor + timedelta(seconds=self._wall() - self._t0)


def _origin(measurement_origin: Optional[str]) -> str:
    return "demo_playback" if measurement_origin == "master_csv_playback_not_camera" else (measurement_origin or "unknown")


class CsvReplayVitals:
    def __init__(self, rows: Optional[list[dict]] = None, clock: Optional[Callable[[], datetime]] = None,
                 stale_after_s: float = STALE_AFTER_S):
        self.rows = rows if rows is not None else load_rows()
        anchor = self.rows[-1]["ts"] if self.rows else mission_start()
        self.clock = clock or ReplayClock(anchor)
        self.stale_after_s = stale_after_s

    def now(self) -> datetime:
        return self.clock()

    def snapshot(self, crew_id: str, now: Optional[datetime] = None) -> dict:
        now = now or self.now()
        hist = [r for r in self.rows if r["crew_id"] == crew_id and r["ts"] <= now]   # no look-ahead (§3.7)
        if not hist:
            return {"reading_id": None, "timestamp": None, "age_seconds": None, "display_label": None,
                    "data_origin": None, "stale": True,
                    "metrics": {m: {"value": None, "measured_at": None, "age_seconds": None, "status": "missing"}
                                for m in METRICS}}
        row = hist[-1]
        metrics = {}
        for m in METRICS:
            val = row[m]
            if val is None:
                metrics[m] = {"value": None, "measured_at": None, "age_seconds": None, "status": "missing"}
                continue
            if m == "heart_rate_bpm":
                src = row
            elif m == "body_temp_c":
                src = next((r for r in reversed(hist) if r["temp_new"]), None)
            elif m in _BP_GROUP:
                src = next((r for r in reversed(hist) if r["bp_new"] or r["temp_new"]), None)
            else:
                src = row
            if src is None:   # value present but never freshly measured in this history: age unknown
                metrics[m] = {"value": val, "measured_at": None, "age_seconds": None, "status": "stale"}
                continue
            age = (now - src["ts"]).total_seconds()
            metrics[m] = {"value": val, "measured_at": _iso(src["ts"]), "age_seconds": round(age),
                          "status": "stale" if age > self.stale_after_s else "ok"}
        age = (now - row["ts"]).total_seconds()
        return {"reading_id": row["reading_id"], "timestamp": _iso(row["ts"]), "age_seconds": round(age),
                "display_label": row["display_label"], "data_origin": _origin(row["measurement_origin"]),
                "stale": age > self.stale_after_s, "metrics": metrics}
