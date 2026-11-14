"""SOS session flow through the API: state machine, 409s, export, vitals display, optional score."""

import json
from itertools import product

import pytest

from halo_agent.api import routes_admin, routes_sos
from halo_agent.sos import service as service_mod
from halo_agent.sos.protocols import load_all
from halo_agent.sos.session_log import SessionLog
from halo_agent.sos.state_machine import (OVERLAYS, STATES, TRANSITIONS, IllegalTransition, Session)
from halo_agent.sos.vitals import snapshot

READING = {   # shape of core.LatestReading (values from QR_DEMO_0276)
    "reading_id": "QR_DEMO_0276", "crew_id": "CREW_01", "timestamp": "2026-11-16T09:05:00Z",
    "heart_rate_bpm": 100.0, "resp_rate_bpm": 20.0, "systolic_bp_mmhg": 189.0, "diastolic_bp_mmhg": 72.0,
    "body_temp_c": 37.3, "spo2_pct": 98.0, "blink_rate_per_min": None,
    "measurement_origin": "master_csv_playback_not_camera", "display_label": "Demo data - not measured from your camera",
    "measured_at": {"heart_rate_bpm": "2026-11-16T09:05:00Z", "resp_rate_bpm": "2026-11-16T09:00:00Z",
                    "systolic_bp_mmhg": "2026-11-16T09:00:00Z", "diastolic_bp_mmhg": "2026-11-16T09:00:00Z",
                    "body_temp_c": "2026-11-16T08:00:00Z", "spo2_pct": "2026-11-16T09:00:00Z"},
    "metric_age_seconds": {"heart_rate_bpm": 0, "resp_rate_bpm": 300, "systolic_bp_mmhg": 300,
                           "diastolic_bp_mmhg": 300, "body_temp_c": 3900, "spo2_pct": 300},
    "age_seconds": 0,
}


def aged(r, seconds):
    """The same reading `seconds` later (what core.latest_reading reports as time passes)."""
    return dict(r, age_seconds=r["age_seconds"] + seconds,
                metric_age_seconds={k: v + seconds for k, v in r["metric_age_seconds"].items()})


def score_for(r, **kw):
    return {"reading_id": r["reading_id"], "status": "normal", "severity": 0, "reasons": [], **kw}


@pytest.fixture
def env(tmp_path, monkeypatch, client):
    monkeypatch.delenv("ALERT_MODE", raising=False)
    holder = {"score": None, "reading": READING}
    svc = service_mod.SosService(reading_fn=lambda c: holder["reading"], score_fn=lambda c: holder["score"],
                                 log=SessionLog(tmp_path), mode_fn=routes_admin.current_mode)
    routes_sos.set_service(svc)
    yield {"c": client, "svc": svc, "score": holder, "h": holder, "dir": tmp_path}
    routes_sos.set_service(None)


def start(c, symptom="chest_pain", **kw):
    r = c.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": symptom, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def step(c, s, action, **kw):
    return c.post(f"/api/v1/sos/{s['session_id']}/step", json={"action": action, **kw})


# ---------- full walks ----------
@pytest.mark.parametrize("symptom", ["chest_pain", "breathing_difficulty", "fainting", "bleeding_injury", "other"])
def test_every_problem_type_walks_to_resolved(env, symptom):
    c = env["c"]
    s = start(c, symptom)
    assert s["state"] == "GUIDING" and s["step"]["index"] == 1
    assert s["review_status"] == "sourced_unreviewed" and s["review_label"] == "Sourced, not medically reviewed"
    assert s["mission_env"] == "partial_gravity" and s["show_vitals"]
    assert [e["action"] for e in s["events"][:4]] == ["report", "start_alert", "start_guide", "escalate"]
    n = 0
    while not s["checklist_complete"]:
        s = step(c, s, "done", step_id=s["step"]["id"]).json()
        n += 1
    assert s["step"]["id"] == "END" and s["step"]["options"] == [] and len(s["steps_done"]) == n
    s = c.post(f"/api/v1/sos/{s['session_id']}/resolve", json={}).json()
    assert s["state"] == "RESOLVED" and s["ended_at"]
    exported = json.loads((env["dir"] / f"{s['session_id']}.json").read_text())
    assert exported["state"] == "RESOLVED" and exported["events"] == s["events"]
    jsonl = (env["dir"] / f"{s['session_id']}.jsonl").read_text().splitlines()
    assert [json.loads(x)["seq"] for x in jsonl] == list(range(1, len(s["events"]) + 1))


