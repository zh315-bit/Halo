"""POST /api/v1/admin/mode — online/offline toggle (AGENTS.md §2). Real logic, not a stub:
the demo "pulls the network", so every route reads the mode from here."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from .replay import ReplayPathError, replayer
from .schemas import ModeRequest, ModeResponse, ReplayRequest, ReplayStatus

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

_runtime_mode: str | None = None  # set by the toggle; None means "follow env"


def current_mode() -> str:
    if _runtime_mode is not None:
        return _runtime_mode
    return "offline" if os.environ.get("HALO_OFFLINE") == "1" else "online"


def mode_source() -> str:
    return "runtime_toggle" if _runtime_mode is not None else "env"


def reset_mode() -> None:
    """Test helper: go back to following HALO_OFFLINE."""
    global _runtime_mode
    _runtime_mode = None


@router.post("/mode", response_model=ModeResponse)
def set_mode(req: ModeRequest) -> ModeResponse:
    global _runtime_mode
    _runtime_mode = req.mode
    return ModeResponse(mode=current_mode(), source=mode_source())


@router.post("/replay", response_model=ReplayStatus, status_code=202)
def replay_start(req: ReplayRequest) -> dict:
    """Feed a data/ CSV into the live Engine at `speed`× real time (demo feed until M1)."""
    try:
        return replayer.start(req.file, req.speed, req.reset)
    except ReplayPathError as e:
        raise HTTPException(422, str(e))
    except FileNotFoundError:
        raise HTTPException(404, f"no such file in data/: {req.file}")
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except (KeyError, ValueError) as e:            # CSV that the adapter can't read
        raise HTTPException(422, f"cannot read {req.file}: {e}")


@router.get("/replay", response_model=ReplayStatus)
def replay_status() -> dict:
    return replayer.status()


@router.delete("/replay", response_model=ReplayStatus)
def replay_stop() -> dict:
    return replayer.stop()
