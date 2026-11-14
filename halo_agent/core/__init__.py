"""HALO deterministic core (M2 baseline + M3 scoring). Stdlib only; never imports halo_agent.shell.

Stable entry points for other modules (SOS, report, API):

    from halo_agent.core import score_latest, ScoreResult
"""

from __future__ import annotations

from typing import Optional

from .csv_source import reading_from_csv_row, reading_to_wire
from .engine import Engine
from .models import LatestReading, ScoreResult, VitalsReading
from .scan_source import ScanConfig, reading_from_scan

__all__ = ["Engine", "LatestReading", "ScoreResult", "VitalsReading", "get_engine", "latest_reading", "latest_scan",
           "ScanConfig", "reading_from_csv_row", "reading_from_scan", "reading_to_wire", "score_latest", "set_engine"]

_engine: Optional[Engine] = None


def set_engine(engine: Optional[Engine]) -> None:
    """Install the process-wide live Engine (done at API startup; the reading feed calls get_engine().update)."""
    global _engine
    _engine = engine


def get_engine() -> Optional[Engine]:
    return _engine


def score_latest(crew_id: str) -> Optional[ScoreResult]:
    """Latest ScoreResult for this crew member, or None if nothing has been scored yet (stable for M4 SOS)."""
    return _engine.latest(crew_id) if _engine is not None else None


def latest_reading(crew_id: str) -> Optional[LatestReading]:
    """Last reading the live Engine processed for this crew (a VitalsReading with measured_at / ages),
    or None. Same source as score_latest, so the SOS display and score never disagree."""
    return _engine.latest_reading(crew_id) if _engine is not None else None


def latest_scan(crew_id: str) -> Optional[dict]:
    """This crew's most recent scan record exactly as the scan page submitted it (field names as in
    tests/fixtures/scan_record_sample.json), or None (no scan, or not a live crew). AGENTS.md §8.6."""
    return _engine.latest_scan(crew_id) if _engine is not None else None
