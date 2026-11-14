"""M5: POST /api/v1/activity/morning and GET /api/v1/crew/{crew_id}/daily-report (template version)."""

from __future__ import annotations

from datetime import date as _date
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from .. import core
from ..config_loader import REPO_ROOT, load_report_config, load_scoring_config
from ..report.daily import ReportConfig, build_daily_report, build_live_report, mission_day_of
from ..report.environment import load_reference
from ..shell.llm import LLMClient
from ..shell.llm import get_llm as _env_llm
from ..shell.rewrite import rewrite_report
from .routes_admin import current_mode
from .schemas import DailyReport, MorningActivityAccepted, MorningActivityRequest

router = APIRouter(prefix="/api/v1", tags=["report"])

# (crew_id, date) -> {"completed_at": datetime | None, "completion_seconds": float | None}
# In memory for the demo; the morning activity itself is still undefined (⚑ Q6).
_mornings: dict[tuple[str, _date], dict] = {}


def reset_mornings() -> None:
    _mornings.clear()


_llm: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    """From env (HALO_LLM_*); NullLLM when not configured. Tests inject FakeLLM with set_llm()."""
    return _llm if _llm is not None else _env_llm()


def set_llm(llm: Optional[LLMClient]) -> None:
    global _llm
    _llm = llm


def _date_or_422(d: str) -> _date:
    try:
        return _date.fromisoformat(d)
    except ValueError:
        raise HTTPException(422, "date must be YYYY-MM-DD")


def _ts_or_422(s: str) -> datetime:
    try:
        t = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(422, "completed_at must be ISO8601")
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


@router.post("/activity/morning", response_model=MorningActivityAccepted, status_code=202)
def morning_activity(req: MorningActivityRequest) -> dict:
    completed = _ts_or_422(req.completed_at) if req.completed_at else None
    if req.date is not None:
        d = _date_or_422(req.date)
    elif completed is not None:
        d = completed.date()
    else:
        d = load_scoring_config().mission_start.date()
    if req.completion_seconds is not None and req.completion_seconds < 0:
        raise HTTPException(422, "completion_seconds must be >= 0")
    _mornings[(req.crew_id, d)] = {"completed_at": completed, "completion_seconds": req.completion_seconds}
    return {"accepted": True, "report_date": d.isoformat()}


def _environment(d: _date) -> Optional[dict]:
    rr = load_report_config()["radiation_reference"]
    try:
        ref = load_reference(str(REPO_ROOT / rr["file"]), str(REPO_ROOT / rr["thresholds"]), rr["label"],
                             rr["source"], rr["first_date"])
    except FileNotFoundError:
        return None
    return ref.for_mission_day(mission_day_of(d, load_scoring_config()))


@router.get("/crew/{crew_id}/daily-report", response_model=DailyReport)
def daily_report(crew_id: str, date: Optional[str] = Query(None, description="YYYY-MM-DD; default = day of the "
                                                                              "crew's latest reading")) -> dict:
    asked = _date_or_422(date) if date else None
    eng = core.get_engine()
    history = eng.history(crew_id) if eng else []
    if not history:
        raise HTTPException(404, f"no readings for {crew_id} yet (start with HALO_WARMUP_CSV or /admin/replay)")
    d = asked or history[-1][0].timestamp.astimezone(timezone.utc).date()
    if eng.is_live(crew_id):                       # real camera scans: own report shape, own baseline only
        rep = build_live_report(crew_id, d, history, load_scoring_config(),
                                ReportConfig.from_dict(load_report_config()), _environment(d))
    else:
        mornings = {k[1]: v for k, v in _mornings.items() if k[0] == crew_id}
        rep = build_daily_report(crew_id, d, history, load_scoring_config(),
                                 ReportConfig.from_dict(load_report_config()), _environment(d), mornings)
    # §9.5: template first; online, the shell may rewrite summary_text (validated, else the template stays)
    return rewrite_report(rep, get_llm(), current_mode())
