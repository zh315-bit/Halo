"""POST /api/v1/admin/mode — online/offline toggle (AGENTS.md §2). Real logic, not a stub:
the demo "pulls the network", so every route reads the mode from here."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from halo_agent.sos.mission_env import configured_env

from .schemas import ModeRequest, ModeResponse

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_runtime_mode: str | None = None  # set by the toggle; None means "follow env"
_runtime_env: str | None = None   # sos-v2: mission_env toggle; None means "follow config/app.yaml"


def current_mode() -> str:
    if _runtime_mode is not None:
        return _runtime_mode
    return "offline" if os.environ.get("HALO_OFFLINE") == "1" else "online"


def mode_source() -> str:
    return "runtime_toggle" if _runtime_mode is not None else "env"


def current_mission_env() -> str:
    return _runtime_env if _runtime_env is not None else configured_env()


def reset_mode() -> None:
    """Test helper: go back to following HALO_OFFLINE and config/app.yaml."""
    global _runtime_mode, _runtime_env
    _runtime_mode = None
    _runtime_env = None


def _response() -> ModeResponse:
    return ModeResponse(mode=current_mode(), source=mode_source(), mission_env=current_mission_env(),
                        mission_env_source="runtime_toggle" if _runtime_env is not None else "config")


@router.post("/mode", response_model=ModeResponse)
def set_mode(req: ModeRequest) -> ModeResponse:
    global _runtime_mode, _runtime_env
    if req.mode is None and req.mission_env is None:
        raise HTTPException(422, "Send `mode` and/or `mission_env`.")
    if req.mode is not None:
        _runtime_mode = req.mode
    if req.mission_env is not None:
        _runtime_env = req.mission_env      # applies to SOS sessions started after this call
    return _response()
