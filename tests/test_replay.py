import time

import pytest
from fastapi.testclient import TestClient

from halo_agent import core
from halo_agent.api import replay as R
from halo_agent.api.app import app
from halo_agent.config_loader import load_scoring_config


@pytest.fixture
def client():
    old = core.get_engine()
    core.set_engine(core.Engine(load_scoring_config()))
    yield TestClient(app)
    R.replayer.stop()
    core.set_engine(old)


def _wait_done(client, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        s = client.get("/api/v1/admin/replay").json()
        if s["state"] != "running":
            return s
        time.sleep(0.02)
    raise AssertionError("replay did not finish")


def test_replay_fast_feeds_engine(client):
    r = client.post("/api/v1/admin/replay", json={"file": "qr_scan_demo_clean.csv", "speed": 1_000_000})
    assert r.status_code == 202 and r.json()["state"] == "running" and r.json()["total"] == 276
    s = _wait_done(client)
    assert s["state"] == "done" and s["index"] == 276 and s["last_reading_id"] == "QR_DEMO_0276"
    latest = core.score_latest("CREW_01")
    assert (latest.reading_id, latest.status, latest.severity) == ("QR_DEMO_0276", "alert", 3)


def test_replay_matches_batch(client):
    from halo_agent.core.csv_source import load_csv
    from halo_agent.core.score import score
    client.post("/api/v1/admin/replay", json={"file": "data/qr_scan_demo_clean.csv", "speed": 1_000_000})
    _wait_done(client)
    cfg = load_scoring_config()
    assert core.score_latest("CREW_01").to_dict() == score(load_csv(R.DATA_DIR / "qr_scan_demo_clean.csv", cfg), cfg)[-1].to_dict()


def test_replay_stop_and_conflict(client):
    r = client.post("/api/v1/admin/replay", json={"file": "qr_scan_demo_clean.csv", "speed": 1})  # 5 min gaps
    assert r.status_code == 202
    assert client.post("/api/v1/admin/replay", json={"file": "qr_scan_demo_clean.csv"}).status_code == 409
    s = client.delete("/api/v1/admin/replay").json()
    assert s["state"] == "stopped" and s["index"] == 1


def test_replay_reset_false_older_file_errors(client):
    client.post("/api/v1/admin/replay", json={"file": "qr_scan_demo_clean.csv", "speed": 1_000_000})
    _wait_done(client)
    client.post("/api/v1/admin/replay", json={"file": "qr_scan_demo_clean.csv", "speed": 1_000_000, "reset": False})
    s = _wait_done(client)
    assert s["state"] == "error" and "older" in s["error"]


@pytest.mark.parametrize("bad,code", [("../AGENTS.md", 422), ("data/../AGENTS.md", 422), ("/etc/passwd", 422),
                                      ("../pyproject.toml", 422), ("bone_density_thresholds.json", 422),
                                      ("nope.csv", 404), ("data/../data/../config/scoring.yaml", 422)])
def test_replay_only_reads_data_csv(client, bad, code):
    assert client.post("/api/v1/admin/replay", json={"file": bad}).status_code == code


def test_replay_rejects_bad_speed(client):
    assert client.post("/api/v1/admin/replay", json={"file": "qr_scan_demo_clean.csv", "speed": 0}).status_code == 422


def test_warmup_from_env(monkeypatch):
    old = core.get_engine()
    core.set_engine(core.Engine(load_scoring_config()))
    try:
        monkeypatch.setenv("HALO_WARMUP_CSV", "qr_scan_demo_clean.csv")
        assert R.warmup_from_env() == 276
        assert core.score_latest("CREW_01").reading_id == "QR_DEMO_0276"
        monkeypatch.setenv("HALO_WARMUP_CSV", "../AGENTS.md")
        with pytest.raises(R.ReplayPathError):
            R.warmup_from_env()
    finally:
        core.set_engine(old)
