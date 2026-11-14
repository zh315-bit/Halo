"""Crew SOS endpoints (AGENTS.md §6.1, §6.4, §8, §10). The SOS state machine never imports the shell; this
route layer calls the shell (classify / ask) and records the results on the session."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException

from halo_agent.shell import ask, classify
from halo_agent.shell.llm import LLMClient, get_llm as _env_llm
from halo_agent.sos.nudge import NudgeService, NudgeStateError, UnknownNudge
from halo_agent.sos.service import DISCLAIMER, SosService, UnknownSession
from halo_agent.sos.state_machine import IllegalTransition

from .routes_admin import current_mission_env, current_mode
from .schemas import (AskRequest, AskResponse, ClassifyRequest, ClassifyResponse, GroundReport, Nudge, NudgeAccepted,
                      NudgeAcceptRequest, NudgeList, NudgeSnoozeRequest, ProblemType, SosAckRequest,
                      SosConfirmRequest, SosResolveRequest, SosSession, SosStartRequest, SosStepRequest)

router = APIRouter(prefix="/api/v1", tags=["sos"])

_service: Optional[SosService] = None


def get_service() -> SosService:
    """Lazily built so a broken checklist fails on first use with a clear error, and tests can swap it."""
    global _service
    if _service is None:
        _service = SosService(mode_fn=current_mode, env_fn=current_mission_env)
    return _service


def set_service(svc: Optional[SosService]) -> None:
    global _service, _nudges
    _service, _nudges = svc, None


_nudges: Optional[NudgeService] = None


def get_nudges() -> NudgeService:
    global _nudges
    if _nudges is None or _nudges.sos is not get_service():
        _nudges = NudgeService(get_service())
    return _nudges


_llm: Optional[LLMClient] = None


def get_llm() -> LLMClient:
    """From env (⚑ Q8: no provider yet -> NullLLM). Tests inject FakeLLM with set_llm()."""
    return _llm if _llm is not None else _env_llm()


def set_llm(llm: Optional[LLMClient]) -> None:
    global _llm
    _llm = llm


def _run(fn, *args, **kw):
    try:
        return fn(*args, **kw)
    except UnknownSession as e:
        raise HTTPException(404, f"unknown session {e.args[0]}")
    except UnknownNudge as e:
        raise HTTPException(404, f"unknown nudge {e.args[0]}")
    except NudgeStateError as e:
        raise HTTPException(409, str(e))
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


@router.get("/sos/ground-reports", response_model=list[GroundReport])
def sos_ground_reports() -> list[dict]:
    """level=report escalations (e.g. fainting f7): logged for the flight surgeon, never texted."""
    return list(get_service().ground_reports)


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


# ---------- shell (LLM optional; every path has a deterministic fallback) ----------
@router.post("/sos/{session_id}/classify", response_model=ClassifyResponse)
def sos_classify(session_id: str, req: ClassifyRequest, bg: BackgroundTasks) -> dict:
    svc = get_service()
    _run(svc.require, session_id)
    mode = current_mode()
    out = classify.classify(req.free_text, svc.symptoms(), get_llm(), mode, svc.rules.min_classification_confidence)
    _schedule(bg, _run(svc.record_classification, session_id, req.free_text, out))
    return {"session_id": session_id, "mode": mode, **out}


@router.post("/sos/{session_id}/confirm", response_model=SosSession)
def sos_confirm(session_id: str, req: SosConfirmRequest, bg: BackgroundTasks) -> dict:
    """The crewmate picks one problem type. A different type switches the checklist (event checklist_switched)."""
    out, jobs = _run(get_service().confirm_symptom, session_id, req.symptom)
    _schedule(bg, jobs)
    return out


@router.post("/agent/ask", response_model=AskResponse)
def agent_ask(req: AskRequest) -> dict:
    svc, mode = get_service(), current_mode()
    ctx = _run(svc.ask_context, req.session_id) if req.session_id else None
    out = ask.ask(req.question, ctx, get_llm(), mode)
    if req.session_id:
        _run(svc.record_ask, req.session_id, req.question, out)
    step = (ctx or {}).get("step") or {}
    cites = [{"kind": "checklist_step", "id": c, "ref": step.get("ref") if c == step.get("id") else None,
              "sources": [{k: s[k] for k in ("key", "title", "url", "medical")} for s in step.get("sources", [])]
              if c == step.get("id") else []} for c in out["citations"]]
    return {**out, "citations": cites,
            "session_id": req.session_id, "mode": mode, "disclaimer": DISCLAIMER}


# ---------- §8.5 proactive nudge ----------
@router.get("/crew/{crew_id}/nudges", response_model=NudgeList)
def crew_nudges(crew_id: str) -> dict:
    """Poll this: evaluates the latest score and returns the active nudges (created on the transition to alert)."""
    return {"crew_id": crew_id, "nudges": get_nudges().evaluate(crew_id)}


@router.post("/nudges/{nudge_id}/snooze", response_model=Nudge)
def nudge_snooze(nudge_id: str, req: NudgeSnoozeRequest) -> dict:
    return _run(get_nudges().snooze, nudge_id, req.minutes)


@router.post("/nudges/{nudge_id}/dismiss", response_model=Nudge)
def nudge_dismiss(nudge_id: str) -> dict:
    return _run(get_nudges().dismiss, nudge_id)


@router.post("/nudges/{nudge_id}/accept", response_model=NudgeAccepted)
def nudge_accept(nudge_id: str, req: NudgeAcceptRequest, bg: BackgroundTasks) -> dict:
    """Open one suggested checklist: starts an SOS session for that crew member (mission support is alerted)."""
    n, session, jobs = _run(get_nudges().accept, nudge_id, req.symptom)
    _schedule(bg, jobs)
    return {"nudge": n, "session": session}

