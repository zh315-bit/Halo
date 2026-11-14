"""HALO Agent API. Run: uv run uvicorn halo_agent.api.app:app --reload"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import core
from ..config_loader import load_scan_dict, load_scoring_config
from . import replay, routes_admin, routes_report, routes_scan, routes_score, routes_sos

app = FastAPI(title="HALO Agent", version="0.1.0",
              description="Prototype decision support, not a medical device. Endpoints still stubbed carry the "
                          "header X-HALO-Stub: true.")
# Browsers may call the API from the team's scan page (GitHub Pages) and from any localhost dev server
# (config/scan.yaml `cors`). Other origins get no CORS headers.
_cors = load_scan_dict()["cors"]
app.add_middleware(CORSMiddleware, allow_origins=list(_cors["allow_origins"]),
                   allow_origin_regex=_cors.get("allow_origin_regex"), allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["X-HALO-Stub"])

# Process-wide live engine behind core.score_latest(); the reading feed (M1 / WS) calls core.get_engine().update().
core.set_engine(core.Engine(load_scoring_config()))
replay.warmup_from_env()     # HALO_WARMUP_CSV=qr_scan_demo_clean.csv -> score_latest works before M1 exists
routes_scan.load_persisted() # real camera scans from data/live_scans.jsonl: live baselines survive a restart

# sos-v2: create nudges as scores change (§8.5). After warm-up, so only live updates nudge.
routes_sos.attach_nudges(core.get_engine())

for r in (routes_score, routes_sos, routes_report, routes_admin, routes_scan):
    app.include_router(r.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "mode": routes_admin.current_mode(), "phase": 1}
