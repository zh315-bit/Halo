"""Crew SOS endpoints (AGENTS.md §6.1, §6.4, §8). The SOS group is real (sos-v0); /classify and
/agent/ask are still Phase 0 stubs until the shell is wired (Phase 2)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response

from halo_agent.sos.service import SosService, UnknownSession
from halo_agent.sos.state_machine import IllegalTransition

from . import stubs
from .routes_admin import current_mode
from .schemas import (AskRequest, AskResponse, ClassifyRequest, ClassifyResponse, ProblemType, SosAckRequest,
                      SosResolveRequest, SosSession, SosStartRequest, SosStepRequest)

router = APIRouter(prefix="/api/v1", tags=["sos"])

_service: Optional[SosService] = None


def get_service() -> SosService:
    """Lazily built so a broken checklist fails on first use with a clear error, and tests can swap it."""
    global _service
    if _service is None:
        _service = SosService(mode_fn=current_mode)
    return _service


def set_service(svc: Optional[SosService]) -> None:
    global _service
    _service = svc


def _run(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except UnknownSession as e:
        raise HTTPException(404, f"unknown session {e.args[0]}")
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


def _schedule(bg: BackgroundTasks, jobs) -> None:
    for job in jobs:          # blocking sends (live Twilio) run after the response: guidance never waits
        bg.add_task(job)


@router.get("/sos/problem-types", response_model=list[ProblemType])
def sos_problem_types() -> list[dict]:
    return get_service().problem_types()


@router.post("/sos", response_model=SosSession)
def sos_start(req: SosStartRequest, bg: BackgroundTasks) -> dict:
    if (req.symptom is None) == (req.free_text is None):
        raise HTTPException(422, "Send exactly one of `symptom` (picker) or `free_text`.")
    out, jobs = _run(get_service().start, req.crew_id, symptom=req.symptom, free_text=req.free_text, note=req.note)
    _schedule(bg, jobs)
    return out


@router.get("/sos/{session_id}", response_model=SosSession)
def sos_get(session_id: str) -> dict:
    return _run(get_service().get, session_id)


@router.post("/sos/{session_id}/step", response_model=SosSession)
def sos_step(session_id: str, req: SosStepRequest, bg: BackgroundTasks) -> dict:
    out, jobs = _run(get_service().step, session_id, req.action, step_id=req.step_id)
    _schedule(bg, jobs)
    return out


@router.post("/sos/{session_id}/ack", response_model=SosSession)
def sos_ack(session_id: str, req: SosAckRequest) -> dict:
    """Manual acknowledgement for the demo (ground taps 'received'). SMS-reply ACK is Phase 3."""
    return _run(get_service().acknowledge, session_id, req.by)


@router.post("/sos/{session_id}/resolve", response_model=SosSession)
def sos_resolve(session_id: str, req: SosResolveRequest) -> dict:
    return _run(get_service().end, session_id, req.outcome)


# ---------- still Phase 0 stubs (Phase 2: shell) ----------
@router.post("/sos/{session_id}/classify", response_model=ClassifyResponse)
def sos_classify(session_id: str, req: ClassifyRequest, response: Response) -> dict:
    response.headers.update(stubs.STUB_HEADER)
    return stubs.classify(session_id, current_mode())


@router.post("/agent/ask", response_model=AskResponse)
def agent_ask(req: AskRequest, response: Response) -> dict:
    response.headers.update(stubs.STUB_HEADER)
    return stubs.ask(req.session_id, current_mode())
