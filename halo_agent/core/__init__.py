"""HALO deterministic core (M2 baseline + M3 scoring). Stdlib only; never imports halo_agent.shell.

Stable entry points for other modules (SOS, report, API):

    from halo_agent.core import score_latest, ScoreResult
"""

from __future__ import annotations

from typing import Optional

from .models import ScoreResult

__all__ = ["ScoreResult", "score_latest"]


def score_latest(crew_id: str) -> Optional[ScoreResult]:
    """Latest ScoreResult for this crew member, or None if nothing has been scored yet.

    Stable signature for M4 (SOS). Phase 0: always None. Phase 1 backs it with the per-crew Engine
    registry fed by the live reading stream.
    """
    return None
