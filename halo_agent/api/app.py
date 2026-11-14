"""HALO Agent API. Run: uv run uvicorn halo_agent.api.app:app --reload"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import core
from ..config_loader import load_scoring_config
from . import replay, routes_admin, routes_report, routes_score, routes_sos

app = FastAPI(title="HALO Agent", version="0.1.0",
              description="Prototype decision support, not a medical device. Endpoints still stubbed carry the "
                          "header X-HALO-Stub: true.")
# Design group wires screens from a local dev server; open CORS is fine for the hackathon demo.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["X-HALO-Stub"])

# Process-wide live engine behind core.score_latest(); the reading feed (M1 / WS) calls core.get_engine().update().
core.set_engine(core.Engine(load_scoring_config()))
replay.warmup_from_env()     # HALO_WARMUP_CSV=qr_scan_demo_clean.csv -> score_latest works before M1 exists

for r in (routes_score, routes_sos, routes_report, routes_admin):
    app.include_router(r.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "mode": routes_admin.current_mode(), "phase": 1}
