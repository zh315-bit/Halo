"""HALO deterministic core (M2 baseline + M3 scoring). Stdlib only; never imports halo_agent.shell.

Stable entry points for other modules (SOS, report, API):

    from halo_agent.core import score_latest, ScoreResult
"""

from __future__ import annotations

from typing import Optional

from .csv_source import reading_from_csv_row, reading_to_wire
from .engine import Engine
from .models import ScoreResult, VitalsReading

__all__ = ["Engine", "ScoreResult", "VitalsReading", "get_engine", "reading_from_csv_row", "reading_to_wire",
           "score_latest", "set_engine"]

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
