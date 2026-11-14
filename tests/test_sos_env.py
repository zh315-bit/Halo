"""mission_env: each checklist walked in all three environments; paths are written out by hand from
sos_checklists_v2.yaml (not derived by the code under test)."""

import pytest

from halo_agent.sos.protocols import END, load_all
from halo_agent.sos.state_machine import Session

CL = load_all()
MICRO, PARTIAL, POST = "microgravity", "partial_gravity", "post_landing"

DONE_PATHS = {
    "chest_pain": {MICRO: "c0 c1 c2 c3 c4 c5 c6 c7 c8", PARTIAL: "c0 c1g c2 c3 c4 c5 c6 c7 c8"},
    "breathing_difficulty": {MICRO: "b0 b1 b2 b3 b4 b5 b6 b7 b8 b9", PARTIAL: "b0 b2 b3 b4 b5 b6 b7 b8 b9"},
    "fainting": {MICRO: "f0 f1 f2 f3 f4 f5 f7", PARTIAL: "f0 f1g f2 f3g f4 f5 f6 f7"},
    "bleeding_injury": {MICRO: "h0 h1 h2 h3 h4 h5 h6 h7", PARTIAL: "h1 h2 h3 h4 h5 h6 h7"},
    "other": {MICRO: "o0 o1 o2 o3 o4 o5", PARTIAL: "o0 o2 o3 o4 o5"},
}
for p in DONE_PATHS.values():
    p[POST] = p[PARTIAL]          # every post_landing variant in v2 is shared with partial_gravity

ESCALATE_STEPS = {"chest_pain": {"c0"}, "breathing_difficulty": {"b0"}, "fainting": {"f7"},
                  "bleeding_injury": set(), "other": {"o5"}}


def session(symptom, env):
    s = Session("t", "CREW_01", CL[symptom], mission_env=env)
    s.report("t")
    s.start_alert("t")
    fired = s.start_guide("t")
    return s, fired


def walk(s, actions):
    """Apply actions; return visited step ids (incl. the start) and all escalation reasons."""
    seen, reasons = [s.current_step], []
    for a in actions:
        out = s.step(a, "t")
        reasons += out.escalate_reasons
        if s.current_step != seen[-1] or a == "condition_changed":
            seen.append(s.current_step)
    return seen, reasons


@pytest.mark.parametrize("symptom", list(DONE_PATHS))
@pytest.mark.parametrize("env", [MICRO, PARTIAL, POST])
def test_done_path_matches_file(symptom, env):
    s, fired = session(symptom, env)
    expected = DONE_PATHS[symptom][env].split()
    visited, reasons = [s.current_step], list(fired)
    while not s.checklist_complete:
        reasons += s.step("done", "t").escalate_reasons
        visited.append(s.current_step)
    assert visited[:-1] == expected and visited[-1] == END
    assert {r.split(":")[1] for r in reasons if r.startswith("escalate_step:")} == ESCALATE_STEPS[symptom]
    for sid in visited[:-1]:
        st = CL[symptom].step(sid)
        assert st.env is None or env in st.env, f"{sid} shown in {env}"


@pytest.mark.parametrize("env,path,fires", [
    (MICRO, ["c2", "c_cpr", "c8"], ["escalate_step:c_cpr"]),
    (PARTIAL, ["c2", "c_cpr_g", "c8"], ["escalate_step:c_cpr_g"]),
    (POST, ["c2", "c_cpr_g", "c8"], ["escalate_step:c_cpr_g"]),
])
def test_chest_no_pulse_branch_skips_by_env(env, path, fires):
    s, _ = session("chest_pain", env)
    while s.current_step != "c2":
        s.step("done", "t")
    seen, reasons = walk(s, ["condition_changed", "done"])
    assert seen == path and reasons == fires


def test_chest_recheck_loops_back_and_cpr_refires_every_time():
    s, _ = session("chest_pain", MICRO)
    while s.current_step != "c2":
        s.step("done", "t")
    _, r1 = walk(s, ["condition_changed", "done"])          # c2 -> c_cpr -> c8
    _, r2 = walk(s, ["condition_changed", "condition_changed", "done"])   # c8 -> c2 -> c_cpr -> c8
    assert r1 == ["escalate_step:c_cpr"] and r2 == ["escalate_step:c_cpr"]


@pytest.mark.parametrize("env,path", [
    (MICRO, ["h5", "h_severe", "h3"]),
    (PARTIAL, ["h5", "h_severe", "h_severe_g", "h3"]),
])
def test_bleeding_shock_branch(env, path):
    s, _ = session("bleeding_injury", env)
    while s.current_step != "h5":
        s.step("done", "t")
    seen, reasons = walk(s, ["condition_changed"] + ["done"] * (len(path) - 2))
    assert seen == path and reasons == ["escalate_step:h_severe"]


