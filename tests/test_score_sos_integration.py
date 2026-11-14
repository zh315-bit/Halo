"""core.score_latest -> SOS: replaying the demo makes the last reading alert / severity 3 -> severity_max."""

from fastapi.testclient import TestClient

from halo_agent import core
from halo_agent.config_loader import REPO_ROOT, load_scoring_config
from halo_agent.core.csv_source import load_csv


def test_sos_uses_score_latest(monkeypatch):
    monkeypatch.setenv("ALERT_MODE", "dry_run")
    from halo_agent.api import routes_sos
    from halo_agent.api.app import app
    cfg = load_scoring_config()
    eng = core.Engine(cfg)
    old = core.get_engine()
    core.set_engine(eng)
    routes_sos.set_service(None)
    try:
        for r in load_csv(REPO_ROOT / "data" / "qr_scan_demo_clean.csv", cfg):
            eng.update(r)
        latest = core.score_latest("CREW_01")
        assert (latest.reading_id, latest.status, latest.severity) == ("QR_DEMO_0276", "alert", 3)
        s = TestClient(app).post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "breathing_difficulty"})
        assert s.status_code == 200, s.text
        j = s.json()
        assert j["score"] == {"status": "alert", "severity": 3}
        assert "severity_max" in j["escalation"]["reasons"]
    finally:
        core.set_engine(old)
        routes_sos.set_service(None)
