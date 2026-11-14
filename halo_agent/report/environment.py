"""Radiation environment reference for the daily report (AGENTS.md §5). Historical ISS cabin data, NOT a
personal dose: it is shown as context and never feeds a crew member's status or severity. Stdlib only."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Optional

LEVEL_MAP = {"normal": "normal", "watch": "caution", "act": "alert"}   # threshold JSON names -> our vocabulary


@dataclass(frozen=True)
class RadiationReference:
    label: str
    source: str
    first_date: date
    daily_ugy: dict          # date -> max observed_dose_accumulated_day_ugy that day
    levels: tuple            # ((status, min, max), ...) from halo_alert_rules, derived thresholds

    def for_mission_day(self, mission_day: int) -> dict:
        ref = self.first_date + timedelta(days=mission_day - 1)
        dose = self.daily_ugy.get(ref)
        return {
            "label": self.label,
            "is_personal_dose": False,
            "reference_date": ref.isoformat() if dose is not None else None,
            "dose_day_ugy": round(dose, 1) if dose is not None else None,
            "level": self.level(dose),
            "threshold_source": "team_derived (from NASA-STD-3001 limits, radiation_thresholds.json)",
            "source": self.source,
            "prediction": "Model not trained",
        }

    def level(self, dose: Optional[float]) -> Optional[str]:
        if dose is None:
            return None
        for status, lo, hi in self.levels:
            if (lo is None or dose >= lo) and (hi is None or dose < hi):
                return LEVEL_MAP[status]
        return None


@lru_cache(maxsize=4)
def load_reference(csv_path: str, thresholds_path: str, label: str, source: str, first_date: str) -> RadiationReference:
    daily: dict = {}
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            v = row.get("observed_dose_accumulated_day_ugy")
            if not v:
                continue
            d = date.fromisoformat(row["date"])
            daily[d] = max(daily.get(d, 0.0), float(v))
    rules = json.loads(Path(thresholds_path).read_text(encoding="utf-8"))["halo_alert_rules"]
    rule = next(r for r in rules if r["metric"] == "observed_dose_accumulated_day_ugy")
    levels = tuple((lv["status"], lv.get("min"), lv.get("max")) for lv in rule["levels"])
    return RadiationReference(label=label, source=source, first_date=date.fromisoformat(first_date),
                              daily_ugy=daily, levels=levels)