def test_cannot_do_target_and_escalate_target():
    s, _ = session("bleeding_injury", PARTIAL)
    while s.current_step != "h3":
        s.step("done", "t")
    seen, reasons = walk(s, ["cannot_do", "done"])            # h3 cannot_do -> h_tourniquet (escalate) -> h5
    assert seen == ["h3", "h_tourniquet", "h5"] and reasons == ["escalate_step:h_tourniquet"]
    s, _ = session("breathing_difficulty", MICRO)
    while s.current_step != "b8":
        s.step("done", "t")
    seen, reasons = walk(s, ["condition_changed", "cannot_do"])   # b8 -> b_wound; cannot_do ESCALATE -> b9
    assert seen == ["b8", "b_wound", "b9"] and reasons == ["cannot_do"]


@pytest.mark.parametrize("env,after", [(MICRO, "f7"), (PARTIAL, "f6"), (POST, "f6")])
def test_check_escalate_target_is_warning_sign_then_next(env, after):
    s, _ = session("fainting", env)
    while s.current_step != "f5":
        s.step("done", "t")
    out = s.step("condition_changed", "t")
    assert s.current_step == after
    assert out.escalate_reasons[0] == "warning_sign:f5"
    assert ("escalate_step:f7" in out.escalate_reasons) == (after == "f7")


@pytest.mark.parametrize("env,after", [(MICRO, "o1"), (PARTIAL, "o2")])
def test_other_unresponsive_escalates_and_continues(env, after):
    s, _ = session("other", env)
    out = s.step("condition_changed", "t")
    assert out.escalate_reasons == ["warning_sign:o0"] and s.current_step == after


def test_choke_then_cpr_in_microgravity():
    s, _ = session("breathing_difficulty", MICRO)
    while s.current_step != "b2":
        s.step("done", "t")
    seen, reasons = walk(s, ["condition_changed", "condition_changed", "done"])
    assert seen == ["b2", "b_choke", "b_cpr", "b9"]
    assert reasons == ["escalate_step:b_choke", "escalate_step:b_cpr"]


def test_switching_checklist_resolves_env_start():
    s, _ = session("other", PARTIAL)
    assert s.confirm_symptom(CL["bleeding_injury"], "t") == [] and s.current_step == "h1"
    s2, _ = session("other", MICRO)
    assert s2.confirm_symptom(CL["chest_pain"], "t") == ["escalate_step:c0"] and s2.current_step == "c0"


# ---------- API: /admin/mode switches the env for new sessions ----------
def test_admin_mode_switches_mission_env(client, tmp_path, monkeypatch):
    from halo_agent.api import routes_admin, routes_sos
    from halo_agent.sos import service as service_mod
    from halo_agent.sos.session_log import SessionLog
    from halo_agent.sos.vitals import CsvReplayVitals, load_rows
    rows = load_rows()
    routes_sos.set_service(service_mod.SosService(
        vitals=CsvReplayVitals(rows, clock=lambda: rows[-1]["ts"]), score_fn=lambda c: None,
        log=SessionLog(tmp_path), mode_fn=routes_admin.current_mode, env_fn=routes_admin.current_mission_env))
    try:
        r = client.post("/api/v1/admin/mode", json={}).status_code
        assert r == 422
        assert client.post("/api/v1/admin/mode", json={"mode": "online"}).json()["mission_env"] == "partial_gravity"
        a = client.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "bleeding_injury"}).json()
        assert a["mission_env"] == "partial_gravity" and a["step"]["id"] == "h1"
        j = client.post("/api/v1/admin/mode", json={"mission_env": "microgravity"}).json()
        assert j["mission_env"] == "microgravity" and j["mission_env_source"] == "runtime_toggle" and j["mode"] == "online"
        b = client.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "bleeding_injury"}).json()
        assert b["mission_env"] == "microgravity" and b["step"]["id"] == "h0"
        # "i of n" counts only this environment's steps: 11 in the file, minus h_severe_g (micro) / h0 (partial)
        assert b["step"]["index"] == 1 and b["step"]["total"] == 10
        assert a["step"]["index"] == 1 and a["step"]["total"] == 10
        assert client.get(f"/api/v1/sos/{a['session_id']}").json()["mission_env"] == "partial_gravity"   # fixed per session
        assert client.post("/api/v1/admin/mode", json={"mission_env": "mars"}).status_code == 422
    finally:
        routes_sos.set_service(None)
