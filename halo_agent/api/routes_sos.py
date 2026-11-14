from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response

from . import stubs
from .routes_admin import current_mode
from .schemas import (AskRequest, AskResponse, ClassifyRequest, ClassifyResponse, SosSession,
                      SosStartRequest, SosStepRequest)

router = APIRouter(prefix="/api/v1", tags=["sos"])


@router.post("/sos", response_model=SosSession)
def sos_start(req: SosStartRequest, response: Response) -> dict:
    if (req.symptom is None) == (req.free_text is None):
        raise HTTPException(422, "Send exactly one of `symptom` (picker) or `free_text`.")
    response.headers.update(stubs.STUB_HEADER)
    out = stubs.sos_start(req.crew_id, current_mode())
    if req.free_text is not None:
        # Free text path: UI must call /classify and get the crewmate to confirm before advancing.
        out["symptom_source"] = "llm"
    return out


@router.post("/sos/{session_id}/step", response_model=SosSession)
def sos_step(session_id: str, req: SosStepRequest, response: Response) -> dict:
    response.headers.update(stubs.STUB_HEADER)
    return stubs.sos_after_step(session_id, req.action, current_mode())


@router.post("/sos/{session_id}/classify", response_model=ClassifyResponse)
def sos_classify(session_id: str, req: ClassifyRequest, response: Response) -> dict:
    response.headers.update(stubs.STUB_HEADER)
    return stubs.classify(session_id, current_mode())


@router.post("/agent/ask", response_model=AskResponse)
def agent_ask(req: AskRequest, response: Response) -> dict:
    response.headers.update(stubs.STUB_HEADER)
    return stubs.ask(req.session_id, current_mode())