def test_step_view_shows_sources_ref_note(env):
    s = start(env["c"], "chest_pain")
    st = s["step"]
    assert st["id"] == "c0" and st["type"] == "escalate" and st["ref"] == "JSC-48031 p.1-36 (contact Surgeon)"
    assert [x["key"] for x in st["sources"]] == ["nasa_medcl", "mlp_heart_attack"]
    assert all(x["title"] and x["url"].startswith("https://") for x in st["sources"])
    assert "escalate_step:c0" in s["escalation"]["reasons"]
    for _ in range(5):                                            # c0 c1g c2 c3 c4 -> c5
        s = step(env["c"], s, "done").json()
    assert s["step"]["id"] == "c5" and s["step"]["note"].startswith("HALO shows the latest vitals")
    s = step(env["c"], s, "done").json()                          # c6: team_policy is not medical
    assert {x["key"]: x["medical"] for x in s["step"]["sources"]} == {"team_policy": False, "nasa_medcl": True}
    s = step(env["c"], step(env["c"], s, "done").json(), "done").json()      # c8
    assert "[N — TBD]" in s["step"]["text"]


def test_cannot_do_escalates_and_checklist_continues(env):
    c = env["c"]
    s = start(c, "chest_pain")
    s = step(c, s, "done").json()                   # c0 -> c1g (partial_gravity)
    assert s["step"]["id"] == "c1g"
    s = step(c, s, "cannot_do").json()              # c1g cannot_do -> escalate, continue at c2
    assert s["step"]["id"] == "c2" and s["state"] == "GUIDING"
    assert [m["reasons"] for m in s["alert"]["messages"]] == [["sos_reported", "escalate_step:c0"], ["cannot_do"]]
    s = step(c, s, "cannot_do").json()              # a second cannot_do sends a second message
    assert [m["reason"] for m in s["alert"]["messages"]].count("cannot_do") == 2


def test_cannot_do_routes_to_step_without_cannot_do_reason(env):
    c = env["c"]
    s = start(c, "bleeding_injury")                  # h1 (h0 is microgravity only)
    for _ in range(2):
        s = step(c, s, "done").json()                # h1 -> h2 -> h3
    s = step(c, s, "cannot_do").json()               # h3 on_cannot_do: h_tourniquet (an escalate step)
    assert s["step"]["id"] == "h_tourniquet"
    assert "cannot_do" not in s["escalation"]["reasons"] and "escalate_step:h_tourniquet" in s["escalation"]["reasons"]


def test_check_warning_sign_escalates(env):
    c = env["c"]
    s = start(c, "other")
    assert s["step"]["type"] == "check" and s["step"]["on_condition_changed_means"] == "warning_sign"
    s = step(c, s, "condition_changed").json()
    assert s["step"]["id"] == "o2" and "warning_sign:o0" in s["escalation"]["reasons"]


def test_unresolved_after_step_fires_once():
    from halo_agent.sos.protocols import parse_checklist, parse_sources
    from halo_agent.sos.state_machine import Session as S
    src = parse_sources({"sources": {"a": {"title": "A", "url": None, "kind": "x"}}})
    cl = parse_checklist({"protocol_id": "u", "symptom": "other", "title": "U", "escalate_if_unresolved_after": "u2",
                          "steps": [{"id": f"u{i}", "type": "action", "text": "t", "source": "a"} for i in (1, 2, 3)]},
                         src)
    s = S("x", "CREW_01", cl)
    s.report("t"); s.start_alert("t"); s.start_guide("t")
    reasons = [r for _ in range(3) for r in s.step("done", "t").escalate_reasons]
    assert reasons == ["unresolved_after_step"]


def test_condition_changed_stays_and_refreshes_vitals(env):
    c = env["c"]
    s = start(c)
    for _ in range(3):
        s = step(c, s, "done").json()                # c0 c1g c2 -> c3 (action, no on_condition_changed)
    s = step(c, s, "condition_changed").json()
    assert s["step"]["id"] == "c3"
    assert any(e["action"] == "vitals_refresh" for e in s["events"])
    s = step(c, s, "skip").json()
    assert s["step"]["id"] == "c4" and s["steps_done"][-1]["action"] == "skip"


def test_hand_off(env):
    c = env["c"]
    s = start(c)
    s = c.post(f"/api/v1/sos/{s['session_id']}/resolve", json={"outcome": "handed_off"}).json()
    assert s["state"] == "HANDED_OFF"


