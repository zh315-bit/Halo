"""POST /api/v1/admin/mode — online/offline toggle (AGENTS.md §2). Real logic, not a stub:
the demo "pulls the network", so every route reads the mode from here."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException

from halo_agent.sos.mission_env import configured_env

from ..config_loader import load_demo_dict
from ..core.csv_source import load_csv
from .replay import ReplayPathError, _engine, replayer, resolve_data_csv
from .schemas import DemoScene, DemoSceneResult, ModeRequest, ModeResponse, ReplayRequest, ReplayStatus

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


# ---------- demo scenes (config/demo.yaml) ----------
_ENVS = ("microgravity", "partial_gravity", "post_landing")


def _scenes() -> dict:
    return load_demo_dict()["scenes"]


@router.get("/demo", response_model=list[DemoScene])
def demo_scenes() -> list[dict]:
    return [{"scene": k, "description": v["description"]} for k, v in _scenes().items()]


@router.post("/demo/{scene}", response_model=DemoSceneResult)
def demo_scene(scene: str) -> dict:
    """Switch the demo to a preset scene. Stops a running replay first; live camera crews are never touched."""
    global _runtime_mode, _runtime_env
    sc = _scenes().get(scene)
    if sc is None:
        raise HTTPException(404, f"unknown scene {scene!r}; GET /api/v1/admin/demo lists them")
    if sc.get("mode") not in (None, "online", "offline") or sc.get("mission_env") not in (None, "config", *_ENVS):
        raise HTTPException(500, f"config/demo.yaml scene {scene!r} is invalid")
    eng = _engine()
    replayer.stop()
    cleared = []
    if sc.get("clear_synthetic"):
        cleared = sorted(c for c in eng.crews() if eng.is_live(c) is False)
        for c in cleared:
            eng.reset(c)
    loaded = []
    try:
        for name in sc.get("load") or []:
            readings = load_csv(resolve_data_csv(name), eng.cfg)
            crews = list(dict.fromkeys(r.crew_id for r in readings))
            for c in crews:
                eng.reset(c)
            for r in readings:
                eng.update(r)
            loaded.append({"file": name, "crews": crews, "readings": len(readings)})
        rp = sc.get("replay")
        replay = replayer.start(rp["file"], float(rp.get("speed", 60)), True) if rp else None
    except (ReplayPathError, FileNotFoundError, KeyError, ValueError) as e:
        raise HTTPException(500, f"scene {scene!r} cannot load its data: {e}")
    if sc.get("mode"):
        _runtime_mode = sc["mode"]
    if sc.get("mission_env"):
        _runtime_env = None if sc["mission_env"] == "config" else sc["mission_env"]
    return {"scene": scene, "description": sc["description"], "cleared_crews": cleared, "loaded": loaded,
            "replay": replay, "mode": current_mode(), "mission_env": current_mission_env()}
