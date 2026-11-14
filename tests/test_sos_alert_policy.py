"""Alert policy: combined opening message, same-reason cooldown, urgent vs report level."""

import copy
import json
from datetime import datetime, timedelta, timezone

import yaml

from halo_agent.api import routes_admin, routes_sos
from halo_agent.sos import service as service_mod
from halo_agent.sos.escalation import CONFIG, parse_config
from halo_agent.sos.session_log import SessionLog

RAW = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
T0 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)
FRESH = {"reading_id": "R1", "timestamp": "2026-11-15T12:00:00Z", "heart_rate_bpm": 80.0,
         "metric_age_seconds": {"heart_rate_bpm": 0}, "age_seconds": 0}   # fresh: no stale_vitals noise


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t.strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, s):
        self.t += timedelta(seconds=s)


def svc(tmp_path, env="partial_gravity", **cfg):
    d = copy.deepcopy(RAW)
    d.update(cfg)
    clock = Clock()
    return service_mod.SosService(reading_fn=lambda c: FRESH, score_fn=lambda c: None, log=SessionLog(tmp_path),
                                  rules=parse_config(d), now_fn=clock, env_fn=lambda: env), clock


def go(s, sid, *actions):
    out = None
    for a in actions:
        out, _ = s.step(sid, a)
    return out


def test_opening_alert_is_one_message_with_both_reasons(tmp_path):
    s, _ = svc(tmp_path)
    v, _ = s.start("CREW_01", "chest_pain")
    (m,) = v["alert"]["messages"]
    assert m["reasons"] == ["sos_reported", "escalate_step:c0"] and m["reason"] == "sos_reported"
    assert "sos_reported, escalate_step:c0" in m["body"]
    assert v["escalation"]["reasons"] == ["sos_reported", "escalate_step:c0"]
    v = go(s, v["session_id"], "done", "cannot_do")                # later escalations: one message each
    assert [x["reasons"] for x in v["alert"]["messages"]] == [["sos_reported", "escalate_step:c0"], ["cannot_do"]]


def test_same_reason_cooldown(tmp_path):
    s, clock = svc(tmp_path, env="microgravity", same_reason_cooldown_s=60)
    v, _ = s.start("CREW_01", "chest_pain")
    sid = v["session_id"]
    v = go(s, sid, "done", "done", "condition_changed")           # c0 c1 c2 -> c_cpr (fires)
    assert v["step"]["id"] == "c_cpr"
    v = go(s, sid, "done", "condition_changed", "condition_changed")   # c8 -> c2 -> c_cpr again, 0 s later
    assert v["step"]["id"] == "c_cpr"
    assert [m["reason"] for m in v["alert"]["messages"]].count("escalate_step:c_cpr") == 1
    assert any(e["action"] == "escalate_suppressed" and e["reason"] == "escalate_step:c_cpr" for e in v["events"])
    clock.advance(61)
    v = go(s, sid, "done", "condition_changed", "condition_changed")   # past the cooldown: sent again
    assert [m["reason"] for m in v["alert"]["messages"]].count("escalate_step:c_cpr") == 2


def test_shipped_cooldown_is_60_and_null_turns_it_off(tmp_path):
    assert svc(tmp_path)[0].rules.same_reason_cooldown_s == 60      # team_derived
    s, _ = svc(tmp_path / "off", same_reason_cooldown_s=None)
    v, _ = s.start("CREW_01", "other")
    v = go(s, v["session_id"], "cannot_do")
    v, _ = s.start("CREW_01", "chest_pain")
    v = go(s, v["session_id"], "done", "cannot_do", "cannot_do")
    assert [m["reason"] for m in v["alert"]["messages"]].count("cannot_do") == 2


def test_report_level_f7_logs_without_sms(tmp_path, client, monkeypatch):
    monkeypatch.delenv("ALERT_MODE", raising=False)
    s, _ = svc(tmp_path)
    s.mode_fn = routes_admin.current_mode
    routes_sos.set_service(s)
    try:
        v = client.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "fainting"}).json()
        sid = v["session_id"]
        while v["step"]["id"] != "f7":
            v = client.post(f"/api/v1/sos/{sid}/step", json={"action": "done"}).json()
        assert v["step"]["level"] == "report" and v["step"]["type"] == "escalate"
        assert [m["reasons"] for m in v["alert"]["messages"]] == [["sos_reported"]]      # no SMS for f7
        (r,) = v["alert"]["reports"]
        assert r["reason"] == "escalate_step:f7" and r["step_id"] == "f7" and r["text"] == "Reported to flight surgeon"
        assert v["alert"]["report_ui_message"] == "Reported to flight surgeon"
        assert v["escalation"]["state"] == "simulated"            # still the opening message's state
        assert any(e["action"] == "escalate" and e.get("level") == "report" for e in v["events"])
        assert client.get("/api/v1/sos/ground-reports").json()[0]["report_id"] == r["report_id"]
        queued = [json.loads(x) for x in (tmp_path / "ground_reports.jsonl").read_text().splitlines()]
        assert queued[0]["reason"] == "escalate_step:f7"
    finally:
        routes_sos.set_service(None)


def test_levels_configurable_in_escalation_yaml(tmp_path):
    s, _ = svc(tmp_path, step_levels={"chest_pain_v2.c0": "report"}, rule_levels={"cannot_do": "report"})
    v, _ = s.start("CREW_01", "chest_pain")
    assert [m["reasons"] for m in v["alert"]["messages"]] == [["sos_reported"]]
    v = go(s, v["session_id"], "done", "cannot_do")
    assert len(v["alert"]["messages"]) == 1
    assert [r["reason"] for r in v["alert"]["reports"]] == ["escalate_step:c0", "cannot_do"]
    s2, _ = svc(tmp_path / "b", step_levels={"fainting_v2.f7": "urgent"})   # override the checklist's report
    v, _ = s2.start("CREW_01", "fainting")
    while v["step"]["id"] != "f7":
        v = go(s2, v["session_id"], "done")
    assert v["alert"]["reports"] == [] and v["alert"]["messages"][-1]["reason"] == "escalate_step:f7"
