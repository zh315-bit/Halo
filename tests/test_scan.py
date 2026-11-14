"""Camera scan ingest -> live crew: adapter, own baseline, missing stays missing, daily report, SOS, CORS.

Fixture tests/fixtures/scan_record_sample.json is a real record downloaded from the team's scan page.
Later scans in these tests are copies of it with changed numbers (marked SIMULATED in their session_id)."""

import copy
import json
import statistics
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from halo_agent import core
from halo_agent.api import replay as R
from halo_agent.api import routes_sos
from halo_agent.api.app import app
from halo_agent.config_loader import REPO_ROOT, load_scan_dict, load_scoring_config
from halo_agent.core.csv_source import load_csv
from halo_agent.core.models import VITAL_METRICS
from halo_agent.core.scan_source import ScanConfig, reading_from_scan
from halo_agent.core.score import score as score_batch

FIXTURE = Path(__file__).parent / "fixtures" / "scan_record_sample.json"
RECORD = json.loads(FIXTURE.read_text())
SCAN_CFG = ScanConfig.from_dict(load_scan_dict())
CFG = load_scoring_config()
NOT_MEASURED = ("systolic_bp_mmhg", "diastolic_bp_mmhg", "body_temp_c", "spo2_pct")


def scan(day: int, resp: float, hr=None, quality=0.6, **extra) -> dict:
    """SIMULATED variant of the real record, `day` days after it."""
    rec = copy.deepcopy(RECORD)
    t = datetime.fromisoformat(RECORD["created_at"].replace("Z", "+00:00")) + timedelta(days=day)
    rec.update(session_id=f"SIMULATED-scan-{day}", created_at=t.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
               breathing_brpm=resp, heart_bpm=hr, heart_quality=quality, **extra)
    return rec


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("ALERT_MODE", "dry_run")
    monkeypatch.setenv("HALO_SOS_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("HALO_LIVE_SCANS_FILE", str(tmp_path / "live_scans.jsonl"))
    old = core.get_engine()
    eng = core.Engine(CFG)
    core.set_engine(eng)
    routes_sos.set_service(None)
    for r in load_csv(REPO_ROOT / "data" / "qr_scan_demo_clean.csv", CFG):   # synthetic CREW_01 alongside
        eng.update(r)
    yield TestClient(app)
    R.replayer.stop()
    routes_sos.set_service(None)
    core.set_engine(old)


# ---------- adapter ----------
def test_reading_from_real_fixture():
    r = reading_from_scan(RECORD, None, SCAN_CFG, CFG)
    assert (r.crew_id, r.reading_id, r.activity) == ("LIVE_01", RECORD["session_id"], "resting")
    assert r.timestamp == datetime(2026, 11, 14, 22, 35, 12, 136000, tzinfo=timezone.utc)
    assert r.heart_rate_bpm is None                      # heart_bpm null, quality 0.15 anyway
    assert (r.resp_rate_bpm, r.blink_rate_per_min) == (10.3, 4)
    assert all(r.value(m) is None for m in NOT_MEASURED)
    assert r.display_label == "Live camera measurement (experimental)"
    ctx = r.scan_context
    assert (ctx["verdict"], ctx["readiness_score"], ctx["flags"]) == ("rest", 76, RECORD["flags"])
    assert (ctx["docking_error_pct"], ctx["docking_on_target_pct"], ctx["reaction_median_ms"]) == (2.8, 98.6, 384)
    assert (ctx["sleep_hours"], ctx["self_alertness"], ctx["self_mood"], ctx["memory_span"]) == (8, 4, 4, None)
    assert (ctx["gonogo_hit_rate_pct"], ctx["reaction_lapses"]) == (50, 0)


def test_low_heart_quality_is_dropped():
    low = reading_from_scan(dict(RECORD, heart_bpm=55.5, heart_quality=0.34), "LIVE_X", SCAN_CFG, CFG)
    assert low.heart_rate_bpm is None and "heart_quality 0.34 < 0.35" in low.scan_context["excluded"]["heart_rate_bpm"]
    ok = reading_from_scan(dict(RECORD, heart_bpm=62, heart_quality=0.35), "LIVE_X", SCAN_CFG, CFG)
    assert ok.heart_rate_bpm == 62 and ok.scan_context["excluded"] == {}
    no_q = reading_from_scan(dict(RECORD, heart_bpm=62, heart_quality=None), "LIVE_X", SCAN_CFG, CFG)
    assert no_q.heart_rate_bpm is None


@pytest.mark.parametrize("bad", [{"created_at": None}, {"session_id": None}, {"created_at": "2026-11-14T22:35:12"}])
def test_bad_record_rejected(bad):
    with pytest.raises(ValueError):
        reading_from_scan(dict(RECORD, **bad), None, SCAN_CFG, CFG)


# ---------- scoring ----------
def test_missing_vitals_never_normal():
    eng = core.Engine(CFG)
    res = eng.update(reading_from_scan(RECORD, None, SCAN_CFG, CFG))
    ind = {i.metric: i for i in res.indicators}
    for m in NOT_MEASURED + ("heart_rate_bpm",):
        assert ind[m].status == "insufficient" and ind[m].value is None
        assert any(x.metric == m and x.rule_id == "missing" for x in res.reasons)
    assert ind["resp_rate_bpm"].status == "insufficient"     # first scan: no baseline yet
    assert (res.confidence, res.baseline_quality, res.data_origin) == (0, "insufficient", "camera_scan")
    assert res.baseline_building == {"n": 0, "needed": 3, "label": "Baseline building (0/3)"}


def test_red_flag_still_fires_without_baseline():
    r = reading_from_scan(RECORD, None, SCAN_CFG, CFG)
    from dataclasses import replace
    res = core.Engine(CFG).update(replace(r, spo2_pct=85.0))   # no camera SpO2 today; checks the rule path
    assert res.status == "alert" and any(x.rule_id == "spo2_low" for x in res.reasons)


def test_live_batch_equals_engine():
    rs = [reading_from_scan(scan(d, 10 + d % 3, hr=60 + d), None, SCAN_CFG, CFG) for d in range(6)]
    eng = core.Engine(CFG)
    assert [eng.update(r).to_dict() for r in rs] == [x.to_dict() for x in score_batch(rs, CFG)]


def test_crew_never_mixes_live_and_synthetic():
    eng = core.Engine(CFG)
    demo = load_csv(REPO_ROOT / "data" / "qr_scan_demo_clean.csv", CFG)[:3]
    for r in demo:
        eng.update(r)
    with pytest.raises(ValueError, match="separate crew_id"):
        eng.update(reading_from_scan(RECORD, "CREW_01", SCAN_CFG, CFG))
    with pytest.raises(ValueError):
        score_batch(demo + [reading_from_scan(scan(400, 10), "CREW_01", SCAN_CFG, CFG)], CFG)


# ---------- API flow: scan -> Engine -> score -> report ----------
def test_scan_flow_own_baseline_and_report(client):
    crew01_resting_resp = statistics.median(
        r.resp_rate_bpm for r in load_csv(REPO_ROOT / "data" / "qr_scan_demo_clean.csv", CFG) if r.activity == "resting")
    resps, hrs = [10.3, 11.0, 12.0, 11.0], [None, 58.0, 60.0, 59.0]
    out = []
    for d, (resp, hr) in enumerate(zip(resps, hrs)):
        rec = RECORD if d == 0 else scan(d, resp, hr=hr, sleep_hours=6 + d, docking_error_pct=2.0 + d)
        r = client.post("/api/v1/scan", json={"record": rec})
        assert r.status_code == 200, r.text
        out.append(r.json())
    # label = the least-built present vital (HR first appears in scan 2)
    assert [o["score"]["baseline_building"]["label"] for o in out[:3]] == [
        "Baseline building (0/3)", "Baseline building (0/3)", "Baseline building (1/3)"]
    assert out[0]["reading"]["scan_context"]["verdict"] == "rest"
    last = {i["metric"]: i for i in out[3]["score"]["indicators"]}
    b = last["resp_rate_bpm"]["baseline"]
    # own baseline: the 3 earlier live scans, never CREW_01's resting respiration (~18 /min)
    assert b == {"median": statistics.median(resps[:3]), "sigma": b["sigma"], "n": 3, "group": "resting"}
    assert b["median"] != crew01_resting_resp
    assert last["heart_rate_bpm"]["baseline"] is None                  # 2 usable HR scans < 3
    assert out[3]["score"]["baseline_building"]["label"] == "Baseline building (2/3)"
    assert all(last[m]["status"] == "insufficient" for m in NOT_MEASURED)
    assert core.score_latest("CREW_01").reading_id == "QR_DEMO_0276"   # synthetic crew untouched

    rep = client.get("/api/v1/crew/LIVE_01/daily-report").json()
    assert rep["crew_source"] == "live" and rep["date"] == "2026-11-17"
    assert rep["sleep"]["source"] == "self_report" and rep["sleep"]["self_report_hours"] == 9
    assert rep["sleep"]["self_report_vs_baseline_hours"] == 9 - statistics.median([8, 7, 8])
    assert rep["sleep"]["in_bed_minutes"] is None
    tests = {t["metric"]: t for t in rep["morning_activity"]["tests"]}
    assert rep["morning_activity"]["source"] == "scan_tests"
    assert tests["docking_error_pct"]["value"] == 5.0 and tests["docking_error_pct"]["n_prior"] == 3
    assert tests["docking_error_pct"]["baseline_median"] == 3.0          # median of 2.8, 3.0, 4.0
    assert tests["docking_error_pct"]["vs_baseline_pct"] == round((5.0 - 3.0) / 3.0 * 100, 1)
    assert tests["reaction_median_ms"]["vs_baseline_pct"] == 0.0
    assert rep["scan_verdict"]["label"] == "Scan verdict" and rep["scan_verdict"]["verdict"] == "rest"
    assert rep["duty_status"] in ("ready", "caution", "rest")
    assert any("Not measured in this scan" in w for w in rep["watch_items"])
    assert "Take a fresh vitals reading." in rep["recommendations"]
    assert set(rep["data_sources"]) >= {"camera_scan", "self_report", "scan_app"}
    assert rep["disclaimer"] == "Decision support only. Not a diagnosis."


def test_first_scan_report_is_not_ready(client):
    client.post("/api/v1/scan", json={"record": RECORD})
    rep = client.get("/api/v1/crew/LIVE_01/daily-report?date=2026-11-14").json()
    assert rep["duty_status"] == "caution"                               # nothing assessable: never "ready"
    assert rep["morning_activity"]["tests"][0]["vs_baseline_pct"] is None  # < 3 earlier scans
    assert rep["sleep"]["self_report_vs_baseline_hours"] is None
    assert "Baseline building (0/3): not enough history to compare yet" in rep["watch_items"]
    assert "Caution because your personal baseline is still building (0/3): not enough history" in rep["summary_text"]
    empty = client.get("/api/v1/crew/LIVE_01/daily-report?date=2026-11-20").json()
    assert empty["duty_status"] == "caution" and empty["scan_verdict"] is None
    assert "No camera scan on this date. Missing, not assumed normal." in empty["watch_items"]


def test_scan_errors(client):
    assert client.post("/api/v1/scan", json={"crew_id": "CREW_01", "record": RECORD}).status_code == 409
    assert client.post("/api/v1/scan", json={"record": {"created_at": "x"}}).status_code == 422
    assert client.post("/api/v1/scan", json={"record": scan(2, 11)}).json()["duplicate"] is False
    again = client.post("/api/v1/scan", json={"record": scan(2, 11)}).json()
    assert again["duplicate"] is True
    assert client.post("/api/v1/scan", json={"record": RECORD}).status_code == 409   # older than the last scan


def test_upload_file(client):
    r = client.post("/api/v1/scan/upload", files={"file": ("scan.json", FIXTURE.read_bytes(), "application/json")},
                    data={"crew_id": "LIVE_02"})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 1 and r.json()["results"][0]["crew_id"] == "LIVE_02"
    many = json.dumps([scan(2, 11), scan(1, 10)]).encode()
    r = client.post("/api/v1/scan/upload", files={"file": ("all.json", many, "application/json")})
    assert [x["reading_id"] for x in r.json()["results"]] == ["SIMULATED-scan-1", "SIMULATED-scan-2"]
    bad = client.post("/api/v1/scan/upload", files={"file": ("x.json", b"not json", "application/json")})
    assert bad.status_code == 422


def test_crews_list(client):
    before = {c["crew_id"]: c for c in client.get("/api/v1/crews").json()["crews"]}
    assert before["CREW_01"]["source"] == "synthetic" and before["CREW_01"]["data_origin"] == "demo_playback"
    assert before["LIVE_01"]["source"] == "live" and before["LIVE_01"]["readings"] == 0
    client.post("/api/v1/scan", json={"record": RECORD})
    after = {c["crew_id"]: c for c in client.get("/api/v1/crews").json()["crews"]}
    assert after["LIVE_01"]["readings"] == 1 and after["LIVE_01"]["data_origin"] == "camera_scan"
    assert after["LIVE_01"]["baseline_building"]["label"] == "Baseline building (0/3)"


def test_replay_reset_keeps_live_crew(client):
    client.post("/api/v1/scan", json={"record": RECORD})
    client.post("/api/v1/admin/replay", json={"file": "qr_scan_demo_clean.csv", "speed": 1_000_000})
    t0 = time.time()
    while client.get("/api/v1/admin/replay").json()["state"] == "running" and time.time() - t0 < 10:
        time.sleep(0.02)
    assert core.score_latest("LIVE_01").reading_id == RECORD["session_id"]
    assert core.score_latest("CREW_01").reading_id == "QR_DEMO_0276"


def test_sos_uses_live_scan(client):
    client.post("/api/v1/scan", json={"record": RECORD})
    s = client.post("/api/v1/sos", json={"crew_id": "LIVE_01", "symptom": "breathing_difficulty"})
    assert s.status_code == 200, s.text
    j = s.json()
    snap = j["vitals_snapshot"]
    assert snap["reading_id"] == RECORD["session_id"] and snap["display_label"] == SCAN_CFG.display_label
    assert snap["metrics"]["spo2_pct"]["status"] == "missing" and snap["metrics"]["spo2_pct"]["value"] is None
    assert snap["metrics"]["resp_rate_bpm"]["value"] == 10.3
    assert j["score"]["reading_id"] == RECORD["session_id"]


@pytest.mark.parametrize("origin,allowed", [("https://dhruvachaitanya13.github.io", True),
                                            ("http://localhost:5173", True), ("http://127.0.0.1:8000", True),
                                            ("https://evil.example.com", False)])
def test_cors(client, origin, allowed):
    r = client.options("/api/v1/scan", headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                                 "Access-Control-Request-Headers": "content-type"})
    assert (r.headers.get("access-control-allow-origin") == origin) is allowed


