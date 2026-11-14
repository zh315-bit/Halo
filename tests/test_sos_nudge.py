"""§8.5 proactive nudge: derived only from checklist red_flags; transition-triggered; rate limit, snooze."""

import copy
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from halo_agent.api import routes_admin, routes_sos
from halo_agent.sos import service as service_mod
from halo_agent.sos.nudge import CONFIG, NudgeConfigError, parse_config, suggest
from halo_agent.sos.protocols import load_all
from halo_agent.sos.session_log import SessionLog

CL = load_all()
RAW = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
CFG = parse_config(RAW, CL)


def score(status="alert", *rules, reading_id="R1"):
    reasons = [{"rule_id": r, "status": "alert", "text": f"{r} text"} for r in rules]
    if status == "alert" and not rules:
        reasons = [{"rule_id": "z_band", "status": "alert", "text": "HR is 3.2 robust SD above your baseline."}]
    return {"reading_id": reading_id, "as_of": "2026-11-15T12:00:00Z", "status": status,
            "severity": {"normal": 0, "caution": 1, "alert": 3}[status], "reasons": reasons}


class Clock:
    def __init__(self):
        self.t = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def env(tmp_path, client, monkeypatch):
    monkeypatch.delenv("ALERT_MODE", raising=False)
    h = {"score": None}
    clock = Clock()
    svc = service_mod.SosService(reading_fn=lambda c: None, score_fn=lambda c: h["score"], log=SessionLog(tmp_path),
                                 mode_fn=routes_admin.current_mode, now_fn=clock)
    routes_sos.set_service(svc)
    yield {"c": client, "h": h, "clock": clock, "svc": svc, "dir": tmp_path}
    routes_sos.set_service(None)


def poll(c, crew="CREW_01"):
    r = c.get(f"/api/v1/crew/{crew}/nudges")
    assert r.status_code == 200, r.text
    return r.json()["nudges"]


def symptoms(n):
    return [s["symptom"] for s in n["suggestions"]]


# ---------- derivation ----------
@pytest.mark.parametrize("fired,expected", [
    (["spo2_low"], ["breathing_difficulty", "chest_pain", "other"]),   # fainting also matches, capped at 2
    (["bp_crisis"], ["chest_pain", "other"]),
    (["fever"], ["other"]),                                           # only `other` lists fever
    (["spo2_low", "bp_crisis"], ["breathing_difficulty", "chest_pain", "other"]),
    ([], ["other"]),
])
def test_suggestions_come_from_red_flags(fired, expected):
    out = suggest(fired, CL, CFG)
    assert [s["symptom"] for s in out] == expected
    for s in out:
        assert set(s["because"]) <= set(CL[s["symptom"]].red_flags)         # every link exists in the checklist


def test_config_cannot_add_a_new_link():
    bad = copy.deepcopy(RAW)
    bad["priority"]["bp_crisis"] = ["chest_pain", "bleeding_injury"]       # bleeding has no bp_crisis red flag
    with pytest.raises(NudgeConfigError, match="no new links"):
        parse_config(bad, CL)
    with pytest.raises(NudgeConfigError):
        parse_config(dict(RAW, max_suggestions=0), CL)


# ---------- trigger ----------
def test_z_band_alert_nudges_other_only(env):
    env["h"]["score"] = score("alert")
    (n,) = poll(env["c"])
    assert n["text"] == "Check on your crewmate" and symptoms(n) == ["other"]
    assert n["trigger"]["kind"] == "z_band" and n["trigger"]["red_flags"] == []
    assert n["trigger"]["reasons"] == ["HR is 3.2 robust SD above your baseline."]
    assert n["disclaimer"] == "Decision support only. Not a diagnosis."


def test_red_flag_alert_nudges_ranked_checklists(env):
    env["h"]["score"] = score("alert", "spo2_low")
    (n,) = poll(env["c"])
    assert symptoms(n) == ["breathing_difficulty", "chest_pain", "other"]
    assert n["trigger"]["kind"] == "red_flag" and n["text"] == "Check on your crewmate. Red flag: spo2_low."
    assert n["suggestions"][0]["review_label"] == "Sourced, not medically reviewed"


def test_only_the_transition_into_alert_creates_a_nudge(env):
    env["h"]["score"] = score("caution")
    assert poll(env["c"]) == []
    env["h"]["score"] = score("alert", "bp_crisis")
    (n,) = poll(env["c"])
    assert poll(env["c"]) == [n]                       # still alert: same nudge, nothing new
    assert len(routes_sos.get_nudges().nudges) == 1


def test_min_interval_and_active_nudge_suppress(env):
    env["h"]["score"] = score("alert")
    (n,) = poll(env["c"])
    env["c"].post(f"/api/v1/nudges/{n['nudge_id']}/dismiss")
    env["h"]["score"] = score("normal")
    poll(env["c"])
    env["clock"].t += timedelta(seconds=60)
    env["h"]["score"] = score("alert", "spo2_low")
    assert poll(env["c"]) == []                        # within min_interval_s (300)
    env["h"]["score"] = score("normal"); poll(env["c"])
    env["clock"].t += timedelta(seconds=300)
    env["h"]["score"] = score("alert", "spo2_low")
    assert len(poll(env["c"])) == 1
    log = (env["dir"] / "nudges.jsonl").read_text()
    assert '"why": "min_interval"' in log


def test_snooze_holds_back_and_expires(env):
    env["h"]["score"] = score("alert")
    (n,) = poll(env["c"])
    assert env["c"].post(f"/api/v1/nudges/{n['nudge_id']}/snooze", json={"minutes": 7}).status_code == 422
    s = env["c"].post(f"/api/v1/nudges/{n['nudge_id']}/snooze", json={"minutes": 15}).json()
    assert s["state"] == "snoozed" and s["snoozed_until"] == "2026-11-15T12:15:00Z"
    assert poll(env["c"]) == []
    env["h"]["score"] = score("normal"); poll(env["c"])
    env["clock"].t += timedelta(minutes=10)
    env["h"]["score"] = score("alert", "bp_crisis")
    assert poll(env["c"]) == []                        # new alert while snoozed: held back
    env["clock"].t += timedelta(minutes=6)
    (back,) = poll(env["c"])                           # snooze over: the snoozed nudge is active again
    assert back["nudge_id"] == n["nudge_id"] and back["state"] == "active"


def test_accept_opens_suggested_checklist_and_open_sos_suppresses(env):
    env["h"]["score"] = score("alert", "spo2_low")
    (n,) = poll(env["c"])
    nid = n["nudge_id"]
    assert env["c"].post(f"/api/v1/nudges/{nid}/accept", json={"symptom": "bleeding_injury"}).status_code == 422
    r = env["c"].post(f"/api/v1/nudges/{nid}/accept", json={"symptom": "breathing_difficulty"}).json()
    assert r["nudge"]["state"] == "accepted" and r["session"]["protocol_id"] == "breathing_difficulty_v2"
    assert r["session"]["note"] == f"from nudge {nid}" and r["nudge"]["session_id"] == r["session"]["session_id"]
    assert env["c"].post(f"/api/v1/nudges/{nid}/dismiss").status_code == 409
    env["h"]["score"] = score("normal"); poll(env["c"])
    env["clock"].t += timedelta(hours=1)
    env["h"]["score"] = score("alert")
    assert poll(env["c"]) == []                        # SOS session still open for this crew
    assert env["c"].post("/api/v1/nudges/nope/dismiss").status_code == 404


def test_no_score_no_nudge(env):
    assert poll(env["c"]) == []