# ---------- 409 / 404 / 422 ----------
def test_illegal_transitions_return_409(env):
    c = env["c"]
    s = start(c)
    sid = s["session_id"]
    assert step(c, s, "done", step_id="c3").status_code == 409            # stale double tap (current c0)
    c.post(f"/api/v1/sos/{sid}/resolve", json={})
    assert step(c, s, "done").status_code == 409                          # after RESOLVED
    assert c.post(f"/api/v1/sos/{sid}/resolve", json={}).status_code == 409
    s2 = start(c, "other")
    while not s2["checklist_complete"]:
        s2 = step(c, s2, "done").json()
    assert step(c, s2, "done").status_code == 409                         # checklist complete
    assert c.get("/api/v1/sos/nope").status_code == 404
    assert step(c, {"session_id": "nope"}, "done").status_code == 404
    assert c.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "headache"}).status_code == 422
    assert step(c, s2, "explode").status_code == 422


def test_transition_table_is_exhaustive():
    cl = load_all()["other"]
    actions = {a for _, a in TRANSITIONS}
    for state, action in product(STATES, actions):
        s = Session("x", "CREW_01", cl)
        s.state, s.current_step = state, cl.start
        if (state, action) in TRANSITIONS:
            continue
        with pytest.raises(IllegalTransition):
            if action in ("done", "cannot_do", "condition_changed", "skip"):
                s.step(action, "t")
            elif action == "start_alert":
                s.start_alert("t")
            elif action == "start_guide":
                s.start_guide("t")
            else:
                s.end({"resolve": "resolved", "hand_off": "handed_off"}[action], "t", "crew")
    assert "acknowledge" in OVERLAYS and "REPORTED" not in OVERLAYS["acknowledge"]


def test_ack_is_overlay_and_idempotent(env):
    c = env["c"]
    s = start(c)
    sid = s["session_id"]
    a = c.post(f"/api/v1/sos/{sid}/ack", json={"by": "capcom"}).json()
    assert a["state"] == "GUIDING" and a["escalation"]["state"] == "acknowledged"
    assert a["alert"]["ack_by"] == "capcom" and a["alert"]["acknowledged_at"]
    b = c.post(f"/api/v1/sos/{sid}/ack", json={"by": "someone"}).json()
    assert b["alert"]["ack_by"] == "capcom" and len(b["events"]) == len(a["events"])
    assert step(c, s, "done").json()["step"]["id"] == "c1g"               # ack does not interrupt guidance


# ---------- vitals display ----------
def test_vitals_ages_stale_and_missing(env):
    c = env["c"]
    v = start(c)["vitals_snapshot"]
    m = v["metrics"]
    assert v["reading_id"] == "QR_DEMO_0276" and v["display_label"] == "Demo data - not measured from your camera"
    assert m["heart_rate_bpm"]["status"] == "ok" and m["heart_rate_bpm"]["age_seconds"] == 0
    assert m["systolic_bp_mmhg"]["measured_at"] == "2026-11-16T09:00:00Z"      # last NEW bp, not the carried row
    assert m["body_temp_c"]["status"] == "stale"                                # last new temp 08:00
    assert m["blink_rate_per_min"] == {"value": None, "measured_at": None, "age_seconds": None, "status": "missing"}
    env["h"]["reading"] = aged(READING, 11 * 60)
    v = start(c)["vitals_snapshot"]
    assert v["stale"] is True and v["metrics"]["heart_rate_bpm"]["status"] == "stale"


def test_missing_values_stay_missing():
    v = snapshot(dict(READING, spo2_pct=None, heart_rate_bpm=float("nan")))
    assert v["metrics"]["spo2_pct"]["status"] == "missing" and v["metrics"]["spo2_pct"]["value"] is None
    assert v["metrics"]["heart_rate_bpm"]["status"] == "missing"
    unknown_age = snapshot(dict(READING, metric_age_seconds={}))
    assert all(m["status"] in ("stale", "missing") for m in unknown_age["metrics"].values())   # never "ok"


def test_no_reading_is_all_missing_and_flow_continues(env):
    env["h"]["reading"] = None
    env["score"]["score"] = score_for(READING, status="alert", severity=3)   # orphan score is not shown
    s = start(env["c"])
    v = s["vitals_snapshot"]
    assert v["reading_id"] is None and all(m["status"] == "missing" for m in v["metrics"].values())
    assert s["score"] is None and s["state"] == "GUIDING"
    assert step(env["c"], s, "done").status_code == 200


def test_score_must_belong_to_displayed_reading(env):
    env["score"]["score"] = score_for(dict(READING, reading_id="QR_DEMO_0275"), status="alert", severity=3)
    s = start(env["c"])
    assert s["vitals_snapshot"]["reading_id"] == "QR_DEMO_0276" and s["score"] is None