def test_every_vital_reported():
    res = core.Engine(CFG).update(reading_from_scan(RECORD, None, SCAN_CFG, CFG))
    assert [i.metric for i in res.indicators][:len(VITAL_METRICS)] == list(VITAL_METRICS)


# ---------- latest_scan, breathing gate, per-origin confidence, persistence ----------
RECORD_2 = json.loads((FIXTURE.parent / "scan_record_sample_2.json").read_text())   # second real scan


def test_latest_scan_is_the_posted_record(client):
    assert core.latest_scan("LIVE_01") is None
    client.post("/api/v1/scan", json={"record": RECORD})
    assert core.latest_scan("LIVE_01") == RECORD
    client.post("/api/v1/scan", json={"record": RECORD_2})
    got = core.latest_scan("LIVE_01")
    assert got == RECORD_2
    got["verdict"] = "changed"                                   # a copy: callers can't edit Engine state
    assert core.latest_scan("LIVE_01")["verdict"] == RECORD_2["verdict"]
    assert core.latest_scan("CREW_01") is None and core.latest_scan("NOBODY") is None


def test_breathing_quality_gate():
    r2 = reading_from_scan(RECORD_2, None, SCAN_CFG, CFG)     # real record: breathing null anyway
    assert r2.resp_rate_bpm is None
    low = reading_from_scan(dict(RECORD, breathing_quality=0.34), None, SCAN_CFG, CFG)
    assert low.resp_rate_bpm is None
    assert low.scan_context["excluded"]["resp_rate_bpm"] == "breathing_quality 0.34 < 0.35"
    none = reading_from_scan(dict(RECORD, breathing_quality=None), None, SCAN_CFG, CFG)
    assert none.resp_rate_bpm is None and none.scan_context["excluded"]["resp_rate_bpm"] == "breathing_quality missing"
    ok = reading_from_scan(dict(RECORD, breathing_quality=0.35), None, SCAN_CFG, CFG)
    assert ok.resp_rate_bpm == 10.3 and ok.scan_context["breathing_quality"] == 0.35


