"""§8.6 live camera-scan data in the SOS: camera vitals with "Not measured by camera scan", scan_context,
live_data kept apart from replayed/simulated data (session view, export, ground reports), nudge baseline rule,
and the smoke script's scan scenario run against a stand-in for main's POST /api/v1/scan."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from halo_agent.api import routes_admin, routes_sos
from halo_agent.sos import live
from halo_agent.sos import service as service_mod
from halo_agent.sos.nudge import alert_baseline_quality
from halo_agent.sos.session_log import SessionLog

ROOT = Path(__file__).resolve().parents[1]
RECORD = json.loads((ROOT / "tests" / "fixtures" / "scan_record_sample.json").read_text())
NOW = "2026-11-14T22:40:12Z"                      # 5 min after the fixture's created_at


def camera_reading(record=RECORD, reading_id="LIVE_01_0001"):
    """What main's /scan is expected to put in the Engine for a live crew (camera can't do SpO2 / BP / temp)."""
    return {"reading_id": reading_id, "crew_id": "LIVE_01", "timestamp": record["created_at"],
            "heart_rate_bpm": record["heart_bpm"], "resp_rate_bpm": record["breathing_brpm"],
            "systolic_bp_mmhg": None, "diastolic_bp_mmhg": None, "body_temp_c": None, "spo2_pct": None,
            "blink_rate_per_min": record["blink_rate_per_min"], "measurement_origin": "camera_scan",
            "display_label": "", "measured_at": {"resp_rate_bpm": record["created_at"]},
            "metric_age_seconds": {"resp_rate_bpm": 300, "blink_rate_per_min": 300}, "age_seconds": 300}


DEMO = {"reading_id": "QR_DEMO_0276", "timestamp": "2026-11-16T09:05:00Z", "heart_rate_bpm": 100.0,
        "measurement_origin": "master_csv_playback_not_camera", "display_label": "Demo data - not measured from your camera",
        "metric_age_seconds": {"heart_rate_bpm": 0}, "age_seconds": 0}


@pytest.fixture
def env(client, tmp_path, monkeypatch):
    monkeypatch.delenv("ALERT_MODE", raising=False)
    h = {"reading": camera_reading(), "scan": RECORD}
    svc = service_mod.SosService(reading_fn=lambda c: h["reading"], scan_fn=lambda c: h["scan"],
                                 score_fn=lambda c: None, log=SessionLog(tmp_path), mode_fn=routes_admin.current_mode,
                                 now_fn=lambda: NOW)
    routes_sos.set_service(svc)
    yield {"c": client, "h": h, "svc": svc, "dir": tmp_path}
    routes_sos.set_service(None)


def start(c, symptom="other", crew="LIVE_01"):
    r = c.post("/api/v1/sos", json={"crew_id": crew, "symptom": symptom})
    assert r.status_code == 200, r.text
    return r.json()


def test_camera_vitals_missing_metrics_say_not_measured_by_camera(env):
    v = start(env["c"])["vitals_snapshot"]
    assert v["data_origin"] == "camera_scan" and v["display_label"] == "Live camera measurement (experimental)"
    for m in ("spo2_pct", "systolic_bp_mmhg", "diastolic_bp_mmhg", "body_temp_c"):
        assert v["metrics"][m] == {"value": None, "measured_at": None, "age_seconds": None, "status": "missing",
                                   "note": "Not measured by camera scan"}
    assert v["metrics"]["heart_rate_bpm"]["status"] == "missing"          # scan judged HR unreliable
    assert v["metrics"]["heart_rate_bpm"]["note"] is None                 # the camera can measure HR in principle
    assert v["metrics"]["resp_rate_bpm"] == {"value": 10.3, "measured_at": RECORD["created_at"],
                                             "age_seconds": 300, "status": "ok", "note": None}


def test_scan_context_summary(env):
    sc = start(env["c"])["scan_context"]
    assert sc == {"label": "Live camera measurement (experimental)", "data_origin": "camera_scan",
                  "scan_id": RECORD["session_id"], "scanned_at": RECORD["created_at"], "age_seconds": 300,
                  "heart_rate_bpm": None, "heart_quality": 0.15, "breathing_brpm": 10.3, "breathing_quality": 0.5,
                  "reaction_median_ms": 384.0, "verdict": "rest", "scan_grade": "C",
                  "notes": ["heart rate not reliable enough to report"]}


def test_scan_context_falls_back_to_reading_attribute_and_is_optional(env):
    env["h"]["scan"] = None
    env["h"]["reading"] = dict(camera_reading(), scan_context=RECORD)
    assert start(env["c"])["scan_context"]["verdict"] == "rest"
    env["h"]["reading"] = DEMO
    s = start(env["c"], crew="CREW_01")
    assert s["scan_context"] is None and s["live_data"] is None              # demo crew: no live block
    assert s["other_data"]["readings"][0]["reading_id"] == "QR_DEMO_0276"
    assert s["other_data"]["label"] == "Replayed demo or simulated data (not a live measurement)"


def test_live_and_other_data_kept_apart_in_view_export_and_ground_report(env):
    env["h"]["scan"] = None
    env["h"]["reading"] = DEMO
    s = start(env["c"], symptom="fainting")
    sid = s["session_id"]
    env["h"]["reading"], env["h"]["scan"] = camera_reading(), RECORD   # a live reading lands mid-session
    while s["step"]["id"] != "f7":                                     # f7 = report-level escalation
        s = env["c"].post(f"/api/v1/sos/{sid}/step", json={"action": "done"}).json()
    assert [r["reading_id"] for r in s["live_data"]["readings"]] == ["LIVE_01_0001"]
    assert s["live_data"]["readings"][0]["data_origin"] == "camera_scan"
    assert [x["scan_id"] for x in s["live_data"]["scans"]] == [RECORD["session_id"]]
    assert [r["reading_id"] for r in s["other_data"]["readings"]] == ["QR_DEMO_0276"]
    (rep,) = s["alert"]["reports"]
    assert rep["live_data"]["readings"][0]["reading_id"] == "LIVE_01_0001"
    assert rep["other_data"]["readings"][0]["reading_id"] == "QR_DEMO_0276"
    assert env["c"].get("/api/v1/sos/ground-reports").json()[0]["live_data"]["scans"][0]["verdict"] == "rest"
    env["c"].post(f"/api/v1/sos/{sid}/resolve", json={"outcome": "resolved"})
    exported = json.loads((env["dir"] / f"{sid}.json").read_text())
    assert exported["live_data"]["label"] == "Live camera measurement (experimental)"
    assert {r["data_origin"] for r in exported["live_data"]["readings"]} == {"camera_scan"}
    assert "camera_scan" not in {r["data_origin"] for r in exported["other_data"]["readings"]}


# ---------- nudge baseline rule ----------
def _score(rules, indicators=(), quality=None):
    sc = {"crew_id": "LIVE_01", "reading_id": "LIVE_01_0001", "status": "alert", "severity": 3,
          "reasons": [{"metric": m, "rule_id": r, "status": "alert", "text": f"{r}"} for m, r in rules],
          "indicators": list(indicators)}
    if quality:
        sc["baseline_quality"] = quality
    return sc


def test_alert_baseline_quality():
    hr_none = {"metric": "heart_rate_bpm", "baseline": None}
    hr_pool = {"metric": "heart_rate_bpm", "baseline": {"group": "pooled", "n": 20}}
    hr_ok = {"metric": "heart_rate_bpm", "baseline": {"group": "resting", "n": 30}}
    assert alert_baseline_quality(_score([("heart_rate_bpm", "z_band")], [hr_none])) == "insufficient"
    assert alert_baseline_quality(_score([("heart_rate_bpm", "z_band")], [hr_pool])) == "partial"
    assert alert_baseline_quality(_score([("heart_rate_bpm", "z_band")], [hr_ok])) == "ok"
    assert alert_baseline_quality(_score([("heart_rate_bpm", "z_band")], quality="insufficient")) == "insufficient"


def test_live_crew_nudge_needs_baseline_for_z_band_but_not_for_red_flags(env, tmp_path):
    from halo_agent.sos import nudge as nudge_mod
    pushed = []
    unsub = nudge_mod.register_on_nudge(pushed.append)
    try:
        ns = routes_sos.get_nudges()
        weak = {"metric": "heart_rate_bpm", "baseline": {"group": "pooled", "n": 14}}
        assert ns.on_score(_score([("heart_rate_bpm", "z_band")], [weak])) is None
        assert '"why": "z_band_baseline_partial"' in (tmp_path / "nudges.jsonl").read_text()
        ns.on_score({"crew_id": "LIVE_01", "status": "normal", "severity": 0, "reasons": []})
        n = ns.on_score(_score([("spo2_pct", "spo2_low")], [{"metric": "spo2_pct", "baseline": None}]))
        assert n is not None and n["trigger"]["kind"] == "red_flag" and [p["nudge_id"] for p in pushed] == [n["nudge_id"]]
    finally:
        unsub()


def test_z_band_with_good_baseline_still_nudges(env):
    ok = {"metric": "heart_rate_bpm", "baseline": {"group": "resting", "n": 40}}
    n = routes_sos.get_nudges().on_score(_score([("heart_rate_bpm", "z_band")], [ok]))
    assert n is not None and n["trigger"]["kind"] == "z_band"


# ---------- smoke script scan scenario against a stand-in /api/v1/scan ----------
def _smoke():
    spec = importlib.util.spec_from_file_location("smoke_sos", ROOT / "scripts" / "smoke_sos.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeScanEndpoint:
    """Wraps the TestClient; implements POST /api/v1/scan the way main is expected to (record -> live reading)."""
    def __init__(self, c, h):
        self.c, self.h, self.posted = c, h, []

    def post(self, path, json=None, **kw):
        if path == "/api/v1/scan":
            self.posted.append(json)
            self.h["reading"], self.h["scan"] = camera_reading(json), json
            return SimpleNamespace(status_code=201, json=lambda: {"accepted": True})
        return self.c.post(path, json=json, **kw)

    def __getattr__(self, name):
        return getattr(self.c, name)


def test_smoke_scan_scenario_passes_with_a_scan_endpoint(env, capsys):
    smoke = _smoke()
    env["h"]["reading"], env["h"]["scan"] = DEMO, None
    fake = FakeScanEndpoint(env["c"], env["h"])
    smoke.scan(fake)
    assert fake.posted[0]["crew_id"] == "LIVE_01" and fake.posted[0]["session_id"] == RECORD["session_id"]
    out = capsys.readouterr().out
    assert "FAIL" not in out and "Not measured by camera scan" in out


def test_smoke_scan_scenario_skips_without_endpoint(env):
    smoke = _smoke()
    with pytest.raises(smoke.Skip):
        smoke.scan(env["c"])
