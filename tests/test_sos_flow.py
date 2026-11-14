"""SOS session flow through the API: state machine, 409s, export, vitals display, optional score."""

import json
from datetime import timedelta
from itertools import product

import pytest

from halo_agent.api import routes_admin, routes_sos
from halo_agent.sos import service as service_mod
from halo_agent.sos.protocols import load_all
from halo_agent.sos.session_log import SessionLog
from halo_agent.sos.state_machine import (OVERLAYS, STATES, TRANSITIONS, IllegalTransition, Session)
from halo_agent.sos.vitals import CsvReplayVitals, load_rows, parse_iso

ROWS = load_rows()
LAST = ROWS[-1]["ts"]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path, monkeypatch, client):
    monkeypatch.delenv("ALERT_MODE", raising=False)
    clock = Clock(LAST)
    holder = {"score": None}
    svc = service_mod.SosService(vitals=CsvReplayVitals(ROWS, clock=clock), score_fn=lambda c: holder["score"],
                                 log=SessionLog(tmp_path), mode_fn=routes_admin.current_mode)
    routes_sos.set_service(svc)
    yield {"c": client, "svc": svc, "clock": clock, "score": holder, "dir": tmp_path}
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
    assert [e["action"] for e in s["events"][:3]] == ["report", "start_alert", "escalate"]
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
    assert [m["reason"] for m in s["alert"]["messages"]] == ["sos_reported", "escalate_step:c0", "cannot_do"]
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
    env["clock"].t = LAST + timedelta(minutes=11)
    v = start(c)["vitals_snapshot"]
    assert v["stale"] is True and v["metrics"]["heart_rate_bpm"]["status"] == "stale"


def test_missing_values_stay_missing(tmp_path):
    rows = load_rows()[:3]
    rows[-1] = dict(rows[-1], spo2_pct=None, heart_rate_bpm=None)
    v = CsvReplayVitals(rows, clock=Clock(rows[-1]["ts"])).snapshot("CREW_01")
    assert v["metrics"]["spo2_pct"]["status"] == "missing" and v["metrics"]["spo2_pct"]["value"] is None
    assert v["metrics"]["heart_rate_bpm"]["status"] == "missing"


def test_nan_in_csv_is_missing(tmp_path):
    src = (load_rows.__globals__["DATA"]).read_text().splitlines()
    head, first = src[0], src[1].split(",")
    first[head.split(",").index("spo2_pct")] = "nan"
    p = tmp_path / "x.csv"
    p.write_text(head + "\n" + ",".join(first) + "\n")
    rows = load_rows(p)
    assert CsvReplayVitals(rows, clock=Clock(rows[0]["ts"])).snapshot("CREW_01")["metrics"]["spo2_pct"]["status"] == "missing"


def test_no_look_ahead_and_unknown_crew():
    mid = parse_iso("2026-11-15T12:00:00Z")
    v = CsvReplayVitals(ROWS, clock=Clock(mid)).snapshot("CREW_01")
    assert parse_iso(v["timestamp"]) <= mid
    u = CsvReplayVitals(ROWS, clock=Clock(LAST)).snapshot("CREW_99")
    assert u["reading_id"] is None and all(x["status"] == "missing" for x in u["metrics"].values())


# ---------- optional score ----------
def test_score_none_keeps_flow(env):
    s = start(env["c"])
    assert s["score"] is None and s["score_reasons"] == []
    assert step(env["c"], s, "done").status_code == 200


def test_score_severity_and_red_flag_escalate(env):
    env["score"]["score"] = {"status": "alert", "severity": 3, "reasons": [
        {"rule_id": "spo2_low", "status": "alert", "text": "SpO2 below red flag"}]}
    s = start(env["c"], "breathing_difficulty")
    assert s["score"] == {"status": "alert", "severity": 3}
    assert s["escalation"]["reasons"] == ["sos_reported", "severity_max", "red_flag:spo2_low", "escalate_step:b0"]
    assert s["score_reasons"] == ["SpO2 below red flag"]
    s = step(env["c"], s, "done").json()      # same conditions again: not re-sent (b0 -> b2, no escalate step)
    assert len(s["alert"]["messages"]) == 4
    s2 = start(env["c"], "bleeding_injury")   # spo2_low is not in this checklist's red_flags
    assert "red_flag:spo2_low" not in s2["escalation"]["reasons"]


def test_bad_or_failing_score_is_null(env):
    env["score"]["score"] = {"status": "weird", "severity": 9}
    assert start(env["c"])["score"] is None
    env["svc"].score_fn = lambda c: 1 / 0
    assert start(env["c"])["score"] is None


def test_default_score_fn_tolerates_missing_core():
    # core.score_latest is owned by the scoring window; until it exists (or when it returns None) -> None.
    out = service_mod.default_score_fn("CREW_01")
    assert out is None or isinstance(out, dict)


def test_very_stale_vitals_escalate(env):
    env["clock"].t = LAST + timedelta(hours=6)
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
