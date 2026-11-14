"""POST /api/v1/admin/demo/{scene} (config/demo.yaml): every scene runs; live crews are never touched."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from halo_agent import core
from halo_agent.api import replay as R
from halo_agent.api import routes_admin
from halo_agent.api.app import app
from halo_agent.config_loader import load_demo_dict, load_scoring_config

RECORD = json.loads((Path(__file__).parent / "fixtures" / "scan_record_sample.json").read_text())


@pytest.fixture
def c(monkeypatch, tmp_path):
    monkeypatch.setenv("HALO_LIVE_SCANS_FILE", str(tmp_path / "live_scans.jsonl"))
    old = core.get_engine()
    core.set_engine(core.Engine(load_scoring_config()))
    routes_admin.reset_mode()
    client = TestClient(app)
    assert client.post("/api/v1/scan", json={"record": RECORD}).status_code == 200   # a live crew alongside
    yield client
    R.replayer.stop()
    routes_admin.reset_mode()
    core.set_engine(old)


def test_list_matches_config(c):
    assert [s["scene"] for s in c.get("/api/v1/admin/demo").json()] == list(load_demo_dict()["scenes"])


@pytest.mark.parametrize("scene", list(load_demo_dict()["scenes"]))
def test_every_scene_runs_and_keeps_live_crew(c, scene):
    r = c.post(f"/api/v1/admin/demo/{scene}")
    assert r.status_code == 200, r.text
    assert core.latest_scan("LIVE_01") == RECORD and core.score_latest("LIVE_01") is not None


def test_baseline_and_drift_load_at_once(c):
    j = c.post("/api/v1/admin/demo/baseline").json()
    assert j["loaded"] == [{"file": "qr_scan_demo_clean.csv", "crews": ["CREW_01"], "readings": 276}]
    assert core.score_latest("CREW_01").reading_id == "QR_DEMO_0276" and j["mode"] == "online"
    j = c.post("/api/v1/admin/demo/drift").json()
    assert j["loaded"][0]["crews"] == ["SIM_CREW_DRIFT"] and j["loaded"][0]["readings"] == 2016
    assert core.score_latest("CREW_01") is not None                       # other crews stay
    rep = c.get("/api/v1/crew/SIM_CREW_DRIFT/daily-report?date=2026-11-19").json()
    assert rep["duty_status"] == "rest"


def test_offline_then_reset(c):
    c.post("/api/v1/admin/demo/baseline")
    c.post("/api/v1/admin/mode", json={"mission_env": "microgravity"})
    assert c.post("/api/v1/admin/demo/offline").json()["mode"] == "offline"
    j = c.post("/api/v1/admin/demo/reset").json()
    assert j["cleared_crews"] == ["CREW_01"] and j["mode"] == "online"
    assert j["mission_env"] == "partial_gravity"                         # back to config/app.yaml
    assert core.score_latest("CREW_01") is None and core.score_latest("LIVE_01") is not None


def test_drift_live_replays_and_scene_switch_stops_it(c):
    j = c.post("/api/v1/admin/demo/drift_live").json()
    assert j["replay"]["state"] == "running" and j["replay"]["file"] == "SIMULATED_drift_7d.csv"
    assert c.post("/api/v1/admin/demo/baseline").status_code == 200     # takes over: replay stopped first
    assert c.get("/api/v1/admin/replay").json()["state"] == "stopped"


def test_unknown_scene(c):
    assert c.post("/api/v1/admin/demo/nope").status_code == 404


def test_sos_event_scene_replays_the_event_file(c):
    j = c.post("/api/v1/admin/demo/sos_event").json()
    assert j["replay"]["file"] == "SIMULATED_sos_event.csv" and j["replay"]["speed"] == 3600 and j["mode"] == "online"
