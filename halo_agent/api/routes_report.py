from __future__ import annotations

from datetime import date as _date
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Response

from . import stubs
from .schemas import DailyReport, MorningActivityAccepted, MorningActivityRequest

router = APIRouter(prefix="/api/v1", tags=["report"])

DEFAULT_REPORT_DATE = "2026-11-15"  # = mission_start date (AGENTS.md §5)


def _check_date(d: str) -> str:
    try:
        _date.fromisoformat(d)
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    return d


@router.post("/activity/morning", response_model=MorningActivityAccepted, status_code=202)
def morning_activity(req: MorningActivityRequest, response: Response) -> dict:
    # Phase 0 stub: accepted, nothing stored; the activity itself is undefined (⚑ Q6).
    if req.date is not None:
        report_date = _check_date(req.date)
    elif req.completed_at is not None:
        try:
            report_date = datetime.fromisoformat(req.completed_at.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            raise HTTPException(422, "completed_at must be ISO8601")
    else:
        report_date = DEFAULT_REPORT_DATE
    response.headers.update(stubs.STUB_HEADER)
    return {"accepted": True, "report_date": report_date}


@router.get("/crew/{crew_id}/daily-report", response_model=DailyReport)
def daily_report(crew_id: str, response: Response,
                 date: str = Query(DEFAULT_REPORT_DATE, description="YYYY-MM-DD")) -> dict:
    _check_date(date)
    response.headers.update(stubs.STUB_HEADER)
    return stubs.daily_report(crew_id, date)
