"""HALO Agent API. Run: uv run uvicorn halo_agent.api.app:app --reload"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import routes_admin, routes_report, routes_score, routes_sos

app = FastAPI(title="HALO Agent", version="0.0.1-phase0",
              description="Prototype decision support, not a medical device. Phase 0: endpoints return fixed JSON "
                          "(header X-HALO-Stub: true).")
# Design group wires screens from a local dev server; open CORS is fine for the hackathon demo.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["X-HALO-Stub"])

for r in (routes_score, routes_sos, routes_report, routes_admin):
    app.include_router(r.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "mode": routes_admin.current_mode(), "phase": 0}
