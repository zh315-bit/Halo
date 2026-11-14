"""Phase 0: every Chris endpoint in AGENTS.md §6.1 returns JSON that validates against §6."""

from halo_agent.api import schemas as S

READING = {
    "reading_id": "QR_DEMO_0001", "crew_id": "CREW_01", "timestamp": "2026-11-15T10:10:00Z",
    "mission_day": 1, "activity": "resting", "heart_rate_bpm": 93.0, "resp_rate_bpm": 18.0,
    "systolic_bp_mmhg": 162.0, "diastolic_bp_mmhg": 58.0, "body_temp_c": 37.2, "spo2_pct": 97.0,
    "blink_rate_per_min": None, "new_reading": {"bp": True, "temp": False},
    "measurement_origin": "master_csv_playback_not_camera",
    "display_label": "Demo data - not measured from your camera",
}
DISCLAIMER = "Decision support only. Not a diagnosis."


def _ok(r, model):
    assert r.status_code == 200, r.text
    assert r.headers.get("X-HALO-Stub") == "true"
    return model.model_validate(r.json())


def test_score(client):
    out = _ok(client.post("/api/v1/score", json={"readings": [READING]}), S.ScoreResult)
    assert out.reading_id == "QR_DEMO_0001" and out.engine == "rules-v0"
    assert any(i.status == "insufficient" and i.value is None for i in out.indicators)  # missing stays missing


def test_score_ignores_unknown_request_fields(client):
    # M1 may post a whole CSV row: extra columns are dropped on requests (responses stay extra="forbid").
    row = dict(READING, scenario="baseline", step=0, bp_new_reading=True, data_context="composite_dataset_demo")
    assert client.post("/api/v1/score", json={"readings": [row], "also": 1}).status_code == 200


def test_morning_activity(client):
    r = client.post("/api/v1/activity/morning", json={"crew_id": "CREW_01", "completed_at": "2026-11-16T07:30:00Z"})
    assert r.status_code == 202 and r.json() == {"accepted": True, "report_date": "2026-11-16"}
    r = client.post("/api/v1/activity/morning", json={"crew_id": "CREW_01", "date": "2026-11-17", "x": 1})
    assert r.json()["report_date"] == "2026-11-17"
    assert client.post("/api/v1/activity/morning", json={"crew_id": "CREW_01", "date": "bad"}).status_code == 422


def test_sos_flow(client):
    s = _ok(client.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "breathing_difficulty"}),
            S.SosSession)
    assert s.step.index == 1 and s.disclaimer == DISCLAIMER and s.symptom_source == "picker"
    for action, idx in [("done", 2), ("cannot_do", 2), ("condition_changed", 1)]:
        n = _ok(client.post(f"/api/v1/sos/{s.session_id}/step", json={"action": action}), S.SosSession)
        assert n.step.index == idx
    assert client.post("/api/v1/sos", json={"crew_id": "CREW_01"}).status_code == 422


def test_sos_free_text_needs_confirmation(client):
    s = _ok(client.post("/api/v1/sos", json={"crew_id": "CREW_01", "free_text": "hard to breathe"}),
            S.SosSession)
    assert s.symptom_source == "llm"
    c = _ok(client.post(f"/api/v1/sos/{s.session_id}/classify", json={"free_text": "hard to breathe"}),
            S.ClassifyResponse)
    assert len(c.candidates) == 3 and c.requires_confirmation


def test_ask(client):
    a = _ok(client.post("/api/v1/agent/ask", json={"question": "why?", "session_id": "sos_001"}), S.AskResponse)
    assert a.citations and a.citations[0].kind == "checklist_step"
    a = _ok(client.post("/api/v1/agent/ask", json={"question": "why?"}), S.AskResponse)
    assert a.answer == "The checklist doesn't cover this — contact ground."


def test_daily_report(client):
    d = _ok(client.get("/api/v1/crew/CREW_01/daily-report", params={"date": "2026-11-16"}), S.DailyReport)
    assert d.date == "2026-11-16" and d.environment_context.is_personal_dose is False
    assert d.disclaimer == DISCLAIMER
    assert client.get("/api/v1/crew/CREW_01/daily-report", params={"date": "tomorrow"}).status_code == 422


def test_offline_toggle_propagates(client):
    r = client.post("/api/v1/admin/mode", json={"mode": "offline"})
    assert r.json() == {"mode": "offline", "source": "runtime_toggle"}
    s = client.post("/api/v1/sos", json={"crew_id": "CREW_01", "symptom": "breathing_difficulty"}).json()
    assert s["mode"] == "offline" and s["escalation"]["state"] == "queued_offline"
    assert client.post("/api/v1/sos/x/classify", json={"free_text": "t"}).json()["source"] == "picker_fallback"
    assert client.post("/api/v1/agent/ask", json={"question": "q"}).json()["answer"] == \
        "Not covered offline — follow the checklist"
    assert client.post("/api/v1/admin/mode", json={"mode": "online"}).json()["mode"] == "online"


def test_env_offline(client, monkeypatch):
    monkeypatch.setenv("HALO_OFFLINE", "1")
    assert client.get("/healthz").json()["mode"] == "offline"
