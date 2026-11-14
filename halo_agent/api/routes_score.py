from __future__ import annotations

from fastapi import APIRouter, Response

from . import stubs
from .schemas import ScoreRequest, ScoreResult

router = APIRouter(prefix="/api/v1", tags=["score"])


@router.post("/score", response_model=ScoreResult)
def score(req: ScoreRequest, response: Response) -> dict:
    # Phase 0: fixed payload. Phase 1 replaces this with halo_agent.core.score.
    response.headers.update(stubs.STUB_HEADER)
    out = dict(stubs.SCORE_RESULT)
    latest = req.readings[-1]
    out.update(crew_id=latest.crew_id, reading_id=latest.reading_id, as_of=latest.timestamp)
    return out