def test_camera_confidence_counts_only_measurable_vitals():
    eng = core.Engine(CFG)
    res = [eng.update(reading_from_scan(scan(d, 11 + d % 2, hr=60 + d % 2), None, SCAN_CFG, CFG)) for d in range(4)]
    last = res[-1]
    assert last.baseline_quality == "ok" and last.baseline_building is None
    assert last.confidence == 1.0                     # HR + resp present, fresh, 3-scan baselines; BP etc. not counted
    ind = {i.metric: i for i in last.indicators}
    assert all(ind[m].status == "insufficient" for m in NOT_MEASURED)   # still reported as missing, never normal
    no_hr = eng.update(reading_from_scan(scan(4, 11), None, SCAN_CFG, CFG))   # HR missing
    assert no_hr.confidence == 0.25            # completeness 1/2 and the missing HR has no baseline n (§7.5)


def test_scans_persist_and_restore(client, tmp_path):
    f = tmp_path / "live_scans.jsonl"
    client.post("/api/v1/scan", json={"record": RECORD})
    client.post("/api/v1/scan", json={"record": RECORD})               # duplicate: not written again
    client.post("/api/v1/scan", json={"crew_id": "LIVE_02", "record": scan(1, 11)})
    client.post("/api/v1/scan", json={"crew_id": "CREW_01", "record": scan(2, 11)})   # 409: not written
    rows = [json.loads(x) for x in f.read_text().splitlines()]
    assert [(x["crew_id"], x["record"]["session_id"]) for x in rows] == [
        ("LIVE_01", RECORD["session_id"]), ("LIVE_02", "SIMULATED-scan-1")]
    assert rows[0]["record"] == RECORD

    before = core.score_latest("LIVE_01").to_dict()
    core.set_engine(core.Engine(CFG))                                 # "restart"
    from halo_agent.api import routes_scan
    assert routes_scan.load_persisted() == 2
    assert core.score_latest("LIVE_01").to_dict() == before and core.latest_scan("LIVE_01") == RECORD

    client.post("/api/v1/admin/replay", json={"file": "qr_scan_demo_clean.csv", "speed": 1_000_000, "reset": True})
    t0 = time.time()
    while client.get("/api/v1/admin/replay").json()["state"] == "running" and time.time() - t0 < 10:
        time.sleep(0.02)
    assert len(f.read_text().splitlines()) == 2 and core.score_latest("LIVE_01") is not None

    assert client.delete("/api/v1/admin/live-scans?crew_id=CREW_01").status_code == 409
    d = client.delete("/api/v1/admin/live-scans?crew_id=LIVE_02").json()
    assert d["cleared_crews"] == ["LIVE_02"] and d["removed_records"] == 1
    assert core.score_latest("LIVE_02") is None and core.score_latest("LIVE_01") is not None
    d = client.delete("/api/v1/admin/live-scans").json()
    assert d["cleared_crews"] == ["LIVE_01"] and d["removed_records"] == 1 and f.read_text() == ""
    assert core.score_latest("LIVE_01") is None and core.score_latest("CREW_01") is not None


def test_flat_body_is_accepted(client):
    r = client.post("/api/v1/scan", json={**RECORD, "crew_id": "LIVE_03"})   # what scripts/smoke_sos.py sends
    assert r.status_code == 200 and r.json()["crew_id"] == "LIVE_03"
    assert core.latest_scan("LIVE_03") == RECORD