def test_reading_and_score_realign_after_a_race(env):
    seq = iter([READING, dict(READING, reading_id="QR_DEMO_0277"), dict(READING, reading_id="QR_DEMO_0277")])
    env["svc"].reading_fn = lambda c: next(seq)
    env["svc"].score_fn = lambda c: score_for(dict(READING, reading_id="QR_DEMO_0277"), status="caution", severity=1)
    s = start(env["c"])            # 1st read 0276 vs score 0277 -> retry -> both 0277
    assert s["vitals_snapshot"]["reading_id"] == s["score"]["reading_id"] == "QR_DEMO_0277"


def test_live_engine_reading_and_score_share_reading_id(tmp_path, monkeypatch, client):
    core = pytest.importorskip("halo_agent.core")
    if not hasattr(core, "latest_reading"):
        pytest.skip("core.latest_reading not merged from main yet")
    from halo_agent.config_loader import REPO_ROOT, load_scoring_config
    from halo_agent.core.csv_source import load_csv
    monkeypatch.setenv("ALERT_MODE", "dry_run")
    eng, old = core.Engine(load_scoring_config()), core.get_engine()
    core.set_engine(eng)
    routes_sos.set_service(service_mod.SosService(log=SessionLog(tmp_path), mode_fn=routes_admin.current_mode))
    try:
        rows = load_csv(REPO_ROOT / "data" / "qr_scan_demo_clean.csv", eng.cfg)
        for i, r in enumerate(rows):
            eng.update(r)
            if i in (100, len(rows) - 1):        # mid-replay and at the end
                s = client.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "other"}).json()
                assert s["vitals_snapshot"]["reading_id"] == r.reading_id
                assert s["score"] is not None and s["score"]["reading_id"] == r.reading_id
        assert s["score"]["status"] == "alert" and s["score"]["severity"] == 3
    finally:
        core.set_engine(old)
        routes_sos.set_service(None)


# ---------- optional score ----------
def test_score_none_keeps_flow(env):
    s = start(env["c"])
    assert s["score"] is None and s["score_reasons"] == []
    assert step(env["c"], s, "done").status_code == 200


def test_score_severity_and_red_flag_escalate(env):
    env["score"]["score"] = score_for(READING, status="alert", severity=3, reasons=[
        {"rule_id": "spo2_low", "status": "alert", "text": "SpO2 below red flag"}])
    s = start(env["c"], "breathing_difficulty")
    assert s["score"] == {"status": "alert", "severity": 3, "reading_id": "QR_DEMO_0276"}
    assert s["escalation"]["reasons"] == ["sos_reported", "severity_max", "red_flag:spo2_low", "escalate_step:b0"]
    assert s["score_reasons"] == ["SpO2 below red flag"]
    (opening,) = s["alert"]["messages"]       # one combined opening message
    assert opening["reasons"] == ["sos_reported", "severity_max", "red_flag:spo2_low", "escalate_step:b0"]
    s = step(env["c"], s, "done").json()      # same conditions again: not re-sent (b0 -> b2, no escalate step)
    assert len(s["alert"]["messages"]) == 1
    s2 = start(env["c"], "bleeding_injury")   # spo2_low is not in this checklist's red_flags
    assert "red_flag:spo2_low" not in s2["escalation"]["reasons"]


def test_bad_or_failing_score_is_null(env):
    env["score"]["score"] = score_for(READING, status="weird", severity=9)
    assert start(env["c"])["score"] is None
    env["svc"].score_fn = lambda c: 1 / 0
    assert start(env["c"])["score"] is None


def test_default_score_fn_tolerates_missing_core():
    # core.score_latest is owned by the scoring window; until it exists (or when it returns None) -> None.
    out = service_mod.default_score_fn("CREW_01")
    assert out is None or isinstance(out, dict)


def test_very_stale_vitals_escalate(env):
    env["h"]["reading"] = aged(READING, 6 * 3600)
    assert "stale_vitals" in start(env["c"])["escalation"]["reasons"]


def test_free_text_starts_generic_checklist_now(env):
    s = env["c"].post("/api/v1/sos", json={"crew_id": "CREW_01", "free_text": "feel strange"}).json()
    assert s["symptom"] == "other" and s["note"] == "feel strange" and s["state"] == "GUIDING"


def test_sos_does_not_import_shell():
    import ast
    from pathlib import Path
    root = Path(service_mod.__file__).parent
    for f in list(root.glob("*.py")) + list((root.parent / "notify").glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text())):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
            assert not any("shell" in n.split(".") or n.startswith(("anthropic", "openai")) for n in names), f
