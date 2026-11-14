from __future__ import annotations

from datetime import date as _date

from fastapi import APIRouter, HTTPException, Query, Response

from . import stubs
from .schemas import DailyReport

router = APIRouter(prefix="/api/v1", tags=["report"])


@router.get("/crew/{crew_id}/daily-report", response_model=DailyReport)
def daily_report(crew_id: str, response: Response,
                 date: str = Query("2026-11-15", description="YYYY-MM-DD")) -> dict:
    try:
        _date.fromisoformat(date)
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    response.headers.update(stubs.STUB_HEADER)
    return stubs.daily_report(crew_id, date)
